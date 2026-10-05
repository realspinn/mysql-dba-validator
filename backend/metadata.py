"""Bounded metadata-only evidence collection for V2.2A."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
import time
from typing import Any, Optional

from .db import AnalysisSession, get_client, get_connection


TABLES_SQL = """SELECT ENGINE, TABLE_ROWS, DATA_LENGTH, INDEX_LENGTH, CREATE_OPTIONS, UPDATE_TIME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
LIMIT %s"""
STATISTICS_SQL = """SELECT INDEX_NAME, COLUMN_NAME, SEQ_IN_INDEX, NON_UNIQUE, CARDINALITY, SUB_PART, INDEX_TYPE
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY INDEX_NAME, SEQ_IN_INDEX
LIMIT %s"""
COLUMNS_SQL = """SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, ORDINAL_POSITION
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
ORDER BY ORDINAL_POSITION
LIMIT %s"""

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_REQUIRED_FIELDS = {
    "tables": {"ENGINE", "TABLE_ROWS", "DATA_LENGTH", "INDEX_LENGTH", "CREATE_OPTIONS", "UPDATE_TIME"},
    "statistics": {"INDEX_NAME", "COLUMN_NAME", "SEQ_IN_INDEX", "NON_UNIQUE", "CARDINALITY", "SUB_PART", "INDEX_TYPE"},
    "columns": {"COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE", "COLUMN_KEY", "ORDINAL_POSITION"},
}


@dataclass
class MetadataConfig:
    max_tables: int = 10
    max_metadata_rows: int = 500
    query_timeout_seconds: float = 2.0
    total_timeout_seconds: float = 5.0


@dataclass
class MetadataQueryState:
    schema: str
    table: str
    query_kind: str
    status: str
    row_limit: int
    observed_row_count: int = 0
    retained_row_count: int = 0
    potentially_truncated: bool = False
    complete: bool = False
    malformed: bool = False
    table_association_unambiguous: bool = True
    table_metadata_eligible: bool = False


@dataclass
class MetadataEvidence:
    status: str = "unavailable"
    warnings: list[str] = field(default_factory=list)
    table_metadata: list[dict[str, Any]] = field(default_factory=list)
    indexes: list[dict[str, Any]] = field(default_factory=list)
    column_metadata: list[dict[str, Any]] = field(default_factory=list)
    query_states: list[MetadataQueryState] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.status == "available"


def _split_reference(reference: str, database: Optional[str]) -> tuple[str, str] | None:
    parts = reference.split(".")
    if len(parts) == 1:
        schema, table = database, parts[0]
    elif len(parts) == 2:
        schema, table = parts
    else:
        return None
    if not schema or not _IDENTIFIER.fullmatch(schema) or not _IDENTIFIER.fullmatch(table):
        return None
    return schema, table


def _status_for_exception(exc: Exception) -> str:
    text = str(exc).lower()
    if "permission" in text or "denied" in text or "access" in text:
        return "permission_denied"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "unavailable"


def _warning(status: str) -> str:
    return {
        "permission_denied": "Database metadata permission was denied.",
        "timeout": "Database metadata collection timed out.",
        "budget_exceeded": "Database metadata collection exceeded its configured budget.",
        "invalid_reference": "A referenced table name was invalid or unsupported.",
        "unavailable": "Database metadata is unavailable.",
    }.get(status, "Database metadata is unavailable.")


def _index_sequence_is_valid(details: list[dict[str, Any]]) -> bool:
    sequences = [detail.get("sequence") for detail in details]
    if not all(isinstance(sequence, int) and not isinstance(sequence, bool) for sequence in sequences):
        return False
    if any(sequence < 1 for sequence in sequences):
        return False
    return sequences == list(range(1, len(sequences) + 1))


def collect_metadata(
    table_references: list[str],
    connection: Any = None,
    database: Optional[str] = None,
    config: Optional[MetadataConfig] = None,
    session: Optional[AnalysisSession] = None,
) -> MetadataEvidence:
    """Collect fixed metadata queries for validated parser-identified tables."""
    config = config or MetadataConfig()
    if not table_references:
        return MetadataEvidence(status="unavailable", warnings=[_warning("unavailable")])
    if len(table_references) > config.max_tables:
        return MetadataEvidence(status="budget_exceeded", warnings=[_warning("budget_exceeded")])

    if database is None:
        database = session.selected_database if session is not None else None
        if database is None:
            database = get_client().config.database
        if not database:
            return MetadataEvidence(status="unavailable", warnings=[_warning("unavailable")])

    references = [_split_reference(reference, database)
                  for reference in table_references]
    if any(reference is None for reference in references):
        return MetadataEvidence(status="invalid_reference", warnings=[_warning("invalid_reference")])

    owns_connection = connection is None
    conn = connection

    started = time.monotonic()
    row_count = 0
    table_metadata: list[dict[str, Any]] = []
    raw_indexes: list[dict[str, Any]] = []
    column_metadata: list[dict[str, Any]] = []

    queries = (
        (TABLES_SQL, "tables", True),
        (STATISTICS_SQL, "statistics", True),
        (COLUMNS_SQL, "columns", False),
    )
    query_states: list[MetadataQueryState] = []
    try:
        for schema, table in references:
            for query, kind, uses_sentinel in queries:
                query_state = MetadataQueryState(
                    schema=schema,
                    table=table,
                    query_kind=kind,
                    status="unavailable",
                    row_limit=config.max_metadata_rows,
                    table_association_unambiguous=True,
                )
                query_states.append(query_state)
                elapsed = time.monotonic() - started
                if elapsed >= config.total_timeout_seconds:
                    result = MetadataEvidence(
                        status="timeout", warnings=[_warning("timeout")])
                    result.query_states = query_states
                    query_state.status = "timeout"
                    query_state.complete = False
                    query_state.potentially_truncated = False
                    if owns_connection and conn is not None:
                        conn.close()
                    return result
                if conn is None:
                    if session is None:
                        conn = get_client().connect_readonly(config.query_timeout_seconds)
                    else:
                        conn = get_client().connect_readonly(
                            config.query_timeout_seconds,
                            session=session,
                        )
                    if conn is None:
                        result = MetadataEvidence(status="unavailable", warnings=[
                                                  _warning("unavailable")])
                        query_state.status = "unavailable"
                        query_state.complete = False
                        query_state.potentially_truncated = False
                        result.query_states = query_states
                        return result
                cur = conn.cursor()
                try:
                    query_limit = config.max_metadata_rows + \
                        1 if uses_sentinel else config.max_metadata_rows
                    cur.execute(query, (schema, table, query_limit))
                    fetch_limit = config.max_metadata_rows + \
                        1 if uses_sentinel else config.max_metadata_rows
                    rows = cur.fetchmany(fetch_limit)
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
                observed_row_count = len(rows)
                potentially_truncated = uses_sentinel and observed_row_count == config.max_metadata_rows + 1
                retained_rows = rows[:config.max_metadata_rows] if uses_sentinel else rows
                malformed = any(
                    not isinstance(row, dict)
                    or not _REQUIRED_FIELDS[kind].issubset(row)
                    for row in retained_rows
                )
                query_state.status = "available"
                query_state.observed_row_count = observed_row_count
                query_state.retained_row_count = len(retained_rows)
                query_state.potentially_truncated = potentially_truncated
                query_state.complete = not potentially_truncated and not malformed and kind != "columns"
                query_state.malformed = malformed
                query_state.table_metadata_eligible = (
                    kind == "tables"
                    and query_state.complete
                    and query_state.table_association_unambiguous
                    and len(retained_rows) == 1
                    and isinstance(retained_rows[0].get("TABLE_ROWS"), int)
                    and not isinstance(retained_rows[0].get("TABLE_ROWS"), bool)
                    and retained_rows[0]["TABLE_ROWS"] >= 0
                ) if kind == "tables" and retained_rows and not malformed else False
                row_count += len(retained_rows)
                if row_count > config.max_metadata_rows:
                    result = MetadataEvidence(status="budget_exceeded", warnings=[
                                              _warning("budget_exceeded")])
                    query_state.status = "budget_exceeded"
                    query_state.complete = False
                    query_state.potentially_truncated = False
                    query_state.table_metadata_eligible = False
                    result.query_states = query_states
                    if owns_connection and conn is not None:
                        conn.close()
                    return result
                if malformed:
                    continue
                if kind == "tables":
                    for row in retained_rows:
                        table_metadata.append({
                            "schema": schema,
                            "table": table,
                            "engine": row.get("ENGINE"),
                            "estimated_table_rows": row.get("TABLE_ROWS"),
                            "estimated_table_rows_semantics": "metadata estimate, not exact row count",
                            "data_length_bytes": row.get("DATA_LENGTH"),
                            "index_length_bytes": row.get("INDEX_LENGTH"),
                            "create_options": row.get("CREATE_OPTIONS"),
                            "update_time": row.get("UPDATE_TIME"),
                        })
                elif kind == "statistics":
                    for row in retained_rows:
                        raw_indexes.append(
                            {**row, "_metadata_schema": schema, "_metadata_table": table})
                else:
                    for row in retained_rows:
                        column_metadata.append({
                            "schema": schema,
                            "table": table,
                            "name": row.get("COLUMN_NAME"),
                            "data_type": row.get("DATA_TYPE"),
                            "nullable": row.get("IS_NULLABLE"),
                            "column_key": row.get("COLUMN_KEY"),
                            "ordinal_position": row.get("ORDINAL_POSITION"),
                        })
                if (
                    time.monotonic() - started > config.query_timeout_seconds
                    or time.monotonic() - started > config.total_timeout_seconds
                ):
                    result = MetadataEvidence(
                        status="timeout", warnings=[_warning("timeout")])
                    query_state.status = "timeout"
                    query_state.complete = False
                    query_state.potentially_truncated = False
                    query_state.table_metadata_eligible = False
                    result.query_states = query_states
                    if owns_connection and conn is not None:
                        conn.close()
                    return result
    except Exception as exc:
        status = _status_for_exception(exc)
        result = MetadataEvidence(status=status, warnings=[_warning(status)])
        if query_states:
            query_states[-1].status = status
            query_states[-1].complete = False
            query_states[-1].potentially_truncated = False
            query_states[-1].table_metadata_eligible = False
            result.query_states = query_states
        if owns_connection and conn is not None:
            conn.close()
        return result

    grouped: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in raw_indexes:
        key = (
            row.get("_metadata_schema", database) or database or "",
            row.get("_metadata_table", ""),
            row.get("INDEX_NAME", ""),
        )
        index = grouped.setdefault(key, {
            "schema": key[0],
            "table": key[1],
            "name": row.get("INDEX_NAME"),
            "columns": [],
            "column_details": [],
            "cardinality_estimates": [],
            "unique": row.get("NON_UNIQUE") == 0,
            "index_type": row.get("INDEX_TYPE"),
            "structure_valid": True,
        })
        detail = {
            "name": row.get("COLUMN_NAME"),
            "sequence": row.get("SEQ_IN_INDEX"),
            "prefix_length": str(row["SUB_PART"]) if row.get("SUB_PART") not in (None, "") else None,
            "cardinality_estimate": row.get("CARDINALITY"),
        }
        index["column_details"].append(detail)
        index["columns"].append(detail["name"])
        index["cardinality_estimates"].append(detail["cardinality_estimate"])

    for index in grouped.values():
        index["structure_valid"] = (
            isinstance(index["name"], str)
            and bool(index["name"])
            and all(isinstance(item["name"], str) and bool(item["name"]) for item in index["column_details"])
            and _index_sequence_is_valid(index["column_details"])
        )
        if index["structure_valid"]:
            index["column_details"].sort(key=lambda item: item["sequence"])
        index["columns"] = [item["name"] for item in index["column_details"]]
        index["cardinality_estimates"] = [item["cardinality_estimate"]
                                          for item in index["column_details"]]

    result = MetadataEvidence(
        status="available",
        table_metadata=table_metadata,
        indexes=list(grouped.values()),
        column_metadata=column_metadata,
        query_states=query_states,
    )
    if owns_connection:
        conn.close()
    return result
