"""Bounded, deterministic risk adjustments from Phase 1 database evidence."""

from __future__ import annotations

from dataclasses import dataclass, replace

from .db_evidence import DatabaseEvidence
from .risk_engine import ScoreFactor, StatementReport, _risk_level


SUPPORTED_STATEMENTS = {"SELECT", "UPDATE", "DELETE"}
SELECTIVE_ACCESS_TYPES = {"const", "eq_ref", "ref"}
HIGH_ESTIMATED_ROWS = 10_000
FULL_SCAN_POINTS = 10
HIGH_ROWS_POINTS = 10
INDEXED_ACCESS_POINTS = -3


@dataclass
class EvidenceAdjustmentResult:
    """The static report and its separately attributable evidence changes."""

    report: StatementReport
    static_score: int
    factors: list[ScoreFactor]
    materially_supports_analysis: bool = False
    high_estimated_rows_applied: bool = False


def apply_evidence_adjustments(
    report: StatementReport,
    evidence: DatabaseEvidence,
) -> EvidenceAdjustmentResult:
    """Apply small evidence adjustments without changing V1 static scoring.

    Unavailable, incomplete, unsupported, or ambiguous evidence is a no-op.
    Hard-critical and unrestricted write reports cannot be reduced by evidence.
    """
    static_score = report.score
    is_write = report.facts.statement_type in {"UPDATE", "DELETE"}
    if (
        not evidence.available
        or not evidence.explain_available
        or report.facts.statement_type not in SUPPORTED_STATEMENTS
        or report.risk_level == "CRITICAL"
    ):
        return EvidenceAdjustmentResult(report, static_score, [], False, False)

    materially_supports = (
        isinstance(evidence.estimated_rows, int)
        or isinstance(evidence.full_table_scan, bool)
        or (
            bool(evidence.access_type)
            and (
                evidence.access_type == "ALL"
                or bool(evidence.key)
            )
        )
    )

    factors: list[ScoreFactor] = []
    findings = list(report.findings)
    checklist = list(report.checklist)
    score_adjustment = 0

    full_scan = evidence.full_table_scan is True
    if is_write and full_scan:
        factors.append(ScoreFactor("EXPLAIN indicates a full table scan", FULL_SCAN_POINTS))
        findings.append(
            "EXPLAIN indicates a full table scan; this may examine a broad portion of the table."
        )
        checklist.append("Review the EXPLAIN plan and confirm the access path is intended.")
        score_adjustment += FULL_SCAN_POINTS

    has_high_estimate = (
        is_write
        and
        isinstance(evidence.estimated_rows, int)
        and evidence.estimated_rows >= HIGH_ESTIMATED_ROWS
    )
    if has_high_estimate:
        factors.append(ScoreFactor("EXPLAIN estimated rows are high", HIGH_ROWS_POINTS))
        findings.append(
            "The optimizer estimates that EXPLAIN will examine a broad number of rows; "
            "this is not an exact affected-row count."
        )
        checklist.append("Review the EXPLAIN estimate and verify the expected business impact.")
        score_adjustment += HIGH_ROWS_POINTS

    indexed_access = (
        evidence.access_type in SELECTIVE_ACCESS_TYPES
        and bool(evidence.key)
        and not full_scan
    )
    if indexed_access and is_write:
        factors.append(ScoreFactor("EXPLAIN confirms selective indexed access", INDEXED_ACCESS_POINTS))
        findings.append(
            "EXPLAIN confirms selective indexed access; verify that the selected key is appropriate."
        )
        score_adjustment += INDEXED_ACCESS_POINTS

    # Confidence (HIGH/LIMITED/LOW) is never changed here: an EXPLAIN plan gives
    # optimizer estimates, not affected-row counts. What evidence a result has is
    # reported separately (backend.evidence_state).
    if not factors:
        return EvidenceAdjustmentResult(
            report,
            static_score,
            [],
            materially_supports,
            has_high_estimate,
        )

    final_score = max(0, min(100, static_score + score_adjustment))
    final_report = replace(
        report,
        score=final_score,
        risk_level=_risk_level(final_score),
        factors=report.factors,
        findings=findings,
        checklist=list(dict.fromkeys(checklist)),
    )
    return EvidenceAdjustmentResult(
        final_report,
        static_score,
        factors,
        materially_supports,
        has_high_estimate,
    )
