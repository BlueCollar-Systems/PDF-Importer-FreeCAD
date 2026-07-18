from __future__ import annotations

import hashlib
import sys
import re
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "PDFVectorImporter" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from PDFVectorImporter.src import PDFSvgTextRenderer as renderer  # noqa: E402
from PDFVectorImporter.src import PDFImporterCore as core  # noqa: E402


SVG = """
<svg width="100" height="100" viewBox="0 0 100 100">
  <defs><path id="glyph-1" d="M 0 0 L 5 0 L 5 5 Z" /></defs>
  <use href="#glyph-1" x="10" y="20" />
  <use href="#glyph-1" x="30" y="20" />
</svg>
"""

PARTIAL_NONEMPTY_SVG = """
<svg width="100" height="100" viewBox="0 0 100 100">
  <defs>
    <path id="glyph-good" d="M 0 0 L 5 0 L 5 5 Z" />
    <path id="glyph-bad" d="M 0 0 Q" />
  </defs>
  <use href="#glyph-good" x="10" y="20" />
  <use href="#glyph-bad" x="30" y="20" />
</svg>
"""

SPACE_GLYPH_SVG = """
<svg width="100" height="100" viewBox="0 0 100 100">
  <defs>
    <path id="glyph-good" d="M 0 0 L 5 0 L 5 5 Z" />
    <path id="glyph-space" d="" />
  </defs>
  <use href="#glyph-good" x="10" y="20" />
  <use href="#glyph-space" x="30" y="20" />
</svg>
"""

DUPLICATE_BBOX_SVG = """
<svg width="100" height="100" viewBox="0 0 100 100">
  <defs><path id="glyph-1" d="M 0 0 L 3 0 L 3 5 Z" /></defs>
  <use href="#glyph-1" x="10" y="20" />
  <use href="#glyph-1" x="15" y="20" />
  <use href="#glyph-1" x="10" y="20" />
  <use href="#glyph-1" x="15" y="20" />
</svg>
"""

THREE_TIED_VISIBLE_GLYPHS_SVG = """
<svg width="100" height="100" viewBox="0 0 100 100">
  <defs><path id="glyph-visible" d="M 0 0 L 3 0 L 3 5 Z" /></defs>
  <use href="#glyph-visible" x="10" y="20" />
  <use href="#glyph-visible" x="15" y="20" />
  <use href="#glyph-visible" x="20" y="20" />
</svg>
"""


class FakeVector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


class FakeEdge:
    def __init__(self, token):
        self.token = token

    def isNull(self):
        return False

    @property
    def Edges(self):
        return [self]


class FakeShape:
    def __init__(self, edges, bounds=(0.0, -5.0, 5.0, 0.0)):
        self.Edges = list(edges)
        self._bounds = tuple(float(value) for value in bounds)
        self.BoundBox = SimpleNamespace(
            XMin=self._bounds[0],
            YMin=self._bounds[1],
            XMax=self._bounds[2],
            YMax=self._bounds[3],
        )

    def isNull(self):
        return False

    def translated(self, vector):
        x0, y0, x1, y1 = self._bounds
        return FakeShape(
            [FakeEdge((edge.token, vector.x, vector.y)) for edge in self.Edges],
            (
                x0 + vector.x,
                y0 + vector.y,
                x1 + vector.x,
                y1 + vector.y,
            ),
        )

    def transformGeometry(self, matrix):
        x0, y0, x1, y1 = self._bounds
        transformed = []
        for x, y in ((x0, y0), (x0, y1), (x1, y0), (x1, y1)):
            transformed.append((
                matrix.A11 * x + matrix.A12 * y + matrix.A14,
                matrix.A21 * x + matrix.A22 * y + matrix.A24,
            ))
        xs = [point[0] for point in transformed]
        ys = [point[1] for point in transformed]
        return FakeShape(
            list(self.Edges),
            (min(xs), min(ys), max(xs), max(ys)),
        )


class FakeMatrix:
    def __init__(self):
        for row in range(1, 5):
            for column in range(1, 5):
                setattr(self, f"A{row}{column}", 0.0)


class FakeConsole:
    @staticmethod
    def PrintMessage(_message):
        return None

    @staticmethod
    def PrintWarning(_message):
        return None


class FakeFreeCAD:
    Matrix = FakeMatrix
    Console = FakeConsole()


class FakePart:
    @staticmethod
    def makeCompound(edges):
        return FakeShape(edges)


class SmallGlyphPart:
    @staticmethod
    def makeCompound(edges):
        return FakeShape(edges, bounds=(0.0, -0.75, 0.75, 0.0))


