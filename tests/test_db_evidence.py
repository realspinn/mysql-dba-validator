from backend.db import (
    AnalysisSession,
    ConnectionProfile,
    DBClient,
    DBConfig,
    validate_readonly_select,
)
from backend.db_evidence import DatabaseEvidence, collect_statement_evidence, parse_explain_rows
from backend.main import EvidenceRequest, ValidateRequest, health, run_evidence, validate
import threading


def test_environment_configuration_adapts_to_connection_profile_and_session(monkeypatch):
    monkeypatch.setenv("MYSQL_HOST", " db.example ")
    monkeypatch.setenv("MYSQL_PORT", "3307")
    monkeypatch.setenv("MYSQL_USER", " validator ")
    monkeypatch.setenv("MYSQL_PASSWORD", " secret-password ")
    monkeypatch.setenv("MYSQL_DATABASE", " app ")

    client = DBClient()

    assert client.session == AnalysisSession(
        ConnectionProfile("db.example", 3307, "validator", "secret-password"),
        "app",
    )
    assert client.session.connection_profile.password == "secret-password"
    assert "secret-password" not in repr(client.config)


def test_analysis_session_database_context_is_separate_from_credentials():
    profile = ConnectionProfile(
        "db.example", 3307, "validator", "secret-password")
    first = AnalysisSession(profile, "first_database")
    second = first.with_database("second_database")

    assert first.selected_database == "first_database"
    assert second.selected_database == "second_database"
    assert second.connection_profile is profile
    assert "secret-password" not in repr(profile)
    assert "secret-password" not in repr(first)
    assert "connection" not in vars(first)


def test_database_config_missing(monkeypatch):
    for key in [
        "MYSQL_HOST",
        "MYSQL_PORT",
        "MYSQL_USER",
        "MYSQL_PASSWORD",
        "MYSQL_DATABASE",
    ]:
        monkeypatch.delenv(key, raising=False)

    cfg = DBConfig.from_env()
    assert cfg.host is None
    assert DBClient().configured is False


def test_database_connection_failure(monkeypatch):
    monkeypatch.setattr(DBClient, "_connect", lambda self: None)
    result = DBClient().execute_readonly("SELECT 1")
    assert result["status"] == "not_configured" or result["status"] == "unavailable"


def test_connect_readonly_uses_configured_parameters_and_timeout(monkeypatch):
    calls = []

    class Cursors:
        DictCursor = object()

    class FakePyMySQL:
        cursors = Cursors()

        @staticmethod
        def connect(**kwargs):
            calls.append(kwargs)
            return object()

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    client = DBClient(DBConfig(
        host="db.example",
        port=3307,
        user="validator",
        password="secret-password",
        database="app",
    ))

    assert client.connect_readonly(0.75) is not None
    assert calls == [{
        "host": "db.example",
        "user": "validator",
        "password": "secret-password",
        "database": "app",
        "port": 3307,
        "connect_timeout": 5,
        "read_timeout": 0.75,
        "cursorclass": Cursors.DictCursor,
    }]


def test_connect_readonly_failure_is_sanitized(monkeypatch):
    class Cursors:
        DictCursor = object()

    class FakePyMySQL:
        cursors = Cursors()

        @staticmethod
        def connect(**kwargs):
            raise RuntimeError(
                "FAKE_DRIVER_SECRET host=secret-host password=secret-password")

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    client = DBClient(DBConfig(host="db", user="user",
                      password="secret", database="app"))

    assert client.connect_readonly(0.5) is None


def test_execute_readonly_closes_one_shot_connection_on_success(monkeypatch):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            pass

        def fetchall(self):
            return [{"ok": 1}]

    class Connection:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()
    client = DBClient(DBConfig(host="db", user="user", database="app"))
    monkeypatch.setattr(client, "_connect", lambda: connection)

    assert client.execute_readonly("SELECT 1;")["status"] == "ok"
    assert connection.closed is True


def test_execute_readonly_closes_one_shot_connection_on_failure(monkeypatch):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            raise RuntimeError("driver failure")

        def fetchall(self):
            return []

    class Connection:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()
    client = DBClient(DBConfig(host="db", user="user", database="app"))
    monkeypatch.setattr(client, "_connect", lambda: connection)

    result = client.execute_readonly("SELECT 1;")
    assert result["status"] == "error"
    assert connection.closed is True


