"""Page-scoped proof for visual fallback when a PDF exposes no text item.

A page is deliberately not treated as a semantic text item.  The proof binds
the exact PDF/page observation which makes structural text representations
unavailable, while allowing a verified full-page raster to preserve appearance.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Set


PAGE_VISUAL_FALLBACK_PROOF_SCHEMA = "pdf_page_visual_fallback_proof_v1"
PAGE_VISUAL_SOURCE_OBSERVATION_SCHEMA = (
    "pdf_page_visual_source_observation_v1"
)
PAGE_VISUAL_REASON_CODE = "no_canonical_text_source_items"
PAGE_VISUAL_SOURCE_OBSERVATION_V2_SCHEMA = (
    "bcs.pdf_page_visual_source_observation/2.0"
)
PAGE_VISUAL_SESSION_ANCHOR_SCHEMA = "bcs.pdf_page_visual_session_anchor/1.0"
PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA = "bcs.pdf_page_visual_fallback_proof/2.0"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_PAGE_VISUAL_NONTERMINAL_REPRESENTATIONS = frozenset(
    {"labels", "text", "3d_text", "glyphs", "geometry"}
)
_PAGE_VISUAL_REPRESENTATIONS = frozenset(
    {*_PAGE_VISUAL_NONTERMINAL_REPRESENTATIONS, "raster"}
)

PAGE_VISUAL_SOURCE_OBSERVATION_V2_FIELDS = frozenset(
    {
        "schema",
        "importer_identity",
        "pdf_sha256",
        "pdf_size_bytes",
        "pdf_page_count",
        "page_number",
        "page_xref",
        "source_item_id",
        "extractor",
        "page_geometry",
        "raw_text",
        "drawings",
        "images",
        "display_list",
        "content_streams",
        "annotations",
        "evidence_complete",
        "classification",
        "observation_sha256",
    }
)
PAGE_VISUAL_SESSION_ANCHOR_FIELDS = frozenset(
    {
        "schema",
        "importer_identity",
        "pdf_sha256",
        "pdf_size_bytes",
        "pdf_page_count",
        "extractor",
        "page_leaves",
        "session_anchor_sha256",
    }
)
PAGE_VISUAL_SESSION_LEAF_FIELDS = frozenset(
    {"page_number", "page_xref", "source_item_id", "observation_sha256"}
)
PAGE_VISUAL_FALLBACK_PROOF_V2_FIELDS = frozenset(
    {
        "schema",
        "page_specific_proven_impossible",
        "importer_identity",
        "pdf_sha256",
        "page_number",
        "source_item_id",
        "requested_type",
        "attempted_type",
        "reason_code",
        "observation_sha256",
        "session_anchor_sha256",
        "created_entity_ids",
        "removed_entity_ids",
        "cleanup_complete",
        "proof_sha256",
    }
)

_EXTRACTOR_FIELDS = frozenset({"name", "version"})
_PAGE_GEOMETRY_FIELDS = frozenset({"mediabox", "cropbox", "rect", "rotation"})
_RAW_TEXT_FIELDS = frozenset(
    {
        "status",
        "error_code",
        "dictionary_sha256",
        "total_block_count",
        "text_block_count",
        "image_block_count",
    }
)
_DRAWINGS_FIELDS = frozenset(
    {"status", "error_code", "count", "item_count", "drawings_sha256"}
)
_IMAGES_FIELDS = frozenset(
    {"status", "error_code", "displayed_count", "image_info_sha256"}
)
_DISPLAY_LIST_FIELDS = frozenset(
    {
        "status",
        "error_code",
        "layers_included",
        "entry_count",
        "entries_sha256",
        "type_counts",
    }
)
_DISPLAY_TYPE_NAMES = (
    "fill-text",
    "stroke-text",
    "ignore-text",
    "fill-path",
    "stroke-path",
    "fill-image",
    "fill-imgmask",
    "fill-shade",
    "unknown",
)
_CONTENT_STREAMS_FIELDS = frozenset(
    {
        "status",
        "error_code",
        "xref_count",
        "streams",
        "read_contents_status",
        "read_contents_error_code",
        "read_contents_length",
        "read_contents_sha256",
        "sequence_sha256",
    }
)
_CONTENT_STREAM_FIELDS = frozenset(
    {
        "ordinal",
        "xref",
        "raw_status",
        "raw_error_code",
        "raw_length",
        "raw_sha256",
        "decoded_status",
        "decoded_error_code",
        "decoded_length",
        "decoded_sha256",
    }
)
_ANNOTATION_FIELDS = frozenset(
    {
        "status",
        "error_code",
        "annotation_count",
        "widget_count",
        "link_count",
        "annot_xref_count",
        "classified_xref_count",
        "unclassified_xref_count",
        "objects_sha256",
    }
)
PAGE_VISUAL_PROOF_FIELDS = frozenset(
    {
        "schema",
        "page_specific_proven_impossible",
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
        "created_entity_ids",
        "removed_entity_ids",
        "cleanup_complete",
        "proof_sha256",
    }
)
PAGE_VISUAL_EVIDENCE_FIELDS = frozenset(
    {
        "text_dictionary_present",
        "canonical_source_item_count",
        "source_item_ids",
        "source_scope",
        "raw_text_dictionary_sha256",
        "raw_text_block_count",
        "visible_source_text_found",
    }
)
PAGE_VISUAL_RESULT_FIELDS = frozenset(
    {
        "source",
        "outcome",
        "importer_identity",
        "pdf_sha256",
        "page_number",
        "source_item_id",
        "source_item_ids",
        "canonical_source_item_count",
        "raw_text_dictionary_sha256",
        "visible_source_text_found",
    }
)
PAGE_VISUAL_SOURCE_OBSERVATION_FIELDS = frozenset(
    {
        "schema",
        "importer_identity",
        "pdf_sha256",
        "page_number",
        "source_item_id",
        "raw_text_dictionary_sha256",
        "raw_text_block_count",
        "canonical_source_item_count",
        "visible_source_text_found",
        "observation_sha256",
    }
)


def _canonical_value(value: Any, seen: Optional[Set[int]] = None) -> Any:
    if seen is None:
        seen = set()
    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("raw text dictionary contains a non-finite number")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        return {
            "__bytes_length__": len(payload),
            "__bytes_sha256__": hashlib.sha256(payload).hexdigest(),
        }
    if isinstance(value, (list, tuple, dict)):
        marker = id(value)
        if marker in seen:
            raise ValueError("raw text dictionary contains a cycle")
        seen.add(marker)
        try:
            if isinstance(value, (list, tuple)):
                return [_canonical_value(item, seen) for item in value]
            if any(type(key) is not str for key in value):
                raise ValueError("raw text dictionary contains a non-string key")
            return {
                key: _canonical_value(value[key], seen)
                for key in sorted(value)
            }
        finally:
            seen.remove(marker)
    raise ValueError("raw text dictionary contains an unsupported value")


def _strict_json_value(value: Any, seen: Optional[Set[int]] = None) -> Any:
    """Return a finite, plain-JSON copy or raise for non-contract values."""

    if seen is None:
        seen = set()
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("page visual proof contains a non-finite number")
        return value
    if type(value) not in {list, dict}:
        raise ValueError("page visual proof contains a non-JSON value")
    marker = id(value)
    if marker in seen:
        raise ValueError("page visual proof contains a cycle")
    seen.add(marker)
    try:
        if type(value) is list:
            return [_strict_json_value(item, seen) for item in value]
        if any(type(key) is not str for key in value):
            raise ValueError("page visual proof contains a non-string key")
        return {
            key: _strict_json_value(value[key], seen)
            for key in sorted(value)
        }
    finally:
        seen.remove(marker)


def raw_text_dictionary_digest(raw_text_dictionary: Dict[str, Any]) -> str:
    """Digest PyMuPDF rawdict data, including image bytes without embedding them."""

    if not isinstance(raw_text_dictionary, dict):
        raise ValueError("raw text dictionary must be a dictionary")
    payload = json.dumps(
        _canonical_value(raw_text_dictionary),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def page_visual_proof_digest(proof: Dict[str, Any]) -> str:
    """Return a canonical digest for one page-scoped impossibility proof."""

    if type(proof) is not dict:
        raise ValueError("page visual proof must be a plain dictionary")
    payload = _strict_json_value(proof)
    payload.pop("proof_sha256", None)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def page_visual_source_observation_digest(
    observation: Dict[str, Any],
) -> str:
    """Return the canonical digest for one independent page observation."""

    if type(observation) is not dict:
        raise ValueError("page visual source observation must be a plain dictionary")
    payload = _strict_json_value(observation)
    payload.pop("observation_sha256", None)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _exact_text(value: Any, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{field} must be an exact nonempty string")
    return value


def raw_text_dictionary_has_text_structure(
    raw_text_dictionary: Dict[str, Any],
) -> bool:
    """Conservatively detect text structure without consulting Unicode ink."""

    if type(raw_text_dictionary) is not dict:
        raise ValueError("raw text dictionary must be a plain dictionary")
    raw_text_dictionary_digest(raw_text_dictionary)
    if "blocks" not in raw_text_dictionary:
        raise ValueError("raw text dictionary must contain a blocks list")
    blocks = raw_text_dictionary.get("blocks")
    if type(blocks) is not list:
        raise ValueError("raw text dictionary blocks must be a list")
    for block in blocks:
        if type(block) is not dict:
            raise ValueError("raw text dictionary block must be a dictionary")
        block_type = block.get("type")
        if type(block_type) is not int or block_type not in {0, 1}:
            raise ValueError("raw text dictionary block type is unknown")
        if block_type == 0:
            return True
        if "lines" in block:
            return True
    return False


def build_page_visual_source_observation(
    *,
    importer_identity: str,
    pdf_sha256: str,
    page_number: int,
    source_scope_id: str,
    raw_text_dictionary: Dict[str, Any],
) -> Dict[str, Any]:
    """Seal a page observation, rejecting every source-text structure."""

    importer = _exact_text(importer_identity, "importer_identity")
    pdf_digest = _exact_text(pdf_sha256, "pdf_sha256")
    if _SHA256_RE.fullmatch(pdf_digest) is None:
        raise ValueError("pdf_sha256 must be a lowercase SHA-256 digest")
    if type(page_number) is not int or page_number <= 0:
        raise ValueError("page_number must be a positive exact integer")
    source_scope = _exact_text(source_scope_id, "source_scope_id")
    raw_digest = raw_text_dictionary_digest(raw_text_dictionary)
    if raw_text_dictionary_has_text_structure(raw_text_dictionary):
        raise ValueError(
            "raw text dictionary contains source-text structure; "
            "zero-text proof is unavailable"
        )
    observation = {
        "schema": PAGE_VISUAL_SOURCE_OBSERVATION_SCHEMA,
        "importer_identity": importer,
        "pdf_sha256": pdf_digest,
        "page_number": page_number,
        "source_item_id": source_scope,
        "raw_text_dictionary_sha256": raw_digest,
        "raw_text_block_count": 0,
        "canonical_source_item_count": 0,
        "visible_source_text_found": False,
    }
    observation["observation_sha256"] = (
        page_visual_source_observation_digest(observation)
    )
    return observation


def page_visual_source_observation_verified(
    observation: Any,
    *,
    expected_importer_identity: str,
    expected_pdf_sha256: str,
    expected_page_number: int,
    expected_source_scope_id: str,
) -> bool:
    """Verify a sealed report-level page observation; total and fail-closed."""

    try:
        return bool(
            type(observation) is dict
            and set(observation) == PAGE_VISUAL_SOURCE_OBSERVATION_FIELDS
            and type(expected_importer_identity) is str
            and expected_importer_identity
            and expected_importer_identity == expected_importer_identity.strip()
            and type(expected_pdf_sha256) is str
            and _SHA256_RE.fullmatch(expected_pdf_sha256) is not None
            and type(expected_page_number) is int
            and expected_page_number > 0
            and type(expected_source_scope_id) is str
            and expected_source_scope_id
            and expected_source_scope_id == expected_source_scope_id.strip()
            and observation.get("schema")
            == PAGE_VISUAL_SOURCE_OBSERVATION_SCHEMA
            and observation.get("importer_identity")
            == expected_importer_identity
            and observation.get("pdf_sha256") == expected_pdf_sha256
            and observation.get("page_number") == expected_page_number
            and observation.get("source_item_id")
            == expected_source_scope_id
            and type(observation.get("raw_text_dictionary_sha256")) is str
            and _SHA256_RE.fullmatch(
                observation.get("raw_text_dictionary_sha256")
            )
            is not None
            and type(observation.get("raw_text_block_count")) is int
            and observation.get("raw_text_block_count") == 0
            and type(observation.get("canonical_source_item_count")) is int
            and observation.get("canonical_source_item_count") == 0
            and observation.get("visible_source_text_found") is False
            and type(observation.get("observation_sha256")) is str
            and _SHA256_RE.fullmatch(observation.get("observation_sha256"))
            is not None
            and observation.get("observation_sha256")
            == page_visual_source_observation_digest(observation)
        )
    except Exception:
        return False


def build_page_visual_fallback_proof(
    *,
    importer_identity: str,
    pdf_sha256: str,
    page_number: int,
    source_scope_id: str,
    requested_type: str,
    attempted_type: str,
    raw_text_dictionary: Dict[str, Any],
) -> Dict[str, Any]:
    """Build the only supported page-scoped impossibility proof."""

    requested = _exact_text(requested_type, "requested_type")
    attempted = _exact_text(attempted_type, "attempted_type")
    if attempted == "raster":
        raise ValueError("raster is a terminal delivery, not an impossible attempt")
    observation = build_page_visual_source_observation(
        importer_identity=importer_identity,
        pdf_sha256=pdf_sha256,
        page_number=page_number,
        source_scope_id=source_scope_id,
        raw_text_dictionary=raw_text_dictionary,
    )
    raw_digest = observation["raw_text_dictionary_sha256"]
    importer = observation["importer_identity"]
    proof = {
        "schema": PAGE_VISUAL_FALLBACK_PROOF_SCHEMA,
        "page_specific_proven_impossible": True,
        "importer_identity": importer,
        "pdf_sha256": observation["pdf_sha256"],
        "page_number": observation["page_number"],
        "source_item_id": observation["source_item_id"],
        "requested_type": requested,
        "attempted_type": attempted,
        "reason_code": PAGE_VISUAL_REASON_CODE,
        "evidence": {
            "text_dictionary_present": True,
            "canonical_source_item_count": 0,
            "source_item_ids": [],
            "source_scope": "page_visual",
            "raw_text_dictionary_sha256": raw_digest,
            "raw_text_block_count": 0,
            "visible_source_text_found": False,
        },
        "attempted_source_results": [
            {
                "source": "pymupdf_raw_text_dictionary",
                "outcome": PAGE_VISUAL_REASON_CODE,
                "importer_identity": importer,
                "pdf_sha256": observation["pdf_sha256"],
                "page_number": observation["page_number"],
                "source_item_id": observation["source_item_id"],
                "source_item_ids": [],
                "canonical_source_item_count": 0,
                "raw_text_dictionary_sha256": raw_digest,
                "visible_source_text_found": False,
            }
        ],
        "attempted_sources_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }
    proof["proof_sha256"] = page_visual_proof_digest(proof)
    if not page_visual_fallback_proof_verified(
        proof,
        expected_pdf_sha256=observation["pdf_sha256"],
        expected_page_number=observation["page_number"],
        expected_source_scope_id=observation["source_item_id"],
        expected_requested_type=requested,
        expected_attempted_type=attempted,
        expected_importer_identity=importer,
        expected_raw_text_dictionary_sha256=raw_digest,
    ):
        raise ValueError("page visual fallback proof could not be sealed")
    return proof


def page_visual_fallback_proof_verified(
    proof: Any,
    *,
    expected_pdf_sha256: str,
    expected_page_number: int,
    expected_source_scope_id: str,
    expected_requested_type: str,
    expected_attempted_type: str,
    expected_importer_identity: str,
    expected_raw_text_dictionary_sha256: str,
) -> bool:
    """Verify one proof without allowing malformed evidence to escape."""

    try:
        return _page_visual_fallback_proof_verified(
            proof,
            expected_pdf_sha256=expected_pdf_sha256,
            expected_page_number=expected_page_number,
            expected_source_scope_id=expected_source_scope_id,
            expected_requested_type=expected_requested_type,
            expected_attempted_type=expected_attempted_type,
            expected_importer_identity=expected_importer_identity,
            expected_raw_text_dictionary_sha256=(
                expected_raw_text_dictionary_sha256
            ),
        )
    except Exception:
        return False


def _page_visual_fallback_proof_verified(
    proof: Any,
    *,
    expected_pdf_sha256: str,
    expected_page_number: int,
    expected_source_scope_id: str,
    expected_requested_type: str,
    expected_attempted_type: str,
    expected_importer_identity: str,
    expected_raw_text_dictionary_sha256: str,
) -> bool:
    """Verify one proof against independently held source and host identity."""

    if (
        type(proof) is not dict
        or set(proof) != PAGE_VISUAL_PROOF_FIELDS
        or type(expected_pdf_sha256) is not str
        or _SHA256_RE.fullmatch(expected_pdf_sha256) is None
        or type(expected_page_number) is not int
        or expected_page_number <= 0
        or type(expected_source_scope_id) is not str
        or not expected_source_scope_id
        or expected_source_scope_id != expected_source_scope_id.strip()
        or type(expected_requested_type) is not str
        or not expected_requested_type
        or expected_requested_type != expected_requested_type.strip()
        or type(expected_attempted_type) is not str
        or not expected_attempted_type
        or expected_attempted_type != expected_attempted_type.strip()
        or type(expected_importer_identity) is not str
        or not expected_importer_identity
        or expected_importer_identity != expected_importer_identity.strip()
        or type(expected_raw_text_dictionary_sha256) is not str
        or _SHA256_RE.fullmatch(expected_raw_text_dictionary_sha256) is None
    ):
        return False

    evidence = proof.get("evidence")
    results = proof.get("attempted_source_results")
    created_ids = proof.get("created_entity_ids")
    removed_ids = proof.get("removed_entity_ids")
    proof_sha256 = proof.get("proof_sha256")
    if (
        type(evidence) is not dict
        or set(evidence) != PAGE_VISUAL_EVIDENCE_FIELDS
        or type(results) is not list
        or len(results) != 1
        or type(results[0]) is not dict
        or set(results[0]) != PAGE_VISUAL_RESULT_FIELDS
        or type(created_ids) is not list
        or created_ids != []
        or type(removed_ids) is not list
        or removed_ids != []
        or proof.get("schema") != PAGE_VISUAL_FALLBACK_PROOF_SCHEMA
        or proof.get("page_specific_proven_impossible") is not True
        or proof.get("importer_identity") != expected_importer_identity
        or proof.get("pdf_sha256") != expected_pdf_sha256
        or proof.get("page_number") != expected_page_number
        or proof.get("source_item_id") != expected_source_scope_id
        or proof.get("requested_type") != expected_requested_type
        or proof.get("attempted_type") != expected_attempted_type
        or proof.get("reason_code") != PAGE_VISUAL_REASON_CODE
        or proof.get("attempted_sources_complete") is not True
        or proof.get("cleanup_complete") is not True
        or evidence.get("text_dictionary_present") is not True
        or type(evidence.get("canonical_source_item_count")) is not int
        or evidence.get("canonical_source_item_count") != 0
        or evidence.get("source_item_ids") != []
        or evidence.get("source_scope") != "page_visual"
        or evidence.get("visible_source_text_found") is not False
        or evidence.get("raw_text_dictionary_sha256")
        != expected_raw_text_dictionary_sha256
        or type(evidence.get("raw_text_block_count")) is not int
        or evidence.get("raw_text_block_count") != 0
        or type(proof_sha256) is not str
        or _SHA256_RE.fullmatch(proof_sha256) is None
    ):
        return False

    [result] = results
    return bool(
        result.get("source") == "pymupdf_raw_text_dictionary"
        and result.get("outcome") == "no_canonical_text_source_items"
        and result.get("importer_identity") == expected_importer_identity
        and result.get("pdf_sha256") == expected_pdf_sha256
        and result.get("page_number") == expected_page_number
        and result.get("source_item_id") == expected_source_scope_id
        and result.get("source_item_ids") == []
        and type(result.get("canonical_source_item_count")) is int
        and result.get("canonical_source_item_count") == 0
        and result.get("raw_text_dictionary_sha256")
        == expected_raw_text_dictionary_sha256
        and result.get("visible_source_text_found") is False
        and proof_sha256 == page_visual_proof_digest(proof)
    )


def _page_visual_v2_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _strict_json_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _page_visual_v2_json_digest(value: Any) -> str:
    return hashlib.sha256(_page_visual_v2_json_bytes(value)).hexdigest()


def _plain_source_value(
    value: Any,
    seen: Optional[Set[int]] = None,
    *,
    pymupdf_module: Any = None,
) -> Any:
    """Normalize PyMuPDF evidence to finite JSON without keeping source bytes."""

    if seen is None:
        seen = set()
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("source evidence contains a non-finite number")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        return {
            "__bytes_length__": len(payload),
            "__bytes_sha256__": hashlib.sha256(payload).hexdigest(),
        }
    pymupdf_types = tuple(
        candidate
        for candidate in (
            getattr(pymupdf_module, name, None)
            for name in ("Rect", "Point", "Matrix", "Quad")
        )
        if isinstance(candidate, type)
    )
    if pymupdf_types and isinstance(value, pymupdf_types):
        marker = id(value)
        if marker in seen:
            raise ValueError("source evidence contains a cycle")
        seen.add(marker)
        try:
            return [
                _plain_source_value(
                    item,
                    seen,
                    pymupdf_module=pymupdf_module,
                )
                for item in value
            ]
        finally:
            seen.remove(marker)
    if type(value) in {list, tuple, dict}:
        marker = id(value)
        if marker in seen:
            raise ValueError("source evidence contains a cycle")
        seen.add(marker)
        try:
            if type(value) in {list, tuple}:
                return [
                    _plain_source_value(
                        item,
                        seen,
                        pymupdf_module=pymupdf_module,
                    )
                    for item in value
                ]
            if any(type(key) is not str for key in value):
                raise ValueError("source evidence contains a non-string key")
            return {
                key: _plain_source_value(
                    value[key],
                    seen,
                    pymupdf_module=pymupdf_module,
                )
                for key in sorted(value)
            }
        finally:
            seen.remove(marker)
    raise ValueError("source evidence contains an unsupported value")


def _finite_source_number(value: Any) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError("source evidence contains a non-finite number")
    return float(value)


def _source_digest(value: Any, *, pymupdf_module: Any = None) -> str:
    return _page_visual_v2_json_digest(
        _plain_source_value(value, pymupdf_module=pymupdf_module)
    )


def _bytes_payload(value: Any) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("content stream extractor did not return bytes")
    return bytes(value)


def page_visual_source_observation_v2_digest(observation: Dict[str, Any]) -> str:
    if type(observation) is not dict:
        raise ValueError("v2 page visual observation must be a plain dictionary")
    payload = _strict_json_value(observation)
    payload.pop("observation_sha256", None)
    return _page_visual_v2_json_digest(payload)


def page_visual_session_anchor_digest(anchor: Dict[str, Any]) -> str:
    if type(anchor) is not dict:
        raise ValueError("page visual session anchor must be a plain dictionary")
    payload = _strict_json_value(anchor)
    payload.pop("session_anchor_sha256", None)
    return _page_visual_v2_json_digest(payload)


def page_visual_fallback_proof_v2_digest(proof: Dict[str, Any]) -> str:
    if type(proof) is not dict:
        raise ValueError("v2 page visual proof must be a plain dictionary")
    payload = _strict_json_value(proof)
    payload.pop("proof_sha256", None)
    return _page_visual_v2_json_digest(payload)


def page_visual_content_stream_sequence_digest(streams: Any) -> str:
    if type(streams) is not list:
        raise ValueError("content stream sequence must be a plain list")
    return _page_visual_v2_json_digest(streams)


def _capture_raw_text(page: Any, pymupdf_module: Any) -> Dict[str, Any]:
    try:
        raw = page.get_text("rawdict", sort=False)
        normalized = _plain_source_value(raw, pymupdf_module=pymupdf_module)
        if type(normalized) is not dict or type(normalized.get("blocks")) is not list:
            raise ValueError("raw text dictionary has no blocks list")
        text_count = 0
        image_count = 0
        for block in normalized["blocks"]:
            if type(block) is not dict or type(block.get("type")) is not int:
                raise ValueError("raw text dictionary has a malformed block")
            if block["type"] == 0:
                text_count += 1
            elif block["type"] == 1:
                image_count += 1
            else:
                raise ValueError("raw text dictionary has an unknown block type")
        return {
            "status": "ok",
            "error_code": "",
            "dictionary_sha256": _page_visual_v2_json_digest(normalized),
            "total_block_count": len(normalized["blocks"]),
            "text_block_count": text_count,
            "image_block_count": image_count,
        }
    except Exception:
        return {
            "status": "error",
            "error_code": "raw_text_extraction_failed",
            "dictionary_sha256": "",
            "total_block_count": 0,
            "text_block_count": 0,
            "image_block_count": 0,
        }


def _capture_drawings(page: Any, pymupdf_module: Any) -> Dict[str, Any]:
    try:
        normalized = _plain_source_value(
            page.get_drawings(),
            pymupdf_module=pymupdf_module,
        )
        if type(normalized) is not list or any(
            type(item) is not dict for item in normalized
        ):
            raise ValueError("drawing extractor returned a malformed list")
        item_count = 0
        for drawing in normalized:
            items = drawing.get("items", [])
            if type(items) is not list:
                raise ValueError("drawing extractor returned malformed items")
            item_count += len(items)
        return {
            "status": "ok",
            "error_code": "",
            "count": len(normalized),
            "item_count": item_count,
            "drawings_sha256": _page_visual_v2_json_digest(normalized),
        }
    except Exception:
        return {
            "status": "error",
            "error_code": "drawing_extraction_failed",
            "count": 0,
            "item_count": 0,
            "drawings_sha256": "",
        }


def _capture_images(page: Any, pymupdf_module: Any) -> Dict[str, Any]:
    try:
        images = page.get_image_info(hashes=True, xrefs=True)
        normalized = _plain_source_value(images, pymupdf_module=pymupdf_module)
        if type(normalized) is not list or any(
            type(item) is not dict for item in normalized
        ):
            raise ValueError("image-info extractor returned a malformed list")
        return {
            "status": "ok",
            "error_code": "",
            "displayed_count": len(normalized),
            "image_info_sha256": _page_visual_v2_json_digest(normalized),
        }
    except Exception:
        return {
            "status": "error",
            "error_code": "image_info_extraction_failed",
            "displayed_count": 0,
            "image_info_sha256": "",
        }


def _capture_display_list(page: Any, pymupdf_module: Any) -> Dict[str, Any]:
    layers_included = True
    try:
        entries = page.get_bboxlog(layers=True)
        normalized = _plain_source_value(entries, pymupdf_module=pymupdf_module)
        if type(normalized) is not list:
            raise ValueError("bbox log extractor returned a malformed list")
        counts = {name: 0 for name in _DISPLAY_TYPE_NAMES}
        for entry in normalized:
            if type(entry) is not list or len(entry) not in {2, 3}:
                raise ValueError("bbox log entry is malformed")
            entry_type = entry[0]
            bbox = entry[1]
            if type(entry_type) is not str or type(bbox) is not list or len(bbox) != 4:
                raise ValueError("bbox log entry is malformed")
            for coordinate in bbox:
                _finite_source_number(coordinate)
            if entry_type in counts:
                counts[entry_type] += 1
            else:
                counts["unknown"] += 1
        return {
            "status": "ok",
            "error_code": "",
            "layers_included": layers_included,
            "entry_count": len(normalized),
            "entries_sha256": _page_visual_v2_json_digest(normalized),
            "type_counts": counts,
        }
    except Exception:
        return {
            "status": "error",
            "error_code": "display_list_extraction_failed",
            "layers_included": layers_included,
            "entry_count": 0,
            "entries_sha256": "",
            "type_counts": {name: 0 for name in _DISPLAY_TYPE_NAMES},
        }


def _capture_content_streams(document: Any, page: Any) -> Dict[str, Any]:
    try:
        xrefs = page.get_contents()
        if type(xrefs) is not list or any(
            type(xref) is not int or xref <= 0 for xref in xrefs
        ):
            raise ValueError("content xref extractor returned a malformed list")
    except Exception:
        return {
            "status": "error",
            "error_code": "content_xref_extraction_failed",
            "xref_count": 0,
            "streams": [],
            "read_contents_status": "error",
            "read_contents_error_code": "content_xref_extraction_failed",
            "read_contents_length": 0,
            "read_contents_sha256": "",
            "sequence_sha256": page_visual_content_stream_sequence_digest([]),
        }

    streams = []
    all_streams_complete = True
    for ordinal, xref in enumerate(xrefs):
        try:
            raw = _bytes_payload(document.xref_stream_raw(xref))
            raw_status = "ok"
            raw_error = ""
            raw_length = len(raw)
            raw_sha = hashlib.sha256(raw).hexdigest()
        except Exception:
            all_streams_complete = False
            raw_status = "error"
            raw_error = "raw_content_stream_failed"
            raw_length = 0
            raw_sha = ""
        try:
            decoded = _bytes_payload(document.xref_stream(xref))
            decoded_status = "ok"
            decoded_error = ""
            decoded_length = len(decoded)
            decoded_sha = hashlib.sha256(decoded).hexdigest()
        except Exception:
            all_streams_complete = False
            decoded_status = "error"
            decoded_error = "decoded_content_stream_failed"
            decoded_length = 0
            decoded_sha = ""
        streams.append(
            {
                "ordinal": ordinal,
                "xref": xref,
                "raw_status": raw_status,
                "raw_error_code": raw_error,
                "raw_length": raw_length,
                "raw_sha256": raw_sha,
                "decoded_status": decoded_status,
                "decoded_error_code": decoded_error,
                "decoded_length": decoded_length,
                "decoded_sha256": decoded_sha,
            }
        )
    try:
        read_contents = _bytes_payload(page.read_contents())
        read_status = "ok"
        read_error = ""
        read_length = len(read_contents)
        read_sha = hashlib.sha256(read_contents).hexdigest()
    except Exception:
        all_streams_complete = False
        read_status = "error"
        read_error = "read_contents_failed"
        read_length = 0
        read_sha = ""
    return {
        "status": "ok" if all_streams_complete else "error",
        "error_code": "" if all_streams_complete else "content_stream_incomplete",
        "xref_count": len(xrefs),
        "streams": streams,
        "read_contents_status": read_status,
        "read_contents_error_code": read_error,
        "read_contents_length": read_length,
        "read_contents_sha256": read_sha,
        "sequence_sha256": page_visual_content_stream_sequence_digest(streams),
    }


def _object_descriptor(
    value: Any,
    attributes: Any,
    pymupdf_module: Any,
) -> Dict[str, Any]:
    return {
        name: _plain_source_value(
            getattr(value, name, None),
            pymupdf_module=pymupdf_module,
        )
        for name in attributes
    }


def _optional_object_list(page: Any, method_name: str) -> Any:
    method = getattr(page, method_name)
    values = method()
    return [] if values is None else list(values)


def _capture_annotations(page: Any, pymupdf_module: Any) -> Dict[str, Any]:
    try:
        annotations = _optional_object_list(page, "annots")
        widgets = _optional_object_list(page, "widgets")
        links = page.get_links()
        if type(links) is not list:
            raise ValueError("link extractor returned a malformed list")
        annot_xrefs = _plain_source_value(
            page.annot_xrefs(),
            pymupdf_module=pymupdf_module,
        )
        if type(annot_xrefs) is not list or any(
            type(item) is not list
            or len(item) not in {2, 3}
            or type(item[0]) is not int
            or item[0] <= 0
            or type(item[1]) is not int
            or (len(item) == 3 and type(item[2]) is not str)
            for item in annot_xrefs
        ):
            raise ValueError("annotation xref extractor returned a malformed list")
        all_xrefs = [item[0] for item in annot_xrefs]
        if len(set(all_xrefs)) != len(all_xrefs):
            raise ValueError("annotation xref extractor returned duplicate xrefs")
        known_xrefs = []
        for item in list(annotations) + list(widgets):
            xref = getattr(item, "xref", None)
            if type(xref) is not int or xref <= 0:
                raise ValueError("annotation object has an invalid xref")
            known_xrefs.append(xref)
        for link in links:
            if type(link) is not dict:
                raise ValueError("link extractor returned a malformed record")
            xref = link.get("xref")
            if type(xref) is not int or xref <= 0:
                raise ValueError("link record has an invalid xref")
            known_xrefs.append(xref)
        if (
            len(set(known_xrefs)) != len(known_xrefs)
            or not set(known_xrefs).issubset(all_xrefs)
        ):
            raise ValueError("annotation objects do not match annot_xrefs")
        unclassified_count = len(set(all_xrefs).difference(known_xrefs))
        normalized = {
            "annot_xrefs": annot_xrefs,
            "annotations": [
                _object_descriptor(
                    item,
                    ("xref", "type", "rect", "info"),
                    pymupdf_module,
                )
                for item in annotations
            ],
            "widgets": [
                _object_descriptor(
                    item,
                    ("xref", "field_name", "field_type", "field_value", "rect"),
                    pymupdf_module,
                )
                for item in widgets
            ],
            "links": _plain_source_value(
                links,
                pymupdf_module=pymupdf_module,
            ),
        }
        return {
            "status": "ok",
            "error_code": "",
            "annotation_count": len(annotations),
            "widget_count": len(widgets),
            "link_count": len(links),
            "annot_xref_count": len(all_xrefs),
            "classified_xref_count": len(known_xrefs),
            "unclassified_xref_count": unclassified_count,
            "objects_sha256": _page_visual_v2_json_digest(normalized),
        }
    except Exception:
        return {
            "status": "error",
            "error_code": "annotation_extraction_failed",
            "annotation_count": 0,
            "widget_count": 0,
            "link_count": 0,
            "annot_xref_count": 0,
            "classified_xref_count": 0,
            "unclassified_xref_count": 0,
            "objects_sha256": "",
        }


def _page_rectangle(value: Any, pymupdf_module: Any) -> Any:
    normalized = _plain_source_value(
        value,
        pymupdf_module=pymupdf_module,
    )
    if type(normalized) is not list or len(normalized) != 4:
        raise ValueError("page rectangle is malformed")
    return [_finite_source_number(item) for item in normalized]


def _page_visual_positive_evidence(observation: Dict[str, Any]) -> Any:
    counts = observation["display_list"]["type_counts"]
    display_ok = observation["display_list"]["status"] == "ok"
    raw_ok = observation["raw_text"]["status"] == "ok"
    drawings_ok = observation["drawings"]["status"] == "ok"
    images_ok = observation["images"]["status"] == "ok"
    text_present = bool(
        (raw_ok and observation["raw_text"]["text_block_count"] > 0)
        or (
            display_ok
            and counts["fill-text"]
            + counts["stroke-text"]
            + counts["ignore-text"]
            > 0
        )
    )
    path_present = bool(
        (display_ok and counts["fill-path"] + counts["stroke-path"] > 0)
        or (drawings_ok and observation["drawings"]["item_count"] > 0)
    )
    shade_present = bool(display_ok and counts["fill-shade"] > 0)
    image_present = bool(
        (raw_ok and observation["raw_text"]["image_block_count"] > 0)
        or (images_ok and observation["images"]["displayed_count"] > 0)
        or (
            display_ok
            and counts["fill-image"] + counts["fill-imgmask"] > 0
        )
    )
    return text_present, path_present, shade_present, image_present


def _page_visual_absence_base_complete(observation: Dict[str, Any]) -> bool:
    display = observation["display_list"]
    annotations = observation["annotations"]
    return bool(
        display["status"] == "ok"
        and display["type_counts"]["unknown"] == 0
        and observation["content_streams"]["status"] == "ok"
        and annotations["status"] == "ok"
        and annotations["annotation_count"] == 0
        and annotations["widget_count"] == 0
        and annotations["unclassified_xref_count"] == 0
    )


def _page_visual_text_absence_complete(observation: Dict[str, Any]) -> bool:
    counts = observation["display_list"]["type_counts"]
    return bool(
        _page_visual_absence_base_complete(observation)
        and observation["raw_text"]["status"] == "ok"
        and observation["raw_text"]["text_block_count"] == 0
        and counts["fill-text"] == 0
        and counts["stroke-text"] == 0
        and counts["ignore-text"] == 0
    )


def _page_visual_geometry_absence_complete(observation: Dict[str, Any]) -> bool:
    counts = observation["display_list"]["type_counts"]
    drawings = observation["drawings"]
    return bool(
        _page_visual_text_absence_complete(observation)
        and drawings["status"] == "ok"
        and drawings["count"] == 0
        and drawings["item_count"] == 0
        and counts["fill-path"] == 0
        and counts["stroke-path"] == 0
        and counts["fill-shade"] == 0
    )


def _derive_page_visual_classification(observation: Dict[str, Any]) -> Any:
    text_present, path_present, shade_present, image_present = (
        _page_visual_positive_evidence(observation)
    )
    if text_present:
        return True, "text_present"
    if path_present or shade_present:
        if image_present:
            return True, "mixed_drawing_image"
        return True, "shade_only" if shade_present and not path_present else "drawing_only"
    if image_present:
        return True, "image_only"
    if (
        _page_visual_geometry_absence_complete(observation)
        and observation["images"]["status"] == "ok"
        and observation["images"]["displayed_count"] == 0
        and observation["raw_text"]["image_block_count"] == 0
    ):
        return True, "blank"
    return False, "indeterminate"


def _capture_page_visual_observation(
    document: Any,
    page: Any,
    *,
    importer_identity: str,
    pdf_sha256: str,
    pdf_size_bytes: int,
    pdf_page_count: int,
    page_number: int,
    source_item_id: str,
    extractor: Dict[str, str],
    pymupdf_module: Any,
) -> Dict[str, Any]:
    page_xref = getattr(page, "xref", None)
    rotation = getattr(page, "rotation", None)
    if type(page_xref) is not int or page_xref <= 0:
        raise ValueError("page xref must be a positive exact integer")
    if type(rotation) is not int:
        raise ValueError("page rotation must be an exact integer")
    observation = {
        "schema": PAGE_VISUAL_SOURCE_OBSERVATION_V2_SCHEMA,
        "importer_identity": importer_identity,
        "pdf_sha256": pdf_sha256,
        "pdf_size_bytes": pdf_size_bytes,
        "pdf_page_count": pdf_page_count,
        "page_number": page_number,
        "page_xref": page_xref,
        "source_item_id": source_item_id,
        "extractor": dict(extractor),
        "page_geometry": {
            "mediabox": _page_rectangle(page.mediabox, pymupdf_module),
            "cropbox": _page_rectangle(page.cropbox, pymupdf_module),
            "rect": _page_rectangle(page.rect, pymupdf_module),
            "rotation": rotation,
        },
        "raw_text": _capture_raw_text(page, pymupdf_module),
        "drawings": _capture_drawings(page, pymupdf_module),
        "images": _capture_images(page, pymupdf_module),
        "display_list": _capture_display_list(page, pymupdf_module),
        "content_streams": _capture_content_streams(document, page),
        "annotations": _capture_annotations(page, pymupdf_module),
    }
    complete, classification = _derive_page_visual_classification(observation)
    observation["evidence_complete"] = complete
    observation["classification"] = classification
    observation["observation_sha256"] = (
        page_visual_source_observation_v2_digest(observation)
    )
    return observation


_PAGE_VISUAL_AUTHORITY_TOKEN = object()
_PYMUPDF_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2,}")
_PYMUPDF_MIN_VERSION = (1, 24, 0)
_PYMUPDF_MAX_VERSION = (2, 0, 0)
_PYMUPDF_MODULE_APIS = ("Rect", "Point", "Matrix", "Quad")
_PYMUPDF_DOCUMENT_APIS = ("close", "xref_stream_raw", "xref_stream")
_PYMUPDF_PAGE_APIS = (
    "get_text",
    "get_drawings",
    "get_image_info",
    "get_bboxlog",
    "get_contents",
    "read_contents",
    "annots",
    "widgets",
    "get_links",
    "annot_xrefs",
)


def _load_page_visual_fitz() -> Any:
    from .fitz_loader import import_fitz

    return import_fitz()


def _validated_page_visual_fitz() -> Any:
    module = _load_page_visual_fitz()
    version = getattr(module, "VersionBind", None)
    if type(version) is not str or _PYMUPDF_VERSION_RE.fullmatch(version) is None:
        raise ValueError("PyMuPDF VersionBind must contain at least three numeric parts")
    version_tuple = tuple(int(part) for part in version.split(".")[:3])
    if not (_PYMUPDF_MIN_VERSION <= version_tuple < _PYMUPDF_MAX_VERSION):
        raise ValueError("PyMuPDF VersionBind must be >= 1.24.0 and < 2.0.0")
    if not callable(getattr(module, "open", None)) or any(
        not isinstance(getattr(module, name, None), type)
        for name in _PYMUPDF_MODULE_APIS
    ):
        raise ValueError("required PyMuPDF API is unavailable")
    return module


def _require_callable_apis(value: Any, names: Any) -> None:
    if any(not callable(getattr(value, name, None)) for name in names):
        raise ValueError("required PyMuPDF API is unavailable")


class PageVisualSourceAuthority:
    """Immutable capability created only by a fresh exact-PDF extraction."""

    __slots__ = (
        "_importer_identity",
        "_pdf_sha256",
        "_pdf_size_bytes",
        "_pdf_page_count",
        "_source_bytes",
        "_extractor",
        "_observation_records",
        "_session_anchor_json",
    )

    def __init__(
        self,
        *,
        token: Any,
        importer_identity: str,
        pdf_sha256: str,
        pdf_size_bytes: int,
        pdf_page_count: int,
        source_bytes: bytes,
        extractor: Dict[str, str],
        observation_records: Any,
        session_anchor_json: str,
    ) -> None:
        if token is not _PAGE_VISUAL_AUTHORITY_TOKEN:
            raise ValueError("page visual authority must come from a fresh PDF capture")
        payload = bytes(source_bytes)
        if not payload or hashlib.sha256(payload).hexdigest() != pdf_sha256:
            raise ValueError("page visual authority source bytes are invalid")
        object.__setattr__(self, "_importer_identity", importer_identity)
        object.__setattr__(self, "_pdf_sha256", pdf_sha256)
        object.__setattr__(self, "_pdf_size_bytes", pdf_size_bytes)
        object.__setattr__(self, "_pdf_page_count", pdf_page_count)
        object.__setattr__(self, "_source_bytes", payload)
        object.__setattr__(self, "_extractor", dict(extractor))
        object.__setattr__(self, "_observation_records", tuple(observation_records))
        object.__setattr__(self, "_session_anchor_json", session_anchor_json)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("page visual source authority is immutable")

    def __reduce_ex__(self, _protocol: int) -> Any:
        raise TypeError("page visual source authority is runtime-only")

    @property
    def importer_identity(self) -> str:
        return self._importer_identity

    @property
    def pdf_sha256(self) -> str:
        return self._pdf_sha256

    @property
    def pdf_size_bytes(self) -> int:
        return self._pdf_size_bytes

    @property
    def pdf_page_count(self) -> int:
        return self._pdf_page_count

    def source_bytes_snapshot(self) -> bytes:
        return memoryview(self._source_bytes).tobytes()

    def _lookup_json(self, page_number: int, source_item_id: Any = None) -> str:
        if type(page_number) is not int or page_number <= 0:
            raise ValueError("page_number must be a positive exact integer")
        for candidate_page, candidate_scope, serialized in self._observation_records:
            if candidate_page == page_number and (
                source_item_id is None or candidate_scope == source_item_id
            ):
                return serialized
        raise KeyError(page_number)

    def observation(self, page_number: int, source_item_id: Any = None) -> Any:
        if source_item_id is not None:
            _exact_text(source_item_id, "source_item_id")
        return json.loads(self._lookup_json(page_number, source_item_id))

    def observations(self) -> Dict[str, Any]:
        return {
            source_item_id: json.loads(serialized)
            for _page_number, source_item_id, serialized in self._observation_records
        }

    def session_anchor(self) -> Dict[str, Any]:
        return json.loads(self._session_anchor_json)

    def manifest(self) -> Dict[str, Any]:
        return self.session_anchor()


def capture_fresh_page_visual_authority(
    source: Any,
    *,
    importer_identity: str,
    page_scope_ids: Dict[int, str],
) -> PageVisualSourceAuthority:
    """Extract an authority from one immutable byte snapshot of the exact PDF."""

    importer = _exact_text(importer_identity, "importer_identity")
    if isinstance(source, (bytes, bytearray, memoryview)):
        payload = bytes(source)
    elif isinstance(source, (str, os.PathLike)):
        payload = Path(source).read_bytes()
    else:
        raise ValueError("PDF source must be bytes or a filesystem path")
    if not payload:
        raise ValueError("PDF source must not be empty")
    if type(page_scope_ids) is not dict or not page_scope_ids:
        raise ValueError("page_scope_ids must be a nonempty plain dictionary")
    normalized_scopes = {}
    for page_number, source_item_id in page_scope_ids.items():
        if type(page_number) is not int or page_number <= 0:
            raise ValueError("page scope keys must be positive exact integers")
        normalized_scopes[page_number] = _exact_text(
            source_item_id,
            "source_item_id",
        )
    if len(set(normalized_scopes.values())) != len(normalized_scopes):
        raise ValueError("page source scope identifiers must be unique")

    fitz_module = _validated_page_visual_fitz()
    extractor = {"name": "PyMuPDF", "version": fitz_module.VersionBind}
    pdf_sha256 = hashlib.sha256(payload).hexdigest()
    document = None
    try:
        document = fitz_module.open(stream=payload, filetype="pdf")
        _require_callable_apis(document, _PYMUPDF_DOCUMENT_APIS)
        if not callable(getattr(document, "__getitem__", None)):
            raise ValueError("required PyMuPDF API is unavailable")
        page_count = getattr(document, "page_count", None)
        if type(page_count) is not int or page_count <= 0:
            raise ValueError("PDF must expose a positive exact page count")
        if bool(getattr(document, "needs_pass", False)) or bool(
            getattr(document, "is_encrypted", False)
        ):
            raise ValueError("encrypted PDFs cannot authorize a page fallback proof")
        if any(page_number > page_count for page_number in normalized_scopes):
            raise ValueError("page scope is outside the exact PDF")

        observation_records = []
        page_leaves = []
        for page_number in sorted(normalized_scopes):
            source_item_id = normalized_scopes[page_number]
            page = document[page_number - 1]
            _require_callable_apis(page, _PYMUPDF_PAGE_APIS)
            observation = _capture_page_visual_observation(
                document,
                page,
                importer_identity=importer,
                pdf_sha256=pdf_sha256,
                pdf_size_bytes=len(payload),
                pdf_page_count=page_count,
                page_number=page_number,
                source_item_id=source_item_id,
                extractor=extractor,
                pymupdf_module=fitz_module,
            )
            serialized = _page_visual_v2_json_bytes(observation).decode("utf-8")
            observation_records.append((page_number, source_item_id, serialized))
            page_leaves.append(
                {
                    "page_number": page_number,
                    "page_xref": observation["page_xref"],
                    "source_item_id": source_item_id,
                    "observation_sha256": observation["observation_sha256"],
                }
            )
        anchor = {
            "schema": PAGE_VISUAL_SESSION_ANCHOR_SCHEMA,
            "importer_identity": importer,
            "pdf_sha256": pdf_sha256,
            "pdf_size_bytes": len(payload),
            "pdf_page_count": page_count,
            "extractor": extractor,
            "page_leaves": page_leaves,
        }
        anchor["session_anchor_sha256"] = page_visual_session_anchor_digest(anchor)
        authority = PageVisualSourceAuthority(
            token=_PAGE_VISUAL_AUTHORITY_TOKEN,
            importer_identity=importer,
            pdf_sha256=pdf_sha256,
            pdf_size_bytes=len(payload),
            pdf_page_count=page_count,
            source_bytes=payload,
            extractor=extractor,
            observation_records=observation_records,
            session_anchor_json=_page_visual_v2_json_bytes(anchor).decode("utf-8"),
        )
    except BaseException:
        if document is not None:
            try:
                document.close()
            except BaseException:
                pass
        raise
    document.close()
    return authority


def _is_sha256(value: Any) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _is_exact_count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _status_pair_valid(status: Any, error_code: Any) -> bool:
    return bool(
        type(status) is str
        and status in {"ok", "error"}
        and type(error_code) is str
        and (
            (status == "ok" and error_code == "")
            or (
                status == "error"
                and bool(error_code)
                and error_code == error_code.strip()
            )
        )
    )


def _channel_digest_valid(status: str, digest: Any) -> bool:
    return _is_sha256(digest) if status == "ok" else digest == ""


def _rectangle_valid(value: Any) -> bool:
    return bool(
        type(value) is list
        and len(value) == 4
        and all(
            type(coordinate) in {int, float}
            and math.isfinite(float(coordinate))
            for coordinate in value
        )
    )


def _extractor_valid(value: Any) -> bool:
    return bool(
        type(value) is dict
        and set(value) == _EXTRACTOR_FIELDS
        and type(value.get("name")) is str
        and value.get("name")
        and value.get("name") == value.get("name").strip()
        and type(value.get("version")) is str
        and value.get("version")
        and value.get("version") == value.get("version").strip()
    )


def _raw_text_channel_valid(value: Any) -> bool:
    if type(value) is not dict or set(value) != _RAW_TEXT_FIELDS:
        return False
    status = value.get("status")
    counts = (
        value.get("total_block_count"),
        value.get("text_block_count"),
        value.get("image_block_count"),
    )
    return bool(
        _status_pair_valid(status, value.get("error_code"))
        and all(_is_exact_count(count) for count in counts)
        and counts[0] == counts[1] + counts[2]
        and _channel_digest_valid(status, value.get("dictionary_sha256"))
        and (status == "ok" or counts == (0, 0, 0))
    )


def _drawings_channel_valid(value: Any) -> bool:
    if type(value) is not dict or set(value) != _DRAWINGS_FIELDS:
        return False
    status = value.get("status")
    return bool(
        _status_pair_valid(status, value.get("error_code"))
        and _is_exact_count(value.get("count"))
        and _is_exact_count(value.get("item_count"))
        and _channel_digest_valid(status, value.get("drawings_sha256"))
        and (
            status == "ok"
            or (value.get("count") == 0 and value.get("item_count") == 0)
        )
    )


def _images_channel_valid(value: Any) -> bool:
    if type(value) is not dict or set(value) != _IMAGES_FIELDS:
        return False
    status = value.get("status")
    return bool(
        _status_pair_valid(status, value.get("error_code"))
        and _is_exact_count(value.get("displayed_count"))
        and _channel_digest_valid(status, value.get("image_info_sha256"))
        and (status == "ok" or value.get("displayed_count") == 0)
    )


def _display_list_channel_valid(value: Any) -> bool:
    if type(value) is not dict or set(value) != _DISPLAY_LIST_FIELDS:
        return False
    status = value.get("status")
    counts = value.get("type_counts")
    return bool(
        _status_pair_valid(status, value.get("error_code"))
        and type(value.get("layers_included")) is bool
        and _is_exact_count(value.get("entry_count"))
        and type(counts) is dict
        and set(counts) == set(_DISPLAY_TYPE_NAMES)
        and all(_is_exact_count(count) for count in counts.values())
        and sum(counts.values()) == value.get("entry_count")
        and _channel_digest_valid(status, value.get("entries_sha256"))
        and (status == "ok" or value.get("entry_count") == 0)
    )


def _content_stream_record_valid(value: Any, ordinal: int) -> bool:
    if type(value) is not dict or set(value) != _CONTENT_STREAM_FIELDS:
        return False
    if (
        type(value.get("ordinal")) is not int
        or value.get("ordinal") != ordinal
        or type(value.get("xref")) is not int
        or value.get("xref") <= 0
    ):
        return False
    for prefix in ("raw", "decoded"):
        status = value.get(f"{prefix}_status")
        length = value.get(f"{prefix}_length")
        digest = value.get(f"{prefix}_sha256")
        if not (
            _status_pair_valid(status, value.get(f"{prefix}_error_code"))
            and _is_exact_count(length)
            and _channel_digest_valid(status, digest)
            and (status == "ok" or length == 0)
        ):
            return False
    return True


def _content_streams_channel_valid(value: Any) -> bool:
    if type(value) is not dict or set(value) != _CONTENT_STREAMS_FIELDS:
        return False
    status = value.get("status")
    read_status = value.get("read_contents_status")
    streams = value.get("streams")
    if not (
        _status_pair_valid(status, value.get("error_code"))
        and _status_pair_valid(
            read_status,
            value.get("read_contents_error_code"),
        )
        and _is_exact_count(value.get("xref_count"))
        and type(streams) is list
        and len(streams) == value.get("xref_count")
        and all(
            _content_stream_record_valid(stream, ordinal)
            for ordinal, stream in enumerate(streams)
        )
        and _is_exact_count(value.get("read_contents_length"))
        and _channel_digest_valid(
            read_status,
            value.get("read_contents_sha256"),
        )
        and (
            read_status == "ok" or value.get("read_contents_length") == 0
        )
        and _is_sha256(value.get("sequence_sha256"))
        and value.get("sequence_sha256")
        == page_visual_content_stream_sequence_digest(streams)
    ):
        return False
    inner_complete = bool(
        read_status == "ok"
        and all(
            stream["raw_status"] == "ok"
            and stream["decoded_status"] == "ok"
            for stream in streams
        )
    )
    return (status == "ok") == inner_complete


def _annotations_channel_valid(value: Any) -> bool:
    if type(value) is not dict or set(value) != _ANNOTATION_FIELDS:
        return False
    status = value.get("status")
    counts = (
        value.get("annotation_count"),
        value.get("widget_count"),
        value.get("link_count"),
        value.get("annot_xref_count"),
        value.get("classified_xref_count"),
        value.get("unclassified_xref_count"),
    )
    return bool(
        _status_pair_valid(status, value.get("error_code"))
        and all(_is_exact_count(count) for count in counts)
        and counts[3] == counts[4] + counts[5]
        and counts[4] == counts[0] + counts[1] + counts[2]
        and _channel_digest_valid(status, value.get("objects_sha256"))
        and (status == "ok" or counts == (0, 0, 0, 0, 0, 0))
    )


def _page_visual_observation_v2_structure_verified(observation: Any) -> bool:
    try:
        if (
            type(observation) is not dict
            or set(observation) != PAGE_VISUAL_SOURCE_OBSERVATION_V2_FIELDS
        ):
            return False
        _strict_json_value(observation)
        geometry = observation.get("page_geometry")
        if not (
            observation.get("schema") == PAGE_VISUAL_SOURCE_OBSERVATION_V2_SCHEMA
            and type(observation.get("importer_identity")) is str
            and observation.get("importer_identity")
            and observation.get("importer_identity")
            == observation.get("importer_identity").strip()
            and _is_sha256(observation.get("pdf_sha256"))
            and type(observation.get("pdf_size_bytes")) is int
            and observation.get("pdf_size_bytes") > 0
            and type(observation.get("pdf_page_count")) is int
            and observation.get("pdf_page_count") > 0
            and type(observation.get("page_number")) is int
            and 0 < observation.get("page_number") <= observation.get("pdf_page_count")
            and type(observation.get("page_xref")) is int
            and observation.get("page_xref") > 0
            and type(observation.get("source_item_id")) is str
            and observation.get("source_item_id")
            and observation.get("source_item_id")
            == observation.get("source_item_id").strip()
            and _extractor_valid(observation.get("extractor"))
            and type(geometry) is dict
            and set(geometry) == _PAGE_GEOMETRY_FIELDS
            and all(
                _rectangle_valid(geometry.get(name))
                for name in ("mediabox", "cropbox", "rect")
            )
            and type(geometry.get("rotation")) is int
            and _raw_text_channel_valid(observation.get("raw_text"))
            and _drawings_channel_valid(observation.get("drawings"))
            and _images_channel_valid(observation.get("images"))
            and _display_list_channel_valid(observation.get("display_list"))
            and _content_streams_channel_valid(observation.get("content_streams"))
            and _annotations_channel_valid(observation.get("annotations"))
            and type(observation.get("evidence_complete")) is bool
            and type(observation.get("classification")) is str
            and _is_sha256(observation.get("observation_sha256"))
        ):
            return False
        complete, classification = _derive_page_visual_classification(observation)
        return bool(
            observation.get("evidence_complete") is complete
            and observation.get("classification") == classification
            and observation.get("observation_sha256")
            == page_visual_source_observation_v2_digest(observation)
        )
    except Exception:
        return False


def _page_visual_session_anchor_structure_verified(anchor: Any) -> bool:
    try:
        if (
            type(anchor) is not dict
            or set(anchor) != PAGE_VISUAL_SESSION_ANCHOR_FIELDS
        ):
            return False
        _strict_json_value(anchor)
        leaves = anchor.get("page_leaves")
        if not (
            anchor.get("schema") == PAGE_VISUAL_SESSION_ANCHOR_SCHEMA
            and type(anchor.get("importer_identity")) is str
            and anchor.get("importer_identity")
            and anchor.get("importer_identity")
            == anchor.get("importer_identity").strip()
            and _is_sha256(anchor.get("pdf_sha256"))
            and type(anchor.get("pdf_size_bytes")) is int
            and anchor.get("pdf_size_bytes") > 0
            and type(anchor.get("pdf_page_count")) is int
            and anchor.get("pdf_page_count") > 0
            and _extractor_valid(anchor.get("extractor"))
            and type(leaves) is list
            and bool(leaves)
            and _is_sha256(anchor.get("session_anchor_sha256"))
        ):
            return False
        pages = []
        scopes = []
        for leaf in leaves:
            if not (
                type(leaf) is dict
                and set(leaf) == PAGE_VISUAL_SESSION_LEAF_FIELDS
                and type(leaf.get("page_number")) is int
                and 0 < leaf.get("page_number") <= anchor.get("pdf_page_count")
                and type(leaf.get("page_xref")) is int
                and leaf.get("page_xref") > 0
                and type(leaf.get("source_item_id")) is str
                and leaf.get("source_item_id")
                and leaf.get("source_item_id") == leaf.get("source_item_id").strip()
                and _is_sha256(leaf.get("observation_sha256"))
            ):
                return False
            pages.append(leaf["page_number"])
            scopes.append(leaf["source_item_id"])
        return bool(
            pages == sorted(pages)
            and len(set(pages)) == len(pages)
            and len(set(scopes)) == len(scopes)
            and anchor.get("session_anchor_sha256")
            == page_visual_session_anchor_digest(anchor)
        )
    except Exception:
        return False


def page_visual_session_anchor_verified(
    anchor: Any,
    authority: Any,
) -> bool:
    try:
        return bool(
            type(authority) is PageVisualSourceAuthority
            and _page_visual_session_anchor_structure_verified(anchor)
            and _page_visual_v2_json_bytes(anchor).decode("utf-8")
            == authority._session_anchor_json
            and anchor.get("importer_identity") == authority.importer_identity
            and anchor.get("pdf_sha256") == authority.pdf_sha256
            and anchor.get("pdf_size_bytes") == authority.pdf_size_bytes
            and anchor.get("pdf_page_count") == authority.pdf_page_count
        )
    except Exception:
        return False


def page_visual_source_observation_v2_verified(
    observation: Any,
    authority: Any,
) -> bool:
    try:
        if not (
            type(authority) is PageVisualSourceAuthority
            and _page_visual_observation_v2_structure_verified(observation)
            and observation.get("importer_identity") == authority.importer_identity
            and observation.get("pdf_sha256") == authority.pdf_sha256
            and observation.get("pdf_size_bytes") == authority.pdf_size_bytes
            and observation.get("pdf_page_count") == authority.pdf_page_count
        ):
            return False
        stored = authority._lookup_json(
            observation["page_number"],
            observation["source_item_id"],
        )
        if _page_visual_v2_json_bytes(observation).decode("utf-8") != stored:
            return False
        anchor = authority.session_anchor()
        return bool(
            page_visual_session_anchor_verified(anchor, authority)
            and any(
                leaf["page_number"] == observation["page_number"]
                and leaf["page_xref"] == observation["page_xref"]
                and leaf["source_item_id"] == observation["source_item_id"]
                and leaf["observation_sha256"]
                == observation["observation_sha256"]
                for leaf in anchor["page_leaves"]
            )
        )
    except Exception:
        return False


def _exact_page_visual_representation(value: Any, field: str) -> str:
    representation = _exact_text(value, field)
    if representation not in _PAGE_VISUAL_NONTERMINAL_REPRESENTATIONS:
        raise ValueError(f"{field} must be a canonical nonterminal representation")
    return representation


def page_visual_representation_status(
    observation: Any,
    attempted_type: str,
) -> str:
    if not _page_visual_observation_v2_structure_verified(observation):
        return "indeterminate"
    attempted = (
        attempted_type
        if type(attempted_type) is str
        and attempted_type in _PAGE_VISUAL_REPRESENTATIONS
        else ""
    )
    if attempted == "raster":
        return "terminal"
    text_present, path_present, shade_present, _image_present = (
        _page_visual_positive_evidence(observation)
    )
    if attempted in {"text", "labels", "glyphs", "3d_text"}:
        if text_present:
            return "reachable"
        return (
            "proven_impossible"
            if _page_visual_text_absence_complete(observation)
            else "indeterminate"
        )
    if attempted == "geometry":
        if text_present or path_present or shade_present:
            return "reachable"
        return (
            "proven_impossible"
            if _page_visual_geometry_absence_complete(observation)
            else "indeterminate"
        )
    return "indeterminate"


def page_visual_schema_trust(value: Any) -> str:
    if type(value) is not dict or type(value.get("schema")) is not str:
        return "unknown"
    schema = value["schema"]
    if schema in {
        PAGE_VISUAL_SOURCE_OBSERVATION_SCHEMA,
        PAGE_VISUAL_FALLBACK_PROOF_SCHEMA,
    }:
        return "legacy_untrusted"
    if schema in {
        PAGE_VISUAL_SOURCE_OBSERVATION_V2_SCHEMA,
        PAGE_VISUAL_SESSION_ANCHOR_SCHEMA,
        PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA,
    }:
        return "source_authority_required"
    return "unknown"


def _page_visual_v2_reason_code(attempted_type: str) -> str:
    attempted = _exact_page_visual_representation(
        attempted_type,
        "attempted_type",
    )
    if attempted == "geometry":
        return "no_source_vector_paint"
    if attempted == "glyphs":
        return "no_source_text_or_glyph_paint"
    return "no_source_text_paint"


def _exact_entity_id_list(value: Any, field: str) -> Any:
    if type(value) is not list:
        raise ValueError(f"{field} must be a plain list")
    checked = []
    for entity_id in value:
        checked.append(_exact_text(entity_id, field))
    if len(set(checked)) != len(checked):
        raise ValueError(f"{field} must contain unique identifiers")
    return checked


def build_page_visual_fallback_proof_v2(
    *,
    observation: Dict[str, Any],
    authority: PageVisualSourceAuthority,
    requested_type: str,
    attempted_type: str,
    created_entity_ids: Any = None,
    removed_entity_ids: Any = None,
) -> Dict[str, Any]:
    """Issue a v2 proof only from the fresh exact-PDF authority capability."""

    requested = _exact_page_visual_representation(
        requested_type,
        "requested_type",
    )
    attempted = _exact_page_visual_representation(
        attempted_type,
        "attempted_type",
    )
    if not page_visual_source_observation_v2_verified(observation, authority):
        raise ValueError("page visual observation is not bound to source authority")
    status = page_visual_representation_status(observation, attempted)
    if status != "proven_impossible":
        raise ValueError(
            f"attempted representation is {status}; impossibility proof unavailable"
        )
    created = _exact_entity_id_list(
        [] if created_entity_ids is None else created_entity_ids,
        "created_entity_ids",
    )
    removed = _exact_entity_id_list(
        [] if removed_entity_ids is None else removed_entity_ids,
        "removed_entity_ids",
    )
    if created or removed:
        raise ValueError("page visual source proof must be pre-mutation")
    anchor = authority.session_anchor()
    if not page_visual_session_anchor_verified(anchor, authority):
        raise ValueError("page visual session anchor is invalid")
    proof = {
        "schema": PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA,
        "page_specific_proven_impossible": True,
        "importer_identity": observation["importer_identity"],
        "pdf_sha256": observation["pdf_sha256"],
        "page_number": observation["page_number"],
        "source_item_id": observation["source_item_id"],
        "requested_type": requested,
        "attempted_type": attempted,
        "reason_code": _page_visual_v2_reason_code(attempted),
        "observation_sha256": observation["observation_sha256"],
        "session_anchor_sha256": anchor["session_anchor_sha256"],
        "created_entity_ids": created,
        "removed_entity_ids": removed,
        "cleanup_complete": True,
    }
    proof["proof_sha256"] = page_visual_fallback_proof_v2_digest(proof)
    if not page_visual_fallback_proof_v2_verified(
        proof,
        observation=observation,
        authority=authority,
        expected_requested_type=requested,
        expected_attempted_type=attempted,
    ):
        raise ValueError("v2 page visual fallback proof could not be sealed")
    return proof


def page_visual_fallback_proof_v2_verified(
    proof: Any,
    *,
    observation: Any,
    authority: Any,
    expected_requested_type: str,
    expected_attempted_type: str,
) -> bool:
    """Verify a v2 proof against an authority not obtainable from report JSON."""

    try:
        requested = _exact_page_visual_representation(
            expected_requested_type,
            "expected_requested_type",
        )
        attempted = _exact_page_visual_representation(
            expected_attempted_type,
            "expected_attempted_type",
        )
        if (
            type(proof) is not dict
            or set(proof) != PAGE_VISUAL_FALLBACK_PROOF_V2_FIELDS
        ):
            return False
        _strict_json_value(proof)
        created = proof.get("created_entity_ids")
        removed = proof.get("removed_entity_ids")
        if not (
            page_visual_source_observation_v2_verified(observation, authority)
            and proof.get("schema") == PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA
            and proof.get("page_specific_proven_impossible") is True
            and proof.get("importer_identity") == observation.get("importer_identity")
            and proof.get("pdf_sha256") == observation.get("pdf_sha256")
            and type(proof.get("page_number")) is int
            and proof.get("page_number") == observation.get("page_number")
            and proof.get("source_item_id") == observation.get("source_item_id")
            and proof.get("requested_type") == requested
            and proof.get("attempted_type") == attempted
            and proof.get("reason_code") == _page_visual_v2_reason_code(attempted)
            and proof.get("observation_sha256")
            == observation.get("observation_sha256")
            and proof.get("session_anchor_sha256")
            == authority.session_anchor().get("session_anchor_sha256")
            and proof.get("cleanup_complete") is True
            and created == []
            and removed == []
            and _is_sha256(proof.get("proof_sha256"))
            and proof.get("proof_sha256")
            == page_visual_fallback_proof_v2_digest(proof)
            and page_visual_representation_status(observation, attempted)
            == "proven_impossible"
        ):
            return False
        return page_visual_session_anchor_verified(
            authority.session_anchor(),
            authority,
        )
    except Exception:
        return False


__all__ = [
    "PAGE_VISUAL_FALLBACK_PROOF_SCHEMA",
    "PAGE_VISUAL_SOURCE_OBSERVATION_SCHEMA",
    "PAGE_VISUAL_REASON_CODE",
    "PAGE_VISUAL_PROOF_FIELDS",
    "PAGE_VISUAL_EVIDENCE_FIELDS",
    "PAGE_VISUAL_RESULT_FIELDS",
    "PAGE_VISUAL_SOURCE_OBSERVATION_FIELDS",
    "raw_text_dictionary_digest",
    "raw_text_dictionary_has_text_structure",
    "page_visual_proof_digest",
    "page_visual_source_observation_digest",
    "build_page_visual_source_observation",
    "page_visual_source_observation_verified",
    "build_page_visual_fallback_proof",
    "page_visual_fallback_proof_verified",
    "PAGE_VISUAL_SOURCE_OBSERVATION_V2_SCHEMA",
    "PAGE_VISUAL_SESSION_ANCHOR_SCHEMA",
    "PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA",
    "PAGE_VISUAL_SOURCE_OBSERVATION_V2_FIELDS",
    "PAGE_VISUAL_SESSION_ANCHOR_FIELDS",
    "PAGE_VISUAL_SESSION_LEAF_FIELDS",
    "PAGE_VISUAL_FALLBACK_PROOF_V2_FIELDS",
    "PageVisualSourceAuthority",
    "capture_fresh_page_visual_authority",
    "page_visual_source_observation_v2_digest",
    "page_visual_session_anchor_digest",
    "page_visual_fallback_proof_v2_digest",
    "page_visual_content_stream_sequence_digest",
    "page_visual_source_observation_v2_verified",
    "page_visual_session_anchor_verified",
    "page_visual_representation_status",
    "page_visual_schema_trust",
    "build_page_visual_fallback_proof_v2",
    "page_visual_fallback_proof_v2_verified",
]
