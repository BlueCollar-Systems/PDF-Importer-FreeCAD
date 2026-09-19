from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "PDFVectorImporter" / "src"))
import PDFImagePaintOrderProof as proof
import PDFImagePaintOrder as delivery


def source(*, after="stroke"):
    doc = fitz.open()
    page = doc.new_page(width=100, height=100)
    page.draw_line((0, 0), (100, 100), color=(0, 0, 0))
    pixels = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 12, 8), False)
    pixels.clear_with(200)
    page.insert_image(fitz.Rect(20, 20, 80, 60), pixmap=pixels)
    if after == "fill":
        page.draw_rect(fitz.Rect(25, 25, 70, 55), fill=(0, 0, 0))
    elif after == "translucent":
        page.draw_rect(fitz.Rect(25, 25, 70, 55), color=(1, 0, 0), stroke_opacity=.5)
    else:
        page.draw_rect(fitz.Rect(25, 25, 70, 55), color=(1, 0, 0))
    page.insert_text((35, 45), "TEST", fontsize=8, color=(1, 0, 0))
    return doc, page


def test_source_occurrence_pixels_later_stroke_and_text_are_independently_bound():
    doc, page = source()
    rows = proof.plan_opaque_images(page, fitz, "a" * 64)
    assert len(rows) == 1
    row = rows[0]
    assert len(row["later_strokes"]) == 1 and len(row["later_text"]) == 1
    assert list(row["later_text"].values())[0]["text"] == "TEST"
    png = proof.exact_image_bytes(page, row, fitz)
    assert hashlib.sha256(fitz.Pixmap(png).samples).hexdigest() == row["rgb_sha256"]
    assert row["source_quad_pdf"] == ((20., 20.), (80., 20.), (80., 60.), (20., 60.))
    doc.close()


@pytest.mark.parametrize("after", ["fill", "translucent"])
def test_unknown_later_paint_does_not_authorize_native_reordering(after):
    doc, page = source(after=after)
    assert proof.plan_opaque_images(page, fitz, "a"*64) == []
    doc.close()


def test_exact_image_reader_rejects_changed_pixel_or_affine_identity():
    doc, page = source()
    row = proof.plan_opaque_images(page, fitz, "a"*64)[0]
    row["rgb_sha256"] = "0"*64
    with pytest.raises(ValueError, match="ownership"):
        proof.exact_image_bytes(page, row, fitz)
    doc.close()


def vector(x, y, z=0):
    return NS(x=x, y=y, z=z)


class Object:
    def __init__(self, name, kind):
        self.Name, self.TypeId = name, kind
        self.PropertiesList = []
        self.ViewObject = None
        self.Placement = NS(Base=vector(0, 0), toMatrix=lambda: NS(A=[1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1]))
        self.Placement.Rotation = NS(Q=(0, 0, 0, 1))

    def addProperty(self, kind, name, group):
        self.PropertiesList.append(name)


class Shape:
    def __init__(self, points):
        self.points = points
        self.Faces = []
        self.Edges = [NS(Vertexes=[NS(Point=vector(*a)), NS(Point=vector(*b))], Length=math.dist(a, b))
                      for a, b in zip(points, points[1:], strict=False)]
        self.BoundBox = NS(ZMin=0., ZMax=0., ZLength=0.)

    def exportBrepToString(self):
        return repr(self.points)


@pytest.fixture
def native(tmp_path):
    original, _ = source()
    path = tmp_path / "source.pdf"
    original.save(path)
    original.close()
    doc = fitz.open(path)
    page = doc[0]
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    plans = proof.plan_opaque_images(page, fitz, sha)
    assert len(plans) == 1
    plan = plans[0]
    imgpath = tmp_path / "image.png"
    imgpath.write_bytes(proof.exact_image_bytes(page, plan, fitz))
    image = Object("Image", "Image::ImagePlane")
    image.PDFOpaqueImageSourceJSON = json.dumps(plan)
    image.ImageFile = str(imgpath)
    image.XSize, image.YSize = 60., 40.
    image.Placement.Base = vector(50, 60)
    image.PDFSourceItemId = "p1:img1"
    objects = [image]
    for seq, spec in plan["later_strokes"].items():
        obj = Object("Stroke"+str(seq), "Part::Feature")
        obj.Shape = Shape([(p[0], 100-p[1], 0.) for p in spec["points_pdf"]])
        obj.PDFImageOrderStrokeJSON = json.dumps(spec)
        objects.append(obj)
    text = Object("NativeText", "Part::Feature")
    text.Shape = Shape([(35., 55., 0.), (40., 55., 0.)])
    text.PDFSourceItemId = next(iter(plan["later_text"].values()))["source_item_id"]
    objects.append(text)
    attempts = [dict(source_item_id=text.PDFSourceItemId, outcome="verified", final_type="glyphs",
                     delivery_entity_ids=[text.Name])]
    kwargs = dict(pdf_path=path, source_sha256=sha, doc=None, objects=objects, attempts=attempts,
                  mapper=lambda p: vector(p[0], 100-p[1]), fitz=fitz)
    yield page, plans, kwargs
    doc.close()


def test_native_order_metadata_keeps_source_shapes_text_and_plane_geometry_unchanged(native):
    page, plans, kwargs = native
    before = [delivery._state(obj) for obj in kwargs["objects"]]
    rows = delivery.apply_image_order(page, plans, **kwargs)
    assert [r["representation_unchanged"] for r in rows] == ["embedded_image", "source_stroke", "glyphs"]
    assert [r["display_offset_z_mm"] for r in rows] == sorted(r["display_offset_z_mm"] for r in rows)
    assert [delivery._state(obj) for obj in kwargs["objects"]] == before
    assert kwargs["attempts"][0]["final_type"] == "glyphs"


@pytest.mark.parametrize("mutation", ["pixels", "corners", "rotation", "stroke", "text_owner", "missing_text"])
def test_native_corruption_is_terminal_before_any_reordering_metadata(native, mutation):
    page, plans, kwargs = native
    image, stroke, text = kwargs["objects"]
    if mutation == "pixels":
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 12, 8), False)
        pix.clear_with(10)
        pix.save(image.ImageFile)
    elif mutation == "corners":
        image.XSize += 1
    elif mutation == "rotation":
        image.Placement.Rotation = NS(Q=(0, 0, 1, 0))
    elif mutation == "stroke":
        stroke.Shape.Edges[0].Length += 1
    elif mutation == "text_owner":
        text.PDFSourceItemId = "unrelated"
    else:
        kwargs["attempts"] = []
    with pytest.raises(ValueError):
        delivery.apply_image_order(page, plans, **kwargs)
    assert not any(hasattr(obj, delivery.PROPERTY) for obj in kwargs["objects"])
