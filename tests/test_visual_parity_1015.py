#!/usr/bin/env python3
"""Visual parity golden checks for private 1015 shop drawing (skip if absent)."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
sys.path.insert(0, str(MOD_ROOT))

from pdfcadcore.primitive_extractor import extract_page  # noqa: E402

try:
    import pymupdf as fitz  # noqa: E402
except ImportError:  # pragma: no cover
    import fitz  # type: ignore  # noqa: E402

PDF_1015 = Path(
    os.environ.get(
        "BCS_PDF_1015",
        r"C:\Users\Rowdy Payton\Desktop\PDFTest Files\1015 - Rev 0.pdf",
    )
)
MM_PER_PT = 25.4 / 72.0


@unittest.skipUnless(PDF_1015.is_file(), f"private reference PDF not found: {PDF_1015}")
class TestVisualParity1015(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        doc = fitz.open(str(PDF_1015))
        cls.page_rect = doc[0].rect
        cls.page_data = extract_page(doc[0], page_num=1, scale=1.0)
        doc.close()

    def test_page_is_d_size_class(self) -> None:
        w_in = self.page_rect.width / 72.0
        h_in = self.page_rect.height / 72.0
        self.assertGreater(w_in, 30.0)
        self.assertGreater(h_in, 20.0)

    def test_text_item_count_reasonable(self) -> None:
        count = len(self.page_data.text_items)
        self.assertGreater(count, 200)
        self.assertLess(count, 600)

    def test_bom_quan_readable_size(self) -> None:
        quan = [t for t in self.page_data.text_items if (t.text or "").strip() == "QUAN"]
        self.assertTrue(quan, "expected BOM header QUAN")
        fs_pt = quan[0].font_size / MM_PER_PT
        self.assertGreater(fs_pt, 8.0)
        self.assertLess(fs_pt, 12.0)

    def test_dimension_strings_not_microscopic(self) -> None:
        dims = [
            t
            for t in self.page_data.text_items
            if "'" in (t.text or "") and "/" in (t.text or "")
        ]
        self.assertTrue(dims, "expected feet-inch dimension strings")
        for item in dims[:5]:
            fs_pt = item.font_size / MM_PER_PT
            self.assertGreater(
                fs_pt,
                8.0,
                msg=f"{item.text!r} font_size {fs_pt:.2f}pt too small",
            )

    def test_no_microscopic_font_sizes_in_bulk(self) -> None:
        tiny = [
            t for t in self.page_data.text_items
            if t.font_size / MM_PER_PT < 1.5
        ]
        self.assertLess(len(tiny), 5, f"too many microscopic text items: {len(tiny)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
