"""One PDF compound paint, with exact source contours and winding semantics.

The native kernel builds original line/Bezier contours and performs planar
Boolean regions. Contour orientation controls nonzero winding; even-odd uses
parity. Separate stroke paint is deliberately outside this module. Unsupported
or invalid native contours are errors, never independent opaque filled loops.
"""
from fractions import Fraction
import hashlib
import json
import math
from numbers import Real


class SourceFillError(ValueError):
    """A source compound fill could not be proved as native painted area."""


def _point(value):
    try:
        values = tuple((value.x, value.y) if hasattr(value, "x") else value)
        if any(isinstance(v, bool) or not isinstance(v, Real) for v in values):
            raise ValueError("Nonnumeric coordinate")
        result = tuple(float(v) for v in values)
    except (TypeError, ValueError, AttributeError) as exc:
        raise SourceFillError("Invalid source fill point") from exc
    if len(result) != 2 or not all(math.isfinite(v) for v in result):
        raise SourceFillError("Nonfinite or nonplanar source fill point")
    return result


def source_contours(path_group, parse_rect):
    """Read the complete source subpaths; implicitly close fill, never stroke.

    Rectangles and quads are self-contained subpaths. Explicit start points
    establish line/cubic connectivity, not a native edge sorter or tolerance.
    """
    if (path_group.get("type") not in ("f", "fs")
            or path_group.get("fill") is None):
        return None
    contours, current = [], []

    def finish():
        nonlocal current
        if current:
            if current[-1][-1] != current[0][1]:
                current.append(("l", current[-1][-1], current[0][1]))
            contours.append(tuple(current))
        current = []

    for item in path_group.get("items", ()):
        kind = item[0]
        if kind in ("re", "qu"):
            finish()
            if kind == "re":
                try:
                    x, y, w, h = map(float, parse_rect(item[1:]))
                except (TypeError, ValueError, AttributeError) as exc:
                    raise SourceFillError("Invalid source fill rectangle") from exc
                points = [_point(p) for p in ((x, y), (x+w, y), (x+w, y+h), (x, y+h))]
                if len(item) > 2 and item[2] == -1:
                    points.reverse()
            else:
                if len(item) != 2:
                    raise SourceFillError("Invalid source fill quad")
                q = item[1]
                points = [_point(p) for p in (q.ul, q.ur, q.lr, q.ll)]
            current = [("l", a, b) for a, b in zip(points, points[1:] + points[:1], strict=True)]
            finish()
        elif kind in ("l", "c") and len(item) == (3 if kind == "l" else 5):
            points = tuple(_point(p) for p in item[1:])
            if current and (current[-1][-1] == current[0][1]
                            or current[-1][-1] != points[0]):
                finish()
            current.append((kind, *points))
        else:
            raise SourceFillError("Unsupported source compound fill command %r" % (kind,))
    finish()
    if len(contours) > 1 and type(path_group.get("even_odd")) is not bool:
        raise SourceFillError("Missing typed source fill rule")
    return tuple(contours)


def _coefficients(points):
    if len(points) == 2:
        a, b = points
        return (a, b-a)
    a, b, c, d = points
    return (a, 3*(b-a), 3*(a-2*b+c), -a+3*b-3*c+d)


def signed_source_area(contour):
    """Exact Green integral of original binary-valued lines/cubic controls."""
    twice_area = Fraction(0)
    for command in contour:
        points = command[1:]
        x = _coefficients(tuple(Fraction(p[0]) for p in points))
        y = _coefficients(tuple(Fraction(p[1]) for p in points))
        for i, a in enumerate(x):
            for j in range(1, len(y)):
                twice_area += a*y[j]*j/Fraction(i+j)
        for i, a in enumerate(y):
            for j in range(1, len(x)):
                twice_area -= a*x[j]*j/Fraction(i+j)
    return twice_area/2


def _controls(command):
    return tuple(tuple(Fraction(v) for v in _point(p)) for p in command[1:])


def _split(points):
    levels = [points]
    while len(levels[-1]) > 1:
        levels.append(tuple(tuple((a+b)/2 for a, b in zip(p, q, strict=True))
                            for p, q in zip(levels[-1], levels[-1][1:], strict=False)))
    return (tuple(level[0] for level in levels),
            tuple(level[-1] for level in reversed(levels)))


