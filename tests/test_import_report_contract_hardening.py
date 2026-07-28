from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "PDFVectorImporter"))

from pdfcadcore.import_report import (  # noqa: E402
    TEXT_ENTITY_DELIVERED_BUCKETS,
    build_actual_text_entity_types,
    build_import_contract_ready,
    build_import_report,
)
from pdfcadcore.text_delivery_report import (  # noqa: E402
    build_text_representation_delivery,
    resolve_text_representation_delivery,
)
from pdfcadcore.page_visual import (  # noqa: E402
    build_page_visual_fallback_proof,
    build_page_visual_source_observation,
    page_visual_schema_trust,
    page_visual_source_observation_digest,
)


def _terminal_attempt(
    host: str,
    *,
    source_item_id: str = "p1:item:1",
    requested_type: str = "text",
    final_type: str = "text",
    entity_ids: list[str] | None = None,
    contribution: int = 1,
) -> dict:
    raw_entity_ids = list(entity_ids or [f"{host}:entity:1"])
    delivery_ids = (
        [f"blender:object:{entity_id}" for entity_id in raw_entity_ids]
        if host == "blender" and contribution > 0
        else list(raw_entity_ids)
    )
    attempt = {
        "source_item_id": source_item_id,
        "requested_type": requested_type,
        "attempted_type": final_type,
        "final_type": final_type,
        "outcome": "verified",
        "created_entity_ids": list(delivery_ids),
        "removed_entity_ids": [],
        "delivery_entity_ids": list(delivery_ids),
        "support_entity_ids": [],
        "referenced_entity_ids": [],
        "reused_entity_ids": [],
        "cleanup_complete": True,
        "record_verified": True,
        "type_verified": True,
        "visual_verified": True,
        "ownership_verified": True,
        "evidence": {"host_verification": True},
    }
    if host == "blender":
        attempt["host_record"] = {
            "item_id": source_item_id,
            "page": 1,
            "source_span_id": 1,
            "requested_representation": requested_type,
            "final_representation": final_type,
            "status": "delivered",
            "fallback_attempted": final_type != requested_type,
            "fallback_used": final_type != requested_type,
            "entity_ids": list(raw_entity_ids),
            "physical_entity_count": len(raw_entity_ids),
            "delivered_count_contribution": contribution,
        }
    return attempt


def _actual_payload(host: str, final_type: str, count: int = 1) -> dict:
    if host == "blender":
        bucket = {
            "labels": "native_label",
            "text": "native_text",
            "3d_text": "native_3d_text",
            "glyphs": "glyph_curve",
            "geometry": "geometry_mesh",
            "raster": "raster_patch",
        }[final_type]
    else:
        bucket = {
            "labels": "native_label",
            "text": "dxf_text",
            "3d_text": "native_3d_text",
            "glyphs": "outline_curve_or_mesh",
            "geometry": "raw_geometry_edges",
            "raster": "raster_image",
        }[final_type]
    payload = build_actual_text_entity_types(
        host_app=host,
        text_mode=final_type,
        delivered_counts={bucket: count},
    )
    payload["entity_type"] = final_type if count else "none"
    return payload


