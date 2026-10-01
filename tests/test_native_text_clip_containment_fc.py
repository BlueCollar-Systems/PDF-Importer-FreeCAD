"""A broad native box must not replace conservative whole-geometry proof."""
from __future__ import annotations

import copy
from fractions import Fraction
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"))
import PDFPageTextClip as clip  # noqa: E402

PAGE = (0., 0., 12., 20.)
PADDED = (10., 2., 0., math.nextafter(12. + 1e-7, math.inf), 5., 1.)
INSIDE = (10., 2., 0., 12., 5., 1.)


class BSplineCurve:
    def __init__(self, poles=None, weights=None, output_domain=(1., 3.)):
        self.poles = poles or [(10., 2., 0.), (14., 5., 1.)]
        self.weights = [1., 1.] if weights is None else weights
        self.output_domain = output_domain
        self.trimmed_poles = [(10., 2., 0.), (12., 5., 1.)]
        self.segment_calls = []

    def copy(self):
        return copy.deepcopy(self)

    def segment(self, *domain):
        self.segment_calls.append(domain)
        assert domain == (1., 3.)
        self.poles = self.trimmed_poles
        self.FirstParameter, self.LastParameter = self.output_domain

    def getPoles(self):
        return self.poles

    def getWeights(self):
        return self.weights


class BezierCurve(BSplineCurve):
    pass


class Edge:
    def __init__(self, curve=None, box=PADDED):
        self.Curve = curve or BSplineCurve()
        self.ParameterRange = (1., 3.)
        self.box = box

    def optimalBoundingBox(self, triangulation, tolerance):
        assert triangulation is False and tolerance is False
        return SimpleNamespace(**dict(zip(
            ("XMin", "YMin", "ZMin", "XMax", "YMax", "ZMax"), self.box, strict=True)))


class BSplineSurface:
    def __init__(self, poles=None):
        self.poles = poles or [[(10., 2., 0.), (10., 2., 1.)],
                               [(12., 5., 0.), (12., 5., 1.)]]
        self.weights = [[1., 1.], [1., 1.]]
        self.domain = (1., 3., 2., 4.)
        self.output_domain = self.domain
        self.segment_calls = []

    def copy(self):
        return copy.deepcopy(self)

    def segment(self, *domain):
        self.segment_calls.append(domain)
        assert domain == self.domain
        self.domain = self.output_domain

    def getPoles(self):
        return self.poles

    def getWeights(self):
        return self.weights

    def bounds(self):
        return self.domain


class BezierSurface(BSplineSurface):
    pass


class Face(Edge):
    def __init__(self, surface=None, closed=True):
        super().__init__()
        self.Surface = surface or BSplineSurface()
        self.ParameterRange = (1., 3., 2., 4.)
        self.OuterWire = SimpleNamespace(isClosed=lambda: closed)
        self.Edges = [Edge()]


@pytest.mark.parametrize("kind", [BSplineCurve, BezierCurve])
def test_trimmed_native_curve_hull_proves_padding_without_changing_original(kind):
    curve = kind(output_domain=(0., 1.) if kind is BezierCurve else (1., 3.))
    original = copy.deepcopy(curve.__dict__)
    proof = clip._edge_containment(Edge(curve), PAGE)
    assert proof["method"] == "positive_rational_trimmed_curve_hull"
    assert proof["parameter_range"] == [1., 3.]
    assert proof["bounds"] == list(INSIDE)
    assert curve.__dict__ == original
    assert PADDED[3] > PAGE[2] + 1e-7


@pytest.mark.parametrize("weights", [[1., 0.], [1., -1.], [1., math.nan],
                                     [1., math.inf], [1.], []])
def test_unproved_rational_curve_weights_remain_failure(weights):
    with pytest.raises(ValueError, match="weights"):
        clip._edge_containment(Edge(BSplineCurve(weights=weights)), PAGE)


@pytest.mark.parametrize("poles", [[(10., 2., 0.), (12.0001, 5., 1.)],
                                   [(10., 2., 0.), (math.nan, 5., 1.)], []])
def test_outside_nonfinite_or_empty_trimmed_curve_cannot_hide_ink(poles):
    curve = BSplineCurve()
    curve.trimmed_poles = poles
    with pytest.raises(ValueError):
        clip._edge_containment(Edge(curve), PAGE)


@pytest.mark.parametrize("domain", [(math.nextafter(1., math.inf), 3.),
                                    (1., math.nextafter(3., -math.inf)), (math.nan, 3.)])
def test_incomplete_or_unavailable_trimmed_domain_cannot_prove_complete_edge(domain):
    with pytest.raises(ValueError):
        clip._edge_containment(Edge(BSplineCurve(output_domain=domain)), PAGE)


