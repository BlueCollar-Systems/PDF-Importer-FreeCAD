"""One drawing item this host cannot build costs that item, never the page or file.

Owner rule: deliver, step down, report. A stroke whose edges do not join, a fill
whose face is refused, a source quadrilateral with unusable corners, or a compound
fill the Boolean builder refuses is drawn as plain lines in its own colour (or
left out when even that is impossible). Every other item and page still arrives,
and the item is listed in extra.geometry_items_degraded, counted as a warning,
explained in human_summary, and keeps its page out of certification.

Host faults outside these item sites still roll back the whole run; that contract
stays in test_transaction_failure_page_report_fc.py. Fixture data is fictional.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import test_clip_fill_degrade_fc as fixture
import test_regular_compound_source_fill_fc as compound

core = fixture.core
host = fixture.host


def line_row(seqno, *segments, color=(0.0, 0.0, 0.0)):
    """A stroke whose segments PyMuPDF gives in the two-point 'l' form."""
    return {"type": "s", "level": 0, "seqno": seqno, "rect": (0, 0, 200, 100), "color": color,
            "width": 1.0, "items": [("l", core.fitz.Point(*a), core.fitz.Point(*b)) for a, b in segments]}


def gap_row(seqno=5):
    # "20 60 m 180 60 l 20 70 m 180 70 l S": two parallel lines in one stroke.
    # The second segment does not start where the first ends.
    return line_row(seqno, ((20, 40), (180, 40)), ((20, 30), (180, 30)), color=(0.0, 0.0, 1.0))


def plain_row(seqno=1):
    return line_row(seqno, ((10, 10), (60, 10)), ((60, 10), (60, 50)))


def triangle_fill(seqno=3, fill=(0.8, 0.1, 0.1)):
    a, b, c = (core.fitz.Point(*point) for point in [(100, 10), (160, 10), (130, 60)])
    return {"type": "f", "level": 0, "seqno": seqno, "rect": core.fitz.Rect(100, 10, 160, 60),
            "fill": fill, "fill_opacity": 1.0, "even_odd": False, "closePath": True,
            "items": [("l", a, b), ("l", b, c), ("l", c, a)]}


def run_import(monkeypatch, tmp_path, pages, **changes):
    """The real multi-page entry point over the fixture's facade pages."""
    from pdfcadcore import fitz_loader

    document = fixture.Document()
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: fixture.Pdf(*[fixture.Page(rows) for rows in pages]))
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_pdf_file_sha256", lambda _path: "a" * 64)
    monkeypatch.setattr(core, "_autofit_import_view", lambda *_args: None)
    opts = core.ImportOptions(
        pages=list(range(1, len(pages) + 1)), import_mode="vector", import_text=False, ignore_images=True,
        raster_fallback=False, layer_mode="none", detect_arcs=False, make_faces=False, hatch_to_faces=False,
        model3d_mode="off", verbose=False, import_report_path=str(tmp_path / "D042_import_report.json"))
    for name, value in changes.items():
        setattr(opts, name, value)
    result = core.import_pdf(str(tmp_path / "D042.pdf"), opts)
    return document, opts, result


def page_groups(document):
    return sorted(obj.Name.rsplit("_", 1)[0] for obj in document.named("PDF_Page_"))


def outline_edges(obj):
    """The fake edges inside a step-down outline compound, as (x, y) point pairs."""
    return [[(point.x, point.y) for point in edge[1]] for edge in obj.Shape[1]]


# (1) a stroke whose edges do not join, on page 2 of 3

