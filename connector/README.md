# Local Connector Stage 2

This package provides the local-only connector for the MySQL DBA Validator.

## Security boundary

- binds only to 127.0.0.1
- no 0.0.0.0 or wildcard listener
- no IPv6 listener
- no outbound network access beyond a single localhost MySQL connection test
- no arbitrary host, port, SQL, or network proxy capability
- pair by short-lived random token
- session tokens live only in memory
- origin validation is enforced against an explicit allowlist
- all other endpoints are rejected

## Stage 2 capability

The connector exposes a single authenticated local database test endpoint at `/db/test` only when the Stage 2 runtime is explicitly created with `allows_database=True`.

The safe enablement path is the explicit constructor/factory used by the Stage 2 launcher, not a default runtime. `create_app()` still creates the Stage 1 default runtime with `allows_database=False`, which keeps `/db/test` hidden unless the app is intentionally created as a Stage 2 runtime.

## Stage 3A company-target policy boundary

> Historical stage note. Company connections, TLS, per-request credentials and
> bounded discovery now exist; the current behaviour is described from
> "Configuration" onward. Only the approved-target / `target_id` principle below
> still applies unchanged.

Stage 3A is policy-only. It introduces a local, explicit Company target registry but does not enable any network or database connection capability.

The connector remains the authority for target approval. The public validator may only refer to an already approved target by `target_id`; it cannot supply an arbitrary hostname, IP, port, TLS configuration, credential, or database target for Company Mode.

Requirements:

- Local Mode remains unchanged and still only allows loopback localhost targets on port 3306.
- Company Mode is represented as a local approved-target policy object, not a generic network connection object.
- No DNS lookups, sockets, HTTP requests, MySQL connection attempts, or credential flows are enabled in Stage 3A.
- A target must be explicitly registered locally before it can be approved.
- Unknown or unapproved target IDs are rejected.
- Company DB connections, TLS, credentials, discovery, and metadata remain deferred to later stages.

This stage is intentionally a policy gate only.

Requirements:

- valid Stage 1 session token required
- valid Origin required
- local target only: `localhost` or `127.0.0.1`
- port must be exactly `3306`
- no arbitrary hosts, ports, DNS names, private IPs, public IPs, or IPv6 targets
- MySQL credentials are accepted only in the browser request and are used only inside the local connector process for the one connection attempt
- credentials are never logged, persisted, stored in URLs, or returned in responses
- the connection is closed immediately after the test

## MySQL connection policy

> Stage 2 (localhost `/db/test`) note. For company targets, discovery is the
> fixed, capped `SHOW DATABASES` described below; there is still no generic SQL
> endpoint.

- no generic SQL endpoint is provided
- no arbitrary SQL execution is allowed
- no discovery query is used in this stage
- no company cloud/dev database targets are supported yet
- only localhost MySQL is allowed for the Stage 2 boundary

## Running locally

```powershell
python -m connector --allow-origin http://localhost:8420
```

The launcher (`connector/launcher.py`) binds to `127.0.0.1:8765`, prints a
single-use pairing code (5 minute lifetime; press Enter for a new one) and serves
`create_app()`. Pairing codes are only ever issued in-process and printed to the
terminal; no HTTP route issues them, and they are never logged.

## Configuration

- Allowed browser origins: `--allow-origin ORIGIN` (repeatable) or the
  `CONNECTOR_ALLOWED_ORIGINS` environment variable (comma-separated). There is no
  default; with none configured the launcher refuses to start and a bare
  `create_app()` rejects every origin. Origins are normalised to
  `scheme://host[:port]`; wildcards, `null`, paths, credentials and non-loopback
  `http` origins are rejected.
- Browser session lifetime: `--session-ttl SECONDS` (60 to 28800, default 3600).
- Target registry file: `--registry PATH`, else `CONNECTOR_TARGET_REGISTRY`, else
  the per-user default `%LOCALAPPDATA%\MySQLDBAValidator\connector-targets.json`
  (`$XDG_STATE_HOME/...` or `~/.local/state/...` elsewhere). The resolved path is
  shown at startup. A registry that exists but cannot be loaded stops the launcher
  (exit code 2) and is left untouched.
