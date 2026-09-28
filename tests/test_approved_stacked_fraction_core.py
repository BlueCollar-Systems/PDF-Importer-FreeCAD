from __future__ import annotations

import hashlib
import inspect
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = REPO_ROOT / "PDFVectorImporter" / "pdfcadcore"
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore import primitive_extractor  # noqa: E402


# This is the reviewed integration output: current main's fraction core plus
# the lossless performance changes and exact covered-clip contours and adjacent source-outline protection.
# The FreeCAD combined bytes were reviewed together; host-specific optional
# font-metadata differences from the other importers remain explicitly recorded.
# September 17 adds raw source RGB/alpha/finite paint order beside the unchanged
# composite colors. No fraction/layout/source-character proof was modified.
# September 19 preserves literal single-open-line endpoints before point
# cleanup, including zero-length and sub-cleanup-tolerance source strokes.
# September 20 adds six lines on the text path that call glyph_code_recovery
# before anything derives from the text dictionary. A span whose raw glyph
# codes are not proven is left byte for byte as MuPDF delivered it, so no
# fraction predicate, layout rule or source-character proof changed here.
REVIEWED_COMBINED_SUCCESSOR_SHA256 = (
    "85d7159f6fd294846528b872fef5f375af4d8a0ec0e44ff949dc34ae8e74a1f9"
)


def test_shared_core_matches_reviewed_combined_successor() -> None:
    raw = (CORE_ROOT / "primitive_extractor.py").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == REVIEWED_COMBINED_SUCCESSOR_SHA256


def test_obsolete_stacked_fraction_scale_clamp_is_absent() -> None:
    source = inspect.getsource(primitive_extractor)
    assert "_FRAC_STACKED_SCALE" not in source
    assert "font_size * 0.6" not in source
