from backend import models
from backend.db_evidence import DatabaseEvidence
from backend.metadata import MetadataEvidence
from backend.parser import parse_sql
from backend.analyzer import analyze_batch
from backend.recommendations import recommendations_from_gaps
from backend import evidence as evidence_mod
from backend.main import MAX_SQL_BYTES, MAX_STATEMENTS, ValidateRequest, validate
from types import SimpleNamespace
from tests.local_evidence import local_request, use_fake_evidence_connection


def test_minimal_assessment():
    a = models.minimal_assessment(10, confidence="LIMITED")
    assert isinstance(a, models.RiskAssessment)
    assert a.overall_score == 10
    assert a.risk_level == "LOW"
    assert a.confidence == "LIMITED"


def test_recommendations_from_gaps():
    gap = models.EvidenceGap(name="affected_row_count", reason="No database connection", suggested_check="SELECT COUNT(*) FROM foo;")
    recs = recommendations_from_gaps([gap])
    assert len(recs) == 1
    assert recs[0].text == gap.suggested_check
    assert recs[0].rationale == gap.reason


def test_evidence_helpers():
    g1 = evidence_mod.affected_row_count_gap("users", "id = 1")
    assert g1.name == "affected_row_count"
    assert "SELECT COUNT(*) FROM users WHERE id = 1" in g1.suggested_check

    g2 = evidence_mod.execution_plan_gap("orders", "created_at <= '2026-01-01'", stmt_type="UPDATE")
    assert g2.name == "execution_plan"
    assert "EXPLAIN UPDATE orders" in g2.suggested_check


def test_analyzer_produces_assessment_and_gaps():
    sql = "UPDATE users SET name = 'x' WHERE id = 1;"
    facts = parse_sql(sql)
    analyses = analyze_batch(facts)
    assert len(analyses) == 1
    a = analyses[0]
    assert a.assessment is not None
    # key-scoped where should produce at least one finding and one evidence gap
    assert any(isinstance(f, models.Finding) for f in a.assessment.findings)
    assert any(g.name == "affected_row_count" for g in a.assessment.evidence_missing)


def test_api_returns_v2_assessment():
    req = ValidateRequest(sql="UPDATE users SET name = 'x' WHERE id = 1;")
    resp = validate(req)
    assert "statements" in resp
    assert len(resp["statements"]) == 1
    stmt = resp["statements"][0]
    assert "v2_assessment" in stmt
    v2 = stmt["v2_assessment"]
    assert "overall_score" in v2
    assert "risk_level" in v2
    assert "confidence" in v2


def test_single_statement_aggregate_matches_final_statement(monkeypatch):
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql: DatabaseEvidence(available=False, error="database_unavailable"),
    )
    result = validate(ValidateRequest(sql="DROP TABLE users;"))
    statement = result["statements"][0]

    assert result["overall_score"] == statement["score"] == 100
    assert result["overall_risk_level"] == statement["risk_level"] == "CRITICAL"


def test_mixed_batch_aggregate_uses_maximum_final_score(monkeypatch):
    use_fake_evidence_connection(monkeypatch)
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql, **_kwargs: DatabaseEvidence(
            available=True,
            explain_available=True,
            estimated_rows=500,
            access_type="ALL",
            full_table_scan=True,
            tables=["users"],
        ),
    )
    metadata = SimpleNamespace(
        status="available",
        table_metadata=[{"schema": "app", "table": "users", "estimated_table_rows": 100000}],
        indexes=[],
        query_states=[
            SimpleNamespace(
                query_kind="tables",
                schema="app",
                table="users",
                status="available",
                complete=True,
                potentially_truncated=False,
                malformed=False,
                table_association_unambiguous=True,
                table_metadata_eligible=True,
            ),
            SimpleNamespace(
                query_kind="statistics",
                schema="app",
                table="users",
                status="available",
                complete=True,
                potentially_truncated=False,
                malformed=False,
                table_association_unambiguous=True,
            ),
        ],
    )
    monkeypatch.setattr("backend.main.collect_metadata", lambda tables, **_kwargs: metadata)

    result = validate(local_request(
        "SELECT * FROM users; "
        "UPDATE users SET active = 0 WHERE status = 'pending'; "
        "DROP TABLE users;"
    ))
    statements = result["statements"]

    assert result["statement_count"] == 3
    assert statements[1]["score"] == 50
    assert statements[1]["metadata_score_adjustment"] == 10
    assert result["overall_score"] == 100
    assert result["overall_risk_level"] == "CRITICAL"


def test_batch_without_critical_uses_maximum_final_score(monkeypatch):
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql: DatabaseEvidence(available=False, error="database_unavailable"),
    )
    result = validate(ValidateRequest(
        sql="SELECT * FROM users; UPDATE users SET active = 0;"
    ))

    assert result["overall_score"] == max(statement["score"] for statement in result["statements"])
    assert result["overall_risk_level"] == max(
        result["statements"], key=lambda statement: statement["score"]
    )["risk_level"]


def test_sql_size_limit_accepts_below_and_exact_boundary(monkeypatch):
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql: DatabaseEvidence(available=False, error="database_unavailable"),
    )
    below = "SELECT " + (" " * (MAX_SQL_BYTES - len("SELECT 1") - 1)) + "1"
    exact = "SELECT " + (" " * (MAX_SQL_BYTES - len("SELECT 1"))) + "1"

    assert len(below.encode("utf-8")) == MAX_SQL_BYTES - 1
    assert len(exact.encode("utf-8")) == MAX_SQL_BYTES
    assert "statements" in validate(ValidateRequest(sql=below))
    assert "statements" in validate(ValidateRequest(sql=exact))


def test_sql_size_limit_rejects_before_parser_or_db(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.main.parse_sql", lambda sql: calls.append("parse"))
    monkeypatch.setattr("backend.main.collect_statement_evidence", lambda sql: calls.append("evidence"))
    oversized = "SELECT " + ("x" * MAX_SQL_BYTES)

    result = validate(ValidateRequest(sql=oversized))

    assert result == {"error": "request_too_large"}
    assert calls == []
    assert oversized not in str(result)


def test_statement_limit_accepts_exact_boundary_and_rejects_above_before_db(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql: calls.append("evidence") or DatabaseEvidence(available=False),
    )
    monkeypatch.setattr(
        "backend.main.collect_metadata",
        lambda tables: calls.append("metadata") or MetadataEvidence(status="unavailable"),
    )
    exact = " ".join(["SELECT 1;"] * MAX_STATEMENTS)
    over = " ".join(["SELECT 1;"] * (MAX_STATEMENTS + 1))

    accepted = validate(ValidateRequest(sql=exact))
    calls.clear()
    rejected = validate(ValidateRequest(sql=over))

    assert accepted["statement_count"] == MAX_STATEMENTS
    assert rejected == {"error": "statement_limit_exceeded"}
    assert calls == []
    assert over not in str(rejected)


def test_statement_count_uses_parser_boundaries_not_semicolons(monkeypatch):
    result = validate(ValidateRequest(sql="SELECT ';' /* ; */; SELECT 1;"))

    assert result["statement_count"] == 2


def test_empty_and_malformed_sql_preserve_existing_behavior():
    assert validate(ValidateRequest(sql="   "))["statement_count"] == 0
    malformed = validate(ValidateRequest(sql="SELECT ("))
    assert malformed["statement_count"] == 1
    assert "statements" in malformed
