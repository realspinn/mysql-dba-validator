from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.db import AnalysisSession, ConnectionProfile, discover_databases
from backend.db_evidence import DatabaseEvidence
from backend.main import (
    ConnectionTestRequest,
    ValidateRequest,
    app,
    connection_test,
    discover_databases_endpoint,
    validate,
)
from backend.metadata import MetadataEvidence


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_connection_test_success_uses_profile_and_closes_connection(monkeypatch):
    calls = []
    connection = FakeConnection()

    class FakePyMySQL:
        @staticmethod
        def connect(**kwargs):
            calls.append(kwargs)
            return connection

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    request = ConnectionTestRequest(
        host="db.example",
        port=3307,
        username="validator",
        password="secret-password",
        database="app",
    )

    result = connection_test(request)

    assert result["status"] == "error"
    assert result["error_code"] == "local_only_target_required"
    assert "secret-password" not in repr(request)
    assert connection.closed is False
    assert calls == []


def test_connection_test_failure_is_stable_and_sanitized(monkeypatch):
    class FakePyMySQL:
        @staticmethod
        def connect(**kwargs):
            raise RuntimeError(
                "driver secret host=private.example port=3307 password=secret-password"
            )

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    request = ConnectionTestRequest(
        host="private.example",
        username="validator",
        password="secret-password",
    )

    result = connection_test(request)
    response_text = repr(result)

    assert result == {
        "status": "error",
        "connected": False,
        "error_code": "local_only_target_required",
        "message": "Only loopback MySQL targets are permitted for Local Mode.",
    }
    assert "driver secret" not in response_text
    assert "private.example" not in response_text
    assert "3307" not in response_text
    assert "secret-password" not in response_text


def test_connection_test_does_not_execute_sql(monkeypatch):
    connection = FakeConnection()
    executed = []

    class CursorForbiddenConnection(FakeConnection):
        def cursor(self):
            executed.append("cursor")
            raise AssertionError("connection test must not create a cursor")

    connection = CursorForbiddenConnection()

    class FakePyMySQL:
        @staticmethod
        def connect(**kwargs):
            return connection

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    result = connection_test(ConnectionTestRequest(
        host="db.example",
        username="validator",
        password="secret-password",
    ))

    assert result["connected"] is False
    assert result["error_code"] == "local_only_target_required"
    assert executed == []
    assert connection.closed is False


def test_connection_test_rejects_user_sql():
    with pytest.raises(ValidationError):
        ConnectionTestRequest.model_validate({
            "host": "db.example",
            "username": "validator",
            "password": "secret-password",
            "sql": "SHOW DATABASES",
        })


def test_company_validation_skips_public_database_evidence_and_metadata(monkeypatch):
    calls = []
    client = SimpleNamespace(
        session=AnalysisSession(
            ConnectionProfile("db.example", 3306,
                              "validator", "secret-password"),
            "company_db",
        ),
        configured=True,
    )

    def fake_evidence(sql, session=None):
        calls.append(("evidence", sql, session))
        return DatabaseEvidence(available=False, error="database_unavailable")

    def fake_metadata(tables, session=None):
        calls.append(("metadata", tables, session))
        return MetadataEvidence(status="unavailable")

    monkeypatch.setattr("backend.main.get_client", lambda: client)
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence", fake_evidence)
    monkeypatch.setattr("backend.main.collect_metadata", fake_metadata)

    result = validate(ValidateRequest(
        sql="SELECT * FROM users",
        database="company_db",
        mode="company",
    ))

    assert result["statement_count"] >= 1
    assert result["analysis_mode"] in {"STATIC", "STATIC_DATABASE_UNAVAILABLE"}
    assert calls and calls[0][0] == "evidence"
    assert any(call[0] == "metadata" for call in calls)
    assert calls[0][2].selected_database == "company_db"
    assert "company_db" in repr(calls[0][2])
    assert "error_code" not in result
    assert "remote_target_unavailable" not in repr(result)


