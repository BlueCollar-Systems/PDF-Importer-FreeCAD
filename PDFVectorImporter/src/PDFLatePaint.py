"""Source-proven final annotation paints and persistent display-only depth.

Source BReps and native text remain intact. Extra planar native faces represent
the proven final PDF paints; their view-node offsets never change source Z.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

try:
    from .PDFPaintProof import final_svg_rectangles
    from .PDFNonTextComposite import stored_shapes
except ImportError:
    from PDFPaintProof import final_svg_rectangles
    from PDFNonTextComposite import stored_shapes

DISPLAY_PROPERTY = "PDFDisplayPaintJSON"
_NODE_NAMES = ("BCSPDFDisplayTranslation", "BCSPDFDisplayMaterial")


def subtract_rectangles(bounds, cutters):
    remaining = [bounds]
    for cx0, cy0, cx1, cy1 in cutters:
        next_rects = []
        for x0, y0, x1, y1 in remaining:
            ix0, iy0, ix1, iy1 = max(x0, cx0), max(y0, cy0), min(x1, cx1), min(y1, cy1)
            if ix0 >= ix1 or iy0 >= iy1:
                next_rects.append((x0, y0, x1, y1))
                continue
            pieces = ((x0, y0, ix0, y1), (ix1, y0, x1, y1),
                      (ix0, y0, ix1, iy0), (ix0, iy1, ix1, y1))
            next_rects.extend(r for r in pieces if r[0] < r[2] and r[1] < r[3])
        remaining = next_rects
    return remaining


def paint_rectangles(bounds, width):
    x0, y0, x1, y1 = bounds
    half = width / 2
    ix0, iy0, ix1, iy1 = x0 + half, y0 + half, x1 - half, y1 - half
    ox0, oy0, ox1, oy1 = x0 - half, y0 - half, x1 + half, y1 + half
    return [(ix0, iy0, ix1, iy1), (ox0, oy0, ox1, iy0),
            (ox0, iy1, ox1, oy1), (ox0, iy0, ix0, iy1), (ix1, iy0, ox1, iy1)]


def final_rows(page):
    candidates = {}
    try:
        drawings = page.get_drawings(extended=True)
    except (AttributeError, TypeError, RuntimeError, ValueError):
        return []  # Optional display proof could not be established.
    for row in drawings:
        order = row.get("seqno")
        items = row.get("items", ())
        alpha, width = row.get("fill_opacity"), row.get("width")
        if (type(order) is not int or row.get("type") != "fs" or row.get("level") != 0
                or len(items) != 1 or items[0][0] != "re" or row.get("lineJoin") != 0
                or row.get("dashes") not in (None, "[] 0")
                or not isinstance(alpha, (int, float)) or not 0 < alpha < 1
                or row.get("stroke_opacity") != 1 or not isinstance(width, (int, float))
                or not math.isfinite(width) or width <= 0):
            continue
        box = tuple(row["rect"])
        if width < min(box[2] - box[0], box[3] - box[1]):
            candidates[order] = row
    if not candidates:
        return []
    log = page.get_bboxlog()
    end = len(log)
    result = []
    while end >= 2 and end - 2 in candidates:
        if log[end - 2][0] != "fill-path" or log[end - 1][0] != "stroke-path":
            break
        result.append(candidates[end - 2])
        end -= 2
    result.reverse()
    return result if result and final_svg_rectangles(page.get_svg_image(text_as_path=True), result) else []


def _property(obj, kind, name, value):
    if name not in obj.PropertiesList:
        obj.addProperty(kind, name, "PDF source display")
    setattr(obj, name, value)


def _metadata(obj, data):
    _property(obj, "App::PropertyBool", "PDFDisplayOnlyGeometry", True)
    _property(obj, "App::PropertyString", DISPLAY_PROPERTY, json.dumps(data, sort_keys=True))


def restore_display(obj):
    """Restore only importer-owned view nodes, for GUI and headless saves alike."""
    text = getattr(obj, DISPLAY_PROPERTY, None)
    if not text or getattr(obj, "ViewObject", None) is None:
        return False
    data = json.loads(text)
    rgb, alpha, depth = data["rgb"], data["opacity"], data["display_z_mm"]
    if (len(rgb) != 3 or not all(math.isfinite(v) and 0 <= v <= 1 for v in rgb)
            or not math.isfinite(alpha) or not 0 <= alpha <= 1 or not math.isfinite(depth)):
        raise ValueError("Invalid persisted PDF display paint")
    from pivy import coin
    root = obj.ViewObject.RootNode
    for index in reversed(range(root.getNumChildren())):
        if str(root.getChild(index).getName()) in _NODE_NAMES:
            root.removeChild(index)
    translation = coin.SoTranslation()
    translation.setName(_NODE_NAMES[0])
    translation.translation.setValue(0, 0, depth)
    material = coin.SoMaterial()
    material.setName(_NODE_NAMES[1])
    material.diffuseColor.setValue(0, 0, 0)
    material.ambientColor.setValue(0, 0, 0)
    material.specularColor.setValue(0, 0, 0)
    material.emissiveColor.setValue(*rgb)
    material.shininess.setValue(0)
    material.transparency.setValue(1 - alpha)
    material.setOverride(True)
    root.insertChild(translation, 0)
    root.insertChild(material, 1)
    return True


def restore_document_displays(doc):
    return sum(restore_display(obj) for obj in getattr(doc, "Objects", ()))


def _native_face(doc, parent, name, rectangles, data):
    import FreeCAD as App
    import Part
    faces = [Part.makePlane(x1 - x0, y1 - y0, App.Vector(x0, y0, 0))
             for x0, y0, x1, y1 in rectangles]
    if not faces:
        return None
    obj = doc.addObject("Part::Feature", name)
    obj.Shape = faces[0] if len(faces) == 1 else Part.makeCompound(faces)
    expected = sum((x1 - x0) * (y1 - y0) for x0, y0, x1, y1 in rectangles)
    if not math.isclose(obj.Shape.Area, expected, rel_tol=1e-9, abs_tol=1e-8):
        raise RuntimeError("Final PDF paint native area differs from source partition")
    _metadata(obj, dict(data, visible_area_mm2=expected))
    if parent is not doc and hasattr(parent, "addObject"):
        parent.addObject(obj)
    if obj.ViewObject is not None:
        obj.ViewObject.DisplayMode = "Shaded"
        obj.ViewObject.ShapeColor = tuple(data["rgb"])
        obj.ViewObject.Transparency = round(100 * (1 - data["opacity"]))
        restore_display(obj)
    return obj


def apply_final_paints(page, *, page_number, pdf_sha256, doc, parent, objects, mapper, scale):
    """Apply a bounded final-paint suffix after native text has been verified."""
    rows = final_rows(page)
    if not rows:
        return []
    objects = list(objects)
    object_shapes = stored_shapes(objects)
    cutters, top = [], 0.
    for obj, shape in object_shapes:
        if shape is not None and not shape.isNull():
            top = max(top, shape.BoundBox.ZMax)
        if getattr(obj, "PDFRepresentation", "") != "raster":
            continue
        source_id = getattr(obj, "PDFSourceItemId", "")
        if re.fullmatch(r"p%d:b\d+:l\d+:s\d+" % page_number, source_id) is None:
            # Embedded source images are not completed-page text crops; their
            # pixels do not already contain the final annotation paints.
            continue
        file = Path(str(obj.PDFRasterFile))
        if (obj.PDFSourceSHA256 != pdf_sha256 or not file.is_file()
                or hashlib.sha256(file.read_bytes()).hexdigest() != obj.PDFRasterSHA256):
            raise RuntimeError("Final PDF text crop has no verified source/pixel identity")
        if abs(obj.Placement.Rotation.Angle) > 1e-9 or abs(obj.Placement.Base.z) > 1e-9:
            raise RuntimeError("Final PDF text crop is not an axis-aligned source-plane rectangle")
        x, y = obj.Placement.Base.x, obj.Placement.Base.y
        w, h = float(obj.XSize), float(obj.YSize)
        if not all(math.isfinite(v) for v in (x, y, w, h)) or w <= 0 or h <= 0:
            raise RuntimeError("Invalid final PDF text crop dimensions")
        cutters.append((x - w / 2, y - h / 2, x + w / 2, y + h / 2))
    result = []
    for index, row in enumerate(rows):
        x0, y0, x1, y1 = row["rect"]
        mapped = [mapper(p) for p in ((x0, y0), (x0, y1), (x1, y0), (x1, y1))]
        bounds = (min(p.x for p in mapped), min(p.y for p in mapped),
                  max(p.x for p in mapped), max(p.y for p in mapped))
        matches = []
        for obj, shape in object_shapes:
            if not getattr(obj, "PDFFillRGB", "") or shape is None or not shape.Faces:
                continue
            try:
                composite = tuple(float(c) for c in obj.PDFFillRGB.split(","))
                expected_color = tuple(1 - row["fill_opacity"] + row["fill_opacity"] * c for c in row["fill"])
                if len(composite) != 3 or max(abs(a - b) for a, b in zip(composite, expected_color, strict=True)) > 1e-5:
                    continue
            except (TypeError, ValueError):
                continue
            b = shape.BoundBox
            if abs(b.ZMin) > 1e-9 or abs(b.ZMax) > 1e-9:
                continue
            if max(abs(a - c) for a, c in zip(bounds, (b.XMin, b.YMin, b.XMax, b.YMax), strict=True)) < 1e-6:
                matches.append(obj)
        if len(matches) != 1:
            # No fill was requested, or no unique native source face can be bound.
            continue
        source_obj = matches[0]
        data = {"schema": "bcs.freecad.final-paint/1", "page": page_number,
                "source_sha256": pdf_sha256, "source_object": source_obj.Name,
                "source_paint_order": row["seqno"], "source_bounds_pdf": list(row["rect"]),
                "source_stroke_width_pdf": row["width"], "source_geometry_z_mm": 0.,
                "display_z_mm": top + .01 + index * .002, "raster_crops_subtracted": len(cutters)}
        created = []
        for paint_index, rect in enumerate(paint_rectangles(bounds, row["width"] * scale)):
            pieces = subtract_rectangles(rect, cutters)
            color = row["fill"] if paint_index == 0 else row["color"]
            opacity = row["fill_opacity"] if paint_index == 0 else 1.
            obj = _native_face(doc, parent, "PDF_Final_Paint", pieces,
                               dict(data, rgb=list(color), opacity=opacity, paint_part=paint_index))
            if obj is not None:
                created.append(obj.Name)
        # Keep the exact editable source BRep, hidden behind its verified display.
        _property(source_obj, "App::PropertyString", "PDFDisplayReplacementJSON", json.dumps(created))
        source_obj.Visibility = False
        if source_obj.ViewObject is not None:
            source_obj.ViewObject.Visibility = False
        result.append(dict(data, display_objects=created))
    return result
