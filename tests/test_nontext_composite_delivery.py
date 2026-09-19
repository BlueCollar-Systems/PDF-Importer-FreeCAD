from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import fitz
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "PDFVectorImporter" / "src"))
import PDFNonTextComposite as delivery
from fake_image_view_fc import ImageView, Translation
from test_nontext_composite_proof import originals, sample


def vector(x, y, z=0):
    return NS(x=x, y=y, z=z)


class Shape:
    def __init__(self, area):
        self.Area = area
        self.Faces = [object()]
        self.Vertexes = [NS(Point=vector(0, 0, 0))]
        self.BoundBox = NS(ZMax=0)

    def exportBrepToString(self):
        return repr((self.Area, len(self.Faces), [v.Point.z for v in self.Vertexes]))


class Object:
    def __init__(self, name, kind):
        self.Name, self.TypeId = name, kind
        self.PropertiesList = ["Shape"] if kind == "Part::Feature" else []
        self.ViewObject = None

    def addProperty(self, kind, name, group):
        self.PropertiesList.append(name)

    def getPropertyByName(self, name):
        assert name in self.PropertiesList
        return getattr(self, name)


class Document:
    def __init__(self):
        self.Objects = []

    def addObject(self, kind, name):
        obj = Object(name + str(len(self.Objects)), kind)
        self.Objects.append(obj)
        return obj

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]


@pytest.fixture
def case(tmp_path, monkeypatch):
    original, source_page = sample()
    original.xref_set_key(source_page.xref, "Group", "null")
    path = tmp_path / "source.pdf"
    original.save(path)
    original.close()
    pdf = fitz.open(path)
    page = pdf[0]
    proofs = originals(page)
    seq, source = next((s, p) for s, p in proofs.items() if "Multiply" in p["source_blend_modes"])
    doc = Document()
    capsule = doc.addObject("Part::Feature", "Capsule")
    capsule.Shape = Shape(source["capsule"]["area"])
    capsule.PDFStrokeFootprintJSON = json.dumps(dict(source["capsule"], page=1,
        source_paint_order=seq, source_svg_stroke=source["source_svg_stroke"],
        source_clip_bounds=source["clip_bounds"], source_blend_modes=source["source_blend_modes"],
        source_geometry_z=0, native_area=capsule.Shape.Area))
    monkeypatch.setitem(sys.modules, "FreeCAD", NS(Vector=vector, Rotation=lambda: NS(Q=(0, 0, 0, 1)),
                        Placement=lambda base, rotation: NS(Base=base, Rotation=rotation)))
    args = dict(pdf_path=path, source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                page_number=1, doc=doc, parent=doc, objects=list(doc.Objects),
                mapper=lambda p: vector(p[0], 100-p[1]), asset_dir=tmp_path / "assets", fitz=fitz)
    yield page, proofs, args, capsule
    pdf.close()


def test_retains_editable_native_capsule_and_embeds_separate_pixel_lattice_without_text_claim(case):
    page, proofs, args, capsule = case
    before = capsule.Shape.exportBrepToString()
    records = delivery.apply_composites(page, proofs, **args)
    assert len(records) == 1
    obj = args["doc"].Objects[-1]
    assert obj.TypeId == "Image::ImagePlane"
    assert not hasattr(obj, "PDFRepresentation") and not hasattr(obj, "PDFSourceItemId")
    assert obj.Placement.Base.z == 0
    assert capsule.Shape.exportBrepToString() == before
    data = json.loads(obj.PDFNonTextCompositeJSON)
    assert data["pixels"]["png_sha256"] == hashlib.sha256(Path(obj.PDFRasterFile).read_bytes()).hexdigest()
    box = data["recipe"]["coverage_bounds_pdf"]
    assert obj.XSize == pytest.approx(box[2]-box[0])
    assert obj.YSize == pytest.approx(box[3]-box[1])
    assert data["display_z_mm"] > 0 and data["source_geometry_z"] == 0


@pytest.mark.parametrize("mutation", ["page", "hash", "shape", "duplicate", "mapping"])
def test_rejects_changed_source_binding_or_missing_editable_geometry_before_host_creation(case, mutation):
    page, proofs, args, capsule = case
    if mutation == "page":
        args["page_number"] = 2
    elif mutation == "hash":
        args["source_sha256"] = "0" * 64
    elif mutation == "shape":
        capsule.Shape.Faces = []
    elif mutation == "duplicate":
        args["objects"].append(capsule)
    else:
        args["mapper"] = lambda p: vector(p[0] + .5*p[1], 100-p[1])
    with pytest.raises(ValueError):
        delivery.apply_composites(page, proofs, **args)
    assert args["doc"].Objects == [capsule]


