from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import importlib.util
import inspect
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


def _attach_visible_source_ink(item: dict) -> dict:
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
            "glyph_bounds": [0.0, 0.0, 500.0, 700.0],
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
        "source_text_sha256": hashlib.sha256(item["text"].encode("utf-8")).hexdigest(),
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


def _canonical_item(requested_type: str = "labels") -> dict:
    span = {
        "text": "A",
        "font": "ArialMT",
        "size": 12.0,
        "bbox": (10.0, 20.0, 18.0, 32.0),
        "origin": (10.0, 30.0),
        "color": 0,
    }
    item = {
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": "a" * 64,
        "page_number": 1,
        "source_item_id": "p1:b0:l0:s0",
        "requested_type": requested_type,
        "text": "A",
        "font_identity": {"raw_name": "ArialMT", "normalized_key": "arialmt"},
        "bbox": span["bbox"],
        "origin": span["origin"],
        "line_direction": (1.0, 0.0),
        "rotation_deg": 0.0,
        "span": span,
        "block_index": 0,
        "line_index": 0,
        "span_index": 0,
    }
    return _attach_visible_source_ink(item)


def _synthetic_whitespace_source(requested_type: str) -> tuple[dict, dict]:
    span = {
        "font": "Siwa-Regular",
        "size": 7.0,
        "bbox": (10.0, 20.0, 16.0, 30.0),
        "origin": (10.0, 27.0),
        "color": 0,
        "chars": [
            {
                "origin": (10.0 + index * 2.0, 27.0),
                "bbox": (10.0 + index * 2.0, 20.0, 12.0 + index * 2.0, 30.0),
                "c": " ",
                "synthetic": True,
            }
            for index in range(3)
        ],
    }
    raw_tdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [{"dir": (1.0, 0.0), "spans": [span]}],
            }
        ]
    }
    item = list(
        core._iter_text_source_items(
            raw_tdict,
            1,
            "a" * 64,
            requested_type,
        )
    )[0]
    return item, raw_tdict


class _SyntheticWhitespacePage:
    def get_texttrace(self):
        return []

    def get_fonts(self, full=True):
        assert full is True
        return []


def _physically_bind_whitespace_item(requested_type: str) -> dict:
    item, raw_tdict = _synthetic_whitespace_source(requested_type)
    [bound] = core._bind_page_text_source_ink_evidence(
        _SyntheticWhitespacePage(),
        raw_tdict,
        [item],
    )
    return bound


def test_production_page_import_has_only_canonical_text_entrypoint():
    tree = ast.parse(inspect.getsource(core._import_pdf_page_inner))
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "_render_canonical_text_items" in called_names
    assert called_names.isdisjoint({
        "_render_text_spans_exact_labels",
        "_render_text_spans_3d",
        "_render_requested_svg_text",
        "_preprocess_text_blocks",
    })


def test_legacy_text_helpers_cannot_reintroduce_whitespace_drop_roadblock():
    for helper in (
        core._render_text_spans_exact_labels,
        core._render_text_spans_3d,
    ):
        source = inspect.getsource(helper)
        assert ".isspace()" not in source
        assert "non-whitespace" not in source


def test_atomic_raster_publication_survives_concurrent_same_key_writers(tmp_path):
    destination = tmp_path / "same-source-key.png"
    barrier = threading.Barrier(2)
    # One content-addressed key may only ever name one exact payload.
    payloads = (b"same-complete-png" * 100, b"same-complete-png" * 100)

    class Pixmap:
        def __init__(self, payload):
            self.payload = payload

        def save(self, path):
            barrier.wait(timeout=5)
            Path(path).write_bytes(self.payload)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(core._save_pixmap_atomic, Pixmap(payload), destination)
            for payload in payloads
        ]
        for future in futures:
            future.result(timeout=10)

    assert destination.read_bytes() in payloads
    assert [path for path in tmp_path.iterdir() if path != destination] == []


class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


class _Rotation:
    def __init__(self, _axis=None, angle=0.0):
        self.angle = float(angle)


class _Placement:
    def __init__(self, base=None, rotation=None):
        self.Base = base or _Vector()
        self.Rotation = rotation or _Rotation()


class _View:
    def __init__(self):
        self.Visibility = False
        self.FontSize = 0.0
        self.FontName = ""
        self.Font = ""
        self.Justification = ""
        self.TextColor = (1.0, 1.0, 1.0)
        self.ShapeColor = (1.0, 1.0, 1.0)
        self.LineColor = (1.0, 1.0, 1.0)
        self.PointColor = (1.0, 1.0, 1.0)
        self.Line = True
        self.Frame = "Rectangle"
        self.ArrowTypeStart = "Dot"
        self.DisplayMode = "Screen"
        self.ScaleMultiplier = 2.0
        self.LineSpacing = 3.0
        self.TextAlignment = "Top"
        self.MaxChars = 12


