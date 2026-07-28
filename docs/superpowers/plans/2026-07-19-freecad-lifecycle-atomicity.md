# FreeCAD Lifecycle Atomicity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every FreeCAD PDF import a single fail-closed lifecycle whose document, files, source evidence, view changes, cancellation, report, and harness exit status are accepted only after all gates succeed.

**Architecture:** `import_pdf` remains the one atomic acceptance coordinator. `import_pdf_page` delegates to it with a one-page selection and the same exact boolean contract. Attempt-owned documents and content-addressed image assets join the existing object/path rollback boundary, while source sidecars consume already-bound source identity rather than rereading a path.

**Tech Stack:** Python 3.10+, PyMuPDF, FreeCAD host API, pytest, Ruff.

## Global Constraints

- Change only the FreeCAD repository and lifecycle behavior in scope.
- Do not change LibreCAD, Blender, SketchUp, raster representation semantics, or style semantics.
- Use a failing test before every production behavior change.
- Do not commit or push from this editor boundary; the parent owns integration.
- Success is literal `True`; cancellation is literal `False`; every other result is failure.

---

### Task 1: Attempt-owned embedded image assets

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Test: `tests/test_freecad_lifecycle_atomicity.py`

**Interfaces:**
- Produces: `_persist_embedded_image_asset(pix, opts, page_number, xref) -> (Path, evidence)`.
- Persists: `PDFRasterFile`, `PDFSourceSHA256`, `PDFRasterSHA256`, and embedded-image identity on every `Image::ImagePlane`.

- [ ] Write a failing test that imports identical xrefs from two bound PDF digests and asserts different content-addressed paths, exact included-file bytes/digest, and path-journal ownership.
- [ ] Run the focused test and confirm the existing fixed `img_p*_x*.png` behavior fails.
- [ ] Save to a unique temporary PNG, hash the completed bytes, derive a key containing bound PDF digest plus PNG digest, journal the final path, and atomically replace it.
- [ ] Verify the final bytes before placement and persist included-file/digest metadata so live and reopened inventory bind the same bytes.
- [ ] Inject a late report failure and assert newly created image assets disappear while pre-existing bytes are restored.

### Task 2: One public acceptance coordinator

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Test: `tests/test_freecad_lifecycle_atomicity.py`
- Migrate: `tests/test_textmode1_invariant_fc.py`

**Interfaces:**
- `import_pdf(pdf_path, opts=None, *, autofit=True) -> bool`.
- `import_pdf_page(pdf_path, page_num=1, opts=None, autofit=True) -> bool` delegates to `import_pdf` after assigning the exact page list.

- [ ] Write a failing public-page test requiring persistence, save/reopen, report, live readiness, commit-last ordering, and literal `True`.
- [ ] Confirm current `import_pdf_page` returns the inner tuple and bypasses the gates.
- [ ] Replace the duplicate page transaction with delegation to `import_pdf`.
- [ ] Migrate rollback tests to exercise the common coordinator without weakening their assertions.

### Task 3: Attempt-created document ownership

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Test: `tests/test_freecad_lifecycle_atomicity.py`

**Interfaces:**
- `_ensure_doc_with_ownership() -> (document, created_by_attempt)`.
- `_close_attempt_created_document(document, created_by_attempt) -> None`.

- [ ] Write parameterized failing tests for cancel, page/report/commit failure, and cleanup failure; assert attempt-created documents close and pre-existing documents remain open.
- [ ] Confirm current `_ensure_doc` loses ownership and never calls `FreeCAD.closeDocument`.
- [ ] Track ownership before mutation and close in a nested finalizer on every non-success path, including exceptions raised during source/path cleanup.
- [ ] Set acceptance only after commit and attempt-path acceptance; preserve the created document on literal success.

### Task 4: View state after acceptance only

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Test: `tests/test_freecad_lifecycle_atomicity.py`

**Interfaces:**
- `autofit` is a coordinator option; `_autofit_import_view` runs only after commit/acceptance.

