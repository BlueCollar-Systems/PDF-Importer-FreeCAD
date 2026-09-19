from __future__ import annotations

import math
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src")
)
import PDFGlyphFill as fill
import PDFSvgTextRenderer as renderer


def rectangle(x0, y0, x1, y1, reverse=False):
    row = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    return row[::-1] if reverse else row


@pytest.mark.parametrize(
    "rule,reverse,area,holes,internal",
    [
        ("nonzero", True, 64, 1, 0),
        ("nonzero", False, 100, 0, 1),
        ("evenodd", True, 64, 1, 0),
        ("evenodd", False, 64, 1, 0),
    ],
)
def test_donut_preserves_actual_winding_rule(rule, reverse, area, holes, internal):
    proof = fill.classify_contours(
        [rectangle(0, 0, 10, 10), rectangle(2, 2, 8, 8, reverse)], rule
    )
    assert proof["area"] == area
    assert sum(len(row["holes"]) for row in proof["faces"]) == holes
    assert proof["roles"].count("internal") == internal
    assert proof["edge_count"] == 8


def test_two_counters_disconnected_dot_and_nested_island():
    b = fill.classify_contours(
        [
            rectangle(0, 0, 10, 20),
            rectangle(2, 2, 8, 8, True),
            rectangle(2, 12, 8, 18, True),
        ]
    )
    assert b["area"] == 128 and b["faces"][0]["holes"] == [1, 2]
    i = fill.classify_contours([rectangle(0, 0, 2, 8), rectangle(0, 10, 2, 12)])
    assert i["area"] == 20 and len(i["faces"]) == 2
    island = fill.classify_contours(
        [rectangle(0, 0, 10, 10), rectangle(2, 2, 8, 8, True), rectangle(4, 4, 6, 6)]
    )
    assert island["area"] == 68 and len(island["faces"]) == 2


@pytest.mark.parametrize("matrix", [(-2, 0, 0, 3), (1, 0.5, 0.2, 2), (0, -2, 3, 0)])
def test_reflection_shear_rotation_preserve_counters_and_determinant_area(matrix):
    a, b, c, d = matrix
    source = [rectangle(0, 0, 10, 10), rectangle(2, 2, 8, 8, True)]
    transformed = [
        [(a * x + b * y + 13, c * x + d * y - 9) for x, y in row] for row in source
    ]
    proof = fill.classify_contours(transformed)
    assert proof["area"] == pytest.approx(64 * abs(a * d - b * c))
    assert len(proof["faces"]) == 1 and len(proof["faces"][0]["holes"]) == 1


@pytest.mark.parametrize(
    "contours",
    [
        [[(0, 0), (3, 0), (3, 3)]],
        [[(0, 0), (4, 4), (0, 4), (4, 0), (0, 0)]],
        [rectangle(0, 0, 4, 4), rectangle(2, 2, 6, 6)],
        [rectangle(0, 0, 4, 4), rectangle(4, 0, 6, 2)],
        [[(0, 0), (4, 0), (2, 0), (2, 2), (0, 0)]],
        [[(0, 0), (math.nan, 0), (2, 2), (0, 0)]],
        [],
    ],
)
def test_invalid_open_crossing_touching_or_backtracking_contours_fail(contours):
    with pytest.raises(ValueError):
        fill.classify_contours(contours)


