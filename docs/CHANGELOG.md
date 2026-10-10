# Change notes - PDF Vector Importer for FreeCAD

These notes used to sit at the top of the main [README](../README.md), in the
same order. Install steps and the feature list are in the README.

## Recent fixes (v4.0.107)

- Qualified round-cap ink, dash-dot dots, opaque image paint order, and source-bound Multiply display from the Codex draft now ship on this cut. Text modes stay in-mode; unproven appearance cases are reported.

## Recent fixes (v4.0.106)

- Glyphs and Geometry retry the bundled SVG renderer when Cairo's filter graph hides all glyph placements from the parser. Each recovered item still needs its original source binding; an unreadable graph is not evidence that the requested text representation is impossible.
- Short, solid round-cap strokes keep their source-sized editable ink faces, including point marks that screen-pixel line widths could lose. Source clipping, opacity, uniform transforms, and unique renderer matches must be verified before a face is added. The original centerline import remains unchanged.

These stroke footprints do not implement general PDF blending or image paint order. Those appearance limits remain subject to native visual review.

## Recent fixes (v4.0.105)

- Native Text and Labels preserve original character positions and font transforms, including after save/reopen, while remaining editable.
- 3D Text places each solid glyph at its original source position and scale, preserving stacked fractions and stretched or slanted text without fitting glyph ink to declared character advances. It uses filled source-colored display. Glyphs and Geometry retain editable source outlines with a thinner source-colored display.
- Verified final rectangle highlights preserve source transparency and physical border widths. A toggleable white paper display keeps dark ink readable without changing the application theme.
- Headless documents restore their saved visibility and display nodes when opened in the GUI. Concurrent imports use separate report folders.
- Original renderer character quads and expanded Raster coverage prevent false glyph shear and cropped letter edges. Wholly off-page paints are removed only when complete renderer bounds prove they are invisible.
- Embedded images and full-page rasters use unshaded source colors, preventing native lighting from turning white image backgrounds gray.

Native wire outlines and general vector lineweights remain screen-dependent; Raster text has finite resolution. Final rectangle highlights are handled only when their complete source paint contract is verified.

## Recent fixes (unreleased)

- Direct single-page imports now remove their own incomplete objects and
  delivery evidence when cancelled or stopped by an error. Pre-existing model
  objects and caller-owned transactions are retained. If cleanup cannot be
  verified, the error reports that explicitly instead of leaving a partial
  drawing that can be mistaken for a completed import.
- **One text item that cannot be delivered now costs that item, not the
  document.** The per-item fallback ladder advances past a rung that failed
  without proof, provided that rung's own attempt shows it removed every host
  object it created. When every rung is spent the item is returned as a
  degraded record instead of aborting the import, so the rest of the sheet is
  still delivered. (Owner directive 2026-09-19.)
- The failure classification is unchanged: every builder raises exactly the
  reason code it raised before, and it stays in `extra.text_delivery_attempts`.
  Only the consequence is different.
- The degrade is loud and certification stays strict. `extra.text_items_degraded`
  lists each degraded item (capped at 200, with the total and a truncated flag)
  with its requested type, every attempted rung and that rung's own reason,
  `proof_class`, the font identity, and the offending character index and
  codepoint where the failure names one. `result.warnings` counts them
  alongside clipped fills and host-font substitutions, one console warning is
  emitted per item, and the human summary says the import is not certified.
- `extra.text_source_spans` and `extra.text_representation_delivery` are now
  reported, so `import_contract_ready.ready` is **false** for any sheet with a
  degraded item. A page that degraded is named in
  `representation_contract_scope.uncertified_degraded_pages`, and the QA
  harness reports that sheet as `DEGRADED`, never `PASS`.
- Three classes remain document-fatal: an attempt whose cleanup is incomplete
  or that left unknown host objects behind, an item with no stable source
  identity, and any importer contract breach (missing deliverer, malformed
  impossibility proof, unverifiable delivery result). `ImportCancelled`
  propagates untouched.
