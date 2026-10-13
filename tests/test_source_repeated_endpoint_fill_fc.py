"""Source-proved fill closure/nonpainting semantics; no native OCC claim."""
import copy
import json
from types import SimpleNamespace

import pymupdf
import pytest

import test_clip_fill_degrade_fc as fixture
import test_source_fill_boundary_fc as existing

core = fixture.core


def source(points, *, kind="f", close=False, rule=False, seq=73):
    return dict(type=kind, fill=(.2, .3, .4), fill_opacity=.75,
                color=(.6, .1, .2) if kind == "fs" else None,
                width=2.75, dashes="[4 2] 0", stroke_opacity=.5,
                closePath=close, even_odd=rule, seqno=seq,
                items=[("l", core.fitz.Point(*a), core.fitz.Point(*b))
                       for a, b in zip(points, points[1:], strict=False)])


def triangle(**fields):
    return source([(10., 10.), (35., 10.), (20., 28.), (10., 10.)], **fields)


class Face:
    def __init__(self, wire):
        assert wire.isClosed() and wire.isValid()
        self.edges = wire.edges
        self.Area = 225.
        self.Faces, self.Solids = [], []

    def isNull(self): return False
    def isValid(self): return True


class Edge(tuple):
    def __new__(cls, a, b):
        return tuple.__new__(cls, ("l", [a, b]))

    def isNull(self): return False
    def isValid(self): return True


def page_host(monkeypatch):
    fixture.host.__wrapped__(monkeypatch)
    monkeypatch.setattr(core.Part, "LineSegment", lambda a, b: SimpleNamespace(toShape=lambda: Edge(a, b)))
    monkeypatch.setattr(core.Part, "Face", Face, raising=False)
    observed = []
    monkeypatch.setattr(core, "_apply_style", lambda obj, *style: observed.append((obj, style)))
    return observed


@pytest.mark.parametrize("kind", ["f", "fs"])
@pytest.mark.parametrize("close", [False, None])
@pytest.mark.parametrize("rule", [False, True])
def test_page_retains_exact_closed_fill_without_changing_open_stroke(monkeypatch, kind, close, rule):
    styles = page_host(monkeypatch)
    path = triangle(kind=kind, close=close, rule=rule)
    before = copy.deepcopy(path)
    opts = fixture.options(hatch_to_faces=True, compound_batch_size=0, min_seg_len=1000.)
    document, opts, _ = fixture.import_page([path], opts)
    assert path == before
    faces = document.named("Face")
    assert len(faces) == 1
    assert opts._report_extra.get("geometry_items_degraded") is None
    proof = json.loads(faces[0].PDFSourceFillBoundaryJSON)
    assert proof["policy"] == "retain_exact_closed_pdf_fill"
    assert proof["source_pdf_sha256"] == "a" * 64
    assert proof["source_stroke_close_path"] is close
    assert proof["closure_origin"] == "explicit_repeated_source_endpoint"
    assert proof["retained_item_range"] == [0, 3]
    assert proof["fill_rule"] == ("even_odd" if rule else "nonzero")
    face_style = next(style for obj, style in styles if obj is faces[0])
    assert face_style[0] is None and face_style[2:4] == (None, None)
    wires = document.named("Wire")
    assert len(wires) == (1 if kind == "fs" else 0)
    if kind == "fs":
        stroke_proof = json.loads(wires[0].PDFSourceFillBoundaryJSON)
        assert stroke_proof["policy"] == "retain_open_pdf_stroke"
        assert stroke_proof["source_stroke_close_path"] is close
        stroke_style = next(style for obj, style in styles if obj is wires[0])
        assert stroke_style[1] is None and stroke_style[2:4] == (2.75, [4., 2.])
        def coordinates(shape):
            return [[(point.x, point.y, point.z) for point in edge[1]] for edge in shape.edges]
        assert coordinates(faces[0].Shape) == coordinates(wires[0].Shape)


