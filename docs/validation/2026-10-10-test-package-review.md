# Local installable test package - 2026-10-10

The owner requested usable packages for local testing. The final FreeCAD ZIP is
`C:/BlueCollar-Test-Builds/2026-10-10/FreeCAD/FreeCAD-PDF-Importer_v4.0.111-test-2026-10-10-r4.zip`
(23,012,496 bytes), SHA-256
`4687272201d0dc625f72d03f36aca29c6cd3dced5a635baad1bb0578dd084e02`.
`START-HERE.txt` contains plain installation, initial import, resume, and rollback
steps. Its extracted `PDFVectorImporter/InitGui.py` layout is ready to copy into
FreeCAD's user Mod directory. Earlier candidate archives/hashes are preserved
under `Superseded`, leaving one current installer at the folder's top level.
Building and testing did not replace the user's installed workbench, modify
original PDFs, or publish a release.

The snapshot includes the current shared font-program/spacing guards and the
workflow changes described in the earlier review. It also fixes checkpoint
identity: session schema 1.1 binds the actual bounded first-party Python bytes,
rather than accepting only a matching version. Changes retaining version, file
size, and modification time cannot reuse previous completed-page claims. Code
traversal errors and linked first-party directories/files fail closed; vendor
and generated-cache trees remain excluded. Legacy 1.0 checkpoints are clearly
rejected with existing output preserved. Start a new import, or resume with the
checkpoint's original importer.

Native FreeCAD 0.21.2 testing also reproduced an upstream C argument-parser bug
in `optimalBoundingBox`: passing optional booleans can corrupt Python's bool
type. `PDFPageTextClip.py` chooses the old-host branch before that unsafe call,
requires native analytic surfaces/curves, obtains an owned `cleaned()` shape
whose cached triangulation is removed, and calls the bounding method with no
arguments. Modern hosts retain explicit analytic/tolerance-disabled arguments.
Original geometry/placement and all finite, containment, and ink-conservation
checks remain unchanged. The upstream implementation is available in the
official [FreeCAD 0.21.2 source](https://raw.githubusercontent.com/FreeCAD/FreeCAD/0.21.2/src/Mod/Part/App/TopoShapePyImp.cpp).

Only the staged package metadata uses `4.0.111+test.20261010`; the repository
version and official release gate were preserved. The package is a working-tree
snapshot, including subsequent edits and externally created commits; this
review made no commits or pushes. `BUILD-INFO.json` binds the shipped shared
files, utility workflow, core, and session module by SHA-256.

The Windows offline bundle contains official hash-locked PyMuPDF 1.28.2 ABI3
and FontTools 4.63.0 wheels for Python 3.10 and 3.11. The installed runtime
manifest and wheel tags were regenerated/verified without shipping the unused
MuPDF development SDK. Upstream license notices accompany the included source.
The production commit-binding build gate was not weakened for a local test ZIP.

Validation:

- The final session/progress/report regression set passed **36 tests**, including
  same-size/same-mtime code edits, legacy checkpoints, walk errors, and directory
  links. Ruff and diff checks passed.
- The final source-page-clip plus code-identity checks passed **24 tests**,
  including pre-call legacy routing, owned-clean bounds, and polygon-only
  rejection. These preserve the existing clipping conservation cases.
- The actual ZIP passed the repository's release ZIP smoke under official
  CPython **3.10.11** and FreeCAD's bundled **3.11.14**. Both runtime ABIs had
  already passed provenance, dependency-version, and 2,048 texttrace lifetime
  checks with natural process exit.
- CPython 3.10.11 compiled **75 first-party files** from the final ZIP. Every
  extracted ZIP member matched the archive bytes after native validation.
- Native FreeCAD **0.21.2 / Python 3.10.13** and **1.1.4 / Python 3.11.14**, using
  only the isolated extracted final importer and bundled runtime, each accepted
  all six modes and one private shop drawing with zero degraded text and ready
  reports. Final mode rows were recorded afresh on both hosts.
- The final package rejected pages 0/out-of-range/nonnumeric without document
  changes, normalized reversed duplicate ranges, cancelled partway through page
  2 while retaining page 1, and resumed to pages 1 and 2 with a ready report.
- A real native same-version, same-size, same-mtime edit to a first-party module
  was rejected by named resume. Native object names, shape vertex/edge counts,
  volumes, code identity, and completed-page claims remained unchanged. Restoring
  the original module allowed the same checkpoint to resume successfully.
- A fresh legacy process also exercised analytic bounds on a coarse-meshed,
  rotated native cylinder. Owned clean bounds differed from cached display
  bounds; source geometry/display, native measures, and placement were preserved.
  Importer font caches were asserted under isolated scratch user data. Earlier
  probes used the native user-cache resolver before that harness correction;
  existing user caches were preserved, and no installed source files replaced.

After native acceptance, only installation notes and top-level test metadata
were updated to report the now-verified 0.21.2 host. Every byte under
`PDFVectorImporter/` was compared with the accepted archive and remained
identical. The final CRC and archive SHA were checked; no source/native suite
was repeated for that documentation-only correction. The earlier archive is
preserved under `Superseded` with its checksum.

Full local evidence and generated native sample documents are in the package's
`Validation` folder; private drawing reports remain under `C:/TMP`. The native
SVG check deliberately used the bundled MuPDF renderer instead of requiring
Poppler. These selected fixtures establish their recorded delivery contracts,
not every PDF, hardware configuration, or a pixel-for-pixel GUI comparison.
Python 3.12+, macOS/Linux offline binaries, and hosts before the declared FreeCAD
0.21 minimum are outside this Windows package. FreeCAD 1.0 and other releases
were not separately tested natively; no acceptance is inferred for every host
from these two versions or from Python-only checks.
