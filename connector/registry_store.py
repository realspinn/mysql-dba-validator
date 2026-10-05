"""Durable storage for the connector's approved company-target registry.

Design
------
* One JSON document per OS user, versioned (``schema_version``) and tagged with a
  ``kind`` marker so an unrelated file is never mistaken for a registry.
* Writes are atomic: serialise to a temporary file in the same directory, flush and
  fsync, then ``os.replace`` over the old file. A crash leaves either the old or the
  new registry, never a partial one.
* Reads are strict. A missing file means "no targets yet". Anything else that is
  not a well-formed registry (bad JSON, unknown version, unknown keys, invalid
  target, malformed DNS snapshot, oversized file) raises ``RegistryCorruptError``.
  The store never replaces an unreadable file with an empty registry.
* The registry holds destination policy only. Company MySQL credentials are per
  request and never stored anywhere; unknown keys are rejected so secrets cannot
  be smuggled into the file.
* This module validates structure. The runtime remains the authority on whether a
  destination is safe to use and re-checks the DNS snapshot on every connection.
"""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from connector.policy import CompanyTarget, validate_company_target

REGISTRY_KIND = "mysql-dba-validator/connector-target-registry"
REGISTRY_SCHEMA_VERSION = 1
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_REGISTRY_TARGETS = 256
REGISTRY_PATH_ENV_VAR = "CONNECTOR_TARGET_REGISTRY"

_TOP_LEVEL_KEYS = {"kind", "schema_version", "updated_at", "retired_target_ids", "targets"}
_TARGET_KEYS = {
    "target_id", "display_name", "mode", "host", "port", "tls_required", "tls_policy",
    "allowed_database_policy", "approval_state", "approved_at", "dns_identity_snapshot",
    "dns_snapshot_created_at", "tls_hostname", "created_at", "updated_at",
}
_SNAPSHOT_KEYS = {"host", "port", "addresses", "address_family", "resolved_at", "tls_hostname"}


class RegistryError(RuntimeError):
    """Base class; the message is a stable error code, never secret material."""


class RegistryCorruptError(RegistryError):
    pass


class RegistryPersistenceError(RegistryError):
    pass


