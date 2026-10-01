"""Exact filled source boundaries remain valid native faces without retraced seams."""
import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import pytest

REPO = Path(__file__).resolve().parents[1]
for folder in (REPO, REPO / "PDFVectorImporter", REPO / "PDFVectorImporter/src"):
    sys.path.insert(0, str(folder))
import PDFImporterCore as core  # noqa: E402

def path(boundary=None):
    if boundary is None:
        boundary = [("l", (2., 0.), (2., 2.)), ("l", (2., 2.), (0., 2.)),
                    ("l", (0., 2.), (0., 0.)), ("l", (0., 0.), (2., 0.))]
    return dict(type="f", even_odd=True, fill=(.2, .3, .4), color=None,
                fill_opacity=.5, seqno=7,
                items=[("l", (1., 1.), (2., 0.))] + boundary
                      + [("l", (2., 0.), (1., 1.))])


class SourceFillBridgeTests(unittest.TestCase):
    def test_exact_cancel_preserves_existing_boundary_and_original_source(self):
        source = path()
        before = copy.deepcopy(source)
        boundary, proof = core._source_fill_without_retraced_bridge(source)
        self.assertEqual(source, before)
        self.assertEqual(boundary, source["items"][1:-1])
        self.assertTrue(all(a is b for a, b in zip(boundary, source["items"][1:-1], strict=True)))
        self.assertEqual(proof["retained_item_range"], [1, 5])
        self.assertEqual(proof["source_paint_order"], 7)
        self.assertEqual(proof["closure_origin"], "explicit_reverse_command")
        self.assertEqual(len(proof["source_commands_sha256"]), 64)

    def test_connected_cubic_and_affine_boundary_keep_every_control_point(self):
        source = path([("c", (2., 0.), (4., 0.), (4., 2.), (2., 2.)),
                       ("c", (2., 2.), (0., 2.), (0., 0.), (2., 0.))])
        for transform in (lambda p:p, lambda p:(-3*p[1]+.125, 2*p[0]-.5)):
            candidate = copy.deepcopy(source)
            candidate["items"] = [tuple([it[0]] + [transform(p) for p in it[1:]])
                                  for it in source["items"]]
            boundary, proof = core._source_fill_without_retraced_bridge(candidate)
            self.assertIsNotNone(proof)
            self.assertEqual(boundary, candidate["items"][1:-1])

    def test_stroke_mixed_unknown_rule_and_absent_fill_are_untouched(self):
        for fields in ({"type":"s"}, {"type":"fs"}, {"color":(0.,0.,0.)},
                       {"stroke":(0.,0.,0.)}, {"even_odd":False},
                       {"even_odd":1}, {"fill":None}):
            with self.subTest(fields=fields):
                source=path(); source.update(fields)
                result, proof=core._source_fill_without_retraced_bridge(source)
                self.assertIs(result,source["items"]); self.assertIsNone(proof)

    def test_near_match_gap_genuine_wedge_and_open_boundary_are_untouched(self):
        for index, value in ((-1,("l",(2.,0.),(1.+2**-51,1.))),
                             (-1,("l",(2.,0.),(1.,1.1))),
                             (2,("l",(2.+2**-50,2.),(0.,2.))),
                             (-2,("l",(0.,0.),(2.,.1)))):
            with self.subTest(index=index,value=value):
                source=path(); source["items"][index]=value
                result, proof=core._source_fill_without_retraced_bridge(source)
                self.assertIs(result,source["items"]); self.assertIsNone(proof)

    def test_multiple_loops_and_unsupported_or_nonfinite_commands_are_untouched(self):
        cases=[]
        source=path(); source["items"][1:1]=[("l",(2.,0.),(3.,0.)),("l",(3.,0.),(2.,0.))]; cases.append(source)
        for command in (("m",(2.,0.)), ("q",(2.,0.),(2.,2.)),
                        ("l",(2.,0.),(float("inf"),2.)),
                        ("l",(2.,0.),(float("nan"),2.)),
                        ("l",(2.,0.),(True,2.)),
                        ([],(2.,0.),(2.,2.))):
            source=path(); source["items"][1]=command; cases.append(source)
        for source in cases:
            result, proof=core._source_fill_without_retraced_bridge(source)
            self.assertIs(result,source["items"]); self.assertIsNone(proof)

    def test_independent_svg_fill_pixels_are_identical(self):
        import pymupdf as fitz
        original='M 10 10 L 20 0 C 30 0 30 20 20 20 C 0 20 0 0 20 0 L 10 10 Z'
        boundary='M 20 0 C 30 0 30 20 20 20 C 0 20 0 0 20 0 Z'
        def render(data):
            svg=('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="30" '
                 'viewBox="-5 -5 40 30"><path fill="#345678" fill-rule="evenodd" '
                 'd="'+data+'"/></svg>').encode()
            with fitz.open(stream=svg,filetype='svg') as doc:
                return doc[0].get_pixmap(matrix=fitz.Matrix(20,20),alpha=True).samples
        self.assertEqual(render(original),render(boundary))
        implicit = original.removesuffix(' L 10 10 Z')
        self.assertEqual(render(implicit), render(boundary))
        self.assertNotEqual(render(original),render(boundary.replace('30 20','27 20')))

    def test_implicit_reverse_bridge_preserves_line_and_cubic_boundaries(self):
        boundaries = [None, [("c", (2., 0.), (4., 0.), (4., 2.), (2., 2.)),
                             ("c", (2., 2.), (0., 2.), (0., 0.), (2., 0.))]]
        for boundary in boundaries:
            source = path(boundary)
            source["items"].pop()
            before = copy.deepcopy(source)
            result, proof = core._source_fill_without_retraced_bridge(source)
            self.assertEqual(source, before)
            self.assertEqual(result, source["items"][1:])
            self.assertTrue(all(a is b for a, b in zip(result, source["items"][1:], strict=True)))
            self.assertEqual(proof["closure_origin"], "implicit_pdf_fill_closure")
            self.assertEqual(proof["retained_item_range"], [1, len(source["items"])])
            source["items"][-1] = ("l", (0., 0.), (2., 1e-20))
            self.assertIsNone(core._source_fill_without_retraced_bridge(source)[1])




