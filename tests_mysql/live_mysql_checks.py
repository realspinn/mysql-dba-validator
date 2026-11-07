"""Local Mode evidence against a real, disposable MySQL 8.4 server (CI only).

Run only by the "MySQL 8.4 integration" job in .github/workflows/ci.yml:

    python -m pytest tests_mysql/live_mysql_checks.py

The file name does not match test_*.py, so a plain `python -m pytest` never
collects it; it runs only when named. Every test fails (never skips) unless the
job's opt-in is present: GITHUB_ACTIONS=true, RUNNER_OS=Linux and
MDV_CI_MYSQL=1. This keeps it away from any MySQL on a developer machine.

The server is the job's service container (mysql:8.4.13 pinned by digest),
reached on 127.0.0.1:3306, the only target Local Mode accepts. Root set it up
with tests_mysql/setup.sql and docs/v2-live-validation/02-fixture.sql (synthetic
rows). These tests use only the accounts that setup created:

- vt_evidence: SELECT on validator_live_test (the validator's login);
- vt_writer: SELECT, INSERT, UPDATE, DELETE there (negative safety test only);
- vt_observer: SELECT on the statement digest table;
- vt_perf: SELECT on validator_live_test and validator_ci_quiet, and column-level
  SELECT on the digest columns the observed-performance collector reads.

The historical schema workload gate tests also use the container's root, only to
change and restore setup_instruments and setup_consumers.

Requests go through the real /api routes, PyMySQL and server (FastAPI
TestClient in-process). Verification level: real MySQL 8.4 server in Docker on
Linux. Not shown: Company Mode, the connector, TLS, RDS, proxies, MySQL 8.0, or
the packaged app.
"""

from __future__ import annotations

import os
import re

import pymysql
import pytest
from fastapi.testclient import TestClient

from backend.main import app

HOST, PORT, DB = "127.0.0.1", 3306, "validator_live_test"
EXPECTED_VERSION = "8.4.13"
# Test-only passwords of the disposable container (tests_mysql/setup.sql).
EVIDENCE = ("vt_evidence", "ci-only-evidence-pw-1")
WRITER = ("vt_writer", "ci-only-writer-pw-1")
OBSERVER = ("vt_observer", "ci-only-observer-pw-1")
SECRETS = (EVIDENCE[1], WRITER[1], OBSERVER[1])

client = TestClient(app, base_url="http://127.0.0.1:8420")


@pytest.fixture(scope="session", autouse=True)
def disposable_ci_server():
    """Fail unless this is the CI job, and the server is the pinned 8.4 container."""
    opted_in = (os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("RUNNER_OS") == "Linux"
                and os.environ.get("MDV_CI_MYSQL") == "1")
    if not opted_in:
        pytest.fail("CI-only: needs GITHUB_ACTIONS=true, RUNNER_OS=Linux and MDV_CI_MYSQL=1 "
                    "(the MySQL 8.4 integration job in ci.yml)", pytrace=False)
    with _connect(OBSERVER) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT @@version AS version")
        assert cursor.fetchone()["version"] == EXPECTED_VERSION


def _connect(account):
    """A direct harness connection (no default database), separate from the validator's."""
    user, password = account
    return pymysql.connect(host=HOST, port=PORT, user=user, password=password,
                           connect_timeout=5, cursorclass=pymysql.cursors.DictCursor)


def _login(account):
    return {"host": HOST, "port": PORT, "username": account[0], "password": account[1]}


def validate(sql, account=EVIDENCE, database=DB):
    response = client.post("/api/validate", json={"sql": sql, "database": database, **_login(account)})
    assert response.status_code == 200, response.status_code
    for secret in SECRETS:
        assert secret not in response.text
    return response.json()


def only(data):
    assert len(data["statements"]) == 1, data
    return data["statements"][0]


def table_state():
    """Every row of the fixture tables, read directly (not through the validator)."""
    with _connect(EVIDENCE) as connection, connection.cursor() as cursor:
        state = {}
        for table in ("users", "users_fx", "audit_log"):
            cursor.execute(f"SELECT * FROM {DB}.{table} ORDER BY id")
            state[table] = list(cursor.fetchall())  # PyMySQL returns () for no rows
        return state


