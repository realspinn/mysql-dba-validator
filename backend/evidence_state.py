"""The database evidence state of a validation result: what evidence it actually has.

Every statement result carries one evidence object, built here from what the
collectors returned. It is the source of truth for the result's evidence claims:
the batch evidence_summary, the compatibility analysis_mode, and the evidence
sentences in reasons and v2_assessment are all derived from it, never set
independently. Local and remote (connector) results use the same states with the
same meaning; only how evidence is collected differs.

    connection  not_attempted | connected | failed
    plan        collected | not_collected_read_only | not_eligible | not_supported
                | failed | not_attempted
    metadata    collected | failed | not_applicable | not_attempted
    overall     static_only | collected | partial | unavailable | not_applicable

"collected" means the database returned that evidence; it never means it is complete.
Execution-plan rows and table rows are optimizer/engine estimates, and no evidence the
validator collects gives the number of rows a statement would affect. Evidence never
changes confidence (HIGH, LIMITED, LOW); scoring adjustments live in evidence_scoring,
metadata_scoring and m2_scoring and do not read this state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from .analyzer import OFFLINE_EXECUTION_REASON
from .db_evidence import (
    PLAN_NOT_COLLECTED_READ_ONLY,
    PLAN_NOT_ELIGIBLE,
    DatabaseEvidence,
    evidence_status,
)
from .evidence import NO_CONNECTION_REASON
from .metadata import MetadataEvidence
from .recommendations import recommendations_from_gaps
from .risk_engine import OFFLINE_EVIDENCE_REASON, OFFLINE_ROW_IMPACT_REASON

NO_DATABASE_SELECTED = "no_database_selected"
UNKNOWN_GAP_REASON = "Evidence for this assessment dimension was not established."


@dataclass(frozen=True)
class EvidenceConnection:
    """Whether the request's evidence connection exists. reason is a stable code."""

    state: str
    reason: Optional[str] = None


NOT_ATTEMPTED = EvidenceConnection("not_attempted", "not_configured")
CONNECTED = EvidenceConnection("connected")


def connection_failed(reason: str) -> EvidenceConnection:
    return EvidenceConnection("failed", reason)


def _plan(connection: EvidenceConnection, evidence: DatabaseEvidence) -> dict[str, Any]:
    if connection.state != "connected":
        return {"state": "not_attempted", "reason": None}
    if evidence.available and evidence.explain_available:
        return {"state": "collected", "reason": None}
    status = evidence_status(evidence)
    if status == PLAN_NOT_COLLECTED_READ_ONLY:
        return {"state": "not_collected_read_only", "reason": status}
    if status == PLAN_NOT_ELIGIBLE:
        return {"state": "not_eligible", "reason": status}
    if status == "unsupported_statement_type":
        return {"state": "not_supported", "reason": status}
    return {"state": "failed", "reason": status}


def _metadata(connection: EvidenceConnection, metadata: Optional[MetadataEvidence],
              tables: list[str]) -> dict[str, Any]:
    if connection.state != "connected":
        return {"state": "not_attempted", "reason": None, "tables": []}
    if not tables:
        return {"state": "not_applicable", "reason": None, "tables": []}
    if metadata is None:
        # Connected but metadata was not requested (a remote validation without a
        # selected database).
        return {"state": "not_attempted", "reason": NO_DATABASE_SELECTED, "tables": []}
    if metadata.status == "available":
        found = getattr(metadata, "tables", None) or []
        return {"state": "collected", "reason": None,
                "tables": [{"name": table["name"], "found": table["found"]} for table in found]}
    return {"state": "failed", "reason": metadata.status, "tables": []}


def _overall(connection: EvidenceConnection, plan: str, metadata: str) -> str:
    if connection.state == "not_attempted":
        return "static_only"
    if connection.state == "failed":
        return "unavailable"
    collected = "collected" in (plan, metadata)
    failed = "failed" in (plan, metadata)
    if collected and failed:
        return "partial"
    if collected:
        return "collected"
    if failed:
        return "unavailable"
    return "not_applicable"


