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

import math
import os
import re
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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
        if isinstance(col, (int, float)):
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
    text_mode: str = "3d_text"            # "labels" | "3d_text" | "glyphs" | "geometry" | "none"
    # When enabled, disables text reconstruction and uses glyph-accurate
    # placement paths only (pdftocairo geometry or exact per-span labels).
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
    import_report_path: Optional[str] = None


def _default_import_report_path(pdf_path: str) -> str:
    base = os.path.splitext(os.path.basename(pdf_path))[0]
    return os.path.join(tempfile.gettempdir(), f"{base}_import_report.json")


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


def write_import_report(
    *,
    pdf_path: str,
    output_path: str,
    opts: ImportOptions,
    pages_imported: int,
    total_pages: int,
    primitive_count: int = 0,
    text_count: int = 0,
    layer_count: int = 0,
    elapsed_ms: float = 0.0,
    fallback_used: bool = False,
    fallback_reason: Optional[str] = None,
) -> str:
    """Emit bcs.import_report/1.1 JSON for one import run."""
    from pdfcadcore.import_report import build_import_report

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
        pages=pages_imported or total_pages,
        primitive_count=primitive_count,
        text_count=text_count,
        layer_count=layer_count,
        elapsed_ms=elapsed_ms,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        pdf_engine_version=_pymupdf_version(),
        extra={
            "auto_resolved_mode": opts.auto_resolved_mode,
            "auto_reason": opts.auto_reason,
        },
    )
    report.write_json(output_path)
    return output_path


# ──────────────────────────────────────────────────────────────────────
# Coordinate transform
# ──────────────────────────────────────────────────────────────────────
def _to_fc(xy: Tuple[float, float], page_h: float,
           opts: ImportOptions, scale: float) -> "Vector":
    """Transform a PDF coordinate pair into a FreeCAD Vector."""
    x, y = xy
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


def _apply_style(obj, stroke_rgb, width, dashes, opts: ImportOptions):
    """Set line color, width, and dash style on a Part::Feature ViewObject."""
    try:
        vo = obj.ViewObject
        if stroke_rgb is not None:
            vo.LineColor = stroke_rgb
        if opts.assign_linewidth and width is not None:
            try:
                # PDF line widths in points are tiny; scale and enforce a visible minimum
                lw = float(width) * (0.7 if opts.scale_to_mm else 1.0)
                # Minimum 2.0 so lines are always visible regardless of background
                vo.LineWidth = max(2.0, lw)
            except (TypeError, ValueError, AttributeError):
                vo.LineWidth = 2.0
        else:
            # Even with no width info, make lines visible
            try:
                vo.LineWidth = 2.0
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
# Text fraction reconstruction
# ──────────────────────────────────────────────────────────────────────
# Common denominators in structural/architectural drawings
# Engineering drawing inch fractions are overwhelmingly base-2 denominators.
# Keeping this strict avoids false positives like "206" -> "2/06" for part IDs.
_VALID_DENOMS = {2, 4, 8, 16, 32, 64}


def _try_split_fraction(text: str) -> Optional[Tuple[str, str]]:
    """Try to split a combined numerator+denominator string like '18' → ('1','8').
    Returns (numerator, denominator) or None if no valid fraction found."""
    if not text or len(text) < 2:
        return None
    # Try each split point: "916" → ("9","16"), ("91","6")
    best = None
    for i in range(1, len(text)):
        num_s, den_s = text[:i], text[i:]
        try:
            num, den = int(num_s), int(den_s)
        except ValueError:
            continue
        if den in _VALID_DENOMS and 0 < num < den:
            # Prefer the split that gives a standard fraction
            if best is None:
                best = (num_s, den_s)
            # Prefer smaller denominators (more common)
            elif int(best[1]) > den:
                best = (num_s, den_s)
    return best


def _is_fraction_slash(span: dict) -> bool:
    """Check if a span is a fraction bar '/' drawn between stacked numbers."""
    text = span.get("text", "").strip()
    return text == "/"


def _is_superscript(span: dict) -> bool:
    """Check if span has the superscript flag (bit 0 of flags)."""
    return bool(span.get("flags", 0) & 1)


def _is_smaller_font(span: dict, ref_size: float) -> bool:
    """Check if span is noticeably smaller than the reference size."""
    size = float(span.get("size", 0))
    return size > 0 and size < ref_size * 0.95


def _repair_fraction_artifact_runs(text: str) -> str:
    """Fix run-on mixed-fraction artifacts produced by fragmented OCR spans.

    Example: "19/163 7/161 9/16" -> "1 9/16 3 7/16 1 9/16"
    """
    if not text:
        return text

    def _to_mixed(num_s: str, den_s: str, tail: str = "") -> str | None:
        try:
            num_v = int(num_s)
            den_v = int(den_s)
        except ValueError:
            return None
        if num_v < den_v:
            return None
        if len(num_s) < 2:
            return None
        whole_s = num_s[:-1]
        frac_num_s = num_s[-1]
        try:
            whole_v = int(whole_s)
            frac_num_v = int(frac_num_s)
        except ValueError:
            return None
        if whole_v < 0 or frac_num_v <= 0 or frac_num_v >= den_v:
            return None
        return f"{whole_v} {frac_num_v}/{den_v}{tail}"

    def _repl_with_tail(m: re.Match) -> str:
        num_s = m.group(1)
        den_s = m.group(2)
        tail = m.group(3)
        try:
            num_v = int(num_s)
            den_v = int(den_s)
        except ValueError:
            return m.group(0)
        if 0 < num_v < den_v:
            return f"{num_v}/{den_v} {tail}"
        mixed = _to_mixed(num_s, den_s, f" {tail}")
        return mixed if mixed else m.group(0)

    def _repl_no_tail(m: re.Match) -> str:
        mixed = _to_mixed(m.group(1), m.group(2))
        return mixed if mixed else m.group(0)

    # "19/163" -> "1 9/16 3"
    out = re.sub(r"(?<!\d)(\d{1,3})/(16|32|64)(\d)(?!\d)", _repl_with_tail, text)
    # "19/16" -> "1 9/16" (keep conservative: only denominators common to inch dims)
    out = re.sub(
        r"(?<!\d)(\d{2,3})/(16|32|64)(?!\d)",
        _repl_no_tail,
        out,
    )
    return out


