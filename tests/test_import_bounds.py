# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "PDFVectorImporter" / "pdfcadcore"
if str(CORE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CORE_DIR.parent))

from pdfcadcore.import_bounds import compute_import_bounds  # noqa: E402
from pdfcadcore.primitives import NormalizedText, PageData, Primitive, next_id, reset_ids  # noqa: E402


class TestImportBounds(unittest.TestCase):
    def setUp(self):
        reset_ids()

    def test_union_bounds_with_padding(self):
        page = PageData(
            page_number=1,
            width=100.0,
            height=200.0,
            primitives=[
                Primitive(
                    id=next_id(),
                    type="line",
                    points=[(10.0, 20.0), (50.0, 80.0)],
                )
            ],
            text_items=[
                NormalizedText(
                    id=next_id(),
                    text="NOTE",
                    normalized="NOTE",
                    insertion=(60.0, 90.0),
                )
            ],
        )

        bounds = compute_import_bounds(page)
        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertLess(bounds.min_x, 10.0)
        self.assertLess(bounds.min_y, 20.0)
        self.assertGreater(bounds.max_x, 60.0)
        self.assertGreater(bounds.max_y, 90.0)

    def test_page_frame_fallback(self):
        page = PageData(page_number=1, width=80.0, height=120.0)
        bounds = compute_import_bounds(page)
        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertLessEqual(bounds.min_x, 0.0)
        self.assertGreaterEqual(bounds.max_x, 80.0)

    def test_sheet_xy_keeps_bottom_edge_and_remaps_z_fence(self):
        from pdfcadcore.import_bounds import sheet_xy

        self.assertEqual(sheet_xy((10.0, 0.0)), (10.0, 0.0))
        self.assertEqual(sheet_xy((10.0, 20.0, 0.1)), (10.0, 20.0))
        self.assertEqual(sheet_xy((10.0, 0.0, 500.0)), (10.0, 500.0))

    def test_orthogonal_points_frame_on_sheet_xy(self):
        page = PageData(
            page_number=1,
            width=100.0,
            height=200.0,
            primitives=[
                Primitive(
                    id=next_id(),
                    type="line",
                    points=[(10.0, 0.0, 20.0), (50.0, 0.0, 80.0)],
                )
            ],
        )
        bounds = compute_import_bounds(page, apply_padding=False)
        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertAlmostEqual(bounds.min_y, 20.0)
        self.assertAlmostEqual(bounds.max_y, 80.0)

    def test_huge_outlier_bbox_uses_page_frame(self):
        page = PageData(
            page_number=1,
            width=1219.2,
            height=914.4,
            primitives=[
                Primitive(
                    id=next_id(),
                    type="line",
                    points=[(10.0, 10.0), (100.0, 20.0)],
                ),
                Primitive(
                    id=next_id(),
                    type="line",
                    bbox=(-1.0e6, -1.0e6, 1.0e6, 1.0e6),
                    points=[(-1.0e6, -1.0e6), (1.0e6, 1.0e6)],
                ),
            ],
        )
        bounds = compute_import_bounds(page, apply_padding=False)
        self.assertIsNotNone(bounds)
        assert bounds is not None
        self.assertGreater(bounds.min_x, -100.0)
        self.assertLess(bounds.max_x, 200.0)
        self.assertLess(bounds.max_x, page.width)


if __name__ == "__main__":
    unittest.main()
