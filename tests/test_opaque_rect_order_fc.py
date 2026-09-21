from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src")
)
import PDFOpaqueRectOrderProof as proof
import PDFOpaqueRectOrder as consumer


def test_svg_rectangle_unique_clip_identity_keeps_full_containment_proof():
    svg = (
        '<svg><defs><clipPath id="c"><path d="M0 0H100V100H0Z"/>'
        '</clipPath></defs><g clip-path="url(#c)">'
        '<path d="M10 10H20V20H10Z" fill="#ffffff"/></g></svg>'
    )
    rows = proof._svg_rects(svg)
    assert len(rows) == 1 and rows[0]["full_clip"] is True
    assert rows[0]["bounds"] == (10.0, 10.0, 20.0, 20.0)


def test_svg_rectangle_duplicate_clip_identity_cannot_choose_last_larger_clip():
    svg = (
        '<svg><defs><clipPath id="c"><path d="M0 0H1V1H0Z"/></clipPath>'
        '<clipPath id="c"><path d="M0 0H100V100H0Z"/></clipPath></defs>'
        '<g clip-path="url(#c)"><path d="M10 10H20V20H10Z" '
        'fill="#ffffff"/></g></svg>'
    )
    assert proof._svg_rects(svg) == []


@pytest.mark.parametrize(
    "viewport",
    [
        'x="100" y="100" width="10" height="10"',
        'viewBox="0 0 100 100" width="10" height="10"',
    ],
)
def test_svg_rectangle_nested_viewport_is_not_treated_as_page_coordinates(viewport):
    svg = (
        "<svg><svg " + viewport + '><path d="M10 10H20V20H10Z" '
        'fill="#ffffff"/></svg></svg>'
    )
    assert proof._svg_rects(svg) == []


@pytest.mark.parametrize(
    "style",
    [
        "<style>path { fill-opacity: 0.5; }</style>",
        "<defs><style>path { display: none; }</style></defs>",
    ],
)
def test_svg_document_styles_cannot_override_certified_opaque_rectangles(style):
    svg = "<svg>" + style + '<path d="M10 10H20V20H10Z" fill="#ffffff"/></svg>'
    assert proof._svg_rects(svg) == []


def test_svg_stylesheet_instruction_is_not_discarded_before_qualification():
    svg = (
        '<?xml-stylesheet type="text/css" href="paint.css"?>'
        '<svg><path d="M10 10H20V20H10Z" fill="#ffffff"/></svg>'
    )
    assert proof._svg_rects(svg) == []


def source(*, stroke=True, later="text", opacity=1, clip=False):
    fitz = pytest.importorskip("pymupdf")
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.draw_line((10, 10), (180, 150), color=(0, 0, 0), width=1)
    page.draw_rect(
        (20, 20, 160, 100),
        color=(1, 0, 0) if stroke else None,
        fill=(1, 1, 1),
        fill_opacity=opacity,
        width=1,
    )
    page.insert_text((35, 45), "CODE COMPLIANCE", fontsize=10)
    page.insert_text((35, 65), "FIELD APPROVAL", fontsize=10)
    if later == "line":
        page.draw_line((30, 25), (140, 80), color=(0, 0, 0))
    elif later == "image":
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 2, 2))
        pix.clear_with(255)
        page.insert_image((40, 50, 60, 70), pixmap=pix)
    if clip:
        # Exact source clipping of the rectangle must fail full-footprint proof.
        streams = page.get_contents()
        doc.update_stream(
            streams[1],
            b"q 30 110 100 65 re W n\n" + doc.xref_stream(streams[1]) + b"\nQ",
        )
    return doc, page


@pytest.mark.parametrize("stroke", [False, True])
def test_original_opaque_rectangle_and_all_later_text_have_unique_ownership(stroke):
    doc, page = source(stroke=stroke)
    plans = proof.plan_opaque_rectangles(page, "a" * 64)
    assert len(plans) == 1
    plan = plans[0]
    assert plan["source_bbox_pdf"] == [20, 20, 160, 100]
    assert plan["stroke_rgb"] == ([1, 0, 0] if stroke else None)
    assert [
        item["text"] for trace in plan["later_text"] for item in trace["items"]
    ] == ["CODE COMPLIANCE", "FIELD APPROVAL"]
    assert plan["source_paint_order"] < min(
        trace["source_paint_order"] for trace in plan["later_text"]
    )
    doc.close()