def _reconstruct_line_text(spans: list) -> str:
    """Reconstruct a line of text, converting stacked fractions back to inline.

    PyMuPDF extracts PDF fractions (stacked numerator/denominator) as separate
    spans. CRITICALLY, the "/" fraction bar often appears on a completely
    separate PyMuPDF line — so we CANNOT rely on seeing "/" within this line.

    Detection strategy: if we see small-font digit strings that form a valid
    fraction (numerator < denominator, denominator is a standard value), we
    reconstruct it as "num/denom" regardless of whether "/" is present.
    """
    if not spans:
        return ""

    # Single span — only try fraction split on longer digit strings (3+ chars)
    # Short strings like "12" are ambiguous (twelve vs 1/2) and at main text
    # size they're always the number. Real fractions like "1516" are 3+ chars.
    if len(spans) == 1:
        text = spans[0].get("text", "")
        if text.isdigit() and len(text) >= 3:
            frac = _try_split_fraction(text)
            if frac:
                return _repair_fraction_artifact_runs(frac[0] + "/" + frac[1])
        return _repair_fraction_artifact_runs(text)

    # Find the dominant non-slash font size in this line. In many CAD PDFs the
    # fraction slash is rendered slightly larger than nearby digits; using it as
    # the reference causes mixed-fraction detection to miss common patterns.
    non_slash_sizes = [
        float(s.get("size", 0))
        for s in spans
        if not _is_fraction_slash(s)
    ]
    sizes = [float(s.get("size", 0)) for s in spans]
    main_size = max(non_slash_sizes) if non_slash_sizes else (max(sizes) if sizes else 10.0)

    result = []
    i = 0

    def _append_frac(frac_str: str):
        """Append a fraction string with conservative spacing.
        Keep fractions tight after hyphens / parens / apostrophes so strings like
        PIPE1-1/2STD do not become PIPE1-1/2 STD. Only insert a space when the
        previous token really looks like a separate word/number."""
        if result and result[-1]:
            last_char = result[-1][-1]
            if last_char not in (" ", "-", "(", "[", "/", "'"):
                if last_char.isalpha() or last_char.isdigit():
                    result.append(" ")
        result.append(frac_str)

    while i < len(spans):
        span = spans[i]
        text = span.get("text", "")
        size = float(span.get("size", 0))

        # Skip standalone "/" — fraction bar (content already reconstructed)
        if _is_fraction_slash(span):
            if result and "/" in result[-1]:
                i += 1
                continue
            result.append(text)
            i += 1
            continue

        # Case A: Superscript numerator + denominator (with or without "/")
        # Pattern 1: ('7', flags=5) + ('16', small) → "7/16"
        # Pattern 2: ('7', flags=5) + ('/', slash) + ('16', small) → "7/16"
        if (_is_superscript(span) and _is_smaller_font(span, main_size)
                and text.isdigit() and i + 1 < len(spans)):
            next_span = spans[i + 1]
            next_text = next_span.get("text", "")

            # Pattern 1: numerator immediately followed by denominator
            if next_text.isdigit() and _is_smaller_font(next_span, main_size):
                try:
                    num_v, den_v = int(text), int(next_text)
                    if den_v in _VALID_DENOMS and 0 < num_v < den_v:
                        _append_frac(text + "/" + next_text)
                        i += 2
                        if i < len(spans) and _is_fraction_slash(spans[i]):
                            i += 1
                        continue
                except ValueError:
                    pass

            # Pattern 2: numerator + "/" + denominator (slash merged from adjacent line)
            if (_is_fraction_slash(next_span) and i + 2 < len(spans)):
                den_span = spans[i + 2]
                den_text = den_span.get("text", "")
                if den_text.isdigit() and _is_smaller_font(den_span, main_size):
                    try:
                        num_v, den_v = int(text), int(den_text)
                        if den_v in _VALID_DENOMS and 0 < num_v < den_v:
                            _append_frac(text + "/" + den_text)
                            i += 3  # skip num, slash, denom
                            continue
                    except ValueError:
                        pass

        # Case B: Small-font combined digits (with or without "/")
        # e.g. ('1516', size=10) → "15/16"
        # e.g. ('18', size=10) → "1/8"
        # e.g. ('12', size=10) → "1/2"
        if _is_smaller_font(span, main_size) and text.isdigit() and len(text) >= 3:
            frac = _try_split_fraction(text)
            if frac:
                _append_frac(frac[0] + "/" + frac[1])
                i += 1
                # Skip trailing "/" if present
                if i < len(spans) and _is_fraction_slash(spans[i]):
                    i += 1
                continue

        # Case C: Two same-sized digit spans → standalone fraction
        # e.g. ('5', size=10) + ('8', size=10) → "5/8"
        # Also handles: ('5', size=10) + ('/', slash) + ('8', size=10) → "5/8"
        if (text.isdigit() and _is_smaller_font(span, main_size) and i + 1 < len(spans)):
            next_span = spans[i + 1]
            next_text = next_span.get("text", "")
            next_size = float(next_span.get("size", 0))

            # Pattern 1: num + denom (adjacent)
            if (next_text.isdigit() and abs(size - next_size) < 1.0):
                try:
                    num_v, den_v = int(text), int(next_text)
                    if den_v in _VALID_DENOMS and 0 < num_v < den_v:
                        _append_frac(text + "/" + next_text)
                        i += 2
                        if i < len(spans) and _is_fraction_slash(spans[i]):
                            i += 1
                        continue
                except ValueError:
                    pass

            # Pattern 2: num + "/" + denom
            if (_is_fraction_slash(next_span) and i + 2 < len(spans)):
                den_span = spans[i + 2]
                den_text = den_span.get("text", "")
                den_size = float(den_span.get("size", 0))
                if (den_text.isdigit() and abs(size - den_size) < 1.0):
                    try:
                        num_v, den_v = int(text), int(den_text)
                        if den_v in _VALID_DENOMS and 0 < num_v < den_v:
                            _append_frac(text + "/" + den_text)
                            i += 3
                            continue
                    except ValueError:
                        pass

        # Case D: compact fraction digits followed by trailing slash
        # e.g. ('34') + ('/') → "3/4"
        if text.isdigit() and len(text) >= 2 and i + 1 < len(spans):
            next_span = spans[i + 1]
            if _is_fraction_slash(next_span):
                frac = _try_split_fraction(text)
                if frac:
                    _append_frac(frac[0] + "/" + frac[1])
                    i += 2
                    continue

        # Case E: mixed fraction where slash trails the compact frac span
        # e.g. ('2') + ('12') + ('/') → "2 1/2"
        # e.g. ("37'-10") + ('12') + ('/') → "37'-10 1/2"
        if i + 2 < len(spans):
            next_span = spans[i + 1]
            slash_span = spans[i + 2]
            next_text = next_span.get("text", "")
            if (
                next_text.isdigit()
                and len(next_text) >= 2
                and _is_fraction_slash(slash_span)
                and _is_smaller_font(next_span, main_size)
            ):
                frac = _try_split_fraction(next_text)
                if frac:
                    result.append(text)
                    _append_frac(frac[0] + "/" + frac[1])
                    i += 3
                    continue

        # Default: just append the text
        result.append(text)
        i += 1

    return _repair_fraction_artifact_runs("".join(result))


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


