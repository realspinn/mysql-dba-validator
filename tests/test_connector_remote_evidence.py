"""B3 correction: remote /validate uses the same evidence pipeline as local.

The connector's MySQL connection returns dictionary rows (SSDictCursor), the
contract the shared backend collectors were written for. Remote validation then
scores through the same build_statement_result() as local /api/validate, so the
same EXPLAIN/metadata rows must yield identical responses.

Verification level: unit / TestClient. The MySQL server is a fake cursor that
returns dictionary rows; no real MySQL, network or TLS is involved.
"""

import logging
import socket

import pytest
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient

import backend.main as backend_main
import backend.metadata as backend_metadata
import connector.server as connector_server
from backend.main import validate as local_validate
from connector.policy import CompanyTarget
from connector.server import CompanyTargetDestination, ConnectorRuntime, create_app
from tests.local_evidence import local_request, use_fake_evidence_connection

ORIGIN = "http://127.0.0.1:8420"
HOST = "db.company.example"
USER = "remote_dba"
SECRET = "Remote-Evidence-Secret-3318"
DB = "appdb"


# ---------------------------------------------------------------- fake MySQL (dictionary rows)


class FakeServer:
    """Answers the fixed queries the connector and shared collectors issue."""

    def __init__(self, *, explain=None, table_rows=100, indexes=None, metadata_error=None, tuple_rows=False):
        self.explain = explain if explain is not None else []
        self.table_rows = table_rows
        self.indexes = indexes if indexes is not None else [("PRIMARY", "id", 1, 0)]
        self.metadata_error = metadata_error
        self.tuple_rows = tuple_rows
        self.executed = []

    def rows_for(self, sql, args):
        self.executed.append((sql, args))
        text = sql.strip()
        if text == "SHOW DATABASES":
            return [{"Database": DB}, {"Database": "reporting"}]
        if text.startswith("EXPLAIN"):
            if isinstance(self.explain, Exception):
                raise self.explain
            return [dict(row) for row in self.explain]
        if text.startswith("SELECT DATABASE()"):
            return [{"db_name": DB}]
        if "information_schema" in text:
            if self.metadata_error is not None:
                raise self.metadata_error
            schema, table, _limit = args
            if "information_schema.TABLES" in text:
                return [{"ENGINE": "InnoDB", "TABLE_ROWS": self.table_rows, "DATA_LENGTH": 16384,
                         "INDEX_LENGTH": 0, "CREATE_OPTIONS": "", "UPDATE_TIME": None}]
            if "information_schema.STATISTICS" in text:
                return [{"INDEX_NAME": name, "COLUMN_NAME": column, "SEQ_IN_INDEX": seq, "NON_UNIQUE": non_unique,
                         "CARDINALITY": 10, "SUB_PART": None, "INDEX_TYPE": "BTREE"}
                        for name, column, seq, non_unique in self.indexes]
            if "information_schema.COLUMNS" in text:
                return [{"COLUMN_NAME": "id", "DATA_TYPE": "int", "IS_NULLABLE": "NO", "COLUMN_KEY": "PRI",
                         "ORDINAL_POSITION": 1}]
        raise AssertionError(f"unexpected query: {text[:60]}")


class FakeCursor:
    def __init__(self, server):
        self.server, self.rows = server, []

    def execute(self, sql, args=None):
        rows = self.server.rows_for(sql, args)
        self.rows = [tuple(row.values()) for row in rows] if self.server.tuple_rows else rows

    def fetchall(self):
        return list(self.rows)

    def fetchmany(self, size):
        return list(self.rows[:size])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, server):
        self.server = server

    def cursor(self):
        return FakeCursor(self.server)

    def close(self):
        pass


def explain_row(access_type, rows, key=None, extra=None):
    return {"id": 1, "select_type": "SIMPLE", "table": "users", "partitions": None, "type": access_type,
            "possible_keys": key, "key": key, "key_len": "4" if key else None, "ref": None, "rows": rows,
            "filtered": 100.0, "Extra": extra}


# ---------------------------------------------------------------- harnesses


def remote_runtime(monkeypatch, server):
    runtime = ConnectorRuntime(allowed_origins=[ORIGIN])
    runtime.company_targets.targets["t1"] = CompanyTarget(
        target_id="t1", display_name="Company", host=HOST, port=3306, approval_state="approved",
        dns_identity_snapshot={"host": HOST, "addresses": ["203.0.113.10"]}, tls_hostname=HOST)
    connects = []

    def fake_connect(destination, **kwargs):
        connects.append(kwargs)
        return FakeConnection(server)

    monkeypatch.setattr(runtime, "resolve_and_validate_approved_company_target",
                        lambda target_id: CompanyTargetDestination(HOST, "203.0.113.10", socket.AF_INET, 3306))
    monkeypatch.setattr(runtime, "connect_company_target_mysql", fake_connect)
    return runtime, connects


