from __future__ import annotations
from .m2_scoring import apply_m2_adjustment, describe_filter_index
from .metadata_scoring import apply_m1_adjustment
from .metadata import MetadataEvidence, collect_metadata
from .evidence_scoring import apply_evidence_adjustments, plan_is_interpretable
from .row_scope import AT_MOST_ONE_ROW, build_row_scope
from .db_evidence import (
    PLAN_NOT_ELIGIBLE,
    DatabaseEvidence,
    ReadOnlySessionLost,
    collect_statement_evidence,
    evidence_status,
    select_not_eligible_for_plan,
)
from .evidence_state import (
    CONNECTED,
    NOT_ATTEMPTED,
    EvidenceConnection,
    analysis_mode as derive_analysis_mode,
    build_evidence_state,
    connection_failed,
    restate_assessment,
    restate_reasons,
    summarize as summarize_evidence,
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
from dataclasses import asdict, replace
from .risk_engine import (
    OFFLINE_ROW_IMPACT_REASON,
    CONFIDENCE_ORDER,
    ScoreFactor,
    score_statement,
    weakest_confidence,
)
from .analyzer import analyze_batch
from .optimization import advise
from .parser import has_executable_comment, parse_sql, unsupported_script
from .version import APP_VERSION
from .update_check import check_for_update
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI, Request

import ipaddress
import os
import re
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


LOOPBACK_HOSTS = ("127.0.0.1", "localhost")
_HOST_NAME = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*")


def configured_allowed_hosts() -> list[str]:
    """Return the Host header names this server answers.

    Always the loopback names, plus the exact host names in MDV_ALLOWED_HOSTS
    (comma-separated) for a trusted self-hosted setup. An entry that is not a plain
    host name or IPv4 address, such as "*" or "*.example.com", is ignored, so the
    check cannot be turned off. A request for any other host, such as another
    website's DNS name rebound to 127.0.0.1, gets 400.
    """
    configured = os.environ.get("MDV_ALLOWED_HOSTS", "")
    extra = [host.strip().lower()
             for host in configured.split(",") if host.strip()]
    return list(LOOPBACK_HOSTS) + [
        host for host in extra
        if _HOST_NAME.fullmatch(host) and host not in LOOPBACK_HOSTS
    ]


app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=configured_allowed_hosts(),
    www_redirect=False,
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


def script_error(sql: str, all_facts: list) -> str | None:
    """The error code when a request has nothing to assess or cannot be split into
    statements reliably; None when its statements can be assessed one by one.
    Shared by local /api/validate and the connector's remote validate.

    - "executable_comment_not_supported": an executable comment ("/*! ... */"), whose
      contents MySQL runs but the parser reads as a comment. Checked first: a script
      whose only statement is in such a comment is not "no_statements".
    - "no_statements": empty, whitespace-only or comment-only input. There is nothing
      to assess, so no risk level or confidence is returned (never "LOW").
    - "delimiter_not_supported" / "stored_program_not_supported": see
      backend.parser.unsupported_script.
    - "statement_limit_exceeded": more than MAX_STATEMENTS statements.
    """
    if has_executable_comment(sql):
        return "executable_comment_not_supported"
    if not all_facts:
        return "no_statements"
    unsupported = unsupported_script(sql)
    if unsupported is not None:
        return unsupported
    if len(all_facts) > MAX_STATEMENTS:
        return "statement_limit_exceeded"
    return None


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
    error = script_error(req.sql, all_facts)
    if error is not None:
        return {"error": error}

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

    if evidence_connection is not None:
        connection = CONNECTED
    elif connection_attempted:
        connection = connection_failed(connection_status)
    else:
        connection = NOT_ATTEMPTED

    statements = []

    try:
        for report, analysis in zip(reports, analyses):
            static_report = report
            statement_connection = connection
            if evidence_connection is None:
                # Remote targets never reach here with a connection (fail-closed), and a
                # local request without a usable connection reports why.
                evidence = DatabaseEvidence(available=False, error=connection_status)
                metadata = None
            else:
                lost = False
                try:
                    if select_not_eligible_for_plan(report.facts):
                        evidence = DatabaseEvidence(available=False, error=PLAN_NOT_ELIGIBLE)
                    else:
                        evidence = collect_statement_evidence(
                            report.facts.raw_sql, connection=evidence_connection,
                            source_facts=report.facts)
                except ReadOnlySessionLost:
                    lost = True
                    evidence = DatabaseEvidence(available=False, error="collection_failed")
                except Exception:
                    evidence = DatabaseEvidence(
                        available=False, error="collection_failed")
                if lost:
                    # The collector closed the connection: nothing more is collected on it.
                    evidence_connection = None
                    connection_status = "collection_failed"
                    connection = connection_failed(connection_status)
                    metadata = None
                else:
                    try:
                        metadata = collect_metadata(
                            report.facts.tables,
                            connection=evidence_connection,
                            database=req.database,
                        )
                    except Exception:
                        metadata = MetadataEvidence(status="unavailable")

            statements.append(build_statement_result(
                static_report, analysis, evidence, metadata, statement_connection))
    finally:
        if evidence_connection is not None:
            try:
                evidence_connection.close()
            except Exception:
                pass

    return batch_result(statements, connection)


def batch_result(statements: list[dict], connection: EvidenceConnection) -> dict:
    """The validation response for scored statements (shared with the connector).

    analysis_mode is a compatibility label derived from the statements' evidence
    states; evidence_summary and each statement's evidence are authoritative.
    """
    evidence_states = [statement["evidence"] for statement in statements]
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
        # A batch is only as well understood as its least understood statement.
        "overall_confidence": (weakest_confidence([statement["confidence"] for statement in statements])
                               if statements else None),
        "analysis_mode": derive_analysis_mode(evidence_states),
        "evidence_summary": summarize_evidence(connection, evidence_states),
        "statements": statements,
    }


