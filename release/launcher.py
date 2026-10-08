"""Desktop launcher used as the entry point of the packaged (portable) build.

The build ships two executables made from this one entry point:

* ``MySQL-DBA-Validator.exe`` (windowed, no console): starts only the public
  FastAPI app (``backend.main:app``) on http://127.0.0.1:8420 and opens it in the
  default browser. It does not start the connector, so it serves static and local
  validation only. Startup failures are shown in a message box. The backend stops
  on its own once no page of this app has been open for a while (idle shutdown,
  driven by the page's heartbeat).
* ``MySQL-DBA-Validator Console.exe`` (console): starts the same backend and the
  local connector on 127.0.0.1:8765, run in the foreground through the existing
  ``connector.launcher.main`` so its pairing codes and operator console appear in
  this window exactly as with ``python -m connector``.

In both, the backend is a separate, window-less child process of the console
executable. Nothing about the security model changes here. The console launcher
only supplies the connector's required ``--allow-origin`` values (this machine's
page), forwards the connector's own options (``--tls-ca``, ``--registry``,
``--session-ttl``, extra ``--allow-origin``), and opens the page in the default
browser.

Persistent data lives in one per-user folder (the connector's existing default
registry location): ``%LOCALAPPDATA%\\MySQLDBAValidator``. The launcher writes
only ``logs\\backend.log`` there (overwritten each start). It never writes
credentials anywhere. The optional local-evidence settings (``MYSQL_*``) are read
only from ``<data folder>\\.env`` if the user creates one; the packaged app does
not search the working directory or its parents for ``.env`` files.

Closing the console window (or Ctrl+C) stops the connector; the backend child is in
a Windows Job Object with kill-on-close, so it stops with the launcher even if the
launcher is killed.

On macOS (the ``MySQL DBA Validator.app`` bundle) the same two roles are the
bundle's main executable and ``Contents/MacOS/MySQL-DBA-Validator Console``. There
is no Job Object, so the backend child watches a pipe from the launcher instead:
when the launcher exits for any reason, including being killed or its Terminal
window being closed, the pipe closes and the backend stops. The data folder is
the connector's existing default (``$XDG_STATE_HOME`` or ``~/.local/state``).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Callable, Mapping, Sequence

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

# The console executable; the backend child always runs from it (window-less), because
# the windowed executable has no standard streams for the backend log.
CONSOLE_EXE_NAME = "MySQL-DBA-Validator Console.exe"
# In the macOS .app bundle both executables sit in Contents/MacOS, without a suffix.
MACOS_CONSOLE_EXE_NAME = "MySQL-DBA-Validator Console"
# POSIX only: set for the backend child, whose stdin is then a pipe from the launcher.
# End of file on it means the launcher is gone, so the backend stops (see watch_parent).
PARENT_PIPE_ENV = "MDV_PARENT_PIPE"
PARENT_EXIT_GRACE_SECONDS = 10.0
# Set on the windowed executable at build time (PyInstaller "X" option -> sys._xoptions).
WINDOWED_XOPTION = "mdv_windowed"
# Idle shutdown (windowed app only). The page sends a heartbeat every 60 s and when it
# becomes visible again. Browsers throttle timers in background tabs (Chrome's intensive
# throttling allows about one wake-up per minute), so the backend stops only after 15
# minutes without any heartbeat: comfortably more than a throttled tab's gaps.
IDLE_SHUTDOWN_ENV = "MDV_IDLE_SHUTDOWN_SECONDS"
DEFAULT_IDLE_SHUTDOWN_SECONDS = 15 * 60
IDLE_CHECK_SECONDS = 5.0


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


def console_executable_name(platform: str | None = None) -> str:
    """File name of the console executable next to this one (``.exe`` on Windows)."""
    return MACOS_CONSOLE_EXE_NAME if (platform or sys.platform) == "darwin" else CONSOLE_EXE_NAME


def uses_parent_pipe() -> bool:
    """POSIX: the backend child is tied to the launcher by a pipe (Windows uses a Job Object)."""
    return os.name != "nt"


def child_stdin():
    """The backend child's stdin: the parent pipe on POSIX, nothing on Windows."""
    return subprocess.PIPE if uses_parent_pipe() else subprocess.DEVNULL