class Shape:
    def __init__(self, *, valid=True, closed=True, null=False):
        self.valid, self.closed, self.null = valid, closed, null
        self.Vertexes = []

    def isNull(self): return self.null
    def isValid(self): return self.valid
    def isClosed(self): return self.closed


class Object:
    def __init__(self, name, kind):
        self.Name, self.TypeId, self.Label = name, kind, name
        self.Group = []
        self.PropertiesList = []

    def addProperty(self, kind, name, group):
        self.PropertiesList.append(name)

    def addObject(self, child): self.Group.append(child)
    def removeObject(self, child): self.Group.remove(child)


class Document:
    def __init__(self):
        self.Objects = []
        self.aborted = self.committed = False

    def addObject(self, kind, name):
        obj = Object(name, kind)
        self.Objects.append(obj)
        return obj

    def getObject(self, name): return next((o for o in self.Objects if o.Name == name), None)
    def removeObject(self, name): self.Objects = [o for o in self.Objects if o.Name != name]
    def openTransaction(self, name): pass
    def commitTransaction(self): self.committed = True
    def abortTransaction(self): self.aborted = True
    def recompute(self, *args): pass


@pytest.mark.parametrize("invalid", ["face", "wire", "null_face", "open_fill"])
def test_invalid_native_geometry_never_becomes_a_wire_fallback_or_persisted_face(monkeypatch, invalid):
    doc = Document()
    wire = Shape(valid=invalid != "wire", closed=invalid != "open_fill")
    face = Shape(valid=invalid != "face", null=invalid == "null_face")
    monkeypatch.setattr(core, "Part", SimpleNamespace(Wire=lambda edges: wire, Face=lambda w: face))
    with pytest.raises(core.DrawingGeometryFailure):
        core._make_shape_obj(["original-edge"], False, True, doc)
    assert doc.Objects == []


def test_source_boundary_provenance_survives_exact_native_property_readback(monkeypatch):
    obj = Object("Face", "Part::Feature")
    opts = core.ImportOptions()
    opts._pdf_sha256 = "d" * 64
    proof = dict(schema="bcs.freecad.source-fill-boundary/1", source_commands_sha256="e" * 64,
                 retained_item_range=[1, 5], cancelled_bridge=[[1, 1], [2, 0]])
    receipt = core._attach_source_fill_boundary(obj, proof, 2, 100, opts, 2, "unused.pdf")
    assert json.loads(obj.PDFSourceFillBoundaryJSON) == receipt
    assert receipt["source_pdf_sha256"] == "d" * 64
    assert receipt["source_page"] == 2
    assert receipt["boundary_representation"] == "original_lines_and_cubic_bezier_controls"
    assert proof.keys() == {"schema", "source_commands_sha256", "retained_item_range", "cancelled_bridge"}


