from dataclasses import replace
from types import SimpleNamespace

import backend.metadata_scoring as scoring
from backend.metadata_scoring import M1_FACTOR_CODE, apply_m1_adjustment
from backend.main import ValidateRequest, validate
from tests.local_evidence import local_request, use_fake_evidence_connection
from backend.parser import StatementFacts
from backend.risk_engine import score_statement


def make_report(statement_type="UPDATE", table="app.users", has_where=True):
    facts = StatementFacts(
        raw_sql=f"{statement_type} {table}",
        index=0,
        statement_type=statement_type,
        tables=[table],
        has_where=has_where,
    )
    return score_statement(facts, batch_size=1)


def make_metadata(table_rows=100000, **state_overrides):
    state_values = {
        "schema": "app",
        "table": "users",
        "query_kind": "TABLES",
        "status": "available",
        "complete": True,
        "potentially_truncated": False,
        "malformed": False,
        "table_association_unambiguous": True,
        "table_metadata_eligible": True,
    }
    state_values.update(state_overrides)
    state = SimpleNamespace(**state_values)
    return SimpleNamespace(
        status="available",
        query_states=[state],
        table_metadata=[
            {"schema": "app", "table": "users", "estimated_table_rows": table_rows}
        ],
    )


def score(report, metadata=None, full_table_scan=True, high_estimated_rows_applied=False):
    return apply_m1_adjustment(
        report,
        metadata or make_metadata(),
        full_table_scan=full_table_scan,
        high_estimated_rows_applied=high_estimated_rows_applied,
        risk_level_for_score=lambda value: "CRITICAL" if value >= 80 else "HIGH" if value >= 50 else "MEDIUM" if value >= 20 else "LOW",
    )


def test_update_and_delete_qualify_at_threshold_and_above():
    for statement_type in ("UPDATE", "DELETE"):
        for table_rows in (100000, 100001):
            result = score(make_report(statement_type), make_metadata(table_rows))
            assert result.metadata_score_adjustment == 5
            assert [factor.code for factor in result.metadata_factors] == [M1_FACTOR_CODE]


def test_m1_is_positive_only_and_requires_all_write_conditions():
    cases = [
        (make_report("SELECT"), make_metadata()),
        (make_report("INSERT"), make_metadata()),
        (make_report("REPLACE"), make_metadata()),
        (make_report("UPDATE", has_where=False), make_metadata()),
        (make_report("DELETE", has_where=False), make_metadata()),
        (replace(make_report(), risk_level="CRITICAL"), make_metadata()),
        (make_report(), make_metadata(), False, False),
        (make_report(), make_metadata(), True, True),
        (make_report(), make_metadata(99999)),
        (make_report(), make_metadata(0)),
        (make_report(), make_metadata(None)),
        (make_report(), make_metadata(-1)),
    ]
    for case in cases:
        result = score(*case)
        assert result.metadata_score_adjustment == 0
        assert result.metadata_factors == ()


def test_m1_rejects_missing_or_ambiguous_metadata_state():
    for overrides in [
        {"status": "unavailable"},
        {"complete": False},
        {"potentially_truncated": True},
        {"malformed": True},
        {"table_association_unambiguous": False},
        {"table_metadata_eligible": False},
    ]:
        assert score(make_report(), make_metadata(**overrides)).metadata_score_adjustment == 0

    assert score(make_report(), SimpleNamespace(status="available", query_states=[], table_metadata=[])).metadata_score_adjustment == 0
    assert score(make_report(), SimpleNamespace(status="available", query_states=make_metadata().query_states, table_metadata=[{}, {}])).metadata_score_adjustment == 0


def test_m1_requires_exact_table_association():
    metadata = make_metadata()
    metadata.query_states[0].schema = "other"
    assert score(make_report(), metadata).metadata_score_adjustment == 0

    metadata = make_metadata()
    metadata.table_metadata[0]["table"] = "other_users"
    assert score(make_report(), metadata).metadata_score_adjustment == 0

    report = make_report(table="users_alias")
    assert score(report, make_metadata()).metadata_score_adjustment == 0


def test_m1_preserves_phase2_fields_and_is_idempotent():
    report = make_report()
    result = score(report)
    second = score(result.report, make_metadata())
    assert result.report.score == report.score + 5
    assert second.report.score == result.report.score
    assert second.metadata_score_adjustment == 5
    assert len(second.metadata_factors) == 1
    assert result.report.factors == report.factors
    assert result.report.confidence == report.confidence


def test_m1_scorer_has_no_database_or_collection_dependencies():
    forbidden = {"db", "metadata", "parser", "risk_engine", "evidence_scoring"}
    assert not forbidden.intersection(scoring.__dict__)


def test_api_exposes_additive_m1_fields_without_merging_factors(monkeypatch):
    use_fake_evidence_connection(monkeypatch)
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql, **_kwargs: SimpleNamespace(
            available=True,
            explain_available=True,
            estimated_rows=500,
            access_type="ALL",
            possible_keys=[],
            key=None,
            key_length=None,
            extra="Using where",
            full_table_scan=True,
            database="app",
            tables=["users"],
            error=None,
        ),
    )
    monkeypatch.setattr("backend.main.collect_metadata",
                        lambda tables, **_kwargs: make_metadata(100000))

    statement = validate(
        local_request("UPDATE users SET active = 0 WHERE status = 'pending';")
    )["statements"][0]

    assert statement["static_score"] == 20
    assert statement["score"] == 35
    assert statement["metadata_score_adjustment"] == 5
    assert [factor["code"] for factor in statement["metadata_factors"]] == [M1_FACTOR_CODE]
    assert statement["factors"]
    assert statement["evidence_factors"]
    assert statement["high_estimated_rows_applied"] is False
