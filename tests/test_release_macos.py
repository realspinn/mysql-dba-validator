"""Tests for the macOS (Apple Silicon) release: launcher portability, build, audit, workflow.

They run on any platform. The macOS-only steps (PyInstaller bundle, codesign,
notarytool, stapler, spctl, hdiutil) are exercised by the release workflow on a macOS
runner; here their inputs, ordering and safety rules are tested.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest
import yaml

from release import audit_macos, build_macos, build_windows
from release import launcher

ROOT = Path(launcher.PROJECT_ROOT)
MACHO = bytes.fromhex("cffaedfe") + b"\x00" * 60  # 64-bit Mach-O header magic (little endian)


# ----------------------------------------------------------------------------- executable discovery

@pytest.mark.parametrize("platform, expected", [
    ("darwin", "MySQL-DBA-Validator Console"),
    ("win32", "MySQL-DBA-Validator Console.exe"),
])
def test_console_executable_name_per_platform(platform, expected):
    assert launcher.console_executable_name(platform) == expected


def test_frozen_macos_backend_runs_the_console_executable_in_contents_macos(monkeypatch, tmp_path):
    macos = tmp_path / "MySQL DBA Validator.app" / "Contents" / "MacOS"
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(macos / "MySQL-DBA-Validator"))
    command, _env = launcher.backend_command(900)
    assert command == [str(macos / "MySQL-DBA-Validator Console"), launcher.RUN_BACKEND_FLAG]


def test_frozen_windows_backend_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MySQL-DBA-Validator.exe"))
    command, _env = launcher.backend_command(900)
    assert command == [str(tmp_path / "MySQL-DBA-Validator Console.exe"), launcher.RUN_BACKEND_FLAG]


# ----------------------------------------------------------------------------- parent pipe

@pytest.mark.parametrize("posix", [True, False])
def test_parent_pipe_only_on_posix_and_never_inherited(monkeypatch, posix):
    monkeypatch.setattr(launcher, "uses_parent_pipe", lambda: posix)
    monkeypatch.setenv(launcher.PARENT_PIPE_ENV, "1")  # an inherited value is never passed on as such
    _command, env = launcher.backend_command()
    assert (env.get(launcher.PARENT_PIPE_ENV) == "1") is posix
    assert launcher.child_stdin() == (subprocess.PIPE if posix else subprocess.DEVNULL)


def test_windows_keeps_the_job_object_and_no_pipe():
    if os.name == "nt":
        assert launcher.uses_parent_pipe() is False
        assert launcher.child_stdin() == subprocess.DEVNULL
    else:
        assert launcher.uses_parent_pipe() is True
        assert launcher.kill_with_parent(object()) is None


def test_watch_parent_stops_at_end_of_file_and_forces_exit_after_the_grace_period():
    stopped, timers, exits = [], [], []
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"ignored bytes")
    os.close(write_fd)
    try:
        launcher.watch_parent(read_fd, lambda: stopped.append(True), grace=7.5, force_exit=exits.append,
                              start_timer=lambda seconds, action: timers.append((seconds, action)))
    finally:
        os.close(read_fd)
    assert stopped == [True]
    assert [seconds for seconds, _ in timers] == [7.5] and exits == []
    timers[0][1]()  # the grace period passes without the server stopping
    assert exits == [0]


def test_watch_parent_stops_when_the_pipe_read_fails():
    read_fd, write_fd = os.pipe()
    stopped = []
    try:  # reading the write end fails (EBADF)
        launcher.watch_parent(write_fd, lambda: stopped.append(True), start_timer=lambda *_: None)
    finally:
        os.close(read_fd)
        os.close(write_fd)
    assert stopped == [True]


CHILD = textwrap.dedent("""
    import sys, threading
    sys.path.insert(0, sys.argv[1])
    from release.launcher import watch_parent
    stopped = threading.Event()
    threading.Thread(target=watch_parent, args=(sys.stdin.fileno(), stopped.set),
                     kwargs={"start_timer": lambda *_: None}, daemon=True).start()
    print("stopped" if stopped.wait(30) else "still running", flush=True)
""")
LAUNCHER = textwrap.dedent("""
    import subprocess, sys, time
    child = subprocess.Popen([sys.executable, "-c", sys.argv[2], sys.argv[1]], stdin=subprocess.PIPE,
                             stdout=open(sys.argv[3], "w"))
    print(child.pid, flush=True)
    time.sleep(120)