def digest_counts():
    """COUNT_STAR per statement digest run with the test schema as default (plus overflow)."""
    with _connect(OBSERVER) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT DIGEST, DIGEST_TEXT, COUNT_STAR FROM performance_schema.events_statements_summary_by_digest "
            "WHERE SCHEMA_NAME = %s OR DIGEST IS NULL", (DB,))
        return {(row["DIGEST"], row["DIGEST_TEXT"]): row["COUNT_STAR"] for row in cursor.fetchall()}


def grown_digests(before, after):
    return [text or "" for (digest, text), count in after.items() if count > before.get((digest, text), 0)]


# ---------------------------------------------------------------- connection and discovery


def test_connection_test_and_discovery_use_the_real_server():
    assert client.post("/api/connections/test", json=_login(EVIDENCE)).json() == {
        "status": "success", "connected": True}
    discovered = client.post("/api/connections/discover-databases", json=_login(EVIDENCE)).json()
    assert discovered["status"] == "success"
    assert DB in discovered["databases"]
    # SHOW DATABASES lists only schemas the account has a privilege on.
    assert "validator_ci_private" not in discovered["databases"]


def test_wrong_password_is_a_safe_error():
    wrong = "ci-only-wrong-pw-1"
    response = client.post("/api/connections/test", json={**_login(EVIDENCE), "password": wrong})
    assert response.json()["error_code"] == "connection_failed"
    assert wrong not in response.text
    statement = only(validate("SELECT * FROM users WHERE id = 1", account=(EVIDENCE[0], wrong)))
    assert statement["database_evidence"]["status"] == "auth_failed"
    assert statement["evidence"]["connection"]["state"] == "failed"


def test_database_without_privilege_is_reported_not_collected():
    statement = only(validate("SELECT * FROM hidden WHERE id = 1", database="validator_ci_private"))
    assert statement["database_evidence"]["status"] == "insufficient_privileges"  # MySQL error 1044
    assert statement["evidence"]["connection"]["state"] == "failed"
    assert statement["evidence"]["overall"] == "unavailable"


# ---------------------------------------------------------------- read-only session and plans


def test_select_gets_a_plan_on_a_confirmed_read_only_session():
    # Evidence exists only after MySQL read back transaction_read_only = 1
    # (backend.db.enforce_read_only_session fails closed otherwise).
    statement = only(validate("SELECT * FROM users WHERE id = 1"))
    assert statement["evidence"]["connection"]["state"] == "connected"
    assert statement["evidence"]["plan"]["state"] == "collected"
    assert statement["database_evidence"]["access_type"] == "const"
    # An optimizer estimate from plain EXPLAIN, not a count of rows read or changed.
    assert statement["evidence"]["metadata"]["tables"] == [{"name": "users", "found": True}]


def test_missing_table_is_reported_and_caps_confidence():
    statement = only(validate("SELECT * FROM vt_missing_table WHERE id = 1"))
    assert statement["evidence"]["metadata"]["tables"] == [{"name": "vt_missing_table", "found": False}]
    assert statement["confidence"] == "LIMITED"
    assert statement["evidence"]["plan"]["state"] != "collected"


# ---------------------------------------------------------------- key evidence and row scope (plan case 5)


@pytest.mark.parametrize("sql, index", [
    ("UPDATE users SET status = 'x' WHERE id = 1", "PRIMARY"),
    ("DELETE FROM users WHERE tenant_id = 1 AND ref = 'a'", "uq_tenant_ref"),
    ("DELETE FROM users WHERE email = 'a@example.test'", "uq_email"),
    ("DELETE FROM users WHERE code = 'c01-0000001'", "uq_code_prefix"),
])
def test_unique_key_verified_by_the_server_bounds_the_write(sql, index):
    statement = only(validate(sql))
    assert statement["scope"]["basis"] == "at_most_one_row", statement["scope"]
    assert statement["scope"]["index"] == index
    assert statement["scope"]["factor"]["medium_floor_applied"] is True
    assert statement["scope"]["indirect_effects"] == "not_checked"
    assert (statement["score"], statement["risk_level"], statement["confidence"]) == (20, "MEDIUM", "HIGH")


