; LAILA GROUP Attendance & Payroll - one-click installer (Inno Setup 6)
; Build with build_installer.bat. Result: installer\Output\LailaPayroll_Setup.exe
;
; What the installer does, in one run:
;  1. Stops any running copy, removes the scheduled start task and any earlier installation.
;  2. Deletes ALL old data (C:\ProgramData\LailaPayroll) for a completely fresh start (asks once if found).
;  3. Installs LailaPayroll.exe, opens the firewall ports, registers it to start with Windows
;     (runs as SYSTEM, restarts itself if it stops), starts it.
;  4. Opens the first-time setup page in the browser.

#define AppName "SPhere Payroll - LAILA GROUP Attendance & Payroll"
#ifndef AppVersion
  #define AppVersion "4.2.0"
#endif
#define AppGuid "6C1E2B7A-4F3D-4E1B-9A57-1A9D2B3C4E5F"
#define HttpPort "8585"
#define HttpsPort "8443"

[Setup]
AppId={{{#AppGuid}}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=SPHere - 01617144755
AppComments=Developed by: SPHere - 01617144755
DefaultDirName={commonpf64}\LailaPayroll
UninstallDisplayIcon={app}\LailaPayroll.exe
UninstallDisplayName={#AppName}
OutputBaseFilename=LailaPayroll_Setup
SetupIconFile=sphere.ico
WizardSmallImageFile=..\static\img\lg-logo-small.bmp
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
CloseApplications=force
Compression=lzma2/max
SolidCompression=yes

[Files]
; program folder built by PyInstaller --onedir (runs in place, nothing unpacked to TEMP)
Source: "..\dist\LailaPayroll\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "check.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "install-task.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\static\img\lg-logo.ico"; DestDir: "{app}"; Flags: ignoreversion
Source: "sphere.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{commondesktop}\SPhere Payroll - Check"; Filename: "{app}\check.bat"; IconFilename: "{app}\sphere.ico"

[INI]
Filename: "{commondesktop}\SPhere Payroll.url"; Section: "InternetShortcut"; Key: "URL"; String: "http://127.0.0.1:{#HttpPort}/"
Filename: "{commondesktop}\SPhere Payroll.url"; Section: "InternetShortcut"; Key: "IconFile"; String: "{app}\sphere.ico"
Filename: "{commondesktop}\SPhere Payroll.url"; Section: "InternetShortcut"; Key: "IconIndex"; String: "0"

[Run]
; firewall: office PCs + ZKTeco machines (HTTP) and phones (HTTPS)
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LailaPayroll"""; Flags: runhidden waituntilterminated
Filename: "netsh.exe"; Parameters: "advfirewall firewall add rule name=""LailaPayroll"" dir=in action=allow protocol=TCP localport={#HttpPort},{#HttpsPort} profile=any"; Flags: runhidden waituntilterminated
; start with Windows as SYSTEM, no time limit, restart if it stops - then start now and wait until it answers
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\install-task.ps1"" -App ""{app}"" -Port {#HttpPort}"; StatusMsg: "Starting SPhere Payroll..."; Flags: runhidden waituntilterminated
Filename: "http://127.0.0.1:{#HttpPort}/setup"; Flags: shellexec runasoriginaluser nowait skipifsilent

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -Command ""Stop-ScheduledTask -TaskName 'LailaPayroll' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName 'LailaPayroll' -Confirm:$false -ErrorAction SilentlyContinue"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveTask"
Filename: "taskkill.exe"; Parameters: "/F /IM LailaPayroll.exe"; Flags: runhidden waituntilterminated; RunOnceId: "KillApp"
Filename: "netsh.exe"; Parameters: "advfirewall firewall delete rule name=""LailaPayroll"""; Flags: runhidden waituntilterminated; RunOnceId: "RemoveFirewall"

[InstallDelete]
Type: files; Name: "{commondesktop}\LAILA GROUP Payroll.url"
Type: files; Name: "{commondesktop}\LAILA Payroll - Check.lnk"

[UninstallDelete]
Type: files; Name: "{commondesktop}\SPhere Payroll.url"

[Code]
procedure Run(const Exe, Params: String);
var Code: Integer;
begin
  Exec(Exe, Params, '', SW_HIDE, ewWaitUntilTerminated, Code);
end;

function InitializeSetup(): Boolean;
var
  Uninst, DataDir, OldApp: String;
  Answer: Integer;
  KeepData: Boolean;
  Backup: String;
begin
  Result := True;
  DataDir := ExpandConstant('{commonappdata}\LailaPayroll');
  OldApp := ExpandConstant('{commonpf64}\LailaPayroll');

  KeepData := WizardSilent and (ExpandConstant('{param:KEEPDATA|0}') = '1');   { silent update: /KEEPDATA=1 }
  if (DirExists(DataDir) or DirExists(OldApp)) and not WizardSilent then
  begin
    Answer := MsgBox('SPhere Payroll is already installed on this PC.' + #13#10 + #13#10 +
              'YES  = FRESH START: delete the old program AND ALL DATA (employees, punches, payroll, settings).' + #13#10 + #13#10 +
              'NO   = UPDATE: install the new version and KEEP all data.' + #13#10 + #13#10 +
              'CANCEL = do nothing.', mbConfirmation, MB_YESNOCANCEL or MB_DEFBUTTON2);
    if Answer = IDCANCEL then
    begin
      Result := False;
      Exit;
    end;
    KeepData := Answer = IDNO;
  end;
  { stop the running copy and its start-up task }
  Run('powershell.exe', '-NoProfile -Command "Stop-ScheduledTask -TaskName ''LailaPayroll'' -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName ''LailaPayroll'' -Confirm:$false -ErrorAction SilentlyContinue"');
  Run('taskkill.exe', '/F /IM LailaPayroll.exe');

  { run the old uninstaller silently, if there is one }
  if RegQueryStringValue(HKLM64, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppGuid}}_is1', 'UninstallString', Uninst) or
     RegQueryStringValue(HKLM32, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppGuid}}_is1', 'UninstallString', Uninst) then
    Run(RemoveQuotes(Uninst), '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART');

  { update: safety copy of the database (program is stopped now, so the copy is complete) }
  if KeepData and FileExists(DataDir + '\laila_payroll.db') then
  begin
    Backup := DataDir + '\backups\before_update_' + GetDateTimeString('yyyymmdd_hhnnss', #0, #0);
    ForceDirectories(Backup);
    Run('cmd.exe', '/c copy /y "' + DataDir + '\laila_payroll.db*" "' + Backup + '\"');
  end;

  { wipe leftovers: old program folder and ALL old data }
  Sleep(1500);
  if DirExists(OldApp) then DelTree(OldApp, True, True, True);
  if not KeepData then
  begin
    if DirExists(DataDir) then DelTree(DataDir, True, True, True);
  end;
  if (not KeepData) and DirExists(DataDir) then
  begin
    MsgBox('Could not delete ' + DataDir + '. Close any program using it and run the installer again.', mbError, MB_OK);
    Result := False;
  end;
end;
