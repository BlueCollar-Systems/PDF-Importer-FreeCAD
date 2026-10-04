"""Exact source geometry regressions; these fake kernels are not OCC proof."""
import copy
from fractions import Fraction
import math
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "PDFVectorImporter", ROOT / "PDFVectorImporter/src", ROOT / "tests"):
    sys.path.insert(0, str(directory))
import PDFImporterCore as core  # noqa: E402
import PDFSourceFill as fill  # noqa: E402
import test_regular_compound_source_fill_fc as fixture  # noqa: E402


def edge(kind="l"):
    points = [(0., 0.), (3., 4.)] if kind == "l" else [(0., 0.), (0., 1.), (1., 1.), (1., 0.)]
    native = [fixture.vector(p) for p in points]
    if kind == "l":
        return fixture.Edge("l", native)
    curve = fixture.BezierCurve()
    curve.setPoles(native)
    return curve.toShape()


def topology_copy(original, reversed_orientation=False):
    result = copy.deepcopy(original)
    assert result is not original and not result.isSame(original)
    if reversed_orientation:
        result.Orientation = "Reversed"
        result.Vertexes.reverse()
    return result


@pytest.mark.parametrize("kind", ["l", "c"])
@pytest.mark.parametrize("reverse", [False, True])
def test_fresh_native_identity_can_preserve_exact_original_geometry(kind, reverse):
    original = edge(kind)
    observed = topology_copy(original, reverse)
    fill._edge_bijection([original], [observed])
    # No identity shortcut: even the same object must satisfy all readbacks.
    fill._edge_bijection([original], [original])


def test_reordered_boundary_with_reversed_topology_is_unique_and_complete():
    originals = [fixture.Edge("l", [fixture.vector(p) for p in command[1:]])
                 for command in fixture.rectangle(0, 0, 10, 20)]
    observed = [topology_copy(original, True) for original in reversed(originals)]
    fill._edge_bijection(originals, observed)


@pytest.mark.parametrize("fault", [
    "endpoint", "location", "direction", "first", "last", "length", "placement",
    "null", "invalid", "shape_type", "internal_orientation", "external_orientation",
    "periodic", "nonfinite_endpoint", "nonfinite_direction", "nonfinite_location",
    "nonfinite_first", "nonfinite_last", "nonfinite_length", "nonfinite_placement",
    "bool_coordinate", "bool_parameter", "zero_length", "zero_direction", "zero_trim",
    "reversed_trim", "vertex_count", "collapsed_endpoints", "short_placement",
    "getter_failure", "unsupported_curve",
])
def test_altered_line_is_refused_even_if_topology_claims_same(fault):
    original = edge()
    observed = topology_copy(original)
    observed.isSame = lambda other: True
    if fault == "endpoint": observed.Vertexes[-1].Point.x += math.ulp(3.)
    elif fault == "location":
        observed.Curve.Location = SimpleNamespace(x=math.ulp(1.),y=0.,z=0.)
    elif fault == "direction": observed.Curve.Direction.y += math.ulp(.8)
    elif fault == "first": observed.FirstParameter += math.ulp(1.)
    elif fault == "last": observed.LastParameter -= math.ulp(5.)
    elif fault == "length": observed.Length += math.ulp(5.)
    elif fault == "placement":
        matrix = list(observed.Placement.toMatrix().A); matrix[3] = math.ulp(1.)
        observed.Placement = SimpleNamespace(toMatrix=lambda: SimpleNamespace(A=matrix))
    elif fault == "null": observed.isNull = lambda: True
    elif fault == "invalid": observed.isValid = lambda: False
    elif fault == "shape_type": observed.ShapeType = "Wire"
    elif fault.endswith("orientation"): observed.Orientation = "Internal" if fault.startswith("internal") else "External"
    elif fault == "periodic": observed.Curve.isPeriodic = lambda: True
    elif fault == "nonfinite_endpoint": observed.Vertexes[-1].Point.x = math.nan
    elif fault == "nonfinite_direction": observed.Curve.Direction.x = math.inf
    elif fault == "nonfinite_location": observed.Curve.Location = SimpleNamespace(x=-math.inf,y=0.,z=0.)
    elif fault == "nonfinite_first": observed.FirstParameter = math.nan
    elif fault == "nonfinite_last": observed.LastParameter = math.inf
    elif fault == "nonfinite_length": observed.Length = math.inf
    elif fault == "nonfinite_placement": observed.Placement = SimpleNamespace(toMatrix=lambda: SimpleNamespace(A=[math.nan]*16))
    elif fault == "bool_coordinate": observed.Curve.Direction.x = True
    elif fault == "bool_parameter": observed.FirstParameter = False
    elif fault == "zero_length": observed.Length = 0.
    elif fault == "zero_direction": observed.Curve.Direction = fixture.vector((0., 0.))
    elif fault == "zero_trim": observed.LastParameter = observed.FirstParameter
    elif fault == "reversed_trim": observed.FirstParameter, observed.LastParameter = 5., 0.
    elif fault == "vertex_count": observed.Vertexes.pop()
    elif fault == "collapsed_endpoints": observed.Vertexes[-1].Point = observed.Vertexes[0].Point
    elif fault == "short_placement": observed.Placement = SimpleNamespace(toMatrix=lambda: SimpleNamespace(A=[1.]*15))
    elif fault == "getter_failure": observed.Placement = SimpleNamespace(toMatrix=lambda: (_ for _ in ()).throw(RuntimeError("native getter refused")))
    else: observed.Curve = type("BSplineCurve", (), {"isPeriodic": lambda self: False})()
    with pytest.raises(fill.SourceFillError):
        fill._edge_bijection([original], [observed])


