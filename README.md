# PDF Vector Importer for FreeCAD

**BUILT. NOT BOUGHT.**

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Version: 4.0.70](https://img.shields.io/badge/Version-4.0.70-blue.svg)
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
| Text Rendering | Text, Labels, 3D Text (default visual-parity path), Glyphs, Geometry, Raster — six distinct requests orthogonal to page mode |
| Arc Reconstruction | Kasa algebraic circle fit converts polyline segments back to true arcs |
| Layer Support | OCG layers (PDF Optional Content Groups) map to FreeCAD groups |
| Color Grouping | Geometry automatically organized by stroke/fill color |
| Dash Patterns | PDF dash arrays mapped to FreeCAD line styles |
| Text Import | PDF text extracted with font size, position, and rotation |
| Image Import | Embedded raster images extracted and placed at correct coordinates |
| Scale Detection | Reference-based scaling from known dimensions on the drawing |
| Steel Detection | Recognizes common structural steel shape profiles |

For Adobe-like visual sign-off, start with **3D Text** at the same zoom/scale as
the source PDF. Use **Labels** when editable FreeCAD text matters more than
model-space PDF appearance, and use **Glyphs/Geometry** when exact outline
geometry is preferred over editability.

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
3. Release ZIP/Setup installs bundle PyMuPDF and fontTools under `PDFVectorImporter/src/lib`. For source checkouts, run `python build_release.py` to stage both private runtime dependencies, or use **PDF Vector Importer > Install / Update PDF Dependencies** after loading the workbench.
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

### Auto-Release Mint on `main` Pushes
The `auto-release` workflow mints a `vX.Y.Z` release automatically when a
push to `main` carries a version bump. Two deliberate guards apply:

- Docs-only pushes never mint: the workflow ignores `**/*.md`, `docs/**`,
  and archive paths (board Q-08-a / ANS-09-1).
- Commits marked `[skip release]` (docs/test/CI-only by convention) skip
  the release job entirely.

**Escape hatch (canonical):** if a release commit's only diff is markdown —
for example a README badge correction that must still ship as a release —
the push cannot trigger `auto-release`. Mint it manually instead:

```bash
gh workflow run auto-release.yml
```

The dispatched run re-reads the committed version, runs the full release
gates, and mints the tag exactly as a push-triggered run would (this is how
v4.0.67's badge correction shipped).

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
| **Text** | FreeCAD-native flat editable text objects |
| **Labels** | FreeCAD-native text objects, editable as text |
| **3D Text** | Extruded geometric text (Draft ShapeString) |
| **Glyphs** | Per-character vector glyphs |
| **Geometry** | Text converted to non-editable geometry |
| **Raster** | One source-bound raster patch per PDF text item |

Plus a separate **Import text** toggle to skip text entirely.
Glyphs and Geometry prefer Poppler/pdftocairo SVG output when available, then fall back to bundled PyMuPDF SVG paths.

### Text-representation contract (TEXTMODE-1)

**The requested text mode is the delivered text mode.** Alignment, rotation,
or scaling defects are fixed *inside* the requested mode — never by
substituting a different mode. Substitution is permitted only when the
requested mode is genuinely impossible for the exact source item. A generic
exception, a missing helper, an empty result, or a visual defect is not proof
of impossibility. Any authorized substitution must walk the closest remaining
representation first and is recorded per source item in `import_report.json`
with the attempted types, exact created/removed host IDs, cleanup result, and
the evidence that proved the requested type impossible. It is never silent.
(Owner directive 2026-07-13.)

Controller attempt order (requested rung first). Each later rung begins only
after affirmative item-specific impossibility proof and exact cleanup for the
preceding rung:

| Requested | Exact finite attempt order |
|-----------|--------|
| **Text** | Text → Labels → 3D Text → Glyphs → Geometry → Raster |
| **Labels** | Labels → Text → 3D Text → Glyphs → Geometry → Raster |
| **3D Text** | 3D Text → Glyphs → Geometry → Text → Labels → Raster |
| **Glyphs** | Glyphs → Geometry → 3D Text → Text → Labels → Raster |
| **Geometry** | Geometry → Glyphs → 3D Text → Text → Labels → Raster |
| **Raster** | Raster |

Notes:
- **Glyphs and Geometry are distinct deliverables.** Glyphs preserve one
  grouped outline object per placed character; Geometry exposes the raw edge
  entities. Sharing an SVG source does not make the host representations
  interchangeable.
- Renderer or font failures stop the transaction unless the failed source item
  has item-specific impossibility evidence and an implemented, verified next
  rung. They never authorize a whole-page or whole-mode substitution.
- Automatic raster classification may add a raster background, but it does not
  discard an explicitly requested text representation. Explicit Raster remains
  raster-only.
- Raster is the terminal verified attempt, not a guaranteed success label. If
  its source-bound pixels, placement, host entity, or persistence cannot be
  verified, the item is reported as failed.
- The invariant is "requested type delivered and verified, or an exact failed
  attempt is reported and the transaction stops, or a proof-gated per-item
  fallback is reported." It is locked by
  `tests/test_textmode1_invariant_fc.py` and
  `tests/test_freecad_representation_contract.py`.

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
- **fontTools** `>=4.50,<5.0` (bundled and installed through the same paths as PyMuPDF). It preserves embedded PDF font outlines and Unicode mappings for native 3D Text instead of substituting a visually similar font.

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