class FakeFeature:
    def __init__(self, name):
        self._name = name
        self._deleted = False
        self.Label = name
        self.TypeId = "Part::Feature"
        self.Shape = None
        self.ViewObject = SimpleNamespace()

    @property
    def Name(self):
        if self._deleted:
            raise RuntimeError("Cannot access attribute 'Name' of deleted object")
        return self._name

    def addProperty(self, _kind, name, _group):
        setattr(self, name, "")


class FakeDocument:
    def __init__(self, fail_after=None):
        self.Objects = [SimpleNamespace(Name="UserObject", TypeId="Part::Feature")]
        self.fail_after = fail_after
        self.created = 0
        self.removed = []

    def addObject(self, kind, name):
        assert kind == "Part::Feature"
        if self.fail_after is not None and self.created >= self.fail_after:
            raise RuntimeError("synthetic host insertion failure")
        obj = FakeFeature(f"{name}_{self.created}")
        self.created += 1
        self.Objects.append(obj)
        return obj

    def removeObject(self, name):
        self.removed.append(name)
        survivors = []
        for obj in self.Objects:
            if obj.Name == name:
                if hasattr(obj, "_deleted"):
                    obj._deleted = True
            else:
                survivors.append(obj)
        self.Objects = survivors

    def recompute(self):
        return None


class FakeGroup:
    def __init__(self):
        self.objects = []

    def addObject(self, obj):
        self.objects.append(obj)


def _install_renderer(monkeypatch):
    monkeypatch.setattr(renderer, "FreeCAD", None)
    monkeypatch.setattr(renderer, "Part", FakePart)
    monkeypatch.setattr(renderer, "Vector", FakeVector)
    monkeypatch.setattr(renderer, "find_pdftocairo", lambda: None)
    monkeypatch.setattr(renderer, "_render_svg_with_pymupdf", lambda *_args: SVG)
    monkeypatch.setattr(
        renderer,
        "_svg_path_to_edges",
        lambda *_args: [FakeEdge("left"), FakeEdge("right")],
    )


def _attach_visible_source_ink(item):
    source_sha = "1" * 64
    usable_sha = "2" * 64
    binding = {
        "asset_id": "sha256:" + usable_sha,
        "source_xref": 1,
        "source_font_sha256": source_sha,
        "usable_font_sha256": usable_sha,
        "base_font_name": item["font_identity"]["raw_name"],
        "span_font_name": item["font_identity"]["raw_name"],
        "source_format": "ttf",
        "usable_format": "ttf",
        "source_origin": "test_exact_font",
    }
    characters = [
        {
            "authority": "exact_pdf_font_glyph_bounds",
            "character": character,
            "synthetic": False,
            "glyph_id": index + 1,
            "glyph_name": "glyph%05d" % (index + 1),
            "glyph_bounds": (0.0, 0.0, 500.0, 700.0),
            "advance_width": 600.0,
            "layout_only_zero_ink": False,
            "font_asset_binding": dict(binding),
            "source_font_sha256": source_sha,
            "usable_font_sha256": usable_sha,
            "trace_type": 0,
            "opacity": 1.0,
            "zero_visible_ink": False,
            "physically_resolved": True,
            "source_index": index,
        }
        for index, character in enumerate(item["text"])
    ]
    evidence = {
        "schema": "pdf_source_ink_evidence_v1",
        "authority": "pymupdf_rawdict_texttrace_exact_font",
        "pdf_sha256": item["pdf_sha256"],
        "page_number": item["page_number"],
        "source_item_id": item["source_item_id"],
        "source_text": item["text"],
        "source_text_sha256": hashlib.sha256(
            item["text"].encode("utf-8")
        ).hexdigest(),
        "font_identity": dict(item["font_identity"]),
        "classification": "visible_ink",
        "zero_ink_characters_layout_only": False,
        "all_characters_physically_resolved": True,
        "font_asset_bindings": [dict(binding)],
        "glyph_id_sequence": [record["glyph_id"] for record in characters],
        "characters": characters,
    }
    evidence["evidence_sha256"] = core._source_ink_evidence_digest(evidence)
    item["source_ink_evidence"] = evidence
    item["source_font_asset_bindings"] = [dict(binding)]
    item["source_glyph_id_sequence"] = list(evidence["glyph_id_sequence"])
    return item


def _source_item(*, bbox, requested_type="glyphs", source_item_id="p1:b0:l0:s0"):
    match = re.fullmatch(r"p(\d+):b(\d+):l(\d+):s(\d+)", source_item_id)
    assert match is not None
    page_number, block_index, line_index, span_index = (
        int(value) for value in match.groups()
    )
    span = {"text": "TEXT", "bbox": tuple(bbox)}
    item = {
        "importer_identity": "bluecollarsystems.freecad.pdf_vector_importer",
        "pdf_sha256": "a" * 64,
        "page_number": page_number,
        "source_item_id": source_item_id,
        "requested_type": requested_type,
        "bbox": tuple(bbox),
        "text": "TEXT",
        "font_identity": {"raw_name": "TestFont", "normalized_key": "testfont"},
        "span": span,
        "block_index": block_index,
        "line_index": line_index,
        "span_index": span_index,
    }
    return _attach_visible_source_ink(item)


