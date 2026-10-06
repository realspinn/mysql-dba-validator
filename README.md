<img src="frontend/assets/logo-mark.svg" alt="" width="48" height="48">

# MySQL DBA Validator

Review MySQL SQL before execution. MySQL DBA Validator is a local-first tool
for SQL from database tickets: paste SQL and get the statement classification,
a deterministic risk score, confidence, findings and a DBA review checklist.
Optional read-only evidence (`SELECT 1`, bounded `EXPLAIN`,
`information_schema` metadata) refines the score. No LLM makes risk or
security decisions. The tool supports a DBA's judgement; it does not guarantee
that SQL is safe.

**Status: v0.3.2, pre-1.0 beta**, available as a portable Windows x64
application ([release page](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.3.2)).
Static validation and the local workflow have automated tests; local database
evidence has not yet been verified against a real MySQL server. The remote/company
workflow through the local connector is tested locally (unit tests, a real
connector process, and real headless Chrome), but it has **not** yet been
verified end-to-end against real company infrastructure. Real company
infrastructure validation, including a hosted HTTPS frontend, company MySQL,
VPN/LAN connectivity and company TLS, remains outstanding. See
[Verification status](#verification-status).

---

## Download v0.3.2 (Windows x64)

A portable ZIP: no installer, and no Python, pip or virtual environment is
needed.

1. From the [v0.3.2 release page](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.3.2),
   download `MySQL-DBA-Validator-v0.3.2-windows-x64.zip` and the matching
   `.sha256` file.
2. Verify the download in PowerShell:

   ```powershell
   Get-FileHash .\MySQL-DBA-Validator-v0.3.2-windows-x64.zip -Algorithm SHA256
   ```

   The result must match the SHA256 shown on the release page and in the
   `.sha256` file.
3. Extract the zip anywhere, for example `C:\Tools\`.
4. Double-click `MySQL-DBA-Validator.exe`.

Your browser opens the local web page at `http://127.0.0.1:8420`; no other
window opens. To stop, close the page: the app stops on its own about 15
minutes after the last page of it was closed. Double-clicking again while it is
running just opens the page.

The executable is not code-signed, so Windows SmartScreen may warn on first
run. Only choose "Run anyway" if you downloaded the zip from this repository's
release page and its SHA256 matches.

**What starts.** The zip has two executables:

- `MySQL-DBA-Validator.exe` starts only the web page and validation API on
  `127.0.0.1:8420`, with no console window: static validation and local MySQL
  evidence.
- `MySQL-DBA-Validator Console.exe` starts the same web page and API plus the
  local connector on `127.0.0.1:8765`, used only for approved remote/company
  targets. Its console window is the connector's terminal. It shows the
  browser pairing code and accepts operator commands; type `help` there.
  Close the window, or press Ctrl+C in it, to stop.

**Where data goes.** The program folder is never written to. Per-user data is
kept in `%LOCALAPPDATA%\MySQLDBAValidator\`:

| File | Contents |
| --- | --- |
| `connector-targets.json` | Approved company targets: host, port, approval state, DNS snapshot. No credentials. |
| `logs\backend.log` | Web app log, replaced on each start. |
| `.env` (legacy, optional) | Not used for validation evidence. If present, only the API health check reports it. |

**Local evidence.** EXPLAIN and metadata evidence for MySQL on this machine
uses the login you enter in the page for that validation, on one connection
to the database you selected. The login is not saved. In **v0.2.0**, local
evidence instead read an account from `%LOCALAPPDATA%\MySQLDBAValidator\.env`;
that changed after v0.2.0.

**Optional settings.** Company targets use the Console version and need the CA
bundle that signs the company MySQL certificates:

```powershell
& ".\MySQL-DBA-Validator Console.exe" --tls-ca C:\path\to\company-ca.pem
```

Run `& ".\MySQL-DBA-Validator Console.exe" --help` for all options. The zip's `README.txt`
covers these steps in more detail.

---

## Security model

- **Local first.** Both services bind to `127.0.0.1` only. The browser talks
  to the local web app, and to the local connector for company targets.
- **Credentials are per request.** MySQL usernames and passwords typed into the
  page are sent with each operation and used only for that operation. They are
  not written to disk, the target registry, logs, URLs or browser storage.
  There is no credential vault. This does not claim that Python erases them
  from process memory.
- **Remote traffic goes through the connector.** Company targets are reached
  only through the local connector. The public web API never receives company
  credentials and has no fallback when the connector is unavailable.
- **Approved targets only.** The browser selects an approved target by its id.
  It cannot supply an arbitrary remote host. Targets are registered and
  approved only from the connector's console. Each connection re-checks the
  approved DNS identity and requires TLS verified against the configured CA.
  The target registry holds destinations and their approval state, never
  credentials.
- **Context is not authorization.** The backend classifies every target from
  its host and port. The deprecated `mode` field in `/api/validate` requests is ignored for
  that decision, so declaring a remote host "local" changes nothing.
- **Pairing.** The connector accepts pages from an explicit origin allowlist.
  A browser gets a session only by entering a single-use, five-minute pairing
  code shown in the console. Sessions live in memory and cannot manage
  targets.
- **No generic SQL endpoint.** Evidence is limited to `SELECT 1`, bounded
  `EXPLAIN` of supported statements and fixed metadata queries.

**Local evidence boundaries.** Local evidence opens one connection per
validation, with the login supplied for that validation, to the loopback MySQL
target and the selected database, and closes it when the validation ends. There
is no other credential source: without a login and database, evidence is
reported as not configured, and a rejected login is reported as such.

- Your MySQL account's privileges are the authorization boundary. `EXPLAIN`
  needs the same privileges as the statement it explains.
- The validator never executes the submitted SQL. On the local evidence
  connection it only runs `EXPLAIN` of one verified plain `SELECT` and fixed
  `information_schema` queries, never `EXPLAIN ANALYZE`.
- As defense in depth, the evidence connection is set to read-only
  transactions first; if that fails, no evidence is collected.
- MySQL refuses `EXPLAIN UPDATE`/`EXPLAIN DELETE` in a read-only session, so
  local `UPDATE` and `DELETE` get no execution-plan evidence (status
  `plan_not_collected_read_only`). Static analysis and table metadata still
  apply, and the analysis mode is shown as "Static + database metadata".

### Verification status

| Area | Status |
| --- | --- |
| Parser, risk engine, API, connector security rules | Automated tests |
| Local web app and connector in real headless Chrome | Passed, company MySQL replaced by a stand-in |
| Windows portable release | Built by CI from the release tag; the zip audited for secret values, private keys, forbidden files and machine-specific paths; smoke-tested from the extracted zip with no Python installed; checked in real Chrome |
| Local database evidence against a real MySQL server | Automated tests with a fake MySQL that models MySQL's read-only behaviour (error 1792); **not yet verified against a real MySQL server** |
| Real company infrastructure: hosted HTTPS frontend, company MySQL, VPN/LAN connectivity, company TLS | **Not verified** |

This is not a multi-user or internet-facing service. It has no user
authentication. See [SECURITY.md](SECURITY.md) to report vulnerabilities.

---

## Development

Requires Python 3.14 on Windows. Other platforms may work but are not tested.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Run the web app and, in a second terminal, the connector:

```powershell
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8420
python -m connector --allow-origin http://localhost:8420
```

Open `http://localhost:8420`. The connector requires an explicit
`--allow-origin` and has no default. Add `--tls-ca <ca.pem>` for company
targets and `--registry <file>` for a non-default registry. Connector details,
including operator commands, are in [connector/README.md](connector/README.md).

Local evidence needs no configuration file: enter a MySQL login and select a
database in the page.

Run the tests:

```powershell
python -m pytest -q
pip check
```

Layout:

```text
backend/     FastAPI app (main.py), parser and risk engine, evidence collectors
connector/   local connector: launcher, server, target policy and registry
frontend/    index.html (served page)
release/     Windows release launcher, PyInstaller spec, build, audit and smoke test
tests/       pytest suite
```

### Building a release

```powershell
python release\build_windows.py
python release\smoke_test_artifact.py dist\MySQL-DBA-Validator-v0.3.2-windows-x64.zip
```

The full process, including tagging and GitHub Releases, is in
[RELEASING.md](RELEASING.md).

---

## How scoring works

- **Deterministic first.** Findings and scores come from SQLGlot parser facts
  and explicit rules.
- **Severity is not certainty.** An `UPDATE` with a real `WHERE` clause is
  reported with explicit row-impact uncertainty instead of an invented number.
- **`LIMIT` is not a universal safety rule.** Missing `LIMIT` on a write is not
  penalised by itself.
- **MySQL transaction semantics.** Transaction advice is scoped to DML,
  because many MySQL DDL statements commit implicitly.
- **Bounded evidence adjustments.** With meaningful `EXPLAIN` evidence, the
  score may add 10 for a write full-table scan, add 10 for an optimizer
  estimate of at least 10,000 rows, or subtract 3 for confirmed
  `const`/`eq_ref`/`ref` access with a key. Adjustments never reduce a
  critical or unrestricted write. `estimated_rows` is an optimizer estimate,
  not an affected-row count.
- **Same scoring locally and remotely.** Remote validation through the
  connector uses the same scoring function as local validation. If evidence
  cannot be collected, the static result is returned and unavailable evidence
  never implies the SQL is safe.

## Running beyond one machine

The packaged release is meant for one user on one machine. Serving the web app
to other machines (`--host 0.0.0.0`, reverse proxy or tunnel) is a trusted
self-hosted setup only. In that setup the connection-test and discovery
endpoints accept a user-supplied MySQL host, and there is no authentication,
SaaS-grade SSRF/egress control or rate limiting. If you do it, set
`CORS_ALLOW_ORIGINS` to the exact HTTPS origin, terminate TLS in the proxy,
keep MySQL private, and use a read-only MySQL account.

## Roadmap

- Windows installer after the portable build has proven reliable.
- Deeper DBA analysis: stored procedures, transaction and session state,
  exportable reports.

## License

Released under the [MIT License](LICENSE). You may use, modify, fork and distribute it, including your own versions, provided the copyright and licence notice are kept.

