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

from .parser import NOT_ANALYSED_WORDING, UNREADABLE, StatementFacts, parse_sql
from . import models
from .db_evidence import PERMITTED_CALL_NAMES, call_names_permitted
from .row_scope import ALL_ROWS, static_row_scope
from typing import Optional, Union


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


# Reasons that describe the static view only. When database evidence was attempted,
# backend.evidence_state replaces them with what the evidence actually showed.
OFFLINE_EVIDENCE_REASON = (
    "This is offline/static analysis; actual database metadata, "
    "row counts and execution-plan evidence are not available."
)
OFFLINE_ROW_IMPACT_REASON = (
    "Row impact cannot be determined in offline analysis; "
    "actual database row counts are not available."
)
NULL_TEST_REASON = (
    "The WHERE clause tests IS NULL or IS NOT NULL; such predicates can match many rows."
)
LOSSY_PARSE_REASON = (
    "The SQL could only be read partly; the analysed statement may differ from the submitted text."
)

# Manual preflight guidance. It is advice for the DBA, never a query the validator
# builds or runs, and it never claims that a count or a transaction makes a write safe.
CANDIDATE_ROWS_CHECK = (
    "Review the candidate rows with a read-only query written for this statement's actual "
    "target table and predicate; a count is a point-in-time check, not a guarantee of what the write will change."
)
CONCURRENCY_CHECK = (
    "Consider transaction boundaries, isolation level and concurrent changes between your check and the write."
)

STATEMENTS_WITH_ROW_IMPACT = {"UPDATE", "DELETE", "INSERT", "REPLACE", "CALL"}

# A SELECT that can lock rows, or that calls a function whose effects were not
# established, is not presented as a read-only query: MEDIUM at least, LIMITED confidence.
SELECT_SIDE_EFFECT_FLOOR = 20
LOCKING_READ_FINDING = "Locking read (FOR UPDATE / FOR SHARE)"
LOCKING_READ_REASON = (
    "This SELECT has a locking clause. It does not change data, but when autocommit is "
    "disabled (for example inside START TRANSACTION) MySQL keeps locks on the rows it reads "
    "until the transaction commits or rolls back, so other transactions that update or lock "
    "those rows may have to wait. The validator cannot see the transaction it runs in."
)
UNREVIEWED_CALL_FINDING = "Calls a function whose effects were not established"
UNREVIEWED_CALL_REASON = (
    "This SELECT calls {calls}, which is not on the validator's reviewed list of MySQL "
    "built-in functions, so its effects were not established. A stored or loadable function "
    "can change data, and some built-ins wait, take locks, read server files or change "
    "session state."
)
UNIDENTIFIED_CALLS = "a function the validator could not identify (a quoted or schema-qualified name)"
EXPLAIN_CALL_NOTE = (
    " EXPLAIN does not run the SELECT, but MySQL can evaluate functions while planning it."
)

# Query statement types as parser._classify names them: a SELECT, a set operation of
# query blocks (UNION, INTERSECT, EXCEPT) and a parenthesised query, (SELECT ...).
QUERY_STATEMENT_TYPES = frozenset({"SELECT", "UNION", "INTERSECT", "EXCEPT", "SUBQUERY"})


def select_side_effects(facts: StatementFacts) -> tuple[bool, Optional[str]]:
    """(locking read, unreviewed calls) for a query, or an EXPLAIN of one.

    A query is a SELECT, a set operation or a parenthesised query; a locking clause or
    call in any of its query blocks counts. A plain EXPLAIN does not run the query, so it
    has no locking read, but MySQL can evaluate functions while planning (evidence
    contract section 11), so its calls count. EXPLAIN ANALYZE runs the query: both count.

    The calls are the function calls (parser.function_call_names: words written before "("
    that the AST does not read as other syntax) that are not on the reviewed function list
    (backend.db_evidence, the same policy that decides plan requests), as text for the
    finding; None when every call is reviewed.
    """
    explains_query = (facts.statement_type == "DESCRIBE"
                      and facts.explained_statement_type in QUERY_STATEMENT_TYPES)
    if facts.parse_error or not (facts.statement_type in QUERY_STATEMENT_TYPES or explains_query):
        return False, None
    locking = facts.locking_read and (not explains_query or facts.explain_analyze)
    names = facts.function_call_names
    if call_names_permitted(names):
        return locking, None
    if names is None:
        return locking, UNIDENTIFIED_CALLS
    return locking, ", ".join(f"{name}()" for name in sorted(names - PERMITTED_CALL_NAMES))


def unreviewed_call_reason(facts: StatementFacts, calls: str) -> str:
    """The unreviewed-call finding text; a plain EXPLAIN also says why planning matters."""
    reason = UNREVIEWED_CALL_REASON.format(calls=calls)
    if facts.statement_type == "DESCRIBE" and not facts.explain_analyze:
        reason += EXPLAIN_CALL_NOTE
    return reason

