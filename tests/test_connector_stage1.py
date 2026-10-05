import logging
import time

import pytest
from fastapi.testclient import TestClient

from connector.server import ConnectorRuntime, create_app

VALID_ORIGIN = "https://inf-extends-gardening-retrieval.trycloudflare.com"
MALICIOUS_ORIGIN = "https://evil.example"


@pytest.fixture
def app():
    runtime = ConnectorRuntime(
        bind_host="127.0.0.1",
        port=8765,
        allowed_origins=[VALID_ORIGIN],
        pairing_ttl_seconds=30,
        session_ttl_seconds=30,
    )
    return create_app(runtime=runtime)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def test_connector_bind_address_is_loopback_only(app):
    assert app.state.connector.bind_host == "127.0.0.1"
    assert app.state.connector.bind_host != "0.0.0.0"
    assert app.state.connector.bind_host != "::"
    assert app.state.connector.bind_host != "[::]"


def test_health_is_safe_and_no_tokens_exposed(client):
    response = client.get("/health", headers={"Origin": VALID_ORIGIN})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["loopback_only"] is True
    assert "pairing_token" not in body
    assert "session_token" not in body
    assert "token" not in body


def test_valid_pairing_succeeds(client):
    token = client.app.state.connector.issue_pairing_token()
    response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "paired"
    assert payload["session_token"]
    assert payload["session_token"] != token
    assert token not in payload


def test_invalid_pairing_token_fails(client):
    response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": "a" * 64},
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "invalid_pairing_token"


def test_expired_pairing_token_fails(client):
    runtime = client.app.state.connector
    expired = runtime.issue_pairing_token(expires_in_seconds=0)
    response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": expired},
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "pairing_expired"


def test_pairing_token_is_single_use(client):
    runtime = client.app.state.connector
    token = runtime.issue_pairing_token()
    first = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    second = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    assert first.status_code == 200
    assert second.status_code == 403
    assert second.json()["error_code"] == "pairing_already_used"