# Verified unique-key scope: replaces the +5 "affected-row count cannot be established"
# factor and credits -5, and only when MySQL table metadata confirms the key.
SCOPE_VERIFIED_POINTS = -10

# The credit never takes a write below MEDIUM (score 20). A verified key bounds the
# directly matched rows of the target table; it does not establish the total data
# impact (triggers, foreign-key actions, whether the row exists or changes). So a
# verified one-row UPDATE/DELETE is MEDIUM at least: the credit can lower a score
# within MEDIUM or above, never to LOW.
SCOPE_VERIFIED_RISK_FLOOR = 20
SCOPE_FLOOR_REASON = (
    "MEDIUM is the minimum overall risk for an UPDATE or DELETE whose direct match is "
    "bounded by a verified unique key: the bound covers the target row only, not the "
    "total data impact."
)


INDIRECT_EFFECTS_NOT_CHECKED = (
    "Triggers, foreign-key actions (ON DELETE / ON UPDATE CASCADE, SET NULL) and other "
    "indirect effects on other rows or tables were not checked."
)


def scope_reason(scope: dict) -> str:
    return (f"At most one row of {scope['table']} can match directly: the WHERE clause fixes every "
            f"column of unique key {scope['index']} (from table metadata). The row may not exist, "
            "and an UPDATE can change fewer rows than it matches. " + INDIRECT_EFFECTS_NOT_CHECKED)


def apply_scope_adjustment(report, scope: dict):
    """The score credit for a verified unique-key scope, or (report, None, False).

    Returns (report, factor, floor_applied). The credit is SCOPE_VERIFIED_POINTS but
    never takes the score below SCOPE_VERIFIED_RISK_FLOOR (MEDIUM); the factor carries
    the points actually applied, which can be 0. Scope and confidence are unaffected:
    the verified key is still reported as the target's direct row scope.
    """
    if scope["basis"] != AT_MOST_ONE_ROW or report.risk_level == "CRITICAL":
        return report, None, False
    credited = max(0, min(100, report.score + SCOPE_VERIFIED_POINTS))
    score = max(credited, min(report.score, SCOPE_VERIFIED_RISK_FLOOR))
    floor_applied = score > credited
    factor = ScoreFactor(f"Unique key {scope['index']} limits the match to at most one row",
                         score - report.score)
    reasons = [scope_reason(scope) if reason == OFFLINE_ROW_IMPACT_REASON else reason
               for reason in report.findings]
    if floor_applied:
        reasons.append(SCOPE_FLOOR_REASON)
    return (replace(report, score=score, risk_level=risk_level_from_score(score), findings=reasons),
            factor, floor_applied)


def plan_estimate_reason(evidence: DatabaseEvidence, high_rows: bool) -> str:
    """The Execution Risk reason when a collected plan's estimates raised the score."""
    estimates = (["a full table scan"] if evidence.full_table_scan is True else []) + (
        [f"about {evidence.estimated_rows:,} rows examined"] if high_rows else [])
    return ("Execution plan collected: the optimizer estimates " + " and ".join(estimates)
            + ". These are estimates, not a measured runtime.")


