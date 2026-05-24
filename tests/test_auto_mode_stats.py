# -*- coding: utf-8 -*-
"""Auto-mode profiling helpers must stay fast on heavy pages."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"
sys.path.insert(0, str(SRC_DIR))

from PDFImporterCore import _vector_group_stats  # noqa: E402


class TestAutoModeStats(unittest.TestCase):
    def test_empty_drawings(self) -> None:
        stats = _vector_group_stats([])
        self.assertEqual(stats["stroke_ratio"], 0.0)

    def test_heavy_page_uses_sampling(self) -> None:
        drawings = [
            {"fill": (0, 0, 0), "color": None, "items": ["l"], "rect": (0, 0, 1, 1)}
            for _ in range(9000)
        ]
        stats = _vector_group_stats(drawings, page_area=1000.0)
        self.assertGreater(stats["fill_only_ratio"], 0.9)
        self.assertEqual(stats["stroke_ratio"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