def backend_command(idle_shutdown_seconds: int | None = None) -> tuple[list[str], dict[str, str]]:
    """Command that runs the backend child, plus its environment.

    Idle shutdown is enabled only when ``idle_shutdown_seconds`` is given (the windowed
    app); an inherited setting is never passed on. On POSIX the child is also told to
    stop when the launcher's pipe closes; that setting is never inherited either.
    """
    env = dict(os.environ)
    env.pop(IDLE_SHUTDOWN_ENV, None)
    env.pop(PARENT_PIPE_ENV, None)
    if idle_shutdown_seconds:
        env[IDLE_SHUTDOWN_ENV] = str(idle_shutdown_seconds)
    if uses_parent_pipe():
        env[PARENT_PIPE_ENV] = "1"
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name(console_executable_name())), RUN_BACKEND_FLAG], env
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


# The message reaches AppleScript only as an argument (item 2 of argv), never as script
# text, so nothing in it is interpreted.
MACOS_ALERT_SCRIPT = ("on run argv", "display alert (item 1 of argv) message (item 2 of argv) as critical",
                      "end run")


def macos_alert_command(message: str) -> list[str]:
    command = ["/usr/bin/osascript"]
    for line in MACOS_ALERT_SCRIPT:
        command += ["-e", line]
    return command + [APP_TITLE, message]


