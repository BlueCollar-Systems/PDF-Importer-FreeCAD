"""Filled glyphs from the renderer's retained polygon contours.

This preserves SVG nonzero/evenodd semantics for disjoint simple contours.
Crossing/touching contours are unsupported, never silently repaired. Curves
retain the SVG renderer's existing polygon approximation, not analytic Beziers.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on(a, b, p):
    return (
        _cross(a, b, p) == 0.0
        and min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
    )


def _intersects(a, b, c, d):
    if (
        max(a[0], b[0]) < min(c[0], d[0])
        or max(c[0], d[0]) < min(a[0], b[0])
        or max(a[1], b[1]) < min(c[1], d[1])
        or max(c[1], d[1]) < min(a[1], b[1])
    ):
        return False
    ab_c, ab_d, cd_a, cd_b = (
        _cross(a, b, c),
        _cross(a, b, d),
        _cross(c, d, a),
        _cross(c, d, b),
    )
    return (
        (ab_c * ab_d < 0 and cd_a * cd_b < 0)
        or _on(a, b, c)
        or _on(a, b, d)
        or _on(c, d, a)
        or _on(c, d, b)
    )


def _inside(point, contour):
    x, y = point
    inside = False
    for a, b in zip(contour, contour[1:], strict=False):
        if _on(a, b, point):
            raise ValueError("glyph contour boundary is ambiguous")
        if (a[1] > y) != (b[1] > y):
            if x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1]):
                inside = not inside
    return inside


def classify_contours(contours, fill_rule="nonzero"):
    """Return source winding, face/hole ownership and expected polygon area."""
    if fill_rule not in ("nonzero", "evenodd"):
        raise ValueError("unsupported SVG glyph fill rule")
    loops, areas = [], []
    for contour in contours:
        points = [tuple(float(v) for v in p[:2]) for p in contour]
        if not points or any(
            len(p) != 2 or not all(map(math.isfinite, p)) for p in points
        ):
            raise ValueError("invalid glyph contour coordinates")
        # Only exact repeated points have no source segment; no geometric snapping.
        points = [p for i, p in enumerate(points) if i == 0 or p != points[i - 1]]
        if len(points) < 4 or points[0] != points[-1]:
            raise ValueError("glyph contour is open or degenerate")
        area = (
            math.fsum(
                a[0] * b[1] - b[0] * a[1]
                for a, b in zip(points, points[1:], strict=False)
            )
            / 2.0
        )
        if area == 0.0:
            raise ValueError("glyph contour has zero signed area")
        segments = list(zip(points, points[1:], strict=False))
        for i, (a, b) in enumerate(segments):
            for j in range(i + 1, len(segments)):
                c, d = segments[j]
                adjacent = j == i + 1 or (i == 0 and j == len(segments) - 1)
                if adjacent:
                    # Shared endpoint is legal; collinear reversal/overlap is not.
                    shared = b if j == i + 1 else a
                    other_a = a if j == i + 1 else b
                    other_b = d if j == i + 1 else c
                    if _on(shared, other_a, other_b) or _on(shared, other_b, other_a):
                        raise ValueError("glyph contour backtracks")
                elif _intersects(a, b, c, d):
                    raise ValueError("self-intersecting glyph contour is unsupported")
        for previous in loops:
            if any(
                _intersects(a, b, c, d)
                for a, b in segments
                for c, d in zip(previous, previous[1:], strict=False)
            ):
                raise ValueError("crossing or touching glyph contours are unsupported")
        loops.append(points)
        areas.append(area)
    if not loops:
        raise ValueError("glyph has no closed contours")
    parents = []
    for i, loop in enumerate(loops):
        enclosing = [
            j for j, other in enumerate(loops) if i != j and _inside(loop[0], other)
        ]
        parents.append(
            min(enclosing, key=lambda j: abs(areas[j])) if enclosing else None
        )
    roles, ancestors = [], []
    for i in range(len(loops)):
        chain, parent = [], parents[i]
        while parent is not None:
            if parent in chain:
                raise ValueError("cyclic glyph contour ownership")
            chain.append(parent)
            parent = parents[parent]
        ancestors.append(chain)
        if fill_rule == "evenodd":
            outside, inside = len(chain) % 2, (len(chain) + 1) % 2
        else:
            outside = sum(1 if areas[j] > 0 else -1 for j in chain)
            inside = outside + (1 if areas[i] > 0 else -1)
        roles.append(
            "outer"
            if outside == 0 and inside != 0
            else "hole"
            if outside != 0 and inside == 0
            else "internal"
        )
    faces = []
    for i, role in enumerate(roles):
        if role != "outer":
            continue
        holes = [
            j
            for j, r in enumerate(roles)
            if r == "hole"
            and next((k for k in ancestors[j] if roles[k] == "outer"), None) == i
        ]
        faces.append(
            {
                "outer": i,
                "holes": holes,
                "area": abs(areas[i]) - math.fsum(abs(areas[j]) for j in holes),
            }
        )
    expected_area = math.fsum(face["area"] for face in faces)
    if not faces or expected_area <= 0:
        raise ValueError("glyph fill has no positive area")
    return {
        "contours": loops,
        "signed_areas": areas,
        "roles": roles,
        "faces": faces,
        "fill_rule": fill_rule,
        "area": expected_area,
        "edge_count": sum(len(loop) - 1 for loop in loops),
    }


def _same_edge(actual, expected):
    def same_point(a, e):
        return all(
            math.isclose(a[axis], e[axis], rel_tol=1e-10, abs_tol=1e-9)
            for axis in (0, 1)
        )

    return (
        same_point(actual[0], expected[0]) and same_point(actual[1], expected[1])
    ) or (same_point(actual[0], expected[1]) and same_point(actual[1], expected[0]))


def _edges_have_bijection(actual, expected):
    """Match edge occurrences, preserving connectivity and duplicate counts."""
    if len(actual) != len(expected) or any(
        len(edge) != 2 for edge in (*actual, *expected)
    ):
        return False
    # Common case: sorted edge ranks agree. Endpoint orientation is not geometry;
    # one-ULP OCC variation at equal X can reverse lexicographic endpoint order.
    if all(_same_edge(a, e) for a, e in zip(actual, expected, strict=True)):
        return True

    compatible = [
        [index for index, e in enumerate(expected) if _same_edge(a, e)] for a in actual
    ]
    if any(not matches for matches in compatible):
        return False
    expected_owner = [None] * len(expected)
    actual_match = [None] * len(actual)
    for start in range(len(actual)):
        # Find an augmenting path rather than greedily consuming a near match.
        # Iteration avoids a recursion limit for glyphs with many contours.
        pending, seen_actual, parent = [start], {start}, {}
        free = None
        for current in pending:
            for index in compatible[current]:
                if index in parent:
                    continue
                parent[index] = current
                owner = expected_owner[index]
                if owner is None:
                    free = index
                    break
                if owner not in seen_actual:
                    seen_actual.add(owner)
                    pending.append(owner)
            if free is not None:
                break
        if free is None:
            return False
        while free is not None:
            owner = parent[free]
            previous = actual_match[owner]
            expected_owner[free] = owner
            actual_match[owner] = free
            free = previous
    return True


def verify_shape(shape, proof, area_scale=1.0):
    """Read live host topology and area; never trust a pre-assignment memo."""
    expected_area = float(proof["area"]) * abs(float(area_scale))
    if not math.isfinite(expected_area) or expected_area <= 0:
        raise ValueError("invalid glyph affine determinant")
    faces = list(shape.Faces)
    if shape.isNull() or not shape.isValid():
        raise ValueError("native glyph fill lost source topology or area")
    # Each getter enters the native kernel. Retain its value only for this
    # invocation; assigned/recomputed shapes are read again at every stage.
    edges = list(shape.Edges)
    if (
        len(faces) != len(proof["faces"])
        or len(edges) != proof["edge_count"]
        or sum(len(face.Wires) for face in faces)
        != sum(1 + len(row["holes"]) for row in proof["faces"])
        or not math.isclose(
            float(shape.Area), expected_area, rel_tol=1e-8, abs_tol=1e-10
        )
    ):
        raise ValueError("native glyph fill lost source topology or area")
    coordinates = []
    for edge in edges:
        points = [vertex.Point for vertex in edge.Vertexes]
        coordinates.append(
            [(float(point.x), float(point.y), float(point.z)) for point in points]
        )
    actual = sorted(
        tuple(sorted((x, y) for x, y, _z in points)) for points in coordinates
    )
    if any(
        not math.isfinite(z) or abs(z) > 1e-9
        for points in coordinates
        for _x, _y, z in points
    ):
        raise ValueError("native glyph fill left the source plane")
    expected = sorted(
        tuple(sorted((a, b)))
        for loop in proof["contours"]
        for a, b in zip(loop, loop[1:], strict=False)
    )
    if not _edges_have_bijection(actual, expected):
        raise ValueError("native glyph fill changed source contour coordinates")


def build_shape(contours, fill_rule, part, vector):
    proof = classify_contours(contours, fill_rule)
    wires = [
        part.Wire(
            [
                part.LineSegment(vector(*a, 0.0), vector(*b, 0.0)).toShape()
                for a, b in zip(loop, loop[1:], strict=False)
            ]
        )
        for loop in proof["contours"]
    ]
    pieces = []
    for row in proof["faces"]:
        # No FaceMakerSimple fallback: it can silently fill counter holes.
        face = part.makeFace(
            [wires[row["outer"]]] + [wires[j] for j in row["holes"]],
            "Part::FaceMakerBullseye",
        )
        if (
            len(face.Faces) != 1
            or len(face.Wires) != 1 + len(row["holes"])
            or not math.isclose(
                float(face.Area), row["area"], rel_tol=1e-8, abs_tol=1e-10
            )
        ):
            raise ValueError("native face maker changed glyph counters")
        pieces.append(face)
    # Nonzero nested boundaries that do not change paint remain editable wires.
    pieces.extend(
        wires[i] for i, role in enumerate(proof["roles"]) if role == "internal"
    )
    shape = part.makeCompound(pieces)
    verify_shape(shape, proof)
    return shape, proof


def placement_fill_rules(
    svg, expected_glyph_ids, *, with_clips=False, defer_errors=False
):
    """Bind inherited SVG fill rules to actual rendered glyph occurrences."""
    if defer_errors and not with_clips:
        raise ValueError("deferred glyph paint errors require occurrence records")
    root = ET.fromstring(svg)
    ids = {}
    for node in root.iter():
        if node.get("id"):
            if node.get("id") in ids:
                raise ValueError("duplicate source glyph SVG identity")
            ids[node.get("id")] = node
        if node.tag.rsplit("}", 1)[-1] == "style":
            raise ValueError("stylesheet-driven source glyph paint is unsupported")
    found = []

    def clip_quad(value):
        match = re.fullmatch(r"url\(#([^)]*)\)", value.strip())
        node = ids.get(match.group(1)) if match else None
        if (
            node is None
            or local(node) != "clipPath"
            or set(node.attrib) - {"id", "clipPathUnits"}
            or node.get("clipPathUnits", "userSpaceOnUse") != "userSpaceOnUse"
            or len(node) != 1
        ):
            raise ValueError("unsupported source glyph clip")
        path = node[0]
        if set(path.attrib) - {"d", "transform", "clip-rule"} or path.get(
            "clip-rule", "nonzero"
        ) not in ("nonzero", "evenodd"):
            raise ValueError("unsupported source glyph clip properties")
        number = r"([-+]?(?:\d*\.\d+|\d+\.?\d*)(?:[eE][-+]?\d+)?)"
        rectangle = re.fullmatch(
            r"\s*M\s*"
            + number
            + r"[ ,]+"
            + number
            + r"\s*H\s*"
            + number
            + r"\s*V\s*"
            + number
            + r"\s*H\s*"
            + number
            + r"\s*Z\s*",
            path.get("d", ""),
        )
        if local(path) != "path" or not rectangle:
            raise ValueError("nonrectangular source glyph clip is unsupported")
        x0, y0, x1, y1, last_x = map(float, rectangle.groups())
        if last_x != x0 or x0 == x1 or y0 == y1:
            raise ValueError("invalid source glyph rectangle clip")
        transform = path.get("transform", "matrix(1,0,0,1,0,0)")
        matrix_match = re.fullmatch(r"matrix\(([^)]*)\)", transform.strip())
        values = (
            re.split(r"[\s,]+", matrix_match.group(1).strip()) if matrix_match else []
        )
        if len(values) != 6:
            raise ValueError("unsupported source glyph clip transform")
        a, b, c, d, e, f = map(float, values)
        if (
            not all(map(math.isfinite, (x0, y0, x1, y1, a, b, c, d, e, f)))
            or a * d - b * c == 0
        ):
            raise ValueError("invalid source glyph clip matrix")
        return [
            (a * x + c * y + e, b * x + d * y + f)
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        ]

    def local(node):
        return node.tag.rsplit("}", 1)[-1]

    def style_declarations(node):
        return {
            key.strip(): value.strip()
            for key, value in (
                part.strip().split(":", 1)
                for part in node.get("style", "").split(";")
                if ":" in part
            )
        }

    def has_local_clip(node):
        declarations = style_declarations(node)
        return (
            declarations.get("clip-path", node.get("clip-path", "none")).strip()
            != "none"
        )

    def style(node, inherited):
        result = dict(inherited)
        result.update(
            {
                key: node.attrib[key]
                for key in (
                    "fill",
                    "fill-rule",
                    "fill-opacity",
                    "stroke",
                    "stroke-opacity",
                )
                if key in node.attrib
            }
        )
        declarations = style_declarations(node)
        if any(
            key.strip()
            not in {
                "fill",
                "fill-rule",
                "fill-opacity",
                "stroke",
                "stroke-opacity",
                "opacity",
                "mask",
                "filter",
                "display",
                "visibility",
                "clip-path",
            }
            for key in declarations
        ):
            result["_unsupported"] = True
        result.update(
            {key.strip(): value.strip() for key, value in declarations.items()}
        )
        try:
            result["_opacity"] = float(inherited.get("_opacity", 1.0)) * float(
                declarations.get("opacity", node.get("opacity", "1"))
            )
        except (TypeError, ValueError):
            result["_unsupported"] = True
        if any(
            node.get(key) not in (None, "none")
            or declarations.get(key) not in (None, "none")
            for key in ("mask", "filter")
        ):
            result["_unsupported"] = True
        if (
            declarations.get("display", node.get("display", "inline")) == "none"
            or declarations.get("visibility", node.get("visibility", "visible"))
            != "visible"
        ):
            result["_unsupported"] = True
        clips = list(inherited.get("_clips", []))
        clip = declarations.get("clip-path", node.get("clip-path", "none"))
        if clip != "none":
            clips.append(clip)
        result["_clips"] = clips
        return result

    def glyph_paths(node, inherited):
        current = style(node, inherited)
        if node.get("transform"):
            raise ValueError("source glyph definition transform is unsupported")
        if has_local_clip(node):
            raise ValueError(
                "definition-local glyph clip needs its own coordinate-space proof"
            )
        if local(node) == "path":
            return [current]
        result = []
        for child in node:
            result.extend(glyph_paths(child, current))
        return result

    def qualify(gid, current):
        if gid not in ids:
            raise ValueError("missing source glyph paint definition")
        paints = glyph_paths(ids[gid], current)
        if len(paints) > 1:
            raise ValueError("multiple source glyph paint paths are unsupported")
        paint = paints[0] if paints else current
        if paint.get("_unsupported") or paint.get("_opacity") != 1.0:
            raise ValueError("glyph paint effects are unsupported")
        rule = paint.get("fill-rule", "nonzero")
        if rule not in ("nonzero", "evenodd"):
            raise ValueError("unknown source glyph fill rule")
        if paint.get("fill", "black") == "none":
            rule = None
        elif (
            float(paint.get("fill-opacity", "1")) != 1.0
            or paint.get("stroke", "none") != "none"
        ):
            raise ValueError("combined or translucent glyph paint is unsupported")
        if paint.get("_clips") and not with_clips:
            raise ValueError("source glyph clips need contour containment proof")
        return {
            "fill_rule": rule,
            "clips": [clip_quad(value) for value in paint.get("_clips", [])],
        }

    def visit(node, inherited, stack=()):
        if local(node) == "defs":
            return
        current = style(node, inherited)
        if local(node) != "use" and node.get("transform"):
            current["_unsupported"] = True
        if local(node) == "use":
            # Parent page clips already share the parsed SVG page space. A clip
            # attached here is in the use's local transformed space, which this
            # bounded parser does not certify as a global page rectangle.
            if has_local_clip(node):
                current["_unsupported"] = True
            ref = node.get("href", node.get("{http://www.w3.org/1999/xlink}href", ""))
            gid = ref[1:] if ref.startswith("#") else ""
            if gid.startswith(("glyph-", "font_", "font-")):
                try:
                    record = qualify(gid, current)
                except (ValueError, TypeError) as exc:
                    if not defer_errors:
                        raise
                    # The occurrence remains bound. Its source item must fail
                    # if selected, without blocking unrelated supported items.
                    record = {"error": str(exc), "fill_rule": None, "clips": []}
                found.append((gid, record))
                return
            if gid.startswith("source-") and gid in ids:
                if gid in stack:
                    raise ValueError("cyclic source glyph use")
                visit(ids[gid], current, stack + (gid,))
                return
        for child in node:
            visit(child, current, stack)

    visit(
        root,
        {"fill": "black", "fill-rule": "nonzero", "stroke": "none", "_opacity": 1.0},
    )
    if [gid for gid, _ in found] != list(expected_glyph_ids):
        raise ValueError("source glyph paint occurrence binding changed")
    return [paint if with_clips else paint["fill_rule"] for _, paint in found]


def verify_clip_containment(contours, clip_quads):
    """All contour vertices inside convex source clips proves every line segment."""
    for quad in clip_quads:
        for loop in contours:
            for point in loop:
                sides = [
                    _cross(a, b, point)
                    for a, b in zip(quad, quad[1:] + quad[:1], strict=False)
                ]
                if not (
                    all(value >= 0 for value in sides)
                    or all(value <= 0 for value in sides)
                ):
                    raise ValueError(
                        "filled glyph would cross its original source clip"
                    )
