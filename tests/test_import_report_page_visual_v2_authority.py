from __future__ import annotations

import base64
import copy
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "PDFVectorImporter" / "src"))
sys.path.insert(0, str(ROOT / "PDFVectorImporter"))
sys.path.insert(0, str(ROOT / "tests"))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore import import_report as report_contract  # noqa: E402
from pdfcadcore.import_report import (  # noqa: E402
    ImportReport,
    build_actual_text_entity_types,
    build_import_contract_ready,
    build_import_report,
    enrich_import_report_extras,
)
from pdfcadcore.page_visual import (  # noqa: E402
    build_page_visual_fallback_proof,
    build_page_visual_fallback_proof_v2,
    build_page_visual_source_observation,
    capture_fresh_page_visual_authority,
    page_visual_fallback_proof_v2_digest,
    page_visual_source_observation_v2_digest,
)
from pdfcadcore.text_delivery_report import (  # noqa: E402
    build_text_representation_delivery,
)
from test_import_report_contract_hardening import (  # noqa: E402
    _actual_payload,
    _report,
    _terminal_attempt,
)
from test_freecad_report_inventory import (  # noqa: E402
    _aws_fallback_opts,
    _aws_objects,
)


fitz = pytest.importorskip("fitz")

ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
HOST_IDENTITIES = {
    "freecad": "bluecollarsystems.freecad.pdf_vector_importer",
    "blender": "bc_pdf_vector_importer.blender",
    "librecad": "bluecollarsystems.librecad.pdf_importer",
}
HOST_LABEL_LADDERS = {
    "freecad": ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
    "blender": ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
    "librecad": ("labels", "text", "glyphs", "geometry", "raster"),
}


def _image_pdf(
    tmp_path: Path,
    name: str = "source.pdf",
    *,
    distinct_mark: bool = False,
) -> Path:
    path = tmp_path / name
    document = fitz.open()
    try:
        page = document.new_page(width=200, height=100)
        page.insert_image(fitz.Rect(20, 20, 80, 80), stream=ONE_PIXEL_PNG)
        if distinct_mark:
            page.draw_line((100, 10), (190, 90))
        document.save(str(path), garbage=4, deflate=True)
    finally:
        document.close()
    return path


def _page_scope(host: str) -> str:
    return "p1:page" if host == "freecad" else "page_visual:1"


def _page_report(
    host: str,
    tmp_path: Path,
    *,
    legacy_v1: bool = False,
):
    source_path = _image_pdf(tmp_path, f"{host}-source.pdf")
    source_item_id = _page_scope(host)
    identity = HOST_IDENTITIES[host]
    authority = capture_fresh_page_visual_authority(
        source_path,
        importer_identity=identity,
        page_scope_ids={1: source_item_id},
    )
    observation = authority.observation(1)
    if legacy_v1:
        rawdict = {"blocks": [{"type": 1}]}
        observation = build_page_visual_source_observation(
            importer_identity=identity,
            pdf_sha256=authority.pdf_sha256,
            page_number=1,
            source_scope_id=source_item_id,
            raw_text_dictionary=rawdict,
        )

    attempts = []
    last_proof = None
    for attempted_type in HOST_LABEL_LADDERS[host][:-1]:
        if legacy_v1:
            proof = build_page_visual_fallback_proof(
                importer_identity=identity,
                pdf_sha256=authority.pdf_sha256,
                page_number=1,
                source_scope_id=source_item_id,
                requested_type="labels",
                attempted_type=attempted_type,
                raw_text_dictionary={"blocks": [{"type": 1}]},
            )
        else:
            proof = build_page_visual_fallback_proof_v2(
                observation=observation,
                authority=authority,
                requested_type="labels",
                attempted_type=attempted_type,
            )
        last_proof = proof
        evidence = {"page_visual_fallback_proof": proof}
        attempt = {
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
            "evidence": evidence,
        }
        if host == "freecad":
            attempt["proof"] = proof
        attempts.append(attempt)

    raster_path = None
    if host == "freecad":
        raster_path = tmp_path / "freecad-page-1.png"
        raster_path.write_bytes(b"verified persisted raster bytes")
        fallback_opts = _aws_fallback_opts(
            str(raster_path),
            authority.pdf_sha256,
            source_path=source_path,
        )
        terminal = _terminal_attempt(
            host,
            source_item_id=source_item_id,
            requested_type="labels",
            final_type="raster",
            entity_ids=["PageRaster001"],
        )
        terminal["evidence"] = copy.deepcopy(
            fallback_opts.text_delivery_attempts[-1]["evidence"]
        )
        terminal["evidence"]["page_visual_fallback_proof"] = last_proof
    else:
        terminal = _terminal_attempt(
            host,
            source_item_id=source_item_id,
            requested_type="labels",
            final_type="raster",
        )
    if host == "blender":
        terminal["host_record"].update({"page": 1, "source_span_id": 0})
    attempts.append(terminal)
    actual = (
        build_actual_text_entity_types(
            host_app="freecad",
            text_mode="raster",
            delivered_counts={"raster_text_patch": 1},
        )
        if host == "freecad"
        else _actual_payload(host, "raster")
    )
    report = _report(
        host,
        attempts,
        requested_type="labels",
        actual=actual,
        page_visual_source_observations={source_item_id: observation},
    )
    report.input["sha256"] = authority.pdf_sha256
    if host == "freecad":
        objects = _aws_objects(str(raster_path), authority.pdf_sha256)
        inventory = core._build_host_object_inventory(objects)
        report.extra["actual_host_object_inventory"] = core._public_report_value(
            inventory
        )
        report.extra["save_reopen_inventory"] = core._public_report_value(
            core._crosscheck_host_object_inventory(inventory, objects)
        )
        report.result["primitives"] = 0
        report.result["text_entities"] = 0
        report.result["images"] = 1
    if host in {"blender", "librecad"}:
        report.extra["host_result_persistence"]["source_pdf_sha256"] = (
            authority.pdf_sha256
        )
    return report, authority, source_path


