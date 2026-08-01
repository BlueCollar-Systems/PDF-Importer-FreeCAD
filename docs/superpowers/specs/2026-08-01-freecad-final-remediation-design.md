# FreeCAD final remediation design

Date: 2026-08-01  
Status: approved

## Objective

Close the remaining agent-actionable FreeCAD findings without weakening the
importer's representation-fidelity, source-identity, privacy, immutable-release,
or fail-closed packaging contracts.

## 1. Reproducible Windows installer

The release workflow currently accepts whichever `ISCC.exe` is on the runner.
The v4.0.79 log reported Inno Setup 6.7.1, but a local build from the exact
published ZIP with the official 6.7.1 distribution produced different bytes
from the published installer. A version string alone is therefore not a build
identity.

The workflow will download the vendor's immutable `is-6_7_1` GitHub release
asset, verify the installer SHA-256, install it in portable mode under the
runner temporary directory, and verify a committed manifest covering the full
compiler tree. `build_windows_installer.py` will fail closed unless the selected
compiler belongs to that exact tree. It will emit an attestation binding the
canonical ZIP, installer, toolchain manifest, version, and source commit.

The release gate will compile twice in isolated directories and require
byte-for-byte equality before publication. The official distribution is fetched
at build time rather than vendored, avoiding repository bloat while respecting
the upstream binary-redistribution license.

## 2. Atomic, idempotent release control plane

A tested Python state machine will own release publication.

- No tag and no release: create the release at the exact target with all assets
  in one `gh release create` operation.
- Exact tag and no release: recover safely with `--verify-tag`, publishing the
  complete asset set without deleting or rewriting the tag.
- Tag at another commit: fail closed.
- Existing release: verify its tag target and the exact expected asset names and
  digests, then return idempotent success without mutation.
- A create race re-reads remote state and converges only when the resulting
  release satisfies the same exact contract.

No path uploads, overwrites, clobbers, release deletion, or tag rewriting are
permitted. ZIP, Setup.exe, and the toolchain attestation are published together.

Post-release digest records live in a dedicated ledger. Its helper permits only
ledger paths and constructs a fixed commit subject containing `[skip release]`.
The auto-release trigger also ignores the ledger path, giving both message- and
path-level protection against a bookkeeping release loop.

## 3. Truthful progress and cancellation

The core will expose a UI-neutral import work plan and progress callback. Work
units are transparent: PDF drawing operations, text characters/items, and image
instances. The GUI entry points will share one overall Qt progress dialog that
reports page, stage, completed/total work, and elapsed time. Bounded callbacks
inside geometry, text, batching, image, and finalization loops keep event
processing and Cancel responsive without exhausting host GUI handles.

Cancel is dedicated control flow, not a successful page return. The outer
orchestrator records the object boundary before each page, removes only partial
objects created for the active page, commits already certified pages, records a
cancelled result/report, and never prints the normal completion message.

## 4. Durable page resume in the user model

Each production import creates a persistent FreeCAD import-session object. It
stores the schema, exact source PDF SHA-256, canonical content-affecting options
and digest, importer/package version, original requested pages, certified
completed pages, created page-group identities, and status.

Resume is offered only when all identity fields match exactly. Completed pages
are skipped, and placement is derived from the original requested-page order so
new pages join the same model without overlap or duplicates. Updating and saving
the FreeCAD document persists the session; reopening that document can resume
the same import. Unsaved application-crash recovery is explicitly out of scope
and will not be claimed.

Ordinary failures retain the existing fidelity doctrine and abort all work from
the current invocation. Only an explicit user cancellation commits certified
page boundaries.

## 5. Verification and boundaries

Implementation uses strict RED-to-GREEN tests for toolchain identity,
double-build reproducibility, release-state convergence, bookkeeping guards,
progress/cancel truth, session identity, cleanup, placement, and save/reopen
resume. Final verification includes the full public suite, style/syntax/sync and
release integrity gates, deterministic ZIP and installer double builds, a real
FreeCAD cancel/save/reopen/resume run, and installer install/payload comparison.

Private corpus files never enter the repository or release artifacts. Old
ACL-protected ignored temporary directories remain an administrator-only cleanup
constraint. The implementation does not weaken ACLs or perform broad deletion.