@pytest.mark.parametrize("sql", [
    "DELETE FROM users WHERE tenant_id = 1",              # part of a composite key
    "DELETE FROM users WHERE email IS NULL",              # a unique key allows many NULLs
    "DELETE FROM users WHERE customer_id = 10",           # non-unique index
    "DELETE FROM users WHERE id = '1'",                   # string literal on an INT key
    "DELETE FROM users WHERE LOWER(email) = 'a@example.test'",
    "DELETE FROM users_fx WHERE id = 1",                  # table with a functional key part
])
def test_scope_not_established_without_a_verified_key(sql):
    statement = only(validate(sql))
    assert statement["scope"]["basis"] == "not_established", statement["scope"]
    assert (statement["score"], statement["risk_level"], statement["confidence"]) == (25, "MEDIUM", "LIMITED")


# ---------------------------------------------------------------- negative safety test (write-capable login)


WRITES = [
    "UPDATE users SET status = 'x' WHERE id = 1",
    "DELETE FROM users WHERE id = 1",
    "DELETE FROM users",
    "INSERT INTO audit_log (note) VALUES ('submitted')",
    "SELECT * FROM users WHERE id = 1; DELETE FROM users WHERE tenant_id = 1",
]
WRITE_DIGEST = re.compile(r"^\s*(EXPLAIN\s+)?(INSERT|UPDATE|DELETE|REPLACE)\b", re.IGNORECASE)


def test_submitted_writes_are_not_executed_even_when_the_login_could_write():
    before_state, before_digests = table_state(), digest_counts()
    assert len(before_state["users"]) == 3 and before_state["audit_log"] == []

    for sql in WRITES:
        for statement in validate(sql, account=WRITER)["statements"]:
            # The validator really connected with the write-capable login.
            assert statement["evidence"]["connection"]["state"] == "connected", sql

    after_digests = digest_counts()
    # Primary evidence: nothing changed.
    assert table_state() == before_state
    # Instrumentation check: the digests did record the validator's own session
    # statements, so their contents can be trusted for this run, and none overflowed.
    grown = grown_digests(before_digests, after_digests)
    assert any(re.match(r"^SET SESSION TRANSACTION READ ONLY", text) for text in grown), grown
    assert not any(digest is None and count > before_digests.get((digest, text), 0)
                   for (digest, text), count in after_digests.items()), "digest table overflowed"
    # Secondary evidence: no write, write plan or ANALYZE reached the server.
    assert not [text for text in grown if WRITE_DIGEST.match(text) or "ANALYZE" in text.upper()], grown


# ---------------------------------------------------------------- historical schema workload gate
#
# Direct checks (not through the app's routes) of the Performance Schema reads the
# observed-performance collector depends on. The instrumentation tests change
# setup_instruments or setup_consumers on this disposable container only, as root,
# and restore the default in a finally block.

ROOT = ("root", "ci-only-root-pw-1")  # the container's root (ci.yml MYSQL_ROOT_PASSWORD)
PERF = ("vt_perf", "ci-only-perf-pw-1")
QUIET_DB = "validator_ci_quiet"
PERF_COLUMNS = ("DIGEST, DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT, AVG_TIMER_WAIT, MAX_TIMER_WAIT, "
                "QUANTILE_95, SUM_LOCK_TIME, SUM_ROWS_EXAMINED, SUM_ROWS_SENT, SUM_ROWS_AFFECTED, "
                "SUM_NO_INDEX_USED, FIRST_SEEN, LAST_SEEN")
DIGEST_TABLE = "performance_schema.events_statements_summary_by_digest"


def _connect_db(account, database):
    user, password = account
    return pymysql.connect(host=HOST, port=PORT, user=user, password=password, database=database,
                           connect_timeout=5, cursorclass=pymysql.cursors.DictCursor)


def _errno(cursor, sql):
    try:
        cursor.execute(sql)
    except pymysql.MySQLError as exc:
        return exc.args[0]
    return None


def _digest_row(statement):
    """The digest summary row of one statement text in the test schema, read as vt_perf."""
    # STATEMENT_DIGEST() resolves the unqualified table against the default database.
    with _connect_db(PERF, DB) as connection, connection.cursor() as cursor:
        cursor.execute(f"SELECT {PERF_COLUMNS} FROM {DIGEST_TABLE} "
                       "WHERE SCHEMA_NAME = %s AND DIGEST = STATEMENT_DIGEST(%s)", (DB, statement))
        return cursor.fetchone()


