from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (SRC_DIR, MOD_ROOT):
    sys.path.insert(0, str(path))

from PDFImporterCore import ImportOptions, _report_fallback_state, write_import_report  # noqa: E402


class TestImportReportWriter(unittest.TestCase):
    def test_report_fallback_state_forced_raster(self) -> None:
        opts = ImportOptions(import_mode="raster")

        self.assertEqual(
            _report_fallback_state(opts),
            (True, "forced_raster_mode"),
        )

    def test_report_fallback_state_counts_raster_pages(self) -> None:
        opts = ImportOptions(import_mode="auto")
        opts.raster_page_count = 2

        self.assertEqual(
            _report_fallback_state(opts),
            (True, "raster_fallback_2_pages"),
        )

    def test_report_fallback_state_uses_recorded_raster_reason(self) -> None:
        opts = ImportOptions(import_mode="auto")
        opts.raster_page_count = 1
        opts.raster_fallback_reasons.append("scanned/raster page")
        opts.auto_reason = "Standard vector content"

        self.assertEqual(
            _report_fallback_state(opts),
            (True, "scanned/raster page"),
        )

    def test_report_fallback_state_summarizes_mixed_raster_reasons(self) -> None:
        opts = ImportOptions(import_mode="auto")
        opts.raster_page_count = 2
        opts.raster_fallback_reasons.extend([
            "scanned/raster page",
            "GIS/topo map",
        ])
        opts.auto_reason = "Standard vector content"

        used, reason = _report_fallback_state(opts)

        self.assertTrue(used)
        self.assertIn("raster_fallback_2_pages", reason)
        self.assertIn("scanned/raster page", reason)
        self.assertIn("GIS/topo map", reason)
        self.assertNotEqual(reason, "Standard vector content")

    def test_report_fallback_state_preserves_auto_reason(self) -> None:
        opts = ImportOptions(import_mode="auto")
        opts.auto_resolved_mode = "raster"
        opts.auto_reason = "text-heavy page"

        self.assertEqual(
            _report_fallback_state(opts),
            (True, "text-heavy page"),
        )

    def test_write_import_report_uses_shared_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fc_import_report_") as tmp:
            report_path = Path(tmp) / "import_report.json"
            opts = ImportOptions(import_mode="auto")
            opts.auto_resolved_mode = "vector"
            opts.auto_reason = "test vector page"
            opts.phase_timings_ms["open_pdf_ms"] = 1.25
            opts.phase_timings_ms["pages_import_ms"] = 8.75
            opts.shapestring_skips["shapestring_failed"] = 2

            result = write_import_report(
                pdf_path=str(Path(tmp) / "sample.pdf"),
                output_path=str(report_path),
                opts=opts,
                pages_imported=1,
                total_pages=1,
                primitive_count=7,
                text_count=2,
                layer_count=1,
                elapsed_ms=12.5,
            )

            self.assertEqual(result, str(report_path))
            data = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], "bcs.import_report/1.1")
            self.assertEqual(data["host"]["app"], "freecad")
            package_xml = (MOD_ROOT / "package.xml").read_text(encoding="utf-8")
            expected_version = package_xml.split("<version>", 1)[1].split("</version>", 1)[0]
            self.assertEqual(data["importer"]["version"], expected_version)
            self.assertEqual(data["result"]["primitives"], 7)
            self.assertEqual(data["result"]["text_entities"], 2)
            self.assertEqual(data["result"]["layers"], 1)
            self.assertEqual(data["performance"]["phases"]["open_pdf_ms"], 1.25)
            self.assertEqual(data["performance"]["phases"]["pages_import_ms"], 8.75)
            self.assertEqual(data["performance"]["phases"]["total_ms"], 12.5)
            self.assertEqual(data["extra"]["auto_resolved_mode"], "vector")
            self.assertEqual(data["extra"]["shapestring_skips"]["shapestring_failed"], 2)
            self.assertEqual(data["extra"]["shapestring_skip_total"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
