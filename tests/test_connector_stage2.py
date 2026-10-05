import logging

import pytest
from fastapi.testclient import TestClient

from connector.server import ConnectorRuntime, create_app, create_stage2_app

VALID_ORIGIN = "https://inf-extends-gardening-retrieval.trycloudflare.com"


@pytest.fixture
def runtime():
    return ConnectorRuntime(
        bind_host="127.0.0.1",
        port=8765,
        allowed_origins=[VALID_ORIGIN],
        pairing_ttl_seconds=30,
        session_ttl_seconds=30,
        allows_database=True,
        database_capabilities=["mysql_connection_test"],
    )


@pytest.fixture
def client(runtime):
    app = create_app(runtime=runtime)
    with TestClient(app) as test_client:
        yield test_client


def _issue_session(client):
    token = client.app.state.connector.issue_pairing_token()
    pair_response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    assert pair_response.status_code == 200
    return pair_response.json()["session_token"]


def test_stage2_factory_explicitly_enables_database_route():
    app = create_stage2_app()
    runtime = app.state.connector
    assert runtime.allows_database is True
    assert runtime.database_capabilities == ["mysql_connection_test"]
    assert runtime.bind_host == "127.0.0.1"
    assert runtime.port == 8765


def test_db_target_accepts_localhost_and_loopback(client, monkeypatch):
    session = _issue_session(client)
    posted = {
        "host": "127.0.0.1",
        "port": 3306,
        "username": "appuser",
        "password": "top-secret-pass",
        "database": "testdb",
    }

    class FakeConn:
        def close(self):
            pass

    def fake_connect(*args, **kwargs):
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 3306
        assert kwargs["user"] == "appuser"
        assert kwargs["password"] == "top-secret-pass"
        return FakeConn()

    monkeypatch.setattr("connector.server.pymysql.connect", fake_connect)

    response = client.post(
        "/db/test",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json=posted,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "connected"}


def test_db_target_rejects_non_local_targets(client):
    session = _issue_session(client)
    for payload in [
        {"host": "localhost", "port": 3307, "username": "u", "password": "p"},
        {"host": "db.internal", "port": 3306, "username": "u", "password": "p"},
        {"host": "0.0.0.0", "port": 3306, "username": "u", "password": "p"},
        {"host": "::1", "port": 3306, "username": "u", "password": "p"},
        {"host": "127.0.0.1", "port": 3307, "username": "u", "password": "p"},
        {"host": "8.8.8.8", "port": 3306, "username": "u", "password": "p"},
    ]:
        response = client.post(
            "/db/test",
            headers={
                "Origin": VALID_ORIGIN,
                "Authorization": f"Bearer {session}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        assert response.status_code == 400
        assert response.json()["error_code"] == "database_target_not_allowed"


def test_db_requires_valid_session_and_origin(client):
    response = client.post(
        "/db/test",
        headers={
            "Origin": VALID_ORIGIN,
            "Content-Type": "application/json",
        },
        json={"host": "127.0.0.1", "port": 3306,
              "username": "u", "password": "p"},
    )
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_session"

    bad_origin = client.post(
        "/db/test",
        headers={
            "Origin": "https://evil.example",
            "Authorization": "Bearer not-a-session",
            "Content-Type": "application/json",
        },
        json={"host": "127.0.0.1", "port": 3306,
              "username": "u", "password": "p"},
    )
    assert bad_origin.status_code == 401
    assert bad_origin.json()["error_code"] == "invalid_origin"


def test_db_password_is_never_returned_or_logged(client, monkeypatch, caplog):
    session = _issue_session(client)
    secret = "super-secret-local-db-password-123"
    calls = {"count": 0}

    class FakeCursor:
        def execute(self, *args, **kwargs):
            pass

    class FakeConn:
        def close(self):
            calls["count"] += 1

    def fake_connect(*args, **kwargs):
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 3306
        assert kwargs["user"] == "appuser"
        assert kwargs["password"] == secret
        return FakeConn()

    monkeypatch.setattr("connector.server.pymysql.connect", fake_connect)

    with caplog.at_level(logging.INFO):
        response = client.post(
            "/db/test",
            headers={
                "Origin": VALID_ORIGIN,
                "Authorization": f"Bearer {session}",
                "Content-Type": "application/json",
            },
            json={
                "host": "127.0.0.1",
                "port": 3306,
                "username": "appuser",
                "password": secret,
                "database": "testdb",
            },
        )

    assert response.status_code == 200
    assert response.json() == {"status": "connected"}
    assert secret not in response.text
    assert secret not in caplog.text
    assert calls["count"] == 1


def test_db_connection_failures_are_sanitized(client, monkeypatch):
    session = _issue_session(client)

    def fake_connect(*args, **kwargs):
        raise RuntimeError("password=very-secret connection attempt")

    monkeypatch.setattr("connector.server.pymysql.connect", fake_connect)

    response = client.post(
        "/db/test",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={"host": "127.0.0.1", "port": 3306,
              "username": "appuser", "password": "badpass"},
    )

    assert response.status_code == 500
    assert response.json()["error_code"] == "database_connection_failed"
    assert "very-secret" not in response.text
    assert "badpass" not in response.text


def test_db_endpoint_has_no_arbitrary_sql_path(client):
    session = _issue_session(client)
    response = client.post(
        "/db/test",
        headers={
            "Origin": VALID_ORIGIN,
            "Authorization": f"Bearer {session}",
            "Content-Type": "application/json",
        },
        json={"host": "127.0.0.1", "port": 3306, "username": "u",
              "password": "p", "query": "SELECT 1"},
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "invalid_db_request"