# A WHERE clause that is true for every row by its structure (WHERE 1, WHERE TRUE,
# WHERE id = 5 OR 1): the write matches every row, exactly as with no WHERE.
ALWAYS_TRUE_WHERE_REASON = (
    "The WHERE clause ({predicate}) is always true; the operation matches every row, "
    "exactly as if it had no WHERE clause."
)

# EXPLAIN / DESCRIBE. The validator never sends these statements to MySQL; the reasons
# describe what MySQL would do if someone ran the submitted text.
EXPLAIN_ANALYZE_REASON = (
    "EXPLAIN ANALYZE runs the statement it explains in order to time it, so this is scored "
    "as that {statement_type}. Where MySQL accepts EXPLAIN ANALYZE for it, running this text "
    "executes the {statement_type}."
)
EXPLAIN_ANALYZE_UNREADABLE_REASON = (
    "EXPLAIN ANALYZE runs the statement it explains, and that statement could not be read; "
    "its effect is unknown."
)
EXPLAIN_OF_CHANGE_REASON = (
    "EXPLAIN without ANALYZE plans the {statement_type} without running it, but the text "
    "contains a {statement_type}: if it is run without the EXPLAIN prefix it changes data. "
    "Review it as that statement."
)


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

CONFIDENCE_ORDER = {"LOW": 0, "LIMITED": 1, "HIGH": 2}


def weakest_confidence(levels: list[str]) -> str:
    """The lowest of the given confidence levels (the batch rule)."""
    return min(levels, key=lambda level: CONFIDENCE_ORDER.get(level, 0)) if levels else "LOW"


def static_confidence(
    facts: StatementFacts,
) -> str:
    """
    Confidence in the assessment from the SQL alone; never confidence that it is safe.

    HIGH: the facts that decide this statement's risk are established by the SQL:
    DDL, DCL, transaction control, SELECT, and an UPDATE/DELETE whose row scope is
    all rows of one table. LIMITED: a fact that could change the assessment is not
    established, usually the row scope of a write. LOW: the SQL could not be read, or
    only partly (the analysed text may not be the submitted text).

    This is the only confidence rule; backend.validation_results caps it with evidence
    (backend.validation_results.final_confidence) and never raises it except through a verified
    row scope.
    """

    if facts.parse_error or facts.lossy_parse:
        return "LOW"

    if facts.statement_type in {"UPDATE", "DELETE"}:
        return "HIGH" if static_row_scope(facts) == ALL_ROWS else "LIMITED"

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

    locking, calls = select_side_effects(facts)
    if locking or calls:
        # Whether it locks or what the called function does is not established by the SQL.
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
        # Not analysed (invalid, unsupported or unreadable): never reassuring, so the
        # score stays at its maximum with LOW confidence; the wording says which case.
        title, first_check = NOT_ANALYSED_WORDING.get(
            facts.analysis_status, NOT_ANALYSED_WORDING[UNREADABLE])

        return StatementReport(
            facts=facts,
            score=100,
            risk_level="CRITICAL",
            confidence="LOW",
            factors=[
                ScoreFactor(
                    "Statement not analysed — risk not assessed",
                    100,
                )
            ],
            findings=[
                f"{title}: {facts.parse_error}"
            ],
            checklist=[
                first_check,
                "Do not execute a statement that the validator could not analyze.",
            ],
        )

    factors: list[ScoreFactor] = []
    reasons: list[str] = []
    checklist: list[str] = []

    statement_type = facts.statement_type

    # --------------------------------------------------------------
    # EXPLAIN ANALYZE: MySQL runs the explained statement
    # --------------------------------------------------------------

    if statement_type == "DESCRIBE" and facts.explain_analyze:

        return _score_explain_analyze(facts, batch_size)

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
            confidence=static_confidence(facts),
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

        if facts.has_where is False or facts.where_always_true:

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

            else:

                factors.append(
                    ScoreFactor(
                        "WHERE clause is always true — matches every row",
                        65,
                    )
                )

                reasons.append(ALWAYS_TRUE_WHERE_REASON.format(
                    predicate=facts.where_predicate_summary))

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
                    CANDIDATE_ROWS_CHECK,
                    "Review the execution plan before execution.",
                ]
            )

            score += 25

        # ----------------------------------------------------------
        # ANY OTHER WHERE
        # ----------------------------------------------------------
        #
        # A WHERE clause is a structural fact, not evidence of a narrow
        # write: it can match zero, one, some or all rows. Column names
        # (id, *_id) and an "=" in the predicate prove nothing about
        # uniqueness. The only narrow-scope credit is a unique key confirmed
        # by table metadata (backend.row_scope, applied in backend.validation_results).

        else:

            checklist.extend(
                [
                    "Verify the expected affected-row count before execution.",
                    CANDIDATE_ROWS_CHECK,
                    "Review the execution plan when database-backed analysis is available.",
                ]
            )

            factors.append(
                ScoreFactor(
                    "Affected-row count cannot be established from the SQL alone",
                    5,
                )
            )

            reasons.append(OFFLINE_ROW_IMPACT_REASON)

            score += 5

        if facts.has_where is True:

            checklist.append(CONCURRENCY_CHECK)

            if facts.where_has_null_test:

                reasons.append(NULL_TEST_REASON)

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
    # EXPLAIN / DESCRIBE OF A STATEMENT THAT CHANGES SOMETHING
    # --------------------------------------------------------------

    if (
        statement_type == "DESCRIBE"
        and facts.explained_statement_type not in (None, "SELECT")
    ):

        factors.append(
            ScoreFactor(
                f"Explains a {facts.explained_statement_type} statement",
                5,
            )
        )

        reasons.append(EXPLAIN_OF_CHANGE_REASON.format(
            statement_type=facts.explained_statement_type))

        checklist.append(
            f"Review the explained {facts.explained_statement_type} as if it will be run on its own."
        )

        score += 5

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
    # SELECT THAT CAN LOCK ROWS OR CALLS AN UNREVIEWED FUNCTION
    # --------------------------------------------------------------

    select_locking, select_calls = select_side_effects(facts)

    if select_locking:

        reasons.append(LOCKING_READ_REASON)

        checklist.append(
            "Check the transaction this SELECT runs in and how long it holds its row locks."
        )

    if select_calls:

        reasons.append(unreviewed_call_reason(facts, select_calls))

        checklist.append(
            "Check what each called function does before running this SELECT."
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

    confidence = static_confidence(facts)

    if facts.lossy_parse:

        reasons.append(LOSSY_PARSE_REASON)

    if statement_type in STATEMENTS_WITH_ROW_IMPACT:

        reasons.append(OFFLINE_EVIDENCE_REASON)

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
    ):
        score = 20

    # A SELECT that can lock rows or calls an unreviewed function is MEDIUM at least.
    if (select_locking or select_calls) and score < SELECT_SIDE_EFFECT_FLOOR:

        factors.append(
            ScoreFactor(
                "MEDIUM minimum for a SELECT that can lock rows or calls an unreviewed function",
                SELECT_SIDE_EFFECT_FLOOR - score,
            )
        )

        score = SELECT_SIDE_EFFECT_FLOOR

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
# EXPLAIN ANALYZE
# ------------------------------------------------------------------

