# -*- coding: utf-8 -*-
"""Auto-mode profiling helpers must stay fast on heavy pages."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"
sys.path.insert(0, str(REPO_DIR))
sys.path.insert(0, str(SRC_DIR))

from PDFVectorImporter.pdfcadcore.auto_mode import classify_page_content  # noqa: E402
from PDFVectorImporter.pdfcadcore.document_profiler import suggest_import_mode  # noqa: E402
from PDFImporterCore import (  # noqa: E402
    _looks_like_fill_art_flood,
    _vector_group_stats,
)


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

    def test_text_only_page_is_not_raster_candidate(self) -> None:
        classification = classify_page_content(
            [],
            text_blocks_count=2,
            text_words_count=5,
        )
        self.assertEqual(classification["type"], "text_only")

        mode, reason = suggest_import_mode(
            classification,
            page_drawing_count=0,
            page_text_count=2,
            page_has_images=False,
        )
        self.assertEqual(mode, "vector")
        self.assertIn("Text-only", reason)

    def test_complex_pure_fill_map_is_fill_art_despite_coalesced_path_groups(
        self,
    ) -> None:
        drawings = [
            {
                "fill": (0, 0, 0),
                "color": None,
                "items": [("l", index)] * 14,
                "rect": (0, 0, 100, 100),
            }
            for index in range(2526)
        ]

        stats = _vector_group_stats(drawings, page_area=20000.0)

        self.assertEqual(stats["total_item_count"], 35364.0)
        self.assertTrue(_looks_like_fill_art_flood(len(drawings), stats))
        self.assertEqual(
            classify_page_content(
                drawings,
                text_blocks_count=0,
                text_words_count=0,
                page_area=20000.0,
            )["type"],
            "fill_art",
        )

    def test_complex_stroked_cad_page_is_not_misclassified_as_fill_art(
        self,
    ) -> None:
        drawings = [
            {
                "fill": None,
                "color": (0, 0, 0),
                "items": [("l", index)] * 14,
                "rect": (0, 0, 100, 100),
            }
            for index in range(2526)
        ]

        stats = _vector_group_stats(drawings, page_area=20000.0)

        self.assertFalse(_looks_like_fill_art_flood(len(drawings), stats))
        self.assertEqual(
            classify_page_content(
                drawings,
                text_blocks_count=1617,
                text_words_count=2916,
                page_area=20000.0,
            )["type"],
            "vectors",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
