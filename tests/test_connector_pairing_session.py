"""Milestone B: production browser -> local connector pairing/session flow.

Covers pairing, session scoping/authorization, origin configuration, the launcher,
and security regressions around the public API credential boundary.
"""

import io
import logging
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app as public_app
from connector import launcher
from connector.policy import CompanyTarget
from connector.server import (
    ALLOWED_ORIGINS_ENV_VAR,
    DEFAULT_ALLOWED_ORIGINS,
    SESSION_SCOPE_BROWSER,
    SESSION_SCOPE_OPERATOR,
    ConnectorRuntime,
    browser_session_route_allowed,
    create_app,
    normalize_allowed_origin,
    parse_allowed_origins,
)

PAGE_ORIGIN = "http://127.0.0.1:8420"
OTHER_ALLOWED_ORIGIN = "http://localhost:8420"
EVIL_ORIGIN = "https://evil.example"
OLD_TUNNEL_ORIGIN = "https://inf-extends-gardening-retrieval.trycloudflare.com"
SECRET_PASSWORD = "company-db-password-do-not-leak-91"
FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def runtime(tmp_path):
    # The launcher always persists the registry; keep it inside the test's tmp dir.
    config = launcher.build_config(
        ["--allow-origin", PAGE_ORIGIN, "--allow-origin", OTHER_ALLOWED_ORIGIN,
         "--registry", str(tmp_path / "targets.json")], environ={})
    return launcher.build_runtime(config)


@pytest.fixture
def client(runtime):
    with TestClient(create_app(runtime=runtime)) as test_client:
        yield test_client


def _json_headers(origin=PAGE_ORIGIN, session=None):
    headers = {"Origin": origin, "Content-Type": "application/json"}
    if session:
        headers["Authorization"] = f"Bearer {session}"
    return headers


def _pair(client, code, origin=PAGE_ORIGIN):
    return client.post("/pair", headers=_json_headers(origin), json={"pairing_token": code})


def _browser_session(client, origin=PAGE_ORIGIN):
    code = client.app.state.connector.issue_browser_pairing_token()
    response = _pair(client, code, origin)
    assert response.status_code == 200, response.text
    return response.json()["session_token"]


def _register(runtime, target_id, *, approved):
    target = runtime.register_company_target(CompanyTarget(
        target_id=target_id,
        display_name=target_id,
        host=f"{target_id}.db.example.com",
        port=3306,
        tls_required=True,
        approval_state="pending",
    ))
    if approved:
        # Simulates the operator approval step (DNS identity snapshot) without network.
        target.approval_state = "approved"
        target.dns_identity_snapshot = {"host": target.host, "addresses": ["203.0.113.10"]}
        target.tls_hostname = target.host
    return target


# ---------------------------------------------------------------- pairing


def test_browser_pairing_creates_browser_scoped_session(client):
    code = client.app.state.connector.issue_browser_pairing_token()
    response = _pair(client, code)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "paired"
    assert body["scope"] == SESSION_SCOPE_BROWSER
    assert body["expires_in_seconds"] == launcher.DEFAULT_BROWSER_SESSION_TTL_SECONDS
    assert body["session_token"] and body["session_token"] != code
    assert code not in response.text

    listed = client.get("/company/targets", headers=_json_headers(session=body["session_token"]))
    assert listed.status_code == 200


def test_pairing_codes_are_unique_high_entropy_and_short_lived(runtime):
    codes = {runtime.issue_browser_pairing_token() for _ in range(50)}
    assert len(codes) == 50
    assert all(len(code) >= 40 for code in codes)
    assert runtime.pairing_ttl_seconds == launcher.PAIRING_CODE_TTL_SECONDS == 300


def test_wrong_pairing_code_is_rejected(client):
    response = _pair(client, "q" * 43)
    assert response.status_code == 403
    assert response.json()["error_code"] == "invalid_pairing_token"
    assert "session_token" not in response.text


