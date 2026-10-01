"""Source-page clipping is an in-mode operation on actual native ink."""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"))
from PDFPageTextClip import clip_native_text_shape  # noqa: E402
import PDFPageTextClip as clip_module  # noqa: E402


class Placement:
    def __init__(self, xyz=(0, 0, 0)):
        self.xyz = xyz

    def inverse(self):
        return Placement(tuple(-v for v in self.xyz))

    def toMatrix(self):
        return self.xyz


class Shape:
    """Analytic rectangle/prism double; native curved cases run in the GUI probe."""
    def __init__(self, box, dimension="Volume", measure=None, corrupt=False):
        self.box = tuple(box)
        self.dimension = dimension
        self.corrupt = corrupt
        self.explicit_measure = measure
        self.Solids = [1] if dimension == "Volume" else []
        self.Faces = [1] if dimension in {"Volume", "Area"} else []

    def copy(self):
        return Shape(self.box, self.dimension, self.explicit_measure, self.corrupt)

    def isNull(self):
        return self._measure() == 0

    def isValid(self):
        return True

    @property
    def BoundBox(self):
        raise AssertionError("display triangulation bounds cannot prove native ink")

    def optimalBoundingBox(self, use_triangulation, use_shape_tolerance):
        assert use_triangulation is False and use_shape_tolerance is False
        return SimpleNamespace(**dict(zip(
            ("XMin", "YMin", "ZMin", "XMax", "YMax", "ZMax"), self.box, strict=True)))

    def _measure(self):
        if self.explicit_measure is not None:
            return self.explicit_measure
        dimensions = {"Volume": 3, "Area": 2, "Length": 1}[self.dimension]
        return math.prod(max(0, self.box[i+3] - self.box[i]) for i in range(dimensions))

    @property
    def Volume(self):
        return self._measure() if self.dimension == "Volume" else 0

    @property
    def Area(self):
        return self._measure() if self.dimension == "Area" else 0

    @property
    def Length(self):
        return self._measure() if self.dimension == "Length" else 0

    def common(self, mask):
        box = tuple(max(self.box[i], mask.box[i]) for i in range(3)) + tuple(
            min(self.box[i], mask.box[i]) for i in range(3, 6))
        result = Shape(box, self.dimension)
        if self.corrupt:
            result.explicit_measure = result._measure() * 0.5
        return result

    def cut(self, mask):
        clean = Shape(self.box, self.dimension)
        return Shape(self.box, self.dimension,
                     measure=self._measure() - clean.common(mask)._measure())

    def transformGeometry(self, xyz):
        return Shape(tuple(value + xyz[i % 3] for i, value in enumerate(self.box)),
                     self.dimension, self.explicit_measure, self.corrupt)

    def transformShape(self, xyz, copy):
        assert copy is True, "object placement must not replace the inverse OCC location"
        self.box = tuple(value + xyz[i % 3] for i, value in enumerate(self.box))


class Host:
    def __init__(self, shape, xyz=(0, 0, 0)):
        self._shape = shape
        self.Placement = Placement(xyz)
        self.TypeId = "Part::Feature"
        self.PropertiesList = ["PDFSourceText", "PDFRepresentation"]
        self.PDFSourceText = "AB"
        self.PDFRepresentation = "3d_text"
        self.assignments = 0

    @property
    def Shape(self):
        return self._shape.transformGeometry(self.Placement.xyz)

    @Shape.setter
    def Shape(self, shape):
        self.assignments += 1
        self._shape = shape

    def addProperty(self, _kind, name, _group):
        self.PropertiesList.append(name)


class Part:
    def __init__(self):
        self.calls = 0

    def makeBox(self, dx, dy, dz, origin):
        self.calls += 1
        return Shape(tuple(origin) + tuple(a+b for a, b in zip(origin, (dx,dy,dz), strict=True)))

    @staticmethod
    def Shape():
        return Shape((0,0,0,0,0,0), measure=0.)


PAGE = ((0,0), (12,0), (12,20), (0,20))


@pytest.mark.parametrize("dimension,zmax", [("Volume", 1), ("Area", 0), ("Length", 0)])
def test_visible_ink_is_clipped_in_mode_and_source_anchor_is_preserved(dimension, zmax):
    host = Host(Shape((0,0,0,10,10,zmax), dimension), xyz=(5,7,2))
    original_placement = host.Placement
    original_semantics = (host.PDFSourceText, host.PDFRepresentation)
    proof = clip_native_text_shape(host, PAGE, part=Part(), vector=lambda *v: v)
    assert host.Placement is original_placement
    assert (host.PDFSourceText, host.PDFRepresentation) == original_semantics
    assert host.TypeId == "Part::Feature"
    assert host.Shape.box == (5,7,2,12,17,2+zmax)
    assert proof["source_measure"] == pytest.approx(proof["delivered_measure"] + proof["removed_measure"])
    assert proof["removed_measure"] > 0
    assert proof["measure"] == dimension.lower()
    assert json.loads(host.PDFSourcePageClipJSON) == proof