EXPLAIN_ANALYZE_UNREADABLE_POINTS = 50


def _score_explain_analyze(facts: StatementFacts, batch_size: int) -> StatementReport:
    """EXPLAIN ANALYZE is scored as the statement it runs.

    MySQL executes the explained statement to time it, so the risk is that
    statement's risk (an EXPLAIN ANALYZE of a write changes data). When the explained
    statement cannot be read, the effect is unknown: HIGH risk, LOW confidence.
    """
    inner = None
    if facts.explained_sql:
        parsed = parse_sql(facts.explained_sql)
        if len(parsed) == 1 and not parsed[0].parse_error:
            inner = parsed[0]

    if inner is None or inner.statement_type in {"UNKNOWN", "DESCRIBE"}:
        score = min(100, EXPLAIN_ANALYZE_UNREADABLE_POINTS + (10 if batch_size > 1 else 0))
        return StatementReport(
            facts=facts,
            score=score,
            risk_level=_risk_level(score),
            confidence="LOW",
            factors=[ScoreFactor(
                "EXPLAIN ANALYZE runs a statement that could not be read",
                EXPLAIN_ANALYZE_UNREADABLE_POINTS,
            )] + ([ScoreFactor("Part of a multi-statement batch", 10)] if batch_size > 1 else []),
            findings=[EXPLAIN_ANALYZE_UNREADABLE_REASON] + ([LOSSY_PARSE_REASON] if facts.lossy_parse else []),
            checklist=[
                "Do not run this EXPLAIN ANALYZE until the statement it explains has been reviewed.",
                "Review the explained statement as if it will be run on its own.",
            ],
        )

    if facts.lossy_parse:
        inner.lossy_parse = True
    report = score_statement(inner, batch_size)
    findings = [EXPLAIN_ANALYZE_REASON.format(statement_type=inner.statement_type)] + [
        finding for finding in report.findings
    ]
    return StatementReport(
        facts=facts,
        score=report.score,
        risk_level=report.risk_level,
        confidence=report.confidence,
        factors=[ScoreFactor("EXPLAIN ANALYZE runs the statement it explains", 0)] + report.factors,
        findings=findings,
        checklist=_deduplicate([
            "Do not run EXPLAIN ANALYZE on a statement you would not run on its own.",
        ] + report.checklist),
    )


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
