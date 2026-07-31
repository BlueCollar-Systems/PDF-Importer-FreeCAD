# -*- coding: utf-8 -*-
# PDFSvgTextRenderer.py — Pixel-perfect text via SVG glyph paths
# BlueCollar Systems — BUILT. NOT BOUGHT.
#
# Renders text as vector glyph outlines using pdftocairo, or bundled PyMuPDF
# when Poppler is absent.
# Each unique glyph outline is built once as a Part.Shape, then translated.
# Glyphs mode preserves each placed glyph as its own host entity. Geometry mode
# deliberately exposes the placed outline edges as raw Part::Feature entities.
# A renderer failure is explicit; choosing a different representation belongs
# to a separately proven, item-specific fallback policy in the caller.

from __future__ import annotations

import os
import math
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

try:
    import FreeCAD
    from FreeCAD import Vector
    import Part
except ImportError:
    FreeCAD = Part = None
    Vector = None

PDF_PT_TO_MM = 25.4 / 72.0


def find_pdftocairo() -> Optional[str]:
    """Find pdftocairo executable on the system.

    Resolution order:
      1. BC_PDFTOCAIRO_PATH environment variable (manual override)
      2. Plugin bundled bin/ directory — place pdftocairo here to make
         the plugin self-contained without any system install:
           <FreeCAD Mod>/PDFVectorImporter/src/lib/bin/pdftocairo[.exe]
      3. System PATH (shutil.which — cross-platform)
      4. Common Windows locations (MiKTeX, Poppler installs)
    """
    # 1) Explicit override
    env = os.environ.get("BC_PDFTOCAIRO_PATH", "")
    if env and os.path.isfile(env):
        return env

    # 2) Bundled bin/ inside the plugin — highest-priority so a bundled
    #    copy always wins over any system version.
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    _lib_bin = os.path.join(_this_dir, "lib", "bin")
    for _name in ("pdftocairo.exe", "pdftocairo"):
        _candidate = os.path.join(_lib_bin, _name)
        if os.path.isfile(_candidate):
            return _candidate

    # 3) System PATH
    found = shutil.which("pdftocairo")
    if found:
        return found

    # 4) Common Windows locations
    if sys.platform == "win32":
        candidates = []
        for pattern_base in [
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Programs", "MiKTeX", "miktex", "bin", "x64"),
            r"C:\Program Files\MiKTeX\miktex\bin\x64",
            r"C:\Program Files\FreeCAD 1.1\bin",
            r"C:\Program Files\poppler\Library\bin",
            r"C:\Program Files\poppler\bin",
            r"C:\poppler\bin",
            r"C:\tools\poppler\bin",
        ]:
            candidates.append(os.path.join(pattern_base, "pdftocairo.exe"))
        # Common FreeCAD installs (portable / multiple versions)
        for cand in (
            list(_glob_paths(r"C:\Program Files\FreeCAD*\bin\pdftocairo.exe")) +
            list(_glob_paths(r"C:\Program Files\FreeCAD *\bin\pdftocairo.exe"))
        ):
            candidates.append(cand)
        for candidate in candidates:
            if os.path.isfile(candidate):
                return candidate

    return None


def _glob_paths(pattern: str):
    try:
        import glob
        return glob.glob(pattern)
    except Exception:
        return []


class TextRepresentationRenderError(RuntimeError):
    """Requested SVG text representation could not be verified."""

    def __init__(self, reason: str, evidence: Optional[dict] = None):
        self.reason = str(reason or "svg_text_render_failed")
        self.evidence = dict(evidence or {})
        super().__init__(self.reason)


def _host_object_id(obj) -> str:
    try:
        return str(getattr(obj, "Name", "") or getattr(obj, "Label", "") or "")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""


def _document_objects(doc) -> List[object]:
    try:
        return list(getattr(doc, "Objects", []) or [])
    except (AttributeError, RuntimeError, TypeError):
        return []


def _cleanup_owned(doc, parent_group, owned: List[object]) -> dict:
    records = [(obj, _host_object_id(obj)) for obj in owned]
    removed: List[str] = []
    cleanup_errors: List[str] = []
    for obj, obj_id in reversed(records):
        if not obj_id:
            cleanup_errors.append("owned host object has no stable entity id")
            continue
        try:
            if parent_group is not None and hasattr(parent_group, "removeObject"):
                parent_group.removeObject(obj)
            elif parent_group is not None and isinstance(
                getattr(parent_group, "objects", None), list
            ):
                parent_group.objects[:] = [
                    candidate
                    for candidate in parent_group.objects
                    if candidate is not obj
                ]
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            cleanup_errors.append(f"{exc.__class__.__name__}: {exc}")
        try:
            doc.removeObject(obj_id)
            removed.append(obj_id)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            cleanup_errors.append(f"{exc.__class__.__name__}: {exc}")
    try:
        live_objects = list(getattr(doc, "Objects", []) or [])
        live_ids = {_host_object_id(obj) for obj in live_objects}
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        live_ids = {obj_id for _obj, obj_id in records if obj_id}
        cleanup_errors.append(f"{exc.__class__.__name__}: {exc}")
    group_objects = None
    if parent_group is not None:
        try:
            if hasattr(parent_group, "Group"):
                group_objects = list(parent_group.Group or [])
            elif hasattr(parent_group, "objects"):
                group_objects = list(parent_group.objects or [])
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            cleanup_errors.append(f"{exc.__class__.__name__}: {exc}")
    return {
        "removed_entity_ids": removed,
        "cleanup_complete": bool(
            not cleanup_errors
            and all(obj_id and obj_id not in live_ids for _obj, obj_id in records)
            and (
                group_objects is None
                or all(
                    not any(candidate is obj for candidate in group_objects)
                    for obj, _obj_id in records
                )
            )
            and set(removed) == {obj_id for _obj, obj_id in records}
        ),
        "cleanup_errors": cleanup_errors,
    }


def _shape_edge_count(shape) -> int:
    if shape is None:
        return 0
    count_element = getattr(shape, "countElement", None)
    if callable(count_element):
        try:
            return max(0, int(count_element("Edge")))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
    try:
        return len(list(getattr(shape, "Edges", []) or []))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return 0


def _shape_nonempty(shape, *, require_edges: bool = False) -> bool:
    if shape is None:
        return False
    try:
        if bool(getattr(shape, "isNull", lambda: False)()):
            return False
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    if require_edges:
        return _shape_edge_count(shape) > 0
    return True


def _annotate_text_entity(
    obj,
    source_item_id: str,
    representation: str,
    *,
    parent_source_item_id: Optional[str] = None,
) -> None:
    add_property = getattr(obj, "addProperty", None)
    metadata = [
        ("PDFSourceItemId", source_item_id),
        ("PDFRepresentation", representation),
    ]
    if parent_source_item_id is not None:
        metadata.append(("PDFParentSourceItemId", parent_source_item_id))
    for name, value in metadata:
        if not hasattr(obj, name):
            if not callable(add_property):
                raise RuntimeError(f"host cannot store required {name} metadata")
            add_property("App::PropertyString", name, "PDF Import")
        setattr(obj, name, str(value))
        if str(getattr(obj, name, "") or "") != str(value):
            raise RuntimeError(f"host did not preserve required {name} metadata")


def _shape_host_bbox(shape) -> Optional[Tuple[float, float, float, float]]:
    try:
        bound_box = shape.BoundBox
        values = tuple(
            float(getattr(bound_box, name))
            for name in ("XMin", "YMin", "XMax", "YMax")
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    if (
        not all(math.isfinite(value) for value in values)
        or values[2] < values[0]
        or values[3] < values[1]
    ):
        return None
    return values


def _serialize_bbox(values) -> str:
    """Return a stable, locale-independent four-value metadata token."""
    if values is None:
        return ""
    try:
        bbox = tuple(float(value) for value in values)
    except (TypeError, ValueError) as exc:
        raise ValueError("geometry bounds are not numeric") from exc
    if len(bbox) != 4 or not all(math.isfinite(value) for value in bbox):
        raise ValueError("geometry bounds are invalid")
    return ",".join(format(value, ".17g") for value in bbox)


def _source_bbox_to_host_bbox(
    source_bbox: Tuple[float, float, float, float],
    *,
    page_rotation_matrix: Tuple[float, float, float, float, float, float],
    vb_min_x: float,
    vb_min_y: float,
    vb_w: float,
    vb_h: float,
    page_w: float,
    page_h: float,
    x_unit_to_mm: float,
    y_unit_to_mm: float,
    flip_y: bool,
) -> Tuple[float, float, float, float]:
    """Map a canonical PDF bbox through the renderer's exact viewBox transform."""
    x0, y0, x1, y1 = source_bbox
    a, b, c, d, e, f = page_rotation_matrix
    host_points = []
    for canonical_x, canonical_y in (
        (x0, y0),
        (x0, y1),
        (x1, y0),
        (x1, y1),
    ):
        page_x = a * canonical_x + c * canonical_y + e
        page_y = b * canonical_x + d * canonical_y + f
        svg_x = vb_min_x + (page_x / page_w) * vb_w
        svg_y = vb_min_y + (page_y / page_h) * vb_h
        host_x = (svg_x - vb_min_x) * x_unit_to_mm
        if flip_y:
            host_y = (vb_h + vb_min_y - svg_y) * y_unit_to_mm
        else:
            host_y = (svg_y - vb_min_y) * y_unit_to_mm
        host_points.append((host_x, host_y))
    host_x_values = [point[0] for point in host_points]
    host_y_values = [point[1] for point in host_points]
    return (
        min(host_x_values),
        min(host_y_values),
        max(host_x_values),
        max(host_y_values),
    )


def _bboxes_intersect(
    left: Tuple[float, float, float, float],
    right: Tuple[float, float, float, float],
    tolerance: float = 1e-7,
) -> bool:
    return bool(
        max(left[0], right[0]) <= min(left[2], right[2]) + tolerance
        and max(left[1], right[1]) <= min(left[3], right[3]) + tolerance
    )


def _bbox_intersection_area(
    left: Tuple[float, float, float, float],
    right: Tuple[float, float, float, float],
) -> float:
    return max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )


def _visible_text_units(value: str) -> int:
    """Estimate visible glyph demand without treating whitespace as ink."""
    return max(1, sum(1 for character in str(value or "") if not character.isspace()))


