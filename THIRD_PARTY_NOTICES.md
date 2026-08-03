# Third-Party Notices

This package bundles third-party runtime components so the importer can run on a
PC without a system-wide Python/PyMuPDF install. The components below are
redistributed under their own licenses; if you redistribute this package,
preserve these notices and comply with the applicable terms.

## PyMuPDF / MuPDF

- Project: PyMuPDF (bindings) over MuPDF (Artifex)
- Bundled version: PyMuPDF 1.28.0 (shared stable-ABI payload at `PDFVectorImporter/src/lib/common/`)
- Upstream: https://github.com/pymupdf/PyMuPDF
- License model: **AGPL-3.0-or-later OR Artifex commercial license**
  (verified in `PDFVectorImporter/src/lib/common/pymupdf-*.dist-info/METADATA`:
  "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License")
- Note: AGPL-3.0 carries source-availability obligations on distribution. Either
  obtain the Artifex commercial license, or ensure corresponding source for the
  bundled MuPDF/PyMuPDF version is made available (or a written offer is
  provided) per AGPL-3.0 §6. This is a compliance item for the product owner /
  counsel, not legal advice.

For complete metadata in this package, see:

- `PDFVectorImporter/src/lib/common/pymupdf-*.dist-info/METADATA`
- `PDFVectorImporter/src/lib/common/pymupdf-*.dist-info/` (license/COPYING files)

## fontTools

- Project: fontTools 4.63.0
- Bundled payloads: exact Windows cp310 and cp311 wheels under
  `PDFVectorImporter/src/lib/cp310/` and `src/lib/cp311/`
- License: MIT (preserve the license metadata shipped in each wheel)

## Auditing what is bundled

A machine-readable manifest of every bundled binary (path, version, license,
SHA-256) can be regenerated from the private dependency-audit tooling:

```
python tools/dependency_audit.py
```

## Inno Setup build toolchain

Windows Setup.exe is compiled in CI with Inno Setup 6.7.1 from the official
JRSoftware release. The compiler is a build-time tool and is not bundled in the
product ZIP or Setup.exe. `installer/inno-toolchain-6.7.1.json` records the
official distribution SHA-256, pinned Authenticode signer identity, Inno Setup
License label, and the exact portable compiler-tree hashes used by the build.
