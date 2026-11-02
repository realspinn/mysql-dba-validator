"""Product homepage local preview (MDV_HOMEPAGE_PREVIEW=1) and the /demo route.

The downloaded app opens "/" (release/launcher.py PAGE_URL), so "/" must stay the
validator unless the preview is explicitly enabled. The preview is routing only; it
is not a public deployment mode.

Verification level: TestClient against the FastAPI app and static file checks.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.main as backend_main
from backend.main import app

ROOT = Path(__file__).resolve().parent.parent
HOME = ROOT / "frontend" / "home.html"
REPO = "https://github.com/realspinn/mysql-dba-validator"
LINKEDIN = "https://www.linkedin.com/in/iao-adeleye/"
VALIDATOR_MARKER = 'id="sqlInput"'
HOMEPAGE_MARKER = "Understand SQL risk before execution."

client = TestClient(app, base_url="http://127.0.0.1:8420")


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs, self.srcs, self.ids, self.tags = [], [], set(), []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.tags.append(tag)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        if attrs.get("href"):
            self.hrefs.append(attrs["href"])
        for name in ("src", "srcset"):
            if attrs.get(name):
                self.srcs.append(attrs[name])


def _home_links() -> _Links:
    parser = _Links()
    parser.feed(HOME.read_text(encoding="utf-8"))
    return parser


@pytest.fixture
def preview(monkeypatch):
    monkeypatch.setenv("MDV_HOMEPAGE_PREVIEW", "1")


def test_root_is_the_validator_by_default(monkeypatch):
    monkeypatch.delenv("MDV_HOMEPAGE_PREVIEW", raising=False)
    response = client.get("/")
    assert response.status_code == 200
    assert VALIDATOR_MARKER in response.text and HOMEPAGE_MARKER not in response.text


@pytest.mark.parametrize("value", ["0", "true", "yes", " 1", ""])
def test_only_the_exact_value_1_enables_the_preview(monkeypatch, value):
    monkeypatch.setenv("MDV_HOMEPAGE_PREVIEW", value)
    assert VALIDATOR_MARKER in client.get("/").text


def test_preview_serves_the_homepage_at_root(preview):
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert HOMEPAGE_MARKER in response.text and VALIDATOR_MARKER not in response.text


@pytest.mark.parametrize("enabled", [False, True])
def test_demo_is_the_validator_in_both_modes(monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv("MDV_HOMEPAGE_PREVIEW", "1")
    else:
        monkeypatch.delenv("MDV_HOMEPAGE_PREVIEW", raising=False)
    response = client.get("/demo")
    assert response.status_code == 200
    assert response.content == (ROOT / "frontend" / "index.html").read_bytes()


def test_preview_without_home_html_falls_back_to_the_validator(preview, monkeypatch, tmp_path):
    # The packaged app bundles index.html but not home.html.
    (tmp_path / "index.html").write_text(f"<textarea {VALIDATOR_MARKER}></textarea>", encoding="utf-8")
    monkeypatch.setattr(backend_main, "FRONTEND_DIR", tmp_path)
    assert VALIDATOR_MARKER in client.get("/").text


def test_packaged_apps_do_not_bundle_the_homepage():
    for spec in ("mysql-dba-validator.spec", "mysql-dba-validator-macos.spec"):
        text = (ROOT / "release" / spec).read_text(encoding="utf-8")
        assert "home.html" not in text and '"index.html"' in text, spec


def test_preview_keeps_the_host_check_and_security_headers(preview):
    assert TestClient(app, base_url="http://evil.example").get("/").status_code == 400
    for path in ("/", "/demo"):
        headers = client.get(path).headers
        assert "default-src 'self'" in headers["content-security-policy"]
        assert headers["x-frame-options"] == "DENY"


def test_homepage_links_lead_somewhere(preview):
    links = _home_links()
    internal = {"/", "/demo", "/#how-it-works", "#main"}
    external = {REPO, f"{REPO}/releases", f"{REPO}#readme", f"{REPO}/blob/main/SECURITY.md",
                f"{REPO}/blob/main/VERIFICATION.md", f"{REPO}/blob/main/LICENSE", LINKEDIN}
    for href in links.hrefs:
        if href.startswith("/static/"):
            assert client.get(href).status_code == 200, href
        else:
            assert href in internal | external, href
    # The required destinations are all present.
    for required in ("/demo", "/#how-it-works", f"{REPO}/releases", REPO):
        assert required in links.hrefs, required
    for anchor in ("how-it-works", "capabilities", "main"):
        assert anchor in links.ids, anchor
    for path in ("README.md", "SECURITY.md", "VERIFICATION.md", "LICENSE"):
        assert (ROOT / path).is_file()


def test_footer_credits_the_author():
    text = " ".join(HOME.read_text(encoding="utf-8").split())
    assert "© 2026 Israel Adeleye" in text
    assert f'Built by <a href="{LINKEDIN}">Israel Adeleye' in text
    assert "All rights reserved" not in text  # the project is MIT licensed


def test_scripts_store_only_the_theme_and_make_no_requests():
    text = HOME.read_text(encoding="utf-8")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", text, re.S)
    assert len(scripts) == 3  # theme before paint, theme control, the fish
    for attrs, body in scripts:
        assert "src" not in attrs  # inline only, allowed by the existing script-src
        for forbidden in ("fetch", "XMLHttpRequest", "sendBeacon", "WebSocket", "eval(", "cookie", "sessionStorage"):
            assert forbidden not in body, forbidden
    keys = set(re.findall(r"localStorage\.\w+\(\s*['\"]?([\w-]+)", text)) | set(re.findall(r"KEY = '([\w-]+)'", text))
    assert keys <= {"mdv-home-theme", "KEY"}, keys
    assert "mdv-theme'" not in text  # the validator's own theme setting is left alone
    choices = re.findall(r'name="theme" value="(\w+)"', text)
    assert choices == ["light", "dark", "system"]
    assert re.search(r'value="system" checked', text)


def test_the_fish_is_decorative_local_and_respects_reduced_motion(preview):
    text = HOME.read_text(encoding="utf-8")
    assert '<div class="fish" aria-hidden="true"><i></i></div>' in text  # nothing to focus or follow
    css = " ".join(text.split())
    assert ".fish { display: none; position: fixed;" in css and "pointer-events: none;" in css
    assert "@media (min-width: 1360px) { .fish { display: block; } }" in css  # only beside the content
    assert "animation: fish-swim .5s steps(12) infinite paused;" in css  # plays only while scrolling
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".fish i { opacity: 1; translate: none; animation: none; }" in reduced
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in text
    assert "{ passive: true }" in text
    response = client.get("/static/assets/fish-swim.webp")
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert len(response.content) < 80_000


def test_the_fish_drawing_source_is_kept_and_needs_only_the_standard_library():
    import importlib.util
    import xml.etree.ElementTree as ET

    source = ROOT / "docs" / "homepage-fish" / "fish_drawing.py"
    imports = re.findall(r"^\s*(?:import|from)\s+(\w+)", source.read_text(encoding="utf-8"), re.M)
    assert imports == ["math"]
    spec = importlib.util.spec_from_file_location("fish_drawing", source)
    drawing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drawing)
    for t in (0.0, 1.5, 3.0):
        root = ET.fromstring(drawing.svg(t))
        assert root.tag.endswith("svg") and root.get("viewBox") == "0 0 400 200"


def test_homepage_loads_only_local_assets(preview):
    links = _home_links()
    for src in links.srcs:
        assert src.startswith("/static/assets/"), src
        response = client.get(src)
        assert response.status_code == 200 and response.headers["content-type"].startswith("image/"), src
    text = HOME.read_text(encoding="utf-8")
    assert not re.search(r"@import|url\(\s*['\"]?https?:", text)  # no remote fonts or stylesheets


def test_homepage_states_the_demo_limits():
    text = " ".join(HOME.read_text(encoding="utf-8").split())
    assert "sends your SQL to the hosted server" in text
    assert "Do not paste confidential company scripts, credentials or production-sensitive information." in text
    assert "None. Static analysis only." in text
