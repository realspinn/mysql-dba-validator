"""Pure M2 metadata-supported scoring adjustment."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


M2_FACTOR_CODE = "M2_FULL_SCAN_NO_LEADING_INDEX"
M2_POINTS = 5
M2_PREDICATE_SHAPE = "DIRECT_COLUMN_LITERAL_EQUALITY"


@dataclass(frozen=True)
class M2Factor:
    code: str
    label: str
    points: int


@dataclass(frozen=True)
class M2ScoreResult:
    report: Any
    adjustment: int = 0
    factors: tuple[M2Factor, ...] = ()


def _query_state(metadata: Any, kind: str, schema: str, table: str) -> Any:
    states = getattr(metadata, "query_states", None)
    if not isinstance(states, list):
        return None
    matching = [
        state for state in states
        if (
            isinstance(getattr(state, "query_kind", None), str)
            and getattr(state, "query_kind").upper() == kind
            and getattr(state, "schema", None) == schema
            and getattr(state, "table", None) == table
        )
    ]
    return matching[0] if len(matching) == 1 else None


def _resolve_schema(metadata: Any, binding: Any) -> str | None:
    schema = getattr(binding, "schema", None)
    table = getattr(binding, "table", None)
    if not isinstance(table, str) or not table:
        return None
    if isinstance(schema, str) and schema:
        return schema
    states = getattr(metadata, "query_states", None)
    if not isinstance(states, list):
        return None
    matching = [
        state for state in states
        if (
            isinstance(getattr(state, "query_kind", None), str)
            and getattr(state, "query_kind").upper() == "TABLES"
            and getattr(state, "table", None) == table
            and isinstance(getattr(state, "schema", None), str)
        )
    ]
    schemas = {state.schema for state in matching}
    return next(iter(schemas)) if len(schemas) == 1 else None


def _table_metadata_is_eligible(metadata: Any, schema: str, table: str) -> bool:
    state = _query_state(metadata, "TABLES", schema, table)
    rows = getattr(metadata, "table_metadata", None)
    if state is None or not isinstance(rows, list) or len(rows) != 1:
        return False
    if not (
        getattr(state, "status", None) == "available"
        and getattr(state, "complete", None) is True
        and getattr(state, "potentially_truncated", None) is False
        and getattr(state, "malformed", None) is False
        and getattr(state, "table_association_unambiguous", None) is True
        and getattr(state, "table_metadata_eligible", None) is True
    ):
        return False
    row = rows[0]
    return (
        isinstance(row, dict)
        and row.get("schema") == schema
        and row.get("table") == table
    )


def _statistics_is_eligible(metadata: Any, schema: str, table: str) -> bool:
    state = _query_state(metadata, "STATISTICS", schema, table)
    indexes = getattr(metadata, "indexes", None)
    if state is None or not isinstance(indexes, list):
        return False
    if not (
        getattr(state, "status", None) == "available"
        and getattr(state, "complete", None) is True
        and getattr(state, "potentially_truncated", None) is False
        and getattr(state, "malformed", None) is False
        and getattr(state, "table_association_unambiguous", None) is True
    ):
        return False
    return all(
        isinstance(index, dict)
        and index.get("schema") == schema
        and index.get("table") == table
        and index.get("structure_valid") is True
        for index in indexes
    )


def _predicate_belongs_to_binding(facts: Any, binding: Any) -> bool:
    columns = getattr(facts, "predicate_columns", None)
    if not isinstance(columns, list) or len(columns) != 1:
        return False
    column = columns[0]
    resolved_table = getattr(column, "resolved_table", None)
    if resolved_table not in {binding.table, f"{binding.schema}.{binding.table}"}:
        return False
    column_schema = getattr(column, "schema", None)
    if column_schema is not None and column_schema != binding.schema:
        return False
    return True


def _has_matching_leading_index(metadata: Any, schema: str, table: str, column_name: str) -> bool | None:
    indexes = getattr(metadata, "indexes", None)
    if not isinstance(indexes, list):
        return None
    for index in indexes:
        if not isinstance(index, dict):
            return None
        if index.get("schema") != schema or index.get("table") != table:
            return None
        if index.get("structure_valid") is not True:
            return None
        details = index.get("column_details")
        if not isinstance(details, list):
            return None
        leading = [detail for detail in details if isinstance(detail, dict) and detail.get("sequence") == 1]
        if len(leading) != 1:
            return None
        detail = leading[0]
        if detail.get("name") == column_name:
            if detail.get("prefix_length") is not None:
                return True
            return True
    return False


def apply_m2_adjustment(
    phase2_report: Any,
    metadata: Any,
    evidence: Any,
    *,
    risk_level_for_score: Any,
    phase2_risk_level: str | None = None,
) -> M2ScoreResult:
    """Apply the frozen M2 +5 rule to the supplied post-M1 report."""
    if getattr(phase2_report, "_m2_applied", False):
        return M2ScoreResult(
            phase2_report,
            getattr(phase2_report, "_m2_adjustment", 0),
            tuple(getattr(phase2_report, "_m2_factors", ())),
        )

    try:
        facts = phase2_report.facts
        if (
            facts.statement_type not in {"UPDATE", "DELETE"}
            or (phase2_risk_level or phase2_report.risk_level) == "CRITICAL"
            or facts.has_where is not True
            or len(getattr(facts, "table_bindings", [])) != 1
            or facts.predicate_shape != M2_PREDICATE_SHAPE
            or facts.predicate_table_association != "UNAMBIGUOUS"
            or getattr(metadata, "status", None) != "available"
            or getattr(evidence, "explain_available", None) is not True
            or getattr(evidence, "full_table_scan", None) is not True
        ):
            return M2ScoreResult(phase2_report)

        binding = facts.table_bindings[0]
        schema = _resolve_schema(metadata, binding)
        table = getattr(binding, "table", None)
        if not isinstance(schema, str) or not isinstance(table, str) or not table:
            return M2ScoreResult(phase2_report)
        if not _predicate_belongs_to_binding(facts, binding):
            return M2ScoreResult(phase2_report)
        if not _table_metadata_is_eligible(metadata, schema, table):
            return M2ScoreResult(phase2_report)
        if not _statistics_is_eligible(metadata, schema, table):
            return M2ScoreResult(phase2_report)

        column_name = facts.predicate_columns[0].name
        matching = _has_matching_leading_index(metadata, schema, table, column_name)
        if matching is None or matching is True:
            return M2ScoreResult(phase2_report)

        final_score = max(0, min(100, phase2_report.score + M2_POINTS))
        factor = M2Factor(
            code=M2_FACTOR_CODE,
            label="Metadata supports a broad full-table scan with no matching leading index",
            points=M2_POINTS,
        )
        final_report = replace(
            phase2_report,
            score=final_score,
            risk_level=risk_level_for_score(final_score),
        )
        setattr(final_report, "_m2_applied", True)
        setattr(final_report, "_m2_adjustment", M2_POINTS)
        setattr(final_report, "_m2_factors", (factor,))
        return M2ScoreResult(final_report, M2_POINTS, (factor,))
    except Exception:
        return M2ScoreResult(phase2_report)