@pytest.mark.parametrize(
    "kwargs", [{"opacity": 0.5}, {"later": "line"}, {"later": "image"}, {"clip": True}]
)
def test_partial_translucent_or_unknown_later_overlap_is_not_reordered(kwargs):
    doc, page = source(**kwargs)
    assert proof.plan_opaque_rectangles(page, "a" * 64) == []
    doc.close()


def test_trace_partition_does_not_merge_or_drop_multiline_source_spans():
    trace = dict(
        type=0,
        opacity=1,
        chars=[
            (ord(c), 0, (x, y)) for c, x, y in [("A", 1, 2), ("B", 2, 2), ("C", 1, 4)]
        ],
    )
    spans = [
        dict(
            chars=[dict(c="A", origin=(1, 2)), dict(c="B", origin=(2, 2))],
            bbox=(0, 0, 3, 3),
        ),
        dict(chars=[dict(c="C", origin=(1, 4))], bbox=(0, 3, 3, 5)),
    ]
    raw = dict(
        blocks=[dict(type=0, lines=[dict(spans=[spans[0]]), dict(spans=[spans[1]])])]
    )
    rows = proof._text_items(trace, raw, 1)
    assert [r["source_item_id"] for r in rows] == ["p1:b0:l0:s0", "p1:b0:l1:s0"]
    raw["blocks"][0]["lines"].append(dict(spans=[spans[0]]))
    assert proof._text_items(trace, raw, 1) is None


def test_duplicate_source_rectangles_cannot_borrow_one_svg_occurrence():
    doc, page = source()
    original = page.get_drawings
    rows = original()
    duplicate = dict(rows[1])
    duplicate["seqno"] += 100
    page.get_drawings = lambda **kwargs: (
        original(**kwargs) if kwargs else rows + [duplicate]
    )
    assert proof.plan_opaque_rectangles(page, "a" * 64) == []
    doc.close()


def test_later_text_cannot_borrow_preceding_path_clip_after_graphics_restore():
    doc, page = source()
    page.draw_rect((0, 0, 300, 200), color=None, fill=(0, 0, 0))
    xref = page.get_contents()[-1]
    doc.update_stream(
        xref, b"q 0 200 m 5 200 l 4 195 l h W n\n" + doc.xref_stream(xref) + b"\nQ"
    )
    page.insert_text((35, 85), "LATE LABEL", fontsize=10)
    plans = proof.plan_opaque_rectangles(page, "a" * 64)
    assert len(plans) == 1
    assert [
        item["text"] for trace in plans[0]["later_text"] for item in trace["items"]
    ] == ["CODE COMPLIANCE", "FIELD APPROVAL", "LATE LABEL"]
    excluded = {p["source_paint_order"] for p in plans[0]["clip_disjoint_later_paints"]}
    final_trace = page.get_texttrace()[-1]["seqno"]
    assert final_trace not in excluded
    assert final_trace - 1 in excluded
    doc.close()


def test_live_rectangle_rejects_missing_fill_or_changed_vertices():
    points = [
        NS(x=x, y=y, z=0) for x, y in [(20, 20), (160, 20), (160, 100), (20, 100)]
    ]
    shape = NS(
        Faces=[1],
        Wires=[1],
        Edges=[1] * 4,
        Vertexes=[NS(Point=p) for p in points],
        Area=11200,
        isNull=lambda: False,
        isValid=lambda: True,
    )
    obj = NS(TypeId="Part::Feature", Shape=shape)
    plan = dict(source_quad_pdf=[(20, 20), (160, 20), (160, 100), (20, 100)])
    consumer.verify_rectangle(obj, plan, lambda p: NS(x=p[0], y=p[1], z=0))
    shape.Faces = []
    with pytest.raises(ValueError, match="face"):
        consumer.verify_rectangle(obj, plan, lambda p: NS(x=p[0], y=p[1], z=0))
    shape.Faces = [1]
    points[0].z = 0.1
    with pytest.raises(ValueError, match="vertices"):
        consumer.verify_rectangle(obj, plan, lambda p: NS(x=p[0], y=p[1], z=0))


