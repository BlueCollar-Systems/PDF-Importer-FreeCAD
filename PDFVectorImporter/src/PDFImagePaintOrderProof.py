"""Bounded proof for opaque source images above earlier native PDF paint.

This does not assign arbitrary PDF painter order to an entire 3D scene. Only an
exact, fully visible opaque image and a closed set of later opaque strokes and
source-bound native text may be reordered. Unsupported paint remains unqualified.
"""

from __future__ import annotations

import base64
import hashlib
import math
import re
import struct
import xml.etree.ElementTree as ET

try:
    from .PDFPaintProof import _RECT
    from .PDFNonTextCompositeProof import _source_group_declarations
except ImportError:
    from PDFPaintProof import _RECT
    from PDFNonTextCompositeProof import _source_group_declarations

_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"


def _matrix(value):
    if value is None:
        return _IDENTITY
    if not re.fullmatch(
        r"matrix\(\s*" + _NUMBER + r"(?:[ ,]+" + _NUMBER + r"){5}\s*\)", value
    ):
        raise ValueError("Unsupported SVG transform")
    result = tuple(float(v) for v in re.split(r"[ ,]+", value[7:-1].strip()))
    if (
        not all(math.isfinite(v) for v in result)
        or result[0] * result[3] == result[1] * result[2]
    ):
        raise ValueError("Degenerate SVG transform")
    return result


def _source_text(trace, rawdict, page_number):
    chars = [(int(c[0]), tuple(c[2])) for c in trace.get("chars", ())]
    if not chars or trace.get("type") != 0 or trace.get("opacity") != 1:
        return None
    matches = []
    for bi, block in enumerate(rawdict.get("blocks", [])):
        if block.get("type") != 0:
            continue
        for li, line in enumerate(block.get("lines", [])):
            for si, span in enumerate(line.get("spans", [])):
                layout = span.get("chars", ())
                if len(layout) != len(chars):
                    continue
                if all(len(c["c"]) == 1 and ord(c["c"]) == code
                       and _close(c["origin"], origin)
                       for c, (code, origin) in zip(layout, chars, strict=True)):
                    matches.append(dict(source_item_id=f"p{page_number}:b{bi}:l{li}:s{si}",
                                        text="".join(c["c"] for c in layout),
                                        bbox_pdf=list(span["bbox"]), chars=chars))
    return matches[0] if len(matches) == 1 else None


def stroke_spec(row):
    if (row.get("type") not in ("s", "fs") or row.get("stroke_opacity") != 1
            or row.get("dashes") not in (None, "[] 0")
            or row.get("color") is None or row.get("width", 0) <= 0):
        return None
    items = row.get("items", ())
    if len(items) == 1 and items[0][0] == "re":
        box = tuple(items[0][1])
        x0, y0, x1, y1 = box
        points = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
    else:
        if not items or any(i[0] != "l" or len(i) != 3 for i in items):
            return None
        points = [tuple(items[0][1])]
        for item in items:
            if tuple(item[1]) != points[-1]:
                return None
            points.append(tuple(item[2]))
        if row.get("closePath") and points[-1] != points[0]:
            points.append(points[0])
    if any(a == b for a, b in zip(points, points[1:], strict=False)):
        return None
    return dict(points_pdf=points, color=list(row["color"]), width=row["width"],
                source_draw_order=row["seqno"])


