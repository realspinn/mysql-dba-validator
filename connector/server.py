from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import socket
import ssl
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pymysql
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from backend.analyzer import analyze_batch
from backend.db import validate_database_identifier
from backend.db_evidence import DatabaseEvidence, collect_statement_evidence
from backend.metadata import MetadataEvidence, collect_metadata
from backend.parser import parse_sql
from backend.risk_engine import score_statement

from connector.policy import (
    CompanyTarget,
    CompanyTargetRegistry,
    validate_company_target,
)
from connector.registry_store import (
    RegistryError,
    TargetRegistryStore,
)


CONNECTOR_VERSION = "0.1.0-stage1"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_PAYLOAD_BYTES = 4096
# Request-size contract for remote validation: the SQL limit matches the public API
# (64 KiB UTF-8). The body limit leaves room for JSON escaping of that SQL plus the
# per-request MySQL login; every other connector route keeps MAX_PAYLOAD_BYTES.
MAX_COMPANY_SQL_BYTES = 64 * 1024
MAX_COMPANY_VALIDATE_PAYLOAD_BYTES = 128 * 1024
MAX_COMPANY_PASSWORD_LENGTH = 512
PAIRING_TOKEN_BYTES = 32
SESSION_TOKEN_BYTES = 32
PAIRING_RATE_LIMIT_MAX_FAILURES = 5
PAIRING_RATE_LIMIT_WINDOW_SECONDS = 60.0
# No browser origin is trusted by default. Allowed origins must be configured
# explicitly (see connector/launcher.py and parse_allowed_origins); an empty list
# makes every origin-checked route fail closed.
DEFAULT_ALLOWED_ORIGINS: list[str] = []
ALLOWED_ORIGINS_ENV_VAR = "CONNECTOR_ALLOWED_ORIGINS"

# Session scopes.
# - "operator": full connector capability (target registration/approval/deletion,
#   evidence, discovery). Only issued programmatically; there is no HTTP route that
#   issues an operator pairing token.
# - "browser": issued by the connector launcher for the served validator page. It can
#   only use approved targets; it can never register, approve, or delete targets.
SESSION_SCOPE_OPERATOR = "operator"
SESSION_SCOPE_BROWSER = "browser"
SESSION_SCOPES = frozenset({SESSION_SCOPE_OPERATOR, SESSION_SCOPE_BROWSER})

# Exhaustive allowlist of (method, path pattern) a browser-scoped session may call.
# Anything not listed is denied for browser sessions (fail closed).
_TARGET_ID_SEGMENT = r"[^/]+"
BROWSER_SESSION_ALLOWED_ROUTES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("GET", re.compile(r"^/health$")),
    ("POST", re.compile(r"^/unpair$")),
    ("GET", re.compile(r"^/company/targets$")),
    ("POST", re.compile(r"^/company/targets/" + _TARGET_ID_SEGMENT + r"/databases$")),
    ("POST", re.compile(r"^/company/targets/" + _TARGET_ID_SEGMENT + r"/validate$")),
)


class CompanyTargetOperationError(ValueError):
    """Operator target-registry operation failure with a stable code and HTTP status."""

    def __init__(self, code: str, status_code: int):
        super().__init__(code)
        self.code = code
        self.status_code = status_code

_LOOPBACK_ORIGIN_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

logger = logging.getLogger("connector.server")


def browser_session_route_allowed(method: str, path: str) -> bool:
    method = (method or "").upper()
    return any(
        allowed_method == method and pattern.fullmatch(path or "")
        for allowed_method, pattern in BROWSER_SESSION_ALLOWED_ROUTES
    )


def normalize_allowed_origin(value: str) -> str:
    """Validate one configured browser origin and return its canonical form.

    The canonical form (scheme://host[:port], default port omitted) matches what
    browsers send in the Origin header, which CORSMiddleware compares exactly.
    """
    if not isinstance(value, str):
        raise ValueError("invalid_allowed_origin")
    raw = value.strip()
    if not raw or raw in {"*", "null"} or "*" in raw:
        raise ValueError("invalid_allowed_origin")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_allowed_origin") from exc
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError("invalid_allowed_origin")
    if parsed.username or parsed.password:
        raise ValueError("invalid_allowed_origin")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("invalid_allowed_origin")
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError("invalid_allowed_origin")
    # Plain http is only acceptable for a page served from this machine.
    if scheme == "http" and hostname not in _LOOPBACK_ORIGIN_HOSTS:
        raise ValueError("insecure_allowed_origin")
    host_part = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 80 if scheme == "http" else 443
    if port is None or port == default_port:
        return f"{scheme}://{host_part}"
    return f"{scheme}://{host_part}:{port}"


def parse_allowed_origins(values: list[str] | str | None) -> list[str]:
    """Parse explicit origin configuration (list or comma-separated string).

    Raises ValueError on any invalid entry rather than silently dropping it.
    """
    if values is None:
        return []
    if isinstance(values, str):
        items = [item for item in values.split(",") if item.strip()]
    else:
        items = list(values)
    normalized: list[str] = []
    for item in items:
        origin = normalize_allowed_origin(item)
        if origin not in normalized:
            normalized.append(origin)
    return normalized


class PairingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pairing_token: str = Field(..., min_length=32, max_length=256)


class DatabaseConnectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(...)
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=512)
    database: str | None = Field(default=None, min_length=1, max_length=128)


class CompanyTargetRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=255)
    mode: str = Field(default="COMPANY", min_length=1, max_length=32)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    approval_state: str | None = Field(
        default=None, min_length=1, max_length=32)


