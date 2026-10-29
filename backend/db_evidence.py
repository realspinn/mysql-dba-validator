from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .db import (
    EVIDENCE_STATUSES,
    READ_ONLY_CHECK_SQL,
    READ_ONLY_TRANSACTION_ERRNO,
    AnalysisSession,
    ReadOnlyEvidenceConnection,
    get_connection,
    mysql_errno,
    mysql_error_status,
    read_only_value_confirmed,
    validate_readonly_select,
)
from .parser import DIALECT, StatementFacts, call_names, parse_sql

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

# UPDATE/DELETE plans are deliberately not collected, locally or through the connector:
# every evidence connection is a confirmed read-only session, MySQL refuses EXPLAIN
# UPDATE/DELETE there, and the validator never asks for write privileges to get a plan.
PLAN_NOT_COLLECTED_READ_ONLY = "plan_not_collected_read_only"
# A SELECT the read-only plan allowlist refuses (for example SELECT ... INTO): no plan
# request is sent for it.
PLAN_NOT_ELIGIBLE = "plan_not_eligible"
# MySQL returned the plan in a format the validator does not read: since 8.0.32 the
# explain_format variable can make plain EXPLAIN return TREE or JSON text. No plan is
# reported rather than a plan whose fields could not be read.
EXPLAIN_FORMAT_UNSUPPORTED = "explain_format_unsupported"
# The session stopped reading back as read-only before a plan request: nothing was sent,
# the connection was closed, and no further evidence is collected on it.
READ_ONLY_NOT_CONFIRMED = "read_only_not_confirmed"


class ReadOnlySessionLost(Exception):
    """Raised after the collector closed a connection whose session was no longer read-only."""


# Statement-shape refusals: EXPLAIN is not attempted for these.
_UNSUPPORTED_STATEMENT_ERRORS = (
    "empty_sql",
    "no_statement_detected",
    "multiple_statements_not_supported",
    "parse_error",
    "unsupported_statement_type",
    "statement_has_no_identifiable_table",
)


def evidence_status(evidence: "DatabaseEvidence") -> str:
    """Return the stable, browser-safe status for an evidence result."""
    if evidence.available:
        return "available"
    error = evidence.error or ""
    if error in EVIDENCE_STATUSES:
        return error
    if error.startswith(_UNSUPPORTED_STATEMENT_ERRORS):
        return "unsupported_statement_type"
    if error in ("database_unavailable", "remote_target_unavailable"):
        return "not_configured"
    if error in ("explain_build_failed", ""):
        return "explain_failed"
    return "collection_failed"


@dataclass
class DatabaseEvidence:
    available: bool = False
    database: Optional[str] = None
    tables: list[str] = field(default_factory=list)
    explain_available: bool = False
    estimated_rows: Optional[int] = None
    access_type: Optional[str] = None
    possible_keys: list[str] = field(default_factory=list)
    key: Optional[str] = None
    key_length: Optional[str] = None
    extra: Optional[str] = None
    full_table_scan: Optional[bool] = None
    # Number of rows EXPLAIN returned. The plan fields above describe the first row
    # only, so they are interpreted (scored) only for a one-row plan.
    plan_row_count: Optional[int] = None
    error: Optional[str] = None


