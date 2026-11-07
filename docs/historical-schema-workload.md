# Historical schema workload (optional diagnostic, API only)

An optional, read-only diagnostic. It shows the statement families MySQL has
already recorded for one schema in Performance Schema. It is separate from SQL
validation:

- It does not measure the SQL you are reviewing.
- It never runs that SQL.
- It never changes a validation result: score, risk level, confidence, row
  scope, findings and recommendations stay exactly as they were.

It runs only when you call its route. Nothing runs it in the background, nothing
stores its results, and there is no UI for it yet.

## Routes

| Mode | Route | Request body |
| --- | --- | --- |
| Local Mode (web app) | `POST /api/diagnostics/observed-performance` | `host` (loopback only, port 3306), `port`, `username`, `password`, `database` |
| Company Mode (connector) | `POST /company/targets/{target_id}/observed-performance` | `username`, `password`, `database` (a database discovered for this target and login) |

Neither route accepts SQL, a query or any other field: an unknown field is
refused before any connection. Both routes use the same checks as validation:

- **Local Mode:** loopback-only target, a valid database name, and one
  per-request connection bound to that database.
- **Company Mode:** paired browser session and origin, an approved target, the
  database scope found by discovery for this login, DNS and pinned IP checks,
  and verified TLS.

Both then require a session MySQL confirms as read-only before anything is read.
The public online demo does not serve either route.

## What it reads

Four fixed queries written by the application, on that read-only connection:

1. `@@SESSION.transaction_read_only` and `@@GLOBAL.performance_schema`. The
   read-only state is read back again here; if it is no longer 1, nothing more
   is sent.
2. `NAME, ENABLED` from `performance_schema.setup_consumers` for
   `global_instrumentation` and `statements_digest`. MySQL aggregates digests
   only when both are enabled.
3. The top five families for the selected schema, ordered by total time
   (`LIMIT 6`, so the result can say whether more exist). The only bound value
   is the schema name:

   ```sql
   SELECT DIGEST, DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT, AVG_TIMER_WAIT, MAX_TIMER_WAIT,
          QUANTILE_95, SUM_LOCK_TIME, SUM_ROWS_EXAMINED, SUM_ROWS_SENT, SUM_ROWS_AFFECTED,
          SUM_NO_INDEX_USED, FIRST_SEEN, LAST_SEEN
   FROM performance_schema.events_statements_summary_by_digest
   WHERE SCHEMA_NAME = ? AND DIGEST IS NOT NULL
   ORDER BY SUM_TIMER_WAIT DESC, COUNT_STAR DESC, DIGEST LIMIT 6
   ```

4. `COUNT_STAR` of the catch-all row (`DIGEST IS NULL`). MySQL counts
   statements there once the digest table is full, for all schemas together.

It never reads these:

- `QUERY_SAMPLE_TEXT`, which holds a real statement with its literal values
- the `events_statements_history` tables
- any other Performance Schema table

It never writes anything: no `TRUNCATE`, no change to `setup_*`, the optimizer
or the server configuration.

## Permissions

The connection uses the login you supply. Without the grants below, the route
reports `insufficient_privileges` and nothing else changes: validation works as
before.

The narrowest grant, verified on MySQL 8.4.13 in CI:

```sql
GRANT SELECT (SCHEMA_NAME, DIGEST, DIGEST_TEXT, COUNT_STAR, SUM_TIMER_WAIT, AVG_TIMER_WAIT,
              MAX_TIMER_WAIT, QUANTILE_95, SUM_LOCK_TIME, SUM_ROWS_EXAMINED, SUM_ROWS_SENT,
              SUM_ROWS_AFFECTED, SUM_NO_INDEX_USED, FIRST_SEEN, LAST_SEEN)
  ON performance_schema.events_statements_summary_by_digest TO 'dba'@'host';
-- Optional: without it the consumer state is reported as "unverified".
GRANT SELECT (NAME, ENABLED) ON performance_schema.setup_consumers TO 'dba'@'host';
```

What the CI checks showed for an account with only these column grants:

- The collector's queries work, including on the validator's read-only session.
- `QUERY_SAMPLE_TEXT`, `SUM_CPU_TIME` and `MIN_TIMER_WAIT` are refused with
  error 1143 (column access denied). `SELECT *` is refused with 1142, and so is
  `events_statements_history`.

**Whether to grant this is your decision as the DBA.** Even the column grant is
server-wide. Through that account you can read the normalized statement text,
counts and timings of every schema and every user, not only the one schema the
route asks for. Normalized text replaces literal values with `?` but keeps
table and column names. A table-level `SELECT` on the digest table, or a global
`SELECT`, exposes more, including `QUERY_SAMPLE_TEXT` with real values. The
route reads only what is listed above, but the account itself can read
whatever it is granted. The validator never requests, checks or changes grants.

## Reading the result

- `status` is one of these:
  - `available`
  - `no_observations`
  - `performance_schema_disabled`
  - `digest_consumer_disabled`
  - `insufficient_privileges`
  - `collection_failed`
  - `unexpected_response`
  - for Local Mode, a connection outcome: `auth_failed`, `unreachable` or
    `database_not_found`

  Families are returned only with `available`. If any returned row has a
  missing, negative or wrongly typed value, or an invalid digest, the whole
  result is `unexpected_response` with no families. Partial results are never
  returned.
- `digest_consumer` is `enabled`, or `unverified` when the account cannot read
  `setup_consumers`.
- `more_families` is true when the schema has more than five families.
- `overflow_executions` is the catch-all row's count. A large value means the
  digest table is full and these rows are not representative; the remedy is
  `performance_schema_digests_size` at server startup, which the validator
  never changes.
- **Times** are in milliseconds: MySQL's picoseconds divided by 10^9, rounded
  to 3 decimals. MySQL describes them as approximate. `p95_time_ms` comes from
  MySQL's histogram and is "a high estimate".
- **Untimed statements:** they are still counted but add nothing to the timer
  sums. A family with a zero total time shows `timing: "not_recorded"` and
  null times, never `0 ms`. The validator does not establish why: it reads no
  `setup_instruments`. Lock time is reported either way, because MySQL 8.4.13
  kept recording it with timing off.
- **The time window:** values accumulate from `first_seen` (server time, as
  MySQL returns it) until `last_seen`. There is no fixed window. A server
  restart or `TRUNCATE` of the table starts it again. `observed_at` is the
  validator's own UTC clock when the result was built.

## What it cannot tell you

- **It does not say how the SQL you submitted will perform.** No family is
  matched to submitted SQL. A family's average does not predict a new
  statement's runtime, its row count or its locks.
- **It is not a full view of the workload.** Only the top five families by
  total time are returned. Families are evicted into the catch-all row once the
  table is full. Rows are grouped by `SCHEMA_NAME`. In CI that was the
  connection's default database, so a statement run against this schema's
  tables from another default database (or none) is not counted here. The
  manual does not define this, so it is observed, not documented.
- **The validator's own statements show up.** Every connection records
  PyMySQL's `SET NAMES`, the read-only setting and its read-back under the
  selected schema. The diagnostic's own queries appear there too, as do
  validation's `EXPLAIN` and `information_schema` queries. In a quiet schema
  these can be all you see.
- **It is not a diagnosis.** It does not detect table bloat, stale optimizer
  statistics or a bad plan, and it does not recommend optimizations.

## MySQL versions

- **MySQL 8.4** is the version verified, against the CI 8.4.13 container.
- **MySQL 8.0:** the 8.0 manual describes every column read here, the
  `SUM_` columns through its general `SUM_xxx` rule. It gives no version note
  for `QUANTILE_95`, and the diagnostic has not been run against 8.0, so 8.0
  is unverified.

Sources:

- [Statement summary tables](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-statement-summary-tables.html)
- [Statement digests](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-statement-digests.html)
- [Timing](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-timing.html)
- [Table characteristics](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-table-characteristics.html)
- [8.0 statement summary tables](https://dev.mysql.com/doc/refman/8.0/en/performance-schema-statement-summary-tables.html)
