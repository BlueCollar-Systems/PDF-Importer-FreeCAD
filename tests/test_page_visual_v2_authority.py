from __future__ import annotations

import base64
import copy
import hashlib
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore import page_visual as pv  # noqa: E402


fitz = pytest.importorskip("fitz")

IMPORTER = "tests/page-visual-v2"
ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _pdf_bytes(kind: str, *, pages: int = 1) -> bytes:
    document = fitz.open()
    try:
        for page_index in range(pages):
            page = document.new_page(width=200, height=100)
            if kind in {"drawing", "mixed"}:
                page.draw_line(
                    (10, 20 + page_index),
                    (180, 20 + page_index),
                )
            if kind in {"image", "mixed"}:
                page.insert_image(
                    fitz.Rect(40, 30, 80, 70),
                    stream=ONE_PIXEL_PNG,
                )
        return document.tobytes(garbage=4, deflate=True)
    finally:
        document.close()


def _authority(payload: bytes, *, pages: int = 1):
    return pv.capture_fresh_page_visual_authority(
        payload,
        importer_identity=IMPORTER,
        page_scope_ids={
            page_number: f"page_visual:{page_number}"
            for page_number in range(1, pages + 1)
        },
    )


def test_real_drawing_only_page_keeps_geometry_reachable() -> None:
    payload = _pdf_bytes("drawing")
    authority = _authority(payload)
    observation = authority.observation(1)

    assert observation["pdf_sha256"] == hashlib.sha256(payload).hexdigest()
    assert observation["classification"] == "drawing_only"
    assert observation["evidence_complete"] is True
    assert observation["drawings"]["count"] > 0
    assert observation["display_list"]["type_counts"]["stroke-path"] > 0
    assert pv.page_visual_source_observation_v2_verified(observation, authority)
    assert (
        pv.page_visual_representation_status(observation, "geometry")
        == "reachable"
    )
    assert pv.page_visual_representation_status(observation, "raster") == "terminal"

    with pytest.raises(ValueError, match="reachable"):
        pv.build_page_visual_fallback_proof_v2(
            observation=observation,
            authority=authority,
            requested_type="geometry",
            attempted_type="geometry",
        )


@pytest.mark.parametrize(
    ("kind", "classification"),
    [("image", "image_only"), ("blank", "blank")],
)
def test_only_complete_image_or_blank_evidence_can_prove_geometry_impossible(
    kind: str,
    classification: str,
) -> None:
    authority = _authority(_pdf_bytes(kind))
    observation = authority.observation(1)

    assert observation["classification"] == classification
    assert observation["evidence_complete"] is True
    assert (
        pv.page_visual_representation_status(observation, "geometry")
        == "proven_impossible"
    )
    proof = pv.build_page_visual_fallback_proof_v2(
        observation=observation,
        authority=authority,
        requested_type="geometry",
        attempted_type="geometry",
    )
    assert pv.page_visual_fallback_proof_v2_verified(
        proof,
        observation=observation,
        authority=authority,
        expected_requested_type="geometry",
        expected_attempted_type="geometry",
    )


def test_real_mixed_page_keeps_geometry_reachable() -> None:
    authority = _authority(_pdf_bytes("mixed"))
    observation = authority.observation(1)

    assert observation["classification"] == "mixed_drawing_image"
    assert (
        pv.page_visual_representation_status(observation, "geometry")
        == "reachable"
    )
    with pytest.raises(ValueError, match="reachable"):
        pv.build_page_visual_fallback_proof_v2(
            observation=observation,
            authority=authority,
            requested_type="geometry",
            attempted_type="geometry",
        )


def test_coordinated_observation_and_proof_redigest_cannot_replace_authority() -> None:
    authority = _authority(_pdf_bytes("image"))
    original = authority.observation(1)
    proof = pv.build_page_visual_fallback_proof_v2(
        observation=original,
        authority=authority,
        requested_type="geometry",
        attempted_type="geometry",
    )

    forged_observation = copy.deepcopy(original)
    forged_observation["classification"] = "blank"
    forged_observation["observation_sha256"] = (
        pv.page_visual_source_observation_v2_digest(forged_observation)
    )
    forged_proof = copy.deepcopy(proof)
    forged_proof["observation_sha256"] = forged_observation[
        "observation_sha256"
    ]
    forged_proof["proof_sha256"] = pv.page_visual_fallback_proof_v2_digest(
        forged_proof
    )

    assert not pv.page_visual_source_observation_v2_verified(
        forged_observation,
        authority,
    )
    assert not pv.page_visual_fallback_proof_v2_verified(
        forged_proof,
        observation=forged_observation,
        authority=authority,
        expected_requested_type="geometry",
        expected_attempted_type="geometry",
    )
    assert authority.observation(1) == original


