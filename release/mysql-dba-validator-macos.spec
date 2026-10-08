# PyInstaller spec for the macOS (Apple Silicon, arm64) app bundle.
# Run through release/build_macos.py, which sets MDV_VERSION.
#
# Same entry point, data and hidden imports as the Windows spec
# (release/mysql-dba-validator.spec); only the packaging differs:
#
# * MySQL DBA Validator.app: its main executable (the first EXE below, which
#   PyInstaller uses as CFBundleExecutable) is the windowed app, and
#   Contents/MacOS/MySQL-DBA-Validator Console is the console app that the windowed
#   app starts as its backend and that operators run from Terminal.
# * arm64 only.
# * No code-signing identity here: PyInstaller signs ad hoc, and release/build_macos.py
#   signs the finished bundle with the Developer ID identity in a separate step.
# * No entitlements file: none has been shown to be needed.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent
VERSION = os.environ["MDV_VERSION"]
TARGET_ARCH = "arm64"

# uvicorn picks its loop/protocol implementations by import string, and sqlglot
# loads dialect modules lazily, so static analysis alone misses them.
hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("sqlglot")
    + ["backend.main", "connector.launcher"]
)

a = Analysis(
    [str(ROOT / "release" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # Only the served page and its favicon.
    datas=[(str(ROOT / "frontend" / "index.html"), "frontend"),
           (str(ROOT / "frontend" / "assets" / "favicon.svg"), "frontend/assets")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest", "httpx", "httpx2", "IPython"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# The app users open: no window; web app only (see release/launcher.py).
app_exe = EXE(
    pyz,
    a.scripts,
    [("X mdv_windowed", None, "OPTION")],
    exclude_binaries=True,
    name="MySQL-DBA-Validator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)
console_exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MySQL-DBA-Validator Console",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # the connector prints pairing codes and takes operator commands here
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    app_exe,
    console_exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MySQL-DBA-Validator",
)
app = BUNDLE(
    coll,
    name="MySQL DBA Validator.app",
    icon=str(ROOT / "release" / "mysql-dba-validator.icns"),
    bundle_identifier="io.github.realspinn.mysql-dba-validator",
    version=VERSION,
    info_plist={
        "CFBundleName": "MySQL DBA Validator",
        "CFBundleDisplayName": "MySQL DBA Validator",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        # No window and no menu: the app serves the page in the browser and stops on
        # its own after the page is closed, so it does not take a Dock icon.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "11.0",
        "LSApplicationCategoryType": "public.app-category.developer-tools",
        "NSHumanReadableCopyright": "MIT License",
    },
)
