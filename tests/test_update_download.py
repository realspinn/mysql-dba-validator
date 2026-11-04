"""Download and verification of a newer release: backend/update_download.py, the asset
selection in backend/update_check.py and the /api/update-* endpoints.

No test contacts GitHub or downloads a real release: the release metadata is synthetic,
the "download" is a fake response serving a ZIP built here, and files are written only
to a temporary folder. Nothing is installed or run in any test.
"""

from __future__ import annotations

import errno
import hashlib
import io
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import backend.update_check as update_check
import backend.update_download as update_download
from backend.main import app
from backend.version import APP_VERSION

client = TestClient(app, base_url="http://127.0.0.1:8420")
OWN_PAGE = {"Origin": "http://127.0.0.1:8420", "Host": "127.0.0.1:8420"}
FOREIGN = {"Origin": "http://evil.example", "Host": "127.0.0.1:8420"}
VERSION = "99.0.0"
NAME = f"MySQL-DBA-Validator-v{VERSION}-windows-x64.zip"
TOP = NAME[:-len(".zip")]
URL = f"https://github.com/realspinn/mysql-dba-validator/releases/download/v{VERSION}/{NAME}"
REAL_OPEN = update_download._open  # the autouse fixture replaces both in every test
REAL_MARK = update_download.mark_from_internet
PAYLOAD = os.urandom(300 * 1024)


