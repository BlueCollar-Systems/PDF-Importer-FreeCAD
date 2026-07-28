from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore.page_visual import (  # noqa: E402
    PAGE_VISUAL_EVIDENCE_FIELDS,
    PAGE_VISUAL_PROOF_FIELDS,
    PAGE_VISUAL_RESULT_FIELDS,
    build_page_visual_fallback_proof,
    build_page_visual_source_observation,
    page_visual_fallback_proof_verified,
    page_visual_proof_digest,
)
from pdfcadcore.source_ink import (  # noqa: E402
    SOURCE_INK_EVIDENCE_AUTHORITY,
    SOURCE_INK_EVIDENCE_SCHEMA,
    source_ink_evidence_digest,
    source_ink_evidence_verified,
)


def _page_visual_proof(source_item_id: str) -> dict:
    pdf_sha256 = "a" * 64
    raw_digest = "b" * 64
    importer_identity = "test-host/1"
    proof = {
        "schema": "pdf_page_visual_fallback_proof_v1",
        "page_specific_proven_impossible": True,
        "pdf_sha256": pdf_sha256,
        "page_number": 2,
        "source_item_id": source_item_id,
        "requested_type": "labels",
        "attempted_type": "labels",
        "reason_code": "no_canonical_text_source_items",
        "attempted_sources_complete": True,
        "cleanup_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "importer_identity": importer_identity,
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
                "pdf_sha256": pdf_sha256,
                "page_number": 2,
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


def test_page_visual_proof_accepts_exact_host_neutral_source_identity() -> None:
    source_item_id = "page_visual:2"
    proof = _page_visual_proof(source_item_id)

    assert page_visual_fallback_proof_verified(
        proof,
        expected_pdf_sha256="a" * 64,
        expected_page_number=2,
        expected_source_scope_id=source_item_id,
        expected_requested_type="labels",
        expected_attempted_type="labels",
        expected_importer_identity="test-host/1",
        expected_raw_text_dictionary_sha256="b" * 64,
    )


def _source_ink_evidence(source_item_id: str) -> tuple[dict, dict]:
    font_identity = {"raw_name": "Exact Test", "normalized_key": "exact test"}
    source_text = "A"
    font_binding = {
        "asset_id": "sha256:" + "d" * 64,
        "source_xref": 1,
        "source_font_sha256": "c" * 64,
        "usable_font_sha256": "d" * 64,
        "base_font_name": "Exact Test",
        "span_font_name": "Exact Test",
        "source_format": "ttf",
        "usable_format": "ttf",
        "source_origin": "embedded_pdf_font",
    }
    evidence = {
        "schema": SOURCE_INK_EVIDENCE_SCHEMA,
        "authority": SOURCE_INK_EVIDENCE_AUTHORITY,
        "pdf_sha256": "c" * 64,
        "page_number": 2,
        "source_item_id": source_item_id,
        "source_text": source_text,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "font_identity": font_identity,
        "all_characters_physically_resolved": True,
        "classification": "zero_visible_ink",
        "zero_ink_characters_layout_only": False,
        "font_asset_bindings": [font_binding],
        "glyph_id_sequence": [1],
        "characters": [
            {
                "source_index": 0,
                "character": source_text,
                "authority": "pymupdf_texttrace_nonpainting_render_mode",
                "physically_resolved": True,
                "zero_visible_ink": True,
                "layout_only_zero_ink": False,
                "synthetic": False,
                "glyph_id": 1,
                "glyph_name": "A",
                "glyph_bounds": [0.0, 0.0, 500.0, 700.0],
                "advance_width": 600.0,
                "font_asset_binding": font_binding,
                "source_font_sha256": "c" * 64,
                "usable_font_sha256": "d" * 64,
                "trace_type": 3,
                "opacity": 1.0,
            }
        ],
    }
    evidence["evidence_sha256"] = source_ink_evidence_digest(evidence)
    expected = {
        "expected_pdf_sha256": "c" * 64,
        "expected_page_number": 2,
        "expected_source_item_id": source_item_id,
        "expected_source_text": source_text,
        "expected_font_identity": font_identity,
        "expected_font_asset_bindings": [font_binding],
        "expected_glyph_id_sequence": [1],
    }
    return evidence, expected


def test_source_ink_proof_accepts_exact_host_neutral_source_identity() -> None:
    evidence, expected = _source_ink_evidence("text_span:2:17")

    assert source_ink_evidence_verified(evidence, **expected)


def test_shared_proofs_reject_padded_source_identity() -> None:
    padded = " page:2:text:17 "
    evidence, expected = _source_ink_evidence(padded)
    assert not source_ink_evidence_verified(evidence, **expected)

    proof = _page_visual_proof(" page_visual:2 ")
    assert not page_visual_fallback_proof_verified(
        proof,
        expected_pdf_sha256="a" * 64,
        expected_page_number=2,
        expected_source_scope_id=" page_visual:2 ",
        expected_requested_type="labels",
        expected_attempted_type="labels",
        expected_importer_identity="test-host/1",
        expected_raw_text_dictionary_sha256="b" * 64,
    )


def _page_proof_verified(
    proof: dict,
    *,
    importer_identity: str = "test-host/1",
    raw_digest: str = "b" * 64,
) -> bool:
    return page_visual_fallback_proof_verified(
        proof,
        expected_pdf_sha256="a" * 64,
        expected_page_number=2,
        expected_source_scope_id="page_visual:2",
        expected_requested_type="labels",
        expected_attempted_type="labels",
        expected_importer_identity=importer_identity,
        expected_raw_text_dictionary_sha256=raw_digest,
    )


def test_page_visual_proof_key_order_is_not_semantic() -> None:
    proof = _page_visual_proof("page_visual:2")
    reordered = {key: proof[key] for key in reversed(list(proof))}
    assert _page_proof_verified(reordered)


@pytest.mark.parametrize("location", ("proof", "evidence", "result"))
def test_page_visual_proof_rejects_redigested_extra_keys(location: str) -> None:
    proof = _page_visual_proof("page_visual:2")
    target = (
        proof
        if location == "proof"
        else proof["evidence"]
        if location == "evidence"
        else proof["attempted_source_results"][0]
    )
    target["forged"] = True
    proof["proof_sha256"] = page_visual_proof_digest(proof)
    assert not _page_proof_verified(proof)


def test_page_visual_proof_requires_independent_importer_and_raw_bindings() -> None:
    proof = _page_visual_proof("page_visual:2")
    assert not _page_proof_verified(proof, importer_identity="other-host/1")
    assert not _page_proof_verified(proof, raw_digest="c" * 64)


def test_page_visual_proof_requires_zero_created_and_removed_entities() -> None:
    proof = _page_visual_proof("page_visual:2")
    proof["created_entity_ids"] = ["temporary:1"]
    proof["removed_entity_ids"] = ["temporary:1"]
    proof["proof_sha256"] = page_visual_proof_digest(proof)
    assert not _page_proof_verified(proof)


def test_page_visual_digest_rejects_nonfinite_and_non_json_values() -> None:
    with pytest.raises(ValueError):
        page_visual_proof_digest({"value": float("nan")})
    with pytest.raises(ValueError):
        page_visual_proof_digest({"value": ("tuple",)})


def test_page_visual_verifier_is_total_for_cyclic_and_nonfinite_proofs() -> None:
    cyclic = _page_visual_proof("page_visual:2")
    cyclic["evidence"]["forged_cycle"] = cyclic
    assert not _page_proof_verified(cyclic)

    nonfinite = _page_visual_proof("page_visual:2")
    nonfinite["evidence"]["raw_text_block_count"] = float("nan")
    assert not _page_proof_verified(nonfinite)


def test_page_visual_builder_emits_exact_roundtrip_contract() -> None:
    rawdict = {"blocks": [{"type": 1, "image": b"pixels"}]}
    observation = build_page_visual_source_observation(
        importer_identity="test-host/1",
        pdf_sha256="a" * 64,
        page_number=2,
        source_scope_id="page_visual:2",
        raw_text_dictionary=rawdict,
    )
    proof = build_page_visual_fallback_proof(
        importer_identity="test-host/1",
        pdf_sha256="a" * 64,
        page_number=2,
        source_scope_id="page_visual:2",
        requested_type="labels",
        attempted_type="labels",
        raw_text_dictionary=rawdict,
    )
    assert set(proof) == PAGE_VISUAL_PROOF_FIELDS
    assert set(proof["evidence"]) == PAGE_VISUAL_EVIDENCE_FIELDS
    assert set(proof["attempted_source_results"][0]) == PAGE_VISUAL_RESULT_FIELDS
    assert _page_proof_verified(
        proof,
        raw_digest=observation["raw_text_dictionary_sha256"],
    )


@pytest.mark.parametrize("value", (" ", "\u200b", "\u2060"))
def test_page_visual_builder_rejects_unicode_only_source_text(value: str) -> None:
    rawdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {
                        "spans": [
                            {
                                "text": value,
                                "chars": [{"c": value, "synthetic": True}],
                            }
                        ]
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="source-text structure"):
        build_page_visual_fallback_proof(
            importer_identity="test-host/1",
            pdf_sha256="a" * 64,
            page_number=2,
            source_scope_id="page_visual:2",
            requested_type="labels",
            attempted_type="labels",
            raw_text_dictionary=rawdict,
        )


@pytest.mark.parametrize(
    "rawdict",
    (
        {},
        {"blocks": None},
        {"blocks": [{"type": 9}]},
        {"blocks": [{"type": 1, "lines": []}]},
    ),
)
def test_page_visual_builder_rejects_malformed_or_unknown_rawdict(
    rawdict: dict,
) -> None:
    with pytest.raises(ValueError):
        build_page_visual_fallback_proof(
            importer_identity="test-host/1",
            pdf_sha256="a" * 64,
            page_number=2,
            source_scope_id="page_visual:2",
            requested_type="labels",
            attempted_type="labels",
            raw_text_dictionary=rawdict,
        )


def test_page_visual_verifier_requires_zero_text_block_count() -> None:
    proof = _page_visual_proof("page_visual:2")
    proof["evidence"]["raw_text_block_count"] = 1
    proof["proof_sha256"] = page_visual_proof_digest(proof)
    assert not _page_proof_verified(proof)
