from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'PDFVectorImporter/src'))
from PDFImporterCore import _apply_planar_outline_display, _apply_style


def object_with_shape(*, depth=0.0, faces=1, solids=0):
    return SimpleNamespace(
        Shape=SimpleNamespace(Faces=[object()] * faces, Solids=[object()] * solids,
                              BoundBox=SimpleNamespace(ZLength=depth)),
        ViewObject=SimpleNamespace(DisplayMode='Flat Lines'))


@pytest.mark.parametrize('stroke', [(1, 0, 0), (0, 0, 0), (1, 1, 1)])
def test_source_stroke_only_face_has_no_invented_fill(stroke):
    obj = object_with_shape()
    original_shape = obj.Shape
    _apply_style(obj, stroke, None, 0.5, [],
                 SimpleNamespace(assign_linewidth=True, map_dashes=False),
                 persist_metadata=False)
    assert obj.ViewObject.DisplayMode == 'Wireframe'
    assert obj.ViewObject.LineColor == stroke
    assert obj.Shape is original_shape
    assert len(obj.Shape.Faces) == 1
    assert _apply_planar_outline_display(obj, obj.ViewObject, stroke, None)
    assert obj.ViewObject.DisplayMode == 'Wireframe'


@pytest.mark.parametrize('stroke,fill,shape_args', [
    ((1, 0, 0), (1, 1, 1), {}),
    ((1, 0, 0), (0, 0, 0), {}),
    (None, (1, 1, 1), {}),
    (None, None, {}),
    ((1, 0, 0), None, {'solids': 1}),
    ((1, 0, 0), None, {'depth': 1.0}),
    ((1, 0, 0), None, {'faces': 0}),
])
def test_fill_and_3d_semantics_are_not_inferred_from_color(stroke, fill, shape_args):
    obj = object_with_shape(**shape_args)
    original = vars(obj.ViewObject).copy()
    assert not _apply_planar_outline_display(obj, obj.ViewObject, stroke, fill)
    assert vars(obj.ViewObject) == original
