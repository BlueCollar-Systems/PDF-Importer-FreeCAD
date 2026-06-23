# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "PDFVectorImporter" / "pdfcadcore"
if str(CORE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CORE_DIR.parent))

from pdfcadcore.qa_report import QAReport  # noqa: E402


class TestQAReportV11(unittest.TestCase):
    def test_v11_fields_round_trip(self):
        report = QAReport(
            importer="FC",
            host_name="FreeCAD",
            host_version="1.0.2",
            runtime_version="3.11.9",
            importer_version="4.0.13",
            dxf_version="R2010",
            memory_peak_mb=512.5,
            phase_timings={"parse": 0.4, "text": 0.6},
            fallback_reason="page_stream",
            preset="auto",
            status="ok",
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.json"
            report.write_json(str(path))
            loaded = QAReport.read_json(str(path))
        self.assertEqual(loaded.schema_version, "1.1")
        self.assertEqual(loaded.host_version, "1.0.2")
        self.assertEqual(loaded.memory_peak_mb, 512.5)
        self.assertEqual(loaded.phase_timings["parse"], 0.4)
        self.assertEqual(loaded.phase_timings["text"], 0.6)
        self.assertEqual(loaded.fallback_reason, "page_stream")

    def test_reads_legacy_v10_payload(self):
        payload = {
            "schema_version": "1.0",
            "importer": "BL",
            "status": "ok",
            "counts_before": {"lines": 1},
            "counts_after": {"lines": 2},
        }
        report = QAReport.from_dict(payload)
        self.assertEqual(report.schema_version, "1.0")
        self.assertEqual(report.host_version, "")
        self.assertEqual(report.memory_peak_mb, 0.0)
        self.assertEqual(report.phase_timings, {})


if __name__ == "__main__":
    unittest.main()