def plan_opaque_images(page, fitz, source_sha256):
    """Prove source occurrences and the complete later overlapping paint set."""
    if re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None:
        raise ValueError("Invalid original PDF image-order identity")
    if int(page.rotation) or tuple(page.rect)[:2] != (0., 0.):
        return []
    inventory = page.get_image_info(hashes=True, xrefs=True)
    if not inventory:
        return []
    try:
        _source_group_declarations(page)
    except ValueError:
        return []
    log = [(kind, _box(bounds)) for kind, bounds in page.get_bboxlog()]
    image_orders = [(i, box) for i, (kind, box) in enumerate(log) if kind == "fill-image"]
    svg = page.get_svg_image(text_as_path=False)
    svg_images = _svg_images(svg, fitz)
    if len(inventory) != len(image_orders) or len(svg_images) != len(inventory):
        return []
    raw = {r["seqno"]: r for r in page.get_drawings()}
    traces = {r["seqno"]: r for r in page.get_texttrace()}
    rawdict = page.get_text("rawdict")
    nonnormal = [r for r in page.get_drawings(extended=True) if r.get("type") == "group"
                 and (r.get("blendmode", "Normal") != "Normal" or r.get("opacity", 1) != 1
                      or r.get("knockout", False))]
    page_quad = _quad((page.rect.width, 0, 0, page.rect.height, 0, 0))
    results = []
    for image_index, (info, (order, bounds), proof) in enumerate(zip(inventory, image_orders, svg_images, strict=True), 1):
        if (proof is None or info.get("has-mask") or not _close(info["bbox"], bounds)
                or bytes(info["digest"]).hex() != proof["pixel_digest"]
                or (info["width"], info["height"]) != (proof["width"], proof["height"])):
            continue
        quad = _quad(tuple(info["transform"]))
        if (not all(_close(a, b) for a, b in zip(quad, proof["quad_pdf"], strict=True))
                or not _contains(page_quad, quad)):
            continue
        # This native consumer supports upright image planes only; do not fit
        # an arbitrary affine image to its axis-aligned bounding rectangle.
        a, b, c, d = quad
        if not (b[0] > a[0] and d[1] > a[1] and _close((a[1], b[0], c[1], d[0]),
                                                       (b[1], c[0], d[1], a[0]))):
            continue
        region, strokes, text = bounds, {}, {}
        eligible = True
        while True:
            before = region
            for seq in range(order+1, len(log)):
                kind, box = log[seq]
                if not _intersects(region, box) or kind == "ignore-text":
                    continue
                if kind in ("fill-text", "stroke-text"):
                    item = _source_text(traces.get(seq, {}), rawdict, page.number+1)
                    if item is None or kind != "fill-text":
                        eligible = False
                        break
                    text[seq] = item
                    continue
                row = raw.get(seq) or raw.get(seq-1)
                if row is None:
                    eligible = False
                    break
                if kind == "fill-path" and seq == row["seqno"] and row.get("fill_opacity") == 0:
                    continue
                spec = stroke_spec(row)
                if (kind != "stroke-path" or spec is None
                        or seq != row["seqno"] + (row["type"] == "fs")):
                    eligible = False
                    break
                strokes[seq] = dict(spec, paint_bounds_pdf=box)
                region = (min(region[0], box[0]), min(region[1], box[1]),
                          max(region[2], box[2]), max(region[3], box[3]))
            if not eligible or region == before:
                break
        if not eligible or any(_intersects(region, _box(g["rect"])) for g in nonnormal):
            continue
        results.append(dict(proof, source_sha256=source_sha256, page=page.number+1,
                            source_image_index=image_index, source_image_number=info["number"],
                            source_xref=info["xref"], source_transform_pdf=list(info["transform"]),
                            source_bbox_pdf=list(info["bbox"]), source_quad_pdf=quad,
                            source_paint_order=order, later_strokes=strokes, later_text=text,
                            source_svg_sha256=hashlib.sha256(svg.encode()).hexdigest()))
    return results


def exact_image_bytes(page, plan, fitz):
    """Join decoded source pixels and the original affine; never block number."""
    matches = []
    for block in page.get_text("dict", flags=fitz.TEXT_PRESERVE_IMAGES)["blocks"]:
        if (block.get("type") != 1 or block.get("mask")
                or not _close(block.get("transform", ()), plan["source_transform_pdf"])):
            continue
        pix = fitz.Pixmap(block["image"])
        if (pix.width == plan["width"] and pix.height == plan["height"]
                and pix.n == 3 and not pix.alpha
                and hashlib.sha256(pix.samples).hexdigest() == plan["rgb_sha256"]):
            matches.append(pix.tobytes("png"))
    if len(matches) != 1:
        raise ValueError("Opaque image lacks unique original pixel/affine ownership")
    return matches[0]


def _point(m, p):
    a, b, c, d, e, f = m
    return a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f


def _multiply(a, b):
    e, f = _point(a, (b[4], b[5]))
    return (
        a[0] * b[0] + a[2] * b[1],
        a[1] * b[0] + a[3] * b[1],
        a[0] * b[2] + a[2] * b[3],
        a[1] * b[2] + a[3] * b[3],
        e,
        f,
    )


def _quad(m, width=1.0, height=1.0):
    return tuple(
        _point(m, p) for p in ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height))
    )


def _bounds(points):
    xs, ys = zip(*points, strict=True)
    return min(xs), min(ys), max(xs), max(ys)


def _box(value):
    if len(value) != 4 or not all(math.isfinite(float(v)) for v in value):
        raise ValueError("Invalid source paint bounds")
    result = tuple(float(v) for v in value)
    if result[0] > result[2] or result[1] > result[3]:
        raise ValueError("Inverted source paint bounds")
    return result


def _ulp32(value):
    value = abs(float(value))
    bits = struct.unpack("I", struct.pack("f", value))[0]
    if bits >= 0x7F7FFFFF:
        raise ValueError("Nonfinite source coordinate")
    return struct.unpack("f", struct.pack("I", bits + 1))[0] - value


def _tolerance(*values):
    # MuPDF coordinates are float32; SVG serializes those same coordinates.
    return max(4 * _ulp32(v) for v in values) + 1e-7


def _close(a, b):
    return len(a) == len(b) and all(
        math.isfinite(x) and math.isfinite(y) and abs(x - y) <= _tolerance(x, y)
        for x, y in zip(a, b, strict=True)
    )


def _intersects(a, b):
    return min(a[2], b[2]) >= max(a[0], b[0]) and min(a[3], b[3]) >= max(a[1], b[1])