def test_page_sized_evenodd_frame_clip_is_proven_disjoint_without_bbox_guess():
    outer = [(0, 0), (100, 0), (100, 100), (0, 100), (0, 0)]
    inner = [(5, 5), (95, 5), (95, 95), (5, 95), (5, 5)]
    clip = dict(
        even_odd=True,
        level=1,
        items=[
            ("l", a, b)
            for row in (outer, inner)
            for a, b in zip(row, row[1:], strict=False)
        ],
    )
    result = proof._clip_excludes_region(clip, (40, 40, 60, 60))
    assert result and result["winding"] == 2
    assert (
        proof._clip_excludes_region(dict(clip, even_odd=False), (40, 40, 60, 60))
        is None
    )
    assert proof._clip_excludes_region(clip, (3, 40, 8, 60)) is None
    # An entire painted island inside the requested region must not disappear.
    assert (
        proof._clip_excludes_region(
            dict(
                clip,
                items=[("l", a, b) for a, b in zip(inner, inner[1:], strict=False)],
            ),
            (-1, -1, 101, 101),
        )
        is None
    )


@pytest.fixture
def native(tmp_path):
    import pymupdf as fitz
    from test_image_paint_order_fc import Object, Shape, vector

    doc, page = source()
    page.draw_rect((180, 20, 280, 100), color=None, fill=(1, 1, 1))
    page.insert_text((195, 45), "SECOND", fontsize=10)
    path = tmp_path / "source.pdf"
    doc.save(path)
    doc.close()
    doc = fitz.open(path)
    page = doc[0]
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    plans = proof.plan_opaque_rectangles(page, sha)
    assert len(plans) == 2
    objects = []
    attempts = []
    for plan in plans:
        obj = Object("Mask" + str(plan["source_draw_order"]), "Part::Feature")
        quad = [(p[0], 200 - p[1], 0) for p in plan["source_quad_pdf"]]
        obj.Shape = Shape(quad + [quad[0]])
        obj.Shape.Faces = [1]
        obj.Shape.Wires = [1]
        obj.Shape.Vertexes = [NS(Point=vector(*p)) for p in quad]
        box = plan["source_bbox_pdf"]
        obj.Shape.Area = (box[2] - box[0]) * (box[3] - box[1])
        obj.Shape.isNull = lambda: False
        obj.Shape.isValid = lambda: True
        setattr(obj, consumer.SOURCE_PROPERTY, json.dumps(plan))
        objects.append(obj)
        for trace in plan["later_text"]:
            for item in trace["items"]:
                obj = Object("Text" + str(len(objects)), "Part::Feature")
                obj.Shape = Shape([(0, 0, 0), (1, 1, 0)])
                obj.PDFSourceItemId = item["source_item_id"]
                objects.append(obj)
                attempts.append(
                    dict(
                        source_item_id=item["source_item_id"],
                        outcome="verified",
                        final_type="glyphs",
                        created_entity_ids=[obj.Name],
                    )
                )
    older = Object("EarlierExtruded", "Part::Feature")
    older.Shape = Shape([(0, 0, 0), (1, 1, 4)])
    older.Shape.BoundBox.ZMax = 4
    objects.append(older)
    kwargs = dict(
        pdf_path=path,
        source_sha256=sha,
        objects=objects,
        attempts=attempts,
        mapper=lambda p: vector(p[0], 200 - p[1]),
    )
    yield page, plans, kwargs
    doc.close()


def test_live_order_keeps_all_physical_shapes_and_requested_text_type(
    native, monkeypatch
):
    from fake_image_view_fc import Root, Translation

    page, plans, kwargs = native
    monkeypatch.setitem(sys.modules, "pivy", NS(coin=NS(SoTranslation=Translation)))
    for obj in kwargs["objects"]:
        obj.ViewObject = NS(
            RootNode=Root(),
            DisplayMode="Flat Lines",
            ShapeAppearance=[],
            Transparency=0,
            LineColor=(1, 0, 0),
        )
    before = [consumer._state(obj) for obj in kwargs["objects"]]
    rows = consumer.apply_rectangle_order(page, plans, **kwargs)
    assert len(rows) == 5
    masks = [
        r for r in rows if r["representation_unchanged"] == "source_opaque_rectangle"
    ]
    assert masks[0]["display_offset_z_mm"] == masks[1]["display_offset_z_mm"] > 4
    for row in masks:
        assert all(
            r["display_offset_z_mm"] > row["display_offset_z_mm"]
            for r in rows
            if r["source_rectangle_order"] == row["source_rectangle_order"]
            and r not in masks
        )
    assert [consumer._state(obj) for obj in kwargs["objects"]] == before
    assert all(row["final_type"] == "glyphs" for row in kwargs["attempts"])
    assert kwargs["objects"][0].ViewObject.DisplayMode == "Flat Lines"
    for obj in kwargs["objects"]:
        if getattr(obj, consumer.PROPERTY, None):
            consumer.restore_display(obj)
            assert len(obj.ViewObject.RootNode.children) == 1