def test_get_connection_lease_closes_explain_connection_when_cursor_closes(monkeypatch):
    class Cursor:
        def close(self):
            pass

    class Connection:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    connection = Connection()
    client = DBClient(DBConfig(host="db", user="user", database="app"))
    monkeypatch.setattr("backend.db.get_client", lambda: client)
    monkeypatch.setattr(client, "connect_readonly", lambda: connection)

    from backend.db import get_connection
    lease = get_connection()
    cursor = lease.cursor()
    cursor.close()

    assert connection.closed is True


def test_dbclient_does_not_reuse_process_global_connection(monkeypatch):
    class Cursors:
        DictCursor = object()

    fresh_connection = object()
    calls = []

    class FakePyMySQL:
        cursors = Cursors()

        @staticmethod
        def connect(**kwargs):
            calls.append(kwargs)
            return fresh_connection

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    client = DBClient(DBConfig(host="db", user="user", database="app"))

    assert client.connect_readonly(0.5) is fresh_connection
    assert client.configured is True
    assert len(calls) == 1


def test_execute_readonly_sanitizes_driver_errors(monkeypatch):
    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            raise RuntimeError(
                "secret-host:3307 password=secret driver detail")

        def fetchall(self):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    client = DBClient(DBConfig(host="db", user="user", database="app"))
    monkeypatch.setattr(client, "_connect", lambda: Connection())

    result = client.execute_readonly("SELECT 1;")

    assert result == {
        "status": "error",
        "error": "query_failed",
        "message": "Database query failed.",
    }


def test_concurrent_operations_get_independent_connections(monkeypatch):
    active = 0
    peak_active = 0
    state_lock = threading.Lock()
    execute_barrier = threading.Barrier(2)
    connections = []

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql):
            nonlocal active, peak_active
            with state_lock:
                active += 1
                peak_active = max(peak_active, active)
            execute_barrier.wait(timeout=2)
            with state_lock:
                active -= 1

        def fetchall(self):
            return [{"ok": 1}]

    class Connection:
        def __init__(self):
            self.closed = False

        def cursor(self):
            return Cursor()

        def close(self):
            self.closed = True

    class Cursors:
        DictCursor = object()

    class FakePyMySQL:
        cursors = Cursors()

        @staticmethod
        def connect(**kwargs):
            connection = Connection()
            connections.append(connection)
            return connection

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    client = DBClient(DBConfig(host="db", user="user", database="app"))
    results = []

    def run_operation():
        results.append(client.execute_readonly("SELECT 1;"))

    workers = [threading.Thread(target=run_operation) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=3)

    assert all(not worker.is_alive() for worker in workers)
    assert len(results) == 2
    assert all(result["status"] == "ok" for result in results)
    assert len(connections) == 2
    assert len({id(connection) for connection in connections}) == 2
    assert peak_active == 2
    assert all(connection.closed for connection in connections)


def test_successful_select_1_health_check(monkeypatch):
    class FakeClient:
        configured = True

        def execute_readonly(self, sql, max_rows=1):
            assert sql.strip().lower().startswith("select")
            return {"status": "ok", "row_count": 1, "rows": [{"ok": 1}]}

    monkeypatch.setattr("backend.main.get_client", lambda: FakeClient())
    response = health()
    assert response["database"]["status"] == "connected"


def test_explain_evidence_parsing():
    rows = [
        {
            "id": 1,
            "select_type": "SIMPLE",
            "table": "users",
            "type": "ALL",
            "possible_keys": None,
            "key": None,
            "key_len": None,
            "rows": 5000,
            "Extra": "Using where",
        }
    ]

    evidence = parse_explain_rows(rows)
    assert evidence["estimated_rows"] == 5000
    assert evidence["access_type"] == "ALL"
    assert evidence["full_table_scan"] is True
    assert evidence["possible_keys"] == []
    assert evidence["key"] is None