@pytest.mark.parametrize("pole", range(4))
@pytest.mark.parametrize("axis", ["x", "y", "z"])
def test_every_original_cubic_control_is_compared_exactly(pole, axis):
    original = edge("c")
    observed = topology_copy(original)
    observed.isSame = lambda other: True
    point = observed.Curve.points[pole]
    replacement = SimpleNamespace(**vars(point))
    setattr(replacement, axis, getattr(point, axis) + math.ulp(1.))
    observed.Curve.points[pole] = replacement
    # Keep endpoint Vertexes unchanged, so every control check is independent
    # of the endpoint gate (including the first and last cubic control).
    assert [fixture.coords(v.Point) for v in observed.Vertexes] == [fixture.coords(v.Point) for v in original.Vertexes]
    with pytest.raises(fill.SourceFillError):
        fill._edge_bijection([original], [observed])


@pytest.mark.parametrize("fault", [
    "rational", "degree", "bool_degree", "weights", "weight_count", "pole_count",
    "trim_first", "trim_last", "periodic", "nonfinite_pole", "nonfinite_weight",
    "reversed_controls", "missing_poles", "missing_weights", "getter_failure",
])
def test_cubic_full_controls_rationality_degree_and_trim_are_required(fault):
    original = edge("c")
    observed = topology_copy(original)
    observed.isSame = lambda other: True
    if fault == "rational": observed.Curve.isRational = lambda: True
    elif fault == "degree": observed.Curve.Degree = 2
    elif fault == "bool_degree": observed.Curve.Degree = True
    elif fault == "weights": observed.Curve.getWeights = lambda: [1.,1.,1.+math.ulp(1.),1.]
    elif fault == "weight_count": observed.Curve.getWeights = lambda: [1.]*3
    elif fault == "pole_count": observed.Curve.points = observed.Curve.points[:-1]
    elif fault == "trim_first": observed.FirstParameter = math.ulp(1.)
    elif fault == "trim_last": observed.LastParameter = 1.-math.ulp(1.)
    elif fault == "periodic": observed.Curve.isPeriodic = lambda: True
    elif fault == "nonfinite_pole": observed.Curve.points[1].z = math.nan
    elif fault == "nonfinite_weight": observed.Curve.getWeights = lambda: [1.,1.,math.inf,1.]
    elif fault == "reversed_controls": observed.Curve.points.reverse()
    elif fault == "missing_poles": observed.Curve.getPoles = None
    elif fault == "missing_weights": observed.Curve.getWeights = None
    else: observed.Curve.getPoles = lambda: (_ for _ in ()).throw(RuntimeError("control getter refused"))
    with pytest.raises(fill.SourceFillError):
        fill._edge_bijection([original], [observed])


@pytest.mark.parametrize("fault", ["missing", "extra", "duplicate", "invented", "ambiguous_source", "bad_source"])
def test_bijection_rejects_missing_duplicate_invented_or_unproved_boundaries(fault):
    originals = [fixture.Edge("l", [fixture.vector(p) for p in command[1:]])
                 for command in fixture.rectangle(0, 0, 10, 20)]
    observed = [topology_copy(original) for original in originals]
    if fault == "missing": observed.pop()
    elif fault == "extra": observed.append(topology_copy(originals[0]))
    elif fault == "duplicate": observed[-1] = topology_copy(originals[0])
    elif fault == "invented": observed[-1] = edge()
    elif fault == "ambiguous_source": originals[-1] = topology_copy(originals[0])
    else: originals[-1].FirstParameter = math.inf
    with pytest.raises(fill.SourceFillError):
        fill._edge_bijection(originals, observed)


class CopiedWire(fixture.Wire):
    """Model only the observed MakeWire identity change, not native geometry."""
    def __init__(self, edges):
        super().__init__([edges[0]] + [topology_copy(e) for e in edges[1:]])
        self.originals = list(edges)