def parse_explain_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize MySQL EXPLAIN response rows into a safer evidence dict."""
    if not rows:
        return {
            "row_count": 0,
            "estimated_rows": None,
            "access_type": None,
            "possible_keys": [],
            "key": None,
            "key_length": None,
            "extra": None,
            "full_table_scan": False,
        }

    row = rows[0]
    access_type = row.get("type") or row.get("access_type")
    possible_keys = row.get("possible_keys")
    key_value = row.get("key")
    key_length = row.get("key_len")
    extra = row.get("Extra") or row.get("extra")
    estimated_rows = row.get("rows")
    try:
        estimated_rows = int(estimated_rows)
    except (TypeError, ValueError):
        estimated_rows = None

    if isinstance(possible_keys, str):
        parts = [p.strip() for p in possible_keys.split(",") if p.strip()]
    elif possible_keys is None:
        parts = []
    else:
        parts = [str(p).strip() for p in possible_keys if str(p).strip()]

    normalized_key = key_value if isinstance(
        key_value, str) and key_value.strip() else None
    normalized_extra = extra.strip() if isinstance(extra, str) else extra
    full_table_scan = bool(
        access_type == "ALL"
        or (isinstance(normalized_extra, str) and "full table scan" in normalized_extra.lower())
    )

    return {
        "row_count": len(rows),
        "estimated_rows": estimated_rows,
        "access_type": access_type,
        "possible_keys": parts,
        "key": normalized_key,
        "key_length": str(key_length).strip() if key_length not in (None, "") else None,
        "extra": normalized_extra,
        "full_table_scan": full_table_scan,
    }


def _safe_explain_sql(statement_sql: str) -> str:
    """Create a read-only EXPLAIN statement from a validated parsed statement.

    Only supported statement types are accepted in collect_statement_evidence(),
    and the SQL is rebuilt as an EXPLAIN prefix rather than executing the user
    statement itself. This keeps the evidence layer read-only and explicit.
    """
    cleaned = (statement_sql or "").strip()
    if not cleaned:
        raise ValueError("empty_sql")

    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()

    if not cleaned:
        raise ValueError("empty_sql")

    return f"EXPLAIN {cleaned};"


# Function policy for execution-plan requests (contract section 12).
#
# EXPLAIN can evaluate functions while planning, and MySQL resolves an unqualified
# name that is not a built-in to a loadable or stored function, which can run any SQL
# its definer allows. "sqlglot recognises the name" is not evidence that MySQL has a
# built-in of that name (SPLIT, LEVENSHTEIN, STRUCT, MAP, LIST and DATETIME are known
# to sqlglot from other dialects; MySQL has no such built-ins). So a plan is requested
# only when every word written before "(" in the outgoing text, and in the submitted
# text, is on this reviewed list. Anything else, and any quoted or schema-qualified
# call, gets no plan request (fail closed: the statement is still analysed, only the
# plan is not collected).
#
# PERMITTED_FUNCTIONS: MySQL 8.4 built-in functions (MySQL 8.4 Reference Manual,
# "Built-In Function and Operator Reference") that compute a value from their
# arguments. Per the manual ("Function Name Parsing and Resolution"), an unqualified
# built-in name resolves to the built-in; a stored function of the same name must be
# called qualified, and qualified calls are refused here. Deliberately excluded:
# functions that sleep, wait, lock, read files, or read or change session state
# (SLEEP, BENCHMARK, GET_LOCK, LOAD_FILE, LAST_INSERT_ID, ...), non-deterministic ones
# (RAND, UUID, SYSDATE), table functions (JSON_TABLE), and anything not reviewed.
PERMITTED_FUNCTIONS = frozenset("""
    COUNT SUM AVG MIN MAX GROUP_CONCAT BIT_AND BIT_OR BIT_XOR STD STDDEV STDDEV_POP
    STDDEV_SAMP VAR_POP VAR_SAMP VARIANCE JSON_ARRAYAGG JSON_OBJECTAGG ANY_VALUE GROUPING
    ROW_NUMBER RANK DENSE_RANK PERCENT_RANK CUME_DIST NTILE LAG LEAD FIRST_VALUE
    LAST_VALUE NTH_VALUE
    IF IFNULL NULLIF COALESCE GREATEST LEAST ISNULL
    ASCII BIN BIT_LENGTH CHAR CHAR_LENGTH CHARACTER_LENGTH CONCAT CONCAT_WS ELT FIELD
    FIND_IN_SET FORMAT FROM_BASE64 HEX INSERT INSTR LCASE LEFT LENGTH LOCATE LOWER LPAD
    LTRIM MID OCT OCTET_LENGTH ORD POSITION QUOTE REPEAT REPLACE REVERSE RIGHT RPAD
    RTRIM SOUNDEX SPACE STRCMP SUBSTR SUBSTRING SUBSTRING_INDEX TO_BASE64 TRIM UCASE
    UNHEX UPPER REGEXP_INSTR REGEXP_LIKE REGEXP_REPLACE REGEXP_SUBSTR
    ABS ACOS ASIN ATAN ATAN2 CEIL CEILING CONV COS COT CRC32 DEGREES EXP FLOOR LN LOG
    LOG10 LOG2 MOD PI POW POWER RADIANS ROUND SIGN SIN SQRT TAN TRUNCATE
    ADDDATE ADDTIME CONVERT_TZ CURDATE CURRENT_DATE CURRENT_TIME CURRENT_TIMESTAMP
    CURTIME DATE DATE_ADD DATE_FORMAT DATE_SUB DATEDIFF DAY DAYNAME DAYOFMONTH DAYOFWEEK
    DAYOFYEAR EXTRACT FROM_DAYS FROM_UNIXTIME HOUR LAST_DAY LOCALTIME LOCALTIMESTAMP
    MAKEDATE MAKETIME MICROSECOND MINUTE MONTH MONTHNAME NOW PERIOD_ADD PERIOD_DIFF
    QUARTER SEC_TO_TIME SECOND STR_TO_DATE SUBDATE SUBTIME TIME TIME_FORMAT TIME_TO_SEC
    TIMEDIFF TIMESTAMP TIMESTAMPADD TIMESTAMPDIFF TO_DAYS TO_SECONDS UNIX_TIMESTAMP
    UTC_DATE UTC_TIME UTC_TIMESTAMP WEEK WEEKDAY WEEKOFYEAR YEAR YEARWEEK
    CAST CONVERT
    JSON_ARRAY JSON_CONTAINS JSON_CONTAINS_PATH JSON_DEPTH JSON_EXTRACT JSON_KEYS
    JSON_LENGTH JSON_OBJECT JSON_OVERLAPS JSON_QUOTE JSON_SEARCH JSON_TYPE JSON_UNQUOTE
    JSON_VALID JSON_VALUE
    MD5 SHA SHA1 SHA2