def _allocate_tied_placements(
    placement_indices: List[int],
    source_ids: Tuple[str, ...],
    *,
    source_units: Dict[str, int],
    assigned_counts: Dict[str, int],
) -> Dict[str, List[int]]:
    """Split an exact geometric tie in stable source order.

    Duplicate PDF text paints can have identical bounds and therefore no
    geometric discriminator at all. SVG placement order follows paint order,
    while the canonical source manifest follows extraction order. Proportional
    contiguous quotas preserve both orders and prevent the first item rendered
    from consuming every duplicate placement.
    """
    ordered_indices = sorted(int(index) for index in placement_indices)
    ordered_ids = tuple(source_ids)
    allocation = {source_id: [] for source_id in ordered_ids}
    if not ordered_indices or not ordered_ids:
        return allocation

    total = len(ordered_indices)
    minimum = 1 if total >= len(ordered_ids) else 0
    quotas = {source_id: minimum for source_id in ordered_ids}
    remaining_total = total - minimum * len(ordered_ids)
    if remaining_total > 0:
        weights = {
            source_id: max(
                int(source_units.get(source_id, 1))
                - int(assigned_counts.get(source_id, 0))
                - minimum,
                0,
            )
            for source_id in ordered_ids
        }
        if sum(weights.values()) <= 0:
            weights = {
                source_id: max(int(source_units.get(source_id, 1)), 1)
                for source_id in ordered_ids
            }
        weight_total = float(sum(weights.values()))
        raw_shares = {
            source_id: remaining_total * weights[source_id] / weight_total
            for source_id in ordered_ids
        }
        for source_id in ordered_ids:
            quotas[source_id] += int(math.floor(raw_shares[source_id]))
        remainder = total - sum(quotas.values())
        ranked_ids = sorted(
            ordered_ids,
            key=lambda source_id: (
                -(raw_shares[source_id] - math.floor(raw_shares[source_id])),
                ordered_ids.index(source_id),
                source_id,
            ),
        )
        for source_id in ranked_ids[:remainder]:
            quotas[source_id] += 1

    cursor = 0
    for source_id in ordered_ids:
        next_cursor = cursor + quotas[source_id]
        allocation[source_id] = ordered_indices[cursor:next_cursor]
        cursor = next_cursor
    return allocation


def _build_global_placement_assignments(
    placed_glyphs,
    source_manifest,
    *,
    page_num: int,
    pdf_sha256: str,
    page_rotation_matrix: Tuple[float, float, float, float, float, float],
    vb_min_x: float,
    vb_min_y: float,
    vb_w: float,
    vb_h: float,
    page_w: float,
    page_h: float,
    x_unit_to_mm: float,
    y_unit_to_mm: float,
    flip_y: bool,
):
    """Bind every visible SVG placement to one canonical source item.

    The result is computed once per source-bound page cache. Rendering call
    order never participates in assignment.
    """
    if not isinstance(source_manifest, list) or not source_manifest:
        raise ValueError("SVG source item manifest is missing")

    normalized = []
    seen_ids = set()
    seen_orders = set()
    for raw_entry in source_manifest:
        if not isinstance(raw_entry, dict):
            raise ValueError("SVG source item manifest entry is invalid")
        source_id = raw_entry.get("source_item_id")
        source_order = raw_entry.get("source_order")
        source_text = raw_entry.get("text")
        raw_bbox = raw_entry.get("bbox")
        try:
            source_bbox = tuple(float(value) for value in raw_bbox)
        except (TypeError, ValueError) as exc:
            raise ValueError("SVG source item manifest bbox is invalid") from exc
        if (
            not isinstance(source_id, str)
            or not re.fullmatch(r"p\d+:b\d+:l\d+:s\d+", source_id)
            or source_id in seen_ids
            or type(source_order) is not int
            or source_order < 0
            or source_order in seen_orders
            or raw_entry.get("page_number") != int(page_num)
            or raw_entry.get("pdf_sha256") != pdf_sha256
            or not isinstance(source_text, str)
            or not source_text
            or source_text.isspace()
            or not isinstance(raw_bbox, tuple)
            or len(source_bbox) != 4
            or not all(math.isfinite(value) for value in source_bbox)
            or source_bbox[2] <= source_bbox[0]
            or source_bbox[3] <= source_bbox[1]
        ):
            raise ValueError("SVG source item manifest entry is invalid")
        host_bbox = _source_bbox_to_host_bbox(
            source_bbox,
            page_rotation_matrix=page_rotation_matrix,
            vb_min_x=vb_min_x,
            vb_min_y=vb_min_y,
            vb_w=vb_w,
            vb_h=vb_h,
            page_w=page_w,
            page_h=page_h,
            x_unit_to_mm=x_unit_to_mm,
            y_unit_to_mm=y_unit_to_mm,
            flip_y=flip_y,
        )
        normalized.append(
            {
                "source_item_id": source_id,
                "source_order": source_order,
                "source_bbox": source_bbox,
                "host_bbox": host_bbox,
                "text": source_text,
            }
        )
        seen_ids.add(source_id)
        seen_orders.add(source_order)

    normalized.sort(key=lambda entry: (entry["source_order"], entry["source_item_id"]))
    if [entry["source_order"] for entry in normalized] != list(range(len(normalized))):
        raise ValueError("SVG source item manifest order is not contiguous")

    assignments = {entry["source_item_id"]: [] for entry in normalized}
    assigned_counts = {entry["source_item_id"]: 0 for entry in normalized}
    source_units = {
        entry["source_item_id"]: _visible_text_units(entry["text"])
        for entry in normalized
    }
    host_bboxes = {
        entry["source_item_id"]: entry["host_bbox"] for entry in normalized
    }
    grid_dimension = max(8, min(64, int(math.ceil(math.sqrt(len(normalized))))))
    grid_min_x = min(entry["host_bbox"][0] for entry in normalized)
    grid_min_y = min(entry["host_bbox"][1] for entry in normalized)
    grid_max_x = max(entry["host_bbox"][2] for entry in normalized)
    grid_max_y = max(entry["host_bbox"][3] for entry in normalized)
    grid_cell_w = max((grid_max_x - grid_min_x) / grid_dimension, 1e-9)
    grid_cell_h = max((grid_max_y - grid_min_y) / grid_dimension, 1e-9)

    def bbox_grid_cells(bbox):
        x0 = max(
            0,
            min(
                grid_dimension - 1,
                int(math.floor((bbox[0] - grid_min_x) / grid_cell_w)),
            ),
        )
        y0 = max(
            0,
            min(
                grid_dimension - 1,
                int(math.floor((bbox[1] - grid_min_y) / grid_cell_h)),
            ),
        )
        x1 = max(
            0,
            min(
                grid_dimension - 1,
                int(math.floor((bbox[2] - grid_min_x) / grid_cell_w)),
            ),
        )
        y1 = max(
            0,
            min(
                grid_dimension - 1,
                int(math.floor((bbox[3] - grid_min_y) / grid_cell_h)),
            ),
        )
        return (
            (grid_x, grid_y)
            for grid_x in range(min(x0, x1), max(x0, x1) + 1)
            for grid_y in range(min(y0, y1), max(y0, y1) + 1)
        )

    source_grid = {}
    for normalized_index, entry in enumerate(normalized):
        for grid_cell in bbox_grid_cells(entry["host_bbox"]):
            source_grid.setdefault(grid_cell, []).append(normalized_index)
    tied_groups: Dict[Tuple[str, ...], List[int]] = {}
    unmatched_indices = []
    placement_bboxes = {}

    for placement_index, _gid, placed_shape in placed_glyphs:
        placed_bbox = _shape_host_bbox(placed_shape)
        if placed_bbox is None:
            raise ValueError(f"SVG placement {placement_index} has no finite bounds")
        placement_bboxes[int(placement_index)] = placed_bbox
        placed_area = max(
            (placed_bbox[2] - placed_bbox[0]) * (placed_bbox[3] - placed_bbox[1]),
            1e-12,
        )
        placed_x = (placed_bbox[0] + placed_bbox[2]) / 2.0
        placed_y = (placed_bbox[1] + placed_bbox[3]) / 2.0
        candidates = []
        candidate_entry_indices = set()
        for grid_cell in bbox_grid_cells(placed_bbox):
            candidate_entry_indices.update(source_grid.get(grid_cell, ()))
        for normalized_index in sorted(candidate_entry_indices):
            entry = normalized[normalized_index]
            host_bbox = entry["host_bbox"]
            overlap = _bbox_intersection_area(placed_bbox, host_bbox)
            if overlap <= 1e-12:
                continue
            host_x = (host_bbox[0] + host_bbox[2]) / 2.0
            host_y = (host_bbox[1] + host_bbox[3]) / 2.0
            host_area = max(
                (host_bbox[2] - host_bbox[0]) * (host_bbox[3] - host_bbox[1]),
                1e-12,
            )
            center_inside = (
                host_bbox[0] <= placed_x <= host_bbox[2]
                and host_bbox[1] <= placed_y <= host_bbox[3]
            )
            # Quantization makes exact geometric ties deterministic across
            # host floating-point implementations without merging visibly
            # different candidates.
            score = (
                1 if center_inside else 0,
                round(overlap / placed_area, 12),
                round(-math.hypot(placed_x - host_x, placed_y - host_y), 12),
                round(-host_area, 12),
            )
            candidates.append((score, entry["source_order"], entry["source_item_id"]))
        if not candidates:
            unmatched_indices.append(int(placement_index))
            continue
        best_score = max(candidate[0] for candidate in candidates)
        tied = tuple(
            source_id
            for _score, _order, source_id in sorted(
                (candidate for candidate in candidates if candidate[0] == best_score),
                key=lambda candidate: (candidate[1], candidate[2]),
            )
        )
        if len(tied) == 1:
            assignments[tied[0]].append(int(placement_index))
            assigned_counts[tied[0]] += 1
        else:
            tied_groups.setdefault(tied, []).append(int(placement_index))

    for source_ids, placement_indices in sorted(
        tied_groups.items(), key=lambda entry: min(entry[1])
    ):
        allocation = _allocate_tied_placements(
            placement_indices,
            source_ids,
            source_units=source_units,
            assigned_counts=assigned_counts,
        )
        for source_id, indices in allocation.items():
            assignments[source_id].extend(indices)
            assigned_counts[source_id] += len(indices)

    # PDF extractors sometimes split a stacked fraction into overlapping
    # source spans (for example ``12`` and ``/``), while the SVG renderer
    # reports all three outlines under the larger span's bounds. Preserve the
    # one-to-one global assignment but move only a donor's proven surplus to
    # an entirely empty overlapping source item. This is deliberately narrow:
    # ordinary spans, ligatures, and non-overlapping text are untouched.
    source_order_by_id = {
        entry["source_item_id"]: entry["source_order"] for entry in normalized
    }
    for target_entry in normalized:
        target_id = target_entry["source_item_id"]
        if assigned_counts[target_id] != 0 or source_units[target_id] <= 0:
            continue
        donor_ids = [
            donor_entry["source_item_id"]
            for donor_entry in normalized
            if (
                donor_entry["source_item_id"] != target_id
                and assigned_counts[donor_entry["source_item_id"]]
                > source_units[donor_entry["source_item_id"]]
                and _bbox_intersection_area(
                    donor_entry["host_bbox"], target_entry["host_bbox"]
                )
                > 1e-12
            )
        ]
        if not donor_ids:
            continue
        target_bbox = target_entry["host_bbox"]
        target_x = (target_bbox[0] + target_bbox[2]) / 2.0
        target_y = (target_bbox[1] + target_bbox[3]) / 2.0
        candidates = []
        for donor_id in donor_ids:
            for placement_index in assignments[donor_id]:
                placed_bbox = placement_bboxes[placement_index]
                placed_x = (placed_bbox[0] + placed_bbox[2]) / 2.0
                placed_y = (placed_bbox[1] + placed_bbox[3]) / 2.0
                candidates.append(
                    (
                        math.hypot(placed_x - target_x, placed_y - target_y),
                        source_order_by_id[donor_id],
                        int(placement_index),
                        donor_id,
                    )
                )
        if not candidates:
            continue
        _distance, _donor_order, placement_index, donor_id = min(candidates)
        assignments[donor_id].remove(placement_index)
        assignments[target_id].append(placement_index)
        assigned_counts[donor_id] -= 1
        assigned_counts[target_id] += 1

    for indices in assignments.values():
        indices.sort()
    assigned_indices = [index for indices in assignments.values() for index in indices]
    if len(assigned_indices) != len(set(assigned_indices)):
        raise ValueError("SVG placement assignment is not one-to-one")
    return assignments, host_bboxes, sorted(unmatched_indices)


