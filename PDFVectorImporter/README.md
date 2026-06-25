# PDF Vector Importer for FreeCAD

![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)
![Version: 4.0.48](https://img.shields.io/badge/Version-4.0.48-green.svg)
![Platform: FreeCAD 0.21+](https://img.shields.io/badge/Platform-FreeCAD%200.21%2B-orange.svg)

**Import vector geometry, text, and images from PDF files into FreeCAD as editable Part objects.**

Arc reconstruction, dash mapping, color grouping, OCG layer support, and reference-based scaling -- all powered by pure-Python PDF parsing via PyMuPDF.

> **BlueCollar Systems** -- BUILT. NOT BOUGHT.

---

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
4. Release ZIP/Setup installs include the private PyMuPDF runtime under `PDFVectorImporter/src/lib`; source checkouts can stage it with `python build_release.py` or **PDF Vector Importer > Install / Update PyMuPDF**.

---

## Requirements

| Dependency | Required | Notes |
|---|---|---|
| **FreeCAD** | 0.21+ | Tested through 1.0 |
| **PyMuPDF** | Yes | Bundled in release ZIP/Setup installs (`>=1.24,<2.0`); source checkouts can stage it locally |
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
