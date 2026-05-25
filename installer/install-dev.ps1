# install-dev.ps1 — Link PDFVectorImporter into FreeCAD Mod (dev workflow)
# Run from repo root:  .\installer\install-dev.ps1
# Requires: FreeCAD 1.1+ (or set $FreeCADCmd to your FreeCADCmd.exe)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path $PSScriptRoot -Parent
$Source = Join-Path $RepoRoot "PDFVectorImporter"

if (-not (Test-Path (Join-Path $Source "package.xml"))) {
    throw "Workbench source not found: $Source"
}

$FreeCADCmd = $env:FREECAD_CMD
if (-not $FreeCADCmd) {
    $candidates = @(
        "C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe",
        "C:\Program Files\FreeCAD 0.21\bin\FreeCADCmd.exe"
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { $FreeCADCmd = $c; break }
    }
}
if (-not $FreeCADCmd -or -not (Test-Path $FreeCADCmd)) {
    throw "FreeCADCmd.exe not found. Set FREECAD_CMD to your FreeCADCmd.exe path."
}

$py = @"
import FreeCAD, os
print(os.path.join(FreeCAD.getUserAppDataDir(), 'Mod', 'PDFVectorImporter'))
"@

$Target = & $FreeCADCmd -c $py 2>$null | Select-Object -Last 1
$Target = $Target.Trim()
if (-not $Target) {
    throw "Could not resolve FreeCAD Mod path from $FreeCADCmd"
}

$ModDir = Split-Path $Target -Parent
New-Item -ItemType Directory -Path $ModDir -Force | Out-Null

if (Test-Path $Target) {
    $item = Get-Item $Target -Force
    if ($item.LinkType -eq "Junction" -and $item.Target -eq $Source) {
        Write-Host "Already linked: $Target -> $Source"
        exit 0
    }
    if ($item.LinkType -eq "Junction") {
        Remove-Item $Target -Force
    } else {
        throw "Path exists and is not a junction (remove manually): $Target"
    }
}

cmd.exe /c mklink /J "$Target" "$Source" | Out-Host
if (-not (Test-Path $Target)) {
    throw "Junction creation failed: $Target"
}

Write-Host ""
Write-Host "PDF Vector Importer installed (dev junction)."
Write-Host "  Source: $Source"
Write-Host "  Target: $Target"
Write-Host ""
Write-Host "Restart FreeCAD. Workbench: PDF Vector Importer"
Write-Host "Mod folder in FreeCAD: $ModDir"
