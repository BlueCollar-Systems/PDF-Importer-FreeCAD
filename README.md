# PDF Vector Importer for FreeCAD

**BUILT. NOT BOUGHT.**

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Version: 4.0.40](https://img.shields.io/badge/Version-4.0.40-blue.svg)
![Platform: FreeCAD 0.21+](https://img.shields.io/badge/Platform-FreeCAD%200.21%2B-orange.svg)

Import vector geometry, text, and images from PDF files into FreeCAD as editable Part objects.

Arc reconstruction, dash mapping, color grouping, OCG layer support, and reference-based scaling -- all powered by pure-Python PDF parsing via PyMuPDF.

> BlueCollar Systems -- BUILT. NOT BOUGHT.

## Structural Steel Shape Assets

The former standalone `Structural-Steel-DXF-DWG-Shapes` repository has been
consolidated here under `steel_shapes/` so the FreeCAD
importer repo is the source home for the DXF/DWG steel shape packs. The
versioned release ZIP from that old repo is intentionally not stored here;
GitHub Releases remain the download layer, while this repo keeps the source
assets, generation scripts, checksums, license, and notes.

## Key Features

| Category | Capability |
|----------|-----------|
| PDF Parsing | PyMuPDF-powered vector extraction with full path, text, and image support |
| Import Modes | Auto (default), Vector, Raster, Hybrid — every mode targets maximum fidelity (BCS-ARCH-001) |
| Text Rendering | Labels, 3D Text, Glyphs, Geometry — orthogonal to mode |
| Arc Reconstruction | Kasa algebraic circle fit converts polyline segments back to true arcs |
| Layer Support | OCG layers (PDF Optional Content Groups) map to FreeCAD groups |
| Color Grouping | Geometry automatically organized by stroke/fill color |
| Dash Patterns | PDF dash arrays mapped to FreeCAD line styles |
| Text Import | PDF text extracted with font size, position, and rotation |
| Image Import | Embedded raster images extracted and placed at correct coordinates |
| Scale Detection | Reference-based scaling from known dimensions on the drawing |
| Steel Detection | Recognizes common structural steel shape profiles |

## Installation

See **[INSTALL.md](INSTALL.md)** for Windows FreeCAD 1.1 paths, dev junction install, and troubleshooting.

**FreeCAD 1.1 Mod path:** `%APPDATA%\FreeCAD\v1-1\Mod\PDFVectorImporter` (not legacy `FreeCAD\Mod\`).

**Dev one-liner (junction to repo):**
```powershell
.\installer\install-dev.ps1
```

### From FreeCAD Addon Manager (Recommended)
1. Open FreeCAD → **Tools** → **Addon Manager**
2. Search for **PDF Vector Importer**
3. Click **Install**
4. Restart FreeCAD

### Windows Setup.exe (Easy Manual Install)
1. Download `FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe` from Releases.
2. Close FreeCAD.
3. Run the installer (no admin rights required).
4. Restart FreeCAD.

### Manual Installation
1. Clone this repository:
   ```bash
   git clone https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD.git
   ```
2. Copy the `PDFVectorImporter` folder into your FreeCAD Mod directory:
   - **Windows (FreeCAD 1.1):** `%APPDATA%\FreeCAD\v1-1\Mod\`
   - **Windows (FreeCAD 0.21):** `%APPDATA%\FreeCAD\Mod\`
   - **macOS:** `~/Library/Application Support/FreeCAD/Mod/`
   - **Linux:** `~/.local/share/FreeCAD/Mod/`
3. For release ZIP/Setup installs, PyMuPDF is already bundled under `PDFVectorImporter/src/lib`. For source checkouts, run `python build_release.py` to stage the private runtime, or use **PDF Vector Importer > Install / Update PyMuPDF** after loading the workbench.
4. Restart FreeCAD

## Building Release Artifacts

### Build Addon ZIP
```bash
python build_release.py
```

### Build Windows Installer (.exe)
1. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php)
2. Run:
   ```bash
   python build_windows_installer.py
   ```
3. Output files are written to `dist/`:
   - `FreeCAD-PDF-Importer_vX.Y.Z.zip`
   - `FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe`

### Auto-Build on GitHub Releases
1. Push a tag in `vX.Y.Z` format (example: `v3.5.1`).
2. GitHub Actions workflow `windows-release` builds both artifacts.
3. The workflow attaches the ZIP and Setup.exe to that GitHub Release.

## Free Structural Steel Shapes (CC0)

This repository also hosts the public-domain AISC v16.0 DXF/DWG shape packs
previously distributed from `Structural-Steel-DXF-DWG-Shapes`.

| Location | Contents |
|----------|----------|
| [`steel_shapes/dxf/`](steel_shapes/dxf/) | 14 family DXF packs |
| [`steel_shapes/dwg/`](steel_shapes/dwg/) | 14 family DWG packs |
| [`steel_shapes/source/`](steel_shapes/source/) | AISC CSV + generation scripts |
| [`steel_shapes/README.md`](steel_shapes/README.md) | Usage, license, checksum notes |
| [`steel_shapes/ATTRIBUTION.md`](steel_shapes/ATTRIBUTION.md) | Merge provenance from the former standalone repo |

**Releases:** tag `steel-v1.0.0` (etc.) to publish
`Structural-Steel-DXF-DWG-Shapes-*.zip` via the `steel-shapes-release` workflow.
PDF Importer addon releases continue to use `v4.x.x` tags.

## Usage

1. Open FreeCAD
2. Go to **File** → **Import** or use the **PDF Vector Importer** workbench
3. Select a PDF file
4. Choose an import **mode** (Auto is the default and works for most files)
5. Choose a **text rendering** option
6. Click **Import**

## Import Modes (BCS-ARCH-001)

Every mode targets **indistinguishable-from-source** fidelity within FreeCAD's
capabilities. Modes differ only in extraction *strategy* for different input
types, not in quality tier.

| Mode | When to Use |
|------|-------------|
| **Auto** *(default)* | Let the importer analyze the PDF and pick the right strategy per page. Reports what it chose. |
| **Vector** | Clean vector PDFs (CAD exports, shop drawings, engineering drawings). |
| **Raster** | Scanned or image-only PDFs. Places the page as a high-DPI image. |
| **Hybrid** | Mixed content: vectors where clean, raster where vector extraction would be lossy. |

## Text Rendering (orthogonal to mode)

| Option | Result |
|--------|--------|
| **Labels** | FreeCAD-native text objects, editable as text |
| **3D Text** | Extruded geometric text (Draft ShapeString) |
| **Glyphs** | Per-character vector glyphs |
| **Geometry** | Text converted to non-editable geometry |

Plus a separate **Import text** toggle to skip text entirely.
Glyphs and Geometry prefer Poppler/pdftocairo SVG output when available, then fall back to bundled PyMuPDF SVG paths before degrading to Labels.

## Compatibility

See **[COMPATIBILITY.md](COMPATIBILITY.md)** for the full matrix. Summary:

| FreeCAD Version | Python | PyMuPDF | Status |
|----------------|--------|---------|--------|
| 0.21.x | 3.10+ | >=1.24,<2.0 | ⚠️ Expected |
| 1.0.x | 3.11+ | >=1.24,<2.0 | ⚠️ Expected |
| 1.1.x | 3.11+ | >=1.24,<2.0 | ⚠️ Expected |
| 0.19–0.20 | 3.8–3.9 | legacy pin | ⚠️ Expected only after legacy branch testing |
| 0.18 and earlier | | | ❌ Not supported |

Evidence levels:
- `✅ Verified`: host-run validation evidence captured.
- `⚠️ Expected`: syntax/runtime compatible but no host-run evidence yet.
- `❌ Not supported`: outside maintained/tested compatibility scope.

## Requirements

- **FreeCAD** 0.21 or later
- **Python** 3.10+ (adapters use PEP 604 union types)
- **PyMuPDF** `>=1.24,<2.0` (bundled in release ZIP/Setup installs under `PDFVectorImporter/src/lib`; source checkouts can stage it with `python build_release.py` or the workbench installer). When Poppler/pdftocairo is absent, bundled PyMuPDF also backs Glyphs/Geometry text rendering.

## Known Limitations

| Limitation | Details |
|-----------|---------|
| Encrypted PDFs | Password-protected PDFs must be unlocked before import |
| Compression filters | Decoding is delegated to PyMuPDF. Malformed or non-standard compressed object streams may fail to parse |
| Raster-only scans | Pure raster PDFs produce no vector geometry |
| Clipped/XObject-heavy PDFs | Complex clip stacks and deeply nested form XObjects can produce partial geometry |
| Very large PDFs | Documents with >10,000 primitives may slow the import process |
| Embedded subset fonts | Text using embedded subset fonts may not render correctly |
| OCG layer assignment | Extractor-level OCG mapping is validated on corpus `layered_ocg.pdf`; FreeCAD host-run grouping verification is still required in target runtime |
| Legacy hosts | FreeCAD versions older than 0.21 are not part of current validation coverage |

## Import report / scale trust

Imports write `<output>_import_report.json` with `extra.resolved_scale` when detected.

- Use `factor` for scaling **only when** `confidence >= 0.70` **and** `fallback_reason` is not `no_scale_detected`.
- Otherwise treat scale as unknown.

## Bad-PDF open gate

FreeCAD refuses bad PDFs at open time (**fail closed**). SketchUp uses the same user-facing messages but may proceed on rare gate-internal errors (**fail open**). Compare `fallback.reason` per host rather than assuming identical refusal behavior.

## License

MIT License — see [LICENSE](LICENSE) for details.

Copyright (c) 2024-2026 BlueCollar Systems
