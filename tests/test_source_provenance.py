#!/usr/bin/env python3
"""Tests for bcs.source_provenance/1.0 sidecar emission."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
_CORPUS_ENV = os.environ.get("BCS_PRIVATE_VALIDATION_ROOT") or os.environ.get("PDF_PRIVATE_VALIDATION_ROOT")
CORPUS_ROOT = Path(_CORPUS_ENV) if _CORPUS_ENV else Path(r"__private_validation_assets_not_configured__")
for path in (SRC_DIR, MOD_ROOT):
    sys.path.insert(0, str(path))

from PDFImporterCore import ImportOptions, write_import_report  # noqa: E402
from pdfcadcore.source_provenance import (  # noqa: E402
    SourceProvenanceObject,
    build_source_provenance,
    ensure_import_session_id,
    record_text_span_provenance,
)

try:
    import pymupdf as fitz  # noqa: E402
except ImportError:  # pragma: no cover
    import fitz  # type: ignore  # noqa: E402


def _load_schema() -> dict:
    path = CORPUS_ROOT / "schemas" / "source_provenance.schema.json"
    if not path.is_file():
        raise unittest.SkipTest(f"source_provenance schema unavailable: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_sample_pdf(pdf_path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=200, height=120)
    page.insert_text((30, 60), "Provenance sample", fontsize=10)
    doc.save(str(pdf_path))
    doc.close()


class TestSourceProvenance(unittest.TestCase):
    def test_build_source_provenance_matches_schema_shape(self) -> None:
        schema = _load_schema()
        payload = build_source_provenance(
            import_session_id="session-1",
            pdf_path="sample.pdf",
            objects=[
                SourceProvenanceObject(
                    object_id="text_span:1:0",
                    page=1,
                    source_kind="text_span",
                    created_entity_type="native_label",
                    source_bbox_pdf=[1.0, 2.0, 3.0, 4.0],
                    selected_text_mode="labels",
                )
            ],
            host_app="freecad",
            importer_version="4.0.58",
            build_stamp="freecad 4.0.58 · report abc",
            page_count=1,
        ).to_dict()

        for key in schema["required"]:
            self.assertIn(key, payload)
        self.assertEqual(payload["schema"], schema["properties"]["schema"]["const"])
        self.assertEqual(payload["objects"][0]["source_kind"], "text_span")

    def test_write_import_report_emits_sidecar_when_provenance_recorded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fc_source_provenance_") as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            _write_sample_pdf(pdf_path)
            report_path = tmp_path / "import_report.json"
            opts = ImportOptions(import_mode="manual", text_mode="labels", import_text=True)
            ensure_import_session_id(opts)
            record_text_span_provenance(
                opts,
                page=1,
                span={"bbox": [30, 50, 120, 62], "text": "Provenance sample"},
                text="Provenance sample",
                created_entity_type="native_label",
            )

            write_import_report(
                pdf_path=str(pdf_path),
                output_path=str(report_path),
                opts=opts,
                pages_imported=1,
                total_pages=1,
                primitive_count=1,
                text_count=1,
                elapsed_ms=5.0,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertIn("source_provenance", report["extra"])
            sidecar_path = tmp_path / "source_provenance.json"
            self.assertTrue(sidecar_path.is_file())
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            self.assertEqual("bcs.source_provenance/1.0", sidecar["schema"])
            self.assertEqual(1, len(sidecar["objects"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
