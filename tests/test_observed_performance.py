"""Historical schema workload (backend.observed_performance) and its two routes.

Verification level: unit / TestClient. The MySQL server is a fake cursor that answers
only the collector's fixed queries; no real MySQL, network or TLS is involved. The
live checks against MySQL 8.4 are in tests_mysql/live_mysql_checks.py.
"""

from __future__ import annotations

import ast
import logging
from datetime import datetime
from pathlib import Path

import pymysql
import pytest
from fastapi.testclient import TestClient

import backend.main as backend_main
from backend import observed_performance as op
from backend.db import READ_ONLY_CHECK_SQL, READ_ONLY_SESSION_SQL, enforce_read_only_session
from tests.test_connector_remote_evidence import FakeServer, paired_client, remote_runtime

ROOT = Path(__file__).resolve().parents[1]
DB = "appdb"
SECRET = "Observed-Perf-Secret-5521"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def family_row(digest=DIGEST_A, **overrides):
    row = {
        "DIGEST": digest, "DIGEST_TEXT": "SELECT `id` FROM `users` WHERE `customer_id` = ?",
        "COUNT_STAR": 25, "SUM_TIMER_WAIT": 2_500_000_000, "AVG_TIMER_WAIT": 100_000_000,
        "MAX_TIMER_WAIT": 400_000_000, "QUANTILE_95": 199_526_231, "SUM_LOCK_TIME": 50_000_000,
        "SUM_ROWS_EXAMINED": 75, "SUM_ROWS_SENT": 50, "SUM_ROWS_AFFECTED": 0, "SUM_NO_INDEX_USED": 0,
        "FIRST_SEEN": datetime(2026, 11, 7, 16, 39, 27, 512000),
        "LAST_SEEN": datetime(2026, 11, 7, 16, 39, 28, 1000),
    }
    row.update(overrides)
    return row


def digest(index):
    return f"{index:064x}"


class PerfServer:
    """Answers the collector's fixed queries; anything else fails the test."""

    def __init__(self, *, read_only=1, enabled=1, consumers=("YES", "YES"), families=None, overflow=()):
        self.read_only, self.enabled, self.consumers = read_only, enabled, consumers
        self.families = [family_row()] if families is None else families
        self.overflow = overflow
        self.executed = []

    def rows_for(self, sql, args):
        self.executed.append((sql, args))
        if sql == op.STATE_SQL:
            return [{"transaction_read_only": self.read_only, "performance_schema": self.enabled}]
        if sql == op.CONSUMERS_SQL:
            if isinstance(self.consumers, Exception):
                raise self.consumers
            return [{"NAME": "global_instrumentation", "ENABLED": self.consumers[0]},
                    {"NAME": "statements_digest", "ENABLED": self.consumers[1]}]
        if sql == op.FAMILIES_SQL:
            if isinstance(self.families, Exception):
                raise self.families
            return list(self.families)
        if sql == op.OVERFLOW_SQL:
            return [{"COUNT_STAR": count} for count in self.overflow]
        raise AssertionError(f"unexpected query: {sql[:60]}")


class Cursor:
    def __init__(self, server):
        self.server, self.rows = server, []

    def execute(self, sql, args=None):
        self.rows = self.server.rows_for(sql, args)

    def fetchmany(self, size):
        return list(self.rows[:size])

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class Connection:
    def __init__(self, server):
        self.server, self.closed = server, 0

    def cursor(self):
        return Cursor(self.server)

    def close(self):
        self.closed += 1


def collect(server):
    return op.collect_observed_performance(Connection(server), DB)


def sent(server):
    return [sql for sql, _ in server.executed]


# ---------------------------------------------------------------- collector