def test_origin_validation_rejects_malicious_and_missing(client):
    token = client.app.state.connector.issue_pairing_token()

    with_origin = client.post(
        "/pair",
        headers={"Origin": MALICIOUS_ORIGIN,
                 "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    assert with_origin.status_code == 401
    assert with_origin.json()["error_code"] == "invalid_origin"

    missing_origin = client.post(
        "/pair",
        headers={"Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    assert missing_origin.status_code == 401
    assert missing_origin.json()["error_code"] == "invalid_origin"

    malformed_origin = client.post(
        "/pair",
        headers={"Origin": "not-a-valid-origin",
                 "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    assert malformed_origin.status_code == 401
    assert malformed_origin.json()["error_code"] == "invalid_origin"


def test_session_is_required_for_authenticated_endpoints(client):
    response = client.get(
        "/health", headers={"Origin": VALID_ORIGIN, "Authorization": "Bearer not-a-session"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_session"


def test_successful_pairing_creates_session_and_expired_session_fails(client):
    token = client.app.state.connector.issue_pairing_token()
    pair_response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    session = pair_response.json()["session_token"]

    health_response = client.get(
        "/health",
        headers={"Origin": VALID_ORIGIN, "Authorization": f"Bearer {session}"},
    )
    assert health_response.status_code == 200

    runtime = client.app.state.connector
    session_record = runtime.sessions[session]
    session_record.expires_at = time.time() - 1

    expired_response = client.get(
        "/health",
        headers={"Origin": VALID_ORIGIN, "Authorization": f"Bearer {session}"},
    )
    assert expired_response.status_code == 401
    assert expired_response.json()["error_code"] == "session_expired"


def test_unpair_invalidates_session(client):
    token = client.app.state.connector.issue_pairing_token()
    pair_response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": token},
    )
    session = pair_response.json()["session_token"]

    unpair_response = client.post(
        "/unpair",
        headers={"Origin": VALID_ORIGIN, "Authorization": f"Bearer {session}"},
    )
    assert unpair_response.status_code == 200

    after_unpair = client.get(
        "/health",
        headers={"Origin": VALID_ORIGIN, "Authorization": f"Bearer {session}"},
    )
    assert after_unpair.status_code == 401
    assert after_unpair.json()["error_code"] == "invalid_session"


def test_connector_rejects_unsupported_methods_and_endpoints(client):
    assert client.post("/query").status_code == 404
    assert client.post("/execute").status_code == 404
    assert client.post("/sql").status_code == 404
    assert client.post("/proxy").status_code == 404
    assert client.post("/tcp").status_code == 404
    assert client.post("/database").status_code == 404
    assert client.post("/metadata").status_code == 404
    assert client.post("/explain").status_code == 404

    response = client.put("/health", headers={"Origin": VALID_ORIGIN})
    assert response.status_code == 405


def test_request_limits_and_error_sanitization(client):
    oversized = {"pairing_token": "x" * 6000}
    response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json=oversized,
    )
    assert response.status_code == 413

    runtime = client.app.state.connector
    assert runtime.max_payload_bytes == 4096

    body = response.json()
    assert "token" not in str(body)
    assert "traceback" not in str(body)
    assert "path" not in str(body)


def test_connector_has_no_database_functionality_toggle(app):
    assert app.state.connector.allows_database is False
    assert app.state.connector.database_capabilities == []


def test_connector_shutdown_invalidates_state(app):
    runtime = app.state.connector
    token = runtime.issue_pairing_token()
    session_token = runtime.create_session_for_pairing_token(
        token, "https://example.test")
    runtime.shutdown()

    assert runtime.pairing_tokens == {}
    assert session_token not in runtime.sessions


def test_wildcard_origin_is_not_accepted(client):
    response = client.get("/health", headers={"Origin": "*"})
    assert response.status_code == 401
    assert response.json()["error_code"] == "invalid_origin"


def test_pairing_attempts_are_bounded(client):
    runtime = client.app.state.connector
    runtime.failed_pairing_attempts.clear()
    runtime.max_failed_pairing_attempts = 2
    runtime.pairing_fail_window_seconds = 60.0

    for _ in range(runtime.max_failed_pairing_attempts):
        response = client.post(
            "/pair",
            headers={"Origin": VALID_ORIGIN,
                     "Content-Type": "application/json"},
            json={"pairing_token": "x" * 64},
        )
        assert response.status_code == 403

    limited = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": "y" * 64},
    )
    assert limited.status_code == 429
    assert limited.json()["error_code"] == "pairing_rate_limited"


def test_pairing_rate_limit_expired_and_success_is_allowed(client):
    runtime = client.app.state.connector
    runtime.failed_pairing_attempts.clear()
    runtime.max_failed_pairing_attempts = 1
    runtime.pairing_fail_window_seconds = 0.2

    response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": "x" * 64},
    )
    assert response.status_code == 403

    limited = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": "y" * 64},
    )
    assert limited.status_code == 429

    time.sleep(0.25)
    valid_token = runtime.issue_pairing_token()
    allowed = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": valid_token},
    )
    assert allowed.status_code == 200
    assert allowed.json()["status"] == "paired"


def test_successful_pairing_does_not_count_as_failure(client):
    runtime = client.app.state.connector
    runtime.failed_pairing_attempts.clear()
    valid_token = runtime.issue_pairing_token()

    response = client.post(
        "/pair",
        headers={"Origin": VALID_ORIGIN, "Content-Type": "application/json"},
        json={"pairing_token": valid_token},
    )

    assert response.status_code == 200
    assert len(runtime.failed_pairing_attempts) == 0


def test_rate_limit_state_is_cleared_on_shutdown(app):
    runtime = app.state.connector
    runtime.failed_pairing_attempts.append(time.monotonic())
    runtime.shutdown()
    assert list(runtime.failed_pairing_attempts) == []


def test_rate_limit_memory_is_bounded(app):
    runtime = app.state.connector
    runtime.failed_pairing_attempts.clear()
    runtime.max_failed_pairing_attempts = 3
    runtime.pairing_fail_window_seconds = 60.0

    for _ in range(10):
        runtime.record_failed_pairing_attempt()

    assert len(
        runtime.failed_pairing_attempts) <= runtime.max_failed_pairing_attempts


def test_pairing_and_session_tokens_are_never_logged(client, caplog):
    logger_name = "connector.server"
    token = client.app.state.connector.issue_pairing_token()
    with caplog.at_level(logging.INFO, logger=logger_name):
        response = client.post(
            "/pair",
            headers={"Origin": VALID_ORIGIN,
                     "Content-Type": "application/json"},
            json={"pairing_token": token},
        )
        assert response.status_code == 200
        session_token = response.json()["session_token"]

        invalid = client.post(
            "/pair",
            headers={"Origin": VALID_ORIGIN,
                     "Content-Type": "application/json"},
            json={"pairing_token": "z" * 64},
        )
        assert invalid.status_code == 403

        expired_token = client.app.state.connector.issue_pairing_token(
            expires_in_seconds=0)
        expired = client.post(
            "/pair",
            headers={"Origin": VALID_ORIGIN,
                     "Content-Type": "application/json"},
            json={"pairing_token": expired_token},
        )
        assert expired.status_code == 403

        rejected = client.get(
            "/health",
            headers={"Origin": VALID_ORIGIN,
                     "Authorization": f"Bearer not-a-session"},
        )
        assert rejected.status_code == 401

        unpair = client.post(
            "/unpair",
            headers={"Origin": VALID_ORIGIN,
                     "Authorization": f"Bearer {session_token}"},
        )
        assert unpair.status_code == 200

    log_text = caplog.text
    assert token not in log_text
    assert session_token not in log_text
    assert "Bearer" not in log_text
    assert "Authorization" not in log_text
    assert "password" not in log_text.lower()
    assert "credential" not in log_text.lower()