def show_error(message: str, platform: str | None = None) -> None:
    """Show a startup problem to the user: a message box (the windowed app has no console)."""
    if (platform or sys.platform) == "darwin":
        # An alert that stays on screen after this process exits; the launcher does
        # not wait for it to be dismissed.
        try:
            subprocess.Popen(macos_alert_command(message), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except Exception:
            pass
    elif os.name == "nt":
        try:
            import ctypes
            # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
            ctypes.windll.user32.MessageBoxW(None, message, APP_TITLE, 0x0 | 0x10 | 0x10000)
            return
        except Exception:
            pass
    if sys.stderr is not None:
        sys.stderr.write(message + "\n")


def validator_already_running(timeout: float = 3.0) -> bool:
    """True when this machine's page is already served by a running validator backend."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{PAGE_URL}/api/health", timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    return isinstance(body, dict) and body.get("status") == "ok" and "version" in body


def idle_shutdown_seconds(environ: Mapping[str, str]) -> int | None:
    """The idle-shutdown timeout the launcher set for this backend, if any."""
    try:
        seconds = int(environ.get(IDLE_SHUTDOWN_ENV, ""))
    except ValueError:
        return None
    return seconds if seconds > 0 else None


class IdleMonitor:
    """Time of the last page heartbeat (monotonic clock)."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self.last_seen = clock()

    def touch(self) -> None:
        self.last_seen = self._clock()


def watch_idle(monitor: IdleMonitor, stop: Callable[[], None], timeout: float,
               tick: float = IDLE_CHECK_SECONDS, clock: Callable[[], float] = time.monotonic,
               sleep: Callable[[float], None] = time.sleep) -> None:
    """Call ``stop`` once no heartbeat has arrived for ``timeout`` seconds.

    A long gap between checks means the computer was asleep; that time is not
    counted as idle, so a sleeping laptop does not shut the app down on resume
    before its open page has had a chance to send a heartbeat.
    """
    previous = clock()
    while True:
        sleep(tick)
        now = clock()
        if now - previous > tick * 6:
            monitor.touch()
        previous = now
        if now - monitor.last_seen >= timeout:
            stop()
            return


def watch_parent(stream, stop: Callable[[], None], grace: float = PARENT_EXIT_GRACE_SECONDS,
                 force_exit: Callable[[int], None] = os._exit,
                 start_timer: Callable[[float, Callable[[], None]], object] | None = None) -> None:
    """Call ``stop`` once the launcher's end of the pipe on ``stream`` is closed.

    The launcher never writes to the pipe, so a read returns only at end of file:
    when the launcher has exited, however it ended. If the server has not stopped
    ``grace`` seconds later, the process exits anyway, so no backend is left behind.
    """
    try:
        while stream.read(4096):
            pass
    except Exception:
        pass
    stop()

    def _force() -> None:
        force_exit(0)

    if start_timer is None:
        timer = threading.Timer(grace, _force)
        timer.daemon = True
        timer.start()
    else:
        start_timer(grace, _force)


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

    idle_timeout = idle_shutdown_seconds(os.environ)
    parent_pipe = os.environ.get(PARENT_PIPE_ENV) == "1" and sys.stdin is not None
    if idle_timeout is None and not parent_pipe:
        uvicorn.run(app, host=BACKEND_HOST, port=BACKEND_PORT, log_level="info")
        return 0

    server = uvicorn.Server(uvicorn.Config(app, host=BACKEND_HOST, port=BACKEND_PORT, log_level="info"))

    if idle_timeout is not None:
        monitor = IdleMonitor()
        app.state.on_heartbeat = monitor.touch

        def stop() -> None:
            print(f"No page heartbeat for {idle_timeout} seconds; stopping.", flush=True)
            server.should_exit = True

        threading.Thread(target=watch_idle, args=(monitor, stop, idle_timeout),
                         name="idle-shutdown", daemon=True).start()

    if parent_pipe:
        def stop_with_launcher() -> None:
            print("The launcher has exited; stopping.", flush=True)
            server.should_exit = True

        threading.Thread(target=watch_parent, args=(sys.stdin.buffer, stop_with_launcher),
                         name="parent-watch", daemon=True).start()

    server.run()
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
        child = subprocess.Popen(command, cwd=str(app_data), env=child_env, stdin=child_stdin(),
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


def run_windowed(argv: Sequence[str]) -> int:
    """The windowed app: backend only (no connector), browser, idle shutdown.

    There is no console, so nothing is written to stdout/stderr; every startup
    problem is shown in a message box (and the backend's own output is in its log).
    """
    from backend.version import APP_VERSION

    unknown = [arg for arg in argv if arg != "--no-browser"]
    if unknown:
        show_error(f"Unknown option(s): {' '.join(unknown)}\n\nConnector options are accepted only by "
                   f"{console_executable_name()}.")
        return 2
    open_browser = "--no-browser" not in argv

    app_data = data_dir()
    app_data.mkdir(parents=True, exist_ok=True)
    if port_in_use(BACKEND_PORT):
        if validator_already_running():
            # Already running (this app or the console version): just show the page.
            if open_browser:
                webbrowser.open(PAGE_URL)
            return 0
        show_error(f"{APP_TITLE} {APP_VERSION} cannot start: port {BACKEND_PORT} on 127.0.0.1 is "
                   "already in use by another program.")
        return 3

    log_path = app_data / "logs" / "backend.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command, child_env = backend_command(
        idle_shutdown_seconds(os.environ) or DEFAULT_IDLE_SHUTDOWN_SECONDS)
    if getattr(sys, "frozen", False) and not Path(command[0]).is_file():
        reinstall = ("Copy the app from the release disk image again." if sys.platform == "darwin"
                     else "Extract the whole release zip again.")
        show_error(f"{APP_TITLE} {APP_VERSION} cannot start: {console_executable_name()} is missing from "
                   f"{Path(command[0]).parent}. {reinstall}")
        return 4
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    with open(log_path, "w", encoding="utf-8") as log_file:
        child = subprocess.Popen(command, cwd=str(app_data), env=child_env, stdin=child_stdin(),
                                 stdout=log_file, stderr=subprocess.STDOUT, creationflags=flags)
    job = kill_with_parent(child)
    try:
        if not wait_for_port(BACKEND_PORT, child, BACKEND_START_TIMEOUT_SECONDS):
            show_error(f"{APP_TITLE} {APP_VERSION}: the web app did not start.\n\nDetails: {log_path}")
            return 4
        if open_browser:
            webbrowser.open(PAGE_URL)
        code = child.wait()  # returns 0 after idle shutdown
        if code:
            show_error(f"{APP_TITLE} {APP_VERSION}: the web app stopped unexpectedly "
                       f"(exit code {code}).\n\nDetails: {log_path}")
        return code
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
        del job


def main_windowed(argv: Sequence[str] | None = None) -> int:
    try:
        return run_windowed(list(sys.argv[1:] if argv is None else argv))
    except Exception as exc:  # never fail silently: there is no console to show a traceback
        show_error(f"{APP_TITLE} could not start: {type(exc).__name__}: {exc}")
        return 1


def is_windowed() -> bool:
    return WINDOWED_XOPTION in getattr(sys, "_xoptions", {})


def main(argv: Sequence[str] | None = None) -> int:
    if is_windowed():
        return main_windowed(argv)
    args = _argument_parser().parse_args(argv)
    app_data = data_dir()
    env_loaded = isolate_dotenv(app_data / ".env")
    if getattr(args, "run_backend"):
        return run_backend()
    app_data.mkdir(parents=True, exist_ok=True)
    return run_app(args, app_data, env_loaded)


if __name__ == "__main__":
    raise SystemExit(main())
