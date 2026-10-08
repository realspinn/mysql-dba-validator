# Releasing

Release binaries are published as GitHub Release assets. They are never
committed to the repository.

## What a release contains

`MySQL-DBA-Validator-v<version>-windows-x64.zip` is a portable build: extract
it and run it, with no installer. Its matching `.zip.sha256` file holds the
checksum. The zip has one top-level folder:

```text
MySQL-DBA-Validator-v<version>-windows-x64/
  MySQL-DBA-Validator.exe           windowed launcher: web app only, idle shutdown
  MySQL-DBA-Validator Console.exe   console launcher: web app + local connector
  README.txt                user instructions (from release/README-WINDOWS.txt)
  LICENSE.txt               if the repository has a LICENSE
  _internal/                Python runtime and dependencies, plus the web page:
    frontend/index.html     the served page
    frontend/assets/        favicon.svg (the README logo mark is not shipped)
```

The executable's Windows icon comes from `release/mysql-dba-validator.ico` and is
embedded in the `.exe`; it is not a separate file in the zip.

The build is a PyInstaller "onedir" build of `release/launcher.py`, using the
spec `release/mysql-dba-validator.spec`. The launcher starts the FastAPI app
as a window-less child process. That child is tied to the launcher through a
Windows Job Object, so it stops when the launcher does. The launcher then runs
the existing connector launcher in the foreground of the console.

## Version

`backend/version.py` (`APP_VERSION`) is the single version source. The API
reports it, and the artifact name is built from it. The connector keeps its
own protocol version (`CONNECTOR_VERSION`). Use `vX.Y.Z` tags that match
`APP_VERSION`.

## Build locally

Requirements: Windows x64, 64-bit Python 3.14, and network access to PyPI.

```powershell
python -m pytest -q
python release\build_windows.py
python release\smoke_test_artifact.py dist\MySQL-DBA-Validator-v<version>-windows-x64.zip
```

`build_windows.py` does the following:

1. Deletes `build\release\` and `dist\`.
2. Creates an isolated venv and installs the exact pins in
   `release/requirements-build.txt`, which contain no test packages.
3. Runs PyInstaller.
4. Adds the user README.
5. Runs `release/audit_artifact.py` on the folder.
6. Zips the folder, writes the SHA256 file and audits the zip.

The build is not bit-for-bit reproducible. Two builds from the same commit and
pinned inputs produce the same file set, and every file except three is
byte-identical. The three that differ are the two executables, which
PyInstaller regenerates on each build, and one `*.dist-info/RECORD` file written
by pip. So the zip's SHA256 changes on every build. Always verify a downloaded
release against the `.sha256` file published with that release.

The audit fails the build on any of these:

- `.env` files, keys or certificates, connector registries, logs, databases,
  tests, venvs or source-tree documents;
- private-key material;
- the build machine's home or checkout path;
- the values of secret-like keys in your local `.env`.

One narrow exception applies to the home-path check. Some PyPI wheels with
compiled Rust code embed their own build path, `C:\Users\runneradmin\.cargo\registry\...`.
`runneradmin` is also the home folder on GitHub-hosted runners. Inside a compiled
`.pyd` or `.dll` only, a home-folder match immediately followed by
`.cargo\registry\` is not a finding. Every other home-path match still fails,
including in those same files.

The audit inspects the executable's embedded Python archive as well as the
loose files. Pass extra literals that must not ship with `--forbid`.

`smoke_test_artifact.py` extracts the zip into a path containing spaces and
runs the executable without Python on `PATH`. It checks the page, the API,
static validation, the connector, origin rules, pairing and the approved-target
list. It also checks that company operations fail closed without a CA, that
credentials do not leak, that a graceful stop, restart and hard kill work, and
that the program folder is never written to. It needs ports 8420 and 8765 free
and DNS for `example.com`, which serves as a stand-in target.

When `requirements.txt` changes, regenerate `release/requirements-build.txt`
from a fresh venv with `pip freeze`, then run the full test suite against
those versions.

## Publish

1. Update `APP_VERSION`, then run the tests, build and smoke test.
2. Tag and push the version, for example `git tag v0.2.0` then
   `git push origin v0.2.0`.
3. The **Windows portable release** workflow
   (`.github/workflows/release-windows.yml`) checks that the tag matches
   `APP_VERSION`, runs the tests, builds and audits the zip, smoke-tests it,
   and attaches the zip and checksum to a **draft** release. The audit and smoke
   test are release gates: if either fails, no draft is created, and the run
   uploads only a diagnostic manifest (file names, sizes, SHA256 hashes and the
   audit output), never the failed binaries. The same tag also starts the
   macOS workflow, which builds and tests the macOS app and adds it to that
   draft: the unsigned zip while signing is off, or, once signing is enabled,
   only the signed disk image (see [macOS](#macos-apple-silicon)). It never
   creates a release of its own.
4. Download the draft's zip and verify it before publishing: check the
   checksum, run the audit against it with your local `.env`, and run the
   smoke test.
5. Add release notes that include the verification status, then publish the
   draft.

## macOS (Apple Silicon)

Prepared for v0.4.1 and **not released**. The macOS status, step by step
(see [VERIFICATION.md](VERIFICATION.md) for what each has actually shown):

1. Implemented: launcher support, build, audit, smoke test and workflow are in
   the repository.
2. Tested on Windows: unit tests that simulate the macOS-specific parts.
3. Built, audited and smoke-tested on a GitHub-hosted Apple Silicon runner:
   **yes**, as the unsigned validation build below (ad hoc signed only, not
   notarized; a CI run on a virtual Mac, not a test on a real Mac).
4. Tested by hand on a real Mac: **not yet**.
5. Signed with a Developer ID: **not yet** (needs an Apple Developer account).
6. Notarized by Apple: **not yet**.
7. Accepted by Gatekeeper on a real Mac: **not yet**.
8. Published as a release: **not yet**.

### Unsigned validation build (no Apple account needed)

`.github/workflows/release-macos.yml`, job `build-test`, runs on a
GitHub-hosted Apple Silicon runner (`macos-15`) for every `v*` tag and by hand
(`workflow_dispatch`). It uses no secrets and has read-only access to the
repository:

1. checks that a tag matches `APP_VERSION`;
2. runs the test suite, then `release/check_pytest_skips.py`, which fails the
   job if any test skipped that is not expected to skip on macOS (only the
   Windows Job Object test is);
3. `python release/build_macos.py build`: an isolated venv with the exact pins in
   `release/requirements-build-macos.txt` (wheels only), PyInstaller with
   `release/mysql-dba-validator-macos.spec` (arm64 only), then
   `release/audit_macos.py` on the app;
4. `python release/build_macos.py package-unsigned`: a `ditto` zip of the app
   (which keeps the bundle's symbolic links and permissions) and its `.sha256`;
5. `release/audit_macos.py` and `release/smoke_test_macos.py` on that zip;
6. keeps the zip as a workflow artifact named
   `macos-arm64-unsigned-<commit>` for 14 days.

The unsigned app is only ad hoc signed (PyInstaller does that; Apple Silicon
needs a signature to run anything) and is not notarized, so Gatekeeper does not
open it normally once downloaded. To try it on a Mac for the acceptance pass,
see [VERIFICATION.md](VERIFICATION.md).

### Unsigned release (while signing is off)

For a `v*` tag, while `MACOS_SIGNING_ENABLED` is not `true`, the job
`publish-unsigned` runs after `build-test` has passed. It uses no secrets. It
downloads that job's artifact and checks it. The zip must be named for the tag
(`MySQL-DBA-Validator-v<version>-macos-arm64-unsigned.zip`), and its `.sha256`
must name exactly that zip and match it. It then waits for the Windows
workflow's **draft** release for the same tag, and uploads only those two files
without `--clobber`. If there is no draft for the tag (missing or already
published), it uploads nothing and fails. A published release, such as v0.4.0,
never receives it. Before publishing the draft, review the zip like the Windows
one, and say in the release notes that the macOS app is Apple Silicon only,
unsigned (ad hoc signed only) and not notarized, and say which real-Mac checks
in [VERIFICATION.md](VERIFICATION.md) have and have not been done. Include these
steps for opening it:

1. Check the download: `shasum -a 256 -c <zip>.sha256` in the download folder.
2. Unzip it with `ditto -x -k <zip> <folder>` (or by double-clicking it), and
   move `MySQL DBA Validator.app` to Applications.
3. Open the app. macOS blocks it, because it is not notarized. In System
   Settings > Privacy & Security, choose "Open Anyway" once, then open it again.

Developer ID signing and notarization remain the future path below.

The app bundle:

```text
MySQL DBA Validator.app
  Contents/MacOS/MySQL-DBA-Validator           windowed app: web app only, idle shutdown
  Contents/MacOS/MySQL-DBA-Validator Console   console app: web app + local connector (run from Terminal)
  Contents/Resources/frontend/...              the served page and favicon