def test_successful_collection_reports_families_in_milliseconds():
    server = PerfServer(overflow=(0,))
    result = collect(server)
    assert result["status"] == "available"
    assert result["scope"] == "historical_schema_workload"
    assert result["source"] == "performance_schema.events_statements_summary_by_digest"
    assert result["schema"] == DB and result["limit"] == 5
    assert result["digest_consumer"] == "enabled"
    assert result["more_families"] is False and result["overflow_executions"] == 0
    assert "not measurements of any SQL you submitted" in result["note"]
    assert result["families"] == [{
        "digest": DIGEST_A, "digest_text": "SELECT `id` FROM `users` WHERE `customer_id` = ?",
        "digest_text_truncated": False, "executions": 25, "timing": "recorded",
        # Picoseconds / 1e9 = milliseconds.
        "total_time_ms": 2.5, "avg_time_ms": 0.1, "max_time_ms": 0.4, "p95_time_ms": 0.2,
        "lock_time_ms": 0.05, "rows_examined": 75, "rows_sent": 50, "rows_affected": 0,
        "no_index_used": 0, "first_seen": "2026-11-07T16:39:27.512000",
        "last_seen": "2026-11-07T16:39:28.001000",
    }]
    assert sent(server) == list(op.FIXED_SQL)
    assert [args for _, args in server.executed] == [None, None, (DB,), None]


def test_empty_digest_results_are_no_observations():
    result = collect(PerfServer(families=[]))
    assert result["status"] == "no_observations" and result["families"] == []


@pytest.mark.parametrize("errno", [1142, 1143])
def test_missing_digest_privilege_is_insufficient_privileges(errno):
    server = PerfServer(families=pymysql.err.OperationalError(errno, f"denied to user 'u' {SECRET}"))
    result = collect(server)
    assert result["status"] == "insufficient_privileges" and result["families"] == []
    assert SECRET not in repr(result)


def test_without_setup_consumers_privilege_the_consumer_is_unverified():
    result = collect(PerfServer(consumers=pymysql.err.OperationalError(1142, "SELECT command denied")))
    assert result["status"] == "available" and result["digest_consumer"] == "unverified"


def test_disabled_performance_schema_stops_before_any_table_read():
    server = PerfServer(enabled=0)
    assert collect(server)["status"] == "performance_schema_disabled"
    assert sent(server) == [op.STATE_SQL]


@pytest.mark.parametrize("consumers", [("YES", "NO"), ("NO", "YES")])
def test_disabled_digest_consumer_reads_no_families(consumers):
    server = PerfServer(consumers=consumers)
    result = collect(server)
    assert result["status"] == "digest_consumer_disabled" and result["families"] == []
    assert op.FAMILIES_SQL not in sent(server)


@pytest.mark.parametrize("total", [0, None])
def test_untimed_families_keep_counts_and_report_no_time(total):
    # The shape MySQL 8.4.13 returned with setup_instruments TIMED = 'NO': timer sums 0,
    # lock time still recorded.
    row = family_row(SUM_TIMER_WAIT=total, AVG_TIMER_WAIT=0, MAX_TIMER_WAIT=0, QUANTILE_95=0,
                     SUM_LOCK_TIME=3_000_000)
    family = collect(PerfServer(families=[row]))["families"][0]
    assert family["timing"] == "not_recorded" and family["executions"] == 25
    assert [family[key] for key in ("total_time_ms", "avg_time_ms", "max_time_ms",
                                    "p95_time_ms")] == [None] * 4
    assert family["lock_time_ms"] == 0.003


@pytest.mark.parametrize("field, value", [
    ("COUNT_STAR", None), ("SUM_ROWS_EXAMINED", None), ("COUNT_STAR", -1), ("COUNT_STAR", True),
    ("COUNT_STAR", "25"), ("COUNT_STAR", 25.0), ("SUM_TIMER_WAIT", -5), ("MAX_TIMER_WAIT", "1"),
    ("FIRST_SEEN", "2026-11-07"), ("LAST_SEEN", None), ("DIGEST_TEXT", 7),
])
def test_null_or_invalid_values_fail_the_whole_result(field, value):
    rows = [family_row(), family_row(DIGEST_B, **{field: value})]
    result = collect(PerfServer(families=rows))
    assert result["status"] == "unexpected_response" and result["families"] == []


@pytest.mark.parametrize("value", [None, "", "A" * 64, "a" * 63, "g" * 64, "a" * 64 + "\n", 12])
def test_digest_must_be_64_lowercase_hex(value):
    assert collect(PerfServer(families=[family_row(DIGEST=value)]))["status"] == "unexpected_response"


def test_long_digest_text_is_cut_and_marked():
    family = collect(PerfServer(families=[family_row(DIGEST_TEXT="x" * 5000)]))["families"][0]
    assert len(family["digest_text"]) == 1024 and family["digest_text_truncated"] is True