def test_nonempty_unparseable_svg_placement_is_never_silently_dropped(monkeypatch):
    _install_renderer(monkeypatch)
    monkeypatch.setattr(
        renderer,
        "_render_svg_with_pymupdf",
        lambda *_args: PARTIAL_NONEMPTY_SVG,
    )
    monkeypatch.setattr(
        renderer,
        "_svg_path_to_edges",
        lambda path_d, *_args: [] if path_d.strip().endswith("Q") else [FakeEdge("ok")],
    )

    with pytest.raises(renderer.TextRepresentationRenderError) as exc_info:
        renderer.render_text(
            "fixture.pdf",
            1,
            100.0,
            1.0,
            page_w=100.0,
            fc_doc=FakeDocument(),
            parent_group=FakeGroup(),
            representation="glyphs",
        )

    assert exc_info.value.reason == "svg_item_placement_unverified"
    assert exc_info.value.evidence["failed_placement_indices"] == [1]


def test_empty_path_space_glyph_is_not_a_failed_visible_placement(monkeypatch):
    _install_renderer(monkeypatch)
    monkeypatch.setattr(
        renderer,
        "_render_svg_with_pymupdf",
        lambda *_args: SPACE_GLYPH_SVG,
    )
    monkeypatch.setattr(
        renderer,
        "_svg_path_to_edges",
        lambda path_d, *_args: [FakeEdge("ok")] if path_d.strip() else [],
    )
    doc = FakeDocument()
    group = FakeGroup()

    result = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=_source_item(bbox=(5.0, 15.0, 20.0, 30.0)),
        requested_representation="glyphs",
    )

    assert result["outcome"] == "verified"
    assert result["glyphs"] == 1
    assert result["item_filter"]["empty_placement_indices"] == [1]


def test_page_svg_cache_is_source_bound_rendered_once_and_claims_each_placement(
    monkeypatch,
):
    _install_renderer(monkeypatch)
    render_calls = []

    def render_once(*_args):
        render_calls.append(True)
        return SVG

    monkeypatch.setattr(renderer, "_render_svg_with_pymupdf", render_once)
    doc = FakeDocument()
    group = FakeGroup()
    cache = {}
    first_item = _source_item(
        bbox=(8.0, 18.0, 18.0, 27.0),
        source_item_id="p1:b0:l0:s0",
    )
    second_item = _source_item(
        bbox=(28.0, 18.0, 38.0, 27.0),
        source_item_id="p1:b0:l0:s1",
    )

    first = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=first_item,
        requested_representation="glyphs",
        render_cache=cache,
    )
    second = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=second_item,
        requested_representation="glyphs",
        render_cache=cache,
    )

    assert len(render_calls) == 1
    assert first["item_filter"]["matched_placement_indices"] == [0]
    assert second["item_filter"]["matched_placement_indices"] == [1]
    assert cache["claimed_placement_indices"] == {0, 1}


def test_global_assignment_is_stable_when_identical_items_are_rendered_out_of_order(
    monkeypatch,
):
    _install_renderer(monkeypatch)
    monkeypatch.setattr(
        renderer,
        "_render_svg_with_pymupdf",
        lambda *_args: DUPLICATE_BBOX_SVG,
    )
    first_item = _source_item(
        bbox=(8.0, 18.0, 20.0, 27.0),
        source_item_id="p1:b0:l0:s0",
    )
    second_item = _source_item(
        bbox=(8.0, 18.0, 20.0, 27.0),
        source_item_id="p1:b0:l1:s0",
    )
    first_item["text"] = first_item["span"]["text"] = "AB"
    second_item["text"] = second_item["span"]["text"] = "CD"
    manifest = [
        {
            "source_order": order,
            "source_item_id": item["source_item_id"],
            "page_number": item["page_number"],
            "pdf_sha256": item["pdf_sha256"],
            "bbox": item["bbox"],
            "text": item["text"],
        }
        for order, item in enumerate((first_item, second_item))
    ]

    doc = FakeDocument()
    group = FakeGroup()
    cache = {"source_item_manifest": manifest}
    second = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=second_item,
        requested_representation="glyphs",
        render_cache=cache,
    )
    first = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=first_item,
        requested_representation="glyphs",
        render_cache=cache,
    )

    assert first["item_filter"]["matched_placement_indices"] == [0, 1]
    assert second["item_filter"]["matched_placement_indices"] == [2, 3]
    assert cache["placement_assignments"] == {
        first_item["source_item_id"]: [0, 1],
        second_item["source_item_id"]: [2, 3],
    }
    assert cache["claimed_placement_indices"] == {0, 1, 2, 3}