class CompanyLoginFields(BaseModel):
    """Per-request MySQL login for a company target.

    The user's own MySQL username/password, supplied in the request body for one
    operation. They are used for that operation's connection only and are never
    stored in the registry, a credential vault, session state, logs, or responses.
    The password is a SecretStr so it cannot leak through model reprs.
    """

    model_config = ConfigDict(extra="forbid")
    username: str = Field(..., min_length=1, max_length=128)
    password: SecretStr

    @field_validator("username")
    @classmethod
    def _username_is_printable(cls, value: str) -> str:
        if not value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError("invalid_username")
        return value

    @field_validator("password")
    @classmethod
    def _password_is_bounded(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not 1 <= len(secret) <= MAX_COMPANY_PASSWORD_LENGTH:
            raise ValueError("invalid_password")
        return value


class CompanyConnectionRequest(CompanyLoginFields):
    target_id: str = Field(..., min_length=1, max_length=128)
    database: str | None = Field(default=None, min_length=1, max_length=128)


class CompanyDiscoveryRequest(CompanyLoginFields):
    pass


class CompanyExplainRequest(CompanyLoginFields):
    operation: str = Field(..., min_length=1, max_length=32)
    database: str = Field(..., min_length=1, max_length=64)
    table: str = Field(..., min_length=1, max_length=64)


class CompanyValidationRequest(CompanyLoginFields):
    sql: str = Field(..., min_length=1, max_length=MAX_COMPANY_SQL_BYTES)
    database: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("sql")
    @classmethod
    def _sql_within_byte_limit(cls, value: str) -> str:
        # Same contract as the public API (backend.main.MAX_SQL_BYTES): 64 KiB UTF-8.
        if len(value.encode("utf-8")) > MAX_COMPANY_SQL_BYTES:
            raise ValueError("sql_too_large")
        return value


D2_EXPLAIN_OPERATIONS = {"EXPLAIN_SELECT", "EXPLAIN_UPDATE", "EXPLAIN_DELETE"}
MAX_EXPLAIN_ROWS = 100
MAX_DISCOVERED_DATABASES = 1000
_MYSQL_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass
class PairingRecord:
    token: str
    expires_at: float
    used: bool = False
    scope: str = SESSION_SCOPE_OPERATOR


@dataclass
class SessionRecord:
    token: str
    expires_at: float
    origin: str
    created_at: float
    scope: str = SESSION_SCOPE_OPERATOR


@dataclass(frozen=True)
class CompanyTargetDestination:
    hostname: str
    validated_ip: str
    address_family: int
    port: int


@dataclass
class ConnectorRuntime:
    bind_host: str = DEFAULT_BIND_HOST
    port: int = DEFAULT_PORT
    max_payload_bytes: int = MAX_PAYLOAD_BYTES
    allows_database: bool = False
    database_capabilities: list[str] = field(default_factory=list)
    allowed_origins: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_ORIGINS)
    )
    session_ttl_seconds: int = 3600
    pairing_ttl_seconds: int = 300
    max_failed_pairing_attempts: int = PAIRING_RATE_LIMIT_MAX_FAILURES
    pairing_fail_window_seconds: float = PAIRING_RATE_LIMIT_WINDOW_SECONDS
    company_tls_ca_path: str | None = None
    pairing_tokens: dict[str, PairingRecord] = field(default_factory=dict)
    sessions: dict[str, SessionRecord] = field(default_factory=dict)
    company_targets: CompanyTargetRegistry = field(
        default_factory=CompanyTargetRegistry)
    # Discovery state keyed by (target_id, session_token): sessions never share or
    # overwrite each other's discovered database lists.
    company_database_scopes: dict[tuple[str, str | None], dict[str, Any]] = field(
        default_factory=dict)
    company_scope_versions: dict[str, int] = field(default_factory=dict)
    # Per-process key for binding discovery state to the MySQL login that produced
    # it. Never persisted; a restart invalidates every discovery result anyway.
    login_binding_key: bytes = field(
        default_factory=lambda: secrets.token_bytes(32), repr=False)
    failed_pairing_attempts: list[float] = field(default_factory=list)
    deleted_target_ids: set[str] = field(default_factory=set)
    company_tls_required: bool = True
    company_tls_client_cert_path: str | None = None
    company_tls_client_key_path: str | None = None
    # None keeps the registry in memory (tests, library use). The launcher always
    # provides a store so approved targets survive restarts.
    registry_store: TargetRegistryStore | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    def _now(self) -> float:
        return time.time()

    def _random_token(self, length: int) -> str:
        return secrets.token_urlsafe(length)

    def issue_pairing_token(
        self,
        expires_in_seconds: int | None = None,
        *,
        scope: str = SESSION_SCOPE_OPERATOR,
    ) -> str:
        # Pairing tokens are only issued in-process (launcher or tests); there is
        # deliberately no HTTP route that issues one.
        if scope not in SESSION_SCOPES:
            raise ValueError("invalid_session_scope")
        ttl = expires_in_seconds if expires_in_seconds is not None else self.pairing_ttl_seconds
        token = self._random_token(PAIRING_TOKEN_BYTES)
        now = self._now()
        with self.lock:
            self.pairing_tokens[token] = PairingRecord(
                token=token,
                expires_at=now + max(ttl, 0),
                used=False,
                scope=scope,
            )
        return token

    def issue_browser_pairing_token(self, expires_in_seconds: int | None = None) -> str:
        """Issue a single-use pairing code for the served validator page."""
        return self.issue_pairing_token(expires_in_seconds, scope=SESSION_SCOPE_BROWSER)

    def session_scope(self, session_token: str | None) -> str | None:
        """Return the scope of a known session, or None if the token is unknown."""
        if not session_token:
            return None
        with self.lock:
            record = self.sessions.get(session_token)
            return record.scope if record is not None else None

    def _consume_pairing_token(self, token: str) -> bool:
        with self.lock:
            record = self.pairing_tokens.get(token)
            if record is None:
                return False
            if record.expires_at <= self._now():
                del self.pairing_tokens[token]
                return False
            if record.used:
                return False
            record.used = True
            return True

    def create_session_for_pairing_token(self, token: str, origin: str = "unknown") -> str | None:
        with self.lock:
            record = self.pairing_tokens.get(token)
            if record is None:
                return None
            if record.expires_at <= self._now():
                del self.pairing_tokens[token]
                return None
            if record.used:
                return None
            record.used = True
            scope = record.scope

        session_token = self._random_token(SESSION_TOKEN_BYTES)
        now = self._now()
        with self.lock:
            self.sessions[session_token] = SessionRecord(
                token=session_token,
                expires_at=now + self.session_ttl_seconds,
                origin=origin,
                created_at=now,
                scope=scope,
            )
        return session_token

    def validate_origin(self, origin: str | None) -> bool:
        if not origin:
            return False
        if origin == "*":
            return False
        raw = origin.strip()
        if not raw:
            return False
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return False
        if parsed.scheme not in {"http", "https"}:
            return False
        hostname = parsed.hostname.lower() if parsed.hostname else None
        if not hostname:
            return False
        port = parsed.port or (80 if parsed.scheme.lower() == "http" else 443)
        normalized = (parsed.scheme.lower(), hostname, port)
        allowed = []
        for candidate in self.allowed_origins:
            if not candidate:
                continue
            try:
                parsed_candidate = urlsplit(candidate.strip())
            except ValueError:
                continue
            candidate_host = parsed_candidate.hostname.lower(
            ) if parsed_candidate.hostname else None
            if not candidate_host or parsed_candidate.scheme.lower() not in {"http", "https"}:
                continue
            candidate_port = parsed_candidate.port or (
                80 if parsed_candidate.scheme.lower() == "http" else 443)
            allowed.append((parsed_candidate.scheme.lower(),
                           candidate_host, candidate_port))
        return normalized in allowed

    def validate_session(self, session_token: str | None, origin: str | None) -> tuple[bool, str | None]:
        if not session_token:
            return False, "invalid_session"
        with self.lock:
            record = self.sessions.get(session_token)
            if record is None:
                return False, "invalid_session"
            if record.expires_at <= self._now():
                del self.sessions[session_token]
                self._drop_session_scopes_locked(session_token)
                return False, "session_expired"
            if origin and record.origin.lower() != origin.lower():
                return False, "invalid_session"
            return True, None

    def _validate_mysql_identifier(self, value: str | None, *, field_name: str) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"invalid_{field_name}")
        cleaned = value.strip()
        if not cleaned or re.fullmatch(r"[A-Za-z0-9_]+", cleaned) is None:
            raise ValueError(f"invalid_{field_name}")
        return cleaned

    def build_bounded_explain_sql(
        self,
        *,
        target_id: str,
        operation: str,
        scope: dict[str, Any],
    ) -> str:
        if operation not in D2_EXPLAIN_OPERATIONS:
            raise ValueError("unsupported_evidence_operation")
        if not isinstance(scope, dict):
            raise ValueError("invalid_explain_statement")

        table = scope.get("table")
        normalized_table = self._validate_mysql_identifier(
            table, field_name="table")
        if normalized_table is None:
            raise ValueError("invalid_explain_statement")

        database = scope.get("database")
        normalized_database = self._validate_mysql_identifier(
            database, field_name="database")

        qualified_table = f"`{normalized_table}`"
        if normalized_database is not None:
            qualified_table = f"`{normalized_database}`.`{normalized_table}`"

        if operation == "EXPLAIN_SELECT":
            statement = f"SELECT * FROM {qualified_table} WHERE 1 = 0"
        elif operation == "EXPLAIN_UPDATE":
            column = "id"
            if isinstance(scope.get("columns"), list) and scope["columns"]:
                first_column = scope["columns"][0]
                normalized_column = self._validate_mysql_identifier(
                    first_column, field_name="column")
                if normalized_column is not None:
                    column = normalized_column
            statement = f"UPDATE {qualified_table} SET `{column}` = `{column}` WHERE 1 = 0"
        elif operation == "EXPLAIN_DELETE":
            statement = f"DELETE FROM {qualified_table} WHERE 1 = 0"
        else:
            raise ValueError("unsupported_evidence_operation")

        return f"EXPLAIN {statement};"

    def execute_bounded_explain_statement(
        self,
        *,
        target_id: str,
        operation: str,
        scope: dict[str, Any],
        sql_digest: str,
        username: str,
        password: str,
        database: str | None = None,
        connect_timeout: int = 3,
    ) -> dict[str, Any]:
        if operation not in D2_EXPLAIN_OPERATIONS:
            raise ValueError("unsupported_evidence_operation")
        if not isinstance(target_id, str) or not target_id.strip():
            raise ValueError("invalid_explain_statement")

        target = self.company_targets.get(target_id)
        if target is None or not self.company_targets.is_approved(target_id):
            raise ValueError("company_target_not_approved")

        try:
            explains_sql = self.build_bounded_explain_sql(
                target_id=target_id,
                operation=operation,
                scope=scope,
            )
        except ValueError:
            raise

        actual_digest = hashlib.sha256(
            explains_sql.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_digest, sql_digest):
            raise ValueError("invalid_evidence_authorization")

        try:
            destination = self.resolve_and_validate_approved_company_target(
                target_id)
            connection = self.connect_company_target_mysql(
                destination,
                username=username,
                password=password,
                database=database or scope.get("database"),
                connect_timeout=connect_timeout,
                tls_hostname=target.tls_hostname or target.host,
                tls_ca_path=self.company_tls_ca_path,
            )
            cursor = None
            try:
                cursor = connection.cursor()
                cursor.execute(explains_sql)
                raw_rows = cursor.fetchmany(MAX_EXPLAIN_ROWS + 1)
            finally:
                if cursor is not None:
                    try:
                        cursor.close()
                    except Exception:
                        pass
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
        except Exception as exc:  # pragma: no cover - defensive guard
            raise ValueError("explain_execution_failed") from exc

        sanitized_rows = []
        for row in raw_rows[:MAX_EXPLAIN_ROWS]:
            if isinstance(row, Mapping):
                row_values = row
            elif isinstance(row, (tuple, list)):
                row_values = dict(zip(
                    ("id", "select_type", "table", "partitions", "type",
                     "possible_keys", "key", "key_len", "ref", "rows",
                     "filtered", "Extra"), row))
            else:
                continue
            sanitized = {
                "id": row_values.get("id"),
                "select_type": row_values.get("select_type"),
                "table": row_values.get("table"),
                "partitions": row_values.get("partitions"),
                "type": row_values.get("type"),
                "possible_keys": row_values.get("possible_keys"),
                "key": row_values.get("key"),
                "key_len": row_values.get("key_len"),
                "ref": row_values.get("ref"),
                "rows": row_values.get("rows"),
                "filtered": row_values.get("filtered"),
                "Extra": row_values.get("Extra"),
            }
            sanitized_rows.append(sanitized)

        return {
            "status": "authorized",
            "target_id": target_id,
            "operation": operation,
            "database": scope.get("database"),
            "rows": sanitized_rows,
            "truncated": len(raw_rows) > MAX_EXPLAIN_ROWS,
            "complete": len(raw_rows) <= MAX_EXPLAIN_ROWS,
            "warnings": [],
            "explain_sql": explains_sql,
        }

    def unpair_session(self, session_token: str | None) -> bool:
        if not session_token:
            return False
        with self.lock:
            if session_token in self.sessions:
                del self.sessions[session_token]
                # Only this session's discovery state goes; other sessions keep theirs.
                # An in-flight discovery for this session cannot commit afterwards
                # because discover_company_databases re-checks the session.
                self._drop_session_scopes_locked(session_token)
                return True
        return False

    def _drop_session_scopes_locked(self, session_token: str) -> None:
        for key in [key for key in self.company_database_scopes if key[1] == session_token]:
            del self.company_database_scopes[key]

    def trim_failed_pairing_attempts_locked(self) -> None:
        now = self._now()
        cutoff = now - self.pairing_fail_window_seconds
        self.failed_pairing_attempts = [
            ts for ts in self.failed_pairing_attempts if ts > cutoff
        ]
        if len(self.failed_pairing_attempts) > self.max_failed_pairing_attempts:
            self.failed_pairing_attempts = self.failed_pairing_attempts[-self.max_failed_pairing_attempts:]

    def prune_failed_pairing_attempts(self) -> list[float]:
        with self.lock:
            self.trim_failed_pairing_attempts_locked()
            return list(self.failed_pairing_attempts)

    def is_pairing_rate_limited(self) -> bool:
        with self.lock:
            self.trim_failed_pairing_attempts_locked()
            return len(self.failed_pairing_attempts) >= self.max_failed_pairing_attempts

    def record_failed_pairing_attempt(self) -> None:
        with self.lock:
            self.trim_failed_pairing_attempts_locked()
            self.failed_pairing_attempts.append(self._now())
            if len(self.failed_pairing_attempts) > self.max_failed_pairing_attempts:
                self.failed_pairing_attempts = self.failed_pairing_attempts[-self.max_failed_pairing_attempts:]

    # ------------------------------------------------------------------
    # Target registry operations (operator only).
    #
    # Every mutation of the registry goes through these methods so that the
    # operator HTTP routes and the launcher's operator console share one
    # implementation. With a registry_store configured, each mutation is
    # persisted atomically before it is considered done; if the write fails the
    # in-memory registry is rolled back and the operation fails.
    # ------------------------------------------------------------------

    def load_registry(self) -> None:
        """Replace the in-memory registry with the persisted one (raises on corruption)."""
        if self.registry_store is None:
            return
        targets, retired = self.registry_store.load()
        with self.lock:
            self.company_targets.targets = targets
            self.deleted_target_ids = set(retired)
            self.company_database_scopes.clear()

    def _registry_state_locked(self) -> tuple[dict[str, CompanyTarget], set[str]]:
        return copy.deepcopy(self.company_targets.targets), set(self.deleted_target_ids)

    def _persist_registry_locked(self, previous: tuple[dict[str, CompanyTarget], set[str]]) -> None:
        if self.registry_store is None:
            return
        try:
            self.registry_store.save(
                self.company_targets.targets, self.deleted_target_ids, now=self._now())
        except RegistryError:
            self.company_targets.targets, self.deleted_target_ids = previous
            self.company_database_scopes.clear()
            logger.warning("target registry write failed; change rolled back")
            raise

    def _invalidate_target_scopes_locked(self, target_id: str) -> None:
        self.company_scope_versions[target_id] = self.company_scope_versions.get(
            target_id, 0) + 1
        for key in [key for key in self.company_database_scopes if key[0] == target_id]:
            del self.company_database_scopes[key]

    def register_company_target(self, target: CompanyTarget) -> CompanyTarget:
        with self.lock:
            if target.target_id in self.deleted_target_ids:
                raise ValueError("deleted_company_target_id_reserved")
            previous = self._registry_state_locked()
            now = self._now()
            target.created_at = now
            target.updated_at = now
            registered = self.company_targets.register(target)
            self._persist_registry_locked(previous)
            return self.company_targets.get(registered.target_id) or registered

    def approve_company_target(self, target_id: str, *, allow_reapproval: bool = False) -> CompanyTarget:
        """Snapshot the host's DNS identity and mark the target approved.

        A target in ``needs_reapproval`` is only re-approved when the operator asks
        for it explicitly (``allow_reapproval``), after re-checking the host.
        """
        with self.lock:
            target = self.company_targets.get(target_id)
            if target is None:
                raise CompanyTargetOperationError("unknown_company_target", 404)
            if target.approval_state == "approved" and target.dns_identity_snapshot is None:
                previous = self._registry_state_locked()
                target.approval_state = "needs_reapproval"
                self._persist_registry_locked(previous)
            if target.approval_state == "needs_reapproval" and not allow_reapproval:
                raise CompanyTargetOperationError("company_target_needs_reapproval", 409)
            host = target.host

        # DNS resolution happens outside the lock so it cannot stall other requests.
        try:
            snapshot = build_company_dns_identity_snapshot(host)
        except ValueError as exc:
            with self.lock:
                current = self.company_targets.get(target_id)
                if current is not None and current.host == host:
                    previous = self._registry_state_locked()
                    current.approval_state = "needs_reapproval"
                    current.updated_at = self._now()
                    self._invalidate_target_scopes_locked(target_id)
                    self._persist_registry_locked(previous)
            raise CompanyTargetOperationError(str(exc), 400) from exc

        with self.lock:
            current = self.company_targets.get(target_id)
            if current is None or current.host != host:
                raise CompanyTargetOperationError("company_target_changed", 409)
            previous = self._registry_state_locked()
            now = self._now()
            current.dns_identity_snapshot = snapshot
            current.dns_snapshot_created_at = now
            current.tls_hostname = current.host
            current.approval_state = "approved"
            if current.approved_at is None:
                current.approved_at = now
            current.updated_at = now
            self._invalidate_target_scopes_locked(target_id)
            self._persist_registry_locked(previous)
            return current

    def revoke_company_target(self, target_id: str) -> CompanyTarget:
        """Deactivate a target: no further use until an operator approves it again."""
        with self.lock:
            target = self.company_targets.get(target_id)
            if target is None:
                raise CompanyTargetOperationError("unknown_company_target", 404)
            previous = self._registry_state_locked()
            target.approval_state = "revoked"
            target.dns_identity_snapshot = None
            target.dns_snapshot_created_at = None
            target.tls_hostname = None
            target.updated_at = self._now()
            self._invalidate_target_scopes_locked(target_id)
            self._persist_registry_locked(previous)
        return target

    def delete_company_target(self, target_id: str) -> None:
        """Remove a target permanently; its identifier can never be reused."""
        with self.lock:
            if target_id not in self.company_targets.targets:
                raise CompanyTargetOperationError("unknown_company_target", 404)
            previous = self._registry_state_locked()
            self.deleted_target_ids.add(target_id)
            del self.company_targets.targets[target_id]
            self._invalidate_target_scopes_locked(target_id)
            self._persist_registry_locked(previous)

    def list_company_targets(self, *, approved_only: bool) -> list[dict[str, Any]]:
        with self.lock:
            targets = list(self.company_targets.targets.values())
            return [
                {
                    "target_id": target.target_id,
                    "display_name": target.display_name,
                    "mode": target.mode,
                    "host": target.host,
                    "port": target.port,
                    "approval_state": target.approval_state,
                }
                for target in targets
                if not approved_only or self.company_targets.is_approved(target.target_id)
            ]

    def is_company_target_approved(self, target_id: str) -> bool:
        return self.company_targets.is_approved(target_id)

    def get_company_target(self, target_id: str) -> CompanyTarget | None:
        return self.company_targets.get(target_id)

    # ------------------------------------------------------------------
    # Company MySQL logins are per request. The connector is not a credential
    # vault: there is no method that stores, reads back, or deletes a user's
    # MySQL credentials. Callers pass the request's username/password straight to
    # connect_company_target_mysql for one operation.
    # ------------------------------------------------------------------

    def company_login_binding(self, username: str) -> str:
        """Keyed fingerprint of a MySQL username, used only to partition discovery
        state between logins. It is not an authorization check: every operation
        still needs a valid session, an approved target, destination validation,
        and MySQL accepting the supplied credentials."""
        return hmac.new(self.login_binding_key, username.encode("utf-8"), hashlib.sha256).hexdigest()

    def validate_company_database_scope(
        self,
        target_id: str,
        database: str,
        *,
        session_token: str | None = None,
        login_binding: str | None = None,
    ) -> None:
        if not self.company_targets.is_approved(target_id):
            raise ValueError("company_target_not_approved")
        if not isinstance(database, str) or not database:
            raise ValueError("invalid_database")
        with self.lock:
            scope = self.company_database_scopes.get((target_id, session_token))
            scope = dict(scope) if scope is not None else None
        if scope is None or scope.get("session_token") != session_token:
            raise ValueError("company_database_scope_unavailable")
        bound_login = scope.get("login_binding")
        if bound_login is not None and not hmac.compare_digest(bound_login, login_binding or ""):
            # Discovery state from one MySQL login is never reusable by another.
            raise ValueError("company_database_scope_unavailable")
        if database not in scope.get("databases", []):
            raise ValueError("database_not_authorized")

    def discover_company_databases(
        self,
        target_id: str,
        *,
        username: str,
        password: str,
        session_token: str | None = None,
    ) -> dict[str, Any]:
        target = self.company_targets.get(target_id)
        if target is None:
            raise CompanyTargetOperationError("unknown_company_target", 404)
        if not self.company_targets.is_approved(target_id):
            raise CompanyTargetOperationError("company_target_not_approved", 409)

        login_binding = self.company_login_binding(username)
        scope_key = (target_id, session_token)
        with self.lock:
            scope_version = self.company_scope_versions.get(target_id, 0)
            self.company_database_scopes.pop(scope_key, None)
        connection = None
        cursor = None
        try:
            destination = self.resolve_and_validate_approved_company_target(
                target_id)
            connection = self.connect_company_target_mysql(
                destination,
                username=username,
                password=password,
                connect_timeout=3,
                tls_hostname=target.tls_hostname or target.host,
                tls_ca_path=self.company_tls_ca_path,
            )
            cursor = connection.cursor()
            cursor.execute("SHOW DATABASES")
            raw_rows = cursor.fetchmany(MAX_DISCOVERED_DATABASES + 1)
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError("database_discovery_failed") from exc
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    pass
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

        if len(raw_rows) > MAX_DISCOVERED_DATABASES:
            rows = raw_rows[:MAX_DISCOVERED_DATABASES]
            complete = False
        else:
            rows = raw_rows
            complete = True

        databases = []
        seen_databases: set[str] = set()
        for row in rows:
            if isinstance(row, Mapping):
                value = next(iter(row.values()), None)
            elif isinstance(row, (tuple, list)) and row:
                value = row[0]
            else:
                raise ValueError("database_discovery_failed")
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 64
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
            ):
                raise ValueError("database_discovery_failed")
            if value not in seen_databases:
                databases.append(value)
                seen_databases.add(value)

        with self.lock:
            if (
                self.company_scope_versions.get(target_id, 0) != scope_version
                or not self.company_targets.is_approved(target_id)
                or (session_token is not None and session_token not in self.sessions)
            ):
                raise ValueError("company_database_scope_stale")
            self.company_database_scopes[scope_key] = {
                "databases": databases,
                "complete": complete,
                "session_token": session_token,
                "login_binding": login_binding,
            }

        return {
            "status": "limited" if not complete else "success",
            "target_id": target_id,
            "databases": databases,
            "complete": complete,
        }

    def execute_company_explain(
        self,
        target_id: str,
        operation: str,
        database: str,
        table: str,
        *,
        username: str,
        password: str,
        session_token: str | None = None,
    ) -> dict[str, Any]:
        if not self.company_targets.is_approved(target_id):
            raise ValueError("company_target_not_approved")
        self._validate_mysql_identifier(database, field_name="database")
        self._validate_mysql_identifier(table, field_name="table")
        self.validate_company_database_scope(
            target_id, database, session_token=session_token,
            login_binding=self.company_login_binding(username))
        scope = {"database": database, "table": table}
        explain_sql = self.build_bounded_explain_sql(
            target_id=target_id, operation=operation, scope=scope)
        sql_digest = hashlib.sha256(explain_sql.encode("utf-8")).hexdigest()
        return self.execute_bounded_explain_statement(
            target_id=target_id,
            operation=operation,
            scope=scope,
            sql_digest=sql_digest,
            username=username,
            password=password,
            database=database,
        )

    def find_approved_company_target_by_host_port(self, host: str, port: int) -> CompanyTarget | None:
        if not isinstance(host, str) or not host.strip():
            return None
        if not isinstance(port, int) or isinstance(port, bool) or not (1 <= port <= 65535):
            return None

        normalized_host = host.strip().lower()
        for target in self.company_targets.targets.values():
            if target.port != port:
                continue
            if target.host.strip().lower() != normalized_host:
                continue
            if self.company_targets.is_approved(target.target_id):
                return target
        return None

    def resolve_approved_company_target_for_hostname_and_port(self, host: str, port: int) -> CompanyTargetDestination:
        target = self.find_approved_company_target_by_host_port(host, port)
        if target is None:
            raise ValueError("company_target_not_approved")
        return self.resolve_and_validate_approved_company_target(target.target_id)

    def resolve_and_validate_approved_company_target(self, target_id: str) -> CompanyTargetDestination:
        target = self.company_targets.get(target_id)
        state_before = target.approval_state if target is not None else None
        try:
            return self._resolve_and_validate_approved_company_target(target_id)
        finally:
            # Destination validation may downgrade a target to needs_reapproval.
            # Persist that fail-closed state so a restart cannot silently undo it.
            if (
                target is not None
                and self.registry_store is not None
                and self.company_targets.get(target_id) is target
                and target.approval_state != state_before
            ):
                with self.lock:
                    try:
                        self.registry_store.save(
                            self.company_targets.targets, self.deleted_target_ids, now=self._now())
                    except RegistryError:
                        logger.warning("target registry write failed after approval downgrade")

    def _resolve_and_validate_approved_company_target(self, target_id: str) -> CompanyTargetDestination:
        target = self.company_targets.get(target_id)
        if target is None:
            raise ValueError("target_not_found")
        if target.approval_state == "approved" and target.dns_identity_snapshot is None:
            target.approval_state = "needs_reapproval"
            with self.lock:
                self._invalidate_target_scopes_locked(target_id)
        if target.approval_state != "approved":
            if target.approval_state == "needs_reapproval":
                raise ValueError("target_needs_reapproval")
            raise ValueError("target_not_approved")
        if not isinstance(target.dns_identity_snapshot, dict):
            target.approval_state = "needs_reapproval"
            raise ValueError("target_needs_reapproval")

        approved_addresses = target.dns_identity_snapshot.get("addresses")
        if not isinstance(approved_addresses, list) or not approved_addresses:
            target.approval_state = "needs_reapproval"
            raise ValueError("target_needs_reapproval")

        normalized_approved: list[str] = []
        for raw_ip in approved_addresses:
            if not isinstance(raw_ip, str):
                target.approval_state = "needs_reapproval"
                raise ValueError("dns_result_invalid")
            try:
                ip_obj = ipaddress.ip_address(raw_ip)
            except ValueError as exc:
                target.approval_state = "needs_reapproval"
                raise ValueError("dns_result_invalid") from exc
            if _is_unsafe_company_destination_ip(ip_obj, host=target.host):
                target.approval_state = "needs_reapproval"
                raise ValueError("unsafe_destination")
            normalized_approved.append(str(ip_obj))

        if not normalized_approved:
            target.approval_state = "needs_reapproval"
            raise ValueError("dns_result_invalid")

        try:
            infos = socket.getaddrinfo(
                target.host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ValueError("dns_resolution_failed") from exc

        current_addresses: list[str] = []
        for _, _, _, _, sockaddr in infos:
            if not sockaddr:
                continue
            ip_text = sockaddr[0]
            try:
                ip_obj = ipaddress.ip_address(ip_text)
            except ValueError as exc:
                raise ValueError("dns_result_invalid") from exc
            if _is_unsafe_company_destination_ip(ip_obj, host=target.host):
                raise ValueError("unsafe_destination")
            current_addresses.append(str(ip_obj))

        if not current_addresses:
            raise ValueError("dns_result_invalid")

        normalized_current = sorted(set(current_addresses))
        normalized_approved_sorted = sorted(set(normalized_approved))
        if normalized_current != normalized_approved_sorted:
            raise ValueError("dns_identity_mismatch")

        selected_ip = current_addresses[0]
        ip_obj = ipaddress.ip_address(selected_ip)
        return CompanyTargetDestination(
            hostname=target.host,
            validated_ip=selected_ip,
            address_family=socket.AF_INET if ip_obj.version == 4 else socket.AF_INET6,
            port=target.port,
        )

    def _build_company_mysql_ssl_config(self, *, hostname: str, ca_path: str | None = None) -> dict[str, Any]:
        resolved_ca = ca_path if ca_path is not None else self.company_tls_ca_path
        if not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("database_connection_failed")
        if resolved_ca is None or not str(resolved_ca).strip():
            raise ValueError("database_connection_failed")
        config: dict[str, Any] = {
            "ca": str(resolved_ca),
            "check_hostname": True,
            "verify_mode": ssl.CERT_REQUIRED,
        }
        if self.company_tls_client_cert_path:
            config["cert"] = self.company_tls_client_cert_path
        if self.company_tls_client_key_path:
            config["key"] = self.company_tls_client_key_path
        return config

    def connect_company_target_mysql(
        self,
        destination: CompanyTargetDestination,
        *,
        username: str,
        password: str,
        database: str | None = None,
        connect_timeout: int = 3,
        tls_hostname: str | None = None,
        tls_ca_path: str | None = None,
    ):
        if not isinstance(destination, CompanyTargetDestination):
            raise ValueError("database_connection_failed")
        if not isinstance(connect_timeout, int) or connect_timeout <= 0:
            raise ValueError("database_connection_failed")

        sock = None
        connection = None
        try:
            tls_host = (tls_hostname or destination.hostname).strip()
            if tls_host != destination.hostname:
                raise ValueError("database_connection_failed")
            ssl_config = self._build_company_mysql_ssl_config(
                hostname=tls_host, ca_path=tls_ca_path)
            sock = socket.socket(
                destination.address_family, socket.SOCK_STREAM)
            sock.settimeout(connect_timeout)
            sock.connect((destination.validated_ip, destination.port))

            connection = pymysql.connect(
                host=tls_host,
                port=destination.port,
                user=username,
                password=password,
                database=database,
                connect_timeout=connect_timeout,
                read_timeout=connect_timeout,
                write_timeout=connect_timeout,
                autocommit=True,
                charset="utf8mb4",
                defer_connect=True,
                # Unbuffered (bounded fetchmany stays streaming) and dict rows: the
                # shared backend evidence/metadata collectors expect the same
                # dictionary-row contract as local validation's DictCursor.
                cursorclass=pymysql.cursors.SSDictCursor,
                ssl=ssl_config,
            )
            connection.connect(sock=sock)
            return connection
        except (pymysql.MySQLError, TimeoutError, OSError, ValueError) as exc:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            raise ValueError("database_connection_failed") from exc
        except Exception as exc:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
            raise ValueError("database_connection_failed") from exc

    def connect_company_target(self, destination: CompanyTargetDestination, *args, **kwargs):
        return self.connect_company_target_mysql(destination, *args, **kwargs)

    def shutdown(self) -> None:
        with self.lock:
            self.pairing_tokens.clear()
            self.sessions.clear()
            self.company_database_scopes.clear()
            self.failed_pairing_attempts.clear()
            self.deleted_target_ids.clear()
            self.company_targets.targets.clear()


def _safe_json_error(message: str, *, status_code: int, error_code: str | None = None) -> dict[str, Any]:
    payload = {"status": "error", "message": message}
    if error_code:
        payload["error_code"] = error_code
    return payload


_COMPANY_VALIDATE_PATH_RE = re.compile(r"^/company/targets/[^/]+/validate$")


def _payload_limit_for(runtime: "ConnectorRuntime", method: str, path: str) -> int:
    """Body-size limit per route: remote validation carries SQL, everything else is small."""
    if method.upper() == "POST" and _COMPANY_VALIDATE_PATH_RE.fullmatch(path or ""):
        return max(runtime.max_payload_bytes, MAX_COMPANY_VALIDATE_PAYLOAD_BYTES)
    return runtime.max_payload_bytes


async def _read_bounded_json(request: Request, limit: int, error_code: str) -> Any:
    """Read the body enforcing the limit even without a Content-Length header."""
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        raise HTTPException(status_code=415, detail=_safe_json_error(
            "unsupported_media_type", status_code=415, error_code="unsupported_media_type"))
    received = bytearray()
    try:
        async for chunk in request.stream():
            received.extend(chunk)
            if len(received) > limit:
                raise HTTPException(status_code=413, detail=_safe_json_error(
                    "request_too_large", status_code=413, error_code="request_too_large"))
        return json.loads(bytes(received))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail=_safe_json_error(
            error_code, status_code=400, error_code=error_code))


