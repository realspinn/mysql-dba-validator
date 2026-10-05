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


def test_audit_passes_clean_folder_and_ignores_variable_names(tmp_path):
    app_dir = tmp_path / "app"
    (app_dir / "_internal" / "frontend").mkdir(parents=True)
    (app_dir / "_internal" / "frontend" / "index.html").write_text(
        '<input id="password" type="password"> token secret key', encoding="utf-8")
    (app_dir / audit_artifact.EXE_NAME).write_bytes(b"")
    original = audit_artifact.embedded_entries
    audit_artifact.embedded_entries = lambda _data: iter(())
    try:
        findings, stats = audit_artifact.audit(app_dir, tmp_path / "no.env", [])
    finally:
        audit_artifact.embedded_entries = original
    assert findings == []
    assert stats["files"] == 2