def test_genuine_frozen_pdf_ring_survives_joined_edge_identity_change(tmp_path):
    pdf = tmp_path / "original joined-edge ring.pdf"
    with core.fitz.open() as document:
        page = document.new_page(width=600, height=400)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
        path = page.new_shape()
        path.draw_rect(page.rect)
        path.draw_rect(core.fitz.Rect(290, 190, 310, 210))
        path.finish(color=None, fill=(0, 1, 0), even_odd=True)
        path.commit()
        document.save(pdf)
    with core.fitz.open(pdf) as document:
        row = document[0].get_drawings()[1]
    before = copy.deepcopy(row)
    contours = fill.source_contours(row, core._parse_rect)
    kernel = fixture.Kernel(contours)
    kernel.Wire = CopiedWire
    shape, receipt = fixture.compose(contours, kernel=kernel)
    assert row == before and shape.Area == 239600 and receipt["native_area"] == 239600
    assert (1, 1) not in shape.cells and receipt["source_native_contour_bijection"] is True
    assert all(not wire.Edges[1].isSame(wire.originals[1])
               for face in kernel.native_faces for wire in face.Faces[0].Wires)


@pytest.mark.parametrize("even_odd,opposite", [(True,False), (True,True), (False,False), (False,True)])
def test_overlap_and_empty_paint_keep_original_winding_after_topology_copy(even_odd, opposite):
    for contours in ([fixture.rectangle(0,0,10,10), fixture.rectangle(5,3,10,10,opposite)],
                     [fixture.rectangle(0,0,10,10), fixture.rectangle(0,0,10,10,opposite)]):
        kernel = fixture.Kernel(contours); kernel.Wire = CopiedWire
        shape, receipt = fixture.compose(contours, even_odd, kernel)
        reference = fixture.reference_cells(contours, even_odd)
        assert (set() if shape is None else shape.cells) == reference
        assert receipt["outcome"] == ("native_painted_area" if reference else "verified_zero_ink")


def test_genuine_pdf_cubic_uses_full_original_controls_through_copied_wire(tmp_path):
    pdf = tmp_path / "original cubic.pdf"
    with core.fitz.open() as document:
        page = document.new_page(width=100,height=100)
        path = page.new_shape()
        path.draw_bezier((10,20),(10,10),(20,10),(20,20))
        path.draw_line((20,20),(10,20))
        path.finish(color=None,fill=(.2,.6,.1)); path.commit(); document.save(pdf)
    with core.fitz.open(pdf) as document:
        row = document[0].get_drawings()[0]
    contours = fill.source_contours(row,core._parse_rect)
    assert len(contours) == 1 and [c[0] for c in contours[0]] == ["c","l"]
    expected = float(abs(fill.signed_source_area(contours[0])))
    assert expected == float(Fraction(60))
    class CurveKernel:
        BezierCurve = fixture.BezierCurve
        Wire = CopiedWire
        def LineSegment(self,a,b): return SimpleNamespace(toShape=lambda: fixture.Edge("l",[a,b]))
        def Face(self,wire):
            return SimpleNamespace(Area=expected,Faces=[SimpleNamespace(Wires=[wire])],
                                   isNull=lambda: False,isValid=lambda: True)
    face, sign = fill._native_face(contours[0],CurveKernel(),fixture.vector,1.,lambda:None)
    assert face.Area == expected and sign != 0
    assert [[fixture.coords(p) for p in e.Curve.getPoles()] for e in face.Faces[0].Wires[0].Edges if e.kind=="c"] == [
        [(p[0],p[1],0.) for p in contours[0][0][1:]]]


@pytest.mark.parametrize("fault", ["endpoint", "trim", "control", "unsupported"])
def test_bad_joined_edge_cannot_pass_genuine_pdf_source_contour(tmp_path, fault):
    pdf = tmp_path / "original square.pdf"
    with core.fitz.open() as document:
        page = document.new_page(width=100,height=100)
        page.draw_rect(core.fitz.Rect(10,10,40,40),color=None,fill=(.2,.6,.1))
        document.save(pdf)
    with core.fitz.open(pdf) as document:
        row = document[0].get_drawings()[0]
    contours = fill.source_contours(row,core._parse_rect)
    kernel = fixture.Kernel(contours)
    class BadWire(CopiedWire):
        def __init__(self, edges):
            super().__init__(edges)
            boundary = self.Edges[1]
            if fault == "endpoint": boundary.Vertexes[-1].Point.z = math.ulp(1.)
            elif fault == "trim": boundary.FirstParameter = math.ulp(1.)
            elif fault == "control":
                point = boundary.Curve.Location
                boundary.Curve.Location = SimpleNamespace(x=point.x,y=point.y,z=math.ulp(1.))
            else: boundary.Curve = type("BSplineCurve",(),{"isPeriodic":lambda self:False})()
    kernel.Wire = BadWire
    with pytest.raises(fill.SourceFillError): fixture.compose(contours,kernel=kernel)
    assert not kernel.native_faces  # Not converted to independent opaque faces.
