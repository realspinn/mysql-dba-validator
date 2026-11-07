# SELECT coverage audit

An audit of how the validator handles each kind of MySQL 8.4 `SELECT`. It covers
what the parser recognises, how the statement is classified, which tables and
predicates it reads, and whether database evidence may be requested.

Successful parsing is not proof of a full assessment. SQLGlot can accept text
MySQL rejects, and a statement that parses may still have parts the risk rules
do not interpret. The corpus is in
[tests/test_select_coverage.py](../tests/test_select_coverage.py). Each row pins
the statement type, analysis status, tables and locking flag listed here. It
also pins what the plan collector does, with every gate applied: the plan is
either sent, or refused with a stated reason and nothing is sent.

Three outcomes:

- **Supported:** classified as `SELECT`, with its tables and `WHERE` predicate
  read. A plain `EXPLAIN` may be requested, under the rules in
  [ARCHITECTURE.md](../ARCHITECTURE.md#what-may-be-sent-to-a-database).
- **Partially assessed:** read and scored, but some evidence is deliberately
  not requested or some structure is not interpreted. The result says what is
  missing.
- **Not analysed:** refused as a whole, with a "Not analysed" or "Could not be
  read" result. It is never reported as low risk.

## Supported

| Form | What is established |
| --- | --- |
| Simple `SELECT` with or without `WHERE` | Table, predicate shape (for example equality on a column) |
| Joins (inner, outer, `STRAIGHT_JOIN`), index hints | Every joined table; the predicate is marked as spanning several tables |
| Subqueries: nested, correlated, `IN`, `EXISTS`, derived tables, `LATERAL` | Tables inside the subqueries. The predicate shape is `SUBQUERY` or `FUNCTION`. A filter on a derived table's column is marked unresolved, not credited to a base table |
| Common table expressions, including `WITH RECURSIVE` without a column list | The base tables inside the CTE; the CTE name is not treated as a table |
| Aggregates, `GROUP BY`, `HAVING`, window functions, `ORDER BY`, `LIMIT`, `SQL_CALC_FOUND_ROWS` | Tables and `LIMIT` |
| Schema-qualified names, including `app.DUAL` | `schema.table` |
| `FROM DUAL` | No table, the same as `SELECT 1`. With no table there is nothing to plan, so no plan is requested |

## Partially assessed

| Form | What is not established, and why |
| --- | --- |
| Locking reads: `FOR UPDATE`, `FOR SHARE`, `LOCK IN SHARE MODE`, `SKIP LOCKED` | Flagged as a locking read (finding, 20 MEDIUM). No plan: the last check before sending refuses any locking clause |
| Locking read without `WHERE` (`SELECT * FROM t FOR SHARE`) | As above, with the same score as a single-row locking read. Row scope is not established. The score does not distinguish locking every row from locking one row; that would be a scoring change, out of this audit's scope |
| `UNION`, `INTERSECT`, `EXCEPT` | Classified as set operations, with the tables of every branch. The branches' `WHERE` clauses are not analysed and no plan is requested: plans are requested only for a single plain `SELECT` |
| Recursive CTE with a column list (`WITH RECURSIVE r (n) AS ...`) | No plan: the column list `r (n)` looks like a call to an unreviewed function, so the plan request is refused. This is conservative |
| `JSON_TABLE(...)` | Not a table, which is correct. No plan: `COLUMNS (` is read as an unreviewed call, also conservative |
| `PARTITION (p1)` selection | Table read. No plan: `PARTITION (` is read as an unreviewed call |
| A stored function call (`my_func(id)`), `SLEEP()` | Finding "calls a function whose effects were not reviewed" (20 MEDIUM). No plan, because `EXPLAIN` can evaluate the function |
| `DUAL` next to a table (`FROM users, DUAL`), or a quoted `` `DUAL` `` | Tables read: `users`; a quoted `` `DUAL` `` names a real table and is reported as one. No plan: the text sent would name `` `DUAL` ``, a different table from the one written, so any unqualified DUAL is refused |

## Not analysed

| Form | Result |
| --- | --- |
| `SELECT ... INTO @var` | Not analysed: the parser would read the target as a new table, so the statement is refused with that reason |
| `SELECT ... INTO OUTFILE` / `DUMPFILE` | "Could not be read": SQLGlot rejects the syntax. The message is generic rather than specific to `INTO` |
| `TABLE t`, `VALUES ROW(...)` | Not analysed: valid MySQL statements that the validator does not analyse |

## Defect fixed in this audit

`FROM DUAL` was listed as a table called DUAL. With a database connected,
validation then:

- asked `information_schema` for a table named DUAL
- reported it as not found
- capped confidence at LIMITED

The MySQL 8.4 manual describes DUAL as "a dummy table name in situations where
no tables are referenced". An unqualified, unquoted DUAL is no longer treated as
a table. Before and after the fix, validation output was compared across 1,071
statements collected from the test files:

- Database-connected results, local and remote (1,422 each): identical.
- Static results changed only for the four DUAL statements:
  - their tables, filter/index state and scope dimension changed
  - score and risk level did not change
  - DUAL-only statements now get the same scope wording as `SELECT 1`

## Noted, not changed

- A `SELECT` without tables (`SELECT 1`, and now `FROM DUAL`) shows the scope
  dimension "Target table could not be determined from the SQL." (UNKNOWN). It
  is accurate that no table was found, but it reads as if one was expected.
  Rewording it would change the result text of every table-less statement, so
  it is left for a separate decision.
- `SELECT ... INTO OUTFILE` gets the generic "could not be read" result. A
  specific message, like the one for `INTO @var`, would be clearer.
- The plan request is refused for recursive CTE column lists, `JSON_TABLE` and
  `PARTITION (...)`, because the text scan reads them as function calls. It is
  also refused for `FROM users, DUAL`. This
  is safe, since no plan is requested, but those statements get less evidence
  than they could. Allowing them would change the reviewed call list, which
  belongs in its own reviewed change.