@pytest.mark.parametrize("kind", [BSplineSurface, BezierSurface])
def test_positive_rational_surface_hull_covers_face_interior(kind):
    surface = kind()
    original = copy.deepcopy(surface.__dict__)
    proof = clip._face_containment(Face(surface), PAGE)
    assert proof["method"] == "positive_rational_trimmed_surface_hull"
    assert proof["bounds"] == list(INSIDE)
    assert surface.__dict__ == original


def test_contained_boundaries_cannot_certify_a_bulging_nonplanar_surface():
    surface = BSplineSurface([[(10., 2., 0.), (10., 2., 1.)],
                              [(13., 5., .5), (12., 5., 1.)]])
    with pytest.raises(ValueError, match="surface hull crosses"):
        clip._face_containment(Face(surface), PAGE)


def planar_surface():
    return BSplineSurface([[(10., 2., 0.), (10., 5., 0.)],
                           [(13., 2., 0.), (13., 5., 0.)]])


def test_exact_planar_support_and_closed_boundary_can_certify_trimmed_cap():
    surface = planar_surface()
    # Native segment knot snapping cannot authorize the shrunken UV rectangle;
    # exact planarity is proved from the original whole support instead.
    surface.output_domain = (math.nextafter(1., math.inf), 3., 2., 4.)
    proof = clip._face_containment(Face(surface), PAGE)
    assert proof["method"] == "exact_planar_closed_boundary_hull"
    assert proof["constant_axes"] == [2]
    assert proof["edges"][0]["method"] == "positive_rational_trimmed_curve_hull"


@pytest.mark.parametrize("defect", ["open", "no_edges", "outside_edge", "unbounded",
                                    "negative_weight", "nonfinite_pole", "inconsistent_grid"])
def test_planar_shortcut_requires_complete_positive_boundary_and_support_proof(defect):
    face = Face(planar_surface(), closed=defect != "open")
    if defect == "no_edges":
        face.Edges = []
    elif defect == "outside_edge":
        face.Edges[0].Curve.trimmed_poles[-1] = (13., 5., 1.)
    elif defect == "unbounded":
        face.ParameterRange = (1., math.inf, 2., 4.)
    elif defect == "negative_weight":
        face.Surface.weights[0][0] = -1.
    elif defect == "nonfinite_pole":
        face.Surface.poles[0][0] = (10., 2., math.nan)
    elif defect == "inconsistent_grid":
        face.Surface.weights[0] = []
    with pytest.raises(ValueError):
        clip._face_containment(face, PAGE)


def test_unsupported_crossing_geometry_fails_but_ordinary_native_bounds_keep_their_path():
    edge = Edge(SimpleNamespace(), box=INSIDE)
    assert clip._edge_containment(edge, PAGE)["method"] == "native_analytic_bounds"
    edge.box = PADDED
    with pytest.raises(ValueError, match="supported"):
        clip._edge_containment(edge, PAGE)


def test_entire_shape_requires_every_face_edge_and_vertex_to_be_contained():
    shape = SimpleNamespace(Edges=[Edge()], Faces=[Face()],
                            Vertexes=[SimpleNamespace(Point=(10., 2., 0.)),
                                      SimpleNamespace(Point=(12., 5., 1.))])
    proof = clip._native_containment(shape, PAGE)
    assert proof["bounds"] == list(INSIDE)
    shape.Vertexes.append(SimpleNamespace(Point=(13., 3., 0.)))
    with pytest.raises(ValueError, match="vertex"):
        clip._native_containment(shape, PAGE)


class SinglePatch(BSplineSurface):
    """A curved quadratic-by-linear patch, not an inferred planar cap."""
    UDegree, VDegree = 2, 1

    def __init__(self):
        super().__init__([[(15., 2., 0.), (15., 2., 1.)],
                          [(11., 2., 0.), (11., 2., 1.)],
                          [(11., 5., 0.), (11., 5., 1.)]])
        self.weights = [[1., 1.], [1., 1.], [1., 1.]]
        self.uknots, self.vknots = [0., 1.], [0., 1.]
        self.umults, self.vmults = [3, 3], [2, 2]
        self.uperiodic = self.vperiodic = False

    def isUPeriodic(self):
        return self.uperiodic

    def isVPeriodic(self):
        return self.vperiodic

    def getUKnots(self):
        return self.uknots

    def getVKnots(self):
        return self.vknots

    def getUMultiplicities(self):
        return self.umults

    def getVMultiplicities(self):
        return self.vmults


