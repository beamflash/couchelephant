; CouchElephant for Windows. Build:
;   iscc /DMyAppVersion=1.5.23 packaging\windows\couchelephant.iss
; Needs dist\couchelephant (the server), dist\CouchElephantTray and the
; WinSW program at packaging\windows\WinSW-x64.exe (fetched by the workflow).
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{26FFE3FA-814C-4C40-8D8B-11250F592674}
AppName=CouchElephant
AppVersion={#MyAppVersion}
AppPublisher=CouchElephant
DefaultDirName={autopf}\CouchElephant
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\dist
OutputBaseFilename=CouchElephant-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
UninstallDisplayName=CouchElephant
WizardStyle=modern

[Files]
Source: "..\..\dist\couchelephant\*"; DestDir: "{app}\couchelephant"; Flags: recursesubdirs ignoreversion
Source: "..\..\dist\CouchElephantTray\*"; DestDir: "{app}\tray"; Flags: recursesubdirs ignoreversion
Source: "WinSW-x64.exe"; DestDir: "{app}"; DestName: "CouchElephantService.exe"; Flags: ignoreversion
Source: "couchelephant-service.xml"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Dirs]
Name: "{commonappdata}\CouchElephant"
Name: "{commonappdata}\CouchElephant\recordings"

[Registry]
Root: HKLM; Subkey: "Software\CouchElephant"; ValueType: string; ValueName: "Port"; ValueData: "{code:GetPort}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "CouchElephantTray"; ValueData: """{app}\tray\CouchElephantTray.exe"" --port {code:GetPort}"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\tray\CouchElephantTray.exe"; Parameters: "--port {code:GetPort}"; Flags: nowait runasoriginaluser
Filename: "http://localhost:{code:GetPort}/welcome"; Description: "Open CouchElephant"; Flags: shellexec postinstall skipifsilent runasoriginaluser

[Code]
var
  ChosenPort: Integer;

function Service(const Verb: String): Integer;
var
  Code: Integer;
begin
  if not Exec(ExpandConstant('{app}\CouchElephantService.exe'), Verb, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
  Result := Code;
end;

procedure RunCmd(const Args: String; var Code: Integer);
begin
  if not Exec(ExpandConstant('{cmd}'), '/C ' + Args, '', SW_HIDE, ewWaitUntilTerminated, Code) then
    Code := -1;
end;

function PortFree(P: Integer): Boolean;
var
  Code: Integer;
begin
  { findstr exits 0 when something is already listening there. }
  RunCmd('netstat -ano -p TCP | findstr /R /C:":' + IntToStr(P) + ' .*LISTENING"', Code);
  Result := Code <> 0;
end;

procedure StopOldInstall;
var
  Code: Integer;
begin
  if FileExists(ExpandConstant('{app}\CouchElephantService.exe')) then
    Service('stop');
  RunCmd('taskkill /F /IM CouchElephantTray.exe', Code);
end;

function GetPort(Param: String): String;
begin
  Result := IntToStr(ChosenPort);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Saved: String;
  P: Integer;
begin
  { An upgrade keeps its port. The old service must stop first or it
    would hold the port and push us onto the next one. }
  StopOldInstall;
  P := 8710;
  if RegQueryStringValue(HKLM, 'Software\CouchElephant', 'Port', Saved) then
    P := StrToIntDef(Saved, 8710);
  while (not PortFree(P)) and (P < 8760) do
    P := P + 1;
  ChosenPort := P;
  Result := '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Template: AnsiString;
  Text: String;
  Code: Integer;
begin
  if CurStep <> ssPostInstall then Exit;
  LoadStringFromFile(ExpandConstant('{tmp}\couchelephant-service.xml'), Template);
  Text := String(Template);
  StringChangeEx(Text, '@PORT@', IntToStr(ChosenPort), True);
  SaveStringToFile(ExpandConstant('{app}\CouchElephantService.xml'), AnsiString(Text), False);
  { Home networks only: Private profile, never Public. }
  RunCmd('netsh advfirewall firewall delete rule name="CouchElephant"', Code);
  RunCmd('netsh advfirewall firewall add rule name="CouchElephant" dir=in action=allow protocol=TCP localport='
    + IntToStr(ChosenPort) + ' profile=private', Code);
  Service('install');
  Service('start');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    RunCmd('taskkill /F /IM CouchElephantTray.exe', Code);
    Service('stop');
    Service('uninstall');
    RunCmd('netsh advfirewall firewall delete rule name="CouchElephant"', Code);
  end;
  if (CurUninstallStep = usPostUninstall) and (ExpandConstant('{param:DELETEDATA|0}') = '1') then
    DelTree(ExpandConstant('{commonappdata}\CouchElephant'), True, True, True);
end;
