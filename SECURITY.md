# Security policy

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: open the repository's
**Security** tab and choose **Report a vulnerability**. Do not open a public
issue for a suspected vulnerability.

Please include the version, from `/api/health` or the release file name, along
with steps to reproduce and the impact you expect. Never include real database
credentials, company hostnames or customer data in a report.

## Supported versions

Only the latest release receives fixes while the project is pre-1.0.

## Security model in brief

How the components and request flows fit together is described in
[ARCHITECTURE.md](ARCHITECTURE.md). What has and has not been verified is in
[VERIFICATION.md](VERIFICATION.md).

- The web app (`127.0.0.1:8420`) and the local connector (`127.0.0.1:8765`)
  bind to loopback only.
- The web app answers only requests whose `Host` is `127.0.0.1` or
  `localhost` (plus exact names in `MDV_ALLOWED_HOSTS`, for a self-hosted
  setup); any other `Host` gets HTTP 400 before a route runs. This stops a web
  page whose DNS name is rebound to `127.0.0.1` from using the validation,
  connection-test and discovery endpoints. *(Since v0.8.0. In v0.7.0 and
  earlier only the heartbeat and update-check routes checked `Host`.)*
- Company MySQL credentials are supplied per request, used for that operation
  and not persisted. There is no credential store.
- The browser can use only connector targets approved at the connector
  console. The page may request a target, but the request is approved only
  with a one-time code that the console prints next to the exact host, port,
  resolved addresses and TLS trust (single use, 5 minutes, bound to the paired
  session, cancelled after three wrong codes). A request opens no connection,
  and changed DNS answers are never approved automatically. The code shows
  that someone at the console relayed it; it cannot show that they read the
  request.
- Each company connection re-validates the approved DNS identity and requires
  TLS verified against the target's trust source: the configured CA, an
  uploaded CA bundle checked by SHA-256 on every connection, or the Windows
  certificate store. A missing, changed or unknown trust source refuses the
  connection; there is no fallback. Python's `VERIFY_X509_STRICT` flag is off,
  as PyMySQL set it before; chain and host-name verification always apply.
- Host names must be ASCII (no look-alike Unicode names) and console output
  escapes every value, so a page-supplied name cannot rewrite the approval
  display. The connector's HTTP access log is off.
- Browser sessions require a single-use pairing code shown only in the
  connector console. Sessions are held in memory and cannot manage targets.
- Evidence queries are fixed or bounded: `SELECT 1`, `EXPLAIN` and fixed
  metadata queries. There is no generic SQL endpoint, and the submitted SQL is
  never executed (never `EXPLAIN ANALYZE`).
- Local evidence uses only the MySQL login supplied with that validation, on
  one connection to the selected database that is closed when the validation
  ends. The login is not saved and there is no other credential source.
- MySQL account privileges are the authorization boundary; the validator's
  statement restrictions are the application safety boundary.
- Every evidence connection, local and company, is set to read-only
  transactions and MySQL must confirm it (`@@SESSION.transaction_read_only`
  reads back as 1) before any evidence query, and again on the same cursor
  immediately before each plan request. Without that confirmation nothing is
  sent, the connection is closed and no further evidence is collected on it.
  *(Main branch, after v0.4.1. In v0.4.1 and earlier only the local
  connection was set read-only, without the read-back, and company
  connections were not.)*
- The read-only transaction is a second control, not a guarantee about
  functions. The MySQL manual says a read-only transaction prohibits changes
  to tables, but temporary tables stay writable, and it does not stop a
  function from sleeping, taking locks or otherwise using the server. It is
  also a session setting, not a privilege: it does not replace a
  least-privileged account. Whether a real server keeps it through every
  route (for example a connection-multiplexing proxy) has not been tested.
