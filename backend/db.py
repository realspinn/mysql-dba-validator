"""
Database evidence scaffold

Provides a DBConfig and DBClient implementation that lazily uses PyMySQL
when the environment is configured. The client stays read-only by default:
if no environment variables are provided or the driver is missing, no
network calls are made and the API returns explanatory statuses.

To enable in future, set environment variables such as:

  MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
"""
from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, Optional

import sqlglot
from sqlglot import exp


# Bound response and in-memory accumulation to a conservative maximum.
MAX_DISCOVERED_DATABASES = 1000
_DATABASE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def validate_database_identifier(database: Optional[str]) -> bool:
    """Return whether a request database is a bounded MySQL identifier."""
    return (
        isinstance(database, str)
        and 1 <= len(database) <= 64
        and _DATABASE_IDENTIFIER.fullmatch(database) is not None
    )


def validate_readonly_select(sql: str) -> bool:
    """Return True only for one side-effect-free SELECT statement."""
    if not isinstance(sql, str) or not sql.strip():
        return False
    try:
        expressions = sqlglot.parse(
            sql,
            read="mysql",
            error_level=sqlglot.ErrorLevel.IGNORE,
        )
    except Exception:
        return False
    if len(expressions) != 1 or not isinstance(expressions[0], exp.Select):
        return False
    return expressions[0].args.get("into") is None


@dataclass(repr=False)
class DBConfig:
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    database: Optional[str] = None

    def __repr__(self) -> str:
        return (
            "DBConfig(host={!r}, port={!r}, user={!r}, password=<redacted>, "
            "database={!r})"
        ).format(self.host, self.port, self.user, self.database)

    @classmethod
    def from_env(cls) -> "DBConfig":
        host = os.environ.get("MYSQL_HOST", "")
        port = os.environ.get("MYSQL_PORT", "")
        try:
            port_value = int(str(port).strip()) if str(port).strip() else None
        except ValueError:
            port_value = None

        return cls(
            host=host.strip() if host else None,
            port=port_value,
            user=(os.environ.get("MYSQL_USER", "") or "").strip() or None,
            password=(os.environ.get("MYSQL_PASSWORD", "")
                      or "").strip() or None,
            database=(os.environ.get("MYSQL_DATABASE", "")
                      or "").strip() or None,
        )


@dataclass(frozen=True, repr=False)
class ConnectionProfile:
    """Immutable server and credential configuration for a MySQL connection."""

    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None

    def __repr__(self) -> str:
        return (
            "ConnectionProfile(host={!r}, port={!r}, username={!r}, "
            "password=<redacted>)"
        ).format(self.host, self.port, self.username)

    @classmethod
    def from_config(cls, config: DBConfig) -> "ConnectionProfile":
        return cls(
            host=config.host,
            port=config.port,
            username=config.user,
            password=config.password,
        )


@dataclass(frozen=True, repr=False)
class AnalysisSession:
    """Analysis context kept separate from a live, per-operation connection."""

    connection_profile: ConnectionProfile
    selected_database: Optional[str] = None

    def __repr__(self) -> str:
        return (
            "AnalysisSession(connection_profile={!r}, selected_database={!r})"
        ).format(self.connection_profile, self.selected_database)

    def with_database(self, database: Optional[str]) -> "AnalysisSession":
        return AnalysisSession(self.connection_profile, database)

    @classmethod
    def from_config(cls, config: DBConfig) -> "AnalysisSession":
        return cls(ConnectionProfile.from_config(config), config.database)


def probe_connection(session: AnalysisSession, read_timeout: float = 10.0) -> Dict[str, Any]:
    """Test one connection and close it without executing SQL."""
    connection = None
    try:
        import pymysql

        profile = session.connection_profile
        connect_kwargs = {
            "host": profile.host,
            "user": profile.username,
            "password": profile.password,
            "port": profile.port or 3306,
            "connect_timeout": 5,
            "read_timeout": read_timeout,
        }
        if session.selected_database:
            connect_kwargs["database"] = session.selected_database
        connection = pymysql.connect(**connect_kwargs)
        return {"status": "success", "connected": True}
    except Exception:
        return {
            "status": "error",
            "connected": False,
            "error_code": "connection_failed",
            "message": "Unable to connect to the database.",
        }
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


