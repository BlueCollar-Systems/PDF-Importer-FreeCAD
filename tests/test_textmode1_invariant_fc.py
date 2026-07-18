"""Exact requested-type invariants for FreeCAD text delivery."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "PDFVectorImporter" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from PDFVectorImporter.src import PDFSvgTextRenderer as renderer  # noqa: E402


def test_readme_documents_six_distinct_modes_and_exact_controller_ladders():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    display = {
        "text": "Text",
        "labels": "Labels",
        "3d_text": "3D Text",
        "glyphs": "Glyphs",
        "geometry": "Geometry",
        "raster": "Raster",
    }

    for mode in display.values():
        assert f"| **{mode}** |" in readme
    for requested, ladder in core.TEXT_ITEM_FALLBACK_LADDERS.items():
        rendered = " → ".join(display[rung] for rung in ladder)
        assert f"| **{display[requested]}** | {rendered} |" in readme

    assert "always achievable" not in readme.lower()
    assert "terminal verified attempt" in readme.lower()


def _report(tmp_path, requested, delivered, count):
    opts = core.ImportOptions(text_mode=requested)
    bucket_by_type = {
        "labels": "native_label",
        "text": "native_text",
        "3d_text": "native_3d_text",
        "glyphs": "outline_curve_or_mesh",
        "geometry": "raw_geometry_edges",
        "raster": "raster_text_patch",
    }
    entity_ids = ["Delivered%03d" % index for index in range(count)]
    evidence = {"host_entity_type": "Part::Feature"}
    if delivered == "labels":
        evidence = {
            "host_entity_type": "App::FeaturePython",
            "host_proxy_type": "Label",
            "source_text": "LABEL",
            "source_text_preserved": True,
            "view_style_verified": True,
            "label_marker_absent": True,
        }
    opts.text_delivery_attempts.append(
        {
            "source_item_id": "p1:text:0",
            "requested_type": requested,
            "attempted_type": delivered,
            "final_type": delivered,
            "outcome": "verified",
            "created_entity_ids": entity_ids,
            "delivery_entity_ids": entity_ids,
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "attempted_types": [delivered],
            "proof_chain": [],
            "evidence": evidence,
        }
    )
    opts.text_delivered_counts[bucket_by_type[delivered]] = count
    opts._report_extra = {
        "actual_text_entity_types": {
            "entity_type": delivered,
            "count": count,
            "font_rendered": delivered in {"labels", "3d_text"},
            "examples": [],
        }
    }
    target = tmp_path / "report.json"
    core.write_import_report(
        pdf_path=str(tmp_path / "fixture.pdf"),
        output_path=str(target),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=count,
    )
    return json.loads(target.read_text(encoding="utf-8"))


def test_glyphs_and_geometry_are_not_treated_as_equivalent_peer_modes(tmp_path):
    report = _report(tmp_path, "glyphs", "geometry", 3)

    violation = report["extra"]["representation_contract_violation"]
    assert violation["requested_type"] == "glyphs"
    assert violation["reason"] == "invalid_item_bound_representation_delivery"
    assert "p1:text:0_non_adjacent_or_repeated_ladder" in violation["invalid_reasons"]


@pytest.mark.parametrize("mode", ["labels", "3d_text", "glyphs", "geometry"])
def test_exact_requested_and_delivered_mode_has_no_contract_violation(tmp_path, mode):
    report = _report(tmp_path, mode, mode, 2)

    assert "representation_contract_violation" not in report["extra"]
    assert "text" not in report["fallback"]


def test_renderer_returning_geometry_for_glyphs_is_terminal_not_accepted(monkeypatch):
    monkeypatch.setattr(
        renderer,
        "render_text",
        lambda *_args, **_kwargs: {
            "outcome": "verified",
            "entity_type": "geometry",
            "glyphs": 1,
            "entities": 1,
            "raw_edges": 1,
            "created_entity_ids": ["Geometry001"],
            "delivery_attempts": [{
                "source_item_id": "p1:g0",
                "requested_type": "glyphs",
                "attempted_type": "geometry",
                "final_type": "geometry",
                "outcome": "verified",
            }],
        },
    )
    opts = core.ImportOptions(text_mode="glyphs")

    with pytest.raises(core.TextRepresentationFailure, match="verification_failed"):
        core._render_requested_svg_text(
            "fixture.pdf", 1, 100.0, 100.0, 1.0, object(), object(), opts
        )

    assert opts.text_delivered_counts == {}
    assert opts.text_delivery_attempts[-1]["requested_type"] == "glyphs"
    assert opts.text_delivery_attempts[-1]["final_type"] is None


def test_terminal_failure_attempt_has_stable_identity_and_cleanup_state(monkeypatch):
    def fail(*_args, **_kwargs):
        raise renderer.TextRepresentationRenderError(
            "svg_renderer_unavailable",
            {
                "requested_type": "geometry",
                "removed_entity_ids": ["Owned001"],
                "cleanup_complete": True,
            },
        )

    monkeypatch.setattr(renderer, "render_text", fail)
    opts = core.ImportOptions(text_mode="geometry")

    with pytest.raises(core.TextRepresentationFailure):
        core._render_requested_svg_text(
            "fixture.pdf", 7, 100.0, 100.0, 1.0, object(), object(), opts
        )

    attempt = opts.text_delivery_attempts[-1]
    assert attempt["source_item_id"] == "p7:page"
    assert attempt["requested_type"] == "geometry"
    assert attempt["attempted_type"] == "geometry"
    assert attempt["removed_entity_ids"] == ["Owned001"]
    assert attempt["cleanup_complete"] is True


@pytest.mark.parametrize("mode", ["labels", "3d_text", "glyphs", "geometry"])
def test_auto_raster_content_classification_cannot_discard_requested_text(mode):
    opts = core.ImportOptions(import_mode="auto", import_text=True, text_mode=mode)

    assert core._auto_raster_needs_text_overlay("raster", 12, opts) is True


@pytest.mark.parametrize("mode", ["labels", "text", "3d_text", "glyphs", "geometry"])
def test_raster_page_strategy_preserves_independent_structural_text_request(mode):
    opts = core.ImportOptions(import_mode="raster", import_text=True, text_mode=mode)

    assert core._raster_page_requires_text_contract_probe(opts) is True
    assert core._auto_raster_needs_text_overlay("raster", 12, opts) is True
    assert core._resolve_raster_text_contract_mode("raster", 12, opts) == (
        "raster",
        True,
    )


def test_only_requested_text_raster_or_disabled_text_is_terminal_on_raster_page():
    requested_raster = core.ImportOptions(
        import_mode="raster", import_text=True, text_mode="raster"
    )
    text_disabled = core.ImportOptions(
        import_mode="raster", import_text=False, text_mode="3d_text"
    )

    assert core._raster_page_requires_text_contract_probe(requested_raster) is False
    assert core._raster_page_requires_text_contract_probe(text_disabled) is False
    assert core._resolve_raster_text_contract_mode(
        "raster", 12, requested_raster
    ) == ("raster", False)
    assert core._resolve_raster_text_contract_mode(
        "raster", 12, text_disabled
    ) == ("raster", False)


def test_failed_fast_text_probe_with_canonical_text_never_places_masking_raster(
    monkeypatch,
):
    class HostObject:
        def __init__(self, document, kind, name):
            self.Document = document
            self.TypeId = kind
            self.Name = name
            self.Label = name
            if kind == "App::DocumentObjectGroup":
                self.Group = []

        def addObject(self, obj):
            if obj not in self.Group:
                self.Group.append(obj)

        def removeObject(self, obj):
            if obj in self.Group:
                self.Group.remove(obj)

        def isDerivedFrom(self, kind):
            return self.TypeId == kind

    class Document:
        def __init__(self):
            self.Objects = []

        def addObject(self, kind, name):
            obj = HostObject(self, kind, name)
            self.Objects.append(obj)
            return obj

        def getObject(self, name):
            return next((obj for obj in self.Objects if obj.Name == name), None)

        def removeObject(self, name):
            self.Objects = [obj for obj in self.Objects if obj.Name != name]

        def recompute(self):
            return None

    raw_tdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {
                        "dir": (1.0, 0.0),
                        "spans": [{"text": "VISIBLE", "size": 12.0}],
                    }
                ],
            }
        ]
    }

    class Page:
        rect = SimpleNamespace(width=100.0, height=100.0)
        rotation_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

        def get_drawings(self):
            return []

        def get_images(self, full=True):
            assert full is True
            return []

        def get_text(self, kind):
            if kind == "blocks":
                raise RuntimeError("synthetic fast probe failure")
            if kind == "rawdict":
                return raw_tdict
            raise AssertionError("unexpected text extraction kind: %s" % kind)

    class Pdf:
        is_encrypted = False

        def __len__(self):
            return 1

        def load_page(self, _index):
            return Page()

        def get_ocgs(self):
            return {}

    raster_calls = []

    def record_raster(*args, **kwargs):
        raster_calls.append((args, kwargs))
        return {
            "outcome": "verified",
            "created_entity_ids": ["UnexpectedRaster"],
            "evidence": {"host_entity_type": "Image::ImagePlane"},
        }

    def render_visible_items(**kwargs):
        assert kwargs["raw_tdict"] is raw_tdict
        return {
            "entity_type": "labels",
            "count": 1,
            "source_item_count": 1,
            "source_item_ids": ["p1:b0:l0:s0"],
            "font_rendered": True,
            "examples": [],
        }

    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(GuiUp=False))
    monkeypatch.setattr(core, "_warn", lambda *_args: None)
    monkeypatch.setattr(core, "_msg", lambda *_args: None)
    monkeypatch.setattr(core, "_pdf_file_sha256", lambda _path: "e" * 64)
    monkeypatch.setattr(core, "_import_page_as_raster", record_raster)
    monkeypatch.setattr(core, "_render_canonical_text_items", render_visible_items)
    opts = core.ImportOptions(
        import_mode="auto",
        import_text=True,
        text_mode="labels",
        raster_fallback=False,
        ignore_images=True,
    )

    _group, text_info = core._import_pdf_page_inner(
        Pdf(), "fixture.pdf", 1, opts, Document()
    )

    assert text_info["entity_type"] == "labels"
    assert text_info["source_item_count"] == 1
    assert raster_calls == []
    assert opts.raster_page_count == 0


@pytest.mark.parametrize("mode", ["labels", "text", "3d_text", "glyphs", "geometry"])
def test_page_without_source_item_uses_page_scoped_visual_fallback(mode):
    opts = core.ImportOptions(import_mode="auto", import_text=True, text_mode=mode)
    attempts_ref = opts.text_delivery_attempts
    fallbacks_ref = opts.text_mode_fallbacks
    counts_ref = opts.text_delivered_counts

    info = core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256="a" * 64,
        raw_tdict={"blocks": [{"type": 1}]},
        raster_result={
            "outcome": "verified",
            "created_entity_ids": ["PageRaster001"],
            "evidence": {
                "host_entity_type": "Image::ImagePlane",
                "pdf_sha256": "a" * 64,
                "source_asset_sha256": "f" * 64,
                "raster_content_verified": True,
                "raster_file_included": True,
            },
        },
    )

    assert opts.text_mode == mode
    assert opts.text_delivery_attempts is attempts_ref
    assert opts.text_mode_fallbacks is fallbacks_ref
    assert opts.text_delivered_counts is counts_ref
    assert info["source_item_count"] == 0
    assert attempts_ref[-1]["final_type"] == "raster"
    assert all(
        attempt["proof"]["page_specific_proven_impossible"] is True
        for attempt in attempts_ref[:-1]
    )
    assert len(fallbacks_ref) == 1
    assert counts_ref == {"raster_text_patch": 1}


def test_no_source_page_fallback_rolls_back_all_ledgers_on_write_failure(monkeypatch):
    opts = core.ImportOptions(import_mode="auto", import_text=True, text_mode="labels")

    def fail_attempt(*_args, **_kwargs):
        raise RuntimeError("ledger write failed")

    monkeypatch.setattr(core, "_append_text_item_attempt", fail_attempt)

    with pytest.raises(RuntimeError, match="ledger write failed"):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256="c" * 64,
            raw_tdict={"blocks": [{"type": 1}]},
            raster_result={
                "outcome": "verified",
                "created_entity_ids": ["PageRaster003"],
                "evidence": {
                    "host_entity_type": "Image::ImagePlane",
                    "pdf_sha256": "c" * 64,
                    "source_asset_sha256": "f" * 64,
                    "raster_content_verified": True,
                    "raster_file_included": True,
                },
            },
        )

    assert opts.text_delivery_attempts == []
    assert opts.text_mode_fallbacks == []
    assert opts.text_delivered_counts == {}
    assert opts.page_visual_source_observations == {}
def test_public_page_import_rolls_back_post_baseline_objects_and_reports_truth(
    monkeypatch,
):
    from pdfcadcore import fitz_loader

    class HostObject:
        def __init__(self, name):
            self.Name = name
            self.Label = name

    class Document:
        def __init__(self):
            self.Objects = []
            self.opened = False
            self.committed = False
            self.aborted = False

        def addObject(self, _kind, name):
            obj = HostObject(name)
            self.Objects.append(obj)
            return obj

        def getObject(self, name):
            return next((obj for obj in self.Objects if obj.Name == name), None)

        def removeObject(self, name):
            self.Objects = [obj for obj in self.Objects if obj.Name != name]

        def recompute(self):
            return None

        def openTransaction(self, _name):
            self.opened = True

        def commitTransaction(self):
            self.committed = True

        def abortTransaction(self):
            self.aborted = True

    class Pdf:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    document = Document()
    existing = document.addObject("Part::Feature", "Existing")
    opened = []

    def safe_open(_path):
        pdf = Pdf()
        opened.append(pdf)
        return pdf

    def fail_page(_pdf_doc, _pdf_path, _page_num, _opts, fc_doc):
        fc_doc.addObject("App::DocumentObjectGroup", "PDF_Page_1")
        fc_doc.addObject("Image::ImagePlane", "Page_1_raster")
        fc_doc.addObject("App::DocumentObjectGroup", "Text")
        raise core.TextRepresentationFailure(
            "synthetic no-source ledger failure",
            {
                "source_item_id": "p1:page",
                "requested_type": "labels",
                "attempted_type": "raster",
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
        )

    monkeypatch.setattr(fitz_loader, "safe_open", safe_open)
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_import_pdf_page_inner", fail_page)
    opts = core.ImportOptions(import_mode="auto", import_text=True, text_mode="labels")

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core.import_pdf_page("fixture.pdf", 1, opts, autofit=False)

    rollback = raised.value.attempt["rollback"]
    expected = {"PDF_Page_1", "Page_1_raster", "Text"}
    assert document.opened is True
    assert document.aborted is True
    assert document.committed is False
    assert document.Objects == [existing]
    assert set(raised.value.attempt["created_entity_ids"]) == expected
    assert set(raised.value.attempt["removed_entity_ids"]) == expected
    assert raised.value.attempt["cleanup_complete"] is True
    assert set(rollback["created_entity_ids"]) == expected
    assert set(rollback["removed_entity_ids"]) == expected
    assert rollback["live_post_baseline_entity_ids"] == []
    assert rollback["cleanup_complete"] is True
    assert opened and opened[0].closed is True


def test_explicit_requested_raster_has_no_false_fallback_chain():
    opts = core.ImportOptions(import_mode="raster", import_text=True, text_mode="raster")

    info = core._record_no_source_text_page_fallback(
        opts,
        page_num=2,
        pdf_sha256="b" * 64,
        raw_tdict={"blocks": []},
        raster_result={
            "created_entity_ids": ["PageRaster002"],
            "evidence": {"host_entity_type": "Image::ImagePlane"},
        },
    )

    assert info["entity_type"] == "raster"
    assert [attempt["attempted_type"] for attempt in opts.text_delivery_attempts] == [
        "raster"
    ]
    assert opts.text_delivery_attempts[0]["outcome"] == "verified"
    assert opts.text_mode_fallbacks == []


def test_explicit_requested_raster_live_branch_records_verified_delivery(monkeypatch):
    class HostObject:
        def __init__(self, document, kind, name):
            self.Document = document
            self.TypeId = kind
            self.Name = name
            self.Label = name
            if kind == "App::DocumentObjectGroup":
                self.Group = []

        def addObject(self, obj):
            if obj not in self.Group:
                self.Group.append(obj)

        def isDerivedFrom(self, kind):
            return self.TypeId == kind

    class Document:
        def __init__(self):
            self.Objects = []

        def addObject(self, kind, name):
            obj = HostObject(self, kind, name)
            self.Objects.append(obj)
            return obj

        def recompute(self):
            return None

    class Page:
        rect = SimpleNamespace(width=100.0, height=100.0)
        rotation_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)

        def get_drawings(self):
            return []

        def get_images(self, full=True):
            assert full is True
            return []

    class Pdf:
        is_encrypted = False

        def __len__(self):
            return 1

        def load_page(self, _index):
            return Page()

        def get_ocgs(self):
            return {}

    raster_result = {
        "outcome": "verified",
        "entity_type": "raster",
        "created_entity_ids": ["PageRaster001"],
        "evidence": {
            "host_entity_type": "Image::ImagePlane",
            "pdf_sha256": "c" * 64,
        },
    }
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(GuiUp=False))
    monkeypatch.setattr(core, "_msg", lambda *_args: None)
    monkeypatch.setattr(core, "_warn", lambda *_args: None)
    monkeypatch.setattr(core, "_pdf_file_sha256", lambda _path: "c" * 64)
    monkeypatch.setattr(core, "_import_page_as_raster", lambda *_args: raster_result)
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=True,
        text_mode="raster",
        ignore_images=True,
    )

    _group, text_info = core._import_pdf_page_inner(
        Pdf(), "fixture.pdf", 1, opts, Document()
    )

    assert text_info["entity_type"] == "raster"
    assert text_info["count"] == 1
    assert opts.text_delivered_counts == {"raster_text_patch": 1}
    assert opts.text_delivery_attempts == [
        {
            "source_item_id": "p1:page",
            "requested_type": "raster",
            "attempted_type": "raster",
            "final_type": "raster",
            "outcome": "verified",
            "created_entity_ids": ["PageRaster001"],
            "delivery_entity_ids": ["PageRaster001"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 1,
            "evidence": raster_result["evidence"],
        }
    ]
    assert opts.text_mode_fallbacks == []


def test_auto_and_hybrid_structural_pages_do_not_request_full_page_masking_raster():
    auto = core.ImportOptions(import_mode="auto", import_text=True, text_mode="labels")
    explicit_hybrid = core.ImportOptions(
        import_mode="hybrid", import_text=True, text_mode="labels"
    )

    auto_mode, auto_probe = core._resolve_raster_text_contract_mode(
        "raster", 12, auto
    )
    image_only_mode, image_only_probe = core._resolve_raster_text_contract_mode(
        "raster", 0, auto
    )

    assert (auto_mode, auto_probe) == ("hybrid", False)
    assert (image_only_mode, image_only_probe) == ("raster", True)
    assert core._should_place_full_page_raster(auto_mode) is False
    assert core._should_place_full_page_raster("hybrid") is False
    assert core._should_place_full_page_raster(explicit_hybrid.import_mode) is False
    assert core._should_place_full_page_raster("raster") is True


def test_legacy_vector_image_only_raster_must_continue_to_text_proof():
    vector = core.ImportOptions(
        import_mode="vector", import_text=True, text_mode="geometry"
    )
    auto = core.ImportOptions(import_mode="auto", import_text=True, text_mode="text")
    explicit_raster = core.ImportOptions(
        import_mode="raster", import_text=True, text_mode="3d_text"
    )
    no_text_contract = core.ImportOptions(
        import_mode="vector", import_text=False, text_mode="geometry"
    )

    assert core._raster_page_requires_text_contract_probe(vector) is True
    assert core._raster_page_requires_text_contract_probe(auto) is True
    assert core._raster_page_requires_text_contract_probe(explicit_raster) is True
    assert core._raster_page_requires_text_contract_probe(no_text_contract) is False
