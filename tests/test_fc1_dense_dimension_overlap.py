"""FC-1 dense-dimension horizontal overlap resolution."""

from __future__ import annotations

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"
sys.path.insert(0, str(SRC_DIR))

from PDFImporterCore import _resolve_horizontal_run_overlaps  # noqa: E402


def test_dense_fraction_nudge_uses_render_width_when_bbox_underestimates():
    """Whole '3' + reduced '13/16' must not collide when PDF bbox is tighter than glyphs."""
    items = [
        {
            "content": "3",
            "x_pdf": 80.0,
            "baseline_y_pdf": 220.0,
            "font_size_fc": 3.5,
            "orig_width_pdf": 5.0,
            "render_width_pdf": 14.0,
            "eligible_for_nudge": True,
            "is_horizontal": True,
            "line_key": 220,
        },
        {
            "content": "13/16",
            "x_pdf": 90.0,
            "baseline_y_pdf": 220.0,
            "font_size_fc": 2.1,
            "orig_width_pdf": 12.0,
            "render_width_pdf": 10.0,
            "eligible_for_nudge": True,
            "is_horizontal": True,
            "line_key": 220,
        },
    ]
    out = _resolve_horizontal_run_overlaps(items, scale=1.0)
    assert out[1]["x_pdf"] > 90.0
    assert out[1]["x_pdf"] >= 80.0 + 14.0


def test_dense_fraction_no_nudge_when_gap_already_clear():
    items = [
        {
            "content": "3",
            "x_pdf": 80.0,
            "baseline_y_pdf": 220.0,
            "font_size_fc": 3.5,
            "orig_width_pdf": 6.0,
            "render_width_pdf": 6.0,
            "eligible_for_nudge": True,
            "is_horizontal": True,
            "line_key": 220,
        },
        {
            "content": "13/16",
            "x_pdf": 100.0,
            "baseline_y_pdf": 220.0,
            "font_size_fc": 2.1,
            "orig_width_pdf": 12.0,
            "render_width_pdf": 10.0,
            "eligible_for_nudge": True,
            "is_horizontal": True,
            "line_key": 220,
        },
    ]
    out = _resolve_horizontal_run_overlaps(items, scale=1.0)
    assert out[1]["x_pdf"] == 100.0