class Vector:
    def __init__(self, x=0, y=0, z=0):
        self.x, self.y, self.z = x, y, z

    def distanceToPoint(self, other):
        return math.sqrt(
            (self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2
        )


def test_strict_parser_retains_tiny_segments_and_all_contours(monkeypatch):
    monkeypatch.setattr(renderer, "Vector", Vector)
    contours = []
    renderer._svg_path_to_edges(
        "M0 0L.00001 0L.00001 1L0 1Z M2 0L3 0L3 1Z", 1, contours=contours
    )
    assert len(contours) == 2
    assert contours[0][1] == (0.00001, 0)
    assert fill.classify_contours(contours)["edge_count"] == 7
    with pytest.raises(ValueError, match="not closed"):
        renderer._svg_path_to_edges("M0 0L1 0L1 1", 1, contours=[])
    with pytest.raises(ValueError, match="incomplete"):
        renderer._svg_path_to_edges("M0 0 Q1", 1, contours=[])


def svg(body, definitions=""):
    return (
        '<svg><defs><path id="glyph-1" d="M0 0L1 0L1 1Z"/>'
        + definitions
        + "</defs>"
        + body
        + "</svg>"
    )


def test_paint_binding_preserves_inherited_and_overridden_fill_rule():
    s = svg(
        '<g fill-rule="evenodd"><use href="#glyph-1"/><use href="#glyph-1" fill-rule="nonzero"/></g>'
    )
    assert fill.placement_fill_rules(s, ["glyph-1", "glyph-1"]) == [
        "evenodd",
        "nonzero",
    ]
    with pytest.raises(ValueError, match="occurrence"):
        fill.placement_fill_rules(s, ["glyph-1"])


@pytest.mark.parametrize(
    "attribute",
    [
        'opacity=".5"',
        'display="none"',
        'visibility="hidden"',
        'filter="url(#f)"',
        'mask="url(#m)"',
        'transform="translate(1,0)"',
    ],
)
def test_unsupported_ancestor_paint_is_not_certified(attribute):
    with pytest.raises(ValueError):
        fill.placement_fill_rules(
            svg(f'<g {attribute}><use href="#glyph-1"/></g>'), ["glyph-1"]
        )


def test_original_rectangular_clip_needs_all_contour_points_inside():
    s = svg(
        '<g clip-path="url(#c)"><use href="#glyph-1"/></g>',
        '<clipPath id="c"><path d="M0 0H2V2H0Z" transform="matrix(0,1,-1,0,4,2)"/></clipPath>',
    )
    with pytest.raises(ValueError, match="containment"):
        fill.placement_fill_rules(s, ["glyph-1"])
    clips = fill.placement_fill_rules(s, ["glyph-1"], with_clips=True)[0]["clips"]
    fill.verify_clip_containment([rectangle(2.1, 2.1, 3.9, 3.9)], clips)
    with pytest.raises(ValueError, match="cross"):
        fill.verify_clip_containment([rectangle(1.9, 2.1, 3.9, 3.9)], clips)


@pytest.mark.parametrize(
    "location",
    [
        "use-attribute",
        "use-style",
        "use-style-spaced",
        "definition-path",
        "definition-group",
    ],
)
def test_local_clip_cannot_be_misread_as_a_global_page_clip(location):
    path = '<path id="glyph-1" d="M0 0H2V2H0Z"/>'
    attr = ""
    if location == "use-attribute":
        attr = 'clip-path="url(#c)"'
    elif location == "use-style":
        attr = 'style="clip-path:url(#c)"'
    elif location == "use-style-spaced":
        attr = 'style="clip-path : url(#c)"'
    elif location == "definition-path":
        path = path.replace(" d=", ' clip-path="url(#c)" d=')
    else:
        path = '<g id="glyph-1" clip-path="url(#c)"><path d="M0 0H2V2H0Z"/></g>'
    value = (
        "<svg><defs>"
        + path
        + '<clipPath id="c"><path d="M100 0H110V10H100Z"/></clipPath></defs>'
        '<use href="#glyph-1" transform="translate(100,0)" ' + attr + "/></svg>"
    )
    # Glyph ink is x100..102 in page space. The local clip moves to x200..210;
    # treating its untransformed x100..110 as global would falsely approve ink.
    with pytest.raises(ValueError):
        fill.placement_fill_rules(value, ["glyph-1"], with_clips=True)
    deferred = fill.placement_fill_rules(
        value, ["glyph-1"], with_clips=True, defer_errors=True
    )
    assert deferred[0]["error"]


def test_translated_glyph_keeps_inherited_global_page_clip():
    value = (
        '<svg><defs><path id="glyph-1" d="M0 0H2V2H0Z"/>'
        '<clipPath id="c"><path d="M100 0H110V10H100Z"/></clipPath></defs>'
        '<g clip-path="url(#c)"><use href="#glyph-1" transform="translate(100,0)"/></g></svg>'
    )
    paint = fill.placement_fill_rules(value, ["glyph-1"], with_clips=True)[0]
    fill.verify_clip_containment([rectangle(100, 0, 102, 2)], paint["clips"])


def test_spaced_css_opacity_cannot_bypass_source_paint_guard():
    with pytest.raises(ValueError):
        fill.placement_fill_rules(
            svg('<g style="opacity : .5"><use href="#glyph-1"/></g>'), ["glyph-1"]
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "root-transform",
        "duplicate-id",
        "stylesheet",
        "blend-style",
        "clip-mask",
        "clip-hidden",
    ],
)
def test_unrepresented_svg_effects_cannot_certify_filled_glyphs(mutation):
    value = svg(
        '<g clip-path="url(#c)"><use href="#glyph-1"/></g>',
        '<clipPath id="c"><path d="M0 0H2V2H0Z"/></clipPath>',
    )
    if mutation == "root-transform":
        value = value.replace("<svg>", '<svg transform="translate(10,0)">')
    elif mutation == "duplicate-id":
        value = value.replace(
            "</defs>", '<path id="glyph-1" d="M0 0L2 0L2 2Z"/></defs>'
        )
    elif mutation == "stylesheet":
        value = value.replace("</defs>", "<style>use {fill:none}</style></defs>")
    elif mutation == "blend-style":
        value = value.replace("<g ", '<g style="mix-blend-mode:multiply" ')
    elif mutation == "clip-mask":
        value = value.replace('<clipPath id="c">', '<clipPath id="c" mask="url(#m)">')
    elif mutation == "clip-hidden":
        value = value.replace('d="M0 0H2V2H0Z"', 'd="M0 0H2V2H0Z" display="none"')
    with pytest.raises(ValueError):
        fill.placement_fill_rules(value, ["glyph-1"], with_clips=True)


