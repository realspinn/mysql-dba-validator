"""A frontend page with its own stylesheet and script put back in place.

frontend/index.html and frontend/home.html load their CSS and JS from
frontend/assets/css/ and frontend/assets/js/. Tests that inspect the page's code
read it through page_source(), so they check the same text the browser runs.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
# The served page tags each link with the running version (?v=0.10.1); the file does not.
_PAGE_FILE = re.compile(r'<link rel="stylesheet" href="(/static/assets/css/[\w-]+\.css)(?:\?v=[\w.]+)?" />'
                        r'|<script src="(/static/assets/js/[\w-]+\.js)(?:\?v=[\w.]+)?"></script>')


def read_static(path: str) -> str:
    """The source file behind a /static/... address."""
    return (FRONTEND_DIR / path.removeprefix("/static/")).read_text(encoding="utf-8")


def with_page_files(html: str, read=read_static) -> str:
    """Replace each stylesheet link and script tag with the file's contents, inline."""
    def inline(match):
        if match.group(1):
            return "<style>\n" + read(match.group(1)) + "</style>"
        return "<script>\n" + read(match.group(2)) + "</script>"
    return _PAGE_FILE.sub(inline, html)


def page_source(name: str) -> str:
    """frontend/<name> with its stylesheet and script inline."""
    return with_page_files((FRONTEND_DIR / name).read_text(encoding="utf-8"))