# Stable, browser-safe evidence states. Only these codes ever describe why evidence is
# missing; raw MySQL messages, hosts, usernames, SQL and credentials never leave here.
EVIDENCE_STATUSES = (
    "available",
    "not_configured",
    "unreachable",
    "auth_failed",
    "database_not_found",
    "insufficient_privileges",
    "explain_failed",
    "unsupported_statement_type",
    "collection_failed",
)

_MYSQL_ERRNO_STATUS = {
    1045: "auth_failed",              # access denied (bad login)
    1049: "database_not_found",       # unknown database
    1044: "insufficient_privileges",  # access denied to database
    1142: "insufficient_privileges",  # command denied on table
    1143: "insufficient_privileges",  # column access denied
    1227: "insufficient_privileges",  # specific privilege required
    2003: "unreachable",              # cannot connect
    2005: "unreachable",              # unknown host
    2006: "unreachable",              # server has gone away
    2013: "unreachable",              # lost connection
}

READ_ONLY_SESSION_SQL = "SET SESSION TRANSACTION READ ONLY"


def mysql_error_status(exc: BaseException, default: str) -> str:
    """Map a driver exception to a safe status using only its MySQL error number."""
    args = getattr(exc, "args", ())
    errno = args[0] if args and isinstance(args[0], int) else None
    return _MYSQL_ERRNO_STATUS.get(errno, default)