@pytest.mark.parametrize(
    "body, content_type, expected_status, expected_code",
    [
        ({"pairing_token": "short"}, "application/json", 400, "invalid_pairing_request"),
        ({}, "application/json", 400, "invalid_pairing_request"),
        ({"pairing_token": "x" * 43, "scope": "operator"}, "application/json", 400, "invalid_pairing_request"),
        ({"pairing_token": 12345678901234567890123456789012345}, "application/json", 400, "invalid_pairing_request"),
        (["not", "an", "object"], "application/json", 400, "invalid_pairing_request"),
        ({"pairing_token": "x" * 43}, "text/plain", 415, "unsupported_media_type"),
    ],
)
def test_malformed_pairing_input_is_rejected(client, body, content_type, expected_status, expected_code):
    import json
    response = client.post(
        "/pair",
        headers={"Origin": PAGE_ORIGIN, "Content-Type": content_type},
        content=json.dumps(body),
    )
    assert response.status_code == expected_status
    assert response.json()["error_code"] == expected_code


def test_non_json_pairing_body_is_rejected(client):
    response = client.post(
        "/pair",
        headers={"Origin": PAGE_ORIGIN, "Content-Type": "application/json"},
        content="pairing_token=abc",
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_pairing_request"


def test_client_cannot_request_operator_scope_during_pairing(client):
    code = client.app.state.connector.issue_browser_pairing_token()
    escalation = client.post(
        "/pair",
        headers=_json_headers(),
        json={"pairing_token": code, "scope": SESSION_SCOPE_OPERATOR},
    )
    assert escalation.status_code == 400
    # The code was not consumed by the rejected request and still yields a browser session.
    ok = _pair(client, code)
    assert ok.status_code == 200
    assert ok.json()["scope"] == SESSION_SCOPE_BROWSER


def test_expired_pairing_code_is_rejected(client):
    code = client.app.state.connector.issue_browser_pairing_token(expires_in_seconds=0)
    response = _pair(client, code)
    assert response.status_code == 403
    assert response.json()["error_code"] == "pairing_expired"


def test_pairing_code_replay_is_rejected(client):
    code = client.app.state.connector.issue_browser_pairing_token()
    assert _pair(client, code).status_code == 200
    replay = _pair(client, code)
    assert replay.status_code == 403
    assert replay.json()["error_code"] == "pairing_already_used"
    replay_other_origin = _pair(client, code, origin=OTHER_ALLOWED_ORIGIN)
    assert replay_other_origin.status_code == 403


def test_expired_browser_session_is_rejected(client):
    session = _browser_session(client)
    client.app.state.connector.sessions[session].expires_at = time.time() - 1
    response = client.get("/company/targets", headers=_json_headers(session=session))
    assert response.status_code == 401
    assert response.json()["error_code"] == "session_expired"


def test_session_is_bound_to_pairing_origin(client):
    session = _browser_session(client, origin=PAGE_ORIGIN)
    response = client.get("/company/targets", headers=_json_headers(origin=OTHER_ALLOWED_ORIGIN, session=session))
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_session"


def test_unpair_ends_browser_session(client):
    session = _browser_session(client)
    assert client.post("/unpair", headers=_json_headers(session=session)).status_code == 200
    after = client.get("/company/targets", headers=_json_headers(session=session))
    assert after.status_code == 401


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/company/targets", None),
        ("POST", "/company/targets/t1/databases", {"username": "u", "password": "p"}),
        ("POST", "/company/targets/t1/validate", {"sql": "SELECT 1", "username": "u", "password": "p"}),
    ],
)
@pytest.mark.parametrize("authorization", [None, "Bearer ", "Bearer not-a-real-session", "Basic abc"])
def test_protected_routes_require_a_session(client, runtime, method, path, body, authorization):
    _register(runtime, "t1", approved=True)
    headers = {"Origin": PAGE_ORIGIN, "Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    response = client.request(method, path, headers=headers, json=body)
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_session"


# ---------------------------------------------------------------- authorization


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("POST", "/company/targets", {"target_id": "evil", "display_name": "evil",
                                      "host": "evil.example.com", "port": 3306, "approval_state": "approved"}),
        ("POST", "/company/targets/t1/approve", None),
        ("DELETE", "/company/targets/t1", None),
        ("POST", "/company/targets/t1/connect", {"target_id": "t1"}),
        ("POST", "/company/targets/t1/revoke", None),
        ("POST", "/company/targets/t1/explain", {"operation": "EXPLAIN_SELECT", "database": "d", "table": "t"}),
        ("POST", "/db/test", {"host": "127.0.0.1", "port": 3306, "username": "u", "password": "p"}),
    ],
)
def test_browser_session_cannot_reach_operator_routes(client, runtime, method, path, body):
    _register(runtime, "t1", approved=False)
    session = _browser_session(client)
    response = client.request(method, path, headers=_json_headers(session=session), json=body)
    assert response.status_code == 403
    assert response.json()["error_code"] == "insufficient_session_scope"
    # Nothing about the registry changed.
    assert set(runtime.company_targets.targets) == {"t1"}
    assert runtime.company_targets.get("t1").approval_state == "pending"


