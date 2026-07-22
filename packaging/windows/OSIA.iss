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
PrivilegesRequiredOverridesAllowed=commandline
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#ReleaseRoot}
OutputBaseFilename=OSIA-Setup
SetupIconFile={#BuildRoot}\generated\osia.ico
UninstallDisplayIcon={app}\OSIA.exe
WizardStyle=modern dark hidebevels includetitlebar
WizardBackColor=#110b15
WizardSizePercent=112
WizardImageFile={#BuildRoot}\generated\wizard-large.png
WizardSmallImageFile={#BuildRoot}\generated\wizard-small.png
WizardImageFileDynamicDark={#BuildRoot}\generated\wizard-large.png
WizardSmallImageFileDynamicDark={#BuildRoot}\generated\wizard-small.png
WizardSmallImageBackColor=#110b15
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
english.BrandEyebrow=OSIA
english.StepLocation=LOCATION  ·  1/3
english.StepTasks=OPTIONS  ·  2/3
english.StepReady=REVIEW  ·  3/3
english.StepInstalling=INSTALLATION
english.StepPreparing=SAFETY CHECK
english.WelcomeHeading=Your OSIA, on this PC
english.WelcomeBody=A private workspace for your assistant, ready in a few steps.
english.VersionArchitecture=Version %s  ·  Windows
english.PreviewState=Internal preview  ·  Not signed
english.StableState=Stable release
english.UpdateState=In-place update  ·  Your data stays in your profile
english.UpdateHeading=Update OSIA
english.UpdateDescription=A secure update, without reconfiguring OSIA.
english.UpdateVersions=OSIA %s → OSIA %s
english.UpdatePreserved=Your settings, memory, and connections will be preserved.
english.UpdateUnsigned=Internal preview  ·  Not signed
english.UpdateButton=Update
english.UpdateMissingInstall=OSIA Update could not find an existing OSIA installation for this user.
english.UpdateInvalidDir=OSIA Update found an invalid existing installation directory. Reinstall OSIA before retrying the update.
english.UpdateInvalidVersion=OSIA Update could not validate the installed OSIA version.
english.UpdateNotNewer=OSIA %s cannot update the installed version %s. The target version must be newer.
english.UpdateBackupError=OSIA Update could not secure the existing application files. Close OSIA and retry. The existing installation was not changed; details are in the Setup log.
english.UpdateStepConfirm=UPDATE
english.UpdateStepProgress=UPDATE IN PROGRESS
english.UpdateProgressHeading=Updating OSIA
english.UpdateProgressDescription=Applying the verified OSIA update.
english.UpdatePhasePreparing=Preparing the update
english.UpdatePhaseClosing=Closing OSIA components
english.UpdatePhaseFiles=Updating OSIA files… %d%%
english.UpdatePhaseIntegration=Restoring Windows integration
english.UpdatePhaseFinalizing=Finalizing
english.UpdatePhaseRelaunch=Relaunching OSIA
english.UpdateDetailCopying=Replacing the verified self-contained OSIA payload.
english.UpdateDetailIntegration=Verifying and repairing the per-user OSIA Server and wake-recovery tasks.
english.UpdateDetailComplete=Update completed. Your personal data was preserved.
english.UpdateIntegrationError=OSIA could not restore its background mobile service. The update was rolled back safely.
english.UpdateFinishedHeading=OSIA has been updated
english.UpdateFinishedVersion=Version %s was installed successfully.
english.UpdateFinishedPreserved=Your personal data was preserved.
english.ReadyVersion=OSIA %s  ·  Windows
english.Destination=Destination: %s
english.FinishedHeading=OSIA is ready
english.FinishedBody=Installation completed successfully. Launch OSIA to get started.
english.FinishedState=Your settings stay in your profile
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
french.BrandEyebrow=OSIA
french.StepLocation=EMPLACEMENT  ·  1/3
french.StepTasks=OPTIONS  ·  2/3
french.StepReady=VÉRIFICATION  ·  3/3
french.StepInstalling=INSTALLATION
french.StepPreparing=CONTRÔLE DE SÉCURITÉ
french.WelcomeHeading=Votre OSIA, sur ce PC
french.WelcomeBody=Un espace privé pour votre assistant, prêt en quelques étapes.
french.VersionArchitecture=Version %s  ·  Windows
french.PreviewState=Aperçu interne  ·  Non signé
french.StableState=Version stable
french.UpdateState=Mise à jour sur place  ·  Vos données restent dans votre profil
french.UpdateHeading=Mise à jour d’OSIA
french.UpdateDescription=Une mise à jour sûre, sans reconfigurer OSIA.
french.UpdateVersions=OSIA %s → OSIA %s
french.UpdatePreserved=Vos réglages, votre mémoire et vos connexions seront conservés.
french.UpdateUnsigned=Aperçu interne  ·  Non signé
french.UpdateButton=Mettre à jour
french.UpdateMissingInstall=OSIA Update n’a trouvé aucune installation OSIA existante pour cet utilisateur.
french.UpdateInvalidDir=OSIA Update a trouvé un dossier d’installation existant invalide. Réinstallez OSIA avant de relancer la mise à jour.
french.UpdateInvalidVersion=OSIA Update n’a pas pu valider la version OSIA installée.
french.UpdateNotNewer=OSIA %s ne peut pas mettre à jour la version %s installée. La version cible doit être plus récente.
french.UpdateBackupError=OSIA Update n’a pas pu sécuriser les fichiers existants. Fermez OSIA puis réessayez. L’installation n’a pas été modifiée ; les détails figurent dans le journal d’installation.
french.UpdateStepConfirm=MISE À JOUR
french.UpdateStepProgress=MISE À JOUR EN COURS
french.UpdateProgressHeading=Mise à jour d’OSIA
french.UpdateProgressDescription=Application de la mise à jour OSIA vérifiée.
french.UpdatePhasePreparing=Préparation de la mise à jour
french.UpdatePhaseClosing=Fermeture des composants OSIA
french.UpdatePhaseFiles=Mise à jour des fichiers OSIA… %d%%
french.UpdatePhaseIntegration=Restauration de l’intégration Windows
french.UpdatePhaseFinalizing=Finalisation
french.UpdatePhaseRelaunch=Relance d’OSIA
french.UpdateDetailCopying=Remplacement du paquet OSIA autonome vérifié.
french.UpdateDetailIntegration=Vérification et réparation du serveur OSIA et des tâches de reprise pour cet utilisateur.
french.UpdateDetailComplete=Mise à jour terminée. Vos données personnelles ont été conservées.
french.UpdateIntegrationError=OSIA n’a pas pu restaurer son service mobile en arrière-plan. La mise à jour a été annulée en toute sécurité.
french.UpdateFinishedHeading=OSIA a été mise à jour
french.UpdateFinishedVersion=Version %s installée avec succès.
french.UpdateFinishedPreserved=Vos données personnelles ont été conservées.
french.ReadyVersion=OSIA %s  ·  Windows
french.Destination=Destination : %s
french.FinishedHeading=OSIA est prêt
french.FinishedBody=L’installation est terminée. Lancez OSIA pour commencer.
french.FinishedState=Vos réglages restent dans votre profil
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
#include "profile_path.iss"

const
  OSIA_MAIN_BACKGROUND = $00150B11;
  OSIA_ELEVATED_SURFACE = $001A1222;
  OSIA_SECONDARY_SURFACE = $00251A30;
  OSIA_ACCENT = $00F1B1D8;
  OSIA_PRIMARY_TEXT = $00FFF9FC;
  OSIA_SECONDARY_TEXT = $00E8D8E2;
  OSIA_HELPER_TEXT = $00BFA9B8;
  OSIA_DISABLED_TEXT = $008C7786;
  OSIA_SUCCESS_TEXT = $00C8E6B2;
  OSIA_ERROR_TEXT = $00B5B4F4;
  OSIA_WARNING_TEXT = $0089C9F2;

var
  StageLabel: TNewStaticText;
  DetailsButton: TNewButton;
  DetailsMemo: TNewMemo;
  HeaderStepLabel: TNewStaticText;
  WelcomeLogo: TBitmapImage;
  WelcomeMetaLabel: TNewStaticText;
  WelcomePreviewLabel: TNewStaticText;
  UpdatePage: TWizardPage;
  UpdateLogo: TBitmapImage;
  UpdateVersionsLabel: TNewStaticText;
  UpdatePreservedLabel: TNewStaticText;
  UpdateUnsignedLabel: TNewStaticText;
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
  UpdateModeRequested: Boolean;
  UpdateModeActive: Boolean;
  ExistingInstallDir: String;
  ExistingVersionText: String;
  UpdateBackupPrepared: Boolean;
  UpdateBackupInternal: String;
  UpdateBackupExe: String;
  UpdateBackupFailure: String;
  FreshInstallOnboardingEligible: Boolean;
  OsiaProfileDirectory: String;

function HasExactUpdateSwitch: Boolean;
var
  Index: Integer;
begin
  Result := False;
  for Index := 1 to ParamCount do
    if CompareText(ParamStr(Index), '/OSIAUPDATE=1') = 0 then
    begin
      Result := True;
      exit;
    end;
end;

function InitializeSetup: Boolean;
var
  UninstallKey: String;
  RegistryVersion: String;
  ExistingExe: String;
  ExistingPackedVersion: Int64;
  RegistryPackedVersion: Int64;
  PackagePackedVersion: Int64;
  ExistingMajor, ExistingMinor, ExistingPatch, ExistingBuild: Word;
begin
  Result := True;
  OsiaProfileDirectory := ResolveOsiaProfileDirectory;
  UninstallKey :=
    'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8B8666D0-0DD4-4D1C-86D0-6B5E23A4FE36}_is1';
  FreshInstallOnboardingEligible :=
    (not DirExists(OsiaProfileDirectory)) and
    (not RegKeyExists(HKCU, UninstallKey));
  UpdateModeRequested := HasExactUpdateSwitch;
  UpdateModeActive := False;
  if not UpdateModeRequested then
    exit;

  if (not RegKeyExists(HKCU, UninstallKey)) or
    (not RegQueryStringValue(HKCU, UninstallKey, 'InstallLocation',
      ExistingInstallDir)) then
  begin
    MsgBox(CustomMessage('UpdateMissingInstall'), mbError, MB_OK);
    Result := False;
    exit;
  end;

  ExistingInstallDir := RemoveBackslashUnlessRoot(Trim(ExistingInstallDir));
  ExistingExe := AddBackslash(ExistingInstallDir) + 'OSIA.exe';
  if (ExistingInstallDir = '') or (not DirExists(ExistingInstallDir)) or
    (not DirExists(AddBackslash(ExistingInstallDir) + '_internal')) or
    (not FileExists(ExistingExe)) then
  begin
    MsgBox(CustomMessage('UpdateInvalidDir'), mbError, MB_OK);
    Result := False;
    exit;
  end;

  if (not RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion',
      RegistryVersion)) or
    (not StrToVersion(RegistryVersion, RegistryPackedVersion)) or
    (not GetVersionComponents(ExistingExe, ExistingMajor, ExistingMinor,
      ExistingPatch, ExistingBuild)) then
  begin
    MsgBox(CustomMessage('UpdateInvalidVersion'), mbError, MB_OK);
    Result := False;
    exit;
  end;
  ExistingPackedVersion := PackVersionComponents(ExistingMajor, ExistingMinor,
    ExistingPatch, ExistingBuild);
  ExistingVersionText := Format('%d.%d.%d', [ExistingMajor, ExistingMinor,
    ExistingPatch]);
  if (ExistingBuild <> 0) or
    (CompareStr(RegistryVersion, ExistingVersionText) <> 0) or
    (not SamePackedVersion(RegistryPackedVersion, ExistingPackedVersion)) then
  begin
    MsgBox(CustomMessage('UpdateInvalidVersion'), mbError, MB_OK);
    Result := False;
    exit;
  end;

  PackagePackedVersion := PackVersionComponents(
    {#AppVersionMajor}, {#AppVersionMinor}, {#AppVersionPatch}, 0);
  if ComparePackedVersion(PackagePackedVersion, ExistingPackedVersion) <= 0 then
  begin
    MsgBox(Format(CustomMessage('UpdateNotNewer'), ['{#AppVersion}',
      ExistingVersionText]), mbError, MB_OK);
    Result := False;
    exit;
  end;

  UpdateModeActive := True;
end;

function IntegrationMarker: String;
begin
  Result := ExpandConstant('{app}\.osia-no-integration-test');
end;

function IsUpdateMode: Boolean;
begin
  Result := UpdateModeActive;
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
  if IsUpdateMode then
  begin
    WizardForm.Caption := CustomMessage('UpdateHeading');
    WizardForm.DirEdit.Text := ExistingInstallDir;
  end;
  WizardForm.Font.Name := 'Segoe UI';
  WizardForm.Font.Size := 9;
  WizardForm.Color := TColor(OSIA_MAIN_BACKGROUND);
  WizardForm.MainPanel.Color := TColor(OSIA_MAIN_BACKGROUND);
  WizardForm.InnerPage.Color := TColor(OSIA_MAIN_BACKGROUND);
  StylePage(WizardForm.WelcomePage);
  StylePage(WizardForm.FinishedPage);
  StylePage(WizardForm.SelectDirPage);
  StylePage(WizardForm.SelectTasksPage);
  StylePage(WizardForm.ReadyPage);
  StylePage(WizardForm.PreparingPage);
  StylePage(WizardForm.InstallingPage);

  { Keep UAC native; Inno's forced dark style provides readable system dialogs
    while the OSIA wizard uses one consistent branded surface. }
  WizardForm.WizardBitmapImage.Visible := False;
  WizardForm.WizardBitmapImage2.Visible := False;
  WizardForm.WizardSmallBitmapImage.BackColor := TColor(OSIA_MAIN_BACKGROUND);
  WizardForm.WizardSmallBitmapImage.SetBounds(
    ScaleX(22), ScaleY(9), ScaleX(46), ScaleY(46));
  WizardForm.WizardSmallBitmapImage.Stretch := True;

  HeaderStepLabel := TNewStaticText.Create(WizardForm);
  HeaderStepLabel.Parent := WizardForm.MainPanel;
  HeaderStepLabel.SetBounds(ScaleX(84), ScaleY(7),
    WizardForm.MainPanel.ClientWidth - ScaleX(108), ScaleY(13));
  HeaderStepLabel.Caption := CustomMessage('BrandEyebrow');
  StyleLabel(HeaderStepLabel, TColor(OSIA_ACCENT), TColor(OSIA_MAIN_BACKGROUND));
  HeaderStepLabel.Font.Size := 8;
  HeaderStepLabel.Font.Style := [fsBold];

  WizardForm.PageNameLabel.SetBounds(ScaleX(84), ScaleY(21),
    WizardForm.MainPanel.ClientWidth - ScaleX(108), ScaleY(23));
  StyleLabel(WizardForm.PageNameLabel, TColor(OSIA_PRIMARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.PageNameLabel.Font.Size := 14;
  WizardForm.PageNameLabel.Font.Style := [fsBold];
  WizardForm.PageDescriptionLabel.SetBounds(ScaleX(84), ScaleY(44),
    WizardForm.MainPanel.ClientWidth - ScaleX(108), ScaleY(16));
  StyleLabel(WizardForm.PageDescriptionLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));

  ContentLeft := ScaleX(28);
  ContentWidth := WizardForm.WelcomePage.ClientWidth - ContentLeft - ScaleX(28);

  if IsUpdateMode then
  begin
    UpdatePage := CreateCustomPage(wpWelcome, CustomMessage('UpdateHeading'),
      CustomMessage('UpdateDescription'));
    StylePage(UpdatePage.Surface);
    UpdateLogo := TBitmapImage.Create(WizardForm);
    UpdateLogo.Parent := UpdatePage.Surface;
    UpdateLogo.SetBounds((UpdatePage.Surface.ClientWidth - ScaleX(82)) div 2,
      ScaleY(24), ScaleX(82), ScaleY(82));
    UpdateLogo.BackColor := TColor(OSIA_MAIN_BACKGROUND);
    UpdateLogo.PngImage.Assign(WizardForm.WizardSmallBitmapImage.PngImage);
    UpdateLogo.Center := True;
    UpdateLogo.Stretch := True;

    UpdateVersionsLabel := TNewStaticText.Create(WizardForm);
    UpdateVersionsLabel.Parent := UpdatePage.Surface;
    UpdateVersionsLabel.AutoSize := False;
    UpdateVersionsLabel.SetBounds(ContentLeft, ScaleY(126),
      ContentWidth, ScaleY(34));
    UpdateVersionsLabel.Caption := Format(CustomMessage('UpdateVersions'), [
      ExistingVersionText, ProductVersionText]);
    UpdateVersionsLabel.WordWrap := False;
    UpdateVersionsLabel.Alignment := taCenter;
    StyleLabel(UpdateVersionsLabel, TColor(OSIA_PRIMARY_TEXT),
      TColor(OSIA_MAIN_BACKGROUND));
    UpdateVersionsLabel.Font.Size := 14;
    UpdateVersionsLabel.Font.Style := [fsBold];

    UpdatePreservedLabel := TNewStaticText.Create(WizardForm);
    UpdatePreservedLabel.Parent := UpdatePage.Surface;
    UpdatePreservedLabel.AutoSize := False;
    UpdatePreservedLabel.SetBounds(ContentLeft + ScaleX(18), ScaleY(171),
      ContentWidth - ScaleX(36), ScaleY(48));
    UpdatePreservedLabel.Caption := CustomMessage('UpdatePreserved');
    UpdatePreservedLabel.WordWrap := True;
    UpdatePreservedLabel.Alignment := taCenter;
    StyleLabel(UpdatePreservedLabel, TColor(OSIA_SECONDARY_TEXT),
      TColor(OSIA_MAIN_BACKGROUND));
    UpdatePreservedLabel.Font.Size := 10;

    UpdateUnsignedLabel := TNewStaticText.Create(WizardForm);
    UpdateUnsignedLabel.Parent := UpdatePage.Surface;
    UpdateUnsignedLabel.AutoSize := False;
    UpdateUnsignedLabel.SetBounds(ContentLeft, ScaleY(232),
      ContentWidth, ScaleY(24));
    UpdateUnsignedLabel.Caption := CustomMessage('UpdateUnsigned');
    UpdateUnsignedLabel.Visible := ({#InternalBuild} = 1) and ({#SignedBuild} = 0);
    StyleLabel(UpdateUnsignedLabel, TColor(OSIA_WARNING_TEXT),
      TColor(OSIA_MAIN_BACKGROUND));
    UpdateUnsignedLabel.Alignment := taCenter;
    UpdateUnsignedLabel.Font.Style := [fsBold];
  end;

  WelcomeLogo := TBitmapImage.Create(WizardForm);
  WelcomeLogo.Parent := WizardForm.WelcomePage;
  WelcomeLogo.SetBounds((WizardForm.WelcomePage.ClientWidth - ScaleX(82)) div 2,
    ScaleY(22), ScaleX(82), ScaleY(82));
  WelcomeLogo.BackColor := TColor(OSIA_MAIN_BACKGROUND);
  WelcomeLogo.PngImage.Assign(WizardForm.WizardSmallBitmapImage.PngImage);
  WelcomeLogo.Center := True;
  WelcomeLogo.Stretch := True;

  WizardForm.WelcomeLabel1.SetBounds(ContentLeft, ScaleY(120),
    ContentWidth, ScaleY(42));
  WizardForm.WelcomeLabel1.Caption := CustomMessage('WelcomeHeading');
  WizardForm.WelcomeLabel1.WordWrap := True;
  WizardForm.WelcomeLabel1.Alignment := taCenter;
  StyleLabel(WizardForm.WelcomeLabel1, TColor(OSIA_PRIMARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.WelcomeLabel1.Font.Size := 18;
  WizardForm.WelcomeLabel1.Font.Style := [fsBold];
  WizardForm.WelcomeLabel2.SetBounds(ContentLeft + ScaleX(24), ScaleY(168),
    ContentWidth - ScaleX(48), ScaleY(44));
  WizardForm.WelcomeLabel2.Caption := CustomMessage('WelcomeBody');
  WizardForm.WelcomeLabel2.WordWrap := True;
  WizardForm.WelcomeLabel2.Alignment := taCenter;
  StyleLabel(WizardForm.WelcomeLabel2, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.WelcomeLabel2.Font.Size := 10;

  WelcomeMetaLabel := TNewStaticText.Create(WizardForm);
  WelcomeMetaLabel.Parent := WizardForm.WelcomePage;
  WelcomeMetaLabel.AutoSize := False;
  WelcomeMetaLabel.SetBounds(ContentLeft, ScaleY(225), ContentWidth, ScaleY(22));
  WelcomeMetaLabel.Caption :=
    Format(CustomMessage('VersionArchitecture'), [ProductVersionText]);
  StyleLabel(WelcomeMetaLabel, TColor(OSIA_HELPER_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WelcomeMetaLabel.Alignment := taCenter;

  WelcomePreviewLabel := TNewStaticText.Create(WizardForm);
  WelcomePreviewLabel.Parent := WizardForm.WelcomePage;
  WelcomePreviewLabel.AutoSize := False;
  WelcomePreviewLabel.SetBounds(ContentLeft, ScaleY(255), ContentWidth, ScaleY(22));
  if IsUpdateMode then
    WelcomePreviewLabel.Caption := CustomMessage('UpdateState')
  else if {#InternalBuild} = 1 then
    WelcomePreviewLabel.Caption := CustomMessage('PreviewState')
  else
    WelcomePreviewLabel.Caption := CustomMessage('StableState');
  StyleLabel(WelcomePreviewLabel, TColor(OSIA_WARNING_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WelcomePreviewLabel.Alignment := taCenter;
  WelcomePreviewLabel.Font.Style := [fsBold];

  FinishedLogo := TBitmapImage.Create(WizardForm);
  FinishedLogo.Parent := WizardForm.FinishedPage;
  FinishedLogo.SetBounds((WizardForm.FinishedPage.ClientWidth - ScaleX(82)) div 2,
    ScaleY(22), ScaleX(82), ScaleY(82));
  FinishedLogo.BackColor := TColor(OSIA_MAIN_BACKGROUND);
  FinishedLogo.PngImage.Assign(WizardForm.WizardSmallBitmapImage.PngImage);
  FinishedLogo.Center := True;
  FinishedLogo.Stretch := True;
  WizardForm.FinishedHeadingLabel.SetBounds(ContentLeft, ScaleY(120),
    ContentWidth, ScaleY(38));
  WizardForm.FinishedHeadingLabel.Caption := CustomMessage('FinishedHeading');
  StyleLabel(WizardForm.FinishedHeadingLabel, TColor(OSIA_PRIMARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  WizardForm.FinishedHeadingLabel.Alignment := taCenter;
  WizardForm.FinishedHeadingLabel.Font.Size := 17;
  WizardForm.FinishedHeadingLabel.Font.Style := [fsBold];
  WizardForm.FinishedLabel.SetBounds(ContentLeft + ScaleX(24), ScaleY(168),
    ContentWidth - ScaleX(48), ScaleY(48));
  WizardForm.FinishedLabel.Caption := CustomMessage('FinishedBody');
  WizardForm.FinishedLabel.WordWrap := True;
  WizardForm.FinishedLabel.Alignment := taCenter;
  StyleLabel(WizardForm.FinishedLabel, TColor(OSIA_SECONDARY_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  FinishedStateLabel := TNewStaticText.Create(WizardForm);
  FinishedStateLabel.Parent := WizardForm.FinishedPage;
  FinishedStateLabel.AutoSize := False;
  FinishedStateLabel.SetBounds(ContentLeft, ScaleY(229), ContentWidth, ScaleY(24));
  FinishedStateLabel.Caption := CustomMessage('FinishedState');
  StyleLabel(FinishedStateLabel, TColor(OSIA_SUCCESS_TEXT),
    TColor(OSIA_MAIN_BACKGROUND));
  FinishedStateLabel.Alignment := taCenter;
  FinishedStateLabel.Font.Size := 8;
  FinishedStateLabel.Font.Style := [fsBold];
  WizardForm.RunList.SetBounds(ContentLeft + ScaleX(42), ScaleY(267),
    ContentWidth - ScaleX(84), ScaleY(50));
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
  WizardForm.NextButton.Font.Name := 'Segoe UI Semibold';
  WizardForm.CancelButton.Font.Name := 'Segoe UI';
  WizardForm.BackButton.Font.Name := 'Segoe UI';

  HadPreviousRunValue := RegQueryStringValue(
    HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'OSIA', PreviousRunValue);
  IntegrationSuppressed :=
    CompareText(ExpandConstant('{param:NOINTEGRATION|0}'), '1') = 0;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := IsUpdateMode and
    ((PageID = wpWelcome) or (PageID = wpSelectDir) or
     (PageID = wpSelectProgramGroup) or (PageID = wpSelectTasks) or
     (PageID = wpReady));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if IsUpdateMode and Assigned(UpdatePage) and (CurPageID = UpdatePage.ID) then
    WizardForm.DirEdit.Text := ExistingInstallDir;
end;

procedure CancelButtonClick(CurPageID: Integer; var Cancel, Confirm: Boolean);
begin
  if IsUpdateMode then
    Confirm := False;
end;

procedure RegisterExtraCloseApplicationsResources;
begin
  if IsUpdateMode then
    RegisterExtraCloseApplicationsResource(
      False, AddBackslash(ExistingInstallDir) + 'OSIA.exe');
end;

function CopyFileWithRetry(const Source, Destination: String): Boolean;
var
  Attempt: Integer;
begin
  Result := False;
  for Attempt := 1 to 20 do
  begin
    if FileExists(Destination) then
      DeleteFile(Destination);
    if FileCopy(Source, Destination, True) then
    begin
      Result := True;
      exit;
    end;
    if Attempt < 20 then
      Sleep(250);
  end;
end;

function RenameFileWithRetry(const Source, Destination: String): Boolean;
var
  Attempt: Integer;
begin
  Result := False;
  for Attempt := 1 to 40 do
  begin
    if RenameFile(Source, Destination) then
    begin
      Result := True;
      exit;
    end;
    if Attempt < 40 then
      Sleep(250);
  end;
end;

function PrepareUpdateRollback: Boolean;
var
  ExistingInternal: String;
  ExistingExe: String;
begin
  Result := False;
  UpdateBackupFailure := '';
  ExistingInternal := AddBackslash(ExistingInstallDir) + '_internal';
  ExistingExe := AddBackslash(ExistingInstallDir) + 'OSIA.exe';
  UpdateBackupInternal := AddBackslash(ExistingInstallDir) +
    '_internal.osia-rollback';
  UpdateBackupExe := AddBackslash(ExistingInstallDir) +
    'OSIA.exe.osia-rollback';

  if (not FileExists(ExistingExe)) or (not DirExists(ExistingInternal)) then
  begin
    UpdateBackupFailure := 'invalid_source';
    Log('WOR-149 rollback preflight failed: invalid source installation.');
    exit;
  end;
  if FileExists(UpdateBackupExe) or DirExists(UpdateBackupInternal) then
  begin
    UpdateBackupFailure := 'stale_backup';
    Log('WOR-149 rollback preflight failed: stale local backup exists.');
    exit;
  end;

  Log('WOR-149 rollback: copying the launcher beside the installation.');
  if not CopyFileWithRetry(ExistingExe, UpdateBackupExe) then
  begin
    UpdateBackupFailure := 'executable_copy_failed';
    Log('WOR-149 rollback preflight failed: launcher copy failed.');
    exit;
  end;

  Log('WOR-149 rollback: atomically preserving the existing payload in place.');
  if not RenameFileWithRetry(ExistingInternal, UpdateBackupInternal) then
  begin
    UpdateBackupFailure := 'payload_rename_failed';
    Log('WOR-149 rollback preflight failed: local payload rename failed after retries.');
    DeleteFile(UpdateBackupExe);
    exit;
  end;
  UpdateBackupPrepared := True;
  Log('WOR-149 rollback: local backup prepared successfully.');
  Result := True;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
  ExistingExe: String;
begin
  Result := '';
  if IsUpdateMode and
    (CompareText(RemoveBackslashUnlessRoot(WizardDirValue),
      ExistingInstallDir) <> 0) then
  begin
    Result := CustomMessage('UpdateInvalidDir');
    exit;
  end;
  if IsUpdateMode then
  begin
    ExistingExe := AddBackslash(ExistingInstallDir) + 'OSIA.exe';
    StageLabel.Caption := CustomMessage('UpdatePhasePreparing');
    AddDetail(CustomMessage('UpdatePhasePreparing'));
    AddDetail(CustomMessage('UpdatePhaseClosing'));
  end;
  if not IsUpdateMode then
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
  if IsUpdateMode then
    StageLabel.Caption := Format(CustomMessage('UpdatePhaseFiles'), [Percent])
  else
    StageLabel.Caption := Format(CustomMessage('PhaseInstalling'), [Percent]);
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if IsUpdateMode then
    WizardForm.Caption := CustomMessage('UpdateHeading');
  if IsUpdateMode and Assigned(UpdatePage) and (CurPageID = UpdatePage.ID) then
  begin
    HeaderStepLabel.Caption := CustomMessage('UpdateStepConfirm');
    WizardForm.NextButton.Caption := CustomMessage('UpdateButton');
    WizardForm.CancelButton.Caption := SetupMessage(msgButtonCancel);
  end
  else if IsUpdateMode and (CurPageID = wpInstalling) then
  begin
    HeaderStepLabel.Caption := CustomMessage('UpdateStepProgress');
    WizardForm.PageNameLabel.Caption := CustomMessage('UpdateProgressHeading');
    WizardForm.PageDescriptionLabel.Caption :=
      CustomMessage('UpdateProgressDescription');
  end
  else if IsUpdateMode and (CurPageID = wpPreparing) then
  begin
    HeaderStepLabel.Caption := CustomMessage('UpdateStepProgress');
    WizardForm.PageNameLabel.Caption := CustomMessage('UpdateProgressHeading');
    WizardForm.PageDescriptionLabel.Caption :=
      CustomMessage('UpdatePhasePreparing');
  end
  else if IsUpdateMode and (CurPageID = wpFinished) then
  begin
    HeaderStepLabel.Caption := CustomMessage('UpdatePhaseRelaunch');
    WizardForm.FinishedHeadingLabel.Caption :=
      CustomMessage('UpdateFinishedHeading');
    WizardForm.FinishedLabel.Caption :=
      Format(CustomMessage('UpdateFinishedVersion'), [ProductVersionText]) + #13#10 +
      CustomMessage('UpdateFinishedPreserved');
    FinishedStateLabel.Caption := CustomMessage('UpdatePhaseRelaunch');
    WizardForm.RunList.Visible := False;
  end
  else if CurPageID = wpSelectDir then
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
    if IsUpdateMode then
    begin
      if not PrepareUpdateRollback then
      begin
        Log('WOR-149 rollback preparation aborted update: ' +
          UpdateBackupFailure + '.');
        RaiseException(CustomMessage('UpdateBackupError'));
      end;
      StageLabel.Caption := Format(CustomMessage('UpdatePhaseFiles'), [0]);
      AddDetail(CustomMessage('UpdateDetailCopying'));
    end
    else
    begin
      StageLabel.Caption := Format(CustomMessage('PhaseInstalling'), [0]);
      AddDetail(CustomMessage('DetailCopying'));
    end;
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
      if IsUpdateMode then
        StageLabel.Caption := CustomMessage('UpdatePhaseIntegration')
      else
        StageLabel.Caption := CustomMessage('PhaseConfiguring');
      if IsUpdateMode then
        AddDetail(CustomMessage('UpdateDetailIntegration'))
      else
        AddDetail(CustomMessage('DetailIntegration'));
      if (not Exec(AppExe, '--osia-install-integration', '', SW_HIDE,
        ewWaitUntilTerminated, ResultCode)) or (ResultCode <> 0) then
      begin
        Exec(AppExe, '--osia-remove-integration', '', SW_HIDE,
          ewWaitUntilTerminated, ResultCode);
        RestorePreviousAutostart;
        if IsUpdateMode then
          RaiseException(CustomMessage('UpdateIntegrationError'))
        else
          RaiseException(CustomMessage('IntegrationError'));
      end;
      IntegrationInstalled := True;
    end;

    AutoCommand := '"' + AppExe + '" --wake';
    if IsUpdateMode then
      RestorePreviousAutostart
    else if WizardIsTaskSelected('autostart') then
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
    if IsUpdateMode then
    begin
      StageLabel.Caption := CustomMessage('UpdatePhaseFinalizing');
      AddDetail(CustomMessage('UpdateDetailComplete'));
    end
    else
    begin
      StageLabel.Caption := CustomMessage('PhaseFinalizing');
      AddDetail(CustomMessage('DetailComplete'));
    end;
    if FreshInstallOnboardingEligible and (not IsUpdateMode) and
      (not IntegrationSuppressed) then
    begin
      ForceDirectories(OsiaProfileDirectory);
      if not SaveStringToFile(
        AddBackslash(OsiaProfileDirectory) + 'onboarding.json',
        '{"schemaVersion":1,"status":"pending"}' + #13#10, False) then
        Log('WOR-151 onboarding marker could not be written.');
    end;
  end;

  if CurStep = ssDone then
  begin
    InstallSucceeded := True;
    if UpdateBackupPrepared then
    begin
      DelTree(UpdateBackupInternal, True, True, True);
      DeleteFile(UpdateBackupExe);
      UpdateBackupPrepared := False;
      Log('WOR-149 rollback: obsolete local backup removed after success.');
    end;
  end;
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
  { InitializeSetup can reject a requested update before the app path exists. No
    mutation has happened in that state, so teardown must be a no-op. }
  if UpdateModeRequested and (not IsUpdateMode) then
    exit;
  if IsUpdateMode then
    AppExe := AddBackslash(ExistingInstallDir) + 'OSIA.exe'
  else
    AppExe := ExpandConstant('{app}\OSIA.exe');
  if IntegrationInstalled and FileExists(AppExe) then
    Exec(AppExe, '--osia-remove-integration', '', SW_HIDE,
      ewWaitUntilTerminated, ResultCode);
  if IsUpdateMode and UpdateBackupPrepared then
  begin
    Log('WOR-149 rollback: restoring the previous local payload.');
    DelTree(AddBackslash(ExistingInstallDir) + '_internal', True, True, True);
    DeleteFile(AppExe);
    if not RenameFileWithRetry(UpdateBackupInternal,
      AddBackslash(ExistingInstallDir) + '_internal') then
      Log('WOR-149 rollback restore failed: payload rename failed.')
    else if not CopyFileWithRetry(UpdateBackupExe, AppExe) then
      Log('WOR-149 rollback restore failed: launcher copy failed.')
    else
      Log('WOR-149 rollback: previous installation restored successfully.');
    DeleteFile(UpdateBackupExe);
    UpdateBackupPrepared := False;
  end;
  if ExistingIntegrationStopped and FileExists(AppExe) then
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
