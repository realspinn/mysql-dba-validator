"""Desktop launcher used as the entry point of the packaged (portable) build.

One console executable starts everything a user needs:

* the public FastAPI app (``backend.main:app``) on http://127.0.0.1:8420, run as a
  separate, window-less child process of this same executable, and
* the local connector on 127.0.0.1:8765, run in the foreground through the
  existing ``connector.launcher.main`` so its pairing codes and operator console
  appear in this window exactly as with ``python -m connector``.

Nothing about the security model changes here. The launcher only supplies the
connector's required ``--allow-origin`` values (this machine's page), forwards
the connector's own options (``--tls-ca``, ``--registry``, ``--session-ttl``,
extra ``--allow-origin``), and opens the page in the default browser.

Persistent data lives in one per-user folder (the connector's existing default
registry location): ``%LOCALAPPDATA%\\MySQLDBAValidator``. The launcher writes
only ``logs\\backend.log`` there (overwritten each start). It never writes
credentials anywhere. The optional local-evidence settings (``MYSQL_*``) are read
only from ``<data folder>\\.env`` if the user creates one; the packaged app does
not search the working directory or its parents for ``.env`` files.

Closing the window (or Ctrl+C) stops the connector; the backend child is in a
Windows Job Object with kill-on-close, so it stops with the launcher even if the
launcher is killed.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Mapping, Sequence

APP_TITLE = "MySQL DBA Validator"
BACKEND_HOST = "127.0.0.1"
BACKEND_PORT = 8420
CONNECTOR_PORT = 8765
PAGE_URL = f"http://{BACKEND_HOST}:{BACKEND_PORT}"
# Both spellings of this machine's page; nothing else is allowed by default.
DEFAULT_PAGE_ORIGINS = (f"http://127.0.0.1:{BACKEND_PORT}", f"http://localhost:{BACKEND_PORT}")
BACKEND_START_TIMEOUT_SECONDS = 60
RUN_BACKEND_FLAG = "--run-backend"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------------- config

def data_dir(environ: Mapping[str, str] | None = None) -> Path:
    """Per-user application data folder: the parent of the connector's default registry."""
    from connector.registry_store import REGISTRY_PATH_ENV_VAR, default_registry_path

    env = dict(os.environ if environ is None else environ)
    env.pop(REGISTRY_PATH_ENV_VAR, None)  # an explicit registry override does not move app data
    return default_registry_path(env).parent


def isolate_dotenv(env_file: Path) -> bool:
    """Load only ``env_file`` (if present) and disable python-dotenv's directory search.

    ``backend.main`` calls ``load_dotenv()`` at import time, which in a frozen app
    searches the working directory and every parent directory for a ``.env``.
    For the packaged app the only configuration file is the one in the data folder.
    Existing environment variables are never overridden.
    """
    import dotenv

    loaded = bool(env_file.is_file() and dotenv.load_dotenv(env_file, override=False))

    def _no_search(*_args, **_kwargs) -> bool:
        return False

    dotenv.load_dotenv = _no_search
    return loaded


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="MySQL-DBA-Validator",
        description=f"{APP_TITLE}: starts the web app ({PAGE_URL}) and the local connector "
                    f"(127.0.0.1:{CONNECTOR_PORT}). Close the window or press Ctrl+C to stop.",
    )
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the page in the default browser")
    parser.add_argument("--tls-ca", metavar="PATH",
                        help="PEM CA bundle for company MySQL servers (connector option)")
    parser.add_argument("--registry", metavar="PATH",
                        help="approved-target registry file (connector option)")
    parser.add_argument("--session-ttl", metavar="SECONDS",
                        help="browser session lifetime in seconds (connector option)")
    parser.add_argument("--allow-origin", action="append", default=[], metavar="ORIGIN",
                        help="additional page origin allowed to pair (connector option); "
                             "this machine's page is always allowed")
    parser.add_argument(RUN_BACKEND_FLAG, action="store_true", help=argparse.SUPPRESS)
    return parser


def connector_argv(args: argparse.Namespace) -> list[str]:
    """Arguments for ``connector.launcher.main``: this page's origins plus forwarded options."""
    argv: list[str] = []
    for origin in list(DEFAULT_PAGE_ORIGINS) + [o for o in args.allow_origin if o not in DEFAULT_PAGE_ORIGINS]:
        argv += ["--allow-origin", origin]
    for flag, value in (("--tls-ca", args.tls_ca), ("--registry", args.registry),
                        ("--session-ttl", args.session_ttl)):
        if value:
            argv += [flag, value]
    return argv