def render_text(pdf_path: str, page_num: int, page_h: float,
                scale: float, page_w: Optional[float] = None,
                fc_doc=None, parent_group=None,
                flip_y: bool = True,
                representation: str = "glyphs",
                source_item: Optional[dict] = None,
                requested_representation: Optional[str] = None,
                 page_rotation_matrix: Optional[
                     Tuple[float, float, float, float, float, float]
                 ] = None,
                 render_cache: Optional[dict] = None,
                 defer_recompute: bool = False) -> dict:
    """Render and verify the explicitly requested SVG text representation.

    Glyphs creates one placed-outline entity per glyph. Geometry preserves
    every raw outline edge in one source-item compound. The different object
    boundaries keep the two modes distinct without making large text pages
    create hundreds of thousands of individual FreeCAD document objects.
    """
    representation = str(representation or "").strip().lower()
    if representation not in {"glyphs", "geometry"}:
        raise TextRepresentationRenderError(
            "unsupported_text_representation",
            {"requested_type": representation},
        )
    item_filter_requested = (
        source_item is not None or requested_representation is not None
    )
    item_filter = None
    if item_filter_requested:
        requested_type = str(requested_representation or "").strip().lower()
        try:
            item_page = source_item.get("page_number")
            source_item_id = source_item.get("source_item_id")
            item_requested_type = source_item.get("requested_type")
            raw_bbox = source_item.get("bbox")
            source_bbox = tuple(float(value) for value in raw_bbox)
            rotation_matrix = (
                tuple(float(value) for value in page_rotation_matrix)
                if page_rotation_matrix is not None
                else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            source_bbox = ()
            rotation_matrix = ()
            raw_bbox = None
            item_page = None
            source_item_id = None
            item_requested_type = None
        expected_source_prefix = f"p{int(page_num)}:b"
        if (
            not isinstance(source_item, dict)
            or requested_type not in {
                "text", "labels", "3d_text", "glyphs", "geometry"
            }
            or item_requested_type != requested_type
            or type(item_page) is not int
            or item_page != int(page_num)
            or not isinstance(source_item_id, str)
            or not source_item_id.startswith(expected_source_prefix)
            or not re.fullmatch(r"p\d+:b\d+:l\d+:s\d+", source_item_id)
            or not isinstance(raw_bbox, tuple)
            or len(raw_bbox) != 4
            or len(source_bbox) != 4
            or not all(math.isfinite(value) for value in source_bbox)
            or len(rotation_matrix) != 6
            or not all(math.isfinite(value) for value in rotation_matrix)
            or source_bbox[2] <= source_bbox[0]
            or source_bbox[3] <= source_bbox[1]
        ):
            raise TextRepresentationRenderError(
                "invalid_svg_source_item_filter",
                {
                    "requested_type": requested_type,
                    "attempted_type": representation,
                    "created_entity_ids": [],
                    "removed_entity_ids": [],
                    "cleanup_complete": True,
                },
            )
        item_filter = {
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "source_bbox": source_bbox,
            "page_rotation_matrix": rotation_matrix,
        }
    cache = None
    if render_cache is not None:
        if not isinstance(render_cache, dict):
            raise TextRepresentationRenderError(
                "invalid_svg_render_cache",
                {"requested_type": representation},
            )
        cache = render_cache
        source_digest = (
            str(source_item.get("pdf_sha256") or "")
            if isinstance(source_item, dict)
            else ""
        )
        cache_binding = {
            "pdf_path": os.path.normcase(os.path.abspath(str(pdf_path))),
            "pdf_sha256": source_digest,
            "page_number": int(page_num),
            "page_height": float(page_h),
            "page_width": float(page_w) if page_w is not None else None,
            "scale": float(scale),
            "flip_y": bool(flip_y),
            "page_rotation_matrix": (
                tuple(float(value) for value in page_rotation_matrix)
                if page_rotation_matrix is not None
                else (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
            ),
        }
        prior_binding = cache.get("source_binding")
        if prior_binding is not None and prior_binding != cache_binding:
            raise TextRepresentationRenderError(
                "svg_cache_source_mismatch",
                {
                    "requested_type": representation,
                    "source_item_id": (
                        item_filter["source_item_id"] if item_filter else None
                    ),
                    "created_entity_ids": [],
                    "removed_entity_ids": [],
                    "cleanup_complete": True,
                },
            )
        cache["source_binding"] = cache_binding
        claimed = cache.setdefault("claimed_placement_indices", set())
        if not isinstance(claimed, set) or any(
            type(index) is not int or index < 0 for index in claimed
        ):
            raise TextRepresentationRenderError(
                "invalid_svg_render_cache",
                {"requested_type": representation},
            )
    doc = fc_doc or (getattr(FreeCAD, "ActiveDocument", None) if FreeCAD else None)
    if doc is None:
        raise TextRepresentationRenderError(
            "freecad_document_unavailable",
            {"requested_type": representation},
        )
    if Part is None or Vector is None:
        raise TextRepresentationRenderError(
            "freecad_part_api_unavailable",
            {"requested_type": representation},
        )
    try:
        baseline_objects = {
            id(host_obj) for host_obj in list(getattr(doc, "Objects", []) or [])
        }
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise TextRepresentationRenderError(
            "host_ownership_snapshot_failed",
            {
                "requested_type": (
                    item_filter["requested_type"] if item_filter else representation
                ),
                "attempted_type": representation,
                "exception": f"{exc.__class__.__name__}: {exc}",
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
        ) from exc

    svg = cache.get("svg") if cache is not None else None
    exe = None
    renderer_name = (
        str(cache.get("renderer_name") or "pymupdf")
        if cache is not None and svg is not None
        else "pymupdf"
    )
    if svg is not None:
        pass
    else:
        exe = find_pdftocairo()
        renderer_name = "pdftocairo" if exe else "pymupdf"
        if exe:
            svg = _render_svg_with_pdftocairo(exe, pdf_path, page_num)
        else:
            if FreeCAD:
                FreeCAD.Console.PrintMessage(
                    "PDFSvgTextRenderer: pdftocairo not found — using bundled "
                    "PyMuPDF SVG text fallback.\n"
                )
            svg = _render_svg_with_pymupdf(pdf_path, page_num)

    if not svg:
        raise TextRepresentationRenderError(
            "svg_renderer_unavailable",
            {"requested_type": representation, "renderer": renderer_name},
        )
    if _svg_too_large(svg):
        if FreeCAD:
            FreeCAD.Console.PrintWarning(
                f"PDFSvgTextRenderer: page {page_num} SVG text payload is too large — "
                "requested representation was not created.\n"
            )
        raise TextRepresentationRenderError(
            "svg_payload_too_large",
            {
                "requested_type": representation,
                "renderer": renderer_name,
                "svg_bytes": len(svg.encode("utf-8", "ignore")),
            },
        )
    if cache is not None:
        cache["svg"] = svg
        cache["renderer_name"] = renderer_name

    # Parse page-wide SVG structures exactly once. Canonical item delivery
    # calls this function for every span, so reparsing the same XML payload
    # turned dense pages into an O(source-items × SVG-size) workload.
    cached_viewbox = cache.get("svg_viewbox") if cache is not None else None
    if cached_viewbox is not None:
        try:
            vb_min_x, vb_min_y, vb_w, vb_h = (
                float(value) for value in cached_viewbox
            )
        except (TypeError, ValueError):
            raise TextRepresentationRenderError(
                "invalid_svg_render_cache",
                {"requested_type": representation},
            )
    else:
        vb_min_x, vb_min_y, vb_w, vb_h = _parse_viewbox(svg)
        if vb_w <= 0 or vb_h <= 0:
            svg_w = _parse_svg_dim(
                svg, "width", page_w if page_w and page_w > 0 else page_h
            )
            svg_h = _parse_svg_dim(svg, "height", page_h)
            vb_min_x, vb_min_y, vb_w, vb_h = (
                0.0,
                0.0,
                float(svg_w),
                float(svg_h),
            )
        if cache is not None:
            cache["svg_viewbox"] = (vb_min_x, vb_min_y, vb_w, vb_h)

    page_w_eff = float(page_w) if page_w and page_w > 0 else float(vb_w)
    page_h_eff = float(page_h) if page_h and page_h > 0 else float(vb_h)
    x_unit_to_mm = (page_w_eff * scale) / max(vb_w, 1e-12)
    y_unit_to_mm = (page_h_eff * scale) / max(vb_h, 1e-12)

    # Parse all glyph definitions so an intentional empty outline (normally a
    # space) is distinguishable from a nonempty outline that failed parsing.
    all_glyph_defs = cache.get("all_glyph_defs") if cache is not None else None
    if all_glyph_defs is None:
        all_glyph_defs = _parse_all_glyph_defs(svg)
        if cache is not None:
            cache["all_glyph_defs"] = all_glyph_defs
    elif not isinstance(all_glyph_defs, dict):
        raise TextRepresentationRenderError(
            "invalid_svg_render_cache",
            {"requested_type": representation},
        )
    glyph_defs = {
        glyph_id: path_d
        for glyph_id, path_d in all_glyph_defs.items()
        if path_d.strip()
    }

    # Parse use placements
    placements = cache.get("placements") if cache is not None else None
    if placements is None:
        placements = _parse_use_placements(svg)
        if cache is not None:
            cache["placements"] = placements
    elif not isinstance(placements, list):
        raise TextRepresentationRenderError(
            "invalid_svg_render_cache",
            {"requested_type": representation},
        )

    if not placements:
        raise TextRepresentationRenderError(
            "svg_has_no_glyph_placements",
            {"requested_type": representation, "renderer": renderer_name},
        )

    # Build Part.Shape for each unique glyph
    cached_shapes = cache.get("glyph_shapes") if cache is not None else None
    if cached_shapes is not None and not isinstance(cached_shapes, dict):
        raise TextRepresentationRenderError(
            "invalid_svg_render_cache",
            {"requested_type": representation},
        )
    if cached_shapes is not None:
        glyph_shapes = cached_shapes
    else:
        glyph_shapes: Dict[str, Part.Shape] = {}
        for gid, path_d in glyph_defs.items():
            edges = _svg_path_to_edges(path_d, x_unit_to_mm, y_unit_to_mm)
            if edges:
                try:
                    compound = Part.makeCompound(edges)
                    glyph_shapes[gid] = compound
                except (RuntimeError, ValueError, TypeError):
                    pass
        if cache is not None:
            cache["glyph_shapes"] = glyph_shapes

    # Place all glyphs
    cached_placed = cache.get("placed_glyphs") if cache is not None else None
    if cached_placed is not None:
        placed_glyphs = list(cached_placed)
        failed_placement_indices = list(cache.get("failed_placement_indices") or [])
        empty_placement_indices = list(cache.get("empty_placement_indices") or [])
    else:
        placed_glyphs = []
        failed_placement_indices: List[int] = []
        empty_placement_indices: List[int] = []

        for placement_index, (gid, use_x, use_y, matrix) in enumerate(placements):
            shape = glyph_shapes.get(gid)
            if shape is None:
                if gid in all_glyph_defs and not all_glyph_defs[gid].strip():
                    empty_placement_indices.append(placement_index)
                else:
                    failed_placement_indices.append(placement_index)
                continue

            # SVG coords → FreeCAD coords
            # Glyph use positions are in viewBox coordinates.
            placed = None
            if matrix and len(matrix) >= 6:
                a, b, c, d, e, f = [float(v) for v in matrix[:6]]
                e += float(use_x)
                f += float(use_y)
                tx = (e - vb_min_x) * x_unit_to_mm
                ty = (vb_h + vb_min_y - f) * y_unit_to_mm if flip_y else (f - vb_min_y) * y_unit_to_mm

                ratio_xy = (x_unit_to_mm / y_unit_to_mm) if abs(y_unit_to_mm) > 1e-12 else 1.0
                ratio_yx = (y_unit_to_mm / x_unit_to_mm) if abs(x_unit_to_mm) > 1e-12 else 1.0
                a11 = a
                a12 = -c * ratio_xy
                a21 = -b * ratio_yx
                a22 = d
                placed = _shape_affine_2d(shape, a11, a12, a21, a22, tx, ty)
            else:
                tx = (float(use_x) - vb_min_x) * x_unit_to_mm
                ty = ((vb_h + vb_min_y - float(use_y)) * y_unit_to_mm) if flip_y else ((float(use_y) - vb_min_y) * y_unit_to_mm)
                try:
                    placed = shape.translated(Vector(tx, ty, 0.0))
                except (AttributeError, RuntimeError, TypeError):
                    placed = None

            try:
                if placed is not None:
                    placed_glyphs.append((placement_index, gid, placed))
                else:
                    failed_placement_indices.append(placement_index)
            except (AttributeError, RuntimeError, TypeError):
                failed_placement_indices.append(placement_index)
        if cache is not None:
            cache["placed_glyphs"] = list(placed_glyphs)
            cache["failed_placement_indices"] = list(failed_placement_indices)
            cache["empty_placement_indices"] = list(empty_placement_indices)

    if failed_placement_indices:
        raise TextRepresentationRenderError(
            "svg_item_placement_unverified",
            {
                "requested_type": (
                    item_filter["requested_type"] if item_filter else representation
                ),
                "attempted_type": representation,
                "source_item_id": (
                    item_filter["source_item_id"] if item_filter else None
                ),
                "renderer": renderer_name,
                "failed_placement_indices": failed_placement_indices,
                "empty_placement_indices": empty_placement_indices,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
        )

    if not placed_glyphs:
        raise TextRepresentationRenderError(
            "svg_glyph_outlines_unavailable",
            {
                "requested_type": representation,
                "renderer": renderer_name,
                "placement_count": len(placements),
            },
        )

    item_filter_evidence = None
    if item_filter is not None:
        try:
            if (
                not all(
                    math.isfinite(value) and value > 0.0
                    for value in (page_w_eff, page_h_eff, x_unit_to_mm, y_unit_to_mm)
                )
            ):
                raise ValueError("SVG item filter dimensions are invalid")
            host_filter_bbox = _source_bbox_to_host_bbox(
                item_filter["source_bbox"],
                page_rotation_matrix=item_filter["page_rotation_matrix"],
                vb_min_x=vb_min_x,
                vb_min_y=vb_min_y,
                vb_w=vb_w,
                vb_h=vb_h,
                page_w=page_w_eff,
                page_h=page_h_eff,
                x_unit_to_mm=x_unit_to_mm,
                y_unit_to_mm=y_unit_to_mm,
                flip_y=flip_y,
            )
        except (RuntimeError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise TextRepresentationRenderError(
                "svg_item_filter_transform_failed",
                {
                    "requested_type": item_filter["requested_type"],
                    "attempted_type": representation,
                    "source_item_id": item_filter["source_item_id"],
                    "exception": f"{exc.__class__.__name__}: {exc}",
                    "created_entity_ids": [],
                    "removed_entity_ids": [],
                    "cleanup_complete": True,
                },
            ) from exc

        claimed_indices = (
            cache["claimed_placement_indices"] if cache is not None else set()
        )
        matched_glyphs = []
        assignment_method = "bounded_greedy_item_filter"
        source_manifest = cache.get("source_item_manifest") if cache is not None else None
        if source_manifest is not None:
            assignments = cache.get("placement_assignments")
            host_bboxes = cache.get("source_item_host_bboxes")
            unmatched_indices = cache.get("unmatched_placement_indices")
            if assignments is None:
                try:
                    assignments, host_bboxes, unmatched_indices = (
                        _build_global_placement_assignments(
                            placed_glyphs,
                            source_manifest,
                            page_num=int(page_num),
                            pdf_sha256=str(cache["source_binding"]["pdf_sha256"]),
                            page_rotation_matrix=item_filter["page_rotation_matrix"],
                            vb_min_x=vb_min_x,
                            vb_min_y=vb_min_y,
                            vb_w=vb_w,
                            vb_h=vb_h,
                            page_w=page_w_eff,
                            page_h=page_h_eff,
                            x_unit_to_mm=x_unit_to_mm,
                            y_unit_to_mm=y_unit_to_mm,
                            flip_y=flip_y,
                        )
                    )
                except (KeyError, RuntimeError, TypeError, ValueError) as exc:
                    raise TextRepresentationRenderError(
                        "svg_global_assignment_failed",
                        {
                            "requested_type": item_filter["requested_type"],
                            "attempted_type": representation,
                            "source_item_id": item_filter["source_item_id"],
                            "exception": f"{exc.__class__.__name__}: {exc}",
                            "created_entity_ids": [],
                            "removed_entity_ids": [],
                            "cleanup_complete": True,
                        },
                    ) from exc
                cache["placement_assignments"] = assignments
                cache["source_item_host_bboxes"] = host_bboxes
                cache["unmatched_placement_indices"] = unmatched_indices
            if (
                not isinstance(assignments, dict)
                or not isinstance(host_bboxes, dict)
                or not isinstance(unmatched_indices, list)
                or item_filter["source_item_id"] not in assignments
                or item_filter["source_item_id"] not in host_bboxes
            ):
                raise TextRepresentationRenderError(
                    "invalid_svg_render_cache",
                    {
                        "requested_type": item_filter["requested_type"],
                        "attempted_type": representation,
                        "source_item_id": item_filter["source_item_id"],
                        "created_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                    },
                )
            if unmatched_indices:
                raise TextRepresentationRenderError(
                    "svg_global_assignment_unmatched",
                    {
                        "requested_type": item_filter["requested_type"],
                        "attempted_type": representation,
                        "source_item_id": item_filter["source_item_id"],
                        "unmatched_placement_indices": list(unmatched_indices),
                        "created_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                    },
                )
            assigned_host_bbox = tuple(host_bboxes[item_filter["source_item_id"]])
            if len(assigned_host_bbox) != 4 or any(
                abs(float(left) - float(right)) > 1e-7
                for left, right in zip(
                    assigned_host_bbox, host_filter_bbox, strict=True
                )
            ):
                raise TextRepresentationRenderError(
                    "svg_manifest_item_mismatch",
                    {
                        "requested_type": item_filter["requested_type"],
                        "attempted_type": representation,
                        "source_item_id": item_filter["source_item_id"],
                        "created_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                    },
                )
            assigned_indices = list(assignments[item_filter["source_item_id"]])
            if any(
                type(index) is not int or index < 0 or index in claimed_indices
                for index in assigned_indices
            ):
                raise TextRepresentationRenderError(
                    "svg_assignment_reuse_detected",
                    {
                        "requested_type": item_filter["requested_type"],
                        "attempted_type": representation,
                        "source_item_id": item_filter["source_item_id"],
                        "assigned_placement_indices": assigned_indices,
                        "created_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                    },
                )
            placed_by_index = {
                placement_index: (placement_index, gid, placed_shape)
                for placement_index, gid, placed_shape in placed_glyphs
            }
            if any(index not in placed_by_index for index in assigned_indices):
                raise TextRepresentationRenderError(
                    "svg_assignment_missing_placement",
                    {
                        "requested_type": item_filter["requested_type"],
                        "attempted_type": representation,
                        "source_item_id": item_filter["source_item_id"],
                        "created_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                    },
                )
            matched_glyphs = [placed_by_index[index] for index in assigned_indices]
            assignment_method = "source_manifest_global_bounded_v1"
        else:
            for placement_index, gid, placed_shape in placed_glyphs:
                placed_bbox = _shape_host_bbox(placed_shape)
                if placed_bbox is None:
                    raise TextRepresentationRenderError(
                        "svg_item_glyph_bounds_unavailable",
                        {
                            "requested_type": item_filter["requested_type"],
                            "attempted_type": representation,
                            "source_item_id": item_filter["source_item_id"],
                            "placement_index": placement_index,
                            "created_entity_ids": [],
                            "removed_entity_ids": [],
                            "cleanup_complete": True,
                        },
                    )
                if (
                    placement_index not in claimed_indices
                    and _bboxes_intersect(placed_bbox, host_filter_bbox)
                ):
                    matched_glyphs.append((placement_index, gid, placed_shape))

        matched_indices = [entry[0] for entry in matched_glyphs]
        item_filter_evidence = {
            "source_item_bbox": item_filter["source_bbox"],
            "host_filter_bbox": host_filter_bbox,
            "matched_placement_indices": matched_indices,
            "assignment_method": assignment_method,
        }
        if source_manifest is not None:
            item_filter_evidence["global_unmatched_placement_indices"] = list(
                unmatched_indices or []
            )
        if empty_placement_indices:
            item_filter_evidence["empty_placement_indices"] = list(
                empty_placement_indices
            )
        if not matched_glyphs:
            raster_source_placements = (
                cache.get("raster_source_placements") if cache is not None else None
            )
            if raster_source_placements is None:
                raster_source_placements = _parse_raster_source_placements(svg)
                if cache is not None:
                    cache["raster_source_placements"] = list(
                        raster_source_placements
                    )
            matching_raster_sources = []
            for source_id, svg_bbox in list(raster_source_placements or []):
                host_points = []
                for svg_x, svg_y in (
                    (svg_bbox[0], svg_bbox[1]),
                    (svg_bbox[0], svg_bbox[3]),
                    (svg_bbox[2], svg_bbox[1]),
                    (svg_bbox[2], svg_bbox[3]),
                ):
                    host_x = (float(svg_x) - vb_min_x) * x_unit_to_mm
                    host_y = (
                        (vb_h + vb_min_y - float(svg_y)) * y_unit_to_mm
                        if flip_y
                        else (float(svg_y) - vb_min_y) * y_unit_to_mm
                    )
                    host_points.append((host_x, host_y))
                host_bbox = (
                    min(point[0] for point in host_points),
                    min(point[1] for point in host_points),
                    max(point[0] for point in host_points),
                    max(point[1] for point in host_points),
                )
                if _bbox_intersection_area(host_bbox, host_filter_bbox) > 1e-12:
                    matching_raster_sources.append((str(source_id), host_bbox))
            if matching_raster_sources:
                raise TextRepresentationRenderError(
                    "svg_item_raster_source_only",
                    {
                        "requested_type": item_filter["requested_type"],
                        "attempted_type": representation,
                        "source_item_id": item_filter["source_item_id"],
                        "renderer": renderer_name,
                        "placement_count": len(placements),
                        **item_filter_evidence,
                        "raster_source_ids": [
                            source_id for source_id, _bbox in matching_raster_sources
                        ],
                        "raster_source_host_bboxes": [
                            host_bbox for _source_id, host_bbox in matching_raster_sources
                        ],
                        "created_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                    },
                )
            raise TextRepresentationRenderError(
                    (
                        "svg_item_assignment_empty"
                        if source_manifest is not None
                        else "svg_item_filter_empty"
                    ),
                {
                    "requested_type": item_filter["requested_type"],
                    "attempted_type": representation,
                    "source_item_id": item_filter["source_item_id"],
                    "renderer": renderer_name,
                    "placement_count": len(placements),
                    **item_filter_evidence,
                    "created_entity_ids": [],
                    "removed_entity_ids": [],
                    "cleanup_complete": True,
                },
            )
        placed_glyphs = matched_glyphs

    owned: List[object] = []
    created_ids: List[str] = []
    child_source_ids: List[str] = []
    attempts: List[dict] = []
    raw_edge_count = 0
    geometry_shapes: List[object] = []
    creation_started = False

    def claim_new_object(obj, role: str) -> None:
        if obj is None:
            raise RuntimeError(f"{role} factory returned no host object")
        if id(obj) in baseline_objects:
            raise RuntimeError(f"{role} factory returned a pre-existing host object")
        if any(candidate is obj for candidate in owned):
            raise RuntimeError(f"{role} factory returned a reused host object")
        owned.append(obj)

    try:
        for glyph_index, _gid, placed_shape in placed_glyphs:
            parent_source_item_id = (
                item_filter["source_item_id"] if item_filter is not None else None
            )
            glyph_source_id = (
                f"{parent_source_item_id}:g{glyph_index}"
                if parent_source_item_id is not None
                else f"p{int(page_num)}:g{glyph_index}"
            )
            glyph_created_ids: List[str] = []
            if representation == "glyphs":
                if not _shape_nonempty(placed_shape, require_edges=True):
                    raise RuntimeError("placed glyph shape is empty")
                creation_started = True
                obj = doc.addObject(
                    "Part::Feature",
                    f"Text_Glyph_p{int(page_num)}_g{glyph_index}",
                )
                claim_new_object(obj, "glyph")
                obj.Shape = placed_shape
                if not _shape_nonempty(getattr(obj, "Shape", None), require_edges=True):
                    raise RuntimeError("host glyph entity failed shape verification")
                _annotate_text_entity(
                    obj,
                    glyph_source_id,
                    representation,
                    parent_source_item_id=parent_source_item_id,
                )
                glyph_created_ids.append(_host_object_id(obj))
                child_source_ids.append(glyph_source_id)
                if parent_group is not None:
                    parent_group.addObject(obj)
            else:
                placed_edge_count = _shape_edge_count(placed_shape)
                if placed_edge_count <= 0:
                    raise RuntimeError("placed glyph has no raw geometry edges")
                geometry_shapes.append(placed_shape)
                raw_edge_count += placed_edge_count

            created_ids.extend(glyph_created_ids)
            if item_filter is None and representation == "glyphs":
                attempts.append(
                    {
                        "source_item_id": glyph_source_id,
                        "requested_type": representation,
                        "attempted_type": representation,
                        "final_type": representation,
                        "outcome": "verified",
                        "reason": f"requested {representation} delivered",
                        "created_entity_ids": glyph_created_ids,
                        "support_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                        "evidence": {
                            "renderer": renderer_name,
                            "host_entity_type": "Part::Feature",
                            "raw_edge_count": (
                                len(glyph_created_ids)
                                if representation == "geometry"
                                else None
                            ),
                        },
                    }
                )

        if representation == "geometry":
            if not geometry_shapes or raw_edge_count <= 0:
                raise RuntimeError("raw geometry edge collection is incomplete")
            parent_source_item_id = (
                item_filter["source_item_id"] if item_filter is not None else None
            )
            geometry_source_id = (
                f"{parent_source_item_id}:geometry"
                if parent_source_item_id is not None
                else f"p{int(page_num)}:geometry"
            )
            creation_started = True
            obj = doc.addObject(
                "Part::Feature",
                f"Text_Geometry_p{int(page_num)}",
            )
            claim_new_object(obj, "source-item geometry compound")
            obj.Shape = Part.makeCompound(geometry_shapes)
            stored_shape = getattr(obj, "Shape", None)
            stored_edge_count = _shape_edge_count(stored_shape)
            if (
                not _shape_nonempty(stored_shape, require_edges=True)
                or stored_edge_count != raw_edge_count
            ):
                raise RuntimeError("host geometry compound lost raw outline edges")
            _annotate_text_entity(
                obj,
                geometry_source_id,
                representation,
                parent_source_item_id=parent_source_item_id,
            )
            add_property = getattr(obj, "addProperty", None)
            geometry_metadata = (
                ("App::PropertyInteger", "PDFRawEdgeCount", int(raw_edge_count)),
                (
                    "App::PropertyString",
                    "PDFSourceBBox",
                    _serialize_bbox(item_filter["source_bbox"] if item_filter else None),
                ),
                (
                    "App::PropertyString",
                    "PDFGeometryBounds",
                    _serialize_bbox(_shape_host_bbox(stored_shape)),
                ),
                (
                    "App::PropertyString",
                    "PDFGeometryGrouping",
                    "source_item_compound_v1",
                ),
            )
            for property_type, property_name, property_value in geometry_metadata:
                if not hasattr(obj, property_name):
                    if not callable(add_property):
                        raise RuntimeError(
                            f"host cannot store required {property_name} metadata"
                        )
                    add_property(property_type, property_name, "PDF Import")
                setattr(obj, property_name, property_value)
                stored_value = getattr(obj, property_name, None)
                if property_name == "PDFRawEdgeCount":
                    verified = int(stored_value) == int(property_value)
                else:
                    verified = str(stored_value or "") == str(property_value)
                if not verified:
                    raise RuntimeError(
                        f"host did not preserve required {property_name} metadata"
                    )
            geometry_created_id = _host_object_id(obj)
            created_ids.append(geometry_created_id)
            child_source_ids.append(geometry_source_id)
            if parent_group is not None:
                parent_group.addObject(obj)
            if item_filter is None:
                attempts.append(
                    {
                        "source_item_id": geometry_source_id,
                        "requested_type": representation,
                        "attempted_type": representation,
                        "final_type": representation,
                        "outcome": "verified",
                        "reason": "requested geometry delivered",
                        "created_entity_ids": [geometry_created_id],
                        "support_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                        "delivery_count": raw_edge_count,
                        "evidence": {
                            "renderer": renderer_name,
                            "host_entity_type": "Part::Feature",
                            "raw_edge_count": raw_edge_count,
                            "geometry_grouping": "source_item_compound_v1",
                        },
                    }
                )

        # The canonical importer delivers one source item at a time, then
        # recomputes once at the page/import commit boundary. Recomputing the
        # whole document here made dense pages quadratic (one full recompute
        # per span). Direct/legacy callers retain the immediate verification
        # barrier unless they explicitly opt into the page-level transaction.
        if not defer_recompute:
            doc.recompute()
        if (
            not created_ids
            or len(created_ids) != len(owned)
            or len(set(created_ids)) != len(created_ids)
            or any(not entity_id for entity_id in created_ids)
        ):
            raise RuntimeError("created SVG text entity IDs could not be verified")
        current_objects = list(getattr(doc, "Objects", []) or [])
        if any(
            not any(candidate is host_obj for candidate in current_objects)
            for host_obj in owned
        ):
            raise RuntimeError("created SVG text host object is not live")
        if parent_group is not None:
            if hasattr(parent_group, "Group"):
                group_objects = list(parent_group.Group or [])
            elif hasattr(parent_group, "objects"):
                group_objects = list(parent_group.objects or [])
            else:
                raise RuntimeError("SVG text parent group membership is unverifiable")
            if any(
                not any(candidate is host_obj for candidate in group_objects)
                for host_obj in owned
            ):
                raise RuntimeError("created SVG text entity is not in its parent group")
        if len(owned) != len(child_source_ids):
            raise RuntimeError("created SVG text child identity count is invalid")
        for child_index, host_obj in enumerate(owned):
            child_source_id = child_source_ids[child_index]
            if (
                str(getattr(host_obj, "TypeId", "") or "") != "Part::Feature"
                or getattr(host_obj, "PDFSourceItemId", None) != child_source_id
                or getattr(host_obj, "PDFRepresentation", None) != representation
                or (
                    item_filter is not None
                    and getattr(host_obj, "PDFParentSourceItemId", None)
                    != item_filter["source_item_id"]
                )
            ):
                raise RuntimeError("created SVG text host metadata is unverifiable")
            if representation == "geometry":
                refreshed_shape = getattr(host_obj, "Shape", None)
                refreshed_edge_count = _shape_edge_count(refreshed_shape)
                try:
                    declared_edge_count = int(host_obj.PDFRawEdgeCount)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    declared_edge_count = -1
                if (
                    not _shape_nonempty(refreshed_shape, require_edges=True)
                    or declared_edge_count != raw_edge_count
                    or refreshed_edge_count != declared_edge_count
                    or str(getattr(host_obj, "PDFGeometryGrouping", "") or "")
                    != "source_item_compound_v1"
                ):
                    raise RuntimeError(
                        "stored geometry compound raw edge count is invalid"
                    )

        if item_filter is not None:
            attempt_evidence = {
                "renderer": renderer_name,
                "host_entity_type": "Part::Feature",
                "raw_edge_count": (
                    raw_edge_count if representation == "geometry" else None
                ),
                **item_filter_evidence,
                "child_source_item_ids": list(child_source_ids),
            }
            attempts.append(
                {
                    "source_item_id": item_filter["source_item_id"],
                    "requested_type": item_filter["requested_type"],
                    "attempted_type": representation,
                    "final_type": representation,
                    "outcome": "verified",
                    "reason": (
                        f"requested {representation} delivered for exact source item"
                    ),
                    "created_entity_ids": list(created_ids),
                    "support_entity_ids": [],
                    "removed_entity_ids": [],
                    "cleanup_complete": True,
                    "delivery_count": (
                        raw_edge_count if representation == "geometry" else len(created_ids)
                    ),
                    "evidence": attempt_evidence,
                }
            )
    except Exception as exc:
        ownership_collection_error = ""
        if creation_started:
            try:
                current_objects = list(getattr(doc, "Objects", []) or [])
                for host_obj in current_objects:
                    if (
                        id(host_obj) not in baseline_objects
                        and not any(candidate is host_obj for candidate in owned)
                    ):
                        owned.append(host_obj)
            except (AttributeError, RuntimeError, TypeError, ValueError) as collect_exc:
                ownership_collection_error = (
                    f"{collect_exc.__class__.__name__}: {collect_exc}"
                )
        created_entity_ids = [_host_object_id(obj) for obj in owned]
        cleanup = _cleanup_owned(doc, parent_group, owned)
        if ownership_collection_error:
            cleanup["cleanup_complete"] = False
        raise TextRepresentationRenderError(
            "host_entity_verification_failed",
            {
                "requested_type": (
                    item_filter["requested_type"] if item_filter else representation
                ),
                "attempted_type": representation,
                "source_item_id": (
                    item_filter["source_item_id"] if item_filter else None
                ),
                "renderer": renderer_name,
                "exception": f"{exc.__class__.__name__}: {exc}",
                "created_entity_ids": created_entity_ids,
                "ownership_collection_error": ownership_collection_error,
                **cleanup,
            },
        ) from exc

    if cache is not None and item_filter is not None:
        cache["claimed_placement_indices"].update(
            item_filter_evidence["matched_placement_indices"]
        )

    return {
        "outcome": "verified",
        "shapes": len(glyph_shapes),
        "glyphs": len(placed_glyphs),
        "raw_edges": raw_edge_count,
        "entities": len(created_ids),
        "entity_type": representation,
        "renderer": renderer_name,
        "created_entity_ids": created_ids,
        "delivery_attempts": attempts,
        "source_item_id": item_filter["source_item_id"] if item_filter else None,
        "item_filter": item_filter_evidence,
    }


def _render_svg_with_pdftocairo(exe: str, pdf_path: str, page_num: int) -> Optional[str]:
    # Always clean up temp file regardless of outcome.
    fd, svg_path = tempfile.mkstemp(suffix=".svg", prefix=f"bc_fc_svg_{page_num}_")
    os.close(fd)  # close fd so subprocess can write to the path

    try:
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        cmd_variants = [
            # Preferred: crop to page crop box (best fidelity when supported).
            [exe, "-svg", "-cropbox", "-f", str(page_num), "-l", str(page_num),
             "--", pdf_path, svg_path],
            # Compatibility fallback: some pdftocairo builds reject -cropbox with -svg.
            [exe, "-svg", "-f", str(page_num), "-l", str(page_num),
             "--", pdf_path, svg_path],
        ]
        last_err = None
        for cmd in cmd_variants:
            try:
                if os.path.isfile(svg_path):
                    os.remove(svg_path)
                subprocess.run(cmd, check=True, timeout=90, capture_output=True, **kw)
                if os.path.isfile(svg_path):
                    with open(svg_path, "r", encoding="utf-8") as f:
                        svg = f.read()
                    if svg:
                        return svg
            except subprocess.TimeoutExpired:
                # Timeout is unlikely to improve by retrying variants.
                raise
            except (subprocess.SubprocessError, OSError, ValueError, UnicodeError) as e:
                last_err = e
                continue

        if last_err:
            raise last_err
        return None

    except subprocess.TimeoutExpired:
        if FreeCAD:
            FreeCAD.Console.PrintWarning(
                f"PDFSvgTextRenderer: pdftocairo timed out on page {page_num} — "
                "requested SVG text representation was not rendered.\n"
            )
        return None
    except (subprocess.SubprocessError, OSError, ValueError, UnicodeError) as e:
        if FreeCAD:
            FreeCAD.Console.PrintWarning(
                f"PDFSvgTextRenderer: pdftocairo failed on page {page_num}: {e}\n"
            )
        return None
    finally:
        try:
            os.remove(svg_path)
        except OSError:
            pass


def _load_fitz():
    try:
        from pdfcadcore.fitz_loader import import_fitz
    except Exception:
        try:
            from PDFVectorImporter.pdfcadcore.fitz_loader import import_fitz
        except Exception as e:
            if FreeCAD:
                FreeCAD.Console.PrintWarning(
                    f"PDFSvgTextRenderer: PyMuPDF fallback loader unavailable: {e}\n"
                )
            return None

    try:
        lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
        return import_fitz(prefer_lib_dir=lib_dir)
    except Exception as e:
        if FreeCAD:
            FreeCAD.Console.PrintWarning(
                f"PDFSvgTextRenderer: PyMuPDF fallback unavailable: {e}\n"
            )
        return None


def _render_svg_with_pymupdf(pdf_path: str, page_num: int) -> Optional[str]:
    fitz = _load_fitz()
    if fitz is None:
        return None

    pdf_doc = None
    try:
        pdf_doc = fitz.open(pdf_path)
        if page_num < 1 or page_num > len(pdf_doc):
            return None
        page = pdf_doc.load_page(page_num - 1)
        return page.get_svg_image(text_as_path=1)
    except Exception as e:
        if FreeCAD:
            FreeCAD.Console.PrintWarning(
                f"PDFSvgTextRenderer: PyMuPDF SVG fallback failed on page {page_num}: {e}\n"
            )
        return None
    finally:
        try:
            if pdf_doc is not None:
                pdf_doc.close()
        except Exception:
            pass


def _max_svg_text_bytes() -> int:
    raw = os.environ.get("BC_FC_SVG_TEXT_MAX_BYTES", "").strip()
    try:
        value = int(raw) if raw else 50_000_000
        return value if value > 0 else 50_000_000
    except (TypeError, ValueError):
        return 50_000_000


def _svg_too_large(svg: str) -> bool:
    try:
        return len(svg.encode("utf-8", "ignore")) > _max_svg_text_bytes()
    except Exception:
        return False


def _glyph_reference_id(gid: str) -> bool:
    return bool(gid) and (
        gid.startswith("glyph-") or gid.startswith("font_") or gid.startswith("font-")
    )


def _parse_all_glyph_defs(svg: str) -> Dict[str, str]:
    glyph_defs: Dict[str, str] = {}
    for gid, path_d in re.findall(
            r'<g id="([^"]+)">\s*<path d="([^"]*)"', svg, re.DOTALL):
        if _glyph_reference_id(gid):
            glyph_defs[gid] = path_d

    # Poppler represents spaces and other zero-outline glyphs as an empty
    # ``<g id="glyph-...">``.  Preserve those definitions explicitly so a
    # valid whitespace placement is not misclassified as a missing visible
    # outline and allowed to block every item on the page.
    for gid, body in re.findall(
        r'<g\b[^>]*\bid="([^"]+)"[^>]*>(.*?)</g>',
        svg,
        re.IGNORECASE | re.DOTALL,
    ):
        if _glyph_reference_id(gid) and gid not in glyph_defs and not body.strip():
            glyph_defs[gid] = ""

    for tag in re.findall(r'<path\b[^>]*>', svg, re.IGNORECASE | re.DOTALL):
        id_m = re.search(r'\bid="([^"]+)"', tag, re.IGNORECASE)
        d_m = re.search(r'\bd="([^"]*)"', tag, re.IGNORECASE | re.DOTALL)
        if not id_m or not d_m:
            continue
        gid = id_m.group(1)
        path_d = d_m.group(1)
        if _glyph_reference_id(gid):
            glyph_defs[gid] = path_d
    return glyph_defs


def _parse_glyph_defs(svg: str) -> Dict[str, str]:
    return {
        glyph_id: path_d
        for glyph_id, path_d in _parse_all_glyph_defs(svg).items()
        if path_d.strip()
    }


def _parse_svg_dim(svg: str, attr: str, fallback: float) -> float:
    m = re.search(rf'{attr}="([^"]+)"', svg)
    if not m:
        return float(fallback)
    raw = m.group(1)
    m_num = re.match(r'\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)', raw)
    if not m_num:
        return float(fallback)
    try:
        return float(m_num.group(1))
    except (TypeError, ValueError):
        return float(fallback)


def _parse_viewbox(svg: str):
    m = re.search(r'viewBox="([^"]+)"', svg, re.IGNORECASE)
    if not m:
        return (0.0, 0.0, 0.0, 0.0)
    try:
        vals = [float(v) for v in re.split(r"[\s,]+", m.group(1).strip()) if v]
        if len(vals) >= 4:
            return (vals[0], vals[1], vals[2], vals[3])
    except (TypeError, ValueError):
        pass
    return (0.0, 0.0, 0.0, 0.0)


def _svg_rendered_body(svg: str) -> str:
    """Return SVG markup with reusable definition bodies removed."""
    return re.sub(
        r"<defs\b[^>]*>.*?</defs>",
        "",
        svg,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _compose_svg_matrices(outer, inner):
    """Compose SVG affine matrices using the standard column-vector form."""
    oa, ob, oc, od, oe, of = outer
    ia, ib, ic, id_, ie, iff = inner
    return (
        oa * ia + oc * ib,
        ob * ia + od * ib,
        oa * ic + oc * id_,
        ob * ic + od * id_,
        oa * ie + oc * iff + oe,
        ob * ie + od * iff + of,
    )


def _svg_matrix_from_transform(value: str):
    match = re.search(r"matrix\(([^)]*)\)", str(value or ""), re.IGNORECASE)
    if not match:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    parts = [
        part
        for part in re.split(r"[\s,]+", match.group(1).strip())
        if part
    ]
    if len(parts) < 6:
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    try:
        matrix = tuple(float(value) for value in parts[:6])
    except (TypeError, ValueError):
        return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    return matrix if all(math.isfinite(value) for value in matrix) else (
        1.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
    )


def _svg_use_matrix_from_tag(tag: str):
    matrix = _svg_matrix_from_transform(
        re.search(r'transform="([^"]+)"', tag, re.IGNORECASE).group(1)
        if re.search(r'transform="([^"]+)"', tag, re.IGNORECASE)
        else ""
    )
    a, b, c, d, e, f = matrix
    return (a, b, c, d, e + _attr_float(tag, "x", 0.0), f + _attr_float(tag, "y", 0.0))


def _parse_vector_source_glyphs(svg: str):
    """Expand reusable Poppler ``source-*`` groups into local glyph matrices."""
    try:
        root = ET.fromstring(svg)
    except (ET.ParseError, TypeError, ValueError):
        return {}

    def local_name(element):
        return str(element.tag).rsplit("}", 1)[-1].lower()

    def href_value(element):
        for key, value in element.attrib.items():
            if str(key).rsplit("}", 1)[-1].lower() == "href":
                return str(value or "").strip().lstrip("#")
        return ""

    def element_matrix(element, *, include_use_offset=False):
        matrix = _svg_matrix_from_transform(element.attrib.get("transform", ""))
        if include_use_offset:
            a, b, c, d, e, f = matrix
            try:
                use_x = float(element.attrib.get("x", 0.0) or 0.0)
                use_y = float(element.attrib.get("y", 0.0) or 0.0)
            except (TypeError, ValueError):
                use_x = use_y = 0.0
            matrix = (a, b, c, d, e + use_x, f + use_y)
        return matrix

    sources = {}

    def collect(node, inherited, target):
        current = _compose_svg_matrices(inherited, element_matrix(node))
        for child in list(node):
            if local_name(child) == "use":
                glyph_id = href_value(child)
                if _glyph_reference_id(glyph_id):
                    target.append(
                        (
                            glyph_id,
                            _compose_svg_matrices(
                                current,
                                element_matrix(child, include_use_offset=True),
                            ),
                        )
                    )
            else:
                collect(child, current, target)

    identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for element in root.iter():
        source_id = str(element.attrib.get("id", "") or "")
        if local_name(element) != "g" or not source_id.startswith("source-"):
            continue
        glyphs = []
        collect(element, identity, glyphs)
        if glyphs:
            sources[source_id] = glyphs
    return sources


def _parse_use_placements(svg: str):
    placements = []
    # A Poppler SVG can contain ``<use>`` nodes inside ``<defs>`` to build a
    # reusable composite source. Those nodes are definition internals, not
    # page paints; treating them as page glyphs creates false placements near
    # the local definition origin and makes otherwise exact global assignment
    # fail. Only scan the rendered document body here.
    rendered_svg = _svg_rendered_body(svg)
    vector_sources = (
        _parse_vector_source_glyphs(svg)
        if re.search(
            r'(?:xlink:href|href)="#source-', rendered_svg, re.IGNORECASE
        )
        else {}
    )
    for m in re.finditer(
        r'<use\b[^>]*>', rendered_svg, re.IGNORECASE | re.DOTALL
    ):
        tag = m.group(0)
        href_m = re.search(r'(?:xlink:href|href)="([^"]+)"', tag, re.IGNORECASE)
        if not href_m:
            continue
        href = href_m.group(1).strip()
        if not href.startswith("#"):
            continue
        gid = href[1:]
        if gid in vector_sources:
            page_matrix = _svg_use_matrix_from_tag(tag)
            for child_gid, child_matrix in vector_sources[gid]:
                placements.append(
                    (
                        child_gid,
                        0.0,
                        0.0,
                        list(_compose_svg_matrices(page_matrix, child_matrix)),
                    )
                )
            continue
        if not _glyph_reference_id(gid):
            continue
        x = _attr_float(tag, "x", 0.0)
        y = _attr_float(tag, "y", 0.0)
        matrix = None
        tr_m = re.search(r'transform="([^"]+)"', tag, re.IGNORECASE)
        if tr_m:
            mm = re.search(r'matrix\(([^)]*)\)', tr_m.group(1), re.IGNORECASE)
            if mm:
                parts = [p for p in re.split(r'[\s,]+', mm.group(1).strip()) if p]
                if len(parts) >= 6:
                    try:
                        matrix = [float(v) for v in parts[:6]]
                    except (TypeError, ValueError):
                        matrix = None
        placements.append((gid, x, y, matrix))
    return placements


def _parse_raster_source_placements(
    svg: str,
) -> List[Tuple[str, Tuple[float, float, float, float]]]:
    """Return page-painted SVG image-source bounds in SVG coordinates."""
    image_sources: Dict[str, Tuple[float, float, float, float]] = {}
    for tag in re.findall(r"<image\b[^>]*>", svg, re.IGNORECASE | re.DOTALL):
        id_match = re.search(r'\bid="([^"]+)"', tag, re.IGNORECASE)
        if not id_match or not id_match.group(1).startswith("source-"):
            continue
        source_id = id_match.group(1)
        x = _attr_float(tag, "x", 0.0)
        y = _attr_float(tag, "y", 0.0)
        width = _attr_float(tag, "width", 0.0)
        height = _attr_float(tag, "height", 0.0)
        if all(math.isfinite(value) for value in (x, y, width, height)) and (
            width > 0.0 and height > 0.0
        ):
            image_sources[source_id] = (x, y, x + width, y + height)

    placements: List[Tuple[str, Tuple[float, float, float, float]]] = []
    if not image_sources:
        return placements
    for match in re.finditer(
        r"<use\b[^>]*>", _svg_rendered_body(svg), re.IGNORECASE | re.DOTALL
    ):
        tag = match.group(0)
        href_match = re.search(
            r'(?:xlink:href|href)="([^"]+)"', tag, re.IGNORECASE
        )
        if not href_match:
            continue
        source_id = href_match.group(1).strip().lstrip("#")
        local_bbox = image_sources.get(source_id)
        if local_bbox is None:
            continue
        use_x = _attr_float(tag, "x", 0.0)
        use_y = _attr_float(tag, "y", 0.0)
        matrix = None
        transform_match = re.search(r'transform="([^"]+)"', tag, re.IGNORECASE)
        if transform_match:
            matrix_match = re.search(
                r"matrix\(([^)]*)\)", transform_match.group(1), re.IGNORECASE
            )
            if matrix_match:
                parts = [
                    part
                    for part in re.split(r"[\s,]+", matrix_match.group(1).strip())
                    if part
                ]
                if len(parts) >= 6:
                    try:
                        matrix = tuple(float(value) for value in parts[:6])
                    except (TypeError, ValueError):
                        matrix = None
        transformed = []
        for local_x, local_y in (
            (local_bbox[0], local_bbox[1]),
            (local_bbox[0], local_bbox[3]),
            (local_bbox[2], local_bbox[1]),
            (local_bbox[2], local_bbox[3]),
        ):
            local_x += use_x
            local_y += use_y
            if matrix is not None:
                a, b, c, d, e, f = matrix
                svg_x = a * local_x + c * local_y + e
                svg_y = b * local_x + d * local_y + f
            else:
                svg_x = local_x
                svg_y = local_y
            transformed.append((svg_x, svg_y))
        x_values = [point[0] for point in transformed]
        y_values = [point[1] for point in transformed]
        placements.append(
            (
                source_id,
                (min(x_values), min(y_values), max(x_values), max(y_values)),
            )
        )
    return placements


def _attr_float(tag: str, name: str, default: float = 0.0) -> float:
    m = re.search(rf'\b{name}="([^"]+)"', tag, re.IGNORECASE)
    if not m:
        return float(default)
    try:
        return float(m.group(1))
    except (TypeError, ValueError):
        return float(default)


def _shape_affine_2d(shape, a11: float, a12: float, a21: float, a22: float,
                     tx: float, ty: float):
    try:
        m = FreeCAD.Matrix()
        m.A11 = float(a11); m.A12 = float(a12); m.A13 = 0.0; m.A14 = float(tx)
        m.A21 = float(a21); m.A22 = float(a22); m.A23 = 0.0; m.A24 = float(ty)
        m.A31 = 0.0; m.A32 = 0.0; m.A33 = 1.0; m.A34 = 0.0
        m.A41 = 0.0; m.A42 = 0.0; m.A43 = 0.0; m.A44 = 1.0
        try:
            transformed = shape.transformGeometry(m)
            if transformed is not None:
                return transformed
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        cp = shape.copy()
        cp.transformShape(m, True, False)
        return cp
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def _svg_path_to_edges(d: str, scale_x: float, scale_y: Optional[float] = None) -> List:
    """Parse SVG path d="" into Part edges.

    Glyph coordinates are in PDF points, Y-down.
    We flip Y and scale to mm for FreeCAD.
    """
    tokens = re.findall(r'[MLHVCSZQTAmlhvcszqta]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', d)
    edges = []
    subpath_pts = []
    start_pt = None
    cx, cy = 0.0, 0.0
    cmd = None
    nums: List[float] = []
    prev_cubic_cp2: Optional[List[float]] = None  # second control point of previous cubic (in abs coords)
    prev_quad_cp: Optional[List[float]] = None    # control point of previous quadratic (in abs coords)

    if scale_y is None:
        scale_y = scale_x

    def mk(gx: float, gy: float) -> Vector:
        return Vector(gx * scale_x, -gy * scale_y, 0.0)

    def flush_subpath():
        nonlocal subpath_pts
        if len(subpath_pts) >= 2:
            for i in range(len(subpath_pts) - 1):
                p1, p2 = subpath_pts[i], subpath_pts[i + 1]
                if p1.distanceToPoint(p2) > 1e-4:
                    try:
                        edges.append(Part.LineSegment(p1, p2).toShape())
                    except (RuntimeError, ValueError, TypeError):
                        pass
        subpath_pts = []

    def append_cubic(p0, p1, p2, p3):
        chord = p0.distanceToPoint(p3)
        n = 6 if chord < 0.5 else (8 if chord < 2.0 else 12)
        for i in range(1, n + 1):
            t = i / n
            mt = 1.0 - t
            bx = mt**3*p0.x + 3*mt**2*t*p1.x + 3*mt*t**2*p2.x + t**3*p3.x
            by = mt**3*p0.y + 3*mt**2*t*p1.y + 3*mt*t**2*p2.y + t**3*p3.y
            subpath_pts.append(Vector(bx, by, 0.0))

    def append_quadratic(p0, p1, p2):
        chord = p0.distanceToPoint(p2)
        n = 5 if chord < 0.5 else (8 if chord < 2.0 else 10)
        for i in range(1, n + 1):
            t = i / n
            mt = 1.0 - t
            bx = mt**2*p0.x + 2*mt*t*p1.x + t**2*p2.x
            by = mt**2*p0.y + 2*mt*t*p1.y + t**2*p2.y
            subpath_pts.append(Vector(bx, by, 0.0))

    def svg_arc_points(x0, y0, rx, ry, angle_deg, large_arc, sweep, x, y):
        rx = abs(float(rx))
        ry = abs(float(ry))
        if rx <= 1e-12 or ry <= 1e-12:
            return [(x, y)]
        if abs(x0 - x) <= 1e-12 and abs(y0 - y) <= 1e-12:
            return []

        phi = math.radians(float(angle_deg))
        cos_phi = math.cos(phi)
        sin_phi = math.sin(phi)
        dx = (x0 - x) / 2.0
        dy = (y0 - y) / 2.0
        x1p = cos_phi * dx + sin_phi * dy
        y1p = -sin_phi * dx + cos_phi * dy

        lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
        if lam > 1.0:
            scale = math.sqrt(lam)
            rx *= scale
            ry *= scale

        rx2 = rx * rx
        ry2 = ry * ry
        x1p2 = x1p * x1p
        y1p2 = y1p * y1p
        denom = rx2 * y1p2 + ry2 * x1p2
        if denom <= 1e-12:
            return [(x, y)]
        num = max(0.0, rx2 * ry2 - rx2 * y1p2 - ry2 * x1p2)
        sign = -1.0 if bool(int(large_arc)) == bool(int(sweep)) else 1.0
        coef = sign * math.sqrt(num / denom)
        cxp = coef * (rx * y1p / ry)
        cyp = coef * (-ry * x1p / rx)
        cx_abs = cos_phi * cxp - sin_phi * cyp + (x0 + x) / 2.0
        cy_abs = sin_phi * cxp + cos_phi * cyp + (y0 + y) / 2.0

        def angle_between(ux, uy, vx, vy):
            dot = ux * vx + uy * vy
            det = ux * vy - uy * vx
            return math.atan2(det, dot)

        ux = (x1p - cxp) / rx
        uy = (y1p - cyp) / ry
        vx = (-x1p - cxp) / rx
        vy = (-y1p - cyp) / ry
        theta1 = angle_between(1.0, 0.0, ux, uy)
        delta = angle_between(ux, uy, vx, vy)
        if not bool(int(sweep)) and delta > 0:
            delta -= 2.0 * math.pi
        elif bool(int(sweep)) and delta < 0:
            delta += 2.0 * math.pi

        n = max(4, min(32, int(math.ceil(abs(delta) / (math.pi / 8.0)))))
        pts = []
        for i in range(1, n + 1):
            theta = theta1 + delta * (i / n)
            xp = rx * math.cos(theta)
            yp = ry * math.sin(theta)
            gx = cos_phi * xp - sin_phi * yp + cx_abs
            gy = sin_phi * xp + cos_phi * yp + cy_abs
            pts.append((gx, gy))
        return pts

    def run():
        nonlocal cx, cy, start_pt, subpath_pts, is_relative
        nonlocal prev_cubic_cp2, prev_quad_cp

        if cmd == "M":
            prev_cubic_cp2 = None
            prev_quad_cp = None
            first_pair = True
            while len(nums) >= 2:
                nx, ny = nums.pop(0), nums.pop(0)
                if is_relative:
                    cx, cy = cx + nx, cy + ny
                else:
                    cx, cy = nx, ny
                if first_pair:
                    flush_subpath()
                    start_pt = mk(cx, cy)
                    subpath_pts = [start_pt]
                    first_pair = False
                else:
                    subpath_pts.append(mk(cx, cy))
        elif cmd == "L":
            prev_cubic_cp2 = None
            prev_quad_cp = None
            while len(nums) >= 2:
                nx, ny = nums.pop(0), nums.pop(0)
                if is_relative:
                    cx, cy = cx + nx, cy + ny
                else:
                    cx, cy = nx, ny
                subpath_pts.append(mk(cx, cy))
        elif cmd == "H":
            prev_cubic_cp2 = None
            prev_quad_cp = None
            while nums:
                nx = nums.pop(0)
                if is_relative:
                    cx = cx + nx
                else:
                    cx = nx
                subpath_pts.append(mk(cx, cy))
        elif cmd == "V":
            prev_cubic_cp2 = None
            prev_quad_cp = None
            while nums:
                ny = nums.pop(0)
                if is_relative:
                    cy = cy + ny
                else:
                    cy = ny
                subpath_pts.append(mk(cx, cy))
        elif cmd == "C":
            prev_quad_cp = None
            while len(nums) >= 6:
                rx1, ry1, rx2, ry2, rx, ry = [nums.pop(0) for _ in range(6)]
                if is_relative:
                    x1, y1 = cx + rx1, cy + ry1
                    x2, y2 = cx + rx2, cy + ry2
                    x, y = cx + rx, cy + ry
                else:
                    x1, y1 = rx1, ry1
                    x2, y2 = rx2, ry2
                    x, y = rx, ry
                p0 = subpath_pts[-1] if subpath_pts else mk(cx, cy)
                p1 = mk(x1, y1)
                p2 = mk(x2, y2)
                p3 = mk(x, y)
                append_cubic(p0, p1, p2, p3)
                prev_cubic_cp2 = [x2, y2]
                cx, cy = x, y
        elif cmd == "S":
            prev_quad_cp = None
            while len(nums) >= 4:
                rx2, ry2, rx, ry = nums.pop(0), nums.pop(0), nums.pop(0), nums.pop(0)
                if is_relative:
                    x2, y2 = cx + rx2, cy + ry2
                    x, y = cx + rx, cy + ry
                else:
                    x2, y2 = rx2, ry2
                    x, y = rx, ry
                # Reflect the second control point of the previous cubic
                if prev_cubic_cp2 is not None:
                    x1 = 2 * cx - prev_cubic_cp2[0]
                    y1 = 2 * cy - prev_cubic_cp2[1]
                else:
                    x1, y1 = cx, cy
                p0 = subpath_pts[-1] if subpath_pts else mk(cx, cy)
                p1 = mk(x1, y1)
                p2 = mk(x2, y2)
                p3 = mk(x, y)
                append_cubic(p0, p1, p2, p3)
                prev_cubic_cp2 = [x2, y2]
                cx, cy = x, y
        elif cmd == "Q":
            prev_cubic_cp2 = None
            while len(nums) >= 4:
                rx1, ry1, rx, ry = nums.pop(0), nums.pop(0), nums.pop(0), nums.pop(0)
                if is_relative:
                    x1, y1 = cx + rx1, cy + ry1
                    x, y = cx + rx, cy + ry
                else:
                    x1, y1 = rx1, ry1
                    x, y = rx, ry
                p0 = subpath_pts[-1] if subpath_pts else mk(cx, cy)
                append_quadratic(p0, mk(x1, y1), mk(x, y))
                prev_quad_cp = [x1, y1]
                cx, cy = x, y
        elif cmd == "T":
            prev_cubic_cp2 = None
            while len(nums) >= 2:
                rx, ry = nums.pop(0), nums.pop(0)
                if is_relative:
                    x, y = cx + rx, cy + ry
                else:
                    x, y = rx, ry
                if prev_quad_cp is not None:
                    x1 = 2 * cx - prev_quad_cp[0]
                    y1 = 2 * cy - prev_quad_cp[1]
                else:
                    x1, y1 = cx, cy
                p0 = subpath_pts[-1] if subpath_pts else mk(cx, cy)
                append_quadratic(p0, mk(x1, y1), mk(x, y))
                prev_quad_cp = [x1, y1]
                cx, cy = x, y
        elif cmd == "A":
            prev_cubic_cp2 = None
            prev_quad_cp = None
            while len(nums) >= 7:
                rx, ry, xrot, large, sweep, ex, ey = [nums.pop(0) for _ in range(7)]
                x0, y0 = cx, cy
                if is_relative:
                    x, y = cx + ex, cy + ey
                else:
                    x, y = ex, ey
                for ax, ay in svg_arc_points(x0, y0, rx, ry, xrot, large, sweep, x, y):
                    subpath_pts.append(mk(ax, ay))
                cx, cy = x, y
        elif cmd == "Z":
            prev_cubic_cp2 = None
            prev_quad_cp = None
            if subpath_pts and start_pt:
                if subpath_pts[-1].distanceToPoint(start_pt) > 1e-4:
                    subpath_pts.append(start_pt)
            flush_subpath()
            if start_pt:
                subpath_pts = [start_pt]

    is_relative = False
    for tok in tokens:
        if re.match(r'^[A-Za-z]$', tok):
            if cmd is not None:
                run()
            is_relative = tok.islower()
            cmd = tok.upper()
            nums = []
        else:
            nums.append(float(tok))
    if cmd is not None:
        run()
    flush_subpath()

    return edges