def _same_text_line(y1: float, y2: float, size1: float, size2: float) -> bool:
    tol = 0.25 * max(size1, size2, 1.0)
    return abs(y1 - y2) <= tol


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


def _line_angle_deg(line: dict) -> float:
    text_dir = line.get("dir", (1.0, 0.0))
    if text_dir and len(text_dir) >= 2:
        try:
            dx, dy = float(text_dir[0]), float(text_dir[1])
            return -math.degrees(math.atan2(dy, dx))
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


def _render_text_spans_exact_labels(
    tdict: dict,
    text_group,
    page_h: float,
    opts: ImportOptions,
    scale: float,
    only_rotated: bool = False,
) -> int:
    """Render one Draft text object per PDF span for highest label fidelity."""
    if Draft is None or text_group is None:
        return 0

    count = 0
    # Per-text placement registry to suppress near-identical duplicate spans
    # (common in some PDFs with layered text paints), while preserving
    # genuinely distinct nearby labels.
    seen = {}
    rotated_threshold = _rotated_text_threshold_deg()
    for block in tdict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", []) or []
            if not spans:
                continue
            angle_deg = _line_angle_deg(line)
            norm_angle = _normalize_text_angle_deg(angle_deg)
            if only_rotated and abs(norm_angle) < rotated_threshold:
                continue
            rot = Rotation(Vector(0, 0, 1), angle_deg)

            for span in spans:
                text = span.get("text", "")
                if not text or text.isspace():
                    continue
                origin = _span_origin_pdf(span)
                if not origin:
                    continue

                try:
                    size_pt = float(span.get("size", 0.0) or 0.0)
                except (TypeError, ValueError):
                    size_pt = 0.0
                font_size_fc = max(0.1, (size_pt if size_pt > 0.0 else 3.0) * scale)
                txt = str(text).strip()
                if not txt:
                    continue

                dedupe_key = (
                    txt,
                    round(float(norm_angle), 1),
                    round(float(font_size_fc), 2),
                )
                bucket = seen.setdefault(dedupe_key, [])
                is_duplicate = False
                ox = float(origin[0])
                oy = float(origin[1])
                for px, py in bucket:
                    # Tight tolerance: only collapse effectively overlaid spans.
                    if abs(ox - px) <= 0.35 and abs(oy - py) <= 0.35:
                        is_duplicate = True
                        break
                if is_duplicate:
                    continue
                bucket.append((ox, oy))

                pos = _to_fc(origin, page_h, opts, scale)
                font_name = _normalize_pdf_font_name(span.get("font", ""))
                # Draft text anchoring differs slightly from PDF span origins for
                # rotated labels; apply a conservative local-Y nudge only for
                # clearly non-horizontal text to improve vertical/diagonal fit.
                if abs(norm_angle) >= rotated_threshold:
                    try:
                        desc = float(span.get("descender", -0.2) or -0.2)
                    except (TypeError, ValueError):
                        desc = -0.2
                    offset_fc = _effective_descender(txt, desc) * font_size_fc * 0.35
                    pos = _apply_text_local_y_offset(pos, angle_deg, offset_fc)
                try:
                    t = Draft.make_text([text], placement=Placement(pos, rot))
                except (RuntimeError, ValueError, TypeError, AttributeError):
                    continue

                try:
                    t.ViewObject.FontSize = font_size_fc
                    if font_name:
                        t.ViewObject.FontName = font_name
                    t.ViewObject.Justification = "Left"
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass

                try:
                    text_group.addObject(t)
                except (AttributeError, RuntimeError, TypeError, ValueError):
                    pass
                count += 1
    return count


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


def _resolve_horizontal_run_overlaps(layout_items: List[dict], scale: float) -> List[dict]:
    """Push later horizontal sibling runs right only when they truly overlap.

    Uses the measured PDF bbox width when available and only falls back to
    estimated render width when needed. This avoids false-positive nudges that
    can misalign callouts in technical drawings.
    """
    if not layout_items:
        return layout_items
    s = max(scale, 1e-12)
    # IMPORTANT: tiny baseline jitter (e.g. 334.560 vs 334.562) can invert run
    # order when sorting primarily by baseline. Preserve left-to-right order
    # within each logical text line using a coarse line key when available.
    items = sorted(
        layout_items,
        key=lambda d: (d.get("line_key", round(d["baseline_y_pdf"], 1)), d["x_pdf"]),
    )
    prev = None
    for item in items:
        if not item.get("eligible_for_nudge", False):
            prev = item if item.get("is_horizontal", False) else prev
            continue
        if prev is None or not prev.get("eligible_for_nudge", False):
            prev = item
            continue
        if not _same_text_line(item["baseline_y_pdf"], prev["baseline_y_pdf"], item["font_size_fc"], prev["font_size_fc"]):
            prev = item
            continue
        prev_width = float(prev.get("orig_width_pdf", 0.0) or 0.0)
        if prev_width <= 1e-6:
            prev_width = float(prev.get("render_width_pdf", 0.0) or 0.0)
        prev_right = float(prev["x_pdf"]) + prev_width
        this_left = item["x_pdf"]
        overlap = prev_right - float(this_left)
        if overlap > 0.0:
            # Keep a tiny separation in PDF-space units.
            pad_pdf = min(
                0.75,
                (0.04 * max(prev["font_size_fc"], item["font_size_fc"], 1.0)) / s,
            )
            item["x_pdf"] += overlap + pad_pdf
        prev = item
    return items