def test_contained_ink_ignores_a_crossing_metric_box_and_does_not_touch_shape():
    host = Host(Shape((1,2,0,8,9,1)))
    host.SourceMetricBox = (-100, -100, 100, 100)
    part = Part()
    assert clip_native_text_shape(host, PAGE, part=part, vector=lambda *v: v) is None
    assert host.assignments == part.calls == 0


def test_fully_outside_native_ink_keeps_semantics_without_visible_geometry():
    host = Host(Shape((0,0,0,10,10,1)), xyz=(15,7,2))
    original_placement = host.Placement
    proof = clip_native_text_shape(host, PAGE, part=Part(), vector=lambda *v: v)
    assert proof["status"] == "fully_clipped"
    assert host.Shape.isNull()
    assert proof["delivered_measure"] == 0.
    assert proof["removed_measure"] == proof["source_measure"]
    assert host.PDFSourceText == "AB"
    assert host.Placement is original_placement


def test_native_common_error_cannot_silently_drop_visible_text():
    host = Host(Shape((5,7,2,15,17,3), corrupt=True))
    with pytest.raises(ValueError, match="conserve"):
        clip_native_text_shape(host, PAGE, part=Part(), vector=lambda *v: v)
    assert host.assignments == 0
    assert "PDFSourcePageClipJSON" not in host.PropertiesList


def test_direct_clip_does_not_overwrite_a_parametric_shapes_recompute():
    host = Host(Shape((5,7,2,15,17,3)))
    host.TypeId = "Part::Extrusion"
    with pytest.raises(ValueError, match="persistent exact text feature"):
        clip_native_text_shape(host, PAGE, part=Part(), vector=lambda *v: v)
    assert host.assignments == 0


@pytest.mark.parametrize("page", [(), ((0,0),)*4,
    ((0,0),(10,1),(10,10),(0,10)), ((0,0),(12,0),(12,float("nan")),(0,20))])
def test_invalid_page_transform_cannot_authorize_clipping(page):
    host = Host(Shape((5,7,2,15,17,3)))
    with pytest.raises(ValueError):
        clip_native_text_shape(host, page, part=Part(), vector=lambda *v: v)
    assert host.assignments == 0


def test_conservative_certificate_is_rechecked_after_assignment_and_persisted(monkeypatch):
    native_bounds = clip_module._bounds
    calls = []

    def padded_bounds(shape):
        box = native_bounds(shape)
        if box[3] == 12.:
            box = box[:3] + (math.nextafter(12. + 1e-7, math.inf),) + box[4:]
        return box

    def conservative_certificate(shape, page):
        # Independent exact rectangle geometry, deliberately not its padded box.
        assert shape.box[0] >= page[0] and shape.box[1] >= page[1]
        assert shape.box[3] <= page[2] and shape.box[4] <= page[3]
        calls.append(shape)
        return {"method": "test_exact_rectangle_hull", "bounds": list(shape.box)}

    monkeypatch.setattr(clip_module, "_bounds", padded_bounds)
    monkeypatch.setattr(clip_module, "_native_containment", conservative_certificate)
    host = Host(Shape((5,7,2,15,17,3)))
    proof = clip_native_text_shape(host, PAGE, part=Part(), vector=lambda *v: v)
    assert len(calls) == 2
    assert proof["delivered_ink_bounds"][3] > 12. + 1e-7
    assert proof["native_containment"]["bounds"][3] == 12.
    assert json.loads(host.PDFSourcePageClipJSON) == proof


@pytest.mark.parametrize("failure_call", [1, 2])
def test_missing_or_failed_certificate_cannot_become_a_successful_clip(monkeypatch, failure_call):
    native_bounds = clip_module._bounds
    calls = []

    def padded_bounds(shape):
        box = native_bounds(shape)
        if box[3] == 12.:
            box = box[:3] + (math.nextafter(12. + 1e-7, math.inf),) + box[4:]
        return box

    def insufficient_proof(shape, page):
        calls.append(shape)
        if len(calls) == failure_call:
            raise ValueError("A native hull crosses the page")
        return {"bounds": list(shape.box)}

    monkeypatch.setattr(clip_module, "_bounds", padded_bounds)
    monkeypatch.setattr(clip_module, "_native_containment", insufficient_proof)
    host = Host(Shape((5,7,2,15,17,3)))
    with pytest.raises(ValueError, match="crosses its source page"):
        clip_native_text_shape(host, PAGE, part=Part(), vector=lambda *v: v)
    assert host.assignments == (1 if failure_call == 2 else 0)
    assert "PDFSourcePageClipJSON" not in host.PropertiesList