def _assert_exact_text_length_controls_tied_visible_glyph_assignment(
    monkeypatch,
    second_text,
):
    _install_renderer(monkeypatch)
    monkeypatch.setattr(
        renderer,
        "_render_svg_with_pymupdf",
        lambda *_args: THREE_TIED_VISIBLE_GLYPHS_SVG,
    )
    first_item = _source_item(
        bbox=(8.0, 18.0, 25.0, 27.0),
        source_item_id="p1:b0:l0:s0",
    )
    second_item = _source_item(
        bbox=(8.0, 18.0, 25.0, 27.0),
        source_item_id="p1:b0:l1:s0",
    )
    first_item["text"] = first_item["span"]["text"] = "A"
    second_item["text"] = second_item["span"]["text"] = second_text
    _attach_visible_source_ink(first_item)
    _attach_visible_source_ink(second_item)
    manifest = [
        {
            "source_order": order,
            "source_item_id": item["source_item_id"],
            "page_number": item["page_number"],
            "pdf_sha256": item["pdf_sha256"],
            "bbox": item["bbox"],
            "text": item["text"],
        }
        for order, item in enumerate((first_item, second_item))
    ]
    doc = FakeDocument()
    group = FakeGroup()
    cache = {"source_item_manifest": manifest}

    first = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=first_item,
        requested_representation="glyphs",
        render_cache=cache,
    )
    second = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=second_item,
        requested_representation="glyphs",
        render_cache=cache,
    )

    assert first["item_filter"]["matched_placement_indices"] == [0]
    assert second["item_filter"]["matched_placement_indices"] == [1, 2]


def test_contoured_all_whitespace_glyphs_use_exact_source_length(monkeypatch):
    _assert_exact_text_length_controls_tied_visible_glyph_assignment(monkeypatch, "  ")


def test_mixed_visible_and_space_span_uses_exact_source_length(monkeypatch):
    _assert_exact_text_length_controls_tied_visible_glyph_assignment(monkeypatch, "A ")


def test_glyphs_create_one_verified_host_entity_per_placed_glyph(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()

    result = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
    )

    assert result["outcome"] == "verified"
    assert result["entity_type"] == "glyphs"
    assert result["glyphs"] == 2
    assert result["entities"] == 2
    assert len(result["created_entity_ids"]) == 2
    assert [obj.PDFSourceItemId for obj in group.objects] == ["p1:g0", "p1:g1"]
    assert all(obj.PDFRepresentation == "glyphs" for obj in group.objects)
    assert all(isinstance(obj.Shape, FakeShape) for obj in group.objects)