_MIXED_FRACTION_RE = re.compile(
    r"""(?:
        \d+\s+\d+/\d+
        |
        \d+'-\d+\s+\d+/\d+"?
    )""",
    re.VERBOSE,
)


def _is_near_vertical_angle(angle_deg: float, tol_deg: float = 15.0) -> bool:
    a = angle_deg % 180.0
    return abs(a - 90.0) <= tol_deg


def _has_mixed_fraction_text(text: str) -> bool:
    return bool(_MIXED_FRACTION_RE.search((text or '').strip()))


def _projected_text_extents(item: dict, scale: float, font_size_fc: Optional[float] = None) -> Tuple[float, float, float, float]:
    """Projected extents in PDF-space for overlap checks.

    Returns (along_min, along_max, normal_min, normal_max). The anchor model
    matches how this importer places text today: left-justified runs extend
    forward from the insertion point; centered runs extend equally both ways.
    """
    fs = float(font_size_fc if font_size_fc is not None else item.get('font_size_fc', 0.0))
    s = max(scale, 1e-12)
    width_pdf = _estimate_text_width_mm(item.get('content', ''), fs) / s
    height_pdf = fs / s

    a = math.radians(float(item.get('angle_deg', 0.0)))
    ux, uy = math.cos(a), math.sin(a)
    nx, ny = -uy, ux

    x = float(item.get('x_pdf', 0.0))
    y = float(item.get('baseline_y_pdf', 0.0))
    anchor_along = x * ux + y * uy
    anchor_normal = x * nx + y * ny

    just = item.get('justification', 'Left')
    if just == 'Center':
        along_min = anchor_along - width_pdf / 2.0
        along_max = anchor_along + width_pdf / 2.0
    elif just == 'Right':
        along_min = anchor_along - width_pdf
        along_max = anchor_along
    else:
        along_min = anchor_along
        along_max = anchor_along + width_pdf

    normal_min = anchor_normal - height_pdf / 2.0
    normal_max = anchor_normal + height_pdf / 2.0
    return along_min, along_max, normal_min, normal_max


def _intervals_overlap(a0: float, a1: float, b0: float, b1: float, tol: float = 0.0) -> bool:
    return not (a1 < b0 - tol or b1 < a0 - tol)


def _axis_gap(a0: float, a1: float, b0: float, b1: float) -> float:
    if _intervals_overlap(a0, a1, b0, b1):
        return 0.0
    if a1 < b0:
        return b0 - a1
    return a0 - b1


def _apply_vertical_mixed_fraction_compaction(layout_items: List[dict], scale: float) -> List[dict]:
    """Shrink risky rotated mixed-fraction runs slightly when they would collide
    along their text direction.

    This is intentionally conservative: it touches only near-vertical mixed
    fractions and only when a projected overlap risk exists.
    """
    if not layout_items:
        return layout_items

    for item in layout_items:
        text = item.get('content', '')
        angle = float(item.get('angle_deg', 0.0))
        if not (_is_near_vertical_angle(angle) and _has_mixed_fraction_text(text)):
            continue

        base_fs = float(item.get('font_size_fc', 0.0))
        if base_fs <= 0:
            continue

        def risky(test_fs: float, _item=item) -> bool:
            a0, a1, n0, n1 = _projected_text_extents(_item, scale, test_fs)
            normal_tol = 0.20 * max(test_fs / max(scale, 1e-12), 1.0)
            min_clearance = 0.18 * max(test_fs / max(scale, 1e-12), 1.0)
            for other in layout_items:
                if other is _item:
                    continue
                oa0, oa1, on0, on1 = _projected_text_extents(other, scale)
                if not _intervals_overlap(n0, n1, on0, on1, tol=normal_tol):
                    continue
                if _axis_gap(a0, a1, oa0, oa1) < min_clearance:
                    return True
            return False

        if risky(base_fs):
            new_fs = base_fs
            for factor in (0.92, 0.88, 0.84):
                trial = base_fs * factor
                if not risky(trial):
                    new_fs = trial
                    break
                new_fs = trial
            item['font_size_fc'] = new_fs

    return layout_items