def test_source_file_mutation_during_render_is_terminal_and_creates_no_host(case, monkeypatch):
    page, proofs, args, capsule = case
    original = delivery.render_recipes

    def changed(*values):
        result = original(*values)
        with args["pdf_path"].open("ab") as stream:
            stream.write(b"\n%changed")
        return result

    monkeypatch.setattr(delivery, "render_recipes", changed)
    with pytest.raises(ValueError, match="Original PDF changed"):
        delivery.apply_composites(page, proofs, **args)
    assert args["doc"].Objects == [capsule]


def test_whole_import_pixel_budget_does_not_reduce_dpi_or_create_partial_patch(case, monkeypatch):
    page, proofs, args, capsule = case
    args["remaining_pixels"] = 1

    def unexpected(*values):
        pytest.fail("over-budget render must not allocate pixels")

    monkeypatch.setattr(delivery, "render_recipes", unexpected)
    assert delivery.apply_composites(page, proofs, **args) == []
    assert args["doc"].Objects == [capsule]


def test_host_factory_losing_pixel_geometry_cleans_owned_object_only(case, monkeypatch):
    page, proofs, args, capsule = case
    monkeypatch.setitem(sys.modules, "FreeCAD", NS(Vector=vector, Rotation=lambda: NS(Q=(0, 0, 0, 1)),
                        Placement=lambda base, rotation: NS(Base=vector(0, 0, 0), Rotation=rotation)))
    with pytest.raises(ValueError, match="Native non-text composite"):
        delivery.apply_composites(page, proofs, **args)
    assert args["doc"].Objects == [capsule]


def test_rotated_image_with_correct_base_and_size_is_rejected(case, monkeypatch):
    page, proofs, args, capsule = case
    monkeypatch.setitem(sys.modules, "FreeCAD", NS(Vector=vector, Rotation=lambda: NS(Q=(0, 0, 1, 0)),
                        Placement=lambda base, rotation: NS(Base=base, Rotation=rotation)))
    with pytest.raises(ValueError, match="Native non-text composite"):
        delivery.apply_composites(page, proofs, **args)
    assert args["doc"].Objects == [capsule]


def test_patch_clears_actual_positive_native_and_existing_owned_display_depth(case):
    page, proofs, args, _ = case
    tall = Object("Tall", "Part::Feature")
    tall.Shape = NS(BoundBox=NS(ZMax=4))
    tall.PDFImageOrderDisplayJSON = json.dumps({"display_offset_z_mm": .7})
    args["objects"].append(tall)
    rows = delivery.apply_composites(page, proofs, **args)
    assert rows[0]["display_z_mm"] == pytest.approx(4.73)


def test_restore_rebinds_missing_external_file_to_verified_embedded_pixels(case, monkeypatch):
    page, proofs, args, _ = case
    delivery.apply_composites(page, proofs, **args)
    obj = args["doc"].Objects[-1]

    obj.ImageFile = "missing.png"
    obj.ViewObject = ImageView()
    monkeypatch.setitem(sys.modules, "pivy", NS(coin=NS(SoTranslation=Translation)))
    assert delivery.restore_display(obj)
    assert delivery.restore_display(obj)
    assert len(obj.ViewObject.RootNode.children) == 1
    assert obj.ViewObject.RootNode.children[0].value == (0, 0, .03)
    assert obj.ImageFile == obj.PDFRasterFile
    assert obj.ViewObject.Visibility is False
    assert obj.ViewObject.DisplayMode == "No shading"
    assert obj.ViewObject.Lighting == "One side"
    assert obj.Placement.Base.z == 0


def test_new_composite_uses_supported_native_image_mode(case, monkeypatch):
    page, proofs, args, _ = case
    original = args["doc"].addObject

    def with_gui(kind, name):
        obj = original(kind, name)
        if kind == "Image::ImagePlane":
            obj.ViewObject = ImageView()
        return obj

    monkeypatch.setattr(args["doc"], "addObject", with_gui)
    monkeypatch.setitem(sys.modules, "pivy", NS(coin=NS(SoTranslation=Translation)))
    assert len(delivery.apply_composites(page, proofs, **args)) == 1
    view = args["doc"].Objects[-1].ViewObject
    assert view.DisplayMode == "No shading" and view.Lighting == "One side"
    assert len(view.RootNode.children) == 1
