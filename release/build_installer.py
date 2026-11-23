"""Build the Windows installer from the portable build's folder.

    python release\\build_installer.py

Run it after release\\build_windows.py (which builds, audits and zips the application and
leaves dist\\MySQL-DBA-Validator\\). It does not build the application again: Inno Setup
packages that same, already audited folder.

Steps (each one fails the build on error):

1. Read the version from backend/version.py (the only version; the script has none).
2. Run the artifact audit on the folder again, so the installer cannot ship a folder that
   did not pass it (forbidden files, credentials, machine paths, missing runtime files).
3. Find the Inno Setup compiler (ISCC.exe) and record its SHA-256 and signature status.
4. Compile release\\installer\\mysql-dba-validator.iss and require compiler 6.7.x, read
   from the compiler's own output (the GitHub Windows runner image provides 6.7.1).
5. Write dist\\MySQL-DBA-Validator-v<version>-windows-x64-setup.exe.sha256.

release\\smoke_test_installer.py then installs, upgrades and uninstalls the result.
Needs Windows and Inno Setup 6.7.x (set MDV_ISCC to the full path of ISCC.exe if it is not
in the default location). The output is unsigned.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from release.build_windows import APP_DIR_NAME, BUILD, DIST, RELEASE, read_version, sha256, write_checksum_file  # noqa: E402

SCRIPT = RELEASE / "installer" / "mysql-dba-validator.iss"
COMPILER_SERIES = "6.7"
DEFAULT_ISCC = (r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe", r"C:\Program Files\Inno Setup 6\ISCC.exe")
_ENGINE = re.compile(r"Compiler engine version: Inno Setup (\d+\.\d+\.\d+)")


def setup_name(version: str) -> str:
    return f"{APP_DIR_NAME}-v{version}-windows-x64-setup.exe"


def numeric_version(version: str) -> str:
    """x.y.z.0 for the setup file's version resource (a pre-release suffix is dropped)."""
    numbers = [int(n) for n in re.findall(r"\d+", version.split("-")[0].split("+")[0])[:3]]
    return ".".join(str(n) for n in (numbers + [0, 0, 0])[:3]) + ".0"


def find_iscc() -> Path:
    candidates = [os.environ.get("MDV_ISCC", ""), *DEFAULT_ISCC, shutil.which("ISCC") or ""]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    sys.exit("Inno Setup compiler not found. Install Inno Setup 6.7.x or set MDV_ISCC to ISCC.exe.")


COMPILER_FILES = ("ISCC.exe", "ISCmplr.dll", "ISPP.dll", "Setup.e32", "Compil32.exe")
# Files that must verify (Windows' own Authenticode check, which also checks the certificate
# chain) and be signed by the publisher named on https://jrsoftware.org/isdl.php.
# Setup.e32 is a stub copied into every setup program and is not signed.
SIGNED_COMPILER_FILES = ("ISCC.exe", "ISCmplr.dll", "ISPP.dll")
COMPILER_PUBLISHER = "Pyrsys B.V."
# True only when the certificate subject has an organisation element that is exactly the publisher
# ("O=Pyrsys B.V." between ", " separators), not a subject that merely contains that text.
TEST_PUBLISHER = r"""
function Test-Publisher([string]$Subject, [string]$Publisher) {
  return $Subject -match ('(^|, )O=' + [regex]::Escape($Publisher) + '(,|$)')
}
"""
COMPILER_REPORT = r"""
param([string]$Folder, [string]$Publisher, [string]$Names)
""" + TEST_PUBLISHER + r"""
$bad = 0
foreach ($name in ($Names -split ',')) {
  $s = Get-AuthenticodeSignature -FilePath (Join-Path $Folder $name)
  $subject = "$($s.SignerCertificate.Subject)"
  $ok = ($s.Status -eq 'Valid') -and (Test-Publisher $subject $Publisher)
  "signature    : $name -> $($s.Status) ($($s.StatusMessage)); signer: $subject" + $(if ($ok) { '' } else { '   <-- NOT ACCEPTED' })
  if (-not $ok) { $bad = 1 }
}
$choco = Get-Command choco -ErrorAction SilentlyContinue
if ($choco) { "chocolatey   : " + ((& choco list --local-only innosetup --limit-output) -join '; ') }
exit $bad
"""


