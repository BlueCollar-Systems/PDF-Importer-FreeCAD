# Installing PDF Vector Importer (FreeCAD)

**Version:** see `PDFVectorImporter/package.xml`  
**Tested:** FreeCAD **1.1.x** on Windows

## Why the workbench can disappear

FreeCAD **1.1** stores user data under a **versioned profile**, not the legacy `FreeCAD\Mod\` folder:

## Before you import (text modes & scale)

Professional import — maximum fidelity; Auto picks vector, raster, or hybrid per page.

- **Labels** = editable text (ShapeString / labels path).
- **Outlines / Glyphs / Geometry** = exact vector fidelity (not editable text).
- **3D text** = ShapeString extrusion where supported.
- Scale is detected from title blocks when possible. If `import_report.json` shows a **scale note** in `human_summary` or `extra.scale_crosscheck`, verify one known dimension before takeoff.

| FreeCAD | Mod folder (Windows) |
|---------|----------------------|
| **1.1.x** | `%APPDATA%\FreeCAD\v1-1\Mod\PDFVectorImporter` |
| 0.21 / older | `%APPDATA%\FreeCAD\Mod\PDFVectorImporter` |

If the workbench was only under `FreeCAD\Mod\` (or the folder was deleted), FreeCAD 1.1 will not load it. The workbench code is fine; the install path must match `FreeCAD.getUserAppDataDir()`.

## One-click dev install (recommended for this repo)

From a PowerShell prompt in the repo root:

```powershell
.\installer\install-dev.ps1
```

This script:

1. Runs `FreeCADCmd` to resolve the correct `Mod` path for your FreeCAD version.
2. Creates a **directory junction** from that path to `PDFVectorImporter` in the repo (live edits, no copy).

Then **restart FreeCAD**. Select workbench **PDF Vector Importer** from the workbench dropdown.

Optional: point at a different FreeCAD binary:

```powershell
$env:FREECAD_CMD = "D:\FreeCAD 1.1\bin\FreeCADCmd.exe"
.\installer\install-dev.ps1
```

## Manual install (copy)

1. Close FreeCAD.
2. Find your profile Mod directory:
   - **FreeCAD 1.1:** `%APPDATA%\FreeCAD\v1-1\Mod\`
   - **FreeCAD 0.21:** `%APPDATA%\FreeCAD\Mod\`
3. Copy the entire `PDFVectorImporter` folder from this repo into that `Mod` folder.
4. Restart FreeCAD.

## Windows Setup.exe (release build)

1. Download `FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe` from [Releases](https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD/releases).
2. Close FreeCAD.
3. Run the installer (no admin required).
4. Restart FreeCAD.

Build locally: `python build_windows_installer.py` (requires [Inno Setup 6](https://jrsoftware.org/isinfo.php)).

### Installer error 448 (untrusted mount point)

Windows blocks **elevated** processes from traversing junctions/symlinks created by a normal user (NTSTATUS `0xC00004BE`). This often appears after:

- `.\installer\install-dev.ps1` (creates a Mod junction to the repo), or
- cleanup of `1PDF-Importer-*` / dev junction folders under `%APPDATA%\FreeCAD\...\Mod\`.

**Fix (try in order):**

1. **Do not** use "Run as administrator" on the Setup.exe - the installer is built for `PrivilegesRequired=lowest`.
2. Remove any dev junction at your Mod path, then rerun Setup:
   ```powershell
   cmd /c rmdir "%APPDATA%\FreeCAD\v1-1\Mod\PDFVectorImporter"
   cmd /c rmdir "%APPDATA%\FreeCAD\Mod\PDFVectorImporter"
   ```
3. **Manual ZIP install:** download `FreeCAD-PDF-Importer_vX.Y.Z.zip`, extract `PDFVectorImporter` into `%APPDATA%\FreeCAD\v1-1\Mod\` (or `FreeCAD\Mod\` for 0.21).
4. **Developer Mode** (Settings -> Privacy & security -> For developers) can reduce symlink restrictions for dev workflows; release users should prefer a real folder copy under Mod.

Setup builds from this repo resolve/remove Mod junctions before copying files and store uninstall metadata outside the Mod tree.

## FreeCAD Addon Manager

1. **Tools → Addon Manager**
2. Search **PDF Vector Importer**
3. **Install** → restart FreeCAD

## PyMuPDF dependency

Release ZIPs and `FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe` are built with a
private **PyMuPDF** copy under:

`…\Mod\PDFVectorImporter\src\lib`

That means release users do not need system Python, pip, or any operating
system Python packages. The **Install / Update PyMuPDF** command remains as a
source/dev fallback if you install directly from a checkout or intentionally
build with `--no-vendor-deps`.

Manual fallback install:

```powershell
& "C:\Program Files\FreeCAD 1.1\bin\python.exe" -m pip install --target "%APPDATA%\FreeCAD\v1-1\Mod\PDFVectorImporter\src\lib" "PyMuPDF>=1.24,<2.0"
```

(Adjust `python.exe` and `lib` path for your FreeCAD version.)

## Verify installation

```powershell
& "C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe" -c "import FreeCAD, os; p=os.path.join(FreeCAD.getUserAppDataDir(),'Mod','PDFVectorImporter'); print('exists:', os.path.isdir(p), p)"
```

Expected: `exists: True` and a path ending in `v1-1\Mod\PDFVectorImporter` on FreeCAD 1.1.

In the GUI: workbench list should include **PDF Vector Importer**; toolbar **PDF Import**.

## Import report / scale trust

Each import can emit `import_report.json` (`bcs.import_report/1.1`) with `extra.resolved_scale`.

- Use `factor` for scaling **only when** `confidence >= 0.70` **and** `fallback_reason` is not `no_scale_detected`.
- Otherwise treat scale as unknown and set scale manually in FreeCAD.

## Bad-PDF open gate

FreeCAD refuses encrypted, non-PDF, and truncated files at open time (**fail closed**).
SketchUp shows the same messages but may **fail open** if the gate check itself errors.
Message parity is intentional; detection parity is not complete across hosts.

## Uninstall

- **Junction (dev):** `Remove-Item "$env:APPDATA\FreeCAD\v1-1\Mod\PDFVectorImporter"` (removes link only, not the repo).
- **Copy / installer:** delete the `PDFVectorImporter` folder under your profile `Mod` directory.
