"""Build the Windows x64 portable release.

    python release\\build_windows.py

Steps (each one fails the build on error):

1. Read the version from backend/version.py.
2. Delete previous output: build\\release\\ and dist\\.
3. Create an isolated build venv (build\\release\\venv) and install the exact
   pins from release\\requirements-build.txt (no test packages).
4. Run PyInstaller with release\\mysql-dba-validator.spec (onedir, console).
5. Add the user README (release\\README-WINDOWS.txt -> README.txt) and LICENSE
   if the repository has one.
6. Audit the folder (release\\audit_artifact.py): no .env, keys, registries,
   logs, tests, developer secrets or machine paths.
7. Zip it as dist\\MySQL-DBA-Validator-v<version>-windows-x64.zip (one top-level
   folder) and write <zip>.sha256.

Requires: 64-bit CPython 3.14 on Windows, and network access to PyPI for step 3.
The output is reproducible in content (same pinned inputs) but not
bit-for-bit (PyInstaller and zip timestamps differ between builds).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE = ROOT / "release"
BUILD = ROOT / "build" / "release"
DIST = ROOT / "dist"
APP_DIR_NAME = "MySQL-DBA-Validator"


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(command: list[str], **kwargs) -> None:
    print("    $ " + " ".join(str(c) for c in command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def read_version() -> str:
    text = (ROOT / "backend" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^APP_VERSION\s*=\s*"(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)"', text, re.M)
    if not match:
        sys.exit("cannot read APP_VERSION from backend/version.py")
    return match.group(1)


def version_resource(version: str, path: Path) -> None:
    """Windows file-properties resource for the executable."""
    numbers = [int(n) for n in re.findall(r"\d+", version.split("-")[0])[:3]] + [0]
    tup = tuple((numbers + [0, 0, 0, 0])[:4])
    path.write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'MySQL DBA Validator'),
      StringStruct('ProductName', 'MySQL DBA Validator'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('OriginalFilename', 'MySQL-DBA-Validator.exe'),
      StringStruct('InternalName', 'MySQL-DBA-Validator')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")


def make_zip(source: Path, zip_path: Path, top: str) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(p for p in source.rglob("*") if p.is_file()):
            archive.write(path, f"{top}/{path.relative_to(source).as_posix()}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    if os.name != "nt" or struct.calcsize("P") != 8:
        sys.exit("The Windows x64 release must be built with 64-bit Python on Windows.")
    if sys.version_info[:2] != (3, 14):
        sys.exit(f"Build with Python 3.14 (the tested runtime); this is {sys.version.split()[0]}.")

    version = read_version()
    artifact = f"{APP_DIR_NAME}-v{version}-windows-x64"
    zip_path = DIST / f"{artifact}.zip"
    print(f"Building {artifact} from {ROOT}")

    step("Cleaning build/release and dist")
    for path in (BUILD, DIST):
        if path.exists():
            shutil.rmtree(path)
    BUILD.mkdir(parents=True)
    DIST.mkdir(parents=True)

    step("Creating isolated build environment")
    venv = BUILD / "venv"
    run([sys.executable, "-m", "venv", str(venv)])
    vpython = venv / "Scripts" / "python.exe"
    run([str(vpython), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-q",
         "-r", str(RELEASE / "requirements-build.txt")])
    run([str(vpython), "-m", "pip", "check"])

    step("Running PyInstaller")
    version_file = BUILD / "version_info.txt"
    version_resource(version, version_file)
    env = dict(os.environ, MDV_VERSION_FILE=str(version_file), PYTHONHASHSEED="0",
               PYTHONDONTWRITEBYTECODE="1")
    run([str(vpython), "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(BUILD / "work"),
         str(RELEASE / "mysql-dba-validator.spec")], cwd=str(ROOT), env=env)
    app_dir = DIST / APP_DIR_NAME

    step("Adding user documentation")
    shutil.copyfile(RELEASE / "README-WINDOWS.txt", app_dir / "README.txt")
    license_file = ROOT / "LICENSE"
    if license_file.is_file():
        shutil.copyfile(license_file, app_dir / "LICENSE.txt")
    else:
        print("    WARNING: no LICENSE file in the repository; the release has no license text.")

    step("Auditing release folder")
    run([str(vpython), str(RELEASE / "audit_artifact.py"), str(app_dir)], cwd=str(ROOT))

    step("Creating zip and checksum")
    make_zip(app_dir, zip_path, f"{artifact}")
    digest = sha256(zip_path)
    checksum_path = zip_path.with_name(zip_path.name + ".sha256")
    checksum_path.write_text(f"{digest}  {zip_path.name}\n", encoding="ascii")
    run([str(vpython), str(RELEASE / "audit_artifact.py"), str(zip_path)], cwd=str(ROOT))

    print(f"\nRelease artifact : {zip_path}")
    print(f"Size             : {zip_path.stat().st_size / 1e6:.1f} MB")
    print(f"SHA256           : {digest}")
    print(f"Checksum file    : {checksum_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
