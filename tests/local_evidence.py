"""Test helper: drive local /api/validate through its per-request evidence connection.

Local evidence uses only the login supplied with the validation request, on one
connection opened by backend.main.open_evidence_connection. Tests that exercise
the local scoring pipeline with stubbed collectors use a fake login and a fake
connection so that no MySQL server or credential is ever needed.
"""

from __future__ import annotations

from backend.main import ValidateRequest

FAKE_LOGIN = {
    "host": "127.0.0.1",
    "port": 3306,
    "username": "fake_local_user",
    "password": "Fake-Local-Password-0731",
    "database": "app",
}


class FakeEvidenceConnection:
    """Stands in for the request's evidence connection and records its lifecycle."""

    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def use_fake_evidence_connection(monkeypatch, connection=None):
    """Make backend.main open `connection` (or a new fake) for each local validation."""
    connection = connection if connection is not None else FakeEvidenceConnection()
    opened = []

    def fake_open(profile, database, **_kwargs):
        opened.append((profile, database))
        return connection, None

    monkeypatch.setattr("backend.main.open_evidence_connection", fake_open)
    connection.opened = opened
    return connection


def local_request(sql, **overrides):
    """A local validation request carrying the fake login and a selected database."""
    return ValidateRequest(sql=sql, **{**FAKE_LOGIN, **overrides})