""".split())

# MySQL 8.4 reserved words that are followed by "(" as syntax, never as a function
# call (IN (...), EXISTS (...), CAST(x AS DECIMAL(10, 2))). A reserved word cannot be
# an unquoted stored-function name. Non-reserved keywords that MySQL could resolve to
# a stored function in expression position (ANY, SOME, AGAINST, ESCAPE, DATETIME) are
# deliberately absent: statements using them get no plan request.
PERMITTED_SYNTAX_WORDS = frozenset("""
    IN EXISTS AND OR NOT XOR ON USING FROM JOIN WHERE HAVING BY THEN ELSE WHEN CASE IS
    DISTINCT ALL LIKE BETWEEN INTERVAL LATERAL SELECT DIV REGEXP RLIKE BINARY OF AS
    OVER INDEX KEY UNION ROW DECIMAL NUMERIC FLOAT DOUBLE
""".split())

PERMITTED_CALL_NAMES = PERMITTED_FUNCTIONS | PERMITTED_SYNTAX_WORDS


def call_names_permitted(names: Optional[frozenset[str]]) -> bool:
    """True only for a known set of call names that are all on the reviewed list."""
    return names is not None and names <= PERMITTED_CALL_NAMES


def _outgoing_call_names(body: str) -> Optional[frozenset[str]]:
    try:
        return call_names(Dialect.get_or_raise(DIALECT).tokenize(body))
    except Exception:
        return None


def _is_plain_select_explain(explain_sql: str) -> bool:
    """The last check before anything is sent: True only for EXPLAIN of one plain SELECT.

    Checks the exact text that would be sent, not the earlier classification (the
    regenerated SQL can differ from the classified statement). The body must parse
    strictly as exactly one SELECT with no INTO, no locking clause and no function the
    parser does not know, and every word written before "(" in that exact text must be
    on the reviewed list (PERMITTED_CALL_NAMES): a name sqlglot knows is not
    necessarily a MySQL built-in. UPDATE, DELETE, the executing ANALYZE form of EXPLAIN
    and every other statement fail this check, so they are never sent.
    """
    prefix, suffix = "EXPLAIN ", ";"
    if not (explain_sql.startswith(prefix) and explain_sql.endswith(suffix)):
        return False
    body = explain_sql[len(prefix):-len(suffix)]
    if not validate_readonly_select(body):
        return False
    try:
        expressions = sqlglot.parse(body, read="mysql", error_level=sqlglot.ErrorLevel.RAISE)
    except Exception:
        return False
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Select):
        return False
    statement = expressions[0]
    if statement.find(exp.Into) or statement.find(exp.Lock):
        return False
    if any(isinstance(function, exp.Anonymous) for function in statement.find_all(exp.Func)):
        return False
    # sqlglot regenerates FROM DUAL as FROM `DUAL`, and a quoted reserved word names an
    # ordinary table (MySQL 8.4 manual: Keywords and Reserved Words), so the plan would be
    # of a table called DUAL, not of the statement. An unqualified DUAL is never planned.
    if any(table.name.upper() == "DUAL" and not table.args.get("db")
           for table in statement.find_all(exp.Table)):
        return False
    return call_names_permitted(_outgoing_call_names(body))


def _confirm_read_only(cursor: Any) -> bool:
    """Read the session's read-only state back on the cursor that will send the plan."""
    cursor.execute(READ_ONLY_CHECK_SQL)
    rows = cursor.fetchall()
    return len(rows) == 1 and read_only_value_confirmed(rows[0])