class _HostObject:
    def __init__(self, document, name, type_id):
        self.Document = document
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.ViewObject = _View()
        self.Placement = _Placement()
        self.PropertiesList = []
        self._property_types = {}

    def addProperty(self, kind, name, _group):
        if name not in self.PropertiesList:
            self.PropertiesList.append(name)
        self._property_types[name] = kind

    def getTypeIdOfProperty(self, name):  # noqa: N802 - FreeCAD API
        return self._property_types.get(name, "")


class _Document:
    def __init__(self):
        self.Objects = []

    def addObject(self, kind, name):
        obj = _HostObject(self, f"{name}_{len(self.Objects)}", kind)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self, *_args):
        return None


class _Group:
    def __init__(self, document):
        self.Document = document
        self.objects = []

    def addObject(self, obj):
        if obj not in self.objects:
            self.objects.append(obj)

    def removeObject(self, obj):
        if obj in self.objects:
            self.objects.remove(obj)


class _Draft:
    def __init__(self, document):
        self.document = document

    def make_text(self, texts, placement=None):
        obj = self.document.addObject("App::FeaturePython", "PDF_Text")
        obj.Text = list(texts)
        obj.Placement = placement
        obj.Proxy = SimpleNamespace(Type="Text")
        return obj

    def make_label(
        self,
        *,
        target_point,
        placement,
        label_type,
        custom_text,
        direction,
        points=None,
    ):
        obj = self.document.addObject("App::FeaturePython", "PDF_Label")
        obj.TargetPoint = target_point
        obj.Placement = placement
        obj.LabelType = label_type
        obj.CustomText = [custom_text] if isinstance(custom_text, str) else list(custom_text)
        obj.Text = list(obj.CustomText)
        obj.StraightDirection = direction
        obj.Points = list(points or [])
        obj.Proxy = SimpleNamespace(Type="Label")
        return obj


def _install_native_host(monkeypatch):
    document = _Document()
    group = _Group(document)
    monkeypatch.setattr(core, "Draft", _Draft(document))
    monkeypatch.setattr(core, "Vector", _Vector)
    monkeypatch.setattr(core, "Rotation", _Rotation)
    monkeypatch.setattr(core, "Placement", _Placement)
    return document, group


def test_shared_text_style_metadata_does_not_add_native_visibility_by_default():
    host = _HostObject(_Document(), "ShapeString", "Part::FeaturePython")

    core._persist_text_style_metadata(
        host,
        font_name="Arial",
        font_size=12.0,
        source_color=(0.2, 0.4, 0.6),
    )

    assert "PDFTextVisibility" not in host.PropertiesList
    assert not hasattr(host, "PDFTextVisibility")


def test_all_requested_representations_have_finite_raster_terminal_ladders():
    expected = {"text", "labels", "3d_text", "glyphs", "geometry", "raster"}

    assert set(core.TEXT_ITEM_FALLBACK_LADDERS) == expected
    for requested in expected:
        ladder = core.TEXT_ITEM_FALLBACK_LADDERS[requested]
        assert ladder[0] == requested
        assert ladder[-1] == "raster"
        assert len(ladder) == len(set(ladder))
        assert set(ladder).issubset(expected)
        assert core._normalize_requested_text_type(requested) == requested


