"""Local /api/validate evidence uses only the login supplied with that request.

Contract (local loopback targets only):
- one evidence connection per request, opened with the request's login and bound to
  the selected database, reused by EXPLAIN and metadata, closed exactly once;
- the session is set to read-only transactions before any evidence query
  (defense in depth); if that fails, no evidence is collected;
- only EXPLAIN of one supported statement, SELECT DATABASE() and the fixed
  information_schema queries run; never the submitted SQL, never EXPLAIN ANALYZE;
- no other credential source: no login means "not_configured", a rejected login is
  reported as such, and .env values are never used;
- failures map to a small, stable status set, with no raw MySQL text or credentials;
- remote hosts, invalid targets and `mode` behave exactly as before.

Everything here uses fake credentials and a fake MySQL; nothing reaches a server.
"""

from __future__ import annotations

import logging

import pymysql
import pytest
from fastapi.testclient import TestClient

import backend.db as backend_db
import backend.main as backend_main
from backend.db import EVIDENCE_STATUSES, READ_ONLY_SESSION_SQL
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

    def rows_for(self, sql, args):
        self.executed.append(sql.strip())
        text = sql.strip()
        if text == READ_ONLY_SESSION_SQL:
            if self.set_error is not None:
                raise self.set_error
            return []
        if text.startswith("EXPLAIN"):
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
    def __init__(self, server):
        self.server, self.rows = server, []

    def execute(self, sql, args=None):
        self.rows = self.server.rows_for(sql, args)

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

    def cursor(self):
        return FakeCursor(self.server)

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
    assert response.json()["statements"][0]["database_evidence"]["status"] == "available"
    assert_no_secrets(response.text)


def test_metadata_uses_the_selected_database_on_the_same_connection(mysql, monkeypatch):
    seen = []
    real = backend_main.collect_metadata

    def spy(tables, connection=None, database=None, **kwargs):
        seen.append((connection, database))
        return real(tables, connection=connection, database=database, **kwargs)

    monkeypatch.setattr(backend_main, "collect_metadata", spy)
    post("UPDATE users SET active = 0 WHERE status = 'old'", database="sakila")
    assert seen == [(mysql.connections[0], "sakila")]
    assert mysql.connect_calls[0]["database"] == "sakila"


def test_one_connection_per_request_reused_and_closed_exactly_once(mysql):
    response = post("SELECT * FROM users WHERE id = 1; UPDATE users SET active = 0 WHERE id = 2;")
    assert response.status_code == 200 and response.json()["statement_count"] == 2
    assert len(mysql.connect_calls) == 1 and len(mysql.connections) == 1
    assert mysql.connections[0].close_calls == 1
    assert sum(sql.startswith("EXPLAIN ") for sql in mysql.executed) == 2


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
    (mysql_error(1142, "DELETE command denied to user"), "insufficient_privileges"),
    (mysql_error(1143, "SELECT command denied for column"), "insufficient_privileges"),
    (mysql_error(1064, "You have an error in your SQL syntax"), "explain_failed"),
    (RuntimeError("driver hiccup"), "explain_failed"),
])
def test_explain_failures_map_to_safe_statuses(mysql, error, expected):
    mysql.explain_error = error
    response = post("DELETE FROM users WHERE id = 5")
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


def test_evidence_and_m1_m2_apply_with_a_working_login(mysql):
    mysql.table_rows = 250_000
    statement = post("UPDATE users SET active = 0 WHERE status = 'old'").json()["statements"][0]
    labels = {factor["label"]: factor["points"] for factor in statement["evidence_factors"]}
    assert labels == {"EXPLAIN indicates a full table scan": 10}
    assert statement["metadata_status"] == "available"
    assert "M1_TABLE_ROWS_FULL_SCAN_WRITE" in {factor["code"] for factor in statement["metadata_factors"]}
    assert statement["metadata_score_adjustment"] >= 5
    assert statement["score"] == statement["static_score"] + 10 + statement["metadata_score_adjustment"]


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
