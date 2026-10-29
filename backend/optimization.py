"""Optimization advice for an analysed SELECT: advisory only, never part of the risk result.

The advice is computed from the parsed statement after the score, confidence and evidence
are final, and is returned beside them; nothing here reads or changes them. Each
recommendation is an observation from the SQL alone ("basis": "sql"): no query is run, no
plan is requested, nothing is measured and no SQL is rewritten. It says what could be
checked, never that the statement is slow or that a change makes it faster.

The rules follow the MySQL 8.4 Reference Manual (the same text is in the 8.0 manual):
- How MySQL Uses Indexes: a query that uses only columns included in some index can read
  them from the index without the data rows (a covering index); with SELECT * that needs
  one index that holds every column.
- Comparison of B-Tree and Hash Indexes: LIKE can use an index only when the pattern is a
  constant that does not start with a wildcard.
- Range Optimization: range access compares a key part with a constant. Optimizer Use of
  Generated Column Indexes: an expression matches an index only when the index is defined
  on the identical expression.
- Mathematical Functions (RAND() is not a constant for the optimizer) and LIMIT Query
  Optimization (with a filesort, every row matching the query without LIMIT is selected
  and sorted before LIMIT applies).
"""

from __future__ import annotations

from typing import Optional

import sqlglot
from sqlglot import exp

from .parser import ANALYSED, DIALECT, StatementFacts


SQL_BASIS = "sql"  # an observation from the submitted SQL; "plan" and "measured" are not produced

VERIFY_WITH_EXPLAIN = (
    "On a non-production copy, compare EXPLAIN for the current and the changed query (type, "
    "key and rows are optimizer estimates) and confirm the changed query returns the rows "
    "the ticket needs; only timing both on representative data shows a real difference."
)

COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NullSafeEQ)
CALCULATIONS = (exp.Func, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.IntDiv)


def advise(facts: StatementFacts) -> dict:
    """The "optimization" field of one statement result."""
    if facts.analysis_status != ANALYSED:
        return _not_eligible("The statement was not analysed, so it was not checked for optimization.")
    if facts.statement_type != "SELECT":
        return _not_eligible("Optimization advice covers single SELECT statements only.")
    if facts.locking_read:
        return _not_eligible("A locking read (FOR UPDATE, FOR SHARE) is not checked for optimization.")
    try:
        # raw_sql of an analysed statement is the parser's own regeneration: it reads back.
        select = sqlglot.parse_one(facts.raw_sql, read=DIALECT)
    except Exception:
        return {"status": "unavailable", "recommendations": []}
    if not isinstance(select, exp.Select):
        return _not_eligible("Optimization advice covers single SELECT statements only.")

    recommendations = _select_star(select)
    # Index advice names a column of a real table only when the query reads one base table
    # (no join, subquery, CTE or derived table): otherwise the column's table is not known.
    if _reads_one_base_table(select) and select.args.get("where") is not None:
        where = select.args["where"]
        recommendations += _leading_wildcards(where) + _calculated_columns(where)
    recommendations += _order_by_rand(select)
    return {"status": "advised" if recommendations else "none_found",
            "recommendations": recommendations}


def _not_eligible(reason: str) -> dict:
    return {"status": "not_eligible", "reason": reason, "recommendations": []}


def _source(select: exp.Select) -> Optional[exp.From]:
    return select.args.get("from") or select.args.get("from_")


def _is_dual(node: exp.Expression) -> bool:
    """FROM DUAL names no table: there is one row. The regenerated SQL quotes it (`DUAL`), so
    a real table named dual, which needs quoting as a reserved word, is treated the same."""
    return (isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier)
            and not node.args.get("db") and node.name.upper() == "DUAL")


def _is_base_table(node: exp.Expression) -> bool:
    """A named table: not DUAL and not a table function such as JSON_TABLE, whose columns
    are declared in the query and have no index."""
    return isinstance(node, exp.Table) and isinstance(node.this, exp.Identifier) and not _is_dual(node)


def _reads_one_base_table(select: exp.Select) -> bool:
    source = _source(select)
    return (source is not None and _is_base_table(source.this)
            and not select.args.get("joins") and not select.args.get("with")
            and sum(1 for _ in select.find_all(exp.Select)) == 1)