def _run(statement, times):
    """Harness workload as vt_evidence with the test schema as default (not via the app)."""
    with _connect_db(EVIDENCE, DB) as connection, connection.cursor() as cursor:
        for _ in range(times):
            cursor.execute(statement)
            cursor.fetchall()


def _report(capsys, label, value):
    with capsys.disabled():
        print(f"\n[perf-gate] {label}: {value}")


def test_perf_gate_column_grant_reads_metrics_but_not_samples(capsys):
    with _connect(PERF) as connection, connection.cursor() as cursor:
        cursor.execute("SHOW GRANTS")
        _report(capsys, "vt_perf grants", [next(iter(row.values())) for row in cursor.fetchall()])
        cursor.execute(f"SELECT {PERF_COLUMNS} FROM {DIGEST_TABLE} WHERE SCHEMA_NAME = %s "
                       "AND DIGEST IS NOT NULL ORDER BY SUM_TIMER_WAIT DESC LIMIT 6", (DB,))
        cursor.fetchall()
        cursor.execute("SELECT NAME, ENABLED FROM performance_schema.setup_consumers "
                       "WHERE NAME IN ('global_instrumentation', 'statements_digest')")
        consumers = {row["NAME"]: row["ENABLED"] for row in cursor.fetchall()}
        _report(capsys, "consumers", consumers)
        assert consumers == {"global_instrumentation": "YES", "statements_digest": "YES"}
        denied = {column: _errno(cursor, f"SELECT {column} FROM {DIGEST_TABLE} LIMIT 1")
                  for column in ("QUERY_SAMPLE_TEXT", "*", "SUM_CPU_TIME", "MIN_TIMER_WAIT")}
        denied["events_statements_history"] = _errno(
            cursor, "SELECT SQL_TEXT FROM performance_schema.events_statements_history LIMIT 1")
        _report(capsys, "vt_perf denied errnos", denied)
        # A named ungranted column is 1143 (column access denied); * is 1142 (the
        # account has no table-level SELECT).
        assert denied == {"QUERY_SAMPLE_TEXT": 1143, "*": 1142, "SUM_CPU_TIME": 1143,
                          "MIN_TIMER_WAIT": 1143, "events_statements_history": 1142}
    with _connect(EVIDENCE) as connection, connection.cursor() as cursor:
        no_grant = _errno(cursor, f"SELECT COUNT_STAR FROM {DIGEST_TABLE} LIMIT 1")
        cursor.execute("SELECT @@GLOBAL.performance_schema AS performance_schema")
        enabled = cursor.fetchone()["performance_schema"]
        _report(capsys, "vt_evidence digest errno, @@GLOBAL.performance_schema", (no_grant, enabled))
        assert no_grant == 1142 and enabled == 1


def test_perf_gate_reads_on_the_validators_read_only_session(capsys):
    from backend.db import READ_ONLY_CHECK_SQL, ConnectionProfile, open_evidence_connection
    connection, status = open_evidence_connection(
        ConnectionProfile(host=HOST, port=PORT, username=PERF[0], password=PERF[1]), DB)
    assert status is None and connection is not None
    try:
        cursor = connection.cursor()
        cursor.execute(READ_ONLY_CHECK_SQL)
        assert cursor.fetchone()["transaction_read_only"] == 1
        cursor.execute(f"SELECT {PERF_COLUMNS} FROM {DIGEST_TABLE} WHERE SCHEMA_NAME = %s "
                       "AND DIGEST IS NOT NULL ORDER BY SUM_TIMER_WAIT DESC, COUNT_STAR DESC, DIGEST "
                       "LIMIT 6", (DB,))
        rows = cursor.fetchmany(7)
        cursor.execute(f"SELECT COUNT_STAR FROM {DIGEST_TABLE} WHERE DIGEST IS NULL")
        overflow = cursor.fetchall()
        cursor.close()
    finally:
        connection.close()
    _report(capsys, "read-only session rows, overflow", (len(rows), overflow))
    _report(capsys, "column types", {key: type(value).__name__ for key, value in rows[0].items()})
    assert 1 <= len(rows) <= 6


