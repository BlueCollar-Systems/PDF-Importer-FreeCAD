# PDF Vector Importer for FreeCAD

**BUILT. NOT BOUGHT.**

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Version: 4.0.111](https://img.shields.io/badge/Version-4.0.111-blue.svg)
![Platform: FreeCAD 1.0+ (Python 3.10+)](https://img.shields.io/badge/Platform-FreeCAD%201.0%2B%20%28Python%203.10%2B%29-orange.svg)

Import vector geometry, text, and images from PDF files into FreeCAD as editable Part objects.

Arc reconstruction, dash mapping, color grouping, OCG layer support, and reference-based scaling -- all powered by pure-Python PDF parsing via PyMuPDF.

> BlueCollar-Systems -- BUILT. NOT BOUGHT.

## Install (Windows, about two minutes)

1. Download `FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe` from the [Releases page](https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD/releases).
2. Close FreeCAD, then double-click the Setup file to run it normally. Do **not** use "Run as administrator".
3. Start FreeCAD again.
4. Pick the **PDF Vector Importer** workbench from the workbench list.
5. Click **Import PDF Vector…** (or use **File → Import**), choose your PDF, and click **OK**.

Needs FreeCAD 1.0 or newer (a FreeCAD with Python 3.10 or newer). Tested by hand on FreeCAD 1.1.4.
Not listed in FreeCAD's Addon Manager yet. Other ways to install (ZIP, manual copy, developer link) are under [Installation](#installation) and in [INSTALL.md](INSTALL.md).

Change notes for each version are in [docs/CHANGELOG.md](docs/CHANGELOG.md).

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
| Text Rendering | Text, Labels, 3D Text (default), Glyphs, Geometry, Raster — orthogonal to mode |
| Arc Reconstruction | Kasa algebraic circle fit converts polyline segments back to true arcs |
| Layer Support | OCG layers (PDF Optional Content Groups) map to FreeCAD groups |
| Color Grouping | Geometry automatically organized by stroke/fill color |
| Dash Patterns | PDF dash arrays mapped to FreeCAD line styles |
| Text Import | PDF text extracted with font size, position, and rotation |
| Image Import | Embedded raster images extracted and placed at correct coordinates |
| Scale Detection | Reference-based scaling from known dimensions on the drawing |
| Steel Detection | Recognizes common structural steel shape profiles |

**3D Text** (the default) gives the closest look to the PDF: solid letter
shapes made from the PDF's own font. They cannot be re-typed. Use **Text** or
**Labels** when you need to edit or search the words, and **Glyphs/Geometry**
when you want exact outline geometry instead.

## Installation

See **[INSTALL.md](INSTALL.md)** for Windows FreeCAD 1.1 paths, dev junction install, and troubleshooting.

**FreeCAD 1.1 Mod path:** `%APPDATA%\FreeCAD\v1-1\Mod\PDFVectorImporter` (not legacy `FreeCAD\Mod\`).

**Dev one-liner (junction to repo):**
```powershell
.\installer\install-dev.ps1
```

### Not listed in FreeCAD's Addon Manager yet
Searching FreeCAD's Addon Manager will not find this importer yet (it is not listed there). Use Setup.exe on Windows, or the manual install below.

### Windows Setup.exe (Easy Manual Install)
1. Download `FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe` from [Releases](https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD/releases).
2. Close FreeCAD.
3. Run the installer normally (no admin rights needed; do not use "Run as administrator").
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
3. Release ZIP/Setup installs bundle an offline runtime matrix under `PDFVectorImporter/src/lib`: shared PyMuPDF in `common/` and ABI-specific fontTools in `cp310/` and `cp311/`. A source checkout without that runtime: switch to the **PDF Vector Importer** workbench and it offers to install PyMuPDF and fontTools for the current FreeCAD user. (The **Install / Update PDF Dependencies** command exists but is not on a menu yet.)
4. Restart FreeCAD

## Building Release Artifacts

### Build Addon ZIP
```bash
python build_release.py --python310 C:\path\to\python310.exe --python311 C:\path\to\python311.exe
```

Release builds are fail-closed and must run from a Git checkout. Every
shippable addon source file must be tracked and byte-identical to `HEAD`;
private PDF/CAD/model inputs, generated import reports, corpus folders, and
nested archives abort the build. Set the masked
`BCS_PRIVATE_RELEASE_DENYLIST_B64` environment value to the project-owned
base64 JSON denylist before a local build; automation reads it from the
`FREECAD_PRIVATE_RELEASE_DENYLIST_B64` repository secret and fails closed when
it is absent or malformed. The ignored `PDFVectorImporter/src/lib`
tree is deleted and rebuilt on every release build from the exact wheel hashes
in `requirements-release-common.lock` and the `cp310`/`cp311` locks, so an
importable but stale local runtime cannot enter the ZIP. Supply exact Windows
CPython 3.10 and 3.11 interpreters; the builder validates each before installing
its ABI-specific wheel. `--no-vendor-deps` intentionally
refuses release creation because ignored runtime bytes are not commit-bound.

The secret decodes to this schema; use only synthetic values in documentation:

```json
{"schema":"bcs.private-release-denylist/1.0","terms":["SYNTHETIC-PRIVATE-DRAWING-ID"]}
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

### Auto-Release Mint on `main` Pushes
The `auto-release` workflow mints a `vX.Y.Z` release automatically when a
push to `main` carries a version bump. The `auto-release` workflow builds and publishes both artifacts
atomically before the release becomes immutable. Two deliberate guards apply:

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

1. Open FreeCAD.
2. Use **File → Import**, or switch to the **PDF Vector Importer** workbench and click **Import PDF Vector…**.
3. Pick the PDF file (**Browse…**; File → Import has already picked it).
4. Pick a **Text Mode**. 3D Text is the default; pick **Text** or **Labels** if you need to edit the words. Untick **Import text** to leave text out.
5. Leave the import strategy on Auto; it works for most files. To force Vector, Raster or Hybrid, tick **Advanced** and choose an **Import strategy**.
6. Click **OK**.

To import several PDFs, select them all in **File → Import**; each one opens its own options dialog.

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
| **Text** | FreeCAD Draft text objects; the words stay editable |
| **Labels** | FreeCAD Draft label objects; the words stay editable |
| **3D Text** *(default)* | Solid letter shapes from the PDF font (closest look; not re-typeable) |
| **Glyphs** | Exact letter outlines from the PDF font, one compound per source text item (not editable as words) |
| **Geometry** | Raw text-outline edges grouped per source text item (not editable as words) |
| **Raster** | One picture patch per source text item that looks exactly like the PDF (not editable) |

Plus a separate **Import text** toggle to skip text entirely.
Glyphs and Geometry prefer Poppler/pdftocairo SVG output when available, then fall back to bundled PyMuPDF SVG paths.

### Text-representation contract (TEXTMODE-1)

**The requested text mode is the delivered text mode.** Alignment, rotation,
or scaling defects are fixed *inside* the requested mode — never by
substituting a different mode. Substitution is permitted only when the
requested mode is genuinely impossible for the exact source item. A generic
exception, a missing helper, an empty result, or a visual defect is not proof
of impossibility. An item whose requested representation failed *without* such
proof is still drawn at the next rung the ladder reaches, so that one item does
not cost the sheet, but that delivery is reported as degraded and is never
certified (owner directive 2026-09-19). Any authorized substitution must walk the closest remaining
representation first and is recorded per source item in `import_report.json`
with the attempted types, exact created/removed host IDs, cleanup result, and
the evidence that proved the requested type impossible. It is never silent.
(Owner directive 2026-07-13.)

Authorized order after item-specific proof (left rung first):

| Requested | Ladder |
|-----------|--------|
| **3D Text** | Glyphs → Geometry → Labels → Raster |
| **Glyphs** | Geometry → 3D Text → Labels → Raster |
| **Geometry** | Glyphs → 3D Text → Labels → Raster |
| **Labels** | 3D Text → Glyphs → Geometry → Raster |
| **Raster** | terminal — always achievable |

Notes:
- **Glyphs and Geometry are distinct deliverables.** Glyphs preserve ordered
  per-character outline subshapes and identity metadata inside one source-item
  compound; Geometry exposes raw edge entities. Sharing an SVG source does not
  make the host representations interchangeable.
- Renderer or font failures never authorize a whole-page or whole-mode
  substitution. A failed source item that has
  item-specific impossibility evidence and an implemented, verified next rung
  walks that rung and is certified there. An item **without** that evidence still walks
  the remaining rungs — one item that cannot be delivered costs that item, not
  the document (owner directive 2026-09-19) — but it is reported as degraded
  and it is never certified.
- Automatic raster classification may add a raster background, but it does not
  discard an explicitly requested text representation. Explicit Raster remains
  raster-only.
- The invariant is "requested type delivered and verified, or a proof-gated
  per-item fallback is reported, or the item is reported as degraded and the
  sheet is not certified." The transaction still stops for an attempt whose
  cleanup is incomplete, for an item with no stable source identity, and for
  any importer contract breach. It is locked by
  `tests/test_textmode1_invariant_fc.py`,
  `tests/test_freecad_representation_contract.py` and
  `tests/test_text_item_degrade_fc.py`.
- A degraded item is never silent and is never counted as a delivery of the
  **requested** representation: it is listed in `extra.text_items_degraded`
  with every rung's own reason code, it adds one to `result.warnings`, it is
  kept out of `extra.host_font_map` / `extra.host_font_substitutions`, and it
  makes `extra.text_representation_delivery.verified` false so
  `import_contract_ready.ready` is false for that sheet. An item that was
  drawn at a lower rung is still counted in `result.text_entities` and in
  `extra.actual_text_entity_types` as what was actually drawn; an item that
  was dropped contributes nothing.
- A page with a degraded item is imported once and is not redone on resume,
  but it is never certified. The page number is persisted with the import
  session, so every later invocation of that session repeats it in
  `extra.representation_contract_scope.uncertified_degraded_pages`, keeps
  `import_contract_ready.ready` false, and keeps the QA harness on `DEGRADED`
  with a non-zero exit code.

## Compatibility

See **[COMPATIBILITY.md](COMPATIBILITY.md)** for the full matrix. Summary:

| FreeCAD Version | Python | PyMuPDF | Status |
|----------------|--------|---------|--------|
| 1.1.x | 3.11 | 1.28.2 bundled offline | ✅ tested by hand on 1.1.4 (no automated FreeCAD test yet) |
| 1.0.x | 3.11 | 1.28.2 bundled offline | ⚠️ Expected (not tested) |
| 0.21.x | 3.10+ builds only | 1.28.2 bundled offline on 3.10 | ⚠️ Only builds that bundle Python 3.10+; the official 0.21 Windows installer (Python 3.8) is not supported yet |
| 26.x (new calendar version numbers) | not checked | | ⚠️ not yet tested |
| Any host using 3.12+ | 3.12+ | System/user install only | ⚠️ No bundled offline runtime |
| 0.20 and earlier | 3.8–3.9 | | ❌ Not supported (needs Python 3.10+) |

Evidence levels:
- `✅ tested by hand`: a person imported real drawings on that version; no automated FreeCAD test runs yet.
- `⚠️ Expected`: same Python and runtime as a tested version, but never run on it.
- `❌ Not supported`: known not to work.

## Requirements

- **FreeCAD** 1.0 or newer, or any FreeCAD build that bundles Python 3.10 or newer. Tested by hand on 1.1.4.
- **Python** 3.10 or 3.11 for the bundled offline Windows runtime. Other source hosts may use compatible system/user packages.
- **PyMuPDF** `1.28.2` in the release runtime’s shared `src/lib/common` tree. When Poppler/pdftocairo is absent, it also backs Glyphs/Geometry text rendering.
- **fontTools** `4.63.0` in the exact `src/lib/cp310` or `src/lib/cp311` tree selected from FreeCAD’s embedded Python ABI. The incompatible sibling tree is never added to `sys.path`.

## Known Limitations

| Limitation | Details |
|-----------|---------|
| Encrypted PDFs | Password-protected PDFs must be unlocked before import |
| Compression filters | Decoding is delegated to PyMuPDF. Malformed or non-standard compressed object streams may fail to parse |
| Raster-only scans | Pure raster PDFs produce no vector geometry |
| Clipped/XObject-heavy PDFs | Complex clip stacks and deeply nested form XObjects can produce partial geometry |
| Very large PDFs | Documents with >10,000 primitives may slow the import process |
| Embedded subset fonts | Valid embedded subsets are staged for native 3D Text; malformed font programs or missing/invalid character maps can still require an item-specific fallback or truthful failure |
| Older FreeCAD | Builds with Python older than 3.10 (0.20 and earlier, and the official 0.21 Windows installer) are not supported |

Large interactive imports show drawing-operation, text-character, and image-instance
counts before host-object creation. Cancellation keeps only certified pages. Resume
is persisted in the `.FCStd` document, so saving after cancellation allows a later
session to continue; unsaved process crashes are not claimed as recoverable.

## Import report / scale trust

Every import writes `<pdf name>_import_report.json` and keeps a copy you can find
again in the FreeCAD user data folder, under `PDF Import Reports`
(`<pdf name>_<date-time>_import_report.json`; the newest 50 are kept). The Report
view prints `Import report: <path>` after every import.

When an import fails, one **Import Failed** box gives the reason, the page and the
report path, with an **Open report folder** button; nothing is added to your
drawing. When an import finishes but some items were drawn another way or left
out (text items, or a drawing item drawn as plain lines), one warning box says how
many and where; a clean import shows no box. File > Open followed by Cancel leaves
no empty document behind. Headless and batch runs never show a box; they print the
same sentences.

The report carries `extra.resolved_scale` when a scale is detected.

- Use `factor` for scaling **only when** `confidence >= 0.70` **and** `fallback_reason` is not `no_scale_detected`.
- Otherwise treat scale as unknown.

## Bad-PDF open gate

FreeCAD refuses bad PDFs at open time (**fail closed**). SketchUp uses the same user-facing messages but may proceed on rare gate-internal errors (**fail open**). Compare `fallback.reason` per host rather than assuming identical refusal behavior.

## License

MIT License — see [LICENSE](LICENSE) for details.

Copyright (c) 2024-2026 BlueCollar-Systems
