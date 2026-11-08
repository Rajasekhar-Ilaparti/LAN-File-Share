; Inno Setup script for LAN File Share.
; Build everything with build_installer.bat in the project folder.

#define AppName "LAN File Share"
; build_installer.bat passes the version from file_share_app.py (/DAppVersion=...)
#ifndef AppVersion
  #define AppVersion "5.0"
#endif
#define AppExe "LAN File Share.exe"
#define FirewallRule "LAN File Share"

[Setup]
AppId={{6F7C2B1E-4A9D-4C51-9E2B-3D8A7F0C5B21}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppName}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
; Per-user install by default (no admin needed); the first page lets
; people pick "install for all users" instead.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Windows 8.1 or newer, 64-bit
MinVersion=6.3
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=Output
OutputBaseFilename=LAN-File-Share-Setup
SetupIconFile=..\app_icon.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardImageFile=wizard_1x.bmp,wizard_2x.bmp
WizardSmallImageFile=wizard_small_1x.bmp,wizard_small_2x.bmp
Compression=lzma2/max
SolidCompression=yes
; The app holds this mutex while open, so setup asks to close it first
AppMutex=LANFileShare_SingleInstance
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "firewall"; Description: "Let phones on my network connect (adds a Windows Firewall rule; asks for administrator permission)"; GroupDescription: "Connection:"

[Files]
Source: "..\dist\{#AppExe}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"; Comment: "Share files with phones and PCs on your Wi-Fi"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
; Allow inbound connections to the app from the local subnet only, on every
; network profile (home Wi-Fi is often marked "Public" by Windows).
Filename: "powershell.exe"; \
  Parameters: "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command ""Remove-NetFirewallRule -DisplayName '{#FirewallRule}' -ErrorAction SilentlyContinue; New-NetFirewallRule -DisplayName '{#FirewallRule}' -Description 'Lets phones and PCs on your local network open LAN File Share.' -Direction Inbound -Action Allow -Protocol TCP -Program '{app}\{#AppExe}' -RemoteAddress LocalSubnet -Profile Any"""; \
  Verb: runas; Flags: shellexec runhidden waituntilterminated; Tasks: firewall; Check: not WizardSilent; \
  StatusMsg: "Allowing phones on your network to connect..."
Filename: "{app}\{#AppExe}"; Description: "Open {#AppName} now"; Flags: nowait postinstall skipifsilent
; In-app updates run setup with /SILENT: reopen the app when they finish. (They also skip the
; firewall step above; the rule from the first install still covers the same program path.)
Filename: "{app}\{#AppExe}"; Flags: nowait skipifnotsilent runasoriginaluser

[Code]
function PowerShellPath(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
end;

{ Remove the firewall rule on uninstall, but only ask for admin permission
  if the rule actually exists. }
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if Exec(PowerShellPath(),
            '-NoProfile -WindowStyle Hidden -Command "if (Get-NetFirewallRule -DisplayName ''{#FirewallRule}'' -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"',
            '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 1) then
      ShellExec('runas', PowerShellPath(),
                '-NoProfile -WindowStyle Hidden -Command "Remove-NetFirewallRule -DisplayName ''{#FirewallRule}''"',
                '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