""")


def test_killing_the_launcher_stops_its_backend_child(tmp_path):
    """The real mechanism, across processes: the launcher is killed, the child sees end of file."""
    out = tmp_path / "child.txt"
    middle = subprocess.Popen([sys.executable, "-c", LAUNCHER, str(ROOT), CHILD, str(out)],
                              stdout=subprocess.PIPE, text=True)
    child_pid = int(middle.stdout.readline())
    assert child_pid > 0
    time.sleep(1.0)
    middle.kill()  # SIGKILL on POSIX, TerminateProcess on Windows: no cleanup code runs
    middle.wait(timeout=10)
    for _ in range(200):
        if out.exists() and out.read_text().strip():
            break
        time.sleep(0.1)
    assert out.read_text().strip() == "stopped"


class FakeServer:
    instances: list["FakeServer"] = []

    def __init__(self, config):
        self.should_exit = False
        FakeServer.instances.append(self)

    def run(self):
        deadline = time.monotonic() + 20
        while not self.should_exit and time.monotonic() < deadline:
            time.sleep(0.05)


def _fake_uvicorn(calls):
    return types.SimpleNamespace(Server=FakeServer, Config=lambda app, **kwargs: kwargs,
                                 run=lambda app, **kwargs: calls.append(kwargs))


def test_backend_with_the_parent_pipe_stops_when_the_launcher_closes_it(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "uvicorn", _fake_uvicorn(calls))
    monkeypatch.setenv(launcher.PARENT_PIPE_ENV, "1")
    monkeypatch.delenv(launcher.IDLE_SHUTDOWN_ENV, raising=False)
    read_fd, write_fd = os.pipe()
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(fileno=lambda: read_fd))
    # The real watcher, minus its force-exit timer (which would end the test process).
    real_watch = launcher.watch_parent
    monkeypatch.setattr(launcher, "watch_parent",
                        lambda fd, stop: real_watch(fd, stop, start_timer=lambda *_: None))
    FakeServer.instances.clear()
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("code", launcher.run_backend()))
    worker.start()
    time.sleep(0.5)
    assert worker.is_alive() and calls == []  # serving through uvicorn.Server, not uvicorn.run
    os.close(write_fd)  # the launcher is gone
    worker.join(timeout=15)
    os.close(read_fd)
    assert not worker.is_alive() and result["code"] == 0
    assert FakeServer.instances[-1].should_exit is True


IDLE_BACKEND = textwrap.dedent("""
    import sys, time, types
    sys.path.insert(0, sys.argv[1])

    class Server:  # uvicorn.Server stand-in: serves until told to stop
        def __init__(self, config):
            self.should_exit = False

        def run(self):
            while not self.should_exit:
                time.sleep(0.05)

    sys.modules["uvicorn"] = types.SimpleNamespace(Server=Server, Config=lambda app, **kwargs: kwargs)
    from release import launcher
    sys.exit(launcher.run_backend())