def build_evidence_state(connection: EvidenceConnection, evidence: DatabaseEvidence,
                         metadata: Optional[MetadataEvidence], tables: list[str]) -> dict[str, Any]:
    """The per-statement evidence object (JSON-ready)."""
    plan = _plan(connection, evidence)
    meta = _metadata(connection, metadata, tables)
    return {
        "connection": {"state": connection.state, "reason": connection.reason},
        "plan": plan,
        "metadata": meta,
        "overall": _overall(connection, plan["state"], meta["state"]),
    }


# ---------------------------------------------------------------- batch view


def _counts(states: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for state in states:
        counts[state] = counts.get(state, 0) + 1
    return counts


def summarize(connection: EvidenceConnection, evidence_states: list[dict[str, Any]]) -> dict[str, Any]:
    """Batch evidence_summary: the request's connection and per-type counts, so mixed
    batches stay visible.

    partial is True when some database evidence was collected and some failed, in
    one statement or across statements. Deliberate non-collection (read-only, not
    eligible, not supported) is not a failure; the plan counts show it.
    """
    plans = [state["plan"]["state"] for state in evidence_states]
    metadata = [state["metadata"]["state"] for state in evidence_states]
    collected = "collected" in plans or "collected" in metadata
    failed = "failed" in plans or "failed" in metadata
    return {
        "connection": {"state": connection.state, "reason": connection.reason},
        "plan": _counts(plans),
        "metadata": _counts(metadata),
        "overall": _counts([state["overall"] for state in evidence_states]),
        "partial": collected and failed,
    }


def analysis_mode(evidence_states: list[dict[str, Any]]) -> str:
    """Compatibility label derived from the evidence states (never authoritative).

    DATABASE_EVIDENCE only says that at least one plan was collected; evidence_summary
    and each statement's evidence say what else was or was not collected.
    """
    if not any(state["connection"]["state"] != "not_attempted" for state in evidence_states):
        return "STATIC"
    if any(state["plan"]["state"] == "collected" for state in evidence_states):
        return "DATABASE_EVIDENCE"
    if any(state["metadata"]["state"] == "collected" for state in evidence_states):
        return "STATIC_DATABASE_METADATA"
    if any(state["overall"] == "unavailable" for state in evidence_states):
        return "STATIC_DATABASE_UNAVAILABLE"
    # Connected, but no database evidence applies to these statements.
    return "STATIC"


# ---------------------------------------------------------------- explanations

_REASON_TEXT = {
    "not_configured": "no database login and database were given",
    "unreachable": "the database could not be reached",
    "auth_failed": "the database rejected the login",
    "database_not_found": "the selected database was not found",
    "insufficient_privileges": "the account lacks a required privilege",
    "explain_failed": "MySQL could not produce an EXPLAIN plan",
    "collection_failed": "evidence collection failed",
    "timeout": "metadata collection timed out",
    "permission_denied": "metadata permission was denied",
    "budget_exceeded": "metadata collection exceeded its size limit",
    "invalid_reference": "a table reference was not supported",
    "unavailable": "metadata was unavailable",
    NO_DATABASE_SELECTED: "no database was selected",
}


def _reason_text(code: Optional[str]) -> str:
    return _REASON_TEXT.get(code or "", "evidence was unavailable")


def _plan_phrase(plan: dict[str, Any]) -> str:
    state = plan["state"]
    if state == "collected":
        return "an execution plan was collected (optimizer estimates)"
    if state == "not_collected_read_only":
        return "the execution plan was not collected because the local evidence connection is read-only"
    if state == "not_eligible":
        return "no execution plan was requested because this statement is not eligible for planning"
    if state == "not_supported":
        return "no execution plan is collected for this statement"
    return "the execution plan could not be collected: " + _reason_text(plan["reason"])


def _metadata_phrase(metadata: dict[str, Any]) -> str:
    state = metadata["state"]
    if state == "collected":
        missing = [table["name"] for table in metadata["tables"] if table["found"] is False]
        if missing:
            return "table metadata was collected; not found in the selected database: " + ", ".join(missing)
        return "table metadata was collected"
    if state == "failed":
        return "table metadata could not be collected: " + _reason_text(metadata["reason"])
    if state == "not_applicable":
        return "no table metadata applies"
    return "table metadata was not collected: " + _reason_text(metadata["reason"])


def evidence_sentence(state: dict[str, Any]) -> str:
    """One sentence on what database evidence this statement has (for reasons)."""
    connection = state["connection"]
    if connection["state"] == "not_attempted":
        return OFFLINE_EVIDENCE_REASON
    if connection["state"] == "failed":
        return ("Database evidence could not be collected (" + _reason_text(connection["reason"])
                + "); this is static analysis only. Actual row counts and execution-plan "
                "evidence are not available.")
    text = _metadata_phrase(state["metadata"]) + "; " + _plan_phrase(state["plan"])
    return ("Database evidence: " + text[0].lower() + text[1:]
            + ". Actual affected-row counts are not known.")


ROW_IMPACT_WITH_EVIDENCE = (
    "Row impact cannot be determined from the SQL or the collected evidence; "
    "actual affected-row counts are not available."
)


def restate_reasons(reasons: list[str], state: dict[str, Any]) -> list[str]:
    """Replace the static-view reasons with ones that match the evidence state."""
    if state["connection"]["state"] == "not_attempted":
        return list(reasons)
    restated = []
    for reason in reasons:
        if reason == OFFLINE_EVIDENCE_REASON:
            restated.append(evidence_sentence(state))
        elif reason == OFFLINE_ROW_IMPACT_REASON:
            restated.append(ROW_IMPACT_WITH_EVIDENCE)
        else:
            restated.append(reason)
    return restated


def _gap_reason(name: str, state: dict[str, Any]) -> Optional[str]:
    """Why a v2 evidence gap is still open, or None when the evidence closed it."""
    connection = state["connection"]
    if connection["state"] == "not_attempted":
        return NO_CONNECTION_REASON
    if connection["state"] == "failed":
        return "Database connection failed: " + _reason_text(connection["reason"])
    if name == "execution_plan":
        if state["plan"]["state"] == "collected":
            return None
        return "Not collected: " + _plan_phrase(state["plan"])
    if name == "affected_row_count":
        return "Not provided by database evidence: metadata and EXPLAIN give estimates, not affected-row counts"
    # A gap this module does not know is never closed by unrelated evidence.
    return UNKNOWN_GAP_REASON


def restate_assessment(assessment: Any, state: dict[str, Any]) -> Any:
    """The v2 assessment with evidence gaps and the execution dimension matching the state."""
    if assessment is None or state["connection"]["state"] == "not_attempted":
        return assessment
    gaps, available = [], []
    for gap in assessment.evidence_missing:
        reason = _gap_reason(gap.name, state)
        if reason is None:
            available.append(gap.name)
        else:
            gaps.append(replace(gap, reason=reason))
    if state["metadata"]["state"] == "collected":
        available.insert(0, "table_metadata")
    dimensions = []
    for dimension in assessment.dimensions:
        if dimension.reason == OFFLINE_EXECUTION_REASON:
            plan = state["plan"]
            reason = ("Execution plan collected (optimizer estimates, not runtime behaviour)."
                      if plan["state"] == "collected" else
                      "Database connection failed; execution plan not collected."
                      if state["connection"]["state"] == "failed" else
                      _plan_phrase(plan)[0].upper() + _plan_phrase(plan)[1:] + ".")
            dimension = replace(dimension, reason=reason)
        dimensions.append(dimension)
    return replace(
        assessment,
        evidence_missing=gaps,
        evidence_available=list(dict.fromkeys([*assessment.evidence_available, *available])),
        recommendations=recommendations_from_gaps(gaps),
        dimensions=dimensions,
    )
