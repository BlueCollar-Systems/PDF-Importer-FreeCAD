"""Display exact, text-free source composites above retained editable paint."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

try:
    from .PDFNonTextCompositeProof import qualify_recipes, render_recipes, pixel_count, multiply_modes
except ImportError:
    from PDFNonTextCompositeProof import qualify_recipes, render_recipes, pixel_count, multiply_modes

PROPERTY = "PDFNonTextCompositeJSON"
_NODE_NAME = "BCSPDFNonTextCompositeTranslation"


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _xyz(point):
    return tuple(float(getattr(point, key)) for key in ("x", "y", "z"))


def _upright(obj):
    q = tuple(float(v) for v in obj.Placement.Rotation.Q)
    return (len(q) == 4 and all(math.isfinite(v) for v in q)
            and max(abs(v) for v in q[:3]) <= 1e-12 and abs(abs(q[3])-1) <= 1e-12)


def display_top(objects):
    """Bound current importer geometry plus all owned existing view offsets."""
    top = 0.
    for obj in objects:
        shape = getattr(obj, "Shape", None)
        placement = getattr(obj, "Placement", None)
        z = float(shape.BoundBox.ZMax) if shape else float(placement.Base.z) if placement else 0.
        for prop, key in (("PDFImageOrderDisplayJSON", "display_offset_z_mm"),
                          ("PDFRectOrderDisplayJSON", "display_offset_z_mm"),
                          ("PDFDisplayPaintJSON", "display_z_mm"),
                          (PROPERTY, "display_z_mm")):
            encoded = getattr(obj, prop, None)
            if encoded:
                z += float(json.loads(encoded)[key])
        if not math.isfinite(z):
            raise ValueError("Nonfinite existing native display depth")
        top = max(top, z)
    return top


def placement(recipe, mapper):
    """Require the complete pixel lattice's actual oriented rectangular map."""
    x0, y0, x1, y1 = recipe["coverage_bounds_pdf"]
    corners = [_xyz(mapper(p)) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    if not all(math.isfinite(v) for p in corners for v in p):
        raise ValueError("Nonfinite source composite placement")
    a, b, c, d = corners
    tolerance = 1e-9
    if (not all(abs(p[2]) <= tolerance for p in corners)
            or b[0] <= a[0] or d[1] >= a[1]
            or abs(b[1]-a[1]) > tolerance or abs(d[0]-a[0]) > tolerance
            or abs(c[0]-b[0]) > tolerance or abs(c[1]-d[1]) > tolerance):
        raise ValueError("Source composite requires an upright orthogonal pixel map")
    return dict(corners_mm=corners, width_mm=b[0]-a[0], height_mm=a[1]-d[1],
                center_mm=((a[0]+c[0])/2, (a[1]+c[1])/2, 0.))


def _property(obj, kind, name, value):
    if name not in obj.PropertiesList:
        obj.addProperty(kind, name, "PDF non-text display")
    setattr(obj, name, value)


def restore_display(obj):
    encoded = getattr(obj, PROPERTY, None)
    if not encoded or getattr(obj, "ViewObject", None) is None:
        return False
    data = json.loads(encoded)
    depth = data["display_z_mm"]
    if (data["schema"] != "bcs.freecad.nontext-composite/1"
            or data["recipe"]["text_and_image_free"] is not True
            or data["recipe"]["opacity"] != 1
            or not math.isfinite(depth) or depth <= 0):
        raise ValueError("Invalid persisted non-text display proof")
    expected_png = data["pixels"]["png_sha256"]
    image_path = str(getattr(obj, "ImageFile", ""))
    if not image_path or not Path(image_path).is_file() or _sha(image_path) != expected_png:
        included = str(getattr(obj, "PDFRasterFile", ""))
        if not included or not Path(included).is_file() or _sha(included) != expected_png:
            raise ValueError("Embedded non-text composite pixels are unavailable or changed")
        obj.ImageFile = included
    if obj.ViewObject.DisplayMode != "No shading":
        obj.ViewObject.DisplayMode = "No shading"
    from pivy import coin
    root = obj.ViewObject.RootNode
    for index in reversed(range(root.getNumChildren())):
        if str(root.getChild(index).getName()) == _NODE_NAME:
            root.removeChild(index)
    translation = coin.SoTranslation()
    translation.setName(_NODE_NAME)
    translation.translation.setValue(0, 0, depth)
    root.insertChild(translation, 0)
    return True


def restore_document_displays(doc):
    return sum(restore_display(obj) for obj in getattr(doc, "Objects", ()))


def _canonical_capsule(objects, recipe):
    matches = []
    for obj in objects:
        text = getattr(obj, "PDFStrokeFootprintJSON", None)
        if not text:
            continue
        data = json.loads(text)
        if (data.get("page") == recipe["page"]
                and data.get("source_paint_order") == recipe["source_paint_order"]):
            matches.append((obj, data))
    if len(matches) != 1:
        raise ValueError("Composite lacks its unique retained editable capsule")
    obj, data = matches[0]
    source = recipe["source_capsule_proof"]
    if (any(not _same(data.get(k), v) for k, v in source["capsule"].items())
            or not _same(data.get("source_svg_stroke"), source["source_svg_stroke"])
            or not _same(data.get("source_clip_bounds"), source["clip_bounds"])):
        raise ValueError("Retained editable capsule source identity changed")
    if (not _same(data.get("source_blend_modes"), source["source_blend_modes"])
            or data.get("source_geometry_z") != 0
            or len(obj.Shape.Faces) != 1
            or not math.isclose(obj.Shape.Area, data["native_area"], rel_tol=1e-7, abs_tol=1e-8)
            or any(abs(vertex.Point.z) > 1e-9 for vertex in obj.Shape.Vertexes)):
        raise ValueError("Retained editable capsule no longer matches source proof")
    return obj, hashlib.sha256(obj.Shape.exportBrepToString().encode()).hexdigest()


def apply_composites(page, capsule_proofs, *, pdf_path, source_sha256, page_number,
                     doc, parent, objects, mapper, asset_dir, fitz, remaining_pixels=64_000_000):
    """Create separate embedded display planes; never replace a text item."""
    if not any(multiply_modes(p.get("source_blend_modes")) for p in capsule_proofs.values()):
        return []
    if int(page.number) + 1 != page_number:
        raise ValueError("Composite page identity differs from the actual original page")
    if _sha(pdf_path) != source_sha256:
        raise ValueError("Original PDF changed before non-text composite qualification")
    recipes = qualify_recipes(page, capsule_proofs, source_sha256=source_sha256,
                              page_number=page_number, dpi=600)
    if type(remaining_pixels) is not int or remaining_pixels < 0:
        raise ValueError("Invalid whole-import composite pixel budget")
    if sum(pixel_count(r) for r in recipes) > remaining_pixels:
        return []  # Reported as unsupported by the caller; canonical geometry remains.
    bindings = [(_canonical_capsule(objects, r), placement(r, mapper)) for r in recipes]
    pixels = render_recipes(page, recipes, fitz)
    if _sha(pdf_path) != source_sha256:
        raise ValueError("Original PDF changed while rendering non-text composites")
    import FreeCAD as App
    offset_z = display_top(objects) + .03
    records, created = [], []
    try:
        for recipe, ((capsule, shape_sha), geometry), (png, pixel_proof) in zip(recipes, bindings, pixels, strict=True):
            path = Path(asset_dir) / ("nontext_" + pixel_proof["png_sha256"] + ".png")
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                # Content-addressed bytes are identical across references.
                with path.open("xb") as stream:
                    stream.write(png)
            if _sha(path) != pixel_proof["png_sha256"]:
                raise ValueError("Source composite asset bytes changed")
            obj = doc.addObject("Image::ImagePlane", "PDF_NonText_Composite")
            created.append(obj)
            obj.Label = "PDF non-text composite (hide to edit source geometry)"
            obj.ImageFile = str(path)
            obj.XSize, obj.YSize = geometry["width_mm"], geometry["height_mm"]
            obj.Placement = App.Placement(App.Vector(*geometry["center_mm"]), App.Rotation())
            data = dict(schema="bcs.freecad.nontext-composite/1", recipe=recipe,
                        pixels=pixel_proof, placement=geometry, source_geometry_z=0.,
                        display_z_mm=offset_z, canonical_object=capsule.Name,
                        canonical_brep_sha256=shape_sha,
                        display_resolution_limit="600 DPI source pixels; hide this plane to edit the retained exact source curves and capsule")
            _property(obj, "App::PropertyFileIncluded", "PDFRasterFile", str(path))
            _property(obj, "App::PropertyString", "PDFRasterSHA256", pixel_proof["png_sha256"])
            _property(obj, "App::PropertyString", "PDFSourceSHA256", source_sha256)
            _property(obj, "App::PropertyBool", "PDFDisplayOnlyGeometry", True)
            _property(obj, "App::PropertyString", PROPERTY, json.dumps(data, sort_keys=True))
            if parent is not doc and hasattr(parent, "addObject"):
                parent.addObject(obj)
            if (obj.TypeId != "Image::ImagePlane"
                    or not math.isclose(float(obj.XSize), geometry["width_mm"], abs_tol=1e-9)
                    or not math.isclose(float(obj.YSize), geometry["height_mm"], abs_tol=1e-9)
                    or _xyz(obj.Placement.Base) != geometry["center_mm"]
                    or not _upright(obj)
                    or hashlib.sha256(capsule.Shape.exportBrepToString().encode()).hexdigest() != shape_sha):
                raise ValueError("Native non-text composite altered source geometry or pixel placement")
            if obj.ViewObject is not None:
                obj.ViewObject.DisplayMode = "No shading"
                restore_display(obj)
            records.append(dict(data, native_object=obj.Name))
        return records
    except Exception:
        for obj in reversed(created):
            doc.removeObject(obj.Name)
        raise
