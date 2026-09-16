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

    def test_packed_integer_color_decodes_to_rgb(self) -> None:
        rgb = _norm_color(0x66AA33)
        self.assertAlmostEqual(rgb[0], 0x66 / 255.0, places=4)
        self.assertAlmostEqual(rgb[1], 0xAA / 255.0, places=4)
        self.assertAlmostEqual(rgb[2], 0x33 / 255.0, places=4)

    def test_layout_free_fraction_near_whole_number_is_preserved(self) -> None:
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
        self.assertEqual([item.text for item in merged], ["2", "1", "4", "/"])
        self.assertEqual(len(merged), len(items))
        for actual, original in zip(merged, items, strict=True):
            self.assertIs(actual, original)

    def test_layout_free_fraction_overlay_candidates_are_preserved(self) -> None:
        items = [
            NormalizedText(
                id=1, text="316", normalized="316",
                insertion=(531.42, 316.45), bbox=(531.42, 316.45, 537.57, 321.40),
                font_size=3.55, page_number=1,
            ),
            NormalizedText(
                id=2, text="/", normalized="/",
                insertion=(533.03, 316.80), bbox=(533.03, 316.80, 534.20, 321.03),
                font_size=4.23, page_number=1,
            ),
            NormalizedText(
                id=3, text="3/16", normalized="3/16",
                insertion=(532.65, 311.83), bbox=(532.65, 311.83, 536.34, 316.78),
                font_size=2.33, page_number=1,
            ),
        ]

        merged = _merge_stacked_fractions(items)
        self.assertEqual([item.text for item in merged], ["316", "/", "3/16"])
        self.assertEqual(len(merged), len(items))
        for actual, original in zip(merged, items, strict=True):
            self.assertIs(actual, original)

    def test_ambiguous_layout_free_overlay_group_is_preserved(self) -> None:
        items = [
            NormalizedText(
                id=1, text="316", normalized="316",
                insertion=(531.42, 318.64), bbox=(531.42, 316.45, 537.57, 321.40),
                font_size=3.55, page_number=1,
            ),
            NormalizedText(
                id=2, text="/", normalized="/",
                insertion=(533.03, 317.75), bbox=(533.03, 316.80, 534.20, 321.03),
                font_size=4.23, page_number=1,
            ),
            NormalizedText(
                id=3, text="316", normalized="316",
                insertion=(531.42, 314.02), bbox=(531.42, 311.83, 537.57, 316.78),
                font_size=3.55, page_number=1,
            ),
            NormalizedText(
                id=4, text="/", normalized="/",
                insertion=(533.03, 313.10), bbox=(533.03, 312.15, 534.20, 316.37),
                font_size=4.23, page_number=1,
            ),
        ]

        merged = _merge_stacked_fractions(items)
        self.assertEqual([item.text for item in merged], ["316", "/", "316", "/"])
        self.assertEqual(len(merged), len(items))
        for actual, original in zip(merged, items, strict=True):
            self.assertIs(actual, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
