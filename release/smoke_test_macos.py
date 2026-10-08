"""Smoke-test the built macOS app itself (the unsigned .zip, the .dmg or the .app), not the source tree.

    python release/smoke_test_macos.py dist/macos/MySQL-DBA-Validator-v<version>-macos-arm64-unsigned.zip
    python release/smoke_test_macos.py dist/macos/MySQL-DBA-Validator-v<version>-macos-arm64.dmg

The macOS counterpart of release/smoke_test_artifact.py, using its helpers. It runs
the executables directly, not through LaunchServices (Finder), so first-launch
Gatekeeper behaviour and reopening a running app are not covered here; those
belong to the real-Mac acceptance pass (VERIFICATION.md). The startup alert is
detected but not asserted, as a CI runner may have no one to show it to.

* copies the app out of the archive or disk image into a fresh folder whose
  path contains spaces, and runs it with a minimal environment: PATH is only the
  system folders, no PYTHON* variables, HOME and XDG_STATE_HOME pointed at temporary
  folders, and a working directory containing a decoy .env that must NOT be read;
* the bundle: Info.plist, both executables present and arm64;
* the console app (Contents/MacOS/MySQL-DBA-Validator Console): page, API, static
  validation, evidence contract, connector, origin rules, single-use pairing,
  browser-session scope (no target management), the approved-target list, company
  operations failing closed without a TLS CA, and no credential in any output;
* lifecycle: Ctrl+C (SIGINT) stops it and its backend; closing its Terminal window
  (SIGHUP) and a hard kill (SIGKILL) of the launcher both stop the backend too; a
  second instance refuses to start while the ports are in use;
* the windowed app (the bundle's main executable): backend only, no connector, the
  browser is opened on this machine's page, heartbeat accepted only from that page,
  idle shutdown, a second start reusing the running app, a busy port refused with a
  visible error, a killed backend reported, and a hard kill stopping the backend;
* the app folder is never written to; the data folder holds only the registry and log.

Needs macOS, ports 8420 and 8765 free, and DNS for example.com (a stand-in target;
no MySQL connection is made because no CA is configured).
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from release import smoke_test_artifact as common  # noqa: E402  (shared helpers and results)
from release.smoke_test_artifact import (  # noqa: E402
    CONNECTOR, DECOY, PAGE, SECRET, USER, as_json, check, port_open, request, seed_registry, tree, wait_ports_closed,
    wait_until,
)

APP_NAME = "MySQL DBA Validator.app"
MAIN_EXE = "MySQL-DBA-Validator"
CONSOLE_EXE = "MySQL-DBA-Validator Console"
# release/launcher.py PARENT_EXIT_GRACE_SECONDS: a backend whose launcher is gone exits within this.
PARENT_GRACE_SECONDS = 10


class MacApp(common.App):
    """The console executable, started like an operator would from Terminal."""

    def __init__(self, exe: Path, env: dict, cwd: Path):
        self.lines: list[str] = []
        self.proc = subprocess.Popen(
            [str(exe), "--no-browser"], cwd=str(cwd), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        threading.Thread(target=self._read, daemon=True).start()

    def signal_and_wait(self, signum: int, timeout: float = 20) -> bool:
        self.proc.send_signal(signum)
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return False

    def stop_graceful(self, timeout: float = 20) -> bool:
        return self.signal_and_wait(signal.SIGINT, timeout)  # Ctrl+C in Terminal


def listening_pid(port: int) -> int | None:
    out = subprocess.run(["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
                         capture_output=True, text=True).stdout.split()
    return int(out[0]) if out else None


def osascript_alerts(text: str) -> list[int]:
    out = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return [int(line.split(None, 1)[0]) for line in out.splitlines()
            if "osascript" in line and text in line]


def install_app(target: Path, install: Path) -> Path:
    """Copy the app from the .dmg (mounted read-only), the unsigned .zip or a .app path into ``install``."""
    if target.suffix == ".zip":
        unpack = Path(tempfile.mkdtemp(prefix="mdv-smoke-zip-"))
        try:
            subprocess.run(["/usr/bin/ditto", "-x", "-k", str(target), str(unpack)], check=True)
            check("archive contains exactly the app", sorted(p.name for p in unpack.iterdir()) == [APP_NAME],
                  sorted(p.name for p in unpack.iterdir()))
            subprocess.run(["/usr/bin/ditto", str(unpack / APP_NAME), str(install / APP_NAME)], check=True)
        finally:
            shutil.rmtree(unpack, ignore_errors=True)
    elif target.suffix == ".dmg":
        mount = Path(tempfile.mkdtemp(prefix="mdv-smoke-mount-"))
        subprocess.run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen",
                        "-mountpoint", str(mount), str(target)], check=True, stdout=subprocess.DEVNULL)
        try:
            check("disk image contains the app, README and licence",
                  (mount / APP_NAME).is_dir() and (mount / "README.txt").is_file() and (mount / "LICENSE.txt").is_file())
            subprocess.run(["/usr/bin/ditto", str(mount / APP_NAME), str(install / APP_NAME)], check=True)
        finally:
            subprocess.run(["/usr/bin/hdiutil", "detach", str(mount)], check=False, stdout=subprocess.DEVNULL)
    else:
        subprocess.run(["/usr/bin/ditto", str(target), str(install / APP_NAME)], check=True)
    return install / APP_NAME


def main() -> int:
    if len(sys.argv) != 2 or sys.platform != "darwin":
        sys.exit(__doc__)
    target = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(ROOT))
    from backend.version import APP_VERSION as version
    if port_open(8420) or port_open(8765):
        sys.exit("ports 8420/8765 are in use; stop any running validator first")

    work = Path(tempfile.mkdtemp(prefix="mdv smoke "))
    install, home, state, cwd = (work / "Applications Test", work / "home", work / "state dir", work / "cwd with decoy")
    for folder in (install, home, state, cwd):
        folder.mkdir()
    (cwd / ".env").write_text(f"MYSQL_HOST=127.0.0.1\nMYSQL_USER=decoy\nMYSQL_PASSWORD={DECOY}\n", encoding="utf-8")
    print(f"work folder: {work}")

    app = install_app(target, install)
    macos = app / "Contents" / "MacOS"
    exe, windowed_exe = macos / CONSOLE_EXE, macos / MAIN_EXE
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    check("Info.plist: windowed executable, version and no Dock icon",
          info.get("CFBundleExecutable") == MAIN_EXE and info.get("CFBundleShortVersionString") == version
          and info.get("LSUIElement") is True, info)
    archs = [subprocess.run(["/usr/bin/lipo", "-archs", str(p)], capture_output=True, text=True).stdout.strip()
             for p in (exe, windowed_exe)]
    check("both executables present and arm64 only", exe.is_file() and windowed_exe.is_file()
          and archs == ["arm64", "arm64"], archs)
    before = tree(app)

    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(home), "XDG_STATE_HOME": str(state),
           "TMPDIR": os.environ.get("TMPDIR", "/tmp"), "LANG": "en_US.UTF-8"}
    check("no PYTHON* variables in the app's environment", not any(k.upper().startswith("PYTHON") for k in env))

    data = state / "MySQLDBAValidator"
    registry = data / "connector-targets.json"
    check("fixture: stand-in target approved in the default registry location", seed_registry(registry))
    registry_before = registry.read_bytes()

    # ------------------------------------------------------------------ console app
    app1 = MacApp(exe, env, cwd)
    code = None
    try:
        check("console app starts: page, API and connector listening, pairing code printed",
              app1.wait_ready(), app1.output()[-2000:])
        out = app1.output()
        check("console shows version, data folder and registry path",
              f"MySQL DBA Validator {version}" in out and str(data) in out and str(registry) in out, out[:1500])
        check("console reports company TLS CA not configured", "NOT CONFIGURED" in out)
        status, headers, body = request("GET", PAGE + "/")
        check("page served", status == 200 and b"targetSelect" in body, status)
        check("page has security headers", "default-src 'self'" in headers.get("content-security-policy", ""))
        status, _, body = request("GET", PAGE + "/api/health")
        health = as_json(body) or {}
        check("API health ok and reports the release version",
              status == 200 and health.get("version") == version, body[:200])
        check("decoy .env in working directory was NOT read", health.get("database", {}).get("configured") is False)
        status, _, body = request("POST", PAGE + "/api/validate", {"sql": "DELETE FROM users"})
        check("static validation works (unrestricted DELETE is high risk)",
              status == 200 and (as_json(body) or {}).get("overall_risk_level") in {"HIGH", "CRITICAL"}, body[:300])
        status, _, body = request("POST", PAGE + "/api/validate", {"sql": "SELECT id FROM users WHERE id = 1"})
        result = as_json(body) or {}
        statement = (result.get("statements") or [{}])[0]
        check("evidence contract: static-only result says so",
              result.get("analysis_mode") == "STATIC" and (statement.get("evidence") or {}).get("overall") == "static_only",
              body[:400])
        status, _, body = request("POST", PAGE + "/api/validate", {"sql": "SELECT 1", "host": "example.com",
                                                                   "port": 3306, "username": USER, "password": SECRET})
        check("public API never takes company credentials for a remote host",
              (as_json(body) or {}).get("error_code") == "permission_denied" and SECRET.encode() not in body, body[:300])

        status, _, body = request("GET", CONNECTOR + "/health")
        check("connector health ok", status == 200 and (as_json(body) or {}).get("loopback_only") is True)
        status, headers, _ = request("OPTIONS", CONNECTOR + "/pair", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
        check("connector CORS refuses other origins", headers.get("access-control-allow-origin") is None)
        status, _, _ = request("POST", CONNECTOR + "/pair", {"pairing_token": app1.pairing_code()},
                               headers={"Origin": "http://evil.example"})
        check("pairing from a foreign origin refused", status == 401, status)
        code = app1.pairing_code()
        status, _, body = request("POST", CONNECTOR + "/pair", {"pairing_token": code}, headers={"Origin": PAGE})
        paired = as_json(body) or {}
        token = paired.get("session_token")
        check("pairing with the console code works (browser scope)",
              status == 200 and token and paired.get("scope") == "browser", (status, body[:200]))
        status, _, _ = request("POST", CONNECTOR + "/pair", {"pairing_token": code}, headers={"Origin": PAGE})
        check("pairing code is single use", status == 403, status)
        auth = {"Origin": PAGE, "Authorization": f"Bearer {token}"}
        status, _, body = request("GET", CONNECTOR + "/company/targets", headers=auth)
        listed = body.decode(errors="replace")
        check("approved target listed; pending one hidden", status == 200 and "t-dns" in listed
              and "t-pending" not in listed, listed[:300])
        denied = [request("POST", CONNECTOR + "/company/targets", {"target_id": "evil", "display_name": "e",
                                                                   "host": "evil.example", "port": 3306}, headers=auth)[0],
                  request("POST", CONNECTOR + "/company/targets/t-pending/approve", {}, headers=auth)[0],
                  request("POST", CONNECTOR + "/company/targets/t-dns/revoke", {}, headers=auth)[0],
                  request("DELETE", CONNECTOR + "/company/targets/t-dns", headers=auth)[0]]
        check("browser session cannot register, approve, revoke or delete targets (403)",
              denied == [403, 403, 403, 403], denied)
        status, _, body = request("POST", CONNECTOR + "/company/targets/t-dns/databases",
                                  {"username": USER, "password": SECRET}, headers=auth, timeout=30)
        check("company discovery fails closed without a TLS CA",
              status >= 400 and not (as_json(body) or {}).get("databases") and SECRET.encode() not in body,
              (status, body[:300]))
        status, _, body = request("POST", CONNECTOR + "/company/targets/t-dns/validate",
                                  {"sql": "SELECT 1", "username": USER, "password": SECRET}, headers=auth, timeout=30)
        check("remote validation without a TLS CA does not answer with evidence or the password",
              status >= 400 and SECRET.encode() not in body, (status, body[:300]))
        busy = MacApp(exe, env, cwd)
        busy.proc.wait(timeout=60)
        check("second instance refuses to start while the ports are in use",
              busy.proc.returncode == 3 and "already in use" in busy.output(), busy.proc.returncode)
    finally:
        graceful = app1.stop_graceful()
    check("Ctrl+C (SIGINT) stops the console app", graceful)
    check("both ports released after Ctrl+C (backend stopped)", wait_ports_closed())

    for signum, label in ((signal.SIGHUP, "closing its Terminal window (SIGHUP)"),
                          (signal.SIGKILL, "a hard kill (SIGKILL)")):
        app2 = MacApp(exe, env, cwd)
        started = app2.wait_ready()
        if signum == signal.SIGHUP and started:
            check("new pairing code issued on restart", app2.pairing_code() and app2.pairing_code() != code)
        app2.signal_and_wait(signum)
        check(f"{label} of the launcher also stops the backend (no orphan on 8420)",
              started and wait_ports_closed(timeout=PARENT_GRACE_SECONDS + 15))

    # ------------------------------------------------------------------ windowed app
    idle_seconds = 20
    browser_log = Path(tempfile.mkdtemp(prefix="mdv-browser-")) / "opened.txt"
    browser = browser_log.with_name("record-url.sh")
    browser.write_text(f'#!/bin/sh\nprintf "%s\\n" "$1" >> "{browser_log}"\n', encoding="utf-8")
    browser.chmod(0o755)
    windowed_env = dict(env, MDV_IDLE_SHUTDOWN_SECONDS=str(idle_seconds), BROWSER=str(browser))

    def start_windowed(*args: str) -> subprocess.Popen:
        return subprocess.Popen([str(windowed_exe), *args], cwd=str(cwd), env=windowed_env,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    win = start_windowed()
    try:
        check("windowed app starts the web app", wait_until(lambda: port_open(8420), 90))
        check("windowed app opens this machine's page in the default browser",
              wait_until(lambda: browser_log.is_file() and PAGE in browser_log.read_text(), 15))
        status, _, body = request("GET", PAGE + "/api/health")
        check("windowed app: API health ok and reports the release version",
              status == 200 and (as_json(body) or {}).get("version") == version)
        check("windowed app does not start the connector", not port_open(8765))
        own = request("POST", PAGE + "/api/heartbeat", headers={"Origin": PAGE})[0]
        foreign = request("POST", PAGE + "/api/heartbeat", headers={"Origin": "http://evil.example"})[0]
        rebound = request("POST", PAGE + "/api/heartbeat", headers={
            "Origin": "http://evil.example:8420", "Host": "evil.example:8420"})[0]
        check("heartbeat accepted only from this machine's page", (own, foreign, rebound) == (204, 403, 403),
              (own, foreign, rebound))
        end = time.time() + idle_seconds * 1.5
        while time.time() < end:
            request("POST", PAGE + "/api/heartbeat", headers={"Origin": PAGE})
            time.sleep(4)
        check("heartbeats keep the windowed app running past the idle timeout", win.poll() is None and port_open(8420))
        second = start_windowed("--no-browser")
        second.wait(timeout=30)
        check("a second start reuses the running app and exits", second.returncode == 0 and win.poll() is None)
        try:
            win.wait(timeout=idle_seconds + 30)
        except subprocess.TimeoutExpired:
            pass
        check("without heartbeats the windowed app stops on its own (idle shutdown, exit code 0)", win.returncode == 0,
              win.returncode)
        check("web app port released after idle shutdown", wait_ports_closed())
    finally:
        if win.poll() is None:
            win.kill()

    # A port taken by another program: the windowed app refuses with a visible alert.
    with socket.socket() as squatter:
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", 8420))
        squatter.listen()
        blocked = start_windowed("--no-browser")
        blocked.wait(timeout=60)
        message = blocked.stdout.read().decode(errors="replace")
        check("busy port: the windowed app refuses to start (exit code 3) with a message",
              blocked.returncode == 3 and "already in use" in message, (blocked.returncode, message[-300:]))
        alerts = osascript_alerts("already in use")
        print(f"diagnostic: on-screen alert processes for the busy-port error: {len(alerts)}")
        for pid in alerts:
            os.kill(pid, signal.SIGTERM)

    # The backend dying underneath the windowed app is reported, not ignored.
    win = start_windowed("--no-browser")
    try:
        started = wait_until(lambda: port_open(8420), 90)
        backend = listening_pid(8420)
        if backend:
            os.kill(backend, signal.SIGKILL)
        win.wait(timeout=30)
        check("a killed backend makes the windowed app exit with an error",
              started and backend and win.returncode not in (0, None), (started, backend, win.returncode))
    finally:
        if win.poll() is None:
            win.kill()
        for pid in osascript_alerts("stopped unexpectedly"):
            os.kill(pid, signal.SIGTERM)
    check("port released after the backend was killed", wait_ports_closed())

    win = start_windowed("--no-browser")
    started = wait_until(lambda: port_open(8420), 90)
    win.kill()
    win.wait(timeout=10)
    check("hard kill (SIGKILL) of the windowed app also stops the backend",
          started and wait_ports_closed(timeout=PARENT_GRACE_SECONDS + 15))

    # ------------------------------------------------------------------ persistent state + leaks
    after = tree(app)
    check("app bundle unchanged (nothing written into it)", before == after,
          sorted(set(after) ^ set(before)) or "sizes changed")
    files = sorted(tree(data))
    check("data folder contains only the registry and the backend log",
          set(files) <= {"connector-targets.json", "logs/backend.log"} and "logs/backend.log" in files, files)
    reg_text = registry.read_text(encoding="utf-8")
    check("registry is valid JSON with the target intact", json.loads(reg_text)["kind"] and
          registry.read_bytes() == registry_before)
    log_text = (data / "logs" / "backend.log").read_text(encoding="utf-8", errors="replace")
    everything = [app1.output(), log_text, reg_text]
    check("test password never in console output, log or registry", not any(SECRET in t for t in everything))
    check("decoy .env password never appears anywhere", not any(DECOY in t for t in everything))
    check("no credential store created", not any(p.suffix in {".db", ".sqlite", ".key", ".pem", ".keychain-db"}
                                                 for p in work.rglob("*")))

    passed = sum(ok for _, ok in common.results)
    print(f"\n{passed}/{len(common.results)} macOS artifact checks passed")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if passed == len(common.results) else 1


if __name__ == "__main__":
    sys.exit(main())
