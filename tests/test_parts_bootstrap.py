# -*- coding: utf-8 -*-
"""Tests for bcs.parts_bootstrap/1.0 sidecar and BOM row extraction."""

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

from pdfcadcore.parts_bootstrap import (  # noqa: E402
    SCHEMA,
    build_parts_bootstrap,
    extract_bootstrap_rows,
    write_parts_bootstrap_sidecar,
)


class PartsBootstrapTest(unittest.TestCase):
    BOM_LINES = [
        'p9001 PL1/2"X5 1/2"',
        'p9002 PL5/8"X8 1/4"',
        'p9003 PL1"X9"',
        "w9004 W6X9 4'-2 1/2\"",
        "w9005 W10X22 11'-6 1/2\"",
        "a9006 L2X2X1/4",
        "9007FR9 W10X22",
    ]

    def test_extract_bootstrap_rows_from_tier1_bom(self):
        items = [{"text": line, "page": 1} for line in self.BOM_LINES]
        rows = extract_bootstrap_rows(items)
        marks = {row["piece_mark"].lower() for row in rows}
        self.assertIn("p9001", marks)
        self.assertIn("w9005", marks)
        self.assertIn("a9006", marks)
        beam = next(r for r in rows if r["piece_mark"] == "w9005")
        self.assertEqual(beam["profile_hint"], "W10X22")
        self.assertAlmostEqual(beam["length_in"], 138.5, places=2)

    def test_extract_bootstrap_rows_from_sequential_bom_lines(self):
        lines = [
            "BILL OF MATERIAL",
            "QUAN",
            "MARK",
            "DESCRIPTION",
            "LENGTH",
            "9007FR9",
            "1",
            "W10X22",
            "11'-6 1/2\"",
            "346",
            "GALV.",
            "A992",
            "p9001",
            "3",
            "PL1/2\"X5 1/2\"",
            "0'-6 1/2\"",
            "14",
            "GALV.",
            "A36",
        ]
        rows = extract_bootstrap_rows({"text": line, "page": 1} for line in lines)
        self.assertEqual(2, len(rows))
        self.assertEqual("9007FR9", rows[0]["piece_mark"])
        self.assertEqual("W10X22", rows[0]["profile_hint"])
        self.assertAlmostEqual(138.5, rows[0]["length_in"], places=2)
        self.assertEqual("p9001", rows[1]["piece_mark"])
        self.assertEqual(3, rows[1]["quantity"])
        self.assertEqual("plate", rows[1]["kind"])

    def test_build_payload_schema(self):
        payload = build_parts_bootstrap("sample.pdf", page_count=2, rows=[])
        self.assertEqual(SCHEMA, payload["schema"])
        self.assertEqual([], payload["rows"])
        self.assertEqual([], payload["parts"])
        self.assertEqual(0, payload["part_count"])
        self.assertEqual(2, payload["source_pdf"]["pages"])

    def test_write_sidecar_with_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "sample.pdf"
            pdf.write_bytes(b"sample")
            out = Path(tmp) / "parts_bootstrap.json"
            items = [{"text": self.BOM_LINES[0], "page": 1}]
            path = write_parts_bootstrap_sidecar(
                str(out),
                str(pdf),
                page_count=1,
                text_items=items,
            )
            self.assertEqual(str(out), path)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(SCHEMA, data["schema"])
            self.assertGreaterEqual(len(data["rows"]), 1)
            self.assertEqual(len(data["rows"]), data["part_count"])
            self.assertIn("sha256", data["source_pdf"])


if __name__ == "__main__":
    unittest.main()