def test_validate_request_rejects_company_credentials():
    with pytest.raises(ValidationError):
        ValidateRequest.model_validate({
            "sql": "SELECT 1",
            "mode": "company",
            "username": "company-user",
            "password": "company-secret",
        })


def test_validate_remote_approved_target_delegates_to_connector(monkeypatch):
    monkeypatch.setattr("backend.main.classify_target",
                        lambda host, port: "remote")
    monkeypatch.setattr(
        "backend.main.resolve_approved_company_target", lambda host, port: "approved-db")

    result = validate(ValidateRequest(
        sql="SELECT 1",
        host="db.company.example",
        port=3306,
        username=None,
        password=None,
    ))

    assert result == {
        "status": "delegated_to_connector",
        "target_id": "approved-db",
        "error_code": "connector_required",
        "message": "Approved company target requires connector validation.",
    }


def test_validate_remote_target_without_approval_is_denied(monkeypatch):
    monkeypatch.setattr("backend.main.classify_target",
                        lambda host, port: "remote")
    monkeypatch.setattr(
        "backend.main.resolve_approved_company_target", lambda host, port: None)

    result = validate(ValidateRequest(
        sql="SELECT 1",
        host="db.company.example",
        port=3306,
        username=None,
        password=None,
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "permission_denied"
    assert result["message"] == "Permission denied for this database target."


def test_validate_remote_credentials_are_rejected_by_public_backend(monkeypatch):
    monkeypatch.setattr("backend.main.classify_target",
                        lambda host, port: "remote")
    monkeypatch.setattr(
        "backend.main.resolve_approved_company_target", lambda host, port: "approved-db")

    result = validate(ValidateRequest(
        sql="SELECT 1",
        host="db.company.example",
        port=3306,
        username="company-user",
        password="company-secret",
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "permission_denied"
    assert result["message"] == "Permission denied for this database target."


def test_connection_test_invalid_values_return_sanitized_result():
    request = ConnectionTestRequest(
        host="   ",
        username="validator",
        password="secret-password",
    )

    result = connection_test(request)

    assert result["error_code"] == "invalid_connection_config"
    assert "secret-password" not in repr(result)


def test_connection_test_request_rejects_invalid_port():
    with pytest.raises(ValidationError):
        ConnectionTestRequest(
            host="db.example",
            port=70000,
            username="validator",
            password="secret-password",
        )


def test_connection_test_route_uses_phase_one_session(monkeypatch):
    captured = []

    def fake_test_connection(session):
        captured.append(session)
        return {"status": "success", "connected": True}

    monkeypatch.setattr("backend.main.probe_connection", fake_test_connection)
    result = connection_test(ConnectionTestRequest(
        host="db.example",
        username="validator",
        password="secret-password",
        database="app",
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "local_only_target_required"
    assert len(captured) == 0
    assert any(
        route.path == "/api/connections/test" and "POST" in route.methods
        for route in app.routes
    )


def test_discovery_success_uses_fixed_query_and_closes_connection(monkeypatch):
    calls = []

    class Cursor:
        def execute(self, sql):
            calls.append(("execute", sql))

        def fetchmany(self, size):
            calls.append(("fetchmany", size))
            return [("db one",), ("db-two",)]

        def close(self):
            calls.append(("cursor_close",))

    class Connection(FakeConnection):
        def cursor(self):
            return Cursor()

    connection = Connection()

    class FakePyMySQL:
        @staticmethod
        def connect(**kwargs):
            calls.append(("connect", kwargs))
            return connection

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    session = AnalysisSession(
        ConnectionProfile("db.example", 3307, "validator", "secret-password"),
        "ignored-database",
    )

    result = discover_databases(session, max_databases=2)

    assert result == {
        "status": "success",
        "databases": ["db one", "db-two"],
        "complete": True,
    }
    assert calls[0] == ("connect", {
        "host": "db.example",
        "user": "validator",
        "password": "secret-password",
        "port": 3307,
        "connect_timeout": 5,
        "read_timeout": 10.0,
    })
    assert ("execute", "SHOW DATABASES") in calls
    assert all("SELECT" not in str(call).upper()
               for call in calls if call[0] == "execute")
    assert connection.closed is True
    assert ("cursor_close",) in calls


def test_discovery_preserves_mapping_database_names():
    class Cursor:
        def execute(self, sql):
            assert sql == "SHOW DATABASES"

        def fetchmany(self, size):
            return [{"Database": "Name With Space"}, {"Database": "lower_case"}]

        def close(self):
            pass

    class Connection(FakeConnection):
        def cursor(self):
            return Cursor()

    class FakePyMySQL:
        @staticmethod
        def connect(**kwargs):
            return Connection()

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    try:
        result = discover_databases(AnalysisSession(
            ConnectionProfile()), max_databases=2)
    finally:
        monkeypatch.undo()

    assert result["databases"] == ["Name With Space", "lower_case"]


def test_discovery_returns_limited_result_without_claiming_completeness(monkeypatch):
    class Cursor:
        def execute(self, sql):
            assert sql == "SHOW DATABASES"

        def fetchmany(self, size):
            assert size == 3
            return [("first",), ("second",), ("third",)]

        def close(self):
            pass

    class Connection(FakeConnection):
        def cursor(self):
            return Cursor()

    class FakePyMySQL:
        @staticmethod
        def connect(**kwargs):
            return Connection()

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    result = discover_databases(AnalysisSession(
        ConnectionProfile()), max_databases=2)

    assert result == {
        "status": "limited",
        "databases": ["first", "second"],
        "complete": False,
    }


@pytest.mark.parametrize("failure_point", ["execute", "fetchmany"])
def test_discovery_closes_connection_when_query_or_fetch_fails(monkeypatch, failure_point):
    connection = FakeConnection()

    class Cursor:
        def execute(self, sql):
            if failure_point == "execute":
                raise RuntimeError("raw driver secret")

        def fetchmany(self, size):
            if failure_point == "fetchmany":
                raise RuntimeError("raw fetch secret")
            return []

        def close(self):
            pass

    connection.cursor = lambda: Cursor()

    class FakePyMySQL:
        @staticmethod
        def connect(**kwargs):
            return connection

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    result = discover_databases(AnalysisSession(ConnectionProfile(
        password="secret-password",
    )))

    assert result == {
        "status": "error",
        "connected": False,
        "error_code": "database_discovery_failed",
        "message": "Unable to discover databases.",
    }
    assert "raw" not in repr(result)
    assert "secret-password" not in repr(result)
    assert connection.closed is True


def test_discovery_empty_result_is_complete(monkeypatch):
    class Cursor:
        def execute(self, sql):
            assert sql == "SHOW DATABASES"

        def fetchmany(self, size):
            return []

        def close(self):
            pass

    class Connection(FakeConnection):
        def cursor(self):
            return Cursor()

    class FakePyMySQL:
        @staticmethod
        def connect(**kwargs):
            return Connection()

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    result = discover_databases(AnalysisSession(ConnectionProfile()))

    assert result == {"status": "success", "databases": [], "complete": True}


def test_discovery_request_ignores_database_and_uses_phase_one_session(monkeypatch):
    captured = []

    def fake_discover_databases(session):
        captured.append(session)
        return {"status": "success", "databases": ["app"], "complete": True}

    monkeypatch.setattr("backend.main.discover_databases",
                        fake_discover_databases)
    request = ConnectionTestRequest(
        host="db.example",
        username="validator",
        password="secret-password",
        database="must-be-ignored",
    )

    result = discover_databases_endpoint(request)

    assert result["status"] == "error"
    assert result["error_code"] == "local_only_target_required"
    assert captured == []
    assert "secret-password" not in repr(request)
    assert any(
        route.path == "/api/connections/discover-databases" and "POST" in route.methods
        for route in app.routes
    )
