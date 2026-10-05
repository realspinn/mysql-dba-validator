"""Company MySQL credentials are per request.

Contract: the browser supplies the user's MySQL username/password to the local
connector in the body of each company operation. The connector uses them for that
operation only. It does not store them (no vault, registry, session state, files,
logs, or responses) and never reads company credentials from anywhere else.

This file replaces the former Windows Credential Manager tests. Tests that guarded
rules unrelated to storage (TLS CA requirement, bounded EXPLAIN/discovery, scope
races, approval states, identifier validation) are kept and adapted.
"""

import hashlib
import importlib
import json
import logging
import socket
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import connector.server as connector_server
from backend.db_evidence import DatabaseEvidence
from backend.main import app as public_app
from backend.metadata import MetadataEvidence
from connector.policy import CompanyTarget, TARGET_MODE_COMPANY
from connector.registry_store import TargetRegistryStore
from connector.server import (
    MAX_COMPANY_SQL_BYTES,
    MAX_COMPANY_VALIDATE_PAYLOAD_BYTES,
    MAX_PAYLOAD_BYTES,
    CompanyTargetDestination,
    ConnectorRuntime,
    create_app,
)

VALID_ORIGIN = "http://127.0.0.1:8420"
USER = "alice_dba"
SECRET = "Per-Request-Secret-Do-Not-Store-7731"
OTHER_USER = "bob_dba"
OTHER_SECRET = "Other-Login-Secret-5519"
ROOT = Path(__file__).resolve().parent.parent


def approved_target(target_id="company-db"):
    return CompanyTarget(
        target_id=target_id,
        display_name="Company database",
        mode=TARGET_MODE_COMPANY,
        host="db.company.example",
        port=3306,
        approval_state="approved",
        dns_identity_snapshot={"host": "db.company.example", "addresses": ["93.184.216.34"]},
        tls_hostname="db.company.example",
    )


def make_runtime(**kwargs):
    runtime = ConnectorRuntime(allowed_origins=[VALID_ORIGIN], **kwargs)
    runtime.company_targets.targets["company-db"] = approved_target()
    return runtime


class FakeCursor:
    def __init__(self, log, rows):
        self.log = log
        self.rows = rows

    def execute(self, statement):
        self.log.append(("execute", statement))

    def fetchmany(self, size):
        self.log.append(("fetchmany", size))
        return self.rows[:size]

    def fetchall(self):
        raise AssertionError("unbounded fetchall must not be used")

    def close(self):
        self.log.append("cursor_closed")


class FakeConnection:
    def __init__(self, log, rows):
        self.log = log
        self.rows = rows

    def cursor(self):
        return FakeCursor(self.log, self.rows)

    def close(self):
        self.log.append("connection_closed")


@pytest.fixture
def fake_mysql(monkeypatch):
    """Replace destination validation + MySQL connect; record the login used."""
    state = {"connects": [], "log": [], "rows": [("appdb",), ("reporting",)]}

    def fake_destination(target_id):
        state["log"].append(("destination", target_id))
        return CompanyTargetDestination(
            hostname="db.company.example", validated_ip="93.184.216.34",
            address_family=socket.AF_INET, port=3306)

    def fake_connect(destination, **kwargs):
        state["connects"].append(kwargs)
        return FakeConnection(state["log"], state["rows"])

    def install(runtime):
        monkeypatch.setattr(runtime, "resolve_and_validate_approved_company_target", fake_destination)
        monkeypatch.setattr(runtime, "connect_company_target_mysql", fake_connect)
        monkeypatch.setattr(connector_server, "collect_statement_evidence",
                            lambda sql, connection=None, **kw: DatabaseEvidence(available=False, error="stubbed"))
        monkeypatch.setattr(connector_server, "collect_metadata",
                            lambda tables, connection=None, database=None, **kw: MetadataEvidence(status="unavailable"))
        return state

    return install


def paired(runtime, scope="browser"):
    client = TestClient(create_app(runtime=runtime))
    code = runtime.issue_pairing_token(scope=scope)
    response = client.post("/pair", headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
                           json={"pairing_token": code})
    assert response.status_code == 200
    return client, response.json()["session_token"]


def hdrs(session):
    return {"Origin": VALID_ORIGIN, "Authorization": f"Bearer {session}", "Content-Type": "application/json"}


def login(user=USER, password=SECRET):
    return {"username": user, "password": password}


