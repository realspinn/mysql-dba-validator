<img src="frontend/assets/logo-mark.svg" alt="" width="48" height="48">

# MySQL DBA Validator

**How risky is this SQL, and how sure are we about that?**

MySQL DBA Validator helps DBAs review MySQL SQL from database tickets before it
is run. Paste up to 16 statements and get, for each one, a risk level and
score, a confidence level, the findings behind the score and a review
checklist. Optionally, it adds read-only evidence from a database (execution
plans and table metadata).

Scoring is rule-based and deterministic; no AI or LLM makes risk decisions.
The validator never runs the SQL you submit. It supports a DBA's judgement and
does not guarantee that SQL is safe.

**Current release: [v0.8.0](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.8.0)**
(pre-1.0), a portable Windows x64 app, plus an unsigned macOS (Apple Silicon)
build.

![Validation results for a batch of four statements: overall risk CRITICAL, score 100 of 100, static analysis, batch confidence LIMITED; the top finding (statement 2, a DELETE with no WHERE clause), execution-plan, filter, limitation and unknown rows; and a table listing each statement with its risk, confidence and key finding](docs/images/validator-batch-review.png)

*The v0.5.0 results page after validating four made-up statements with no
database connected, so the result is static analysis only.*

## What it does

- **Classifies and scores each statement** from explicit rules: risk `LOW`,
  `MEDIUM`, `HIGH` or `CRITICAL`, with a score from 0 to 100. A batch's overall
  score is its highest statement score, not a sum.
- **Reports confidence separately from risk** (`HIGH`, `LIMITED` or `LOW`).
  Confidence is how well the evidence supports the assessment, not a safety
  rating.
- **Explains the result:** findings with the points they add, the rows a write
  can change where that is known, what is unknown, and a DBA checklist.
- **Says when it could not assess SQL.** Each statement is either analysed,
  not valid MySQL, a statement type it does not analyse, or SQL it could not
  read. Anything not analysed is shown as `CRITICAL` with `LOW` confidence,
  never as low risk.
- **Adds database evidence on request.** With a MySQL login it collects
  `EXPLAIN` for a plain `SELECT` and table metadata, on a read-only
  connection. Company servers are reached only through a local connector, for
  targets an operator has approved, over verified TLS. Each result says what
  evidence was collected, not collected, or failed.
