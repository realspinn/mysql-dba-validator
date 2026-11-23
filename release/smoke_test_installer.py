"""Test the built Windows installer by installing, upgrading and uninstalling it.

    python release\\smoke_test_installer.py [dist\\MySQL-DBA-Validator-v<version>-windows-x64-setup.exe]

Run after release\\build_installer.py. It:

* builds a clearly older installer (version 0.0.1, same AppId, same script) as the
  starting point for the upgrade test;
* installs it silently for the current user into a temporary folder, with the
  application's data folder (LOCALAPPDATA) pointed at a temporary one holding disposable
  user data;
* checks the installed files, shortcuts and uninstall entry, and runs both executables
  from the installed folder (page, API, static validation, connector);
* checks an upgrade is refused while the app runs (nothing changed), then upgrades with
  the real installer: same installation, user data intact; checks a downgrade is refused;
* uninstalls: application files, shortcuts and the uninstall entry go, user data stays;
* checks the setup file's name, version resource and checksum.

Setup writes its uninstall entry (HKCU) and Start menu shortcuts for the real current user,
which cannot be redirected. So this refuses to run outside GitHub Actions unless --allow-local
is given, and always uninstalls what it installed. Needs Windows, free ports 8420 and 8765.
Not covered: a failure half way through copying files, Windows on Arm, other Windows versions.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from backend.version import APP_VERSION  # noqa: E402
from release import build_installer, smoke_test_artifact as smoke  # noqa: E402
from release.build_windows import APP_DIR_NAME, DIST, sha256  # noqa: E402

APP_GUID = "6A922AD5-1E15-4205-B269-916B86375374"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{{{APP_GUID}}}_is1"
OLD_VERSION = "0.0.1"
MARKER = "installed-by-setup.txt"
START_MENU = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "MySQL DBA Validator"
DESKTOP = Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "MySQL DBA Validator.lnk"

check = smoke.check


def hashes(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): sha256(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def user_files(data: Path) -> dict[str, str]:
    """The user's data, except the backend log, which every start of the app replaces."""
    return {k: v for k, v in hashes(data).items() if k != "logs/backend.log"}


def entry() -> dict | None:
    """The current user's uninstall entry for this application, or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return {name: winreg.QueryValueEx(key, name)[0] for name in ("DisplayVersion", "InstallLocation", "UninstallString")}
    except FileNotFoundError:
        return None


def run_setup(setup: Path, app: Path, log: Path, *extra: str, timeout: int = 900) -> int:
    command = [str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/DIR={app}", f"/LOG={log}", *extra]
    return subprocess.run(command, timeout=timeout, stdin=subprocess.DEVNULL).returncode


def shortcut(path: Path) -> tuple[str, str] | None:
    if not path.is_file():
        return None
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{path}'); "
              "'{0}|{1}' -f $s.TargetPath, $s.WorkingDirectory")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                         capture_output=True, text=True, timeout=60).stdout.strip()
    target, _, working = out.partition("|")
    return target, working


def wait_until(predicate, timeout: float) -> bool:
    return smoke.wait_until(predicate, timeout)


def app_environment(data_root: Path) -> dict:
    """The same minimal environment as the artifact smoke test, with a temporary data folder."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    path = os.pathsep.join([str(Path(system_root) / "System32"), str(Path(system_root) / "System32" / "Wbem")])
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "TEMP", "TMP", "COMPUTERNAME", "USERNAME",
                            "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
                            "OS", "PATHEXT", "COMSPEC"}}
    env.update(PATH=path, LOCALAPPDATA=str(data_root), MDV_UPDATE_CHECK="off")
    return env


def seed_user_data(data: Path) -> dict[str, str]:
    """Disposable stand-ins for the user's persistent data (never real credentials)."""
    (data / "trust").mkdir(parents=True)
    (data / "logs").mkdir()
    # A valid registry with one approved stand-in target, written by the connector's own code.
    smoke.seed_registry(data / "connector-targets.json")
    (data / ".env").write_text("MYSQL_HOST=127.0.0.1\n# disposable test file\n", encoding="utf-8")
    (data / "trust" / "company-ca.pem").write_text("not a real certificate: test data\n", encoding="utf-8")
    (data / "logs" / "backend.log").write_text("old log\n", encoding="utf-8")
    return user_files(data)


