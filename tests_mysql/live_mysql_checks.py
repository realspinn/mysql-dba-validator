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
- vt_observer: SELECT on the statement digest table.

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
