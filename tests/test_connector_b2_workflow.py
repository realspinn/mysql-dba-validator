"""V2 B2: launcher configuration, operator target lifecycle, discovery state and
remote validation through approved targets.

Verification level: unit / FastAPI TestClient only. DNS, sockets and pymysql are
faked; no real MySQL, VPN, TLS handshake or browser is involved here.
"""

import datetime
import io
import json
import logging
import socket
import threading
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

import connector.server as connector_server
from backend.db_evidence import DatabaseEvidence
from backend.metadata import MetadataEvidence
from connector import launcher
from connector.policy import CompanyTarget
from connector.registry_store import REGISTRY_PATH_ENV_VAR
from connector.server import CompanyTargetDestination, ConnectorRuntime, create_app

ORIGIN = "http://127.0.0.1:8420"
HOST = "db.company.example"
PINNED_IP = "203.0.113.10"
USER = "alice_dba"
SECRET = "B2-Per-Request-Secret-4471"
OTHER_USER = "bob_dba"
OTHER_SECRET = "B2-Other-Secret-9902"
ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- helpers


@pytest.fixture
def dns(monkeypatch):
    answers = {HOST: [PINNED_IP], "db2.company.example": ["203.0.113.20"]}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port or 0)) for ip in answers[host]]

    monkeypatch.setattr("connector.server.socket.getaddrinfo", fake_getaddrinfo)
    return answers


@pytest.fixture(autouse=True)
def no_ambient_tls_ca(monkeypatch):
    monkeypatch.delenv(launcher.TLS_CA_ENV_VAR, raising=False)
    monkeypatch.delenv(REGISTRY_PATH_ENV_VAR, raising=False)


def patch_connector_socket(monkeypatch, factory):
    """Intercept socket.socket() only inside connector.server (asyncio needs the real one)."""
    class SocketModuleProxy:
        def __getattr__(self, name):
            return getattr(socket, name)

    proxy = SocketModuleProxy()
    proxy.socket = factory
    monkeypatch.setattr(connector_server, "socket", proxy)


def _write_cert(path, *, ca):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "B2 Test CA" if ca else HOST)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


@pytest.fixture
def ca_file(tmp_path):
    return _write_cert(tmp_path / "company-ca.pem", ca=True)


def config_for(tmp_path, *extra, environ=None):
    return launcher.build_config(
        ["--allow-origin", ORIGIN, "--registry", str(tmp_path / "targets.json"), *extra],
        environ=environ if environ is not None else {})


def pair(runtime, scope="browser"):
    client = TestClient(create_app(runtime=runtime))
    code = runtime.issue_pairing_token(scope=scope)
    response = client.post("/pair", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                           json={"pairing_token": code})
    assert response.status_code == 200
    return client, response.json()["session_token"]


def hdrs(session):
    return {"Origin": ORIGIN, "Content-Type": "application/json", "Authorization": f"Bearer {session}"}


def login(user=USER, secret=SECRET):
    return {"username": user, "password": secret}


def console(runtime, *lines):
    return [launcher.run_operator_command(runtime, line) for line in lines]


class FakeCursor:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def execute(self, statement):
        self.log.append(("execute", statement))

    def fetchmany(self, size):
        self.log.append(("fetchmany", size))
        return self.rows[:size]

    def close(self):
        pass


class FakeConnection:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def cursor(self):
        return FakeCursor(self.log, self.rows)

    def close(self):
        pass


@pytest.fixture
def stub_evidence(monkeypatch):
    monkeypatch.setattr(connector_server, "collect_statement_evidence",
                        lambda sql, connection=None, **kw: DatabaseEvidence(available=False, error="stubbed"))
    monkeypatch.setattr(connector_server, "collect_metadata",
                        lambda tables, connection=None, database=None, **kw: MetadataEvidence(status="unavailable"))


