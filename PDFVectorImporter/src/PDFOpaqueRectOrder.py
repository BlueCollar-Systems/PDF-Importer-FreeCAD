"""Original opaque rectangle/text display order; physical geometry stays planar."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

try:
    from .PDFOpaqueRectOrderProof import plan_opaque_rectangles
    from .PDFImagePaintOrder import (
        _state,
        _same,
        _text_objects,
        restore_display as _restore,
    )
    from .PDFNonTextComposite import display_top
    from .PDFStyleRestore import apply_text3d_filled_style
except ImportError:
    from PDFOpaqueRectOrderProof import plan_opaque_rectangles
    from PDFImagePaintOrder import (
        _state,
        _same,
        _text_objects,
        restore_display as _restore,
    )
    from PDFNonTextComposite import display_top
    from PDFStyleRestore import apply_text3d_filled_style

PROPERTY = "PDFRectOrderDisplayJSON"
SOURCE_PROPERTY = "PDFOpaqueRectSourceJSON"
SCHEMA = "bcs.freecad.opaque-rectangle-order/1"
NODE = "BCSPDFOpaqueRectangleTranslation"


def _rectangle_state(obj):
    shape = obj.Shape
    return dict(
        vertices=sorted(
            (float(v.Point.x), float(v.Point.y), float(v.Point.z))
            for v in shape.Vertexes
        ),
        area=float(shape.Area),
        faces=len(shape.Faces),
        wires=len(shape.Wires),
        edges=len(shape.Edges),
        valid=not shape.isNull() and shape.isValid(),
    )


def restore_display(obj):
    if not getattr(obj, PROPERTY, None) or getattr(obj, "ViewObject", None) is None:
        return False
    data = json.loads(getattr(obj, PROPERTY))
    depth = data["display_offset_z_mm"]
    if data["schema"] != SCHEMA or not math.isfinite(depth) or depth <= 0:
        raise ValueError("invalid persisted rectangle display schema/depth")
    actual = _state(obj)
    if any(
        not _same(actual[key], data["native_state"][key])
        for key in ("type", "placement", "text", "custom_text", "source_id")
    ):
        raise ValueError(
            "stale rectangle display: native placement/text/source identity changed"
        )
    if data["representation_unchanged"] == "source_opaque_rectangle":
        plan = data["source_proof"]
        if not _same(plan, json.loads(getattr(obj, SOURCE_PROPERTY))):
            raise ValueError("stale rectangle display source proof")
        expected = data["native_rectangle"]
        actual = _rectangle_state(obj)
        if (
            not actual["valid"]
            or any(actual[key] != expected[key] for key in ("faces", "wires", "edges"))
            or len(actual["vertices"]) != len(expected["vertices"])
            or not math.isclose(
                actual["area"], expected["area"], rel_tol=1e-8, abs_tol=1e-8
            )
            or any(
                not math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8)
                for p, q in zip(actual["vertices"], expected["vertices"], strict=True)
                for a, b in zip(p, q, strict=True)
            )
        ):
            raise ValueError("stale rectangle display: source face geometry changed")
    restored = _restore(obj, property_name=PROPERTY, node_name=NODE, schema=SCHEMA)
    if data["representation_unchanged"] == "source_opaque_rectangle":
        plan = data["source_proof"]
        view = obj.ViewObject
        apply_text3d_filled_style(view, plan["fill_rgb"])
        view.Transparency = 0
        if plan["stroke_rgb"] is not None:
            view.DisplayMode = "Flat Lines"
            view.LineColor = tuple(plan["stroke_rgb"])
    return restored


def restore_document_displays(doc):
    return sum(restore_display(obj) for obj in getattr(doc, "Objects", ()))


def verify_rectangle(obj, plan, mapper):
    shape = obj.Shape
    if (
        obj.TypeId != "Part::Feature"
        or shape.isNull()
        or not shape.isValid()
        or len(shape.Faces) != 1
        or len(shape.Wires) != 1
        or len(shape.Edges) != 4
        or len(shape.Vertexes) != 4
    ):
        raise ValueError("opaque source rectangle lost native face ownership")
    quad = [mapper(p) for p in plan["source_quad_pdf"]]
    expected = sorted((float(p.x), float(p.y), float(p.z)) for p in quad)
    actual = sorted(
        (float(v.Point.x), float(v.Point.y), float(v.Point.z)) for v in shape.Vertexes
    )
    if any(
        not all(
            math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-8)
            for a, b in zip(p, q, strict=False)
        )
        for p, q in zip(actual, expected, strict=False)
    ):
        raise ValueError("opaque source rectangle native vertices changed")
    area = (
        abs(
            math.fsum(
                quad[i].x * quad[(i + 1) % 4].y - quad[(i + 1) % 4].x * quad[i].y
                for i in range(4)
            )
        )
        / 2
    )
    if not math.isclose(float(shape.Area), area, rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError("opaque source rectangle native fill area changed")


def apply_rectangle_order(
    page, plans, *, pdf_path, source_sha256, objects, attempts, mapper
):
    if not plans:
        return []
    if hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest() != source_sha256:
        raise ValueError("original PDF changed before opaque rectangle delivery")
    if not _same(plans, plan_opaque_rectangles(page, source_sha256)):
        raise ValueError("original opaque rectangle paint proof changed")
    bound = []
    owned = set()
    for plan in plans:
        matches = [
            obj
            for obj in objects
            if getattr(obj, SOURCE_PROPERTY, None)
            and _same(json.loads(getattr(obj, SOURCE_PROPERTY)), plan)
        ]
        if len(matches) != 1:
            raise ValueError("source rectangle lacks a unique native face")
        rectangle = matches[0]
        verify_rectangle(rectangle, plan, mapper)
        ordered = [(plan["source_paint_order"], [rectangle], "source_opaque_rectangle")]
        for trace in plan["later_text"]:
            for item in trace["items"]:
                ordered += _text_objects(
                    {"later_text": {trace["source_paint_order"]: item}},
                    attempts,
                    objects,
                )
        for _, rows, _ in ordered:
            for obj in rows:
                if obj.Name in owned or getattr(obj, "PDFImageOrderDisplayJSON", None):
                    raise ValueError(
                        "source rectangle display has conflicting native ownership"
                    )
                owned.add(obj.Name)
        bound.append((plan, ordered))
    if hashlib.sha256(Path(pdf_path).read_bytes()).hexdigest() != source_sha256:
        raise ValueError("original PDF changed during opaque rectangle delivery")
    records = []
    base_top = display_top(objects) + 0.1
    for plan, ordered in bound:
        # Each accepted closure rejects other later masks. Independent regions
        # share a safe height instead of manufacturing a document-sized stack.
        top = base_top
        for seq, rows, representation in sorted(ordered, key=lambda row: row[0]):
            top += 0.03
            for obj in rows:
                before = _state(obj)
                low = (
                    float(obj.Shape.BoundBox.ZMin)
                    if getattr(obj, "Shape", None)
                    else float(obj.Placement.Base.z)
                )
                data = dict(
                    schema=SCHEMA,
                    source_sha256=source_sha256,
                    page=plan["page"],
                    source_paint_order=seq,
                    source_rectangle_order=plan["source_paint_order"],
                    display_offset_z_mm=top - low,
                    native_state=before,
                    representation_unchanged=representation,
                    source_proof=plan,
                )
                if representation == "source_opaque_rectangle":
                    data["native_rectangle"] = _rectangle_state(obj)
                if obj.TypeId == "Image::ImagePlane":
                    data["image_png_sha256"] = hashlib.sha256(
                        Path(str(obj.ImageFile)).read_bytes()
                    ).hexdigest()
                if PROPERTY not in obj.PropertiesList:
                    obj.addProperty(
                        "App::PropertyString", PROPERTY, "PDF source display"
                    )
                setattr(obj, PROPERTY, json.dumps(data, sort_keys=True))
                restore_display(obj)
                if before != _state(obj):
                    raise ValueError(
                        "rectangle display ordering changed physical source geometry/text"
                    )
                records.append(dict(data, native_object=obj.Name))
            top += max(
                [
                    float(obj.Shape.BoundBox.ZLength)
                    for obj in rows
                    if getattr(obj, "Shape", None)
                ]
                + [0.0]
            )
    return records
