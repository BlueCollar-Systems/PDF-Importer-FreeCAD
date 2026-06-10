from __future__ import annotations

import sys
import unittest
from pathlib import Path


MOD_ROOT = Path(__file__).resolve().parents[1] / "PDFVectorImporter"
sys.path.insert(0, str(MOD_ROOT))

from pdfcadcore.primitive_extractor import _norm_color  # noqa: E402


class TestPdfPrimitiveExtractor(unittest.TestCase):
    def test_cmyk_converts_to_rgb(self) -> None:
        rgb = _norm_color((0.0, 1.0, 1.0, 0.0))
        self.assertAlmostEqual(rgb[0], 1.0, places=3)
        self.assertAlmostEqual(rgb[1], 0.0, places=3)
        self.assertAlmostEqual(rgb[2], 0.0, places=3)

    def test_grayscale_scalar_expands(self) -> None:
        rgb = _norm_color(0.5)
        self.assertEqual(rgb, (0.5, 0.5, 0.5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