@pytest.fixture
def fake_mysql(monkeypatch, stub_evidence):
    """Fake destination + MySQL: rows per username so logins see different databases."""
    state = {"connects": [], "log": [], "rows": {USER: [("appdb",), ("reporting",)],
                                                  OTHER_USER: [("otherdb",)]}}

    def install(runtime):
        def fake_destination(target_id):
            target = runtime.company_targets.get(target_id)
            return CompanyTargetDestination(hostname=target.host, validated_ip=PINNED_IP,
                                            address_family=socket.AF_INET, port=target.port)

        def fake_connect(destination, **kwargs):
            state["connects"].append(kwargs)
            return FakeConnection(state["log"], state["rows"].get(kwargs["username"], []))

        monkeypatch.setattr(runtime, "resolve_and_validate_approved_company_target", fake_destination)
        monkeypatch.setattr(runtime, "connect_company_target_mysql", fake_connect)
        return state

    return install


def approved_runtime(tmp_path, dns, *target_ids):
    runtime = launcher.build_runtime(config_for(tmp_path))
    hosts = {"t1": HOST, "t2": "db2.company.example"}
    for target_id in target_ids or ("t1",):
        console(runtime, f"register {target_id} {hosts[target_id]} 3306 Company {target_id}",
                f"approve {target_id}")
        assert runtime.is_company_target_approved(target_id)
    return runtime


# ---------------------------------------------------------------- B2.1 launcher: registry path


def test_registry_flag_overrides_environment(tmp_path):
    env = {REGISTRY_PATH_ENV_VAR: str(tmp_path / "env.json"), "LOCALAPPDATA": str(tmp_path / "appdata")}
    config = launcher.build_config(
        ["--allow-origin", ORIGIN, "--registry", str(tmp_path / "flag.json")], environ=env)
    assert config.registry_path == (tmp_path / "flag.json").absolute()


def test_registry_environment_overrides_default(tmp_path):
    env = {REGISTRY_PATH_ENV_VAR: str(tmp_path / "env.json"), "LOCALAPPDATA": str(tmp_path / "appdata")}
    config = launcher.build_config(["--allow-origin", ORIGIN], environ=env)
    assert config.registry_path == (tmp_path / "env.json").absolute()


def test_default_registry_is_per_user_application_data_not_project_relative(tmp_path, monkeypatch):
    appdata = tmp_path / "appdata"
    config = launcher.build_config(["--allow-origin", ORIGIN], environ={"LOCALAPPDATA": str(appdata)})
    assert config.registry_path == appdata / "MySQLDBAValidator" / "connector-targets.json"

    # Without LOCALAPPDATA/XDG_STATE_HOME it falls back to the user's home, never the
    # project directory or the current working directory.
    monkeypatch.chdir(ROOT)
    fallback = launcher.build_config(["--allow-origin", ORIGIN], environ={}).registry_path
    assert fallback.is_absolute()
    assert Path.home() in fallback.parents
    assert ROOT not in fallback.parents


def test_no_developer_specific_paths_in_launcher_or_registry_code():
    for relative in ("connector/launcher.py", "connector/registry_store.py"):
        source = (ROOT / relative).read_text(encoding="utf-8")
        for marker in ("C:\\", "C:/", "\\Users\\", "/home/", Path.home().name):
            assert marker not in source, (relative, marker)


def test_registry_directory_is_rejected(tmp_path):
    with pytest.raises(SystemExit):
        launcher.build_config(["--allow-origin", ORIGIN, "--registry", str(tmp_path)], environ={})


def test_launcher_runtime_persists_targets_across_restart(tmp_path, dns):
    runtime = launcher.build_runtime(config_for(tmp_path))
    assert runtime.registry_store is not None
    assert console(runtime, f"register t1 {HOST} 3306 Company DB")[0].startswith("registered t1")
    console(runtime, "approve t1")

    restarted = launcher.build_runtime(config_for(tmp_path))
    assert restarted.is_company_target_approved("t1")
    assert restarted.company_targets.get("t1").dns_identity_snapshot["addresses"] == [PINNED_IP]