def test_result_limit_keeps_five_and_reports_more():
    result = collect(PerfServer(families=[family_row(digest(i)) for i in range(6)]))
    assert result["status"] == "available" and len(result["families"]) == 5
    assert result["more_families"] is True


def test_rows_beyond_the_sql_limit_fail_safely():
    result = collect(PerfServer(families=[family_row(digest(i)) for i in range(7)]))
    assert result["status"] == "unexpected_response" and result["families"] == []


def test_sixth_row_is_validated_although_not_returned():
    rows = [family_row(digest(i)) for i in range(5)] + [family_row("bad")]
    assert collect(PerfServer(families=rows))["status"] == "unexpected_response"


@pytest.mark.parametrize("overflow, expected", [((), 0), ((1234,), 1234)])
def test_overflow_row_count(overflow, expected):
    assert collect(PerfServer(overflow=overflow))["overflow_executions"] == expected


def test_more_than_one_overflow_row_is_unexpected():
    assert collect(PerfServer(overflow=(1, 2)))["status"] == "unexpected_response"


def test_query_failure_is_collection_failed_without_the_server_message():
    result = collect(PerfServer(families=RuntimeError(f"boom {SECRET}")))
    assert result["status"] == "collection_failed" and SECRET not in repr(result)


def test_connection_loss_is_collection_failed():
    lost = pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
    assert collect(PerfServer(families=lost))["status"] == "collection_failed"


def test_lost_read_only_session_sends_nothing_more():
    server = PerfServer(read_only=0)
    assert collect(server)["status"] == "collection_failed"
    assert sent(server) == [op.STATE_SQL]


@pytest.mark.parametrize("state", [[], [{"x": 1}, {"x": 2}], [("1", "1")]])
def test_malformed_state_rows_are_unexpected(state):
    server = PerfServer()
    server.rows_for = lambda sql, args: state
    assert collect(server)["status"] == "unexpected_response"


def test_unknown_status_codes_never_pass_through():
    assert op.result(DB, "Access denied for user 'x'")["status"] == "collection_failed"


def test_fixed_sql_reads_only_the_documented_columns_and_never_samples():
    joined = " ".join(op.FIXED_SQL).upper()
    assert "QUERY_SAMPLE" not in joined and "EVENTS_STATEMENTS_HISTORY" not in joined
    assert "SQL_TEXT" not in joined and "SELECT *" not in joined
    assert not any(word in joined for word in ("UPDATE ", "INSERT ", "DELETE ", "TRUNCATE", "SET "))
    # The only bound value is the schema; the limit is a constant.
    assert [sql.count("%s") for sql in op.FIXED_SQL] == [0, 0, 1, 0]
    assert "LIMIT 6" in op.FAMILIES_SQL


def test_collector_stays_out_of_sql_validation_and_scoring():
    # Only the two routes use it; no scoring or result-building module imports it.
    importers = []
    for path in sorted((ROOT / "backend").glob("*.py")) + sorted((ROOT / "connector").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any("observed_performance" in name for name in names):
                importers.append(path.name)
    assert sorted(set(importers)) == ["main.py", "server.py"]


# ---------------------------------------------------------------- local route

LOCAL = {"host": "127.0.0.1", "port": 3306, "username": "local_dba", "password": SECRET, "database": DB}
PATH = "/api/diagnostics/observed-performance"


@pytest.fixture
def local(monkeypatch):
    server = PerfServer()
    connection = Connection(server)
    opened = []

    def fake_open(profile, database, **_kwargs):
        opened.append((profile.host, profile.username, database))
        return connection, None

    monkeypatch.setattr(backend_main, "open_evidence_connection", fake_open)
    client = TestClient(backend_main.app, base_url="http://127.0.0.1:8420")
    return client, server, connection, opened


def test_local_route_collects_on_the_request_connection_and_closes_it(local, caplog):
    client, server, connection, opened = local
    with caplog.at_level(logging.DEBUG):
        response = client.post(PATH, json=LOCAL)
    assert response.status_code == 200 and response.json()["status"] == "available"
    assert opened == [("127.0.0.1", "local_dba", DB)]
    assert connection.closed == 1
    assert sent(server) == list(op.FIXED_SQL)
    assert SECRET not in response.text and SECRET not in caplog.text


def test_local_route_reports_a_failed_connection(monkeypatch):
    monkeypatch.setattr(backend_main, "open_evidence_connection", lambda profile, database, **_: (None, "auth_failed"))
    response = TestClient(backend_main.app, base_url="http://127.0.0.1:8420").post(PATH, json=LOCAL)
    assert response.json()["status"] == "auth_failed" and response.json()["families"] == []
    assert SECRET not in response.text


@pytest.mark.parametrize("change, code", [
    ({"database": "app`; DROP"}, "invalid_database"),
    ({"database": "x" * 65}, "invalid_database"),
    ({"host": "db.company.example"}, "local_only_target_required"),
    ({"port": 3307}, "local_only_target_required"),
    ({"username": "  "}, "invalid_connection_config"),
])
def test_local_route_refuses_before_any_connection(local, monkeypatch, change, code):
    client, server, _, opened = local
    monkeypatch.setattr(backend_main.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("203.0.113.10", 3306))])
    response = client.post(PATH, json={**LOCAL, **change})
    assert response.json()["error_code"] == code
    assert opened == [] and server.executed == []