def test_update_explain_does_not_execute_update(monkeypatch):
    calls = []

    class FakeCursor:
        def execute(self, sql):
            calls.append(sql)
            if sql.lower().startswith("explain update"):
                return None
            raise AssertionError(f"unexpected SQL executed: {sql}")

        def fetchall(self):
            return [{
                "id": 1,
                "table": "users",
                "type": "const",
                "possible_keys": "PRIMARY",
                "key": "PRIMARY",
                "key_len": "4",
                "rows": 1,
                "Extra": "",
            }]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr("backend.db_evidence.get_connection",
                        lambda: FakeConnection())
    evidence = collect_statement_evidence(
        "UPDATE users SET status = 'inactive' WHERE id = 10;")
    assert evidence is not None
    assert evidence.available is True
    assert any("EXPLAIN UPDATE" in sql.upper() for sql in calls)
    assert not any("UPDATE users SET status" in sql.upper() for sql in calls)


def test_delete_explain_does_not_execute_delete(monkeypatch):
    calls = []

    class FakeCursor:
        def execute(self, sql):
            calls.append(sql)
            if sql.lower().startswith("explain delete"):
                return None
            raise AssertionError(f"unexpected SQL executed: {sql}")

        def fetchall(self):
            return [{
                "id": 1,
                "table": "users",
                "type": "ref",
                "possible_keys": "idx_users_status",
                "key": "idx_users_status",
                "key_len": "5",
                "rows": 12,
                "Extra": "Using where",
            }]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr("backend.db_evidence.get_connection",
                        lambda: FakeConnection())
    evidence = collect_statement_evidence(
        "DELETE FROM users WHERE status = 'inactive';")
    assert evidence is not None
    assert evidence.available is True
    assert any("EXPLAIN DELETE" in sql.upper() for sql in calls)


def test_select_explain_evidence(monkeypatch):
    class FakeCursor:
        def execute(self, sql):
            assert sql.lower().startswith("explain select")

        def fetchall(self):
            return [{
                "id": 1,
                "table": "orders",
                "type": "ref",
                "possible_keys": "idx_orders_customer_id",
                "key": "idx_orders_customer_id",
                "key_len": "5",
                "rows": 3,
                "Extra": "Using where",
            }]

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return FakeCursor()

    monkeypatch.setattr("backend.db_evidence.get_connection",
                        lambda: FakeConnection())
    evidence = collect_statement_evidence(
        "SELECT * FROM orders WHERE customer_id = 42;")
    assert evidence is not None
    assert evidence.available is True
    assert evidence.database is None
    assert evidence.estimated_rows == 3
    assert evidence.access_type == "ref"
    assert evidence.key == "idx_orders_customer_id"
    assert evidence.full_table_scan is False


def test_full_table_scan_detection():
    result = parse_explain_rows([
        {
            "type": "ALL",
            "possible_keys": None,
            "key": None,
            "rows": 2000,
            "Extra": "Using where",
        }
    ])
    assert result["full_table_scan"] is True


def test_index_usage_detection():
    result = parse_explain_rows([
        {
            "type": "ref",
            "possible_keys": "PRIMARY, idx_users_status",
            "key": "idx_users_status",
            "key_len": "5",
            "rows": 7,
            "Extra": "Using index condition",
        }
    ])
    assert result["full_table_scan"] is False
    assert result["key"] == "idx_users_status"
    assert result["possible_keys"] == ["PRIMARY", "idx_users_status"]


def test_database_evidence_unavailable_does_not_break_api_validate(monkeypatch):
    monkeypatch.setattr("backend.main.collect_statement_evidence", lambda sql,
                        *, connection=None: DatabaseEvidence(available=False, error="unavailable"))

    payload = validate(ValidateRequest(
        sql="UPDATE users SET status = 'inactive' WHERE id = 10;"))
    assert payload["analysis_mode"] in {
        "STATIC", "STATIC_DATABASE_UNAVAILABLE"}
    assert payload["statements"][0]["database_evidence"]["available"] is False


def test_supported_destructive_statement_is_refused_by_evidence_layer():
    result = collect_statement_evidence("DROP TABLE users;")
    assert result is not None
    assert result.available is False
    assert "unsupported" in (result.error or "").lower()