- Company TLS CA: `--tls-ca PATH` or `CONNECTOR_TLS_CA` (PEM bundle containing at
  least one CA certificate). An invalid bundle stops the launcher. With none
  configured the connector still starts (local use, pairing), but every company
  discovery/validation fails closed before any TCP connection is opened.
  Client certificates are not configurable from the launcher.

```powershell
python -m connector --allow-origin http://localhost:8420 `
  --registry D:\connector\targets.json --tls-ca D:\connector\company-ca.pem
```

## Operator console (target lifecycle)

Target registration and approval are done by typing commands in the launcher
terminal. Whoever can type there is the operator: the same trust model as the
pairing codes printed there. There is no HTTP route, token or browser path for
it, and no command takes MySQL credentials. (The console starts only when stdin
is an interactive terminal.)

```text
targets                               list all targets and their states
register <id> <host> <port> <name...> register a target (always pending)
approve <id>                          snapshot the host's DNS identity and approve
reapprove <id>                        explicit re-approval of a needs_reapproval target
revoke <id>                           stop use until approved again
delete <id>                           remove; the id can never be reused
help
```

Each command prints one line with the outcome or a stable error code, and is
persisted atomically before it reports success. Approve, reapprove, revoke and
delete invalidate every session's discovered database list for that target.

## Company MySQL credentials (per request, never stored)

The browser supplies company MySQL credentials to the local connector per
operation. The connector uses them for that operation and does not persist them.

- Remote validate, database discovery, connect and explain each take `username`
  and `password` in the JSON request body (never in URLs or query strings).
- The connector is not a credential vault: there is no credential-provisioning
  endpoint, no Windows Credential Manager integration, and no code path that
  stores, reads back, or deletes a user's MySQL credentials.
- Credentials are never written to the target registry, session state, logs,
  error messages or responses, and never reach the public FastAPI API.
- Python cannot guarantee that memory is wiped; "per request" means the
  credentials exist only in the request/operation scope that needs them.
- Target approval is persistent; user MySQL credentials are not. The target
  registry and user credentials are separate concepts.
- Discovered database lists are stored per (target, browser session) and bound
  to a keyed fingerprint of the MySQL username, so another login, session or
  target cannot reuse them, and sessions never overwrite each other's lists. A
  session's lists are dropped on unpair or expiry. This binding is not
  authorization: every operation still requires a valid session, an approved
  target, destination validation, and MySQL accepting the supplied login.

Remote validate collects evidence on the per-request connection, which returns
dictionary rows (the same row contract as local validation), and scores it with
the same shared pipeline as local `/api/validate` (EXPLAIN adjustments, then
metadata M1 and M2). Metadata is only collected for the selected, discovered
database; without one it is reported unavailable rather than read from local
`.env` settings. A collector failure leaves the static result usable.

Request-size limits: remote validate accepts bodies up to 128 KiB with SQL up to
64 KiB of UTF-8 (the same SQL limit as the public API); every other connector
route keeps the 4 KiB limit. Limits are enforced with or without a
`Content-Length` header.

## Target registry

Approved targets persist in a versioned JSON file (`--registry` /
`CONNECTOR_TARGET_REGISTRY` / per-user default, see Configuration).
Writes are atomic (temp file, fsync, replace). A registry that cannot be parsed or
fails validation is never replaced; loading fails closed with a clear error. The
file holds destination policy only and rejects unknown fields.

## Session scopes

- `browser` sessions (from launcher pairing codes) may only call `GET /health`,
  `POST /unpair`, `GET /company/targets` (approved targets only), and
  `POST /company/targets/{id}/databases` and `/validate`. Every other route returns
  `403 insufficient_session_scope`. The approved-target, DNS, TLS and
  exact-destination checks still apply.
- `operator` sessions keep the full HTTP capability (target registration,
  approval, revocation, deletion, discovery, connect, bounded `/explain`). The
  former signed `/evidence` route has been removed; remote evidence comes from
  `/validate`. Operator sessions are only issued
  programmatically (`ConnectorRuntime.issue_pairing_token()`). The production
  operator workflow is the launcher's operator console, which needs no session.

## Stage 2 intent

This stage proves the connector can safely validate a local MySQL target and establish a localhost-only connection boundary without routing credentials to the public validator or enabling arbitrary database access.
