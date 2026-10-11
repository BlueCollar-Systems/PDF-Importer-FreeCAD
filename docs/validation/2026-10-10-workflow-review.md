# Workflow review - 2026-10-10

## Changes made in this review

`PDFVectorImporter/PDFTools.py` no longer invents 9,999 pages when a PDF cannot
open. Utility selections are checked against the actual PDF before allocation;
invalid entries reject with clear errors, reversed ranges work, and duplicate
selections import once. Recursive batches prune generated import-evidence
folders instead of re-importing copied QA inputs.

Batch Import now has a progress dialog and a Cancel button connected to the
core's object-creation heartbeats. Cancelling stops subsequent work and keeps
completed pages. A bad file does not stop the remaining batch. Only an explicit
successful core result, or pages confirmed complete in a cancelled session,
count as completed; a false/absent failure is reported. Selected pages now share
one core invocation per PDF, avoiding repeated font/outline-cache rebuilding,
allowing deferred recomputes, and preserving the chosen multi-page layout.
Import via Console likewise no longer prints success after a failed or cancelled
result.

`tests/test_utility_page_selection_fc.py` and `tests/test_batch_workflow_fc.py`
exercise page validation, unreadable PDFs, huge-range rejection, evidence
pruning, corrupt-file isolation, false-result accounting, and callback
cancellation. README usage now describes these behaviors. No shared
`pdfcadcore` files were changed.

## Validation

- Initial complete suite with an explicit writable temporary directory:
  **2,272 passed, 21 skipped, 11 subtests passed**, with one existing deprecation
  warning (223.64 seconds).
- The new focused cases plus existing file-enumeration checks: **20 passed**.
- Repeat complete suite: **2,293 passed, 21 skipped, 11 subtests passed**, with
  the same deprecation warning (359.52 seconds), including concurrently authored
  stroke-subpath cases. The final per-PDF batch optimization was made after this
  run loaded its utility module. Its final validation was **31 focused tests
  passed**, including existing progress/resume contracts, plus clean Ruff.
- Before the fix, the new page/evidence cases reproduced **11 failures / 3
  passes**; the huge-range test was intentionally excluded from that unfixed
  run to avoid allocating an enormous range.
- Ruff on the changed Python files: passed.
- `python pdfcadcore_sync_check.py`: passed.
- Initial pytest runs encountered Windows ACL errors in the machine's existing
  default temporary directories. The complete run used
  `--basetemp=C:/TMP/audit-freecad-20261010-01`; no fidelity assertion was weakened.

Native **FreeCAD 1.1.4**, using its bundled Python 3.11 and actual FreeCAD/Part
libraries, imported an exact-font synthetic fixture in all six text modes.
Each returned explicit success, reported zero degraded text items, and wrote
`import_contract_ready.ready=true`. One private shop drawing produced 74 native
objects with zero degraded text and a ready report. Evidence is under
`C:/TMP/pdf-import-audit-20261010/`, including `native-freecad.json` and the
per-mode reports. The compact native result snapshot is retained as
`2026-10-10-native-probe.json` beside this record.

## Final shared-core review

The shared font-binding changes were reviewed separately from the initial
utility changes above. Same-name font programs are identified by their exact
original program SHA and character occurrence. Missing printable paint traces
remain unverified. An inferred U+0020 space is accepted only with the native
MuPDF synthetic flag, a unique original occurrence without a painted trace,
and an empty space glyph in the original exact font program. It retains its
source quad and semantic gap with no invented painted glyph ID. Visible or
missing original space glyphs remain rejected. The CFF proof uses the existing
original CFF wrapper directly; an empty PDF Unicode census cannot gate that
nonpainting proof.

After the synthetic-spacing files and tests were synchronized, **270 focused
font, positioned 3D text, font staging, zero-extent glyph, and glyph-fill tests
passed** (39.38 seconds). The real Helvetica A-gap-B extraction probe retained
native synthetic flags 532, exact font binding, and no paint glyph ID for the
inferred space. The shared-core synchronization check passed.

Native FreeCAD 1.1.4, using its bundled Python 3.11, actual FreeCAD/Part
libraries, and the source checkout, also imported that new synthetic-spacing
fixture in all six text modes. Every mode returned explicit success, reported
zero degraded text, and wrote `extra.import_contract_ready.ready=true`.
The synthetic-only result snapshot is
`2026-10-10-synthetic-spacing-probe.json` beside this record; full temporary
reports remain under `C:/TMP/pdf-import-audit-20261010/`.

The final competing-paint guard also rejects a synthetic space when exactly
one painted occurrence shares its identity/origin, and clears any stale trace
ID. The settled source-font regression file passed **28 tests** (10.00 seconds)
after that final guard was synchronized. The native spacing fixture has no
competing paint at its inferred space, so this tightening preserves its
recorded acceptance. The earlier broad and native runs precede this last
negative-case change.

This review did not create commits. Concurrent workspace activity committed
the earlier workflow changes as `626f676`; the final synthetic-spacing changes
were reviewed as subsequent source checkout edits. No installed workbench or
release artifact was replaced by these checks.

## Boundaries and remaining work

The initial review above covered **source checkout changes**. The subsequent
owner-authorized installable test snapshot and native extracted-package checks
are recorded in `2026-10-10-test-package-review.md`. No installed workbench was
replaced and no publication was performed. The minimum host
remains FreeCAD 0.21. Windows offline bundles currently cover Python 3.10/3.11;
other host Python versions still require separately available dependencies.
Legacy FreeCAD 0.19/0.20 support is not established by this run. For comparison,
the current Blender host used elsewhere in this review is 5.2.2 LTS, documented
on the official [Blender download page](https://www.blender.org/download/).

The native checks verify recorded delivery contracts for selected inputs. They
do not certify every PDF, every host release, or a pixel-perfect GUI comparison.
The batch GUI transport was exercised with lightweight widget adapters; a human
interactive usability pass remains useful on the supported host versions.

Concurrent edits to `src/PDFImporterCore.py` and
`tests/test_disconnected_stroke_subpath_fc.py` were observed and preserved. They
address disconnected stroke subpaths and are separate from this utility-workflow
change.
