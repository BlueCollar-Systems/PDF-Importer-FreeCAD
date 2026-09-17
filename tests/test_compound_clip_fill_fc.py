"""Clipped vector fills keep compound contours, holes and exact source edges."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'PDFVectorImporter' / 'src'))
import PDFImporterCore as core


class Vector:
    def __init__(self, x, y, z=0):
        self.x, self.y, self.z = x, y, z


class Wire:
    def __init__(self, edges):
        self.edges = edges

    def isClosed(self):
        a, b = self.edges[0][1][0], self.edges[-1][1][-1]
        return (a.x, a.y, a.z) == (b.x, b.y, b.z)


class Curve:
    def setPoles(self, points):
        self.points = points

    def toShape(self):
        return ('c', self.points)


@pytest.fixture
def native(monkeypatch):
    calls = []
    face = SimpleNamespace(normalAt=lambda *args: Vector(0, 0, 1))
    shape = SimpleNamespace(Faces=[face], isValid=lambda: True)

    def make_face(wires, maker):
        calls.append((wires, maker))
        return shape

    part = SimpleNamespace(
        LineSegment=lambda a, b: SimpleNamespace(toShape=lambda: ('l', [a, b])),
        BezierCurve=Curve, Wire=Wire, makeFace=make_face)
    monkeypatch.setattr(core, 'Part', part)
    monkeypatch.setattr(core, '_to_fc', lambda point, page_h, opts, scale:
                        Vector(point[0] * scale, (page_h - point[1]) * scale))
    return calls, shape


def rect(x0, y0, x1, y1):
    return ('re', core.fitz.Rect(x0, y0, x1, y1), 1)


def build(items, even_odd=True):
    return core._compound_clip_fill_shape(
        {'items': items, 'even_odd': even_odd, 'bcs_clip_fill_group_id': 'clip-fill:7'},
        100, core.ImportOptions(), 2)


def test_counter_and_island_are_one_even_odd_fill(native):
    calls, shape = native
    result = build([rect(0, 0, 20, 20), rect(3, 3, 17, 17), rect(7, 7, 13, 13)])
    assert result is shape
    assert len(calls) == 1
    wires, maker = calls[0]
    assert maker == 'Part::FaceMakerBullseye'
    assert len(wires) == 3
    assert all(wire.isClosed() for wire in wires)
    assert wires[0].edges[0][1][0].y == 200
    assert wires[0].edges[1][1][1].x == 40


def test_discontinuous_segments_close_separate_contours(native):
    build([('l', (0, 0), (4, 0)), ('l', (4, 0), (2, 3)),
           ('l', (10, 0), (14, 0)), ('l', (14, 0), (12, 3))])
    wires = native[0][0][0]
    assert len(wires) == 2
    assert [len(w.edges) for w in wires] == [3, 3]


def test_cubic_keeps_exact_control_points_without_tessellation(native):
    build([('c', (0, 0), (0, 3), (4, 3), (4, 0))])
    wire = native[0][0][0][0]
    assert [edge[0] for edge in wire.edges] == ['c', 'l']
    assert [(p.x, p.y) for p in wire.edges[0][1]] == [(0, 200), (0, 194), (8, 194), (8, 200)]


def test_compound_nonzero_fails_instead_of_inventing_holes(native):
    with pytest.raises(RuntimeError, match='compound nonzero winding'):
        build([rect(0, 0, 10, 10), rect(2, 2, 8, 8)], even_odd=False)
    assert not native[0]


def test_single_nonzero_contour_is_supported(native):
    build([rect(0, 0, 10, 10)], even_odd=False)
    assert len(native[0]) == 1


@pytest.mark.parametrize('items', [[], [('unsupported', 1)]])
def test_unusable_source_never_becomes_an_empty_success(native, items):
    with pytest.raises(RuntimeError, match='Clipped fill clip-fill:7'):
        build(items)


def test_invalid_native_face_is_not_silently_downgraded(native):
    native[1].isValid = lambda: False
    with pytest.raises(RuntimeError, match='valid counter-aware faces'):
        build([rect(0, 0, 10, 10)])


def test_unsupported_inventory_clip_is_not_downgraded_to_raster(monkeypatch):
    from pdfcadcore.drawing_clips import UnsupportedClipFillError

    def fail(page):
        raise UnsupportedClipFillError('Cannot preserve a partial clip')

    monkeypatch.setattr(core, 'get_clip_aware_drawings', fail)
    with pytest.raises(UnsupportedClipFillError, match='partial clip'):
        core._page_visual_inventory(SimpleNamespace(), 'auto')
