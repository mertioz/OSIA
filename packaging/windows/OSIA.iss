#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef RepoRoot
  #define RepoRoot "..\.."
#endif
#ifndef BuildRoot
  #define BuildRoot "..\..\build\windows"
#endif
#ifndef ReleaseRoot
  #define ReleaseRoot "..\..\release\windows"
#endif

[Setup]
AppId={{8B8666D0-0DD4-4D1C-86D0-6B5E23A4FE36}
AppName=OSIA
AppVersion={#AppVersion}
AppVerName=OSIA {#AppVersion}
AppPublisher=OSIA
AppPublisherURL=https://osia.space
AppSupportURL=https://osia.space
AppUpdatesURL=https://osia.space
DefaultDirName={localappdata}\Programs\OSIA
DefaultGroupName=OSIA
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#ReleaseRoot}
OutputBaseFilename=OSIA-Setup
SetupIconFile={#BuildRoot}\generated\osia.ico
UninstallDisplayIcon={app}\OSIA.exe
WizardStyle=modern
WizardSizePercent=115
WizardImageFile={#BuildRoot}\generated\wizard-large.png
WizardSmallImageFile={#BuildRoot}\generated\wizard-small.png
WizardBackColor=$120B0D
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
SetupLogging=yes
CloseApplications=yes
RestartApplications=no
AppMutex=Local\OSIA.Desktop
UsePreviousAppDir=yes
UsePreviousTasks=yes
VersionInfoVersion={#AppVersion}.0
VersionInfoCompany=OSIA
VersionInfoDescription=OSIA Installer
VersionInfoProductName=OSIA
VersionInfoProductVersion={#AppVersion}
ChangesAssociations=no
AllowNoIcons=yes
DisableWelcomePage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "french"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "autostart"; Description: "Launch OSIA when I sign in"; GroupDescription: "Startup:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked
Name: "firewall"; Description: "Enable secure phone access on private local networks"; GroupDescription: "Mobile connectivity:"; Flags: checkedonce

[Files]
Source: "{#RepoRoot}\dist\OSIA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\windows\configure_lan_firewall.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion

[Icons]
Name: "{group}\OSIA"; Filename: "{app}\OSIA.exe"; WorkingDir: "{app}"; IconFilename: "{app}\OSIA.exe"
Name: "{group}\Uninstall OSIA"; Filename: "{uninstallexe}"
Name: "{autodesktop}\OSIA"; Filename: "{app}\OSIA.exe"; WorkingDir: "{app}"; IconFilename: "{app}\OSIA.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\OSIA.exe"; Description: "Launch OSIA"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\OSIA.exe"; Parameters: "--osia-remove-integration"; Flags: runhidden waituntilterminated; RunOnceId: "OSIAIntegration"; Check: ShouldRemoveIntegration
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\tools\configure_lan_firewall.ps1"" -Action Remove"; Flags: runhidden waituntilterminated; RunOnceId: "OSIAFirewall"; Tasks: firewall; Check: ShouldRemoveIntegration

[UninstallDelete]
Type: files; Name: "{app}\.osia-no-integration-test"

[Code]
var
  StageLabel: TNewStaticText;
  DetailsButton: TNewButton;
  DetailsMemo: TNewMemo;
  ExistingIntegrationStopped: Boolean;
  IntegrationInstalled: Boolean;
  FirewallInstalled: Boolean;
  InstallSucceeded: Boolean;
  HadPreviousRunValue: Boolean;
  PreviousRunValue: String;
  IntegrationSuppressed: Boolean;

function IntegrationMarker: String;
begin
  Result := ExpandConstant('{app}\.osia-no-integration-test');
end;

function ShouldRemoveIntegration: Boolean;
begin
  { [UninstallRun] checks are evaluated while Setup records uninstall actions,
    before ssPostInstall creates files. Use the command-line state initialized
    with the wizard, not an app-local marker that does not exist yet. }
  Result := not IntegrationSuppressed;
end;

procedure AddDetail(const Text: String);
begin
  if Assigned(DetailsMemo) then
    DetailsMemo.Lines.Add(Text);
end;

procedure ToggleDetails(Sender: TObject);
begin
  DetailsMemo.Visible := not DetailsMemo.Visible;
  if DetailsMemo.Visible then
    DetailsButton.Caption := 'Hide details'
  else
    DetailsButton.Caption := 'Show details';
end;

procedure InitializeWizard;
begin
  WizardForm.Color := TColor($00120B0D);
  WizardForm.MainPanel.Color := TColor($00120B0D);
  WizardForm.PageNameLabel.Font.Color := TColor($00F5F1FF);
  WizardForm.PageNameLabel.Font.Size := 13;
  WizardForm.PageDescriptionLabel.Font.Color := TColor($00C5BDE0);

  StageLabel := TNewStaticText.Create(WizardForm);
  StageLabel.Parent := WizardForm.InstallingPage;
  StageLabel.Left := 0;
  StageLabel.Top := WizardForm.ProgressGauge.Top - ScaleY(28);
  StageLabel.Width := WizardForm.InstallingPage.ClientWidth;
  StageLabel.Font.Color := TColor($00F5F1FF);
  StageLabel.Caption := 'Preparing installation…';

  DetailsButton := TNewButton.Create(WizardForm);
  DetailsButton.Parent := WizardForm.InstallingPage;
  DetailsButton.Left := 0;
  DetailsButton.Top := WizardForm.ProgressGauge.Top + ScaleY(42);
  DetailsButton.Width := ScaleX(110);
  DetailsButton.Caption := 'Show details';
  DetailsButton.OnClick := @ToggleDetails;

  DetailsMemo := TNewMemo.Create(WizardForm);
  DetailsMemo.Parent := WizardForm.InstallingPage;
  DetailsMemo.Left := 0;
  DetailsMemo.Top := DetailsButton.Top + DetailsButton.Height + ScaleY(8);
  DetailsMemo.Width := WizardForm.InstallingPage.ClientWidth;
  DetailsMemo.Height := ScaleY(92);
  DetailsMemo.ReadOnly := True;
  DetailsMemo.ScrollBars := ssVertical;
  DetailsMemo.Visible := False;

  HadPreviousRunValue := RegQueryStringValue(
    HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'OSIA', PreviousRunValue);
  IntegrationSuppressed :=
    CompareText(ExpandConstant('{param:NOINTEGRATION|0}'), '1') = 0;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  ExistingExe: String;
begin
  Result := '';
  ExistingExe := ExpandConstant('{app}\OSIA.exe');
  if (not IntegrationSuppressed) and FileExists(ExistingExe) then
  begin
    AddDetail('Stopping the existing OSIA background service…');
    if Exec(ExistingExe, '--osia-remove-integration', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
      ExistingIntegrationStopped := True
    else
      Result := 'The existing OSIA background service could not be stopped.';
  end;
end;

procedure CurInstallProgressChanged(CurProgress, MaxProgress: Integer);
var
  Percent: Integer;
begin
  if MaxProgress > 0 then
    Percent := (CurProgress * 100) div MaxProgress
  else
    Percent := 0;
  StageLabel.Caption := Format('Installing OSIA files… %d%%', [Percent]);
end;

procedure RestorePreviousAutostart;
begin
  if HadPreviousRunValue then
    RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
      'OSIA', PreviousRunValue)
  else
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'OSIA');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  AppExe: String;
  FirewallScript: String;
  AutoCommand: String;
begin
  if CurStep = ssInstall then
  begin
    StageLabel.Caption := 'Installing OSIA files…';
    AddDetail('Copying the self-contained application and offline speech models.');
  end;

  if CurStep = ssPostInstall then
  begin
    AppExe := ExpandConstant('{app}\OSIA.exe');
    if IntegrationSuppressed then
    begin
      SaveStringToFile(IntegrationMarker, 'isolated validation', False)
    end
    else
    begin
      StageLabel.Caption := 'Configuring mobile connectivity…';
      AddDetail('Registering the per-user OSIA Server and wake-recovery tasks.');
      if (not Exec(AppExe, '--osia-install-integration', '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      begin
        Exec(AppExe, '--osia-remove-integration', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode);
        RestorePreviousAutostart;
        RaiseException('OSIA could not configure its background mobile service.');
      end;
      IntegrationInstalled := True;
    end;

    AutoCommand := '"' + AppExe + '" --wake';
    if WizardIsTaskSelected('autostart') then
      RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
        'OSIA', AutoCommand)
    else
      RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'OSIA');

    if WizardIsTaskSelected('firewall') then
    begin
      StageLabel.Caption := 'Securing private-network access…';
      AddDetail('Requesting the narrow Private/LocalSubnet TCP 8766 firewall rule.');
      FirewallScript := ExpandConstant('{app}\tools\configure_lan_firewall.ps1');
      if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
        '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
        FirewallScript + '" -Action Install -ProgramPath "' + AppExe + '"',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
        FirewallInstalled := True
      else
        MsgBox('OSIA was installed, but Windows did not create the optional private-network firewall rule. You can retry from the installer repair flow.',
          mbInformation, MB_OK);
    end;
    StageLabel.Caption := 'Finalizing OSIA…';
    AddDetail('Installation completed. Your settings remain in your user profile.');
  end;

  if CurStep = ssDone then
    InstallSucceeded := True;
end;

procedure DeinitializeSetup;
var
  ResultCode: Integer;
  AppExe: String;
  FirewallScript: String;
begin
  if InstallSucceeded then
    exit;
  AppExe := ExpandConstant('{app}\OSIA.exe');
  if IntegrationInstalled and FileExists(AppExe) then
    Exec(AppExe, '--osia-remove-integration', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode)
  else if ExistingIntegrationStopped and FileExists(AppExe) then
    Exec(AppExe, '--osia-install-integration', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
  if FirewallInstalled then
  begin
    FirewallScript := ExpandConstant('{app}\tools\configure_lan_firewall.ps1');
    Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
      '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
      FirewallScript + '" -Action Remove', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
  end;
  RestorePreviousAutostart;
end;
