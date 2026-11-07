"""Statement and batch result construction for a validation.

Shared by local /api/validate (backend.main) and the connector's company validate
(connector.server), so equivalent evidence yields identical scores, confidence and
evidence semantics on both paths. Pure result assembly: no FastAPI, no environment
settings, no database access; collection is the caller's job.
"""

from __future__ import annotations

from dataclasses import asdict, replace

from .db_evidence import PLAN_NOT_ELIGIBLE, DatabaseEvidence, evidence_status
from .evidence_scoring import apply_evidence_adjustments, plan_is_interpretable
from .evidence_state import (
    EvidenceConnection,
    analysis_mode as derive_analysis_mode,
    build_evidence_state,
    restate_assessment,
    restate_reasons,
    summarize as summarize_evidence,
)
from .m2_scoring import apply_m2_adjustment, describe_filter_index
from .metadata import MetadataEvidence
from .metadata_scoring import apply_m1_adjustment
from .optimization import advise
from .parser import has_executable_comment, unsupported_script
from .risk_engine import (
    OFFLINE_ROW_IMPACT_REASON,
    CONFIDENCE_ORDER,
    ScoreFactor,
    weakest_confidence,
)
from .row_scope import AT_MOST_ONE_ROW, build_row_scope

MAX_STATEMENTS = 16


def script_error(sql: str, all_facts: list) -> str | None:
    """The error code when a request has nothing to assess or cannot be split into
    statements reliably; None when its statements can be assessed one by one.
    Shared by local /api/validate and the connector's remote validate.

    - "executable_comment_not_supported": an executable comment ("/*! ... */"), whose
      contents MySQL runs but the parser reads as a comment. Checked first: a script
      whose only statement is in such a comment is not "no_statements".
    - "no_statements": empty, whitespace-only or comment-only input. There is nothing
      to assess, so no risk level or confidence is returned (never "LOW").
    - "delimiter_not_supported" / "stored_program_not_supported": see
      backend.parser.unsupported_script.
    - "statement_limit_exceeded": more than MAX_STATEMENTS statements.
    """
    if has_executable_comment(sql):
        return "executable_comment_not_supported"
    if not all_facts:
        return "no_statements"
    unsupported = unsupported_script(sql)
    if unsupported is not None:
        return unsupported
    if len(all_facts) > MAX_STATEMENTS:
        return "statement_limit_exceeded"
    return None


def risk_level_from_score(score: int) -> str:

    if score >= 80:
        return "CRITICAL"

    if score >= 50:
        return "HIGH"

    if score >= 20:
        return "MEDIUM"

    return "LOW"


def batch_result(statements: list[dict], connection: EvidenceConnection) -> dict:
    """The validation response for scored statements (shared with the connector).

    analysis_mode is a compatibility label derived from the statements' evidence
    states; evidence_summary and each statement's evidence are authoritative.
    """
    evidence_states = [statement["evidence"] for statement in statements]
    aggregate_statement = max(
        statements,
        key=lambda statement: statement["score"],
        default=None,
    )
    overall_score = aggregate_statement["score"] if aggregate_statement else 0
    overall_risk_level = (
        aggregate_statement["risk_level"] if aggregate_statement else risk_level_from_score(
            0)
    )

    return {
        "statement_count": len(statements),
        "overall_score": overall_score,
        "overall_risk_level": overall_risk_level,
        # A batch is only as well understood as its least understood statement.
        "overall_confidence": (weakest_confidence([statement["confidence"] for statement in statements])
                               if statements else None),
        "analysis_mode": derive_analysis_mode(evidence_states),
        "evidence_summary": summarize_evidence(connection, evidence_states),
        "statements": statements,
    }


# Verified unique-key scope: replaces the +5 "affected-row count cannot be established"
# factor and credits -5, and only when MySQL table metadata confirms the key.
SCOPE_VERIFIED_POINTS = -10

# The credit never takes a write below MEDIUM (score 20). A verified key bounds the
# directly matched rows of the target table; it does not establish the total data
# impact (triggers, foreign-key actions, whether the row exists or changes). So a
# verified one-row UPDATE/DELETE is MEDIUM at least: the credit can lower a score
# within MEDIUM or above, never to LOW.
SCOPE_VERIFIED_RISK_FLOOR = 20
SCOPE_FLOOR_REASON = (
    "MEDIUM is the minimum overall risk for an UPDATE or DELETE whose direct match is "
    "bounded by a verified unique key: the bound covers the target row only, not the "
    "total data impact."
)


INDIRECT_EFFECTS_NOT_CHECKED = (
    "Triggers, foreign-key actions (ON DELETE / ON UPDATE CASCADE, SET NULL) and other "
    "indirect effects on other rows or tables were not checked."
)


def scope_reason(scope: dict) -> str:
    return (f"At most one row of {scope['table']} can match directly: the WHERE clause fixes every "
            f"column of unique key {scope['index']} (from table metadata). The row may not exist, "
            "and an UPDATE can change fewer rows than it matches. " + INDIRECT_EFFECTS_NOT_CHECKED)


