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

from .parser import StatementFacts
from . import models
from .recommendations import recommendations_from_gaps
from .evidence import affected_row_count_gap, execution_plan_gap, generic_gap


# Static-view reason; backend.evidence_state restates it when evidence was attempted.
OFFLINE_EXECUTION_REASON = "Execution plan and row estimates unavailable in offline mode."


def _looks_key_scoped(facts: StatementFacts) -> bool:
    """Conservative heuristic copied from the V1.1 risk engine logic.

    Returns True when the WHERE predicate appears to target a single row
    via equality on an "id" or "*_id" column.
    """

    if not facts.columns_in_where:
        return False

    predicate = (facts.where_predicate_summary or "").lower()

    if "=" not in predicate:
        return False

    if " in " in f" {predicate} ":
        return False

    if "between" in predicate:
        return False

    for column in facts.columns_in_where:
        normalized = column.lower().strip()

        if normalized == "id":
            return True

        if normalized.endswith("_id"):
            return True

    return False


def _confidence_for_offline_analysis(facts: StatementFacts) -> str:
    if facts.parse_error:
        return "LOW"

    if facts.statement_type in {
        "DROP",
        "TRUNCATE",
        "CREATE",
        "ALTER",
        "GRANT",
        "REVOKE",
        "COMMIT",
        "ROLLBACK",
        "BEGIN",
        "START",
    }:
        return "HIGH"

    if facts.statement_type in {
        "UPDATE",
        "DELETE",
        "INSERT",
        "REPLACE",
        "CALL",
    }:
        return "LIMITED"

    return "HIGH"


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
    confidence = _confidence_for_offline_analysis(facts)

    findings: List[models.Finding] = []
    evidence_gaps: List[models.EvidenceGap] = []
    recommendations: List[models.Recommendation] = []
    checklist: List[str] = []
    dimensions: List[models.RiskDimension] = []

    # Helper for table-anchored suggested checks
    table_for_check: Optional[str] = tables[0] if tables else None

    # Parse error => immediate finding
    if facts.parse_error:
        findings.append(models.Finding(
            severity="CRITICAL",
            title="Parser error — cannot analyze",
            description=f"{facts.parse_error}",
        ))

        checklist.append("Fix SQL syntax before proceeding.")

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

        return analysis

    # DML: UPDATE/DELETE/REPLACE/INSERT
    is_write = stmt_type in {"UPDATE", "DELETE", "REPLACE", "INSERT"}

    if is_write:
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
                "Run a SELECT COUNT(*) using the same predicate.",
                "Review the execution plan before execution.",
            ])

            if table_for_check:
                evidence_gaps.append(affected_row_count_gap(table_for_check, facts.where_predicate_summary))
                evidence_gaps.append(execution_plan_gap(table_for_check, facts.where_predicate_summary, stmt_type=stmt_type))

        else:
            # Normal WHERE — still limited offline
            checklist.extend([
                "Verify the expected affected-row count before execution.",
                "Consider running the WHERE predicate as SELECT COUNT(*) first.",
                "Review the execution plan when database-backed analysis is available.",
            ])

            if not _looks_key_scoped(facts):
                findings.append(models.Finding(
                    severity="MEDIUM",
                    title="Affected-row count is unknown",
                    description=(
                        "The predicate appears bounded but the validator cannot determine "
                        "how many rows it will match without database metadata."
                    ),
                ))

                if table_for_check:
                    evidence_gaps.append(affected_row_count_gap(table_for_check, facts.where_predicate_summary))
                    evidence_gaps.append(execution_plan_gap(table_for_check, facts.where_predicate_summary, stmt_type=stmt_type))

            else:
                findings.append(models.Finding(
                    severity="LOW",
                    title="WHERE predicate appears key-scoped",
                    description=(
                        "The predicate looks like it targets a single row (e.g. equality on an id column). "
                        "This reduces expected impact but confirm with a SELECT COUNT(*) if unsure."
                    ),
                ))

                if table_for_check:
                    evidence_gaps.append(affected_row_count_gap(table_for_check, facts.where_predicate_summary))

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
        elif facts.where_looks_trivial:
            data_level = "MEDIUM"
            data_reason = "WHERE predicate appears broad."
        elif _looks_key_scoped(facts):
            data_level = "LOW"
            data_reason = "Predicate appears key-scoped."
        else:
            data_level = "MEDIUM"
            data_reason = "Affected-row count unknown without DB metadata."
    else:
        data_level = "LOW"
        data_reason = "Read-only or no data mutation detected."

    dimensions.append(models.RiskDimension(name="Data Risk", level=data_level, reason=data_reason))

    # Execution Risk
    if confidence != "HIGH":
        exec_level = "UNKNOWN"
        exec_reason = OFFLINE_EXECUTION_REASON
    else:
        exec_level = "LOW"
        exec_reason = "Offline analysis indicates low execution risk from statement alone."

    if stmt_type == "ALTER" and facts.is_full_table_ddl:
        exec_level = "MEDIUM"
        exec_reason = "ALTER dropping a column can be impactful on large tables."

    dimensions.append(models.RiskDimension(name="Execution Risk", level=exec_level, reason=exec_reason))

    # Scope Risk
    if len(tables) == 0:
        scope_level = "UNKNOWN"
        scope_reason = "Target table could not be determined from the SQL."
    elif len(tables) > 1:
        scope_level = "MEDIUM"
        scope_reason = "Multiple tables referenced; broader scope."
    else:
        if is_write and facts.has_where is False:
            scope_level = "HIGH"
            scope_reason = "Single-target write without WHERE may still affect all rows."
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

    # Prepare an initial conservative RiskAssessment (score 0, unknown until risk engine runs)
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
