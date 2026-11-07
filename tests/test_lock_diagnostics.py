"""InnoDB lock waits (backend.lock_diagnostics) and its two routes.

Verification level: unit / TestClient. The MySQL server is a fake cursor that answers
only the collector's fixed queries; no real MySQL, network or TLS is involved. The
live checks with a real lock wait on MySQL 8.4 are in tests_mysql/live_mysql_checks.py.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pymysql
import pytest
from fastapi.testclient import TestClient

import backend.main as backend_main
from backend import lock_diagnostics as ld
from backend.db import READ_ONLY_CHECK_SQL, READ_ONLY_SESSION_SQL
from tests.test_connector_remote_evidence import FakeServer, paired_client, remote_runtime
from tests.test_observed_performance import Connection

ROOT = Path(__file__).resolve().parents[1]
DB = "appdb"
SECRET = "Lock-Waits-Secret-7713"


def wait_row(waiting=112, blocking=111, **overrides):
    # The shape MySQL 8.4.13 returned for the CI lock wait.
    row = {"REQUESTING_THREAD_ID": waiting, "BLOCKING_THREAD_ID": blocking, "OBJECT_SCHEMA": DB,
           "OBJECT_NAME": "users", "PARTITION_NAME": None, "INDEX_NAME": "PRIMARY", "LOCK_TYPE": "RECORD",
           "REQUESTED_MODE": "X,REC_NOT_GAP", "BLOCKING_MODE": "X,REC_NOT_GAP"}
    row.update(overrides)
    return row


class LockServer:
    """Answers the collector's fixed queries; anything else fails the test."""

    def __init__(self, *, read_only=1, enabled=1, waits=None, threads=None):
        self.read_only, self.enabled = read_only, enabled
        self.waits = [wait_row()] if waits is None else waits
        self.threads = threads if threads is not None else [
            {"THREAD_ID": 111, "PROCESSLIST_ID": 74, "PROCESSLIST_TIME": 9},
            {"THREAD_ID": 112, "PROCESSLIST_ID": 75, "PROCESSLIST_TIME": 3}]
        self.executed = []

    def rows_for(self, sql, args):
        self.executed.append((sql, args))
        if sql == ld.STATE_SQL:
            return [{"transaction_read_only": self.read_only, "performance_schema": self.enabled}]
        if sql == ld.LOCK_WAITS_SQL:
            if isinstance(self.waits, Exception):
                raise self.waits
            return list(self.waits)
        if sql.startswith(ld.THREADS_SQL.split("{}")[0]):
            if isinstance(self.threads, Exception):
                raise self.threads
            return list(self.threads)
        raise AssertionError(f"unexpected query: {sql[:60]}")


def collect(server):
    return ld.collect_lock_waits(Connection(server), DB)


# ---------------------------------------------------------------- collector


def test_a_wait_shows_who_waits_on_whom_and_where():
    server = LockServer()
    result = collect(server)
    assert result["status"] == "available" and result["scope"] == "innodb_lock_waits_snapshot"
    assert result["schema"] == DB and result["limit"] == 20 and result["more_waits"] is False
    assert result["sessions"] == "available"
    assert "may already have finished" in result["note"]
    assert result["waits"] == [{
        "waiting": {"thread_id": 112, "session_id": 75, "seconds_in_state": 3},
        "blocking": {"thread_id": 111, "session_id": 74, "seconds_in_state": 9},
        "table": "users", "partition": None, "index": "PRIMARY", "lock_type": "RECORD",
        "requested_mode": "X,REC_NOT_GAP", "blocking_mode": "X,REC_NOT_GAP",
    }]
    assert [sql for sql, _ in server.executed] == [
        ld.STATE_SQL, ld.LOCK_WAITS_SQL, ld.THREADS_SQL.format("%s, %s")]
    assert [args for _, args in server.executed] == [None, (DB,), (111, 112)]


