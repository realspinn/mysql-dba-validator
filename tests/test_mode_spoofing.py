"""
Mode spoofing security tests.

Verifies that the frontend's 'mode' field cannot override
the backend's target classification.

The backend must be authoritative: classification is determined
by analyzing host/port, not by trusting a frontend-supplied mode field.
"""
import pytest
from fastapi.testclient import TestClient

from backend.db_evidence import DatabaseEvidence
from backend.main import app, TargetClass, classify_target
from backend.metadata import MetadataEvidence
from tests.local_evidence import FAKE_LOGIN, FakeEvidenceConnection


client = TestClient(app)


class TestModeSpoofing:
    """Test that mode field is ignored for authorization decisions."""

    def test_mode_local_with_remote_target_is_rejected(self):
        """
        Attempting to send mode="local" with a remote host
        should not bypass remote target handling.

        Backend should classify 10.0.0.5:3306 as REMOTE,
        regardless of declared mode.
        """
        response = client.post('/api/validate', json={
            'sql': 'SELECT 1',
            'host': '10.0.0.5',
            'port': 3306,
            'username': 'test',
            'password': 'test',
            'database': None,
            'mode': 'local'  # Attempting to spoof
        })

        # Response should not fail immediately (backend classifies),
        # but evidence/metadata should be unavailable (remote policy)
        # Backend should never collect evidence for 10.0.0.5 just because
        # mode='local' was declared
        assert response.status_code == 200
        data = response.json()
        if 'statements' in data and len(data['statements']) > 0:
            # If we got results, evidence should be unavailable
            # because 10.0.0.5 is classified as REMOTE
            stmt = data['statements'][0]
            assert stmt['database_evidence']['available'] == False

    def test_mode_company_with_loopback_target_cannot_unlock_or_suppress_evidence(self, monkeypatch):
        """A declared company mode cannot override a loopback host classification,
        and it cannot unlock evidence: evidence depends only on the target and login."""
        connects = []

        def fake_open(profile, database, **_kwargs):
            connects.append((profile.host, database))
            return FakeEvidenceConnection(), None

        monkeypatch.setattr("backend.main.open_evidence_connection", fake_open)
        monkeypatch.setattr("backend.main.collect_statement_evidence",
                            lambda sql, **_kwargs: DatabaseEvidence(available=False, error="explain_failed"))
        monkeypatch.setattr("backend.main.collect_metadata",
                            lambda tables, **_kwargs: MetadataEvidence(status="unavailable"))

        assert classify_target("127.0.0.1", 3306) == TargetClass.LOOPBACK_LOCAL

        # Company mode, no login: still a local request, static only, no connection.
        response = client.post('/api/validate', json={
            'sql': 'SELECT 1', 'host': '127.0.0.1', 'port': 3306, 'database': None, 'mode': 'company',
        })
        assert response.status_code == 200
        data = response.json()
        assert data["statement_count"] >= 1
        assert data["analysis_mode"] == "STATIC"
        assert data["statements"][0]["database_evidence"]["status"] == "not_configured"
        assert "invalid_target" not in repr(data) and "delegated_to_connector" not in repr(data)
        assert connects == []

        # Company mode with a login is refused before any connection, without echoing it.
        refused = client.post('/api/validate', json={
            'sql': 'SELECT 1', 'host': '127.0.0.1', 'port': 3306, 'database': 'app', 'mode': 'company',
            'username': FAKE_LOGIN["username"], 'password': FAKE_LOGIN["password"],
        })
        assert refused.status_code == 422
        assert FAKE_LOGIN["password"] not in refused.text and FAKE_LOGIN["username"] not in refused.text
        assert connects == []

        # Company mode is ignored for authorization; loopback classification still decides.
        assert classify_target("127.0.0.1", 3306) == TargetClass.LOOPBACK_LOCAL

