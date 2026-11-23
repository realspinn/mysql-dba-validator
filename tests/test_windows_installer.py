"""The Windows installer (release/installer, release/build_installer.py) and how the
application behaves once installed.

Source and unit checks only. That the installer installs, upgrades and uninstalls is tested
by release/smoke_test_installer.py, which runs on a Windows runner (package-check.yml and
release-windows.yml) and needs Inno Setup.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import backend.update_check as update_check
from release import audit_artifact, build_installer
from backend.version import APP_VERSION
from tests.test_update_download import NAME, OWN_PAGE, VERSION, api_asset, client

ROOT = Path(__file__).resolve().parent.parent
ISS_FULL = (ROOT / "release" / "installer" / "mysql-dba-validator.iss").read_text(encoding="utf-8")
# Without comment lines: what Setup actually does.
ISS = "\n".join(line for line in ISS_FULL.splitlines() if not line.lstrip().startswith((";", "//")))
# The application's installer identity. Changing it makes Setup treat every earlier
# installation as a different program (no upgrade in place), so a change must be deliberate.
APP_GUID = "6A922AD5-1E15-4205-B269-916B86375374"


# ------------------------------------------------------------------ the script

def _sections() -> dict[str, str]:
    parts = re.split(r"^\[(\w+)\]\s*$", ISS, flags=re.M)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_the_application_id_is_one_fixed_value():
    assert re.findall(r'#define AppGuid "([0-9A-F-]{36})"', ISS) == [APP_GUID]
    assert "AppId={{{#AppGuid}}" in ISS
    assert "AUTO_GUID" not in ISS.upper()


def test_it_installs_for_the_current_user_only_and_for_x64():
    setup = _sections()["Setup"]
    assert "PrivilegesRequired=lowest\n" in setup
    assert "PrivilegesRequiredOverridesAllowed" not in setup
    assert "ArchitecturesAllowed=x64compatible" in setup and "ArchitecturesInstallIn64BitMode=x64compatible" in setup
    assert "DefaultDirName={autopf}\\{#AppName}" in setup and '#define AppName "MySQL DBA Validator"' in ISS
    for machine_wide in ("{commonappdata}", "{commonpf", "{commonpf64}", "{sys}", "{win}", "HKLM"):
        assert machine_wide not in ISS, machine_wide


def test_it_adds_no_service_task_startup_path_firewall_or_registry_entry():
    sections = _sections()
    assert set(sections) == {"Setup", "Files", "Tasks", "Icons", "Run", "Code"}
    for forbidden in ("[Registry]", "[UninstallDelete]", "[InstallDelete]", "[Dirs]", "schtasks", "netsh", "sc.exe",
                      "ChangesEnvironment=yes", "{userstartup}", "{commonstartup}", "{startup}", "RunOnce"):
        assert forbidden not in ISS, forbidden
    run = sections["Run"].strip().splitlines()
    assert len(run) == 1 and "postinstall" in run[0] and "skipifsilent" in run[0] and "nowait" in run[0]


def test_it_never_touches_the_data_folder():
    assert "MySQLDBAValidator" not in ISS
    assert "{localappdata}" not in ISS


def test_running_applications_are_not_closed_or_restarted_by_setup():
    setup = _sections()["Setup"]
    assert "CloseApplications=no\n" in setup and "RestartApplications=no\n" in setup
    code = _sections()["Code"]
    assert "function PrepareToInstall" in code and "function InitializeUninstall" in code
    assert "ExecutablePath" in code and "Win32_Process" in code
    for forbidden in ("Terminate", "taskkill", "Exec(", "ShellExec"):
        assert forbidden not in code, forbidden


def test_a_failed_process_check_is_a_refusal_never_a_go_ahead():
    code = _sections()["Code"]
    check = code[code.index("function CheckValidatorRunning"):code.index("function RunningHelp")]
    # The question can fail (an exception), and the function then says so: False, not "not running".
    assert re.search(r"except\s+Result := False;\s+end;", check)
    assert check.count("Result := True;") == 1 and check.index("Result := True;") > check.index("Found.Count")
    reason = code[code.index("function StopReason"):code.index("function PrepareToInstall")]
    # An unanswered question is handled first and gives a stop reason, like a running app.
    assert reason.index("if not CheckValidatorRunning") < reason.index("else if Running then")
    assert reason.count("Result := ") == 3 and "could not check whether" in reason and "Nothing has been changed" in reason
    # Both Setup and the uninstaller stop on any reason; neither calls the check directly.
    assert "Result := StopReason(" in code[code.index("function PrepareToInstall"):code.index("function InitializeUninstall")]
    uninstall = code[code.index("function InitializeUninstall"):]
    assert "StopReason(" in uninstall and "Result := False" in uninstall and "CheckValidatorRunning" not in uninstall


def test_the_process_question_is_only_changed_by_the_installer_test():
    assert '#define ProcessNamespace "root\\CIMV2"' in ISS_FULL
    assert "ConnectServer('.', '{#ProcessNamespace}')" in ISS
    scripts = {path: path.read_text(encoding="utf-8") for path in [*(ROOT / "release").glob("*.py"), *(ROOT / ".github" / "workflows").glob("*.yml")]}
    using = sorted(path.name for path, text in scripts.items() if "ProcessNamespace" in text)
    assert using == ["smoke_test_installer.py"]


def test_it_installs_the_built_folder_and_the_marker_and_has_no_version_of_its_own():
    files = _sections()["Files"]
    assert 'Source: "{#PayloadDir}\\*"; DestDir: "{app}"' in files
    assert 'Source: "installed-by-setup.txt"; DestDir: "{app}"' in files
    assert re.search(r'AppVersion=\{#AppVersion\}', ISS) and not re.search(r'#define AppVersion', ISS)
    assert "{#AppVersion}" in _sections()["Setup"]


def test_shortcuts_point_at_both_executables_and_the_desktop_one_is_optional():
    assert 'Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked' in _sections()["Tasks"]
    icons = _sections()["Icons"]
    assert 'Filename: "{app}\\{#AppExe}"' in icons and 'Filename: "{app}\\{#ConsoleExe}"' in icons
    assert '#define AppExe "MySQL-DBA-Validator.exe"' in ISS and '#define ConsoleExe "MySQL-DBA-Validator Console.exe"' in ISS
    assert audit_artifact.EXE_NAME == "MySQL-DBA-Validator.exe" and audit_artifact.CONSOLE_EXE_NAME == "MySQL-DBA-Validator Console.exe"
    desktop = [line for line in icons.splitlines() if "{autodesktop}" in line]
    assert len(desktop) == 1 and "Tasks: desktopicon" in desktop[0]


def test_an_older_installer_does_not_replace_a_newer_installation():
    code = _sections()["Code"]
    assert "function InitializeSetup" in code and "ComparePackedVersion(NewVersion, OldVersion) < 0" in code
    assert "Result := False" in code


# ------------------------------------------------------------------ the build script

def test_the_setup_file_is_named_from_the_one_version_source():
    assert build_installer.setup_name(APP_VERSION) == f"MySQL-DBA-Validator-v{APP_VERSION}-windows-x64-setup.exe"
    assert "AppVersion" not in re.sub(r"\{#AppVersion\}|#ifndef AppVersion|AppVersionInfo|/DAppVersion", "",
                                      "\n".join(l for l in ISS.splitlines() if "#define" in l))


@pytest.mark.parametrize("version, expected", [("0.11.0", "0.11.0.0"), ("1.2.3-rc1", "1.2.3.0"), ("2.0.0+build5", "2.0.0.0")])
def test_the_version_resource_number(version, expected):
    assert build_installer.numeric_version(version) == expected


def _fake_compiler(monkeypatch, tmp_path, banner: str, write: bool = True):
    def fake_run(command, **kwargs):
        if write:
            (tmp_path / "out" / build_installer.setup_name("9.9.9")).write_bytes(b"MZ")
        return subprocess.CompletedProcess(command, 0, stdout=banner, stderr="")

    monkeypatch.setattr(build_installer.subprocess, "run", fake_run)


def test_only_the_expected_compiler_series_is_accepted(monkeypatch, tmp_path):
    (tmp_path / "out").mkdir()
    _fake_compiler(monkeypatch, tmp_path, "Compiler engine version: Inno Setup 6.7.1\n")
    setup, engine = build_installer.compile_setup(Path("ISCC.exe"), tmp_path, tmp_path / "out", "9.9.9")
    assert engine == "6.7.1" and setup.is_file()


def _fake_signature_check(monkeypatch, tmp_path, code: int, out: str = "signature    : ISCC.exe -> Valid\n"):
    for name in build_installer.COMPILER_FILES:
        (tmp_path / name).write_bytes(b"MZ" + bytes(200))
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        return subprocess.CompletedProcess(command, code, stdout=out, stderr="")

    monkeypatch.setattr(build_installer.subprocess, "run", fake_run)
    return seen


def test_the_build_requires_the_compiler_files_to_verify_as_signed_by_the_publisher(monkeypatch, tmp_path):
    seen = _fake_signature_check(monkeypatch, tmp_path, 0)
    build_installer.record_compiler(tmp_path / "ISCC.exe")
    command = seen[0]
    assert command[command.index("-Publisher") + 1] == "Pyrsys B.V."
    assert command[command.index("-Names") + 1] == "ISCC.exe,ISCmplr.dll,ISPP.dll"
    assert "Setup.e32" not in command[command.index("-Names") + 1]  # the unsigned stub copied into every setup
    # The script itself demands a Valid status and the publisher in the certificate subject.
    assert "$s.Status -eq 'Valid'" in build_installer.COMPILER_REPORT and "Test-Publisher $subject $Publisher" in build_installer.COMPILER_REPORT
    assert "exit $bad" in build_installer.COMPILER_REPORT


def test_a_compiler_that_does_not_verify_stops_the_build(monkeypatch, tmp_path):
    _fake_signature_check(monkeypatch, tmp_path, 1, "signature    : ISCC.exe -> HashMismatch   <-- NOT ACCEPTED\n")
    with pytest.raises(SystemExit) as stop:
        build_installer.record_compiler(tmp_path / "ISCC.exe")
    assert "Pyrsys B.V." in str(stop.value)


@pytest.mark.parametrize("size, expected", [(0, False), (40, True)])
def test_the_signature_block_reader(tmp_path, size, expected):
    pe = bytearray(1024)
    pe[0x3C:0x40] = (128).to_bytes(4, "little")
    pe[128:132] = b"PE\0\0"
    pe[128 + 24:128 + 26] = (0x20B).to_bytes(2, "little")  # PE32+
    directories = 128 + 24 + 112
    pe[directories + 32:directories + 36] = (512 if size else 0).to_bytes(4, "little")
    pe[directories + 36:directories + 40] = size.to_bytes(4, "little")
    path = tmp_path / "x.exe"
    path.write_bytes(bytes(pe))
    assert build_installer.has_embedded_signature(path) is expected


def test_extra_defines_reach_the_compiler_and_the_release_build_passes_none(monkeypatch, tmp_path):
    (tmp_path / "out").mkdir()
    seen = []

    def fake_run(command, **kwargs):
        seen.append(command)
        (tmp_path / "out" / build_installer.setup_name("9.9.9")).write_bytes(b"MZ")
        return subprocess.CompletedProcess(command, 0, stdout="Compiler engine version: Inno Setup 6.7.1\n", stderr="")

    monkeypatch.setattr(build_installer.subprocess, "run", fake_run)
    build_installer.compile_setup(Path("ISCC.exe"), tmp_path, tmp_path / "out", "9.9.9", "/DSomething=1")
    assert seen[0][-2:] == ["/DSomething=1", str(build_installer.SCRIPT)]
    build_installer.compile_setup(Path("ISCC.exe"), tmp_path, tmp_path / "out", "9.9.9")
    assert seen[1][-1] == str(build_installer.SCRIPT) and not any(a.startswith("/D") and "Something" in a for a in seen[1])
    main_source = Path(build_installer.__file__).read_text(encoding="utf-8")
    assert "compile_setup(iscc, payload, DIST, version)" in main_source


@pytest.mark.parametrize("banner", ["Compiler engine version: Inno Setup 6.6.1\n", "Compiler engine version: Inno Setup 7.0.0\n",
                                    "Compiler engine version: Inno Setup 6.70.0\n", "no version here\n"])
def test_another_or_unreadable_compiler_version_fails_and_leaves_no_installer(monkeypatch, tmp_path, banner):
    (tmp_path / "out").mkdir()
    _fake_compiler(monkeypatch, tmp_path, banner)
    with pytest.raises(SystemExit):
        build_installer.compile_setup(Path("ISCC.exe"), tmp_path, tmp_path / "out", "9.9.9")
    assert not (tmp_path / "out" / build_installer.setup_name("9.9.9")).exists()


# ------------------------------------------------------------------ installed or portable

def _frozen(monkeypatch, folder: Path, marker: bool):
    folder.mkdir(parents=True, exist_ok=True)
    exe = folder / "MySQL-DBA-Validator Console.exe"
    exe.write_bytes(b"MZ")
    if marker:
        (folder / update_check.INSTALLER_MARKER).write_text("x", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(folder.parent / "appdata"))


def test_the_marker_name_is_the_one_the_installer_ships():
    shipped = ROOT / "release" / "installer" / update_check.INSTALLER_MARKER
    assert shipped.is_file() and f'Source: "{update_check.INSTALLER_MARKER}"' in ISS


def test_a_build_is_installed_only_when_the_marker_is_beside_its_executable(monkeypatch, tmp_path):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert update_check.installed_edition() is False  # from source
    _frozen(monkeypatch, tmp_path / "portable", marker=False)
    assert update_check.installed_edition() is False
    _frozen(monkeypatch, tmp_path / "installed", marker=True)
    assert update_check.installed_edition() is True


def test_the_working_directory_does_not_decide(monkeypatch, tmp_path):
    _frozen(monkeypatch, tmp_path / "portable", marker=False)
    other = tmp_path / "other"
    other.mkdir()
    (other / update_check.INSTALLER_MARKER).write_text("x", encoding="utf-8")
    monkeypatch.chdir(other)
    assert update_check.installed_edition() is False
    _frozen(monkeypatch, tmp_path / "installed", marker=True)
    monkeypatch.chdir(tmp_path)
    assert update_check.installed_edition() is True


def test_the_portable_build_still_downloads_in_the_app_and_the_installed_one_never_does(monkeypatch, tmp_path):
    _frozen(monkeypatch, tmp_path / "portable", marker=False)
    assert update_check.in_app_download_supported() is True
    _frozen(monkeypatch, tmp_path / "installed", marker=True)
    assert update_check.in_app_download_supported() is False


def _status(monkeypatch):
    release = {"tag_name": f"v{VERSION}", "html_url": update_check.RELEASE_PAGE + f"v{VERSION}",
               "draft": False, "prerelease": False, "assets": [api_asset()]}

    class Answer:
        status = 200

        def read(self, limit=-1):
            import json
            return json.dumps(release).encode()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(update_check, "_open", lambda request: Answer())
    monkeypatch.setattr(update_check, "_result", None)
    monkeypatch.setattr(update_check, "_asset", None)
    monkeypatch.delenv(update_check.OFF_SWITCH, raising=False)
    return client.post("/api/update-status", headers=OWN_PAGE).json()


def test_an_installed_build_is_sent_to_the_release_page_and_offered_no_zip(monkeypatch, tmp_path):
    _frozen(monkeypatch, tmp_path / "installed", marker=True)
    monkeypatch.setattr(update_check, "platform_build", lambda *a: update_check.WINDOWS)
    body = _status(monkeypatch)
    assert body["state"] == "update_available" and body["url"] == update_check.RELEASE_PAGE + f"v{VERSION}"
    assert "download" not in body and NAME not in str(body)
    assert body["installed"] is True  # the page then tells the user to download the setup file
    assert update_check.checked_asset() is None
    refused = client.post("/api/update-download", headers=OWN_PAGE)
    assert refused.status_code == 409 and refused.json() == {"error": "update_download_unavailable"}


def test_the_portable_build_is_offered_its_zip_as_before(monkeypatch, tmp_path):
    _frozen(monkeypatch, tmp_path / "portable", marker=False)
    monkeypatch.setattr(update_check, "platform_build", lambda *a: update_check.WINDOWS)
    body = _status(monkeypatch)
    assert body["download"]["name"] == NAME and body["download"]["in_app"] is True
    assert "installed" not in body
    assert update_check.checked_asset()["name"] == NAME


# ------------------------------------------------------------------ the portable build

def test_the_audit_refuses_a_portable_folder_that_holds_the_installer_marker(tmp_path):
    folder = tmp_path / "MySQL-DBA-Validator"
    folder.mkdir()
    (folder / update_check.INSTALLER_MARKER).write_text("x", encoding="utf-8")
    findings, _ = audit_artifact.audit(folder, tmp_path / "no.env", [])
    assert any("installer marker" in finding for finding in findings)


@pytest.mark.skipif(shutil.which("powershell") is None, reason="needs Windows PowerShell")
@pytest.mark.parametrize("subject, accepted", [
    ("CN=Pyrsys B.V., O=Pyrsys B.V., S=Noord-Holland, C=NL", True),
    ("O=Pyrsys B.V.", True),
    ("CN=Evil, O=Pyrsys B.V. Evil Ltd, C=GB", False),
    ('CN=Evil, OU="O=Pyrsys B.V.", O=Evil, C=GB', False),
    ("CN=Evil, O=Not Pyrsys B.V., C=GB", False),
    ("CN=Pyrsys B.V., O=Evil", False),
    ("", False),
])
def test_the_compiler_signer_must_be_exactly_the_publisher(tmp_path, subject, accepted):
    """Runs the script's own Test-Publisher: a subject that only contains the publisher's text is refused."""
    script = tmp_path / "check.ps1"
    script.write_text("param([string]$Subject, [string]$Publisher)\n" + build_installer.TEST_PUBLISHER
                      + "\nif (Test-Publisher $Subject $Publisher) { 'accepted' } else { 'refused' }\n", encoding="utf-8")
    out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script),
                          "-Subject", subject, "-Publisher", build_installer.COMPILER_PUBLISHER],
                         capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == ("accepted" if accepted else "refused"), out.stderr