def _report(
    host: str,
    attempts: list[dict],
    *,
    requested_type: str = "text",
    actual: dict | None = None,
    resolved_scale: dict | None = None,
    page_visual_source_observations: dict | None = None,
):
    source_pdf_sha256 = "a" * 64
    import_session_id = "test-import-session-1"
    importer_identity = {
        "blender": "bc_pdf_vector_importer.blender",
        "freecad": "bluecollarsystems.freecad.pdf_vector_importer",
        "librecad": "bluecollarsystems.librecad.pdf_importer",
    }.get(host, "bluecollarsystems.unknown.pdf_vector_importer")
    source_ids = list(dict.fromkeys(a["source_item_id"] for a in attempts))
    required = bool(source_ids)
    delivery = build_text_representation_delivery(
        attempts,
        requested_type=requested_type,
        required=required,
        expected_source_item_ids=source_ids,
    )
    if actual is None:
        actual = build_actual_text_entity_types(
            host_app=host,
            text_mode=requested_type,
            delivered_counts={},
        )
    terminal_delivery_ids = sorted(
        entity_id
        for attempt in attempts
        if attempt.get("outcome") == "verified"
        for entity_id in list(attempt.get("delivery_entity_ids") or [])
    )
    persistence = None
    if host in {"blender", "librecad"}:
        delivery_entity_ids_sha256 = hashlib.sha256(
            json.dumps(
                terminal_delivery_ids,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        persistence = {
            "schema": "bcs.host_result_persistence/1.0",
            "host_app": host,
            "importer_identity": importer_identity,
            "source_pdf_sha256": source_pdf_sha256,
            "import_session_id": import_session_id,
            "method": {
                "blender": "blender_post_commit_scene_reinspection_sha256",
                "librecad": "librecad_atomic_dxf_write_reopen_sha256",
            }[host],
            "commit_complete": True,
            "persistence_verified": True,
            "artifact_reinspection_complete": True,
            "persistence_sha256": "e" * 64,
            "delivery_entity_ids_sha256": delivery_entity_ids_sha256,
            "observed_delivery_entity_ids_sha256": delivery_entity_ids_sha256,
        }
    extra = {
        "result_status": "success",
        "import_session_id": import_session_id,
        "resolved_scale": resolved_scale
        or {"factor": 1.0, "confidence": 1.0, "source": "titleblock"},
        "actual_text_entity_types": actual,
        "text_delivery_obligations": {
            "schema": "bcs.text_delivery_obligations/1.0",
            "required": required,
            "requested_type": requested_type,
            "source_item_ids": source_ids,
        },
        "text_delivery_attempts": attempts,
        "text_representation_delivery": delivery,
        "page_visual_source_observations": copy.deepcopy(
            page_visual_source_observations or {}
        ),
    }
    if persistence is not None:
        extra["host_result_persistence"] = persistence
    report = build_import_report(
        host_app=host,
        pdf_path="contract.pdf",
        text_mode=requested_type,
        text_count=int(actual.get("count") or 0),
        extra=extra,
    )
    report.input["sha256"] = source_pdf_sha256
    report.importer["identity"] = importer_identity
    return report


def test_compact_delivery_requires_exact_top_level_keys() -> None:
    delivery = build_text_representation_delivery(
        [], requested_type="text", required=False, expected_source_item_ids=[]
    )
    delivery["forged"] = True
    resolved = resolve_text_representation_delivery(
        [], delivery, expected_source_item_ids=[]
    )
    assert resolved["contract_valid"] is False
    assert resolved["verified"] is False


@pytest.mark.parametrize(
    "mutator",
    [
        lambda delivery: delivery.update(attempt_count=True),
        lambda delivery: delivery.update(attempt_count=1.0),
        lambda delivery: delivery["items"][0].update(
            terminal_attempt_index=False
        ),
        lambda delivery: delivery["items"][0].update(
            terminal_attempt_index=0.0
        ),
        lambda delivery: delivery["items"][0].update(verified=1),
    ],
)
def test_compact_delivery_rejects_recursive_nonexact_json_types(mutator) -> None:
    attempts = [_terminal_attempt("freecad")]
    delivery = build_text_representation_delivery(
        attempts,
        requested_type="text",
        expected_source_item_ids=["p1:item:1"],
    )
    mutator(delivery)

    resolved = resolve_text_representation_delivery(
        attempts,
        delivery,
        expected_source_item_ids=["p1:item:1"],
    )

    assert resolved["contract_valid"] is False
    assert resolved["verified"] is False


def _shared_ladder_attempts(sequence: list[str], requested: str) -> list[dict]:
    attempts = []
    for index, attempted_type in enumerate(sequence):
        if index == len(sequence) - 1:
            terminal = _terminal_attempt(
                "freecad",
                requested_type=requested,
                final_type=attempted_type,
            )
            attempts.append(terminal)
        else:
            attempts.append(
                {
                    "source_item_id": "p1:item:1",
                    "requested_type": requested,
                    "attempted_type": attempted_type,
                    "final_type": None,
                    "outcome": "proven_impossible",
                    "created_entity_ids": [],
                    "removed_entity_ids": [],
                    "delivery_entity_ids": [],
                    "support_entity_ids": [],
                    "referenced_entity_ids": [],
                    "reused_entity_ids": [],
                    "cleanup_complete": True,
                    "evidence": {"closed_impossibility": True},
                }
            )
    return attempts


@pytest.mark.parametrize(
    "sequence",
    [
        ["text", "raster"],
        ["text", "text"],
    ],
)
def test_shared_resolver_rejects_direct_or_repeated_fallbacks(sequence) -> None:
    attempts = _shared_ladder_attempts(sequence, "text")
    delivery = build_text_representation_delivery(
        attempts,
        requested_type="text",
        expected_source_item_ids=["p1:item:1"],
    )

    assert delivery["verified"] is False
    assert any("ladder" in reason for reason in delivery["invalid_reasons"])


def test_shared_resolver_uses_caller_bound_ladder_and_preserves_adjacent_step() -> None:
    fallback_ladders = {
        "text": ("text", "labels", "3d_text", "glyphs", "geometry", "raster"),
        "labels": ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
    }
    jumped_attempts = _shared_ladder_attempts(["text", "3d_text"], "text")
    jumped = build_text_representation_delivery(
        jumped_attempts,
        requested_type="text",
        expected_source_item_ids=["p1:item:1"],
        fallback_ladders=fallback_ladders,
    )
    adjacent_attempts = _shared_ladder_attempts(["labels", "text"], "labels")
    adjacent = build_text_representation_delivery(
        adjacent_attempts,
        requested_type="labels",
        expected_source_item_ids=["p1:item:1"],
        fallback_ladders=fallback_ladders,
    )

    assert jumped["verified"] is False
    assert adjacent["verified"] is True


@pytest.mark.parametrize(
    "status",
    [None, "", "SUCCESS", "partial", "delivered", True, 1, {}, []],
)
def test_readiness_requires_explicit_exact_success_status(status) -> None:
    report = _report("blender", [])
    if status is None:
        report.extra.pop("result_status")
    else:
        report.extra["result_status"] = status

    ready = build_import_contract_ready(report)

    assert ready["checks"]["result_succeeded"] is False
    assert ready["ready"] is False


@pytest.mark.parametrize(
    ("extra_status", "result_status"),
    [("success", "failed"), ("failed", "success")],
)
def test_readiness_rejects_conflicting_dual_status_authorities(
    extra_status, result_status
) -> None:
    report = _report("blender", [])
    report.extra["result_status"] = extra_status
    report.result["status"] = result_status

    ready = build_import_contract_ready(report)

    assert ready["checks"]["result_succeeded"] is False
    assert ready["ready"] is False


@pytest.mark.parametrize("host", ["blender", "librecad"])
def test_non_freecad_readiness_requires_bound_commit_and_persistence(host) -> None:
    report = _report(host, [])
    ready = build_import_contract_ready(report)
    assert ready["checks"]["host_result_persistence"] is True
    assert ready["checks"]["host_result_persistence_required"] is True
    assert ready["ready"] is True

    for field_name, wrong_value in (
        ("host_app", "freecad"),
        ("importer_identity", "untrusted.importer"),
        ("source_pdf_sha256", "f" * 64),
        ("import_session_id", "another-session"),
        ("method", "generic_sha256"),
        ("commit_complete", False),
        ("persistence_verified", False),
        ("artifact_reinspection_complete", False),
        ("persistence_sha256", "not-a-digest"),
        ("delivery_entity_ids_sha256", "0" * 64),
        ("observed_delivery_entity_ids_sha256", "0" * 64),
    ):
        tampered = copy.deepcopy(report)
        tampered.extra["host_result_persistence"][field_name] = wrong_value
        tampered_ready = build_import_contract_ready(tampered)
        assert tampered_ready["checks"]["host_result_persistence"] is False
        assert tampered_ready["ready"] is False

    missing = copy.deepcopy(report)
    missing.extra.pop("host_result_persistence")
    missing_ready = build_import_contract_ready(missing)
    assert missing_ready["checks"]["host_result_persistence"] is False
    assert missing_ready["ready"] is False


def test_obligations_require_exact_top_level_keys() -> None:
    report = _report("blender", [])
    report.extra["text_delivery_obligations"]["forged"] = True
    ready = build_import_contract_ready(report)
    assert ready["checks"]["text_delivery"] is False


@pytest.mark.parametrize(
    "delivered_counts",
    [
        {"native_text": True},
        {"native_text": 1.5},
        {"native_text": "2"},
        {"native_text": -1},
        {"unknown_bucket": 1},
        [1],
    ],
)
def test_delivered_counts_reject_nonexact_nonnegative_integer_maps(
    delivered_counts,
) -> None:
    payload = build_actual_text_entity_types(
        host_app="blender",
        text_mode="text",
        delivered_counts=delivered_counts,
    )
    assert payload["delivery_counts_valid"] is False
    assert payload["entity_type"] == "none"
    assert payload["count"] == 0
    assert payload["font_rendered"] is False
    assert all(payload[bucket] == 0 for bucket in TEXT_ENTITY_DELIVERED_BUCKETS)


def test_scale_crosscheck_accepts_clean_absence_and_rejects_injection_or_stale_data() -> None:
    clean = _report("blender", [])
    clean_ready = build_import_contract_ready(clean)
    assert "scale_crosscheck" not in clean.extra
    assert clean_ready["checks"]["scale_crosscheck"] is True
    assert clean_ready["ready"] is True

    clean.extra["scale_crosscheck"] = {}
    assert build_import_contract_ready(clean)["checks"]["scale_crosscheck"] is False

    warned = _report(
        "blender",
        [],
        resolved_scale={"factor": 1.0, "confidence": 0.2, "source": "page_text"},
    )
    assert warned.extra["scale_crosscheck"]["reasons"] == ["low_confidence"]
    assert build_import_contract_ready(warned)["checks"]["scale_crosscheck"] is True
    warned.extra["scale_crosscheck"]["reasons"] = ["stale"]
    assert build_import_contract_ready(warned)["checks"]["scale_crosscheck"] is False

    malformed = _report("blender", [])
    malformed.extra["resolved_scale"] = []
    assert build_import_contract_ready(malformed)["checks"]["scale_crosscheck"] is False

    empty_scale = _report("blender", [])
    empty_scale.extra["resolved_scale"] = {}
    assert build_import_contract_ready(empty_scale)["checks"]["scale_crosscheck"] is False

    stale = _report(
        "blender",
        [],
        resolved_scale={"factor": 1.0, "confidence": 0.2, "source": "page_text"},
    )
    stale.extra["resolved_scale"] = {
        "factor": 1.0,
        "confidence": 1.0,
        "source": "titleblock",
    }
    assert build_import_contract_ready(stale)["checks"]["scale_crosscheck"] is False


def test_host_specific_persistence_checks_are_truthful() -> None:
    ready = build_import_contract_ready(_report("librecad", []))
    assert ready["checks"]["host_object_inventory"] is False
    assert ready["checks"]["save_reopen_inventory"] is False
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["checks"]["host_object_inventory_required"] is False
    assert ready["checks"]["save_reopen_inventory_required"] is False
    assert ready["checks"]["delivery_inventory_binding_required"] is False
    assert ready["checks"]["host_result_persistence"] is True
    assert ready["checks"]["host_result_persistence_required"] is True
    assert ready["ready"] is True


@pytest.mark.parametrize("host", ["freecad", "blender", "librecad"])
def test_required_false_zero_contract_is_valid_for_every_host(host: str) -> None:
    ready = build_import_contract_ready(_report(host, []))
    assert ready["checks"]["actual_text_entity_types"] is True
    assert ready["checks"]["text_delivery"] is True
    if host != "freecad":
        assert ready["ready"] is True


def test_unknown_host_cannot_claim_a_ready_empty_contract() -> None:
    ready = build_import_contract_ready(_report("unknown-host", []))
    assert ready["checks"]["supported_host"] is False
    assert ready["checks"]["actual_text_entity_types"] is False
    assert ready["ready"] is False


def test_extraneous_page_source_observation_is_not_ignored() -> None:
    report = _report("blender", [])
    report.extra["page_visual_source_observations"] = {
        "page_visual:9": build_page_visual_source_observation(
            importer_identity="bc_pdf_vector_importer.blender",
            pdf_sha256="a" * 64,
            page_number=9,
            source_scope_id="page_visual:9",
            raw_text_dictionary={"blocks": []},
        )
    }
    assert build_import_contract_ready(report)["checks"]["text_delivery"] is False


@pytest.mark.parametrize(
    ("host", "final_type"),
    [("blender", "geometry"), ("librecad", "text")],
)
def test_non_freecad_actual_entity_types_reconcile_exact_terminal_truth(
    host: str, final_type: str
) -> None:
    attempt = _terminal_attempt(
        host,
        requested_type=final_type,
        final_type=final_type,
    )
    report = _report(
        host,
        [attempt],
        requested_type=final_type,
        actual=_actual_payload(host, final_type),
    )
    ready = build_import_contract_ready(report)
    assert ready["checks"]["actual_text_entity_types"] is True
    assert ready["checks"]["text_delivery"] is True
    assert ready["ready"] is True

    for field, value in (("count", 2), ("entity_type", "raster")):
        tampered = copy.deepcopy(report)
        tampered.extra["actual_text_entity_types"][field] = value
        assert (
            build_import_contract_ready(tampered)["checks"][
                "actual_text_entity_types"
            ]
            is False
        )

    wrong_bucket = copy.deepcopy(report)
    wrong_bucket.extra["actual_text_entity_types"]["native_text"] = 1
    assert (
        build_import_contract_ready(wrong_bucket)["checks"][
            "actual_text_entity_types"
        ]
        is False
    )

    unknown_bucket = copy.deepcopy(report)
    unknown_bucket.extra["actual_text_entity_types"]["unknown_bucket"] = 1
    assert (
        build_import_contract_ready(unknown_bucket)["checks"][
            "actual_text_entity_types"
        ]
        is False
    )


def test_blender_logical_zero_delivery_contributes_zero_entities_truthfully() -> None:
    source_item_id = "page:1:text:1"
    raw_logical_id = f"{source_item_id}:zero-ink:text"
    canonical_logical_id = f"blender:logical:{raw_logical_id}"
    attempt = _terminal_attempt(
        "blender",
        source_item_id=source_item_id,
        requested_type="text",
        final_type="text",
        entity_ids=[canonical_logical_id],
        contribution=0,
    )
    attempt["host_record"].update(
        {
            "entity_ids": [],
            "physical_entity_count": 0,
            "zero_ink_delivery": True,
            "logical_delivery_id": raw_logical_id,
            "zero_ink_character_count": 2,
            "source_manifest_sha256": "a" * 64,
            "zero_ink_delivery_manifest_sha256": "b" * 64,
        }
    )
    actual = build_actual_text_entity_types(
        host_app="blender", text_mode="text", delivered_counts={}
    )
    ready = build_import_contract_ready(
        _report("blender", [attempt], actual=actual)
    )
    assert ready["checks"]["actual_text_entity_types"] is True
    assert ready["checks"]["text_delivery"] is True
    assert ready["ready"] is True


def _sealed_prior(
    host: str = "librecad",
    *,
    requested_type: str = "labels",
    attempted_type: str = "labels",
) -> dict:
    from pdfcadcore.item_impossibility import (  # noqa: PLC0415
        EVIDENCE_KEY,
        build_item_representation_impossibility_proof,
    )

    branch_evidence = {"host_attempted": True}
    values = {
        "branch_evidence": branch_evidence,
        "source_item_id": "p1:item:1",
        "requested_type": requested_type,
        "attempted_type": attempted_type,
        "strategy": f"{host}_{attempted_type}",
        "reason": f"{attempted_type}_unavailable",
        "host_outcome": "impossible",
        "cleanup_complete": True,
        "created_entity_ids": [f"{host}:temporary:1"],
        "removed_entity_ids": [f"{host}:temporary:1"],
        "delivery_entity_ids": [],
        "support_entity_ids": [],
        "referenced_entity_ids": [],
        "reused_entity_ids": [],
        "owned_block_names": [],
    }
    evidence = dict(branch_evidence)
    evidence[EVIDENCE_KEY] = build_item_representation_impossibility_proof(
        **values
    )
    return {
        "source_item_id": values["source_item_id"],
        "requested_type": values["requested_type"],
        "attempted_type": values["attempted_type"],
        "final_type": None,
        "strategy": values["strategy"],
        "reason": values["reason"],
        "host_outcome": values["host_outcome"],
        "owned_block_names": values["owned_block_names"],
        "outcome": "proven_impossible",
        "cleanup_complete": values["cleanup_complete"],
        "created_entity_ids": values["created_entity_ids"],
        "removed_entity_ids": values["removed_entity_ids"],
        "delivery_entity_ids": values["delivery_entity_ids"],
        "support_entity_ids": values["support_entity_ids"],
        "referenced_entity_ids": values["referenced_entity_ids"],
        "reused_entity_ids": values["reused_entity_ids"],
        "evidence": evidence,
    }


def test_host_ladder_requires_exact_raw_prefix_and_generic_prior_seal() -> None:
    prior = _sealed_prior()
    terminal = _terminal_attempt(
        "librecad", requested_type="labels", final_type="text"
    )
    report = _report(
        "librecad",
        [prior, terminal],
        requested_type="labels",
        actual=_actual_payload("librecad", "text"),
    )
    assert build_import_contract_ready(report)["checks"]["text_delivery"] is True

    report.extra["text_delivery_attempts"][0]["evidence"][
        "host_attempted"
    ] = False
    assert build_import_contract_ready(report)["checks"]["text_delivery"] is False

    repeated = _report(
        "librecad",
        [prior, _terminal_attempt("librecad", requested_type="labels", final_type="labels")],
        requested_type="labels",
        actual=_actual_payload("librecad", "labels"),
    )
    assert build_import_contract_ready(repeated)["checks"]["text_delivery"] is False


def test_blender_accepts_a_sealed_exact_ladder_prefix() -> None:
    prior = _sealed_prior(
        "blender", requested_type="text", attempted_type="text"
    )
    terminal = _terminal_attempt(
        "blender", requested_type="text", final_type="3d_text"
    )
    ready = build_import_contract_ready(
        _report(
            "blender",
            [prior, terminal],
            requested_type="text",
            actual=_actual_payload("blender", "3d_text"),
        )
    )
    assert ready["checks"]["text_delivery"] is True
    assert ready["ready"] is True


def _unsealed_sequence_report(host: str, requested: str, sequence: list[str]):
    source_item_id = "p1:item:1"
    attempts = []
    for index, attempted_type in enumerate(sequence):
        if index == len(sequence) - 1:
            attempts.append(
                _terminal_attempt(
                    host,
                    source_item_id=source_item_id,
                    requested_type=requested,
                    final_type=attempted_type,
                )
            )
            continue
        attempts.append(
            {
                "source_item_id": source_item_id,
                "requested_type": requested,
                "attempted_type": attempted_type,
                "final_type": None,
                "strategy": attempted_type,
                "reason": "unsealed",
                "host_outcome": "impossible",
                "owned_block_names": [],
                "outcome": "proven_impossible",
                "cleanup_complete": True,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "delivery_entity_ids": [],
                "support_entity_ids": [],
                "referenced_entity_ids": [],
                "reused_entity_ids": [],
                "evidence": {"unsealed": True},
            }
        )
    return _report(
        host,
        attempts,
        requested_type=requested,
        actual=_actual_payload(host, sequence[-1]),
    )


@pytest.mark.parametrize(
    ("host", "requested", "sequence"),
    [
        ("blender", "text", ["text", "geometry"]),
        ("blender", "text", ["text", "text"]),
        ("blender", "text", ["text", "glyphs", "3d_text"]),
        ("librecad", "labels", ["labels", "glyphs"]),
        ("librecad", "labels", ["labels", "labels"]),
        ("librecad", "labels", ["labels", "glyphs", "text"]),
    ],
)
def test_host_ladders_reject_jump_repeat_and_reordering(
    host: str, requested: str, sequence: list[str]
) -> None:
    ready = build_import_contract_ready(
        _unsealed_sequence_report(host, requested, sequence)
    )
    assert ready["checks"]["text_delivery"] is False
    assert any(
        "ladder" in reason
        for reason in ready["text_delivery_invalid_reasons"]
    )


def test_v1_page_visual_proof_is_explicitly_legacy_untrusted() -> None:
    from pdfcadcore.page_visual import page_visual_proof_digest  # noqa: PLC0415

    digest = "a" * 64
    rawdict = {"blocks": [{"type": 1}]}
    source_item_id = "page_visual:1"
    importer_identity = "bluecollarsystems.librecad.pdf_importer"
    source_observation = build_page_visual_source_observation(
        importer_identity=importer_identity,
        pdf_sha256=digest,
        page_number=1,
        source_scope_id=source_item_id,
        raw_text_dictionary=rawdict,
    )
    raw_digest = source_observation["raw_text_dictionary_sha256"]

    def page_proof(attempted_type: str) -> dict:
        proof = {
            "schema": "pdf_page_visual_fallback_proof_v1",
            "page_specific_proven_impossible": True,
            "importer_identity": importer_identity,
            "pdf_sha256": digest,
            "page_number": 1,
            "source_item_id": source_item_id,
            "requested_type": "labels",
            "attempted_type": attempted_type,
            "reason_code": "no_canonical_text_source_items",
            "attempted_sources_complete": True,
            "cleanup_complete": True,
            "created_entity_ids": [],
            "removed_entity_ids": [],
            "evidence": {
                "text_dictionary_present": True,
                "canonical_source_item_count": 0,
                "source_item_ids": [],
                "source_scope": "page_visual",
                "visible_source_text_found": False,
                "raw_text_dictionary_sha256": raw_digest,
                "raw_text_block_count": 0,
            },
            "attempted_source_results": [
                {
                    "source": "pymupdf_raw_text_dictionary",
                    "outcome": "no_canonical_text_source_items",
                    "importer_identity": importer_identity,
                    "pdf_sha256": digest,
                    "page_number": 1,
                    "source_item_id": source_item_id,
                    "source_item_ids": [],
                    "canonical_source_item_count": 0,
                    "raw_text_dictionary_sha256": raw_digest,
                    "visible_source_text_found": False,
                }
            ],
        }
        proof["proof_sha256"] = page_visual_proof_digest(proof)
        return proof

    attempts = []
    for attempted_type in ("labels", "text", "glyphs", "geometry"):
        attempts.append(
            {
                "source_item_id": source_item_id,
                "requested_type": "labels",
                "attempted_type": attempted_type,
                "final_type": None,
                "outcome": "proven_impossible",
                "cleanup_complete": True,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "delivery_entity_ids": [],
                "support_entity_ids": [],
                "referenced_entity_ids": [],
                "reused_entity_ids": [],
                "evidence": {
                    "page_visual_importer_identity": importer_identity,
                    "page_visual_raw_text_dictionary_sha256": raw_digest,
                    "page_visual_fallback_proof": page_proof(attempted_type),
                },
            }
        )
    terminal = _terminal_attempt(
        "librecad",
        source_item_id=source_item_id,
        requested_type="labels",
        final_type="raster",
    )
    attempts.append(terminal)
    report = _report(
        "librecad",
        attempts,
        requested_type="labels",
        actual=_actual_payload("librecad", "raster"),
        page_visual_source_observations={
            source_item_id: source_observation
        },
    )
    report.input["sha256"] = digest
    assert page_visual_schema_trust(source_observation) == "legacy_untrusted"
    assert build_import_contract_ready(report)["checks"]["text_delivery"] is False

    proof = report.extra["text_delivery_attempts"][0]["evidence"][
        "page_visual_fallback_proof"
    ]
    proof["attempted_type"] = "geometry"
    proof["proof_sha256"] = page_visual_proof_digest(proof)
    assert build_import_contract_ready(report)["checks"]["text_delivery"] is False

    trusted_host_tamper = copy.deepcopy(report)
    tampered_observation = trusted_host_tamper.extra[
        "page_visual_source_observations"
    ][source_item_id]
    tampered_observation["importer_identity"] = "bc_pdf_vector_importer.blender"
    tampered_observation["observation_sha256"] = (
        page_visual_source_observation_digest(tampered_observation)
    )
    assert (
        build_import_contract_ready(trusted_host_tamper)["checks"][
            "text_delivery"
        ]
        is False
    )


def test_blender_v1_page_visual_scope_is_explicitly_legacy_untrusted() -> None:
    digest = "c" * 64
    rawdict = {"blocks": [{"type": 1, "image": b"page"}]}
    source_item_id = "page_visual:2"
    importer_identity = "bc_pdf_vector_importer.blender"
    observation = build_page_visual_source_observation(
        importer_identity=importer_identity,
        pdf_sha256=digest,
        page_number=2,
        source_scope_id=source_item_id,
        raw_text_dictionary=rawdict,
    )
    attempts = []
    for attempted_type in ("labels", "text", "3d_text", "glyphs", "geometry"):
        proof = build_page_visual_fallback_proof(
            importer_identity=importer_identity,
            pdf_sha256=digest,
            page_number=2,
            source_scope_id=source_item_id,
            requested_type="labels",
            attempted_type=attempted_type,
            raw_text_dictionary=rawdict,
        )
        attempts.append(
            {
                "source_item_id": source_item_id,
                "requested_type": "labels",
                "attempted_type": attempted_type,
                "final_type": None,
                "outcome": "proven_impossible",
                "cleanup_complete": True,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "delivery_entity_ids": [],
                "support_entity_ids": [],
                "referenced_entity_ids": [],
                "reused_entity_ids": [],
                "evidence": {
                    "page_visual_importer_identity": importer_identity,
                    "page_visual_raw_text_dictionary_sha256": observation[
                        "raw_text_dictionary_sha256"
                    ],
                    "page_visual_fallback_proof": proof,
                },
            }
        )
    terminal = _terminal_attempt(
        "blender",
        source_item_id=source_item_id,
        requested_type="labels",
        final_type="raster",
    )
    terminal["host_record"].update({"page": 2, "source_span_id": 0})
    attempts.append(terminal)
    report = _report(
        "blender",
        attempts,
        requested_type="labels",
        actual=_actual_payload("blender", "raster"),
        page_visual_source_observations={source_item_id: observation},
    )
    report.input["sha256"] = digest
    report.extra["host_result_persistence"]["source_pdf_sha256"] = digest
    report.extra["text_source_spans"] = 0
    report.extra["text_delivery_source_item_ids"] = [source_item_id]
    report.extra["text_delivery_source_item_count"] = 1
    ready = build_import_contract_ready(report)
    assert page_visual_schema_trust(observation) == "legacy_untrusted"
    assert ready["checks"]["text_delivery"] is False
    assert ready["checks"]["actual_text_entity_types"] is True
    assert ready["ready"] is False
    assert report.extra["text_source_spans"] == 0
    assert report.extra["text_delivery_obligations"]["source_item_ids"] == [
        source_item_id
    ]

    raw_observation_tamper = copy.deepcopy(report)
    tampered = raw_observation_tamper.extra[
        "page_visual_source_observations"
    ][source_item_id]
    tampered["raw_text_dictionary_sha256"] = "e" * 64
    tampered["observation_sha256"] = page_visual_source_observation_digest(
        tampered
    )
    assert (
        build_import_contract_ready(raw_observation_tamper)["checks"][
            "text_delivery"
        ]
        is False
    )


def test_freecad_prior_requires_deep_item_bound_proof() -> None:
    digest = "a" * 64
    source_item_id = "p1:item:1"
    reason = "svg_renderer_unavailable"
    created = ["fc:temporary:1"]
    proof = {
        "item_specific_proven_impossible": True,
        "importer_identity": "bluecollarsystems.freecad.pdf_vector_importer",
        "pdf_sha256": digest,
        "page_number": 1,
        "source_item_id": source_item_id,
        "requested_type": "glyphs",
        "attempted_type": "glyphs",
        "reason_code": reason,
        "evidence": {"renderer_attempted": True},
        "attempted_source_results": [
            {
                "source": "svg_item_renderer",
                "outcome": "proven_impossible",
                "reason_code": reason,
                "pdf_sha256": digest,
                "page_number": 1,
                "source_item_id": source_item_id,
            }
        ],
        "attempted_sources_complete": True,
        "cleanup_complete": True,
        "created_entity_ids": created,
        "removed_entity_ids": created,
    }
    prior = {
        "source_item_id": source_item_id,
        "requested_type": "glyphs",
        "attempted_type": "glyphs",
        "final_type": None,
        "outcome": "proven_impossible",
        "reason_code": reason,
        "cleanup_complete": True,
        "created_entity_ids": created,
        "removed_entity_ids": created,
        "delivery_entity_ids": [],
        "support_entity_ids": [],
        "referenced_entity_ids": [],
        "reused_entity_ids": [],
        "proof": proof,
    }
    terminal = _terminal_attempt(
        "freecad",
        source_item_id=source_item_id,
        requested_type="glyphs",
        final_type="geometry",
    )
    report = _report(
        "freecad",
        [prior, terminal],
        requested_type="glyphs",
        actual=build_actual_text_entity_types(
            host_app="freecad",
            text_mode="geometry",
            delivered_counts={"raw_geometry_edges": 1},
        ),
    )
    report.input["sha256"] = digest
    assert build_import_contract_ready(report)["checks"]["text_delivery"] is True

    report.extra["text_delivery_attempts"][0]["proof"] = {"x": 1}
    assert build_import_contract_ready(report)["checks"]["text_delivery"] is False

    report = _report(
        "freecad",
        [prior, terminal],
        requested_type="glyphs",
        actual=build_actual_text_entity_types(
            host_app="freecad",
            text_mode="geometry",
            delivered_counts={"raw_geometry_edges": 1},
        ),
    )
    report.input["sha256"] = digest
    arbitrary = report.extra["text_delivery_attempts"][0]["proof"]
    arbitrary["reason_code"] = "made_up_family"
    report.extra["text_delivery_attempts"][0]["reason_code"] = "made_up_family"
    arbitrary["attempted_source_results"] = [
        {
            "source": "made_up_source",
            "outcome": "made_up_outcome",
            "reason_code": "made_up_family",
            "pdf_sha256": digest,
            "page_number": 1,
            "source_item_id": source_item_id,
        }
    ]
    assert build_import_contract_ready(report)["checks"]["text_delivery"] is False
