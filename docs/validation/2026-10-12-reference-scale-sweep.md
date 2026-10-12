# FreeCAD reference-scale sweep, 2026-10-12

The sweep started from main
`45cc5ceed917159627f79ee66ac8f1d51197d114` (v4.0.113). Scale by Reference
opened an undo transaction and changed the drawing before recomputing it.
If the transform or recompute raised, the transaction remained open. Quick
Scale already aborted its transaction in those cases. Scale by Reference
now uses the same bounded error cleanup and re-raises the original error.
Its successful operation remains one undo step.

Two regression cases failed before the repair and passed afterwards: a
transform refusal and a final recompute refusal. All 56 scale-tool cases
passed, including picture, paper, label, grouping and successful undo-step
contracts. The fresh baseline full suite passed 2,539 tests and 83 subtests,
with 24 optional/environment checks skipped and the existing import-config
deprecation warning. Ruff and the shared-core manifest check passed.

Separate native probes used the actual FreeCAD App/Part libraries in the
0.21.2 conda Windows x64 Python 3.10 build and installed 1.1.4 Python 3.11.
Each probe scaled a synthetic native line, injected a failure at the
transform or recompute boundary, and verified the actual document aborted
without committing. A successful scale followed by native Undo also restored
the line. Complete sampled line payloads matched exactly: curve type,
origin/direction/parameters, both vertices, placement matrix, topology counts,
length, bounds, area, volume, orientation, validity and closure.

These checks delegate the native document's transaction APIs through a
wrapper solely to inject the error boundary. They do not assert that a real
PDF caused those particular errors, or establish GUI, package, arbitrary-CAD
or full PDF fidelity acceptance. The original raw BREP comparison failure
and before/after evidence remain preserved. FreeCAD can serialize an
equivalent identity location as an inverse identity; raw BREP byte equality
is reported as false where it changed. No product verification gate or
general BREP comparator was loosened.

The specific 0.21.2 conda Python 3.10 build also has separate earlier r6
native import/save/reopen evidence for all six requested text modes. Those
App/Part API results do not imply that the separate Python 3.8 Windows
installer is supported, or certify a complete desktop button journey.
Compatibility help now identifies that distinction, and the human checklist
uses the shipped Setup EXE rather than an Addon Manager listing that does
not exist.
