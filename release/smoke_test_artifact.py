"""Smoke-test the built Windows release ZIP itself (not the source tree).

    python release\\smoke_test_artifact.py dist\\MySQL-DBA-Validator-v<version>-windows-x64.zip

What it does:

* extracts the ZIP into a fresh temporary folder whose path contains spaces;
* runs MySQL-DBA-Validator.exe with a minimal environment: PATH is only the
  Windows system folders (no Python), no PYTHON* variables, LOCALAPPDATA pointed
  at a temporary folder, and a working directory containing a decoy .env that
  must NOT be read;
* checks the page, the public API (static validation), the connector, origin
  enforcement, single-use pairing with the code printed in the console, the
  approved-target list, a company discovery request that must fail closed (no
  TLS CA), and that the test password never reaches the console, log, registry
  or program folder;
* stops the app gracefully (Ctrl+Break), restarts it, kills it hard, and checks
  that the backend child dies with it, the registry survives intact, old
  sessions are invalid and the program folder was never written to.

Fixture: before the first start, an approved target (``example.com``, a real
public DNS name used only as a stand-in; no MySQL connection is made because no
CA is configured) is written into the temporary registry with the source tree's
own connector code. Only standard library modules are used against the artifact.
Needs DNS resolution for example.com.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = "http://127.0.0.1:8420"
CONNECTOR = "http://127.0.0.1:8765"
SECRET = "Smoke-Artifact-Secret-8841"
USER = "smoke_user"
DECOY = "Decoy-Dotenv-Password-5150"

results: list[tuple[str, bool]] = []
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(label: str, ok, detail="") -> bool:
    results.append((label, bool(ok)))
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail and not ok else ""), flush=True)
    return bool(ok)


def request(method: str, url: str, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def as_json(raw: bytes):
    try:
        return json.loads(raw)
    except ValueError:
        return None


def port_open(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class App:
    """The packaged executable, started like a user would, with its console captured."""

    def __init__(self, exe: Path, env: dict, cwd: Path):
        self.lines: list[str] = []
        self.proc = subprocess.Popen(
            [str(exe), "--no-browser"], cwd=str(cwd), env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.append(line.rstrip("\n"))

    def output(self) -> str:
        return "\n".join(self.lines)

    def wait_ready(self, timeout=90) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if self.proc.poll() is not None:
                return False
            if self.pairing_code() and port_open(8420) and port_open(8765):
                return True
            time.sleep(0.25)
        return False

    def pairing_code(self) -> str | None:
        match = re.search(r"Pairing code[^\n]*:\s*\n\s*\n\s+(\S+)", self.output())
        return match.group(1) if match else None

    def stop_graceful(self, timeout=20) -> bool:
        self.proc.send_signal(signal.CTRL_BREAK_EVENT)
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return False

    def kill(self):
        self.proc.kill()  # TerminateProcess: like Task Manager "End task"
        self.proc.wait(timeout=10)


def wait_ports_closed(timeout=15) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not port_open(8420) and not port_open(8765):
            return True
        time.sleep(0.25)
    return False


def seed_registry(registry: Path) -> bool:
    """Approve a stand-in target using the source tree's own operator commands."""
    sys.path.insert(0, str(ROOT))
    from connector import launcher as source_launcher

    config = source_launcher.build_config(
        ["--allow-origin", PAGE, "--registry", str(registry)], environ={})
    runtime = source_launcher.build_runtime(config)
    print("  fixture> " + source_launcher.run_operator_command(runtime, "register t-dns example.com 3306 Stand-in"))
    out = source_launcher.run_operator_command(runtime, "approve t-dns")
    print("  fixture> " + out)
    print("  fixture> " + source_launcher.run_operator_command(runtime, "register t-pending example.org 3306 Pending"))
    runtime.shutdown()
    return out.startswith("approved")


def tree(folder: Path) -> dict[str, int]:
    return {p.relative_to(folder).as_posix(): p.stat().st_size for p in folder.rglob("*") if p.is_file()}


