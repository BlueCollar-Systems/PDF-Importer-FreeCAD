# PDF Vector Importer for FreeCAD

**BUILT. NOT BOUGHT.**

![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)
![Version: 4.0.90](https://img.shields.io/badge/Version-4.0.94-blue.svg)
![Platform: FreeCAD 0.21+](https://img.shields.io/badge/Platform-FreeCAD%200.21%2B-orange.svg)

Import vector geometry, text, and images from PDF files into FreeCAD as editable Part objects.

Arc reconstruction, dash mapping, color grouping, OCG layer support, and reference-based scaling -- all powered by pure-Python PDF parsing via PyMuPDF.

> BlueCollar Systems -- BUILT. NOT BOUGHT.

## Recent fixes (v4.0.87)

- Corrupt embedded font cmap staging (e.g. Arial Italic) is treated as unusable
  embedded program evidence so exact Windows system-font resolution can still
  deliver in-mode 3D Text (CMJ page-31 canary).
## Recent fixes (v4.0.86)

- 3D Text now treats a font missing from a completed embedded-font inventory as
  proven absence, allowing the finite fallback ladder to continue instead of
  aborting the import as though staging had failed.
- Corrupt, incomplete, or malformed font-staging evidence remains fail-closed,
  and regression fixtures no longer expose private corpus identifiers.

## Recent fixes (v4.0.85)

- Mixed text delivery is now reconciled by exact source-item identity as well as
  representation type. Every verified fallback item must have its own matching,
  proof-gated fallback record; proof for one item can no longer authorize an
  unproven peer that happens to use the same fallback representation.
- Mixed-delivery reports fail closed when their terminal item ledger is missing,
  duplicated, internally conflicting, or inconsistent with delivered buckets.
- Proof identity, terminal host evidence, and the complete multi-page source
  roster are cross-checked for mixed and homogeneous fallback delivery,
  including source-free pages represented by an exact page-level raster item.
- Resumed-import reports identify the exact pages evaluated in the current
  invocation and explicitly exclude earlier certified pages whose proof
  telemetry is not repeated, preventing session-wide overclaiming.
- Cancellation and terminal-failure cleanup now roll back page-result telemetry
  with the removed host objects and identify every evaluated page whose effects
  were discarded.

## Recent fixes (v4.0.84)

- Import reports now reconcile every concrete type inside an aggregate `mixed`
  text delivery. Requested types and item-specifically proven fallbacks pass;
  any unproven subtype still fails closed. This prevents exact mixed glyph,
  geometry, raster, or native-text delivery from being falsely rejected.

## Recent fixes (v4.0.83)

- Exact 3D Text preserves leading/trailing whitespace with FreeCAD-native pen
  advance and ink-origin verification. It tries the optimized compound path and
  the ShapeString path before any evidence-backed cross-mode fallback.
- Glyphs now stores ordered per-character subshapes inside one source-item
  compound, with glyph IDs/count/grouping in metadata. This prevents oversized
  FCStd archives; individual glyphs remain addressable as subshapes rather than
  separate tree objects.
- SVG text conversion renders every selected page from one verified immutable
  import-run PDF snapshot, so source changes cannot mix bytes between pages.
- Windows releases bundle one shared PyMuPDF payload plus exact CPython 3.10 and
  3.11 fontTools runtimes; the host activates only its matching ABI tree.
- Release packaging is `HEAD`-bound and fails closed on private paths,
  generated corpus artifacts, and identifiers supplied through the masked
  external private denylist.
- Import reports now publish an explicit `ok` or `warn` scale evaluation;
  missing or malformed scale evaluations remain fail closed for consumers.

## Recent fixes (v4.0.82)

- MuPDF's measured 24-character span-font truncation is now the only prefix
  case eligible for unique staged-font recovery. Longer untruncated names must
  match exactly, preserving fail-closed font identity while completed staging
  resolves genuine truncation before 3D text delivery.

## Recent fixes (v4.0.81)

- Exact staged-font matching now recognizes MuPDF's measured 24-character
  span-name truncation only when one unique longer staged font proves the
  identity. Ambiguous and untruncated prefix matches still fail closed, so the
  fix restores affected Noto symbol text without risking Arial/Arial Narrow or
  other wrong-font substitutions.

## Recent fixes (v4.0.80)

- Interactive imports now show content-derived work estimates and one truthful
  whole-import progress dialog. Cancel removes the incomplete active page,
  commits only completed pages, and records an exact resumable session in the
  FreeCAD document; save the document to resume after restarting FreeCAD.
- Resume requires the same PDF SHA-256, content-affecting options, importer
  package version, and requested page order. It imports only unfinished pages
  and derives placement from the original multi-page layout.
- Setup.exe is built twice with the exact attested Inno Setup 6.7.1 portable
  tree. CI verifies the official installer hash and pinned Authenticode signer,
  then publishes the canonical ZIP, byte-reproducible Setup.exe, and their
  deterministic attestation in one atomic release operation.
- Existing exact tags converge safely to their missing Release object without
  tag rewriting or asset clobbering. Post-release digest records are scoped to
  `release-bookkeeping/` and carry `[skip release]` by construction.

## Recent fixes (v4.0.79)

- Type3 fonts with no extractable embedded program now produce an exact,
  item-scoped fallback reason without poisoning unrelated text on the page;
  malformed non-Type3 font records still fail closed.
- Unicode Windows paths are delegated directly to PyMuPDF after a bounded
  header check, avoiding a second whole-PDF memory copy on older hardware.
- Heavy-page QA runs are complexity-gated before host objects are created and
  use source-bound page checkpoints, so acceptance cannot report unfinished
  pages as complete.
- Release builds now reject private PDF/CAD/model/report/archive artifacts,
  dirty or untracked package inputs, and stale checkout dependencies. Every
  ZIP vendors fresh hash-locked runtime wheels in an isolated staging tree.

## Recent fixes (v4.0.78)

- Windows `/SILENT` and `/VERYSILENT` installs now terminate unattended; the
  post-install completion notice is shown only during an interactive install.

- Windows ZIP and Setup.exe assets are now built from one canonical payload
  and published together, allowing future releases to be immutable without a
  second workflow appending files after publication.

- Release ZIPs are now reproducible and omit unused Python console launchers
  and wheel records that embed build-machine paths. The shipped runtime stays
  self-contained while every published byte can be verified locally.

- Font and raster caches now verify that FreeCAD's user Mod directory is
  writable and automatically use a temporary cache when it is not. This keeps
  exact 3D text and hybrid embedded-image delivery working on locked-down PCs
  without setup commands or manual dependency installs.

## Structural Steel Shape Assets

The former standalone `Structural-Steel-DXF-DWG-Shapes` repository has been
consolidated here under `steel_shapes/` so the FreeCAD
importer repo is the source home for the DXF/DWG steel shape packs. The
versioned release ZIP from that old repo is intentionally not stored here;
GitHub Releases remain the download layer, while this repo keeps the source
assets, generation scripts, checksums, license, and notes.

## Key Features

| Category | Capability |
|----------|-----------|
| PDF Parsing | PyMuPDF-powered vector extraction with full path, text, and image support |
| Import Modes | Auto (default), Vector, Raster, Hybrid — every mode targets maximum fidelity (BCS-ARCH-001) |
| Text Rendering | 3D Text (default visual-parity path), Labels, Glyphs, Geometry — orthogonal to mode |
| Arc Reconstruction | Kasa algebraic circle fit converts polyline segments back to true arcs |
| Layer Support | OCG layers (PDF Optional Content Groups) map to FreeCAD groups |
| Color Grouping | Geometry automatically organized by stroke/fill color |
| Dash Patterns | PDF dash arrays mapped to FreeCAD line styles |
| Text Import | PDF text extracted with font size, position, and rotation |
| Image Import | Embedded raster images extracted and placed at correct coordinates |
| Scale Detection | Reference-based scaling from known dimensions on the drawing |
| Steel Detection | Recognizes common structural steel shape profiles |

For Adobe-like visual sign-off, start with **3D Text** at the same zoom/scale as
the source PDF. Use **Labels** when editable FreeCAD text matters more than
model-space PDF appearance, and use **Glyphs/Geometry** when exact outline
geometry is preferred over editability.

## Installation

See **[INSTALL.md](INSTALL.md)** for Windows FreeCAD 1.1 paths, dev junction install, and troubleshooting.

**FreeCAD 1.1 Mod path:** `%APPDATA%\FreeCAD\v1-1\Mod\PDFVectorImporter` (not legacy `FreeCAD\Mod\`).

**Dev one-liner (junction to repo):**
```powershell
.\installer\install-dev.ps1
```

### From FreeCAD Addon Manager (Recommended)
1. Open FreeCAD → **Tools** → **Addon Manager**
2. Search for **PDF Vector Importer**
3. Click **Install**
4. Restart FreeCAD

### Windows Setup.exe (Easy Manual Install)
1. Download `FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe` from Releases.
2. Close FreeCAD.
3. Run the installer (no admin rights required).
4. Restart FreeCAD.

### Manual Installation
1. Clone this repository:
   ```bash
   git clone https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD.git
   ```
2. Copy the `PDFVectorImporter` folder into your FreeCAD Mod directory:
   - **Windows (FreeCAD 1.1):** `%APPDATA%\FreeCAD\v1-1\Mod\`
   - **Windows (FreeCAD 0.21):** `%APPDATA%\FreeCAD\Mod\`
   - **macOS:** `~/Library/Application Support/FreeCAD/Mod/`
   - **Linux:** `~/.local/share/FreeCAD/Mod/`
3. Release ZIP/Setup installs bundle an offline runtime matrix under `PDFVectorImporter/src/lib`: shared PyMuPDF in `common/` and ABI-specific fontTools in `cp310/` and `cp311/`. Source checkouts can use **PDF Vector Importer > Install / Update PDF Dependencies** for the current FreeCAD user.
4. Restart FreeCAD

## Building Release Artifacts

### Build Addon ZIP
```bash
python build_release.py --python310 C:\path\to\python310.exe --python311 C:\path\to\python311.exe
```

Release builds are fail-closed and must run from a Git checkout. Every
shippable addon source file must be tracked and byte-identical to `HEAD`;
private PDF/CAD/model inputs, generated import reports, corpus folders, and
nested archives abort the build. Set the masked
`BCS_PRIVATE_RELEASE_DENYLIST_B64` environment value to the project-owned
base64 JSON denylist before a local build; automation reads it from the
`FREECAD_PRIVATE_RELEASE_DENYLIST_B64` repository secret and fails closed when
it is absent or malformed. The ignored `PDFVectorImporter/src/lib`
tree is deleted and rebuilt on every release build from the exact wheel hashes
in `requirements-release-common.lock` and the `cp310`/`cp311` locks, so an
importable but stale local runtime cannot enter the ZIP. Supply exact Windows
CPython 3.10 and 3.11 interpreters; the builder validates each before installing
its ABI-specific wheel. `--no-vendor-deps` intentionally
refuses release creation because ignored runtime bytes are not commit-bound.

The secret decodes to this schema; use only synthetic values in documentation:

```json
{"schema":"bcs.private-release-denylist/1.0","terms":["SYNTHETIC-PRIVATE-DRAWING-ID"]}
```

### Build Windows Installer (.exe)
1. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php)
2. Run:
   ```bash
   python build_windows_installer.py
   ```
3. Output files are written to `dist/`:
   - `FreeCAD-PDF-Importer_vX.Y.Z.zip`
   - `FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe`

### Auto-Release Mint on `main` Pushes
The `auto-release` workflow mints a `vX.Y.Z` release automatically when a
push to `main` carries a version bump. The `auto-release` workflow builds and publishes both artifacts
atomically before the release becomes immutable. Two deliberate guards apply:

- Docs-only pushes never mint: the workflow ignores `**/*.md`, `docs/**`,
  and archive paths (board Q-08-a / ANS-09-1).
- Commits marked `[skip release]` (docs/test/CI-only by convention) skip
  the release job entirely.

**Escape hatch (canonical):** if a release commit's only diff is markdown —
for example a README badge correction that must still ship as a release —
the push cannot trigger `auto-release`. Mint it manually instead:

```bash
gh workflow run auto-release.yml
```

The dispatched run re-reads the committed version, runs the full release
gates, and mints the tag exactly as a push-triggered run would (this is how
v4.0.67's badge correction shipped).

## Free Structural Steel Shapes (CC0)

This repository also hosts the public-domain AISC v16.0 DXF/DWG shape packs
previously distributed from `Structural-Steel-DXF-DWG-Shapes`.

| Location | Contents |
|----------|----------|
| [`steel_shapes/dxf/`](steel_shapes/dxf/) | 14 family DXF packs |
| [`steel_shapes/dwg/`](steel_shapes/dwg/) | 14 family DWG packs |
| [`steel_shapes/source/`](steel_shapes/source/) | AISC CSV + generation scripts |
| [`steel_shapes/README.md`](steel_shapes/README.md) | Usage, license, checksum notes |
| [`steel_shapes/ATTRIBUTION.md`](steel_shapes/ATTRIBUTION.md) | Merge provenance from the former standalone repo |

**Releases:** tag `steel-v1.0.0` (etc.) to publish
`Structural-Steel-DXF-DWG-Shapes-*.zip` via the `steel-shapes-release` workflow.
PDF Importer addon releases continue to use `v4.x.x` tags.

## Usage

1. Open FreeCAD
2. Go to **File** → **Import** or use the **PDF Vector Importer** workbench
3. Select a PDF file
4. Choose an import **mode** (Auto is the default and works for most files)
5. Choose a **text rendering** option
6. Click **Import**

## Import Modes (BCS-ARCH-001)

Every mode targets **indistinguishable-from-source** fidelity within FreeCAD's
capabilities. Modes differ only in extraction *strategy* for different input
types, not in quality tier.

| Mode | When to Use |
|------|-------------|
| **Auto** *(default)* | Let the importer analyze the PDF and pick the right strategy per page. Reports what it chose. |
| **Vector** | Clean vector PDFs (CAD exports, shop drawings, engineering drawings). |
| **Raster** | Scanned or image-only PDFs. Places the page as a high-DPI image. |
| **Hybrid** | Mixed content: vectors where clean, raster where vector extraction would be lossy. |

## Text Rendering (orthogonal to mode)

| Option | Result |
|--------|--------|
| **Labels** | FreeCAD-native text objects, editable as text |
| **3D Text** | Extruded geometric text (Draft ShapeString) |
| **Glyphs** | Per-character vector glyph subshapes grouped by source text item |
| **Geometry** | Text converted to non-editable geometry |

Plus a separate **Import text** toggle to skip text entirely.
Glyphs and Geometry prefer Poppler/pdftocairo SVG output when available, then fall back to bundled PyMuPDF SVG paths.

### Text-representation contract (TEXTMODE-1)

**The requested text mode is the delivered text mode.** Alignment, rotation,
or scaling defects are fixed *inside* the requested mode — never by
substituting a different mode. Substitution is permitted only when the
requested mode is genuinely impossible for the exact source item. A generic
exception, a missing helper, an empty result, or a visual defect is not proof
of impossibility. Any authorized substitution must walk the closest remaining
representation first and is recorded per source item in `import_report.json`
with the attempted types, exact created/removed host IDs, cleanup result, and
the evidence that proved the requested type impossible. It is never silent.
(Owner directive 2026-07-13.)

Authorized order after item-specific proof (left rung first):

| Requested | Ladder |
|-----------|--------|
| **3D Text** | Glyphs → Geometry → Labels → Raster |
| **Glyphs** | Geometry → 3D Text → Labels → Raster |
| **Geometry** | Glyphs → 3D Text → Labels → Raster |
| **Labels** | 3D Text → Glyphs → Geometry → Raster |
| **Raster** | terminal — always achievable |

Notes:
- **Glyphs and Geometry are distinct deliverables.** Glyphs preserve ordered
  per-character outline subshapes and identity metadata inside one source-item
  compound; Geometry exposes raw edge entities. Sharing an SVG source does not
  make the host representations interchangeable.
- Renderer or font failures stop the transaction unless the failed source item
  has item-specific impossibility evidence and an implemented, verified next
  rung. They never authorize a whole-page or whole-mode substitution.
- Automatic raster classification may add a raster background, but it does not
  discard an explicitly requested text representation. Explicit Raster remains
  raster-only.
- The invariant is "requested type delivered and verified, or an exact failed
  attempt is reported and the transaction stops, or a proof-gated per-item
  fallback is reported." It is locked by
  `tests/test_textmode1_invariant_fc.py` and
  `tests/test_freecad_representation_contract.py`.

## Compatibility

See **[COMPATIBILITY.md](COMPATIBILITY.md)** for the full matrix. Summary:

| FreeCAD Version | Python | PyMuPDF | Status |
|----------------|--------|---------|--------|
| 0.21.x | 3.10 | 1.28.0 bundled offline | ⚠️ Expected |
| 1.0.x | 3.11 | 1.28.0 bundled offline | ⚠️ Expected |
| 1.1.x | 3.11 | 1.28.0 bundled offline | ✅ Verified |
| Any host using 3.12+ | 3.12+ | System/user install only | ⚠️ No bundled offline runtime |
| 0.19–0.20 | 3.8–3.9 | legacy pin | ⚠️ Expected only after legacy branch testing |
| 0.18 and earlier | | | ❌ Not supported |

Evidence levels:
- `✅ Verified`: host-run validation evidence captured.
- `⚠️ Expected`: syntax/runtime compatible but no host-run evidence yet.
- `❌ Not supported`: outside maintained/tested compatibility scope.

## Requirements

- **FreeCAD** 0.21 or later
- **Python** 3.10 or 3.11 for the bundled offline Windows runtime. Other source hosts may use compatible system/user packages.
- **PyMuPDF** `1.28.0` in the release runtime’s shared `src/lib/common` tree. When Poppler/pdftocairo is absent, it also backs Glyphs/Geometry text rendering.
- **fontTools** `4.63.0` in the exact `src/lib/cp310` or `src/lib/cp311` tree selected from FreeCAD’s embedded Python ABI. The incompatible sibling tree is never added to `sys.path`.

## Known Limitations

| Limitation | Details |
|-----------|---------|
| Encrypted PDFs | Password-protected PDFs must be unlocked before import |
| Compression filters | Decoding is delegated to PyMuPDF. Malformed or non-standard compressed object streams may fail to parse |
| Raster-only scans | Pure raster PDFs produce no vector geometry |
| Clipped/XObject-heavy PDFs | Complex clip stacks and deeply nested form XObjects can produce partial geometry |
| Very large PDFs | Documents with >10,000 primitives may slow the import process |
| Embedded subset fonts | Valid embedded subsets are staged for native 3D Text; malformed font programs or missing/invalid character maps can still require an item-specific fallback or truthful failure |
| Legacy hosts | FreeCAD versions older than 0.21 are not part of current validation coverage |

Large interactive imports show drawing-operation, text-character, and image-instance
counts before host-object creation. Cancellation keeps only certified pages. Resume
is persisted in the `.FCStd` document, so saving after cancellation allows a later
session to continue; unsaved process crashes are not claimed as recoverable.

## Import report / scale trust

Imports write `<output>_import_report.json` with `extra.resolved_scale` when detected.

- Use `factor` for scaling **only when** `confidence >= 0.70` **and** `fallback_reason` is not `no_scale_detected`.
- Otherwise treat scale as unknown.

## Bad-PDF open gate

FreeCAD refuses bad PDFs at open time (**fail closed**). SketchUp uses the same user-facing messages but may proceed on rare gate-internal errors (**fail open**). Compare `fallback.reason` per host rather than assuming identical refusal behavior.

## License

MIT License — see [LICENSE](LICENSE) for details.

Copyright (c) 2024-2026 BlueCollar Systems
