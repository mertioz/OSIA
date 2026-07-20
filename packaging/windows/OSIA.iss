#ifndef AppVersion
  #error AppVersion must be supplied by build_windows.py
#endif
#ifndef AppVersionMajor
  #error AppVersionMajor must be supplied by build_windows.py
#endif
#ifndef AppVersionMinor
  #error AppVersionMinor must be supplied by build_windows.py
#endif
#ifndef AppVersionPatch
  #error AppVersionPatch must be supplied by build_windows.py
#endif
#ifndef BuildChannel
  #error BuildChannel must be supplied by build_windows.py
#endif
#ifndef InternalBuild
  #error InternalBuild must be supplied by build_windows.py
#endif
#ifndef SignedBuild
  #error SignedBuild must be supplied by build_windows.py
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
WizardStyle=modern dynamic hidebevels
WizardSizePercent=115
WizardImageFile={#BuildRoot}\generated\wizard-large.png
WizardSmallImageFile={#BuildRoot}\generated\wizard-small.png
WizardImageFileDynamicDark={#BuildRoot}\generated\wizard-large.png
WizardSmallImageFileDynamicDark={#BuildRoot}\generated\wizard-small.png
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

[CustomMessages]
english.AutostartTask=Launch OSIA when I sign in
english.AutostartGroup=Startup
english.DesktopTask=Create a desktop shortcut
english.ShortcutsGroup=Shortcuts
english.FirewallTask=Enable secure phone access on private local networks
english.MobileGroup=Mobile connectivity
english.LaunchOSIA=Launch OSIA
english.UninstallOSIA=Uninstall OSIA
english.BrandEyebrow=OSIA  ·  WINDOWS SETUP
english.StepLocation=STEP 1 OF 3  ·  LOCATION
english.StepTasks=STEP 2 OF 3  ·  OPTIONS
english.StepReady=STEP 3 OF 3  ·  REVIEW
english.StepInstalling=INSTALLING  ·  REAL-TIME PROGRESS
english.StepPreparing=PREPARING  ·  SAFETY CHECK
english.WelcomeHeading=Meet OSIA
english.WelcomeBody=Your personal AI assistant, ready to run on this PC.
english.VersionArchitecture=Version %s  ·  Windows x64
english.PreviewState=Internal preview  ·  Unsigned build
english.StableState=Stable channel  ·  Signed build
english.UpdateState=In-place update  ·  Your data stays in your profile
english.UpdateDowngrade=This installer cannot replace a newer OSIA version with %s.
english.ReadyVersion=OSIA %s  ·  Windows x64
english.Destination=Destination: %s
english.FinishedHeading=OSIA is ready
english.FinishedBody=Installation completed successfully. Launch OSIA to get started.
english.FinishedState=SUCCESS  ·  YOUR SETTINGS STAY IN YOUR USER PROFILE
english.ShowDetails=Show details
english.HideDetails=Hide details
english.PhasePreparing=Preparing OSIA…
english.PhaseInstalling=Installing application files… %d%%
english.PhaseConfiguring=Configuring Windows integration…
english.PhaseFirewall=Securing private-network access…
english.PhaseFinalizing=Finalizing installation…
english.DetailStopping=Stopping the existing OSIA background service…
english.DetailCopying=Copying the self-contained application and offline speech models.
english.DetailIntegration=Registering the per-user OSIA Server and wake-recovery tasks.
english.DetailFirewall=Requesting the narrow Private/LocalSubnet TCP 8766 firewall rule.
english.DetailComplete=Installation completed. Your settings remain in your user profile.
english.ExistingServiceError=The existing OSIA background service could not be stopped. Close OSIA and try again.
english.IntegrationError=OSIA could not configure its background mobile service. The installation was rolled back safely.
english.FirewallWarning=OSIA was installed, but Windows did not create the optional private-network firewall rule. You can retry from the installer repair flow.
french.AutostartTask=Lancer OSIA à l’ouverture de ma session
french.AutostartGroup=Démarrage
french.DesktopTask=Créer un raccourci sur le Bureau
french.ShortcutsGroup=Raccourcis
french.FirewallTask=Autoriser l’accès sécurisé du téléphone sur le réseau privé local
french.MobileGroup=Connexion mobile
french.LaunchOSIA=Lancer OSIA
french.UninstallOSIA=Désinstaller OSIA
french.BrandEyebrow=OSIA  ·  INSTALLATION WINDOWS
french.StepLocation=ÉTAPE 1 SUR 3  ·  EMPLACEMENT
french.StepTasks=ÉTAPE 2 SUR 3  ·  OPTIONS
french.StepReady=ÉTAPE 3 SUR 3  ·  VÉRIFICATION
french.StepInstalling=INSTALLATION  ·  PROGRESSION RÉELLE
french.StepPreparing=PRÉPARATION  ·  CONTRÔLE DE SÉCURITÉ
french.WelcomeHeading=Découvrez OSIA
french.WelcomeBody=Votre assistant IA personnel, prêt à fonctionner sur ce PC.
french.VersionArchitecture=Version %s  ·  Windows x64
french.PreviewState=Aperçu interne  ·  Build non signé
french.StableState=Canal stable  ·  Build signé
french.UpdateState=Mise à jour sur place  ·  Vos données restent dans votre profil
french.UpdateDowngrade=Cet installateur ne peut pas remplacer une version plus récente d’OSIA par la version %s.
french.ReadyVersion=OSIA %s  ·  Windows x64
french.Destination=Destination : %s
french.FinishedHeading=OSIA est prêt
french.FinishedBody=L’installation est terminée. Lancez OSIA pour commencer.
french.FinishedState=SUCCÈS  ·  VOS RÉGLAGES RESTENT DANS VOTRE PROFIL UTILISATEUR
french.ShowDetails=Afficher les détails
french.HideDetails=Masquer les détails
french.PhasePreparing=Préparation d’OSIA…
french.PhaseInstalling=Installation des fichiers de l’application… %d%%
french.PhaseConfiguring=Configuration de l’intégration Windows…
french.PhaseFirewall=Sécurisation de l’accès au réseau privé…
french.PhaseFinalizing=Finalisation de l’installation…
french.DetailStopping=Arrêt du service OSIA existant…
french.DetailCopying=Copie de l’application autonome et des modèles vocaux hors ligne.
french.DetailIntegration=Enregistrement du serveur OSIA et des tâches de reprise pour cet utilisateur.
french.DetailFirewall=Demande de la règle TCP 8766 limitée au profil privé et au sous-réseau local.
french.DetailComplete=Installation terminée. Vos réglages restent dans votre profil utilisateur.
french.ExistingServiceError=Le service OSIA existant n’a pas pu être arrêté. Fermez OSIA puis réessayez.
french.IntegrationError=OSIA n’a pas pu configurer son service mobile en arrière-plan. L’installation a été annulée en toute sécurité.
french.FirewallWarning=OSIA est installé, mais Windows n’a pas créé la règle facultative du pare-feu privé. Vous pourrez réessayer depuis le mode réparation de l’installateur.

[Tasks]
Name: "autostart"; Description: "{cm:AutostartTask}"; GroupDescription: "{cm:AutostartGroup}:"; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:DesktopTask}"; GroupDescription: "{cm:ShortcutsGroup}:"; Flags: unchecked
Name: "firewall"; Description: "{cm:FirewallTask}"; GroupDescription: "{cm:MobileGroup}:"; Flags: checkedonce

[Files]
Source: "{#RepoRoot}\dist\OSIA\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#RepoRoot}\windows\configure_lan_firewall.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\_internal"; Check: IsUpdateMode

[Icons]
Name: "{group}\OSIA"; Filename: "{app}\OSIA.exe"; WorkingDir: "{app}"; IconFilename: "{app}\OSIA.exe"
Name: "{group}\{cm:UninstallOSIA}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\OSIA"; Filename: "{app}\OSIA.exe"; WorkingDir: "{app}"; IconFilename: "{app}\OSIA.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\OSIA.exe"; Description: "{cm:LaunchOSIA}"; Flags: nowait postinstall skipifsilent; Check: not IsUpdateMode
Filename: "{app}\OSIA.exe"; Parameters: "--wake"; Flags: nowait; Check: IsUpdateMode

[UninstallRun]
Filename: "{app}\OSIA.exe"; Parameters: "--osia-remove-integration"; Flags: runhidden waituntilterminated; RunOnceId: "OSIAIntegration"; Check: ShouldRemoveIntegration
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File ""{app}\tools\configure_lan_firewall.ps1"" -Action Remove"; Flags: runhidden waituntilterminated; RunOnceId: "OSIAFirewall"; Tasks: firewall; Check: ShouldRemoveIntegration

[UninstallDelete]
Type: files; Name: "{app}\.osia-no-integration-test"

[Code]
const
  OSIA_MAIN_BACKGROUND = $00120B0D;
  OSIA_ELEVATED_SURFACE = $00211217;
  OSIA_SECONDARY_SURFACE = $002F1821;
  OSIA_ACCENT = $00F5B5C4;
  OSIA_PRIMARY_TEXT = $00FFF6F8;
  OSIA_SECONDARY_TEXT = $00E7CED5;
  OSIA_HELPER_TEXT = $00C2A4AD;
  OSIA_DISABLED_TEXT = $00907981;
  OSIA_SUCCESS_TEXT = $00C1E3A7;
  OSIA_ERROR_TEXT = $00A7A7F3;
  OSIA_WARNING_TEXT = $007DC2F1;

var
  StageLabel: TNewStaticText;
  DetailsButton: TNewButton;
  DetailsMemo: TNewMemo;
  HeaderStepLabel: TNewStaticText;
  WelcomeLogo: TBitmapImage;
  WelcomeMetaLabel: TNewStaticText;
  WelcomePreviewLabel: TNewStaticText;
  FinishedLogo: TBitmapImage;
  FinishedStateLabel: TNewStaticText;
  ReadySummaryLabel: TNewStaticText;
  ProductVersionText: String;
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

function IsUpdateMode: Boolean;
begin
  Result := CompareText(ExpandConstant('{param:OSIAUPDATE|0}'), '1') = 0;
end;

function IsDowngrade: Boolean;
var
  Major, Minor, Revision, Build: Word;
  InstalledVersion, PackageVersion: Int64;
begin
  Result := False;
  if GetVersionComponents(ExpandConstant('{app}\OSIA.exe'),
    Major, Minor, Revision, Build) then
  begin
    InstalledVersion := PackVersionComponents(Major, Minor, Revision, Build);
    PackageVersion := PackVersionComponents(
      {#AppVersionMajor}, {#AppVersionMinor}, {#AppVersionPatch}, 0);
    Result := PackageVersion < InstalledVersion;
  end;
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
    DetailsButton.Caption := CustomMessage('HideDetails')
  else
    DetailsButton.Caption := CustomMessage('ShowDetails');
end;

procedure StyleLabel(Control: TNewStaticText; Foreground, Background: TColor);
begin
  Control.Color := Background;
  Control.Font.Name := 'Segoe UI';
  Control.Font.Color := Foreground;
end;

procedure StylePage(Page: TNewNotebookPage);
begin
  Page.Color := TColor(OSIA_MAIN_BACKGROUND);
end;

procedure StyleDarkMemo(Control: TNewMemo);
begin
  Control.Color := TColor(OSIA_SECONDARY_SURFACE);
  Control.Font.Name := 'Segoe UI';
  Control.Font.Color := TColor(OSIA_SECONDARY_TEXT);
end;

procedure StyleDarkList(Control: TNewCheckListBox);
begin
  Control.Color := TColor(OSIA_SECONDARY_SURFACE);
  Control.Font.Name := 'Segoe UI';
  Control.Font.Color := TColor(OSIA_PRIMARY_TEXT);
  Control.MinItemHeight := ScaleY(24);
end;

procedure InitializeWizard;
var
  ContentLeft: Integer;
  ContentWidth: Integer;
begin
  ProductVersionText := '{#AppVersion}';
  WizardForm.Font.Name := 'Segoe UI';
  WizardForm.Font.Size := 9;
  WizardForm.Color := TColor(OSIA_MAIN_BACKGROUND);
  WizardForm.MainPanel.Color := TColor(OSIA_ELEVATED_SURFACE);
  WizardForm.InnerPage.Color := TColor(OSIA_MAIN_BACKGROUND);
  StylePage(WizardForm.WelcomePage);
  StylePage(WizardForm.FinishedPage);
  StylePage(WizardForm.SelectDirPage);
  StylePage(WizardForm.SelectTasksPage);
  StylePage(WizardForm.ReadyPage);
  StylePage(WizardForm.PreparingPage);
  StylePage(WizardForm.InstallingPage);

  { Keep Windows task dialogs and UAC native. The main wizard alone owns the
    dark OSIA surface, so system dialogs retain their accessible system colors. }
  WizardForm.WizardBitmapImage.Visible := False;
  WizardForm.WizardBitmapImage2.Visible := False;
  WizardForm.WizardSmallBitmapImage.BackColor := TColor(OSIA_ELEVATED_SURFACE);
  WizardForm.WizardSmallBitmapImage.SetBounds(
    WizardForm.MainPanel.ClientWidth - ScaleX(66), ScaleY(8),
    ScaleX(54), ScaleY(54));
  WizardForm.WizardSmallBitmapImage.Stretch := True;

  HeaderStepLabel := TNewStaticText.Create(WizardForm);
  HeaderStepLabel.Parent := WizardForm.MainPanel;
  HeaderStepLabel.SetBounds(ScaleX(20), ScaleY(5),
    WizardForm.MainPanel.ClientWidth - ScaleX(98), ScaleY(13));
  HeaderStepLabel.Caption := CustomMessage('BrandEyebrow');
  StyleLabel(HeaderStepLabel, TColor(OSIA_ACCENT), TColor(OSIA_ELEVATED_SURFACE));
  HeaderStepLabel.Font.Size := 8;
  HeaderStepLabel.Font.Style := [fsBold];

  WizardForm.PageNameLabel.SetBounds(ScaleX(20), ScaleY(19),
    WizardForm.MainPanel.ClientWidth - ScaleX(100), ScaleY(23));
  StyleLabel(WizardForm.PageNameLabel, TColor(OSIA_PRIMARY_TEXT),
    TColor(OSIA_ELEVATED_SURFACE));
  WizardForm.PageNameLabel.Font.Size := 14;
  WizardForm.PageNameLabel.Font.Style := [fsBold];
  WizardForm.PageDescriptionLabel.SetBounds(ScaleX(20), ScaleY(42),
    WizardForm.MainPanel.ClientWidth - ScaleX(100), ScaleY(16));
  StyleLabel(WizardForm.PageDescriptionLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_ELEVATED_SURFACE));

  ContentLeft := ScaleX(28);
  ContentWidth := WizardForm.WelcomePage.ClientWidth - ContentLeft - ScaleX(28);
  WelcomeLogo := TBitmapImage.Create(WizardForm);
  WelcomeLogo.Parent := WizardForm.WelcomePage;
  WelcomeLogo.SetBounds(ContentLeft, ScaleY(34), ScaleX(74), ScaleY(74));
  WelcomeLogo.BackColor := TColor(OSIA_MAIN_BACKGROUND);
  WelcomeLogo.PngImage.Assign(WizardForm.WizardSmallBitmapImage.PngImage);
  WelcomeLogo.Center := True;
  WelcomeLogo.Stretch := True;

  WizardForm.WelcomeLabel1.SetBounds(ContentLeft + ScaleX(98), ScaleY(35),
    ContentWidth - ScaleX(98), ScaleY(47));
  WizardForm.WelcomeLabel1.Caption := CustomMessage('WelcomeHeading');
  WizardForm.WelcomeLabel1.WordWrap := True;
  StyleLabel(WizardForm.WelcomeLabel1, TColor(OSIA_PRIMARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.WelcomeLabel1.Font.Size := 18;
  WizardForm.WelcomeLabel1.Font.Style := [fsBold];
  WizardForm.WelcomeLabel2.SetBounds(ContentLeft + ScaleX(98), ScaleY(88),
    ContentWidth - ScaleX(98), ScaleY(48));
  WizardForm.WelcomeLabel2.Caption := CustomMessage('WelcomeBody');
  WizardForm.WelcomeLabel2.WordWrap := True;
  StyleLabel(WizardForm.WelcomeLabel2, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.WelcomeLabel2.Font.Size := 10;

  WelcomeMetaLabel := TNewStaticText.Create(WizardForm);
  WelcomeMetaLabel.Parent := WizardForm.WelcomePage;
  WelcomeMetaLabel.SetBounds(ContentLeft + ScaleX(98), ScaleY(147),
    ContentWidth - ScaleX(98), ScaleY(24));
  WelcomeMetaLabel.Caption :=
    Format(CustomMessage('VersionArchitecture'), [ProductVersionText]);
  StyleLabel(WelcomeMetaLabel, TColor(OSIA_HELPER_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));

  WelcomePreviewLabel := TNewStaticText.Create(WizardForm);
  WelcomePreviewLabel.Parent := WizardForm.WelcomePage;
  WelcomePreviewLabel.SetBounds(ContentLeft + ScaleX(98), ScaleY(179),
    ContentWidth - ScaleX(98), ScaleY(24));
  if IsUpdateMode then
    WelcomePreviewLabel.Caption := CustomMessage('UpdateState')
  else if {#InternalBuild} = 1 then
    WelcomePreviewLabel.Caption := CustomMessage('PreviewState')
  else
    WelcomePreviewLabel.Caption := CustomMessage('StableState');
  StyleLabel(WelcomePreviewLabel, TColor(OSIA_WARNING_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WelcomePreviewLabel.Font.Style := [fsBold];

  FinishedLogo := TBitmapImage.Create(WizardForm);
  FinishedLogo.Parent := WizardForm.FinishedPage;
  FinishedLogo.SetBounds(ContentLeft, ScaleY(34), ScaleX(74), ScaleY(74));
  FinishedLogo.BackColor := TColor(OSIA_MAIN_BACKGROUND);
  FinishedLogo.PngImage.Assign(WizardForm.WizardSmallBitmapImage.PngImage);
  FinishedLogo.Center := True;
  FinishedLogo.Stretch := True;
  WizardForm.FinishedHeadingLabel.SetBounds(ContentLeft + ScaleX(98), ScaleY(38),
    ContentWidth - ScaleX(98), ScaleY(42));
  WizardForm.FinishedHeadingLabel.Caption := CustomMessage('FinishedHeading');
  StyleLabel(WizardForm.FinishedHeadingLabel, TColor(OSIA_PRIMARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.FinishedHeadingLabel.Font.Size := 17;
  WizardForm.FinishedHeadingLabel.Font.Style := [fsBold];
  WizardForm.FinishedLabel.SetBounds(ContentLeft + ScaleX(98), ScaleY(88),
    ContentWidth - ScaleX(98), ScaleY(48));
  WizardForm.FinishedLabel.Caption := CustomMessage('FinishedBody');
  WizardForm.FinishedLabel.WordWrap := True;
  StyleLabel(WizardForm.FinishedLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  FinishedStateLabel := TNewStaticText.Create(WizardForm);
  FinishedStateLabel.Parent := WizardForm.FinishedPage;
  FinishedStateLabel.SetBounds(ContentLeft + ScaleX(98), ScaleY(151),
    ContentWidth - ScaleX(98), ScaleY(26));
  FinishedStateLabel.Caption := CustomMessage('FinishedState');
  StyleLabel(FinishedStateLabel, TColor(OSIA_SUCCESS_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  FinishedStateLabel.Font.Size := 8;
  FinishedStateLabel.Font.Style := [fsBold];
  WizardForm.RunList.SetBounds(ContentLeft + ScaleX(98), ScaleY(199),
    ContentWidth - ScaleX(98), ScaleY(58));
  StyleDarkList(WizardForm.RunList);

  StyleLabel(WizardForm.SelectDirLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  StyleLabel(WizardForm.SelectDirBrowseLabel, TColor(OSIA_HELPER_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  StyleLabel(WizardForm.DiskSpaceLabel, TColor(OSIA_HELPER_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.DirEdit.Color := TColor($00FFFFFF);
  WizardForm.DirEdit.Font.Color := TColor($0021182F);
  WizardForm.DirEdit.Font.Name := 'Segoe UI';

  StyleLabel(WizardForm.SelectTasksLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  StyleDarkList(WizardForm.TasksList);

  StyleLabel(WizardForm.ReadyLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  ReadySummaryLabel := TNewStaticText.Create(WizardForm);
  ReadySummaryLabel.Parent := WizardForm.ReadyPage;
  ReadySummaryLabel.SetBounds(WizardForm.ReadyMemo.Left,
    WizardForm.ReadyMemo.Top + ScaleY(12), WizardForm.ReadyMemo.Width, ScaleY(62));
  ReadySummaryLabel.WordWrap := True;
  StyleLabel(ReadySummaryLabel, TColor(OSIA_HELPER_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.ReadyMemo.Top := WizardForm.ReadyMemo.Top + ScaleY(82);
  WizardForm.ReadyMemo.Height := WizardForm.ReadyMemo.Height - ScaleY(82);
  StyleDarkMemo(WizardForm.ReadyMemo);

  StyleLabel(WizardForm.PreparingLabel, TColor(OSIA_ERROR_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  StyleDarkMemo(WizardForm.PreparingMemo);
  WizardForm.PreparingYesRadio.Color := TColor(OSIA_MAIN_BACKGROUND);
  WizardForm.PreparingYesRadio.Font.Color := TColor(OSIA_SECONDARY_TEXT);
  WizardForm.PreparingNoRadio.Color := TColor(OSIA_MAIN_BACKGROUND);
  WizardForm.PreparingNoRadio.Font.Color := TColor(OSIA_SECONDARY_TEXT);

  StyleLabel(WizardForm.StatusLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  StyleLabel(WizardForm.FilenameLabel, TColor(OSIA_HELPER_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));

  StageLabel := TNewStaticText.Create(WizardForm);
  StageLabel.Parent := WizardForm.InstallingPage;
  StageLabel.Left := 0;
  StageLabel.Top := WizardForm.ProgressGauge.Top - ScaleY(28);
  StageLabel.Width := WizardForm.InstallingPage.ClientWidth;
  StageLabel.Height := ScaleY(22);
  StageLabel.Caption := CustomMessage('PhasePreparing');
  StyleLabel(StageLabel, TColor(OSIA_PRIMARY_TEXT), TColor(OSIA_MAIN_BACKGROUND));
  StageLabel.Font.Style := [fsBold];

  DetailsButton := TNewButton.Create(WizardForm);
  DetailsButton.Parent := WizardForm.InstallingPage;
  DetailsButton.Left := 0;
  DetailsButton.Top := WizardForm.ProgressGauge.Top + ScaleY(42);
  DetailsButton.Width := ScaleX(110);
  DetailsButton.Caption := CustomMessage('ShowDetails');
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
  StyleDarkMemo(DetailsMemo);

  StyleLabel(WizardForm.BeveledLabel, TColor(OSIA_DISABLED_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.NextButton.Default := True;
  WizardForm.CancelButton.Cancel := True;

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
  if IsUpdateMode and IsDowngrade then
  begin
    Result := Format(CustomMessage('UpdateDowngrade'), ['{#AppVersion}']);
    exit;
  end;
  ExistingExe := ExpandConstant('{app}\OSIA.exe');
  if (not IntegrationSuppressed) and FileExists(ExistingExe) then
  begin
    AddDetail(CustomMessage('DetailStopping'));
    if Exec(ExistingExe, '--osia-remove-integration', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
      ExistingIntegrationStopped := True
    else
      Result := CustomMessage('ExistingServiceError');
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
  StageLabel.Caption := Format(CustomMessage('PhaseInstalling'), [Percent]);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if CurPageID = wpSelectDir then
    HeaderStepLabel.Caption := CustomMessage('StepLocation')
  else if CurPageID = wpSelectTasks then
    HeaderStepLabel.Caption := CustomMessage('StepTasks')
  else if CurPageID = wpReady then
  begin
    HeaderStepLabel.Caption := CustomMessage('StepReady');
    ReadySummaryLabel.Caption :=
      Format(CustomMessage('ReadyVersion'), [ProductVersionText]) + #13#10 +
      Format(CustomMessage('Destination'), [WizardDirValue]) + #13#10 +
      WizardForm.DiskSpaceLabel.Caption;
  end
  else if CurPageID = wpPreparing then
    HeaderStepLabel.Caption := CustomMessage('StepPreparing')
  else if CurPageID = wpInstalling then
    HeaderStepLabel.Caption := CustomMessage('StepInstalling')
  else
    HeaderStepLabel.Caption := CustomMessage('BrandEyebrow');
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
    StageLabel.Caption := Format(CustomMessage('PhaseInstalling'), [0]);
    AddDetail(CustomMessage('DetailCopying'));
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
      StageLabel.Caption := CustomMessage('PhaseConfiguring');
      AddDetail(CustomMessage('DetailIntegration'));
      if (not Exec(AppExe, '--osia-install-integration', '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      begin
        Exec(AppExe, '--osia-remove-integration', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode);
        RestorePreviousAutostart;
        RaiseException(CustomMessage('IntegrationError'));
      end;
      IntegrationInstalled := True;
    end;

    AutoCommand := '"' + AppExe + '" --wake';
    if WizardIsTaskSelected('autostart') then
      RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run',
        'OSIA', AutoCommand)
    else
      RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'OSIA');

    if WizardIsTaskSelected('firewall') and (not IsUpdateMode) then
    begin
      StageLabel.Caption := CustomMessage('PhaseFirewall');
      AddDetail(CustomMessage('DetailFirewall'));
      FirewallScript := ExpandConstant('{app}\tools\configure_lan_firewall.ps1');
      if Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
        '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' +
        FirewallScript + '" -Action Install -ProgramPath "' + AppExe + '"',
        '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then
        FirewallInstalled := True
      else
        MsgBox(CustomMessage('FirewallWarning'),
          mbInformation, MB_OK);
    end;
    StageLabel.Caption := CustomMessage('PhaseFinalizing');
    AddDetail(CustomMessage('DetailComplete'));
  end;

  if CurStep = ssDone then
    InstallSucceeded := True;
end;

procedure InitializeUninstallProgressForm;
begin
  UninstallProgressForm.Font.Name := 'Segoe UI';
  UninstallProgressForm.Color := TColor(OSIA_MAIN_BACKGROUND);
  UninstallProgressForm.InnerPage.Color := TColor(OSIA_MAIN_BACKGROUND);
  UninstallProgressForm.InstallingPage.Color := TColor(OSIA_MAIN_BACKGROUND);
  UninstallProgressForm.MainPanel.Color := TColor(OSIA_ELEVATED_SURFACE);
  StyleLabel(UninstallProgressForm.PageNameLabel, TColor(OSIA_PRIMARY_TEXT),
    TColor(OSIA_ELEVATED_SURFACE));
  UninstallProgressForm.PageNameLabel.Font.Style := [fsBold];
  StyleLabel(UninstallProgressForm.PageDescriptionLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_ELEVATED_SURFACE));
  StyleLabel(UninstallProgressForm.StatusLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  StyleLabel(UninstallProgressForm.BeveledLabel, TColor(OSIA_DISABLED_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
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
  if IsUpdateMode and ExistingIntegrationStopped and FileExists(AppExe) then
    Exec(AppExe, '--wake', '', SW_SHOWNORMAL,
      ewNoWait, ResultCode);
end;
