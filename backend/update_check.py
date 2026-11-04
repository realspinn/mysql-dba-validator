"""Whether a newer official release exists, and which of its files fits this computer.

One fixed GET, with no body, to the official repository's latest-release endpoint on the
GitHub REST API, at most once per backend start; the result is kept in memory. The
request carries only the fixed headers below: no SQL, login, target, schema or other
connection details. GitHub sees the requester's IP address and this User-Agent (which
names the installed version). Nothing is downloaded or installed here; the Windows
desktop app can download and verify the file with backend.update_download.

The file for this computer is the release asset with exactly the expected name
(Windows x64 or macOS Apple Silicon), fully uploaded, of a plausible size, with a
SHA-256 digest from GitHub and the official download address for that name. Without
one, the notice links to the release page only.

The latest release is GitHub's most recent published release that is not a draft or a
prerelease. It is reported as newer only when its tag is a strict vX.Y.Z, numerically
greater than APP_VERSION, and its page is exactly the official release page for that tag.
Anything else (offline, timeout, failed TLS verification, any HTTP error including 403,
404 and 429 rate limiting, a redirect, an unexpected response) is "unavailable", never
"up to date". MDV_UPDATE_CHECK=off turns the check off: nothing is sent.
"""

from __future__ import annotations

import json
import os
import platform
import re
import ssl
import sys
import threading
import urllib.request
from typing import Any, Optional

from .version import APP_VERSION

REPOSITORY = "realspinn/mysql-dba-validator"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_PAGE = f"https://github.com/{REPOSITORY}/releases/tag/"
DOWNLOAD_BASE = f"https://github.com/{REPOSITORY}/releases/download/"
# Release archives are about 35 MB; anything far outside that is not a release file.
MIN_ASSET_BYTES = 1024 * 1024
MAX_ASSET_BYTES = 300 * 1024 * 1024
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})\Z")
REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2026-03-10",
    "User-Agent": f"MySQL-DBA-Validator/{APP_VERSION}",
}
TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 256 * 1024
OFF_SWITCH = "MDV_UPDATE_CHECK"

UPDATE_AVAILABLE = "update_available"
UP_TO_DATE = "up_to_date"
UNAVAILABLE = "unavailable"
DISABLED = "disabled"

# No leading zeros, so the version text and its numbers map one to one.
_NUMBER = r"(0|[1-9][0-9]{0,3})"
_VERSION = re.compile(rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}\Z")

_lock = threading.Lock()
_result: Optional[dict] = None
# The checked file for this computer, with its digest, for backend.update_download.
_asset: Optional[dict] = None

# Where this build runs -> (asset name suffix, how it is shown). The Windows build is
# x64 only (it also runs on Windows on Arm through emulation); the macOS build is Apple
# Silicon only.
WINDOWS = ("windows-x64", "Windows x64")
MACOS = ("macos-arm64-unsigned", "macOS Apple Silicon")


def platform_build(system: Optional[str] = None, machine: Optional[str] = None) -> Optional[tuple[str, str]]:
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if system == "win32":
        return WINDOWS
    if system == "darwin" and machine == "arm64":
        return MACOS
    return None


def in_app_download_supported() -> bool:
    """Only the packaged Windows app downloads an update itself (backend.update_download).

    Everywhere else the page links to the file and the browser downloads it, so the
    operating system's own checks for downloaded files (Gatekeeper on macOS) apply.
    """
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False)) and bool(os.environ.get("LOCALAPPDATA"))


def enabled() -> bool:
    return os.environ.get(OFF_SWITCH, "").strip().lower() != "off"


def check_for_update() -> dict:
    """The update state for the page; the first call per process does the one request."""
    global _result
    if not enabled():
        return {"state": DISABLED}
    with _lock:  # concurrent first calls wait for the one request instead of repeating it
        if _result is None:
            _result = _check()
        return dict(_result)


def _version(text: Any) -> Optional[tuple[int, int, int]]:
    match = _VERSION.match(text) if isinstance(text, str) else None
    return tuple(int(part) for part in match.groups()) if match else None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect is not followed: it ends as an HTTP error (unavailable)."""

    def redirect_request(self, *args, **kwargs):
        return None


def _open(request: urllib.request.Request):
    # Certificate and host name verification: ssl.create_default_context() requires both.
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))
    return opener.open(request, timeout=TIMEOUT_SECONDS)


def _check() -> dict:
    current = _version(APP_VERSION)
    if current is None:
        return {"state": UNAVAILABLE}
    request = urllib.request.Request(LATEST_RELEASE_API, headers=REQUEST_HEADERS, method="GET")
    try:
        with _open(request) as response:
            if response.status != 200:
                return {"state": UNAVAILABLE}
            body = response.read(MAX_RESPONSE_BYTES + 1)
        release = json.loads(body) if len(body) <= MAX_RESPONSE_BYTES else None
    except Exception:  # network, TLS, HTTP (403/404/429, redirect) or JSON failure
        return {"state": UNAVAILABLE}
    if (not isinstance(release, dict) or release.get("draft") is not False
            or release.get("prerelease") is not False):
        return {"state": UNAVAILABLE}
    tag = release.get("tag_name")
    latest = _version(tag[1:]) if isinstance(tag, str) and tag.startswith("v") else None
    if latest is None or release.get("html_url") != RELEASE_PAGE + tag:
        return {"state": UNAVAILABLE}
    if latest <= current:
        # The installed version, so a freshly updated copy can confirm what it runs.
        return {"state": UP_TO_DATE, "current": APP_VERSION}
    result = {"state": UPDATE_AVAILABLE, "current": APP_VERSION, "version": tag[1:], "url": RELEASE_PAGE + tag}
    global _asset
    _asset = platform_asset(release, tag[1:])
    if _asset is not None:
        result["download"] = {
            "name": _asset["name"], "size": _asset["size"], "platform": _asset["platform"],
            "url": _asset["url"], "in_app": in_app_download_supported(),
        }
    return result


def platform_asset(release: dict, version: str, build: Optional[tuple[str, str]] = None) -> Optional[dict]:
    """The release file for this computer, or None when there is not exactly one valid one."""
    build = platform_build() if build is None else build
    assets = release.get("assets")
    if build is None or not isinstance(assets, list):
        return None
    name = f"MySQL-DBA-Validator-v{version}-{build[0]}.zip"
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == name]
    if len(matches) != 1:
        return None
    asset = matches[0]
    size = asset.get("size")
    digest = _DIGEST.match(asset.get("digest")) if isinstance(asset.get("digest"), str) else None
    url = DOWNLOAD_BASE + f"v{version}/{name}"
    if (asset.get("state") != "uploaded" or type(size) is not int
            or not MIN_ASSET_BYTES <= size <= MAX_ASSET_BYTES
            or digest is None or asset.get("browser_download_url") != url):
        return None
    return {"name": name, "size": size, "sha256": digest.group(1), "url": url,
            "platform": build[1], "version": version}


def checked_asset() -> Optional[dict]:
    """The file found by the last check (with its digest), for backend.update_download."""
    with _lock:
        return dict(_asset) if _asset is not None else None