- An item-scoped rollback now refreshes the page's native-text object index.
  FreeCAD recycles a removed object's name, and a stale index turned one
  rolled-back item into thousands of induced failures on a dense sheet.
- **Two root causes fixed, so the spans are delivered at the requested
  representation instead of degrading.** A `cmap` or `hmtx` table fontTools
  cannot decode is now reported as absent measurement data rather than a veto:
  ordinary PDF subset fonts ship an `hmtx` whose trailing side-bearing array
  was dropped while `hhea` still describes the full face, and the importer
  already measures the pen advance from the outlines when metrics are missing.
  The em-ink calibration reads the same outlines straight from `glyf` when
  fontTools' glyph set cannot be built for the same reason; it refuses that
  route for a variable or CFF font. Kerning values stay fail-closed, because a
  wrong kern moves glyph ink. Measured on a 183-span structural sheet in
  default 3D-text mode: from writing nothing at all to 183/183 native 3D Text,
  certified.
- The zero-advance escape in the source character layout now covers any
  combining mark, enclosing mark or variation selector, not only whitespace. An
  emoji followed by `U+FE0F` no longer makes an otherwise exact span
  undeliverable. Zero-width joiners and other format characters are
  deliberately not included.
- An unreadable character map is never reported as "this font has no glyph for
  that codepoint"; the two are now separate messages, and a genuinely missing
  glyph names its codepoint.
- **Text the PDF delivers as raw glyph codes is recovered where this tool can
  prove the characters, and reported either way.** A `/Type0` font with an
  Identity CMap and no `/ToUnicode`, over a subset program carrying no usable
  mapping of its own, leaves the content stream holding glyph indices that
  nothing in the file explains. The engine substitutes the index as the
  character, so a member mark arrives as `06-3` where the drawing says `MS-3`:
  already wrong, already legible, and invisible to any check that looks for
  control characters. The trigger is exactly that structure - a font with any
  real encoding, including the many that simply lack a `/ToUnicode`, is
  untouched.
- Characters are proven per character, stopping at the first route that
  succeeds: `embedded_cmap` (the subset's own `cmap`, reverse mapped),
  `post_glyph_name` (a real `post` table's names through the AGL; names
  fontTools synthesises from the glyph index are never accepted),
  `outline_identity` (the glyph's contour command list, hashed and matched for
  exact structural equality against a face-matched installed reference whose
  `/W` advance agrees), and `blank_glyph_advance` (a glyph that draws nothing,
  whose advance is a reference face's space). There is no fifth route: no
  offsets, no standard-glyph-order assumption, no encoding guesses. Where no
  reference face for the declared family is installed, the last two routes
  recover nothing and say which family they looked for.
- **Substitution is all-or-nothing per span.** One unproven character leaves
  the entire span byte for byte as it was delivered, because a half-read
  dimension reads as a measurement and is worse than raw codes.
- `extra.text_glyph_codes` (`bcs.text_glyph_codes/1.0`) records every affected
  span: the recovered ones with the route that proved them - never as if the
  PDF had declared them - and the unproven ones with the font, the page, the
  location and the raw codes, sorted first so the item cap cannot hide one. A
  character the engine itself resolved, and a space its layout inserted, are
  counted apart under `characters_left_as_delivered`. A span this run could not
  examine is reported as a limitation of the import, never as the sheet failing
  to say. `result.warnings` gains a term for unproven spans only: a span whose
  characters were proven is a clean delivery, stated once per import rather
  than warned about. A degraded-item row says when its `source_text` was
  recovered rather than read.
- Because the last two routes take their characters from an installed
  reference face rather than from the file, `text_glyph_codes` is worth reading
  on any sheet that reports one. `BCS_GLYPH_REFERENCE_FONTS` overrides the
  search with a path-separated list of directories or files, or the single word
  `none` to switch reference matching off entirely.

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