def test_geometry_groups_all_raw_edges_into_one_distinct_page_geometry_entity(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()

    result = renderer.render_text(
        "fixture.pdf",
        2,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="geometry",
    )

    assert result["outcome"] == "verified"
    assert result["entity_type"] == "geometry"
    assert result["glyphs"] == 2
    assert result["raw_edges"] == 4
    assert result["entities"] == 1
    assert len(group.objects) == 1
    assert all(obj.PDFRepresentation == "geometry" for obj in group.objects)
    assert isinstance(group.objects[0].Shape, FakeShape)
    assert len(group.objects[0].Shape.Edges) == 4
    assert group.objects[0].PDFSourceItemId == "p2:geometry"
    assert group.objects[0].PDFRawEdgeCount == 4
    assert group.objects[0].PDFSourceBBox == ""
    assert group.objects[0].PDFGeometryBounds == "0,-5,5,0"
    assert group.objects[0].PDFGeometryGrouping == "source_item_compound_v1"


def test_geometry_container_failure_rolls_back_only_current_attempt(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()

    class RejectingGroup(FakeGroup):
        def addObject(self, obj):
            super().addObject(obj)
            raise RuntimeError("synthetic group insertion failure")

    group = RejectingGroup()

    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        renderer.render_text(
            "fixture.pdf",
            1,
            100.0,
            1.0,
            page_w=100.0,
            fc_doc=doc,
            parent_group=group,
            representation="geometry",
        )

    assert [obj.Name for obj in doc.Objects] == ["UserObject"]
    assert caught.value.evidence["cleanup_complete"] is True
    assert caught.value.evidence["removed_entity_ids"] == [
        "Text_Geometry_p1_0",
    ]
    assert caught.value.evidence["created_entity_ids"] == [
        "Text_Geometry_p1_0",
    ]


def test_source_annotation_failure_is_not_reported_as_verified(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()

    def fail_annotation(*_args, **_kwargs):
        raise RuntimeError("synthetic annotation failure")

    monkeypatch.setattr(FakeFeature, "addProperty", fail_annotation)

    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        renderer.render_text(
            "fixture.pdf",
            1,
            100.0,
            1.0,
            page_w=100.0,
            fc_doc=doc,
            parent_group=group,
            representation="glyphs",
        )

    assert [obj.Name for obj in doc.Objects] == ["UserObject"]
    assert caught.value.reason == "host_entity_verification_failed"
    assert caught.value.evidence["cleanup_complete"] is True
    assert caught.value.evidence["created_entity_ids"] == ["Text_Glyph_p1_g0_0"]


def test_item_filter_creates_only_glyphs_intersecting_exact_source_bbox(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(bbox=(8.0, 18.0, 18.0, 27.0))

    result = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=item,
        requested_representation="glyphs",
    )

    assert result["outcome"] == "verified"
    assert result["glyphs"] == 1
    assert result["entities"] == 1
    assert result["source_item_id"] == item["source_item_id"]
    assert result["item_filter"]["matched_placement_indices"] == [0]
    assert [obj.PDFParentSourceItemId for obj in group.objects] == [
        item["source_item_id"]
    ]
    assert [obj.PDFSourceItemId for obj in group.objects] == [
        item["source_item_id"] + ":g0"
    ]


def test_real_rotated_page_item_filters_never_cross_relabel(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "rotated-two-items.pdf"
    pdf_doc = fitz.open()
    page = pdf_doc.new_page(width=200.0, height=100.0)
    page.insert_text((20.0, 30.0), "ONE", fontsize=12.0)
    page.insert_text((30.0, 80.0), "TWO", fontsize=12.0)
    page.set_rotation(90)

    canonical_spans = []
    for block_index, block in enumerate(page.get_text("rawdict")["blocks"]):
        for line_index, line in enumerate(block.get("lines", [])):
            for span_index, span in enumerate(line.get("spans", [])):
                text = "".join(char["c"] for char in span.get("chars", []))
                if text in {"ONE", "TWO"}:
                    canonical_spans.append(
                        (
                            block_index,
                            line_index,
                            span_index,
                            text,
                            tuple(float(value) for value in span["bbox"]),
                        )
                    )
    page_rotation_matrix = tuple(float(value) for value in page.rotation_matrix)
    page_w = float(page.rect.width)
    page_h = float(page.rect.height)
    pdf_doc.save(str(pdf_path))
    pdf_doc.close()

    assert [entry[3] for entry in canonical_spans] == ["ONE", "TWO"]
    assert page_rotation_matrix == (0.0, 1.0, -1.0, 0.0, 100.0, 0.0)
    assert (page_w, page_h) == (100.0, 200.0)

    items = []
    for block_index, line_index, span_index, text, bbox in canonical_spans:
        source_item_id = (
            f"p1:b{block_index}:l{line_index}:s{span_index}"
        )
        item = _source_item(bbox=bbox, source_item_id=source_item_id)
        item["text"] = text
        item["span"] = {"text": text, "bbox": bbox}
        _attach_visible_source_ink(item)
        items.append(item)

    monkeypatch.setattr(renderer, "FreeCAD", FakeFreeCAD)
    monkeypatch.setattr(renderer, "Part", SmallGlyphPart)
    monkeypatch.setattr(renderer, "Vector", FakeVector)
    monkeypatch.setattr(renderer, "find_pdftocairo", lambda: None)
    monkeypatch.setattr(
        renderer,
        "_svg_path_to_edges",
        lambda *_args: [FakeEdge("outline")],
    )
    doc = FakeDocument()
    group = FakeGroup()

    results = []
    for item in items:
        opts = core.ImportOptions(text_mode="glyphs")
        opts._page_rotation_matrix = page_rotation_matrix
        results.append(
            core._deliver_text_item_svg(
                item,
                "glyphs",
                opts,
                pdf_path=str(pdf_path),
                page_h=page_h,
                page_w=page_w,
                scale=1.0,
                fc_doc=doc,
                parent_group=group,
            )
        )

    assert [
        result["evidence"]["matched_placement_indices"]
        for result in results
    ] == [[0, 1, 2], [3, 4, 5]]
    for item, result in zip(items, results, strict=True):
        assert result["source_item_id"] == item["source_item_id"]
        assert all(
            child_id.startswith(item["source_item_id"] + ":g")
            for child_id in result["evidence"]["child_source_item_ids"]
        )


def test_item_filtered_entities_keep_parent_and_unique_child_ids(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(
        bbox=(8.0, 18.0, 38.0, 27.0),
        requested_type="labels",
        source_item_id="p1:b2:l3:s4",
    )

    result = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="glyphs",
        source_item=item,
        requested_representation="labels",
    )

    child_ids = [obj.PDFSourceItemId for obj in group.objects]
    assert child_ids == [
        item["source_item_id"] + ":g0",
        item["source_item_id"] + ":g1",
    ]
    assert len(set(child_ids)) == 2
    assert all(
        obj.PDFParentSourceItemId == item["source_item_id"]
        for obj in group.objects
    )
    assert result["delivery_attempts"] == [
        {
            "source_item_id": item["source_item_id"],
            "requested_type": "labels",
            "attempted_type": "glyphs",
            "final_type": "glyphs",
            "outcome": "verified",
            "reason": "requested glyphs delivered for exact source item",
            "created_entity_ids": result["created_entity_ids"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 2,
            "evidence": {
                "renderer": "pymupdf",
                "host_entity_type": "Part::Feature",
                "raw_edge_count": None,
                    "source_item_bbox": item["bbox"],
                    "host_filter_bbox": (8.0, 73.0, 38.0, 82.0),
                    "matched_placement_indices": [0, 1],
                    "assignment_method": "bounded_greedy_item_filter",
                    "child_source_item_ids": child_ids,
            },
        }
    ]


def test_no_matching_item_glyph_output_is_unproven_terminal_failure(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(bbox=(70.0, 70.0, 80.0, 80.0))

    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        renderer.render_text(
            "fixture.pdf",
            1,
            100.0,
            1.0,
            page_w=100.0,
            fc_doc=doc,
            parent_group=group,
            representation="glyphs",
            source_item=item,
            requested_representation="glyphs",
        )

    assert caught.value.reason == "svg_item_filter_empty"
    assert caught.value.evidence["source_item_id"] == item["source_item_id"]
    assert caught.value.evidence["created_entity_ids"] == []
    assert caught.value.evidence["removed_entity_ids"] == []
    assert caught.value.evidence["cleanup_complete"] is True
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]
    assert group.objects == []


def test_geometry_item_filter_returns_one_verified_raw_edge_compound(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(
        bbox=(8.0, 18.0, 18.0, 27.0),
        requested_type="3d_text",
    )

    result = renderer.render_text(
        "fixture.pdf",
        1,
        100.0,
        1.0,
        page_w=100.0,
        fc_doc=doc,
        parent_group=group,
        representation="geometry",
        source_item=item,
        requested_representation="3d_text",
    )

    assert result["outcome"] == "verified"
    assert result["entity_type"] == "geometry"
    assert result["glyphs"] == 1
    assert result["raw_edges"] == 2
    assert result["entities"] == 1
    assert len(group.objects) == 1
    assert isinstance(group.objects[0].Shape, FakeShape)
    assert len(group.objects[0].Shape.Edges) == 2
    assert group.objects[0].PDFSourceItemId == item["source_item_id"] + ":geometry"
    assert group.objects[0].PDFRawEdgeCount == 2
    assert group.objects[0].PDFSourceBBox == "8,18,18,27"
    assert group.objects[0].PDFGeometryBounds == "0,-5,5,0"
    assert group.objects[0].PDFGeometryGrouping == "source_item_compound_v1"
    assert tuple(float(value) for value in group.objects[0].PDFSourceBBox.split(",")) == (
        8.0,
        18.0,
        18.0,
        27.0,
    )
    assert all(
        obj.PDFParentSourceItemId == item["source_item_id"]
        for obj in group.objects
    )
    attempt = result["delivery_attempts"][0]
    assert attempt["requested_type"] == "3d_text"
    assert attempt["attempted_type"] == "geometry"
    assert attempt["final_type"] == "geometry"
    assert attempt["created_entity_ids"] == result["created_entity_ids"]


def test_geometry_rechecks_compound_raw_edge_count_after_recompute(monkeypatch):
    _install_renderer(monkeypatch)

    class RecomputeCorruptsGeometry(FakeDocument):
        def recompute(self):
            for host_obj in self.Objects:
                if getattr(host_obj, "PDFRepresentation", None) == "geometry":
                    host_obj.Shape = FakeShape(
                        [FakeEdge("replacement-only")]
                    )

    doc = RecomputeCorruptsGeometry()
    group = FakeGroup()
    item = _source_item(
        bbox=(8.0, 18.0, 18.0, 27.0),
        requested_type="geometry",
    )

    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        renderer.render_text(
            "fixture.pdf",
            1,
            100.0,
            1.0,
            page_w=100.0,
            fc_doc=doc,
            parent_group=group,
            representation="geometry",
            source_item=item,
            requested_representation="geometry",
        )

    assert caught.value.reason == "host_entity_verification_failed"
    assert "raw edge count" in caught.value.evidence["exception"]
    assert caught.value.evidence["cleanup_complete"] is True
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]
    assert group.objects == []


def test_item_filter_stops_on_unverified_placement_instead_of_partial_delivery(
    monkeypatch,
):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(bbox=(8.0, 18.0, 38.0, 27.0))
    original_translated = FakeShape.translated

    def lose_first_placement(shape, vector):
        if vector.x < 20.0:
            return None
        return original_translated(shape, vector)

    monkeypatch.setattr(FakeShape, "translated", lose_first_placement)

    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        renderer.render_text(
            "fixture.pdf",
            1,
            100.0,
            1.0,
            page_w=100.0,
            fc_doc=doc,
            parent_group=group,
            representation="glyphs",
            source_item=item,
            requested_representation="glyphs",
        )

    assert caught.value.reason == "svg_item_placement_unverified"
    assert caught.value.evidence["failed_placement_indices"] == [0]
    assert caught.value.evidence["created_entity_ids"] == []
    assert caught.value.evidence["cleanup_complete"] is True
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]


def test_item_renderer_rejects_factory_baseline_return_before_mutation(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    prior = doc.Objects[0]
    item = _source_item(bbox=(8.0, 18.0, 18.0, 27.0))

    def return_prior(kind, _name):
        assert kind == "Part::Feature"
        return prior

    doc.addObject = return_prior

    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        renderer.render_text(
            "fixture.pdf",
            1,
            100.0,
            1.0,
            page_w=100.0,
            fc_doc=doc,
            parent_group=group,
            representation="glyphs",
            source_item=item,
            requested_representation="glyphs",
        )

    assert caught.value.reason == "host_entity_verification_failed"
    assert "pre-existing host object" in caught.value.evidence["exception"]
    assert caught.value.evidence["created_entity_ids"] == []
    assert caught.value.evidence["removed_entity_ids"] == []
    assert caught.value.evidence["cleanup_complete"] is True
    assert doc.Objects == [prior]
    assert group.objects == []
    assert not hasattr(prior, "Shape")
    assert not hasattr(prior, "PDFSourceItemId")


def test_core_item_svg_adapter_returns_exact_ladder_delivery_contract(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(
        bbox=(8.0, 18.0, 18.0, 27.0),
        requested_type="labels",
        source_item_id="p1:b4:l5:s6",
    )

    result = core._deliver_text_item_svg(
        item,
        "glyphs",
        core.ImportOptions(text_mode="labels"),
        pdf_path="fixture.pdf",
        page_h=100.0,
        page_w=100.0,
        scale=1.0,
        fc_doc=doc,
        parent_group=group,
    )

    assert result["source_item_id"] == item["source_item_id"]
    assert result["requested_type"] == "labels"
    assert result["attempted_type"] == "glyphs"
    assert result["final_type"] == "glyphs"
    assert result["outcome"] == "verified"
    assert result["created_entity_ids"] == [obj.Name for obj in group.objects]
    assert result["removed_entity_ids"] == []
    assert result["cleanup_complete"] is True
    assert result["evidence"]["matched_placement_indices"] == [0]


def test_core_item_svg_rejects_duplicate_live_child_identity_claims(monkeypatch):
    item = _source_item(
        bbox=(8.0, 18.0, 38.0, 27.0),
        source_item_id="p1:b4:l5:s6",
    )
    doc = FakeDocument()
    group = FakeGroup()

    def duplicate_live_ids(*_args, fc_doc, parent_group, **_kwargs):
        child_ids = [
            item["source_item_id"] + ":g0",
            item["source_item_id"] + ":g1",
        ]
        created = []
        for index in range(2):
            host_obj = fc_doc.addObject("Part::Feature", f"Duplicate_{index}")
            host_obj.PDFSourceItemId = child_ids[0]
            host_obj.PDFParentSourceItemId = item["source_item_id"]
            host_obj.PDFRepresentation = "glyphs"
            parent_group.addObject(host_obj)
            created.append(host_obj.Name)
        evidence = {
            "renderer": "adversarial",
            "host_entity_type": "Part::Feature",
            "raw_edge_count": None,
            "source_item_bbox": item["bbox"],
            "host_filter_bbox": (8.0, 73.0, 38.0, 82.0),
            "matched_placement_indices": [0, 1],
            "child_source_item_ids": child_ids,
        }
        attempt = {
            "source_item_id": item["source_item_id"],
            "requested_type": "glyphs",
            "attempted_type": "glyphs",
            "final_type": "glyphs",
            "outcome": "verified",
            "reason": "adversarial duplicate live identity claims",
            "created_entity_ids": created,
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": evidence,
        }
        return {
            "outcome": "verified",
            "glyphs": 2,
            "raw_edges": 0,
            "entities": 2,
            "entity_type": "glyphs",
            "created_entity_ids": created,
            "delivery_attempts": [attempt],
            "source_item_id": item["source_item_id"],
            "item_filter": {
                "source_item_bbox": item["bbox"],
                "host_filter_bbox": (8.0, 73.0, 38.0, 82.0),
                "matched_placement_indices": [0, 1],
            },
        }

    monkeypatch.setattr(renderer, "render_text", duplicate_live_ids)

    with pytest.raises(core.TextRepresentationFailure) as caught:
        core._deliver_text_item_svg(
            item,
            "glyphs",
            core.ImportOptions(text_mode="glyphs"),
            pdf_path="fixture.pdf",
            page_h=100.0,
            page_w=100.0,
            scale=1.0,
            fc_doc=doc,
            parent_group=group,
        )

    assert caught.value.attempt["reason"] == (
        "requested_representation_verification_failed"
    )
    assert caught.value.attempt["cleanup_complete"] is True
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]


def test_core_geometry_item_accepts_one_compound_and_counts_all_raw_edges(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(
        bbox=(8.0, 18.0, 18.0, 27.0),
        requested_type="geometry",
    )
    opts = core.ImportOptions(text_mode="geometry")

    attempt = core._deliver_text_item_svg(
        item,
        "geometry",
        opts,
        pdf_path="fixture.pdf",
        page_h=100.0,
        page_w=100.0,
        scale=1.0,
        fc_doc=doc,
        parent_group=group,
        render_cache={},
    )

    assert attempt["outcome"] == "verified"
    assert attempt["final_type"] == "geometry"
    assert attempt["delivery_count"] == 2
    assert len(attempt["created_entity_ids"]) == 1
    assert attempt["evidence"]["child_source_item_ids"] == [
        item["source_item_id"] + ":geometry"
    ]
    assert group.objects[0].PDFRawEdgeCount == 2


@pytest.mark.parametrize("attempted_type", ["glyphs", "geometry"])
def test_core_item_svg_rejects_child_identity_not_derived_from_parent(
    monkeypatch,
    attempted_type,
):
    _install_renderer(monkeypatch)
    item = _source_item(
        bbox=(8.0, 18.0, 18.0, 27.0),
        requested_type=attempted_type,
    )
    doc = FakeDocument()
    group = FakeGroup()
    real_render = renderer.render_text

    def corrupt_child_syntax(*args, **kwargs):
        result = real_render(*args, **kwargs)
        invalid_child_ids = [
            item["source_item_id"] + f":curve{index}"
            for index, _host_obj in enumerate(group.objects)
        ]
        for host_obj, invalid_child_id in zip(
            group.objects,
            invalid_child_ids,
            strict=True,
        ):
            host_obj.PDFSourceItemId = invalid_child_id
        result["delivery_attempts"][0]["evidence"][
            "child_source_item_ids"
        ] = invalid_child_ids
        return result

    monkeypatch.setattr(renderer, "render_text", corrupt_child_syntax)

    with pytest.raises(core.TextRepresentationFailure) as caught:
        core._deliver_text_item_svg(
            item,
            attempted_type,
            core.ImportOptions(text_mode=attempted_type),
            pdf_path="fixture.pdf",
            page_h=100.0,
            page_w=100.0,
            scale=1.0,
            fc_doc=doc,
            parent_group=group,
        )

    assert caught.value.attempt["reason"] == (
        "requested_representation_verification_failed"
    )
    assert caught.value.attempt["cleanup_complete"] is True
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]


def test_core_item_svg_empty_filter_is_not_impossibility_or_fallback_permission(
    monkeypatch,
):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(bbox=(70.0, 70.0, 80.0, 80.0))
    opts = core.ImportOptions(text_mode="glyphs")

    with pytest.raises(core.TextRepresentationFailure) as caught:
        core._deliver_text_item_svg(
            item,
            "glyphs",
            opts,
            pdf_path="fixture.pdf",
            page_h=100.0,
            page_w=100.0,
            scale=1.0,
            fc_doc=doc,
            parent_group=group,
        )

    assert caught.value.attempt["reason"] == "svg_item_filter_empty"
    assert caught.value.attempt["created_entity_ids"] == []
    assert caught.value.attempt["removed_entity_ids"] == []
    assert caught.value.attempt["cleanup_complete"] is True
    assert opts.text_mode_fallbacks == []
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]


def test_core_item_svg_rejects_source_id_index_mismatch_before_render(monkeypatch):
    _install_renderer(monkeypatch)
    doc = FakeDocument()
    group = FakeGroup()
    item = _source_item(bbox=(8.0, 18.0, 18.0, 27.0))
    item["block_index"] = 99

    with pytest.raises(core.TextRepresentationFailure) as caught:
        core._deliver_text_item_svg(
            item,
            "glyphs",
            core.ImportOptions(text_mode="glyphs"),
            pdf_path="fixture.pdf",
            page_h=100.0,
            page_w=100.0,
            scale=1.0,
            fc_doc=doc,
            parent_group=group,
        )

    assert caught.value.attempt["reason"] == "invalid_svg_text_source_item"
    assert caught.value.attempt["created_entity_ids"] == []
    assert caught.value.attempt["cleanup_complete"] is True
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]
    assert group.objects == []