def test_explain_driver_errors_are_sanitized():
    class FailingCursor:
        def execute(self, sql):
            raise RuntimeError(
                "FAKE_DRIVER_SECRET host=secret-host password=secret-password")

        def close(self):
            pass

    class FailingConnection:
        def cursor(self):
            return FailingCursor()

    result = collect_statement_evidence(
        "UPDATE users SET status = 'inactive' WHERE id = 10;",
        connection=FailingConnection(),
    )

    assert result.error == "explain_failed"
    assert "FAKE_DRIVER_SECRET" not in str(result)
    assert "secret-host" not in str(result)
    assert "secret-password" not in str(result)


def test_evidence_endpoint_sanitizes_driver_errors(monkeypatch):
    class FailingClient:
        def execute_readonly(self, sql):
            return {
                "status": "error",
                "message": "FAKE_DRIVER_SECRET host=secret-host password=secret-password",
            }

    monkeypatch.setattr("backend.main.get_client", lambda: FailingClient())
    result = run_evidence(EvidenceRequest(sql="SELECT 1;"))

    assert result == {
        "status": "error",
        "error": "evidence_queries_disabled",
        "message": "Public arbitrary evidence queries are disabled. Use Local validation or the Company connector-only evidence contract.",
    }
    assert "FAKE_DRIVER_SECRET" not in str(result)
    assert "secret-host" not in str(result)
    assert "secret-password" not in str(result)


def test_readonly_validator_accepts_one_ordinary_select():
    assert validate_readonly_select(
        "SELECT id, status FROM users WHERE id = 10;") is True


def test_evidence_endpoint_rejects_non_select_without_db_call(monkeypatch):
    calls = []

    class Client:
        def execute_readonly(self, sql):
            calls.append(sql)
            return {"status": "ok"}

    monkeypatch.setattr("backend.main.get_client", lambda: Client())
    for sql in [
        "INSERT INTO users (id) VALUES (1);",
        "UPDATE users SET active = 0;",
        "DELETE FROM users;",
        "DROP TABLE users;",
        "ALTER TABLE users ADD COLUMN note TEXT;",
    ]:
        assert run_evidence(EvidenceRequest(sql=sql))[
            "error"] == "evidence_queries_disabled"
    assert calls == []


def test_evidence_endpoint_rejects_multiple_statements_without_db_call(monkeypatch):
    calls = []

    class Client:
        def execute_readonly(self, sql):
            calls.append(sql)
            return {"status": "ok"}

    monkeypatch.setattr("backend.main.get_client", lambda: Client())
    for sql in ["SELECT 1; SELECT 2;", "SELECT 1; DROP TABLE users;"]:
        assert run_evidence(EvidenceRequest(sql=sql))[
            "error"] == "evidence_queries_disabled"
    assert calls == []


def test_evidence_endpoint_rejects_select_into_file_without_db_call(monkeypatch):
    calls = []

    class Client:
        def execute_readonly(self, sql):
            calls.append(sql)
            return {"status": "ok"}

    monkeypatch.setattr("backend.main.get_client", lambda: Client())
    sql_cases = ["SELECT 1 INTO OUTFILE '/tmp/x';"]
    if validate_readonly_select("SELECT 1 INTO DUMPFILE '/tmp/x';") is False:
        sql_cases.append("SELECT 1 INTO DUMPFILE '/tmp/x';")
    for sql in sql_cases:
        assert run_evidence(EvidenceRequest(sql=sql))[
            "error"] == "evidence_queries_disabled"
    assert calls == []


def test_db_client_rejects_unsafe_sql_before_connect(monkeypatch):
    client = DBClient(DBConfig(host="db", user="user", database="app"))
    calls = []
    monkeypatch.setattr(client, "_connect", lambda: calls.append("connect"))

    for sql in [
        "SELECT 1; SELECT 2;",
        "SELECT 1 INTO OUTFILE '/tmp/x';",
        "SELECT 1 INTO DUMPFILE '/tmp/x';",
    ]:
        result = client.execute_readonly(sql)
        assert result["error"] == "only_select_allowed"
    assert calls == []