def _reads_only_base_tables(select: exp.Select) -> bool:
    """FROM and every JOIN name a table (no derived table, subquery source, table function or CTE)."""
    source = _source(select)
    return (source is not None and not (select.args.get("with") or select.args.get("with_"))
            and all(_is_base_table(item.this) for item in [source, *(select.args.get("joins") or [])]))


def _select_star(select: exp.Select) -> list[dict]:
    # Only the outer SELECT list: COUNT(*) is not a column list, and * inside EXISTS or a
    # derived table selects no column for the result. A derived table or CTE may itself
    # name only some columns, so * is reported only over base tables.
    if not _reads_only_base_tables(select) or not any(
            isinstance(item, exp.Star) or (isinstance(item, exp.Column) and isinstance(item.this, exp.Star))
            for item in select.expressions):
        return []
    return [{
        "code": "select_star",
        "basis": SQL_BASIS,
        "title": "SELECT * returns every column",
        "observation": ("The SELECT list uses * (or table.*), so every column of each table it "
                        "covers is returned."),
        "action": ("If the ticket needs only some columns, name them in the SELECT list instead "
                   "of *."),
        "reasoning": ("If a query uses from a table only columns that are included in some index, "
                      "MySQL can read the values from the index tree without consulting the data "
                      "rows (a covering index; MySQL 8.4 manual: How MySQL Uses Indexes). With * "
                      "that is possible only when one index holds every column, and every row "
                      "returned carries every column, including any large TEXT, BLOB or JSON value."),
        "caveats": [
            "Naming columns does not change which rows match; any difference depends on how "
            "wide the rows are, how many are returned and which indexes exist.",
            "If the ticket genuinely needs every column, * is a reasonable choice.",
            "This advice does not check the table's columns or index definitions.",
        ],
        "verify": ("Compare EXPLAIN for the current and the changed query on a non-production "
                   "copy ('Using index' in Extra means the values come from the index alone), "
                   "and confirm the named columns are the ones the ticket needs; only timing both "
                   "on representative data shows a real difference."),
    }]


def _names(columns: list[exp.Column]) -> str:
    return ", ".join(f"`{name}`" for name in sorted({column.name for column in columns}))


def _unwrap(node: exp.Expression) -> exp.Expression:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _leading_wildcards(where: exp.Where) -> list[dict]:
    columns = []
    for like in where.find_all(exp.Like):
        column, pattern = _unwrap(like.this), like.expression
        # Only a positive LIKE of a bare column with a plain string pattern. NOT LIKE (sqlglot
        # sets negate) and NOT (... LIKE ...) are not narrowed by an index whatever the
        # pattern; with ESCAPE the first character may not be a wildcard.
        if (isinstance(column, exp.Column) and isinstance(pattern, exp.Literal) and pattern.is_string
                and not like.args.get("negate") and not like.args.get("escape")
                and not isinstance(like.parent, exp.Escape)
                and like.find_ancestor(exp.Not) is None
                and pattern.this[:1] in ("%", "_") and pattern.this.strip("%_")):
            columns.append(column)
    if not columns:
        return []
    return [{
        "code": "leading_wildcard_like",
        "basis": SQL_BASIS,
        "title": "LIKE pattern starts with a wildcard",
        "observation": f"A LIKE pattern for {_names(columns)} starts with a wildcard (% or _).",
        "action": ("If the search can be anchored at the start of the value, a pattern that "
                   "starts with literal text (for example 'abc%') can use an index on the column. "
                   "If it must match anywhere in the value, consider whether a FULLTEXT index "
                   "with MATCH ... AGAINST meets the requirement."),
        "reasoning": ("MySQL can use a B-tree index for LIKE only when the pattern is a constant "
                      "that does not start with a wildcard (MySQL 8.4 manual: Comparison of B-Tree "
                      "and Hash Indexes), so this condition cannot narrow the rows through an index."),
        "caveats": [
            "Anchoring the pattern changes the result: 'abc%' does not find values that contain "
            "abc later in the text.",
            "FULLTEXT search matches words, not substrings, and ignores stopwords and short words, "
            "so its results differ from LIKE.",
            "On a small table, or when other conditions already narrow the rows through an index, "
            "the cost may be low.",
        ],
        "verify": VERIFY_WITH_EXPLAIN,
    }]


