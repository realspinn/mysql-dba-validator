# Verification

What has been tested, how, and at what level. The goal of this document is to
let you judge how much to trust a result or a release, so it keeps the levels
of verification apart and lists what has **not** been verified.

## Levels of verification

| Level | Meaning |
| --- | --- |
| Automated tests | `pytest` suite: unit tests, `TestClient` API tests, fake MySQL servers and mocked DNS. Needs no MySQL server and no real credentials. |
| Real browser | The served page driven in real headless Chrome. |
| Packaged application | The built Windows executables run from the extracted release zip, with no Python on `PATH`. |
| Real MySQL | A run against an actual MySQL server. |
| Company infrastructure | A run against real company MySQL, VPN/LAN connectivity and company TLS. |

A test at one level is not evidence for a higher one. A fake MySQL that models
MySQL's behaviour is not a real MySQL server.

Each result also says where it comes from: CI (a workflow run with logs), a
maintainer run whose results were recorded, or a manual test reported by the
maintainer without a recorded transcript. A reported test is weaker evidence
than a recorded one.

## Current status

| Area | Status |
| --- | --- |
| Parser, risk engine, scoring adjustments, API, evidence contract, connector security rules | Automated tests at v0.5.0: 1313 passed, 4 skipped on the maintainer's Windows machine (Python 3.13.5) before tagging; in the v0.5.0 tag runs (Python 3.14), 1315 passed, 2 skipped on Windows and 1316 passed, 1 skipped on macOS. See [below](#v050-release-verification). For v0.5.1: in the tag runs (Python 3.14), 1346 passed, 2 skipped on Windows and 1347 passed, 1 skipped on macOS; see [v0.5.1 release verification](#v051-release-verification) and its [source commit](#v051-source-commit-comment-parsing-fix-c884f44). For v0.6.0: in the tag runs (Python 3.14), 1546 passed, 2 skipped on Windows and 1547 passed, 1 skipped on macOS; see [v0.6.0 release verification](#v060-release-verification). For v0.7.0: in the tag runs, 1938 passed, 2 skipped on Windows and 1939 passed, 1 skipped on macOS (its [release notes](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.7.0)). For the v0.8.0 candidate, see [below](#unreleased-v080-candidate-local-checks) |
| Results page rendering of every evidence state | Real headless Chrome, driven by real API responses (part of the test suite) |
| Local web app and connector together in a browser | Real headless Chrome, with company MySQL replaced by a stand-in |
| Windows portable release | Built by CI from the release tag, audited, smoke-tested from the extracted zip. The published v0.6.0 zip's checksum was re-checked on the maintainer's machine; its smoke test and artifact audit ran in CI only (below) |
| Local database evidence against real MySQL | **Verified by the maintainer** with the Sakila sample database (below), on code before v0.5.0; not repeated for v0.5.0, v0.5.1, v0.6.0, v0.7.0 or the v0.8.0 candidate. Also covered by automated tests with a fake MySQL that models MySQL's read-only behaviour (error 1792) |
| Local Mode evidence against MySQL 8.4 in CI | **Automated, real server (unreleased, PR #34).** The CI job "MySQL 8.4 integration (ubuntu-24.04)" runs `tests_mysql/live_mysql_checks.py` against a disposable `mysql:8.4.13` service container (pinned by digest) on each pull request and push to `main`. Not a required check. First green run: [41205998394](https://github.com/realspinn/mysql-dba-validator/actions/runs/41205998394), 16 passed, 0 skipped. See [below](#ci-mysql-84-integration-job) |
| Remote/company workflow (local connector) end to end against a real MySQL server | **Reported by the maintainer** (manual test, recorded after the v0.4.1 release): successful on Windows against an AWS RDS for MySQL test database the maintainer created. No transcript or logs were provided for this record; see [below](#remote-workflow-against-aws-rds-for-mysql-maintainer-reported) |
| Real company MySQL, VPN/LAN connectivity, company-issued TLS, hosted HTTPS frontend | **Not verified** |
| Positive M1 metadata adjustment against a live large table | **Not verified.** It needs a collected `UPDATE`/`DELETE` plan. In v0.4.1 only the remote connector path collected one; since v0.5.0 neither path does (see below), so M1 does not arise in practice. Automated tests only |
| Evidence contract revision and batch contract (v0.5.0) | Automated tests only; no live-server check has been run. See [below](#evidence-contract-revision-v050) |

## Automated test suite

Run with `python -m pytest -q` and `pip check` (see the README's Development
section). The suite covers, among others:

- **Parsing and scoring:** `test_parser.py`, `test_risk_engine.py`,
  `test_evidence_scoring.py`, `test_metadata_scoring.py`, `test_m2_scoring.py`,
  `test_v2_integration.py`.
- **Evidence contract:** `test_evidence_contract.py` checks every response for
  internal consistency (evidence state, `evidence_summary`, `analysis_mode`,
  reasons, compatibility fields) across local and remote paths, mixed batches,
  `SELECT … INTO` forms, unsafe statements, missing tables and failures, and
  that missing evidence never changes risk or confidence.
- **Local evidence boundary:** `test_local_evidence_credentials.py`,
  `test_db_evidence.py`, `test_metadata.py`, `test_connection_api.py`,
  `test_database_selection.py`, `test_target_classification.py`,
  `test_mode_spoofing.py`. These assert what SQL reaches the (fake) server:
  the read-only session first, no plan request for anything but one plain
  `SELECT`, no retry after error 1792, one connection per validation.
- **Connector security:** `test_connector_pairing_session.py`,
  `test_connector_company_policy.py`, `test_connector_credentials.py`,
  `test_connector_target_registry.py`, `test_connector_remote_evidence.py`,
  `test_connector_b2_workflow.py`, `test_connector_stage1.py`,
  `test_connector_stage2.py`: origins, pairing, session scopes, approved
  targets, DNS identity and destination checks, TLS requirements, database
  binding, request limits, and remote scoring.
- **Credential handling:** `test_validation_error_redaction.py`,
  `test_security_hardening.py` and the credential tests above check that
  submitted credentials never appear in responses, errors or logs.
- **Row scope, risk and confidence (contract W1–W12):** `test_row_scope.py`,
  with fake servers that return explicit schema metadata (`NON_UNIQUE`,
  `DATA_TYPE`), never inferred from column names.
- **Pre-commit review fixes:** `test_review_remediation.py`: the function
  list for plan requests (names sqlglot knows that are not MySQL built-ins,
  quoted and qualified calls, nesting, rewriting), always-true `WHERE`,
  `EXPLAIN ANALYZE` and `DESCRIBE` scoring, the per-plan read-only read-back,
  and the collector's own eligibility checks.
- **Read-only boundary:** `test_read_only_boundary.py`: only allowlisted
  queries reach MySQL for every statement type on both paths, a session MySQL
  does not confirm as read-only gets no evidence query, TLS still requires a
  CA with certificate and host-name checks, and multi-statement mode stays off.
- **Page and packaging:** `test_frontend_served_page.py`,
  `test_frontend_evidence_rendering.py` (real Chrome; skipped if Chrome is not
  installed), `test_heartbeat.py`, `test_release_packaging.py`.
- **Honest assessment:** `test_honest_assessment.py`: statements the parser
  reads only by their keyword, `KILL`, `SET` of server or session settings and
  `START REPLICA` are not analysed; execution risk is not assessed without a
  plan and reports a plan's estimates as estimates; `sql` is the submitted
  text while the EXPLAIN sent is unchanged; validation logs no SQL or login.
- **Optimization advice:** `test_optimization_advice.py`: each of the seven
  rules' pattern, preconditions and non-matches; only an analysed, non-locking
  single `SELECT`, or a `UNION` of plain `SELECT` blocks that removes
  duplicates, is checked; replacing or breaking the adviser changes nothing else in the
  result or sent to the (fake) database; no rewritten SQL, no network or
  database access, no SQL logged. The page shows it collapsed beside the result,
  and the "AI Advisor — Coming Soon" note has no control but a close button,
  which hides it until the page is reloaded and stores nothing
  (`test_frontend_evidence_rendering.py`, real Chrome). Whether the advice
  helps on a real server has not been tested.
- **Host header:** `test_host_header.py`: a foreign `Host` gets HTTP 400 on
  every route before any MySQL call, loopback names still work, and
  `MDV_ALLOWED_HOSTS` accepts only exact names (a wildcard cannot open it).
- **Update notice:** `test_update_check.py`: version comparison, every
  failure path (HTTP 403/404/429, offline, timeout, TLS, malformed or
  unexpected JSON, a non-official release page), the exact request, caching,
  the off switch, origin protection, an unchanged CSP, and the banner in real
  Chrome (skipped if Chrome is not installed). GitHub is never contacted: the
  network is faked.

`.github/workflows/ci.yml` runs the suite and `pip check` on GitHub-hosted
Windows and macOS (Apple Silicon) runners, Python 3.14, for every pull request
to `main` and every push to `main`; on macOS it also fails on any unexpected
skipped test. It builds, packages and publishes nothing. The release workflows
run the suite again for release tags, before the build (below).

Windows has no skip check: the suite skips 2 tests there by design (POSIX-only
link resolution and port probe) and 2 more where symbolic links cannot be
created, as on the maintainer's machine.

### CI on `main` and branch protection

The workflow was added after v0.6.0 by pull request #1. It passed on the pull
request (run
[40093053323](https://github.com/realspinn/mysql-dba-validator/actions/runs/40093053323),
commit `4fe423c`) and, after the rebase merge, on the push to `main` (run
[40094012274](https://github.com/realspinn/mysql-dba-validator/actions/runs/40094012274),
commit `9ddc5a80f131af7952c0e48c2b0e59a68d4f5dd8`, 2026-10-24). From the job
logs of the `main` run:

| Check | `Tests (windows-latest)` | `Tests (macos-15)` |
| --- | --- | --- |
| Runner, Python | Windows Server 2025 (`windows-2025-vs2026`, GitHub-hosted), Python 3.14.9 | macOS 15.7.9 (`macos-15-arm64`, GitHub-hosted Apple Silicon), Python 3.14.7 |
| Test environment (installed from `requirements.txt`) | PyMySQL 1.2.3, sqlglot 30.24.0, cryptography 45.0.7, pytest 9.1.1 | The same versions |
| Test suite | 1546 passed, 2 skipped (the POSIX link-resolution and port-probe tests) | 1547 passed, 1 skipped (the Windows Job Object test); skip check: 1548 tests, 1 skipped, 0 unexpected |
| `pip check` | No broken requirements | No broken requirements |

These are CI results on Python 3.14, not the maintainer's Windows 10 machine
(Python 3.13.5) used for the local results in this document.

`main` is protected by a classic branch protection rule and, since 2026-10-26,
by two repository rulesets; all of them apply to `main`. The classic rule, read
back through the GitHub API on 2026-10-24 and again, unchanged, on 2026-10-26:

| Setting | Value | What it means ([GitHub docs](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)) |
| --- | --- | --- |
| Require a pull request before merging | On, 0 approvals required | Changes go through a pull request, but no approving review is needed to merge |
| Required status checks | `Tests (windows-latest)` and `Tests (macos-15)`, both from GitHub Actions (app ID 15368) | Both checks must pass on the pull request's latest commit before it can merge. They are the names the workflow's jobs report (`Tests (${{ matrix.os }})`); renaming the job or changing the matrix leaves the required names waiting for a status that never arrives |
| Require branches to be up to date | Off | A pull request can merge without being retested against the latest `main`; the push run on `main` then tests the merged result, after the merge |
| Do not allow bypassing the above settings | Off | The rule does not apply to administrators: on its own, the repository owner could still push to `main` or merge without the checks; the rulesets below now prevent both |
| Allow force pushes, allow deletions | Both off | GitHub's default for a protected branch: force pushes to `main` and deleting it are blocked |
| Linear history, signed commits, conversation resolution, code-owner review, push restrictions, branch lock, merge queue | Not configured | |

The rulesets, read back through the GitHub API on 2026-10-26. Both are active
and target the default branch (`main`):

| Ruleset | Rules | Bypass |
| --- | --- | --- |
| `main: owner review required` | Pull request required, with 1 approval and code-owner review (`.github/CODEOWNERS` assigns every file to `@realspinn`). New commits dismiss earlier approvals, and the most recent push must be approved by someone other than the person who pushed it. Review threads need not be resolved | Repository admins, for pull requests only: an admin can merge a pull request without the approval, as an explicit bypass |
| `main: required CI and history (no bypass)` | `Tests (windows-latest)` and `Tests (macos-15)` from GitHub Actions (app ID 15368) must pass; branches need not be up to date; force pushes and deleting `main` are blocked | None, administrators included |

With the rulesets, the classic rule's bypass setting no longer lets an
administrator merge without the two checks or push to `main` without a pull
request. GitHub does not let a pull request's author approve it, and the
repository owner is the only account with write access, so the owner's own pull
requests can merge only through the admin bypass.

This is the configuration as GitHub reports it, not an enforcement test: no
pull request with a failing or missing check has been tried against it, and
pull request #1 merged after its checks passed. A passing run means the suite
and `pip check` passed on those runners for that commit. The workflow builds,
packages, audits and smoke-tests nothing (the release workflows do), so it is
not evidence for a release, a real Mac, signing or notarization, any live MySQL
server (including MySQL 8.4 and a server that offers no TLS, T4b), or company
infrastructure, VPN and TLS.

## Release pipeline (CI, every release tag)

`.github/workflows/release-windows.yml` runs on a clean Windows runner:

1. The tag must match `APP_VERSION` in `backend/version.py`.
2. The full test suite and `pip check`.
3. A build in an isolated virtual environment from the exact pins in
   `release/requirements-build.txt`.
4. **Artifact audit** (`release/audit_artifact.py`, a release gate): no `.env`
   files, keys, certificates, registries, logs, tests, virtual environments or
   source-tree documents; no private-key material; no build-machine paths; no
   secret values from the developer `.env`. It inspects the executables'
   embedded Python archives as well as the loose files.
5. **Smoke test** (`release/smoke_test_artifact.py`, a release gate): extracts
   the zip into a path with spaces and runs the executable without Python on
   `PATH`. It checks the page, the API, static validation, the evidence
   contract for static and failed-connection results, the connector, origin
   rules, pairing, the approved-target list, company operations failing closed
   without a CA, credentials not leaking, graceful stop, restart and hard kill,
   and that the program folder is never written to.
6. The zip and its `.sha256` are attached to a **draft** release, which a
   maintainer verifies and publishes by hand ([RELEASING.md](RELEASING.md)).

## Unreleased: company connection errors and session expiry

Working tree on top of `b44d364` (not committed, not released), 2026-10-23.
Company connection failures get stable codes (see
[connector/README.md](connector/README.md#connection-errors)), discovery
returns the same codes as validation, and the page warns before a connector
session expires and reselects the previous target after re-pairing. Error
numbers were checked against the MySQL 8.0 and 8.4 server and client error
references; PyMySQL behaviour against the 1.2.3 source (the release pin).

| Check | Result | Where |
| --- | --- | --- |
| Full test suite and `pip check` | 1443 passed, 4 skipped; `pip check` clean | Maintainer's Windows 10 machine, Python 3.13.5, PyMySQL 1.2.0 (installed) |
| New connection-error tests, read-only boundary, company policy | 171 passed | Same machine, PyMySQL 1.2.3 (the release pin, loaded from a scratch directory) |
| New tests against a weakened connector | Error-number-only classifier: 21 fail. Discovery without the shared codes: 29 fail | Same machine, scratch pytest plugin |

The real-driver tests run PyMySQL's own `Connection.connect()` over an
in-memory socket. They show that the TLS context requires a certificate and
host-name match against the configured CA, and that the certificate is checked
against the approved host name while TCP goes to the pinned IP. They also show
that only the TLS request is written before the handshake, and nothing is
written when the server offers no TLS. One existing assertion changed: a TCP
connect timeout is now `network_timeout`, not `database_connection_failed`
(`test_stage3c_c1_closes_socket_when_connect_fails`).

Not shown: any live MySQL server, real TLS handshakes, company infrastructure,
the page in a browser (frontend checks are static), or CI.

## Unreleased: adding targets from the page

Same working tree, 2026-10-23, on top of the work above (not committed, not
released). The page can request a target; the console prints the exact request
and a one-time code that approves it (see
[connector/README.md](connector/README.md#adding-a-target-from-the-page)).
Trust is per target (`configured`, `bundle:<sha256>`, `system` on Windows), and
the connector passes PyMySQL an `ssl.SSLContext` it builds itself.

| Check | Result | Where |
| --- | --- | --- |
| Full test suite and `pip check` | 1543 passed, 4 skipped; `pip check` clean | Maintainer's Windows 10 machine, Python 3.13.5, PyMySQL 1.2.0 (installed) |
| Onboarding, connection-error, company policy, read-only boundary, B2 and credential tests | 385 passed | Same machine, PyMySQL 1.2.3 (the release pin, loaded from a scratch directory) |
| Onboarding tests against 12 weakened variants (code check, session binding, DNS re-check, single use, trust fallback, driver TLS-required check, bundle hash, private-key refusal, ASCII hosts, name characters, console escaping, access log) | Every variant makes at least one test fail | Same machine, scratch pytest plugin |
| Page scripts parse | Both inline scripts compile (`new Function`) | Headless Chrome on a scratch copy; nothing was executed |

Existing tests changed because the connector now loads the configured CA
itself: tests that pointed at a CA path that does not exist now use a generated
CA file (`tests/tls_test_ca.py`), assertions on the old `ssl` dict now check
the `SSLContext` (certificate required, host-name check, the loaded CA), and the
route-list test includes the two new routes.

Not shown: any live MySQL server or real TLS handshake, the Windows certificate
store against a real server, macOS behaviour, the page used in a browser, the
packaged Console executable, or CI. Strict X.509 verification
(`VERIFY_X509_STRICT`) is unchanged and untested against real chains.

## Unreleased: release-candidate checks

Same working tree, 2026-10-23 (not committed, not released). Adds a PyMySQL
floor (`pymysql>=1.2.0` in `requirements.txt`): the PyMySQL v1.0.2 and v1.1.3
sources skip TLS without an error when the server does not offer it, and
v1.2.0 raises `CR_SSL_CONNECTION_ERROR` instead. The release builds already
pin 1.2.3. The Windows certificate-store test no longer skips on other
platforms (it checks the refusal there); the macOS allowed-skip list is
unchanged.

| Check | Result | Where |
| --- | --- | --- |
| Connector, read-only boundary and frontend tests (13 files, not the full suite) | 659 passed, 0 skipped | Maintainer's Windows 10 machine, Python 3.13.5, PyMySQL 1.2.0; this tree, offline |
| `tests/test_release_macos.py` | 83 passed, 3 skipped (symbolic-link and POSIX path tests, which run on macOS) | Same; this tree |
| Store test with `sys.platform` reported as `darwin` | The refusal branch passes | Same machine; a simulation, not a macOS run |
| Live TLS through the connector's HTTP routes, including the page request and approval routes (in-process test client; real DNS, sockets and PyMySQL) | T1, uploaded CA bundle that signs the server: databases listed, and the server's general log shows the login over TLS. T2, uploaded bundle from another CA: `tls_verification_failed`, although the connector's configured CA would have verified (no fallback). T3, host name not in the certificate: `tls_verification_failed`. No login reached the server in T2 or T3. T5, after T2 and T3: works again. The password was never in the bytes sent before TLS | Disposable MySQL Community Server 8.0.45 on the maintainer's machine, PyMySQL 1.2.0; the tree before the dependency and documentation changes above |
| T4a, plaintext login with the `mysql` client | `ERROR 1045` from an account that requires SSL | Same server. Shows the account setting, not `require_secure_transport` |
| PyMySQL 1.2.3 runs of the server-offers-no-TLS tests | Reported as passed during development; not re-run for this record | Scratch install of 1.2.3 |

Not shown: T4b (a live server that offers no TLS; only the real-driver test
over an in-memory socket covers it), the negative cases through the page in a
browser, the full suite, Python 3.14 or PyMySQL 1.2.3 on this tree (the totals
in the sections above are for earlier states of the tree), any CI run of this
tree (the macOS CI result recorded for v0.5.1 applies to v0.5.1 only), a real
Mac (including approval prompts in Terminal), MySQL 8.4, a packaged build, and
company infrastructure, VPN and TLS.

## CI: MySQL 8.4 integration job

Unreleased (PR #34). Level: real MySQL 8.4.13 server (the official Docker
image, pinned by digest) in a GitHub Actions service container on
`ubuntu-24.04`. The validator runs in-process (FastAPI TestClient) with the real
PyMySQL driver over TCP on `127.0.0.1:3306`. Root is used only to load
`tests_mysql/setup.sql` and the existing
[02-fixture.sql](docs/v2-live-validation/02-fixture.sql). The tests use
`vt_evidence` (SELECT on the test schema), `vt_writer` (SELECT, INSERT, UPDATE,
DELETE; negative test only) and `vt_observer` (the statement digest table).
The container is removed when the job ends.

Shown on each run:

- Connection test and database discovery. Discovery does not list a schema the
  account has no privilege on.
- A wrong password gives `auth_failed`, a schema without privilege gives
  `insufficient_privileges`, and no password appears in any response.
- The read-only session is confirmed by the server before evidence is
  collected. An eligible `SELECT` gets a plain `EXPLAIN` plan (access type
  `const`): an optimizer estimate, not a count of rows read or changed.
- A missing table is reported as not found and caps confidence at LIMITED.
- [Live plan](docs/v2-live-mysql-validation-plan.md) case 5 row scope:
  - `PRIMARY`, `uq_tenant_ref`, `uq_email` and `uq_code_prefix` give
    `at_most_one_row`, 20 MEDIUM, HIGH, with the one-row floor.
  - The partial-key, `IS NULL`, non-unique, type-mismatch and `LOWER()`
    statements, and the functional-key table, give `not_established`,
    25 MEDIUM, LIMITED.
- Submitted `UPDATE`, `DELETE`, `INSERT` and a mixed batch, validated with the
  write-capable `vt_writer`:
  - every row of the fixture tables is unchanged afterwards;
  - no write, write-plan or `ANALYZE` statement digest appears. The digests are
    trusted only because the same run sees the validator's own `SET SESSION
    TRANSACTION READ ONLY` digest and no digest overflow.
- The full suite on Linux: 2289 passed, 1 skipped (Windows Job Object).

Not shown: Company Mode and the connector, any TLS (Local Mode has none),
AWS RDS, proxies, VPN, MySQL 8.0, the packaged app against a database, the
stored-function probes (plan cases 3(b) and 4), and server version detection
(the validator has none). Linux is a run-from-source check, not a release
platform.

## Unreleased: v0.8.0 candidate, local checks

Branch `release/v0.8.0-prep`, 2026-10-29 (not released), on `57e0614`
(`main`, after pull requests #30 and #31): documentation and the version bump
to 0.8.0 only. The code is that of `main`.

| Check | Result | Where |
| --- | --- | --- |
| Full suite | 2286 passed, 4 skipped (three symbolic-link or POSIX tests that run on macOS, one POSIX probe) | Maintainer's Windows 10 machine, Python 3.13.5; sqlglot 30.17.0, FastAPI 0.141.1, pydantic 2.11.7. This environment is not the release pins: PyMySQL 2.2.8 and cryptography 45.0.7 are outside `requirements.txt` (CI installs from it) |
| `pip check` | No broken requirements | Same machine |
| Version | `/api/health`, the FastAPI app and `release/build_windows.py` report 0.8.0 | Same machine |
| Web app from source (`uvicorn backend.main:app --host 127.0.0.1 --port 8420`), on the code of `main` | `/api/health` 200; the served page is byte-identical to `frontend/index.html`; one offline `/api/validate` (no login) returned the expected advice (`select_star` only for a join linked by `ST_Contains`); a foreign `Host` got 400 | Same machine, recorded by script |
| The same running app in a browser | End-to-end UI test, offline, **reported by the maintainer** without a transcript | Same machine |

Not shown by these local checks: any MySQL server (the new optimization rules
have not been checked against one), a packaged build, a real Mac, a screen
reader, and company infrastructure, VPN and TLS. CI, the release builds and
the published checksums are in the v0.8.0 release notes.

## Unreleased: v0.7.0 candidate, local checks

Branch `release/v0.7.0-prep`, 2026-10-25 (not released), on
`6f2d821` (`main`): `91148ee` (statements the validator cannot fully assess
are not analysed, SQL shown as submitted, Execution Risk without a plan, parser
logging silenced), `8452e4e` (optimization adviser), `3e3338d` (AI Advisor
teaser, update notice layout) and the version bump to 0.7.0.

| Check | Result | Where |
| --- | --- | --- |
| Full suite on each commit | `91148ee`: 1861 passed, 4 skipped. `8452e4e`: 1931 passed, 4 skipped. With the version bump: 1936 passed, 4 skipped. The skips are the platform-only ones (three symbolic-link or POSIX tests that run on macOS, one POSIX probe) | Maintainer's Windows 10 machine, Python 3.13.5, sqlglot 30.21.0 (the release pin); FastAPI 0.141.1, pydantic 2.11.7, PyMySQL 1.2.0, older than the release pins |
| Full suite with the version bump, other sqlglot versions | 1936 passed, 4 skipped on 30.17.0 and on 30.24.0 | Same machine |
| `pip check` | No broken requirements | Same machine |
| Version | `/api/health`, the FastAPI app and `release/build_windows.py` report 0.7.0 | Same machine |
| The running app in headless Chrome (ad hoc scripts, not in the repository) | Update notice: shown from a simulated `update_available` answer given in the browser (the server ran with `MDV_UPDATE_CHECK=off`), closed with the keyboard, still dismissed after a reload. AI Advisor teaser: closed with the keyboard, still closed after the next validation, nothing written to browser storage or cookies, shown again after a reload. Both close buttons show a focus outline in the light and dark themes; no horizontal scrolling at 390 px; no request left 127.0.0.1 | Same machine, on the tree of `3e3338d` |

Not shown by these local checks: any MySQL server, a real Mac, and a screen
reader. CI, the release builds and one real update check are in the next
section.

## v0.7.0 candidate: CI on `5f604a9` (not released)

Run on 2026-10-25 at `5f604a9828e180be3d9d7a3a40bed4b4223bf652`, the head of
`release/v0.7.0-prep` (the four commits above), through pull request
[#6](https://github.com/realspinn/mysql-dba-validator/pull/6). Both release
workflows were run by hand (`workflow_dispatch`) on that branch. These are
validation builds, not release builds: no tag, draft or release was created.

| Check | Windows | macOS |
| --- | --- | --- |
| CI (pull request), run [40148573700](https://github.com/realspinn/mysql-dba-validator/actions/runs/40148573700) | `Tests (windows-latest)` passed: 1938 passed, 2 skipped; `pip check` passed. This job has no unexpected-skip check | `Tests (macos-15)` passed: 1939 passed, 1 skipped (the Windows Job Object test); skip check: 1940 tests, 1 skipped, 0 unexpected; `pip check` passed |
| Release workflow run | [40148610155](https://github.com/realspinn/mysql-dba-validator/actions/runs/40148610155) | [40148613031](https://github.com/realspinn/mysql-dba-validator/actions/runs/40148613031) |
| Runner, Python | Windows Server 2025 (`windows-2025-vs2026`, GitHub-hosted), Python 3.14.9, x64 | macOS 15.7.9 (`macos-15-arm64`, GitHub-hosted Apple Silicon), Python 3.14.7 |
| Test environment (installed from `requirements.txt`, CI and release runs) | PyMySQL 1.2.3, sqlglot 30.24.0, cryptography 45.0.7, pytest 9.1.1 | The same versions |
| Test suite in the release run | 1938 passed, 2 skipped; `pip check` passed | 1939 passed, 1 skipped; skip check: 1940 tests, 1 skipped, 0 unexpected; `pip check` passed |
| Build and artifact audit | Portable build passed (PyInstaller 6.22.3); audit passed on the folder and the zip: 119 files, 2188 embedded modules, 0 secret values checked | Unsigned app build passed (PyInstaller 6.22.3); audit passed on the app and the `ditto` zip: 112 files, 2224 embedded modules, 0 secret values checked, expected version 0.7.0 |
| Smoke test (includes the packaged app reporting the release version) | 55 checks passed, 0 failed | 49/49 |
| Artifact | `MySQL-DBA-Validator-v0.7.0-windows-x64.zip`, SHA256 `94f37b7a4f07553adb3bcc3a46fa4d956f03755545b1619334abdd5de26c38ab` (workflow artifact `windows-x64-portable`) | `MySQL-DBA-Validator-v0.7.0-macos-arm64-unsigned.zip`, SHA256 `be27d3afe4f2cd315e772c46caffebca04fbeebfb7dd47f63f7fb0a5841df4dd` (workflow artifact `macos-arm64-unsigned-5f604a9…`) |
| Release, signing, notarization, publishing | "Tag matches backend/version.py" and "Create draft GitHub Release" skipped | "Tag matches backend/version.py", `publish-unsigned`, `sign-notarize`, `verify-signed` and `publish` skipped |

These zips were not downloaded or inspected on the maintainer's machine. A tag
run rebuilds the zips, so the published checksums may differ from these.

**Update check against the real GitHub API.** One request, on 2026-10-25, from
the source tree at `5f604a9` on the maintainer's Windows machine (Python
3.13.5), by calling `backend.update_check.check_for_update()` with
`MDV_UPDATE_CHECK` unset. Result: `up_to_date`, as expected while v0.6.0 is
the latest published release. This shows the fixed endpoint answered and was
read under certificate and host-name verification. It does not show the
`update_available` answer from the real API (simulated only, above), the check
from a packaged build, or the macOS certificate behaviour.

Not shown by these runs: a real-Mac acceptance pass (a CI run on a virtual Mac
is not one, and the macOS zip is ad hoc signed only, not notarized), any live
MySQL server, MySQL 8.4, a screen reader, a signed build, and company
infrastructure, VPN and TLS.

## v0.6.0 candidate: CI on `3b38133` (not released)

Both release workflows were run by hand (`workflow_dispatch`) on 2026-10-23 on
branch `release/v0.6.0-prep` at `3b38133cf191ba4d3d5924d43bb884331ce4a255`:
`4efbaf1` (the work in the sections above) plus the version bump to 0.6.0.
These are validation builds, not release builds: no tag, draft or release was
created.

| Check | Windows, run [40019920320](https://github.com/realspinn/mysql-dba-validator/actions/runs/40019920320) | macOS, run [40019926529](https://github.com/realspinn/mysql-dba-validator/actions/runs/40019926529) |
| --- | --- | --- |
| Runner, Python | Windows Server 2025 (`windows-2025-vs2026`, GitHub-hosted), Python 3.14.9, x64 | macOS 15.7.9 (`macos-15-arm64`, GitHub-hosted Apple Silicon), Python 3.14.7 |
| Test environment (installed from `requirements.txt`) | PyMySQL 1.2.3, sqlglot 30.24.0, cryptography 45.0.7, pytest 9.1.1 | The same versions |
| Test suite | 1546 passed, 2 skipped. The log does not name the skipped tests, and this job has no unexpected-skip check | 1547 passed, 1 skipped (the Windows Job Object test); skip check: 1548 tests, 1 skipped, 0 unexpected |
| `pip check` | Passed | Passed |
| Build and artifact audit | Portable build passed; audit passed on the folder and the zip: 119 files, 2184 embedded modules, 0 secret values checked | Unsigned app build passed (PyInstaller 6.22.3); audit passed on the app and the `ditto` zip: 112 files, 2220 embedded modules, 0 secret values checked, expected version 0.6.0 |
| Smoke test (includes the packaged app reporting version 0.6.0) | 55/55 | 49/49 |
| Artifact | `MySQL-DBA-Validator-v0.6.0-windows-x64.zip`, SHA256 `12aaf059489745e77ca8c1d8c4236e9b9dc7bd08193102e11cf6c0f77e4d6d56` (workflow artifact `windows-x64-portable`) | `MySQL-DBA-Validator-v0.6.0-macos-arm64-unsigned.zip`, SHA256 `b725966427b5be9c34f2612b1e750850d9c010b8231174728fd55b75d09bd8b6` (workflow artifact `macos-arm64-unsigned-3b38133…`) |
| Release, signing, notarization, publishing | "Create draft GitHub Release" skipped | `publish-unsigned`, `sign-notarize`, `verify-signed` and `publish` skipped |

Both zips were then downloaded to the maintainer's machine, and each matches
its `.sha256` file and the CI log. Their file lists, read without extracting or
running anything, contain no test files, certificates, keys, `.env` files,
registries, caches or source documents. Both ship the new page (adding targets,
the Windows-only certificate-store label, the per-target TLS message), and the
Windows zip's `README.txt` has the new instructions. The apps are built from
`release/requirements-build*.txt`, which pins PyMySQL 1.2.3. Reading the
PyInstaller module archive inside all four executables (both `.exe` files and
both macOS executables), without running them, confirmed it: each bundles the
`pymysql` package with `VERSION_STRING` 1.2.3. The bundled sqlglot version was
not read.

Not shown by these runs: a real-Mac acceptance pass (a CI run on a virtual Mac
is not one, and the macOS zip is ad hoc signed only, not notarized), any live
MySQL server (including T4b), MySQL 8.4, adding a target through the page in a
browser, a signed build, and company infrastructure, VPN and TLS.

## v0.6.0 release verification

All from tag `v0.6.0` (tag object `e4c37312791116391fa4d44a3affefb913533760`),
commit `a708ed54a6b35fbea63a6fbf25eb6d94a5011751`: the candidate `3b38133`
([CI above](#v060-candidate-ci-on-3b38133-not-released)) plus the README and
verification-record update. The sections headed "Unreleased" above describe
the work released in v0.6.0. Published as
[v0.6.0](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.6.0)
on 2026-10-23 at 21:05:03 UTC; GitHub reports it as published (not a draft or
prerelease) and Latest. v0.5.1 is still published, with its tag and asset
digests unchanged.

| Check | Result | Where |
| --- | --- | --- |
| Windows tag run ([40023050682](https://github.com/realspinn/mysql-dba-validator/actions/runs/40023050682)) | Tag matched `APP_VERSION`. Tests 1546 passed, 2 skipped (the log does not name the skipped tests); `pip check` passed; portable folder and zip built; artifact audit passed on both (119 files, 2184 embedded modules, 0 secret values checked); smoke test 55/55; draft release created | CI, Windows 2025, Python 3.14.9, PyMySQL 1.2.3 and sqlglot 30.24.0 for the tests |
| macOS tag run ([40023050657](https://github.com/realspinn/mysql-dba-validator/actions/runs/40023050657)) | Tag matched `APP_VERSION`. Tests 1547 passed, 1 skipped (the Windows Job Object test, expected); skip check passed (0 unexpected); `pip check` passed; unsigned app and zip built; audits passed on both (112 files, 2220 embedded modules, 0 secret values checked); smoke test 49/49; `publish-unsigned` checked the zip's checksum and uploaded it to the draft. `sign-notarize`, `verify-signed` and `publish` were skipped | CI, GitHub-hosted Apple Silicon runner, macOS 15.7.9, Python 3.14.7, PyMySQL 1.2.3 and sqlglot 30.24.0 for the tests |
| Published Windows zip | `MySQL-DBA-Validator-v0.6.0-windows-x64.zip`: 32,984,835 bytes, SHA256 `7f077ba982fbde49d1b0b18ae7c7cf54f0294ad4256af26e0be8ed8b71c18c41`; its `.sha256` file is 110 bytes. Downloaded from the draft: matches its `.sha256` file, GitHub's asset digest and the CI log. Not run outside CI: the smoke test needs ports 8420 and 8765, which a running test environment held | Maintainer's machine (checksums and file list only) |
| Published macOS zip | `MySQL-DBA-Validator-v0.6.0-macos-arm64-unsigned.zip`: 33,695,827 bytes, SHA256 `9eca43facbb4e8567f42acdeab567ad18e057c623399e6b0a20911d6de865f5b`; its `.sha256` file is 118 bytes. Downloaded from the draft: matches its `.sha256` file, GitHub's asset digest and the CI log. Not run outside CI | Maintainer's machine (checksums and file list only) |
| Contents of the built apps | File lists of both downloaded zips, read without extracting: no test files, keys, certificates, `.env` files, registries or caches. Static reading of the PyInstaller module archive in all four executables, without running them: each bundles `pymysql` with `VERSION_STRING` 1.2.3 (the release pin). The bundled sqlglot version was not read | Maintainer's machine |
| Published release | After publication, all four assets have the same sizes and digests as the downloaded draft files | GitHub API |

Not done for v0.6.0:

- the smoke test and the artifact audit of the published zips on the
  maintainer's machine (the CI smoke tests and audits passed);
- a live test against a server that offers no TLS (T4b), MySQL 8.4, or real
  company MySQL, VPN and company-issued TLS; the only live check is the
  connector TLS test against one disposable local MySQL 8.0.45 server
  ([above](#unreleased-release-candidate-checks));
- adding a target through the page by hand in a browser, including its
  failure cases;
- a real-Mac acceptance pass: the macOS zip has been tested in CI only;
- code signing: the Windows zip is unsigned, and the macOS app is ad hoc
  signed only, not notarized.

## v0.5.1 release verification

All from tag `v0.5.1`, commit `d8aef32fc5dc12648bbf576fbb579350e563f0fd`
(`c884f44`, the comment-parsing fix, plus the version bump and documentation;
see [below](#v051-source-commit-comment-parsing-fix-c884f44)). Published as
[v0.5.1](https://github.com/realspinn/mysql-dba-validator/releases/tag/v0.5.1)
on 2026-10-22 at 15:25:47 UTC; GitHub reports it as published (not a draft or
prerelease) and Latest. v0.5.0 is still published, with its tag and asset
digests unchanged.

| Check | Result | Where |
| --- | --- | --- |
| Windows tag run ([39800602988](https://github.com/realspinn/mysql-dba-validator/actions/runs/39800602988)) | Tag matched `APP_VERSION`. Tests 1346 passed, 2 skipped (the log does not name the skipped tests); `pip check` passed; portable folder and zip built; artifact audit passed on both (119 files, 2184 embedded modules, 0 secret values checked); smoke test 55/55; draft release created | CI, Windows 2025, Python 3.14.8, sqlglot 30.24.0 for the tests |
| macOS tag run ([39800602982](https://github.com/realspinn/mysql-dba-validator/actions/runs/39800602982)) | Tag matched `APP_VERSION`. Tests 1347 passed, 1 skipped (the Windows Job Object test, expected); skip check passed (0 unexpected); `pip check` passed; unsigned app and zip built; audits passed on both (112 files, 2220 embedded modules, 0 secret values checked); smoke test 49/49; `publish-unsigned` uploaded the zip to the draft. `sign-notarize`, `verify-signed` and `publish` were skipped | CI, GitHub-hosted Apple Silicon runner, macOS 15.7.9, Python 3.14.7, sqlglot 30.24.0 for the tests |
| Published Windows zip | `MySQL-DBA-Validator-v0.5.1-windows-x64.zip`: 32,937,113 bytes, SHA256 `8fa0e05d3b1bc34c382dcdaf80742ad59ec28da196c24c61ecdf5c8bfe979612`; its `.sha256` file is 110 bytes. Downloaded from the draft: matches its `.sha256` file, GitHub's asset digest and the CI log; smoke test 55/55 | Maintainer's machine |
| Published macOS zip | `MySQL-DBA-Validator-v0.5.1-macos-arm64-unsigned.zip`: 33,653,413 bytes, SHA256 `02a103db80c5b23a6cab06d527dd71dc7842032d12410c582552ea35e1eb8d03`; its `.sha256` file is 118 bytes. Downloaded from the draft: matches its `.sha256` file, GitHub's asset digest and the CI log. Not run outside CI | Maintainer's machine (checksums only) |
| sqlglot and the fix inside the built apps | Static reading of the PyInstaller archives in both downloaded zips, without running them: all four executables embed sqlglot 30.21.0 (the release pin) and contain the comment-parsing fix. The CI tests ran on sqlglot 30.24.0, so the packaged apps and the CI tests used different sqlglot versions | Maintainer's machine |
| Published release | After publication, all four assets have the same sizes and digests as the downloaded draft files | GitHub API |

Not done for v0.5.1:

- the artifact audit of the published zips on the maintainer's machine
  (PyInstaller, which the audit needs, is not installed there; the CI audits
  passed);
- any check against a live MySQL server (v0.5.0 and v0.5.1 changes are covered
  by automated tests with fake connections only);
- a real-Mac acceptance pass: the macOS zip has been tested in CI only;
- code signing: the Windows zip is unsigned, and the macOS app is ad hoc
  signed only, not notarized.

The CI checks and the static archive reading above show what was built and
packaged; they are not live database, real-Mac, signing or notarization
validation. Known limitations of the fix itself (executable comments,
including `mysqldump` output, are refused; MariaDB `/*M! */` comments are not
recognised; partly read statements are still scored from what was read) are in
the [README](README.md#important-limitations) and the evidence contract.

## v0.5.0 release verification

All from tag `v0.5.0`, commit `86eb2994b885e9c573e710148f10c04564c47ead`.

| Check | Result | Where |
| --- | --- | --- |
| Full test suite and `pip check` before tagging | 1313 passed, 4 skipped; `pip check` clean | Maintainer's Windows 10 machine, Python 3.13.5 (the project targets 3.14) |
| Windows tag run (39752902851) | Tests 1315 passed, 2 skipped; artifact audit passed on the folder and the zip (119 files, 2184 embedded modules); smoke test 55/55 | CI, Python 3.14 |
| macOS tag run (39752902827) | Tests 1316 passed, 1 skipped (the job fails on any skip other than the Windows Job Object test); app and zip audits passed; smoke test 49/49; unsigned zip uploaded by `publish-unsigned` | CI, GitHub-hosted Apple Silicon runner, Python 3.14 |
| Published Windows zip | Downloaded from the draft release: 32,931,962 bytes, SHA256 `929b20f3d3eb8fe2a6be7c0948a5d317bc3c17299ffdab50e50cadf0d6467c0a`, matching its `.sha256` file and the CI build log; smoke test 55/55 | Maintainer's machine |
| Published macOS zip | SHA256 `12259af89d8b931f5b78e3b7111376786e56f6a2dfe928b5f980d7cefba1bbd6` agrees in its `.sha256` file, GitHub's asset digest and the CI log. The zip itself was not downloaded or run outside CI | Maintainer's machine (checksums only) |

Not done for v0.5.0:

- the artifact audit of the published zip on the maintainer's machine
  (PyInstaller, which the audit needs, is not installed there; the CI audit
  passed);
- the packaged real-Chrome and credential-boundary checks that were run for
  v0.4.0 (below);
- any check against a live MySQL server (see
  [Evidence contract revision](#evidence-contract-revision-v050)).

## v0.5.1 source commit: comment-parsing fix (`c884f44`)

v0.5.1 is `v0.5.0` plus this fix and a version bump. Commit
`c884f44279a2fa8aa6e3ef9952454319a8445fce` on `main`, 2026-10-22: executable comments
(`/*! ... */`) are refused (`executable_comment_not_supported`), and `--`
before anything other than whitespace or a control character is read as two
minus signs, as MySQL reads it. The connector now decides every whole-request
refusal before opening a connection to the target. These results are not
release results: they are for the source commit, not the `v0.5.1` tag. The
tag runs and the checks of the published files are in
[v0.5.1 release verification](#v051-release-verification).

| Check | Result | Where |
| --- | --- | --- |
| Full test suite and `pip check` | 1344 passed, 4 skipped; `pip check` clean | Maintainer's Windows 10 machine, Python 3.13.5 (the project targets 3.14), sqlglot 30.17.0 (installed) |
| Focused files (batch contract, connector remote evidence, no submitted SQL, local evidence credentials, evidence contract, served page) | 291 passed with sqlglot 30.17.0 and 291 passed with 30.21.0 (the release pin, loaded from a scratch directory) | Same machine |
| New and changed tests against the code before the fix | 21 fail, plus 4 that end in HTTP 500 (`- -` and similar input crashed the parser's SQL writer in v0.5.0) | Same machine; a scratch copy of `v0.5.0` with the new tests |
| Refusal before connection, against the v0.5.0 connector order | 7 fail (a connection was opened before refusing) | Same machine; a scratch copy with only `connector/server.py` from `v0.5.0` |

The local results above were taken on the working tree before the commit; the
committed tree is the same.

**CI on `c884f44`.** Both workflows were run by hand (`workflow_dispatch`) on
`main`. These are validation builds, not release builds: no tag, no draft and
no release was created, and the artifacts are named `v0.5.0` only because
`APP_VERSION` was not changed. They are not the published v0.5.0 assets.

| Check | Windows, run 39794532710 | macOS, run 39794828032 |
| --- | --- | --- |
| Runner, Python | Windows 2025 (GitHub-hosted), Python 3.14.8 | macOS 15.7.9 (GitHub-hosted, Apple Silicon), Python 3.14.7 |
| sqlglot in the test environment | 30.24.0 (`requirements.txt` allows `>=30.17.0,<31.0.0`) | 30.24.0 |
| Test suite | 1346 passed, 2 skipped. The log does not name the skipped tests, and this job has no unexpected-skip check | 1347 passed, 1 skipped (the Windows Job Object test); skip check: 1348 tests, 1 skipped, 0 unexpected |
| `pip check` | Passed | Passed |
| Build and artifact audit | Portable build passed; audit passed on the folder and the zip: 119 files, 2184 embedded modules, 0 secret values checked | Unsigned app build passed; audit passed on the app and the `ditto` zip: 112 files, 2220 embedded modules, 0 secret values checked |
| Smoke test | 55/55 | 49/49 |
| Release, signing, notarization, publishing | "Create draft GitHub Release" skipped | `sign-notarize`, `verify-signed`, `publish-unsigned` and `publish` skipped |

**sqlglot inside the built apps.** The build installs the pinned
`release/requirements-build*.txt` quietly, so the logs do not show the version.
The workflow artifacts were downloaded afterwards, checked against their
`.sha256` files (Windows `4caae239…`, macOS `e1aeba74…`), and their PyInstaller
archives read without running anything: all four executables (both Windows
`.exe` files and both macOS executables) embed `sqlglot._version` 30.21.0 for
Python 3.14, and their `backend.main`/`backend.parser` contain
`executable_comment_not_supported`, `has_executable_comment` and
`mysql_minus_signs`. So the packaged apps use the release pin (30.21.0) while
the CI tests ran on 30.24.0; the full suite has not been run on 30.21.0.

Not shown by these runs: a real-Mac acceptance pass, any live MySQL server,
company infrastructure, or a signed build. All database tests use fake
connections and TestClient.

## v0.4.0 release verification

**CI:** the v0.4.0 tag run passed every step above.

**Maintainer verification of the published zip.** Before publishing, the
exact CI-built `MySQL-DBA-Validator-v0.4.0-windows-x64.zip` was downloaded
from the draft release and checked:

| Check | Result |
| --- | --- |
| Size | 32,896,750 bytes, matching the release asset |
| SHA256 | `dff16b986c68d9ba50c3cf8c8af260e674a1ce8082bd4bb690814384bf98d64d`, matching the published `.sha256` |
| Contents | Both executables report version 0.4.0 and contain the v0.4.0 evidence code; no test modules |
| Artifact audit (with extra forbidden test values) | Passed |
| Smoke test | 55/55 |
| Real Chrome against the packaged Console executable | 26/26 |
| Real Chrome against the packaged windowed executable | Evidence messages correct, 0 console errors |
| Credential-boundary checks | 16/16 on each executable |

The packaged real-Chrome and credential-boundary checks were run by the
maintainer with scripts that are not yet part of this repository, so they
cannot be re-run from a checkout today. The credential-boundary checks start
the packaged app against stand-in listeners on `127.0.0.1` (no MySQL), type a
fake login into the page, and confirm that every connection goes only to the
loopback target, that a decoy `.env` is never used, and that the fake
credentials appear in no output or written file.

**Idle shutdown.** Earlier releases were checked for stopping on their own
after the last page closed. For v0.4.0 the launcher code is unchanged, but the
automated desktop check could not confirm it on the maintainer's machine: in
that environment Chrome kept the closed page alive, and the published v0.4.0
and v0.3.2 builds behaved identically.

## Real MySQL (maintainer)

Local database evidence was run by the maintainer against a real MySQL server
with the Sakila sample database. The MySQL version was not recorded. Paths
exercised:

- `SELECT` with an execution plan and table metadata;
- `UPDATE` and `DELETE` with a `WHERE` clause: no plan on the read-only
  connection, metadata collected, no affected-row count claimed;
- unrestricted `DELETE`;
- a missing table;
- a rejected login and an unavailable connection;
- `SELECT … INTO`;
- the evidence display in the page;
- credential handling (no leakage).

## Remote workflow against AWS RDS for MySQL (maintainer-reported)

Recorded on 2026-10-21, after the v0.4.1 release; it was not part of the
v0.4.1 release verification.

The maintainer created a test database in AWS RDS for MySQL and reports that
the remote/company workflow, which goes through the local connector to an
approved target, was tested successfully end to end on Windows.

| | |
| --- | --- |
| Evidence source | Manual test reported by the maintainer. No transcript, logs or screenshots were provided for this record, so it cannot be re-checked from this repository |
| Client | Windows. The validator version and whether the packaged Console executable or a source checkout was used were not recorded |
| Database | AWS RDS for MySQL, a test database created by the maintainer. The MySQL version, region, instance settings, network path and account privileges were not recorded |
| TLS | The connector refuses a company connection without a configured CA and verifies the certificate and host name (`connector/server.py`), so a successful run implies verified TLS. Which CA bundle was used was not recorded |
| Outcome | Successful, as reported |

What it does not establish:

- other MySQL versions, MySQL-compatible services or Aurora;
- real company infrastructure: company MySQL servers, VPN/LAN connectivity,
  company-issued CAs, proxies or firewalls;
- particular IAM, security-group, network or MySQL privilege configurations;
- which evidence was collected for each statement type, including remote
  `UPDATE`/`DELETE` plans and the positive M1 and M2 adjustments;
- error paths against RDS (rejected logins, unreachable targets, DNS changes,
  certificate failures) and behaviour under production workloads.

## Evidence contract revision (v0.5.0)

The changes in
[docs/v2-database-evidence-contract.md](docs/v2-database-evidence-contract.md)
are released in v0.5.0 with automated tests only (1313 passed, 4 skipped, on
Windows before the release). The live checks marked
**Not run** below are written up as cases, with fixtures, expected results and
cleanup, in
[docs/v2-live-mysql-validation-plan.md](docs/v2-live-mysql-validation-plan.md).

**Execution plans (2026-10-22):** after a one-day removal (2026-10-21), plan
collection for `SELECT` is restored. An eligible `SELECT` is sent to MySQL only as
plain `EXPLAIN <SELECT>` on the confirmed read-only evidence connection; the
statement itself is never executed, no result rows are fetched and `EXPLAIN
ANALYZE` is never used. A plan that MySQL returns in a non-tabular format
(`explain_format` TREE or JSON, MySQL 8.0.32+) is reported as not collected
(`explain_format_unsupported`). Checked with fake connections only
(`tests/test_no_submitted_sql.py`); no live server.

**Live Run A status (local MySQL 8.0.45, 2026-10-21): not completed.** The
maintainer ran the read-only preflight (client and server 8.0.45; binary
logging, Performance Schema and the statement consumers on; InnoDB default; no
anonymous accounts; no `validator_live_test` schema, no `vt_*` accounts, no
replication) and started script 01 by hand. The `GRANT` to `vt_observer`
returned error 1410 (`ER_CANT_CREATE_USER_WITH_GRANT`: in MySQL 8.0 `GRANT`
cannot create an account, so that account did not exist when the `GRANT` ran).
The resulting state of the schema and the three accounts has not been
confirmed. No validation case has been run, so no row of the plan's section 9
is live-verified. The digest checks are blocked until either `vt_observer`
exists with its one grant, or the administrator runs `05` instead (plan
section 2); no wider Performance Schema grant is to be used.

| Check | Result | Environment and evidence source |
| --- | --- | --- |
| Full automated suite, after the pre-commit review fixes, the one-row write floor and the unusual-name fix | 1182 passed, 11 skipped, 4 failed, both with sqlglot 30.17.0 (installed) and with 30.21.0 (the release pin, loaded from a scratch directory) | Windows 10, Python 3.13.5 (the project targets 3.14), maintainer's machine, 2026-10-21. The 4 failures are `test_windows_test_step_fails_when_any_command_fails`, which cannot run there: no `pwsh`, and the execution policy blocks scripts. They pass in CI on `pwsh`. The 11 skips are macOS/POSIX-only tests |
| Review regression tests (function policy, always-true `WHERE`, `EXPLAIN ANALYZE`, read-back, collector boundary, one-row write floor, unusual call names) | 197 passed; 104 of the first 154 fail against the code before the fixes, and 7 of the 32 unusual-name tests fail against the code before that fix | Same; fake MySQL and the installed sqlglot |
| Row-scope acceptance tests (W1–W12) | 59 passed | Same; fake MySQL with explicit schema metadata |
| Read-only boundary tests | 49 passed | Same; fake MySQL. Every `EXPLAIN` sent must pass the final check and the function list |
| Real-Chrome rendering of scope, headlines, warnings and batch confidence | Passed | Same, Google Chrome headless |
| CI tag run, packaged build, audit, smoke test | Passed in the v0.5.0 tag runs (2026-10-22) | CI, Python 3.14; see [v0.5.0 release verification](#v050-release-verification) |
| `SET SESSION TRANSACTION READ ONLY` then `@@SESSION.transaction_read_only` = 1 on MySQL 8.4 | **Not run** | No authorized 8.4 test server |
| `EXPLAIN UPDATE`/`DELETE` refused with error 1792 on a read-only session, 8.4 | **Not run** | v0.3.0's maintainer test observed it on a server whose version was not recorded |
| Unique, composite, prefix and nullable-unique index metadata on 8.4 | **Not run** | |
| Whether `EXPLAIN SELECT` evaluates a stored function on 8.4 | **Not run** | Percona showed it on 8.0.21 and earlier; the validator plans only statements whose calls are on its reviewed built-in list, under a read-only session |
| How 8.4 resolves a stored or loadable function named like a non-MySQL function (`split`, `levenshtein`) and like a listed built-in (`upper`) | **Not run** | The function list relies on the manual's name-resolution rule |
| Whether evaluated SQL (a stored function) can change `transaction_read_only` between statements, and whether the per-plan read-back catches it | **Not run** | Fake servers only |
| Read-only session through a connection-multiplexing proxy (ProxySQL, RDS Proxy) | **Not run** | |
| The same checks on MySQL 8.0 (compatibility only) | **Not run** | |
| Company Mode through the connector against AWS RDS | **Not run for this revision** | The maintainer-reported RDS test (above) used the v0.4.1-era code and is not evidence for these changes |

## Not verified

- Remote/company workflow against real company infrastructure: company MySQL,
  VPN/LAN connectivity, company-issued TLS, and a hosted HTTPS frontend. The
  connector's security rules are covered by automated tests, its process has
  been run locally with a stand-in target, and the maintainer reports one
  successful end-to-end test against an AWS RDS for MySQL test database
  (above).
- The positive M1 and M2 adjustments against a live database (since v0.5.0 they
  need an `UPDATE`/`DELETE` plan, which is no longer requested).
- Every live-server check of the evidence contract revision (below).
- Whether the optimization advice helps on a real server: none of its seven
  rules has been checked against a real MySQL server, and the `DUAL` plan
  refusal (v0.8.0) is tested only with fake servers.
- The update check against the real GitHub API from a running or packaged
  app (only faked responses are tested). On macOS the packaged app may have no
  CA certificates for Python to verify GitHub with, in which case the check is
  always unavailable (it never falls back to an unverified connection); not
  yet checked on a Mac.
- Platforms other than Windows x64 on a real machine. The macOS (Apple
  Silicon) unsigned zips (v0.4.1, v0.5.0, v0.5.1) have been tested only in CI; see below.

## macOS (Apple Silicon), v0.4.1 unsigned: not yet verified on a Mac

These are separate stages; each is reached only when it has actually happened.

| Stage | Status |
| --- | --- |
| 1. Implemented in the repository | Yes: launcher support, `release/build_macos.py`, `release/audit_macos.py`, `release/smoke_test_macos.py`, `.github/workflows/release-macos.yml` |
| 2. Tested on Windows (simulated macOS) | Yes, see below |
| 3. Built, audited and smoke-tested on a GitHub-hosted Apple Silicon runner | Yes, by the workflow's `build-test` job (no credentials needed): run 37799047469 on `main` at `31ad995`, 2026-10-08. Test suite 861 passed, 1 expected skip; app and zip audits passed; smoke test 49/49 on the unsigned arm64 zip. That build is ad hoc signed only and not notarized; it is a workflow artifact, not a release asset |
| 4. Tested by hand on a real Mac | **Not yet** |
| 5. Signed with a Developer ID | **Not yet** (needs an Apple Developer account; not planned until resources allow) |
| 6. Notarized by Apple | **Not yet** |
| 7. Accepted by Gatekeeper on a real Mac | **Not yet** |
| 8. Published as a release | Yes: v0.4.1, v0.5.0 and v0.5.1, the unsigned zip only (ad hoc signed, not notarized), uploaded by the workflow's `publish-unsigned` job from the tag run after its build, audits and smoke test passed |

What the Windows testing (stage 2) covers, and what it does not:

| Check | How it was tested |
| --- | --- |
| Launcher on macOS: console executable found in `Contents/MacOS`, backend tied to the launcher by a pipe, startup alert built without script injection | Unit tests on Windows with the platform simulated. The pipe mechanism is also tested across real processes (killing a launcher process stops its child), which uses the same OS pipe semantics but on Windows |
| Windows launcher behaviour unchanged | Existing Windows tests pass unchanged; the Windows packaged smoke test (55 checks) passes with the new launcher |
| Signing order and `codesign`/`notarytool` commands, no entitlements, credentials never echoed | Unit tests on Windows (no signing performed) |
| macOS audit rules (bundle structure, version, page, signing-material names, hidden items, physical symlink containment) | Unit tests on synthetic bundles on Windows; the symbolic-link tests are skipped on Windows and run on the macOS runner |
| Workflow trust boundary (no secrets in `build-test`, signing gated and isolated) | Unit tests on the workflow file |
| Every pinned dependency has a CPython 3.14 macOS arm64 (or universal2) wheel | Checked against PyPI; no source builds needed |
| Building a real `.app`, running it, macOS test suite | Not covered by Windows testing; done in stage 3 |

On the macOS runner the test suite runs in full; the job fails if any test
skips other than the Windows Job Object test (`release/check_pytest_skips.py`),
so the real-Chrome rendering tests and the symbolic-link tests must run there.

The smoke test runs the app's executables directly. It does not go through
LaunchServices (opening the app from Finder), so it cannot show first-launch
Gatekeeper behaviour or what happens when a running app is opened again, and it
only detects (does not assert) the on-screen startup alert. Those are in the
real-Mac pass below.

### Required real-Mac acceptance pass

CI runs on a clean virtual Mac and cannot show what a user sees. A maintainer
runs these on a real Apple Silicon Mac. None has been done yet.

With the unsigned CI build (stage 4): download the `macos-arm64-unsigned-<commit>`
artifact from the workflow run, check its SHA256, unzip it with
`ditto -x -k <zip> <folder>`, and move the app to Applications. macOS blocks an
unsigned app that was downloaded; allow it once in System Settings > Privacy &
Security ("Open Anyway"). A tagged release can carry this unsigned zip (see
RELEASING.md); it is not notarized, and passing CI does not mean any of these
real-Mac checks were done.

- [ ] SHA256 of the download matches its `.sha256` file.
- [ ] Opening the app opens `http://127.0.0.1:8420` in the default browser;
      static validation works.
- [ ] No window and no Dock icon.
- [ ] What happens when the app is opened again while it runs (LaunchServices
      does not start a second copy); record the behaviour. The page itself is
      always at `http://127.0.0.1:8420`.
- [ ] A startup error is visible: with port 8420 taken by another program, an
      alert names the problem.
- [ ] Closing the page: the app stops on its own about 15 minutes later, and
      port 8420 is released.
- [ ] Console workflow from Terminal (`.../Contents/MacOS/MySQL-DBA-Validator
      Console`): pairing code shown, operator commands work, the page pairs, a
      browser session cannot manage targets, company operations fail closed
      without `--tls-ca`.
- [ ] Closing that Terminal window, and Ctrl+C in it, both stop the web app
      too (port 8420 released, no leftover process).
- [ ] Local MySQL evidence against a MySQL server on the Mac: `SELECT` with
      plan and metadata, `UPDATE`/`DELETE` with metadata and the read-only plan
      message, a rejected login.
- [ ] The typed MySQL password appears in no file under
      `~/.local/state/MySQLDBAValidator` and in no output.
- [ ] Record the macOS version used.

Only with a signed and notarized build (stages 5 to 7):

- [ ] The downloaded `.dmg` carries the quarantine attribute
      (`xattr -p com.apple.quarantine <dmg>`); opening it and the app shows the
      normal first-launch dialog for a notarized app, with no "unidentified
      developer" or "damaged" warning and nothing to allow in System Settings.
- [ ] `spctl --assess --type exec -vvv "/Applications/MySQL DBA Validator.app"`
      reports `accepted` and `source=Notarized Developer ID`.

## Reproducing the checks

```powershell
python -m pytest -q
pip check
python release\build_windows.py
python release\smoke_test_artifact.py dist\MySQL-DBA-Validator-v<version>-windows-x64.zip
```

`build_windows.py` runs the artifact audit on the built folder and on the
zip. To audit a downloaded release zip, run the audit with the build
environment that `build_windows.py` creates, because it uses PyInstaller to
read the executables:

```powershell
build\release\venv\Scripts\python.exe release\audit_artifact.py <path-to-zip>
```

The smoke test needs ports 8420 and 8765 free. Details are in
[RELEASING.md](RELEASING.md).