def test_corrupt_registry_stops_launcher_and_is_not_modified(tmp_path, capsys):
    path = tmp_path / "targets.json"
    path.write_text("{not json", encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(connector_server.RegistryError):
        launcher.build_runtime(config_for(tmp_path))
    assert launcher.main(["--allow-origin", ORIGIN, "--registry", str(path)]) == 2
    err = capsys.readouterr().err
    assert "registry_not_valid_json" in err and "Refusing to start" in err
    assert path.read_bytes() == original


def test_startup_banner_shows_registry_and_tls_status(tmp_path, monkeypatch, capsys):
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    path = tmp_path / "targets.json"
    assert launcher.main(["--allow-origin", ORIGIN, "--registry", str(path)]) == 0
    out = capsys.readouterr().out
    assert f"registry     : {path.absolute()}" in out
    assert "company TLS CA: NOT CONFIGURED" in out


# ---------------------------------------------------------------- B2.1 launcher: TLS CA


def test_tls_ca_flag_is_validated_and_wired(tmp_path, ca_file):
    config = config_for(tmp_path, "--tls-ca", str(ca_file))
    assert config.tls_ca_path == str(ca_file.absolute())
    runtime = launcher.build_runtime(config)
    assert runtime.company_tls_ca_path == str(ca_file.absolute())


def test_tls_ca_environment_variable_and_flag_precedence(tmp_path, ca_file):
    other = _write_cert(tmp_path / "other-ca.pem", ca=True)
    from_env = config_for(tmp_path, environ={launcher.TLS_CA_ENV_VAR: str(ca_file)})
    assert from_env.tls_ca_path == str(ca_file.absolute())
    from_flag = config_for(tmp_path, "--tls-ca", str(other), environ={launcher.TLS_CA_ENV_VAR: str(ca_file)})
    assert from_flag.tls_ca_path == str(other.absolute())


@pytest.mark.parametrize("kind", ["missing", "garbage", "not_a_ca", "directory"])
def test_invalid_tls_ca_fails_closed_at_startup(tmp_path, kind):
    if kind == "missing":
        value = tmp_path / "nope.pem"
    elif kind == "garbage":
        value = tmp_path / "garbage.pem"
        value.write_text("-----BEGIN CERTIFICATE-----\nnot base64\n-----END CERTIFICATE-----\n")
    elif kind == "not_a_ca":
        value = _write_cert(tmp_path / "leaf.pem", ca=False)
    else:
        value = tmp_path
    with pytest.raises(SystemExit):
        config_for(tmp_path, "--tls-ca", str(value))


def test_without_tls_ca_connector_starts_but_company_operations_fail_closed(tmp_path, dns, monkeypatch):
    runtime = approved_runtime(tmp_path, dns)
    assert runtime.company_tls_ca_path is None
    sockets = []
    patch_connector_socket(monkeypatch, lambda *a, **k: sockets.append(a))
    client, session = pair(runtime)
    discovery = client.post("/company/targets/t1/databases", headers=hdrs(session), json=login())
    assert discovery.status_code == 500 and discovery.json()["error_code"] == "database_discovery_failed"
    validate = client.post("/company/targets/t1/validate", headers=hdrs(session),
                           json={"sql": "SELECT 1", **login()})
    assert validate.status_code == 400 and validate.json()["error_code"] == "database_connection_failed"
    assert sockets == []  # no TCP connection was even attempted


# ---------------------------------------------------------------- B2.2 operator console


def test_operator_console_full_lifecycle_persists(tmp_path, dns):
    runtime = launcher.build_runtime(config_for(tmp_path))
    out = console(runtime, f"register t1 {HOST} 3306 \"Company primary\"")
    assert out == [f"registered t1: {HOST}:3306 (pending; run 'approve t1')"]
    assert runtime.company_targets.get("t1").approval_state == "pending"
    assert not runtime.is_company_target_approved("t1")

    assert console(runtime, "approve t1") == [f"approved t1: {HOST}:3306 pinned to {PINNED_IP}"]
    assert launcher.build_runtime(config_for(tmp_path)).is_company_target_approved("t1")

    listing = console(runtime, "targets")[0]
    assert "t1" in listing and "approved" in listing and "Company primary" in listing

    assert console(runtime, "revoke t1") == ["revoked t1"]
    reloaded = launcher.build_runtime(config_for(tmp_path))
    assert reloaded.company_targets.get("t1").approval_state == "revoked"
    assert not reloaded.is_company_target_approved("t1")

    # A revoked target can be approved again explicitly.
    assert console(runtime, "approve t1")[0].startswith("approved t1")

    assert console(runtime, "delete t1") == ["deleted t1"]
    reloaded = launcher.build_runtime(config_for(tmp_path))
    assert reloaded.company_targets.targets == {}
    assert console(reloaded, f"register t1 {HOST} 3306 Again") == [
        "error: register t1: deleted_company_target_id_reserved"]


def test_operator_reapprove_is_explicit(tmp_path, dns):
    runtime = approved_runtime(tmp_path, dns)
    runtime.company_targets.get("t1").approval_state = "needs_reapproval"
    assert console(runtime, "approve t1") == ["error: approve t1: company_target_needs_reapproval"]
    assert console(runtime, "reapprove t1") == [f"re-approved t1: {HOST}:3306 pinned to {PINNED_IP}"]
    assert launcher.build_runtime(config_for(tmp_path)).is_company_target_approved("t1")


@pytest.mark.parametrize("line, expected", [
    ("register t1 127.0.0.1 3306 Loop", "error: register t1: local_target_not_allowed_in_company_mode"),
    ("register t1 10.0.0.5 3306 Raw", "error: register t1: invalid_company_target_host"),
    (f"register t1 {HOST} port Name", "error: register t1: invalid_company_target_port"),
    (f"register t1 {HOST} 70000 Name", "error: register t1: invalid_company_target_port"),
    (f"register bad/id {HOST} 3306 Name", "error: register bad/id: invalid_company_target_id"),
    ("register t1 host", "usage: register <id> <host> <port> <name...>"),
    ("approve", "usage: approve <id>"),
    ("approve missing", "error: approve missing: unknown_company_target"),
    ("revoke missing", "error: revoke missing: unknown_company_target"),
    ("delete missing", "error: delete missing: unknown_company_target"),
    ("frobnicate", "error: unknown command 'frobnicate'; type 'help'"),
    ('register "unterminated', "error: could not parse command (check quotes); type 'help'"),
])
def test_operator_console_rejects_invalid_commands(tmp_path, line, expected):
    runtime = launcher.build_runtime(config_for(tmp_path))
    assert console(runtime, line) == [expected]
    assert runtime.company_targets.targets == {}


def test_operator_console_duplicate_and_unresolvable_host(tmp_path, dns):
    runtime = launcher.build_runtime(config_for(tmp_path))
    console(runtime, f"register t1 {HOST} 3306 One")
    assert console(runtime, f"register t1 {HOST} 3306 Two") == ["error: register t1: duplicate_company_target_id"]
    console(runtime, "register t9 unknown.company.example 3306 Nine")
    out = console(runtime, "approve t9")[0]
    assert out.startswith("error: approve t9: ")
    assert not runtime.is_company_target_approved("t9")


def test_operator_console_loop_handles_commands_and_pairing(tmp_path, dns):
    runtime = launcher.build_runtime(config_for(tmp_path))
    out = io.StringIO()
    launcher._pairing_prompt_loop(runtime, io.StringIO(f"help\nregister t1 {HOST} 3306 DB\napprove t1\n\n"), out)
    text = out.getvalue()
    assert "Operator commands" in text and "registered t1" in text and "approved t1" in text
    assert "Pairing code" in text
    assert runtime.is_company_target_approved("t1")


def test_operator_help_never_asks_for_credentials():
    lowered = launcher.OPERATOR_HELP.lower()
    assert "never entered here" in lowered
    for command_line in lowered.splitlines()[1:-1]:
        assert "<password" not in command_line and "<user" not in command_line


def test_browser_session_cannot_perform_operator_operations(tmp_path, dns):
    runtime = approved_runtime(tmp_path, dns)
    registry = tmp_path / "targets.json"
    before = registry.read_bytes()
    client, session = pair(runtime, scope="browser")
    attempts = [
        ("POST", "/company/targets", {"target_id": "evil", "display_name": "e", "host": "evil.example", "port": 3306}),
        ("POST", "/company/targets/t1/approve", None),
        ("POST", "/company/targets/t1/revoke", None),
        ("DELETE", "/company/targets/t1", None),
        ("POST", "/company/targets/t1/connect", {"target_id": "t1", **login()}),
        ("POST", "/company/targets/t1/explain", {"operation": "EXPLAIN_SELECT", "database": "a", "table": "b", **login()}),
    ]
    for method, path, body in attempts:
        response = client.request(method, path, headers=hdrs(session), json=body)
        assert response.status_code == 403, (method, path)
        assert response.json()["error_code"] == "insufficient_session_scope"
    assert registry.read_bytes() == before
    assert runtime.is_company_target_approved("t1")


def test_retired_evidence_route_no_longer_exists(tmp_path, dns):
    runtime = approved_runtime(tmp_path, dns)
    client, session = pair(runtime, scope="operator")
    response = client.post("/company/targets/t1/evidence", headers=hdrs(session), json={})
    assert response.status_code == 404
    assert "/company/targets/{target_id}/evidence" not in {getattr(r, "path", None) for r in client.app.routes}
    for retired in ("evidence_replay_cache", "evidence_authorization_public_key", "validate_evidence_authorization"):
        assert not hasattr(runtime, retired), retired


def test_operator_http_delete_reports_deleted(tmp_path, dns):
    runtime = approved_runtime(tmp_path, dns)
    client, session = pair(runtime, scope="operator")
    response = client.delete("/company/targets/t1", headers=hdrs(session))
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "target_id": "t1"}
    assert launcher.build_runtime(config_for(tmp_path)).company_targets.targets == {}


