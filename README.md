<img src="frontend/assets/logo-mark.svg" alt="" width="48" height="48">

# MySQL DBA Validator

A local-first tool for reviewing MySQL SQL from database tickets before you run
it. Paste SQL and get the statement classification, a deterministic risk score,
confidence, findings and a DBA review checklist. Optional read-only evidence
(`SELECT 1`, bounded `EXPLAIN`, `information_schema` metadata) refines the
score. No LLM makes risk or security decisions.

**Status: 0.2.0, pre-1.0 beta.** Static validation and the local workflow are
tested. The remote/company workflow through the local connector is tested
locally (unit tests, a real connector process, and real headless Chrome), but
it has **not** yet been verified end-to-end against real company
infrastructure. Real company infrastructure validation, including a hosted
HTTPS frontend, company MySQL, VPN/LAN connectivity and company TLS, remains
outstanding. See [Verification status](#verification-status).

---

## Download and run (Windows)

No Python, pip or virtual environment is needed.

> **Status:** no downloadable release has been published yet. Until the
> Releases page lists one, run from source (see [Development](#development)).

1. Open the repository's **Releases** page and download
   `MySQL-DBA-Validator-v<version>-windows-x64.zip`. Download the matching
   `.sha256` file too.
2. Verify the download in PowerShell, and compare the result with the `.sha256` file:

   ```powershell
   Get-FileHash .\MySQL-DBA-Validator-v0.2.0-windows-x64.zip -Algorithm SHA256
   ```

3. Extract the zip anywhere, for example `C:\Tools\`.
4. Double-click `MySQL-DBA-Validator.exe`.

A console window opens and your browser opens `http://127.0.0.1:8420`. Keep
the console window open while you use the page. Close it, or press Ctrl+C in
it, to stop everything.

The build is not code-signed, so Windows SmartScreen may warn on first run.
Check the SHA256 before you choose "Run anyway".

**What starts.** The executable starts two loopback-only services:

- the web page and validation API on `127.0.0.1:8420`;
- the local connector on `127.0.0.1:8765`, used only for approved
  remote/company targets.

The console window is the connector's terminal. It shows the browser pairing
code and accepts operator commands; type `help` there.

**Where data goes.** The program folder is never written to. Per-user data is
kept in `%LOCALAPPDATA%\MySQLDBAValidator\`:

| File | Contents |
| --- | --- |
| `connector-targets.json` | Approved company targets: host, port, approval state, DNS snapshot. No credentials. |
| `logs\backend.log` | Web app log, replaced on each start. |
| `.env` (optional, you create it) | `MYSQL_*` settings for local evidence. |

**Optional settings.** Local evidence against a MySQL server on this machine
needs a read-only account in `%LOCALAPPDATA%\MySQLDBAValidator\.env`. The
packaged app reads no other `.env` file. Company targets need the CA bundle
that signs the company MySQL certificates:

```powershell
.\MySQL-DBA-Validator.exe --tls-ca C:\path\to\company-ca.pem
```

Run `.\MySQL-DBA-Validator.exe --help` for all options. The zip's `README.txt`
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
- **Pairing.** The connector accepts pages from an explicit origin allowlist.
  A browser gets a session only by entering a single-use, five-minute pairing
  code shown in the console. Sessions live in memory and cannot manage
  targets.
- **No generic SQL endpoint.** Evidence is limited to `SELECT 1`, bounded
  `EXPLAIN` of supported statements and fixed metadata queries.

**Local-evidence exception.** Local `EXPLAIN`/metadata evidence uses the
`MYSQL_USER`/`MYSQL_PASSWORD` from the optional `.env` file, which you store
in plain text. Credentials typed in the page are used for the local connection
test and database discovery.

### Verification status

| Area | Status |
| --- | --- |
| Parser, risk engine, API, connector security rules | Automated tests |
| Local web app and connector in real headless Chrome | Passed, company MySQL replaced by a stand-in |
| Windows portable release | Built and smoke-tested from the extracted zip |
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

For local evidence from source, copy `.env.example` to `.env` in the project
root and fill in a read-only account. `.env` is git-ignored.

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
python release\smoke_test_artifact.py dist\MySQL-DBA-Validator-v0.2.0-windows-x64.zip
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

