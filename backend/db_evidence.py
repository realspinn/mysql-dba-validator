from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .db import (
    EVIDENCE_STATUSES,
    READ_ONLY_TRANSACTION_ERRNO,
    AnalysisSession,
    ReadOnlyEvidenceConnection,
    get_connection,
    mysql_errno,
    mysql_error_status,
    validate_readonly_select,
)
from .parser import parse_sql

# Local DML plans are deliberately not collected: the local evidence connection is
# read-only and MySQL refuses EXPLAIN UPDATE/DELETE there.
PLAN_NOT_COLLECTED_READ_ONLY = "plan_not_collected_read_only"

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
    error: Optional[str] = None


def parse_explain_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalize MySQL EXPLAIN response rows into a safer evidence dict."""
    if not rows:
        return {
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


def _is_plain_select_explain(explain_sql: str) -> bool:
    """Return True only when explain_sql is EXPLAIN of exactly one plain SELECT (no INTO).

    Checks the exact text that would be sent, not the earlier classification: the
    regenerated SQL can differ from the classified statement.
    """
    prefix, suffix = "EXPLAIN ", ";"
    if not (explain_sql.startswith(prefix) and explain_sql.endswith(suffix)):
        return False
    return validate_readonly_select(explain_sql[len(prefix):-len(suffix)])


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


def collect_statement_evidence(
    sql: str,
    connection: Any = None,
    session: Optional[AnalysisSession] = None,
) -> DatabaseEvidence:
    """Collect limited read-only EXPLAIN evidence for supported statement types.

    Supported: SELECT, UPDATE, DELETE.
    Destructive or unsupported statements are refused and returned with an error
    payload rather than executed. On the local ReadOnlyEvidenceConnection only one
    verified plain SELECT is planned; UPDATE/DELETE return plan_not_collected_read_only.
    """
    if not sql or not sql.strip():
        return DatabaseEvidence(available=False, error="empty_sql")

    statements = parse_sql(sql)
    if not statements:
        return DatabaseEvidence(available=False, error="no_statement_detected")
    if len(statements) > 1:
        return DatabaseEvidence(available=False, error="multiple_statements_not_supported")

    fact = statements[0]
    if fact.parse_error:
        return DatabaseEvidence(available=False, error=f"parse_error: {fact.parse_error}")

    statement_type = fact.statement_type
    if statement_type not in {"SELECT", "UPDATE", "DELETE"}:
        return DatabaseEvidence(
            available=False,
            error=f"unsupported_statement_type: {statement_type}. EXPLAIN evidence is collected only for SELECT, UPDATE, and DELETE statements.",
        )

    if not fact.tables:
        return DatabaseEvidence(available=False, error="statement_has_no_identifiable_table")

    try:
        explain_sql = _safe_explain_sql(fact.raw_sql)
    except Exception:  # pragma: no cover - defensive parsing guard
        return DatabaseEvidence(available=False, error="explain_build_failed")

    if connection is not None:
        conn = connection
    elif session is None:
        conn = get_connection()
    else:
        conn = get_connection(session=session)
    if conn is None:
        return DatabaseEvidence(available=False, error="database_unavailable")

    read_only = isinstance(conn, ReadOnlyEvidenceConnection)
    if read_only and not _is_plain_select_explain(explain_sql):
        # The local read-only connection receives a plan request only for one verified
        # plain SELECT. Nothing is sent for anything else: no retry, no other connection.
        if statement_type in {"UPDATE", "DELETE"}:
            return DatabaseEvidence(available=False, error=PLAN_NOT_COLLECTED_READ_ONLY)
        return DatabaseEvidence(available=False, error="unsupported_statement_type")

    try:
        cur = conn.cursor()
        try:
            cur.execute(explain_sql)
            rows = cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:
                pass
    except Exception as exc:
        # Only the MySQL error number is used; the driver message never leaves here.
        if read_only and mysql_errno(exc) == READ_ONLY_TRANSACTION_ERRNO:
            return DatabaseEvidence(available=False, error=PLAN_NOT_COLLECTED_READ_ONLY)
        return DatabaseEvidence(available=False, error=mysql_error_status(exc, "explain_failed"))

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
    evidence.available = True
    evidence.explain_available = True

    return _add_database_name(conn, evidence)