def test_browser_listing_shows_only_approved_targets(tmp_path, dns):
    runtime = approved_runtime(tmp_path, dns)
    console(runtime, "register t2 db2.company.example 3306 Pending")
    console(runtime, "register t3 db2.company.example 3307 Revoked", "approve t3", "revoke t3")
    client, session = pair(runtime)
    targets = client.get("/company/targets", headers=hdrs(session)).json()["targets"]
    assert [target["target_id"] for target in targets] == ["t1"]
    assert all(target["approval_state"] == "approved" for target in targets)
    assert not any(key in json.dumps(targets).lower() for key in ("password", "username"))


# ---------------------------------------------------------------- B3.1 finding: readable errors in a real browser


def test_enforcement_errors_carry_cors_headers_for_allowed_origin_only(tmp_path):
    """Found with real Chrome: middleware-generated 403/413/404 lacked CORS headers, so
    the page saw an opaque network failure instead of the error code."""
    runtime = launcher.build_runtime(config_for(tmp_path))
    client, session = pair(runtime)
    cases = [
        client.post("/company/targets/t1/approve", headers=hdrs(session)),
        client.post("/company/targets/t1/databases", headers={**hdrs(session), "Content-Length": "999999"}, content=b"{}"),
        client.get("/not-a-route", headers=hdrs(session)),
    ]
    assert [r.status_code for r in cases] == [403, 413, 404]
    assert [r.json()["error_code"] for r in cases] == ["insufficient_session_scope", "request_too_large", "not_found"]
    assert all(r.headers.get("access-control-allow-origin") == ORIGIN for r in cases)

    # A disallowed origin still gets no CORS grant and is still rejected.
    evil = client.post("/company/targets/t1/databases",
                       headers={**hdrs(session), "Origin": "https://evil.example"}, json=login())
    assert evil.status_code == 401 and "access-control-allow-origin" not in evil.headers
    # A preflight is answered by CORS but grants nothing beyond the configured methods,
    # and the real request is still enforced (browser scope -> 403 above).
    preflight = client.options("/company/targets/t1/approve", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "DELETE"})
    assert "DELETE" not in preflight.headers.get("access-control-allow-methods", "")


