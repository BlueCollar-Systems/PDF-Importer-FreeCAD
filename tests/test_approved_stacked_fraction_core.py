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
# The combined bytes were reviewed together and match the Blender and LibreCAD
# shared cores; ordinary paths and the approved fraction logic are unchanged.
# September 17 adds raw source RGB/alpha/finite paint order beside the unchanged
# composite colors. No fraction/layout/source-character proof was modified.
REVIEWED_COMBINED_SUCCESSOR_SHA256 = (
    "19649d27cd5e860a243cbbe429a20dfe7d56aebb501335cc53d4482f427cd595"
)


def test_shared_core_matches_reviewed_combined_successor() -> None:
    raw = (CORE_ROOT / "primitive_extractor.py").read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(raw).hexdigest() == REVIEWED_COMBINED_SUCCESSOR_SHA256


def test_obsolete_stacked_fraction_scale_clamp_is_absent() -> None:
    source = inspect.getsource(primitive_extractor)
    assert "_FRAC_STACKED_SCALE" not in source
    assert "font_size * 0.6" not in source