def _hulls_disjoint_except_endpoints(a, b, allowed):
    # Exact projection separates convex control hulls. On a supporting plane,
    # a nonconstant Bernstein polynomial is strict in the open parameter
    # interval, so only original endpoints can touch that plane.
    axes = {(Fraction(1), Fraction(0)), (Fraction(0), Fraction(1))}
    for points in (a, b):
        for i, p in enumerate(points):
            for q in points[i+1:]:
                dx, dy = q[0]-p[0], q[1]-p[1]
                if dx or dy:
                    axes.add((-dy, dx))
    for axis in sorted(axes):
        pa = [sum(v*w for v, w in zip(p, axis, strict=True)) for p in a]
        pb = [sum(v*w for v, w in zip(p, axis, strict=True)) for p in b]
        for left, right, lp, rp in ((a, b, pa, pb), (b, a, pb, pa)):
            if max(lp) < min(rp):
                return True
            if max(lp) != min(rp):
                continue
            plane = max(lp)
            left_flat, right_flat = min(lp) == plane, max(rp) == plane
            left_ends = {p for p, value in ((left[0], lp[0]), (left[-1], lp[-1]))
                         if value == plane}
            right_ends = {p for p, value in ((right[0], rp[0]), (right[-1], rp[-1]))
                          if value == plane}
            if not left_flat and not right_flat:
                if left_ends & right_ends <= allowed:
                    return True
            elif not left_flat and left_ends <= allowed:
                return True
            elif not right_flat and right_ends <= allowed:
                return True
    return False


def _prove_simple(contour, cancel):
    """Conservative exact simple-boundary proof; never flatten a cubic.

    Each original curve must be injective in a monotone coordinate. Exact
    rational subdivision is used only for convex hull separation, not native
    geometry. Unresolved contacts, self crossings or excessive proof work are
    refused. Different contours may overlap: native winding regions handle
    those intersections after every contour is independently proved simple.
    """
    curves = [_controls(c) for c in contour if c[1] != c[-1] or c[0] == "c"]
    if len(curves) < 2:
        raise SourceFillError("Unproved source fill boundary")
    for points in curves:
        cancel()
        monotone = False
        for axis in (0, 1):
            values = [p[axis] for p in points]
            differences = [b-a for a, b in zip(values, values[1:], strict=False)]
            if values[0] != values[-1] and (all(v >= 0 for v in differences)
                                           or all(v <= 0 for v in differences)):
                monotone = True
        if not monotone:
            raise SourceFillError("Source fill curve injectivity is unproved")
    checks = 0

    def separate(a, b, allowed, depth=0):
        nonlocal checks
        checks += 1
        cancel()
        if checks > 4096 or depth > 24:
            raise SourceFillError("Source fill boundary separation is unproved")
        if _hulls_disjoint_except_endpoints(a, b, allowed):
            return
        if len(a) == len(b) == 2:
            raise SourceFillError("Source fill boundary crosses, overlaps or touches itself")
        if len(a) > 2:
            for half in _split(a):
                separate(half, b, allowed, depth+1)
        else:
            for half in _split(b):
                separate(a, half, allowed, depth+1)

    for i, a in enumerate(curves):
        if a[-1] != curves[(i+1) % len(curves)][0]:
            raise SourceFillError("Source fill boundary is disconnected")
        for j in range(i+1, len(curves)):
            b = curves[j]
            allowed = set()
            if j == i+1:
                allowed.add(a[-1])
            if i == 0 and j == len(curves)-1:
                allowed.add(a[0])
            separate(a, b, allowed)


def _region(shape, context):
    if shape is None or shape.isNull():
        return None
    if shape.isValid() is not True:
        raise SourceFillError("Invalid native source fill %s" % context)
    area = float(shape.Area)
    if not math.isfinite(area) or area < 0:
        raise SourceFillError("Invalid native source fill area")
    if area == 0:
        return None  # A Boolean may return only lower-dimensional boundaries.
    if not shape.Faces:
        raise SourceFillError("Native source fill has area without faces")
    return shape


def _operation(left, method, right):
    try:
        return _region(getattr(left, method)(right), method)
    except (RuntimeError, TypeError, AttributeError, ValueError) as exc:
        raise SourceFillError("Native source fill Boolean %s failed" % method) from exc


def _union(left, right):
    return right if left is None else left if right is None else _operation(left, "fuse", right)


def _area(shape):
    return 0.0 if shape is None else float(shape.Area)


def _require_area(actual, expected, context):
    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9):
        raise SourceFillError("Native source fill area conservation failed: %s" % context)


def _partition(region, cutter):
    outside = _operation(region, "cut", cutter)
    overlap = _operation(region, "common", cutter)
    _require_area(_area(outside)+_area(overlap), _area(region), "cut/common partition")
    if _area(overlap) > _area(cutter) + 1e-9*max(1.0, _area(cutter)):
        raise SourceFillError("Native source fill intersection exceeds cutter")
    return outside, overlap