@pytest.mark.parametrize("extra", [{"sql": "SELECT 1"}, {"query": "SELECT 1"}, {"mode": "company"}])
def test_local_route_accepts_no_sql_or_other_fields(local, extra):
    client, server, _, opened = local
    response = client.post(PATH, json={**LOCAL, **extra})
    assert response.status_code == 422
    assert opened == [] and server.executed == []


def test_local_route_requires_a_database(local):
    client, _, _, opened = local
    body = {key: value for key, value in LOCAL.items() if key != "database"}
    assert client.post(PATH, json=body).status_code == 422 and opened == []


def test_validate_response_carries_nothing_from_the_diagnostic(local):
    _, server, _, opened = local
    result = backend_main.validate(backend_main.ValidateRequest(sql="SELECT * FROM users WHERE id = 1"))
    assert "families" not in repr(result) and "historical_schema_workload" not in repr(result)
    assert opened == [] and server.executed == []


# ---------------------------------------------------------------- company route (connector)

COMPANY_PATH = "/company/targets/t1/observed-performance"


class CompanyServer(FakeServer):
    """The connector's fake MySQL plus the collector's fixed queries."""

    def __init__(self, perf=None, **kwargs):
        super().__init__(**kwargs)
        self.perf = perf or PerfServer()

    def rows_for(self, sql, args):
        if sql in op.FIXED_SQL:
            self.executed.append((sql, args))
            if sql == op.STATE_SQL:
                return [{"transaction_read_only": 1 if self.read_only else 0, "performance_schema": 1}]
            return self.perf.rows_for(sql, args)
        return super().rows_for(sql, args)


def company_login(user="remote_dba"):
    return {"username": user, "password": SECRET}


def company_client(monkeypatch, server, *, discover=True):
    runtime, connects = remote_runtime(monkeypatch, server)
    client, headers = paired_client(runtime)
    if discover:
        assert client.post("/company/targets/t1/databases", headers=headers, json=company_login()).status_code == 200
    return runtime, client, headers, connects


def test_company_route_collects_after_every_existing_gate(monkeypatch, caplog):
    server = CompanyServer()
    _, client, headers, connects = company_client(monkeypatch, server)
    with caplog.at_level(logging.DEBUG):
        response = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **company_login()})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "available"
    assert connects[-1]["database"] == DB
    executed = [sql for sql, _ in server.executed]
    # Discovery's connection, then this request's: read-only first, then only fixed SQL.
    tail = executed[executed.index(READ_ONLY_SESSION_SQL, executed.index("SHOW DATABASES")):]
    assert tail == [READ_ONLY_SESSION_SQL, READ_ONLY_CHECK_SQL, *op.FIXED_SQL]
    assert SECRET not in response.text and SECRET not in caplog.text


def test_company_route_refuses_an_unapproved_target(monkeypatch):
    server = CompanyServer()
    runtime, client, headers, connects = company_client(monkeypatch, server)
    runtime.company_targets.targets["t1"].approval_state = "needs_reapproval"
    before = len(connects)
    response = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **company_login()})
    assert response.status_code == 409 and response.json()["error_code"] == "company_target_not_approved"
    assert len(connects) == before