@pytest.mark.parametrize("report_failure", [False, True])
def test_actual_import_rolls_back_and_writes_failed_geometry_report(monkeypatch, tmp_path, report_failure):
    pdf = tmp_path / "synthetic.pdf"
    with core.fitz.open() as document:
        document.new_page(width=100, height=100)
        document.save(pdf)
    doc = Document()
    existing = doc.addObject("Part::Feature", "Existing")
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(
        GuiUp=False, Version=lambda: ("1", "1", "0"),
        Console=SimpleNamespace(PrintMessage=lambda value: None,
                                PrintWarning=lambda value: None, PrintError=lambda value: None)))
    monkeypatch.setattr(core, "_ensure_doc", lambda: doc)
    monkeypatch.setattr(core, "_err", lambda *args: None)
    failure = core.DrawingGeometryFailure("synthetic invalid face")

    def fail_page(*args, **kwargs):
        doc.addObject("Part::Feature", "Partial_Invalid_Face")
        raise failure

    monkeypatch.setattr(core, "_import_pdf_page_inner", fail_page)
    if report_failure:
        def fail_report(**kwargs): raise OSError("synthetic unwritable report")
        monkeypatch.setattr(core, "_write_terminal_representation_failure_report", fail_report)
    report = tmp_path / "report.json"
    opts = core.ImportOptions(pages=[1], import_text=False, import_mode="vector",
                              import_report_path=str(report), verbose=False)
    with pytest.raises(core.DrawingGeometryFailure) as caught:
        core.import_pdf(str(pdf), opts)
    assert caught.value is failure
    assert doc.aborted and not doc.committed
    assert doc.Objects == [existing]
    assert opts.import_status == "failed"
    if not report_failure:
        result = json.loads(report.read_text())
        assert result["extra"]["result_status"] == "failed"
        assert result["extra"]["terminal_failure"]["type"] == "DrawingGeometryFailure"
        assert result["extra"]["import_contract_ready"]["ready"] is False
        assert result["extra"]["import_contract_ready"]["checks"]["result_succeeded"] is False


def test_exact_retained_line_ignores_segment_threshold_and_cubic_keeps_controls(monkeypatch):
    calls = []
    def line(a, b):
        calls.append(("l", a, b))
        return SimpleNamespace(toShape=lambda: Shape())
    class Curve:
        def setPoles(self, points): calls.append(("c", *points))
        def toShape(self): return Shape()
    monkeypatch.setattr(core, "Part", SimpleNamespace(LineSegment=line, BezierCurve=Curve))
    monkeypatch.setattr(core, "_to_fc", lambda point, *args: point)
    opts = core.ImportOptions(min_seg_len=1000.)
    items = [("l", (0., 0.), (1e-10, 0.)),
             ("c", (1e-10, 0.), (3., 7.), (4., -2.), (0., 0.))]
    result = core._source_fill_boundary_edges(items, 100, opts, 1)
    assert len(result) == 2
    assert calls == items


def test_native_edge_invalidity_is_terminal_before_face_creation(monkeypatch):
    monkeypatch.setattr(core, "Part", SimpleNamespace(
        LineSegment=lambda *args: SimpleNamespace(toShape=lambda: Shape(valid=False))))
    monkeypatch.setattr(core, "_to_fc", lambda point, *args: point)
    with pytest.raises(core.DrawingGeometryFailure, match="source fill edge"):
        core._source_fill_boundary_edges([("l", (0., 0.), (1., 0.))], 100, core.ImportOptions(), 1)


def test_native_constructor_exception_is_typed_and_retains_cause(monkeypatch):
    error = RuntimeError("native constructor rejected source edge")
    def line(*args): raise error
    monkeypatch.setattr(core, "Part", SimpleNamespace(LineSegment=line))
    monkeypatch.setattr(core, "_to_fc", lambda point, *args: point)
    with pytest.raises(core.DrawingGeometryFailure) as caught:
        core._source_fill_boundary_edges([("l", (0., 0.), (1., 0.))], 100, core.ImportOptions(), 1)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("close_path,hatch_faces,make_faces", [(False, True, False), (True, False, True)])