def paired_client(runtime):
    client = TestClient(create_app(runtime=runtime))
    token = client.post("/pair", headers={"Origin": ORIGIN, "Content-Type": "application/json"},
                        json={"pairing_token": runtime.issue_browser_pairing_token()}).json()["session_token"]
    return client, {"Origin": ORIGIN, "Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def login(user=USER, secret=SECRET):
    return {"username": user, "password": secret}


def remote_validate(monkeypatch, server, sql, *, database=DB):
    runtime, connects = remote_runtime(monkeypatch, server)
    client, headers = paired_client(runtime)
    if database is not None:
        found = client.post("/company/targets/t1/databases", headers=headers, json=login())
        assert found.status_code == 200 and DB in found.json()["databases"]
    body = {"sql": sql, **login()}
    if database is not None:
        body["database"] = database
    response = client.post("/company/targets/t1/validate", headers=headers, json=body)
    return response, connects


def local_validate_with(monkeypatch, server, sql):
    """Local /api/validate with the same fake server as its per-request evidence
    connection: both paths now run the real shared collectors on one connection."""
    use_fake_evidence_connection(monkeypatch, FakeConnection(server))
    return jsonable_encoder(local_validate(local_request(sql, database=DB)))


# ---------------------------------------------------------------- remote == local scoring semantics

SCENARIOS = {
    "full_scan_update_metadata_available": dict(
        sql="UPDATE users SET active = 0 WHERE status = 'old'",
        server=dict(explain=[explain_row("ALL", 500, extra="Using where")], table_rows=250_000)),
    "full_scan_update_metadata_unavailable": dict(
        sql="UPDATE users SET active = 0 WHERE status = 'old'",
        server=dict(explain=[explain_row("ALL", 500, extra="Using where")],
                    metadata_error=RuntimeError("SELECT command denied"))),
    "high_estimated_rows_delete": dict(
        sql="DELETE FROM users WHERE created_at < '2020-01-01'",
        server=dict(explain=[explain_row("range", 50_000, key="idx_created")])),
    "indexed_selective_update": dict(
        sql="UPDATE users SET active = 0 WHERE id = 5",
        server=dict(explain=[explain_row("const", 1, key="PRIMARY")])),
    "select": dict(
        sql="SELECT * FROM users WHERE id = 1",
        server=dict(explain=[explain_row("const", 1, key="PRIMARY")])),
    "critical_delete_no_reduction": dict(
        sql="DELETE FROM users",
        server=dict(explain=[explain_row("const", 1, key="PRIMARY")])),
    "unrestricted_update_no_reduction": dict(
        sql="UPDATE users SET active = 0",
        server=dict(explain=[explain_row("const", 1, key="PRIMARY")])),
    "evidence_unavailable": dict(
        sql="UPDATE users SET active = 0 WHERE id = 5",
        server=dict(explain=RuntimeError("Table 'appdb.users' doesn't exist"))),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_remote_validation_matches_local_evidence_scoring(monkeypatch, name):
    scenario = SCENARIOS[name]
    remote, _ = remote_validate(monkeypatch, FakeServer(**scenario["server"]), scenario["sql"])
    assert remote.status_code == 200, remote.text
    local = local_validate_with(monkeypatch, FakeServer(**scenario["server"]), scenario["sql"])
    # Every field: score, risk_level, confidence, reasons, checklist, factors,
    # evidence_factors, metadata_*, high_estimated_rows_applied, database_evidence,
    # analysis_mode and the overall score/risk level.
    assert remote.json() == local

    statement = remote.json()["statements"][0]
    labels = {factor["label"]: factor["points"] for factor in statement["evidence_factors"]}
    metadata_codes = {factor["code"] for factor in statement["metadata_factors"]}

    if name == "full_scan_update_metadata_available":
        assert labels == {"EXPLAIN indicates a full table scan": 10}
        assert "M1_TABLE_ROWS_FULL_SCAN_WRITE" in metadata_codes
        assert statement["metadata_score_adjustment"] >= 5 and statement["metadata_status"] == "available"
        assert statement["score"] == statement["static_score"] + 10 + statement["metadata_score_adjustment"]
    elif name == "full_scan_update_metadata_unavailable":
        assert labels == {"EXPLAIN indicates a full table scan": 10}
        assert statement["metadata_status"] != "available" and statement["metadata_score_adjustment"] == 0
        assert statement["score"] == statement["static_score"] + 10
    elif name == "high_estimated_rows_delete":
        assert labels == {"EXPLAIN estimated rows are high": 10}
        assert statement["high_estimated_rows_applied"] is True
    elif name == "indexed_selective_update":
        assert labels == {"EXPLAIN confirms selective indexed access": -3}
        assert statement["score"] == statement["static_score"] - 3
    elif name == "select":
        assert labels == {} and statement["score"] == statement["static_score"]
        assert statement["confidence"] == "DATABASE_EVIDENCE"
    elif name == "critical_delete_no_reduction":
        assert statement["static_risk_level"] == "CRITICAL"
        assert labels == {} and statement["score"] == statement["static_score"]
    elif name == "unrestricted_update_no_reduction":
        assert statement["score"] >= statement["static_score"]
        assert "EXPLAIN confirms selective indexed access" not in labels
    elif name == "evidence_unavailable":
        assert statement["database_evidence"]["available"] is False
        assert statement["database_evidence"]["error"] == "explain_failed"
        assert labels == {} and statement["score"] == statement["static_score"]
        assert remote.json()["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"

    if name != "evidence_unavailable":
        assert remote.json()["analysis_mode"] == "DATABASE_EVIDENCE"
    assert remote.json()["overall_score"] == statement["score"]
    assert remote.json()["overall_risk_level"] == statement["risk_level"]


# ---------------------------------------------------------------- row shape


def test_connector_connection_uses_unbuffered_dictionary_rows():
    assert connector_server.pymysql.cursors.SSDictCursor is not None
    import inspect
    source = inspect.getsource(ConnectorRuntime.connect_company_target_mysql)
    assert "cursorclass=pymysql.cursors.SSDictCursor" in source
    assert "cursorclass=pymysql.cursors.SSCursor," not in source


def test_dictionary_row_explain_and_metadata_are_populated(monkeypatch):
    server = FakeServer(explain=[explain_row("ALL", 500, extra="Using where")], table_rows=250_000,
                        indexes=[("PRIMARY", "id", 1, 0), ("idx_status", "status", 1, 1)])
    response, _ = remote_validate(monkeypatch, server, "UPDATE users SET active = 0 WHERE status = 'old'")
    assert response.status_code == 200
    evidence = response.json()["statements"][0]["database_evidence"]
    assert evidence["available"] is True and evidence["access_type"] == "ALL"
    assert evidence["estimated_rows"] == 500 and evidence["full_table_scan"] is True
    assert evidence["database"] == DB
    assert response.json()["statements"][0]["metadata_status"] == "available"
    metadata_queries = [args for sql, args in server.executed if "information_schema" in sql]
    assert metadata_queries and all(args[0] == DB and args[1] == "users" for args in metadata_queries)


def test_former_tuple_row_crash_no_longer_produces_500(monkeypatch):
    """With tuple rows the shared collector cannot parse EXPLAIN; that is now a
    collector failure (evidence unavailable), not an unhandled 500."""
    server = FakeServer(explain=[explain_row("ALL", 500)], tuple_rows=True)
    response, _ = remote_validate(monkeypatch, server, "UPDATE users SET active = 0 WHERE id = 5", database=None)
    assert response.status_code == 200, response.text
    statement = response.json()["statements"][0]
    assert statement["database_evidence"]["available"] is False
    assert statement["database_evidence"]["error"] == "collection_failed"
    assert statement["score"] == statement["static_score"]


# ---------------------------------------------------------------- collector failures


def test_explain_collector_failure_keeps_static_result(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(connector_server, "collect_statement_evidence", boom)
    response, _ = remote_validate(monkeypatch, FakeServer(), "UPDATE users SET active = 0 WHERE id = 5")
    assert response.status_code == 200, response.text
    body = response.json()
    statement = body["statements"][0]
    assert statement["database_evidence"]["error"] == "collection_failed"
    assert statement["score"] == statement["static_score"] and statement["evidence_factors"] == []
    assert body["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"


def test_metadata_collector_failure_keeps_result_usable(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("metadata exploded")

    monkeypatch.setattr(connector_server, "collect_metadata", boom)
    server = FakeServer(explain=[explain_row("ALL", 500)], table_rows=250_000)
    response, _ = remote_validate(monkeypatch, server, "UPDATE users SET active = 0 WHERE status = 'old'")
    assert response.status_code == 200, response.text
    statement = response.json()["statements"][0]
    assert statement["metadata_status"] == "unavailable" and statement["metadata_score_adjustment"] == 0
    assert statement["evidence_factors"] == [{"label": "EXPLAIN indicates a full table scan", "points": 10}]
    assert response.json()["analysis_mode"] == "DATABASE_EVIDENCE"


def test_connection_and_request_failures_remain_real_http_errors(monkeypatch):
    runtime, _ = remote_runtime(monkeypatch, FakeServer())
    client, headers = paired_client(runtime)

    def refuse(destination, **kwargs):
        raise ValueError("database_connection_failed")

    monkeypatch.setattr(runtime, "connect_company_target_mysql", refuse)
    failed = client.post("/company/targets/t1/validate", headers=headers, json={"sql": "SELECT 1", **login()})
    assert failed.status_code == 400 and failed.json()["error_code"] == "database_connection_failed"

    monkeypatch.setattr(runtime, "resolve_and_validate_approved_company_target",
                        lambda target_id: (_ for _ in ()).throw(ValueError("dns_identity_mismatch")))
    mismatch = client.post("/company/targets/t1/validate", headers=headers, json={"sql": "SELECT 1", **login()})
    assert mismatch.status_code == 400 and mismatch.json()["error_code"] == "dns_identity_mismatch"

    too_many = ";".join(["SELECT 1"] * 17)
    runtime2, _ = remote_runtime(monkeypatch, FakeServer())
    client2, headers2 = paired_client(runtime2)
    limited = client2.post("/company/targets/t1/validate", headers=headers2, json={"sql": too_many, **login()})
    assert limited.status_code == 400 and limited.json()["error_code"] == "statement_limit_exceeded"


# ---------------------------------------------------------------- remote database selection


def test_selected_database_is_used_for_connection_and_metadata(monkeypatch):
    server = FakeServer(explain=[explain_row("const", 1, key="PRIMARY")])
    response, connects = remote_validate(monkeypatch, server, "UPDATE users SET active = 0 WHERE id = 5")
    assert response.status_code == 200
    assert connects[-1]["database"] == DB
    assert any("information_schema" in sql and args[0] == DB for sql, args in server.executed)


def test_missing_remote_database_never_falls_back_to_local_env(monkeypatch):
    monkeypatch.setenv("MYSQL_DATABASE", "local_env_db")
    calls = []

    def forbidden_client():
        calls.append("get_client")
        raise AssertionError("remote validation must not read local DB configuration")

    monkeypatch.setattr(backend_metadata, "get_client", forbidden_client)
    server = FakeServer(explain=[explain_row("ALL", 500)])
    response, connects = remote_validate(monkeypatch, server, "UPDATE users SET active = 0 WHERE id = 5",
                                         database=None)
    assert response.status_code == 200, response.text
    assert calls == []
    assert connects[-1]["database"] is None
    assert response.json()["statements"][0]["metadata_status"] == "unavailable"
    assert not any("information_schema" in sql for sql, _ in server.executed)
    assert "local_env_db" not in repr(server.executed) and "local_env_db" not in response.text


def test_undiscovered_database_and_foreign_login_remain_rejected(monkeypatch):
    server = FakeServer(explain=[explain_row("const", 1, key="PRIMARY")])
    runtime, connects = remote_runtime(monkeypatch, server)
    client, headers = paired_client(runtime)
    undiscovered = client.post("/company/targets/t1/validate", headers=headers,
                               json={"sql": "SELECT 1", "database": DB, **login()})
    assert undiscovered.status_code == 409 and connects == []

    assert client.post("/company/targets/t1/databases", headers=headers, json=login()).status_code == 200
    connects_after_discovery = len(connects)
    foreign = client.post("/company/targets/t1/validate", headers=headers,
                          json={"sql": "SELECT 1", "database": DB, **login("other_user", "Other-Secret-1")})
    assert foreign.status_code == 409 and foreign.json()["error_code"] == "company_database_scope_unavailable"
    not_listed = client.post("/company/targets/t1/validate", headers=headers,
                             json={"sql": "SELECT 1", "database": "payroll", **login()})
    assert not_listed.status_code == 409 and not_listed.json()["error_code"] == "database_not_authorized"
    assert len(connects) == connects_after_discovery


# ---------------------------------------------------------------- credentials on the evidence path


def test_evidence_path_never_leaks_credentials(monkeypatch, caplog):
    server = FakeServer(explain=RuntimeError(f"Access denied for user '{USER}' (using password: {SECRET})"),
                        metadata_error=RuntimeError(f"SELECT command denied to user '{USER}' password {SECRET}"))
    with caplog.at_level(logging.DEBUG):
        response, _ = remote_validate(monkeypatch, server, "UPDATE users SET active = 0 WHERE status = 'old'")
    assert response.status_code == 200
    assert SECRET not in response.text and USER not in response.text
    assert SECRET not in caplog.text