def _cap(level: str, ceiling: str) -> str:
    return level if CONFIDENCE_ORDER[level] <= CONFIDENCE_ORDER[ceiling] else ceiling


def final_confidence(static_confidence: str, scope: dict, state: dict) -> str:
    """Statement confidence: the static level, raised only by a verified row scope, capped.

    Missing or failed evidence never raises it. A unique key confirmed in table
    metadata establishes the row scope of a write (HIGH unless the SQL was unreadable).
    A referenced table that metadata did not find, or whose existence could not be
    determined, caps it at LIMITED: the assessment may be about the wrong schema.
    """
    level = static_confidence
    if scope["basis"] == AT_MOST_ONE_ROW and level != "LOW":
        level = "HIGH"
    tables = state["metadata"].get("tables") or []
    if any(table.get("found") is not True for table in tables):
        level = _cap(level, "LIMITED")
    return level


def build_statement_result(static_report, analysis, evidence: DatabaseEvidence,
                           metadata: MetadataEvidence | None, connection: EvidenceConnection) -> dict:
    """Score one statement with collected evidence and build its response payload.

    Shared by local /api/validate and the connector's remote validate so equivalent
    evidence yields identical scoring and evidence semantics: static report -> EXPLAIN
    evidence adjustments -> M1 metadata -> M2 -> final report. Collection is the
    caller's job; metadata is None when it was not requested.
    """
    if (static_report.facts.statement_type == "SELECT" and not evidence.available
            and (evidence.error or "").startswith("unsupported_statement_type")):
        # A SELECT whose regenerated text is not a SELECT (SELECT ... INTO becomes
        # CREATE TABLE ... AS SELECT) gets no plan request: it is not eligible, which
        # is different from a statement type that never has a plan.
        evidence = replace(evidence, error=PLAN_NOT_ELIGIBLE)
    state = build_evidence_state(connection, evidence, metadata, list(static_report.facts.tables))
    if metadata is None:
        metadata = MetadataEvidence(status="unavailable")
    scope = build_row_scope(static_report.facts, metadata)
    scoped_report, scope_factor, floor_applied = apply_scope_adjustment(static_report, scope)
    # points: the change actually applied (0 when the MEDIUM floor absorbs the credit).
    scope["factor"] = ({"label": scope_factor.label, "points": scope_factor.points,
                        "medium_floor_applied": floor_applied}
                       if scope_factor else None)
    # Plan fields describe EXPLAIN's first row: only a one-row plan is interpreted.
    interpreted = evidence if plan_is_interpretable(evidence) else replace(evidence, full_table_scan=None)
    evidence_result = apply_evidence_adjustments(scoped_report, evidence)
    phase2_report = evidence_result.report

    m1_result = apply_m1_adjustment(
        phase2_report,
        metadata,
        full_table_scan=interpreted.full_table_scan,
        high_estimated_rows_applied=evidence_result.high_estimated_rows_applied,
        risk_level_for_score=risk_level_from_score,
    )
    m2_result = apply_m2_adjustment(
        m1_result.report,
        metadata,
        interpreted,
        risk_level_for_score=risk_level_from_score,
        phase2_risk_level=phase2_report.risk_level,
    )
    final_report = m2_result.report
    confidence = final_confidence(static_report.confidence, scope, state)
    metadata_factors = [
        *m1_result.metadata_factors,
        *m2_result.factors,
    ]

    payload = {
        "index": final_report.facts.index,
        # As submitted; raw_sql of an analysed statement is the parser's regeneration.
        "sql": final_report.facts.submitted_sql or final_report.facts.raw_sql,
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
        # "analysed", or why not: "invalid", "unsupported" or "unreadable" (backend.parser).
        "analysis_status": final_report.facts.analysis_status,
        "static_score": static_report.score,
        "static_risk_level": static_report.risk_level,
        "score": final_report.score,
        "risk_level": final_report.risk_level,
        "confidence": confidence,
        "static_confidence": static_report.confidence,
        # What is established about the rows an UPDATE/DELETE matches (backend.row_scope).
        "scope": scope,
        # The SQL could only be read partly: the analysed text may differ from the submitted text.
        "analysis_text_differs": bool(final_report.facts.lossy_parse),
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
        # Filter and index assessment from table metadata (display only; see m2_scoring).
        "filter_index": describe_filter_index(final_report.facts, metadata),
        "reasons": restate_reasons(final_report.reasons, state),
        "checklist": final_report.checklist,
    }

    # Advisory only, from the SQL alone, after the result above is final: it never
    # changes the score, confidence, completeness or evidence (backend.optimization).
    try:
        payload["optimization"] = advise(final_report.facts)
    except Exception:
        payload["optimization"] = {"status": "unavailable", "recommendations": []}

    if analysis.assessment is not None:
        try:
            assessment = restate_assessment(analysis.assessment, state)
            if evidence_result.factors:
                # The plan's estimates raised the score (full table scan, high row
                # estimate): the execution dimension says so, as optimizer estimates.
                reason = plan_estimate_reason(evidence, evidence_result.high_estimated_rows_applied)
                assessment = replace(assessment, dimensions=[
                    replace(dimension, level="MEDIUM", reason=reason)
                    if dimension.name == "Execution Risk" and dimension.level == "UNKNOWN" else dimension
                    for dimension in assessment.dimensions])
            # The analyzer's score fields are placeholders; never return them as a result.
            assessment = replace(assessment, overall_score=final_report.score,
                                 risk_level=final_report.risk_level, confidence=confidence)
            if scope["basis"] == AT_MOST_ONE_ROW:
                # The direct match is bounded; the total data impact is not: Data Risk
                # keeps its level, and the unknown-count finding says what is still open.
                direct = (f"Direct match: at most one row of {scope['table']} "
                          f"(unique key {scope['index']}, from table metadata). ")
                assessment = replace(
                    assessment,
                    dimensions=[
                        replace(dimension, reason=direct + INDIRECT_EFFECTS_NOT_CHECKED)
                        if dimension.name == "Data Risk" else dimension
                        for dimension in assessment.dimensions],
                    findings=[
                        replace(finding, title="Direct match bounded; total impact not established",
                                description=direct + "Whether the row exists and how many rows change "
                                "are not known. " + INDIRECT_EFFECTS_NOT_CHECKED)
                        if finding.title == "Affected-row count is unknown" else finding
                        for finding in assessment.findings])
            payload["v2_assessment"] = asdict(assessment)
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
    # The authoritative evidence state for this statement (backend.evidence_state).
    payload["evidence"] = state

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