def _calculated_columns(where: exp.Where) -> list[dict]:
    columns = []
    for comparison in where.find_all(*COMPARISONS, exp.Between, exp.In, exp.Like):
        if isinstance(comparison, COMPARISONS):
            pairs = [(comparison.this, [comparison.expression]),
                     (comparison.expression, [comparison.this])]
        else:  # BETWEEN low AND high, IN (...), LIKE pattern: the values are every other argument
            pairs = [(comparison.this,
                      [value for value in comparison.iter_expressions() if value is not comparison.this])]
        for side, others in pairs:
            calculated = _calculated_columns_in(side)
            # Compared with a constant: the other side reads no column and no subquery.
            if calculated and all(other.find(exp.Column) is None and other.find(exp.Select) is None
                                  for other in others):
                columns += calculated
    if not columns:
        return []
    return [{
        "code": "column_in_expression",
        "basis": SQL_BASIS,
        "title": "Filtered column inside a function or calculation",
        "observation": (f"The WHERE clause compares {_names(columns)} inside a function or "
                        "calculation, not as the bare column."),
        "action": ("Where possible, write the condition so the bare column is compared with a "
                   "constant (for example a date range instead of YEAR(column) = 2024). "
                   "Otherwise, an index on the same expression (a functional key part or an "
                   "indexed generated column) can be considered."),
        "reasoning": ("MySQL's range access uses an index when a key part is compared with a "
                      "constant (MySQL 8.4 manual: Range Optimization). An index on the plain "
                      "column cannot narrow the rows by an expression on it; only an index defined "
                      "on the identical expression can (Optimizer Use of Generated Column Indexes)."),
        "caveats": [
            "A rewritten condition must return exactly the same rows: a date range must cover "
            "the whole last day for DATETIME values, and replacing LOWER(column) = 'x' with "
            "column = 'x' depends on the column's collation.",
            "An index on the matching expression may already exist; this advice does not check "
            "index definitions.",
            "Every added index uses storage and slows inserts, updates and deletes on the table.",
        ],
        "verify": VERIFY_WITH_EXPLAIN,
    }]


def _calculated_columns_in(side: Optional[exp.Expression]) -> list[exp.Column]:
    """Every column read inside a function or calculation on this side (CONCAT(a, b) reads
    both a and b); none when this side is not a calculation."""
    if side is None:
        return []
    side = _unwrap(side)
    if not isinstance(side, CALCULATIONS) or side.find(exp.Select) is not None:
        return []
    # MATCH ... AGAINST is a full-text search: comparing the bare column does not express it,
    # and a FULLTEXT index cannot have functional key parts (MySQL 8.4 manual: CREATE INDEX),
    # so its columns are left out, also inside a larger calculation.
    return [column for column in side.find_all(exp.Column)
            if column.find_ancestor(exp.MatchAgainst) is None]


def _order_by_rand(select: exp.Select) -> list[dict]:
    order = select.args.get("order")
    # Without FROM, or with FROM DUAL alone, there is a single row and nothing to sort.
    source = _source(select)
    if (order is None or source is None or (_is_dual(source.this) and not select.args.get("joins"))
            or not any(isinstance(_unwrap(term.this), exp.Rand) for term in order.expressions)):
        return []
    return [{
        "code": "order_by_rand",
        "basis": SQL_BASIS,
        "title": "ORDER BY RAND()",
        "observation": "The result is ordered by RAND().",
        "action": ("For a small set of matching rows this is a reasonable way to pick rows at "
                   "random. If many rows can match, consider a method that does not sort all of "
                   "them, for example choosing random key values first."),
        "reasoning": ("For the optimizer RAND() is not a constant and cannot be used for index "
                      "optimizations (MySQL 8.4 manual: Mathematical Functions), so no index "
                      "returns rows in this order. When a filesort is needed, all rows that match "
                      "the query without the LIMIT clause are selected, and most or all of them "
                      "are sorted, before LIMIT applies (LIMIT Query Optimization)."),
        "caveats": [
            "The cost depends on how many rows match the WHERE clause, not on the table size alone.",
            "Choosing random key values is not uniformly random when the key values have gaps.",
        ],
        "verify": ("Check EXPLAIN for 'Using filesort' and the estimated rows, and time both "
                   "approaches on a non-production copy with representative data."),
    }]