- **Suggests what to check for performance (advisory).** For a single `SELECT`,
  or a `UNION` that removes duplicates, it notes patterns seen in the SQL text,
  each with caveats and how to verify (see
  [Optimization advice](#optimization-advice)). This advice comes from fixed
  rules, never changes the risk result, does not rewrite or run the SQL and is
  not a full performance review.
- **Keeps credentials out of storage.** Logins are used for one operation and
  never saved.

## Important limitations

- **Risk and confidence are assessments, not guarantees.** Review the SQL
  yourself before running it.
- **Statements the parser cannot read in full are not analysed.** Each
  statement is analysed only when the parser reads all of its text. Otherwise
  it is reported as not analysed (`CRITICAL`, `LOW` confidence, shown as
  submitted) and the review is marked incomplete; the other statements are
  still analysed. This includes valid MySQL the parser does not fully support,
  such as `SELECT ... INTO` in any position (`INTO OUTFILE`, `INTO @var`).
  When the statements cannot be told apart reliably (for example an
  unterminated string), the whole input is one unreadable result. A statement
  the parser reads in full is not proof that MySQL accepts it. Statements the
  parser reads only by their first keyword (`PREPARE`, `EXECUTE`,
  `LOCK TABLES`, `RENAME TABLE`, `OPTIMIZE TABLE`, `GRANT`, `CREATE USER`,
  `REPLACE`, ...), `KILL`, `SET` of anything but user variables, and
  `START REPLICA` are not analysed either. Execution risk is not assessed from
  the SQL alone; with an execution plan it reports the optimizer's estimates.
  Results show each statement's SQL as submitted, without comments before or
  after it.
- **A `SELECT` is not always a plain read.** A `SELECT` with a locking clause
  (`FOR UPDATE`, `FOR SHARE`, `LOCK IN SHARE MODE`), or one that calls a
  function outside the validator's reviewed list of MySQL built-ins (a stored
  function, `SLEEP`, `GET_LOCK`, `LOAD_FILE`, ...), is reported as at least
  `MEDIUM` with `LIMITED` confidence. This includes any part of a `UNION`,
  `INTERSECT` or `EXCEPT`, and the call (not the locking clause) in a
  `SELECT` that a plain `EXPLAIN` plans, because MySQL can evaluate functions
  while planning. The validator cannot see the transaction it runs in or what
  a stored function does.
- **Executable comments are refused.** MySQL runs the contents of executable
  comments (`/*! ... */`, `/*!50000 ... */`) as SQL, so input containing one
  is refused (`executable_comment_not_supported`) rather than assessed. This
  includes `mysqldump` headers such as `/*!40101 SET NAMES utf8mb4 */`. To
  validate, remove the comment or move its SQL out of it: the result then
  describes the edited SQL, not the original script. `/*!` inside a string,
  quoted identifier or ordinary comment is not refused, except in a string
  that also contains `\'`, which is refused because the server's backslash
  mode is not known.
- **`--` is read as MySQL reads it.** `--` starts a comment only when followed
  by whitespace, a control character or the end of the input. Otherwise it is
  two minus signs (`balance--1` is `balance - -1`), so a line of dashes such
  as `----------` is not a comment and the statement it starts is reported as
  not analysed (MySQL rejects it too). MariaDB `/*M! ... */` comments are
  treated as ordinary comments, and a comment ending at a lone carriage return
  may be read differently than by MySQL.
- **Refused input:** empty or comment-only input, `DELIMITER` scripts, and
  stored program definitions with a multi-statement body or mixed with other
  statements are refused rather than split unreliably. `CALL` never reports
  what a procedure does.
- **Little live MySQL validation since v0.5.0.** Changes since v0.5.0 are
  covered by automated tests. The only live check is v0.6.0's connector TLS
  behaviour (a trusted and an untrusted CA bundle, a host name not in the
  certificate) against one disposable local MySQL 8.0.45 server; a server that
  offers no TLS and MySQL 8.4 were not tested live. On earlier code, local
  evidence was verified against a real MySQL server, and the maintainer
  reported a successful test against an AWS RDS for MySQL test database.
  Company MySQL, VPN/LAN and company-issued TLS have never been verified.
- **Unsigned builds.** The Windows build is not code-signed. The macOS build
  is ad hoc signed only, not notarized, and has been tested only in CI, not on
  a real Mac.
- **Single user, local only.** There is no user authentication.

Details: the
[evidence contract](docs/v2-database-evidence-contract.md#17-open-questions-and-known-limitations),
[SECURITY.md](SECURITY.md#known-limitations) and
[VERIFICATION.md](VERIFICATION.md).

## Download and run

Download from the
[v0.8.0 release page](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.8.0).
Each file has a matching `.sha256` checksum file there.

| Platform | File | Status |
| --- | --- | --- |
| Windows x64 | `MySQL-DBA-Validator-v0.8.0-windows-x64.zip` | Built, audited and smoke-tested in CI; the release notes say which checks the maintainer repeated on the published zip |
| macOS, Apple Silicon only | `MySQL-DBA-Validator-v0.8.0-macos-arm64-unsigned.zip` | Built, audited and smoke-tested in CI only; ad hoc signed, not notarized |

**Windows**

1. Check the download in PowerShell, and compare the result with the
   `.sha256` file:

   ```powershell
   Get-FileHash .\MySQL-DBA-Validator-v0.8.0-windows-x64.zip -Algorithm SHA256
   ```

2. Extract the zip anywhere, for example `C:\Tools\`. No installer or Python
   is needed.
3. Run `MySQL-DBA-Validator.exe`. Your browser opens `http://127.0.0.1:8420`.
   SmartScreen may warn because the app is not code-signed; continue only if
   the checksum matched.
4. Paste SQL and choose **Validate SQL**. To stop, close the page; the app
   exits about 15 minutes later.

For approved company targets, run `MySQL-DBA-Validator Console.exe` instead:
its window is the local connector's operator console. The zip's `README.txt`
covers local evidence, company targets and troubleshooting.

**macOS (Apple Silicon)**

Check the download with
`shasum -a 256 -c MySQL-DBA-Validator-v0.8.0-macos-arm64-unsigned.zip.sha256`,
unzip it, and move `MySQL DBA Validator.app` to Applications. macOS blocks the
first launch because the app is not notarized; the
[release notes](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.8.0)
explain how to open it.

**Update notice (since v0.7.0).** When a newer official release exists, the
page shows a small notice with a link to its release notes; you can dismiss
it. Nothing is downloaded or installed. To check, the app asks GitHub once per
start for the latest release: GitHub sees your IP address and the installed
version, and no SQL, login or connection details are sent. Set
`MDV_UPDATE_CHECK=off` to turn it off; see
[SECURITY.md](SECURITY.md#security-model-in-brief).

## How it works

1. **Paste the SQL** from the ticket. Without a login, the result is static
   analysis of the SQL text, and says so.
2. **Optionally add evidence.** Open *Database connection*, enter a MySQL
   login, discover and select a database. For company servers, pair the page
   with the local connector and choose an approved target.
3. **Read the result:** overall risk and confidence first, then each
   statement's findings, row scope, evidence and limitations.
4. **Follow the checklist** and verify the change yourself before running it.

Evidence can adjust a score by small, bounded amounts. Missing evidence never
lowers a score. The rules are in [ARCHITECTURE.md](ARCHITECTURE.md#risk-scoring)
and the [evidence contract](docs/v2-database-evidence-contract.md).

## Optimization advice

Each statement card has a collapsed **Optimization advice** section. It is
checked for a fully analysed single `SELECT` without a locking clause, and for
a `UNION` of plain `SELECT` blocks that removes duplicates. Everything else
(writes, `UNION ALL` only, `INTERSECT`, `EXCEPT`, `FOR UPDATE`, SQL that was
not analysed) is shown as not checked. The rules, by their API `code`:

| Rule | What it notes | What to consider | Limits |
| --- | --- | --- | --- |
| `select_star` | `SELECT *` or `t.*` in the outer select list, when every source is a named table | Name only the columns the ticket needs; that can allow a covering index and returns less data | `*` is fine when every column is needed; it cannot see the table's columns or indexes |
| `column_in_expression` | A filtered column inside a function or calculation compared with a constant, such as `YEAR(created_at) = 2024` (one table, no join, subquery or CTE; not `MATCH ... AGAINST`) | Compare the bare column with a constant (a date range), or an index on the same expression | The rewrite must return exactly the same rows (whole last day for `DATETIME`, collation for `LOWER()`); a matching index may already exist; indexes slow writes |
| `leading_wildcard_like` | A constant `LIKE` pattern that starts with `%` or `_` (one table, as above) | An anchored pattern such as `'abc%'`, or whether `FULLTEXT` search meets the need | Both change the results; on a small or already narrowed table the cost may be low |
| `order_by_rand` | `ORDER BY RAND()` with a `FROM` other than `DUAL` alone | Fine for few matching rows; for many, a method that does not sort them all | Cost depends on the matching rows; random key values are not uniform when keys have gaps |
| `join_without_condition` | A table in `FROM` that no `ON`, `USING`, `NATURAL` or cross-table `WHERE` comparison links to the others | Add the missing condition, or write `CROSS JOIN` if every combination is wanted | Says nothing when it cannot tell: derived tables, table functions, `DUAL`, CTEs, subqueries in a condition, a function over columns of two tables (such as `ST_Contains`), unqualified columns. It does not check that a condition is the right one |
| `having_could_be_where` | A `HAVING` condition (one `AND` part) comparing a grouping column with constants | Move it to `WHERE`, so rows are filtered before grouping; aggregate conditions such as `COUNT(*) > 1` stay | Not with `WITH ROLLUP` or `OR`. A string column compared with a number can change the result when moved; column types are not visible, so check them |
| `union_distinct` | A `UNION` (or `UNION DISTINCT`) of plain `SELECT` blocks | `UNION ALL` only if duplicates cannot occur or the ticket accepts them | `UNION ALL` is not equivalent when duplicates exist, and that is not checked; a `UNION` also removes duplicates from every `UNION ALL` to its left |

The join, `HAVING` and `UNION` rules look at the outer query only, not inside
subqueries, derived tables or CTE bodies. The advice names a pattern seen in
the SQL text, not a measured cost: it does no cost-based or semantic
optimization, cannot see indexes, column types or data, and does not promise
that a change is faster. Each item says how to check it, with `EXPLAIN` and
timing on a non-production copy. The rules are covered by automated tests;
they have not been checked against a real MySQL server. Exact conditions are in
the [evidence contract](docs/v2-database-evidence-contract.md).

## Running beyond one machine

The release is built for one user on one machine: both services bind to
`127.0.0.1`. Company MySQL is reached only through the local connector on
that machine, under an operator's approval; see
[connector/README.md](connector/README.md).

Serving the web app to other machines (`--host 0.0.0.0`, a reverse proxy or a
tunnel) is a trusted self-hosted setup only. In that setup there is no
authentication, rate limiting or egress control, and anyone who can reach the
page can use the connection-test and discovery endpoints against the MySQL
server on the machine running the web app (they accept only loopback targets:
`127.0.0.1`, `::1` or `localhost`, port 3306). The web app answers only requests
whose `Host` is `127.0.0.1` or `localhost`; others get HTTP 400. If you do it,
add the exact host names clients use to `MDV_ALLOWED_HOSTS` (comma-separated;
wildcards are ignored), set `CORS_ALLOW_ORIGINS` to the exact HTTPS origin,
terminate TLS in the proxy, keep MySQL private and use a read-only MySQL
account.

## Documentation

| Document | Covers |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, request flows, what is sent to a database, scoring rules, stored data |
| [Evidence contract](docs/v2-database-evidence-contract.md) | What each result field means, the batch contract, known limitations |
| [SECURITY.md](SECURITY.md) | Security model, known limitations, vulnerability reporting |
| [VERIFICATION.md](VERIFICATION.md) | What has and has not been tested, and at which level |
| [connector/README.md](connector/README.md) | Local connector: configuration, operator console, sessions, credentials |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development setup, tests, review areas, pull requests |
| [RELEASING.md](RELEASING.md) | Maintainer build and release procedure |
| [Releases](https://github.com/realspinn/mysql-dba-validator/releases) | Changes, test results and checksums for each version |

## Development

The project is developed and tested on Windows with Python 3.14 (CI runs
Windows and macOS). To run the web app from source, from the repository root in
PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.main:app --host 127.0.0.1 --port 8420
```

Then open `http://localhost:8420`; stop it with Ctrl+C. Run the tests with
`python -m pytest -q` and `pip check` (no MySQL server needed). The connector
for company targets, `--reload` for development and the other platforms are in
[CONTRIBUTING.md](CONTRIBUTING.md#development-setup).

## Contributing and support

Bug reports with steps to reproduce, and suggestions, are welcome in
[GitHub Issues](https://github.com/realspinn/mysql-dba-validator/issues). Use
made-up SQL and never include credentials, hostnames or company data. Report
security vulnerabilities privately as described in [SECURITY.md](SECURITY.md),
not in a public issue. Read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[Code of Conduct](CODE_OF_CONDUCT.md) before opening a pull request.

## License

[MIT](LICENSE).
