; Inno Setup script for Atelier PDM - run build.bat rather than this directly.
#define AppName "Atelier PDM"
#define AppVersion "2.9.5"
#define AppExe "Atelier PDM.exe"
#ifndef DistDir
  #define DistDir "dist\Atelier PDM"
#endif

[Setup]
AppId={{8C1F3B52-6A1E-4C7B-9C55-2F4E7A9D1A26}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Luc Davenport
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=Output
OutputBaseFilename=AtelierPDM-Setup-{#AppVersion}
SetupIconFile=atelier.ico
UninstallDisplayIcon={app}\icons\vault_app.ico
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Tasks]
Name: "solidcraft"; Description: "SolidCraft with the Atelier panel"; GroupDescription: "Shortcuts:"
Name: "physcad"; Description: "PhysCAD with the Atelier panel"; GroupDescription: "Shortcuts:"
Name: "cam"; Description: "Atelier CAM && Tools (NC programs, tool libraries, machines)"; GroupDescription: "Shortcuts:"
Name: "desktopicon"; Description: "Put the shortcuts on the desktop too"; GroupDescription: "Desktop:"; Flags: unchecked

[Files]
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "atelier.ico"; DestDir: "{app}\icons"; Flags: ignoreversion
Source: "atelier_solidcraft.ico"; DestDir: "{app}\icons"; Flags: ignoreversion
Source: "atelier_physcad.ico"; DestDir: "{app}\icons"; Flags: ignoreversion
Source: "atelier_cam.ico"; DestDir: "{app}\icons"; Flags: ignoreversion
Source: "atelier_solidcraft.ico"; DestDir: "{app}\icons"; DestName: "vault_solidcraft.ico"; Flags: ignoreversion
Source: "atelier_physcad.ico"; DestDir: "{app}\icons"; DestName: "vault_physcad.ico"; Flags: ignoreversion
Source: "atelier_cam.ico"; DestDir: "{app}\icons"; DestName: "vault_cam.ico"; Flags: ignoreversion
Source: "atelier.ico"; DestDir: "{app}\icons"; DestName: "vault_app.ico"; Flags: ignoreversion

[Icons]
; Each shortcut has its own icon and taskbar ID (matching the launcher), so they never share a taskbar button.
Name: "{group}\Atelier PDM"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\icons\vault_app.ico"; AppUserModelID: "AtelierPDM.App"
Name: "{group}\SolidCraft with Atelier"; Filename: "{app}\{#AppExe}"; Parameters: "--panel solidcraft --launch"; IconFilename: "{app}\icons\vault_solidcraft.ico"; AppUserModelID: "AtelierPDM.Panel.SolidCraft"; Tasks: solidcraft
Name: "{group}\PhysCAD with Atelier"; Filename: "{app}\{#AppExe}"; Parameters: "--panel physcad --launch"; IconFilename: "{app}\icons\vault_physcad.ico"; AppUserModelID: "AtelierPDM.Panel.PhysCAD"; Tasks: physcad
Name: "{group}\Atelier CAM & Tools"; Filename: "{app}\{#AppExe}"; Parameters: "--cam"; IconFilename: "{app}\icons\vault_cam.ico"; AppUserModelID: "AtelierPDM.CAM"; Tasks: cam
Name: "{group}\Atelier PDM setup"; Filename: "{app}\{#AppExe}"; Parameters: "--setup"; IconFilename: "{app}\icons\vault_app.ico"; AppUserModelID: "AtelierPDM.App"
Name: "{group}\Uninstall Atelier PDM"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Atelier PDM"; Filename: "{app}\{#AppExe}"; IconFilename: "{app}\icons\vault_app.ico"; AppUserModelID: "AtelierPDM.App"; Tasks: desktopicon
Name: "{autodesktop}\SolidCraft with Atelier"; Filename: "{app}\{#AppExe}"; Parameters: "--panel solidcraft --launch"; IconFilename: "{app}\icons\vault_solidcraft.ico"; AppUserModelID: "AtelierPDM.Panel.SolidCraft"; Tasks: desktopicon and solidcraft
Name: "{autodesktop}\PhysCAD with Atelier"; Filename: "{app}\{#AppExe}"; Parameters: "--panel physcad --launch"; IconFilename: "{app}\icons\vault_physcad.ico"; AppUserModelID: "AtelierPDM.Panel.PhysCAD"; Tasks: desktopicon and physcad
Name: "{autodesktop}\Atelier CAM & Tools"; Filename: "{app}\{#AppExe}"; Parameters: "--cam"; IconFilename: "{app}\icons\vault_cam.ico"; AppUserModelID: "AtelierPDM.CAM"; Tasks: desktopicon and cam

[Run]
Filename: "{sys}\ie4uinit.exe"; Parameters: "-show"; Flags: runhidden nowait skipifdoesntexist
Filename: "{app}\{#AppExe}"; Parameters: "--setup"; Description: "Set up Atelier PDM now"; Flags: postinstall nowait skipifsilent; Check: NotSetUp
Filename: "{app}\{#AppExe}"; Description: "Open Atelier PDM"; Flags: postinstall nowait skipifsilent; Check: IsSetUp

; The vault, your working folder and %LOCALAPPDATA%\Atelier PDM are never removed by the uninstaller.

[Code]
function IsSetUp: Boolean;
begin
  Result := FileExists(ExpandConstant('{localappdata}\Atelier PDM\install.json'));
end;

function NotSetUp: Boolean;
begin
  Result := not IsSetUp;
end;