# ---------------------------------------------------------------- B2.4/B2.5 discovery state


def test_two_sessions_on_one_target_do_not_overwrite_discovery_state(tmp_path, dns, fake_mysql):
    runtime = approved_runtime(tmp_path, dns)
    fake_mysql(runtime)
    client_a, session_a = pair(runtime)
    client_b, session_b = pair(runtime)
    found_a = client_a.post("/company/targets/t1/databases", headers=hdrs(session_a), json=login())
    found_b = client_b.post("/company/targets/t1/databases", headers=hdrs(session_b),
                            json=login(OTHER_USER, OTHER_SECRET))
    assert found_a.json()["databases"] == ["appdb", "reporting"]
    assert found_b.json()["databases"] == ["otherdb"]

    ok_a = client_a.post("/company/targets/t1/validate", headers=hdrs(session_a),
                         json={"sql": "SELECT 1", "database": "appdb", **login()})
    ok_b = client_b.post("/company/targets/t1/validate", headers=hdrs(session_b),
                         json={"sql": "SELECT 1", "database": "otherdb", **login(OTHER_USER, OTHER_SECRET)})
    assert ok_a.status_code == 200 and ok_b.status_code == 200

    # Neither session can use the other's discovered database.
    cross = client_a.post("/company/targets/t1/validate", headers=hdrs(session_a),
                          json={"sql": "SELECT 1", "database": "otherdb", **login()})
    assert cross.status_code == 409 and cross.json()["error_code"] == "database_not_authorized"


