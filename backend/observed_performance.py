"""Historical schema workload: statement digest summaries MySQL has already recorded.

An optional diagnostic, separate from SQL validation. It reads
performance_schema.events_statements_summary_by_digest for the selected schema with
fixed, application-owned queries on a connection the caller has already opened and
confirmed read-only. It never sends submitted SQL, never changes instrumentation and
keeps nothing.

The rows are aggregated statement families (one per schema and normalized digest),
accumulated since each family was first seen. They are not measurements of any SQL a
user submits, and nothing here feeds a validation result, score, confidence or row
scope.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .db import mysql_errno, read_only_value_confirmed

SOURCE = "performance_schema.events_statements_summary_by_digest"
SCOPE = "historical_schema_workload"
FAMILY_LIMIT = 5
DIGEST_TEXT_MAX_CHARS = 1024
NOTE = ("Statement families MySQL recorded for this schema, accumulated since each was first "
        "seen. They are not measurements of any SQL you submitted and never change a validation result.")

# The session's read-only state (read back on the cursor that sends the queries) and
# whether the Performance Schema is enabled. Neither needs a privilege.
STATE_SQL = ("SELECT @@SESSION.transaction_read_only AS transaction_read_only, "
             "@@GLOBAL.performance_schema AS performance_schema")
# Digest aggregation needs both consumers. Optional: without SELECT on these two
# columns of setup_consumers the consumer state is reported as unverified.
CONSUMERS_SQL = ("SELECT NAME, ENABLED FROM performance_schema.setup_consumers "
                 "WHERE NAME IN ('global_instrumentation', 'statements_digest')")
# One row more than the limit shows whether more families exist.
FAMILIES_SQL = (
    "SELECT DIGEST, DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT, AVG_TIMER_WAIT, MAX_TIMER_WAIT, "
    "QUANTILE_95, SUM_LOCK_TIME, SUM_ROWS_EXAMINED, SUM_ROWS_SENT, SUM_ROWS_AFFECTED, "
    "SUM_NO_INDEX_USED, FIRST_SEEN, LAST_SEEN "
    "FROM performance_schema.events_statements_summary_by_digest "
    "WHERE SCHEMA_NAME = %s AND DIGEST IS NOT NULL "
    f"ORDER BY SUM_TIMER_WAIT DESC, COUNT_STAR DESC, DIGEST LIMIT {FAMILY_LIMIT + 1}"
)
# Statements counted in the catch-all row once the table is full (server-wide).
OVERFLOW_SQL = ("SELECT COUNT_STAR FROM performance_schema.events_statements_summary_by_digest "
                "WHERE DIGEST IS NULL")
FIXED_SQL = (STATE_SQL, CONSUMERS_SQL, FAMILIES_SQL, OVERFLOW_SQL)

STATUSES = (
    "available",
    "no_observations",
    "performance_schema_disabled",
    "digest_consumer_disabled",
    "insufficient_privileges",
    "collection_failed",
    "unexpected_response",
    # Connection outcomes, as the evidence connection reports them.
    "auth_failed",
    "unreachable",
    "database_not_found",
)

_PRIVILEGE_ERRNOS = frozenset({1142, 1143, 1227})  # table, column, specific privilege
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PICOSECONDS_PER_MS = 1_000_000_000


class _Unexpected(Exception):
    """A server response that does not have the documented shape."""


def result(schema: str, status: str, **fields: Any) -> dict[str, Any]:
    """The response for one request. Families are present only when status is available."""
    return {
        "status": status if status in STATUSES else "collection_failed",
        "scope": SCOPE,
        "source": SOURCE,
        "schema": schema,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "limit": FAMILY_LIMIT,
        "digest_consumer": fields.get("digest_consumer"),
        "more_families": fields.get("more_families", False),
        "overflow_executions": fields.get("overflow_executions"),
        "families": fields.get("families", []),
        "note": NOTE,
    }


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _Unexpected
    return value


def _timer(value: Any) -> int | None:
    """A picosecond value; None stays None (not measured), never 0."""
    return None if value is None else _count(value)


def _ms(picoseconds: int | None) -> float | None:
    return None if picoseconds is None else round(picoseconds / _PICOSECONDS_PER_MS, 3)


def _timestamp(value: Any) -> str:
    if not isinstance(value, datetime):
        raise _Unexpected
    return value.isoformat()


def _family(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise _Unexpected
    digest = row.get("DIGEST")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise _Unexpected
    text = row.get("DIGEST_TEXT")
    if text is not None and not isinstance(text, str):
        raise _Unexpected
    total = _timer(row.get("SUM_TIMER_WAIT"))
    avg = _timer(row.get("AVG_TIMER_WAIT"))
    maximum = _timer(row.get("MAX_TIMER_WAIT"))
    p95 = _timer(row.get("QUANTILE_95"))
    lock = _timer(row.get("SUM_LOCK_TIME"))
    # Untimed statements still count but add nothing to the timer sums, so a zero total
    # is "no time recorded", not a measured zero; the reason is not established here.
    # Lock time is recorded separately (MySQL 8.4.13 kept it with TIMED = 'NO'), so it
    # is reported either way.
    timed = bool(total)
    return {
        "digest": digest,
        "digest_text": text[:DIGEST_TEXT_MAX_CHARS] if text is not None else None,
        "digest_text_truncated": text is not None and len(text) > DIGEST_TEXT_MAX_CHARS,
        "executions": _count(row.get("COUNT_STAR")),
        "timing": "recorded" if timed else "not_recorded",
        "total_time_ms": _ms(total) if timed else None,
        "avg_time_ms": _ms(avg) if timed else None,
        "max_time_ms": _ms(maximum) if timed else None,
        "p95_time_ms": _ms(p95) if timed and p95 else None,
        "lock_time_ms": _ms(lock),
        "rows_examined": _count(row.get("SUM_ROWS_EXAMINED")),
        "rows_sent": _count(row.get("SUM_ROWS_SENT")),
        "rows_affected": _count(row.get("SUM_ROWS_AFFECTED")),
        "no_index_used": _count(row.get("SUM_NO_INDEX_USED")),
        "first_seen": _timestamp(row.get("FIRST_SEEN")),
        "last_seen": _timestamp(row.get("LAST_SEEN")),
    }


def _consumer_state(cursor: Any) -> str:
    """'enabled', 'disabled' or 'unverified' (no privilege to read setup_consumers)."""
    try:
        cursor.execute(CONSUMERS_SQL)
    except Exception as exc:
        if mysql_errno(exc) in _PRIVILEGE_ERRNOS:
            return "unverified"
        raise
    rows = cursor.fetchmany(3)
    if len(rows) != 2 or not all(isinstance(row, dict) for row in rows):
        raise _Unexpected
    states = {row.get("NAME"): row.get("ENABLED") for row in rows}
    if set(states) != {"global_instrumentation", "statements_digest"}:
        raise _Unexpected
    if not set(states.values()) <= {"YES", "NO"}:
        raise _Unexpected
    return "enabled" if all(value == "YES" for value in states.values()) else "disabled"


def collect_observed_performance(connection: Any, schema: str) -> dict[str, Any]:
    """Read the schema's top statement families on a confirmed read-only connection.

    The caller validated the schema, opened the connection with it bound and owns
    closing it. Any failure becomes a status; no partly validated result is returned.
    """
    try:
        cursor = connection.cursor()
    except Exception:
        return result(schema, "collection_failed")
    try:
        cursor.execute(STATE_SQL)
        state = cursor.fetchmany(2)
        if len(state) != 1 or not isinstance(state[0], dict):
            return result(schema, "unexpected_response")
        if not read_only_value_confirmed(state[0]):
            # Fail closed: nothing more is sent on a session no longer read-only.
            return result(schema, "collection_failed")
        enabled = state[0].get("performance_schema")
        if enabled in (0, "0"):
            return result(schema, "performance_schema_disabled")
        if enabled not in (1, "1"):
            return result(schema, "unexpected_response")

        consumer = _consumer_state(cursor)
        if consumer == "disabled":
            return result(schema, "digest_consumer_disabled", digest_consumer="disabled")

        cursor.execute(FAMILIES_SQL, (schema,))
        rows = cursor.fetchmany(FAMILY_LIMIT + 2)
        if len(rows) > FAMILY_LIMIT + 1:
            return result(schema, "unexpected_response", digest_consumer=consumer)
        families = [_family(row) for row in rows[:FAMILY_LIMIT]]
        more = len(rows) > FAMILY_LIMIT
        if more:
            _family(rows[FAMILY_LIMIT])  # validated too, though not returned

        cursor.execute(OVERFLOW_SQL)
        overflow_rows = cursor.fetchmany(2)
        if len(overflow_rows) > 1 or not all(isinstance(row, dict) for row in overflow_rows):
            return result(schema, "unexpected_response", digest_consumer=consumer)
        overflow = _count(overflow_rows[0].get("COUNT_STAR")) if overflow_rows else 0
    except _Unexpected:
        return result(schema, "unexpected_response")
    except Exception as exc:
        status = "insufficient_privileges" if mysql_errno(exc) in _PRIVILEGE_ERRNOS else "collection_failed"
        return result(schema, status)
    finally:
        try:
            cursor.close()
        except Exception:
            pass

    return result(schema, "available" if families else "no_observations",
                  digest_consumer=consumer, more_families=more,
                  overflow_executions=overflow, families=families)
