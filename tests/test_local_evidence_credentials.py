"""Local /api/validate evidence uses only the login supplied with that request.

Contract (local loopback targets only):
- one evidence connection per request, opened with the request's login and bound to
  the selected database, reused by EXPLAIN and metadata, closed exactly once;
- the session is set to read-only transactions before any evidence query
  (defense in depth); if that fails, no evidence is collected;
- only EXPLAIN of one verified plain SELECT, SELECT DATABASE() and the fixed
  information_schema queries run; never the submitted SQL, never EXPLAIN ANALYZE;
- UPDATE/DELETE get no plan request on the read-only connection (MySQL refuses it
  with error 1792) and report "plan_not_collected_read_only"; metadata still runs;
- no other credential source: no login means "not_configured", a rejected login is
  reported as such, and .env values are never used;
- failures map to a small, stable status set, with no raw MySQL text or credentials;
- remote hosts, invalid targets and `mode` behave exactly as before.

Everything here uses fake credentials and a fake MySQL; nothing reaches a server.
"""

from __future__ import annotations

import logging
import re

import pymysql
import pytest
from fastapi.testclient import TestClient

import backend.db as backend_db
import backend.main as backend_main
from backend.db import EVIDENCE_STATUSES, READ_ONLY_SESSION_SQL, ReadOnlyEvidenceConnection
from backend.main import app

USER = "fake_local_user"
PASSWORD = "Fake-Local-Password-0731"
DB = "app"
DECOY = {
    "MYSQL_HOST": "decoy-host.invalid",
    "MYSQL_PORT": "3399",
    "MYSQL_USER": "decoy_env_user",
    "MYSQL_PASSWORD": "Decoy-Env-Password-5150",
    "MYSQL_DATABASE": "decoy_env_db",
}
client = TestClient(app)


def mysql_error(errno, message):
    return pymysql.err.OperationalError(errno, message)


# Statements MySQL refuses to EXPLAIN in a read-only session (error 1792), as observed
# against a real server for v0.3.0.
READ_ONLY_REFUSED = re.compile(r"EXPLAIN\s+(WITH\b.*\)\s*)?(UPDATE|DELETE|INSERT|REPLACE)\b", re.I | re.S)


class FakeMySQL:
    """Records connections and statements; answers the fixed evidence queries."""

    def __init__(self, *, connect_error=None, set_error=None, explain=None, explain_error=None,
                 table_rows=100, metadata_error=None):
        self.connect_error = connect_error
        self.set_error = set_error
        self.explain = explain if explain is not None else [explain_row("ALL", 500, extra="Using where")]
        self.explain_error = explain_error
        self.table_rows = table_rows
        self.metadata_error = metadata_error
        self.connect_calls = []
        self.executed = []
        self.connections = []

    def connect(self, **kwargs):
        self.connect_calls.append(kwargs)
        if self.connect_error is not None:
            raise self.connect_error
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection

    def rows_for(self, sql, args, connection):
        self.executed.append(sql.strip())
        text = sql.strip()
        if text == READ_ONLY_SESSION_SQL:
            if self.set_error is not None:
                raise self.set_error
            connection.read_only = True
            return []
        if text.startswith("EXPLAIN"):
            if connection.read_only and READ_ONLY_REFUSED.match(text):
                # Real MySQL: EXPLAIN of a write is refused in a read-only session.
                raise mysql_error(1792, "Cannot execute statement in a READ ONLY transaction.")
            if self.explain_error is not None:
                raise self.explain_error
            return [dict(row) for row in self.explain]
        if text.startswith("SELECT DATABASE()"):
            return [{"db_name": DB}]
        if "information_schema" in text:
            if self.metadata_error is not None:
                raise self.metadata_error
            if "information_schema.TABLES" in text:
                return [{"ENGINE": "InnoDB", "TABLE_ROWS": self.table_rows, "DATA_LENGTH": 16384,
                         "INDEX_LENGTH": 0, "CREATE_OPTIONS": "", "UPDATE_TIME": None}]
            if "information_schema.STATISTICS" in text:
                return [{"INDEX_NAME": "PRIMARY", "COLUMN_NAME": "id", "SEQ_IN_INDEX": 1, "NON_UNIQUE": 0,
                         "CARDINALITY": 10, "SUB_PART": None, "INDEX_TYPE": "BTREE"}]
            if "information_schema.COLUMNS" in text:
                return [{"COLUMN_NAME": "id", "DATA_TYPE": "int", "IS_NULLABLE": "NO", "COLUMN_KEY": "PRI",
                         "ORDINAL_POSITION": 1}]
        raise AssertionError(f"unexpected query: {text[:60]}")


