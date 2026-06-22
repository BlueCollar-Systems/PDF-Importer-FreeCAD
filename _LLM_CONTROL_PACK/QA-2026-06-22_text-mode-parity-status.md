# QA-2026-06-22 — Text Mode Parity Status

**Date:** 2026-06-22  
**Scope:** SketchUp, FreeCAD, LibreCAD, Blender — text modes × import modes (BCS-ARCH-001)

## Executive answer

**NO** — not all text modes are fully and correctly functional in all import modes in all importers. SketchUp is the reference implementation (four distinct paths). Python hosts share pdfcadcore extraction but host adapters differ; LibreCAD is intentionally 2D-limited.

## Status matrix

| Host | labels | 3d_text | glyphs | geometry | import_report `extra.text_mode` |
|------|--------|---------|--------|----------|----------------------------------|
| **SketchUp** | ✅ Native `add_text` | ✅ `add_3d_text` mesh | ✅ SVG → edges | ✅ SVG → edges | ✅ |
| **FreeCAD** | ✅ Draft text | ✅ ShapeString extrude (v4.0.31+) | ✅ pdftocairo SVG | ✅ pdftocairo SVG | ✅ (this pass) |
| **Blender** | ✅ FONT curves | ✅ extruded curves | ✅ meshified curves | ✅ meshified curves | ✅ (this pass) |
| **LibreCAD** | ✅ DXF TEXT | ⚠️ Alias → TEXT (2D) | ⚠️ Alias → TEXT (2D) | ✅ Skips editable TEXT | ✅ (this pass) |

### × Import mode (text when `import_text` on)

| Mode | SU | FC | BL | LC (package pipeline) |
|------|----|----|----|-------------------------|
| **auto** | ✅ all 4 modes; raster page fallback drops text | ✅ | ✅ | ✅ via `librecad_pdf_importer` |
| **vector** | ✅ OCG labels use native path | ✅ | ✅ | ✅ |
| **raster** | N/A (text off) | N/A (early return) | N/A (text skipped) | ✅ raster image only |
| **hybrid** | ✅ text on vector overlay | ✅ | ✅ | ✅ |

**LC note:** `pdf2dxf.py` / GUI now delegate `auto|raster|hybrid` to the package pipeline (`dxf_import_engine._convert_via_package`). Forced `vector` still uses the legacy fast path.

## Fixes in this pass

1. **pdfcadcore `build_import_report`** — `import_text` + `text_mode` in `extra` (FC canonical, synced BL/LC).
2. **FreeCAD** — `3d_text` uses `_render_text_spans_3d` (Draft ShapeString + extrusion); labels/glyphs/geometry unchanged.
3. **LibreCAD** — package pipeline wired for non-vector CLI/GUI modes; `DxfExportOptions.text_mode` respected.
4. **Tests** — `test_import_report_text_mode.py` (FC/LC), `test_text_mode_builder.py` (BL), `test_text_mode_routing.rb` (SU).

## Deferred (honest)

| Gap | Reason |
|-----|--------|
| LC true vector-glyphs / 3D DXF | LibreCAD is 2D; no MTEXT/3D text in host |
| FC glyphs without pdftocairo | Falls back to labels (documented) |
| SU SVG missing on clean PC | Geometry/Glyphs fail-closed to labels (or 3D if selected) |
| Cross-host host-run CI matrix | Headless tests only; no SketchUp/FreeCAD GUI automation in CI |
| Top-level `text_mode` field in schema | Still in `extra` only (schema 1.1); SU already used `extra.text_mode` |

---

## Q&A

### Q1: Is text rendering orthogonal to import mode everywhere?

**A:** Yes by design (BCS-ARCH-001). Import mode chooses extraction strategy per page; text mode chooses how surviving text is rendered. **Exception:** `raster` mode sets `import_text=False` in pdfcadcore `ImportConfig.raster()` and SketchUp/FC/BL skip text on raster-only pages — text is N/A, not broken.

### Q2: Why does LibreCAD CLI list four text modes but the GUI shows two?

**A:** LibreCAD cannot display true 3D or per-glyph vector text. GUI exposes **Labels** and **Outlines** (`geometry`). CLI accepts `3d_text` and `glyphs` for scripting parity; they map to editable DXF TEXT until a vector-glyph DXF path exists.

### Q3: Where is `text_mode` recorded after import?

**A:** `bcs.import_report/1.1` → `extra.text_mode` and `extra.import_text` on all Python hosts after this pass. SketchUp also records `extra.text_renderers[]` per page for degradation auditing.

### Q4: Which host should I use as the fidelity reference for text?

**A:** **SketchUp** for four-way routing and OCG-aware label placement. **FreeCAD** for CAD editing with Draft labels or pdftocairo geometry. **Blender** for 3D scenes (extruded/mesh text). **LibreCAD** for 2D DXF exchange (labels/outlines only).

### Q5: What happens to text when auto mode raster-fallbacks a page?

**A:** Text on that page is not imported as geometry (page becomes raster image). Other pages in the same job keep the selected text mode. Check `import_report.fallback` and per-page resolved mode in LC `extra.auto_mode` / FC `extra.auto_resolved_mode`.
