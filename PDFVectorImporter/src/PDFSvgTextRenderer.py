# -*- coding: utf-8 -*-
# PDFSvgTextRenderer.py — Pixel-perfect text via SVG glyph paths
# BlueCollar Systems — BUILT. NOT BOUGHT.
#
# Renders text as vector glyph outlines using pdftocairo, or bundled PyMuPDF
# when Poppler is absent.
# Each unique glyph is built once as a Part.Shape, then translated
# and compounded into a single Part::Feature for all text on the page.
#
# Falls back gracefully to caller-provided label text if no SVG renderer is
# available.

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional

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


def render_text(pdf_path: str, page_num: int, page_h: float,
                scale: float, page_w: Optional[float] = None,
                fc_doc=None, parent_group=None,
                flip_y: bool = True) -> Optional[dict]:
    """Render text from a PDF page as vector glyph geometry.

    Returns {"shapes": int, "glyphs": int} or None if unavailable.
    """
    exe = find_pdftocairo()
    renderer_name = "pdftocairo" if exe else "pymupdf"

    doc = fc_doc or FreeCAD.ActiveDocument
    if doc is None:
        return None

    svg = None
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
        return None
    if _svg_too_large(svg):
        if FreeCAD:
            FreeCAD.Console.PrintWarning(
                f"PDFSvgTextRenderer: page {page_num} SVG text payload is too large — "
                "falling back to editable labels.\n"
            )
        return None

    # Parse SVG dimensions / viewBox
    vb_min_x, vb_min_y, vb_w, vb_h = _parse_viewbox(svg)
    if vb_w <= 0 or vb_h <= 0:
        svg_w = _parse_svg_dim(svg, "width", page_w if page_w and page_w > 0 else page_h)
        svg_h = _parse_svg_dim(svg, "height", page_h)
        vb_min_x, vb_min_y, vb_w, vb_h = 0.0, 0.0, float(svg_w), float(svg_h)

    page_w_eff = float(page_w) if page_w and page_w > 0 else float(vb_w)
    page_h_eff = float(page_h) if page_h and page_h > 0 else float(vb_h)
    x_unit_to_mm = (page_w_eff * scale) / max(vb_w, 1e-12)
    y_unit_to_mm = (page_h_eff * scale) / max(vb_h, 1e-12)

    # Parse glyph definitions
    glyph_defs = _parse_glyph_defs(svg)

    # Parse use placements
    placements = _parse_use_placements(svg)

    if not placements:
        return {"shapes": 0, "glyphs": 0, "renderer": renderer_name}

    # Build Part.Shape for each unique glyph
    glyph_shapes: Dict[str, Part.Shape] = {}
    for gid, path_d in glyph_defs.items():
        edges = _svg_path_to_edges(path_d, x_unit_to_mm, y_unit_to_mm)
        if edges:
            try:
                compound = Part.makeCompound(edges)
                glyph_shapes[gid] = compound
            except (RuntimeError, ValueError, TypeError):
                pass

    # Place all glyphs
    all_shapes = []
    glyph_count = 0

    for gid, use_x, use_y, matrix in placements:
        shape = glyph_shapes.get(gid)
        if shape is None:
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
                all_shapes.append(placed)
                glyph_count += 1
        except (AttributeError, RuntimeError, TypeError):
            pass

    if not all_shapes:
        return {"shapes": 0, "glyphs": 0}

    # Combine all text into one compound object
    try:
        text_compound = Part.makeCompound(all_shapes)
        text_obj = doc.addObject("Part::Feature", "Text_Glyphs")
        text_obj.Shape = text_compound
        try:
            text_obj.ViewObject.LineWidth = 1.0
            text_obj.ViewObject.LineColor = (0.0, 0.0, 0.0)
        except (AttributeError, RuntimeError, TypeError):
            pass
        if parent_group:
            parent_group.addObject(text_obj)
        return {"shapes": len(glyph_shapes), "glyphs": glyph_count, "renderer": renderer_name}
    except (RuntimeError, ValueError, TypeError):
        return None


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
                "falling back to editable labels.\n"
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