- [ ] Write a failing ordering test asserting late persistence/report/live/commit failures never call autofit and success orders `commit` before `autofit`.
- [ ] Remove pre-gate autofit from both old entry paths.
- [ ] Invoke autofit after successful commit only, containing view-only exceptions so accepted model state cannot be reclassified as failed.

### Task 5: Immutable source identity in sidecars

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Modify: `PDFVectorImporter/pdfcadcore/source_provenance.py`
- Modify: `PDFVectorImporter/pdfcadcore/parts_bootstrap.py`
- Test: `tests/test_freecad_lifecycle_atomicity.py`
- Test: `tests/test_source_provenance.py`
- Test: `tests/test_parts_bootstrap.py`

**Interfaces:**
- Sidecar builders accept optional `source_display_path` and `source_sha256` already bound by the caller.
- When a digest is supplied, builders validate syntax and do not read either display or snapshot path.

- [ ] Write failing tests that delete/mutate the original after `_initialize_pdf_source_attempt`, then assert both sidecars retain the original display path and the prebound digest without exposing the snapshot path.
- [ ] Add explicit source identity parameters to both builders while preserving backward-compatible direct-path behavior.
- [ ] Pass original provenance path and `_pdf_sha256` from `write_import_report`; never pass the temporary snapshot path to sidecar output.

### Task 6: Truthful and revalidated snapshot protection

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Test: `tests/test_freecad_lifecycle_atomicity.py`

**Interfaces:**
- `_snapshot_protection_evidence(snapshot_path, snapshot_root) -> dict` reports verified read-only and privacy state plus platform/method.
- `_validated_pdf_source_snapshot_path` validates bytes and current protection against bound provenance.

- [ ] Write a failing test that makes the snapshot writable without changing bytes during a consumer and requires terminal protection failure.
- [ ] Add a platform-aware test: POSIX privacy is derived from directory group/other mode bits; Windows never claims private ACL solely from `chmod`.
- [ ] Replace optimistic flags with measured evidence after creation and at every pre/post consumer validation.
- [ ] Reject loss of required read-only protection and any change in the recorded protection evidence.

### Task 7: Cancellation checkpoints across work and finalization

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Test: `tests/test_freecad_lifecycle_atomicity.py`

**Interfaces:**
- `_invoke_import_cancellation_checkpoint(opts, phase) -> None` processes GUI events and raises `ImportCancelled` when the progress dialog or injected callback cancels.

- [ ] Write failing tests that cancel inside text-item, fallback, raster retry, hatch, and embedded-image loops and immediately before persistence, report, live gate, and commit; assert complete object/file rollback and literal `False`.
- [ ] Install one per-page checkpoint callback that processes Qt events before checking cancellation.
- [ ] Call the checkpoint at bounded intervals in geometry/render loops and on every text item, fallback attempt, raster retry, hatch group, and embedded-image iteration.
- [ ] Call it immediately before every final acceptance boundary.

### Task 8: Harness exit status

**Files:**
- Modify: `PDFVectorImporter/adapters/freecad_harness.py`
- Test: `tests/test_freecad_harness_contract.py`

**Interfaces:**
- `main() -> int` returns `0` only for result status `PASS`, otherwise a nonzero code.
- Both direct and environment-triggered module branches raise `SystemExit(main())`.

- [ ] Write failing tests for PASS, caught FAIL, cancellation/non-exact result, and the environment-triggered branch.
- [ ] Return `0 if result["status"] == "PASS" else 1` after writing JSON.
- [ ] Raise `SystemExit(main())` in both execution branches.

### Task 9: Verification and release of editor boundary

**Files:**
- Verify all modified files; do not add scope.

- [ ] Run focused lifecycle/harness/sidecar tests.
- [ ] Run every `test_freecad*.py` test with a fresh basetemp.
- [ ] Run Ruff `F,B`, `compileall`, and `git diff --check`.
- [ ] Inspect the final diff for unrelated representation/style changes.
- [ ] Report exact counts and explicitly release exclusive editor ownership.