@pytest.mark.parametrize(
    "mutation",
    ["source", "proof", "face", "point", "text-owner", "missing-text", "other-owner"],
)
def test_bad_binding_fails_before_any_display_change(native, mutation):
    page, plans, kwargs = native
    if mutation == "source":
        kwargs["pdf_path"].write_bytes(b"changed")
    elif mutation == "proof":
        plans[0]["fill_rgb"] = [0, 0, 0]
    elif mutation == "face":
        kwargs["objects"][0].Shape.Faces = []
    elif mutation == "point":
        kwargs["objects"][0].Shape.Vertexes[0].Point.x += 1
    elif mutation == "text-owner":
        kwargs["objects"][1].PDFSourceItemId = "wrong"
    elif mutation == "missing-text":
        kwargs["attempts"] = []
    elif mutation == "other-owner":
        kwargs["objects"][1].PDFImageOrderDisplayJSON = "{}"
    with pytest.raises(ValueError):
        consumer.apply_rectangle_order(page, plans, **kwargs)
    assert not any(getattr(obj, consumer.PROPERTY, None) for obj in kwargs["objects"])


@pytest.mark.parametrize(
    "mutation",
    [
        "schema",
        "depth",
        "point",
        "area",
        "face",
        "placement",
        "text",
        "source-id",
        "proof",
    ],
)
def test_stale_restore_fails_before_recoloring_or_replacing_display_nodes(
    native, monkeypatch, mutation
):
    from fake_image_view_fc import Root, Translation

    page, plans, kwargs = native
    monkeypatch.setitem(sys.modules, "pivy", NS(coin=NS(SoTranslation=Translation)))
    for obj in kwargs["objects"]:
        obj.ViewObject = NS(
            RootNode=Root(),
            DisplayMode="Flat Lines",
            ShapeAppearance=[],
            Transparency=0,
            LineColor=(1, 0, 0),
        )
    consumer.apply_rectangle_order(page, plans, **kwargs)
    obj = kwargs["objects"][0]
    data = json.loads(getattr(obj, consumer.PROPERTY))
    if mutation == "schema":
        data["schema"] = "wrong"
    elif mutation == "depth":
        data["display_offset_z_mm"] = 0
    elif mutation == "point":
        obj.Shape.Vertexes[0].Point.x += 1
    elif mutation == "area":
        obj.Shape.Area += 1
    elif mutation == "face":
        obj.Shape.Faces = []
    elif mutation == "placement":
        obj.Placement.toMatrix = lambda: NS(A=[2] * 16)
    elif mutation == "text":
        obj.Text = ["User edit"]
    elif mutation == "source-id":
        obj.PDFSourceItemId = "wrong"
    elif mutation == "proof":
        data["source_proof"]["fill_rgb"] = [0, 0, 0]
    setattr(obj, consumer.PROPERTY, json.dumps(data))
    view = obj.ViewObject
    view.DisplayMode = "User display"
    view.Transparency = 61
    view.LineColor = (0, 0, 1)
    before = dict(vars(view))
    nodes = list(view.RootNode.children)
    with pytest.raises(ValueError):
        consumer.restore_display(obj)
    assert vars(view) == before and view.RootNode.children == nodes


def test_closure_exclusion_ledger_is_bound_to_final_expanded_region():
    doc, page = source()
    # Large bboxlog stroke bound overlaps the mask, but its exact stroked
    # rectangle remains far away. Its tiny clip will be inside the text-expanded
    # closure and therefore cannot remain a clip-disjoint certificate.
    page.draw_rect((250, 30, 260, 50), color=(0, 0, 0), width=20)
    xref = page.get_contents()[-1]
    doc.update_stream(
        xref,
        b"q 170 155 m 175 155 l 174 150 l h W n\n" + doc.xref_stream(xref) + b"\nQ",
    )
    clipped_seq = page.get_drawings()[-1]["seqno"]
    page.insert_text((35, 45), "A" * 24, fontsize=10)
    plans = proof.plan_opaque_rectangles(page, "a" * 64)
    assert len(plans) == 1 and plans[0]["paint_bounds_pdf"][2] > 175
    assert clipped_seq not in [
        r["source_paint_order"] for r in plans[0]["clip_disjoint_later_paints"]
    ]
    assert all(
        p["region_pdf"] == plans[0]["paint_bounds_pdf"]
        for row in plans[0]["clip_disjoint_later_paints"]
        for p in row["source_clip_proofs"]
    )
    doc.close()