def test_a_stroke_that_is_not_one_wire_is_drawn_as_plain_lines_and_every_page_arrives(host, monkeypatch, tmp_path):
    styles = []
    monkeypatch.setattr(core, "_apply_style", lambda obj, stroke, fill, width, dashes, opts, **_kw:
                        styles.append((obj.Name, stroke, fill, width)))
    document, opts, result = run_import(
        monkeypatch, tmp_path, [[plain_row()], [plain_row(), gap_row()], [plain_row()]])
    assert result is True and opts.import_status == "success"
    assert page_groups(document) == ["PDF_Page_1", "PDF_Page_2", "PDF_Page_3"]
    block = opts._report_extra["geometry_items_degraded"]
    assert (block["total"], block["delivered_as_outline"], block["skipped"], block["pages"]) == (1, 1, 0, [2])
    entry, = block["items"]
    assert (entry["page_number"], entry["source_paint_order"], entry["kind"], entry["delivered"]) == (2, 5, "stroke", "outline")
    assert "Native drawing stroke construction failed" in entry["reason"]
    assert "BRep_API: command not done" in entry["reason"]                 # the native cause is kept
    outline = document.getObject(entry["created_entity_ids"][0])
    assert outline is not None and outline.Name.startswith("StepDownOutline")
    assert outline_edges(outline) == [[(20, 60), (180, 60)], [(20, 70), (180, 70)]]
    # In its own stroke colour and width.
    assert (outline.Name, (0.0, 0.0, 1.0), None, 1.0) in styles
    # It sits in page 2's group, like every other item of that page.
    page2 = next(obj for obj in document.named("PDF_Page_2"))
    assert outline in page2.Group
    # One console line names it.
    lines = [text for text in host["warning"] if "drawing item" in text]
    assert len(lines) == 1 and "page 2" in lines[0] and "plain lines" in lines[0]


# (2) a fill whose face the host refuses

@pytest.mark.parametrize("refusal", ["invalid_face", "stored_face"])
def test_a_refused_fill_face_is_drawn_as_its_outline_in_the_fill_colour(host, monkeypatch, refusal):
    checks = []

    class Face:
        def __init__(self, wire):
            self.edges = wire.edges

        def isNull(self):
            return False

        def isValid(self):
            checks.append(1)
            if refusal == "invalid_face":
                return False
            return len(checks) == 1          # valid when built, refused once stored

    monkeypatch.setattr(core.Part, "Face", Face, raising=False)
    styles = []
    monkeypatch.setattr(core, "_apply_style", lambda obj, stroke, fill, width, dashes, opts, **_kw:
                        styles.append((obj.Name, stroke, fill)))
    document, opts, group = fixture.import_page([triangle_fill(), plain_row(seqno=4)],
                                                fixture.options(hatch_to_faces=True))
    assert group is not None
    assert not document.named("Face")                                   # no invalid face is persisted
    if refusal == "stored_face":
        assert [name for name in document.removed if name.startswith("Face")]
    entry, = opts._report_extra["geometry_items_degraded"]["items"]
    assert (entry["kind"], entry["delivered"], entry["source_paint_order"]) == ("fill", "outline", 3)
    assert "face" in entry["reason"].lower() and "null or invalid" in entry["reason"]
    outline = document.getObject(entry["created_entity_ids"][0])
    assert outline in group.Group
    assert outline_edges(outline) == [[(100, 90), (160, 90)], [(160, 90), (130, 40)], [(130, 40), (100, 90)]]
    assert (outline.Name, (0.8, 0.1, 0.1), None) in styles             # fill colour as the line colour
    assert len(document.named("Batch")) == 1                            # the rest of the page


# (3) a quadrilateral with a NaN corner

def test_a_nan_quad_is_left_out_and_listed_and_the_page_survives(host):
    quad = SimpleNamespace(ul=(0, 0), ur=(float("nan"), 1), lr=(2, 2), ll=(0, 2))
    row = {"type": "s", "seqno": 9, "items": [("qu", quad)], "color": (1.0, 0.0, 0.0), "width": 1.0,
           "closePath": False}
    document, opts, group = fixture.import_page([row, plain_row()])
    assert group is not None and len(document.named("Batch")) == 1
    block = opts._report_extra["geometry_items_degraded"]
    assert (block["total"], block["skipped"], block["delivered_as_outline"]) == (1, 1, 0)
    entry, = block["items"]
    assert (entry["kind"], entry["delivered"], entry["created_entity_ids"]) == ("quad", "skipped", [])
    assert not document.named("StepDownOutline")


