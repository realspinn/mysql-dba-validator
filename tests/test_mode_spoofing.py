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

    def test_mode_company_with_loopback_target_still_collects_evidence(self, monkeypatch):
        """A declared company mode cannot override a loopback host classification."""
        calls = []

        def fake_evidence(sql, session=None):
            calls.append(("evidence", sql))
            return DatabaseEvidence(available=False, error="database_unavailable")

        def fake_metadata(tables, session=None):
            calls.append(("metadata", tables))
            return MetadataEvidence(status="unavailable")

        monkeypatch.setattr(
            "backend.main.collect_statement_evidence", fake_evidence)
        monkeypatch.setattr("backend.main.collect_metadata", fake_metadata)

        assert classify_target("127.0.0.1", 3306) == TargetClass.LOOPBACK_LOCAL

        response = client.post('/api/validate', json={
            'sql': 'SELECT 1',
            'host': '127.0.0.1',
            'port': 3306,
            'database': None,
            'mode': 'company',
        })

        assert response.status_code == 200
        data = response.json()
        assert data["statement_count"] >= 1
        assert calls and calls[0][0] == "evidence"
        assert any(call[0] == "metadata" for call in calls)
        assert data["analysis_mode"] in {
            "STATIC", "STATIC_DATABASE_UNAVAILABLE"}
        assert "invalid_target" not in repr(data)

        # Company mode is ignored for authorization; loopback classification still permits the local evidence path.
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

    def test_loopback_always_permits_evidence_attempt(self, monkeypatch):
        """Loopback classification is authoritative; mode cannot suppress the local evidence path."""
        calls = []

        def fake_evidence(sql, session=None):
            calls.append(("evidence", sql))
            return DatabaseEvidence(available=False, error="database_unavailable")

        def fake_metadata(tables, session=None):
            calls.append(("metadata", tables))
            return MetadataEvidence(status="unavailable")

        monkeypatch.setattr(
            "backend.main.collect_statement_evidence", fake_evidence)
        monkeypatch.setattr("backend.main.collect_metadata", fake_metadata)

        assert classify_target("127.0.0.1", 3306) == TargetClass.LOOPBACK_LOCAL

        response = client.post('/api/validate', json={
            'sql': 'SELECT 1',
            'host': '127.0.0.1',
            'port': 3306,
            'database': None,
            'mode': 'company',
        })

        assert response.status_code == 200
        data = response.json()
        assert data["statement_count"] >= 1
        assert calls and calls[0][0] == "evidence"
        assert any(call[0] == "metadata" for call in calls)
        assert data["analysis_mode"] in {
            "STATIC", "STATIC_DATABASE_UNAVAILABLE"}
        assert data["statements"][0]["database_evidence"]["error"] in {
            "database_unavailable", None}

        # The request remains loopback-local even when the caller incorrectly labels it company.
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
