from __future__ import annotations

import hashlib
import inspect
import json
import os
import stat
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore import fitz_loader  # noqa: E402
from pdfcadcore.page_visual import (  # noqa: E402
    PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA,
    page_visual_fallback_proof_v2_verified,
)


def _blank_pdf_bytes(page_count: int = 1) -> bytes:
    document = core.fitz.open()
    try:
        for _ in range(page_count):
            document.new_page()
        return bytes(document.tobytes())
    finally:
        document.close()


def _raster_result(pdf_sha256: str, page_number: int) -> dict:
    return {
        "outcome": "verified",
        "entity_type": "raster",
        "created_entity_ids": [f"PageRaster{page_number:03d}"],
        "evidence": {
            "host_entity_type": "Image::ImagePlane",
            "source_page_number": page_number,
            "pdf_sha256": pdf_sha256,
            "source_asset_sha256": "f" * 64,
            "raster_content_verified": True,
            "raster_file_included": True,
        },
    }


def _bound_opts(tmp_path: Path, *, pages: tuple[int, ...] = (1,)):
    source_path = tmp_path / "source.pdf"
    source_bytes = _blank_pdf_bytes(max(pages))
    source_path.write_bytes(source_bytes)
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    core._initialize_pdf_source_attempt(str(source_path), opts)
    core._capture_page_visual_runtime_authority(opts, list(pages))
    return source_path, source_bytes, opts


def test_safe_open_bytes_uses_the_supplied_snapshot() -> None:
    payload = _blank_pdf_bytes(2)

    with fitz_loader.safe_open_bytes(payload) as document:
        assert document.page_count == 2


def test_source_attempt_reads_original_once_and_survives_path_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_path = tmp_path / "source.pdf"
    original = _blank_pdf_bytes(1)
    replacement = _blank_pdf_bytes(2)
    source_path.write_bytes(original)
    real_read_bytes = Path.read_bytes
    reads = 0

    def counted_read_bytes(path: Path) -> bytes:
        nonlocal reads
        if path.resolve() == source_path.resolve():
            reads += 1
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", counted_read_bytes)
    opts = core.ImportOptions()
    snapshot_path = Path(core._initialize_pdf_source_attempt(str(source_path), opts))
    source_path.write_bytes(replacement)

    assert reads == 1
    assert opts._pdf_sha256 == hashlib.sha256(original).hexdigest()
    assert core._validated_pdf_source_bytes(opts) == original
    assert snapshot_path.read_bytes() == original
    assert Path(core._validated_pdf_source_snapshot_path(opts)) == snapshot_path
    with core._open_pdf_source_attempt(opts) as document:
        assert document.page_count == 1


def test_bound_external_snapshot_fails_closed_after_mutation(tmp_path: Path) -> None:
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(_blank_pdf_bytes())
    opts = core.ImportOptions()
    snapshot_path = Path(core._initialize_pdf_source_attempt(str(source_path), opts))
    os.chmod(snapshot_path, stat.S_IREAD | stat.S_IWRITE)
    snapshot_path.write_bytes(_blank_pdf_bytes(2))

    with pytest.raises(ValueError, match="snapshot.*digest"):
        core._validated_pdf_source_snapshot_path(opts)


def test_v2_page_fallback_is_bound_to_selected_exact_page(tmp_path: Path) -> None:
    _source_path, _source_bytes, opts = _bound_opts(tmp_path, pages=(2,))
    digest = opts._pdf_sha256

    with pytest.raises(ValueError, match="authority|selected page"):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256=digest,
            raw_tdict={"blocks": []},
            raster_result=_raster_result(digest, 1),
        )

    info = core._record_no_source_text_page_fallback(
        opts,
        page_num=2,
        pdf_sha256=digest,
        raw_tdict={"blocks": []},
        raster_result=_raster_result(digest, 2),
    )

    assert info["source_item_ids"] == ["p2:page"]
    observation = opts.page_visual_source_observations["p2:page"]
    authority = opts._page_visual_authority
    for attempt in opts.text_delivery_attempts[:-1]:
        proof = attempt["proof"]
        assert proof["schema"] == PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA
        assert page_visual_fallback_proof_v2_verified(
            proof,
            observation=observation,
            authority=authority,
            expected_requested_type="labels",
            expected_attempted_type=attempt["attempted_type"],
        )
    assert core._validate_freecad_text_representation_delivery(
        opts,
        opts.text_delivery_attempts,
    )["verified"] is True