def main() -> int:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    zip_path = Path(sys.argv[1]).resolve()
    version_match = re.search(r"-v(\d+\.\d+\.\d+[^-]*)-windows-x64\.zip$", zip_path.name)
    if not version_match:
        sys.exit("expected a file named MySQL-DBA-Validator-v<version>-windows-x64.zip")
    version = version_match.group(1)
    if port_open(8420) or port_open(8765):
        sys.exit("ports 8420/8765 are in use; stop any running validator first")

    work = Path(tempfile.mkdtemp(prefix="mdv smoke "))
    install = work / "Program Files Test"
    appdata = work / "Local App Data"
    cwd = work / "cwd with decoy"
    for folder in (install, appdata, cwd):
        folder.mkdir()
    (cwd / ".env").write_text(f"MYSQL_HOST=127.0.0.1\nMYSQL_USER=decoy\nMYSQL_PASSWORD={DECOY}\n", encoding="utf-8")
    print(f"work folder: {work}")

    with zipfile.ZipFile(zip_path) as archive:
        archive.extractall(install)
    tops = [p for p in install.iterdir()]
    check("zip has exactly one top-level folder", len(tops) == 1 and tops[0].is_dir(), tops)
    app_dir = tops[0]
    exe = app_dir / "MySQL-DBA-Validator.exe"
    check("executable present after extraction", exe.is_file())
    before = tree(app_dir)

    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    # Not the bare SystemRoot folder: an all-users Python install puts py.exe there
    # (C:\Windows\py.exe on GitHub's Windows runners), which would make Python reachable.
    path = os.pathsep.join([str(Path(system_root) / "System32"),
                            str(Path(system_root) / "System32" / "Wbem")])
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "COMPUTERNAME",
                            "USERNAME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "NUMBER_OF_PROCESSORS",
                            "PROCESSOR_ARCHITECTURE", "OS", "PATHEXT", "COMSPEC"}}
    env.update(PATH=path, LOCALAPPDATA=str(appdata))
    found_python = shutil.which("python", path=path)
    found_py = shutil.which("py", path=path)
    print(f"diagnostic: app PATH = {path}")
    print(f"diagnostic: shutil.which('python', path=<app PATH>) -> {found_python}")
    print(f"diagnostic: shutil.which('py', path=<app PATH>) -> {found_py}")
    check("Python is not reachable on the app's PATH", found_python is None and found_py is None)
    check("no PYTHON* variables in the app's environment", not any(k.upper().startswith("PYTHON") for k in env))

    data = appdata / "MySQLDBAValidator"
    registry = data / "connector-targets.json"
    check("fixture: stand-in target approved in the default registry location", seed_registry(registry))
    registry_before = registry.read_bytes()

    # ------------------------------------------------------------------ first run
    app = App(exe, env, cwd)
    try:
        check("app starts: page, API and connector listening, pairing code printed", app.wait_ready(), app.output()[-2000:])
        out = app.output()
        check("console shows version, data folder and registry path",
              f"MySQL DBA Validator {version}" in out and str(data) in out and str(registry) in out, out[:1500])
        check("console reports company TLS CA not configured", "NOT CONFIGURED" in out)

        status, headers, body = request("GET", PAGE + "/")
        check("page served", status == 200 and b"<html" in body.lower() and b"targetSelect" in body, status)
        check("page has security headers", "default-src 'self'" in headers.get("content-security-policy", ""))
        status, _, body = request("GET", PAGE + "/api/health")
        health = as_json(body) or {}
        check("API health ok and reports the release version",
              status == 200 and health.get("status") == "ok" and health.get("version") == version, body[:200])
        check("decoy .env in working directory was NOT read",
              health.get("database", {}).get("configured") is False, health)
        status, _, body = request("POST", PAGE + "/api/validate", {"sql": "DELETE FROM users"})
        result = as_json(body) or {}
        check("static validation works (unrestricted DELETE is high risk)",
              status == 200 and result.get("statement_count") == 1
              and result.get("overall_risk_level") in {"HIGH", "CRITICAL"}, body[:300])
        status, _, body = request("POST", PAGE + "/api/validate", {"sql": "SELECT id FROM users WHERE id = 1"})
        result = as_json(body) or {}
        check("SQLGlot parsing works in the frozen build (simple SELECT is low risk)",
              status == 200 and result.get("overall_risk_level") == "LOW", body[:300])

        status, _, body = request("GET", CONNECTOR + "/health")
        check("connector health ok", status == 200 and (as_json(body) or {}).get("loopback_only") is True, body[:200])
        status, headers, _ = request("OPTIONS", CONNECTOR + "/pair", headers={
            "Origin": PAGE, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        check("connector CORS allows this machine's page",
              headers.get("access-control-allow-origin") == PAGE, (status, headers))
        status, headers, _ = request("OPTIONS", CONNECTOR + "/pair", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
        check("connector CORS refuses other origins", headers.get("access-control-allow-origin") is None, headers)
        status, _, body = request("POST", CONNECTOR + "/pair", {"pairing_token": app.pairing_code()},
                                  headers={"Origin": "http://evil.example"})
        check("pairing from a foreign origin refused", status == 401, (status, body[:200]))

        code = app.pairing_code()
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
        check("approved target listed from persisted registry; pending one hidden",
              status == 200 and "t-dns" in listed and "t-pending" not in listed, listed[:300])
        status, _, body = request("POST", CONNECTOR + "/company/targets", {
            "target_id": "evil", "display_name": "e", "host": "evil.example", "port": 3306}, headers=auth)
        check("browser session cannot register targets (403)", status == 403, status)
        status, _, body = request("POST", CONNECTOR + "/company/targets/t-dns/databases",
                                  {"username": USER, "password": SECRET}, headers=auth, timeout=30)
        disc = as_json(body) or {}
        check("company discovery fails closed without a TLS CA",
              status >= 400 and not disc.get("databases"), (status, body[:300]))
        status, _, body = request("POST", CONNECTOR + "/company/targets/t-dns/validate",
                                  {"sql": "SELECT 1", "username": USER, "password": SECRET}, headers=auth, timeout=30)
        check("remote validation answered by connector without exposing the password",
              SECRET not in body.decode(errors="replace"), status)
        check("company password never echoed in discovery response", SECRET not in json.dumps(disc))

        status, _, _ = request("POST", CONNECTOR + "/unpair", {}, headers=auth)
        status2, _, _ = request("GET", CONNECTOR + "/company/targets", headers=auth)
        check("unpair ends the session", status == 200 and status2 == 401, (status, status2))
    finally:
        graceful = app.stop_graceful()
    check("Ctrl+Break stops the app", graceful)
    check("both ports released after graceful stop (backend child stopped)", wait_ports_closed())

    # ------------------------------------------------------------------ restart + hard kill
    app2 = App(exe, env, cwd)
    try:
        check("restart works", app2.wait_ready(), app2.output()[-1500:])
        check("new pairing code issued on restart", app2.pairing_code() and app2.pairing_code() != code)
        status, _, _ = request("GET", CONNECTOR + "/company/targets", headers=auth)
        check("previous session invalid after restart (sessions are in memory only)", status == 401, status)
        status, _, body = request("POST", CONNECTOR + "/pair", {"pairing_token": app2.pairing_code()},
                                  headers={"Origin": "http://localhost:8420"})
        token2 = (as_json(body) or {}).get("session_token")
        status, _, body = request("GET", CONNECTOR + "/company/targets",
                                  headers={"Origin": "http://localhost:8420", "Authorization": f"Bearer {token2}"})
        check("localhost:8420 origin also pairs; target still approved after restart",
              token2 and status == 200 and "t-dns" in body.decode(), (status, body[:200]))
        status, _, body = request("GET", PAGE + "/api/health")
        check("backend responds after restart", status == 200)
        busy = App(exe, env, cwd)
        busy.proc.wait(timeout=60)
        check("second instance refuses to start while ports are in use",
              busy.proc.returncode == 3 and "already in use" in busy.output(), (busy.proc.returncode, busy.output()[-500:]))
    finally:
        app2.kill()
    check("hard kill of the launcher also stops the backend child (Job Object)", wait_ports_closed())

    # ------------------------------------------------------------------ persistent state + leaks
    after = tree(app_dir)
    check("program folder unchanged (nothing written next to the exe)", before == after,
          sorted(set(after) ^ set(before)) or "sizes changed")
    files = sorted(tree(data))
    check("app data contains only the registry and the backend log",
          set(files) <= {"connector-targets.json", "logs/backend.log"} and "logs/backend.log" in files, files)
    reg_text = registry.read_text(encoding="utf-8")
    check("registry is valid JSON with the target intact after kill",
          json.loads(reg_text)["kind"] and "t-dns" in reg_text and registry.read_bytes() == registry_before)
    log_text = (data / "logs" / "backend.log").read_text(encoding="utf-8", errors="replace")
    everything = [app.output(), app2.output(), log_text, reg_text] + [
        p.read_text(encoding="utf-8", errors="replace") for p in app_dir.rglob("*.txt")]
    check("test password never in console output, log, registry or program folder",
          not any(SECRET in t for t in everything))
    check("decoy .env password never appears anywhere", not any(DECOY in t for t in everything))
    check("no credential store created (no new files beyond registry/log)",
          not any(p.suffix in {".db", ".sqlite", ".key", ".pem"} for p in appdata.rglob("*")))

    passed = sum(ok for _, ok in results)
    print(f"\n{passed}/{len(results)} artifact checks passed")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
