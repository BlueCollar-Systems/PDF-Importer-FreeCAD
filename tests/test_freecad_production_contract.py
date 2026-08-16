from __future__ import annotations

import ast
import concurrent.futures
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


def _canonical_item(requested_type: str = "labels") -> dict:
    span = {
        "text": "A",
        "font": "ArialMT",
        "size": 12.0,
        "bbox": (10.0, 20.0, 18.0, 32.0),
        "origin": (10.0, 30.0),
        "color": 0,
    }
    return {
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


def test_atomic_raster_publication_survives_concurrent_same_key_writers(tmp_path):
    destination = tmp_path / "same-source-key.png"
    barrier = threading.Barrier(2)
    payloads = (b"first-complete-png" * 100, b"second-complete-png" * 100)

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
        self.FontSize = 0.0
        self.FontName = ""
        self.Font = ""
        self.Justification = ""
        self.TextColor = (1.0, 1.0, 1.0)
        self.ShapeColor = (1.0, 1.0, 1.0)
        self.LineColor = (1.0, 1.0, 1.0)
        self.PointColor = (1.0, 1.0, 1.0)


class _HostObject:
    def __init__(self, document, name, type_id):
        self.Document = document
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.ViewObject = _View()
        self.Placement = _Placement()
        self.PropertiesList = []

    def addProperty(self, _kind, name, _group):
        if name not in self.PropertiesList:
            self.PropertiesList.append(name)


class _Document:
    def __init__(self):
        self.Objects = []
        self.recompute_calls = 0

    def addObject(self, kind, name):
        obj = _HostObject(self, f"{name}_{len(self.Objects)}", kind)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self, *_args):
        self.recompute_calls += 1
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
        points,
    ):
        obj = self.document.addObject("App::FeaturePython", "PDF_Label")
        obj.TargetPoint = target_point
        obj.Placement = placement
        obj.LabelType = label_type
        obj.CustomText = [custom_text] if isinstance(custom_text, str) else list(custom_text)
        obj.Text = list(obj.CustomText)
        obj.StraightDirection = direction
        obj.Points = list(points)
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


