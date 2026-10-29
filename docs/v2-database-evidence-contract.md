# Database Evidence Contract — V2 revision

Status: **implemented in the main working tree, not released.** Drafted and
implemented 2026-10-21 on `main` at `aaee4e8` (the v0.4.1 release commit
`42f9244` plus a documentation commit). v0.4.1 and its release notes describe
the earlier behaviour; nothing here was part of v0.4.1's verification.

This revises the evidence contract that shipped in v0.4.0 (commit `2675cd5`).
That contract made evidence *availability* explicit. This revision makes
evidence *meaning* explicit: what each fact proves, what it does not, and which
facts may change risk or confidence.

## Contents

1. [Purpose, product decisions and non-goals](#1-purpose-product-decisions-and-non-goals)
2. [Architecture and evidence flow](#2-architecture-and-evidence-flow)
3. [Findings and their resolution](#3-findings-and-their-resolution)
4. [Evidence taxonomy, proof and non-proof](#4-evidence-taxonomy-proof-and-non-proof)
5. [Availability, completeness and failure semantics](#5-availability-completeness-and-failure-semantics)
6. [Evidence truth table](#6-evidence-truth-table)
7. [WHERE-clause specification](#7-where-clause-specification)
8. [Row scope and risk contract](#8-row-scope-and-risk-contract)
9. [Confidence contract](#9-confidence-contract)
10. [API and frontend contract](#10-api-and-frontend-contract)
11. [Security and operational boundaries](#11-security-and-operational-boundaries)
12. [Row-impact preflight (deferred)](#12-row-impact-preflight-deferred)
13. [MySQL version policy](#13-mysql-version-policy)
14. [Test matrix and results](#14-test-matrix-and-results)
15. [Implementation traceability](#15-implementation-traceability)
16. [Decision register](#16-decision-register)
17. [Open questions and known limitations](#17-open-questions-and-known-limitations)
18. [Acceptance criteria](#18-acceptance-criteria)
19. [Sources](#19-sources)

---

## 1. Purpose, product decisions and non-goals

The validator answers three separate questions and never lets one stand in for
another:

1. What does the SQL text alone tell us? (static facts)
2. What did MySQL actually tell us? (database evidence)
3. How well do those facts support the assessment? (confidence)

**Risk** is the potential impact of the operation if it runs as written.
**Confidence** is how well the available evidence supports the assessment.
They are independent: `DROP TABLE` is CRITICAL with HIGH confidence; an
`UPDATE … WHERE tenant_id = 3` can look moderate and deserve LIMITED confidence
because its row scope is unknown. HIGH confidence never means safe.

**Product decisions (requirements, set by the product owner):**

| # | Decision | Where implemented |
| --- | --- | --- |
| Q1 | Risk and confidence are independent. HIGH confidence only when the statement is understood, the relevant analysis is complete, and evidence supports it. A known full-table write may be HIGH confidence and CRITICAL risk. Missing, partial, ambiguous or failed evidence never raises confidence. A `WHERE` clause never reduces risk or establishes narrow scope by itself. A verified key supports narrow scope only for the real target table and its real key | Sections 8, 9 |
| Q2 | MySQL 8.4 LTS is the primary supported version; 8.0 is a compatibility target only where tested and documented | Section 13 |
| Q3 | Least privilege: never grant `UPDATE`/`DELETE` to obtain plans; never execute submitted SQL; never `EXPLAIN ANALYZE`; keep TLS and target authorization; mark evidence unavailable when the read-only boundary forbids it; add a last-moment check on the exact SQL sent | Section 11 |
| Q4 | No automatic row-count preflight and no general rewrite of writes into `SELECT COUNT` queries; manual guidance only | Section 12 |
| Q5 | `v2_assessment` must not present placeholder scores as results | Section 10.3 |
| Q6 | Execution plans: removed for every statement on 2026-10-21, restored for `SELECT` on 2026-10-22. An eligible `SELECT` reaches MySQL only as plain `EXPLAIN`; it is never executed | Section 1.1 |

**Non-goals:** automatic preflight; execution of submitted SQL; new evidence
collectors; new scores, panels or frontend frameworks; any weakening of
security boundaries.

### 1.1 What reaches MySQL (owner decision, 2026-10-22)

On the local and connector validate paths the evidence connection receives the
read-only session statements, the fixed `information_schema` metadata queries,
and, for each eligible `SELECT` only, one plain `EXPLAIN <SELECT>` followed by
`SELECT DATABASE()`. The submitted statement is never executed, no result rows
are fetched, and `EXPLAIN ANALYZE` is never used. `UPDATE`/`DELETE` and every
other statement get no plan request. A plan returned in a non-tabular format
(`explain_format` TREE or JSON, MySQL 8.0.32+) is reported as not collected
(`explain_format_unsupported`) rather than read wrongly. A plan is optimizer
evidence, not proof of runtime, rows changed or safety, and it never lowers a
score or raises confidence. `tests/test_no_submitted_sql.py` checks this with
fake connections on both paths; no live server has been used.

---

## 2. Architecture and evidence flow

```text
POST /api/validate (local)                      POST /company/targets/{id}/validate (connector)
  backend/main.py  validate()                     connector/server.py  validate_company_target_sql_endpoint()
        │                                                │
        ├─ parse_sql()             backend/parser.py     ├─ same
        ├─ analyze_batch()         backend/analyzer.py   ├─ same
        ├─ score_statement()       backend/risk_engine.py├─ same (static score, static confidence)
        ├─ open_evidence_connection()                     ├─ connect_company_target_mysql()
        │    └─ enforce_read_only_session()  backend/db.py│    └─ require_read_only_session()  (same check)
        ├─ per statement:                                ├─ per statement:
        │    collect_statement_evidence()  (SELECT only) │    collect_statement_evidence()
        │    collect_metadata()                          │    collect_metadata() (selected database only)
        └─ build_statement_result()  main.py  ◄──────────┴─ same function
               ├─ build_evidence_state()        evidence_state.py
               ├─ build_row_scope()             row_scope.py       (static + metadata)
               ├─ apply_scope_adjustment()      main.py            (−10, verified key only, never below MEDIUM)
               ├─ apply_evidence_adjustments()  evidence_scoring.py (+10/+10, one-row plans only)
               ├─ apply_m1_adjustment() / apply_m2_adjustment()      (+5 each)
               └─ final_confidence()            main.py            (raise only by verified scope; caps)
        batch_result()  main.py   overall_score = max, overall_confidence = min
```

Both paths share `build_statement_result()` and `batch_result()`, so equal
evidence yields equal results; `tests/test_connector_remote_evidence.py`
asserts byte-equal responses for nine scenarios.

**What may reach MySQL** (exact prefixes, asserted by
`tests/test_read_only_boundary.py`):

| Query | Purpose |
| --- | --- |
| `SET SESSION TRANSACTION READ ONLY` | Make the session read-only |
| `SELECT @@SESSION.transaction_read_only AS transaction_read_only` | Confirm it (must be 1) |
| `EXPLAIN <one plain SELECT>` | Plan of an eligible `SELECT` |
| Parameterised `information_schema.TABLES / STATISTICS / COLUMNS`, `LIMIT`ed | Table metadata |
| `SELECT DATABASE()` | Database name after a plan |
| `SHOW DATABASES` (capped) | Discovery (separate route) |

Timeouts are client-side: local connect 5 s / read 10 s; company 3 s; metadata
2 s per query, 5 s total. No server-side `MAX_EXECUTION_TIME` is set (it would
apply only to read-only `SELECT`s; see section 12).

---

## 3. Findings and their resolution

Reproduced against the code at `aaee4e8` with the real scorer and API
(`TestClient`); EXPLAIN inputs for F2 were synthetic.

| # | Finding (v0.4.1) | Resolution (main) |
| --- | --- | --- |
| F1 | "Key-scoped" heuristic: any `=` in the predicate plus a column named `id`/`*_id` gave −5 and skipped the MEDIUM floor. `WHERE id >= 5`, `WHERE customer_id = 42`, `WHERE order_id = 5 OR 1`, `WHERE tenant_id = 3 AND created_at < NOW()` and a key on a joined table were all LOW, with the reason "appears to target a specific key value" | **Fixed.** Heuristic and text removed from `risk_engine.py` and `analyzer.py`; every non-trivial `WHERE` write gets +5 and the floor; only a metadata-verified unique key credits narrow scope (8.2) |
| F2 | `ref`/`eq_ref`/`const` access lowered a write's score by 3 (`ref` is non-unique) | **Fixed.** Removed; no plan lowers a score |
| F3 | Only EXPLAIN's first row was read | **Fixed.** Plan fields are interpreted only for a one-row plan; multi-row plans are `interpreted: false`, shown, not scored |
| F4 | A lossy parse got normal (even HIGH) confidence with no warning; `SELECT * INTO @a, @b FROM users` was analysed as `CREATE TABLE @a AS SELECT *, @b` with HIGH confidence | **Fixed.** `analysis_text_differs` flag, warning, confidence LOW |
| F5 | Aliases and CTE names reported as tables (`DELETE o FROM orders o …` → `o: not found`) | **Fixed** in `parser._physical_tables` |
| F6 | "Database evidence was collected." shown for metadata-only results | **Fixed.** Headline names what was collected |
| F7 | `v2_assessment` always `overall_score: 0`, `risk_level: "UNKNOWN"` | **Fixed** (10.3) |
| F8 | `IS [NOT] NULL` predicates not called out | **Fixed.** Reason added (no points) |
| F9 | Key-scope and confidence rules duplicated in two modules | **Fixed.** One `static_confidence`; analyzer imports it |
| F10 | No documented MySQL version | **Fixed** (section 13) |
| F11 | Suggested checks interpolated the predicate into `SELECT COUNT(*) FROM <table> WHERE …` and `EXPLAIN UPDATE …` | **Fixed.** Manual guidance only (section 12) |
| F12 | Company connections had no read-only session and requested `EXPLAIN UPDATE/DELETE`, needing a write-capable account; `EXPLAIN SELECT` on a writable session can run a data-modifying stored function (Percona, 8.0.21 and earlier) | **Fixed.** All evidence sessions confirmed read-only; write plans never requested; plans only for calls on the reviewed built-in list (section 11, review C1) |
| F13 | `UPDATE users SET WHERE` crashed `/api/validate` (pre-existing in v0.4.1) | **Fixed.** Unreadable `WHERE` is "present, unknown"; lossy parse → LOW |
| F14 | The local session was set read-only but never read back | **Fixed.** Read-back required, fail closed |
| F15 | `SELECT /*+ SET_VAR(transaction_read_only = OFF) */ …` was sent as an `EXPLAIN` | **Fixed.** Function-like hint content makes the statement not eligible; nothing is sent |


### 3.1 Adversarial review before commit

An independent review of this revision (before commit) found the defects below.
"Fixed" means changed in code and covered by `tests/test_review_remediation.py`
(fake servers and the installed sqlglot only; no live server).

| # | Review finding | Resolution |
| --- | --- | --- |
| C1 | The plan check refused only functions sqlglot could not name; 595 of 628 sqlglot-known names passed, including `SPLIT`, `LEVENSHTEIN`, `STRUCT`, `MAP`, `LIST`, `DATETIME`, which MySQL resolves to a loadable or stored function. SECURITY.md claimed "no call to a stored function" | **Fixed.** Reviewed allowlist of MySQL 8.4 built-ins and reserved syntax words, checked on tokens of the submitted and the exact outgoing text; quoted and qualified calls refused; rewriting (`ARRAY_AGG` → `GROUP_CONCAT`) cannot approve a call. Docs state only what the check does (section 11) |
| H1 | `WHERE 1`, `WHERE TRUE`, `WHERE id = 5 OR 1` scored MEDIUM 45 while no `WHERE` scored CRITICAL | **Fixed.** Structural always-true detection; such writes are `all_rows`, CRITICAL, `HIGH` confidence (7, 8) |
| H2 | `EXPLAIN ANALYZE DELETE …` and `DESCRIBE DELETE …` scored LOW with HIGH confidence | **Fixed.** `EXPLAIN ANALYZE` is scored as the statement it runs; unreadable target HIGH / LOW confidence; plain `EXPLAIN` of a change is MEDIUM with a finding (8.3) |
| H3 | A verified key set Data Risk LOW and nothing mentioned triggers or foreign-key actions | **Fixed** as documentation of a limit: `scope.indirect_effects: "not_checked"`, the scope reason and page say so, Data Risk keeps its level. Owner decision after the review: a verified one-row `UPDATE`/`DELETE` is never below MEDIUM overall (8.3) |
| M1 | Read-only state confirmed only at connect | **Fixed.** Read back on the same cursor before each plan request; on failure nothing is sent, the connection is closed, and the rest of the batch gets no evidence |
| M2 | The partly-read `SELECT` guard lived in the callers | **Fixed.** The collector checks the submitted statement's facts itself (`source_facts`) |
| M3 | Broad-`WHERE` detection matched the text "true" (`'untrue'`) | **Fixed.** AST only; an `AND` with other terms is not "broad" |
| M4 | Boundary tests accepted any text starting `EXPLAIN SELECT` | **Fixed.** Each sent `EXPLAIN` must pass the final check and the function list |
| M5 | `sqlglot>=23` allowed untested versions | **Narrowed** to `>=30.17.0,<31` (tested on 30.17.0 and on the release pin 30.21.0) |
| M6 | `v2_assessment` said Data Risk LOW and "affected-row count unknown" together | **Fixed.** Finding restated as "Direct match bounded; total impact not established" |
| L1 | `DELETE … PARTITION (p0)` was "every row of t" | **Fixed.** Partition selection is not `all_rows` |
| L2 | Collector could open and leak a connection for writes; metadata fallback connection was not confirmed read-only | **Fixed.** No connection for writes; owned connection closed; fallback confirmed or closed |
| L3 | Stale docstrings | **Fixed** |
| L4 | Page asserted unverified MySQL 8.4 behaviour about write plans | **Fixed.** States the validator's policy instead |
| L5 | `/api/health` runs `SELECT 1` on a `.env`-configured connection without the read-only session | **Unchanged, documented.** Fixed query, pre-existing, not evidence |

---

## 4. Evidence taxonomy, proof and non-proof

### A. Static SQL evidence (parser, no database)

| Fact | Proves | Does not prove |
| --- | --- | --- |
| Statement type | The kind of operation in the analysed text | That the analysed text is the submitted text (`analysis_text_differs`) |
| Physical tables | Names the text refers to (aliases and CTE names excluded) | Existence, schema, or which are written |
| No `WHERE`, or an always-true one, on a single-table write without `LIMIT` or partition selection | Every row of the target is matched (`all_rows`) | How many rows; triggers or cascades |
| `WHERE` present | The text restricts rows somehow | Narrowness; that it excludes anything |
| Predicate shape (`=`, range, `IN`, `OR`, subquery, `IS [NOT] NULL`) | The syntax | Selectivity |
| `column = literal` top-level `AND` terms | Which columns are fixed to which literals | That those columns form a key |
| Column named `id`/`*_id` | Nothing | Uniqueness, existence, count |
| `LIMIT n` (single table) | MySQL stops after `n` rows *matched* | Which rows; that `n` is small |
| Lossy parse / parse error | The analysed text may differ / could not be analysed | Anything about the dropped text |

### B. Table metadata (`information_schema`)

| Fact | Proves | Does not prove |
| --- | --- | --- |
| `found: true` | `TABLES` returned the table in the schema looked up | Row count, predicate selectivity, write privilege |
| `found: false` | No row for that name in that schema at lookup time | That the SQL is invalid (temporary tables, other schema, privileges) |
| `found: null` | Existence could not be determined | Absence |
| `TABLE_ROWS` | An estimate of table size | Rows the statement touches |
| Index with `NON_UNIQUE = 0` on columns C | No two rows share non-NULL values for C | That a given value exists |
| `DATA_TYPE`, `IS_NULLABLE`, `COLUMN_KEY` | Column definitions at lookup time | Data distribution |

### C. Execution plan (`EXPLAIN SELECT` only)

| Fact | Proves | Does not prove |
| --- | --- | --- |
| A plan was returned | MySQL accepted the shape and privileges at that moment | Later behaviour |
| `rows` | Optimizer estimate of rows **examined** ("an educated guess") | Rows matched or changed |
| `type` `const` | At most one matching row for that table | That it exists |
| `type` `ref`/`range` | Index access to possibly many rows | Selectivity |
| Several plan rows | Several tables or steps | Anything from row 1 alone |

No `UPDATE`/`DELETE` plan is collected (section 11).

### D. Row-impact preflight

Not collected (section 12).

### E. Evidence unavailable

Not evidence of safety and not of danger. It never moves the score and never
raises confidence.

### F. Partial or conflicting evidence

Represented, never merged into success (section 5).

### G. Actual execution outcomes

Never collected. Submitted statements are never executed, and `EXPLAIN
ANALYZE` (which "runs a statement", including multi-table `UPDATE`/`DELETE`) is
never used. The API has no field for executed row counts.

---

## 5. Availability, completeness and failure semantics

- `collected` means MySQL returned that evidence, never that it is complete,
  sufficient or exact; table existence says nothing about row counts.
- Stable reason codes only (`not_configured`, `unreachable`, `auth_failed`,
  `database_not_found`, `insufficient_privileges`, `explain_failed`,
  `collection_failed`, `timeout`, `permission_denied`, `budget_exceeded`,
  `invalid_reference`, `unavailable`, `no_database_selected`,
  `plan_not_collected_read_only`, `plan_not_eligible`, `multi_row_plan`). No
  host, user, SQL or driver message is returned.
- Metadata: a timeout or error after some queries succeeded reports all
  metadata as `failed` (fail closed; kept from v0.4.0).
- `overall: partial` when one evidence type was collected and another failed
  for the same statement; deliberate non-collection (`not_collected_read_only`,
  `not_eligible`, `not_supported`) is not a failure.
- A connection that MySQL does not confirm as read-only is closed: locally the
  statement reports `connection: failed / collection_failed`; through the
  connector the request fails with `database_connection_failed` (Q6 below).
- Statements in a batch have independent states; `evidence_summary` counts
  them and sets `partial` across statements.

**Conflicts:**

| Conflict | Resolution |
| --- | --- |
| Column looks like a key, metadata shows it is not unique | Metadata wins: `not_established` |
| Metadata `found: false` | Scope cannot be `at_most_one_row`; confidence capped LIMITED |
| Plan collected for a SELECT but metadata failed | `partial`; plan shown; plan adjustments apply (section 8.3); M2 does not (it needs metadata) |

---

## 6. Evidence truth table

| State | DBA can conclude | Still unknown | Risk | Confidence |
| --- | --- | --- | --- | --- |
| Static, single-table write, no `WHERE`, no `LIMIT` | Every row is matched | How many rows | Hard 100 CRITICAL | HIGH |
| Static, write with `WHERE` | The text restricts rows somehow | Selectivity, count | +5, MEDIUM floor | LIMITED |
| Static, lossy parse | The analysed text may not be the submitted one | What was dropped | Unchanged | LOW |
| Metadata `found: true`, no verified key | Tables exist | Row scope | Unchanged | LIMITED |
| Metadata-verified unique key fixed by the `WHERE` | At most one row of the target can match directly | Whether it exists; triggers and foreign-key actions (`indirect_effects: not_checked`) | −10 (replaces +5, credits −5), never below MEDIUM (score 20) | HIGH |
| Metadata `found: false` / `null` | Not found / undeterminable in that schema | Why | Unchanged | Capped LIMITED |
| Write | No plan is requested | Access path | Unchanged | Unchanged |
| SELECT plan, one row | Expected access path, estimated rows examined | Actual rows | Unchanged | Unchanged |
| SELECT plan, several rows | Several steps | Per-step meaning | Unchanged; shown only | Unchanged |
| Partial / unavailable | What the collected part says | The rest | Unchanged | Never raised |

---

## 7. WHERE-clause specification

Community feedback (a Reddit thread, as reported by the maintainer; not quoted
here) pointed out that a `WHERE` clause was being read as a sign of safety.

**Rules**

1. Predicate presence is a structural fact, not a safety guarantee: a `WHERE`
   can match zero, one, some or all rows.
2. Selectivity cannot in general be determined from SQL text.
3. `IS NULL`, `IS NOT NULL` and broad conditions can match many rows. A
   predicate that is true for every row by its structure (`1`, `TRUE`,
   `1 = 1`, an `OR` with such a branch, `NOT FALSE`) matches every row and is
   treated exactly as no `WHERE`. This is decided from the parse tree; words
   and string literals (`'untrue'`, `'true'`) are never Boolean constants.
4. Column names prove nothing about uniqueness.
5. A primary-key or unique-key equality bounds the match to at most one row
   only when table metadata confirms the key (8.2), and even then the row may
   not exist.
6. Plans, counts and affected rows are different things: a plan estimates rows
   examined; a count measures rows a read matched; `UPDATE` reports rows
   *changed*.
7. A count can be stale before the write runs (consistent-read manual).
8. The product never claims a plan, count or predicate guarantees the impact.

**Acceptance criteria** (automated in `tests/test_row_scope.py` unless noted)

| # | Given | Then | Test |
| --- | --- | --- | --- |
| W1 | `UPDATE`/`DELETE` with any `WHERE`, no metadata | Score ≥ 20, no negative factor, `not_established` | `test_w1_where_without_metadata_is_never_narrow` |
| W2 | `WHERE id = 10`, no metadata | As W1; no "key"/"single row" wording | same; `test_risk_engine.py::test_id_equality_without_metadata_gets_no_key_credit` |
| W3 | `WHERE id >= 5`, `<= 999999999`, `BETWEEN` | As W1 | `test_w1_…` |
| W4 | `WHERE id = 5 OR status <> 'open'`; `WHERE order_id = 5 OR 1` | As W1; `OR 1` is always true: `all_rows`, CRITICAL, as with no `WHERE` (review H1) | `test_w1_…`, `test_w4_…`, `test_always_true_where_…` |
| W5 | `IS NOT NULL`, `IS NULL` | As W1 plus a NULL reason | `test_w5_…` |
| W6 | `WHERE customer_id = 42`, non-unique index | `not_established` | `test_predicates_that_do_not_fix_a_unique_key_are_not_narrow` |
| W7 | `WHERE id = 10`, `PRIMARY(id)` int confirmed | `at_most_one_row`, score 20 (MEDIUM floor; was 10 before the review), HIGH; "the row may not exist" | `test_w7_confirmed_primary_key_bounds_the_match` |
| W8 | Unique `VARCHAR code`, `WHERE code = 5` | `not_established` | `test_predicates_that_do_not_fix_…` |
| W9 | Composite unique key, partial equality | `not_established`; full equality → `at_most_one_row` | both parametrised tests |
| W10 | Joined, multi-table, subquery or CTE write | `not_established` | `test_joined_multi_table_and_cte_writes_are_not_narrow` |
| W11 | Any plan access type | No negative factor, no "selective" wording | `test_evidence_scoring.py::test_index_access_never_reduces_risk` |
| W12 | Any rendered result | No "safe" claim | `test_frontend_evidence_rendering.py::test_no_rendered_result_calls_sql_safe` |

---

## 8. Row scope and risk contract

### 8.1 Meaning of the score

Score (0–100) and `risk_level` express operational severity if the statement
runs as written; thresholds unchanged (LOW < 20 ≤ MEDIUM < 50 ≤ HIGH < 80 ≤
CRITICAL). Uncertainty is confidence, never the score. Evidence failures never
change the score. Batch `overall_score` is the maximum statement score.

### 8.2 Row scope (`backend/row_scope.py`)

| `scope.basis` | Established when | Source |
| --- | --- | --- |
| `all_rows` | `UPDATE`/`DELETE` on one physical table, no join, subquery, CTE, multi-table form or partition selection, no `LIMIT`, no `WHERE` or an always-true one, parsed faithfully | `sql` |
| `at_most_one_row` | All of: single-table `UPDATE`/`DELETE` parsed faithfully; `WHERE` not always-true; metadata collected with complete, untruncated, unambiguous `TABLES` and `STATISTICS` for that table and readable `COLUMNS`; an index with `NON_UNIQUE = 0` whose **every** column is fixed by a top-level `AND` term `column = literal` (column unqualified or qualified by the target table or its alias); each literal non-NULL and type-compatible: integer literal for integer columns, integer or decimal literal for `DECIMAL`/`NUMERIC`, string literal for `CHAR`/`VARCHAR`; anything else (dates, floats, binary, negative numbers, expressions) is not counted | `table_metadata` |
| `not_established` | Any other `UPDATE`/`DELETE` | — |
| `not_applicable` | Statements that do not change existing rows (`SELECT`, `INSERT`, DDL, DCL, …) | — |

Why `at_most_one_row` is justified: a `UNIQUE` index "creates a constraint such
that all values in the index must be distinct" and "permits multiple `NULL`
values" (CREATE INDEX manual); an equality with a non-NULL literal never
matches NULL; for a prefix index "the column values must be unique within the
prefix length", and equal full values have equal prefixes. Extra `AND` terms
only narrow. A string column compared with a number is compared as floating
point and many strings match one number (the manual's `WHERE c3 = 0` on a
`VARCHAR` matched every row), hence the type rule. `IS NULL` on a nullable
unique column is never `at_most_one_row`.

It is a bound, not a count: zero rows can match, and an `UPDATE` reports rows
changed, not matched.

Every basis describes the target table's directly matched rows only. Triggers,
foreign-key actions (`ON DELETE`/`ON UPDATE` `CASCADE`, `SET NULL`; see the
MySQL manual on foreign-key constraints) and other indirect effects are never
inspected; every write scope carries `indirect_effects: "not_checked"`, and the
scope reason and page say so.

### 8.3 Score rules

| Rule | Points | Evidence source |
| --- | --- | --- |
| Base by statement type (unchanged) | 0–40 | SQL |
| No `WHERE`, or an always-true one, on `UPDATE`/`DELETE`/`REPLACE` | score 100 (hard floor) | SQL |
| Broad `WHERE` heuristic (`id > 0`, `id >= 0`, not narrowed by `AND`) | +25 | SQL |
| `EXPLAIN ANALYZE <statement>` | Scored as that statement; unreadable target 50, `LOW` confidence | SQL |
| Plain `EXPLAIN`/`DESCRIBE` of a non-`SELECT` statement | +5 (MEDIUM) and a finding | SQL |
| Other `WHERE` on a write | +5, MEDIUM floor | SQL |
| Verified `at_most_one_row` | −10 (replaces the +5, credits −5), but never below score 20: a verified one-row `UPDATE`/`DELETE` is at least MEDIUM. `scope.factor.points` is the change actually applied (0 when the floor absorbs it) and `scope.factor.medium_floor_applied` says whether the floor limited it; the scope stays `at_most_one_row` and confidence `HIGH` | Table metadata |
| Multi-statement batch | +10 | SQL |
| Full table scan in a one-row plan: `SELECT` (2026-10-22), or a write | +10 | EXPLAIN (write plans are not collected in practice) |
| Optimizer estimate ≥ 10,000 rows examined in a one-row plan: `SELECT` (2026-10-22, not with `LIMIT` unless the plan shows `Using filesort` or `Using temporary`), or a write | +10 | EXPLAIN (an estimate, never a count) |
| M2 "filter and index": full scan, one `column = literal` filter on one table, and table metadata showing no index whose leading column is that column; `SELECT` (2026-10-22) or write | +5 | Plan + metadata |
| M1 "table size": large `TABLE_ROWS` with a full-scan write plan | +5 | Plan + metadata (not in practice: write plans are not collected) |

**SELECT plans (owner request, 2026-10-22).** The score is operational severity if
the statement runs (8.1); a full scan or a large optimizer estimate is load on the
server even for a read, so the plan rules above apply to a `SELECT`. With
`LIMIT`, MySQL stops once it has sent the requested rows, unless it must first
sort or group every matching row: "If a filesort must be done, all rows that
match the query without the LIMIT clause are selected, and most or all of them
are sorted" (MySQL 8.0, LIMIT Query Optimization). So with `LIMIT` and `Using
filesort` or `Using temporary` in the plan both rules apply as without `LIMIT`;
otherwise the full-scan rule applies with "how many rows are read is not known"
and the row-volume rule does not (the manual does not relate the `rows` estimate
to `LIMIT`). Not verified: aggregates such as `COUNT(*) … LIMIT 1`, which read
every row, may show neither Extra value. A small full scan stays
`LOW` (+10); the reported example (`ALL`, an estimate of 1,902,192 rows) is
`MEDIUM` (+20). Confidence is unchanged. The page names these "Table scan and
estimated row volume" and "Filter and index assessment"; `filter_index` (additive
response field) states the filter/index result even when no points apply:
`no_filter`, `not_assessed`, `not_checked`, `matching_index` or
`no_matching_index` (from table metadata only; it never says whether MySQL would
use an index).

Removed: −5 name-based "key-scoped", −3 indexed access. `LIMIT` is still not
penalised. CRITICAL is never reduced.

---

## 9. Confidence contract

| Level | Observable meaning | Cases |
| --- | --- | --- |
| HIGH | The facts that decide the risk are established | DDL, DCL, transaction control, `SELECT` (other than below); `UPDATE`/`DELETE` with `all_rows` or verified `at_most_one_row` |
| LIMITED | Sound for what is known, but a deciding fact (usually row scope) is not established | Other writes; `INSERT`, `REPLACE`, `CALL`; a `SELECT` with a locking clause (`FOR UPDATE`, `FOR SHARE`, `LOCK IN SHARE MODE`) or a call outside the reviewed function list (section 12), in any query block of a `UNION`/`INTERSECT`/`EXCEPT` or parenthesised query, also MEDIUM at least; a plain `EXPLAIN` of a `SELECT` with such a call (section 11: `EXPLAIN` can evaluate functions; no locking finding, as `EXPLAIN` takes no locks), also MEDIUM at least; any statement whose referenced table metadata did not find |
| LOW | The analysed statement may not be the submitted one, or could not be analysed | Parse error; lossy parse |

Rules (`risk_engine.static_confidence`, `main.final_confidence`):

1. Computed per statement from the SQL; one implementation.
2. Raised only by a verified `at_most_one_row` (never above LOW for an
   unreadable statement).
3. Capped at LIMITED when any referenced table is `found: false` or `null`.
4. Never raised by a connection, collected metadata, a plan, or missing
   evidence. Evidence that is not needed for the conclusion does not lower it
   (a failed plan does not lower a `DROP TABLE`'s HIGH).
5. Batch `overall_confidence` = lowest statement confidence.

---

## 10. API and frontend contract

### 10.1 Statement fields (additive)

| Field | Type / values | Absent vs empty | Producer | Frontend may show |
| --- | --- | --- | --- | --- |
| `scope.basis` | `all_rows` \| `at_most_one_row` \| `not_established` \| `not_applicable` | Always present | `row_scope.build_row_scope` | The matching sentence; never a count |
| `scope.source` | `sql` \| `table_metadata` \| `null` | `null` unless established | same | "from the SQL" / "confirmed from table metadata" |
| `scope.table`, `scope.index`, `scope.columns` | string, string, list | `null`/`[]` unless established | same | Names |
| `scope.factor` | `{label, points, medium_floor_applied}` \| `null` | `null` unless the scope is a verified `at_most_one_row` on a non-CRITICAL statement; `points` is the change applied (0 to −10) | `main.apply_scope_adjustment` | Factor |
| `confidence` | `HIGH` \| `LIMITED` \| `LOW` | Always | `main.final_confidence` | Label |
| `static_confidence` | same | Always | `risk_engine.static_confidence` | — |
| `analysis_text_differs` | boolean | Always (now always `false`; see 10.6) | parser (`lossy_parse`) | Warning |
| `analysis_status` | `analysed` \| `invalid` \| `unsupported` \| `unreadable` | Always | parser (`_not_analysed`); see 10.6 | Why a statement was not analysed |
| `evidence.plan.rows`, `.interpreted` | int, boolean | Only when `state = collected` | `evidence_state._plan` | Grid only when interpreted |

### 10.2 Batch fields

`overall_confidence`: lowest statement confidence. An empty batch is never
returned: input without statements is refused (`no_statements`, 10.6).
`overall_score` / `overall_risk_level` unchanged (maximum).

### 10.3 `v2_assessment` (Q5)

The analyzer builds it before scoring with placeholder `overall_score: 0` and
`risk_level: "UNKNOWN"`. `build_statement_result` now replaces
`overall_score`, `risk_level` and `confidence` with the statement's final
values. For a verified `at_most_one_row` it restates the Data Risk reason
(direct match bounded, triggers and foreign-key actions not checked) without
lowering its level, and restates the "Affected-row count is unknown" finding as
"Direct match bounded; total impact not established". Field names and types are unchanged, so existing
consumers keep working; only the misleading values changed. Its suggested
checks are guidance, never SQL derived from the submitted text. It remains a
secondary view; `score`, `risk_level`, `confidence`, `scope` and `evidence` are
authoritative. Tests: `test_v2_assessment_matches_the_result_and_is_never_a_placeholder`,
`test_v2_assessment_never_suggests_generated_sql`.

### 10.4 Compatibility

Additive fields; one added reason code (`multi_row_plan`). Behaviour changes
for API consumers: some `WHERE` writes move from LOW to MEDIUM; no-`WHERE`
writes report HIGH confidence; lossy parses report LOW; Company Mode write
plans are no longer collected (`plan.state: not_collected_read_only`), so
remote `evidence_factors`, `M1`/`M2` no longer appear for writes;
`analysis_mode` for a write-only Company Mode batch becomes
`STATIC_DATABASE_METADATA`. Deprecated but kept: `analysis_mode`,
`metadata_status` (both places), `high_estimated_rows_applied`.

### 10.5 Frontend (`frontend/index.html`)

| Element | Behaviour |
| --- | --- |
| Row scope line (Final result panel) | `all_rows`: "every row of T (no WHERE clause, from the SQL)"; `at_most_one_row`: "at most one row, by unique key I on T (confirmed from table metadata). The row may not exist."; `not_established`: "not established. A WHERE clause can match zero, one, some or all rows; …" |
| Evidence headline for `collected` | "Collected: table metadata only." / "Collected: an execution plan only (optimizer estimates)." / "Collected: table metadata and an execution plan (optimizer estimates)." |
| Plan | One-row plan: grid with "Estimated rows examined (estimate)"; multi-row: "Multi-step execution plan (N rows) collected: shown for reference, not summarised or used for scoring." |
| Read-only message | Explains that MySQL plans writes only on a writable session and the validator never asks for write access |
| Lossy parse | Warning above the SQL: "The SQL could only be read partly…" |
| Batch summary | Fourth cell "Batch confidence" (lowest statement; not a safety rating), separate from risk |

Every string is a fixed client-side message keyed by stable codes; only table
and index names from the user's own schema are interpolated.

---

### 10.6 Batch contract: what is analysed, what is refused (2026-10-22)

**Per statement (`analysis_status`).** A statement that is not `analysed`
always scores CRITICAL 100 with LOW confidence, gets no plan or metadata
evidence, keeps the check "Do not execute a statement that the validator could
not analyze.", and is shown with its submitted text. The wording says which
case it is:

| Status | When | Wording |
| --- | --- | --- |
| `invalid` | The statement does not begin with a word a MySQL 8.0 statement can begin with (`parser.MYSQL_STATEMENT_KEYWORDS`, from the manual's "SQL Statements" chapter), or begins with a literal or quoted identifier. MySQL rejects it. | "Not valid MySQL"; "Fix the SQL before proceeding." |
| `unsupported` | Valid MySQL the validator does not analyse: `VALUES` and `TABLE` (MySQL 8.0.19+), `HANDLER`, `KILL`, a `SET` of anything but user variables (system variables at any scope, `NAMES`, transaction characteristics; or a user variable set from an unreviewed function or a locking read), and a stored program definition (`CREATE [DEFINER = …] PROCEDURE \| FUNCTION \| TRIGGER \| EVENT`) submitted on its own. | "Not analysed (statement type not supported)"; "Review this statement manually …". Never called invalid. |
| `unreadable` | Begins with a statement keyword, but the parser could not read it as a statement (for example `XA START 'x'`), or read only its first keyword and kept the rest as unparsed text (`PREPARE`, `EXECUTE`, `LOCK`/`UNLOCK TABLES`, `RENAME`, `OPTIMIZE`, `CREATE`/`ALTER`/`DROP USER`, `CREATE ROLE`, `SET ROLE`, `GRANT`, some `REVOKE` forms, `REPLACE`; `CALL` keeps its rule), or misread it (`START REPLICA` read as a transaction). Validity is not determined. | "Could not be read (it may be invalid, or valid MySQL the validator cannot read)". Never called invalid. |

Invalidity is decided from the submitted tokens only, never from a parser
failure. When a script does not parse strictly, each statement's own text is
parsed strictly on its own: a statement that does not parse in full (for
example a trailing `SELECT … INTO @a`) is `unreadable` (or `invalid`), shown as
submitted, and never scored from a partial reading; the others are analysed as
usual. When the statements cannot be attributed to their text (the script
cannot be tokenized, or every statement parses on its own), the whole script is
one `unreadable` result. `analysis_text_differs` is therefore always `false`.

Every statement's `sql` is its text exactly as submitted, from its first to its
last token: comments before or after it and the `;` that ends it are not
included. An analysed statement is analysed, and planned, from the parser's
regeneration of it (`REGEXP` read as `REGEXP_LIKE()`, `LIMIT 20, 10` as
`LIMIT 10 OFFSET 20`), which is not shown. If the statements of a script parse
but cannot be matched to their text, each analysed statement's `sql` is that
regeneration instead (no input is known to do this).

**Optimization advice (`optimization`).** Advisory only, computed after the
result is final (`backend.optimization`): it never changes the score, the
confidence, completeness, the evidence or what is sent to a database, and it
rewrites, runs and measures nothing. `status` is `advised`, `none_found`,
`not_eligible` (with a `reason`: not analysed, not a single SELECT, or a locking
read) or `unavailable`. Each recommendation has `code`, `basis` (always `sql`:
observed in the SQL; nothing from a plan or a measurement), `title`,
`observation`, `action`, `reasoning`, `caveats` and `verify`. The rules for a
single SELECT are a `LIKE` pattern that starts with a wildcard and a filtered
column inside a function or calculation (both only when the query reads one base
table, with no join, subquery or CTE), and `ORDER BY RAND()`; and, citing the
MySQL 8.4 manual:

- `join_without_condition`: a table in `FROM` that no `ON`, `USING`, `NATURAL`
  or cross-table comparison in `WHERE` links to the others ("`INNER JOIN` and
  `,` … both produce a Cartesian product" without a join condition: JOIN
  Clause). An explicit `CROSS JOIN` is taken as intended. Nothing is said when
  a source is not a named table, a CTE is used, a condition has a subquery, or a
  column has no known table qualifier.
- `having_could_be_where`: a top-level `HAVING` condition that compares a
  grouping column, written as in `GROUP BY` and not a select-list alias, with
  constants only (`=`, `<>`, `<`, `>`, `<=>`, `IS`, `BETWEEN`, `IN`); never
  with `WITH ROLLUP`, a function, `LIKE`, `COLLATE`, `BINARY`, an aggregate or a
  subquery. `HAVING` is merged with `WHERE` only without `GROUP BY` and
  aggregates (WHERE Clause Optimization) and is otherwise "applied nearly last
  … with no optimization" (SELECT Statement).
- `union_distinct`: a `UNION` chain of plain `SELECT` blocks with at least one
  `UNION` or `UNION DISTINCT`. Duplicates are removed unless `ALL` is given, and
  `DISTINCT` overrides any `ALL` to its left (Set Operations with UNION,
  INTERSECT, and EXCEPT); a `UNION ALL` with no global `ORDER BY` can avoid the
  temporary table (Internal Temporary Tables). It suggests `UNION ALL` only if
  duplicates cannot occur or are acceptable, and does not check which. This is
  the only rule checked for a `UNION`; a `UNION ALL`-only chain, parenthesized
  blocks, `INTERSECT` and `EXCEPT` remain `not_eligible`.

Each names a pattern seen in the SQL, not a measured cost.

**Whole request refused.** Local `/api/validate` returns HTTP 200
`{"error": code}` (as for `statement_limit_exceeded`); the connector returns
HTTP 400 with `error_code`. No statements, no risk level and no confidence are
returned, and no database connection is used for evidence. The connector
decides these refusals before it opens a connection to the target.

| Code | When |
| --- | --- |
| `executable_comment_not_supported` | An executable comment (`/*! … */`, `/*!50000 … */`) outside strings, quoted identifiers and ordinary comments. MySQL runs its contents as SQL, but the parser would read it as a comment, so SQL inside it would be neither counted nor scored. The user must remove the comment or move its SQL out; the edited SQL, not the original script, is then assessed. Checked with and without backslash escapes in strings (the server's `NO_BACKSLASH_ESCAPES` mode is unknown), so a string containing `\'` followed by `/*!` text is also refused. |
| `no_statements` | Empty, whitespace-only or comment-only input (previously 200 with 0 statements, `overall_risk_level` LOW). |
| `delimiter_not_supported` | A line starting with `DELIMITER`: a mysql client command, not SQL, that changes where statements end. |
| `stored_program_not_supported` | A stored program definition together with other statements, or with a multi-statement body (`BEGIN … ; … END`): splitting on `;` would read body fragments as statements. |
| `statement_limit_exceeded` | More than 16 statements (unchanged). |

Order: `executable_comment_not_supported`, then `no_statements`, then the
script checks, then the statement limit (`main.script_error`, shared by both
paths). Semicolons and keywords inside strings and comments are never
boundaries (tokenizer-based).

**`--`** starts a comment only when followed by whitespace, a control
character or the end of the input, as in MySQL; otherwise it is read as two
minus signs (`balance--1` is `balance - -1`, and a statement after it is not
hidden). `parser.mysql_minus_signs` inserts a space between such a pair before
parsing. A statement the parser then cannot read (for example a line of
dashes, which MySQL also rejects) is `unreadable`, never a crash; its
displayed text shows the inserted space.

**`CALL`** is analysed as the call only: base risk 10, LIMITED confidence, the
finding "Stored procedure behavior cannot be fully determined from the CALL
statement alone." and the check to inspect the procedure definition. The
procedure's body is never read or reported.

Tests: `tests/test_batch_contract.py`, `tests/test_batch_validation.py`,
`test_remote_scripts_without_assessable_statements_are_refused`,
`test_statements_table_says_why_a_statement_was_not_analysed`,
`test_batch_contract_errors_have_the_same_message_locally_and_remotely`,
`test_executable_comments_are_refused`,
`test_double_minus_before_a_non_space_is_two_minus_signs`,
`test_executable_comment_is_refused_before_any_submitted_text_is_sent`.

## 11. Security and operational boundaries

| Boundary | Contract (main) |
| --- | --- |
| No execution | Submitted SQL is never executed; no `EXPLAIN ANALYZE`; `/api/evidence` stays disabled |
| Read-only sessions | `enforce_read_only_session()` (`backend/db.py`) runs `SET SESSION TRANSACTION READ ONLY` and requires `@@SESSION.transaction_read_only` = 1, or closes the connection. Used by `open_evidence_connection` (local), `get_connection` (legacy session path) and `ConnectorRuntime.require_read_only_session` (every company connection, including the operator `/explain` route) |
| No write plans | `UPDATE`/`DELETE` are never sent for `EXPLAIN` on any path; the operator `/explain` route refuses `EXPLAIN_UPDATE`/`EXPLAIN_DELETE` with `plan_not_collected_read_only` before connecting. No write privilege is needed or requested |
| Last-moment check | `db_evidence._is_plain_select_explain()` strictly re-parses the exact outgoing text: `EXPLAIN ` + exactly one `SELECT`; no `INTO`, no locking clause, no `exp.Anonymous` function; and every word directly before `(` in that text (tokens, so comments and nesting do not hide a call) is on `PERMITTED_CALL_NAMES`. The collector applies the same list to the submitted statement's tokens, so parser rewriting cannot approve a call. A connection that is not a confirmed `ReadOnlyEvidenceConnection` receives nothing, and the read-only state is read back on the same cursor immediately before the plan request |
| Function policy | `PERMITTED_FUNCTIONS`: reviewed MySQL 8.4 built-ins that compute a value from their arguments. `PERMITTED_SYNTAX_WORDS`: reserved words that take `(` as syntax (`IN`, `EXISTS`, `DECIMAL`, …). Excluded: anything that sleeps, waits, locks, reads files or reads/changes session state; non-deterministic and table functions; non-reserved keywords MySQL could resolve to a stored function (`ANY`, `SOME`, `AGAINST`, `DATETIME`). Quoted (`` `f`(x) ``) and qualified (`db.f(x)`) calls are refused, and so is any name that is not a plain ASCII word (`fünf(x)`, `1split(x)`) or a literal written before `(` (`9e(x)`, which sqlglot regenerates as `9e AS (x)`): the statement's call names are then unknown and it is not eligible. It relies on the manual's rule that an unqualified built-in name resolves to the built-in. Cost: some legitimate `SELECT`s (for example `MATCH … AGAINST`, `NOW()`, which sqlglot does not model) get no plan |
| Function evaluation | `EXPLAIN` can evaluate stored functions while planning (Percona, MySQL 8.0.21 and earlier: a function in a derived table deleted rows). Two controls: the function policy above (first) and the read-only transaction (second). The read-only transaction prohibits changes to tables but not to temporary tables (SET TRANSACTION manual), and it does not stop sleeping, locking or other server load; it is a session setting, not a privilege. Neither control has been tested against a live server |
| Least privilege | Recommended evidence account: `SELECT` on the reviewed tables (plans and metadata). `EXPLAIN` needs the explained statement's privileges, so `SELECT` plans need `SELECT`. Never `UPDATE`/`DELETE` for evidence |
| TLS and targets | Unchanged: CA required, `CERT_REQUIRED`, host-name check, pinned IP, approved targets only (`tests/test_read_only_boundary.py` re-asserts) |
| Multi-statement | PyMySQL default `client_flag` 0; no evidence connection sets it (asserted) |
| Credentials and errors | Unchanged: per request, never stored; only stable codes reach the page |
| Bounded work | Client timeouts; metadata row/table budgets; 16 statements; 64 KiB SQL |

---

## 12. Row-impact preflight (deferred)

**Not implemented (Q4).** No automatic count; no rewrite of writes into
`SELECT COUNT`. The suggested checks and checklist give manual guidance only:

> Review the candidate rows with a read-only query written for this
> statement's actual target table and predicate; a count is a point-in-time
> check, not a guarantee of what the write will change.
>
> Consider transaction boundaries, isolation level and concurrent changes
> between your check and the write.

Why deferred: under REPEATABLE READ a `DELETE` can affect rows committed after
a `SELECT COUNT` returned 0 (consistent-read manual, its own example); `UPDATE`
reports rows changed, not matched; a derived count is provably equivalent only
for narrow shapes; a count can scan the whole table; `MAX_EXECUTION_TIME`
applies only to read-only `SELECT`s, and a `SELECT` invoking a data-modifying
stored function is not read-only; locking reads (`FOR UPDATE`) lock every
scanned row until commit and need autocommit off.

A future preflight needs its own design decision covering: opt-in only;
single-table shapes without functions, subqueries or `LIMIT`; read-only
transaction plus timeout hint; never a locking read; result labelled "rows
matched by a read at T"; never changing the score; live-server tests.

---

## 13. MySQL version policy

- **Primary: MySQL 8.4 LTS.** All MySQL behaviour this contract relies on was
  checked in the 8.4 manual (section 19). **Not live-certified:** no MySQL 8.4
  server has been tested.
- **MySQL 8.0: compatibility target only where tested and documented.** Oracle
  states that "as of April 21, 2026, MySQL 8.0 is covered under Oracle
  Sustaining Support" and encourages upgrading to 8.4 LTS or 9.7 LTS. `EXPLAIN`
  semantics used here were also checked in the 8.0 manual; no 8.0 live test has
  been run for this revision, so nothing here is claimed as tested on 8.0.
- **Not considered:** 5.7, 9.x innovation releases, MariaDB, Aurora.
- **AWS RDS:** the maintainer reports a successful Company Mode test on RDS for
  MySQL with the v0.4.1-era code (VERIFICATION.md). Its MySQL version was not
  recorded and must not be inferred. It is not evidence for this revision.
- Every live test must record the server version and environment.

---

## 14. Test matrix and results

Environment for all results below: Windows 10, Python 3.13.5 (the project
targets 3.14), maintainer's machine, 2026-10-21, fake MySQL servers. No real
MySQL server was used.

| Area | Tests | Result |
| --- | --- | --- |
| Full suite (after the review fixes, 3.1, the one-row write floor and the unusual-name fix) | `python -m pytest -q` | 1182 passed, 11 skipped (macOS/POSIX-only), 4 failed: the 4 `test_windows_test_step_fails_when_any_command_fails` cases, which cannot run on this machine (no `pwsh`, PowerShell execution policy blocks scripts); they ran under `pwsh` in CI for v0.4.1. Same result with sqlglot 30.17.0 and with 30.21.0 (the release pin) |
| Review regressions (C1, H1–H3, M1–M6, L1–L2, one-row floor, unusual names) | `tests/test_review_remediation.py` | 197 passed (104 of the first 154 fail on the code before the fixes; 7 of the 32 unusual-name tests fail on the code before that fix) |
| Static predicates and row scope (W1–W10) | `tests/test_row_scope.py` | 59 passed |
| Read-only boundary and last-moment check | `tests/test_read_only_boundary.py`, `tests/test_local_evidence_credentials.py` | 49 and 74 passed |
| Local == Company Mode results | `tests/test_connector_remote_evidence.py` | 19 passed |
| Evidence contract semantics | `tests/test_evidence_contract.py` | 46 passed |
| Plan adjustments (W11, multi-row) | `tests/test_evidence_scoring.py` | Passed |
| Real-browser rendering (W12, scope, headlines, warnings, batch confidence) | `tests/test_frontend_evidence_rendering.py` with headless Google Chrome | Passed (not skipped) |

**Tests changed deliberately** (old expectation encoded the removed behaviour):
`test_risk_engine.py` (`WHERE id = 10` key credit → W2),
`test_evidence_scoring.py` (−3 → W11), `test_v2_integration.py` (generated
`COUNT(*)` → guidance), `test_connector_remote_evidence.py` (remote write plans
→ never requested; scenarios rewritten), `test_evidence_contract.py` (remote
DML plan → never requested; lossy `INTO` forms → LOW),
`test_local_evidence_credentials.py` (raw-connection `EXPLAIN DELETE` →
nothing sent; `SET_VAR` hint → not planned), `test_db_evidence.py` (`EXPLAIN
UPDATE/DELETE` → never sent), `test_connector_company_policy.py` (`/explain`
write operations refused), `test_frontend_evidence_rendering.py` (headlines
and messages). Test doubles gained the read-only read-back
(`tests/read_only_session.py`); stubbed plans now declare `plan_row_count=1`.
After the review (3.1): `test_row_scope.py` (`OR 1` → `all_rows`/CRITICAL;
Data Risk no longer `LOW`; `indirect_effects` field),
`test_frontend_evidence_rendering.py` (scope and plan wording),
`test_connector_remote_evidence.py` (`indirect_effects`),
`test_local_evidence_credentials.py` (`source_facts` parameter),
`test_database_selection.py`, `test_db_evidence.py`, `test_metadata.py` (fakes
answer the per-plan read-back and confirm the metadata fallback session).

**Not run** (each stays unknown until run on an authorized, disposable server
with known data):

| Live check | Expected | Status |
| --- | --- | --- |
| `SET SESSION TRANSACTION READ ONLY` → `@@SESSION.transaction_read_only` = 1 (8.4; 8.0) | 1 | NOT RUN |
| `EXPLAIN UPDATE/DELETE` on that session | Error 1792 | NOT RUN (observed for v0.3.0 on an unrecorded version) |
| `STATISTICS`/`COLUMNS` for primary, unique, composite, prefix, nullable unique | As 8.2 assumes | NOT RUN |
| `EXPLAIN SELECT` with a data-modifying stored function, on the read-only session | Refused before sending; if sent directly, MySQL refuses the write | NOT RUN |
| Stored/loadable functions named `split`, `levenshtein`, and one named like a listed built-in | Unlisted names never sent; the listed name resolves to the built-in | NOT RUN |
| A function that sets `transaction_read_only` back to 0 between plans | Next read-back fails; nothing more is sent | NOT RUN |
| Joined/subquery `SELECT` plans | Several rows → `interpreted: false` | NOT RUN |
| Exact SQL sent by the connector to a real server (general log) | Only section 2's list | NOT RUN |
| Company Mode against AWS RDS with this revision | Same as local | NOT RUN |
| Packaged Windows build, audit and smoke test with this revision | Pass | NOT RUN (no build; needs Python 3.14) |

No linter, type checker or static security scanner is configured in the
repository, so none was run.

---

## 15. Implementation traceability

| Change | Files |
| --- | --- |
| Physical tables, single-table shape, equality terms, NULL tests, constant-true `OR`, unreadable `WHERE` | `backend/parser.py` |
| Row scope and unique-key verification | `backend/row_scope.py` (new) |
| Static scoring without name heuristics; one confidence rule; guidance text | `backend/risk_engine.py`, `backend/analyzer.py`, `backend/evidence.py` |
| Read-only enforcement and read-back; leased legacy path | `backend/db.py` |
| Last-moment check; no write plans; plan row count | `backend/db_evidence.py` |
| One-row plan interpretation; −3 removed | `backend/evidence_scoring.py`, `backend/evidence_state.py` |
| Scope credit, final confidence, `v2_assessment`, batch confidence | `backend/main.py` |
| Company Mode read-only, `/explain` write refusal | `connector/server.py` |
| Scope line, headlines, plan, warning, batch confidence | `frontend/index.html` |
| Tests | `tests/test_row_scope.py`, `tests/test_read_only_boundary.py`, `tests/read_only_session.py` (new); updates listed in 14 |
| Docs | `ARCHITECTURE.md`, `SECURITY.md`, `VERIFICATION.md`, `connector/README.md`, `README.md` (unreleased-changes note) |

---

## 16. Decision register

| # | Decision | Rationale | Evidence | Alternatives | Limits |
| --- | --- | --- | --- | --- | --- |
| D1 | Keep and extend the v0.4.0 evidence object | Sound and shipped | `evidence_state.py`, contract tests | Replace | Legacy duplicates kept, deprecated |
| D2 | Score = severity, confidence = support (Q1) | Product decision | — | Merge | — |
| D3 | `WHERE`/names never narrow scope | Selectivity is data-dependent | F1 probe; consistent-read manual | More heuristic exclusions | Genuine key writes rise to MEDIUM without metadata |
| D4 | Only metadata-verified unique-key equality narrows scope | Constraint bounds matches | CREATE INDEX, Type Conversion manuals | Plan `const`; counts | Needs metadata (a login and database) |
| D5 | No plan lowers a score | `ref` non-unique | EXPLAIN Output manual | Keep −3 for `const` | — |
| D6 | All evidence sessions confirmed read-only; no write plans (Q3) | Least privilege; stored functions under `EXPLAIN` | Percona; SET TRANSACTION manual | Writable company session with write-capable account | Company Mode loses write plans, full-scan/high-estimate factors and M1/M2 |
| D7 | Last-moment check: AST shape plus a reviewed allowlist of call names on tokens of the exact outgoing and the submitted text (review C1) | `EXPLAIN` may evaluate functions; a name sqlglot knows is not a MySQL built-in | Percona; MySQL function name resolution; SQLGlot tokens | "Refuse functions sqlglot cannot name" (the reviewed defect); keyword search | Unlisted built-ins and `MATCH … AGAINST` get no plan |
| D8 | Lossy parse → LOW + warning | Analysed ≠ submitted | F4 probe | Refuse request | More LOW results |
| D9 | Batch confidence = minimum | Weakest statement bounds the batch | — | Majority | — |
| D10 | No preflight now (Q4) | Staleness, cost, equivalence | Consistent-read, UPDATE, locking manuals | Automatic count | Manual counting remains |
| D11 | Fill `v2_assessment` scores (Q5) | Remove misleading placeholders, keep schema | F7 | Remove field | Still a secondary view |
| D12 | One new query: read-back of `@@SESSION.transaction_read_only` | Prove enforcement | SET TRANSACTION manual | Trust the `SET` | Requires MySQL 8.0+ variable name |

---

## 17. Open questions and known limitations

- **Q6 (resolved by documentation):** a Company Mode connection failure,
  including an unconfirmed read-only session, fails the whole request with
  `database_connection_failed` rather than per-statement `connection: failed`
  as locally. The failure is visible, not hidden; changing it would be a
  separate API change.
- **Q7 (partly resolved):** `EXPLAIN` can evaluate stored functions
  (Percona, 8.0.21 and earlier; not confirmed for 8.4). Mitigated by refusing
  such plans and by the read-only session; the 8.4 behaviour is a live check
  (NOT RUN).
- Company Mode no longer produces write plans, so the full-scan,
  high-estimate, M1 and M2 adjustments do not arise in practice. Their code and
  unit tests remain for any plan supplied to them.
- `at_most_one_row` requires a selected database and readable metadata; without
  them a genuine key write is MEDIUM / LIMITED.
- A table whose `STATISTICS` include a functional key part (MySQL 8.0.13+,
  reported with `COLUMN_NAME` NULL) is treated as having ineligible index
  metadata, so no key of that table verifies scope, not even its primary key.
  This fails safe (MEDIUM / LIMITED); whether to verify the other keys is an
  open product question. Found while preparing the live plan (fake metadata,
  2026-10-21); not yet observed on a server.
- **Statements the parser cannot read in full (batch contract, 10.6):** such a
  statement is not analysed (`unreadable`, CRITICAL, LOW), including valid
  MySQL the parser does not fully support (`SELECT … INTO` in any position:
  before `FROM` sqlglot reads the target as a new table), which is therefore
  refused rather than assessed. The permissive
  fallback, which scored a partial reading and could show LOW 0 for text with a
  dropped write (`SELECT 1 DROP TABLE t`), was removed after v0.6.0. A
  statement read in full is still not proof that MySQL accepts it: the parser
  is lenient.
- **Comments (10.6):** executable comments are refused, not analysed, which
  also refuses valid scripts such as `mysqldump` output. The comment scan
  follows MySQL's rules for strings, quoted identifiers and comments but is
  not a MySQL lexer: MariaDB `/*M! … */` is an ordinary comment here, `#` and
  `--` comments end only at a line feed, and outside the executable-comment
  check the tokenizer assumes backslash escapes in strings.
- The stored-program checks read tokens, not stored-program grammar: a
  definition is recognised by `CREATE [DEFINER = account] PROCEDURE | FUNCTION
  | TRIGGER | EVENT`; a line starting with the word `delimiter` is always read
  as the client command (as the mysql client does).
- Temporary tables shadowing a base table, views, triggers and foreign-key
  actions are not modelled. Every write scope says `indirect_effects:
  not_checked`.
- **One-row write floor (owner decision, after the review):** a verified
  one-row `UPDATE`/`DELETE` is never `LOW` overall. The key bounds the direct
  match only; triggers, foreign-key actions and whether the row exists or
  changes are not established, so the overall risk stays at least `MEDIUM`.
  The verified scope remains visible (`scope.basis`, `scope.index`, the scope
  reason, `HIGH` confidence), and the credit still lowers a score that is
  above the floor (for example in a multi-statement batch). This refines Q1:
  a verified key still counts, but not down to `LOW`.
- The function allowlist depends on sqlglot's tokenizer and on the MySQL
  manual's name-resolution rule; neither has been checked against a live 8.4
  server. A server with a loadable function shadowing a listed name, or a
  proxy that rewrites statements, is outside what was tested.
- No live-server, packaged-build or RDS test has been run for this revision.

---

## 18. Acceptance criteria

| # | Criterion | Status |
| --- | --- | --- |
| 1 | W1–W12 automated | Met (section 7) |
| 2 | No score lowered by name, `WHERE` presence or access type; only D4 lowers | Met |
| 3 | One confidence implementation; caps and batch minimum tested | Met |
| 4 | `scope`, `analysis_text_differs`, `plan.interpreted`, `overall_confidence` in responses | Met |
| 5 | Page shows scope line, collected-what headline, warning, batch confidence (real Chrome) | Met |
| 6 | Every evidence session confirmed read-only; no write plan requested; last-moment check | Met (fake servers) |
| 7 | Existing security tests unchanged and passing; TLS rules re-asserted | Met |
| 8 | Packaged app audit and smoke test | **Not met: not run** |
| 9 | Live MySQL checks (section 14) recorded in VERIFICATION.md | **Not met: not run** (MySQL 8.4: no server available; the local 8.0.45 run was not completed). What a local 8.0.45 run could verify: live plan section 9 |
| 10 | Docs describe the new behaviour; v0.4.1 notes untouched | Met |

---

## 19. Sources

Fetched 2026-10-21.

| Source | Establishes | Used in |
| --- | --- | --- |
| [8.4 EXPLAIN](https://dev.mysql.com/doc/refman/8.4/en/explain.html) | Statements `EXPLAIN` supports; same privileges as the statement; `EXPLAIN ANALYZE` runs the statement, including multi-table `UPDATE`/`DELETE` | 4G, 11 |
| [8.0 EXPLAIN](https://dev.mysql.com/doc/refman/8.0/en/explain.html) | Same; `EXPLAIN ANALYZE` from 8.0.18 | 13 |
| [8.4 EXPLAIN Output](https://dev.mysql.com/doc/refman/8.4/en/explain-output.html) | `const`, `eq_ref`, `ref` ("cannot select a single row"), `range`, `ALL`; `rows` is an estimate of rows examined | 4C, D5 |
| [8.4 UPDATE](https://dev.mysql.com/doc/refman/8.4/en/update.html) | Returns rows changed; `LIMIT` is rows-matched; multi-table restrictions | 4A, 7, 12 |
| [8.4 DELETE](https://dev.mysql.com/doc/refman/8.4/en/delete.html) | Multi-table syntax deletes only from tables before `FROM`; restrictions | 4A, F5 |
| [8.4 Consistent nonlocking reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-consistent-read.html) | DML can affect rows a prior `SELECT COUNT` did not see | 7, 12 |
| [8.4 Locking reads](https://dev.mysql.com/doc/refman/8.4/en/innodb-locking-reads.html) | `FOR UPDATE`/`FOR SHARE` lock until commit; need autocommit off | 11, 12 |
| [8.4 Locks set by statements](https://dev.mysql.com/doc/refman/8.4/en/innodb-locks-set.html) | DML locks every scanned record; no index locks every row | 4C |
| [8.4 InnoDB transaction model](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-model.html) | Nonlocking consistent reads by default | 12 |
| [8.4 SET TRANSACTION](https://dev.mysql.com/doc/refman/8.4/en/set-transaction.html) | `READ ONLY` prohibits changes to tables (temporary tables excepted); session scope; `SELECT @@SESSION.transaction_read_only` reads the session value | 11, D12 |
| [8.4 CREATE INDEX](https://dev.mysql.com/doc/refman/8.4/en/create-index.html) | Unique values distinct; multiple `NULL`s allowed; uniqueness within prefix length | 8.2 |
| [8.4 Type conversion](https://dev.mysql.com/doc/refman/8.4/en/type-conversion.html) | String vs number compared as floating point; `WHERE c3 = 0` on `VARCHAR` matched every row | 8.2 |
| [8.4 Optimizer hints](https://dev.mysql.com/doc/refman/8.4/en/optimizer-hints.html) | `MAX_EXECUTION_TIME` only for read-only `SELECT`s; a data-modifying stored function makes a `SELECT` not read-only | 12 |
| [8.4 Function Name Parsing and Resolution](https://dev.mysql.com/doc/refman/8.4/en/function-resolution.html) | An unqualified name is a built-in if one exists; a stored function named like a built-in must be called schema-qualified; a loadable function cannot share a built-in's name (fetched 2026-10-21, after the review) | 11 (function policy) |
| [MySQL EOL notice](https://www.mysql.com/support/eol-notice.html) | 8.0 under Sustaining Support since April 21, 2026; upgrade to 8.4 LTS or 9.7 LTS | 13 |
| [Percona: when EXPLAIN can trash your database](https://www.percona.com/blog/uncommon-sense-mysql-when-explain-can-trash-your-database/) | On 8.0.21 and earlier `EXPLAIN` ran a stored function that deleted rows; a read-only transaction makes it fail | 11, Q7 (secondary source) |

Code references are to the main working tree on 2026-10-21.
