from backend.db_evidence import DatabaseEvidence
from backend.evidence_scoring import apply_evidence_adjustments
from backend.main import ValidateRequest, validate
from backend.metadata import MetadataEvidence
from tests.local_evidence import local_request, use_fake_evidence_connection
from backend.parser import parse_sql
from backend.risk_engine import score_batch


def static_report(sql: str):
    return score_batch(parse_sql(sql))[0]


def evidence(**kwargs):
    return DatabaseEvidence(available=True, explain_available=True, **kwargs)


def test_unavailable_update_evidence_is_an_exact_static_noop():
    report = static_report("UPDATE users SET active = 0 WHERE status = 'pending';")
    adjusted = apply_evidence_adjustments(report, DatabaseEvidence())
    assert adjusted.report.score == report.score == 20
    assert adjusted.report.risk_level == report.risk_level == "MEDIUM"
    assert adjusted.factors == []
    assert adjusted.report.confidence == report.confidence == "LIMITED"
    assert adjusted.high_estimated_rows_applied is False


def test_unavailable_delete_evidence_is_an_exact_static_noop():
    report = static_report("DELETE FROM users WHERE status = 'inactive';")
    adjusted = apply_evidence_adjustments(report, DatabaseEvidence())
    assert adjusted.report.score == report.score == 25
    assert adjusted.report.risk_level == report.risk_level == "MEDIUM"
    assert adjusted.factors == []


def test_unavailable_select_evidence_preserves_static_behavior():
    report = static_report("SELECT * FROM users WHERE id = 1;")
    adjusted = apply_evidence_adjustments(report, DatabaseEvidence())
    assert adjusted.report.score == report.score == 0
    assert adjusted.report.risk_level == report.risk_level == "LOW"
    assert adjusted.report.confidence == report.confidence == "HIGH"


def test_successful_select_explain_is_evidence_assisted_without_score_change():
    report = static_report("SELECT * FROM users WHERE id = 1;")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="ref", key="PRIMARY", estimated_rows=1, full_table_scan=False),
    )
    assert adjusted.report.score == report.score == 0
    # Evidence never changes confidence; the evidence state reports what was collected.
    assert adjusted.report.confidence == report.confidence == "HIGH"
    assert adjusted.materially_supports_analysis is True


def test_update_full_scan_adds_bounded_evidence_factor():
    report = static_report("UPDATE users SET active = 0 WHERE status = 'pending';")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="ALL", full_table_scan=True, estimated_rows=500),
    )
    assert adjusted.report.score == 30
    assert adjusted.report.risk_level == "MEDIUM"
    # An EXPLAIN estimate is not an affected-row count: DML confidence stays LIMITED.
    assert adjusted.report.confidence == report.confidence == "LIMITED"
    assert any(f.points == 10 and "full table scan" in f.label.lower() for f in adjusted.factors)
    assert any("EXPLAIN indicates a full table scan" in finding for finding in adjusted.report.findings)


def test_delete_full_scan_adds_bounded_evidence_factor():
    report = static_report("DELETE FROM users WHERE status = 'inactive';")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="ALL", full_table_scan=True, estimated_rows=500),
    )
    assert adjusted.report.score == 35
    assert any("full table scan" in f.label.lower() for f in adjusted.factors)


def test_high_estimated_rows_adds_estimate_factor_without_claiming_exact_count():
    report = static_report("UPDATE users SET active = 0 WHERE status = 'pending';")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="range", estimated_rows=10000, full_table_scan=False),
    )
    assert adjusted.report.score == 30
    assert any(f.points == 10 and "estimated rows" in f.label.lower() for f in adjusted.factors)
    assert any("optimizer estimates" in finding for finding in adjusted.report.findings)
    assert not any("will affect 10000" in finding for finding in adjusted.report.findings)
    assert adjusted.high_estimated_rows_applied is True


def test_high_estimate_is_not_a_dml_adjustment_for_select():
    report = static_report("SELECT * FROM users;")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="ALL", estimated_rows=10000, full_table_scan=False),
    )
    assert adjusted.report.score == report.score == 0
    assert adjusted.high_estimated_rows_applied is False
    assert not any("estimated rows are high" in f.label.lower() for f in adjusted.factors)