def test_unpairing_one_session_keeps_other_sessions_discovery_state(tmp_path, dns, fake_mysql):
    runtime = approved_runtime(tmp_path, dns)
    fake_mysql(runtime)
    client_a, session_a = pair(runtime)
    client_b, session_b = pair(runtime)
    for client, session in ((client_a, session_a), (client_b, session_b)):
        assert client.post("/company/targets/t1/databases", headers=hdrs(session), json=login()).status_code == 200

    assert client_b.post("/unpair", headers=hdrs(session_b)).status_code == 200
    assert ("t1", session_b) not in runtime.company_database_scopes
    assert ("t1", session_a) in runtime.company_database_scopes
    still = client_a.post("/company/targets/t1/validate", headers=hdrs(session_a),
                          json={"sql": "SELECT 1", "database": "appdb", **login()})
    assert still.status_code == 200


def test_session_expiry_clears_discovery_state(tmp_path, dns, fake_mysql):
    runtime = approved_runtime(tmp_path, dns)
    fake_mysql(runtime)
    client, session = pair(runtime)
    client.post("/company/targets/t1/databases", headers=hdrs(session), json=login())
    assert ("t1", session) in runtime.company_database_scopes
    runtime.sessions[session].expires_at = 0
    expired = client.post("/company/targets/t1/validate", headers=hdrs(session),
                          json={"sql": "SELECT 1", "database": "appdb", **login()})
    assert expired.status_code == 401 and expired.json()["error_code"] == "session_expired"
    assert ("t1", session) not in runtime.company_database_scopes


def test_discovery_cannot_commit_after_its_session_is_unpaired(tmp_path, dns, monkeypatch):
    runtime = approved_runtime(tmp_path, dns)
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
    session = runtime.create_session_for_pairing_token(runtime.issue_browser_pairing_token(), origin=ORIGIN)
    result = {}

    def discover():
        try:
            runtime.discover_company_databases("t1", username=USER, password=SECRET, session_token=session)
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=discover)
    thread.start()
    assert started.wait(timeout=2)
    runtime.unpair_session(session)
    release.set()
    thread.join(timeout=2)
    assert str(result.get("error")) == "company_database_scope_stale"
    assert ("t1", session) not in runtime.company_database_scopes


@pytest.mark.parametrize("operator_action", ["revoke t1", "approve t1", "reapprove t1", "delete t1"])
def test_target_lifecycle_change_invalidates_discovery_for_all_sessions(tmp_path, dns, fake_mysql, operator_action):
    runtime = approved_runtime(tmp_path, dns)
    state = fake_mysql(runtime)
    client_a, session_a = pair(runtime)
    client_b, session_b = pair(runtime)
    for client, session in ((client_a, session_a), (client_b, session_b)):
        client.post("/company/targets/t1/databases", headers=hdrs(session), json=login())

    console(runtime, operator_action)
    assert not [key for key in runtime.company_database_scopes if key[0] == "t1"]
    connects = len(state["connects"])
    response = client_a.post("/company/targets/t1/validate", headers=hdrs(session_a),
                             json={"sql": "SELECT 1", "database": "appdb", **login()})
    assert response.status_code in {404, 409}
    assert len(state["connects"]) == connects


def test_database_selection_is_bound_to_the_target(tmp_path, dns, fake_mysql):
    runtime = approved_runtime(tmp_path, dns, "t1", "t2")
    state = fake_mysql(runtime)
    client, session = pair(runtime)
    client.post("/company/targets/t1/databases", headers=hdrs(session), json=login())
    connects = len(state["connects"])
    other_target = client.post("/company/targets/t2/validate", headers=hdrs(session),
                               json={"sql": "SELECT 1", "database": "appdb", **login()})
    assert other_target.status_code == 409
    assert other_target.json()["error_code"] == "company_database_scope_unavailable"
    assert len(state["connects"]) == connects


