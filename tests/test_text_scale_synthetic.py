#!/usr/bin/env python3
"""Synthetic regression tests for nominal PDF text size handling."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
sys.path.insert(0, str(MOD_ROOT))

from pdfcadcore.primitive_extractor import extract_page  # noqa: E402
from pdfcadcore.text_scale import (  # noqa: E402
    effective_font_size_from_text_matrix,
    effective_span_font_size_pt,
    fit_font_size_to_span_bbox,
)

try:
    import pymupdf as fitz  # noqa: E402
except ImportError:  # pragma: no cover
    import fitz  # type: ignore  # noqa: E402

MM_PER_PT = 25.4 / 72.0


class TestTextScaleHelpers(unittest.TestCase):
    def test_effective_span_font_size_ignores_tall_bbox(self) -> None:
        span = {"size": 6.0, "bbox": (10.0, 20.0, 70.0, 34.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 6.0, places=2)

    def test_effective_span_font_size_keeps_nominal_size(self) -> None:
        span = {"size": 10.0, "bbox": (10.0, 20.0, 80.0, 32.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 10.0, places=2)

    def test_effective_span_font_size_ignores_short_bbox(self) -> None:
        span = {"size": 18.0, "bbox": (10.0, 20.0, 40.0, 32.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 18.0, places=2)

    def test_effective_span_font_size_ignores_vertical_bbox_axes(self) -> None:
        span = {"size": 5.0, "bbox": (10.0, 20.0, 18.0, 80.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 90.0), 5.0, places=2)

    def test_fit_font_size_is_noop_for_native_text(self) -> None:
        span = {"size": 6.0, "bbox": (10.0, 20.0, 70.0, 34.0)}
        base = 6.0 * MM_PER_PT
        fitted = fit_font_size_to_span_bbox("W12X30", base, span, MM_PER_PT, 0.0)
        self.assertAlmostEqual(fitted, base)

    def test_non_uniform_text_matrix_uses_vertical_axis_for_height(self) -> None:
        tm = (2.0, 0.0, 0.0, 0.5, 0.0, 0.0)
        self.assertAlmostEqual(
            effective_font_size_from_text_matrix(8.0, tm),
            4.0,
            places=4,
        )

    def test_raw_font_size_can_use_text_matrix_vertical_axis(self) -> None:
        span = {"tf_size": 6.0, "text_matrix": (1.0, 0.0, 0.0, 2.0, 10.0, 20.0)}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 12.0, places=4)


class TestTextScaleSyntheticPdf(unittest.TestCase):
    def test_morphed_text_span_dict_keeps_nominal_size_not_bbox(self) -> None:
        # PyMuPDF span.size is the nominal rendered height; bbox remains placement data.
        span = {"size": 6.0, "bbox": (40.0, 60.0, 120.0, 78.0), "text": "CTM-SCALE"}
        self.assertAlmostEqual(effective_span_font_size_pt(span, 0.0), 6.0, places=2)

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
