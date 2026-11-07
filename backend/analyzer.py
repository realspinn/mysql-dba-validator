"""
Statement analyzer for V2.

Converts StatementFacts (from parser.py) into StatementAnalysis (models.py)
producing initial Findings, EvidenceGaps, RiskDimensions and Recommendations.

This module is intentionally additive and conservative: it does not change
existing risk scoring behavior and is safe to introduce while keeping
V1.1 tests passing.
"""

from __future__ import annotations

from typing import List, Optional

from .parser import NOT_ANALYSED_WORDING, UNREADABLE, StatementFacts
from . import models
from .recommendations import recommendations_from_gaps
from .evidence import affected_row_count_gap, execution_plan_gap, generic_gap
from .risk_engine import (
    CANDIDATE_ROWS_CHECK,
    LOCKING_READ_FINDING,
    LOCKING_READ_REASON,
    UNREVIEWED_CALL_FINDING,
    select_side_effects,
    static_confidence,
    unreviewed_call_reason,
)


# Static-view reason; backend.evidence_state restates it when evidence was attempted.
OFFLINE_EXECUTION_REASON = "Not assessed: no execution plan or row estimates are available in offline mode."


def analyze_statement(facts: StatementFacts) -> models.StatementAnalysis:
    """Produce a StatementAnalysis for a single StatementFacts.

    The analysis is deliberately conservative: findings and evidence gaps
    reflect what cannot be known offline and suggest safe verification
    queries for the DBA to run manually.
    """

    # Basic scope
    stmt_type = facts.statement_type
    tables = list(facts.tables or [])

    # Confidence in offline analysis
    confidence = static_confidence(facts)

    findings: List[models.Finding] = []
    evidence_gaps: List[models.EvidenceGap] = []
    recommendations: List[models.Recommendation] = []
    checklist: List[str] = []
    dimensions: List[models.RiskDimension] = []

    # Helper for table-anchored suggested checks
    table_for_check: Optional[str] = tables[0] if tables else None

    # Parse error => immediate finding
    if facts.parse_error:
        title, first_check = NOT_ANALYSED_WORDING.get(
            facts.analysis_status, NOT_ANALYSED_WORDING[UNREADABLE])
        findings.append(models.Finding(
            severity="CRITICAL",
            title=title,
            description=f"{facts.parse_error}",
        ))

        checklist.append(first_check)

        assessment = models.minimal_assessment(100, confidence="LOW")

        analysis = models.StatementAnalysis(
            raw_sql=facts.raw_sql,
            index=facts.index,
            statement_type=stmt_type,
            tables=tables,
            assessment=assessment,
        )

        # attach findings/evidence into assessment
        analysis.assessment.findings = findings
        analysis.assessment.evidence_missing = evidence_gaps
        analysis.assessment.recommendations = recommendations
        analysis.assessment.checklist = checklist
        # The risk engine reads parse_error from these facts (as in the normal path below).
        analysis.assessment.source_facts = facts

        return analysis

    # DML: UPDATE/DELETE/REPLACE/INSERT
    is_write = stmt_type in {"UPDATE", "DELETE", "REPLACE", "INSERT"}

    if is_write:
        if facts.has_where is False or facts.where_always_true:
            if facts.has_where is False:
                findings.append(models.Finding(
                    severity="HIGH",
                    title="No WHERE clause — potentially affects every row",
                    description=(
                        "This write operation has no WHERE clause and may modify all rows "
                        "in the target table(s). Verify the intention and estimate the "
                        "affected-row count before execution."
                    ),
                ))
            else:
                findings.append(models.Finding(
                    severity="HIGH",
                    title="WHERE clause is always true — matches every row",
                    description=(
                        f"The WHERE predicate ({facts.where_predicate_summary}) is true for every row, "
                        "so this write matches every row of the target table(s), exactly as with "
                        "no WHERE clause."
                    ),
                ))

            checklist.extend([
                "Confirm that affecting ALL rows is explicitly intended.",
                "Estimate the affected-row count before execution.",
                "Verify backup or point-in-time recovery capability.",
            ])

            # Evidence we cannot obtain offline
            evidence_gaps.append(affected_row_count_gap(table_for_check))

        elif facts.where_looks_trivial:
            findings.append(models.Finding(
                severity="MEDIUM",
                title="WHERE clause appears unusually broad",
                description=(
                    f"The WHERE predicate ({facts.where_predicate_summary}) may match most or all rows. "
                    "Confirm the predicate narrows the target rows and estimate the affected-row count."
                ),
            ))

            checklist.extend([
                "Verify that the WHERE predicate actually narrows the target rows.",
                CANDIDATE_ROWS_CHECK,
                "Review the execution plan before execution.",
            ])

            if table_for_check:
                evidence_gaps.append(affected_row_count_gap(table_for_check, facts.where_predicate_summary))
                evidence_gaps.append(execution_plan_gap(table_for_check, facts.where_predicate_summary, stmt_type=stmt_type))

        else:
            # Any other WHERE: a structural fact, not evidence of a narrow write.
            # Only table metadata can establish a unique-key scope (backend.row_scope).
            checklist.extend([
                "Verify the expected affected-row count before execution.",
                CANDIDATE_ROWS_CHECK,
                "Review the execution plan when database-backed analysis is available.",
            ])

            findings.append(models.Finding(
                severity="MEDIUM",
                title="Affected-row count is unknown",
                description=(
                    "A WHERE clause can match zero, one, some or all rows. The validator cannot "
                    "determine how many rows it matches from the SQL alone."
                ),
            ))

            if table_for_check:
                evidence_gaps.append(affected_row_count_gap(table_for_check, facts.where_predicate_summary))
                evidence_gaps.append(execution_plan_gap(table_for_check, facts.where_predicate_summary, stmt_type=stmt_type))

    # EXPLAIN / DESCRIBE of a statement other than a query. The validator never sends
    # these; the finding says what MySQL would do with the submitted text.
    explained = facts.explained_statement_type
    if stmt_type == "DESCRIBE" and facts.explain_analyze:
        findings.append(models.Finding(
            severity="HIGH" if explained not in ("SELECT",) else "MEDIUM",
            title="EXPLAIN ANALYZE runs the statement it explains",
            description=(
                f"MySQL executes the explained {explained or 'statement'} to time it, where it "
                "accepts EXPLAIN ANALYZE for that statement. Review it as that statement; "
                "this is scored as the statement it runs."
                if explained else
                "MySQL executes the explained statement to time it, and that statement could "
                "not be read. Its effect is unknown."
            ),
        ))
        checklist.append("Do not run EXPLAIN ANALYZE on a statement you would not run on its own.")
    elif stmt_type == "DESCRIBE" and explained not in (None, "SELECT"):
        findings.append(models.Finding(
            severity="MEDIUM",
            title=f"EXPLAIN of a {explained} statement",
            description=(
                f"EXPLAIN without ANALYZE plans the {explained} without running it, but the text "
                f"contains a {explained}: run without the EXPLAIN prefix, it changes data."
            ),
        ))
        checklist.append(f"Review the explained {explained} as if it will be run on its own.")

    # A query (or one an EXPLAIN plans or runs) that can lock rows, or that calls a
    # function whose effects were not established: not a plain read.
    select_locking, select_calls = select_side_effects(facts)
    if select_locking:
        findings.append(models.Finding(
            severity="MEDIUM",
            title=LOCKING_READ_FINDING,
            description=LOCKING_READ_REASON,
        ))
        checklist.append("Check the transaction this SELECT runs in and how long it holds its row locks.")
    if select_calls:
        findings.append(models.Finding(
            severity="MEDIUM",
            title=UNREVIEWED_CALL_FINDING,
            description=unreviewed_call_reason(facts, select_calls),
        ))
        checklist.append("Check what each called function does before running this SELECT.")

    # Stored-procedure calls
    if stmt_type == "CALL":
        findings.append(models.Finding(
            severity="MEDIUM",
            title="Stored procedure behavior unknown",
            description=(
                "The CALL statement does not expose what the procedure does. Inspect the procedure definition "
                "and identify which tables and objects it may modify."
            ),
        ))

        checklist.extend([
            "Inspect the stored procedure definition before execution.",
            "Identify which tables and objects the procedure may modify.",
        ])

    # DDL
    if stmt_type == "ALTER":
        if facts.is_full_table_ddl:
            findings.append(models.Finding(
                severity="HIGH",
                title="ALTER drops a column — potentially irreversible",
                description=(
                    "Dropping a column can permanently remove data. Verify that no code or downstream jobs depend on the column."
                ),
            ))
            checklist.append("Confirm no application code or downstream job depends on the column.")
        else:
            checklist.append("Review potential locking and application-impact concerns for the ALTER statement.")

    if stmt_type in {"DROP", "TRUNCATE"}:
        findings.append(models.Finding(
            severity="CRITICAL",
            title="Destructive DDL",
            description=(
                "This statement will remove database objects or data. Ensure explicit approval, backups, and maintenance windows."
            ),
        ))

        checklist.extend([
            "Verify the exact object being removed.",
            "Confirm the change is explicitly approved.",
            "Verify a recent backup or point-in-time recovery capability exists.",
        ])

    # Privilege changes
    if stmt_type in {"GRANT", "REVOKE"}:
        findings.append(models.Finding(
            severity="MEDIUM",
            title="Privilege change",
            description=(
                "This statement changes access privileges. Confirm it matches an approved access-control request."
            ),
        ))

        checklist.append("Confirm this privilege change matches an approved access-control request.")

    # Fill risk dimensions conservatively
    # Data Risk
    if is_write:
        if facts.has_where is False:
            data_level = "HIGH"
            data_reason = "No WHERE clause; may affect all rows."
        elif facts.where_always_true:
            data_level = "HIGH"
            data_reason = "WHERE clause is always true; matches every row."
        elif facts.where_looks_trivial:
            data_level = "MEDIUM"
            data_reason = "WHERE predicate appears broad."
        else:
            data_level = "MEDIUM"
            data_reason = "Row scope not established from the SQL."
    elif stmt_type == "DESCRIBE" and facts.explain_analyze and explained != "SELECT":
        data_level = "HIGH" if explained else "UNKNOWN"
        data_reason = (f"EXPLAIN ANALYZE runs the explained {explained}." if explained
                       else "EXPLAIN ANALYZE runs a statement that could not be read.")
    elif stmt_type == "DESCRIBE" and explained not in (None, "SELECT"):
        data_level = "MEDIUM"
        data_reason = f"Plans a {explained} without running it; the {explained} changes data if run on its own."
    elif select_calls:
        data_level = "UNKNOWN"
        data_reason = ("Calls a function whose effects were not established; a stored or loadable "
                       "function can change data." + (" It is also a locking read." if select_locking else ""))
    elif select_locking:
        data_level = "LOW"
        data_reason = "Does not change data, but it is a locking read: it can lock the rows it reads."
    else:
        data_level = "LOW"
        data_reason = "Read-only or no data mutation detected."

    dimensions.append(models.RiskDimension(name="Data Risk", level=data_level, reason=data_reason))

    # Execution Risk: not assessed from the SQL alone, whatever the confidence (confidence
    # is about the risk assessment, not about how the statement runs). With database
    # evidence, backend.evidence_state and main restate it from what was collected.
    exec_level = "UNKNOWN"
    exec_reason = OFFLINE_EXECUTION_REASON

    if stmt_type == "ALTER" and facts.is_full_table_ddl:
        exec_level = "MEDIUM"
        exec_reason = "ALTER dropping a column can be impactful on large tables."

    if select_locking:
        exec_level = "MEDIUM"
        exec_reason = ("Locking read: other transactions that update or lock the same rows may have "
                       "to wait while its locks are held.")

    dimensions.append(models.RiskDimension(name="Execution Risk", level=exec_level, reason=exec_reason))

    # Scope Risk
    if len(tables) == 0:
        scope_level = "UNKNOWN"
        scope_reason = "Target table could not be determined from the SQL."
    elif len(tables) > 1:
        scope_level = "MEDIUM"
        scope_reason = "Multiple tables referenced; broader scope."
    else:
        if is_write and (facts.has_where is False or facts.where_always_true):
            scope_level = "HIGH"
            scope_reason = "Single-target write without a restricting WHERE may affect all rows."
        else:
            scope_level = "LOW"
            scope_reason = "Targeting a single table."

    dimensions.append(models.RiskDimension(name="Scope Risk", level=scope_level, reason=scope_reason))

    # Schema Risk
    if stmt_type in {"ALTER", "DROP", "TRUNCATE"}:
        schema_level = "HIGH"
        schema_reason = "Schema-changing operation detected."
    elif stmt_type == "CREATE":
        schema_level = "LOW"
        schema_reason = "Creating objects usually low-risk for existing data."
    else:
        schema_level = "LOW"
        schema_reason = "No schema change detected."

    dimensions.append(models.RiskDimension(name="Schema Risk", level=schema_level, reason=schema_reason))

    # Privilege Risk
    if stmt_type in {"GRANT", "REVOKE"}:
        priv_level = "HIGH"
        priv_reason = "Access privileges are being modified."
    else:
        priv_level = "LOW"
        priv_reason = "No privilege changes detected."

    dimensions.append(models.RiskDimension(name="Privilege Risk", level=priv_level, reason=priv_reason))

    # Recommendations derived from evidence gaps
    recommendations.extend(recommendations_from_gaps(evidence_gaps))

    # Score and risk level are placeholders here; backend.validation_results fills them from the
    # final report before the assessment is returned (never 0 / UNKNOWN to a client).
    assessment = models.RiskAssessment(
        overall_score=0,
        risk_level="UNKNOWN",
        confidence=confidence,
        dimensions=dimensions,
        findings=findings,
        evidence_available=[],
        evidence_missing=evidence_gaps,
        recommendations=recommendations,
        checklist=checklist,
    )

    analysis = models.StatementAnalysis(
        raw_sql=facts.raw_sql,
        index=facts.index,
        statement_type=stmt_type,
        tables=tables,
        assessment=assessment,
    )

    # Attach the originating StatementFacts for downstream components
    # (non-serialized runtime attribute). This lets the risk engine
    # consume the richer parser facts when available without creating
    # a hard dependency or changing the dataclass schema.
    try:
        analysis.assessment.source_facts = facts
    except Exception:
        # Best-effort: do not let this attachment break analysis.
        pass

    return analysis


def analyze_batch(all_facts: List[StatementFacts]) -> List[models.StatementAnalysis]:
    return [analyze_statement(f) for f in all_facts]
