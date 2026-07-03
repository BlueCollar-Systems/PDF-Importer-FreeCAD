from __future__ import annotations

import sys
import unittest
from pathlib import Path


MOD_ROOT = Path(__file__).resolve().parents[1] / "PDFVectorImporter"
sys.path.insert(0, str(MOD_ROOT))

from pdfcadcore.primitive_extractor import _merge_stacked_fractions, _norm_color  # noqa: E402
from pdfcadcore.primitives import NormalizedText  # noqa: E402


class TestPdfPrimitiveExtractor(unittest.TestCase):
    def test_cmyk_converts_to_rgb(self) -> None:
        rgb = _norm_color((0.0, 1.0, 1.0, 0.0))
        self.assertAlmostEqual(rgb[0], 1.0, places=3)
        self.assertAlmostEqual(rgb[1], 0.0, places=3)
        self.assertAlmostEqual(rgb[2], 0.0, places=3)

    def test_grayscale_scalar_expands(self) -> None:
        rgb = _norm_color(0.5)
        self.assertEqual(rgb, (0.5, 0.5, 0.5))

    def test_stacked_fraction_merge_ignores_full_size_whole_number(self) -> None:
        items = [
            NormalizedText(
                id=1, text="2", normalized="2",
                insertion=(425.62, 276.96), bbox=(425.62, 274.93, 427.86, 278.98),
                font_size=4.05, page_number=1,
            ),
            NormalizedText(
                id=2, text="1", normalized="1",
                insertion=(427.91, 277.88), bbox=(427.91, 276.10, 429.89, 279.65),
                font_size=3.55, page_number=1,
            ),
            NormalizedText(
                id=3, text="4", normalized="4",
                insertion=(430.12, 276.35), bbox=(430.12, 274.58, 432.09, 278.13),
                font_size=3.55, page_number=1,
            ),
            NormalizedText(
                id=4, text="/", normalized="/",
                insertion=(429.44, 277.26), bbox=(429.44, 275.15, 430.61, 279.37),
                font_size=4.23, page_number=1,
            ),
        ]

        merged = _merge_stacked_fractions(items)
        texts = [item.text for item in merged]

        self.assertIn("2", texts)
        self.assertIn("1/4", texts)
        self.assertNotIn("2/4", texts)


if __name__ == "__main__":
    unittest.main(verbosity=2)
