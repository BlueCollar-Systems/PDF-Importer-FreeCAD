# -*- coding: utf-8 -*-
# PDFImporterCore.py — FreeCAD PDF Vector Import Engine
# BlueCollar Systems — BUILT. NOT BOUGHT.
# License: MIT (PyMuPDF itself is AGPL-3 / commercial)
"""
Converts PDF vector paths, text, and embedded images into native FreeCAD
Part geometry (wires, faces, arcs) with full color/layer grouping.

Converts PDF drawings into editable FreeCAD geometry with text and image support.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import json
import math
from contextlib import contextmanager
import os
import re
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

# Ensure bundled PyMuPDF is importable (skip namespace-only stubs in lib/)
_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

_mod_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _mod_root not in sys.path:
    sys.path.insert(0, _mod_root)
from pdfcadcore.fitz_loader import import_fitz as _import_fitz

fitz = _import_fitz(prefer_lib_dir=_lib_dir)

# FreeCAD modules — lazy import for IDE friendliness outside FreeCAD
try:
    import FreeCAD
    import Draft
    import Part
    from FreeCAD import Placement, Rotation, Vector
except ImportError:
    FreeCAD = Draft = Part = None
    Vector = Placement = Rotation = None

try:
    import ImageGui  # noqa: F401
    IMAGE_WB = True
except ImportError:
    IMAGE_WB = False

# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
MM_PER_PT = 25.4 / 72.0       # 1 PDF point = 0.352778 mm
ZERO_TOL  = 1e-9              # near-zero length tolerance
CLOSE_TOL = 1e-6              # endpoint-coincidence tolerance

# Auto-mode heuristics for pages that contain mostly vectorized glyph fills.
# These PDFs often look "vector" to PyMuPDF but are effectively not useful as
# editable CAD geometry (thousands of tiny filled path groups).
AUTO_GLYPH_DRAWING_THRESHOLD = 1500
AUTO_GLYPH_FILL_RATIO = 0.75          # was 0.85 — loosened to catch more flood types
AUTO_GLYPH_TINY_RECT_RATIO = 0.45
AUTO_GLYPH_TEXT_BLOCK_THRESHOLD = 50  # was 200 — text-sparse maps still trigger
AUTO_GLYPH_WORD_THRESHOLD = 400       # was 800 — lower text requirement
AUTO_GLYPH_STROKE_SPARSE_RATIO = 0.05
AUTO_GLYPH_TINY_RECT_AREA_PT2 = 36.0

# Fill-art flood detection — catches map PDFs, illustrated layouts, decorative
# art where most drawing groups are filled shapes rather than engineering strokes.
# Unlike glyph-flood (text-as-vectors), these pages have organic filled areas
# (tree canopies, planting beds, terrain fills) with almost no stroked lines.
AUTO_FILL_DRAWING_THRESHOLD = 400  # minimum groups to trigger fill-art check
AUTO_FILL_HEAVY_RATIO = 0.60       # fill-only ratio — 60%+ fills signals art/map
AUTO_FILL_STROKE_MAX = 0.22        # stroke ratio ceiling — if too many strokes
#                                    it's a hybrid worth processing as vectors
#
# PyMuPDF 1.27+ can coalesce many path ops into fewer drawing groups. That means
# some decorative art pages now present as ~10-50 groups (not hundreds), but are
# still pure fill geometry with almost no useful CAD strokes.
AUTO_FILL_PURE_RATIO = 0.95
AUTO_FILL_PURE_STROKE_MAX = 0.02
AUTO_FILL_PURE_MIN_GROUPS = 12
AUTO_FILL_PURE_MIN_ITEMS = 24
AUTO_FILL_PURE_LARGE_RECT_RATIO = 0.03


# ──────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────
def _msg(s: str):
    if FreeCAD:
        FreeCAD.Console.PrintMessage(s + "\n")

def _warn(s: str):
    if FreeCAD:
        FreeCAD.Console.PrintWarning(s + "\n")

def _err(s: str):
    if FreeCAD:
        FreeCAD.Console.PrintError(s + "\n")


# ──────────────────────────────────────────────────────────────────────
# Vector helpers  (FreeCAD.Vector.multiply is IN-PLACE — never use it
# for math expressions.  Use the * operator which returns a NEW vector.)
# ──────────────────────────────────────────────────────────────────────
def _v(x: float, y: float, z: float = 0.0) -> "Vector":
    return Vector(x, y, z)


def _len2d(a: "Vector", b: "Vector") -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def _pts_closed(pts: List["Vector"], tol: float = CLOSE_TOL) -> bool:
    return len(pts) > 2 and _len2d(pts[0], pts[-1]) <= tol


# ──────────────────────────────────────────────────────────────────────
# PyMuPDF 1.24+ / 1.26 compatibility  (objects may be Point, tuple, etc.)
# ──────────────────────────────────────────────────────────────────────
def _is_point(obj) -> bool:
    return hasattr(obj, "x") and hasattr(obj, "y")


def _is_rect(obj) -> bool:
    return all(hasattr(obj, a) for a in ("x0", "y0", "x1", "y1"))


def _xy(obj) -> Tuple[float, float]:
    """Extract (x, y) from a fitz.Point, tuple, or list."""
    if _is_point(obj):
        return float(obj.x), float(obj.y)
    if isinstance(obj, (tuple, list)) and len(obj) >= 2:
        return float(obj[0]), float(obj[1])
    return float(obj), 0.0


def _rect_coords(obj) -> Tuple[float, float, float, float]:
    """Return (x0, y0, w, h) from a fitz.Rect or 4-element sequence."""
    if _is_rect(obj):
        x0, y0 = float(obj.x0), float(obj.y0)
        return x0, y0, float(obj.x1) - x0, float(obj.y1) - y0
    if isinstance(obj, (tuple, list)) and len(obj) >= 4:
        x0, y0, x1, y1 = float(obj[0]), float(obj[1]), float(obj[2]), float(obj[3])
        return x0, y0, x1 - x0, y1 - y0
    return 0.0, 0.0, 0.0, 0.0


def _rect_area(obj) -> Optional[float]:
    """Return absolute rectangle area in PDF point units² or None."""
    try:
        _x, _y, w, h = _rect_coords(obj)
        return abs(float(w) * float(h))
    except (TypeError, ValueError):
        return None


def _vector_group_stats(drawings: List[dict], page_area: Optional[float] = None) -> Dict[str, float]:
    """Profile coarse vector composition for auto-mode heuristics."""
    total = len(drawings)
    # Full scans on 10k+ group pages dominated auto-mode time on multi-page shop PDFs.
    sample_stride = 1
    if total > 8000:
        sample_stride = max(1, total // 4000)
    if total <= 0:
        return {
            "fill_only_ratio": 0.0,
            "stroke_ratio": 0.0,
            "tiny_rect_ratio": 0.0,
            "fill_only_count": 0.0,
            "stroke_count": 0.0,
            "tiny_rect_count": 0.0,
            "total_item_count": 0.0,
            "avg_items_per_group": 0.0,
            "max_rect_ratio": 0.0,
        }

    fill_only = 0
    stroke_count = 0
    tiny_rect_count = 0
    total_item_count = 0
    max_rect_ratio = 0.0

    for idx, grp in enumerate(drawings):
        if sample_stride > 1 and (idx % sample_stride) != 0:
            continue
        fill = grp.get("fill")
        stroke = grp.get("color") or grp.get("stroke")
        if fill is not None and stroke is None:
            fill_only += 1
        if stroke is not None:
            stroke_count += 1
        total_item_count += len(grp.get("items", []) or [])
        area = _rect_area(grp.get("rect"))
        if area is not None and area <= AUTO_GLYPH_TINY_RECT_AREA_PT2:
            tiny_rect_count += 1
        if area is not None and page_area and page_area > 0:
            ratio = area / page_area
            if ratio > max_rect_ratio:
                max_rect_ratio = ratio

    sampled = float(max(1, (total + sample_stride - 1) // sample_stride))
    denom = sampled
    return {
        "fill_only_ratio": fill_only / denom,
        "stroke_ratio": stroke_count / denom,
        "tiny_rect_ratio": tiny_rect_count / denom,
        "fill_only_count": float(fill_only),
        "stroke_count": float(stroke_count),
        "tiny_rect_count": float(tiny_rect_count),
        "total_item_count": float(total_item_count),
        "avg_items_per_group": (float(total_item_count) / denom),
        "max_rect_ratio": max_rect_ratio,
    }


def _looks_like_vector_glyph_flood(n_drawings: int,
                                   n_text_blocks: int,
                                   n_words: int,
                                   stats: Dict[str, float]) -> bool:
    """Heuristic for pages where text/vector art overwhelms usable CAD lines.

    Targets PDFs where text characters are stored as filled vector paths
    (each glyph = one filled path group), producing thousands of objects
    that are geometrically useless for CAD but look like line work to PyMuPDF.
    """
    if n_drawings < AUTO_GLYPH_DRAWING_THRESHOLD:
        return False
    text_dense = (n_text_blocks >= AUTO_GLYPH_TEXT_BLOCK_THRESHOLD
                  or n_words >= AUTO_GLYPH_WORD_THRESHOLD)
    if not text_dense:
        return False
    return (stats.get("fill_only_ratio", 0.0) >= AUTO_GLYPH_FILL_RATIO
            and stats.get("tiny_rect_ratio", 0.0) >= AUTO_GLYPH_TINY_RECT_RATIO)


def _looks_like_fill_art_flood(n_drawings: int,
                               stats: Dict[str, float]) -> bool:
    """Detect map / illustrated-art PDFs dominated by filled decorative shapes.

    These pages differ from glyph floods: they are not text-as-vectors but
    rather artistic fills — garden beds, terrain contours, tree canopies,
    landscape features — where each shape is a complex filled path.  Importing
    as vectors produces an unusable tangle of faces; raster is far better.

    Signature: high fill-only ratio, very low stroke ratio, many groups.
    This check is intentionally independent of text density so it fires even
    on map pages with few text labels (e.g. a garden plan with only plant names).
    """
    fill_ratio = stats.get("fill_only_ratio", 0.0)
    stroke_ratio = stats.get("stroke_ratio", 0.0)
    total_items = stats.get("total_item_count", 0.0)
    max_rect_ratio = stats.get("max_rect_ratio", 0.0)

    # Average items per drawing — glyph/fill-art floods have 1-3 items each,
    # while real drawings (garden plans, floor plans) have many more.
    avg_items = total_items / float(max(n_drawings, 1))

    # New fast path for coalesced pure-fill PDFs (common with newer PyMuPDF):
    # if the page is almost entirely fill-only and has virtually no stroke
    # signals, treat it as decorative/map art even at low drawing-group counts.
    # Guard: only trigger when avg items per drawing is low (glyph-like).
    pure_fill = (fill_ratio >= AUTO_FILL_PURE_RATIO
                 and stroke_ratio <= AUTO_FILL_PURE_STROKE_MAX
                 and avg_items <= 5.0)
    if pure_fill and n_drawings >= AUTO_FILL_PURE_MIN_GROUPS:
        if total_items >= AUTO_FILL_PURE_MIN_ITEMS:
            return True
        if max_rect_ratio >= AUTO_FILL_PURE_LARGE_RECT_RATIO:
            return True

    # Legacy high-count fallback (kept for older parser behavior).
    if n_drawings < AUTO_FILL_DRAWING_THRESHOLD:
        return False
    return (fill_ratio >= AUTO_FILL_HEAVY_RATIO
            and stroke_ratio <= AUTO_FILL_STROKE_MAX
            and avg_items <= 5.0)


def _as_float(v) -> Optional[float]:
    """Coerce a value to float (handles fitz.Point, scalar, tuple)."""
    try:
        if hasattr(v, "x") and not isinstance(v, (int, float)):
            return float(v.x)
        return float(v)
    except (TypeError, ValueError, AttributeError):
        if isinstance(v, (tuple, list)) and v:
            try:
                return float(v[0])
            except (TypeError, ValueError):
                pass
    return None


def _as_float_list(seq) -> List[float]:
    out = []
    for x in (seq or []):
        fx = _as_float(x)
        if fx is not None:
            out.append(fx)
    return out


def _parse_dashes(val) -> Tuple[List[float], float]:
    """Parse a dash pattern from PyMuPDF which may be:
    - A string like '[ 6 6 ] 0'  (bracket-delimited, with trailing phase)
    - A list of floats [6.0, 6.0]
    - None or empty
    Returns a (dash_array, phase) tuple.  Empty list means solid."""
    if val is None:
        return [], 0.0
    if isinstance(val, str):
        # Extract numbers from between brackets: "[ 6 6 ] 0" -> [6.0, 6.0]
        bracket_match = re.search(r'\[([^\]]*)\]', val)
        if bracket_match:
            inner = bracket_match.group(1).strip()
            if not inner:
                return [], 0.0  # empty brackets = solid
            nums = []
            for part in inner.split():
                try:
                    nums.append(float(part))
                except ValueError:
                    pass
            # Extract phase after closing bracket
            phase = 0.0
            after = val[bracket_match.end():].strip()
            if after:
                try:
                    phase = float(after)
                except ValueError:
                    pass
            return nums, phase
        # No brackets -- try splitting as space-separated numbers
        nums = []
        for part in val.split():
            try:
                nums.append(float(part))
            except ValueError:
                pass
        return nums, 0.0
    # Handle nested tuple/list: ([dash_array], phase) from newer PyMuPDF
    if isinstance(val, (tuple, list)) and len(val) >= 1:
        if isinstance(val[0], (tuple, list)):
            phase = 0.0
            if len(val) >= 2:
                try:
                    phase = float(val[1])
                except (TypeError, ValueError):
                    pass
            return _as_float_list(val[0]), phase
    # Already a flat list/tuple
    return _as_float_list(val), 0.0


# ──────────────────────────────────────────────────────────────────────
# Color normalization
# ──────────────────────────────────────────────────────────────────────
def _clamp01(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _norm_color(col) -> Tuple[float, float, float]:
    """Normalize any PyMuPDF color representation to (r, g, b) in [0..1]."""
    if col is None:
        return (0.0, 0.0, 0.0)
    try:
        if isinstance(col, int) and not isinstance(col, bool):
            if col < 0:
                return (0.0, 0.0, 0.0)
            packed = int(col) & 0xFFFFFF
            return (
                ((packed >> 16) & 0xFF) / 255.0,
                ((packed >> 8) & 0xFF) / 255.0,
                (packed & 0xFF) / 255.0,
            )
        if isinstance(col, float):
            g = _clamp01(col)
            return (g, g, g)
        vals = []
        for c in col:
            f = _as_float(c)
            vals.append(_clamp01(f) if f is not None else 0.0)
        if len(vals) == 0:
            return (0.0, 0.0, 0.0)
        if len(vals) == 1:
            return (vals[0], vals[0], vals[0])
        if len(vals) >= 4:
            # PyMuPDF can return CMYK tuples for some PDFs. Convert CMYK -> RGB
            # instead of incorrectly truncating to the first 3 channels.
            c, m, y, k = vals[0], vals[1], vals[2], vals[3]
            r = (1.0 - c) * (1.0 - k)
            g = (1.0 - m) * (1.0 - k)
            b = (1.0 - y) * (1.0 - k)
            return (_clamp01(r), _clamp01(g), _clamp01(b))
        while len(vals) < 3:
            vals.append(vals[-1])
        return (vals[0], vals[1], vals[2])
    except (TypeError, ValueError, AttributeError):
        return (0.0, 0.0, 0.0)


def _optional_color(col) -> Optional[Tuple[float, float, float]]:
    """Return normalized RGB only when the source PDF actually supplied a color."""
    if col is None:
        return None
    return _norm_color(col)


# ──────────────────────────────────────────────────────────────────────
# Options
# ──────────────────────────────────────────────────────────────────────
@dataclass
class ImportOptions:
    pages: Optional[List[int]] = None       # 1-based page numbers; None → [1]
    scale_to_mm: bool = True                # convert PDF points → mm
    user_scale: float = 1.0                 # additional multiplier
    flip_y: bool = True                     # PDF Y-down → CAD Y-up
    join_tol: float = 0.1                   # mm — snap endpoints
    min_seg_len: float = 0.0                # skip degenerate micro-edges
    curve_step_mm: float = 0.5              # Bezier linearization chord
    make_faces: bool = True                 # close filled paths → Part::Face
    import_text: bool = True
    text_mode: str = "3d_text"            # "text" | "labels" | "3d_text" | "glyphs" | "geometry" | "raster" | "none"
    # Retained for configuration compatibility. Exact per-span placement is
    # mandatory for requested Labels and cannot be disabled by this value.
    strict_text_fidelity: bool = True
    group_by_color: bool = True
    assign_linewidth: bool = True
    map_dashes: bool = True
    verbose: bool = True
    create_top_group: bool = True
    hatch_to_faces: bool = True
    hatch_mode: str = "import"              # "import" | "skip" | "group"
    ignore_images: bool = False
    raster_fallback: bool = True             # render page as image if no vectors
    raster_dpi: int = 300                    # DPI for raster fallback rendering (BCS-ARCH-001)
    raster_dpi_user_set: bool = False        # True when user explicitly chose the DPI
    # Import mode: "auto" | "vector" | "raster" | "hybrid"  (BCS-ARCH-001)
    #   auto    — detect scanned/image-heavy and vector-glyph-flood pages
    #   vectors — vector geometry only (original behavior)
    #   raster  — render full page as image, skip vectors
    #   hybrid  — raster background + vector geometry on top
    import_mode: str = "auto"
    # Optional 3D model generation.  "auto" only runs when text evidence
    # indicates an honest third dimension; "extrude" forces closed-shape
    # extrusion with page-background guards.
    model3d_mode: str = "off"              # "off" | "auto" | "extrude"
    model3d_depth_mm: float = 3.175        # default 1/8 in plate thickness
    max_bezier_segments: int = 128
    # Arc reconstruction
    detect_arcs: bool = True
    arc_fit_tol_mm: float = 0.08
    min_arc_angle_deg: float = 5.0
    arc_sampling_pts: int = 7
    # Layering
    layer_mode: str = "auto"                # "auto" | "ocg" | "color" | "none"
    # Object-count management (prevents Windows GDI handle exhaustion)
    compound_batch_size: int = 200          # batch N shapes into one Part::Compound
    #   0 = no batching (original behavior, risky on large PDFs)
    # Heavy-page safe mode — auto-engaged when drawing groups exceed threshold
    heavy_page_threshold: int = 3000        # above this: larger batches, throttled
    #   progress updates, deferred arc fitting on polyline runs
    #   0 = never auto-engage heavy mode
    # Multi-page page placement:
    #   spread  - 20% page gap (default)
    #   compact - configurable smaller gap
    #   touch   - edge-to-edge
    #   overlay - same origin
    page_arrangement: str = "spread"
    page_gap_ratio: float = 0.20
    # Populated when import_mode == "auto" (BCS-ARCH-001 Rule 9).
    auto_resolved_mode: Optional[str] = None
    auto_reason: Optional[str] = None
    raster_page_count: int = 0
    raster_fallback_reasons: List[str] = field(default_factory=list)
    import_report_path: Optional[str] = None
    # ShapeString telemetry. A renderer failure is reported as a failed attempt;
    # it does not authorize changing the requested representation.
    shapestring_skips: Dict[str, int] = field(default_factory=dict)
    # Representation substitutions are permitted only after item-specific
    # impossibility is proven. Every authorized fallback carries stable source
    # ids plus the proof used to authorize it.
    text_mode_fallbacks: List[Dict[str, Any]] = field(default_factory=list)
    # DELIVERED entity counts by bucket (native_3d_text, native_label, ...)
    # so actual_text_entity_types reflects what was created, not what was
    # requested (TEXTMODE-1 report honesty).
    text_delivered_counts: Dict[str, int] = field(default_factory=dict)
    # Per-item requested-representation attempt ledger.  Entries carry stable
    # source ids plus exact created/removed host object ids and cleanup state.
    text_delivery_attempts: List[Dict[str, Any]] = field(default_factory=list)
    # PyMuPDF extracts in unrotated crop-box coordinates.  The active page's
    # rotation matrix maps those coordinates to the displayed page exactly once.
    _page_rotation_matrix: Optional[Tuple[float, float, float, float, float, float]] = None
    # Scale detection telemetry for import_report cross-check (Round 5).
    resolved_scale: Optional[Dict[str, Any]] = None
    scale_hints: Dict[str, Any] = field(default_factory=dict)
    phase_timings_ms: Dict[str, float] = field(default_factory=dict)


def _default_import_report_path(pdf_path: str) -> str:
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    return os.path.join(tempfile.gettempdir(), f"{base}_import_report.json")


def _pdf_file_sha256(pdf_path: str) -> str:
    digest = hashlib.sha256()
    with open(pdf_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _report_fallback_state(opts: ImportOptions):
    # Raster is a first-class requested representation.  Selecting it explicitly
    # is not evidence that the importer failed to deliver another representation.
    if opts.import_mode == "raster":
        return False, None
    fallback_used = (
        opts.raster_page_count > 0
        or opts.auto_resolved_mode == "raster"
    )
    if opts.raster_page_count > 0:
        pages = opts.raster_page_count
        reasons = []
        for reason in getattr(opts, "raster_fallback_reasons", []) or []:
            reason = str(reason).strip()
            if reason and reason not in reasons:
                reasons.append(reason)
        if len(reasons) == 1:
            fallback_reason = reasons[0]
        elif len(reasons) > 1:
            label = f"raster_fallback_{pages}_page{'s' if pages != 1 else ''}"
            fallback_reason = f"{label}: {'; '.join(reasons[:4])}"
            if len(reasons) > 4:
                fallback_reason += f"; +{len(reasons) - 4} more"
        else:
            fallback_reason = f"raster_fallback_{pages}_page{'s' if pages != 1 else ''}"
    elif opts.auto_resolved_mode == "raster":
        fallback_reason = opts.auto_reason
    else:
        fallback_reason = None
    return fallback_used, fallback_reason


def _record_raster_page(opts: ImportOptions, reason: Optional[str] = None) -> None:
    opts.raster_page_count += 1
    reason = str(reason or "").strip()
    if reason:
        opts.raster_fallback_reasons.append(reason)


def _auto_raster_needs_text_overlay(
    effective_mode: str,
    source_text_blocks: Optional[int],
    opts: ImportOptions,
) -> bool:
    """Keep a Raster page strategy from overriding an explicit text choice."""
    return bool(
        str(effective_mode or "").strip().lower() == "raster"
        and _raster_page_requires_text_contract_probe(opts)
    )


def _raster_page_requires_text_contract_probe(opts: ImportOptions) -> bool:
    """Return whether a Raster page must still prove structural text delivery."""
    return bool(
        bool(getattr(opts, "import_text", False))
        and str(getattr(opts, "text_mode", "") or "").strip().lower()
        in {"text", "labels", "3d_text", "glyphs", "geometry"}
    )


def _resolve_raster_text_contract_mode(
    effective_mode: str,
    source_text_blocks: Optional[int],
    opts: ImportOptions,
) -> Tuple[str, bool]:
    """Preserve a structural text request without a masking page raster.

    The boolean result means the page raster remains a background while the
    requested structural text representation is rendered or proven impossible.
    An explicitly selected Raster page strategy retains that background.  An
    Auto-classified page with source text continues through the structural
    Hybrid path instead of receiving a complete-page masking raster.
    """
    normalized = str(effective_mode or "").strip().lower()
    if not _auto_raster_needs_text_overlay(normalized, source_text_blocks, opts):
        return normalized, False
    if str(getattr(opts, "import_mode", "") or "").strip().lower() == "raster":
        return "raster", True
    if source_text_blocks is None:
        return "hybrid", False
    if int(source_text_blocks or 0) > 0:
        return "hybrid", False
    return "raster", True


def _should_place_full_page_raster(effective_mode: str) -> bool:
    """Full-page raster is reserved for the Raster path, never Hybrid."""
    return str(effective_mode or "").strip().lower() == "raster"


def _merge_page_scale_into_opts(opts: ImportOptions, resolved) -> None:
    """Merge one page's scale detection into multi-page import_report fields."""
    if resolved is None or float(getattr(resolved, "confidence", 0) or 0) <= 0:
        return
    current = getattr(opts, "resolved_scale", None) or {}
    if not current or float(resolved.confidence) > float(current.get("confidence", 0)):
        opts.resolved_scale = {
            "factor": resolved.factor,
            "notation": resolved.notation,
            "source": resolved.source,
            "confidence": resolved.confidence,
            "fallback_reason": resolved.fallback_reason,
        }
    hints = dict(getattr(opts, "scale_hints", {}) or {})
    alts = {float(v) for v in hints.get("alternate_scale_factors") or []}
    if resolved.factor:
        alts.add(float(resolved.factor))
    hints["alternate_scale_factors"] = sorted(alts)
    opts.scale_hints = hints


def _pymupdf_version() -> str:
    return str(getattr(fitz, "__version__", "") or "")


def _freecad_version() -> str:
    try:
        ver = getattr(FreeCAD, "Version", None)
        if callable(ver):
            return str(ver() or "")
        return str(ver or "")
    except (AttributeError, RuntimeError, TypeError):
        return ""


def _importer_version() -> str:
    pkg_xml = os.path.join(_mod_root, "package.xml")
    try:
        with open(pkg_xml, "r", encoding="utf-8") as f:
            m = re.search(r"<version>(.*?)</version>", f.read())
        if m:
            return m.group(1).strip()
    except OSError:
        pass
    return ""


def _model3d_report_payload(opts: ImportOptions) -> Dict[str, Any]:
    mode = _normalize_model3d_mode(getattr(opts, "model3d_mode", "off"))
    intent = getattr(opts, "_model3d_intent", None)
    solids = int(getattr(opts, "_model3d_solids", 0) or 0)
    payload: Dict[str, Any] = {
        "supported": True,
        "enabled": mode != "off",
        "mode": mode,
        "depth_mm": round(_model3d_depth_units(opts), 4),
        "solids_created": solids,
    }
    if mode == "off":
        payload["skipped_reason"] = "option_off"
    elif mode == "auto" and not bool(getattr(opts, "_model3d_intent_feasible", False)):
        reason = None
        if isinstance(intent, dict):
            reason = intent.get("skipped_reason")
        payload["skipped_reason"] = reason or "no_3d_intent_evidence"
    elif solids == 0:
        payload["skipped_reason"] = "no_extrudable_closed_regions"
    return payload


def _build_text_representation_delivery(
    opts: ImportOptions,
    attempts: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Summarize the item-bound attempt ledger without inventing success."""

    def normalize_type(value: Any) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"", "none", "off", "disabled"}:
            return "none" if raw else ""
        try:
            return _normalize_requested_text_type(raw)
        except (TypeError, ValueError):
            return ""

    def source_page_number(source_item_id: str) -> Optional[int]:
        match = re.match(r"^p([1-9][0-9]*):", source_item_id)
        return int(match.group(1)) if match else None

    def proof_authority_error(
        proof: Any,
        *,
        source_item_id: str,
        requested_type: str,
        attempted_type: str,
    ) -> str:
        """Return why a report proof is not bound to one exact source item."""

        if not isinstance(proof, dict):
            return "proof_missing"
        if proof.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY:
            return "proof_importer_identity_invalid"
        digest = proof.get("pdf_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            return "proof_pdf_sha256_invalid"
        page_number = proof.get("page_number")
        expected_page_number = source_page_number(source_item_id)
        if (
            type(page_number) is not int
            or page_number <= 0
            or expected_page_number is None
            or page_number != expected_page_number
        ):
            return "proof_page_invalid"
        if proof.get("source_item_id") != source_item_id:
            return "proof_source_item_id_invalid"
        if normalize_type(proof.get("requested_type")) != requested_type:
            return "proof_requested_type_invalid"
        if normalize_type(proof.get("attempted_type")) != attempted_type:
            return "proof_attempted_type_invalid"

        attempted_source_results = proof.get("attempted_source_results")
        if not isinstance(attempted_source_results, list) or not attempted_source_results:
            return "proof_source_results_invalid"
        for result in attempted_source_results:
            if not isinstance(result, dict):
                return "proof_source_results_invalid"
            if "pdf_sha256" in result and result.get("pdf_sha256") != digest:
                return "proof_source_result_pdf_sha256_invalid"
            if "page_number" in result and (
                type(result.get("page_number")) is not int
                or result.get("page_number") != page_number
            ):
                return "proof_source_result_page_invalid"
            if (
                "source_item_id" in result
                and result.get("source_item_id") != source_item_id
            ):
                return "proof_source_result_item_id_invalid"
        if proof.get("reason_code") == "no_source_text_items":
            evidence = proof.get("evidence")
            if (
                not isinstance(evidence, dict)
                or evidence.get("text_dictionary_present") is not True
                or type(evidence.get("canonical_source_item_count")) is not int
                or evidence.get("canonical_source_item_count") != 0
                or evidence.get("visible_source_text_found") is not False
                or proof.get("attempted_sources_complete") is not True
                or len(attempted_source_results) != 1
            ):
                return "proof_no_source_evidence_invalid"
            result = attempted_source_results[0]
            if (
                result.get("source") != "pymupdf_text_dictionary"
                or result.get("outcome") != "not_found"
                or result.get("importer_identity")
                != FREECAD_TEXT_IMPORTER_IDENTITY
                or result.get("pdf_sha256") != digest
                or type(result.get("page_number")) is not int
                or result.get("page_number") != page_number
                or result.get("source_item_id") != source_item_id
                or result.get("source_item_ids") != []
                or type(result.get("canonical_source_item_count")) is not int
                or result.get("canonical_source_item_count") != 0
                or result.get("visible_source_text_found") is not False
            ):
                return "proof_no_source_result_invalid"
        return ""

    requested_raw = str(getattr(opts, "text_mode", "") or "none").strip().lower()
    requested = normalize_type(requested_raw)
    expected_pdf_digest = str(getattr(opts, "_pdf_sha256", "") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_pdf_digest) is None:
        expected_pdf_digest = ""
    required = bool(getattr(opts, "import_text", False) and requested != "none")
    raw_attempts = list(attempts or [])
    normalized_attempts = [
        dict(attempt) for attempt in raw_attempts if isinstance(attempt, dict)
    ]
    source_ids: List[str] = []
    attempts_by_source: Dict[str, List[Dict[str, Any]]] = {}
    invalid_reasons: List[str] = []

    if len(normalized_attempts) != len(raw_attempts):
        invalid_reasons.append("malformed_delivery_attempt")

    if required and not requested:
        invalid_reasons.append("requested_type_unsupported")

    for index, attempt in enumerate(normalized_attempts):
        source_id = attempt.get("source_item_id")
        if (
            not isinstance(source_id, str)
            or not source_id
            or source_id != source_id.strip()
        ):
            invalid_reasons.append("attempt_%d_missing_source_item_id" % index)
            continue
        if source_id not in source_ids:
            source_ids.append(source_id)
        attempts_by_source.setdefault(source_id, []).append(attempt)

    raw_fallback_events = list(getattr(opts, "text_mode_fallbacks", []) or [])
    fallback_events = [
        dict(event)
        for event in raw_fallback_events
        if isinstance(event, dict)
    ]
    if len(fallback_events) != len(raw_fallback_events):
        invalid_reasons.append("malformed_fallback_event")
    final_types: Dict[str, str] = {}
    terminal_attempts: List[Dict[str, Any]] = []
    removed_entity_ids: List[str] = []
    entity_identity_owners: Dict[str, str] = {}
    matched_fallback_event_indexes = set()

    def claim_entity_identities(source_id: str, entity_ids: Any) -> None:
        if not isinstance(entity_ids, list):
            return
        for entity_id in entity_ids:
            if not isinstance(entity_id, str) or not entity_id:
                continue
            prior_owner = entity_identity_owners.get(entity_id)
            if prior_owner is None:
                entity_identity_owners[entity_id] = source_id
            elif prior_owner != source_id:
                invalid_reasons.append(
                    "%s_entity_identity_shared_with_%s" % (source_id, prior_owner)
                )

    for source_id in source_ids:
        source_attempts = attempts_by_source[source_id]
        terminal = source_attempts[-1]
        terminal_attempts.append(dict(terminal))
        attempted_sequence = [
            normalize_type(attempt.get("attempted_type"))
            for attempt in source_attempts
        ]
        requested_sequence = [
            normalize_type(attempt.get("requested_type"))
            for attempt in source_attempts
        ]
        if any(value != requested for value in requested_sequence):
            invalid_reasons.append("%s_requested_type_drift" % source_id)
        ladder = list(TEXT_ITEM_FALLBACK_LADDERS.get(requested, ()))
        if (
            not ladder
            or any(not attempted_type for attempted_type in attempted_sequence)
            or attempted_sequence != ladder[: len(attempted_sequence)]
        ):
            invalid_reasons.append("%s_non_adjacent_or_repeated_ladder" % source_id)

        for local_index, prior in enumerate(source_attempts[:-1]):
            prior_proof = prior.get("proof")
            prior_attempted = attempted_sequence[local_index]
            created_ids = prior.get("created_entity_ids")
            removed_ids = prior.get("removed_entity_ids")
            if str(prior.get("outcome") or "").strip().lower() != "proven_impossible":
                invalid_reasons.append(
                    "%s_attempt_%d_not_proven_impossible" % (source_id, local_index)
                )
            if prior.get("cleanup_complete") is not True:
                invalid_reasons.append(
                    "%s_attempt_%d_cleanup_unverified" % (source_id, local_index)
                )
            if prior.get("final_type") is not None:
                invalid_reasons.append(
                    "%s_attempt_%d_impossible_final_type_present"
                    % (source_id, local_index)
                )
            if (
                not isinstance(created_ids, list)
                or not isinstance(removed_ids, list)
                or any(
                    not isinstance(entity_id, str)
                    or not entity_id
                    or entity_id != entity_id.strip()
                    for entity_id in list(created_ids or []) + list(removed_ids or [])
                )
                or len(created_ids or []) != len(set(created_ids or []))
                or len(removed_ids or []) != len(set(removed_ids or []))
            ):
                invalid_reasons.append(
                    "%s_attempt_%d_cleanup_ids_invalid" % (source_id, local_index)
                )
            elif set(created_ids) != set(removed_ids):
                invalid_reasons.append(
                    "%s_attempt_%d_cleanup_entity_mismatch" % (source_id, local_index)
                )
            if isinstance(removed_ids, list):
                removed_entity_ids.extend(removed_ids)
            claim_entity_identities(source_id, created_ids)
            claim_entity_identities(source_id, removed_ids)
            if (
                not isinstance(prior_proof, dict)
                or prior_proof.get("item_specific_proven_impossible") is not True
                or str(prior_proof.get("source_item_id") or "") != source_id
                or normalize_type(prior_proof.get("requested_type")) != requested
                or normalize_type(prior_proof.get("attempted_type")) != prior_attempted
                or prior_proof.get("cleanup_complete") is not True
                or prior_proof.get("created_entity_ids") != created_ids
                or prior_proof.get("removed_entity_ids") != removed_ids
                or not str(prior.get("reason_code") or "").strip()
                or prior_proof.get("reason_code") != prior.get("reason_code")
                or not isinstance(prior_proof.get("evidence"), dict)
                or not prior_proof.get("evidence")
                or not isinstance(prior_proof.get("attempted_source_results"), list)
                or not prior_proof.get("attempted_source_results")
                or prior_proof.get("attempted_sources_complete") is not True
            ):
                invalid_reasons.append(
                    "%s_attempt_%d_proof_invalid" % (source_id, local_index)
                )
            proof_error = proof_authority_error(
                prior_proof,
                source_item_id=source_id,
                requested_type=requested,
                attempted_type=prior_attempted,
            )
            if proof_error:
                invalid_reasons.append(
                    "%s_attempt_%d_%s" % (source_id, local_index, proof_error)
                )
            elif (
                expected_pdf_digest
                and prior_proof.get("pdf_sha256") != expected_pdf_digest
            ):
                invalid_reasons.append(
                    "%s_attempt_%d_proof_pdf_sha256_mismatch"
                    % (source_id, local_index)
                )

        outcome = str(terminal.get("outcome") or "").strip().lower()
        final_type = normalize_type(terminal.get("final_type"))
        terminal_attempted = normalize_type(terminal.get("attempted_type"))
        final_types[source_id] = final_type
        if outcome != "verified":
            invalid_reasons.append("%s_terminal_outcome_%s" % (source_id, outcome or "missing"))
        if terminal.get("cleanup_complete") is not True:
            invalid_reasons.append("%s_cleanup_unverified" % source_id)
        if not isinstance(terminal.get("evidence"), dict) or not terminal.get("evidence"):
            invalid_reasons.append("%s_host_evidence_missing" % source_id)
        elif final_type in {"labels", "text"}:
            native_evidence = terminal["evidence"]
            expected_proxy_type = "Label" if final_type == "labels" else "Text"
            if (
                native_evidence.get("host_entity_type") != "App::FeaturePython"
                or native_evidence.get("host_proxy_type") != expected_proxy_type
                or not isinstance(native_evidence.get("source_text"), str)
                or not native_evidence.get("source_text")
                or native_evidence.get("source_text_preserved") is not True
                or native_evidence.get("view_style_verified") is not True
                or (
                    final_type == "labels"
                    and native_evidence.get("label_marker_absent") is not True
                )
            ):
                invalid_reasons.append(
                    "%s_%s_visual_evidence_unverified" % (source_id, final_type)
                )
        if not final_type or final_type == "none":
            invalid_reasons.append("%s_final_type_missing" % source_id)
        if not terminal_attempted or final_type != terminal_attempted:
            invalid_reasons.append("%s_terminal_type_drift" % source_id)

        created_ids = terminal.get("created_entity_ids")
        removed_ids = terminal.get("removed_entity_ids", [])
        delivery_ids = terminal.get("delivery_entity_ids")
        if delivery_ids is None:
            delivery_ids = created_ids
        support_ids = terminal.get("support_entity_ids", [])
        terminal_id_lists = (created_ids, removed_ids, delivery_ids, support_ids)
        if (
            any(not isinstance(values, list) for values in terminal_id_lists)
            or any(
                not isinstance(entity_id, str)
                or not entity_id
                or entity_id != entity_id.strip()
                for values in terminal_id_lists
                for entity_id in (values if isinstance(values, list) else [])
            )
            or any(
                len(values) != len(set(values))
                for values in terminal_id_lists
                if isinstance(values, list)
            )
        ):
            invalid_reasons.append("%s_terminal_entity_ids_invalid" % source_id)
        else:
            created_set = set(created_ids)
            removed_set = set(removed_ids)
            delivery_set = set(delivery_ids)
            support_set = set(support_ids)
            removed_entity_ids.extend(removed_ids)
            claim_entity_identities(source_id, created_ids)
            claim_entity_identities(source_id, removed_ids)
            if (
                not created_set
                or not delivery_set
                or not removed_set.issubset(created_set)
                or not delivery_set.issubset(created_set.difference(removed_set))
                or not support_set.issubset(created_set.difference(removed_set))
                or delivery_set.intersection(support_set)
                or delivery_set.union(support_set)
                != created_set.difference(removed_set)
            ):
                invalid_reasons.append("%s_terminal_ownership_invalid" % source_id)
            reported_delivery_count = terminal.get("delivery_count")
            if "delivery_count" in terminal and (
                type(reported_delivery_count) is not int
                or reported_delivery_count <= 0
                or (
                    final_type != "geometry"
                    and reported_delivery_count != len(delivery_set)
                )
            ):
                invalid_reasons.append("%s_delivery_count_invalid" % source_id)

        expected_proof_chain = [
            prior.get("proof") for prior in source_attempts[:-1]
        ]
        terminal_attempted_types = terminal.get("attempted_types")
        terminal_proof_chain = terminal.get("proof_chain")
        if len(source_attempts) > 1 and (
            terminal_attempted_types != attempted_sequence
            or terminal_proof_chain != expected_proof_chain
        ):
            invalid_reasons.append("%s_terminal_proof_chain_invalid" % source_id)
        elif terminal_attempted_types is not None and (
            terminal_attempted_types != attempted_sequence
        ):
            invalid_reasons.append("%s_terminal_attempted_types_invalid" % source_id)
        if final_type and final_type != requested:
            expected_transitions = [
                {"from": left, "to": right}
                for left, right in zip(attempted_sequence, attempted_sequence[1:])
            ]
            expected_created_ids = [
                entity_id
                for prior in source_attempts[:-1]
                for entity_id in list(prior.get("created_entity_ids") or [])
            ]
            expected_removed_ids = [
                entity_id
                for prior in source_attempts[:-1]
                for entity_id in list(prior.get("removed_entity_ids") or [])
            ]
            fallback_proof_attempted = (
                attempted_sequence[-2] if len(attempted_sequence) >= 2 else ""
            )
            matching_fallback_event_indexes = [
                event_index
                for event_index, event in enumerate(fallback_events)
                if normalize_type(event.get("requested")) == requested
                and normalize_type(event.get("delivered")) == final_type
                and event.get("source_item_ids") == [source_id]
                and type(event.get("count")) is int
                and event.get("count") == 1
                and isinstance(event.get("proof"), dict)
                and event["proof"].get("item_specific_proven_impossible") is True
                and str(event["proof"].get("source_item_id") or "") == source_id
                and normalize_type(event["proof"].get("requested_type")) == requested
                and normalize_type(event["proof"].get("attempted_type"))
                == fallback_proof_attempted
                and event["proof"].get("attempted_types") == attempted_sequence
                and event["proof"].get("proof_chain") == expected_proof_chain
                and event["proof"].get("transition_chain") == expected_transitions
                and event["proof"].get("created_entity_ids") == expected_created_ids
                and event["proof"].get("removed_entity_ids") == expected_removed_ids
                and event["proof"].get("cleanup_complete") is True
                and isinstance(event["proof"].get("evidence"), dict)
                and bool(event["proof"].get("evidence"))
                and not proof_authority_error(
                    event["proof"],
                    source_item_id=source_id,
                    requested_type=requested,
                    attempted_type=fallback_proof_attempted,
                )
                and (
                    not expected_pdf_digest
                    or event["proof"].get("pdf_sha256") == expected_pdf_digest
                )
            ]
            fallback_verified = len(matching_fallback_event_indexes) == 1
            if fallback_verified:
                matched_fallback_event_indexes.add(matching_fallback_event_indexes[0])
            if not fallback_verified:
                invalid_reasons.append("%s_fallback_proof_missing" % source_id)

    if len(matched_fallback_event_indexes) != len(fallback_events):
        invalid_reasons.append("unbound_or_inflated_fallback_event")

    if required and not source_ids:
        invalid_reasons.append("no_item_bound_delivery_attempts")

    verified = bool(not required or (source_ids and not invalid_reasons))
    return {
        "required": required,
        "requested_type": requested,
        "attempt_count": len(normalized_attempts),
        "source_item_ids": source_ids,
        "terminal_attempts": terminal_attempts,
        "removed_entity_ids": list(dict.fromkeys(removed_entity_ids)),
        "final_types": final_types,
        "invalid_reasons": list(dict.fromkeys(invalid_reasons)),
        "verified": verified,
    }


def write_import_report(
    *,
    pdf_path: str,
    output_path: str,
    opts: ImportOptions,
    pages_imported: int,
    total_pages: int,
    primitive_count: int = 0,
    text_count: int = 0,
    image_count: int = 0,
    layer_count: int = 0,
    elapsed_ms: float = 0.0,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
) -> str:
    """Emit bcs.import_report/1.1 JSON for one import run."""
    from pdfcadcore.fitz_loader import sample_process_mb
    from pdfcadcore.import_report import (
        TEXT_ENTITY_DELIVERED_BUCKETS,
        build_actual_text_entity_types,
        build_import_report,
    )

    phases = dict(getattr(opts, "phase_timings_ms", {}) or {})
    if elapsed_ms > 0 and "total_ms" not in phases:
        phases["total_ms"] = float(elapsed_ms)

    skips = dict(getattr(opts, "shapestring_skips", {}) or {})
    extra = {
        "auto_resolved_mode": opts.auto_resolved_mode,
        "auto_reason": opts.auto_reason,
        "raster_page_count": opts.raster_page_count,
        "raster_fallback_reasons": list(opts.raster_fallback_reasons),
        "model_3d_intent": getattr(opts, "_model3d_intent", None),
        "model_3d": _model3d_report_payload(opts),
    }
    
    # Add text entity info to extra if available
    if hasattr(opts, '_report_extra') and opts._report_extra:
        extra.update(opts._report_extra)
    raw_delivered_counts = getattr(opts, "text_delivered_counts", {}) or {}
    raw_delivered_count_items = (
        list(raw_delivered_counts.items())
        if isinstance(raw_delivered_counts, dict)
        else []
    )
    delivered_counts_valid = bool(
        isinstance(raw_delivered_counts, dict)
        and all(
            isinstance(bucket, str)
            and bool(bucket)
            and bucket in TEXT_ENTITY_DELIVERED_BUCKETS
            and type(value) is int
            and value >= 0
            for bucket, value in raw_delivered_count_items
        )
    )
    delivered_counts = {
        bucket: value
        for bucket, value in raw_delivered_count_items
        if bucket in TEXT_ENTITY_DELIVERED_BUCKETS
        and type(value) is int
        and value > 0
    }
    entity_info = extra.get("actual_text_entity_types")
    if isinstance(entity_info, dict):
        extra["actual_text_entity_types"] = build_actual_text_entity_types(
            host_app="freecad",
            text_mode=str(entity_info.get("entity_type") or opts.text_mode or "3d_text"),
            count=int(entity_info.get("count") or 0),
            font_rendered=bool(entity_info.get("font_rendered")),
            examples=list(entity_info.get("examples") or []),
            delivered_counts=delivered_counts or None,
        )
    else:
        extra["actual_text_entity_types"] = build_actual_text_entity_types(
            host_app="freecad",
            text_mode=str(opts.text_mode or "none"),
            count=0,
            font_rendered=False,
            examples=[],
            delivered_counts=delivered_counts,
        )
    extra["actual_text_entity_types"]["delivery_counts_valid"] = (
        delivered_counts_valid
    )

    text_attempts = [
        dict(attempt)
        for attempt in (getattr(opts, "text_delivery_attempts", []) or [])
    ]
    extra["text_delivery_attempts"] = text_attempts
    extra["text_representation_delivery"] = _build_text_representation_delivery(
        opts,
        text_attempts,
    )

    font_stage_failures = [
        dict(failure)
        for failure in (getattr(opts, "_font_stage_failures", []) or [])
    ]
    if font_stage_failures:
        extra["font_stage_failures"] = font_stage_failures

    # Surface only proof-gated representation substitutions. The dominant
    # event lands in fallback.text and the complete ledger stays in extra.
    text_fallback_events = [
        dict(event)
        for event in (getattr(opts, "text_mode_fallbacks", []) or [])
        if isinstance(event, dict)
        and type(event.get("count")) is int
        and event.get("count") > 0
        and bool((event.get("proof") or {}).get("item_specific_proven_impossible"))
        and bool(event.get("source_item_ids"))
    ]
    text_fallback: Optional[Dict[str, Any]] = None
    if text_fallback_events:
        text_fallback = dict(
            max(text_fallback_events, key=lambda ev: ev.get("count", 0))
        )
        extra["text_mode_fallbacks"] = text_fallback_events

    delivery_summary = extra["text_representation_delivery"]
    extra.pop("representation_contract_violation", None)
    if (
        delivery_summary.get("required") is True
        and delivery_summary.get("verified") is not True
    ):
        extra["representation_contract_violation"] = {
            "requested_type": delivery_summary.get("requested_type"),
            "reason": "invalid_item_bound_representation_delivery",
            "invalid_reasons": list(delivery_summary.get("invalid_reasons") or []),
        }
    if skips:
        extra["shapestring_skips"] = skips
        extra["shapestring_skip_total"] = sum(int(v) for v in skips.values())
    resolved_scale = getattr(opts, "resolved_scale", None)
    scale_hints = getattr(opts, "scale_hints", None)
    if resolved_scale:
        extra["resolved_scale"] = resolved_scale
    if scale_hints:
        extra["scale_hints"] = scale_hints

    report = build_import_report(
        host_app="freecad",
        host_version=_freecad_version(),
        runtime_lang="python",
        runtime_version=(
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        importer_version=_importer_version(),
        pdf_path=pdf_path,
        mode=opts.import_mode,
        pages=int(pages_imported),
        primitive_count=primitive_count,
        text_count=text_count,
        image_count=image_count,
        layer_count=layer_count,
        elapsed_ms=elapsed_ms,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        pdf_engine_version=_pymupdf_version(),
        import_text=bool(opts.import_text),
        text_mode=str(opts.text_mode or "3d_text"),
        text_fallback=text_fallback,
        peak_mb=sample_process_mb(),
        performance_phases=phases or None,
        extra=extra,
    )

    if text_fallback and isinstance(getattr(report, "fallback", None), dict):
        fallback_text = report.fallback.get("text")
        if isinstance(fallback_text, dict):
            fallback_text["source_item_ids"] = list(
                text_fallback.get("source_item_ids") or []
            )
            fallback_text["proof"] = dict(text_fallback.get("proof") or {})

    provenance_objects = list(getattr(opts, "_source_provenance_objects", []) or [])
    if provenance_objects:
        from pdfcadcore.source_provenance import (
            ensure_import_session_id,
            write_source_provenance_sidecar,
        )

        session_id = ensure_import_session_id(opts)
        sidecar_path = str(Path(output_path).with_name("source_provenance.json"))
        build_stamp = str((report.report_meta or {}).get("build_stamp") or "")
        write_source_provenance_sidecar(
            output_path=sidecar_path,
            import_session_id=session_id,
            pdf_path=pdf_path,
            objects=provenance_objects,
            host_app="freecad",
            importer_version=_importer_version(),
            build_stamp=build_stamp,
            page_count=int(total_pages or pages_imported or 0) or None,
        )
        extra_ref = report.extra
        extra_ref["source_provenance_path"] = Path(sidecar_path).name
        extra_ref["source_provenance"] = {
            "schema": "bcs.source_provenance/1.0",
            "import_session_id": session_id,
            "object_count": len(provenance_objects),
        }

    from pdfcadcore.parts_bootstrap import extract_bootstrap_rows, write_parts_bootstrap_sidecar

    bootstrap_path = str(Path(output_path).with_name("parts_bootstrap.json"))
    build_stamp = str((report.report_meta or {}).get("build_stamp") or "")
    bootstrap_text_items = list(getattr(opts, "_bootstrap_text_items", []) or [])
    if not bootstrap_text_items:
        for page_text in list(getattr(opts, "_model3d_text_evidence", []) or []):
            for line in str(page_text).splitlines():
                line = line.strip()
                if line:
                    bootstrap_text_items.append({"text": line, "page": 1})
    bootstrap_rows = extract_bootstrap_rows(bootstrap_text_items)
    import_build_stamp = {
        "host": "freecad",
        "semver": _importer_version(),
    }
    if build_stamp:
        import_build_stamp["build_stamp"] = build_stamp
    write_parts_bootstrap_sidecar(
        bootstrap_path,
        pdf_path,
        page_count=int(total_pages or pages_imported or 0) or None,
        rows=bootstrap_rows,
        import_build_stamp=import_build_stamp,
    )
    extra_ref = report.extra
    extra_ref["parts_bootstrap"] = {
        "schema": "bcs.parts_bootstrap/1.0",
        "sidecar_path": Path(bootstrap_path).name,
        "row_count": len(bootstrap_rows),
        "note": "BOM row extraction from drawing text" if bootstrap_rows else "no BOM rows detected",
    }

    report.write_json(output_path)
    try:
        import_build_stamp["report_sha256"] = hashlib.sha256(
            Path(output_path).read_bytes()
        ).hexdigest()
        write_parts_bootstrap_sidecar(
            bootstrap_path,
            pdf_path,
            page_count=int(total_pages or pages_imported or 0) or None,
            rows=bootstrap_rows,
            import_build_stamp=import_build_stamp,
        )
    except OSError:
        pass
    return output_path


# ──────────────────────────────────────────────────────────────────────
# Coordinate transform
# ──────────────────────────────────────────────────────────────────────
def _page_matrix_values(opts: ImportOptions) -> Tuple[float, float, float, float, float, float]:
    raw = getattr(opts, "_page_rotation_matrix", None)
    if raw and len(raw) >= 6:
        try:
            return tuple(float(value) for value in raw[:6])
        except (TypeError, ValueError):
            pass
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _transform_pdf_direction(
    direction: Tuple[float, float], opts: Optional[ImportOptions] = None
) -> Tuple[float, float]:
    dx, dy = float(direction[0]), float(direction[1])
    if opts is not None:
        a, b, c, d, _e, _f = _page_matrix_values(opts)
        dx, dy = a * dx + c * dy, b * dx + d * dy
        if opts.flip_y:
            dy = -dy
    else:
        # Historical callers supply an unrotated PDF direction.  FreeCAD is
        # Y-up, so preserve the established PDF Y-down conversion.
        dy = -dy
    return dx, dy


def _to_fc(xy: Tuple[float, float], page_h: float,
           opts: ImportOptions, scale: float) -> "Vector":
    """Transform a PDF coordinate pair into a FreeCAD Vector."""
    x, y = float(xy[0]), float(xy[1])
    a, b, c, d, e, f = _page_matrix_values(opts)
    x, y = a * x + c * y + e, b * x + d * y + f
    if opts.flip_y:
        y = page_h - y
    return _v(x * scale, y * scale, 0)


# ──────────────────────────────────────────────────────────────────────
# Cubic Bezier evaluation  (SAFE — never mutates vectors)
# ──────────────────────────────────────────────────────────────────────
def _bezier_point(p0: "Vector", p1: "Vector", p2: "Vector",
                  p3: "Vector", t: float) -> "Vector":
    """De Casteljau evaluation of cubic Bézier at parameter t ∈ [0,1].

    FreeCAD.Vector.multiply() is **in-place** and returns None, so we
    must use the ``*`` and ``+`` operators which return new Vectors.
    """
    u = 1.0 - t
    # B(t) = (1-t)^3·P0 + 3(1-t)^2·t·P1 + 3(1-t)·t^2·P2 + t^3·P3
    return (p0 * (u * u * u)
            + p1 * (3.0 * u * u * t)
            + p2 * (3.0 * u * t * t)
            + p3 * (t * t * t))


# ──────────────────────────────────────────────────────────────────────
# Circle / arc fitting  (Kåsa algebraic fit)
# ──────────────────────────────────────────────────────────────────────
def _circle_fit(points: List["Vector"]) -> Tuple["Vector", float, float]:
    """Return (center, radius, rms_error) via Kåsa algebraic circle fit."""
    n = len(points)
    if n < 3:
        raise ValueError("Need ≥ 3 points")
    sx  = sum(p.x for p in points)
    sy  = sum(p.y for p in points)
    sx2 = sum(p.x * p.x for p in points)
    sy2 = sum(p.y * p.y for p in points)
    sxy = sum(p.x * p.y for p in points)
    sz  = sum(p.x * p.x + p.y * p.y for p in points)
    sxz = sum(p.x * (p.x * p.x + p.y * p.y) for p in points)
    syz = sum(p.y * (p.x * p.x + p.y * p.y) for p in points)

    A = [[sx, sy, n], [sx2, sxy, sx], [sxy, sy2, sy]]
    B = [sz, sxz, syz]

    def det3(m):
        return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
              - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
              + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))

    D = det3(A)
    if abs(D) < 1e-12:
        raise ValueError("Singular matrix in circle fit")

    A1 = [[B[0], A[0][1], A[0][2]], [B[1], A[1][1], A[1][2]], [B[2], A[2][1], A[2][2]]]
    A2 = [[A[0][0], B[0], A[0][2]], [A[1][0], B[1], A[1][2]], [A[2][0], B[2], A[2][2]]]
    A3 = [[A[0][0], A[0][1], B[0]], [A[1][0], A[1][1], B[1]], [A[2][0], A[2][1], B[2]]]

    a = det3(A1) / D
    b = det3(A2) / D
    c = det3(A3) / D
    cx, cy = 0.5 * a, 0.5 * b
    r = math.sqrt(max(0, c + cx * cx + cy * cy))
    center = _v(cx, cy, 0)
    rms = math.sqrt(sum((_len2d(p, center) - r) ** 2 for p in points) / n)
    return center, r, rms


def _arc_from_cubic(p0, p1, p2, p3, opts: ImportOptions):
    """If cubic ≈ circular arc, return (start, mid, end) for Part.Arc."""
    if not opts.detect_arcs:
        return None
    m = max(3, opts.arc_sampling_pts)
    if m % 2 == 0:
        m += 1
    tvals = [i / (m - 1) for i in range(m)]
    pts = [_bezier_point(p0, p1, p2, p3, t) for t in tvals]
    try:
        center, r, rms = _circle_fit(pts)
    except ValueError:
        return None
    if rms > opts.arc_fit_tol_mm:
        return None

    # Guard against fits that only look good on average.
    max_err = max(abs(_len2d(p, center) - r) for p in pts)
    if max_err > max(opts.arc_fit_tol_mm * 1.8, r * 0.008):
        return None

    v0 = p0 - center
    v3 = p3 - center
    if v0.Length < ZERO_TOL or v3.Length < ZERO_TOL:
        return None
    a0 = math.atan2(v0.y, v0.x)
    a3 = math.atan2(v3.y, v3.x)
    d = a3 - a0
    while d <= -math.pi:
        d += 2 * math.pi
    while d > math.pi:
        d -= 2 * math.pi
    if abs(d) * 180.0 / math.pi < opts.min_arc_angle_deg:
        return None

    # Midpoint must align with the selected minor sweep.
    pmid = pts[len(pts) // 2]
    vm = pmid - center
    if vm.Length < ZERO_TOL:
        return None
    am = math.atan2(vm.y, vm.x)
    expected_mid = _normalize_angle(a0 + (d * 0.5))
    mid_diff = abs(_normalize_angle(am - expected_mid))
    if mid_diff > (math.pi / 4.0):
        return None

    # Tangents at cubic endpoints should be close to perpendicular to the
    # radius vector for a true circular arc.
    t0 = p1 - p0
    t3 = p3 - p2
    for tan, rad in ((t0, v0), (t3, v3)):
        if tan.Length <= ZERO_TOL or rad.Length <= ZERO_TOL:
            continue
        cosang = abs((tan.x * rad.x + tan.y * rad.y) / (tan.Length * rad.Length))
        if cosang > 0.35:
            return None

    return (p0, pmid, p3)


# ──────────────────────────────────────────────────────────────────────
# Edge / wire / face builders
# ──────────────────────────────────────────────────────────────────────
def _edge_line(p1: "Vector", p2: "Vector"):
    """Part.Edge from two points; returns None if degenerate."""
    try:
        if _len2d(p1, p2) <= ZERO_TOL:
            return None
        return Part.LineSegment(p1, p2).toShape()
    except (RuntimeError, ValueError, TypeError):
        return None


def _edge_arc(p1: "Vector", pmid: "Vector", p2: "Vector"):
    try:
        return Part.Arc(p1, pmid, p2).toShape()
    except (RuntimeError, ValueError, TypeError):
        return None


def _normalize_angle(angle: float) -> float:
    """Normalize angle to (-pi, pi]."""
    while angle <= -math.pi:
        angle += 2.0 * math.pi
    while angle > math.pi:
        angle -= 2.0 * math.pi
    return angle


def _polyline_run_is_smooth(verts: List["Vector"], max_turn_deg: float = 60.0) -> bool:
    """Return True when a run behaves like a smooth arc candidate.

    Rejects runs with hard corners or turn-direction reversals that commonly
    produce false giant arcs when circle-fitting mixed corner+line geometry.
    """
    if len(verts) < 5:
        return False

    max_turn = math.radians(max_turn_deg)
    prev_sign = 0
    valid_turns = 0

    for i in range(1, len(verts) - 1):
        a = verts[i] - verts[i - 1]
        b = verts[i + 1] - verts[i]
        ax, ay = a.x, a.y
        bx, by = b.x, b.y

        la = math.hypot(ax, ay)
        lb = math.hypot(bx, by)
        if la <= ZERO_TOL or lb <= ZERO_TOL:
            continue

        cross = ax * by - ay * bx
        dot = ax * bx + ay * by
        turn = abs(math.atan2(cross, dot))
        if turn > max_turn:
            return False

        sign = 1 if cross > 1e-9 else (-1 if cross < -1e-9 else 0)
        if sign != 0:
            if prev_sign != 0 and sign != prev_sign:
                return False
            prev_sign = sign

        valid_turns += 1

    return valid_turns >= 2


def _make_shape_obj(edges: List, closed: bool, make_face: bool, fc_doc=None):
    """Build Part::Feature from edges, optionally closing + making a Face."""
    if not edges:
        return None
    doc = fc_doc or FreeCAD.ActiveDocument
    try:
        wire = Part.Wire(edges)
        if closed and not wire.isClosed():
            # Use wire vertexes (topologically safe) instead of edge vertexes
            if wire.Vertexes:
                p0 = wire.Vertexes[0].Point
                pN = wire.Vertexes[-1].Point
                if _len2d(_v(p0.x, p0.y), _v(pN.x, pN.y)) > ZERO_TOL:
                    closer = Part.LineSegment(pN, p0).toShape()
                    wire = Part.Wire(edges + [closer])
        if make_face and wire.isClosed():
            try:
                face = Part.Face(wire)
                obj = doc.addObject("Part::Feature", "Face")
                obj.Shape = face
                return obj
            except (RuntimeError, ValueError, TypeError):
                pass
        obj = doc.addObject("Part::Feature", "Wire")
        obj.Shape = wire
        return obj
    except (RuntimeError, ValueError, TypeError):
        return None


def _normalize_model3d_mode(raw) -> str:
    mode = str(raw or "off").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in {"yes", "true", "on", "auto_if_evidence"}:
        return "auto"
    if mode in {"closed", "closed_shapes", "extrude_closed_shapes", "force"}:
        return "extrude"
    if mode in {"auto", "extrude"}:
        return mode
    return "off"


def _model3d_depth_units(opts: ImportOptions) -> float:
    try:
        depth = float(getattr(opts, "model3d_depth_mm", 3.175) or 3.175)
    except (TypeError, ValueError):
        depth = 3.175
    return max(0.0, depth)


def _model3d_should_extrude(
    opts: ImportOptions,
    *,
    is_closed: bool,
    fill,
    face_area: float,
    page_area: float,
) -> bool:
    mode = _normalize_model3d_mode(getattr(opts, "model3d_mode", "off"))
    if mode == "off" or not is_closed:
        return False
    if _model3d_depth_units(opts) <= 0.0:
        return False
    if face_area <= 1e-6:
        return False
    # Skip full-page paper/background fills and border frames.
    if page_area > 1e-6 and face_area / page_area >= 0.80:
        return False
    if mode == "auto":
        if not bool(getattr(opts, "_model3d_intent_feasible", False)):
            return False
        # Auto is deliberately conservative: only extrude filled closed
        # regions when the drawing text supplies third-dimension evidence.
        return fill is not None
    return True


def _make_model3d_obj(edges: List, fc_doc=None):
    if not edges:
        return None
    doc = fc_doc or FreeCAD.ActiveDocument
    try:
        wire = Part.Wire(edges)
        if not wire.isClosed() and wire.Vertexes:
            p0 = wire.Vertexes[0].Point
            pN = wire.Vertexes[-1].Point
            if _len2d(_v(p0.x, p0.y), _v(pN.x, pN.y)) > ZERO_TOL:
                closer = Part.LineSegment(pN, p0).toShape()
                wire = Part.Wire(edges + [closer])
        if not wire.isClosed():
            return None
        face = Part.Face(wire)
        if face.Area <= 1e-6:
            return None
        obj = doc.addObject("Part::Feature", "PDF_3D_Solid")
        obj.Shape = face
        return obj
    except (RuntimeError, ValueError, TypeError, AttributeError):
        return None


def _extrude_model3d_obj(obj, opts: ImportOptions) -> bool:
    if obj is None:
        return False
    try:
        depth = _model3d_depth_units(opts)
        obj.Shape = obj.Shape.extrude(Vector(0, 0, depth))
        return True
    except (RuntimeError, ValueError, TypeError, AttributeError) as e:
        _warn(f"3D extrusion failed: {e}")
        return False


def _apply_style(obj, stroke_rgb, fill_rgb, width, dashes, opts: ImportOptions):
    """Set source stroke/fill color, line width, and dash style on a ViewObject."""
    try:
        vo = obj.ViewObject
        visible_rgb = stroke_rgb or fill_rgb
        if visible_rgb is not None:
            try:
                vo.LineColor = visible_rgb
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
            try:
                vo.PointColor = visible_rgb
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
        if fill_rgb is not None:
            try:
                vo.ShapeColor = fill_rgb
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
        if opts.assign_linewidth and width is not None:
            try:
                # width from get_drawings() is in PDF points (1 pt = 1/72 in).
                # FreeCAD LineWidth is in screen pixels at ~96 dpi.
                # 1 pt ≈ 1.333 px at 96 dpi.  Preserve relative variation;
                # use a 1 px floor so hairlines stay visible without inflating
                # heavier lines that carry structural meaning.
                lw_px = float(width) * (96.0 / 72.0)
                vo.LineWidth = max(1.0, round(lw_px, 1))
            except (TypeError, ValueError, AttributeError):
                vo.LineWidth = 1.0
        else:
            try:
                vo.LineWidth = 1.0
            except (AttributeError, RuntimeError):
                pass
        if opts.map_dashes and dashes and len(dashes) >= 2:
            if all(d > 0 for d in dashes):
                if len(dashes) == 2:
                    # [dash, gap] → simple dashed
                    vo.DrawStyle = "Dashed"
                elif len(dashes) >= 4:
                    # [dash, gap, dot, gap] → center line / dashdot
                    vo.DrawStyle = "Dashdot"
                elif len(dashes) == 3:
                    vo.DrawStyle = "Dashdot"
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass


def _make_group(parent, label: str, fc_doc=None):
    doc = fc_doc or FreeCAD.ActiveDocument
    grp = doc.addObject("App::DocumentObjectGroup", label)
    parent.addObject(grp)
    return grp


_temp_files: List[str] = []

def _register_temp_cleanup(path: str):
    """Track a temp file for later cleanup."""
    _temp_files.append(path)

def cleanup_temp_files():
    """Remove temp raster images from previous imports."""
    removed = 0
    for p in list(_temp_files):
        try:
            if os.path.isfile(p):
                os.remove(p)
                removed += 1
            _temp_files.remove(p)
        except OSError:
            pass
    if removed:
        _msg(f"Cleaned up {removed} temporary raster image(s)")

def _ensure_doc():
    doc = FreeCAD.ActiveDocument
    if doc is None:
        doc = FreeCAD.newDocument("PDF_Import")
    # Note: temp file cleanup is deferred to explicit calls, not run
    # automatically here, to avoid deleting images still referenced by
    # Image::ImagePlane objects from the previous import.
    return doc


# ──────────────────────────────────────────────────────────────────────
# Path item parsing helpers  (handles all PyMuPDF item formats)
# ──────────────────────────────────────────────────────────────────────
def _parse_point(data) -> Tuple[float, float]:
    """Parse a moveto / lineto data payload → (x, y)."""
    if len(data) == 1:
        return _xy(data[0])
    if len(data) >= 2:
        if _is_point(data[0]):
            return _xy(data[0])
        return float(data[0]), float(data[1])
    return 0.0, 0.0


def _parse_cubic(data) -> Tuple[Tuple[float, float], ...]:
    """Parse curveto data → ((x1,y1), (x2,y2), (x3,y3))."""
    if len(data) == 3 and all(_is_point(d) for d in data):
        return _xy(data[0]), _xy(data[1]), _xy(data[2])
    if len(data) >= 6:
        return ((float(data[0]), float(data[1])),
                (float(data[2]), float(data[3])),
                (float(data[4]), float(data[5])))
    # 3 points as tuples
    if len(data) == 3:
        return _xy(data[0]), _xy(data[1]), _xy(data[2])
    raise ValueError(f"Cannot parse cubic data: {data}")


def _parse_quad(data) -> Tuple[Tuple[float, float], Tuple[float, float]]:
    """Parse quadratic Bezier ('v') → ((cx,cy), (x,y))."""
    if len(data) == 2 and all(_is_point(d) for d in data):
        return _xy(data[0]), _xy(data[1])
    if len(data) >= 4:
        return ((float(data[0]), float(data[1])),
                (float(data[2]), float(data[3])))
    if len(data) == 2:
        return _xy(data[0]), _xy(data[1])
    raise ValueError(f"Cannot parse quad data: {data}")


def _parse_rect(data) -> Tuple[float, float, float, float]:
    """Parse 're' data → (x, y, w, h). Handles Rect, (Rect, int), or 4 floats."""
    # PyMuPDF may give (Rect,), (Rect, winding_number), or (x, y, w, h)
    if len(data) >= 1 and _is_rect(data[0]):
        return _rect_coords(data[0])
    if len(data) == 1:
        return _rect_coords(data[0])
    if len(data) >= 4:
        try:
            return float(data[0]), float(data[1]), float(data[2]), float(data[3])
        except (TypeError, ValueError, IndexError):
            pass
    if len(data) >= 2 and _is_point(data[0]) and _is_point(data[1]):
        x0, y0 = _xy(data[0])
        x1, y1 = _xy(data[1])
        return x0, y0, x1 - x0, y1 - y0
    return 0.0, 0.0, 0.0, 0.0


def _polyline_edges_to_arcs(edges: List, opts: ImportOptions) -> List:
    """Detect runs of line segments that form circular arcs and replace
    them with true Part::Arc edges.

    Many PDF generators (Tekla, SDS2, etc.) pre-linearize circles and arcs
    into 16-32 segment polylines. This function reconstructs true arcs.
    """
    if len(edges) < 3:
        return edges

    # Extract vertices from consecutive line edges
    verts = []
    for e in edges:
        try:
            v = e.Vertexes
            if not verts:
                verts.append(v[0].Point)
            verts.append(v[-1].Point)
        except (AttributeError, TypeError, IndexError):
            verts.append(None)  # non-line edge (already an arc, etc.)

    if len(verts) < 4:
        return edges

    # Scan for runs of vertices that fit a circle
    result_edges = []
    i = 0
    n = len(edges)

    while i < n:
        # Try to find the longest arc starting at position i
        best_arc_end = -1
        best_arc = None

        # Need at least 4 edges (5 vertices) for a reliable arc fit.
        # 3-edge runs are often orthogonal corners that can falsely circle-fit.
        for j in range(i + 4, min(i + 65, n + 1)):  # max 64 segments
            run_verts = verts[i:j + 1]
            # Skip if any vertex is None (non-line edge in the run)
            if any(v is None for v in run_verts):
                break
            if len(run_verts) < 5:
                continue
            if not _polyline_run_is_smooth(run_verts):
                continue

            try:
                pts_2d = [_v(v.x, v.y, 0) for v in run_verts]
                center, r, rms = _circle_fit(pts_2d)

                # Accept if fit is good relative to the radius
                tol = max(opts.arc_fit_tol_mm, r * 0.005)  # 0.5% of radius
                if rms < tol and r > 0.1:
                    # Check arc sweep is meaningful and midpoint-consistent.
                    # We only accept the minor sweep between endpoints; if the
                    # sampled midpoint is far from that sweep's centerline,
                    # this is likely a false arc candidate.
                    v0 = pts_2d[0] - center
                    vN = pts_2d[-1] - center
                    vm = pts_2d[len(pts_2d) // 2] - center
                    if v0.Length > ZERO_TOL and vN.Length > ZERO_TOL and vm.Length > ZERO_TOL:
                        a0 = math.atan2(v0.y, v0.x)
                        aN = math.atan2(vN.y, vN.x)
                        am = math.atan2(vm.y, vm.x)

                        sweep = _normalize_angle(aN - a0)
                        if abs(sweep) * 180.0 / math.pi < opts.min_arc_angle_deg:
                            continue

                        test_mid = _normalize_angle(a0 + sweep * 0.5)
                        mid_diff = abs(_normalize_angle(am - test_mid))
                        if mid_diff > (math.pi / 2.0):
                            continue

                        best_arc_end = j
                        mid_idx = len(pts_2d) // 2
                        best_arc = (run_verts[0], run_verts[mid_idx], run_verts[-1])
            except ValueError:
                pass

        if best_arc is not None and best_arc_end > i + 3:
            # Replace edges[i:best_arc_end] with a single arc
            p_start, p_mid, p_end = best_arc
            arc_edge = _edge_arc(p_start, p_mid, p_end)
            if arc_edge is not None:
                result_edges.append(arc_edge)
                i = best_arc_end
                continue

        # No arc found starting here — keep the original edge
        result_edges.append(edges[i])
        i += 1

    return result_edges


# ──────────────────────────────────────────────────────────────────────
# Text layout helpers
# ──────────────────────────────────────────────────────────────────────
def _estimate_text_width_units(text: str) -> float:
    """Rough width estimate in font-size units for Draft text."""
    units = 0.0
    for ch in text or "":
        if ch in "ilI|":
            units += 0.35
        elif ch == "1":
            units += 0.45
        elif ch in " /'-\".":
            units += 0.30
        elif ch in "MW@#%":
            units += 0.95
        else:
            units += 0.65
    return units


def _estimate_text_width_mm(text: str, font_size_fc: float) -> float:
    return _estimate_text_width_units(text) * font_size_fc




# Lowercase characters whose glyphs genuinely descend below the baseline.
# ONLY lowercase forms — uppercase Q, J, etc. do NOT descend in standard
# engineering/technical fonts.  Symbols like / ( ) [ ] | are rendered ON
# the baseline in virtually all fonts used in shop drawings.
_DESCENDER_CHARS = frozenset("gjpqyçÿýĝĵ")


def _effective_descender(text: str, font_descender: float) -> float:
    """Return the descender offset to apply for *text*.

    Draft.make_text anchors at the bottom-left of the bounding box,
    but PDF positions text at the baseline.  The full font descender
    must be applied only when the rendered text actually contains
    glyphs that descend below the baseline (g, j, p, q, y — lowercase
    only).

    For all-caps or non-descending text (common in BOMs, dimension
    labels, and title blocks), we apply only a small fraction of the
    descender to avoid pushing the label below its correct position
    within table cells and annotation boxes.
    """
    if not text:
        return font_descender
    has_descenders = any(ch in _DESCENDER_CHARS for ch in text)
    if has_descenders:
        return font_descender          # full correction
    # All-caps / numeric rows in schedules and title blocks are typically
    # baseline-tight; keep the correction minimal for these runs.
    if text.upper() == text:
        return font_descender * 0.02
    # No descending glyphs — apply only a small fraction of the descender.
    # Draft and PDF font metrics can differ slightly; using too much offset
    # pushes all-caps table text visibly low.
    # Keep a conservative baseline-to-bbox gap for non-descending text.
    #
    # Tuned from 15% -> 8% based on OCR/engineering title-block samples.
    # This keeps descender-bearing words accurate while improving alignment
    # for labels like "TOTAL WEIGHT THIS DRAWING".
    # No descending glyphs — apply ~8% of the descender as a minimal
    # baseline-to-bottom-of-bbox gap (accounts for the tiny space most
    # fonts leave below the baseline even for non-descending glyphs).
    return font_descender * 0.08


def _normalize_pdf_font_name(font_name: str) -> str:
    """Normalize PDF font names to practical system font family names.

    PDF fonts often arrive as subset names like "ABCDEE+Helvetica-Bold".
    Draft accepts family names more reliably than subset/raw PDF names.
    """
    raw = str(font_name or "").strip()
    if not raw:
        return ""

    if "+" in raw:
        prefix, rest = raw.split("+", 1)
        if len(prefix) == 6 and prefix.isupper():
            raw = rest.strip()

    low = raw.lower()
    if "helvetica" in low or "arial" in low:
        family = "Arial"
    elif "times" in low:
        family = "Times New Roman"
    elif "courier" in low:
        family = "Courier New"
    elif "calibri" in low:
        family = "Calibri"
    else:
        return raw

    is_bold = bool(re.search(r"\bbold\b|\bbd\b", low))
    is_italic = bool(re.search(r"\bitalic\b|\boblique\b|\bit\b", low))
    if is_bold and is_italic:
        return f"{family} Bold Italic"
    if is_bold:
        return f"{family} Bold"
    if is_italic:
        return f"{family} Italic"
    return family


def _line_angle_deg(line: dict, opts: Optional[ImportOptions] = None) -> float:
    text_dir = line.get("dir", (1.0, 0.0))
    if text_dir and len(text_dir) >= 2:
        try:
            dx, dy = _transform_pdf_direction(
                (float(text_dir[0]), float(text_dir[1])), opts
            )
            return math.degrees(math.atan2(dy, dx))
        except (TypeError, ValueError):
            pass
    return 0.0


def _normalize_text_angle_deg(angle_deg: float) -> float:
    """Normalize to [-90, 90] for orientation tests."""
    a = float(angle_deg) % 180.0
    if a > 90.0:
        a -= 180.0
    return a


def _rotated_text_threshold_deg(default: float = 12.0) -> float:
    """Shared threshold for routing rotated/diagonal labels."""
    raw = os.environ.get("BC_PDF_ROTATED_LABEL_DEG", "").strip()
    if raw:
        try:
            val = float(raw)
            if 0.0 <= val <= 89.0:
                return val
        except (TypeError, ValueError):
            pass
    return float(default)


def _apply_text_local_y_offset(pos: "Vector", angle_deg: float, offset_fc: float) -> "Vector":
    """Apply baseline->bbox offset in the text's local +Y axis (rotation aware)."""
    if abs(float(offset_fc)) <= 1e-12:
        return pos
    a = math.radians(float(angle_deg))
    dx = -math.sin(a) * float(offset_fc)
    dy = math.cos(a) * float(offset_fc)
    try:
        pos.x += dx
        pos.y += dy
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return pos


def _span_origin_pdf(span: dict) -> Optional[Tuple[float, float]]:
    """Return a best-effort PDF-space baseline origin for one span."""
    o = span.get("origin")
    if o and len(o) >= 2:
        try:
            return float(o[0]), float(o[1])
        except (TypeError, ValueError):
            pass

    bbox = span.get("bbox")
    if bbox and len(bbox) >= 4:
        try:
            x0, _y0, _x1, y1 = [float(v) for v in bbox[:4]]
            size_pt = max(0.0, float(span.get("size", 0.0) or 0.0))
            desc = abs(float(span.get("descender", -0.2) or -0.2))
            return x0, (y1 - desc * size_pt)
        except (TypeError, ValueError):
            return None
    return None


def _span_bbox_pdf(span: dict) -> Optional[Tuple[float, float, float, float]]:
    """Return a normalized PDF-space bbox for one span, if available."""
    from pdfcadcore.text_scale import span_bbox_pdf as _core_span_bbox_pdf

    return _core_span_bbox_pdf(span)


def _fit_font_size_to_span_bbox(
    text: str,
    font_size_fc: float,
    span: dict,
    scale: float,
    angle_deg: float = 0.0,
) -> float:
    """Return nominal host text size; bboxes are placement hints only."""
    from pdfcadcore.text_scale import fit_font_size_to_span_bbox

    return fit_font_size_to_span_bbox(
        text,
        font_size_fc,
        span,
        scale,
        angle_deg,
        estimate_width_units=_estimate_text_width_units,
    )


def _span_source_color(span: dict) -> Optional[Tuple[float, float, float]]:
    return _optional_color(span.get("color"))


def _apply_text_color(obj, rgb: Optional[Tuple[float, float, float]]) -> None:
    """Apply a source text color to Draft text or ShapeString view providers."""
    if rgb is None:
        return
    try:
        vo = obj.ViewObject
    except (AttributeError, RuntimeError):
        return
    for prop in ("TextColor", "ShapeColor", "LineColor", "PointColor"):
        try:
            setattr(vo, prop, rgb)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass


def _render_text_spans_exact_labels(
    tdict: dict,
    text_group,
    page_h: float,
    opts: ImportOptions,
    scale: float,
    only_rotated: bool = False,
    layout_context: Optional[dict] = None,
) -> int:
    """Render and verify exactly one Draft label per eligible PDF span."""
    del layout_context
    if Draft is None or text_group is None:
        attempt = {
            "source_item_id": "page",
            "requested_type": "labels",
            "attempted_type": "labels",
            "final_type": None,
            "outcome": "failed",
            "reason": "freecad_draft_or_group_unavailable",
            "created_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
        }
        opts.text_delivery_attempts.append(attempt)
        raise TextRepresentationFailure(
            "Labels unavailable: FreeCAD Draft/group missing", attempt
        )

    delivered: List[Tuple[dict, str, Any, int]] = []
    owned: List[Any] = []
    attempts_for_call: List[Dict[str, Any]] = []
    page_num = int(getattr(opts, "_provenance_page", 1) or 1)
    rotated_threshold = _rotated_text_threshold_deg()

    def fail_attempt(attempt: Dict[str, Any], reason: str, evidence: Dict[str, Any]):
        doc = _text_host_document(owned[0] if owned else None, text_group)
        removed, cleanup_complete = (
            _remove_owned_text_objects(doc, text_group, owned)
            if doc is not None
            else ([], not owned)
        )
        removed_set = set(removed)
        for earlier in attempts_for_call:
            if earlier.get("outcome") == "verified":
                earlier["outcome"] = "rolled_back"
                earlier["final_type"] = None
                earlier["superseded_by"] = attempt.get("source_item_id")
                earlier["removed_entity_ids"] = [
                    entity_id
                    for entity_id in list(earlier.get("created_entity_ids") or [])
                    if entity_id in removed_set
                ]
                earlier["cleanup_complete"] = all(
                    entity_id in removed_set
                    for entity_id in list(earlier.get("created_entity_ids") or [])
                )
        attempt.update({
            "outcome": "failed",
            "reason": reason,
            "evidence": dict(evidence),
            "removed_entity_ids": removed,
            "cleanup_complete": bool(cleanup_complete),
            "final_type": None,
        })
        attempts_for_call.append(attempt)
        opts.text_delivery_attempts.extend(attempts_for_call)
        opts.text_delivered_counts.pop("native_label", None)
        raise TextRepresentationFailure(
            "Labels failed for %s: %s" % (attempt["source_item_id"], reason),
            attempt,
        )

    for block_index, block in enumerate(tdict.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            spans = line.get("spans", []) or []
            if not spans:
                continue
            angle_deg = _line_angle_deg(line, opts)
            norm_angle = _normalize_text_angle_deg(angle_deg)
            if only_rotated and abs(norm_angle) < rotated_threshold:
                continue

            for span_index, span in enumerate(spans):
                source_text = str(span.get("text", "") or "")
                if not source_text or source_text.isspace():
                    continue
                from pdfcadcore.text_scale import effective_span_font_size_pt

                source_item_id = "p%d:b%d:l%d:s%d" % (
                    page_num, block_index, line_index, span_index
                )
                attempt = {
                    "source_item_id": source_item_id,
                    "requested_type": "labels",
                    "attempted_type": "labels",
                    "created_entity_ids": [],
                    "removed_entity_ids": [],
                    "cleanup_complete": False,
                    "superseded_by": None,
                }
                # Source direction is authoritative. Content heuristics (for
                # example, recognizing a BOM quantity) must never rotate or
                # reposition a requested text representation.
                span_angle_deg = angle_deg
                size_pt = effective_span_font_size_pt(span, span_angle_deg)
                if size_pt <= 0.0:
                    size_pt = 3.0
                font_size_fc = max(0.1, size_pt * scale)
                font_size_fc = _fit_font_size_to_span_bbox(
                    source_text, font_size_fc, span, scale, span_angle_deg
                )
                origin = _span_origin_pdf(span)
                if not origin:
                    fail_attempt(
                        attempt,
                        "source_origin_unavailable",
                        {"source_font": str(span.get("font", "") or "")},
                    )

                pos = _to_fc(origin, page_h, opts, scale)
                font_name = _normalize_pdf_font_name(span.get("font", ""))
                # Draft text is placed from a host text-box anchor while PDF
                # spans report a baseline origin. Apply the same local-axis
                # correction for horizontal and rotated exact labels so leader
                # callouts do not drift when switching modes.
                try:
                    desc = float(span.get("descender", -0.2) or -0.2)
                except (TypeError, ValueError):
                    desc = -0.2
                offset_fc = _effective_descender(source_text, desc) * font_size_fc * 0.35
                if abs(offset_fc) > 1e-12:
                    pos = _apply_text_local_y_offset(pos, span_angle_deg, offset_fc)
                rot = Rotation(Vector(0, 0, 1), span_angle_deg)
                doc = _text_host_document(None, text_group)
                before_objects = (
                    {id(obj) for obj in _document_objects(doc)}
                    if doc is not None
                    else set()
                )
                try:
                    t = Draft.make_text(
                        [source_text], placement=Placement(pos, rot)
                    )
                except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
                    if doc is not None:
                        owned.extend(
                            obj for obj in _document_objects(doc)
                            if id(obj) not in before_objects and obj not in owned
                        )
                    fail_attempt(
                        attempt,
                        "label creation failed",
                        {
                            "source_font": str(span.get("font", "") or ""),
                            "exception": "%s: %s" % (exc.__class__.__name__, exc),
                            "item_specific_attempted": True,
                        },
                    )
                if t is None:
                    fail_attempt(
                        attempt,
                        "label creation returned no host object",
                        {"item_specific_attempted": True},
                    )
                owned.append(t)

                try:
                    t.ViewObject.FontSize = font_size_fc
                    if font_name:
                        t.ViewObject.FontName = font_name
                    t.ViewObject.Justification = "Left"
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass
                _apply_text_color(t, _span_source_color(span))

                try:
                    text_group.addObject(t)
                    add_property = getattr(t, "addProperty", None)
                    if callable(add_property):
                        add_property("App::PropertyString", "PDFSourceItemId", "PDF Import")
                        add_property("App::PropertyString", "PDFRepresentation", "PDF Import")
                        t.PDFSourceItemId = source_item_id
                        t.PDFRepresentation = "labels"
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    fail_attempt(
                        attempt,
                        "label host annotation failed",
                        {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
                    )

                actual_text = getattr(t, "Text", None)
                if isinstance(actual_text, (list, tuple)):
                    source_text_preserved = list(actual_text) == [source_text]
                else:
                    source_text_preserved = str(actual_text or "") == source_text
                entity_id = _host_object_id(t)
                if not entity_id or not source_text_preserved:
                    fail_attempt(
                        attempt,
                        "label host verification failed",
                        {
                            "host_entity_id": entity_id,
                            "source_text_preserved": source_text_preserved,
                            "host_entity_type": str(getattr(t, "TypeId", "") or ""),
                        },
                    )

                attempt.update({
                    "outcome": "verified",
                    "reason": "requested Labels delivered",
                    "final_type": "labels",
                    "created_entity_ids": [entity_id],
                    "cleanup_complete": True,
                    "evidence": {
                        "host_entity_type": str(getattr(t, "TypeId", "") or ""),
                        "source_text_preserved": True,
                        "source_font": str(span.get("font", "") or ""),
                        "font_name": font_name,
                        "rotation_deg": float(span_angle_deg),
                        "font_size": float(font_size_fc),
                    },
                })
                attempts_for_call.append(attempt)
                stable_index = block_index * 1_000_000 + line_index * 10_000 + span_index
                delivered.append((span, source_item_id, t, stable_index))

    opts.text_delivery_attempts.extend(attempts_for_call)
    _record_text_delivery(opts, "native_label", len(delivered))
    try:
        from pdfcadcore.source_provenance import record_text_span_provenance

        for span, _source_item_id, label_obj, stable_index in delivered:
            record_text_span_provenance(
                opts,
                page=page_num,
                span=span,
                text=str(span.get("text", "") or ""),
                created_entity_type="native_label",
                parent_handle=_host_object_id(label_obj),
                import_mode=str(getattr(opts, "import_mode", "") or ""),
                text_mode="labels",
                span_index=stable_index,
            )
    except (ImportError, TypeError, ValueError):
        pass
    return len(delivered)


def _shapestring_font_cache_dir() -> Path:
    try:
        base = Path(FreeCAD.getUserAppDataDir())
        return base / "Mod" / "PDFVectorImporter" / "font_cache"
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return Path(tempfile.gettempdir()) / "bc_fc_pdf_font_cache"


def _stage_page_shapestring_fonts(
    pdf_doc,
    page,
    opts: ImportOptions,
    *,
    pdf_sha256: Optional[str] = None,
    page_number: Optional[int] = None,
) -> Dict[str, Dict[str, Any]]:
    """Stage fonts and optionally record one completed proof-bound page session."""
    from PDFEmbeddedFonts import EmbeddedFontInventoryError, stage_page_fonts

    exact_context_requested = pdf_sha256 is not None or page_number is not None
    context_digest = ""
    context_page = 0
    if exact_context_requested:
        if not isinstance(pdf_sha256, str):
            raise ValueError("proof-capable font staging requires a PDF SHA-256")
        context_digest = pdf_sha256.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", context_digest) is None:
            raise ValueError("proof-capable font staging requires a valid PDF SHA-256")
        if type(page_number) is not int or page_number <= 0:
            raise ValueError("proof-capable font staging requires a positive page number")
        context_page = page_number
        existing_sessions = getattr(
            opts, "_shapestring_font_staging_sessions", []
        )
        if existing_sessions is None:
            existing_sessions = []
        if not isinstance(existing_sessions, list):
            raise ValueError("font staging sessions must be a list")
        # A retry owns this digest/page slot immediately.  If staging raises,
        # no stale completed session for the same page may survive and certify
        # the failed retry as an exact-font absence.
        opts._shapestring_font_staging_sessions = [
            existing
            for existing in existing_sessions
            if not (
                isinstance(existing, dict)
                and existing.get("pdf_sha256") == context_digest
                and existing.get("page_number") == context_page
            )
        ]

    failures = list(getattr(opts, "_font_stage_failures", []) or [])
    prior_failure_count = len(failures)
    try:
        staged = stage_page_fonts(
            pdf_doc,
            page,
            _shapestring_font_cache_dir(),
            failures=failures,
        )
    except Exception as exc:
        if exact_context_requested:
            reason = (
                "embedded_font_inventory_failed"
                if isinstance(exc, EmbeddedFontInventoryError)
                else "embedded_font_page_staging_failed"
            )
            page_failure = {
                "reason": reason,
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
                "pdf_sha256": context_digest,
                "page_number": context_page,
            }
            sessions = list(
                getattr(opts, "_shapestring_font_staging_sessions", []) or []
            )
            sessions.append(
                {
                    "pdf_sha256": context_digest,
                    "page_number": context_page,
                    "staging_complete": False,
                    "records": {},
                    "failures": copy.deepcopy(failures[prior_failure_count:]),
                    "page_failure": page_failure,
                }
            )
            opts._shapestring_font_staging_sessions = sessions
        raise
    telemetry_page = int(getattr(opts, "_provenance_page", 0) or 0)
    if telemetry_page > 0:
        for failure in failures[prior_failure_count:]:
            failure.setdefault("page", telemetry_page)
    opts._font_stage_failures = failures
    merged = dict(getattr(opts, "_shapestring_font_paths", {}) or {})
    merged.update(staged)
    opts._shapestring_font_paths = merged
    if exact_context_requested:
        session = {
            "pdf_sha256": context_digest,
            "page_number": context_page,
            "staging_complete": True,
            "records": copy.deepcopy(staged),
            "failures": copy.deepcopy(failures[prior_failure_count:]),
        }
        sessions = list(opts._shapestring_font_staging_sessions)
        sessions = [
            existing
            for existing in sessions
            if not (
                isinstance(existing, dict)
                and existing.get("pdf_sha256") == context_digest
                and existing.get("page_number") == context_page
            )
        ]
        sessions.append(session)
        opts._shapestring_font_staging_sessions = sessions
    return staged


def _canonical_font_identity(font_name: str) -> Dict[str, str]:
    """Bind a stripped PDF font name to the shared exact normalization key."""
    from PDFEmbeddedFonts import normalize_font_key

    raw_name = str(font_name or "").strip()
    return {
        "raw_name": raw_name,
        "normalized_key": normalize_font_key(raw_name),
    }


def _font_source_result(
    source: str,
    outcome: str,
    font_identity: Dict[str, str],
    **details,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "source": source,
        "outcome": outcome,
        "font_identity": dict(font_identity),
    }
    result.update(details)
    return result


def _resolve_shapestring_font_path_with_evidence(
    font_name: str,
    opts: Optional[ImportOptions] = None,
    *,
    pdf_sha256: Optional[str] = None,
    page_number: Optional[int] = None,
    _allow_unbound_compat: bool = False,
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Resolve one exact font and retain auditable embedded/system results.

    Only a clean miss is reported as ``not_found``.  Corrupt metadata, stale
    staged files, hash/IO failures, and lookup exceptions are ``invalid`` so
    callers cannot mistake a runtime failure for proof that the font is absent.
    """
    context_digest = ""
    context_page = 0
    staging_complete = False
    if not _allow_unbound_compat:
        if (
            not isinstance(pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
            or type(page_number) is not int
            or page_number <= 0
        ):
            raw_name = str(font_name or "").strip()
            identity = {"raw_name": raw_name, "normalized_key": ""}
            return None, [_font_source_result(
                "embedded_font",
                "invalid",
                identity,
                pdf_sha256=pdf_sha256,
                page_number=page_number,
                staging_complete=False,
                reason="font_proof_context_invalid",
            )]
        context_digest = pdf_sha256
        context_page = page_number

    def source_result(
        source: str,
        outcome: str,
        identity: Dict[str, str],
        **details,
    ) -> Dict[str, Any]:
        if not _allow_unbound_compat:
            details.update({
                "pdf_sha256": context_digest,
                "page_number": context_page,
                "staging_complete": staging_complete,
            })
        return _font_source_result(source, outcome, identity, **details)

    try:
        font_identity = _canonical_font_identity(font_name)
    except Exception as exc:
        raw_name = str(font_name or "").strip()
        identity = {"raw_name": raw_name, "normalized_key": ""}
        return None, [source_result(
            "embedded_font",
            "invalid",
            identity,
            reason="font_identity_normalization_failed",
            exception="%s: %s" % (exc.__class__.__name__, exc),
        )]

    key = font_identity["normalized_key"]
    if not font_identity["raw_name"] or not key:
        return None, [source_result(
            "embedded_font",
            "invalid",
            font_identity,
            reason="malformed_font_identity",
        )]

    if _allow_unbound_compat:
        embedded = (
            getattr(opts, "_shapestring_font_paths", {}) if opts is not None else {}
        )
        failures = getattr(opts, "_font_stage_failures", []) if opts is not None else []
    else:
        try:
            sessions = (
                getattr(opts, "_shapestring_font_staging_sessions", [])
                if opts is not None
                else []
            )
        except Exception as exc:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="font_staging_session_lookup_failed",
                exception="%s: %s" % (exc.__class__.__name__, exc),
            )]
        if not isinstance(sessions, list):
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="malformed_font_staging_sessions",
            )]
        exact_sessions: List[Dict[str, Any]] = []
        try:
            for session in sessions:
                if not isinstance(session, dict):
                    return None, [source_result(
                        "embedded_font",
                        "invalid",
                        font_identity,
                        reason="malformed_font_staging_session",
                    )]
                session_digest = session.get("pdf_sha256")
                session_page = session.get("page_number")
                if (
                    not isinstance(session_digest, str)
                    or re.fullmatch(r"[0-9a-f]{64}", session_digest) is None
                    or type(session_page) is not int
                    or session_page <= 0
                ):
                    return None, [source_result(
                        "embedded_font",
                        "invalid",
                        font_identity,
                        reason="malformed_font_staging_session",
                    )]
                if (
                    session_digest == context_digest
                    and session_page == context_page
                ):
                    exact_sessions.append(session)
        except Exception as exc:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="font_staging_session_lookup_failed",
                exception="%s: %s" % (exc.__class__.__name__, exc),
            )]
        if not exact_sessions:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason=(
                    "font_staging_session_missing"
                    if not sessions
                    else "font_staging_session_mismatch"
                ),
            )]
        if len(exact_sessions) != 1:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="duplicate_font_staging_session",
            )]
        session = exact_sessions[0]
        try:
            page_failure = session.get("page_failure")
            if session.get("staging_complete") is not True:
                if page_failure is not None:
                    if (
                        not isinstance(page_failure, dict)
                        or page_failure.get("reason")
                        not in {
                            "embedded_font_inventory_failed",
                            "embedded_font_page_staging_failed",
                        }
                        or page_failure.get("pdf_sha256") != context_digest
                        or page_failure.get("page_number") != context_page
                        or not isinstance(page_failure.get("exception"), str)
                        or not page_failure.get("exception")
                    ):
                        return None, [source_result(
                            "embedded_font",
                            "invalid",
                            font_identity,
                            reason="malformed_font_staging_page_failure",
                        )]
                    return None, [source_result(
                        "embedded_font",
                        "invalid",
                        font_identity,
                        reason=page_failure["reason"],
                        exception=page_failure["exception"],
                        page_failure=copy.deepcopy(page_failure),
                    )]
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason="font_staging_session_incomplete",
                )]
            if page_failure not in (None, {}):
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason="completed_font_session_has_page_failure",
                )]
            embedded = session.get("records")
            failures = session.get("failures")
        except Exception as exc:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="font_staging_session_lookup_failed",
                exception="%s: %s" % (exc.__class__.__name__, exc),
            )]
        staging_complete = True

    try:
        if embedded is None:
            if _allow_unbound_compat:
                embedded = {}
            else:
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason="malformed_staged_font_lookup",
                )]
        if not isinstance(embedded, dict):
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="malformed_staged_font_lookup",
            )]
        record_present = key in embedded
        record = embedded[key] if record_present else None
    except Exception as exc:
        return None, [source_result(
            "embedded_font",
            "invalid",
            font_identity,
            reason="staged_font_lookup_failed",
            exception="%s: %s" % (exc.__class__.__name__, exc),
        )]

    results: List[Dict[str, Any]] = []
    if record_present:
        if not isinstance(record, dict):
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="malformed_staged_font_record",
            )]

        try:
            path_value = record.get("path")
            sha_value = record.get("sha256")
            source_value = record.get("source")
            xref_value = record.get("xref")
        except Exception as exc:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="staged_font_record_lookup_failed",
                exception="%s: %s" % (exc.__class__.__name__, exc),
            )]
        if (
            not isinstance(path_value, str)
            or not path_value
            or path_value != path_value.strip()
            or not isinstance(sha_value, str)
            or re.fullmatch(r"[0-9a-f]{64}", sha_value) is None
            or source_value != "pdf_embedded"
            or type(xref_value) is not int
            or xref_value <= 0
        ):
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="malformed_staged_font_record",
            )]

        staged_path = Path(path_value)
        try:
            if not staged_path.is_file():
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason="staged_font_file_missing",
                    path=path_value,
                    sha256=sha_value,
                )]
            actual_sha = hashlib.sha256(staged_path.read_bytes()).hexdigest()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="staged_font_file_unreadable",
                path=path_value,
                sha256=sha_value,
                exception="%s: %s" % (exc.__class__.__name__, exc),
            )]
        if actual_sha != sha_value:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="staged_font_sha256_mismatch",
                path=path_value,
                sha256=sha_value,
                actual_sha256=actual_sha,
            )]
        return path_value, [source_result(
            "embedded_font",
            "found",
            font_identity,
            path=path_value,
            sha256=sha_value,
            xref=xref_value,
        )]

    exact_nonembedded_observation: Optional[Dict[str, Any]] = None
    try:
        if failures is None:
            if _allow_unbound_compat:
                failures = []
            else:
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason="malformed_font_staging_failures",
                )]
        if not isinstance(failures, list):
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="malformed_font_staging_failures",
            )]
        for failure in failures:
            if not isinstance(failure, dict):
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason="malformed_font_staging_failure",
                )]
            failed_name_value = failure.get("font")
            if not isinstance(failed_name_value, str):
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason="malformed_font_staging_failure",
                )]
            failed_name = failed_name_value.strip()
            if not failed_name:
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason=str(
                        failure.get("reason")
                        or "malformed_font_staging_failure"
                    ),
                    exception=str(failure.get("exception") or ""),
                )]
            failed_key = _canonical_font_identity(failed_name)["normalized_key"]
            if failure.get("outcome") == "not_embedded":
                if (
                    failure.get("reason") != "embedded_font_not_present"
                    or type(failure.get("xref")) is not int
                    or failure.get("xref") != 0
                    or str(failure.get("exception") or "")
                ):
                    return None, [source_result(
                        "embedded_font",
                        "invalid",
                        font_identity,
                        reason="malformed_font_inventory_observation",
                    )]
                if failed_key == key and exact_nonembedded_observation is None:
                    exact_nonembedded_observation = copy.deepcopy(failure)
                continue
            if failed_key == key:
                return None, [source_result(
                    "embedded_font",
                    "invalid",
                    font_identity,
                    reason=str(failure.get("reason") or "embedded_font_staging_failed"),
                    exception=str(failure.get("exception") or ""),
                )]
    except Exception as exc:
        return None, [source_result(
            "embedded_font",
            "invalid",
            font_identity,
            reason="font_staging_failure_lookup_failed",
            exception="%s: %s" % (exc.__class__.__name__, exc),
        )]

    if exact_nonembedded_observation is None and not _allow_unbound_compat:
        return None, [source_result(
            "embedded_font",
            "invalid",
            font_identity,
            reason="font_not_observed_in_completed_inventory",
        )]

    embedded_absence_details: Dict[str, Any] = {}
    if exact_nonembedded_observation is not None:
        embedded_absence_details["inventory_observation"] = (
            exact_nonembedded_observation
        )
    results.append(source_result(
        "embedded_font",
        "not_found",
        font_identity,
        **embedded_absence_details,
    ))

    # Exact Windows family/style aliases only.  Helvetica is intentionally not
    # mapped to Arial: visual similarity is not source-font identity.
    system_files = {
        "arial": "arial.ttf",
        "arialmt": "arial.ttf",
        "arialbold": "arialbd.ttf",
        "arialboldmt": "arialbd.ttf",
        "arialitalic": "ariali.ttf",
        "arialitalicmt": "ariali.ttf",
        "arialbolditalic": "arialbi.ttf",
        "arialbolditalicmt": "arialbi.ttf",
        "calibri": "calibri.ttf",
        "calibriregular": "calibri.ttf",
        "calibribold": "calibrib.ttf",
        "calibriitalic": "calibrii.ttf",
        "calibribolditalic": "calibriz.ttf",
        "timesnewroman": "times.ttf",
        "timesnewromanpsmt": "times.ttf",
        "timesnewromanbold": "timesbd.ttf",
        "timesnewromanpsboldmt": "timesbd.ttf",
        "timesnewromanitalic": "timesi.ttf",
        "timesnewromanpsitalicmt": "timesi.ttf",
        "couriernew": "cour.ttf",
        "couriernewpsmt": "cour.ttf",
        "couriernewbold": "courbd.ttf",
        "couriernewpsboldmt": "courbd.ttf",
    }
    filename = system_files.get(key)
    if not filename:
        results.append(source_result(
            "system_font", "not_found", font_identity
        ))
        return None, results

    system_path = os.path.join(
        os.environ.get("WINDIR", r"C:\Windows"), "Fonts", filename
    )
    try:
        if not Path(system_path).is_file():
            results.append(source_result(
                "system_font", "not_found", font_identity
            ))
            return None, results
        with open(system_path, "rb") as font_file:
            font_file.read(1)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        results.append(source_result(
            "system_font",
            "invalid",
            font_identity,
            reason="system_font_file_unreadable",
            path=system_path,
            exception="%s: %s" % (exc.__class__.__name__, exc),
        ))
        return None, results

    results.append(source_result(
        "system_font", "found", font_identity, path=system_path
    ))
    return system_path, results


def _resolve_shapestring_font_path(
    font_name: str, opts: Optional[ImportOptions] = None
) -> Optional[str]:
    """Compatibility wrapper returning only the old optional exact-font path."""
    try:
        path, _source_results = _resolve_shapestring_font_path_with_evidence(
            font_name, opts, _allow_unbound_compat=True
        )
        return path
    except Exception:
        return None


def _record_shapestring_skip(opts: ImportOptions, reason: str) -> None:
    skips = getattr(opts, "shapestring_skips", None)
    if skips is None:
        return
    skips[reason] = int(skips.get(reason, 0)) + 1


def _record_text_delivery(opts: ImportOptions, bucket: str, count: int) -> None:
    """Accumulate DELIVERED text-entity counts by bucket (TEXTMODE-1)."""
    if int(count or 0) <= 0:
        return
    delivered = getattr(opts, "text_delivered_counts", None)
    if delivered is None:
        return
    delivered[bucket] = int(delivered.get(bucket, 0)) + int(count)


FREECAD_TEXT_IMPORTER_IDENTITY = "bluecollarsystems.freecad.pdf_vector_importer"


TEXT_ITEM_FALLBACK_LADDERS = {
    "text": ("text", "labels", "3d_text", "glyphs", "geometry", "raster"),
    "labels": ("labels", "text", "3d_text", "glyphs", "geometry", "raster"),
    "3d_text": ("3d_text", "glyphs", "geometry", "text", "labels", "raster"),
    "glyphs": ("glyphs", "geometry", "3d_text", "text", "labels", "raster"),
    "geometry": ("geometry", "glyphs", "3d_text", "text", "labels", "raster"),
    "raster": ("raster",),
}

CLOSED_SVG_ITEM_IMPOSSIBILITY_REASONS = frozenset(
    {
        "svg_renderer_unavailable",
        "svg_payload_too_large",
        "svg_has_no_glyph_placements",
        "svg_glyph_outlines_unavailable",
        "svg_item_glyph_bounds_unavailable",
    }
)


def _normalize_requested_text_type(requested_type: str) -> str:
    if not isinstance(requested_type, str):
        raise ValueError("requested text representation must be a string")
    normalized = re.sub(r"[\s-]+", "_", requested_type.strip().lower())
    normalized = {
        "label": "labels",
        "native_text": "text",
        "draft_text": "text",
        "text3d": "3d_text",
        "3dtext": "3d_text",
        "3d": "3d_text",
        "outline": "glyphs",
        "outlines": "glyphs",
    }.get(normalized, normalized)
    if normalized not in TEXT_ITEM_FALLBACK_LADDERS:
        raise ValueError("unsupported requested text representation")
    return normalized


def _plain_text_source_value(value: Any, field_name: str) -> Any:
    """Copy PyMuPDF text data into host-independent Python containers."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("%s contains a non-finite number" % field_name)
        return value
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if not isinstance(key, (str, int, float, bool)):
                raise ValueError("%s contains a non-plain dictionary key" % field_name)
            result[key] = _plain_text_source_value(
                child, "%s.%s" % (field_name, key)
            )
        return result
    if isinstance(value, tuple):
        return tuple(
            _plain_text_source_value(child, field_name) for child in value
        )
    if isinstance(value, list):
        return [
            _plain_text_source_value(child, field_name) for child in value
        ]
    try:
        if all(hasattr(value, attr) for attr in ("x0", "y0", "x1", "y1")):
            return tuple(float(getattr(value, attr)) for attr in ("x0", "y0", "x1", "y1"))
        if all(hasattr(value, attr) for attr in ("x", "y")):
            return (float(value.x), float(value.y))
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("%s cannot be copied safely" % field_name) from exc
    raise ValueError("%s contains a host-specific value" % field_name)


def _finite_source_tuple(value: Any, length: int, field_name: str) -> Tuple[float, ...]:
    plain = _plain_text_source_value(value, field_name)
    if not isinstance(plain, (tuple, list)) or len(plain) != length:
        raise ValueError("%s must contain %d numeric values" % (field_name, length))
    numbers: List[float] = []
    for component in plain:
        if isinstance(component, bool):
            raise ValueError("%s contains a non-numeric value" % field_name)
        try:
            number = float(component)
        except (TypeError, ValueError) as exc:
            raise ValueError("%s contains a non-numeric value" % field_name) from exc
        if not math.isfinite(number):
            raise ValueError("%s contains a non-finite number" % field_name)
        numbers.append(number)
    return tuple(numbers)


def _iter_text_source_items(
    tdict: dict,
    page_num: int,
    pdf_sha256: str,
    requested_type: str,
):
    """Yield canonical, stable, host-independent source items for one page."""
    if not isinstance(tdict, dict):
        raise ValueError("text dictionary must be a dictionary")
    if type(page_num) is not int or page_num <= 0:
        raise ValueError("page number must be a positive integer")
    if not isinstance(pdf_sha256, str):
        raise ValueError("PDF SHA-256 must be a string")
    digest = pdf_sha256.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("PDF SHA-256 must be exactly 64 hexadecimal characters")
    requested = _normalize_requested_text_type(requested_type)

    blocks = tdict.get("blocks", [])
    if not isinstance(blocks, list):
        raise ValueError("text dictionary blocks must be a list")
    for block_index, block in enumerate(blocks):
        if not isinstance(block, dict):
            raise ValueError("text block must be a dictionary")
        if block.get("type") != 0:
            continue
        lines = block.get("lines", [])
        if not isinstance(lines, list):
            raise ValueError("text block lines must be a list")
        for line_index, line in enumerate(lines):
            if not isinstance(line, dict):
                raise ValueError("text line must be a dictionary")
            spans = line.get("spans", [])
            if not isinstance(spans, list):
                raise ValueError("text line spans must be a list")
            for span_index, source_span in enumerate(spans):
                if not isinstance(source_span, dict):
                    raise ValueError("text span must be a dictionary")
                source_text = source_span.get("text", "")
                if not isinstance(source_text, str):
                    raise ValueError("text span content must be a string")
                if not source_text or source_text.isspace():
                    continue

                source_font = source_span.get("font", "")
                if not isinstance(source_font, str):
                    raise ValueError("text span font must be a string")
                font_identity = _canonical_font_identity(source_font)
                bbox = _finite_source_tuple(
                    source_span.get("bbox"), 4, "span.bbox"
                )
                origin = _finite_source_tuple(
                    source_span.get("origin"), 2, "span.origin"
                )
                line_direction = _finite_source_tuple(
                    line.get("dir"), 2, "line.dir"
                )
                if math.hypot(line_direction[0], line_direction[1]) <= 1e-12:
                    raise ValueError("line direction must be non-zero")
                rotation_deg = _line_angle_deg({"dir": line_direction})
                if not math.isfinite(rotation_deg):
                    raise ValueError("line direction angle must be finite")
                span = _plain_text_source_value(source_span, "span")
                if type(span) is not dict:
                    raise ValueError("canonical span must be a plain dictionary")

                yield {
                    "importer_identity": FREECAD_TEXT_IMPORTER_IDENTITY,
                    "pdf_sha256": digest,
                    "page_number": page_num,
                    "source_item_id": "p%d:b%d:l%d:s%d" % (
                        page_num, block_index, line_index, span_index
                    ),
                    "requested_type": requested,
                    "text": source_text,
                    "font_identity": dict(font_identity),
                    "bbox": bbox,
                    "origin": origin,
                    "line_direction": line_direction,
                    "rotation_deg": float(rotation_deg),
                    "span": span,
                    "block_index": block_index,
                    "line_index": line_index,
                    "span_index": span_index,
                }


class TextItemImpossible(RuntimeError):
    """One exact source item cannot use one exact representation."""

    def __init__(
        self,
        message: str,
        *,
        attempt: Dict[str, Any],
        proof: Dict[str, Any],
    ):
        super().__init__(message)
        self.attempt = dict(attempt or {})
        self.proof = dict(proof or {})


def _validated_entity_ids(value, *, field_name: str, allow_empty: bool) -> List[str]:
    if not isinstance(value, list):
        raise ValueError("%s must be a list" % field_name)
    if not allow_empty and not value:
        raise ValueError("%s must not be empty" % field_name)
    if any(
        not isinstance(entity_id, str)
        or not entity_id
        or entity_id != entity_id.strip()
        for entity_id in value
    ):
        raise ValueError("%s must contain exact nonempty string ids" % field_name)
    if len(value) != len(set(value)):
        raise ValueError("%s must contain unique ids" % field_name)
    return list(value)


def _validate_item_impossibility_proof(
    item: Dict[str, Any],
    requested: str,
    attempted: str,
    proof: Dict[str, Any],
) -> Dict[str, Any]:
    """Return a copy only when an impossibility proof is exact and complete."""
    if not isinstance(item, dict) or not isinstance(proof, dict):
        raise ValueError("item and impossibility proof must be dictionaries")
    if proof.get("item_specific_proven_impossible") is not True:
        raise ValueError("proof is not item-specifically proven impossible")
    if (
        item.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY
        or proof.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY
    ):
        raise ValueError("proof importer identity does not exactly match FreeCAD")

    pdf_sha256 = proof.get("pdf_sha256")
    if (
        not isinstance(pdf_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
        or pdf_sha256 != item.get("pdf_sha256")
    ):
        raise ValueError("proof PDF SHA-256 does not exactly match the source item")

    page_number = proof.get("page_number")
    if (
        type(page_number) is not int
        or page_number <= 0
        or type(item.get("page_number")) is not int
        or page_number != item.get("page_number")
    ):
        raise ValueError("proof page number does not exactly match the source item")

    source_item_id = proof.get("source_item_id")
    if (
        not isinstance(source_item_id, str)
        or not source_item_id
        or source_item_id != source_item_id.strip()
        or source_item_id != item.get("source_item_id")
    ):
        raise ValueError("proof source item id does not exactly match")
    if proof.get("requested_type") != requested:
        raise ValueError("proof requested representation does not exactly match")
    if proof.get("attempted_type") != attempted:
        raise ValueError("proof attempted representation does not exactly match")
    if (
        requested not in TEXT_ITEM_FALLBACK_LADDERS
        or attempted not in TEXT_ITEM_FALLBACK_LADDERS[requested]
        or attempted == "raster"
    ):
        raise ValueError("attempted representation is not a fallible ladder rung")

    reason_code = proof.get("reason_code")
    if attempted in {"glyphs", "geometry"}:
        if reason_code not in CLOSED_SVG_ITEM_IMPOSSIBILITY_REASONS:
            raise ValueError("SVG impossibility reason is not a closed predicate")
        evidence = proof.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("SVG impossibility evidence is empty")
        attempted_source_results = proof.get("attempted_source_results")
        if (
            not isinstance(attempted_source_results, list)
            or len(attempted_source_results) != 1
            or not isinstance(attempted_source_results[0], dict)
            or attempted_source_results[0].get("source") != "svg_item_renderer"
            or attempted_source_results[0].get("outcome")
            != "proven_impossible"
            or attempted_source_results[0].get("reason_code") != reason_code
            or attempted_source_results[0].get("pdf_sha256") != pdf_sha256
            or attempted_source_results[0].get("page_number") != page_number
            or attempted_source_results[0].get("source_item_id") != source_item_id
            or proof.get("attempted_sources_complete") is not True
        ):
            raise ValueError("SVG impossibility source result is incomplete")
        created_ids = _validated_entity_ids(
            proof.get("created_entity_ids"),
            field_name="created_entity_ids",
            allow_empty=True,
        )
        removed_ids = _validated_entity_ids(
            proof.get("removed_entity_ids"),
            field_name="removed_entity_ids",
            allow_empty=True,
        )
        if proof.get("cleanup_complete") is not True:
            raise ValueError("proof cleanup is incomplete")
        if set(created_ids) != set(removed_ids):
            raise ValueError("proof cleanup did not remove exactly the owned entities")
        return dict(proof)

    if attempted != "3d_text":
        raise ValueError("representation has no closed impossibility predicate")
    if reason_code != "exact_font_unavailable":
        raise ValueError("proof reason is not the closed exact-font predicate")

    font_identity = proof.get("font_identity")
    item_font_identity = item.get("font_identity")
    if (
        not isinstance(font_identity, dict)
        or not isinstance(item_font_identity, dict)
        or font_identity != item_font_identity
    ):
        raise ValueError("proof font identity does not exactly match")
    raw_name = font_identity.get("raw_name")
    normalized_key = font_identity.get("normalized_key")
    if (
        not isinstance(raw_name, str)
        or not raw_name.strip()
        or raw_name != raw_name.strip()
        or not isinstance(normalized_key, str)
        or not normalized_key
        or normalized_key != normalized_key.strip()
        or normalized_key != _canonical_font_identity(raw_name)["normalized_key"]
    ):
        raise ValueError("proof font identity lacks an exact normalized binding")

    evidence = proof.get("evidence")
    if (
        not isinstance(evidence, dict)
        or not evidence
        or evidence.get("normalized_key") != normalized_key
    ):
        raise ValueError("proof evidence must be a nonempty dictionary")

    attempted_source_results = proof.get("attempted_source_results")
    if (
        not isinstance(attempted_source_results, list)
        or len(attempted_source_results) != 2
        or [
            result.get("source") if isinstance(result, dict) else None
            for result in attempted_source_results
        ] != ["embedded_font", "system_font"]
        or any(
            not isinstance(result, dict)
            or result.get("outcome") != "not_found"
            or result.get("font_identity") != font_identity
            or result.get("pdf_sha256") != pdf_sha256
            or result.get("page_number") != page_number
            or result.get("staging_complete") is not True
            for result in attempted_source_results
        )
        or proof.get("attempted_sources_complete") is not True
    ):
        raise ValueError(
            "proof must contain exact embedded/system font not-found results"
        )

    created_ids = _validated_entity_ids(
        proof.get("created_entity_ids"),
        field_name="created_entity_ids",
        allow_empty=True,
    )
    removed_ids = _validated_entity_ids(
        proof.get("removed_entity_ids"),
        field_name="removed_entity_ids",
        allow_empty=True,
    )
    if proof.get("cleanup_complete") is not True:
        raise ValueError("proof cleanup is incomplete")
    if set(created_ids) != set(removed_ids):
        raise ValueError("proof cleanup did not remove exactly the owned entities")

    return dict(proof)


def _record_text_mode_fallback(
    opts: ImportOptions,
    *,
    requested: str,
    delivered: str,
    reason: str,
    count: int,
    source_item_id: str,
    proof: Dict[str, Any],
) -> None:
    """Record only an item-specific, evidence-backed representation fallback."""
    requested = str(requested or "").strip().lower()
    delivered = str(delivered or "").strip().lower()
    reason = str(reason or "").strip()
    source_item_id = str(source_item_id or "").strip()
    proof = dict(proof or {})
    evidence = str(proof.get("evidence") or "").strip()
    attempted_types = [
        str(value or "").strip().lower()
        for value in list(proof.get("attempted_types") or [])
        if str(value or "").strip()
    ]
    if (
        not bool(proof.get("item_specific_proven_impossible"))
        or not evidence
        or requested not in attempted_types
    ):
        raise ValueError(
            "requested representation must be item-specifically proven impossible"
        )
    if (
        type(count) is not int
        or count <= 0
        or not requested
        or not delivered
        or requested == delivered
        or not reason
        or not source_item_id
    ):
        raise ValueError("fallback record requires exact modes, reason, count, and source id")
    events = getattr(opts, "text_mode_fallbacks", None)
    if events is None:
        raise ValueError("fallback ledger unavailable")
    for event in events:
        if (
            event.get("requested") == requested
            and event.get("delivered") == delivered
            and event.get("reason") == reason
            and event.get("proof") == proof
        ):
            source_ids = list(event.get("source_item_ids") or [])
            if source_item_id not in source_ids:
                source_ids.append(source_item_id)
                prior_count = event.get("count")
                if type(prior_count) is not int or prior_count <= 0:
                    raise ValueError("existing fallback record count is invalid")
                event["count"] = prior_count + count
            event["source_item_ids"] = source_ids
            return
    events.append(
        {
            "requested": requested,
            "delivered": delivered,
            "reason": reason,
            "count": count,
            "source_item_ids": [source_item_id],
            "proof": proof,
        }
    )


def _record_explicit_page_raster_delivery(
    opts: ImportOptions,
    *,
    page_num: int,
    raster_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Record one directly requested, verified full-page Raster delivery."""

    requested = _normalize_requested_text_type(str(getattr(opts, "text_mode", "") or ""))
    if requested != "raster" or not bool(getattr(opts, "import_text", False)):
        raise ValueError("explicit page Raster delivery was not requested")
    if type(page_num) is not int or page_num < 1 or not isinstance(raster_result, dict):
        raise ValueError("explicit page Raster context is invalid")
    page_number = page_num
    if raster_result.get("outcome", "verified") != "verified":
        raise ValueError("explicit page Raster was not verified")
    raster_ids = _validated_entity_ids(
        raster_result.get("created_entity_ids"),
        field_name="raster_result.created_entity_ids",
        allow_empty=False,
    )
    raster_evidence = dict(raster_result.get("evidence") or {})
    if not raster_evidence:
        raise ValueError("verified page Raster evidence is required")
    source_item_id = "p%d:page" % page_number
    attempt = {
        "source_item_id": source_item_id,
        "requested_type": "raster",
        "attempted_type": "raster",
        "final_type": "raster",
        "outcome": "verified",
        "created_entity_ids": list(raster_ids),
        "delivery_entity_ids": list(raster_ids),
        "support_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "delivery_count": len(raster_ids),
        "evidence": raster_evidence,
    }
    _append_text_item_attempt(opts, attempt)
    _record_text_delivery(opts, "raster_text_patch", len(raster_ids))
    return {
        "entity_type": "raster",
        "count": len(raster_ids),
        "source_item_count": 1,
        "source_item_ids": [source_item_id],
        "font_rendered": False,
        "examples": [],
        "attempts": [dict(attempt)],
    }


def _record_no_source_text_page_fallback(
    opts: ImportOptions,
    *,
    page_num: int,
    pdf_sha256: str,
    raw_tdict: Dict[str, Any],
    raster_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Record a finite page-bound ladder when canonical source text is absent."""
    requested = _normalize_requested_text_type(str(getattr(opts, "text_mode", "") or ""))
    if requested == "raster":
        return _record_explicit_page_raster_delivery(
            opts,
            page_num=page_num,
            raster_result=raster_result,
        )
    page_number = page_num
    digest = pdf_sha256.strip().lower() if isinstance(pdf_sha256, str) else ""
    if (
        type(page_number) is not int
        or page_number < 1
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not isinstance(raw_tdict, dict)
        or not isinstance(raster_result, dict)
        or raster_result.get("outcome", "verified") != "verified"
    ):
        raise ValueError("no-source page fallback context is invalid")
    source_item_id = "p%d:page" % page_number
    try:
        canonical_source_items = list(
            _iter_text_source_items(raw_tdict, page_number, digest, requested)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("no-source page fallback source text is invalid") from exc
    if canonical_source_items:
        raise ValueError("no-source page fallback found canonical source text")
    raster_ids = _validated_entity_ids(
        raster_result.get("created_entity_ids"),
        field_name="raster_result.created_entity_ids",
        allow_empty=False,
    )
    raster_evidence = dict(raster_result.get("evidence") or {})
    if not raster_evidence:
        raise ValueError("verified page raster evidence is required")

    blocks = list(raw_tdict.get("blocks") or [])
    source_observation = {
        "text_dictionary_present": True,
        "canonical_source_item_count": 0,
        "raw_text_block_count": sum(
            1 for block in blocks if isinstance(block, dict) and block.get("type") == 0
        ),
        "source_inspection_reused": True,
        "visible_source_text_found": False,
    }
    ladder = list(TEXT_ITEM_FALLBACK_LADDERS[requested])
    attempted_types: List[str] = []
    proof_chain: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []

    attempt_ledger = getattr(opts, "text_delivery_attempts", None)
    fallback_ledger = getattr(opts, "text_mode_fallbacks", None)
    delivery_counts = getattr(opts, "text_delivered_counts", None)
    if (
        not isinstance(attempt_ledger, list)
        or not isinstance(fallback_ledger, list)
        or not isinstance(delivery_counts, dict)
    ):
        raise ValueError("no-source text fallback ledgers are unavailable")
    prior_attempts = copy.deepcopy(attempt_ledger)
    prior_fallbacks = copy.deepcopy(fallback_ledger)
    prior_delivery_counts = copy.deepcopy(delivery_counts)
    staged_opts = copy.copy(opts)
    staged_opts.text_delivery_attempts = copy.deepcopy(prior_attempts)
    staged_opts.text_mode_fallbacks = copy.deepcopy(prior_fallbacks)
    staged_opts.text_delivered_counts = copy.deepcopy(prior_delivery_counts)

    try:
        for index, attempted_type in enumerate(ladder):
            attempted_types.append(attempted_type)
            if attempted_type == "raster":
                break
            following_type = ladder[index + 1]
            proof = {
                "item_specific_proven_impossible": True,
                "importer_identity": FREECAD_TEXT_IMPORTER_IDENTITY,
                "pdf_sha256": digest,
                "page_number": page_number,
                "source_item_id": source_item_id,
                "requested_type": requested,
                "attempted_type": attempted_type,
                "reason_code": "no_source_text_items",
                "evidence": dict(source_observation),
                "attempted_types": list(attempted_types),
                "attempted_source_results": [
                    {
                        "source": "pymupdf_text_dictionary",
                        "outcome": "not_found",
                        "importer_identity": FREECAD_TEXT_IMPORTER_IDENTITY,
                        "pdf_sha256": digest,
                        "page_number": page_number,
                        "source_item_id": source_item_id,
                        "source_item_ids": [],
                        "canonical_source_item_count": 0,
                        "visible_source_text_found": False,
                    }
                ],
                "attempted_sources_complete": True,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            }
            attempt = {
                "source_item_id": source_item_id,
                "requested_type": requested,
                "attempted_type": attempted_type,
                "final_type": None,
                "outcome": "proven_impossible",
                "reason": "no_source_text_items",
                "reason_code": "no_source_text_items",
                "transition_from": attempted_type,
                "transition_to": following_type,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
                "proof": proof,
            }
            _append_text_item_attempt(staged_opts, attempt)
            attempts.append(attempt)
            proof_chain.append(proof)

        verified_attempt = {
            "source_item_id": source_item_id,
            "requested_type": requested,
            "attempted_type": "raster",
            "final_type": "raster",
            "outcome": "verified",
            "reason": (
                "explicit requested page Raster"
                if requested == "raster"
                else "verified page Raster after finite no-source-text proof chain"
            ),
            "created_entity_ids": list(raster_ids),
            "delivery_entity_ids": list(raster_ids),
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": len(raster_ids),
            "attempted_types": list(attempted_types),
            "proof_chain": [dict(proof) for proof in proof_chain],
            "evidence": raster_evidence,
        }
        _append_text_item_attempt(staged_opts, verified_attempt)
        attempts.append(verified_attempt)

        if requested != "raster":
            fallback_proof = {
                "item_specific_proven_impossible": True,
                "importer_identity": FREECAD_TEXT_IMPORTER_IDENTITY,
                "pdf_sha256": digest,
                "page_number": page_number,
                "source_item_id": source_item_id,
                "requested_type": requested,
                "attempted_type": ladder[-2],
                "reason_code": "no_source_text_items",
                "evidence": dict(source_observation),
                "attempted_types": list(attempted_types),
                "attempted_source_results": [
                    {
                        "source": "pymupdf_text_dictionary",
                        "outcome": "not_found",
                        "importer_identity": FREECAD_TEXT_IMPORTER_IDENTITY,
                        "pdf_sha256": digest,
                        "page_number": page_number,
                        "source_item_id": source_item_id,
                        "source_item_ids": [],
                        "canonical_source_item_count": 0,
                        "visible_source_text_found": False,
                    }
                ],
                "attempted_sources_complete": True,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
                "proof_chain": [dict(proof) for proof in proof_chain],
                "transition_chain": [
                    {"from": left, "to": right}
                    for left, right in zip(ladder, ladder[1:])
                ],
            }
            _record_text_mode_fallback(
                staged_opts,
                requested=requested,
                delivered="raster",
                reason="proof_gated:%s:no_source_text_items" % requested,
                count=1,
                source_item_id=source_item_id,
                proof=fallback_proof,
            )

        _record_text_delivery(staged_opts, "raster_text_patch", len(raster_ids))
        attempt_ledger[:] = copy.deepcopy(staged_opts.text_delivery_attempts)
        fallback_ledger[:] = copy.deepcopy(staged_opts.text_mode_fallbacks)
        delivery_counts.clear()
        delivery_counts.update(copy.deepcopy(staged_opts.text_delivered_counts))
    except Exception:
        attempt_ledger[:] = prior_attempts
        fallback_ledger[:] = prior_fallbacks
        delivery_counts.clear()
        delivery_counts.update(prior_delivery_counts)
        raise
    return {
        "entity_type": "raster",
        "count": len(raster_ids),
        "source_item_count": 0,
        "source_item_ids": [source_item_id],
        "font_rendered": False,
        "examples": [],
        "attempts": attempts,
    }


class TextRepresentationFailure(RuntimeError):
    """A requested text representation failed without proven fallback authority."""

    def __init__(self, message: str, attempt: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.attempt = dict(attempt or {})


def _append_text_item_attempt(opts: ImportOptions, attempt: Dict[str, Any]) -> None:
    ledger = getattr(opts, "text_delivery_attempts", None)
    if not isinstance(ledger, list):
        raise ValueError("text delivery attempt ledger is unavailable")
    entry = dict(attempt or {})
    if not ledger or ledger[-1] != entry:
        ledger.append(entry)


def _failed_text_item_attempt(
    item: Dict[str, Any],
    requested: str,
    attempted: str,
    reason: str,
    reported: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    reported = reported if isinstance(reported, dict) else {}

    def reported_ids(field_name: str) -> List[str]:
        value = reported.get(field_name)
        if not isinstance(value, list):
            return []
        return [
            entity_id
            for entity_id in value
            if isinstance(entity_id, str) and entity_id
        ]

    created_ids = reported_ids("created_entity_ids")
    removed_ids = reported_ids("removed_entity_ids")
    cleanup_complete = bool(
        reported.get("cleanup_complete") is True
        and set(created_ids) == set(removed_ids)
    )
    return {
        "source_item_id": str(item.get("source_item_id") or ""),
        "requested_type": requested,
        "attempted_type": attempted,
        "final_type": None,
        "outcome": "failed",
        "reason": str(reason or "text_delivery_failed"),
        "created_entity_ids": created_ids,
        "removed_entity_ids": removed_ids,
        "cleanup_complete": cleanup_complete,
    }


def _normalize_impossible_attempt(
    item: Dict[str, Any],
    requested: str,
    attempted: str,
    attempt: Dict[str, Any],
    proof: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(attempt, dict) or not attempt:
        raise ValueError("typed impossibility must include an exact attempt")
    if attempt.get("source_item_id") != item.get("source_item_id"):
        raise ValueError("impossibility attempt source item id does not match")
    if attempt.get("requested_type") != requested:
        raise ValueError("impossibility attempt requested representation does not match")
    if attempt.get("attempted_type") != attempted:
        raise ValueError("impossibility attempt representation does not match")
    if attempt.get("final_type") is not None:
        raise ValueError("impossible attempt cannot have a final representation")
    if attempt.get("outcome") != "proven_impossible":
        raise ValueError("impossibility attempt outcome is not proven_impossible")
    if attempt.get("reason_code") != proof.get("reason_code"):
        raise ValueError("impossibility attempt reason does not match its proof")

    created_ids = _validated_entity_ids(
        attempt.get("created_entity_ids"),
        field_name="attempt.created_entity_ids",
        allow_empty=True,
    )
    removed_ids = _validated_entity_ids(
        attempt.get("removed_entity_ids"),
        field_name="attempt.removed_entity_ids",
        allow_empty=True,
    )
    if created_ids != proof.get("created_entity_ids"):
        raise ValueError("impossibility attempt created ids do not match its proof")
    if removed_ids != proof.get("removed_entity_ids"):
        raise ValueError("impossibility attempt removed ids do not match its proof")
    if attempt.get("cleanup_complete") is not True:
        raise ValueError("impossibility attempt cleanup is incomplete")

    normalized = dict(attempt)
    normalized["proof"] = dict(proof)
    return normalized


def _normalize_verified_text_item_result(
    item: Dict[str, Any],
    requested: str,
    attempted: str,
    result: Dict[str, Any],
) -> Dict[str, Any]:
    if not isinstance(result, dict) or not result:
        raise ValueError("deliverer returned no result")
    if result.get("outcome") != "verified":
        raise ValueError("deliverer result is not verified")
    if result.get("source_item_id") != item.get("source_item_id"):
        raise ValueError("deliverer result source item id does not match")
    if "requested_type" in result and result.get("requested_type") != requested:
        raise ValueError("deliverer result requested representation does not match")
    if result.get("attempted_type") != attempted:
        raise ValueError("deliverer result attempted representation does not match")
    if result.get("final_type") != attempted:
        raise ValueError("deliverer result final representation does not match")
    created_ids = _validated_entity_ids(
        result.get("created_entity_ids"),
        field_name="created_entity_ids",
        allow_empty=False,
    )
    removed_ids = _validated_entity_ids(
        result.get("removed_entity_ids", []),
        field_name="removed_entity_ids",
        allow_empty=True,
    )
    delivery_ids = _validated_entity_ids(
        result.get("delivery_entity_ids", created_ids),
        field_name="delivery_entity_ids",
        allow_empty=False,
    )
    support_ids = _validated_entity_ids(
        result.get("support_entity_ids", []),
        field_name="support_entity_ids",
        allow_empty=True,
    )
    created_set = set(created_ids)
    removed_set = set(removed_ids)
    delivery_set = set(delivery_ids)
    support_set = set(support_ids)
    live_set = created_set.difference(removed_set)
    if not removed_set.issubset(created_set):
        raise ValueError("deliverer result reports removal of an unowned entity")
    if (
        not delivery_set.issubset(live_set)
        or not support_set.issubset(live_set)
        or delivery_set.intersection(support_set)
        or delivery_set.union(support_set) != live_set
    ):
        raise ValueError("deliverer result delivery/support ownership is invalid")
    if not live_set:
        raise ValueError("deliverer result has no verified final entity")
    if result.get("cleanup_complete") is not True:
        raise ValueError("deliverer result cleanup is unverified")
    evidence = result.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("deliverer result evidence is empty or unverified")

    normalized = dict(result)
    normalized.update(
        {
            "requested_type": requested,
            "attempted_type": attempted,
            "final_type": attempted,
            "created_entity_ids": created_ids,
            "delivery_entity_ids": delivery_ids,
            "support_entity_ids": support_ids,
            "removed_entity_ids": removed_ids,
            "evidence": dict(evidence),
        }
    )
    return normalized


def _aggregate_text_item_fallback_proof(
    item: Dict[str, Any],
    requested: str,
    attempted_types: List[str],
    proofs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    proof_chain = [dict(proof) for proof in proofs]
    last_proof = proof_chain[-1]
    aggregate = {
        "item_specific_proven_impossible": True,
        "importer_identity": item.get("importer_identity"),
        "pdf_sha256": item.get("pdf_sha256"),
        "page_number": item.get("page_number"),
        "source_item_id": item.get("source_item_id"),
        "requested_type": requested,
        "attempted_type": last_proof["attempted_type"],
        "reason_code": last_proof["reason_code"],
        "evidence": {
            "reason_codes": [proof["reason_code"] for proof in proof_chain],
            "proof_chain_length": len(proof_chain),
        },
        "attempted_types": list(attempted_types),
        "attempted_source_results": [
            dict(result)
            for proof in proof_chain
            for result in proof["attempted_source_results"]
        ],
        "attempted_sources_complete": True,
        "created_entity_ids": [
            entity_id
            for proof in proof_chain
            for entity_id in proof["created_entity_ids"]
        ],
        "removed_entity_ids": [
            entity_id
            for proof in proof_chain
            for entity_id in proof["removed_entity_ids"]
        ],
        "cleanup_complete": True,
        "proof_chain": proof_chain,
        "transition_chain": [
            {"from": left, "to": right}
            for left, right in zip(attempted_types, attempted_types[1:])
        ],
    }
    for proof in proof_chain:
        if proof.get("font_identity"):
            aggregate["font_identity"] = dict(proof["font_identity"])
            break
    return aggregate


def _run_text_item_fallback_ladder(
    item: Dict[str, Any],
    requested: str,
    deliverers: Dict[str, Any],
    opts: ImportOptions,
) -> Dict[str, Any]:
    """Deliver one source item, advancing only across exact proven impossibility."""
    requested_mode = requested if isinstance(requested, str) else ""
    bound_item = copy.deepcopy(item) if isinstance(item, dict) else {}
    source_item_id = bound_item.get("source_item_id")
    item_requested = bound_item.get("requested_type")
    if (
        requested_mode not in TEXT_ITEM_FALLBACK_LADDERS
        or not isinstance(item, dict)
        or bound_item.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY
        or not isinstance(source_item_id, str)
        or not source_item_id
        or source_item_id != source_item_id.strip()
        or (item_requested is not None and item_requested != requested_mode)
        or not isinstance(deliverers, dict)
    ):
        failed = _failed_text_item_attempt(
            bound_item,
            requested_mode,
            requested_mode,
            "invalid_fallback_executor_input",
        )
        _append_text_item_attempt(opts, failed)
        raise TextRepresentationFailure("Invalid text item fallback request", failed)

    attempted_types: List[str] = []
    validated_proofs: List[Dict[str, Any]] = []
    for attempted_mode in TEXT_ITEM_FALLBACK_LADDERS[requested_mode]:
        attempted_types.append(attempted_mode)
        deliverer = deliverers.get(attempted_mode)
        if not callable(deliverer):
            failed = _failed_text_item_attempt(
                bound_item,
                requested_mode,
                attempted_mode,
                "deliverer_unavailable",
            )
            _append_text_item_attempt(opts, failed)
            raise TextRepresentationFailure(
                "%s deliverer is unavailable" % attempted_mode,
                failed,
            )

        try:
            result = deliverer(copy.deepcopy(bound_item), attempted_mode, opts)
        except TextItemImpossible as impossible:
            try:
                proof = _validate_item_impossibility_proof(
                    bound_item,
                    requested_mode,
                    attempted_mode,
                    impossible.proof,
                )
                proven_attempt = _normalize_impossible_attempt(
                    bound_item,
                    requested_mode,
                    attempted_mode,
                    impossible.attempt,
                    proof,
                )
            except Exception as proof_error:
                failed = _failed_text_item_attempt(
                    bound_item,
                    requested_mode,
                    attempted_mode,
                    "invalid_impossibility_proof",
                    impossible.attempt,
                )
                _append_text_item_attempt(opts, failed)
                raise TextRepresentationFailure(
                    "%s impossibility proof rejected: %s"
                    % (attempted_mode, proof_error),
                    failed,
                ) from impossible
            _append_text_item_attempt(opts, proven_attempt)
            validated_proofs.append(proof)
            if attempted_mode == "raster":
                raise TextRepresentationFailure(
                    "Raster is terminal for source item %s" % source_item_id,
                    proven_attempt,
                ) from impossible
            continue
        except TextRepresentationFailure as failure:
            if failure.attempt:
                _append_text_item_attempt(opts, failure.attempt)
                raise
            failed = _failed_text_item_attempt(
                bound_item,
                requested_mode,
                attempted_mode,
                "text_representation_failure",
            )
            _append_text_item_attempt(opts, failed)
            raise TextRepresentationFailure(str(failure), failed) from failure
        except Exception as error:
            failed = _failed_text_item_attempt(
                bound_item,
                requested_mode,
                attempted_mode,
                "generic_exception:%s" % error.__class__.__name__,
            )
            _append_text_item_attempt(opts, failed)
            raise TextRepresentationFailure(
                "%s delivery failed without a validated impossibility proof: %s"
                % (attempted_mode, error),
                failed,
            ) from error

        try:
            normalized = _normalize_verified_text_item_result(
                bound_item,
                requested_mode,
                attempted_mode,
                result,
            )
        except Exception as result_error:
            failed = _failed_text_item_attempt(
                bound_item,
                requested_mode,
                attempted_mode,
                "empty_mismatched_or_unverified_result",
                result if isinstance(result, dict) else None,
            )
            _append_text_item_attempt(opts, failed)
            raise TextRepresentationFailure(
                "%s returned an empty, mismatched, or unverified result: %s"
                % (attempted_mode, result_error),
                failed,
            ) from result_error

        normalized["attempted_types"] = list(attempted_types)
        normalized["proof_chain"] = [dict(proof) for proof in validated_proofs]
        _append_text_item_attempt(opts, normalized)
        if validated_proofs:
            fallback_proof = _aggregate_text_item_fallback_proof(
                bound_item,
                requested_mode,
                attempted_types,
                validated_proofs,
            )
            _record_text_mode_fallback(
                opts,
                requested=requested_mode,
                delivered=attempted_mode,
                reason="proof_gated:" + " -> ".join(
                    "%s:%s" % (proof["attempted_type"], proof["reason_code"])
                    for proof in validated_proofs
                ),
                count=1,
                source_item_id=source_item_id,
                proof=fallback_proof,
            )
        return normalized

    failed = _failed_text_item_attempt(
        bound_item,
        requested_mode,
        "raster",
        "fallback_ladder_exhausted",
    )
    _append_text_item_attempt(opts, failed)
    raise TextRepresentationFailure("Text item fallback ladder exhausted", failed)


def _write_terminal_representation_failure_report(
    *,
    pdf_path: str,
    opts: ImportOptions,
    total_pages: int,
    pages_imported: int,
    elapsed_ms: float,
    failure: TextRepresentationFailure,
) -> str:
    """Persist the exact failed attempt after the document transaction aborts."""
    report_extra = dict(getattr(opts, "_report_extra", {}) or {})
    report_extra.update(
        {
            "result_status": "failed",
            "terminal_failure": {
                "type": failure.__class__.__name__,
                "message": str(failure),
                "attempt": dict(getattr(failure, "attempt", {}) or {}),
            },
        }
    )
    opts._report_extra = report_extra
    opts.phase_timings_ms["total_ms"] = float(elapsed_ms)
    fallback_used, fallback_reason = _report_fallback_state(opts)
    report_path = opts.import_report_path or _default_import_report_path(pdf_path)
    return write_import_report(
        pdf_path=pdf_path,
        output_path=report_path,
        opts=opts,
        pages_imported=int(pages_imported),
        total_pages=int(total_pages),
        primitive_count=0,
        text_count=_structural_text_delivery_count(opts),
        image_count=0,
        elapsed_ms=float(elapsed_ms),
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )


def _host_object_id(obj) -> str:
    return str(getattr(obj, "Name", "") or getattr(obj, "Label", "") or "")


def _document_objects(doc) -> List[Any]:
    try:
        return list(getattr(doc, "Objects", []) or [])
    except (AttributeError, RuntimeError, TypeError):
        return []


def _post_baseline_document_objects(
    objects: List[Any],
    baseline_object_ids: set,
    baseline_object_names: set,
) -> List[Any]:
    """Return only objects new by both runtime identity and stable FreeCAD name."""

    ids = set(baseline_object_ids or set())
    names = set(baseline_object_names or set())
    return [
        host_obj
        for host_obj in list(objects or [])
        if id(host_obj) not in ids and _host_object_id(host_obj) not in names
    ]


_STRUCTURAL_TEXT_REPRESENTATIONS = {"labels", "text", "3d_text", "glyphs", "geometry"}


def _host_object_type_id(obj) -> str:
    try:
        return str(getattr(obj, "TypeId", "") or "")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""


def _host_object_representation(obj) -> str:
    try:
        return str(getattr(obj, "PDFRepresentation", "") or "").strip().lower()
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""


def _host_object_source_item_id(obj) -> str:
    try:
        return str(getattr(obj, "PDFSourceItemId", "") or "")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""


def _host_object_parent_source_item_id(obj) -> str:
    try:
        return str(getattr(obj, "PDFParentSourceItemId", "") or "")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""


def _host_object_text_values(obj, property_name: str) -> List[str]:
    try:
        value = getattr(obj, property_name)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    try:
        return [str(item) for item in list(value)]
    except (TypeError, ValueError, RuntimeError):
        text = str(value or "")
        return [text] if text else []


def _host_image_content_snapshot(image_file: str) -> Dict[str, Any]:
    """Identify a persisted image by readable bytes, independent of cache pathname."""

    raw_path = str(image_file or "")
    snapshot: Dict[str, Any] = {
        "image_file": "persisted" if raw_path else "",
        "image_sha256": "",
        "image_bytes": 0,
    }
    if not raw_path:
        return snapshot
    try:
        path = Path(raw_path)
        byte_count = path.stat().st_size
        if byte_count <= 0:
            return snapshot
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        snapshot["image_sha256"] = digest.hexdigest()
        snapshot["image_bytes"] = int(byte_count)
    except (OSError, RuntimeError, TypeError, ValueError):
        pass
    return snapshot


def _finite_host_number(value: Any) -> Optional[str]:
    """Return a stable finite numeric token suitable for save/reopen equality."""

    try:
        number = float(value)
    except (TypeError, ValueError, RuntimeError, AttributeError):
        return None
    return format(number, ".12g") if math.isfinite(number) else None


_SHAPE_FINGERPRINT_QUANTUM = 1e-7
_SHAPE_FINGERPRINT_EDGE_SAMPLES = 7


def _quantized_shape_number(value: Any) -> Optional[int]:
    """Quantize a finite OCCT value beyond normal save/reopen roundoff."""

    try:
        number = float(value)
        scaled = number / _SHAPE_FINGERPRINT_QUANTUM
    except (TypeError, ValueError, RuntimeError, AttributeError, OverflowError):
        return None
    if not math.isfinite(number) or not math.isfinite(scaled):
        return None
    return int(round(scaled))


def _stable_shape_number(value: Any) -> Optional[str]:
    """Return a readable token backed by the canonical shape quantum."""

    units = _quantized_shape_number(value)
    if units is None:
        return None
    quantized = float(units) * _SHAPE_FINGERPRINT_QUANTUM
    if quantized == 0.0:
        quantized = 0.0
    return format(quantized, ".12g")


def _host_point_token(point: Any) -> Optional[Tuple[int, int, int]]:
    """Read a FreeCAD vector-like point into a tolerance-stable tuple."""

    coordinates: List[int] = []
    for lower_name, upper_name in (("x", "X"), ("y", "Y"), ("z", "Z")):
        try:
            value = getattr(point, lower_name)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            try:
                value = getattr(point, upper_name)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                return None
        token = _quantized_shape_number(value)
        if token is None:
            return None
        coordinates.append(token)
    return coordinates[0], coordinates[1], coordinates[2]


def _canonical_point_path(
    points: List[Tuple[int, int, int]],
) -> Tuple[bool, Tuple[Tuple[int, int, int], ...]]:
    """Canonicalize edge orientation and a closed edge's possible seam."""

    if not points:
        return False, ()
    sequence = tuple(points)
    closed = len(sequence) > 2 and sequence[0] == sequence[-1]
    if closed:
        sequence = sequence[:-1]
        if not sequence:
            return True, ()
        candidates = []
        for oriented in (sequence, tuple(reversed(sequence))):
            candidates.extend(
                oriented[index:] + oriented[:index]
                for index in range(len(oriented))
            )
        return True, min(candidates)
    return False, min(sequence, tuple(reversed(sequence)))


def _host_edge_fingerprint(edge: Any) -> Tuple[Any, ...]:
    """Fingerprint an edge without trusting volatile BREP serialization."""

    try:
        curve = getattr(edge, "Curve", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        curve = None
    curve_type = type(curve).__name__ if curve is not None else ""
    try:
        length_token = _quantized_shape_number(edge.Length)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        length_token = None

    sampled_points: List[Any] = []
    try:
        discretize = getattr(edge, "discretize", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        discretize = None
    if callable(discretize):
        try:
            sampled_points = list(
                discretize(Number=_SHAPE_FINGERPRINT_EDGE_SAMPLES) or []
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            sampled_points = []
    if not sampled_points:
        try:
            sampled_points = [
                vertex.Point
                for vertex in list(getattr(edge, "Vertexes", []) or [])
            ]
        except (AttributeError, RuntimeError, TypeError, ValueError):
            sampled_points = []

    point_tokens = [
        token
        for token in (_host_point_token(point) for point in sampled_points)
        if token is not None
    ]
    closed, canonical_path = _canonical_point_path(point_tokens)
    return (
        curve_type,
        length_token if length_token is not None else "",
        1 if closed else 0,
        canonical_path,
    )


def _host_shape_geometry_fingerprint(shape: Any) -> Dict[str, Any]:
    """Build an order-independent, tolerance-aware geometry fingerprint."""

    try:
        vertices = list(getattr(shape, "Vertexes", []) or [])
    except (AttributeError, RuntimeError, TypeError, ValueError):
        vertices = []
    vertex_tokens = []
    invalid_vertex_count = 0
    for vertex in vertices:
        try:
            point = vertex.Point
        except (AttributeError, RuntimeError, TypeError, ValueError):
            invalid_vertex_count += 1
            continue
        token = _host_point_token(point)
        if token is None:
            invalid_vertex_count += 1
        else:
            vertex_tokens.append(token)
    vertex_tokens.sort()

    try:
        edges = list(getattr(shape, "Edges", []) or [])
    except (AttributeError, RuntimeError, TypeError, ValueError):
        edges = []
    edge_tokens = [_host_edge_fingerprint(edge) for edge in edges]
    edge_tokens.sort(
        key=lambda token: json.dumps(
            token,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    sampled_edge_count = sum(1 for token in edge_tokens if token[-1])
    return {
        "schema": "bcs.freecad_shape_fingerprint/1.0",
        "quantum": format(_SHAPE_FINGERPRINT_QUANTUM, ".12g"),
        "vertices": vertex_tokens,
        "edges": edge_tokens,
        "vertex_count": len(vertices),
        "invalid_vertex_count": invalid_vertex_count,
        "edge_count": len(edges),
        "sampled_edge_count": sampled_edge_count,
        "canonical_geometry_present": bool(vertex_tokens or sampled_edge_count),
    }


def _host_shape_content_snapshot(obj, type_id: str) -> Dict[str, Any]:
    """Capture meaningful persisted topology and geometry, not just non-nullness."""

    shape_nonempty = _has_nonempty_host_geometry(obj, type_id)
    topology_counts: Dict[str, int] = {}
    metrics: Dict[str, str] = {}
    bounds: Dict[str, str] = {}
    try:
        shape = getattr(obj, "Shape", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        shape = None

    if shape is not None:
        for property_name in (
            "Vertexes",
            "Edges",
            "Wires",
            "Faces",
            "Shells",
            "Solids",
            "CompSolids",
            "Compounds",
        ):
            try:
                collection = getattr(shape, property_name)
                topology_counts[property_name.lower()] = len(collection)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
        for property_name in ("Length", "Area", "Volume"):
            try:
                token = _stable_shape_number(getattr(shape, property_name))
            except (AttributeError, RuntimeError, TypeError, ValueError):
                token = None
            if token is not None:
                metrics[property_name.lower()] = token
        try:
            bound_box = shape.BoundBox
        except (AttributeError, RuntimeError, TypeError, ValueError):
            bound_box = None
        if bound_box is not None:
            for property_name in (
                "XMin",
                "YMin",
                "ZMin",
                "XMax",
                "YMax",
                "ZMax",
            ):
                try:
                    token = _stable_shape_number(getattr(bound_box, property_name))
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    token = None
                if token is not None:
                    bounds[property_name.lower()] = token
    else:
        try:
            geometry_count = obj.GeometryCount
            if type(geometry_count) is int and geometry_count >= 0:
                topology_counts["geometry_count"] = geometry_count
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass

    meaningful_topology = any(
        type(count) is int and count > 0 for count in topology_counts.values()
    )
    geometry_fingerprint = (
        _host_shape_geometry_fingerprint(shape) if shape is not None else {}
    )
    digest_payload = {
        "topology_counts": topology_counts,
        "metrics": metrics,
        "bounds": bounds,
        "geometry": geometry_fingerprint,
    }
    shape_digest = ""
    if shape_nonempty and meaningful_topology:
        shape_digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    return {
        "shape_nonempty": bool(shape_nonempty),
        "shape_structure_verified": bool(
            shape_nonempty and meaningful_topology and shape_digest
        ),
        "shape_topology_counts": topology_counts,
        "shape_metrics": metrics,
        "shape_bounds": bounds,
        "shape_digest": shape_digest,
        "shape_fingerprint_schema": geometry_fingerprint.get("schema", ""),
        "shape_fingerprint_quantum": geometry_fingerprint.get("quantum", ""),
        "shape_fingerprint_vertex_count": geometry_fingerprint.get(
            "vertex_count", 0
        ),
        "shape_fingerprint_edge_count": geometry_fingerprint.get("edge_count", 0),
        "shape_fingerprint_sampled_edge_count": geometry_fingerprint.get(
            "sampled_edge_count", 0
        ),
        "shape_fingerprint_verified": bool(
            geometry_fingerprint.get("canonical_geometry_present")
            and geometry_fingerprint.get("invalid_vertex_count") == 0
        ),
    }


def _host_view_style_snapshot(obj) -> Dict[str, Any]:
    """Capture the real GUI view-provider style when the host exposes one."""

    try:
        view = getattr(obj, "ViewObject", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        view = None
    snapshot: Dict[str, Any] = {"view_present": view is not None}
    if view is None:
        return snapshot
    for property_name in ("FontName", "Font", "Justification"):
        try:
            if hasattr(view, property_name):
                snapshot[property_name.lower()] = str(
                    getattr(view, property_name) or ""
                )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
    try:
        if hasattr(view, "FontSize"):
            token = _finite_host_number(view.FontSize)
            snapshot["font_size"] = token
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    colors: Dict[str, List[str]] = {}
    for property_name in ("TextColor", "ShapeColor", "LineColor", "PointColor"):
        try:
            value = getattr(view, property_name)
            channels = list(value)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
        normalized = [_finite_host_number(channel) for channel in channels[:4]]
        if normalized and all(channel is not None for channel in normalized):
            colors[property_name.lower()] = [str(channel) for channel in normalized]
    if colors:
        snapshot["colors"] = colors
    return snapshot


def _host_object_content_snapshot(
    obj,
    type_id: str,
    representation: str,
) -> Dict[str, Any]:
    """Capture persisted content that metadata-only delivery cannot imitate."""

    content: Dict[str, Any] = {}
    if type_id.startswith(("Part::", "PartDesign::", "Sketcher::")):
        content.update(_host_shape_content_snapshot(obj, type_id))
    if representation in {"labels", "text"}:
        try:
            proxy_type = str(getattr(getattr(obj, "Proxy", None), "Type", "") or "")
        except (AttributeError, RuntimeError, TypeError, ValueError):
            proxy_type = ""
        content.update(
            {
                "proxy_type": proxy_type,
                "text": _host_object_text_values(obj, "Text"),
                "custom_text": (
                    _host_object_text_values(obj, "CustomText")
                    if representation == "labels"
                    else []
                ),
                "font_name": str(getattr(obj, "PDFTextFontName", "") or ""),
                "justification": str(
                    getattr(obj, "PDFTextJustification", "") or ""
                ),
                "color_rgb": str(getattr(obj, "PDFTextColorRGB", "") or ""),
                "view_style": _host_view_style_snapshot(obj),
            }
        )
        try:
            font_size = float(obj.PDFTextFontSize)
            content["font_size"] = font_size if math.isfinite(font_size) else None
        except (AttributeError, RuntimeError, TypeError, ValueError):
            content["font_size"] = None
    if representation == "3d_text":
        content["string"] = _host_object_text_values(obj, "String")
        try:
            base = getattr(obj, "Base", None)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            base = None
        content["base_entity_id"] = _host_object_id(base) if base is not None else ""
    if representation == "raster" or type_id.startswith("Image::"):
        try:
            image_file = str(getattr(obj, "ImageFile", "") or "")
        except (AttributeError, RuntimeError, TypeError, ValueError):
            image_file = ""
        image_snapshot = _host_image_content_snapshot(image_file)
        if image_snapshot.get("image_bytes", 0) <= 0:
            try:
                included_file = str(getattr(obj, "PDFRasterFile", "") or "")
            except (AttributeError, RuntimeError, TypeError, ValueError):
                included_file = ""
            included_snapshot = _host_image_content_snapshot(included_file)
            if included_snapshot.get("image_bytes", 0) > 0:
                image_snapshot = included_snapshot
        content.update(image_snapshot)
        for property_name, content_name in (
            ("PDFSourceSHA256", "pdf_source_sha256"),
            ("PDFRasterSHA256", "declared_raster_sha256"),
        ):
            try:
                content[content_name] = str(getattr(obj, property_name, "") or "")
            except (AttributeError, RuntimeError, TypeError, ValueError):
                content[content_name] = ""
    return content


def _is_host_container(obj, type_id: str) -> bool:
    if type_id.startswith("App::DocumentObjectGroup"):
        return True
    try:
        derived = getattr(obj, "isDerivedFrom", None)
        return bool(callable(derived) and derived("App::DocumentObjectGroup"))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


def _has_nonempty_host_geometry(obj, type_id: str) -> bool:
    if not type_id.startswith(("Part::", "PartDesign::", "Sketcher::")):
        return False
    try:
        shape = getattr(obj, "Shape", None)
        if shape is not None:
            is_null = getattr(shape, "isNull", None)
            return not bool(is_null()) if callable(is_null) else True
        geometry_count = getattr(obj, "GeometryCount", None)
        if geometry_count is not None:
            return int(geometry_count) > 0
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    return False


def _host_object_category(obj, type_id: str, representation: str) -> str:
    if _is_host_container(obj, type_id):
        return "containers"
    if type_id.startswith("Image::"):
        return "images"
    if representation in _STRUCTURAL_TEXT_REPRESENTATIONS:
        return "text_representation_objects"
    if _has_nonempty_host_geometry(obj, type_id):
        return "vector_primitives"
    return "unclassified"


def _build_host_object_inventory(objects: List[Any]) -> Dict[str, Any]:
    """Inventory actual FreeCAD objects without treating containers as geometry."""

    categories: Dict[str, List[str]] = {
        "containers": [],
        "images": [],
        "vector_primitives": [],
        "text_representation_objects": [],
        "unclassified": [],
    }
    records: List[Dict[str, Any]] = []
    type_counts: Dict[str, int] = {}
    entity_ids: List[str] = []
    for host_obj in list(objects or []):
        entity_id = _host_object_id(host_obj)
        type_id = _host_object_type_id(host_obj)
        representation = _host_object_representation(host_obj)
        source_item_id = _host_object_source_item_id(host_obj)
        parent_source_item_id = _host_object_parent_source_item_id(host_obj)
        category = _host_object_category(host_obj, type_id, representation)
        records.append(
            {
                "entity_id": entity_id,
                "type_id": type_id,
                "representation": representation,
                "source_item_id": source_item_id,
                "parent_source_item_id": parent_source_item_id,
                "category": category,
                "content": _host_object_content_snapshot(
                    host_obj, type_id, representation
                ),
            }
        )
        entity_ids.append(entity_id)
        categories[category].append(entity_id)
        type_counts[type_id] = int(type_counts.get(type_id, 0)) + 1

    unique_nonempty_ids = bool(
        records and len(entity_ids) == len(set(entity_ids)) and all(entity_ids)
    )
    counts = {"total": len(records)}
    counts.update({key: len(value) for key, value in categories.items()})
    return {
        "schema": "bcs.freecad_host_object_inventory/1.1",
        "verified": unique_nonempty_ids,
        "entity_ids": entity_ids,
        "type_counts": type_counts,
        "counts": counts,
        "categories": categories,
        "objects": records,
    }


def _crosscheck_host_object_inventory(
    expected_inventory: Dict[str, Any],
    actual_objects: List[Any],
) -> Dict[str, Any]:
    """Cross-check saved/reopened identities, types, representations, and counts."""

    expected_records = [
        dict(record)
        for record in (expected_inventory.get("objects") or [])
        if isinstance(record, dict)
    ]
    expected_ids = [str(record.get("entity_id") or "") for record in expected_records]
    actual_by_id: Dict[str, Any] = {}
    duplicate_actual_ids: List[str] = []
    for host_obj in list(actual_objects or []):
        entity_id = _host_object_id(host_obj)
        if entity_id in actual_by_id:
            duplicate_actual_ids.append(entity_id)
        actual_by_id[entity_id] = host_obj

    expected_id_set = set(expected_ids)
    unexpected = [
        entity_id for entity_id in actual_by_id if entity_id not in expected_id_set
    ]

    missing: List[str] = []
    mismatches: List[Dict[str, Any]] = []
    matched_objects: List[Any] = []
    for expected in expected_records:
        entity_id = str(expected.get("entity_id") or "")
        actual = actual_by_id.get(entity_id)
        if actual is None:
            missing.append(entity_id)
            continue
        matched_objects.append(actual)
        actual_type = _host_object_type_id(actual)
        actual_representation = _host_object_representation(actual)
        expected_type = str(expected.get("type_id") or "")
        expected_representation = str(expected.get("representation") or "")
        actual_source_id = _host_object_source_item_id(actual)
        expected_source_id = str(expected.get("source_item_id") or "")
        actual_parent_source_id = _host_object_parent_source_item_id(actual)
        expected_parent_source_id = str(expected.get("parent_source_item_id") or "")
        actual_content = _host_object_content_snapshot(
            actual, actual_type, actual_representation
        )
        expected_content = expected.get("content")
        if (
            actual_type != expected_type
            or actual_representation != expected_representation
            or actual_source_id != expected_source_id
            or actual_parent_source_id != expected_parent_source_id
        ):
            mismatch = {
                "entity_id": entity_id,
                "expected_type_id": expected_type,
                "actual_type_id": actual_type,
                "expected_representation": expected_representation,
                "actual_representation": actual_representation,
            }
            if actual_source_id != expected_source_id:
                mismatch["expected_source_item_id"] = expected_source_id
                mismatch["actual_source_item_id"] = actual_source_id
            if actual_parent_source_id != expected_parent_source_id:
                mismatch["expected_parent_source_item_id"] = expected_parent_source_id
                mismatch["actual_parent_source_item_id"] = actual_parent_source_id
            mismatches.append(mismatch)
        elif isinstance(expected_content, dict) and actual_content != expected_content:
            mismatches.append(
                {
                    "entity_id": entity_id,
                    "expected_content": expected_content,
                    "actual_content": actual_content,
                }
            )

    actual_inventory = _build_host_object_inventory(list(actual_objects or []))
    expected_counts = dict(expected_inventory.get("counts") or {})
    actual_counts = dict(actual_inventory.get("counts") or {})
    counts_match = expected_counts == actual_counts
    verified = bool(
        expected_inventory.get("verified") is True
        and not missing
        and not mismatches
        and not duplicate_actual_ids
        and not unexpected
        and actual_inventory.get("verified") is True
        and set(actual_inventory.get("entity_ids") or []) == expected_id_set
        and counts_match
    )
    return {
        "required": True,
        "method": "document_object_identity_type_crosscheck",
        "verified": verified,
        "expected_entity_ids": expected_ids,
        "missing_entity_ids": missing,
        "duplicate_actual_entity_ids": list(dict.fromkeys(duplicate_actual_ids)),
        "unexpected_entity_ids": unexpected,
        "mismatched_entities": mismatches,
        "expected_counts": expected_counts,
        "actual_counts": actual_counts,
        "counts_match": counts_match,
        "expected_objects": expected_records,
        "actual_objects": list(actual_inventory.get("objects") or []),
    }


def _save_reopen_host_object_inventory(
    fc_doc,
    inventory: Dict[str, Any],
    baseline_entity_names: Optional[set] = None,
) -> Dict[str, Any]:
    """Save a temporary FCStd copy, reopen it, and verify imported host objects."""

    failure = {
        "required": True,
        "method": "temporary_fcstd_save_copy_reopen",
        "verified": False,
        "expected_entity_ids": list(inventory.get("entity_ids") or []),
        "missing_entity_ids": [],
        "duplicate_actual_entity_ids": [],
        "unexpected_entity_ids": [],
        "mismatched_entities": [],
        "expected_counts": dict(inventory.get("counts") or {}),
        "actual_counts": {},
        "counts_match": False,
        "expected_objects": list(inventory.get("objects") or []),
        "actual_objects": [],
    }
    if FreeCAD is None:
        failure["error"] = "freecad_runtime_unavailable"
        return failure
    save_copy = getattr(fc_doc, "saveCopy", None)
    if not callable(save_copy):
        failure["error"] = "document_save_copy_unavailable"
        return failure

    descriptor, temp_path = tempfile.mkstemp(prefix="bcs_pdf_inventory_", suffix=".FCStd")
    os.close(descriptor)
    try:
        os.remove(temp_path)
    except OSError:
        pass
    reopened = None
    original_name = str(getattr(fc_doc, "Name", "") or "")
    try:
        save_result = save_copy(temp_path)
        if save_result is False or not Path(temp_path).is_file():
            raise RuntimeError("temporary FCStd saveCopy did not create a file")
        open_document = getattr(FreeCAD, "openDocument", None)
        if not callable(open_document):
            raise RuntimeError("FreeCAD.openDocument is unavailable")
        try:
            reopened = open_document(temp_path, True)
        except TypeError:
            reopened = open_document(temp_path)
        reopened_objects = _document_objects(reopened)
        ignored_names = set(baseline_entity_names or set())
        if ignored_names:
            reopened_objects = [
                host_obj
                for host_obj in reopened_objects
                if _host_object_id(host_obj) not in ignored_names
            ]
        result = _crosscheck_host_object_inventory(inventory, reopened_objects)
        result["method"] = "temporary_fcstd_save_copy_reopen"
        return result
    except (OSError, RuntimeError, TypeError, ValueError, AttributeError) as exc:
        failure["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        return failure
    finally:
        if reopened is not None:
            reopened_name = str(getattr(reopened, "Name", "") or "")
            close_document = getattr(FreeCAD, "closeDocument", None)
            if reopened_name and callable(close_document):
                try:
                    close_document(reopened_name)
                except (RuntimeError, TypeError, ValueError, AttributeError):
                    pass
        if original_name:
            set_active = getattr(FreeCAD, "setActiveDocument", None)
            if callable(set_active):
                try:
                    set_active(original_name)
                except (RuntimeError, TypeError, ValueError, AttributeError):
                    pass
        try:
            os.remove(temp_path)
        except OSError:
            pass


def _structural_text_delivery_count(
    opts: ImportOptions,
    fallback_count: int = 0,
) -> int:
    delivered = getattr(opts, "text_delivered_counts", {}) or {}
    if isinstance(delivered, dict) and delivered:
        return sum(
            max(0, int(value or 0))
            for bucket, value in delivered.items()
            if str(bucket) != "raster_text_patch"
        )
    return max(0, int(fallback_count or 0))


def _text_host_document(shape_string, text_group):
    for candidate in (
        getattr(shape_string, "Document", None),
        getattr(text_group, "Document", None),
        getattr(FreeCAD, "ActiveDocument", None) if FreeCAD is not None else None,
    ):
        if candidate is not None:
            return candidate
    return None


def _remove_owned_text_objects(doc, text_group, owned: List[Any]) -> Tuple[List[str], bool]:
    """Remove only objects created by the current attempt, in reverse order."""
    owned_records: List[Tuple[Any, str]] = []
    for obj in owned:
        try:
            obj_id = _host_object_id(obj)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            obj_id = ""
        owned_records.append((obj, obj_id))

    removed: List[str] = []
    seen = set()
    for obj, obj_id in reversed(owned_records):
        if not obj_id or obj_id in seen:
            continue
        seen.add(obj_id)
        current = None
        try:
            current = doc.getObject(obj_id)
        except (AttributeError, RuntimeError, TypeError):
            current = next(
                (candidate for candidate in _document_objects(doc) if candidate is obj),
                None,
            )
        if current is not obj:
            continue
        try:
            remove_from_group = getattr(text_group, "removeObject", None)
            if callable(remove_from_group):
                remove_from_group(obj)
            elif hasattr(text_group, "objects") and obj in text_group.objects:
                text_group.objects.remove(obj)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        try:
            doc.removeObject(obj_id)
            removed.append(obj_id)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue

    live_ids = {_host_object_id(obj) for obj in _document_objects(doc)}
    complete = all(not obj_id or obj_id not in live_ids for _obj, obj_id in owned_records)
    return removed, complete


def _source_span_advance_fc(span: dict, line: dict, scale: float) -> float:
    """Return the source span's baseline advance in host units."""
    bbox = _span_bbox_pdf(span)
    direction = line.get("dir", (1.0, 0.0)) or (1.0, 0.0)
    try:
        dx, dy = float(direction[0]), float(direction[1])
    except (IndexError, TypeError, ValueError):
        dx, dy = 1.0, 0.0
    length = math.hypot(dx, dy)
    if length <= 1e-12:
        dx, dy, length = 1.0, 0.0, 1.0
    ux, uy = dx / length, dy / length

    # Axis-aligned source spans carry their exact advance directly in bbox.
    # recover_span_quad can expand those bounds using font metrics by ~1%,
    # which is enough to create visible drift across long table headings.
    if bbox and abs(uy) <= 1e-7:
        advance = abs(float(bbox[2]) - float(bbox[0]))
    elif bbox and abs(ux) <= 1e-7:
        advance = abs(float(bbox[3]) - float(bbox[1]))
    else:
        try:
            quad = fitz.recover_span_quad(direction, span)
            left = _xy(quad.ul)
            right = _xy(quad.ur)
            advance = math.hypot(right[0] - left[0], right[1] - left[1])
        except (AttributeError, RuntimeError, TypeError, ValueError):
            if not bbox:
                return 0.0
            x0, y0, x1, y1 = bbox
            advance = abs(ux) * abs(x1 - x0) + abs(uy) * abs(y1 - y0)
    return max(0.0, float(advance) * abs(float(scale)))


def _shape_baseline_extent(shape, angle_deg: float) -> Optional[float]:
    """Project a host shape onto its rendered text-baseline axis."""
    try:
        vertices = list(getattr(shape, "Vertexes", []) or [])
        if not vertices:
            return None
        radians = math.radians(float(angle_deg))
        ux, uy = math.cos(radians), math.sin(radians)
        projected = [
            float(vertex.Point.x) * ux + float(vertex.Point.y) * uy
            for vertex in vertices
        ]
        return max(projected) - min(projected)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None


def _annotate_text_host_object(obj, source_item_id: str, representation: str) -> None:
    """Persist stable source identity and requested representation on a host object."""
    properties = set(getattr(obj, "PropertiesList", []) or [])
    add_property = getattr(obj, "addProperty", None)
    for name, value in (
        ("PDFSourceItemId", source_item_id),
        ("PDFRepresentation", representation),
    ):
        if name not in properties and callable(add_property):
            add_property("App::PropertyString", name, "PDF Import")
            properties.add(name)
        setattr(obj, name, str(value))


def _format_color_metadata(
    source_color: Optional[Tuple[float, float, float]],
) -> str:
    """Format a source color as persisted metadata (read-only companion of
    _persist_text_style_metadata so verifiers never re-write properties)."""
    return (
        ",".join(format(float(channel), ".9g") for channel in source_color)
        if source_color is not None
        else ""
    )


def _persist_text_style_metadata(
    obj,
    *,
    font_name: str,
    font_size: float,
    source_color: Optional[Tuple[float, float, float]],
) -> str:
    """Persist source style on an App object, including in headless FreeCADCmd."""
    color_metadata = _format_color_metadata(source_color)
    properties = set(getattr(obj, "PropertiesList", []) or [])
    add_property = getattr(obj, "addProperty", None)
    for property_kind, property_name, property_value in (
        ("App::PropertyString", "PDFTextFontName", str(font_name)),
        ("App::PropertyFloat", "PDFTextFontSize", float(font_size)),
        ("App::PropertyString", "PDFTextJustification", "Left"),
        ("App::PropertyString", "PDFTextColorRGB", color_metadata),
    ):
        if property_name not in properties and callable(add_property):
            add_property(property_kind, property_name, "PDF Import")
            properties.add(property_name)
        setattr(obj, property_name, property_value)
    return color_metadata


def _make_shapestring_host(doc, source_text: str, font_path: str):
    """Create a ShapeString host WITHOUT tessellating it (perf lever P1).

    Draft.make_shapestring recomputes immediately at its default Size=100
    (draftmake/make_shapestring.py), so every span used to tessellate twice:
    once at the wrong size in the factory and again after the caller set the
    real Size. Constructing the Part::Part2DObjectPython with the Draft
    ShapeString proxy directly defers the one real tessellation to
    _create_verified_text3d_entity's recompute at final properties.

    Falls back to the Draft factory (identical semantics, slower) whenever the
    Draft internals are unavailable, so behavior never changes — only cost.
    """
    proxy_cls = None
    try:
        from draftobjects.shapestring import ShapeString as proxy_cls  # type: ignore
    except ImportError:
        proxy_cls = None
    if proxy_cls is not None and doc is not None and hasattr(doc, "addObject"):
        obj = None
        try:
            obj = doc.addObject("Part::Part2DObjectPython", "ShapeString")
            proxy_cls(obj)
            obj.String = source_text
            obj.FontFile = font_path
            obj.Tracking = 0
            if FreeCAD is not None and bool(getattr(FreeCAD, "GuiUp", False)):
                try:
                    from draftviewproviders.view_shapestring import (
                        ViewProviderShapeString,
                    )

                    ViewProviderShapeString(obj.ViewObject)
                except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
                    pass
            return obj
        except (AttributeError, RuntimeError, TypeError, ValueError):
            # Never leak a half-built host object into the document.
            if obj is not None:
                try:
                    doc.removeObject(obj.Name)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass
    make_shapestring = getattr(
        Draft,
        "make_shapestring",
        getattr(Draft, "makeShapeString", None),
    )
    if not callable(make_shapestring):
        raise AttributeError("Draft ShapeString API unavailable")
    return make_shapestring(source_text, font_path)


def _create_verified_text3d_entity(
    shape_string,
    *,
    font_size_fc: float,
    depth: float,
    target_advance_fc: float,
    baseline_angle_deg: float,
    text_group,
    baseline_object_ids: Optional[set] = None,
    configure_host=None,
):
    """Create and verify a width-calibrated ShapeString clone + extrusion."""
    try:
        protected_baseline_ids = set(baseline_object_ids or ())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("3D Text ownership baseline is invalid") from exc
    if any(type(object_id) is not int for object_id in protected_baseline_ids):
        raise RuntimeError("3D Text ownership baseline is invalid")
    if id(shape_string) in protected_baseline_ids:
        raise RuntimeError("ShapeString is a pre-existing baseline object")

    doc = _text_host_document(shape_string, text_group)
    if doc is None:
        raise RuntimeError("FreeCAD document unavailable for Part::Extrusion")

    shape_string.Size = float(font_size_fc)
    try:
        # ScaleToSize normalizes against the font cap-height and changed the
        # supplied chart's horizontal scale by ~1.8x. Preserve native font
        # units, then apply the PDF's authoritative text-matrix width below.
        shape_string.ScaleToSize = False
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    try:
        # P2 perf lever: keep the ShapeString as wires only. Draft's
        # MakeFace=True runs makeFace + Face.validate per character (the two
        # dominant costs on dense pages) and a per-execute sticky-font probe.
        # Part::Extrusion(Solid=True) builds identical solids from the wires
        # (FaceMakerCheese honors glyph counters; measured volume ratio 1.0
        # against the faces path on hole-bearing glyphs "O8ABe00").
        shape_string.MakeFace = False
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    # P1 ordering: all custom property writes must land BEFORE this object's
    # only recompute — post-recompute writes re-touch the object and force the
    # page-end doc.recompute() to tessellate every span again (measured ~2x).
    if callable(configure_host):
        configure_host(shape_string)
    try:
        doc.recompute([shape_string])
    except TypeError:
        doc.recompute()

    support_shape = getattr(shape_string, "Shape", None)
    if (
        support_shape is None
        or bool(getattr(support_shape, "isNull", lambda: True)())
        or not (
            list(getattr(support_shape, "Faces", []) or [])
            or list(getattr(support_shape, "Wires", []) or [])
        )
    ):
        raise RuntimeError("ShapeString did not produce face or wire geometry")

    native_advance = _shape_baseline_extent(support_shape, baseline_angle_deg)
    if native_advance is None or native_advance <= 1e-9:
        raise RuntimeError("ShapeString baseline extent could not be measured")
    if not math.isfinite(target_advance_fc) or target_advance_fc <= 1e-9:
        raise RuntimeError("source span baseline advance is unavailable")
    x_scale = float(target_advance_fc) / float(native_advance)
    if not math.isfinite(x_scale) or x_scale <= 1e-4 or x_scale >= 1e4:
        raise RuntimeError("source span horizontal scale is invalid")

    clone_factory = getattr(Draft, "clone", None)
    if not callable(clone_factory):
        raise RuntimeError("Draft clone API unavailable for exact text width")
    calibrated_support = clone_factory(shape_string)
    if isinstance(calibrated_support, (list, tuple)):
        calibrated_support = calibrated_support[0] if calibrated_support else None
    if calibrated_support is None:
        raise RuntimeError("Draft clone did not create calibrated text support")
    if id(calibrated_support) in protected_baseline_ids:
        raise RuntimeError("Draft clone returned a pre-existing baseline object")
    if calibrated_support is shape_string:
        raise RuntimeError("Draft clone returned the source ShapeString")
    calibrated_support.Scale = Vector(float(x_scale), 1.0, 1.0)
    try:
        calibrated_support.Label = "PDF 3D Text Calibrated Support"
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    if callable(configure_host):
        configure_host(calibrated_support)
    try:
        doc.recompute([calibrated_support])
    except TypeError:
        doc.recompute()

    calibrated_shape = getattr(calibrated_support, "Shape", None)
    if (
        calibrated_shape is None
        or bool(getattr(calibrated_shape, "isNull", lambda: True)())
        or not (
            list(getattr(calibrated_shape, "Faces", []) or [])
            or list(getattr(calibrated_shape, "Wires", []) or [])
        )
    ):
        raise RuntimeError(
            "calibrated ShapeString clone did not produce face or wire geometry"
        )
    verified_advance = _shape_baseline_extent(calibrated_shape, baseline_angle_deg)
    tolerance = max(0.05, float(target_advance_fc) * 0.03)
    if (
        verified_advance is None
        or abs(float(verified_advance) - float(target_advance_fc)) > tolerance
    ):
        raise RuntimeError(
            "calibrated ShapeString width verification failed "
            "(target %.6g, actual %s)"
            % (target_advance_fc, verified_advance)
        )

    extrusion = doc.addObject("Part::Extrusion", "PDF_3D_Text")
    if extrusion is None:
        raise RuntimeError("Part::Extrusion factory returned no host object")
    if id(extrusion) in protected_baseline_ids:
        raise RuntimeError(
            "Part::Extrusion factory returned a pre-existing baseline object"
        )
    if extrusion is shape_string or extrusion is calibrated_support:
        raise RuntimeError("Part::Extrusion factory returned an existing support object")
    extrusion.Base = calibrated_support
    try:
        extrusion.DirMode = "Custom"
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    extrusion.Dir = Vector(0.0, 0.0, float(depth))
    extrusion.Solid = True
    if callable(configure_host):
        configure_host(extrusion)
    try:
        doc.recompute([extrusion])
    except TypeError:
        doc.recompute()

    shape = getattr(extrusion, "Shape", None)
    solids = list(getattr(shape, "Solids", []) or []) if shape is not None else []
    volume = float(getattr(shape, "Volume", 0.0) or 0.0) if shape is not None else 0.0
    if (
        str(getattr(extrusion, "TypeId", "")) != "Part::Extrusion"
        or getattr(extrusion, "Base", None) is not calibrated_support
        or not bool(getattr(extrusion, "Solid", False))
        or shape is None
        or bool(getattr(shape, "isNull", lambda: True)())
        or not solids
        or volume <= 0.0
    ):
        raise RuntimeError("Part::Extrusion did not produce verified solid 3D text")

    for support in (shape_string, calibrated_support):
        app_visibility_verified = False
        try:
            if "Visibility" in set(getattr(support, "PropertiesList", []) or []):
                support.Visibility = False
                app_visibility_verified = getattr(support, "Visibility", None) is False
        except (AttributeError, RuntimeError, TypeError, ValueError):
            app_visibility_verified = False
        view = getattr(support, "ViewObject", None)
        view_visibility_verified = False
        if view is not None:
            try:
                view.Visibility = False
                view_visibility_verified = getattr(view, "Visibility", None) is False
            except (AttributeError, RuntimeError, TypeError, ValueError):
                view_visibility_verified = False
        if not app_visibility_verified and not view_visibility_verified:
            raise RuntimeError("3D Text support visibility could not be disabled")
    text_group.addObject(shape_string)
    text_group.addObject(calibrated_support)
    text_group.addObject(extrusion)
    return extrusion, calibrated_support, x_scale, float(verified_advance)


def _host_text_rotation_deg(obj) -> Optional[float]:
    """Read the rendered host baseline rotation without trusting source input."""
    try:
        if str(getattr(obj, "TypeId", "") or "") == "App::Annotation" and hasattr(
            obj, "Position"
        ):
            return 0.0
        rotation = obj.Placement.Rotation
        mult_vec = getattr(rotation, "multVec", None)
        if callable(mult_vec) and Vector is not None:
            direction = mult_vec(Vector(1.0, 0.0, 0.0))
            angle = math.degrees(math.atan2(float(direction.y), float(direction.x)))
            return angle if math.isfinite(angle) else None
        if hasattr(rotation, "angle"):
            angle = float(rotation.angle)
            return angle if math.isfinite(angle) else None
        if hasattr(rotation, "Angle"):
            angle = math.degrees(float(rotation.Angle))
            axis = getattr(rotation, "Axis", None)
            if axis is not None and float(getattr(axis, "z", 1.0)) < 0.0:
                angle = -angle
            return angle if math.isfinite(angle) else None
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    return None


def _host_anchor_xyz(obj) -> Optional[Tuple[float, float, float]]:
    """Read a host object's rendered anchor as a finite XYZ tuple."""
    try:
        if str(getattr(obj, "TypeId", "") or "") == "App::Annotation" and hasattr(
            obj, "Position"
        ):
            base = obj.Position
        else:
            base = obj.Placement.Base
        coordinates = tuple(
            float(
                getattr(base, lower)
                if hasattr(base, lower)
                else getattr(base, lower.upper())
            )
            for lower in ("x", "y", "z")
        )
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    return coordinates if all(math.isfinite(value) for value in coordinates) else None


def _rotation_matches(actual: float, expected: float, tolerance: float = 1e-6) -> bool:
    try:
        difference = (float(actual) - float(expected) + 180.0) % 360.0 - 180.0
    except (TypeError, ValueError):
        return False
    return math.isfinite(difference) and abs(difference) <= float(tolerance)


def _deliver_text_item_native(
    item: Dict[str, Any],
    attempted_type: str,
    opts: ImportOptions,
    *,
    text_group,
    page_h: float,
    scale: float,
) -> Dict[str, Any]:
    """Create and reread one exact native Text or Draft Label source item."""
    try:
        bound_item = copy.deepcopy(item) if isinstance(item, dict) else {}
    except Exception:
        bound_item = {}
    source_item_id = str(bound_item.get("source_item_id") or "")
    requested_type = bound_item.get("requested_type")
    doc = _text_host_document(None, text_group)
    baseline_objects: set = set()
    owned: List[Any] = []

    def fail(reason: str, evidence: Optional[Dict[str, Any]] = None):
        if doc is not None and baseline_objects:
            try:
                for host_obj in _document_objects(doc):
                    if id(host_obj) not in baseline_objects and all(
                        candidate is not host_obj for candidate in owned
                    ):
                        owned.append(host_obj)
            except Exception:
                pass
        created_ids = [_host_object_id(host_obj) for host_obj in owned]
        removed_ids: List[str] = []
        cleanup_complete = not owned
        if doc is not None and owned:
            try:
                removed_ids, cleanup_complete = _remove_owned_text_objects(
                    doc, text_group, owned
                )
            except Exception:
                cleanup_complete = False
        attempt = {
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "attempted_type": attempted_type,
            "final_type": None,
            "outcome": "failed",
            "reason": str(reason),
            "created_entity_ids": created_ids,
            "removed_entity_ids": removed_ids,
            "cleanup_complete": bool(
                cleanup_complete and set(created_ids) == set(removed_ids)
            ) if created_ids else bool(cleanup_complete and not removed_ids),
            "evidence": dict(evidence or {}),
        }
        raise TextRepresentationFailure(
            "%s failed for %s: %s"
            % (attempted_type, source_item_id or "item", reason),
            attempt,
        )

    try:
        page_number = bound_item.get("page_number")
        indices = (
            bound_item.get("block_index"),
            bound_item.get("line_index"),
            bound_item.get("span_index"),
        )
        expected_source_id = "p%d:b%d:l%d:s%d" % ((page_number,) + indices)
        source_text = bound_item.get("text")
        span = bound_item.get("span")
        pdf_sha256 = bound_item.get("pdf_sha256")
        bbox = _finite_source_tuple(bound_item.get("bbox"), 4, "item.bbox")
        origin = _finite_source_tuple(bound_item.get("origin"), 2, "item.origin")
        direction = _finite_source_tuple(
            bound_item.get("line_direction"), 2, "item.line_direction"
        )
        canonical_rotation_deg = float(bound_item.get("rotation_deg"))
        authoritative_rotation_deg = _line_angle_deg({"dir": direction})
        if (
            attempted_type not in {"text", "labels"}
            or requested_type not in TEXT_ITEM_FALLBACK_LADDERS
            or attempted_type not in TEXT_ITEM_FALLBACK_LADDERS[requested_type]
            or bound_item.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY
            or type(page_number) is not int
            or page_number <= 0
            or any(type(index) is not int or index < 0 for index in indices)
            or source_item_id != expected_source_id
            or not isinstance(pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
            or not isinstance(source_text, str)
            or not source_text
            or source_text.isspace()
            or not isinstance(span, dict)
            or span.get("text") != source_text
            or bbox != _finite_source_tuple(span.get("bbox"), 4, "span.bbox")
            or origin != _finite_source_tuple(span.get("origin"), 2, "span.origin")
            or math.hypot(direction[0], direction[1]) <= 1e-12
            or not math.isfinite(canonical_rotation_deg)
            or not _rotation_matches(
                canonical_rotation_deg, authoritative_rotation_deg
            )
            or doc is None
            or text_group is None
            or Vector is None
            or Placement is None
            or Rotation is None
            or not math.isfinite(float(page_h))
            or not math.isfinite(float(scale))
            or float(scale) <= 0.0
        ):
            raise ValueError("canonical native text delivery context is invalid")
        if attempted_type in {"text", "labels"} and Draft is None:
            raise ValueError("FreeCAD Draft is unavailable for native Text/Labels")
        baseline_objects = {id(host_obj) for host_obj in _document_objects(doc)}
    except Exception as exc:
        fail(
            "invalid_native_text_source_item",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        from pdfcadcore.text_scale import effective_span_font_size_pt

        host_rotation_deg = _line_angle_deg({"dir": direction}, opts)
        size_pt = float(effective_span_font_size_pt(span, host_rotation_deg))
        if not math.isfinite(size_pt) or size_pt <= 0.0:
            raise ValueError("source font size is unavailable")
        font_size_fc = _fit_font_size_to_span_bbox(
            source_text,
            size_pt * float(scale),
            span,
            float(scale),
            host_rotation_deg,
        )
        if not math.isfinite(font_size_fc) or font_size_fc <= 0.0:
            raise ValueError("native text font size is invalid")
        anchor = _to_fc(origin, float(page_h), opts, float(scale))
        try:
            descender = float(span.get("descender", -0.2) or -0.2)
        except (TypeError, ValueError):
            descender = -0.2
        anchor = _apply_text_local_y_offset(
            anchor,
            host_rotation_deg,
            _effective_descender(source_text, descender) * font_size_fc * 0.35,
        )
        placement = Placement(
            anchor,
            Rotation(Vector(0.0, 0.0, 1.0), host_rotation_deg),
        )
    except Exception as exc:
        fail(
            "native_text_transform_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        if attempted_type == "text":
            host_obj = Draft.make_text([source_text], placement=placement)
            text_property = "Text"
            expected_type = "App::FeaturePython"
            expected_proxy_type = "Text"
        else:
            host_obj = Draft.make_label(
                target_point=anchor,
                placement=placement,
                label_type="Custom",
                custom_text=source_text,
                direction="Custom",
                points=[anchor, anchor],
            )
            text_property = "Text"
            expected_type = "App::FeaturePython"
            expected_proxy_type = "Label"
        if host_obj is None or id(host_obj) in baseline_objects:
            raise RuntimeError("native text factory returned no new host object")
        owned.append(host_obj)
        text_group.addObject(host_obj)
        _annotate_text_host_object(host_obj, source_item_id, attempted_type)

        normalized_font = _normalize_pdf_font_name(span.get("font", ""))
        source_color = _span_source_color(span)
        color_metadata = (
            ",".join(format(float(channel), ".9g") for channel in source_color)
            if source_color is not None
            else ""
        )
        properties = set(getattr(host_obj, "PropertiesList", []) or [])
        add_property = getattr(host_obj, "addProperty", None)
        for property_kind, property_name, property_value in (
            ("App::PropertyString", "PDFTextFontName", normalized_font),
            ("App::PropertyFloat", "PDFTextFontSize", float(font_size_fc)),
            ("App::PropertyString", "PDFTextJustification", "Left"),
            ("App::PropertyString", "PDFTextColorRGB", color_metadata),
        ):
            if property_name not in properties and callable(add_property):
                add_property(property_kind, property_name, "PDF Import")
                properties.add(property_name)
            setattr(host_obj, property_name, property_value)

        # FreeCADCmd intentionally has no GUI view provider. Persist and reread
        # the complete style contract on the document object in every host, then
        # additionally require the real view-provider style when one exists.
        view = getattr(host_obj, "ViewObject", None)
        font_properties = []
        color_properties = []
        if view is not None:
            view.FontSize = float(font_size_fc)
            for property_name in ("FontName", "Font"):
                if hasattr(view, property_name):
                    setattr(view, property_name, normalized_font)
                    font_properties.append(property_name)
            if not font_properties:
                raise RuntimeError("native host exposes no writable font property")
            if hasattr(view, "Justification"):
                view.Justification = "Left"
            if source_color is not None:
                for property_name in (
                    "TextColor", "ShapeColor", "LineColor", "PointColor"
                ):
                    if hasattr(view, property_name):
                        setattr(view, property_name, source_color)
                        color_properties.append(property_name)
                if not color_properties:
                    raise RuntimeError("native host exposes no writable color property")
        doc.recompute()
    except Exception as exc:
        fail(
            "native_text_creation_or_style_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        entity_id = _host_object_id(host_obj)
        live_object = doc.getObject(entity_id)
        actual_text = getattr(host_obj, text_property, None)
        actual_text_values = (
            list(actual_text)
            if isinstance(actual_text, (list, tuple))
            else [str(actual_text or "")]
        )
        actual_anchor = _host_anchor_xyz(host_obj)
        actual_rotation = _host_text_rotation_deg(host_obj)
        actual_proxy_type = str(
            getattr(getattr(host_obj, "Proxy", None), "Type", "") or ""
        )
        actual_custom_text = getattr(host_obj, "CustomText", None)
        actual_custom_text_values = (
            list(actual_custom_text)
            if isinstance(actual_custom_text, (list, tuple))
            else [str(actual_custom_text or "")]
        )
        metadata_font_name = str(getattr(host_obj, "PDFTextFontName", "") or "")
        metadata_font_size = float(host_obj.PDFTextFontSize)
        metadata_justification = str(
            getattr(host_obj, "PDFTextJustification", "") or ""
        )
        metadata_color = str(getattr(host_obj, "PDFTextColorRGB", "") or "")
        if view is None:
            actual_font_size = metadata_font_size
            actual_fonts = [metadata_font_name]
            actual_justification = metadata_justification
            color_verified = metadata_color == color_metadata
            style_verification = "headless_app_metadata"
            view_style_verified = False
        else:
            actual_font_size = float(view.FontSize)
            actual_fonts = [
                str(getattr(view, property_name) or "")
                for property_name in font_properties
            ]
            actual_justification = (
                str(view.Justification or "")
                if hasattr(view, "Justification")
                else "Left"
            )
            color_verified = source_color is None or any(
                isinstance(getattr(view, property_name, None), (tuple, list))
                and len(getattr(view, property_name)) >= 3
                and all(
                    abs(
                        float(getattr(view, property_name)[index])
                        - source_color[index]
                    )
                    <= 1e-6
                    for index in range(3)
                )
                for property_name in (
                    "TextColor", "ShapeColor", "LineColor", "PointColor"
                )
            )
            style_verification = "gui_view_and_app_metadata"
            view_style_verified = True
        expected_anchor = (float(anchor.x), float(anchor.y), float(anchor.z))
        if (
            not entity_id
            or live_object is not host_obj
            or host_obj not in _document_objects(doc)
            or (expected_type and str(getattr(host_obj, "TypeId", "")) != expected_type)
            or actual_proxy_type != expected_proxy_type
            or actual_text_values != [source_text]
            or (
                attempted_type == "labels"
                and actual_custom_text_values != [source_text]
            )
            or getattr(host_obj, "PDFSourceItemId", None) != source_item_id
            or getattr(host_obj, "PDFRepresentation", None) != attempted_type
            or actual_anchor is None
            or any(
                abs(actual_anchor[index] - expected_anchor[index]) > 1e-7
                for index in range(3)
            )
            or actual_rotation is None
            or not _rotation_matches(actual_rotation, host_rotation_deg)
            or not math.isclose(actual_font_size, font_size_fc, abs_tol=1e-7)
            or normalized_font not in actual_fonts
            or actual_justification != "Left"
            or metadata_font_name != normalized_font
            or not math.isclose(metadata_font_size, font_size_fc, abs_tol=1e-7)
            or metadata_justification != "Left"
            or metadata_color != color_metadata
            or not color_verified
        ):
            raise RuntimeError("native text host evidence could not be verified")
    except Exception as exc:
        fail(
            "native_text_host_verification_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    delivery_evidence = {
        "host_entity_type": str(getattr(host_obj, "TypeId", "") or ""),
        "host_proxy_type": actual_proxy_type,
        "source_text": source_text,
        "source_text_preserved": True,
        "source_item_id_verified": True,
        "expected_anchor_xyz": expected_anchor,
        "verified_anchor_xyz": tuple(actual_anchor),
        "rotation_deg": float(actual_rotation),
        "font_name": normalized_font,
        "font_size": float(actual_font_size),
        "source_color": source_color,
        "color_verified": bool(color_verified),
        "style_verification": style_verification,
        "view_style_verified": bool(view_style_verified),
    }
    if attempted_type == "labels":
        # A coincident Draft Label leader can still display a visible marker.
        # Until the reopened GUI proves that marker absent, the report must
        # remain pending instead of treating placement metadata as appearance.
        delivery_evidence.update(
            {
                "label_marker_absent": False,
                "label_marker_verification": "pending",
            }
        )

    return {
        "source_item_id": source_item_id,
        "requested_type": requested_type,
        "attempted_type": attempted_type,
        "final_type": attempted_type,
        "outcome": "verified",
        "created_entity_ids": [entity_id],
        "delivery_entity_ids": [entity_id],
        "support_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "delivery_count": 1,
        "evidence": delivery_evidence,
    }


def _raster_asset_dir() -> Path:
    """Persistent, content-addressed raster assets owned by this importer."""
    try:
        root = Path(FreeCAD.getUserAppDataDir())
        return root / "Mod" / "PDFVectorImporter" / "raster_cache"
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return Path(tempfile.gettempdir()) / "bc_fc_pdf_raster_cache"


def _save_pixmap_atomic(pix, image_path: Path) -> None:
    """Publish a complete raster atomically so concurrent imports cannot tear it."""
    image_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=image_path.stem + ".",
        suffix=".png",
        dir=str(image_path.parent),
    )
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        pix.save(str(temporary_path))
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise RuntimeError("raster renderer produced an empty temporary asset")
        for attempt_index in range(8):
            try:
                os.replace(str(temporary_path), str(image_path))
                break
            except PermissionError:
                if attempt_index >= 7:
                    raise
                # Windows can briefly deny replacement while another importer
                # publishes the same content-addressed key. Keep retries bounded.
                time.sleep(0.005 * (attempt_index + 1))
    finally:
        try:
            if temporary_path.exists():
                temporary_path.unlink()
        except OSError:
            pass


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _raster_file_evidence(host_obj, source_asset_path: Path) -> Dict[str, Any]:
    """Verify both FreeCAD raster-file properties by content, not cache pathname."""
    source_path = Path(source_asset_path)
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        raise RuntimeError("persistent source raster asset is unavailable")

    source_sha256 = _path_sha256(source_path)
    evidence: Dict[str, Any] = {
        "source_asset_path": str(source_path),
        "source_asset_sha256": source_sha256,
    }
    for property_name, evidence_name in (
        ("ImageFile", "image_file_path"),
        ("PDFRasterFile", "included_file_path"),
    ):
        actual_path = Path(str(getattr(host_obj, property_name, "") or ""))
        if not actual_path.is_file() or actual_path.stat().st_size <= 0:
            raise RuntimeError("%s raster file is unavailable" % property_name)
        actual_sha256 = _path_sha256(actual_path)
        if actual_sha256 != source_sha256:
            raise RuntimeError("%s raster content does not match source" % property_name)
        evidence[evidence_name] = str(actual_path)
        evidence[evidence_name + "_sha256"] = actual_sha256
    evidence["raster_content_verified"] = True
    return evidence


def _deliver_text_item_raster(
    item: Dict[str, Any],
    attempted_type: str,
    opts: ImportOptions,
    *,
    page,
    page_h: float,
    scale: float,
    fc_doc,
    parent_group,
) -> Dict[str, Any]:
    """Render one exact source span to a persistent verified ImagePlane patch."""
    try:
        bound_item = copy.deepcopy(item) if isinstance(item, dict) else {}
    except Exception:
        bound_item = {}
    source_item_id = str(bound_item.get("source_item_id") or "")
    requested_type = bound_item.get("requested_type")
    owned: List[Any] = []
    baseline_ids: set = set()

    def fail(reason: str, evidence: Optional[Dict[str, Any]] = None):
        if fc_doc is not None and baseline_ids:
            for host_obj in _document_objects(fc_doc):
                if id(host_obj) not in baseline_ids and all(
                    candidate is not host_obj for candidate in owned
                ):
                    owned.append(host_obj)
        created_ids = [_host_object_id(host_obj) for host_obj in owned]
        removed_ids: List[str] = []
        cleanup_complete = not owned
        if owned:
            try:
                removed_ids, cleanup_complete = _remove_owned_text_objects(
                    fc_doc, parent_group, owned
                )
            except Exception:
                cleanup_complete = False
        attempt = {
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "attempted_type": attempted_type,
            "final_type": None,
            "outcome": "failed",
            "reason": str(reason),
            "created_entity_ids": created_ids,
            "removed_entity_ids": removed_ids,
            "cleanup_complete": bool(
                cleanup_complete and set(created_ids) == set(removed_ids)
            ) if created_ids else bool(cleanup_complete and not removed_ids),
            "evidence": dict(evidence or {}),
        }
        raise TextRepresentationFailure(
            "Raster delivery failed for %s: %s"
            % (source_item_id or "item", reason),
            attempt,
        )

    try:
        page_number = bound_item.get("page_number")
        indices = (
            bound_item.get("block_index"),
            bound_item.get("line_index"),
            bound_item.get("span_index"),
        )
        expected_source_id = "p%d:b%d:l%d:s%d" % ((page_number,) + indices)
        bbox = _finite_source_tuple(bound_item.get("bbox"), 4, "item.bbox")
        pdf_sha256 = bound_item.get("pdf_sha256")
        if (
            attempted_type != "raster"
            or requested_type not in TEXT_ITEM_FALLBACK_LADDERS
            or TEXT_ITEM_FALLBACK_LADDERS[requested_type][-1] != "raster"
            or bound_item.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY
            or source_item_id != expected_source_id
            or not isinstance(pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
            or bbox[2] <= bbox[0]
            or bbox[3] <= bbox[1]
            or page is None
            or fc_doc is None
            or parent_group is None
            or Vector is None
            or Placement is None
            or Rotation is None
            or not math.isfinite(float(page_h))
            or not math.isfinite(float(scale))
            or float(scale) <= 0.0
        ):
            raise ValueError("canonical raster item delivery context is invalid")
        baseline_ids = {id(host_obj) for host_obj in _document_objects(fc_doc)}
    except Exception as exc:
        fail(
            "invalid_raster_text_source_item",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        dpi = max(72, int(getattr(opts, "raster_dpi", 200) or 200))
        clip = fitz.Rect(*bbox)
        page_rect = page.rect
        clip &= fitz.Rect(
            float(page_rect.x0),
            float(page_rect.y0),
            float(page_rect.x1),
            float(page_rect.y1),
        )
        if clip.is_empty or clip.width <= 0.0 or clip.height <= 0.0:
            raise ValueError("source item raster clip is empty")
        zoom = dpi / 72.0
        pix = page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            clip=clip,
            alpha=True,
        )
        if int(getattr(pix, "width", 0) or 0) <= 0 or int(
            getattr(pix, "height", 0) or 0
        ) <= 0:
            raise RuntimeError("source item raster contains no pixels")
        cache_key = hashlib.sha256(
            (
                "%s|%d|%s|%.9g,%.9g,%.9g,%.9g|%d"
                % (
                    bound_item.get("pdf_sha256"),
                    page_number,
                    source_item_id,
                    clip.x0,
                    clip.y0,
                    clip.x1,
                    clip.y1,
                    dpi,
                )
            ).encode("utf-8")
        ).hexdigest()
        asset_dir = _raster_asset_dir()
        image_path = asset_dir / ("text_%s.png" % cache_key)
        _save_pixmap_atomic(pix, image_path)
        if not image_path.is_file() or image_path.stat().st_size <= 0:
            raise RuntimeError("source item raster was not persisted")
        raster_sha256 = _path_sha256(image_path)
    except Exception as exc:
        fail(
            "raster_text_render_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        transformed = [
            _to_fc(point, float(page_h), opts, float(scale))
            for point in (
                (clip.x0, clip.y0),
                (clip.x0, clip.y1),
                (clip.x1, clip.y0),
                (clip.x1, clip.y1),
            )
        ]
        xs = [float(point.x) for point in transformed]
        ys = [float(point.y) for point in transformed]
        expected_width = max(xs) - min(xs)
        expected_height = max(ys) - min(ys)
        expected_anchor = (min(xs), min(ys), -0.05)
        if expected_width <= 0.0 or expected_height <= 0.0:
            raise ValueError("source item raster placement has no area")
        host_obj = fc_doc.addObject("Image::ImagePlane", "PDF_Text_Raster")
        if host_obj is None or id(host_obj) in baseline_ids:
            raise RuntimeError("ImagePlane factory returned no new host object")
        owned.append(host_obj)
        host_obj.ImageFile = str(image_path)
        host_obj.XSize = float(expected_width)
        host_obj.YSize = float(expected_height)
        host_obj.Placement = Placement(
            Vector(*expected_anchor),
            Rotation(),
        )
        add_property = getattr(host_obj, "addProperty", None)
        if not callable(add_property):
            raise RuntimeError("ImagePlane cannot embed its raster asset")
        if "PDFRasterFile" not in set(getattr(host_obj, "PropertiesList", []) or []):
            add_property("App::PropertyFileIncluded", "PDFRasterFile", "PDF Import")
        host_obj.PDFRasterFile = str(image_path)
        if "PDFSourceSHA256" not in set(
            getattr(host_obj, "PropertiesList", []) or []
        ):
            add_property("App::PropertyString", "PDFSourceSHA256", "PDF Import")
        host_obj.PDFSourceSHA256 = pdf_sha256
        if "PDFRasterSHA256" not in set(
            getattr(host_obj, "PropertiesList", []) or []
        ):
            add_property("App::PropertyString", "PDFRasterSHA256", "PDF Import")
        host_obj.PDFRasterSHA256 = raster_sha256
        _annotate_text_host_object(host_obj, source_item_id, "raster")
        parent_group.addObject(host_obj)
        fc_doc.recompute()
    except Exception as exc:
        fail(
            "raster_text_host_creation_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        entity_id = _host_object_id(host_obj)
        actual_anchor = _host_anchor_xyz(host_obj)
        raster_file_evidence = _raster_file_evidence(host_obj, image_path)
        if (
            not entity_id
            or fc_doc.getObject(entity_id) is not host_obj
            or str(getattr(host_obj, "TypeId", "")) != "Image::ImagePlane"
            or str(getattr(host_obj, "PDFSourceSHA256", "")) != pdf_sha256
            or str(getattr(host_obj, "PDFRasterSHA256", ""))
            != raster_file_evidence["source_asset_sha256"]
            or not math.isclose(float(host_obj.XSize), expected_width, abs_tol=1e-7)
            or not math.isclose(float(host_obj.YSize), expected_height, abs_tol=1e-7)
            or actual_anchor is None
            or any(
                abs(actual_anchor[index] - expected_anchor[index]) > 1e-7
                for index in range(3)
            )
            or getattr(host_obj, "PDFSourceItemId", None) != source_item_id
            or getattr(host_obj, "PDFRepresentation", None) != "raster"
        ):
            raise RuntimeError("raster text host evidence could not be verified")
    except Exception as exc:
        fail(
            "raster_text_host_verification_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    return {
        "source_item_id": source_item_id,
        "requested_type": requested_type,
        "attempted_type": "raster",
        "final_type": "raster",
        "outcome": "verified",
        "created_entity_ids": [entity_id],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "evidence": {
            "host_entity_type": "Image::ImagePlane",
            "raster_file": str(image_path),
            "raster_file_included": True,
            "pdf_sha256": pdf_sha256,
            "pixel_width": int(pix.width),
            "pixel_height": int(pix.height),
            "dpi": dpi,
            "source_bbox": bbox,
            "expected_anchor_xyz": expected_anchor,
            "verified_anchor_xyz": tuple(actual_anchor),
            "x_size": float(expected_width),
            "y_size": float(expected_height),
            **raster_file_evidence,
        },
    }


def _deliver_text_item_3d(
    item: Dict[str, Any],
    attempted_type: str,
    opts: ImportOptions,
    *,
    text_group,
    page_h: float,
    scale: float,
) -> Dict[str, Any]:
    """Deliver and verify exactly one canonical 3D Text source item."""
    try:
        bound_item = copy.deepcopy(item) if isinstance(item, dict) else {}
    except Exception:
        bound_item = {}
    source_item_id = str(bound_item.get("source_item_id") or "")
    requested_type = bound_item.get("requested_type")
    owned: List[Any] = []
    doc = None
    baseline_objects = set()
    baseline_valid = False
    creation_started = False
    ownership_collection_failed = False
    ownership_collection_error = ""

    def add_owned(obj) -> None:
        if obj is not None and baseline_valid and id(obj) in baseline_objects:
            raise RuntimeError("host factory returned a pre-existing baseline object")
        if obj is not None and not any(candidate is obj for candidate in owned):
            owned.append(obj)

    def collect_owned() -> Optional[str]:
        nonlocal ownership_collection_failed, ownership_collection_error
        if doc is None:
            return None
        if not baseline_valid:
            return "baseline object snapshot is unavailable"
        if ownership_collection_failed:
            return ownership_collection_error or "prior ownership collection failed"
        try:
            current_objects = list(getattr(doc, "Objects", []) or [])
        except Exception as exc:
            error = "%s: %s" % (exc.__class__.__name__, exc)
            if creation_started:
                ownership_collection_failed = True
                ownership_collection_error = error
            return error
        for host_obj in current_objects:
            if id(host_obj) not in baseline_objects:
                add_owned(host_obj)
        return None

    def owned_ids() -> Tuple[List[str], bool]:
        entity_ids: List[str] = []
        complete = True
        for host_obj in owned:
            try:
                entity_id = _host_object_id(host_obj)
            except Exception:
                entity_id = ""
            if not entity_id or entity_id in entity_ids:
                complete = False
                continue
            entity_ids.append(entity_id)
        if len(entity_ids) != len(owned):
            complete = False
        return entity_ids, complete

    def terminal_failure(
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ):
        collection_error = None
        if baseline_valid and creation_started:
            collection_error = collect_owned()
        created_ids, ids_complete = owned_ids()
        removed_ids: List[str] = []
        helper_complete = not owned
        cleanup_error = ""
        if doc is not None and owned:
            try:
                removed_ids, helper_complete = _remove_owned_text_objects(
                    doc, text_group, owned
                )
            except Exception as exc:
                cleanup_error = "%s: %s" % (exc.__class__.__name__, exc)
                helper_complete = False
        live_objects: List[Any] = []
        owned_still_live = False
        unknown_post_baseline: List[Any] = []
        if baseline_valid and creation_started:
            try:
                live_objects = list(getattr(doc, "Objects", []) or []) if doc is not None else []
                owned_still_live = any(
                    candidate is host_obj
                    for candidate in live_objects
                    for host_obj in owned
                )
                known_owned = {id(host_obj) for host_obj in owned}
                unknown_post_baseline = [
                    candidate
                    for candidate in live_objects
                    if id(candidate) not in baseline_objects
                    and id(candidate) not in known_owned
                ]
            except Exception as exc:
                live_objects = []
                owned_still_live = True
                cleanup_error = cleanup_error or "%s: %s" % (
                    exc.__class__.__name__, exc
                )
        unknown_ids: List[str] = []
        for host_obj in unknown_post_baseline:
            try:
                entity_id = _host_object_id(host_obj)
            except Exception:
                entity_id = ""
            unknown_ids.append(entity_id or "unidentified_host_object_%d" % id(host_obj))
        cleanup_complete = bool(
            helper_complete
            and ids_complete
            and not owned_still_live
            and set(removed_ids) == set(created_ids)
            and not ownership_collection_failed
            and not unknown_ids
        )
        failure_evidence = dict(evidence or {})
        effective_collection_error = ownership_collection_error or collection_error
        if effective_collection_error:
            failure_evidence["ownership_collection_error"] = effective_collection_error
        if unknown_ids:
            failure_evidence["unknown_post_baseline_entity_ids"] = unknown_ids
        if cleanup_error:
            failure_evidence["cleanup_error"] = cleanup_error
        attempt = {
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "attempted_type": attempted_type,
            "final_type": None,
            "outcome": "failed",
            "reason": str(reason),
            "created_entity_ids": created_ids,
            "removed_entity_ids": removed_ids,
            "cleanup_complete": cleanup_complete,
            "evidence": failure_evidence,
        }
        raise TextRepresentationFailure(
            "3D Text failed for %s: %s" % (source_item_id or "item", reason),
            attempt,
        )

    if (
        attempted_type != "3d_text"
        or requested_type not in TEXT_ITEM_FALLBACK_LADDERS
        or attempted_type not in TEXT_ITEM_FALLBACK_LADDERS[requested_type]
    ):
        terminal_failure(
            "unsupported_3d_text_attempt",
            {
                "accepted_attempted_type": "3d_text",
                "accepted_requested_types": sorted(TEXT_ITEM_FALLBACK_LADDERS),
            },
        )

    try:
        page_number = bound_item.get("page_number")
        block_index = bound_item.get("block_index")
        line_index = bound_item.get("line_index")
        span_index = bound_item.get("span_index")
        expected_source_id = "p%d:b%d:l%d:s%d" % (
            page_number, block_index, line_index, span_index
        )
        pdf_sha256 = bound_item.get("pdf_sha256")
        source_text = bound_item.get("text")
        font_identity = bound_item.get("font_identity")
        span = bound_item.get("span")
        if (
            bound_item.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY
            or type(page_number) is not int
            or page_number <= 0
            or any(
                type(index) is not int or index < 0
                for index in (block_index, line_index, span_index)
            )
            or source_item_id != expected_source_id
            or not isinstance(pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
            or not isinstance(source_text, str)
            or not source_text
            or source_text.isspace()
            or not isinstance(font_identity, dict)
            or not isinstance(span, dict)
            or span.get("text") != source_text
        ):
            raise ValueError("canonical item identity is incomplete")
        source_font = span.get("font")
        if (
            not isinstance(source_font, str)
            or font_identity != _canonical_font_identity(source_font)
        ):
            raise ValueError("canonical item font identity does not match its span")
        bbox = _finite_source_tuple(bound_item.get("bbox"), 4, "item.bbox")
        origin = _finite_source_tuple(bound_item.get("origin"), 2, "item.origin")
        line_direction = _finite_source_tuple(
            bound_item.get("line_direction"), 2, "item.line_direction"
        )
        if (
            not isinstance(bound_item.get("bbox"), tuple)
            or not isinstance(bound_item.get("origin"), tuple)
            or not isinstance(bound_item.get("line_direction"), tuple)
            or bbox != _finite_source_tuple(span.get("bbox"), 4, "span.bbox")
            or origin != _finite_source_tuple(span.get("origin"), 2, "span.origin")
            or math.hypot(line_direction[0], line_direction[1]) <= 1e-12
        ):
            raise ValueError("canonical item placement does not match its span")
        rotation_deg = float(bound_item.get("rotation_deg"))
        authoritative_rotation = _line_angle_deg({"dir": line_direction})
        if (
            not math.isfinite(rotation_deg)
            or not _rotation_matches(rotation_deg, authoritative_rotation)
        ):
            raise ValueError("canonical item rotation does not match line direction")
    except Exception as exc:
        terminal_failure(
            "invalid_3d_text_source_item",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        font_path, source_results = _resolve_shapestring_font_path_with_evidence(
            font_identity["raw_name"],
            opts,
            pdf_sha256=pdf_sha256,
            page_number=page_number,
        )
        source_results = copy.deepcopy(source_results)
    except Exception as exc:
        terminal_failure(
            "exact_font_resolution_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    valid_result_list = bool(
        isinstance(source_results, list)
        and 1 <= len(source_results) <= 2
        and all(isinstance(result, dict) for result in source_results)
        and [result.get("source") for result in source_results]
        in (["embedded_font"], ["embedded_font", "system_font"])
        and all(
            result.get("font_identity") == font_identity
            and result.get("pdf_sha256") == pdf_sha256
            and result.get("page_number") == page_number
            and result.get("staging_complete") is True
            for result in source_results
        )
    )
    if not valid_result_list:
        terminal_failure(
            "exact_font_resolution_invalid",
            {"attempted_source_results": source_results},
        )

    exhaustive_absence = bool(
        font_path is None
        and len(source_results) == 2
        and [result.get("source") for result in source_results]
        == ["embedded_font", "system_font"]
        and all(result.get("outcome") == "not_found" for result in source_results)
    )
    if exhaustive_absence:
        attempt = {
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "attempted_type": "3d_text",
            "final_type": None,
            "outcome": "proven_impossible",
            "reason_code": "exact_font_unavailable",
            "created_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
        }
        proof = {
            "item_specific_proven_impossible": True,
            "importer_identity": FREECAD_TEXT_IMPORTER_IDENTITY,
            "pdf_sha256": pdf_sha256,
            "page_number": page_number,
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "attempted_type": "3d_text",
            "reason_code": "exact_font_unavailable",
            "font_identity": dict(font_identity),
            "evidence": {
                "normalized_key": font_identity["normalized_key"],
                "exact_sources": ["embedded_font", "system_font"],
                "exhaustive": True,
            },
            "attempted_source_results": source_results,
            "attempted_sources_complete": True,
            "created_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
        }
        raise TextItemImpossible(
            "Exact source font is unavailable for %s" % source_item_id,
            attempt=attempt,
            proof=proof,
        )

    found_results = [
        result for result in source_results if result.get("outcome") == "found"
    ]
    if (
        not isinstance(font_path, str)
        or not font_path
        or font_path != font_path.strip()
        or len(found_results) != 1
        or source_results[-1] is not found_results[0]
        or found_results[0].get("path") != font_path
        or any(
            result.get("outcome") not in {"not_found", "found"}
            for result in source_results
        )
    ):
        terminal_failure(
            "exact_font_resolution_invalid",
            {"attempted_source_results": source_results},
        )
    font_source_result = copy.deepcopy(found_results[0])

    if (
        Draft is None
        or Vector is None
        or Placement is None
        or Rotation is None
        or text_group is None
    ):
        terminal_failure(
            "freecad_3d_text_host_unavailable",
            {"attempted_source_results": source_results},
        )
    doc = _text_host_document(None, text_group)
    if doc is None:
        terminal_failure(
            "freecad_3d_text_document_unavailable",
            {"attempted_source_results": source_results},
        )
    try:
        baseline_objects = {
            id(host_obj) for host_obj in list(getattr(doc, "Objects", []) or [])
        }
        baseline_valid = True
    except Exception as exc:
        terminal_failure(
            "freecad_3d_text_document_snapshot_failed",
            {
                "attempted_source_results": source_results,
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )

    try:
        page_height = float(page_h)
        item_scale = float(scale)
        if (
            not math.isfinite(page_height)
            or not math.isfinite(item_scale)
            or item_scale <= 0.0
        ):
            raise ValueError("page height and scale must be finite with positive scale")
        from pdfcadcore.text_scale import effective_span_font_size_pt

        host_rotation_deg = _line_angle_deg({"dir": line_direction}, opts)
        if not math.isfinite(host_rotation_deg):
            raise ValueError("host text rotation is invalid")
        size_pt = float(effective_span_font_size_pt(span, host_rotation_deg))
        if not math.isfinite(size_pt) or size_pt <= 0.0:
            raise ValueError("source font size is unavailable")
        font_size_fc = _fit_font_size_to_span_bbox(
            source_text,
            size_pt * item_scale,
            span,
            item_scale,
            host_rotation_deg,
        )
        if not math.isfinite(font_size_fc) or font_size_fc <= 0.0:
            raise ValueError("source font size transform is invalid")
        target_advance_fc = _source_span_advance_fc(
            span, {"dir": line_direction}, item_scale
        )
        if not math.isfinite(target_advance_fc) or target_advance_fc <= 1e-9:
            raise ValueError("source span advance is unavailable")
        pos = _to_fc(origin, page_height, opts, item_scale)
        rot = Rotation(Vector(0.0, 0.0, 1.0), host_rotation_deg)
    except Exception as exc:
        terminal_failure(
            "text_transform_or_dimension_failed",
            {
                "attempted_source_results": source_results,
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )

    try:
        creation_started = True
        shape_string = _make_shapestring_host(
            _text_host_document(None, text_group), source_text, font_path
        )
        if shape_string is None:
            raise RuntimeError("Draft ShapeString returned no host object")
        add_owned(shape_string)
        collection_error = collect_owned()
        if collection_error:
            raise RuntimeError("owned object collection failed: %s" % collection_error)
    except Exception as exc:
        terminal_failure(
            "shapestring_creation_failed",
            {
                "attempted_source_results": source_results,
                "font_path": font_path,
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )

    stage = "placement"
    try:
        shape_string.Placement = Placement(pos, rot)
        depth = max(font_size_fc * 0.12, 0.05)
        source_color = _span_source_color(span)
        normalized_font = _normalize_pdf_font_name(source_font)

        def _configure_item_host(host_obj):
            # P1 ordering: every custom property write happens BEFORE the host
            # object's recompute inside _create_verified_text3d_entity, so the
            # page-end document recompute has nothing left to re-execute.
            nonlocal stage
            stage = "host_annotation"
            _annotate_text_host_object(host_obj, source_item_id, "3d_text")
            stage = "source_color"
            _persist_text_style_metadata(
                host_obj,
                font_name=normalized_font,
                font_size=font_size_fc,
                source_color=source_color,
            )
            _apply_text_color(host_obj, source_color)
            stage = "calibration_extrusion"

        stage = "calibration_extrusion"
        (
            extrusion,
            calibrated_support,
            horizontal_scale,
            verified_advance_fc,
        ) = _create_verified_text3d_entity(
            shape_string,
            font_size_fc=font_size_fc,
            depth=depth,
            target_advance_fc=target_advance_fc,
            baseline_angle_deg=host_rotation_deg,
            text_group=text_group,
            baseline_object_ids=baseline_objects,
            configure_host=_configure_item_host,
        )
        add_owned(calibrated_support)
        add_owned(extrusion)
        collection_error = collect_owned()
        if collection_error:
            raise RuntimeError("owned object collection failed: %s" % collection_error)

        stage = "source_color"
        color_metadata = _format_color_metadata(source_color)
        style_verifications: List[str] = []
        for host_obj in (shape_string, calibrated_support, extrusion):
            if (
                str(getattr(host_obj, "PDFTextFontName", "") or "")
                != normalized_font
                or not math.isclose(
                    float(host_obj.PDFTextFontSize),
                    float(font_size_fc),
                    abs_tol=1e-7,
                )
                or str(getattr(host_obj, "PDFTextJustification", "") or "")
                != "Left"
                or str(getattr(host_obj, "PDFTextColorRGB", "") or "")
                != color_metadata
            ):
                raise RuntimeError("source text style metadata could not be verified")
            view = getattr(host_obj, "ViewObject", None)
            if view is None:
                style_verifications.append("headless_app_metadata")
                continue
            if source_color is not None:
                rendered_colors = [
                    getattr(view, prop, None)
                    for prop in ("TextColor", "ShapeColor", "LineColor", "PointColor")
                ]
                if not any(
                    isinstance(color, (tuple, list))
                    and len(color) >= 3
                    and all(
                        abs(float(color[index]) - float(source_color[index])) <= 1e-6
                        for index in range(3)
                    )
                    for color in rendered_colors
                ):
                    raise RuntimeError("source text color could not be verified")
            style_verifications.append("gui_view_and_app_metadata")

        stage = "host_verification"
        created_ids, ids_complete = owned_ids()
        required_objects = (shape_string, calibrated_support, extrusion)
        required_ids = [_host_object_id(host_obj) for host_obj in required_objects]
        source_text_preserved = getattr(shape_string, "String", None) == source_text
        source_item_id_verified = all(
            getattr(host_obj, "PDFSourceItemId", None) == source_item_id
            and getattr(host_obj, "PDFRepresentation", None) == "3d_text"
            for host_obj in required_objects
        )
        verified_rotation_deg = _host_text_rotation_deg(shape_string)
        calibrated_rotation_deg = _host_text_rotation_deg(calibrated_support)
        verified_anchor_xyz = _host_anchor_xyz(shape_string)
        calibrated_anchor_xyz = _host_anchor_xyz(calibrated_support)
        expected_anchor_xyz = (
            float(pos.x),
            float(pos.y),
            float(pos.z),
        )
        anchors_match = bool(
            verified_anchor_xyz is not None
            and calibrated_anchor_xyz is not None
            and all(
                abs(verified_anchor_xyz[index] - expected_anchor_xyz[index]) <= 1e-7
                for index in range(3)
            )
            and all(
                abs(calibrated_anchor_xyz[index] - expected_anchor_xyz[index]) <= 1e-7
                for index in range(3)
            )
        )
        shape = getattr(extrusion, "Shape", None)
        solids = list(getattr(shape, "Solids", []) or []) if shape is not None else []
        volume = float(getattr(shape, "Volume", 0.0) or 0.0) if shape is not None else 0.0
        live_objects = list(getattr(doc, "Objects", []) or [])
        if (
            not ids_complete
            or not created_ids
            or any(not entity_id or entity_id not in created_ids for entity_id in required_ids)
            or any(not any(candidate is host_obj for candidate in live_objects) for host_obj in owned)
            or not source_text_preserved
            or not source_item_id_verified
            or str(getattr(extrusion, "TypeId", "") or "") != "Part::Extrusion"
            or shape is None
            or bool(getattr(shape, "isNull", lambda: True)())
            or not solids
            or not math.isfinite(volume)
            or volume <= 0.0
            or verified_rotation_deg is None
            or calibrated_rotation_deg is None
            or not _rotation_matches(verified_rotation_deg, host_rotation_deg)
            or not _rotation_matches(calibrated_rotation_deg, host_rotation_deg)
            or not anchors_match
            or getattr(extrusion, "Base", None) is not calibrated_support
            or not math.isfinite(float(verified_advance_fc))
            or abs(float(verified_advance_fc) - target_advance_fc)
            > max(0.05, target_advance_fc * 0.03)
        ):
            raise RuntimeError("3D Text host evidence could not be verified")
    except Exception as exc:
        terminal_failure(
            "%s_failed" % stage,
            {
                "attempted_source_results": source_results,
                "font_path": font_path,
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )

    return {
        "source_item_id": source_item_id,
        "requested_type": requested_type,
        "attempted_type": "3d_text",
        "final_type": "3d_text",
        "outcome": "verified",
        "created_entity_ids": created_ids,
        "delivery_entity_ids": [required_ids[2]],
        "support_entity_ids": required_ids[:2],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "evidence": {
            "source_text": source_text,
            "source_text_preserved": True,
            "source_item_id": source_item_id,
            "source_item_id_verified": True,
            "entity_type": str(getattr(extrusion, "TypeId", "") or ""),
            "solid_count": len(solids),
            "volume": volume,
            "rotation_deg": float(verified_rotation_deg),
            "verified_anchor_xyz": tuple(verified_anchor_xyz),
            "target_advance": float(target_advance_fc),
            "verified_advance": float(verified_advance_fc),
            "font_path": font_path,
            "font_source_result": font_source_result,
            "nominal_height": float(font_size_fc),
            "horizontal_scale": float(horizontal_scale),
            "extrusion_depth": float(depth),
            "source_color": source_color,
            "color_verified": True,
            "style_verification": (
                style_verifications[0]
                if len(set(style_verifications)) == 1
                else "mixed_view_and_app_metadata"
            ),
            "view_style_verified": all(
                value == "gui_view_and_app_metadata"
                for value in style_verifications
            ),
        },
    }


def _render_text_spans_3d(
    tdict: dict,
    text_group,
    page_h: float,
    opts: ImportOptions,
    scale: float,
    layout_context: Optional[dict] = None,
) -> int:
    """Render verified native 3D Text without silently changing representation."""
    if Draft is None or text_group is None:
        attempt = {
            "source_item_id": "page",
            "requested_type": "3d_text",
            "attempted_type": "3d_text",
            "outcome": "failed",
            "reason": "freecad_draft_or_group_unavailable",
            "created_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
        }
        opts.text_delivery_attempts.append(attempt)
        raise TextRepresentationFailure("3D Text unavailable: FreeCAD Draft/group missing", attempt)

    delivered: List[Tuple[dict, str, Any, Any, int]] = []
    owned: List[Any] = []
    attempts_for_call: List[Dict[str, Any]] = []
    page_num = int(getattr(opts, "_provenance_page", 1) or 1)

    def fail_attempt(attempt: Dict[str, Any], reason: str, evidence: Dict[str, Any]):
        doc = _text_host_document(owned[0] if owned else None, text_group)
        removed, cleanup_complete = (
            _remove_owned_text_objects(doc, text_group, owned) if doc is not None
            else ([], not owned)
        )
        removed_set = set(removed)
        for earlier in attempts_for_call:
            if earlier.get("outcome") == "verified":
                earlier["outcome"] = "rolled_back"
                earlier["final_type"] = None
                earlier["superseded_by"] = attempt.get("source_item_id")
                earlier["removed_entity_ids"] = [
                    entity_id
                    for entity_id in (
                        list(earlier.get("created_entity_ids") or [])
                        + list(earlier.get("support_entity_ids") or [])
                    )
                    if entity_id in removed_set
                ]
                earlier["cleanup_complete"] = all(
                    entity_id in removed_set
                    for entity_id in (
                        list(earlier.get("created_entity_ids") or [])
                        + list(earlier.get("support_entity_ids") or [])
                    )
                )
        attempt.update(
            {
                "outcome": "failed",
                "reason": reason,
                "evidence": evidence,
                "removed_entity_ids": removed,
                "cleanup_complete": bool(cleanup_complete),
                "final_type": None,
            }
        )
        attempts_for_call.append(attempt)
        opts.text_delivery_attempts.extend(attempts_for_call)
        opts.text_delivered_counts.pop("native_3d_text", None)
        raise TextRepresentationFailure(
            "3D Text failed for %s: %s" % (attempt["source_item_id"], reason),
            attempt,
        )

    for block_index, block in enumerate(tdict.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for line_index, line in enumerate(block.get("lines", [])):
            spans = line.get("spans", []) or []
            if not spans:
                continue
            angle_deg = _line_angle_deg(line, opts)

            for span_index, span in enumerate(spans):
                source_text = str(span.get("text", "") or "")
                if not source_text or source_text.isspace():
                    continue
                source_item_id = "p%d:b%d:l%d:s%d" % (
                    page_num, block_index, line_index, span_index
                )
                attempt = {
                    "source_item_id": source_item_id,
                    "requested_type": "3d_text",
                    "attempted_type": "3d_text",
                    "created_entity_ids": [],
                    "support_entity_ids": [],
                    "removed_entity_ids": [],
                    "cleanup_complete": False,
                    "superseded_by": None,
                }
                from pdfcadcore.text_scale import effective_span_font_size_pt

                # Quantity-column heuristics must never change source rotation.
                # The source line direction is authoritative for 3D Text.
                span_angle_deg = angle_deg
                size_pt = effective_span_font_size_pt(span, span_angle_deg)
                font_size_fc = max(0.1, size_pt * scale)
                font_size_fc = _fit_font_size_to_span_bbox(
                    source_text, font_size_fc, span, scale, span_angle_deg
                )

                origin = _span_origin_pdf(span)
                if not origin:
                    fail_attempt(
                        attempt,
                        "source_origin_unavailable",
                        {"source_font": str(span.get("font", "") or "")},
                    )
                pos = _to_fc(origin, page_h, opts, scale)
                rot = Rotation(Vector(0, 0, 1), span_angle_deg)
                target_advance_fc = _source_span_advance_fc(span, line, scale)

                source_font = str(span.get("font", "") or "").strip()
                font_path = _resolve_shapestring_font_path(source_font, opts)
                if not font_path:
                    _record_shapestring_skip(opts, "exact_source_font_unavailable")
                    fail_attempt(
                        attempt,
                        "exact source font unavailable",
                        {"source_font": source_font, "item_specific_attempted": True},
                    )

                doc = _text_host_document(None, text_group)
                before_objects = {id(obj) for obj in _document_objects(doc)} if doc is not None else set()
                try:
                    ss = _make_shapestring_host(doc, source_text, font_path)
                except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
                    if doc is not None:
                        owned.extend(
                            obj for obj in _document_objects(doc)
                            if id(obj) not in before_objects and obj not in owned
                        )
                    _record_shapestring_skip(opts, "shapestring_failed")
                    fail_attempt(
                        attempt,
                        "shapestring creation failed",
                        {
                            "source_font": source_font,
                            "font_path": font_path,
                            "exception": "%s: %s" % (exc.__class__.__name__, exc),
                            "item_specific_attempted": True,
                        },
                    )
                owned.append(ss)
                try:
                    ss.Placement = Placement(pos, rot)
                    depth = max(font_size_fc * 0.12, 0.05)
                    span_color = _span_source_color(span)

                    def _configure_span_host(
                        host_obj, _sid=source_item_id, _color=span_color
                    ):
                        # P1 ordering: writes land before each host object's
                        # only recompute (see _create_verified_text3d_entity).
                        _annotate_text_host_object(host_obj, _sid, "3d_text")
                        _apply_text_color(host_obj, _color)

                    (
                        extrusion,
                        calibrated_support,
                        horizontal_scale,
                        verified_advance_fc,
                    ) = _create_verified_text3d_entity(
                        ss,
                        font_size_fc=font_size_fc,
                        depth=depth,
                        target_advance_fc=target_advance_fc,
                        baseline_angle_deg=span_angle_deg,
                        text_group=text_group,
                        configure_host=_configure_span_host,
                    )
                    owned.extend([calibrated_support, extrusion])
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    if doc is not None:
                        owned.extend(
                            obj for obj in _document_objects(doc)
                            if id(obj) not in before_objects and obj not in owned
                        )
                    _record_shapestring_skip(opts, "extrusion_verification_failed")
                    fail_attempt(
                        attempt,
                        "3D extrusion verification failed",
                        {
                            "source_font": source_font,
                            "font_path": font_path,
                            "exception": "%s: %s" % (exc.__class__.__name__, exc),
                            "item_specific_attempted": True,
                        },
                    )

                attempt.update(
                    {
                        "outcome": "verified",
                        "reason": "requested 3D Text delivered",
                        "final_type": "3d_text",
                        "created_entity_ids": [_host_object_id(extrusion)],
                        "support_entity_ids": [
                            _host_object_id(ss),
                            _host_object_id(calibrated_support),
                        ],
                        "cleanup_complete": True,
                        "evidence": {
                            "entity_type": str(getattr(extrusion, "TypeId", "")),
                            "source_font": source_font,
                            "font_path": font_path,
                            "source_text_preserved": getattr(ss, "String", source_text) == source_text,
                            "rotation_deg": float(span_angle_deg),
                            "nominal_height": float(font_size_fc),
                            "source_advance": float(target_advance_fc),
                            "verified_advance": float(verified_advance_fc),
                            "horizontal_scale": float(horizontal_scale),
                            "extrusion_depth": float(depth),
                            "solid_count": len(getattr(extrusion.Shape, "Solids", []) or []),
                            "volume": float(getattr(extrusion.Shape, "Volume", 0.0) or 0.0),
                        },
                    }
                )
                attempts_for_call.append(attempt)
                stable_index = block_index * 1_000_000 + line_index * 10_000 + span_index
                delivered.append(
                    (span, source_item_id, extrusion, calibrated_support, stable_index)
                )

    if not delivered:
        attempt = {
            "source_item_id": "p%d:page" % page_num,
            "requested_type": "3d_text",
            "attempted_type": "3d_text",
            "created_entity_ids": [],
            "support_entity_ids": [],
        }
        fail_attempt(attempt, "no non-whitespace source text items", {"item_specific_attempted": True})

    opts.text_delivery_attempts.extend(attempts_for_call)
    _record_text_delivery(opts, "native_3d_text", len(delivered))
    try:
        from pdfcadcore.source_provenance import record_text_span_provenance

        for span, _source_item_id, extrusion, _support, stable_index in delivered:
            record_text_span_provenance(
                opts,
                page=page_num,
                span=span,
                text=str(span.get("text", "") or ""),
                created_entity_type="native_3d_text",
                parent_handle=_host_object_id(extrusion),
                import_mode=str(getattr(opts, "import_mode", "") or ""),
                text_mode="3d_text",
                span_index=stable_index,
            )
    except (ImportError, TypeError, ValueError):
        pass
    return len(delivered)


def _render_requested_svg_text(
    pdf_path: str,
    page_num: int,
    page_h: float,
    page_w: float,
    scale: float,
    fc_doc,
    parent_group,
    opts: ImportOptions,
) -> Tuple[dict, dict]:
    """Render Glyphs or Geometry and prove that the requested type survived."""
    requested = str(getattr(opts, "text_mode", "") or "").strip().lower()
    if requested not in {"glyphs", "geometry"}:
        raise ValueError("SVG text renderer requires glyphs or geometry mode")

    failure_evidence: Dict[str, Any] = {}
    failure_reason = "svg_text_render_failed"
    try:
        from PDFVectorImporter.src.PDFSvgTextRenderer import (
            TextRepresentationRenderError,
            render_text,
        )
    except (RuntimeError, ValueError, TypeError, OSError, ImportError, AttributeError) as exc:
        failure_reason = "svg_renderer_exception"
        failure_evidence = {"exception": "%s: %s" % (exc.__class__.__name__, exc)}
        result = None
    else:
        try:
            result = render_text(
                pdf_path,
                page_num,
                page_h,
                scale,
                page_w,
                fc_doc=fc_doc,
                parent_group=parent_group,
                flip_y=opts.flip_y,
                representation=requested,
            )
        except TextRepresentationRenderError as exc:
            failure_reason = str(getattr(exc, "reason", "") or failure_reason)
            failure_evidence = dict(getattr(exc, "evidence", {}) or {})
            result = None
        except (RuntimeError, ValueError, TypeError, OSError, ImportError, AttributeError) as exc:
            failure_reason = "svg_renderer_exception"
            failure_evidence = {"exception": "%s: %s" % (exc.__class__.__name__, exc)}
            result = None

    if result:
        entity_type = str(result.get("entity_type", "") or "").strip().lower()
        outcome = str(result.get("outcome", "") or "").strip().lower()
        created_ids = [
            str(value)
            for value in list(result.get("created_entity_ids") or [])
            if str(value or "")
        ]
        entity_count = int(result.get("entities", 0) or 0)
        delivery_count = (
            int(result.get("raw_edges", 0) or 0)
            if requested == "geometry"
            else int(result.get("glyphs", 0) or 0)
        )
        result_attempts = list(result.get("delivery_attempts") or [])
        attempts_match = bool(result_attempts) and all(
            str(item.get("requested_type", "") or "").strip().lower() == requested
            and str(item.get("attempted_type", "") or "").strip().lower() == requested
            and str(item.get("final_type", "") or "").strip().lower() == requested
            and str(item.get("outcome", "") or "").strip().lower() == "verified"
            for item in result_attempts
        )
        verified = (
            outcome == "verified"
            and entity_type == requested
            and entity_count > 0
            and delivery_count > 0
            and len(created_ids) == entity_count
            and len(set(created_ids)) == entity_count
            and attempts_match
        )
        if verified:
            opts.text_delivery_attempts.extend(result_attempts)
            bucket = (
                "raw_geometry_edges" if requested == "geometry"
                else "outline_curve_or_mesh"
            )
            _record_text_delivery(opts, bucket, delivery_count)
            return result, {
                "entity_type": requested,
                "count": delivery_count,
                "font_rendered": False,
                "examples": [],
            }
        failure_reason = "requested_representation_verification_failed"
        failure_evidence = {
            "renderer_outcome": outcome,
            "renderer_entity_type": entity_type,
            "entity_count": entity_count,
            "delivery_count": delivery_count,
            "created_entity_ids": created_ids,
            "attempts_match": attempts_match,
        }

    attempt = {
        "source_item_id": "p%d:page" % int(page_num),
        "requested_type": requested,
        "attempted_type": requested,
        "final_type": None,
        "outcome": "failed",
        "reason": failure_reason,
        "created_entity_ids": list(failure_evidence.get("created_entity_ids") or []),
        "support_entity_ids": [],
        "removed_entity_ids": list(failure_evidence.get("removed_entity_ids") or []),
        "cleanup_complete": bool(failure_evidence.get("cleanup_complete", True)),
        "evidence": failure_evidence,
    }
    opts.text_delivery_attempts.append(attempt)
    opts.text_delivered_counts.pop("outline_curve_or_mesh", None)
    opts.text_delivered_counts.pop("raw_geometry_edges", None)
    raise TextRepresentationFailure(
        "%s rendering failed: %s" % (requested, failure_reason),
        attempt,
    )


def _deliver_text_item_svg(
    item: Dict[str, Any],
    attempted_type: str,
    opts: ImportOptions,
    *,
    pdf_path: str,
    page_h: float,
    page_w: float,
    scale: float,
    fc_doc,
    parent_group,
    render_cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deliver one canonical item as verified SVG Glyphs or raw Geometry."""
    try:
        bound_item = copy.deepcopy(item) if isinstance(item, dict) else {}
    except Exception:
        bound_item = {}
    source_item_id = str(bound_item.get("source_item_id") or "")
    requested_type = bound_item.get("requested_type")

    def raise_attempt(
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
        *,
        cleanup_complete: bool = True,
    ):
        failure_evidence = dict(evidence or {})
        attempt = {
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "attempted_type": attempted_type,
            "final_type": None,
            "outcome": "failed",
            "reason": str(reason),
            "created_entity_ids": list(
                failure_evidence.get("created_entity_ids") or []
            ),
            "support_entity_ids": [],
            "removed_entity_ids": list(
                failure_evidence.get("removed_entity_ids") or []
            ),
            "cleanup_complete": bool(cleanup_complete),
            "evidence": failure_evidence,
        }
        raise TextRepresentationFailure(
            "%s delivery failed for %s: %s"
            % (attempted_type, source_item_id or "item", reason),
            attempt,
        )

    try:
        page_number = bound_item.get("page_number")
        block_index = bound_item.get("block_index")
        line_index = bound_item.get("line_index")
        span_index = bound_item.get("span_index")
        pdf_sha256 = bound_item.get("pdf_sha256")
        bbox = _finite_source_tuple(bound_item.get("bbox"), 4, "item.bbox")
        source_text = bound_item.get("text")
        span = bound_item.get("span")
        span_bbox = _finite_source_tuple(
            span.get("bbox") if isinstance(span, dict) else None,
            4,
            "span.bbox",
        )
        expected_source_item_id = "p%d:b%d:l%d:s%d" % (
            page_number,
            block_index,
            line_index,
            span_index,
        )
        if (
            bound_item.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY
            or type(page_number) is not int
            or page_number <= 0
            or any(
                type(index) is not int or index < 0
                for index in (block_index, line_index, span_index)
            )
            or source_item_id != expected_source_item_id
            or not isinstance(pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
            or not isinstance(source_text, str)
            or not source_text
            or source_text.isspace()
            or not isinstance(span, dict)
            or span.get("text") != source_text
            or requested_type not in TEXT_ITEM_FALLBACK_LADDERS
            or str(getattr(opts, "text_mode", "") or "").strip().lower()
            != requested_type
            or attempted_type not in {"glyphs", "geometry"}
            or attempted_type not in TEXT_ITEM_FALLBACK_LADDERS[requested_type]
            or not isinstance(bound_item.get("bbox"), tuple)
            or not isinstance(span.get("bbox"), tuple)
            or span_bbox != bbox
            or bbox[2] <= bbox[0]
            or bbox[3] <= bbox[1]
            or not isinstance(pdf_path, str)
            or not pdf_path.strip()
            or fc_doc is None
            or any(
                not math.isfinite(float(value)) or float(value) <= 0.0
                for value in (page_h, page_w, scale)
            )
        ):
            raise ValueError("canonical SVG text item delivery context is invalid")
    except Exception as exc:
        raise_attempt(
            "invalid_svg_text_source_item",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        baseline_object_ids = {
            id(host_obj)
            for host_obj in list(getattr(fc_doc, "Objects", []) or [])
        }
    except Exception as exc:
        raise_attempt(
            "svg_text_document_snapshot_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    def fail_after_render(
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ):
        failure_evidence = dict(evidence or {})
        evidence_created = [
            value
            for value in list(failure_evidence.get("created_entity_ids") or [])
            if isinstance(value, str) and value
        ]
        evidence_removed = [
            value
            for value in list(failure_evidence.get("removed_entity_ids") or [])
            if isinstance(value, str) and value
        ]
        collection_error = ""
        owned_now: List[Any] = []
        try:
            current_objects = list(getattr(fc_doc, "Objects", []) or [])
            owned_now = [
                host_obj
                for host_obj in current_objects
                if id(host_obj) not in baseline_object_ids
            ]
        except Exception as exc:
            collection_error = "%s: %s" % (exc.__class__.__name__, exc)
        current_created = [_host_object_id(host_obj) for host_obj in owned_now]
        current_removed: List[str] = []
        helper_complete = not owned_now
        if owned_now:
            try:
                current_removed, helper_complete = _remove_owned_text_objects(
                    fc_doc, parent_group, owned_now
                )
            except Exception as exc:
                failure_evidence["cleanup_error"] = "%s: %s" % (
                    exc.__class__.__name__, exc
                )
                helper_complete = False
        created_ids = list(dict.fromkeys(evidence_created + current_created))
        removed_ids = list(dict.fromkeys(evidence_removed + current_removed))
        cleanup_complete = bool(
            not collection_error
            and helper_complete
            and failure_evidence.get("cleanup_complete", True) is True
            and all(created_ids)
            and set(created_ids) == set(removed_ids)
        ) if created_ids else bool(
            not collection_error
            and helper_complete
            and failure_evidence.get("cleanup_complete", True) is True
            and not removed_ids
        )
        failure_evidence["created_entity_ids"] = created_ids
        failure_evidence["removed_entity_ids"] = removed_ids
        failure_evidence["cleanup_complete"] = cleanup_complete
        if collection_error:
            failure_evidence["ownership_collection_error"] = collection_error
        raise_attempt(
            reason,
            failure_evidence,
            cleanup_complete=cleanup_complete,
        )

    def impossible_after_render(
        reason: str,
        evidence: Optional[Dict[str, Any]] = None,
    ):
        cleaned_attempt: Dict[str, Any] = {}
        try:
            fail_after_render(reason, evidence)
        except TextRepresentationFailure as cleaned_failure:
            cleaned_attempt = dict(cleaned_failure.attempt or {})
        if cleaned_attempt.get("cleanup_complete") is not True:
            raise TextRepresentationFailure(
                "SVG item impossibility cleanup was incomplete",
                cleaned_attempt,
            )
        created_ids = list(cleaned_attempt.get("created_entity_ids") or [])
        removed_ids = list(cleaned_attempt.get("removed_entity_ids") or [])
        proof_evidence = {
            "source_item_id": source_item_id,
            "source_bbox": bbox,
            "renderer_reason": reason,
            "renderer_evidence": dict(evidence or {}),
        }
        proof = {
            "item_specific_proven_impossible": True,
            "importer_identity": FREECAD_TEXT_IMPORTER_IDENTITY,
            "pdf_sha256": pdf_sha256,
            "page_number": page_number,
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "attempted_type": attempted_type,
            "reason_code": reason,
            "evidence": proof_evidence,
            "attempted_source_results": [
                {
                    "source": "svg_item_renderer",
                    "outcome": "proven_impossible",
                    "reason_code": reason,
                    "pdf_sha256": pdf_sha256,
                    "page_number": page_number,
                    "source_item_id": source_item_id,
                }
            ],
            "attempted_sources_complete": True,
            "created_entity_ids": created_ids,
            "removed_entity_ids": removed_ids,
            "cleanup_complete": True,
        }
        impossible_attempt = dict(cleaned_attempt)
        impossible_attempt.update(
            {
                "final_type": None,
                "outcome": "proven_impossible",
                "reason": reason,
                "reason_code": reason,
                "proof": proof,
            }
        )
        raise TextItemImpossible(
            "%s is proven impossible for %s" % (attempted_type, source_item_id),
            attempt=impossible_attempt,
            proof=proof,
        )

    try:
        from PDFVectorImporter.src.PDFSvgTextRenderer import (
            TextRepresentationRenderError,
            render_text,
        )
    except Exception as exc:
        fail_after_render(
            "svg_renderer_exception",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        result = render_text(
            pdf_path,
            page_number,
            float(page_h),
            float(scale),
            float(page_w),
            fc_doc=fc_doc,
            parent_group=parent_group,
            flip_y=bool(getattr(opts, "flip_y", True)),
            representation=attempted_type,
            source_item=bound_item,
            requested_representation=requested_type,
            page_rotation_matrix=_page_matrix_values(opts),
            render_cache=render_cache,
        )
    except TextRepresentationRenderError as exc:
        renderer_reason = str(
            getattr(exc, "reason", "") or "svg_text_render_failed"
        )
        renderer_evidence = dict(getattr(exc, "evidence", {}) or {})
        if renderer_reason in CLOSED_SVG_ITEM_IMPOSSIBILITY_REASONS:
            impossible_after_render(renderer_reason, renderer_evidence)
        fail_after_render(renderer_reason, renderer_evidence)
    except Exception as exc:
        fail_after_render(
            "svg_renderer_exception",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        created_ids = result.get("created_entity_ids")
        attempts = result.get("delivery_attempts")
        attempt = attempts[0]
        entity_count = result.get("entities")
        delivery_count = (
            result.get("raw_edges")
            if attempted_type == "geometry"
            else result.get("glyphs")
        )
        item_filter_evidence = result.get("item_filter")
        attempt_evidence = attempt.get("evidence")
        child_ids = attempt_evidence.get("child_source_item_ids")
        child_id_pattern = re.compile(
            re.escape(source_item_id)
            + (r":geometry" if attempted_type == "geometry" else r":g\d+")
        )
        if (
            not isinstance(result, dict)
            or result.get("outcome") != "verified"
            or result.get("entity_type") != attempted_type
            or result.get("source_item_id") != source_item_id
            or type(entity_count) is not int
            or entity_count <= 0
            or type(delivery_count) is not int
            or delivery_count <= 0
            or not isinstance(created_ids, list)
            or len(created_ids) != entity_count
            or any(not isinstance(value, str) or not value for value in created_ids)
            or len(set(created_ids)) != entity_count
            or not isinstance(attempts, list)
            or len(attempts) != 1
            or not isinstance(attempt, dict)
            or attempt.get("source_item_id") != source_item_id
            or attempt.get("requested_type") != requested_type
            or attempt.get("attempted_type") != attempted_type
            or attempt.get("final_type") != attempted_type
            or attempt.get("outcome") != "verified"
            or attempt.get("created_entity_ids") != created_ids
            or type(attempt.get("delivery_count")) is not int
            or attempt.get("delivery_count") != delivery_count
            or attempt.get("support_entity_ids") != []
            or attempt.get("removed_entity_ids") != []
            or attempt.get("cleanup_complete") is not True
            or not isinstance(item_filter_evidence, dict)
            or not item_filter_evidence.get("matched_placement_indices")
            or not isinstance(attempt_evidence, dict)
            or attempt_evidence.get("source_item_bbox") != bbox
            or attempt_evidence.get("matched_placement_indices")
            != item_filter_evidence.get("matched_placement_indices")
            or not isinstance(child_ids, list)
            or len(child_ids) != entity_count
            or any(
                not isinstance(child_id, str)
                or child_id_pattern.fullmatch(child_id) is None
                for child_id in child_ids
            )
            or len(set(child_ids)) != entity_count
        ):
            raise ValueError("item-filtered SVG result contract is invalid")

        current_objects = list(getattr(fc_doc, "Objects", []) or [])
        delivered_objects = [
            host_obj
            for host_obj in current_objects
            if id(host_obj) not in baseline_object_ids
        ]
        delivered_ids = [_host_object_id(host_obj) for host_obj in delivered_objects]
        live_child_ids = [
            getattr(host_obj, "PDFSourceItemId", None)
            for host_obj in delivered_objects
        ]
        if (
            len(delivered_objects) != entity_count
            or set(delivered_ids) != set(created_ids)
            or len(live_child_ids) != entity_count
            or len(set(live_child_ids)) != entity_count
            or set(live_child_ids) != set(child_ids)
            or any(
                str(getattr(host_obj, "TypeId", "") or "") != "Part::Feature"
                or getattr(host_obj, "PDFParentSourceItemId", None) != source_item_id
                or getattr(host_obj, "PDFRepresentation", None) != attempted_type
                for host_obj in delivered_objects
            )
        ):
            raise ValueError("item-filtered SVG host entities are invalid")
    except Exception as exc:
        result_summary = {
            "exception": "%s: %s" % (exc.__class__.__name__, exc),
            "created_entity_ids": (
                list(result.get("created_entity_ids") or [])
                if isinstance(result, dict)
                else []
            ),
        }
        fail_after_render(
            "requested_representation_verification_failed",
            result_summary,
        )

    return copy.deepcopy(attempt)


def _render_canonical_text_items(
    *,
    pdf_doc,
    page,
    pdf_path: str,
    page_num: int,
    page_h: float,
    page_w: float,
    scale: float,
    fc_doc,
    parent_group,
    opts: ImportOptions,
    pdf_sha256: str,
    raw_tdict: Optional[dict] = None,
) -> Dict[str, Any]:
    """Deliver raw PDF spans through the finite item representation contract."""
    requested = _normalize_requested_text_type(str(opts.text_mode or ""))
    source_dict = raw_tdict if raw_tdict is not None else page.get_text("dict")
    items = list(
        _iter_text_source_items(source_dict, int(page_num), pdf_sha256, requested)
    )

    font_stage_complete = False
    svg_render_cache: Dict[str, Any] = {
        "source_item_manifest": [
            {
                "source_order": source_order,
                "source_item_id": item["source_item_id"],
                "page_number": item["page_number"],
                "pdf_sha256": item["pdf_sha256"],
                "bbox": item["bbox"],
                "text": item["text"],
            }
            for source_order, item in enumerate(items)
        ]
    }

    def deliver_3d(item, attempted, state):
        nonlocal font_stage_complete
        if not font_stage_complete:
            _stage_page_shapestring_fonts(
                pdf_doc,
                page,
                opts,
                pdf_sha256=pdf_sha256,
                page_number=int(page_num),
            )
            font_stage_complete = True
        return _deliver_text_item_3d(
            item,
            attempted,
            state,
            text_group=parent_group,
            page_h=page_h,
            scale=scale,
        )

    deliverers = {
        "text": lambda item, attempted, state: _deliver_text_item_native(
            item,
            attempted,
            state,
            text_group=parent_group,
            page_h=page_h,
            scale=scale,
        ),
        "labels": lambda item, attempted, state: _deliver_text_item_native(
            item,
            attempted,
            state,
            text_group=parent_group,
            page_h=page_h,
            scale=scale,
        ),
        "3d_text": deliver_3d,
        "glyphs": lambda item, attempted, state: _deliver_text_item_svg(
            item,
            attempted,
            state,
            pdf_path=pdf_path,
            page_h=page_h,
            page_w=page_w,
            scale=scale,
            fc_doc=fc_doc,
            parent_group=parent_group,
            render_cache=svg_render_cache,
        ),
        "geometry": lambda item, attempted, state: _deliver_text_item_svg(
            item,
            attempted,
            state,
            pdf_path=pdf_path,
            page_h=page_h,
            page_w=page_w,
            scale=scale,
            fc_doc=fc_doc,
            parent_group=parent_group,
            render_cache=svg_render_cache,
        ),
        "raster": lambda item, attempted, state: _deliver_text_item_raster(
            item,
            attempted,
            state,
            page=page,
            page_h=page_h,
            scale=scale,
            fc_doc=fc_doc,
            parent_group=parent_group,
        ),
    }

    results: List[Dict[str, Any]] = []
    delivered_source_ids: List[str] = []
    final_types: List[str] = []
    delivered_entity_count = 0
    bucket_by_type = {
        "text": "native_text",
        "labels": "native_label",
        "3d_text": "native_3d_text",
        "glyphs": "outline_curve_or_mesh",
        "geometry": "raw_geometry_edges",
        "raster": "raster_text_patch",
    }
    for item in items:
        result = _run_text_item_fallback_ladder(item, requested, deliverers, opts)
        results.append(result)
        source_item_id = str(result["source_item_id"])
        final_type = str(result["final_type"])
        delivery_ids = result.get(
            "delivery_entity_ids", result.get("created_entity_ids")
        )
        reported_delivery_count = result.get("delivery_count")
        created_count = (
            int(reported_delivery_count)
            if type(reported_delivery_count) is int and reported_delivery_count > 0
            else len(list(delivery_ids or []))
        )
        delivered_source_ids.append(source_item_id)
        final_types.append(final_type)
        delivered_entity_count += created_count
        _record_text_delivery(opts, bucket_by_type[final_type], created_count)

    unique_final_types = sorted(set(final_types))
    return {
        "entity_type": (
            unique_final_types[0] if len(unique_final_types) == 1 else "mixed"
        ),
        "count": delivered_entity_count,
        "source_item_count": len(results),
        "source_item_ids": delivered_source_ids,
        "font_rendered": any(value in {"text", "labels", "3d_text"} for value in final_types),
        "examples": [],
        "attempts": results,
    }


def _is_near_horizontal(dx: float, dy: float) -> bool:
    return abs(dx) > 0.95 and abs(dy) < 0.10


def _preprocess_text_blocks(tdict: dict) -> dict:
    """Split PyMuPDF lines conservatively when span coordinates prove the text
    is not a single clean run. This helps when PyMuPDF merges neighboring runs
    that are on almost the same line but should remain separate."""
    for block in tdict.get("blocks", []):
        if block.get("type") != 0:
            continue
        fixed_lines = []
        for line in block.get("lines", []):
            spans = line.get("spans", []) or []
            if not spans:
                continue
            line_dir = line.get("dir", (1.0, 0.0))
            try:
                dx, dy = float(line_dir[0]), float(line_dir[1])
            except (TypeError, ValueError, IndexError):
                dx, dy = 1.0, 0.0
            is_horizontal = _is_near_horizontal(dx, dy)
            current_spans = [spans[0]]
            current_bbox = list(spans[0].get("bbox", (0, 0, 0, 0)))
            prev_bbox = current_bbox[:]
            for span in spans[1:]:
                bbox = list(span.get("bbox", (0, 0, 0, 0)))
                should_split = False
                if is_horizontal:
                    gap_x = float(bbox[0]) - float(prev_bbox[2])
                    gap_y = abs(float(bbox[1]) - float(prev_bbox[1]))
                    # Conservative: split on strong wrap-back / large gaps / clear stacked shift.
                    if gap_x < -1.0 or gap_x > 28.0 or gap_y > 4.0:
                        should_split = True
                else:
                    gap_x = abs(float(bbox[0]) - float(prev_bbox[0]))
                    gap_y = float(bbox[1]) - float(prev_bbox[3])
                    if gap_y > 28.0 or gap_x > 4.0:
                        should_split = True
                if should_split:
                    fixed_lines.append({"spans": current_spans, "bbox": tuple(current_bbox), "dir": line_dir})
                    current_spans = [span]
                    current_bbox = bbox[:]
                else:
                    current_spans.append(span)
                    current_bbox[0] = min(current_bbox[0], bbox[0])
                    current_bbox[1] = min(current_bbox[1], bbox[1])
                    current_bbox[2] = max(current_bbox[2], bbox[2])
                    current_bbox[3] = max(current_bbox[3], bbox[3])
                prev_bbox = bbox[:]
            if current_spans:
                fixed_lines.append({"spans": current_spans, "bbox": tuple(current_bbox), "dir": line_dir})
        block["lines"] = fixed_lines
    return tdict




# ──────────────────────────────────────────────────────────────────────
# Raster page import (scanned PDF fallback)
# ──────────────────────────────────────────────────────────────────────
def _import_page_as_raster(pdf_doc, page, page_num: int, page_h: float,
                           opts: ImportOptions, scale: float,
                           parent, fc_doc):
    """Render, persist, place, and reread one verified full-page ImagePlane."""
    del pdf_doc, page_h
    dpi = opts.raster_dpi or 200

    # Adaptive DPI: scale with page physical size so the image is always
    # readable without wasting memory on large sheets.
    #   A4 / Letter   (≤ 700 cm²)  → 200 DPI (default)
    #   A3 / Tabloid  (700–2000 cm²) → 300 DPI (maps need more detail)
    #   A2 and larger (> 2000 cm²) → 150 DPI (save memory, still readable)
    if not opts.raster_dpi_user_set:   # only adjust when user hasn't explicitly set a value
        w_cm = page.rect.width  * MM_PER_PT / 10.0
        h_cm = page.rect.height * MM_PER_PT / 10.0
        area_cm2 = w_cm * h_cm
        if area_cm2 > 2000:
            dpi = 150
        elif area_cm2 > 700:
            dpi = 300

    dpi, pixel_budget, was_capped = _cap_raster_dpi(page, dpi)
    if was_capped:
        _warn(
            f"Page {page_num}: raster DPI capped to {dpi} "
            f"({pixel_budget:,} pixel budget)"
        )

    pix = None
    last_error = None
    retry_dpis = []
    for candidate in (dpi, max(96, dpi // 2), 96):
        if candidate not in retry_dpis:
            retry_dpis.append(candidate)
    for candidate in retry_dpis:
        try:
            zoom = candidate / 72.0
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            dpi = candidate
            break
        except (RuntimeError, MemoryError, ValueError, OverflowError) as e:
            last_error = e
            _warn(
                f"Page {page_num}: raster render failed at {candidate} DPI: {e}"
            )
    if pix is None:
        raise RuntimeError(
            "Raster render failed after retries: %s" % (last_error or "unknown error")
        )

    digest = str(getattr(opts, "_pdf_sha256", "") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        digest = hashlib.sha256(
            ("page=%d|dpi=%d|w=%.9g|h=%.9g" % (
                page_num, dpi, float(page.rect.width), float(page.rect.height)
            )).encode("utf-8")
        ).hexdigest()
    asset_dir = _raster_asset_dir()
    asset_dir.mkdir(parents=True, exist_ok=True)
    img_path = asset_dir / ("page_%s_p%d_%ddpi.png" % (digest, page_num, dpi))
    try:
        _save_pixmap_atomic(pix, img_path)
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        raise RuntimeError("Raster asset could not be persisted: %s" % exc) from exc
    if not img_path.is_file() or img_path.stat().st_size <= 0:
        raise RuntimeError("Raster asset was not persisted")
    raster_sha256 = _path_sha256(img_path)

    # Match the vector/text transform exactly: PDF page units multiplied by the
    # effective import scale (MM_PER_PT when scale_to_mm is enabled, plus any
    # user scale). Hybrid imports must not place the raster at an unscaled size.
    w_units = page.rect.width * scale
    h_units = page.rect.height * scale

    baseline_ids = {id(host_obj) for host_obj in _document_objects(fc_doc)}
    ip = None
    try:
        ip = fc_doc.addObject("Image::ImagePlane", f"Page_{page_num}_raster")
        if ip is None or id(ip) in baseline_ids:
            raise RuntimeError("ImagePlane factory returned no new host object")
        ip.ImageFile = str(img_path)
        ip.XSize = w_units
        ip.YSize = h_units
        ip.Placement = Placement(_v(0, 0, -0.1), Rotation())  # slightly behind vectors
        add_property = getattr(ip, "addProperty", None)
        if not callable(add_property):
            raise RuntimeError("ImagePlane cannot embed its raster asset")
        if "PDFRasterFile" not in set(getattr(ip, "PropertiesList", []) or []):
            add_property("App::PropertyFileIncluded", "PDFRasterFile", "PDF Import")
        ip.PDFRasterFile = str(img_path)
        if "PDFSourceSHA256" not in set(getattr(ip, "PropertiesList", []) or []):
            add_property("App::PropertyString", "PDFSourceSHA256", "PDF Import")
        ip.PDFSourceSHA256 = digest
        if "PDFRasterSHA256" not in set(getattr(ip, "PropertiesList", []) or []):
            add_property("App::PropertyString", "PDFRasterSHA256", "PDF Import")
        ip.PDFRasterSHA256 = raster_sha256
        _annotate_text_host_object(ip, "p%d:page" % int(page_num), "raster")
        parent.addObject(ip)
        fc_doc.recompute()
        entity_id = _host_object_id(ip)
        live = fc_doc.getObject(entity_id)
        anchor = _host_anchor_xyz(ip)
        raster_file_evidence = _raster_file_evidence(ip, img_path)
        if (
            not entity_id
            or live is not ip
            or str(getattr(ip, "TypeId", "")) != "Image::ImagePlane"
            or str(getattr(ip, "PDFSourceSHA256", "")) != digest
            or str(getattr(ip, "PDFRasterSHA256", ""))
            != raster_file_evidence["source_asset_sha256"]
            or not math.isclose(float(ip.XSize), float(w_units), abs_tol=1e-7)
            or not math.isclose(float(ip.YSize), float(h_units), abs_tol=1e-7)
            or anchor is None
            or any(
                abs(anchor[index] - expected) > 1e-7
                for index, expected in enumerate((0.0, 0.0, -0.1))
            )
            or getattr(ip, "PDFSourceItemId", None) != "p%d:page" % int(page_num)
            or getattr(ip, "PDFRepresentation", None) != "raster"
        ):
            raise RuntimeError("full-page raster host evidence could not be verified")
    except Exception as exc:
        if ip is not None:
            try:
                _remove_owned_text_objects(fc_doc, parent, [ip])
            except Exception:
                pass
        raise RuntimeError("Raster placement failed: %s" % exc) from exc

    _msg(
        f"Placed page {page_num} as {dpi} DPI raster "
        f"({w_units:.0f} x {h_units:.0f} model units)"
    )
    return {
        "outcome": "verified",
        "entity_type": "raster",
        "created_entity_ids": [entity_id],
        "evidence": {
            "host_entity_type": "Image::ImagePlane",
            "raster_file": str(img_path),
            "raster_file_included": True,
            "pdf_sha256": digest,
            "dpi": int(dpi),
            "pixel_width": int(getattr(pix, "width", 0) or 0),
            "pixel_height": int(getattr(pix, "height", 0) or 0),
            "x_size": float(w_units),
            "y_size": float(h_units),
            **raster_file_evidence,
        },
    }


def _raster_pixel_budget() -> int:
    raw = os.environ.get("BC_FC_RASTER_PIXEL_BUDGET", "").strip()
    try:
        value = int(raw) if raw else 120_000_000
    except (TypeError, ValueError):
        value = 120_000_000
    return max(10_000_000, min(240_000_000, value))


def _cap_raster_dpi(page, requested_dpi: int):
    budget = _raster_pixel_budget()
    dpi = max(72, int(requested_dpi or 200))
    page_area_pt2 = max(1.0, float(page.rect.width) * float(page.rect.height))
    pixels = page_area_pt2 * ((dpi / 72.0) ** 2)
    if pixels <= budget:
        return dpi, budget, False
    capped = int(math.floor(math.sqrt(budget / page_area_pt2) * 72.0))
    capped = max(72, min(dpi, capped))
    return capped, budget, capped != dpi


# ──────────────────────────────────────────────────────────────────────
# View autofit
# ──────────────────────────────────────────────────────────────────────
def _pdf_import_root_objects(fc_doc):
    """Return top-level objects created by this importer (page groups)."""
    roots = []
    for obj in fc_doc.Objects:
        name = getattr(obj, "Name", "") or ""
        if not name.startswith("PDF_Page_"):
            continue
        try:
            if obj.isDerivedFrom("App::DocumentObjectGroup"):
                roots.append(obj)
        except (AttributeError, RuntimeError):
            continue
    return roots


def _autofit_import_view(fc_doc) -> None:
    """Frame the viewport on imported PDF geometry, not unrelated document content."""
    try:
        import FreeCADGui as Gui
    except ImportError:
        return

    try:
        fc_doc.recompute()
    except (RuntimeError, AttributeError):
        pass

    try:
        Gui.updateGui()
    except (AttributeError, RuntimeError):
        pass

    view = None
    try:
        adoc = Gui.ActiveDocument
        if adoc:
            view = adoc.ActiveView
    except (AttributeError, RuntimeError):
        view = None
    if view is None:
        return

    roots = _pdf_import_root_objects(fc_doc)
    prior_sel = []
    try:
        prior_sel = list(Gui.Selection.getSelection())
    except (AttributeError, RuntimeError):
        prior_sel = []

    try:
        if roots:
            try:
                Gui.Selection.clearSelection()
            except (AttributeError, RuntimeError):
                pass
            for obj in roots:
                try:
                    Gui.Selection.addSelection(obj)
                except (AttributeError, RuntimeError):
                    pass
            try:
                Gui.SendMsgToActiveView("ViewSelection")
            except (AttributeError, RuntimeError):
                pass

        try:
            view.setCameraType("Orthographic")
            view.viewTop()
            view.fitAll()
        except (AttributeError, RuntimeError):
            pass
    finally:
        try:
            Gui.Selection.clearSelection()
            for obj in prior_sel:
                try:
                    Gui.Selection.addSelection(obj)
                except (AttributeError, RuntimeError):
                    pass
        except (AttributeError, RuntimeError):
            pass


# ──────────────────────────────────────────────────────────────────────
# Wire-string tessellation memo (perf levers P3 + P4)
# ──────────────────────────────────────────────────────────────────────
class _WireStringMemo:
    """Per-import memo for Part.makeWireString.

    Dense pages repeat identical (text, font, size) spans (369 -> 266 unique
    on the owner's dense chart, P3) and Draft's ShapeString.execute
    re-tessellates the "M" cap-height probe — plus the sticky-font "L" probe
    on faces paths — on every execute (P4). FreeType tessellation is
    deterministic for a given (string, font file, size, tracking), so repeat
    calls return fresh copies of the first result. Copies are returned in
    both directions (never the cached originals) because ShapeString.execute
    translates the returned wires in place. Installed per import run and
    always restored, so no state outlives an import.
    """

    def __init__(self, part_module):
        self._part = part_module
        self._original = part_module.makeWireString
        self._cache: Dict[Tuple[str, str, float, float], list] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def _copy_result(result):
        return [[wire.copy() for wire in char] for char in result]

    def __call__(self, *args, **kwargs):
        if kwargs or len(args) != 4:
            return self._original(*args, **kwargs)
        string, font_file, size, tracking = args
        try:
            key = (str(string), str(font_file), float(size), float(tracking))
        except (TypeError, ValueError):
            return self._original(*args, **kwargs)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return self._copy_result(cached)
        result = self._original(*args, **kwargs)
        try:
            self._cache[key] = self._copy_result(result)
        except (AttributeError, RuntimeError, TypeError):
            return result  # uncacheable result shape — pass through untouched
        self.misses += 1
        return result

    def install(self):
        self._part.makeWireString = self

    def restore(self):
        if getattr(self._part, "makeWireString", None) is self:
            self._part.makeWireString = self._original
        self._cache.clear()


@contextmanager
def _wirestring_memo_scope(opts: Optional["ImportOptions"] = None):
    """Install the makeWireString memo for one import run (always restored)."""
    if Part is None or not callable(getattr(Part, "makeWireString", None)):
        yield None
        return
    memo = _WireStringMemo(Part)
    memo.install()
    try:
        yield memo
    finally:
        memo.restore()
        if opts is not None:
            try:
                opts.wirestring_cache_stats = {
                    "hits": int(memo.hits),
                    "misses": int(memo.misses),
                }
            except (AttributeError, TypeError):
                pass


def _memoized_wirestrings(func):
    """Run an import entry point inside a makeWireString memo scope."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        opts = kwargs.get("opts")
        if opts is None:
            for value in args:
                if isinstance(value, ImportOptions):
                    opts = value
                    break
        with _wirestring_memo_scope(opts):
            return func(*args, **kwargs)

    return wrapper


# ──────────────────────────────────────────────────────────────────────
# Page importer
# ──────────────────────────────────────────────────────────────────────
@_memoized_wirestrings
def import_pdf_page(pdf_path: str, page_num: int = 1,
                    opts: Optional[ImportOptions] = None,
                    autofit: bool = True):
    """Import a single PDF page into the active FreeCAD document."""
    if opts is None:
        opts = ImportOptions(ignore_images=not IMAGE_WB)
    _reset_import_run_state(opts)
    fc_doc = _ensure_doc()  # Store reference — don't rely on ActiveDocument later
    baseline_objects = _document_objects(fc_doc)
    baseline_object_ids = {id(host_obj) for host_obj in baseline_objects}
    baseline_object_names = {_host_object_id(host_obj) for host_obj in baseline_objects}

    # Validate PDF before opening
    from pdfcadcore.fitz_loader import PdfOpenError, safe_open

    try:
        pdf_doc = safe_open(pdf_path)
    except PdfOpenError:
        raise
    transaction_open = False
    try:
        fc_doc.openTransaction("Import PDF Page")
        transaction_open = True
        try:
            result = _import_pdf_page_inner(pdf_doc, pdf_path, page_num, opts, fc_doc)
            fc_doc.commitTransaction()
            transaction_open = False
        except Exception as failure:
            created_objects = [
                host_obj
                for host_obj in _document_objects(fc_doc)
                if id(host_obj) not in baseline_object_ids
                and _host_object_id(host_obj) not in baseline_object_names
            ]
            created_ids = [_host_object_id(host_obj) for host_obj in created_objects]
            abort_errors: List[str] = []
            if transaction_open:
                try:
                    fc_doc.abortTransaction()
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    abort_errors.append("abortTransaction: %s" % exc)
                transaction_open = False
            removed_by_abort = [
                entity_id
                for host_obj, entity_id in zip(created_objects, created_ids)
                if entity_id and fc_doc.getObject(entity_id) is not host_obj
            ]
            cleanup = _remove_post_baseline_document_objects(
                fc_doc, baseline_object_ids, baseline_object_names
            )
            rollback_created = list(dict.fromkeys(
                created_ids + list(cleanup.get("created_entity_ids") or [])
            ))
            rollback_removed = list(dict.fromkeys(
                removed_by_abort + list(cleanup.get("removed_entity_ids") or [])
            ))
            cleanup_errors = abort_errors + list(cleanup.get("cleanup_errors") or [])
            live_ids = list(cleanup.get("live_post_baseline_entity_ids") or [])
            rollback = {
                "created_entity_ids": rollback_created,
                "removed_entity_ids": rollback_removed,
                "live_post_baseline_entity_ids": live_ids,
                "cleanup_errors": cleanup_errors,
                "cleanup_complete": bool(
                    not cleanup_errors
                    and not live_ids
                    and set(rollback_created).issubset(set(rollback_removed))
                ),
            }
            if isinstance(failure, TextRepresentationFailure):
                failure.attempt["created_entity_ids"] = list(rollback_created)
                failure.attempt["removed_entity_ids"] = list(rollback_removed)
                failure.attempt["cleanup_complete"] = rollback["cleanup_complete"]
                failure.attempt["rollback"] = rollback
            if not rollback["cleanup_complete"] and not isinstance(
                failure, TextRepresentationFailure
            ):
                raise RuntimeError(
                    "Page import failed and rollback was incomplete: %s" % rollback
                ) from failure
            raise
    finally:
        pdf_doc.close()

    if autofit:
        _autofit_import_view(fc_doc)

    return result


def _import_pdf_page_inner(pdf_doc, pdf_path, page_num, opts, fc_doc):
    """Inner implementation — pdf_doc is guaranteed to be closed by caller."""
    if pdf_doc.is_encrypted:
        raise ValueError(
            "This PDF is encrypted and cannot be imported. "
            "Please remove the encryption (e.g., print to a new PDF) and try again.")
    if page_num < 1 or page_num > len(pdf_doc):
        raise ValueError(f"Page {page_num} out of range 1..{len(pdf_doc)}")

    opts._provenance_page = int(page_num)

    page = pdf_doc.load_page(page_num - 1)
    # PyMuPDF drawing/text coordinates are in unrotated crop-box space.  Apply
    # its authoritative rotation matrix once, then flip the displayed page's Y.
    try:
        opts._page_rotation_matrix = tuple(float(v) for v in page.rotation_matrix)
    except (AttributeError, TypeError, ValueError):
        opts._page_rotation_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    page_w = float(page.rect.width)
    page_h = float(page.rect.height)
    scale = (MM_PER_PT if opts.scale_to_mm else 1.0) * opts.user_scale
    page_area_units = max(abs(page_w * scale * page_h * scale), 1e-9)

    # Top-level group
    top_group = None
    if opts.create_top_group:
        top_group = fc_doc.addObject(
            "App::DocumentObjectGroup", f"PDF_Page_{page_num}")

    # ── Layer / color grouping ──
    use_ocg = False
    if opts.layer_mode in ("auto", "ocg"):
        try:
            ocgs = pdf_doc.get_ocgs()
            use_ocg = bool(ocgs)
        except (RuntimeError, AttributeError, ValueError):
            use_ocg = False

    group_by_color = False
    if opts.layer_mode == "color":
        group_by_color = True
    elif opts.layer_mode == "none":
        group_by_color = False
    elif opts.layer_mode == "ocg":
        group_by_color = False
    else:  # auto
        group_by_color = opts.group_by_color and not use_ocg

    color_groups: Dict[Tuple[float, float, float], object] = {}
    layer_groups: Dict[str, object] = {}

    def _parent_for(stroke_rgb, layer_name):
        parent = top_group or fc_doc
        if use_ocg and layer_name:
            if layer_name not in layer_groups:
                layer_groups[layer_name] = _make_group(parent, f"Layer_{layer_name}", fc_doc)
            return layer_groups[layer_name]
        if group_by_color and stroke_rgb is not None:
            key = stroke_rgb
            if key not in color_groups:
                r, g, b = key
                label = f"Color_{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}"
                color_groups[key] = _make_group(parent, label, fc_doc)
            return color_groups[key]
        return parent

    # ── Progress dialog (created early to cover all phases) ──
    _import_start = time.time()
    progress = None
    QtWidgets = None
    try:
        if FreeCAD.GuiUp:
            from PySide6 import QtWidgets, QtCore
    except ImportError:
        try:
            from PySide2 import QtWidgets, QtCore
        except ImportError:
            QtWidgets = None

    if FreeCAD.GuiUp and QtWidgets:
        try:
            progress = QtWidgets.QProgressDialog(
                f"Importing PDF page {page_num}...", "Cancel", 0, 100)
            progress.setWindowTitle("PDF Vector Importer")
            progress.setMinimumDuration(500)  # only show if > 500ms
            progress.setWindowModality(QtCore.Qt.WindowModal)
            progress.setValue(0)
        except (AttributeError, RuntimeError, TypeError):
            progress = None

    def _progress_check_cancel():
        """Check if user cancelled; closes dialog and returns True if cancelled."""
        if progress and progress.wasCanceled():
            _warn("Import cancelled by user")
            progress.close()
            return True
        return False

    def _progress_update(value, label):
        """Update progress dialog value and label, process events."""
        if not progress:
            return
        elapsed = time.time() - _import_start
        progress.setValue(value)
        progress.setLabelText(f"{label}  [{elapsed:.1f}s]")
        try:
            QtWidgets.QApplication.processEvents()
        except (AttributeError, RuntimeError):
            pass

    # ── Vector drawings ──
    try:
        drawings = page.get_drawings()
    except Exception as e:
        _warn(f"get_drawings() failed: {e}")
        drawings = []
    n_drawings = len(drawings)
    try:
        n_images = len(page.get_images(full=True))
    except Exception as e:
        _warn(f"get_images() failed: {e}")
        n_images = 0
    if opts.verbose:
        _msg(f"PDF page {page_num}: {n_drawings} drawing groups, "
             f"{n_images} embedded images found")

    # ── Determine effective import mode ──
    effective_mode = opts.import_mode
    n_text_blocks: Optional[int] = 0
    if effective_mode == "auto":
        # Auto-detect: profile the page content to choose best mode
        _progress_update(2, "Analyzing page content...")
        try:
            # blocks is far cheaper than dict on text-heavy shop drawings.
            blocks = page.get_text("blocks") or []
            n_text_blocks = sum(1 for b in blocks if len(b) >= 7 and b[6] == 0)
        except Exception as e:
            _warn(f"get_text(blocks) failed during auto-mode: {e}")
            n_text_blocks = None

        # Build lightweight vector density metrics once so multiple auto rules
        # can reuse the same profile without rescanning the page.
        vg_stats = _vector_group_stats(
            drawings,
            page_area=(page.rect.width * page.rect.height)
        ) if n_drawings > 0 else {}

        # Word extraction is only needed for heavy vector pages where we need
        # to distinguish CAD drawings from text-outline floods.
        n_words = 0
        if n_drawings >= AUTO_GLYPH_DRAWING_THRESHOLD:
            try:
                n_words = len(page.get_text("words"))
            except (RuntimeError, ValueError, TypeError, OSError):
                n_words = 0

        glyph_flood = _looks_like_vector_glyph_flood(
            n_drawings, int(n_text_blocks or 0), n_words, vg_stats)
        fill_art_flood = _looks_like_fill_art_flood(n_drawings, vg_stats)

        _flood_reason = ""  # human-readable explanation for the log

        if n_drawings < 5 and n_text_blocks == 0:
            # Scanned / pure raster page — no usable vector content
            effective_mode = "raster"
            _flood_reason = "scanned/raster page (no usable vector content)"

        elif n_drawings < 5 and n_text_blocks is not None and n_text_blocks > 0:
            # Text-only or text-dominant page. Preserve editable text; if a
            # raster image is present, keep it as a background.
            effective_mode = "hybrid" if n_images > 0 else "vector"
            _flood_reason = "text-only page — preserving editable text"

        elif glyph_flood:
            # Vectorized text/map-art flood: huge counts of tiny filled groups.
            # Preserve only a raster appearance by default; if substantial
            # stroked vectors exist, keep a hybrid overlay.
            if vg_stats.get("stroke_ratio", 0.0) <= AUTO_GLYPH_STROKE_SPARSE_RATIO:
                effective_mode = "raster"
            else:
                effective_mode = "hybrid"
            _flood_reason = (
                f"vector glyph flood — "
                f"{n_drawings} groups, "
                f"fill-only={vg_stats.get('fill_only_ratio', 0.0):.0%}, "
                f"tiny-rect={vg_stats.get('tiny_rect_ratio', 0.0):.0%}, "
                f"text_blocks={n_text_blocks}"
            )

        elif fill_art_flood:
            # Map / illustrated-art flood: dominated by filled decorative shapes
            # (garden beds, terrain fills, tree canopies, etc.) with few strokes.
            # Importing as vectors creates unusable geometry — use raster instead.
            # If a meaningful stroke layer exists, hybrid preserves those lines.
            if vg_stats.get("stroke_ratio", 0.0) > AUTO_GLYPH_STROKE_SPARSE_RATIO:
                effective_mode = "hybrid"
            else:
                effective_mode = "raster"
            _flood_reason = (
                f"fill-art flood — "
                f"{n_drawings} groups, "
                f"fill-only={vg_stats.get('fill_only_ratio', 0.0):.0%}, "
                f"strokes={vg_stats.get('stroke_ratio', 0.0):.0%} "
                f"(map/decorative PDF — vectors would be unusable geometry)"
            )

        elif (n_drawings > 3000 and n_images > 20
              and vg_stats.get("stroke_ratio", 1.0) <= 0.35):
            # GIS / topo PDF: dense imagery with sparse linework (low stroke ratio).
            # Garden/CAD maps with many strokes + tiled photos must stay vector/hybrid.
            effective_mode = "raster"
            _flood_reason = (
                f"GIS/topo map — {n_drawings} vector groups over "
                f"{n_images} embedded images "
                f"(stroke_ratio={vg_stats.get('stroke_ratio', 0.0):.0%})"
            )

        elif n_images > 0 and n_drawings > 0:
            # Has both images and vectors — hybrid gives best result
            effective_mode = "hybrid"

        else:
            effective_mode = "vector"

        _default_reasons = {
            "vector": "Standard vector content",
            "hybrid": "Vectors + embedded raster imagery",
            "raster": "Raster rendering selected",
        }
        auto_reason = _flood_reason or _default_reasons.get(effective_mode, "")
        opts.auto_resolved_mode = effective_mode
        opts.auto_reason = auto_reason

        if opts.verbose:
            if _flood_reason:
                _msg(
                    f"Page {page_num}: smart mode override — {_flood_reason}"
                )
            _msg(f"Page {page_num}: auto-detected mode = {effective_mode}"
                 + (" (use Import Mode = Vectors to override)"
                    if effective_mode == "raster" and _flood_reason else ""))

    prior_effective_mode = effective_mode
    effective_mode, auto_raster_text_overlay = _resolve_raster_text_contract_mode(
        effective_mode,
        n_text_blocks,
        opts,
    )
    if prior_effective_mode == "raster" and (
        effective_mode != prior_effective_mode or auto_raster_text_overlay
    ):
        opts.auto_resolved_mode = effective_mode
        opts.auto_reason = (
            str(opts.auto_reason or "Raster content strategy").rstrip()
            + "; requested text representation contract retained"
        )

    if _progress_check_cancel():
        if progress:
            progress.close()
        fc_doc.recompute()
        return top_group, None

    placed_full_page_raster_background = False
    full_page_raster_result = None

    # ── Raster-only mode, optionally with the separately requested text layer ──
    if _should_place_full_page_raster(effective_mode):
        _record_raster_page(opts, opts.auto_reason or "raster mode")
        _msg(f"Page {page_num}: rendering at {opts.raster_dpi} DPI (raster mode)")
        _progress_update(5, f"Rendering raster image at {opts.raster_dpi} DPI...")
        full_page_raster_result = _import_page_as_raster(
            pdf_doc, page, page_num, page_h, opts, scale,
            top_group or fc_doc, fc_doc)
        if auto_raster_text_overlay:
            placed_full_page_raster_background = True
            drawings = []
            n_drawings = 0
            effective_mode = "raster_text_overlay"
            _msg(
                f"Page {page_num}: raster background placed; rendering requested "
                f"{opts.text_mode} text representation"
            )
        else:
            text_entity_info = None
            if (
                bool(getattr(opts, "import_text", False))
                and str(getattr(opts, "text_mode", "") or "").strip().lower()
                == "raster"
            ):
                text_entity_info = _record_explicit_page_raster_delivery(
                    opts,
                    page_num=int(page_num),
                    raster_result=full_page_raster_result,
                )
            if progress:
                progress.setValue(100)
                progress.close()
            fc_doc.recompute()
            _msg(f"Page {page_num}: imported as raster image")
            return top_group, text_entity_info

    # ── Hybrid mode: preserve structural vectors and genuine source images ──
    if effective_mode == "hybrid":
        _msg(
            f"Page {page_num}: preserving vector geometry and genuine embedded "
            "images without a complete-page masking raster"
        )
        # Fall through to vector import; embedded images are imported below.

    # ── Legacy raster fallback (vectors mode, backwards compat) ──
    if effective_mode == "vector" and opts.raster_fallback and n_drawings < 5:
        tdict = page.get_text("dict")
        n_text = sum(1 for b in tdict.get("blocks", []) if b.get("type") == 0)
        if n_text == 0:
            _msg(f"Page {page_num}: appears to be scanned/raster — "
                 f"rendering at {opts.raster_dpi} DPI")
            _progress_update(5, f"Rendering raster image at {opts.raster_dpi} DPI...")
            _record_raster_page(
                opts,
                "legacy_vector_raster_fallback: scanned/raster page"
            )
            full_page_raster_result = _import_page_as_raster(
                pdf_doc, page, page_num, page_h, opts, scale,
                top_group or fc_doc, fc_doc)
            if _raster_page_requires_text_contract_probe(opts):
                placed_full_page_raster_background = True
                drawings = []
                n_drawings = 0
                effective_mode = "raster_text_overlay"
                _msg(
                    f"Page {page_num}: page Raster placed provisionally; "
                    f"proving requested {opts.text_mode} representation contract"
                )
            else:
                if progress:
                    progress.setValue(100)
                    progress.close()
                fc_doc.recompute()
                _msg(f"Page {page_num}: imported as raster image")
                return top_group, None

    # ── Hatch detection ──
    hatch_indices = set()
    hatch_drawings = []
    if opts.hatch_mode != "import" and n_drawings > 20:
        try:
            import PDFHatchDetector
            hatch_indices = PDFHatchDetector.detect(drawings)
            if hatch_indices:
                n_hatch = len(hatch_indices)
                if opts.verbose:
                    _msg(f"Page {page_num}: {n_hatch} hatch lines detected "
                         f"(mode: {opts.hatch_mode})")
                if opts.hatch_mode == "skip":
                    drawings = [d for i, d in enumerate(drawings)
                                if i not in hatch_indices]
                    n_drawings = len(drawings)
                elif opts.hatch_mode == "group":
                    hatch_drawings = [d for i, d in enumerate(drawings)
                                      if i in hatch_indices]
                    drawings = [d for i, d in enumerate(drawings)
                                if i not in hatch_indices]
                    n_drawings = len(drawings)
        except (RuntimeError, ValueError, TypeError, AttributeError, KeyError, IndexError) as e:
            _warn(f"Hatch detection failed: {e}")

    obj_count = 0

    # ── Heavy-page detection ──
    # When a page has a huge number of drawing groups, automatically engage
    # safe-mode behavior: larger compound batches, throttled progress updates,
    # and guarded arc fitting.  This keeps vector import fully intact but
    # stops FreeCAD from drowning in per-object GUI overhead.
    _is_heavy = (opts.heavy_page_threshold > 0
                 and n_drawings > opts.heavy_page_threshold)
    if _is_heavy and opts.verbose:
        _msg(f"Page {page_num}: heavy page detected ({n_drawings} groups > "
             f"{opts.heavy_page_threshold}) — engaging safe-mode batching")

    # ── Compound batching state ──
    # Collect shapes in memory and commit them as Part::Compound objects
    # instead of one Part::Feature per wire.  This reduces GDI handle count
    # by ~batch_size× while keeping every vector path in the document.
    _batch_size = opts.compound_batch_size if opts.compound_batch_size > 0 else 0
    # Heavy pages get a larger batch to further reduce object count
    if _is_heavy and _batch_size:
        _batch_size = max(_batch_size, 500)
    # Batch by (parent, color, width, dash_style) so styling is preserved
    _batch_shapes: Dict[str, List] = {}   # style_key → list of shapes
    _batch_parents: Dict[str, object] = {}  # style_key → parent object
    _batch_styles: Dict[str, Tuple] = {}   # style_key → (stroke_rgb, fill_rgb, width, dashes)
    _batch_idx: Dict[str, int] = {}        # parent_key → compound index

    def _flush_batch(style_key: str = None, force: bool = False):
        """Flush accumulated shapes into Part::Compound objects."""
        nonlocal obj_count
        keys = [style_key] if style_key else list(_batch_shapes.keys())
        for key in keys:
            shapes = _batch_shapes.get(key, [])
            if not shapes:
                continue
            if not force and len(shapes) < _batch_size:
                continue
            parent = _batch_parents[key]
            parent_name = parent.Name if hasattr(parent, 'Name') else str(id(parent))
            idx = _batch_idx.get(parent_name, 0) + 1
            _batch_idx[parent_name] = idx
            stroke_rgb, fill_rgb, width, dashes = _batch_styles.get(key, (None, None, None, None))
            try:
                compound = Part.makeCompound(shapes)
                obj = fc_doc.addObject("Part::Feature", f"Batch_{idx}")
                obj.Shape = compound
                _apply_style(obj, stroke_rgb, fill_rgb, width, dashes, opts)
                parent.addObject(obj)
                obj_count += 1
            except (RuntimeError, ValueError, TypeError) as e:
                # Fallback: create individually if compound fails
                _warn(f"Compound batch failed ({len(shapes)} shapes): {e}")
                for shp in shapes:
                    try:
                        obj = fc_doc.addObject("Part::Feature", "Wire")
                        obj.Shape = shp
                        _apply_style(obj, stroke_rgb, fill_rgb, width, dashes, opts)
                        parent.addObject(obj)
                        obj_count += 1
                    except (RuntimeError, ValueError, TypeError):
                        pass
            _batch_shapes[key] = []

    def _add_to_batch(shape, parent, stroke_rgb, fill_rgb, width, dashes):
        """Add a shape to the batch or create immediately if batching disabled."""
        nonlocal obj_count
        if not _batch_size:
            # No batching — original behavior
            obj = fc_doc.addObject("Part::Feature", "Wire")
            obj.Shape = shape
            _apply_style(obj, stroke_rgb, fill_rgb, width, dashes, opts)
            parent.addObject(obj)
            obj_count += 1
            return
        parent_name = parent.Name if hasattr(parent, 'Name') else str(id(parent))
        # Build a style key so shapes with different visual styles stay separate
        dash_key = tuple(dashes) if dashes else ()
        style_key = f"{parent_name}|{stroke_rgb}|{fill_rgb}|{width}|{dash_key}"
        if style_key not in _batch_shapes:
            _batch_shapes[style_key] = []
            _batch_parents[style_key] = parent
            _batch_styles[style_key] = (stroke_rgb, fill_rgb, width, dashes)
        _batch_shapes[style_key].append(shape)
        if len(_batch_shapes[style_key]) >= _batch_size:
            _flush_batch(style_key, force=True)

    # ── Progress update frequency ──
    # On heavy pages, throttle to every 500 paths instead of 100.
    # This alone prevents thousands of Qt timer allocations that cause
    # the GDI handle exhaustion.
    _progress_interval = 500 if _is_heavy else 100

    # Update progress range now that we know the geometry count.
    # Layout: 0-9 = pre-analysis, 10-79 = geometry, 80-89 = text,
    #         90-95 = batching/cleanup, 96-100 = final placement.
    if progress:
        progress.setMaximum(100)
    _progress_update(10, f"Processing geometry... 0/{n_drawings}")

    for pg_idx, path_group in enumerate(drawings):
        # Throttled progress updates — every 500 on heavy pages, 100 otherwise.
        # Each processEvents() call allocates Qt timers; doing it 19k× is
        # what exhausts Windows GDI handles.
        if progress and pg_idx % _progress_interval == 0:
            geo_pct = 10 + int(69 * pg_idx / max(n_drawings, 1))
            _progress_update(
                geo_pct,
                f"Processing geometry... {pg_idx}/{n_drawings}")
            if progress.wasCanceled():
                _warn("Import cancelled by user")
                # Flush any pending batches before returning
                if _batch_size:
                    _flush_batch(force=True)
                progress.close()
                fc_doc.recompute()
                return top_group, None

        items = path_group.get("items", [])
        if not items:
            continue

        # PyMuPDF may include clip/group container entries in drawing streams.
        # These are not visible edges and should never become CAD geometry.
        grp_type = str(path_group.get("type", "") or "").lower()
        if grp_type in {"clip", "group"}:
            continue

        stroke = path_group.get("color") or path_group.get("stroke")
        stroke_rgb = _optional_color(stroke)
        fill = path_group.get("fill")
        fill_rgb = _optional_color(fill)
        close_path = path_group.get("closePath", False)
        width = _as_float(path_group.get("width") or path_group.get("lineWidth"))
        dashes, dash_phase = _parse_dashes(path_group.get("dashes"))  # noqa: F841 — dash_phase stored for QA/adapter use; FC DrawStyle has no phase param
        layer_name = path_group.get("oc") or path_group.get("layer")

        # ── Skip invisible / clipping paths ──
        # Paths with no stroke AND no fill are PDF clipping boundaries — they
        # define mask regions, not visible geometry.  Drawing them produces
        # large arcs/rectangles that extend beyond the page and clutter the view.
        if stroke is None and fill is None:
            continue

        # ── Skip page-sized background fills ──
        # Some PDFs include a full-page rectangle as a background fill.
        # These add no useful geometry and obscure the actual drawing content.
        grp_rect = path_group.get("rect")
        if grp_rect and _is_rect(grp_rect):
            grp_area = abs(grp_rect.width * grp_rect.height)
            page_area = page.rect.width * page.rect.height
            if grp_area > page_area * 0.95:
                continue

        parent = _parent_for(stroke_rgb or fill_rgb, layer_name)

        # Build edges per sub-path
        current_pt: Optional[Vector] = None
        sub_edges: List = []
        wires_edges: List[List] = []

        def flush_sub(close_flag: bool, _wires=wires_edges):
            nonlocal sub_edges, current_pt
            if sub_edges:
                _wires.append((sub_edges[:], close_flag))
            sub_edges = []
            current_pt = None

        for item in items:
            kind = item[0]
            data = item[1:]

            if kind == "m":  # moveto
                flush_sub(False)
                x, y = _parse_point(data)
                current_pt = _to_fc((x, y), page_h, opts, scale)

            elif kind == "l":  # lineto
                # PyMuPDF may give ('l', start_pt, end_pt) with BOTH points,
                # or ('l', end_pt) with just the destination.
                if len(data) >= 2 and _is_point(data[0]) and _is_point(data[1]):
                    # Two-point format: self-contained line segment
                    x0, y0 = _xy(data[0])
                    x1, y1 = _xy(data[1])
                    p_start = _to_fc((x0, y0), page_h, opts, scale)
                    p_end   = _to_fc((x1, y1), page_h, opts, scale)
                    seg = _len2d(p_start, p_end)
                    if seg > max(ZERO_TOL, opts.min_seg_len):
                        e = _edge_line(p_start, p_end)
                        if e:
                            sub_edges.append(e)
                    current_pt = p_end
                else:
                    # Single-point format: line from current_pt to destination
                    if current_pt is None:
                        continue
                    x, y = _parse_point(data)
                    p = _to_fc((x, y), page_h, opts, scale)
                    seg = _len2d(current_pt, p)
                    if seg > max(ZERO_TOL, opts.min_seg_len):
                        e = _edge_line(current_pt, p)
                        if e:
                            sub_edges.append(e)
                    current_pt = p

            elif kind == "c":  # cubic Bezier
                # PyMuPDF may give ('c', P0, P1, P2, P3) with 4 points
                # or ('c', P1, P2, P3) with 3 control/end points + implicit start
                if len(data) == 4 and all(_is_point(d) for d in data):
                    # Four-point format: all points explicit
                    x0, y0 = _xy(data[0])
                    x1, y1 = _xy(data[1])
                    x2, y2 = _xy(data[2])
                    x3, y3 = _xy(data[3])
                    p0 = _to_fc((x0, y0), page_h, opts, scale)
                    p1 = _to_fc((x1, y1), page_h, opts, scale)
                    p2 = _to_fc((x2, y2), page_h, opts, scale)
                    p3 = _to_fc((x3, y3), page_h, opts, scale)
                    current_pt = p0  # set current in case it was None
                else:
                    if current_pt is None:
                        continue
                    try:
                        (x1, y1), (x2, y2), (x3, y3) = _parse_cubic(data)
                    except (TypeError, ValueError, IndexError):
                        continue
                    p0 = current_pt
                    p1 = _to_fc((x1, y1), page_h, opts, scale)
                    p2 = _to_fc((x2, y2), page_h, opts, scale)
                    p3 = _to_fc((x3, y3), page_h, opts, scale)

                # Try arc reconstruction first
                arc = _arc_from_cubic(p0, p1, p2, p3, opts)
                if arc is not None:
                    e = _edge_arc(*arc)
                    if e is not None:
                        sub_edges.append(e)
                        current_pt = p3
                        continue

                # Fallback: linearize the cubic
                chord = max(ZERO_TOL, _len2d(p0, p3))
                N = max(4, min(opts.max_bezier_segments,
                               int(math.ceil(chord / max(ZERO_TOL, opts.curve_step_mm)))))
                prev = p0
                for i in range(1, N + 1):
                    t = i / float(N)
                    q = _bezier_point(p0, p1, p2, p3, t)
                    if _len2d(prev, q) > max(ZERO_TOL, opts.min_seg_len):
                        e = _edge_line(prev, q)
                        if e:
                            sub_edges.append(e)
                    prev = q
                current_pt = p3

            elif kind == "v":  # quadratic Bezier  (PDF rare but possible)
                if current_pt is None:
                    continue
                try:
                    (cx, cy), (ex, ey) = _parse_quad(data)
                except (TypeError, ValueError, IndexError):
                    continue
                p0 = current_pt
                # Promote quadratic to cubic:  CP1 = P0 + 2/3*(C-P0),  CP2 = P + 2/3*(C-P)
                ctrl = _to_fc((cx, cy), page_h, opts, scale)
                end  = _to_fc((ex, ey), page_h, opts, scale)
                cp1 = p0 + (ctrl - p0) * (2.0 / 3.0)
                cp2 = end + (ctrl - end) * (2.0 / 3.0)
                # Reuse cubic logic
                chord = max(ZERO_TOL, _len2d(p0, end))
                N = max(4, min(opts.max_bezier_segments,
                               int(math.ceil(chord / max(ZERO_TOL, opts.curve_step_mm)))))
                prev = p0
                for i in range(1, N + 1):
                    t = i / float(N)
                    q = _bezier_point(p0, cp1, cp2, end, t)
                    if _len2d(prev, q) > max(ZERO_TOL, opts.min_seg_len):
                        e = _edge_line(prev, q)
                        if e:
                            sub_edges.append(e)
                    prev = q
                current_pt = end

            elif kind == "y":  # curveto with final point == control point 2
                if current_pt is None:
                    continue
                try:
                    (x1, y1), (x3, y3) = _parse_quad(data)
                except (TypeError, ValueError, IndexError):
                    continue
                p0 = current_pt
                p1 = _to_fc((x1, y1), page_h, opts, scale)
                p3 = _to_fc((x3, y3), page_h, opts, scale)
                p2 = p3  # control point 2 == endpoint for 'y' command
                chord = max(ZERO_TOL, _len2d(p0, p3))
                N = max(4, min(opts.max_bezier_segments,
                               int(math.ceil(chord / max(ZERO_TOL, opts.curve_step_mm)))))
                prev = p0
                for i in range(1, N + 1):
                    t = i / float(N)
                    q = _bezier_point(p0, p1, p2, p3, t)
                    if _len2d(prev, q) > max(ZERO_TOL, opts.min_seg_len):
                        e = _edge_line(prev, q)
                        if e:
                            sub_edges.append(e)
                    prev = q
                current_pt = p3

            elif kind == "re":  # rectangle
                flush_sub(False)
                x, y, w, h = _parse_rect(data)
                if abs(w) < ZERO_TOL or abs(h) < ZERO_TOL:
                    continue
                c1 = _to_fc((x, y), page_h, opts, scale)
                c2 = _to_fc((x + w, y), page_h, opts, scale)
                c3 = _to_fc((x + w, y + h), page_h, opts, scale)
                c4 = _to_fc((x, y + h), page_h, opts, scale)
                edges = [_edge_line(c1, c2), _edge_line(c2, c3),
                         _edge_line(c3, c4), _edge_line(c4, c1)]
                edges = [e for e in edges if e is not None]
                wires_edges.append((edges, True))

            elif kind == "h":  # closePath
                flush_sub(True)
            # else: unknown command — silently skip

        # Flush any remaining sub-path
        flush_sub(close_path)

        # Post-process: detect polyline arcs and replace with true Part::Arc.
        # On heavy pages, guard arc fitting — only attempt on candidate chains
        # with a reasonable edge count.  Giant polyline runs (>64 edges) on
        # monster PDFs are almost certainly contour lines or map features, not
        # arcs from a CAD exporter.  The arc fitter still runs; it just skips
        # chains that are obviously not arc candidates.
        if opts.detect_arcs:
            processed = []
            for edges, is_closed in wires_edges:
                if _is_heavy and len(edges) > 200:
                    # Heavy-page guard: skip arc fitting on very long chains.
                    # Raised from 64 to 200 to preserve arc accuracy on complex
                    # shop drawings while still protecting against map contours.
                    processed.append((edges, is_closed))
                else:
                    new_edges = _polyline_edges_to_arcs(edges, opts)
                    processed.append((new_edges, is_closed))
            wires_edges = processed

        # Create FreeCAD objects from collected edges
        for edges, is_closed in wires_edges:
            want_face = ((opts.hatch_to_faces and fill is not None)
                         or (opts.make_faces and is_closed))
            if _batch_size and not want_face:
                # Batch wires into compounds to reduce GDI handle count
                try:
                    wire = Part.Wire(edges)
                    if is_closed and not wire.isClosed():
                        if wire.Vertexes:
                            p0 = wire.Vertexes[0].Point
                            pN = wire.Vertexes[-1].Point
                            if _len2d(_v(p0.x, p0.y), _v(pN.x, pN.y)) > ZERO_TOL:
                                closer = Part.LineSegment(pN, p0).toShape()
                                wire = Part.Wire(edges + [closer])
                    _add_to_batch(wire, parent, stroke_rgb, fill_rgb, width, dashes)
                except (RuntimeError, ValueError, TypeError, AttributeError):
                    pass
            else:
                # Faces and non-batchable shapes: create individually
                obj = _make_shape_obj(edges, is_closed, make_face=want_face, fc_doc=fc_doc)
                if obj is not None:
                    _apply_style(obj, stroke_rgb, fill_rgb, width, dashes, opts)
                    parent.addObject(obj)
                    obj_count += 1
                    try:
                        face_area = float(getattr(obj.Shape, "Area", 0.0) or 0.0)
                    except (AttributeError, TypeError, ValueError):
                        face_area = 0.0
                    if _model3d_should_extrude(
                        opts,
                        is_closed=is_closed,
                        fill=fill,
                        face_area=abs(face_area),
                        page_area=page_area_units,
                    ):
                        solid = _make_model3d_obj(edges, fc_doc=fc_doc)
                        if solid is not None and _extrude_model3d_obj(solid, opts):
                            _apply_style(solid, stroke_rgb, fill_rgb, width, dashes, opts)
                            parent.addObject(solid)
                            obj_count += 1
                            opts._model3d_solids = int(getattr(opts, "_model3d_solids", 0) or 0) + 1

    # ── Flush remaining batched shapes ──
    if _batch_size:
        total_pending = sum(len(v) for v in _batch_shapes.values())
        n_style_keys = len([k for k, v in _batch_shapes.items() if v])
        if total_pending > 0:
            _progress_update(80, f"Building compound 1/{n_style_keys}...")
        _flush_idx = 0
        for _fk in list(_batch_shapes.keys()):
            if _batch_shapes.get(_fk):
                _flush_idx += 1
                if n_style_keys > 1:
                    _progress_update(
                        80 + int(5 * _flush_idx / max(n_style_keys, 1)),
                        f"Building compound {_flush_idx}/{n_style_keys}...")
                if _progress_check_cancel():
                    fc_doc.recompute()
                    return top_group, None
                _flush_batch(_fk, force=True)
        if opts.verbose:
            total_batches = sum(_batch_idx.values())
            _msg(f"Page {page_num}: geometry batched into {total_batches} "
                 f"compound(s) (batch_size={_batch_size})")

    text_entity_info = None

    # ── Text import ──
    if opts.import_text and opts.text_mode != "none":
        _progress_update(86, "Importing text...")

        if _progress_check_cancel():
            fc_doc.recompute()
            return top_group, None
        text_group = _make_group(top_group or fc_doc, "Text", fc_doc)
        try:
            raw_tdict = page.get_text("dict")
        except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
            attempt = {
                "source_item_id": "p%d:page" % int(page_num),
                "requested_type": str(opts.text_mode),
                "attempted_type": str(opts.text_mode),
                "final_type": None,
                "outcome": "failed",
                "reason": "source_text_extraction_failed",
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
                "evidence": {
                    "exception": "%s: %s" % (exc.__class__.__name__, exc)
                },
            }
            opts.text_delivery_attempts.append(attempt)
            raise TextRepresentationFailure(
                "Source text extraction failed for page %d" % int(page_num),
                attempt,
            ) from exc

        pdf_sha256 = str(getattr(opts, "_pdf_sha256", "") or "")
        if re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None:
            pdf_sha256 = _pdf_file_sha256(pdf_path)
            opts._pdf_sha256 = pdf_sha256
        text_entity_info = _render_canonical_text_items(
            pdf_doc=pdf_doc,
            page=page,
            pdf_path=pdf_path,
            page_num=int(page_num),
            page_h=float(page_h),
            page_w=float(page.rect.width),
            scale=float(scale),
            fc_doc=fc_doc,
            parent_group=text_group,
            opts=opts,
            pdf_sha256=pdf_sha256,
            raw_tdict=raw_tdict,
        )
        if int(text_entity_info.get("source_item_count", 0) or 0) <= 0:
            if full_page_raster_result is None:
                full_page_raster_result = _import_page_as_raster(
                    pdf_doc,
                    page,
                    page_num,
                    page_h,
                    opts,
                    scale,
                    text_group,
                    fc_doc,
                )
                placed_full_page_raster_background = True
            try:
                text_entity_info = _record_no_source_text_page_fallback(
                    opts,
                    page_num=int(page_num),
                    pdf_sha256=pdf_sha256,
                    raw_tdict=raw_tdict,
                    raster_result=full_page_raster_result,
                )
            except Exception as exc:
                raise TextRepresentationFailure(
                    "No-source text page fallback could not be verified: %s" % exc,
                    {
                        "source_item_id": "p%d:page" % int(page_num),
                        "requested_type": str(opts.text_mode or ""),
                        "attempted_type": "raster",
                        "final_type": None,
                        "outcome": "failed",
                        "reason": "invalid_no_source_text_page_fallback",
                        "created_entity_ids": [],
                        "removed_entity_ids": [],
                        "cleanup_complete": True,
                        "evidence": {
                            "exception": "%s: %s" % (exc.__class__.__name__, exc)
                        },
                    },
                ) from exc
        obj_count += int(text_entity_info.get("count", 0) or 0)
        _progress_update(
            89,
            "Rendering %s (%d source items)..."
            % (opts.text_mode, text_entity_info["source_item_count"]),
        )

    # ── Build hatch group (if group mode) ──
    if hatch_drawings and opts.hatch_mode == "group":
        try:
            hatch_group = _make_group(top_group or fc_doc, "Hatching", fc_doc)
            for _pg_idx, path_group in enumerate(hatch_drawings):
                items = path_group.get("items", [])
                if not items:
                    continue
                stroke = path_group.get("color") or path_group.get("stroke")
                stroke_rgb = _optional_color(stroke)
                current_pt = None
                sub_edges = []
                for item in items:
                    kind = item[0]
                    data = item[1:]
                    if kind == "m":
                        if sub_edges:
                            try:
                                wire = Part.Wire(sub_edges)
                                obj = fc_doc.addObject("Part::Feature", "Hatch")
                                obj.Shape = wire
                                hatch_group.addObject(obj)
                                _apply_style(obj, stroke_rgb, None, None, None, opts)
                                obj_count += 1
                            except (RuntimeError, ValueError, TypeError, AttributeError):
                                pass
                            sub_edges = []
                        pt = data[0] if data else None
                        if pt and hasattr(pt, 'x'):
                            current_pt = _to_fc((pt.x, pt.y), page_h, opts, scale)
                    elif kind == "l" and current_pt is not None:
                        if len(data) >= 2 and _is_point(data[0]) and _is_point(data[1]):
                            p_start = _to_fc((_xy(data[0])), page_h, opts, scale)
                            p_end = _to_fc((_xy(data[1])), page_h, opts, scale)
                        else:
                            pt = data[0] if data else None
                            if pt and hasattr(pt, 'x'):
                                p_start = current_pt
                                p_end = _to_fc((pt.x, pt.y), page_h, opts, scale)
                            else:
                                continue
                        seg = _len2d(p_start, p_end)
                        if seg > ZERO_TOL:
                            e = _edge_line(p_start, p_end)
                            if e:
                                sub_edges.append(e)
                        current_pt = p_end
                if sub_edges:
                    try:
                        wire = Part.Wire(sub_edges)
                        obj = fc_doc.addObject("Part::Feature", "Hatch")
                        obj.Shape = wire
                        hatch_group.addObject(obj)
                        _apply_style(obj, stroke_rgb, None, None, None, opts)
                        obj_count += 1
                    except (RuntimeError, ValueError, TypeError):
                        pass
            # Default hatching to hidden
            try:
                if hasattr(hatch_group, "ViewObject"):
                    hatch_group.ViewObject.Visibility = False
            except (AttributeError, RuntimeError):
                pass
            if opts.verbose:
                _msg(f"Page {page_num}: {len(hatch_drawings)} hatch lines → "
                     f"Hatching group (hidden)")
        except (RuntimeError, ValueError, TypeError, AttributeError, IndexError) as e:
            _warn(f"Hatch group creation failed: {e}")

    # ── Embedded images ──
    if not opts.ignore_images and not placed_full_page_raster_background:
        try:
            img_group = _make_group(top_group or fc_doc, "Images", fc_doc)
            imglist = page.get_images(full=True)
            seen_xrefs = set()
            for img_info in imglist:
                xref = img_info[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)
                rects = page.get_image_rects(xref)
                if not rects:
                    continue
                try:
                    pix = fitz.Pixmap(pdf_doc, xref)
                    # Convert any non-plain-RGB source to RGB before saving PNG.
                    # This handles CMYK / DeviceN / grayscale / alpha safely.
                    cs = getattr(pix, "colorspace", None)
                    cs_n = None
                    try:
                        cs_n = int(getattr(cs, "n", 0)) if cs is not None else None
                    except (TypeError, ValueError):
                        cs_n = None
                    needs_rgb = (
                        pix.alpha
                        or pix.n != 3
                        or (cs_n is not None and cs_n != 3)
                    )
                    if needs_rgb:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    tmpdir = os.path.join(
                        FreeCAD.getUserAppDataDir(),
                        "Mod", "PDFVectorImporter", "temp")
                    os.makedirs(tmpdir, exist_ok=True)
                    img_path = os.path.join(tmpdir, f"img_p{page_num}_x{xref}.png")
                    pix.save(img_path)
                except (RuntimeError, OSError, ValueError, TypeError) as e:
                    _warn(f"Image xref {xref} extract failed: {e}")
                    continue
                for r in rects:
                    pt0 = _to_fc((r.x0, r.y0), page_h, opts, scale)
                    pt1 = _to_fc((r.x1, r.y1), page_h, opts, scale)
                    w = abs(pt1.x - pt0.x)
                    h = abs(pt1.y - pt0.y)
                    try:
                        ip = fc_doc.addObject(
                            "Image::ImagePlane", "Image")
                        ip.ImageFile = img_path
                        ip.XSize = w
                        ip.YSize = h
                        ip.Placement = Placement(
                            _v(min(pt0.x, pt1.x), min(pt0.y, pt1.y), 0),
                            Rotation())
                        img_group.addObject(ip)
                        obj_count += 1
                    except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as e:
                        _warn(f"Image placement failed: {e}")
        except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as e:
            _warn(f"Image import failed: {e}")

    # ── Final cleanup / placement ──
    _progress_update(96, "Placing objects in document...")

    # Clean up empty groups (Text, Images, Color groups with no content)
    if top_group and hasattr(top_group, "Group"):
        for child in top_group.Group[:]:
            if (child.isDerivedFrom("App::DocumentObjectGroup")
                    and hasattr(child, "Group") and not child.Group):
                top_group.removeObject(child)
                fc_doc.removeObject(child.Name)

    _progress_update(98, "Placing objects in document...")

    # Close progress
    if progress:
        progress.setValue(100)
        progress.close()

    fc_doc.recompute()
    elapsed_total = time.time() - _import_start
    _msg(f"Page {page_num}: {obj_count} objects created in {elapsed_total:.1f}s")
    return top_group, text_entity_info


# ──────────────────────────────────────────────────────────────────────
# Multi-page entry point
# ──────────────────────────────────────────────────────────────────────
def _normalize_page_arrangement(raw: str | None) -> str:
    key = (raw or "spread").strip().lower()
    if key in {"overlay", "touch", "compact", "spread"}:
        return key
    return "spread"


def _normalize_page_gap_ratio(raw: float | None) -> float:
    try:
        ratio = float(raw if raw is not None else 0.20)
    except (TypeError, ValueError):
        ratio = 0.20
    return max(0.0, min(1.0, ratio))


def _page_stack_step(page_height: float, arrangement: str, gap_ratio: float) -> float:
    h = page_height if page_height > 0 else 1.0
    if arrangement == "overlay":
        return 0.0
    if arrangement == "touch":
        return h
    if arrangement == "compact":
        return h * (1.0 + gap_ratio)
    # "spread" — use gap_ratio (default 0.20 = 20% gap) + fixed minimum
    # to prevent pages from overlapping on dense/large-format drawings.
    min_gap_mm = 10.0  # minimum 10mm gap regardless of page size
    return h + max(h * gap_ratio, min_gap_mm)


def _reset_import_run_state(opts: ImportOptions) -> None:
    """Clear output evidence from a prior run while preserving user choices."""
    opts.phase_timings_ms.clear()
    opts.shapestring_skips.clear()
    opts.text_mode_fallbacks.clear()
    opts.text_delivered_counts.clear()
    opts.text_delivery_attempts.clear()
    opts.raster_page_count = 0
    opts.raster_fallback_reasons.clear()
    opts.auto_resolved_mode = None
    opts.auto_reason = None
    opts.resolved_scale = None
    opts.scale_hints.clear()
    opts._page_rotation_matrix = None
    opts._font_stage_failures = []
    opts._shapestring_font_paths = {}
    opts._shapestring_font_staging_sessions = []
    opts._report_extra = {}
    opts._model3d_solids = 0
    opts._model3d_intent = None
    opts._model3d_intent_feasible = False
    opts._model3d_text_evidence = []
    opts._bootstrap_text_items = []
    opts._pdf_sha256 = ""


def _remove_post_baseline_document_objects(
    fc_doc,
    baseline_object_ids: set,
    baseline_object_names: set,
) -> Dict[str, Any]:
    """Remove every object created after the import snapshot and verify absence."""
    post_baseline = [
        host_obj
        for host_obj in _document_objects(fc_doc)
        if id(host_obj) not in baseline_object_ids
        and _host_object_id(host_obj) not in baseline_object_names
    ]
    created_ids = [_host_object_id(host_obj) for host_obj in post_baseline]
    removed_ids: List[str] = []
    errors: List[str] = []
    for host_obj in reversed(post_baseline):
        entity_id = _host_object_id(host_obj)
        if not entity_id:
            errors.append("post-baseline object has no stable id")
            continue
        try:
            current = fc_doc.getObject(entity_id)
        except (AttributeError, RuntimeError, TypeError) as exc:
            current = None
            errors.append("getObject(%s): %s" % (entity_id, exc))
        if current is None:
            removed_ids.append(entity_id)
            continue
        if current is not host_obj:
            errors.append("post-baseline identity changed for %s" % entity_id)
            continue
        try:
            fc_doc.removeObject(entity_id)
            removed_ids.append(entity_id)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            errors.append("removeObject(%s): %s" % (entity_id, exc))
    try:
        fc_doc.recompute()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        errors.append("recompute: %s" % exc)
    live_post_baseline_ids = [
        _host_object_id(host_obj)
        for host_obj in _document_objects(fc_doc)
        if id(host_obj) not in baseline_object_ids
        and _host_object_id(host_obj) not in baseline_object_names
    ]
    cleanup_complete = bool(
        not errors
        and not live_post_baseline_ids
        and set(created_ids).issubset(set(removed_ids))
    )
    return {
        "created_entity_ids": created_ids,
        "removed_entity_ids": list(dict.fromkeys(removed_ids)),
        "live_post_baseline_entity_ids": live_post_baseline_ids,
        "cleanup_errors": errors,
        "cleanup_complete": cleanup_complete,
    }


@_memoized_wirestrings
def import_pdf(pdf_path: str, opts: Optional[ImportOptions] = None):
    """Import one or more pages from a PDF file."""
    if opts is None:
        opts = ImportOptions(ignore_images=not IMAGE_WB)
    fc_doc = _ensure_doc()
    t_import_start = time.perf_counter()
    _reset_import_run_state(opts)
    baseline_objects = _document_objects(fc_doc)
    baseline_object_ids = {id(host_obj) for host_obj in baseline_objects}
    baseline_object_names = {_host_object_id(host_obj) for host_obj in baseline_objects}
    try:
        opts._pdf_sha256 = _pdf_file_sha256(pdf_path)
    except OSError as exc:
        _err("Cannot read PDF for source identity: %s" % exc)
        return

    # Reset ID counter once at the start of a multi-page import
    try:
        from pdfcadcore.primitives import reset_ids
        reset_ids()
    except ImportError:
        pass

    # Clean up temp raster images from previous imports
    cleanup_temp_files()

    # Open PDF once to gather page count and heights (avoids triple-open + handle leaks)
    _unit_scale = (MM_PER_PT if opts.scale_to_mm else 1.0) * opts.user_scale
    page_height_scaled = 792 * _unit_scale  # default: US Letter height in points
    page_heights_scaled: Dict[int, float] = {}
    model3d_text_evidence: List[str] = []
    t_phase = time.perf_counter()
    try:
        from pdfcadcore.fitz_loader import PdfOpenError, safe_open

        with safe_open(pdf_path) as pdoc:
            total_pages = len(pdoc)
            # Default to all pages when no explicit page list is provided
            pages = opts.pages or list(range(1, total_pages + 1))
            if total_pages > 0:
                page_height_scaled = pdoc.load_page(0).rect.height * _unit_scale
            for p in pages:
                if 1 <= p <= total_pages:
                    try:
                        _page_for_meta = pdoc.load_page(p - 1)
                        page_heights_scaled[p] = _page_for_meta.rect.height * _unit_scale
                        try:
                            page_text = _page_for_meta.get_text("text") or ""
                            model3d_text_evidence.append(page_text)
                            for line in page_text.splitlines():
                                line = line.strip()
                                if line:
                                    opts._bootstrap_text_items.append(
                                        {"text": line, "page": p}
                                    )
                        except (RuntimeError, ValueError, AttributeError):
                            pass
                    except (ValueError, RuntimeError):
                        pass
        opts.phase_timings_ms["open_pdf_ms"] = (time.perf_counter() - t_phase) * 1000.0
        try:
            from pdfcadcore.model3d_intent import analyze_model3d_intent

            intent = analyze_model3d_intent(model3d_text_evidence, host_supports_3d=True)
            opts._model3d_intent = intent.to_dict()
            opts._model3d_intent_feasible = bool(intent.feasible)
            opts._model3d_text_evidence = list(model3d_text_evidence)
        except (ImportError, RuntimeError, TypeError, ValueError, AttributeError):
            opts._model3d_intent = {
                "feasible": False,
                "plates": [],
                "members": [],
                "skipped_reason": "3D intent analysis unavailable",
            }
            opts._model3d_intent_feasible = False
    except PdfOpenError as e:
        _err(str(e))
        return
    except (RuntimeError, OSError) as e:
        _err(f"Cannot open PDF: {e}")
        return

    try:
        pdf_doc_for_import = safe_open(pdf_path)
    except PdfOpenError as e:
        _err(str(e))
        return
    except (RuntimeError, OSError) as e:
        _err(f"Cannot open PDF: {e}")
        return

    # Wrap entire import in a FreeCAD transaction so Ctrl+Z undoes it in one step
    fc_doc.openTransaction("Import PDF")
    t_phase = time.perf_counter()
    try:
        imported_count = 0
        running_stack_offset = 0.0
        page_arrangement = _normalize_page_arrangement(getattr(opts, "page_arrangement", "spread"))
        page_gap_ratio = _normalize_page_gap_ratio(getattr(opts, "page_gap_ratio", 0.20))
        first_page = True
        all_text_entity_info = None
        for p in pages:
            if p < 1 or p > total_pages:
                _warn(f"Skipping out-of-range page {p} (PDF has {total_pages} pages)")
                continue
            try:
                _msg(f"Importing page {p}/{total_pages} ({imported_count+1} of {len(pages)})...")
                _, page_text_info = _import_pdf_page_inner(
                    pdf_doc_for_import,
                    pdf_path,
                    p,
                    opts,
                    fc_doc,
                )
                if page_text_info:
                    if all_text_entity_info is None:
                        all_text_entity_info = page_text_info.copy()
                    else:
                        all_text_entity_info["count"] += page_text_info.get("count", 0)
                        examples = page_text_info.get("examples", [])
                        if examples and len(all_text_entity_info["examples"]) < 3:
                            all_text_entity_info["examples"].extend(
                                examples[:3 - len(all_text_entity_info["examples"])]
                            )
                curr_page_height = page_heights_scaled.get(p, page_height_scaled)
                # Offset each page group downward so they don't overlap.
                # FreeCAD may rename the group (e.g., PDF_Page_2 → PDF_Page_2001)
                # so we search for the most recently created matching group.
                if len(pages) > 1 and not first_page:
                    running_stack_offset += _page_stack_step(
                        curr_page_height,
                        page_arrangement,
                        page_gap_ratio,
                    )
                    y_shift = -running_stack_offset
                    grp = None
                    for obj in reversed(fc_doc.Objects):
                        if (obj.Name.startswith(f"PDF_Page_{p}") and
                                obj.isDerivedFrom("App::DocumentObjectGroup")):
                            grp = obj
                            break
                    if grp and hasattr(grp, "Group"):
                        _msg(f"Offsetting {grp.Name} by Y={y_shift:.1f}")
                        for child in grp.Group:
                            try:
                                if hasattr(child, "Placement"):
                                    child.Placement.Base.y += y_shift
                                if hasattr(child, "Group"):
                                    for sub in child.Group:
                                        if hasattr(sub, "Placement"):
                                            sub.Placement.Base.y += y_shift
                            except (AttributeError, RuntimeError):
                                pass
                first_page = False
                imported_count += 1
            except TextRepresentationFailure:
                raise
            except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as e:
                _err(f"Failed to import page {p}: {e}\n{traceback.format_exc()}")
                raise
        fc_doc.commitTransaction()
        opts.phase_timings_ms["pages_import_ms"] = (time.perf_counter() - t_phase) * 1000.0
    except TextRepresentationFailure as failure:
        fc_doc.abortTransaction()
        rollback = _remove_post_baseline_document_objects(
            fc_doc, baseline_object_ids, baseline_object_names
        )
        report_extra = dict(getattr(opts, "_report_extra", {}) or {})
        report_extra["rollback"] = rollback
        opts._report_extra = report_extra
        elapsed_ms = (time.perf_counter() - t_import_start) * 1000.0
        opts.phase_timings_ms["pages_import_ms"] = (
            time.perf_counter() - t_phase
        ) * 1000.0
        try:
            failure_report = _write_terminal_representation_failure_report(
                pdf_path=pdf_path,
                opts=opts,
                total_pages=total_pages,
                pages_imported=imported_count,
                elapsed_ms=elapsed_ms,
                failure=failure,
            )
            _err(
                f"Import stopped because requested {opts.text_mode} could not be "
                f"delivered. Failure report: {failure_report}"
            )
        except (OSError, RuntimeError, TypeError, ValueError, ImportError) as report_error:
            _err(f"Terminal import failure report could not be written: {report_error}")
        if not rollback["cleanup_complete"]:
            failure.attempt["cleanup_complete"] = False
            failure.attempt["rollback"] = rollback
        raise
    except Exception as failure:
        fc_doc.abortTransaction()
        rollback = _remove_post_baseline_document_objects(
            fc_doc, baseline_object_ids, baseline_object_names
        )
        if not rollback["cleanup_complete"]:
            raise RuntimeError(
                "Import failed and rollback was incomplete: %s" % rollback
            ) from failure
        raise
    finally:
        try:
            pdf_doc_for_import.close()
        except (RuntimeError, AttributeError):
            pass

    t_phase = time.perf_counter()
    fc_doc.recompute()
    _autofit_import_view(fc_doc)
    opts.phase_timings_ms["postprocess_ms"] = (time.perf_counter() - t_phase) * 1000.0

    if opts.import_mode == "auto" and opts.auto_resolved_mode:
        _msg(
            f"Auto mode summary: {opts.auto_resolved_mode}"
            + (f" — {opts.auto_reason}" if opts.auto_reason else "")
        )

    # Round 5: merge per-page title-block scale into import_report (LC parity).
    try:
        from pdfcadcore.fitz_loader import safe_open as _scale_safe_open
        from pdfcadcore.resolved_scale import probe_page_scale

        with _scale_safe_open(pdf_path) as scale_doc:
            for p in pages:
                if p < 1 or p > total_pages:
                    continue
                _merge_page_scale_into_opts(
                    opts,
                    probe_page_scale(scale_doc.load_page(p - 1), p),
                )
    except ImportError:
        pass
    except (RuntimeError, OSError, ValueError, TypeError) as e:
        if opts.verbose:
            _warn(f"Scale detection pass skipped: {e}")

    try:
        report_path = opts.import_report_path or _default_import_report_path(pdf_path)
        fallback_used, fallback_reason = _report_fallback_state(opts)
        elapsed_ms = (time.perf_counter() - t_import_start) * 1000.0
        opts.phase_timings_ms["total_ms"] = elapsed_ms

        # Add text entity info to extra
        if not hasattr(opts, '_report_extra'):
            opts._report_extra = {}
        if all_text_entity_info:
            opts._report_extra['actual_text_entity_types'] = all_text_entity_info
        opts._report_extra["result_status"] = "success"
        opts._report_extra.pop("terminal_failure", None)

        if bool(getattr(opts, "model3d_semantic", False)):
            intent = getattr(opts, "_model3d_intent", None) or {}
            members = intent.get("members") if isinstance(intent, dict) else []
            if members:
                try:
                    from pdfcadcore.semantic_members import create_semantic_members

                    semantic_report = create_semantic_members(
                        list(members),
                        doc=fc_doc,
                    )
                    if not hasattr(opts, "_report_extra"):
                        opts._report_extra = {}
                    model3d_extra = dict(
                        getattr(opts, "_report_extra", {}).get("model_3d") or {}
                    )
                    model3d_extra["semantic_members"] = semantic_report
                    opts._report_extra["model_3d"] = model3d_extra
                except (ImportError, RuntimeError, TypeError, ValueError) as e:
                    if opts.verbose:
                        _warn(f"Semantic member generation skipped: {e}")

        imported_objects = _post_baseline_document_objects(
            _document_objects(fc_doc),
            baseline_object_ids,
            baseline_object_names,
        )
        host_inventory = _build_host_object_inventory(imported_objects)
        opts._report_extra["actual_host_object_inventory"] = host_inventory
        opts._report_extra["save_reopen_inventory"] = _save_reopen_host_object_inventory(
            fc_doc,
            host_inventory,
            baseline_object_names,
        )
        inventory_counts = dict(host_inventory.get("counts") or {})

        write_import_report(
            pdf_path=pdf_path,
            output_path=report_path,
            opts=opts,
            pages_imported=imported_count,
            total_pages=total_pages,
            primitive_count=int(inventory_counts.get("vector_primitives") or 0),
            text_count=_structural_text_delivery_count(
                opts,
                int((all_text_entity_info or {}).get("count", 0) or 0),
            ),
            image_count=int(inventory_counts.get("images") or 0),
            elapsed_ms=elapsed_ms,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )
        if opts.verbose:
            _msg(f"Import report: {report_path}")
    except (OSError, RuntimeError, TypeError, ValueError, ImportError) as e:
        _warn(f"Import report write failed: {e}")

    return True
