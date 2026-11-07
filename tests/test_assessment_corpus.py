"""
Offline assessment corpus.

Pins representative static assessment results without changing risk policy.
Exact scores are asserted only where documented rules fix them (100 for
unrestricted writes / DROP / not-analysed, and 20 for the MEDIUM floor).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app, base_url="http://127.0.0.1:8420")


def validate(sql: str) -> dict:
    """Run static validation and return the statement or script-level error."""
    response = client.post("/api/validate", json={"sql": sql})
    assert response.status_code == 200, response.text
    data = response.json()

    if "error" in data:
        return data
    if data.get("statement_count") == 1 and data.get("statements"):
        return data["statements"][0]
    return data


CASES = [
    # Unrestricted writes and always-true predicates.
    ("UPDATE users SET status = 'inactive'", {"statement_type": "UPDATE", "analysis_status": "analysed", "risk_level": "CRITICAL", "confidence": "HIGH", "score": 100, "scope_basis": "all_rows"}),
    ("DELETE FROM users", {"statement_type": "DELETE", "analysis_status": "analysed", "risk_level": "CRITICAL", "confidence": "HIGH", "score": 100, "scope_basis": "all_rows"}),
    ("UPDATE users SET status = 'inactive' WHERE 1 = 1", {"statement_type": "UPDATE", "analysis_status": "analysed", "risk_level": "CRITICAL", "confidence": "HIGH", "score": 100, "scope_basis": "all_rows"}),
    ("DELETE FROM users WHERE TRUE", {"statement_type": "DELETE", "analysis_status": "analysed", "risk_level": "CRITICAL", "confidence": "HIGH", "score": 100, "scope_basis": "all_rows"}),
    # Ordinary and broad WHERE clauses; the MEDIUM-band score is not pinned.
    ("UPDATE users SET status = 'inactive' WHERE id = 10", {"statement_type": "UPDATE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED"}),
    ("DELETE FROM users WHERE status = 'old'", {"statement_type": "DELETE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED"}),
    ("UPDATE users SET status = 'inactive' WHERE id > 0", {"statement_type": "UPDATE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED"}),
    # LIMIT does not make an otherwise unrestricted delete safe.
    ("DELETE FROM users LIMIT 100", {"statement_type": "DELETE", "analysis_status": "analysed", "risk_level": "CRITICAL", "confidence": "LIMITED", "score": 100}),
    ("DELETE FROM users WHERE status = 'old' LIMIT 100", {"statement_type": "DELETE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED"}),
    # INSERT variants.
    ("INSERT INTO t (a) VALUES (1)", {"statement_type": "INSERT", "analysis_status": "analysed", "risk_level": "LOW", "confidence": "LIMITED"}),
    ("INSERT INTO t (a) VALUES (1) ON DUPLICATE KEY UPDATE a = 2", {"statement_type": "INSERT", "analysis_status": "analysed", "risk_level": "LOW", "confidence": "LIMITED"}),
    ("INSERT IGNORE INTO t (a) VALUES (1)", {"statement_type": "INSERT", "analysis_status": "analysed", "risk_level": "LOW", "confidence": "LIMITED"}),
    ("INSERT INTO t (a) SELECT id FROM users", {"statement_type": "INSERT", "analysis_status": "analysed", "risk_level": "LOW", "confidence": "LIMITED"}),
    # Multi-table UPDATE / DELETE.
    ("UPDATE users u JOIN orders o ON o.user_id = u.id SET u.status = 'inactive' WHERE o.id = 5", {"statement_type": "UPDATE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED"}),
    ("DELETE u FROM users u JOIN orders o ON o.user_id = u.id WHERE u.id = 10", {"statement_type": "DELETE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED"}),
    ("DELETE FROM users, orders USING users INNER JOIN orders ON orders.user_id = users.id WHERE users.id = 10", {"statement_type": "DELETE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED"}),
    # Destructive DDL.
    ("DROP TABLE users", {"statement_type": "DROP", "analysis_status": "analysed", "risk_level": "CRITICAL", "confidence": "HIGH", "score": 100}),
    ("DROP DATABASE shop", {"statement_type": "DROP", "analysis_status": "analysed", "risk_level": "CRITICAL", "confidence": "HIGH", "score": 100}),
    ("TRUNCATE TABLE users", {"statement_type": "TRUNCATE", "analysis_status": "analysed", "risk_level": "CRITICAL", "confidence": "HIGH", "score": 100}),
    # Other DDL.
    ("ALTER TABLE users DROP COLUMN last_login", {"statement_type": "ALTER", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "HIGH"}),
    ("ALTER TABLE users ADD INDEX idx_status (status)", {"statement_type": "ALTER", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "HIGH"}),
    ("CREATE INDEX idx_status ON users (status)", {"statement_type": "CREATE", "analysis_status": "analysed", "risk_level": "LOW", "confidence": "HIGH"}),
    # Stored procedures and DELIMITER script refusal.
    ("CALL process_monthly_billing(2026, 8)", {"statement_type": "CALL", "analysis_status": "analysed", "risk_level": "LOW", "confidence": "LIMITED"}),
    ("CREATE PROCEDURE p() SELECT 1", {"statement_type": "CREATE PROCEDURE", "analysis_status": "unsupported", "risk_level": "CRITICAL", "confidence": "LOW"}),
    ("CREATE PROCEDURE p() BEGIN SELECT 1; END", {"error": "stored_program_not_supported"}),
    ("DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END //\nDELIMITER ;", {"error": "delimiter_not_supported"}),
    # Uncertain row scope without metadata.
    ("UPDATE users SET status = 'inactive' WHERE id IN (SELECT user_id FROM orders)", {"statement_type": "UPDATE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED", "score": 25, "scope_basis": "not_established"}),
    ("DELETE FROM users WHERE status IS NULL", {"statement_type": "DELETE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED", "scope_basis": "not_established"}),
    ("UPDATE users SET status = 'inactive' WHERE id = 10", {"statement_type": "UPDATE", "analysis_status": "analysed", "risk_level": "MEDIUM", "confidence": "LIMITED", "score": 20, "scope_basis": "not_established"}),
    # Invalid, unreadable, and INTO OUTFILE statements.
    ("SELEC * FROM users", {"statement_type": "UNKNOWN", "analysis_status": "invalid", "risk_level": "CRITICAL", "confidence": "LOW", "score": 100}),
    ("UPDATE users SET status = 'x' WHERE id IN (1, 2", {"analysis_status": "unreadable", "risk_level": "CRITICAL", "confidence": "LOW", "score": 100}),
    ("SELECT * FROM orders INTO OUTFILE '/tmp/x'", {"risk_level": "CRITICAL", "confidence": "LOW", "score": 100, "allow_analysis_statuses": ["unreadable", "analysed"]}),
]


@pytest.mark.parametrize("sql, expected", CASES)
def test_assessment_corpus(sql, expected):
    result = validate(sql)

    if "error" in expected:
        assert result == {"error": expected["error"]}
        return

    assert "statement_type" in result, "statement response must contain statement_type"
    if "statement_type" in expected:
        assert result["statement_type"] == expected["statement_type"]

    if "allow_analysis_statuses" in expected:
        assert result["analysis_status"] in expected["allow_analysis_statuses"]
    else:
        assert result["analysis_status"] == expected["analysis_status"]

    assert result["risk_level"] == expected["risk_level"]
    assert result["confidence"] == expected["confidence"]
    if "score" in expected:
        assert result["score"] == expected["score"]
    if "scope_basis" in expected:
        assert result["scope"]["basis"] == expected["scope_basis"]
