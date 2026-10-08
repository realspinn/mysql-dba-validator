"""Audit the macOS build (the .app bundle, its .zip, or the .dmg) for content that must never ship.

    python release/audit_macos.py "dist/macos/MySQL DBA Validator.app" [--version X.Y.Z]
    python release/audit_macos.py dist/macos/MySQL-DBA-Validator-v<version>-macos-arm64-unsigned.zip
    python release/audit_macos.py dist/macos/MySQL-DBA-Validator-v<version>-macos-arm64.dmg
        [--env-file .env] [--forbid TEXT ...] [--forbid-env VARIABLE ...]

Uses the rules of release/audit_artifact.py (the Windows audit) and adds what is
specific to a macOS bundle. Fails (exit 1) on any finding:

* bundle structure: Info.plist names the windowed executable, the expected version
  and LSUIElement; Contents/MacOS holds exactly the two executables (as Mach-O
  files); the served page and its favicon are present and identical to the source
  tree's frontend files; the embedded app version matches;
* no symbolic link leaves the bundle: every link is resolved physically (through
  chains of links) and must stay inside it; absolute link targets are rejected (in
  a .dmg, only the conventional Applications link to /Applications is allowed next
  to the app);
* the top level of a .dmg holds only the app, its README and licence, the
  Applications link and the volume metadata hdiutil itself creates; any other
  item, hidden or not, is a finding;
* forbidden files, as in the Windows audit, plus signing material: .p12, .p8,
  keychains, provisioning profiles, certificate signing requests; and source files
  of this project;
* content of every file and of both executables' embedded archives: secret values
  (the developer .env, --forbid strings and the values of --forbid-env variables),
  PEM private-key blocks, machine-specific paths, and bundled test or development
  HTTP-client modules.

A .dmg is mounted read-only with hdiutil and a .zip is unpacked with ditto (both
macOS only). Secret values are never printed; --forbid-env reads them from the
environment so they never appear on a command line.

What this does not prove: the value checks find only the literal values they are
given (and PEM-armoured private keys). A key or certificate stored in some other
encoding under an innocent file name would not be found. Keeping signing material
out of the build is therefore the job of the release workflow's separation (the
build never runs where signing credentials exist); this audit is a backstop.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release import audit_artifact as base  # noqa: E402  (shared rules)

APP_NAME = "MySQL DBA Validator.app"
MAIN_EXE = "MySQL-DBA-Validator"
CONSOLE_EXE = "MySQL-DBA-Validator Console"
EXECUTABLES = (f"Contents/MacOS/{MAIN_EXE}", f"Contents/MacOS/{CONSOLE_EXE}")
FRONTEND_FILES = {
    "Contents/Resources/frontend/index.html": ROOT / "frontend" / "index.html",
    "Contents/Resources/frontend/assets/favicon.svg": ROOT / "frontend" / "assets" / "favicon.svg",
}
DMG_ALLOWED_TOP_LEVEL = {APP_NAME, "README.txt", "LICENSE.txt", "Applications"}
# Volume metadata that macOS itself may create on a mounted disk image; nothing else
# at the top level is skipped, hidden or not.
DMG_VOLUME_METADATA = {".fseventsd", ".Trashes", ".Spotlight-V100", ".DS_Store"}

MACOS_FORBIDDEN_NAME_PATTERNS = [
    (re.compile(r"\.(p8|keychain|keychain-db|mobileprovision|provisionprofile|certsigningrequest|csr)$", re.I),
     "signing material"),
    (re.compile(r"(^|/)AuthKey_[^/]*$", re.I), "App Store Connect API key"),
    (re.compile(r"\.xcarchive(/|$)", re.I), "Xcode archive"),
    # This project's own source must ship only inside the embedded archive, compiled.
    (re.compile(r"(^|/)(backend|connector|release|tests)/[^/]+\.py$", re.I), "project source file"),
]
MACHO_MAGICS = {bytes.fromhex(m) for m in ("feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca")}
COMPILED_SUFFIXES = (".so", ".dylib")


def read_version() -> str:
    text = (ROOT / "backend" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise SystemExit("cannot read APP_VERSION from backend/version.py")
    return match.group(1)


def is_macho(data: bytes) -> bool:
    return data[:4] in MACHO_MAGICS


def embedded_entries(data: bytes):
    """The executable's PyInstaller archives (shared with the Windows audit; needs PyInstaller)."""
    return base.embedded_entries(data)


def walk_bundle(app: Path):
    """Yield (relative posix path, Path, is_symlink) for every file and link, never following links."""
    for folder, dirnames, filenames in os.walk(app, followlinks=False):
        here = Path(folder)
        for name in sorted(dirnames):
            path = here / name
            if path.is_symlink():
                yield path.relative_to(app).as_posix(), path, True
        for name in sorted(filenames):
            path = here / name
            yield path.relative_to(app).as_posix(), path, path.is_symlink()
        dirnames[:] = sorted(d for d in dirnames if not (here / d).is_symlink())


