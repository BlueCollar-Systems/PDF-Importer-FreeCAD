"""Restore bounded opaque-image painter order without altering native text."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

try:
    from .PDFImagePaintOrderProof import plan_opaque_images, _close
    from .PDFNonTextComposite import _upright, display_top
except ImportError:
    from PDFImagePaintOrderProof import plan_opaque_images, _close
    from PDFNonTextComposite import _upright, display_top

PROPERTY = "PDFImageOrderDisplayJSON"
NODE_NAME = "BCSPDFImageOrderTranslation"


def _same(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _xyz(p):
    return [float(p.x), float(p.y), float(p.z)]


def _state(obj):
    """Immutable native geometry/content; custom display metadata is separate."""
    shape = getattr(obj, "Shape", None)
    placement = getattr(obj, "Placement", None)
    return dict(type=obj.TypeId,
                brep=hashlib.sha256(shape.exportBrepToString().encode()).hexdigest() if shape else None,
                placement=list(placement.toMatrix().A) if placement else None,
                text=list(getattr(obj, "Text", ())), custom_text=str(getattr(obj, "CustomText", "")),
                source_id=str(getattr(obj, "PDFSourceItemId", "")))


def restore_display(obj):
    encoded = getattr(obj, PROPERTY, None)
    if not encoded or getattr(obj, "ViewObject", None) is None:
        return False
    data = json.loads(encoded)
    depth = data["display_offset_z_mm"]
    if (data["schema"] != "bcs.freecad.image-order-display/1"
            or not math.isfinite(depth) or depth <= 0):
        raise ValueError("Invalid persisted source image-order depth")
    # ImagePlane's included payload remains usable after moving the FCStd away
    # from the original cache. Never substitute a same-named but different PNG.
    if obj.TypeId == "Image::ImagePlane":
        expected = data["image_png_sha256"]
        path = Path(str(obj.ImageFile))
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            path = Path(str(obj.PDFRasterFile))
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("Opaque image's embedded source pixels are unavailable")
            obj.ImageFile = str(path)
        obj.ViewObject.DisplayMode = "No shading"
    from pivy import coin
    root = obj.ViewObject.RootNode
    for index in reversed(range(root.getNumChildren())):
        if str(root.getChild(index).getName()) == NODE_NAME:
            root.removeChild(index)
    node = coin.SoTranslation()
    node.setName(NODE_NAME)
    node.translation.setValue(0, 0, depth)
    root.insertChild(node, 0)
    return True


def restore_document_displays(doc):
    return sum(restore_display(obj) for obj in getattr(doc, "Objects", ()))


def _stroke_matches(obj, spec, mapper):
    if obj.TypeId != "Part::Feature" or len(obj.Shape.Faces):
        return False
    points = [_xyz(mapper(p)) for p in spec["points_pdf"]]
    expected = list(zip(points, points[1:], strict=False))
    if len(obj.Shape.Edges) != len(expected):
        return False
    remaining = list(expected)
    for edge in obj.Shape.Edges:
        if len(edge.Vertexes) != 2:
            return False
        a, b = [_xyz(v.Point) for v in edge.Vertexes]
        matches = [i for i, (x, y) in enumerate(remaining)
                   if (_close(a, x) and _close(b, y)) or (_close(a, y) and _close(b, x))]
        if len(matches) != 1:
            return False
        x, y = remaining.pop(matches[0])
        if not math.isclose(edge.Length, math.dist(x, y), rel_tol=1e-9, abs_tol=1e-8):
            return False
    return not remaining


def _text_objects(plan, attempts, objects):
    result = []
    by_name = {obj.Name: obj for obj in objects}
    for seq, source in sorted(plan["later_text"].items()):
        sid = source["source_item_id"]
        matches = [a for a in attempts if a.get("source_item_id") == sid
                   and a.get("outcome") == "verified"]
        if len(matches) != 1:
            raise ValueError("Later source text has no unique verified delivery")
        attempt = matches[0]
        ids = attempt.get("delivery_entity_ids") or attempt.get("created_entity_ids") or []
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("Later source text delivery has no unique native entities")
        bound = []
        for name in ids:
            obj = by_name.get(name)
            native_id = str(getattr(obj, "PDFSourceItemId", ""))
            if obj is None or not (native_id == sid or native_id.startswith(sid + ":")):
                raise ValueError("Later source text native ownership changed")
            bound.append(obj)
        # Parent and child translations together would apply depth twice.
        if any(parent in bound for obj in bound for parent in getattr(obj, "InList", ())):
            raise ValueError("Later source text has nested display ownership")
        result.append((seq, bound, attempt["final_type"]))
    return result


def apply_image_order(page, plans, *, pdf_path, source_sha256, doc, objects,
                      attempts, mapper, fitz):
    if not plans:
        return []
    if hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest() != source_sha256:
        raise ValueError("Original PDF changed before image-order delivery")
    if not _same(plans, plan_opaque_images(page, fitz, source_sha256)):
        raise ValueError("Original page image-order proof changed during import")
    bound = []
    for plan in plans:
        matched = [obj for obj in objects if getattr(obj, "PDFOpaqueImageSourceJSON", None)
                   and _same(json.loads(obj.PDFOpaqueImageSourceJSON), plan)]
        if len(matched) != 1:
            raise ValueError("Opaque source image lacks a unique native plane")
        image = matched[0]
        pix = fitz.Pixmap(str(image.ImageFile))
        if (image.TypeId != "Image::ImagePlane" or pix.n != 3 or pix.alpha
                or (pix.width, pix.height) != (plan["width"], plan["height"])
                or hashlib.sha256(pix.samples).hexdigest() != plan["rgb_sha256"]):
            raise ValueError("Native image differs from original opaque source pixels")
        quad = [_xyz(mapper(p)) for p in plan["source_quad_pdf"]]
        a, b, c, d = quad
        if (not _close(_xyz(image.Placement.Base), [(a[0]+c[0])/2, (a[1]+c[1])/2, 0.])
                or not _upright(image)
                or not math.isclose(float(image.XSize), b[0]-a[0], abs_tol=1e-8)
                or not math.isclose(float(image.YSize), a[1]-d[1], abs_tol=1e-8)):
            raise ValueError("Native image does not retain the source affine corners")
        strokes = []
        for seq, spec in sorted(plan["later_strokes"].items()):
            matches = [obj for obj in objects if getattr(obj, "PDFImageOrderStrokeJSON", None)
                       and _same(json.loads(obj.PDFImageOrderStrokeJSON), spec)]
            if len(matches) != 1 or not _stroke_matches(matches[0], spec, mapper):
                raise ValueError("Later native stroke lacks exact source centerline ownership")
            strokes.append((seq, matches[0]))
        bound.append((plan, image, strokes, _text_objects(plan, attempts, objects)))
    if hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest() != source_sha256:
        raise ValueError("Original PDF changed during image-order delivery")
    records = []
    top = display_top(objects) + 0.1
    for plan, image, strokes, texts in bound:
        ordered = [(plan["source_paint_order"], [image], "embedded_image")]
        ordered += [(seq, [obj], "source_stroke") for seq, obj in strokes]
        ordered += texts
        for seq, native_objects, representation in sorted(ordered, key=lambda x: x[0]):
            top += .03
            for obj in native_objects:
                before = _state(obj)
                low = float(obj.Shape.BoundBox.ZMin) if getattr(obj, "Shape", None) else float(obj.Placement.Base.z)
                data = dict(schema="bcs.freecad.image-order-display/1", source_sha256=source_sha256,
                            page=plan["page"], source_paint_order=seq, source_image_order=plan["source_paint_order"],
                            display_offset_z_mm=top-low, native_state=before,
                            representation_unchanged=representation, source_proof=plan)
                if obj is image:
                    data["image_png_sha256"] = hashlib.sha256(Path(str(image.ImageFile)).read_bytes()).hexdigest()
                if PROPERTY not in obj.PropertiesList:
                    obj.addProperty("App::PropertyString", PROPERTY, "PDF source display")
                setattr(obj, PROPERTY, json.dumps(data, sort_keys=True))
                restore_display(obj)
                if before != _state(obj):
                    raise ValueError("Source image ordering changed native geometry or text")
                records.append(dict(data, native_object=obj.Name))
            top += max([float(obj.Shape.BoundBox.ZLength) for obj in native_objects
                        if getattr(obj, "Shape", None)] + [0.])
    return records
