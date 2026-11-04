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
from tests.frontend_page import page_source

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


# GitHub redirects this to the release marked Latest: never a draft, and a prerelease
# only if someone marks it Latest by hand. Asset names carry the version, so there is
# no stable direct file URL; the release page lists both downloads.
LATEST = f"{REPO}/releases/latest"


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
                f"{REPO}/blob/main/VERIFICATION.md", f"{REPO}/blob/main/LICENSE", LINKEDIN, LATEST}
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
    text = " ".join(page_source("home.html").split())
    assert "© 2026 Israel Adeleye" in text
    assert f'Built by <a href="{LINKEDIN}">Israel Adeleye' in text
    assert "All rights reserved" not in text  # the project is MIT licensed


def test_scripts_store_only_the_theme_and_make_no_requests():
    # The theme before first paint stays inline so it runs before anything is drawn;
    # the rest is the page's own script from this server (script-src 'self').
    markup = HOME.read_text(encoding="utf-8")
    assert re.findall(r"<script\b([^>]*)>", markup) == ["", ' src="/static/assets/js/home.js"']
    text = page_source("home.html")
    scripts = re.findall(r"<script\b([^>]*)>(.*?)</script>", text, re.S)
    assert len(scripts) == 2  # theme before paint; theme control, menus, screenshots and the fish
    for attrs, body in scripts:
        for forbidden in ("fetch", "XMLHttpRequest", "sendBeacon", "WebSocket", "eval(", "cookie", "sessionStorage"):
            assert forbidden not in body, forbidden
    keys = set(re.findall(r"localStorage\.\w+\(\s*['\"]?([\w-]+)", text)) | set(re.findall(r"KEY = '([\w-]+)'", text))
    assert keys <= {"mdv-home-theme", "KEY"}, keys
    assert "mdv-theme'" not in text  # the validator's own theme setting is left alone
    choices = re.findall(r'name="theme" value="(\w+)"', text)
    assert choices == ["light", "dark", "system"]
    assert re.search(r'value="system" checked', text)


def test_the_fish_is_decorative_local_and_respects_reduced_motion(preview):
    text = page_source("home.html")
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
        kind = "text/javascript" if src.endswith(".js") else "image/"
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind), src
    stylesheet = "/static/assets/css/home.css"
    assert stylesheet in links.hrefs
    response = client.get(stylesheet)
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/css")
    text = page_source("home.html")
    assert not re.search(r"@import|url\(\s*['\"]?https?:", text)  # no remote fonts or stylesheets


def test_homepage_states_the_demo_limits():
    text = " ".join(page_source("home.html").split())
    assert "sends your SQL to the hosted server" in text
    assert "Do not paste confidential company scripts, credentials or production-sensitive information." in text
    assert "None. Static analysis only." in text


def _download_menu() -> str:
    text = " ".join(page_source("home.html").split())
    start = text.index('<details class="menu download">')
    return text[start:text.index("</details>", start)]


def test_download_menu_follows_the_latest_release():
    # Windows, macOS and the release-notes line all open the release GitHub marks
    # Latest, so publishing a release needs no homepage change or redeploy.
    menu = _download_menu()
    links = re.findall(r'<a class="(menu-item|menu-foot)" href="([^"]+)"', menu)
    assert links == [("menu-item", LATEST), ("menu-item", LATEST), ("menu-foot", LATEST)], links
    # Honest labels: an x64 Windows build, and a macOS build for Apple Silicon only, unsigned.
    windows, macos = menu.split('<a class="menu-item"')[1:3]
    assert "<strong>Windows</strong><small>x64 · portable ZIP, no installer</small>" in windows
    assert "<strong>macOS</strong><small>Apple Silicon (M1 or later) only · unsigned, not notarized</small>" in macos
    assert "choose the ZIP for your computer" in menu


def test_homepage_names_no_fixed_release():
    # A pinned version, file or size would go stale at the next release.
    text = page_source("home.html")
    assert not re.search(r"\bv\d+\.\d+\.\d+", text), re.search(r"\bv\d+\.\d+\.\d+", text)
    for pinned in ("/releases/download/", "/releases/tag/", "MySQL-DBA-Validator-v", " MB<"):
        assert pinned not in text, pinned


def test_menus_are_native_disclosures_closed_by_escape_or_outside_click():
    text = page_source("home.html")
    assert text.count('<details class="menu') == 2  # download and theme
    assert "<summary" in text and "role=\"menu\"" not in text  # links and radios, not an ARIA menu
    css = " ".join(text.split())
    assert ":hover .menu-panel" not in css and "details:hover" not in css  # never opens on hover
    assert "event.key === 'Escape'" in text and ".querySelector('summary').focus()" in text
    assert "closeAll(menu)" in text  # a click outside closes the open menu


def _webp_size(data: bytes) -> tuple[int, int]:
    # Lossless WebP: RIFF....WEBPVP8L, then 14 bits of width - 1 and 14 bits of height - 1.
    assert data[:4] == b"RIFF" and data[8:16] == b"WEBPVP8L"
    bits = int.from_bytes(data[21:25], "little")
    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1


def test_screenshots_are_lossless_and_twice_their_display_size(preview):
    text = page_source("home.html")
    for name in ("validator-review.webp", "validator-review-dark.webp"):
        response = client.get(f"/static/assets/{name}")
        assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
        # Sharp on 2x screens; drawn at 1440 x 762 CSS pixels at most.
        assert _webp_size(response.content) == (2880, 1524), name
        assert len(response.content) < 150_000, name
        assert f'src="/static/assets/{name}" width="1440" height="762"' in text


def test_screenshot_motion_is_optional():
    css = " ".join(page_source("home.html").split())
    frame = css[css.index(".frame { display: block;"):]
    frame = frame[:frame.index("}")]
    assert "animation" not in frame  # still unless scroll-driven animation is supported
    motion = css[css.index("@supports (animation-timeline: scroll())"):]
    assert motion.startswith("@supports (animation-timeline: scroll()) { @media (prefers-reduced-motion: no-preference) { .frame {")
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert ".frame img, .fish, .fish i { transition: none; }" in reduced