def test_exact_patch_covers_full_one_ulp_overrun_and_preserves_original():
    surface = SinglePatch()
    original = copy.deepcopy(surface.__dict__)
    upper = math.nextafter(1., math.inf)
    domain = (.9, upper, 0., 1.)
    proof = clip._single_patch_surface_hull(surface, domain, PAGE)
    assert proof["method"] == "exact_homogeneous_single_patch_hull"
    assert proof["parameter_range"] == list(domain)
    assert proof["covers_endpoint_clamp_and_continuation"] is True
    assert proof["bounds"][3] < 12.
    # At the actual overrun the y coordinate exceeds its native endpoint y=5.
    # The exact certificate must include this, rather than silently clamp to1.
    exact_y = Fraction(2) + 3 * Fraction.from_float(upper) ** 2
    assert Fraction(proof["exact_bounds"][4]) >= exact_y > 5
    assert surface.__dict__ == original


def test_homogeneous_restriction_proves_rational_patch_with_positive_new_weights():
    surface = SinglePatch()
    surface.weights = [[2., 2.], [3., 3.], [1., 1.]]
    proof = clip._single_patch_surface_hull(surface, (.9, math.nextafter(1., math.inf), 0., 1.), PAGE)
    assert proof["bounds"][3] < 12.
    assert all(math.isfinite(value) for value in proof["bounds"])


@pytest.mark.parametrize("defect", ["two_ulp", "negative_weight", "nonfinite_weight", "zero_weight",
                                    "multispan", "unclamped", "periodic_u", "periodic_v",
                                    "large_degree", "bad_pole_count", "nonfinite_knot",
                                    "reversed_knots", "outside_hull", "nonpositive_extended_weight"])
def test_single_patch_certificate_rejects_unproved_or_crossing_surfaces(defect):
    surface = SinglePatch()
    domain = (.9, math.nextafter(1., math.inf), 0., 1.)
    if defect == "two_ulp":
        domain = (.9, math.nextafter(domain[1], math.inf), 0., 1.)
    elif defect in {"negative_weight", "nonfinite_weight", "zero_weight"}:
        surface.weights[0][0] = {"negative_weight": -1., "nonfinite_weight": math.nan, "zero_weight": 0.}[defect]
    elif defect == "multispan":
        surface.uknots = [0., .5, 1.]
        surface.umults = [3, 1, 3]
    elif defect == "unclamped":
        surface.umults = [2, 2]
    elif defect == "periodic_u":
        surface.uperiodic = True
    elif defect == "periodic_v":
        surface.vperiodic = True
    elif defect == "large_degree":
        surface.UDegree = 4
    elif defect == "bad_pole_count":
        surface.poles.pop()
        surface.weights.pop()
    elif defect == "nonfinite_knot":
        surface.uknots[1] = math.inf
    elif defect == "reversed_knots":
        surface.uknots.reverse()
    elif defect == "outside_hull":
        surface.poles[-1] = [(13., 5., 0.), (13., 5., 1.)]
    elif defect == "nonpositive_extended_weight":
        surface.weights[-1] = [1e-20, 1e-20]
    with pytest.raises(ValueError):
        clip._single_patch_surface_hull(surface, domain, PAGE)


def test_exact_blossom_retains_evaluation_at_both_requested_endpoints():
    polygon = [(Fraction(0), Fraction(2)), (Fraction(4), Fraction(3)),
               (Fraction(1), Fraction(1))]
    start, end = Fraction(9, 10), Fraction.from_float(math.nextafter(1., math.inf))
    result = clip._restrict_bezier(polygon, start, end)
    for parameter, actual in ((start, result[0]), (end, result[-1])):
        expected = tuple((1-parameter)**2*a + 2*(1-parameter)*parameter*b + parameter**2*c
                         for a, b, c in zip(*polygon, strict=True))
        assert actual == expected


def test_face_dispatches_incomplete_trim_to_exact_full_domain_certificate():
    # Name matches the real native class, with a synthetic clamped patch API.
    native_class = type("BSplineSurface", (SinglePatch,), {})
    surface = native_class()
    requested = (.9, math.nextafter(1., math.inf), 0., 1.)
    surface.domain = requested
    surface.output_domain = (.9, 1., 0., 1.)
    face = Face(surface)
    face.ParameterRange = requested
    proof = clip._face_containment(face, PAGE)
    assert proof["method"] == "exact_homogeneous_single_patch_hull"
    assert proof["parameter_range"][1] > 1.


class Plane:
    pass


def test_native_plane_uses_exact_closed_boundary_containment():
    face = Face(Plane())
    proof = clip._face_containment(face, PAGE)
    assert proof["planarity"] == "native_plane"
    assert proof["method"] == "exact_planar_closed_boundary_hull"


@pytest.mark.parametrize("defect", ["open", "outside_edge", "unbounded"])
def test_native_plane_requires_contained_closed_finite_boundaries(defect):
    face = Face(Plane(), closed=defect != "open")
    if defect == "outside_edge":
        face.Edges[0].Curve.trimmed_poles[-1] = (13., 5., 1.)
    elif defect == "unbounded":
        face.ParameterRange = (1., math.inf, 2., 4.)
    with pytest.raises(ValueError):
        clip._face_containment(face, PAGE)
