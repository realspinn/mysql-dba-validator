"""Tests for the portable-release launcher and the artifact audit (release/)."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import dotenv
import pytest
from fastapi.testclient import TestClient

from backend.version import APP_VERSION
from connector import launcher as connector_launcher
from release import audit_artifact, build_windows
from release import launcher


def _args(*argv: str):
    return launcher._argument_parser().parse_args(list(argv))


# ----------------------------------------------------------------------------- connector arguments

def test_connector_argv_defaults_allow_only_this_machines_page():
    assert launcher.connector_argv(_args()) == [
        "--allow-origin", "http://127.0.0.1:8420", "--allow-origin", "http://localhost:8420"]


def test_connector_argv_forwards_connector_options_without_duplicates():
    argv = launcher.connector_argv(_args(
        "--tls-ca", r"C:\ca.pem", "--registry", r"D:\reg.json", "--session-ttl", "600",
        "--allow-origin", "https://validator.example", "--allow-origin", "http://127.0.0.1:8420"))
    assert argv == [
        "--allow-origin", "http://127.0.0.1:8420", "--allow-origin", "http://localhost:8420",
        "--allow-origin", "https://validator.example",
        "--tls-ca", r"C:\ca.pem", "--registry", r"D:\reg.json", "--session-ttl", "600"]


def test_connector_accepts_launcher_arguments_and_keeps_its_validation(tmp_path):
    argv = launcher.connector_argv(_args("--registry", str(tmp_path / "targets.json")))
    config = connector_launcher.build_config(argv, environ={})
    assert config.allowed_origins == ("http://127.0.0.1:8420", "http://localhost:8420")
    assert config.registry_path == (tmp_path / "targets.json").absolute()
    assert config.tls_ca_path is None
    # The connector's own checks still apply to forwarded values.
    with pytest.raises(SystemExit):
        connector_launcher.build_config(
            launcher.connector_argv(_args("--tls-ca", str(tmp_path / "missing.pem"))), environ={})
    with pytest.raises(SystemExit):
        connector_launcher.build_config(
            launcher.connector_argv(_args("--allow-origin", "http://evil.example")), environ={})


def test_backend_flag_is_hidden_from_help():
    assert launcher.RUN_BACKEND_FLAG not in launcher._argument_parser().format_help()


# ----------------------------------------------------------------------------- data location

def test_data_dir_is_per_user_and_matches_registry_default(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path)}
    assert launcher.data_dir(env) == tmp_path / "MySQLDBAValidator"
    from connector.registry_store import default_registry_path
    assert default_registry_path(env).parent == launcher.data_dir(env)


def test_registry_override_does_not_move_app_data(tmp_path):
    env = {"LOCALAPPDATA": str(tmp_path), "CONNECTOR_TARGET_REGISTRY": str(tmp_path / "elsewhere" / "r.json")}
    assert launcher.data_dir(env) == tmp_path / "MySQLDBAValidator"


# ----------------------------------------------------------------------------- .env isolation

@pytest.fixture
def restore_dotenv(monkeypatch):
    monkeypatch.setattr(dotenv, "load_dotenv", dotenv.load_dotenv)
    for key in ("MDV_TEST_A", "MDV_TEST_B", "MDV_TEST_CWD"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_isolate_dotenv_loads_only_the_data_dir_file(tmp_path, restore_dotenv):
    data_env = tmp_path / "data" / ".env"
    data_env.parent.mkdir()
    data_env.write_text("MDV_TEST_A=from-data-dir\nMDV_TEST_B=file\n", encoding="utf-8")
    restore_dotenv.setenv("MDV_TEST_B", "preexisting")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".env").write_text("MDV_TEST_CWD=leaked\n", encoding="utf-8")
    restore_dotenv.chdir(cwd)

    assert launcher.isolate_dotenv(data_env) is True
    assert os.environ["MDV_TEST_A"] == "from-data-dir"
    assert os.environ["MDV_TEST_B"] == "preexisting"  # never overrides the environment
    # What backend.main's bare load_dotenv() now does: nothing.
    assert dotenv.load_dotenv() is False
    assert "MDV_TEST_CWD" not in os.environ


def test_isolate_dotenv_without_file_loads_nothing(tmp_path, restore_dotenv):
    (tmp_path / ".env").write_text("MDV_TEST_CWD=leaked\n", encoding="utf-8")
    restore_dotenv.chdir(tmp_path)
    assert launcher.isolate_dotenv(tmp_path / "data" / ".env") is False
    dotenv.load_dotenv()
    assert "MDV_TEST_CWD" not in os.environ


# ----------------------------------------------------------------------------- process helpers

def test_port_in_use_detects_a_bound_loopback_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert launcher.port_in_use(port) is True
    assert launcher.port_in_use(port) is False


def test_backend_command_from_source_runs_this_module():
    command, env = launcher.backend_command()
    assert command[1:] == ["-m", "release.launcher", launcher.RUN_BACKEND_FLAG]
    assert str(launcher.PROJECT_ROOT) in env["PYTHONPATH"].split(os.pathsep)


def test_backend_command_enables_idle_shutdown_only_when_asked(monkeypatch):
    monkeypatch.setenv(launcher.IDLE_SHUTDOWN_ENV, "5")  # inherited value is never passed on
    _command, env = launcher.backend_command()
    assert launcher.IDLE_SHUTDOWN_ENV not in env
    _command, env = launcher.backend_command(900)
    assert env[launcher.IDLE_SHUTDOWN_ENV] == "900"


def test_frozen_backend_always_runs_from_the_console_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MySQL-DBA-Validator.exe"))
    command, _env = launcher.backend_command(900)
    assert command == [str(tmp_path / launcher.CONSOLE_EXE_NAME), launcher.RUN_BACKEND_FLAG]


@pytest.mark.parametrize("value, expected", [("900", 900), ("20", 20), ("0", None), ("-5", None),
                                             ("", None), ("abc", None)])
def test_idle_shutdown_seconds_parsing(value, expected):
    assert launcher.idle_shutdown_seconds({launcher.IDLE_SHUTDOWN_ENV: value}) == expected
    assert launcher.idle_shutdown_seconds({}) is None


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def run_watch(clock, monitor, timeout, steps):
    """Drive watch_idle with a fake clock: each sleep advances it by the next step."""
    stopped = []
    plan = iter(steps)

    def sleep(_tick):
        try:
            step = next(plan)
        except StopIteration:
            raise AssertionError("watch_idle did not stop") from None
        if callable(step):
            step()
        else:
            clock.now += step

    launcher.watch_idle(monitor, lambda: stopped.append(clock.now), timeout, tick=5.0, clock=clock, sleep=sleep)
    return stopped


def test_idle_shutdown_after_timeout_without_heartbeats():
    clock = FakeClock()
    monitor = launcher.IdleMonitor(clock)
    stopped = run_watch(clock, monitor, timeout=900, steps=[5.0] * 200)
    assert stopped == [1900.0]


def test_heartbeats_keep_the_app_alive():
    clock = FakeClock()
    monitor = launcher.IdleMonitor(clock)
    # A heartbeat every 60 s for an hour, then none.
    steps = []
    for _ in range(60):
        steps += [5.0] * 12 + [monitor.touch]
    steps += [5.0] * 200
    stopped = run_watch(clock, monitor, timeout=900, steps=steps)
    assert stopped and stopped[0] >= 1000 + 3600 + 900


def test_throttled_background_tab_does_not_cause_premature_shutdown():
    # Chrome's intensive throttling: timers in a hidden tab run about once a minute,
    # with occasional longer gaps. Heartbeats up to 5 minutes apart stay well inside 15.
    clock = FakeClock()
    monitor = launcher.IdleMonitor(clock)
    steps = []
    for gap in (60, 120, 60, 300, 60, 180, 300, 60) * 3:
        steps += [5.0] * (gap // 5) + [monitor.touch]
    total = sum((60, 120, 60, 300, 60, 180, 300, 60)) * 3
    steps += [5.0] * 200
    stopped = run_watch(clock, monitor, timeout=900, steps=steps)
    assert stopped[0] >= 1000 + total + 900


def test_computer_sleep_is_not_counted_as_idle_time():
    clock = FakeClock()
    monitor = launcher.IdleMonitor(clock)
    # Two hours asleep between two checks: on resume the page has time to send a heartbeat.
    steps = [5.0, 7200.0] + [5.0] * 200
    stopped = run_watch(clock, monitor, timeout=900, steps=steps)
    assert stopped == [1000 + 5 + 7200 + 900]


def test_console_mode_is_the_default_and_windowed_mode_comes_from_the_build_marker(monkeypatch):
    monkeypatch.setattr(sys, "_xoptions", {})
    assert launcher.is_windowed() is False
    monkeypatch.setattr(sys, "_xoptions", {launcher.WINDOWED_XOPTION: True})
    assert launcher.is_windowed() is True
    seen = []
    monkeypatch.setattr(launcher, "run_windowed", lambda argv: seen.append(argv) or 0)
    assert launcher.main(["--no-browser"]) == 0
    assert seen == [["--no-browser"]]


@pytest.fixture
def windowed(monkeypatch, tmp_path):
    """The windowed role with message boxes, browser and ports replaced by recorders."""
    calls = {"errors": [], "browser": [], "popen": []}
    monkeypatch.setattr(launcher, "show_error", lambda message: calls["errors"].append(message))
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: calls["browser"].append(url))
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(launcher.subprocess, "Popen",
                        lambda *args, **kwargs: calls["popen"].append(args) or pytest.fail("no child expected"))
    return calls


def test_windowed_rejects_connector_options_with_a_visible_message(windowed):
    assert launcher.run_windowed(["--tls-ca", "ca.pem"]) == 2
    assert len(windowed["errors"]) == 1 and "Console" in windowed["errors"][0]
    assert windowed["popen"] == []


def test_windowed_reuses_a_running_validator(windowed, monkeypatch):
    monkeypatch.setattr(launcher, "port_in_use", lambda port: port == launcher.BACKEND_PORT)
    monkeypatch.setattr(launcher, "validator_already_running", lambda: True)
    assert launcher.run_windowed([]) == 0
    assert windowed["browser"] == [launcher.PAGE_URL]
    assert windowed["errors"] == [] and windowed["popen"] == []


def test_windowed_port_taken_by_another_program_is_a_visible_error(windowed, monkeypatch):
    monkeypatch.setattr(launcher, "port_in_use", lambda port: port == launcher.BACKEND_PORT)
    monkeypatch.setattr(launcher, "validator_already_running", lambda: False)
    assert launcher.run_windowed([]) == 3
    assert len(windowed["errors"]) == 1 and "already in use" in windowed["errors"][0]
    assert windowed["browser"] == [] and windowed["popen"] == []


def test_windowed_missing_console_executable_is_a_visible_error(windowed, monkeypatch, tmp_path):
    monkeypatch.setattr(launcher, "port_in_use", lambda port: False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "MySQL-DBA-Validator.exe"))
    assert launcher.run_windowed([]) == 4
    assert len(windowed["errors"]) == 1 and launcher.CONSOLE_EXE_NAME in windowed["errors"][0]


def test_windowed_unexpected_failure_is_shown_not_swallowed(windowed, monkeypatch):
    def boom(_argv):
        raise RuntimeError("disk full")

    monkeypatch.setattr(launcher, "run_windowed", boom)
    assert launcher.main_windowed([]) == 1
    assert windowed["errors"] == [f"{launcher.APP_TITLE} could not start: RuntimeError: disk full"]


def test_windowed_backend_failure_and_unexpected_exit_are_visible(monkeypatch, tmp_path):
    errors, browser = [], []
    monkeypatch.setattr(launcher, "show_error", errors.append)
    monkeypatch.setattr(launcher.webbrowser, "open", browser.append)
    monkeypatch.setattr(launcher, "data_dir", lambda: tmp_path / "data")
    monkeypatch.setattr(launcher, "port_in_use", lambda port: False)
    monkeypatch.setattr(launcher, "kill_with_parent", lambda child: None)

    class Child:
        def __init__(self, code):
            self.code = code

        def poll(self):
            return self.code

        def wait(self, timeout=None):
            return self.code

    started = []

    def popen(command, **kwargs):
        started.append((command, kwargs))
        return Child(1)

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "wait_for_port", lambda port, child, timeout: False)
    assert launcher.run_windowed(["--no-browser"]) == 4
    assert "did not start" in errors[-1] and "backend.log" in errors[-1]
    command, kwargs = started[0]
    assert command[-1] == launcher.RUN_BACKEND_FLAG
    assert kwargs["env"][launcher.IDLE_SHUTDOWN_ENV] == str(launcher.DEFAULT_IDLE_SHUTDOWN_SECONDS)
    if os.name == "nt":
        assert kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW

    monkeypatch.setattr(launcher, "wait_for_port", lambda port, child, timeout: True)
    assert launcher.run_windowed([]) == 1
    assert "stopped unexpectedly" in errors[-1] and browser == [launcher.PAGE_URL]

    monkeypatch.setattr(launcher.subprocess, "Popen", lambda command, **kwargs: Child(0))
    errors.clear()
    assert launcher.run_windowed(["--no-browser"]) == 0  # idle shutdown: a clean exit, no message
    assert errors == []


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object")
def test_job_object_kills_child_when_launcher_handle_closes():
    import ctypes

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        job = launcher.kill_with_parent(child)
        assert job, "child could not be assigned to a kill-on-close job"
        assert child.poll() is None
        ctypes.WinDLL("kernel32").CloseHandle(ctypes.c_void_p(job))  # what process exit does
        for _ in range(50):
            if child.poll() is not None:
                break
            time.sleep(0.1)
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()


# ----------------------------------------------------------------------------- version

def test_each_executable_gets_its_own_version_resource(tmp_path):
    path = tmp_path / "v.txt"
    build_windows.version_resource("1.2.3", path, "MySQL-DBA-Validator Console.exe", "MySQL DBA Validator Console")
    text = path.read_text(encoding="utf-8")
    assert "StringStruct('OriginalFilename', 'MySQL-DBA-Validator Console.exe')" in text
    assert "StringStruct('InternalName', 'MySQL-DBA-Validator Console')" in text
    assert "StringStruct('FileDescription', 'MySQL DBA Validator Console')" in text
    build_windows.version_resource("1.2.3", path)
    assert "StringStruct('OriginalFilename', 'MySQL-DBA-Validator.exe')" in path.read_text(encoding="utf-8")


def test_spec_builds_a_windowed_app_and_a_console_app():
    spec = (Path(launcher.PROJECT_ROOT) / "release" / "mysql-dba-validator.spec").read_text(encoding="utf-8")
    assert f'("X {launcher.WINDOWED_XOPTION}", None, "OPTION")' in spec
    assert 'name="MySQL-DBA-Validator",' in spec and 'name="MySQL-DBA-Validator Console",' in spec
    assert spec.count("console=False") == 1 and spec.count("console=True") == 1
    assert launcher.CONSOLE_EXE_NAME == audit_artifact.CONSOLE_EXE_NAME == "MySQL-DBA-Validator Console.exe"


def test_single_version_source_used_by_api_and_build():
    from backend.main import app

    assert build_windows.read_version() == APP_VERSION
    assert TestClient(app).get("/api/health").json()["version"] == APP_VERSION
    assert app.version == APP_VERSION


# ----------------------------------------------------------------------------- artifact audit

def test_audit_reads_only_secret_valued_env_keys(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("MYSQL_HOST=127.0.0.1\nMYSQL_USER=readonly\nMYSQL_PASSWORD=Sup3r-Secret\n"
                        "API_TOKEN=tok-123456\n# MYSQL_PASSWORD=commented\nEMPTY_SECRET=\n", encoding="utf-8")
    assert audit_artifact.read_env_secrets(env_file) == [
        (".env MYSQL_PASSWORD", "Sup3r-Secret"), (".env API_TOKEN", "tok-123456")]


def test_audit_flags_forbidden_files_secrets_and_missing_entry_points(tmp_path):
    app_dir = tmp_path / "MySQL-DBA-Validator"
    (app_dir / "_internal" / "frontend").mkdir(parents=True)
    (app_dir / "_internal" / "frontend" / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    (app_dir / ".env").write_text("X=1", encoding="utf-8")
    (app_dir / "connector-targets.json").write_text("{}", encoding="utf-8")
    (app_dir / "company-ca.pem").write_text("cert", encoding="utf-8")
    (app_dir / "notes.txt").write_text("password is Pa55-Word-77 ok", encoding="utf-8")
    (app_dir / "key.txt").write_text("-----BEGIN RSA PRIVATE KEY-----\n" + "QUJD" * 40 + "\n", encoding="utf-8")
    # The bare header as a library parsing constant is not key material.
    (app_dir / "lib_constant.txt").write_text('_SK_START = b"-----BEGIN OPENSSH PRIVATE KEY-----"', encoding="utf-8")

    findings, _stats = audit_artifact.audit(app_dir, tmp_path / "no.env", ["Pa55-Word-77"])
    text = "\n".join(findings)
    assert "forbidden file (.env file): .env" in text
    assert "connector target registry" in text
    assert "key/certificate file" in text
    assert "--forbid #1 found in notes.txt" in text
    assert "Pa55-Word-77" not in text  # values are never printed
    assert "PEM private key block in key.txt" in text
    assert "lib_constant.txt" not in text
    assert f"required file missing: {audit_artifact.EXE_NAME}" in text
    assert f"required file missing: {audit_artifact.CONSOLE_EXE_NAME}" in text


def test_audit_passes_clean_folder_and_ignores_variable_names(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "_internal" / "frontend").mkdir(parents=True)
    (app_dir / "_internal" / "frontend" / "index.html").write_text(
        '<input id="password" type="password"> token secret key', encoding="utf-8")
    for name in audit_artifact.EXE_NAMES:
        (app_dir / name).write_bytes(b"")
    original = audit_artifact.embedded_entries
    audit_artifact.embedded_entries = lambda _data: iter(())
    try:
        findings, stats = audit_artifact.audit(app_dir, tmp_path / "no.env", [])
    finally:
        audit_artifact.embedded_entries = original
    assert findings == []
    assert stats["files"] == 3


def test_audit_allows_only_upstream_cargo_registry_paths_in_compiled_binaries(tmp_path, monkeypatch):
    home = r"C:\Users\runneradmin"
    checkout = r"D:\a\repo\repo"
    monkeypatch.setattr(audit_artifact, "machine_paths", lambda: [
        (audit_artifact.HOME_LABEL, home), ("source checkout path", checkout)])
    monkeypatch.setattr(audit_artifact, "embedded_entries", lambda _data: iter(()))
    app_dir = tmp_path / "app"
    internal = app_dir / "_internal"
    (internal / "frontend").mkdir(parents=True)
    (internal / "frontend" / "index.html").write_text("<html>ok</html>", encoding="utf-8")
    for name in audit_artifact.EXE_NAMES:
        (app_dir / name).write_bytes(b"")
    cargo = rb"\.cargo\registry\src\index.crates.io-1949cf8c6b5b557f\pyo3-0.25.1\src\err.rs"
    # Allowed: upstream Rust wheel build paths (both separator styles) inside .pyd/.dll.
    (internal / "_rust.pyd").write_bytes(b"\x00MZ" + home.encode() + cargo + b"\x00")
    (internal / "_rust_notify.dll").write_bytes(home.encode() + b"/.cargo\\registry\\src\\x.rs")
    # Rejected: a genuine home path in a binary, even next to an allowed one.
    (internal / "mixed.pyd").write_bytes(home.encode() + cargo + b"\x00" + home.encode() + rb"\Documents\x")
    # Rejected: the same cargo path outside a compiled binary.
    (internal / "notes.txt").write_bytes(home.encode() + cargo)
    # Rejected: UTF-16 occurrences stay strict.
    (internal / "wide.dll").write_bytes((home + r"\.cargo\registry\x").encode("utf-16-le"))
    # Rejected: the exception never covers the checkout path.
    (internal / "checkout.pyd").write_bytes(checkout.encode() + cargo)
    (internal / "leak.pyd").write_bytes(home.encode() + rb"\AppData\Local\x")

    findings, _stats = audit_artifact.audit(app_dir, tmp_path / "no.env", [])
    text = "\n".join(findings)
    assert "_rust.pyd" not in text
    assert "_rust_notify.dll" not in text
    assert "build user's home folder found in _internal/mixed.pyd" in text
    assert "build user's home folder found in _internal/notes.txt" in text
    assert "build user's home folder found in _internal/wide.dll" in text
    assert "source checkout path found in _internal/checkout.pyd" in text
    assert "build user's home folder found in _internal/leak.pyd" in text
    assert len(findings) == 5
