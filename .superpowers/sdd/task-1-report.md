# FreeCAD Task 1 report — image-only proof chain and no masking raster

## Scope

- Worktree: `C:\TMP\freecad-week-defect-sweep`
- Plan task: Task 1 only
- Tracked files: `PDFVectorImporter/src/PDFImporterCore.py`, `tests/test_textmode1_invariant_fc.py`

## RED

Focused owner-contract tests were written before production changes.

- Initial run: `8 failed, 11 passed in 1.78s`.
- Failures proved image-only Auto returned Raster without structural attempts; the page fallback helper/policy did not exist; explicit Raster had no separately testable terminal contract.
- A later regression for legacy Vector raster fallback failed with missing proof-continuation policy: `1 failed in 0.52s`.

## Implementation

- Auto Raster classification can no longer use `source_text_blocks > 0` to decide whether a requested structural text contract deserves evidence.
- Source-bearing Auto Raster pages resolve to structural Hybrid without a full-page masking raster.
- Hybrid preserves vector geometry plus genuine embedded images; it does not place a complete-page raster.
- Auto and legacy Vector image-only paths place one provisional page Raster, continue through canonical source inspection, and record every adjacent ladder rung as `proven_impossible` with stable `pN:page` identity, one reused source observation, no owned artifacts, and verified cleanup/not-applicable state.
- Only after the finite chain does the real `Image::ImagePlane` become the verified final Raster. Explicit requested Raster remains terminal with no false fallback chain.

## Automated GREEN

- Focused file: `19 passed in 0.68s` before the legacy addition.
- Representation/fallback/production contract set after all changes: `131 passed in 2.36s`.
- `python -m compileall -q PDFVectorImporter/src/PDFImporterCore.py tests/test_textmode1_invariant_fc.py` — PASS.
- `git diff --check` — PASS.

## FreeCAD 1.1.1 live evidence

All probes loaded the isolated worktree core, not the installed 4.0.67 workbench.

### AWS image-only, Auto, requested Labels

Evidence: `C:\TMP\freecad-week-task1-aws-labels\diagnostic.json`

- no exception; saved FCStd reopened;
- attempt types: `labels -> text -> 3d_text -> glyphs -> geometry -> raster`;
- first five outcomes `proven_impossible`, final Raster `verified`;
- one live Raster after reopen, zero reported IDs missing, zero removed IDs still live;
- pre-save/post-reopen names identical;
- elapsed 3.34 s.

### AWS image-only, Vector fallback, requested Geometry

Evidence: `C:\TMP\freecad-week-task1-aws-vector-geometry\diagnostic.json`

- no exception; six adjacent attempts; final Raster survived reopen;
- one fallback record and one live Raster;
- elapsed 1.65 s.

### Welding mixed page, Auto, requested Labels

Evidence: `C:\TMP\freecad-week-task1-welding-labels\diagnostic.json`

- no exception; Auto resolved Hybrid;
- 369 requested native Label objects and 93 genuine `Image::ImagePlane` source images survived reopen;
- `raster_page_count == 0`; no `Page_1_raster`/Raster representation object exists;
- 173 structural Part objects remain; pre-save/post-reopen names identical; zero verified IDs missing;
- elapsed 16.56 s.

## Remaining plan

Task 2 must correct report object accounting/readiness. Tasks 3–5 handle Label markers/style, Glyph/Geometry color, and performance. This task does not claim those later defects are fixed.

## Independent review correction — 2026-07-17

An independent review rejected the first implementation on three failure/contract paths. Each correction followed a separate RED → GREEN cycle before the full suite was run.

### RED evidence

- Atomic ledger/rollback cycle: two injected ledger failures retained partial state and the public page wrapper exposed no rollback record: `3 failed in 0.45s`.
  - `_append_text_item_attempt` raised after its third append.
  - `_record_text_mode_fallback` raised after a verified page Raster result already existed.
  - A synthetic direct page failure left page/raster/text objects without truthful cleanup evidence.
- Orthogonal Raster/text cycle: all five structural modes (`labels`, `text`, `3d_text`, `glyphs`, `geometry`) were suppressed by `import_mode="raster"`: `6 failed, 1 passed in 0.66s`.
- Unknown fast-probe cycle: `get_text("blocks")` failed while canonical `get_text("dict")` returned visible text, yet one full-page Raster was placed: `1 failed in 0.44s`.

### Corrections

- `_record_no_source_text_page_fallback` now builds and validates attempts, fallback records, and delivery counts on an isolated options copy. It publishes all three ledgers only after every operation succeeds and restores the exact prior values and container identities on any exception.
- Public `import_pdf_page` now owns a single-page transaction and a pre-import object baseline. A failure aborts the transaction, removes only post-baseline objects, verifies the live document, and attaches actual created/removed IDs plus cleanup truth to `TextRepresentationFailure`. Multi-page `import_pdf` still calls `_import_pdf_page_inner` directly, so its existing outer transaction is not nested.
- Page strategy and text representation are orthogonal again. An explicitly requested page Raster remains a background while a requested structural text mode is rendered/proven. Only requested text Raster or disabled text is terminal.
- A failed lightweight blocks probe is represented as unknown (`None`), never zero. Auto Raster classification is held on a non-masking structural path until canonical source inspection. Canonical visible text therefore receives structural delivery without a page Raster; canonical no-text still reaches the existing proof-gated Raster path.

### Automated verification

- Defect-specific invariant file: `29 passed in 0.67s`.
- Representation/fallback/production contract set: `140 passed in 2.24s`.
- Full repository suite: `389 passed, 16 skipped, 1 known deprecation warning in 7.00s`.
- AST parse: PASS for both changed Python files.
- Built-in `compile(...)`: PASS for both changed Python files.
- `git diff --check`: PASS.

### FreeCAD 1.1.1 live verification

- Injected direct-wrapper failure: created `PDF_Page_1`, `Page_1_raster`, and `Text`; all three were reported removed, `cleanup_complete == true`, no post-baseline object remained, and the pre-existing `Existing` object survived.
- AWS Auto + requested Labels: 6 attempts (`5 proven_impossible + 1 verified Raster`), 1 fallback, 1 page Raster, 2 live objects.
- Welding Auto + requested Labels: 369/369 verified Label attempts, 369 structural Labels, 93 genuine images, 648 live objects, 0 fallbacks, and 0 page Rasters.

The branch was intentionally not rebased; origin/main performance/report decorators must be preserved during later integration.