def test_recomputed_objects_cannot_cross_to_a_different_fresh_pdf_authority() -> None:
    first_authority = _authority(_pdf_bytes("blank"))
    second_authority = _authority(_pdf_bytes("image"))
    first_observation = first_authority.observation(1)
    first_proof = pv.build_page_visual_fallback_proof_v2(
        observation=first_observation,
        authority=first_authority,
        requested_type="labels",
        attempted_type="labels",
    )

    second_observation = second_authority.observation(1)
    forged_observation = copy.deepcopy(first_observation)
    for field in (
        "pdf_sha256",
        "pdf_size_bytes",
        "pdf_page_count",
        "page_xref",
        "extractor",
        "page_geometry",
    ):
        forged_observation[field] = copy.deepcopy(second_observation[field])
    forged_observation["observation_sha256"] = (
        pv.page_visual_source_observation_v2_digest(forged_observation)
    )
    assert (
        pv.page_visual_representation_status(forged_observation, "labels")
        == "proven_impossible"
    )

    forged_proof = copy.deepcopy(first_proof)
    forged_proof["pdf_sha256"] = second_observation["pdf_sha256"]
    forged_proof["observation_sha256"] = forged_observation[
        "observation_sha256"
    ]
    forged_proof["session_anchor_sha256"] = second_authority.session_anchor()[
        "session_anchor_sha256"
    ]
    forged_proof["proof_sha256"] = pv.page_visual_fallback_proof_v2_digest(
        forged_proof
    )

    assert not pv.page_visual_source_observation_v2_verified(
        forged_observation,
        second_authority,
    )
    assert not pv.page_visual_fallback_proof_v2_verified(
        forged_proof,
        observation=forged_observation,
        authority=second_authority,
        expected_requested_type="labels",
        expected_attempted_type="labels",
    )


def test_multi_page_anchor_rejects_leaf_reordering_and_scope_reuse() -> None:
    authority = _authority(_pdf_bytes("blank", pages=2), pages=2)
    anchor = authority.session_anchor()

    assert pv.page_visual_session_anchor_verified(anchor, authority)
    assert [leaf["page_number"] for leaf in anchor["page_leaves"]] == [1, 2]

    reordered = copy.deepcopy(anchor)
    reordered["page_leaves"].reverse()
    reordered["session_anchor_sha256"] = pv.page_visual_session_anchor_digest(
        reordered
    )
    assert not pv.page_visual_session_anchor_verified(reordered, authority)

    with pytest.raises(ValueError, match="unique"):
        pv.capture_fresh_page_visual_authority(
            _pdf_bytes("blank", pages=2),
            importer_identity=IMPORTER,
            page_scope_ids={1: "page_visual:same", 2: "page_visual:same"},
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pdf_sha256", "0" * 64),
        ("page_number", 2),
        ("importer_identity", "tests/forged-importer"),
        ("source_item_id", "page_visual:forged"),
    ],
)
def test_authority_rejects_pdf_page_importer_and_scope_leaf_swaps(
    field: str,
    value: object,
) -> None:
    authority = _authority(_pdf_bytes("blank"))
    forged = authority.observation(1)
    forged[field] = value
    forged["observation_sha256"] = (
        pv.page_visual_source_observation_v2_digest(forged)
    )

    assert not pv.page_visual_source_observation_v2_verified(forged, authority)


@pytest.mark.parametrize(
    "digest_location",
    ["raw", "decoded", "read_contents"],
)
def test_authority_rejects_exact_content_digest_tamper(
    digest_location: str,
) -> None:
    authority = _authority(_pdf_bytes("mixed"))
    original = authority.observation(1)
    proof = pv.build_page_visual_fallback_proof_v2(
        observation=original,
        authority=authority,
        requested_type="labels",
        attempted_type="labels",
    )
    forged = copy.deepcopy(original)
    content = forged["content_streams"]
    if digest_location == "read_contents":
        content["read_contents_sha256"] = "f" * 64
    else:
        content["streams"][0][f"{digest_location}_sha256"] = "f" * 64
        content["sequence_sha256"] = (
            pv.page_visual_content_stream_sequence_digest(content["streams"])
        )
    forged["observation_sha256"] = (
        pv.page_visual_source_observation_v2_digest(forged)
    )
    forged_proof = copy.deepcopy(proof)
    forged_proof["observation_sha256"] = forged["observation_sha256"]
    forged_proof["proof_sha256"] = pv.page_visual_fallback_proof_v2_digest(
        forged_proof
    )

    assert not pv.page_visual_source_observation_v2_verified(forged, authority)
    assert not pv.page_visual_fallback_proof_v2_verified(
        forged_proof,
        observation=forged,
        authority=authority,
        expected_requested_type="labels",
        expected_attempted_type="labels",
    )


class _FakePage:
    mediabox = (0.0, 0.0, 200.0, 100.0)
    cropbox = (0.0, 0.0, 200.0, 100.0)
    rect = (0.0, 0.0, 200.0, 100.0)
    rotation = 0
    xref = 4

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def get_text(self, *_args, **_kwargs):
        return {"width": 200.0, "height": 100.0, "blocks": []}

    def get_drawings(self):
        if self.mode == "drawing_failure":
            raise RuntimeError("drawing extractor failed")
        if self.mode == "drawing_mismatch":
            return [{"type": "l", "items": []}]
        return []

    def get_bboxlog(self, **_kwargs):
        if self.mode == "shade":
            return [("fill-shade", (0.0, 0.0, 10.0, 10.0))]
        if self.mode == "unknown":
            return [("future-paint", (0.0, 0.0, 10.0, 10.0))]
        return []

    def get_image_info(self, **_kwargs):
        return []

    def get_contents(self):
        return [5] if self.mode == "decoded_failure" else []

    def read_contents(self):
        return b"q Q" if self.mode == "decoded_failure" else b""

    def annots(self):
        return None

    def widgets(self):
        return None

    def get_links(self):
        return []

    def annot_xrefs(self):
        return []


