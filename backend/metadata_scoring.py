"""Pure M1 metadata-supported scoring adjustment."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable


M1_FACTOR_CODE = "M1_TABLE_ROWS_FULL_SCAN_WRITE"
M1_POINTS = 5
M1_TABLE_ROWS_THRESHOLD = 100_000


@dataclass(frozen=True)
class M1Factor:
    code: str
    label: str
    points: int


@dataclass(frozen=True)
class M1ScoreResult:
    report: Any
    metadata_score_adjustment: int = 0
    metadata_factors: tuple[M1Factor, ...] = ()


def _is_exact_table_association(report: Any, metadata: Any) -> bool:
    tables = getattr(report.facts, "tables", None)
    states = getattr(metadata, "query_states", None)
    rows = getattr(metadata, "table_metadata", None)
    if not isinstance(tables, list) or len(tables) != 1 or not isinstance(tables[0], str):
        return False
    if not isinstance(states, list) or not isinstance(rows, list) or len(rows) != 1:
        return False

    reference = tables[0].strip()
    parts = reference.split(".")
    if len(parts) == 1:
        statement_schema = None
        statement_table = parts[0]
    elif len(parts) == 2:
        statement_schema, statement_table = parts
    else:
        return False
    if not statement_table or (statement_schema is not None and not statement_schema):
        return False

    table_states = [
        state for state in states
        if isinstance(getattr(state, "query_kind", None), str)
        and getattr(state, "query_kind").upper() == "TABLES"
    ]
    if len(table_states) != 1:
        return False
    state = table_states[0]
    state_schema = getattr(state, "schema", None)
    state_table = getattr(state, "table", None)
    if not isinstance(state_schema, str) or not isinstance(state_table, str):
        return False
    if state_table != statement_table:
        return False
    if statement_schema is not None and state_schema != statement_schema:
        return False

    row = rows[0]
    if not isinstance(row, dict):
        return False
    row_schema = row.get("schema")
    row_table = row.get("table")
    if row_schema != state_schema or row_table != state_table:
        return False
    return True


def apply_m1_adjustment(
    phase2_report: Any,
    metadata: Any,
    *,
    full_table_scan: Any,
    high_estimated_rows_applied: Any,
    risk_level_for_score: Callable[[int], str],
) -> M1ScoreResult:
    """Apply M1 to a Phase 2 baseline, returning a separate metadata factor."""
    if getattr(phase2_report, "_m1_applied", False):
        return M1ScoreResult(
            phase2_report,
            getattr(phase2_report, "_m1_adjustment", 0),
            tuple(getattr(phase2_report, "_m1_factors", ())),
        )

    try:
        facts = phase2_report.facts
        if (
            facts.statement_type not in {"UPDATE", "DELETE"}
            or phase2_report.risk_level == "CRITICAL"
            or facts.has_where is not True
            or full_table_scan is not True
            or high_estimated_rows_applied is not False
            or getattr(metadata, "status", None) != "available"
            or not _is_exact_table_association(phase2_report, metadata)
        ):
            return M1ScoreResult(phase2_report)

        states = [
            state for state in metadata.query_states
            if isinstance(getattr(state, "query_kind", None), str)
            and getattr(state, "query_kind").upper() == "TABLES"
        ]
        state = states[0]
        if not (
            getattr(state, "status", None) == "available"
            and getattr(state, "complete", None) is True
            and getattr(state, "potentially_truncated", None) is False
            and getattr(state, "malformed", None) is False
            and getattr(state, "table_association_unambiguous", None) is True
            and getattr(state, "table_metadata_eligible", None) is True
        ):
            return M1ScoreResult(phase2_report)

        table_rows = metadata.table_metadata[0].get("estimated_table_rows")
        if (
            not isinstance(table_rows, int)
            or isinstance(table_rows, bool)
            or table_rows < M1_TABLE_ROWS_THRESHOLD
        ):
            return M1ScoreResult(phase2_report)

        adjustment = M1_POINTS
        final_score = max(0, min(100, phase2_report.score + adjustment))
        factor = M1Factor(
            code=M1_FACTOR_CODE,
            label="Metadata supports a large table full-scan write adjustment",
            points=adjustment,
        )
        final_report = replace(
            phase2_report,
            score=final_score,
            risk_level=risk_level_for_score(final_score),
        )
        setattr(final_report, "_m1_applied", True)
        setattr(final_report, "_m1_adjustment", adjustment)
        setattr(final_report, "_m1_factors", (factor,))
        return M1ScoreResult(final_report, adjustment, (factor,))
    except Exception:
        return M1ScoreResult(phase2_report)
