"""
Deterministic risk scoring and DBA checklist generation.

This is the offline/static analysis layer of the MySQL DBA Validator.

It deliberately does NOT pretend to know:
- actual row counts
- table sizes
- indexes
- query execution plans
- locks
- stored procedure internals

Those will be introduced when database-backed analysis is added.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parser import StatementFacts
from . import models
from typing import Union


# ------------------------------------------------------------------
# BASE SCORES
# ------------------------------------------------------------------

BASE_SCORE = {
    "SELECT": 0,
    "INSERT": 5,
    "UPDATE": 15,
    "DELETE": 20,
    "REPLACE": 15,
    "CREATE": 10,
    "ALTER": 25,
    "DROP": 40,
    "TRUNCATE": 40,
    "GRANT": 20,
    "REVOKE": 20,
    "CALL": 10,
    "COMMIT": 0,
    "ROLLBACK": 0,
    "BEGIN": 0,
    "START": 0,
}


# ------------------------------------------------------------------
# RESULT MODELS
# ------------------------------------------------------------------

@dataclass
class ScoreFactor:
    label: str
    points: int


@dataclass
class StatementReport:
    facts: StatementFacts
    score: int
    risk_level: str
    confidence: str = "LIMITED"
    factors: list[ScoreFactor] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        """
        Backwards-compatible alias.

        Older API/UI code may refer to findings as 'reasons'.
        Internally, 'findings' is the canonical name.
        """
        return self.findings


# ------------------------------------------------------------------
# RISK LEVEL
# ------------------------------------------------------------------

def _risk_level(score: int) -> str:
    if score >= 80:
        return "CRITICAL"

    if score >= 50:
        return "HIGH"

    if score >= 20:
        return "MEDIUM"

    return "LOW"


# ------------------------------------------------------------------
# CONFIDENCE
# ------------------------------------------------------------------

def _confidence_for_offline_analysis(
    facts: StatementFacts,
) -> str:
    """
    Confidence means confidence in the STATIC classification,
    not confidence that the SQL is safe.

    DROP/TRUNCATE are deterministic from the SQL itself, so their
    classification can be HIGH confidence even without connecting
    to MySQL.

    DML gets LIMITED confidence because actual affected-row counts
    require database evidence.
    """

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


# ------------------------------------------------------------------
# SCORE ONE STATEMENT
# ------------------------------------------------------------------

def _ensure_facts(item: Union[StatementFacts, models.StatementAnalysis]) -> StatementFacts:
    """Return a StatementFacts instance for the given input.

    If a models.StatementAnalysis is provided and it carries an attached
    source_facts attribute (set by analyzer.analyze_statement), use that.
    Otherwise construct a minimal StatementFacts object from the available
    StatementAnalysis fields so the rest of the scoring code can run.
    """

    if isinstance(item, StatementFacts):
        return item

    # models.StatementAnalysis
    analysis = item

    # Prefer the original attached parser facts when available
    if analysis.assessment is not None and hasattr(analysis.assessment, "source_facts"):
        src = getattr(analysis.assessment, "source_facts")
        if isinstance(src, StatementFacts):
            return src

    # Fallback: create a minimal StatementFacts so scoring can continue.
    facts = StatementFacts(raw_sql=analysis.raw_sql, index=analysis.index)
    facts.statement_type = analysis.statement_type
    facts.tables = list(analysis.tables or [])
    # Other fields remain defaults (has_where=None, etc.)
    return facts


def score_statement(
    facts: Union[StatementFacts, models.StatementAnalysis],
    batch_size: int,
) -> StatementReport:

    # Normalize input to StatementFacts for existing scoring logic
    facts = _ensure_facts(facts)

    # --------------------------------------------------------------
    # PARSE ERROR
    # --------------------------------------------------------------

    if facts.parse_error:

        return StatementReport(
            facts=facts,
            score=100,
            risk_level="CRITICAL",
            confidence="LOW",
            factors=[
                ScoreFactor(
                    "Parse error — statement could not be safely analyzed",
                    100,
                )
            ],
            findings=[
                f"Parser error: {facts.parse_error}"
            ],
            checklist=[
                "Fix the SQL syntax before proceeding.",
                "Do not execute a statement that the validator could not analyze.",
            ],
        )

    factors: list[ScoreFactor] = []
    reasons: list[str] = []
    checklist: list[str] = []

    statement_type = facts.statement_type

    # --------------------------------------------------------------
    # BASE SCORE
    # --------------------------------------------------------------

    base = BASE_SCORE.get(statement_type, 15)

    factors.append(
        ScoreFactor(
            f"Base risk for {statement_type or 'UNKNOWN'}",
            base,
        )
    )

    score = base

    # --------------------------------------------------------------
    # HARD CRITICAL RULES
    # --------------------------------------------------------------

    if statement_type in {"DROP", "TRUNCATE"}:

        reasons.append(
            f"{statement_type} is a destructive database operation."
        )

        checklist.extend(
            [
                "Verify the exact object being removed.",
                "Confirm the change is explicitly approved.",
                "Verify a recent backup or point-in-time recovery capability exists.",
                "Confirm the maintenance/change window is appropriate.",
                "Do not execute until the DBA has independently reviewed the operation.",
            ]
        )

        return StatementReport(
            facts=facts,
            score=100,
            risk_level="CRITICAL",
            confidence="HIGH",
            factors=factors + [
                ScoreFactor(
                    f"{statement_type} is classified as CRITICAL",
                    60,
                )
            ],
            findings=reasons,
            checklist=_deduplicate(checklist),
        )

    # --------------------------------------------------------------
    # WRITE OPERATIONS
    # --------------------------------------------------------------

    is_write = statement_type in {
        "UPDATE",
        "DELETE",
        "REPLACE",
    }

    if is_write:

        # ----------------------------------------------------------
        # NO WHERE
        # ----------------------------------------------------------

        if facts.has_where is False:

            factors.append(
                ScoreFactor(
                    "No WHERE clause — potentially affects every row",
                    65,
                )
            )

            reasons.append(
                f"{statement_type} has no WHERE clause; "
                "the operation may affect every row."
            )

            checklist.extend(
                [
                    "Confirm that affecting ALL rows is explicitly intended.",
                    "Estimate the affected-row count before execution.",
                    "Verify backup or point-in-time recovery capability.",
                    "Consider executing in controlled batches if appropriate.",
                ]
            )

            # No-WHERE DML is always critical.
            score = 100

        # ----------------------------------------------------------
        # TRIVIAL WHERE
        # ----------------------------------------------------------

        elif facts.where_looks_trivial:

            factors.append(
                ScoreFactor(
                    "WHERE clause appears unusually broad",
                    25,
                )
            )

            reasons.append(
                f"WHERE clause ({facts.where_predicate_summary}) "
                "may match most or all rows."
            )

            checklist.extend(
                [
                    "Verify that the WHERE predicate actually narrows the target rows.",
                    "Run a SELECT COUNT(*) using the same predicate.",
                    "Review the execution plan before execution.",
                ]
            )

            score += 25

        # ----------------------------------------------------------
        # NORMAL WHERE
        # ----------------------------------------------------------

        else:

            checklist.extend(
                [
                    "Verify the expected affected-row count before execution.",
                    "Consider running the WHERE predicate as SELECT COUNT(*) first.",
                    "Review the execution plan when database-backed analysis is available.",
                ]
            )

            # ------------------------------------------------------
            # KEY-SCOPED WHERE
            # ------------------------------------------------------
            #
            # The original V1.1 behavior/test contract expects:
            #
            # UPDATE ... WHERE id = 10
            #
            # to receive a 5-point reduction from the UPDATE base
            # score of 15.
            #
            # Therefore:
            #
            # 15 - 5 = 10
            # ------------------------------------------------------

            key_scoped = (
                _looks_key_scoped(facts)
            )

            if key_scoped:

                factors.append(
                    ScoreFactor(
                        "WHERE predicate appears key-scoped",
                        -5,
                    )
                )

                reasons.append(
                    "The WHERE predicate appears to target a specific key value."
                )

                score -= 5

            else:

                # A normal bounded predicate is still MEDIUM-risk
                # in offline mode because we cannot know how many rows
                # it actually matches.

                factors.append(
                    ScoreFactor(
                        "Affected-row count cannot be established offline",
                        5,
                    )
                )

                reasons.append(
                    "Row impact cannot be determined in offline analysis; "
                    "actual database row counts are not available."
                )

                score += 5

        checklist.append(
            "Confirm the expected business impact before execution."
        )

        checklist.append(
            "Confirm backup / point-in-time recovery is available for affected data."
        )

        # IMPORTANT:
        #
        # We deliberately do not penalize the absence of LIMIT.
        #
        # LIMIT is not universally required for UPDATE/DELETE.
        #
        # The validator should instead determine whether the predicate
        # is narrow and eventually use database evidence to estimate
        # affected rows.

    # --------------------------------------------------------------
    # INSERT
    # --------------------------------------------------------------

    if statement_type == "INSERT":

        checklist.extend(
            [
                "Confirm the target table and column mapping.",
                "Verify duplicate-key and constraint behavior.",
                "Confirm the expected number of rows being inserted.",
            ]
        )

    # --------------------------------------------------------------
    # ALTER
    # --------------------------------------------------------------

    if statement_type == "ALTER":

        checklist.extend(
            [
                "Confirm this schema change has been reviewed against the migration/change process.",
                "Review potential locking and application-impact concerns.",
                "Review the affected table size before execution.",
            ]
        )

        if facts.is_full_table_ddl:

            factors.append(
                ScoreFactor(
                    "ALTER drops a column — potentially irreversible schema change",
                    15,
                )
            )

            reasons.append(
                "Dropping a column can permanently remove data."
            )

            score += 15

            checklist.append(
                "Confirm no application code or downstream job still depends on the column."
            )

    # --------------------------------------------------------------
    # GRANT / REVOKE
    # --------------------------------------------------------------

    if statement_type in {"GRANT", "REVOKE"}:

        factors.append(
            ScoreFactor(
                f"{statement_type} changes access privileges",
                5,
            )
        )

        score += 5

        checklist.append(
            "Confirm this privilege change matches an approved access-control request."
        )

    # --------------------------------------------------------------
    # STORED PROCEDURE
    # --------------------------------------------------------------

    if statement_type == "CALL":

        reasons.append(
            "Stored procedure behavior cannot be fully determined from the CALL statement alone."
        )

        checklist.extend(
            [
                "Inspect the stored procedure definition before execution.",
                "Identify which tables and objects the procedure may modify.",
                "Review the procedure's transaction and error-handling behavior.",
            ]
        )

    # --------------------------------------------------------------
    # MULTI-STATEMENT BATCH
    # --------------------------------------------------------------

    if batch_size > 1:

        factors.append(
            ScoreFactor(
                "Part of a multi-statement batch",
                10,
            )
        )

        score += 10

        reasons.append(
            f"This statement was pasted alongside "
            f"{batch_size - 1} other statement(s)."
        )

        checklist.append(
            "Review each statement independently before execution."
        )

    # --------------------------------------------------------------
    # TRANSACTION GUIDANCE
    # --------------------------------------------------------------

    if statement_type in {
        "INSERT",
        "UPDATE",
        "DELETE",
        "REPLACE",
    }:

        checklist.append(
            "Where appropriate, test the DML inside a transaction before committing."
        )

    # --------------------------------------------------------------
    # OFFLINE LIMITATION
    # --------------------------------------------------------------

    confidence = _confidence_for_offline_analysis(facts)

    if confidence == "LIMITED":

        reasons.append(
            "This is offline/static analysis; actual database metadata, "
            "row counts and execution-plan evidence are not available."
        )

    # --------------------------------------------------------------
    # FINAL SCORE
    # --------------------------------------------------------------

    score = max(
        0,
        min(100, score),
    )

    # A write operation with a normal WHERE should not be LOW.
    #
    # This is intentional:
    #
    # UPDATE orders
    # SET ...
    # WHERE created_at <= ...
    #
    # may affect 10 rows or 10 million rows.
    #
    # Offline mode cannot know which.
    #
    # Therefore the minimum classification for a normal non-key DML
    # operation is MEDIUM.

    if (
        is_write
        and facts.has_where is True
        and not facts.where_looks_trivial
        and score < 20
        and not _looks_key_scoped(facts)
    ):
        score = 20

    risk_level = _risk_level(score)

    return StatementReport(
        facts=facts,
        score=score,
        risk_level=risk_level,
        confidence=confidence,
        factors=factors,
        findings=reasons,
        checklist=_deduplicate(checklist),
    )


# ------------------------------------------------------------------
# KEY-SCOPED DETECTION
# ------------------------------------------------------------------

def _looks_key_scoped(
    facts: StatementFacts,
) -> bool:
    """
    Conservative heuristic for a predicate that appears to target
    one specific row through an equality condition.

    Examples that may qualify:

        WHERE id = 10
        WHERE user_id = 123

    Examples that do not:

        WHERE created_at <= '2026-08-19'
        WHERE status = 'pending'
        WHERE id IN (1,2,3)
    """

    if not facts.columns_in_where:
        return False

    predicate = (
        facts.where_predicate_summary or ""
    ).lower()

    # Must use equality.
    if "=" not in predicate:
        return False

    # Avoid treating IN / BETWEEN / range predicates as key-scoped.
    if " in " in f" {predicate} ":
        return False

    if "between" in predicate:
        return False

    # Common primary-key / foreign-key naming patterns.
    for column in facts.columns_in_where:

        normalized = column.lower().strip()

        if normalized == "id":
            return True

        if normalized.endswith("_id"):
            return True

    return False


# ------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------

def _deduplicate(
    items: list[str],
) -> list[str]:

    seen: set[str] = set()
    result: list[str] = []

    for item in items:

        if item not in seen:
            seen.add(item)
            result.append(item)

    return result


# ------------------------------------------------------------------
# BATCH SCORING
# ------------------------------------------------------------------

def score_batch(
    all_facts: list[StatementFacts],
) -> list[StatementReport]:

    return [
        score_statement(
            facts,
            batch_size=len(all_facts),
        )
        for facts in all_facts
    ]