def native_shape(proof):
    edges = [
        NS(Vertexes=[NS(Point=Vector(*a)), NS(Point=Vector(*b))])
        for row in proof["contours"]
        for a, b in zip(row, row[1:], strict=False)
    ]
    return NS(
        Faces=[
            NS(Wires=[object()] * (1 + len(row["holes"]))) for row in proof["faces"]
        ],
        Edges=edges,
        Area=proof["area"],
        isNull=lambda: False,
        isValid=lambda: True,
    )


@pytest.mark.parametrize(
    "mutation", ["drop-face", "fill-hole", "wrong-area", "xy", "z", "connectivity"]
)
def test_assigned_native_shape_cannot_lose_holes_faces_or_coordinates(mutation):
    proof = fill.classify_contours(
        [rectangle(0, 0, 10, 10), rectangle(2, 2, 8, 8, True)]
    )
    shape = native_shape(proof)
    fill.verify_shape(shape, proof)
    if mutation == "drop-face":
        shape.Faces = []
    elif mutation == "fill-hole":
        shape.Faces[0].Wires = [object()]
    elif mutation == "wrong-area":
        shape.Area = 100
    elif mutation == "xy":
        shape.Edges[0].Vertexes[0].Point.x = 0.2
    elif mutation == "z":
        for edge in shape.Edges:
            for v in edge.Vertexes:
                v.Point.z = 0.2
    elif mutation == "connectivity":
        shape.Edges[0].Vertexes[0], shape.Edges[2].Vertexes[0] = (
            shape.Edges[2].Vertexes[0],
            shape.Edges[0].Vertexes[0],
        )
    with pytest.raises(ValueError):
        fill.verify_shape(shape, proof)


def test_failed_face_maker_never_falls_back_to_simple():
    calls = []

    def fail(wires, maker):
        calls.append(maker)
        raise RuntimeError("kernel failed")

    part = NS(
        Wire=lambda e: e,
        LineSegment=lambda a, b: NS(toShape=lambda: NS()),
        makeFace=fail,
    )
    with pytest.raises(RuntimeError, match="kernel failed"):
        fill.build_shape(
            [rectangle(0, 0, 10, 10), rectangle(2, 2, 8, 8, True)],
            "nonzero",
            part,
            Vector,
        )
    assert calls == ["Part::FaceMakerBullseye"]


