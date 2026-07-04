# -*- coding: utf-8 -*-
"""Tests for bcs.parts_bootstrap/1.0 stub sidecar."""

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
    build_parts_bootstrap_stub,
    write_parts_bootstrap_sidecar,
)


class PartsBootstrapStubTest(unittest.TestCase):
    def test_build_stub_schema(self):
        payload = build_parts_bootstrap_stub("sample.pdf", page_count=2)
        self.assertEqual(SCHEMA, payload["schema"])
        self.assertEqual([], payload["rows"])
        self.assertEqual(2, payload["source_pdf"]["pages"])

    def test_write_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "sample.pdf"
            pdf.write_bytes(b"sample")
            out = Path(tmp) / "parts_bootstrap.json"
            path = write_parts_bootstrap_sidecar(str(out), str(pdf), page_count=1)
            self.assertEqual(str(out), path)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(SCHEMA, data["schema"])
            self.assertIn("sha256", data["source_pdf"])


if __name__ == "__main__":
    unittest.main()