def _xyz(point):
    return tuple(float(getattr(point, axis)) for axis in ("x", "y", "z"))


def _edge_matches(edge, command, points):
    if edge.isNull() or edge.isValid() is not True:
        raise SourceFillError("Invalid native source fill edge")
    if command == "l":
        actual = [_xyz(v.Point) for v in edge.Vertexes]
        expected = [_xyz(p) for p in points]
        if (edge.Curve.__class__.__name__ not in ("Line", "LineSegment")
                or len(actual) != 2
                or not (actual == expected or actual == expected[::-1])):
            raise SourceFillError("Native fill line differs from original transformed endpoints")
    else:
        if (edge.Curve.__class__.__name__ != "BezierCurve"
                or [_xyz(p) for p in edge.Curve.getPoles()] != [_xyz(p) for p in points]):
            raise SourceFillError("Native fill cubic differs from original transformed controls")


def _boundary_numbers(values, size):
    values = tuple(values)
    if (len(values) != size
            or any(isinstance(v, bool) or not isinstance(v, Real) for v in values)):
        raise SourceFillError("Invalid native fill boundary numeric data")
    result = tuple(float(v) for v in values)
    if not all(math.isfinite(v) for v in result):
        raise SourceFillError("Nonfinite native fill boundary numeric data")
    return result


def _boundary_point(point):
    return _boundary_numbers((getattr(point, axis) for axis in ("x", "y", "z")), 3)


def _boundary_geometry(edge):
    """Read exact original geometry, never a topology ID or approximation.

    MakeWire may copy an edge while joining its vertices. A TShape identity
    check therefore cannot establish source geometry conservation. Compare
    the complete supported curve, its original trim and placement instead.
    Edge orientation and vertex enumeration can reverse without changing
    that underlying parameterized geometry, just as isSame ignores orientation.
    """
    try:
        if (edge.ShapeType != "Edge" or edge.isNull() is not False
                or edge.isValid() is not True
                or edge.Orientation not in ("Forward", "Reversed")):
            raise SourceFillError("Invalid native fill boundary edge")
        placement = _boundary_numbers(edge.Placement.toMatrix().A, 16)
        first, last, length = _boundary_numbers(
            (edge.FirstParameter, edge.LastParameter, edge.Length), 3)
        if first >= last or length <= 0:
            raise SourceFillError("Invalid native fill boundary parameter interval or length")
        endpoints = tuple(_boundary_point(v.Point) for v in edge.Vertexes)
        if len(endpoints) != 2 or endpoints[0] == endpoints[1]:
            raise SourceFillError("Invalid native fill boundary endpoints")
        curve = edge.Curve
        kind = curve.__class__.__name__
        if curve.isPeriodic() is not False:
            raise SourceFillError("Periodic native fill boundary is unsupported")
        if kind == "Line":
            location, direction = _boundary_point(curve.Location), _boundary_point(curve.Direction)
            if not any(direction):
                raise SourceFillError("Invalid native fill line direction")
            controls = (location, direction)
        elif kind == "BezierCurve":
            if (type(curve.Degree) is not int or curve.Degree != 3
                    or curve.isRational() is not False or (first, last) != (0.0, 1.0)):
                raise SourceFillError("Native fill cubic degree, rationality or full trim changed")
            poles = tuple(_boundary_point(p) for p in curve.getPoles())
            weights = _boundary_numbers(curve.getWeights(), 4)
            if len(poles) != 4 or weights != (1.0, 1.0, 1.0, 1.0):
                raise SourceFillError("Native fill cubic controls or weights changed")
            controls = (poles, weights)
        else:
            raise SourceFillError("Unsupported native fill boundary curve %r" % kind)
        return kind, controls, (first, last), placement, tuple(sorted(endpoints)), length
    except (RuntimeError, TypeError, ValueError, AttributeError, OverflowError) as exc:
        if isinstance(exc, SourceFillError):
            raise
        raise SourceFillError("Native fill boundary geometry could not be verified") from exc


def _edge_bijection(source, observed):
    if len(source) != len(observed):
        raise SourceFillError("Native fill boundary edge count changed")
    originals = [_boundary_geometry(edge) for edge in source]
    if len(set(originals)) != len(originals):
        raise SourceFillError("Native fill boundary original geometry is ambiguous")
    used = set()
    for edge in observed:
        geometry = _boundary_geometry(edge)
        matches = [i for i, original in enumerate(originals) if geometry == original]
        if len(matches) != 1 or matches[0] in used:
            raise SourceFillError("Native fill boundary source-edge geometry bijection failed")
        used.add(matches[0])