class KernelShape:
    """Topology-bearing double; production FreeCAD face behavior needs native QA."""

    def __init__(self, edges=(), faces=(), wires=(), area=0, children=()):
        self.Edges, self.Faces, self.Wires = list(edges), list(faces), list(wires)
        self.Area, self._children = area, list(children)
        points = [v.Point for edge in self.Edges for v in edge.Vertexes]
        self.BoundBox = NS(
            XMin=min(p.x for p in points),
            XMax=max(p.x for p in points),
            YMin=min(p.y for p in points),
            YMax=max(p.y for p in points),
        )

    def isNull(self):
        return False

    def isValid(self):
        return True

    def childShapes(self):
        return self._children

    def translated(self, vector):
        edges = [
            KernelEdge(
                Vector(
                    v[0].Point.x + vector.x,
                    v[0].Point.y + vector.y,
                    v[0].Point.z + vector.z,
                ),
                Vector(
                    v[1].Point.x + vector.x,
                    v[1].Point.y + vector.y,
                    v[1].Point.z + vector.z,
                ),
            )
            for edge in self.Edges
            for v in [edge.Vertexes]
        ]
        return KernelShape(
            edges,
            self.Faces,
            self.Wires,
            self.Area,
            [child.translated(vector) for child in self._children],
        )


class KernelEdge:
    def __init__(self, a, b):
        self.Vertexes = [NS(Point=a), NS(Point=b)]

    def isNull(self):
        return False

    def toShape(self):
        return self

    def translated(self, vector):
        return KernelEdge(
            *[
                Vector(v.Point.x + vector.x, v.Point.y + vector.y, v.Point.z + vector.z)
                for v in self.Vertexes
            ]
        )


class KernelPart:
    LineSegment = KernelEdge

    @staticmethod
    def Wire(edges):
        return KernelShape(edges)

    @staticmethod
    def makeFace(wires, maker):
        assert maker == "Part::FaceMakerBullseye"
        areas = [
            abs(
                sum(
                    e.Vertexes[0].Point.x * e.Vertexes[1].Point.y
                    - e.Vertexes[1].Point.x * e.Vertexes[0].Point.y
                    for e in wire.Edges
                )
            )
            / 2
            for wire in wires
        ]
        return KernelShape(
            [e for wire in wires for e in wire.Edges],
            [NS(Wires=wires)],
            wires,
            areas[0] - sum(areas[1:]),
        )

    @staticmethod
    def makeCompound(children):
        children = list(children)
        return KernelShape(
            [e for child in children for e in getattr(child, "Edges", [child])],
            [f for child in children for f in getattr(child, "Faces", [])],
            area=sum(getattr(child, "Area", 0) for child in children),
            children=children,
        )


def install_topology_renderer(monkeypatch):
    import test_svg_text_representation_fc as existing
    from PDFVectorImporter.src import PDFGlyphFill as actual_fill

    module = existing.renderer
    parser, paints = module._svg_path_to_edges, actual_fill.placement_fill_rules
    existing._install_renderer(monkeypatch)
    monkeypatch.setattr(module, "_svg_path_to_edges", parser)
    monkeypatch.setattr(actual_fill, "placement_fill_rules", paints)
    monkeypatch.setattr(module, "Part", KernelPart)
    monkeypatch.setattr(module, "Vector", Vector)
    return existing, module


def test_glyphs_then_geometry_keep_distinct_cached_native_topology(monkeypatch):
    from PDFVectorImporter.src import PDFStyleRestore as style

    existing, module = install_topology_renderer(monkeypatch)
    source = (
        '<svg width="100" height="100" viewBox="0 0 100 100"><defs>'
        '<path id="glyph-O" d="M0 0H10V10H0Z M2 2V8H8V2Z"/></defs>'
        '<use href="#glyph-O" x="10" y="20"/></svg>'
    )
    monkeypatch.setattr(module, "_render_svg_with_pymupdf", lambda *_: source)
    cache = {}
    groups = []
    for representation in ("glyphs", "geometry"):
        group = existing.FakeGroup()
        groups.append(group)
        result = module.render_text(
            "fixture.pdf",
            1,
            100,
            1,
            page_w=100,
            fc_doc=existing.FakeDocument(),
            parent_group=group,
            representation=representation,
            render_cache=cache,
        )
        assert result["outcome"] == "verified"
    filled = groups[0].objects[0]
    assert len(filled.Shape.Faces) == 1 and filled.Shape.Area == 64
    assert len(filled.Shape.Faces[0].Wires) == 2
    assert json.loads(filled.PDFGlyphFillJSON)["glyphs"][0]["hole_count"] == 1
    assert style.has_source_glyph_fill(filled)
    assert all(not obj.Shape.Faces for obj in groups[1].objects)
    assert all(not style.has_source_glyph_fill(obj) for obj in groups[1].objects)
    assert cache["glyph_shapes"]["glyph-O"].Faces == []
    assert (
        len(cache["filled_glyph_prototypes_v1"][("glyph-O", "nonzero")][0].Faces) == 1
    )


