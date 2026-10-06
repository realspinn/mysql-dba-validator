from __future__ import annotations
from .m2_scoring import apply_m2_adjustment
from .metadata_scoring import apply_m1_adjustment
from .metadata import MetadataEvidence, collect_metadata
from .evidence_scoring import apply_evidence_adjustments
from .db_evidence import (
    PLAN_NOT_COLLECTED_READ_ONLY,
    DatabaseEvidence,
    collect_statement_evidence,
    evidence_status,
)
from .db import (
    AnalysisSession,
    ConnectionProfile,
    discover_databases,
    get_client,
    open_evidence_connection,
    probe_connection,
    validate_database_identifier,
    validate_readonly_select,
)
from pydantic import BaseModel as PydanticBaseModel
from dataclasses import asdict
from .risk_engine import score_statement
from .analyzer import analyze_batch
from .parser import parse_sql
from .version import APP_VERSION
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI, Request

import ipaddress
import os
import socket
from enum import Enum
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv

# Load .env from project root (if present) so DB config can be provided via .env file
load_dotenv()


app = FastAPI(
    title="MySQL DBA Validator",
    version=APP_VERSION,
)


@app.exception_handler(RequestValidationError)
async def redacted_validation_error(request: Request, exc: RequestValidationError):
    """422 without echoing request values: FastAPI's default includes each error's
    `input` (and `ctx`), which can carry submitted credentials."""
    detail = [
        {"type": error.get("type"), "loc": list(error.get("loc", ())), "msg": error.get("msg")}
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": detail})


def configured_cors_origins() -> list[str]:
    """Return the explicit trusted browser origins for this deployment."""
    configured = os.environ.get("CORS_ALLOW_ORIGINS", "")
    origins = [origin.strip()
               for origin in configured.split(",") if origin.strip()]
    if origins:
        return origins
    return []


app.add_middleware(
    CORSMiddleware,
    allow_origins=configured_cors_origins(),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
    allow_credentials=False,
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self' http://127.0.0.1:8765 http://localhost:8765; "
        "img-src 'self' data:; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


FRONTEND_DIR = (
    Path(__file__).resolve().parent.parent / "frontend"
)

MAX_SQL_BYTES = 64 * 1024
MAX_STATEMENTS = 16


class TargetClass(str, Enum):
    """Classification of a database target based on its host and port."""
    LOOPBACK_LOCAL = "loopback_local"
    REMOTE = "remote"
    INVALID = "invalid"


class ValidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str
    host: str | None = None
    port: int = Field(default=3306, ge=1, le=65535)
    username: str | None = None
    password: SecretStr | None = None
    database: str | None = None
    # DEPRECATED: ignored; target classification is authoritative
    mode: Literal["local", "company"] = "local"

    @model_validator(mode="after")
    def _validate_legacy_fields(self):
        if self.mode == "company" and (self.username is not None or self.password is not None):
            raise ValueError("company mode does not accept user credentials")
        if (self.username is None) != (self.password is None):
            raise ValueError("username and password must be supplied together")
        return self


class ConnectionTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = Field(default=3306, ge=1, le=65535)
    username: str
    password: SecretStr
    database: str | None = None


def _validate_local_only_target(host: str, port: int) -> None:
    if not isinstance(host, str) or not host.strip():
        raise ValueError("local_only_target_required")
    if not isinstance(port, int) or isinstance(port, bool) or port != 3306:
        raise ValueError("local_only_target_required")

    normalized = host.strip().lower()
    try:
        infos = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("local_only_target_required") from exc

    for _, _, _, _, sockaddr in infos:
        if not sockaddr:
            continue
        ip_text = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if ip.is_loopback:
            return

    raise ValueError("local_only_target_required")


def classify_target(host: str | None, port: int) -> TargetClass:
    """
    Classify a database target as LOOPBACK_LOCAL, REMOTE, or INVALID.

    The backend is authoritative. The frontend supplied mode cannot influence
    the target classification.
    """
    if host is None:
        return TargetClass.REMOTE
    if not isinstance(host, str):
        return TargetClass.INVALID
    if not isinstance(port, int) or isinstance(port, bool):
        return TargetClass.INVALID
    if port < 1 or port > 65535:
        return TargetClass.INVALID

    normalized = host.strip().lower()
    if not normalized:
        return TargetClass.INVALID
    if normalized in {"localhost", "127.0.0.1", "::1"}:
        if port != 3306:
            return TargetClass.INVALID
        try:
            _validate_local_only_target(normalized, port)
            return TargetClass.LOOPBACK_LOCAL
        except ValueError:
            return TargetClass.INVALID

    try:
        infos = socket.getaddrinfo(normalized, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return TargetClass.REMOTE

    for _, _, _, _, sockaddr in infos:
        if not sockaddr:
            continue
        ip_text = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError:
            continue
        if ip.is_loopback:
            if port == 3306:
                return TargetClass.LOOPBACK_LOCAL
            return TargetClass.INVALID

    return TargetClass.REMOTE


def resolve_approved_company_target(host: str | None, port: int) -> str | None:
    """Resolve an exact approved connector target for a remote host+port.

    The public backend never becomes a credential proxy: it only checks whether the
    host+port exactly matches an approved company target already registered in the
    local connector. This preserves the existing connector authorization model and
    fails closed when no exact, approved target exists.
    """
    if host is None or not isinstance(host, str):
        return None
    normalized = host.strip().lower()
    if not normalized:
        return None
    try:
        from connector.server import ConnectorRuntime
    except Exception:
        return None
    try:
        runtime = ConnectorRuntime()
        target = runtime.find_approved_company_target_by_host_port(
            normalized, port)
    except Exception:
        return None
    if target is None:
        return None
    return target.target_id


def _invalid_connection_config():
    return {
        "status": "error",
        "connected": False,
        "error_code": "invalid_connection_config",
        "message": "Host, username, and password are required.",
    }


def _session_from_connection_request(
    req: ConnectionTestRequest,
    include_database: bool = True,
) -> AnalysisSession | None:
    if not req.host.strip() or not req.username.strip() or not req.password.get_secret_value():
        return None

    profile = ConnectionProfile(
        host=req.host.strip(),
        port=req.port,
        username=req.username.strip(),
        password=req.password.get_secret_value(),
    )
    database = req.database.strip() if include_database and req.database else None
    return AnalysisSession(profile, database)


@app.post("/api/connections/test")
def connection_test(req: ConnectionTestRequest):
    """Test a local-only MySQL connection for trusted self-hosted deployment."""
    session = _session_from_connection_request(req)
    if session is None:
        return _invalid_connection_config()
    try:
        _validate_local_only_target(req.host, req.port)
    except ValueError:
        return {
            "status": "error",
            "connected": False,
            "error_code": "local_only_target_required",
            "message": "Only loopback MySQL targets are permitted for Local Mode.",
        }
    return probe_connection(session)


@app.post("/api/connections/discover-databases")
def discover_databases_endpoint(req: ConnectionTestRequest):
    """Discover databases for a trusted local-only deployment."""
    session = _session_from_connection_request(req, include_database=False)
    if session is None:
        return _invalid_connection_config()
    try:
        _validate_local_only_target(req.host, req.port)
    except ValueError:
        return {
            "status": "error",
            "connected": False,
            "error_code": "local_only_target_required",
            "message": "Only loopback MySQL targets are permitted for Local Mode.",
        }
    return discover_databases(session)


def risk_level_from_score(score: int) -> str:

    if score >= 80:
        return "CRITICAL"

    if score >= 50:
        return "HIGH"

    if score >= 20:
        return "MEDIUM"

    return "LOW"


@app.post("/api/validate")
def validate(req: ValidateRequest):

    if len(req.sql.encode("utf-8")) > MAX_SQL_BYTES:
        return {"error": "request_too_large"}

    if req.database is not None and not validate_database_identifier(req.database):
        return {
            "error_code": "invalid_database",
            "message": "Invalid database selection.",
        }

    # Legacy/static validation requests intentionally omit host/port to exercise
    # the existing orchestration contract without implying a real remote target.
    # Real host-based requests remain authoritative and must still be classified
    # by host/port, not by any frontend mode field.
    if req.host is None:
        target_class = TargetClass.LOOPBACK_LOCAL
        allow_evidence_collection = True
    else:
        target_class = classify_target(req.host, req.port)
        if target_class == TargetClass.INVALID:
            return {
                "error_code": "invalid_target",
                "message": "Invalid database target. Only loopback (127.0.0.1, ::1, localhost:3306) or authenticated remote targets are permitted.",
            }
        if target_class == TargetClass.REMOTE:
            # Remote company validation is explicitly routed through the local connector.
            # The public API must never act as a credential proxy, so it fails closed
            # on any remote request that tries to use remote credentials here.
            if req.username is not None or req.password is not None:
                return {
                    "status": "error",
                    "error_code": "permission_denied",
                    "message": "Permission denied for this database target.",
                }
            target_id = resolve_approved_company_target(req.host, req.port)
            if target_id is None:
                return {
                    "status": "error",
                    "error_code": "permission_denied",
                    "message": "Permission denied for this database target.",
                }
            return {
                "status": "delegated_to_connector",
                "target_id": target_id,
                "error_code": "connector_required",
                "message": "Approved company target requires connector validation.",
            }
        allow_evidence_collection = (
            target_class == TargetClass.LOOPBACK_LOCAL)

    # Parse SQL into StatementFacts
    all_facts = parse_sql(req.sql)
    if len(all_facts) > MAX_STATEMENTS:
        return {"error": "statement_limit_exceeded"}

    # Analyze statements into the V2 StatementAnalysis (findings, evidence gaps, recommendations, dimensions)
    analyses = analyze_batch(all_facts)

    # Score each analysis with the existing risk engine (preserves deterministic V1 behavior)
    reports = [
        score_statement(analysis, batch_size=len(analyses))
        for analysis in analyses
    ]

    # Local evidence uses only the login supplied with this request, on one fresh
    # connection bound to the selected database. There is no other credential source
    # and no fallback: without a login and database, evidence is "not_configured".
    evidence_connection = None
    connection_status = "not_configured"
    connection_attempted = False
    username = (req.username or "").strip()
    password = req.password.get_secret_value() if req.password is not None else ""
    if (allow_evidence_collection and req.host is not None and username and password
            and req.database is not None):
        connection_attempted = True
        evidence_connection, connection_status = open_evidence_connection(
            ConnectionProfile(host=req.host.strip(), port=req.port,
                              username=username, password=password),
            req.database,
        )

    statements = []
    evidence_available = False
    read_only_plan_skipped_with_metadata = False
    analysis_mode = "STATIC"
    overall_score = 0

    try:
        for report, analysis in zip(reports, analyses):
            static_report = report
            if evidence_connection is None:
                # Remote targets never reach here with a connection (fail-closed), and a
                # local request without a usable connection reports why.
                evidence = DatabaseEvidence(available=False, error=connection_status)
                metadata = MetadataEvidence(status="unavailable")
            else:
                try:
                    evidence = collect_statement_evidence(
                        report.facts.raw_sql, connection=evidence_connection)
                except Exception:
                    evidence = DatabaseEvidence(
                        available=False, error="collection_failed")
                try:
                    metadata = collect_metadata(
                        report.facts.tables,
                        connection=evidence_connection,
                        database=req.database,
                    )
                except Exception:
                    metadata = MetadataEvidence(status="unavailable")

            payload = build_statement_result(static_report, analysis, evidence, metadata)

            if evidence.available:
                evidence_available = True
                analysis_mode = "DATABASE_EVIDENCE"
            if evidence.error == PLAN_NOT_COLLECTED_READ_ONLY and metadata.status == "available":
                read_only_plan_skipped_with_metadata = True

            statements.append(payload)
    finally:
        if evidence_connection is not None:
            try:
                evidence_connection.close()
            except Exception:
                pass

    if not evidence_available and connection_attempted:
        # Local UPDATE/DELETE plans are deliberately not collected on the read-only
        # connection; a database that still returned metadata is not "unavailable".
        analysis_mode = ("STATIC_DATABASE_METADATA" if read_only_plan_skipped_with_metadata
                         else "STATIC_DATABASE_UNAVAILABLE")

    aggregate_statement = max(
        statements,
        key=lambda statement: statement["score"],
        default=None,
    )
    overall_score = aggregate_statement["score"] if aggregate_statement else 0
    overall_risk_level = (
        aggregate_statement["risk_level"] if aggregate_statement else risk_level_from_score(
            0)
    )

    return {
        "statement_count": len(statements),
        "overall_score": overall_score,
        "overall_risk_level": overall_risk_level,
        "analysis_mode": analysis_mode,
        "statements": statements,
    }


def build_statement_result(static_report, analysis, evidence: DatabaseEvidence, metadata: MetadataEvidence) -> dict:
    """Score one statement with collected evidence and build its response payload.

    Shared by local /api/validate and the connector's remote validate so equivalent
    evidence yields identical scoring: static report -> EXPLAIN evidence
    adjustments -> M1 metadata -> M2 -> final report. Collection is the caller's job.
    """
    evidence_result = apply_evidence_adjustments(static_report, evidence)
    phase2_report = evidence_result.report

    m1_result = apply_m1_adjustment(
        phase2_report,
        metadata,
        full_table_scan=evidence.full_table_scan,
        high_estimated_rows_applied=evidence_result.high_estimated_rows_applied,
        risk_level_for_score=risk_level_from_score,
    )
    m2_result = apply_m2_adjustment(
        m1_result.report,
        metadata,
        evidence,
        risk_level_for_score=risk_level_from_score,
        phase2_risk_level=phase2_report.risk_level,
    )
    final_report = m2_result.report
    metadata_factors = [
        *m1_result.metadata_factors,
        *m2_result.factors,
    ]

    payload = {
        "index": final_report.facts.index,
        "sql": final_report.facts.raw_sql,
        "statement_type": final_report.facts.statement_type,
        "category": final_report.facts.category,
        "tables": final_report.facts.tables,
        "has_where": final_report.facts.has_where,
        "where_looks_trivial": final_report.facts.where_looks_trivial,
        "where_predicate": final_report.facts.where_predicate_summary,
        "columns_in_where": final_report.facts.columns_in_where,
        "has_limit": final_report.facts.has_limit,
        "is_full_table_ddl": final_report.facts.is_full_table_ddl,
        "parse_error": final_report.facts.parse_error,
        "static_score": static_report.score,
        "static_risk_level": static_report.risk_level,
        "score": final_report.score,
        "risk_level": final_report.risk_level,
        "confidence": final_report.confidence,
        "factors": [
            {"label": factor.label, "points": factor.points}
            for factor in static_report.factors
        ],
        "evidence_factors": [
            {"label": factor.label, "points": factor.points}
            for factor in evidence_result.factors
        ],
        "metadata_score_adjustment": m1_result.metadata_score_adjustment + m2_result.adjustment,
        "metadata_factors": [asdict(factor) for factor in metadata_factors],
        "metadata_status": metadata.status,
        "high_estimated_rows_applied": evidence_result.high_estimated_rows_applied,
        "reasons": final_report.reasons,
        "checklist": final_report.checklist,
    }

    if analysis.assessment is not None:
        try:
            payload["v2_assessment"] = asdict(analysis.assessment)
        except Exception:
            payload["v2_assessment"] = None

    payload["database_evidence"] = {
        "available": evidence.available,
        "database": evidence.database,
        "tables": evidence.tables,
        "explain_available": evidence.explain_available,
        "estimated_rows": evidence.estimated_rows,
        "access_type": evidence.access_type,
        "possible_keys": evidence.possible_keys,
        "key": evidence.key,
        "key_length": evidence.key_length,
        "extra": evidence.extra,
        "full_table_scan": evidence.full_table_scan,
        "estimated_rows_semantics": "optimizer estimate, not exact affected-row count",
        "metadata_status": metadata.status,
        "error": evidence.error,
        # Stable, browser-safe reason (see backend.db.EVIDENCE_STATUSES).
        "status": evidence_status(evidence),
    }

    return payload


@app.get("/api/health")
def health():
    client = get_client()
    configured = bool(client.configured)
    db_status = "not_configured"

    if configured:
        probe = client.execute_readonly("SELECT 1 AS ok", max_rows=1)
        if probe.get("status") == "ok":
            db_status = "connected"
        else:
            db_status = probe.get("status", "unavailable")

    return {
        "status": "ok",
        "version": APP_VERSION,
        "database": {
            "configured": configured,
            "status": db_status,
        },
    }


class EvidenceRequest(PydanticBaseModel):
    sql: str


@app.post("/api/evidence")
def run_evidence(req: EvidenceRequest):
    """Reject public arbitrary SQL evidence queries."""
    return {
        "status": "error",
        "error": "evidence_queries_disabled",
        "message": "Public arbitrary evidence queries are disabled. Use Local validation or the Company connector-only evidence contract.",
    }


app.mount(
    "/static",
    StaticFiles(directory=str(FRONTEND_DIR)),
    name="static",
)


@app.get("/")
def index():

    return FileResponse(
        str(FRONTEND_DIR / "index.html")
    )
