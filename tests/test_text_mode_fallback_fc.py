"""Proof-gated text fallback and report-honesty contracts for FreeCAD."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402


def _write_report(tmp_path, opts, entity_info=None):
    if entity_info is not None:
        opts._report_extra = {"actual_text_entity_types": dict(entity_info)}
    report_path = tmp_path / "import_report.json"
    core.write_import_report(
        pdf_path=str(tmp_path / "fixture.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=int((entity_info or {}).get("count", 0) or 0),
        elapsed_ms=1.0,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def _fallback_item(*, requested="labels"):
    return {
        "importer_identity": "bluecollarsystems.freecad.pdf_vector_importer",
        "pdf_sha256": "a" * 64,
        "page_number": 7,
        "source_item_id": "p7:b2:l1:s3",
        "requested_type": requested,
        "font_identity": {
            "raw_name": "FixtureFont-Regular",
            "normalized_key": "fixturefontregular",
        },
    }


def _verified_result(item, mode, *, entity_id=None):
    return {
        "outcome": "verified",
        "source_item_id": item["source_item_id"],
        "attempted_type": mode,
        "final_type": mode,
        "created_entity_ids": [entity_id or f"{mode}-entity"],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "evidence": {"host_entity_verified": True},
    }


def _impossibility_proof(item, requested, attempted, **overrides):
    proof = {
        "item_specific_proven_impossible": True,
        "importer_identity": item["importer_identity"],
        "pdf_sha256": item["pdf_sha256"],
        "page_number": item["page_number"],
        "source_item_id": item["source_item_id"],
        "requested_type": requested,
        "attempted_type": attempted,
        "reason_code": "exact_font_unavailable",
        "evidence": {"normalized_key": item["font_identity"]["normalized_key"]},
        "attempted_source_results": [
            {
                "source": "embedded_font",
                "outcome": "not_found",
                "font_identity": dict(item["font_identity"]),
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "staging_complete": True,
            },
            {
                "source": "system_font",
                "outcome": "not_found",
                "font_identity": dict(item["font_identity"]),
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "staging_complete": True,
            },
        ],
        "attempted_sources_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "font_identity": dict(item["font_identity"]),
    }
    proof.update(overrides)
    return proof


def _impossible_attempt(item, requested, attempted, proof):
    return {
        "source_item_id": item["source_item_id"],
        "requested_type": requested,
        "attempted_type": attempted,
        "final_type": None,
        "outcome": "proven_impossible",
        "reason_code": proof["reason_code"],
        "created_entity_ids": list(proof["created_entity_ids"]),
        "removed_entity_ids": list(proof["removed_entity_ids"]),
        "cleanup_complete": proof["cleanup_complete"],
    }


def _report_fallback_proof(source_item_id, requested):
    return {
        "item_specific_proven_impossible": True,
        "importer_identity": "bluecollarsystems.freecad.pdf_vector_importer",
        "pdf_sha256": "b" * 64,
        "page_number": 1,
        "source_item_id": source_item_id,
        "requested_type": requested,
        "attempted_type": requested,
        "reason_code": "requested_type_unavailable",
        "evidence": {"exact_source_inspection": True},
        "attempted_types": [requested],
        "attempted_source_results": [
            {"source": "exact_source", "outcome": "not_found"}
        ],
        "attempted_sources_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }


def _report_terminal_attempt(source_item_id, requested, final_type):
    entity_id = f"{source_item_id}:{final_type}:entity"
    return {
        "source_item_id": source_item_id,
        "requested_type": requested,
        "attempted_type": final_type,
        "final_type": final_type,
        "outcome": "verified",
        "created_entity_ids": [entity_id],
        "delivery_entity_ids": [entity_id],
        "support_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "evidence": {"host_entity_verified": True},
    }


def test_generic_failure_cannot_authorize_a_representation_fallback():
    opts = core.ImportOptions(text_mode="3d_text")

    with pytest.raises(ValueError, match="proven impossible"):
        core._record_text_mode_fallback(
            opts,
            requested="3d_text",
            delivered="labels",
            reason="ShapeString raised RuntimeError",
            count=1,
            source_item_id="p1:b0:l0:s0",
            proof={
                "item_specific_proven_impossible": False,
                "evidence": "generic host exception",
            },
        )

    assert opts.text_mode_fallbacks == []


def test_item_specific_proven_impossibility_can_record_one_exact_fallback():
    opts = core.ImportOptions(text_mode="3d_text")
    proof = _report_fallback_proof("p1:b4:l2:s0", "3d_text")

    core._record_text_mode_fallback(
        opts,
        requested="3d_text",
        delivered="glyphs",
        reason="source glyph has no valid native text outline",
        count=1,
        source_item_id="p1:b4:l2:s0",
        proof=proof,
    )

    assert opts.text_mode_fallbacks == [{
        "requested": "3d_text",
        "delivered": "glyphs",
        "reason": "source glyph has no valid native text outline",
        "count": 1,
        "source_item_ids": ["p1:b4:l2:s0"],
        "proof": proof,
    }]


def test_fallback_record_rejects_proof_bound_to_a_different_source_item():
    opts = core.ImportOptions(text_mode="glyphs")
    proof = _report_fallback_proof("p1:b0:l0:different", "glyphs")

    with pytest.raises(ValueError, match="source item id"):
        core._record_text_mode_fallback(
            opts,
            requested="glyphs",
            delivered="raster",
            reason="exact source outline is unavailable",
            count=1,
            source_item_id="p1:b0:l0:actual",
            proof=proof,
        )

    assert opts.text_mode_fallbacks == []


def test_report_never_fabricates_fallback_to_hide_unexplained_mode_divergence(tmp_path):
    opts = core.ImportOptions(text_mode="3d_text")

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "labels",
            "count": 2,
            "font_rendered": True,
            "examples": [],
        },
    )

    assert "text" not in report["fallback"]
    violation = report["extra"]["representation_contract_violation"]
    assert violation["requested_type"] == "3d_text"
    assert violation["delivered_type"] == "labels"
    assert violation["reason"] == "unproven_representation_substitution"


def test_report_includes_exact_attempt_ledger_and_proven_fallback(tmp_path):
    opts = core.ImportOptions(text_mode="3d_text")
    opts.text_delivery_attempts.extend([{
        "source_item_id": "p1:b0:l0:s0",
        "requested_type": "3d_text",
        "attempted_type": "3d_text",
        "final_type": None,
        "outcome": "proven_impossible",
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }, _report_terminal_attempt("p1:b0:l0:s0", "3d_text", "glyphs")])
    core._record_text_mode_fallback(
        opts,
        requested="3d_text",
        delivered="glyphs",
        reason="source glyph has no native text outline",
        count=1,
        source_item_id="p1:b0:l0:s0",
        proof=_report_fallback_proof("p1:b0:l0:s0", "3d_text"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "glyphs",
            "count": 1,
            "source_item_count": 1,
            "source_item_ids": ["p1:b0:l0:s0"],
            "font_rendered": False,
            "examples": [],
        },
    )

    assert report["fallback"]["text"]["requested"] == "3d_text"
    assert report["fallback"]["text"]["delivered"] == "glyphs"
    assert report["fallback"]["text"]["source_item_ids"] == ["p1:b0:l0:s0"]
    assert report["extra"]["text_delivery_attempts"] == opts.text_delivery_attempts


def test_report_accepts_mixed_requested_and_proven_fallback_delivery(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 5,
        "raster_text_patch": 2,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:b0:l0:requested", "glyphs", "glyphs"),
        _report_terminal_attempt("p1:b0:l0:s0", "glyphs", "raster"),
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="two exact source items have no usable outline",
        count=2,
        source_item_id="p1:b0:l0:s0",
        proof=_report_fallback_proof("p1:b0:l0:s0", "glyphs"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "glyphs",
            "count": 7,
            "source_item_count": 2,
            "source_item_ids": ["p1:b0:l0:requested", "p1:b0:l0:s0"],
            "font_rendered": False,
            "examples": [],
        },
    )

    actual = report["extra"]["actual_text_entity_types"]
    assert actual["entity_type"] == "mixed"
    assert actual["outline_curve_or_mesh"] == 5
    assert actual["raster_text_patch"] == 2
    assert "representation_contract_violation" not in report["extra"]


def test_report_rejects_mixed_delivery_with_any_unproven_type(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "raw_geometry_edges": 5,
        "raster_text_patch": 2,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:b0:l0:unproven", "glyphs", "geometry"),
        _report_terminal_attempt("p1:b0:l0:s0", "glyphs", "raster"),
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="two exact source items have no usable outline",
        count=2,
        source_item_id="p1:b0:l0:s0",
        proof=_report_fallback_proof("p1:b0:l0:s0", "glyphs"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "glyphs",
            "count": 7,
            "source_item_count": 2,
            "source_item_ids": ["p1:b0:l0:unproven", "p1:b0:l0:s0"],
            "font_rendered": False,
            "examples": [],
        },
    )

    assert report["extra"]["representation_contract_violation"] == {
        "requested_type": "glyphs",
        "delivered_type": "mixed",
        "delivered_count": 7,
        "reason": "unproven_representation_substitution",
        "unproven_delivered_types": ["geometry"],
        "unproven_source_item_ids": ["p1:b0:l0:unproven"],
        "mixed_delivery_ledger_complete": True,
    }


def test_report_accepts_multiple_proven_fallback_types_without_requested_type(tmp_path):
    opts = core.ImportOptions(text_mode="3d_text")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 5,
        "native_text": 2,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:b0:l0:s0", "3d_text", "glyphs"),
        _report_terminal_attempt("p1:b0:l0:s1", "3d_text", "text"),
    ])
    for index, delivered in enumerate(("glyphs", "text")):
        core._record_text_mode_fallback(
            opts,
            requested="3d_text",
            delivered=delivered,
            reason=f"exact source item requires {delivered}",
            count=5 if delivered == "glyphs" else 2,
            source_item_id=f"p1:b0:l0:s{index}",
            proof=_report_fallback_proof(f"p1:b0:l0:s{index}", "3d_text"),
        )

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "3d_text",
            "count": 7,
            "source_item_count": 2,
            "source_item_ids": ["p1:b0:l0:s0", "p1:b0:l0:s1"],
            "font_rendered": True,
            "examples": [],
        },
    )

    assert report["extra"]["actual_text_entity_types"]["entity_type"] == "mixed"
    assert "representation_contract_violation" not in report["extra"]


def test_mixed_report_rejects_unproven_peer_with_same_fallback_type(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 5,
        "raster_text_patch": 2,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:b0:l0:requested", "glyphs", "glyphs"),
        _report_terminal_attempt("p1:b0:l0:proven", "glyphs", "raster"),
        _report_terminal_attempt("p1:b0:l0:unproven", "glyphs", "raster"),
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="one exact source item has no usable outline",
        count=1,
        source_item_id="p1:b0:l0:proven",
        proof=_report_fallback_proof("p1:b0:l0:proven", "glyphs"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "glyphs",
            "count": 7,
            "source_item_count": 3,
            "source_item_ids": [
                "p1:b0:l0:requested",
                "p1:b0:l0:proven",
                "p1:b0:l0:unproven",
            ],
            "font_rendered": False,
            "examples": [],
        },
    )

    violation = report["extra"]["representation_contract_violation"]
    assert violation["requested_type"] == "glyphs"
    assert violation["delivered_type"] == "mixed"
    assert violation["unproven_source_item_ids"] == ["p1:b0:l0:unproven"]


def test_report_rejects_incomplete_verified_terminal_rows(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 1,
        "raster_text_patch": 1,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:requested", "glyphs", "glyphs"),
        {
            "source_item_id": "p1:fallback",
            "requested_type": "glyphs",
            "attempted_type": "raster",
            "final_type": "raster",
            "outcome": "verified",
        },
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="exact source outline is unavailable",
        count=1,
        source_item_id="p1:fallback",
        proof=_report_fallback_proof("p1:fallback", "glyphs"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {"entity_type": "glyphs", "count": 2, "examples": []},
    )

    violation = report["extra"]["representation_contract_violation"]
    assert violation["mixed_delivery_ledger_complete"] is False


def test_report_rejects_fallback_delivery_without_authoritative_source_roster(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 1,
        "raster_text_patch": 1,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:requested", "glyphs", "glyphs"),
        _report_terminal_attempt("p1:proven", "glyphs", "raster"),
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="exact source outline is unavailable",
        count=1,
        source_item_id="p1:proven",
        proof=_report_fallback_proof("p1:proven", "glyphs"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {"entity_type": "glyphs", "count": 2, "examples": []},
    )

    violation = report["extra"]["representation_contract_violation"]
    assert violation["mixed_delivery_ledger_complete"] is False


def test_homogeneous_fallback_rejects_unproven_peer_of_same_type(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts["raster_text_patch"] = 2
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:proven", "glyphs", "raster"),
        _report_terminal_attempt("p1:unproven", "glyphs", "raster"),
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="one exact source item has no usable outline",
        count=1,
        source_item_id="p1:proven",
        proof=_report_fallback_proof("p1:proven", "glyphs"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {"entity_type": "raster", "count": 2, "examples": []},
    )

    violation = report["extra"]["representation_contract_violation"]
    assert violation["requested_type"] == "glyphs"
    assert violation["delivered_type"] == "raster"


def test_mixed_report_requires_every_reported_source_item_in_terminal_ledger(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 1,
        "raster_text_patch": 1,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:requested", "glyphs", "glyphs"),
        _report_terminal_attempt("p1:proven", "glyphs", "raster"),
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="one exact source item has no usable outline",
        count=1,
        source_item_id="p1:proven",
        proof=_report_fallback_proof("p1:proven", "glyphs"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "glyphs",
            "count": 2,
            "source_item_count": 3,
            "source_item_ids": ["p1:requested", "p1:proven", "p1:missing"],
            "examples": [],
        },
    )

    violation = report["extra"]["representation_contract_violation"]
    assert violation["mixed_delivery_ledger_complete"] is False


def test_two_page_text_roster_preserves_requested_and_proven_fallback_items(tmp_path):
    page_one = {
        "entity_type": "glyphs",
        "count": 1,
        "source_item_count": 1,
        "source_item_ids": ["p1:requested"],
        "examples": ["page one"],
    }
    page_two = {
        "entity_type": "raster",
        "count": 1,
        "source_item_count": 1,
        "source_item_ids": ["p2:proven"],
        "examples": ["page two"],
    }
    merged = core._merge_text_entity_info(None, page_one)
    merged = core._merge_text_entity_info(merged, page_two)

    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 1,
        "raster_text_patch": 1,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:requested", "glyphs", "glyphs"),
        _report_terminal_attempt("p2:proven", "glyphs", "raster"),
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="page two source outline is unavailable",
        count=1,
        source_item_id="p2:proven",
        proof=_report_fallback_proof("p2:proven", "glyphs"),
    )

    report = _write_report(tmp_path, opts, merged)

    actual = report["extra"]["actual_text_entity_types"]
    assert actual["source_item_count"] == 2
    assert actual["source_item_ids"] == ["p1:requested", "p2:proven"]
    assert "representation_contract_violation" not in report["extra"]


def test_source_free_page_raster_pseudo_item_is_part_of_delivery_roster(tmp_path):
    page_one = {
        "entity_type": "glyphs",
        "count": 1,
        "source_item_count": 1,
        "source_item_ids": ["p1:requested"],
        "examples": [],
    }
    page_two = {
        "entity_type": "raster",
        "count": 1,
        "source_item_count": 0,
        "source_item_ids": ["p2:page"],
        "examples": [],
    }
    merged = core._merge_text_entity_info(None, page_one)
    merged = core._merge_text_entity_info(merged, page_two)

    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 1,
        "raster_text_patch": 1,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:requested", "glyphs", "glyphs"),
        _report_terminal_attempt("p2:page", "glyphs", "raster"),
    ])
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="page two contains no source text items",
        count=1,
        source_item_id="p2:page",
        proof=_report_fallback_proof("p2:page", "glyphs"),
    )

    report = _write_report(tmp_path, opts, merged)

    actual = report["extra"]["actual_text_entity_types"]
    assert actual["source_item_count"] == 1
    assert actual["source_item_ids"] == ["p1:requested", "p2:page"]
    assert "representation_contract_violation" not in report["extra"]


def test_fallback_terminal_cannot_be_hidden_by_requested_only_delivery_bucket(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts["outline_curve_or_mesh"] = 1
    opts.text_delivery_attempts.append(
        _report_terminal_attempt("p1:fallback", "glyphs", "raster")
    )
    core._record_text_mode_fallback(
        opts,
        requested="glyphs",
        delivered="raster",
        reason="exact source outline is unavailable",
        count=1,
        source_item_id="p1:fallback",
        proof=_report_fallback_proof("p1:fallback", "glyphs"),
    )

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "glyphs",
            "count": 1,
            "source_item_count": 1,
            "source_item_ids": ["p1:fallback"],
            "examples": [],
        },
    )

    violation = report["extra"]["representation_contract_violation"]
    assert violation["requested_type"] == "glyphs"
    assert violation["delivered_type"] == "glyphs"


def test_fallback_proof_source_must_have_matching_terminal_and_roster_item(tmp_path):
    opts = core.ImportOptions(text_mode="glyphs")
    opts.text_delivered_counts.update({
        "outline_curve_or_mesh": 1,
        "raster_text_patch": 1,
    })
    opts.text_delivery_attempts.extend([
        _report_terminal_attempt("p1:requested", "glyphs", "glyphs"),
        _report_terminal_attempt("p1:proven", "glyphs", "raster"),
    ])
    for source_item_id in ("p1:proven", "p1:phantom"):
        core._record_text_mode_fallback(
            opts,
            requested="glyphs",
            delivered="raster",
            reason=f"exact source outline is unavailable for {source_item_id}",
            count=1,
            source_item_id=source_item_id,
            proof=_report_fallback_proof(source_item_id, "glyphs"),
        )

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "glyphs",
            "count": 2,
            "source_item_count": 2,
            "source_item_ids": ["p1:requested", "p1:proven"],
            "examples": [],
        },
    )

    violation = report["extra"]["representation_contract_violation"]
    assert violation["unproven_source_item_ids"] == ["p1:phantom"]


def test_terminal_representation_failure_writes_a_failure_report(tmp_path):
    opts = core.ImportOptions(text_mode="geometry")
    opts.import_report_path = str(tmp_path / "terminal_failure.json")
    attempt = {
        "source_item_id": "p2:page",
        "requested_type": "geometry",
        "attempted_type": "geometry",
        "final_type": None,
        "outcome": "failed",
        "reason": "svg_renderer_unavailable",
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }
    opts.text_delivery_attempts.append(attempt)
    failure = core.TextRepresentationFailure(
        "geometry rendering failed: svg_renderer_unavailable",
        attempt,
    )

    report_path = core._write_terminal_representation_failure_report(
        pdf_path=str(tmp_path / "fixture.pdf"),
        opts=opts,
        total_pages=3,
        pages_imported=1,
        elapsed_ms=12.0,
        failure=failure,
    )
    report = json.loads(Path(report_path).read_text(encoding="utf-8"))

    assert report["extra"]["result_status"] == "failed"
    assert report["extra"]["terminal_failure"]["type"] == "TextRepresentationFailure"
    assert report["extra"]["terminal_failure"]["attempt"] == attempt
    assert report["extra"]["text_delivery_attempts"] == [attempt]
    assert "text" not in report["fallback"]


def test_generic_error_stops_without_calling_next_rung():
    item = _fallback_item(requested="labels")
    opts = core.ImportOptions(text_mode="labels")
    calls = []

    def fail_generic(delivered_item, mode, delivered_opts):
        assert delivered_item == item
        assert delivered_opts is opts
        calls.append(mode)
        raise RuntimeError("synthetic host failure")

    def next_rung(*_args):
        calls.append("3d_text")
        return _verified_result(item, "3d_text")

    with pytest.raises(core.TextRepresentationFailure, match="labels"):
        core._run_text_item_fallback_ladder(
            item,
            "labels",
            {"labels": fail_generic, "3d_text": next_rung},
            opts,
        )

    assert calls == ["labels"]
    assert opts.text_delivery_attempts[-1]["attempted_type"] == "labels"
    assert opts.text_delivery_attempts[-1]["outcome"] == "failed"
    assert opts.text_mode_fallbacks == []


def test_text_representation_failure_stops_without_calling_next_rung():
    item = _fallback_item(requested="labels")
    opts = core.ImportOptions(text_mode="labels")
    calls = []

    def fail_terminal(_item, mode, _opts):
        calls.append(mode)
        raise core.TextRepresentationFailure(
            "already terminal",
            {
                "source_item_id": item["source_item_id"],
                "requested_type": "labels",
                "attempted_type": "labels",
                "final_type": None,
                "outcome": "failed",
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
        )

    def next_rung(*_args):
        calls.append("3d_text")
        return _verified_result(item, "3d_text")

    with pytest.raises(core.TextRepresentationFailure, match="already terminal"):
        core._run_text_item_fallback_ladder(
            item,
            "labels",
            {"labels": fail_terminal, "3d_text": next_rung},
            opts,
        )

    assert calls == ["labels"]
    assert opts.text_mode_fallbacks == []


@pytest.mark.parametrize(
    "invalid_case",
    [
        "none",
        "empty",
        "unverified",
        "mismatched_source",
        "mismatched_attempted",
        "mismatched_final",
        "mismatched_requested",
        "empty_created_ids",
        "duplicate_created_ids",
        "all_created_ids_removed",
        "unowned_removed_ids",
        "incomplete_cleanup",
        "empty_evidence",
    ],
)
def test_empty_or_unverified_result_stops_without_fallback(invalid_case):
    item = _fallback_item(requested="labels")
    opts = core.ImportOptions(text_mode="labels")
    calls = []
    result = _verified_result(item, "labels")
    if invalid_case == "none":
        result = None
    elif invalid_case == "empty":
        result = {}
    elif invalid_case == "unverified":
        result["outcome"] = "created"
    elif invalid_case == "mismatched_source":
        result["source_item_id"] = "p7:b2:l1:s999"
    elif invalid_case == "mismatched_attempted":
        result["attempted_type"] = "geometry"
    elif invalid_case == "mismatched_final":
        result["final_type"] = "geometry"
    elif invalid_case == "mismatched_requested":
        result["requested_type"] = "geometry"
    elif invalid_case == "empty_created_ids":
        result["created_entity_ids"] = []
    elif invalid_case == "duplicate_created_ids":
        result["created_entity_ids"] = ["Label001", "Label001"]
    elif invalid_case == "all_created_ids_removed":
        result["removed_entity_ids"] = list(result["created_entity_ids"])
    elif invalid_case == "unowned_removed_ids":
        result["removed_entity_ids"] = ["Unowned001"]
    elif invalid_case == "incomplete_cleanup":
        result["cleanup_complete"] = False
    elif invalid_case == "empty_evidence":
        result["evidence"] = {}

    def invalid_result(_item, mode, _opts):
        calls.append(mode)
        return result

    def next_rung(*_args):
        calls.append("3d_text")
        return _verified_result(item, "3d_text")

    with pytest.raises(core.TextRepresentationFailure):
        core._run_text_item_fallback_ladder(
            item,
            "labels",
            {"labels": invalid_result, "3d_text": next_rung},
            opts,
        )

    assert calls == ["labels"]
    assert opts.text_mode_fallbacks == []


def test_exact_bound_proof_advances_to_only_the_next_rung():
    item = _fallback_item(requested="3d_text")
    opts = core.ImportOptions(text_mode="3d_text")
    proof = _impossibility_proof(item, "3d_text", "3d_text")
    attempt = _impossible_attempt(item, "3d_text", "3d_text", proof)
    calls = []

    def text_3d_impossible(_item, mode, _opts):
        calls.append(mode)
        raise core.TextItemImpossible(
            "The exact source font is unavailable for this item",
            attempt=attempt,
            proof=proof,
        )

    def glyphs_verified(_item, mode, _opts):
        calls.append(mode)
        return _verified_result(item, mode, entity_id="Glyph001")

    def unexpected_rung(_item, mode, _opts):
        calls.append(mode)
        pytest.fail(f"executor skipped directly to unexpected rung {mode}")

    result = core._run_text_item_fallback_ladder(
        item,
        "3d_text",
        {
            "3d_text": text_3d_impossible,
            "glyphs": glyphs_verified,
            "geometry": unexpected_rung,
            "labels": unexpected_rung,
            "raster": unexpected_rung,
        },
        opts,
    )

    assert calls == ["3d_text", "glyphs"]
    assert result["requested_type"] == "3d_text"
    assert result["attempted_type"] == "glyphs"
    assert result["final_type"] == "glyphs"
    assert result["attempted_types"] == ["3d_text", "glyphs"]
    assert result["proof_chain"] == [proof]
    assert [entry["outcome"] for entry in opts.text_delivery_attempts] == [
        "proven_impossible",
        "verified",
    ]
    assert [entry["attempted_type"] for entry in opts.text_delivery_attempts] == [
        "3d_text",
        "glyphs",
    ]
    assert len(opts.text_mode_fallbacks) == 1
    event = opts.text_mode_fallbacks[0]
    assert event["requested"] == "3d_text"
    assert event["delivered"] == "glyphs"
    assert event["source_item_ids"] == [item["source_item_id"]]
    assert event["proof"]["importer_identity"] == item["importer_identity"]
    assert event["proof"]["proof_chain"] == [proof]


@pytest.mark.parametrize("requested", ["labels", "3d_text"])
def test_exact_3d_font_proof_schema_accepts_native_text_ladder_requests(requested):
    item = _fallback_item(requested=requested)
    proof = _impossibility_proof(item, requested, "3d_text")

    assert core._validate_item_impossibility_proof(
        item,
        requested,
        "3d_text",
        proof,
    ) == proof


def test_incomplete_cleanup_stops_even_with_otherwise_valid_proof():
    item = _fallback_item(requested="3d_text")
    opts = core.ImportOptions(text_mode="3d_text")
    proof = _impossibility_proof(
        item,
        "3d_text",
        "3d_text",
        created_entity_ids=["Owned001"],
        removed_entity_ids=[],
        cleanup_complete=True,
    )
    attempt = _impossible_attempt(item, "3d_text", "3d_text", proof)
    calls = []

    def incomplete_cleanup(_item, mode, _opts):
        calls.append(mode)
        raise core.TextItemImpossible(
            "3D Text is impossible but cleanup is incomplete",
            attempt=attempt,
            proof=proof,
        )

    def next_rung(*_args):
        calls.append("glyphs")
        return _verified_result(item, "glyphs")

    with pytest.raises(core.TextRepresentationFailure, match="proof|cleanup"):
        core._run_text_item_fallback_ladder(
            item,
            "3d_text",
            {"3d_text": incomplete_cleanup, "glyphs": next_rung},
            opts,
        )

    assert calls == ["3d_text"]
    assert opts.text_mode_fallbacks == []


@pytest.mark.parametrize(
    "invalid_case",
    [
        "importer_identity",
        "pdf_sha256",
        "page_number",
        "source_item_id",
        "requested_type",
        "attempted_type",
        "reason_code",
        "evidence",
        "attempted_source_results",
        "attempted_sources_complete",
        "font_identity",
        "meaningless_source_result",
        "one_source_result",
        "duplicate_source_result",
        "unsupported_source_outcome",
        "mismatched_source_font_identity",
        "mismatched_source_pdf_sha256",
        "mismatched_source_page_number",
        "incomplete_source_staging",
        "missing_raw_name_binding",
        "missing_normalized_key_binding",
    ],
)
def test_inexact_or_incomplete_impossibility_proof_stops_without_fallback(
    invalid_case,
):
    item = _fallback_item(requested="3d_text")
    opts = core.ImportOptions(text_mode="3d_text")
    proof = _impossibility_proof(item, "3d_text", "3d_text")
    attempt = _impossible_attempt(item, "3d_text", "3d_text", proof)
    invalid_values = {
        "importer_identity": "another.importer",
        "pdf_sha256": "b" * 64,
        "page_number": 8,
        "source_item_id": "p8:b2:l1:s3",
        "requested_type": "labels",
        "attempted_type": "glyphs",
        "reason_code": "",
        "evidence": {},
        "attempted_source_results": [],
        "attempted_sources_complete": False,
        "font_identity": {},
    }
    if invalid_case == "meaningless_source_result":
        proof["attempted_source_results"] = [{"anything": True}, {"other": True}]
    elif invalid_case == "one_source_result":
        proof["attempted_source_results"] = proof["attempted_source_results"][:1]
    elif invalid_case == "duplicate_source_result":
        proof["attempted_source_results"][1]["source"] = "embedded_font"
    elif invalid_case == "unsupported_source_outcome":
        proof["attempted_source_results"][0]["outcome"] = "unsupported"
    elif invalid_case == "mismatched_source_font_identity":
        proof["attempted_source_results"][0]["font_identity"] = {
            "raw_name": "OtherFont",
            "normalized_key": "otherfont",
        }
    elif invalid_case == "mismatched_source_pdf_sha256":
        proof["attempted_source_results"][0]["pdf_sha256"] = "b" * 64
    elif invalid_case == "mismatched_source_page_number":
        proof["attempted_source_results"][1]["page_number"] = 8
    elif invalid_case == "incomplete_source_staging":
        proof["attempted_source_results"][0]["staging_complete"] = False
    elif invalid_case in {"missing_raw_name_binding", "missing_normalized_key_binding"}:
        missing_key = (
            "raw_name"
            if invalid_case == "missing_raw_name_binding"
            else "normalized_key"
        )
        item["font_identity"].pop(missing_key)
        proof["font_identity"].pop(missing_key)
        for source_result in proof["attempted_source_results"]:
            source_result["font_identity"].pop(missing_key)
    else:
        proof[invalid_case] = invalid_values[invalid_case]
    calls = []

    def invalid_proof(_item, mode, _opts):
        calls.append(mode)
        raise core.TextItemImpossible(
            "invalid proof must not authorize fallback",
            attempt=attempt,
            proof=proof,
        )

    def next_rung(*_args):
        calls.append("glyphs")
        return _verified_result(item, "glyphs")

    with pytest.raises(core.TextRepresentationFailure, match="proof"):
        core._run_text_item_fallback_ladder(
            item,
            "3d_text",
            {"3d_text": invalid_proof, "glyphs": next_rung},
            opts,
        )

    assert calls == ["3d_text"]
    assert opts.text_mode_fallbacks == []


@pytest.mark.parametrize(
    "reason_code",
    [
        "placement_failure",
        "placementFailure",
        "rotation_failure",
        "scale_failure",
        "runtime_failure",
        "renderer_error",
        "generic_failure",
        "source_representation_unsupported",
    ],
)
def test_layout_or_generic_failure_reason_cannot_authorize_substitution(reason_code):
    item = _fallback_item(requested="3d_text")
    opts = core.ImportOptions(text_mode="3d_text")
    proof = _impossibility_proof(
        item,
        "3d_text",
        "3d_text",
        reason_code=reason_code,
    )
    attempt = _impossible_attempt(item, "3d_text", "3d_text", proof)
    calls = []

    def invalid_reason(_item, mode, _opts):
        calls.append(mode)
        raise core.TextItemImpossible(
            "layout defects cannot authorize representation substitution",
            attempt=attempt,
            proof=proof,
        )

    def next_rung(*_args):
        calls.append("glyphs")
        return _verified_result(item, "glyphs")

    with pytest.raises(core.TextRepresentationFailure, match="proof"):
        core._run_text_item_fallback_ladder(
            item,
            "3d_text",
            {"3d_text": invalid_reason, "glyphs": next_rung},
            opts,
        )

    assert calls == ["3d_text"]
    assert opts.text_mode_fallbacks == []


def test_each_rung_receives_an_unmutated_source_item_snapshot():
    item = _fallback_item(requested="3d_text")
    item["text"] = "ORIGINAL"
    opts = core.ImportOptions(text_mode="3d_text")
    calls = []

    def mutate_first_rung(delivered_item, mode, _opts):
        calls.append(mode)
        delivered_item["text"] = "MUTATED"
        proof = _impossibility_proof(delivered_item, "3d_text", "3d_text")
        attempt = _impossible_attempt(
            delivered_item,
            "3d_text",
            "3d_text",
            proof,
        )
        raise core.TextItemImpossible(
            "exact source font unavailable",
            attempt=attempt,
            proof=proof,
        )

    def verify_second_rung(delivered_item, mode, _opts):
        calls.append(mode)
        assert delivered_item["text"] == "ORIGINAL"
        return _verified_result(delivered_item, mode)

    result = core._run_text_item_fallback_ladder(
        item,
        "3d_text",
        {"3d_text": mutate_first_rung, "glyphs": verify_second_rung},
        opts,
    )

    assert calls == ["3d_text", "glyphs"]
    assert item["text"] == "ORIGINAL"
    assert result["final_type"] == "glyphs"


def test_deliverer_cannot_rebind_proof_by_mutating_item_identity():
    item = _fallback_item(requested="3d_text")
    opts = core.ImportOptions(text_mode="3d_text")
    calls = []

    def mutate_then_claim_impossible(delivered_item, mode, _opts):
        calls.append(mode)
        delivered_item.update(
            {
                "pdf_sha256": "b" * 64,
                "page_number": 8,
                "source_item_id": "p8:b9:l9:s9",
            }
        )
        proof = _impossibility_proof(delivered_item, "3d_text", "3d_text")
        attempt = _impossible_attempt(
            delivered_item,
            "3d_text",
            "3d_text",
            proof,
        )
        raise core.TextItemImpossible(
            "mutated identity must not become the proof boundary",
            attempt=attempt,
            proof=proof,
        )

    def next_rung(*_args):
        calls.append("glyphs")
        return _verified_result(item, "glyphs")

    with pytest.raises(core.TextRepresentationFailure, match="proof"):
        core._run_text_item_fallback_ladder(
            item,
            "3d_text",
            {"3d_text": mutate_then_claim_impossible, "glyphs": next_rung},
            opts,
        )

    assert calls == ["3d_text"]
    assert opts.text_mode_fallbacks == []


def test_missing_canonical_item_importer_identity_stops_before_delivery():
    item = _fallback_item(requested="geometry")
    item.pop("importer_identity")
    opts = core.ImportOptions(text_mode="geometry")
    calls = []

    def deliver(*_args):
        calls.append("geometry")
        return _verified_result(item, "geometry")

    with pytest.raises(core.TextRepresentationFailure, match="Invalid"):
        core._run_text_item_fallback_ladder(
            item,
            "geometry",
            {"geometry": deliver},
            opts,
        )

    assert calls == []
    assert opts.text_mode_fallbacks == []


def test_every_requested_mode_has_a_finite_noncyclic_ladder_ending_raster():
    expected = {
        "text": ("text", "labels", "3d_text", "glyphs", "geometry", "raster"),
        "labels": ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
        "3d_text": ("3d_text", "glyphs", "geometry", "text", "labels", "raster"),
        "glyphs": ("glyphs", "geometry", "raster"),
        "geometry": ("geometry", "glyphs", "raster"),
        "raster": ("raster",),
    }

    assert core.TEXT_ITEM_FALLBACK_LADDERS == expected
    for requested, ladder in core.TEXT_ITEM_FALLBACK_LADDERS.items():
        assert ladder[0] == requested
        assert ladder[-1] == "raster"
        assert len(ladder) == len(set(ladder))
        assert len(ladder) <= 6


def test_outline_modes_do_not_cross_into_semantically_different_native_text():
    assert core.TEXT_ITEM_FALLBACK_LADDERS["glyphs"] == (
        "glyphs",
        "geometry",
        "raster",
    )
    assert core.TEXT_ITEM_FALLBACK_LADDERS["geometry"] == (
        "geometry",
        "glyphs",
        "raster",
    )


def test_requested_success_records_requested_equals_delivered():
    item = _fallback_item(requested="geometry")
    opts = core.ImportOptions(text_mode="geometry")
    calls = []

    def verified(_item, mode, _opts):
        calls.append(mode)
        return _verified_result(item, mode, entity_id="Geometry001")

    result = core._run_text_item_fallback_ladder(
        item,
        "geometry",
        {"geometry": verified},
        opts,
    )

    assert calls == ["geometry"]
    assert result["requested_type"] == "geometry"
    assert result["attempted_type"] == "geometry"
    assert result["final_type"] == "geometry"
    assert result["attempted_types"] == ["geometry"]
    assert result["proof_chain"] == []
    assert opts.text_delivery_attempts == [result]
    assert opts.text_mode_fallbacks == []