@pytest.mark.parametrize("implicit", [False, True])
def test_page_builder_delivers_face_without_invented_bridge_or_closure(
        monkeypatch, close_path, hatch_faces, make_faces, implicit):
    sys.path.insert(0, str(REPO / "tests"))
    import test_clip_fill_degrade_fc as host_fixture
    monkeypatch.setattr(host_fixture, "core", core)
    host_fixture.host.__wrapped__(monkeypatch)
    native_calls = []
    class Wire(Shape):
        def __init__(self, edges):
            super().__init__()
            self.edges = edges
    class Curve:
        def setPoles(self, points): native_calls.append(("c", points))
        def toShape(self): return Shape()
    def line(a, b):
        native_calls.append(("l", a, b))
        return SimpleNamespace(toShape=lambda: Shape())
    monkeypatch.setattr(core, "Part", SimpleNamespace(
        Wire=Wire, Face=lambda wire: Shape(), BezierCurve=Curve, LineSegment=line))
    source = dict(type="f", even_odd=True, fill=(0., 0., 0.), color=None,
                  closePath=close_path, fill_opacity=1., seqno=7,
                  items=[("l", (10., 10.), (20., 0.)),
                         ("c", (20., 0.), (30., 0.), (30., 20.), (20., 20.)),
                         ("c", (20., 20.), (0., 20.), (0., 0.), (20., 0.)),
                         ("l", (20., 0.), (10., 10.))])
    if implicit:
        source["items"].pop()
    opts = host_fixture.options(hatch_to_faces=hatch_faces, make_faces=make_faces,
                                compound_batch_size=0, detect_arcs=True, min_seg_len=1000.)
    observed = []
    original = core._make_shape_obj
    def make_shape(edges, closed, make_face, fc_doc):
        observed.append((closed, make_face))
        return original(edges, closed, make_face, fc_doc)
    monkeypatch.setattr(core, "_make_shape_obj", make_shape)
    document, _, _ = host_fixture.import_page([source], opts)
    assert observed == [(False, True)]
    assert [record[0] for record in native_calls] == ["c", "c"]
    faces = document.named("Face")
    assert len(faces) == 1
    receipt = json.loads(faces[0].PDFSourceFillBoundaryJSON)
    assert receipt["retained_item_range"] == [1, 3]
    assert len(source["items"]) == (3 if implicit else 4)


def open_triangle(kind="f"):
    return dict(type=kind, even_odd=False, fill=(.2, .4, .6), fill_opacity=1.,
                color=(.9, .1, .2) if kind == "fs" else None,
                width=2.25, dashes="[3 2] 0", closePath=False, seqno=8,
                items=[("l", (10., 10.), (30., 10.)),
                       ("l", (30., 10.), (20., 25.))])


def test_ordinary_implicit_closure_is_source_bound_and_never_mutates_commands():
    source = open_triangle()
    before = copy.deepcopy(source)
    items, proof = core._source_implicit_fill_boundary(source)
    assert source == before
    assert all(a is b for a, b in zip(items[:-1], source["items"], strict=True))
    assert items[-1] == ("l", (20., 25.), (10., 10.))
    assert proof["fill_closing_segment"] == [[20., 25.], [10., 10.]]
    assert proof["policy"] == "close_connected_pdf_fill"
    assert proof["retained_item_range"] == [0, 2]
    assert proof["fill_rule"] == "nonzero"


@pytest.mark.parametrize("change", ["disconnected", "multiple_loops", "closed", "unsupported",
                                    "nonfinite", "stroke_only", "no_fill", "explicit_close"])
def test_ordinary_closure_rejects_unproved_connections(change):
    source = open_triangle()
    if change == "disconnected": source["items"][1] = ("l", (30.000000000000004, 10.), (20., 25.))
    elif change == "multiple_loops": source["items"] += [("l", (20., 25.), (30., 10.)), ("l", (30., 10.), (50., 30.))]
    elif change == "closed": source["items"] += [("l", (20., 25.), (10., 10.))]
    elif change == "unsupported": source["items"][1] = ("q", (30., 10.), (20., 25.))
    elif change == "nonfinite": source["items"][1] = ("l", (30., 10.), (float("inf"), 25.))
    elif change == "stroke_only": source["type"] = "s"
    elif change == "no_fill": source["fill"] = None
    elif change == "explicit_close": source["closePath"] = True
    items, proof = core._source_implicit_fill_boundary(source)
    assert items is source["items"] and proof is None


