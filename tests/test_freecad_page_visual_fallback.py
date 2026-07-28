from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore.page_visual import (  # noqa: E402
    PAGE_VISUAL_FALLBACK_PROOF_V2_FIELDS,
    PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA,
    page_visual_fallback_proof_v2_digest,
    page_visual_fallback_proof_v2_verified,
    page_visual_source_observation_v2_digest,
)


STRUCTURAL_MODES = ("labels", "text", "3d_text", "glyphs", "geometry")


def _bind_blank_authority(
    opts: core.ImportOptions,
    *,
    page_number: int = 1,
    source_path: Path | None = None,
) -> str:
    if source_path is not None:
        core._initialize_pdf_source_attempt(str(source_path), opts)
    else:
        with tempfile.TemporaryDirectory(prefix="fc_v2_fallback_test_") as td:
            path = Path(td) / "source.pdf"
            document = core.fitz.open()
            try:
                for _ in range(page_number):
                    document.new_page()
                document.save(str(path))
            finally:
                document.close()
            core._initialize_pdf_source_attempt(str(path), opts)
    core._capture_page_visual_runtime_authority(opts, [page_number])
    return opts._pdf_sha256


def _raster_result(
    pdf_sha256: str = "a" * 64,
    page_number: int = 1,
) -> dict:
    return {
        "outcome": "verified",
        "entity_type": "raster",
        "created_entity_ids": ["PageRaster001"],
        "evidence": {
            "host_entity_type": "Image::ImagePlane",
            "source_page_number": page_number,
            "pdf_sha256": pdf_sha256,
            "source_asset_sha256": "f" * 64,
            "raster_content_verified": True,
            "raster_file_included": True,
        },
    }


@pytest.mark.parametrize("requested_mode", STRUCTURAL_MODES)
def test_no_canonical_text_uses_finite_page_scoped_visual_fallback(requested_mode):
    opts = core.ImportOptions(
        import_mode="auto",
        import_text=True,
        text_mode=requested_mode,
        raster_fallback=False,
    )
    _bind_blank_authority(opts)
    raw_tdict = {"width": 100.0, "height": 200.0, "blocks": [{"type": 1}]}

    info = core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256=opts._pdf_sha256,
        raw_tdict=raw_tdict,
        raster_result=_raster_result(opts._pdf_sha256),
    )

    ladder = list(core.TEXT_ITEM_FALLBACK_LADDERS[requested_mode])
    assert info["entity_type"] == "raster"
    assert info["source_item_count"] == 0
    assert info["source_item_ids"] == ["p1:page"]
    assert [attempt["attempted_type"] for attempt in opts.text_delivery_attempts] == ladder
    assert [attempt["outcome"] for attempt in opts.text_delivery_attempts[:-1]] == [
        "proven_impossible"
    ] * (len(ladder) - 1)
    assert opts.text_delivery_attempts[-1]["outcome"] == "verified"
    assert opts.text_delivery_attempts[-1]["final_type"] == "raster"
    assert opts.text_delivery_attempts[-1]["evidence"]["page_visual_fallback_proof"]
    assert opts.text_delivered_counts == {"raster_text_patch": 1}
    assert len(opts.text_mode_fallbacks) == 1
    fallback_event = opts.text_mode_fallbacks[0]
    assert set(fallback_event["proof"]) == PAGE_VISUAL_FALLBACK_PROOF_V2_FIELDS
    assert fallback_event["proof"] == opts.text_delivery_attempts[-2]["proof"]
    assert fallback_event["attempted_types"] == ladder
    assert fallback_event["proof_chain"] == [
        attempt["proof"] for attempt in opts.text_delivery_attempts[:-1]
    ]

    for attempt in opts.text_delivery_attempts[:-1]:
        proof = attempt["proof"]
        assert proof["schema"] == PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA
        assert proof["page_specific_proven_impossible"] is True
        assert "item_specific_proven_impossible" not in proof
        assert "attempted_types" not in proof
        assert proof["source_item_id"] == "p1:page"
        assert page_visual_fallback_proof_v2_verified(
            proof,
            observation=opts.page_visual_source_observations["p1:page"],
            authority=opts._page_visual_authority,
            expected_requested_type=requested_mode,
            expected_attempted_type=attempt["attempted_type"],
        )

    delivery = core._build_text_representation_delivery(
        opts, opts.text_delivery_attempts
    )
    assert delivery["verified"] is True
    assert delivery["invalid_reasons"] == []


def test_redigested_page_observation_with_extra_field_fails_runtime_delivery():
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    _bind_blank_authority(opts)
    core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256=opts._pdf_sha256,
        raw_tdict={"blocks": [{"type": 1}]},
        raster_result=_raster_result(opts._pdf_sha256),
    )
    observation = opts.page_visual_source_observations["p1:page"]
    observation["untrusted_extra_field"] = True
    observation["observation_sha256"] = page_visual_source_observation_v2_digest(
        observation
    )

    runtime_delivery = core._validate_freecad_text_representation_delivery(
        opts, opts.text_delivery_attempts
    )
    delivery = core._build_text_representation_delivery(opts, opts.text_delivery_attempts)

    assert delivery["verified"] is False
    assert any(
        "proof_page_visual_observation_invalid" in reason
        for reason in runtime_delivery["invalid_reasons"]
    )


