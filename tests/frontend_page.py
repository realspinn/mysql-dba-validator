"""The frontend as tests see it: the TypeScript source in web/src/ and the pages Vite
built from it into frontend/app/ (committed; CI rebuilds and compares them).

Source checks read web/src, the text a reviewer reads. Checks on what the browser
actually runs read the built pages and their bundles.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT / "frontend"
PAGES_DIR = FRONTEND_DIR / "app"
WEB_DIR = ROOT / "web"
WEB_SRC = WEB_DIR / "src"

# A built page's own module script and stylesheet (the served page adds ?v=<version>).
_ASSET = re.compile(r'(?:src|href)="(/static/app/assets/[\w.-]+\.(?:js|css))(?:\?v=[\w.]+)?"')


def source(path: str) -> str:
    """web/src/<path>."""
    return (WEB_SRC / path).read_text(encoding="utf-8")


def source_files() -> dict[str, str]:
    """Every TypeScript and CSS file under web/src, by path relative to it."""
    return {p.relative_to(WEB_SRC).as_posix(): p.read_text(encoding="utf-8")
            for p in sorted(WEB_SRC.rglob("*")) if p.suffix in (".ts", ".tsx", ".css")}


def all_source() -> str:
    return "\n".join(source_files().values())


def built_page(name: str) -> str:
    """frontend/app/<name>, as built."""
    return (PAGES_DIR / name).read_text(encoding="utf-8")


def page_assets(html: str) -> list[str]:
    """The /static/app/assets/... files a page loads."""
    return _ASSET.findall(html)


def read_static(path: str) -> str:
    """The file behind a /static/... address."""
    return (FRONTEND_DIR / path.removeprefix("/static/")).read_text(encoding="utf-8")


def page_bundle(name: str) -> str:
    """A built page with the scripts and styles it loads appended: what the browser runs."""
    html = built_page(name)
    return html + "\n".join(read_static(path) for path in page_assets(html))


def function_body(text: str, name: str) -> str:
    """The brace-delimited body of `function name(...) {...}` or `const name = ... => {...}`."""
    match = re.search(r"function\s+" + re.escape(name) + r"\b[^{]*\{", text)
    if match:
        start = match.end()
    else:
        declared = re.search(r"const\s+" + re.escape(name) + r"\s*=", text)
        assert declared, f"{name} not found"
        arrow = text.find("=> {", declared.end())
        assert arrow != -1, f"{name} has no body"
        # Not the body of a later declaration (`name` itself has an expression body).
        later = re.search(r"\b(?:const|function)\s", text[declared.end():arrow])
        assert not later, f"{name} has no block body"
        start = arrow + len("=> {")
    depth = 0
    for index in range(start - 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start:index]
    raise AssertionError(f"unbalanced braces in {name}")


def object_block(text: str, name: str) -> str:
    """The `{...}` literal assigned to `name` (e.g. a message map)."""
    start = re.search(r"\b" + re.escape(name) + r"\b[^=\n]*=\s*\{", text)
    assert start, f"{name} not found"
    depth = 0
    for index in range(start.end() - 1, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start.end():index]
    raise AssertionError(f"unbalanced braces in {name}")


def message_map(text: str, name: str) -> dict[str, str]:
    """A map of fixed one-line string literals: {code: message}."""
    return dict(re.findall(r"\n\t+(\w+): '((?:[^'\\]|\\.)*)',", object_block(text, name)))


def contrast(foreground: str, background: str) -> float:
    """WCAG contrast ratio of two colours given as rgb(...) or #rrggbb."""
    def channels(colour):
        colour = colour.strip()
        if colour.startswith("#"):
            digits = colour[1:]
            if len(digits) == 3:  # the minifier's short form, #fff
                digits = "".join(c * 2 for c in digits)
            return [int(digits[i:i + 2], 16) for i in (0, 2, 4)]
        return [int(float(part)) for part in re.findall(r"[\d.]+", colour)[:3]]

    def luminance(colour):
        linear = [(c / 255) / 12.92 if c / 255 <= 0.03928 else ((c / 255 + 0.055) / 1.055) ** 2.4
                  for c in channels(colour)]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)