""")


def test_backend_stops_cleanly_on_idle_while_the_launcher_still_holds_the_pipe(tmp_path):
    """The windowed app's idle shutdown: the backend ends on its own while its parent-pipe
    watcher is still waiting on the open pipe. It must exit 0, not abort (SIGABRT) at
    interpreter shutdown, as a watcher blocked in a buffered stdin read made it do."""
    env = dict(os.environ, **{launcher.PARENT_PIPE_ENV: "1", launcher.IDLE_SHUTDOWN_ENV: "1"})
    child = subprocess.Popen([sys.executable, "-c", IDLE_BACKEND, str(ROOT)], cwd=tmp_path, env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:  # the launcher stays alive: the pipe is never closed while the backend runs
        output = child.stdout.read().decode(errors="replace")
        code = child.wait(timeout=60)
    finally:
        child.stdin.close()
        if child.poll() is None:
            child.kill()
    assert "No page heartbeat for 1 seconds; stopping." in output
    assert "Fatal Python error" not in output, output
    assert code == 0, output


def test_backend_without_parent_pipe_or_idle_shutdown_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "uvicorn", _fake_uvicorn(calls))
    monkeypatch.delenv(launcher.PARENT_PIPE_ENV, raising=False)
    monkeypatch.delenv(launcher.IDLE_SHUTDOWN_ENV, raising=False)
    assert launcher.run_backend() == 0
    assert calls == [{"host": "127.0.0.1", "port": 8420, "log_level": "info"}]


# ----------------------------------------------------------------------------- visible errors on macOS

def test_macos_error_is_an_alert_whose_message_is_never_script_text(monkeypatch, capsys):
    started = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, **kwargs: started.append((command, kwargs)))
    hostile = 'port busy" & do shell script "touch /tmp/pwned" & "'
    launcher.show_error(hostile, platform="darwin")
    command, kwargs = started[0]
    assert command[0] == "/usr/bin/osascript"
    script = [command[i + 1] for i, arg in enumerate(command) if arg == "-e"]
    assert script == list(launcher.MACOS_ALERT_SCRIPT)
    assert command[-2:] == [launcher.APP_TITLE, hostile]  # data, not code
    assert kwargs["start_new_session"] is True  # the launcher does not wait for the alert
    assert hostile in capsys.readouterr().err


def test_macos_error_still_reaches_stderr_when_the_alert_cannot_start(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise OSError("no osascript")

    monkeypatch.setattr(launcher.subprocess, "Popen", fail)
    launcher.show_error("cannot start", platform="darwin")
    assert "cannot start" in capsys.readouterr().err


def test_macos_missing_console_message_names_the_bundle_executable(monkeypatch, tmp_path):
    errors = []
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MySQL-DBA-Validator"))
    monkeypatch.setattr(launcher, "show_error", errors.append)
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(launcher, "port_in_use", lambda port: False)
    assert launcher.run_windowed(["--no-browser"]) == 4
    assert "MySQL-DBA-Validator Console is missing" in errors[0] and "disk image" in errors[0]


# ----------------------------------------------------------------------------- spec and dependency lock

def test_macos_spec_builds_an_arm64_bundle_with_no_identity_and_no_entitlements():
    spec = (ROOT / "release" / "mysql-dba-validator-macos.spec").read_text(encoding="utf-8")
    assert f'("X {launcher.WINDOWED_XOPTION}", None, "OPTION")' in spec
    assert 'name="MySQL-DBA-Validator",' in spec and 'name="MySQL-DBA-Validator Console",' in spec
    assert spec.count("console=False") == 1 and spec.count("console=True") == 1
    assert 'TARGET_ARCH = "arm64"' in spec and spec.count("target_arch=TARGET_ARCH") == 2
    assert spec.count("codesign_identity=None") == 2 and spec.count("entitlements_file=None") == 2
    assert '"LSUIElement": True' in spec and 'name="MySQL DBA Validator.app"' in spec
    # PyInstaller takes CFBundleExecutable from the first executable collected: the windowed app.
    assert re.search(r"COLLECT\(\s*app_exe,\s*console_exe,", spec)
    assert (ROOT / "release" / "mysql-dba-validator.icns").read_bytes()[:4] == b"icns"


def _pins(name):
    lines = (ROOT / "release" / name).read_text(encoding="utf-8").splitlines()
    pins = {}
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if line:
            package, _, version = line.partition("==")
            assert version, f"{name}: {line!r} is not an exact pin"
            pins[package.split("[")[0].lower().replace("_", "-")] = version
    return pins


def test_macos_lock_is_fully_pinned_and_matches_the_windows_lock():
    mac, windows = _pins("requirements-build-macos.txt"), _pins("requirements-build.txt")
    assert set(mac) - set(windows) == {"uvloop", "macholib"}
    assert set(windows) - set(mac) == {"pefile", "pywin32-ctypes"}
    assert all(mac[name] == windows[name] for name in set(mac) & set(windows))


# ----------------------------------------------------------------------------- signing and notarization

def _fake_bundle(tmp_path: Path) -> Path:
    app = tmp_path / "MySQL DBA Validator.app"
    files = {
        "Contents/MacOS/MySQL-DBA-Validator": MACHO,
        "Contents/MacOS/MySQL-DBA-Validator Console": MACHO,
        "Contents/Frameworks/libpython3.14.dylib": MACHO,
        "Contents/Frameworks/lib-dynload/_ssl.cpython-314-darwin.so": MACHO,
        "Contents/Frameworks/Python.framework/Versions/3.14/Python": MACHO,
        "Contents/Resources/base_library.zip": b"PK\x03\x04",
        "Contents/Info.plist": b"<plist/>",
    }
    for rel, data in files.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_bytes(data)
    return app


def test_signing_order_is_inside_out_with_the_main_executable_and_app_last(tmp_path):
    app = _fake_bundle(tmp_path)
    order = [p.relative_to(tmp_path).as_posix() for p in build_macos.signing_order(app)]
    name = "MySQL DBA Validator.app"
    assert order[-3:] == [f"{name}/Contents/MacOS/MySQL-DBA-Validator Console",
                          f"{name}/Contents/MacOS/MySQL-DBA-Validator", name]
    framework = order.index(f"{name}/Contents/Frameworks/Python.framework")
    assert order.index(f"{name}/Contents/Frameworks/Python.framework/Versions/3.14/Python") < framework
    assert f"{name}/Contents/Resources/base_library.zip" not in order  # not code
    assert len(order) == len(set(order)) == 7


def test_signing_never_follows_or_signs_symbolic_links(tmp_path):
    app = _fake_bundle(tmp_path)
    try:
        (app / "Contents" / "MacOS" / "frontend").symlink_to("../Resources", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links not available")
    assert all(not p.is_symlink() for p in build_macos.signing_order(app))


def test_codesign_uses_hardened_runtime_and_secure_timestamp_without_entitlements_or_deep():
    command = build_macos.codesign_command("Developer ID Application: X (T)", Path("a.app"))
    assert command[:5] == ["/usr/bin/codesign", "--force", "--timestamp", "--sign", "Developer ID Application: X (T)"]
    assert "--options" in command and command[command.index("--options") + 1] == "runtime"
    assert "--entitlements" not in command and "--deep" not in command
    dmg = build_macos.codesign_command("Developer ID Application: X (T)", Path("a.dmg"), hardened_runtime=False)
    assert "--options" not in dmg and "--timestamp" in dmg


def test_only_a_developer_id_application_identity_is_accepted(monkeypatch):
    monkeypatch.setenv("MDV_CODESIGN_IDENTITY", "Apple Development: someone (T)")
    with pytest.raises(SystemExit):
        build_macos.codesign_identity()
    monkeypatch.delenv("MDV_CODESIGN_IDENTITY")
    with pytest.raises(SystemExit):
        build_macos.codesign_identity()
    monkeypatch.setenv("MDV_CODESIGN_IDENTITY", "Developer ID Application: Example (TEAM123)")
    assert build_macos.codesign_identity() == "Developer ID Application: Example (TEAM123)"


def test_notary_commands_put_authentication_last_and_never_show_it(monkeypatch, tmp_path):
    key = tmp_path / "AuthKey_TEST.p8"
    key.write_text("key", encoding="utf-8")
    monkeypatch.setenv("MDV_NOTARY_KEY_PATH", str(key))
    monkeypatch.setenv("MDV_NOTARY_KEY_ID", "KEYID12345")
    monkeypatch.setenv("MDV_NOTARY_ISSUER", "issuer-0000-1111")
    auth = build_macos.notary_auth_args()
    submit = build_macos.notary_submit_command(Path("app.zip"), auth)
    assert submit[:4] == ["/usr/bin/xcrun", "notarytool", "submit", "app.zip"] and "--wait" in submit
    assert submit[-len(auth):] == auth
    shown = build_macos.shown_command(submit, len(auth))
    assert "KEYID12345" not in shown and "issuer-0000-1111" not in shown and str(key) not in shown
    assert shown.endswith("<authentication>")
    log = build_macos.notary_log_command("abc", Path("log.json"), auth)
    assert log[-len(auth):] == auth and "KEYID12345" not in build_macos.shown_command(log, len(auth))
    monkeypatch.setenv("MDV_NOTARY_KEY_PATH", str(tmp_path / "missing.p8"))
    with pytest.raises(SystemExit):
        build_macos.notary_auth_args()


def test_only_an_accepted_notarization_passes():
    assert build_macos.notary_accepted({"status": "Accepted", "id": "x"})
    assert not build_macos.notary_accepted({"status": "Invalid"})
    assert not build_macos.notary_accepted({})


def test_artifact_name_and_version_source():
    assert build_macos.read_version() == build_windows.read_version()
    assert build_macos.dmg_path("0.4.1").name == "MySQL-DBA-Validator-v0.4.1-macos-arm64.dmg"


# ----------------------------------------------------------------------------- macOS audit

VERSION = "9.9.9"


def _audit_bundle(tmp_path: Path, version: str = VERSION) -> Path:
    app = tmp_path / "MySQL DBA Validator.app"
    contents = app / "Contents"
    for rel, data in {
        "MacOS/MySQL-DBA-Validator": MACHO,
        "MacOS/MySQL-DBA-Validator Console": MACHO,
        "Frameworks/lib-dynload/_ssl.so": MACHO,
        "Resources/frontend/index.html": (ROOT / "frontend" / "index.html").read_bytes(),
        "Resources/frontend/assets/favicon.svg": (ROOT / "frontend" / "assets" / "favicon.svg").read_bytes(),
        "Resources/mysql-dba-validator.icns": b"icns",
    }.items():
        (contents / rel).parent.mkdir(parents=True, exist_ok=True)
        (contents / rel).write_bytes(data)
    # Executable, as PyInstaller ships them: on macOS the audit requires it.
    for name in ("MySQL-DBA-Validator", "MySQL-DBA-Validator Console"):
        (contents / "MacOS" / name).chmod(0o755)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "MySQL-DBA-Validator", "CFBundleShortVersionString": version,
        "CFBundleVersion": version, "LSUIElement": True}))
    return app


@pytest.fixture
def embedded(monkeypatch):
    """Embedded archive contents for both executables (PyInstaller is not needed here)."""
    entries = [("PYZ:backend.version", f'APP_VERSION = "{VERSION}"'.encode()), ("PYZ:backend.main", b"code")]
    monkeypatch.setattr(audit_macos, "embedded_entries", lambda _data: iter(entries))
    monkeypatch.setattr(audit_macos.base, "machine_paths", lambda: [])
    return entries


def test_macos_audit_passes_a_clean_bundle(tmp_path, embedded):
    findings, stats = audit_macos.audit(_audit_bundle(tmp_path), VERSION, tmp_path / "no.env", [])
    assert findings == []
    assert stats["embedded_modules"] == 4 and stats["files"] == 7


def test_macos_audit_requires_executable_bits(tmp_path, embedded):
    """On macOS a bundle executable without the executable bit is a finding (Windows has no such bit)."""
    app = _audit_bundle(tmp_path)
    (app / "Contents" / "MacOS" / "MySQL-DBA-Validator Console").chmod(0o644)
    findings, _ = audit_macos.audit(app, VERSION, tmp_path / "no.env", [])
    assert findings == ([] if os.name == "nt" else ["not executable: Contents/MacOS/MySQL-DBA-Validator Console"])


@pytest.mark.parametrize("rel", [
    "Contents/Resources/signing.p12", "Contents/Resources/AuthKey_ABC123.p8", "Contents/Resources/login.keychain-db",
    "Contents/Resources/build.keychain", "Contents/embedded.provisionprofile", "Contents/Resources/x.mobileprovision",
    "Contents/Resources/request.certSigningRequest", "Contents/Resources/company-ca.pem",
    "Contents/Resources/connector-targets.json", "Contents/Resources/.env", "Contents/Resources/backend/main.py",
    "Contents/Resources/tests/test_x.py",
])
def test_macos_audit_rejects_signing_material_secrets_files_and_source(tmp_path, embedded, rel):
    app = _audit_bundle(tmp_path)
    (app / rel).parent.mkdir(parents=True, exist_ok=True)
    (app / rel).write_bytes(b"x")
    findings, _ = audit_macos.audit(app, VERSION, tmp_path / "no.env", [])
    assert any(rel in f and f.startswith("forbidden file") for f in findings), findings


def test_macos_audit_checks_structure_version_and_page(tmp_path, embedded):
    app = _audit_bundle(tmp_path, version="1.0.0")
    (app / "Contents" / "MacOS" / "extra-tool").write_bytes(MACHO)
    (app / "Contents" / "Resources" / "frontend" / "index.html").write_text("<html>changed</html>", encoding="utf-8")
    (app / "Contents" / "MacOS" / "MySQL-DBA-Validator Console").write_bytes(b"#!/bin/sh\n")
    findings, _ = audit_macos.audit(app, VERSION, tmp_path / "no.env", [])
    text = "\n".join(findings)
    assert f"Info.plist version is not {VERSION}" in text
    assert "unexpected file in Contents/MacOS: Contents/MacOS/extra-tool" in text
    assert "served page differs from the source tree" in text
    assert "not a Mach-O executable: Contents/MacOS/MySQL-DBA-Validator Console" in text


def test_macos_audit_requires_lsuielement_the_windowed_executable_and_the_embedded_version(tmp_path, monkeypatch):
    app = _audit_bundle(tmp_path)
    (app / "Contents" / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": "MySQL-DBA-Validator Console", "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION}))
    monkeypatch.setattr(audit_macos, "embedded_entries", lambda _d: iter([("PYZ:backend.version", b"0.0.1"),
                                                                       ("PYZ:pytest", b"")]))
    monkeypatch.setattr(audit_macos.base, "machine_paths", lambda: [])
    findings, _ = audit_macos.audit(app, VERSION, tmp_path / "no.env", [])
    text = "\n".join(findings)
    assert "CFBundleExecutable is not MySQL-DBA-Validator" in text
    assert "LSUIElement is not true" in text
    assert f"embedded backend.version does not contain {VERSION}" in text
    assert "forbidden module bundled: pytest" in text


def test_macos_audit_finds_signing_secrets_from_the_environment_without_printing_them(tmp_path, embedded,
                                                                                     monkeypatch):
    app = _audit_bundle(tmp_path)
    (app / "Contents" / "Resources" / "notes.txt").write_text("cert password Hunter-2-Secret", encoding="utf-8")
    monkeypatch.setenv("MDV_FORBID_TEST", "Hunter-2-Secret")
    findings, stats = audit_macos.audit(app, VERSION, tmp_path / "no.env", [], ["MDV_FORBID_TEST", "MDV_UNSET_VAR"])
    text = "\n".join(findings)
    assert "--forbid-env MDV_FORBID_TEST found in Contents/Resources/notes.txt" in text
    assert "--forbid-env MDV_UNSET_VAR is not set" in text
    assert "Hunter-2-Secret" not in text and stats["secret_values_checked"] == 1


def test_macos_audit_allows_upstream_cargo_paths_only_in_compiled_code(tmp_path, embedded, monkeypatch):
    monkeypatch.setattr(audit_macos.base, "machine_paths", lambda: [(audit_macos.base.HOME_LABEL, "/Users/runner")])
    app = _audit_bundle(tmp_path)
    cargo = b"/Users/runner/.cargo/registry/src/index.crates.io-x/pyo3/src/err.rs"
    (app / "Contents" / "Frameworks" / "lib-dynload" / "_rust.so").write_bytes(MACHO + cargo)
    (app / "Contents" / "Resources" / "notes.txt").write_bytes(cargo)
    findings, _ = audit_macos.audit(app, VERSION, tmp_path / "no.env", [])
    assert findings == ["build user's home folder found in Contents/Resources/notes.txt"]


# Strings found in the pinned macOS wheels (run 37766741435 flagged exactly these files).
UPSTREAM_WHEEL_PATHS = {
    "_cffi_backend.cpython-314-darwin.so": b"/Users/runner/work/cffi/cffi/src/c/_cffi_backend.c",
    "cryptography/hazmat/bindings/_rust.abi3.so":
        b"/Users/runner/.cargo/registry/src/index.crates.io-1949cf8c6b5b557f/asn1-0.21.3/src/writer.rs\x00"
        b"/Users/runner/work/cryptography/cryptography/src/rust/src/lib.rs\x00"
        b"/Users/runner/work/infra/infra/artifact/lib/ossl-modules",
    "httptools/parser/parser.cpython-314-darwin.so": b"/Users/runner/work/httptools/httptools/httptools/parser/",
    "uvloop/loop.cpython-314-darwin.so": b"/Users/runner/work/uvloop/uvloop/uvloop/loop.pyx",
    "websockets/speedups.cpython-314-darwin.so": b"/Users/runner/work/websockets/websockets/src/websockets/",
    "yaml/_yaml.cpython-314-darwin.so": b"/Users/runner/work/pyyaml/pyyaml/yaml/_yaml.pyx\x00"
                                        b"/Users/runner/work/pyyaml/pyyaml/pyyaml/../libyaml/src/.libs/libyaml.a(api.o)",
}
RUNNER_HOME = "/Users/runner"
RUNNER_CHECKOUT = "/Users/runner/work/mysql-dba-validator/mysql-dba-validator"


def test_macos_audit_allows_upstream_wheel_build_paths_only_in_macho_content(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_macos.base, "machine_paths", lambda: [
        (audit_macos.base.HOME_LABEL, RUNNER_HOME), ("source checkout path", RUNNER_CHECKOUT)])
    # This project's own code (the embedded archive) is never exempt.
    monkeypatch.setattr(audit_macos, "embedded_entries", lambda _data: iter([
        ("PYZ:backend.version", f'APP_VERSION = "{VERSION}"'.encode()),
        ("PYZ:backend.main", b"/Users/runner/work/cffi/cffi/x")]))
    app = _audit_bundle(tmp_path)
    frameworks = app / "Contents" / "Frameworks"
    for rel, text in UPSTREAM_WHEEL_PATHS.items():
        (frameworks / rel).parent.mkdir(parents=True, exist_ok=True)
        (frameworks / rel).write_bytes(MACHO + text + b"\x00")
    rejected = {
        # This repository, in any case, even in the upstream workspace layout.
        "own.so": MACHO + RUNNER_CHECKOUT.encode() + b"/backend/main.py",
        "own_upper.so": MACHO + b"/Users/runner/work/MySQL-DBA-Validator/MySQL-DBA-Validator/build/x",
        # Not a repository workspace: runner temp, tool cache, other home folders, one-level work paths.
        "temp.so": MACHO + b"/Users/runner/work/_temp/build/x.c",
        "toolcache.so": MACHO + b"/Users/runner/hostedtoolcache/Python/3.14.7/arm64/lib",
        "documents.so": MACHO + b"/Users/runner/Documents/notes",
        "onelevel.so": MACHO + b"/Users/runner/work/foo/bar/x.c",
        # Crafted to look upstream but climbing out of it.
        "escape.so": MACHO + b"/Users/runner/work/cffi/cffi/../../mysql-dba-validator/x",
        # A real upstream path next to a genuine leak.
        "mixed.so": MACHO + UPSTREAM_WHEEL_PATHS["yaml/_yaml.cpython-314-darwin.so"] + b"\x00/Users/runner/.ssh/id",
        # UTF-16 stays strict, as in the Windows audit.
        "wide.so": MACHO + "/Users/runner/work/cffi/cffi/x".encode("utf-16-le"),
        # A .so name without Mach-O content is not compiled code.
        "named.so": b"/Users/runner/work/cffi/cffi/x",
    }
    for name, data in rejected.items():
        (frameworks / name).write_bytes(data)
    resources = app / "Contents" / "Resources"
    (resources / "notes.txt").write_bytes(b"/Users/runner/work/cffi/cffi/x")
    (resources / "build.cfg").write_bytes(b"/Users/runner/.cargo/registry/src/x")

    findings, _ = audit_macos.audit(app, VERSION, tmp_path / "no.env", [])
    home = "build user's home folder found in "
    expected = [home + f"Contents/Frameworks/{name}" for name in rejected]
    expected += [f"source checkout path found in Contents/Frameworks/{name}" for name in ("own.so", "own_upper.so")]
    expected += [home + "Contents/Resources/notes.txt", home + "Contents/Resources/build.cfg"]
    expected += [home + f"{exe}!PYZ:backend.main" for exe in audit_macos.EXECUTABLES]
    assert sorted(findings) == sorted(expected)


@pytest.mark.parametrize("path, upstream", [
    (b"/users/runner/work/cffi/cffi/src/c/", True),
    (b"/users/runner/work/infra/infra/artifact/lib/ossl-modules", True),
    (b"/users/runner/.cargo/registry/src/index.crates.io-x/pyo3/src/err.rs", True),
    (b"/users/runner/work/mysql-dba-validator/mysql-dba-validator/backend/", False),
    (b"/users/runner/work/cffi/other/src/", False),
    (b"/users/runner/work/cffi/cffi", False),  # no path inside the workspace
    (b"/users/runner/work/cffi/cffi/a/../../x", False),  # climbs out of the workspace
    (b"/users/runner/work/cffi/cffi/../cffi/x", False),
    (b"/users/runner/.cargo/registry/../../work/mysql-dba-validator/x", False),
    # Real PyYAML path: ".." that stays inside its own workspace.
    (b"/users/runner/work/pyyaml/pyyaml/pyyaml/../libyaml/src/.libs/libyaml.a(api.o)", True),
    (b"/users/runner/workx/cffi/cffi/a", False),
    (b"/users/runner/library/caches/x", False),
])
def test_upstream_build_path_rule(path, upstream):
    assert audit_macos.upstream_build_path(path, len(b"/users/runner")) is upstream


def test_this_repository_is_never_an_upstream_workspace():
    assert b"mysql-dba-validator" in audit_macos.OWN_WORKSPACE_NAMES
    assert audit_macos.ROOT.name.lower().encode() in audit_macos.OWN_WORKSPACE_NAMES


def test_macos_audit_rejects_links_that_leave_the_bundle(tmp_path, embedded):
    app = _audit_bundle(tmp_path)
    try:
        (app / "Contents" / "Resources" / "escape").symlink_to("/etc")
        (app / "Contents" / "MacOS" / "frontend").symlink_to("../Resources/frontend", target_is_directory=True)
    except OSError:
        pytest.skip("symbolic links not available")
    findings, _ = audit_macos.audit(app, VERSION, tmp_path / "no.env", [])
    assert findings == ["symbolic link points outside the bundle: Contents/Resources/escape"]


# The audit runs on macOS only. Windows does not follow "b" before applying "..":
# there "escape" resolves to Contents/Resources (and cannot be opened at all), so
# the chain this test needs does not exist.
@pytest.mark.skipif(os.name == "nt", reason="POSIX link resolution; the macOS audit runs on macOS")
def test_macos_audit_follows_link_chains_physically(tmp_path, embedded):
    """"escape" -> "b/.." looks inside, but b links to the bundle root, so it really lands outside."""
    app = _audit_bundle(tmp_path)
    resources = app / "Contents" / "Resources"
    try:
        (resources / "b").symlink_to("../..", target_is_directory=True)  # the bundle root: inside
        (resources / "escape").symlink_to("b/..", target_is_directory=True)  # physically: the bundle's parent
    except OSError:
        pytest.skip("symbolic links not available")
    assert os.path.normpath(os.path.join(resources, "b/..")).startswith(str(app))  # a lexical check is fooled
    findings, _ = audit_macos.audit(app, VERSION, tmp_path / "no.env", [])
    assert findings == ["symbolic link points outside the bundle: Contents/Resources/escape"]


def _volume(tmp_path: Path) -> Path:
    volume = tmp_path / "volume"
    volume.mkdir()
    _audit_bundle(volume)
    (volume / "README.txt").write_text("readme", encoding="utf-8")
    (volume / "LICENSE.txt").write_text("MIT", encoding="utf-8")
    return volume


def _audit_contents(check, folder: Path, tmp_path: Path) -> list[str]:
    findings: list[str] = []
    stats = {"files": 0, "bytes": 0, "embedded_modules": 0, "secret_values_checked": 0}
    rules, _ = audit_macos.content_rules(tmp_path / "no.env", [], [], findings)
    check(folder, VERSION, rules, findings, stats)
    return findings


def test_disk_image_audit_skips_only_volume_metadata_and_checks_hidden_items(tmp_path, embedded):
    volume = _volume(tmp_path)
    (volume / ".fseventsd").mkdir()
    (volume / ".DS_Store").write_bytes(b"\x00")
    assert _audit_contents(audit_macos.audit_volume, volume, tmp_path) == []
    (volume / ".env").write_text("MYSQL_PASSWORD=x", encoding="utf-8")
    (volume / ".hidden-notes").write_text("x", encoding="utf-8")
    (volume / "extra.pkg").write_bytes(b"x")
    assert sorted(_audit_contents(audit_macos.audit_volume, volume, tmp_path)) == [
        "unexpected item on the disk image: .env",
        "unexpected item on the disk image: .hidden-notes",
        "unexpected item on the disk image: extra.pkg"]


def test_disk_image_readme_must_be_a_regular_file(tmp_path, embedded):
    volume = _volume(tmp_path)
    (volume / "README.txt").unlink()
    (volume / "README.txt").mkdir()
    assert _audit_contents(audit_macos.audit_volume, volume, tmp_path) == [
        "disk image 'README.txt' is not a regular file"]


def test_unsigned_archive_must_hold_exactly_the_app(tmp_path, embedded):
    folder = tmp_path / "unpacked"
    folder.mkdir()
    _audit_bundle(folder)
    assert _audit_contents(audit_macos.audit_zip_contents, folder, tmp_path) == []
    (folder / ".secret").write_text("x", encoding="utf-8")
    assert _audit_contents(audit_macos.audit_zip_contents, folder, tmp_path) == [
        "unexpected item in the archive: .secret"]


def test_audit_says_what_its_value_checks_cannot_prove():
    assert "What this does not prove" in audit_macos.__doc__ and "backstop" in audit_macos.__doc__


# ----------------------------------------------------------------------------- build commands

def test_build_uses_the_pinned_lock_wheels_only_and_passes_the_version(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(build_macos, "require_macos_arm64", lambda: None)
    monkeypatch.setattr(build_macos, "BUILD", tmp_path / "build")
    monkeypatch.setattr(build_macos, "DIST", tmp_path / "dist")
    monkeypatch.setattr(build_macos, "run", lambda command, **kwargs: commands.append((command, kwargs)))
    assert build_macos.cmd_build() == 0
    version = build_macos.read_version()
    install = next(c for c, _ in commands if "install" in c)
    assert install[install.index("-r") + 1].endswith("requirements-build-macos.txt")
    assert install[install.index("--only-binary") + 1] == ":all:"
    pyinstaller, kwargs = next((c, k) for c, k in commands if "PyInstaller" in c)
    assert pyinstaller[-1].endswith("mysql-dba-validator-macos.spec")
    assert kwargs["env"]["MDV_VERSION"] == version  # the bundle version comes from backend/version.py
    audit = commands[-1][0]
    assert audit[1].endswith("audit_macos.py") and audit[-2:] == ["--version", version]
    assert not any("codesign" in " ".join(c) or "notarytool" in " ".join(c) for c, _ in commands)


def test_unsigned_package_is_a_ditto_zip_with_checksum_and_never_signs(monkeypatch, tmp_path):
    commands = []
    monkeypatch.setattr(build_macos, "require_macos_arm64", lambda: None)
    monkeypatch.setattr(build_macos, "DIST", tmp_path)
    (tmp_path / build_macos.APP_NAME).mkdir()

    def run(command, **kwargs):
        commands.append(command)
        Path(command[-1]).write_bytes(b"zip bytes")  # what ditto would write

    monkeypatch.setattr(build_macos, "run", run)
    assert build_macos.cmd_package_unsigned("9.9.9") == 0
    archive = tmp_path / "MySQL-DBA-Validator-v9.9.9-macos-arm64-unsigned.zip"
    assert commands == [["/usr/bin/ditto", "-c", "-k", "--keepParent", str(tmp_path / build_macos.APP_NAME),
                         str(archive)]]
    checksum = (tmp_path / (archive.name + ".sha256")).read_text(encoding="ascii")
    assert checksum == f"{build_windows.sha256(archive)}  {archive.name}\n"


def test_code_run_by_the_signing_job_needs_only_the_standard_library():
    """The signing job runs `python -I` with nothing installed: these files import stdlib (and each other) only."""
    import ast

    for name in ("build_macos.py", "build_windows.py"):
        tree = ast.parse((ROOT / "release" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                top = module.split(".")[0]
                assert top in sys.stdlib_module_names or top in ("release", "__future__"), (name, module)


def test_bundle_identifier_is_the_documented_long_term_identity():
    spec = (ROOT / "release" / "mysql-dba-validator-macos.spec").read_text(encoding="utf-8")
    identifier = "io.github.realspinn.mysql-dba-validator"
    assert f'bundle_identifier="{identifier}"' in spec
    assert identifier in (ROOT / "RELEASING.md").read_text(encoding="utf-8")


def test_macos_readme_makes_no_unproven_claims():
    text = (ROOT / "release" / "README-MACOS.txt").read_text(encoding="utf-8")
    assert "just opens the page" not in text and "browse to http://127.0.0.1:8420" in text
    assert "11 or later" not in text and "not yet been independently" in text
    assert "The app is signed with a Developer ID" not in text


# ----------------------------------------------------------------------------- skipped-test guard

from release import check_pytest_skips  # noqa: E402

MACOS_SKIPS = check_pytest_skips.EXPECTED_SKIPS["macos"]


def _junit_report(tmp_path: Path) -> Path:
    """A real pytest JUnit report: tests/test_sample.py with one passing and two skipping tests."""
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    (project / "tests" / "test_sample.py").write_text(textwrap.dedent("""
        import pytest
        def test_runs(): pass
        def test_windows_only(): pytest.skip("not here")
        @pytest.mark.parametrize("x", [1])
        def test_chrome(x): pytest.skip("no chrome")
    """), encoding="utf-8")
    report = tmp_path / "report.xml"
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={report}",
                    "tests"], cwd=project, check=True, capture_output=True)
    return report


def test_skip_guard_reads_real_pytest_reports_and_fails_on_unexpected_skips(tmp_path, monkeypatch):
    report = _junit_report(tmp_path)
    total, skipped = check_pytest_skips.skipped_tests(report)
    assert total == 3 and sorted(skipped) == ["tests.test_sample::test_chrome[1]",
                                              "tests.test_sample::test_windows_only"]
    monkeypatch.setitem(check_pytest_skips.EXPECTED_SKIPS, "macos",
                        {"tests.test_sample::test_windows_only": "documented reason"})
    assert check_pytest_skips.main([str(report), "--platform", "macos"]) == 1  # test_chrome[1] was not expected
    monkeypatch.setitem(check_pytest_skips.EXPECTED_SKIPS, "macos",
                        {"tests.test_sample::test_windows_only": "r", "tests.test_sample::test_chrome": "r"})
    assert check_pytest_skips.main([str(report), "--platform", "macos"]) == 0


def test_skip_guard_fails_without_a_report_or_without_tests(tmp_path):
    assert check_pytest_skips.main([str(tmp_path / "missing.xml"), "--platform", "macos"]) == 1
    empty = tmp_path / "empty.xml"
    empty.write_text('<testsuites><testsuite name="pytest" tests="0"/></testsuites>', encoding="utf-8")
    assert check_pytest_skips.main([str(empty), "--platform", "macos"]) == 1


def test_only_the_windows_job_object_test_may_skip_on_macos():
    assert list(MACOS_SKIPS) == [
        "tests.test_release_packaging::test_job_object_kills_child_when_launcher_handle_closes"]
    # That test exists and really is Windows-only.
    source = (ROOT / "tests" / "test_release_packaging.py").read_text(encoding="utf-8")
    assert re.search(r'@pytest\.mark\.skipif\(os\.name != "nt", reason="Windows Job Object"\)\s+'
                     r"def test_job_object_kills_child_when_launcher_handle_closes\(", source)


# ----------------------------------------------------------------------------- release workflow

def _workflow():
    return yaml.safe_load((ROOT / ".github" / "workflows" / "release-macos.yml").read_text(encoding="utf-8"))


def _runs(job) -> str:
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_build_test_job_needs_no_secrets_and_never_signs():
    workflow = _workflow()
    build = workflow["jobs"]["build-test"]
    assert workflow["permissions"] == {"contents": "read"} and build["permissions"] == {"contents": "read"}
    assert build["runs-on"] == "macos-15" and "environment" not in build and "if" not in build
    dumped = yaml.safe_dump(build)
    assert "secrets." not in dumped and "vars." not in dumped
    runs = _runs(build)
    for forbidden in ("build_macos.py sign", "notarize", "staple", "build_macos.py dmg", "codesign", "security "):
        assert forbidden not in runs, forbidden
    for required in ("pytest", "check_pytest_skips.py", "--platform macos", "build_macos.py build",
                     "build_macos.py package-unsigned", "audit_macos.py", "smoke_test_macos.py"):
        assert required in runs, required
    order = ["check_pytest_skips.py", "build_macos.py build", "package-unsigned", "audit_macos.py",
             "smoke_test_macos.py"]
    assert [runs.index(item) for item in order] == sorted(runs.index(item) for item in order)
    upload = [s for s in build["steps"] if s.get("uses", "").startswith("actions/upload-artifact")][0]
    assert "unsigned" in upload["with"]["name"] and "github.sha" in upload["with"]["name"]
    assert "unsigned.zip" in upload["with"]["path"] and ".dmg" not in upload["with"]["path"]
    assert upload["with"]["if-no-files-found"] == "error"


def test_signed_path_runs_only_for_a_tag_with_explicit_opt_in():
    jobs = _workflow()["jobs"]
    sign = jobs["sign-notarize"]
    assert sign["needs"] == "build-test"
    assert sign["if"] == "startsWith(github.ref, 'refs/tags/v') && vars.MACOS_SIGNING_ENABLED == 'true'"
    assert jobs["verify-signed"]["needs"] == "sign-notarize"  # skipped whenever signing is skipped
    assert jobs["publish"]["needs"] == "verify-signed"
    assert jobs["publish"]["if"] == "startsWith(github.ref, 'refs/tags/v')"


def test_only_the_signing_job_sees_secrets_and_it_runs_no_third_party_code():
    jobs = _workflow()["jobs"]
    for name, job in jobs.items():
        if name != "sign-notarize":
            assert "secrets." not in yaml.safe_dump(job), name
    sign = jobs["sign-notarize"]
    assert sign["environment"] == "macos-release" and sign["permissions"] == {"contents": "read"}
    assert "env" not in sign  # secrets go to individual steps, never the whole job
    runs = _runs(sign)
    for forbidden in ("pip ", "pytest", "smoke_test", "audit_macos", "venv", "Console"):
        assert forbidden not in runs, forbidden
    python_lines = [line.strip() for line in runs.splitlines() if line.strip().startswith("python")]
    assert python_lines and all(line.startswith("python -I release/build_macos.py ") for line in python_lines)
    for step in sign["steps"]:
        if "secrets." in yaml.safe_dump(step):
            assert "secrets." not in step.get("run", "")  # given through env, never pasted into a script


def test_secret_bearing_and_downstream_jobs_pin_actions_to_commit_shas():
    jobs = _workflow()["jobs"]
    for name in ("sign-notarize", "verify-signed", "publish"):
        for step in jobs[name]["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), (name, step["uses"])
    for name in ("build-test", "sign-notarize", "verify-signed"):
        for step in jobs[name]["steps"]:
            if step.get("uses", "").startswith("actions/checkout"):
                assert step["with"]["persist-credentials"] is False, name


def test_signing_material_is_removed_right_after_its_last_use():
    steps = _workflow()["jobs"]["sign-notarize"]["steps"]
    names = [s.get("name", "") for s in steps]
    cleanup = names.index("Remove signing material")
    assert steps[cleanup]["if"] == "always()"
    assert all(item in steps[cleanup]["run"] for item in ("delete-keychain", ".p8", ".p12"))
    last_secret = max(i for i, s in enumerate(steps) if "secrets." in yaml.safe_dump(s))
    assert cleanup == last_secret + 1
    later = steps[cleanup + 1:]
    leak_check = names.index("Nothing but the release files leaves this job")
    upload = next(i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/upload-artifact"))
    assert cleanup < leak_check < upload
    assert all("secrets." not in yaml.safe_dump(s) for s in later)


def test_macos_workflow_never_creates_or_overwrites_a_release():
    text = (ROOT / ".github" / "workflows" / "release-macos.yml").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "gh release create" not in body and "--clobber" not in body
    jobs = _workflow()["jobs"]
    publish = jobs["publish"]
    assert publish["permissions"] == {"contents": "write"} and "environment" not in publish
    assert "isDraft" in _runs(publish) and "gh release upload" in _runs(publish)
    for name, job in jobs.items():
        if name != "publish":
            assert job["permissions"] == {"contents": "read"}, name


def test_windows_workflow_is_still_the_only_release_creator():
    windows = (ROOT / ".github" / "workflows" / "release-windows.yml").read_text(encoding="utf-8")
    assert "gh release create" in windows and "--draft" in windows
