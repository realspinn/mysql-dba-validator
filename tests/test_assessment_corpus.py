"""
Offline assessment corpus.

Pins the current static assessment results (risk level, confidence,
analysis status, row scope and key findings) for a representative set
of SQL statements.

This file does not change any risk policy or backend code.
Exact scores are only asserted where a documented rule fixes them
(100 for unrestricted writes / DROP / not-analysed).
For ordinary MEDIUM-scope cases only the risk level and confidence
are pinned (the exact score within the MEDIUM band is not stable).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.main import app

client = TestClient(app, base_url="http://127.0.0.1:8420")


def validate(sql: str) -> dict:
    """Run static validation (no database connection).

    Returns:
    - the single statement dict when statement_count == 1
    - the top-level {"error": "..."} dict for script-level refusals
    - otherwise the full response
    """
    response = client.post("/api/validate", json={"sql": sql})
    assert response.status_code == 200, response.text
    data = response.json()

    if "error" in data:
        return data

    if data.get("statement_count") == 1 and data.get("statements"):
        return data["statements"][0]

    return data


@pytest.mark.parametrize(
    "sql, expected",
    [
        # -----------------------------------------------------------------
        # DML – UPDATE / DELETE (unrestricted → CRITICAL / 100)
        # -----------------------------------------------------------------
        (
            "UPDATE users SET status = 'inactive'",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "CRITICAL",
                "confidence": "HIGH",
                "score": 100,
                "scope_basis": "all_rows",
            },
        ),
        (
            "DELETE FROM users",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "CRITICAL",
                "confidence": "HIGH",
                "score": 100,
                "scope_basis": "all_rows",
            },
        ),
        (
            "UPDATE users SET status = 'inactive' WHERE 1 = 1",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "CRITICAL",
                "confidence": "HIGH",
                "score": 100,
                "scope_basis": "all_rows",
            },
        ),
        (
            "DELETE FROM users WHERE TRUE",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "CRITICAL",
                "confidence": "HIGH",
                "score": 100,
                "scope_basis": "all_rows",
            },
        ),

        # -----------------------------------------------------------------
        # DML – UPDATE / DELETE with ordinary or broad WHERE
        # (exact score inside MEDIUM band is not stable → not pinned)
        # -----------------------------------------------------------------
        (
            "UPDATE users SET status = 'inactive' WHERE id = 10",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
            },
        ),
        (
            "DELETE FROM users WHERE status = 'old'",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
            },
        ),
        (
            "UPDATE users SET status = 'inactive' WHERE id > 0",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
            },
        ),

        # -----------------------------------------------------------------
        # DML – LIMIT (with and without WHERE)
        # -----------------------------------------------------------------
        (
            "DELETE FROM users LIMIT 100",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "CRITICAL",
                "confidence": "LIMITED",
                "score": 100,
            },
        ),
        (
            "DELETE FROM users WHERE status = 'old' LIMIT 100",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
            },
        ),

        # -----------------------------------------------------------------
        # DML – INSERT variants
        # -----------------------------------------------------------------
        (
            "INSERT INTO t (a) VALUES (1)",
            {
                "statement_type": "INSERT",
                "analysis_status": "analysed",
                "risk_level": "LOW",
                "confidence": "LIMITED",
            },
        ),
        (
            "INSERT INTO t (a) VALUES (1) ON DUPLICATE KEY UPDATE a = 2",
            {
                "statement_type": "INSERT",
                "analysis_status": "analysed",
                "risk_level": "LOW",
                "confidence": "LIMITED",
            },
        ),
        (
            "INSERT IGNORE INTO t (a) VALUES (1)",
            {
                "statement_type": "INSERT",
                "analysis_status": "analysed",
                "risk_level": "LOW",
                "confidence": "LIMITED",
            },
        ),
        (
            "INSERT INTO t (a) SELECT id FROM users",
            {
                "statement_type": "INSERT",
                "analysis_status": "analysed",
                "risk_level": "LOW",
                "confidence": "LIMITED",
            },
        ),

        # -----------------------------------------------------------------
        # DML – Multi-table UPDATE / DELETE
        # -----------------------------------------------------------------
        (
            "UPDATE users u JOIN orders o ON o.user_id = u.id SET u.status = 'inactive' WHERE o.id = 5",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
            },
        ),
        (
            "DELETE u FROM users u JOIN orders o ON o.user_id = u.id WHERE u.id = 10",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
            },
        ),
        (
            "DELETE FROM users, orders USING users INNER JOIN orders ON orders.user_id = users.id WHERE users.id = 10",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
            },
        ),

        # -----------------------------------------------------------------
        # DDL – DROP / TRUNCATE
        # -----------------------------------------------------------------
        (
            "DROP TABLE users",
            {
                "statement_type": "DROP",
                "analysis_status": "analysed",
                "risk_level": "CRITICAL",
                "confidence": "HIGH",
                "score": 100,
            },
        ),
        (
            "DROP DATABASE shop",
            {
                "statement_type": "DROP",
                "analysis_status": "analysed",
                "risk_level": "CRITICAL",
                "confidence": "HIGH",
                "score": 100,
            },
        ),
        (
            "TRUNCATE TABLE users",
            {
                "statement_type": "TRUNCATE",
                "analysis_status": "analysed",
                "risk_level": "CRITICAL",
                "confidence": "HIGH",
                "score": 100,
            },
        ),

        # -----------------------------------------------------------------
        # DDL – ALTER / CREATE INDEX
        # -----------------------------------------------------------------
        (
            "ALTER TABLE users DROP COLUMN last_login",
            {
                "statement_type": "ALTER",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "HIGH",
            },
        ),
        (
            "ALTER TABLE users ADD INDEX idx_status (status)",
            {
                "statement_type": "ALTER",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "HIGH",
            },
        ),
        (
            "CREATE INDEX idx_status ON users (status)",
            {
                "statement_type": "CREATE",
                "analysis_status": "analysed",
                "risk_level": "LOW",
                "confidence": "HIGH",
            },
        ),

        # -----------------------------------------------------------------
        # Stored procedures / CALL / DELIMITER
        # -----------------------------------------------------------------
        (
            "CALL process_monthly_billing(2026, 8)",
            {
                "statement_type": "CALL",
                "analysis_status": "analysed",
                "risk_level": "LOW",
                "confidence": "LIMITED",
            },
        ),
        (
            "CREATE PROCEDURE p() SELECT 1",
            {
                "statement_type": "CREATE PROCEDURE",
                "analysis_status": "unsupported",
                "risk_level": "CRITICAL",
                "confidence": "LOW",
            },
        ),
        (
            "CREATE PROCEDURE p() BEGIN SELECT 1; END",
            {
                "error": "stored_program_not_supported",
            },
        ),
        (
            "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END //\nDELIMITER ;",
            {
                "error": "delimiter_not_supported",
            },
        ),

        # -----------------------------------------------------------------
        # Uncertain row scope (no metadata)
        # -----------------------------------------------------------------
        (
            "UPDATE users SET status = 'inactive' WHERE id IN (SELECT user_id FROM orders)",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
                "scope_basis": "not_established",
            },
        ),
        (
            "DELETE FROM users WHERE status IS NULL",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
                "scope_basis": "not_established",
            },
        ),
        (
            "UPDATE users SET status = 'inactive' WHERE id = 10",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
                "scope_basis": "not_established",
            },
        ),

        # -----------------------------------------------------------------
        # Not analysed / malformed / INTO OUTFILE
        # -----------------------------------------------------------------
        (
            "SELEC * FROM users",
            {
                "statement_type": "UNKNOWN",
                "analysis_status": "invalid",
                "risk_level": "CRITICAL",
                "confidence": "LOW",
                "score": 100,
            },
        ),
        (
            "UPDATE users SET status = 'x' WHERE id IN (1, 2",
            {
                "analysis_status": "unreadable",
                "risk_level": "CRITICAL",
                "confidence": "LOW",
                "score": 100,
            },
        ),
        (
            "SELECT * FROM orders INTO OUTFILE '/tmp/x'",
            {
                "risk_level": "CRITICAL",
                "confidence": "LOW",
                "score": 100,
                "allow_analysis_statuses": ["unreadable", "analysed"],
            },
        ),
    ],
)
def test_assessment_corpus(sql, expected):
    result = validate(sql)

    # Script-level refusal (DELIMITER, multi-statement stored programs, etc.)
    if "error" in expected:
        assert result == {"error": expected["error"]}
        return

    # Normal statement path
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
        assert result["scope"]["basis"] == expected["scope_basis"]analysis_status": "analysed",
                "risk_level": "LOW",
                "confidence": "LIMITED",
            },
        ),
        (
            "CREATE PROCEDURE p() SELECT 1",
            {
                "statement_type": "CREATE PROCEDURE",
                "analysis_status": "unsupported",
                "risk_level": "CRITICAL",
                "confidence": "LOW",
            },
        ),
        (
            "CREATE PROCEDURE p() BEGIN SELECT 1; END",
            {
                "error": "stored_program_not_supported",
            },
        ),
        (
            "DELIMITER //\nCREATE PROCEDURE p() BEGIN SELECT 1; END //\nDELIMITER ;",
            {
                "error": "delimiter_not_supported",
            },
        ),

        # -----------------------------------------------------------------
        # Uncertain row scope
        # -----------------------------------------------------------------
        (
            "UPDATE users SET status = 'inactive' WHERE id IN (SELECT user_id FROM orders)",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
                "score": 25,
                "scope_basis": "not_established",
            },
        ),
        (
            "DELETE FROM users WHERE status IS NULL",
            {
                "statement_type": "DELETE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
                "scope_basis": "not_established",
            },
        ),
        (
            "UPDATE users SET status = 'inactive' WHERE id = 10",
            {
                "statement_type": "UPDATE",
                "analysis_status": "analysed",
                "risk_level": "MEDIUM",
                "confidence": "LIMITED",
                "score": 20,
                "scope_basis": "not_established",
            },
        ),

        # -----------------------------------------------------------------
        # Not analysed / malformed / INTO OUTFILE
        # -----------------------------------------------------------------
        (
            "SELEC * FROM users",
            {
                "statement_type": "UNKNOWN",
                "analysis_status": "invalid",
                "risk_level": "CRITICAL",
                "confidence": "LOW",
                "score": 100,
            },
        ),
        (
            "UPDATE users SET status = 'x' WHERE id IN (1, 2",
            {
                "analysis_status": "unreadable",
                "risk_level": "CRITICAL",
                "confidence": "LOW",
                "score": 100,
            },
        ),
        (
            "SELECT * FROM orders INTO OUTFILE '/tmp/x'",
            {
                # Intentionally version-dependent (sqlglot may or may not read INTO fully)
                "risk_level": "CRITICAL",
                "confidence": "LOW",
                "score": 100,
                "allow_analysis_statuses": ["unreadable", "analysed"],
            },
        ),
    ],
)
def test_assessment_corpus(sql, expected):
    result = validate(sql)

    if "error" in expected:
        assert result == {"error": expected["error"]}
        return

    # Normal statement path
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
