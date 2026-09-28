"""Depth scans retain all stored geometry without dynamic group Shape access."""

from pathlib import Path
from types import SimpleNamespace as NS
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "PDFVectorImporter" / "src"))
import PDFLatePaint as late
from PDFNonTextComposite import display_top, stored_shapes


class Object:
    def __init__(self, kind="Part::Feature", **properties):
        self.TypeId = kind
        self.PropertiesList = list(properties)
        self.values = properties
        self.reads = []

    def getPropertyByName(self, name):
        self.reads.append(name)
        return self.values[name]

    def addProperty(self, kind, name, group_name):
        self.PropertiesList.append(name)

    def __getattr__(self, name):
        if name == "Shape":
            raise AssertionError("Dynamic Shape getter must never be used")
        if name in self.values:
            return self.values[name]
        raise AttributeError(name)


def shape(z):
    return NS(BoundBox=NS(ZMax=z), isNull=lambda: False)


def group(*members):
    return Object("App::DocumentObjectGroup", Group=list(members))


def test_complete_nested_groups_equal_leaf_max_without_any_dynamic_shape_read():
    leaves = [Object(Shape=shape(z)) for z in (-4, 2, 7, 1)]
    inner = group(*leaves[:2])
    outer = group(inner, *leaves[2:])
    assert display_top([outer, *leaves, inner]) == 7
    assert all(leaf.reads == ["Shape"] for leaf in leaves)
    assert outer.reads == inner.reads == ["Group"]
    assert display_top(leaves) == 7


def test_incomplete_group_refuses_depth_instead_of_omitting_unlisted_tall_leaf():
    listed, missing = Object(Shape=shape(2)), Object(Shape=shape(20))
    with pytest.raises(ValueError, match="group member"):
        display_top([group(listed, missing), listed])
    assert listed.reads == missing.reads == []


@pytest.mark.parametrize(
    "kind, placement",
    [
        ("App::Part", False),
        ("App::DocumentObjectGroup", True),
    ],
)
def test_unproved_container_coordinates_are_not_treated_as_plain_group(kind, placement):
    values = {"Group": []}
    if placement:
        values["Placement"] = NS(Base=NS(z=8))
    with pytest.raises(ValueError, match="display container"):
        display_top([Object(kind, **values)])


def test_custom_declared_shape_is_not_discarded_by_type_or_group_property():
    obj = Object("App::FeaturePython", Shape=shape(13), Group=[])
    assert display_top([obj]) == 13
    assert obj.reads == ["Shape"]


@pytest.mark.parametrize(
    "property_name, key",
    [
        ("PDFImageOrderDisplayJSON", "display_offset_z_mm"),
        ("PDFRectOrderDisplayJSON", "display_offset_z_mm"),
        ("PDFDisplayPaintJSON", "display_z_mm"),
        ("PDFNonTextCompositeJSON", "display_z_mm"),
    ],
)
def test_actual_leaf_depth_and_each_owned_offset_still_control_top(property_name, key):
    obj = Object(Shape=shape(4), **{property_name: json.dumps({key: 0.7})})
    assert display_top([group(obj), obj]) == pytest.approx(4.7)


def test_image_and_native_text_placement_fallbacks_remain_live():
    image = Object("Image::ImagePlane", Placement=NS(Base=NS(z=9)))
    text = Object("App::FeaturePython", Placement=NS(Base=NS(z=11)))
    assert display_top([image, text]) == 11
    text.values["Placement"].Base.z = 12
    assert display_top([image, text]) == 12


def test_shape_is_read_again_on_next_call_and_nonfinite_depth_still_rejects():
    leaf = Object(Shape=shape(2))
    assert display_top([leaf]) == 2
    leaf.values["Shape"] = shape(5)
    assert display_top([leaf]) == 5
    assert leaf.reads == ["Shape", "Shape"]
    leaf.values["Shape"] = shape(float("nan"))
    with pytest.raises(ValueError, match="Nonfinite"):
        display_top([leaf])


def test_late_paint_scans_complete_group_and_leaf_without_dynamic_shape(monkeypatch):
    leaf = Object(Shape=shape(8))
    source_shape = NS(
        Faces=[1],
        isNull=lambda: False,
        BoundBox=NS(XMin=0, XMax=10, YMin=0, YMax=10, ZMin=0, ZMax=0),
    )
    fill = Object(
        Shape=source_shape, PDFFillRGB="0.7,1,1", Name="SourceFill", ViewObject=None
    )
    plain = group(leaf, fill)
    plain.values["PDFFillRGB"] = "0.7,1,1"
    captured = []

    def capture(_doc, _parent, _name, _pieces, data):
        captured.append(data)
        return None

    monkeypatch.setattr(late, "_native_face", capture)
    monkeypatch.setattr(
        late,
        "final_rows",
        lambda page: [
            dict(
                rect=(0, 0, 10, 10),
                fill=(0, 1, 1),
                fill_opacity=0.3,
                color=(0, 0, 0),
                width=1,
                seqno=12,
            ),
        ],
    )
    result = late.apply_final_paints(
        None,
        page_number=1,
        pdf_sha256="a" * 64,
        doc=None,
        parent=None,
        objects=[plain, leaf, fill],
        scale=1,
        mapper=lambda p: NS(x=p[0], y=p[1]),
    )
    assert len(result) == 1 and len(captured) == 5
    assert all(data["display_z_mm"] == pytest.approx(8.01) for data in captured)
    assert leaf.reads == ["Shape"] and plain.reads == ["Group"]
    assert fill.reads == ["Shape"]
    assert stored_shapes([leaf]) == [(leaf, leaf.values["Shape"])]
