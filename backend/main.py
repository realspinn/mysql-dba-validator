from __future__ import annotations
from .metadata import MetadataEvidence, collect_metadata
from .db_evidence import (
    PLAN_NOT_ELIGIBLE,
    DatabaseEvidence,
    ReadOnlySessionLost,
    collect_statement_evidence,
    select_not_eligible_for_plan,
)
from .evidence_state import (
    CONNECTED,
    NOT_ATTEMPTED,
    connection_failed,
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
from .risk_engine import score_statement
from .analyzer import analyze_batch
from .parser import parse_sql
from .validation_results import batch_result, build_statement_result, script_error
from .version import APP_VERSION
from .update_check import check_for_update
from . import lock_diagnostics, observed_performance, update_check, update_download
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi import FastAPI, HTTPException, Request

import ipaddress
import mimetypes
import os
import posixpath
import re
import socket
import threading
from enum import Enum
from pathlib import Path
from typing import Literal
from dotenv import load_dotenv

# Load .env from project root (if present) so DB config can be provided via .env file
load_dotenv()


PUBLIC_DEMO_ENV = "MDV_PUBLIC_DEMO"


def public_demo_enabled() -> bool:
    """The hosted online demo (MDV_PUBLIC_DEMO=1): static analysis only.

    Read once at startup; no request can change it. Unset or "0" is the normal local
    application. Any other value stops the server from starting rather than guessing.
    """
    value = os.environ.get(PUBLIC_DEMO_ENV, "")
    if value in ("", "0"):
        return False
    if value == "1":
        return True
    raise RuntimeError(f"{PUBLIC_DEMO_ENV} must be 0 or 1")


PUBLIC_DEMO = public_demo_enabled()

if os.environ.get("RENDER") and not PUBLIC_DEMO:
    # Render sets RENDER=true in every service. Hosted there, the server is the public
    # demo; a missing setting must not publish the full local application.
    raise RuntimeError(f"On Render the server runs only with {PUBLIC_DEMO_ENV}=1")

if PUBLIC_DEMO and os.environ.get("CORS_ALLOW_ORIGINS", "").strip():
    # The demo page and its API share one origin; cross-origin access is never needed.
    raise RuntimeError(f"CORS_ALLOW_ORIGINS cannot be set with {PUBLIC_DEMO_ENV}=1")

app = FastAPI(
    title="MySQL DBA Validator",
    version=APP_VERSION,
    # The public demo publishes no API description or interactive docs.
    **({"docs_url": None, "redoc_url": None, "openapi_url": None} if PUBLIC_DEMO else {}),
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


MAX_SQL_BYTES = 64 * 1024

# Public demo: the only routes served. Everything else (connection test, database
# discovery, evidence, heartbeat, update check, API docs) is 404 before any route
# code runs, whatever the request contains.
PUBLIC_ROUTES = frozenset({
    ("GET", "/"),
    ("GET", "/demo"),
    ("GET", "/api/health"),
    ("POST", "/api/validate"),
})
PUBLIC_STATIC_PREFIX = "/static/assets/"
# Largest legitimate /api/validate body: MAX_SQL_BYTES of SQL, where JSON can write one
# byte as up to six (a control character becomes a \u00XX escape), plus room for the key.
PUBLIC_MAX_BODY_BYTES = 6 * MAX_SQL_BYTES + 16 * 1024


def public_route_allowed(method: str, path: str) -> bool:
    if (method, path) in PUBLIC_ROUTES:
        return True
    # Only the assets folder, and no "..", "//" or backslash that could leave it.
    return (method == "GET" and path.startswith(PUBLIC_STATIC_PREFIX)
            and posixpath.normpath(path) == path and "\\" not in path)


class PublicDemoGuard:
    """ASGI middleware for the public demo: route allowlist and request-body bound.

    The body bound holds before the body is buffered: a declared Content-Length over
    the limit is refused at once, and the bytes actually received are counted as they
    arrive (chunked bodies, or a Content-Length that understates the body), so no more
    than the limit plus one chunk is ever held.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not public_route_allowed(scope["method"], scope["path"]):
            await JSONResponse({"detail": "Not Found"}, status_code=404)(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            if not declared.isdigit():
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
                return
            if int(declared) > PUBLIC_MAX_BODY_BYTES:
                await JSONResponse({"error": "request_too_large"}, status_code=413)(scope, receive, send)
                return
        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > PUBLIC_MAX_BODY_BYTES:
                    # FastAPI passes an HTTPException raised while reading the body
                    # through as the response.
                    raise HTTPException(status_code=413, detail="request_too_large")
            return message

        await self.app(scope, bounded_receive, send)


if PUBLIC_DEMO:
    app.add_middleware(PublicDemoGuard)


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
    if PUBLIC_DEMO:
        # The public demo answers only the host names it is deployed under (loopback
        # too only if listed), and does not start without one.
        public = [host for host in extra if _HOST_NAME.fullmatch(host)]
        if not public:
            raise RuntimeError(f"{PUBLIC_DEMO_ENV}=1 needs MDV_ALLOWED_HOSTS with the public host name")
        return public
    return list(LOOPBACK_HOSTS) + [
        host for host in extra
        if _HOST_NAME.fullmatch(host) and host not in LOOPBACK_HOSTS
    ]


app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=configured_allowed_hosts(),
    www_redirect=False,
)


# The page may call its own server and, in the local application, the local connector.
# The public demo has no connector, so only its own server.
CONNECT_SRC = "'self'" if PUBLIC_DEMO else "'self' http://127.0.0.1:8765 http://localhost:8765"


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        f"style-src 'self' 'unsafe-inline'; connect-src {CONNECT_SRC}; "
        "img-src 'self' data:; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


FRONTEND_DIR = (
    Path(__file__).resolve().parent.parent / "frontend"
)


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


def public_demo_refusal() -> JSONResponse:
    """The answer to anything the public demo does not do: it only analyses SQL text."""
    return JSONResponse(status_code=403, content={
        "error_code": "public_demo_static_only",
        "message": ("The online demo runs static analysis only. Database connections are "
                    "available only in the downloaded application."),
    })


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
    if PUBLIC_DEMO:  # also unreachable there (PublicDemoGuard); refused before any lookup
        return public_demo_refusal()
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
    if PUBLIC_DEMO:  # also unreachable there (PublicDemoGuard); refused before any lookup
        return public_demo_refusal()
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


class DiagnosticRequest(BaseModel):
    """A loopback login and the selected database. No SQL or query of any kind."""
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = Field(default=3306, ge=1, le=65535)
    username: str
    password: SecretStr
    database: str


def _local_diagnostic(req: DiagnosticRequest, collect, result):
    """Run one optional diagnostic for a Local Mode database.

    Separate from /api/validate: nothing here is evidence about submitted SQL. Same
    loopback-only target rule and the same per-request, read-only evidence connection
    as local validation; the collector sends only its own fixed queries.
    """
    if PUBLIC_DEMO:  # also unreachable there (PublicDemoGuard); refused before any lookup
        return public_demo_refusal()
    if not validate_database_identifier(req.database):
        return {"error_code": "invalid_database", "message": "Invalid database selection."}
    username = req.username.strip()
    password = req.password.get_secret_value()
    if not req.host.strip() or not username or not password:
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
    connection, status = open_evidence_connection(
        ConnectionProfile(host=req.host.strip(), port=req.port, username=username, password=password),
        req.database,
    )
    if connection is None:
        return result(req.database, status or "collection_failed")
    try:
        return collect(connection, req.database)
    finally:
        try:
            connection.close()
        except Exception:
            pass


@app.post("/api/diagnostics/observed-performance")
def observed_performance_endpoint(req: DiagnosticRequest):
    """Historical schema workload: statement families MySQL already recorded."""
    return _local_diagnostic(req, observed_performance.collect_observed_performance, observed_performance.result)


@app.post("/api/diagnostics/lock-waits")
def lock_waits_endpoint(req: DiagnosticRequest):
    """A snapshot of current InnoDB lock waits on tables in the selected database."""
    return _local_diagnostic(req, lock_diagnostics.collect_lock_waits, lock_diagnostics.result)


# Public demo: validations allowed at once. Analysis is CPU-bound, so more would only
# queue; a request over the limit is told to retry instead of waiting.
PUBLIC_MAX_CONCURRENT_VALIDATIONS = 2
_public_validations = threading.BoundedSemaphore(PUBLIC_MAX_CONCURRENT_VALIDATIONS)


@app.post("/api/validate")
def validate(req: ValidateRequest):
    if PUBLIC_DEMO:
        # Only the SQL text. A connection field, even empty, is refused rather than
        # dropped, before the target is classified (which can look up a host name).
        if req.model_fields_set != {"sql"}:
            return public_demo_refusal()
        if not _public_validations.acquire(blocking=False):
            return JSONResponse(status_code=503, headers={"Retry-After": "5"},
                                content={"error": "server_busy"})
        try:
            return _validate_or_report_too_complex(req)
        finally:
            _public_validations.release()
    return _validate_or_report_too_complex(req)


def _validate_or_report_too_complex(req: ValidateRequest):
    try:
        return _validate(req)
    except RecursionError:
        # Deeply nested or very long expressions can exceed Python's recursion limit
        # in the analysis; nothing was assessed. Any evidence connection is already
        # closed by _validate.
        return {"error": "sql_too_complex"}


def _validate(req: ValidateRequest):

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


@app.get("/api/health")
def health():
    if PUBLIC_DEMO:
        # Readiness only: no database probe even if MYSQL_* variables are set, and
        # nothing about the deployment.
        return {"status": "ok"}
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
    result = check_for_update()
    if update_check.in_app_download_supported():
        update_download.remove_installed_downloads()
    return result


# Download and verify a newer release in the Windows desktop app (backend.update_download).
# Same caller rule as the update check; nothing is installed or run. Not in the public
# demo's route allowlist, so 404 there.
@app.post("/api/update-download")
def update_download_start(request: Request):
    if not is_own_loopback_page(request):
        return Response(status_code=403)
    asset = update_check.checked_asset()
    if asset is None or not update_check.in_app_download_supported():
        return JSONResponse(status_code=409, content={"error": "update_download_unavailable"})
    if not update_download.start(asset):
        return JSONResponse(status_code=409, content={"error": "update_download_running"})
    return update_download.snapshot()


@app.post("/api/update-progress")
def update_download_progress(request: Request):
    if not is_own_loopback_page(request):
        return Response(status_code=403)
    return update_download.snapshot()


@app.post("/api/update-cancel")
def update_download_cancel(request: Request):
    if not is_own_loopback_page(request):
        return Response(status_code=403)
    update_download.cancel()
    return update_download.snapshot()


@app.post("/api/update-show")
def update_download_show(request: Request):
    if not is_own_loopback_page(request):
        return Response(status_code=403)
    if not update_check.in_app_download_supported() or not update_download.show_in_folder():
        return JSONResponse(status_code=409, content={"error": "update_not_ready"})
    return Response(status_code=204)


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


# The page's styles and scripts are separate files. On Windows, Python takes file
# types from the registry, where some installed programs register .js as text/plain;
# with nosniff the browser would then refuse the page's script. Fix the two types.
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/javascript", ".js")

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


# Each release's page asks for its own stylesheet and script (".../validator.js?v=0.10.1"),
# and the page itself is revalidated on every load. Without both, a browser reused the
# previous release's files after an update (the asset URLs did not change and nothing
# said not to cache them), so the new page ran the old script.
ASSET_LINK = re.compile(r'((?:href|src)="/static/assets/[\w/.-]+\.(?:css|js))"')
PAGE_HEADERS = {"Cache-Control": "no-cache"}


def frontend_page(name: str) -> str:
    """A page from frontend/, with its stylesheet and script links tagged with APP_VERSION.

    Nothing else changes, line endings included."""
    with open(FRONTEND_DIR / name, encoding="utf-8", newline="") as file:
        page = file.read()
    return ASSET_LINK.sub(rf'\1?v={APP_VERSION}"', page)


@app.get("/")
def index():
    if PUBLIC_DEMO or homepage_preview_enabled():
        return HTMLResponse(frontend_page("home.html"), headers=PAGE_HEADERS)
    return HTMLResponse(frontend_page("index.html"), headers=PAGE_HEADERS)


def public_demo_page() -> str:
    """The validator marked as the online demo. The page then shows the SQL privacy
    notice, hides the connection, connector and database controls, and sends only the
    SQL text; the server enforces the same limits whatever the page does."""
    page = frontend_page("index.html")
    marked = page.replace('<html lang="en">', '<html lang="en" data-public-demo>', 1)
    if marked == page:
        raise RuntimeError("frontend/index.html has no <html lang=\"en\"> tag to mark")
    return marked


PUBLIC_DEMO_PAGE = public_demo_page() if PUBLIC_DEMO else None


@app.get("/demo")
def demo():
    """The validator, at the address the homepage links to."""
    if PUBLIC_DEMO:
        return HTMLResponse(PUBLIC_DEMO_PAGE, headers=PAGE_HEADERS)
    return HTMLResponse(frontend_page("index.html"), headers=PAGE_HEADERS)
