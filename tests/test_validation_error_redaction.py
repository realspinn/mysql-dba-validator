"""No public API response, including 422 validation errors, contains client-supplied
credential values.

FastAPI's default 422 body includes each error's `input` (and `ctx`); for model-level
or missing-field errors that `input` is the whole request, password included. The
public app replaces it with a redacted body that keeps only type, location and message.
Fake credentials only.
"""

import pytest
from fastapi.testclient import TestClient

from backend.main import app

USER = "fake_redaction_user"
PASSWORD = "Fake-Redaction-Password-4417"
client = TestClient(app)

CASES = [
    # /api/validate
    ("/api/validate", {"sql": "SELECT 1", "host": "127.0.0.1", "port": 3306, "password": PASSWORD},
     "password_without_username"),
    ("/api/validate", {"sql": "SELECT 1", "host": "127.0.0.1", "port": 3306, "username": USER},
     "username_without_password"),
    ("/api/validate", {"sql": "SELECT 1", "host": "127.0.0.1", "mode": "company",
                       "username": USER, "password": PASSWORD}, "company_mode_with_login"),
    ("/api/validate", {"sql": "SELECT 1", "username": USER, "password": PASSWORD, "extra": PASSWORD},
     "extra_field_carrying_secret"),
    ("/api/validate", {"username": USER, "password": PASSWORD}, "missing_sql"),
    ("/api/validate", {"sql": "SELECT 1", "port": "not-a-port", "username": USER, "password": PASSWORD},
     "bad_port_type"),
    ("/api/validate", {"sql": "SELECT 1", "mode": PASSWORD, "username": USER, "password": PASSWORD},
     "secret_in_literal_field"),
    ("/api/validate", {"sql": 12345, "username": USER, "password": PASSWORD}, "bad_sql_type"),
    # /api/connections/test
    ("/api/connections/test", {"username": USER, "password": PASSWORD}, "test_missing_host"),
    ("/api/connections/test", {"host": "127.0.0.1", "port": "x", "username": USER, "password": PASSWORD},
     "test_bad_port"),
    ("/api/connections/test", {"host": "127.0.0.1", "password": PASSWORD}, "test_missing_username"),
    ("/api/connections/test", {"host": "127.0.0.1", "username": USER, "password": PASSWORD,
                               "extra": PASSWORD}, "test_extra_field"),
    ("/api/connections/test", {"host": "127.0.0.1", "username": USER, "password": ["x", PASSWORD]},
     "test_password_wrong_type"),
    # /api/connections/discover-databases
    ("/api/connections/discover-databases", {"username": USER, "password": PASSWORD}, "discover_missing_host"),
    ("/api/connections/discover-databases", {"host": "127.0.0.1", "port": 0, "username": USER,
                                             "password": PASSWORD}, "discover_port_out_of_range"),
    # /api/evidence (always refused, but its body is still validated)
    ("/api/evidence", {"password": PASSWORD, "username": USER, "unexpected": PASSWORD}, "evidence_bad_body"),
]


@pytest.mark.parametrize("path, body, case", CASES, ids=[case for _, _, case in CASES])
def test_validation_errors_never_echo_credentials(path, body, case):
    response = client.post(path, json=body)
    assert response.status_code == 422, (case, response.status_code, response.text[:200])
    assert PASSWORD not in response.text, case
    assert USER not in response.text, case


def test_redacted_body_keeps_type_location_and_message_only():
    response = client.post("/api/validate", json={"sql": "SELECT 1", "password": PASSWORD})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail and all(set(error) == {"type", "loc", "msg"} for error in detail)


def test_malformed_json_body_is_redacted():
    raw = '{"sql": "SELECT 1", "password": "' + PASSWORD + '", '
    response = client.post("/api/validate", content=raw, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert PASSWORD not in response.text
