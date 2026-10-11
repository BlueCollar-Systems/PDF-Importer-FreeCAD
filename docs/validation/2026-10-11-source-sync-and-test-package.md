# Source synchronization and local test package — 2026-10-11

The completed source changes were preserved and main was synchronized with
upstream through `772ae8f3e4adc1361b36b6656bd43c6aaa119831` (PRs 98 and 97).
The resulting source snapshot is
`c96d6516f55ce59c8424e03acbc266286bad9bf0`. It includes the newer page controls,
batch cancellation, grouping, geometry step-down reports, fill-boundary and
subpath handling, and existing exact source-font and resume-content proofs.

Only overlapping regression fixtures needed merge resolution. Valid disconnected
stroke subpaths retain two separate editable wires without degradation. Negative
geometry tests explicitly make the host facade refuse those wires rather than
depend on the former disconnected-wire bug. A separate unsplit-gap test retains
the upstream fallback case and verifies both original source lines survive. The
font regression uses conflicting glyph IDs 3 and 7 at one origin; a positive
case separately verifies that coincident identical glyph IDs preserve their
original font-program binding. No shared-core proof gate was changed.

## Checks for this snapshot

- The affected merged regression set passed **152 tests**.
- The full nonnative suite passed **2,524 tests and 23 subtests**, with 19 skips
  and **seven actual-host tests explicitly deselected**, in 330.89 seconds.
  The one warning is the existing `PDFImportConfig` deprecation.
- The two conflict-resolved test modules passed Ruff; the staged diff passed
  `git diff --check`.
- Source and merge commits were pushed to configured `origin/main` without a
  force push. The server reported its existing administrative bypass while
  five required CI checks were still expected. Local passes do not establish
  that those remote checks have completed.

No native FreeCAD or other CAD process was launched for these checks. The
seven deselected tests cover actual-host embedded-font delivery, image/raster
delivery, 3D-text performance, and item-level geometry. They remain pending for
the new package rather than being counted as passing tests.

## Usable Windows test package

`FreeCAD-PDF-Importer_v4.0.111-test-2026-10-11-r5.zip` is a local test build,
23,026,250 bytes, with 823 archive members. Its SHA-256 is:

```text
117b2d06294ed0c737434a901034fde309ba9c24f6939b9448883c01bb4f0f9c
```

The package is built from the exact committed source above. Only its staged
`package.xml` version changes to `4.0.111+test.20261011.r5`; the repository base
version remains 4.0.111. All selected first-party source files were checked
against Git object bytes. Every extracted member was checked against its ZIP
member. The extracted workbench is under `Installable-r5/PDFVectorImporter`,
with the ordinary `InitGui.py` layout, installation instructions, and notices.

The package's first-party Python content identity used by native resume is:

```text
ed4a0447604b58b89c1b70051dc20d6c3c6cf24682f0011560d0e0f16e17cc5b
```

The verified r4 offline runtime is reused byte for byte: PyMuPDF 1.28.2 and
FontTools 4.63.0 for Windows x64 CPython 3.10 and 3.11. Standalone Python
3.10.11 and 3.11.14 each compiled all 75 packaged first-party Python files,
imported the exact bundled dependency versions from the extracted package,
and passed 2,048 repeated text-trace extraction/disposal cycles. These are
dependency and syntax checks, not native host execution. The MIT importer and
FontTools notices and PyMuPDF/MuPDF licensing notices remain included.

Release privacy/link guards ran before capture. No private PDFs, credentials,
generated CAD evidence, test harnesses, or Git metadata are package payloads.
This local build does not alter production release gates or user workbenches,
profiles, and caches.

The r5 archive, `BUILD-INFO-r5.json`, and `Validation-r5` records are in the
local FreeCAD test-build folder. `START-HERE.txt` names the current installer.
Native import/save/reopen, all text modes, cancellation, and checkpoint resume
against the exact r5 package are **pending**. Historical r4 acceptance on
FreeCAD 0.21.2 and 1.1.4 still applies only to r4; its ZIP, extracted
`Installable` folder, and receipts were preserved. No universal PDF-fidelity or
unavailable-platform compatibility claim follows from these tests.