def test_when_even_plain_lines_are_refused_the_item_is_left_out_and_listed(host, monkeypatch):
    def refuse(_shapes):
        raise RuntimeError("compound refused")

    monkeypatch.setattr(core.Part, "makeCompound", refuse)
    document, opts, group = fixture.import_page([plain_row(), gap_row()],
                                                fixture.options(compound_batch_size=0))
    assert group is not None and len(document.named("Wire")) == 1      # the good stroke
    assert not document.named("StepDownOutline")
    entry, = opts._report_extra["geometry_items_degraded"]["items"]
    assert (entry["kind"], entry["delivered"], entry["created_entity_ids"]) == ("stroke", "skipped", [])
    line, = [text for text in host["warning"] if "drawing item" in text]
    assert "nothing was drawn for it" in line


def test_non_finite_outline_points_are_detected():
    nan = float("nan")
    point = lambda x, y: SimpleNamespace(x=x, y=y, z=0.0)  # noqa: E731
    finite = SimpleNamespace(Vertexes=[SimpleNamespace(Point=point(0.0, 0.0)), SimpleNamespace(Point=point(1.0, 1.0))])
    broken = SimpleNamespace(Vertexes=[SimpleNamespace(Point=point(0.0, 0.0)), SimpleNamespace(Point=point(nan, 1.0))])
    assert core._edges_have_nonfinite_points([finite]) is False
    assert core._edges_have_nonfinite_points([finite, broken]) is True
    assert core._edges_have_nonfinite_points([("l", [])]) is False      # unreadable fake edges are not judged


# (4) a compound fill the Boolean builder refuses

def test_a_refused_compound_fill_leaves_no_fill_object_and_no_receipt(monkeypatch, tmp_path):
    contours = [compound.rectangle(10, 10, 20, 20), compound.rectangle(15, 15, 10, 10)]
    compound.page_host(monkeypatch, contours)
    error = compound.fill.SourceFillError("synthetic Boolean refusal")

    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(compound.fill, "build_compound_fill", refuse)
    opts = compound.fixture.options(hatch_to_faces=True, compound_batch_size=0, scale_to_mm=False)
    document = compound.fixture.Document()
    group, _ = compound.run_page_wrapper(monkeypatch, tmp_path, contours, opts, document)
    assert group is not None
    assert not document.named("SourceCompoundFill")
    assert "source_compound_fill_delivery" not in opts._report_extra
    entry, = opts._report_extra["geometry_items_degraded"]["items"]
    assert (entry["kind"], entry["delivered"]) == ("compound_fill", "outline")
    assert "synthetic Boolean refusal" in entry["reason"]
    assert entry["created_entity_ids"] and all(document.getObject(name) for name in entry["created_entity_ids"])


