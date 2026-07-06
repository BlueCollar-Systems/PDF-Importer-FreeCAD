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
        'p1016 PL3/8"X6 7/8"',
        'p1019 PL3/8"X7 3/4"',
        'p1052 PL3/4"X7"',
        "w1023 W8X15 3'-11 3/4\"",
        "w1025 W12X30 13'-11 1/4\"",
        "a1005 L3X3X3/8",
        "1017FR1 W12X30",
    ]

    def test_extract_bootstrap_rows_from_tier1_bom(self):
        items = [{"text": line, "page": 1} for line in self.BOM_LINES]
        rows = extract_bootstrap_rows(items)
        marks = {row["piece_mark"].lower() for row in rows}
        self.assertIn("p1016", marks)
        self.assertIn("w1025", marks)
        self.assertIn("a1005", marks)
        w1025 = next(r for r in rows if r["piece_mark"] == "w1025")
        self.assertEqual(w1025["profile_hint"], "W12X30")
        self.assertAlmostEqual(w1025["length_in"], 167.25, places=2)

    def test_extract_bootstrap_rows_from_sequential_bom_lines(self):
        lines = [
            "BILL OF MATERIAL",
            "QUAN",
            "MARK",
            "DESCRIPTION",
            "LENGTH",
            "1017FR1",
            "1",
            "W12X30",
            "13'-11 1/4\"",
            "417",
            "GALV.",
            "A992",
            "p1016",
            "3",
            "PL3/8\"X6 7/8\"",
            "0'-7 3/4\"",
            "17",
            "GALV.",
            "A36",
        ]
        rows = extract_bootstrap_rows({"text": line, "page": 1} for line in lines)
        self.assertEqual(2, len(rows))
        self.assertEqual("1017FR1", rows[0]["piece_mark"])
        self.assertEqual("W12X30", rows[0]["profile_hint"])
        self.assertAlmostEqual(167.25, rows[0]["length_in"], places=2)
        self.assertEqual("p1016", rows[1]["piece_mark"])
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