def test_closed_missing_svg_outlines_advances_exactly_one_ladder_rung():
    item = _canonical_item("glyphs")
    proof = {
        "item_specific_proven_impossible": True,
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": item["pdf_sha256"],
        "page_number": item["page_number"],
        "source_item_id": item["source_item_id"],
        "requested_type": "glyphs",
        "attempted_type": "glyphs",
        "reason_code": "svg_glyph_outlines_unavailable",
        "evidence": {
            "source_item_id": item["source_item_id"],
            "source_bbox": item["bbox"],
            "glyph_definition_count": 0,
        },
        "attempted_source_results": [
            {
                "source": "svg_item_renderer",
                "outcome": "proven_impossible",
                "reason_code": "svg_glyph_outlines_unavailable",
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "source_item_id": item["source_item_id"],
            }
        ],
        "attempted_sources_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }
    impossible_attempt = {
        "source_item_id": item["source_item_id"],
        "requested_type": "glyphs",
        "attempted_type": "glyphs",
        "final_type": None,
        "outcome": "proven_impossible",
        "reason_code": "svg_glyph_outlines_unavailable",
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }
    calls = []

    def glyphs(*_args):
        calls.append("glyphs")
        raise core.TextItemImpossible(
            "the SVG source contains no usable glyph outlines",
            attempt=impossible_attempt,
            proof=proof,
        )

    def geometry(source_item, attempted, _opts):
        calls.append("geometry")
        return {
            "source_item_id": source_item["source_item_id"],
            "requested_type": "glyphs",
            "attempted_type": attempted,
            "final_type": attempted,
            "outcome": "verified",
            "created_entity_ids": ["Geometry001"],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {"raw_edge_count": 1},
        }

    unused = lambda *_args: pytest.fail("executor skipped a proven nearer result")
    opts = core.ImportOptions(text_mode="glyphs")
    result = core._run_text_item_fallback_ladder(
        item,
        "glyphs",
        {
            "glyphs": glyphs,
            "geometry": geometry,
            "3d_text": unused,
            "text": unused,
            "labels": unused,
            "raster": unused,
        },
        opts,
    )

    assert calls == ["glyphs", "geometry"]
    assert result["final_type"] == "geometry"
    assert opts.text_mode_fallbacks[0]["source_item_ids"] == [
        item["source_item_id"]
    ]


def test_svg_item_deliverer_emits_valid_typed_impossibility_for_closed_reason(
    monkeypatch,
):
    from PDFVectorImporter.src import PDFSvgTextRenderer as renderer

    def no_glyph_outlines(*_args, **_kwargs):
        raise renderer.TextRepresentationRenderError(
            "svg_glyph_outlines_unavailable",
            {
                "glyph_definition_count": 0,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
        )

    monkeypatch.setattr(renderer, "render_text", no_glyph_outlines)
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item("glyphs")
    opts = core.ImportOptions(text_mode="glyphs")

    with pytest.raises(core.TextItemImpossible) as raised:
        core._deliver_text_item_svg(
            item,
            "glyphs",
            opts,
            pdf_path="fixture.pdf",
            page_h=100.0,
            page_w=100.0,
            scale=1.0,
            fc_doc=document,
            parent_group=group,
            render_cache={},
        )

    assert raised.value.proof["reason_code"] == "svg_glyph_outlines_unavailable"
    assert core._validate_item_impossibility_proof(
        item,
        "glyphs",
        "glyphs",
        raised.value.proof,
    )["cleanup_complete"] is True


@pytest.mark.parametrize(
    ("attempted_type", "child_suffix"),
    [("glyphs", ":g0"), ("geometry", ":geometry")],
)
def test_visible_svg_delivery_persists_physical_source_ink_on_every_host(
    monkeypatch,
    attempted_type,
    child_suffix,
):
    from PDFVectorImporter.src import PDFSvgTextRenderer as renderer

    document, group = _install_native_host(monkeypatch)
    item = _canonical_item(attempted_type)
    child_source_id = item["source_item_id"] + child_suffix

    def verified_item_render(*_args, **_kwargs):
        host = document.addObject("Part::Feature", "PDF_SVG_Text")
        core._annotate_text_host_object(host, child_source_id, attempted_type)
        host.addProperty(
            "App::PropertyString", "PDFParentSourceItemId", "PDF Import"
        )
        host.PDFParentSourceItemId = item["source_item_id"]
        group.addObject(host)
        evidence = {
            "source_item_bbox": item["bbox"],
            "matched_placement_indices": [0],
            "child_source_item_ids": [child_source_id],
        }
        return {
            "outcome": "verified",
            "entity_type": attempted_type,
            "source_item_id": item["source_item_id"],
            "entities": 1,
            "glyphs": 1 if attempted_type == "glyphs" else 0,
            "raw_edges": 1 if attempted_type == "geometry" else 0,
            "created_entity_ids": [host.Name],
            "item_filter": {"matched_placement_indices": [0]},
            "delivery_attempts": [
                {
                    "source_item_id": item["source_item_id"],
                    "requested_type": attempted_type,
                    "attempted_type": attempted_type,
                    "final_type": attempted_type,
                    "outcome": "verified",
                    "created_entity_ids": [host.Name],
                    "delivery_entity_ids": [host.Name],
                    "support_entity_ids": [],
                    "removed_entity_ids": [],
                    "cleanup_complete": True,
                    "delivery_count": 1,
                    "evidence": evidence,
                }
            ],
        }

    monkeypatch.setattr(renderer, "render_text", verified_item_render)

    result = core._deliver_text_item_svg(
        item,
        attempted_type,
        core.ImportOptions(text_mode=attempted_type, import_text=True),
        pdf_path="fixture.pdf",
        page_h=100.0,
        page_w=100.0,
        scale=1.0,
        fc_doc=document,
        parent_group=group,
        render_cache={},
    )

    assert result["outcome"] == "verified"
    assert result["delivery_count"] == 1
    assert result["evidence"]["source_ink_evidence"] == item[
        "source_ink_evidence"
    ]
    assert result["evidence"]["source_ink_evidence_persisted"] is True
    host = document.getObject(result["delivery_entity_ids"][0])
    assert host.PDFSourceInkClassification == "visible_ink"
    assert (
        host.PDFSourceInkEvidenceSHA256
        == item["source_ink_evidence"]["evidence_sha256"]
    )


def test_canonical_page_orchestrator_preserves_original_source_identity(monkeypatch):
    item = _canonical_item("labels")
    raw_tdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {
                        "dir": (1.0, 0.0),
                        "spans": [dict(item["span"])],
                    }
                ],
            }
        ]
    }
    observed = []

    def fake_executor(source_item, requested, deliverers, opts):
        observed.append((source_item["source_item_id"], requested, set(deliverers)))
        return {
            "source_item_id": source_item["source_item_id"],
            "requested_type": requested,
            "attempted_type": requested,
            "final_type": requested,
            "outcome": "verified",
            "created_entity_ids": ["Text001"],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {},
        }

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", fake_executor)
    monkeypatch.setattr(core, "_stage_page_shapestring_fonts", lambda *_a, **_k: {})
    opts = core.ImportOptions(text_mode="labels", import_text=True)

    result = core._render_canonical_text_items(
        pdf_doc=SimpleNamespace(),
        page=SimpleNamespace(),
        pdf_path="fixture.pdf",
        page_num=1,
        page_h=100.0,
        page_w=100.0,
        scale=1.0,
        fc_doc=SimpleNamespace(),
        parent_group=SimpleNamespace(),
        opts=opts,
        pdf_sha256="a" * 64,
        raw_tdict=raw_tdict,
    )

    assert observed == [
        (
            "p1:b0:l0:s0",
            "labels",
            {"text", "labels", "3d_text", "glyphs", "geometry", "raster"},
        )
    ]
    assert result["count"] == 1
    assert result["source_item_ids"] == ["p1:b0:l0:s0"]


