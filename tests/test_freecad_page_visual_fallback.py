from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore.page_visual import (  # noqa: E402
    page_visual_fallback_proof_verified,
    page_visual_proof_digest,
)


STRUCTURAL_MODES = ("labels", "text", "3d_text", "glyphs", "geometry")


def _raster_result(pdf_sha256: str = "a" * 64) -> dict:
    return {
        "outcome": "verified",
        "entity_type": "raster",
        "created_entity_ids": ["PageRaster001"],
        "evidence": {
            "host_entity_type": "Image::ImagePlane",
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
    opts._pdf_sha256 = "a" * 64
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

    for attempt in opts.text_delivery_attempts[:-1]:
        proof = attempt["proof"]
        assert proof["schema"] == "pdf_page_visual_fallback_proof_v1"
        assert proof["page_specific_proven_impossible"] is True
        assert "item_specific_proven_impossible" not in proof
        assert proof["source_item_id"] == "p1:page"
        assert proof["evidence"]["canonical_source_item_count"] == 0
        assert len(proof["evidence"]["raw_text_dictionary_sha256"]) == 64
        assert page_visual_fallback_proof_verified(
            proof,
            expected_pdf_sha256=opts._pdf_sha256,
            expected_page_number=1,
            expected_source_scope_id="p1:page",
            expected_requested_type=requested_mode,
            expected_attempted_type=attempt["attempted_type"],
            expected_raw_text_dictionary_sha256=proof["evidence"][
                "raw_text_dictionary_sha256"
            ],
        )

    delivery = core._build_text_representation_delivery(
        opts, opts.text_delivery_attempts
    )
    assert delivery["verified"] is True
    assert delivery["invalid_reasons"] == []


def test_page_visual_proof_rejects_redigested_raw_dictionary_tamper():
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    opts._pdf_sha256 = "b" * 64
    core._record_no_source_text_page_fallback(
        opts,
        page_num=2,
        pdf_sha256=opts._pdf_sha256,
        raw_tdict={"blocks": [{"type": 1, "image": "omitted"}]},
        raster_result=_raster_result(opts._pdf_sha256),
    )
    proof = copy.deepcopy(opts.text_delivery_attempts[0]["proof"])
    expected_digest = proof["evidence"]["raw_text_dictionary_sha256"]
    proof["evidence"]["raw_text_dictionary_sha256"] = "e" * 64
    proof["attempted_source_results"][0]["raw_text_dictionary_sha256"] = "e" * 64
    proof["proof_sha256"] = page_visual_proof_digest(proof)

    assert not page_visual_fallback_proof_verified(
        proof,
        expected_pdf_sha256=opts._pdf_sha256,
        expected_page_number=2,
        expected_source_scope_id="p2:page",
        expected_requested_type="labels",
        expected_attempted_type="labels",
        expected_raw_text_dictionary_sha256=expected_digest,
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
    result = _raster_result("a" * 64)
    mutator(result)

    with pytest.raises(ValueError, match=match):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256="a" * 64,
            raw_tdict={"blocks": []},
            raster_result=result,
        )


def test_page_visual_fallback_rejects_page_that_has_canonical_text():
    opts = core.ImportOptions(import_text=True, text_mode="labels")
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
            pdf_sha256="a" * 64,
            raw_tdict=raw_tdict,
            raster_result=_raster_result(),
        )
