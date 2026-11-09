"""Product homepage local preview (MDV_HOMEPAGE_PREVIEW=1) and the /demo route.

The downloaded app opens "/" (release/launcher.py PAGE_URL), so "/" must stay the
validator unless the preview is explicitly enabled. The preview is routing only; it
is not a public deployment mode.

The homepage is web/home.html with web/src/home/ (TypeScript and CSS, no framework),
built by Vite into frontend/app/home.html.

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
from release.frontend_files import packaged_frontend_files
from tests.frontend_page import built_page, page_assets, page_bundle, source

ROOT = Path(__file__).resolve().parent.parent
HOME = ROOT / "frontend" / "app" / "home.html"
HOME_SOURCE = (ROOT / "web" / "home.html").read_text(encoding="utf-8")
HOME_TS = source("home/home.ts")
HOME_CSS = source("home/home.css")
REPO = "https://github.com/realspinn/mysql-dba-validator"
LINKEDIN = "https://www.linkedin.com/in/iao-adeleye/"
VALIDATOR_MARKER = "<title>MySQL DBA Validator</title>"
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
    # The built file as it is, apart from the version on its script and stylesheet links.
    tag = f"?v={backend_main.APP_VERSION}".encode()
    assert response.content.replace(tag, b"") == (ROOT / "frontend" / "app" / "index.html").read_bytes()


def test_preview_without_home_html_falls_back_to_the_validator(preview, monkeypatch, tmp_path):
    # The packaged app bundles index.html but not home.html.
    (tmp_path / "index.html").write_text(VALIDATOR_MARKER, encoding="utf-8")
    monkeypatch.setattr(backend_main, "PAGES_DIR", tmp_path)
    assert VALIDATOR_MARKER in client.get("/").text


def test_packaged_apps_do_not_bundle_the_homepage():
    shipped = [path for _, path in packaged_frontend_files(ROOT)]
    assert shipped[0] == "app/index.html" and "assets/favicon.svg" in shipped, shipped
    assert not any("home" in path for path in shipped), shipped
    for spec in ("mysql-dba-validator.spec", "mysql-dba-validator-macos.spec"):
        text = (ROOT / "release" / spec).read_text(encoding="utf-8")
        assert "home.html" not in text and "in packaged_frontend_files(ROOT)]" in text, spec


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
    for required in ("/demo", "/#how-it-works", f"{REPO}/releases", REPO):
        assert required in links.hrefs, required
    for anchor in ("how-it-works", "capabilities", "main"):
        assert anchor in links.ids, anchor
    for path in ("README.md", "SECURITY.md", "VERIFICATION.md", "LICENSE"):
        assert (ROOT / path).is_file()


def test_footer_credits_the_author():
    text = " ".join(HOME_SOURCE.split())
    assert "© 2026 Israel Adeleye" in text
    assert f'Built by <a href="{LINKEDIN}">Israel Adeleye' in text
    assert "All rights reserved" not in text  # the project is MIT licensed


def test_scripts_store_only_the_theme_and_make_no_requests():
    # The theme before first paint stays inline so it runs before anything is drawn; the
    # rest is the page's own module from this server (script-src 'self').
    markup = HOME.read_text(encoding="utf-8")
    tags = re.findall(r"<script\b([^>]*)>", markup)
    assert tags[0] == "" and len(tags) == 2
    assert re.fullmatch(r' type="module" crossorigin src="/static/app/assets/home-[\w-]+\.js(\?v=[\w.]+)?"', tags[1])
    inline = re.findall(r"<script>(.*?)</script>", markup, re.S)[0]
    for body in (inline, HOME_TS, page_bundle("home.html")):
        for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "eval(", "cookie", "sessionStorage"):
            assert forbidden not in body, forbidden
    keys = set(re.findall(r"localStorage\.\w+\(\s*['\"]?([\w-]+)", inline + HOME_TS)) | set(re.findall(r"KEY = '([\w-]+)'", HOME_TS))
    assert keys <= {"mdv-home-theme", "KEY"}, keys
    assert "mdv-theme'" not in inline + HOME_TS  # the validator's own theme setting is left alone
    choices = re.findall(r'name="theme" value="(\w+)"', markup)
    assert choices == ["light", "dark", "system"]
    assert re.search(r'value="system" checked', markup)


def test_the_homepage_shares_the_workspace_design_tokens_and_light():
    for shared in ('@import "../styles/tokens.css";', '@import "../styles/base.css";', '@import "../styles/ambient.css";'):
        assert shared in HOME_CSS, shared
    assert '<div class="ambient ambient-home" aria-hidden="true">' in HOME_SOURCE
    # No framework on the homepage: static content, one small module.
    assert "react" not in HOME_TS.lower()
    assert len(page_bundle("home.html")) < 60_000


def test_the_fish_is_decorative_local_and_respects_reduced_motion(preview):
    assert '<div class="fish" aria-hidden="true"><i></i></div>' in HOME_SOURCE  # nothing to focus or follow
    css = " ".join(HOME_CSS.split())
    assert ".fish { display: none; position: fixed;" in css and "pointer-events: none;" in css
    assert "@media (min-width: 1360px) { .fish { display: block; } }" in css  # only beside the content
    assert "animation: fish-swim .5s steps(12) infinite paused;" in css  # plays only while scrolling
    reduced = css[css.index("@media (prefers-reduced-motion: reduce) {", css.index(".fish")):]
    assert ".fish i { opacity: 1; translate: none; animation: none; }" in reduced
    assert "matchMedia('(prefers-reduced-motion: reduce)')" in HOME_TS
    assert "{ passive: true }" in HOME_TS
    response = client.get("/static/assets/fish-swim.webp")
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert len(response.content) < 80_000


def test_the_fish_drawing_source_is_kept_and_needs_only_the_standard_library():
    import importlib.util
    import xml.etree.ElementTree as ET

    drawing_source = ROOT / "docs" / "homepage-fish" / "fish_drawing.py"
    imports = re.findall(r"^\s*(?:import|from)\s+(\w+)", drawing_source.read_text(encoding="utf-8"), re.M)
    assert imports == ["math"]
    spec = importlib.util.spec_from_file_location("fish_drawing", drawing_source)
    drawing = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drawing)
    for t in (0.0, 1.5, 3.0):
        root = ET.fromstring(drawing.svg(t))
        assert root.tag.endswith("svg") and root.get("viewBox") == "0 0 400 200"


def test_homepage_loads_only_local_assets(preview):
    links = _home_links()
    for src in links.srcs:
        assert src.startswith(("/static/assets/", "/static/app/assets/")), src
        response = client.get(src)
        kind = "text/javascript" if ".js" in src else "image/"
        assert response.status_code == 200 and response.headers["content-type"].startswith(kind), src
    stylesheets = [path for path in page_assets(HOME.read_text(encoding="utf-8")) if path.endswith(".css")]
    assert len(stylesheets) == 1 and stylesheets[0] in " ".join(links.hrefs)
    response = client.get(stylesheets[0])
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/css")
    bundle = page_bundle("home.html")
    assert not re.search(r"@import|url\(\s*['\"]?https?:", bundle)  # no remote fonts or stylesheets


def test_homepage_states_the_demo_limits():
    text = " ".join(HOME_SOURCE.split())
    assert "sends your SQL to the hosted server" in text
    assert "Do not paste confidential company scripts, credentials or production-sensitive information." in text
    assert "None. Static analysis only." in text


def _download_menu() -> str:
    text = " ".join(HOME_SOURCE.split())
    start = text.index('<details class="menu download">')
    return text[start:text.index("</details>", start)]


def test_download_menu_follows_the_latest_release():
    # Windows, macOS and the release-notes line all open the release GitHub marks
    # Latest, so publishing a release needs no homepage change or redeploy.
    menu = _download_menu()
    links = re.findall(r'<a class="(menu-item|menu-foot)" href="([^"]+)"', menu)
    assert links == [("menu-item", LATEST), ("menu-item", LATEST), ("menu-foot", LATEST)], links
    windows, macos = menu.split('<a class="menu-item"')[1:3]
    assert "<strong>Windows</strong><small>x64 · portable ZIP, no installer</small>" in windows
    assert "<strong>macOS</strong><small>Apple Silicon (M1 or later) only · signing status in the release notes</small>" in macos
    assert "ZIP" not in macos and "unsigned" not in macos
    assert "choose the file for your computer" in menu
    assert "unsigned" not in " ".join(HOME_SOURCE.split())


def test_homepage_names_no_fixed_release():
    # A pinned version, file or size would go stale at the next release.
    for text in (HOME_SOURCE, HOME_TS):
        assert not re.search(r"\bv\d+\.\d+\.\d+", text), re.search(r"\bv\d+\.\d+\.\d+", text)
        for pinned in ("/releases/download/", "/releases/tag/", "MySQL-DBA-Validator-v", " MB<"):
            assert pinned not in text, pinned


def test_menus_are_native_disclosures_closed_by_escape_or_outside_click():
    assert HOME_SOURCE.count('<details class="menu') == 2  # download and theme
    assert "<summary" in HOME_SOURCE and "role=\"menu\"" not in HOME_SOURCE  # links and radios, not an ARIA menu
    css = " ".join(HOME_CSS.split())
    assert ":hover .menu-panel" not in css and "details:hover" not in css  # never opens on hover
    assert "event.key === 'Escape'" in HOME_TS and "m.querySelector('summary')?.focus()" in HOME_TS
    assert "menus.forEach(m => { if (m !== menu) m.open = false; });" in HOME_TS  # a click outside closes it


def _webp_size(data: bytes) -> tuple[int, int]:
    # Lossless WebP: RIFF....WEBPVP8L, then 14 bits of width - 1 and 14 bits of height - 1.
    assert data[:4] == b"RIFF" and data[8:16] == b"WEBPVP8L"
    bits = int.from_bytes(data[21:25], "little")
    return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1


def test_screenshots_are_lossless_and_twice_their_display_size(preview):
    for name in ("validator-review.webp", "validator-review-dark.webp"):
        response = client.get(f"/static/assets/{name}")
        assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
        # Sharp on 2x screens; drawn at 1440 x 762 CSS pixels at most.
        assert _webp_size(response.content) == (2880, 1524), name
        assert len(response.content) < 150_000, name
        assert f'src="/static/assets/{name}" width="1440" height="762"' in HOME_SOURCE


def test_screenshot_motion_is_optional():
    css = " ".join(HOME_CSS.split())
    frame = css[css.index(".frame { position: relative;"):]
    frame = frame[:frame.index("}")]
    assert "animation" not in frame  # still unless scroll-driven animation is supported
    motion = css[css.index("@supports (animation-timeline: scroll())"):]
    assert motion.startswith("@supports (animation-timeline: scroll()) { @media (prefers-reduced-motion: no-preference) { .frame {")
    reduced = css[css.index("@media (prefers-reduced-motion: reduce) {", css.index(".fish")):]
    assert ".frame img, .fish, .fish i, .points li { transition: none; }" in reduced


def test_the_built_homepage_is_the_source_entry():
    built = built_page("home.html")
    for marker in (HOMEPAGE_MARKER, "Online demo or downloaded application", '<div class="fish" aria-hidden="true">'):
        assert marker in built, marker


# Real Chrome: the open Download menu paints above the product screenshot. The hero's
# entrance animation keeps its columns as stacking contexts, which once confined the
# menu's z-index to its column and let the screenshot, later in the page, paint over it.
MENU_PROBE = """<script>
window.addEventListener('load', () => setTimeout(() => {
  const menu = document.querySelector('details.download');
  menu.querySelector('summary').click();
  setTimeout(() => {
    const panel = menu.querySelector('.menu-panel'), box = panel.getBoundingClientRect();
    const shot = document.querySelector('.frame').getBoundingClientRect();
    const covered = [];
    for (let i = 1; i < 10; i++) for (let j = 1; j < 10; j++) {
      const x = box.left + box.width * i / 10, y = box.top + box.height * j / 10;
      if (y >= innerHeight) continue;
      const top = document.elementFromPoint(x, y);
      if (!panel.contains(top)) covered.push(top ? top.tagName + '.' + top.className : null);
    }
    const out = document.createElement('pre');
    out.id = 'menu-out';
    out.textContent = JSON.stringify({open: menu.open, covered, width: innerWidth,
      box: [box.left, box.top, box.right, box.bottom], overShot: box.bottom > shot.top && box.top < shot.bottom});
    document.body.append(out);
  }, 1000);
}, 2000));
</script>"""


@pytest.mark.parametrize("size", ["1440,900", "1024,768", "390,844"])
def test_the_open_download_menu_paints_above_the_screenshot(tmp_path, size):
    import html
    import json

    from tests.test_frontend_evidence_rendering import dump_dom, find_chrome, serve

    chrome = find_chrome()
    if chrome is None:
        pytest.skip("Chrome is not installed")
    server = serve(built_page("home.html").replace("</body>", MENU_PROBE + "</body>", 1))
    try:
        document, stderr, code = dump_dom(
            [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             f"--window-size={size}", "--virtual-time-budget=10000", f"--user-data-dir={tmp_path / 'profile'}",
             "--dump-dom", f"http://127.0.0.1:{server.server_port}/harness.html"], tmp_path)
    finally:
        server.shutdown()
    match = re.search(r'<pre id="menu-out">(.*?)</pre>', document, re.S)
    assert match, f"page did not render (exit {code}): {stderr[-500:]}"
    result = json.loads(html.unescape(match.group(1)))
    assert result["open"] and result["overShot"], result  # the reported situation
    assert result["covered"] == [], result
    left, _, right, _ = result["box"]
    assert 0 <= left and right <= result["width"], result  # on screen, not past its edge