HEARTBEAT_PAGE_HOSTS = ("127.0.0.1", "localhost")


def is_own_loopback_page(request: Request) -> bool:
    """True only for a browser request made by this server's own loopback page.

    The browser sets Origin; it must be exactly this server (``http://`` + Host) on a
    loopback name. Another website, including one that rebinds its DNS name to
    127.0.0.1, has a different Origin and Host, and a request without Origin
    (not from a page) is refused.
    """
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if not origin or origin != f"http://{host}":
        return False
    return host.rsplit(":", 1)[0] in HEARTBEAT_PAGE_HOSTS


@app.post("/api/heartbeat")
def heartbeat(request: Request):
    """The page is still open: keeps the windowed desktop app from idling out.

    Carries no data, returns none and creates no session. Without idle shutdown
    (console version, development server) it does nothing.
    """
    if not is_own_loopback_page(request):
        return Response(status_code=403)
    on_heartbeat = getattr(request.app.state, "on_heartbeat", None)
    if on_heartbeat is not None:
        on_heartbeat()
    return Response(status_code=204)


@app.post("/api/update-status")
def update_status(request: Request):
    """Whether a newer official release exists (backend.update_check).

    Only for this server's own loopback page, as the heartbeat, so another website
    cannot make the app contact GitHub. Takes no data; any body is ignored.
    """
    if not is_own_loopback_page(request):
        return Response(status_code=403)
    return check_for_update()


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


HOMEPAGE_PREVIEW_ENV = "MDV_HOMEPAGE_PREVIEW"


def homepage_preview_enabled() -> bool:
    """Local development preview of the product homepage (MDV_HOMEPAGE_PREVIEW=1).

    Routing only: it changes no security control and does not make the server safe
    to expose publicly. The downloaded app never sets it (and does not bundle
    home.html), so "/" stays the validator there.
    """
    return os.environ.get(HOMEPAGE_PREVIEW_ENV) == "1" and (FRONTEND_DIR / "home.html").is_file()


@app.get("/")
def index():
    if homepage_preview_enabled():
        return FileResponse(str(FRONTEND_DIR / "home.html"))
    return FileResponse(
        str(FRONTEND_DIR / "index.html")
    )


@app.get("/demo")
def demo():
    """The validator, at the address the homepage links to."""
    return FileResponse(str(FRONTEND_DIR / "index.html"))