class FakeCursor:
    def __init__(self, server, connection):
        self.server, self.connection, self.rows = server, connection, []

    def execute(self, sql, args=None):
        self.rows = self.server.rows_for(sql, args, self.connection)

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
        self.close_calls = 0
        self.read_only = False

    def cursor(self):
        return FakeCursor(self.server, self)

    def close(self):
        self.close_calls += 1


def explain_row(access_type, rows, key=None, extra=None):
    return {"id": 1, "select_type": "SIMPLE", "table": "users", "partitions": None, "type": access_type,
            "possible_keys": key, "key": key, "key_len": "4" if key else None, "ref": None, "rows": rows,
            "filtered": 100.0, "Extra": extra}


@pytest.fixture
def mysql(monkeypatch):
    """A fake MySQL behind pymysql.connect, with decoy .env values that must never be used."""
    server = FakeMySQL()
    for key, value in DECOY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(backend_db, "_client", None)
    monkeypatch.setattr(pymysql, "connect", lambda **kwargs: server.connect(**kwargs))
    return server


def post(sql, **overrides):
    body = {"sql": sql, "host": "127.0.0.1", "port": 3306, "username": USER, "password": PASSWORD,
            "database": DB, **overrides}
    body = {key: value for key, value in body.items() if value is not None}
    return client.post("/api/validate", json=body)


def assert_no_secrets(text):
    for secret in (PASSWORD, USER, *DECOY.values()):
        assert secret not in text, secret


ALLOWED_PREFIXES = (READ_ONLY_SESSION_SQL, "EXPLAIN ", "SELECT DATABASE()", "SELECT ENGINE",
                    "SELECT INDEX_NAME", "SELECT COLUMN_NAME")


def assert_only_evidence_queries(server):
    for sql in server.executed:
        assert sql.startswith(ALLOWED_PREFIXES), sql
        assert "ANALYZE" not in sql.upper(), sql
    for sql in server.executed:
        if sql.startswith("EXPLAIN "):
            # one statement per EXPLAIN: no embedded statement separators
            assert ";" not in sql.rstrip(";"), sql


# ---------------------------------------------------------------- credentials and binding


def test_typed_login_and_selected_database_are_used_never_env(mysql):
    response = post("UPDATE users SET active = 0 WHERE status = 'old'")
    assert response.status_code == 200
    assert len(mysql.connect_calls) == 1
    kwargs = mysql.connect_calls[0]
    assert (kwargs["host"], kwargs["port"], kwargs["user"], kwargs["password"], kwargs["database"]) == (
        "127.0.0.1", 3306, USER, PASSWORD, DB)
    assert not set(DECOY.values()) & {str(value) for value in kwargs.values()}
    assert response.json()["statements"][0]["database_evidence"]["status"] == "plan_not_collected_read_only"
    assert_no_secrets(response.text)


def test_metadata_uses_the_selected_database_on_the_same_connection(mysql, monkeypatch):
    seen = []
    real = backend_main.collect_metadata

    def spy(tables, connection=None, database=None, **kwargs):
        seen.append((connection, database))
        return real(tables, connection=connection, database=database, **kwargs)

    monkeypatch.setattr(backend_main, "collect_metadata", spy)
    post("UPDATE users SET active = 0 WHERE status = 'old'", database="sakila")
    assert len(seen) == 1 and seen[0][1] == "sakila"
    assert isinstance(seen[0][0], ReadOnlyEvidenceConnection)
    assert seen[0][0]._connection is mysql.connections[0]
    assert mysql.connect_calls[0]["database"] == "sakila"


