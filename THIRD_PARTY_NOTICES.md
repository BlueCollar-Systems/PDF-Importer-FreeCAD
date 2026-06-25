# Third-Party Notices

This package bundles third-party runtime components so the importer can run on a
PC without a system-wide Python/PyMuPDF install. The components below are
redistributed under their own licenses; if you redistribute this package,
preserve these notices and comply with the applicable terms.

## PyMuPDF / MuPDF

- Project: PyMuPDF (bindings) over MuPDF (Artifex)
- Bundled version: PyMuPDF 1.27.2.3 (vendored at `PDFVectorImporter/src/lib/`)
- Upstream: https://github.com/pymupdf/PyMuPDF
- License model: **AGPL-3.0-or-later OR Artifex commercial license**
  (verified in `PDFVectorImporter/src/lib/pymupdf-*.dist-info/METADATA`:
  "Dual Licensed - GNU AFFERO GPL 3.0 or Artifex Commercial License")
- Note: AGPL-3.0 carries source-availability obligations on distribution. Either
  obtain the Artifex commercial license, or ensure corresponding source for the
  bundled MuPDF/PyMuPDF version is made available (or a written offer is
  provided) per AGPL-3.0 §6. This is a compliance item for the product owner /
  counsel, not legal advice.

For complete metadata in this package, see:

- `PDFVectorImporter/src/lib/pymupdf-*.dist-info/METADATA`
- `PDFVectorImporter/src/lib/pymupdf-*.dist-info/` (license/COPYING files)

## Auditing what is bundled

A machine-readable manifest of every bundled binary (path, version, license,
SHA-256) can be regenerated from the shared corpus tool:

```
python C:\1pdf-test-corpus\tools\dependency_audit.py
```