def test_company_route_refuses_unknown_target(monkeypatch):
    _, client, headers, connects = company_client(monkeypatch, CompanyServer(), discover=False)
    response = client.post("/company/targets/nope/observed-performance", headers=headers,
                           json={"database": DB, **company_login()})
    assert response.status_code == 404 and connects == []


@pytest.mark.parametrize("headers_change", [
    {"Authorization": "Bearer not-a-session-token-at-all-0000000000"},
    {"Authorization": ""},
    {"Origin": "https://evil.example"},
])
def test_company_route_refuses_an_invalid_pairing_or_origin(monkeypatch, headers_change):
    _, client, headers, connects = company_client(monkeypatch, CompanyServer(), discover=False)
    response = client.post(COMPANY_PATH, headers={**headers, **headers_change},
                           json={"database": DB, **company_login()})
    assert response.status_code == 401 and connects == []


def test_company_route_refuses_an_undiscovered_or_foreign_database(monkeypatch):
    server = CompanyServer()
    _, client, headers, connects = company_client(monkeypatch, server, discover=False)
    undiscovered = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **company_login()})
    assert undiscovered.status_code == 409 and connects == []
    assert client.post("/company/targets/t1/databases", headers=headers, json=company_login()).status_code == 200
    before = len(connects)
    foreign = client.post(COMPANY_PATH, headers=headers,
                          json={"database": DB, **company_login("other_user")})
    not_listed = client.post(COMPANY_PATH, headers=headers, json={"database": "payroll", **company_login()})
    assert foreign.status_code == 409 and not_listed.status_code == 409
    assert len(connects) == before


@pytest.mark.parametrize("body_change", [
    {"sql": "SELECT 1"}, {"query": "SELECT 1"}, {"database": None}, {"host": "other.example"},
])
def test_company_route_accepts_no_sql_or_destination(monkeypatch, body_change):
    _, client, headers, connects = company_client(monkeypatch, CompanyServer(), discover=False)
    body = {"database": DB, **company_login(), **body_change}
    response = client.post(COMPANY_PATH, headers=headers, json=body)
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_observed_performance_request"
    assert connects == []


def test_company_route_connection_failure_is_a_safe_error(monkeypatch):
    runtime, client, headers, _ = company_client(monkeypatch, CompanyServer())

    def refuse(destination, **kwargs):
        raise ValueError("database_connection_failed")

    monkeypatch.setattr(runtime, "connect_company_target_mysql", refuse)
    response = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **company_login()})
    assert response.status_code == 400 and response.json()["error_code"] == "database_connection_failed"
    assert SECRET not in response.text


def test_company_route_fails_closed_without_a_read_only_session(monkeypatch):
    server = CompanyServer()
    runtime, client, headers, _ = company_client(monkeypatch, server)

    class NotReadOnly(CompanyServer):
        def rows_for(self, sql, args):
            if sql == READ_ONLY_CHECK_SQL:
                self.executed.append((sql, args))
                return [{"transaction_read_only": 0}]
            return super().rows_for(sql, args)

    stubborn = NotReadOnly()
    monkeypatch.setattr(runtime, "connect_company_target_mysql",
                        lambda destination, **kwargs: Connection(stubborn))
    response = client.post(COMPANY_PATH, headers=headers, json={"database": DB, **company_login()})
    assert response.status_code == 400 and response.json()["error_code"] == "database_connection_failed"
    assert not any(sql in op.FIXED_SQL for sql, _ in stubborn.executed)


def test_read_only_wrapper_is_what_the_collector_receives(monkeypatch):
    # The connector hands the collector the confirmed ReadOnlyEvidenceConnection.
    received = []
    runtime, client, headers, _ = company_client(monkeypatch, CompanyServer())
    import backend.observed_performance as module
    original = module.collect_observed_performance

    def spy(connection, schema):
        received.append(type(connection).__name__)
        return original(connection, schema)

    monkeypatch.setattr(module, "collect_observed_performance", spy)
    assert client.post(COMPANY_PATH, headers=headers, json={"database": DB, **company_login()}).status_code == 200
    assert received == [type(enforce_read_only_session(Connection(CompanyServer()))).__name__]
