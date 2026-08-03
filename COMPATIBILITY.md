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
| **Python 3.10 / 3.11** | Maintained Windows offline bundle (exact ABI selected at runtime) |
| Python 3.12+ | Source/system packages only; no bundled offline payload |
| Python 3.8–3.9 | CI compile-only for legacy FreeCAD 0.19–0.20 hosts |
| Ruby | Not used |

Embedded Python comes from the installed FreeCAD build. Release installs bundle PyMuPDF once under `src/lib/common` and fontTools separately under `src/lib/cp310` and `src/lib/cp311`. The incompatible sibling tree is never added to `sys.path`.

## Bundled dependencies

| Dependency | Release installer | Source checkout |
|------------|-------------------|-----------------|
| PyMuPDF 1.28.0 | ✅ Shared cp310-abi3 Windows wheel | Workbench **Install / Update PDF Dependencies** or system/user package |
| fontTools 4.63.0 | ✅ Exact cp310 and cp311 Windows wheels | Same fallback command |
| Poppler / pdfcadcore | ✅ In workbench | Same |

No system Python, pip, or OS packages required for release users.

## Legacy hardware notes

- Large multi-page PDFs: import page ranges on **&lt; 8 GB RAM** machines; see `import_report.extra.performance_hint`.
- Use **3D Text** first for Adobe-like visual review. **Glyphs/Geometry** text modes increase sketch complexity, so avoid them on weak PCs unless exact outlines are required.
- Use **Labels** only when editable FreeCAD text matters more than model-space PDF appearance.
- Windows SmartScreen may warn — installer is unsigned but functional.

## Offline install

Release **Inno Setup EXE** works without internet after download on embedded CPython 3.10/3.11. Dev/source installs and other Python versions may use the workbench dependency command once with network access.

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
| 1.1.x | 3.11 | 1.28.0 offline bundle | ✅ Verified (Windows installer smoke) |
| 1.0.x | 3.11 | 1.28.0 offline bundle | ⚠️ Expected |
| 0.21.x | 3.10 | 1.28.0 offline bundle | ⚠️ Expected |
| Any host using 3.12+ | 3.12+ | System/user install only | ⚠️ No bundled offline runtime |
| 0.19–0.20 | 3.8–3.9 | legacy pin | ⚠️ Expected only after legacy branch testing |
| 0.18 and earlier | | | ❌ Not supported |

### Text rendering

| Option | FreeCAD result |
|--------|----------------|
| **3D Text** | Default visual-parity path; ShapeString / extruded text |
| **Labels** | Editable Draft / native text objects |
| **Glyphs** | Vector glyph geometry |
| **Geometry** | pdftocairo outlines (non-editable) |

## CI coverage

GitHub Actions: Python **3.8–3.12**, Windows runtime-selector contracts on 3.10/3.11, dual-interpreter release smoke, `pdfcadcore_sync_check.py`, pytest, and BCS-ARCH mode smoke.
