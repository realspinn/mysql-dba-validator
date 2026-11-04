"""Downloads and verifies a newer official release for the Windows desktop app.

It never installs or runs anything. The user installs the verified ZIP the same way as
a first download: close the app, extract the ZIP, open the new folder. That keeps
Windows' own checks: the ZIP is marked as downloaded from the internet (Mark of the
Web), so Windows SmartScreen checks the extracted program as it would after a browser
download. The running app's files are never touched.

Only the file found by backend.update_check is fetched: its name, size, SHA-256 digest
and download address come from GitHub's release API over verified HTTPS, and must match
exactly. Redirects are followed only over HTTPS to GitHub's own download hosts. The
bytes are counted as they arrive (the progress the page shows), the size must match,
the SHA-256 digest must match GitHub's, and the archive must hold one release folder
with no unsafe paths. A file that fails any check is deleted. The digest proves the file
is the one published on the official release, not who built it: the app is not code
signed.

Files live only in <LOCALAPPDATA>\\MySQLDBAValidator\\updates. A new download first
removes earlier ones, and files for the installed version or older are removed when the
app checks for updates. No SQL, login or connection detail is involved.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import shutil
import ssl
import subprocess
import threading
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Optional

from .version import APP_VERSION

DOWNLOAD_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com",
                            "objects.githubusercontent.com"})
READ_TIMEOUT_SECONDS = 30
CHUNK_BYTES = 64 * 1024
FREE_SPACE_MARGIN_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 20000
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
APP_EXE = "MySQL-DBA-Validator.exe"
_OWN_FILE = re.compile(r"MySQL-DBA-Validator-v(\d{1,4})\.(\d{1,4})\.(\d{1,4})-windows-x64\.zip(\.part)?\Z")

IDLE, DOWNLOADING, VERIFYING, READY, FAILED, CANCELLED = (
    "idle", "downloading", "verifying", "ready", "failed", "cancelled")
ACTIVE = (DOWNLOADING, VERIFYING)

MESSAGES = {
    "network": "The download stopped. Check the internet connection and try again.",
    "incomplete": "The download was incomplete. Try again.",
    "digest": "The downloaded file did not match the official release, so it was deleted.",
    "archive": "The downloaded file is not a valid release package, so it was deleted.",
    "disk_space": "There is not enough free disk space for the download.",
    "storage": "The download could not be saved in the app's data folder.",
    "mark": "The download could not be marked as coming from the internet, so it was deleted.",
}


class UpdateFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class _Cancelled(Exception):
    pass


_lock = threading.Lock()
_cancel = threading.Event()
_state: dict = {"stage": IDLE}


def updates_dir() -> Path:
    return Path(os.environ["LOCALAPPDATA"]) / "MySQLDBAValidator" / "updates"


def snapshot() -> dict:
    """What the page shows: the stage, real byte counts, and an error code and message."""
    with _lock:
        return {key: value for key, value in _state.items() if key != "path"}


def _set(**fields) -> None:
    with _lock:
        _state.update(fields)


def start(asset: dict) -> bool:
    """Start downloading `asset` (from update_check.checked_asset) unless one is under way.

    False when a download is already running. A file already downloaded and verified for
    the same version is kept, not fetched again.
    """
    with _lock:
        if _state["stage"] in ACTIVE:
            return False
        path = _state.get("path")
        if (_state["stage"] == READY and _state.get("version") == asset["version"]
                and path is not None and Path(path).is_file()):
            return True
        _cancel.clear()
        _state.clear()
        _state.update(stage=DOWNLOADING, version=asset["version"], received=0, total=asset["size"])
    threading.Thread(target=_run, args=(asset,), name="update-download", daemon=True).start()
    return True


def cancel() -> None:
    with _lock:
        if _state["stage"] in ACTIVE:
            _cancel.set()


def ready_file() -> Optional[Path]:
    with _lock:
        path = _state.get("path") if _state["stage"] == READY else None
    return Path(path) if path is not None and Path(path).is_file() else None


def show_in_folder() -> bool:
    """Open File Explorer with the verified ZIP selected. Nothing is extracted or run."""
    path = ready_file()
    if path is None:
        return False
    explorer = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "explorer.exe"
    # explorer.exe reads its own command line: /select,"<path>" (a Windows path holds no ").
    subprocess.Popen(f'"{explorer}" /select,"{path}"')
    return True


def remove_installed_downloads(directory: Optional[Path] = None) -> None:
    """Delete this app's downloads for the installed version or older (already installed)."""
    current = tuple(int(part) for part in APP_VERSION.split("."))
    try:
        directory = updates_dir() if directory is None else directory
        entries = list(directory.iterdir())
    except (KeyError, OSError):
        return
    for entry in entries:
        match = _OWN_FILE.match(entry.name)
        if match and tuple(int(part) for part in match.groups()[:3]) <= current:
            _remove(entry)


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass  # in use or protected: left for the next attempt


