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
AppPublisherURL=https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD
; Placeholder only — [Code] resolves the real Mod path at runtime.
DefaultDirName={userappdata}\FreeCAD\v1-1\Mod\PDFVectorImporter
DisableDirPage=yes
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename=FreeCAD-PDF-Importer-Setup_v{#MyAppVersion}
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
UsePreviousAppDir=no
; Keep uninstall metadata off the FreeCAD Mod path (avoids junction traversal).
UninstallFilesDir={userappdata}\BlueCollar Systems\PDF Vector Importer Installer
LicenseFile={#SourceDir}\LICENSE
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Code]
const
  INVALID_HANDLE_VALUE = THandle(-1);
  FILE_FLAG_BACKUP_SEMANTICS = $02000000;
  OPEN_EXISTING = 3;
  FILE_SHARE_READ = 1;
  FILE_SHARE_WRITE = 2;
  FILE_NAME_NORMALIZED = 0;
  FILE_ATTRIBUTE_REPARSE_POINT = $00000400;

var
  ResolvedInstallDir: string;

function CreateFileW(lpFileName: String; dwDesiredAccess, dwShareMode: DWORD;
  lpSecurityAttributes: Integer; dwCreationDisposition, dwFlagsAndAttributes: DWORD;
  hTemplateFile: THandle): THandle;
  external 'CreateFileW@kernel32.dll stdcall setuponly';

function GetFinalPathNameByHandleW(hFile: THandle; lpszFilePath: String;
  cchFilePath: DWORD; dwFlags: DWORD): DWORD;
  external 'GetFinalPathNameByHandleW@kernel32.dll stdcall setuponly';

function CloseHandle(hObject: THandle): LongBool;
  external 'CloseHandle@kernel32.dll stdcall setuponly';

function GetFileAttributesW(lpFileName: String): Integer;
  external 'GetFileAttributesW@kernel32.dll stdcall setuponly';

function StripExtendedPathPrefix(const Path: string): string;
begin
  Result := Path;
  if (Length(Result) >= 8) and (Copy(Result, 1, 8) = '\\?\UNC\') then
  begin
    Delete(Result, 1, 7);
    Insert('\\', Result, 1);
  end
  else if (Length(Result) >= 4) and (Copy(Result, 1, 4) = '\\?\') then
    Delete(Result, 1, 4);
end;

function ResolveRealPath(const Path: string): string;
var
  Handle: THandle;
  Buf: String;
  Len: DWORD;
begin
  Result := Path;
  if Path = '' then
    Exit;

  Handle := CreateFileW(Path, 0, FILE_SHARE_READ or FILE_SHARE_WRITE, 0,
    OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, 0);
  if Handle = INVALID_HANDLE_VALUE then
    Exit;

  try
    SetLength(Buf, 2048);
    Len := GetFinalPathNameByHandleW(Handle, Buf, 2048, FILE_NAME_NORMALIZED);
    if (Len > 0) and (Len < 2048) then
    begin
      SetLength(Buf, Len);
      Result := StripExtendedPathPrefix(Buf);
    end;
  finally
    CloseHandle(Handle);
  end;
end;

function IsReparsePoint(const Path: string): Boolean;
var
  Attributes: Integer;
begin
  Attributes := GetFileAttributesW(Path);
  Result := (Attributes <> -1) and ((Attributes and FILE_ATTRIBUTE_REPARSE_POINT) <> 0);
end;

function RemoveJunctionOrLink(const Path: string): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(ExpandConstant('{cmd}'), '/C rmdir /Q "' + Path + '"',
    '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function ConfirmRemoveReparsePoint(const Path: string): Boolean;
begin
  Result := MsgBox(
    'A junction or symlink was found on the install path:' + #13#10 + Path + #13#10#13#10 +
    'Windows blocks installers from traversing untrusted junctions (error 448).' + #13#10 +
    'The installer will remove this link and continue with a normal folder install.',
    mbConfirmation, MB_OKCANCEL) = IDOK;
end;

function ClearReparsePointsUpToFreeCadRoot(const Path: string): Boolean;
var
  Current, FreeCadRoot: string;
begin
  Result := True;
  FreeCadRoot := ExpandConstant('{userappdata}\FreeCAD');
  Current := Path;

  while (CompareText(Current, FreeCadRoot) <> 0) and (Length(Current) > 3) do
  begin
    if DirExists(Current) and IsReparsePoint(Current) then
    begin
      if not ConfirmRemoveReparsePoint(Current) then
      begin
        Result := False;
        Exit;
      end;

      if not RemoveJunctionOrLink(Current) then
      begin
        MsgBox(
          'Could not remove the existing junction/link at:' + #13#10 + Current + #13#10#13#10 +
          'Remove it manually, then run the installer again.',
          mbError, MB_OK);
        Result := False;
        Exit;
      end;
    end;

    Current := ExtractFileDir(Current);
  end;
end;

function DetectFreeCadModDir(): string;
var
  UserAppData, V11Mod, LegacyMod: string;
begin
  UserAppData := ExpandConstant('{userappdata}');
  V11Mod := UserAppData + '\FreeCAD\v1-1\Mod';
  LegacyMod := UserAppData + '\FreeCAD\Mod';

  if DirExists(V11Mod) then
    Result := V11Mod
  else if DirExists(LegacyMod) then
    Result := LegacyMod
  else
    Result := V11Mod;
end;

function EnsureRealDirectory(const Path: string): Boolean;
begin
  Result := True;
  if Path = '' then
  begin
    Result := False;
    Exit;
  end;

  if not ClearReparsePointsUpToFreeCadRoot(Path) then
  begin
    Result := False;
    Exit;
  end;

  if not DirExists(Path) then
  begin
    if not ForceDirectories(Path) then
    begin
      MsgBox(
        'Could not create install directory:' + #13#10 + Path,
        mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function PrepareInstallDirectory(): Boolean;
var
  ModDir, InstallDir: string;
begin
  ModDir := DetectFreeCadModDir();
  InstallDir := ModDir + '\PDFVectorImporter';

  if not EnsureRealDirectory(InstallDir) then
  begin
    Result := False;
    Exit;
  end;

  ResolvedInstallDir := ResolveRealPath(InstallDir);
  if ResolvedInstallDir = '' then
    ResolvedInstallDir := InstallDir;

  Result := True;
end;

function InitializeSetup: Boolean;
var
  FreeCadProfileDir: string;
  ContinueInstall: Integer;
begin
  if IsAdminInstallMode then
  begin
    MsgBox(
      'Do not run this installer as Administrator.' + #13#10#13#10 +
      'Elevated installs fail with error 448 when the FreeCAD Mod path contains ' +
      'junctions (common after install-dev.ps1 or 1PDF-Importer-* cleanup).' + #13#10#13#10 +
      'Close this window and run the installer normally (double-click, no "Run as administrator").',
      mbError, MB_OK);
    Result := False;
    Exit;
  end;

  FreeCadProfileDir := ExpandConstant('{userappdata}\FreeCAD');
  if not DirExists(FreeCadProfileDir) then
  begin
    ContinueInstall := MsgBox(
      'FreeCAD user profile folder was not found at:' + #13#10 + FreeCadProfileDir + #13#10#13#10 +
      'Install FreeCAD first, then run this installer again.' + #13#10#13#10 +
      'Continue anyway?',
      mbConfirmation, MB_YESNO
    );
    if ContinueInstall <> IDYES then
    begin
      Result := False;
      Exit;
    end;
  end;

  Result := PrepareInstallDirectory();
end;

procedure InitializeWizard;
begin
  if ResolvedInstallDir <> '' then
    WizardForm.DirEdit.Text := ResolvedInstallDir;
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