def release_zip(entries=None, top=TOP) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in (entries if entries is not None else {
                f"{top}/MySQL-DBA-Validator.exe": b"MZ" + b"\0" * 4096,
                # Random bytes do not compress, so the ZIP takes several reads to download.
                f"{top}/_internal/base_library.zip": PAYLOAD}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def asset_for(body: bytes, **changes) -> dict:
    asset = {"name": NAME, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "url": URL,
             "platform": "Windows x64", "version": VERSION}
    asset.update(changes)
    return asset


class FakeResponse:
    def __init__(self, body: bytes, length=None, status=200, gate=None, fail_at=None):
        self.stream = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Length": str(len(body) if length is None else length)}
        self.gate, self.fail_at, self.reads = gate, fail_at, 0

    def read(self, size):
        self.reads += 1
        if self.gate is not None and self.reads == 2:
            self.gate.wait(5)  # hold the download until the test has cancelled it
        if self.fail_at is not None and self.reads == self.fail_at:
            raise self.fail_at_error
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeDownloads:
    """Stands in for update_download._open: records requests, returns one response."""

    def __init__(self):
        self.requests: list[urllib.request.Request] = []
        self.response = None

    def __call__(self, request):
        self.requests.append(request)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    fake = FakeDownloads()
    monkeypatch.setattr(update_download, "_open", fake)
    monkeypatch.setattr(update_download, "updates_dir", lambda: tmp_path / "updates")
    monkeypatch.setattr(update_download, "_state", {"stage": "idle"})
    update_download._cancel.clear()
    marks = []
    monkeypatch.setattr(update_download, "mark_from_internet", lambda path, url: marks.append((path.name, url)))
    fake.marks = marks
    fake.folder = tmp_path / "updates"
    yield fake
    update_download._cancel.set()  # stop a test's leftover thread at its next read


def finish(timeout=10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = update_download.snapshot()
        if state["stage"] not in update_download.ACTIVE:
            return state
        time.sleep(0.01)
    raise AssertionError(f"download did not finish: {update_download.snapshot()}")


def leftovers(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


# ------------------------------------------------------------------ asset selection

def api_asset(**changes) -> dict:
    asset = {"name": NAME, "size": 33361621, "state": "uploaded", "browser_download_url": URL,
             "digest": "sha256:" + "a" * 64, "content_type": "application/zip"}
    asset.update(changes)
    return asset


def test_the_exact_official_windows_file_is_selected():
    found = update_check.platform_asset({"assets": [api_asset(name=NAME + ".sha256"), api_asset()]}, VERSION,
                                        update_check.WINDOWS)
    assert found == {"name": NAME, "size": 33361621, "sha256": "a" * 64, "url": URL,
                     "platform": "Windows x64", "version": VERSION}


@pytest.mark.parametrize("changes", [
    {"browser_download_url": "https://evil.example/" + NAME},
    {"browser_download_url": URL.replace("https://", "http://")},
    {"state": "starter"}, {"state": None},
    {"digest": None}, {"digest": "sha256:" + "A" * 64}, {"digest": "md5:" + "a" * 32}, {"digest": "sha256:abc"},
    {"size": 10}, {"size": 400 * 1024 * 1024}, {"size": "33361621"}, {"size": True}, {"size": None},
])
def test_a_file_that_is_not_exactly_as_expected_is_not_offered(changes):
    assert update_check.platform_asset({"assets": [api_asset(**changes)]}, VERSION, update_check.WINDOWS) is None


@pytest.mark.parametrize("assets", [None, "x", [], [api_asset(), api_asset()], [api_asset(name="other.zip")]])
def test_missing_or_duplicate_files_are_not_offered(assets):
    assert update_check.platform_asset({"assets": assets}, VERSION, update_check.WINDOWS) is None


@pytest.mark.parametrize("system, machine, expected", [
    ("win32", "AMD64", update_check.WINDOWS), ("win32", "ARM64", update_check.WINDOWS),
    ("darwin", "arm64", update_check.MACOS), ("darwin", "x86_64", None), ("linux", "x86_64", None),
])
def test_the_build_for_this_computer(system, machine, expected):
    assert update_check.platform_build(system, machine) == expected


def test_an_unsupported_computer_gets_no_file(monkeypatch):
    monkeypatch.setattr(update_check, "platform_build", lambda *a: None)  # e.g. an Intel Mac
    assert update_check.platform_asset({"assets": [api_asset()]}, VERSION) is None


def test_only_the_packaged_windows_app_downloads_in_the_app(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local")
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert update_check.in_app_download_supported() is False  # run from source
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert update_check.in_app_download_supported() is True
    monkeypatch.delenv("LOCALAPPDATA")
    assert update_check.in_app_download_supported() is False
    monkeypatch.setenv("LOCALAPPDATA", "C:\\Users\\x\\AppData\\Local")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert update_check.in_app_download_supported() is False


def test_the_status_reports_the_file_without_its_digest(monkeypatch):
    release = {"tag_name": f"v{VERSION}", "html_url": update_check.RELEASE_PAGE + f"v{VERSION}",
               "draft": False, "prerelease": False, "assets": [api_asset()]}

    class Answer:
        status = 200

        def read(self, limit=-1):
            import json
            return json.dumps(release).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(update_check, "_open", lambda request: Answer())
    monkeypatch.setattr(update_check, "_result", None)
    monkeypatch.setattr(update_check, "platform_build", lambda *a: update_check.WINDOWS)
    monkeypatch.delenv(update_check.OFF_SWITCH, raising=False)
    body = client.post("/api/update-status", headers=OWN_PAGE).json()
    assert body["download"] == {"name": NAME, "size": 33361621, "platform": "Windows x64", "url": URL,
                                "in_app": False}
    assert "a" * 64 not in str(body)
    assert update_check.checked_asset()["sha256"] == "a" * 64


# ------------------------------------------------------------------ download and verification

def test_a_verified_download_is_ready_and_marked_as_from_the_internet(isolated):
    body = release_zip()
    isolated.response = FakeResponse(body)
    assert update_download.start(asset_for(body)) is True
    state = finish()
    assert state == {"stage": "ready", "version": VERSION, "received": len(body), "total": len(body), "file": NAME}
    assert "path" not in state
    assert leftovers(isolated.folder) == [NAME]
    assert (isolated.folder / NAME).read_bytes() == body
    assert isolated.marks == [(NAME, URL)]
    request = isolated.requests[0]
    assert request.full_url == URL and request.get_method() == "GET" and request.data is None
    assert set(dict(request.header_items())) == {"Accept", "User-agent"}


@pytest.mark.parametrize("response, code", [
    (lambda body: FakeResponse(body, length=len(body) + 1), "incomplete"),
    (lambda body: FakeResponse(body, length="abc"), "incomplete"),
    (lambda body: FakeResponse(body[:-10], length=len(body)), "incomplete"),  # cut short
    (lambda body: FakeResponse(body + b"extra", length=len(body)), "incomplete"),  # longer than declared
    (lambda body: FakeResponse(body, status=206), "incomplete"),
])
def test_a_wrong_or_incomplete_size_fails_and_leaves_nothing(isolated, response, code):
    body = release_zip()
    isolated.response = response(body)
    update_download.start(asset_for(body))
    state = finish()
    assert state["stage"] == "failed" and state["error"] == code
    assert state["message"] == update_download.MESSAGES[code]
    assert leftovers(isolated.folder) == [] and isolated.marks == []


def test_a_digest_mismatch_deletes_the_file(isolated):
    body = release_zip()
    isolated.response = FakeResponse(body)
    update_download.start(asset_for(body, sha256="0" * 64))
    state = finish()
    assert state["error"] == "digest" and leftovers(isolated.folder) == []


@pytest.mark.parametrize("entries", [
    {f"{TOP}/MySQL-DBA-Validator.exe": b"MZ", "../evil.exe": b"x"},
    {f"{TOP}/MySQL-DBA-Validator.exe": b"MZ", f"{TOP}/../../evil.exe": b"x"},
    {f"{TOP}/MySQL-DBA-Validator.exe": b"MZ", "/etc/evil": b"x"},
    {f"{TOP}/MySQL-DBA-Validator.exe": b"MZ", "C:/Windows/evil.exe": b"x"},
    {f"{TOP}/MySQL-DBA-Validator.exe": b"MZ", f"{TOP}\\..\\evil.exe": b"x"},
    {f"{TOP}/MySQL-DBA-Validator.exe": b"MZ", "Other-Folder/x.exe": b"x"},
    {f"{TOP}/README.txt": b"no app inside"},
    {},
])
def test_an_unsafe_or_wrong_archive_is_refused(isolated, entries):
    body = release_zip(entries)
    isolated.response = FakeResponse(body)
    update_download.start(asset_for(body))
    state = finish()
    assert state["error"] == "archive" and leftovers(isolated.folder) == []


def test_a_corrupt_archive_is_refused(isolated):
    body = bytearray(release_zip())
    body[60:70] = b"\xff" * 10  # damage compressed data inside the first entry
    body = bytes(body)
    isolated.response = FakeResponse(body)
    update_download.start(asset_for(body))
    assert finish()["error"] == "archive"
    assert leftovers(isolated.folder) == []


def test_not_enough_disk_space_fails_before_downloading(isolated, monkeypatch):
    body = release_zip()
    monkeypatch.setattr(update_download.shutil, "disk_usage", lambda path: type("U", (), {"free": len(body)})())
    isolated.response = FakeResponse(body)
    update_download.start(asset_for(body))
    assert finish()["error"] == "disk_space"
    assert isolated.requests == []


def test_a_full_disk_while_writing_is_reported_as_disk_space(isolated):
    body = release_zip()
    response = FakeResponse(body, fail_at=2)
    response.fail_at_error = OSError(errno.ENOSPC, "No space left on device")
    isolated.response = response
    update_download.start(asset_for(body))
    assert finish()["error"] == "disk_space"
    assert leftovers(isolated.folder) == []


@pytest.mark.parametrize("error", [
    urllib.error.URLError("offline"), socket.timeout("timed out"), ConnectionResetError(),
    urllib.error.HTTPError(URL, 404, "Not Found", {}, None),
    update_download.UpdateFailure("network"),
])
def test_network_failures_are_reported_and_can_be_retried(isolated, error):
    body = release_zip()
    isolated.response = error
    update_download.start(asset_for(body))
    assert finish()["error"] == "network"
    isolated.response = FakeResponse(body)
    assert update_download.start(asset_for(body)) is True
    assert finish()["stage"] == "ready"


def test_an_interrupted_download_fails_without_a_partial_file(isolated):
    body = release_zip()
    response = FakeResponse(body, fail_at=3)
    response.fail_at_error = ConnectionResetError("reset")
    isolated.response = response
    update_download.start(asset_for(body))
    assert finish()["error"] == "network"
    assert leftovers(isolated.folder) == []


def test_cancel_stops_the_download_and_a_retry_starts_again(isolated):
    body = release_zip()
    gate = threading.Event()
    isolated.response = FakeResponse(body, gate=gate)
    update_download.start(asset_for(body))
    deadline = time.monotonic() + 5
    while update_download.snapshot()["received"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    # Under way: a second start is refused, so there are never two downloads.
    assert update_download.start(asset_for(body)) is False
    update_download.cancel()
    gate.set()
    assert finish()["stage"] == "cancelled"
    assert leftovers(isolated.folder) == []
    isolated.response = FakeResponse(body)
    assert update_download.start(asset_for(body)) is True
    assert finish()["stage"] == "ready"


def test_progress_is_the_bytes_actually_received(isolated):
    body = release_zip()
    gate = threading.Event()
    isolated.response = FakeResponse(body, gate=gate)
    update_download.start(asset_for(body))
    deadline = time.monotonic() + 5
    while update_download.snapshot()["received"] == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    partial = update_download.snapshot()
    assert partial["stage"] == "downloading"
    assert partial["received"] == update_download.CHUNK_BYTES and partial["total"] == len(body)
    gate.set()
    assert finish()["received"] == len(body)


def test_a_verified_download_is_not_fetched_again(isolated):
    body = release_zip()
    isolated.response = FakeResponse(body)
    update_download.start(asset_for(body))
    finish()
    assert update_download.start(asset_for(body)) is True
    assert update_download.snapshot()["stage"] == "ready" and len(isolated.requests) == 1


def test_a_new_download_removes_earlier_ones_but_nothing_else(isolated):
    isolated.folder.mkdir(parents=True)
    old = isolated.folder / "MySQL-DBA-Validator-v98.0.0-windows-x64.zip"
    other = isolated.folder / "notes.txt"
    old.write_bytes(b"old")
    other.write_bytes(b"keep")
    body = release_zip()
    isolated.response = FakeResponse(body)
    update_download.start(asset_for(body))
    finish()
    assert leftovers(isolated.folder) == [NAME, "notes.txt"]


def test_installed_or_older_downloads_are_removed(tmp_path):
    current = tuple(int(part) for part in APP_VERSION.split("."))
    older = f"MySQL-DBA-Validator-v{current[0]}.{current[1]}.{current[2]}-windows-x64.zip"
    newer = f"MySQL-DBA-Validator-v{current[0] + 1}.0.0-windows-x64.zip"
    for name in (older, older + ".part", "MySQL-DBA-Validator-v0.1.0-windows-x64.zip", newer, "keep.txt"):
        (tmp_path / name).write_bytes(b"x")
    update_download.remove_installed_downloads(tmp_path)
    assert leftovers(tmp_path) == sorted([newer, "keep.txt"])
    update_download.remove_installed_downloads(tmp_path / "missing")  # no folder: nothing to do


@pytest.mark.parametrize("target, allowed", [
    ("https://release-assets.githubusercontent.com/github-production-release-asset/1/2", True),
    ("https://objects.githubusercontent.com/x", True),
    ("http://release-assets.githubusercontent.com/x", False),
    ("https://evil.example/x", False),
    ("https://release-assets.githubusercontent.com.evil.example/x", False),
    ("file:///C:/Windows/notepad.exe", False),
])
def test_redirects_go_only_to_githubs_download_hosts_over_https(target, allowed):
    handler = update_download._GitHubRedirects()
    request = urllib.request.Request(URL)
    if allowed:
        assert handler.redirect_request(request, None, 302, "Found", {}, target).full_url == target
    else:
        with pytest.raises(update_download.UpdateFailure):
            handler.redirect_request(request, None, 302, "Found", {}, target)


def test_the_real_opener_verifies_tls_and_uses_the_redirect_check(monkeypatch):
    built = {}

    def build_opener(*handlers):
        built["handlers"] = handlers
        raise RuntimeError("stop before any network")

    monkeypatch.setattr(update_download.urllib.request, "build_opener", build_opener)
    with pytest.raises(RuntimeError):
        REAL_OPEN(urllib.request.Request(URL))
    redirect, https = built["handlers"]
    assert redirect is update_download._GitHubRedirects
    assert https._context.verify_mode.name == "CERT_REQUIRED" and https._context.check_hostname is True


@pytest.mark.skipif(sys.platform != "win32", reason="Mark of the Web is an NTFS alternate data stream")
def test_the_mark_of_the_web_is_written_and_read_back(tmp_path):
    path = tmp_path / NAME
    path.write_bytes(b"zip")
    REAL_MARK(path, URL)
    with open(f"{path}:Zone.Identifier", encoding="ascii", newline="") as stream:
        assert stream.read() == f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={URL}\r\n"
    assert path.read_bytes() == b"zip"


def test_show_in_folder_only_selects_a_verified_file(isolated, monkeypatch):
    started = []
    monkeypatch.setattr(update_download.subprocess, "Popen", lambda command: started.append(command))
    assert update_download.show_in_folder() is False and started == []
    body = release_zip()
    isolated.response = FakeResponse(body)
    update_download.start(asset_for(body))
    finish()
    assert update_download.show_in_folder() is True
    explorer = Path(update_download.os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
    assert started == [f'"{explorer}" /select,"{isolated.folder / NAME}"']


# ------------------------------------------------------------------ endpoints

ENDPOINTS = ["/api/update-download", "/api/update-progress", "/api/update-cancel", "/api/update-show"]


@pytest.mark.parametrize("path", ENDPOINTS)
@pytest.mark.parametrize("headers", [FOREIGN, {"Host": "127.0.0.1:8420"},
                                     {"Origin": "http://evil.example:8420", "Host": "evil.example:8420"}])
def test_only_the_own_loopback_page_can_use_the_update_endpoints(isolated, path, headers):
    response = client.post(path, headers=headers)
    assert response.status_code in (400, 403)
    assert isolated.requests == []


@pytest.mark.parametrize("path", ENDPOINTS)
def test_the_update_endpoints_accept_only_post(path):
    assert client.get(path, headers=OWN_PAGE).status_code == 405


def test_nothing_is_downloaded_where_the_app_does_not_download_itself(isolated, monkeypatch):
    monkeypatch.setattr(update_check, "in_app_download_supported", lambda: False)
    monkeypatch.setattr(update_check, "_asset", asset_for(release_zip()))
    response = client.post("/api/update-download", headers=OWN_PAGE)
    assert response.status_code == 409 and response.json() == {"error": "update_download_unavailable"}
    assert client.post("/api/update-show", headers=OWN_PAGE).status_code == 409
    assert isolated.requests == []


def test_without_a_checked_file_nothing_is_downloaded(isolated, monkeypatch):
    monkeypatch.setattr(update_check, "in_app_download_supported", lambda: True)
    monkeypatch.setattr(update_check, "_asset", None)
    assert client.post("/api/update-download", headers=OWN_PAGE).status_code == 409
    assert isolated.requests == []


def test_the_endpoints_download_verify_and_show_the_file(isolated, monkeypatch):
    body = release_zip()
    isolated.response = FakeResponse(body)
    monkeypatch.setattr(update_check, "in_app_download_supported", lambda: True)
    monkeypatch.setattr(update_check, "_asset", asset_for(body))
    started = []
    monkeypatch.setattr(update_download.subprocess, "Popen", lambda command: started.append(command))
    first = client.post("/api/update-download", headers=OWN_PAGE)
    assert first.status_code == 200 and first.json()["stage"] in ("downloading", "verifying", "ready")
    finish()
    assert client.post("/api/update-progress", headers=OWN_PAGE).json()["stage"] == "ready"
    assert client.post("/api/update-show", headers=OWN_PAGE).status_code == 204
    assert len(started) == 1
    # Ignores anything sent: the endpoints take no data.
    assert client.post("/api/update-cancel", headers=OWN_PAGE, json={"url": "https://evil.example"}).json()[
        "stage"] == "ready"
    assert len(isolated.requests) == 1 and isolated.requests[0].full_url == URL


def test_a_second_start_while_downloading_is_refused(isolated, monkeypatch):
    body = release_zip()
    gate = threading.Event()
    isolated.response = FakeResponse(body, gate=gate)
    monkeypatch.setattr(update_check, "in_app_download_supported", lambda: True)
    monkeypatch.setattr(update_check, "_asset", asset_for(body))
    assert client.post("/api/update-download", headers=OWN_PAGE).status_code == 200
    second = client.post("/api/update-download", headers=OWN_PAGE)
    assert second.status_code == 409 and second.json() == {"error": "update_download_running"}
    client.post("/api/update-cancel", headers=OWN_PAGE)
    gate.set()
    assert finish()["stage"] == "cancelled"
    assert len(isolated.requests) == 1