def has_embedded_signature(path: Path) -> bool:
    """True when the PE file carries an Authenticode signature block (it is not checked here)."""
    data = path.read_bytes()
    pe = int.from_bytes(data[0x3C:0x40], "little")
    magic = int.from_bytes(data[pe + 24:pe + 26], "little")
    directories = pe + 24 + (112 if magic == 0x20B else 96)
    address, size = (int.from_bytes(data[directories + 32 + i:directories + 36 + i], "little") for i in (0, 4))
    return address != 0 and size != 0


def record_compiler(iscc: Path) -> None:
    """Print what identifies the compiler used (for the build log) and require that its main
    files verify and are signed by the publisher Inno Setup names (the build fails otherwise).

    This ties the compiler on the build machine to the vendor's signed release, not to a
    checksum kept here. It shows who signed the files and that they are unmodified; it does
    not show the code is free of faults."""
    print(f"    compiler file : {iscc}")
    print(f"    compiler SHA-256: {sha256(iscc)}")
    for name in COMPILER_FILES:
        if (iscc.parent / name).is_file():
            print(f"    signature block: {name} -> " + ("present" if has_embedded_signature(iscc.parent / name) else "absent"))
    script = Path(tempfile.mkdtemp()) / "compiler-report.ps1"
    script.write_text(COMPILER_REPORT, encoding="utf-8")
    try:
        report = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                                 "-Folder", str(iscc.parent), "-Publisher", COMPILER_PUBLISHER, "-Names", ",".join(SIGNED_COMPILER_FILES)],
                                capture_output=True, text=True, timeout=180,
                                # Windows PowerShell started from PowerShell 7 (the CI shell) inherits its module
                                # path and then returns no signature information at all.
                                env={k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"})
    finally:
        shutil.rmtree(script.parent, ignore_errors=True)
    print("\n".join("    " + line for line in (report.stdout + report.stderr).strip().splitlines()) or "    (no report)")
    if report.returncode != 0:
        sys.exit(f"The Inno Setup compiler files did not verify as signed by {COMPILER_PUBLISHER}; not building.")


def compile_setup(iscc: Path, payload: Path, out_dir: Path, version: str, *defines: str) -> tuple[Path, str]:
    """Compile the installer for `version` from `payload` into `out_dir`: (setup file, compiler version).

    The version is not read from the script, which has none: release/smoke_test_installer.py also
    uses this to build a clearly older installer for the upgrade test, and a variant whose process
    check fails (extra /D defines)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [str(iscc), f"/DAppVersion={version}", f"/DAppVersionInfo={numeric_version(version)}",
               f"/DPayloadDir={payload}", f"/DOutputDir={out_dir}", *defines, str(SCRIPT)]
    print("    $ " + " ".join(command), flush=True)
    done = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(done.stdout + done.stderr, flush=True)
    setup = out_dir / setup_name(version)
    if done.returncode != 0:
        sys.exit(f"ISCC failed (exit {done.returncode}).")
    engine = _ENGINE.search(done.stdout)
    if engine is None or not (engine.group(1) + ".").startswith(COMPILER_SERIES + "."):
        setup.unlink(missing_ok=True)
        sys.exit(f"Expected Inno Setup {COMPILER_SERIES}.x; the compiler reported "
                 f"{engine.group(1) if engine else 'no engine version'}. Update COMPILER_SERIES and RELEASING.md deliberately.")
    if not setup.is_file():
        sys.exit(f"ISCC did not write {setup}")
    return setup, engine.group(1)


def main() -> int:
    version = read_version()
    payload = DIST / APP_DIR_NAME
    if not payload.is_dir():
        sys.exit(f"{payload} not found: run release/build_windows.py first.")

    print("\n==> Auditing the payload folder")
    # The audit reads inside the executables with PyInstaller, which only the build venv has.
    venv_python = BUILD / "venv" / "Scripts" / "python.exe"
    if not venv_python.is_file():
        sys.exit(f"{venv_python} not found: run release/build_windows.py first.")
    subprocess.run([str(venv_python), str(RELEASE / "audit_artifact.py"), str(payload)], check=True, cwd=str(ROOT))

    print("\n==> Compiling the installer")
    iscc = find_iscc()
    record_compiler(iscc)
    setup, engine = compile_setup(iscc, payload, DIST, version)
    digest = sha256(setup)
    write_checksum_file(setup.with_name(setup.name + ".sha256"), digest, setup.name)

    print(f"\nInstaller      : {setup}")
    print(f"Inno Setup     : {engine}")
    print(f"Size           : {setup.stat().st_size / 1e6:.1f} MB")
    print(f"SHA256         : {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