@pytest.mark.parametrize("host", ["freecad", "blender", "librecad"])
def test_page_fallback_requires_exact_v2_authority_for_every_host(
    host: str,
    tmp_path: Path,
) -> None:
    report, authority, _source_path = _page_report(host, tmp_path)

    ready = build_import_contract_ready(
        report,
        page_visual_authority=authority,
    )

    assert ready["checks"]["text_delivery"] is True
    assert not any(
        "page_visual" in reason or "impossibility proof" in reason
        for reason in ready["text_delivery_invalid_reasons"]
    )
    assert ready["ready"] is True
    if host == "freecad":
        assert ready["checks"]["delivery_inventory_binding"] is True


@pytest.mark.parametrize("host", ["freecad", "blender", "librecad"])
def test_v1_page_fallback_is_untrusted_even_when_authority_is_supplied(
    host: str,
    tmp_path: Path,
) -> None:
    report, authority, _source_path = _page_report(
        host,
        tmp_path,
        legacy_v1=True,
    )

    ready = build_import_contract_ready(
        report,
        page_visual_authority=authority,
    )

    assert ready["checks"]["text_delivery"] is False
    assert ready["ready"] is False
    if host == "freecad":
        assert ready["checks"]["delivery_inventory_binding"] is False


def test_freecad_inventory_binding_requires_exact_live_authority(
    tmp_path: Path,
) -> None:
    report, authority, _source_path = _page_report("freecad", tmp_path)
    different_path = _image_pdf(
        tmp_path,
        "different-freecad.pdf",
        distinct_mark=True,
    )
    different_authority = capture_fresh_page_visual_authority(
        different_path,
        importer_identity=HOST_IDENTITIES["freecad"],
        page_scope_ids={1: "p1:page"},
    )

    assert build_import_contract_ready(
        report,
        page_visual_authority=authority,
    )["checks"]["delivery_inventory_binding"] is True
    assert build_import_contract_ready(report)["checks"][
        "delivery_inventory_binding"
    ] is False
    assert build_import_contract_ready(
        report,
        page_visual_authority=different_authority,
    )["checks"]["delivery_inventory_binding"] is False


def test_missing_or_different_fresh_authority_cannot_authorize_report_json(
    tmp_path: Path,
) -> None:
    report, authority, _source_path = _page_report("librecad", tmp_path)
    different_path = _image_pdf(
        tmp_path,
        "different.pdf",
        distinct_mark=True,
    )
    different_authority = capture_fresh_page_visual_authority(
        different_path,
        importer_identity=HOST_IDENTITIES["librecad"],
        page_scope_ids={1: "page_visual:1"},
    )

    assert build_import_contract_ready(report)["checks"]["text_delivery"] is False
    assert (
        build_import_contract_ready(
            report,
            page_visual_authority=different_authority,
        )["checks"]["text_delivery"]
        is False
    )
    assert authority.pdf_sha256 != different_authority.pdf_sha256