def test_one_connection_per_request_reused_and_closed_exactly_once(mysql):
    response = post("SELECT * FROM users WHERE id = 1; UPDATE users SET active = 0 WHERE id = 2;")
    assert response.status_code == 200 and response.json()["statement_count"] == 2
    assert len(mysql.connect_calls) == 1 and len(mysql.connections) == 1
    assert mysql.connections[0].close_calls == 1
    # Only the SELECT is planned; the UPDATE gets no plan request.
    assert [sql for sql in mysql.executed if sql.startswith("EXPLAIN ")] == [
        "EXPLAIN SELECT * FROM users WHERE id = 1;"]


def test_read_only_session_is_set_before_any_evidence_query(mysql):
    post("DELETE FROM users WHERE id = 5")
    assert mysql.executed[0] == READ_ONLY_SESSION_SQL
    assert mysql.executed.count(READ_ONLY_SESSION_SQL) == 1


def test_read_only_session_failure_fails_closed(mysql):
    mysql.set_error = mysql_error(1227, "Access denied; you need the SUPER privilege")
    response = post("DELETE FROM users WHERE id = 5")
    data = response.json()
    assert mysql.executed == [READ_ONLY_SESSION_SQL]
    assert mysql.connections[0].close_calls == 1
    assert data["statements"][0]["database_evidence"]["status"] == "collection_failed"
    assert data["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"


def test_connection_closes_even_when_a_collector_raises(mysql, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("collector exploded with Fake-Local-Password-0731")

    monkeypatch.setattr(backend_main, "collect_metadata", boom)
    response = post("UPDATE users SET active = 0 WHERE id = 2")
    assert response.status_code == 200
    assert mysql.connections[0].close_calls == 1
    assert_no_secrets(response.text)


def test_only_approved_evidence_queries_run_never_the_submitted_sql(mysql):
    for sql in ("DELETE FROM users", "UPDATE users SET active = 0", "SELECT * FROM users WHERE id = 1"):
        post(sql)
    assert_only_evidence_queries(mysql)
    assert not any(sql.startswith(("DELETE", "UPDATE", "INSERT", "DROP")) for sql in mysql.executed)
    for kwargs in mysql.connect_calls:
        flags = kwargs.get("client_flag", 0)
        assert not flags & pymysql.constants.CLIENT.MULTI_STATEMENTS


def test_unsupported_statement_type_runs_no_explain(mysql):
    data = post("INSERT INTO users (id) VALUES (1)").json()
    assert data["statements"][0]["database_evidence"]["status"] == "unsupported_statement_type"
    assert not any(sql.startswith("EXPLAIN") for sql in mysql.executed)


# ---------------------------------------------------------------- no login, no fallback


@pytest.mark.parametrize("overrides", [
    {"username": None, "password": None},
    {"username": "", "password": ""},
    {"password": None, "username": None, "database": None},
    {"database": None},
    {"host": None},
], ids=["no_login", "empty_login", "nothing", "no_database", "no_host"])
def test_missing_login_or_database_is_not_configured_and_never_uses_env(mysql, overrides):
    response = post("UPDATE users SET active = 0 WHERE id = 1", **overrides)
    assert response.status_code == 200, response.text
    data = response.json()
    assert mysql.connect_calls == []
    assert data["analysis_mode"] == "STATIC"
    assert data["statements"][0]["database_evidence"]["status"] == "not_configured"
    assert_no_secrets(response.text)


def test_rejected_login_is_reported_and_never_falls_back(mysql):
    mysql.connect_error = mysql_error(1045, f"Access denied for user '{USER}'@'localhost' (using password: YES)")
    response = post("UPDATE users SET active = 0 WHERE id = 1")
    data = response.json()
    assert len(mysql.connect_calls) == 1
    assert data["statements"][0]["database_evidence"]["status"] == "auth_failed"
    assert data["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"
    assert_no_secrets(response.text)
    assert "Access denied" not in response.text


# ---------------------------------------------------------------- status mapping


@pytest.mark.parametrize("errno, expected", [
    (1045, "auth_failed"),
    (1049, "database_not_found"),
    (1044, "insufficient_privileges"),
    (2003, "unreachable"),
    (2005, "unreachable"),
    (2013, "unreachable"),
    (9999, "unreachable"),
])
def test_connection_failures_map_to_safe_statuses(mysql, errno, expected):
    mysql.connect_error = mysql_error(errno, f"server said something about {USER} and {DB}")
    response = post("UPDATE users SET active = 0 WHERE id = 1")
    statement = response.json()["statements"][0]
    assert statement["database_evidence"]["status"] == expected
    assert statement["metadata_status"] == "unavailable"
    assert "server said" not in response.text
    assert_no_secrets(response.text)


@pytest.mark.parametrize("error, expected", [
    (mysql_error(1142, "SELECT command denied to user"), "insufficient_privileges"),
    (mysql_error(1143, "SELECT command denied for column"), "insufficient_privileges"),
    (mysql_error(1064, "You have an error in your SQL syntax"), "explain_failed"),
    (RuntimeError("driver hiccup"), "explain_failed"),
])
def test_explain_failures_map_to_safe_statuses(mysql, error, expected):
    mysql.explain_error = error
    response = post("SELECT * FROM users WHERE id = 5")
    assert response.json()["statements"][0]["database_evidence"]["status"] == expected
    assert "command denied" not in response.text and "driver hiccup" not in response.text


def test_every_reported_status_is_in_the_stable_set(mysql):
    for sql in ("SELECT 1", "DELETE FROM users", "INSERT INTO users VALUES (1)", "UPDATE users SET a = 1"):
        for statement in post(sql).json()["statements"]:
            assert statement["database_evidence"]["status"] in EVIDENCE_STATUSES


# ---------------------------------------------------------------- scoring


def test_static_result_is_unchanged_when_evidence_is_unavailable(mysql):
    sql = "UPDATE users SET active = 0 WHERE status = 'pending'"
    static_only = post(sql, username=None, password=None).json()["statements"][0]
    mysql.connect_error = mysql_error(2003, "Can't connect")
    failed = post(sql).json()["statements"][0]
    for key in ("static_score", "static_risk_level", "factors", "score", "risk_level", "metadata_factors"):
        assert static_only[key] == failed[key], key
    assert failed["score"] == failed["static_score"]


def test_select_evidence_applies_with_a_working_login(mysql):
    statement = post("SELECT * FROM users WHERE status = 'old'").json()["statements"][0]
    assert statement["database_evidence"]["status"] == "available"
    assert statement["database_evidence"]["access_type"] == "ALL"
    assert statement["metadata_status"] == "available"


def test_local_dml_gets_metadata_but_no_plan_and_no_plan_based_adjustment(mysql):
    # M1/M2 need a full-scan EXPLAIN plan, which local UPDATE/DELETE no longer have.
    mysql.table_rows = 250_000
    data = post("UPDATE users SET active = 0 WHERE status = 'old'").json()
    statement = data["statements"][0]
    assert statement["database_evidence"]["status"] == "plan_not_collected_read_only"
    assert statement["evidence_factors"] == [] and statement["metadata_factors"] == []
    assert statement["metadata_status"] == "available"
    assert statement["score"] == statement["static_score"]
    assert data["analysis_mode"] == "STATIC_DATABASE_METADATA"


# ---------------------------------------------------------------- boundaries (no connection ever)


def test_remote_host_with_login_is_refused_without_connecting(mysql):
    response = post("SELECT 1", host="db.company.example")
    assert response.json()["error_code"] == "permission_denied"
    assert mysql.connect_calls == []
    assert_no_secrets(response.text)


def test_localhost_resolving_off_loopback_is_refused_without_connecting(mysql, monkeypatch):
    monkeypatch.setattr(backend_main.socket, "getaddrinfo",
                        lambda *args, **kwargs: [(2, 1, 6, "", ("10.1.2.3", 3306))])
    response = post("SELECT 1", host="localhost")
    assert response.json()["error_code"] == "invalid_target"
    assert mysql.connect_calls == []


def test_loopback_on_another_port_is_refused_without_connecting(mysql):
    response = post("SELECT 1", port=3307)
    assert response.json()["error_code"] == "invalid_target"
    assert mysql.connect_calls == []


def test_invalid_database_identifier_is_refused_without_connecting(mysql):
    response = post("SELECT 1", database="app; DROP TABLE users")
    assert response.json()["error_code"] == "invalid_database"
    assert mysql.connect_calls == []


def test_company_mode_with_login_is_refused_without_connecting(mysql):
    response = post("SELECT 1", mode="company")
    assert response.status_code == 422
    assert mysql.connect_calls == []
    assert_no_secrets(response.text)


def test_credentials_never_reach_logs(mysql, caplog):
    caplog.set_level(logging.DEBUG)
    mysql.connect_error = mysql_error(1045, f"Access denied for user '{USER}'")
    post("UPDATE users SET active = 0 WHERE id = 1")
    mysql.connect_error = None
    post("UPDATE users SET active = 0 WHERE id = 1")
    assert_no_secrets(caplog.text)


# ---------------------------------------------------------------- read-only plan allowlist (v0.3.1)


WRITE_PREFIXES = ("UPDATE", "DELETE", "INSERT", "REPLACE", "CREATE", "DROP", "ALTER", "TRUNCATE",
                  "CALL", "DO ", "SET ", "START", "BEGIN", "COMMIT", "ROLLBACK", "LOCK")


def explains(server):
    return [sql for sql in server.executed if sql.startswith("EXPLAIN")]


def assert_read_only_boundary(server):
    """Read-only first, nothing writable, and only verified plain SELECTs planned."""
    assert server.executed[0] == READ_ONLY_SESSION_SQL
    assert server.executed.count(READ_ONLY_SESSION_SQL) == 1
    assert all(connection.read_only for connection in server.connections)
    for sql in server.executed[1:]:
        assert not sql.upper().startswith(WRITE_PREFIXES), sql
        assert "READ WRITE" not in sql.upper() and "ANALYZE" not in sql.upper(), sql
    for sql in explains(server):
        assert backend_db.validate_readonly_select(sql[len("EXPLAIN "):].rstrip(";")), sql


@pytest.mark.parametrize("sql", [
    "UPDATE users SET last_name = 'TEST' WHERE id = 1",
    "DELETE FROM users WHERE id = 1",
], ids=["update", "delete"])
def test_local_dml_sends_no_plan_request_and_keeps_metadata(mysql, sql):
    response = post(sql)
    data = response.json()
    statement = data["statements"][0]
    assert explains(mysql) == []
    assert not any(executed.startswith(sql.split()[0]) for executed in mysql.executed)
    assert_read_only_boundary(mysql)
    assert any("information_schema" in executed for executed in mysql.executed)
    assert statement["database_evidence"]["status"] == "plan_not_collected_read_only"
    assert statement["database_evidence"]["available"] is False
    assert statement["database_evidence"]["access_type"] is None
    assert statement["database_evidence"]["estimated_rows"] is None
    assert statement["evidence_factors"] == []
    assert statement["metadata_status"] == "available"
    assert data["analysis_mode"] == "STATIC_DATABASE_METADATA"
    assert len(mysql.connect_calls) == 1 and mysql.connections[0].close_calls == 1
    assert_no_secrets(response.text)


def test_local_select_is_still_planned_on_the_read_only_connection(mysql):
    data = post("SELECT * FROM users").json()
    assert explains(mysql) == ["EXPLAIN SELECT * FROM users;"]
    assert_read_only_boundary(mysql)
    assert data["statements"][0]["database_evidence"]["status"] == "available"
    assert data["analysis_mode"] == "DATABASE_EVIDENCE"


@pytest.mark.parametrize("sql", [
    "SELECT * INTO @a FROM users",
    "WITH a AS (SELECT 1 AS id) UPDATE users SET active = 0 WHERE id IN (SELECT id FROM a)",
    "WITH a AS (SELECT 1 AS id) DELETE FROM users WHERE id IN (SELECT id FROM a)",
    "UPDATE users u JOIN orders o ON u.id = o.user_id SET u.active = 0",
    "DELETE u FROM users u JOIN orders o ON u.id = o.user_id",
    "DELETE FROM users USING users JOIN orders ON users.id = orders.user_id",
    "UPDATE LOW_PRIORITY IGNORE users SET active = 0 ORDER BY id LIMIT 1",
    "DELETE LOW_PRIORITY QUICK IGNORE FROM users WHERE id = 1 ORDER BY id LIMIT 1",
    "UPDATE users SET active = 0 WHERE id = 1 /*! , name = 'x' */",
    "UPDATE /*+ SET_VAR(transaction_read_only = OFF) */ users SET active = 0 WHERE id = 1",
    "UPDATE users SET active = 0 WHERE id = 1 -- */ ; DELETE FROM users; /*",
    "INSERT INTO users SELECT * FROM users",
    "REPLACE INTO users (id) VALUES (1)",
    "(SELECT id FROM users) UNION (SELECT id FROM orders)",
])
def test_anything_but_a_verified_plain_select_gets_no_plan_request(mysql, sql):
    data = post(sql).json()
    assert explains(mysql) == []
    assert_read_only_boundary(mysql)
    for statement in data["statements"]:
        assert statement["database_evidence"]["available"] is False
        assert statement["database_evidence"]["status"] in {
            "plan_not_collected_read_only", "plan_not_eligible", "unsupported_statement_type"}
        assert statement["evidence"]["plan"]["state"] in {
            "not_collected_read_only", "not_eligible", "not_supported"}


@pytest.mark.parametrize("sql", [
    "SELECT * FROM users /*!50000 , (SELECT 2) */",
    "SELECT * FROM users -- */ /*!50000 UNION SELECT 2 */",
    "SELECT /*+ SET_VAR(transaction_read_only = OFF) */ * FROM users",
    "SELECT * FROM users WHERE id IN (SELECT user_id FROM orders)",
    "WITH a AS (SELECT 1 AS id) SELECT * FROM users WHERE id IN (SELECT id FROM a)",
])
def test_planned_selects_are_plain_and_carry_no_executable_comment(mysql, sql):
    post(sql)
    assert len(explains(mysql)) == 1
    assert_read_only_boundary(mysql)
    assert not any("/*!" in executed for executed in mysql.executed)


def test_the_gate_checks_the_outgoing_sql_not_the_classification(mysql, monkeypatch):
    """Even if regeneration turned a SELECT into something else, nothing is sent."""
    import backend.db_evidence as db_evidence
    monkeypatch.setattr(db_evidence, "_safe_explain_sql",
                        lambda statement_sql: "EXPLAIN UPDATE users SET active = 0;")
    data = post("SELECT * FROM users WHERE id = 1").json()
    assert explains(mysql) == []
    # A SELECT whose outgoing plan request is not a plain SELECT is "not eligible".
    assert data["statements"][0]["database_evidence"]["status"] == "plan_not_eligible"
    assert data["statements"][0]["evidence"]["plan"]["state"] == "not_eligible"


def test_error_1792_maps_to_read_only_status_without_retry_or_new_connection(mysql):
    mysql.explain_error = mysql_error(1792, "Cannot execute statement in a READ ONLY transaction.")
    response = post("SELECT * FROM users WHERE id = 1")
    statement = response.json()["statements"][0]
    assert statement["database_evidence"]["status"] == "plan_not_collected_read_only"
    assert len(explains(mysql)) == 1
    assert len(mysql.connect_calls) == 1 and mysql.connections[0].close_calls == 1
    assert_read_only_boundary(mysql)
    assert "READ ONLY transaction" not in response.text


def test_error_1792_on_a_non_local_connection_keeps_its_previous_mapping():
    """The connector passes a raw connection; its EXPLAIN error mapping is unchanged."""
    from backend.db_evidence import collect_statement_evidence

    server = FakeMySQL(explain_error=mysql_error(1792, "read only"))
    evidence = collect_statement_evidence("DELETE FROM users WHERE id = 1", connection=FakeConnection(server))
    assert evidence.error == "explain_failed"
    assert explains(server) == ["EXPLAIN DELETE FROM users WHERE id = 1;"]


def test_dml_only_batch_uses_one_read_only_connection_and_one_close(mysql):
    data = post("UPDATE users SET active = 0 WHERE id = 1; DELETE FROM users WHERE id = 2;").json()
    assert data["statement_count"] == 2
    assert len(mysql.connect_calls) == 1 and len(mysql.connections) == 1
    assert mysql.connections[0].close_calls == 1
    assert explains(mysql) == []
    assert_read_only_boundary(mysql)
    assert data["analysis_mode"] == "STATIC_DATABASE_METADATA"


def test_dml_without_metadata_is_still_reported_as_unavailable(mysql):
    mysql.metadata_error = mysql_error(1142, "SELECT command denied")
    data = post("UPDATE users SET active = 0 WHERE id = 1").json()
    assert data["statements"][0]["database_evidence"]["status"] == "plan_not_collected_read_only"
    assert data["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"


def test_validate_never_passes_a_missing_connection_to_the_collectors(mysql, monkeypatch):
    seen = []
    real_evidence, real_metadata = backend_main.collect_statement_evidence, backend_main.collect_metadata

    def evidence_spy(sql, connection=None, **kwargs):
        seen.append(connection)
        return real_evidence(sql, connection=connection, **kwargs)

    def metadata_spy(tables, connection=None, **kwargs):
        seen.append(connection)
        return real_metadata(tables, connection=connection, **kwargs)

    monkeypatch.setattr(backend_main, "collect_statement_evidence", evidence_spy)
    monkeypatch.setattr(backend_main, "collect_metadata", metadata_spy)
    post("SELECT * FROM users WHERE id = 1; UPDATE users SET active = 0 WHERE id = 1;")
    assert len(seen) == 4
    assert all(isinstance(connection, ReadOnlyEvidenceConnection) for connection in seen)


def test_no_safety_off_switch_exists():
    import inspect

    from backend.db_evidence import collect_statement_evidence

    assert list(inspect.signature(collect_statement_evidence).parameters) == ["sql", "connection", "session"]
    assert list(inspect.signature(backend_db.open_evidence_connection).parameters) == [
        "profile", "database", "read_timeout"]
    assert set(vars(ReadOnlyEvidenceConnection)) - {"__module__", "__qualname__", "__doc__", "__dict__",
                                                    "__weakref__", "__firstlineno__",
                                                    "__static_attributes__"} == {
        "__init__", "cursor", "close"}


def test_evidence_code_never_contains_a_writable_session_or_transaction():
    from pathlib import Path

    backend_dir = Path(backend_db.__file__).parent
    for name in ("db.py", "db_evidence.py", "metadata.py", "main.py", "evidence_state.py"):
        source = (backend_dir / name).read_text(encoding="utf-8").upper()
        for forbidden in ("READ WRITE", "START TRANSACTION", "ROLLBACK", "EXPLAIN ANALYZE"):
            assert forbidden not in source, (name, forbidden)


@pytest.mark.parametrize("sql", [
    "UPDATE users SET active = 0 WHERE status = 'pending'",
    "DELETE FROM users WHERE id = 5",
    "UPDATE users SET active = 0",
    "DELETE FROM users",
    "SELECT * FROM users WHERE id = 1",
])
def test_static_scoring_is_identical_with_and_without_the_read_only_connection(mysql, sql):
    static_only = post(sql, username=None, password=None).json()["statements"][0]
    with_login = post(sql).json()["statements"][0]
    for key in ("static_score", "static_risk_level", "factors"):
        assert static_only[key] == with_login[key], key
