from dataclasses import replace
from types import SimpleNamespace

import backend.m2_scoring as m2_module
from backend.db_evidence import DatabaseEvidence
from backend.m2_scoring import M2_FACTOR_CODE, apply_m2_adjustment
from backend.parser import parse_sql
from backend.risk_engine import score_batch
from backend.metadata_scoring import apply_m1_adjustment
from backend.main import ValidateRequest, validate


def report(sql="UPDATE users SET active = 0 WHERE status = 'pending';"):
    return score_batch(parse_sql(sql))[0]


def state(kind, schema="app", table="users", **overrides):
    values = {
        "query_kind": kind,
        "schema": schema,
        "table": table,
        "status": "available",
        "complete": True,
        "potentially_truncated": False,
        "malformed": False,
        "table_association_unambiguous": True,
        "table_metadata_eligible": kind.lower() == "tables",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def metadata(rows=100000, indexes=None, **overrides):
    values = {
        "status": "available",
        "table_metadata": [{"schema": "app", "table": "users", "estimated_table_rows": rows}],
        "indexes": [] if indexes is None else indexes,
        "query_states": [state("tables"), state("statistics")],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def index(name, columns, schema="app", table="users", prefix_length=None, valid=True):
    return {
        "schema": schema,
        "table": table,
        "name": name,
        "columns": columns,
        "column_details": [
            {
                "name": column,
                "sequence": position,
                "prefix_length": prefix_length if position == 1 else None,
                "cardinality_estimate": 1,
            }
            for position, column in enumerate(columns, 1)
        ],
        "structure_valid": valid,
    }


def evidence(full_table_scan=True, explain_available=True):
    return DatabaseEvidence(
        available=explain_available,
        explain_available=explain_available,
        full_table_scan=full_table_scan,
    )


def score(item=None, metadata_value=None, evidence_value=None):
    return apply_m2_adjustment(
        item or report(),
        metadata_value or metadata(),
        evidence_value or evidence(),
        risk_level_for_score=lambda value: "CRITICAL" if value >= 80 else "HIGH" if value >= 50 else "MEDIUM" if value >= 20 else "LOW",
    )


def test_update_and_delete_positive_without_leading_index():
    for statement in (
        "UPDATE users SET active = 0 WHERE status = 'pending';",
        "DELETE FROM users WHERE status = 'pending';",
    ):
        result = score(report(statement), metadata(indexes=[index("idx_id", ["id"])]))
        assert result.adjustment == 5
        assert [factor.code for factor in result.factors] == [M2_FACTOR_CODE]


def test_select_critical_and_no_where_are_rejected():
    cases = [
        report("SELECT * FROM users WHERE status = 'pending';"),
        replace(report(), risk_level="CRITICAL"),
        report("UPDATE users SET active = 0;"),
    ]
    for item in cases:
        assert score(item).adjustment == 0


def test_joins_and_self_joins_are_rejected():
    for sql in [
        "UPDATE users AS u JOIN orders AS o ON u.id = o.user_id SET u.active = 0 WHERE u.id = 10;",
        "UPDATE users AS u JOIN users AS v ON u.id = v.id SET u.active = 0 WHERE u.id = 10;",
    ]:
        assert score(report(sql)).adjustment == 0


def test_unsupported_predicates_and_multiple_columns_are_rejected():
    for predicate in [
        "id = 1 OR id = 2",
        "id IN (1, 2)",
        "id BETWEEN 1 AND 10",
        "id = other_id",
    ]:
        assert score(report(f"UPDATE users SET active = 0 WHERE {predicate};")).adjustment == 0

    item = report()
    item.facts.predicate_columns = [item.facts.predicate_columns[0], item.facts.predicate_columns[0]]
    assert score(item).adjustment == 0


def test_ambiguous_or_mismatched_table_association_is_rejected():
    item = report()
    item.facts.predicate_table_association = "UNRESOLVED"
    assert score(item).adjustment == 0

    binding = item.facts.table_bindings[0]
    item.facts.table_bindings = [replace(binding, table="orders")]
    assert score(item).adjustment == 0


def test_incomplete_statistics_and_table_metadata_are_rejected():
    for overrides in [
        {"status": "unavailable"},
        {"complete": False},
        {"potentially_truncated": True},
        {"malformed": True},
    ]:
        assert score(metadata_value=metadata(query_states=[state("tables"), state("statistics", **overrides)])).adjustment == 0

    assert score(metadata_value=metadata(query_states=[state("tables", complete=False), state("statistics")])).adjustment == 0
    assert score(metadata_value=metadata(table_metadata=[])).adjustment == 0


def test_invalid_index_and_wrong_associations_are_rejected():
    cases = [
        [index("idx", ["id"], valid=False)],
        [index("idx", ["id"], schema="other")],
        [index("idx", ["id"], table="orders")],
    ]
    for indexes in cases:
        assert score(metadata_value=metadata(indexes=indexes)).adjustment == 0


def test_leading_index_matching_and_nonleading_cases():
    assert score(metadata_value=metadata(indexes=[index("idx", ["status", "id"])] )).adjustment == 0
    assert score(metadata_value=metadata(indexes=[index("idx", ["id", "status"])] )).adjustment == 5
    assert score(metadata_value=metadata(indexes=[index("idx", ["id", "status", "other"])] )).adjustment == 5
    assert score(metadata_value=metadata(indexes=[index("idx", ["status"])] )).adjustment == 0


def test_prefix_index_suppresses_m2():
    assert score(metadata_value=metadata(indexes=[index("idx", ["status"], prefix_length="4")])).adjustment == 0


def test_explain_gates_are_required():
    assert score(evidence_value=evidence(explain_available=False)).adjustment == 0
    assert score(evidence_value=evidence(full_table_scan=False)).adjustment == 0


def test_high_estimate_flag_does_not_duplicate_or_control_m2():
    result = score(evidence_value=evidence())
    assert result.adjustment == 5


def test_m1_and_m2_stack_and_metadata_factors_stay_separate():
    phase2 = report()
    md = metadata(rows=100000)
    m1 = apply_m1_adjustment(
        phase2,
        md,
        full_table_scan=True,
        high_estimated_rows_applied=False,
        risk_level_for_score=lambda value: "CRITICAL" if value >= 80 else "HIGH" if value >= 50 else "MEDIUM" if value >= 20 else "LOW",
    )
    m2 = apply_m2_adjustment(
        m1.report,
        md,
        evidence(),
        risk_level_for_score=lambda value: "CRITICAL" if value >= 80 else "HIGH" if value >= 50 else "MEDIUM" if value >= 20 else "LOW",
        phase2_risk_level=phase2.risk_level,
    )
    assert m1.metadata_score_adjustment == 5
    assert m2.adjustment == 5
    assert m2.report.score == phase2.score + 5 + 5


def test_m2_uses_phase2_critical_gate_not_post_m1_risk():
    phase2 = report()
    phase2 = replace(phase2, score=79, risk_level="HIGH")
    md = metadata(rows=100000)
    m1 = apply_m1_adjustment(
        phase2,
        md,
        full_table_scan=True,
        high_estimated_rows_applied=False,
        risk_level_for_score=lambda value: "CRITICAL" if value >= 80 else "HIGH" if value >= 50 else "MEDIUM" if value >= 20 else "LOW",
    )
    assert m1.report.risk_level == "CRITICAL"
    m2 = apply_m2_adjustment(
        m1.report,
        md,
        evidence(),
        risk_level_for_score=lambda value: "CRITICAL" if value >= 80 else "HIGH" if value >= 50 else "MEDIUM" if value >= 20 else "LOW",
        phase2_risk_level=phase2.risk_level,
    )
    assert m2.adjustment == 5


def test_score_is_clamped_and_m2_is_idempotent():
    item = replace(report(), score=98, risk_level="HIGH")
    clamped = score(item)
    assert clamped.adjustment == 5
    assert clamped.report.score == 100

    first = score()
    second = score(first.report)
    assert first.adjustment == 5
    assert second.adjustment == 5
    assert second.report.score == first.report.score
    assert len(second.factors) == 1


def test_m2_scorer_has_no_collection_or_scoring_dependencies():
    forbidden = {"db", "metadata", "parser", "risk_engine", "evidence_scoring", "metadata_scoring"}
    assert not forbidden.intersection(m2_module.__dict__)


def test_api_composes_m2_as_a_separate_metadata_factor(monkeypatch):
    monkeypatch.setattr(
        "backend.main.collect_statement_evidence",
        lambda sql: evidence(),
    )
    monkeypatch.setattr("backend.main.collect_metadata", lambda tables: metadata(rows=99999))

    statement = validate(
        ValidateRequest(sql="UPDATE users SET active = 0 WHERE status = 'pending';")
    )["statements"][0]

    assert statement["static_score"] == 20
    assert statement["score"] == 35
    assert statement["metadata_score_adjustment"] == 5
    assert [factor["code"] for factor in statement["metadata_factors"]] == [
        M2_FACTOR_CODE
    ]
    assert statement["factors"]
    assert statement["evidence_factors"]
    assert statement["high_estimated_rows_applied"] is False
