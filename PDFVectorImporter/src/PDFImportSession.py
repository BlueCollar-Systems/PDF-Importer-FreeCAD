"""Persistent production import-session identity and page-work helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


SESSION_SCHEMA = "bcs.freecad_import_session/1.0"
PROPERTY_GROUP = "PDF Import Resume"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
STATUS_VALUES = {"running", "cancelled", "complete", "failed"}

# Public result/transport controls do not change created model content and must
# not make an otherwise exact session impossible to resume.
NON_CONTENT_FIELDS = {
    "pages",
    "verbose",
    "import_report_path",
    "progress_callback",
    "resume_session_name",
    "auto_resolved_mode",
    "auto_reason",
    "raster_page_count",
    "raster_fallback_reasons",
    "image_plane_count",
    "shapestring_skips",
    "text_mode_fallbacks",
    "text_delivered_counts",
    "text_delivery_attempts",
    "resolved_scale",
    "scale_hints",
    "phase_timings_ms",
    "import_status",
    "work_plan",
}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("import options contain a non-finite number")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_json_value(item) for item in value)
    raise ValueError(f"import option is not persistently serializable: {type(value).__name__}")


def canonical_json(payload: Any) -> str:
    return json.dumps(_json_value(payload), separators=(",", ":"), sort_keys=True)


def content_options(opts: Any) -> dict[str, Any]:
    values = vars(opts) if hasattr(opts, "__dict__") else dict(opts)
    return {
        key: _json_value(value)
        for key, value in sorted(values.items())
        if not key.startswith("_") and key not in NON_CONTENT_FIELDS
    }


def options_sha256(opts: Any) -> str:
    return hashlib.sha256(canonical_json(content_options(opts)).encode("utf-8")).hexdigest()


def _normalize_pages(values: Iterable[int]) -> list[int]:
    pages = list(dict.fromkeys(int(value) for value in values))
    if not pages or any(page < 1 for page in pages):
        raise ValueError("requested pages must contain positive page numbers")
    return pages


def build_identity(
    *,
    source_sha256: str,
    source_name: str,
    opts: Any,
    importer_version: str,
    requested_pages: Iterable[int],
) -> dict[str, Any]:
    source_digest = str(source_sha256).lower()
    if not SHA256_RE.fullmatch(source_digest):
        raise ValueError("source SHA-256 is invalid")
    option_payload = canonical_json(content_options(opts))
    return {
        "schema": SESSION_SCHEMA,
        "source_sha256": source_digest,
        "source_name": Path(str(source_name)).name,
        "options_json": option_payload,
        "options_sha256": hashlib.sha256(option_payload.encode("utf-8")).hexdigest(),
        "importer_version": str(importer_version),
        "requested_pages": _normalize_pages(requested_pages),
    }


_PROPERTIES = (
    ("App::PropertyString", "PDFImportSessionSchema"),
    ("App::PropertyString", "PDFSourceSHA256"),
    ("App::PropertyString", "PDFSourceName"),
    ("App::PropertyString", "PDFOptionsJSON"),
    ("App::PropertyString", "PDFOptionsSHA256"),
    ("App::PropertyString", "PDFImporterVersion"),
    ("App::PropertyString", "PDFRequestedPagesJSON"),
    ("App::PropertyString", "PDFCompletedPagesJSON"),
    ("App::PropertyString", "PDFPageGroupsJSON"),
    ("App::PropertyString", "PDFImportStatus"),
)


def _set_property(host: Any, name: str, value: str) -> None:
    if not hasattr(host, name) and hasattr(host, "addProperty"):
        kind = next(kind for kind, prop_name in _PROPERTIES if prop_name == name)
        host.addProperty(kind, name, PROPERTY_GROUP)
    setattr(host, name, value)


def _validate_identity(identity: Mapping[str, Any]) -> dict[str, Any]:
    source_digest = str(identity.get("source_sha256") or "").lower()
    option_digest = str(identity.get("options_sha256") or "").lower()
    options_json = str(identity.get("options_json") or "")
    if identity.get("schema") != SESSION_SCHEMA:
        raise ValueError("import session schema is invalid")
    if not SHA256_RE.fullmatch(source_digest):
        raise ValueError("import session source SHA-256 is invalid")
    if not SHA256_RE.fullmatch(option_digest):
        raise ValueError("import session options SHA-256 is invalid")
    try:
        decoded_options = json.loads(options_json)
    except json.JSONDecodeError as exc:
        raise ValueError("import session options JSON is invalid") from exc
    canonical_options = canonical_json(decoded_options)
    if hashlib.sha256(canonical_options.encode("utf-8")).hexdigest() != option_digest:
        raise ValueError("import session options SHA-256 does not match options JSON")
    importer_version = str(identity.get("importer_version") or "")
    if not importer_version:
        raise ValueError("import session package identity is missing")
    return {
        "schema": SESSION_SCHEMA,
        "source_sha256": source_digest,
        "source_name": Path(str(identity.get("source_name") or "")).name,
        "options_json": canonical_options,
        "options_sha256": option_digest,
        "importer_version": importer_version,
        "requested_pages": _normalize_pages(identity.get("requested_pages") or []),
    }


def create_session_object(document: Any, identity: Mapping[str, Any]) -> Any:
    exact = _validate_identity(identity)
    host = document.addObject("App::FeaturePython", "PDF_Import_Session")
    try:
        host.Label = f"PDF Import — {exact['source_name']}"
    except (AttributeError, RuntimeError):
        pass
    values = {
        "PDFImportSessionSchema": SESSION_SCHEMA,
        "PDFSourceSHA256": exact["source_sha256"],
        "PDFSourceName": exact["source_name"],
        "PDFOptionsJSON": exact["options_json"],
        "PDFOptionsSHA256": exact["options_sha256"],
        "PDFImporterVersion": exact["importer_version"],
        "PDFRequestedPagesJSON": canonical_json(exact["requested_pages"]),
        "PDFCompletedPagesJSON": "[]",
        "PDFPageGroupsJSON": "{}",
        "PDFImportStatus": "running",
    }
    for name, value in values.items():
        _set_property(host, name, value)
    return host


def read_session_object(host: Any) -> dict[str, Any]:
    identity = _validate_identity(
        {
            "schema": getattr(host, "PDFImportSessionSchema", ""),
            "source_sha256": getattr(host, "PDFSourceSHA256", ""),
            "source_name": getattr(host, "PDFSourceName", ""),
            "options_json": getattr(host, "PDFOptionsJSON", ""),
            "options_sha256": getattr(host, "PDFOptionsSHA256", ""),
            "importer_version": getattr(host, "PDFImporterVersion", ""),
            "requested_pages": json.loads(getattr(host, "PDFRequestedPagesJSON", "[]")),
        }
    )
    try:
        completed = _normalize_pages(
            json.loads(getattr(host, "PDFCompletedPagesJSON", "[]"))
        )
    except ValueError:
        completed = []
    if any(page not in identity["requested_pages"] for page in completed):
        raise ValueError("import session completed pages are outside requested pages")
    page_groups = json.loads(getattr(host, "PDFPageGroupsJSON", "{}"))
    if not isinstance(page_groups, dict):
        raise ValueError("import session page group map is invalid")
    status = str(getattr(host, "PDFImportStatus", ""))
    if status not in STATUS_VALUES:
        raise ValueError("import session status is invalid")
    return {
        **identity,
        "completed_pages": completed,
        "page_groups": {str(key): str(value) for key, value in page_groups.items()},
        "status": status,
        "host": host,
    }


def update_session_object(
    host: Any,
    *,
    status: str,
    completed_pages: Iterable[int],
    page_groups: Mapping[int, str],
) -> None:
    if status not in STATUS_VALUES:
        raise ValueError(f"invalid import session status: {status!r}")
    requested = json.loads(getattr(host, "PDFRequestedPagesJSON", "[]"))
    completed_set = {int(page) for page in completed_pages}
    completed = [int(page) for page in requested if int(page) in completed_set]
    if any(page not in requested for page in completed):
        raise ValueError("completed page is outside the session request")
    groups = {str(int(page)): str(name) for page, name in sorted(page_groups.items())}
    _set_property(host, "PDFCompletedPagesJSON", canonical_json(completed))
    _set_property(host, "PDFPageGroupsJSON", canonical_json(groups))
    _set_property(host, "PDFImportStatus", status)


def validate_completed_page_groups(document: Any, state: Mapping[str, Any]) -> None:
    """Fail closed when persisted completion no longer has its certified group."""
    groups = state.get("page_groups") or {}
    for page in state.get("completed_pages") or []:
        group_name = str(groups.get(str(int(page))) or "")
        host = document.getObject(group_name) if group_name else None
        try:
            is_group = bool(
                host is not None
                and host.isDerivedFrom("App::DocumentObjectGroup")
            )
        except (AttributeError, RuntimeError, TypeError):
            is_group = False
        if not is_group:
            raise ValueError(
                f"import session certified page group is missing for page {int(page)}"
            )


def find_matching_session(document: Any, identity: Mapping[str, Any]) -> Any | None:
    expected = _validate_identity(identity)
    fields = (
        "schema",
        "source_sha256",
        "options_json",
        "options_sha256",
        "importer_version",
        "requested_pages",
    )
    for host in reversed(list(getattr(document, "Objects", []) or [])):
        if getattr(host, "PDFImportSessionSchema", "") != SESSION_SCHEMA:
            continue
        try:
            state = read_session_object(host)
            validate_completed_page_groups(document, state)
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
        if state["status"] not in {"running", "cancelled"}:
            continue
        if all(state[field] == expected[field] for field in fields):
            return host
    return None


def remaining_pages(state: Mapping[str, Any]) -> list[int]:
    complete = {int(page) for page in state.get("completed_pages") or []}
    return [int(page) for page in state.get("requested_pages") or [] if int(page) not in complete]


def page_stack_offsets(
    requested_pages: Iterable[int],
    page_heights: Mapping[int, float],
    *,
    arrangement: str,
    gap_ratio: float,
) -> dict[int, float]:
    pages = list(requested_pages)
    offsets: dict[int, float] = {}
    running = 0.0
    mode = str(arrangement or "spread").lower()
    gap = max(0.0, float(gap_ratio))
    for index, page in enumerate(pages):
        page = int(page)
        if index and mode != "overlay":
            height = float(page_heights.get(page, 0.0) or 0.0)
            ratio = {"touch": 0.0, "compact": min(gap, 0.05), "spread": gap}.get(
                mode, gap
            )
            running += height * (1.0 + ratio)
        offsets[page] = -running
    return offsets


def build_work_plan(profiles: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    pages = []
    for raw in profiles:
        drawing = max(0, int(raw.get("drawing_operations", 0) or 0))
        text = max(0, int(raw.get("text_characters", 0) or 0))
        images = max(0, int(raw.get("image_instances", 0) or 0))
        total = drawing + text + images
        risk = "very_high" if total >= 10_000 else "high" if total >= 3_000 else "normal"
        pages.append(
            {
                "page_number": int(raw["page_number"]),
                "drawing_operations": drawing,
                "text_characters": text,
                "image_instances": images,
                "total_units": total,
                "risk": risk,
            }
        )
    return {
        "pages": pages,
        "total_units": sum(page["total_units"] for page in pages),
        "highest_risk": (
            "very_high"
            if any(page["risk"] == "very_high" for page in pages)
            else "high"
            if any(page["risk"] == "high" for page in pages)
            else "normal"
        ),
    }
