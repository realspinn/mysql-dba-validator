# Releasing

Release binaries are published as GitHub Release assets. They are never
committed to the repository.

## What a release contains

`MySQL-DBA-Validator-v<version>-windows-x64.zip` is a portable build: extract
it and run it, with no installer. Its matching `.zip.sha256` file holds the
checksum. The zip has one top-level folder:

```text
MySQL-DBA-Validator-v<version>-windows-x64/
  MySQL-DBA-Validator.exe   console launcher: web app + local connector
  README.txt                user instructions (from release/README-WINDOWS.txt)
  LICENSE.txt               if the repository has a LICENSE
  _internal/                Python runtime, dependencies and frontend/index.html
```

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
pinned inputs produce the same file set, and every file except two is
byte-identical. The two that differ are `MySQL-DBA-Validator.exe`, which
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
   audit output), never the failed binaries.
4. Download the draft's zip and verify it before publishing: check the
   checksum, run the audit against it with your local `.env`, and run the
   smoke test.
5. Add release notes that include the verification status, then publish the
   draft.

## Not done yet

- No installer. A future installer would be named
  `MySQL-DBA-Validator-v<version>-windows-x64-setup.exe`.
- No code signing.
- No macOS or Linux builds.
