# InnoDB lock waits (optional diagnostic, API only)

An optional, read-only diagnostic. It answers four questions for one database:

- which session is waiting
- which session is blocking it
- on which table and index
- in which lock mode

It is separate from SQL validation: it never changes a score, risk level,
confidence, row scope, finding or recommendation.

It runs only when its route is called. Nothing polls in the background and
nothing stores the result. There is no UI for it yet.

**It never acts on what it finds.** It does not run `KILL`, `COMMIT`, `ROLLBACK`,
`LOCK TABLES` or any other statement that changes a session or data.

## Routes

| Mode | Route | Request body |
| --- | --- | --- |
| Local Mode (web app) | `POST /api/diagnostics/lock-waits` | `host` (loopback only, port 3306), `port`, `username`, `password`, `database` |
| Company Mode (connector) | `POST /company/targets/{target_id}/lock-waits` | `username`, `password`, `database` (a database discovered for this target and login) |

Neither route accepts SQL, a session ID or any other field: an unknown field is
refused before any connection. Both routes use the same checks as validation:

- **Local Mode:** loopback-only target, a valid database name, and one
  per-request connection bound to that database.
- **Company Mode:** paired browser session and origin, an approved target, the
  database scope found by discovery for this login, DNS and pinned IP checks,
  and verified TLS.

Both then require a session MySQL confirms as read-only before anything is read.
The public online demo does not serve either route.

## What it reads

Three fixed queries written by the application, on that read-only connection:

1. `@@SESSION.transaction_read_only` and `@@GLOBAL.performance_schema`. If the
   session is no longer read-only, nothing more is sent.
2. `performance_schema.data_lock_waits`, joined twice to
   `performance_schema.data_locks` (the waiting lock and the blocking lock).
   - It is limited to InnoDB waits whose waiting lock is on a table in the
     selected database.
   - It returns at most 20 waits (`LIMIT 21`, so the result can say whether
     more exist).
   - The only bound value is the database name.
3. `THREAD_ID, PROCESSLIST_ID, PROCESSLIST_TIME` from
   `performance_schema.threads`, only for the threads in those waits. This adds
   each session's connection ID (the ID `SHOW PROCESSLIST` shows) and the
   seconds it has spent in its current state. This query is optional: without
   the privilege, `sessions` is `unverified` and those values are null.

It never reads:

- `LOCK_DATA`, which holds the primary key or index values of the locked rows
- any SQL text: `PROCESSLIST_INFO`, `INNODB_TRX.trx_query` or statement history
- InnoDB transaction IDs, which for some transactions hold internal data that
  should not be interpreted (MySQL manual)

## Permissions

Without the grants below, the route reports `insufficient_privileges` and
validation works as before. The narrowest grant, verified on MySQL 8.4.13 in CI:

```sql
GRANT SELECT (ENGINE, REQUESTING_ENGINE_LOCK_ID, REQUESTING_THREAD_ID,
              BLOCKING_ENGINE_LOCK_ID, BLOCKING_THREAD_ID)
  ON performance_schema.data_lock_waits TO 'dba'@'host';
GRANT SELECT (ENGINE, ENGINE_LOCK_ID, OBJECT_SCHEMA, OBJECT_NAME, PARTITION_NAME,
              INDEX_NAME, LOCK_TYPE, LOCK_MODE)
  ON performance_schema.data_locks TO 'dba'@'host';
-- Optional: connection IDs and seconds in state.
GRANT SELECT (THREAD_ID, PROCESSLIST_ID, PROCESSLIST_TIME)
  ON performance_schema.threads TO 'dba'@'host';
```

What the CI checks showed for an account with only these column grants:

- It saw a lock wait between another account's sessions.
- `LOCK_DATA` and `threads.PROCESSLIST_INFO` are refused (1143).
- `INFORMATION_SCHEMA.INNODB_TRX` is refused (1227: it needs `PROCESS`).
- An account without the grants is refused (1142).

**Whether to grant this is your decision as the DBA.** These privileges are
server-wide: the account can see lock waits, table and index names, and
connection IDs for every schema and every user. The route asks only about the
selected database, but filtering by database is not a permission boundary. The
validator never requests, checks or changes grants.

## Reading the result

- `status` is one of these:
  - `available`
  - `no_lock_waits`
  - `performance_schema_disabled`
  - `insufficient_privileges`
  - `collection_failed`
  - `unexpected_response`
  - for Local Mode, a connection outcome: `auth_failed`, `unreachable` or
    `database_not_found`

  If any returned row has a missing or wrongly typed value, the whole result is
  `unexpected_response` with no waits. Partial results are never returned.
- Each wait has:
  - `waiting` and `blocking`, each with `thread_id`, `session_id` and
    `seconds_in_state`
  - `table`, `partition`, `index` and `lock_type` (`RECORD` or `TABLE`)
  - `requested_mode` and `blocking_mode`, as InnoDB reports them, for example
    `X,REC_NOT_GAP`

  One waiting session can appear more than once if several sessions block it.
- `seconds_in_state` is MySQL's `PROCESSLIST_TIME`: whole seconds in the
  thread's current state.
  - For a waiting session, that is roughly how long it has waited.
  - For a blocking session that is idle in an open transaction, it is how long
    it has been idle, not how long it has held the lock.
  - A thread that ended between the two reads has a null value, never 0.
- `observed_at` is the validator's UTC clock when the result was built.

## What a snapshot cannot tell you

- **Locks change quickly.** A blocking session shown here may already have
  committed or rolled back, and new waits may have started. Read the snapshot
  again before acting on it.
- **The read is not atomic.** The three queries run one after another, and the
  manual documents no consistency between `data_lock_waits` and `data_locks`.
- **`no_lock_waits` means only one thing:** no InnoDB row or table lock wait on
  this database's tables was found at that moment. It says nothing about:
  - metadata locks (for example, `ALTER TABLE` waiting)
  - other databases
  - waits that started or ended a moment before or after the read
  - other kinds of contention
- **It is not a deadlock report.** InnoDB resolves deadlocks itself;
  `SHOW ENGINE INNODB STATUS` has the latest one.

## MySQL versions

- **MySQL 8.4** is the version verified, against the CI 8.4.13 container.
- **MySQL 8.0:** the diagnostic has not been checked against the 8.0 manual or
  run against 8.0, so 8.0 is unverified.

Sources:

- [`data_lock_waits`](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-lock-waits-table.html)
- [`data_locks`](https://dev.mysql.com/doc/refman/8.4/en/performance-schema-data-locks-table.html)
- [`INNODB_TRX`](https://dev.mysql.com/doc/refman/8.4/en/information-schema-innodb-trx-table.html)
