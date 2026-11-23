; Inno Setup script for the MySQL DBA Validator Windows installer (current user, no administrator).
; It packages the folder that release/build_windows.py already built and audited; it does not
; build the application. Compile it with release/build_installer.py, which passes:
;   /DAppVersion=<backend/version.py>  /DAppVersionInfo=<numeric x.y.z.0>  /DPayloadDir=<folder>  /DOutputDir=<folder>
; Compiler: Inno Setup 6.7 series (see RELEASING.md).

#ifndef AppVersion
  #error "Pass /DAppVersion=<version> (release/build_installer.py does)."
#endif
#ifndef AppVersionInfo
  #error "Pass /DAppVersionInfo=<x.y.z.0>."
#endif
#ifndef PayloadDir
  #error "Pass /DPayloadDir=<the audited PyInstaller folder>."
#endif
#ifndef OutputDir
  #define OutputDir "."
#endif
; The WMI namespace Setup asks which validator processes run. Only release/smoke_test_installer.py
; overrides it (with a namespace that does not exist) to make the question fail, and checks
; that Setup then refuses; release builds never do.
#ifndef ProcessNamespace
  #define ProcessNamespace "root\CIMV2"
#endif

; The application's identity for every installer release. NEVER change it: Setup decides
; whether it is upgrading an existing installation by this value (and it names the
; uninstall registry key). Generated once.
#define AppGuid "6A922AD5-1E15-4205-B269-916B86375374"
#define AppName "MySQL DBA Validator"
#define AppExe "MySQL-DBA-Validator.exe"
#define ConsoleExe "MySQL-DBA-Validator Console.exe"
#define RepoUrl "https://github.com/realspinn/mysql-dba-validator"

[Setup]
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Israel Adeleye
AppPublisherURL={#RepoUrl}
AppSupportURL={#RepoUrl}/issues
AppUpdatesURL={#RepoUrl}/releases
VersionInfoVersion={#AppVersionInfo}
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
; Current user only: no administrator prompt, installs under %LOCALAPPDATA%\Programs. The
; application's data stays where it already is, in %LOCALAPPDATA%\MySQLDBAValidator.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; x64 build; also runs on Arm64 Windows 11 through emulation (not tested there).
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename=MySQL-DBA-Validator-v{#AppVersion}-windows-x64-setup
SetupIconFile=..\mysql-dba-validator.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
LicenseFile=..\..\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Setup never closes or restarts the application itself: a running validator may hold a
; connector session. [Code] below stops with an explanation instead (no forced close).
CloseApplications=no
RestartApplications=no

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Tells the application it was installed with Setup (backend/update_check.py).
Source: "installed-by-setup.txt"; DestDir: "{app}"; Flags: ignoreversion

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{%USERPROFILE}"
Name: "{group}\{#AppName} Console (company connector)"; Filename: "{app}\{#ConsoleExe}"; WorkingDir: "{%USERPROFILE}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; WorkingDir: "{%USERPROFILE}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName}"; WorkingDir: "{%USERPROFILE}"; Flags: nowait postinstall skipifsilent

; No [UninstallDelete] and no [Registry] section: the uninstaller removes only what Setup
; installed, and never touches %LOCALAPPDATA%\MySQLDBAValidator (approved targets, .env, trust files, logs).

[Code]
const
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppGuid}}_is1';

// "0.11.0" or "0.12.0-rc1" -> a number Setup can compare; False when it is not a version.
function ParseVersion(const Text: String; var Value: Int64): Boolean;
var
  Core: String;
  P: Integer;
begin
  Core := Text;
  P := Pos('-', Core);
  if P > 0 then Core := Copy(Core, 1, P - 1);
  P := Pos('+', Core);
  if P > 0 then Core := Copy(Core, 1, P - 1);
  Result := StrToVersion(Core, Value);
end;

// Setup will not replace a newer installed version with this older one.
function InitializeSetup(): Boolean;
var
  OldText: String;
  OldVersion, NewVersion: Int64;
begin
  Result := True;
  if RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', OldText)
     and ParseVersion(OldText, OldVersion) and ParseVersion('{#AppVersion}', NewVersion) then
  begin
    if ComparePackedVersion(NewVersion, OldVersion) < 0 then
    begin
      SuppressibleMsgBox('{#AppName} ' + OldText + ' is already installed. This Setup installs the older version {#AppVersion}, ' +
        'and will not replace a newer version.' + #13#10#13#10 + 'Download the latest setup file from {#RepoUrl}/releases.',
        mbCriticalError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;

// Asks Windows (WMI) whether a validator started from this folder is running: the windowed
// app, or the Console app, which is also what the web app's background process runs as.
// Checked by the full path of the executable, so a portable copy elsewhere is not affected.
// Returns False when the question could not be answered; Running is then not meaningful and
// the caller must not treat it as "not running".
function CheckValidatorRunning(const Dir: String; var Running: Boolean): Boolean;
var
  Locator, Service, Found: Variant;
  Base: String;
begin
  Result := False;
  Running := False;
  Base := AddBackslash(Dir);
  StringChangeEx(Base, '\', '\\', True);
  // A ' in the path (a profile such as O'Brien) must not end the quoted WQL string.
  StringChangeEx(Base, '''', '\''', True);
  try
    Locator := CreateOleObject('WbemScripting.SWbemLocator');
    Service := Locator.ConnectServer('.', '{#ProcessNamespace}');
    Found := Service.ExecQuery('SELECT ProcessId FROM Win32_Process WHERE ExecutablePath = ''' + Base + '{#AppExe}'' OR ExecutablePath = ''' + Base + '{#ConsoleExe}''');
    Running := (not VarIsNull(Found)) and (Found.Count > 0);
    Result := True;
  except
    Result := False;
  end;
end;

function RunningHelp(): String;
begin
  Result := 'Close it first: close the Console window, or close the validator page in the browser and wait up to 15 minutes for the app to stop on its own (or end it in Task Manager). ' +
    'Setup does not close it for you, because it may hold a company connector session.';
end;

// Setup (and the uninstaller) go on only when it is known that no validator runs from the
// folder. If that cannot be found out, they stop too: replacing or removing the files of a
// running program is not safe, and Windows would only refuse part of it.
function StopReason(const Dir: String; const Action: String): String;
var
  Running: Boolean;
begin
  Result := '';
  if not CheckValidatorRunning(Dir, Running) then
    Result := 'Setup could not check whether {#AppName} is running from this folder, so it cannot ' + Action + ' safely.' + #13#10#13#10 +
      'Close {#AppName} if it is open (' + RunningHelp() + ')' + #13#10#13#10 + 'Then run it again. Nothing has been changed.'
  else if Running then
    Result := '{#AppName} is running from this folder, so Setup cannot ' + Action + '.' + #13#10#13#10 + RunningHelp() + #13#10#13#10 +
      'Then run it again. Nothing has been changed.';
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := StopReason(ExpandConstant('{app}'), 'replace its files');
end;

function InitializeUninstall(): Boolean;
var
  Reason: String;
begin
  Result := True;
  Reason := StopReason(ExpandConstant('{app}'), 'remove it');
  if Reason <> '' then
  begin
    SuppressibleMsgBox(Reason, mbCriticalError, MB_OK, IDOK);
    Result := False;
  end;
end;