def _native_face(contour, part, transform, scale, cancel):
    _prove_simple(contour, cancel)
    edges = []
    for command in contour:
        cancel()
        try:
            points = [transform(p) for p in command[1:]]
            if command[0] == "l":
                if command[1] == command[2]:
                    continue
                edge = part.LineSegment(*points).toShape()
            else:
                curve = part.BezierCurve()
                curve.setPoles(points)
                edge = curve.toShape()
            _edge_matches(edge, command[0], points)
            edges.append(edge)
        except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
            raise SourceFillError("Native source fill edge construction failed") from exc
    if not edges:
        raise SourceFillError("Source fill contour has no boundary")
    try:
        wire = part.Wire(edges)
        if not wire.isClosed() or wire.isValid() is not True:
            raise SourceFillError("Native source fill contour is open or invalid")
        _edge_bijection(edges, list(wire.Edges))
        face = _region(part.Face(wire), "contour")
    except (RuntimeError, TypeError, ValueError, AttributeError) as exc:
        if isinstance(exc, SourceFillError):
            raise
        raise SourceFillError("Native source fill contour construction failed") from exc
    source_area = signed_source_area(contour)
    if source_area == 0 or face is None or len(face.Faces) != 1:
        raise SourceFillError("Unproved degenerate or self-crossing source fill contour")
    if len(face.Faces[0].Wires) != 1:
        raise SourceFillError("Native source contour acquired extra wires")
    _edge_bijection(edges, list(face.Faces[0].Wires[0].Edges))
    if not math.isclose(float(face.Area), float(abs(source_area))*scale*scale,
                        rel_tol=1e-9, abs_tol=1e-9):
        raise SourceFillError("Native fill contour area differs from exact source integral")
    return face, 1 if source_area > 0 else -1


def build_compound_fill(contours, even_odd, part, transform, scale, cancel=lambda: None):
    """Compose disjoint winding regions, including overlap and cancellation.

    An absent region means zero winding. Splitting old regions by each contour
    keeps their integer winding; the overlap adds the contour's sign. At zero,
    the region vanishes. Parity composition is symmetric difference. No native
    object is created here, so the caller retains ordinary page rollback.
    """
    if (type(even_odd) is not bool or isinstance(scale, bool)
            or not math.isfinite(scale) or scale <= 0):
        raise SourceFillError("Invalid source fill rule or scale")
    cells = []
    for contour in contours:
        cancel()
        face, sign = _native_face(contour, part, transform, scale, cancel)
        old_union = None
        for region, _winding in cells:
            cancel()
            old_union = _union(old_union, region)
        _require_area(_area(old_union), sum(_area(region) for region, _ in cells),
                      "disjoint winding region union")
        added = face if old_union is None else _partition(face, old_union)[0]
        next_cells = []
        for region, winding in cells:
            cancel()
            outside, overlap = _partition(region, face)
            if outside is not None:
                next_cells.append((outside, winding))
            new_winding = (1-winding) if even_odd else winding+sign
            if overlap is not None and new_winding:
                next_cells.append((overlap, new_winding))
        if added is not None:
            next_cells.append((added, 1 if even_odd else sign))
        cells = next_cells
    painted = None
    for region, _winding in cells:
        cancel()
        painted = _union(painted, region)
    expected_area = sum(_area(region) for region, _ in cells)
    _require_area(_area(painted), expected_area, "final disjoint painted union")
    if painted is not None:
        try:
            painted = _region(painted.removeSplitter(), "final planar paint")
            _require_area(_area(painted), expected_area, "removeSplitter")
        except (RuntimeError, AttributeError, TypeError) as exc:
            raise SourceFillError("Native source fill final composition failed") from exc
    canonical = json.dumps(contours, separators=(",", ":"), allow_nan=False)
    receipt = {"schema": "bcs.freecad.source-compound-fill/1",
               "fill_rule": "even_odd" if even_odd else "nonzero",
               "source_contour_count": len(contours),
               "source_contours_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
               "source_signed_areas": [str(signed_source_area(c)) for c in contours],
               "boundary_representation": "original_lines_and_cubic_bezier_controls_plus_pdf_fill_closure",
               "native_composition": "kernel_winding_regions",
               "source_native_contour_bijection": True,
               "source_simple_contours": "exact_rational_injectivity_and_control_hull_separation",
               "outcome": "verified_zero_ink" if painted is None else "native_painted_area",
               "native_area": 0.0 if painted is None else float(painted.Area)}
    return painted, receipt