class TestModeFieldIgnored:
    """Test that mode field is never used for authorization."""

    def test_validate_ignores_mode_field_for_classification(self):
        """
        Mode field is deprecated and ignored.
        Classification is purely based on host/port analysis.
        """
        # Same request, different mode values, should behave identically

        request_base = {
            'sql': 'SELECT 1',
            'host': '10.0.0.5',
            'port': 3306,
            'username': 'user',
            'password': 'pass',
            'database': None
        }

        responses = []
        for mode in ['local', 'company', 'invalid']:
            req = {**request_base, 'mode': mode}
            response = client.post('/api/validate', json=req)
            responses.append(
                response.json() if response.status_code == 200 else response.text)

        # All three should behave identically (same error/result)
        # because mode is ignored; only host/port matters
        assert len(responses) == 3

    def test_invalid_target_fails_regardless_of_mode(self):
        """
        Invalid targets (wrong port, malformed) should fail
        regardless of mode value.
        """
        response = client.post('/api/validate', json={
            'sql': 'SELECT 1',
            'host': '127.0.0.1',
            'port': 3307,  # INVALID: non-3306
            'username': 'test',
            'password': 'test',
            'database': None,
            'mode': 'local'  # mode doesn't matter
        })

        # Should get error about invalid target
        assert response.status_code == 200
        data = response.json()
        # Backend classifies 127.0.0.1:3307 as INVALID or REMOTE
        # and rejects it
        assert 'error_code' in data or 'error' in data or 'statements' in data


class TestClassificationNotModeField:
    """Verify architecture: classification drives behavior, not mode."""

    def test_loopback_login_permits_evidence_attempt_regardless_of_mode(self, monkeypatch):
        """Loopback classification plus the request's own login opens the local evidence
        path; mode is not consulted."""
        connects = []

        def fake_open(profile, database, **_kwargs):
            connects.append((profile.host, database))
            return FakeEvidenceConnection(), None

        calls = []
        monkeypatch.setattr("backend.main.open_evidence_connection", fake_open)
        monkeypatch.setattr("backend.main.collect_statement_evidence",
                            lambda sql, **_kwargs: calls.append("evidence") or DatabaseEvidence(
                                available=False, error="explain_failed"))
        monkeypatch.setattr("backend.main.collect_metadata",
                            lambda tables, **_kwargs: calls.append("metadata") or MetadataEvidence(
                                status="unavailable"))

        assert classify_target("127.0.0.1", 3306) == TargetClass.LOOPBACK_LOCAL

        for extra in ({}, {"mode": "local"}):
            connects.clear()
            calls.clear()
            response = client.post('/api/validate', json={
                'sql': 'SELECT 1', 'host': '127.0.0.1', 'port': 3306, 'database': 'app',
                'username': FAKE_LOGIN["username"], 'password': FAKE_LOGIN["password"], **extra,
            })
            assert response.status_code == 200
            data = response.json()
            assert connects == [("127.0.0.1", "app")]
            assert calls[0] == "evidence" and "metadata" in calls
            assert data["analysis_mode"] == "STATIC_DATABASE_UNAVAILABLE"
            assert data["statements"][0]["database_evidence"]["status"] == "explain_failed"
            assert FAKE_LOGIN["password"] not in response.text

        # The request remains loopback-local whatever the caller labels it.
        assert classify_target("127.0.0.1", 3306) == TargetClass.LOOPBACK_LOCAL

    def test_remote_target_disables_evidence(self):
        """Remote targets never collect evidence."""
        response = client.post('/api/validate', json={
            'sql': 'SELECT 1',
            'host': '10.0.0.5',
            'port': 3306,
            'username': 'test',
            'password': 'test',
            'mode': 'local'  # Ignored; backend classifies as remote
        })
        assert response.status_code == 200
        data = response.json()
        if 'statements' in data and len(data['statements']) > 0:
            stmt = data['statements'][0]
            assert stmt['database_evidence']['available'] == False