class _FakeDocument:
    page_count = 1
    needs_pass = False
    is_encrypted = False

    def __init__(self, mode: str) -> None:
        self.page = _FakePage(mode)
        self.mode = mode

    def __getitem__(self, index: int):
        assert index == 0
        return self.page

    def xref_stream_raw(self, xref: int):
        assert xref == 5
        return b"q Q"

    def xref_stream(self, xref: int):
        assert xref == 5
        if self.mode == "decoded_failure":
            raise RuntimeError("decode failed")
        return b"q Q"

    def close(self) -> None:
        pass


class _FakeFitz:
    VersionBind = "1.28.0"
    Rect = fitz.Rect
    Point = fitz.Point
    Matrix = fitz.Matrix
    Quad = fitz.Quad

    def __init__(self, mode: str) -> None:
        self.mode = mode

    def open(self, *_args, **_kwargs):
        return _FakeDocument(self.mode)


@pytest.mark.parametrize(
    "mode",
    [
        "drawing_failure",
        "drawing_mismatch",
        "unknown",
        "decoded_failure",
    ],
)
def test_extractor_failure_mismatch_and_unknown_are_indeterminate(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    module = _FakeFitz(mode)
    monkeypatch.setattr(pv, "_load_page_visual_fitz", lambda: module)
    authority = pv.capture_fresh_page_visual_authority(
        b"%PDF-fake-page-visual-v2",
        importer_identity=IMPORTER,
        page_scope_ids={1: "page_visual:1"},
    )
    observation = authority.observation(1)

    assert observation["classification"] == "indeterminate"
    assert observation["evidence_complete"] is False
    assert (
        pv.page_visual_representation_status(observation, "geometry")
        == "indeterminate"
    )
    with pytest.raises(ValueError, match="indeterminate"):
        pv.build_page_visual_fallback_proof_v2(
            observation=observation,
            authority=authority,
            requested_type="geometry",
            attempted_type="geometry",
        )


def test_shading_paint_keeps_geometry_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _FakeFitz("shade")
    monkeypatch.setattr(pv, "_load_page_visual_fitz", lambda: module)
    authority = pv.capture_fresh_page_visual_authority(
        b"%PDF-fake-page-visual-v2",
        importer_identity=IMPORTER,
        page_scope_ids={1: "page_visual:1"},
    )
    observation = authority.observation(1)

    assert observation["classification"] == "shade_only"
    assert observation["evidence_complete"] is True
    assert pv.page_visual_representation_status(observation, "geometry") == "reachable"


def test_v1_page_visual_objects_are_legacy_untrusted() -> None:
    assert (
        pv.page_visual_schema_trust(
            {"schema": pv.PAGE_VISUAL_SOURCE_OBSERVATION_SCHEMA}
        )
        == "legacy_untrusted"
    )


def test_v2_contract_rejects_extra_keys_wrong_exact_types_and_nonfinite_values() -> None:
    authority = _authority(_pdf_bytes("blank"))
    observation = authority.observation(1)
    proof = pv.build_page_visual_fallback_proof_v2(
        observation=observation,
        authority=authority,
        requested_type="geometry",
        attempted_type="geometry",
    )

    extra_observation = copy.deepcopy(observation)
    extra_observation["extra"] = True
    extra_observation["observation_sha256"] = (
        pv.page_visual_source_observation_v2_digest(extra_observation)
    )
    assert not pv.page_visual_source_observation_v2_verified(
        extra_observation,
        authority,
    )

    wrong_type = copy.deepcopy(observation)
    wrong_type["page_number"] = True
    wrong_type["observation_sha256"] = (
        pv.page_visual_source_observation_v2_digest(wrong_type)
    )
    assert not pv.page_visual_source_observation_v2_verified(
        wrong_type,
        authority,
    )

    extra_proof = copy.deepcopy(proof)
    extra_proof["extra"] = True
    extra_proof["proof_sha256"] = pv.page_visual_fallback_proof_v2_digest(
        extra_proof
    )
    assert not pv.page_visual_fallback_proof_v2_verified(
        extra_proof,
        observation=observation,
        authority=authority,
        expected_requested_type="geometry",
        expected_attempted_type="geometry",
    )

    nonfinite = copy.deepcopy(observation)
    nonfinite["page_geometry"]["rect"][0] = float("nan")
    with pytest.raises(ValueError, match="non-finite"):
        pv.page_visual_source_observation_v2_digest(nonfinite)
    assert (
        pv.page_visual_schema_trust(
            {"schema": pv.PAGE_VISUAL_FALLBACK_PROOF_SCHEMA}
        )
        == "legacy_untrusted"
    )
