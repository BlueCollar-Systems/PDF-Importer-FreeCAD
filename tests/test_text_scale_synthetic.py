#!/usr/bin/env python3
"""Synthetic regression tests for pdfcadcore text scale / CTM reconciliation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
sys.path.insert(0, str(MOD_ROOT))

from pdfcadcore.text_scale import (  # noqa: E402
    bbox_glyph_height_pt,
    effective_font_size_from_text_matrix,
    effective_span_font_size_pt,
    fit_font_size_to_span_bbox,
)
from pdfcadcore.primitive_extractor import extract_page  # noqa: E402

try:
    import pymupdf as fitz  # noqa: E402
except ImportError:  # pragma: no cover
    import fitz  # type: ignore  # noqa: E402

MM_PER_PT = 25.4 / 72.0


class TestTextScaleHelpers(unittest.TestCase):
    def test_effective_span_font_size_grows_when_tf_undershoots_bbox(self) -> None:
        span = {"size": 6.0, "bbox": (10.0, 20.0, 70.0, 34.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 13.44, places=2)

    def test_effective_span_font_size_keeps_normal_tf_near_bbox_height(self) -> None:
        span = {"size": 10.0, "bbox": (10.0, 20.0, 80.0, 32.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 10.0, places=2)

    def test_effective_span_font_size_shrinks_when_tf_overshoots_bbox(self) -> None:
        span = {"size": 18.0, "bbox": (10.0, 20.0, 40.0, 32.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 11.52, places=2)

    def test_effective_span_font_size_uses_short_side_for_vertical_text(self) -> None:
        span = {"size": 5.0, "bbox": (10.0, 20.0, 18.0, 80.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 90.0), 7.68, places=2)

    def test_fit_font_size_grows_undersized_host_font(self) -> None:
        span = {"size": 6.0, "bbox": (10.0, 20.0, 70.0, 34.0)}
        grown = effective_span_font_size_pt(span, 0.0) * MM_PER_PT
        base = 6.0 * MM_PER_PT
        self.assertGreater(grown, base)
        fitted = fit_font_size_to_span_bbox("W12X30", grown, span, MM_PER_PT, 0.0)
        self.assertGreaterEqual(fitted, grown * 0.95)

    def test_non_uniform_text_matrix_uses_max_axis(self) -> None:
        tm = (2.0, 0.0, 0.0, 0.5, 0.0, 0.0)
        self.assertAlmostEqual(
            effective_font_size_from_text_matrix(8.0, tm),
            16.0,
            places=4,
        )


class TestTextScaleSyntheticPdf(unittest.TestCase):
    def test_morphed_text_span_dict_reconciles_ctm_inflation(self) -> None:
        # Simulate PyMuPDF span where Tf=6 but measured bbox height reflects 2x CTM.
        span = {"size": 6.0, "bbox": (40.0, 60.0, 120.0, 78.0), "text": "CTM-SCALE"}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 17.28, places=2)

    def test_extract_page_returns_text_on_rotated_page(self) -> None:
        with tempfile.TemporaryDirectory(prefix="text_scale_rot_") as tmp:
            pdf_path = Path(tmp) / "page_rotate.pdf"
            doc = fitz.open()
            page = doc.new_page(width=400, height=300)
            page.insert_text((60, 120), "ROT270", fontsize=10)
            page.set_rotation(270)
            doc.save(str(pdf_path))
            doc.close()

            doc = fitz.open(str(pdf_path))
            page_data = extract_page(doc[0], page_num=1, scale=1.0)
            doc.close()

            matches = [item for item in page_data.text_items if "ROT" in (item.text or "")]
            self.assertTrue(matches, "expected text on rotated page")
            self.assertGreater(matches[0].font_size, 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
