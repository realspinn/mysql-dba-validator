"""
SQL parsing and static analysis for the MySQL DBA Validator.

Uses SQLGlot with the MySQL dialect to parse pasted SQL into one or more
statements, classify each statement, and extract structural facts that
the risk engine can evaluate.

This layer does NOT decide whether SQL is safe. It extracts facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import sqlglot
from sqlglot import exp


DIALECT = "mysql"


CATEGORY_MAP = {
    "select": "READ",
    "insert": "WRITE",
    "update": "WRITE",
    "delete": "WRITE",
    "replace": "WRITE",
    "create": "DDL",
    "alter": "DDL",
    "drop": "DDL",
    "truncatetable": "DDL",
    "grant": "DCL",
    "revoke": "DCL",
    "command": "OTHER",
    "call": "PROCEDURE",
    "transaction": "TRANSACTION",
    "commit": "TRANSACTION",
    "rollback": "TRANSACTION",
}


COMMAND_KEYWORDS_TXN = {
    "BEGIN",
    "START",
    "COMMIT",
    "ROLLBACK",
}


COMMAND_KEYWORDS_DCL = {
    "GRANT",
    "REVOKE",
}


@dataclass
class StatementFacts:
    """
    Structured facts extracted from one SQL statement.

    The risk engine consumes this object instead of having to understand
    SQL syntax itself.
    """

    raw_sql: str
    index: int

    statement_type: str = "UNKNOWN"
    category: str = "OTHER"

    tables: list[str] = field(default_factory=list)
    table_bindings: list["TableBinding"] = field(default_factory=list)

    # None means the concept is not applicable to this statement.
    has_where: Optional[bool] = None

    where_looks_trivial: bool = False
    where_predicate_summary: Optional[str] = None
    columns_in_where: list[str] = field(default_factory=list)
    predicate_shape: str = "NO_WHERE"
    predicate_columns: list["PredicateColumn"] = field(default_factory=list)
    predicate_table_association: str = "NOT_APPLICABLE"

    has_limit: bool = False

    # True when the operation is capable of affecting an entire table
    # or making a broad schema/data change.
    is_full_table_ddl: bool = False

    parse_error: Optional[str] = None

    # True when the script only parsed with the permissive fallback, which can drop
    # tokens (for example a trailing SELECT ... INTO): raw_sql may then not be the
    # submitted statement. Set for every statement of that script.
    lossy_parse: bool = False


@dataclass(frozen=True)
class PredicateColumn:
    """A column reference retained from a parsed WHERE expression."""

    name: str
    table: Optional[str] = None
    schema: Optional[str] = None
    resolved_table: Optional[str] = None


@dataclass(frozen=True)
class TableBinding:
    """One AST table occurrence, including its optional alias."""

    schema: Optional[str]
    table: str
    alias: Optional[str] = None


_TRIVIAL_WHERE_SNIPPETS = (
    "1=1",
    "1 = 1",
    "true",
    "'1'='1'",
    '"1"="1"',
)


def _classify(expr: exp.Expression) -> tuple[str, str]:
    """
    Return:
        (statement_type, category)

    SQLGlot's internal AST names are normalized here so the rest of
    DBA Guard does not need to know parser-specific naming quirks.
    """

    key = (expr.key or "").lower()

    # Standard structured statement types.
    if isinstance(expr, exp.Select):
        return "SELECT", "READ"

    if isinstance(expr, exp.Insert):
        return "INSERT", "WRITE"

    if isinstance(expr, exp.Update):
        return "UPDATE", "WRITE"

    if isinstance(expr, exp.Delete):
        return "DELETE", "WRITE"

    if isinstance(expr, exp.Create):
        return "CREATE", "DDL"

    if isinstance(expr, exp.Alter):
        return "ALTER", "DDL"

    if isinstance(expr, exp.Drop):
        return "DROP", "DDL"

    # SQLGlot represents MySQL TRUNCATE TABLE as a TruncateTable
    # expression in some versions.
    #
    # We normalize it to the application's canonical statement type:
    # TRUNCATE.
    if key == "truncatetable":
        return "TRUNCATE", "DDL"

    # Some SQLGlot versions may represent certain statements as Command.
    if isinstance(expr, exp.Command):
        text = str(expr.this or "").upper().strip()

        first_word = text.split()[0] if text else ""

        if first_word == "TRUNCATE":
            return "TRUNCATE", "DDL"

        if first_word in COMMAND_KEYWORDS_DCL:
            return first_word, "DCL"

        if first_word in COMMAND_KEYWORDS_TXN:
            return first_word, "TRANSACTION"

        if first_word == "CALL":
            return "CALL", "PROCEDURE"

        if first_word == "REPLACE":
            return "REPLACE", "WRITE"

        if first_word:
            return first_word, "OTHER"

        return "UNKNOWN", "OTHER"

    # Final fallback.
    return key.upper() or "UNKNOWN", CATEGORY_MAP.get(key, "OTHER")


def _where_is_trivial(where_expr: exp.Expression) -> bool:
    """
    Best-effort detection of predicates that are likely to match
    most or all rows.

    This is intentionally conservative.

    It does NOT replace EXPLAIN or actual row-count analysis.
    """

    sql_text = (
        where_expr.sql(dialect=DIALECT)
        .lower()
        .replace(" ", "")
    )

    for snippet in _TRIVIAL_WHERE_SNIPPETS:
        if snippet.replace(" ", "") in sql_text:
            return True

    # Detect cases such as:
    #
    # WHERE id > 0
    # WHERE id >= 0
    #
    # These can technically be valid predicates but may match almost
    # every row depending on the data.
    for node in where_expr.find_all((exp.GT, exp.GTE)):
        right = node.expression

        if isinstance(right, exp.Literal) and right.is_number:
            try:
                if float(right.this) <= 0:
                    return True
            except (ValueError, TypeError):
                pass

    return False


def _extract_where_columns(where_expr: exp.Expression) -> list[str]:
    """Extract unique column names referenced by a WHERE predicate."""

    columns: list[str] = []

    for column in where_expr.find_all(exp.Column):
        name = column.name

        if name and name not in columns:
            columns.append(name)

    return columns


def _unwrap_parentheses(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _predicate_shape(where_expr: exp.Expression) -> str:
    """Classify only AST-observable WHERE shapes needed by M2."""
    expression = _unwrap_parentheses(where_expr)
    if expression.find(exp.Subquery):
        return "SUBQUERY"
    if expression.find(exp.Or):
        return "OR"
    if expression.find(exp.In):
        return "IN"
    if expression.find(exp.Between):
        return "BETWEEN"
    if expression.find((exp.LT, exp.LTE, exp.GT, exp.GTE)):
        return "RANGE"
    if expression.find(exp.Cast):
        return "CAST"
    if expression.find(exp.Func):
        return "FUNCTION"

    if isinstance(expression, exp.EQ):
        left = _unwrap_parentheses(expression.left)
        right = _unwrap_parentheses(expression.right)
        if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
            return "DIRECT_COLUMN_LITERAL_EQUALITY"
        if isinstance(right, exp.Column) and isinstance(left, exp.Literal):
            return "DIRECT_COLUMN_LITERAL_EQUALITY"
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            return "COLUMN_COMPARISON"
        return "COMPUTED_EXPRESSION"

    return "UNSUPPORTED"


def _table_references(expr: exp.Expression) -> dict[str, str]:
    references: dict[str, str] = {}
    for table in expr.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        resolved = f"{table.db}.{name}" if table.db else name
        references[name] = resolved
        if table.db:
            references[resolved] = resolved
        if table.alias:
            references[table.alias] = resolved
    return references


def _predicate_columns(
    where_expr: exp.Expression,
    facts_tables: list[str],
    expression: exp.Expression,
) -> tuple[list[PredicateColumn], str]:
    references = _table_references(expression)
    columns: list[PredicateColumn] = []
    association = "UNAMBIGUOUS" if len(facts_tables) == 1 else "MULTIPLE_TABLES"

    for column in where_expr.find_all(exp.Column):
        qualifier = column.table or None
        schema = column.db or None
        resolved_table = None

        if len(facts_tables) == 1:
            statement_table = facts_tables[0]
            statement_schema, _, statement_name = statement_table.rpartition(".")
            if qualifier:
                resolved_table = references.get(qualifier)
                if resolved_table is None:
                    association = "UNRESOLVED"
                elif statement_name != resolved_table.rsplit(".", 1)[-1]:
                    association = "UNRESOLVED"
                elif schema and statement_schema and schema != statement_schema:
                    association = "UNRESOLVED"
            elif schema:
                if statement_schema and schema != statement_schema:
                    association = "UNRESOLVED"
                else:
                    resolved_table = statement_table
            else:
                resolved_table = statement_table

        columns.append(
            PredicateColumn(
                name=column.name,
                table=qualifier,
                schema=schema,
                resolved_table=resolved_table,
            )
        )

    if not columns and association == "UNAMBIGUOUS":
        association = "UNRESOLVED"
    return columns, association


def _extract_tables(expr: exp.Expression) -> list[str]:
    """Extract unique table names referenced by a statement."""

    tables: list[str] = []

    for table in expr.find_all(exp.Table):
        name = table.name

        if table.db:
            name = f"{table.db}.{name}"

        if name and name not in tables:
            tables.append(name)

    return tables


def _extract_table_bindings(expr: exp.Expression) -> list[TableBinding]:
    return [
        TableBinding(
            schema=table.db or None,
            table=table.name,
            alias=table.alias or None,
        )
        for table in expr.find_all(exp.Table)
        if table.name
    ]


def parse_sql(raw_sql: str) -> list[StatementFacts]:
    """
    Parse a possibly multi-statement MySQL script into StatementFacts.

    If strict parsing fails, a permissive second attempt is made so
    the validator can still surface useful information instead of
    immediately failing the entire request.
    """

    raw_sql = raw_sql.strip()

    if not raw_sql:
        return []

    lossy_parse = False
    try:
        expressions = sqlglot.parse(
            raw_sql,
            read=DIALECT,
            error_level=sqlglot.ErrorLevel.RAISE,
        )

    except Exception as strict_error:

        try:
            expressions = sqlglot.parse(
                raw_sql,
                read=DIALECT,
                error_level=sqlglot.ErrorLevel.IGNORE,
            )
            lossy_parse = True

        except Exception:
            return [
                StatementFacts(
                    raw_sql=raw_sql,
                    index=0,
                    parse_error=str(strict_error),
                )
            ]

    facts_list: list[StatementFacts] = []

    for index, expr in enumerate(expressions):

        if expr is None:
            continue

        statement_sql = expr.sql(dialect=DIALECT)

        statement_type, category = _classify(expr)

        facts = StatementFacts(
            raw_sql=statement_sql,
            index=index,
            statement_type=statement_type,
            category=category,
            lossy_parse=lossy_parse,
        )

        # ---------------------------------------------------------
        # TABLES
        # ---------------------------------------------------------

        facts.tables = _extract_tables(expr)
        facts.table_bindings = _extract_table_bindings(expr)

        # ---------------------------------------------------------
        # WHERE
        # ---------------------------------------------------------

        where_node = (
            expr.args.get("where")
            if hasattr(expr, "args")
            else None
        )

        if statement_type in {
            "UPDATE",
            "DELETE",
            "SELECT",
            "REPLACE",
        }:

            facts.has_where = where_node is not None

            if where_node is not None:

                where_expr = (
                    where_node.this
                    if isinstance(where_node, exp.Where)
                    else where_node
                )

                facts.where_predicate_summary = (
                    where_expr.sql(dialect=DIALECT)
                )

                facts.where_looks_trivial = _where_is_trivial(
                    where_expr
                )

                facts.columns_in_where = _extract_where_columns(
                    where_expr
                )
                facts.predicate_shape = _predicate_shape(where_expr)
                (
                    facts.predicate_columns,
                    facts.predicate_table_association,
                ) = _predicate_columns(where_expr, facts.tables, expr)
            else:
                facts.predicate_shape = "NO_WHERE"
                facts.predicate_table_association = "NOT_APPLICABLE"

        else:
            facts.has_where = None

        # ---------------------------------------------------------
        # LIMIT
        # ---------------------------------------------------------

        facts.has_limit = (
            expr.args.get("limit") is not None
            if hasattr(expr, "args")
            else False
        )

        # ---------------------------------------------------------
        # DDL IMPACT
        # ---------------------------------------------------------

        if statement_type in {
            "DROP",
            "TRUNCATE",
        }:
            facts.is_full_table_ddl = True

        elif statement_type == "ALTER":

            sql_lower = statement_sql.lower()

            facts.is_full_table_ddl = (
                "drop" in sql_lower
                and "column" in sql_lower
            )

        facts_list.append(facts)

    return facts_list