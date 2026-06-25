# Compatibility — PDF Vector Importer (FreeCAD)

**Canonical path:** `C:\1PDF-Importer-FreeCAD`  
Modes are extraction **strategy** (Auto / Vector / Raster / Hybrid), not quality tiers.

---

## Minimum host version

**FreeCAD 0.21** (`package.xml` declares `<freecadmin>0.21</freecadmin>`).

## Oldest tested

| Host | Status |
|------|--------|
| FreeCAD 1.1.x | ✅ Verified (Windows installer smoke) |
| FreeCAD 1.0.x / 0.21.x | ⚠️ Expected |
| FreeCAD 0.19–0.20 | ⚠️ Expected only after legacy branch testing |
| FreeCAD 0.18 and earlier | ❌ Not supported |

## Ruby / Python ABI

| Runtime | Notes |
|---------|-------|
| **Python 3.10+** | Maintained runtime floor (FreeCAD 0.21+) |
| Python 3.8–3.9 | CI compile-only for legacy FreeCAD 0.19–0.20 hosts |
| Ruby | Not used |

Embedded Python comes from the installed FreeCAD build. Release installer bundles **PyMuPDF** under `Mod/PDFVectorImporter/src/lib`.

## Bundled dependencies

| Dependency | Release installer | Source checkout |
|------------|-------------------|-----------------|
| PyMuPDF (>=1.24, &lt;2.0) | ✅ Bundled | Workbench **Install / Update PyMuPDF** or `preflight_check.py --diagnostics` |
| Poppler / pdfcadcore | ✅ In workbench | Same |

No system Python, pip, or OS packages required for release users.

## Legacy hardware notes

- Large multi-page PDFs: import page ranges on **&lt; 8 GB RAM** machines; see `import_report.extra.performance_hint`.
- **Glyphs/Geometry** text modes increase sketch complexity — prefer **Labels** on weak PCs.
- Windows SmartScreen may warn — installer is unsigned but functional.

## Offline install

Release **Inno Setup EXE** from GitHub works without internet after download. Dev/source installs may run `preflight_check.py --install` once if wheels are not vendored.

## Enterprise / roaming

Workbench installs under `%APPDATA%\FreeCAD\…\Mod\`. Roaming profiles may break junction-based dev installs — use the release EXE for golden images.

## Preflight command

```powershell
cd C:\1PDF-Importer-FreeCAD
python preflight_check.py
python preflight_check.py --diagnostics
```

In FreeCAD GUI: select workbench **PDF Vector Importer** → verify toolbar **PDF Import** appears after install.

---

## FreeCAD version matrix

| FreeCAD | Python | PyMuPDF | Status |
|---------|--------|---------|--------|
| 1.1.x | 3.11+ | >=1.24,<2.0 | ⚠️ Expected |
| 1.0.x | 3.11+ | >=1.24,<2.0 | ⚠️ Expected |
| 0.21.x | 3.10+ | >=1.24,<2.0 | ⚠️ Expected |
| 0.19–0.20 | 3.8–3.9 | legacy pin | ⚠️ Expected only after legacy branch testing |
| 0.18 and earlier | | | ❌ Not supported |

### Text rendering

| Option | FreeCAD result |
|--------|----------------|
| **Labels** | Draft / native text objects |
| **3D Text** | ShapeString / extruded text |
| **Glyphs** | Vector glyph geometry |
| **Geometry** | pdftocairo outlines (non-editable) |

## CI coverage

GitHub Actions: Python **3.8–3.12**, `pdfcadcore_sync_check.py`, pytest, BCS-ARCH mode smoke.