def _registry_unavailable() -> HTTPException:
    return HTTPException(status_code=500, detail=_safe_json_error(
        "target_registry_unavailable", status_code=500, error_code="target_registry_unavailable"))


def _target_summary(target: CompanyTarget) -> dict[str, Any]:
    return {
        "target_id": target.target_id,
        "display_name": target.display_name,
        "mode": target.mode,
        "host": target.host,
        "port": target.port,
        "approval_state": target.approval_state,
    }


def validate_database_target(host: str, port: int) -> tuple[str, int]:
    if not isinstance(host, str):
        raise ValueError("database_target_not_allowed")

    normalized = host.strip().lower()
    if normalized in {"localhost", "127.0.0.1"}:
        if port != 3306:
            raise ValueError("database_target_not_allowed")
        return "127.0.0.1", 3306

    raise ValueError("database_target_not_allowed")


def _is_unsafe_company_destination_ip(ip_obj: ipaddress._BaseAddress, *, host: str | None = None) -> bool:
    return (
        ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
        or ip_obj.is_reserved
    )


def validate_company_dns_identity(host: str) -> str:
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("company_dns_validation_failed") from exc
    if not infos:
        raise ValueError("company_dns_validation_failed")

    resolved_ips: list[str] = []
    for _, _, _, _, sockaddr in infos:
        if not sockaddr:
            continue
        ip_text = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError("company_dns_validation_failed") from exc

        if _is_unsafe_company_destination_ip(ip, host=host):
            raise ValueError("company_target_address_not_allowed")

        resolved_ips.append(str(ip))

    if not resolved_ips:
        raise ValueError("company_dns_validation_failed")

    return sorted(set(resolved_ips))[0]


