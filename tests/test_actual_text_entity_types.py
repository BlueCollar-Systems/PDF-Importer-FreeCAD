from __future__ import annotations

import ast
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
_CORPUS_ENV = os.environ.get("BCS_CORPUS_ROOT") or os.environ.get("PDF_TEST_CORPUS")
CORPUS_ROOT = Path(_CORPUS_ENV) if _CORPUS_ENV else Path(r"C:\1pdf-test-corpus")
for path in (SRC_DIR, MOD_ROOT):
    sys.path.insert(0, str(path))

from PDFImporterCore import ImportOptions, write_import_report  # noqa: E402

try:
    import pymupdf as fitz  # noqa: E402
except ImportError:  # pragma: no cover - legacy PyMuPDF name
    import fitz  # type: ignore  # noqa: E402


def _load_text_entity_schema() -> dict:
    path = CORPUS_ROOT / "schemas" / "text_entity_verification.schema.json"
    if not path.is_file():
        raise unittest.SkipTest(f"text entity schema unavailable: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_contract_shape(payload: dict, schema: dict) -> None:
    for key in schema["required"]:
        assert key in payload, f"missing required key: {key}"
    assert payload["schema"] == schema["properties"]["schema"]["const"]
    assert payload["host"] in schema["properties"]["host"]["enum"]
    assert payload["requested_text_mode"] in schema["properties"]["requested_text_mode"]["enum"]
    assert payload["status"] in schema["properties"]["status"]["enum"]

    actual = payload["actual_text_entity_types"]
    assert isinstance(actual, dict)
    bucket_schema = schema["properties"]["actual_text_entity_types"]["properties"]
    for key, value in actual.items():
        if key not in bucket_schema:
            continue
        assert isinstance(value, int), f"{key} must be an integer"
        assert value >= 0, f"{key} must be non-negative"


def _write_sample_pdf(pdf_path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=200, height=120)
    page.insert_text((30, 60), "Text entity contract", fontsize=10)
    doc.save(str(pdf_path))
    doc.close()


class TestActualTextEntityTypes(unittest.TestCase):
    def test_import_pdf_does_not_reimport_pages_for_report(self) -> None:
        source_path = SRC_DIR / "PDFImporterCore.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        import_pdf_node = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "import_pdf"
        )
        calls = [
            node for node in ast.walk(import_pdf_node)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_import_pdf_page_inner"
        ]
        self.assertEqual(
            len(calls),
            1,
            "import_pdf must call _import_pdf_page_inner exactly once and never reopen pages via import_pdf_page",
        )

    def test_freecad_report_emits_schema_compatible_text_entity_payload(self) -> None:
        schema = _load_text_entity_schema()
        with tempfile.TemporaryDirectory(prefix="fc_text_entity_report_") as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            _write_sample_pdf(pdf_path)
            report_path = tmp_path / "import_report.json"

            opts = ImportOptions(
                import_mode="manual",
                text_mode="labels",
                import_text=True,
            )
            opts._report_extra = {
                "actual_text_entity_types": {
                    "entity_type": "labels",
                    "count": 5,
                    "font_rendered": True,
                    "examples": ["Test", "Example"],
                }
            }

            write_import_report(
                pdf_path=str(pdf_path),
                output_path=str(report_path),
                opts=opts,
                pages_imported=1,
                total_pages=1,
                primitive_count=8,
                text_count=5,
                elapsed_ms=10.0,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            actual = report["extra"]["actual_text_entity_types"]
            self.assertEqual(actual["entity_type"], "labels")
            self.assertEqual(actual["count"], 5)
            self.assertEqual(actual["native_label"], 5)
            self.assertEqual(actual["examples"], ["Test", "Example"])

            verification_payload = {
                "schema": "bcs.text_entity_verification/1.0",
                "host": report["host"]["app"],
                "source_pdf": {
                    "path": report["input"]["file"],
                    "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                    "page": 1,
                },
                "requested_text_mode": report["extra"]["text_mode"],
                "actual_text_entity_types": actual,
                "status": "pass",
            }
            _assert_contract_shape(verification_payload, schema)

    def test_no_text_mode_does_not_emit_actual_entity_payload(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fc_no_text_entity_report_") as tmp:
            tmp_path = Path(tmp)
            pdf_path = tmp_path / "sample.pdf"
            _write_sample_pdf(pdf_path)
            report_path = tmp_path / "import_report.json"
            opts = ImportOptions(import_mode="manual", text_mode="none", import_text=False)

            write_import_report(
                pdf_path=str(pdf_path),
                output_path=str(report_path),
                opts=opts,
                pages_imported=1,
                total_pages=1,
                primitive_count=8,
                text_count=0,
                elapsed_ms=10.0,
            )

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertNotIn("actual_text_entity_types", report["extra"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