- A plan request is sent only when the exact outgoing statement is one plain
  `SELECT` with no `INTO`, no locking clause and no unqualified `DUAL` (the
  parser rewrites `FROM DUAL` as a quoted table name), and every word written
  directly before `(` in both the submitted text and the outgoing text is on a
  reviewed list of MySQL 8.4 built-in functions and reserved syntax words
  (`backend/db_evidence.py`, `PERMITTED_CALL_NAMES`). A name sqlglot happens to
  recognise is not enough: names such as `SPLIT` or `LEVENSHTEIN` are not MySQL
  built-ins, and MySQL resolves them to a loadable or stored function. Quoted
  and schema-qualified calls (`` `f`(x) ``, `db.f(x)`) are always refused, as
  is any name that is not a plain ASCII word (`fünf(x)`, `1split(x)`) and any
  literal written before `(` (`9e(x)`),
  and so are functions that sleep, wait, take locks, read files, or read or
  change session state. A statement that fails this check is still analysed;
  only its plan is not requested. This relies on the manual's rule that an
  unqualified built-in name resolves to the built-in; how a real server
  resolves and evaluates functions during `EXPLAIN` has not been tested.
- `UPDATE`, `DELETE` and everything else get no plan request, locally or
  through the connector, so the validator never needs an account with write
  privileges. There is no writable evidence connection and no switch to
  enable one.
- Each result reports its database evidence from what was actually collected
  (the per-statement `evidence` object). Missing or failed evidence never
  lowers a risk score and never changes confidence. A plan is reported only
  for a statement the validator parsed faithfully: a `SELECT … INTO` in any
  form is not analysed and gets no plan request, locally and through the
  connector.
- API responses, including request-validation errors, do not include submitted
  credential values.
- Submitted SQL is not logged. The SQL parser's own warnings quote the whole
  statement (including any password in `ALTER USER ... IDENTIFIED BY`); in
  v0.6.0 and earlier they reached `backend.log` and the connector console.
  They are silenced since v0.7.0. The log is replaced each time the app starts
  and can be deleted: `%LOCALAPPDATA%\MySQLDBAValidator\logs\backend.log` on
  Windows, `~/.local/state/MySQLDBAValidator/logs/backend.log` on macOS.
- **Update check (the only request to the internet).** When the page opens,
  the web app asks GitHub for the latest official release, at most once per
  start: one `GET` with no body to
  `https://api.github.com/repos/realspinn/mysql-dba-validator/releases/latest`,
  over TLS with certificate and host-name verification. It sends no SQL,
  login, host name, schema or other connection details. GitHub sees your IP
  address and a User-Agent naming the installed version
  (`MySQL-DBA-Validator/<version>`). Nothing is downloaded or installed; a
  newer release is only shown as a dismissible notice with a link to its
  official release page. Any failure (offline, timeout, failed TLS
  verification, rate limit, unexpected answer) shows nothing. Only this
  server's own loopback page can trigger it (`POST /api/update-status`,
  checked like the heartbeat), so it does not run when the page is served to
  other machines. To turn it off, set the environment variable
  `MDV_UPDATE_CHECK=off` (or add that line to the `.env` file in the data
  folder, `%LOCALAPPDATA%\MySQLDBAValidator\` on Windows): nothing is sent.

## Known limitations

- End-to-end use against real company infrastructure has not been verified.
  That includes a hosted HTTPS frontend, company MySQL, VPN/LAN and company TLS.
- Software running as the same Windows user can read the connector console
  and change the target registry and trust files. Target approval from the
  page does not protect against that.
- There is no user authentication. The tool is for a single user on their own
  machine, or a trusted self-hosted setup. It is not an internet-facing
  service.
- Local evidence runs with the privileges of the login you enter, so prefer
  the least-privileged account that can review the SQL. (v0.2.0 instead used a
  plain-text `.env` account for local evidence.)
- `UPDATE`/`DELETE` statements get no execution-plan evidence, locally or
  through the connector, because evidence connections are read-only and MySQL
  refuses `EXPLAIN` of writes there. Static analysis and table metadata still
  apply; a unique key confirmed in table metadata can still bound a write to at
  most one row.
- Grant the evidence account only what review needs: `SELECT` on the tables
  under review (for `EXPLAIN SELECT` and table metadata). Do not grant `UPDATE`
  or `DELETE` to obtain plans; the validator never requests write plans.
- Release builds are not code-signed. Verify the published SHA256 checksum.
