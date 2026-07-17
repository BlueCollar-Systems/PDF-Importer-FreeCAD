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

    def test_scale_crosscheck_low_confidence(self) -> None:
        report = build_import_report(
            host_app="freecad",
            pdf_path="drawing.pdf",
            mode="auto",
            pages=1,
            primitive_count=40,
            extra={
                "resolved_scale": {
                    "factor": 48.0,
                    "notation": '1/4" = 1\'-0"',
                    "source": "titleblock",
                    "confidence": 0.55,
                },
                "scale_hints": {"title_block_detected": True, "dimension_count": 4},
            },
        )
        crosscheck = report.extra.get("scale_crosscheck")
        self.assertIsInstance(crosscheck, dict)
        self.assertIn("low_confidence", crosscheck.get("reasons", []))
        summary = report.extra.get("human_summary", "")
        self.assertIn("Scale note:", summary)

    def test_import_contract_ready_aggregate(self) -> None:
        report = build_import_report(
            host_app="freecad",
            importer_version="4.0.60",
            pdf_path="drawing.pdf",
            mode="auto",
            pages=1,
            primitive_count=40,
            text_count=2,
            text_mode="labels",
            extra={
                "actual_text_entity_types": {
                    "entity_type": "labels",
                    "count": 2,
                    "native_label": 2,
                },
                "resolved_scale": {
                    "factor": 48.0,
                    "notation": '1/4" = 1\'-0"',
                    "source": "titleblock",
                    "confidence": 0.55,
                },
            },
        )

        ready = report.extra.get("import_contract_ready")
        self.assertIsInstance(ready, dict)
        self.assertTrue(ready["ready"])
        self.assertTrue(ready["checks"]["build_stamp"])
        self.assertTrue(ready["checks"]["scale_crosscheck"])
        self.assertTrue(ready["checks"]["actual_text_entity_types"])
        self.assertTrue(ready["checks"]["no_open_failure"])

    def test_import_contract_ready_fails_when_text_entities_unverified(self) -> None:
        report = build_import_report(
            host_app="freecad",
            importer_version="4.0.60",
            pdf_path="drawing.pdf",
            mode="auto",
            pages=1,
            primitive_count=40,
            text_count=2,
        )

        ready = report.extra.get("import_contract_ready")
        self.assertIsInstance(ready, dict)
        self.assertFalse(ready["ready"])
        self.assertTrue(ready["checks"]["scale_crosscheck"])
        self.assertFalse(ready["checks"]["actual_text_entity_types"])

    def test_import_contract_ready_ignores_source_spans_when_text_is_disabled(self) -> None:
        report = build_import_report(
            host_app="freecad",
            importer_version="4.0.70",
            pdf_path="drawing.pdf",
            mode="vector",
            pages=1,
            primitive_count=40,
            import_text=False,
            text_mode="none",
            text_source_spans=7,
        )

        ready = report.extra.get("import_contract_ready")
        self.assertIsInstance(ready, dict)
        self.assertTrue(ready["ready"])
        self.assertTrue(ready["checks"]["text_delivery"])

    def test_human_summary_records_importer_version(self) -> None:
        """Evidence attribution: every report names the importer version in
        both the JSON importer block and the human summary."""
        report = build_import_report(
            host_app="freecad",
            importer_version="9.9.9",
            pdf_path="drawing.pdf",
            mode="auto",
            pages=1,
            primitive_count=5,
        )

        self.assertEqual(report.importer.get("version"), "9.9.9")
        self.assertEqual(report.to_dict()["importer"]["version"], "9.9.9")
        summary = report.extra.get("human_summary", "")
        self.assertIn("Importer v9.9.9", summary)
        # Standalone builder path stays consistent with the enriched extra.
        self.assertIn("Importer v9.9.9", build_human_summary(report))

    def test_human_summary_omits_importer_version_when_empty(self) -> None:
        report = build_import_report(
            host_app="freecad",
            pdf_path="drawing.pdf",
            mode="auto",
            pages=1,
            primitive_count=5,
        )

        self.assertNotIn("Importer v", report.extra.get("human_summary", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