```

It is built from the same `release/launcher.py` as the Windows release. The app
has no Dock icon (`LSUIElement`). There is no Job Object on macOS: the backend
child instead watches a pipe from the launcher and stops when the launcher
exits, however it exits. The data folder is the connector's existing default,
`~/.local/state/MySQLDBAValidator` (or `$XDG_STATE_HOME/MySQLDBAValidator`).
The bundle identifier is `io.github.realspinn.mysql-dba-validator`; it is the
app's long-term identity and must not change once a signed build has shipped.
The app declares a minimum of macOS 11, but the minimum supported version has
not been independently verified.

### Signed distribution (future; needs an Apple Developer account)

Not in use. The workflow's `sign-notarize`, `verify-signed` and `publish` jobs
are skipped unless the run is for a `v*` tag **and** the repository variable
`MACOS_SIGNING_ENABLED` is `true`. To enable them later:

1. A **Developer ID Application** certificate, exported with its private key as
   a `.p12`, and an **App Store Connect API key** (`.p8`) that can use the
   notary service, with its key ID and issuer ID.
2. A GitHub **environment** named `macos-release`, with required reviewers, and
   these environment secrets: `MACOS_CERT_P12_BASE64` (the `.p12`, base64),
   `MACOS_CERT_PASSWORD`, `MACOS_SIGNING_IDENTITY` (`Developer ID Application:
   <name> (<team id>)`), `NOTARY_API_KEY_P8` (the `.p8` contents),
   `NOTARY_API_KEY_ID`, `NOTARY_API_ISSUER_ID`. None of these may ever be
   committed or placed in the repository.
3. The repository variable `MACOS_SIGNING_ENABLED` set to `true`.

The signed path keeps the signing credentials away from third-party code:

- `sign-notarize` (the only job with the secrets; actions pinned to commit
  SHAs) takes the **already tested** unsigned app from `build-test`, checks its
  checksum, and uses only the Python standard library and Apple's tools: no
  `pip install`, no tests, and it never runs the app. It signs every Mach-O file
  inside out, then the executables and the app, with the Developer ID identity,
  Hardened Runtime and a secure timestamp, and **no entitlements**; notarizes the
  app (as a `ditto` zip) and staples it; builds and signs the disk image;
  notarizes and staples that; then **removes the keychain and key files** before
  anything else, and checks `codesign`, `spctl` (`Notarized Developer ID`),
  `stapler validate` and `hdiutil verify`.
- `verify-signed` (no secrets) runs the smoke test and the audit on the signed
  disk image.
- `publish` (no signing secrets; the only job that can write releases) waits for
  the **draft** release that the Windows workflow creates for the tag and
  uploads the disk image and its checksum. It never creates a release and never
  replaces an asset.

The release then contains `MySQL-DBA-Validator-v<version>-macos-arm64.dmg`
(the stapled app, `README.txt` from `release/README-MACOS.txt`, `LICENSE.txt`
and a link to `/Applications`) and its `.dmg.sha256`. Before publishing,
download the `.dmg` from the draft, check its SHA256, read the notary logs kept
with the workflow run, and run the real-Mac acceptance pass in
[VERIFICATION.md](VERIFICATION.md). Notarization means Apple's automated
service found no known malicious content and no code-signing problems; it is
not a review of the tool.

## Not done yet

- No installer. A future installer would be named
  `MySQL-DBA-Validator-v<version>-windows-x64-setup.exe`.
- No Windows code signing.
- No Intel (x86_64) or universal macOS build, and no Linux build.
