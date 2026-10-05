from types import SimpleNamespace

from backend.db import AnalysisSession, ConnectionProfile, DBClient, DBConfig
from backend.db_evidence import DatabaseEvidence, collect_statement_evidence
from backend.main import ValidateRequest, validate
from backend.metadata import MetadataEvidence, collect_metadata


class EvidenceCursor:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, sql, *params):
        self.calls.append(("execute", sql, params))

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

    def fetchone(self):
        return {"db_name": "selected_db"}

    def close(self):
        self.calls.append(("cursor_close",))


class EvidenceConnection:
    def __init__(self, calls):
        self.calls = calls
        self.closed = False

    def cursor(self):
        return EvidenceCursor(self.calls)

    def close(self):
        self.closed = True
        self.calls.append(("connection_close",))


class MetadataCursor:
    def __init__(self, calls, rows):
        self.calls = calls
        self.rows = iter(rows)

    def execute(self, sql, params):
        self.calls.append((sql, params))

    def fetchmany(self, size):
        return next(self.rows)

    def close(self):
        pass


class MetadataConnection:
    def __init__(self, calls, rows):
        self.calls = calls
        self.rows = rows
        self.closed = False

    def cursor(self):
        return MetadataCursor(self.calls, self.rows)

    def close(self):
        self.closed = True


def selected_session():
    return AnalysisSession(
        ConnectionProfile("db.example", 3307, "validator", "secret-password"),
        "selected_db",
    )


def test_validate_rejects_invalid_database_before_parser_or_db(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.main.parse_sql",
                        lambda sql: calls.append("parse"))
    monkeypatch.setattr("backend.main.get_client",
                        lambda: calls.append("client"))

    result = validate(ValidateRequest(
        sql="SELECT * FROM users", database="bad-name"))

    assert result == {
        "error_code": "invalid_database",
        "message": "Invalid database selection.",
    }
    assert calls == []


def test_validate_hostless_static_request_stays_on_evidence_path(monkeypatch):
    captured = []
    session = selected_session()
    client = SimpleNamespace(
        session=AnalysisSession(session.connection_profile, None),
        configured=False,
    )

    monkeypatch.setattr("backend.main.get_client", lambda: client)

    def fake_evidence(sql, session=None):
        captured.append(("evidence", sql, session))
        return DatabaseEvidence(available=False, error="database_unavailable")

    def fake_metadata(tables, session=None):
        captured.append(("metadata", tables, session))
        return MetadataEvidence(status="unavailable")

    monkeypatch.setattr(
        "backend.main.collect_statement_evidence", fake_evidence)
    monkeypatch.setattr("backend.main.collect_metadata", fake_metadata)

    result = validate(ValidateRequest(
        sql="UPDATE users SET active = 0 WHERE id = 1;", database="selected_db"))

    assert result["analysis_mode"] in {"STATIC", "STATIC_DATABASE_UNAVAILABLE"}
    assert captured[0][0] == "evidence"
    assert captured[0][2].selected_database == "selected_db"
    assert captured[1][0] == "metadata"
    assert captured[1][2].selected_database == "selected_db"


def test_validate_propagates_selected_database_without_modifying_sql(monkeypatch):
    captured = []
    session = selected_session()
    client = SimpleNamespace(
        session=AnalysisSession(session.connection_profile, None),
        configured=False,
    )

    monkeypatch.setattr("backend.main.get_client", lambda: client)

    def fake_evidence(sql, session=None):
        captured.append(("evidence", sql, session))
        return DatabaseEvidence(available=False, error="database_unavailable")

    def fake_metadata(tables, session=None):
        captured.append(("metadata", tables, session))
        return MetadataEvidence(status="unavailable")

    monkeypatch.setattr(
        "backend.main.collect_statement_evidence", fake_evidence)
    monkeypatch.setattr("backend.main.collect_metadata", fake_metadata)
    submitted_sql = "UPDATE users SET active = 0 WHERE id = 1;"

    result = validate(ValidateRequest(
        sql=submitted_sql, database="selected_db"))

    assert result["statements"][0]["sql"] == submitted_sql[:-1]
    assert captured[0] == ("evidence", submitted_sql[:-1], session)
    assert captured[1] == ("metadata", ["users"], session)
    assert all(item[2].selected_database == "selected_db" for item in captured)
    assert "secret-password" not in repr(session)


