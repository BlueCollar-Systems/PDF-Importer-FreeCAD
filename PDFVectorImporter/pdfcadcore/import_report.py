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
from pathlib import Path
from typing import Any, Dict, List, Optional

from .preflight_copy import SCALE_CROSSCHECK_BANNER

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
    geometry_comparison = _freecad_shape_geometry_comparison(left_raw, right_raw)
    max_delta = _freecad_finite_number(geometry_comparison.get("max_delta"))
    if geometry_comparison.get("verified") is not True or max_delta is None:
        return {"verified": False, "certificate": None}
    certificate: Dict[str, Any] = {
        "schema": _FREECAD_SHAPE_COMPARISON_SCHEMA,
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
    topology = content.get("shape_topology_counts")
    metrics = content.get("shape_metrics")
    geometry = content.get("shape_fingerprint_geometry")
    digest = content.get("source_ink_evidence_sha256")
    return bool(
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
) -> bool:
    """Revalidate the sealed source/font proof; Unicode is not ink authority."""

    if not isinstance(evidence, dict) or not isinstance(source_text, str):
        return False
    characters = evidence.get("characters")
    classification = evidence.get("classification")
    digest = evidence.get("evidence_sha256")
    if (
        evidence.get("schema") != "pdf_source_ink_evidence_v1"
        or evidence.get("authority") != "pymupdf_rawdict_texttrace_exact_font"
        or not isinstance(evidence.get("pdf_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence.get("pdf_sha256")) is None
        or type(evidence.get("page_number")) is not int
        or evidence.get("page_number") <= 0
        or evidence.get("source_item_id") != source_item_id
        or evidence.get("source_text") != source_text
        or evidence.get("source_text_sha256")
        != hashlib.sha256(source_text.encode("utf-8")).hexdigest()
        or not isinstance(evidence.get("font_identity"), dict)
        or not evidence.get("font_identity")
        or classification not in {"zero_visible_ink", "visible_ink"}
        or evidence.get("all_characters_physically_resolved") is not True
        or not isinstance(characters, list)
        or not characters
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        return False

    resolved_text: List[str] = []
    zero_flags: List[bool] = []
    for index, record in enumerate(characters):
        if not isinstance(record, dict):
            return False
        character = record.get("character")
        authority = record.get("authority")
        if (
            not isinstance(character, str)
            or not character
            or record.get("source_index") != index
            or record.get("physically_resolved") is not True
            or type(record.get("zero_visible_ink")) is not bool
            or authority
            not in {
                "pymupdf_rawdict_synthetic_character",
                "pymupdf_texttrace_nonpainting_render_mode",
                "exact_pdf_font_glyph_bounds",
            }
        ):
            return False
        if authority == "pymupdf_rawdict_synthetic_character":
            if (
                record.get("synthetic") is not True
                or record.get("zero_visible_ink") is not True
            ):
                return False
        elif authority == "pymupdf_texttrace_nonpainting_render_mode":
            opacity = record.get("opacity")
            if (
                record.get("synthetic") is not False
                or record.get("zero_visible_ink") is not True
                or type(record.get("glyph_id")) is not int
                or isinstance(opacity, bool)
                or not isinstance(opacity, (int, float))
                or not math.isfinite(float(opacity))
                or (record.get("trace_type") != 3 and float(opacity) > 0.0)
            ):
                return False
        else:
            bounds = record.get("glyph_bounds")
            opacity = record.get("opacity")
            for hash_name in ("source_font_sha256", "usable_font_sha256"):
                value = record.get(hash_name)
                if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                    return False
            if (
                record.get("synthetic") is not False
                or type(record.get("glyph_id")) is not int
                or record.get("glyph_id") < 0
                or not isinstance(record.get("glyph_name"), str)
                or not record.get("glyph_name")
                or type(record.get("trace_type")) is not int
                or record.get("trace_type") == 3
                or isinstance(opacity, bool)
                or not isinstance(opacity, (int, float))
                or not math.isfinite(float(opacity))
                or float(opacity) <= 0.0
                or (
                    bounds is not None
                    and (
                        not isinstance(bounds, (list, tuple))
                        or len(bounds) != 4
                        or any(
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(float(value))
                            for value in bounds
                        )
                    )
                )
                or record.get("zero_visible_ink") != (bounds is None)
            ):
                return False
        resolved_text.append(character)
        zero_flags.append(record["zero_visible_ink"])

    digest_payload = dict(evidence)
    digest_payload.pop("evidence_sha256", None)
    expected_digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return bool(
        "".join(resolved_text) == source_text
        and (
            (classification == "zero_visible_ink" and all(zero_flags))
            or (classification == "visible_ink" and not all(zero_flags))
        )
        and digest == expected_digest
    )


def _freecad_source_ink_inventory_binding_verified(
    terminal_evidence: Any,
    content: Any,
    source_item_id: str,
    source_text: str,
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
        )
        and content.get("source_ink_classification")
        == source_evidence.get("classification")
        and content.get("source_ink_evidence_sha256")
        == source_evidence.get("evidence_sha256")
    )


def _freecad_delivery_inventory_binding_verified(
    delivery: Any,
    inventory: Any,
) -> bool:
    """Bind each terminal text delivery to its exact persisted host object."""

    if not isinstance(delivery, dict) or delivery.get("verified") is not True:
        return False
    if not isinstance(inventory, dict) or inventory.get("verified") is not True:
        return False

    records = inventory.get("objects")
    terminals = delivery.get("terminal_attempts")
    if not isinstance(records, list) or not isinstance(terminals, list) or not terminals:
        return False

    records_by_id: Dict[str, Dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            return False
        entity_id = str(record.get("entity_id") or "").strip()
        if not entity_id or entity_id in records_by_id:
            return False
        records_by_id[entity_id] = record

    removed_entity_ids = delivery.get("removed_entity_ids", [])
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

    def meaningful_shape_content(content: Dict[str, Any]) -> bool:
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
        if source_ink_evidence is not None:
            if not isinstance(source_text, str) or not _freecad_source_ink_evidence_verified(
                source_ink_evidence,
                source_item_id,
                source_text,
            ):
                return False
            source_ink_verified = True
        zero_ink_terminal = bool(
            source_ink_verified
            and source_ink_evidence.get("classification") == "zero_visible_ink"
        )

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
            if final_type in {"glyphs", "geometry"}:
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
            category = str(record.get("category") or "")
            type_id = str(record.get("type_id") or "")
            content = record.get("content")
            if not isinstance(content, dict):
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
                ):
                    return False
            else:
                if category != "text_representation_objects":
                    return False
                if final_type in {"labels", "text"}:
                    expected_proxy = "Label" if final_type == "labels" else "Text"
                    if (
                        type_id != "App::FeaturePython"
                        or not isinstance(source_text, str)
                        or not source_text
                        or evidence.get("source_text_preserved") is not True
                        or evidence.get("view_style_verified") is not True
                        or content.get("proxy_type") != expected_proxy
                        or content.get("text") != [source_text]
                        or (
                            final_type == "labels"
                            and content.get("custom_text") != [source_text]
                        )
                        or not isinstance(content.get("view_style"), dict)
                        or content["view_style"].get("view_present") is not True
                        or (
                            source_ink_evidence is not None
                            and not _freecad_source_ink_inventory_binding_verified(
                                evidence,
                                content,
                                source_item_id,
                                source_text,
                            )
                        )
                    ):
                        return False
                if final_type in {"3d_text", "glyphs", "geometry"}:
                    if not type_id.startswith(("Part::", "PartDesign::", "Sketcher::")):
                        return False
                    if zero_ink_terminal:
                        exact_source_content = (
                            content.get("string") == [source_text]
                            if final_type == "3d_text"
                            else content.get("source_text") == source_text
                        )
                        if (
                            not exact_source_content
                            or not _freecad_zero_ink_shape_snapshot_verified(content)
                            or not _freecad_source_ink_inventory_binding_verified(
                                evidence,
                                content,
                                source_item_id,
                                source_text,
                            )
                        ):
                            return False
                    elif not meaningful_shape_content(content):
                        return False

        if final_type in {"glyphs", "geometry"} and not zero_ink_terminal:
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
        if final_type in {"glyphs", "geometry"} and zero_ink_terminal:
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
                or type(evidence.get("shape_is_null")) is not bool
                or evidence.get("shape_is_null") != zero_content.get("shape_is_null")
                or evidence.get("zero_visible_ink_verified") is not True
            ):
                return False

        if final_type == "3d_text":
            delivery_records = [live_records[entity_id] for entity_id in delivery_ids]
            support_records = [live_records[entity_id] for entity_id in support_ids]
            if zero_ink_terminal:
                zero_record = delivery_records[0] if len(delivery_records) == 1 else {}
                zero_content = zero_record.get("content") or {}
                if (
                    len(delivery_records) != 1
                    or support_records
                    or not isinstance(source_text, str)
                    or not source_text
                    or evidence.get("source_text_preserved") is not True
                    or zero_content.get("string") != [source_text]
                    or not _freecad_zero_ink_shape_snapshot_verified(zero_content)
                    or not _freecad_source_ink_inventory_binding_verified(
                        evidence,
                        zero_content,
                        source_item_id,
                        source_text,
                    )
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
                    or type(evidence.get("shape_is_null")) is not bool
                    or evidence.get("shape_is_null")
                    != zero_content.get("shape_is_null")
                    or evidence.get("zero_visible_ink_verified") is not True
                ):
                    return False
                continue
            if (
                len(delivery_records) != 1
                or len(support_records) < 2
                or not isinstance(source_text, str)
                or not source_text
                or evidence.get("source_text_preserved") is not True
                or type(evidence.get("solid_count")) is not int
                or evidence.get("solid_count") <= 0
                or isinstance(evidence.get("volume"), bool)
                or not isinstance(evidence.get("volume"), (int, float))
                or not math.isfinite(float(evidence.get("volume")))
                or float(evidence.get("volume")) <= 0.0
            ):
                return False
            extrusion_record = delivery_records[0]
            extrusion_content = extrusion_record.get("content") or {}
            if (
                extrusion_record.get("type_id") != "Part::Extrusion"
                or extrusion_content.get("base_entity_id") not in support_set
            ):
                return False
            shape_string_records = [
                record
                for record in support_records
                if record.get("entity_id") != extrusion_content.get("base_entity_id")
                and str(record.get("type_id") or "").startswith(
                    ("Part::", "PartDesign::")
                )
                and (record.get("content") or {}).get("string") == [source_text]
            ]
            calibrated_support_records = [
                record
                for record in support_records
                if record.get("entity_id") == extrusion_content.get("base_entity_id")
                and str(record.get("type_id") or "").startswith(
                    ("Part::", "PartDesign::")
                )
            ]
            if len(shape_string_records) != 1 or len(calibrated_support_records) != 1:
                return False

    return bool(
        all_live_ids == representation_ids
        and all_terminal_removed_ids.issubset(set(removed_entity_ids))
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
        and record["content"].get("shape_fingerprint_geometry") is not None
        and not _freecad_zero_ink_shape_snapshot_verified(record["content"])
    }
    objects_match = bool(
        len(expected_by_id) == len(expected_objects)
        and len(actual_by_id) == len(actual_objects)
        and len(comparison_by_id) == len(geometry_comparisons)
        and set(expected_by_id) == set(actual_by_id)
        and set(comparison_by_id) == shape_ids
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
        if representation in {"3d_text", "glyphs", "geometry"}:
            zero_ink_shape = _freecad_zero_ink_shape_snapshot_verified(content)
            if zero_ink_shape:
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
                if (
                    content.get("shape_nonempty") is not True
                    or content.get("shape_structure_verified") is not True
                    or not isinstance(topology, dict)
                    or not any(
                        type(count) is int and count > 0 for count in topology.values()
                    )
                    or not isinstance(content.get("shape_digest"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", content.get("shape_digest"))
                    is None
                    or not _freecad_shape_fingerprint_verified(content)
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
    return bool(
        type(result.get("primitives")) is int
        and result.get("primitives") == derived_counts["vector_primitives"]
        and type(result.get("images")) is int
        and result.get("images") == derived_counts["images"]
    )


def _freecad_actual_text_entity_types_verified(
    delivery: Any,
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
    terminals = delivery.get("terminal_attempts")
    if not isinstance(terminals, list) or not terminals:
        return False

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


def build_import_contract_ready(report: "ImportReport") -> Dict[str, Any]:
    """Aggregate report-contract readiness for app/Report Doctor gates."""

    extra = report.extra if isinstance(report.extra, dict) else {}
    result = report.result if isinstance(report.result, dict) else {}
    meta = report.report_meta if isinstance(report.report_meta, dict) else {}

    open_failure = extra.get("open_failure")
    has_stamp = bool(str(meta.get("build_stamp") or "").strip())
    has_crosscheck = "scale_crosscheck" in extra
    host_app = str((report.host or {}).get("app") or "").strip().lower()
    entity_types = extra.get("actual_text_entity_types")
    has_entity_types = isinstance(entity_types, dict)
    status = str(extra.get("result_status") or result.get("status") or "success").lower()
    terminal_failure = extra.get("terminal_failure")
    result_succeeded = status not in {
        "failed",
        "error",
        "incomplete",
        "cancelled",
        "pending",
        "pending_export",
    } and terminal_failure is None
    import_text_enabled = extra.get("import_text") is not False
    delivery = extra.get("text_representation_delivery")
    text_ok = bool(not import_text_enabled or has_entity_types)
    text_delivery_ok = (
        not import_text_enabled
        or (
            isinstance(delivery, dict)
            and delivery.get("required") is True
            and delivery.get("verified") is True
        )
    )
    representation_contract_ok = "representation_contract_violation" not in extra

    inventory = extra.get("actual_host_object_inventory")
    save_reopen = extra.get("save_reopen_inventory")
    if host_app == "freecad":
        inventory_ok = _freecad_host_inventory_verified(inventory, result)
        save_reopen_ok = _freecad_save_reopen_inventory_verified(
            inventory,
            save_reopen,
        )
        text_ok = bool(
            not import_text_enabled
            or _freecad_actual_text_entity_types_verified(
                delivery,
                entity_types,
                result,
            )
        )
        delivery_inventory_binding_ok = bool(
            not import_text_enabled
            or _freecad_delivery_inventory_binding_verified(delivery, inventory)
        )
    else:
        inventory_ok = True
        save_reopen_ok = True
        delivery_inventory_binding_ok = True
    ready = (
        has_stamp
        and has_crosscheck
        and text_ok
        and text_delivery_ok
        and representation_contract_ok
        and inventory_ok
        and save_reopen_ok
        and delivery_inventory_binding_ok
        and result_succeeded
        and open_failure is None
    )

    return {
        "ready": ready,
        "checks": {
            "build_stamp": has_stamp,
            "scale_crosscheck": has_crosscheck,
            "actual_text_entity_types": text_ok,
            "text_delivery": text_delivery_ok,
            "representation_contract": representation_contract_ok,
            "host_object_inventory": inventory_ok,
            "save_reopen_inventory": save_reopen_ok,
            "delivery_inventory_binding": delivery_inventory_binding_ok,
            "result_succeeded": result_succeeded,
            # Compatibility spelling retained for existing Report Doctor clients.
            "successful_result": result_succeeded,
            "no_terminal_failure": terminal_failure is None,
            "no_open_failure": open_failure is None,
        },
        "note": (
            "ready for contract consumers"
            if ready
            else "one or more import report contract checks need review"
        ),
    }


def enrich_import_report_extras(report: "ImportReport") -> None:
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
    report.extra["import_contract_ready"] = build_import_contract_ready(report)


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
    text_mode = _format_text_mode(str(extra.get("text_mode") or ""))
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


#: Bucket names hosts may report as DELIVERED entity counts (TEXTMODE-1).
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
        delivered_buckets: List[str] = []
        for bucket in TEXT_ENTITY_DELIVERED_BUCKETS:
            try:
                value = int(delivered_counts.get(bucket, 0) or 0)
            except (TypeError, ValueError):
                value = 0
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
            info.outline_curve_or_mesh = max(
                int(info.outline_curve_or_mesh or 0),
                int(info.glyph_curve or 0) + int(info.geometry_mesh or 0),
            )
        delivered_total = sum(int(getattr(info, bucket, 0) or 0) for bucket in count_buckets)
        info.count = delivered_total
        if not delivered_buckets:
            info.entity_type = "none"
            info.font_rendered = False
            return info.to_dict()

        delivered_types = {
            _DELIVERED_ENTITY_TYPES[bucket] for bucket in delivered_buckets
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
        return asdict(self)

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
    def from_dict(cls, data: Dict[str, Any]) -> "ImportReport":
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
        importer={"version": importer_version},
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
    enrich_import_report_extras(report)
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