def stop(app: smoke.App | None) -> None:
    if app is not None and app.proc.poll() is None:
        app.stop_graceful()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("setup", nargs="?", type=Path, default=DIST / build_installer.setup_name(APP_VERSION))
    parser.add_argument("--allow-local", action="store_true", help="run outside GitHub Actions (installs for the current user)")
    args = parser.parse_args()
    if os.name != "nt":
        sys.exit("Windows only.")
    if os.environ.get("GITHUB_ACTIONS") != "true" and not args.allow_local:
        sys.exit("This installs for the current user (uninstall entry, Start menu). Run it on a CI runner, or pass --allow-local.")
    setup = args.setup.resolve()
    payload = (DIST / APP_DIR_NAME).resolve()
    if not setup.is_file() or not payload.is_dir():
        sys.exit(f"Needs {setup} and {payload}: run release/build_windows.py and release/build_installer.py first.")
    if smoke.port_open(8420) or smoke.port_open(8765):
        sys.exit("ports 8420/8765 are in use; stop any running validator first")
    if entry() is not None:
        sys.exit("The validator is already installed for this user; refusing to touch it.")

    work = Path(tempfile.mkdtemp(prefix="mdv installer ")).resolve()  # long form, as shortcuts store it
    app_dir = work / "Program Folder" / "MySQL DBA Validator"
    data_root = work / "Local App Data"
    data = data_root / "MySQLDBAValidator"
    cwd = work / "elsewhere"
    for folder in (data_root, cwd):
        folder.mkdir(parents=True)
    env = app_environment(data_root)
    print(f"work folder: {work}")
    app: smoke.App | None = None
    try:
        # ---------------------------------------------------------------- the setup file itself
        expected = build_installer.setup_name(APP_VERSION)
        check("setup file has the expected name", setup.name == expected, setup.name)
        info = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                               f"(Get-Item -LiteralPath '{setup}').VersionInfo.FileVersion"],
                              capture_output=True, text=True, timeout=60).stdout.strip()
        check("setup file's version resource matches backend/version.py", info == build_installer.numeric_version(APP_VERSION), info)
        sidecar = setup.with_name(setup.name + ".sha256")
        check("checksum file is '<sha256>  <name>' and matches the setup file",
              sidecar.read_bytes() == f"{sha256(setup)}  {setup.name}\n".encode("ascii"))

        # ---------------------------------------------------------------- older fixture (same script and AppId)
        old_payload = work / "old payload"
        shutil.copytree(payload, old_payload)
        (old_payload / "old-fixture-only.txt").write_text("only in the old fixture\n", encoding="utf-8")
        old_setup, engine = build_installer.compile_setup(build_installer.find_iscc(), old_payload, work / "old setup", OLD_VERSION)
        check(f"older installer fixture ({OLD_VERSION}) compiled with Inno Setup {engine}", old_setup.is_file())

        # ---------------------------------------------------------------- clean per-user install of the older one
        user_data = seed_user_data(data)
        code = run_setup(old_setup, app_dir, work / "old.log", "/TASKS=desktopicon")
        check("clean current-user installation completes (no administrator rights)", code == 0, code)
        found = entry()
        check("uninstall entry registered for this user with the old version",
              found is not None and found["DisplayVersion"] == OLD_VERSION and Path(found["InstallLocation"]) == app_dir, found)
        installed = hashes(app_dir)
        expected_files = {k: v for k, v in hashes(old_payload).items()}
        extra = {k for k in installed if k not in expected_files}
        check("installed files are the payload plus the marker and the uninstaller only",
              all(installed.get(k) == v for k, v in expected_files.items()) and extra <= {MARKER, "unins000.dat", "unins000.exe", "unins000.msg"}
              and MARKER in extra, sorted(extra))
        check("both executables are installed", (app_dir / "MySQL-DBA-Validator.exe").is_file() and (app_dir / "MySQL-DBA-Validator Console.exe").is_file())
        check("Start menu shortcut for the application", shortcut(START_MENU / "MySQL DBA Validator.lnk")
              == (str(app_dir / "MySQL-DBA-Validator.exe"), os.environ["USERPROFILE"]), shortcut(START_MENU / "MySQL DBA Validator.lnk"))
        check("Start menu shortcut for the Console edition",
              shortcut(START_MENU / "MySQL DBA Validator Console (company connector).lnk")
              == (str(app_dir / "MySQL-DBA-Validator Console.exe"), os.environ["USERPROFILE"]))
        check("optional desktop shortcut created when chosen", shortcut(DESKTOP) is not None)
        check("user data untouched by the installation", user_files(data) == user_data)

        # ---------------------------------------------------------------- run from the installed location
        console = app_dir / "MySQL-DBA-Validator Console.exe"
        app = smoke.App(console, env, cwd)
        check("installed Console app starts (page, API, connector, pairing code)", app.wait_ready(), app.output()[-1500:])
        status, _, body = smoke.request("GET", smoke.PAGE + "/api/health")
        health = smoke.as_json(body) or {}
        check("installed app serves the API on 127.0.0.1:8420 and reports the release version",
              status == 200 and health.get("version") == APP_VERSION, body[:200])
        status, _, body = smoke.request("POST", smoke.PAGE + "/api/validate", {"sql": "DELETE FROM users"})
        check("static SQL validation works in the installed app",
              status == 200 and (smoke.as_json(body) or {}).get("overall_risk_level") in {"HIGH", "CRITICAL"}, body[:200])
        status, _, body = smoke.request("GET", smoke.CONNECTOR + "/health")
        check("connector is loopback only", status == 200 and (smoke.as_json(body) or {}).get("loopback_only") is True, body[:200])
        _, headers, _ = smoke.request("OPTIONS", smoke.CONNECTOR + "/pair", headers={
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST"})
        status, _, _ = smoke.request("POST", smoke.CONNECTOR + "/pair", {"pairing_token": app.pairing_code()},
                                     headers={"Origin": "http://evil.example"})
        check("connector still refuses other origins", headers.get("access-control-allow-origin") is None and status == 401, status)
        check("program folder not written to while running", hashes(app_dir) == installed)

        # ---------------------------------------------------------------- upgrade refused while it runs
        before = hashes(app_dir)
        code = run_setup(setup, app_dir, work / "blocked.log")
        found = entry()
        check("upgrade is refused while the app runs", code != 0, code)
        check("a refused upgrade changes nothing and leaves the app running",
              hashes(app_dir) == before and found is not None and found["DisplayVersion"] == OLD_VERSION
              and app.proc.poll() is None and smoke.port_open(8420))
        check("the app was not closed or restarted by Setup", app.proc.poll() is None)
        stop(app)
        check("ports released after stopping the app", smoke.wait_ports_closed())
        app = None

        # ---------------------------------------------------------------- the real upgrade
        code = run_setup(setup, app_dir, work / "upgrade.log")
        found = entry()
        check("upgrade over the older installation completes", code == 0, code)
        check("same installation identity: one entry, new version, same folder",
              found is not None and found["DisplayVersion"] == APP_VERSION and Path(found["InstallLocation"]) == app_dir, found)
        installed = hashes(app_dir)
        new_files = hashes(payload)
        check("every file of the new payload is installed unchanged",
              all(installed.get(k) == v for k, v in new_files.items()) and installed.get(MARKER) is not None)
        check("user data survived the upgrade", user_files(data) == user_data)
        check("the desktop shortcut from the first installation is still there", shortcut(DESKTOP) is not None)

        # ---------------------------------------------------------------- downgrade refused
        before = hashes(app_dir)
        code = run_setup(old_setup, app_dir, work / "downgrade.log")
        found = entry()
        check("an older installer does not replace the newer installation",
              code != 0 and hashes(app_dir) == before and found is not None and found["DisplayVersion"] == APP_VERSION, (code, found))

        # ---------------------------------------------------------------- both executables after the upgrade
        windowed = subprocess.Popen([str(app_dir / "MySQL-DBA-Validator.exe"), "--no-browser"], cwd=str(cwd), env=env,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            started = wait_until(lambda: smoke.port_open(8420), 90)
            status, _, body = smoke.request("GET", smoke.PAGE + "/api/health")
            check("installed windowed app starts from the installed folder and serves the page",
                  started and status == 200 and (smoke.as_json(body) or {}).get("version") == APP_VERSION and not smoke.port_open(8765), body[:200])
            blocked = run_setup(setup, app_dir, work / "blocked2.log")
            check("upgrade is also refused while only the windowed app (and its background process) runs", blocked != 0, blocked)
        finally:
            windowed.kill()
            windowed.wait(timeout=10)
        check("ports released after the windowed app is killed", smoke.wait_ports_closed())
        app = smoke.App(app_dir / "MySQL-DBA-Validator Console.exe", env, cwd)
        check("installed Console app starts after the upgrade", app.wait_ready(), app.output()[-1500:])
        uninstaller = app_dir / "unins000.exe"
        code = subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300,
                              stdin=subprocess.DEVNULL).returncode
        time.sleep(3)
        check("uninstall is refused while the app runs, and changes nothing",
              (code != 0 or entry() is not None) and (app_dir / "MySQL-DBA-Validator.exe").is_file() and entry() is not None, code)
        stop(app)
        app = None
        check("ports released", smoke.wait_ports_closed())

        # ---------------------------------------------------------------- uninstall
        code = subprocess.run([str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300,
                              stdin=subprocess.DEVNULL).returncode
        gone = wait_until(lambda: entry() is None and not (app_dir / "MySQL-DBA-Validator.exe").exists(), 120)
        check("uninstall completes and removes the uninstall entry and the application files", code == 0 and gone, code)
        leftovers = sorted(hashes(app_dir)) if app_dir.exists() else []
        check("no application file is left in the installation folder", not leftovers, leftovers[:10])
        check("Start menu shortcuts and desktop shortcut removed",
              not (START_MENU / "MySQL DBA Validator.lnk").exists() and not DESKTOP.exists())
        check("user data preserved by uninstall", user_files(data) == user_data)
    finally:
        stop(app)
        if entry() is not None:  # a failed run must not leave an installation behind
            leftover = Path(entry()["InstallLocation"]) / "unins000.exe"
            if leftover.is_file() and str(work) in str(leftover):
                subprocess.run([str(leftover), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], timeout=300)
                wait_until(lambda: entry() is None, 120)
        shutil.rmtree(work, ignore_errors=True)

    passed = sum(ok for _, ok in smoke.results)
    print(f"\n{passed}/{len(smoke.results)} installer checks passed")
    return 0 if passed == len(smoke.results) else 1


if __name__ == "__main__":
    sys.exit(main())