def default_registry_path(environ: dict[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    configured = env.get(REGISTRY_PATH_ENV_VAR)
    if configured:
        return Path(configured)
    base = env.get("LOCALAPPDATA") or env.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "MySQLDBAValidator" / "connector-targets.json"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_optional_number(record: dict[str, Any], key: str, where: str) -> None:
    value = record.get(key)
    if value is not None and not _is_number(value):
        raise RegistryCorruptError(f"invalid_{key}:{where}")


def _validate_snapshot(snapshot: Any, *, host: str, where: str) -> None:
    if not isinstance(snapshot, dict) or set(snapshot) - _SNAPSHOT_KEYS:
        raise RegistryCorruptError(f"invalid_dns_identity_snapshot:{where}")
    if str(snapshot.get("host", "")).lower() != host.lower():
        raise RegistryCorruptError(f"invalid_dns_identity_snapshot:{where}")
    addresses = snapshot.get("addresses")
    if not isinstance(addresses, list) or not addresses or len(addresses) > 64:
        raise RegistryCorruptError(f"invalid_dns_identity_snapshot:{where}")
    for address in addresses:
        try:
            ipaddress.ip_address(address)
        except (ValueError, TypeError) as exc:
            raise RegistryCorruptError(f"invalid_dns_identity_snapshot:{where}") from exc


def _target_from_record(record: Any, index: int) -> CompanyTarget:
    where = f"targets[{index}]"
    if not isinstance(record, dict):
        raise RegistryCorruptError(f"invalid_target:{where}")
    unknown = set(record) - _TARGET_KEYS
    if unknown:
        raise RegistryCorruptError(f"unknown_target_field:{where}")
    for key in ("target_id", "display_name", "mode", "host", "approval_state"):
        if not isinstance(record.get(key), str):
            raise RegistryCorruptError(f"invalid_{key}:{where}")
    if not isinstance(record.get("port"), int) or isinstance(record.get("port"), bool):
        raise RegistryCorruptError(f"invalid_port:{where}")
    if not isinstance(record.get("tls_required", False), bool):
        raise RegistryCorruptError(f"invalid_tls_required:{where}")
    for key in ("approved_at", "dns_snapshot_created_at", "created_at", "updated_at"):
        _check_optional_number(record, key, where)
    for key in ("tls_policy", "allowed_database_policy", "tls_hostname"):
        if record.get(key) is not None and not isinstance(record.get(key), str):
            raise RegistryCorruptError(f"invalid_{key}:{where}")

    try:
        target = validate_company_target(CompanyTarget(**record))
    except (TypeError, ValueError) as exc:
        raise RegistryCorruptError(f"invalid_target:{where}") from exc

    if target.dns_identity_snapshot is not None:
        _validate_snapshot(target.dns_identity_snapshot, host=target.host, where=where)
    if target.approval_state == "approved" and target.dns_identity_snapshot is None:
        raise RegistryCorruptError(f"approved_target_without_dns_snapshot:{where}")
    if target.tls_hostname is not None and target.tls_hostname.lower() != target.host.lower():
        raise RegistryCorruptError(f"invalid_tls_hostname:{where}")
    return target


class TargetRegistryStore:
    """Load/save the registry file. Not thread-safe by itself; the runtime locks."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> tuple[dict[str, CompanyTarget], set[str]]:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return {}, set()
        except OSError as exc:
            raise RegistryCorruptError("registry_unreadable") from exc
        if size > MAX_REGISTRY_BYTES:
            raise RegistryCorruptError("registry_too_large")
        try:
            raw = self.path.read_bytes()
            document = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise RegistryCorruptError("registry_not_valid_json") from exc

        if not isinstance(document, dict) or set(document) - _TOP_LEVEL_KEYS:
            raise RegistryCorruptError("registry_invalid_structure")
        if document.get("kind") != REGISTRY_KIND:
            raise RegistryCorruptError("registry_wrong_kind")
        if document.get("schema_version") != REGISTRY_SCHEMA_VERSION:
            raise RegistryCorruptError("registry_unsupported_schema_version")

        records = document.get("targets")
        retired = document.get("retired_target_ids", [])
        if not isinstance(records, list) or len(records) > MAX_REGISTRY_TARGETS:
            raise RegistryCorruptError("registry_invalid_targets")
        if not isinstance(retired, list) or not all(isinstance(item, str) for item in retired):
            raise RegistryCorruptError("registry_invalid_retired_target_ids")

        targets: dict[str, CompanyTarget] = {}
        for index, record in enumerate(records):
            target = _target_from_record(record, index)
            if target.target_id in targets:
                raise RegistryCorruptError(f"duplicate_target_id:targets[{index}]")
            if target.target_id in retired:
                raise RegistryCorruptError(f"retired_target_id_in_use:targets[{index}]")
            targets[target.target_id] = target
        return targets, set(retired)

    def save(self, targets: dict[str, CompanyTarget], retired_target_ids: set[str], *, now: float) -> None:
        document = {
            "kind": REGISTRY_KIND,
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "updated_at": now,
            "retired_target_ids": sorted(retired_target_ids),
            "targets": [
                {key: value for key, value in asdict(target).items() if key in _TARGET_KEYS}
                for target in sorted(targets.values(), key=lambda item: item.target_id)
            ],
        }
        payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8")
        if len(payload) > MAX_REGISTRY_BYTES:
            raise RegistryPersistenceError("registry_too_large")

        temp_name = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                prefix=".connector-targets-", suffix=".tmp", dir=str(self.path.parent))
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
            temp_name = None
        except OSError as exc:
            raise RegistryPersistenceError("registry_write_failed") from exc
        finally:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