# ---------------------------------------------------------------- A / B: per-request login


def test_remote_validate_uses_per_request_login(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    response = client.post("/company/targets/company-db/validate", headers=hdrs(session),
                           json={"sql": "SELECT * FROM users WHERE id = 1", **login()})
    assert response.status_code == 200, response.text
    assert response.json()["statement_count"] == 1
    assert state["connects"][0]["username"] == USER
    assert state["connects"][0]["password"] == SECRET
    assert ("destination", "company-db") in state["log"]
    assert SECRET not in response.text and USER not in response.text


def test_remote_discovery_uses_per_request_login_and_is_bounded(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    state["rows"] = [(f"db_{index}",) for index in range(1001)]
    client, session = paired(runtime)
    response = client.post("/company/targets/company-db/databases", headers=hdrs(session), json=login())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "limited" and body["complete"] is False
    assert len(body["databases"]) == 1000
    assert set(body) == {"status", "target_id", "databases", "complete"}
    assert state["connects"][0]["username"] == USER
    assert state["connects"][0]["password"] == SECRET
    assert ("execute", "SHOW DATABASES") in state["log"]
    assert ("fetchmany", 1001) in state["log"]
    assert state["log"][-2:] == ["cursor_closed", "connection_closed"]
    assert SECRET not in response.text


@pytest.mark.parametrize("path, extra", [
    ("/company/targets/company-db/validate", {"sql": "SELECT 1"}),
    ("/company/targets/company-db/databases", {}),
])
@pytest.mark.parametrize("login_fields", [
    {},
    {"username": USER},
    {"password": SECRET},
    {"username": "", "password": SECRET},
    {"username": "   ", "password": SECRET},
    {"username": "bad\nname", "password": SECRET},
    {"username": USER, "password": ""},
    {"username": USER, "password": "x" * 513},
    {"username": USER, "password": 12345},
])
def test_remote_operations_require_a_valid_login(fake_mysql, path, extra, login_fields):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    response = client.post(path, headers=hdrs(session), json={**extra, **login_fields})
    assert response.status_code == 400
    assert state["connects"] == []
    assert SECRET not in response.text


@pytest.mark.parametrize("injected", [
    {"host": "evil.example"}, {"port": 3307}, {"query": "SELECT * FROM mysql.user"},
    {"credential_id": "company-db"}, {"tls_hostname": "evil.example"}, {"approval_state": "approved"},
])
def test_discovery_accepts_no_destination_or_query_input(fake_mysql, injected):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    response = client.post("/company/targets/company-db/databases", headers=hdrs(session),
                           json={**login(), **injected})
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_database_discovery_request"
    assert state["connects"] == []


def test_operator_connect_and_explain_use_per_request_login(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    state["rows"] = [("appdb",)]
    client, session = paired(runtime, scope="operator")
    discovered = client.post("/company/targets/company-db/databases", headers=hdrs(session), json=login())
    assert discovered.status_code == 200

    connect = client.post("/company/targets/company-db/connect", headers=hdrs(session),
                          json={"target_id": "company-db", "database": "appdb", **login()})
    assert connect.status_code == 200 and connect.json() == {"status": "connected"}

    state["rows"] = [(1, "SIMPLE", "users", None, "ALL", None, None, None, None, 4, 100.0, None)]
    explain = client.post("/company/targets/company-db/explain", headers=hdrs(session),
                          json={"operation": "EXPLAIN_SELECT", "database": "appdb", "table": "users", **login()})
    assert explain.status_code == 200, explain.text
    assert ("execute", "EXPLAIN SELECT * FROM `appdb`.`users` WHERE 1 = 0;") in state["log"]
    assert all(c["username"] == USER and c["password"] == SECRET for c in state["connects"])
    for response in (discovered, connect, explain):
        assert SECRET not in response.text


# ---------------------------------------------------------------- C / D: no vault, no persistence


def test_vault_module_and_methods_are_gone():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("connector.credentials")
    runtime = ConnectorRuntime()
    for name in ("credential_provider", "get_company_credential", "provision_company_credential",
                 "remove_company_credential", "_get_credential_provider"):
        assert not hasattr(runtime, name), name
    source = (ROOT / "connector" / "server.py").read_text(encoding="utf-8")
    for marker in ("keyring", "WinVault", "set_password", "get_password", "CredentialStore",
                   "credential_provider", "mysql-dba-validator.connector"):
        assert marker not in source, marker
    assert "keyring" not in (ROOT / "requirements.txt").read_text(encoding="utf-8")


def test_credentials_never_persisted_anywhere(tmp_path, fake_mysql):
    store = TargetRegistryStore(tmp_path / "registry" / "targets.json")
    runtime = ConnectorRuntime(allowed_origins=[VALID_ORIGIN], registry_store=store)
    runtime.register_company_target(CompanyTarget(
        target_id="company-db", display_name="Company", host="db.company.example", port=3306,
        approval_state="pending"))
    target = runtime.company_targets.get("company-db")
    target.approval_state = "approved"
    target.dns_identity_snapshot = {"host": "db.company.example", "addresses": ["93.184.216.34"]}
    target.tls_hostname = target.host
    store.save(runtime.company_targets.targets, runtime.deleted_target_ids, now=1.0)

    keyring_loaded_before = "keyring" in sys.modules
    fake_mysql(runtime)
    client, session = paired(runtime)
    assert client.post("/company/targets/company-db/databases", headers=hdrs(session), json=login()).status_code == 200
    assert client.post("/company/targets/company-db/validate", headers=hdrs(session),
                       json={"sql": "SELECT 1", "database": "appdb", **login()}).status_code == 200
    runtime.revoke_company_target("company-db")

    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert SECRET.encode() not in data and USER.encode() not in data, path
    assert ("keyring" in sys.modules) == keyring_loaded_before

    # Nothing credential-like is kept in runtime state either.
    state_dump = repr(runtime.sessions) + repr(runtime.company_database_scopes) + repr(runtime.company_targets.targets)
    assert SECRET not in state_dump and USER not in state_dump


# ---------------------------------------------------------------- E: provisioning endpoint removed


def test_browser_credential_provisioning_endpoint_is_gone():
    runtime = make_runtime()
    app = create_app(runtime=runtime)
    paths = {route.path for route in app.routes}
    assert "/company/targets/{target_id}/credentials" not in paths
    assert "/credentials" not in paths

    browser_client, browser_session = paired(runtime)
    browser = browser_client.post("/company/targets/company-db/credentials", headers=hdrs(browser_session), json=login())
    assert browser.status_code == 403
    assert browser.json()["error_code"] == "insufficient_session_scope"

    operator_client, operator_session = paired(runtime, scope="operator")
    operator = operator_client.post("/company/targets/company-db/credentials", headers=hdrs(operator_session), json=login())
    assert operator.status_code == 404
    for response in (browser, operator):
        assert SECRET not in response.text


def test_served_page_sends_login_per_request_to_connector_only():
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    assert "/credentials" not in html
    assert "provisionConnectorCredentials" not in html
    start = html.index("async function validateRemote(")
    remote = html[start:html.index("async function validateLocal(")]
    assert "const login = remoteLogin();" in remote
    assert "username: login.username, password: login.password" in remote
    assert "CONNECTOR_URL + '/company/targets/'" in remote and "'/validate'" in remote
    assert "/api/" not in remote
    for forbidden in ("localStorage", "sessionStorage", "document.cookie", "URLSearchParams"):
        assert forbidden not in html


# ---------------------------------------------------------------- F / G: no leakage


def test_credentials_never_in_responses_logs_or_errors(fake_mysql, monkeypatch, caplog):
    runtime = make_runtime()
    fake_mysql(runtime)

    def failing_connect(destination, **kwargs):
        raise RuntimeError(f"Access denied for user '{kwargs['username']}' using password {kwargs['password']}")

    monkeypatch.setattr(runtime, "connect_company_target_mysql", failing_connect)
    client, session = paired(runtime)
    with caplog.at_level(logging.DEBUG):
        responses = [
            client.post("/company/targets/company-db/databases", headers=hdrs(session), json=login()),
            client.post("/company/targets/company-db/validate", headers=hdrs(session),
                        json={"sql": "SELECT 1", **login()}),
            client.post("/company/targets/company-db/databases", headers=hdrs(session),
                        json={**login(), "unexpected": True}),
            client.post("/company/targets/company-db/validate", headers=hdrs(session),
                        json={"sql": "", **login()}),
        ]
    assert [r.status_code for r in responses] == [500, 500, 400, 400]
    assert responses[0].json()["error_code"] == "database_discovery_failed"
    assert responses[1].json()["error_code"] == "database_validation_failed"
    for response in responses:
        assert SECRET not in response.text and "Access denied" not in response.text
    assert SECRET not in caplog.text


def test_login_model_repr_never_shows_password():
    payload = connector_server.CompanyValidationRequest.model_validate({"sql": "SELECT 1", **login()})
    assert SECRET not in repr(payload) and SECRET not in str(payload)
    assert payload.password.get_secret_value() == SECRET


# ---------------------------------------------------------------- H / I: approval still required


@pytest.mark.parametrize("state_name", ["pending", "revoked", "needs_reapproval"])
def test_non_approved_target_cannot_be_used(fake_mysql, state_name):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    runtime.company_targets.targets["company-db"].approval_state = state_name
    client, session = paired(runtime)
    discovery = client.post("/company/targets/company-db/databases", headers=hdrs(session), json=login())
    validate = client.post("/company/targets/company-db/validate", headers=hdrs(session),
                           json={"sql": "SELECT 1", **login()})
    assert discovery.status_code == 409 and discovery.json()["error_code"] == "company_target_not_approved"
    assert validate.status_code == 409 and validate.json()["error_code"] == "company_target_not_approved"
    assert state["connects"] == []


def test_revoked_target_cannot_be_used_after_revocation(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    assert client.post("/company/targets/company-db/databases", headers=hdrs(session), json=login()).status_code == 200
    assert ("company-db", session) in runtime.company_database_scopes
    runtime.revoke_company_target("company-db")
    assert ("company-db", session) not in runtime.company_database_scopes
    after = client.post("/company/targets/company-db/validate", headers=hdrs(session),
                        json={"sql": "SELECT 1", "database": "appdb", **login()})
    assert after.status_code == 409
    assert len(state["connects"]) == 1


def test_unknown_target_cannot_be_used(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    response = client.post("/company/targets/missing/databases", headers=hdrs(session), json=login())
    assert response.status_code == 404
    assert state["connects"] == []


def test_discovery_rejects_approved_target_without_dns_snapshot_before_connecting(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    runtime.company_targets.targets["company-db"].dns_identity_snapshot = None
    with pytest.raises(ValueError, match="company_target_not_approved"):
        runtime.discover_company_databases("company-db", username=USER, password=SECRET)
    assert state["connects"] == []


# ---------------------------------------------------------------- J: login-bound discovery state


def test_other_login_cannot_reuse_discovery_state(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    assert client.post("/company/targets/company-db/databases", headers=hdrs(session), json=login()).status_code == 200

    other = client.post("/company/targets/company-db/validate", headers=hdrs(session),
                        json={"sql": "SELECT 1", "database": "appdb", **login(OTHER_USER, OTHER_SECRET)})
    assert other.status_code == 409
    assert other.json()["error_code"] == "company_database_scope_unavailable"
    connects_before = len(state["connects"])

    same = client.post("/company/targets/company-db/validate", headers=hdrs(session),
                       json={"sql": "SELECT 1", "database": "appdb", **login()})
    assert same.status_code == 200
    assert len(state["connects"]) == connects_before + 1

    # The binding is a keyed fingerprint, never the username itself.
    scope = runtime.company_database_scopes[("company-db", session)]
    assert scope["login_binding"] != USER and USER not in json.dumps(scope)


def test_other_session_cannot_reuse_discovery_state(fake_mysql):
    runtime = make_runtime()
    fake_mysql(runtime)
    client_a, session_a = paired(runtime)
    client_b, session_b = paired(runtime)
    assert client_a.post("/company/targets/company-db/databases", headers=hdrs(session_a), json=login()).status_code == 200
    reuse = client_b.post("/company/targets/company-db/validate", headers=hdrs(session_b),
                          json={"sql": "SELECT 1", "database": "appdb", **login()})
    assert reuse.status_code == 409


def test_login_binding_is_keyed_per_process():
    first, second = ConnectorRuntime(), ConnectorRuntime()
    assert first.company_login_binding(USER) == first.company_login_binding(USER)
    assert first.company_login_binding(USER) != first.company_login_binding(OTHER_USER)
    assert first.company_login_binding(USER) != second.company_login_binding(USER)
    assert first.company_login_binding(USER) != hashlib.sha256(USER.encode()).hexdigest()


def test_unknown_database_is_rejected_before_connection(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    client.post("/company/targets/company-db/databases", headers=hdrs(session), json=login())
    connects = len(state["connects"])
    response = client.post("/company/targets/company-db/validate", headers=hdrs(session),
                           json={"sql": "SELECT 1", "database": "otherdb", **login()})
    assert response.status_code == 409
    assert response.json()["error_code"] == "database_not_authorized"
    assert len(state["connects"]) == connects


def test_discovery_cannot_publish_scope_after_revocation(monkeypatch):
    runtime = make_runtime()
    started, release = threading.Event(), threading.Event()

    class SlowCursor:
        def execute(self, query):
            started.set()
            assert release.wait(timeout=2)

        def fetchmany(self, size):
            return [("appdb",)]

        def close(self):
            pass

    class SlowConnection:
        def cursor(self):
            return SlowCursor()

        def close(self):
            pass

    monkeypatch.setattr(runtime, "resolve_and_validate_approved_company_target", lambda _: object())
    monkeypatch.setattr(runtime, "connect_company_target_mysql", lambda *a, **k: SlowConnection())
    result = {}
    # A real, live session: revocation (not a missing session) must be what blocks the scope.
    session_a = runtime.create_session_for_pairing_token(
        runtime.issue_browser_pairing_token(), origin=VALID_ORIGIN)

    def discover():
        try:
            result["value"] = runtime.discover_company_databases(
                "company-db", username=USER, password=SECRET, session_token=session_a)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=discover)
    thread.start()
    assert started.wait(timeout=2)
    runtime.revoke_company_target("company-db")
    release.set()
    thread.join(timeout=2)
    assert str(result.get("error")) == "company_database_scope_stale"
    assert ("company-db", session_a) not in runtime.company_database_scopes


# ---------------------------------------------------------------- K: request-size contract


def test_remote_validate_accepts_sql_well_beyond_old_4kb_cap(fake_mysql):
    runtime = make_runtime()
    fake_mysql(runtime)
    client, session = paired(runtime)
    sql = "SELECT * FROM users WHERE note = '" + ("x" * 60_000) + "'"
    response = client.post("/company/targets/company-db/validate", headers=hdrs(session),
                           json={"sql": sql, **login()})
    assert response.status_code == 200, response.text


def test_remote_validate_rejects_sql_over_64kib_bytes(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    sql = "SELECT '" + ("é" * (MAX_COMPANY_SQL_BYTES // 2)) + "'"  # > 64 KiB once UTF-8 encoded
    assert len(sql) < MAX_COMPANY_SQL_BYTES < len(sql.encode("utf-8"))
    body = json.dumps({"sql": sql, **login()}, ensure_ascii=False).encode("utf-8")
    assert len(body) < MAX_COMPANY_VALIDATE_PAYLOAD_BYTES  # rejected by the SQL rule, not the body cap
    response = client.post("/company/targets/company-db/validate", headers=hdrs(session), content=body)
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_company_validation_request"
    assert state["connects"] == []


def test_remote_validate_body_over_limit_is_rejected_cleanly(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    body = json.dumps({"sql": "x" * (MAX_COMPANY_VALIDATE_PAYLOAD_BYTES + 10), **login()})
    response = client.post("/company/targets/company-db/validate", headers=hdrs(session), content=body)
    assert response.status_code == 413
    assert response.json()["error_code"] == "request_too_large"
    assert SECRET not in response.text

    def chunks():  # no Content-Length header: enforced while streaming
        yield body[: len(body) // 2].encode()
        yield body[len(body) // 2:].encode()

    streamed = client.post("/company/targets/company-db/validate", headers=hdrs(session), content=chunks())
    assert streamed.status_code == 413
    assert state["connects"] == []


def test_other_connector_routes_keep_the_small_body_limit(fake_mysql):
    runtime = make_runtime()
    state = fake_mysql(runtime)
    client, session = paired(runtime)
    assert runtime.max_payload_bytes == MAX_PAYLOAD_BYTES == 4096
    response = client.post("/company/targets/company-db/databases", headers=hdrs(session),
                           json={"username": USER, "password": "p" * 5000})
    assert response.status_code == 413
    pair = client.post("/pair", headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
                       json={"pairing_token": "x" * 6000})
    assert pair.status_code == 413
    assert state["connects"] == []


# ---------------------------------------------------------------- L: local flow unchanged


def test_local_flow_is_unchanged():
    with TestClient(public_app) as client:
        local = client.post("/api/validate", json={
            "sql": "SELECT * FROM users;", "host": "127.0.0.1", "port": 3306, "username": "", "password": ""})
        assert local.status_code == 200 and local.json()["statement_count"] == 1
        remote = client.post("/api/validate", json={
            "sql": "SELECT 1", "host": "db.company.example", "port": 3306, "username": USER, "password": SECRET})
        assert remote.json()["error_code"] == "permission_denied"
        assert SECRET not in remote.text
        test = client.post("/api/connections/test", json={
            "host": "db.company.example", "port": 3306, "username": USER, "password": SECRET})
        assert test.json()["error_code"] == "local_only_target_required"
    main_source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert "analysis_session = get_client().session.with_database(req.database)" in main_source


# ---------------------------------------------------------------- kept: TLS / bounded explain / states


def test_company_tls_requires_ca_even_when_target_policy_requests_downgrade():
    runtime = make_runtime()
    runtime.company_targets.get("company-db").tls_required = False
    destination = CompanyTargetDestination(
        hostname="db.company.example", validated_ip="93.184.216.34", address_family=socket.AF_INET, port=3306)
    with pytest.raises(ValueError, match="database_connection_failed"):
        runtime.connect_company_target_mysql(
            destination, username=USER, password=SECRET, tls_hostname="db.company.example", tls_ca_path=None)


def test_company_explain_fetch_is_bounded_and_reports_truncation(monkeypatch):
    runtime = make_runtime()
    log = []
    rows = [(1, "SIMPLE", "users", None, "ALL", None, None, None, None, 1, 100.0, None)] * 200
    sql = runtime.build_bounded_explain_sql(
        target_id="company-db", operation="EXPLAIN_SELECT", scope={"database": "appdb", "table": "users"})
    monkeypatch.setattr(runtime, "resolve_and_validate_approved_company_target", lambda _: object())
    monkeypatch.setattr(runtime, "connect_company_target_mysql", lambda *a, **k: FakeConnection(log, rows))
    result = runtime.execute_bounded_explain_statement(
        target_id="company-db", operation="EXPLAIN_SELECT", scope={"database": "appdb", "table": "users"},
        sql_digest=hashlib.sha256(sql.encode("utf-8")).hexdigest(), username=USER, password=SECRET)
    assert ("fetchmany", 101) in log
    assert len(result["rows"]) == 100 and result["truncated"] is True and result["complete"] is False
    assert log[-2:] == ["cursor_closed", "connection_closed"]


def test_company_explain_rejects_unsafe_database_and_unsupported_operation():
    runtime = make_runtime()
    runtime.company_database_scopes[("company-db", None)] = {
        "databases": ["appdb"], "complete": True, "session_token": None,
        "login_binding": runtime.company_login_binding(USER)}
    with pytest.raises(ValueError, match="invalid_database"):
        runtime.execute_company_explain("company-db", "EXPLAIN_SELECT", "appdb;DROP", "users",
                                        username=USER, password=SECRET)
    with pytest.raises(ValueError, match="unsupported_evidence_operation"):
        runtime.execute_company_explain("company-db", "SHOW", "appdb", "users", username=USER, password=SECRET)


@pytest.mark.parametrize("state_name", ["pending", "revoked"])
def test_internal_company_explain_rejects_nonapproved_states(state_name):
    runtime = make_runtime()
    runtime.company_targets.get("company-db").approval_state = state_name
    with pytest.raises(ValueError, match="company_target_not_approved"):
        runtime.execute_bounded_explain_statement(
            target_id="company-db", operation="EXPLAIN_SELECT", scope={"database": "appdb", "table": "users"},
            sql_digest="0" * 64, username=USER, password=SECRET)


def test_internal_company_explain_rejects_approved_target_without_dns_snapshot():
    runtime = make_runtime()
    runtime.company_targets.get("company-db").dns_identity_snapshot = None
    with pytest.raises(ValueError, match="company_target_not_approved"):
        runtime.execute_bounded_explain_statement(
            target_id="company-db", operation="EXPLAIN_SELECT", scope={"database": "appdb", "table": "users"},
            sql_digest="0" * 64, username=USER, password=SECRET)


def test_deleted_target_id_cannot_be_reused():
    runtime = make_runtime()
    runtime.delete_company_target("company-db")
    with pytest.raises(ValueError, match="deleted_company_target_id_reserved"):
        runtime.register_company_target(approved_target("company-db"))