def test_a_compound_fill_that_fails_after_its_objects_exist_removes_them(monkeypatch, tmp_path):
    contours = [compound.rectangle(10, 10, 20, 20), compound.rectangle(15, 15, 10, 10)]
    compound.page_host(monkeypatch, contours)
    monkeypatch.setattr(core, "_model3d_should_extrude", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(core, "_extrude_model3d_obj", lambda *_args: False)
    opts = compound.fixture.options(hatch_to_faces=True, compound_batch_size=0, scale_to_mm=False)
    document = compound.fixture.Document()
    group, _ = compound.run_page_wrapper(monkeypatch, tmp_path, contours, opts, document)
    assert not document.named("SourceCompoundFill") and not document.named("PDF_3D_Solid")
    removed = document.removed
    assert any(name.startswith("SourceCompoundFill") for name in removed)
    assert any(name.startswith("PDF_3D_Solid") for name in removed)
    assert all(not child.Name.startswith("SourceCompoundFill") for child in group.Group)
    assert "source_compound_fill_delivery" not in opts._report_extra
    entry, = opts._report_extra["geometry_items_degraded"]["items"]
    assert "extrusion failed" in entry["reason"] and entry["delivered"] == "outline"


# (5) the written report: warnings, plain sentence, fallback

def test_the_written_report_counts_explains_and_does_not_certify_the_step_down(host, monkeypatch, tmp_path):
    _document, opts, _result = run_import(monkeypatch, tmp_path, [[plain_row()], [gap_row()]])
    report = json.loads((tmp_path / "D042_import_report.json").read_text(encoding="utf-8"))
    extra = report["extra"]
    assert extra["geometry_items_degraded"]["total"] == 1
    assert report["result"]["warnings"] >= 1
    assert report["fallback"]["used"] is True
    assert report["fallback"]["reason"] == "geometry_items_degraded"
    sentence = ("1 drawing item on page 2 could not be built as requested (1 drawn as plain lines, "
                "0 not drawn)")
    assert sentence in extra["human_summary"]
    assert sentence in extra["geometry_degrade_note"]
    assert extra["import_contract_ready"]["ready"] is False
    assert extra["import_contract_ready"]["checks"]["geometry_delivery"] is False
    assert extra["representation_contract_scope"]["uncertified_degraded_pages"] == [2]


def test_a_clean_report_has_no_geometry_block_and_no_extra_sentence(host, monkeypatch, tmp_path):
    _document, opts, _result = run_import(monkeypatch, tmp_path, [[plain_row()], [plain_row()]])
    report = json.loads((tmp_path / "D042_import_report.json").read_text(encoding="utf-8"))
    assert "geometry_items_degraded" not in report["extra"]
    assert "drawing item" not in report["extra"]["human_summary"]
    assert report["fallback"]["used"] is False
    assert "geometry_delivery" not in report["extra"]["import_contract_ready"]["checks"]


# (6) the import session never certifies the page

def test_the_session_lists_the_degraded_page(host, monkeypatch, tmp_path):
    _document, opts, _result = run_import(monkeypatch, tmp_path, [[plain_row()], [gap_row()], [plain_row()]])
    session = opts._report_extra["import_session"]
    assert session["degraded_pages"] == [2]
    assert session["completed_pages"] == [1, 2, 3]


# Console lines are capped; the report keeps them all (up to its own limit).

def test_console_lines_are_capped_and_closed_by_one_overflow_line(host):
    rows = [gap_row(seqno=index) for index in range(core.GEOMETRY_ITEM_DEGRADE_CONSOLE_LIMIT + 3)]
    _document, opts, _group = fixture.import_page(rows)
    block = opts._report_extra["geometry_items_degraded"]
    assert block["total"] == len(rows) and len(block["items"]) == len(rows)
    lines = [text for text in host["warning"] if "drawing item" in text]
    assert len(lines) == core.GEOMETRY_ITEM_DEGRADE_CONSOLE_LIMIT
    assert "3 more drawing items" in core._geometry_degrade_console_overflow_line(opts)


def test_report_items_are_capped_but_all_are_counted():
    opts = core.ImportOptions()
    for index in range(core.GEOMETRY_ITEM_DEGRADE_REPORT_LIMIT + 2):
        core._record_degraded_geometry_item(opts, {"page_number": 1, "source_paint_order": index,
                                                   "kind": "stroke", "reason": "x", "delivered": "skipped",
                                                   "created_entity_ids": []})
    block = opts._report_extra["geometry_items_degraded"]
    assert block["total"] == core.GEOMETRY_ITEM_DEGRADE_REPORT_LIMIT + 2
    assert len(block["items"]) == core.GEOMETRY_ITEM_DEGRADE_REPORT_LIMIT and block["items_truncated"]


# A host fault outside the item sites is still a whole-run failure.

def test_a_host_fault_outside_the_item_sites_still_fails_the_run(host, monkeypatch, tmp_path):
    def broken_flush(*_args, **_kwargs):
        raise OSError("disk full while caching a picture")

    import PDFPaperDisplay
    monkeypatch.setattr(PDFPaperDisplay, "create_paper", broken_flush)
    monkeypatch.setattr(core, "_err", lambda *_args: None)
    with pytest.raises(OSError):
        run_import(monkeypatch, tmp_path, [[plain_row()], [gap_row()]])
