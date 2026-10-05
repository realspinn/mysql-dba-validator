# CLAUDE.md — MySQL DBA Validator

Operational context for Claude Code sessions. Keep it lean; README.md and
connector/README.md hold user-facing detail. Tests are the source of truth.

## Purpose

Deterministic SQL risk review for MySQL ticket SQL: paste SQL, get statement
classification, risk score, confidence, findings and a DBA checklist. Optional
read-only evidence (`SELECT 1`, bounded `EXPLAIN`, metadata) refines the score.
No LLM makes risk or security decisions. Never overclaim what has been tested.

## Layout (only what you need to navigate)

- `backend/` public FastAPI app (`main.py`). Parser/risk engine (`parser.py`,
  `risk_engine.py`, `analyzer.py`, scoring modules) is established; do not
  touch unless the task requires it. DB evidence: `db.py`, `db_evidence.py`,
  `metadata.py`.
- `connector/` local connector on `127.0.0.1:8765`.
  `server.py` (large: grep for the section you need, do not read it whole),
  `launcher.py` (CLI, pairing code prompt), `policy.py` (CompanyTarget),
  `registry_store.py` (persistent target registry), `__main__.py`.
- `frontend/index.html` the served, connector-aware page.
- `release/` Windows portable release: `launcher.py` (packaged entry: backend
  child in a kill-on-close Job Object + connector launcher in the console),
  PyInstaller spec, `build_windows.py`, `audit_artifact.py`,
  `smoke_test_artifact.py`, pinned `requirements-build.txt`. See RELEASING.md.
- `backend/version.py` single app version (`APP_VERSION`).
- `tests/` pytest suite; `test_qa.py` at the root is also collected.