def test_no_waits_is_its_own_status_and_reads_no_threads():
    server = LockServer(waits=[])
    result = collect(server)
    assert result["status"] == "no_lock_waits" and result["waits"] == []
    assert len(server.executed) == 2


@pytest.mark.parametrize("errno", [1142, 1143])
def test_missing_lock_privilege_is_insufficient_privileges(errno):
    result = collect(LockServer(waits=pymysql.err.OperationalError(errno, f"denied {SECRET}")))
    assert result["status"] == "insufficient_privileges" and result["waits"] == []
    assert SECRET not in repr(result)


def test_without_threads_privilege_sessions_are_unverified_not_zero():
    result = collect(LockServer(threads=pymysql.err.OperationalError(1143, "column denied")))
    assert result["status"] == "available" and result["sessions"] == "unverified"
    assert result["waits"][0]["waiting"] == {"thread_id": 112, "session_id": None, "seconds_in_state": None}


def test_a_thread_that_ended_meanwhile_is_unknown_not_zero():
    result = collect(LockServer(threads=[{"THREAD_ID": 111, "PROCESSLIST_ID": 74, "PROCESSLIST_TIME": 9}]))
    assert result["waits"][0]["waiting"] == {"thread_id": 112, "session_id": None, "seconds_in_state": None}
    assert result["waits"][0]["blocking"]["session_id"] == 74


def test_disabled_performance_schema_stops_before_any_table_read():
    server = LockServer(enabled=0)
    assert collect(server)["status"] == "performance_schema_disabled"
    assert [sql for sql, _ in server.executed] == [ld.STATE_SQL]


def test_lost_read_only_session_sends_nothing_more():
    server = LockServer(read_only=0)
    assert collect(server)["status"] == "collection_failed"
    assert [sql for sql, _ in server.executed] == [ld.STATE_SQL]


@pytest.mark.parametrize("field, value", [
    ("REQUESTING_THREAD_ID", None), ("REQUESTING_THREAD_ID", 0), ("BLOCKING_THREAD_ID", "111"),
    ("BLOCKING_THREAD_ID", True), ("OBJECT_SCHEMA", "other_db"), ("OBJECT_NAME", None), ("OBJECT_NAME", ""),
    ("OBJECT_NAME", "x" * 300), ("LOCK_TYPE", "GAP"), ("REQUESTED_MODE", None), ("INDEX_NAME", 5),
])
def test_null_or_invalid_values_fail_the_whole_result(field, value):
    result = collect(LockServer(waits=[wait_row(), wait_row(**{field: value})]))
    assert result["status"] == "unexpected_response" and result["waits"] == []


@pytest.mark.parametrize("thread", [
    {"THREAD_ID": 999, "PROCESSLIST_ID": 1, "PROCESSLIST_TIME": 1},
    {"THREAD_ID": 111, "PROCESSLIST_ID": 0, "PROCESSLIST_TIME": 1},
    {"THREAD_ID": 111, "PROCESSLIST_ID": 74, "PROCESSLIST_TIME": -1},
    {"THREAD_ID": 111, "PROCESSLIST_ID": 74, "PROCESSLIST_TIME": "9"},
])
def test_unexpected_thread_rows_fail_the_whole_result(thread):
    assert collect(LockServer(threads=[thread]))["status"] == "unexpected_response"


def test_a_table_lock_has_no_index():
    result = collect(LockServer(waits=[wait_row(LOCK_TYPE="TABLE", INDEX_NAME=None, REQUESTED_MODE="X")]))
    assert result["waits"][0]["index"] is None and result["waits"][0]["lock_type"] == "TABLE"


def test_result_limit_keeps_twenty_and_reports_more():
    rows = [wait_row(waiting=200 + i) for i in range(21)]
    result = collect(LockServer(waits=rows, threads=[]))
    assert result["status"] == "available" and len(result["waits"]) == 20 and result["more_waits"] is True


def test_rows_beyond_the_sql_limit_fail_safely():
    result = collect(LockServer(waits=[wait_row(waiting=200 + i) for i in range(22)]))
    assert result["status"] == "unexpected_response" and result["waits"] == []


