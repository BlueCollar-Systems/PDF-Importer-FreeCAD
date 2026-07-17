# FreeCAD Live Representation Corrections Plan

> **For agentic workers:** execute one task at a time with strict TDD, task review, and real FreeCAD save/reopen evidence where specified.

**Goal:** Make every requested FreeCAD representation truthful, visually persistent, and bounded in cost on the owner Welding and AWS PDFs.

**Architecture:** Preserve the requested representation as the immutable contract. Record an item/page-scoped impossibility before each adjacent fallback. Separate extraction counts from host-object counts in the report. Store source visual style as durable object properties and restore it through a serializable FreeCAD view-provider path when a headless-created document later opens in the GUI. Batch recomputes and compact repeated evidence without changing output types.

**Tech Stack:** Python / FreeCAD 1.1.1 and supported older FreeCAD hosts / Part, Draft, TechDraw-compatible document objects / pytest / existing normalized PDF core and report schema.

## Binding Constraints

- Labels, Text, 3D Text, Glyphs, Geometry, and Raster are distinct requested outcomes. Alignment, rotation, scale, or style defects cannot be hidden by substituting another type.
- Fallback is allowed only after affirmative item/page-specific impossibility, owned cleanup, and the next adjacent rung. Missing source text is proof for that page, not permission for an unreported substitution.
- A successful report must describe actual reopened host objects. Zero requested entities, a Group, or a Raster cannot be counted as vector primitives or accepted as the requested non-Raster type.
- `view_style_verified == false`, `color_verified == false`, or absent live-host evidence cannot be reported as visually verified.
- Auto/Hybrid must not place a complete page raster behind complete vector/text content merely to make defects less visible. Preserve genuine embedded/source images; use a full-page raster only for explicit Raster or a proof-gated image-only page fallback.
- The fixes must not require paid software or online services.

---

### Task 1: Make image-only page fallback explicit and adjacent

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Modify: `tests/test_textmode1_invariant_fc.py`
- Add/modify focused live-core tests beside the existing representation contract tests.

- [ ] Reverse the existing false-green test near `test_textmode1_invariant_fc.py:129`: for an image-only page requested as each non-Raster mode, a silent early Raster return is forbidden.
- [ ] Add RED tests parameterized across Labels, Text, 3D Text, Glyphs, and Geometry. Require immutable requested mode; stable page source ID (`p1:page`); `no_source_text_items` affirmative impossibility; each transition adjacent in the declared ladder; verified cleanup/not-applicable ownership; and one final verified page Raster with real host entity ID.
- [ ] Move the no-source-text capability decision ahead of the early Raster branch around `PDFImporterCore.py:6715`. Remove the `source_text_blocks > 0` condition around line 561 as an authority for whether representation evidence is required.
- [ ] Reuse one source inspection across the finite ladder, but emit a separate transition record for each adjacent rung. Do not loop or retry a prior mode.
- [ ] Keep explicit requested Raster as Raster with no fallback. Keep non-Raster pages in their requested structural path when source text exists.
- [ ] Add a Welding Auto/Hybrid RED regression proving that complete-page raster is not added behind complete vector/text extraction; retain the 93 genuine source images and structural content.
- [ ] Run focused tests and a real FreeCADCmd AWS save/reopen probe to GREEN.

### Task 2: Make report accounting and readiness describe reopened objects

**Files:**
- Modify: `PDFVectorImporter/pdfcadcore/import_report.py`
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Modify/add report-contract tests.

- [ ] Add RED report tests for the real AWS structure: one Group plus one Raster image, zero vector primitives, zero requested structural text entities, one verified fallback Raster, and `images == 1`.
- [ ] Reject `actual_text_entity_types` and `text_delivery` readiness when a non-Raster request has zero source/delivery evidence. The current zero-count shortcuts near `import_report.py:511` and `:525` must not certify an unattempted contract.
- [ ] Emit actual entity types and delivery attempts even when their arrays/counts are empty, so absence is observable and validated rather than omitted around `PDFImporterCore.py:687`.
- [ ] Count Groups as containers, Raster/Image objects as images, and only actual geometric host objects as vector primitives. Add the missing image count.
- [ ] Reopen the saved FCStd and cross-check report IDs/types/counts against the actual document before setting readiness true.
- [ ] Run focused report tests plus AWS explicit Raster and proof-gated non-Raster fallback to GREEN.