def test_svg_raster_backed_item_is_a_closed_vector_impossibility(
    monkeypatch,
):
    from PDFVectorImporter.src import PDFSvgTextRenderer as renderer

    def raster_source_only(*_args, **_kwargs):
        raise renderer.TextRepresentationRenderError(
            "svg_item_raster_source_only",
            {
                "raster_source_ids": ["source-17"],
                "raster_source_host_bboxes": [(8.0, 73.0, 38.0, 82.0)],
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
        )

    monkeypatch.setattr(renderer, "render_text", raster_source_only)
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item("glyphs")

    with pytest.raises(core.TextItemImpossible) as raised:
        core._deliver_text_item_svg(
            item,
            "glyphs",
            core.ImportOptions(text_mode="glyphs"),
            pdf_path="fixture.pdf",
            page_h=100.0,
            page_w=100.0,
            scale=1.0,
            fc_doc=document,
            parent_group=group,
            render_cache={},
        )

    assert raised.value.proof["reason_code"] == "svg_item_raster_source_only"
    assert core._validate_item_impossibility_proof(
        item,
        "glyphs",
        "glyphs",
        raised.value.proof,
    )["cleanup_complete"] is True


def test_exhaustive_global_assignment_empty_is_a_closed_vector_impossibility(
    monkeypatch,
):
    from PDFVectorImporter.src import PDFSvgTextRenderer as renderer

    def globally_unassigned(*_args, **_kwargs):
        raise renderer.TextRepresentationRenderError(
            "svg_item_assignment_empty",
            {
                "source_item_id": item["source_item_id"],
                "assignment_method": "source_manifest_global_bounded_v1",
                "placement_count": 3,
                "matched_placement_indices": [],
                "global_unmatched_placement_indices": [],
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
        )

    monkeypatch.setattr(renderer, "render_text", globally_unassigned)
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item("glyphs")

    with pytest.raises(core.TextItemImpossible) as raised:
        core._deliver_text_item_svg(
            item,
            "glyphs",
            core.ImportOptions(text_mode="glyphs"),
            pdf_path="fixture.pdf",
            page_h=100.0,
            page_w=100.0,
            scale=1.0,
            fc_doc=document,
            parent_group=group,
            render_cache={},
        )

    assert raised.value.proof["reason_code"] == "svg_item_assignment_empty"
    assert core._validate_item_impossibility_proof(
        item,
        "glyphs",
        "glyphs",
        raised.value.proof,
    )["cleanup_complete"] is True


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
    assert result["host_entity_count"] == 1
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
    assert result["host_entity_count"] == 3
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
    assert result["host_entity_count"] == 1
    assert opts.text_delivered_counts == {"raw_geometry_edges": 17}


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
    assert result["evidence"]["style_verification"] == "gui_view_and_app_metadata"
    assert result["evidence"]["view_style_verified"] is True
    assert core._host_anchor_xyz(host) == pytest.approx(
        result["evidence"]["expected_anchor_xyz"]
    )
    assert math.isclose(
        core._host_text_rotation_deg(host),
        result["evidence"]["rotation_deg"],
        abs_tol=1e-7,
    )
    assert document.recompute_calls == 0


def test_native_item_delivery_indexes_document_once_for_many_spans(monkeypatch):
    document, group = _install_native_host(monkeypatch)
    for index in range(100):
        document.addObject("Part::Feature", "Existing_%03d" % index)

    real_document_objects = core._document_objects
    scans = []

    def counted_document_objects(doc, *, required=False):
        scans.append(len(doc.Objects))
        return real_document_objects(doc, required=required)

    monkeypatch.setattr(core, "_document_objects", counted_document_objects)
    opts = core.ImportOptions(
        text_mode="text",
        import_text=True,
        scale_to_mm=False,
        user_scale=1.0,
    )

    created_ids = []
    for span_index in range(25):
        item = _canonical_item("text")
        item["source_item_id"] = "p1:b0:l0:s%d" % span_index
        item["span_index"] = span_index
        result = core._deliver_text_item_native(
            item,
            "text",
            opts,
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )
        created_ids.extend(result["created_entity_ids"])

    assert len(created_ids) == 25
    assert len(set(created_ids)) == 25
    assert scans == [100]
    assert all(document.getObject(entity_id) is not None for entity_id in created_ids)


def test_raster_item_delivery_indexes_document_once_for_many_spans(monkeypatch, tmp_path):
    document, group = _install_native_host(monkeypatch)
    for index in range(100):
        document.addObject("Part::Feature", "Existing_%03d" % index)

    real_document_objects = core._document_objects
    scans = []

    def counted_document_objects(doc, *, required=False):
        scans.append(len(doc.Objects))
        return real_document_objects(doc, required=required)

    monkeypatch.setattr(core, "_document_objects", counted_document_objects)
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path)
    opts = core.ImportOptions(
        text_mode="raster",
        import_text=True,
        scale_to_mm=False,
        user_scale=1.0,
        raster_dpi=144,
    )
    opts._defer_page_recompute = True

    class Pixmap:
        width = 16
        height = 24

        def save(self, path):
            Path(path).write_bytes(b"verified-raster-patch")

    class Page:
        rect = SimpleNamespace(x0=0.0, y0=0.0, x1=100.0, y1=100.0)

        def get_pixmap(self, **_kwargs):
            return Pixmap()

    page = Page()
    created_ids = []
    for span_index in range(25):
        item = _canonical_item("raster")
        item["source_item_id"] = "p1:b0:l0:s%d" % span_index
        item["span_index"] = span_index
        result = core._deliver_text_item_raster(
            item,
            "raster",
            opts,
            page=page,
            page_h=100.0,
            scale=1.0,
            fc_doc=document,
            parent_group=group,
        )
        created_ids.extend(result["created_entity_ids"])

    assert len(created_ids) == 25
    assert len(set(created_ids)) == 25
    assert scans == [100]
    assert all(document.getObject(entity_id) is not None for entity_id in created_ids)


def test_3d_text_item_delivery_indexes_document_once_for_many_spans(monkeypatch):
    document, group = _install_native_host(monkeypatch)
    for index in range(100):
        document.addObject("Part::Feature", "Existing_%03d" % index)

    real_document_objects = core._document_objects
    scans = []

    def counted_document_objects(doc, *, required=False):
        scans.append(len(doc.Objects))
        return real_document_objects(doc, required=required)

    monkeypatch.setattr(core, "_document_objects", counted_document_objects)

    def fake_compound(
        doc,
        *,
        source_text,
        font_path,
        font_size_fc,
        depth,
        target_advance_fc,
        placement,
        text_group,
        baseline_object_ids=None,
        configure_host=None,
    ):
        host = doc.addObject("Part::Feature", "PDF_3D_Text")
        host.TypeId = "Part::Feature"
        host.Shape = SimpleNamespace(
            isNull=lambda: False,
            Solids=[object(), object()],
            Volume=12.5,
        )
        host.Placement = placement
        if callable(configure_host):
            configure_host(host)
        text_group.addObject(host)
        return host, 1.0, float(target_advance_fc), float(target_advance_fc)

    monkeypatch.setattr(core, "_create_verified_compound_text3d_entity", fake_compound)

    def fake_font_resolution(font_name, _opts, **context):
        identity = core._canonical_font_identity(font_name)
        path = "C:/fonts/source.ttf"
        return path, [
            {
                "source": "embedded_font",
                "outcome": "found",
                "font_identity": identity,
                "path": path,
                "sha256": "b" * 64,
                "pdf_sha256": context["pdf_sha256"],
                "page_number": context["page_number"],
                "staging_complete": True,
            }
        ]

    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        fake_font_resolution,
    )
    opts = core.ImportOptions(
        text_mode="3d_text",
        import_text=True,
        scale_to_mm=False,
        user_scale=1.0,
    )

    created_ids = []
    for span_index in range(25):
        item = _canonical_item("3d_text")
        item["font_identity"] = core._canonical_font_identity(item["span"]["font"])
        item["source_item_id"] = "p1:b0:l0:s%d" % span_index
        item["span_index"] = span_index
        result = core._deliver_text_item_3d(
            item,
            "3d_text",
            opts,
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )
        created_ids.extend(result["created_entity_ids"])

    assert len(created_ids) == 25
    assert len(set(created_ids)) == 25
    assert scans == [100]
    assert all(document.getObject(entity_id) is not None for entity_id in created_ids)