# ──────────────────────────────────────────────────────────────────────
# Raster page import (scanned PDF fallback)
# ──────────────────────────────────────────────────────────────────────
def _import_page_as_raster(pdf_doc, page, page_num: int, page_h: float,
                           opts: ImportOptions, scale: float,
                           parent, fc_doc):
    """Render a PDF page as a raster image and place it as an ImagePlane.

    Used for scanned PDFs, map/art PDFs, or pages with no usable vector content.
    DPI is auto-scaled to page physical size so small pages stay crisp and
    very large pages don't exhaust memory.
    """
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

    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat)

    # Save to temp file
    tmpdir = os.path.join(FreeCAD.getUserAppDataDir(),
                          "Mod", "PDFVectorImporter", "temp")
    os.makedirs(tmpdir, exist_ok=True)
    img_path = os.path.join(tmpdir, f"page_{page_num}_{dpi}dpi.png")
    pix.save(img_path)
    # Register cleanup: remove temp image when document is closed or on next import
    _register_temp_cleanup(img_path)

    # Calculate real-world size in mm from PDF page dimensions
    w_mm = page.rect.width * MM_PER_PT
    h_mm = page.rect.height * MM_PER_PT

    try:
        ip = fc_doc.addObject("Image::ImagePlane", f"Page_{page_num}_raster")
        ip.ImageFile = img_path
        ip.XSize = w_mm
        ip.YSize = h_mm
        ip.Placement = Placement(_v(0, 0, -0.1), Rotation())  # slightly behind vectors
        parent.addObject(ip)
        _msg(f"Placed page {page_num} as {dpi} DPI raster ({w_mm:.0f} x {h_mm:.0f} mm)")
    except (RuntimeError, OSError, ValueError, TypeError) as e:
        _warn(f"Raster placement failed: {e}\n"
              f"Image saved to: {img_path}")


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
# Page importer
# ──────────────────────────────────────────────────────────────────────
def import_pdf_page(pdf_path: str, page_num: int = 1,
                    opts: Optional[ImportOptions] = None,
                    autofit: bool = True):
    """Import a single PDF page into the active FreeCAD document."""
    if opts is None:
        opts = ImportOptions(ignore_images=not IMAGE_WB)
    fc_doc = _ensure_doc()  # Store reference — don't rely on ActiveDocument later

    # Validate PDF before opening
    try:
        with open(pdf_path, 'rb') as f:
            header = f.read(5)
        if header != b'%PDF-':
            raise ValueError(f"Not a valid PDF file: {pdf_path}")
    except OSError as e:
        raise ValueError(f"Cannot read PDF file: {e}") from e

    pdf_doc = fitz.open(pdf_path)
    try:
        result = _import_pdf_page_inner(pdf_doc, pdf_path, page_num, opts, fc_doc)
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

    page = pdf_doc.load_page(page_num - 1)
    page_h = page.rect.height
    scale = (MM_PER_PT if opts.scale_to_mm else 1.0) * opts.user_scale

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
    if effective_mode == "auto":
        # Auto-detect: profile the page content to choose best mode
        _progress_update(2, "Analyzing page content...")
        n_text_blocks = 0
        try:
            # blocks is far cheaper than dict on text-heavy shop drawings.
            blocks = page.get_text("blocks") or []
            n_text_blocks = sum(1 for b in blocks if len(b) >= 7 and b[6] == 0)
        except Exception as e:
            _warn(f"get_text(blocks) failed during auto-mode: {e}")
            n_text_blocks = 0

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
            n_drawings, n_text_blocks, n_words, vg_stats)
        fill_art_flood = _looks_like_fill_art_flood(n_drawings, vg_stats)

        _flood_reason = ""  # human-readable explanation for the log

        if n_drawings < 5 and n_text_blocks < 3:
            # Scanned / pure raster page — no usable vector content
            effective_mode = "raster"
            _flood_reason = "scanned/raster page (no usable vector content)"

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

    if _progress_check_cancel():
        if progress:
            progress.close()
        fc_doc.recompute()
        return top_group

    # ── Raster-only mode ──
    if effective_mode == "raster":
        _msg(f"Page {page_num}: rendering at {opts.raster_dpi} DPI (raster mode)")
        _progress_update(5, f"Rendering raster image at {opts.raster_dpi} DPI...")
        _import_page_as_raster(
            pdf_doc, page, page_num, page_h, opts, scale,
            top_group or fc_doc, fc_doc)
        if progress:
            progress.setValue(100)
            progress.close()
        fc_doc.recompute()
        _msg(f"Page {page_num}: imported as raster image")
        return top_group

    # ── Hybrid mode: place raster background, then overlay vectors ──
    if effective_mode == "hybrid":
        _msg(f"Page {page_num}: placing {opts.raster_dpi} DPI raster background…")
        _progress_update(5, f"Rendering raster image at {opts.raster_dpi} DPI...")
        _import_page_as_raster(
            pdf_doc, page, page_num, page_h, opts, scale,
            top_group or fc_doc, fc_doc)
        _msg(f"Page {page_num}: overlaying vector geometry…")
        # Fall through to vector import below

    # ── Legacy raster fallback (vectors mode, backwards compat) ──
    if effective_mode == "vector" and opts.raster_fallback and n_drawings < 5:
        tdict = page.get_text("dict")
        n_text = sum(1 for b in tdict.get("blocks", []) if b.get("type") == 0)
        if n_text < 3:
            _msg(f"Page {page_num}: appears to be scanned/raster — "
                 f"rendering at {opts.raster_dpi} DPI")
            _progress_update(5, f"Rendering raster image at {opts.raster_dpi} DPI...")
            _import_page_as_raster(
                pdf_doc, page, page_num, page_h, opts, scale,
                top_group or fc_doc, fc_doc)
            if progress:
                progress.setValue(100)
                progress.close()
            fc_doc.recompute()
            _msg(f"Page {page_num}: imported as raster image")
            return top_group

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
    _batch_styles: Dict[str, Tuple] = {}   # style_key → (stroke_rgb, width, dashes)
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
            stroke_rgb, width, dashes = _batch_styles.get(key, (None, None, None))
            try:
                compound = Part.makeCompound(shapes)
                obj = fc_doc.addObject("Part::Feature", f"Batch_{idx}")
                obj.Shape = compound
                _apply_style(obj, stroke_rgb, width, dashes, opts)
                parent.addObject(obj)
                obj_count += 1
            except (RuntimeError, ValueError, TypeError) as e:
                # Fallback: create individually if compound fails
                _warn(f"Compound batch failed ({len(shapes)} shapes): {e}")
                for shp in shapes:
                    try:
                        obj = fc_doc.addObject("Part::Feature", "Wire")
                        obj.Shape = shp
                        _apply_style(obj, stroke_rgb, width, dashes, opts)
                        parent.addObject(obj)
                        obj_count += 1
                    except (RuntimeError, ValueError, TypeError):
                        pass
            _batch_shapes[key] = []

    def _add_to_batch(shape, parent, stroke_rgb, width, dashes):
        """Add a shape to the batch or create immediately if batching disabled."""
        nonlocal obj_count
        if not _batch_size:
            # No batching — original behavior
            obj = fc_doc.addObject("Part::Feature", "Wire")
            obj.Shape = shape
            _apply_style(obj, stroke_rgb, width, dashes, opts)
            parent.addObject(obj)
            obj_count += 1
            return
        parent_name = parent.Name if hasattr(parent, 'Name') else str(id(parent))
        # Build a style key so shapes with different visual styles stay separate
        dash_key = tuple(dashes) if dashes else ()
        style_key = f"{parent_name}|{stroke_rgb}|{width}|{dash_key}"
        if style_key not in _batch_shapes:
            _batch_shapes[style_key] = []
            _batch_parents[style_key] = parent
            _batch_styles[style_key] = (stroke_rgb, width, dashes)
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
                return top_group

        items = path_group.get("items", [])
        if not items:
            continue

        # PyMuPDF may include clip/group container entries in drawing streams.
        # These are not visible edges and should never become CAD geometry.
        grp_type = str(path_group.get("type", "") or "").lower()
        if grp_type in {"clip", "group"}:
            continue

        stroke = path_group.get("color") or path_group.get("stroke")
        stroke_rgb = _norm_color(stroke)
        fill = path_group.get("fill")
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

        parent = _parent_for(stroke_rgb, layer_name)

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
                    _add_to_batch(wire, parent, stroke_rgb, width, dashes)
                except (RuntimeError, ValueError, TypeError, AttributeError):
                    pass
            else:
                # Faces and non-batchable shapes: create individually
                obj = _make_shape_obj(edges, is_closed, make_face=want_face, fc_doc=fc_doc)
                if obj is not None:
                    _apply_style(obj, stroke_rgb, width, dashes, opts)
                    parent.addObject(obj)
                    obj_count += 1

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
                    return top_group
                _flush_batch(_fk, force=True)
        if opts.verbose:
            total_batches = sum(_batch_idx.values())
            _msg(f"Page {page_num}: geometry batched into {total_batches} "
                 f"compound(s) (batch_size={_batch_size})")

    # ── Text import ──
    if opts.import_text and opts.text_mode != "none":
        _progress_update(86, "Importing text...")

        if _progress_check_cancel():
            fc_doc.recompute()
            return top_group

        # Try pdftocairo vector text (Glyphs / Geometry modes)
        svg_text_done = False
        if opts.text_mode in ("glyphs", "geometry"):
            try:
                from PDFVectorImporter.src.PDFSvgTextRenderer import render_text
                text_parent = top_group or fc_doc
                label = "text geometry" if opts.text_mode == "geometry" else "text glyphs"
                _progress_update(87, f"Rendering {label} via pdftocairo...")
                result = render_text(
                    pdf_path, page_num, page_h, scale, page.rect.width,
                    fc_doc=fc_doc, parent_group=text_parent, flip_y=opts.flip_y)
                if result and result.get("glyphs", 0) > 0:
                    svg_text_done = True
                    obj_count += 1
                    n_glyphs = result['glyphs']
                    _progress_update(
                        89,
                        f"Rendering {label} ({n_glyphs} items)...")
                    if opts.verbose:
                        _msg(f"  Text: {result['glyphs']} glyphs from "
                             f"{result['shapes']} unique shapes (pdftocairo)")
            except (RuntimeError, ValueError, TypeError, OSError, ImportError, AttributeError) as e:
                _warn(f"SVG text renderer failed, falling back to Draft text: {e}")

        # Fall back to Draft text (Labels mode, or if pdftocairo unavailable)
        if not svg_text_done:
          try:
            tdict = _preprocess_text_blocks(page.get_text("dict"))
            text_group = _make_group(top_group or fc_doc, "Text", fc_doc)

            # High-fidelity Labels path: render each PDF span at its exact origin.
            # This preserves stacked fractions and micro-positioning much closer
            # to the source PDF than reconstructed line text.
            prefer_exact_labels = bool(getattr(opts, "strict_text_fidelity", True))
            _env_exact = os.environ.get("BC_PDF_EXACT_LABELS", "").strip().lower()
            if _env_exact:
                prefer_exact_labels = _env_exact not in ("0", "false", "off", "no")
            exact_span_count = 0
            if prefer_exact_labels:
                exact_span_count = _render_text_spans_exact_labels(
                    tdict, text_group, page_h, opts, scale
                )
                if exact_span_count > 0:
                    obj_count += exact_span_count
                    _progress_update(
                        89,
                        f"Rendering text labels ({exact_span_count} spans)...")
                    if opts.verbose:
                        _msg(f"  Text: {exact_span_count} span labels (exact placement)")
                elif opts.verbose:
                    _warn("Strict text fidelity enabled, but exact span labels produced 0 items.")
                # In strict mode, never run legacy line reconstruction.
                tdict["blocks"] = []
            else:
                # Route rotated text through exact span placement even when
                # strict mode is off. Legacy reconstruction remains for
                # horizontal text only.
                prefer_rotated_exact = True
                _env_rot = os.environ.get("BC_PDF_ROTATED_EXACT_LABELS", "").strip().lower()
                if _env_rot:
                    prefer_rotated_exact = _env_rot not in ("0", "false", "off", "no")
                if prefer_rotated_exact:
                    rotated_span_count = _render_text_spans_exact_labels(
                        tdict, text_group, page_h, opts, scale, only_rotated=True
                    )
                    if rotated_span_count > 0 and opts.verbose:
                        _msg(f"  Text: {rotated_span_count} rotated span labels (exact placement)")
                    obj_count += rotated_span_count

            for block in tdict.get("blocks", []):
                if block.get("type") != 0:
                    continue

                # Group lines within this block by Y + X proximity, but ONLY
                # merge when at least one line is a fraction fragment (pure digits
                # or "/").  This prevents BOM table cells from merging while still
                # recombining split fractions with their main dimension text.
                block_lines = block.get("lines", [])
                if not prefer_exact_labels:
                    horizontal_lines = []
                    rotated_threshold = _rotated_text_threshold_deg()
                    for _ln in block_lines:
                        _ang = _line_angle_deg(_ln)
                        # Keep legacy reconstruction for horizontal-ish lines.
                        # Rotated/diagonal lines are handled via exact spans above.
                        if abs(_normalize_text_angle_deg(_ang)) < rotated_threshold:
                            horizontal_lines.append(_ln)
                    block_lines = horizontal_lines
                y_groups: List[List] = []

                def _is_frac_fragment(ln, main_sz=0) -> bool:
                    """Is this line a fraction part (small-font orphaned digits or slash)?"""
                    spans = ln.get("spans", [])
                    txt = "".join(s.get("text", "") for s in spans).strip()
                    if txt == "/":
                        return True
                    # Pure digits at SMALLER font size = fraction numerator/denominator
                    if txt.isdigit() and spans:
                        span_size = float(spans[0].get("size", 0))
                        if main_sz > 0 and span_size < main_sz * 0.95:
                            return True
                    return False

                # Find the dominant font size in this block for reference
                block_main_size = 0
                for ln in block_lines:
                    for s in ln.get("spans", []):
                        sz = float(s.get("size", 0))
                        if sz > block_main_size:
                            block_main_size = sz

                for line in block_lines:
                    spans = line.get("spans", [])
                    if not spans:
                        continue
                    origin = spans[0].get("origin")
                    ly = origin[1] if origin else line.get("bbox", (0, 0, 0, 0))[1]
                    lbbox = line.get("bbox", (0, 0, 0, 0))
                    lx_start = lbbox[0]
                    lx_end = lbbox[2]
                    line_is_frac = _is_frac_fragment(line, block_main_size)

                    placed = False
                    for grp in y_groups:
                        _ref_spans = grp[0].get("spans") or []
                        ref_origin = _ref_spans[0].get("origin") if _ref_spans else None
                        ref_y = ref_origin[1] if ref_origin else grp[0].get("bbox", (0, 0, 0, 0))[1]
                        if abs(ly - ref_y) < 2.0:  # same Y
                            grp_has_frac = any(_is_frac_fragment(g, block_main_size) for g in grp)
                            grp_has_nonfrac = any(not _is_frac_fragment(g, block_main_size) for g in grp)

                            # Only merge if at least one side is a fraction fragment.
                            # NEVER merge two non-fragment lines into the same group —
                            # that creates jumbled span sequences like "6'-9 15/16 (PIPE1-..."
                            can_merge = False
                            if line_is_frac:
                                can_merge = True  # fragments always welcome
                            elif grp_has_frac and not grp_has_nonfrac:
                                can_merge = True  # non-frag joining a frag-only group
                            # else: non-frag trying to join a group that already has a non-frag → refuse

                            if can_merge:
                                for existing in grp:
                                    eb = existing.get("bbox", (0, 0, 0, 0))
                                    gap = min(abs(lx_start - eb[2]), abs(eb[0] - lx_end))
                                    if gap < 20:
                                        grp.append(line)
                                        placed = True
                                        break
                            if placed:
                                break
                    if not placed:
                        y_groups.append([line])

                # Count sibling groups on the same Y level so short horizontal
                # text is not accidentally centered into neighboring runs.
                _grp_y_map: Dict[int, int] = {}
                for grp in y_groups:
                    if not grp:
                        continue
                    spans0 = grp[0].get("spans", []) or []
                    ref_o = spans0[0].get("origin") if spans0 else None
                    gy = round(ref_o[1] if ref_o else grp[0].get("bbox", (0,0,0,0))[1])
                    _grp_y_map[gy] = _grp_y_map.get(gy, 0) + 1

                # Build layout items first so we can resolve same-line overlap
                layout_items = []
                for grp in y_groups:
                    def _line_x(ln):
                        o = ln.get("spans", [{}])[0].get("origin")
                        return o[0] if o else ln.get("bbox", (0, 0, 0, 0))[0]
                    grp.sort(key=_line_x)

                    all_spans = []
                    for line in grp:
                        all_spans.extend(line.get("spans", []))
                    if not all_spans:
                        continue

                    content = _reconstruct_line_text(all_spans)
                    if not content.strip() or content.strip() == "/":
                        continue

                    all_x0 = min(ln.get("bbox", (9e9,0,0,0))[0] for ln in grp)
                    all_x1 = max(ln.get("bbox", (0,0,-9e9,0))[2] for ln in grp)
                    all_y1 = max(ln.get("bbox", (0,0,0,-9e9))[3] for ln in grp)
                    _stripped = content.strip()
                    is_short = len(_stripped) <= 40

                    # Use span origins to recover the PDF baseline. Some OCR /
                    # generated PDFs contain outlier span origins, so we use the
                    # median origin and sanity-check it against a bbox-derived
                    # baseline estimate.
                    _first_span = grp[0].get("spans", [{}])[0]
                    origin_xy = []
                    for _sp in all_spans:
                        _o = _sp.get("origin")
                        if _o and len(_o) >= 2:
                            try:
                                origin_xy.append((float(_o[0]), float(_o[1])))
                            except (TypeError, ValueError):
                                pass

                    size_pt = max(float(s.get("size", 3)) for s in all_spans)
                    _desc_abs = abs(float(_first_span.get("descender", 0.15)))
                    baseline_from_bbox = all_y1 - _desc_abs * size_pt

                    if origin_xy:
                        ys = sorted(p[1] for p in origin_xy)
                        mid = len(ys) // 2
                        if len(ys) % 2 == 1:
                            baseline_from_origin = ys[mid]
                        else:
                            baseline_from_origin = (ys[mid - 1] + ys[mid]) * 0.5

                        # If origin baseline disagrees strongly with bbox-based
                        # estimate, trust bbox. This prevents occasional low/high
                        # label drift in title blocks.
                        drift = abs(baseline_from_origin - baseline_from_bbox)
                        drift_tol = max(0.9, size_pt * 0.28)
                        baseline_from_origin_used = drift <= drift_tol
                        baseline_y = (
                            baseline_from_bbox
                            if drift > drift_tol
                            else baseline_from_origin
                        )
                    else:
                        baseline_from_origin_used = False
                        baseline_y = baseline_from_bbox

                    text_dir = grp[0].get("dir", (1.0, 0.0))
                    if text_dir and len(text_dir) >= 2:
                        dx, dy = float(text_dir[0]), float(text_dir[1])
                        is_horizontal = _is_near_horizontal(dx, dy)
                        angle_deg = -math.degrees(math.atan2(dy, dx))
                    else:
                        dx, dy = 1.0, 0.0
                        is_horizontal = True
                        angle_deg = 0.0

                    _spans0 = grp[0].get("spans", []) or []
                    _ref_o = _spans0[0].get("origin") if _spans0 else None
                    _gy = round(_ref_o[1] if _ref_o else grp[0].get("bbox", (0,0,0,0))[1])
                    has_siblings = _grp_y_map.get(_gy, 1) > 1

                    if is_short and is_horizontal and not has_siblings:
                        x_pdf = (all_x0 + all_x1) / 2.0
                        justification = "Center"
                    else:
                        # Left-anchored rows should start from the true left-most
                        # text origin, not whichever span happened to come first.
                        if (not is_horizontal) and origin_xy and baseline_from_origin_used:
                            dlen = math.hypot(dx, dy)
                            if dlen <= 1e-12:
                                ux, uy = 1.0, 0.0
                            else:
                                ux, uy = dx / dlen, dy / dlen
                            anchor = min(origin_xy, key=lambda p: (p[0] * ux + p[1] * uy))
                            x_pdf = float(anchor[0])
                            baseline_y = float(anchor[1])
                        else:
                            x_pdf = min((p[0] for p in origin_xy), default=all_x0)
                        justification = "Left"

                    font = _normalize_pdf_font_name(all_spans[0].get("font", ""))
                    # Grab PyMuPDF normalised font metrics for baseline offset
                    _asc = float(all_spans[0].get("ascender", 0.8))
                    _desc = float(all_spans[0].get("descender", -0.2))
                    if "/" in _stripped and _stripped.replace("/", "").isdigit():
                        size_pt *= 0.65
                    elif not is_horizontal and " " in _stripped and "/" in _stripped:
                        size_pt *= 0.85

                    font_size_fc = size_pt * scale
                    render_width_pdf = _estimate_text_width_mm(content, font_size_fc) / max(scale, 1e-12)
                    orig_width_pdf = max(0.0, all_x1 - all_x0)

                    layout_items.append({
                        "content": content,
                        "font": font,
                        "font_size_fc": font_size_fc,
                        "angle_deg": angle_deg,
                        "is_horizontal": is_horizontal,
                        "baseline_y_pdf": baseline_y,
                        "baseline_is_origin": bool(origin_xy and baseline_from_origin_used),
                        "x_pdf": x_pdf,
                        "orig_width_pdf": orig_width_pdf,
                        "render_width_pdf": render_width_pdf,
                        "justification": justification,
                        "eligible_for_nudge": bool(is_horizontal and justification == "Left" and has_siblings),
                        "line_key": _gy,
                        "ascender": _asc,
                        "descender": _desc,
                    })

                layout_items = _resolve_horizontal_run_overlaps(layout_items, scale)
                layout_items = _apply_vertical_mixed_fraction_compaction(layout_items, scale)

                for item in layout_items:
                    pos = _to_fc((item["x_pdf"], item["baseline_y_pdf"]), page_h, opts, scale)
                    # Draft.make_text anchors at the bottom-left of the text
                    # box, but we have the PDF baseline position.  Shift down
                    # (in FreeCAD Y-up space) by the descender so the glyph
                    # baseline lands where the PDF specified it.
                    # SKIP this correction when baseline came from span origins
                    # (which are already the true baseline — no descender needed).
                    if not item.get("baseline_is_origin", False):
                        _d = _effective_descender(
                            item["content"],
                            float(item.get("descender", -0.2)),
                        )
                        pos = _apply_text_local_y_offset(
                            pos,
                            float(item.get("angle_deg", 0.0)),
                            _d * float(item["font_size_fc"]),
                        )
                    try:
                        rot = Rotation(Vector(0, 0, 1), item["angle_deg"])
                        t = Draft.make_text([item["content"]], placement=Placement(pos, rot))
                        try:
                            t.ViewObject.FontSize = item["font_size_fc"]
                            if item["font"]:
                                t.ViewObject.FontName = item["font"]
                            t.ViewObject.Justification = item["justification"]
                        except (AttributeError, RuntimeError, TypeError, ValueError):
                            pass
                        text_group.addObject(t)
                        obj_count += 1
                    except (RuntimeError, ValueError, TypeError, AttributeError):
                        pass
          except (RuntimeError, ValueError, TypeError, AttributeError) as e:
            _warn(f"Text import failed: {e}")

    # ── Build hatch group (if group mode) ──
    if hatch_drawings and opts.hatch_mode == "group":
        try:
            hatch_group = _make_group(top_group or fc_doc, "Hatching", fc_doc)
            for _pg_idx, path_group in enumerate(hatch_drawings):
                items = path_group.get("items", [])
                if not items:
                    continue
                stroke = path_group.get("color") or path_group.get("stroke")
                stroke_rgb = _norm_color(stroke)
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
                                if stroke_rgb:
                                    try:
                                        obj.ViewObject.LineColor = stroke_rgb
                                    except (AttributeError, RuntimeError, TypeError):
                                        pass
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
    if not opts.ignore_images:
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
    return top_group


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