### Task 3: Remove Label markers and persist native Label/Text visual style

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Create or modify the smallest serializable view-provider/style-restoration module in `PDFVectorImporter/src/`.
- Add headless-create/GUI-reopen tests and lightweight unit tests.

- [ ] Add RED Label evidence showing the zero-length `Draft.make_label(points=[anchor, anchor])` call near line 4019 still produces a visible target marker outside the source glyph bbox.
- [ ] Add RED lifecycle tests: create Label/Text in FreeCADCmd, save, close, reopen with GUI, and assert actual `ViewObject` font size, family where supported, text color, visibility, rotation/placement, and absence of leader/target marker. Custom metadata alone does not pass.
- [ ] Configure the native Draft Label view provider so only requested label text is visible—no leader, arrow, or target marker. If Draft Label cannot express that in a supported host, record that item-specific impossibility and advance one rung; do not relabel another object type as Labels.
- [ ] Persist source style through a serializable proxy/view-provider or document-restore hook that runs when GUI view properties become available. Store only the minimal durable source style and keep reopening safe when the importer is unavailable.
- [ ] Change delivery verification to require real view-style evidence when a GUI is available and to remain explicitly pending/unverified after headless creation until the GUI-reopen gate supplies it.
- [ ] Run focused tests, then render the Welding Labels/Text documents without the masking page Raster and compare the title/body colors and sizes to the source.

### Task 4: Persist Glyphs/Geometry color and truthful visual proof

**Files:**
- Modify: `PDFVectorImporter/src/PDFSvgTextRenderer.py`
- Modify related SVG representation/report tests.

- [ ] Add RED exact-color tests for blue and black Welding spans in Glyphs and Geometry, including FCStd save/reopen and actual `ViewObject` ShapeColor/LineColor values.
- [ ] Apply normalized source color to the created Part objects around line 1130 through the same durable view-style restoration mechanism used by Task 3.
- [ ] Add `color_verified` and source/actual RGB evidence. A missing view provider remains pending, never true.
- [ ] Preserve distinct Glyphs structure (per source glyph identity) and Geometry structure (flat path/compound) while applying style.
- [ ] Render and compare representative Welding regions after reopen.

### Task 5: Remove recompute and diagnostic-size explosions

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Modify: `PDFVectorImporter/src/PDFSvgTextRenderer.py`
- Modify performance/diagnostic tests.

- [ ] Instrument recompute calls in RED performance tests. Welding 3D Text currently performs at least 1,107 staged recomputes for 369 spans; Glyphs/Geometry recompute the growing document once per span.
- [ ] Create complete owned objects first, recompute at bounded batch/page checkpoints, then verify. A failed batch must still identify and clean the exact item without deleting successful peers.
- [ ] Cache reusable ShapeString/font/source computations and avoid rebuilding identical font/path data per span.
- [ ] Replace unconditional high-segment line approximation near `PDFSvgTextRenderer.py:1714` with the closest native curve/edge representation supported by FreeCAD, using adaptive tolerance tied to source scale. Do not lower visual accuracy to meet a timing budget.
- [ ] Deduplicate `empty_placement_indices` and other page-wide arrays in diagnostic output; reference one page-level record from attempts instead of copying it hundreds of times.
- [ ] Set measurable non-regression budgets from the current baselines: 3D full audit 851.7 s, Glyphs 368.5 s, Geometry 459.5 s/290,408 edges/24.23 MB FCStd/14.23 MB diagnostic. Require material improvement without changed requested type or failed visual gates.

### Task 6: Full live verification, version, commit, and push

- [ ] Run the complete test suite, syntax/import gates, shared-core sync checks, build/release safety, and release artifact validation.
- [ ] Run all six modes on both owner PDFs with FreeCADCmd 1.1.1, save/reopen ID/type checks, and GUI rendering for every style-dependent mode.
- [ ] Pixel-compare page and targeted regions for alignment, rotation, scale, clipping, color, missing/duplicate items, black Label markers, and unintended full-page raster duplication.
- [ ] Verify the installed-workbench/source mismatch is reported truthfully; only update/install after the source candidate passes.
- [ ] Review the complete diff, update version/current authority, commit, push, and verify zero ahead/behind.
