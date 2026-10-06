"""Audit a built release (folder or .zip) for content that must never ship.

    python release/audit_artifact.py dist/MySQL-DBA-Validator-v0.2.0-windows-x64.zip
        [--env-file .env] [--forbid TEXT ...]

Checks, failing (exit 1) on any finding:

* forbidden files: .env files, private keys/certificates, connector registry
  files, logs, databases, __pycache__/.pyc outside the bundle, tests, venvs,
  pytest artifacts, the legacy page;
* required files: both executables (windowed and console) and the served page;
* content, scanned in every file AND inside each executable's embedded archives
  (PyInstaller PKG + PYZ, decompressed):
  - secret values taken from the developer's .env (keys containing PASSWORD,
    SECRET, TOKEN or KEY), plus any --forbid strings;
  - PEM private-key blocks;
  - machine-specific paths: the build user's home folder and this source checkout.
    One narrow exception: inside a compiled .pyd/.dll, a home-folder path
    immediately followed by .cargo/registry/ (either separator; upstream Rust
    wheel build path).

Secret values are never printed; findings name the key and the file only.
Variable names such as ``password`` in source code are not findings.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE_NAME = "MySQL-DBA-Validator.exe"  # windowed app
CONSOLE_EXE_NAME = "MySQL-DBA-Validator Console.exe"  # console app with the connector
EXE_NAMES = (EXE_NAME, CONSOLE_EXE_NAME)
REQUIRED = (*EXE_NAMES, "_internal/frontend/index.html")

FORBIDDEN_NAME_PATTERNS = [
    (re.compile(r"(^|/)\.env(\..*)?$", re.I), ".env file"),
    (re.compile(r"\.(pem|key|pfx|p12|crt|cer|der|jks|kdbx)$", re.I), "key/certificate file"),
    (re.compile(r"(^|/)connector-targets[^/]*\.json$", re.I), "connector target registry"),
    (re.compile(r"\.(log|sqlite|sqlite3|db)$", re.I), "log or database file"),
    (re.compile(r"(^|/)(\.venv|venv|\.pytest_cache|test_clean_env|tests|\.git|\.claude|\.vscode)(/|$)", re.I),
     "development directory"),
    (re.compile(r"(^|/)(test_[^/]*\.py|conftest\.py|pytest\.ini)$", re.I), "test file"),
    (re.compile(r"(^|/)index_legacy\.html$", re.I), "legacy page"),
    (re.compile(r"(^|/)(CLAUDE\.md|ARCHITECTURE_DESIGN_V2\.md|requirements\.txt)$", re.I), "source-tree document"),
]
# Public CA bundles shipped by well-known libraries are not secrets.
ALLOWED_NAME_EXCEPTIONS = [re.compile(r"(^|/)certifi/cacert\.pem$", re.I)]
# Our own modules must not be test code, and test frameworks must not be bundled.
FORBIDDEN_MODULE_PREFIXES = ("tests", "test_qa", "pytest", "_pytest", "httpx", "httpx2")

# A header followed by real base64 key material. Libraries such as cryptography
# contain the bare header text as a parsing constant; that is not a key.
PRIVATE_KEY_RE = re.compile(rb"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----\s*[A-Za-z0-9+/=\r\n]{64,}")
SECRET_KEY_RE = re.compile(r"PASSWORD|SECRET|TOKEN|KEY", re.I)

# Rust wheels from PyPI (cryptography, pydantic-core, watchfiles) are built on
# GitHub-hosted runners and embed C:\Users\runneradmin\.cargo\registry\... source
# paths for panic messages. On our own GitHub runner the home folder is that same
# path, so a home-folder hit in a compiled .pyd/.dll is allowed only when it is
# immediately followed by .cargo\registry\. Any other occurrence still fails.
HOME_LABEL = "build user's home folder"
COMPILED_SUFFIXES = (".pyd", ".dll")
UPSTREAM_CARGO_SUFFIX = re.compile(rb"[\\/]+\.cargo[\\/]+registry[\\/]")  # UTF-8 only; UTF-16 stays strict


def read_env_secrets(env_file: Path) -> list[tuple[str, str]]:
    secrets = []
    if not env_file.is_file():
        return secrets
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if SECRET_KEY_RE.search(key) and len(value) >= 4:
            secrets.append((f".env {key}", value))
    return secrets


def machine_paths() -> list[tuple[str, str]]:
    paths = []
    home = Path.home()
    paths.append((HOME_LABEL, str(home)))
    paths.append(("source checkout path", str(ROOT)))
    short_home = os.environ.get("USERPROFILE", "")
    if short_home and short_home.lower() != str(home).lower():
        paths.append((HOME_LABEL, short_home))
    return paths


def iter_files(target: Path):
    """Yield (relative posix path, bytes) for every file in a folder or zip, minus the top folder."""
    if target.is_file() and target.suffix.lower() == ".zip":
        with zipfile.ZipFile(target) as archive:
            names = [n for n in archive.namelist() if not n.endswith("/")]
            tops = {n.split("/", 1)[0] for n in names}
            strip = len(tops) == 1
            for name in sorted(names):
                rel = name.split("/", 1)[1] if strip and "/" in name else name
                yield rel, archive.read(name)
    else:
        for path in sorted(p for p in target.rglob("*") if p.is_file()):
            yield path.relative_to(target).as_posix(), path.read_bytes()


def embedded_entries(exe_bytes: bytes):
    """Yield (entry name, decompressed bytes) for the PyInstaller archives inside the executable."""
    import tempfile

    from PyInstaller.archive.readers import CArchiveReader

    with tempfile.TemporaryDirectory() as tmp:
        exe_path = Path(tmp) / EXE_NAME
        exe_path.write_bytes(exe_bytes)
        pkg = CArchiveReader(str(exe_path))
        for name, entry in pkg.toc.items():
            typecode = entry[-1]
            if typecode == "z":
                pyz = pkg.open_embedded_archive(name)
                for module in pyz.toc:
                    data = pyz.extract(module, raw=True)
                    if data:
                        yield f"PYZ:{module}", data
            else:
                yield f"PKG:{name}", pkg.extract(name)


def needles_for(text: str) -> list[bytes]:
    lowered = text.lower()
    variants = {lowered, lowered.replace("\\", "/"), lowered.replace("\\", "\\\\")}
    out = []
    for value in variants:
        out.append(value.encode("utf-8"))
        out.append(value.encode("utf-16-le"))
    return out


def contains_needle(lowered: bytes, needles: list[bytes], allow_upstream_cargo: bool) -> bool:
    """True if any needle occurs; with allow_upstream_cargo, occurrences followed by .cargo\\registry\\ don't count."""
    for needle in needles:
        start = lowered.find(needle)
        while start != -1:
            end = start + len(needle)
            if not (allow_upstream_cargo and UPSTREAM_CARGO_SUFFIX.match(lowered, end)):
                return True
            start = lowered.find(needle, start + 1)
    return False