def test_coordinated_report_redigest_cannot_replace_authority_leaf(
    tmp_path: Path,
) -> None:
    report, authority, _source_path = _page_report("librecad", tmp_path)
    forged = copy.deepcopy(report)
    observation = forged.extra["page_visual_source_observations"]["page_visual:1"]
    observation["raw_text"]["dictionary_sha256"] = "f" * 64
    observation["observation_sha256"] = (
        page_visual_source_observation_v2_digest(observation)
    )
    for attempt in forged.extra["text_delivery_attempts"][:-1]:
        proof = attempt["evidence"]["page_visual_fallback_proof"]
        proof["observation_sha256"] = observation["observation_sha256"]
        proof["proof_sha256"] = page_visual_fallback_proof_v2_digest(proof)

    ready = build_import_contract_ready(
        forged,
        page_visual_authority=authority,
    )

    assert ready["checks"]["text_delivery"] is False
    assert ready["ready"] is False


@pytest.mark.parametrize("host", ["freecad", "blender", "librecad"])
def test_direct_requested_raster_needs_no_page_fallback_authority(
    host: str,
) -> None:
    source_item_id = _page_scope(host)
    terminal = _terminal_attempt(
        host,
        source_item_id=source_item_id,
        requested_type="raster",
        final_type="raster",
    )
    actual = None if host == "freecad" else _actual_payload(host, "raster")
    report = _report(
        host,
        [terminal],
        requested_type="raster",
        actual=actual,
    )

    assert build_import_contract_ready(report)["checks"]["text_delivery"] is True


def test_non_page_delivery_without_fallback_preserves_existing_behavior() -> None:
    terminal = _terminal_attempt(
        "librecad",
        source_item_id="p1:item:1",
        requested_type="text",
        final_type="text",
    )
    report = _report(
        "librecad",
        [terminal],
        requested_type="text",
        actual=_actual_payload("librecad", "text"),
    )

    ready = build_import_contract_ready(report)
    assert ready["checks"]["text_delivery"] is True
    assert ready["ready"] is True


def test_enrichment_uses_authority_without_serializing_the_capability(
    tmp_path: Path,
) -> None:
    report, authority, _source_path = _page_report("librecad", tmp_path)

    enrich_import_report_extras(
        report,
        page_visual_authority=authority,
    )

    assert report.extra["import_contract_ready"]["ready"] is True
    live_ready = copy.deepcopy(report.extra["import_contract_ready"])
    dict_payload = report.to_dict()
    json_payload = json.loads(report.to_json())

    for payload in (dict_payload, json_payload):
        assert payload["extra"]["import_contract_ready"]["ready"] is False
        assert "page_visual_authority" not in json.dumps(payload, sort_keys=True)
    assert report.extra["import_contract_ready"] == live_ready
    assert report.extra["import_contract_ready"]["ready"] is True


def test_deserialization_recomputes_injected_authority_readiness_without_authority(
    tmp_path: Path,
) -> None:
    report, authority, _source_path = _page_report("librecad", tmp_path)
    enrich_import_report_extras(report, page_visual_authority=authority)
    payload = report.to_dict()
    payload["extra"]["import_contract_ready"] = copy.deepcopy(
        report.extra["import_contract_ready"]
    )
    assert payload["extra"]["import_contract_ready"]["ready"] is True

    restored = ImportReport.from_dict(payload)
    assert restored.extra["import_contract_ready"]["ready"] is False
    assert build_import_contract_ready(restored)["ready"] is False

    input_path = tmp_path / "injected-ready.import_report.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    reread = ImportReport.read_json(str(input_path))
    assert reread.extra["import_contract_ready"]["ready"] is False
    assert build_import_contract_ready(reread)["ready"] is False


