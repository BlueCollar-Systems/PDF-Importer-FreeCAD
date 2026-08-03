# PDF Vector Importer for FreeCAD

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Version: 4.0.84](https://img.shields.io/badge/Version-4.0.84-green.svg)
![Platform: FreeCAD 0.21+](https://img.shields.io/badge/Platform-FreeCAD%200.21%2B-orange.svg)

**Import vector geometry, text, and images from PDF files into FreeCAD as editable Part objects.**

Arc reconstruction, dash mapping, color grouping, OCG layer support, and reference-based scaling -- all powered by pure-Python PDF parsing via PyMuPDF.

> **BlueCollar Systems** -- BUILT. NOT BOUGHT.

---

## Recent fixes (v4.0.84)

- Import reports reconcile every concrete type inside aggregate `mixed` text
  delivery. Requested types and item-proven fallbacks pass; an unproven subtype
  still fails closed instead of being hidden by the aggregate.

## Recent fixes (v4.0.83)

- Exact 3D Text preserves leading/trailing whitespace with FreeCAD-native pen
  advance and ink-origin verification, and exhausts both same-mode construction
  paths before an evidence-backed cross-mode fallback.
- Glyphs now stores ordered per-character subshapes inside one source-item
  compound with glyph identity metadata, avoiding oversized FCStd archives;
  per-glyph editing moves from tree objects to compound subshapes.
- SVG conversion renders every selected page from one verified immutable
  import-run PDF snapshot.
- Windows releases select exact CPython 3.10/3.11 fontTools payloads beside one
  shared PyMuPDF runtime.
- `HEAD`-bound packaging fails closed on private paths, generated artifacts,
  and project identifiers supplied only through the masked external denylist.
- Import reports always publish a validated `ok` or `warn` scale evaluation;
  missing or malformed evaluations remain fail closed for consumers.

## Recent fixes (v4.0.82)

- Unique staged-font recovery is restricted to MuPDF's measured 24-character
  span-name truncation boundary. Longer names remain exact-match only, while a
  genuine unique truncation resolves through completed font staging before 3D
  text delivery.

## Recent fixes (v4.0.81)

- Exact staged-font matching now accepts a 24-character MuPDF-truncated span
  name only when it uniquely extends to one staged font. Ambiguous and normal
  short prefixes remain terminal, preventing wrong-font substitution while
  restoring affected Noto symbol text.

## Recent fixes (v4.0.80)

- Interactive imports preflight transparent work units and report real page,
  geometry, text, and image progress through one responsive Cancel control.
- Cancel removes the incomplete active page and persists completed-page state
  in the FreeCAD document. After saving/reopening, an exact matching PDF,
  options, package version, and page order resumes only the unfinished pages.
- Windows releases use a hash- and Authenticode-pinned Inno Setup 6.7.1 tree,
  prove two Setup.exe builds byte-identical, and publish a deterministic
  ZIP/Setup/toolchain attestation without tag rewriting or asset clobbering.

## Recent fixes (v4.0.79)

- Type3 font-program absence is now an exact item-level fallback condition;
  unrelated page text remains importable while malformed font inventory still
  fails closed.
- Unicode PDF paths use bounded header validation and path-based PyMuPDF open,
  avoiding an unnecessary full-file memory copy.
- Heavy-page acceptance adds pre-host complexity limits and source-bound page
  checkpoints with explicit remaining-page reporting.
- Packaging rejects private or dirty inputs and builds a fresh hash-locked
  runtime in isolation instead of reusing stale local dependency bytes.

## Recent fixes (v4.0.78)

- Windows `/SILENT` and `/VERYSILENT` installs terminate unattended; the
  completion notice is retained for interactive installs only.

- The canonical ZIP and Windows Setup.exe now publish in one atomic release,
  so immutable release protection can lock both assets together.

- Release archives are reproducible and no longer contain unused
  build-machine-bound Python console launchers or their wheel records.

- Exact 3D-text font staging and hybrid raster assets now fall back
  automatically to a writable temporary cache when the FreeCAD user Mod
  directory is unavailable or locked down.

## Key Features

| Category | Capability |
|---|---|
| **PDF Parsing** | PyMuPDF-powered vector extraction with full path, text, and image support |
| **Import Modes** | Auto (default), Vector, Raster, Hybrid — every mode targets maximum fidelity (BCS-ARCH-001) |
| **Text Rendering** | Labels, 3D Text, Glyphs, Geometry — orthogonal to mode |
| **Arc Reconstruction** | Kasa algebraic circle fit converts polyline segments back to true arcs |
| **Layer Support** | OCG layers (PDF Optional Content Groups) map to FreeCAD groups |
| **Color Grouping** | Geometry automatically organized by stroke/fill color |
| **Dash Patterns** | Hidden, center, and phantom line types mapped from PDF dash arrays |
| **Scale by Reference** | Pick two points on a known dimension, type the real-world value |
| **Quick Scale** | Architectural presets from 1:1 through 1:200 |
| **Text Import** | Labels, 3D Text, and vector glyph/geometry via pdftocairo or bundled PyMuPDF fallback |
| **Raster Fallback** | Scanned pages imported as positioned images when no vectors are found |
| **Image Extraction** | Embedded images extracted and placed in the model |
| **Hatch Detection** | Three modes: Import, Group, or Skip detected hatch regions |
| **Batch Import** | Multi-file import and drag-and-drop support |
| **SKP Bridge** | Import SketchUp `.skp` models via workbench command when backend support exists |
| **Auto View** | Orthographic top-down view set automatically after import |

---

## Installation

1. Copy the `PDFVectorImporter` folder into your FreeCAD `Mod` directory:

   | OS | Typical Path |
   |---|---|
   | **Windows** | `%APPDATA%\FreeCAD\Mod\` |
   | **macOS** | `~/Library/Application Support/FreeCAD/Mod/` |
   | **Linux** | `~/.FreeCAD/Mod/` |

2. Restart FreeCAD.
3. Switch to the **PDF Vector Importer** workbench from the workbench selector.
4. Release ZIP/Setup installs include shared PyMuPDF under `src/lib/common` and exact CPython 3.10/3.11 fontTools payloads under `src/lib/cp310` and `src/lib/cp311`. FreeCAD selects only its matching ABI tree. Source checkouts can use **PDF Vector Importer > Install / Update PDF Dependencies** for the current user.

---

## Requirements

| Dependency | Required | Notes |
|---|---|---|
| **FreeCAD** | 0.21+ | Offline bundle supports embedded CPython 3.10 and 3.11; 1.1 installer smoke verified |
| **PyMuPDF** | Yes | Version 1.28.0 bundled once in the shared stable-ABI tree |
| **fontTools** | Yes | Version 4.63.0 bundled separately for cp310 and cp311 to preserve embedded fonts and Unicode mappings |
| **pdftocairo** | Optional | Preferred SVG renderer for text-as-geometry; bundled PyMuPDF is used when Poppler is absent |

---

## Architecture

```
PDFVectorImporter/
|-- Init.py                     # FreeCAD workbench registration
|-- InitGui.py                  # GUI commands and menus
|-- PDFImportHandler.py         # Top-level import orchestration
|-- PDFTools.py                 # Toolbar actions (Scale, Quick Scale, Batch)
|-- src/
|   |-- PDFImporterCore.py      # Central import pipeline
|   |-- PDFImporterCmd.py       # FreeCAD command wrappers
|   |-- PDFScaleTool.py         # Scale by Reference implementation
|   |-- PDFHatchDetector.py     # Hatch region detection engine
|   |-- PDFPrimitives.py        # Primitive geometry builders
|   |-- PDFSvgTextRenderer.py   # SVG/text rendering pipeline
|   |-- PDFPrimitiveExtractor.py
|   |-- PDFRecognition.py       # Pattern and symbol recognition
|   |-- PDFRegions.py           # Spatial region analysis
|   |-- PDFValidation.py        # Import validation checks
|   |-- PDFDimensionParser.py   # Dimension text extraction
|   |-- PDFDocumentProfiler.py  # Document type classification
|   |-- PDFGenericClassifier.py # Generic element classification
|   |-- PDFGenericRecognizer.py # Generic pattern recognition
|   |-- PDFGeometryCleanup.py   # Duplicate/overlap removal
```

---

## QA and Testing

The project includes a dedicated test runner system for automated validation.

**Test Runner:** `run_pdf_vector_importer_tests.py`

The test harness supports multiple target platforms through an adapter pattern:

| Adapter | Target | Description |
|---|---|---|
| **FreeCAD** | FreeCAD 0.21+ | Full integration tests against live FreeCAD |
| **SketchUp** | SketchUp | Cross-platform validation via SketchUp adapter |
| **Blender** | Blender 3.6+ | Headless CLI validation via Blender importer adapter |
| **LibreCAD** | LibreCAD (DXF flow) | PDF-to-DXF validation via LibreCAD adapter |

**Test artifacts:**
- `qa_config_*.json` -- test suite configuration files
- `qa_results_*.json` / `*.csv` -- machine-readable test results
- Test PDFs in the project root for validation against known inputs

Run the full suite:

```bash
python run_pdf_vector_importer_tests.py --workbook path/to/your_workbook.xlsx --config qa_config_local_full.json
```

Run a smoke test:

```bash
python run_pdf_vector_importer_tests.py --workbook path/to/your_workbook.xlsx --config qa_config_local_smoke.json
```

Run platform-specific smoke tests:

```bash
python run_pdf_vector_importer_tests.py --workbook qa_workbook.xlsx --config qa_config.json --platform BL --automation AUTO
python run_pdf_vector_importer_tests.py --workbook qa_workbook.xlsx --config qa_config.json --platform LC --automation AUTO
```

Workbook platform sheet names are:
`SketchUp Tests`, `FreeCAD Tests`, `Blender Tests`, and `LibreCAD Tests`.

Bootstrap a starter workbook on a fresh clone:

```bash
python run_pdf_vector_importer_tests.py --init-workbook qa_workbook.xlsx
```

---

## Usage

1. Open FreeCAD and switch to the **PDF Vector Importer** workbench.
2. Click **Import PDF** or drag a PDF file onto the 3D view.
3. Select an import mode (leave as **Auto** for most files — it picks the right strategy per page).
4. Geometry appears as editable Part objects, grouped by color and layer.
5. Use **Scale by Reference** to calibrate to real-world dimensions.

---

## License

MIT License. See [LICENSE](LICENSE) for details.

Copyright (c) 2024-2026 BlueCollar Systems