def test_page_recompute_can_be_deferred_until_import_finishes():
    document = _Document()
    opts = core.ImportOptions()

    assert core._recompute_page_if_needed(document, opts) is True
    assert document.recompute_calls == 1

    opts._defer_page_recompute = True

    assert core._recompute_page_if_needed(document, opts) is False
    assert document.recompute_calls == 1


def test_orchestrated_import_defers_item_recompute_even_for_one_page():
    assert core._should_defer_item_recompute(0) is False
    assert core._should_defer_item_recompute(1) is True
    assert core._should_defer_item_recompute(2) is True


def test_page_inner_never_bypasses_deferred_recompute():
    source = inspect.getsource(core._import_pdf_page_inner)

    assert "fc_doc.recompute()" not in source


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
    assert result["outcome"] == "verified"
    assert result["evidence"]["style_verification"] == "headless_app_metadata"
    assert result["evidence"]["view_style_verified"] is False


def test_label_delivery_accepts_custom_text_before_deferred_document_recompute(
    monkeypatch,
):
    document, group = _install_native_host(monkeypatch)
    original_make_label = core.Draft.make_label

    def make_label_with_deferred_derived_text(**kwargs):
        host = original_make_label(**kwargs)
        # Real Draft Labels populate their authoritative CustomText immediately,
        # but derive Text only when the document recomputes.
        host.Text = []
        return host

    monkeypatch.setattr(core.Draft, "make_label", make_label_with_deferred_derived_text)
    item = _canonical_item("labels")

    result = core._deliver_text_item_native(
        item,
        "labels",
        core.ImportOptions(
            text_mode="labels",
            import_text=True,
            scale_to_mm=False,
            user_scale=1.0,
        ),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    host = document.getObject(result["created_entity_ids"][0])
    assert list(host.CustomText) == [item["text"]]
    assert list(host.Text) == []
    assert result["outcome"] == "verified"
    assert result["evidence"]["source_text_property"] == "CustomText"
    assert document.recompute_calls == 0


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


@pytest.mark.parametrize("attempted_type", ["text", "labels"])
def test_native_text_nul_is_proven_impossible_before_host_creation(
    monkeypatch, attempted_type
):
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item("text")
    item["text"] = "\x00"
    item["span"]["text"] = "\x00"
    opts = core.ImportOptions(
        text_mode="text",
        import_text=True,
        scale_to_mm=False,
        user_scale=1.0,
    )

    with pytest.raises(core.TextItemImpossible) as raised:
        core._deliver_text_item_native(
            item,
            attempted_type,
            opts,
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    assert document.Objects == []
    proof = raised.value.proof
    assert proof["reason_code"] == "source_text_contains_nul"
    assert proof["evidence"]["nul_codepoint_indices"] == [0]
    assert core._validate_item_impossibility_proof(
        item,
        "text",
        attempted_type,
        proof,
    ) == proof


def test_nul_text_advances_to_exact_svg_glyph_delivery(monkeypatch):
    document, group = _install_native_host(monkeypatch)
    item = _canonical_item("text")
    item["text"] = "\x00"
    item["span"]["text"] = "\x00"
    opts = core.ImportOptions(
        text_mode="text",
        import_text=True,
        scale_to_mm=False,
        user_scale=1.0,
    )

    def native(source_item, attempted, state):
        return core._deliver_text_item_native(
            source_item,
            attempted,
            state,
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    def text_3d(source_item, attempted, state):
        return core._deliver_text_item_3d(
            source_item,
            attempted,
            state,
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    def glyphs(source_item, attempted, _state):
        return {
            "source_item_id": source_item["source_item_id"],
            "requested_type": "text",
            "attempted_type": attempted,
            "final_type": attempted,
            "outcome": "verified",
            "created_entity_ids": ["Glyph001"],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {"svg_source_glyph_verified": True},
        }

    result = core._run_text_item_fallback_ladder(
        item,
        "text",
        {
            "text": native,
            "labels": native,
            "3d_text": text_3d,
            "glyphs": glyphs,
        },
        opts,
    )

    assert result["final_type"] == "glyphs"
    assert result["attempted_types"] == ["text", "labels", "3d_text", "glyphs"]
    assert [proof["reason_code"] for proof in result["proof_chain"]] == [
        "source_text_contains_nul",
        "source_text_contains_nul",
        "source_text_contains_nul",
    ]
    assert document.Objects == []


def test_item_raster_delivery_is_persistent_verified_and_source_bound(
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
    opts._defer_page_recompute = True

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

    result = core._deliver_text_item_raster(
        item,
        "raster",
        opts,
        page=Page(),
        page_h=100.0,
        scale=1.0,
        fc_doc=document,
        parent_group=group,
    )

    assert result["outcome"] == "verified"
    host = document.getObject(result["created_entity_ids"][0])
    raster_path = Path(host.PDFRasterFile)
    assert raster_path.is_file()
    assert raster_path.read_bytes() == b"verified-raster-patch"
    assert host.ImageFile == str(raster_path)
    assert host.PDFSourceItemId == item["source_item_id"]
    assert host.PDFRepresentation == "raster"
    assert host.PDFSourceSHA256 == item["pdf_sha256"]
    assert host.PDFRasterSHA256 == result["evidence"]["source_asset_sha256"]
    assert result["evidence"]["raster_content_verified"] is True
    assert host.XSize == pytest.approx(item["bbox"][2] - item["bbox"][0])
    assert host.YSize == pytest.approx(item["bbox"][3] - item["bbox"][1])
    assert document.recompute_calls == 0


def test_text_raster_cache_renders_page_once_at_bounded_effective_dpi(
    monkeypatch,
):
    fitz = pytest.importorskip("fitz")
    pdf = fitz.open()
    page = pdf.new_page(width=200.0, height=100.0)
    page.insert_text((20.0, 50.0), "CACHE", fontsize=12.0)
    opts = core.ImportOptions(raster_dpi=300)
    monkeypatch.setenv("BC_FC_TEXT_RASTER_CACHE_MAX_PIXELS", "40000")

    first, first_dpi = core._cached_text_raster_pixmap(
        page,
        fitz.Rect(15.0, 35.0, 80.0, 55.0),
        requested_dpi=300,
        page_number=1,
        opts=opts,
    )
    second, second_dpi = core._cached_text_raster_pixmap(
        page,
        fitz.Rect(90.0, 35.0, 150.0, 55.0),
        requested_dpi=300,
        page_number=1,
        opts=opts,
    )

    assert 72 <= first_dpi < 300
    assert second_dpi == first_dpi
    assert first.width > 0 and first.height > 0
    assert second.width > 0 and second.height > 0
    assert opts._text_raster_page_cache["render_count"] == 1
    pdf.close()


def test_full_page_raster_returns_verified_persistent_host_evidence(
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
    opts._defer_page_recompute = True

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

    result = core._import_page_as_raster(
        SimpleNamespace(),
        Page(),
        1,
        80.0,
        opts,
        1.0,
        group,
        document,
    )

    assert result["outcome"] == "verified"
    host = document.getObject(result["created_entity_ids"][0])
    assert host.TypeId == "Image::ImagePlane"
    assert Path(host.PDFRasterFile).read_bytes() == b"verified-full-page-raster"
    assert host.ImageFile == host.PDFRasterFile
    assert host.PDFSourceSHA256 == "b" * 64
    assert host.PDFRasterSHA256 == result["evidence"]["source_asset_sha256"]
    assert host.XSize == pytest.approx(120.0)
    assert host.YSize == pytest.approx(80.0)
    assert result["evidence"]["raster_file_included"] is True
    assert result["evidence"]["raster_content_verified"] is True
    assert document.recompute_calls == 0


def test_raster_verifier_accepts_freecad_cache_rewrites_only_when_bytes_match(tmp_path):
    source = tmp_path / "persistent-source.png"
    image_cache = tmp_path / "freecad-image-cache.png"
    included_cache = tmp_path / "freecad-included-cache.png"
    for path in (source, image_cache, included_cache):
        path.write_bytes(b"same-source-bound-raster")
    host = SimpleNamespace(
        ImageFile=str(image_cache),
        PDFRasterFile=str(included_cache),
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


def test_raster_verifier_hashes_identical_resolved_paths_once(tmp_path, monkeypatch):
    source = tmp_path / "shared-source.png"
    source.write_bytes(b"one-source-bound-raster")
    host = SimpleNamespace(
        ImageFile=str(source),
        PDFRasterFile=str(source),
    )
    hash_calls = []
    real_path_sha256 = core._path_sha256

    def counted_path_sha256(path):
        hash_calls.append(str(Path(path)))
        return real_path_sha256(path)

    monkeypatch.setattr(core, "_path_sha256", counted_path_sha256)

    evidence = core._raster_file_evidence(host, source)

    assert evidence["raster_content_verified"] is True
    assert hash_calls == [str(source)]


def test_raster_verifier_reuses_precomputed_source_digest(tmp_path, monkeypatch):
    source = tmp_path / "shared-source.png"
    source.write_bytes(b"one-source-bound-raster")
    host = SimpleNamespace(
        ImageFile=str(source),
        PDFRasterFile=str(source),
    )
    hash_calls = []

    def counted_path_sha256(path):
        hash_calls.append(str(Path(path)))
        return "deadbeef"

    monkeypatch.setattr(core, "_path_sha256", counted_path_sha256)

    evidence = core._raster_file_evidence(
        host, source, source_sha256="a" * 64
    )

    assert evidence["source_asset_sha256"] == "a" * 64
    assert evidence["raster_content_verified"] is True
    assert hash_calls == []


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
    from pdfcadcore import fitz_loader

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

    def safe_open(_path):
        pdf = Pdf()
        opened.append(pdf)
        return pdf

    def fail_page(*_args, **_kwargs):
        document.addObject("App::DocumentObjectGroup", "PDF_Page_1")
        document.addObject("Part::Feature", "Partial_Text")
        raise RuntimeError("synthetic page failure")

    monkeypatch.setattr(fitz_loader, "safe_open", safe_open)
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_pdf_file_sha256", lambda _path: "c" * 64)
    monkeypatch.setattr(core, "_import_pdf_page_inner", fail_page)
    monkeypatch.setattr(core, "_err", lambda *_args: None)
    opts = core.ImportOptions(
        pages=[1],
        import_text=True,
        text_mode="labels",
        import_report_path=str(tmp_path / "report.json"),
    )

    with pytest.raises(RuntimeError, match="synthetic page failure"):
        core.import_pdf("fixture.pdf", opts)

    assert document.aborted is True
    assert document.committed is False
    assert document.Objects == [existing]


def test_model3d_off_skips_document_text_preflight(monkeypatch, tmp_path):
    from pdfcadcore import fitz_loader

    document = _Document()
    document.openTransaction = lambda _name: None
    document.commitTransaction = lambda: None
    document.abortTransaction = lambda: None
    text_reads = []

    class Page:
        rect = SimpleNamespace(height=100.0, width=100.0)

        def get_text(self, kind):
            text_reads.append(kind)
            raise AssertionError("disabled 3D mode must not pre-read page text")

    class Pdf:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def __len__(self):
            return 1

        def load_page(self, _index):
            return Page()

        def close(self):
            pass

    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: Pdf())
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_pdf_file_sha256", lambda _path: "d" * 64)
    monkeypatch.setattr(
        core,
        "_import_pdf_page_inner",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic page stop")
        ),
    )
    monkeypatch.setattr(core, "_err", lambda *_args: None)
    opts = core.ImportOptions(
        pages=[1],
        import_text=True,
        text_mode="text",
        import_report_path=str(tmp_path / "report.json"),
    )
    opts.model3d_mode = "off"
    opts.model3d_semantic = False

    with pytest.raises(RuntimeError, match="synthetic page stop"):
        core.import_pdf("fixture.pdf", opts)

    assert text_reads == []


def test_canonical_text_items_cache_bootstrap_and_scale_without_page_reread():
    opts = core.ImportOptions(import_text=True, text_mode="text")
    opts._bootstrap_text_items = []
    opts._scale_cached_pages = set()
    items = [
        {
            "text": 'SCALE 1/4" = 1\'-0"',
            "origin": (80.0, 90.0),
            "bbox": (80.0, 85.0, 99.0, 95.0),
        }
    ]

    resolved = core._cache_canonical_text_metadata(
        opts,
        items,
        page_num=1,
        page_w=100.0,
        page_h=100.0,
    )

    assert opts._bootstrap_text_items == [
        {"text": 'SCALE 1/4" = 1\'-0"', "page": 1}
    ]
    assert opts._scale_cached_pages == {1}
    assert resolved.factor == pytest.approx(48.0)
    assert opts.resolved_scale["factor"] == pytest.approx(48.0)


def test_explicit_raster_inventory_never_interprets_vector_drawings():
    class Page:
        def get_drawings(self):
            raise AssertionError("explicit raster must not interpret vectors")

        def get_images(self, **_kwargs):
            raise AssertionError("explicit raster needs only the rendered page")

    drawings, image_count = core._page_visual_inventory(Page(), "raster")

    assert drawings == []
    assert image_count == 0
