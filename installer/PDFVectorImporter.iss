#define MyAppName "PDF Vector Importer for FreeCAD"
#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\\PDFVectorImporter"
#endif

[Setup]
AppId={{35D2F41F-0EA4-4C80-8480-7B6C2ADAC327}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=BlueCollar Systems
AppPublisherURL=https://github.com/BlueCollar-Systems/FC-PDFimporter
; FreeCAD 1.1+ uses a versioned profile (v1-1). Legacy 0.21 uses FreeCAD\Mod\.
DefaultDirName={userappdata}\FreeCAD\v1-1\Mod\PDFVectorImporter
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=PDFVectorImporter_Setup_v{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
LicenseFile={#SourceDir}\LICENSE
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Code]
function InitializeSetup: Boolean;
var
  FreeCadProfileDir: string;
  ContinueInstall: Integer;
begin
  FreeCadProfileDir := ExpandConstant('{userappdata}\FreeCAD');
  if not DirExists(FreeCadProfileDir) then
  begin
    ContinueInstall := MsgBox(
      'FreeCAD user profile folder was not found at:' + #13#10 + FreeCadProfileDir + #13#10#13#10 +
      'Install FreeCAD first, then run this installer again.' + #13#10#13#10 +
      'Continue anyway?',
      mbConfirmation, MB_YESNO
    );
    Result := ContinueInstall = IDYES;
    exit;
  end;

  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    MsgBox(
      'PDF Vector Importer was installed to:' + #13#10 + ExpandConstant('{app}') + #13#10#13#10 +
      'Restart FreeCAD. If PyMuPDF is missing, install it from FreeCAD Addon Manager.',
      mbInformation, MB_OK
    );
  end;
end;
