"""Text delivered as raw glyph codes reaches the FreeCAD operator and report.

This host does not call pdfcadcore.extract_page for text: it builds its own
page text dictionaries, and its raster cross-check asserts that the core's
dictionary and its own agree character for character. So the host has to run
the shared recovery on every dictionary it builds, and publish what happened.

Every record here is synthetic (SAMPLE font, fictional job 1000-01).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402


def recovered(page=1, codes=(2, 4, 5, 1), route="outline_identity", bbox=(10.0, 90.0, 60.0, 104.0)):
    return {
        "page_number": page, "font_name": "SampleGothic", "source_xref": 7,
        "status": "recovered", "route": route, "routes": {route: len(codes)},
        "reason": "", "glyphs": len(codes), "glyphs_recovered": len(codes),
        "raw_codes": list(codes), "raw_codes_truncated": False,
        "reference_faces": ["sample.ttf"], "bbox_pdf": list(bbox),
    }


def unproven(page=1, codes=(9, 9), reason="no_reference_face_available",
             bbox=(200.0, 90.0, 240.0, 104.0)):
    return {
        "page_number": page, "font_name": "SampleGothic", "source_xref": 7,
        "status": "unproven", "route": "", "routes": {}, "reason": reason,
        "detail": "no installed face matches 'samplegothic' (0 faces indexed)",
        "glyphs": len(codes), "glyphs_unproven": len(codes),
        "raw_codes": list(codes), "raw_codes_truncated": False,
        "looked_for_face": "samplegothic", "reference_faces": [],
        "bbox_pdf": list(bbox),
    }


def seed(opts, *records):
    store = {}
    for index, record in enumerate(records):
        store[(record["page_number"], tuple(record["bbox_pdf"]), index)] = record
    opts._glyph_code_records = store
    return opts


def written_report(tmp_path, opts):
    report_path = tmp_path / "import_report.json"
    core.write_import_report(
        pdf_path=str(tmp_path / "D042.pdf"), output_path=str(report_path), opts=opts,
        pages_imported=1, total_pages=1, elapsed_ms=1.0,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


@pytest.fixture
def console(monkeypatch):
    lines = {"msg": [], "warn": []}
    monkeypatch.setattr(core, "_msg", lambda text: lines["msg"].append(text))
    monkeypatch.setattr(core, "_warn", lambda text: lines["warn"].append(text))
    return lines


def test_a_recovered_span_is_reported_with_its_route_and_never_warns(tmp_path, console):
    opts = seed(core.ImportOptions(), recovered(), recovered(codes=(0x30, 0x36)))

    report = written_report(tmp_path, opts)

    block = report["extra"]["text_glyph_codes"]
    assert block["schema"] == "bcs.text_glyph_codes/1.0"
    assert (block["recovered"], block["unproven"], block["spans_examined"]) == (2, 0, 2)
    assert block["spans_by_route"] == {"outline_identity": 2}
    assert block["glyphs_recovered"] == 6                  # four codes and two
    assert report["result"]["warnings"] == 0

    core._emit_glyph_code_console_line(opts)
    assert console["warn"] == []
    assert "outline_identity" in console["msg"][0]
    assert "not read from the PDF" in console["msg"][0]


def test_an_unproven_span_warns_and_names_the_font_and_its_raw_codes(tmp_path, console):
    opts = seed(core.ImportOptions(), recovered(), unproven(), unproven(codes=(11,)))

    report = written_report(tmp_path, opts)

    block = report["extra"]["text_glyph_codes"]
    assert (block["recovered"], block["unproven"]) == (1, 2)
    assert block["by_reason"] == {"no_reference_face_available": 2}
    # Unproven spans sort first, so the report's item cap never hides one.
    assert [item["status"] for item in block["items"]][:2] == ["unproven", "unproven"]
    assert block["items"][0]["font_name"] == "SampleGothic"
    assert block["items"][0]["raw_codes"] == [9, 9]
    assert block["items"][0]["bbox_pdf"] == [200.0, 90.0, 240.0, 104.0]
    assert report["result"]["warnings"] == 2

    core._emit_glyph_code_console_line(opts)
    assert console["msg"] == []
    assert "could not be proven" in console["warn"][0]
    assert "text_glyph_codes" in console["warn"][0]


def test_the_count_adds_to_the_other_warning_sums_and_never_replaces_them(tmp_path):
    opts = core.ImportOptions()
    core._record_clip_fill_issues(opts, 1, [
        {"seqno": 2, "action": "dropped-unsupported", "severity": "warning", "dropped": True},
    ])
    core._record_degraded_text_item(opts, {
        "source_item_id": "p1:b0:l0:s0", "page_number": 1, "delivered": False,
    })
    baseline = written_report(tmp_path, opts)["result"]["warnings"]
    assert baseline == 2                                   # one fill, one text item

    seed(opts, unproven(), unproven(codes=(12,)), recovered())
    report = written_report(tmp_path, opts)

    assert report["result"]["warnings"] == baseline + 2    # the two unproven spans
    assert report["extra"]["clip_fill_delivery"]["dropped"] == 1
    assert report["extra"]["text_items_degraded"]["total"] == 1


def test_a_document_with_no_glyph_code_spans_publishes_no_block(tmp_path, console):
    report = written_report(tmp_path, core.ImportOptions(import_text=False))

    assert "text_glyph_codes" not in report["extra"]
    assert report["result"]["warnings"] == 0
    core._emit_glyph_code_console_line(core.ImportOptions())
    assert console["warn"] == [] and console["msg"] == []


# ── the host's own text dictionaries ──


def test_every_page_text_dictionary_this_host_builds_goes_through_recovery(monkeypatch):
    seen = []
    monkeypatch.setattr(core, "recover_glyph_codes_in_place",
                        lambda page, tdict: seen.append((page, tdict)))
    monkeypatch.setattr(core, "glyph_code_issues", lambda page: [unproven()])
    delivered = {"blocks": [{"type": 0, "lines": []}]}

    class Page:
        def get_text(self, kind):
            assert kind == "dict"
            return delivered

    opts = core.ImportOptions()
    page = Page()
    result = core._page_text_dict(page, opts)

    assert result is delivered
    assert seen == [(page, delivered)]
    assert len(core._glyph_code_records(opts)) == 1


def test_the_raster_cross_check_dictionary_is_recovered_with_the_same_call(monkeypatch):
    # _raster_source_coverage_bbox compares the CORE's dictionary against this
    # host's item text. If only one side were recovered every raster-delivered
    # text item on an affected sheet would raise.
    fitz = core.fitz
    if fitz is None:                                        # pragma: no cover
        pytest.skip("PDF engine is not available in this environment")
    document = fitz.open()
    page = document.new_page()
    span = {
        "font": "SampleGothic", "bbox": (10.0, 90.0, 60.0, 104.0),
        "origin": (10.0, 100.0),
        "chars": [{"c": "D", "quad": ((10.0, 90.0), (20.0, 90.0), (20.0, 104.0), (10.0, 104.0))}],
    }
    raw = {"blocks": [{"type": 0, "lines": [{"spans": [span]}]}]}
    monkeypatch.setattr("pdfcadcore.primitive_extractor._raw_text_with_source_quads",
                        lambda _page: raw)
    recovered_dicts = []
    monkeypatch.setattr(core, "recover_glyph_codes_in_place",
                        lambda _page, tdict: recovered_dicts.append(tdict))
    monkeypatch.setattr(core, "glyph_code_issues", lambda _page: [])
    item = {
        "bbox": (10.0, 90.0, 60.0, 104.0), "text": "D", "pdf_sha256": "0" * 64,
        "page_number": 1, "block_index": 0, "line_index": 0, "span_index": 0,
        "origin": (10.0, 100.0), "span": {"font": "SampleGothic"},
    }

    bbox = core._raster_source_coverage_bbox(item, page, core.ImportOptions())

    assert recovered_dicts == [raw]
    assert bbox == (10.0, 90.0, 60.0, 104.0)
    document.close()
