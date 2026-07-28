# -*- coding: utf-8 -*-
"""Shared import_report.json schema for BlueCollar PDF importers."""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional

from .item_impossibility import (
    EVIDENCE_KEY as ITEM_IMPOSSIBILITY_EVIDENCE_KEY,
    item_representation_impossibility_proof_verified,
)
from .page_visual import (
    page_visual_fallback_proof_v2_verified,
    page_visual_source_observation_v2_verified,
)
from .preflight_copy import SCALE_CROSSCHECK_BANNER
from .text_delivery_report import resolve_text_representation_delivery

SCHEMA = "bcs.import_report/1.1"
SCALE_TRUST_CONFIDENCE = 0.70
SCALE_DIMENSION_TENSION_CONFIDENCE = 0.85
SCALE_FACTOR_DISAGREE_RATIO = 0.15
PERFORMANCE_HINT_ENTITY_THRESHOLD = 50_000
PERFORMANCE_HINT_PEAK_MB = 1024.0


def _require_finite_json_numbers(value: Any, path: str = "") -> None:
    """Reject NaN and infinities with the exact report path that contains them."""

    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                f"non-finite JSON number at {path or '<root>'}: {value!r}"
            )
        return
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else str(key)
            _require_finite_json_numbers(child, child_path)
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            _require_finite_json_numbers(child, child_path)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _unique_strings(values: List[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def build_text_mode_fallback(
    *,
    requested: str,
    delivered: str,
    reason: str,
    count: int = 0,
) -> Optional[Dict[str, Any]]:
    """Normalize a text-mode substitution record for the fallback block.

    TEXTMODE-1 (owner directive 2026-07-13): mode substitution is never
    silent — the report carries requested mode, delivered mode, and reason.
    Returns ``None`` when there is no real substitution (missing modes or
    requested == delivered) so callers can pass the result straight to
    ``build_import_report(text_fallback=...)``.
    """
    req = str(requested or "").strip().lower()
    dlv = str(delivered or "").strip().lower()
    why = str(reason or "").strip()
    if not req or not dlv or req == dlv:
        return None
    try:
        n = int(count or 0)
    except (TypeError, ValueError):
        n = 0
    return {
        "requested": req,
        "delivered": dlv,
        "reason": why or "unspecified",
        "count": max(0, n),
    }


def build_fidelity_diagnostics(
    *,
    primitive_count: int = 0,
    text_count: int = 0,
    image_count: int = 0,
    layer_count: int = 0,
    warnings: int = 0,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
    import_text: Optional[bool] = None,
    text_mode: Optional[str] = None,
    text_source_spans: Optional[int] = None,
    text_glyph_estimate: Optional[int] = None,
    text_fallback: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return portable, user-facing fidelity signals for support and UI."""

    primitives = int(primitive_count or 0)
    text_entities = int(text_count or 0)
    images = int(image_count or 0)
    layers = int(layer_count or 0)
    warning_count = int(warnings or 0)
    source_spans = int(text_source_spans or 0)
    glyph_estimate = int(text_glyph_estimate or 0)
    mode = str(text_mode or "").strip()
    if primitives >= 50:
        quality_level = "high"
        signals = ["good_vector_content"]
    elif primitives >= 10:
        quality_level = "moderate"
        signals = ["limited_vector_content"]
    elif primitives > 0:
        quality_level = "low"
        signals = ["very_limited_vector_content"]
    elif images > 0:
        quality_level = "raster"
        signals = ["raster_or_image_content_delivered"]
    else:
        quality_level = "empty"
        signals = ["no_vector_geometry_created"]

    actions: List[str] = []
    if fallback_used:
        signals.append("fallback_used")
        actions.append(
            "Inspect the item-specific proof and correct the importer while keeping "
            "the requested representation unchanged."
        )

    if text_fallback:
        signals.append("text_mode_fallback")
        requested_mode = str(text_fallback.get("requested") or "").strip()
        delivered_mode = str(text_fallback.get("delivered") or "").strip()
        if requested_mode and delivered_mode:
            actions.append(
                f"Requested text mode '{requested_mode}' was delivered as '{delivered_mode}' — "
                "see fallback.text in this report for the reason."
            )

    if warning_count:
        signals.append("warnings_present")
        actions.append("Review the warning count and last import log before trusting the drawing for production use.")

    if layers == 0:
        signals.append("no_pdf_layers_detected")
    elif layers > 0:
        signals.append("pdf_layers_preserved")

    if import_text is False:
        signals.append("text_import_disabled")
    elif mode:
        signals.append(f"text_mode_{mode}")
        actions.append(
            "Confirm each source item reports the requested text representation "
            "with verified content, placement, rotation, width, and height."
        )

    if import_text and source_spans > 0 and text_entities == 0:
        signals.append("source_text_seen_but_no_text_entities_created")
        actions.append(
            "Treat missing delivered text entities as a failed import; inspect the "
            "item attempt history without changing the requested representation."
        )

    if glyph_estimate >= 1000:
        signals.append("dense_text_glyph_workload")
        actions.append(
            "For heavy PDFs on older PCs, diagnose one page first while keeping "
            "the requested representation unchanged."
        )

    return {
        "quality_level": quality_level,
        "signals": _unique_strings(signals),
        "recommended_actions": _unique_strings(actions),
    }


_HOST_LABELS = {
    "freecad": "FreeCAD",
    "librecad": "LibreCAD",
    "blender": "Blender",
    "sketchup": "SketchUp",
}


def _basename(path: str) -> str:
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return name or "the PDF"


def build_scale_crosscheck(extra: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Non-blocking scale warning when detection is weak or sources disagree."""

    scale = extra.get("resolved_scale") or {}
    if not isinstance(scale, dict):
        scale = {}

    hints = extra.get("scale_hints") or {}
    if not isinstance(hints, dict):
        hints = {}

    title_block = bool(hints.get("title_block_detected"))
    dimension_count = int(hints.get("dimension_count") or 0)
    alternate_factors = hints.get("alternate_scale_factors") or []
    if not isinstance(alternate_factors, list):
        alternate_factors = []

    warnings: List[str] = []
    reasons: List[str] = []

    conf: Optional[float] = None
    raw_conf = scale.get("confidence")
    if raw_conf is not None:
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            conf = None

    factor = scale.get("factor")
    fallback = str(scale.get("fallback_reason") or "").strip()
    source = str(scale.get("source") or "").strip()

    if fallback == "no_scale_detected" or not factor:
        warnings.append(
            "No drawing scale was detected in the title block or page text — verify manually before takeoff."
        )
        reasons.append("no_scale_detected")
    elif conf is not None and conf < SCALE_TRUST_CONFIDENCE:
        warnings.append(
            f"Scale detection confidence is low ({conf * 100:.0f}%) — verify with manual scale tools before takeoff."
        )
        reasons.append("low_confidence")

    if title_block and source and source != "titleblock" and factor:
        warnings.append(
            "A title block was detected but scale came from other page text — compare the title-block notation."
        )
        reasons.append("titleblock_source_mismatch")

    if (
        title_block
        and dimension_count >= 3
        and conf is not None
        and conf < SCALE_DIMENSION_TENSION_CONFIDENCE
        and factor
    ):
        warnings.append(
            f"Title-block scale may disagree with {dimension_count} detected dimension strings — spot-check one known dimension."
        )
        reasons.append("titleblock_dimension_tension")

    try:
        primary = float(factor) if factor else None
    except (TypeError, ValueError):
        primary = None

    if primary and primary > 0 and alternate_factors:
        for alt in alternate_factors:
            try:
                alt_factor = float(alt)
            except (TypeError, ValueError):
                continue
            if alt_factor <= 0:
                continue
            if abs(alt_factor - primary) / max(primary, alt_factor) > SCALE_FACTOR_DISAGREE_RATIO:
                warnings.append(
                    "Multiple scale notations on the sheet disagree — confirm which scale applies to this view."
                )
                reasons.append("conflicting_scale_notations")
                break

    if not warnings:
        return None

    return {
        "level": "warn",
        "reasons": _unique_strings(reasons),
        "messages": _unique_strings(warnings),
        "banner": SCALE_CROSSCHECK_BANNER,
        "user_message": SCALE_CROSSCHECK_BANNER,
    }


def build_font_embedding_hints(doc: Any) -> Dict[str, Any]:
    """Detect non-embedded PDF fonts that may substitute on the host OS."""

    if doc is None:
        return {}
    non_embedded: List[str] = []
    try:
        page_count = len(doc)
    except TypeError:
        return {}
    for page_index in range(page_count):
        try:
            page = doc[page_index]
            fonts = page.get_fonts(full=True)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
        for entry in fonts or []:
            if len(entry) < 6:
                continue
            extension = str(entry[1] or "").strip().lower()
            # PyMuPDF get_fonts(full=True) returns:
            # xref, extension, type, basefont, resource name, encoding, referencer.
            # extension == "n/a" is the practical signal for base/non-embedded fonts.
            embedded = bool(extension and extension != "n/a")
            if embedded:
                continue
            name = str(entry[3] or entry[4] or "unknown").strip()
            if name and name not in non_embedded:
                non_embedded.append(name)
    if not non_embedded:
        return {}
    sample = ", ".join(non_embedded[:5])
    if len(non_embedded) > 5:
        sample += f" (+{len(non_embedded) - 5} more)"
    note = (
        f"Non-embedded PDF fonts detected ({sample}); preserve the requested "
        "representation, report any parent-native font substitution as "
        "source-font non-equivalent, and use an item fallback only after proof."
    )
    return {
        "non_embedded_fonts": non_embedded,
        "font_substitution_note": note,
    }


def build_pdf_interactive_note(doc: Any) -> Dict[str, Any]:
    """Warn when PDF contains JavaScript or open actions (never executed by importers)."""

    if doc is None:
        return {}

    def key_present(xref: int, key: str) -> bool:
        try:
            value = doc.xref_get_key(xref, key)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return False
        if not value:
            return False
        if isinstance(value, tuple):
            kind = str(value[0] or "").strip().lower()
            raw = str(value[1] or "").strip().lower()
            return kind not in {"", "null"} and raw not in {"", "null"}
        return str(value).strip().lower() not in {"", "null"}

    flags: List[str] = []
    try:
        js = doc.get_js() if hasattr(doc, "get_js") else None
        if js:
            flags.append("JavaScript")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    try:
        catalog = doc.pdf_catalog()
        if catalog and key_present(catalog, "OpenAction"):
            flags.append("OpenAction")
        if catalog and key_present(catalog, "AA"):
            flags.append("AdditionalActions")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    try:
        for xref in range(1, int(doc.xref_length())):
            if key_present(xref, "JS") or key_present(xref, "JavaScript"):
                flags.append("JavaScript")
                break
            try:
                subtype = doc.xref_get_key(xref, "S")
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            raw = " ".join(str(part) for part in subtype) if isinstance(subtype, tuple) else str(subtype)
            if "JavaScript" in raw:
                flags.append("JavaScript")
                break
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    if not flags:
        return {}
    flags = _unique_strings(flags)
    joined = ", ".join(flags)
    return {
        "pdf_interactive_flags": flags,
        "pdf_interactive_note": (
            f"PDF contains document scripts or actions ({joined}) — import uses static "
            "geometry only; scripts are not executed."
        ),
    }


def build_report_meta(
    *,
    host_app: str,
    importer_version: str,
    report_sha256: str = "",
    imported_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Return report_meta block with build_stamp for support and T-01 checks."""

    host = str(host_app or "").strip().lower()
    semver = str(importer_version or "").strip()
    sha = str(report_sha256 or "").strip().lower()
    stamp_parts = [part for part in (host, semver) if part]
    if sha:
        stamp_parts.append(f"report {sha[:12]}")
    return {
        "build_stamp": " · ".join(stamp_parts),
        "host": host,
        "semver": semver,
        "report_sha256": sha,
        "imported_at": imported_at or datetime.now(timezone.utc).isoformat(),
    }


def build_performance_hint(
    *,
    primitive_count: int = 0,
    text_count: int = 0,
    peak_mb: float = 0.0,
) -> Optional[str]:
    """Plain-English hint for weak PCs on large imports."""

    entities = int(primitive_count or 0) + int(text_count or 0)
    peak = float(peak_mb or 0.0)
    if entities >= PERFORMANCE_HINT_ENTITY_THRESHOLD or peak >= PERFORMANCE_HINT_PEAK_MB:
        return (
            "Large PDF — on PCs with less than 8 GB RAM, import one page at a time "
            "using the Pages field."
        )
    return None


def _pdf_audit_extras(pdf_path: str) -> Dict[str, Any]:
    """Open PDF briefly for font/interactive audits (best-effort)."""

    path = str(pdf_path or "").strip()
    if not path or not Path(path).is_file():
        return {}
    try:
        from .fitz_loader import safe_open
    except ImportError:
        return {}
    doc = None
    merged: Dict[str, Any] = {}
    try:
        doc = safe_open(path)
        merged.update(build_font_embedding_hints(doc))
        merged.update(build_pdf_interactive_note(doc))
    except (OSError, RuntimeError, TypeError, ValueError):
        return merged
    finally:
        if doc is not None:
            try:
                doc.close()
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
    return merged


def build_model_3d_extra(
    host_app: str,
    *,
    enabled: bool = False,
    stats: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Honest model_3d block for import_report.extra (R8-1)."""

    if stats:
        return dict(stats)
    host = str(host_app or "").lower()
    if host == "librecad":
        return {
            "supported": False,
            "enabled": False,
            "reason": "2D host — PDF import produces planar DXF only",
        }
    return {
        "supported": host in ("freecad", "blender", "sketchup"),
        "enabled": bool(enabled),
    }


_FREECAD_SHAPE_FINGERPRINT_SCHEMA = "bcs.freecad_shape_fingerprint/1.1"
_FREECAD_SHAPE_FINGERPRINT_QUANTUM = "1e-07"
_FREECAD_SHAPE_FINGERPRINT_TOLERANCE = 1e-7
_FREECAD_RAW_SHAPE_GEOMETRY_SCHEMA = "bcs.freecad_raw_shape_geometry/1.0"
_FREECAD_SHAPE_GEOMETRY_SCHEMA = "bcs.freecad_shape_geometry_digest/1.0"
_FREECAD_SHAPE_COMPARISON_SCHEMA = "bcs.freecad_shape_persistence_comparison/1.1"
_FREECAD_FCSTD_ARCHIVE_SCHEMA = "bcs.freecad_fcstd_shape_archive/1.1"
_FREECAD_FCSTD_ARCHIVE_METHOD = "fcstd_brep_archive_sha256"
_FREECAD_UNORDERED_FALLBACK_PAIR_LIMIT = 4_000_000


def _freecad_finite_number(value: Any) -> Optional[float]:
    """Read a report number without accepting bools, NaN, or infinities."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _freecad_point(value: Any) -> Optional[List[float]]:
    if not isinstance(value, list) or len(value) != 3:
        return None
    point = [_freecad_finite_number(coordinate) for coordinate in value]
    if any(coordinate is None for coordinate in point):
        return None
    return [float(coordinate) for coordinate in point if coordinate is not None]


def _freecad_points_close(left: Any, right: Any) -> bool:
    left_point = _freecad_point(left)
    right_point = _freecad_point(right)
    return bool(
        left_point is not None
        and right_point is not None
        and all(
            abs(left_coordinate - right_coordinate)
            <= _FREECAD_SHAPE_FINGERPRINT_TOLERANCE
            for left_coordinate, right_coordinate in zip(
                left_point, right_point, strict=True
            )
        )
    )


def _freecad_unordered_alignment(left: Any, right: Any, predicate):
    """Return a deterministic, stack-safe one-to-one alignment or None.

    The aligned fast path is linear for the normal already-sorted inventory.
    A failed greedy choice falls back to iterative layered maximum matching,
    with every predicate pair evaluated at most once and a fixed pair budget.
    """

    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return None
    item_count = len(left)
    if item_count == 0:
        return {}

    class _PairBudgetExceededError(RuntimeError):
        pass

    compatibility_cache: Dict[tuple[int, int], bool] = {}
    pair_budget = max(item_count, _FREECAD_UNORDERED_FALLBACK_PAIR_LIMIT)

    def compatible(left_index: int, right_index: int) -> bool:
        key = (left_index, right_index)
        if key not in compatibility_cache:
            if len(compatibility_cache) >= pair_budget:
                raise _PairBudgetExceededError
            compatibility_cache[key] = bool(
                predicate(left[left_index], right[right_index])
            )
        return compatibility_cache[key]

    available_right = bytearray([1]) * item_count
    fast_alignment: Dict[int, int] = {}
    try:
        for left_index in range(item_count):
            for offset in range(item_count):
                right_index = (left_index + offset) % item_count
                if available_right[right_index] and compatible(
                    left_index, right_index
                ):
                    fast_alignment[left_index] = right_index
                    available_right[right_index] = 0
                    break
            else:
                break
    except _PairBudgetExceededError:
        return None
    if len(fast_alignment) == item_count:
        return fast_alignment

    if item_count > _FREECAD_UNORDERED_FALLBACK_PAIR_LIMIT // item_count:
        return None
    adjacency = []
    try:
        for left_index in range(item_count):
            candidates = [
                (left_index + offset) % item_count
                for offset in range(item_count)
                if compatible(left_index, (left_index + offset) % item_count)
            ]
            adjacency.append(candidates)
    except _PairBudgetExceededError:
        return None
    if any(not candidates for candidates in adjacency):
        return None

    unmatched = -1
    matched_right_by_left = [unmatched] * item_count
    matched_left_by_right = [unmatched] * item_count
    matched_count = 0
    infinity = item_count + 1

    while matched_count < item_count:
        distances = [infinity] * item_count
        queue = deque()
        for left_index in range(item_count):
            if matched_right_by_left[left_index] == unmatched:
                distances[left_index] = 0
                queue.append(left_index)

        shortest_path = infinity
        while queue:
            left_index = queue.popleft()
            if distances[left_index] >= shortest_path:
                continue
            for right_index in adjacency[left_index]:
                paired_left = matched_left_by_right[right_index]
                if paired_left == unmatched:
                    shortest_path = distances[left_index] + 1
                elif distances[paired_left] == infinity:
                    distances[paired_left] = distances[left_index] + 1
                    queue.append(paired_left)
        if shortest_path == infinity:
            break

        next_candidate = [0] * item_count
        phase_matches = 0
        for root_left in range(item_count):
            if (
                matched_right_by_left[root_left] != unmatched
                or distances[root_left] != 0
            ):
                continue
            left_path = [root_left]
            right_path: List[int] = []
            augmented = False
            while left_path:
                left_index = left_path[-1]
                advanced = False
                while next_candidate[left_index] < len(adjacency[left_index]):
                    right_index = adjacency[left_index][next_candidate[left_index]]
                    next_candidate[left_index] += 1
                    paired_left = matched_left_by_right[right_index]
                    if paired_left == unmatched:
                        if distances[left_index] + 1 != shortest_path:
                            continue
                        complete_right_path = right_path + [right_index]
                        for path_left, path_right in zip(
                            left_path,
                            complete_right_path,
                            strict=True,
                        ):
                            matched_right_by_left[path_left] = path_right
                            matched_left_by_right[path_right] = path_left
                        augmented = True
                        break
                    if distances[paired_left] == distances[left_index] + 1:
                        left_path.append(paired_left)
                        right_path.append(right_index)
                        advanced = True
                        break
                if augmented:
                    phase_matches += 1
                    break
                if advanced:
                    continue
                distances[left_index] = infinity
                left_path.pop()
                if right_path and len(right_path) >= len(left_path):
                    right_path.pop()
        if phase_matches == 0:
            break
        matched_count += phase_matches

    if matched_count != item_count:
        return None
    return dict(enumerate(matched_right_by_left))


def _freecad_unordered_match(left: Any, right: Any, predicate) -> bool:
    """Return whether two lists have a one-to-one predicate match."""

    return _freecad_unordered_alignment(left, right, predicate) is not None


def _freecad_path_close(
    left: Any,
    right: Any,
    closed: bool,
    closure_authoritative: bool = False,
) -> bool:
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return False
    if not left:
        return False

    def linear_match(first: List[Any], second: List[Any]) -> bool:
        return all(
            _freecad_points_close(a, b)
            for a, b in zip(first, second, strict=True)
        )

    if not closed:
        matched = linear_match(left, right) or linear_match(
            left, list(reversed(right))
        )
        if not matched or not closure_authoritative or len(left) <= 2:
            return matched
        for oriented in (right, list(reversed(right))):
            for offset in range(1, len(oriented)):
                if linear_match(left, oriented[offset:] + oriented[:offset]):
                    return False
        return True

    left_cycle = list(left)
    right_cycle = list(right)
    if len(left_cycle) > 1 and _freecad_points_close(left_cycle[0], left_cycle[-1]):
        left_cycle.pop()
    if len(right_cycle) > 1 and _freecad_points_close(right_cycle[0], right_cycle[-1]):
        right_cycle.pop()
    if not left_cycle or len(left_cycle) != len(right_cycle):
        return False
    for oriented in (right_cycle, list(reversed(right_cycle))):
        for offset in range(len(oriented)):
            rotated = oriented[offset:] + oriented[:offset]
            if linear_match(left_cycle, rotated):
                return True
    return False


def _freecad_edge_close(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if (
        left.get("curve_type") != right.get("curve_type")
        or type(left.get("closed")) is not bool
        or type(right.get("closed")) is not bool
        or left.get("closed") != right.get("closed")
        or type(left.get("closure_authoritative")) is not bool
        or type(right.get("closure_authoritative")) is not bool
        or left.get("closure_authoritative")
        != right.get("closure_authoritative")
    ):
        return False
    left_length = _freecad_finite_number(left.get("length"))
    right_length = _freecad_finite_number(right.get("length"))
    if left_length is None or right_length is None:
        return False
    if abs(left_length - right_length) > _FREECAD_SHAPE_FINGERPRINT_TOLERANCE:
        return False
    return _freecad_path_close(
        left.get("points"),
        right.get("points"),
        bool(left.get("closed")),
        bool(left.get("closure_authoritative")),
    )


def _freecad_point_delta(left: Any, right: Any) -> Optional[float]:
    left_point = _freecad_point(left)
    right_point = _freecad_point(right)
    if left_point is None or right_point is None:
        return None
    return max(
        abs(left_coordinate - right_coordinate)
        for left_coordinate, right_coordinate in zip(
            left_point, right_point, strict=True
        )
    )


def _freecad_path_delta(
    left: Any,
    right: Any,
    closed: bool,
    closure_authoritative: bool = False,
) -> Optional[float]:
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return None
    if not _freecad_path_close(
        left,
        right,
        closed,
        closure_authoritative,
    ):
        return None

    def linear_delta(first: List[Any], second: List[Any]) -> Optional[float]:
        deltas = [
            _freecad_point_delta(a, b)
            for a, b in zip(first, second, strict=True)
        ]
        if not deltas or any(delta is None for delta in deltas):
            return None
        return max(float(delta) for delta in deltas if delta is not None)

    candidates: List[float] = []
    if not closed:
        for oriented in (right, list(reversed(right))):
            delta = linear_delta(left, oriented)
            if delta is not None and delta <= _FREECAD_SHAPE_FINGERPRINT_TOLERANCE:
                candidates.append(delta)
        return min(candidates) if candidates else None

    left_cycle = list(left)
    right_cycle = list(right)
    if len(left_cycle) > 1 and _freecad_points_close(left_cycle[0], left_cycle[-1]):
        left_cycle.pop()
    if len(right_cycle) > 1 and _freecad_points_close(right_cycle[0], right_cycle[-1]):
        right_cycle.pop()
    if not left_cycle or len(left_cycle) != len(right_cycle):
        return None
    for oriented in (right_cycle, list(reversed(right_cycle))):
        for offset in range(len(oriented)):
            delta = linear_delta(left_cycle, oriented[offset:] + oriented[:offset])
            if delta is not None and delta <= _FREECAD_SHAPE_FINGERPRINT_TOLERANCE:
                candidates.append(delta)
    return min(candidates) if candidates else None


def _freecad_edge_delta(left: Any, right: Any) -> Optional[float]:
    if not _freecad_edge_close(left, right):
        return None
    left_length = _freecad_finite_number(left.get("length"))
    right_length = _freecad_finite_number(right.get("length"))
    path_delta = _freecad_path_delta(
        left.get("points"),
        right.get("points"),
        bool(left.get("closed")),
        bool(left.get("closure_authoritative")),
    )
    if left_length is None or right_length is None or path_delta is None:
        return None
    return max(abs(left_length - right_length), path_delta)


def _freecad_raw_shape_geometry_valid(geometry: Any) -> bool:
    if not isinstance(geometry, dict):
        return False
    vertices = geometry.get("vertices")
    edges = geometry.get("edges")
    if (
        geometry.get("schema") != _FREECAD_RAW_SHAPE_GEOMETRY_SCHEMA
        or geometry.get("tolerance") != _FREECAD_SHAPE_FINGERPRINT_QUANTUM
        or not isinstance(vertices, list)
        or not isinstance(edges, list)
        or any(_freecad_point(vertex) is None for vertex in vertices)
    ):
        return False
    return all(_freecad_edge_close(edge, edge) for edge in edges)


def _freecad_shape_geometry_equivalent(left: Any, right: Any) -> bool:
    """Compare canonical samples with a continuous absolute tolerance."""

    if not _freecad_raw_shape_geometry_valid(
        left
    ) or not _freecad_raw_shape_geometry_valid(right):
        return False
    return bool(
        _freecad_unordered_match(
            left.get("vertices"), right.get("vertices"), _freecad_points_close
        )
        and _freecad_unordered_match(
            left.get("edges"), right.get("edges"), _freecad_edge_close
        )
    )


def _freecad_shape_geometry_comparison(left: Any, right: Any) -> Dict[str, Any]:
    """Return live one-to-one comparison evidence for two raw sample sets."""

    if not _freecad_raw_shape_geometry_valid(
        left
    ) or not _freecad_raw_shape_geometry_valid(right):
        return {"verified": False, "max_delta": None}
    left_vertices = left.get("vertices")
    right_vertices = right.get("vertices")
    left_edges = left.get("edges")
    right_edges = right.get("edges")
    vertex_alignment = _freecad_unordered_alignment(
        left_vertices, right_vertices, _freecad_points_close
    )
    edge_alignment = _freecad_unordered_alignment(
        left_edges, right_edges, _freecad_edge_close
    )
    if vertex_alignment is None or edge_alignment is None:
        return {"verified": False, "max_delta": None}
    deltas: List[float] = []
    for left_index, right_index in vertex_alignment.items():
        delta = _freecad_point_delta(
            left_vertices[left_index], right_vertices[right_index]
        )
        if delta is None:
            return {"verified": False, "max_delta": None}
        deltas.append(delta)
    for left_index, right_index in edge_alignment.items():
        delta = _freecad_edge_delta(left_edges[left_index], right_edges[right_index])
        if delta is None:
            return {"verified": False, "max_delta": None}
        deltas.append(delta)
    max_delta = max(deltas) if deltas else 0.0
    return {
        "verified": max_delta <= _FREECAD_SHAPE_FINGERPRINT_TOLERANCE,
        "max_delta": max_delta,
    }


def _freecad_shape_geometry_digest_valid(geometry: Any) -> bool:
    """Validate the compact public binding to transient raw shape samples."""

    if not isinstance(geometry, dict):
        return False
    digest = geometry.get("sample_digest")
    vertex_count = geometry.get("vertex_count")
    edge_count = geometry.get("edge_count")
    sampled_point_count = geometry.get("sampled_point_count")
    return bool(
        geometry.get("schema") == _FREECAD_SHAPE_GEOMETRY_SCHEMA
        and geometry.get("tolerance") == _FREECAD_SHAPE_FINGERPRINT_QUANTUM
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and type(vertex_count) is int
        and vertex_count >= 0
        and type(edge_count) is int
        and edge_count >= 0
        and type(sampled_point_count) is int
        and sampled_point_count >= edge_count
        and vertex_count + sampled_point_count > 0
    )


def _freecad_numeric_mapping_equivalent(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict) or set(left) != set(right):
        return False
    for key in left:
        left_number = _freecad_finite_number(left.get(key))
        right_number = _freecad_finite_number(right.get(key))
        if (
            left_number is None
            or right_number is None
            or abs(left_number - right_number) > _FREECAD_SHAPE_FINGERPRINT_TOLERANCE
        ):
            return False
    return True


def _freecad_raw_shape_digest(geometry: Any) -> Optional[str]:
    if not _freecad_raw_shape_geometry_valid(geometry):
        return None
    payload = json.dumps(
        geometry, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _freecad_archive_entry_name_valid(value: Any) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\\" in value
        or ":" in value
        or value.startswith("/")
        or value.endswith("/")
        or "//" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    return all(part and part not in {".", ".."} for part in value.split("/"))


def _freecad_static_shape_digest(content: Any) -> Optional[str]:
    if not isinstance(content, dict):
        return None
    payload = {
        "method": "cheap_topology_metrics_bounds",
        "topology_counts": content.get("shape_topology_counts"),
        "metrics": content.get("shape_metrics"),
        "bounds": content.get("shape_bounds"),
    }
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _freecad_fcstd_archive_shape_snapshot_verified(content: Any) -> bool:
    """Validate one cheap shape snapshot bound to exact persisted FCStd bytes."""

    if not isinstance(content, dict):
        return False
    digest_pattern = re.compile(r"[0-9a-f]{64}")
    if content.get("shape_snapshot_method") == _FREECAD_FCSTD_ARCHIVE_METHOD:
        return bool(
            content.get("shape_persistence_method")
            == _FREECAD_FCSTD_ARCHIVE_METHOD
            and content.get("shape_archive_schema")
            == _FREECAD_FCSTD_ARCHIVE_SCHEMA
            and content.get("shape_nonempty") is True
            and content.get("shape_structure_verified") is True
            and isinstance(content.get("shape_digest"), str)
            and digest_pattern.fullmatch(content.get("shape_digest")) is not None
            and all(
                field not in content
                for field in (
                    "shape_topology_counts",
                    "shape_metrics",
                    "shape_bounds",
                    "shape_archive_entry",
                    "shape_archive_sha256",
                    "shape_archive_evidence_digest",
                )
            )
        )
    topology = content.get("shape_topology_counts")
    integer_fields = {
        "shape_archive_fcstd_bytes": 1,
        "shape_archive_document_xml_bytes": 1,
        "shape_archive_entry_count": 1,
        "shape_archive_mapping_count": 1,
        "shape_archive_expected_shape_count": 1,
        "shape_archive_expected_nonempty_shape_count": 0,
        "shape_archive_expected_zero_ink_shape_count": 0,
        "shape_archive_bytes": 1,
        "shape_archive_compression_method": 0,
        "shape_archive_crc32": 0,
    }
    if any(
        type(content.get(field)) is not int or content.get(field) < minimum
        for field, minimum in integer_fields.items()
    ):
        return False
    return bool(
        content.get("shape_persistence_method") == _FREECAD_FCSTD_ARCHIVE_METHOD
        and content.get("shape_archive_schema") == _FREECAD_FCSTD_ARCHIVE_SCHEMA
        and content.get("shape_snapshot_method")
        == "cheap_topology_metrics_bounds"
        and all(
            isinstance(content.get(field), str)
            and digest_pattern.fullmatch(content.get(field)) is not None
            for field in (
                "shape_digest",
                "shape_archive_evidence_digest",
                "shape_archive_fcstd_sha256",
                "shape_archive_document_xml_sha256",
                "shape_archive_sha256",
            )
        )
        and content.get("shape_digest") == _freecad_static_shape_digest(content)
        and content.get("shape_archive_zero_visible_ink") is False
        and content.get("shape_nonempty") is True
        and content.get("shape_structure_verified") is True
        and isinstance(topology, dict)
        and bool(topology)
        and any(type(count) is int and count > 0 for count in topology.values())
        and _freecad_archive_entry_name_valid(content.get("shape_archive_entry"))
        and content.get("shape_archive_mapping_count")
        >= content.get("shape_archive_expected_shape_count")
        and content.get("shape_archive_entry_count")
        >= content.get("shape_archive_mapping_count") + 1
        and content.get("shape_archive_compression_method") in {0, 8}
        and content.get("shape_archive_crc32") <= 0xFFFFFFFF
    )


def _freecad_shape_content_static_fields_match(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    if not _freecad_numeric_mapping_equivalent(
        left.get("shape_metrics"), right.get("shape_metrics")
    ) or not _freecad_numeric_mapping_equivalent(
        left.get("shape_bounds"), right.get("shape_bounds")
    ):
        return False
    digest_pattern = re.compile(r"[0-9a-f]{64}")
    if not all(
        isinstance(content.get("shape_digest"), str)
        and digest_pattern.fullmatch(content.get("shape_digest")) is not None
        for content in (left, right)
    ):
        return False
    ignored = {
        "_shape_comparison_geometry",
        "shape_fingerprint_geometry",
        "shape_metrics",
        "shape_bounds",
        "shape_digest",
    }
    return {
        key: value for key, value in left.items() if key not in ignored
    } == {key: value for key, value in right.items() if key not in ignored}


def _freecad_shape_comparison_digest(certificate: Dict[str, Any]) -> str:
    payload = {
        key: value
        for key, value in certificate.items()
        if key != "comparison_digest"
    }
    return hashlib.sha256(
        json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _freecad_public_shape_content_digest(content: Any) -> Optional[str]:
    """Digest retained public content for internal consistency verification.

    This unkeyed checksum makes independent report-field tampering observable.
    It is not an authenticity claim: coordinated rewriting remains possible
    without a trusted external signature or other anchor.
    """

    if not isinstance(content, dict):
        return None
    public_content = {
        key: value
        for key, value in content.items()
        if key != "_shape_comparison_geometry"
    }
    try:
        payload = json.dumps(
            public_content,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    return hashlib.sha256(payload).hexdigest()


def _freecad_shape_static_max_delta(left: Any, right: Any) -> Optional[float]:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    deltas: List[float] = []
    for mapping_name in ("shape_metrics", "shape_bounds"):
        left_values = left.get(mapping_name)
        right_values = right.get(mapping_name)
        if (
            not isinstance(left_values, dict)
            or not isinstance(right_values, dict)
            or set(left_values) != set(right_values)
        ):
            return None
        for key in left_values:
            left_number = _freecad_finite_number(left_values.get(key))
            right_number = _freecad_finite_number(right_values.get(key))
            if left_number is None or right_number is None:
                return None
            deltas.append(abs(left_number - right_number))
    return max(deltas) if deltas else 0.0


def _freecad_host_content_comparison(
    left: Any,
    right: Any,
    entity_id: str = "",
) -> Dict[str, Any]:
    """Compare live content and issue a compact certificate for shape roundoff."""

    if not isinstance(left, dict) or not isinstance(right, dict):
        return {"verified": False, "certificate": None}
    left_zero_ink = _freecad_zero_ink_shape_snapshot_verified(left)
    right_zero_ink = _freecad_zero_ink_shape_snapshot_verified(right)
    if left_zero_ink or right_zero_ink:
        return {
            "verified": bool(left_zero_ink and right_zero_ink and left == right),
            "certificate": None,
        }
    left_archive = _freecad_fcstd_archive_shape_snapshot_verified(left)
    right_archive = _freecad_fcstd_archive_shape_snapshot_verified(right)
    if left_archive or right_archive:
        archive_only = bool(
            left.get("shape_snapshot_method") == _FREECAD_FCSTD_ARCHIVE_METHOD
            or right.get("shape_snapshot_method") == _FREECAD_FCSTD_ARCHIVE_METHOD
        )
        if archive_only:
            return {
                "verified": bool(left_archive and right_archive and left == right),
                "certificate": None,
            }
        left_content_digest = _freecad_public_shape_content_digest(left)
        right_content_digest = _freecad_public_shape_content_digest(right)
        max_delta = _freecad_shape_static_max_delta(left, right)
        if (
            not left_archive
            or not right_archive
            or left_content_digest is None
            or right_content_digest is None
            or max_delta is None
            or max_delta > _FREECAD_SHAPE_FINGERPRINT_TOLERANCE
            or not _freecad_shape_content_static_fields_match(left, right)
        ):
            return {"verified": False, "certificate": None}
        certificate = {
            "schema": _FREECAD_SHAPE_COMPARISON_SCHEMA,
            "method": _FREECAD_FCSTD_ARCHIVE_METHOD,
            "tolerance": _FREECAD_SHAPE_FINGERPRINT_QUANTUM,
            "entity_id": str(entity_id or ""),
            "archive_evidence_digest": left.get("shape_archive_evidence_digest"),
            "fcstd_sha256": left.get("shape_archive_fcstd_sha256"),
            "fcstd_bytes": left.get("shape_archive_fcstd_bytes"),
            "document_xml_sha256": left.get(
                "shape_archive_document_xml_sha256"
            ),
            "document_xml_bytes": left.get("shape_archive_document_xml_bytes"),
            "archive_entry_count": left.get("shape_archive_entry_count"),
            "shape_mapping_count": left.get("shape_archive_mapping_count"),
            "expected_shape_count": left.get(
                "shape_archive_expected_shape_count"
            ),
            "archive_entry": left.get("shape_archive_entry"),
            "archive_entry_sha256": left.get("shape_archive_sha256"),
            "archive_entry_bytes": left.get("shape_archive_bytes"),
            "archive_entry_compression_method": left.get(
                "shape_archive_compression_method"
            ),
            "archive_entry_crc32": left.get("shape_archive_crc32"),
            "expected_content_digest": left_content_digest,
            "actual_content_digest": right_content_digest,
            "expected_topology_counts": dict(left.get("shape_topology_counts") or {}),
            "actual_topology_counts": dict(right.get("shape_topology_counts") or {}),
            "max_delta": max_delta,
            "verified": True,
        }
        certificate["comparison_digest"] = _freecad_shape_comparison_digest(
            certificate
        )
        return {"verified": True, "certificate": certificate}
    left_public = left.get("shape_fingerprint_geometry")
    right_public = right.get("shape_fingerprint_geometry")
    if left_public is None and right_public is None:
        return {"verified": left == right, "certificate": None}
    left_raw = left.get("_shape_comparison_geometry")
    right_raw = right.get("_shape_comparison_geometry")
    left_raw_digest = _freecad_raw_shape_digest(left_raw)
    right_raw_digest = _freecad_raw_shape_digest(right_raw)
    left_content_digest = _freecad_public_shape_content_digest(left)
    right_content_digest = _freecad_public_shape_content_digest(right)
    left_sampled_point_count = (
        sum(len(edge.get("points", [])) for edge in left_raw.get("edges", []))
        if isinstance(left_raw, dict)
        else -1
    )
    right_sampled_point_count = (
        sum(len(edge.get("points", [])) for edge in right_raw.get("edges", []))
        if isinstance(right_raw, dict)
        else -1
    )
    if (
        not _freecad_shape_geometry_digest_valid(left_public)
        or not _freecad_shape_geometry_digest_valid(right_public)
        or left_raw_digest != left_public.get("sample_digest")
        or right_raw_digest != right_public.get("sample_digest")
        or left_public.get("vertex_count") != len(left_raw.get("vertices", []))
        or right_public.get("vertex_count") != len(right_raw.get("vertices", []))
        or left_public.get("edge_count") != len(left_raw.get("edges", []))
        or right_public.get("edge_count") != len(right_raw.get("edges", []))
        or left_public.get("sampled_point_count") != left_sampled_point_count
        or right_public.get("sampled_point_count") != right_sampled_point_count
        or left_content_digest is None
        or right_content_digest is None
        or not _freecad_shape_content_static_fields_match(left, right)
    ):
        return {"verified": False, "certificate": None}
    geometry_comparison = (
        {"verified": True, "max_delta": 0.0}
        if left_raw_digest == right_raw_digest
        else _freecad_shape_geometry_comparison(left_raw, right_raw)
    )
    max_delta = _freecad_finite_number(geometry_comparison.get("max_delta"))
    if geometry_comparison.get("verified") is not True or max_delta is None:
        return {"verified": False, "certificate": None}
    certificate: Dict[str, Any] = {
        "schema": _FREECAD_SHAPE_COMPARISON_SCHEMA,
        "method": "sampled_geometry_tolerant",
        "tolerance": _FREECAD_SHAPE_FINGERPRINT_QUANTUM,
        "entity_id": str(entity_id or ""),
        "expected_sample_digest": left_raw_digest,
        "actual_sample_digest": right_raw_digest,
        "expected_content_digest": left_content_digest,
        "actual_content_digest": right_content_digest,
        "vertex_count": left_public.get("vertex_count"),
        "edge_count": left_public.get("edge_count"),
        "sampled_point_count": left_sampled_point_count,
        "max_delta": max_delta,
        "verified": True,
    }
    certificate["comparison_digest"] = _freecad_shape_comparison_digest(certificate)
    return {"verified": True, "certificate": certificate}


def _freecad_host_content_equivalent(left: Any, right: Any) -> bool:
    """Compatibility bool for live persisted-content comparisons."""

    return _freecad_host_content_comparison(left, right).get("verified") is True


def _freecad_shape_comparison_certificate_valid(
    certificate: Any,
    left: Dict[str, Any],
    right: Dict[str, Any],
    entity_id: str,
) -> bool:
    if not isinstance(certificate, dict):
        return False
    if certificate.get("method") == _FREECAD_FCSTD_ARCHIVE_METHOD:
        left_content_digest = _freecad_public_shape_content_digest(left)
        right_content_digest = _freecad_public_shape_content_digest(right)
        max_delta = _freecad_finite_number(certificate.get("max_delta"))
        expected_max_delta = _freecad_shape_static_max_delta(left, right)
        comparison_digest = certificate.get("comparison_digest")
        bound_fields = {
            "archive_evidence_digest": "shape_archive_evidence_digest",
            "fcstd_sha256": "shape_archive_fcstd_sha256",
            "fcstd_bytes": "shape_archive_fcstd_bytes",
            "document_xml_sha256": "shape_archive_document_xml_sha256",
            "document_xml_bytes": "shape_archive_document_xml_bytes",
            "archive_entry_count": "shape_archive_entry_count",
            "shape_mapping_count": "shape_archive_mapping_count",
            "expected_shape_count": "shape_archive_expected_shape_count",
            "archive_entry": "shape_archive_entry",
            "archive_entry_sha256": "shape_archive_sha256",
            "archive_entry_bytes": "shape_archive_bytes",
            "archive_entry_compression_method": (
                "shape_archive_compression_method"
            ),
            "archive_entry_crc32": "shape_archive_crc32",
        }
        return bool(
            _freecad_fcstd_archive_shape_snapshot_verified(left)
            and _freecad_fcstd_archive_shape_snapshot_verified(right)
            and certificate.get("schema") == _FREECAD_SHAPE_COMPARISON_SCHEMA
            and certificate.get("tolerance")
            == _FREECAD_SHAPE_FINGERPRINT_QUANTUM
            and certificate.get("entity_id") == entity_id
            and all(
                certificate.get(certificate_field) == left.get(content_field)
                and certificate.get(certificate_field) == right.get(content_field)
                for certificate_field, content_field in bound_fields.items()
            )
            and certificate.get("expected_content_digest") == left_content_digest
            and certificate.get("actual_content_digest") == right_content_digest
            and certificate.get("expected_topology_counts")
            == left.get("shape_topology_counts")
            and certificate.get("actual_topology_counts")
            == right.get("shape_topology_counts")
            and expected_max_delta is not None
            and max_delta == expected_max_delta
            and 0.0 <= max_delta <= _FREECAD_SHAPE_FINGERPRINT_TOLERANCE
            and _freecad_shape_content_static_fields_match(left, right)
            and certificate.get("verified") is True
            and isinstance(comparison_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", comparison_digest) is not None
            and comparison_digest == _freecad_shape_comparison_digest(certificate)
        )
    left_geometry = left.get("shape_fingerprint_geometry")
    right_geometry = right.get("shape_fingerprint_geometry")
    left_content_digest = _freecad_public_shape_content_digest(left)
    right_content_digest = _freecad_public_shape_content_digest(right)
    max_delta = _freecad_finite_number(certificate.get("max_delta"))
    comparison_digest = certificate.get("comparison_digest")
    return bool(
        _freecad_shape_geometry_digest_valid(left_geometry)
        and _freecad_shape_geometry_digest_valid(right_geometry)
        and certificate.get("schema") == _FREECAD_SHAPE_COMPARISON_SCHEMA
        and certificate.get("method") == "sampled_geometry_tolerant"
        and certificate.get("tolerance") == _FREECAD_SHAPE_FINGERPRINT_QUANTUM
        and certificate.get("entity_id") == entity_id
        and certificate.get("expected_sample_digest")
        == left_geometry.get("sample_digest")
        and certificate.get("actual_sample_digest")
        == right_geometry.get("sample_digest")
        and certificate.get("expected_content_digest") == left_content_digest
        and certificate.get("actual_content_digest") == right_content_digest
        and certificate.get("vertex_count") == left_geometry.get("vertex_count")
        and certificate.get("vertex_count") == right_geometry.get("vertex_count")
        and certificate.get("edge_count") == left_geometry.get("edge_count")
        and certificate.get("edge_count") == right_geometry.get("edge_count")
        and certificate.get("sampled_point_count")
        == left_geometry.get("sampled_point_count")
        and certificate.get("sampled_point_count")
        == right_geometry.get("sampled_point_count")
        and max_delta is not None
        and 0.0 <= max_delta <= _FREECAD_SHAPE_FINGERPRINT_TOLERANCE
        and certificate.get("verified") is True
        and isinstance(comparison_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", comparison_digest) is not None
        and comparison_digest == _freecad_shape_comparison_digest(certificate)
    )


def _freecad_host_record_equivalent(
    left: Any, right: Any, certificate: Any = None
) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    ignored = {"content"}
    if {key: value for key, value in left.items() if key not in ignored} != {
        key: value for key, value in right.items() if key not in ignored
    }:
        return False
    left_content = left.get("content")
    right_content = right.get("content")
    if not isinstance(left_content, dict) or not isinstance(right_content, dict):
        return False
    left_zero_ink = _freecad_zero_ink_shape_snapshot_verified(left_content)
    right_zero_ink = _freecad_zero_ink_shape_snapshot_verified(right_content)
    if left_zero_ink or right_zero_ink:
        return bool(
            certificate is None
            and left_zero_ink
            and right_zero_ink
            and left_content == right_content
        )
    left_archive = _freecad_fcstd_archive_shape_snapshot_verified(left_content)
    right_archive = _freecad_fcstd_archive_shape_snapshot_verified(right_content)
    if left_archive or right_archive:
        if (
            left_content.get("shape_snapshot_method")
            == _FREECAD_FCSTD_ARCHIVE_METHOD
            or right_content.get("shape_snapshot_method")
            == _FREECAD_FCSTD_ARCHIVE_METHOD
        ):
            return bool(
                certificate is None
                and left_archive
                and right_archive
                and left_content == right_content
            )
        return bool(
            left_archive
            and right_archive
            and _freecad_shape_content_static_fields_match(left_content, right_content)
            and _freecad_shape_comparison_certificate_valid(
                certificate,
                left_content,
                right_content,
                str(left.get("entity_id") or ""),
            )
        )
    left_geometry = left_content.get("shape_fingerprint_geometry")
    right_geometry = right_content.get("shape_fingerprint_geometry")
    if left_geometry is None and right_geometry is None:
        return certificate is None and left_content == right_content
    return bool(
        _freecad_shape_content_static_fields_match(left_content, right_content)
        and _freecad_shape_comparison_certificate_valid(
            certificate,
            left_content,
            right_content,
            str(left.get("entity_id") or ""),
        )
    )


def _freecad_shape_fingerprint_verified(content: Any) -> bool:
    """Validate the canonical geometry proof carried by a host snapshot."""

    if not isinstance(content, dict):
        return False
    topology = content.get("shape_topology_counts")
    vertex_count = content.get("shape_fingerprint_vertex_count")
    edge_count = content.get("shape_fingerprint_edge_count")
    sampled_edge_count = content.get("shape_fingerprint_sampled_edge_count")
    geometry = content.get("shape_fingerprint_geometry")
    if (
        content.get("shape_fingerprint_verified") is not True
        or content.get("shape_fingerprint_schema")
        != _FREECAD_SHAPE_FINGERPRINT_SCHEMA
        or content.get("shape_fingerprint_quantum")
        != _FREECAD_SHAPE_FINGERPRINT_QUANTUM
        or not isinstance(topology, dict)
        or type(vertex_count) is not int
        or vertex_count < 0
        or type(edge_count) is not int
        or edge_count < 0
        or type(sampled_edge_count) is not int
        or sampled_edge_count < 0
        or sampled_edge_count != edge_count
        or vertex_count + sampled_edge_count <= 0
        or not _freecad_shape_geometry_digest_valid(geometry)
        or geometry.get("vertex_count") != vertex_count
        or geometry.get("edge_count") != edge_count
    ):
        return False
    if "vertexes" in topology and topology.get("vertexes") != vertex_count:
        return False
    if "edges" in topology and topology.get("edges") != edge_count:
        return False
    return True


def _freecad_zero_ink_shape_snapshot_verified(content: Any) -> bool:
    """Validate the persisted empty host shape used for physical zero ink."""

    if not isinstance(content, dict):
        return False
    digest = content.get("source_ink_evidence_sha256")
    archive_base_valid = bool(
        content.get("source_ink_classification") == "zero_visible_ink"
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and content.get("shape_nonempty") is False
        and content.get("shape_structure_verified") is False
        and content.get("shape_digest") == ""
    )
    if content.get("shape_snapshot_method") == _FREECAD_FCSTD_ARCHIVE_METHOD:
        return bool(
            archive_base_valid
            and content.get("shape_persistence_method")
            == _FREECAD_FCSTD_ARCHIVE_METHOD
            and content.get("shape_archive_schema")
            == _FREECAD_FCSTD_ARCHIVE_SCHEMA
            and all(
                field not in content
                for field in (
                    "shape_topology_counts",
                    "shape_metrics",
                    "shape_bounds",
                    "shape_archive_entry",
                    "shape_archive_sha256",
                    "shape_archive_evidence_digest",
                )
            )
        )
    topology = content.get("shape_topology_counts")
    metrics = content.get("shape_metrics")
    common_valid = bool(
        content.get("source_ink_classification") == "zero_visible_ink"
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
        and content.get("shape_nonempty") is False
        and type(content.get("shape_is_null")) is bool
        and content.get("shape_structure_verified") is False
        and isinstance(topology, dict)
        and topology
        and all(type(count) is int and count == 0 for count in topology.values())
        and isinstance(metrics, dict)
        and all(
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(float(value))
            and abs(float(value)) <= 1e-12
            for value in metrics.values()
        )
        and content.get("shape_digest") == ""
    )
    if not common_valid:
        return False
    if content.get("shape_snapshot_method") == "cheap_topology_metrics_bounds":
        bounds = content.get("shape_bounds")
        return bool(
            isinstance(bounds, dict)
            and all(
                not isinstance(value, bool)
                and isinstance(value, (int, float))
                and math.isfinite(float(value))
                for value in bounds.values()
            )
            and all(
                field not in content
                for field in (
                    "shape_fingerprint_schema",
                    "shape_fingerprint_quantum",
                    "shape_fingerprint_vertex_count",
                    "shape_fingerprint_edge_count",
                    "shape_fingerprint_sampled_edge_count",
                    "shape_fingerprint_verified",
                    "shape_fingerprint_geometry",
                )
            )
        )
    geometry = content.get("shape_fingerprint_geometry")
    return bool(
        "shape_snapshot_method" not in content
        and content.get("shape_fingerprint_schema")
        == _FREECAD_SHAPE_FINGERPRINT_SCHEMA
        and content.get("shape_fingerprint_quantum")
        == _FREECAD_SHAPE_FINGERPRINT_QUANTUM
        and content.get("shape_fingerprint_vertex_count") == 0
        and content.get("shape_fingerprint_edge_count") == 0
        and content.get("shape_fingerprint_sampled_edge_count") == 0
        and content.get("shape_fingerprint_verified") is False
        and isinstance(geometry, dict)
        and geometry.get("schema") == _FREECAD_SHAPE_GEOMETRY_SCHEMA
        and geometry.get("tolerance") == _FREECAD_SHAPE_FINGERPRINT_QUANTUM
        and isinstance(geometry.get("sample_digest"), str)
        and re.fullmatch(r"[0-9a-f]{64}", geometry.get("sample_digest")) is not None
        and geometry.get("vertex_count") == 0
        and geometry.get("edge_count") == 0
        and geometry.get("sampled_point_count") == 0
    )


def _freecad_source_ink_evidence_verified(
    evidence: Any,
    source_item_id: str,
    source_text: str,
    *,
    expected_pdf_sha256: str,
    expected_page_number: int,
    expected_font_identity: Dict[str, Any],
    expected_font_asset_bindings: List[Dict[str, Any]],
    expected_glyph_id_sequence: List[Optional[int]],
) -> bool:
    """Revalidate one sealed proof against independent report/source identity."""

    from .source_ink import source_ink_evidence_verified

    return source_ink_evidence_verified(
        evidence,
        expected_pdf_sha256=expected_pdf_sha256,
        expected_page_number=expected_page_number,
        expected_source_item_id=source_item_id,
        expected_source_text=source_text,
        expected_font_identity=expected_font_identity,
        expected_font_asset_bindings=expected_font_asset_bindings,
        expected_glyph_id_sequence=expected_glyph_id_sequence,
    )


def _freecad_source_ink_inventory_binding_verified(
    terminal_evidence: Any,
    content: Any,
    source_item_id: str,
    source_text: str,
    *,
    expected_pdf_sha256: str,
    expected_page_number: int,
    expected_font_identity: Dict[str, Any],
    expected_font_asset_bindings: List[Dict[str, Any]],
    expected_glyph_id_sequence: List[Optional[int]],
) -> bool:
    if not isinstance(terminal_evidence, dict) or not isinstance(content, dict):
        return False
    source_evidence = terminal_evidence.get("source_ink_evidence")
    return bool(
        terminal_evidence.get("source_ink_evidence_persisted") is True
        and _freecad_source_ink_evidence_verified(
            source_evidence,
            source_item_id,
            source_text,
            expected_pdf_sha256=expected_pdf_sha256,
            expected_page_number=expected_page_number,
            expected_font_identity=expected_font_identity,
            expected_font_asset_bindings=expected_font_asset_bindings,
            expected_glyph_id_sequence=expected_glyph_id_sequence,
        )
        and content.get("source_ink_classification")
        == source_evidence.get("classification")
        and content.get("source_ink_evidence_sha256")
        == source_evidence.get("evidence_sha256")
    )


_FREECAD_FONT_DELIVERY_BOUND_FIELDS = frozenset(
    {
        "source_font_equivalence",
        "font_substitution_applied",
        "font_identity_verified",
        "glyph_coverage_verified",
        "delivered_font_sha256",
        "font_candidate_source",
        "source_font_asset_path",
        "source_font_asset_sha256",
        "staged_font_path",
        "staged_font_sha256",
        "staged_asset_verified",
        "staged_asset_read_only",
        "font_internal_identity_sha256",
        "font_coverage_evidence_sha256",
    }
)
_FREECAD_FONT_DELIVERY_INVENTORY_FIELDS = (
    _FREECAD_FONT_DELIVERY_BOUND_FIELDS
    | {
        "delivered_font_path",
        "source_font_identity_json",
        "staged_asset_actual_sha256",
        "staged_asset_digest_matches",
    }
)
_FREECAD_REPORT_FONT_MAX_BYTES = 64 * 1024 * 1024


def _freecad_font_delivery_proof_recomputed(
    terminal_evidence: Dict[str, Any],
    staged_payload: bytes,
    staged_sha256: str,
) -> bool:
    """Recompute both structured font proofs from the immutable staged bytes."""

    internal_evidence = terminal_evidence.get("font_internal_identity_evidence")
    coverage_evidence = terminal_evidence.get("font_coverage_evidence")
    source_identity = terminal_evidence.get("source_font_identity")
    source_text = terminal_evidence.get("source_text")
    if (
        type(internal_evidence) is not dict
        or type(coverage_evidence) is not dict
        or type(source_identity) is not dict
        or not isinstance(source_text, str)
        or not source_text
    ):
        return False
    try:
        # PDFImporterCore owns the canonical parser used at delivery.  Importing
        # here is intentionally runtime-only: report verification happens after
        # Core has loaded, and recomputes rather than trusting caller-supplied
        # hexadecimal strings or semantic flags.
        from PDFImporterCore import (
            _font_bytes_glyph_coverage,
            _font_internal_identity_evidence,
        )

        recomputed_identity = _font_internal_identity_evidence(
            staged_payload,
            source_identity,
        )
        recomputed_coverage = _font_bytes_glyph_coverage(
            staged_payload,
            source_text,
        )
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return False
    return bool(
        internal_evidence == recomputed_identity
        and coverage_evidence == recomputed_coverage
        and internal_evidence.get("font_sha256") == staged_sha256
        and coverage_evidence.get("font_sha256") == staged_sha256
        and terminal_evidence.get("font_internal_identity_sha256")
        == recomputed_identity.get("evidence_sha256")
        and terminal_evidence.get("font_coverage_evidence_sha256")
        == recomputed_coverage.get("coverage_evidence_sha256")
        and (
            terminal_evidence.get("source_font_equivalence") is False
            or recomputed_identity.get("font_identity_verified") is True
        )
        and terminal_evidence.get("glyph_coverage_verified")
        is recomputed_coverage.get("glyph_coverage_verified")
    )


def _freecad_font_delivery_inventory_binding_verified(
    terminal_evidence: Any,
    content: Any,
) -> bool:
    """Bind visible 3D text to its exact immutable staged font bytes."""

    if not isinstance(terminal_evidence, dict) or not isinstance(content, dict):
        return False
    font_delivery = content.get("font_delivery")
    if (
        type(font_delivery) is not dict
        or set(font_delivery) != _FREECAD_FONT_DELIVERY_INVENTORY_FIELDS
        or not _FREECAD_FONT_DELIVERY_BOUND_FIELDS.issubset(terminal_evidence)
        or any(
            font_delivery.get(field) != terminal_evidence.get(field)
            for field in _FREECAD_FONT_DELIVERY_BOUND_FIELDS
        )
    ):
        return False

    source_font_identity = terminal_evidence.get("source_font_identity")
    source_identity_json = font_delivery.get("source_font_identity_json")
    if (
        type(source_font_identity) is not dict
        or set(source_font_identity) != {"raw_name", "normalized_key"}
        or any(
            not isinstance(source_font_identity.get(field), str)
            or not source_font_identity.get(field)
            or source_font_identity.get(field) != source_font_identity.get(field).strip()
            for field in ("raw_name", "normalized_key")
        )
        or not isinstance(source_identity_json, str)
        or not source_identity_json
    ):
        return False
    try:
        canonical_identity_json = json.dumps(
            source_font_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        parsed_identity = json.loads(source_identity_json)
    except (RecursionError, TypeError, ValueError):
        return False
    if (
        source_identity_json != canonical_identity_json
        or parsed_identity != source_font_identity
    ):
        return False

    equivalence = terminal_evidence.get("source_font_equivalence")
    substitution = terminal_evidence.get("font_substitution_applied")
    identity_verified = terminal_evidence.get("font_identity_verified")
    coverage_verified = terminal_evidence.get("glyph_coverage_verified")
    staged_verified = terminal_evidence.get("staged_asset_verified")
    staged_read_only = terminal_evidence.get("staged_asset_read_only")
    if (
        type(equivalence) is not bool
        or type(substitution) is not bool
        or type(identity_verified) is not bool
        or type(coverage_verified) is not bool
        or type(staged_verified) is not bool
        or type(staged_read_only) is not bool
        or equivalence is substitution
        or identity_verified is not equivalence
        or coverage_verified is not True
        or staged_verified is not True
        or staged_read_only is not True
        or font_delivery.get("staged_asset_digest_matches") is not True
    ):
        return False

    digest_fields = (
        "delivered_font_sha256",
        "source_font_asset_sha256",
        "staged_font_sha256",
        "font_internal_identity_sha256",
        "font_coverage_evidence_sha256",
    )
    if any(
        not isinstance(terminal_evidence.get(field), str)
        or re.fullmatch(r"[0-9a-f]{64}", terminal_evidence.get(field)) is None
        for field in digest_fields
    ):
        return False
    delivered_digest = terminal_evidence["delivered_font_sha256"]
    if not all(
        terminal_evidence.get(field) == delivered_digest
        for field in ("source_font_asset_sha256", "staged_font_sha256")
    ):
        return False

    source_path_value = terminal_evidence.get("source_font_asset_path")
    staged_path_value = terminal_evidence.get("staged_font_path")
    font_path_value = terminal_evidence.get("font_path")
    candidate_source = terminal_evidence.get("font_candidate_source")
    if (
        not isinstance(source_path_value, str)
        or not source_path_value
        or source_path_value != source_path_value.strip()
        or not Path(source_path_value).is_absolute()
        or not isinstance(staged_path_value, str)
        or not staged_path_value
        or staged_path_value != staged_path_value.strip()
        or not Path(staged_path_value).is_absolute()
        or font_path_value != staged_path_value
        or font_delivery.get("delivered_font_path") != staged_path_value
        or font_delivery.get("staged_asset_actual_sha256")
        != terminal_evidence.get("staged_font_sha256")
        or not isinstance(candidate_source, str)
        or not candidate_source
        or candidate_source != candidate_source.strip()
    ):
        return False

    staged_path = Path(staged_path_value)
    if (
        staged_path.stem.lower() != delivered_digest
        or staged_path.suffix.lower() not in {".ttf", ".otf", ".ttc"}
    ):
        return False
    try:
        before = staged_path.stat()
        if (
            not staged_path.is_file()
            or before.st_size <= 0
            or before.st_size > _FREECAD_REPORT_FONT_MAX_BYTES
            or before.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        ):
            return False
        digest = hashlib.sha256()
        staged_payload = bytearray()
        with staged_path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                staged_payload.extend(chunk)
        after = staged_path.stat()
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    actual_digest = digest.hexdigest()
    return bool(
        (before.st_size, before.st_mtime_ns)
        == (after.st_size, after.st_mtime_ns)
        and actual_digest == delivered_digest
        and _freecad_font_delivery_proof_recomputed(
            terminal_evidence,
            bytes(staged_payload),
            actual_digest,
        )
    )


def _freecad_expected_segment_source_ink_evidence(
    parent_evidence: Any,
    *,
    child_source_item_id: str,
    source_index_start: int,
    source_index_end: int,
) -> Optional[Dict[str, Any]]:
    """Derive one child proof exclusively from an authoritative parent slice."""

    if not isinstance(parent_evidence, dict):
        return None
    parent_characters = parent_evidence.get("characters")
    parent_bindings = parent_evidence.get("font_asset_bindings")
    if (
        not isinstance(parent_characters, list)
        or not isinstance(parent_bindings, list)
        or type(source_index_start) is not int
        or type(source_index_end) is not int
        or source_index_start < 0
        or source_index_end <= source_index_start
        or source_index_end > len(parent_characters)
    ):
        return None
    source_records = parent_characters[source_index_start:source_index_end]
    if not source_records or any(not isinstance(record, dict) for record in source_records):
        return None
    characters = [
        {**record, "source_index": index}
        for index, record in enumerate(source_records)
    ]
    zero_flags = [record.get("zero_visible_ink") for record in characters]
    if any(type(flag) is not bool for flag in zero_flags):
        return None
    classification = (
        "zero_visible_ink"
        if all(zero_flags)
        else "mixed_visible_and_zero_ink"
        if any(zero_flags)
        else "visible_ink"
    )
    if classification == "mixed_visible_and_zero_ink":
        return None
    source_text = "".join(str(record.get("character") or "") for record in characters)
    if not source_text:
        return None
    child_bindings = [
        dict(binding)
        for binding in parent_bindings
        if isinstance(binding, dict)
        and any(record.get("font_asset_binding") == binding for record in characters)
    ]
    expected = dict(parent_evidence)
    expected.update(
        {
            "source_item_id": child_source_item_id,
            "source_text": source_text,
            "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "classification": classification,
            "zero_ink_characters_layout_only": bool(
                classification == "zero_visible_ink"
                and all(record.get("layout_only_zero_ink") is True for record in characters)
            ),
            "font_asset_bindings": child_bindings,
            "glyph_id_sequence": [record.get("glyph_id") for record in characters],
            "characters": characters,
        }
    )
    try:
        from .source_ink import source_ink_evidence_digest

        expected["evidence_sha256"] = source_ink_evidence_digest(expected)
    except (RecursionError, TypeError, ValueError):
        return None
    return expected


def _freecad_segment_delivery_contexts(
    terminal: Dict[str, Any],
    *,
    expected_pdf_sha256: str,
    expected_page_number: int,
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Validate one mixed-run manifest and index its exact child host contexts."""

    evidence = terminal.get("evidence")
    if not isinstance(evidence, dict):
        return None
    manifest = evidence.get("source_segment_manifest")
    deliveries = evidence.get("segment_deliveries")
    if manifest is None and deliveries is None:
        return None
    if not isinstance(manifest, dict) or not isinstance(deliveries, list):
        return {}

    parent_id = terminal.get("source_item_id")
    requested_type = str(terminal.get("requested_type") or "").strip().lower()
    final_type = str(terminal.get("final_type") or "").strip().lower()
    source_text = evidence.get("source_text")
    segments = manifest.get("segments")
    manifest_sha256 = manifest.get("manifest_sha256")
    if (
        manifest.get("schema") != "bcs.freecad_text_source_segments/1.0"
        or manifest.get("parent_source_item_id") != parent_id
        or manifest.get("requested_type") != requested_type
        or manifest.get("parent_source_ink_evidence_sha256")
        != (evidence.get("source_ink_evidence") or {}).get("evidence_sha256")
        or manifest.get("source_text") != source_text
        or manifest.get("source_text_sha256")
        != hashlib.sha256(str(source_text).encode("utf-8")).hexdigest()
        or not isinstance(segments, list)
        or len(segments) < 2
        or len(deliveries) != len(segments)
        or re.fullmatch(r"[0-9a-f]{64}", str(manifest_sha256 or "")) is None
    ):
        return {}
    unsigned_manifest = {
        key: value for key, value in manifest.items() if key != "manifest_sha256"
    }
    try:
        computed_manifest_sha256 = hashlib.sha256(
            json.dumps(
                unsigned_manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    except (TypeError, ValueError):
        return {}
    if computed_manifest_sha256 != manifest_sha256:
        return {}

    parent_source_ink = evidence.get("source_ink_evidence")
    parent_font_identity = evidence.get("source_font_identity")
    parent_font_bindings = evidence.get("source_font_asset_bindings")
    parent_glyph_ids = evidence.get("source_glyph_id_sequence")
    parent_characters = (
        parent_source_ink.get("characters")
        if isinstance(parent_source_ink, dict)
        else None
    )
    if (
        evidence.get("source_pdf_sha256") != expected_pdf_sha256
        or evidence.get("source_page_number") != expected_page_number
        or not isinstance(parent_font_identity, dict)
        or not isinstance(parent_font_bindings, list)
        or not isinstance(parent_glyph_ids, list)
        or not isinstance(parent_characters, list)
        or not _freecad_source_ink_evidence_verified(
            parent_source_ink,
            parent_id,
            source_text,
            expected_pdf_sha256=expected_pdf_sha256,
            expected_page_number=expected_page_number,
            expected_font_identity=parent_font_identity,
            expected_font_asset_bindings=parent_font_bindings,
            expected_glyph_id_sequence=parent_glyph_ids,
        )
    ):
        return {}

    child_ids: List[str] = []
    cursor = 0
    rebuilt_text: List[str] = []
    contexts: Dict[str, Dict[str, Any]] = {}
    all_created: set[str] = set()
    all_delivery: set[str] = set()
    all_support: set[str] = set()
    for index, (segment, child) in enumerate(zip(segments, deliveries, strict=True)):
        if not isinstance(segment, dict) or not isinstance(child, dict):
            return {}
        child_id = "%s:seg%d" % (parent_id, index)
        child_text = segment.get("source_text")
        start = segment.get("source_index_start")
        end = segment.get("source_index_end")
        role = segment.get("physical_role")
        child_evidence = child.get("evidence")
        child_source_ink = (
            child_evidence.get("source_ink_evidence")
            if isinstance(child_evidence, dict)
            else None
        )
        child_created = child.get("created_entity_ids")
        child_delivery = child.get("delivery_entity_ids")
        child_support = child.get("support_entity_ids")
        expected_child_source_ink = _freecad_expected_segment_source_ink_evidence(
            parent_source_ink,
            child_source_item_id=child_id,
            source_index_start=start,
            source_index_end=end,
        )
        expected_child_text = (
            expected_child_source_ink.get("source_text")
            if isinstance(expected_child_source_ink, dict)
            else None
        )
        expected_child_role = (
            "zero_visible_ink"
            if isinstance(expected_child_source_ink, dict)
            and expected_child_source_ink.get("classification")
            == "zero_visible_ink"
            else "visible"
        )
        if (
            segment.get("child_source_item_id") != child_id
            or segment.get("parent_source_item_id") != parent_id
            or segment.get("requested_type") != requested_type
            or type(start) is not int
            or type(end) is not int
            or start != cursor
            or end <= start
            or role not in {"visible", "zero_visible_ink"}
            or expected_child_source_ink is None
            or child_text != expected_child_text
            or role != expected_child_role
            or not isinstance(child_text, str)
            or not child_text
            or segment.get("source_text_sha256")
            != hashlib.sha256(child_text.encode("utf-8")).hexdigest()
            or child.get("source_item_id") != child_id
            or child.get("requested_type") != requested_type
            or child.get("attempted_type") != final_type
            or child.get("final_type") != final_type
            or child.get("outcome") != "verified"
            or child.get("cleanup_complete") is not True
            or child.get("removed_entity_ids") != []
            or not isinstance(child_evidence, dict)
            or child_evidence.get("source_text") != child_text
            or child_evidence.get("source_text_preserved") is not True
            or child_evidence.get("source_ink_evidence_persisted") is not True
            or not isinstance(child_created, list)
            or not isinstance(child_delivery, list)
            or not child_delivery
            or not isinstance(child_support, list)
        ):
            return {}
        id_lists = (child_created, child_delivery, child_support)
        if any(
            len(values) != len(set(values))
            or any(
                not isinstance(entity_id, str)
                or not entity_id
                or entity_id != entity_id.strip()
                for entity_id in values
            )
            for values in id_lists
        ):
            return {}
        created_set = set(child_created)
        delivery_set = set(child_delivery)
        support_set = set(child_support)
        if (
            delivery_set.intersection(support_set)
            or delivery_set.union(support_set) != created_set
            or all_created.intersection(created_set)
            or (
                "delivery_count" in child
                and (
                    type(child.get("delivery_count")) is not int
                    or child.get("delivery_count") != len(delivery_set)
                )
            )
            or not isinstance(child_source_ink, dict)
            or child_source_ink.get("classification")
            != ("zero_visible_ink" if role == "zero_visible_ink" else "visible_ink")
        ):
            return {}
        child_font_identity = child_evidence.get("source_font_identity")
        child_font_bindings = child_evidence.get("source_font_asset_bindings")
        child_glyph_ids = child_evidence.get("source_glyph_id_sequence")
        if (
            child_evidence.get("source_pdf_sha256") != expected_pdf_sha256
            or child_evidence.get("source_page_number") != expected_page_number
            or child_font_identity != parent_font_identity
            or child_font_bindings
            != expected_child_source_ink.get("font_asset_bindings")
            or child_glyph_ids != expected_child_source_ink.get("glyph_id_sequence")
            or child_source_ink != expected_child_source_ink
            or not _freecad_source_ink_evidence_verified(
                child_source_ink,
                child_id,
                child_text,
                expected_pdf_sha256=expected_pdf_sha256,
                expected_page_number=expected_page_number,
                expected_font_identity=child_font_identity,
                expected_font_asset_bindings=child_font_bindings,
                expected_glyph_id_sequence=child_glyph_ids,
            )
        ):
            return {}
        context = {
            "source_item_id": child_id,
            "source_text": child_text,
            "evidence": child_evidence,
            "source_ink_evidence": child_source_ink,
            "zero_visible_ink": role == "zero_visible_ink",
            "font_identity": child_font_identity,
            "font_asset_bindings": child_font_bindings,
            "glyph_id_sequence": child_glyph_ids,
            "delivery_entity_ids": list(child_delivery),
            "support_entity_ids": list(child_support),
        }
        for entity_id in created_set:
            contexts[entity_id] = context
        child_ids.append(child_id)
        rebuilt_text.append(child_text)
        cursor = end
        all_created.update(created_set)
        all_delivery.update(delivery_set)
        all_support.update(support_set)

    if (
        cursor != len(parent_characters)
        or "".join(rebuilt_text) != source_text
        or evidence.get("child_source_item_ids") != child_ids
        or evidence.get("source_segment_ink_evidence_persisted") is not True
        or all_created != set(terminal.get("created_entity_ids") or [])
        or all_delivery != set(terminal.get("delivery_entity_ids") or [])
        or all_support != set(terminal.get("support_entity_ids") or [])
    ):
        return {}
    return contexts


def _freecad_delivery_terminal_attempts(
    delivery: Any,
    attempt_ledger: Any,
) -> Optional[List[Dict[str, Any]]]:
    """Resolve terminal attempts from one digest-bound canonical ledger."""

    if (
        not isinstance(delivery, dict)
        or delivery.get("schema")
        != "bcs.text_representation_delivery/1.1"
        or set(delivery)
        != {
            "schema",
            "required",
            "requested_type",
            "verified",
            "attempt_count",
            "source_item_count",
            "delivered_item_count",
            "failed_item_count",
            "items",
            "invalid_reasons",
        }
        or any(
            duplicate_field in delivery
            for duplicate_field in (
                "terminal_attempts",
                "terminal_attempt_indexes",
                "source_item_ids",
            )
        )
        or not isinstance(attempt_ledger, list)
        or any(not isinstance(attempt, dict) for attempt in attempt_ledger)
        or type(delivery.get("attempt_count")) is not int
        or delivery.get("attempt_count") != len(attempt_ledger)
    ):
        return None
    resolution = resolve_text_representation_delivery(attempt_ledger, delivery)
    if resolution.get("verified") is not True:
        return None
    source_ids: List[str] = []
    terminal_index_by_source: Dict[str, int] = {}
    for index, attempt in enumerate(attempt_ledger):
        source_id = attempt.get("source_item_id")
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id != source_id.strip()
        ):
            return None
        if source_id not in terminal_index_by_source:
            source_ids.append(source_id)
        terminal_index_by_source[source_id] = index
    expected_indexes = [terminal_index_by_source[source_id] for source_id in source_ids]
    items = delivery.get("items")
    if (
        not isinstance(items, list)
        or len(items) != len(source_ids)
    ):
        return None
    terminals: List[Dict[str, Any]] = []
    for source_id, terminal_index, item in zip(
        source_ids,
        expected_indexes,
        items,
        strict=True,
    ):
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "source_item_id",
                "terminal_attempt_index",
                "final_type",
                "verified",
            }
            or item.get("source_item_id") != source_id
            or item.get("terminal_attempt_index") != terminal_index
        ):
            return None
        source_indexes = [
            index
            for index, attempt in enumerate(attempt_ledger)
            if attempt.get("source_item_id") == source_id
        ]
        for prior_index in source_indexes[:-1]:
            prior = attempt_ledger[prior_index]
            created_ids = prior.get("created_entity_ids")
            removed_ids = prior.get("removed_entity_ids")
            if (
                prior.get("outcome") != "proven_impossible"
                or prior.get("cleanup_complete") is not True
                or not isinstance(created_ids, list)
                or not isinstance(removed_ids, list)
                or set(created_ids) != set(removed_ids)
                or not isinstance(prior.get("proof"), dict)
                or prior["proof"].get("cleanup_complete") is not True
                or prior["proof"].get("source_item_id") != source_id
            ):
                return None
        terminal = attempt_ledger[terminal_index]
        final_type = (
            str(terminal.get("final_type") or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        attempted_type = (
            str(terminal.get("attempted_type") or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        terminal_verified = bool(
            terminal.get("outcome") == "verified"
            and terminal.get("cleanup_complete") is True
            and final_type
            and final_type == attempted_type
        )
        if (
            item.get("final_type") != final_type
            or item.get("verified") is not terminal_verified
            or terminal_verified is not True
        ):
            return None
        terminals.append(terminal)
    return terminals


def _freecad_delivery_inventory_binding_verified(
    delivery: Any,
    attempt_ledger: Any,
    inventory: Any,
    expected_pdf_sha256: Optional[str],
    page_source_observations: Any = None,
    page_visual_authority: Any = None,
) -> bool:
    """Bind each terminal text delivery to its exact persisted host object."""

    if (
        not isinstance(delivery, dict)
        or delivery.get("verified") is not True
    ):
        return False
    if not isinstance(inventory, dict) or inventory.get("verified") is not True:
        return False

    records = inventory.get("objects")
    terminals = _freecad_delivery_terminal_attempts(delivery, attempt_ledger)
    if not isinstance(records, list) or not isinstance(terminals, list):
        return False
    if not terminals:
        return bool(
            delivery.get("required") is False
            and delivery.get("source_item_count") == 0
            and attempt_ledger == []
        )

    records_by_id: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            return False
        entity_id = str(record.get("entity_id") or "").strip()
        if not entity_id or entity_id in records_by_id:
            return False
        records_by_id[entity_id] = record

    removed_entity_ids = [
        entity_id
        for attempt in attempt_ledger
        for entity_id in (
            attempt.get("removed_entity_ids", [])
            if isinstance(attempt.get("removed_entity_ids", []), list)
            else []
        )
    ]
    if (
        not isinstance(removed_entity_ids, list)
        or any(
            not isinstance(entity_id, str)
            or not entity_id
            or entity_id != entity_id.strip()
            for entity_id in removed_entity_ids
        )
        or len(removed_entity_ids) != len(set(removed_entity_ids))
        or any(entity_id in records_by_id for entity_id in removed_entity_ids)
    ):
        return False

    def normalize_representation(value: Any) -> str:
        return (
            str(value or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )

    archive_evidence = inventory.get("shape_archive_evidence")
    archive_entries_by_id = {
        entry.get("entity_id"): entry
        for entry in (
            archive_evidence.get("shape_entries", [])
            if isinstance(archive_evidence, dict)
            else []
        )
        if isinstance(entry, dict)
        and isinstance(entry.get("entity_id"), str)
        and entry.get("entity_id")
    }

    def archive_bound_shape_content(
        entity_id: str,
        content: Dict[str, Any],
        *,
        zero_visible_ink: bool,
    ) -> bool:
        entry = archive_entries_by_id.get(entity_id)
        return bool(
            isinstance(archive_evidence, dict)
            and isinstance(entry, dict)
            and entry.get("zero_visible_ink") is zero_visible_ink
            and _freecad_content_bound_to_archive_evidence(
                content,
                archive_evidence,
                entry,
            )
        )

    def zero_ink_shape_content(entity_id: str, content: Dict[str, Any]) -> bool:
        if not _freecad_zero_ink_shape_snapshot_verified(content):
            return False
        return bool(
            content.get("shape_snapshot_method") != _FREECAD_FCSTD_ARCHIVE_METHOD
            or archive_bound_shape_content(
                entity_id,
                content,
                zero_visible_ink=True,
            )
        )

    def zero_ink_shape_is_null_verified(
        entity_id: str,
        content: Dict[str, Any],
        expected_is_null: Any,
    ) -> bool:
        if type(expected_is_null) is not bool:
            return False
        if "shape_is_null" in content:
            return content.get("shape_is_null") is expected_is_null
        return bool(
            expected_is_null is True
            and content.get("shape_snapshot_method")
            == _FREECAD_FCSTD_ARCHIVE_METHOD
            and archive_bound_shape_content(
                entity_id,
                content,
                zero_visible_ink=True,
            )
        )

    def meaningful_shape_content(entity_id: str, content: Dict[str, Any]) -> bool:
        if content.get("shape_snapshot_method") == _FREECAD_FCSTD_ARCHIVE_METHOD:
            return archive_bound_shape_content(
                entity_id,
                content,
                zero_visible_ink=False,
            )
        topology = content.get("shape_topology_counts")
        digest = content.get("shape_digest")
        return bool(
            content.get("shape_nonempty") is True
            and content.get("shape_structure_verified") is True
            and isinstance(topology, dict)
            and any(type(count) is int and count > 0 for count in topology.values())
            and isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
            and _freecad_shape_fingerprint_verified(content)
        )

    representation_ids = {
        entity_id
        for entity_id, record in records_by_id.items()
        if (
            normalize_representation(record.get("representation"))
            in {"labels", "text", "3d_text", "glyphs", "geometry", "raster"}
            or bool(str(record.get("source_item_id") or ""))
            or bool(str(record.get("parent_source_item_id") or ""))
            or (record.get("content") or {}).get("proxy_type") in {"Label", "Text"}
        )
    }
    all_live_ids: set[str] = set()
    all_terminal_removed_ids: set[str] = set()

    for terminal in terminals:
        if not isinstance(terminal, dict) or terminal.get("outcome") != "verified":
            return False
        source_item_id = str(terminal.get("source_item_id") or "").strip()
        final_type = normalize_representation(terminal.get("final_type"))
        evidence = terminal.get("evidence")
        created_ids = terminal.get("created_entity_ids")
        delivery_ids = terminal.get("delivery_entity_ids")
        if delivery_ids is None:
            delivery_ids = created_ids
        support_ids = terminal.get("support_entity_ids", [])
        terminal_removed_ids = terminal.get("removed_entity_ids", [])
        if (
            not source_item_id
            or not final_type
            or not isinstance(created_ids, list)
            or not isinstance(delivery_ids, list)
            or not delivery_ids
            or not isinstance(support_ids, list)
            or not isinstance(terminal_removed_ids, list)
            or not isinstance(evidence, dict)
            or not evidence
        ):
            return False
        source_text = evidence.get("source_text")
        source_ink_evidence = evidence.get("source_ink_evidence")
        source_ink_verified = False
        expected_page_number = None
        expected_font_identity = None
        expected_font_asset_bindings = None
        expected_glyph_id_sequence = None
        if source_ink_evidence is not None:
            page_match = re.fullmatch(r"p([1-9][0-9]*):b[0-9]+:l[0-9]+:s[0-9]+", source_item_id)
            expected_page_number = int(page_match.group(1)) if page_match else None
            expected_font_identity = evidence.get("source_font_identity")
            expected_font_asset_bindings = evidence.get(
                "source_font_asset_bindings"
            )
            expected_glyph_id_sequence = evidence.get("source_glyph_id_sequence")
            if (
                not isinstance(source_text, str)
                or expected_page_number is None
                or not isinstance(expected_pdf_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_pdf_sha256) is None
                or evidence.get("source_pdf_sha256") != expected_pdf_sha256
                or evidence.get("source_page_number") != expected_page_number
                or not isinstance(expected_font_identity, dict)
                or not isinstance(expected_font_asset_bindings, list)
                or not isinstance(expected_glyph_id_sequence, list)
                or not _freecad_source_ink_evidence_verified(
                    source_ink_evidence,
                    source_item_id,
                    source_text,
                    expected_pdf_sha256=expected_pdf_sha256,
                    expected_page_number=expected_page_number,
                    expected_font_identity=expected_font_identity,
                    expected_font_asset_bindings=expected_font_asset_bindings,
                    expected_glyph_id_sequence=expected_glyph_id_sequence,
                )
            ):
                return False
            source_ink_verified = True
        zero_ink_terminal = bool(
            source_ink_verified
            and source_ink_evidence.get("classification") == "zero_visible_ink"
        )
        segment_contexts: Optional[Dict[str, Dict[str, Any]]] = None
        if (
            evidence.get("source_segment_manifest") is not None
            or evidence.get("segment_deliveries") is not None
        ):
            if not source_ink_verified:
                return False
            segment_contexts = _freecad_segment_delivery_contexts(
                terminal,
                expected_pdf_sha256=expected_pdf_sha256,
                expected_page_number=expected_page_number,
            )
            if not segment_contexts:
                return False
        page_scope_match = re.fullmatch(r"p([1-9][0-9]*):page", source_item_id)
        if page_scope_match is not None:
            requested_type = normalize_representation(terminal.get("requested_type"))
            page_number = int(page_scope_match.group(1))
            if (
                final_type != "raster"
                or not isinstance(expected_pdf_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_pdf_sha256) is None
                or evidence.get("pdf_sha256") != expected_pdf_sha256
            ):
                return False
            if requested_type != "raster":
                page_proof = evidence.get("page_visual_fallback_proof")
                observation = (
                    page_source_observations.get(source_item_id)
                    if isinstance(page_source_observations, dict)
                    else None
                )
                attempted_type = normalize_representation(
                    page_proof.get("attempted_type")
                    if isinstance(page_proof, dict)
                    else ""
                )
                if (
                    evidence.get("source_pdf_sha256") != expected_pdf_sha256
                    or evidence.get("source_page_number") != page_number
                    or evidence.get("page_visual_scope_id") != source_item_id
                    or not isinstance(observation, dict)
                    or observation.get("importer_identity")
                    != "bluecollarsystems.freecad.pdf_vector_importer"
                    or observation.get("pdf_sha256") != expected_pdf_sha256
                    or observation.get("page_number") != page_number
                    or observation.get("source_item_id") != source_item_id
                    or not page_visual_source_observation_v2_verified(
                        observation,
                        page_visual_authority,
                    )
                    or not page_visual_fallback_proof_v2_verified(
                        page_proof,
                        observation=observation,
                        authority=page_visual_authority,
                        expected_requested_type=requested_type,
                        expected_attempted_type=attempted_type,
                    )
                ):
                    return False

        id_lists = (created_ids, delivery_ids, support_ids, terminal_removed_ids)
        if any(
            any(
                not isinstance(entity_id, str)
                or not entity_id
                or entity_id != entity_id.strip()
                for entity_id in values
            )
            or len(values) != len(set(values))
            for values in id_lists
        ):
            return False
        created_set = set(created_ids)
        delivery_set = set(delivery_ids)
        support_set = set(support_ids)
        terminal_removed_set = set(terminal_removed_ids)
        live_set = created_set.difference(terminal_removed_set)
        if (
            not terminal_removed_set.issubset(created_set)
            or delivery_set.intersection(support_set)
            or delivery_set.union(support_set) != live_set
            or all_live_ids.intersection(live_set)
            or all_terminal_removed_ids.intersection(terminal_removed_set)
            or live_set.intersection(set(removed_entity_ids))
        ):
            return False
        all_live_ids.update(live_set)
        all_terminal_removed_ids.update(terminal_removed_set)

        reported_delivery_count = terminal.get("delivery_count")
        if "delivery_count" in terminal and (
            type(reported_delivery_count) is not int
            or reported_delivery_count <= 0
            or (
                final_type != "geometry"
                and reported_delivery_count != len(delivery_set)
            )
        ):
            return False

        live_records: Dict[str, Dict[str, Any]] = {}
        for raw_entity_id in list(delivery_ids) + list(support_ids):
            entity_id = str(raw_entity_id or "").strip()
            record = records_by_id.get(entity_id)
            if record is None:
                return False
            live_records[entity_id] = record
            representation = normalize_representation(record.get("representation"))
            if representation != final_type:
                return False
            record_source_id = str(record.get("source_item_id") or "")
            record_parent_source_id = str(
                record.get("parent_source_item_id") or ""
            )
            record_context = (
                segment_contexts.get(entity_id)
                if segment_contexts is not None
                else None
            )
            if segment_contexts is not None:
                if (
                    record_context is None
                    or record_source_id != record_context["source_item_id"]
                    or record_parent_source_id != source_item_id
                ):
                    return False
            elif final_type in {"glyphs", "geometry"}:
                if zero_ink_terminal:
                    if (
                        record_source_id != source_item_id
                        or record_parent_source_id != ""
                    ):
                        return False
                else:
                    child_source_ids = evidence.get("child_source_item_ids")
                    if (
                        not isinstance(child_source_ids, list)
                        or record_source_id not in child_source_ids
                        or record_parent_source_id != source_item_id
                    ):
                        return False
            elif (
                record_source_id != source_item_id
                or record_parent_source_id != ""
            ):
                return False
            record_evidence = (
                record_context["evidence"]
                if record_context is not None
                else evidence
            )
            record_source_text = (
                record_context["source_text"]
                if record_context is not None
                else source_text
            )
            record_source_ink_evidence = (
                record_context["source_ink_evidence"]
                if record_context is not None
                else source_ink_evidence
            )
            record_source_ink_verified = bool(
                record_context is not None or source_ink_verified
            )
            record_zero_ink_terminal = (
                record_context["zero_visible_ink"]
                if record_context is not None
                else zero_ink_terminal
            )
            record_font_identity = (
                record_context["font_identity"]
                if record_context is not None
                else expected_font_identity
            )
            record_font_asset_bindings = (
                record_context["font_asset_bindings"]
                if record_context is not None
                else expected_font_asset_bindings
            )
            record_glyph_id_sequence = (
                record_context["glyph_id_sequence"]
                if record_context is not None
                else expected_glyph_id_sequence
            )
            category = str(record.get("category") or "")
            type_id = str(record.get("type_id") or "")
            content = record.get("content")
            if not isinstance(content, dict):
                return False
            if record_source_ink_verified and not _freecad_source_ink_inventory_binding_verified(
                record_evidence,
                content,
                record_source_id,
                record_source_text,
                expected_pdf_sha256=expected_pdf_sha256,
                expected_page_number=expected_page_number,
                expected_font_identity=record_font_identity,
                expected_font_asset_bindings=record_font_asset_bindings,
                expected_glyph_id_sequence=record_glyph_id_sequence,
            ):
                return False
            if (
                final_type == "3d_text"
                and not record_zero_ink_terminal
                and not _freecad_font_delivery_inventory_binding_verified(
                    record_evidence,
                    content,
                )
            ):
                return False
            if final_type == "raster":
                if (
                    category != "images"
                    or not type_id.startswith("Image::")
                    or not isinstance(content.get("image_file"), str)
                    or not content.get("image_file")
                    or type(content.get("image_bytes")) is not int
                    or content.get("image_bytes") <= 0
                    or not isinstance(content.get("image_sha256"), str)
                    or re.fullmatch(
                        r"[0-9a-f]{64}", content.get("image_sha256")
                    )
                    is None
                    or evidence.get("raster_content_verified") is not True
                    or evidence.get("source_asset_sha256")
                    != content.get("image_sha256")
                    or (
                        page_scope_match is not None
                        and (
                            content.get("pdf_source_sha256")
                            != expected_pdf_sha256
                            or content.get("declared_raster_sha256")
                            != content.get("image_sha256")
                        )
                    )
                ):
                    return False
            else:
                if category != "text_representation_objects":
                    return False
                if final_type in {"labels", "text"}:
                    expected_proxy = "Label" if final_type == "labels" else "Text"
                    if (
                        type_id != "App::FeaturePython"
                        or not isinstance(record_source_text, str)
                        or not record_source_text
                        or record_evidence.get("source_text_preserved") is not True
                        or record_evidence.get("view_style_verified") is not True
                        or content.get("proxy_type") != expected_proxy
                        or content.get("text") != [record_source_text]
                        or (
                            final_type == "labels"
                            and content.get("custom_text") != [record_source_text]
                        )
                        or not isinstance(content.get("view_style"), dict)
                        or content["view_style"].get("view_present") is not True
                        or (
                            record_zero_ink_terminal
                            and (
                                record_evidence.get("physical_visibility") is not False
                                or content.get("text_visibility") is not False
                                or content["view_style"].get("visibility") is not False
                            )
                        )
                        or (
                            record_source_ink_evidence is not None
                            and not _freecad_source_ink_inventory_binding_verified(
                                record_evidence,
                                content,
                                record_source_id,
                                record_source_text,
                                expected_pdf_sha256=expected_pdf_sha256,
                                expected_page_number=expected_page_number,
                                expected_font_identity=record_font_identity,
                                expected_font_asset_bindings=record_font_asset_bindings,
                                expected_glyph_id_sequence=record_glyph_id_sequence,
                            )
                        )
                    ):
                        return False
                if final_type in {"3d_text", "glyphs", "geometry"}:
                    if not type_id.startswith(("Part::", "PartDesign::", "Sketcher::")):
                        return False
                    if record_zero_ink_terminal:
                        exact_source_content = (
                            content.get("string") == [record_source_text]
                            if final_type == "3d_text"
                            else content.get("source_text") == record_source_text
                        )
                        if (
                            not exact_source_content
                            or (
                                final_type == "3d_text"
                                and (
                                    record_evidence.get("physical_visibility") is not False
                                    or content.get("text_visibility") is not False
                                )
                            )
                            or not zero_ink_shape_content(entity_id, content)
                            or not _freecad_source_ink_inventory_binding_verified(
                                record_evidence,
                                content,
                                record_source_id,
                                record_source_text,
                                expected_pdf_sha256=expected_pdf_sha256,
                                expected_page_number=expected_page_number,
                                expected_font_identity=record_font_identity,
                                expected_font_asset_bindings=record_font_asset_bindings,
                                expected_glyph_id_sequence=record_glyph_id_sequence,
                            )
                        ):
                            return False
                    elif not meaningful_shape_content(entity_id, content):
                        return False

        if segment_contexts is not None:
            if {
                str(record.get("source_item_id") or "")
                for record in live_records.values()
            } != set(evidence.get("child_source_item_ids") or []):
                return False
        elif final_type in {"glyphs", "geometry"} and not zero_ink_terminal:
            child_source_ids = evidence.get("child_source_item_ids")
            if (
                not isinstance(child_source_ids, list)
                or not child_source_ids
                or any(
                    not isinstance(child_id, str)
                    or not child_id
                    or child_id != child_id.strip()
                    for child_id in child_source_ids
                )
                or len(child_source_ids) != len(set(child_source_ids))
                or len(child_source_ids) != len(live_set)
                or {
                    str(record.get("source_item_id") or "")
                    for record in live_records.values()
                }
                != set(child_source_ids)
            ):
                return False
        if (
            segment_contexts is None
            and final_type in {"glyphs", "geometry"}
            and zero_ink_terminal
        ):
            zero_content = (
                live_records[delivery_ids[0]].get("content") or {}
                if len(delivery_ids) == 1 and delivery_ids[0] in live_records
                else {}
            )
            if (
                len(delivery_ids) != 1
                or support_ids
                or not isinstance(source_text, str)
                or not source_text
                or evidence.get("source_text_preserved") is not True
                or any(
                    evidence.get(count_name) != 0
                    for count_name in (
                        "vertex_count",
                        "edge_count",
                        "face_count",
                        "solid_count",
                    )
                )
                or isinstance(evidence.get("volume"), bool)
                or not isinstance(evidence.get("volume"), (int, float))
                or not math.isfinite(float(evidence.get("volume")))
                or abs(float(evidence.get("volume"))) > 1e-12
                or not zero_ink_shape_is_null_verified(
                    delivery_ids[0],
                    zero_content,
                    evidence.get("shape_is_null"),
                )
                or evidence.get("zero_visible_ink_verified") is not True
            ):
                return False

        if final_type == "3d_text":
            if segment_contexts is None:
                delivery_groups = [
                    {
                        "source_text": source_text,
                        "evidence": evidence,
                        "zero_visible_ink": zero_ink_terminal,
                        "delivery_entity_ids": list(delivery_ids),
                        "support_entity_ids": list(support_ids),
                    }
                ]
            else:
                contexts_by_child = {
                    context["source_item_id"]: context
                    for context in segment_contexts.values()
                }
                child_order = evidence.get("child_source_item_ids") or []
                if set(contexts_by_child) != set(child_order):
                    return False
                delivery_groups = [
                    contexts_by_child[child_source_item_id]
                    for child_source_item_id in child_order
                ]

            for group_context in delivery_groups:
                group_delivery_ids = group_context["delivery_entity_ids"]
                group_support_ids = group_context["support_entity_ids"]
                group_delivery_records = [
                    live_records[entity_id] for entity_id in group_delivery_ids
                ]
                group_support_records = [
                    live_records[entity_id] for entity_id in group_support_ids
                ]
                group_source_text = group_context["source_text"]
                group_evidence = group_context["evidence"]
                group_zero_ink = group_context["zero_visible_ink"]
                if not group_zero_ink:
                    if (
                        len(group_delivery_records) != 1
                        or len(group_support_records) < 2
                        or not isinstance(group_source_text, str)
                        or not group_source_text
                        or group_evidence.get("source_text_preserved") is not True
                        or type(group_evidence.get("solid_count")) is not int
                        or group_evidence.get("solid_count") <= 0
                        or isinstance(group_evidence.get("volume"), bool)
                        or not isinstance(group_evidence.get("volume"), (int, float))
                        or not math.isfinite(float(group_evidence.get("volume")))
                        or float(group_evidence.get("volume")) <= 0.0
                    ):
                        return False
                    extrusion_record = group_delivery_records[0]
                    extrusion_content = extrusion_record.get("content") or {}
                    group_support_set = set(group_support_ids)
                    if (
                        extrusion_record.get("type_id") != "Part::Extrusion"
                        or extrusion_content.get("base_entity_id")
                        not in group_support_set
                    ):
                        return False
                    shape_string_records = [
                        record
                        for record in group_support_records
                        if record.get("entity_id")
                        != extrusion_content.get("base_entity_id")
                        and str(record.get("type_id") or "").startswith(
                            ("Part::", "PartDesign::")
                        )
                        and (record.get("content") or {}).get("string")
                        == [group_source_text]
                    ]
                    calibrated_support_records = [
                        record
                        for record in group_support_records
                        if record.get("entity_id")
                        == extrusion_content.get("base_entity_id")
                        and str(record.get("type_id") or "").startswith(
                            ("Part::", "PartDesign::")
                        )
                    ]
                    if (
                        len(shape_string_records) != 1
                        or len(calibrated_support_records) != 1
                    ):
                        return False
                    continue

                zero_record = (
                    group_delivery_records[0]
                    if len(group_delivery_records) == 1
                    else {}
                )
                zero_content = zero_record.get("content") or {}
                if (
                    len(group_delivery_records) != 1
                    or group_support_records
                    or not isinstance(group_source_text, str)
                    or not group_source_text
                    or group_evidence.get("source_text_preserved") is not True
                    or zero_content.get("string") != [group_source_text]
                    or not zero_ink_shape_content(
                        group_delivery_ids[0],
                        zero_content,
                    )
                    or any(
                        group_evidence.get(count_name) != 0
                        for count_name in (
                            "vertex_count",
                            "edge_count",
                            "face_count",
                            "solid_count",
                        )
                    )
                    or isinstance(group_evidence.get("volume"), bool)
                    or not isinstance(group_evidence.get("volume"), (int, float))
                    or not math.isfinite(float(group_evidence.get("volume")))
                    or abs(float(group_evidence.get("volume"))) > 1e-12
                    or not zero_ink_shape_is_null_verified(
                        group_delivery_ids[0],
                        zero_content,
                        group_evidence.get("shape_is_null"),
                    )
                    or group_evidence.get("zero_visible_ink_verified") is not True
                ):
                    return False

    return bool(
        all_live_ids == representation_ids
        and all_terminal_removed_ids.issubset(set(removed_entity_ids))
    )


def _freecad_fcstd_archive_evidence_digest(evidence: Any) -> Optional[str]:
    if not isinstance(evidence, dict):
        return None
    payload = {
        key: value for key, value in evidence.items() if key != "evidence_digest"
    }
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _freecad_host_inventory_digest(inventory: Any) -> Optional[str]:
    if not isinstance(inventory, dict):
        return None
    payload = {
        key: value for key, value in inventory.items() if key != "inventory_digest"
    }
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return None
    return hashlib.sha256(encoded).hexdigest()


def _freecad_fcstd_archive_evidence_verified(
    evidence: Any,
    required_shape_ids: set,
    required_zero_ink_shape_ids: Optional[set] = None,
) -> bool:
    if required_zero_ink_shape_ids is None:
        required_zero_ink_shape_ids = set()
    if (
        not isinstance(evidence, dict)
        or not isinstance(required_shape_ids, set)
        or not isinstance(required_zero_ink_shape_ids, set)
        or required_shape_ids.intersection(required_zero_ink_shape_ids)
    ):
        return False
    required_all_shape_ids = required_shape_ids.union(required_zero_ink_shape_ids)
    digest_pattern = re.compile(r"[0-9a-f]{64}")
    entries = evidence.get("shape_entries")
    integer_minimums = {
        "fcstd_bytes": 1,
        "document_xml_bytes": 1,
        "archive_entry_count": 1,
        "document_object_count": 0,
        "shape_mapping_count": 0,
        "expected_shape_count": 0,
        "expected_nonempty_shape_count": 0,
        "expected_zero_ink_shape_count": 0,
    }
    if (
        evidence.get("schema") != _FREECAD_FCSTD_ARCHIVE_SCHEMA
        or evidence.get("method") != _FREECAD_FCSTD_ARCHIVE_METHOD
        or evidence.get("verified") is not True
        or any(
            type(evidence.get(field)) is not int
            or evidence.get(field) < minimum
            for field, minimum in integer_minimums.items()
        )
        or not isinstance(entries, list)
        or evidence.get("expected_shape_count") != len(required_all_shape_ids)
        or evidence.get("expected_nonempty_shape_count")
        != len(required_shape_ids)
        or evidence.get("expected_zero_ink_shape_count")
        != len(required_zero_ink_shape_ids)
        or evidence.get("shape_mapping_count") < len(required_all_shape_ids)
        or evidence.get("archive_entry_count")
        < evidence.get("shape_mapping_count") + 1
        or evidence.get("document_object_count")
        < evidence.get("shape_mapping_count")
        or not all(
            isinstance(evidence.get(field), str)
            and digest_pattern.fullmatch(evidence.get(field)) is not None
            for field in (
                "fcstd_sha256",
                "document_xml_sha256",
                "evidence_digest",
            )
        )
        or evidence.get("evidence_digest")
        != _freecad_fcstd_archive_evidence_digest(evidence)
    ):
        return False
    entries_by_id: Dict[str, Dict[str, Any]] = {}
    entry_names = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return False
        entity_id = entry.get("entity_id")
        entry_name = entry.get("entry_name")
        zero_visible_ink = entry.get("zero_visible_ink")
        if (
            not isinstance(entity_id, str)
            or not entity_id
            or entity_id in entries_by_id
            or not _freecad_archive_entry_name_valid(entry_name)
            or entry_name.casefold() in entry_names
            or not isinstance(entry.get("sha256"), str)
            or digest_pattern.fullmatch(entry.get("sha256")) is None
            or type(entry.get("bytes")) is not int
            or entry.get("bytes") < 0
            or type(entry.get("compression_method")) is not int
            or entry.get("compression_method") not in {0, 8}
            or type(entry.get("crc32")) is not int
            or not 0 <= entry.get("crc32") <= 0xFFFFFFFF
            or type(zero_visible_ink) is not bool
            or (
                zero_visible_ink
                and (
                    entity_id not in required_zero_ink_shape_ids
                    or entry.get("bytes") != 0
                    or entry.get("sha256") != hashlib.sha256(b"").hexdigest()
                    or entry.get("crc32") != 0
                )
            )
            or (
                not zero_visible_ink
                and (
                    entity_id not in required_shape_ids
                    or entry.get("bytes") <= 0
                )
            )
        ):
            return False
        entries_by_id[entity_id] = entry
        entry_names.add(entry_name.casefold())
    return set(entries_by_id) == required_all_shape_ids


def _freecad_content_bound_to_archive_evidence(
    content: Any,
    evidence: Dict[str, Any],
    entry: Dict[str, Any],
) -> bool:
    zero_visible_ink = entry.get("zero_visible_ink") is True
    if content.get("shape_snapshot_method") == _FREECAD_FCSTD_ARCHIVE_METHOD:
        return bool(
            (
                _freecad_zero_ink_shape_snapshot_verified(content)
                if zero_visible_ink
                else _freecad_fcstd_archive_shape_snapshot_verified(content)
            )
            and content.get("shape_archive_schema") == evidence.get("schema")
            and (
                content.get("shape_digest") == ""
                if zero_visible_ink
                else content.get("shape_digest") == entry.get("sha256")
            )
        )
    if zero_visible_ink:
        if not _freecad_zero_ink_shape_snapshot_verified(content):
            return False
    elif not _freecad_fcstd_archive_shape_snapshot_verified(content):
        return False
    bindings = {
        "shape_archive_evidence_digest": evidence.get("evidence_digest"),
        "shape_archive_fcstd_sha256": evidence.get("fcstd_sha256"),
        "shape_archive_fcstd_bytes": evidence.get("fcstd_bytes"),
        "shape_archive_document_xml_sha256": evidence.get("document_xml_sha256"),
        "shape_archive_document_xml_bytes": evidence.get("document_xml_bytes"),
        "shape_archive_entry_count": evidence.get("archive_entry_count"),
        "shape_archive_mapping_count": evidence.get("shape_mapping_count"),
        "shape_archive_expected_shape_count": evidence.get(
            "expected_shape_count"
        ),
        "shape_archive_expected_nonempty_shape_count": evidence.get(
            "expected_nonempty_shape_count"
        ),
        "shape_archive_expected_zero_ink_shape_count": evidence.get(
            "expected_zero_ink_shape_count"
        ),
        "shape_archive_entry": entry.get("entry_name"),
        "shape_archive_sha256": entry.get("sha256"),
        "shape_archive_bytes": entry.get("bytes"),
        "shape_archive_compression_method": entry.get("compression_method"),
        "shape_archive_crc32": entry.get("crc32"),
        "shape_archive_zero_visible_ink": zero_visible_ink,
    }
    return all(content.get(field) == value for field, value in bindings.items())


def _freecad_archive_phase_timings_verified(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    required = {
        "save_ms",
        "archive_hash_ms",
        "open_ms",
        "cheap_inventory_ms",
    }
    return bool(
        required.issubset(value)
        and all(
            _freecad_finite_number(value.get(field)) is not None
            and _freecad_finite_number(value.get(field)) >= 0.0
            for field in required
        )
    )


def _freecad_compact_save_reopen_inventory_verified(
    inventory: Dict[str, Any],
    save_reopen: Dict[str, Any],
) -> bool:
    records = inventory.get("objects")
    archive_evidence = inventory.get("shape_archive_evidence")
    inventory_digest = inventory.get("inventory_digest")
    if (
        not isinstance(records, list)
        or not isinstance(archive_evidence, dict)
        or not isinstance(inventory_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", inventory_digest) is None
        or inventory_digest != _freecad_host_inventory_digest(inventory)
    ):
        return False
    required_shape_ids = {
        record.get("entity_id")
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("content"), dict)
        and str(record.get("type_id") or "").startswith(
            ("Part::", "PartDesign::", "Sketcher::")
        )
        and record["content"].get("shape_nonempty") is True
    }
    required_zero_ink_shape_ids = {
        record.get("entity_id")
        for record in records
        if isinstance(record, dict)
        and isinstance(record.get("content"), dict)
        and str(record.get("type_id") or "").startswith(
            ("Part::", "PartDesign::", "Sketcher::")
        )
        and _freecad_zero_ink_shape_snapshot_verified(record["content"])
    }
    if not _freecad_fcstd_archive_evidence_verified(
        archive_evidence,
        required_shape_ids,
        required_zero_ink_shape_ids,
    ):
        return False
    entries_by_id = {
        entry.get("entity_id"): entry
        for entry in archive_evidence.get("shape_entries", [])
        if isinstance(entry, dict)
    }
    records_by_id = {
        record.get("entity_id"): record
        for record in records
        if isinstance(record, dict)
    }
    required_all_ids = required_shape_ids.union(required_zero_ink_shape_ids)
    if not all(
        entity_id in records_by_id
        and entity_id in entries_by_id
        and _freecad_content_bound_to_archive_evidence(
            records_by_id[entity_id].get("content"),
            archive_evidence,
            entries_by_id[entity_id],
        )
        for entity_id in required_all_ids
    ):
        return False
    forbidden_duplicate_fields = {
        "expected_objects",
        "actual_objects",
        "geometry_comparisons",
        "shape_archive_evidence",
        "expected_entity_ids",
    }
    discrepancy_fields = (
        "missing_entity_ids",
        "duplicate_actual_entity_ids",
        "unexpected_entity_ids",
        "mismatched_entities",
    )
    return bool(
        save_reopen.get("schema")
        == "bcs.freecad_save_reopen_inventory/1.0"
        and save_reopen.get("method")
        == "temporary_fcstd_save_copy_archive_reopen"
        and save_reopen.get("required") is True
        and save_reopen.get("verified") is True
        and save_reopen.get("archive_unchanged_after_open") is True
        and _freecad_archive_phase_timings_verified(
            save_reopen.get("phase_timings_ms")
        )
        and not forbidden_duplicate_fields.intersection(save_reopen)
        and all(
            isinstance(save_reopen.get(field), list)
            and not save_reopen.get(field)
            for field in discrepancy_fields
        )
        and save_reopen.get("inventory_digest") == inventory_digest
        and save_reopen.get("reopened_inventory_digest") == inventory_digest
        and save_reopen.get("shape_archive_evidence_digest")
        == archive_evidence.get("evidence_digest")
        and save_reopen.get("expected_counts") == inventory.get("counts")
        and save_reopen.get("actual_counts") == inventory.get("counts")
        and save_reopen.get("counts_match") is True
    )


def _freecad_save_reopen_inventory_verified(
    inventory: Any,
    save_reopen: Any,
) -> bool:
    """Reject self-contradictory persistence evidence even when marked verified."""

    if not isinstance(inventory, dict) or inventory.get("verified") is not True:
        return False
    if not isinstance(save_reopen, dict) or save_reopen.get("verified") is not True:
        return False
    if save_reopen.get("schema") == "bcs.freecad_save_reopen_inventory/1.0":
        return _freecad_compact_save_reopen_inventory_verified(
            inventory,
            save_reopen,
        )

    inventory_ids = inventory.get("entity_ids")
    inventory_counts = inventory.get("counts")
    inventory_objects = inventory.get("objects")
    if (
        not isinstance(inventory_ids, list)
        or not inventory_ids
        or any(not isinstance(entity_id, str) or not entity_id for entity_id in inventory_ids)
        or len(inventory_ids) != len(set(inventory_ids))
        or not isinstance(inventory_counts, dict)
        or not isinstance(inventory_objects, list)
        or len(inventory_objects) != len(inventory_ids)
    ):
        return False

    for evidence_field in (
        "missing_entity_ids",
        "duplicate_actual_entity_ids",
        "unexpected_entity_ids",
        "mismatched_entities",
    ):
        if not isinstance(save_reopen.get(evidence_field), list) or save_reopen.get(
            evidence_field
        ):
            return False

    expected_objects = save_reopen.get("expected_objects")
    actual_objects = save_reopen.get("actual_objects")
    geometry_comparisons = save_reopen.get("geometry_comparisons")
    if (
        expected_objects != inventory_objects
        or not isinstance(actual_objects, list)
        or not isinstance(geometry_comparisons, list)
    ):
        return False
    expected_by_id = {
        record.get("entity_id"): record
        for record in expected_objects
        if isinstance(record, dict) and isinstance(record.get("entity_id"), str)
    }
    actual_by_id = {
        record.get("entity_id"): record
        for record in actual_objects
        if isinstance(record, dict) and isinstance(record.get("entity_id"), str)
    }
    comparison_by_id = {
        certificate.get("entity_id"): certificate
        for certificate in geometry_comparisons
        if isinstance(certificate, dict)
        and isinstance(certificate.get("entity_id"), str)
    }
    shape_ids = {
        entity_id
        for entity_id, record in expected_by_id.items()
        if isinstance(record.get("content"), dict)
        and str(record.get("type_id") or "").startswith(
            ("Part::", "PartDesign::", "Sketcher::")
        )
        and record["content"].get("shape_nonempty") is True
    }
    zero_ink_shape_ids = {
        entity_id
        for entity_id, record in expected_by_id.items()
        if isinstance(record.get("content"), dict)
        and str(record.get("type_id") or "").startswith(
            ("Part::", "PartDesign::", "Sketcher::")
        )
        and _freecad_zero_ink_shape_snapshot_verified(record["content"])
    }
    shape_evidence_mode = inventory.get("shape_evidence_mode", "sampled")
    shape_proofs_valid = False
    if shape_evidence_mode == "cheap":
        inventory_archive = inventory.get("shape_archive_evidence")
        save_archive = save_reopen.get("shape_archive_evidence")
        entries_by_id = {
            entry.get("entity_id"): entry
            for entry in (
                inventory_archive.get("shape_entries", [])
                if isinstance(inventory_archive, dict)
                else []
            )
            if isinstance(entry, dict)
        }
        shape_proofs_valid = bool(
            save_reopen.get("method")
            == "temporary_fcstd_save_copy_archive_reopen"
            and save_reopen.get("archive_unchanged_after_open") is True
            and _freecad_archive_phase_timings_verified(
                save_reopen.get("phase_timings_ms")
            )
            and inventory_archive == save_archive
            and _freecad_fcstd_archive_evidence_verified(
                inventory_archive,
                set(shape_ids),
                set(zero_ink_shape_ids),
            )
            and set(entries_by_id) == set(shape_ids).union(zero_ink_shape_ids)
            and all(
                _freecad_content_bound_to_archive_evidence(
                    expected_by_id[entity_id].get("content"),
                    inventory_archive,
                    entries_by_id[entity_id],
                )
                and _freecad_content_bound_to_archive_evidence(
                    actual_by_id.get(entity_id, {}).get("content"),
                    inventory_archive,
                    entries_by_id[entity_id],
                )
                for entity_id in set(shape_ids).union(zero_ink_shape_ids)
            )
        )
    elif shape_evidence_mode == "sampled":
        shape_proofs_valid = all(
            _freecad_shape_fingerprint_verified(
                expected_by_id[entity_id].get("content")
            )
            and _freecad_shape_fingerprint_verified(
                actual_by_id.get(entity_id, {}).get("content")
            )
            for entity_id in shape_ids
        )
    objects_match = bool(
        len(expected_by_id) == len(expected_objects)
        and len(actual_by_id) == len(actual_objects)
        and len(comparison_by_id) == len(geometry_comparisons)
        and set(expected_by_id) == set(actual_by_id)
        and set(comparison_by_id) == shape_ids
        and shape_proofs_valid
        and all(
            _freecad_host_record_equivalent(
                expected_by_id[entity_id],
                actual_by_id[entity_id],
                comparison_by_id.get(entity_id),
            )
            for entity_id in expected_by_id
        )
    )

    return bool(
        save_reopen.get("expected_entity_ids") == inventory_ids
        and save_reopen.get("expected_counts") == inventory_counts
        and save_reopen.get("actual_counts") == inventory_counts
        and save_reopen.get("counts_match") is True
        and objects_match
    )


def _freecad_host_inventory_verified(inventory: Any, result: Any) -> bool:
    """Recompute inventory identities, categories, types, and result counts."""

    if (
        not isinstance(inventory, dict)
        or inventory.get("verified") is not True
        or inventory.get("schema") != "bcs.freecad_host_object_inventory/1.1"
        or not isinstance(result, dict)
    ):
        return False
    records = inventory.get("objects")
    entity_ids = inventory.get("entity_ids")
    categories = inventory.get("categories")
    counts = inventory.get("counts")
    type_counts = inventory.get("type_counts")
    category_names = (
        "containers",
        "images",
        "vector_primitives",
        "text_representation_objects",
        "unclassified",
    )
    if (
        not isinstance(records, list)
        or not records
        or not isinstance(entity_ids, list)
        or not isinstance(categories, dict)
        or not isinstance(counts, dict)
        or not isinstance(type_counts, dict)
    ):
        return False

    derived_ids: List[str] = []
    derived_categories = {name: [] for name in category_names}
    derived_type_counts: Dict[str, int] = {}
    shape_evidence_mode = inventory.get("shape_evidence_mode", "sampled")
    if shape_evidence_mode not in {"sampled", "cheap"}:
        return False
    required_shape_ids = set()
    required_zero_ink_shape_ids = set()
    for record in records:
        if not isinstance(record, dict):
            return False
        entity_id = record.get("entity_id")
        type_id = record.get("type_id")
        category = record.get("category")
        representation = record.get("representation")
        source_item_id = record.get("source_item_id")
        parent_source_item_id = record.get("parent_source_item_id")
        content = record.get("content")
        if (
            not isinstance(entity_id, str)
            or not entity_id
            or not isinstance(type_id, str)
            or not type_id
            or category not in derived_categories
            or not isinstance(representation, str)
            or not isinstance(source_item_id, str)
            or not isinstance(parent_source_item_id, str)
            or not isinstance(content, dict)
        ):
            return False
        if representation == "raster" and (
            category != "images"
            or not type_id.startswith("Image::")
            or not isinstance(content.get("image_file"), str)
            or not content.get("image_file")
            or type(content.get("image_bytes")) is not int
            or content.get("image_bytes") <= 0
            or not isinstance(content.get("image_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", content.get("image_sha256")) is None
        ):
            return False
        if representation in {"labels", "text", "3d_text", "glyphs", "geometry"} and (
            category != "text_representation_objects"
        ):
            return False
        if category == "containers" and not type_id.startswith(
            "App::DocumentObjectGroup"
        ):
            return False
        if category == "images" and not type_id.startswith("Image::"):
            return False
        if category == "vector_primitives" and (
            not type_id.startswith(("Part::", "PartDesign::", "Sketcher::"))
            or content.get("shape_nonempty") is not True
        ):
            return False
        if type_id.startswith(("Part::", "PartDesign::", "Sketcher::")) and (
            type(content.get("shape_nonempty")) is not bool
        ):
            return False
        if (
            type_id.startswith(("Part::", "PartDesign::", "Sketcher::"))
            and content.get("shape_nonempty") is True
        ):
            required_shape_ids.add(entity_id)
            topology = content.get("shape_topology_counts")
            archive_only = (
                content.get("shape_snapshot_method")
                == _FREECAD_FCSTD_ARCHIVE_METHOD
            )
            shape_proof_valid = (
                _freecad_shape_fingerprint_verified(content)
                if shape_evidence_mode == "sampled"
                else _freecad_fcstd_archive_shape_snapshot_verified(content)
            )
            if (
                content.get("shape_structure_verified") is not True
                or (
                    not archive_only
                    and (
                        not isinstance(topology, dict)
                        or not any(
                            type(count) is int and count > 0
                            for count in topology.values()
                        )
                    )
                )
                or not shape_proof_valid
            ):
                return False
        if representation in {"3d_text", "glyphs", "geometry"}:
            zero_ink_shape = _freecad_zero_ink_shape_snapshot_verified(content)
            if zero_ink_shape:
                if (
                    shape_evidence_mode == "cheap"
                    and type_id.startswith(
                        ("Part::", "PartDesign::", "Sketcher::")
                    )
                ):
                    required_zero_ink_shape_ids.add(entity_id)
                exact_source_present = (
                    isinstance(content.get("string"), list)
                    and len(content.get("string")) == 1
                    and isinstance(content["string"][0], str)
                    and bool(content["string"][0])
                    if representation == "3d_text"
                    else isinstance(content.get("source_text"), str)
                    and bool(content.get("source_text"))
                )
                if not exact_source_present:
                    return False
            else:
                topology = content.get("shape_topology_counts")
                archive_only = (
                    content.get("shape_snapshot_method")
                    == _FREECAD_FCSTD_ARCHIVE_METHOD
                )
                if (
                    content.get("shape_nonempty") is not True
                    or content.get("shape_structure_verified") is not True
                    or (
                        not archive_only
                        and (
                            not isinstance(topology, dict)
                            or not any(
                                type(count) is int and count > 0
                                for count in topology.values()
                            )
                        )
                    )
                    or not isinstance(content.get("shape_digest"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", content.get("shape_digest"))
                    is None
                    or not (
                        _freecad_shape_fingerprint_verified(content)
                        or _freecad_fcstd_archive_shape_snapshot_verified(content)
                    )
                ):
                    return False
        derived_ids.append(entity_id)
        derived_categories[category].append(entity_id)
        derived_type_counts[type_id] = derived_type_counts.get(type_id, 0) + 1

    if derived_ids != entity_ids or len(derived_ids) != len(set(derived_ids)):
        return False
    if categories != derived_categories or type_counts != derived_type_counts:
        return False
    derived_counts = {"total": len(records)}
    derived_counts.update(
        {name: len(derived_categories[name]) for name in category_names}
    )
    if counts != derived_counts:
        return False
    if shape_evidence_mode == "cheap":
        archive_evidence = inventory.get("shape_archive_evidence")
        if not _freecad_fcstd_archive_evidence_verified(
            archive_evidence,
            required_shape_ids,
            required_zero_ink_shape_ids,
        ):
            return False
        entries_by_id = {
            entry.get("entity_id"): entry
            for entry in archive_evidence.get("shape_entries", [])
            if isinstance(entry, dict)
        }
        records_by_id = {record.get("entity_id"): record for record in records}
        if not all(
            _freecad_content_bound_to_archive_evidence(
                records_by_id[entity_id].get("content"),
                archive_evidence,
                entries_by_id[entity_id],
            )
            for entity_id in required_shape_ids.union(
                required_zero_ink_shape_ids
            )
        ):
            return False
    return bool(
        type(result.get("primitives")) is int
        and result.get("primitives") == derived_counts["vector_primitives"]
        and type(result.get("images")) is int
        and result.get("images") == derived_counts["images"]
    )


def _freecad_actual_text_entity_types_verified(
    delivery: Any,
    attempt_ledger: Any,
    entity_types: Any,
    result: Any,
) -> bool:
    """Reconcile reported FreeCAD text buckets to exact terminal deliveries."""

    if not isinstance(delivery, dict) or delivery.get("verified") is not True:
        return False
    if not isinstance(entity_types, dict) or not isinstance(result, dict):
        return False

    bucket_by_type = {
        "labels": "native_label",
        "text": "native_text",
        "3d_text": "native_3d_text",
        "glyphs": "outline_curve_or_mesh",
        "geometry": "raw_geometry_edges",
        "raster": "raster_text_patch",
    }
    expected_buckets = {bucket: 0 for bucket in TEXT_ENTITY_DELIVERED_BUCKETS}
    terminal_types: List[str] = []
    terminals = _freecad_delivery_terminal_attempts(delivery, attempt_ledger)
    if not isinstance(terminals, list):
        return False
    if not terminals:
        return bool(
            delivery.get("required") is False
            and delivery.get("source_item_count") == 0
            and attempt_ledger == []
            and entity_types.get("delivery_counts_valid") is True
            and entity_types.get("entity_type") == "none"
            and entity_types.get("count") == 0
            and all(entity_types.get(bucket) == 0 for bucket in expected_buckets)
            and result.get("text_entities") == 0
        )

    for terminal in terminals:
        if not isinstance(terminal, dict) or terminal.get("outcome") != "verified":
            return False
        final_type = (
            str(terminal.get("final_type") or "")
            .strip()
            .lower()
            .replace("-", "_")
            .replace(" ", "_")
        )
        bucket = bucket_by_type.get(final_type)
        if bucket is None:
            return False

        delivery_ids = terminal.get("delivery_entity_ids")
        if delivery_ids is None:
            delivery_ids = terminal.get("created_entity_ids")
        if (
            not isinstance(delivery_ids, list)
            or not delivery_ids
            or any(not isinstance(entity_id, str) or not entity_id for entity_id in delivery_ids)
            or len(delivery_ids) != len(set(delivery_ids))
        ):
            return False
        reported_count = terminal.get("delivery_count")
        if "delivery_count" in terminal:
            if type(reported_count) is not int or reported_count <= 0:
                return False
            if final_type != "geometry" and reported_count != len(delivery_ids):
                return False
            terminal_count = reported_count
        else:
            terminal_count = len(delivery_ids)
        expected_buckets[bucket] += terminal_count
        terminal_types.append(final_type)

    unique_types = sorted(set(terminal_types))
    expected_entity_type = (
        unique_types[0] if len(unique_types) == 1 else "mixed"
    )
    expected_total = sum(expected_buckets.values())
    expected_structural = expected_total - expected_buckets["raster_text_patch"]

    if entity_types.get("delivery_counts_valid") is not True:
        return False
    actual_type = (
        str(entity_types.get("entity_type") or "")
        .strip()
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if actual_type != expected_entity_type:
        return False
    if type(entity_types.get("count")) is not int or entity_types.get("count") != expected_total:
        return False
    for bucket, expected_count in expected_buckets.items():
        if (
            bucket not in entity_types
            or type(entity_types.get(bucket)) is not int
            or entity_types.get(bucket) != expected_count
        ):
            return False
    return bool(
        type(result.get("text_entities")) is int
        and result.get("text_entities") == expected_structural
    )


_TEXT_DELIVERY_OBLIGATION_FIELDS = {
    "schema",
    "required",
    "requested_type",
    "source_item_ids",
}
_HOST_TEXT_FALLBACK_LADDERS = {
    "freecad": {
        "text": ("text", "labels", "3d_text", "glyphs", "geometry", "raster"),
        "labels": ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
        "3d_text": ("3d_text", "glyphs", "geometry", "text", "labels", "raster"),
        "glyphs": ("glyphs", "geometry", "3d_text", "text", "labels", "raster"),
        "geometry": ("geometry", "glyphs", "3d_text", "text", "labels", "raster"),
        "raster": ("raster",),
    },
    "blender": {
        "labels": ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
        "text": ("text", "3d_text", "glyphs", "geometry", "raster"),
        "3d_text": ("3d_text", "text", "glyphs", "geometry", "raster"),
        "glyphs": ("glyphs", "geometry", "raster"),
        "geometry": ("geometry", "glyphs", "raster"),
        "raster": ("raster",),
    },
    "librecad": {
        "text": ("text", "glyphs", "geometry", "raster"),
        "labels": ("labels", "text", "glyphs", "geometry", "raster"),
        "glyphs": ("glyphs", "geometry", "text", "raster"),
        "geometry": ("geometry", "glyphs", "text", "raster"),
        "3d_text": ("3d_text", "text", "glyphs", "geometry", "raster"),
        "raster": ("raster",),
    },
}
_FREECAD_CLOSED_SVG_IMPOSSIBILITY_REASONS = {
    "svg_renderer_unavailable",
    "svg_payload_too_large",
    "svg_has_no_glyph_placements",
    "svg_glyph_outlines_unavailable",
    "svg_item_glyph_bounds_unavailable",
}
_PAGE_VISUAL_IMPORTER_IDENTITIES = {
    "freecad": "bluecollarsystems.freecad.pdf_vector_importer",
    "blender": "bc_pdf_vector_importer.blender",
    "librecad": "bluecollarsystems.librecad.pdf_importer",
}
_HOST_RESULT_PERSISTENCE_METHODS = {
    "blender": "blender_post_commit_scene_reinspection_sha256",
    "librecad": "librecad_atomic_dxf_write_reopen_sha256",
}
_HOST_RESULT_PERSISTENCE_FIELDS = {
    "schema",
    "host_app",
    "importer_identity",
    "source_pdf_sha256",
    "import_session_id",
    "method",
    "commit_complete",
    "persistence_verified",
    "artifact_reinspection_complete",
    "persistence_sha256",
    "delivery_entity_ids_sha256",
    "observed_delivery_entity_ids_sha256",
}
TEXT_ENTITY_DELIVERED_BUCKETS = (
    "native_label",
    "native_text",
    "native_3d_text",
    "glyph_curve",
    "geometry_mesh",
    "raster_patch",
    "outline_curve_or_mesh",
    "raw_geometry_edges",
    "raster_text_patch",
    "dxf_text",
    "raster_image",
    "fallback_geometry",
)
_ACTUAL_TEXT_ENTITY_TYPE_FIELDS = {
    "entity_type",
    "count",
    "font_rendered",
    "examples",
    "delivery_counts_valid",
    *TEXT_ENTITY_DELIVERED_BUCKETS,
}
_BLENDER_BUCKET_BY_TYPE = {
    "labels": "native_label",
    "text": "native_text",
    "3d_text": "native_3d_text",
    "glyphs": "glyph_curve",
    "geometry": "geometry_mesh",
    "raster": "raster_patch",
}
_LIBRECAD_BUCKET_BY_TYPE = {
    "labels": "native_label",
    "text": "dxf_text",
    "3d_text": "native_3d_text",
    "glyphs": "outline_curve_or_mesh",
    "geometry": "raw_geometry_edges",
    "raster": "raster_image",
}


def _contract_exact_text(value: Any) -> bool:
    return bool(
        isinstance(value, str)
        and value
        and value == value.strip()
    )


def _contract_exact_string_list(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and all(_contract_exact_text(item) for item in value)
        and len(value) == len(set(value))
    )


def _scale_crosscheck_verified(extra: Dict[str, Any]) -> bool:
    if "resolved_scale" in extra:
        resolved_scale = extra.get("resolved_scale")
        if not isinstance(resolved_scale, dict) or not resolved_scale:
            return False
    if "scale_hints" in extra and not isinstance(extra.get("scale_hints"), dict):
        return False
    try:
        expected = build_scale_crosscheck(extra)
    except (TypeError, ValueError, OverflowError):
        return False
    if expected is None:
        return "scale_crosscheck" not in extra
    return bool(
        "scale_crosscheck" in extra
        and isinstance(extra.get("scale_crosscheck"), dict)
        and extra.get("scale_crosscheck") == expected
    )


def _page_visual_prior_verified(
    host_app: str,
    attempt: Dict[str, Any],
    expected_pdf_sha256: str,
    page_source_observations: Any,
    page_visual_authority: Any,
) -> Optional[bool]:
    source_item_id = attempt.get("source_item_id")
    if not isinstance(source_item_id, str):
        return None
    evidence = attempt.get("evidence")
    if host_app == "freecad":
        match = re.fullmatch(r"p([1-9][0-9]*):page", source_item_id)
        proof = attempt.get("proof")
    elif host_app in {"blender", "librecad"}:
        match = re.fullmatch(r"page_visual:([1-9][0-9]*)", source_item_id)
        proof = (
            evidence.get("page_visual_fallback_proof")
            if isinstance(evidence, dict)
            else None
        )
    else:
        return None
    if match is None:
        return None
    expected_importer_identity = _PAGE_VISUAL_IMPORTER_IDENTITIES.get(host_app)
    observation = (
        page_source_observations.get(source_item_id)
        if isinstance(page_source_observations, dict)
        else None
    )
    page_number = int(match.group(1))
    if not (
        isinstance(observation, dict)
        and observation.get("importer_identity") == expected_importer_identity
        and observation.get("pdf_sha256") == expected_pdf_sha256
        and observation.get("page_number") == page_number
        and observation.get("source_item_id") == source_item_id
        and page_visual_source_observation_v2_verified(
            observation,
            page_visual_authority,
        )
    ):
        return False
    created_ids = attempt.get("created_entity_ids")
    removed_ids = attempt.get("removed_entity_ids")
    attempted_type = attempt.get("attempted_type")
    requested_type = attempt.get("requested_type")
    return bool(
        isinstance(proof, dict)
        and _contract_exact_text(expected_importer_identity)
        and isinstance(evidence, dict)
        and _contract_exact_string_list(created_ids)
        and _contract_exact_string_list(removed_ids)
        and proof.get("created_entity_ids") == created_ids
        and proof.get("removed_entity_ids") == removed_ids
        and proof.get("cleanup_complete") is True
        and page_visual_fallback_proof_v2_verified(
            proof,
            observation=observation,
            authority=page_visual_authority,
            expected_requested_type=requested_type,
            expected_attempted_type=attempted_type,
        )
    )


def _generic_item_impossibility_prior_verified(attempt: Dict[str, Any]) -> bool:
    evidence = attempt.get("evidence")
    if not isinstance(evidence, dict):
        return False
    proof = evidence.get(ITEM_IMPOSSIBILITY_EVIDENCE_KEY)
    branch_evidence = dict(evidence)
    branch_evidence.pop(ITEM_IMPOSSIBILITY_EVIDENCE_KEY, None)
    return item_representation_impossibility_proof_verified(
        proof,
        branch_evidence=branch_evidence,
        source_item_id=attempt.get("source_item_id"),
        requested_type=attempt.get("requested_type"),
        attempted_type=attempt.get("attempted_type"),
        strategy=attempt.get("strategy"),
        reason=attempt.get("reason"),
        host_outcome=attempt.get("host_outcome"),
        cleanup_complete=attempt.get("cleanup_complete"),
        created_entity_ids=attempt.get("created_entity_ids"),
        removed_entity_ids=attempt.get("removed_entity_ids"),
        delivery_entity_ids=attempt.get("delivery_entity_ids"),
        support_entity_ids=attempt.get("support_entity_ids"),
        referenced_entity_ids=attempt.get("referenced_entity_ids"),
        reused_entity_ids=attempt.get("reused_entity_ids"),
        owned_block_names=attempt.get("owned_block_names"),
    )


def _freecad_item_impossibility_prior_verified(
    attempt: Dict[str, Any],
    expected_pdf_sha256: str,
) -> bool:
    proof = attempt.get("proof")
    source_item_id = attempt.get("source_item_id")
    page_match = (
        re.match(r"^p([1-9][0-9]*):", source_item_id)
        if isinstance(source_item_id, str)
        else None
    )
    if (
        not isinstance(proof, dict)
        or proof.get("item_specific_proven_impossible") is not True
        or "page_specific_proven_impossible" in proof
        or proof.get("importer_identity")
        != "bluecollarsystems.freecad.pdf_vector_importer"
        or re.fullmatch(r"[0-9a-f]{64}", expected_pdf_sha256) is None
        or proof.get("pdf_sha256") != expected_pdf_sha256
        or page_match is None
        or proof.get("page_number") != int(page_match.group(1))
        or proof.get("source_item_id") != source_item_id
        or proof.get("requested_type") != attempt.get("requested_type")
        or proof.get("attempted_type") != attempt.get("attempted_type")
        or not _contract_exact_text(attempt.get("reason_code"))
        or proof.get("reason_code") != attempt.get("reason_code")
        or not isinstance(proof.get("evidence"), dict)
        or not proof.get("evidence")
        or proof.get("attempted_sources_complete") is not True
        or proof.get("cleanup_complete") is not True
        or proof.get("created_entity_ids") != attempt.get("created_entity_ids")
        or proof.get("removed_entity_ids") != attempt.get("removed_entity_ids")
    ):
        return False
    results = proof.get("attempted_source_results")
    if not isinstance(results, list) or not results:
        return False
    for result in results:
        if (
            not isinstance(result, dict)
            or not _contract_exact_text(result.get("source"))
            or not _contract_exact_text(result.get("outcome"))
            or (
                "pdf_sha256" in result
                and result.get("pdf_sha256") != expected_pdf_sha256
            )
            or (
                "page_number" in result
                and result.get("page_number") != int(page_match.group(1))
            )
            or (
                "source_item_id" in result
                and result.get("source_item_id") != source_item_id
            )
        ):
            return False

    reason_code = proof.get("reason_code")
    attempted_type = attempt.get("attempted_type")
    page_number = int(page_match.group(1))
    if reason_code == "mixed_source_ink_not_exactly_representable":
        evidence = proof.get("evidence")
        zero_indexes = evidence.get("zero_character_indexes")
        visible_indexes = evidence.get("visible_character_indexes")
        source_ink_digest = proof.get("source_ink_evidence_sha256")
        if (
            not isinstance(proof.get("font_identity"), dict)
            or evidence.get("classification") != "mixed_visible_and_zero_ink"
            or not isinstance(evidence.get("source_text_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", evidence.get("source_text_sha256")) is None
            or not isinstance(source_ink_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_ink_digest) is None
            or evidence.get("source_ink_evidence_sha256") != source_ink_digest
            or not isinstance(zero_indexes, list)
            or not isinstance(visible_indexes, list)
            or not zero_indexes
            or not visible_indexes
            or any(type(index) is not int or index < 0 for index in zero_indexes)
            or any(type(index) is not int or index < 0 for index in visible_indexes)
            or len(zero_indexes) != len(set(zero_indexes))
            or len(visible_indexes) != len(set(visible_indexes))
            or set(zero_indexes).intersection(visible_indexes)
            or len(results) != 1
        ):
            return False
        [result] = results
        return bool(
            result.get("source") == "physical_source_ink_evidence"
            and result.get("outcome") == "proven_impossible"
            and result.get("reason_code") == reason_code
            and result.get("pdf_sha256") == expected_pdf_sha256
            and result.get("page_number") == page_number
            and result.get("source_item_id") == source_item_id
            and result.get("source_ink_evidence_sha256") == source_ink_digest
        )

    if attempted_type in {"glyphs", "geometry"}:
        if (
            reason_code not in _FREECAD_CLOSED_SVG_IMPOSSIBILITY_REASONS
            or len(results) != 1
        ):
            return False
        [result] = results
        return bool(
            result.get("source") == "svg_item_renderer"
            and result.get("outcome") == "proven_impossible"
            and result.get("reason_code") == reason_code
            and result.get("pdf_sha256") == expected_pdf_sha256
            and result.get("page_number") == page_number
            and result.get("source_item_id") == source_item_id
        )

    # Missing an exact source font lowers font fidelity; it does not make the
    # requested 3D Text representation impossible.  The producer must retain
    # 3D Text with a substitute font instead of advancing the type ladder.
    return False


def _host_text_delivery_invalid_reasons(
    host_app: str,
    attempt_ledger: Any,
    expected_pdf_sha256: str,
    page_source_observations: Any,
    page_visual_authority: Any = None,
) -> List[str]:
    if not isinstance(attempt_ledger, list):
        return ["text delivery attempt ledger is not a list"]
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for attempt in attempt_ledger:
        if not isinstance(attempt, dict):
            continue
        source_item_id = attempt.get("source_item_id")
        if _contract_exact_text(source_item_id):
            groups.setdefault(source_item_id, []).append(attempt)
    if groups and host_app not in _HOST_TEXT_FALLBACK_LADDERS:
        return [f"unsupported host-specific text ladder: {host_app or 'missing'}"]

    reasons: List[str] = []
    page_observation_ids = {
        source_item_id
        for source_item_id, attempts in groups.items()
        if len(attempts) > 1
        and (
            (
                host_app == "freecad"
                and re.fullmatch(r"p[1-9][0-9]*:page", source_item_id)
            )
            or (
                host_app in {"blender", "librecad"}
                and re.fullmatch(r"page_visual:[1-9][0-9]*", source_item_id)
            )
        )
    }
    if (
        page_source_observations is None
        and page_observation_ids
    ) or (
        page_source_observations is not None
        and (
            not isinstance(page_source_observations, dict)
            or set(page_source_observations) != page_observation_ids
        )
    ):
        reasons.append(
            "page_visual_source_observations do not exactly match page scopes"
        )
    host_ladders = _HOST_TEXT_FALLBACK_LADDERS.get(host_app, {})
    for source_item_id, attempts in groups.items():
        requested_type = attempts[0].get("requested_type")
        sequence = [attempt.get("attempted_type") for attempt in attempts]
        ladder = host_ladders.get(requested_type)
        if (
            ladder is None
            or any(attempt.get("requested_type") != requested_type for attempt in attempts)
            or sequence != list(ladder[: len(sequence)])
        ):
            reasons.append(f"{source_item_id} does not follow the exact host ladder prefix")

        page_scope = bool(
            (host_app == "freecad" and re.fullmatch(r"p[1-9][0-9]*:page", source_item_id))
            or (
                host_app in {"blender", "librecad"}
                and re.fullmatch(r"page_visual:[1-9][0-9]*", source_item_id)
            )
        )
        if page_scope and attempts[-1].get("final_type") != "raster":
            reasons.append(f"{source_item_id} page-visual delivery is not terminal raster")

        for local_index, prior in enumerate(attempts[:-1]):
            if prior.get("final_type") is not None:
                reasons.append(
                    f"{source_item_id} attempt {local_index} impossible final_type is present"
                )
            page_verified = _page_visual_prior_verified(
                host_app,
                prior,
                expected_pdf_sha256,
                page_source_observations,
                page_visual_authority,
            )
            if page_verified is not None:
                proof_verified = page_verified
            elif host_app == "freecad":
                proof_verified = _freecad_item_impossibility_prior_verified(
                    prior, expected_pdf_sha256
                )
            else:
                proof_verified = _generic_item_impossibility_prior_verified(prior)
            if not proof_verified:
                reasons.append(
                    f"{source_item_id} attempt {local_index} impossibility proof is invalid"
                )
    return reasons


def _blender_terminal_delivery_count(terminal: Dict[str, Any]) -> Optional[int]:
    record = terminal.get("host_record")
    if not isinstance(record, dict):
        return None
    source_item_id = terminal.get("source_item_id")
    requested_type = terminal.get("requested_type")
    final_type = terminal.get("final_type")
    raw_entity_ids = record.get("entity_ids")
    contribution = record.get("delivered_count_contribution")
    physical_count = record.get("physical_entity_count")
    if (
        record.get("item_id") != source_item_id
        or type(record.get("page")) is not int
        or record.get("page") <= 0
        or type(record.get("source_span_id")) is not int
        or record.get("source_span_id") < 0
        or record.get("requested_representation") != requested_type
        or record.get("final_representation") != final_type
        or record.get("status") != "delivered"
        or type(record.get("fallback_attempted")) is not bool
        or type(record.get("fallback_used")) is not bool
        or (
            record.get("fallback_used")
            and not record.get("fallback_attempted")
        )
        or record.get("fallback_used") != (requested_type != final_type)
        or not _contract_exact_string_list(raw_entity_ids)
        or type(physical_count) is not int
        or physical_count < 0
        or type(contribution) is not int
        or contribution not in {0, 1}
    ):
        return None
    delivery_ids = terminal.get("delivery_entity_ids")
    if record.get("zero_ink_delivery") is True:
        logical_id = record.get("logical_delivery_id")
        if (
            contribution != 0
            or raw_entity_ids != []
            or physical_count != 0
            or not _contract_exact_text(logical_id)
            or delivery_ids != [f"blender:logical:{logical_id}"]
            or type(record.get("zero_ink_character_count")) is not int
            or record.get("zero_ink_character_count") <= 0
            or not isinstance(record.get("source_manifest_sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", record.get("source_manifest_sha256")) is None
            or not isinstance(record.get("zero_ink_delivery_manifest_sha256"), str)
            or re.fullmatch(
                r"[0-9a-f]{64}",
                record.get("zero_ink_delivery_manifest_sha256"),
            )
            is None
        ):
            return None
        return 0
    expected_delivery_ids = [f"blender:object:{entity_id}" for entity_id in raw_entity_ids]
    if (
        contribution != 1
        or not raw_entity_ids
        or physical_count != len(raw_entity_ids)
        or delivery_ids != expected_delivery_ids
    ):
        return None
    return 1


def _non_freecad_actual_text_entity_types_verified(
    host_app: str,
    delivery_resolution: Dict[str, Any],
    entity_types: Any,
) -> bool:
    if host_app not in {"blender", "librecad"}:
        return False
    if (
        not isinstance(entity_types, dict)
        or set(entity_types) != _ACTUAL_TEXT_ENTITY_TYPE_FIELDS
        or entity_types.get("delivery_counts_valid") is not True
        or type(entity_types.get("count")) is not int
        or entity_types.get("count") < 0
        or type(entity_types.get("font_rendered")) is not bool
        or not isinstance(entity_types.get("examples"), list)
        or any(not isinstance(value, str) for value in entity_types.get("examples"))
    ):
        return False
    if any(
        type(entity_types.get(bucket)) is not int
        or entity_types.get(bucket) < 0
        for bucket in TEXT_ENTITY_DELIVERED_BUCKETS
    ):
        return False

    terminals = delivery_resolution.get("terminal_attempts")
    if not isinstance(terminals, list):
        return False
    expected_buckets = {bucket: 0 for bucket in TEXT_ENTITY_DELIVERED_BUCKETS}
    positive_types: List[str] = []
    for terminal in terminals:
        if not isinstance(terminal, dict):
            return False
        final_type = terminal.get("final_type")
        if host_app == "blender":
            bucket = _BLENDER_BUCKET_BY_TYPE.get(final_type)
            contribution = _blender_terminal_delivery_count(terminal)
        elif host_app == "librecad":
            bucket = _LIBRECAD_BUCKET_BY_TYPE.get(final_type)
            delivery_ids = terminal.get("delivery_entity_ids")
            contribution = (
                len(delivery_ids)
                if _contract_exact_string_list(delivery_ids)
                else None
            )
        else:
            return False
        if bucket is None or contribution is None:
            return False
        expected_buckets[bucket] += contribution
        if contribution > 0:
            positive_types.append(final_type)

    if host_app == "blender":
        expected_buckets["outline_curve_or_mesh"] = (
            expected_buckets["glyph_curve"] + expected_buckets["geometry_mesh"]
        )
        total_buckets = [
            bucket
            for bucket in TEXT_ENTITY_DELIVERED_BUCKETS
            if bucket != "outline_curve_or_mesh"
        ]
    else:
        total_buckets = list(TEXT_ENTITY_DELIVERED_BUCKETS)
    expected_total = sum(expected_buckets[bucket] for bucket in total_buckets)
    unique_types = sorted(set(positive_types))
    expected_type = (
        "none"
        if not unique_types
        else unique_types[0]
        if len(unique_types) == 1
        else "mixed"
    )
    return bool(
        entity_types.get("entity_type") == expected_type
        and entity_types.get("count") == expected_total
        and all(
            entity_types.get(bucket) == expected_count
            for bucket, expected_count in expected_buckets.items()
        )
    )


def _terminal_delivery_entity_ids_sha256(
    delivery_resolution: Dict[str, Any],
) -> Optional[str]:
    terminals = delivery_resolution.get("terminal_attempts")
    if type(terminals) is not list:
        return None
    entity_ids: List[str] = []
    for terminal in terminals:
        if type(terminal) is not dict:
            return None
        delivery_ids = terminal.get("delivery_entity_ids")
        if type(delivery_ids) is not list or any(
            type(entity_id) is not str
            or not entity_id
            or entity_id != entity_id.strip()
            for entity_id in delivery_ids
        ):
            return None
        entity_ids.extend(delivery_ids)
    if len(entity_ids) != len(set(entity_ids)):
        return None
    canonical = json.dumps(
        sorted(entity_ids),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _non_freecad_host_result_persistence_verified(
    *,
    host_app: str,
    persistence: Any,
    importer: Any,
    input_block: Any,
    import_session_id: Any,
    delivery_resolution: Dict[str, Any],
) -> bool:
    """Validate an adapter's fully bound post-commit artifact attestation.

    Shared code can recompute report-side identities and delivery IDs, but it
    cannot inspect a live Blender scene or a LibreCAD DXF itself. Producers
    MUST calculate ``persistence_sha256`` only after committing and reinspecting
    Blender scene state, or after atomically writing and reopening the DXF.
    Format-valid hashes and flags without every binding below are insufficient.
    """

    expected_method = _HOST_RESULT_PERSISTENCE_METHODS.get(host_app)
    expected_identity = _PAGE_VISUAL_IMPORTER_IDENTITIES.get(host_app)
    source_pdf_sha256 = (
        input_block.get("sha256") if type(input_block) is dict else None
    )
    importer_identity = (
        importer.get("identity") if type(importer) is dict else None
    )
    delivery_ids_sha256 = _terminal_delivery_entity_ids_sha256(
        delivery_resolution
    )
    if (
        expected_method is None
        or expected_identity is None
        or type(persistence) is not dict
        or set(persistence) != _HOST_RESULT_PERSISTENCE_FIELDS
        or delivery_resolution.get("verified") is not True
        or re.fullmatch(r"[0-9a-f]{64}", str(source_pdf_sha256 or "")) is None
        or not _contract_exact_text(import_session_id)
        or delivery_ids_sha256 is None
    ):
        return False
    persistence_sha256 = persistence.get("persistence_sha256")
    return bool(
        persistence.get("schema") == "bcs.host_result_persistence/1.0"
        and persistence.get("host_app") == host_app
        and importer_identity == expected_identity
        and persistence.get("importer_identity") == expected_identity
        and persistence.get("source_pdf_sha256") == source_pdf_sha256
        and persistence.get("import_session_id") == import_session_id
        and persistence.get("method") == expected_method
        and persistence.get("commit_complete") is True
        and persistence.get("persistence_verified") is True
        and persistence.get("artifact_reinspection_complete") is True
        and type(persistence_sha256) is str
        and re.fullmatch(r"[0-9a-f]{64}", persistence_sha256) is not None
        and persistence.get("delivery_entity_ids_sha256")
        == delivery_ids_sha256
        and persistence.get("observed_delivery_entity_ids_sha256")
        == delivery_ids_sha256
    )


def build_import_contract_ready(
    report: "ImportReport",
    page_visual_authority: Any = None,
) -> Dict[str, Any]:
    """Aggregate report-contract readiness for app/Report Doctor gates."""

    extra = report.extra if isinstance(report.extra, dict) else {}
    result = report.result if isinstance(report.result, dict) else {}
    meta = report.report_meta if isinstance(report.report_meta, dict) else {}

    open_failure = extra.get("open_failure")
    has_stamp = bool(str(meta.get("build_stamp") or "").strip())
    scale_crosscheck_ok = _scale_crosscheck_verified(extra)
    host_app = str((report.host or {}).get("app") or "").strip().lower()
    supported_host = host_app in _HOST_TEXT_FALLBACK_LADDERS
    entity_types = extra.get("actual_text_entity_types")
    has_entity_types = isinstance(entity_types, dict)
    terminal_failure = extra.get("terminal_failure")
    status_authorities = []
    if "result_status" in extra:
        status_authorities.append(extra.get("result_status"))
    if "status" in result:
        status_authorities.append(result.get("status"))
    result_succeeded = bool(
        status_authorities
        and all(
            type(status) is str and status == "success"
            for status in status_authorities
        )
        and terminal_failure is None
    )
    delivery = extra.get("text_representation_delivery")
    attempt_ledger = extra.get("text_delivery_attempts")
    obligations = extra.get("text_delivery_obligations")
    expected_source_item_ids: List[str] = []
    obligations_valid = False
    delivery_resolution: Dict[str, Any] = {
        "verified": False,
        "invalid_reasons": ["text delivery contract was not resolved"],
    }
    if (
        isinstance(obligations, dict)
        and set(obligations) == _TEXT_DELIVERY_OBLIGATION_FIELDS
    ):
        source_ids_value = obligations.get("source_item_ids")
        if isinstance(source_ids_value, list):
            expected_source_item_ids = list(source_ids_value)
            exact_source_ids = bool(
                all(
                    isinstance(source_item_id, str)
                    and bool(source_item_id)
                    and source_item_id == source_item_id.strip()
                    for source_item_id in expected_source_item_ids
                )
                and len(expected_source_item_ids)
                == len(set(expected_source_item_ids))
            )
            requested_type = obligations.get("requested_type")
            exact_requested_type = bool(
                isinstance(requested_type, str)
                and bool(requested_type)
                and requested_type == requested_type.strip()
                and requested_type == extra.get("text_mode")
            )
            required = obligations.get("required")
            obligations_valid = bool(
                obligations.get("schema")
                == "bcs.text_delivery_obligations/1.0"
                and type(required) is bool
                and required == bool(expected_source_item_ids)
                and exact_source_ids
                and exact_requested_type
                and isinstance(delivery, dict)
                and delivery.get("required") is required
                and delivery.get("requested_type") == requested_type
            )
    if (
        obligations_valid
        and isinstance(attempt_ledger, list)
        and all(isinstance(attempt, dict) for attempt in attempt_ledger)
    ):
        delivery_resolution = resolve_text_representation_delivery(
            attempt_ledger,
            delivery,
            expected_source_item_ids=expected_source_item_ids,
            fallback_ladders=_HOST_TEXT_FALLBACK_LADDERS.get(host_app),
        )
    text_delivery_invalid_reasons = list(
        delivery_resolution.get("invalid_reasons") or []
    )
    if not obligations_valid:
        text_delivery_invalid_reasons.append(
            "text_delivery_obligations keys or values do not exactly match schema"
        )
    host_delivery_reasons = _host_text_delivery_invalid_reasons(
        host_app,
        attempt_ledger,
        str((report.input or {}).get("sha256") or ""),
        extra.get("page_visual_source_observations"),
        page_visual_authority,
    )
    text_delivery_invalid_reasons.extend(host_delivery_reasons)
    text_delivery_ok = bool(
        obligations_valid
        and delivery_resolution.get("verified") is True
        and not host_delivery_reasons
    )
    text_ok = bool(has_entity_types)
    representation_contract_ok = "representation_contract_violation" not in extra

    inventory = extra.get("actual_host_object_inventory")
    save_reopen = extra.get("save_reopen_inventory")
    inventory_required = host_app == "freecad"
    save_reopen_required = host_app == "freecad"
    delivery_inventory_binding_required = host_app == "freecad"
    host_result_persistence_required = supported_host
    if host_app == "freecad":
        inventory_ok = _freecad_host_inventory_verified(inventory, result)
        save_reopen_ok = _freecad_save_reopen_inventory_verified(
            inventory,
            save_reopen,
        )
        text_ok = _freecad_actual_text_entity_types_verified(
            delivery,
            attempt_ledger,
            entity_types,
            result,
        )
        delivery_inventory_binding_ok = _freecad_delivery_inventory_binding_verified(
            delivery,
            attempt_ledger,
            inventory,
            str((report.input or {}).get("sha256") or "").strip().lower(),
            extra.get("page_visual_source_observations"),
            page_visual_authority,
        )
        host_result_persistence_ok = bool(
            inventory_ok and save_reopen_ok and delivery_inventory_binding_ok
        )
    else:
        inventory_ok = False
        save_reopen_ok = False
        delivery_inventory_binding_ok = False
        text_ok = _non_freecad_actual_text_entity_types_verified(
            host_app,
            delivery_resolution,
            entity_types,
        )
        host_result_persistence_ok = _non_freecad_host_result_persistence_verified(
            host_app=host_app,
            persistence=extra.get("host_result_persistence"),
            importer=report.importer,
            input_block=report.input,
            import_session_id=extra.get("import_session_id"),
            delivery_resolution=delivery_resolution,
        )
    ready = (
        supported_host
        and has_stamp
        and scale_crosscheck_ok
        and text_ok
        and text_delivery_ok
        and representation_contract_ok
        and (not inventory_required or inventory_ok)
        and (not save_reopen_required or save_reopen_ok)
        and (
            not delivery_inventory_binding_required
            or delivery_inventory_binding_ok
        )
        and (
            not host_result_persistence_required
            or host_result_persistence_ok
        )
        and result_succeeded
        and open_failure is None
    )

    return {
        "ready": ready,
        "checks": {
            "build_stamp": has_stamp,
            "supported_host": supported_host,
            "scale_crosscheck": scale_crosscheck_ok,
            "actual_text_entity_types": text_ok,
            "text_delivery": text_delivery_ok,
            "representation_contract": representation_contract_ok,
            "host_object_inventory": inventory_ok,
            "save_reopen_inventory": save_reopen_ok,
            "delivery_inventory_binding": delivery_inventory_binding_ok,
            "host_object_inventory_required": inventory_required,
            "save_reopen_inventory_required": save_reopen_required,
            "delivery_inventory_binding_required": (
                delivery_inventory_binding_required
            ),
            "host_result_persistence": host_result_persistence_ok,
            "host_result_persistence_required": host_result_persistence_required,
            "result_succeeded": result_succeeded,
            # Compatibility spelling retained for existing Report Doctor clients.
            "successful_result": result_succeeded,
            "no_terminal_failure": terminal_failure is None,
            "no_open_failure": open_failure is None,
        },
        "text_delivery_invalid_reasons": text_delivery_invalid_reasons,
        "note": (
            "ready for contract consumers"
            if ready
            else "one or more import report contract checks need review"
        ),
    }


def enrich_import_report_extras(
    report: "ImportReport",
    page_visual_authority: Any = None,
) -> None:
    """Attach shared derived fields and refresh human_summary."""

    crosscheck = build_scale_crosscheck(report.extra)
    if crosscheck:
        report.extra["scale_crosscheck"] = crosscheck
    perf = report.performance if isinstance(report.performance, dict) else {}
    result = report.result if isinstance(report.result, dict) else {}
    hint = build_performance_hint(
        primitive_count=int(result.get("primitives") or 0),
        text_count=int(result.get("text_entities") or 0),
        peak_mb=float(perf.get("peak_mb") or 0.0),
    )
    if hint:
        report.extra["performance_hint"] = hint
    if "model_3d" not in report.extra:
        host = str((report.host or {}).get("app") or "")
        report.extra["model_3d"] = build_model_3d_extra(host)
    report.extra["human_summary"] = build_human_summary(report)
    report.extra["import_contract_ready"] = build_import_contract_ready(
        report,
        page_visual_authority=page_visual_authority,
    )


def _format_text_mode(mode: str) -> str:
    key = str(mode or "").strip().lower()
    labels = {
        "geometry": "geometry text",
        "glyphs": "glyph geometry",
        "3d_text": "3D text",
        "text": "flat editable text",
        "labels": "labels",
        "outlines": "outlines",
        "raster": "item-scoped raster text",
    }
    return labels.get(key, key.replace("_", " ") if key else "")


def build_human_summary(report: ImportReport | Dict[str, Any]) -> str:
    """One plain-English paragraph describing what happened during import."""

    data = report.to_dict() if isinstance(report, ImportReport) else dict(report or {})
    host_key = str((data.get("host") or {}).get("app") or "importer").lower()
    host = _HOST_LABELS.get(host_key, host_key.title() or "Importer")

    input_block = data.get("input") or {}
    result = data.get("result") or {}
    perf = data.get("performance") or {}
    fallback = data.get("fallback") or {}
    extra = data.get("extra") or {}
    diagnostics = extra.get("diagnostics") or {}

    pages = int(input_block.get("pages") or 0)
    primitives = int(result.get("primitives") or 0)
    text_count = int(result.get("text_entities") or 0)
    image_count = int(result.get("images") or 0)
    layers = int(result.get("layers") or 0)
    warnings = int(result.get("warnings") or 0)
    elapsed_ms = float(perf.get("elapsed_ms") or 0.0)
    elapsed_s = elapsed_ms / 1000.0 if elapsed_ms > 0 else 0.0

    mode = str(data.get("mode") or "auto")
    raw_text_mode = str(extra.get("text_mode") or "").strip()
    text_mode = (
        ""
        if extra.get("import_text") is False or raw_text_mode.lower() == "none"
        else _format_text_mode(raw_text_mode)
    )
    pdf_name = _basename(str(input_block.get("file") or ""))

    parts: List[str] = []
    page_phrase = f"{pages} page{'s' if pages != 1 else ''}" if pages else "the PDF"
    result_status = str(
        extra.get("result_status") or result.get("status") or "success"
    ).lower()
    if result_status in {"failed", "error", "cancelled"}:
        parts.append(
            f"Import failed for {page_phrase} from {pdf_name} in {host} using {mode} mode"
        )
    elif result_status in {"incomplete", "pending", "pending_export"}:
        parts.append(
            f"Import is incomplete for {page_phrase} from {pdf_name} in {host} using {mode} mode"
        )
    else:
        parts.append(f"Imported {page_phrase} from {pdf_name} into {host} using {mode} mode")

    if text_mode:
        parts[-1] += f" with {text_mode}"

    outcome = []
    if primitives:
        outcome.append(f"{primitives} vector primitive{'s' if primitives != 1 else ''}")
    if text_count:
        outcome.append(f"{text_count} text item{'s' if text_count != 1 else ''}")
    if image_count:
        outcome.append(
            f"{image_count} raster/image placement{'s' if image_count != 1 else ''}"
        )
    if layers:
        outcome.append(f"{layers} PDF layer{'s' if layers != 1 else ''}")

    if outcome:
        parts.append("Created " + ", ".join(outcome))
    else:
        parts.append("No editable geometry was created")

    if elapsed_s > 0:
        parts.append(f"in {elapsed_s:.1f}s")

    scale = extra.get("resolved_scale") or {}
    if isinstance(scale, dict) and scale.get("factor"):
        notation = str(scale.get("notation") or "").strip()
        source = str(scale.get("source") or "drawing").replace("_", " ")
        scale_bit = f"Scale resolved from {source}"
        if notation:
            scale_bit += f" ({notation})"
        conf = scale.get("confidence")
        if conf is not None:
            try:
                scale_bit += f", confidence {float(conf) * 100:.0f}%"
            except (TypeError, ValueError):
                pass
        parts.append(scale_bit)

    auto_reason = str(extra.get("auto_reason") or "").strip()
    if auto_reason and mode == "auto":
        parts.append(f"Auto mode chose this path because {auto_reason.rstrip('.')}")

    if fallback.get("used"):
        reason = str(fallback.get("reason") or "fallback").replace("_", " ")
        parts.append(f"Raster or degraded fallback was used ({reason})")
    elif primitives > 0:
        parts.append("Vector extraction completed without raster fallback")

    quality = str(diagnostics.get("quality_level") or "").strip()
    if quality:
        parts.append(f"Overall fidelity: {quality}")

    if warnings:
        parts.append(
            f"{warnings} warning{'s' if warnings != 1 else ''} recorded — review the import log before production use"
        )

    crosscheck = extra.get("scale_crosscheck") or {}
    if isinstance(crosscheck, dict):
        banner = str(crosscheck.get("banner") or crosscheck.get("user_message") or "").strip()
        if banner:
            parts.append(f"Scale note: {banner.rstrip('.')}")

    font_note = str(extra.get("font_substitution_note") or "").strip()
    if font_note:
        parts.append(font_note.rstrip("."))

    interactive_note = str(extra.get("pdf_interactive_note") or "").strip()
    if interactive_note:
        parts.append(interactive_note.rstrip("."))

    perf_hint = str(extra.get("performance_hint") or "").strip()
    if perf_hint:
        parts.append(perf_hint.rstrip("."))

    importer_version = str((data.get("importer") or {}).get("version") or "").strip()
    if importer_version:
        parts.append(f"Importer v{importer_version}")

    paragraph = ". ".join(part.rstrip(".") for part in parts if part).strip()
    if paragraph and not paragraph.endswith("."):
        paragraph += "."
    return paragraph


@dataclass
class TextEntityVerification:
    """Text entity type verification for cross-host consistency."""

    entity_type: str = ""  # text, labels, 3d_text, glyphs, geometry, raster, mixed
    count: int = 0
    font_rendered: bool = False
    examples: List[str] = field(default_factory=list)
    native_label: int = 0
    native_text: int = 0
    native_3d_text: int = 0
    glyph_curve: int = 0
    geometry_mesh: int = 0
    raster_patch: int = 0
    outline_curve_or_mesh: int = 0
    raw_geometry_edges: int = 0
    raster_text_patch: int = 0
    dxf_text: int = 0
    raster_image: int = 0
    fallback_geometry: int = 0
    delivery_counts_valid: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


_DELIVERED_ENTITY_TYPES = {
    "native_label": "labels",
    "native_text": "text",
    "native_3d_text": "3d_text",
    "glyph_curve": "glyphs",
    "geometry_mesh": "geometry",
    "outline_curve_or_mesh": "glyphs",
    "raw_geometry_edges": "geometry",
    "raster_patch": "raster",
    "raster_text_patch": "raster",
    "dxf_text": "text",
    "raster_image": "raster",
    "fallback_geometry": "geometry",
}


def build_actual_text_entity_types(
    *,
    host_app: str,
    text_mode: str,
    count: int = 0,
    font_rendered: Optional[bool] = None,
    examples: Optional[List[str]] = None,
    delivered_counts: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """Return the shared actual_text_entity_types payload.

    When ``delivered_counts`` is provided (mapping TEXT_ENTITY_DELIVERED_BUCKETS
    names to counts the host actually created), the payload reflects DELIVERED
    entities instead of being derived from the requested ``text_mode`` string
    (TEXTMODE-1: the report must be able to tell the truth when delivered
    differs from requested). Without it, the historical requested-mode
    derivation is used — fully backward compatible.
    """

    host = str(host_app or "").strip().lower()
    mode = str(text_mode or "").strip().lower()
    total = max(0, int(count or 0))
    rendered = font_rendered
    if rendered is None:
        rendered = mode in {
            "text",
            "native_text",
            "labels",
            "label",
            "3d_text",
            "text3d",
        }

    info = TextEntityVerification(
        entity_type=mode,
        count=total,
        font_rendered=bool(rendered),
        examples=list(examples or [])[:3],
    )
    if delivered_counts is not None:
        valid_delivered_counts = bool(
            isinstance(delivered_counts, dict)
            and set(delivered_counts).issubset(TEXT_ENTITY_DELIVERED_BUCKETS)
            and all(
                type(value) is int and value >= 0
                for value in delivered_counts.values()
            )
        )
        if not valid_delivered_counts:
            return TextEntityVerification(
                entity_type="none",
                count=0,
                font_rendered=False,
                examples=list(examples or [])[:3],
                delivery_counts_valid=False,
            ).to_dict()
        delivered_buckets: List[str] = []
        for bucket in TEXT_ENTITY_DELIVERED_BUCKETS:
            value = delivered_counts.get(bucket, 0)
            if value > 0:
                setattr(info, bucket, value)
                delivered_buckets.append(bucket)

        # Blender reports specific curve/mesh buckets plus the historical
        # outline aggregate. Count the aggregate only when no specific bucket
        # was supplied, so compatibility fields cannot double-count entities.
        count_buckets = list(delivered_buckets)
        if {"glyph_curve", "geometry_mesh"} & set(delivered_counts):
            count_buckets = [
                bucket for bucket in count_buckets
                if bucket != "outline_curve_or_mesh"
            ]
            info.outline_curve_or_mesh = (
                int(info.glyph_curve or 0) + int(info.geometry_mesh or 0)
            )
        delivered_total = sum(int(getattr(info, bucket, 0) or 0) for bucket in count_buckets)
        info.count = delivered_total
        if not delivered_buckets:
            info.entity_type = "none"
            info.font_rendered = False
            return info.to_dict()

        delivered_types = {
            _DELIVERED_ENTITY_TYPES[bucket] for bucket in count_buckets
        }
        info.entity_type = (
            next(iter(delivered_types)) if len(delivered_types) == 1 else "mixed"
        )
        if font_rendered is None:
            info.font_rendered = bool(
                {"native_label", "native_text", "native_3d_text", "dxf_text"}
                & set(delivered_buckets)
            )
        return info.to_dict()
    if total <= 0 or mode in {"", "none"}:
        return info.to_dict()

    if host == "librecad":
        if mode in {"text", "labels", "label", "3d_text", "text3d"}:
            info.dxf_text = total
        elif mode in {"glyphs", "geometry", "outlines"}:
            info.raw_geometry_edges = total
        else:
            info.fallback_geometry = total
    elif host == "blender":
        if mode in {"labels", "label"}:
            info.native_label = total
        elif mode in {"text"}:
            info.native_text = total
        elif mode in {"3d_text", "text3d"}:
            info.native_3d_text = total
        elif mode in {"glyphs"}:
            info.glyph_curve = total
            info.outline_curve_or_mesh = total
        elif mode in {"geometry", "outlines"}:
            info.geometry_mesh = total
            info.outline_curve_or_mesh = total
        elif mode == "raster":
            info.raster_patch = total
        else:
            info.fallback_geometry = total
    elif host == "freecad":
        if mode in {"text", "native_text"}:
            info.native_text = total
        elif mode in {"labels", "label"}:
            info.native_label = total
        elif mode in {"3d_text", "text3d"}:
            info.native_3d_text = total
        elif mode in {"glyphs", "geometry", "outlines"}:
            info.outline_curve_or_mesh = total
        elif mode in {"raster", "raster_text_patch"}:
            info.raster_text_patch = total
        else:
            info.fallback_geometry = total
    elif host == "sketchup":
        if mode in {"labels", "label"}:
            info.native_label = total
        elif mode in {"3d_text", "text3d"}:
            info.native_3d_text = total
        elif mode in {"glyphs", "geometry", "outlines"}:
            info.outline_curve_or_mesh = total
        else:
            info.fallback_geometry = total
    return info.to_dict()


@dataclass
class ImportReport:
    """Cross-host import report aligned with board Q-03-a."""

    schema: str = SCHEMA
    host: Dict[str, str] = field(default_factory=dict)
    runtime: Dict[str, str] = field(default_factory=dict)
    importer: Dict[str, str] = field(default_factory=dict)
    pdf_engine: Dict[str, str] = field(default_factory=dict)
    input: Dict[str, Any] = field(default_factory=dict)
    result: Dict[str, Any] = field(default_factory=dict)
    performance: Dict[str, Any] = field(default_factory=dict)
    fallback: Dict[str, Any] = field(default_factory=lambda: {"used": False, "reason": None})
    mode: str = "auto"
    report_meta: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        extra = payload.get("extra")
        if isinstance(extra, dict) and "import_contract_ready" in extra:
            detached = type(self)._from_dict_unchecked(payload)
            extra["import_contract_ready"] = build_import_contract_ready(detached)
        return payload

    def to_json(self, indent: int = 2) -> str:
        payload = self.to_dict()
        _require_finite_json_numbers(payload)
        return json.dumps(
            payload,
            indent=indent,
            sort_keys=False,
            allow_nan=False,
        )

    def write_json(self, output_path: str, indent: int = 2) -> None:
        from .atomic_io import atomic_write_text

        atomic_write_text(output_path, self.to_json(indent=indent) + "\n")

    @classmethod
    def _from_dict_unchecked(cls, data: Dict[str, Any]) -> "ImportReport":
        return cls(
            schema=str(data.get("schema", SCHEMA)),
            host=dict(data.get("host", {}) or {}),
            runtime=dict(data.get("runtime", {}) or {}),
            importer=dict(data.get("importer", {}) or {}),
            pdf_engine=dict(data.get("pdf_engine", {}) or {}),
            input=dict(data.get("input", {}) or {}),
            result=dict(data.get("result", {}) or {}),
            performance=dict(data.get("performance", {}) or {}),
            fallback=dict(data.get("fallback", {}) or {"used": False, "reason": None}),
            mode=str(data.get("mode", "auto")),
            report_meta=dict(data.get("report_meta", {}) or {}),
            extra=dict(data.get("extra", {}) or {}),
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ImportReport":
        report = cls._from_dict_unchecked(data)
        if "import_contract_ready" in report.extra:
            report.extra["import_contract_ready"] = build_import_contract_ready(report)
        return report

    @classmethod
    def read_json(cls, input_path: str) -> "ImportReport":
        return cls.from_dict(json.loads(Path(input_path).read_text(encoding="utf-8")))


def build_import_report(
    *,
    host_app: str,
    host_version: str = "",
    runtime_lang: str = "python",
    runtime_version: str = "",
    importer_version: str = "",
    pdf_path: str,
    mode: str = "auto",
    pages: int = 0,
    primitive_count: int = 0,
    text_count: int = 0,
    image_count: int = 0,
    layer_count: int = 0,
    bbox: Optional[List[float]] = None,
    warnings: int = 0,
    elapsed_ms: float = 0.0,
    peak_mb: float = 0.0,
    performance_phases: Optional[Dict[str, float]] = None,
    helper_timings_ms: Optional[Dict[str, float]] = None,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
    pdf_engine_name: str = "pymupdf",
    pdf_engine_version: str = "",
    pdf_engine_wheel_tag: str = "",
    import_text: Optional[bool] = None,
    text_mode: Optional[str] = None,
    text_source_spans: Optional[int] = None,
    text_glyph_estimate: Optional[int] = None,
    text_fallback: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
    page_visual_authority: Any = None,
) -> ImportReport:
    # TEXTMODE-1: a text-mode substitution is a loud fallback. Normalize the
    # host-supplied record; a real substitution marks fallback.used and adds
    # the fallback.text block {requested, delivered, reason, count}.
    text_fallback_block = None
    if text_fallback:
        text_fallback_block = build_text_mode_fallback(
            requested=str(text_fallback.get("requested") or ""),
            delivered=str(text_fallback.get("delivered") or ""),
            reason=str(text_fallback.get("reason") or ""),
            count=text_fallback.get("count") or 0,
        )
    effective_fallback_used = bool(fallback_used) or text_fallback_block is not None
    effective_fallback_reason = fallback_reason
    if text_fallback_block is not None and not effective_fallback_reason:
        effective_fallback_reason = (
            "text_mode_fallback: "
            f"{text_fallback_block['requested']} -> {text_fallback_block['delivered']} "
            f"({text_fallback_block['reason']})"
        )

    input_block: Dict[str, Any] = {
        "file": str(pdf_path),
        "pages": int(pages),
    }
    report_sha256 = ""
    try:
        if pdf_path and Path(pdf_path).is_file():
            report_sha256 = _sha256_file(pdf_path)
            input_block["sha256"] = report_sha256
    except OSError:
        pass

    extra_block = dict(extra or {})
    if import_text is not None:
        extra_block.setdefault("import_text", bool(import_text))
    if text_mode is not None:
        extra_block.setdefault("text_mode", str(text_mode))
    if text_source_spans is not None:
        extra_block.setdefault("text_source_spans", int(text_source_spans))
    if text_glyph_estimate is not None:
        extra_block.setdefault("text_glyph_estimate", int(text_glyph_estimate))
    if pdf_path:
        extra_block.update(_pdf_audit_extras(pdf_path))

    extra_block.setdefault(
        "diagnostics",
        build_fidelity_diagnostics(
            primitive_count=primitive_count,
            text_count=text_count,
            image_count=image_count,
            layer_count=layer_count,
            warnings=warnings,
            fallback_used=effective_fallback_used,
            fallback_reason=effective_fallback_reason,
            import_text=import_text,
            text_mode=text_mode,
            text_source_spans=text_source_spans,
            text_glyph_estimate=text_glyph_estimate,
            text_fallback=text_fallback_block,
        ),
    )

    performance_block: Dict[str, Any] = {
        "elapsed_ms": float(elapsed_ms),
        "peak_mb": float(peak_mb),
    }
    if performance_phases:
        phases = {
            str(k): float(v)
            for k, v in performance_phases.items()
            if v is not None
        }
        if phases:
            performance_block["phases"] = phases
    if helper_timings_ms:
        helpers = {
            str(k): float(v)
            for k, v in helper_timings_ms.items()
            if v is not None
        }
        if helpers:
            performance_block["helpers_ms"] = helpers

    fallback_block: Dict[str, Any] = {
        "used": bool(effective_fallback_used),
        "reason": effective_fallback_reason,
    }
    if text_fallback_block is not None:
        fallback_block["text"] = text_fallback_block

    report = ImportReport(
        host={"app": host_app, "version": host_version},
        runtime={"lang": runtime_lang, "version": runtime_version},
        importer={
            "version": importer_version,
            "identity": _PAGE_VISUAL_IMPORTER_IDENTITIES.get(
                str(host_app or "").strip().lower(), ""
            ),
        },
        pdf_engine={
            "name": pdf_engine_name,
            "version": pdf_engine_version,
            "wheel_tag": pdf_engine_wheel_tag,
        },
        input=input_block,
        result={
            "primitives": int(primitive_count),
            "text_entities": int(text_count),
            "images": int(image_count),
            "layers": int(layer_count),
            "bbox": bbox,
            "warnings": int(warnings),
        },
        performance=performance_block,
        fallback=fallback_block,
        mode=mode,
        report_meta=build_report_meta(
            host_app=host_app,
            importer_version=importer_version,
            report_sha256=report_sha256,
        ),
        extra=extra_block,
    )
    enrich_import_report_extras(
        report,
        page_visual_authority=page_visual_authority,
    )
    return report


__all__ = [
    "SCHEMA",
    "SCALE_TRUST_CONFIDENCE",
    "PERFORMANCE_HINT_ENTITY_THRESHOLD",
    "PERFORMANCE_HINT_PEAK_MB",
    "TEXT_ENTITY_DELIVERED_BUCKETS",
    "ImportReport",
    "build_fidelity_diagnostics",
    "build_text_mode_fallback",
    "build_actual_text_entity_types",
    "build_report_meta",
    "build_font_embedding_hints",
    "build_human_summary",
    "build_pdf_interactive_note",
    "build_performance_hint",
    "build_scale_crosscheck",
    "build_model_3d_extra",
    "build_import_contract_ready",
    "enrich_import_report_extras",
    "build_import_report",
]
