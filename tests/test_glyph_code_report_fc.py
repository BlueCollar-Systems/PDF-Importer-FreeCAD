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
             bbox=(200.0, 90.0, 240.0, 104.0), limitation=False):
    return {
        "page_number": page, "font_name": "SampleGothic", "source_xref": 7,
        "status": "unproven", "route": "", "routes": {}, "reason": reason,
        "limitation": limitation,
        "detail": "no installed face matches 'samplegothic' (0 faces indexed)",
        "glyphs": len(codes), "glyphs_unproven": len(codes),
        "raw_codes": list(codes), "raw_codes_truncated": False,
        "looked_for_face": "samplegothic", "reference_faces": [],
        "bbox_pdf": list(bbox),
    }


def seed(opts, *records):
    store = {}
    for index, record in enumerate(records):
        # A distinct box per seeded record: one span is one row, so two rows
        # sharing a box would merge exactly as they do in a real run.
        box = tuple(value + index for value in record["bbox_pdf"])
        store[(record["page_number"], box)] = record
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


def _one_span_page(monkeypatch, delivered_text, recovered_text):
    """A page whose plain dictionary and per-character dictionary agree."""
    plain_span = {"font": "SampleGothic", "bbox": (10.0, 90.0, 60.0, 104.0),
                  "size": 10.0, "origin": (10.0, 100.0), "text": delivered_text}
    plain = {"blocks": [{"type": 0, "lines": [{"spans": [plain_span]}]}]}
    raw_span = {
        "font": "SampleGothic", "bbox": (10.0, 90.0, 60.0, 104.0),
        "origin": (10.0, 100.0),
        "chars": [
            {"c": character, "origin": (10.0 + index * 7.0, 100.0),
             "quad": ((10.0, 90.0), (20.0, 90.0), (20.0, 104.0), (10.0, 104.0))}
            for index, character in enumerate(delivered_text)
        ],
    }
    raw = {"blocks": [{"type": 0, "lines": [{"spans": [raw_span]}]}]}

    def recover(_page, tdict):
        # Stands in for the shared module: it only ever rewrites a dictionary
        # that carries per-character origins.
        if tdict is raw:
            for index, character in enumerate(recovered_text):
                raw_span["chars"][index]["c"] = character

    monkeypatch.setattr("pdfcadcore.primitive_extractor._raw_text_with_source_quads",
                        lambda _page: raw)
    monkeypatch.setattr(core, "recover_glyph_codes_in_place", recover)
    monkeypatch.setattr(core, "glyph_code_issues", lambda _page: [])
    monkeypatch.setattr(core, "page_delivers_glyph_codes", lambda _page: True)
    return plain, plain_span, raw, raw_span


def test_the_host_dictionary_takes_its_text_from_the_per_character_one(monkeypatch):
    # page.get_text("dict") has no per-character origins, so nothing in it can
    # bind a character to the glyph that drew it. The host recovers the
    # dictionary that does and copies the result across.
    plain, plain_span, raw, _raw_span = _one_span_page(monkeypatch, "\x01\x02\x03\x04", "D042")

    class Page:
        number = 0
        parent = object()

        def get_text(self, kind):
            assert kind == "dict"
            return plain

    result = core._page_text_dict(Page(), core.ImportOptions())

    assert result is plain
    assert plain_span["text"] == "D042"


def test_a_page_that_draws_no_unmapped_character_costs_nothing(monkeypatch):
    monkeypatch.setattr(core, "page_delivers_glyph_codes", lambda _page: False)
    monkeypatch.setattr("pdfcadcore.primitive_extractor._raw_text_with_source_quads",
                        lambda _page: pytest.fail("a clean page was extracted twice"))
    delivered = {"blocks": [{"type": 0, "lines": []}]}

    class Page:
        number = 0
        parent = object()

        def get_text(self, kind):
            return delivered

    assert core._page_text_dict(Page(), core.ImportOptions()) is delivered


def test_the_raster_cross_check_reads_the_very_dictionary_the_text_came_from(monkeypatch):
    # _raster_source_coverage_bbox compares the core's dictionary against this
    # host's item text. Two separate recoveries could disagree; one shared
    # dictionary cannot.
    fitz = core.fitz
    if fitz is None:                                        # pragma: no cover
        pytest.skip("PDF engine is not available in this environment")
    document = fitz.open()
    page = document.new_page()
    plain, plain_span, raw, _raw_span = _one_span_page(monkeypatch, "\x01\x02\x03\x04", "D042")
    opts = core.ImportOptions()

    class Page:
        number = 0
        parent = page.parent

        def get_text(self, _kind):
            return plain

    core._page_text_dict(Page(), opts)
    item = {
        "bbox": (10.0, 90.0, 60.0, 104.0), "text": plain_span["text"],
        "pdf_sha256": "0" * 64,
        "page_number": 1, "block_index": 0, "line_index": 0, "span_index": 0,
        "origin": (10.0, 100.0), "span": {"font": "SampleGothic"},
    }

    bbox = core._raster_source_coverage_bbox(item, page, opts)

    assert item["text"] == "D042"
    assert bbox == (10.0, 90.0, 60.0, 104.0)
    document.close()


def test_a_dictionary_that_cannot_be_built_is_reported_as_this_run_s_limit(monkeypatch):
    monkeypatch.setattr(core, "page_delivers_glyph_codes", lambda _page: True)

    def refuse(_page):
        raise RuntimeError("EX404")

    monkeypatch.setattr("pdfcadcore.primitive_extractor._raw_text_with_source_quads", refuse)
    limits = []
    monkeypatch.setattr(core, "record_glyph_code_limitation",
                        lambda page, reason, detail="": limits.append((reason, detail)))
    monkeypatch.setattr(core, "glyph_code_issues", lambda _page: [])
    delivered = {"blocks": [{"type": 0, "lines": []}]}

    class Page:
        number = 0
        parent = object()

        def get_text(self, _kind):
            return delivered

    assert core._page_text_dict(Page(), core.ImportOptions()) is delivered
    assert limits and limits[0][0] == "recovered_text_not_transferable"


def test_a_degraded_row_says_when_its_text_was_recovered_rather_than_read():
    # source_text is whatever the dictionary held, and on an affected sheet
    # that is a string this run recovered. The row has to say so.
    opts = seed(core.ImportOptions(), recovered(bbox=(10.0, 90.0, 60.0, 104.0)))
    item = {"page_number": 1, "bbox": (10.0, 90.0, 60.0, 104.0), "text": "D042"}
    record = {"source_item_id": "p1:b0:l0:s0", "requested_type": "native",
              "final_type": None}

    entry = core._degraded_text_item_report_entry(item, record, opts)

    assert entry["source_text"] == "D042"
    assert entry["text_recovered_by"] == "outline_identity"
    assert "text_recovered_by" not in core._degraded_text_item_report_entry(item, record)


def test_a_run_limitation_is_not_reported_as_the_document_failing(tmp_path, console):
    opts = seed(core.ImportOptions(),
                unproven(reason="recovered_text_not_transferable", limitation=True))

    report = written_report(tmp_path, opts)
    assert report["extra"]["text_glyph_codes"]["unproven_from_run_limitation"] == 1

    core._emit_glyph_code_console_line(opts)
    assert "a limitation of this import, not of the sheet" in console["warn"][0]
    assert "no usable Unicode map" not in console["warn"][0]