def test_database_selection_is_bound_to_the_login(tmp_path, dns, fake_mysql):
    runtime = approved_runtime(tmp_path, dns)
    fake_mysql(runtime)
    client, session = pair(runtime)
    client.post("/company/targets/t1/databases", headers=hdrs(session), json=login())
    other = client.post("/company/targets/t1/validate", headers=hdrs(session),
                        json={"sql": "SELECT 1", "database": "appdb", **login(OTHER_USER, OTHER_SECRET)})
    assert other.status_code == 409 and other.json()["error_code"] == "company_database_scope_unavailable"


def test_database_never_accepted_without_prior_discovery(tmp_path, dns, fake_mysql):
    runtime = approved_runtime(tmp_path, dns)
    state = fake_mysql(runtime)
    client, session = pair(runtime)
    response = client.post("/company/targets/t1/validate", headers=hdrs(session),
                           json={"sql": "SELECT 1", "database": "appdb", **login()})
    assert response.status_code == 409
    assert state["connects"] == []


@pytest.mark.parametrize("route, body", [
    ("databases", {"host": "evil.example", **login()}),
    ("databases", {"port": 3307, **login()}),
    ("databases", {"query": "SELECT * FROM mysql.user", **login()}),
    ("validate", {"sql": "SELECT 1", "host": "evil.example", **login()}),
    ("validate", {"sql": "SELECT 1", "port": 3307, **login()}),
    ("validate", {"sql": "SELECT 1", "target_id": "t2", **login()}),
])
def test_browser_cannot_supply_destination_fields(tmp_path, dns, fake_mysql, route, body):
    runtime = approved_runtime(tmp_path, dns)
    state = fake_mysql(runtime)
    client, session = pair(runtime)
    response = client.post(f"/company/targets/t1/{route}", headers=hdrs(session), json=body)
    assert response.status_code == 400
    assert state["connects"] == []


def test_discovery_is_fixed_and_bounded(tmp_path, dns, fake_mysql):
    runtime = approved_runtime(tmp_path, dns)
    state = fake_mysql(runtime)
    state["rows"][USER] = [(f"db{i}",) for i in range(connector_server.MAX_DISCOVERED_DATABASES + 5)]
    client, session = pair(runtime)
    response = client.post("/company/targets/t1/databases", headers=hdrs(session), json=login())
    data = response.json()
    assert data["status"] == "limited" and data["complete"] is False
    assert len(data["databases"]) == connector_server.MAX_DISCOVERED_DATABASES
    assert ("execute", "SHOW DATABASES") in state["log"]
    assert ("fetchmany", connector_server.MAX_DISCOVERED_DATABASES + 1) in state["log"]
    assert state["connects"][0]["username"] == USER and state["connects"][0]["password"] == SECRET


def test_credentials_never_persisted_logged_or_returned(tmp_path, dns, fake_mysql, monkeypatch, caplog):
    runtime = approved_runtime(tmp_path, dns)
    fake_mysql(runtime)
    client, session = pair(runtime)
    outputs = []
    with caplog.at_level(logging.DEBUG):
        responses = [
            client.post("/company/targets/t1/databases", headers=hdrs(session), json=login()),
            client.post("/company/targets/t1/validate", headers=hdrs(session),
                        json={"sql": "SELECT 1", "database": "appdb", **login()}),
        ]
        outputs += console(runtime, "targets", "reapprove t1", "revoke t1", "approve t1",
                           "register t2 db2.company.example 3306 Second", "approve t2", "delete t2")

        def failing_connect(destination, **kwargs):
            raise RuntimeError(f"Access denied for '{kwargs['username']}' password {kwargs['password']}")

        monkeypatch.setattr(runtime, "connect_company_target_mysql", failing_connect)
        responses += [
            client.post("/company/targets/t1/databases", headers=hdrs(session), json=login()),
            client.post("/company/targets/t1/validate", headers=hdrs(session),
                        json={"sql": "SELECT 1", **login()}),
        ]

    registry_text = (tmp_path / "targets.json").read_text(encoding="utf-8")
    state_dump = repr(runtime.sessions) + repr(runtime.company_database_scopes) + repr(runtime.company_targets.targets)
    for secret in (SECRET, USER):
        assert secret not in registry_text
        assert secret not in "\n".join(outputs)
        assert secret not in caplog.text
        assert secret not in state_dump
        for response in responses:
            assert secret not in response.text
    assert not {"username", "password"} & {key for target in json.loads(registry_text)["targets"] for key in target}