def audit(target: Path, env_file: Path, forbid: list[str]) -> tuple[list[str], dict]:
    findings: list[str] = []
    secrets = read_env_secrets(env_file) + [(f"--forbid #{i + 1}", v) for i, v in enumerate(forbid)]
    content_rules = [(label, needles_for(value), True) for label, value in secrets]
    content_rules += [(label, needles_for(value), False) for label, value in machine_paths()]

    seen: set[str] = set()
    stats = {"files": 0, "bytes": 0, "embedded_modules": 0, "top_level": set()}
    for rel, data in iter_files(target):
        seen.add(rel)
        stats["files"] += 1
        stats["bytes"] += len(data)
        stats["top_level"].add(rel.split("/", 1)[0])
        if not any(rx.search(rel) for rx in ALLOWED_NAME_EXCEPTIONS):
            for rx, why in FORBIDDEN_NAME_PATTERNS:
                if rx.search(rel):
                    findings.append(f"forbidden file ({why}): {rel}")
        if "__pycache__/" in rel:
            findings.append(f"forbidden file (__pycache__): {rel}")

        blobs = [(rel, data)]
        if rel in EXE_NAMES:
            for name, blob in embedded_entries(data):
                blobs.append((f"{rel}!{name}", blob))
                if name.startswith("PYZ:"):
                    stats["embedded_modules"] += 1
                    module = name[4:]
                    if module.split(".")[0] in FORBIDDEN_MODULE_PREFIXES:
                        findings.append(f"forbidden module bundled: {module}")
        for where, blob in blobs:
            lowered = blob.lower()
            if PRIVATE_KEY_RE.search(blob):
                findings.append(f"PEM private key block in {where}")
            compiled = where.lower().endswith(COMPILED_SUFFIXES)
            for label, needles, _secret in content_rules:
                if contains_needle(lowered, needles, compiled and label == HOME_LABEL):
                    findings.append(f"{label} found in {where}")

    for required in REQUIRED:
        if required not in seen:
            findings.append(f"required file missing: {required}")
    stats["top_level"] = sorted(stats["top_level"])
    stats["secret_values_checked"] = len(secrets)
    return findings, stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", type=Path, help="release folder or .zip")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env",
                        help="developer .env whose secret values must not appear (default: ./.env)")
    parser.add_argument("--forbid", action="append", default=[], metavar="TEXT",
                        help="additional literal that must not appear (e.g. a test password)")
    args = parser.parse_args(argv)

    findings, stats = audit(args.target, args.env_file, args.forbid)
    print(f"audit: {args.target.name}: {stats['files']} files, {stats['bytes'] / 1e6:.1f} MB uncompressed, "
          f"{stats['embedded_modules']} embedded modules, {stats['secret_values_checked']} secret values checked")
    print("audit: top level: " + ", ".join(stats["top_level"]))
    for finding in findings:
        print("FINDING " + finding)
    print("audit: " + ("FAILED" if findings else "PASSED"))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
