# Compatibility — PDF Vector Importer (FreeCAD)

**Canonical path:** `C:\1PDF-Importer-FreeCAD`  
Modes are extraction **strategy** (Auto / Vector / Raster / Hybrid), not quality tiers.

---

## Minimum host version

**A FreeCAD that bundles Python 3.10 or newer**: FreeCAD 1.0+, or a 0.21 build
with Python 3.10+. `package.xml` still declares `<freecadmin>0.21</freecadmin>`
so those 0.21 builds can load it; the official 0.21 Windows installer ships
Python 3.8 and is not supported yet.

## Oldest tested

| Host | Status |
|------|--------|
| FreeCAD 1.1.x | ✅ Tested by hand on 1.1.4 (no automated FreeCAD test yet) |
| FreeCAD 1.0.x | ⚠️ Expected (not tested) |
| FreeCAD 0.21.x | ⚠️ Only builds that bundle Python 3.10+; the official Windows installer (Python 3.8) is not supported yet |
| FreeCAD 26.x (calendar version numbers) | ⚠️ Not yet tested |
| FreeCAD 0.20 and earlier | ❌ Not supported (needs Python 3.10+) |

## Ruby / Python ABI

| Runtime | Notes |
|---------|-------|
| **Python 3.10 / 3.11** | Maintained Windows offline bundle (exact ABI selected at runtime) |
| Python 3.12+ | Source/system packages only; no bundled offline payload |
| Python 3.8–3.9 | Not supported (the importer needs Python 3.10+); CI only byte-compiles on them |
| Ruby | Not used |

Embedded Python comes from the installed FreeCAD build. Release installs bundle PyMuPDF once under `src/lib/common` and fontTools separately under `src/lib/cp310` and `src/lib/cp311`. The incompatible sibling tree is never added to `sys.path`.

## Bundled dependencies

| Dependency | Release installer | Source checkout |
|------------|-------------------|-----------------|
| PyMuPDF 1.28.2 | ✅ Shared cp310-abi3 Windows wheel | Switching to the workbench offers to install it, or a system/user package |
| fontTools 4.63.0 | ✅ Exact cp310 and cp311 Windows wheels | Same workbench install offer |
| Poppler / pdfcadcore | ✅ In workbench | Same |

No system Python, pip, or OS packages required for release users.

PyMuPDF 1.28.2 is the minimum supported source runtime. It fixes the repeated
`Page.get_texttrace()` reference-ownership defect that can terminate Python 3.10
and 3.11 during batch extraction or shutdown ([upstream #5042](https://github.com/pymupdf/PyMuPDF/issues/5042)).
Restart FreeCAD after updating a loaded dependency; do not reuse the old native module.
Release validation exercises repeated text-trace extraction in both bundled ABIs
and requires each interpreter to exit normally.

## Legacy hardware notes

- Large multi-page PDFs: import page ranges on **&lt; 8 GB RAM** machines; see `import_report.extra.performance_hint`.
- **3D Text** (the default) gives the closest look to the PDF, as solid letter shapes that cannot be re-typed. On a slow PC, **Text** or **Labels** imports a text-heavy sheet many times faster, and the words stay editable.
- **Glyphs/Geometry** text modes increase sketch complexity, so avoid them on weak PCs unless exact outlines are required.
- Windows SmartScreen may warn — installer is unsigned but functional.

## Offline install

Release **Inno Setup EXE** works without internet after download on embedded CPython 3.10/3.11. Dev/source installs and other Python versions may accept the workbench's dependency install offer once, with network access.

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
| 1.1.x | 3.11 | 1.28.2 offline bundle | ✅ Tested by hand on 1.1.4 (no automated FreeCAD test yet) |
| 1.0.x | 3.11 | 1.28.2 offline bundle | ⚠️ Expected (not tested) |
| 0.21.x | 3.10+ builds only | 1.28.2 offline bundle on 3.10 | ⚠️ Only builds that bundle Python 3.10+; the official Windows installer (Python 3.8) is not supported yet |
| 26.x | not checked | | ⚠️ Not yet tested |
| Any host using 3.12+ | 3.12+ | System/user install only | ⚠️ No bundled offline runtime |
| 0.20 and earlier | 3.8–3.9 | | ❌ Not supported (needs Python 3.10+) |

### Text rendering

| Option | FreeCAD result |
|--------|----------------|
| **Text** | Editable Draft text objects |
| **Labels** | Editable Draft label objects |
| **3D Text** | Default; solid letter shapes from the PDF font (closest look; not re-typeable) |
| **Glyphs** | Vector glyph geometry (not editable as words) |
| **Geometry** | pdftocairo outlines (non-editable) |
| **Raster** | One picture patch per text item (not editable) |

## CI coverage

GitHub Actions: Python **3.8–3.12**, Windows runtime-selector contracts on 3.10/3.11, dual-interpreter release smoke, `pdfcadcore_sync_check.py`, pytest, and BCS-ARCH mode smoke.
