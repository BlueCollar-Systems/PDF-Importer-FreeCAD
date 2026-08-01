# FreeCAD final remediation implementation plan

> Execute each implementation task with strict RED -> GREEN -> refactor. Do not
> alter immutable tags/releases; publish only a new patch version after all
> product and release gates pass.

**Goal:** Make Setup.exe reproducible and attested, make release publication
recoverable and idempotent, prevent bookkeeping release loops, and deliver
truthful cancellable/resumable production imports in the user model.

**Architecture:** Release concerns are isolated in small testable scripts and
the workflow becomes orchestration only. Import progress remains UI-neutral in
the core; Qt adapts structured events. Resume state is persisted as properties
on a FreeCAD document object and is accepted only under exact source/options/
package identity.

**Stack:** Python 3.10+, pytest, FreeCAD Python API, PySide2/PySide6, GitHub CLI,
PowerShell, Inno Setup 6.7.1.

---

## Task 1: Lock and attest the installer toolchain

**Files:**
- Create: `installer/inno-toolchain-6.7.1.json`
- Create: `scripts/install_inno_toolchain.ps1`
- Modify: `build_windows_installer.py`
- Modify: `tests/test_windows_installer_byte_parity.py`
- Modify: `.github/workflows/auto-release.yml`
- Modify: `README.md`, `INSTALL.md`, `THIRD_PARTY_NOTICES.md`

1. Add failing tests requiring the exact upstream installer URL/hash, a full
   portable-tree manifest, fail-closed compiler verification, deterministic
   attestation fields, and two isolated build comparison in the workflow.
2. Run the focused test file and capture RED.
3. Add the manifest and bootstrap verifier; make installer compilation accept
   explicit output/stage directories and require the exact toolchain.
4. Replace Chocolatey installation with the pinned vendor asset and add the
   double-build gate plus attestation asset.
5. Run focused tests and empirical two-root/two-toolchain builds to GREEN.

## Task 2: Make release publication converge safely

**Files:**
- Create: `scripts/publish_release.py`
- Create: `tests/test_publish_release.py`
- Modify: `.github/workflows/auto-release.yml`
- Modify: `tests/test_release_safety.py`

1. Add fake-`gh` RED tests for absent tag, exact orphan tag, mismatched tag,
   existing valid release, invalid existing assets, and create races.
2. Implement a subprocess-injected state machine that validates target and
   complete local/remote asset identities and never overwrites immutable state.
3. Replace inline shell mint logic with the helper and preserve `minted` output.
4. Run release-control focused tests to GREEN.

## Task 3: Make bookkeeping non-triggering by construction

**Files:**
- Create: `scripts/release_bookkeeping.py`
- Create: `tests/test_release_bookkeeping.py`
- Create: `release-bookkeeping/README.md`
- Modify: `.github/workflows/auto-release.yml`

1. Add RED tests for canonical JSON, ledger-only paths, fixed `[skip release]`
   subject, and workflow path exclusion.
2. Implement record generation and an explicit commit-subject command; reject
   paths outside the ledger.
3. Add the trigger exclusion and run focused tests to GREEN.

## Task 4: Add pure session identity and work planning

**Files:**
- Create: `PDFVectorImporter/src/PDFImportSession.py`
- Create: `tests/test_import_session_fc.py`
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`

1. Add RED tests for canonical content-option serialization/digest, exact
   source/options/package match, completed/remaining page math, page placement,
   complexity aggregation, and invalid persisted state.
2. Implement pure helpers plus the FreeCAD-property adapter. Never persist PDF
   bytes; store only source digest and a user-facing basename.
3. Run the focused pure tests to GREEN.

## Task 5: Add truthful progress and responsive cancellation

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Modify: `PDFVectorImporter/src/PDFImporterCmd.py`
- Modify: `PDFVectorImporter/PDFImportHandler.py`
- Create: `tests/test_import_progress_resume_fc.py`
- Modify: `tests/test_gui_fc.py`

1. Add RED tests proving structured page/stage/unit events, bounded inner-loop
   cancellation, partial-page cleanup, certified-page retention, cancelled
   report truth, and no success message on Cancel.
2. Add a dedicated `ImportCancelled` control-flow exception and UI-neutral
   callback helpers. Instrument geometry, text, batching, image, and final work.
3. Centralize GUI execution behind one Qt progress adapter shared by command and
   file-handler entry points; display the transparent complexity estimate.
4. Run focused tests to GREEN.

## Task 6: Persist and resume into one assembled model

**Files:**
- Modify: `PDFVectorImporter/src/PDFImporterCore.py`
- Modify: `PDFVectorImporter/src/PDFImporterCmd.py`
- Modify: `PDFVectorImporter/PDFImportHandler.py`
- Modify: `tests/test_import_progress_resume_fc.py`
- Modify: `README.md`, `PDFVectorImporter/README.md`

1. Add RED tests for exact-match resume discovery, package/options mismatch
   rejection, duplicate-page prevention, original-order placement, successful
   completion, and save/reopen property round-trip.
2. Create/update the in-document session inside the import transaction. On
   Cancel, remove the active-page delta, commit certified pages, and return a
   cancelled outcome. On resume, import only remaining pages into the same
   session and page groups.
3. Offer Resume only for exact matches and document the save/reopen versus
   unsaved-crash boundary.
4. Run focused tests to GREEN.

## Task 7: Version, audit, and verify the release candidate

**Files:**
- Modify version surfaces required by `package.xml`, `pyproject.toml`, and both
  README badges.
- Modify any genuine actionable TODO/limitation found by the final audit.

1. Audit tracked TODO/FIXME/KNOWN GAP/limitations; close agent-actionable items
   and document only external constraints.
2. Bump exactly one patch version because product bytes changed.
3. Run compile, Ruff, sync, full pytest, privacy, source-integrity, and release
   safety gates.
4. Build the canonical ZIP twice in isolated trees and require identical bytes.
5. Build the installer twice with the pinned toolchain and require identical
   bytes/attestations.
6. Run a real FreeCAD host cancel -> save -> reopen -> resume test and verify one
   complete model with no duplicate pages.
7. Install Setup.exe silently to an isolated target and compare every installed
   payload file with the canonical ZIP.

## Task 8: Publish and record evidence

1. Re-read local/remote HEAD and preserve any external changes.
2. Commit the implementation, push `main`, and wait for required CI.
3. Publish only the new immutable patch tag/release; verify target, asset names,
   sizes, and digests from GitHub readback. Never modify v4.0.79.
4. Append exact commands/results/digests/workflows/constraints to the requested
   Q&A report and supporting board/status entries without including private
   corpus paths or bytes in the repository.