def import_pdf(pdf_path: str, opts: Optional[ImportOptions] = None):
    """Import one or more pages from a PDF file."""
    if opts is None:
        opts = ImportOptions(ignore_images=not IMAGE_WB)
    fc_doc = _ensure_doc()
    t_import_start = time.perf_counter()
    obj_count_before = len(fc_doc.Objects)

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
    try:
        with fitz.open(pdf_path) as pdoc:
            total_pages = len(pdoc)
            # Default to all pages when no explicit page list is provided
            pages = opts.pages or list(range(1, total_pages + 1))
            if total_pages > 0:
                page_height_scaled = pdoc.load_page(0).rect.height * _unit_scale
            for p in pages:
                if 1 <= p <= total_pages:
                    try:
                        page_heights_scaled[p] = pdoc.load_page(p - 1).rect.height * _unit_scale
                    except (ValueError, RuntimeError):
                        pass
    except (RuntimeError, OSError) as e:
        _err(f"Cannot open PDF: {e}")
        return

    # Wrap entire import in a FreeCAD transaction so Ctrl+Z undoes it in one step
    fc_doc.openTransaction("Import PDF")
    try:
        imported_count = 0
        running_stack_offset = 0.0
        page_arrangement = _normalize_page_arrangement(getattr(opts, "page_arrangement", "spread"))
        page_gap_ratio = _normalize_page_gap_ratio(getattr(opts, "page_gap_ratio", 0.20))
        first_page = True
        for p in pages:
            if p < 1 or p > total_pages:
                _warn(f"Skipping out-of-range page {p} (PDF has {total_pages} pages)")
                continue
            try:
                _msg(f"Importing page {p}/{total_pages} ({imported_count+1} of {len(pages)})...")
                import_pdf_page(pdf_path, page_num=p, opts=opts, autofit=False)
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
            except (RuntimeError, OSError, ValueError, TypeError, AttributeError) as e:
                _err(f"Failed to import page {p}: {e}\n{traceback.format_exc()}")
        fc_doc.commitTransaction()
    except Exception:
        fc_doc.abortTransaction()
        raise

    fc_doc.recompute()
    _autofit_import_view(fc_doc)

    if opts.import_mode == "auto" and opts.auto_resolved_mode:
        _msg(
            f"Auto mode summary: {opts.auto_resolved_mode}"
            + (f" — {opts.auto_reason}" if opts.auto_reason else "")
        )

    try:
        report_path = opts.import_report_path or _default_import_report_path(pdf_path)
        fallback_used = (
            opts.import_mode == "raster"
            or opts.auto_resolved_mode == "raster"
        )
        write_import_report(
            pdf_path=pdf_path,
            output_path=report_path,
            opts=opts,
            pages_imported=imported_count,
            total_pages=total_pages,
            primitive_count=max(0, len(fc_doc.Objects) - obj_count_before),
            elapsed_ms=(time.perf_counter() - t_import_start) * 1000.0,
            fallback_used=fallback_used,
            fallback_reason=opts.auto_reason if fallback_used else None,
        )
        if opts.verbose:
            _msg(f"Import report: {report_path}")
    except (OSError, RuntimeError, TypeError, ValueError, ImportError) as e:
        _warn(f"Import report write failed: {e}")

    return True