def link_stays_inside(app: Path, link: Path) -> bool:
    """True if ``link`` resolves physically, through any chain of links, to a place inside ``app``.

    The target is resolved with realpath, not lexically: "b/.." where b is itself a
    link to the bundle root would look inside lexically but lands outside. Absolute
    targets are rejected outright, as a bundle must not depend on outside paths.
    """
    if os.path.isabs(os.readlink(link)):
        return False
    resolved = os.path.realpath(link)
    root = os.path.realpath(app)
    return resolved == root or resolved.startswith(root + os.sep)


def content_rules(env_file: Path, forbid: list[str], forbid_env: list[str], findings: list[str]):
    secrets = base.read_env_secrets(env_file) + [(f"--forbid #{i + 1}", v) for i, v in enumerate(forbid)]
    for name in forbid_env:
        value = os.environ.get(name, "")
        if len(value) >= 4:
            secrets.append((f"--forbid-env {name}", value))
        else:
            findings.append(f"--forbid-env {name} is not set (or too short to check)")
    rules = [(label, base.needles_for(value)) for label, value in secrets]
    rules += [(label, base.needles_for(value)) for label, value in base.machine_paths()]
    return rules, len(secrets)


def scan(where: str, blob: bytes, rules, compiled: bool, findings: list[str]) -> None:
    if base.PRIVATE_KEY_RE.search(blob):
        findings.append(f"PEM private key block in {where}")
    lowered = blob.lower()
    for label, needles in rules:
        if base.contains_needle(lowered, needles, compiled and label == base.HOME_LABEL):
            findings.append(f"{label} found in {where}")


def forbidden_name(rel: str) -> str | None:
    if any(rx.search(rel) for rx in base.ALLOWED_NAME_EXCEPTIONS):
        return None
    for rx, why in base.FORBIDDEN_NAME_PATTERNS + MACOS_FORBIDDEN_NAME_PATTERNS:
        if rx.search(rel):
            return why
    return None


def audit_app(app: Path, version: str, rules, findings: list[str], stats: dict, prefix: str = "") -> None:
    if not app.is_dir():
        findings.append(f"app bundle missing: {prefix}{app.name}")
        return

    info_path = app / "Contents" / "Info.plist"
    try:
        info = plistlib.loads(info_path.read_bytes())
    except Exception:
        info = None
        findings.append(f"Info.plist missing or unreadable: {prefix}Contents/Info.plist")
    if info is not None:
        if info.get("CFBundleExecutable") != MAIN_EXE:
            findings.append(f"Info.plist CFBundleExecutable is not {MAIN_EXE}")
        if info.get("CFBundleShortVersionString") != version or info.get("CFBundleVersion") != version:
            findings.append(f"Info.plist version is not {version}")
        if info.get("LSUIElement") is not True:
            findings.append("Info.plist LSUIElement is not true")

    seen: set[str] = set()
    version_found = False
    for rel, path, is_link in walk_bundle(app):
        shown = prefix + rel
        if is_link:
            if not link_stays_inside(app, path):
                findings.append(f"symbolic link points outside the bundle: {shown}")
            continue
        data = path.read_bytes()
        seen.add(rel)
        stats["files"] += 1
        stats["bytes"] += len(data)
        why = forbidden_name(rel)
        if why:
            findings.append(f"forbidden file ({why}): {shown}")
        if "__pycache__/" in rel:
            findings.append(f"forbidden file (__pycache__): {shown}")
        if rel.startswith("Contents/MacOS/") and rel.count("/") == 2 and rel not in EXECUTABLES:
            findings.append(f"unexpected file in Contents/MacOS: {shown}")

        compiled = is_macho(data) or rel.lower().endswith(COMPILED_SUFFIXES)
        scan(shown, data, rules, compiled, findings)
        if rel in EXECUTABLES:
            if not is_macho(data):
                findings.append(f"not a Mach-O executable: {shown}")
            if not os.access(path, os.X_OK) and os.name != "nt":
                findings.append(f"not executable: {shown}")
            for name, blob in embedded_entries(data):
                if name.startswith("PYZ:"):
                    stats["embedded_modules"] += 1
                    module = name[4:]
                    if module.split(".")[0] in base.FORBIDDEN_MODULE_PREFIXES:
                        findings.append(f"forbidden module bundled: {module}")
                    if module == "backend.version" and version.encode() in blob:
                        version_found = True
                scan(f"{shown}!{name}", blob, rules, False, findings)
        expected = FRONTEND_FILES.get(rel)
        if expected is not None and hashlib.sha256(data).digest() != hashlib.sha256(expected.read_bytes()).digest():
            findings.append(f"served page differs from the source tree: {shown}")

    for required in (*EXECUTABLES, *FRONTEND_FILES, "Contents/Info.plist"):
        if required not in seen:
            findings.append(f"required file missing: {prefix}{required}")
    if not any(rel.startswith("Contents/Resources/") and rel.endswith(".icns") for rel in seen):
        findings.append("app icon (.icns) missing from Contents/Resources")
    if stats["embedded_modules"] and not version_found:
        findings.append(f"embedded backend.version does not contain {version}")