@pytest.mark.parametrize("kind", ["f", "fs"])
@pytest.mark.parametrize("hatch_faces", [False, True])
def test_actual_page_implicit_fill_and_open_stroke_preserve_separate_boundaries(
        monkeypatch, kind, hatch_faces):
    sys.path.insert(0, str(REPO / "tests"))
    import test_clip_fill_degrade_fc as fixture
    monkeypatch.setattr(fixture, "core", core)
    real_style = core._apply_style
    fixture.host.__wrapped__(monkeypatch)
    monkeypatch.setattr(core, "_apply_style", real_style)

    class Edge(Shape):
        def __init__(self, points):
            super().__init__(closed=False)
            self.points = points
    class Wire(Shape):
        def __init__(self, edges):
            def xy(point): return point.x, point.y
            super().__init__(closed=xy(edges[0].points[0]) == xy(edges[-1].points[-1]),
                             valid=all(xy(a.points[-1]) == xy(b.points[0])
                                       for a, b in zip(edges, edges[1:], strict=False)))
            self.edges = edges
    class Face(Shape):
        def __init__(self, wire):
            assert wire.isClosed() and wire.isValid()
            super().__init__()
            self.edges = wire.edges
            self.Faces, self.Solids = [], []
    class Doc(fixture.Document):
        def addObject(self, kind, name):
            obj = super().addObject(kind, name)
            obj.ViewObject = SimpleNamespace(DisplayMode="Flat Lines")
            return obj
    monkeypatch.setattr(fixture, "Document", Doc)
    monkeypatch.setattr(core, "Part", SimpleNamespace(
        Wire=Wire, Face=Face,
        LineSegment=lambda a, b: SimpleNamespace(toShape=lambda: Edge([a, b]))))
    source = open_triangle(kind)
    source["items"] = [tuple([item[0]] + [core.fitz.Point(point) for point in item[1:]])
                       for item in source["items"]]
    before = copy.deepcopy(source)
    opts = fixture.options(hatch_to_faces=hatch_faces, make_faces=True, compound_batch_size=0,
                           min_seg_len=0., assign_linewidth=True, map_dashes=True)
    document, _, _ = fixture.import_page([source], opts)
    assert source == before
    faces, wires = document.named("Face"), document.named("Wire")
    if not hatch_faces:
        assert not faces and len(wires) == 1
        assert not wires[0].Shape.isClosed() and len(wires[0].Shape.edges) == 2
        assert not hasattr(wires[0], "PDFSourceFillBoundaryJSON")
        return
    assert len(faces) == 1
    face = faces[0]
    assert len(face.Shape.edges) == 3
    assert face.ViewObject.DisplayMode == "Shaded"
    assert face.PDFStrokeRGB == "" and face.PDFFillRGB
    assert face.PDFLineWidthPt == 0. and face.PDFDashPattern == ""
    closing = face.Shape.edges[-1].points
    assert [(p.x, p.y) for p in closing] == [(20., 75.), (10., 90.)]
    # The persisted-only restore path must hide the fill closer again.
    face.ViewObject.DisplayMode = "Flat Lines"
    real_style(face, None, source["fill"], None, None, opts, persist_metadata=False)
    assert face.ViewObject.DisplayMode == "Shaded"
    if kind == "f":
        assert not wires
    else:
        assert len(wires) == 1
        wire = wires[0]
        assert len(wire.Shape.edges) == 2 and not wire.Shape.isClosed()
        assert wire.PDFFillRGB == "" and wire.PDFStrokeRGB
        assert wire.PDFLineWidthPt == 2.25 and wire.PDFDashPattern == "3,2"
        assert wire.ViewObject.LineColor == source["color"]
        assert wire.ViewObject.DrawStyle == "Dashed"
        assert json.loads(wire.PDFSourceFillBoundaryJSON)["policy"] == "retain_open_pdf_stroke"


@pytest.mark.parametrize("failure", ["setter", "readback"])
def test_implicit_fill_display_cannot_silently_show_unpainted_closing_stroke(failure):
    class View:
        @property
        def DisplayMode(self): return "Flat Lines"
        @DisplayMode.setter
        def DisplayMode(self, value):
            if failure == "setter": raise RuntimeError("native view rejected Shaded")
    obj = Object("Face", "Part::Feature")
    obj.ViewObject = View()
    obj.Shape = SimpleNamespace(Faces=[], Solids=[])
    obj.PDFSourceFillBoundaryJSON = json.dumps(dict(policy="close_connected_pdf_fill"))
    with pytest.raises(core.DrawingGeometryFailure, match="implicit fill display"):
        core._apply_style(obj, None, (.2, .4, .6), None, None, core.ImportOptions())