@pytest.mark.parametrize(
    ("requested_type", "final_type", "source_item_id"),
    [
        ("text", "text", "p1:item:1"),
        ("raster", "raster", "page_visual:1"),
    ],
)
def test_authority_free_exact_reports_recompute_ready_across_round_trips(
    tmp_path: Path,
    requested_type: str,
    final_type: str,
    source_item_id: str,
) -> None:
    terminal = _terminal_attempt(
        "librecad",
        source_item_id=source_item_id,
        requested_type=requested_type,
        final_type=final_type,
    )
    report = _report(
        "librecad",
        [terminal],
        requested_type=requested_type,
        actual=_actual_payload("librecad", final_type),
    )
    enrich_import_report_extras(report)

    assert report.extra["import_contract_ready"]["ready"] is True
    payload = report.to_dict()
    assert payload["extra"]["import_contract_ready"]["ready"] is True
    assert ImportReport.from_dict(payload).extra["import_contract_ready"]["ready"] is True

    output_path = tmp_path / f"{requested_type}.import_report.json"
    report.write_json(str(output_path))
    reread = ImportReport.read_json(str(output_path))
    assert reread.extra["import_contract_ready"]["ready"] is True


def test_builder_propagates_authority_only_to_readiness_evaluation(
    tmp_path: Path,
) -> None:
    seed, authority, source_path = _page_report("librecad", tmp_path)
    extra = copy.deepcopy(seed.extra)
    extra.pop("import_contract_ready", None)
    extra.pop("human_summary", None)

    report = build_import_report(
        host_app="librecad",
        pdf_path=str(source_path),
        text_mode="labels",
        text_count=1,
        extra=extra,
        page_visual_authority=authority,
    )

    assert report.input["sha256"] == authority.pdf_sha256
    assert report.extra["import_contract_ready"]["ready"] is True
    assert build_import_contract_ready(
        report,
        page_visual_authority=authority,
    )["ready"] is True
    payload = report.to_dict()
    assert payload["extra"]["import_contract_ready"]["ready"] is False
    assert report.extra["import_contract_ready"]["ready"] is True
    assert "page_visual_authority" not in json.dumps(payload, sort_keys=True)


def test_freecad_inventory_binding_rejects_page_fallback_without_v2_authority(
    tmp_path: Path,
) -> None:
    source_path = _image_pdf(tmp_path, "freecad-inventory-source.pdf")
    raster_path = tmp_path / "freecad-page-1.png"
    raster_path.write_bytes(b"verified persisted raster bytes")
    authority = capture_fresh_page_visual_authority(
        source_path,
        importer_identity=HOST_IDENTITIES["freecad"],
        page_scope_ids={1: "p1:page"},
    )
    observation = authority.observation(1)
    opts = _aws_fallback_opts(
        str(raster_path),
        authority.pdf_sha256,
        source_path=source_path,
    )
    last_proof = None
    for attempt in opts.text_delivery_attempts[:-1]:
        for entity_field in (
            "delivery_entity_ids",
            "support_entity_ids",
            "referenced_entity_ids",
            "reused_entity_ids",
        ):
            attempt[entity_field] = []
        last_proof = build_page_visual_fallback_proof_v2(
            observation=observation,
            authority=authority,
            requested_type="labels",
            attempted_type=attempt["attempted_type"],
        )
        attempt["proof"] = last_proof
        attempt["evidence"]["page_visual_fallback_proof"] = last_proof
    terminal_evidence = copy.deepcopy(opts.text_delivery_attempts[-1]["evidence"])
    terminal_evidence["page_visual_fallback_proof"] = last_proof
    terminal = _terminal_attempt(
        "freecad",
        source_item_id="p1:page",
        requested_type="labels",
        final_type="raster",
        entity_ids=["PageRaster001"],
    )
    terminal["evidence"] = terminal_evidence
    opts.text_delivery_attempts[-1] = terminal

    objects = _aws_objects(str(raster_path), authority.pdf_sha256)
    inventory = core._public_report_value(core._build_host_object_inventory(objects))
    attempts = core._public_report_value(opts.text_delivery_attempts)
    delivery = build_text_representation_delivery(
        attempts,
        requested_type="labels",
        required=True,
        expected_source_item_ids=["p1:page"],
    )
    observations = {"p1:page": observation}

    assert delivery["verified"] is True, delivery["invalid_reasons"]
    assert report_contract._freecad_delivery_inventory_binding_verified(
        delivery,
        attempts,
        inventory,
        authority.pdf_sha256,
        page_source_observations=observations,
        page_visual_authority=authority,
    )
    assert not report_contract._freecad_delivery_inventory_binding_verified(
        delivery,
        attempts,
        inventory,
        authority.pdf_sha256,
        page_source_observations=observations,
    )
