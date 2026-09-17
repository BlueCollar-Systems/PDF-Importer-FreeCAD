from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'PDFVectorImporter/src'))
from PDFImporterCore import _apply_planar_fill_material


def shape(depth=0, faces=1, solids=0):
    return SimpleNamespace(Shape=SimpleNamespace(
        Faces=[object()] * faces, Solids=[object()] * solids,
        BoundBox=SimpleNamespace(ZLength=depth)))


@pytest.mark.parametrize('color', [(1.0, 1.0, 1.0), (0.15, 0.45, 0.75), (0.0, 0.0, 0.0)])
def test_planar_source_fill_survives_without_lighting_or_specular_tint(color):
    materials = [SimpleNamespace(Transparency=0.0), SimpleNamespace(Transparency=0.0)]
    view = SimpleNamespace(ShapeAppearance=materials)
    assert _apply_planar_fill_material(shape(), view, color)
    assert len(view.ShapeAppearance) == 2
    for material in view.ShapeAppearance:
        assert material.EmissiveColor == color
        assert material.AmbientColor == material.DiffuseColor == material.SpecularColor == (0, 0, 0)
        assert material.Shininess == 0
        assert material.Transparency == 0


def test_legacy_native_material_is_supported():
    view = SimpleNamespace(ShapeMaterial=SimpleNamespace())
    assert _apply_planar_fill_material(shape(), view, (1, 1, 1))
    assert view.ShapeMaterial.EmissiveColor == (1, 1, 1)


@pytest.mark.parametrize('obj', [shape(depth=1), shape(solids=1), shape(faces=0)])
def test_solid_and_nonplanar_geometry_keep_ordinary_material(obj):
    original = SimpleNamespace(DiffuseColor=(0.7, 0.7, 0.7))
    view = SimpleNamespace(ShapeAppearance=[original])
    assert not _apply_planar_fill_material(obj, view, (1, 1, 1))
    assert vars(original) == {'DiffuseColor': (0.7, 0.7, 0.7)}


def test_absent_native_material_uses_existing_color_only():
    view = SimpleNamespace(ShapeColor=(1, 1, 1))
    assert not _apply_planar_fill_material(shape(), view, (1, 1, 1))
    assert view.ShapeColor == (1, 1, 1)
