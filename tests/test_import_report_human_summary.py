from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
sys.path.insert(0, str(MOD_ROOT))

from pdfcadcore.import_report import build_human_summary, build_import_report  # noqa: E402


class TestImportReportHumanSummary(unittest.TestCase):
    def test_build_import_report_attaches_human_summary(self) -> None:
        report = build_import_report(
            host_app="freecad",
            pdf_path="C:/drawings/shop-floor.pdf",
            mode="auto",
            pages=2,
            primitive_count=120,
            text_count=15,
            layer_count=3,
            warnings=0,
            elapsed_ms=1300.0,
            text_mode="geometry",
            extra={
                "auto_reason": "Standard vector content",
                "resolved_scale": {
                    "factor": 48.0,
                    "notation": '1/4" = 1\'-0"',
                    "source": "titleblock",
                    "confidence": 0.91,
                },
            },
        )

        summary = report.extra.get("human_summary", "")
        self.assertTrue(summary)
        self.assertIn("shop-floor.pdf", summary)
        self.assertIn("120 vector", summary)
        self.assertIn("geometry text", summary)
        self.assertIn("titleblock", summary)

    def test_build_human_summary_describes_fallback(self) -> None:
        report = build_import_report(
            host_app="librecad",
            pdf_path="scan.pdf",
            mode="auto",
            pages=1,
            primitive_count=0,
            warnings=1,
            fallback_used=True,
            fallback_reason="raster_fallback_1_pages",
        )
        summary = build_human_summary(report)
        self.assertIn("fallback", summary.lower())
        self.assertIn("No editable geometry", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