def open_evidence_connection(
    profile: ConnectionProfile,
    database: str,
    read_timeout: float = 10.0,
) -> tuple[Any, Optional[str]]:
    """Open the one evidence connection for a validation request.

    Uses the credentials supplied with that request and binds the selected database
    at connect time. Before any evidence query the session is set to read-only
    transactions (defense in depth; MySQL account privileges remain the authorization
    boundary). If that cannot be set, the connection is closed and no evidence is
    collected (fail closed).

    Returns (connection, None) on success or (None, status) with a safe status code.
    The caller owns the connection and must close it exactly once.
    """
    try:
        import pymysql
    except Exception:
        return None, "collection_failed"

    try:
        connection = pymysql.connect(
            host=profile.host,
            user=profile.username,
            password=profile.password,
            database=database,
            port=profile.port or 3306,
            connect_timeout=5,
            read_timeout=read_timeout,
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as exc:
        return None, mysql_error_status(exc, "unreachable")

    try:
        cursor = connection.cursor()
        try:
            cursor.execute(READ_ONLY_SESSION_SQL)
        finally:
            try:
                cursor.close()
            except Exception:
                pass
    except Exception:
        try:
            connection.close()
        except Exception:
            pass
        return None, "collection_failed"

    return connection, None


def discover_databases(
    session: AnalysisSession,
    max_databases: int = MAX_DISCOVERED_DATABASES,
) -> Dict[str, Any]:
    """Return visible database names using one bounded SHOW DATABASES query."""
    connection = None
    cursor = None
    try:
        import pymysql

        profile = session.connection_profile
        connection = pymysql.connect(
            host=profile.host,
            user=profile.username,
            password=profile.password,
            port=profile.port or 3306,
            connect_timeout=5,
            read_timeout=10.0,
        )
        cursor = connection.cursor()
        cursor.execute("SHOW DATABASES")
        rows = cursor.fetchmany(max_databases + 1)
        if len(rows) > max_databases:
            database_rows = rows[:max_databases]
            complete = False
            status = "limited"
        else:
            database_rows = rows
            complete = True
            status = "success"

        databases = []
        for row in database_rows:
            if isinstance(row, Mapping):
                databases.append(next(iter(row.values())))
            else:
                databases.append(row[0])
        return {
            "status": status,
            "databases": databases,
            "complete": complete,
        }
    except Exception:
        return {
            "status": "error",
            "connected": False,
            "error_code": "database_discovery_failed",
            "message": "Unable to discover databases.",
        }
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


class DBClient:
    """A minimal DB client that uses pymysql when available.

    Safety:
    - The client will not attempt any network activity unless environment
      variables are provided and the pymysql driver can be imported.
    - execute_readonly enforces SELECT-only and a configurable LIMIT.
    """

    def __init__(self, config: Optional[DBConfig] = None):
        self.config = config or DBConfig.from_env()
        self.session = AnalysisSession.from_config(self.config)
        self._pymysql = None

    @property
    def configured(self) -> bool:
        return bool(
            self.session.connection_profile.host
            and self.session.connection_profile.username
            and self.session.selected_database
        )

    def _connect(
        self,
        read_timeout: float = 10,
        session: Optional[AnalysisSession] = None,
    ):
        """Attempt to lazily import pymysql and open a connection.

        Returns a fresh live connection on success or None on any failure.
        Callers own the returned connection and must close it.
        """
        try:
            import pymysql
        except Exception:
            return None

        self._pymysql = pymysql

        active_session = session or self.session
        if not (
            active_session.connection_profile.host
            and active_session.connection_profile.username
            and active_session.selected_database
        ):
            return None

        try:
            profile = active_session.connection_profile
            conn = pymysql.connect(
                host=profile.host,
                user=profile.username,
                password=profile.password,
                database=active_session.selected_database,
                port=profile.port or 3306,
                connect_timeout=5,
                read_timeout=read_timeout,
                cursorclass=pymysql.cursors.DictCursor,
            )
            return conn
        except Exception:
            return None

    def connect_readonly(
        self,
        read_timeout: float = 10.0,
        session: Optional[AnalysisSession] = None,
    ):
        """Return the configured read-only connection for bounded DB work."""
        return self._connect(read_timeout=read_timeout, session=session)

    def execute_readonly(self, sql: str, max_rows: int = 1000) -> Dict[str, Any]:
        """Execute a read-only SELECT and return rows and columns.

        Returns structured dicts with keys:
        - status: 'ok'|'not_configured'|'unavailable'|'error'
        - columns: list[str]
        - rows: list[dict]
        - row_count: int

        If the driver is not available or connection fails, returns a
        descriptive status instead of raising.
        """
        if not self.configured:
            return {
                "status": "not_configured",
                "message": "No database configured for evidence queries.",
            }

        if not sql or not sql.strip():
            return {"status": "error", "error": "empty_sql"}

        if not validate_readonly_select(sql):
            return {
                "status": "error",
                "error": "only_select_allowed",
                "message": "Only read-only SELECT statements are allowed for evidence queries.",
            }

        conn = self._connect()
        if not conn:
            return {
                "status": "unavailable",
                "message": "DB driver not available or connection failed.",
            }

        if "limit" not in sql.strip().lower():
            sql_to_run = sql.rstrip(";") + f" LIMIT {max_rows};"
        else:
            sql_to_run = sql

        try:
            with conn.cursor() as cur:
                cur.execute(sql_to_run)
                rows = cur.fetchall()
                cols = list(rows[0].keys()) if rows else []
                return {
                    "status": "ok",
                    "columns": cols,
                    "rows": rows,
                    "row_count": len(rows),
                }
        except Exception:
            return {
                "status": "error",
                "error": "query_failed",
                "message": "Database query failed.",
            }
        finally:
            try:
                conn.close()
            except Exception:
                pass


_client: Optional[DBClient] = None


def get_client() -> DBClient:
    global _client
    if _client is None:
        _client = DBClient()
    return _client


class _CursorLease:
    def __init__(self, cursor: Any, connection: Any):
        self._cursor = cursor
        self._connection = connection

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)

    def close(self):
        try:
            return self._cursor.close()
        finally:
            try:
                self._connection.close()
            except Exception:
                pass

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            return self._cursor.__exit__(exc_type, exc, tb)
        finally:
            try:
                self._connection.close()
            except Exception:
                pass


class _ConnectionLease:
    def __init__(self, connection: Any):
        self._connection = connection

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def cursor(self, *args, **kwargs):
        return _CursorLease(self._connection.cursor(*args, **kwargs), self._connection)

    def close(self):
        return self._connection.close()


def get_connection(session: Optional[AnalysisSession] = None):
    """Return a live MySQL connection when the environment is configured.

    This is intentionally safe and read-only in practice: the evidence layer
    only issues EXPLAIN queries and never executes user-submitted DML.
    """
    if session is None:
        connection = get_client().connect_readonly()
    else:
        connection = get_client().connect_readonly(session=session)
    return _ConnectionLease(connection) if connection is not None else None