def build_company_dns_identity_snapshot(host: str) -> dict[str, Any]:
    if not isinstance(host, str):
        raise ValueError("invalid_company_target_host")

    normalized_host = host.strip().lower()
    if not normalized_host:
        raise ValueError("invalid_company_target_host")

    try:
        infos = socket.getaddrinfo(
            normalized_host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("company_dns_validation_failed") from exc

    if not infos:
        raise ValueError("company_dns_validation_failed")

    normalized_ips: list[str] = []
    for _, _, _, _, sockaddr in infos:
        if not sockaddr:
            continue
        ip_text = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError("company_dns_validation_failed") from exc

        if _is_unsafe_company_destination_ip(ip, host=normalized_host):
            raise ValueError("company_target_address_not_allowed")

        normalized_ips.append(str(ip))

    if not normalized_ips:
        raise ValueError("company_dns_validation_failed")

    deduped = sorted(set(normalized_ips))
    return {
        "host": normalized_host,
        "port": 3306,
        "addresses": deduped,
        "address_family": "ipv4" if all(ipaddress.ip_address(addr).version == 4 for addr in deduped) else "ipv6",
        "resolved_at": time.time(),
        "tls_hostname": normalized_host,
    }


def create_app(*, runtime: ConnectorRuntime | None = None) -> FastAPI:
    runtime = runtime or ConnectorRuntime()
    app = FastAPI(title="MySQL DBA Validator Local Connector",
                  version=CONNECTOR_VERSION)
    app.state.connector = runtime

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            payload = dict(detail)
        else:
            payload = {"status": "error", "message": str(detail)}
        if "error_code" not in payload:
            payload["error_code"] = "request_failed"
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.middleware("http")
    async def enforce_origin_and_limits(request: Request, call_next):
        if request.method not in {"GET", "POST", "OPTIONS", "DELETE"}:
            return JSONResponse(
                status_code=405,
                content=_safe_json_error(
                    "method_not_allowed", status_code=405, error_code="method_not_allowed"),
            )

        allowed_paths = {"/health", "/pair",
                         "/unpair", "/db/test", "/company/targets"}
        path = request.url.path
        if path not in allowed_paths and not path.startswith("/company/targets/"):
            return JSONResponse(
                status_code=404,
                content=_safe_json_error(
                    "not_found", status_code=404, error_code="not_found"),
            )

        if request.method == "OPTIONS":
            return await call_next(request)

        if path in {"/health", "/pair", "/unpair", "/db/test", "/company/targets"} or path.startswith("/company/targets/"):
            if request.method == "GET" and path == "/health":
                origin = request.headers.get("Origin")
                if origin and not runtime.validate_origin(origin):
                    return JSONResponse(
                        status_code=401,
                        content=_safe_json_error(
                            "invalid_origin", status_code=401, error_code="invalid_origin"),
                    )
                if not origin:
                    return await call_next(request)
            else:
                origin = request.headers.get("Origin")
                if not runtime.validate_origin(origin):
                    return JSONResponse(
                        status_code=401,
                        content=_safe_json_error(
                            "invalid_origin", status_code=401, error_code="invalid_origin"),
                    )

        # Central least-privilege gate: a browser-scoped session may only reach the
        # explicitly allowlisted routes. Unknown/expired tokens fall through so the
        # route's own session validation returns its normal 401.
        bearer = request.headers.get("Authorization", "")
        presented_session = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        if runtime.session_scope(presented_session) == SESSION_SCOPE_BROWSER and not browser_session_route_allowed(request.method, path):
            logger.info("session rejected: insufficient scope")
            return JSONResponse(
                status_code=403,
                content=_safe_json_error(
                    "insufficient_session_scope", status_code=403, error_code="insufficient_session_scope"),
            )

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _payload_limit_for(runtime, request.method, path):
                    return JSONResponse(
                        status_code=413,
                        content=_safe_json_error(
                            "request_too_large", status_code=413, error_code="request_too_large"),
                    )
            except ValueError:
                pass

        return await call_next(request)

    # Added after the enforcement middleware so CORS is the outer layer: the
    # enforcement layer's own 401/403/404/413 responses then carry CORS headers
    # (for allowed origins only), so a real browser can read the error code instead
    # of seeing an opaque network failure. Enforcement itself is unchanged.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime.allowed_origins),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization", "Origin"],
        allow_credentials=False,
    )

    @app.get("/health")
    async def health(request: Request):
        origin = request.headers.get("Origin")
        if origin and not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))
        bearer = request.headers.get("Authorization", "")
        if bearer:
            session_token = bearer.replace("Bearer ", "", 1).strip()
            valid, reason = runtime.validate_session(session_token, origin)
            if not valid:
                raise HTTPException(status_code=401, detail=_safe_json_error(
                    reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))
        return {
            "status": "ok",
            "connector": "mysql-dba-validator-local-connector",
            "version": CONNECTOR_VERSION,
            "protocol": "connector-v1",
            "loopback_only": True,
            "database_capabilities": runtime.database_capabilities,
        }

    @app.post("/pair")
    async def pair(request: Request):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        if runtime.is_pairing_rate_limited():
            logger.info("pairing rate limited")
            raise HTTPException(status_code=429, detail=_safe_json_error(
                "pairing_rate_limited", status_code=429, error_code="pairing_rate_limited"))

        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            raise HTTPException(status_code=415, detail=_safe_json_error(
                "unsupported_media_type", status_code=415, error_code="unsupported_media_type"))

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_pairing_request", status_code=400, error_code="invalid_pairing_request"))

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_pairing_request", status_code=400, error_code="invalid_pairing_request"))

        if len(str(body)) > runtime.max_payload_bytes:
            raise HTTPException(status_code=413, detail=_safe_json_error(
                "request_too_large", status_code=413, error_code="request_too_large"))

        try:
            payload = PairingRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_pairing_request", status_code=400, error_code="invalid_pairing_request"))

        token = payload.pairing_token
        if token not in runtime.pairing_tokens:
            runtime.record_failed_pairing_attempt()
            logger.info("pairing rejected: invalid pairing token")
            raise HTTPException(status_code=403, detail=_safe_json_error(
                "invalid_pairing_token", status_code=403, error_code="invalid_pairing_token"))

        record = runtime.pairing_tokens[token]
        if record.expires_at <= runtime._now():
            del runtime.pairing_tokens[token]
            runtime.record_failed_pairing_attempt()
            logger.info("pairing rejected: expired pairing token")
            raise HTTPException(status_code=403, detail=_safe_json_error(
                "pairing_expired", status_code=403, error_code="pairing_expired"))
        if record.used:
            runtime.record_failed_pairing_attempt()
            logger.info("pairing rejected: already used pairing token")
            raise HTTPException(status_code=403, detail=_safe_json_error(
                "pairing_already_used", status_code=403, error_code="pairing_already_used"))

        session_token = runtime.create_session_for_pairing_token(token, origin)
        if session_token is None:
            runtime.record_failed_pairing_attempt()
            logger.info("pairing rejected: invalid pairing token")
            raise HTTPException(status_code=403, detail=_safe_json_error(
                "invalid_pairing_token", status_code=403, error_code="invalid_pairing_token"))

        logger.info("pairing succeeded")
        return {
            "status": "paired",
            "session_token": session_token,
            "expires_in_seconds": runtime.session_ttl_seconds,
            "scope": runtime.session_scope(session_token),
        }

    @app.post("/unpair")
    async def unpair(request: Request):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        if not runtime.unpair_session(session_token):
            logger.info("session rejected")
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_session", status_code=401, error_code="invalid_session"))

        logger.info("session removed")
        return {"status": "unpaired"}

    @app.post("/company/targets")
    async def register_company_target(request: Request):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))

        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            raise HTTPException(status_code=415, detail=_safe_json_error(
                "unsupported_media_type", status_code=415, error_code="unsupported_media_type"))

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_target_request", status_code=400, error_code="invalid_company_target_request"))

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_target_request", status_code=400, error_code="invalid_company_target_request"))

        if len(str(body)) > runtime.max_payload_bytes:
            raise HTTPException(status_code=413, detail=_safe_json_error(
                "request_too_large", status_code=413, error_code="request_too_large"))

        try:
            payload = CompanyTargetRegistrationRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_target_request", status_code=400, error_code="invalid_company_target_request"))

        target = CompanyTarget(
            target_id=payload.target_id,
            display_name=payload.display_name,
            mode=payload.mode,
            host=payload.host,
            port=payload.port,
            tls_required=True,
            approval_state=payload.approval_state or "pending",
        )

        try:
            validated = runtime.register_company_target(target)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                str(exc), status_code=400, error_code=str(exc))) from exc
        except RegistryError as exc:
            raise _registry_unavailable() from exc

        return JSONResponse(
            status_code=201,
            content={
                "status": "registered",
                "target": {
                    "target_id": validated.target_id,
                    "display_name": validated.display_name,
                    "mode": validated.mode,
                    "host": validated.host,
                    "port": validated.port,
                    "approval_state": validated.approval_state,
                },
            },
        )

    @app.get("/company/targets")
    async def list_company_targets(request: Request):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))

        # Browser sessions only ever see targets they are allowed to use.
        approved_only = runtime.session_scope(session_token) == SESSION_SCOPE_BROWSER
        return {"status": "ok", "targets": runtime.list_company_targets(approved_only=approved_only)}

    @app.post("/company/targets/{target_id}/approve")
    async def approve_company_target(request: Request, target_id: str):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))

        try:
            target = runtime.approve_company_target(target_id)
        except CompanyTargetOperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=_safe_json_error(
                exc.code, status_code=exc.status_code, error_code=exc.code)) from exc
        except RegistryError as exc:
            raise _registry_unavailable() from exc

        return {"status": "approved", "target": _target_summary(target)}

    @app.post("/company/targets/{target_id}/revoke")
    async def revoke_company_target_endpoint(request: Request, target_id: str):
        # Operator scope only: the central middleware denies browser sessions.
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))
        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))
        try:
            target = runtime.revoke_company_target(target_id)
        except CompanyTargetOperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=_safe_json_error(
                exc.code, status_code=exc.status_code, error_code=exc.code)) from exc
        except RegistryError as exc:
            raise _registry_unavailable() from exc
        return {"status": "revoked", "target": _target_summary(target)}

    @app.post("/company/targets/{target_id}/connect")
    async def connect_company_target_endpoint(request: Request, target_id: str):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))

        body = await _read_bounded_json(
            request, runtime.max_payload_bytes, "invalid_company_connection_request")

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_connection_request", status_code=400, error_code="invalid_company_connection_request"))

        try:
            payload = CompanyConnectionRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_connection_request", status_code=400, error_code="invalid_company_connection_request"))

        if payload.target_id and payload.target_id != target_id:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_connection_request", status_code=400, error_code="invalid_company_connection_request"))

        target = runtime.company_targets.get(target_id)
        if target is None:
            raise HTTPException(status_code=404, detail=_safe_json_error(
                "unknown_company_target", status_code=404, error_code="unknown_company_target"))
        if not runtime.is_company_target_approved(target_id):
            raise HTTPException(status_code=409, detail=_safe_json_error(
                "company_target_not_approved", status_code=409, error_code="company_target_not_approved"))

        if payload.database is not None:
            try:
                runtime.validate_company_database_scope(
                    target_id, payload.database, session_token=session_token,
                    login_binding=runtime.company_login_binding(payload.username))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=_safe_json_error(
                    str(exc), status_code=409, error_code=str(exc))) from exc

        connection = None
        try:
            destination = runtime.resolve_and_validate_approved_company_target(
                target_id)
            # Per-request login: used for this connection only, never stored.
            connection = runtime.connect_company_target_mysql(
                destination,
                username=payload.username,
                password=payload.password.get_secret_value(),
                database=payload.database,
                connect_timeout=3,
                tls_hostname=target.tls_hostname or target.host,
                tls_ca_path=runtime.company_tls_ca_path,
            )
            return {"status": "connected"}
        except ValueError as exc:
            raise HTTPException(status_code=500, detail=_safe_json_error(
                "database_connection_failed", status_code=500, error_code="database_connection_failed")) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_safe_json_error(
                "database_connection_failed", status_code=500, error_code="database_connection_failed")) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @app.post("/company/targets/{target_id}/validate")
    async def validate_company_target_sql_endpoint(request: Request, target_id: str):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))

        body = await _read_bounded_json(
            request, _payload_limit_for(runtime, "POST", request.url.path),
            "invalid_company_validation_request")

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_validation_request", status_code=400, error_code="invalid_company_validation_request"))

        try:
            payload = CompanyValidationRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_validation_request", status_code=400, error_code="invalid_company_validation_request"))

        target = runtime.company_targets.get(target_id)
        if target is None:
            raise HTTPException(status_code=404, detail=_safe_json_error(
                "unknown_company_target", status_code=404, error_code="unknown_company_target"))
        if not runtime.is_company_target_approved(target_id):
            raise HTTPException(status_code=409, detail=_safe_json_error(
                "company_target_not_approved", status_code=409, error_code="company_target_not_approved"))

        if payload.database is not None:
            if not validate_database_identifier(payload.database):
                raise HTTPException(status_code=400, detail=_safe_json_error(
                    "invalid_database", status_code=400, error_code="invalid_database"))
            try:
                runtime.validate_company_database_scope(
                    target_id, payload.database, session_token=session_token,
                    login_binding=runtime.company_login_binding(payload.username))
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=_safe_json_error(
                    str(exc), status_code=409, error_code=str(exc))) from exc

        connection = None
        try:
            destination = runtime.resolve_and_validate_approved_company_target(
                target_id)
            # Per-request login: used for this connection only, never stored.
            connection = runtime.connect_company_target_mysql(
                destination,
                username=payload.username,
                password=payload.password.get_secret_value(),
                database=payload.database,
                connect_timeout=3,
                tls_hostname=target.tls_hostname or target.host,
                tls_ca_path=runtime.company_tls_ca_path,
            )

            sql_text = payload.sql.strip()
            if not sql_text:
                raise ValueError("invalid_sql")

            from backend.main import build_statement_result
            all_facts = parse_sql(sql_text)
            if len(all_facts) > 16:
                raise HTTPException(status_code=400, detail=_safe_json_error(
                    "statement_limit_exceeded", status_code=400, error_code="statement_limit_exceeded"))

            analyses = analyze_batch(all_facts)
            reports = [score_statement(analysis, batch_size=len(
                analyses)) for analysis in analyses]
            statements = []
            evidence_available = False

            for report, analysis in zip(reports, analyses):
                # Evidence collection on the already-validated connection. A collector
                # failure degrades to "evidence unavailable" (same as local); it never
                # turns a usable static result into a 500.
                try:
                    evidence = collect_statement_evidence(
                        report.facts.raw_sql, connection=connection)
                except Exception:
                    evidence = DatabaseEvidence(available=False, error="collection_failed")
                if payload.database is None:
                    # Never let collect_metadata fall back to the local .env database:
                    # remote metadata only ever uses the discovered, bound database.
                    metadata = MetadataEvidence(status="unavailable")
                else:
                    try:
                        metadata = collect_metadata(
                            report.facts.tables, connection=connection, database=payload.database)
                    except Exception:
                        metadata = MetadataEvidence(status="unavailable")

                # Same scoring pipeline and payload as local /api/validate.
                statements.append(build_statement_result(report, analysis, evidence, metadata))
                if evidence.available:
                    evidence_available = True

            # A live connection exists, so "no evidence" is reported the way local
            # validation reports a configured database that yielded none.
            analysis_mode = "DATABASE_EVIDENCE" if evidence_available else "STATIC_DATABASE_UNAVAILABLE"
            aggregate_statement = max(
                statements, key=lambda statement: statement["score"], default=None)
            overall_score = aggregate_statement["score"] if aggregate_statement else 0
            overall_risk_level = aggregate_statement["risk_level"] if aggregate_statement else "LOW"

            return {
                "statement_count": len(statements),
                "overall_score": overall_score,
                "overall_risk_level": overall_risk_level,
                "analysis_mode": analysis_mode,
                "statements": statements,
            }
        except HTTPException:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                str(exc), status_code=400, error_code=str(exc))) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_safe_json_error(
                "database_validation_failed", status_code=500, error_code="database_validation_failed")) from exc
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    @app.post("/company/targets/{target_id}/databases")
    async def discover_company_databases_endpoint(request: Request, target_id: str):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))

        # Fixed-purpose discovery: the body carries only the per-request MySQL login.
        # No query, host, port, or other destination input is accepted.
        body = await _read_bounded_json(
            request, runtime.max_payload_bytes, "invalid_database_discovery_request")
        try:
            payload = CompanyDiscoveryRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_database_discovery_request", status_code=400, error_code="invalid_database_discovery_request"))

        try:
            return runtime.discover_company_databases(
                target_id,
                username=payload.username,
                password=payload.password.get_secret_value(),
                session_token=session_token,
            )
        except CompanyTargetOperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=_safe_json_error(
                exc.code, status_code=exc.status_code, error_code=exc.code)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=_safe_json_error(
                "database_discovery_failed", status_code=500, error_code="database_discovery_failed")) from exc

    @app.post("/company/targets/{target_id}/explain")
    async def execute_company_explain_endpoint(request: Request, target_id: str):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))
        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))
        body = await _read_bounded_json(
            request, runtime.max_payload_bytes, "invalid_company_explain_request")
        try:
            payload = CompanyExplainRequest.model_validate(body)
        except (TypeError, ValueError, ValidationError):
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_company_explain_request", status_code=400, error_code="invalid_company_explain_request"))
        if payload.operation not in D2_EXPLAIN_OPERATIONS:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "unsupported_evidence_operation", status_code=400, error_code="unsupported_evidence_operation"))
        try:
            result = runtime.execute_company_explain(
                target_id, payload.operation, payload.database, payload.table,
                username=payload.username,
                password=payload.password.get_secret_value(),
                session_token=session_token)
        except ValueError as exc:
            code = str(exc)
            if code in {"company_target_not_approved", "invalid_database", "invalid_table", "invalid_explain_statement", "unsupported_evidence_operation"}:
                raise HTTPException(status_code=400, detail=_safe_json_error(
                    code, status_code=400, error_code=code)) from exc
            raise HTTPException(status_code=500, detail=_safe_json_error(
                "explain_execution_failed", status_code=500, error_code="explain_execution_failed")) from exc
        return {"status": "authorized", "target_id": target_id, "operation": payload.operation, "database": payload.database, "table": payload.table, "explain": result}

    @app.delete("/company/targets/{target_id}")
    async def delete_company_target(request: Request, target_id: str):
        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))

        try:
            runtime.delete_company_target(target_id)
        except CompanyTargetOperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=_safe_json_error(
                exc.code, status_code=exc.status_code, error_code=exc.code)) from exc
        except RegistryError as exc:
            raise _registry_unavailable() from exc
        return {"status": "deleted", "target_id": target_id}

    @app.post("/db/test")
    async def db_test(request: Request):
        if not runtime.allows_database:
            raise HTTPException(status_code=404, detail=_safe_json_error(
                "not_found", status_code=404, error_code="not_found"))

        origin = request.headers.get("Origin")
        if not runtime.validate_origin(origin):
            raise HTTPException(status_code=401, detail=_safe_json_error(
                "invalid_origin", status_code=401, error_code="invalid_origin"))

        bearer = request.headers.get("Authorization", "")
        session_token = bearer.replace(
            "Bearer ", "", 1).strip() if bearer else ""
        valid, reason = runtime.validate_session(session_token, origin)
        if not valid:
            raise HTTPException(status_code=401, detail=_safe_json_error(
                reason or "invalid_session", status_code=401, error_code=reason or "invalid_session"))

        content_type = request.headers.get("content-type", "")
        if "application/json" not in content_type.lower():
            raise HTTPException(status_code=415, detail=_safe_json_error(
                "unsupported_media_type", status_code=415, error_code="unsupported_media_type"))

        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_db_request", status_code=400, error_code="invalid_db_request"))

        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_db_request", status_code=400, error_code="invalid_db_request"))

        if len(str(body)) > runtime.max_payload_bytes:
            raise HTTPException(status_code=413, detail=_safe_json_error(
                "request_too_large", status_code=413, error_code="request_too_large"))

        try:
            payload = DatabaseConnectionRequest.model_validate(body)
        except ValidationError:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "invalid_db_request", status_code=400, error_code="invalid_db_request"))

        try:
            target_host, target_port = validate_database_target(
                payload.host, payload.port)
        except ValueError:
            raise HTTPException(status_code=400, detail=_safe_json_error(
                "database_target_not_allowed", status_code=400, error_code="database_target_not_allowed"))

        connection = None
        try:
            connection = pymysql.connect(
                host=target_host,
                port=target_port,
                user=payload.username,
                password=payload.password,
                database=payload.database,
                connect_timeout=3,
                read_timeout=3,
                write_timeout=3,
                autocommit=True,
                charset="utf8mb4",
            )
            return {"status": "connected"}
        except (pymysql.MySQLError, TimeoutError, OSError):
            raise HTTPException(status_code=500, detail=_safe_json_error(
                "database_connection_failed", status_code=500, error_code="database_connection_failed"))
        except Exception:
            raise HTTPException(status_code=500, detail=_safe_json_error(
                "database_connection_failed", status_code=500, error_code="database_connection_failed"))
        finally:
            if connection is not None:
                connection.close()

    return app


def create_stage2_app(*, bind_host: str = DEFAULT_BIND_HOST, port: int = DEFAULT_PORT,
                      allowed_origins: list[str] | None = None) -> FastAPI:
    if bind_host != DEFAULT_BIND_HOST:
        raise ValueError(
            "Stage 2 connector bind host must be exactly 127.0.0.1")
    runtime = ConnectorRuntime(
        bind_host=bind_host,
        port=port,
        allowed_origins=list(allowed_origins or DEFAULT_ALLOWED_ORIGINS),
        pairing_ttl_seconds=300,
        session_ttl_seconds=900,
        allows_database=True,
        database_capabilities=["mysql_connection_test"],
    )
    return create_app(runtime=runtime)
