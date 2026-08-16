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

    def test_fraction_overlay_artifacts_are_deduped(self) -> None:
        # Fixture is the both-sides weld-symbol geometry (see the next test and
        # tests/test_both_sides_weld_fraction.py): a fresh '316' + '/' stack with
        # an already-merged '3/16' one text height (4.62 mm) below it, i.e. the
        # other side of the weld reference line -- NOT an overlay.  Overlay
        # duplicates coincide (<= _FRAC_OVERLAY_TOL_MM); a neighbouring
        # fraction is kept, so TWO '3/16' survive.  Evidence: 1011 (1 OF 2)
        # p1 has 14 stacked-over-stacked weld pairs, e.g. PDF pt spans '316'
        # [949.5,927.5,966.8,941.5] + '/' and '316' [949.5,940.5,966.8,954.6]
        # + '/', that used to collapse to one fraction each.
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
        texts = [item.text for item in merged]

        self.assertEqual(texts.count("3/16"), 2)
        self.assertNotIn("316", texts)
        self.assertNotIn("/", texts)

    def test_fraction_overlay_artifacts_are_deduped_when_coincident(self) -> None:
        # The genuine overlay case: the same '3/16' drawn twice at the SAME
        # place (a CAD PDF printer double-stroking a text stack) still
        # collapses to one fraction, and the leftover slash inside it is dropped.
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
                insertion=(532.65, 316.45), bbox=(532.65, 316.45, 536.34, 321.40),
                font_size=2.33, page_number=1,
            ),
        ]

        merged = _merge_stacked_fractions(items)
        texts = [item.text for item in merged]

        self.assertEqual(texts.count("3/16"), 1)
        self.assertNotIn("316", texts)
        self.assertNotIn("/", texts)

    def test_ambiguous_same_fraction_overlay_group_merges_once(self) -> None:
        # Both-sides weld symbol: '316' + '/' above the reference line and a
        # second '316' + '/' 4.62 mm below it (tiling bboxes).  Each slash must
        # merge with exactly ONE numerator/denominator span -- the one whose
        # own stacked band straddles that slash -- so the symbol yields TWO
        # '3/16' items and no bare '/'.  Evidence: 1011 (1 OF 2) p1, 14
        # stacked-over-stacked weld pairs, e.g. PDF pt spans '316'
        # [949.5,927.5,966.8,941.5] + '/' and '316' [949.5,940.5,966.8,954.6]
        # + '/' (each pair used to collapse to a single '3/16').
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
        texts = [item.text for item in merged]

        self.assertEqual(texts, ["3/16", "3/16"])
        top, bottom = sorted(merged, key=lambda it: -it.insertion[1])
        self.assertEqual(top.insertion, (533.03, 317.75))
        self.assertEqual(bottom.insertion, (533.03, 313.10))


if __name__ == "__main__":
    unittest.main(verbosity=2)