@pytest.mark.parametrize("error, status", [
    (RuntimeError(f"boom {SECRET}"), "collection_failed"),
    (pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query"), "collection_failed"),
])
def test_query_failure_and_connection_loss(error, status):
    result = collect(LockServer(waits=error))
    assert result["status"] == status and SECRET not in repr(result)


def test_unknown_status_codes_never_pass_through():
    assert ld.result(DB, "Access denied for user 'x'")["status"] == "collection_failed"


def test_fixed_sql_never_reads_row_values_sql_text_or_changes_anything():
    joined = " ".join(ld.FIXED_SQL).upper()
    for word in ("LOCK_DATA", "PROCESSLIST_INFO", "SQL_TEXT", "INNODB_TRX", "TRX_QUERY", "SELECT *",
                 "KILL", "COMMIT", "ROLLBACK", "UPDATE ", "INSERT ", "DELETE ", "SET ", "LOCK TABLES"):
        assert word not in joined, word
    assert ld.LOCK_WAITS_SQL.count("%s") == 1 and "LIMIT 21" in ld.LOCK_WAITS_SQL


def test_collector_stays_out_of_sql_validation_and_scoring():
    importers = []
    for path in sorted((ROOT / "backend").glob("*.py")) + sorted((ROOT / "connector").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any("lock_diagnostics" in name for name in names):
                importers.append(path.name)
    assert sorted(set(importers)) == ["main.py", "server.py"]


# ---------------------------------------------------------------- local route

LOCAL = {"host": "127.0.0.1", "port": 3306, "username": "local_dba", "password": SECRET, "database": DB}
PATH = "/api/diagnostics/lock-waits"


@pytest.fixture
def local(monkeypatch):
    server = LockServer()
    connection = Connection(server)
    opened = []

    def fake_open(profile, database, **_kwargs):
        opened.append((profile.host, profile.username, database))
        return connection, None

    monkeypatch.setattr(backend_main, "open_evidence_connection", fake_open)
    return TestClient(backend_main.app, base_url="http://127.0.0.1:8420"), server, connection, opened


def test_local_route_collects_on_the_request_connection_and_closes_it(local, caplog):
    client, server, connection, opened = local
    with caplog.at_level(logging.DEBUG):
        response = client.post(PATH, json=LOCAL)
    assert response.status_code == 200 and response.json()["status"] == "available"
    assert opened == [("127.0.0.1", "local_dba", DB)] and connection.closed == 1
    assert SECRET not in response.text and SECRET not in caplog.text


@pytest.mark.parametrize("change, code", [
    ({"database": "app`; DROP"}, "invalid_database"),
    ({"host": "db.company.example"}, "local_only_target_required"),
    ({"port": 3307}, "local_only_target_required"),
])
def test_local_route_refuses_before_any_connection(local, monkeypatch, change, code):
    client, server, _, opened = local
    monkeypatch.setattr(backend_main.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("203.0.113.10", 3306))])
    assert client.post(PATH, json={**LOCAL, **change}).json()["error_code"] == code
    assert opened == [] and server.executed == []


@pytest.mark.parametrize("extra", [{"sql": "KILL 75"}, {"query": "SELECT 1"}, {"kill": True}])
def test_local_route_accepts_no_sql_or_action(local, extra):
    client, server, _, opened = local
    assert client.post(PATH, json={**LOCAL, **extra}).status_code == 422
    assert opened == [] and server.executed == []


# ---------------------------------------------------------------- company route (connector)

COMPANY_PATH = "/company/targets/t1/lock-waits"