def test_rawdict_only_v1_page_authorization_is_rejected() -> None:
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    opts._pdf_sha256 = "a" * 64

    with pytest.raises(ValueError, match="authority"):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256=opts._pdf_sha256,
            raw_tdict={"blocks": []},
            raster_result=_raster_result(opts._pdf_sha256, 1),
        )


def test_authority_rejects_wrong_retained_source_bytes(tmp_path: Path) -> None:
    _source_path, _source_bytes, opts = _bound_opts(tmp_path)
    opts._pdf_source_bytes = _blank_pdf_bytes(2)

    with pytest.raises(ValueError, match="source bytes.*digest"):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256=opts._pdf_sha256,
            raw_tdict={"blocks": []},
            raster_result=_raster_result(opts._pdf_sha256, 1),
        )


def test_reset_clears_authority_session_source_and_provenance(tmp_path: Path) -> None:
    _source_path, _source_bytes, opts = _bound_opts(tmp_path)
    snapshot_path = Path(opts._pdf_source_snapshot_path)
    opts._source_provenance_objects = [object()]
    opts._import_session_id = "old-session"
    opts._provenance_page = 1

    core._reset_import_run_state(opts)

    assert not snapshot_path.exists()
    assert opts._pdf_sha256 == ""
    assert opts._pdf_source_bytes is None
    assert opts._pdf_source_snapshot_path is None
    assert opts._pdf_source_snapshot_owner is None
    assert opts._pdf_source_provenance == {}
    assert opts._page_visual_authority is None
    assert opts._page_visual_session_anchor is None
    assert opts.page_visual_source_observations == {}
    assert opts._source_provenance_objects == []
    assert opts._import_session_id == ""
    assert opts._provenance_page == 0


def test_live_report_uses_authority_but_persisted_report_is_fail_closed(
    tmp_path: Path,
) -> None:
    source_path, original, opts = _bound_opts(tmp_path)
    digest = hashlib.sha256(original).hexdigest()
    core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256=digest,
        raw_tdict={"blocks": []},
        raster_result=_raster_result(digest, 1),
    )
    opts._report_extra = {
        "actual_text_entity_types": {
            "entity_type": "raster",
            "count": 1,
            "font_rendered": False,
            "examples": [],
        }
    }
    source_path.write_bytes(_blank_pdf_bytes(2))
    output_path = tmp_path / "report.json"

    core.write_import_report(
        pdf_path=str(source_path),
        output_path=str(output_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    live_report = opts._live_import_report
    assert live_report._page_visual_authority is opts._page_visual_authority
    assert live_report.input["file"] == str(source_path)
    assert live_report.input["sha256"] == digest
    assert live_report.extra["import_contract_ready"]["checks"]["text_delivery"] is True
    persisted = json.loads(output_path.read_text(encoding="utf-8"))
    assert persisted["input"]["file"] == str(source_path)
    assert persisted["input"]["sha256"] == digest
    assert (
        persisted["extra"]["import_contract_ready"]["checks"]["text_delivery"]
        is False
    )


def test_freecad_entrypoints_and_harness_route_all_pymupdf_imports_through_loader() -> None:
    import_source = inspect.getsource(core.import_pdf.__wrapped__)
    page_source = inspect.getsource(core.import_pdf_page.__wrapped__)
    command_source = (
        REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCmd.py"
    ).read_text(encoding="utf-8")
    harness_source = (
        REPO_ROOT / "PDFVectorImporter" / "adapters" / "freecad_harness.py"
    ).read_text(encoding="utf-8")

    assert "_open_pdf_source_attempt" in import_source
    assert "safe_open(pdf_path)" not in import_source
    assert "_pdf_file_sha256(pdf_path)" not in import_source
    assert "return import_pdf(" in page_source
    assert "_open_pdf_source_attempt" not in page_source
    assert "safe_open(pdf_path)" not in page_source
    for source in (command_source, harness_source):
        assert "import pymupdf" not in source
        assert "import fitz" not in source
        assert "pdfcadcore.fitz_loader" in source