def test_redigested_page_proof_with_extra_field_fails_runtime_delivery():
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    _bind_blank_authority(opts)
    core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256=opts._pdf_sha256,
        raw_tdict={"blocks": [{"type": 1}]},
        raster_result=_raster_result(opts._pdf_sha256),
    )
    proof = opts.text_mode_fallbacks[0]["proof"]
    proof["untrusted_extra_field"] = True
    proof["proof_sha256"] = page_visual_fallback_proof_v2_digest(proof)

    runtime_delivery = core._validate_freecad_text_representation_delivery(
        opts, opts.text_delivery_attempts
    )
    delivery = core._build_text_representation_delivery(opts, opts.text_delivery_attempts)

    assert delivery["verified"] is False
    assert any(
        "fallback_proof_missing" in reason
        for reason in runtime_delivery["invalid_reasons"]
    )


def test_duplicate_page_fallback_rejects_conflicting_event_metadata():
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    _bind_blank_authority(opts)
    core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256=opts._pdf_sha256,
        raw_tdict={"blocks": [{"type": 1}]},
        raster_result=_raster_result(opts._pdf_sha256),
    )
    event = opts.text_mode_fallbacks[0]
    metadata = {
        field_name: copy.deepcopy(event[field_name])
        for field_name in (
            "attempted_types",
            "proof_chain",
            "transition_chain",
            "created_entity_ids",
            "removed_entity_ids",
            "cleanup_complete",
        )
    }
    metadata["transition_chain"] = []

    with pytest.raises(ValueError, match="conflicting page fallback event metadata"):
        core._record_text_mode_fallback(
            opts,
            requested=event["requested"],
            delivered=event["delivered"],
            reason=event["reason"],
            count=event["count"],
            source_item_id=event["source_item_ids"][0],
            proof=event["proof"],
            event_metadata=metadata,
        )


def test_page_scoped_fallback_is_exported_with_its_exact_proof(tmp_path):
    source_path = tmp_path / "source.pdf"
    document = core.fitz.open()
    document.new_page()
    document.save(str(source_path))
    document.close()
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    digest = _bind_blank_authority(opts, source_path=source_path)
    core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256=digest,
        raw_tdict={"blocks": [{"type": 1}]},
        raster_result=_raster_result(digest),
    )
    opts.text_delivery_obligation_source_item_ids = ["p1:page"]
    opts._report_extra = {
        "actual_text_entity_types": {
            "entity_type": "raster",
            "count": 1,
            "font_rendered": False,
            "examples": [],
        }
    }
    output_path = tmp_path / "report.json"

    core.write_import_report(
        pdf_path=str(source_path),
        output_path=str(output_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    fallback_event = payload["extra"]["text_mode_fallbacks"][0]
    assert set(fallback_event["proof"]) == PAGE_VISUAL_FALLBACK_PROOF_V2_FIELDS
    assert payload["fallback"]["text"]["proof"] == fallback_event["proof"]
    assert fallback_event["attempted_types"] == list(
        core.TEXT_ITEM_FALLBACK_LADDERS["labels"]
    )


def test_page_visual_proof_rejects_redigested_observation_tamper():
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    _bind_blank_authority(opts, page_number=2)
    core._record_no_source_text_page_fallback(
        opts,
        page_num=2,
        pdf_sha256=opts._pdf_sha256,
        raw_tdict={"blocks": [{"type": 1, "image": "omitted"}]},
        raster_result=_raster_result(opts._pdf_sha256, page_number=2),
    )
    proof = copy.deepcopy(opts.text_delivery_attempts[0]["proof"])
    observation = copy.deepcopy(opts.page_visual_source_observations["p2:page"])
    observation["page_xref"] += 1
    observation["observation_sha256"] = page_visual_source_observation_v2_digest(
        observation
    )

    assert not page_visual_fallback_proof_v2_verified(
        proof,
        observation=observation,
        authority=opts._page_visual_authority,
        expected_requested_type="labels",
        expected_attempted_type="labels",
    )


@pytest.mark.parametrize(
    "mutator, match",
    (
        (lambda result: result["evidence"].update(pdf_sha256="c" * 64), "PDF"),
        (lambda result: result["evidence"].pop("source_asset_sha256"), "raster"),
        (lambda result: result["evidence"].update(raster_content_verified=False), "raster"),
    ),
)
def test_page_visual_fallback_rejects_unbound_raster(mutator, match):
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    _bind_blank_authority(opts)
    result = _raster_result(opts._pdf_sha256)
    mutator(result)

    with pytest.raises(ValueError, match=match):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256=opts._pdf_sha256,
            raw_tdict={"blocks": []},
            raster_result=result,
        )


def test_page_visual_fallback_rejects_page_that_has_canonical_text():
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    _bind_blank_authority(opts)
    raw_tdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {
                        "dir": (1.0, 0.0),
                        "spans": [
                            {
                                "text": "VISIBLE",
                                "bbox": (0.0, 0.0, 20.0, 10.0),
                                "origin": (0.0, 8.0),
                                "font": "Helvetica",
                                "size": 10.0,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    with pytest.raises(ValueError, match="canonical source text"):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256=opts._pdf_sha256,
            raw_tdict=raw_tdict,
            raster_result=_raster_result(),
        )
