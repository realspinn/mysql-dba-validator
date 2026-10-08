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

## Current status (v0.4.0)

| Area | Status |
| --- | --- |
| Parser, risk engine, scoring adjustments, API, evidence contract, connector security rules | Automated tests: 770 passed, 0 skipped (maintainer run at the v0.4.0 release commit; the CI tag run also passed the suite). |
| Results page rendering of every evidence state | Real headless Chrome, driven by real API responses (part of the test suite) |
| Local web app and connector together in a browser | Real headless Chrome, with company MySQL replaced by a stand-in |
| Windows portable release | Built by CI from the release tag, audited, smoke-tested from the extracted zip; the published v0.4.0 zip re-verified by the maintainer (below) |
| Local database evidence against real MySQL | **Verified by the maintainer** with the Sakila sample database (below). Also covered by automated tests with a fake MySQL that models MySQL's read-only behaviour (error 1792) |
| Remote/company MySQL, VPN/LAN connectivity, company TLS, hosted HTTPS frontend | **Not verified** |
| Positive M1 metadata adjustment against a live large table | **Not verified.** It needs a collected `UPDATE`/`DELETE` plan, which only the remote connector path collects. Automated tests only |

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
- **Page and packaging:** `test_frontend_served_page.py`,
  `test_frontend_evidence_rendering.py` (real Chrome; skipped if Chrome is not
  installed), `test_heartbeat.py`, `test_release_packaging.py`.

The GitHub Actions workflow runs the suite only for release tags, before the
build. It does not run on pushes or pull requests, so contributors run it
locally.

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

## Not verified

- Remote/company MySQL through the connector, VPN/LAN connectivity, company
  TLS, and a hosted HTTPS frontend. The connector's security rules are covered
  by automated tests and its process has been run locally with a stand-in
  target, but not against real company infrastructure.
- Remote `UPDATE`/`DELETE` plans and the positive M1 and M2 adjustments
  against a live database.
- Platforms other than Windows x64 on a real machine. The v0.4.1 macOS (Apple
  Silicon) unsigned zip has been tested only in CI; see below.

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
| 8. Published as a release | Yes: v0.4.1, the unsigned zip only (ad hoc signed, not notarized), uploaded by the workflow's `publish-unsigned` job from the tag run after its build, audits and smoke test passed |

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