def _add_database_name(conn: Any, evidence: DatabaseEvidence) -> DatabaseEvidence:
    if evidence.database:
        return evidence

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DATABASE() AS db_name")
            row = cur.fetchone()
            if row:
                evidence.database = row.get("db_name")
    except Exception:
        pass

    return evidence


def select_not_eligible_for_plan(facts: Any) -> bool:
    """True for a submitted SELECT that gets no plan request, from its parse facts alone.

    A SELECT that only parsed with the permissive fallback (which can silently drop
    tokens, as in a trailing SELECT ... INTO or PROCEDURE ANALYSE()), or whose text
    calls a name outside the reviewed function list, or a quoted or schema-qualified
    name: its regenerated text is not known to be the submitted statement, or a call
    may resolve to a stored function. The collector applies this itself; callers may
    apply it first to skip the call.
    """
    if facts.statement_type != "SELECT":
        return False
    if bool(getattr(facts, "lossy_parse", False)):
        return True
    return not call_names_permitted(getattr(facts, "source_call_names", None))


def _single_planned_statement(sql: str) -> tuple[Optional[StatementFacts], Optional[DatabaseEvidence]]:
    """The one SELECT/UPDATE/DELETE with a table in `sql`, or the evidence saying why not."""
    if not sql or not sql.strip():
        return None, DatabaseEvidence(available=False, error="empty_sql")

    statements = parse_sql(sql)
    if not statements:
        return None, DatabaseEvidence(available=False, error="no_statement_detected")
    if len(statements) > 1:
        return None, DatabaseEvidence(available=False, error="multiple_statements_not_supported")

    fact = statements[0]
    if fact.parse_error:
        return None, DatabaseEvidence(available=False, error=f"parse_error: {fact.parse_error}")

    statement_type = fact.statement_type
    if statement_type not in {"SELECT", "UPDATE", "DELETE"}:
        return None, DatabaseEvidence(
            available=False,
            error=f"unsupported_statement_type: {statement_type}. EXPLAIN evidence is collected only for SELECT statements.",
        )

    if not fact.tables:
        return None, DatabaseEvidence(available=False, error="statement_has_no_identifiable_table")

    if statement_type in {"UPDATE", "DELETE"}:
        # Never sent, and no connection is opened for it: a write plan needs a
        # writable session and write privileges.
        return None, DatabaseEvidence(available=False, error=PLAN_NOT_COLLECTED_READ_ONLY)

    return fact, None


def collect_statement_evidence(
    sql: str,
    connection: Any = None,
    session: Optional[AnalysisSession] = None,
    source_facts: Optional[StatementFacts] = None,
) -> DatabaseEvidence:
    """Collect limited read-only EXPLAIN evidence for one statement (collect_select_plan).

    Only one verified plain SELECT is ever planned, as `EXPLAIN <SELECT>` on the caller's
    confirmed read-only connection; the statement itself is never executed and no result
    rows are fetched. UPDATE/DELETE return plan_not_collected_read_only; any other SELECT
    returns plan_not_eligible; other statement types are refused.
    """
    return collect_select_plan(sql, connection=connection, session=session, source_facts=source_facts)


