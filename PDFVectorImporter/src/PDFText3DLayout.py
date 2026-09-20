"""Bind native 3D glyph placement to original MuPDF character quads.

This module has no CAD dependency. The caller obtains raw_dict through
read_source_character_geometry, which binds original fz_stext_char geometry
and font metrics to rawdict Unicode/origin occurrences. Missing data is a failure, never
authorization to flatten a positioned span onto a guessed baseline.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque

from PDFTextLayout import _finite, build_source_layout


SCHEMA = "bcs.freecad.source_3d_character_layout/1"


def read_source_character_geometry(page):
    """Read original character quads and their actual source font metrics once.

    MuPDF's horizontal, non-accurate-bbox text extraction constructs UL and LL
    by transforming (0, font ascender) and (0, font descender). Their difference
    divided by that metric difference is therefore the original font-Y vector,
    including anisotropic scale and shear. RAWDICT size is only the matrix's
    geometric expansion and cannot supply that vector. Keep this enrichment
    local to FreeCAD; do not infer source metrics from normalized RAWDICT boxes.
    """
    import pymupdf as fitz

    flags = fitz.TEXTFLAGS_RAWDICT
    if flags & (fitz.TEXT_ACCURATE_BBOXES | fitz.TEXT_ACCURATE_SIDE_BEARINGS):
        raise ValueError("source extraction flags do not retain font-metric quads")
    textpage = page.get_textpage(flags=flags)
    raw = textpage.extractRAWDICT()
    queues = defaultdict(deque)
    for block in textpage.this:
        if block.m_internal.type != 0:
            continue
        for line in block:
            for native_char in line:
                char = native_char.m_internal
                origin = (float(char.origin.x), float(char.origin.y))
                quad = tuple((float(point.x), float(point.y)) for point in
                             (char.quad.ul, char.quad.ur, char.quad.lr, char.quad.ll))
                metrics = dict(
                    schema="mupdf_original_font_metrics/1", text=chr(char.c),
                    origin=origin, quad=quad, writing_mode=int(line.m_internal.wmode),
                    size=float(char.size),
                    ascender=float(fitz.mupdf.ll_fz_font_ascender(char.font)),
                    descender=float(fitz.mupdf.ll_fz_font_descender(char.font)),
                )
                queues[(chr(char.c), *origin)].append((quad, metrics))
    for block in raw.get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                for char in span.get("chars", ()):
                    candidates = queues.get((char["c"], *_finite(char["origin"], 2)))
                    if not candidates:
                        raise ValueError("source character has no original metric/quad occurrence")
                    char["quad"], char["source_font_metrics"] = candidates.popleft()
    if any(queues.values()):
        raise ValueError("original source character inventory was not completely bound")
    return raw


def _source_font_metrics(char, quad, span_size):
    metrics = char.get("source_font_metrics")
    if not isinstance(metrics, dict) or metrics.get("schema") != "mupdf_original_font_metrics/1":
        raise ValueError("original source font metrics are unavailable")
    if (metrics.get("text") != char["c"]
            or _finite(metrics.get("origin"), 2) != _finite(char["origin"], 2)
            or tuple(_finite(point, 2) for point in metrics.get("quad", ())) != quad
            or metrics.get("writing_mode") != 0):
        raise ValueError("source font metrics are not bound to horizontal character geometry")
    ascender, descender, size = _finite(
        (metrics.get("ascender"), metrics.get("descender"), metrics.get("size")), 3)
    if ascender <= descender or size <= 0 or size != float(span_size):
        raise ValueError("source font metric dimensions are invalid or do not match the span")
    # Check the exact metric-derived baseline against the original character
    # origin. This also rejects bounds/normalized-metric geometry masquerading
    # as original font-metric quads. The tolerance only permits float32 rounding.
    origin = _finite(char["origin"], 2)
    tolerance = _quad_tolerance(quad)
    ratio = ascender/(ascender-descender)
    if any(abs(quad[0][i] - ratio*(quad[0][i]-quad[3][i]) - origin[i]) > tolerance
           for i in range(2)):
        raise ValueError("source font metrics do not reconstruct the original baseline")
    return ascender, descender


def _quad_tolerance(quad):
    # MuPDF stores fz_quad coordinates as float32. Arithmetic on the four
    # independently rounded corners can differ by a few source-coordinate
    # ULPs; this validates binding, and never snaps or changes any coordinate.
    magnitude = max(abs(value) for point in quad for value in point)
    return max(1e-7, 4.0 * math.ldexp(1., math.frexp(magnitude)[1] - 24))


def _source_quad(char):
    raw = char.get("quad")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("source character quad is unavailable")
    quad = tuple(_finite(point, 2) for point in raw)
    bbox = _finite(char.get("bbox"), 4)
    tolerance = _quad_tolerance(quad)
    if bbox[2] < bbox[0] or bbox[3] < bbox[1]:
        raise ValueError("source character bbox is invalid")
    # RAWDICT boxes use normalized font metrics and need not equal original
    # fz_quad bounds. Actual fz_char origin is on the quad's left baseline
    # edge, giving an independent positional check without fitting to a box.
    ox, oy = _finite(char.get("origin"), 2)
    vx, vy = quad[3][0]-quad[0][0], quad[3][1]-quad[0][1]
    length = math.hypot(vx, vy)
    if length <= 0:
        raise ValueError("source character quad height is degenerate")
    perpendicular = abs(vx*(oy-quad[0][1])-vy*(ox-quad[0][0]))/length
    if perpendicular > tolerance:
        raise ValueError("source character quad is not bound to its raw origin")
    # UL, UR, LR, LL must describe one affine glyph plane. Reject arbitrary
    # quadrilateral distortion that two native basis vectors cannot reproduce.
    if any(abs(quad[0][i]+quad[2][i]-quad[1][i]-quad[3][i]) > tolerance
           for i in range(2)):
        raise ValueError("source character quad is not affine")
    return quad


def _mark_source_character(error, index, character):
    """Name the exact source character a layout failure is about.

    A degraded text item has to say which character it could not place; the
    message alone ("source character advance is degenerate") does not.
    """
    error.source_character_index = int(index)
    error.source_character_codepoint = (
        "U+%04X" % ord(character)
        if isinstance(character, str) and len(character) == 1
        else ""
    )
    return error


def build_source_character_layout(item, raw_dict, *, scale, font_size, font_name,
                                  host_rotation_deg, flip_y=True,
                                  page_matrix=(1., 0., 0., 1., 0., 0.)):
    """Return exact local origins and affine axes for every source character.

    Translation belongs to the host object. Positions are relative to the
    canonical span origin, after page transform/optional Y flip and inverse
    host rotation. Quad width is a source advance, not an ink-box fit. The
    supplied nominal font_size is retained, including an explicit text scale;
    up_scale separately restores the original source font-Y matrix magnitude.
    """
    layout = build_source_layout(item, raw_dict, scale=scale, font_size=font_size,
                                 font_name=font_name, host_rotation_deg=host_rotation_deg,
                                 flip_y=flip_y, page_matrix=page_matrix)
    a, b, c, d, _e, _f = _finite(page_matrix, 6)
    if a*d-b*c == 0:
        raise ValueError("source page transform is singular")
    scale = float(scale)
    angle = math.radians(float(host_rotation_deg))
    cosine, sine = math.cos(angle), math.sin(angle)
    flip = -1. if flip_y else 1.

    def local_vector(vector):
        x, y = vector
        dx, dy = (a*x+c*y)*scale, (b*x+d*y)*scale*flip
        result = cosine*dx+sine*dy, -sine*dx+cosine*dy
        if not all(math.isfinite(value) for value in result):
            raise ValueError("source character transform is not finite")
        return result

    def unit(vector, label):
        length = math.hypot(*vector)
        if not math.isfinite(length) or length <= 0:
            raise ValueError("source character " + label + " is degenerate")
        return length, [component/length for component in vector]

    span = raw_dict["blocks"][item["block_index"]]["lines"][item["line_index"]]["spans"][item["span_index"]]
    characters = []
    for index, (char, placement) in enumerate(zip(span["chars"], layout["characters"], strict=True)):
        quad = _source_quad(char)
        baseline = local_vector((quad[1][0]-quad[0][0], quad[1][1]-quad[0][1]))
        up = local_vector((quad[0][0]-quad[3][0], quad[0][1]-quad[3][1]))
        height, up_axis = unit(up, "height")
        ascender, descender = _source_font_metrics(char, quad, span["size"])
        source_em_height = height/(ascender-descender)
        up_scale = source_em_height/(float(span["size"])*scale)
        if not math.isfinite(up_scale) or up_scale <= 0:
            raise ValueError("source font-Y scale is invalid")
        advance = math.hypot(*baseline)
        # A source zero-width spacing/control character has no native ink;
        # retain its true position and character, without fabricating advance.
        if advance == 0 and char["c"].isspace():
            direction = local_vector(_finite(item["line_direction"], 2))
            _length, baseline_axis = unit(direction, "baseline")
        else:
            try:
                advance, baseline_axis = unit(baseline, "advance")
            except ValueError as error:
                _mark_source_character(error, index, char["c"])
                raise
        determinant = baseline_axis[0]*up_axis[1]-baseline_axis[1]*up_axis[0]
        if not math.isfinite(determinant) or abs(determinant) <= 1e-12:
            raise ValueError("source character axes are collinear")
        # MuPDF char.size = sqrt(abs(det(text matrix))). Recover the original
        # font-X magnitude from that area and the metric-bound font-Y vector.
        # The PDF's declared glyph advance may differ from font-program widths;
        # it is positional evidence, not permission to stretch the glyph ink.
        baseline_scale = abs(a*d-b*c)/(up_scale*abs(determinant))
        if not math.isfinite(baseline_scale) or baseline_scale <= 0:
            raise ValueError("source font-X scale is invalid")
        characters.append(dict(placement, source_character_index=index,
                               source_quad=[list(point) for point in quad],
                               advance=advance, baseline_axis=baseline_axis,
                               up_axis=up_axis, up_scale=up_scale, baseline_scale=baseline_scale,
                               source_quad_height=height, source_em_height=source_em_height,
                               source_font_ascender=ascender, source_font_descender=descender))

    direction = local_vector(_finite(item["line_direction"], 2))
    _length, axis = unit(direction, "line direction")
    distances = [abs(char["local_origin"][0]*axis[1] - char["local_origin"][1]*axis[0])
                 for char in characters]
    # This flag is descriptive only: all coordinates survive unchanged, even
    # below this numerical threshold. It never selects a less accurate path.
    noncollinear = any(value > 1e-7*scale for value in distances)
    return dict(layout, schema=SCHEMA, characters=characters,
                noncollinear=noncollinear,
                max_baseline_offset=max(distances, default=0.),
                source_geometry="original_mupdf_character_quads",
                nominal_font_size_preserved=True)