# ----------------------------------------------------------------------------- process helpers

def port_in_use(port: int, host: str = BACKEND_HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return True
    return False


def wait_for_port(port: int, child: subprocess.Popen | None, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child is not None and child.poll() is not None:
            return False
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((BACKEND_HOST, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def backend_command() -> tuple[list[str], dict[str, str]]:
    """Command that re-runs this program as the backend child, plus its environment."""
    env = dict(os.environ)
    if getattr(sys, "frozen", False):
        return [sys.executable, RUN_BACKEND_FLAG], env
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return [sys.executable, "-m", "release.launcher", RUN_BACKEND_FLAG], env


def kill_with_parent(child: subprocess.Popen):
    """Put ``child`` in a Job Object that kills it when this process exits. Windows only."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
        return None  # 9 = JobObjectExtendedLimitInformation
    if not kernel32.AssignProcessToJobObject(job, wintypes.HANDLE(int(child._handle))):
        return None
    return job  # the handle must stay open for the launcher's lifetime


def set_console_title(title: str) -> None:
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def pause_if_interactive() -> None:
    """Keep a double-clicked window open long enough to read an error."""
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            input("Press Enter to close this window.")
        except (EOFError, KeyboardInterrupt):
            pass


# ----------------------------------------------------------------------------- roles

def run_backend() -> int:
    import uvicorn
    from backend.main import app

    uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT, log_level="info")
    return 0


def run_app(args: argparse.Namespace, app_data: Path, env_loaded: bool) -> int:
    from backend.version import APP_VERSION

    set_console_title(f"{APP_TITLE} {APP_VERSION}")
    log_path = app_data / "logs" / "backend.log"
    env_file = app_data / ".env"
    sys.stdout.write(
        f"{APP_TITLE} {APP_VERSION}\n"
        f"  web page     : {PAGE_URL}\n"
        f"  app data     : {app_data}\n"
        f"  backend log  : {log_path}\n"
        f"  local MySQL  : {'settings loaded from ' + str(env_file) if env_loaded else 'not configured (static validation only); optional settings file: ' + str(env_file)}\n"
        "  Close this window or press Ctrl+C to stop.\n\n"
    )
    sys.stdout.flush()

    busy = [port for port in (BACKEND_PORT, CONNECTOR_PORT) if port_in_use(port)]
    if busy:
        sys.stderr.write(
            "Cannot start: port(s) %s on 127.0.0.1 already in use. Is the validator already "
            "running in another window?\n" % ", ".join(map(str, busy)))
        pause_if_interactive()
        return 3

    log_path.parent.mkdir(parents=True, exist_ok=True)
    command, child_env = backend_command()
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(log_path, "w", encoding="utf-8") as log_file:
        child = subprocess.Popen(command, cwd=str(app_data), env=child_env, stdin=subprocess.DEVNULL,
                                 stdout=log_file, stderr=subprocess.STDOUT, creationflags=flags)
    job = kill_with_parent(child)
    try:
        if not wait_for_port(BACKEND_PORT, child, BACKEND_START_TIMEOUT_SECONDS):
            sys.stderr.write(f"The web app did not start. See {log_path}\n")
            pause_if_interactive()
            return 4

        if not args.no_browser:
            def open_when_ready() -> None:
                if wait_for_port(CONNECTOR_PORT, None, 30):
                    webbrowser.open(PAGE_URL)
            threading.Thread(target=open_when_ready, name="open-browser", daemon=True).start()

        from connector.launcher import main as connector_main

        try:
            code = connector_main(connector_argv(args))
        except SystemExit as exc:  # configuration error, already explained by argparse
            code = exc.code if isinstance(exc.code, int) else 2
        except KeyboardInterrupt:
            code = 0
        if code:
            pause_if_interactive()
        return code
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        del job


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    app_data = data_dir()
    env_loaded = isolate_dotenv(app_data / ".env")
    if getattr(args, "run_backend"):
        return run_backend()
    app_data.mkdir(parents=True, exist_ok=True)
    return run_app(args, app_data, env_loaded)


if __name__ == "__main__":
    raise SystemExit(main())