def _parse_glyph_defs(svg: str) -> Dict[str, str]:
    glyph_defs: Dict[str, str] = {}
    for gid, path_d in re.findall(
            r'<g id="([^"]+)">\s*<path d="([^"]*)"', svg, re.DOTALL):
        if _glyph_reference_id(gid) and path_d.strip():
            glyph_defs[gid] = path_d

    for tag in re.findall(r'<path\b[^>]*>', svg, re.IGNORECASE | re.DOTALL):
        id_m = re.search(r'\bid="([^"]+)"', tag, re.IGNORECASE)
        d_m = re.search(r'\bd="([^"]*)"', tag, re.IGNORECASE | re.DOTALL)
        if not id_m or not d_m:
            continue
        gid = id_m.group(1)
        path_d = d_m.group(1)
        if _glyph_reference_id(gid) and path_d.strip():
            glyph_defs[gid] = path_d
    return glyph_defs


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


def _parse_use_placements(svg: str):
    placements = []
    for m in re.finditer(r'<use\b[^>]*>', svg, re.IGNORECASE | re.DOTALL):
        tag = m.group(0)
        href_m = re.search(r'(?:xlink:href|href)="([^"]+)"', tag, re.IGNORECASE)
        if not href_m:
            continue
        href = href_m.group(1).strip()
        if not href.startswith("#"):
            continue
        gid = href[1:]
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
    tokens = re.findall(r'[MLHVCSZmlhvcsz]|[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?', d)
    edges = []
    subpath_pts = []
    start_pt = None
    cx, cy = 0.0, 0.0
    cmd = None
    nums: List[float] = []
    prev_cubic_cp2: Optional[List[float]] = None  # second control point of previous cubic (in abs coords)

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

    def run():
        nonlocal cx, cy, start_pt, subpath_pts, is_relative, prev_cubic_cp2

        if cmd == "M":
            prev_cubic_cp2 = None
            while len(nums) >= 2:
                flush_subpath()
                nx, ny = nums.pop(0), nums.pop(0)
                if is_relative:
                    cx, cy = cx + nx, cy + ny
                else:
                    cx, cy = nx, ny
                start_pt = mk(cx, cy)
                subpath_pts = [start_pt]
                # After first M pair, implicit coords are treated as L
                is_relative = is_relative  # keep relative state for implicit L
        elif cmd == "L":
            prev_cubic_cp2 = None
            while len(nums) >= 2:
                nx, ny = nums.pop(0), nums.pop(0)
                if is_relative:
                    cx, cy = cx + nx, cy + ny
                else:
                    cx, cy = nx, ny
                subpath_pts.append(mk(cx, cy))
        elif cmd == "H":
            prev_cubic_cp2 = None
            while nums:
                nx = nums.pop(0)
                if is_relative:
                    cx = cx + nx
                else:
                    cx = nx
                subpath_pts.append(mk(cx, cy))
        elif cmd == "V":
            prev_cubic_cp2 = None
            while nums:
                ny = nums.pop(0)
                if is_relative:
                    cy = cy + ny
                else:
                    cy = ny
                subpath_pts.append(mk(cx, cy))
        elif cmd == "C":
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
                chord = p0.distanceToPoint(p3)
                n = 6 if chord < 0.5 else (8 if chord < 2.0 else 12)
                for i in range(1, n + 1):
                    t = i / n
                    mt = 1.0 - t
                    bx = mt**3*p0.x + 3*mt**2*t*p1.x + 3*mt*t**2*p2.x + t**3*p3.x
                    by = mt**3*p0.y + 3*mt**2*t*p1.y + 3*mt*t**2*p2.y + t**3*p3.y
                    pt = Vector(bx, by, 0.0)
                    subpath_pts.append(pt)
                prev_cubic_cp2 = [x2, y2]
                cx, cy = x, y
        elif cmd == "S":
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
                chord = p0.distanceToPoint(p3)
                n = 6 if chord < 0.5 else (8 if chord < 2.0 else 12)
                for i in range(1, n + 1):
                    t = i / n
                    mt = 1.0 - t
                    bx = mt**3*p0.x + 3*mt**2*t*p1.x + 3*mt*t**2*p2.x + t**3*p3.x
                    by = mt**3*p0.y + 3*mt**2*t*p1.y + 3*mt*t**2*p2.y + t**3*p3.y
                    pt = Vector(bx, by, 0.0)
                    subpath_pts.append(pt)
                prev_cubic_cp2 = [x2, y2]
                cx, cy = x, y
        elif cmd == "Z":
            prev_cubic_cp2 = None
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
