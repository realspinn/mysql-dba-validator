"""
Evidence helpers

Centralize creation of EvidenceGap objects used by analyzer.py so the
analysis logic stays clear and easy to maintain.
"""
from __future__ import annotations

from typing import Optional, List

from . import models


def affected_row_count_gap(table: Optional[str], predicate: Optional[str] = None) -> models.EvidenceGap:
    """Return an EvidenceGap suggesting a SELECT COUNT(*) check for the target.

    If table is provided, include a concrete SQL snippet; otherwise return a
    generic suggestion.
    """

    if table:
        if predicate:
            suggested = f"SELECT COUNT(*) FROM {table} WHERE {predicate};"
        else:
            suggested = f"SELECT COUNT(*) FROM {table};"
    else:
        if predicate:
            suggested = "Run SELECT COUNT(*) with the intended WHERE predicate against the target table."
        else:
            suggested = "Run SELECT COUNT(*) against the target table."

    return models.EvidenceGap(
        name="affected_row_count",
        reason="No database connection",
        suggested_check=suggested,
    )


def execution_plan_gap(table: Optional[str], predicate: Optional[str] = None, stmt_type: str = "UPDATE") -> models.EvidenceGap:
    """Return an EvidenceGap suggesting an EXPLAIN check for the statement.

    The suggested SQL will be a best-effort human-readable hint and not
    intended for direct execution without minor edits (e.g. the "SET ..."
    placeholder remains for clarity where necessary).
    """

    if table and predicate:
        suggested = f"EXPLAIN {stmt_type} {table} SET ... WHERE {predicate};"
    elif table:
        suggested = f"EXPLAIN {stmt_type} {table} ...;"
    else:
        suggested = "Run EXPLAIN on the statement to inspect the execution plan."

    return models.EvidenceGap(
        name="execution_plan",
        reason="No database connection",
        suggested_check=suggested,
    )


def generic_gap(name: str, reason: str, suggested_check: Optional[str] = None) -> models.EvidenceGap:
    return models.EvidenceGap(name=name, reason=reason, suggested_check=suggested_check)
