from __future__ import annotations

import tempfile
import unittest

from pdfcadcore.import_report import (
    build_font_embedding_hints,
    build_import_report,
    build_performance_hint,
    build_scale_crosscheck,
)
from pdfcadcore.preflight_copy import SCALE_CROSSCHECK_BANNER

try:
    import pymupdf as fitz
except ImportError:
    import fitz


class TestImportReportPdfAudit(unittest.TestCase):
    def test_performance_hint_threshold(self) -> None:
        self.assertIsNone(build_performance_hint(primitive_count=100, text_count=50))
        hint = build_performance_hint(primitive_count=60_000, text_count=0)
        self.assertIn("8 GB RAM", hint or "")

    def test_scale_crosscheck_banner_unified(self) -> None:
        cross = build_scale_crosscheck(
            {
                "resolved_scale": {"fallback_reason": "no_scale_detected"},
                "scale_hints": {},
            }
        )
        self.assertIsNotNone(cross)
        assert cross is not None
        self.assertEqual(cross["banner"], SCALE_CROSSCHECK_BANNER)

    def test_font_hints_empty_on_simple_pdf(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            path = tmp.name
        doc = fitz.open()
        page = doc.new_page(width=200, height=120)
        page.draw_line((20, 20), (120, 20), color=(0, 0, 0), width=1.0)
        doc.save(path)
        doc.close()
        doc2 = fitz.open(path)
        hints = build_font_embedding_hints(doc2)
        doc2.close()
        self.assertIsInstance(hints, dict)

    def test_build_import_report_merges_pdf_audit(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            path = tmp.name
        doc = fitz.open()
        page = doc.new_page(width=200, height=120)
        page.draw_line((20, 20), (120, 20), color=(0, 0, 0), width=1.0)
        doc.save(path)
        doc.close()
        report = build_import_report(
            host_app="freecad",
            pdf_path=path,
            pages=1,
            primitive_count=1,
        )
        self.assertIn("human_summary", report.extra)


if __name__ == "__main__":
    unittest.main(verbosity=2)
