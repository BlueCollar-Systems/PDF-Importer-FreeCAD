from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore.cli_error_copy import cli_error  # noqa: E402
from pdfcadcore.import_report import build_import_report, build_report_meta  # noqa: E402


class TestCliErrorCopy(unittest.TestCase):
    def test_known_codes_render_plain_english(self) -> None:
        self.assertIn("input file path is required", cli_error("missing_input"))
        self.assertIn("missing.pdf", cli_error("file_not_found", path="missing.pdf"))

    def test_unknown_code_falls_back(self) -> None:
        self.assertEqual("Error: {message}", cli_error("unknown_code"))


class TestReportMeta(unittest.TestCase):
    def test_build_report_meta_includes_build_stamp(self) -> None:
        meta = build_report_meta(
            host_app="freecad",
            importer_version="4.0.58",
            report_sha256="abc123",
        )
        self.assertEqual("freecad", meta["host"])
        self.assertEqual("4.0.58", meta["semver"])
        self.assertIn("freecad", meta["build_stamp"])
        self.assertIn("4.0.58", meta["build_stamp"])
        self.assertIn("abc123", meta["build_stamp"])

    def test_import_report_emits_report_meta(self) -> None:
        try:
            import pymupdf as fitz  # noqa: E402
        except ImportError:
            import fitz  # type: ignore  # noqa: E402

        with tempfile.TemporaryDirectory(prefix="fc_report_meta_") as tmp:
            pdf_path = Path(tmp) / "sample.pdf"
            doc = fitz.open()
            doc.new_page(width=200, height=120)
            doc.save(str(pdf_path))
            doc.close()
            report = build_import_report(
                host_app="freecad",
                importer_version="4.0.58",
                pdf_path=str(pdf_path),
                pages=1,
                primitive_count=3,
                text_count=1,
                import_text=True,
                text_mode="labels",
            )
            payload = report.to_dict()
            self.assertIn("report_meta", payload)
            self.assertEqual("freecad", payload["report_meta"]["host"])
            self.assertEqual("4.0.58", payload["report_meta"]["semver"])
            self.assertTrue(payload["report_meta"]["build_stamp"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