def test_evidence_connection_receives_selected_database_and_never_uses_use(monkeypatch):
    calls = []
    connection = EvidenceConnection(calls)
    client_calls = []

    class Client:
        def connect_readonly(self, session=None):
            client_calls.append(session)
            return connection

    monkeypatch.setattr("backend.db.get_client", lambda: Client())
    submitted_sql = "SELECT * FROM users WHERE id = 1;"

    result = collect_statement_evidence(
        submitted_sql, session=selected_session())

    assert result.available is True
    assert client_calls == [selected_session()]
    executed_sql = [call[1] for call in calls if call[0] == "execute"]
    assert executed_sql[0] == "EXPLAIN SELECT * FROM users WHERE id = 1;"
    assert all(not sql.upper().startswith("USE ") for sql in executed_sql)
    assert submitted_sql not in executed_sql
    assert connection.closed is True


def test_dbclient_connection_uses_selected_database_for_evidence(monkeypatch):
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
        user="validator",
        password="secret-password",
        database="environment_db",
    ))

    client.connect_readonly(session=selected_session())

    assert calls[0]["database"] == "selected_db"
    assert calls[0]["host"] == "db.example"
    assert calls[0]["user"] == "validator"


def test_metadata_connection_and_all_query_scopes_receive_selected_database(monkeypatch):
    calls = []
    client_calls = []
    rows = [
        [{
            "ENGINE": "InnoDB",
            "TABLE_ROWS": 1,
            "DATA_LENGTH": 1,
            "INDEX_LENGTH": 1,
            "CREATE_OPTIONS": "",
            "UPDATE_TIME": None,
        }],
        [{
            "INDEX_NAME": "idx_users_id",
            "COLUMN_NAME": "id",
            "SEQ_IN_INDEX": 1,
            "NON_UNIQUE": 0,
            "CARDINALITY": 1,
            "SUB_PART": None,
            "INDEX_TYPE": "BTREE",
        }],
        [{
            "COLUMN_NAME": "id",
            "DATA_TYPE": "int",
            "IS_NULLABLE": "NO",
            "COLUMN_KEY": "PRI",
            "ORDINAL_POSITION": 1,
        }],
    ]
    connection = MetadataConnection(calls, rows)

    class Client:
        def connect_readonly(self, read_timeout, session=None):
            client_calls.append(session)
            return connection

    monkeypatch.setattr("backend.metadata.get_client", lambda: Client())
    session = selected_session()

    result = collect_metadata(["users"], session=session)

    assert result.status == "available"
    assert client_calls == [session]
    assert len(calls) == 3
    assert all(params[0] == "selected_db" for _, params in calls)
    assert all("selected_db" not in sql for sql, _ in calls)
    assert connection.closed is True


def test_metadata_dbclient_connection_uses_selected_database(monkeypatch):
    connect_calls = []
    metadata_calls = []
    rows = [
        [{
            "ENGINE": "InnoDB",
            "TABLE_ROWS": 1,
            "DATA_LENGTH": 1,
            "INDEX_LENGTH": 1,
            "CREATE_OPTIONS": "",
            "UPDATE_TIME": None,
        }],
        [],
        [],
    ]
    connection = MetadataConnection(metadata_calls, rows)

    class Cursors:
        DictCursor = object()

    class FakePyMySQL:
        cursors = Cursors()

        @staticmethod
        def connect(**kwargs):
            connect_calls.append(kwargs)
            return connection

    monkeypatch.setitem(__import__("sys").modules, "pymysql", FakePyMySQL)
    client = DBClient(DBConfig(
        host="db.example",
        user="validator",
        password="secret-password",
        database="environment_db",
    ))
    monkeypatch.setattr("backend.metadata.get_client", lambda: client)

    result = collect_metadata(["users"], session=selected_session())

    assert result.status == "available"
    assert connect_calls[0]["database"] == "selected_db"
    assert all(params[0] == "selected_db" for _, params in metadata_calls)


def test_selected_database_identifier_validation_is_conservative():
    from backend.db import validate_database_identifier

    assert validate_database_identifier("selected_db") is True
    assert validate_database_identifier("db-name") is False
    assert validate_database_identifier("db.name") is False
    assert validate_database_identifier(" selected_db") is False
    assert validate_database_identifier("a" * 65) is False