def apply_scope_adjustment(report, scope: dict):
    """The score credit for a verified unique-key scope, or (report, None, False).

    Returns (report, factor, floor_applied). The credit is SCOPE_VERIFIED_POINTS but
    never takes the score below SCOPE_VERIFIED_RISK_FLOOR (MEDIUM); the factor carries
    the points actually applied, which can be 0. Scope and confidence are unaffected:
    the verified key is still reported as the target's direct row scope.
    """
    if scope["basis"] != AT_MOST_ONE_ROW or report.risk_level == "CRITICAL":
        return report, None, False
    credited = max(0, min(100, report.score + SCOPE_VERIFIED_POINTS))
    score = max(credited, min(report.score, SCOPE_VERIFIED_RISK_FLOOR))
    floor_applied = score > credited
    factor = ScoreFactor(f"Unique key {scope['index']} limits the match to at most one row",
                         score - report.score)
    reasons = [scope_reason(scope) if reason == OFFLINE_ROW_IMPACT_REASON else reason
               for reason in report.findings]
    if floor_applied:
        reasons.append(SCOPE_FLOOR_REASON)
    return (replace(report, score=score, risk_level=risk_level_from_score(score), findings=reasons),
            factor, floor_applied)


def plan_estimate_reason(evidence: DatabaseEvidence, high_rows: bool) -> str:
    """The Execution Risk reason when a collected plan's estimates raised the score."""
    estimates = (["a full table scan"] if evidence.full_table_scan is True else []) + (
        [f"about {evidence.estimated_rows:,} rows examined"] if high_rows else [])
    return ("Execution plan collected: the optimizer estimates " + " and ".join(estimates)
            + ". These are estimates, not a measured runtime.")


def _cap(level: str, ceiling: str) -> str:
    return level if CONFIDENCE_ORDER[level] <= CONFIDENCE_ORDER[ceiling] else ceiling


def final_confidence(static_confidence: str, scope: dict, state: dict) -> str:
    """Statement confidence: the static level, raised only by a verified row scope, capped.

    Missing or failed evidence never raises it. A unique key confirmed in table
    metadata establishes the row scope of a write (HIGH unless the SQL was unreadable).
    A referenced table that metadata did not find, or whose existence could not be
    determined, caps it at LIMITED: the assessment may be about the wrong schema.
    """
    level = static_confidence
    if scope["basis"] == AT_MOST_ONE_ROW and level != "LOW":
        level = "HIGH"
    tables = state["metadata"].get("tables") or []
    if any(table.get("found") is not True for table in tables):
        level = _cap(level, "LIMITED")
    return level