def test_canonical_3d_counts_visible_delivery_not_hidden_support_objects(monkeypatch):
    item = _canonical_item("3d_text")
    raw_tdict = {
        "blocks": [{
            "type": 0,
            "lines": [{"dir": (1.0, 0.0), "spans": [dict(item["span"])]}],
        }]
    }

    def fake_executor(source_item, requested, _deliverers, _opts):
        assert requested == "3d_text"
        return {
            "source_item_id": source_item["source_item_id"],
            "requested_type": requested,
            "attempted_type": requested,
            "final_type": requested,
            "outcome": "verified",
            "created_entity_ids": ["ShapeString", "Clone2D", "PDF_3D_Text"],
            "delivery_entity_ids": ["PDF_3D_Text"],
            "support_entity_ids": ["ShapeString", "Clone2D"],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {"solid_count": 1},
        }

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", fake_executor)
    opts = core.ImportOptions(text_mode="3d_text", import_text=True)

    result = core._render_canonical_text_items(
        pdf_doc=SimpleNamespace(),
        page=SimpleNamespace(),
        pdf_path="fixture.pdf",
        page_num=1,
        page_h=100.0,
        page_w=100.0,
        scale=1.0,
        fc_doc=SimpleNamespace(),
        parent_group=SimpleNamespace(),
        opts=opts,
        pdf_sha256="a" * 64,
        raw_tdict=raw_tdict,
    )

    assert result["source_item_count"] == 1
    assert result["count"] == 1
    assert opts.text_delivered_counts == {"native_3d_text": 1}


def test_canonical_geometry_counts_serialized_raw_edges_not_container_objects(
    monkeypatch,
):
    item = _canonical_item("geometry")
    monkeypatch.setattr(core, "_iter_text_source_items", lambda *_args: iter([item]))
    monkeypatch.setattr(
        core,
        "_run_text_item_fallback_ladder",
        lambda *_args: {
            "source_item_id": item["source_item_id"],
            "requested_type": "geometry",
            "attempted_type": "geometry",
            "final_type": "geometry",
            "outcome": "verified",
            "created_entity_ids": ["GeometryContainer001"],
            "delivery_count": 17,
        },
    )
    opts = core.ImportOptions(text_mode="geometry", import_text=True)

    result = core._render_canonical_text_items(
        pdf_doc=SimpleNamespace(),
        page=SimpleNamespace(),
        pdf_path="fixture.pdf",
        page_num=1,
        page_h=100.0,
        page_w=100.0,
        scale=1.0,
        fc_doc=SimpleNamespace(),
        parent_group=SimpleNamespace(),
        opts=opts,
        pdf_sha256="a" * 64,
        raw_tdict={"blocks": []},
    )

    assert result["count"] == 17
    assert opts.text_delivered_counts == {"raw_geometry_edges": 17}


