"""Build the macOS (Apple Silicon, arm64) app, and later sign, notarize and verify it.

Unsigned validation path (no Apple account, no credentials; what CI runs today):

    python release/build_macos.py build             # isolated venv, PyInstaller, audit of the .app
    python release/build_macos.py package-unsigned  # ditto zip of the .app + .sha256, for inspection

The unsigned app is only ad hoc signed (PyInstaller does that, as Apple Silicon
requires a signature to run). It is a test build: Gatekeeper does not accept it as
a downloaded app, and it is never attached to a release.

Signed distribution path (future; needs an Apple Developer ID and an App Store
Connect API key; see RELEASING.md):

    python release/build_macos.py sign        # Developer ID signature, Hardened Runtime, secure timestamp
    python release/build_macos.py notarize app|dmg
    python release/build_macos.py staple app|dmg
    python release/build_macos.py dmg         # disk image with the (stapled) app, signed
    python release/build_macos.py verify      # codesign, spctl, stapler, architecture, entitlements
    python release/build_macos.py checksum    # .sha256 of the final disk image

Output: dist/macos/MySQL DBA Validator.app and
dist/macos/MySQL-DBA-Validator-v<version>-macos-arm64.dmg (+ .sha256), plus the
notary service's submission results and logs in dist/macos/notary/.

Signing and notarization read their credentials from the environment, never from
the command line, and never print them:

    MDV_CODESIGN_IDENTITY   "Developer ID Application: <name> (<team id>)", whose
                            certificate and key are in an unlocked keychain
    MDV_NOTARY_KEY_PATH     App Store Connect API key file (.p8)
    MDV_NOTARY_KEY_ID       its key ID
    MDV_NOTARY_ISSUER       its issuer ID

Order used by the release workflow (.github/workflows/release-macos.yml), following
Apple's notarization guidance: build -> sign -> notarize app (as a zip) -> staple app
-> dmg (sign) -> notarize dmg -> staple dmg -> verify -> checksum. The app is stapled
before it goes into the disk image, so the copy users drag to /Applications carries
its own ticket.

No entitlements are used: none has been shown to be needed. Requires macOS on
Apple Silicon, Xcode command line tools, and Python 3.14.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release.build_windows import read_version, run, sha256, step  # noqa: E402  (shared helpers)

RELEASE = ROOT / "release"
BUILD = ROOT / "build" / "release-macos"
DIST = ROOT / "dist" / "macos"
APP_NAME = "MySQL DBA Validator.app"
COLLECT_DIR_NAME = "MySQL-DBA-Validator"
MAIN_EXE = "MySQL-DBA-Validator"
CONSOLE_EXE = "MySQL-DBA-Validator Console"
DEVELOPER_ID_PREFIX = "Developer ID Application: "
MACHO_MAGICS = {bytes.fromhex(m) for m in ("feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca")}


def app_path() -> Path:
    return DIST / APP_NAME


def dmg_path(version: str) -> Path:
    return DIST / f"{COLLECT_DIR_NAME}-v{version}-macos-arm64.dmg"


def unsigned_zip_path(version: str) -> Path:
    return DIST / f"{COLLECT_DIR_NAME}-v{version}-macos-arm64-unsigned.zip"


def require(condition: bool, message: str) -> None:
    if not condition:
        sys.exit(message)


def require_macos_arm64() -> None:
    require(sys.platform == "darwin" and platform.machine() == "arm64",
            "The macOS release is built on macOS on Apple Silicon (arm64).")
    require(sys.version_info[:2] == (3, 14), f"Build with Python 3.14; this is {sys.version.split()[0]}.")


def env_value(name: str) -> str:
    value = os.environ.get(name, "")
    require(bool(value), f"{name} is not set.")
    return value


def codesign_identity() -> str:
    identity = env_value("MDV_CODESIGN_IDENTITY")
    require(identity.startswith(DEVELOPER_ID_PREFIX),
            "MDV_CODESIGN_IDENTITY must be a 'Developer ID Application' identity.")
    return identity


# ----------------------------------------------------------------------------- signing order

def is_macho(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


def signing_order(app: Path) -> list[Path]:
    """Everything to sign, innermost first: nested code, frameworks, executables, then the app.

    Symbolic links are never signed or followed; the main executable is signed last
    before the bundle itself.
    """
    nested: list[Path] = []
    for folder, dirnames, filenames in os.walk(app, followlinks=False):
        here = Path(folder)
        for name in dirnames:
            path = here / name
            if name.endswith(".framework") and not path.is_symlink():
                nested.append(path)
        for name in filenames:
            path = here / name
            if not path.is_symlink() and is_macho(path):
                nested.append(path)
    macos = app / "Contents" / "MacOS"
    executables = [macos / CONSOLE_EXE, macos / MAIN_EXE]
    nested = [p for p in nested if p not in executables]
    nested.sort(key=lambda p: (-len(p.relative_to(app).parts), p.as_posix()))
    return nested + executables + [app]


def codesign_command(identity: str, path: Path, hardened_runtime: bool = True) -> list[str]:
    command = ["/usr/bin/codesign", "--force", "--timestamp", "--sign", identity]
    if hardened_runtime:
        command += ["--options", "runtime"]
    return command + [str(path)]


# ----------------------------------------------------------------------------- notarization

def notary_auth_args() -> list[str]:
    key_path = env_value("MDV_NOTARY_KEY_PATH")
    require(Path(key_path).is_file(), "MDV_NOTARY_KEY_PATH does not name a file.")
    return ["--key", key_path, "--key-id", env_value("MDV_NOTARY_KEY_ID"),
            "--issuer", env_value("MDV_NOTARY_ISSUER")]


# Authentication arguments always come last, so commands can be shown without them.
def notary_submit_command(path: Path, auth: list[str]) -> list[str]:
    return ["/usr/bin/xcrun", "notarytool", "submit", str(path), "--wait", "--output-format", "json", *auth]


def notary_log_command(submission_id: str, log: Path, auth: list[str]) -> list[str]:
    return ["/usr/bin/xcrun", "notarytool", "log", submission_id, str(log), *auth]


def notary_accepted(result: dict) -> bool:
    return result.get("status") == "Accepted"


def shown_command(command: list[str], auth_count: int) -> str:
    """The command as printed: without its trailing authentication arguments."""
    visible = command[:-auth_count] if auth_count else command
    return " ".join(visible) + (" <authentication>" if auth_count else "")


def run_without_echoing_auth(command: list[str], auth_count: int) -> subprocess.CompletedProcess:
    print("    $ " + shown_command(command, auth_count), flush=True)
    return subprocess.run(command, check=False, capture_output=True, text=True)


def notarize(target: str, version: str) -> int:
    path = app_path() if target == "app" else dmg_path(version)
    require(path.exists(), f"{path} does not exist.")
    notary = DIST / "notary"
    notary.mkdir(parents=True, exist_ok=True)
    upload = path
    if target == "app":
        # The notary service accepts zip archives, not bare .app bundles.
        upload = notary / f"{path.stem}.zip"
        run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(path), str(upload)])
    auth = notary_auth_args()
    step(f"Submitting {upload.name} to the Apple notary service")
    completed = run_without_echoing_auth(notary_submit_command(upload, auth), len(auth))
    try:
        result = json.loads(completed.stdout or "{}")
    except ValueError:
        result = {}
    (notary / f"{target}-submission.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"    notary status: {result.get('status')}  id: {result.get('id')}", flush=True)
    if completed.returncode != 0:
        # notarytool's own error text (it does not contain the key, only its file path).
        print("    notarytool: " + (completed.stderr or completed.stdout or "").strip()[-2000:], flush=True)
    submission_id = result.get("id")
    if submission_id:
        log = notary / f"{target}-notary-log.json"
        run_without_echoing_auth(notary_log_command(str(submission_id), log, auth), len(auth))
    if target == "app" and upload.exists():
        upload.unlink()  # only the stapled app is kept
    require(completed.returncode == 0 and notary_accepted(result),
            f"Notarization of {path.name} was not accepted (see {notary}).")
    return 0


# ----------------------------------------------------------------------------- commands

def cmd_build() -> int:
    require_macos_arm64()
    version = read_version()
    print(f"Building MySQL DBA Validator {version} for macOS arm64 from {ROOT}")

    step("Cleaning build/release-macos and dist/macos")
    for path in (BUILD, DIST):
        if path.exists():
            shutil.rmtree(path)
    BUILD.mkdir(parents=True)
    DIST.mkdir(parents=True)

    step("Creating isolated build environment")
    venv = BUILD / "venv"
    run([sys.executable, "-m", "venv", str(venv)])
    vpython = venv / "bin" / "python"
    run([str(vpython), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-q",
         "--only-binary", ":all:", "-r", str(RELEASE / "requirements-build-macos.txt")])
    run([str(vpython), "-m", "pip", "check"])

    step("Running PyInstaller")
    env = dict(os.environ, MDV_VERSION=version, PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
    run([str(vpython), "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(BUILD / "work"),
         str(RELEASE / "mysql-dba-validator-macos.spec")], cwd=str(ROOT), env=env)
    # PyInstaller also leaves the plain onedir folder next to the bundle; only the bundle ships.
    shutil.rmtree(DIST / COLLECT_DIR_NAME, ignore_errors=True)

    step("Auditing the app bundle")
    run([str(vpython), str(RELEASE / "audit_macos.py"), str(app_path()), "--version", version], cwd=str(ROOT))
    print(f"\nApp bundle: {app_path()}")
    return 0


def cmd_package_unsigned(version: str) -> int:
    """Zip the unsigned app with ditto (keeps symlinks and permissions) and write its checksum."""
    require_macos_arm64()
    app = app_path()
    require(app.is_dir(), f"{app} does not exist; run 'build' first.")
    archive = unsigned_zip_path(version)
    if archive.exists():
        archive.unlink()
    step("Packaging the unsigned app for inspection")
    run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(archive)])
    digest = sha256(archive)
    archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    print(f"\nUnsigned test build: {archive}\nSHA256: {digest}")
    return 0


def cmd_sign() -> int:
    require_macos_arm64()
    identity = codesign_identity()
    app = app_path()
    require(app.is_dir(), f"{app} does not exist; run 'build' first.")
    step("Signing with the Developer ID identity (Hardened Runtime, secure timestamp, no entitlements)")
    for path in signing_order(app):
        run(codesign_command(identity, path))
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    return 0


def cmd_staple(target: str, version: str) -> int:
    path = app_path() if target == "app" else dmg_path(version)
    require(path.exists(), f"{path} does not exist.")
    run(["/usr/bin/xcrun", "stapler", "staple", str(path)])
    run(["/usr/bin/xcrun", "stapler", "validate", str(path)])
    return 0


def cmd_dmg(version: str) -> int:
    require_macos_arm64()
    identity = codesign_identity()
    app = app_path()
    require(app.is_dir(), f"{app} does not exist.")
    staging = BUILD / "dmg"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    step("Assembling the disk image contents")
    run(["/usr/bin/ditto", str(app), str(staging / APP_NAME)])
    shutil.copyfile(RELEASE / "README-MACOS.txt", staging / "README.txt")
    shutil.copyfile(ROOT / "LICENSE", staging / "LICENSE.txt")
    os.symlink("/Applications", staging / "Applications")
    image = dmg_path(version)
    if image.exists():
        image.unlink()
    step("Creating the disk image")
    run(["/usr/bin/hdiutil", "create", "-volname", f"MySQL DBA Validator {version}", "-srcfolder", str(staging),
         "-fs", "HFS+", "-format", "UDZO", "-ov", str(image)])
    run(["/usr/bin/hdiutil", "verify", str(image)])
    step("Signing the disk image")
    run(codesign_command(identity, image, hardened_runtime=False))
    run(["/usr/bin/codesign", "--verify", "--verbose=2", str(image)])
    print(f"\nDisk image: {image}")
    return 0


def capture(command: list[str]) -> tuple[int, str]:
    completed = subprocess.run(command, check=False, capture_output=True, text=True)
    return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def cmd_verify(version: str) -> int:
    """Check the signed, notarized and stapled app and disk image; fail on any problem."""
    require_macos_arm64()
    app, image = app_path(), dmg_path(version)
    results: list[tuple[str, bool]] = []

    def check(label: str, ok: bool) -> None:
        results.append((label, ok))
        print(("PASS " if ok else "FAIL ") + label, flush=True)

    code, out = capture(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)])
    check("app: codesign --verify --deep --strict", code == 0)
    for exe in (MAIN_EXE, CONSOLE_EXE):
        path = app / "Contents" / "MacOS" / exe
        code, out = capture(["/usr/bin/codesign", "-dvv", str(path)])
        check(f"{exe}: signed by a Developer ID Application identity", code == 0 and
              f"Authority={DEVELOPER_ID_PREFIX}" in out)
        check(f"{exe}: secure timestamp", "Timestamp=" in out and "Signed Time=" not in out)
        check(f"{exe}: Hardened Runtime", "(runtime)" in out)
        code, out = capture(["/usr/bin/codesign", "-d", "--entitlements", "-", "--xml", str(path)])
        check(f"{exe}: no entitlements", code == 0 and "<key>" not in out and "get-task-allow" not in out)
        code, out = capture(["/usr/bin/lipo", "-archs", str(path)])
        check(f"{exe}: arm64 only", code == 0 and out.strip() == "arm64")
    code, out = capture(["/usr/sbin/spctl", "--assess", "--type", "exec", "-vvv", str(app)])
    check("app: Gatekeeper assessment accepts it as Notarized Developer ID",
          code == 0 and "accepted" in out and "Notarized Developer ID" in out)
    code, out = capture(["/usr/bin/xcrun", "stapler", "validate", str(app)])
    check("app: stapled notarization ticket", code == 0)

    code, out = capture(["/usr/bin/codesign", "--verify", "--verbose=2", str(image)])
    check("dmg: codesign --verify", code == 0)
    code, out = capture(["/usr/bin/codesign", "-dvv", str(image)])
    check("dmg: signed by a Developer ID Application identity with a secure timestamp",
          f"Authority={DEVELOPER_ID_PREFIX}" in out and "Timestamp=" in out)
    code, out = capture(["/usr/sbin/spctl", "--assess", "--type", "open", "--context", "context:primary-signature",
                         "-vvv", str(image)])
    check("dmg: Gatekeeper assessment accepts it as Notarized Developer ID",
          code == 0 and "accepted" in out and "Notarized Developer ID" in out)
    code, out = capture(["/usr/bin/xcrun", "stapler", "validate", str(image)])
    check("dmg: stapled notarization ticket", code == 0)
    code, out = capture(["/usr/bin/hdiutil", "verify", str(image)])
    check("dmg: hdiutil verify", code == 0)

    passed = sum(ok for _, ok in results)
    summary = "\n".join(("PASS " if ok else "FAIL ") + label for label, ok in results)
    (DIST / "macos-verification.txt").write_text(summary + f"\n{passed}/{len(results)} checks passed\n",
                                                encoding="utf-8")
    print(f"\n{passed}/{len(results)} signature and notarization checks passed")
    return 0 if passed == len(results) else 1


def cmd_checksum(version: str) -> int:
    image = dmg_path(version)
    require(image.is_file(), f"{image} does not exist.")
    digest = sha256(image)
    checksum = image.with_name(image.name + ".sha256")
    checksum.write_text(f"{digest}  {image.name}\n", encoding="ascii")
    print(f"Disk image : {image}\nSize       : {image.stat().st_size} bytes\nSHA256     : {digest}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    usage = __doc__
    if not args:
        sys.exit(usage)
    command, rest = args[0], args[1:]
    version = read_version()
    if command == "build" and not rest:
        return cmd_build()
    if command == "package-unsigned" and not rest:
        return cmd_package_unsigned(version)
    if command == "sign" and not rest:
        return cmd_sign()
    if command in ("notarize", "staple") and rest in (["app"], ["dmg"]):
        require_macos_arm64()
        return notarize(rest[0], version) if command == "notarize" else cmd_staple(rest[0], version)
    if command == "dmg" and not rest:
        return cmd_dmg(version)
    if command == "verify" and not rest:
        return cmd_verify(version)
    if command == "checksum" and not rest:
        return cmd_checksum(version)
    sys.exit(usage)


if __name__ == "__main__":
    sys.exit(main())