def test_perf_gate_timer_units_and_counters(capsys):
    import time
    statement = "SELECT id FROM users WHERE customer_id = 10"
    before = _digest_row(statement) or {}
    started = time.perf_counter()
    _run(statement, 25)
    wall_seconds = time.perf_counter() - started
    after = _digest_row(statement)
    _report(capsys, "workload row", after)
    count = after["COUNT_STAR"] - before.get("COUNT_STAR", 0)
    total_ps = after["SUM_TIMER_WAIT"] - before.get("SUM_TIMER_WAIT", 0)
    _report(capsys, "wall seconds, server seconds (ps / 1e12)", (wall_seconds, total_ps / 1e12))
    assert count == 25
    assert after["SUM_ROWS_SENT"] - before.get("SUM_ROWS_SENT", 0) == 50  # ids 1 and 2
    assert after["SUM_ROWS_AFFECTED"] == 0
    # Picoseconds: 25 sequential statements took no longer than the client's wall clock.
    assert 0 < total_ps / 1e12 <= wall_seconds
    assert 0 < after["AVG_TIMER_WAIT"] <= after["MAX_TIMER_WAIT"]
    assert after["QUANTILE_95"] > 0
    assert after["FIRST_SEEN"] <= after["LAST_SEEN"]


def test_perf_gate_untimed_statements_still_count(capsys):
    statement = "SELECT id FROM users WHERE tenant_id = 2"
    with _connect(ROOT) as root, root.cursor() as cursor:
        cursor.execute("SELECT TIMED FROM performance_schema.setup_instruments "
                       "WHERE NAME = 'statement/sql/select'")
        assert cursor.fetchone()["TIMED"] == "YES"
        cursor.execute("UPDATE performance_schema.setup_instruments SET TIMED = 'NO' "
                       "WHERE NAME = 'statement/sql/select'")
        try:
            _run(statement, 10)
        finally:
            cursor.execute("UPDATE performance_schema.setup_instruments SET TIMED = 'YES' "
                           "WHERE NAME = 'statement/sql/select'")
    row = _digest_row(statement)
    _report(capsys, "untimed row", row)
    assert row["COUNT_STAR"] == 10
    assert row["SUM_TIMER_WAIT"] == 0 and row["MAX_TIMER_WAIT"] == 0


def test_perf_gate_disabled_digest_consumer_records_nothing(capsys):
    statement = "SELECT id FROM users WHERE tenant_id = 1 AND status = 'open'"
    with _connect(ROOT) as root, root.cursor() as cursor:
        cursor.execute("UPDATE performance_schema.setup_consumers SET ENABLED = 'NO' "
                       "WHERE NAME = 'statements_digest'")
        try:
            _run(statement, 5)
            with _connect(PERF) as perf, perf.cursor() as perf_cursor:
                perf_cursor.execute("SELECT ENABLED FROM performance_schema.setup_consumers "
                                    "WHERE NAME = 'statements_digest'")
                seen = perf_cursor.fetchone()["ENABLED"]
        finally:
            cursor.execute("UPDATE performance_schema.setup_consumers SET ENABLED = 'YES' "
                           "WHERE NAME = 'statements_digest'")
    row = _digest_row(statement)
    _report(capsys, "consumer seen by vt_perf, row while disabled", (seen, row))
    assert seen == "NO" and row is None


def test_perf_gate_quiet_schema_shows_only_the_validators_own_session(capsys):
    from backend.db import ConnectionProfile, open_evidence_connection
    with _connect(ROOT) as root, root.cursor() as cursor:
        cursor.execute(f"SELECT COUNT(*) AS n FROM {DIGEST_TABLE} WHERE SCHEMA_NAME = %s", (QUIET_DB,))
        assert cursor.fetchone()["n"] == 0
    connection, status = open_evidence_connection(
        ConnectionProfile(host=HOST, port=PORT, username=PERF[0], password=PERF[1]), QUIET_DB)
    assert status is None
    try:
        cursor = connection.cursor()
        cursor.execute(f"SELECT DIGEST_TEXT, COUNT_STAR FROM {DIGEST_TABLE} WHERE SCHEMA_NAME = %s "
                       "AND DIGEST IS NOT NULL", (QUIET_DB,))
        rows = cursor.fetchall()
        cursor.close()
    finally:
        connection.close()
    _report(capsys, "quiet schema rows", rows)
    texts = [row["DIGEST_TEXT"] for row in rows]
    # PyMySQL's SET NAMES on connect, then the read-only setting and its read-back.
    assert all(text.startswith(("SET NAMES", "SET SESSION TRANSACTION READ ONLY", "SELECT @@SESSION"))
               for text in texts), texts
