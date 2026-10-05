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
    # Only the served page. index_legacy.html is a source-tree rollback copy.
    datas=[(str(ROOT / "frontend" / "index.html"), "frontend")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "_pytest", "httpx", "httpx2", "IPython"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MySQL-DBA-Validator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # the connector prints pairing codes and takes operator commands here
    version=os.environ.get("MDV_VERSION_FILE") or None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MySQL-DBA-Validator",
)