# ---------------------------------------------------------------- B2.6 remote validation, real destination checks


class FakeSocket:
    def __init__(self, family, kind, record):
        self.record = record
        record.append({"family": family})

    def settimeout(self, value):
        pass

    def connect(self, address):
        self.record[-1]["address"] = address

    def close(self):
        pass


@pytest.fixture
def fake_network(monkeypatch, stub_evidence):
    """Real destination validation (DNS faked); fake TCP socket and pymysql."""
    record = {"sockets": [], "pymysql": []}

    class FakePyMySQLConnection:
        def connect(self, sock=None):
            record["pymysql"][-1]["sock"] = sock

        def close(self):
            pass

    def fake_pymysql_connect(**kwargs):
        record["pymysql"].append(kwargs)
        return FakePyMySQLConnection()

    patch_connector_socket(monkeypatch, lambda family, kind: FakeSocket(family, kind, record["sockets"]))
    monkeypatch.setattr(connector_server.pymysql, "connect", fake_pymysql_connect)
    return record


def test_approved_target_validation_uses_registry_destination_and_tls(tmp_path, dns, ca_file, fake_network):
    runtime = launcher.build_runtime(config_for(tmp_path, "--tls-ca", str(ca_file)))
    console(runtime, f"register t1 {HOST} 3306 Company", "approve t1")
    client, session = pair(runtime)
    response = client.post("/company/targets/t1/validate", headers=hdrs(session),
                           json={"sql": "UPDATE users SET a = 1", **login()})
    assert response.status_code == 200, response.text
    assert response.json()["statement_count"] == 1
    assert fake_network["sockets"] == [{"family": socket.AF_INET, "address": (PINNED_IP, 3306)}]
    used = fake_network["pymysql"][0]
    assert used["host"] == HOST and used["port"] == 3306
    assert used["user"] == USER and used["password"] == SECRET
    assert used["ssl"]["ca"] == str(ca_file.absolute())
    assert used["ssl"]["check_hostname"] is True


def test_dns_change_after_approval_fails_closed(tmp_path, dns, ca_file, fake_network):
    runtime = launcher.build_runtime(config_for(tmp_path, "--tls-ca", str(ca_file)))
    console(runtime, f"register t1 {HOST} 3306 Company", "approve t1")
    dns[HOST] = ["203.0.113.99"]
    client, session = pair(runtime)
    response = client.post("/company/targets/t1/validate", headers=hdrs(session),
                           json={"sql": "SELECT 1", **login()})
    assert response.status_code == 400 and response.json()["error_code"] == "dns_identity_mismatch"
    assert fake_network["sockets"] == []


@pytest.mark.parametrize("setup, expected_status, expected_code", [
    (["register t1 db.company.example 3306 C"], 409, "company_target_not_approved"),
    (["register t1 db.company.example 3306 C", "approve t1", "revoke t1"], 409, "company_target_not_approved"),
    (["register t1 db.company.example 3306 C", "approve t1", "delete t1"], 404, "unknown_company_target"),
    ([], 404, "unknown_company_target"),
])
def test_unapproved_targets_fail_closed(tmp_path, dns, ca_file, fake_network, setup, expected_status, expected_code):
    runtime = launcher.build_runtime(config_for(tmp_path, "--tls-ca", str(ca_file)))
    console(runtime, *setup)
    client, session = pair(runtime)
    for route, body in (("validate", {"sql": "SELECT 1", **login()}), ("databases", login())):
        response = client.post(f"/company/targets/t1/{route}", headers=hdrs(session), json=body)
        assert response.status_code == expected_status, route
        assert response.json()["error_code"] == expected_code
    assert fake_network["sockets"] == [] and fake_network["pymysql"] == []