@pytest.mark.parametrize("transform", [lambda p: p, lambda p: (-3*p[1]+.125, 2*p[0]-.5)])
def test_closed_source_proof_retains_cubic_controls_and_original_objects(transform):
    path = triangle()
    path["items"] = [("c", (0., 0.), (2., 0.), (2., 3.), (0., 3.)),
                     ("c", (0., 3.), (-2., 3.), (-2., 0.), (0., 0.))]
    path["items"] = [tuple([command[0]] + [transform(p) for p in command[1:]]) for command in path["items"]]
    before = copy.deepcopy(path)
    items, proof = core._source_closed_fill_boundary(path)
    assert proof is not None and path == before
    assert items is path["items"] and all(a is b for a, b in zip(items, path["items"], strict=True))


@pytest.mark.parametrize("change", ["gap", "near_closure", "self_cross", "second_loop", "nonfinite",
                                   "unsupported", "no_fill", "stroke_only", "unknown_rule", "unknown_close"])
def test_closed_boundary_refuses_unproved_source(change):
    path = triangle()
    if change == "gap": path["items"][1] = ("l", (35.+1e-12, 10.), (20., 28.))
    elif change == "near_closure": path["items"][-1] = ("l", (20., 28.), (10.+1e-12, 10.))
    elif change == "self_cross": path = source([(0., 0.), (8., 8.), (8., 0.), (0., 8.), (0., 0.)])
    elif change == "second_loop": path["items"] += [("l", (10., 10.), (0., 0.)), ("l", (0., 0.), (10., 10.))]
    elif change == "nonfinite": path["items"][1] = ("l", (35., 10.), (float("inf"), 28.))
    elif change == "unsupported": path["items"][1] = ("q", (35., 10.), (20., 28.))
    elif change == "no_fill": path["fill"] = None
    elif change == "stroke_only": path["type"] = "s"
    elif change == "unknown_rule": path["even_odd"] = 1
    elif change == "unknown_close": path["closePath"] = 0
    result, proof = core._source_closed_fill_boundary(path)
    assert result is path["items"] and proof is None


@pytest.mark.parametrize("points", [[(10., 10.), (40., 10.)],
                                   [(10., 10.), (40., 10.), (20., 10.)],
                                   [(10., 10.), (40., 25.), (20., 15.)],
                                   [(10., 10.), (10., 10.)]])
@pytest.mark.parametrize("rule", [False, True])
def test_fill_only_collinear_source_is_omitted_with_bound_proof(monkeypatch, points, rule):
    page_host(monkeypatch)
    path = source(points, rule=rule)
    before = copy.deepcopy(path)
    def refuse_face(_wire): raise AssertionError("A proven zero-ink fill must never request a face")
    monkeypatch.setattr(core.Part, "Face", refuse_face)
    document, opts, _ = fixture.import_page([path], fixture.options(hatch_to_faces=True, compound_batch_size=0))
    assert not [obj for obj in document.Objects if obj.TypeId == "Part::Feature"]
    assert path == before and opts._report_extra.get("geometry_items_degraded") is None
    proof, = opts._report_extra["source_zero_ink_fills"]
    assert proof["source_pdf_sha256"] == "a" * 64 and proof["source_page"] == 1
    assert proof["source_paint_order"] == 73 and len(proof["source_commands_sha256"]) == 64
    assert proof["visible_fill_ink_expected"] is False and proof["fill_omitted"] is True
    assert proof["source_stroke_present"] is False


def test_collinear_fill_keeps_genuine_stroke_and_source_closing_semantics(monkeypatch):
    styles = page_host(monkeypatch)
    path = source([(10., 10.), (40., 10.), (20., 10.)], kind="fs")
    before = copy.deepcopy(path)
    document, opts, _ = fixture.import_page([path], fixture.options(hatch_to_faces=True, compound_batch_size=0))
    assert not document.named("Face") and len(document.named("Wire")) == 1, (
        [obj.Name for obj in document.Objects], opts._report_extra)
    assert path == before and opts._report_extra.get("geometry_items_degraded") is None
    proof, = opts._report_extra["source_zero_ink_fills"]
    assert proof["source_stroke_present"] is True and proof["source_stroke_close_path"] is False
    style = next(style for obj, style in styles if obj is document.named("Wire")[0])
    assert style[1] is None and style[2:4] == (2.75, [4., 2.])


