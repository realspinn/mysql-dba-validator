"""The optional update notice: backend/update_check.py, POST /api/update-status and the page.

No test contacts GitHub: every request goes to a fake opener. The check sends one fixed,
bodyless GET at most once per process, reports a newer release only for a strict vX.Y.Z
whose page is the official release page, treats every failure as unavailable, and can be
turned off. The page asks only its own backend, never covers the review, and remembers a
dismissal per version (or for the page only when storage is unavailable). The banner is
checked in headless Chrome; skipped when Chrome is not installed.
"""

from __future__ import annotations

import html
import inspect
import json
import re
import socket
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.update_check as update_check
from backend.main import app
from backend.version import APP_VERSION
from tests.frontend_page import page_source
from tests.test_frontend_evidence_rendering import dump_dom, find_chrome

client = TestClient(app, base_url="http://127.0.0.1:8420")
REAL_OPEN = update_check._open  # the autouse fixture replaces it in every test
OWN_PAGE = {"Origin": "http://127.0.0.1:8420", "Host": "127.0.0.1:8420"}
RELEASE = "https://github.com/realspinn/mysql-dba-validator/releases/tag/"
CURRENT = tuple(int(part) for part in APP_VERSION.split("."))


def release(tag="v99.0.0", **fields) -> dict:
    data = {"tag_name": tag, "html_url": RELEASE + str(tag), "draft": False, "prerelease": False,
            "name": f"MySQL DBA Validator {tag}", "published_at": "2026-10-23T21:05:03Z"}
    data.update(fields)
    return data


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200):
        self.body, self.status = body, status

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeGitHub:
    """Stands in for update_check._open: records each request, answers or raises."""

    def __init__(self):
        self.requests: list[urllib.request.Request] = []
        self.answer = release()

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.answer, BaseException):
            raise self.answer
        if isinstance(self.answer, FakeResponse):
            return self.answer
        body = self.answer if isinstance(self.answer, bytes) else json.dumps(self.answer).encode()
        return FakeResponse(body)


