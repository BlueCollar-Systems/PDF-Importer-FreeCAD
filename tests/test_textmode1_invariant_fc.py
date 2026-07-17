"""Exact requested-type invariants for FreeCAD text delivery."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "PDFVectorImporter" / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from PDFVectorImporter.src import PDFSvgTextRenderer as renderer  # noqa: E402


def _report(tmp_path, requested, delivered, count):
    opts = core.ImportOptions(text_mode=requested)
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
    assert violation == {
        "requested_type": "glyphs",
        "delivered_type": "geometry",
        "delivered_count": 3,
        "reason": "unproven_representation_substitution",
    }


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


def test_explicit_raster_is_terminal_but_image_only_structural_request_needs_proof():
    explicit = core.ImportOptions(import_mode="raster", import_text=True, text_mode="3d_text")
    no_text = core.ImportOptions(import_mode="auto", import_text=True, text_mode="3d_text")

    assert core._auto_raster_needs_text_overlay("raster", 12, explicit) is False
    assert core._auto_raster_needs_text_overlay("raster", 0, no_text) is True


@pytest.mark.parametrize("mode", ["labels", "text", "3d_text", "glyphs", "geometry"])
def test_image_only_page_records_adjacent_proof_gated_raster_fallback(mode):
    opts = core.ImportOptions(import_mode="auto", import_text=True, text_mode=mode)
    raster_result = {
        "created_entity_ids": ["PageRaster001"],
        "evidence": {
            "host_entity_type": "Image::ImagePlane",
            "save_reopen_identity_verified": True,
        },
    }

    info = core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256="a" * 64,
        raw_tdict={"blocks": [{"type": 1}]},
        raster_result=raster_result,
    )

    ladder = list(core.TEXT_ITEM_FALLBACK_LADDERS[mode])
    assert opts.text_mode == mode
    assert [attempt["attempted_type"] for attempt in opts.text_delivery_attempts] == ladder
    assert all(
        attempt["source_item_id"] == "p1:page"
        and attempt["requested_type"] == mode
        for attempt in opts.text_delivery_attempts
    )
    impossible = opts.text_delivery_attempts[:-1]
    assert impossible
    assert all(attempt["outcome"] == "proven_impossible" for attempt in impossible)
    assert all(attempt["reason_code"] == "no_source_text_items" for attempt in impossible)
    assert all(attempt["cleanup_complete"] is True for attempt in impossible)
    assert all(attempt["created_entity_ids"] == [] for attempt in impossible)
    assert all(attempt["removed_entity_ids"] == [] for attempt in impossible)
    assert all(
        attempt["proof"]["item_specific_proven_impossible"] is True
        and attempt["proof"]["source_item_id"] == "p1:page"
        and attempt["proof"]["attempted_type"] == attempt["attempted_type"]
        for attempt in impossible
    )
    assert list(zip(ladder, ladder[1:])) == [
        (attempted, following)
        for attempted, following in zip(
            [attempt["attempted_type"] for attempt in opts.text_delivery_attempts],
            [attempt["attempted_type"] for attempt in opts.text_delivery_attempts][1:],
        )
    ]
    final = opts.text_delivery_attempts[-1]
    assert final["outcome"] == "verified"
    assert final["final_type"] == "raster"
    assert final["created_entity_ids"] == ["PageRaster001"]
    assert info["entity_type"] == "raster"
    assert info["count"] == 1
    assert info["source_item_ids"] == ["p1:page"]
    assert opts.text_mode_fallbacks[-1]["requested"] == mode
    assert opts.text_mode_fallbacks[-1]["delivered"] == "raster"
    assert opts.text_mode_fallbacks[-1]["proof"]["attempted_types"] == ladder


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
    assert core._raster_page_requires_text_contract_probe(explicit_raster) is False
    assert core._raster_page_requires_text_contract_probe(no_text_contract) is False