def collect_select_plan(
    sql: str,
    connection: Any = None,
    session: Optional[AnalysisSession] = None,
    source_facts: Optional[StatementFacts] = None,
) -> DatabaseEvidence:
    """Collect limited read-only EXPLAIN evidence for a SELECT.

    Only one verified plain SELECT is ever planned, on a connection confirmed
    read-only. UPDATE/DELETE return plan_not_collected_read_only; any other SELECT
    returns plan_not_eligible; other statement types are refused. Nothing is executed.

    source_facts are the parse facts of the statement as submitted, when `sql` is a
    regenerated text (validation passes report.facts.raw_sql). Eligibility is checked
    on both, so a partly read or rewritten statement is refused here even when a
    caller skips its own check.

    Immediately before the plan request, the session's read-only state is read back on
    the same cursor. If it is not confirmed, nothing is sent, the connection is closed
    and ReadOnlySessionLost is raised: the caller must stop collecting on it.
    """
    fact, refused = _single_planned_statement(sql)
    if refused is not None:
        return refused

    if select_not_eligible_for_plan(fact) or (
            source_facts is not None and select_not_eligible_for_plan(source_facts)):
        return DatabaseEvidence(available=False, error=PLAN_NOT_ELIGIBLE)

    try:
        explain_sql = _safe_explain_sql(fact.raw_sql)
    except Exception:  # pragma: no cover - defensive parsing guard
        return DatabaseEvidence(available=False, error="explain_build_failed")

    if not _is_plain_select_explain(explain_sql):
        # Nothing is sent and no connection is opened: no retry, no other connection.
        return DatabaseEvidence(available=False, error=PLAN_NOT_ELIGIBLE)

    owns_connection = connection is None
    if connection is not None:
        conn = connection
    elif session is None:
        conn = get_connection()
    else:
        conn = get_connection(session=session)
    if conn is None:
        return DatabaseEvidence(available=False, error="database_unavailable")

    try:
        if not isinstance(conn, ReadOnlyEvidenceConnection):
            # Only a session confirmed read-only (backend.db.enforce_read_only_session)
            # ever receives a plan request.
            return DatabaseEvidence(available=False, error="collection_failed")

        rows: list = []
        try:
            cur = conn.cursor()
            try:
                confirmed = _confirm_read_only(cur)
                if confirmed:
                    cur.execute(explain_sql)
                    rows = cur.fetchall()
            finally:
                try:
                    cur.close()
                except Exception:
                    pass
        except Exception as exc:
            # Only the MySQL error number is used; the driver message never leaves here.
            if mysql_errno(exc) == READ_ONLY_TRANSACTION_ERRNO:
                return DatabaseEvidence(available=False, error=PLAN_NOT_COLLECTED_READ_ONLY)
            return DatabaseEvidence(available=False, error=mysql_error_status(exc, "explain_failed"))

        if not confirmed:
            try:
                conn.close()
            except Exception:
                pass
            raise ReadOnlySessionLost(READ_ONLY_NOT_CONFIRMED)

        if rows and isinstance(rows[0], dict) and "type" not in rows[0]:
            # Not the tabular format (explain_format TREE or JSON): report no plan.
            return DatabaseEvidence(available=False, error=EXPLAIN_FORMAT_UNSUPPORTED)

        evidence = DatabaseEvidence(
            available=bool(rows),
            tables=list(fact.tables),
            explain_available=bool(rows),
        )

        if not rows:
            return _add_database_name(conn, evidence)

        explain = parse_explain_rows(rows)
        evidence.estimated_rows = explain["estimated_rows"]
        evidence.access_type = explain["access_type"]
        evidence.possible_keys = explain["possible_keys"]
        evidence.key = explain["key"]
        evidence.key_length = explain["key_length"]
        evidence.extra = explain["extra"]
        evidence.full_table_scan = explain["full_table_scan"]
        evidence.plan_row_count = explain["row_count"]
        evidence.available = True
        evidence.explain_available = True

        return _add_database_name(conn, evidence)
    finally:
        if owns_connection:
            try:
                conn.close()
            except Exception:
                pass
