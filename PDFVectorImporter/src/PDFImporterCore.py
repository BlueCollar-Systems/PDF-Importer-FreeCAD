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
import stat
import sys
import tempfile
import time
import zipfile
import zlib
import xml.etree.ElementTree as ElementTree
from io import BytesIO
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple, Any

# Ensure bundled PyMuPDF is importable (skip namespace-only stubs in lib/)
_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

_mod_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _mod_root not in sys.path:
    sys.path.insert(0, _mod_root)
from pdfcadcore.fitz_loader import import_fitz as _import_fitz
from pdfcadcore.page_visual import (
    PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA,
    build_page_visual_fallback_proof_v2,
    capture_fresh_page_visual_authority,
    page_visual_fallback_proof_v2_verified,
    page_visual_source_observation_v2_verified,
)
from pdfcadcore.text_delivery_report import (
    build_text_representation_delivery as _build_shared_text_delivery,
)

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


class ImportCancelled(RuntimeError):
    """Typed signal that the user cancelled one atomic import attempt."""


class ImportLifecycleError(RuntimeError):
    """Typed rejection when an import cannot satisfy its acceptance contract."""


class ImportCleanupError(ImportLifecycleError):
    """Retryable failure to remove or restore attempt-owned runtime state."""

    retryable = True

    def __init__(self, message: str, *, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})

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
    # Explicit DPI is an output contract. Degradation is opt-in and is never
    # inferred from memory pressure or a failed renderer retry.
    allow_raster_dpi_degradation: bool = False
    # Import mode: "auto" | "vector" | "raster" | "hybrid"  (BCS-ARCH-001)
    #   auto    — detect scanned/image-heavy and vector-glyph-flood pages
    #   vectors — vector geometry only (original behavior)
    #   raster  — render full page as image, skip vectors
    #   hybrid  — vector geometry plus genuine embedded image occurrences
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
    # Source identities registered before any delivery attempt. This is the
    # independent obligation set used to catch missing, fabricated, or extra
    # ledger items; it must never be reconstructed from the attempt ledger.
    text_delivery_obligation_source_item_ids: List[str] = field(default_factory=list)
    # Independent page observations used to revalidate page-scoped visual
    # fallback proofs while building the report.  These are not text items.
    page_visual_source_observations: Dict[str, Dict[str, Any]] = field(
        default_factory=dict
    )
    # PyMuPDF extracts in unrotated crop-box coordinates.  The active page's
    # rotation matrix maps those coordinates to the displayed page exactly once.
    _page_rotation_matrix: Optional[Tuple[float, float, float, float, float, float]] = None
    # Scale detection telemetry for import_report cross-check (Round 5).
    resolved_scale: Optional[Dict[str, Any]] = None
    scale_hints: Dict[str, Any] = field(default_factory=dict)
    phase_timings_ms: Dict[str, float] = field(default_factory=dict)
    # One import attempt owns one immutable source byte snapshot. The original
    # path is display metadata only after initialization; every parser pass and
    # external helper is bound to these fields instead.
    _pdf_sha256: str = field(default="", repr=False)
    _pdf_source_bytes: Optional[bytes] = field(default=None, repr=False)
    _pdf_source_snapshot_path: Optional[str] = field(default=None, repr=False)
    _pdf_source_snapshot_owner: Any = field(default=None, repr=False, compare=False)
    _pdf_source_provenance: Dict[str, Any] = field(default_factory=dict, repr=False)
    _page_visual_authority: Any = field(default=None, repr=False, compare=False)
    _page_visual_session_anchor: Optional[Dict[str, Any]] = field(
        default=None,
        repr=False,
    )
    _page_raster_fallback_contexts: Dict[int, Any] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _source_cleanup_error: Optional[Dict[str, Any]] = field(
        default=None,
        repr=False,
    )
    _attempt_path_journal: Dict[str, Dict[str, Any]] = field(
        default_factory=dict,
        repr=False,
    )
    _import_cancellation_checkpoint: Any = field(
        default=None,
        repr=False,
        compare=False,
    )


def _default_import_report_path(pdf_path: str) -> str:
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    return os.path.join(tempfile.gettempdir(), f"{base}_import_report.json")


def _pdf_file_sha256(pdf_path: str) -> str:
    digest = hashlib.sha256()
    with open(pdf_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_protection_evidence(
    snapshot_path: Path,
    snapshot_root: Path,
) -> Dict[str, Any]:
    """Measure read-only and privacy protection without optimistic claims."""

    path = Path(snapshot_path)
    root = Path(snapshot_root)
    file_mode = stat.S_IMODE(path.stat().st_mode)
    root_mode = stat.S_IMODE(root.stat().st_mode)
    mode_blocks_write = not bool(
        file_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )
    write_open_rejected = False
    descriptor = None
    try:
        descriptor = os.open(str(path), os.O_WRONLY)
    except PermissionError:
        write_open_rejected = True
    except OSError:
        write_open_rejected = False
    finally:
        if descriptor is not None:
            os.close(descriptor)

    read_only_verified = bool(mode_blocks_write and write_open_rejected)
    if os.name == "nt":
        private = False
        private_verified = False
        privacy_method = "windows_acl_not_verified"
    else:
        private = not bool(root_mode & (stat.S_IRWXG | stat.S_IRWXO))
        private_verified = private
        privacy_method = "posix_directory_mode_bits"
    return {
        "snapshot_read_only": read_only_verified,
        "snapshot_read_only_verified": read_only_verified,
        "snapshot_mode_blocks_write": mode_blocks_write,
        "snapshot_write_open_rejected": write_open_rejected,
        "snapshot_private": private,
        "snapshot_private_verified": private_verified,
        "snapshot_privacy_method": privacy_method,
        "snapshot_protection_method": (
            "file_mode_bits_and_denied_write_open+" + privacy_method
        ),
    }


def _detach_pdf_source_authority(opts: ImportOptions) -> List[str]:
    """Remove every live capability that can re-read the retained source bytes."""

    errors: List[str] = []
    live_report = getattr(opts, "_live_import_report", None)
    if live_report is not None:
        try:
            live_report._page_visual_authority = None
        except (AttributeError, RuntimeError, TypeError) as exc:
            errors.append("live_report_authority_detach: %s" % exc)
        try:
            if getattr(live_report, "_page_visual_authority", None) is not None:
                errors.append("live report retained page-visual authority")
        except (AttributeError, RuntimeError, TypeError) as exc:
            errors.append("live_report_authority_verify: %s" % exc)
    opts._page_visual_authority = None
    opts._page_visual_session_anchor = None
    return errors


def _dispose_pdf_source_attempt(opts: ImportOptions) -> None:
    """Release an attempt snapshot, retaining ownership when cleanup must retry."""

    cleanup_errors = _detach_pdf_source_authority(opts)
    owner = getattr(opts, "_pdf_source_snapshot_owner", None)
    raw_path = getattr(opts, "_pdf_source_snapshot_path", None)
    snapshot_path = Path(raw_path) if isinstance(raw_path, str) and raw_path else None
    if snapshot_path is not None and snapshot_path.exists():
        try:
            snapshot_path.chmod(stat.S_IREAD | stat.S_IWRITE)
        except (OSError, RuntimeError, ValueError) as exc:
            cleanup_errors.append("snapshot_chmod: %s" % exc)
    if owner is not None:
        try:
            owner.cleanup()
        except (OSError, RuntimeError, AttributeError) as exc:
            cleanup_errors.append("snapshot_owner_cleanup: %s" % exc)
    elif snapshot_path is not None and snapshot_path.exists():
        cleanup_errors.append("snapshot owner is unavailable while snapshot still exists")
    if snapshot_path is not None and snapshot_path.exists():
        cleanup_errors.append("snapshot file still exists after cleanup")
    if cleanup_errors:
        details = {
            "retryable": True,
            "snapshot_path": str(snapshot_path) if snapshot_path is not None else None,
            "errors": cleanup_errors,
        }
        opts._source_cleanup_error = details
        raise ImportCleanupError(
            "PDF source snapshot cleanup failed and can be retried",
            details=details,
        )

    opts._pdf_sha256 = ""
    opts._pdf_source_bytes = None
    opts._pdf_source_snapshot_path = None
    opts._pdf_source_snapshot_owner = None
    opts._pdf_source_provenance = {}
    opts._source_cleanup_error = None


def _initialize_pdf_source_attempt(pdf_path: str, opts: ImportOptions) -> str:
    """Read the requested PDF exactly once and bind an attempt-owned snapshot."""

    _dispose_pdf_source_attempt(opts)
    source_path = Path(pdf_path)
    payload = bytes(source_path.read_bytes())
    if not payload:
        raise OSError("PDF source is empty")
    digest = hashlib.sha256(payload).hexdigest()
    owner = tempfile.TemporaryDirectory(prefix="bc_pdf_source_attempt_")
    snapshot_root = Path(owner.name)
    snapshot_path = snapshot_root / (digest + ".pdf")
    opts._pdf_sha256 = digest
    opts._pdf_source_bytes = payload
    opts._pdf_source_snapshot_path = str(snapshot_path)
    opts._pdf_source_snapshot_owner = owner
    opts._pdf_source_provenance = {
        "source_path": str(source_path.resolve()),
        "pdf_sha256": digest,
        "size_bytes": len(payload),
        "snapshot_path": str(snapshot_path),
        "snapshot_read_only": False,
        "snapshot_read_only_verified": False,
        "snapshot_private": False,
        "snapshot_private_verified": False,
        "snapshot_privacy_method": "not_measured",
        "snapshot_protection_method": "not_measured",
    }
    try:
        snapshot_root.chmod(stat.S_IRWXU)
        snapshot_path.write_bytes(payload)
        if (
            snapshot_path.stat().st_size != len(payload)
            or _pdf_file_sha256(str(snapshot_path)) != digest
        ):
            raise OSError("immutable PDF source snapshot verification failed")
        snapshot_path.chmod(stat.S_IREAD)
        protection = _snapshot_protection_evidence(snapshot_path, snapshot_root)
        opts._pdf_source_provenance.update(protection)
        if protection["snapshot_read_only_verified"] is not True:
            raise OSError("PDF source snapshot could not be made read-only")
        if os.name != "nt" and protection["snapshot_private_verified"] is not True:
            raise OSError("PDF source snapshot directory could not be made private")
    except Exception:
        _dispose_pdf_source_attempt(opts)
        raise
    return str(snapshot_path)


def _validated_pdf_source_bytes(opts: ImportOptions) -> bytes:
    """Return the exact attempt bytes only while their digest binding is valid."""

    payload = getattr(opts, "_pdf_source_bytes", None)
    digest = str(getattr(opts, "_pdf_sha256", "") or "")
    provenance = getattr(opts, "_pdf_source_provenance", None)
    if (
        type(payload) is not bytes
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or hashlib.sha256(payload).hexdigest() != digest
        or not isinstance(provenance, dict)
        or provenance.get("pdf_sha256") != digest
        or provenance.get("size_bytes") != len(payload)
    ):
        raise ValueError("PDF source bytes do not match the bound digest")
    return payload


def _validated_pdf_source_snapshot_path(opts: ImportOptions) -> str:
    """Return the external-tool path only while it matches the attempt bytes."""

    payload = _validated_pdf_source_bytes(opts)
    digest = str(opts._pdf_sha256)
    raw_path = getattr(opts, "_pdf_source_snapshot_path", None)
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("PDF source snapshot path is unavailable")
    snapshot_path = Path(raw_path)
    try:
        valid = bool(
            snapshot_path.is_file()
            and snapshot_path.stat().st_size == len(payload)
            and _pdf_file_sha256(str(snapshot_path)) == digest
        )
    except OSError:
        valid = False
    if not valid:
        raise ValueError("PDF source snapshot digest does not match the attempt")
    provenance = getattr(opts, "_pdf_source_provenance", None)
    if not isinstance(provenance, dict):
        raise ValueError("PDF source snapshot protection evidence is unavailable")
    try:
        current_protection = _snapshot_protection_evidence(
            snapshot_path,
            snapshot_path.parent,
        )
    except OSError as exc:
        raise ValueError("PDF source snapshot protection could not be measured") from exc
    protection_fields = (
        "snapshot_read_only",
        "snapshot_read_only_verified",
        "snapshot_mode_blocks_write",
        "snapshot_write_open_rejected",
        "snapshot_private",
        "snapshot_private_verified",
        "snapshot_privacy_method",
        "snapshot_protection_method",
    )
    if (
        provenance.get("snapshot_path") != str(snapshot_path)
        or current_protection.get("snapshot_read_only_verified") is not True
        or any(
            provenance.get(field) != current_protection.get(field)
            for field in protection_fields
        )
    ):
        raise ValueError("PDF source snapshot protection does not match the attempt")
    return str(snapshot_path)


@contextmanager
def _verified_pdf_snapshot_consumer(opts: ImportOptions, consumer: str):
    """Bracket an external path consumer with exact snapshot digest checks."""

    label = str(consumer or "external consumer").strip()
    before = _validated_pdf_source_snapshot_path(opts)
    try:
        yield before
    finally:
        try:
            after = _validated_pdf_source_snapshot_path(opts)
        except ValueError as exc:
            raise ValueError(
                "PDF source snapshot protection or digest changed during %s" % label
            ) from exc
        if Path(after) != Path(before):
            raise ValueError("PDF source snapshot path changed during %s" % label)


def _journal_attempt_path(opts: ImportOptions, path: Any) -> str:
    """Record exact pre-attempt file state before any publication mutates it."""

    target = Path(path).resolve()
    key = os.path.normcase(str(target))
    journal = getattr(opts, "_attempt_path_journal", None)
    if not isinstance(journal, dict):
        journal = {}
        opts._attempt_path_journal = journal
    if key in journal:
        return str(target)
    if target.exists() and not target.is_file():
        raise ImportLifecycleError("attempt publication path is not a file: %s" % target)
    existed = target.is_file()
    payload = target.read_bytes() if existed else None
    mode = stat.S_IMODE(target.stat().st_mode) if existed else None
    journal[key] = {
        "path": str(target),
        "existed": existed,
        "bytes": payload,
        "sha256": hashlib.sha256(payload).hexdigest() if payload is not None else None,
        "mode": mode,
    }
    return str(target)


def _journal_new_attempt_path(opts: ImportOptions, path: Any) -> str:
    """Own a newly-created staging file so rollback can retry its removal."""

    target = Path(path).resolve()
    key = os.path.normcase(str(target))
    journal = getattr(opts, "_attempt_path_journal", None)
    if not isinstance(journal, dict):
        journal = {}
        opts._attempt_path_journal = journal
    if key in journal:
        raise ImportLifecycleError("attempt staging path is already journaled: %s" % target)
    journal[key] = {
        "path": str(target),
        "existed": False,
        "bytes": None,
        "sha256": None,
        "mode": None,
    }
    return str(target)


def _forget_attempt_path(opts: ImportOptions, path: Any) -> None:
    """Release a staging-path claim only after its deletion is verified."""

    journal = getattr(opts, "_attempt_path_journal", None)
    if isinstance(journal, dict):
        journal.pop(os.path.normcase(str(Path(path).resolve())), None)


def _rollback_attempt_paths(opts: ImportOptions) -> Dict[str, Any]:
    """Restore pre-existing bytes and remove every newly published attempt file."""

    journal = getattr(opts, "_attempt_path_journal", None)
    if not isinstance(journal, dict):
        journal = {}
    restored: List[str] = []
    removed: List[str] = []
    errors: List[str] = []
    for entry in reversed(list(journal.values())):
        if not isinstance(entry, dict):
            errors.append("invalid attempt path journal entry")
            continue
        target = Path(str(entry.get("path") or ""))
        try:
            if entry.get("existed") is True:
                payload = entry.get("bytes")
                if type(payload) is not bytes:
                    raise ValueError("pre-existing file bytes are unavailable")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target.chmod(stat.S_IREAD | stat.S_IWRITE)
                fd, temp_name = tempfile.mkstemp(
                    prefix=target.name + ".rollback.",
                    dir=str(target.parent),
                )
                os.close(fd)
                temp_path = Path(temp_name)
                try:
                    temp_path.write_bytes(payload)
                    os.replace(str(temp_path), str(target))
                finally:
                    if temp_path.exists():
                        temp_path.unlink()
                original_mode = entry.get("mode")
                if type(original_mode) is int:
                    target.chmod(original_mode)
                if (
                    not target.is_file()
                    or target.read_bytes() != payload
                    or hashlib.sha256(payload).hexdigest() != entry.get("sha256")
                ):
                    raise OSError("pre-existing file bytes were not restored exactly")
                restored.append(str(target))
            else:
                if target.exists():
                    target.chmod(stat.S_IREAD | stat.S_IWRITE)
                    target.unlink()
                if target.exists():
                    raise OSError("new attempt file still exists after cleanup")
                removed.append(str(target))
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            errors.append("%s: %s" % (target, exc))
    complete = not errors
    if complete:
        opts._attempt_path_journal = {}
    return {
        "restored_paths": restored,
        "removed_paths": removed,
        "cleanup_errors": errors,
        "cleanup_complete": complete,
    }


def _accept_attempt_paths(opts: ImportOptions) -> None:
    """Forget rollback bytes only after the transaction commits successfully."""

    opts._attempt_path_journal = {}


def _open_pdf_source_attempt(opts: ImportOptions):
    """Open a new validated PyMuPDF pass over the retained exact bytes."""

    from pdfcadcore.fitz_loader import safe_open_bytes

    return safe_open_bytes(
        _validated_pdf_source_bytes(opts),
        prefer_lib_dir=_lib_dir,
    )


def _capture_page_visual_runtime_authority(
    opts: ImportOptions,
    pages: List[int],
) -> Any:
    """Capture v2 observations for exactly the selected pages of this attempt."""

    if not isinstance(pages, list) or not pages:
        raise ValueError("selected pages must be a nonempty list")
    page_scope_ids: Dict[int, str] = {}
    for page_number in pages:
        if type(page_number) is not int or page_number <= 0:
            raise ValueError("selected page numbers must be positive exact integers")
        page_scope_ids[page_number] = "p%d:page" % page_number
    authority = capture_fresh_page_visual_authority(
        _validated_pdf_source_bytes(opts),
        importer_identity=FREECAD_TEXT_IMPORTER_IDENTITY,
        page_scope_ids=page_scope_ids,
    )
    if authority.pdf_sha256 != opts._pdf_sha256:
        raise ValueError("page visual authority PDF digest does not match the attempt")
    opts._page_visual_authority = authority
    opts._page_visual_session_anchor = authority.session_anchor()
    opts.page_visual_source_observations.clear()
    opts.page_visual_source_observations.update(authority.observations())
    return authority


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
    """Resolve page strategy without changing an explicit Raster request.

    Explicit Raster remains the page representation.  Its boolean companion
    requires a verified text-suppressed background before structural text is
    delivered independently.  Auto may choose Hybrid because Auto, unlike an
    explicit Raster request, delegates the page-strategy decision.
    """
    normalized = str(effective_mode or "").strip().lower()
    if not _auto_raster_needs_text_overlay(normalized, source_text_blocks, opts):
        return normalized, False
    if str(getattr(opts, "import_mode", "") or "").strip().lower() == "raster":
        return "raster", True
    return "hybrid", False


def _auto_sparse_page_mode(
    n_drawings: int,
    n_text_blocks: Optional[int],
    n_images: int,
) -> str:
    """Resolve sparse Auto pages without inventing a full-page Raster fallback."""

    if (
        type(n_drawings) is not int
        or n_drawings < 0
        or (n_text_blocks is not None and (type(n_text_blocks) is not int or n_text_blocks < 0))
        or type(n_images) is not int
        or n_images < 0
    ):
        raise ValueError("sparse page profile is invalid")
    if n_text_blocks is None:
        return "hybrid" if n_images > 0 else "vector"
    if n_drawings < 5 and n_text_blocks == 0:
        return "hybrid" if n_images > 0 else "vector"
    if n_drawings < 5 and n_text_blocks > 0:
        return "hybrid" if n_images > 0 else "vector"
    return ""


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


def _register_text_delivery_obligations(
    opts: ImportOptions,
    source_item_ids: List[str],
) -> None:
    """Register exact source identities before delivery attempts begin."""

    ledger = getattr(opts, "text_delivery_obligation_source_item_ids", None)
    if not isinstance(ledger, list):
        raise ValueError("text delivery obligation ledger is unavailable")
    for source_item_id in source_item_ids:
        if (
            not isinstance(source_item_id, str)
            or not source_item_id
            or source_item_id != source_item_id.strip()
        ):
            raise ValueError("text delivery obligation identity is invalid")
        if source_item_id not in ledger:
            ledger.append(source_item_id)


def _validate_freecad_text_representation_delivery(
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

    def proof_scope_matches(proof: Any, source_item_id: str) -> bool:
        if not isinstance(proof, dict):
            return False
        if proof.get("item_specific_proven_impossible") is True:
            return "page_specific_proven_impossible" not in proof
        return bool(
            proof.get("page_specific_proven_impossible") is True
            and "item_specific_proven_impossible" not in proof
            and re.fullmatch(r"p[1-9][0-9]*:page", source_item_id) is not None
        )

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

        page_scoped = proof.get("page_specific_proven_impossible") is True
        if page_scoped:
            if proof.get("schema") != PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA:
                return "proof_page_visual_v2_required"
            authority = getattr(opts, "_page_visual_authority", None)
            observation = getattr(opts, "page_visual_source_observations", {}).get(
                source_item_id
            )
            if digest != str(getattr(opts, "_pdf_sha256", "") or ""):
                return "proof_page_visual_pdf_binding_invalid"
            if not page_visual_source_observation_v2_verified(
                observation,
                authority,
            ):
                return "proof_page_visual_observation_invalid"
            if not page_visual_fallback_proof_v2_verified(
                proof,
                observation=observation,
                authority=authority,
                expected_requested_type=requested_type,
                expected_attempted_type=attempted_type,
            ):
                return "proof_page_visual_evidence_invalid"
            return ""

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
        return ""

    requested_raw = str(getattr(opts, "text_mode", "") or "none").strip().lower()
    requested = normalize_type(requested_raw)
    expected_pdf_digest = str(getattr(opts, "_pdf_sha256", "") or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_pdf_digest) is None:
        expected_pdf_digest = ""
    raw_attempts = list(attempts or [])
    obligation_ids = list(
        getattr(opts, "text_delivery_obligation_source_item_ids", []) or []
    )
    # A successfully extracted page with no canonical text items has no text
    # delivery work. Extraction failures are raised before this gate, so an
    # empty independent obligation set is not permission to hide a failure.
    required = bool(
        getattr(opts, "import_text", False)
        and requested != "none"
        and (obligation_ids or raw_attempts)
    )
    normalized_attempts = [
        dict(attempt) for attempt in raw_attempts if isinstance(attempt, dict)
    ]
    source_ids: List[str] = []
    attempts_by_source: Dict[str, List[Dict[str, Any]]] = {}
    attempt_indexes_by_source: Dict[str, List[int]] = {}
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
        attempt_indexes_by_source.setdefault(source_id, []).append(index)

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
    terminal_attempt_indexes: List[int] = []
    delivery_items: List[Dict[str, Any]] = []
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
        terminal_attempt_indexes.append(attempt_indexes_by_source[source_id][-1])
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
                or not proof_scope_matches(prior_proof, source_id)
                or str(prior_proof.get("source_item_id") or "") != source_id
                or normalize_type(prior_proof.get("requested_type")) != requested
                or normalize_type(prior_proof.get("attempted_type")) != prior_attempted
                or prior_proof.get("cleanup_complete") is not True
                or prior_proof.get("created_entity_ids") != created_ids
                or prior_proof.get("removed_entity_ids") != removed_ids
                or not str(prior.get("reason_code") or "").strip()
                or prior_proof.get("reason_code") != prior.get("reason_code")
                or not (
                    (
                        prior_proof.get("page_specific_proven_impossible") is True
                        and prior_proof.get("schema")
                        == PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA
                    )
                    or (
                        isinstance(prior_proof.get("evidence"), dict)
                        and bool(prior_proof.get("evidence"))
                        and isinstance(
                            prior_proof.get("attempted_source_results"),
                            list,
                        )
                        and bool(prior_proof.get("attempted_source_results"))
                        and prior_proof.get("attempted_sources_complete") is True
                    )
                )
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

            def native_host_evidence_verified(
                candidate: Any,
                *,
                proxy_type: str = expected_proxy_type,
                representation: str = final_type,
            ) -> bool:
                return bool(
                    isinstance(candidate, dict)
                    and candidate.get("host_entity_type") == "App::FeaturePython"
                    and candidate.get("host_proxy_type") == proxy_type
                    and isinstance(candidate.get("source_text"), str)
                    and candidate.get("source_text")
                    and candidate.get("source_text_preserved") is True
                    and candidate.get("view_style_verified") is True
                    and (
                        representation != "labels"
                        or candidate.get("label_marker_absent") is True
                    )
                )

            segment_manifest = native_evidence.get("source_segment_manifest")
            segment_deliveries = native_evidence.get("segment_deliveries")
            if segment_manifest is not None or segment_deliveries is not None:
                child_ids = native_evidence.get("child_source_item_ids")
                segmented_native_verified = bool(
                    isinstance(segment_manifest, dict)
                    and segment_manifest.get("schema")
                    == "bcs.freecad_text_source_segments/1.0"
                    and segment_manifest.get("parent_source_item_id") == source_id
                    and segment_manifest.get("requested_type") == requested
                    and isinstance(segment_deliveries, list)
                    and len(segment_deliveries) >= 2
                    and isinstance(child_ids, list)
                    and [child.get("source_item_id") for child in segment_deliveries]
                    == child_ids
                    and all(
                        isinstance(child, dict)
                        and child.get("requested_type") == requested
                        and normalize_type(child.get("attempted_type")) == final_type
                        and normalize_type(child.get("final_type")) == final_type
                        and child.get("outcome") == "verified"
                        and child.get("cleanup_complete") is True
                        and native_host_evidence_verified(child.get("evidence"))
                        for child in segment_deliveries
                    )
                )
                native_visual_verified = segmented_native_verified
            else:
                native_visual_verified = native_host_evidence_verified(native_evidence)
            if not native_visual_verified:
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
                for left, right in zip(
                    attempted_sequence, attempted_sequence[1:], strict=False
                )
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

            def fallback_chain_metadata(event: Dict[str, Any]) -> Dict[str, Any]:
                proof = event.get("proof")
                if (
                    isinstance(proof, dict)
                    and proof.get("page_specific_proven_impossible") is True
                ):
                    return event
                return proof if isinstance(proof, dict) else {}

            matching_fallback_event_indexes = [
                event_index
                for event_index, event in enumerate(fallback_events)
                if normalize_type(event.get("requested")) == requested
                and normalize_type(event.get("delivered")) == final_type
                and event.get("source_item_ids") == [source_id]
                and type(event.get("count")) is int
                and event.get("count") == 1
                and isinstance(event.get("proof"), dict)
                and proof_scope_matches(event["proof"], source_id)
                and str(event["proof"].get("source_item_id") or "") == source_id
                and normalize_type(event["proof"].get("requested_type")) == requested
                and normalize_type(event["proof"].get("attempted_type"))
                == fallback_proof_attempted
                and fallback_chain_metadata(event).get("attempted_types")
                == attempted_sequence
                and fallback_chain_metadata(event).get("proof_chain")
                == expected_proof_chain
                and fallback_chain_metadata(event).get("transition_chain")
                == expected_transitions
                and fallback_chain_metadata(event).get("created_entity_ids")
                == expected_created_ids
                and fallback_chain_metadata(event).get("removed_entity_ids")
                == expected_removed_ids
                and fallback_chain_metadata(event).get("cleanup_complete") is True
                and (
                    event["proof"].get("schema")
                    == PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA
                    or (
                        isinstance(event["proof"].get("evidence"), dict)
                        and bool(event["proof"].get("evidence"))
                    )
                )
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

        delivery_items.append(
            {
                "source_item_id": source_id,
                "terminal_attempt_index": terminal_attempt_indexes[-1],
                "final_type": final_type,
                "verified": bool(
                    outcome == "verified"
                    and final_type
                    and final_type == terminal_attempted
                ),
            }
        )

    if len(matched_fallback_event_indexes) != len(fallback_events):
        invalid_reasons.append("unbound_or_inflated_fallback_event")

    if required and not source_ids:
        invalid_reasons.append("no_item_bound_delivery_attempts")

    verified = bool(not required or (source_ids and not invalid_reasons))
    return {
        "schema": "bcs.freecad_text_representation_delivery/1.1",
        "required": required,
        "requested_type": requested,
        "attempt_count": len(normalized_attempts),
        "items": delivery_items,
        "removed_entity_ids": list(dict.fromkeys(removed_entity_ids)),
        "invalid_reasons": list(dict.fromkeys(invalid_reasons)),
        "verified": verified,
    }


def _canonical_text_delivery_attempts(
    opts: ImportOptions,
    attempts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Project FreeCAD attempts into the strict shared lifecycle vocabulary."""

    def exact_text(value: Any) -> bool:
        return bool(
            isinstance(value, str)
            and value
            and value == value.strip()
        )

    def entity_ids(value: Any, default: List[str]) -> Tuple[List[str], bool]:
        if value is None:
            return list(default), True
        if not isinstance(value, list):
            return [], False
        valid = bool(
            all(exact_text(entity_id) for entity_id in value)
            and len(value) == len(set(value))
        )
        return (list(value) if valid else []), valid

    canonical: List[Dict[str, Any]] = []
    input_validity: List[bool] = []
    for raw_attempt in list(attempts or []):
        if not isinstance(raw_attempt, dict):
            canonical.append(
                {
                    "source_item_id": "",
                    "requested_type": "",
                    "attempted_type": "",
                    "final_type": None,
                    "outcome": "failed",
                    "cleanup_complete": False,
                    "record_verified": False,
                    "type_verified": False,
                    "visual_verified": False,
                    "ownership_verified": False,
                    "created_entity_ids": [],
                    "removed_entity_ids": [],
                    "delivery_entity_ids": [],
                    "support_entity_ids": [],
                    "referenced_entity_ids": [],
                    "reused_entity_ids": [],
                    "evidence": {"malformed_attempt": True},
                }
            )
            input_validity.append(False)
            continue

        attempt = dict(raw_attempt)
        outcome = attempt.get("outcome")
        created, created_valid = entity_ids(
            attempt.get("created_entity_ids"),
            [],
        )
        removed, removed_valid = entity_ids(
            attempt.get("removed_entity_ids"),
            [],
        )
        default_delivery = (
            [entity_id for entity_id in created if entity_id not in set(removed)]
            if outcome == "verified"
            else []
        )
        delivery, delivery_valid = entity_ids(
            attempt.get("delivery_entity_ids"),
            default_delivery,
        )
        support, support_valid = entity_ids(
            attempt.get("support_entity_ids"),
            [],
        )
        retained = list(dict.fromkeys(delivery + support))
        referenced, referenced_valid = entity_ids(
            attempt.get("referenced_entity_ids"),
            retained,
        )
        reused, reused_valid = entity_ids(
            attempt.get("reused_entity_ids"),
            [entity_id for entity_id in retained if entity_id not in set(created)],
        )
        arrays_valid = bool(
            created_valid
            and removed_valid
            and delivery_valid
            and support_valid
            and referenced_valid
            and reused_valid
        )
        state_valid = bool(
            exact_text(attempt.get("source_item_id"))
            and exact_text(attempt.get("requested_type"))
            and exact_text(attempt.get("attempted_type"))
            and exact_text(outcome)
            and outcome in {"proven_impossible", "verified", "failed"}
        )
        evidence_present = bool(
            any(
                isinstance(attempt.get(field), dict) and bool(attempt.get(field))
                for field in ("evidence", "proof")
            )
        )
        record_verified = bool(
            outcome == "verified"
            and attempt.get("cleanup_complete") is True
            and state_valid
            and arrays_valid
            and evidence_present
        )
        final_type = attempt.get("final_type")
        type_verified = bool(
            record_verified
            and exact_text(final_type)
            and final_type == attempt.get("attempted_type")
        )
        created_set = set(created)
        removed_set = set(removed)
        delivery_set = set(delivery)
        support_set = set(support)
        reused_set = set(reused)
        retained_set = delivery_set.union(support_set)
        ownership_verified = bool(
            record_verified
            and delivery_set
            and removed_set.issubset(created_set)
            and not delivery_set.intersection(support_set)
            and reused_set == retained_set.difference(created_set)
            and not reused_set.intersection(created_set.union(removed_set))
            and created_set
            == removed_set.union(retained_set.difference(reused_set))
        )
        visual_verified = bool(record_verified and evidence_present)

        attempt.update(
            {
                "created_entity_ids": created,
                "removed_entity_ids": removed,
                "delivery_entity_ids": delivery,
                "support_entity_ids": support,
                "referenced_entity_ids": referenced,
                "reused_entity_ids": reused,
                "record_verified": record_verified,
                "type_verified": type_verified,
                "visual_verified": visual_verified,
                "ownership_verified": ownership_verified,
            }
        )
        canonical.append(attempt)
        input_validity.append(bool(state_valid and arrays_valid))

    deep_validation = _validate_freecad_text_representation_delivery(
        opts,
        canonical,
    )
    terminal_index_by_source: Dict[str, int] = {}
    for index, attempt in enumerate(canonical):
        source_item_id = attempt.get("source_item_id")
        if exact_text(source_item_id):
            terminal_index_by_source[source_item_id] = index
    deep_verified = bool(
        deep_validation.get("verified") is True
        and all(input_validity)
    )
    for terminal_index in terminal_index_by_source.values():
        terminal = canonical[terminal_index]
        for field_name in (
            "record_verified",
            "type_verified",
            "visual_verified",
            "ownership_verified",
        ):
            terminal[field_name] = bool(
                terminal.get(field_name) is True
                and input_validity[terminal_index]
                and deep_verified
            )
    return canonical


def _build_text_representation_delivery(
    opts: ImportOptions,
    attempts: List[Dict[str, Any]],
    *,
    expected_source_item_ids: Optional[List[str]] = None,
    required: Optional[bool] = None,
) -> Dict[str, Any]:
    """Build schema-1.1 from one canonical ledger after FreeCAD validation."""

    requested_raw = str(getattr(opts, "text_mode", "") or "none").strip().lower()
    try:
        requested = _normalize_requested_text_type(requested_raw)
    except (TypeError, ValueError):
        requested = requested_raw
    canonical = _canonical_text_delivery_attempts(opts, attempts)
    if isinstance(attempts, list):
        attempts[:] = canonical
    if expected_source_item_ids is None:
        expected_source_item_ids = list(
            dict.fromkeys(
                attempt.get("source_item_id")
                for attempt in canonical
                if isinstance(attempt.get("source_item_id"), str)
                and bool(attempt.get("source_item_id"))
            )
        )
    if required is None:
        required = bool(expected_source_item_ids)
    return _build_shared_text_delivery(
        canonical,
        requested_type=requested,
        required=bool(required),
        expected_source_item_ids=expected_source_item_ids,
    )


def _public_report_value(value: Any) -> Any:
    """Remove transient raw geometry while retaining its bound public digest."""

    if isinstance(value, dict):
        return {
            key: _public_report_value(child)
            for key, child in value.items()
            if key != "_shape_comparison_geometry"
        }
    if isinstance(value, list):
        return [_public_report_value(child) for child in value]
    if isinstance(value, tuple):
        return [_public_report_value(child) for child in value]
    return value


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

    _invoke_import_cancellation_checkpoint(opts, "report construction")
    runtime_source_present = bool(
        getattr(opts, "_pdf_source_bytes", None) is not None
        or getattr(opts, "_pdf_source_snapshot_path", None)
    )
    source_pdf_path = (
        _validated_pdf_source_snapshot_path(opts)
        if runtime_source_present
        else pdf_path
    )
    bound_source_provenance = getattr(opts, "_pdf_source_provenance", None)
    source_display_path = str(
        (
            bound_source_provenance.get("source_path")
            if isinstance(bound_source_provenance, dict)
            else None
        )
        or pdf_path
    )
    bound_source_sha256 = (
        str(getattr(opts, "_pdf_sha256", "") or "")
        if runtime_source_present
        else None
    )
    if bool(getattr(opts, "_atomic_import_active", False)):
        _journal_attempt_path(opts, output_path)
        _journal_attempt_path(
            opts,
            Path(output_path).with_name("parts_bootstrap.json"),
        )
        if list(getattr(opts, "_source_provenance_objects", []) or []):
            _journal_attempt_path(
                opts,
                Path(output_path).with_name("source_provenance.json"),
            )
    page_visual_authority = getattr(opts, "_page_visual_authority", None)

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
        extra.update(_public_report_value(opts._report_extra))
    _invoke_import_cancellation_checkpoint(opts, "report text delivery")
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

    obligation_source_item_ids = list(
        getattr(opts, "text_delivery_obligation_source_item_ids", []) or []
    )
    if (
        any(
            not isinstance(source_item_id, str)
            or not source_item_id
            or source_item_id != source_item_id.strip()
            for source_item_id in obligation_source_item_ids
        )
        or len(obligation_source_item_ids) != len(set(obligation_source_item_ids))
    ):
        raise ValueError("text delivery obligation identities are invalid")
    try:
        obligation_requested_type = _normalize_requested_text_type(
            str(opts.text_mode or "none")
        )
    except (TypeError, ValueError):
        obligation_requested_type = str(opts.text_mode or "")
    obligation_required = bool(obligation_source_item_ids)
    extra["text_delivery_obligations"] = {
        "schema": "bcs.text_delivery_obligations/1.0",
        "required": obligation_required,
        "requested_type": obligation_requested_type,
        "source_item_ids": obligation_source_item_ids,
    }

    text_attempts = list(getattr(opts, "text_delivery_attempts", []) or [])
    extra["text_representation_delivery"] = _build_text_representation_delivery(
        opts,
        text_attempts,
        expected_source_item_ids=obligation_source_item_ids,
        required=obligation_required,
    )
    extra["text_delivery_attempts"] = text_attempts
    extra["page_visual_source_observations"] = copy.deepcopy(
        getattr(opts, "page_visual_source_observations", {}) or {}
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
        and bool(
            (event.get("proof") or {}).get("item_specific_proven_impossible")
            or (event.get("proof") or {}).get("page_specific_proven_impossible")
        )
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
    delivered_without_obligations = bool(
        delivery_summary.get("required") is False
        and (
            int((extra.get("actual_text_entity_types") or {}).get("count") or 0) > 0
            or int(text_count or 0) > 0
        )
    )
    if (
        delivery_summary.get("required") is True
        and delivery_summary.get("verified") is not True
    ) or delivered_without_obligations:
        invalid_reasons = list(delivery_summary.get("invalid_reasons") or [])
        if delivered_without_obligations:
            invalid_reasons.append(
                "delivered text entities exist without independent source obligations"
            )
        extra["representation_contract_violation"] = {
            "requested_type": delivery_summary.get("requested_type"),
            "reason": "invalid_item_bound_representation_delivery",
            "invalid_reasons": list(dict.fromkeys(invalid_reasons)),
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

    _invoke_import_cancellation_checkpoint(opts, "report assembly")
    report = build_import_report(
        host_app="freecad",
        host_version=_freecad_version(),
        runtime_lang="python",
        runtime_version=(
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        ),
        importer_version=_importer_version(),
        pdf_path=source_pdf_path,
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
        page_visual_authority=page_visual_authority,
    )
    if runtime_source_present:
        _validated_pdf_source_snapshot_path(opts)
    report.input["file"] = str(pdf_path)
    report._page_visual_authority = page_visual_authority
    opts._live_import_report = report

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
        _invoke_import_cancellation_checkpoint(
            opts, "report provenance publication"
        )
        write_source_provenance_sidecar(
            output_path=sidecar_path,
            import_session_id=session_id,
            pdf_path=source_pdf_path,
            source_display_path=source_display_path,
            source_sha256=bound_source_sha256,
            objects=provenance_objects,
            host_app="freecad",
            importer_version=_importer_version(),
            build_stamp=build_stamp,
            page_count=int(total_pages or pages_imported or 0) or None,
        )
        if runtime_source_present:
            _validated_pdf_source_snapshot_path(opts)
        extra_ref = report.extra
        extra_ref["source_provenance_path"] = Path(sidecar_path).name
        extra_ref["source_provenance"] = {
            "schema": "bcs.source_provenance/1.0",
            "import_session_id": session_id,
            "object_count": len(provenance_objects),
        }

    _invoke_import_cancellation_checkpoint(opts, "report bootstrap construction")
    from pdfcadcore.parts_bootstrap import extract_bootstrap_rows, write_parts_bootstrap_sidecar

    bootstrap_path = str(Path(output_path).with_name("parts_bootstrap.json"))
    build_stamp = str((report.report_meta or {}).get("build_stamp") or "")
    bootstrap_text_items = list(getattr(opts, "_bootstrap_text_items", []) or [])
    if not bootstrap_text_items:
        for page_text in list(getattr(opts, "_model3d_text_evidence", []) or []):
            _invoke_import_cancellation_checkpoint(
                opts, "report bootstrap source page"
            )
            for line in str(page_text).splitlines():
                _invoke_import_cancellation_checkpoint(
                    opts, "report bootstrap source line"
                )
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
    _invoke_import_cancellation_checkpoint(opts, "report bootstrap publication")
    write_parts_bootstrap_sidecar(
        bootstrap_path,
        source_pdf_path,
        source_display_path=source_display_path,
        source_sha256=bound_source_sha256,
        page_count=int(total_pages or pages_imported or 0) or None,
        rows=bootstrap_rows,
        import_build_stamp=import_build_stamp,
    )
    if runtime_source_present:
        _validated_pdf_source_snapshot_path(opts)
    extra_ref = report.extra
    extra_ref["parts_bootstrap"] = {
        "schema": "bcs.parts_bootstrap/1.0",
        "sidecar_path": Path(bootstrap_path).name,
        "row_count": len(bootstrap_rows),
        "note": "BOM row extraction from drawing text" if bootstrap_rows else "no BOM rows detected",
    }

    _invoke_import_cancellation_checkpoint(opts, "report publication")
    report.write_json(output_path)
    _invoke_import_cancellation_checkpoint(opts, "report publication verification")
    import_build_stamp["report_sha256"] = hashlib.sha256(
        Path(output_path).read_bytes()
    ).hexdigest()
    _invoke_import_cancellation_checkpoint(opts, "report bootstrap finalization")
    write_parts_bootstrap_sidecar(
        bootstrap_path,
        source_pdf_path,
        source_display_path=source_display_path,
        source_sha256=bound_source_sha256,
        page_count=int(total_pages or pages_imported or 0) or None,
        rows=bootstrap_rows,
        import_build_stamp=import_build_stamp,
    )
    if runtime_source_present:
        _validated_pdf_source_snapshot_path(opts)
    _invoke_import_cancellation_checkpoint(opts, "report publication complete")
    return output_path


def _require_live_import_contract_ready(opts: ImportOptions):
    """Return the live report only when every authority-backed gate is exact true."""

    report = getattr(opts, "_live_import_report", None)
    extra = getattr(report, "extra", None)
    readiness = extra.get("import_contract_ready") if isinstance(extra, dict) else None
    if not isinstance(readiness, dict) or readiness.get("ready") is not True:
        raise ImportLifecycleError("live import_contract_ready is not exact True")
    authority = getattr(opts, "_page_visual_authority", None)
    if authority is None:
        raise ImportLifecycleError("live page-visual authority is unavailable")
    if getattr(report, "_page_visual_authority", None) is not authority:
        raise ImportLifecycleError("live report lost its page-visual authority binding")
    report_input = getattr(report, "input", None)
    expected_digest = str(getattr(opts, "_pdf_sha256", "") or "")
    if (
        not isinstance(report_input, dict)
        or report_input.get("sha256") != expected_digest
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        raise ImportLifecycleError("live report PDF digest binding is invalid")
    report_extra = getattr(opts, "_report_extra", None)
    if not isinstance(report_extra, dict):
        raise ImportLifecycleError("live persistence evidence is unavailable")
    host_inventory = report_extra.get("actual_host_object_inventory")
    save_reopen = report_extra.get("save_reopen_inventory")
    if not isinstance(host_inventory, dict) or host_inventory.get("verified") is not True:
        raise ImportLifecycleError("live host inventory is not verified")
    if not isinstance(save_reopen, dict) or save_reopen.get("verified") is not True:
        raise ImportLifecycleError("save/reopen inventory is not verified")
    try:
        raster_required = bool(_inventory_required_included_rasters(host_inventory))
    except (RuntimeError, TypeError, ValueError) as exc:
        raise ImportLifecycleError(
            "live raster persistence obligations are invalid"
        ) from exc
    if raster_required:
        raster_evidence = host_inventory.get("raster_archive_evidence")
        raster_digest = str(
            save_reopen.get("raster_archive_evidence_digest") or ""
        )
        if (
            not isinstance(raster_evidence, dict)
            or raster_evidence.get("verified") is not True
            or raster_evidence.get("schema")
            != _FCSTD_RASTER_ARCHIVE_EVIDENCE_SCHEMA
            or raster_evidence.get("method")
            != _FCSTD_RASTER_ARCHIVE_EVIDENCE_METHOD
            or re.fullmatch(r"[0-9a-f]{64}", raster_digest) is None
            or raster_evidence.get("evidence_digest") != raster_digest
            or raster_digest != _fcstd_archive_evidence_digest(raster_evidence)
        ):
            raise ImportLifecycleError(
                "included raster FCStd persistence evidence is not verified"
            )
    return report


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

def _ensure_doc_with_ownership() -> Tuple[Any, bool]:
    """Return the active document and whether this import created it."""

    doc = FreeCAD.ActiveDocument
    created = doc is None
    if created:
        doc = FreeCAD.newDocument("PDF_Import")
    if doc is None:
        raise ImportLifecycleError("FreeCAD did not provide an import document")
    # Note: temp file cleanup is deferred to explicit calls, not run
    # automatically here, to avoid deleting images still referenced by
    # Image::ImagePlane objects from the previous import.
    return doc, created


def _ensure_doc():
    doc, _created = _ensure_doc_with_ownership()
    return doc


def _close_attempt_created_document(fc_doc, created_by_attempt: bool) -> None:
    """Close a failed attempt's document without touching pre-existing documents."""

    if created_by_attempt is not True:
        return
    document_name = str(getattr(fc_doc, "Name", "") or "")
    close_document = getattr(FreeCAD, "closeDocument", None)
    if not document_name or not callable(close_document):
        raise ImportCleanupError(
            "attempt-created FreeCAD document could not be closed",
            details={
                "retryable": True,
                "document_name": document_name,
                "errors": ["FreeCAD.closeDocument is unavailable"],
            },
        )
    try:
        close_document(document_name)
        get_document = getattr(FreeCAD, "getDocument", None)
        if callable(get_document) and get_document(document_name) is not None:
            raise RuntimeError("document remains open after closeDocument")
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ImportCleanupError(
            "attempt-created FreeCAD document cleanup failed",
            details={
                "retryable": True,
                "document_name": document_name,
                "errors": [str(exc)],
            },
        ) from exc


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
                if not source_text:
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
    candidates: List[Path] = []
    installed_mod_root = ""
    try:
        app_data = FreeCAD.getUserAppDataDir()
        if isinstance(app_data, (str, os.PathLike)) and str(app_data).strip():
            installed_mod_root = os.path.normcase(
                os.path.abspath(os.fspath(Path(app_data) / "Mod"))
            )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        installed_mod_root = ""
    try:
        user_cache = FreeCAD.getUserCachePath()
        if isinstance(user_cache, (str, os.PathLike)) and str(user_cache).strip():
            candidates.append(
                Path(user_cache) / "PDFVectorImporter" / "font_cache"
            )
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        pass
    candidates.append(Path(tempfile.gettempdir()) / "bc_fc_pdf_font_cache")

    failures: List[str] = []
    seen: set = set()
    marker = b"bcs-pdf-font-cache-write-probe-v1"
    for candidate in candidates:
        probe_path = ""
        try:
            normalized = os.path.normcase(os.path.abspath(os.fspath(candidate)))
            if normalized in seen:
                continue
            seen.add(normalized)
            inside_installed_mod = False
            if installed_mod_root:
                try:
                    inside_installed_mod = (
                        os.path.commonpath([normalized, installed_mod_root])
                        == installed_mod_root
                    )
                except ValueError:
                    # Windows paths on different volumes cannot share a common
                    # path and therefore cannot place the cache inside Mod.
                    inside_installed_mod = False
            if inside_installed_mod:
                failures.append("%s: inside installed Mod tree" % candidate)
                continue
            candidate.mkdir(parents=True, exist_ok=True)
            descriptor, probe_path = tempfile.mkstemp(
                prefix=".bcs_font_cache_probe_",
                suffix=".tmp",
                dir=str(candidate),
            )
            with os.fdopen(descriptor, "wb") as probe:
                probe.write(marker)
                probe.flush()
                os.fsync(probe.fileno())
            if Path(probe_path).read_bytes() != marker:
                raise OSError("font cache write probe did not round-trip")
            os.remove(probe_path)
            probe_path = ""
            return candidate
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            failures.append("%s: %s: %s" % (candidate, exc.__class__.__name__, exc))
        finally:
            if probe_path:
                try:
                    os.remove(probe_path)
                except OSError:
                    pass
    raise OSError(
        "No writable font cache directory is available (%s)"
        % "; ".join(failures)
    )


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
            approved_root = (
                staged_path.parent.resolve(strict=True)
                if _allow_unbound_compat
                else _shapestring_font_cache_dir().resolve(strict=True)
            )
            canonical_path, staged_bytes, actual_sha = _read_stable_font_asset(
                staged_path,
                approved_root=approved_root,
            )
        except FileNotFoundError:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="staged_font_file_missing",
                path=path_value,
                sha256=sha_value,
            )]
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
        internal_identity = _font_internal_identity_evidence(
            staged_bytes,
            font_identity,
        )
        if internal_identity.get("font_identity_verified") is not True:
            return None, [source_result(
                "embedded_font",
                "invalid",
                font_identity,
                reason="embedded_font_internal_identity_mismatch",
                path=str(canonical_path),
                sha256=actual_sha,
                font_identity_verified=False,
                internal_identity_evidence=internal_identity,
            )]
        return str(canonical_path), [source_result(
            "embedded_font",
            "found",
            font_identity,
            path=str(canonical_path),
            sha256=actual_sha,
            xref=xref_value,
            font_identity_verified=True,
            internal_identity_evidence=internal_identity,
            approved_font_root=str(approved_root),
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

    system_root = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    system_path = os.path.join(str(system_root), filename)
    try:
        if not Path(system_path).is_file():
            results.append(source_result(
                "system_font", "not_found", font_identity
            ))
            return None, results
        canonical_path, system_bytes, system_sha256 = _read_stable_font_asset(
            system_path,
            approved_root=system_root,
        )
    except Exception as exc:
        results.append(source_result(
            "system_font",
            "invalid",
            font_identity,
            reason="system_font_file_unreadable",
            path=system_path,
            exception="%s: %s" % (exc.__class__.__name__, exc),
        ))
        return None, results

    internal_identity = _font_internal_identity_evidence(system_bytes, font_identity)
    if internal_identity.get("font_identity_verified") is not True:
        results.append(source_result(
            "system_font",
            "invalid",
            font_identity,
            reason="system_font_internal_identity_mismatch",
            path=str(canonical_path),
            font_sha256=system_sha256,
            font_identity_verified=False,
            internal_identity_evidence=internal_identity,
        ))
        return None, results

    results.append(source_result(
        "system_font",
        "found",
        font_identity,
        path=str(canonical_path),
        sha256=system_sha256,
        font_sha256=system_sha256,
        font_identity_verified=True,
        internal_identity_evidence=internal_identity,
        approved_font_root=str(system_root.resolve(strict=True)),
    ))
    return str(canonical_path), results


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


def _font_evidence_digest(evidence: Dict[str, Any]) -> str:
    unsigned = {
        key: value
        for key, value in dict(evidence or {}).items()
        if key not in {"evidence_sha256", "coverage_evidence_sha256"}
    }
    return hashlib.sha256(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _font_identity_aliases(value: Any) -> set[str]:
    """Return conservative canonical aliases for one internal/PDF font name."""

    try:
        key = _canonical_font_identity(str(value or ""))["normalized_key"]
    except Exception:
        key = ""
    if not key:
        return set()
    aliases = {key}
    if key.endswith("psmt") and len(key) > 4:
        aliases.add(key[:-4])
    if key.endswith("mt") and len(key) > 2:
        aliases.add(key[:-2])
    aliases.update(
        alias.replace("psbold", "bold")
        .replace("psitalic", "italic")
        .replace("psregular", "regular")
        for alias in list(aliases)
    )
    return {alias for alias in aliases if alias}


def _font_style_signature(value: Any) -> Tuple[bool, bool]:
    aliases = tuple(sorted(_font_identity_aliases(value)))
    return (
        any(
            token in alias
            for alias in aliases
            for token in ("bold", "demi", "semibold", "black")
        ),
        any(
            token in alias
            for alias in aliases
            for token in ("italic", "oblique")
        ),
    )


def _font_style_details(value: Any) -> Dict[str, bool]:
    """Describe only an explicitly named style; a family name has no style."""

    key = _canonical_font_identity(str(value or ""))["normalized_key"]
    style_suffixes = (
        "semibolditalic",
        "semiboldoblique",
        "bolditalic",
        "boldoblique",
        "demiitalic",
        "demioblique",
        "blackitalic",
        "blackoblique",
        "semibold",
        "bold",
        "demi",
        "black",
        "italic",
        "oblique",
        "regular",
        "roman",
        "normal",
        "book",
    )
    matched_suffix = next(
        (suffix for suffix in style_suffixes if key.endswith(suffix)),
        "",
    )
    return {
        "explicit": bool(matched_suffix),
        "bold": any(
            token in matched_suffix for token in ("bold", "semibold", "demi", "black")
        ),
        "italic": any(token in matched_suffix for token in ("italic", "oblique")),
        "regular": matched_suffix in {"regular", "roman", "normal", "book"},
    }


def _font_internal_identity_evidence(
    font_bytes: bytes,
    source_font_identity: Dict[str, str],
) -> Dict[str, Any]:
    """Validate a paired internal family/style identity, never a source label."""

    source_raw_name = str(source_font_identity.get("raw_name", "") or "")
    source_key = _canonical_font_identity(source_raw_name)["normalized_key"]
    source_style_details = _font_style_details(source_raw_name)
    evidence: Dict[str, Any] = {
        "font_identity_verified": False,
        "source_font_identity": copy.deepcopy(source_font_identity),
        "font_sha256": hashlib.sha256(font_bytes).hexdigest(),
        "internal_names": [],
        "internal_name_keys": [],
        "source_style_signature": list(_font_style_signature(source_raw_name)),
        "source_style_explicit": source_style_details["explicit"],
        "internal_style_signatures": [],
        "internal_family_style_pairs": [],
        "matched_identity_records": [],
        "os2_fs_selection": None,
        "head_mac_style": None,
    }
    font = None
    try:
        from fontTools.ttLib import TTFont

        font = TTFont(
            BytesIO(font_bytes),
            lazy=False,
            recalcBBoxes=False,
            recalcTimestamp=False,
        )
        name_table = font["name"]
        relevant_name_ids = {1, 2, 4, 6, 16, 17}
        grouped_names: Dict[Tuple[int, int, int], Dict[int, set[str]]] = {}
        for record in name_table.names:
            if record.nameID not in relevant_name_ids:
                continue
            decoded = str(record.toUnicode()).strip()
            if not decoded:
                continue
            group_key = (
                int(record.platformID),
                int(record.platEncID),
                int(record.langID),
            )
            grouped_names.setdefault(group_key, {}).setdefault(
                int(record.nameID), set()
            ).add(decoded)
        internal_names = sorted(
            {
                name
                for grouped in grouped_names.values()
                for names in grouped.values()
                for name in names
            },
            key=str.casefold,
        )
        internal_keys = sorted(
            {
                alias
                for name in internal_names
                for alias in _font_identity_aliases(name)
            }
        )
        source_aliases = _font_identity_aliases(source_raw_name)
        internal_styles = sorted({_font_style_signature(name) for name in internal_names})
        os2_fs_selection = (
            int(font["OS/2"].fsSelection) if "OS/2" in font else None
        )
        head_mac_style = int(font["head"].macStyle) if "head" in font else None
        pairs: List[Dict[str, Any]] = []
        matches: List[Dict[str, Any]] = []
        for group_key in sorted(grouped_names):
            grouped = grouped_names[group_key]
            families = sorted(
                grouped.get(16) or grouped.get(1) or set(),
                key=str.casefold,
            )
            subfamilies = sorted(
                grouped.get(17) or grouped.get(2) or set(),
                key=str.casefold,
            )
            full_names = sorted(
                (grouped.get(4) or set()) | (grouped.get(6) or set()),
                key=str.casefold,
            )
            for family in families:
                family_aliases = _font_identity_aliases(family)
                for subfamily in subfamilies:
                    style_details = _font_style_details(subfamily)
                    style_aliases = _font_identity_aliases(subfamily)
                    combined_aliases = {
                        alias
                        for combined in (
                            "%s %s" % (family, subfamily),
                            "%s-%s" % (family, subfamily),
                        )
                        for alias in _font_identity_aliases(combined)
                    }
                    full_aliases = {
                        alias
                        for full_name in full_names
                        for alias in _font_identity_aliases(full_name)
                    }
                    os2_consistent = bool(
                        os2_fs_selection is None
                        or (
                            bool(os2_fs_selection & 0x20) == style_details["bold"]
                            and bool(os2_fs_selection & 0x01)
                            == style_details["italic"]
                        )
                    )
                    head_consistent = bool(
                        head_mac_style is None
                        or (
                            bool(head_mac_style & 0x01) == style_details["bold"]
                            and bool(head_mac_style & 0x02)
                            == style_details["italic"]
                        )
                    )
                    flags_consistent = os2_consistent and head_consistent
                    # A bare family match is not a family+style identity.  Exact
                    # equivalence requires either the paired composite identity
                    # or a non-family full/PostScript name from this same record
                    # group, with style bits agreeing when those tables exist.
                    composite_match = source_key in combined_aliases
                    full_name_match = bool(
                        source_key in full_aliases and source_key not in family_aliases
                    )
                    source_style_consistent = bool(
                        not source_style_details["explicit"]
                        or (
                            source_style_details["bold"] == style_details["bold"]
                            and source_style_details["italic"]
                            == style_details["italic"]
                            and (
                                not source_style_details["regular"]
                                or style_details["regular"]
                            )
                        )
                    )
                    verified = bool(
                        (composite_match or full_name_match)
                        and source_style_consistent
                        and flags_consistent
                    )
                    pair = {
                        "platform_id": group_key[0],
                        "encoding_id": group_key[1],
                        "language_id": group_key[2],
                        "family": family,
                        "subfamily": subfamily,
                        "family_keys": sorted(family_aliases),
                        "subfamily_keys": sorted(style_aliases),
                        "combined_keys": sorted(combined_aliases),
                        "full_name_keys": sorted(full_aliases),
                        "style_bold": style_details["bold"],
                        "style_italic": style_details["italic"],
                        "style_regular": style_details["regular"],
                        "style_flags_consistent": flags_consistent,
                    }
                    pairs.append(pair)
                    if verified:
                        matches.append(copy.deepcopy(pair))
        evidence.update(
            {
                "internal_names": internal_names,
                "internal_name_keys": internal_keys,
                "internal_style_signatures": [list(value) for value in internal_styles],
                "internal_family_style_pairs": pairs,
                "matched_identity_records": matches,
                "os2_fs_selection": os2_fs_selection,
                "head_mac_style": head_mac_style,
                "font_identity_verified": bool(matches and source_aliases),
            }
        )
    except Exception as exc:
        evidence["exception"] = "%s: %s" % (exc.__class__.__name__, exc)
    finally:
        if font is not None:
            try:
                font.close()
            except (AttributeError, RuntimeError):
                pass
    evidence["evidence_sha256"] = _font_evidence_digest(evidence)
    return evidence


def _font_bytes_glyph_coverage(
    font_bytes: bytes,
    source_text: str,
) -> Dict[str, Any]:
    """Require a real nonempty outline for every source-visible character."""

    required_codepoints = sorted({ord(character) for character in source_text})
    base = {
        "font_sha256": hashlib.sha256(font_bytes).hexdigest(),
        "required_codepoints": required_codepoints,
        "missing_codepoints": [],
        "empty_outline_codepoints": [],
        "nonempty_outline_codepoints": [],
        "replacement_behavior_verified": False,
    }
    if not font_bytes or not source_text or not required_codepoints:
        base.update(
            {
                "glyph_coverage_verified": False,
                "missing_codepoints": required_codepoints,
            }
        )
        base["coverage_evidence_sha256"] = _font_evidence_digest(base)
        return base
    font = None
    try:
        from fontTools.pens.boundsPen import BoundsPen
        from fontTools.ttLib import TTFont

        font = TTFont(
            BytesIO(font_bytes),
            lazy=False,
            recalcBBoxes=False,
            recalcTimestamp=False,
        )
        cmap = dict(font.getBestCmap() or {})
        glyph_set = font.getGlyphSet()
        missing: List[int] = []
        empty_outlines: List[int] = []
        nonempty_outlines: List[int] = []
        for codepoint in required_codepoints:
            glyph_name = cmap.get(codepoint)
            if (
                not isinstance(glyph_name, str)
                or not glyph_name
                or glyph_name == ".notdef"
            ):
                missing.append(codepoint)
                continue
            try:
                if int(font.getGlyphID(glyph_name)) <= 0:
                    missing.append(codepoint)
                    continue
                bounds_pen = BoundsPen(glyph_set)
                glyph_set[glyph_name].draw(bounds_pen)
                bounds = bounds_pen.bounds
                if (
                    bounds is None
                    or len(bounds) != 4
                    or not all(math.isfinite(float(value)) for value in bounds)
                    or float(bounds[2]) <= float(bounds[0])
                    or float(bounds[3]) <= float(bounds[1])
                ):
                    empty_outlines.append(codepoint)
                    continue
                nonempty_outlines.append(codepoint)
            except Exception:
                empty_outlines.append(codepoint)
        base.update(
            {
                "glyph_coverage_verified": not missing and not empty_outlines,
                "missing_codepoints": missing,
                "empty_outline_codepoints": empty_outlines,
                "nonempty_outline_codepoints": nonempty_outlines,
                # A replacement glyph can never satisfy a different source
                # codepoint; cmap absence remains explicit above.
                "replacement_behavior_verified": True,
            }
        )
    except Exception as exc:
        base.update(
            {
                "glyph_coverage_verified": False,
                "missing_codepoints": required_codepoints,
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            }
        )
    finally:
        if font is not None:
            try:
                font.close()
            except (AttributeError, RuntimeError):
                pass
    base["coverage_evidence_sha256"] = _font_evidence_digest(base)
    return base


def _no_cost_font_candidate_roots() -> List[Tuple[str, Path]]:
    """Return deterministic local-only substitute roots with auditable origins."""

    roots: List[Tuple[str, Path]] = []
    windir = os.environ.get("WINDIR")
    if windir:
        roots.append(("installed_no_cost_substitute", Path(windir) / "Fonts"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(
            (
                "installed_no_cost_substitute",
                Path(local_app_data) / "Microsoft" / "Windows" / "Fonts",
            )
        )
    module_root = Path(_mod_root)
    for bundled_root in (
        module_root / "fonts",
        module_root / "resources" / "fonts",
        module_root / "lib" / "fonts",
    ):
        roots.append(("bundled_no_cost_substitute", bundled_root))
    return roots


_FONT_SCAN_MAX_ROOTS = 8
_FONT_SCAN_MAX_DIRECTORIES = 2048
_FONT_SCAN_MAX_FILES = 8192
_FONT_SCAN_MAX_FILE_BYTES = 64 * 1024 * 1024
_FONT_SCAN_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_FONT_SCAN_MAX_SECONDS = 4.0
_FONT_COVERAGE_CACHE_MAX = 512
_FONT_COVERAGE_CACHE: Dict[Tuple[str, Tuple[int, ...]], Dict[str, Any]] = {}


def _path_is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0) or 0)
        return bool(attributes & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)))
    except (OSError, RuntimeError, TypeError, ValueError):
        return True


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((str(path), str(root))) == str(root)
    except (OSError, RuntimeError, TypeError, ValueError):
        return False


def _read_stable_font_asset(
    path: Any,
    *,
    approved_root: Optional[Any] = None,
    max_bytes: int = _FONT_SCAN_MAX_FILE_BYTES,
) -> Tuple[Path, bytes, str]:
    """Read one regular non-reparse file and reject containment or TOCTOU drift."""

    raw_path = Path(path)
    raw_root = Path(approved_root) if approved_root is not None else None
    # Preserve the resolver's dedicated missing-asset classification.  This
    # check does not authorize the path: an existing asset must still pass the
    # raw and canonical reparse, containment, type, size, and stability gates.
    raw_path.lstat()
    if _path_is_reparse_point(raw_path):
        raise ValueError("font asset is a symlink or reparse point")
    if raw_root is not None and _path_is_reparse_point(raw_root):
        raise ValueError("approved font root is a symlink or reparse point")
    resolved = raw_path.resolve(strict=True)
    root = raw_root.resolve(strict=True) if raw_root is not None else None
    if root is not None and not _path_is_within(resolved, root):
        raise ValueError("font asset escaped its approved canonical root")
    if not resolved.is_file():
        raise ValueError("font asset is not a regular file")
    before = resolved.stat()
    if before.st_size <= 0 or before.st_size > int(max_bytes):
        raise ValueError("font asset size is outside the bounded policy")
    with open(resolved, "rb") as font_file:
        # The pre-read regular-file stat is authoritative for the permitted
        # length.  Never read a sentinel byte beyond the caller's remaining
        # aggregate budget; growth is detected by the post-read stat/token.
        payload = font_file.read(int(max_bytes))
    after = resolved.stat()
    before_token = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_token = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_token != after_token or len(payload) != after.st_size:
        raise RuntimeError("font asset changed while it was being read")
    if not payload or len(payload) > int(max_bytes):
        raise ValueError("font asset size is outside the bounded policy")
    return resolved, payload, hashlib.sha256(payload).hexdigest()


def _bounded_font_candidates() -> Tuple[List[Tuple[str, Path, Path]], Dict[str, Any]]:
    """Enumerate only canonical, contained, non-reparse font files within caps."""

    started = time.perf_counter()
    candidates: List[Tuple[str, Path, Path]] = []
    seen_paths: set[str] = set()
    seen_roots: set[str] = set()
    directory_count = 0
    file_count = 0
    outside_rejections = 0
    reparse_rejections = 0
    scan_errors = 0
    capped = False

    for candidate_source, raw_root in list(_no_cost_font_candidate_roots())[
        :_FONT_SCAN_MAX_ROOTS
    ]:
        if time.perf_counter() - started > _FONT_SCAN_MAX_SECONDS:
            capped = True
            break
        root_path = Path(raw_root)
        try:
            if _path_is_reparse_point(root_path):
                reparse_rejections += 1
                continue
            root = root_path.resolve(strict=True)
            if not root.is_dir():
                continue
        except (OSError, RuntimeError, TypeError, ValueError):
            scan_errors += 1
            continue
        root_key = os.path.normcase(str(root))
        if root_key in seen_roots:
            continue
        seen_roots.add(root_key)
        pending = [root]
        while pending:
            if (
                directory_count >= _FONT_SCAN_MAX_DIRECTORIES
                or file_count >= _FONT_SCAN_MAX_FILES
                or time.perf_counter() - started > _FONT_SCAN_MAX_SECONDS
            ):
                capped = True
                pending.clear()
                break
            directory = pending.pop()
            directory_count += 1
            try:
                with os.scandir(directory) as iterator:
                    entries = sorted(iterator, key=lambda entry: entry.name.casefold())
            except (OSError, RuntimeError, TypeError, ValueError):
                scan_errors += 1
                continue
            child_directories: List[Path] = []
            for entry in entries:
                if time.perf_counter() - started > _FONT_SCAN_MAX_SECONDS:
                    capped = True
                    break
                entry_path = Path(entry.path)
                try:
                    if entry.is_symlink() or _path_is_reparse_point(entry_path):
                        reparse_rejections += 1
                        continue
                    resolved = entry_path.resolve(strict=True)
                    if not _path_is_within(resolved, root):
                        outside_rejections += 1
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        child_directories.append(resolved)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if file_count >= _FONT_SCAN_MAX_FILES:
                        capped = True
                        break
                    file_count += 1
                    if resolved.suffix.lower() not in {".ttf", ".otf"}:
                        continue
                    path_key = os.path.normcase(str(resolved))
                    if path_key in seen_paths:
                        continue
                    seen_paths.add(path_key)
                    candidates.append((str(candidate_source), resolved, root))
                except (OSError, RuntimeError, TypeError, ValueError):
                    scan_errors += 1
            pending.extend(reversed(child_directories))
            if capped:
                break
        if capped:
            break

    return candidates, {
        "scan_bounded": True,
        "scan_capped": capped,
        "scanned_root_count": len(seen_roots),
        "scanned_directory_count": directory_count,
        "scanned_file_count": file_count,
        "outside_root_rejection_count": outside_rejections,
        "reparse_rejection_count": reparse_rejections,
        "scan_error_count": scan_errors,
        "scan_limits": {
            "max_roots": _FONT_SCAN_MAX_ROOTS,
            "max_directories": _FONT_SCAN_MAX_DIRECTORIES,
            "max_files": _FONT_SCAN_MAX_FILES,
            "max_file_bytes": _FONT_SCAN_MAX_FILE_BYTES,
            "max_total_bytes": _FONT_SCAN_MAX_TOTAL_BYTES,
            "max_seconds": _FONT_SCAN_MAX_SECONDS,
        },
    }


def _select_no_cost_font_substitute(
    source_text: str,
    *,
    exclude_paths: Optional[List[str]] = None,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Select the first bounded local candidate with nonempty outline coverage."""

    excluded = {
        os.path.normcase(os.path.abspath(path))
        for path in list(exclude_paths or [])
        if isinstance(path, str) and path
    }
    candidates, scan_evidence = _bounded_font_candidates()

    attempted: List[Dict[str, Any]] = []
    total_bytes = 0
    for candidate_source, candidate_path, approved_root in candidates:
        if total_bytes >= _FONT_SCAN_MAX_TOTAL_BYTES:
            scan_evidence["scan_capped"] = True
            break
        normalized = os.path.normcase(str(candidate_path))
        if normalized in excluded:
            continue
        record: Dict[str, Any] = {
            "font_candidate_source": candidate_source,
            "path": str(candidate_path),
        }
        try:
            remaining_bytes = _FONT_SCAN_MAX_TOTAL_BYTES - total_bytes
            candidate_size = int(candidate_path.stat().st_size)
            if candidate_size <= 0 or candidate_size > _FONT_SCAN_MAX_FILE_BYTES:
                raise ValueError("font asset size is outside the bounded policy")
            if candidate_size > remaining_bytes:
                scan_evidence["scan_capped"] = True
                scan_evidence["cap_rejected_path"] = str(candidate_path)
                break
            stable_path, payload, digest = _read_stable_font_asset(
                candidate_path,
                approved_root=approved_root,
                max_bytes=min(_FONT_SCAN_MAX_FILE_BYTES, remaining_bytes),
            )
            total_bytes += len(payload)
            record["path"] = str(stable_path)
            record["source_font_asset_path"] = str(stable_path)
            record["source_font_asset_sha256"] = digest
            record["delivered_font_sha256"] = digest
            cache_key = (digest, tuple(sorted({ord(value) for value in source_text})))
            coverage = _FONT_COVERAGE_CACHE.get(cache_key)
            if coverage is None:
                coverage = _font_bytes_glyph_coverage(payload, source_text)
                if len(_FONT_COVERAGE_CACHE) >= _FONT_COVERAGE_CACHE_MAX:
                    _FONT_COVERAGE_CACHE.pop(next(iter(_FONT_COVERAGE_CACHE)))
                _FONT_COVERAGE_CACHE[cache_key] = copy.deepcopy(coverage)
            record.update(coverage)
            record["font_coverage_evidence"] = copy.deepcopy(coverage)
        except Exception as exc:
            record.update(
                {
                    "glyph_coverage_verified": False,
                    "replacement_behavior_verified": False,
                    "exception": "%s: %s" % (exc.__class__.__name__, exc),
                }
            )
        attempted.append(record)
        if record.get("glyph_coverage_verified") is True:
            scan_evidence["scanned_candidate_bytes"] = total_bytes
            return str(stable_path), {
                **record,
                "font_substitution_applied": True,
                "source_font_equivalence": False,
                "font_identity_verified": False,
                "attempted_candidate_count": len(attempted),
                "attempted_candidates": copy.deepcopy(attempted),
                **scan_evidence,
                # Transient immutable handoff.  These private fields are removed
                # by staging and can never enter persisted/report evidence.
                "_selected_font_bytes": payload,
                "_selected_font_path": str(stable_path),
                "_selected_font_approved_root": str(approved_root),
            }

    scan_evidence["scanned_candidate_bytes"] = total_bytes
    return None, {
        "font_substitution_applied": False,
        "source_font_equivalence": False,
        "font_identity_verified": False,
        "glyph_coverage_verified": False,
        "replacement_behavior_verified": all(
            result.get("replacement_behavior_verified") is True
            for result in attempted
        ) if attempted else False,
        "attempted_candidate_count": len(attempted),
        "attempted_candidates": attempted,
        **scan_evidence,
    }


def _font_asset_is_read_only(path: Any) -> bool:
    try:
        mode = Path(path).stat().st_mode
    except (OSError, RuntimeError, TypeError, ValueError):
        return False
    return not bool(mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def _stage_shapestring_font_asset(
    source_path: Any,
    font_delivery_evidence: Dict[str, Any],
    opts: ImportOptions,
) -> Tuple[str, Dict[str, Any]]:
    """Copy stable selected bytes into attempt-journaled content-addressed storage."""

    evidence = copy.deepcopy(font_delivery_evidence)
    payload = evidence.pop("_selected_font_bytes", None)
    selected_path_value = evidence.pop("_selected_font_path", None)
    approved_root_value = evidence.pop("_selected_font_approved_root", None)
    if (
        not isinstance(payload, bytes)
        or not payload
        or not isinstance(selected_path_value, str)
        or not selected_path_value
        or not isinstance(approved_root_value, str)
        or not approved_root_value
    ):
        raise RuntimeError("selected font immutable handoff is incomplete")
    canonical_source = Path(selected_path_value)
    approved_root = Path(approved_root_value)
    if (
        not canonical_source.is_absolute()
        or not approved_root.is_absolute()
        or not _path_is_within(canonical_source, approved_root)
        or os.path.normcase(os.path.abspath(os.fspath(source_path)))
        != os.path.normcase(str(canonical_source))
        or evidence.get("source_font_asset_path") != str(canonical_source)
    ):
        raise RuntimeError("selected font path/root handoff is invalid")
    source_sha256 = hashlib.sha256(payload).hexdigest()
    expected_source_sha256 = str(
        evidence.get("source_font_asset_sha256")
        or evidence.get("delivered_font_sha256")
        or ""
    )
    if (
        re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256) is None
        or source_sha256 != expected_source_sha256
    ):
        raise RuntimeError("selected font bytes changed before attempt staging")

    delivery_root = _shapestring_font_cache_dir() / "delivery-assets"
    delivery_root.mkdir(parents=True, exist_ok=True)
    if _path_is_reparse_point(delivery_root):
        raise RuntimeError("font delivery staging root is a reparse point")
    canonical_root = delivery_root.resolve(strict=True)
    suffix = canonical_source.suffix.lower()
    if suffix not in {".ttf", ".otf"}:
        suffix = ".ttf"
    staged_path = canonical_root / (source_sha256 + suffix)
    if not _path_is_within(staged_path, canonical_root):
        raise RuntimeError("content-addressed font target escaped staging root")

    replace_required = True
    if staged_path.exists():
        try:
            _existing_path, existing_bytes, existing_sha256 = _read_stable_font_asset(
                staged_path,
                approved_root=canonical_root,
            )
            replace_required = bool(
                existing_sha256 != source_sha256 or existing_bytes != payload
            )
        except Exception:
            replace_required = True
    if replace_required:
        _journal_attempt_path(opts, staged_path)
        fd, temporary_name = tempfile.mkstemp(
            prefix=source_sha256 + ".",
            suffix=suffix,
            dir=str(canonical_root),
        )
        os.close(fd)
        temporary_path = Path(temporary_name)
        try:
            with open(temporary_path, "wb") as staged_file:
                staged_file.write(payload)
                staged_file.flush()
                os.fsync(staged_file.fileno())
            if staged_path.exists():
                try:
                    os.chmod(staged_path, stat.S_IREAD | stat.S_IWRITE)
                except OSError:
                    pass
            os.replace(temporary_path, staged_path)
        finally:
            if temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
    elif not _font_asset_is_read_only(staged_path):
        _journal_attempt_path(opts, staged_path)

    os.chmod(staged_path, stat.S_IREAD)
    _verified_path, verified_bytes, staged_sha256 = _read_stable_font_asset(
        staged_path,
        approved_root=canonical_root,
    )
    staged_read_only = _font_asset_is_read_only(staged_path)
    if (
        verified_bytes != payload
        or staged_sha256 != source_sha256
        or not staged_read_only
    ):
        raise RuntimeError("content-addressed staged font verification failed")

    evidence.update(
        {
            "source_font_asset_path": str(canonical_source),
            "source_font_asset_sha256": source_sha256,
            "staged_font_path": str(staged_path),
            "staged_font_sha256": staged_sha256,
            "staged_asset_verified": True,
            "staged_asset_read_only": True,
            "delivered_font_sha256": staged_sha256,
            # Legacy key retained as the actual path handed to ShapeString.
            "path": str(staged_path),
        }
    )
    return str(staged_path), evidence


def _verify_staged_shapestring_font_asset(
    font_delivery_evidence: Dict[str, Any],
) -> bool:
    """Re-read the exact staged asset and bind every use to its declared digest."""

    try:
        staged_path = Path(font_delivery_evidence["staged_font_path"])
        expected_sha256 = str(font_delivery_evidence["staged_font_sha256"])
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            or staged_path.stem != expected_sha256
            or font_delivery_evidence.get("staged_asset_verified") is not True
            or font_delivery_evidence.get("staged_asset_read_only") is not True
        ):
            return False
        canonical_root = (
            _shapestring_font_cache_dir() / "delivery-assets"
        ).resolve(strict=True)
        canonical_path, _payload, actual_sha256 = _read_stable_font_asset(
            staged_path,
            approved_root=canonical_root,
        )
        return bool(
            str(canonical_path) == str(staged_path.resolve(strict=True))
            and actual_sha256 == expected_sha256
            and actual_sha256 == font_delivery_evidence.get("delivered_font_sha256")
            and _font_asset_is_read_only(canonical_path)
        )
    except Exception:
        return False


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


class _RasterFallbackContext:
    """Executor-owned immutable handoff proving the exact terminal prefix."""

    __slots__ = ("requested_type", "attempted_types", "proof_chain")

    def __init__(self, requested_type, attempted_types, proof_chain):
        self.requested_type = requested_type
        self.attempted_types = tuple(attempted_types)
        self.proof_chain = tuple(copy.deepcopy(proof_chain))

    def __deepcopy__(self, _memo):
        return self


class _PageRasterFallbackContext:
    """Executor-owned page Raster authority sealed from one retained v2 page."""

    __slots__ = (
        "page_number",
        "source_item_id",
        "requested_type",
        "attempted_types",
        "proof_chain",
        "observation_sha256",
    )

    def __init__(
        self,
        *,
        page_number,
        source_item_id,
        requested_type,
        attempted_types,
        proof_chain,
        observation_sha256,
    ):
        self.page_number = page_number
        self.source_item_id = source_item_id
        self.requested_type = requested_type
        self.attempted_types = tuple(attempted_types)
        self.proof_chain = tuple(copy.deepcopy(proof_chain))
        self.observation_sha256 = observation_sha256

    def __deepcopy__(self, _memo):
        return self


def _authorize_page_raster_fallback(
    opts: ImportOptions,
    *,
    page_number: int,
    requested_type: str,
) -> Dict[str, Any]:
    """Seal a complete v2 page ladder before a non-direct page Raster exists."""

    requested = _normalize_requested_text_type(requested_type)
    scope_id = "p%d:page" % int(page_number)
    if scope_id not in list(
        getattr(opts, "text_delivery_obligation_source_item_ids", []) or []
    ):
        raise ValueError("page Raster fallback has no independent page obligation")
    observation = _validated_page_visual_observation(opts, int(page_number))
    authority = getattr(opts, "_page_visual_authority", None)
    ladder = list(TEXT_ITEM_FALLBACK_LADDERS[requested])
    attempted_types: List[str] = []
    proof_chain: List[Dict[str, Any]] = []
    for attempted in ladder:
        attempted_types.append(attempted)
        if attempted == "raster":
            break
        proof = build_page_visual_fallback_proof_v2(
            observation=observation,
            authority=authority,
            requested_type=requested,
            attempted_type=attempted,
        )
        if not page_visual_fallback_proof_v2_verified(
            proof,
            observation=observation,
            authority=authority,
            expected_requested_type=requested,
            expected_attempted_type=attempted,
        ):
            raise ValueError("page Raster fallback proof could not be verified")
        proof_chain.append(proof)
    if attempted_types != ladder or len(proof_chain) != len(ladder) - 1:
        raise ValueError("page Raster fallback ladder prefix is incomplete")
    context = _PageRasterFallbackContext(
        page_number=int(page_number),
        source_item_id=scope_id,
        requested_type=requested,
        attempted_types=attempted_types,
        proof_chain=proof_chain,
        observation_sha256=observation["observation_sha256"],
    )
    opts._page_raster_fallback_contexts[int(page_number)] = context
    return {
        "source_item_id": scope_id,
        "requested_type": requested,
        "attempted_types": attempted_types,
        "proof_chain": copy.deepcopy(proof_chain),
        "page_visual_observation_sha256": observation["observation_sha256"],
    }


def _validated_page_raster_request_context(
    opts: ImportOptions,
    page_number: int,
) -> Dict[str, Any]:
    """Accept direct Raster or one fully proven v2 page ladder, never a heuristic."""

    if str(getattr(opts, "import_mode", "") or "").strip().lower() == "raster":
        return {
            "direct_requested_raster": True,
            "page_fallback_authorized": False,
            "attempted_types": ["raster"],
            "proof_chain": [],
        }
    context = getattr(opts, "_page_raster_fallback_contexts", {}).get(page_number)
    if not isinstance(context, _PageRasterFallbackContext):
        raise ValueError("non-direct full-page Raster has no v2 ladder authority")
    observation = _validated_page_visual_observation(opts, page_number)
    authority = getattr(opts, "_page_visual_authority", None)
    ladder = list(TEXT_ITEM_FALLBACK_LADDERS.get(context.requested_type, ()))
    attempted_types = list(context.attempted_types)
    proof_chain = [copy.deepcopy(proof) for proof in context.proof_chain]
    if (
        context.page_number != page_number
        or context.source_item_id != "p%d:page" % page_number
        or context.source_item_id not in list(
            getattr(opts, "text_delivery_obligation_source_item_ids", []) or []
        )
        or attempted_types != ladder
        or len(proof_chain) != len(ladder) - 1
        or context.observation_sha256 != observation["observation_sha256"]
    ):
        raise ValueError("non-direct full-page Raster ladder authority is invalid")
    for attempted, proof in zip(ladder[:-1], proof_chain, strict=True):
        if not page_visual_fallback_proof_v2_verified(
            proof,
            observation=observation,
            authority=authority,
            expected_requested_type=context.requested_type,
            expected_attempted_type=attempted,
        ):
            raise ValueError("non-direct full-page Raster proof chain is invalid")
    return {
        "direct_requested_raster": False,
        "page_fallback_authorized": True,
        "source_item_id": context.source_item_id,
        "requested_type": context.requested_type,
        "attempted_types": attempted_types,
        "proof_chain": proof_chain,
    }


def _validated_raster_ladder_context(
    item: Dict[str, Any],
    attempted_type: str,
) -> Dict[str, Any]:
    """Accept Raster only from the executor's exact ladder prefix."""

    context = item.get("_raster_fallback_context") if isinstance(item, dict) else None
    requested = item.get("requested_type") if isinstance(item, dict) else None
    if (
        attempted_type != "raster"
        or not isinstance(context, _RasterFallbackContext)
        or requested not in TEXT_ITEM_FALLBACK_LADDERS
        or context.requested_type != requested
    ):
        raise ValueError("executor-owned raster ladder prefix is invalid")
    ladder = list(TEXT_ITEM_FALLBACK_LADDERS[requested])
    try:
        terminal_index = ladder.index("raster")
    except ValueError as exc:
        raise ValueError("executor-owned raster ladder prefix is invalid") from exc
    expected_prefix = ladder[: terminal_index + 1]
    attempted_types = list(context.attempted_types)
    proof_chain = [copy.deepcopy(proof) for proof in context.proof_chain]
    if (
        attempted_types != expected_prefix
        or len(proof_chain) != len(expected_prefix) - 1
    ):
        raise ValueError("executor-owned raster ladder prefix is invalid")
    for prior_type, proof in zip(expected_prefix[:-1], proof_chain, strict=True):
        _validate_item_impossibility_proof(item, requested, prior_type, proof)
    return {
        "requested_type": requested,
        "attempted_types": attempted_types,
        "proof_chain": proof_chain,
        "direct_requested_raster": requested == "raster",
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


def _raw_span_text(source_span: Dict[str, Any]) -> str:
    """Return exact span text from either PyMuPDF dict or rawdict data."""
    if "text" in source_span:
        source_text = source_span.get("text")
        if not isinstance(source_text, str):
            raise ValueError("text span content must be a string")
        return source_text
    chars = source_span.get("chars", [])
    if not isinstance(chars, list):
        raise ValueError("raw text span chars must be a list")
    text_parts: List[str] = []
    for char_index, char in enumerate(chars):
        if not isinstance(char, dict):
            raise ValueError("raw text character must be a dictionary")
        value = char.get("c", "")
        if not isinstance(value, str):
            raise ValueError(
                "raw text character %d content must be a string" % char_index
            )
        text_parts.append(value)
    return "".join(text_parts)


def _source_ink_evidence_digest(evidence: Dict[str, Any]) -> str:
    from pdfcadcore.source_ink import source_ink_evidence_digest

    return source_ink_evidence_digest(evidence)


def _trace_font_key(value: Any) -> str:
    return _canonical_font_identity(str(value or ""))["normalized_key"]


def _bind_page_text_source_ink_evidence(
    page,
    raw_tdict: Dict[str, Any],
    items: List[Dict[str, Any]],
    *,
    opts: Optional[ImportOptions] = None,
) -> List[Dict[str, Any]]:
    """Bind source items to physical PDF paint evidence without Unicode guesses.

    PyMuPDF rawdict marks synthesized characters that have no PDF text-paint
    operator. Non-synthetic characters are matched by codepoint, font and
    origin to get_texttrace glyph ids, then inspected in the exact embedded
    font program with FontTools. An unresolved character yields no authority.
    """
    if not isinstance(raw_tdict, dict) or not isinstance(items, list):
        raise ValueError("source ink binding context is invalid")
    if opts is not None:
        _invoke_import_cancellation_checkpoint(opts, "source ink preprocessing")
    bound_items = copy.deepcopy(items)
    blocks = raw_tdict.get("blocks", [])
    if not isinstance(blocks, list):
        return bound_items

    try:
        traces = list(page.get_texttrace() or [])
    except (AttributeError, RuntimeError, TypeError, ValueError):
        traces = []
    trace_chars: List[Dict[str, Any]] = []
    for trace_index, trace in enumerate(traces):
        if opts is not None:
            _invoke_import_cancellation_checkpoint(opts, "source ink trace")
        if not isinstance(trace, dict):
            continue
        trace_font = str(trace.get("font", "") or "")
        try:
            trace_type = int(trace.get("type", 0) or 0)
            opacity = float(trace.get("opacity", 1.0))
        except (TypeError, ValueError):
            continue
        for trace_char_index, record in enumerate(trace.get("chars") or ()):
            if opts is not None:
                _invoke_import_cancellation_checkpoint(
                    opts, "source ink trace character"
                )
            try:
                codepoint = int(record[0])
                glyph_id = int(record[1])
                origin = _finite_source_tuple(record[2], 2, "trace.origin")
            except (IndexError, TypeError, ValueError):
                continue
            trace_chars.append(
                {
                    "trace_index": trace_index,
                    "trace_char_index": trace_char_index,
                    "codepoint": codepoint,
                    "glyph_id": glyph_id,
                    "origin": origin,
                    "font": trace_font,
                    "font_key": _trace_font_key(trace_font),
                    "trace_type": trace_type,
                    "opacity": opacity,
                }
            )

    catalog = None
    font_cache: Dict[str, Any] = {}
    claimed_trace_chars: set = set()

    def resolved_font_asset(
        source_font: str,
        preferred_font: str = "",
    ) -> Optional[Tuple[Any, Dict[str, Any]]]:
        nonlocal catalog
        if catalog is None:
            try:
                from pdfcadcore.embedded_fonts import EmbeddedFontCatalog

                page_number = (
                    int(bound_items[0].get("page_number", 1)) if bound_items else 1
                )
                catalog = EmbeddedFontCatalog.from_page(page, page_number)
            except (ImportError, RuntimeError, TypeError, ValueError):
                catalog = False
        if catalog is False:
            return None
        asset = catalog.for_span(preferred_font) if preferred_font else None
        if asset is None:
            asset = catalog.for_span(source_font)
        if asset is None:
            return None
        from pdfcadcore.source_ink import verified_font_asset_binding

        binding = verified_font_asset_binding(asset)
        if binding is None:
            return None
        return asset, binding

    def exact_glyph_record(
        raw_char: Dict[str, Any],
        source_font: str,
    ) -> Optional[Dict[str, Any]]:
        nonlocal catalog
        value = raw_char.get("c")
        if not isinstance(value, str) or len(value) != 1:
            return None
        try:
            raw_origin = _finite_source_tuple(raw_char.get("origin"), 2, "char.origin")
        except ValueError:
            return None
        font_key = _trace_font_key(source_font)
        candidates = []
        for index, candidate in enumerate(trace_chars):
            if opts is not None:
                _invoke_import_cancellation_checkpoint(
                    opts, "source ink candidate character"
                )
            if index in claimed_trace_chars:
                continue
            if (
                candidate["codepoint"] != ord(value)
                or candidate["font_key"] != font_key
            ):
                continue
            distance = math.hypot(
                candidate["origin"][0] - raw_origin[0],
                candidate["origin"][1] - raw_origin[1],
            )
            if distance <= 0.05:
                candidates.append((distance, index, candidate))
        if not candidates:
            return None
        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        if len(candidates) > 1 and math.isclose(
            candidates[0][0], candidates[1][0], abs_tol=1e-9
        ):
            tied = [
                entry[2]
                for entry in candidates
                if math.isclose(entry[0], candidates[0][0], abs_tol=1e-9)
            ]
            signatures = {
                (
                    candidate["glyph_id"],
                    candidate["trace_type"],
                    candidate["opacity"],
                    candidate["font_key"],
                )
                for candidate in tied
            }
            if len(signatures) != 1:
                return None
        _distance, trace_char_id, trace_record = candidates[0]

        asset_result = resolved_font_asset(source_font, trace_record["font"])
        if asset_result is None:
            return None
        asset, font_asset_binding = asset_result
        font = font_cache.get(font_asset_binding["usable_font_sha256"])
        if font is None:
            try:
                from fontTools.ttLib import TTFont

                font = TTFont(BytesIO(asset.usable_bytes), lazy=False)
                font_cache[font_asset_binding["usable_font_sha256"]] = font
            except (ImportError, OSError, RuntimeError, TypeError, ValueError):
                return None
        try:
            from fontTools.pens.boundsPen import BoundsPen

            glyph_order = font.getGlyphOrder()
            glyph_id = int(trace_record["glyph_id"])
            if glyph_id < 0 or glyph_id >= len(glyph_order):
                return None
            glyph_name = glyph_order[glyph_id]
            glyph_set = font.getGlyphSet()
            pen = BoundsPen(glyph_set)
            glyph_set[glyph_name].draw(pen)
            bounds = None if pen.bounds is None else [float(v) for v in pen.bounds]
            advance_width = float(font["hmtx"].metrics[glyph_name][0])
        except (AttributeError, IndexError, KeyError, RuntimeError, TypeError, ValueError):
            return None
        claimed_trace_chars.add(trace_char_id)
        nonpainting = bool(
            trace_record["trace_type"] == 3 or trace_record["opacity"] <= 0.0
        )
        from pdfcadcore.source_ink import layout_only_glyph_name

        layout_only_zero_ink = bool(
            bounds is None
            and advance_width > 0.0
            and layout_only_glyph_name(glyph_name)
        )
        return {
            "authority": (
                "pymupdf_texttrace_nonpainting_render_mode"
                if nonpainting
                else "exact_pdf_font_glyph_bounds"
            ),
            "character": value,
            "synthetic": False,
            "glyph_id": glyph_id,
            "glyph_name": glyph_name,
            "glyph_bounds": bounds,
            "advance_width": advance_width,
            "layout_only_zero_ink": layout_only_zero_ink,
            "font_asset_binding": copy.deepcopy(font_asset_binding),
            "source_font_sha256": font_asset_binding["source_font_sha256"],
            "usable_font_sha256": font_asset_binding["usable_font_sha256"],
            "trace_type": trace_record["trace_type"],
            "opacity": trace_record["opacity"],
            "zero_visible_ink": True if nonpainting else bounds is None,
            "physically_resolved": True,
        }

    try:
        for item in bound_items:
            if opts is not None:
                _invoke_import_cancellation_checkpoint(opts, "source ink item")
            source_text = item.get("text") if isinstance(item, dict) else None
            if not isinstance(source_text, str) or not source_text:
                continue
            try:
                block = blocks[item["block_index"]]
                line = block["lines"][item["line_index"]]
                raw_span = line["spans"][item["span_index"]]
            except (IndexError, KeyError, TypeError):
                continue
            raw_chars = raw_span.get("chars", [])
            if not isinstance(raw_chars, list) or not raw_chars:
                continue
            if _raw_span_text(raw_span) != item.get("text"):
                continue
            source_font = str(raw_span.get("font", "") or "")
            character_evidence: List[Dict[str, Any]] = []
            resolved = True
            for source_index, raw_char in enumerate(raw_chars):
                if opts is not None:
                    _invoke_import_cancellation_checkpoint(
                        opts, "source ink character"
                    )
                if not isinstance(raw_char, dict):
                    resolved = False
                    break
                value = raw_char.get("c")
                if not isinstance(value, str) or not value:
                    resolved = False
                    break
                if raw_char.get("synthetic") is True:
                    resolved = False
                    break
                else:
                    record = exact_glyph_record(raw_char, source_font)
                    if record is None:
                        resolved = False
                        break
                record["source_index"] = source_index
                character_evidence.append(record)
            if not resolved or "".join(
                record["character"] for record in character_evidence
            ) != item.get("text"):
                continue
            zero_visible_ink = all(
                record["zero_visible_ink"] for record in character_evidence
            )
            any_zero_visible_ink = any(
                record["zero_visible_ink"] for record in character_evidence
            )
            zero_ink_records = [
                record
                for record in character_evidence
                if record["zero_visible_ink"]
            ]
            font_asset_bindings: List[Dict[str, Any]] = []
            seen_asset_ids = set()
            for record in character_evidence:
                binding = record.get("font_asset_binding")
                asset_id = binding.get("asset_id") if isinstance(binding, dict) else None
                if isinstance(asset_id, str) and asset_id not in seen_asset_ids:
                    seen_asset_ids.add(asset_id)
                    font_asset_bindings.append(copy.deepcopy(binding))
            glyph_id_sequence = [
                record.get("glyph_id") for record in character_evidence
            ]
            evidence = {
                "schema": "pdf_source_ink_evidence_v1",
                "authority": "pymupdf_rawdict_texttrace_exact_font",
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "source_item_id": item["source_item_id"],
                "source_text": item["text"],
                "source_text_sha256": hashlib.sha256(
                    item["text"].encode("utf-8")
                ).hexdigest(),
                "font_identity": copy.deepcopy(item["font_identity"]),
                "classification": (
                    "zero_visible_ink"
                    if zero_visible_ink
                    else "mixed_visible_and_zero_ink"
                    if any_zero_visible_ink
                    else "visible_ink"
                ),
                "zero_ink_characters_layout_only": bool(
                    zero_ink_records
                    and all(
                        record.get("layout_only_zero_ink") is True
                        for record in zero_ink_records
                    )
                ),
                "all_characters_physically_resolved": True,
                "font_asset_bindings": font_asset_bindings,
                "glyph_id_sequence": glyph_id_sequence,
                "characters": character_evidence,
            }
            evidence["evidence_sha256"] = _source_ink_evidence_digest(evidence)
            item["source_ink_evidence"] = evidence
            item["source_font_asset_bindings"] = copy.deepcopy(font_asset_bindings)
            item["source_glyph_id_sequence"] = list(glyph_id_sequence)
    finally:
        for font in font_cache.values():
            try:
                font.close()
            except (AttributeError, RuntimeError):
                pass
    return bound_items


def _validated_source_ink_evidence(item: Dict[str, Any]) -> Dict[str, Any]:
    evidence = item.get("source_ink_evidence") if isinstance(item, dict) else None
    if not isinstance(evidence, dict):
        raise ValueError("source ink evidence is unavailable")
    from pdfcadcore.source_ink import source_ink_evidence_verified

    if not source_ink_evidence_verified(
        evidence,
        expected_pdf_sha256=item.get("pdf_sha256"),
        expected_page_number=item.get("page_number"),
        expected_source_item_id=item.get("source_item_id"),
        expected_source_text=item.get("text"),
        expected_font_identity=item.get("font_identity"),
        expected_font_asset_bindings=item.get("source_font_asset_bindings"),
        expected_glyph_id_sequence=item.get("source_glyph_id_sequence"),
    ):
        raise ValueError("source ink evidence is not physically source-bound")
    return copy.deepcopy(evidence)


def _build_source_ink_segments(item: Dict[str, Any]) -> Dict[str, Any]:
    """Build a deterministic run manifest for one mixed-ink source item.

    The parent span remains the sole delivery obligation.  Contiguous physical
    paint runs are child identities only; every child retains the parent's
    requested representation so ink fidelity can never authorize a rung change.
    """

    evidence = _validated_source_ink_evidence(item)
    parent_source_item_id = item.get("source_item_id")
    requested_type = item.get("requested_type")
    source_text = item.get("text")
    characters = evidence.get("characters")
    if (
        evidence.get("classification") != "mixed_visible_and_zero_ink"
        or not isinstance(parent_source_item_id, str)
        or not parent_source_item_id
        or requested_type not in TEXT_ITEM_FALLBACK_LADDERS
        or requested_type == "raster"
        or not isinstance(source_text, str)
        or not source_text
        or not isinstance(characters, list)
        or not characters
    ):
        raise ValueError("source-ink segmentation requires one mixed structural item")

    segments: List[Dict[str, Any]] = []
    run_start = 0
    run_zero_ink = bool(characters[0]["zero_visible_ink"])
    for source_index in range(1, len(characters) + 1):
        next_zero_ink = (
            bool(characters[source_index]["zero_visible_ink"])
            if source_index < len(characters)
            else not run_zero_ink
        )
        if source_index < len(characters) and next_zero_ink == run_zero_ink:
            continue
        run_records = characters[run_start:source_index]
        run_text = "".join(record["character"] for record in run_records)
        if not run_text:
            raise ValueError("source-ink segment text is empty")
        segment_index = len(segments)
        segments.append(
            {
                "child_source_item_id": "%s:seg%d"
                % (parent_source_item_id, segment_index),
                "parent_source_item_id": parent_source_item_id,
                "requested_type": requested_type,
                "source_index_start": run_start,
                "source_index_end": source_index,
                "source_text": run_text,
                "source_text_sha256": hashlib.sha256(
                    run_text.encode("utf-8")
                ).hexdigest(),
                "physical_role": (
                    "zero_visible_ink" if run_zero_ink else "visible"
                ),
            }
        )
        run_start = source_index
        run_zero_ink = next_zero_ink

    if (
        len(segments) < 2
        or "".join(segment["source_text"] for segment in segments) != source_text
        or segments[0]["source_index_start"] != 0
        or segments[-1]["source_index_end"] != len(characters)
        or any(
            left["source_index_end"] != right["source_index_start"]
            for left, right in zip(segments, segments[1:], strict=False)
        )
    ):
        raise ValueError("source-ink segment coverage is incomplete")

    manifest: Dict[str, Any] = {
        "schema": "bcs.freecad_text_source_segments/1.0",
        "parent_source_item_id": parent_source_item_id,
        "requested_type": requested_type,
        "parent_source_ink_evidence_sha256": evidence["evidence_sha256"],
        "source_text": source_text,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "segments": segments,
    }
    serialized = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    manifest["manifest_sha256"] = hashlib.sha256(serialized).hexdigest()
    return manifest


def _build_source_ink_segment_items(
    item: Dict[str, Any],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Materialize source-bound child items for a mixed-ink run manifest."""

    manifest = _build_source_ink_segments(item)
    parent_evidence = _validated_source_ink_evidence(item)
    span = item.get("span")
    span_characters = span.get("chars") if isinstance(span, dict) else None
    evidence_characters = parent_evidence["characters"]
    if (
        not isinstance(span_characters, list)
        or len(span_characters) != len(evidence_characters)
        or "".join(str(character.get("c") or "") for character in span_characters)
        != item.get("text")
    ):
        raise ValueError("mixed source item lacks exact raw character geometry")

    children: List[Dict[str, Any]] = []
    parent_id = manifest["parent_source_item_id"]
    for segment_index, segment in enumerate(manifest["segments"]):
        start = segment["source_index_start"]
        end = segment["source_index_end"]
        raw_run = copy.deepcopy(span_characters[start:end])
        evidence_run = copy.deepcopy(evidence_characters[start:end])
        if not raw_run or not evidence_run or len(raw_run) != len(evidence_run):
            raise ValueError("mixed source segment is empty or misaligned")
        try:
            boxes = [
                _finite_source_tuple(character.get("bbox"), 4, "segment char bbox")
                for character in raw_run
            ]
            run_bbox = (
                min(box[0] for box in boxes),
                min(box[1] for box in boxes),
                max(box[2] for box in boxes),
                max(box[3] for box in boxes),
            )
            run_origin = _finite_source_tuple(
                raw_run[0].get("origin"), 2, "segment char origin"
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("mixed source segment geometry is invalid") from exc
        if run_bbox[2] <= run_bbox[0] or run_bbox[3] <= run_bbox[1]:
            raise ValueError("mixed source segment bbox is empty")

        for child_index, record in enumerate(evidence_run):
            record["source_index"] = child_index
        child_bindings: List[Dict[str, Any]] = []
        for binding in parent_evidence["font_asset_bindings"]:
            if any(record.get("font_asset_binding") == binding for record in evidence_run):
                child_bindings.append(copy.deepcopy(binding))
        child_glyph_ids = [record["glyph_id"] for record in evidence_run]
        child_evidence = copy.deepcopy(parent_evidence)
        child_evidence.update(
            {
                "source_item_id": segment["child_source_item_id"],
                "source_text": segment["source_text"],
                "source_text_sha256": segment["source_text_sha256"],
                "classification": (
                    "zero_visible_ink"
                    if segment["physical_role"] == "zero_visible_ink"
                    else "visible_ink"
                ),
                "zero_ink_characters_layout_only": bool(
                    segment["physical_role"] == "zero_visible_ink"
                    and all(record["layout_only_zero_ink"] for record in evidence_run)
                ),
                "font_asset_bindings": child_bindings,
                "glyph_id_sequence": child_glyph_ids,
                "characters": evidence_run,
            }
        )
        child_evidence["evidence_sha256"] = _source_ink_evidence_digest(child_evidence)

        child = copy.deepcopy(item)
        child_span = copy.deepcopy(span)
        child_span.update(
            {
                "text": segment["source_text"],
                "chars": raw_run,
                "bbox": run_bbox,
                "origin": run_origin,
            }
        )
        child.update(
            {
                "source_item_id": segment["child_source_item_id"],
                "parent_source_item_id": parent_id,
                "source_segment_index": segment_index,
                "source_segment_physical_role": segment["physical_role"],
                "source_segment_manifest_sha256": manifest["manifest_sha256"],
                "text": segment["source_text"],
                "bbox": run_bbox,
                "origin": run_origin,
                "span": child_span,
                "source_ink_evidence": child_evidence,
                "source_font_asset_bindings": child_bindings,
                "source_glyph_id_sequence": child_glyph_ids,
            }
        )
        _validated_source_ink_evidence(child)
        children.append(child)
    return manifest, children


def _canonical_or_segment_source_id_matches(item: Dict[str, Any]) -> bool:
    """Validate either a canonical parent id or its exact manifest child id."""

    try:
        canonical_id = "p%d:b%d:l%d:s%d" % (
            item["page_number"],
            item["block_index"],
            item["line_index"],
            item["span_index"],
        )
    except (KeyError, TypeError, ValueError):
        return False
    source_item_id = item.get("source_item_id")
    parent_source_item_id = item.get("parent_source_item_id")
    segment_index = item.get("source_segment_index")
    if parent_source_item_id is None and segment_index is None:
        return source_item_id == canonical_id
    return bool(
        parent_source_item_id == canonical_id
        and type(segment_index) is int
        and segment_index >= 0
        and source_item_id == "%s:seg%d" % (canonical_id, segment_index)
    )


def _source_ink_delivery_binding_fields(
    item: Dict[str, Any],
    evidence: Dict[str, Any],
) -> Dict[str, Any]:
    """Copy the independent canonical fields report validation must compare."""

    validated = _validated_source_ink_evidence(item)
    if validated != evidence:
        raise ValueError("source ink delivery evidence changed after validation")
    return {
        "source_pdf_sha256": item["pdf_sha256"],
        "source_page_number": item["page_number"],
        "source_font_identity": copy.deepcopy(item["font_identity"]),
        "source_font_asset_bindings": copy.deepcopy(
            item["source_font_asset_bindings"]
        ),
        "source_glyph_id_sequence": list(item["source_glyph_id_sequence"]),
    }


def _mixed_source_ink_requires_fallback(evidence: Dict[str, Any]) -> bool:
    """Compatibility predicate: mixed physical ink never authorizes fallback."""

    del evidence
    return False


def _raise_mixed_source_ink_impossible(
    item: Dict[str, Any],
    attempted_type: str,
) -> None:
    """Reject the retired mixed-ink fallback authority."""

    del item, attempted_type
    raise ValueError(
        "mixed source ink must be delivered as same-representation segments"
    )


def _persist_source_ink_evidence(host_obj, evidence: Dict[str, Any]) -> None:
    digest = str(evidence.get("evidence_sha256") or "")
    classification = str(evidence.get("classification") or "")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None or classification not in {
        "zero_visible_ink",
        "visible_ink",
        "mixed_visible_and_zero_ink",
    }:
        raise ValueError("source ink evidence cannot be persisted")
    properties = set(getattr(host_obj, "PropertiesList", []) or [])
    add_property = getattr(host_obj, "addProperty", None)
    for name, value in (
        ("PDFSourceInkClassification", classification),
        ("PDFSourceInkEvidenceSHA256", digest),
    ):
        if name not in properties and callable(add_property):
            add_property("App::PropertyString", name, "PDF Import")
            properties.add(name)
        setattr(host_obj, name, value)


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
                source_text = _raw_span_text(source_span)
                if not source_text:
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
                # rawdict stores characters instead of the convenience `text`
                # field. Delivery code receives one uniform exact span without
                # trimming leading, trailing, or all-whitespace content.
                span["text"] = source_text

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
    if reason_code in {
        "mixed_source_ink_not_exactly_representable",
        "exact_font_unavailable",
    }:
        raise ValueError(
            "%s is fidelity evidence, not fallback authority" % reason_code
        )
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

    raise ValueError("representation has no closed impossibility predicate")


def _record_text_mode_fallback(
    opts: ImportOptions,
    *,
    requested: str,
    delivered: str,
    reason: str,
    count: int,
    source_item_id: str,
    proof: Dict[str, Any],
    event_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Record an evidence-backed item- or page-scoped representation fallback."""
    requested = str(requested or "").strip().lower()
    delivered = str(delivered or "").strip().lower()
    reason = str(reason or "").strip()
    source_item_id = str(source_item_id or "").strip()
    proof = dict(proof or {})
    event_metadata = dict(event_metadata or {})
    page_scoped = bool(
        proof.get("page_specific_proven_impossible") is True
        and re.fullmatch(r"p[1-9][0-9]*:page", source_item_id) is not None
    )
    proof_has_authority_evidence = bool(
        str(proof.get("evidence") or "").strip()
        or (
            page_scoped
            and proof.get("schema") == PAGE_VISUAL_FALLBACK_PROOF_V2_SCHEMA
            and isinstance(proof.get("proof_sha256"), str)
        )
    )
    if page_scoped:
        expected_metadata_fields = {
            "attempted_types",
            "proof_chain",
            "transition_chain",
            "created_entity_ids",
            "removed_entity_ids",
            "cleanup_complete",
        }
        if set(event_metadata) != expected_metadata_fields:
            raise ValueError("page fallback event metadata is incomplete")
        metadata_source = event_metadata
    else:
        if event_metadata:
            raise ValueError("item fallback metadata must remain proof-bound")
        metadata_source = proof
    attempted_types = [
        str(value or "").strip().lower()
        for value in list(metadata_source.get("attempted_types") or [])
        if str(value or "").strip()
    ]
    scope_is_valid = bool(
        proof.get("item_specific_proven_impossible") is True
        or page_scoped
    )
    if (
        not scope_is_valid
        or not proof_has_authority_evidence
        or requested not in attempted_types
    ):
        raise ValueError(
            "requested representation must be exactly proven impossible"
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
            if page_scoped and any(
                event.get(field_name) != event_metadata.get(field_name)
                for field_name in event_metadata
            ):
                raise ValueError("conflicting page fallback event metadata")
            source_ids = list(event.get("source_item_ids") or [])
            if source_item_id not in source_ids:
                source_ids.append(source_item_id)
                prior_count = event.get("count")
                if type(prior_count) is not int or prior_count <= 0:
                    raise ValueError("existing fallback record count is invalid")
                event["count"] = prior_count + count
            event["source_item_ids"] = source_ids
            return
    event = {
        "requested": requested,
        "delivered": delivered,
        "reason": reason,
        "count": count,
        "source_item_ids": [source_item_id],
        "proof": proof,
    }
    event.update(copy.deepcopy(event_metadata))
    events.append(event)


def _record_explicit_page_raster_delivery(
    opts: ImportOptions,
    *,
    page_num: int,
    raster_result: Dict[str, Any],
    pdf_sha256: Optional[str] = None,
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
    digest = (
        pdf_sha256.strip().lower()
        if isinstance(pdf_sha256, str)
        else str(getattr(opts, "_pdf_sha256", "") or "").strip().lower()
    )
    raster_asset_sha256 = raster_evidence.get("source_asset_sha256")
    if (
        re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or raster_evidence.get("pdf_sha256") != digest
        or raster_evidence.get("source_page_number") != page_number
        or raster_evidence.get("host_entity_type") != "Image::ImagePlane"
        or not isinstance(raster_asset_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", raster_asset_sha256) is None
        or raster_evidence.get("raster_content_verified") is not True
        or raster_evidence.get("raster_file_included") is not True
    ):
        raise ValueError("verified page Raster evidence is invalid")
    source_item_id = "p%d:page" % page_number
    _register_text_delivery_obligations(opts, [source_item_id])
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
        "record_verified": True,
        "type_verified": True,
        "visual_verified": True,
        "ownership_verified": True,
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
    """Record a finite page-scoped visual ladder without fabricating a text item."""
    requested = _normalize_requested_text_type(
        str(getattr(opts, "text_mode", "") or "")
    )
    if requested == "raster":
        return _record_explicit_page_raster_delivery(
            opts,
            page_num=page_num,
            raster_result=raster_result,
            pdf_sha256=pdf_sha256,
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

    source_scope_id = "p%d:page" % page_number
    authority = getattr(opts, "_page_visual_authority", None)
    if authority is None:
        raise ValueError("page visual source authority is required")
    source_bytes = _validated_pdf_source_bytes(opts)
    if (
        digest != str(getattr(opts, "_pdf_sha256", "") or "")
        or hashlib.sha256(source_bytes).hexdigest() != digest
    ):
        raise ValueError("page visual authority source bytes do not match the PDF digest")
    try:
        page_source_observation = authority.observation(
            page_number,
            source_scope_id,
        )
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "page visual authority does not cover the selected page"
        ) from exc
    retained_observation = getattr(
        opts,
        "page_visual_source_observations",
        {},
    ).get(source_scope_id)
    if (
        getattr(authority, "pdf_sha256", None) != digest
        or retained_observation != page_source_observation
        or not page_visual_source_observation_v2_verified(
            retained_observation,
            authority,
        )
    ):
        raise ValueError("page visual authority observation is invalid")

    # The live raw dictionary remains a conservative extraction consistency
    # check. It can reject a fallback, but it can never authorize one.
    try:
        canonical_source_items = list(
            _iter_text_source_items(raw_tdict, page_number, digest, requested)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("no-source page fallback source text is invalid") from exc
    if canonical_source_items:
        raise ValueError("no-source page fallback found canonical source text")
    _register_text_delivery_obligations(opts, [source_scope_id])

    raster_ids = _validated_entity_ids(
        raster_result.get("created_entity_ids"),
        field_name="raster_result.created_entity_ids",
        allow_empty=False,
    )
    raster_evidence = dict(raster_result.get("evidence") or {})
    raster_asset_sha256 = raster_evidence.get("source_asset_sha256")
    if raster_evidence.get("pdf_sha256") != digest:
        raise ValueError("page raster PDF binding is invalid")
    if (
        raster_evidence.get("host_entity_type") != "Image::ImagePlane"
        or raster_evidence.get("source_page_number") != page_number
        or not isinstance(raster_asset_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", raster_asset_sha256) is None
        or raster_evidence.get("raster_content_verified") is not True
        or raster_evidence.get("raster_file_included") is not True
    ):
        raise ValueError("verified page raster evidence is invalid")

    ladder = list(TEXT_ITEM_FALLBACK_LADDERS[requested])
    attempted_types: List[str] = []
    proof_chain: List[Dict[str, Any]] = []
    attempts: List[Dict[str, Any]] = []

    attempt_ledger = getattr(opts, "text_delivery_attempts", None)
    fallback_ledger = getattr(opts, "text_mode_fallbacks", None)
    delivery_counts = getattr(opts, "text_delivered_counts", None)
    source_observations = getattr(opts, "page_visual_source_observations", None)
    if (
        not isinstance(attempt_ledger, list)
        or not isinstance(fallback_ledger, list)
        or not isinstance(delivery_counts, dict)
        or not isinstance(source_observations, dict)
    ):
        raise ValueError("no-source text fallback ledgers are unavailable")

    prior_attempts = copy.deepcopy(attempt_ledger)
    prior_fallbacks = copy.deepcopy(fallback_ledger)
    prior_delivery_counts = copy.deepcopy(delivery_counts)
    prior_source_observations = copy.deepcopy(source_observations)
    staged_opts = copy.copy(opts)
    staged_opts.text_delivery_attempts = copy.deepcopy(prior_attempts)
    staged_opts.text_mode_fallbacks = copy.deepcopy(prior_fallbacks)
    staged_opts.text_delivered_counts = copy.deepcopy(prior_delivery_counts)
    staged_opts.page_visual_source_observations = copy.deepcopy(
        prior_source_observations
    )
    staged_opts._page_visual_authority = authority
    staged_opts._page_visual_session_anchor = copy.deepcopy(
        getattr(opts, "_page_visual_session_anchor", None)
    )

    try:
        for index, attempted_type in enumerate(ladder):
            attempted_types.append(attempted_type)
            if attempted_type == "raster":
                break
            following_type = ladder[index + 1]
            proof = build_page_visual_fallback_proof_v2(
                observation=page_source_observation,
                authority=authority,
                requested_type=requested,
                attempted_type=attempted_type,
            )
            if not page_visual_fallback_proof_v2_verified(
                proof,
                observation=page_source_observation,
                authority=authority,
                expected_requested_type=requested,
                expected_attempted_type=attempted_type,
            ):
                raise ValueError("page visual fallback proof could not be sealed")
            attempt = {
                "source_item_id": source_scope_id,
                "requested_type": requested,
                "attempted_type": attempted_type,
                "final_type": None,
                "outcome": "proven_impossible",
                "reason": proof["reason_code"],
                "reason_code": proof["reason_code"],
                "transition_from": attempted_type,
                "transition_to": following_type,
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
                "evidence": {
                    "page_visual_importer_identity": (
                        FREECAD_TEXT_IMPORTER_IDENTITY
                    ),
                    "page_visual_observation_sha256": proof[
                        "observation_sha256"
                    ],
                    "page_visual_session_anchor_sha256": proof[
                        "session_anchor_sha256"
                    ],
                },
                "proof": proof,
            }
            _append_text_item_attempt(staged_opts, attempt)
            attempts.append(attempt)
            proof_chain.append(proof)

        terminal_evidence = copy.deepcopy(raster_evidence)
        terminal_evidence.update(
            {
                "source_pdf_sha256": digest,
                "source_page_number": page_number,
                "page_visual_scope_id": source_scope_id,
                "page_visual_importer_identity": FREECAD_TEXT_IMPORTER_IDENTITY,
                "page_visual_observation_sha256": page_source_observation[
                    "observation_sha256"
                ],
                "page_visual_session_anchor_sha256": proof_chain[-1][
                    "session_anchor_sha256"
                ],
                "page_visual_fallback_proof": copy.deepcopy(proof_chain[-1]),
            }
        )
        verified_attempt = {
            "source_item_id": source_scope_id,
            "requested_type": requested,
            "attempted_type": "raster",
            "final_type": "raster",
            "outcome": "verified",
            "reason": "verified page Raster after finite page-scoped proof chain",
            "created_entity_ids": list(raster_ids),
            "delivery_entity_ids": list(raster_ids),
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "record_verified": True,
            "type_verified": True,
            "visual_verified": True,
            "ownership_verified": True,
            "delivery_count": len(raster_ids),
            "attempted_types": list(attempted_types),
            "proof_chain": [copy.deepcopy(proof) for proof in proof_chain],
            "evidence": terminal_evidence,
        }
        _append_text_item_attempt(staged_opts, verified_attempt)
        attempts.append(verified_attempt)

        fallback_proof = copy.deepcopy(proof_chain[-1])
        _record_text_mode_fallback(
            staged_opts,
            requested=requested,
            delivered="raster",
            reason="proof_gated:%s:%s"
            % (requested, fallback_proof["reason_code"]),
            count=1,
            source_item_id=source_scope_id,
            proof=fallback_proof,
            event_metadata={
                "attempted_types": list(attempted_types),
                "proof_chain": [copy.deepcopy(proof) for proof in proof_chain],
                "transition_chain": [
                    {"from": left, "to": right}
                    for left, right in zip(ladder, ladder[1:], strict=False)
                ],
                "created_entity_ids": [
                    entity_id
                    for attempt in attempts[:-1]
                    for entity_id in list(attempt.get("created_entity_ids") or [])
                ],
                "removed_entity_ids": [
                    entity_id
                    for attempt in attempts[:-1]
                    for entity_id in list(attempt.get("removed_entity_ids") or [])
                ],
                "cleanup_complete": True,
            },
        )
        _record_text_delivery(staged_opts, "raster_text_patch", len(raster_ids))

        attempt_ledger[:] = copy.deepcopy(staged_opts.text_delivery_attempts)
        fallback_ledger[:] = copy.deepcopy(staged_opts.text_mode_fallbacks)
        delivery_counts.clear()
        delivery_counts.update(copy.deepcopy(staged_opts.text_delivered_counts))
        source_observations.clear()
        source_observations.update(
            copy.deepcopy(staged_opts.page_visual_source_observations)
        )
    except Exception:
        attempt_ledger[:] = prior_attempts
        fallback_ledger[:] = prior_fallbacks
        delivery_counts.clear()
        delivery_counts.update(prior_delivery_counts)
        source_observations.clear()
        source_observations.update(prior_source_observations)
        raise

    return {
        "entity_type": "raster",
        "count": len(raster_ids),
        "source_item_count": 0,
        "source_item_ids": [source_scope_id],
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
            for left, right in zip(
                attempted_types, attempted_types[1:], strict=False
            )
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
        _invoke_import_cancellation_checkpoint(
            opts,
            "text fallback: %s" % attempted_mode,
        )
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

        delivery_item = copy.deepcopy(bound_item)
        if attempted_mode == "raster":
            delivery_item["_raster_fallback_context"] = _RasterFallbackContext(
                requested_mode,
                attempted_types,
                validated_proofs,
            )
        try:
            result = deliverer(delivery_item, attempted_mode, opts)
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


def _host_object_persistent_name(obj) -> str:
    """Return the immutable FreeCAD document name used by FCStd ObjectData."""

    try:
        return str(getattr(obj, "Name", "") or "")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""


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


def _host_property_type_id(obj: Any, property_name: str) -> str:
    """Return the host's persisted property type without guessing from its value."""

    try:
        getter = getattr(obj, "getTypeIdOfProperty", None)
        return str(getter(property_name) or "") if callable(getter) else ""
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""


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


def _raw_shape_number(value: Any) -> Optional[float]:
    """Return a finite round-trip value for pairwise tolerance comparison."""

    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, RuntimeError, AttributeError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return 0.0 if number == 0.0 else number


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


def _host_point_sample(point: Any) -> Optional[List[float]]:
    """Read a point without introducing discontinuous pre-comparison rounding."""

    coordinates: List[float] = []
    for lower_name, upper_name in (("x", "X"), ("y", "Y"), ("z", "Z")):
        try:
            value = getattr(point, lower_name)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            try:
                value = getattr(point, upper_name)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                return None
        coordinate = _raw_shape_number(value)
        if coordinate is None:
            return None
        coordinates.append(coordinate)
    return coordinates


def _canonical_point_path(
    points: List[Tuple[int, int, int]],
    *,
    closed: bool,
) -> Tuple[Tuple[int, int, int], ...]:
    """Canonicalize edge orientation and a closed edge's possible seam."""

    if not points:
        return ()
    sequence = tuple(points)
    if closed:
        if len(sequence) > 1 and sequence[0] == sequence[-1]:
            sequence = sequence[:-1]
        if not sequence:
            return ()
        candidates = []
        for oriented in (sequence, tuple(reversed(sequence))):
            candidates.extend(
                oriented[index:] + oriented[:index]
                for index in range(len(oriented))
            )
        return min(candidates)
    return min(sequence, tuple(reversed(sequence)))


def _host_edge_closed_state(
    edge: Any,
    point_samples: List[List[float]],
) -> Tuple[bool, bool]:
    """Read topological closure before using sample proximity as a fallback."""

    try:
        is_closed = getattr(edge, "isClosed", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        is_closed = None
    if callable(is_closed):
        try:
            authoritative = is_closed()
        except (AttributeError, RuntimeError, TypeError, ValueError):
            authoritative = None
        if type(authoritative) is bool:
            return authoritative, True

    try:
        vertices = list(edge.Vertexes)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        vertices = []
    if len(vertices) == 1:
        return True, True
    if len(vertices) >= 2:
        first_vertex = vertices[0]
        last_vertex = vertices[-1]
        for method_name in ("isSame", "isEqual"):
            try:
                compare = getattr(first_vertex, method_name, None)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                compare = None
            if callable(compare):
                try:
                    topologically_equal = compare(last_vertex)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    continue
                if type(topologically_equal) is bool:
                    return topologically_equal, True
        return first_vertex is last_vertex, True

    return (
        bool(
            len(point_samples) > 2
            and all(
                abs(first - last) <= _SHAPE_FINGERPRINT_QUANTUM
                for first, last in zip(
                    point_samples[0], point_samples[-1], strict=True
                )
            )
        ),
        False,
    )


def _host_edge_fingerprint(
    edge: Any,
) -> Tuple[Tuple[Any, ...], Dict[str, Any], bool]:
    """Fingerprint an edge and retain independently comparable sample points."""

    try:
        curve = getattr(edge, "Curve", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        curve = None
    curve_type = type(curve).__name__ if curve is not None else ""
    try:
        length_token = _quantized_shape_number(edge.Length)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        length_token = None
    try:
        raw_length = _raw_shape_number(edge.Length)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        raw_length = None

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
    if not sampled_points and not callable(discretize):
        try:
            sampled_points = [
                vertex.Point
                for vertex in list(getattr(edge, "Vertexes", []) or [])
            ]
        except (AttributeError, RuntimeError, TypeError, ValueError):
            sampled_points = []

    point_tokens: List[Tuple[int, int, int]] = []
    point_samples: List[List[float]] = []
    samples_valid = bool(sampled_points)
    for point in sampled_points:
        token = _host_point_token(point)
        sample = _host_point_sample(point)
        if token is None or sample is None:
            samples_valid = False
            continue
        point_tokens.append(token)
        point_samples.append(sample)
    samples_valid = bool(
        samples_valid
        and len(point_tokens) == len(sampled_points)
        and raw_length is not None
        and raw_length >= 0.0
        and (raw_length == 0.0 or len(point_tokens) >= 2)
    )
    closed, closure_authoritative = _host_edge_closed_state(edge, point_samples)
    canonical_path = _canonical_point_path(
        point_tokens,
        closed=closed,
    )
    return (
        (
            curve_type,
            length_token if length_token is not None else "",
            1 if closed else 0,
            canonical_path,
        ),
        {
            "curve_type": curve_type,
            "length": raw_length,
            "closed": closed,
            "closure_authoritative": closure_authoritative,
            "points": point_samples,
        },
        samples_valid,
    )


def _host_shape_geometry_fingerprint(shape: Any) -> Dict[str, Any]:
    """Build an order-independent, tolerance-aware geometry fingerprint."""

    try:
        vertices = list(getattr(shape, "Vertexes", []) or [])
    except (AttributeError, RuntimeError, TypeError, ValueError):
        vertices = []
    vertex_tokens = []
    vertex_samples: List[List[float]] = []
    invalid_vertex_count = 0
    for vertex in vertices:
        try:
            point = vertex.Point
        except (AttributeError, RuntimeError, TypeError, ValueError):
            invalid_vertex_count += 1
            continue
        token = _host_point_token(point)
        sample = _host_point_sample(point)
        if token is None or sample is None:
            invalid_vertex_count += 1
        else:
            vertex_tokens.append(token)
            vertex_samples.append(sample)
    vertex_tokens.sort()
    vertex_samples.sort()

    try:
        edges = list(getattr(shape, "Edges", []) or [])
    except (AttributeError, RuntimeError, TypeError, ValueError):
        edges = []
    edge_results = [_host_edge_fingerprint(edge) for edge in edges]
    edge_tokens = [result[0] for result in edge_results]
    edge_samples = [result[1] for result in edge_results]
    edge_validity = [result[2] for result in edge_results]
    edge_tokens.sort(
        key=lambda token: json.dumps(
            token,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    edge_samples.sort(
        key=lambda sample: json.dumps(
            sample,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    sampled_edge_count = sum(1 for valid in edge_validity if valid)
    comparison_geometry = {
        "schema": "bcs.freecad_raw_shape_geometry/1.0",
        "tolerance": format(_SHAPE_FINGERPRINT_QUANTUM, ".12g"),
        "vertices": vertex_samples,
        "edges": edge_samples,
    }
    comparison_digest = hashlib.sha256(
        json.dumps(
            comparison_geometry,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "schema": "bcs.freecad_shape_fingerprint/1.1",
        "quantum": format(_SHAPE_FINGERPRINT_QUANTUM, ".12g"),
        "vertices": vertex_tokens,
        "edges": edge_tokens,
        "vertex_count": len(vertices),
        "invalid_vertex_count": invalid_vertex_count,
        "edge_count": len(edges),
        "sampled_edge_count": sampled_edge_count,
        "invalid_edge_count": len(edges) - sampled_edge_count,
        "canonical_geometry_present": bool(vertex_tokens or sampled_edge_count),
        "comparison_geometry": comparison_geometry,
        "public_geometry": {
            "schema": "bcs.freecad_shape_geometry_digest/1.0",
            "tolerance": format(_SHAPE_FINGERPRINT_QUANTUM, ".12g"),
            "sample_digest": comparison_digest,
            "vertex_count": len(vertex_samples),
            "edge_count": len(edge_samples),
            "sampled_point_count": sum(
                len(edge_sample.get("points", [])) for edge_sample in edge_samples
            ),
        },
    }


def _host_shape_content_snapshot(
    obj,
    type_id: str,
    *,
    shape_evidence_mode: str = "sampled",
) -> Dict[str, Any]:
    """Capture meaningful persisted topology and geometry, not just non-nullness."""

    if shape_evidence_mode not in {"sampled", "cheap"}:
        raise ValueError("unsupported shape evidence mode: %s" % shape_evidence_mode)

    if shape_evidence_mode == "cheap":
        try:
            source_ink_classification = str(
                getattr(obj, "PDFSourceInkClassification", "") or ""
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            source_ink_classification = ""
        zero_visible_ink = source_ink_classification == "zero_visible_ink"
        return {
            "shape_nonempty": not zero_visible_ink,
            "shape_structure_verified": False,
            "shape_digest": "",
            "shape_snapshot_method": "fcstd_archive_pending",
        }

    shape_nonempty = _has_nonempty_host_geometry(obj, type_id)
    topology_counts: Dict[str, int] = {}
    metrics: Dict[str, float] = {}
    bounds: Dict[str, float] = {}
    try:
        shape = getattr(obj, "Shape", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        shape = None

    if shape is not None:
        try:
            is_null = getattr(shape, "isNull", None)
            shape_is_null = bool(is_null()) if callable(is_null) else None
        except (AttributeError, RuntimeError, TypeError, ValueError):
            shape_is_null = None
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
                number = _raw_shape_number(getattr(shape, property_name))
            except (AttributeError, RuntimeError, TypeError, ValueError):
                number = None
            if number is not None:
                metrics[property_name.lower()] = number
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
                    number = _raw_shape_number(getattr(bound_box, property_name))
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    number = None
                if number is not None:
                    bounds[property_name.lower()] = number
    else:
        shape_is_null = None
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
        _host_shape_geometry_fingerprint(shape)
        if shape_evidence_mode == "sampled" and shape is not None
        else {}
    )
    digest_payload = {
        "method": (
            "sampled_geometry_tolerant"
            if shape_evidence_mode == "sampled"
            else "cheap_topology_metrics_bounds"
        ),
        "topology_counts": topology_counts,
        "metrics": metrics,
        "bounds": bounds,
    }
    if shape_evidence_mode == "sampled":
        digest_payload["geometry"] = geometry_fingerprint
    shape_digest = ""
    if shape_nonempty and meaningful_topology:
        shape_digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    snapshot: Dict[str, Any] = {
        "shape_nonempty": bool(shape_nonempty),
        "shape_is_null": shape_is_null,
        "shape_structure_verified": bool(
            shape_nonempty and meaningful_topology and shape_digest
        ),
        "shape_topology_counts": topology_counts,
        "shape_metrics": metrics,
        "shape_bounds": bounds,
        "shape_digest": shape_digest,
    }
    if shape_evidence_mode == "cheap":
        snapshot["shape_snapshot_method"] = "cheap_topology_metrics_bounds"
    else:
        snapshot.update(
            {
                "shape_fingerprint_schema": geometry_fingerprint.get("schema", ""),
                "shape_fingerprint_quantum": geometry_fingerprint.get("quantum", ""),
                "shape_fingerprint_vertex_count": geometry_fingerprint.get(
                    "vertex_count", 0
                ),
                "shape_fingerprint_edge_count": geometry_fingerprint.get(
                    "edge_count", 0
                ),
                "shape_fingerprint_sampled_edge_count": geometry_fingerprint.get(
                    "sampled_edge_count", 0
                ),
                "shape_fingerprint_geometry": geometry_fingerprint.get(
                    "public_geometry", {}
                ),
                "_shape_comparison_geometry": geometry_fingerprint.get(
                    "comparison_geometry", {}
                ),
                "shape_fingerprint_verified": bool(
                    geometry_fingerprint.get("canonical_geometry_present")
                    and geometry_fingerprint.get("invalid_vertex_count") == 0
                    and geometry_fingerprint.get("invalid_edge_count") == 0
                ),
            }
        )
    return snapshot


def _host_view_style_snapshot(obj) -> Dict[str, Any]:
    """Capture the real GUI view-provider style when the host exposes one."""

    try:
        view = getattr(obj, "ViewObject", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        view = None
    snapshot: Dict[str, Any] = {"view_present": view is not None}
    if view is None:
        return snapshot
    try:
        visibility = view.Visibility
    except (AttributeError, RuntimeError, TypeError, ValueError):
        visibility = None
    if type(visibility) is bool:
        snapshot["visibility"] = visibility
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


def _host_font_delivery_snapshot(obj: Any) -> Optional[Dict[str, Any]]:
    property_names = {
        "source_font_equivalence": "PDFSourceFontEquivalence",
        "font_substitution_applied": "PDFFontSubstitutionApplied",
        "font_identity_verified": "PDFFontIdentityVerified",
        "glyph_coverage_verified": "PDFFontGlyphCoverageVerified",
        "delivered_font_sha256": "PDFDeliveredFontSHA256",
        "font_candidate_source": "PDFFontCandidateSource",
        "delivered_font_path": "PDFDeliveredFontPath",
        "source_font_asset_path": "PDFSourceFontAssetPath",
        "source_font_asset_sha256": "PDFSourceFontAssetSHA256",
        "staged_font_path": "PDFStagedFontPath",
        "staged_font_sha256": "PDFStagedFontSHA256",
        "staged_asset_verified": "PDFStagedFontVerified",
        "staged_asset_read_only": "PDFStagedFontReadOnly",
        "font_internal_identity_sha256": "PDFFontInternalIdentitySHA256",
        "font_coverage_evidence_sha256": "PDFFontCoverageEvidenceSHA256",
        "source_font_identity_json": "PDFSourceFontIdentityJSON",
    }
    if not any(hasattr(obj, property_name) for property_name in property_names.values()):
        return None
    boolean_fields = {
        "source_font_equivalence",
        "font_substitution_applied",
        "font_identity_verified",
        "glyph_coverage_verified",
        "staged_asset_verified",
        "staged_asset_read_only",
    }
    snapshot: Dict[str, Any] = {}
    for content_name, property_name in property_names.items():
        try:
            value = getattr(obj, property_name)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            value = None
        snapshot[content_name] = (
            value if content_name in boolean_fields and type(value) is bool
            else str(value or "") if content_name not in boolean_fields
            else None
        )

    staged_path = snapshot.get("staged_font_path")
    declared_sha256 = snapshot.get("staged_font_sha256")
    try:
        _canonical_path, _payload, actual_sha256 = _read_stable_font_asset(
            staged_path
        )
        snapshot["staged_asset_digest_matches"] = bool(
            re.fullmatch(r"[0-9a-f]{64}", str(declared_sha256 or ""))
            and actual_sha256 == declared_sha256
            and actual_sha256 == snapshot.get("delivered_font_sha256")
            and _font_asset_is_read_only(staged_path)
        )
        snapshot["staged_asset_actual_sha256"] = actual_sha256
    except Exception as exc:
        snapshot["staged_asset_digest_matches"] = False
        snapshot["staged_asset_actual_sha256"] = ""
        snapshot["staged_asset_verification_error"] = "%s: %s" % (
            exc.__class__.__name__,
            exc,
        )
    return snapshot


def _host_object_content_snapshot(
    obj,
    type_id: str,
    representation: str,
    *,
    shape_evidence_mode: str = "sampled",
) -> Dict[str, Any]:
    """Capture persisted content that metadata-only delivery cannot imitate."""

    content: Dict[str, Any] = {}
    for property_name, content_name in (
        ("PDFSourceInkClassification", "source_ink_classification"),
        ("PDFSourceInkEvidenceSHA256", "source_ink_evidence_sha256"),
    ):
        try:
            value = str(getattr(obj, property_name, "") or "")
        except (AttributeError, RuntimeError, TypeError, ValueError):
            value = ""
        if value:
            content[content_name] = value
    try:
        text_visibility = obj.PDFTextVisibility
    except (AttributeError, RuntimeError, TypeError, ValueError):
        text_visibility = None
    if type(text_visibility) is bool:
        content["text_visibility"] = text_visibility
    if type_id.startswith(("Part::", "PartDesign::", "Sketcher::")):
        content.update(
            _host_shape_content_snapshot(
                obj,
                type_id,
                shape_evidence_mode=shape_evidence_mode,
            )
        )
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
        font_delivery = _host_font_delivery_snapshot(obj)
        if font_delivery is not None:
            content["font_delivery"] = font_delivery
        try:
            base = getattr(obj, "Base", None)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            base = None
        content["base_entity_id"] = (
            (
                _host_object_persistent_name(base)
                if shape_evidence_mode == "cheap"
                else _host_object_id(base)
            )
            if base is not None
            else ""
        )
    if representation in {"glyphs", "geometry"}:
        try:
            content["source_text"] = str(obj.PDFSourceText)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            content["source_text"] = ""
    if representation == "raster" or type_id.startswith("Image::"):
        try:
            image_file = str(getattr(obj, "ImageFile", "") or "")
        except (AttributeError, RuntimeError, TypeError, ValueError):
            image_file = ""
        image_snapshot = _host_image_content_snapshot(image_file)
        try:
            included_file = str(getattr(obj, "PDFRasterFile", "") or "")
        except (AttributeError, RuntimeError, TypeError, ValueError):
            included_file = ""
        included_snapshot = _host_image_content_snapshot(included_file)
        content.update(image_snapshot)
        content["image_file_property_type"] = _host_property_type_id(
            obj, "ImageFile"
        )
        content["included_file_property_type"] = _host_property_type_id(
            obj, "PDFRasterFile"
        )
        for property_name, content_name in (
            ("PDFSourceSHA256", "pdf_source_sha256"),
            ("PDFRasterSHA256", "declared_raster_sha256"),
        ):
            try:
                content[content_name] = str(getattr(obj, property_name, "") or "")
            except (AttributeError, RuntimeError, TypeError, ValueError):
                content[content_name] = ""
        if included_file:
            content.update(
                {
                    "included_image_file": included_snapshot.get("image_file", ""),
                    "included_image_sha256": included_snapshot.get(
                        "image_sha256", ""
                    ),
                    "included_image_bytes": int(
                        included_snapshot.get("image_bytes", 0) or 0
                    ),
                }
            )
        for property_name, content_name, minimum in (
            ("PDFImagePageNumber", "embedded_image_page_number", 1),
            ("PDFImageSourceXRef", "embedded_image_source_xref", 0),
            ("PDFImageOccurrenceIndex", "image_occurrence_index", 0),
        ):
            try:
                value = getattr(obj, property_name)
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            if type(value) is int and value >= minimum:
                content[content_name] = value
        for property_name, content_name in (
            ("PDFImageOccurrenceJSON", "image_occurrence_json"),
            (
                "PDFImageOccurrenceEvidenceSHA256",
                "image_occurrence_evidence_sha256",
            ),
        ):
            try:
                value = str(getattr(obj, property_name, "") or "")
            except (AttributeError, RuntimeError, TypeError, ValueError):
                value = ""
            if value:
                content[content_name] = value
        try:
            source_has_mask = getattr(obj, "PDFImageSourceHasMask")
        except (AttributeError, RuntimeError, TypeError, ValueError):
            source_has_mask = None
        if type(source_has_mask) is bool:
            content["image_source_has_mask"] = source_has_mask

        for property_name, content_name in (
            ("XSize", "raster_x_size"),
            ("YSize", "raster_y_size"),
        ):
            try:
                token = _finite_host_number(getattr(obj, property_name))
            except (AttributeError, RuntimeError, TypeError, ValueError):
                token = None
            if token is not None:
                content[content_name] = token
        anchor = _host_anchor_xyz(obj)
        if anchor is not None:
            anchor_tokens = [_finite_host_number(value) for value in anchor]
            if all(value is not None for value in anchor_tokens):
                content["raster_anchor_xyz"] = anchor_tokens

        occurrence_json = str(content.get("image_occurrence_json") or "")
        occurrence_digest = str(
            content.get("image_occurrence_evidence_sha256") or ""
        )
        if occurrence_json or occurrence_digest:
            content["image_occurrence_binding_verified"] = bool(
                occurrence_json
                and re.fullmatch(r"[0-9a-f]{64}", occurrence_digest)
                and hashlib.sha256(occurrence_json.encode("utf-8")).hexdigest()
                == occurrence_digest
            )
        if included_file or content.get("declared_raster_sha256"):
            image_sha256 = str(content.get("image_sha256") or "")
            included_sha256 = str(content.get("included_image_sha256") or "")
            declared_sha256 = str(content.get("declared_raster_sha256") or "")
            content["raster_asset_binding_verified"] = bool(
                re.fullmatch(r"[0-9a-f]{64}", declared_sha256)
                and image_sha256 == declared_sha256
                and included_sha256 == declared_sha256
                and int(content.get("image_bytes") or 0) > 0
                and int(content.get("included_image_bytes") or 0) > 0
                and content.get("included_file_property_type")
                == "App::PropertyFileIncluded"
            )
        content.update(_host_page_text_suppression_snapshot(obj))
    return content


def _host_page_text_suppression_snapshot(obj) -> Dict[str, Any]:
    """Read and independently verify persisted page text-suppression metadata."""

    declarations = (
        ("PDFTextSuppressionSchema", "text_suppression_schema", "App::PropertyString"),
        ("PDFTextSuppressionMethod", "text_suppression_method", "App::PropertyString"),
        ("PDFTextSuppressionEvidenceJSON", "text_suppression_evidence_json", "App::PropertyString"),
        ("PDFTextSuppressionEvidenceSHA256", "text_suppression_evidence_sha256", "App::PropertyString"),
        ("PDFTextSuppressionSourceItemIDsJSON", "text_suppression_source_item_ids_json", "App::PropertyString"),
        ("PDFTextSuppressionSourceItemIDsSHA256", "text_suppression_source_item_ids_sha256", "App::PropertyString"),
        ("PDFTextSuppressionSourceItemCount", "text_suppression_source_item_count", "App::PropertyInteger"),
        ("PDFTextSuppressionDeliveredItemIDsJSON", "text_suppression_delivered_item_ids_json", "App::PropertyString"),
        ("PDFTextSuppressionDeliveredItemIDsSHA256", "text_suppression_delivered_item_ids_sha256", "App::PropertyString"),
        ("PDFTextSuppressionDeliveryBound", "text_suppression_delivery_bound", "App::PropertyBool"),
        ("PDFTextSuppressionVerified", "text_suppression_verified", "App::PropertyBool"),
    )
    properties = set(getattr(obj, "PropertiesList", []) or [])
    content: Dict[str, Any] = {}
    variant_present = "PDFRasterContentVariant" in properties
    if variant_present:
        try:
            content["raster_content_variant"] = str(
                getattr(obj, "PDFRasterContentVariant", "") or ""
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
            content["raster_content_variant"] = ""
        content["raster_content_variant_property_verified"] = bool(
            _host_property_type_id(obj, "PDFRasterContentVariant")
            == "App::PropertyString"
            and content["raster_content_variant"]
            in {
                "full_page_original",
                "text_suppressed_page_background",
                "original_page_no_canonical_text",
            }
        )
    present = [name for name, _content_name, _kind in declarations if name in properties]
    if not present:
        return content

    property_types_verified = len(present) == len(declarations)
    for property_name, content_name, expected_type in declarations:
        if property_name not in properties:
            property_types_verified = False
            continue
        try:
            value = getattr(obj, property_name)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            value = None
        if expected_type == "App::PropertyString":
            content[content_name] = str(value or "")
        elif expected_type == "App::PropertyInteger":
            content[content_name] = value if type(value) is int else None
        else:
            content[content_name] = value if type(value) is bool else None
        property_types_verified = bool(
            property_types_verified
            and _host_property_type_id(obj, property_name) == expected_type
        )
    content["text_suppression_property_types_verified"] = property_types_verified

    evidence_json = str(content.get("text_suppression_evidence_json") or "")
    source_ids_json = str(content.get("text_suppression_source_item_ids_json") or "")
    delivered_ids_json = str(content.get("text_suppression_delivered_item_ids_json") or "")
    try:
        evidence = json.loads(evidence_json)
        source_ids = json.loads(source_ids_json)
        delivered_ids = json.loads(delivered_ids_json)
        canonical_evidence_json = _canonical_page_manifest_json(evidence)
        canonical_source_ids_json = _canonical_page_manifest_json(source_ids)
        canonical_delivered_ids_json = _canonical_page_manifest_json(delivered_ids)
    except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
        evidence = None
        source_ids = None
        delivered_ids = None
        canonical_evidence_json = ""
        canonical_source_ids_json = ""
        canonical_delivered_ids_json = ""

    evidence_digest = str(content.get("text_suppression_evidence_sha256") or "")
    source_ids_digest = str(content.get("text_suppression_source_item_ids_sha256") or "")
    delivered_ids_digest = str(content.get("text_suppression_delivered_item_ids_sha256") or "")
    variant = str(content.get("raster_content_variant") or "")
    content["page_text_suppression_binding_verified"] = bool(
        content.get("raster_content_variant_property_verified") is True
        and property_types_verified
        and isinstance(evidence, dict)
        and isinstance(source_ids, list)
        and isinstance(delivered_ids, list)
        and all(isinstance(value, str) and value for value in source_ids)
        and all(isinstance(value, str) and value for value in delivered_ids)
        and len(set(source_ids)) == len(source_ids)
        and delivered_ids == source_ids
        and evidence_json == canonical_evidence_json
        and source_ids_json == canonical_source_ids_json
        and delivered_ids_json == canonical_delivered_ids_json
        and variant in {"text_suppressed_page_background", "original_page_no_canonical_text"}
        and evidence.get("schema") == content.get("text_suppression_schema")
        and evidence.get("text_suppression_method") == content.get("text_suppression_method")
        and evidence.get("raster_content_variant") == variant
        and evidence.get("verified") is True
        and evidence.get("source_text_item_ids") == source_ids
        and evidence.get("source_text_item_count") == content.get("text_suppression_source_item_count") == len(source_ids)
        and evidence.get("delivered_source_text_item_ids") == delivered_ids
        and evidence.get("delivery_source_item_ids_bound") is True
        and content.get("text_suppression_delivery_bound") is True
        and content.get("text_suppression_verified") is True
        and re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is not None
        and evidence.get("evidence_sha256") == evidence_digest
        and _page_text_suppression_evidence_digest(evidence) == evidence_digest
        and re.fullmatch(r"[0-9a-f]{64}", source_ids_digest) is not None
        and evidence.get("source_text_item_ids_sha256") == source_ids_digest
        and _page_manifest_digest(source_ids) == source_ids_digest
        and re.fullmatch(r"[0-9a-f]{64}", delivered_ids_digest) is not None
        and evidence.get("delivered_source_text_item_ids_sha256") == delivered_ids_digest
        and _page_manifest_digest(delivered_ids) == delivered_ids_digest
    )
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


def _host_object_category_for_inventory(
    obj,
    type_id: str,
    representation: str,
    shape_evidence_mode: str,
) -> str:
    if shape_evidence_mode != "cheap":
        return _host_object_category(obj, type_id, representation)
    if _is_host_container(obj, type_id):
        return "containers"
    if type_id.startswith("Image::"):
        return "images"
    if representation in _STRUCTURAL_TEXT_REPRESENTATIONS:
        return "text_representation_objects"
    if type_id.startswith(("Part::", "PartDesign::", "Sketcher::")):
        # This is an obligation, not an assumption of success: the streamed
        # FCStd manifest must later bind it to a nonempty exact Shape member.
        return "vector_primitives"
    return "unclassified"


def _build_host_object_inventory(
    objects: List[Any],
    *,
    shape_evidence_mode: str = "sampled",
    opts: Optional[ImportOptions] = None,
) -> Dict[str, Any]:
    """Inventory actual FreeCAD objects without treating containers as geometry."""

    if shape_evidence_mode not in {"sampled", "cheap"}:
        raise ValueError("unsupported shape evidence mode: %s" % shape_evidence_mode)

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
        if opts is not None:
            _invoke_import_cancellation_checkpoint(
                opts, "persistence inventory object"
            )
        entity_id = (
            _host_object_persistent_name(host_obj)
            if shape_evidence_mode == "cheap"
            else _host_object_id(host_obj)
        )
        type_id = _host_object_type_id(host_obj)
        representation = _host_object_representation(host_obj)
        source_item_id = _host_object_source_item_id(host_obj)
        parent_source_item_id = _host_object_parent_source_item_id(host_obj)
        category = _host_object_category_for_inventory(
            host_obj,
            type_id,
            representation,
            shape_evidence_mode,
        )
        records.append(
            {
                "entity_id": entity_id,
                "type_id": type_id,
                "representation": representation,
                "source_item_id": source_item_id,
                "parent_source_item_id": parent_source_item_id,
                "category": category,
                "content": _host_object_content_snapshot(
                    host_obj,
                    type_id,
                    representation,
                    shape_evidence_mode=shape_evidence_mode,
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
        "shape_evidence_mode": shape_evidence_mode,
        "verified": unique_nonempty_ids,
        "entity_ids": entity_ids,
        "type_counts": type_counts,
        "counts": counts,
        "categories": categories,
        "objects": records,
    }


_FCSTD_ARCHIVE_EVIDENCE_SCHEMA = "bcs.freecad_fcstd_shape_archive/1.1"
_FCSTD_ARCHIVE_EVIDENCE_METHOD = "fcstd_brep_archive_sha256"
_FCSTD_MAX_ARCHIVE_ENTRIES = 1_000_000
_FCSTD_MAX_DOCUMENT_XML_BYTES = 256 * 1024 * 1024
_FCSTD_MAX_SHAPE_ENTRY_BYTES = 16 * 1024 * 1024 * 1024
_FCSTD_MAX_EXPECTED_SHAPE_BYTES = 128 * 1024 * 1024 * 1024
_FCSTD_RASTER_ARCHIVE_EVIDENCE_SCHEMA = "bcs.freecad_fcstd_raster_archive/1.0"
_FCSTD_RASTER_ARCHIVE_EVIDENCE_METHOD = "fcstd_property_file_included_sha256"
_FCSTD_MAX_RASTER_ENTRY_BYTES = 16 * 1024 * 1024 * 1024
_FCSTD_MAX_EXPECTED_RASTER_BYTES = 128 * 1024 * 1024 * 1024
_FCSTD_HASH_CHUNK_BYTES = 1024 * 1024
_FCSTD_SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}


def _raster_persistence_module():
    """Load the standalone FCStd raster verifier in package and legacy modes."""

    try:
        import PDFRasterPersistence as persistence
    except ImportError:
        from . import PDFRasterPersistence as persistence
    return persistence


def _inventory_required_included_rasters(
    inventory: Any,
) -> Dict[str, Dict[str, Any]]:
    return _raster_persistence_module().required_included_rasters(inventory)


def _read_fcstd_raster_archive_evidence(
    fcstd_path: Any,
    inventory: Any,
    *,
    opts: Optional[ImportOptions] = None,
    full_document_object_count: Optional[int] = None,
) -> Dict[str, Any]:
    cancel_check = None
    if opts is not None:
        cancel_check = lambda: _invoke_import_cancellation_checkpoint(
            opts,
            "persistence raster archive evidence",
        )
    return _raster_persistence_module().read_archive_evidence(
        fcstd_path,
        inventory,
        cancel_check=cancel_check,
        full_document_object_count=full_document_object_count,
    )


def _bind_fcstd_raster_archive_evidence(
    inventory: Dict[str, Any],
    evidence: Dict[str, Any],
) -> bool:
    return bool(
        _raster_persistence_module().bind_archive_evidence(inventory, evidence)
    )


class _FCStdArchiveEvidenceError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = str(reason)


def _fcstd_archive_failure(reason: str) -> Dict[str, Any]:
    return {
        "schema": _FCSTD_ARCHIVE_EVIDENCE_SCHEMA,
        "method": _FCSTD_ARCHIVE_EVIDENCE_METHOD,
        "verified": False,
        "reason": str(reason),
    }


def _fcstd_archive_evidence_digest(evidence: Any) -> str:
    """Digest the complete public archive manifest, excluding only itself."""

    if not isinstance(evidence, dict):
        return ""
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
        return ""
    return hashlib.sha256(encoded).hexdigest()


def _host_inventory_digest(inventory: Any) -> str:
    """Digest one canonical inventory without embedding a second copy."""

    if not isinstance(inventory, dict):
        return ""
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
        return ""
    return hashlib.sha256(encoded).hexdigest()


def _canonical_fcstd_entry_name(
    value: Any,
    *,
    allow_directory: bool,
) -> str:
    """Validate a ZIP member path without ever extracting it."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise _FCStdArchiveEvidenceError("unsafe_archive_entry_name")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise _FCStdArchiveEvidenceError("unsafe_archive_entry_name")
    if "\\" in value or ":" in value or value.startswith("/"):
        raise _FCStdArchiveEvidenceError("unsafe_archive_entry_name")
    is_directory = value.endswith("/")
    if is_directory and not allow_directory:
        raise _FCStdArchiveEvidenceError("unsafe_archive_entry_name")
    candidate = value[:-1] if is_directory else value
    if not candidate or "//" in candidate:
        raise _FCStdArchiveEvidenceError("unsafe_archive_entry_name")
    parts = candidate.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise _FCStdArchiveEvidenceError("unsafe_archive_entry_name")
    normalized = PurePosixPath(candidate).as_posix()
    if normalized != candidate or PurePosixPath(candidate).is_absolute():
        raise _FCStdArchiveEvidenceError("unsafe_archive_entry_name")
    return value


def _zip_info_is_symlink(info: zipfile.ZipInfo) -> bool:
    return ((int(info.external_attr) >> 16) & 0o170000) == 0o120000


def _read_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_bytes: int,
    capture: bool,
    opts: Optional[ImportOptions] = None,
    stage: str = "persistence shape archive member",
) -> Tuple[str, int, bytes]:
    """Stream a member through ZipFile so EOF performs its CRC check."""

    declared_size = int(info.file_size)
    if declared_size < 0 or declared_size > maximum_bytes:
        raise _FCStdArchiveEvidenceError("archive_entry_size_invalid")
    digest = hashlib.sha256()
    observed_size = 0
    chunks: List[bytes] = []
    with archive.open(info, "r") as stream:
        while True:
            if opts is not None:
                _invoke_import_cancellation_checkpoint(opts, stage)
            chunk = stream.read(_FCSTD_HASH_CHUNK_BYTES)
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > maximum_bytes:
                raise _FCStdArchiveEvidenceError("archive_entry_size_invalid")
            digest.update(chunk)
            if capture:
                chunks.append(chunk)
    if observed_size != declared_size:
        raise _FCStdArchiveEvidenceError("archive_entry_size_mismatch")
    return digest.hexdigest(), observed_size, b"".join(chunks) if capture else b""


def _hash_file_sha256(
    path: Path,
    *,
    opts: Optional[ImportOptions] = None,
    stage: str = "persistence archive hash",
) -> Tuple[str, int]:
    digest = hashlib.sha256()
    observed_size = 0
    with path.open("rb") as stream:
        while True:
            if opts is not None:
                _invoke_import_cancellation_checkpoint(opts, stage)
            chunk = stream.read(_FCSTD_HASH_CHUNK_BYTES)
            if not chunk:
                break
            observed_size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), observed_size


def _xml_local_name(tag: Any) -> str:
    token = str(tag or "")
    return token.rsplit("}", 1)[-1]


def _inventory_required_shape_entity_ids(inventory: Any) -> List[str]:
    """Derive archive obligations from host facts, never from supplied proof fields."""

    if not isinstance(inventory, dict):
        return []
    required: List[str] = []
    for record in list(inventory.get("objects") or []):
        if not isinstance(record, dict):
            continue
        type_id = str(record.get("type_id") or "")
        content = record.get("content")
        if (
            type_id.startswith(("Part::", "PartDesign::", "Sketcher::"))
            and isinstance(content, dict)
            and content.get("shape_nonempty") is True
        ):
            required.append(str(record.get("entity_id") or ""))
    return required


def _inventory_zero_ink_shape_entity_ids(inventory: Any) -> List[str]:
    """Return empty Part shapes that are intentional, source-bound zero ink."""

    if not isinstance(inventory, dict):
        return []
    required: List[str] = []
    for record in list(inventory.get("objects") or []):
        if not isinstance(record, dict):
            continue
        type_id = str(record.get("type_id") or "")
        representation = str(record.get("representation") or "")
        content = record.get("content")
        if (
            type_id.startswith(("Part::", "PartDesign::", "Sketcher::"))
            and representation in {"3d_text", "glyphs", "geometry"}
            and isinstance(content, dict)
            and content.get("shape_nonempty") is False
            and content.get("source_ink_classification") == "zero_visible_ink"
        ):
            required.append(str(record.get("entity_id") or ""))
    return required


def _read_fcstd_shape_archive_evidence(
    fcstd_path: Any,
    expected_shape_entity_ids: List[str],
    *,
    expected_zero_ink_shape_entity_ids: Optional[List[str]] = None,
    opts: Optional[ImportOptions] = None,
) -> Dict[str, Any]:
    """Build exact persisted-shape evidence directly from a saved FCStd archive."""

    try:
        path = Path(fcstd_path)
        expected_ids = [str(value or "") for value in expected_shape_entity_ids]
        zero_ink_ids = [
            str(value or "")
            for value in list(expected_zero_ink_shape_entity_ids or [])
        ]
        if (
            any(not entity_id for entity_id in expected_ids)
            or len(expected_ids) != len(set(expected_ids))
            or any(not entity_id for entity_id in zero_ink_ids)
            or len(zero_ink_ids) != len(set(zero_ink_ids))
            or set(expected_ids).intersection(zero_ink_ids)
        ):
            raise _FCStdArchiveEvidenceError("expected_shape_ids_invalid")
        all_expected_ids = expected_ids + zero_ink_ids
        zero_ink_id_set = set(zero_ink_ids)
        if not path.is_file() or path.stat().st_size <= 0:
            raise _FCStdArchiveEvidenceError("fcstd_file_missing_or_empty")
        initial_fcstd_digest, initial_fcstd_size = _hash_file_sha256(
            path,
            opts=opts,
            stage="persistence shape archive hash",
        )
        if initial_fcstd_size <= 0:
            raise _FCStdArchiveEvidenceError("fcstd_file_missing_or_empty")

        with zipfile.ZipFile(path, "r") as archive:
            infos = list(archive.infolist())
            if not infos or len(infos) > _FCSTD_MAX_ARCHIVE_ENTRIES:
                raise _FCStdArchiveEvidenceError("archive_entry_count_invalid")
            info_by_name: Dict[str, zipfile.ZipInfo] = {}
            canonical_keys = set()
            for info in infos:
                if opts is not None:
                    _invoke_import_cancellation_checkpoint(
                        opts, "persistence shape archive entries"
                    )
                canonical_name = _canonical_fcstd_entry_name(
                    info.filename,
                    allow_directory=True,
                )
                canonical_key = canonical_name.casefold()
                if canonical_key in canonical_keys:
                    raise _FCStdArchiveEvidenceError("duplicate_archive_entry")
                canonical_keys.add(canonical_key)
                info_by_name[canonical_name] = info
                if int(info.flag_bits) & 0x1:
                    raise _FCStdArchiveEvidenceError("encrypted_archive_entry")
                if _zip_info_is_symlink(info):
                    raise _FCStdArchiveEvidenceError("symlink_archive_entry")
                if not info.is_dir() and info.compress_type not in (
                    _FCSTD_SUPPORTED_COMPRESSION
                ):
                    raise _FCStdArchiveEvidenceError(
                        "unsupported_archive_compression"
                    )

            document_info = info_by_name.get("Document.xml")
            if document_info is None:
                raise _FCStdArchiveEvidenceError("document_xml_missing")
            if document_info.is_dir():
                raise _FCStdArchiveEvidenceError("document_xml_missing")
            try:
                document_digest, document_size, document_xml = _read_zip_member(
                    archive,
                    document_info,
                    maximum_bytes=_FCSTD_MAX_DOCUMENT_XML_BYTES,
                    capture=True,
                    opts=opts,
                    stage="persistence shape document XML",
                )
            except (zipfile.BadZipFile, zlib.error) as exc:
                raise _FCStdArchiveEvidenceError(
                    "document_xml_crc_or_read_error"
                ) from exc
            if document_size <= 0:
                raise _FCStdArchiveEvidenceError("document_xml_empty")
            if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", document_xml, re.IGNORECASE):
                raise _FCStdArchiveEvidenceError(
                    "document_xml_unsafe_declaration"
                )
            try:
                root = ElementTree.fromstring(document_xml)
            except ElementTree.ParseError as exc:
                raise _FCStdArchiveEvidenceError("document_xml_malformed") from exc
            if _xml_local_name(root.tag) != "Document":
                raise _FCStdArchiveEvidenceError("document_xml_root_invalid")
            object_data_nodes = [
                child
                for child in list(root)
                if _xml_local_name(child.tag) == "ObjectData"
            ]
            if len(object_data_nodes) != 1:
                raise _FCStdArchiveEvidenceError(
                    "document_xml_object_data_invalid"
                )

            object_names = set()
            shape_mapping: Dict[str, str] = {}
            mapped_entries = set()
            object_nodes = [
                child
                for child in list(object_data_nodes[0])
                if _xml_local_name(child.tag) == "Object"
            ]
            for object_node in object_nodes:
                if opts is not None:
                    _invoke_import_cancellation_checkpoint(
                        opts, "persistence shape document objects"
                    )
                object_name = str(object_node.attrib.get("name") or "")
                if not object_name:
                    raise _FCStdArchiveEvidenceError(
                        "document_object_name_missing"
                    )
                if object_name in object_names:
                    raise _FCStdArchiveEvidenceError(
                        "duplicate_document_object_name"
                    )
                object_names.add(object_name)
                properties_nodes = [
                    child
                    for child in list(object_node)
                    if _xml_local_name(child.tag) == "Properties"
                ]
                shape_properties = []
                for properties_node in properties_nodes:
                    shape_properties.extend(
                        child
                        for child in list(properties_node)
                        if _xml_local_name(child.tag) == "Property"
                        and child.attrib.get("name") == "Shape"
                    )
                if len(shape_properties) > 1:
                    raise _FCStdArchiveEvidenceError(
                        "duplicate_shape_property"
                    )
                if not shape_properties:
                    continue
                part_nodes = [
                    child
                    for child in list(shape_properties[0])
                    if _xml_local_name(child.tag) == "Part"
                ]
                if len(part_nodes) != 1:
                    raise _FCStdArchiveEvidenceError("shape_mapping_invalid")
                entry_name = _canonical_fcstd_entry_name(
                    part_nodes[0].attrib.get("file"),
                    allow_directory=False,
                )
                if entry_name in mapped_entries:
                    raise _FCStdArchiveEvidenceError(
                        "duplicate_shape_entry_mapping"
                    )
                mapped_entries.add(entry_name)
                shape_mapping[object_name] = entry_name

            missing_mappings = [
                entity_id
                for entity_id in all_expected_ids
                if entity_id not in shape_mapping
            ]
            if missing_mappings:
                raise _FCStdArchiveEvidenceError("expected_shape_unmapped")
            for entry_name in shape_mapping.values():
                info = info_by_name.get(entry_name)
                if info is None:
                    raise _FCStdArchiveEvidenceError("shape_entry_missing")
                if info.is_dir():
                    raise _FCStdArchiveEvidenceError("shape_entry_empty")
                if int(info.file_size) > _FCSTD_MAX_SHAPE_ENTRY_BYTES:
                    raise _FCStdArchiveEvidenceError("shape_entry_size_invalid")

            shape_entries: List[Dict[str, Any]] = []
            total_expected_shape_bytes = 0
            for entity_id in sorted(all_expected_ids):
                if opts is not None:
                    _invoke_import_cancellation_checkpoint(
                        opts, "persistence shape archive obligations"
                    )
                entry_name = shape_mapping[entity_id]
                info = info_by_name[entry_name]
                zero_visible_ink = entity_id in zero_ink_id_set
                if zero_visible_ink and int(info.file_size) != 0:
                    raise _FCStdArchiveEvidenceError(
                        "zero_ink_shape_entry_nonempty"
                    )
                if not zero_visible_ink and int(info.file_size) <= 0:
                    raise _FCStdArchiveEvidenceError("shape_entry_empty")
                try:
                    entry_digest, entry_size, _unused = _read_zip_member(
                        archive,
                        info,
                        maximum_bytes=_FCSTD_MAX_SHAPE_ENTRY_BYTES,
                        capture=False,
                        opts=opts,
                        stage="persistence shape archive payload",
                    )
                except (zipfile.BadZipFile, zlib.error) as exc:
                    raise _FCStdArchiveEvidenceError(
                        "shape_entry_crc_or_read_error"
                    ) from exc
                total_expected_shape_bytes += entry_size
                if total_expected_shape_bytes > _FCSTD_MAX_EXPECTED_SHAPE_BYTES:
                    raise _FCStdArchiveEvidenceError(
                        "expected_shape_bytes_limit_exceeded"
                    )
                shape_entries.append(
                    {
                        "entity_id": entity_id,
                        "entry_name": entry_name,
                        "sha256": entry_digest,
                        "bytes": entry_size,
                        "compression_method": int(info.compress_type),
                        "crc32": int(info.CRC),
                        "zero_visible_ink": zero_visible_ink,
                    }
                )

        fcstd_digest, fcstd_size = _hash_file_sha256(
            path,
            opts=opts,
            stage="persistence shape archive rehash",
        )
        if fcstd_size <= 0:
            raise _FCStdArchiveEvidenceError("fcstd_file_missing_or_empty")
        if (
            fcstd_digest != initial_fcstd_digest
            or fcstd_size != initial_fcstd_size
        ):
            raise _FCStdArchiveEvidenceError(
                "fcstd_archive_changed_during_evidence"
            )
        evidence: Dict[str, Any] = {
            "schema": _FCSTD_ARCHIVE_EVIDENCE_SCHEMA,
            "method": _FCSTD_ARCHIVE_EVIDENCE_METHOD,
            "verified": True,
            "fcstd_sha256": fcstd_digest,
            "fcstd_bytes": fcstd_size,
            "document_xml_sha256": document_digest,
            "document_xml_bytes": document_size,
            "archive_entry_count": len(infos),
            "document_object_count": len(object_names),
            "shape_mapping_count": len(shape_mapping),
            "expected_shape_count": len(all_expected_ids),
            "expected_nonempty_shape_count": len(expected_ids),
            "expected_zero_ink_shape_count": len(zero_ink_ids),
            "shape_entries": shape_entries,
        }
        evidence["evidence_digest"] = _fcstd_archive_evidence_digest(evidence)
        return evidence
    except ImportCancelled:
        raise
    except _FCStdArchiveEvidenceError as exc:
        return _fcstd_archive_failure(exc.reason)
    except zipfile.BadZipFile:
        return _fcstd_archive_failure("fcstd_archive_invalid")
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        NotImplementedError,
        zlib.error,
    ):
        return _fcstd_archive_failure("fcstd_archive_read_error")


def _bind_fcstd_shape_archive_evidence(
    inventory: Dict[str, Any],
    evidence: Dict[str, Any],
) -> bool:
    """Bind one persisted FCStd manifest into every required shape record."""

    if (
        not isinstance(inventory, dict)
        or not isinstance(evidence, dict)
        or evidence.get("verified") is not True
        or evidence.get("schema") != _FCSTD_ARCHIVE_EVIDENCE_SCHEMA
        or evidence.get("method") != _FCSTD_ARCHIVE_EVIDENCE_METHOD
        or evidence.get("evidence_digest")
        != _fcstd_archive_evidence_digest(evidence)
    ):
        return False
    required_nonempty_ids = _inventory_required_shape_entity_ids(inventory)
    required_zero_ink_ids = _inventory_zero_ink_shape_entity_ids(inventory)
    required_ids = required_nonempty_ids + required_zero_ink_ids
    if (
        any(not entity_id for entity_id in required_ids)
        or len(required_ids) != len(set(required_ids))
    ):
        return False
    entries = evidence.get("shape_entries")
    if not isinstance(entries, list):
        return False
    entry_by_id = {
        str(entry.get("entity_id") or ""): entry
        for entry in entries
        if isinstance(entry, dict)
    }
    if (
        len(entry_by_id) != len(entries)
        or set(entry_by_id) != set(required_ids)
        or evidence.get("expected_shape_count") != len(required_ids)
        or evidence.get("expected_nonempty_shape_count")
        != len(required_nonempty_ids)
        or evidence.get("expected_zero_ink_shape_count")
        != len(required_zero_ink_ids)
        or any(
            entry_by_id[entity_id].get("zero_visible_ink") is not False
            for entity_id in required_nonempty_ids
        )
        or any(
            entry_by_id[entity_id].get("zero_visible_ink") is not True
            for entity_id in required_zero_ink_ids
        )
    ):
        return False
    record_by_id = {
        str(record.get("entity_id") or ""): record
        for record in list(inventory.get("objects") or [])
        if isinstance(record, dict)
    }
    if any(entity_id not in record_by_id for entity_id in required_ids):
        return False
    inventory["shape_archive_evidence"] = dict(evidence)
    for entity_id in required_ids:
        content = record_by_id[entity_id].get("content")
        entry = entry_by_id[entity_id]
        if not isinstance(content, dict):
            return False
        zero_visible_ink = entry["zero_visible_ink"] is True
        content.update(
            {
                "shape_persistence_method": _FCSTD_ARCHIVE_EVIDENCE_METHOD,
                "shape_archive_schema": _FCSTD_ARCHIVE_EVIDENCE_SCHEMA,
                "shape_snapshot_method": _FCSTD_ARCHIVE_EVIDENCE_METHOD,
                "shape_nonempty": not zero_visible_ink,
                "shape_structure_verified": not zero_visible_ink,
                "shape_digest": "" if zero_visible_ink else entry["sha256"],
            }
        )
    inventory["inventory_digest"] = _host_inventory_digest(inventory)
    return bool(inventory["inventory_digest"])


def _crosscheck_host_object_inventory(
    expected_inventory: Dict[str, Any],
    actual_objects: List[Any],
    *,
    shape_evidence_mode: str = "sampled",
    archive_evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Cross-check saved/reopened identities, types, representations, and counts."""

    actual_object_list = list(actual_objects or [])
    actual_inventory = _build_host_object_inventory(
        actual_object_list,
        shape_evidence_mode=shape_evidence_mode,
    )
    archive_bound = True
    if shape_evidence_mode == "cheap":
        archive_bound = bool(
            isinstance(archive_evidence, dict)
            and _bind_fcstd_shape_archive_evidence(
                expected_inventory,
                archive_evidence,
            )
            and _bind_fcstd_shape_archive_evidence(
                actual_inventory,
                archive_evidence,
            )
        )
    elif archive_evidence is not None:
        archive_bound = False
    expected_records = [
        dict(record)
        for record in (expected_inventory.get("objects") or [])
        if isinstance(record, dict)
    ]
    expected_ids = [str(record.get("entity_id") or "") for record in expected_records]
    actual_records = [
        dict(record)
        for record in (actual_inventory.get("objects") or [])
        if isinstance(record, dict)
    ]
    actual_by_id: Dict[str, Dict[str, Any]] = {}
    duplicate_actual_ids: List[str] = []
    for record in actual_records:
        entity_id = str(record.get("entity_id") or "")
        if entity_id in actual_by_id:
            duplicate_actual_ids.append(entity_id)
        actual_by_id[entity_id] = record

    expected_id_set = set(expected_ids)
    unexpected = [
        entity_id for entity_id in actual_by_id if entity_id not in expected_id_set
    ]

    missing: List[str] = []
    mismatches: List[Dict[str, Any]] = []
    geometry_comparisons: List[Dict[str, Any]] = []
    for expected in expected_records:
        entity_id = str(expected.get("entity_id") or "")
        actual = actual_by_id.get(entity_id)
        if actual is None:
            missing.append(entity_id)
            continue
        actual_type = str(actual.get("type_id") or "")
        actual_representation = str(actual.get("representation") or "")
        expected_type = str(expected.get("type_id") or "")
        expected_representation = str(expected.get("representation") or "")
        actual_source_id = str(actual.get("source_item_id") or "")
        expected_source_id = str(expected.get("source_item_id") or "")
        actual_parent_source_id = str(actual.get("parent_source_item_id") or "")
        expected_parent_source_id = str(expected.get("parent_source_item_id") or "")
        actual_content = actual.get("content")
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
        elif isinstance(expected_content, dict):
            from pdfcadcore.import_report import _freecad_host_content_comparison

            content_comparison = _freecad_host_content_comparison(
                expected_content, actual_content, entity_id
            )
            if content_comparison.get("verified") is True:
                certificate = content_comparison.get("certificate")
                if isinstance(certificate, dict):
                    geometry_comparisons.append(certificate)
                continue
            mismatches.append(
                {
                    "entity_id": entity_id,
                    "expected_content": expected_content,
                    "actual_content": actual_content,
                }
            )

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
        and archive_bound
        and expected_inventory.get("shape_evidence_mode", "sampled")
        == shape_evidence_mode
    )
    result = {
        "required": True,
        "method": "document_object_identity_type_crosscheck",
        "verified": verified,
        "expected_entity_ids": expected_ids,
        "missing_entity_ids": missing,
        "duplicate_actual_entity_ids": list(dict.fromkeys(duplicate_actual_ids)),
        "unexpected_entity_ids": unexpected,
        "mismatched_entities": mismatches,
        "geometry_comparisons": geometry_comparisons,
        "expected_counts": expected_counts,
        "actual_counts": actual_counts,
        "counts_match": counts_match,
        "expected_objects": expected_records,
        "actual_objects": list(actual_inventory.get("objects") or []),
    }
    if shape_evidence_mode == "cheap":
        result["shape_archive_evidence"] = (
            dict(archive_evidence) if isinstance(archive_evidence, dict) else {}
        )
    return result


def _compact_crosscheck_host_object_inventory(
    expected_inventory: Dict[str, Any],
    actual_objects: List[Any],
    archive_evidence: Dict[str, Any],
    raster_archive_evidence: Optional[Dict[str, Any]] = None,
    *,
    opts: Optional[ImportOptions] = None,
) -> Dict[str, Any]:
    """Compare reopened metadata without serializing a second inventory."""

    actual_inventory = _build_host_object_inventory(
        list(actual_objects or []),
        shape_evidence_mode="cheap",
        opts=opts,
    )
    shape_archive_bound = _bind_fcstd_shape_archive_evidence(
        actual_inventory,
        archive_evidence,
    )
    try:
        raster_required = bool(
            _inventory_required_included_rasters(expected_inventory)
            or _inventory_required_included_rasters(actual_inventory)
        )
    except (RuntimeError, TypeError, ValueError):
        raster_required = True
    raster_archive_bound = bool(
        isinstance(raster_archive_evidence, dict)
        and _bind_fcstd_raster_archive_evidence(
            actual_inventory,
            raster_archive_evidence,
        )
    )
    if raster_archive_evidence is None and not raster_required:
        raster_archive_bound = True
    expected_records = list(expected_inventory.get("objects") or [])
    actual_records = list(actual_inventory.get("objects") or [])
    expected_by_id = {
        record.get("entity_id"): record
        for record in expected_records
        if isinstance(record, dict) and isinstance(record.get("entity_id"), str)
    }
    actual_by_id: Dict[str, Dict[str, Any]] = {}
    duplicate_actual_ids: List[str] = []
    for record in actual_records:
        if not isinstance(record, dict):
            continue
        entity_id = str(record.get("entity_id") or "")
        if entity_id in actual_by_id:
            duplicate_actual_ids.append(entity_id)
        actual_by_id[entity_id] = record
    missing = [entity_id for entity_id in expected_by_id if entity_id not in actual_by_id]
    unexpected = [entity_id for entity_id in actual_by_id if entity_id not in expected_by_id]
    mismatches = [
        {"entity_id": entity_id, "reason": "persisted_metadata_mismatch"}
        for entity_id in expected_by_id.keys() & actual_by_id.keys()
        if expected_by_id[entity_id] != actual_by_id[entity_id]
    ]
    expected_counts = dict(expected_inventory.get("counts") or {})
    actual_counts = dict(actual_inventory.get("counts") or {})
    inventory_digest = str(expected_inventory.get("inventory_digest") or "")
    reopened_digest = str(actual_inventory.get("inventory_digest") or "")
    verified = bool(
        shape_archive_bound
        and raster_archive_bound
        and inventory_digest
        and inventory_digest == _host_inventory_digest(expected_inventory)
        and reopened_digest == inventory_digest
        and len(expected_by_id) == len(expected_records)
        and len(actual_by_id) == len(actual_records)
        and not duplicate_actual_ids
        and not missing
        and not unexpected
        and not mismatches
        and expected_counts == actual_counts
    )
    return {
        "schema": "bcs.freecad_save_reopen_inventory/1.0",
        "required": True,
        "verified": verified,
        "inventory_digest": inventory_digest,
        "reopened_inventory_digest": reopened_digest,
        "shape_archive_evidence_digest": str(
            archive_evidence.get("evidence_digest") or ""
        ),
        "raster_archive_evidence_digest": str(
            (raster_archive_evidence or {}).get("evidence_digest") or ""
        ),
        "missing_entity_ids": missing,
        "duplicate_actual_entity_ids": duplicate_actual_ids,
        "unexpected_entity_ids": unexpected,
        "mismatched_entities": mismatches,
        "expected_counts": expected_counts,
        "actual_counts": actual_counts,
        "counts_match": expected_counts == actual_counts,
    }


def _save_reopen_host_object_inventory(
    fc_doc,
    inventory: Dict[str, Any],
    baseline_entity_names: Optional[set] = None,
    *,
    opts: Optional[ImportOptions] = None,
) -> Dict[str, Any]:
    """Verify cheap live facts against exact persisted FCStd archive entries."""

    if opts is not None:
        _invoke_import_cancellation_checkpoint(opts, "persistence save setup")
    phase_timings_ms = {
        "save_ms": 0.0,
        "archive_hash_ms": 0.0,
        "open_ms": 0.0,
        "cheap_inventory_ms": 0.0,
        "close_ms": 0.0,
    }
    failure = {
        "schema": "bcs.freecad_save_reopen_inventory/1.0",
        "required": True,
        "method": "temporary_fcstd_save_copy_archive_reopen",
        "verified": False,
        "missing_entity_ids": [],
        "duplicate_actual_entity_ids": [],
        "unexpected_entity_ids": [],
        "mismatched_entities": [],
        "expected_counts": dict(inventory.get("counts") or {}),
        "actual_counts": {},
        "counts_match": False,
        "inventory_digest": "",
        "reopened_inventory_digest": "",
        "shape_archive_evidence_digest": "",
        "raster_archive_evidence_digest": "",
        "archive_unchanged_after_open": False,
        "phase_timings_ms": phase_timings_ms,
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
    except OSError as exc:
        raise ImportCleanupError(
            "temporary FCStd placeholder cleanup failed",
            details={"retryable": True, "path": temp_path, "errors": [str(exc)]},
        ) from exc
    if Path(temp_path).exists():
        raise ImportCleanupError(
            "temporary FCStd placeholder still exists after cleanup",
            details={"retryable": True, "path": temp_path},
        )
    reopened = None
    original_name = str(getattr(fc_doc, "Name", "") or "")
    try:
        if opts is not None:
            _invoke_import_cancellation_checkpoint(opts, "persistence save")
        save_started = time.perf_counter()
        save_result = save_copy(temp_path)
        phase_timings_ms["save_ms"] = (
            time.perf_counter() - save_started
        ) * 1000.0
        if save_result is False or not Path(temp_path).is_file():
            raise RuntimeError("temporary FCStd saveCopy did not create a file")
        if opts is not None:
            _invoke_import_cancellation_checkpoint(
                opts, "persistence archive evidence"
            )
        archive_started = time.perf_counter()
        archive_evidence = _read_fcstd_shape_archive_evidence(
            temp_path,
            _inventory_required_shape_entity_ids(inventory),
            expected_zero_ink_shape_entity_ids=(
                _inventory_zero_ink_shape_entity_ids(inventory)
            ),
            opts=opts,
        )
        phase_timings_ms["archive_hash_ms"] = (
            time.perf_counter() - archive_started
        ) * 1000.0
        failure["shape_archive_evidence_digest"] = str(
            archive_evidence.get("evidence_digest") or ""
        )
        if archive_evidence.get("verified") is not True:
            failure["reason"] = str(
                archive_evidence.get("reason") or "fcstd_archive_evidence_failed"
            )
            return failure
        if not _bind_fcstd_shape_archive_evidence(inventory, archive_evidence):
            failure["reason"] = "fcstd_archive_binding_failed"
            return failure
        raster_archive_started = time.perf_counter()
        raster_archive_evidence = _read_fcstd_raster_archive_evidence(
            temp_path,
            inventory,
            opts=opts,
            full_document_object_count=len(_document_objects(fc_doc)),
        )
        phase_timings_ms["archive_hash_ms"] += (
            time.perf_counter() - raster_archive_started
        ) * 1000.0
        failure["raster_archive_evidence_digest"] = str(
            raster_archive_evidence.get("evidence_digest") or ""
        )
        if raster_archive_evidence.get("verified") is not True:
            failure["reason"] = str(
                raster_archive_evidence.get("reason")
                or "fcstd_raster_archive_evidence_failed"
            )
            return failure
        if not _bind_fcstd_raster_archive_evidence(
            inventory,
            raster_archive_evidence,
        ):
            failure["reason"] = "fcstd_raster_archive_binding_failed"
            return failure
        failure["inventory_digest"] = str(inventory.get("inventory_digest") or "")
        open_document = getattr(FreeCAD, "openDocument", None)
        if not callable(open_document):
            raise RuntimeError("FreeCAD.openDocument is unavailable")
        if opts is not None:
            _invoke_import_cancellation_checkpoint(opts, "persistence reopen")
        open_started = time.perf_counter()
        try:
            reopened = open_document(temp_path, True, True)
        except TypeError:
            try:
                reopened = open_document(temp_path, True)
            except TypeError:
                reopened = open_document(temp_path)
        phase_timings_ms["open_ms"] = (
            time.perf_counter() - open_started
        ) * 1000.0
        cheap_inventory_started = time.perf_counter()
        reopened_objects = _document_objects(reopened)
        ignored_names = set(baseline_entity_names or set())
        if ignored_names:
            retained_objects = []
            for host_obj in reopened_objects:
                if opts is not None:
                    _invoke_import_cancellation_checkpoint(
                        opts, "persistence reopened object filter"
                    )
                if _host_object_persistent_name(host_obj) not in ignored_names:
                    retained_objects.append(host_obj)
            reopened_objects = retained_objects
        result = _compact_crosscheck_host_object_inventory(
            inventory,
            reopened_objects,
            archive_evidence,
            raster_archive_evidence,
            opts=opts,
        )
        phase_timings_ms["cheap_inventory_ms"] = (
            time.perf_counter() - cheap_inventory_started
        ) * 1000.0
        post_open_hash_started = time.perf_counter()
        post_open_digest, post_open_size = _hash_file_sha256(
            Path(temp_path),
            opts=opts,
            stage="persistence post-open archive hash",
        )
        phase_timings_ms["archive_hash_ms"] += (
            time.perf_counter() - post_open_hash_started
        ) * 1000.0
        archive_unchanged = bool(
            post_open_digest == archive_evidence.get("fcstd_sha256")
            and post_open_size == archive_evidence.get("fcstd_bytes")
            and post_open_digest == raster_archive_evidence.get("fcstd_sha256")
            and post_open_size == raster_archive_evidence.get("fcstd_bytes")
        )
        if not archive_unchanged:
            result["verified"] = False
            result["reason"] = "fcstd_archive_changed_after_open"
        result["method"] = "temporary_fcstd_save_copy_archive_reopen"
        result["archive_unchanged_after_open"] = archive_unchanged
        result["phase_timings_ms"] = phase_timings_ms
        return result
    except ImportCancelled:
        raise
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        AttributeError,
        zipfile.BadZipFile,
    ) as exc:
        failure["reason"] = "save_reopen_runtime_error"
        failure["error_type"] = exc.__class__.__name__
        return failure
    finally:
        primary_error = sys.exc_info()[1]
        finalization_errors: List[str] = []
        if original_name:
            set_active = getattr(FreeCAD, "setActiveDocument", None)
            if callable(set_active):
                try:
                    set_active(original_name)
                except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
                    finalization_errors.append("restore_active_document: %s" % exc)
        if reopened is not None:
            reopened_name = str(getattr(reopened, "Name", "") or "")
            close_document = getattr(FreeCAD, "closeDocument", None)
            if reopened_name and callable(close_document):
                close_started = time.perf_counter()
                try:
                    close_document(reopened_name)
                except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
                    finalization_errors.append("close_reopened_document: %s" % exc)
                phase_timings_ms["close_ms"] = (
                    time.perf_counter() - close_started
                ) * 1000.0
        try:
            if Path(temp_path).exists():
                os.remove(temp_path)
        except OSError as exc:
            finalization_errors.append("remove_temporary_fcstd: %s" % exc)
        if Path(temp_path).exists():
            finalization_errors.append("temporary FCStd still exists after cleanup")
        if finalization_errors:
            cleanup_error = ImportCleanupError(
                "temporary persistence evidence cleanup failed",
                details={
                    "retryable": True,
                    "path": temp_path,
                    "errors": finalization_errors,
                },
            )
            if primary_error is not None:
                try:
                    primary_error.add_note(str(cleanup_error))
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass
                try:
                    setattr(primary_error, "cleanup_error", cleanup_error)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass
            else:
                raise cleanup_error


def _build_persistence_host_evidence(
    fc_doc,
    imported_objects: List[Any],
    baseline_entity_names: Optional[set] = None,
    *,
    opts: Optional[ImportOptions] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build cheap live evidence and verify the exact persisted FCStd archive."""

    live_objects = list(imported_objects or [])
    if opts is not None:
        _invoke_import_cancellation_checkpoint(opts, "persistence evidence")
    inventory = _build_host_object_inventory(
        live_objects,
        shape_evidence_mode="cheap",
        opts=opts,
    )
    if opts is None:
        save_reopen = _save_reopen_host_object_inventory(
            fc_doc,
            inventory,
            baseline_entity_names,
        )
    else:
        save_reopen = _save_reopen_host_object_inventory(
            fc_doc,
            inventory,
            baseline_entity_names,
            opts=opts,
        )
    return inventory, save_reopen


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


def _zero_visible_ink_shape_evidence(shape) -> Dict[str, Any]:
    """Reread an evaluated host shape and certify that it contains no ink."""
    if shape is None:
        raise ValueError("evaluated host shape is unavailable")
    try:
        counts = {
            "vertex_count": len(list(getattr(shape, "Vertexes", []) or [])),
            "edge_count": len(list(getattr(shape, "Edges", []) or [])),
            "face_count": len(list(getattr(shape, "Faces", []) or [])),
            "solid_count": len(list(getattr(shape, "Solids", []) or [])),
        }
        is_null = bool(getattr(shape, "isNull", lambda: False)())
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("evaluated host shape could not be inspected") from exc
    try:
        volume = float(getattr(shape, "Volume", 0.0) or 0.0)
        volume_authority = "evaluated_shape_volume"
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        if not is_null or any(counts.values()):
            raise ValueError("evaluated host shape volume could not be inspected") from exc
        # FreeCAD's real Part.Shape() reports isNull()==True and zero topology,
        # but its Volume property raises "shape is invalid".  Nullness plus
        # independently reread zero topology is the physical host authority;
        # treating its unavailable volume as zero avoids rejecting a genuinely
        # empty requested representation.
        volume = 0.0
        volume_authority = "null_shape_has_no_evaluable_volume"
    if (
        any(counts.values())
        or not math.isfinite(volume)
        or abs(volume) > 1e-12
    ):
        raise ValueError("evaluated host shape contains visible geometry")
    return {
        **counts,
        "volume": volume,
        "volume_authority": volume_authority,
        "shape_is_null": is_null,
        "zero_visible_ink_verified": True,
    }


def _annotate_text_host_object(
    obj,
    source_item_id: str,
    representation: str,
    *,
    parent_source_item_id: Optional[str] = None,
) -> None:
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
    if parent_source_item_id is not None:
        if (
            not isinstance(parent_source_item_id, str)
            or not parent_source_item_id
            or parent_source_item_id == source_item_id
        ):
            raise ValueError("text segment parent identity is invalid")
        if "PDFParentSourceItemId" not in properties and callable(add_property):
            add_property("App::PropertyString", "PDFParentSourceItemId", "PDF Import")
        obj.PDFParentSourceItemId = parent_source_item_id


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
    visibility: Optional[bool] = None,
) -> str:
    """Persist source style on an App object, including in headless FreeCADCmd."""
    color_metadata = _format_color_metadata(source_color)
    properties = set(getattr(obj, "PropertiesList", []) or [])
    add_property = getattr(obj, "addProperty", None)
    metadata = [
        ("App::PropertyString", "PDFTextFontName", str(font_name)),
        ("App::PropertyFloat", "PDFTextFontSize", float(font_size)),
        ("App::PropertyString", "PDFTextJustification", "Left"),
        ("App::PropertyString", "PDFTextColorRGB", color_metadata),
    ]
    if visibility is not None:
        metadata.append(("App::PropertyBool", "PDFTextVisibility", bool(visibility)))
    for property_kind, property_name, property_value in metadata:
        if property_name not in properties and callable(add_property):
            add_property(property_kind, property_name, "PDF Import")
            properties.add(property_name)
        setattr(obj, property_name, property_value)
    return color_metadata


def _persist_font_delivery_metadata(
    obj,
    *,
    source_font_equivalence: bool,
    font_substitution_applied: bool,
    font_identity_verified: bool,
    glyph_coverage_verified: bool,
    delivered_font_sha256: str,
    font_candidate_source: str,
    delivered_font_path: str,
    source_font_asset_path: str,
    source_font_asset_sha256: str,
    staged_font_path: str,
    staged_font_sha256: str,
    staged_asset_verified: bool,
    staged_asset_read_only: bool,
    font_internal_identity_sha256: str,
    font_coverage_evidence_sha256: str,
    source_font_identity: Dict[str, str],
) -> None:
    """Persist and reread the exact 3D font-delivery decision on every host."""

    if (
        type(source_font_equivalence) is not bool
        or type(font_substitution_applied) is not bool
        or type(font_identity_verified) is not bool
        or type(glyph_coverage_verified) is not bool
        or re.fullmatch(r"[0-9a-f]{64}", delivered_font_sha256) is None
        or not isinstance(font_candidate_source, str)
        or not font_candidate_source
        or not isinstance(delivered_font_path, str)
        or not delivered_font_path
        or not isinstance(source_font_asset_path, str)
        or not source_font_asset_path
        or re.fullmatch(r"[0-9a-f]{64}", source_font_asset_sha256) is None
        or not isinstance(staged_font_path, str)
        or not staged_font_path
        or re.fullmatch(r"[0-9a-f]{64}", staged_font_sha256) is None
        or type(staged_asset_verified) is not bool
        or type(staged_asset_read_only) is not bool
        or re.fullmatch(r"[0-9a-f]{64}", font_internal_identity_sha256) is None
        or re.fullmatch(r"[0-9a-f]{64}", font_coverage_evidence_sha256) is None
        or not isinstance(source_font_identity, dict)
        or not source_font_identity.get("raw_name")
        or not source_font_identity.get("normalized_key")
    ):
        raise ValueError("3D font delivery metadata is invalid")
    source_identity_json = json.dumps(
        source_font_identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    properties = set(getattr(obj, "PropertiesList", []) or [])
    add_property = getattr(obj, "addProperty", None)
    metadata = (
        ("App::PropertyBool", "PDFSourceFontEquivalence", source_font_equivalence),
        ("App::PropertyBool", "PDFFontSubstitutionApplied", font_substitution_applied),
        ("App::PropertyBool", "PDFFontIdentityVerified", font_identity_verified),
        ("App::PropertyBool", "PDFFontGlyphCoverageVerified", glyph_coverage_verified),
        ("App::PropertyString", "PDFDeliveredFontSHA256", delivered_font_sha256),
        ("App::PropertyString", "PDFFontCandidateSource", font_candidate_source),
        ("App::PropertyString", "PDFDeliveredFontPath", delivered_font_path),
        ("App::PropertyString", "PDFSourceFontAssetPath", source_font_asset_path),
        ("App::PropertyString", "PDFSourceFontAssetSHA256", source_font_asset_sha256),
        ("App::PropertyString", "PDFStagedFontPath", staged_font_path),
        ("App::PropertyString", "PDFStagedFontSHA256", staged_font_sha256),
        ("App::PropertyBool", "PDFStagedFontVerified", staged_asset_verified),
        ("App::PropertyBool", "PDFStagedFontReadOnly", staged_asset_read_only),
        (
            "App::PropertyString",
            "PDFFontInternalIdentitySHA256",
            font_internal_identity_sha256,
        ),
        (
            "App::PropertyString",
            "PDFFontCoverageEvidenceSHA256",
            font_coverage_evidence_sha256,
        ),
        ("App::PropertyString", "PDFSourceFontIdentityJSON", source_identity_json),
    )
    for property_kind, property_name, property_value in metadata:
        if property_name not in properties and callable(add_property):
            add_property(property_kind, property_name, "PDF Import")
            properties.add(property_name)
        setattr(obj, property_name, property_value)


def _host_font_delivery_metadata_matches(
    host_obj: Any,
    evidence: Dict[str, Any],
    source_font_identity: Dict[str, str],
) -> bool:
    try:
        expected_identity_json = json.dumps(
            source_font_identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return bool(
            getattr(host_obj, "PDFSourceFontEquivalence", None)
            is evidence["source_font_equivalence"]
            and getattr(host_obj, "PDFFontSubstitutionApplied", None)
            is evidence["font_substitution_applied"]
            and getattr(host_obj, "PDFFontIdentityVerified", None)
            is evidence["font_identity_verified"]
            and getattr(host_obj, "PDFFontGlyphCoverageVerified", None)
            is evidence["glyph_coverage_verified"]
            and getattr(host_obj, "PDFDeliveredFontSHA256", None)
            == evidence["delivered_font_sha256"]
            and getattr(host_obj, "PDFFontCandidateSource", None)
            == evidence["font_candidate_source"]
            and getattr(host_obj, "PDFDeliveredFontPath", None)
            == evidence["staged_font_path"]
            and getattr(host_obj, "PDFSourceFontAssetPath", None)
            == evidence["source_font_asset_path"]
            and getattr(host_obj, "PDFSourceFontAssetSHA256", None)
            == evidence["source_font_asset_sha256"]
            and getattr(host_obj, "PDFStagedFontPath", None)
            == evidence["staged_font_path"]
            and getattr(host_obj, "PDFStagedFontSHA256", None)
            == evidence["staged_font_sha256"]
            and getattr(host_obj, "PDFStagedFontVerified", None)
            is evidence["staged_asset_verified"]
            and getattr(host_obj, "PDFStagedFontReadOnly", None)
            is evidence["staged_asset_read_only"]
            and getattr(host_obj, "PDFFontInternalIdentitySHA256", None)
            == evidence["font_internal_identity_sha256"]
            and getattr(host_obj, "PDFFontCoverageEvidenceSHA256", None)
            == evidence["font_coverage_evidence_sha256"]
            and getattr(host_obj, "PDFSourceFontIdentityJSON", None)
            == expected_identity_json
        )
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return False


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

    # The extrusion depends on the calibrated clone.  Configure both first,
    # then let FreeCAD recompute the dependency pair in one ordered pass.  A
    # separate pass for each object performs the same work with one additional
    # document traversal per source span on dense pages.
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
        doc.recompute([calibrated_support, extrusion])
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


def _deliver_mixed_source_ink_segments(
    item: Dict[str, Any],
    attempted_type: str,
    opts: ImportOptions,
    *,
    deliver_child,
    text_group,
) -> Dict[str, Any]:
    """Deliver all contiguous mixed-ink children on one representation rung."""

    manifest, children = _build_source_ink_segment_items(item)
    parent_id = manifest["parent_source_item_id"]
    child_results: List[Dict[str, Any]] = []
    created_ids: List[str] = []
    delivery_ids: List[str] = []
    support_ids: List[str] = []

    def append_unique(target: List[str], values) -> None:
        for value in list(values or []):
            if isinstance(value, str) and value and value not in target:
                target.append(value)

    doc = _text_host_document(None, text_group)
    baseline_object_ids: set[int] = set()
    baseline_snapshot_valid = doc is None
    current_child: Optional[Dict[str, Any]] = None
    raw_created_count = 0
    raw_delivery_count = 0
    raw_support_count = 0

    try:
        if doc is not None:
            baseline_object_ids = {
                id(host_obj) for host_obj in list(getattr(doc, "Objects", []) or [])
            }
            baseline_snapshot_valid = True
        for child in children:
            current_child = child
            result = deliver_child(copy.deepcopy(child))
            normalized = _normalize_verified_text_item_result(
                child,
                item["requested_type"],
                attempted_type,
                result,
            )
            child_results.append(normalized)
            raw_created_count += len(normalized["created_entity_ids"])
            raw_delivery_count += len(normalized["delivery_entity_ids"])
            raw_support_count += len(normalized["support_entity_ids"])
            append_unique(created_ids, normalized["created_entity_ids"])
            append_unique(delivery_ids, normalized["delivery_entity_ids"])
            append_unique(support_ids, normalized["support_entity_ids"])

        if (
            len(child_results) != len(children)
            or not created_ids
            or not delivery_ids
            or raw_created_count != len(created_ids)
            or raw_delivery_count != len(delivery_ids)
            or raw_support_count != len(support_ids)
            or set(delivery_ids).intersection(support_ids)
            or set(delivery_ids).union(support_ids) != set(created_ids)
        ):
            raise ValueError("segmented delivery ownership is incomplete")
    except Exception as failure:
        failed_attempt = dict(getattr(failure, "attempt", {}) or {})
        append_unique(created_ids, failed_attempt.get("created_entity_ids"))
        removed_ids: List[str] = []
        append_unique(removed_ids, failed_attempt.get("removed_entity_ids"))
        owned: List[Any] = []
        cleanup_error = ""
        if doc is not None and baseline_snapshot_valid:
            try:
                owned = [
                    host_obj
                    for host_obj in list(getattr(doc, "Objects", []) or [])
                    if id(host_obj) not in baseline_object_ids
                ]
                append_unique(
                    created_ids,
                    [_host_object_id(host_obj) for host_obj in owned],
                )
            except Exception as exc:
                cleanup_error = "%s: %s" % (exc.__class__.__name__, exc)
        elif doc is not None:
            cleanup_error = "segmented baseline snapshot is unavailable"
        helper_complete = not owned and not cleanup_error
        if doc is not None and owned:
            try:
                helper_removed, helper_complete = _remove_owned_text_objects(
                    doc, text_group, owned
                )
                append_unique(removed_ids, helper_removed)
            except Exception as exc:
                cleanup_error = "%s: %s" % (exc.__class__.__name__, exc)
                helper_complete = False
        still_live = False
        if doc is not None and baseline_snapshot_valid:
            try:
                still_live = any(
                    id(host_obj) not in baseline_object_ids
                    for host_obj in list(getattr(doc, "Objects", []) or [])
                )
            except Exception as exc:
                cleanup_error = cleanup_error or "%s: %s" % (
                    exc.__class__.__name__,
                    exc,
                )
                still_live = True
        cleanup_complete = bool(
            helper_complete
            and failed_attempt.get("cleanup_complete", True) is True
            and set(created_ids) == set(removed_ids)
            and not still_live
            and not cleanup_error
        )
        failure_evidence = {
            "source_segment_manifest": manifest,
            "failed_child_source_item_id": (
                failed_attempt.get("source_item_id")
                or (current_child or {}).get("source_item_id")
            ),
            "failed_child_attempt": failed_attempt,
            "exception": "%s: %s" % (failure.__class__.__name__, failure),
        }
        if cleanup_error:
            failure_evidence["cleanup_error"] = cleanup_error
        attempt = {
            "source_item_id": parent_id,
            "requested_type": item["requested_type"],
            "attempted_type": attempted_type,
            "final_type": None,
            "outcome": "failed",
            "reason": "source_segment_delivery_failed",
            "created_entity_ids": created_ids,
            "removed_entity_ids": removed_ids,
            "cleanup_complete": cleanup_complete,
            "evidence": failure_evidence,
        }
        raise TextRepresentationFailure(
            "%s segmented delivery failed for %s" % (attempted_type, parent_id),
            attempt,
        ) from failure

    parent_evidence = _validated_source_ink_evidence(item)
    child_ids = [child["source_item_id"] for child in children]
    delivery_count = sum(
        int(result.get("delivery_count") or len(result["delivery_entity_ids"]))
        for result in child_results
    )
    return {
        "source_item_id": parent_id,
        "requested_type": item["requested_type"],
        "attempted_type": attempted_type,
        "final_type": attempted_type,
        "outcome": "verified",
        "created_entity_ids": created_ids,
        "delivery_entity_ids": delivery_ids,
        "support_entity_ids": support_ids,
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "delivery_count": delivery_count,
        "evidence": {
            "source_text": item["text"],
            "source_text_preserved": True,
            "source_item_id_verified": True,
            "source_ink_evidence": parent_evidence,
            "source_segment_ink_evidence_persisted": True,
            "source_segment_manifest": manifest,
            "child_source_item_ids": child_ids,
            "segment_deliveries": child_results,
            **_source_ink_delivery_binding_fields(item, parent_evidence),
        },
    }


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
    source_ink_evidence: Optional[Dict[str, Any]] = None
    doc = _text_host_document(None, text_group)
    baseline_objects: set = set()
    owned: List[Any] = []

    raw_source_ink = bound_item.get("source_ink_evidence")
    if (
        bound_item.get("parent_source_item_id") is None
        and isinstance(raw_source_ink, dict)
        and raw_source_ink.get("classification")
        == "mixed_visible_and_zero_ink"
    ):
        return _deliver_mixed_source_ink_segments(
            bound_item,
            attempted_type,
            opts,
            deliver_child=lambda child: _deliver_text_item_native(
                child,
                attempted_type,
                opts,
                text_group=text_group,
                page_h=page_h,
                scale=scale,
            ),
            text_group=text_group,
        )

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
            or not _canonical_or_segment_source_id_matches(bound_item)
            or not isinstance(pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
            or not isinstance(source_text, str)
            or not source_text
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
        source_ink_evidence = _validated_source_ink_evidence(bound_item)
    except ValueError as exc:
        fail(
            "source_ink_authority_missing",
            {
                "source_text_sha256": hashlib.sha256(
                    source_text.encode("utf-8")
                ).hexdigest(),
                "required_authority": "physical_pdf_character_evidence",
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )
    if _mixed_source_ink_requires_fallback(source_ink_evidence):
        _raise_mixed_source_ink_impossible(bound_item, attempted_type)
    expected_visibility = source_ink_evidence["classification"] != "zero_visible_ink"

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
            )
            text_property = "Text"
            expected_type = "App::FeaturePython"
            expected_proxy_type = "Label"
        if host_obj is None or id(host_obj) in baseline_objects:
            raise RuntimeError("native text factory returned no new host object")
        owned.append(host_obj)
        text_group.addObject(host_obj)
        _annotate_text_host_object(
            host_obj,
            source_item_id,
            attempted_type,
            parent_source_item_id=bound_item.get("parent_source_item_id"),
        )
        if source_ink_evidence is not None:
            _persist_source_ink_evidence(host_obj, source_ink_evidence)

        normalized_font = _normalize_pdf_font_name(span.get("font", ""))
        source_color = _span_source_color(span)
        color_metadata = _persist_text_style_metadata(
            host_obj,
            font_name=normalized_font,
            font_size=font_size_fc,
            source_color=source_color,
            visibility=expected_visibility,
        )

        # FreeCADCmd intentionally has no GUI view provider. Persist and reread
        # the complete style contract on the document object in every host, then
        # additionally require the real view-provider style when one exists.
        view = getattr(host_obj, "ViewObject", None)
        font_properties = []
        if view is not None:
            try:
                from PDFVectorImporter.src.PDFGuiStyleRestorer import (
                    restore_importer_text_object,
                )
            except ImportError:
                from PDFGuiStyleRestorer import restore_importer_text_object

            if not restore_importer_text_object(host_obj):
                raise RuntimeError("native host view style could not be restored")
            font_properties = [
                property_name
                for property_name in ("FontName", "Font")
                if hasattr(view, property_name)
            ]
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
        metadata_visibility = getattr(host_obj, "PDFTextVisibility", None)
        label_marker_absent = False
        actual_ink_classification = str(
            getattr(host_obj, "PDFSourceInkClassification", "") or ""
        )
        actual_ink_digest = str(
            getattr(host_obj, "PDFSourceInkEvidenceSHA256", "") or ""
        )
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
            view_style_verified = bool(
                bool(getattr(view, "Visibility", False)) == expected_visibility
                and (
                    not hasattr(view, "DisplayMode")
                    or str(view.DisplayMode or "") == "World"
                )
                and (
                    not hasattr(view, "ScaleMultiplier")
                    or math.isclose(float(view.ScaleMultiplier), 1.0, abs_tol=1e-7)
                )
                and (
                    not hasattr(view, "LineSpacing")
                    or math.isclose(float(view.LineSpacing), 1.0, abs_tol=1e-7)
                )
            )
            if attempted_type == "labels":
                arrow_properties = [
                    property_name
                    for property_name in ("ArrowTypeStart", "ArrowType")
                    if hasattr(view, property_name)
                ]
                label_marker_absent = bool(
                    not list(getattr(host_obj, "Points", []) or [])
                    and hasattr(view, "Line")
                    and view.Line is False
                    and hasattr(view, "Frame")
                    and str(view.Frame or "") == "None"
                    and arrow_properties
                    and all(
                        str(getattr(view, property_name) or "") == "None"
                        for property_name in arrow_properties
                    )
                )
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
            or getattr(host_obj, "PDFParentSourceItemId", None)
            != bound_item.get("parent_source_item_id")
            or getattr(host_obj, "PDFRepresentation", None) != attempted_type
            or actual_anchor is None
            or any(
                abs(actual_anchor[index] - expected_anchor[index]) > 1e-7
                for index in range(3)
            )
            or actual_rotation is None
            or not _rotation_matches(actual_rotation, host_rotation_deg)
            or not math.isclose(actual_font_size, font_size_fc, abs_tol=1e-7)
            or (normalized_font and normalized_font not in actual_fonts)
            or actual_justification != "Left"
            or metadata_font_name != normalized_font
            or not math.isclose(metadata_font_size, font_size_fc, abs_tol=1e-7)
            or metadata_justification != "Left"
            or metadata_color != color_metadata
            or metadata_visibility is not expected_visibility
            or (
                source_ink_evidence is not None
                and (
                    actual_ink_classification
                    != source_ink_evidence["classification"]
                    or actual_ink_digest
                    != source_ink_evidence["evidence_sha256"]
                )
            )
            or not color_verified
            or (view is not None and not view_style_verified)
            or (
                attempted_type == "labels"
                and view is not None
                and not label_marker_absent
            )
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
        "parent_source_item_id": bound_item.get("parent_source_item_id"),
        "expected_anchor_xyz": expected_anchor,
        "verified_anchor_xyz": tuple(actual_anchor),
        "rotation_deg": float(actual_rotation),
        "font_name": normalized_font,
        "font_size": float(actual_font_size),
        "source_color": source_color,
        "color_verified": bool(color_verified),
        "style_verification": style_verification,
        "view_style_verified": bool(view_style_verified),
        "physical_visibility": expected_visibility,
    }
    if source_ink_evidence is not None:
        delivery_evidence["source_ink_evidence"] = copy.deepcopy(
            source_ink_evidence
        )
        delivery_evidence["source_ink_evidence_persisted"] = True
        delivery_evidence.update(
            _source_ink_delivery_binding_fields(bound_item, source_ink_evidence)
        )
    if attempted_type == "labels":
        delivery_evidence.update(
            {
                "label_marker_absent": bool(label_marker_absent),
                "label_marker_verification": (
                    "gui_view" if view is not None else "pending"
                ),
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


def _publish_prepared_raster_target(
    temporary_path: Path,
    target: Path,
    payload_sha256: str,
    opts: Optional[ImportOptions],
) -> bool:
    """Atomically publish a shared immutable blob without ever replacing bytes.

    The unique temporary path is attempt-owned.  Once the content-addressed
    target has been created, however, it is shared cache state: a concurrent or
    later successful attempt may already depend on it.  The final target must
    therefore never enter an attempt rollback journal.
    """

    temporary_path = Path(temporary_path).resolve()
    target = Path(target).resolve()
    if (
        re.fullmatch(r"[0-9a-f]{64}", payload_sha256) is None
        or not temporary_path.is_file()
        or temporary_path.stat().st_size <= 0
        or _path_sha256(temporary_path) != payload_sha256
    ):
        raise ImportLifecycleError("prepared raster asset is invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    payload_size = temporary_path.stat().st_size
    if target.exists():
        if (
            not target.is_file()
            or target.stat().st_size != payload_size
            or _path_sha256(target) != payload_sha256
        ):
            raise ImportLifecycleError(
                "content-addressed raster target is corrupt: %s" % target
            )
        return False

    try:
        os.link(str(temporary_path), str(target))
        published = True
    except FileExistsError:
        published = False
    if (
        not target.is_file()
        or target.stat().st_size != payload_size
        or _path_sha256(target) != payload_sha256
    ):
        raise ImportLifecycleError("content-addressed raster publication failed")
    if published:
        try:
            target.chmod(stat.S_IREAD)
        except OSError:
            # Content addressing plus create-if-absent remains authoritative on
            # filesystems that cannot expose a stable read-only mode.
            pass
    return published


def _persist_content_addressed_pixmap(
    pix,
    opts: ImportOptions,
    *,
    asset_kind: str,
) -> Tuple[Path, Dict[str, Any]]:
    """Save exact PNG bytes once, name them by their real SHA-256, and verify."""

    kind = str(asset_kind or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9_]{0,31}", kind) is None:
        raise ValueError("raster asset kind is invalid")
    asset_dir = _raster_asset_dir().resolve()
    asset_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=kind + ".",
        suffix=".png",
        dir=str(asset_dir),
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name).resolve()
    _journal_new_attempt_path(opts, temporary_path)
    try:
        pix.save(str(temporary_path))
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise RuntimeError("raster renderer produced an empty temporary asset")
        payload_sha256 = _path_sha256(temporary_path)
        target = (asset_dir / ("%s_%s.png" % (kind, payload_sha256))).resolve()
        published = _publish_prepared_raster_target(
            temporary_path,
            target,
            payload_sha256,
            opts,
        )
        if _path_sha256(target) != payload_sha256:
            raise ImportLifecycleError("published raster SHA-256 changed")
        return target, {
            "source_asset_path": str(target),
            "source_asset_sha256": payload_sha256,
            "source_asset_bytes": int(target.stat().st_size),
            "asset_content_addressed": True,
            "asset_atomic_create_if_absent": True,
            "asset_never_overwritten": True,
            "asset_published_by_attempt": published,
        }
    finally:
        if temporary_path.exists():
            try:
                temporary_path.chmod(stat.S_IREAD | stat.S_IWRITE)
                temporary_path.unlink()
            except OSError as exc:
                raise ImportCleanupError(
                    "temporary raster asset cleanup failed",
                    details={
                        "retryable": True,
                        "path": str(temporary_path),
                        "errors": [str(exc)],
                    },
                ) from exc
        if temporary_path.exists():
            raise ImportCleanupError(
                "temporary raster asset still exists after cleanup",
                details={"retryable": True, "path": str(temporary_path)},
            )
        _forget_attempt_path(opts, temporary_path)


def _save_pixmap_atomic(
    pix,
    image_path: Path,
    opts: Optional[ImportOptions] = None,
) -> None:
    """Create one exact target atomically; existing bytes are never replaced."""
    image_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=image_path.stem + ".",
        suffix=".png",
        dir=str(image_path.parent),
    )
    os.close(fd)
    temporary_path = Path(temporary_name).resolve()
    if opts is not None:
        _journal_new_attempt_path(opts, temporary_path)
    try:
        pix.save(str(temporary_path))
        if not temporary_path.is_file() or temporary_path.stat().st_size <= 0:
            raise RuntimeError("raster renderer produced an empty temporary asset")
        payload_sha256 = _path_sha256(temporary_path)
        _publish_prepared_raster_target(
            temporary_path,
            Path(image_path),
            payload_sha256,
            opts,
        )
    finally:
        if temporary_path.exists():
            try:
                temporary_path.chmod(stat.S_IREAD | stat.S_IWRITE)
                temporary_path.unlink()
            except OSError as exc:
                raise ImportCleanupError(
                    "temporary raster asset cleanup failed",
                    details={
                        "retryable": True,
                        "path": str(temporary_path),
                        "errors": [str(exc)],
                    },
                ) from exc
            if temporary_path.exists():
                raise ImportCleanupError(
                    "temporary raster asset still exists after cleanup",
                    details={"retryable": True, "path": str(temporary_path)},
                )
        if opts is not None:
            _forget_attempt_path(opts, temporary_path)


def _persist_embedded_image_asset(
    pix,
    opts: ImportOptions,
    *,
    page_number: int,
    xref: int,
) -> Tuple[Path, Dict[str, Any]]:
    """Atomically publish one PDF-bound, content-addressed embedded image."""

    pdf_sha256 = str(getattr(opts, "_pdf_sha256", "") or "").strip().lower()
    if (
        re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
        or type(page_number) is not int
        or page_number <= 0
        or type(xref) is not int
        or xref < 0
    ):
        raise ValueError("embedded image source identity is invalid")

    if hashlib.sha256(_validated_pdf_source_bytes(opts)).hexdigest() != pdf_sha256:
        raise ValueError("embedded image PDF bytes do not match the attempt")
    target, evidence = _persist_content_addressed_pixmap(
        pix,
        opts,
        asset_kind="embedded",
    )
    evidence.update(
        {
            "pdf_sha256": pdf_sha256,
            "page_number": page_number,
            "source_xref": xref,
            "asset_atomic_publish": True,
            "asset_shared_immutable": True,
        }
    )
    return target, evidence


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
    included_property_type = _host_property_type_id(host_obj, "PDFRasterFile")
    if included_property_type != "App::PropertyFileIncluded":
        raise RuntimeError(
            "PDFRasterFile is not an App::PropertyFileIncluded property"
        )
    evidence["included_file_property_type"] = included_property_type
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


def _bind_embedded_image_host_asset(
    host_obj,
    source_asset_path: Path,
    *,
    pdf_sha256: str,
    page_number: int,
    xref: int,
) -> Dict[str, Any]:
    """Embed and bind exact extracted bytes to one ImagePlane host object."""

    digest = str(pdf_sha256 or "").strip().lower()
    if (
        re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or type(page_number) is not int
        or page_number <= 0
        or type(xref) is not int
        or xref < 0
    ):
        raise ValueError("embedded image host source identity is invalid")
    asset_path = Path(source_asset_path).resolve()
    if not asset_path.is_file() or asset_path.stat().st_size <= 0:
        raise RuntimeError("embedded image asset is unavailable")

    add_property = getattr(host_obj, "addProperty", None)
    if not callable(add_property):
        raise RuntimeError("ImagePlane cannot embed its extracted image asset")
    properties = set(getattr(host_obj, "PropertiesList", []) or [])
    declarations = (
        ("App::PropertyFileIncluded", "PDFRasterFile"),
        ("App::PropertyString", "PDFSourceSHA256"),
        ("App::PropertyString", "PDFRasterSHA256"),
        ("App::PropertyInteger", "PDFImagePageNumber"),
        ("App::PropertyInteger", "PDFImageSourceXRef"),
    )
    for property_type, property_name in declarations:
        if property_name not in properties:
            add_property(property_type, property_name, "PDF Import")
            properties.add(property_name)

    asset_sha256 = _path_sha256(asset_path)
    host_obj.ImageFile = str(asset_path)
    host_obj.PDFRasterFile = str(asset_path)
    host_obj.PDFSourceSHA256 = digest
    host_obj.PDFRasterSHA256 = asset_sha256
    host_obj.PDFImagePageNumber = page_number
    host_obj.PDFImageSourceXRef = xref
    evidence = _raster_file_evidence(host_obj, asset_path)
    if (
        evidence.get("source_asset_sha256") != asset_sha256
        or str(getattr(host_obj, "PDFSourceSHA256", "")) != digest
        or str(getattr(host_obj, "PDFRasterSHA256", "")) != asset_sha256
        or int(getattr(host_obj, "PDFImagePageNumber", 0) or 0) != page_number
        or int(getattr(host_obj, "PDFImageSourceXRef", 0) or 0) != xref
    ):
        raise RuntimeError("embedded image host asset binding could not be verified")
    evidence.update(
        {
            "pdf_sha256": digest,
            "page_number": page_number,
            "source_xref": xref,
            "included_asset_verified": True,
        }
    )
    return evidence


def _create_bound_embedded_image_plane(
    *,
    fc_doc,
    image_group,
    image_path: Path,
    image_asset_evidence: Dict[str, Any],
    page_number: int,
    xref: int,
    x_size: float,
    y_size: float,
    placement,
):
    """Create one ImagePlane, removing that exact object if binding fails."""

    image_plane = None
    added_to_group = False
    try:
        image_plane = fc_doc.addObject("Image::ImagePlane", "Image")
        image_plane.XSize = x_size
        image_plane.YSize = y_size
        image_plane.Placement = placement
        host_asset_evidence = _bind_embedded_image_host_asset(
            image_plane,
            image_path,
            pdf_sha256=image_asset_evidence["pdf_sha256"],
            page_number=page_number,
            xref=xref,
        )
        if (
            host_asset_evidence.get("source_asset_sha256")
            != image_asset_evidence.get("source_asset_sha256")
        ):
            raise RuntimeError("embedded image host bytes changed before placement")
        image_group.addObject(image_plane)
        added_to_group = True
        return image_plane, host_asset_evidence
    except BaseException as failure:
        cleanup_errors: List[str] = []
        if image_plane is not None:
            if added_to_group or image_plane in list(
                getattr(image_group, "Group", []) or []
            ):
                try:
                    image_group.removeObject(image_plane)
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    cleanup_errors.append("remove image from group: %s" % exc)
            object_name = str(getattr(image_plane, "Name", "") or "")
            try:
                current = fc_doc.getObject(object_name) if object_name else None
            except (AttributeError, RuntimeError, TypeError) as exc:
                current = None
                cleanup_errors.append("find image object: %s" % exc)
            if current is image_plane:
                try:
                    fc_doc.removeObject(object_name)
                except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                    cleanup_errors.append("remove image object: %s" % exc)
            try:
                still_live = bool(
                    object_name and fc_doc.getObject(object_name) is image_plane
                )
            except (AttributeError, RuntimeError, TypeError) as exc:
                still_live = True
                cleanup_errors.append("verify image cleanup: %s" % exc)
            if still_live:
                cleanup_errors.append("exact ImagePlane remains after cleanup")
        if cleanup_errors:
            raise ImportCleanupError(
                "embedded image placement cleanup failed",
                details={
                    "retryable": True,
                    "entity_id": str(getattr(image_plane, "Name", "") or ""),
                    "errors": cleanup_errors,
                },
            ) from failure
        raise


def _validated_page_visual_observation(
    opts: ImportOptions,
    page_number: int,
) -> Dict[str, Any]:
    """Return the exact retained v2 observation for one selected page."""

    if type(page_number) is not int or page_number <= 0:
        raise ValueError("source page number is invalid")
    authority = getattr(opts, "_page_visual_authority", None)
    scope_id = "p%d:page" % page_number
    retained = getattr(opts, "page_visual_source_observations", {}).get(scope_id)
    if authority is None:
        raise ValueError("page visual source authority is unavailable")
    try:
        authoritative = authority.observation(page_number, scope_id)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("page visual authority does not cover the source page") from exc
    if (
        retained != authoritative
        or not page_visual_source_observation_v2_verified(retained, authority)
        or retained.get("pdf_sha256") != getattr(opts, "_pdf_sha256", None)
    ):
        raise ValueError("page visual source observation is invalid")
    return copy.deepcopy(retained)


def _validated_live_source_page(
    page,
    opts: ImportOptions,
    page_number: int,
    *,
    document=None,
) -> Dict[str, Any]:
    """Reject a live page unless its parent retains the exact attempt bytes."""

    source_bytes = _validated_pdf_source_bytes(opts)
    parent = getattr(page, "parent", None)
    if document is not None and parent is not document:
        raise ValueError("live page does not belong to the supplied document")
    parent_stream = getattr(parent, "stream", None)
    page_index = getattr(page, "number", None)
    if (
        parent is None
        or not isinstance(parent_stream, (bytes, bytearray, memoryview))
        or bytes(parent_stream) != source_bytes
        or hashlib.sha256(bytes(parent_stream)).hexdigest() != opts._pdf_sha256
        or type(page_index) is not int
        or page_index != page_number - 1
    ):
        raise ValueError("live page is not from the retained exact PDF bytes")
    return _validated_page_visual_observation(opts, page_number)


def _quad_points(quad) -> Tuple[Tuple[float, float], ...]:
    return tuple(
        (float(point.x), float(point.y))
        for point in (quad.ul, quad.ur, quad.lr, quad.ll)
    )


def _canonical_page_manifest_value(value: Any) -> Any:
    """Project PyMuPDF page structures into deterministic finite JSON."""

    if value is None or type(value) in {bool, int, str}:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("page manifest contains a non-finite number")
        rounded = round(float(value), 9)
        return 0.0 if rounded == 0.0 else rounded
    if isinstance(value, (bytes, bytearray, memoryview)):
        payload = bytes(value)
        return {
            "byte_count": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    if isinstance(value, dict):
        normalized: Dict[str, Any] = {}
        for key, child in value.items():
            normalized_key = str(key)
            if normalized_key in normalized:
                raise ValueError("page manifest keys are ambiguous")
            normalized[normalized_key] = _canonical_page_manifest_value(child)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonical_page_manifest_value(child) for child in value]
    try:
        children = list(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("page manifest contains an unsupported value") from exc
    return [_canonical_page_manifest_value(child) for child in children]


def _page_manifest_digest(value: Any) -> str:
    payload = _canonical_page_manifest_value(value)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _canonical_page_manifest_json(value: Any) -> str:
    return json.dumps(
        _canonical_page_manifest_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _page_text_suppression_evidence_digest(evidence: Any) -> str:
    """Digest one complete suppression proof, excluding only its own digest."""

    if not isinstance(evidence, dict):
        return ""
    payload = {
        key: value for key, value in evidence.items() if key != "evidence_sha256"
    }
    try:
        return _page_manifest_digest(payload)
    except (TypeError, ValueError, OverflowError):
        return ""


def _rawdict_printable_character_count(raw: Any) -> int:
    if not isinstance(raw, dict) or not isinstance(raw.get("blocks"), list):
        raise ValueError("page text dictionary is invalid")
    count = 0
    for block in raw["blocks"]:
        if not isinstance(block, dict) or block.get("type") != 0:
            continue
        lines = block.get("lines")
        if not isinstance(lines, list):
            raise ValueError("page text lines are invalid")
        for line in lines:
            if not isinstance(line, dict) or not isinstance(line.get("spans"), list):
                raise ValueError("page text spans are invalid")
            for span in line["spans"]:
                if not isinstance(span, dict):
                    raise ValueError("page text span is invalid")
                count += sum(1 for character in _raw_span_text(span) if not character.isspace())
    return count


def _page_nontext_bbox_manifest(page) -> List[Any]:
    try:
        bbox_log = page.get_bboxlog()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("page non-text paint manifest is unavailable") from exc
    if not isinstance(bbox_log, list):
        raise ValueError("page non-text paint manifest is invalid")
    return [
        entry
        for entry in bbox_log
        if isinstance(entry, (list, tuple))
        and entry
        and "text" not in str(entry[0]).strip().lower()
    ]


def _page_drawing_manifest(page) -> List[Dict[str, Any]]:
    try:
        drawings = page.get_drawings()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("page drawing manifest is unavailable") from exc
    if not isinstance(drawings, list):
        raise ValueError("page drawing manifest is invalid")
    normalized: List[Dict[str, Any]] = []
    for drawing in drawings:
        if not isinstance(drawing, dict):
            raise ValueError("page drawing entry is invalid")
        normalized.append(
            {key: value for key, value in drawing.items() if str(key) != "seqno"}
        )
    return normalized


def _page_image_manifest(page) -> List[Dict[str, Any]]:
    try:
        images = page.get_image_info(xrefs=True)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("page image manifest is unavailable") from exc
    if not isinstance(images, list) or any(not isinstance(image, dict) for image in images):
        raise ValueError("page image manifest is invalid")
    return images


def _page_annotation_manifest(page) -> List[Dict[str, Any]]:
    """Capture every non-Redact annotation without retaining host objects."""

    try:
        annotations = list(page.annots() or ())
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("page annotation manifest is unavailable") from exc
    redact_type = getattr(fitz, "PDF_ANNOT_REDACT", 12)
    manifest: List[Dict[str, Any]] = []
    for annotation in annotations:
        annotation_type = getattr(annotation, "type", None)
        code = annotation_type[0] if isinstance(annotation_type, tuple) else None
        label = annotation_type[1] if isinstance(annotation_type, tuple) else ""
        if code == redact_type or str(label).strip().lower() == "redact":
            continue
        try:
            manifest.append(
                {
                    "xref": int(getattr(annotation, "xref", 0) or 0),
                    "type": annotation_type,
                    "rect": tuple(float(value) for value in annotation.rect),
                    "flags": int(getattr(annotation, "flags", 0) or 0),
                    "opacity": float(getattr(annotation, "opacity", 0.0) or 0.0),
                    "info": dict(getattr(annotation, "info", {}) or {}),
                    "colors": dict(getattr(annotation, "colors", {}) or {}),
                    "border": dict(getattr(annotation, "border", {}) or {}),
                    "vertices": list(getattr(annotation, "vertices", []) or []),
                }
            )
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ValueError("page annotation entry is invalid") from exc
    return manifest


def _later_nontext_occlusions(page, text_rects: List[Any]) -> List[Dict[str, Any]]:
    """Find paint after text that a structural overlay would incorrectly cover."""

    try:
        bbox_log = page.get_bboxlog()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("page paint-order manifest is unavailable") from exc
    if not isinstance(bbox_log, list):
        raise ValueError("page paint-order manifest is invalid")
    occlusions: List[Dict[str, Any]] = []
    for text_index, text_rect_value in enumerate(text_rects):
        text_rect = fitz.Rect(text_rect_value)
        source_text_indexes = [
            index
            for index, entry in enumerate(bbox_log)
            if isinstance(entry, (list, tuple))
            and len(entry) >= 2
            and "text" in str(entry[0]).strip().lower()
            and _rectangles_overlap_with_area(entry[1], text_rect)
        ]
        if not source_text_indexes:
            raise ValueError("canonical text has no matching paint-order entry")
        first_text_index = min(source_text_indexes)
        for paint_index, entry in enumerate(bbox_log):
            if (
                paint_index <= first_text_index
                or not isinstance(entry, (list, tuple))
                or len(entry) < 2
                or "text" in str(entry[0]).strip().lower()
                or not _rectangles_overlap_with_area(entry[1], text_rect)
            ):
                continue
            occlusions.append(
                {
                    "text_region_index": text_index,
                    "text_paint_index": first_text_index,
                    "later_paint_index": paint_index,
                    "later_paint_type": str(entry[0]),
                }
            )
    return occlusions


def _reject_unsafe_page_text_suppression_state(page) -> None:
    """Reject source state that text-only redaction cannot isolate safely."""

    try:
        annotations = list(page.annots() or ())
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("page annotation state is unavailable") from exc
    redact_type = getattr(fitz, "PDF_ANNOT_REDACT", 12)
    for annotation in annotations:
        annotation_type = getattr(annotation, "type", None)
        code = annotation_type[0] if isinstance(annotation_type, tuple) else None
        label = annotation_type[1] if isinstance(annotation_type, tuple) else ""
        if code == redact_type or str(label).strip().lower() == "redact":
            raise ValueError("page has a pre-existing Redact annotation")

    try:
        traces = page.get_texttrace()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("page text render-mode evidence is unavailable") from exc
    if not isinstance(traces, list):
        raise ValueError("page text render-mode evidence is invalid")
    unsupported = sorted(
        {
            int(trace.get("type"))
            for trace in traces
            if isinstance(trace, dict)
            and type(trace.get("type")) is int
            and int(trace["type"]) not in {0, 1, 2}
        }
    )
    if unsupported:
        raise ValueError(
            "page uses invisible or clipping text render mode(s): %s"
            % ", ".join(str(value) for value in unsupported)
        )


def _validated_text_only_redaction_actions() -> Tuple[int, int, int]:
    """Return the exact PyMuPDF actions required for text-only redaction."""

    required = (
        ("PDF_REDACT_IMAGE_NONE", 0),
        ("PDF_REDACT_LINE_ART_NONE", 0),
        ("PDF_REDACT_TEXT_REMOVE", 0),
    )
    actions: List[int] = []
    for name, expected in required:
        value = getattr(fitz, name, None)
        if type(value) is not int or value != expected:
            raise ValueError(
                "PyMuPDF text-only redaction action %s is unavailable or incompatible"
                % name
            )
        actions.append(value)
    return actions[0], actions[1], actions[2]


def _text_delta_complement_pixmap(
    original,
    redacted,
    *,
    page_rect,
    delivered_dpi: int,
    text_rects: List[Any],
) -> Tuple[Any, Dict[str, Any]]:
    """Keep exact original pixels except where verified text redaction changed them."""

    width = int(getattr(original, "width", 0) or 0)
    height = int(getattr(original, "height", 0) or 0)
    original_samples = bytes(getattr(original, "samples", b""))
    redacted_samples = bytes(getattr(redacted, "samples", b""))
    if (
        width <= 0
        or height <= 0
        or int(getattr(original, "n", 0) or 0) != 4
        or int(getattr(redacted, "n", 0) or 0) != 4
        or not bool(getattr(original, "alpha", False))
        or not bool(getattr(redacted, "alpha", False))
        or int(getattr(redacted, "width", 0) or 0) != width
        or int(getattr(redacted, "height", 0) or 0) != height
        or len(original_samples) != width * height * 4
        or len(redacted_samples) != len(original_samples)
    ):
        raise ValueError("page text suppression requires compatible RGBA pixmaps")
    if type(delivered_dpi) is not int or delivered_dpi < 72 or not text_rects:
        raise ValueError("page text suppression mask authority is invalid")

    zoom = delivered_dpi / 72.0
    margin_pixels = 2
    row_intervals: Dict[int, List[Tuple[int, int]]] = {}
    for value in text_rects:
        rect = fitz.Rect(value) & fitz.Rect(page_rect)
        if rect.is_empty or rect.width <= 0.0 or rect.height <= 0.0:
            raise ValueError("canonical text region is empty")
        x0 = max(0, int(math.floor((rect.x0 - page_rect.x0) * zoom)) - margin_pixels)
        y0 = max(0, int(math.floor((rect.y0 - page_rect.y0) * zoom)) - margin_pixels)
        x1 = min(width, int(math.ceil((rect.x1 - page_rect.x0) * zoom)) + margin_pixels)
        y1 = min(height, int(math.ceil((rect.y1 - page_rect.y0) * zoom)) + margin_pixels)
        if x1 <= x0 or y1 <= y0:
            raise ValueError("canonical text pixel region is empty")
        for row in range(y0, y1):
            row_intervals.setdefault(row, []).append((x0, x1))
    for row, intervals in row_intervals.items():
        merged: List[Tuple[int, int]] = []
        for start, end in sorted(intervals):
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        row_intervals[row] = merged

    output = bytearray(original_samples)
    changed = 0
    outside = 0
    for y_value in range(height):
        intervals = row_intervals.get(y_value, ())
        row_start = y_value * width * 4
        cursor = 0
        for start, end in intervals:
            gap_start = row_start + cursor * 4
            gap_end = row_start + start * 4
            if original_samples[gap_start:gap_end] != redacted_samples[gap_start:gap_end]:
                outside += sum(
                    1
                    for offset in range(gap_start, gap_end, 4)
                    if original_samples[offset : offset + 4]
                    != redacted_samples[offset : offset + 4]
                )
            for x_value in range(start, end):
                offset = row_start + x_value * 4
                if (
                    original_samples[offset : offset + 4]
                    == redacted_samples[offset : offset + 4]
                ):
                    continue
                output[offset : offset + 4] = b"\x00\x00\x00\x00"
                changed += 1
            cursor = end
        gap_start = row_start + cursor * 4
        gap_end = row_start + width * 4
        if original_samples[gap_start:gap_end] != redacted_samples[gap_start:gap_end]:
            outside += sum(
                1
                for offset in range(gap_start, gap_end, 4)
                if original_samples[offset : offset + 4]
                != redacted_samples[offset : offset + 4]
            )
    if changed <= 0:
        raise ValueError("canonical page text changed no rendered pixels")
    if outside:
        raise ValueError(
            "text-only redaction changed %d pixel(s) outside canonical text regions"
            % outside
        )
    background = fitz.Pixmap(fitz.csRGB, width, height, bytes(output), True)
    background_samples = bytes(background.samples)
    if background_samples != bytes(output):
        raise ValueError("text-suppressed page background samples changed")
    return background, {
        "original_samples_sha256": hashlib.sha256(original_samples).hexdigest(),
        "redacted_samples_sha256": hashlib.sha256(redacted_samples).hexdigest(),
        "background_samples_sha256": hashlib.sha256(background_samples).hexdigest(),
        "changed_pixel_count": changed,
        "unchanged_pixel_count": width * height - changed,
        "changed_pixels_outside_text_regions": outside,
        "changed_pixel_region_margin_pixels": margin_pixels,
        "changed_pixels_within_text_regions": True,
        "unchanged_pixels_preserved": True,
        "changed_pixels_transparent": True,
    }


def _render_text_suppressed_page_background(
    page,
    opts: ImportOptions,
    *,
    page_number: int,
    delivered_dpi: int,
) -> Tuple[Any, Dict[str, Any]]:
    """Render one exact Raster page background with canonical text paint punched out."""

    if type(page_number) is not int or page_number <= 0:
        raise ValueError("page number is invalid")
    if type(delivered_dpi) is not int or delivered_dpi < 72:
        raise ValueError("delivered page Raster DPI is invalid")
    observation = _validated_live_source_page(
        page,
        opts,
        page_number,
        document=getattr(page, "parent", None),
    )
    raw = page.get_text("rawdict", sort=False)
    source_items = list(
        _iter_text_source_items(
            raw,
            page_number,
            str(getattr(opts, "_pdf_sha256", "") or ""),
            str(getattr(opts, "text_mode", "") or ""),
        )
    )
    printable_count = _rawdict_printable_character_count(raw)
    item_printable_count = sum(
        sum(1 for character in str(item.get("text") or "") if not character.isspace())
        for item in source_items
    )
    if not source_items and printable_count == 0 and item_printable_count == 0:
        matrix = fitz.Matrix(delivered_dpi / 72.0, delivered_dpi / 72.0)
        original = page.get_pixmap(matrix=matrix, alpha=True)
        samples = bytes(getattr(original, "samples", b""))
        if (
            not samples
            or int(getattr(original, "n", 0) or 0) != 4
            or not bool(getattr(original, "alpha", False))
        ):
            raise ValueError("zero-text page did not render as exact RGBA")
        nontext_digest = _page_manifest_digest(_page_nontext_bbox_manifest(page))
        drawing_digest = _page_manifest_digest(_page_drawing_manifest(page))
        image_digest = _page_manifest_digest(_page_image_manifest(page))
        annotation_digest = _page_manifest_digest(_page_annotation_manifest(page))
        samples_digest = hashlib.sha256(samples).hexdigest()
        evidence = {
            "schema": "bcs.freecad_page_text_suppression/1.0",
            "verified": True,
            "pdf_sha256": str(getattr(opts, "_pdf_sha256", "") or ""),
            "page_number": page_number,
            "page_visual_scope_id": "p%d:page" % page_number,
            "page_visual_observation_sha256": observation["observation_sha256"],
            "text_suppression_method": "none_no_canonical_text",
            "redaction_method": "none_no_canonical_text",
            "raster_content_variant": "original_page_no_canonical_text",
            "source_text_item_ids": [],
            "source_text_item_ids_sha256": _page_manifest_digest([]),
            "source_text_item_count": 0,
            "source_printable_character_count": 0,
            "redaction_quad_count": 0,
            "redaction_quads_sha256": _page_manifest_digest([]),
            "rotated_text_item_count": 0,
            "remaining_printable_character_count": 0,
            "nontext_paint_before_sha256": nontext_digest,
            "nontext_paint_after_sha256": nontext_digest,
            "drawings_before_sha256": drawing_digest,
            "drawings_after_sha256": drawing_digest,
            "images_before_sha256": image_digest,
            "images_after_sha256": image_digest,
            "annotations_before_sha256": annotation_digest,
            "annotations_after_sha256": annotation_digest,
            "graphics_preserved": True,
            "images_preserved": True,
            "annotations_preserved": True,
            "nontext_paint_preserved": True,
            "underlay_structure_preserved": True,
            "later_nontext_occlusion_count": 0,
            "later_nontext_occlusion_verified": True,
            "delivered_dpi": delivered_dpi,
            "original_samples_sha256": samples_digest,
            "redacted_samples_sha256": samples_digest,
            "background_samples_sha256": samples_digest,
            "changed_pixel_count": 0,
            "unchanged_pixel_count": int(original.width) * int(original.height),
            "changed_pixels_outside_text_regions": 0,
            "changed_pixel_region_margin_pixels": 0,
            "changed_pixels_within_text_regions": True,
            "unchanged_pixels_preserved": True,
            "changed_pixels_transparent": True,
        }
        evidence["evidence_sha256"] = _page_text_suppression_evidence_digest(evidence)
        if not evidence["evidence_sha256"]:
            raise ValueError("zero-text page evidence is not canonical")
        return original, evidence
    if not source_items or printable_count <= 0 or item_printable_count != printable_count:
        raise ValueError("canonical page text regions are incomplete or ambiguous")
    _reject_unsafe_page_text_suppression_state(page)

    text_quads: List[Any] = []
    text_rects: List[Any] = []
    source_item_ids: List[str] = []
    rotated_count = 0
    for item in source_items:
        block = raw["blocks"][item["block_index"]]
        line = block["lines"][item["line_index"]]
        span = line["spans"][item["span_index"]]
        quad = fitz.recover_quad(line["dir"], span)
        rect = quad.rect & page.rect
        source_item_id = item.get("source_item_id")
        if (
            not isinstance(source_item_id, str)
            or not source_item_id
            or rect.is_empty
            or rect.width <= 0.0
            or rect.height <= 0.0
        ):
            raise ValueError("canonical page text region is invalid")
        text_quads.append(quad)
        text_rects.append(rect)
        source_item_ids.append(source_item_id)
        if abs(float(item.get("rotation_deg") or 0.0)) > 1e-7:
            rotated_count += 1

    before_nontext_digest = _page_manifest_digest(_page_nontext_bbox_manifest(page))
    before_drawing_digest = _page_manifest_digest(_page_drawing_manifest(page))
    before_image_digest = _page_manifest_digest(_page_image_manifest(page))
    before_annotation_digest = _page_manifest_digest(_page_annotation_manifest(page))
    later_occlusions = _later_nontext_occlusions(page, text_rects)
    if later_occlusions:
        raise ValueError(
            "later non-text paint occludes %d canonical text region(s)"
            % len({item["text_region_index"] for item in later_occlusions})
        )
    matrix = fitz.Matrix(delivered_dpi / 72.0, delivered_dpi / 72.0)
    original = page.get_pixmap(matrix=matrix, alpha=True)
    for quad in text_quads:
        page.add_redact_annot(quad, fill=False, cross_out=False)
    image_action, graphics_action, text_action = (
        _validated_text_only_redaction_actions()
    )
    applied = page.apply_redactions(
        images=image_action,
        graphics=graphics_action,
        text=text_action,
    )
    if applied is not True:
        raise ValueError("all-text page redaction was not applied")
    remaining_printable = _rawdict_printable_character_count(
        page.get_text("rawdict", sort=False)
    )
    if remaining_printable != 0:
        raise ValueError("canonical text remains after page text-only redaction")

    after_nontext_digest = _page_manifest_digest(_page_nontext_bbox_manifest(page))
    after_drawing_digest = _page_manifest_digest(_page_drawing_manifest(page))
    after_image_digest = _page_manifest_digest(_page_image_manifest(page))
    after_annotation_digest = _page_manifest_digest(_page_annotation_manifest(page))
    graphics_preserved = before_drawing_digest == after_drawing_digest
    images_preserved = before_image_digest == after_image_digest
    nontext_paint_preserved = before_nontext_digest == after_nontext_digest
    annotations_preserved = before_annotation_digest == after_annotation_digest
    if (
        not graphics_preserved
        or not images_preserved
        or not nontext_paint_preserved
        or not annotations_preserved
    ):
        raise ValueError("page text redaction changed non-text source content")
    redacted = page.get_pixmap(matrix=matrix, alpha=True)
    background, delta_evidence = _text_delta_complement_pixmap(
        original,
        redacted,
        page_rect=page.rect,
        delivered_dpi=delivered_dpi,
        text_rects=text_rects,
    )
    quad_points = [_quad_points(quad) for quad in text_quads]
    evidence = {
        "schema": "bcs.freecad_page_text_suppression/1.0",
        "verified": True,
        "pdf_sha256": str(getattr(opts, "_pdf_sha256", "") or ""),
        "page_number": page_number,
        "page_visual_scope_id": "p%d:page" % page_number,
        "page_visual_observation_sha256": observation["observation_sha256"],
        "text_suppression_method": (
            "exact_source_original_with_all_text_delta_transparent"
        ),
        "redaction_method": "exact_span_quads_text_only_redaction",
        "redaction_image_action": "PDF_REDACT_IMAGE_NONE",
        "redaction_graphics_action": "PDF_REDACT_LINE_ART_NONE",
        "redaction_text_action": "PDF_REDACT_TEXT_REMOVE",
        "raster_content_variant": "text_suppressed_page_background",
        "source_text_item_ids": source_item_ids,
        "source_text_item_ids_sha256": _page_manifest_digest(source_item_ids),
        "source_text_item_count": len(source_items),
        "source_printable_character_count": printable_count,
        "redaction_quad_count": len(text_quads),
        "redaction_quads_sha256": _page_manifest_digest(quad_points),
        "rotated_text_item_count": rotated_count,
        "remaining_printable_character_count": remaining_printable,
        "nontext_paint_before_sha256": before_nontext_digest,
        "nontext_paint_after_sha256": after_nontext_digest,
        "drawings_before_sha256": before_drawing_digest,
        "drawings_after_sha256": after_drawing_digest,
        "images_before_sha256": before_image_digest,
        "images_after_sha256": after_image_digest,
        "annotations_before_sha256": before_annotation_digest,
        "annotations_after_sha256": after_annotation_digest,
        "graphics_preserved": graphics_preserved,
        "images_preserved": images_preserved,
        "annotations_preserved": annotations_preserved,
        "nontext_paint_preserved": nontext_paint_preserved,
        "underlay_structure_preserved": True,
        "later_nontext_occlusion_count": 0,
        "later_nontext_occlusion_verified": True,
        "delivered_dpi": delivered_dpi,
        **delta_evidence,
    }
    evidence["evidence_sha256"] = _page_text_suppression_evidence_digest(evidence)
    if not evidence["evidence_sha256"]:
        raise ValueError("page text suppression evidence is not canonical")
    return background, evidence


def _rectangles_overlap_with_area(left, right) -> bool:
    overlap = fitz.Rect(left) & fitz.Rect(right)
    return bool(not overlap.is_empty and overlap.width > 1e-7 and overlap.height > 1e-7)


def _resolve_raster_dpi_for_rect(
    rect,
    opts: ImportOptions,
    *,
    requested_dpi: Optional[int] = None,
) -> Tuple[int, int, int, bool]:
    """Resolve requested/delivered DPI without silently weakening user policy."""

    requested = max(
        72,
        int(
            requested_dpi
            if requested_dpi is not None
            else (getattr(opts, "raster_dpi", 200) or 200)
        ),
    )
    budget = _raster_pixel_budget()
    area = max(1.0, float(rect.width) * float(rect.height))
    pixels = area * ((requested / 72.0) ** 2)
    delivered = requested
    if pixels > budget:
        delivered = max(
            72,
            min(requested, int(math.floor(math.sqrt(budget / area) * 72.0))),
        )
    degraded = delivered != requested
    if (
        degraded
        and bool(getattr(opts, "raster_dpi_user_set", False))
        and not bool(getattr(opts, "allow_raster_dpi_degradation", False))
    ):
        raise RuntimeError(
            "explicit raster DPI %d exceeds the %d pixel safety budget"
            % (requested, budget)
        )
    return requested, delivered, budget, degraded


def _transparent_pixmap_delta(original, redacted):
    """Keep original RGBA only where redaction changed a pixel."""

    width = int(getattr(original, "width", 0) or 0)
    height = int(getattr(original, "height", 0) or 0)
    original_samples = bytes(getattr(original, "samples", b""))
    redacted_samples = bytes(getattr(redacted, "samples", b""))
    if (
        width <= 0
        or height <= 0
        or int(getattr(original, "n", 0) or 0) != 4
        or int(getattr(redacted, "n", 0) or 0) != 4
        or not bool(getattr(original, "alpha", False))
        or not bool(getattr(redacted, "alpha", False))
        or int(getattr(redacted, "width", 0) or 0) != width
        or int(getattr(redacted, "height", 0) or 0) != height
        or len(original_samples) != width * height * 4
        or len(redacted_samples) != len(original_samples)
    ):
        raise ValueError("raster isolation samples are incompatible RGBA pixmaps")
    output = bytearray(len(original_samples))
    changed = 0
    unchanged = 0
    for offset in range(0, len(original_samples), 4):
        original_pixel = original_samples[offset : offset + 4]
        redacted_pixel = redacted_samples[offset : offset + 4]
        if original_pixel == redacted_pixel:
            unchanged += 1
            continue
        output[offset : offset + 4] = original_pixel
        changed += 1
    if changed <= 0:
        raise ValueError("redaction changed no source pixels")
    isolated = fitz.Pixmap(fitz.csRGB, width, height, bytes(output), True)
    isolated_samples = bytes(isolated.samples)
    if any(isolated_samples[offset + 3] != 0 for offset in range(0, len(output), 4) if output[offset : offset + 4] == b"\x00\x00\x00\x00"):
        raise ValueError("unchanged raster pixels are not transparent")
    return isolated, {
        "original_samples_sha256": hashlib.sha256(original_samples).hexdigest(),
        "redacted_samples_sha256": hashlib.sha256(redacted_samples).hexdigest(),
        "isolated_samples_sha256": hashlib.sha256(isolated_samples).hexdigest(),
        "changed_pixel_count": changed,
        "unchanged_pixel_count": unchanged,
        "unchanged_pixels_transparent": True,
        "transparent_alpha_preserved": True,
    }


def _exact_source_text_item_and_quad(
    page,
    item: Dict[str, Any],
) -> Tuple[Dict[str, Any], Any, int]:
    """Re-extract one canonical span and prove no other text intersects its quad."""

    raw = page.get_text("rawdict", sort=False)
    requested = item.get("requested_type")
    candidates = list(
        _iter_text_source_items(
            raw,
            int(item.get("page_number")),
            str(item.get("pdf_sha256")),
            str(requested),
        )
    )
    exact = next(
        (candidate for candidate in candidates if candidate["source_item_id"] == item.get("source_item_id")),
        None,
    )
    identity_fields = (
        "importer_identity",
        "pdf_sha256",
        "page_number",
        "source_item_id",
        "requested_type",
        "text",
        "font_identity",
        "bbox",
        "origin",
        "line_direction",
        "rotation_deg",
        "span",
        "block_index",
        "line_index",
        "span_index",
    )
    if exact is None or any(exact.get(field) != item.get(field) for field in identity_fields):
        raise ValueError("canonical source text item changed on the exact source pass")
    block = raw["blocks"][exact["block_index"]]
    line = block["lines"][exact["line_index"]]
    span = line["spans"][exact["span_index"]]
    quad = fitz.recover_quad(line["dir"], span)
    target_rect = quad.rect
    intersections = sum(
        1
        for candidate in candidates
        if candidate["source_item_id"] != exact["source_item_id"]
        and _rectangles_overlap_with_area(candidate["bbox"], target_rect)
    )
    return exact, quad, intersections


def _render_isolated_text_item_pixmap(
    item: Dict[str, Any],
    opts: ImportOptions,
) -> Tuple[Any, Any, Dict[str, Any]]:
    """Render only one exact source span using an original/redacted delta."""

    page_number = int(item["page_number"])
    observation = _validated_page_visual_observation(opts, page_number)
    with _open_pdf_source_attempt(opts) as source_document:
        source_page = source_document.load_page(page_number - 1)
        exact, quad, intersections = _exact_source_text_item_and_quad(source_page, item)
        if intersections:
            raise ValueError(
                "target text quad intersects %d other canonical text item(s)" % intersections
            )
        clip = quad.rect & source_page.rect
        if clip.is_empty or clip.width <= 0.0 or clip.height <= 0.0:
            raise ValueError("source item raster clip is empty")
        requested_dpi, delivered_dpi, pixel_budget, degraded = (
            _resolve_raster_dpi_for_rect(clip, opts)
        )
        matrix = fitz.Matrix(delivered_dpi / 72.0, delivered_dpi / 72.0)
        original = source_page.get_pixmap(matrix=matrix, clip=clip, alpha=True)
        source_page.add_redact_annot(quad, fill=False)
        applied = source_page.apply_redactions(images=0, graphics=0, text=0)
        if applied is not True:
            raise ValueError("exact target text redaction was not applied")
        redacted = source_page.get_pixmap(matrix=matrix, clip=clip, alpha=True)
        isolated, delta_evidence = _transparent_pixmap_delta(original, redacted)
    evidence = {
        "isolation_method": "exact_source_original_minus_target_redacted",
        "source_quad": _quad_points(quad),
        "source_clip": (float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1)),
        "source_line_direction": tuple(exact["line_direction"]),
        "source_rotation_deg": float(exact["rotation_deg"]),
        "other_text_intersection_count": intersections,
        "requested_dpi": requested_dpi,
        "delivered_dpi": delivered_dpi,
        "dpi": delivered_dpi,
        "dpi_degraded": degraded,
        "pixel_budget": pixel_budget,
        "page_visual_scope_id": "p%d:page" % page_number,
        "page_visual_observation_sha256": observation["observation_sha256"],
        "page_visual_session_anchor_sha256": getattr(
            opts, "_page_visual_session_anchor", {}
        ).get("session_anchor_sha256"),
        "source_page_observation_verified": True,
        **delta_evidence,
    }
    return isolated, clip, evidence


def _source_image_occurrences(page) -> List[Dict[str, Any]]:
    """Return one strict record per displayed image occurrence, never per xref."""

    raw_occurrences = page.get_image_info(xrefs=True)
    if not isinstance(raw_occurrences, list):
        raise ValueError("image occurrence extractor returned no list")
    occurrences: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_occurrences):
        if not isinstance(raw, dict):
            raise ValueError("image occurrence is not a dictionary")
        bbox = _finite_source_tuple(raw.get("bbox"), 4, "image.bbox")
        transform = _finite_source_tuple(raw.get("transform"), 6, "image.transform")
        width = raw.get("width")
        height = raw.get("height")
        xref = raw.get("xref", 0)
        number = raw.get("number")
        digest_value = raw.get("digest")
        if isinstance(digest_value, (bytes, bytearray, memoryview)):
            image_digest = bytes(digest_value).hex()
        elif isinstance(digest_value, str):
            image_digest = digest_value.strip().lower()
        else:
            image_digest = ""
        if (
            bbox[2] <= bbox[0]
            or bbox[3] <= bbox[1]
            or type(width) is not int
            or width <= 0
            or type(height) is not int
            or height <= 0
            or type(xref) is not int
            or xref < 0
            or type(number) is not int
            or not image_digest
        ):
            raise ValueError("displayed image occurrence identity is invalid")
        occurrences.append(
            {
                "source_occurrence_index": index,
                "source_number": number,
                "source_xref": xref,
                "source_bbox": bbox,
                "source_transform": transform,
                "source_width": width,
                "source_height": height,
                "source_colorspace": int(raw.get("colorspace", 0) or 0),
                "source_bpc": int(raw.get("bpc", 0) or 0),
                "source_xres": int(raw.get("xres", 0) or 0),
                "source_yres": int(raw.get("yres", 0) or 0),
                "source_encoded_size": int(raw.get("size", 0) or 0),
                "source_image_digest": image_digest,
                "source_has_mask": bool(raw.get("has-mask", False)),
            }
        )
    return occurrences


def _render_isolated_image_occurrence_pixmap(
    opts: ImportOptions,
    *,
    page_number: int,
    occurrence: Dict[str, Any],
) -> Tuple[Any, Any, Dict[str, Any]]:
    """Capture one image occurrence's exact page appearance as transparent RGBA."""

    observation = _validated_page_visual_observation(opts, page_number)
    occurrence_index = occurrence.get("source_occurrence_index")
    if type(occurrence_index) is not int or occurrence_index < 0:
        raise ValueError("source image occurrence index is invalid")
    with _open_pdf_source_attempt(opts) as source_document:
        page = source_document.load_page(page_number - 1)
        fresh_occurrences = _source_image_occurrences(page)
        if occurrence_index >= len(fresh_occurrences):
            raise ValueError("source image occurrence is absent on the exact pass")
        exact = fresh_occurrences[occurrence_index]
        if exact != occurrence:
            raise ValueError("source image occurrence changed on the exact pass")
        target_rect = fitz.Rect(exact["source_bbox"])
        overlapping = [
            candidate["source_occurrence_index"]
            for candidate in fresh_occurrences
            if candidate["source_occurrence_index"] != occurrence_index
            and _rectangles_overlap_with_area(candidate["source_bbox"], target_rect)
        ]
        if overlapping:
            raise ValueError(
                "image occurrence isolation is ambiguous with occurrences %s"
                % overlapping
            )
        clip = target_rect & page.rect
        if clip.is_empty or clip.width <= 0.0 or clip.height <= 0.0:
            raise ValueError("image occurrence clip is empty")
        requested_dpi, delivered_dpi, pixel_budget, degraded = (
            _resolve_raster_dpi_for_rect(clip, opts)
        )
        matrix = fitz.Matrix(delivered_dpi / 72.0, delivered_dpi / 72.0)
        original = page.get_pixmap(matrix=matrix, clip=clip, alpha=True)
        page.add_redact_annot(target_rect, fill=False)
        applied = page.apply_redactions(images=1, graphics=0, text=1)
        if applied is not True:
            raise ValueError("exact image occurrence redaction was not applied")
        redacted = page.get_pixmap(matrix=matrix, clip=clip, alpha=True)
        isolated, delta_evidence = _transparent_pixmap_delta(original, redacted)
    evidence = {
        **copy.deepcopy(occurrence),
        "isolation_method": "exact_source_original_minus_image_redacted",
        "source_clip": (float(clip.x0), float(clip.y0), float(clip.x1), float(clip.y1)),
        "overlapping_image_occurrence_indexes": [],
        "requested_dpi": requested_dpi,
        "delivered_dpi": delivered_dpi,
        "dpi": delivered_dpi,
        "dpi_degraded": degraded,
        "pixel_budget": pixel_budget,
        "page_visual_scope_id": "p%d:page" % page_number,
        "page_visual_observation_sha256": observation["observation_sha256"],
        "source_page_observation_verified": True,
        "ctm_preserved_by_page_render_delta": True,
        "clip_preserved_by_page_render_delta": True,
        "mask_alpha_opacity_preserved_by_page_render_delta": True,
        **delta_evidence,
    }
    return isolated, clip, evidence


def _bind_embedded_occurrence_host_evidence(
    host_obj,
    evidence: Dict[str, Any],
) -> str:
    """Persist the exact occurrence CTM/isolation binding on its ImagePlane."""

    canonical = {
        key: copy.deepcopy(evidence[key])
        for key in (
            "source_occurrence_index",
            "source_number",
            "source_xref",
            "source_bbox",
            "source_transform",
            "source_width",
            "source_height",
            "source_image_digest",
            "source_has_mask",
            "source_clip",
            "original_samples_sha256",
            "redacted_samples_sha256",
            "isolated_samples_sha256",
            "page_visual_observation_sha256",
        )
    }
    encoded = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    add_property = getattr(host_obj, "addProperty", None)
    if not callable(add_property):
        raise RuntimeError("ImagePlane cannot persist occurrence evidence")
    properties = set(getattr(host_obj, "PropertiesList", []) or [])
    for property_type, property_name in (
        ("App::PropertyInteger", "PDFImageOccurrenceIndex"),
        ("App::PropertyString", "PDFImageOccurrenceJSON"),
        ("App::PropertyString", "PDFImageOccurrenceEvidenceSHA256"),
        ("App::PropertyBool", "PDFImageSourceHasMask"),
    ):
        if property_name not in properties:
            add_property(property_type, property_name, "PDF Import")
            properties.add(property_name)
    host_obj.PDFImageOccurrenceIndex = int(evidence["source_occurrence_index"])
    host_obj.PDFImageOccurrenceJSON = encoded
    host_obj.PDFImageOccurrenceEvidenceSHA256 = digest
    host_obj.PDFImageSourceHasMask = bool(evidence["source_has_mask"])
    if (
        int(getattr(host_obj, "PDFImageOccurrenceIndex", -1))
        != evidence["source_occurrence_index"]
        or str(getattr(host_obj, "PDFImageOccurrenceJSON", "")) != encoded
        or str(getattr(host_obj, "PDFImageOccurrenceEvidenceSHA256", "")) != digest
        or bool(getattr(host_obj, "PDFImageSourceHasMask", False))
        is not bool(evidence["source_has_mask"])
    ):
        raise RuntimeError("embedded image occurrence host binding failed")
    return digest


def _import_embedded_image_occurrences(
    *,
    opts: ImportOptions,
    page_number: int,
    page_h: float,
    scale: float,
    fc_doc,
    image_group,
) -> Dict[str, Any]:
    """Import every displayed image occurrence once with full visual CTM fidelity."""

    observation = _validated_page_visual_observation(opts, int(page_number))
    with _open_pdf_source_attempt(opts) as source_document:
        source_page = source_document.load_page(int(page_number) - 1)
        occurrences = _source_image_occurrences(source_page)
    displayed_count = observation.get("images", {}).get("displayed_count")
    if type(displayed_count) is not int or displayed_count != len(occurrences):
        raise ImportLifecycleError("v2 image occurrence count does not match exact source")

    results: List[Dict[str, Any]] = []
    for occurrence in occurrences:
        _invoke_import_cancellation_checkpoint(opts, "embedded image occurrence")
        pix, clip, isolation_evidence = _render_isolated_image_occurrence_pixmap(
            opts,
            page_number=int(page_number),
            occurrence=occurrence,
        )
        image_path, asset_evidence = _persist_content_addressed_pixmap(
            pix,
            opts,
            asset_kind="embedded",
        )
        image_asset_evidence = {
            **asset_evidence,
            "pdf_sha256": str(opts._pdf_sha256),
            "page_number": int(page_number),
            "source_xref": int(occurrence["source_xref"]),
        }
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
        x_size = max(xs) - min(xs)
        y_size = max(ys) - min(ys)
        if x_size <= 0.0 or y_size <= 0.0:
            raise ImportLifecycleError("embedded image occurrence placement has no area")
        host_obj = None
        try:
            host_obj, host_asset_evidence = _create_bound_embedded_image_plane(
                fc_doc=fc_doc,
                image_group=image_group,
                image_path=image_path,
                image_asset_evidence=image_asset_evidence,
                page_number=int(page_number),
                xref=int(occurrence["source_xref"]),
                x_size=x_size,
                y_size=y_size,
                placement=Placement(
                    _v(min(xs), min(ys), 0),
                    Rotation(),
                ),
            )
            occurrence_evidence_sha256 = _bind_embedded_occurrence_host_evidence(
                host_obj,
                isolation_evidence,
            )
            actual_anchor = _host_anchor_xyz(host_obj)
            if (
                actual_anchor is None
                or abs(actual_anchor[2]) > 1e-7
                or not math.isclose(float(host_obj.XSize), x_size, abs_tol=1e-7)
                or not math.isclose(float(host_obj.YSize), y_size, abs_tol=1e-7)
            ):
                raise RuntimeError("embedded image occurrence placement is invalid")
        except BaseException:
            if host_obj is not None:
                _remove_owned_text_objects(fc_doc, image_group, [host_obj])
            raise
        results.append(
            {
                **isolation_evidence,
                **asset_evidence,
                **host_asset_evidence,
                "occurrence_evidence_sha256": occurrence_evidence_sha256,
                "entity_id": _host_object_id(host_obj),
                "x_size": float(x_size),
                "y_size": float(y_size),
                "anchor_xyz": tuple(actual_anchor),
                "raster_file_included": True,
                "raster_file_included_property_bound": True,
            }
        )
    return {
        "count": len(results),
        "occurrences": results,
        "page_visual_classification": observation.get("classification"),
        "image_only_single_occurrence_reused": bool(
            observation.get("classification") == "image_only" and len(results) == 1
        ),
    }


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
        source_text = bound_item.get("text")
        if (
            attempted_type != "raster"
            or requested_type not in TEXT_ITEM_FALLBACK_LADDERS
            or TEXT_ITEM_FALLBACK_LADDERS[requested_type][-1] != "raster"
            or bound_item.get("importer_identity") != FREECAD_TEXT_IMPORTER_IDENTITY
            or source_item_id != expected_source_id
            or not isinstance(pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
            or not isinstance(source_text, str)
            or not source_text
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
        ladder_context = _validated_raster_ladder_context(bound_item, attempted_type)
    except ValueError as exc:
        fail(
            "invalid_raster_ladder_prefix",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        _validated_live_source_page(page, opts, int(page_number))
    except ValueError as exc:
        fail(
            "raster_source_page_identity_mismatch",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )

    try:
        source_ink_evidence = _validated_source_ink_evidence(bound_item)
    except ValueError as exc:
        fail(
            "source_ink_authority_missing",
            {
                "required_authority": "physical_pdf_character_evidence",
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )

    try:
        pix, clip, isolation_evidence = _render_isolated_text_item_pixmap(
            bound_item,
            opts,
        )
        image_path, asset_evidence = _persist_content_addressed_pixmap(
            pix,
            opts,
            asset_kind="text",
        )
        raster_sha256 = asset_evidence["source_asset_sha256"]
    except ValueError as exc:
        if "intersects" in str(exc) or "canonical source text item changed" in str(exc):
            fail(
                "raster_item_isolation_unproven",
                {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
            )
        fail(
            "raster_text_render_failed",
            {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
        )
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
        expected_anchor = (min(xs), min(ys), 0.0)
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
        _persist_source_ink_evidence(host_obj, source_ink_evidence)
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
            or getattr(host_obj, "PDFSourceInkClassification", None)
            != source_ink_evidence["classification"]
            or getattr(host_obj, "PDFSourceInkEvidenceSHA256", None)
            != source_ink_evidence["evidence_sha256"]
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
            "raster_file_included_property_bound": True,
            "pdf_sha256": pdf_sha256,
            "pixel_width": int(pix.width),
            "pixel_height": int(pix.height),
            "dpi": int(isolation_evidence["delivered_dpi"]),
            "source_bbox": bbox,
            "expected_anchor_xyz": expected_anchor,
            "verified_anchor_xyz": tuple(actual_anchor),
            "x_size": float(expected_width),
            "y_size": float(expected_height),
            "source_text": source_text,
            "source_ink_evidence": copy.deepcopy(source_ink_evidence),
            "source_ink_evidence_persisted": True,
            "raster_ladder_context": copy.deepcopy(ladder_context),
            **isolation_evidence,
            **_source_ink_delivery_binding_fields(bound_item, source_ink_evidence),
            **asset_evidence,
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
    latest_current_objects: Optional[List[Any]] = None
    source_ink_evidence: Optional[Dict[str, Any]] = None

    raw_source_ink = bound_item.get("source_ink_evidence")
    if (
        bound_item.get("parent_source_item_id") is None
        and isinstance(raw_source_ink, dict)
        and raw_source_ink.get("classification")
        == "mixed_visible_and_zero_ink"
    ):
        return _deliver_mixed_source_ink_segments(
            bound_item,
            attempted_type,
            opts,
            deliver_child=lambda child: _deliver_text_item_3d(
                child,
                attempted_type,
                opts,
                text_group=text_group,
                page_h=page_h,
                scale=scale,
            ),
            text_group=text_group,
        )

    def add_owned(obj) -> None:
        if obj is not None and baseline_valid and id(obj) in baseline_objects:
            raise RuntimeError("host factory returned a pre-existing baseline object")
        if obj is not None and not any(candidate is obj for candidate in owned):
            owned.append(obj)

    def collect_owned() -> Optional[str]:
        nonlocal ownership_collection_failed, ownership_collection_error
        nonlocal latest_current_objects
        if doc is None:
            return None
        if not baseline_valid:
            return "baseline object snapshot is unavailable"
        if ownership_collection_failed:
            return ownership_collection_error or "prior ownership collection failed"
        try:
            current_objects = list(getattr(doc, "Objects", []) or [])
            latest_current_objects = current_objects
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
            or not _canonical_or_segment_source_id_matches(bound_item)
            or not isinstance(pdf_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", pdf_sha256) is None
            or not isinstance(source_text, str)
            or not source_text
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
        source_ink_evidence = _validated_source_ink_evidence(bound_item)
    except ValueError as exc:
        terminal_failure(
            "source_ink_authority_missing",
            {
                "source_text_sha256": hashlib.sha256(
                    source_text.encode("utf-8")
                ).hexdigest(),
                "required_authority": "physical_pdf_character_evidence",
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )
    if _mixed_source_ink_requires_fallback(source_ink_evidence):
        _raise_mixed_source_ink_impossible(bound_item, attempted_type)

    if source_ink_evidence["classification"] == "zero_visible_ink":
        try:
            if (
                Part is None
                or not callable(getattr(Part, "Shape", None))
                or Vector is None
                or Placement is None
                or Rotation is None
                or text_group is None
            ):
                raise RuntimeError("FreeCAD empty 3D Text host is unavailable")
            doc = _text_host_document(None, text_group)
            if doc is None:
                raise RuntimeError("FreeCAD 3D Text document is unavailable")
            baseline_objects = {
                id(host_obj) for host_obj in list(getattr(doc, "Objects", []) or [])
            }
            baseline_valid = True

            from pdfcadcore.text_scale import effective_span_font_size_pt

            page_height = float(page_h)
            item_scale = float(scale)
            host_rotation_deg = _line_angle_deg({"dir": line_direction}, opts)
            size_pt = float(effective_span_font_size_pt(span, host_rotation_deg))
            font_size_fc = _fit_font_size_to_span_bbox(
                source_text,
                size_pt * item_scale,
                span,
                item_scale,
                host_rotation_deg,
            )
            if (
                not math.isfinite(page_height)
                or not math.isfinite(item_scale)
                or item_scale <= 0.0
                or not math.isfinite(host_rotation_deg)
                or not math.isfinite(font_size_fc)
                or font_size_fc <= 0.0
            ):
                raise ValueError("zero-ink 3D Text transform is invalid")
            pos = _to_fc(origin, page_height, opts, item_scale)
            placement = Placement(
                pos,
                Rotation(Vector(0.0, 0.0, 1.0), host_rotation_deg),
            )
            source_color = _span_source_color(span)
            normalized_font = _normalize_pdf_font_name(source_font)

            creation_started = True
            host_obj = doc.addObject("Part::Feature", "PDF_Empty_3D_Text")
            if host_obj is None:
                raise RuntimeError("empty 3D Text factory returned no object")
            add_owned(host_obj)
            host_obj.Shape = Part.Shape()
            properties = set(getattr(host_obj, "PropertiesList", []) or [])
            add_property = getattr(host_obj, "addProperty", None)
            if "String" not in properties:
                if not callable(add_property):
                    raise RuntimeError("empty 3D Text cannot persist source text")
                add_property("App::PropertyString", "String", "PDF Import")
            host_obj.String = source_text
            host_obj.Placement = placement
            _annotate_text_host_object(
                host_obj,
                source_item_id,
                "3d_text",
                parent_source_item_id=bound_item.get("parent_source_item_id"),
            )
            _persist_text_style_metadata(
                host_obj,
                font_name=normalized_font,
                font_size=font_size_fc,
                source_color=source_color,
                visibility=False,
            )
            _persist_source_ink_evidence(host_obj, source_ink_evidence)
            text_group.addObject(host_obj)
            doc.recompute()
            collection_error = collect_owned()
            if collection_error:
                raise RuntimeError(
                    "owned object collection failed: %s" % collection_error
                )

            created_ids, ids_complete = owned_ids()
            entity_id = _host_object_id(host_obj)
            shape_evidence = _zero_visible_ink_shape_evidence(
                getattr(host_obj, "Shape", None)
            )
            if (
                not ids_complete
                or created_ids != [entity_id]
                or doc.getObject(entity_id) is not host_obj
                or str(getattr(host_obj, "TypeId", "") or "") != "Part::Feature"
                or getattr(host_obj, "String", None) != source_text
                or getattr(host_obj, "PDFSourceItemId", None) != source_item_id
                or getattr(host_obj, "PDFParentSourceItemId", None)
                != bound_item.get("parent_source_item_id")
                or getattr(host_obj, "PDFRepresentation", None) != "3d_text"
                or getattr(host_obj, "PDFTextVisibility", None) is not False
                or getattr(host_obj, "PDFSourceInkClassification", None)
                != "zero_visible_ink"
                or getattr(host_obj, "PDFSourceInkEvidenceSHA256", None)
                != source_ink_evidence["evidence_sha256"]
                or shape_evidence.get("zero_visible_ink_verified") is not True
            ):
                raise RuntimeError("empty 3D Text host evidence could not be verified")
        except Exception as exc:
            terminal_failure(
                "zero_ink_3d_text_delivery_failed",
                {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
            )

        evidence = {
            "source_text": source_text,
            "source_text_preserved": True,
            "source_item_id": source_item_id,
            "source_item_id_verified": True,
            "host_entity_type": "Part::Feature",
            "physical_visibility": False,
            "source_ink_evidence": copy.deepcopy(source_ink_evidence),
            "source_ink_evidence_persisted": True,
            **_source_ink_delivery_binding_fields(bound_item, source_ink_evidence),
            **shape_evidence,
        }
        return {
            "source_item_id": source_item_id,
            "requested_type": requested_type,
            "attempted_type": "3d_text",
            "final_type": "3d_text",
            "outcome": "verified",
            "created_entity_ids": [entity_id],
            "delivery_entity_ids": [entity_id],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 1,
            "evidence": evidence,
        }

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

    font_delivery_evidence: Optional[Dict[str, Any]] = None
    found_results = [
        result for result in source_results if result.get("outcome") == "found"
    ]
    if (
        any(
            result.get("outcome") not in {"not_found", "found", "invalid"}
            for result in source_results
        )
        or len(found_results) > 1
        or (
            found_results
            and (
                not isinstance(font_path, str)
                or not font_path
                or font_path != font_path.strip()
                or source_results[-1] is not found_results[0]
                or found_results[0].get("path") != font_path
            )
        )
        or (not found_results and font_path is not None)
    ):
        terminal_failure(
            "exact_font_resolution_invalid",
            {"attempted_source_results": source_results},
        )

    exact_rejection: Optional[Dict[str, Any]] = None
    font_source_result: Dict[str, Any]
    if found_results:
        font_source_result = copy.deepcopy(found_results[0])
        try:
            approved_root_value = font_source_result.get("approved_font_root")
            if (
                not isinstance(approved_root_value, str)
                or not approved_root_value
                or not Path(approved_root_value).is_absolute()
            ):
                raise RuntimeError("exact font approved root is missing")
            exact_path, exact_font_bytes, exact_sha256 = _read_stable_font_asset(
                font_path,
                approved_root=approved_root_value,
            )
            declared_sha256 = str(font_source_result.get("sha256") or "")
            if (
                re.fullmatch(r"[0-9a-f]{64}", declared_sha256) is None
                or declared_sha256 != exact_sha256
            ):
                raise RuntimeError("exact font digest does not match source evidence")
            internal_identity = _font_internal_identity_evidence(
                exact_font_bytes,
                font_identity,
            )
            exact_source = str(font_source_result.get("source") or "")
            identity_verified = bool(
                internal_identity.get("font_identity_verified") is True
            )
            if not identity_verified:
                raise RuntimeError("exact font internal identity is not verified")
            exact_coverage = _font_bytes_glyph_coverage(
                exact_font_bytes,
                source_text,
            )
            if exact_coverage.get("glyph_coverage_verified") is not True:
                exact_rejection = {
                    "reason": "exact_font_glyph_coverage_invalid",
                    "path": str(exact_path),
                    "sha256": exact_sha256,
                    "glyph_coverage": exact_coverage,
                }
            else:
                font_path = str(exact_path)
                font_delivery_evidence = {
                    "source_font_equivalence": True,
                    "font_substitution_applied": False,
                    "font_identity_verified": True,
                    **copy.deepcopy(exact_coverage),
                    "font_candidate_source": exact_source,
                    "source_font_asset_path": str(exact_path),
                    "source_font_asset_sha256": exact_sha256,
                    "delivered_font_sha256": exact_sha256,
                    "font_internal_identity_evidence": internal_identity,
                    "font_internal_identity_sha256": internal_identity[
                        "evidence_sha256"
                    ],
                    "font_coverage_evidence_sha256": exact_coverage[
                        "coverage_evidence_sha256"
                    ],
                    "font_coverage_evidence": copy.deepcopy(exact_coverage),
                    "_selected_font_bytes": exact_font_bytes,
                    "_selected_font_path": str(exact_path),
                    "_selected_font_approved_root": str(
                        Path(approved_root_value).resolve(strict=True)
                    ),
                    "path": str(exact_path),
                }
        except Exception as exc:
            exact_rejection = {
                "reason": "exact_font_asset_invalid",
                "path": str(font_path or ""),
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            }
    elif any(result.get("outcome") == "invalid" for result in source_results):
        exact_rejection = {
            "reason": "exact_font_source_invalid",
            "attempted_source_results": copy.deepcopy(source_results),
        }

    if font_delivery_evidence is None:
        exact_path_to_exclude = (
            str(font_path)
            if isinstance(font_path, str) and font_path
            else ""
        )
        substitute_path, substitute_evidence = _select_no_cost_font_substitute(
            source_text,
            exclude_paths=[exact_path_to_exclude] if exact_path_to_exclude else None,
        )
        if (
            substitute_path is None
            or substitute_evidence.get("glyph_coverage_verified") is not True
        ):
            terminal_failure(
                "no_cost_font_substitute_unavailable",
                {
                    "attempted_source_results": source_results,
                    "exact_font_rejection": exact_rejection,
                    "font_substitution_evidence": substitute_evidence,
                },
            )
        try:
            candidate_path_value = substitute_evidence.get("_selected_font_path")
            candidate_bytes = substitute_evidence.get("_selected_font_bytes")
            approved_root_value = substitute_evidence.get(
                "_selected_font_approved_root"
            )
            if (
                not isinstance(candidate_path_value, str)
                or not candidate_path_value
                or not isinstance(candidate_bytes, bytes)
                or not candidate_bytes
                or not isinstance(approved_root_value, str)
                or not approved_root_value
            ):
                raise RuntimeError("substitute immutable selection is incomplete")
            candidate_path = Path(candidate_path_value)
            if (
                str(candidate_path) != str(substitute_path)
                or not candidate_path.is_absolute()
                or not Path(approved_root_value).is_absolute()
                or not _path_is_within(candidate_path, Path(approved_root_value))
            ):
                raise RuntimeError("substitute path/root selection is invalid")
            candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
            if candidate_sha256 != substitute_evidence.get(
                "source_font_asset_sha256"
            ):
                raise RuntimeError("substitute font changed after bounded selection")
            internal_identity = _font_internal_identity_evidence(
                candidate_bytes,
                font_identity,
            )
            coverage_sha256 = substitute_evidence.get(
                "coverage_evidence_sha256"
            )
            coverage_evidence = substitute_evidence.get("font_coverage_evidence")
            if (
                re.fullmatch(r"[0-9a-f]{64}", str(coverage_sha256 or "")) is None
                or not isinstance(coverage_evidence, dict)
                or coverage_evidence.get("coverage_evidence_sha256")
                != coverage_sha256
                or _font_evidence_digest(coverage_evidence) != coverage_sha256
            ):
                raise RuntimeError("substitute glyph coverage evidence is incomplete")
        except Exception as exc:
            terminal_failure(
                "no_cost_font_substitute_invalid",
                {
                    "attempted_source_results": source_results,
                    "exact_font_rejection": exact_rejection,
                    "font_substitution_evidence": substitute_evidence,
                    "exception": "%s: %s" % (exc.__class__.__name__, exc),
                },
            )
        font_path = str(candidate_path)
        font_delivery_evidence = {
            **copy.deepcopy(substitute_evidence),
            "source_font_equivalence": False,
            "font_substitution_applied": True,
            "font_identity_verified": False,
            "source_font_asset_path": str(candidate_path),
            "source_font_asset_sha256": candidate_sha256,
            "delivered_font_sha256": candidate_sha256,
            "font_internal_identity_evidence": internal_identity,
            "font_internal_identity_sha256": internal_identity["evidence_sha256"],
            "font_coverage_evidence": copy.deepcopy(coverage_evidence),
            "font_coverage_evidence_sha256": coverage_sha256,
            "exact_font_rejection": exact_rejection,
            "path": str(candidate_path),
        }
        font_source_result = {
            "source": font_delivery_evidence["font_candidate_source"],
            "outcome": "found",
            "font_identity": dict(font_identity),
            "path": str(candidate_path),
            "sha256": candidate_sha256,
            "pdf_sha256": pdf_sha256,
            "page_number": page_number,
            "staging_complete": True,
        }

    try:
        font_path, font_delivery_evidence = _stage_shapestring_font_asset(
            font_path,
            font_delivery_evidence,
            opts,
        )
        if not _verify_staged_shapestring_font_asset(font_delivery_evidence):
            raise RuntimeError("staged font failed pre-ShapeString digest verification")
    except Exception as exc:
        terminal_failure(
            "font_asset_staging_failed",
            {
                "attempted_source_results": source_results,
                "exact_font_rejection": exact_rejection,
                "font_delivery_evidence": font_delivery_evidence,
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )

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
    except Exception as exc:
        terminal_failure(
            "shapestring_creation_failed",
            {
                "attempted_source_results": source_results,
                "font_path": font_path,
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )
    if not _verify_staged_shapestring_font_asset(font_delivery_evidence):
        terminal_failure(
            "staged_font_digest_changed",
            {
                "attempted_source_results": source_results,
                "font_delivery_evidence": font_delivery_evidence,
                "verification_point": "after_shapestring_creation",
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
            _annotate_text_host_object(
                host_obj,
                source_item_id,
                "3d_text",
                parent_source_item_id=bound_item.get("parent_source_item_id"),
            )
            _persist_source_ink_evidence(host_obj, source_ink_evidence)
            stage = "source_color"
            _persist_text_style_metadata(
                host_obj,
                font_name=normalized_font,
                font_size=font_size_fc,
                source_color=source_color,
            )
            if font_delivery_evidence is not None:
                _persist_font_delivery_metadata(
                    host_obj,
                    source_font_equivalence=font_delivery_evidence[
                        "source_font_equivalence"
                    ],
                    font_substitution_applied=font_delivery_evidence[
                        "font_substitution_applied"
                    ],
                    font_identity_verified=font_delivery_evidence[
                        "font_identity_verified"
                    ],
                    glyph_coverage_verified=font_delivery_evidence[
                        "glyph_coverage_verified"
                    ],
                    delivered_font_sha256=font_delivery_evidence[
                        "delivered_font_sha256"
                    ],
                    font_candidate_source=font_delivery_evidence[
                        "font_candidate_source"
                    ],
                    delivered_font_path=font_delivery_evidence["staged_font_path"],
                    source_font_asset_path=font_delivery_evidence[
                        "source_font_asset_path"
                    ],
                    source_font_asset_sha256=font_delivery_evidence[
                        "source_font_asset_sha256"
                    ],
                    staged_font_path=font_delivery_evidence["staged_font_path"],
                    staged_font_sha256=font_delivery_evidence["staged_font_sha256"],
                    staged_asset_verified=font_delivery_evidence[
                        "staged_asset_verified"
                    ],
                    staged_asset_read_only=font_delivery_evidence[
                        "staged_asset_read_only"
                    ],
                    font_internal_identity_sha256=font_delivery_evidence[
                        "font_internal_identity_sha256"
                    ],
                    font_coverage_evidence_sha256=font_delivery_evidence[
                        "font_coverage_evidence_sha256"
                    ],
                    source_font_identity=font_identity,
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
            and getattr(host_obj, "PDFParentSourceItemId", None)
            == bound_item.get("parent_source_item_id")
            and getattr(host_obj, "PDFRepresentation", None) == "3d_text"
            for host_obj in required_objects
        )
        source_ink_evidence_persisted = all(
            getattr(host_obj, "PDFSourceInkClassification", None)
            == source_ink_evidence["classification"]
            and getattr(host_obj, "PDFSourceInkEvidenceSHA256", None)
            == source_ink_evidence["evidence_sha256"]
            for host_obj in required_objects
        )
        font_delivery_metadata_persisted = all(
            _host_font_delivery_metadata_matches(
                host_obj,
                font_delivery_evidence,
                font_identity,
            )
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
        if latest_current_objects is None:
            raise RuntimeError("owned object collection did not retain its live snapshot")
        live_objects = latest_current_objects
        if (
            not ids_complete
            or not created_ids
            or any(not entity_id or entity_id not in created_ids for entity_id in required_ids)
            or any(not any(candidate is host_obj for candidate in live_objects) for host_obj in owned)
            or not source_text_preserved
            or not source_item_id_verified
            or not source_ink_evidence_persisted
            or not font_delivery_metadata_persisted
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

    if not _verify_staged_shapestring_font_asset(font_delivery_evidence):
        terminal_failure(
            "staged_font_digest_changed",
            {
                "attempted_source_results": source_results,
                "font_delivery_evidence": font_delivery_evidence,
                "verification_point": "after_host_persistence",
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
        "delivery_count": 1,
        "evidence": {
            "source_text": source_text,
            "source_text_preserved": True,
            "source_item_id": source_item_id,
            "source_item_id_verified": True,
            "source_ink_evidence": copy.deepcopy(source_ink_evidence),
            "source_ink_evidence_persisted": True,
            **_source_ink_delivery_binding_fields(bound_item, source_ink_evidence),
            "entity_type": str(getattr(extrusion, "TypeId", "") or ""),
            "solid_count": len(solids),
            "volume": volume,
            "rotation_deg": float(verified_rotation_deg),
            "verified_anchor_xyz": tuple(verified_anchor_xyz),
            "target_advance": float(target_advance_fc),
            "verified_advance": float(verified_advance_fc),
            "font_path": font_path,
            "font_source_result": font_source_result,
            **(copy.deepcopy(font_delivery_evidence) if font_delivery_evidence else {}),
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
    """Render exact non-empty spans, including all-space text, as native 3D Text.

    This compatibility helper is not a production page-import entrypoint; the
    canonical item ladder owns production delivery and physical-ink evidence.
    """
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
                if not source_text:
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
        fail_attempt(
            attempt,
            "no exact source text items",
            {"item_specific_attempted": True},
        )

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
    source_ink_evidence: Optional[Dict[str, Any]] = None

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
        source_ink_evidence = _validated_source_ink_evidence(bound_item)
    except ValueError as exc:
        raise_attempt(
            "source_ink_authority_missing",
            {
                "source_text_sha256": hashlib.sha256(
                    source_text.encode("utf-8")
                ).hexdigest(),
                "required_authority": "physical_pdf_character_evidence",
                "exception": "%s: %s" % (exc.__class__.__name__, exc),
            },
        )
    if _mixed_source_ink_requires_fallback(source_ink_evidence):
        _raise_mixed_source_ink_impossible(bound_item, attempted_type)

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

    if (
        source_ink_evidence is not None
        and source_ink_evidence["classification"] == "zero_visible_ink"
    ):
        try:
            if Part is None or not callable(getattr(Part, "Shape", None)):
                raise RuntimeError("FreeCAD empty Part shape factory is unavailable")
            host_obj = fc_doc.addObject(
                "Part::Feature",
                "PDF_Empty_Glyph" if attempted_type == "glyphs" else "PDF_Empty_Geometry",
            )
            if host_obj is None or id(host_obj) in baseline_object_ids:
                raise RuntimeError("zero-ink host factory returned no new object")
            host_obj.Shape = Part.Shape()
            _annotate_text_host_object(host_obj, source_item_id, attempted_type)
            _persist_source_ink_evidence(host_obj, source_ink_evidence)
            properties = set(getattr(host_obj, "PropertiesList", []) or [])
            add_property = getattr(host_obj, "addProperty", None)
            if "PDFSourceText" not in properties and callable(add_property):
                add_property("App::PropertyString", "PDFSourceText", "PDF Import")
            host_obj.PDFSourceText = source_text
            parent_group.addObject(host_obj)
            fc_doc.recompute()

            entity_id = _host_object_id(host_obj)
            live_objects = list(getattr(fc_doc, "Objects", []) or [])
            new_objects = [
                candidate
                for candidate in live_objects
                if id(candidate) not in baseline_object_ids
            ]
            shape_evidence = _zero_visible_ink_shape_evidence(
                getattr(host_obj, "Shape", None)
            )
            if (
                new_objects != [host_obj]
                or not entity_id
                or fc_doc.getObject(entity_id) is not host_obj
                or str(getattr(host_obj, "TypeId", "") or "") != "Part::Feature"
                or getattr(host_obj, "PDFSourceItemId", None) != source_item_id
                or getattr(host_obj, "PDFRepresentation", None) != attempted_type
                or getattr(host_obj, "PDFSourceText", None) != source_text
                or getattr(host_obj, "PDFSourceInkClassification", None)
                != "zero_visible_ink"
                or getattr(host_obj, "PDFSourceInkEvidenceSHA256", None)
                != source_ink_evidence["evidence_sha256"]
            ):
                raise RuntimeError(
                    "zero-ink requested representation could not be verified"
                )
        except Exception as exc:
            fail_after_render(
                "zero_ink_requested_representation_failed",
                {"exception": "%s: %s" % (exc.__class__.__name__, exc)},
            )
        evidence = {
            "source_text": source_text,
            "source_text_preserved": True,
            "source_item_id": source_item_id,
            "source_item_id_verified": True,
            "host_entity_type": "Part::Feature",
            "source_ink_evidence": copy.deepcopy(source_ink_evidence),
            "source_ink_evidence_persisted": True,
            "raw_edge_count": 0,
            "glyph_count": 0,
            **shape_evidence,
        }
        evidence.update(
            _source_ink_delivery_binding_fields(bound_item, source_ink_evidence)
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
            "evidence": evidence,
        }

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
        for host_obj in delivered_objects:
            _persist_source_ink_evidence(host_obj, source_ink_evidence)
        attempt_evidence.update(
            {
                "source_text": source_text,
                "source_text_preserved": True,
                "source_ink_evidence": copy.deepcopy(source_ink_evidence),
                "source_ink_evidence_persisted": True,
                **_source_ink_delivery_binding_fields(
                    bound_item, source_ink_evidence
                ),
            }
        )
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
                or getattr(host_obj, "PDFSourceInkClassification", None)
                != source_ink_evidence["classification"]
                or getattr(host_obj, "PDFSourceInkEvidenceSHA256", None)
                != source_ink_evidence["evidence_sha256"]
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
    items = _bind_page_text_source_ink_evidence(
        page,
        source_dict,
        items,
        opts=opts,
    )
    _register_text_delivery_obligations(
        opts,
        [str(item["source_item_id"]) for item in items],
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
        _invoke_import_cancellation_checkpoint(opts, "text item")
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


def _bind_page_text_suppression_delivery_evidence(
    raster_result: Any,
    text_entity_info: Any,
    *,
    page_number: int,
) -> Dict[str, Any]:
    """Bind a page background suppression proof to the text IDs it delivered."""

    if type(page_number) is not int or page_number <= 0:
        raise ImportLifecycleError("page Raster text-suppression page is invalid")
    raster_evidence = (
        raster_result.get("evidence") if isinstance(raster_result, dict) else None
    )
    suppression_evidence = (
        raster_evidence.get("page_text_suppression")
        if isinstance(raster_evidence, dict)
        else None
    )
    if (
        not isinstance(suppression_evidence, dict)
        or suppression_evidence.get("verified") is not True
        or suppression_evidence.get("page_number") != page_number
    ):
        raise ImportLifecycleError(
            "page %d Raster text-suppression evidence is invalid" % page_number
        )
    variant = suppression_evidence.get("raster_content_variant")
    if (
        variant not in {
            "text_suppressed_page_background",
            "original_page_no_canonical_text",
        }
        or raster_evidence.get("raster_content_variant") != variant
    ):
        raise ImportLifecycleError(
            "page %d Raster text-suppression variant is invalid" % page_number
        )
    stored_digest = suppression_evidence.get("evidence_sha256")
    canonical_digest = _page_text_suppression_evidence_digest(suppression_evidence)
    if (
        not isinstance(stored_digest, str)
        or not stored_digest
        or stored_digest != canonical_digest
        or raster_evidence.get("page_text_suppression_evidence_sha256")
        != stored_digest
    ):
        raise ImportLifecycleError(
            "page %d Raster text-suppression evidence digest is stale or invalid"
            % page_number
        )
    suppressed_source_ids = suppression_evidence.get("source_text_item_ids")
    delivered_source_ids = (
        text_entity_info.get("source_item_ids")
        if isinstance(text_entity_info, dict)
        else None
    )
    for source_ids in (suppressed_source_ids, delivered_source_ids):
        if (
            not isinstance(source_ids, list)
            or any(not isinstance(value, str) or not value for value in source_ids)
            or len(set(source_ids)) != len(source_ids)
        ):
            raise ImportLifecycleError(
                "page %d Raster text source IDs are invalid" % page_number
            )
    if (
        suppression_evidence.get("source_text_item_count")
        != len(suppressed_source_ids)
        or delivered_source_ids != suppressed_source_ids
    ):
        raise ImportLifecycleError(
            "page %d Raster text suppression does not exactly match the delivered "
            "structural text source IDs" % page_number
        )
    suppression_evidence["delivered_source_text_item_ids"] = list(
        delivered_source_ids
    )
    suppression_evidence["delivered_source_text_item_ids_sha256"] = (
        _page_manifest_digest(delivered_source_ids)
    )
    suppression_evidence["delivery_source_item_ids_bound"] = True
    bound_digest = _page_text_suppression_evidence_digest(suppression_evidence)
    if not bound_digest:
        raise ImportLifecycleError(
            "page %d Raster text-suppression delivery binding is not canonical"
            % page_number
        )
    suppression_evidence["evidence_sha256"] = bound_digest
    raster_evidence["page_text_suppression_evidence_sha256"] = bound_digest
    if (
        _page_text_suppression_evidence_digest(suppression_evidence)
        != bound_digest
    ):
        raise ImportLifecycleError(
            "page %d Raster text-suppression delivery digest could not be verified"
            % page_number
        )
    return suppression_evidence


def _persist_page_text_suppression_host_binding(
    fc_doc,
    raster_result: Any,
    suppression_evidence: Any,
    *,
    page_number: int,
) -> Dict[str, Any]:
    """Persist and reread the delivery-bound suppression proof on its ImagePlane."""

    if (
        not isinstance(raster_result, dict)
        or not isinstance(suppression_evidence, dict)
        or type(page_number) is not int
        or page_number <= 0
    ):
        raise ImportLifecycleError("page Raster text-suppression host binding is invalid")
    entity_ids = raster_result.get("created_entity_ids")
    if (
        not isinstance(entity_ids, list)
        or len(entity_ids) != 1
        or not isinstance(entity_ids[0], str)
        or not entity_ids[0]
    ):
        raise ImportLifecycleError(
            "page %d Raster text-suppression host identity is invalid" % page_number
        )
    entity_id = entity_ids[0]
    try:
        host_obj = fc_doc.getObject(entity_id)
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ImportLifecycleError(
            "page %d Raster text-suppression host is unavailable" % page_number
        ) from exc
    if (
        host_obj is None
        or _host_object_id(host_obj) != entity_id
        or str(getattr(host_obj, "TypeId", "") or "") != "Image::ImagePlane"
        or str(getattr(host_obj, "PDFSourceItemId", "") or "")
        != "p%d:page" % page_number
        or str(getattr(host_obj, "PDFRepresentation", "") or "") != "raster"
    ):
        raise ImportLifecycleError(
            "page %d Raster text-suppression host does not match its delivery"
            % page_number
        )

    source_ids = suppression_evidence.get("source_text_item_ids")
    delivered_ids = suppression_evidence.get("delivered_source_text_item_ids")
    evidence_digest = suppression_evidence.get("evidence_sha256")
    if (
        not isinstance(source_ids, list)
        or not isinstance(delivered_ids, list)
        or source_ids != delivered_ids
        or suppression_evidence.get("delivery_source_item_ids_bound") is not True
        or evidence_digest
        != _page_text_suppression_evidence_digest(suppression_evidence)
    ):
        raise ImportLifecycleError(
            "page %d Raster text-suppression delivery proof is invalid" % page_number
        )

    evidence_json = _canonical_page_manifest_json(suppression_evidence)
    source_ids_json = _canonical_page_manifest_json(source_ids)
    delivered_ids_json = _canonical_page_manifest_json(delivered_ids)
    expected = {
        "PDFRasterContentVariant": suppression_evidence["raster_content_variant"],
        "PDFTextSuppressionSchema": suppression_evidence["schema"],
        "PDFTextSuppressionMethod": suppression_evidence["text_suppression_method"],
        "PDFTextSuppressionEvidenceJSON": evidence_json,
        "PDFTextSuppressionEvidenceSHA256": evidence_digest,
        "PDFTextSuppressionSourceItemIDsJSON": source_ids_json,
        "PDFTextSuppressionSourceItemIDsSHA256": suppression_evidence[
            "source_text_item_ids_sha256"
        ],
        "PDFTextSuppressionSourceItemCount": len(source_ids),
        "PDFTextSuppressionDeliveredItemIDsJSON": delivered_ids_json,
        "PDFTextSuppressionDeliveredItemIDsSHA256": suppression_evidence[
            "delivered_source_text_item_ids_sha256"
        ],
        "PDFTextSuppressionDeliveryBound": True,
        "PDFTextSuppressionVerified": True,
    }
    property_types = {
        name: (
            "App::PropertyInteger"
            if name == "PDFTextSuppressionSourceItemCount"
            else (
                "App::PropertyBool"
                if name
                in {"PDFTextSuppressionDeliveryBound", "PDFTextSuppressionVerified"}
                else "App::PropertyString"
            )
        )
        for name in expected
    }
    add_property = getattr(host_obj, "addProperty", None)
    if not callable(add_property):
        raise ImportLifecycleError(
            "page %d Raster host cannot persist text-suppression evidence"
            % page_number
        )
    properties = set(getattr(host_obj, "PropertiesList", []) or [])
    try:
        for property_name, property_type in property_types.items():
            if property_name not in properties:
                add_property(property_type, property_name, "PDF Import")
                properties.add(property_name)
        for property_name, value in expected.items():
            setattr(host_obj, property_name, value)
        fc_doc.recompute()
    except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
        raise ImportLifecycleError(
            "page %d Raster text-suppression host properties could not be written"
            % page_number
        ) from exc

    for property_name, expected_value in expected.items():
        try:
            actual_value = getattr(host_obj, property_name)
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            raise ImportLifecycleError(
                "page %d Raster text-suppression host property is unreadable"
                % page_number
            ) from exc
        if (
            actual_value != expected_value
            or _host_property_type_id(host_obj, property_name)
            != property_types[property_name]
        ):
            raise ImportLifecycleError(
                "page %d Raster text-suppression host property did not persist"
                % page_number
            )
    snapshot = _host_page_text_suppression_snapshot(host_obj)
    if snapshot.get("page_text_suppression_binding_verified") is not True:
        raise ImportLifecycleError(
            "page %d Raster text-suppression host reread could not be verified"
            % page_number
        )
    raster_evidence = raster_result.get("evidence")
    if not isinstance(raster_evidence, dict):
        raise ImportLifecycleError(
            "page %d Raster evidence disappeared during host binding" % page_number
        )
    raster_evidence.update(
        {
            "page_text_suppression_host_entity_id": entity_id,
            "page_text_suppression_host_binding_verified": True,
            "page_text_suppression_host_snapshot": copy.deepcopy(snapshot),
        }
    )
    return snapshot




# ──────────────────────────────────────────────────────────────────────
# Raster page import (scanned PDF fallback)
# ──────────────────────────────────────────────────────────────────────
def _import_page_as_raster(pdf_doc, page, page_num: int, page_h: float,
                           opts: ImportOptions, scale: float,
                           parent, fc_doc, *,
                           suppress_structural_text: bool = False):
    """Render, persist, place, and reread one verified full-page ImagePlane."""
    del page_h
    if type(suppress_structural_text) is not bool:
        raise ValueError("page Raster text-suppression policy must be boolean")
    try:
        raster_request_context = _validated_page_raster_request_context(
            opts,
            int(page_num),
        )
        observation = _validated_live_source_page(
            page,
            opts,
            int(page_num),
            document=pdf_doc,
        )
        source_bytes = _validated_pdf_source_bytes(opts)
        digest = hashlib.sha256(source_bytes).hexdigest()
    except (TypeError, ValueError) as exc:
        raise ImportLifecycleError("full-page Raster source identity is invalid") from exc

    requested_dpi = max(72, int(getattr(opts, "raster_dpi", 200) or 200))
    adaptive_dpi = requested_dpi
    if not bool(getattr(opts, "raster_dpi_user_set", False)):
        w_cm = float(page.rect.width) * MM_PER_PT / 10.0
        h_cm = float(page.rect.height) * MM_PER_PT / 10.0
        area_cm2 = w_cm * h_cm
        if area_cm2 > 2000:
            adaptive_dpi = 150
        elif area_cm2 > 700:
            adaptive_dpi = 300
    requested_dpi, delivered_dpi, pixel_budget, dpi_degraded = (
        _resolve_raster_dpi_for_rect(
            page.rect,
            opts,
            requested_dpi=adaptive_dpi,
        )
    )

    candidates = [delivered_dpi]
    explicit_strict = bool(
        getattr(opts, "raster_dpi_user_set", False)
        and not getattr(opts, "allow_raster_dpi_degradation", False)
    )
    if not explicit_strict:
        for candidate in (max(96, delivered_dpi // 2), 96):
            if candidate not in candidates:
                candidates.append(candidate)
    pix = None
    text_suppression_evidence = None
    last_error = None
    attempted_dpis: List[int] = []
    for candidate in candidates:
        with _open_pdf_source_attempt(opts) as source_document:
            source_page = source_document.load_page(int(page_num) - 1)
            attempted_dpis.append(candidate)
            _invoke_import_cancellation_checkpoint(opts, "raster render retry")
            try:
                if suppress_structural_text:
                    pix, text_suppression_evidence = (
                        _render_text_suppressed_page_background(
                            source_page,
                            opts,
                            page_number=int(page_num),
                            delivered_dpi=int(candidate),
                        )
                    )
                else:
                    zoom = candidate / 72.0
                    pix = source_page.get_pixmap(
                        matrix=fitz.Matrix(zoom, zoom),
                        alpha=True,
                    )
                delivered_dpi = candidate
                break
            except ValueError as exc:
                if suppress_structural_text:
                    raise ImportLifecycleError(
                        "page %d text-suppressed Raster background is not provable: %s"
                        % (int(page_num), exc)
                    ) from exc
                last_error = exc
            except (RuntimeError, MemoryError, OverflowError) as exc:
                last_error = exc
                if explicit_strict:
                    break
                _warn(
                    "Page %d: raster render failed at %d DPI: %s"
                    % (page_num, candidate, exc)
                )
    if pix is None:
        raise RuntimeError(
            "Raster render failed at required DPI: %s" % (last_error or "unknown error")
        )
    dpi_degraded = delivered_dpi != requested_dpi
    if dpi_degraded and explicit_strict:
        raise RuntimeError("explicit raster DPI was not delivered exactly")
    try:
        img_path, asset_evidence = _persist_content_addressed_pixmap(
            pix,
            opts,
            asset_kind="page",
        )
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        raise RuntimeError("Raster asset could not be persisted: %s" % exc) from exc
    raster_sha256 = asset_evidence["source_asset_sha256"]
    raster_content_variant = (
        str(text_suppression_evidence.get("raster_content_variant") or "")
        if suppress_structural_text and isinstance(text_suppression_evidence, dict)
        else "full_page_original"
    )
    if raster_content_variant not in {
        "full_page_original",
        "text_suppressed_page_background",
        "original_page_no_canonical_text",
    }:
        raise RuntimeError("full-page Raster content variant is invalid")

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
        ip.Placement = Placement(_v(0, 0, 0), Rotation())
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
        if "PDFRasterContentVariant" not in set(
            getattr(ip, "PropertiesList", []) or []
        ):
            add_property(
                "App::PropertyString", "PDFRasterContentVariant", "PDF Import"
            )
        ip.PDFRasterContentVariant = raster_content_variant
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
            or str(getattr(ip, "PDFRasterContentVariant", ""))
            != raster_content_variant
            or _host_property_type_id(ip, "PDFRasterContentVariant")
            != "App::PropertyString"
            or not math.isclose(float(ip.XSize), float(w_units), abs_tol=1e-7)
            or not math.isclose(float(ip.YSize), float(h_units), abs_tol=1e-7)
            or anchor is None
            or any(
                abs(anchor[index] - expected) > 1e-7
                for index, expected in enumerate((0.0, 0.0, 0.0))
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
        f"Placed page {page_num} as {delivered_dpi} DPI raster "
        f"({w_units:.0f} x {h_units:.0f} model units)"
    )
    return {
        "outcome": "verified",
        "entity_type": "raster",
        "created_entity_ids": [entity_id],
        "evidence": {
            "host_entity_type": "Image::ImagePlane",
            "source_page_number": int(page_num),
            "raster_file": str(img_path),
            "raster_file_included": True,
            "raster_file_included_property_bound": True,
            "pdf_sha256": digest,
            "dpi": int(delivered_dpi),
            "requested_dpi": int(requested_dpi),
            "delivered_dpi": int(delivered_dpi),
            "dpi_degraded": bool(dpi_degraded),
            "raster_render_attempted_dpis": attempted_dpis,
            "pixel_budget": int(pixel_budget),
            "pixel_width": int(getattr(pix, "width", 0) or 0),
            "pixel_height": int(getattr(pix, "height", 0) or 0),
            "x_size": float(w_units),
            "y_size": float(h_units),
            "raster_content_variant": raster_content_variant,
            "page_text_suppression": copy.deepcopy(text_suppression_evidence),
            "page_text_suppression_evidence_sha256": (
                text_suppression_evidence.get("evidence_sha256")
                if isinstance(text_suppression_evidence, dict)
                else None
            ),
            **copy.deepcopy(raster_request_context),
            "source_page_observation_verified": True,
            "page_visual_observation_sha256": observation["observation_sha256"],
            **asset_evidence,
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
def _raise_if_import_cancelled(progress, phase: str) -> None:
    """Raise the only cancellation signal; never authorize partial delivery."""

    if progress is None:
        return
    try:
        cancelled = progress.wasCanceled()
    except (AttributeError, RuntimeError, TypeError):
        cancelled = False
    if not cancelled:
        return
    _warn("Import cancelled by user")
    try:
        progress.close()
    except (AttributeError, RuntimeError, TypeError):
        pass
    raise ImportCancelled("PDF import cancelled during %s" % str(phase or "page import"))


def _invoke_import_cancellation_checkpoint(
    opts: ImportOptions,
    phase: str,
) -> None:
    """Run the active GUI checkpoint or a deterministic injected callback."""

    checkpoint = getattr(opts, "_import_cancellation_checkpoint", None)
    if callable(checkpoint):
        checkpoint(str(phase or "import"))
        return
    callback = getattr(opts, "_import_cancellation_callback", None)
    if not callable(callback):
        return
    cancelled = callback(str(phase or "import"))
    if cancelled is True:
        raise ImportCancelled(
            "PDF import cancelled during %s" % str(phase or "import")
        )


@_memoized_wirestrings
def import_pdf_page(pdf_path: str, page_num: int = 1,
                    opts: Optional[ImportOptions] = None,
                    autofit: bool = True):
    """Import exactly one page through the common atomic acceptance pipeline."""

    if opts is None:
        opts = ImportOptions(ignore_images=not IMAGE_WB)
    opts.pages = [page_num]
    return import_pdf(pdf_path, opts, autofit=autofit)


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

    last_event_pump = [0.0]

    def _page_cancellation_checkpoint(phase):
        now = time.perf_counter()
        force_event_pump = str(phase or "").startswith("before ")
        if QtWidgets is not None and (
            force_event_pump or now - last_event_pump[0] >= 0.025
        ):
            try:
                QtWidgets.QApplication.processEvents()
                last_event_pump[0] = now
            except (AttributeError, RuntimeError, TypeError):
                pass
        _raise_if_import_cancelled(progress, str(phase or "page import"))
        callback = getattr(opts, "_import_cancellation_callback", None)
        if callable(callback) and callback(str(phase or "page import")) is True:
            raise ImportCancelled(
                "PDF import cancelled during %s" % str(phase or "page import")
            )

    opts._import_cancellation_checkpoint = _page_cancellation_checkpoint

    def _progress_update(value, label):
        """Update progress dialog value and label, process events."""
        if progress:
            elapsed = time.time() - _import_start
            progress.setValue(value)
            progress.setLabelText(f"{label}  [{elapsed:.1f}s]")
        _invoke_import_cancellation_checkpoint(opts, str(label or "page progress"))

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

        sparse_mode = _auto_sparse_page_mode(n_drawings, n_text_blocks, n_images)
        if sparse_mode:
            # Sparse pages are not proof that a complete-page Raster is needed.
            # Genuine image occurrences are imported below; drawing-only and
            # blank pages simply have no fabricated text/raster obligation.
            effective_mode = sparse_mode
            _flood_reason = (
                "sparse image page — preserving exact image occurrences"
                if n_images > 0
                else "sparse vector/textless page — no page Raster fallback"
            )

        elif glyph_flood:
            # Vectorized text/map-art flood: huge counts of tiny filled groups.
            # Preserve only a raster appearance by default; if substantial
            # stroked vectors exist, keep a hybrid overlay.
            effective_mode = "hybrid" if n_images > 0 else "vector"
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
            effective_mode = "hybrid" if n_images > 0 else "vector"
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
            effective_mode = "hybrid"
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
    if (
        str(getattr(opts, "import_mode", "") or "").strip().lower() == "auto"
        and prior_effective_mode == "raster"
        and (
            effective_mode != prior_effective_mode or auto_raster_text_overlay
        )
    ):
        opts.auto_resolved_mode = effective_mode
        opts.auto_reason = (
            str(opts.auto_reason or "Raster content strategy").rstrip()
            + "; requested text representation contract retained"
        )

    _raise_if_import_cancelled(progress, "page analysis")

    placed_full_page_raster_background = False
    full_page_raster_result = None

    # ── Raster-only mode, optionally with the separately requested text layer ──
    if _should_place_full_page_raster(effective_mode):
        _record_raster_page(opts, opts.auto_reason or "raster mode")
        _msg(f"Page {page_num}: rendering at {opts.raster_dpi} DPI (raster mode)")
        _progress_update(5, f"Rendering raster image at {opts.raster_dpi} DPI...")
        full_page_raster_result = _import_page_as_raster(
            pdf_doc, page, page_num, page_h, opts, scale,
            top_group or fc_doc, fc_doc,
            suppress_structural_text=auto_raster_text_overlay,
        )
        if auto_raster_text_overlay:
            placed_full_page_raster_background = True
            drawings = []
            n_drawings = 0
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
                    pdf_sha256=hashlib.sha256(
                        _validated_pdf_source_bytes(opts)
                    ).hexdigest(),
                )
            if progress:
                progress.setValue(100)
                progress.close()
            _invoke_import_cancellation_checkpoint(
                opts,
                "before page persistence",
            )
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
        _invoke_import_cancellation_checkpoint(opts, "geometry group")
        # Throttled progress updates — every 500 on heavy pages, 100 otherwise.
        # Each processEvents() call allocates Qt timers; doing it 19k× is
        # what exhausts Windows GDI handles.
        if progress and pg_idx % _progress_interval == 0:
            geo_pct = 10 + int(69 * pg_idx / max(n_drawings, 1))
            _progress_update(
                geo_pct,
                f"Processing geometry... {pg_idx}/{n_drawings}")
            _raise_if_import_cancelled(progress, "geometry")

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
            _invoke_import_cancellation_checkpoint(opts, "geometry item")
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
                _raise_if_import_cancelled(progress, "geometry batch publication")
                _flush_batch(_fk, force=True)
        if opts.verbose:
            total_batches = sum(_batch_idx.values())
            _msg(f"Page {page_num}: geometry batched into {total_batches} "
                 f"compound(s) (batch_size={_batch_size})")

    text_entity_info = None

    # ── Text import ──
    if opts.import_text and opts.text_mode != "none":
        _progress_update(86, "Importing text...")

        _raise_if_import_cancelled(progress, "text")
        text_group = _make_group(top_group or fc_doc, "Text", fc_doc)
        try:
            raw_tdict = page.get_text("rawdict")
        except (RuntimeError, ValueError, TypeError, AttributeError) as exc:
            _register_text_delivery_obligations(
                opts,
                ["p%d:page" % int(page_num)],
            )
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

        pdf_sha256 = hashlib.sha256(
            _validated_pdf_source_bytes(opts)
        ).hexdigest()
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
        if placed_full_page_raster_background:
            suppression_evidence = _bind_page_text_suppression_delivery_evidence(
                full_page_raster_result,
                text_entity_info,
                page_number=int(page_num),
            )
            _persist_page_text_suppression_host_binding(
                fc_doc,
                full_page_raster_result,
                suppression_evidence,
                page_number=int(page_num),
            )
        if int(text_entity_info.get("source_item_count", 0) or 0) <= 0:
            _msg(
                "Page %d: no canonical source text items; text obligation set is empty"
                % int(page_num)
            )
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
                _invoke_import_cancellation_checkpoint(opts, "hatch group")
                items = path_group.get("items", [])
                if not items:
                    continue
                stroke = path_group.get("color") or path_group.get("stroke")
                stroke_rgb = _optional_color(stroke)
                current_pt = None
                sub_edges = []
                for item in items:
                    _invoke_import_cancellation_checkpoint(opts, "hatch item")
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
        img_group = _make_group(top_group or fc_doc, "Images", fc_doc)
        embedded_result = _import_embedded_image_occurrences(
            opts=opts,
            page_number=int(page_num),
            page_h=float(page_h),
            scale=float(scale),
            fc_doc=fc_doc,
            image_group=img_group,
        )
        obj_count += int(embedded_result.get("count", 0) or 0)

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

    _invoke_import_cancellation_checkpoint(opts, "before page persistence")
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
    path_cleanup = _rollback_attempt_paths(opts)
    if path_cleanup.get("cleanup_complete") is not True:
        raise ImportCleanupError(
            "prior import publication cleanup is incomplete",
            details={"retryable": True, "path_cleanup": path_cleanup},
        )
    _dispose_pdf_source_attempt(opts)
    opts.phase_timings_ms.clear()
    opts.shapestring_skips.clear()
    opts.text_mode_fallbacks.clear()
    opts.text_delivered_counts.clear()
    opts.text_delivery_attempts.clear()
    opts.text_delivery_obligation_source_item_ids.clear()
    opts.page_visual_source_observations.clear()
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
    opts._source_provenance_objects = []
    opts._import_session_id = ""
    opts._provenance_page = 0
    opts._live_import_report = None
    opts._atomic_import_active = False
    opts._import_cancellation_checkpoint = None


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


def _rollback_import_attempt(
    fc_doc,
    opts: ImportOptions,
    baseline_object_ids: set,
    baseline_object_names: set,
    *,
    transaction_open: bool,
) -> Dict[str, Any]:
    """Abort one transaction and verify all model/file effects are gone."""

    created_before_abort = [
        _host_object_id(host_obj)
        for host_obj in _document_objects(fc_doc)
        if id(host_obj) not in baseline_object_ids
        and _host_object_id(host_obj) not in baseline_object_names
    ]
    errors: List[str] = []
    if transaction_open:
        try:
            fc_doc.abortTransaction()
        except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
            errors.append("abortTransaction: %s" % exc)
    remaining_after_abort = [
        host_obj
        for host_obj in _document_objects(fc_doc)
        if id(host_obj) not in baseline_object_ids
        and _host_object_id(host_obj) not in baseline_object_names
    ]
    if remaining_after_abort:
        object_cleanup = _remove_post_baseline_document_objects(
            fc_doc,
            baseline_object_ids,
            baseline_object_names,
        )
    else:
        object_cleanup = {
            "created_entity_ids": list(created_before_abort),
            "removed_entity_ids": list(created_before_abort),
            "live_post_baseline_entity_ids": [],
            "cleanup_errors": [],
            "cleanup_complete": True,
        }
    path_cleanup = _rollback_attempt_paths(opts)
    errors.extend(list(object_cleanup.get("cleanup_errors") or []))
    errors.extend(list(path_cleanup.get("cleanup_errors") or []))
    created_ids = list(
        dict.fromkeys(
            created_before_abort
            + list(object_cleanup.get("created_entity_ids") or [])
        )
    )
    removed_ids = list(object_cleanup.get("removed_entity_ids") or [])
    live_ids = list(object_cleanup.get("live_post_baseline_entity_ids") or [])
    complete = bool(
        not errors
        and not live_ids
        and object_cleanup.get("cleanup_complete") is True
        and path_cleanup.get("cleanup_complete") is True
    )
    return {
        "created_entity_ids": created_ids,
        "removed_entity_ids": removed_ids,
        "live_post_baseline_entity_ids": live_ids,
        "object_cleanup": object_cleanup,
        "path_cleanup": path_cleanup,
        "cleanup_errors": errors,
        "cleanup_complete": complete,
    }


def _raise_incomplete_rollback(rollback: Dict[str, Any], failure: BaseException) -> None:
    if rollback.get("cleanup_complete") is True:
        return
    raise ImportCleanupError(
        "Import failed and rollback cleanup is incomplete",
        details={"retryable": True, "rollback": rollback},
    ) from failure


@_memoized_wirestrings
def import_pdf(
    pdf_path: str,
    opts: Optional[ImportOptions] = None,
    *,
    autofit: bool = True,
):
    """Atomically import selected pages and return exact ``True`` or ``False``."""

    if opts is None:
        opts = ImportOptions(ignore_images=not IMAGE_WB)
    fc_doc, document_created_by_attempt = _ensure_doc_with_ownership()
    attempt_accepted = False
    t_import_start = time.perf_counter()
    baseline_objects: List[Any] = []
    baseline_object_ids: set = set()
    baseline_object_names: set = set()
    pdf_doc_for_import = None
    transaction_open = False
    imported_count = 0
    total_pages = 0
    try:
        # Ownership starts at acquisition, so even baseline/default setup
        # failures cannot leak an attempt-created document.
        baseline_objects = _document_objects(fc_doc)
        baseline_object_ids = {id(host_obj) for host_obj in baseline_objects}
        baseline_object_names = {
            _host_object_id(host_obj) for host_obj in baseline_objects
        }
        from pdfcadcore.fitz_loader import PdfOpenError

        _reset_import_run_state(opts)
        opts._atomic_import_active = True
        try:
            _initialize_pdf_source_attempt(pdf_path, opts)
        except OSError as exc:
            raise PdfOpenError(
                "empty_file",
                "Cannot read PDF source: %s" % exc,
            ) from exc

        try:
            from pdfcadcore.primitives import reset_ids

            reset_ids()
        except ImportError:
            pass
        cleanup_temp_files()

        unit_scale = (MM_PER_PT if opts.scale_to_mm else 1.0) * opts.user_scale
        page_height_scaled = 792 * unit_scale
        page_heights_scaled: Dict[int, float] = {}
        model3d_text_evidence: List[str] = []
        open_started = time.perf_counter()
        with _open_pdf_source_attempt(opts) as metadata_doc:
            total_pages = len(metadata_doc)
            if total_pages <= 0:
                raise PdfOpenError("empty_file", "PDF contains no pages")
            requested_pages = getattr(opts, "pages", None)
            if requested_pages is None or requested_pages == []:
                pages = list(range(1, total_pages + 1))
            elif not isinstance(requested_pages, (list, tuple)):
                raise ValueError("selected pages must be a list of exact integers")
            else:
                pages = list(requested_pages)
            if (
                not pages
                or any(type(page) is not int for page in pages)
                or len(pages) != len(set(pages))
                or any(page < 1 or page > total_pages for page in pages)
            ):
                raise ValueError(
                    "selected pages must be unique exact integers in range 1..%d"
                    % total_pages
                )
            opts.pages = list(pages)
            page_height_scaled = metadata_doc.load_page(0).rect.height * unit_scale
            for page_number in pages:
                metadata_page = metadata_doc.load_page(page_number - 1)
                page_heights_scaled[page_number] = (
                    metadata_page.rect.height * unit_scale
                )
                try:
                    page_text = metadata_page.get_text("text") or ""
                except (RuntimeError, ValueError, AttributeError) as exc:
                    raise ImportLifecycleError(
                        "metadata text extraction failed on page %d" % page_number
                    ) from exc
                model3d_text_evidence.append(page_text)
                for line in page_text.splitlines():
                    line = line.strip()
                    if line:
                        opts._bootstrap_text_items.append(
                            {"text": line, "page": page_number}
                        )
        _capture_page_visual_runtime_authority(opts, list(pages))
        _validated_pdf_source_snapshot_path(opts)
        opts.phase_timings_ms["open_pdf_ms"] = (
            time.perf_counter() - open_started
        ) * 1000.0

        try:
            from pdfcadcore.model3d_intent import analyze_model3d_intent

            intent = analyze_model3d_intent(
                model3d_text_evidence,
                host_supports_3d=True,
            )
            opts._model3d_intent = intent.to_dict()
            opts._model3d_intent_feasible = bool(intent.feasible)
            opts._model3d_text_evidence = list(model3d_text_evidence)
        except ImportError:
            opts._model3d_intent = {
                "feasible": False,
                "plates": [],
                "members": [],
                "skipped_reason": "3D intent analysis unavailable",
            }
            opts._model3d_intent_feasible = False

        pdf_doc_for_import = _open_pdf_source_attempt(opts)
        fc_doc.openTransaction("Import PDF")
        transaction_open = True
        pages_started = time.perf_counter()
        running_stack_offset = 0.0
        page_arrangement = _normalize_page_arrangement(
            getattr(opts, "page_arrangement", "spread")
        )
        page_gap_ratio = _normalize_page_gap_ratio(
            getattr(opts, "page_gap_ratio", 0.20)
        )
        first_page = True
        all_text_entity_info = None
        for page_number in pages:
            _msg(
                "Importing page %d/%d (%d of %d)..."
                % (page_number, total_pages, imported_count + 1, len(pages))
            )
            with _verified_pdf_snapshot_consumer(
                opts,
                "page %d import" % page_number,
            ) as snapshot_path:
                _, page_text_info = _import_pdf_page_inner(
                    pdf_doc_for_import,
                    snapshot_path,
                    page_number,
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
                            examples[: 3 - len(all_text_entity_info["examples"])]
                        )
            current_height = page_heights_scaled.get(
                page_number,
                page_height_scaled,
            )
            if len(pages) > 1 and not first_page:
                running_stack_offset += _page_stack_step(
                    current_height,
                    page_arrangement,
                    page_gap_ratio,
                )
                y_shift = -running_stack_offset
                group = None
                for host_obj in reversed(fc_doc.Objects):
                    if (
                        host_obj.Name.startswith("PDF_Page_%d" % page_number)
                        and host_obj.isDerivedFrom("App::DocumentObjectGroup")
                    ):
                        group = host_obj
                        break
                if group and hasattr(group, "Group"):
                    for child in group.Group:
                        if hasattr(child, "Placement"):
                            child.Placement.Base.y += y_shift
                        if hasattr(child, "Group"):
                            for nested in child.Group:
                                if hasattr(nested, "Placement"):
                                    nested.Placement.Base.y += y_shift
            first_page = False
            imported_count += 1
        if imported_count != len(pages):
            raise ImportLifecycleError("not every selected page completed")
        opts.phase_timings_ms["pages_import_ms"] = (
            time.perf_counter() - pages_started
        ) * 1000.0

        postprocess_started = time.perf_counter()
        fc_doc.recompute()
        try:
            from pdfcadcore.resolved_scale import probe_page_scale

            with _open_pdf_source_attempt(opts) as scale_doc:
                for page_number in pages:
                    _merge_page_scale_into_opts(
                        opts,
                        probe_page_scale(
                            scale_doc.load_page(page_number - 1),
                            page_number,
                        ),
                    )
        except ImportError:
            pass

        if not hasattr(opts, "_report_extra"):
            opts._report_extra = {}
        if all_text_entity_info:
            opts._report_extra["actual_text_entity_types"] = all_text_entity_info
        opts._report_extra["result_status"] = "success"
        opts._report_extra.pop("terminal_failure", None)

        if bool(getattr(opts, "model3d_semantic", False)):
            intent_payload = getattr(opts, "_model3d_intent", None) or {}
            members = (
                intent_payload.get("members")
                if isinstance(intent_payload, dict)
                else []
            )
            if members:
                from pdfcadcore.semantic_members import create_semantic_members

                semantic_report = create_semantic_members(
                    list(members),
                    doc=fc_doc,
                )
                model3d_extra = dict(
                    getattr(opts, "_report_extra", {}).get("model_3d") or {}
                )
                model3d_extra["semantic_members"] = semantic_report
                opts._report_extra["model_3d"] = model3d_extra
        fc_doc.recompute()
        opts.phase_timings_ms["postprocess_ms"] = (
            time.perf_counter() - postprocess_started
        ) * 1000.0

        pdf_doc_for_import.close()
        pdf_doc_for_import = None
        imported_objects = _post_baseline_document_objects(
            _document_objects(fc_doc),
            baseline_object_ids,
            baseline_object_names,
        )
        _invoke_import_cancellation_checkpoint(opts, "before persistence")
        persistence_started = time.perf_counter()
        try:
            host_inventory, save_reopen_inventory = _build_persistence_host_evidence(
                fc_doc,
                imported_objects,
                baseline_object_names,
                opts=opts,
            )
        finally:
            opts.phase_timings_ms["persistence_evidence_ms"] = (
                time.perf_counter() - persistence_started
            ) * 1000.0
        if host_inventory.get("verified") is not True:
            raise ImportLifecycleError("live host inventory verification failed")
        if save_reopen_inventory.get("verified") is not True:
            raise ImportLifecycleError("save/reopen host inventory verification failed")
        opts._report_extra["actual_host_object_inventory"] = host_inventory
        opts._report_extra["save_reopen_inventory"] = save_reopen_inventory
        inventory_counts = dict(host_inventory.get("counts") or {})

        report_path = opts.import_report_path or _default_import_report_path(pdf_path)
        report_target = Path(_journal_attempt_path(opts, report_path))
        bootstrap_target = Path(
            _journal_attempt_path(
                opts,
                report_target.with_name("parts_bootstrap.json"),
            )
        )
        provenance_target = Path(
            _journal_attempt_path(
                opts,
                report_target.with_name("source_provenance.json"),
            )
        )
        fallback_used, fallback_reason = _report_fallback_state(opts)
        elapsed_ms = (time.perf_counter() - t_import_start) * 1000.0
        opts.phase_timings_ms["total_ms"] = elapsed_ms
        _invoke_import_cancellation_checkpoint(opts, "before report")
        written_report = write_import_report(
            pdf_path=pdf_path,
            output_path=str(report_target),
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
        if Path(written_report).resolve() != report_target.resolve():
            raise ImportLifecycleError("report writer returned an unexpected path")
        if not report_target.is_file() or report_target.stat().st_size <= 0:
            raise ImportLifecycleError("import report publication is missing or empty")
        if not bootstrap_target.is_file() or bootstrap_target.stat().st_size <= 0:
            raise ImportLifecycleError("parts bootstrap publication is missing or empty")
        if (
            list(getattr(opts, "_source_provenance_objects", []) or [])
            and (
                not provenance_target.is_file()
                or provenance_target.stat().st_size <= 0
            )
        ):
            raise ImportLifecycleError(
                "source provenance publication is missing or empty"
            )
        _validated_pdf_source_snapshot_path(opts)
        _invoke_import_cancellation_checkpoint(opts, "before live gate")
        _require_live_import_contract_ready(opts)
        _dispose_pdf_source_attempt(opts)
        _invoke_import_cancellation_checkpoint(opts, "before commit")
        fc_doc.commitTransaction()
        transaction_open = False
        _accept_attempt_paths(opts)
        attempt_accepted = True
        if autofit:
            try:
                _autofit_import_view(fc_doc)
            except (AttributeError, RuntimeError, TypeError, ValueError) as exc:
                _warn("Import succeeded, but view autofit failed: %s" % exc)
        if opts.import_mode == "auto" and opts.auto_resolved_mode:
            try:
                _msg(
                    "Auto mode summary: %s%s"
                    % (
                        opts.auto_resolved_mode,
                        " — %s" % opts.auto_reason if opts.auto_reason else "",
                    )
                )
            except (AttributeError, RuntimeError, TypeError, ValueError):
                pass
        return True
    except ImportCancelled as failure:
        rollback = _rollback_import_attempt(
            fc_doc,
            opts,
            baseline_object_ids,
            baseline_object_names,
            transaction_open=transaction_open,
        )
        transaction_open = False
        _raise_incomplete_rollback(rollback, failure)
        return False
    except BaseException as failure:
        rollback = _rollback_import_attempt(
            fc_doc,
            opts,
            baseline_object_ids,
            baseline_object_names,
            transaction_open=transaction_open,
        )
        transaction_open = False
        if isinstance(failure, TextRepresentationFailure):
            failure.attempt["created_entity_ids"] = list(
                rollback.get("created_entity_ids") or []
            )
            failure.attempt["removed_entity_ids"] = list(
                rollback.get("removed_entity_ids") or []
            )
            failure.attempt["cleanup_complete"] = rollback["cleanup_complete"]
            failure.attempt["rollback"] = rollback
        _raise_incomplete_rollback(rollback, failure)
        raise
    finally:
        opts._atomic_import_active = False
        try:
            if pdf_doc_for_import is not None:
                pdf_doc_for_import.close()
        finally:
            try:
                _dispose_pdf_source_attempt(opts)
            finally:
                try:
                    if not attempt_accepted:
                        _close_attempt_created_document(
                            fc_doc,
                            document_created_by_attempt,
                        )
                finally:
                    opts._import_cancellation_checkpoint = None