def test_rawdict_whitespace_span_is_a_canonical_exact_source_item():
    item, _raw_tdict = _synthetic_whitespace_source("text")

    assert item["source_item_id"] == "p1:b0:l0:s0"
    assert item["text"] == "   "
    assert item["span"]["text"] == "   "
    assert [char["c"] for char in item["span"]["chars"]] == [" ", " ", " "]


def test_synthetic_raw_characters_are_not_bound_as_physical_zero_ink_evidence():
    item = _physically_bind_whitespace_item("text")

    assert "source_ink_evidence" not in item
    assert "source_font_asset_bindings" not in item
    assert "source_glyph_id_sequence" not in item


def test_non_synthetic_whitespace_without_physical_trace_is_not_called_zero_ink():
    item, raw_tdict = _synthetic_whitespace_source("text")
    for char in raw_tdict["blocks"][0]["lines"][0]["spans"][0]["chars"]:
        char["synthetic"] = False
    item = list(core._iter_text_source_items(raw_tdict, 1, "a" * 64, "text"))[0]

    [bound] = core._bind_page_text_source_ink_evidence(
        _SyntheticWhitespacePage(),
        raw_tdict,
        [item],
    )

    assert "source_ink_evidence" not in bound


@pytest.mark.parametrize(
    ("attempted_type", "expected_proxy_type", "text_property"),
    [
        ("text", "Text", "Text"),
        ("labels", "Label", "Text"),
    ],
)
def test_native_item_delivery_rereads_live_text_transform_style_and_metadata(
    monkeypatch, attempted_type, expected_proxy_type, text_property
):
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item(attempted_type)
    item["span"]["color"] = 0x336699
    opts = core.ImportOptions(
        text_mode=attempted_type,
        import_text=True,
        scale_to_mm=False,
        user_scale=1.0,
    )

    result = core._deliver_text_item_native(
        item,
        attempted_type,
        opts,
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["outcome"] == "verified"
    assert result["final_type"] == attempted_type
    assert len(result["created_entity_ids"]) == 1
    host = document.getObject(result["created_entity_ids"][0])
    assert host.TypeId == "App::FeaturePython"
    assert host.Proxy.Type == expected_proxy_type
    assert list(getattr(host, text_property)) == [item["text"]]
    assert host.PDFSourceItemId == item["source_item_id"]
    assert host.PDFRepresentation == attempted_type
    assert host.ViewObject.FontSize > 0.0
    assert host.ViewObject.FontName == "Arial"
    assert host.ViewObject.Justification == "Left"
    assert host.ViewObject.TextColor == pytest.approx((0.2, 0.4, 0.6))
    assert host.PDFTextFontName == "Arial"
    assert host.PDFTextFontSize == pytest.approx(host.ViewObject.FontSize)
    assert host.PDFTextJustification == "Left"
    assert host.PDFTextColorRGB == "0.2,0.4,0.6"
    assert host.PDFTextVisibility is True
    assert host.ViewObject.Visibility is True
    assert host.ViewObject.DisplayMode == "World"
    assert host.ViewObject.ScaleMultiplier == pytest.approx(1.0)
    assert host.ViewObject.LineSpacing == pytest.approx(1.0)
    assert result["evidence"]["style_verification"] == "gui_view_and_app_metadata"
    assert result["evidence"]["view_style_verified"] is True
    if attempted_type == "labels":
        assert host.Points == []
        assert host.ViewObject.Line is False
        assert host.ViewObject.Frame == "None"
        assert host.ViewObject.ArrowTypeStart == "None"
        assert host.ViewObject.TextAlignment == "Bottom"
        assert host.ViewObject.MaxChars == 0
        assert result["evidence"]["label_marker_absent"] is True
        assert result["evidence"]["label_marker_verification"] == "gui_view"
    assert core._host_anchor_xyz(host) == pytest.approx(
        result["evidence"]["expected_anchor_xyz"]
    )
    assert math.isclose(
        core._host_text_rotation_deg(host),
        result["evidence"]["rotation_deg"],
        abs_tol=1e-7,
    )


@pytest.mark.parametrize("attempted_type", ["text", "labels"])
def test_native_synthetic_whitespace_is_terminal_and_clean(
    monkeypatch, attempted_type
):
    document, group = _install_native_host(monkeypatch)
    item = _physically_bind_whitespace_item(attempted_type)

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_native(
            item,
            attempted_type,
            core.ImportOptions(text_mode=attempted_type, import_text=True),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    assert raised.value.attempt["reason"] == "source_ink_authority_missing"
    assert document.Objects == []


@pytest.mark.parametrize("attempted_type", ["text", "labels"])
def test_native_whitespace_without_physical_authority_is_terminal_and_clean(
    monkeypatch, attempted_type
):
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item(attempted_type)
    item["text"] = item["span"]["text"] = "   "

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_native(
            item,
            attempted_type,
            core.ImportOptions(text_mode=attempted_type, import_text=True),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    assert raised.value.attempt["reason"] == "source_ink_authority_missing"
    assert document.Objects == []


class _EmptyShape:
    Vertexes = []
    Edges = []
    Faces = []
    Solids = []
    Volume = 0.0

    @staticmethod
    def isNull():
        return True


class _FreeCADNullShape(_EmptyShape):
    @property
    def Volume(self):
        raise RuntimeError("shape is invalid")


def test_zero_ink_evidence_accepts_freecad_null_shape_without_volume() -> None:
    evidence = core._zero_visible_ink_shape_evidence(_FreeCADNullShape())

    assert evidence["zero_visible_ink_verified"] is True
    assert evidence["shape_is_null"] is True
    assert evidence["volume"] == 0.0
    assert evidence["volume_authority"] == "null_shape_has_no_evaluable_volume"


def test_synthetic_whitespace_3d_text_is_terminal_and_clean(
    monkeypatch,
):
    document, group = _install_native_host(monkeypatch)
    item = _physically_bind_whitespace_item("3d_text")
    monkeypatch.setattr(core, "Part", SimpleNamespace(Shape=_EmptyShape))
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: pytest.fail(
            "zero-ink 3D text must not instantiate a visible ShapeString"
        ),
    )
    monkeypatch.setattr(
        core,
        "_make_shapestring_host",
        lambda *_args, **_kwargs: pytest.fail(
            "zero-ink 3D text must not instantiate a visible ShapeString"
        ),
    )

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text", import_text=True),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    assert raised.value.attempt["reason"] == "source_ink_authority_missing"
    assert document.Objects == []


@pytest.mark.parametrize("attempted_type", ["glyphs", "geometry"])
def test_synthetic_whitespace_svg_modes_are_terminal_and_clean(
    monkeypatch, attempted_type
):
    from PDFVectorImporter.src import PDFSvgTextRenderer as renderer

    document, group = _install_native_host(monkeypatch)
    item = _physically_bind_whitespace_item(attempted_type)
    monkeypatch.setattr(core, "Part", SimpleNamespace(Shape=_EmptyShape))
    monkeypatch.setattr(
        renderer,
        "render_text",
        lambda *_args, **_kwargs: pytest.fail(
            "a physically zero-ink source item must not fabricate SVG outlines"
        ),
    )

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_svg(
            item,
            attempted_type,
            core.ImportOptions(text_mode=attempted_type, import_text=True),
            pdf_path="fixture.pdf",
            page_h=100.0,
            page_w=100.0,
            scale=1.0,
            fc_doc=document,
            parent_group=group,
            render_cache={},
        )

    assert raised.value.attempt["reason"] == "source_ink_authority_missing"
    assert document.Objects == []


@pytest.mark.parametrize("attempted_type", ["text", "labels"])
def test_native_item_delivery_headless_rereads_honest_app_style_metadata(
    monkeypatch, attempted_type
):
    document, group = _install_native_host(monkeypatch)
    original_add_object = document.addObject

    def add_headless_object(kind, name):
        obj = original_add_object(kind, name)
        obj.ViewObject = None
        return obj

    monkeypatch.setattr(document, "addObject", add_headless_object)
    item = _canonical_item(attempted_type)
    item["span"]["color"] = 0x336699

    result = core._deliver_text_item_native(
        item,
        attempted_type,
        core.ImportOptions(
            text_mode=attempted_type,
            import_text=True,
            scale_to_mm=False,
            user_scale=1.0,
        ),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    host = document.getObject(result["created_entity_ids"][0])
    assert host.ViewObject is None
    assert host.PDFTextFontName == "Arial"
    assert host.PDFTextFontSize > 0.0
    assert host.PDFTextJustification == "Left"
    assert host.PDFTextColorRGB == "0.2,0.4,0.6"
    assert host.PDFTextVisibility is True
    assert result["outcome"] == "verified"
    assert result["evidence"]["style_verification"] == "headless_app_metadata"
    assert result["evidence"]["view_style_verified"] is False
    if attempted_type == "labels":
        assert result["evidence"]["label_marker_absent"] is False
        assert result["evidence"]["label_marker_verification"] == "pending"


def test_native_text_with_unknown_source_font_keeps_supported_host_default(monkeypatch):
    document, group = _install_native_host(monkeypatch)
    original_add_object = document.addObject

    def add_object_with_host_font(kind, name):
        obj = original_add_object(kind, name)
        obj.ViewObject.FontName = "Supported Host Font"
        obj.ViewObject.Font = "Supported Host Font"
        return obj

    monkeypatch.setattr(document, "addObject", add_object_with_host_font)
    item = _canonical_item("text")
    item["span"]["font"] = ""

    result = core._deliver_text_item_native(
        item,
        "text",
        core.ImportOptions(
            text_mode="text",
            import_text=True,
            scale_to_mm=False,
            user_scale=1.0,
        ),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    host = document.getObject(result["created_entity_ids"][0])
    assert result["outcome"] == "verified"
    assert result["final_type"] == "text"
    assert host.PDFRepresentation == "text"
    assert host.PDFTextFontName == ""
    assert host.ViewObject.FontName == "Supported Host Font"


def test_rotated_native_text_remains_text_with_verified_persistent_placement(
    monkeypatch,
):
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item("text")
    item["line_direction"] = (0.0, 1.0)
    item["rotation_deg"] = -90.0
    opts = core.ImportOptions(
        text_mode="text",
        import_text=True,
        scale_to_mm=False,
        user_scale=1.0,
    )

    def deliver(source_item, attempted, state):
        return core._deliver_text_item_native(
            source_item,
            attempted,
            state,
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    result = deliver(item, "text", opts)

    assert result["final_type"] == "text"
    assert result["evidence"]["rotation_deg"] == pytest.approx(-90.0)
    assert opts.text_delivery_attempts == []
    assert opts.text_mode_fallbacks == []
    assert len(document.Objects) == 1
    assert document.Objects[0].TypeId == "App::FeaturePython"
    assert document.Objects[0].Proxy.Type == "Text"


def test_item_raster_rejects_unbound_mock_page_and_direct_deliverer_bypass(
    monkeypatch, tmp_path
):
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item("raster")
    opts = core.ImportOptions(
        text_mode="raster",
        import_text=True,
        scale_to_mm=False,
        user_scale=1.0,
        raster_dpi=144,
    )

    class Pixmap:
        width = 16
        height = 24

        def save(self, path):
            Path(path).write_bytes(b"verified-raster-patch")

    class Page:
        rect = SimpleNamespace(x0=0.0, y0=0.0, x1=100.0, y1=100.0)

        def get_pixmap(self, **kwargs):
            assert kwargs["clip"].x0 == pytest.approx(item["bbox"][0])
            return Pixmap()

    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path)

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_raster(
            item,
            "raster",
            opts,
            page=Page(),
            page_h=100.0,
            scale=1.0,
            fc_doc=document,
            parent_group=group,
        )

    assert raised.value.attempt["reason"] == "invalid_raster_ladder_prefix"
    assert document.Objects == []


def test_full_page_raster_rejects_fabricated_digest_and_mock_page(
    monkeypatch, tmp_path
):
    document, group = _install_native_host(monkeypatch)
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=False,
        text_mode="none",
        scale_to_mm=False,
        user_scale=1.0,
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    opts._pdf_sha256 = "b" * 64

    class Rect:
        x0 = 0.0
        y0 = 0.0
        x1 = 120.0
        y1 = 80.0
        width = 120.0
        height = 80.0

    class Pixmap:
        width = 240
        height = 160

        def save(self, path):
            Path(path).write_bytes(b"verified-full-page-raster")

    class Page:
        rect = Rect()

        def get_pixmap(self, **_kwargs):
            return Pixmap()

    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path)

    with pytest.raises(core.ImportLifecycleError, match="source identity"):
        core._import_page_as_raster(
            SimpleNamespace(),
            Page(),
            1,
            80.0,
            opts,
            1.0,
            group,
            document,
        )
    assert document.Objects == []


def test_raster_verifier_accepts_freecad_cache_rewrites_only_when_bytes_match(tmp_path):
    source = tmp_path / "persistent-source.png"
    image_cache = tmp_path / "freecad-image-cache.png"
    included_cache = tmp_path / "freecad-included-cache.png"
    for path in (source, image_cache, included_cache):
        path.write_bytes(b"same-source-bound-raster")
    host = SimpleNamespace(
        ImageFile=str(image_cache),
        PDFRasterFile=str(included_cache),
        getTypeIdOfProperty=lambda name: (
            "App::PropertyFileIncluded" if name == "PDFRasterFile" else ""
        ),
    )

    evidence = core._raster_file_evidence(host, source)

    assert evidence["source_asset_path"] == str(source)
    assert evidence["image_file_path"] == str(image_cache)
    assert evidence["included_file_path"] == str(included_cache)
    assert evidence["raster_content_verified"] is True
    assert len({
        evidence["source_asset_sha256"],
        evidence["image_file_path_sha256"],
        evidence["included_file_path_sha256"],
    }) == 1

    included_cache.write_bytes(b"wrong-raster")
    with pytest.raises(RuntimeError, match="does not match source"):
        core._raster_file_evidence(host, source)


def test_explicit_raster_is_requested_output_not_fallback():
    opts = core.ImportOptions(import_mode="raster", import_text=False, text_mode="none")

    assert core._report_fallback_state(opts) == (False, None)


def test_release_dependency_probe_rejects_empty_vendor_directory(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "freecad_build_release_contract", REPO_ROOT / "build_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    assert not any(tmp_path.iterdir())
    assert module._lib_has_runtime_dependencies(Path(sys.executable), tmp_path) is False


def test_failed_report_contract_is_never_ready():
    from pdfcadcore.import_report import ImportReport, build_import_contract_ready

    report = ImportReport(
        report_meta={"build_stamp": "freecad · test"},
        result={"text_entities": 0},
        extra={
            "scale_crosscheck": {},
            "result_status": "failed",
            "terminal_failure": {"message": "requested Labels failed"},
        },
    )

    readiness = build_import_contract_ready(report)

    assert readiness["ready"] is False
    assert readiness["checks"]["successful_result"] is False


def test_page_import_function_has_consistent_two_value_return_contract():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(core._import_pdf_page_inner))
    root = tree.body[0]

    class FunctionReturnVisitor(ast.NodeVisitor):
        def __init__(self):
            self.returns = []

        def visit_Return(self, node):
            self.returns.append(node)

        def visit_FunctionDef(self, node):
            if node is root:
                self.generic_visit(node)

    visitor = FunctionReturnVisitor()
    visitor.visit(root)
    returns = visitor.returns

    assert returns
    assert all(
        isinstance(node.value, ast.Tuple) and len(node.value.elts) == 2
        for node in returns
        if node.value is not None
    )


def test_production_import_rolls_back_every_post_baseline_object_on_page_failure(
    monkeypatch, tmp_path
):
    document = _Document()
    existing = document.addObject("Part::Feature", "Existing")
    document.committed = False
    document.aborted = False
    document.openTransaction = lambda _name: None
    document.commitTransaction = lambda: setattr(document, "committed", True)
    document.abortTransaction = lambda: setattr(document, "aborted", True)

    class Page:
        rect = SimpleNamespace(height=100.0, width=100.0)

        def get_text(self, kind):
            return {"blocks": []} if kind == "dict" else ""

    class Pdf:
        def __init__(self):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def __len__(self):
            return 1

        def load_page(self, _index):
            return Page()

        def close(self):
            self.closed = True

    opened = []

    def open_source_attempt(_opts):
        pdf = Pdf()
        opened.append(pdf)
        return pdf

    def fail_page(*_args, **_kwargs):
        document.addObject("App::DocumentObjectGroup", "PDF_Page_1")
        document.addObject("Part::Feature", "Partial_Text")
        raise RuntimeError("synthetic page failure")

    monkeypatch.setattr(core, "_open_pdf_source_attempt", open_source_attempt)
    monkeypatch.setattr(
        core,
        "_ensure_doc_with_ownership",
        lambda: (document, False),
    )
    monkeypatch.setattr(core, "_import_pdf_page_inner", fail_page)
    monkeypatch.setattr(core, "_err", lambda *_args: None)
    opts = core.ImportOptions(
        pages=[1],
        import_text=True,
        text_mode="labels",
        import_report_path=str(tmp_path / "report.json"),
    )
    source_path = tmp_path / "fixture.pdf"
    source_document = core.fitz.open()
    source_document.new_page()
    source_document.save(str(source_path))
    source_document.close()

    with pytest.raises(RuntimeError, match="synthetic page failure"):
        core.import_pdf(str(source_path), opts)

    assert document.aborted is True
    assert document.committed is False
    assert document.Objects == [existing]
