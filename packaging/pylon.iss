; Inno Setup script for Pylon. Build the application first, then this:
;
;     uv run pyinstaller packaging/pylon.spec --noconfirm
;     iscc packaging/pylon.iss
;
; Produces packaging/Output/Pylon-Setup-<version>.exe.
;
; Decisions worth knowing about:
;
; INSTALLS PER USER, NOT PER MACHINE. PrivilegesRequired=lowest means no UAC prompt,
; no administrator, and no "do you want to allow this app to make changes" on a
; download people are already unsure about. It also puts the application somewhere the
; updater can replace without elevation.
;
; THE SETTINGS ARE NOT IN THE INSTALL DIRECTORY. They live in %LOCALAPPDATA%\Pylon
; (config.app_dir), so an upgrade that replaces the program folder cannot take
; somebody's show name, colour and stream key with it. The uninstaller leaves them
; alone for the same reason; removing them is offered, not assumed.

#define AppName "Pylon"
#define AppVersion "1.0.0"
#define AppPublisher "Pylon"
#define AppURL "https://github.com/YOURNAME/pylon"
#define AppExeName "Pylon.exe"

[Setup]
AppId={{8D4F2A61-3C7E-4B29-9F15-6E0A8C3D5B74}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=Output
OutputBaseFilename=Pylon-Setup-{#AppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; No UAC prompt: see the note at the top.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; The uninstaller and Add/Remove Programs entry get the real icon.
UninstallDisplayIcon={app}\{#AppExeName}
SetupIconFile=pylon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; The whole PyInstaller onedir tree. recursesubdirs picks up overlays\ with it, which
; is what the timing tower and the holding cards are served from.
Source: "..\dist\Pylon\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Check my setup"; Filename: "{app}\{#AppExeName}"; Parameters: "doctor"; Comment: "Check OBS, iRacing, the scenes and the ports"
Name: "{group}\Settings"; Filename: "{app}\{#AppExeName}"; Parameters: "config --edit"; Comment: "Open the settings file"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
; Offered, not forced. Someone installing an hour before a race wants to get on with
; it; someone installing now wants to see whether it works.
Filename: "{app}\{#AppExeName}"; Parameters: "doctor"; Description: "Check my setup now"; Flags: postinstall nowait skipifsilent
Filename: "{#AppURL}#getting-started"; Description: "Read the five-minute setup guide"; Flags: postinstall nowait shellexec skipifsilent unchecked

[UninstallDelete]
; PyInstaller and Python leave these behind; without this the program folder survives
; an uninstall holding nothing but cache.
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
// Offer to remove the settings, never assume it. Someone uninstalling to reinstall a
// newer build would otherwise lose their show name, their colour and their stream key
// with no warning at all.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Pylon');
    if DirExists(DataDir) then
      if MsgBox('Remove your Pylon settings and logs as well?' + #13#10 + #13#10 +
                DataDir + #13#10 + #13#10 +
                'Choose No if you are reinstalling and want to keep your show set up.',
                mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
