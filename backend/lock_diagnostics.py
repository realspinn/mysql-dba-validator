"""InnoDB lock waits: who is waiting, who is blocking it, and on which table.

An optional diagnostic, separate from SQL validation. It reads
performance_schema.data_lock_waits joined to data_locks (and, when permitted,
threads for the connection IDs) with fixed, application-owned queries on a
connection the caller has already opened and confirmed read-only. It never reads
LOCK_DATA (key values of locked rows) or any SQL text, never sends submitted SQL,
never kills, commits or rolls back anything, and keeps nothing.

The result is one snapshot: locks change while it is read and after it is shown.
Nothing here feeds a validation result, score, confidence or row scope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .db import mysql_errno, read_only_value_confirmed

SOURCE = "performance_schema.data_lock_waits, performance_schema.data_locks"
SCOPE = "innodb_lock_waits_snapshot"
WAIT_LIMIT = 20
NOTE = ("A snapshot of InnoDB lock waits on tables in this database at the time shown. "
        "Waits change quickly: a blocker shown here may already have finished. Metadata "
        "locks and other kinds of contention are not included.")

STATE_SQL = ("SELECT @@SESSION.transaction_read_only AS transaction_read_only, "
             "@@GLOBAL.performance_schema AS performance_schema")
# One row more than the limit shows whether more waits exist. The only bound value
# is the schema; it selects which waits are listed, it is not a permission boundary.
LOCK_WAITS_SQL = (
    "SELECT w.REQUESTING_THREAD_ID, w.BLOCKING_THREAD_ID, r.OBJECT_SCHEMA, r.OBJECT_NAME, "
    "r.PARTITION_NAME, r.INDEX_NAME, r.LOCK_TYPE, r.LOCK_MODE AS REQUESTED_MODE, "
    "b.LOCK_MODE AS BLOCKING_MODE "
    "FROM performance_schema.data_lock_waits w "
    "JOIN performance_schema.data_locks r "
    "ON r.ENGINE = w.ENGINE AND r.ENGINE_LOCK_ID = w.REQUESTING_ENGINE_LOCK_ID "
    "JOIN performance_schema.data_locks b "
    "ON b.ENGINE = w.ENGINE AND b.ENGINE_LOCK_ID = w.BLOCKING_ENGINE_LOCK_ID "
    "WHERE w.ENGINE = 'INNODB' AND r.OBJECT_SCHEMA = %s "
    f"ORDER BY w.REQUESTING_THREAD_ID, w.BLOCKING_THREAD_ID LIMIT {WAIT_LIMIT + 1}"
)
# Optional: connection IDs (what SHOW PROCESSLIST shows) and seconds in the current
# state, for the threads listed. Without SELECT on these columns they are unverified.
THREADS_SQL = ("SELECT THREAD_ID, PROCESSLIST_ID, PROCESSLIST_TIME FROM performance_schema.threads "
               "WHERE THREAD_ID IN ({})")
FIXED_SQL = (STATE_SQL, LOCK_WAITS_SQL, THREADS_SQL)

STATUSES = (
    "available",
    "no_lock_waits",
    "performance_schema_disabled",
    "insufficient_privileges",
    "collection_failed",
    "unexpected_response",
    # Connection outcomes, as the evidence connection reports them.
    "auth_failed",
    "unreachable",
    "database_not_found",
)

_PRIVILEGE_ERRNOS = frozenset({1142, 1143, 1227})
_LOCK_TYPES = frozenset({"RECORD", "TABLE"})
_MAX_TEXT = 256


class _Unexpected(Exception):
    """A server response that does not have the documented shape."""


def result(schema: str, status: str, **fields: Any) -> dict[str, Any]:
    """The response for one request. Waits are present only when status is available."""
    return {
        "status": status if status in STATUSES else "collection_failed",
        "scope": SCOPE,
        "source": SOURCE,
        "schema": schema,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "limit": WAIT_LIMIT,
        "more_waits": fields.get("more_waits", False),
        "sessions": fields.get("sessions"),
        "waits": fields.get("waits", []),
        "note": NOTE,
    }


def _id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _Unexpected
    return value


def _text(value: Any, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value or len(value) > _MAX_TEXT:
        raise _Unexpected
    return value


def _wait(row: Any, schema: str) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise _Unexpected
    if row.get("OBJECT_SCHEMA") != schema:
        raise _Unexpected
    lock_type = row.get("LOCK_TYPE")
    if lock_type not in _LOCK_TYPES:
        raise _Unexpected
    return {
        "waiting": {"thread_id": _id(row.get("REQUESTING_THREAD_ID"))},
        "blocking": {"thread_id": _id(row.get("BLOCKING_THREAD_ID"))},
        "table": _text(row.get("OBJECT_NAME")),
        "partition": _text(row.get("PARTITION_NAME"), nullable=True),
        "index": _text(row.get("INDEX_NAME"), nullable=True),
        "lock_type": lock_type,
        "requested_mode": _text(row.get("REQUESTED_MODE")),
        "blocking_mode": _text(row.get("BLOCKING_MODE")),
    }


def _sessions(cursor: Any, waits: list[dict[str, Any]]) -> str:
    """Add connection IDs and seconds in state; 'available' or 'unverified'."""
    thread_ids = sorted({side["thread_id"] for wait in waits for side in (wait["waiting"], wait["blocking"])})
    try:
        cursor.execute(THREADS_SQL.format(", ".join(["%s"] * len(thread_ids))), tuple(thread_ids))
    except Exception as exc:
        if mysql_errno(exc) in _PRIVILEGE_ERRNOS:
            for wait in waits:
                for side in (wait["waiting"], wait["blocking"]):
                    side.update(session_id=None, seconds_in_state=None)
            return "unverified"
        raise
    rows = cursor.fetchmany(len(thread_ids) + 1)
    if len(rows) > len(thread_ids):
        raise _Unexpected
    found: dict[int, tuple[int, int | None]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise _Unexpected
        thread_id = _id(row.get("THREAD_ID"))
        if thread_id not in thread_ids:
            raise _Unexpected
        seconds = row.get("PROCESSLIST_TIME")
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0):
            raise _Unexpected
        session = row.get("PROCESSLIST_ID")
        found[thread_id] = (None if session is None else _id(session), seconds)
    for wait in waits:
        for side in (wait["waiting"], wait["blocking"]):
            # A thread that ended after the first read has no row: unknown, not zero.
            side["session_id"], side["seconds_in_state"] = found.get(side["thread_id"], (None, None))
    return "available"


def collect_lock_waits(connection: Any, schema: str) -> dict[str, Any]:
    """Read the schema's current InnoDB lock waits on a confirmed read-only connection.

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

        cursor.execute(LOCK_WAITS_SQL, (schema,))
        rows = cursor.fetchmany(WAIT_LIMIT + 2)
        if len(rows) > WAIT_LIMIT + 1:
            return result(schema, "unexpected_response")
        waits = [_wait(row, schema) for row in rows]
        more = len(waits) > WAIT_LIMIT
        waits = waits[:WAIT_LIMIT]
        if not waits:
            return result(schema, "no_lock_waits")
        sessions = _sessions(cursor, waits)
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

    return result(schema, "available", more_waits=more, sessions=sessions, waits=waits)