def test_indexed_access_gets_only_a_modest_reduction():
    report = static_report("UPDATE users SET active = 0 WHERE status = 'pending';")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="ref", key="idx_users_status", estimated_rows=7, full_table_scan=False),
    )
    assert adjusted.report.score == 17
    assert any(f.points == -3 and "indexed" in f.label.lower() for f in adjusted.factors)


def test_ambiguous_evidence_does_not_reduce_risk():
    report = static_report("UPDATE users SET active = 0 WHERE status = 'pending';")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="ref", key=None, estimated_rows=None, full_table_scan=None),
    )
    assert adjusted.report.score == report.score
    assert adjusted.factors == []
    assert adjusted.report.confidence == report.confidence


def test_delete_high_estimate_adds_only_estimate_factor():
    report = static_report("DELETE FROM users WHERE status = 'inactive';")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="range", estimated_rows=10000, full_table_scan=False),
    )
    assert adjusted.report.score == 35
    assert len(adjusted.factors) == 1
    assert adjusted.factors[0].points == 10


def test_delete_indexed_access_gets_modest_reduction():
    report = static_report("DELETE FROM users WHERE status = 'inactive';")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="eq_ref", key="PRIMARY", estimated_rows=1, full_table_scan=False),
    )
    assert adjusted.report.score == 22
    assert adjusted.factors[0].points == -3


def test_combined_write_evidence_has_two_explicit_adjustments():
    report = static_report("UPDATE users SET active = 0 WHERE status = 'pending';")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="ALL", estimated_rows=10000, full_table_scan=True),
    )
    assert adjusted.report.score == 40
    assert [factor.points for factor in adjusted.factors] == [10, 10]
    assert adjusted.report.score == adjusted.static_score + sum(
        factor.points for factor in adjusted.factors
    )


def test_score_adjustment_is_clamped_to_100():
    report = static_report("UPDATE users SET active = 0 WHERE status = 1;")
    adjusted = apply_evidence_adjustments(
        report,
        evidence(access_type="ALL", estimated_rows=10000, full_table_scan=True),
    )
    assert 0 <= adjusted.report.score <= 100


def test_evidence_cannot_reduce_hard_critical_or_no_where_writes():
    for sql in ["DROP TABLE users;", "TRUNCATE TABLE users;", "UPDATE users SET active = 0;", "DELETE FROM users;"]:
        report = static_report(sql)
        adjusted = apply_evidence_adjustments(
            report,
            evidence(access_type="const", key="PRIMARY", estimated_rows=1, full_table_scan=False),
        )
        assert adjusted.report.score == 100
        assert adjusted.report.risk_level == "CRITICAL"


def test_api_exposes_static_final_and_evidence_scores_additively(monkeypatch):
    use_fake_evidence_connection(monkeypatch)
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql, **_kwargs: evidence(
            access_type="ALL",
            full_table_scan=True,
            estimated_rows=12000,
        ),
    )
    monkeypatch.setattr("backend.main.collect_metadata",
                        lambda tables, **_kwargs: MetadataEvidence(status="unavailable"))
    result = validate(local_request("UPDATE users SET active = 0 WHERE status = 'pending';"))
    statement = result["statements"][0]
    assert statement["static_score"] == 20
    assert statement["static_risk_level"] == "MEDIUM"
    assert statement["score"] == 40
    assert statement["high_estimated_rows_applied"] is True
    assert all("EXPLAIN" not in factor["label"] for factor in statement["factors"])
    assert all("EXPLAIN" in factor["label"] for factor in statement["evidence_factors"])
    assert statement["risk_level"] == "MEDIUM"
    assert statement["database_evidence"]["estimated_rows_semantics"] == (
        "optimizer estimate, not exact affected-row count"
    )
    assert statement["evidence_factors"]
    assert result["analysis_mode"] == "DATABASE_EVIDENCE"