@pytest.mark.parametrize("change", ["near_collinear", "self_cross_zero_area", "curve", "gap", "nonfinite", "unknown_rule"])
def test_zero_ink_requires_exact_original_line_collinearity(change):
    path = source([(0., 0.), (8., 0.), (3., 0.)])
    if change == "near_collinear": path["items"][1] = ("l", (8., 0.), (3., 1e-20))
    elif change == "self_cross_zero_area": path = source([(0., 0.), (8., 8.), (8., 0.), (0., 8.), (0., 0.)])
    elif change == "curve": path["items"][1] = ("c", (8., 0.), (6., 0.), (4., 0.), (3., 0.))
    elif change == "gap": path["items"][1] = ("l", (8.+1e-12, 0.), (3., 0.))
    elif change == "nonfinite": path["items"][1] = ("l", (8., 0.), (float("nan"), 0.))
    elif change == "unknown_rule": path["even_odd"] = 0
    assert core._source_collinear_zero_ink_fill(path) is None


def test_source_proof_does_not_certify_a_refused_native_face(monkeypatch):
    page_host(monkeypatch)
    monkeypatch.setattr(core.Part, "Face", lambda wire: existing.Shape(valid=False))
    _, opts, _ = fixture.import_page([triangle()], fixture.options(hatch_to_faces=True, compound_batch_size=0))
    assert opts._report_extra["geometry_items_degraded"]["total"] == 1
    assert "invalid" in opts._report_extra["geometry_items_degraded"]["items"][0]["reason"]


def test_unavailable_legacy_source_proof_api_is_not_a_page_failure(monkeypatch):
    import PDFSourceFill
    def unsupported(*args): raise TypeError("legacy zip has no strict parameter")
    monkeypatch.setattr(PDFSourceFill, "_prove_simple", unsupported)
    path = triangle()
    items, proof = core._source_closed_fill_boundary(path)
    assert items is path["items"] and proof is None


@pytest.mark.parametrize("kind", ["f", "fs"])
def test_refused_new_closed_fill_edge_steps_down_only_its_item(monkeypatch, kind):
    page_host(monkeypatch)
    original = core.Part.LineSegment
    def line(a, b):
        if a.x == 10. and b.x == 35.:
            raise RuntimeError("neutral native edge refusal")
        return original(a, b)
    monkeypatch.setattr(core.Part, "LineSegment", line)
    bad = triangle(kind=kind)
    good = source([(60., 10.), (85., 10.), (70., 28.), (60., 10.)], seq=74)
    document, opts, _ = fixture.import_page([bad, good], fixture.options(hatch_to_faces=True, compound_batch_size=0))
    assert len(document.named("Face")) == 1
    degraded = opts._report_extra["geometry_items_degraded"]
    assert degraded["total"] == 1 and degraded["items"][0]["source_paint_order"] == 73
    assert "Source fill" in degraded["items"][0]["reason"]


@pytest.mark.parametrize("stream", [b"20 20 m 90 20 l f", b"20 20 m 90 20 l 40 20 l f"])
def test_independent_pdf_renderer_confirms_collinear_fill_has_no_pixels(stream):
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=120, height=100)
        xref = pdf.get_new_xref()
        pdf.update_object(xref, "<<>>")
        pdf.update_stream(xref, stream)
        page.set_contents(xref)
        pixels = page.get_pixmap(matrix=pymupdf.Matrix(4, 4), alpha=True).samples
    assert not any(pixels[3::4])


def test_independent_renderer_retains_visible_fill_and_original_open_stroke_caps():
    path = b"20 20 m 90 20 l 60 70 l 20 20 l "
    state = b"0.2 0.3 0.4 rg 0.8 0.1 0.2 RG 8 w 2 J 1 j "
    def render(stream):
        with pymupdf.open() as pdf:
            page = pdf.new_page(width=120, height=100)
            xref = pdf.get_new_xref()
            pdf.update_object(xref, "<<>>")
            pdf.update_stream(xref, stream)
            page.set_contents(xref)
            return page.get_pixmap(matrix=pymupdf.Matrix(4, 4), alpha=True).samples
    fill_only = render(state + path + b"f")
    assert any(fill_only[3::4])
    assert fill_only == render(state + path + b"h f")
    original_open_stroke = render(state + path + b"B")
    separate_original_stroke = render(state + path + b"f " + path + b"S")
    assert original_open_stroke == separate_original_stroke
    assert original_open_stroke != render(state + path + b"h B")
