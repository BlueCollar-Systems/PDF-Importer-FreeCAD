"""One clipped fill that cannot be delivered costs that fill, never the page.

The core resolver drops an unprovable fill and records it on the rows; this host
adds a per-fill backstop around its own compound-fill build. Both kinds of drop
must reach the operator (one console line per import) and the import report
(extra.clip_fill_delivery plus result.warnings). Fixture geometry is synthetic.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
import PDFLatePaint  # noqa: E402
import PDFPaperDisplay  # noqa: E402

# The host builder is made to fail for any mask that starts at this x (PDF points).
UNBUILDABLE_X = 100


class Vector:
    def __init__(self, x, y, z=0):
        self.x, self.y, self.z = x, y, z


class Wire:
    def __init__(self, edges):
        self.edges = edges

    def isClosed(self):
        a, b = self.edges[0][1][0], self.edges[-1][1][-1]
        return (a.x, a.y) == (b.x, b.y)


class HostObject:
    def __init__(self, name, type_id):
        self.Name = self.Label = name
        self.TypeId = type_id
        self.Group = []
        self.Placement = SimpleNamespace(Base=SimpleNamespace(y=0.0))

    def addProperty(self, *_args):
        return self

    def addObject(self, child):
        self.Group.append(child)

    def removeObject(self, child):
        self.Group.remove(child)

    def isDerivedFrom(self, type_id):
        return self.TypeId == type_id


class Document:
    def __init__(self):
        self.Objects = []
        self.removed = []

    def addObject(self, type_id, name):
        host = HostObject(f"{name}_{len(self.Objects) + len(self.removed)}", type_id)
        self.Objects.append(host)
        return host

    def removeObject(self, name):
        self.removed.append(name)
        self.Objects = [host for host in self.Objects if host.Name != name]

    def getObject(self, name):
        return next((host for host in self.Objects if host.Name == name), None)

    def recompute(self, *_args):
        return None

    def openTransaction(self, _name):
        return None

    commitTransaction = abortTransaction = recompute

    def named(self, prefix):
        return [host for host in self.Objects if host.Name.startswith(prefix)]


class Page:
    rotation = 0

    def __init__(self, rows):
        self.rows = rows
        self.rect = core.fitz.Rect(0, 0, 200, 100)

    def get_drawings(self, *, extended):
        assert extended is True
        return self.rows

    def get_images(self, full=True):
        return []

    def get_text(self, kind, **_kwargs):
        return {"blocks": []} if kind in {"dict", "rawdict"} else []


class Pdf:
    is_encrypted = False

    def __init__(self, *pages):
        self.pages = pages

    def __len__(self):
        return len(self.pages)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def load_page(self, index):
        return self.pages[index]

    def close(self):
        return None


@pytest.fixture
def host(monkeypatch):
    """The FreeCAD surface the vector page path touches, and what it printed."""
    console = {"message": [], "warning": [], "error": []}
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(
        GuiUp=False,
        Console=SimpleNamespace(
            PrintMessage=console["message"].append,
            PrintWarning=console["warning"].append,
            PrintError=console["error"].append,
        ),
    ))

    def make_face(wires, _maker):
        if any(wire.edges[0][1][0].x == UNBUILDABLE_X for wire in wires):
            raise RuntimeError("face maker found no valid wires")
        face = SimpleNamespace(normalAt=lambda *_args: Vector(0, 0, 1))
        return SimpleNamespace(Faces=[face], isValid=lambda: True)

    monkeypatch.setattr(core, "Part", SimpleNamespace(
        LineSegment=lambda a, b: SimpleNamespace(toShape=lambda: ("l", [a, b])),
        Wire=Wire, makeFace=make_face, makeCompound=lambda shapes: ("compound", list(shapes)),
    ))
    monkeypatch.setattr(core, "_to_fc", lambda point, page_h, opts, scale: Vector(point[0], page_h - point[1]))
    monkeypatch.setattr(core, "_apply_style", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(PDFLatePaint, "apply_final_paints", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(PDFPaperDisplay, "create_paper", lambda *_args, **_kwargs: None)
    return console


def options(**changes):
    opts = core.ImportOptions(
        import_mode="vector", import_text=False, ignore_images=True, raster_fallback=False,
        layer_mode="none", detect_arcs=False, make_faces=False, hatch_to_faces=False, verbose=False,
    )
    for name, value in changes.items():
        setattr(opts, name, value)
    core._reset_import_run_state(opts)
    opts._pdf_sha256 = "a" * 64                              # import_pdf hashes the file before any page
    return opts


def import_page(rows, opts=None, page_num=1):
    opts = opts or options()
    document = Document()
    pages = [Page([])] * (page_num - 1) + [Page(rows)]
    group, _text = core._import_pdf_page_inner(Pdf(*pages), "D042.pdf", page_num, opts, document)
    return document, opts, group


def ring_clip(x, level=0):
    """A 20 x 10 box with a counter hole at ``x``: a non-rectangular even-odd clip."""
    return {"type": "clip", "level": level, "scissor": (x, 0, x + 20, 10), "even_odd": True, "items": [
        ("l", (x, 0), (x + 20, 0)), ("l", (x + 20, 0), (x + 20, 10)),
        ("l", (x + 20, 10), (x, 10)), ("l", (x, 10), (x, 0)),
        ("re", (x + 5, 2, x + 15, 8), 1)]}


def wedge_clip(x, level):
    return {"type": "clip", "level": level, "scissor": (x, 0, x + 20, 10), "even_odd": True, "items": [
        ("l", (x, 0), (x + 20, 0)), ("l", (x + 20, 0), (x + 20, 10)), ("l", (x + 20, 10), (x, 0))]}


def curved_clip():
    """A bulging box: cutting it to a narrower fill flattens the curve (warning, not dropped)."""
    return {"type": "clip", "level": 0, "scissor": (0, 20, 20.5, 30), "even_odd": True, "items": [
        ("l", (0, 20), (10, 20)), ("c", (10, 20), (24, 20), (24, 30), (10, 30)),
        ("l", (10, 30), (0, 30)), ("l", (0, 30), (0, 20))]}


def rect_clip(bounds, level=0):
    return {"type": "clip", "level": level, "scissor": bounds, "even_odd": False, "items": [("re", bounds, 1)]}


def fill(seqno, bounds, level=1):
    # PyMuPDF rectangles, as on a real page: the resolver answers in the type it was given.
    return {"type": "f", "level": level, "seqno": seqno, "rect": core.fitz.Rect(bounds),
            "items": [("re", core.fitz.Rect(bounds), 1)],
            "fill": (0.2, 0.4, 0.6), "fill_opacity": 1.0, "even_odd": False}


def stroke(seqno, x):
    return {"type": "s", "level": 0, "seqno": seqno, "rect": (x, 40, x + 10, 50), "color": (0, 0, 0),
            "width": 1.0, "items": [("l", core.fitz.Point(x, 40), core.fitz.Point(x + 10, 50))]}


def mixed_page():
    """Every outcome on one sheet, in drawing order."""
    return [
        stroke(0, 150),
        ring_clip(0), fill(1, (0, 0, 20, 10)),                                  # plain compound fill
        ring_clip(30), wedge_clip(30, level=1), fill(2, (30, 0, 50, 10), level=2),  # core cannot prove it
        ring_clip(60), fill(3, (60, 0, 70, 10)),                                # cut exactly: info
        ring_clip(UNBUILDABLE_X), fill(4, (UNBUILDABLE_X, 0, UNBUILDABLE_X + 20, 10)),  # host cannot build it
        rect_clip((120, 0, 140, 10)), fill(5, (110, 0, 150, 10)),               # rectangle overlap: info
        rect_clip((170, 60, 180, 70)), fill(6, (0, 0, 20, 10)),                 # never visible: info
        stroke(7, 165),
    ]


# (a) the core left one fill out: everything else on the page still arrives

def test_an_unresolvable_clipped_fill_costs_that_fill_and_the_page_still_imports(host):
    document, opts, group = import_page([
        stroke(0, 150),
        ring_clip(30), wedge_clip(30, level=1), fill(2, (30, 0, 50, 10), level=2),
        ring_clip(0), fill(1, (0, 0, 20, 10)),
        stroke(7, 165),
    ])
    assert group is not None and group.Name.startswith("PDF_Page_1")
    assert len(document.named("ClippedFill")) == 1          # the provable mask
    (batch,) = document.named("Batch")                      # both strokes
    assert len(batch.Shape[1]) == 2
    block = opts._report_extra["clip_fill_delivery"]
    assert (block["dropped"], block["approximated"], block["resolved_exactly"]) == (1, 0, 0)
    (issue,) = block["issues"]
    assert (issue["page"], issue["seqno"], issue["reason"], issue["action"]) == (1, 2, "nested", "dropped-unsupported")
    assert "stage" not in issue                             # recorded by the core, not by this host


# (b) this host's own builder fails for one mask: that mask is dropped, the rest is built

def test_a_compound_fill_the_host_cannot_build_is_dropped_and_the_page_continues(host):
    document, opts, _group = import_page(mixed_page())
    # Three masks reach the builder (seqno 1, 3, 4); the unbuildable one is left out.
    assert len(document.named("ClippedFill")) == 2
    # One batch per paint style: the two strokes, and the rectangle cut to its clip.
    assert sorted(len(batch.Shape[1]) for batch in document.named("Batch_")) == [1, 2]
    block = opts._report_extra["clip_fill_delivery"]
    assert block == {
        "resolved_exactly": 2,
        "dropped_invisible": 1,
        "approximated": 0,
        "dropped": 2,
        "by_action": {"dropped-unsupported": 2, "polygon-rect": 1, "rect-intersection": 1, "dropped-invisible": 1},
        "pages_with_warnings": [1],
        "issues": block["issues"],
        "issues_truncated": False,
    }
    core_drop, host_drop = block["issues"]
    assert (core_drop["seqno"], core_drop["reason"]) == (2, "nested")
    assert host_drop == {
        "seqno": 4, "reason": "host-build-error", "action": "dropped-unsupported", "exact": False,
        "severity": "warning", "dropped": True, "detail": "RuntimeError: face maker found no valid wires",
        "paint_rect": [100.0, 0.0, 120.0, 10.0], "fill": [0.2, 0.4, 0.6], "fill_opacity": 1.0,
        "stage": "host-build", "page": 1,
    }
    json.dumps(block)                                       # the report writer must be able to store it
    assert host["warning"] == []                            # never one console line per fill


def test_a_mask_that_fails_after_its_object_exists_leaves_nothing_half_built(host, monkeypatch):
    def style(obj, *_args, **_kwargs):
        if obj.Name.startswith("ClippedFill"):
            raise ValueError("view provider rejected the shape")

    monkeypatch.setattr(core, "_apply_style", style)
    document, opts, group = import_page([ring_clip(0), fill(1, (0, 0, 20, 10)), stroke(2, 150)])
    assert document.named("ClippedFill") == []
    assert [name for name in document.removed if name.startswith("ClippedFill")]
    assert all(not child.Name.startswith("ClippedFill") for child in group.Group)
    assert len(document.named("Batch")) == 1
    (issue,) = opts._report_extra["clip_fill_delivery"]["issues"]
    assert issue["stage"] == "host-build" and issue["detail"].startswith("ValueError: view provider")


def test_a_frame_whose_bounds_are_the_sheet_is_built_and_a_plain_background_is_still_skipped(host):
    sheet = (0, 0, 200, 100)
    frame = {"type": "clip", "level": 0, "scissor": core.fitz.Rect(sheet), "even_odd": True, "items": [
        ("re", core.fitz.Rect(sheet), 1), ("re", core.fitz.Rect(20, 20, 180, 80), 1)]}
    document, opts, _group = import_page([fill(0, sheet, level=0), frame, fill(1, sheet), stroke(7, 165)])
    # The mask's bounds are its clip's, the whole sheet; its ink is a 20 pt band.
    (mask,) = document.named("ClippedFill")
    assert mask.PDFClipFillGroupId == "clip-fill:1"
    (batch,) = document.named("Batch")                      # the stroke; the sheet-sized background is not drawn
    assert len(batch.Shape[1]) == 1
    assert "clip_fill_delivery" not in opts._report_extra   # covered exactly and built: nothing to report


def test_a_fill_the_core_delivered_and_this_host_then_dropped_is_counted_once(host, monkeypatch, tmp_path):
    def unbuildable(*_args):
        raise RuntimeError("face maker found no valid wires")

    monkeypatch.setattr(core, "_compound_clip_fill_shape", unbuildable)
    _document, opts, _group = import_page([
        stroke(0, 150),
        curved_clip(), fill(8, (0, 20, 15, 30)),                                # the core flattened it: warning
        ring_clip(60), fill(3, (60, 0, 70, 10)),                                # the core cut it exactly: info
    ])
    block = opts._report_extra["clip_fill_delivery"]
    assert (block["resolved_exactly"], block["approximated"], block["dropped"]) == (0, 0, 2)
    assert block["by_action"] == {"dropped-unsupported": 2}
    assert [(issue["seqno"], issue["stage"], issue["core_action"]) for issue in block["issues"]] == [
        (8, "host-build", "polygon-rect-flattened"), (3, "host-build", "polygon-rect")]
    assert written_report(tmp_path, opts)["result"]["warnings"] == 2     # two fills, two warnings
    line = core._clip_fill_warning_line(opts)
    assert "2 clipped fill(s) could not be resolved and were left out (drawing order 8, 3)" in line
    assert "flattened" not in line


class Unprintable(Exception):
    def __str__(self):
        raise RuntimeError("cannot describe myself")


def test_recording_a_failed_mask_cannot_itself_fail_the_page(host, monkeypatch):
    def unbuildable(*_args):
        raise Unprintable()

    monkeypatch.setattr(core, "_compound_clip_fill_shape", unbuildable)
    document, opts, _group = import_page([ring_clip(0), fill(1, (0, 0, 20, 10)), stroke(2, 150)])
    assert len(document.named("Batch")) == 1                # the rest of the page
    (issue,) = opts._report_extra["clip_fill_delivery"]["issues"]
    assert (issue["seqno"], issue["stage"], issue["detail"]) == (1, "host-build", "Unprintable")
    # Nor may numbers float() refuses: OverflowError is neither a TypeError nor a ValueError.
    huge = {"seqno": 5, "rect": (0, 0, 10 ** 400, 10), "fill": (10 ** 400, 0, 0), "fill_opacity": 10 ** 400}
    issue = core._host_clip_fill_issue(huge, Unprintable())
    assert (issue["paint_rect"], issue["fill"], issue["fill_opacity"]) == ([], None, 1.0)


def test_a_number_json_cannot_hold_never_costs_the_import_report(host, tmp_path):
    nan = float("nan")
    drop = {"seqno": 9, "reason": "nested", "action": "dropped-unsupported", "exact": False, "severity": "warning",
            "dropped": True, "detail": "", "paint_rect": [0, 0, 1, 1], "fill": (nan, 0.0, 0.5),
            "fill_opacity": float("inf")}
    opts = options()
    core._record_clip_fill_issues(opts, 1, [drop])
    (issue,) = opts._report_extra["clip_fill_delivery"]["issues"]
    assert (issue["fill"], issue["fill_opacity"]) == ([None, 0.0, 0.5], None)
    assert written_report(tmp_path, opts)["result"]["warnings"] == 1


# (d) cancelling is not a build failure

@pytest.mark.parametrize("signal", [core.ImportCancelled("PDF import cancelled by user"), KeyboardInterrupt()])
def test_cancellation_inside_a_clipped_fill_build_still_propagates(host, monkeypatch, signal):
    def cancelled(*_args):
        raise signal

    monkeypatch.setattr(core, "_compound_clip_fill_shape", cancelled)
    with pytest.raises(type(signal)):
        import_page([ring_clip(0), fill(1, (0, 0, 20, 10))])


# (c) the report block, the warnings count and the one operator line

def written_report(tmp_path, opts):
    report_path = tmp_path / "import_report.json"
    core.write_import_report(
        pdf_path=str(tmp_path / "D042.pdf"), output_path=str(report_path), opts=opts,
        pages_imported=1, total_pages=1, elapsed_ms=1.0,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def test_the_import_report_carries_the_delivery_block_and_counts_the_warnings(host, tmp_path):
    _document, opts, _group = import_page(mixed_page() + [curved_clip(), fill(8, (0, 20, 15, 30))])
    report = written_report(tmp_path, opts)
    block = report["extra"]["clip_fill_delivery"]
    assert (block["resolved_exactly"], block["dropped_invisible"], block["approximated"], block["dropped"]) == (2, 1, 1, 2)
    assert [(issue["page"], issue["seqno"], issue["action"]) for issue in block["issues"]] == [
        (1, 2, "dropped-unsupported"), (1, 8, "polygon-rect-flattened"), (1, 4, "dropped-unsupported")]
    assert all(issue["severity"] == "warning" for issue in block["issues"])
    assert report["result"]["warnings"] == 3                # two dropped and one approximated
    assert "warnings_present" in report["extra"]["diagnostics"]["signals"]
    line = core._clip_fill_warning_line(opts)
    assert "2 clipped fill(s) could not be resolved and were left out" in line
    assert "1 clipped fill(s) are approximate (flattened curves or crossing contours)" in line
    assert "page(s) 1." in line


def test_fills_that_resolved_exactly_are_counted_and_never_warned_about(host, tmp_path):
    rows = [stroke(0, 150)]
    for index in range(40):                                 # corpus sheets carry hundreds of these
        rows += [rect_clip((0, 0, 10, 10)), fill(index + 1, (5, 0, 30, 10))]
    _document, opts, _group = import_page(rows)
    report = written_report(tmp_path, opts)
    block = report["extra"]["clip_fill_delivery"]
    assert (block["resolved_exactly"], block["issues"], block["pages_with_warnings"]) == (40, [], [])
    assert report["result"]["warnings"] == 0
    assert core._clip_fill_warning_line(opts) == ""
    assert host["warning"] == []


def test_a_report_without_clipped_fills_still_states_the_empty_block(tmp_path):
    report = written_report(tmp_path, core.ImportOptions(import_text=False))
    assert report["extra"]["clip_fill_delivery"] == core._new_clip_fill_delivery()
    assert report["result"]["warnings"] == 0


def test_only_the_first_two_hundred_warning_records_are_listed_but_all_are_counted():
    opts = core.ImportOptions()
    drop = {"seqno": 9, "action": "dropped-unsupported", "severity": "warning", "dropped": True}
    core._record_clip_fill_issues(opts, 3, [dict(drop, seqno=index) for index in range(core.CLIP_FILL_REPORT_ISSUE_LIMIT + 5)])
    block = opts._report_extra["clip_fill_delivery"]
    assert (block["dropped"], len(block["issues"]), block["issues_truncated"]) == (205, 200, True)
    assert "205 fills are affected in all" in core._clip_fill_warning_line(opts)


def test_a_page_delivered_as_raster_reports_nothing_about_vector_fills(host, monkeypatch):
    rastered = []
    monkeypatch.setattr(core, "_import_page_as_raster",
                        lambda *args, **kwargs: rastered.append(args[2]) or {"created_entity_ids": ["raster"]})
    # Fewer than five paint rows and no text: auto mode delivers this page as an image.
    opts = options(import_mode="auto")
    _document, opts, _group = import_page(
        [ring_clip(30), wedge_clip(30, level=1), fill(2, (30, 0, 50, 10), level=2), stroke(0, 150)], opts)
    assert rastered == [1]
    assert "clip_fill_delivery" not in opts._report_extra
    assert core._clip_fill_warning_line(opts) == ""


def test_the_planning_pass_survives_the_fill_and_records_nothing(host, monkeypatch):
    from pdfcadcore import fitz_loader

    rows = [ring_clip(30), wedge_clip(30, level=1), fill(2, (30, 0, 50, 10), level=2), stroke(0, 150)]
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: Pdf(Page(rows)))
    opts = core.ImportOptions(pages=[1], import_text=False)
    plan = core.estimate_import_work("D042.pdf", opts)
    assert plan["pages"][0]["drawing_operations"] == 1     # the stroke; the unprovable fill is left out
    assert not getattr(opts, "_report_extra", None)
    assert host["warning"] == []


def test_the_single_page_entry_point_prints_the_line_too(host, monkeypatch):
    from pdfcadcore import fitz_loader

    rows = [ring_clip(30), wedge_clip(30, level=1), fill(2, (30, 0, 50, 10), level=2), stroke(0, 150)]
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: Pdf(Page(rows)))
    monkeypatch.setattr(core, "_ensure_doc", Document)
    monkeypatch.setattr(core, "_pdf_file_sha256", lambda _path: "a" * 64)
    core.import_pdf_page("D042.pdf", 1, options(), autofit=False)
    (line,) = [text for text in host["warning"] if "lipped fill" in text]
    assert "1 clipped fill(s) could not be resolved" in line and "page(s) 1." in line


# Multi-page runs accumulate; a cancelled page takes its record with it.

def test_pages_accumulate_one_warning_line_is_printed_and_a_cancelled_page_is_forgotten(host, monkeypatch, tmp_path):
    from pdfcadcore import fitz_loader

    drop = {"reason": "nested", "action": "dropped-unsupported", "exact": False, "severity": "warning",
            "dropped": True, "detail": "2 different non-rectangular clips are active", "paint_rect": [0, 0, 1, 1],
            "fill": [0, 0, 0], "fill_opacity": 1.0}
    exact = dict(drop, action="rect-intersection", exact=True, severity="info", dropped=False)

    cancel_on = {3}

    def page_importer(_pdf, _path, page_number, page_opts, doc):
        core._record_clip_fill_issues(page_opts, page_number, [dict(drop, seqno=10 * page_number), exact])
        if page_number in cancel_on:
            raise core.ImportCancelled("PDF import cancelled by user")
        return doc.addObject("App::DocumentObjectGroup", f"PDF_Page_{page_number}"), None

    reports = []
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: Pdf(Page([]), Page([]), Page([])))
    document = Document()
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_pdf_file_sha256", lambda _path: "a" * 64)
    monkeypatch.setattr(core, "_import_pdf_page_inner", page_importer)
    monkeypatch.setattr(core, "_autofit_import_view", lambda *_args: None)
    monkeypatch.setattr(core, "write_import_report", lambda **kwargs: reports.append(
        json.loads(json.dumps(kwargs["opts"]._report_extra))))
    opts = core.ImportOptions(pages=[1, 2, 3], import_text=False, model3d_mode="off", verbose=False,
                              import_report_path=str(tmp_path / "report.json"))

    assert core.import_pdf("D042.pdf", opts) is False       # page 3 was cancelled
    block = reports[-1]["clip_fill_delivery"]
    assert (block["dropped"], block["resolved_exactly"], block["pages_with_warnings"]) == (2, 2, [1, 2])
    assert [(issue["page"], issue["seqno"]) for issue in block["issues"]] == [(1, 10), (2, 20)]
    lines = [text for text in host["warning"] if "lipped fill" in text]
    assert len(lines) == 1
    assert "2 clipped fill(s) could not be resolved" in lines[0] and "page(s) 1, 2." in lines[0]

    # The resumed run imports only page 3. Like every other result in this host's
    # report it covers this invocation, and the report says which pages it leaves out.
    cancel_on.clear()
    session_host = next(obj for obj in document.Objects if obj.Name.startswith("PDF_Import_Session"))
    resumed = core.ImportOptions(pages=[1, 2, 3], import_text=False, model3d_mode="off", verbose=False,
                                 import_report_path=str(tmp_path / "report.json"),
                                 resume_session_name=session_host.Name)
    monkeypatch.setattr(core, "_create_semantic_model3d_members", lambda *_args: None)
    monkeypatch.setattr(core, "_finalize_import_recompute", lambda *_args, **_kwargs: None)
    assert core.import_pdf("D042.pdf", resumed) is True
    block = reports[-1]["clip_fill_delivery"]
    assert (block["dropped"], block["pages_with_warnings"]) == (1, [3])
    assert [(issue["page"], issue["seqno"]) for issue in block["issues"]] == [(3, 30)]
    assert reports[-1]["representation_contract_scope"]["previously_certified_pages_excluded"] == [1, 2]
    assert len([text for text in host["warning"] if "lipped fill" in text]) == 2   # one per import