def test_browser_session_cannot_approve_its_way_to_a_target(client, runtime):
    _register(runtime, "pending-target", approved=False)
    session = _browser_session(client)
    approve = client.post("/company/targets/pending-target/approve", headers=_json_headers(session=session))
    assert approve.status_code == 403
    discovery = client.post(
        "/company/targets/pending-target/databases",
        headers=_json_headers(session=session),
        json={"username": "u", "password": SECRET_PASSWORD},
    )
    assert discovery.status_code == 409
    assert discovery.json()["error_code"] == "company_target_not_approved"
    validate = client.post(
        "/company/targets/pending-target/validate",
        headers=_json_headers(session=session),
        json={"sql": "SELECT 1", "username": "u", "password": SECRET_PASSWORD},
    )
    assert validate.status_code == 409
    assert validate.json()["error_code"] == "company_target_not_approved"


def test_unknown_target_is_rejected_for_browser_session(client):
    session = _browser_session(client)
    response = client.post(
        "/company/targets/does-not-exist/validate",
        headers=_json_headers(session=session),
        json={"sql": "SELECT 1", "username": "u", "password": "p"},
    )
    assert response.status_code == 404
    assert response.json()["error_code"] == "unknown_company_target"


def test_browser_supplied_target_metadata_is_not_trusted(client, runtime):
    _register(runtime, "t1", approved=True)
    session = _browser_session(client)
    for injected in ({"host": "evil.example.com"}, {"port": 3307}, {"approval_state": "approved"},
                     {"target_id": "other"}, {"tls_hostname": "evil.example.com"}):
        response = client.post(
            "/company/targets/t1/validate",
            headers=_json_headers(session=session),
            json={"sql": "SELECT 1", "username": "u", "password": "p", **injected},
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "invalid_company_validation_request"


def test_browser_session_reaches_approved_target_only_through_destination_validation(client, runtime, monkeypatch):
    _register(runtime, "t1", approved=True)
    session = _browser_session(client)

    called = {}

    def fake_destination_check(target_id):
        called["target_id"] = target_id
        raise ValueError("destination_check_reached")

    monkeypatch.setattr(runtime, "resolve_and_validate_approved_company_target", fake_destination_check)
    response = client.post(
        "/company/targets/t1/validate",
        headers=_json_headers(session=session),
        json={"sql": "SELECT 1", "username": "dba", "password": SECRET_PASSWORD},
    )
    # Authorization passed and the existing DNS/TLS/exact-destination path was invoked.
    assert called == {"target_id": "t1"}
    assert response.status_code == 400
    assert response.json()["error_code"] == "destination_check_reached"
    assert SECRET_PASSWORD not in response.text


def test_operator_scope_behavior_is_unchanged(client, runtime):
    code = runtime.issue_pairing_token()
    session = _pair(client, code).json()["session_token"]
    assert runtime.session_scope(session) == SESSION_SCOPE_OPERATOR
    response = client.post(
        "/company/targets",
        headers=_json_headers(session=session),
        json={"target_id": "op-target", "display_name": "Op", "host": "op.db.example.com", "port": 3306},
    )
    assert response.status_code == 201


def test_unknown_scope_cannot_be_issued(runtime):
    with pytest.raises(ValueError):
        runtime.issue_pairing_token(scope="admin")


@pytest.mark.parametrize(
    "method, path, allowed",
    [
        ("GET", "/health", True),
        ("POST", "/unpair", True),
        ("GET", "/company/targets", True),
        ("POST", "/company/targets/abc/databases", True),
        ("POST", "/company/targets/abc/validate", True),
        ("POST", "/company/targets/abc/credentials", False),
        ("POST", "/company/targets", False),
        ("POST", "/company/targets/abc/approve", False),
        ("POST", "/company/targets/abc/revoke", False),
        ("DELETE", "/company/targets/abc", False),
        ("GET", "/company/targets/abc/databases", False),
        ("POST", "/company/targets/abc/explain", False),
        ("POST", "/company/targets/abc/connect", False),
        ("POST", "/company/targets/a/b/validate", False),
        ("GET", "/company/targets/abc/validate", False),
        ("POST", "/db/test", False),
        ("POST", "/pair", False),
    ],
)
def test_browser_route_allowlist(method, path, allowed):
    assert browser_session_route_allowed(method, path) is allowed


def test_no_http_route_issues_pairing_codes(client):
    paths = {getattr(route, "path", None) for route in client.app.routes}
    paths.discard(None)
    expected = {
        "/health", "/pair", "/unpair", "/company/targets",
        "/company/targets/{target_id}/approve", "/company/targets/{target_id}/connect",
        "/company/targets/{target_id}/validate", "/company/targets/{target_id}/revoke",
        "/company/targets/{target_id}/databases", "/company/targets/{target_id}/explain",
        "/company/targets/{target_id}",
        "/db/test", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc",
    }
    assert paths <= expected, paths - expected
    health = client.get("/health", headers={"Origin": PAGE_ORIGIN})
    assert "pairing" not in health.text.lower()


# ---------------------------------------------------------------- origins


def test_no_origin_is_trusted_by_default():
    assert DEFAULT_ALLOWED_ORIGINS == []
    default_runtime = ConnectorRuntime()
    assert default_runtime.allowed_origins == []
    assert default_runtime.validate_origin(OLD_TUNNEL_ORIGIN) is False
    assert default_runtime.validate_origin(PAGE_ORIGIN) is False
    with TestClient(create_app()) as default_client:
        code = default_client.app.state.connector.issue_browser_pairing_token()
        response = default_client.post(
            "/pair", headers=_json_headers(origin=PAGE_ORIGIN), json={"pairing_token": code})
        assert response.status_code == 401
        assert response.json()["error_code"] == "invalid_origin"


def test_temporary_tunnel_origin_is_not_in_production_code():
    root = Path(__file__).resolve().parent.parent
    for relative in ("connector/server.py", "connector/launcher.py", "frontend/index.html", "backend/main.py"):
        assert "trycloudflare" not in (root / relative).read_text(encoding="utf-8"), relative


@pytest.mark.parametrize(
    "value, expected",
    [
        ("http://127.0.0.1:8420", "http://127.0.0.1:8420"),
        ("HTTP://LOCALHOST:8420/", "http://localhost:8420"),
        ("http://[::1]:8420", "http://[::1]:8420"),
        ("https://validator.example.com", "https://validator.example.com"),
        ("https://validator.example.com:443", "https://validator.example.com"),
        ("https://validator.example.com:8443", "https://validator.example.com:8443"),
        ("http://127.0.0.1:80", "http://127.0.0.1"),
    ],
)
def test_allowed_origin_normalization(value, expected):
    assert normalize_allowed_origin(value) == expected


@pytest.mark.parametrize(
    "value",
    ["", "*", "null", "https://*.example.com", "http://validator.example.com", "ftp://127.0.0.1",
     "https://validator.example.com/app", "https://validator.example.com?x=1",
     "https://validator.example.com#frag", "https://user:pw@validator.example.com",
     "127.0.0.1:8420", "https://", "http://127.0.0.1:99999"],
)
def test_invalid_allowed_origins_are_rejected(value):
    with pytest.raises(ValueError):
        normalize_allowed_origin(value)


def test_parse_allowed_origins_from_env_style_string():
    parsed = parse_allowed_origins(" http://127.0.0.1:8420 , https://validator.example.com,http://127.0.0.1:8420 ")
    assert parsed == ["http://127.0.0.1:8420", "https://validator.example.com"]
    with pytest.raises(ValueError):
        parse_allowed_origins("http://127.0.0.1:8420,*")


def test_configured_origin_accepted_and_others_rejected(client):
    assert _pair(client, client.app.state.connector.issue_browser_pairing_token()).status_code == 200
    for origin in (EVIL_ORIGIN, OLD_TUNNEL_ORIGIN, "null", "http://127.0.0.1:8421", "https://127.0.0.1:8420"):
        code = client.app.state.connector.issue_browser_pairing_token()
        response = _pair(client, code, origin=origin)
        assert response.status_code == 401, origin
        assert response.json()["error_code"] == "invalid_origin"


def test_missing_origin_is_rejected(client):
    code = client.app.state.connector.issue_browser_pairing_token()
    response = client.post("/pair", headers={"Content-Type": "application/json"}, json={"pairing_token": code})
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_origin"


def test_cors_preflight_only_allows_configured_origin(client):
    preflight_headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    allowed = client.options("/company/targets/t1/validate", headers={"Origin": PAGE_ORIGIN, **preflight_headers})
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == PAGE_ORIGIN
    assert allowed.headers.get("access-control-allow-credentials") is None

    denied = client.options("/company/targets/t1/validate", headers={"Origin": EVIL_ORIGIN, **preflight_headers})
    assert denied.headers.get("access-control-allow-origin") is None


def test_served_page_does_not_set_origin_header_itself():
    html = FRONTEND_INDEX.read_text(encoding="utf-8")
    assert re.search(r"['\"]Origin['\"]\s*:", html) is None


# ---------------------------------------------------------------- launcher


def test_launcher_refuses_to_start_without_origin():
    with pytest.raises(SystemExit):
        launcher.build_config([], environ={})


@pytest.mark.parametrize("origin", ["*", "http://validator.example.com", "https://a.example.com/path"])
def test_launcher_refuses_invalid_origin(origin):
    with pytest.raises(SystemExit):
        launcher.build_config(["--allow-origin", origin], environ={})


def test_launcher_reads_env_and_flag_overrides_env():
    env = {ALLOWED_ORIGINS_ENV_VAR: "http://localhost:9000, https://validator.example.com"}
    from_env = launcher.build_config([], environ=env)
    assert from_env.allowed_origins == ("http://localhost:9000", "https://validator.example.com")
    from_flag = launcher.build_config(["--allow-origin", PAGE_ORIGIN], environ=env)
    assert from_flag.allowed_origins == (PAGE_ORIGIN,)


@pytest.mark.parametrize("ttl", ["0", "59", "28801", "-5"])
def test_launcher_bounds_session_ttl(ttl):
    with pytest.raises(SystemExit):
        launcher.build_config(["--allow-origin", PAGE_ORIGIN, "--session-ttl", ttl], environ={})


def test_launcher_runtime_is_loopback_and_least_privilege(tmp_path):
    rt = launcher.build_runtime(launcher.build_config(
        ["--allow-origin", PAGE_ORIGIN, "--registry", str(tmp_path / "targets.json")], environ={}))
    assert rt.bind_host == "127.0.0.1"
    assert rt.port == 8765
    assert rt.allows_database is False
    assert rt.allowed_origins == [PAGE_ORIGIN]


def test_launcher_prints_browser_code_to_terminal_only(runtime, caplog):
    stream = io.StringIO()
    with caplog.at_level(logging.DEBUG):
        code = launcher.print_pairing_code(runtime, stream)
    assert code in stream.getvalue()
    assert code not in caplog.text
    assert runtime.pairing_tokens[code].scope == SESSION_SCOPE_BROWSER


def test_launcher_prompt_loop_issues_fresh_codes(runtime):
    out = io.StringIO()
    launcher._pairing_prompt_loop(runtime, io.StringIO("\n\n"), out)
    browser_codes = [t for t, rec in runtime.pairing_tokens.items() if rec.scope == SESSION_SCOPE_BROWSER]
    assert len(browser_codes) == 2
    assert all(code in out.getvalue() for code in browser_codes)


def test_pairing_secrets_and_credentials_never_logged(client, runtime, caplog):
    _register(runtime, "t1", approved=True)
    with caplog.at_level(logging.DEBUG):
        code = runtime.issue_browser_pairing_token()
        session = _pair(client, code).json()["session_token"]
        client.post("/company/targets/t1/databases", headers=_json_headers(session=session),
                    json={"username": "dba", "password": SECRET_PASSWORD})
        client.post("/company/targets/t1/validate", headers=_json_headers(session=session),
                    json={"sql": "SELECT 1", "username": "dba", "password": SECRET_PASSWORD})
        client.post("/company/targets", headers=_json_headers(session=session), json={})
        _pair(client, code)
    for secret in (code, session, SECRET_PASSWORD, "Bearer"):
        assert secret not in caplog.text


# ---------------------------------------------------------------- security regression


def test_public_api_still_refuses_company_credentials():
    with TestClient(public_app) as public_client:
        response = public_client.post("/api/validate", json={
            "sql": "SELECT 1",
            "host": "company-db.example.com",
            "port": 3306,
            "username": "dba",
            "password": SECRET_PASSWORD,
        })
    body = response.json()
    assert body.get("error_code") == "permission_denied"
    assert "statements" not in body
    assert SECRET_PASSWORD not in response.text


def _function_body(source, name):
    match = re.search(r"(?:async\s+)?function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", source)
    assert match, name
    depth = 0
    for index in range(match.end() - 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[match.end():index]
    raise AssertionError(name)


def test_remote_browser_flow_never_falls_back_to_public_api():
    html = FRONTEND_INDEX.read_text(encoding="utf-8")
    # B2: host/port matching (findApprovedRemoteTarget) was replaced by target_id selection.
    for name in ("connectorRequest", "pairConnector", "unpairConnector", "ensureConnectorSession",
                 "loadApprovedTargets", "selectTarget", "selectedRemoteTarget", "discoverRemote",
                 "discoveryMatches", "remoteLogin", "validateRemote"):
        body = _function_body(html, name)
        assert "/api/" not in body, name
        assert "validateLocal" not in body, name
    # connectorRequest refuses any URL outside the connector and re-throws on failure.
    request_body = _function_body(html, "connectorRequest")
    assert "indexOf(CONNECTOR_URL + '/') !== 0" in request_body
    assert "throw wrapped" in request_body
    # Connector failures surface as errors in validateSql; the catch block never retries.
    validate = _function_body(html, "validateSql")
    catch_block = validate[validate.index("catch (error)"):]
    assert "validateLocal" not in catch_block and "requestJson" not in catch_block
    assert "/api/" not in catch_block


def test_pairing_code_and_session_are_never_persisted_or_put_in_urls():
    html = FRONTEND_INDEX.read_text(encoding="utf-8")
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "indexedDB",
                      "URLSearchParams", "location.search", "location.hash", "window.__"):
        assert forbidden not in html, forbidden
    pair = _function_body(html, "pairConnector")
    assert "pairingCode.value = ''" in pair  # code is cleared from the input immediately
    assert "body: JSON.stringify({ pairing_token: code })" in pair  # sent in the body, not the URL
    assert 'id="pairingCode" type="password"' in html