@pytest.fixture(autouse=True)
def github(monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(update_check, "_open", fake)
    monkeypatch.setattr(update_check, "_result", None)
    monkeypatch.delenv(update_check.OFF_SWITCH, raising=False)
    return fake


def status(headers=OWN_PAGE, **kwargs):
    return client.post("/api/update-status", headers=headers, **kwargs)


def bump(index: int, by: int) -> str:
    parts = list(CURRENT)
    parts[index] += by
    if by > 0:
        parts[index + 1:] = [0] * (2 - index)
    return "v" + ".".join(map(str, parts))


# ---------------------------------------------------------------- version comparison

@pytest.mark.parametrize("tag", [bump(0, 1), bump(1, 1), bump(2, 1), "v99.0.0",
                                 # numeric, not text, comparison: 0.10.0 is newer than 0.9.x
                                 "v%d.%d.0" % (CURRENT[0], CURRENT[1] + 10)])
def test_a_strictly_newer_release_is_reported_with_its_official_page(github, tag):
    github.answer = release(tag)
    response = status()
    assert response.status_code == 200
    assert response.json() == {"state": "update_available", "current": APP_VERSION,
                               "version": tag[1:], "url": RELEASE + tag}


def test_the_installed_version_is_up_to_date(github):
    github.answer = release("v" + APP_VERSION)
    # The installed version is included, so a freshly updated copy can confirm it.
    assert status().json() == {"state": "up_to_date", "current": APP_VERSION}


@pytest.mark.parametrize("tag", ["v0.0.0", "v0.5.9", "v0.5.10"])
def test_an_older_release_is_up_to_date(github, tag):
    assert tuple(int(p) for p in tag[1:].split(".")) < CURRENT
    github.answer = release(tag)
    assert status().json() == {"state": "up_to_date", "current": APP_VERSION}


def test_versions_compare_as_numbers_not_text():
    assert update_check._version("0.10.0") > update_check._version("0.9.9")
    assert update_check._version("10.0.0") > update_check._version("9.99.99")


# ---------------------------------------------------------------- unexpected responses

@pytest.mark.parametrize("tag", ["0.7.0", "v0.7", "v0.7.0-rc1", "v0.07.0", "v0.7.0 ", " v0.7.0", "V0.7.0",
                                 "v1.2.3.4", "v99999.0.0", "v", "", 7, None, ["v0.7.0"]])
def test_an_invalid_tag_is_unavailable(github, tag):
    github.answer = release(tag, html_url=RELEASE + str(tag))
    assert status().json() == {"state": "unavailable"}


@pytest.mark.parametrize("url", [
    None, "", "https://github.com/realspinn/mysql-dba-validator/releases/tag/v99.0.1",
    "http://github.com/realspinn/mysql-dba-validator/releases/tag/v99.0.0",
    "https://github.com/someone-else/mysql-dba-validator/releases/tag/v99.0.0",
    "https://github.com.evil.example/realspinn/mysql-dba-validator/releases/tag/v99.0.0",
    "https://evil.example/?https://github.com/realspinn/mysql-dba-validator/releases/tag/v99.0.0",
    "https://github.com/realspinn/mysql-dba-validator/releases/tag/v99.0.0/../../../x",
    "https://github.com/realspinn/mysql-dba-validator/releases/download/v99.0.0/app.zip",
    "javascript:alert(1)",
])
def test_a_release_page_other_than_the_official_one_is_never_exposed(github, url):
    github.answer = release("v99.0.0", html_url=url)
    data = status().json()
    assert data == {"state": "unavailable"}


@pytest.mark.parametrize("fields", [{"draft": True}, {"prerelease": True}, {"draft": None},
                                    {"prerelease": "false"}, {"draft": 0}])
def test_a_draft_prerelease_or_unclear_release_is_unavailable(github, fields):
    github.answer = release(**fields)
    assert status().json() == {"state": "unavailable"}


@pytest.mark.parametrize("body", [b"", b"not json", b"{", b"[]", b'"v99.0.0"', b"null", b"{}",
                                  b'{"message": "Not Found"}', b"\xff\xfe"])
def test_malformed_or_unexpected_json_is_unavailable(github, body):
    github.answer = body
    assert status().json() == {"state": "unavailable"}


def test_an_oversized_response_is_unavailable(github):
    github.answer = json.dumps(release()).encode() + b" " * update_check.MAX_RESPONSE_BYTES
    assert status().json() == {"state": "unavailable"}


def test_a_non_200_success_status_is_unavailable(github):
    github.answer = FakeResponse(json.dumps(release()).encode(), status=203)
    assert status().json() == {"state": "unavailable"}


@pytest.mark.parametrize("code", [301, 302, 304, 401, 403, 404, 410, 429, 500, 503])
def test_an_http_error_or_rate_limit_is_unavailable(github, code):
    github.answer = urllib.error.HTTPError(update_check.LATEST_RELEASE_API, code, "error", {}, None)
    assert status().json() == {"state": "unavailable"}


@pytest.mark.parametrize("error", [
    urllib.error.URLError("offline"), TimeoutError(), socket.timeout(), ConnectionResetError(),
    ssl.SSLCertVerificationError("certificate verify failed"), ssl.SSLError("handshake"),
    OSError("no route to host"),
])
def test_offline_timeout_or_tls_failure_is_unavailable(github, error):
    github.answer = error
    assert status().json() == {"state": "unavailable"}


def test_an_unreadable_installed_version_is_unavailable_without_a_request(github, monkeypatch):
    monkeypatch.setattr(update_check, "APP_VERSION", "dev")
    assert status().json() == {"state": "unavailable"}
    assert github.requests == []


# ---------------------------------------------------------------- the request

def test_the_request_is_one_fixed_bodyless_get_with_fixed_headers(github):
    status()
    (request,) = github.requests
    assert request.full_url == "https://api.github.com/repos/realspinn/mysql-dba-validator/releases/latest"
    assert request.get_method() == "GET" and request.data is None
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers == {"accept": "application/vnd.github+json", "x-github-api-version": "2026-03-10",
                       "user-agent": f"MySQL-DBA-Validator/{APP_VERSION}"}


def test_nothing_submitted_to_the_app_reaches_the_request(github):
    secrets = {"sql": "SELECT secret_column FROM payroll", "host": "db.corp.example", "username": "dba_admin",
               "password": "hunter2-password", "database": "finance_schema"}
    client.post("/api/validate", json=secrets)
    status(json=secrets)  # any body is ignored
    (request,) = github.requests
    sent = request.full_url + repr(request.header_items()) + repr(request.data)
    for value in secrets.values():
        for part in re.split(r"\W+", value):
            if len(part) > 3:
                assert part not in sent, part
    assert list(inspect.signature(update_check.check_for_update).parameters) == []


def test_the_real_opener_verifies_tls_times_out_and_does_not_follow_redirects(monkeypatch):
    seen = {}

    class Opener:
        def open(self, request, timeout):
            seen["timeout"] = timeout
            return "response"

    def build_opener(*handlers):
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(update_check.urllib.request, "build_opener", build_opener)
    assert REAL_OPEN(urllib.request.Request(update_check.LATEST_RELEASE_API)) == "response"
    assert seen["timeout"] == update_check.TIMEOUT_SECONDS == 5
    https = [h for h in seen["handlers"] if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1
    context = https[0]._context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert update_check._NoRedirect in seen["handlers"]
    assert update_check._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://x") is None


def test_the_real_opener_stops_at_a_redirect():
    # A throwaway HTTP server on 127.0.0.1 (no internet): the opener _open builds must
    # end at the 302 as an error and never request the redirect target.
    import http.server

    paths = []

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            self.send_response(302)
            self.send_header("Location", "/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/latest"
        with pytest.raises(urllib.error.HTTPError) as error:
            REAL_OPEN(urllib.request.Request(url))
        error.value.close()
        assert error.value.code == 302
        assert paths == ["/latest"]
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- caching and the off switch

def test_the_check_is_made_at_most_once_per_process(github):
    for _ in range(5):
        assert status().json()["state"] == "update_available"
    assert len(github.requests) == 1


def test_a_failed_check_is_not_retried(github):
    github.answer = urllib.error.HTTPError(update_check.LATEST_RELEASE_API, 429, "rate limited", {}, None)
    assert status().json() == {"state": "unavailable"}
    github.answer = release()
    assert status().json() == {"state": "unavailable"}
    assert len(github.requests) == 1


def test_concurrent_first_calls_make_one_request(github):
    results = []
    threads = [threading.Thread(target=lambda: results.append(update_check.check_for_update()))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(github.requests) == 1 and len(results) == 8
    assert all(result["state"] == "update_available" for result in results)


@pytest.mark.parametrize("value", ["off", "OFF", " Off "])
def test_the_off_switch_sends_nothing(github, monkeypatch, value):
    monkeypatch.setenv("MDV_UPDATE_CHECK", value)
    assert status().json() == {"state": "disabled"}
    assert github.requests == []


@pytest.mark.parametrize("value", ["", "on", "1"])
def test_other_values_leave_the_check_on(github, monkeypatch, value):
    monkeypatch.setenv("MDV_UPDATE_CHECK", value)
    assert status().json()["state"] == "update_available"


# ---------------------------------------------------------------- endpoint protection

@pytest.mark.parametrize("headers, refused", [
    ({}, 403),                                                             # not from a page
    ({"Host": "127.0.0.1:8420"}, 403),
    ({"Origin": "http://evil.example", "Host": "127.0.0.1:8420"}, 403),     # another website
    ({"Origin": "http://evil.example:8420", "Host": "evil.example:8420"}, 400),  # DNS rebinding
    ({"Origin": "null", "Host": "127.0.0.1:8420"}, 403),
    ({"Origin": "http://127.0.0.1:8420", "Host": "127.0.0.1:9999"}, 403),
])
def test_only_the_own_loopback_page_can_trigger_the_check(github, headers, refused):
    assert status(headers=headers).status_code == refused
    assert github.requests == []


def test_the_endpoint_accepts_only_post(github):
    assert client.get("/api/update-status", headers=OWN_PAGE).status_code == 405
    assert github.requests == []


def test_the_content_security_policy_is_unchanged():
    expected = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self' http://127.0.0.1:8765 http://localhost:8765; "
                "img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
    assert status().headers["content-security-policy"] == expected
    assert client.get("/").headers["content-security-policy"] == expected


# ---------------------------------------------------------------- the page (source)

def test_the_page_asks_only_its_own_backend_and_writes_text_not_html():
    page = page_source("index.html")
    assert "fetch('/api/update-status', { method: 'POST', cache: 'no-store' })" in page
    assert "api.github.com" not in page
    start = page.index("function renderUpdateNotice(")
    render = page[start:page.index("\n}\n", start)]
    assert "innerHTML" not in render and "insertAdjacentHTML" not in render
    assert "data.url !== RELEASE_PAGE + version" in render
    # The whole update block writes text only and calls only this server's update endpoints.
    block = page[page.index("// Update notice and download."):page.index("// Initial state")]
    assert "innerHTML" not in block and "insertAdjacentHTML" not in block and "eval(" not in block
    assert set(re.findall(r"'(/api/[^']*)'", block)) == {
        "/api/update-status", "/api/update-download", "/api/update-progress", "/api/update-cancel",
        "/api/update-show"}
    assert block.count("fetch(") == 2  # postUpdate and the initial check
    for name in ("readUpdateDismissal", "saveUpdateDismissal", "renderUpdateNotice", "updateDownloadOffer"):
        start = page.index(f"function {name}(")
        body = page[start:page.index("\n}\n", start)]
        for secret in ("password", "username", "credentials", "sessionToken", "pairing", "CONNECTOR_URL",
                       "state.", "fetch"):
            assert secret not in body, (name, secret)


# ---------------------------------------------------------------- the page (headless Chrome)

UPDATE = {"state": "update_available", "current": "0.6.0", "version": "0.7.0", "url": RELEASE + "v0.7.0"}
DOWNLOAD = "https://github.com/realspinn/mysql-dba-validator/releases/download/v0.7.0/"
WINDOWS = {**UPDATE, "download": {"name": "MySQL-DBA-Validator-v0.7.0-windows-x64.zip", "size": 33361621,
                                  "platform": "Windows x64", "in_app": True,
                                  "url": DOWNLOAD + "MySQL-DBA-Validator-v0.7.0-windows-x64.zip"}}
MACOS = {**UPDATE, "download": {"name": "MySQL-DBA-Validator-v0.7.0-macos-arm64-unsigned.zip", "size": 34068693,
                                "platform": "macOS Apple Silicon", "in_app": False,
                                "url": DOWNLOAD + "MySQL-DBA-Validator-v0.7.0-macos-arm64-unsigned.zip"}}

# Drives the real page code with synthetic backend answers: fetch is replaced after the
# page's own (failing, file://) request, and every call is recorded.
HARNESS = """
<script>
const UPDATE = %s, WINDOWS = %s, MACOS = %s;
const notice = document.getElementById('updateNotice');
const byId = id => document.getElementById(id);
const shown = node => !node.hidden && getComputedStyle(node).display !== 'none';
const OUT = { initial: shown(notice) };
const settle = (ms = 60) => new Promise(resolve => setTimeout(resolve, ms));
const answers = {}, calls = [];
const view = () => ({
  stage: notice.dataset.stage, title: byId('updateTitle').textContent, text: byId('updateNoticeText').textContent,
  detail: byId('updateNoticeDetail').textContent, live: byId('updateLive').textContent,
  action: shown(byId('updateAction')) ? byId('updateAction').textContent : null,
  link: shown(byId('updateDownloadLink')) ? byId('updateDownloadLink').getAttribute('href') : null,
  secondary: byId('updateNoticeDismiss').textContent,
  indeterminate: byId('updateProgress').classList.contains('indeterminate'),
  valuenow: byId('updateProgress').getAttribute('aria-valuenow'),
  ring: byId('updateRing').style.strokeDashoffset, bar: byId('updateBar').style.transform,
  track: getComputedStyle(notice.querySelector('.update-track')).display,
});
(async () => {
  await settle();  // the page's own request fails on file:// and must leave it hidden
  OUT.afterFailedRequest = shown(notice);
  window.fetch = (path, options) => {
    calls.push([path, options && options.method, options && 'body' in options]);
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(answers[path] || null) });
  };
  renderUpdateNotice(UPDATE);
  OUT.plain = view();
  OUT.visible = shown(notice);
  OUT.href = byId('updateNoticeLink').getAttribute('href');
  OUT.rel = byId('updateNoticeLink').getAttribute('rel');
  OUT.target = byId('updateNoticeLink').getAttribute('target');
  OUT.position = getComputedStyle(notice).position;
  OUT.look = {
    heading: byId('updateTitle').tagName, labelledBy: notice.getAttribute('aria-labelledby'),
    meter: byId('updateProgress').getAttribute('role'), meterLabel: byId('updateProgress').getAttribute('aria-label'),
    live: byId('updateLive').getAttribute('role'),
    glyphsHidden: [...notice.querySelectorAll('svg')].every(svg => svg.getAttribute('aria-hidden') === 'true'),
    external: notice.querySelectorAll('img, image, use, iframe, [src]').length,
  };
  OUT.themes = {};
  for (const theme of ['light', 'dark']) {
    document.documentElement.setAttribute('data-theme', theme);
    document.getAnimations().forEach(animation => animation.finish());
    const colour = node => getComputedStyle(node).color;
    OUT.themes[theme] = {
      surface: getComputedStyle(document.documentElement).getPropertyValue('--surface').trim(),
      title: colour(byId('updateTitle')), text: colour(byId('updateNoticeText')),
      note: colour(byId('updateNoticeDetail')), link: colour(byId('updateNoticeLink')),
    };
  }
  document.documentElement.removeAttribute('data-theme');
  OUT.beforeViews = !!(notice.compareDocumentPosition(document.getElementById('dashboardView'))
                       & Node.DOCUMENT_POSITION_FOLLOWING);
  OUT.rejected = {};
  for (const [name, data] of Object.entries({
    nothing: null, unavailable: { state: 'unavailable' }, disabled: { state: 'disabled' },
    upToDate: { state: 'up_to_date' }, otherUrl: { ...UPDATE, url: 'https://evil.example/v0.7.0' },
    mismatchedUrl: { ...UPDATE, url: UPDATE.url.replace('0.7.0', '0.7.1') },
    badVersion: { ...UPDATE, version: '<img src=x onerror=alert(1)>' }, numberVersion: { ...UPDATE, version: 7 },
  })) {
    renderUpdateNotice(UPDATE);
    renderUpdateNotice(data);
    OUT.rejected[name] = shown(notice);
  }
  OUT.injected = document.querySelectorAll('#updateNotice img').length;
  // A download offer that is not exactly the official file is not offered.
  OUT.badDownloads = {};
  for (const [name, change] of Object.entries({
    otherHost: { url: 'https://evil.example/MySQL-DBA-Validator-v0.7.0-windows-x64.zip' },
    otherName: { name: 'setup.exe' }, otherPlatform: { platform: 'Linux' }, noSize: { size: 0 },
  })) {
    renderUpdateNotice({ ...WINDOWS, download: { ...WINDOWS.download, ...change } });
    OUT.badDownloads[name] = [view().action, view().link, view().detail];
  }
  renderUpdateNotice({ ...MACOS, download: { ...MACOS.download, in_app: true } });
  OUT.macInAppRefused = [view().action, view().link];

  // Windows desktop app: download, verify, ready (the backend's answers are synthetic).
  answers['/api/update-progress'] = { stage: 'idle' };
  renderUpdateNotice(WINDOWS);
  await settle();
  OUT.offer = view();
  answers['/api/update-download'] = { stage: 'downloading', version: '0.7.0', received: 0, total: 33361621 };
  answers['/api/update-progress'] = { stage: 'downloading', version: '0.7.0', received: 12345678, total: 33361621 };
  byId('updateAction').click();
  await settle(600);
  OUT.downloading = view();
  answers['/api/update-progress'] = { stage: 'downloading', version: '0.7.0', received: 5000000, total: 0 };
  await settle(600);
  OUT.unknownTotal = view();
  answers['/api/update-progress'] = { stage: 'verifying', version: '0.7.0' };
  await settle(600);
  OUT.verifying = view();
  answers['/api/update-progress'] = { stage: 'ready', version: '0.7.0', file: 'x.zip' };
  await settle(600);
  OUT.ready = view();
  OUT.pendingAfterReady = localStorage.getItem('mdv-update-pending');
  answers['/api/update-show'] = null;
  byId('updateAction').click();
  await settle();
  followUpdate({ stage: 'failed', version: '0.7.0', error: 'digest',
                 message: 'The downloaded file did not match the official release, so it was deleted.' });
  OUT.failed = view();
  followUpdate({ stage: 'cancelled', version: '0.7.0' });
  OUT.cancelled = view();
  followUpdate({ stage: 'ready', version: '9.9.9' });
  OUT.otherVersionIgnored = view().stage;
  OUT.calls = calls;

  // macOS (and any copy that is not the Windows desktop app): the browser downloads it.
  localStorage.removeItem('mdv-update-pending');
  renderUpdateNotice(MACOS);
  OUT.mac = view();
  byId('updateDownloadLink').onclick();
  OUT.pendingAfterMacLink = localStorage.getItem('mdv-update-pending');

  // The older copy opened again leaves the pending version; the new copy confirms it.
  renderUpdateNotice({ ...UPDATE, current: '0.6.0' });
  OUT.oldCopyKeepsPending = localStorage.getItem('mdv-update-pending');
  renderUpdateNotice({ state: 'up_to_date', current: '0.7.0' });
  OUT.updated = view();
  OUT.updatedShown = shown(notice);
  OUT.pendingAfterConfirm = localStorage.getItem('mdv-update-pending');
  byId('updateNoticeDismiss').click();
  OUT.afterClose = shown(notice);

  renderUpdateNotice(UPDATE);
  byId('updateNoticeDismiss').click();
  OUT.afterDismiss = shown(notice);
  OUT.stored = localStorage.getItem('mdv-update-dismissed');
  updateDismissedHere = '';  // as after a reload: only storage remembers it
  renderUpdateNotice(UPDATE);
  OUT.sameVersionAfterReload = shown(notice);
  renderUpdateNotice({ ...UPDATE, version: '0.8.0', url: UPDATE.url.replace('0.7.0', '0.8.0') });
  OUT.newerVersionShown = shown(notice);
  // Storage unavailable (blocked, private mode): the page still works.
  Storage.prototype.getItem = () => { throw new Error('blocked') };
  Storage.prototype.setItem = () => { throw new Error('blocked') };
  Storage.prototype.removeItem = () => { throw new Error('blocked') };
  const blocked = { ...UPDATE, version: '0.9.0', url: UPDATE.url.replace('0.7.0', '0.9.0') };
  OUT.blocked = {};
  try {
    renderUpdateNotice(blocked);
    OUT.blocked.shown = shown(notice);
    byId('updateNoticeDismiss').click();
    OUT.blocked.afterDismiss = shown(notice);
    renderUpdateNotice(blocked);
    OUT.blocked.samePage = shown(notice);
  } catch (error) { OUT.blocked.error = String(error) }
  const out = document.createElement('pre');
  out.id = 'update-out';
  out.textContent = JSON.stringify(OUT);
  document.body.append(out);
})();
</script>
</body>"""


@pytest.fixture(scope="module")
def banner(tmp_path_factory):
    chrome = find_chrome()
    if chrome is None:
        pytest.skip("Chrome is not installed")
    work = tmp_path_factory.mktemp("update")
    harness = work / "harness.html"
    page = page_source("index.html")
    script = HARNESS % (json.dumps(UPDATE), json.dumps(WINDOWS), json.dumps(MACOS))
    harness.write_text(page.replace("</body>", script, 1), encoding="utf-8")
    document, stderr, code = dump_dom(
        [chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
         "--virtual-time-budget=15000", f"--user-data-dir={work / 'profile'}", "--dump-dom", harness.as_uri()],
        work)
    match = re.search(r'<pre id="update-out">(.*?)</pre>', document, re.S)
    assert match, f"page did not render (exit {code}): {stderr[-500:]}"
    return json.loads(html.unescape(match.group(1)))


def test_the_banner_is_hidden_until_a_newer_release_is_reported(banner):
    assert banner["initial"] is False and banner["afterFailedRequest"] is False
    assert banner["visible"] is True
    assert banner["plain"]["text"] == "MySQL DBA Validator 0.7.0 · you have 0.6.0"


def test_without_a_file_for_this_computer_the_banner_links_to_the_release_page(banner):
    plain = banner["plain"]
    assert plain["title"] == "Update available" and plain["stage"] == "available"
    assert plain["detail"] == "Download it from the release notes page."
    assert plain["action"] is None and plain["link"] is None and plain["secondary"] == "Later"


def test_the_banner_is_an_accessible_region_with_a_progress_meter(banner):
    assert banner["look"] == {
        "heading": "H2", "labelledBy": "updateTitle", "meter": "progressbar",
        "meterLabel": "Update download", "live": "status", "glyphsHidden": True, "external": 0,
    }


def test_the_banner_reads_well_in_light_and_dark_themes(banner):
    from tests.test_frontend_evidence_rendering import contrast

    themes = banner["themes"]
    assert themes["light"]["surface"] != themes["dark"]["surface"]
    for theme, look in themes.items():
        for part in ("title", "text", "note", "link"):
            assert contrast(look[part], look["surface"]) >= 4.5, (theme, part)
    for part in ("title", "text", "link"):
        assert themes["light"][part] != themes["dark"][part], part


def test_the_banner_links_to_the_official_release_notes_and_does_not_cover_the_review(banner):
    assert banner["href"] == RELEASE + "v0.7.0"
    assert banner["target"] == "_blank" and banner["rel"] == "noopener noreferrer"
    assert banner["position"] == "static" and banner["beforeViews"] is True


def test_the_banner_is_never_shown_for_an_unavailable_or_untrusted_answer(banner):
    assert banner["rejected"] == {name: False for name in banner["rejected"]}
    assert len(banner["rejected"]) == 8
    assert banner["injected"] == 0


def test_only_the_exact_official_file_is_offered_for_download(banner):
    for name, (action, link, detail) in banner["badDownloads"].items():
        assert action is None and link is None, name
        assert detail == "Download it from the release notes page.", name
    # Only the Windows desktop app downloads in the app; a macOS file never does.
    assert banner["macInAppRefused"] == [None, MACOS["download"]["url"]]


def test_the_windows_app_offers_an_update_with_platform_and_size(banner):
    offer = banner["offer"]
    assert offer["stage"] == "available" and offer["action"] == "Update" and offer["link"] is None
    assert offer["detail"] == "Windows x64 · 33.4 MB"
    assert offer["secondary"] == "Later" and offer["track"] == "none"


def test_download_progress_shows_the_real_bytes(banner):
    down = banner["downloading"]
    assert down["title"] == "Downloading update" and down["stage"] == "downloading"
    assert down["detail"] == "12.3 MB of 33.4 MB · 37%"
    assert down["valuenow"] == "37" and down["indeterminate"] is False
    assert float(down["ring"]) == pytest.approx(100 - 12345678 / 33361621 * 100)
    assert down["bar"].startswith("scaleX(0.37")
    assert down["track"] == "block"
    assert down["action"] is None and down["secondary"] == "Cancel"
    assert down["live"] == "Downloading update. MySQL DBA Validator 0.7.0"


def test_an_unknown_total_shows_no_percentage(banner):
    unknown = banner["unknownTotal"]
    assert unknown["indeterminate"] is True and unknown["valuenow"] is None
    assert unknown["detail"] == "5.0 MB" and "%" not in unknown["detail"]


def test_verification_is_a_separate_unmeasured_stage(banner):
    verify = banner["verifying"]
    assert verify["title"] == "Verifying download" and verify["stage"] == "verifying"
    assert verify["indeterminate"] is True and verify["valuenow"] is None
    assert verify["detail"] == "Checking the size, the SHA-256 digest from GitHub and the archive contents."
    assert verify["secondary"] == "Cancel" and verify["track"] == "none"


def test_ready_to_install_says_what_the_user_does_and_never_claims_installed(banner):
    ready = banner["ready"]
    assert ready["title"] == "Ready to install" and ready["text"] == "MySQL DBA Validator 0.7.0 · verified"
    assert ready["detail"] == ("Close MySQL DBA Validator, extract the ZIP, then open MySQL-DBA-Validator.exe "
                               "in the new folder. Your saved settings are kept.")
    assert ready["action"] == "Show in folder" and ready["valuenow"] == "100"
    assert banner["pendingAfterReady"] == "0.7.0"
    for stage in ("offer", "downloading", "verifying", "ready", "failed", "cancelled", "mac"):
        assert "nstalled" not in banner[stage]["title"] + banner[stage]["detail"], stage


def test_failure_and_cancellation_offer_a_retry(banner):
    failed = banner["failed"]
    assert failed["title"] == "Update not downloaded" and failed["action"] == "Try again"
    assert failed["detail"] == "The downloaded file did not match the official release, so it was deleted."
    cancelled = banner["cancelled"]
    assert cancelled["stage"] == "available" and cancelled["action"] == "Update"
    assert cancelled["detail"] == "Download cancelled. Nothing was changed."
    assert banner["otherVersionIgnored"] == "available"


def test_the_page_calls_only_its_own_update_endpoints_with_bodyless_posts(banner):
    paths = {path for path, method, has_body in banner["calls"]}
    assert paths == {"/api/update-progress", "/api/update-download", "/api/update-show"}
    assert all(method == "POST" and has_body is False for path, method, has_body in banner["calls"])


def test_other_copies_link_to_the_official_file_for_the_browser_to_download(banner):
    mac = banner["mac"]
    assert mac["action"] is None and mac["link"] == MACOS["download"]["url"]
    assert mac["detail"] == "macOS Apple Silicon · 34.1 MB · downloaded by your browser"
    assert banner["pendingAfterMacLink"] == "0.7.0"


def test_the_new_copy_confirms_the_version_it_runs(banner):
    assert banner["oldCopyKeepsPending"] == "0.7.0"
    updated = banner["updated"]
    assert banner["updatedShown"] is True and updated["stage"] == "updated"
    assert updated["title"] == "Update complete" and updated["text"] == "MySQL DBA Validator 0.7.0 is running."
    assert banner["pendingAfterConfirm"] is None and banner["afterClose"] is False


def test_dismissal_is_remembered_per_version(banner):
    assert banner["afterDismiss"] is False
    assert banner["stored"] == "0.7.0"
    assert banner["sameVersionAfterReload"] is False
    assert banner["newerVersionShown"] is True


def test_the_banner_works_when_browser_storage_is_unavailable(banner):
    assert banner["blocked"] == {"shown": True, "afterDismiss": False, "samePage": False}