@pytest.mark.parametrize("effect", ['opacity=".5"', 'clip-path="url(#curved)"'])
def test_unrelated_unsupported_second_item_does_not_block_first(monkeypatch, effect):
    existing, module = install_topology_renderer(monkeypatch)
    source = (
        '<svg width="100" height="100" viewBox="0 0 100 100"><defs>'
        '<path id="glyph-A" d="M0 0H5V5H0Z"/>'
        '<clipPath id="curved"><path d="M0 0Q5 5 10 0Z"/></clipPath></defs>'
        '<use href="#glyph-A" x="10" y="20"/>'
        '<use href="#glyph-A" x="30" y="20" ' + effect + "/></svg>"
    )
    monkeypatch.setattr(module, "_render_svg_with_pymupdf", lambda *_: source)
    cache = {}
    doc = existing.FakeDocument()
    group = existing.FakeGroup()
    kwargs = dict(
        page_w=100,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        requested_representation="glyphs",
        render_cache=cache,
    )
    first = existing._source_item(
        bbox=(8.0, 18.0, 18.0, 27.0), source_item_id="p1:b0:l0:s0"
    )
    second = existing._source_item(
        bbox=(28.0, 18.0, 38.0, 27.0), source_item_id="p1:b0:l0:s1"
    )
    result = module.render_text("fixture.pdf", 1, 100, 1, source_item=first, **kwargs)
    assert result["outcome"] == "verified" and len(group.objects[0].Shape.Faces) == 1
    first_shape = group.objects[0].Shape
    with pytest.raises(module.TextRepresentationRenderError) as error:
        module.render_text("fixture.pdf", 1, 100, 1, source_item=second, **kwargs)
    assert (
        "source glyph paint qualification failed" in error.value.evidence["exception"]
    )
    assert group.objects[0].Shape is first_shape
    assert len(doc.Objects) == 2 and cache["claimed_placement_indices"] == {0}


def test_saved_fill_contract_restores_source_ink_but_geometry_stays_edges():
    import PDFStyleRestore as style

    proof = json.dumps(
        dict(
            schema="source-svg-glyph-fill/1",
            glyphs=[dict(fill_rule="nonzero", face_count=1, area=64, hole_count=1)],
        )
    )
    for representation, filled, expected_mode in [
        ("glyphs", True, "Shaded"),
        ("glyphs", False, "Flat Lines"),
        ("geometry", True, "Flat Lines"),
    ]:
        material = NS(
            DiffuseColor=(0.8, 0.8, 0.8),
            AmbientColor=(0.3, 0.3, 0.3),
            SpecularColor=(1, 1, 1),
            EmissiveColor=(0, 0, 0),
            Shininess=80.0,
        )
        view = NS(
            DisplayMode="Flat Lines",
            ShapeAppearance=[material],
            LineColor=(0, 0, 0),
            LineWidth=3.0,
        )
        obj = NS(
            PDFGlyphFillJSON=proof if filled else "", PDFTextColorRGB="0.2,0.3,0.4"
        )
        style._restore_text(obj, view, representation)
        assert view.DisplayMode == expected_mode
        if expected_mode == "Shaded":
            assert material.EmissiveColor == (0.2, 0.3, 0.4)
            assert material.DiffuseColor == (0, 0, 0)
        assert view.LineWidth == 1
        assert not style._restore_text(obj, view, representation)
