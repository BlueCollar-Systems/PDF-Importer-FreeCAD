from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from PDFVectorImporter.adapters import freecad_harness
from PDFVectorImporter.pdfcadcore.import_config import ImportConfig
from PDFVectorImporter.pdfcadcore.fitz_loader import PdfOpenError


class _Core:
    class ImportOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


def test_harness_maps_canonical_lineweight_config_to_core_linewidth_option():
    config = ImportConfig.vector()

    options = freecad_harness._build_import_options(
        _Core,
        config,
        [1],
        text_mode="3d_text",
    )

    assert not hasattr(config, "assign_linewidth")
    assert options.assign_linewidth is config.assign_lineweight
    assert options.text_mode == "3d_text"
    assert options.pages == [1]


@pytest.mark.parametrize(
    "text_mode",
    ("text", "labels", "glyphs", "3d_text", "geometry", "raster"),
)
def test_harness_propagates_each_explicit_canonical_text_mode(text_mode):
    config = ImportConfig.vector()

    options = freecad_harness._build_import_options(
        _Core,
        config,
        [1, 3],
        text_mode=text_mode,
    )

    assert config.text_mode == text_mode
    assert config.import_text is True
    assert options.text_mode == text_mode
    assert options.import_text is True
    assert options.pages == [1, 3]


@pytest.mark.parametrize(
    "text_mode",
    (None, "", " Labels", "labels ", "LABELS", "native_text", 3),
)
def test_harness_rejects_missing_or_noncanonical_payload_text_mode(text_mode):
    with pytest.raises(ValueError, match="text_mode"):
        freecad_harness._require_payload_text_mode({"text_mode": text_mode})


def test_harness_all_pages_defers_resolution_to_core_bound_source_bytes(tmp_path):
    missing_original = tmp_path / "may-change-before-core-binds.pdf"

    assert freecad_harness.parse_pages("all", str(missing_original)) is None


@pytest.mark.parametrize("result", (None, False, 0, 1, "true"))
def test_harness_rejects_every_non_exact_true_core_result(result):
    with pytest.raises(RuntimeError, match="exact True"):
        freecad_harness._require_exact_core_success(result)


def test_harness_accepts_exact_true_core_result():
    assert freecad_harness._require_exact_core_success(True) is True


@pytest.mark.parametrize(
    "result,expected",
    (
        ({"status": "PASS"}, 0),
        ({"status": "FAIL"}, 1),
        ({"status": "CANCELLED"}, 1),
        ({}, 1),
        (None, 1),
    ),
)
def test_harness_exit_code_is_zero_only_for_literal_pass(result, expected):
    assert freecad_harness._result_exit_code(result) == expected


def test_environment_triggered_harness_branch_propagates_system_exit():
    source = inspect.getsource(freecad_harness)
    environment_branch = source[source.index("elif os.environ.get(\"BC_PDF_QA_PAYLOAD\"):") :]

    assert "raise SystemExit(main())" in environment_branch


@pytest.mark.parametrize(
    "failure,expected_message",
    (
        (
            PdfOpenError("malformed_pdf", "xref table is invalid"),
            "PdfOpenError[malformed_pdf]: xref table is invalid",
        ),
        (Exception("unexpected harness defect"), "Exception: unexpected harness defect"),
    ),
)
def test_harness_serializes_all_import_exceptions_as_meaningful_failures(
    failure: Exception,
    expected_message: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result_path = tmp_path / "result.json"
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "test_id": "exception-contract",
                "result_json": str(result_path),
                "text_mode": "3d_text",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BC_PDF_QA_PAYLOAD", str(payload_path))
    monkeypatch.setattr(
        freecad_harness,
        "setup_import_paths",
        lambda *_args: (_ for _ in ()).throw(failure),
    )

    exit_code = freecad_harness.main()
    result = json.loads(result_path.read_text(encoding="utf-8"))

    assert exit_code == 1
    assert result["status"] == "FAIL"
    assert result["message"] == expected_message
    assert failure.__class__.__name__ in result["traceback"]
