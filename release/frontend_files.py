"""The frontend files the packaged apps ship: the validator page and what it loads.

The page is built by Vite into frontend/app/ (committed; see web/README.md). The
packaged apps carry frontend/app/index.html, the hashed script and stylesheet it
links, and the favicon. The homepage (frontend/app/home.html and its files) is not
shipped: "/" in the app is always the validator. Used by both PyInstaller specs and
both artifact audits, so they can never disagree.
"""

from __future__ import annotations

import re
from pathlib import Path

_ASSET = re.compile(r'(?:src|href)="/static/(app/assets/[\w.-]+\.(?:js|css))"')


def packaged_frontend_files(root: Path) -> list[tuple[Path, str]]:
    """(source file, path under the bundle's frontend/ folder), in a fixed order."""
    frontend = root / "frontend"
    page = frontend / "app" / "index.html"
    assets = _ASSET.findall(page.read_text(encoding="utf-8"))
    if not any(a.endswith(".js") for a in assets) or not any(a.endswith(".css") for a in assets):
        raise SystemExit(f"{page} links no built script or stylesheet: run npm run build in web/")
    files = [(page, "app/index.html")] + [(frontend / a, a) for a in sorted(set(assets))]
    files.append((frontend / "assets" / "favicon.svg", "assets/favicon.svg"))
    missing = [str(source) for source, _ in files if not source.is_file()]
    if missing:
        raise SystemExit("missing frontend files: " + ", ".join(missing))
    return files