class _GitHubRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only over HTTPS to GitHub's own download hosts."""

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(newurl)
        if target.scheme != "https" or target.hostname not in DOWNLOAD_HOSTS:
            raise UpdateFailure("network")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(request: urllib.request.Request):
    # Certificate and host name verification: ssl.create_default_context() requires both.
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(_GitHubRedirects, urllib.request.HTTPSHandler(context=context))
    return opener.open(request, timeout=READ_TIMEOUT_SECONDS)


def _run(asset: dict) -> None:
    directory = updates_dir()
    final = directory / asset["name"]
    part = directory / (asset["name"] + ".part")
    try:
        _download(asset, directory, part)
        _set(stage=VERIFYING)
        check_archive(part, asset["name"][:-len(".zip")])
        if _cancel.is_set():
            raise _Cancelled()
        os.replace(part, final)
        mark_from_internet(final, asset["url"])
        _set(stage=READY, path=str(final), file=final.name)
    except _Cancelled:
        _remove(part)
        _remove(final)
        _set(stage=CANCELLED)
    except UpdateFailure as failure:
        _remove(part)
        _remove(final)
        _set(stage=FAILED, error=failure.code, message=MESSAGES[failure.code])
    except Exception:
        _remove(part)
        _remove(final)
        _set(stage=FAILED, error="network", message=MESSAGES["network"])


def _download(asset: dict, directory: Path, part: Path) -> None:
    size = asset["size"]
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for entry in directory.iterdir():  # one download at a time is kept
            if _OWN_FILE.match(entry.name):
                _remove(entry)
        free = shutil.disk_usage(directory).free
    except OSError:
        raise UpdateFailure("storage")
    if free < size + FREE_SPACE_MARGIN_BYTES:
        raise UpdateFailure("disk_space")
    request = urllib.request.Request(asset["url"], method="GET", headers={
        "Accept": "application/octet-stream", "User-Agent": f"MySQL-DBA-Validator/{APP_VERSION}"})
    digest = hashlib.sha256()
    received = 0
    try:
        with _open(request) as response:
            length = response.headers.get("Content-Length")
            if response.status != 200 or length is None or not length.isdigit() or int(length) != size:
                raise UpdateFailure("incomplete")
            with open(part, "wb") as out:
                while True:
                    if _cancel.is_set():
                        raise _Cancelled()
                    chunk = response.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > size:
                        raise UpdateFailure("incomplete")
                    out.write(chunk)
                    digest.update(chunk)
                    _set(received=received)
    except (UpdateFailure, _Cancelled):
        raise
    except OSError as error:
        if error.errno == errno.ENOSPC or getattr(error, "winerror", None) in (39, 112):
            raise UpdateFailure("disk_space")
        raise UpdateFailure("network")
    if received != size:
        raise UpdateFailure("incomplete")
    if digest.hexdigest() != asset["sha256"]:
        raise UpdateFailure("digest")


def check_archive(path: Path, top: str) -> None:
    """The ZIP must be the release folder `top` and nothing else: no absolute, drive,
    backslash or '..' paths, no encryption, a sane size, the app inside, intact data."""
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_ARCHIVE_ENTRIES:
                raise UpdateFailure("archive")
            unpacked = 0
            for entry in entries:
                name = entry.filename
                parts = PurePosixPath(name).parts
                if ("\\" in name or ":" in name or name.startswith("/") or not parts
                        or parts[0] != top or any(part in ("..", ".") for part in parts)
                        or entry.flag_bits & 0x1):
                    raise UpdateFailure("archive")
                unpacked += entry.file_size
            if unpacked > MAX_UNPACKED_BYTES or f"{top}/{APP_EXE}" not in archive.namelist():
                raise UpdateFailure("archive")
            if archive.testzip() is not None:
                raise UpdateFailure("archive")
    except (zipfile.BadZipFile, OSError, EOFError, RuntimeError, ValueError):
        raise UpdateFailure("archive")


def mark_from_internet(path: Path, url: str) -> None:
    """Write the Mark of the Web (Zone.Identifier, Internet zone) and read it back."""
    mark = f"[ZoneTransfer]\r\nZoneId=3\r\nHostUrl={url}\r\n"
    stream = f"{path}:Zone.Identifier"
    try:
        with open(stream, "w", encoding="ascii", newline="") as out:
            out.write(mark)
        with open(stream, encoding="ascii", newline="") as back:
            if back.read() != mark:
                raise UpdateFailure("mark")
    except OSError:
        raise UpdateFailure("mark")