Do not normally read: `frontend/index_legacy.html` (rollback copy only),
`ARCHITECTURE_DESIGN_V2.md` (66 KB early design, partly superseded),
`test_clean_env/` (broken bundled venv, points at another user's Python),
`syntax_check.py`, `verify_refactor.py`, `__pycache__/`, `.env`.

## Architecture

```text
LOCAL:   Browser -> public FastAPI (backend.main:app, :8420) -> loopback MySQL :3306
REMOTE:  Browser -> local connector (127.0.0.1:8765) -> approved target -> company MySQL
```

The backend classifies targets by host/port; the frontend does not choose a
security mode (`mode` is deprecated and ignored). The frontend routes remote
hosts only to the connector; the public API never receives remote credentials.

## Credential rule (critical)

LOCAL: the user may type MySQL credentials in the browser for test-connection
and database discovery via the public API. Local evidence (EXPLAIN/metadata)
uses `MYSQL_USER`/`MYSQL_PASSWORD` from `.env`. This is intentional, unchanged
behaviour; whether local evidence should use typed credentials is an open
decision. Do not change it while working on remote features.

REMOTE/company: Browser -> local connector only. Company MySQL credentials are
per-request (`username`/`password` in the JSON body of validate, discovery,
connect, explain) and used only for that operation. They MUST NOT:

- reach the public FastAPI
- be persisted anywhere
- be stored in Windows Credential Manager (vault removed; do not reintroduce)
- be stored in the target registry
- be stored in browser localStorage/sessionStorage
- appear in URLs or query strings
- appear in logs, exception messages or responses

The connector is NOT a credential vault. Do not add any credential store.
Do not claim Python wipes memory.

## Target registry rule

The registry stores approved destination metadata/policy (host, port, approval
state, DNS identity snapshot, timestamps). It answers "WHERE may the connector
connect?", never "WHO is connecting?". Target metadata != user credentials;
target approval != user authentication. Registry: versioned JSON, atomic
writes, strict schema, fails closed on corruption (never silently recreated).

## Security boundaries (already implemented)

- Connector binds 127.0.0.1 only; explicit origin allowlist (`--allow-origin`
  or `CONNECTOR_ALLOWED_ORIGINS`), no default, no Cloudflare/tunnel default.
- Pairing: single-use code, 300 s, printed to the launcher terminal only; no
  HTTP route issues codes; no hidden JS globals.
- Sessions: in memory; `browser` scope allowlist (health, unpair, list approved
  targets, discovery, validate); everything else 403
  `insufficient_session_scope`. `operator` scope (register/approve/revoke/
  delete/connect/explain) is issued only programmatically. The old signed
  `/evidence` route (D2/Ed25519) was retired; do not reintroduce it.
- Only approved targets; destination validation re-checks DNS snapshot, exact
  IP and TLS (CA required) on every connection.
- No public-API fallback for remote; connector failure fails closed.
- No arbitrary SQL endpoint: discovery is fixed `SHOW DATABASES` (cap 1000);
  operator `/explain` is connector-built bounded `EXPLAIN`.
- Remote `/validate` evidence: connection uses `SSDictCursor` (dict rows, as the
  shared collectors expect) and scores via `backend.main.build_statement_result`,
  the same function as local. Remote metadata uses only the selected database
  (never `.env`); collector failure -> evidence unavailable, not 500.
- Body limits: 4 KiB default; remote validate 128 KiB with SQL <= 64 KiB UTF-8;
  enforced with or without Content-Length.
- Discovery results stored per (target, session), bound to HMAC(username,
  per-process key); dropped on unpair/expiry/target lifecycle change. This
  binding is not authorization.

## Milestone status (as of 2026-10-05)

- V1.1: DONE (static parser/risk engine).
- V2 Milestone A: DONE (connector-aware `index.html`; legacy page kept).
- V2 B1: DONE (pairing, browser sessions, origin restrictions).
- V2 B2: DONE (unit/TestClient + local connector-process smoke only).
- B3: IN PROGRESS, not complete. B3.1 = PASS (real headless Chrome + public
  app process + local connector). B3.2-B3.6 = NOT VERIFIED: no hosted HTTPS
  frontend, company MySQL/target/credentials, VPN/LAN route or company TLS CA.
  Connector `/evidence` retired; remote evidence scoring now matches local
  (unit/TestClient-verified only; no real MySQL yet). Deferred UX: page shows raw `database_connection_failed` (no friendly message).
  V2 is not complete.

### B2 (done)

Launcher `--registry` (> `CONNECTOR_TARGET_REGISTRY` > per-user default) and
`--tls-ca` (`CONNECTOR_TLS_CA`; invalid = refuse to start, absent = company ops
fail closed). Operator console in the launcher terminal (`targets`, `register`,
`approve`, `reapprove`, `revoke`, `delete`): terminal access = operator, no HTTP
or token. Discovery state keyed by `(target_id, session_token)`. Page: pair ->
approved-target selector (`target_id` only) -> own login -> connector discovery
-> database bound to target+login+session.

### B3 (integration milestone; do not redesign B2)

Final real-world end-to-end: real browser, hosted/public app, local connector,
approved company target, real MySQL, VPN/LAN as applicable, TLS/destination
validation, remote SQL validation/evidence. No real company MySQL/VPN/TLS or
hosted frontend has been tested. Local web-app + connector flows are covered by
real headless-Chrome harnesses (B3.1 and a web-app pass; company MySQL faked).

## Commands (confirmed working, Windows PowerShell)

Setup (repo has no committed venv; any Python 3.14 venv works):

```powershell
python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -r requirements.txt
```

```powershell
python -m pytest -q          # full suite (pytest.ini sets pythonpath=.)
pip check
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8420   # public API + page
python -m connector --allow-origin http://localhost:8420         # connector
python -m connector --allow-origin http://localhost:8420 --tls-ca <ca.pem> [--registry <file>]
```

Release (writes build/, dist/; both git-ignored; binaries go to GitHub Releases):

```powershell
python release\build_windows.py
python release\smoke_test_artifact.py dist\MySQL-DBA-Validator-v<ver>-windows-x64.zip
```

Packaged app data: `%LOCALAPPDATA%\MySQLDBAValidator\` (registry, logs\backend.log,
optional user-created `.env`; no other `.env` is read when packaged).

Project venv: `..\.venv` (parent folder), Python 3.14.8, recreated 2026-10-05.
`test_clean_env/` is broken; do not use it.

Environment notes: this machine has a system proxy; Python HTTP clients hitting
localhost need `httpx.Client(trust_env=False)`. PowerShell 5.1 has no
`Invoke-WebRequest -NoProxy`; `Set-Content -Encoding utf8` writes a BOM (use
the Write tool for files). Optional local DB config: copy `.env.example` to
`.env` (`MYSQL_*` variables).

## Testing rules

- Run focused tests, then the full suite after meaningful changes; run `pip check`.
- Do not modify existing tests merely to make them pass.
- When requirements intentionally change, replace obsolete tests with tests for
  the new contract and say so in the report.
- Security regressions (credential leakage, scope, origin, size limits,
  persistence) must be tested explicitly.
- Report verification honestly: unit/TestClient vs real HTTP process vs live
  infrastructure are different levels.

## Scope control

Before coding: 1) inspect the current implementation, 2) identify affected
files, 3) explain the design, 4) implement the smallest change, 5) run focused
tests, 6) run the full suite, 7) report remaining blockers.

- Do not redesign unrelated systems or modify parser/risk/evidence logic
  unless required.
- Do not silently change local behaviour while implementing remote behaviour.
- Do not add new infrastructure (Postgres, Redis, OAuth, vaults) unless asked.
- The user stages milestones strictly: stop at the milestone boundary.

## Context efficiency

- Prefer targeted Grep/partial reads over reading whole files or the repo.
- Use tests and existing docs as sources of truth.
- Do not reopen large files when the relevant section is already known.
- Decide which files are relevant before inspecting many.
- Avoid dumping large command output; filter it.
- For a new milestone, summarise current state before implementing.
- Use `/compact` when active context grows large; `/clear` for unrelated tasks.
