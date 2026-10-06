# PyInstaller spec for the Windows portable ("onedir") build.
# Run through release/build_windows.py, which sets MDV_VERSION_FILE.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent

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

# Committed multi-size icon (16-256 px) made from frontend/assets/favicon.svg;
# used as-is, so the build needs no image-conversion dependency.
ICON = str(ROOT / "release" / "mysql-dba-validator.ico")

# The app users double-click: no console window; web app only (see release/launcher.py).
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
    version=os.environ.get("MDV_VERSION_FILE") or None,
    icon=ICON,
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
    version=os.environ.get("MDV_CONSOLE_VERSION_FILE") or None,
    icon=ICON,
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