def _contains(quad, points):
    """Convex ordered nondegenerate quadrilateral, with source-roundoff only."""
    if len(quad) != 4 or any(len(p) != 2 for p in quad):
        return False
    if not all(math.isfinite(v) for p in quad for v in p):
        return False
    cross = lambda a, b, c: (
        (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    )
    signs = [cross(quad[i], quad[(i + 1) % 4], quad[(i + 2) % 4]) for i in range(4)]
    if not (all(v > 0 for v in signs) or all(v < 0 for v in signs)):
        return False
    direction = 1 if signs[0] > 0 else -1
    for point in points:
        for i in range(4):
            a, b = quad[i], quad[(i + 1) % 4]
            error = _tolerance(*a, *b, *point) * math.hypot(b[0] - a[0], b[1] - a[1])
            if direction * cross(a, b, point) < -error:
                return False
    return True


def _neutral(node, allowed):
    return set(node.attrib) <= allowed and float(node.get("opacity", "1")) == 1


def _clip_quad(node, inherited):
    if (
        set(node.attrib) - {"id", "clipPathUnits"}
        or node.get("clipPathUnits", "userSpaceOnUse") != "userSpaceOnUse"
    ):
        raise ValueError("Unsupported SVG clipping units")
    children = list(node)
    if len(children) != 1 or children[0].tag.rsplit("}", 1)[-1] != "path":
        raise ValueError("Unsupported SVG clip geometry")
    path = children[0]
    if set(path.attrib) - {"d", "transform", "clip-rule"} or path.get(
        "clip-rule", "nonzero"
    ) not in {"nonzero", "evenodd"}:
        raise ValueError("Unsupported SVG clip properties")
    match = _RECT.fullmatch(path.get("d", ""))
    if match is None:
        raise ValueError("Nonrectangular SVG clip")
    x0, y0, x1, y1, end = map(float, match.groups())
    if end != x0 or x0 == x1 or y0 == y1:
        raise ValueError("Degenerate SVG clip")
    matrix = _multiply(inherited, _matrix(path.get("transform")))
    return tuple(_point(matrix, p) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))


def _svg_images(svg, fitz):
    """Painted, opaque image occurrences with proven full-quad clipping."""
    root = ET.fromstring(svg)
    if not _neutral(root, {"version", "width", "height", "viewBox", "opacity"}):
        return []
    ids = {}
    for node in root.iter():
        if node.get("id"):
            if node.get("id") in ids:
                return []
            ids[node.get("id")] = node
    result = []

    def visit(node, matrix, clips, safe):
        tag = node.tag.rsplit("}", 1)[-1]
        if tag in {"defs", "clipPath", "mask", "symbol"}:
            return
        if tag == "g":
            safe = safe and _neutral(node, {"id", "transform", "clip-path", "opacity"})
            matrix = _multiply(matrix, _matrix(node.get("transform")))
            if node.get("clip-path"):
                match = re.fullmatch(r"url\(#([^()]+)\)", node.get("clip-path"))
                if match is None or match[1] not in ids:
                    safe = False
                else:
                    try:
                        clips = clips + [_clip_quad(ids[match[1]], matrix)]
                    except ValueError:
                        safe = False
        elif tag == "image":
            # Even an unsupported image occurrence is retained as a placeholder
            # so it cannot shift the occurrence join for later images.
            record = None
            if safe and _neutral(
                node,
                {
                    "id",
                    "width",
                    "height",
                    "transform",
                    "opacity",
                    "{http://www.w3.org/1999/xlink}href",
                },
            ):
                try:
                    width, height = (
                        int(node.get("width", "0")),
                        int(node.get("height", "0")),
                    )
                    if width <= 0 or height <= 0:
                        raise ValueError("Invalid source image dimensions")
                    matrix = _multiply(matrix, _matrix(node.get("transform")))
                    quad = _quad(matrix, width, height)
                    href = node.get("{http://www.w3.org/1999/xlink}href", "")
                    prefix = "data:image/png;base64,"
                    if not href.startswith(prefix):
                        raise ValueError("Nonlocal SVG image")
                    encoded = re.sub(r"[\t\r\n ]", "", href[len(prefix) :])
                    pixels = fitz.Pixmap(base64.b64decode(encoded, validate=True))
                    if (
                        pixels.width != width
                        or pixels.height != height
                        or pixels.alpha
                        or pixels.n != 3
                    ):
                        raise ValueError("Image is not demonstrably opaque RGB")
                    if not all(_contains(clip, quad) for clip in clips):
                        raise ValueError("Image footprint is clipped")
                    record = {
                        "quad_pdf": quad,
                        "bbox_pdf": _bounds(quad),
                        "rgb_sha256": hashlib.sha256(pixels.samples).hexdigest(),
                        "pixel_digest": bytes(pixels.digest).hex(),
                        "width": width,
                        "height": height,
                        "clip_quads_pdf": clips,
                        "svg_pixel_matrix": matrix,
                    }
                except (ValueError, TypeError, RuntimeError):
                    record = None
            result.append(record)
            return
        elif tag != "svg":
            # A referenced image hidden in a filter/use is not guessed to be a
            # direct paint. The complete image count/order join must also pass.
            return
        for child in node:
            visit(child, matrix, clips, safe)

    try:
        visit(root, _IDENTITY, [], True)
    except (TypeError, ValueError):
        return []
    return result