def build_statement_result(static_report, analysis, evidence: DatabaseEvidence,
                           metadata: MetadataEvidence | None, connection: EvidenceConnection) -> dict:
    """Score one statement with collected evidence and build its response payload.

    Shared by local /api/validate and the connector's remote validate so equivalent
    evidence yields identical scoring and evidence semantics: static report -> EXPLAIN
    evidence adjustments -> M1 metadata -> M2 -> final report. Collection is the
    caller's job; metadata is None when it was not requested.
    """
    if (static_report.facts.statement_type == "SELECT" and not evidence.available
            and (evidence.error or "").startswith("unsupported_statement_type")):
        # A SELECT whose regenerated text is not a SELECT (SELECT ... INTO becomes
        # CREATE TABLE ... AS SELECT) gets no plan request: it is not eligible, which
        # is different from a statement type that never has a plan.
        evidence = replace(evidence, error=PLAN_NOT_ELIGIBLE)
    state = build_evidence_state(connection, evidence, metadata, list(static_report.facts.tables))
    if metadata is None:
        metadata = MetadataEvidence(status="unavailable")
    scope = build_row_scope(static_report.facts, metadata)
    scoped_report, scope_factor, floor_applied = apply_scope_adjustment(static_report, scope)
    # points: the change actually applied (0 when the MEDIUM floor absorbs the credit).
    scope["factor"] = ({"label": scope_factor.label, "points": scope_factor.points,
                        "medium_floor_applied": floor_applied}
                       if scope_factor else None)
    # Plan fields describe EXPLAIN's first row: only a one-row plan is interpreted.
    interpreted = evidence if plan_is_interpretable(evidence) else replace(evidence, full_table_scan=None)
    evidence_result = apply_evidence_adjustments(scoped_report, evidence)
    phase2_report = evidence_result.report

    m1_result = apply_m1_adjustment(
        phase2_report,
        metadata,
        full_table_scan=interpreted.full_table_scan,
        high_estimated_rows_applied=evidence_result.high_estimated_rows_applied,
        risk_level_for_score=risk_level_from_score,
    )
    m2_result = apply_m2_adjustment(
        m1_result.report,
        metadata,
        interpreted,
        risk_level_for_score=risk_level_from_score,
        phase2_risk_level=phase2_report.risk_level,
    )
    final_report = m2_result.report
    confidence = final_confidence(static_report.confidence, scope, state)
    metadata_factors = [
        *m1_result.metadata_factors,
        *m2_result.factors,
    ]

    payload = {
        "index": final_report.facts.index,
        # As submitted; raw_sql of an analysed statement is the parser's regeneration.
        "sql": final_report.facts.submitted_sql or final_report.facts.raw_sql,
        "statement_type": final_report.facts.statement_type,
        "category": final_report.facts.category,
        "tables": final_report.facts.tables,
        "has_where": final_report.facts.has_where,
        "where_looks_trivial": final_report.facts.where_looks_trivial,
        "where_predicate": final_report.facts.where_predicate_summary,
        "columns_in_where": final_report.facts.columns_in_where,
        "has_limit": final_report.facts.has_limit,
        "is_full_table_ddl": final_report.facts.is_full_table_ddl,
        "parse_error": final_report.facts.parse_error,
        # "analysed", or why not: "invalid", "unsupported" or "unreadable" (backend.parser).
        "analysis_status": final_report.facts.analysis_status,
        "static_score": static_report.score,
        "static_risk_level": static_report.risk_level,
        "score": final_report.score,
        "risk_level": final_report.risk_level,
        "confidence": confidence,
        "static_confidence": static_report.confidence,
        # What is established about the rows an UPDATE/DELETE matches (backend.row_scope).
        "scope": scope,
        # The SQL could only be read partly: the analysed text may differ from the submitted text.
        "analysis_text_differs": bool(final_report.facts.lossy_parse),
        "factors": [
            {"label": factor.label, "points": factor.points}
            for factor in static_report.factors
        ],
        "evidence_factors": [
            {"label": factor.label, "points": factor.points}
            for factor in evidence_result.factors
        ],
        "metadata_score_adjustment": m1_result.metadata_score_adjustment + m2_result.adjustment,
        "metadata_factors": [asdict(factor) for factor in metadata_factors],
        "metadata_status": metadata.status,
        "high_estimated_rows_applied": evidence_result.high_estimated_rows_applied,
        # Filter and index assessment from table metadata (display only; see m2_scoring).
        "filter_index": describe_filter_index(final_report.facts, metadata),
        "reasons": restate_reasons(final_report.reasons, state),
        "checklist": final_report.checklist,
    }

    # Advisory only, from the SQL alone, after the result above is final: it never
    # changes the score, confidence, completeness or evidence (backend.optimization).
    try:
        payload["optimization"] = advise(final_report.facts)
    except Exception:
        payload["optimization"] = {"status": "unavailable", "recommendations": []}

    if analysis.assessment is not None:
        try:
            assessment = restate_assessment(analysis.assessment, state)
            if evidence_result.factors:
                # The plan's estimates raised the score (full table scan, high row
                # estimate): the execution dimension says so, as optimizer estimates.
                reason = plan_estimate_reason(evidence, evidence_result.high_estimated_rows_applied)
                assessment = replace(assessment, dimensions=[
                    replace(dimension, level="MEDIUM", reason=reason)
                    if dimension.name == "Execution Risk" and dimension.level == "UNKNOWN" else dimension
                    for dimension in assessment.dimensions])
            # The analyzer's score fields are placeholders; never return them as a result.
            assessment = replace(assessment, overall_score=final_report.score,
                                 risk_level=final_report.risk_level, confidence=confidence)
            if scope["basis"] == AT_MOST_ONE_ROW:
                # The direct match is bounded; the total data impact is not: Data Risk
                # keeps its level, and the unknown-count finding says what is still open.
                direct = (f"Direct match: at most one row of {scope['table']} "
                          f"(unique key {scope['index']}, from table metadata). ")
                assessment = replace(
                    assessment,
                    dimensions=[
                        replace(dimension, reason=direct + INDIRECT_EFFECTS_NOT_CHECKED)
                        if dimension.name == "Data Risk" else dimension
                        for dimension in assessment.dimensions],
                    findings=[
                        replace(finding, title="Direct match bounded; total impact not established",
                                description=direct + "Whether the row exists and how many rows change "
                                "are not known. " + INDIRECT_EFFECTS_NOT_CHECKED)
                        if finding.title == "Affected-row count is unknown" else finding
                        for finding in assessment.findings])
            payload["v2_assessment"] = asdict(assessment)
        except Exception:
            payload["v2_assessment"] = None

    payload["database_evidence"] = {
        "available": evidence.available,
        "database": evidence.database,
        "tables": evidence.tables,
        "explain_available": evidence.explain_available,
        "estimated_rows": evidence.estimated_rows,
        "access_type": evidence.access_type,
        "possible_keys": evidence.possible_keys,
        "key": evidence.key,
        "key_length": evidence.key_length,
        "extra": evidence.extra,
        "full_table_scan": evidence.full_table_scan,
        "estimated_rows_semantics": "optimizer estimate, not exact affected-row count",
        "metadata_status": metadata.status,
        "error": evidence.error,
        # Stable, browser-safe reason (see backend.db.EVIDENCE_STATUSES).
        "status": evidence_status(evidence),
    }
    # The authoritative evidence state for this statement (backend.evidence_state).
    payload["evidence"] = state

    return payload