class CompanyLockServer(FakeServer):
    """The connector's fake MySQL plus the collector's fixed queries."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.locks = LockServer()

    def rows_for(self, sql, args):
        if sql == ld.STATE_SQL or sql == ld.LOCK_WAITS_SQL or sql.startswith(ld.THREADS_SQL.split("{}")[0]):
            self.executed.append((sql, args))
            if sql == ld.STATE_SQL:
                return [{"transaction_read_only": 1 if self.read_only else 0, "performance_schema": 1}]
            return self.locks.rows_for(sql, args)
        return super().rows_for(sql, args)


def login(user="remote_dba"):
    return {"username": user, "password": SECRET}


def company_client(monkeypatch, server, *, discover=True):
    runtime, connects = remote_runtime(monkeypatch, server)
    client, headers = paired_client(runtime)
    if discover:
        assert client.post("/company/targets/t1/databases", headers=headers, json=login()).status_code == 200
    return runtime, client, headers, connects


def test_company_route_collects_after_every_existing_gate(monkeypatch, caplog):
    server = CompanyLockServer()
    _, client, headers, connects = company_client(monkeypatch, server)
    with caplog.at_level(logging.DEBUG):
        response = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **login()})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "available" and connects[-1]["database"] == DB
    executed = [sql for sql, _ in server.executed]
    tail = executed[executed.index(READ_ONLY_SESSION_SQL, executed.index("SHOW DATABASES")):]
    assert tail == [READ_ONLY_SESSION_SQL, READ_ONLY_CHECK_SQL, ld.STATE_SQL, ld.LOCK_WAITS_SQL,
                    ld.THREADS_SQL.format("%s, %s")]
    assert SECRET not in response.text and SECRET not in caplog.text


def test_company_route_refuses_an_unapproved_target(monkeypatch):
    runtime, client, headers, connects = company_client(monkeypatch, CompanyLockServer())
    runtime.company_targets.targets["t1"].approval_state = "needs_reapproval"
    before = len(connects)
    response = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **login()})
    assert response.status_code == 409 and response.json()["error_code"] == "company_target_not_approved"
    assert len(connects) == before


@pytest.mark.parametrize("headers_change", [
    {"Authorization": "Bearer not-a-session-token-at-all-0000000000"},
    {"Origin": "https://evil.example"},
])
def test_company_route_refuses_an_invalid_pairing_or_origin(monkeypatch, headers_change):
    _, client, headers, connects = company_client(monkeypatch, CompanyLockServer(), discover=False)
    response = client.post(COMPANY_PATH, headers={**headers, **headers_change}, json={"database": DB, **login()})
    assert response.status_code == 401 and connects == []


def test_company_route_refuses_an_undiscovered_or_foreign_database(monkeypatch):
    _, client, headers, connects = company_client(monkeypatch, CompanyLockServer(), discover=False)
    assert client.post(COMPANY_PATH, headers=headers, json={"database": DB, **login()}).status_code == 409
    assert connects == []
    assert client.post("/company/targets/t1/databases", headers=headers, json=login()).status_code == 200
    before = len(connects)
    foreign = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **login("other_user")})
    not_listed = client.post(COMPANY_PATH, headers=headers, json={"database": "payroll", **login()})
    assert foreign.status_code == 409 and not_listed.status_code == 409 and len(connects) == before


@pytest.mark.parametrize("body_change", [{"sql": "KILL 75"}, {"database": None}, {"host": "other.example"}])
def test_company_route_accepts_no_sql_or_destination(monkeypatch, body_change):
    _, client, headers, connects = company_client(monkeypatch, CompanyLockServer(), discover=False)
    response = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **login(), **body_change})
    assert response.status_code == 400 and response.json()["error_code"] == "invalid_lock_waits_request"
    assert connects == []


def test_company_route_connection_failure_is_a_safe_error(monkeypatch):
    runtime, client, headers, _ = company_client(monkeypatch, CompanyLockServer())

    def refuse(destination, **kwargs):
        raise ValueError("database_connection_failed")

    monkeypatch.setattr(runtime, "connect_company_target_mysql", refuse)
    response = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **login()})
    assert response.status_code == 400 and response.json()["error_code"] == "database_connection_failed"
    assert SECRET not in response.text