def audit(target: Path, version: str, env_file: Path, forbid: list[str],
          forbid_env: list[str] | None = None) -> tuple[list[str], dict]:
    findings: list[str] = []
    stats = {"files": 0, "bytes": 0, "embedded_modules": 0, "secret_values_checked": 0}
    rules, stats["secret_values_checked"] = content_rules(env_file, forbid, list(forbid_env or []), findings)

    suffix = target.suffix.lower()
    if suffix == ".dmg":
        with mounted(target) as volume:
            audit_volume(volume, version, rules, findings, stats)
    elif suffix == ".zip":
        with unpacked(target) as folder:
            audit_zip_contents(folder, version, rules, findings, stats)
    else:
        audit_app(target, version, rules, findings, stats)
    return findings, stats


def audit_volume(volume: Path, version: str, rules, findings: list[str], stats: dict) -> None:
    """The disk image's top level: the app, its README and licence, and the Applications link.

    Only the volume metadata macOS itself creates is skipped; any other item, hidden
    or not, is a finding.
    """
    for entry in sorted(volume.iterdir(), key=lambda p: p.name):
        name = entry.name
        if name in DMG_VOLUME_METADATA:
            continue
        if name not in DMG_ALLOWED_TOP_LEVEL:
            findings.append(f"unexpected item on the disk image: {name}")
        elif name == "Applications":
            if not (entry.is_symlink() and os.readlink(entry) == "/Applications"):
                findings.append("disk image 'Applications' is not the link to /Applications")
        elif name != APP_NAME:
            if entry.is_symlink() or not entry.is_file():
                findings.append(f"disk image '{name}' is not a regular file")
                continue
            data = entry.read_bytes()
            stats["files"] += 1
            stats["bytes"] += len(data)
            scan(name, data, rules, False, findings)
    audit_app(volume / APP_NAME, version, rules, findings, stats, prefix=f"{APP_NAME}/")


def audit_zip_contents(folder: Path, version: str, rules, findings: list[str], stats: dict) -> None:
    """An unpacked app archive: exactly the app bundle at the top, nothing else (hidden or not)."""
    extra = sorted(entry.name for entry in folder.iterdir() if entry.name != APP_NAME)
    for name in extra:
        findings.append(f"unexpected item in the archive: {name}")
    audit_app(folder / APP_NAME, version, rules, findings, stats, prefix=f"{APP_NAME}/")


class mounted:
    """Mount a .dmg read-only at a private mount point for the duration of a with block."""

    def __init__(self, dmg: Path):
        self.dmg = dmg
        self.point = Path(tempfile.mkdtemp(prefix="mdv-audit-"))

    def __enter__(self) -> Path:
        subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                        "-mountpoint", str(self.point), str(self.dmg)], check=True, stdout=subprocess.DEVNULL)
        return self.point

    def __exit__(self, *exc) -> None:
        detached = subprocess.run(["/usr/bin/hdiutil", "detach", str(self.point)], check=False,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if detached.returncode != 0:
            subprocess.run(["/usr/bin/hdiutil", "detach", "-force", str(self.point)], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class unpacked:
    """Unpack an app .zip with ditto (which keeps symlinks and permissions) into a private folder."""

    def __init__(self, archive: Path):
        self.archive = archive
        self.folder = Path(tempfile.mkdtemp(prefix="mdv-audit-zip-"))

    def __enter__(self) -> Path:
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(self.archive), str(self.folder)], check=True)
        return self.folder

    def __exit__(self, *exc) -> None:
        import shutil
        shutil.rmtree(self.folder, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("target", type=Path, help="the .app bundle or the .dmg")
    parser.add_argument("--version", default=None, help="expected version (default: backend/version.py)")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env",
                        help="developer .env whose secret values must not appear (default: ./.env)")
    parser.add_argument("--forbid", action="append", default=[], metavar="TEXT",
                        help="additional literal that must not appear (e.g. a test password)")
    parser.add_argument("--forbid-env", action="append", default=[], metavar="VARIABLE",
                        help="environment variable whose value must not appear (e.g. a signing secret)")
    args = parser.parse_args(argv)

    version = args.version or read_version()
    findings, stats = audit(args.target, version, args.env_file, args.forbid, args.forbid_env)
    print(f"audit: {args.target.name}: {stats['files']} files, {stats['bytes'] / 1e6:.1f} MB, "
          f"{stats['embedded_modules']} embedded modules, {stats['secret_values_checked']} secret values checked, "
          f"expected version {version}")
    for finding in findings:
        print("FINDING " + finding)
    print("audit: " + ("FAILED" if findings else "PASSED"))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
