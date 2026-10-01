"""Keep exact native text shapes inside their source page, in the same mode."""
from __future__ import annotations

import json
import math
from fractions import Fraction


def _bounds(shape):
    # BoundBox may use a GUI triangulation and miss curved extrema after the
    # object is displayed. Compare the analytic shape, not its display mesh.
    box = shape.optimalBoundingBox(False, False)
    values = tuple(float(getattr(box, name)) for name in
                   ("XMin", "YMin", "ZMin", "XMax", "YMax", "ZMax"))
    if not all(math.isfinite(value) for value in values) or any(
        values[i] > values[i + 3] for i in range(3)
    ):
        raise ValueError("source text shape has invalid native bounds")
    return values


def _page_bounds(quad):
    points = tuple(tuple(float(value) for value in point) for point in quad)
    if len(points) != 4 or any(len(point) != 2 for point in points):
        raise ValueError("source page clip must have four XY corners")
    if not all(math.isfinite(value) for point in points for value in point):
        raise ValueError("source page clip coordinates are not finite")
    x0, y0 = min(p[0] for p in points), min(p[1] for p in points)
    x1, y1 = max(p[0] for p in points), max(p[1] for p in points)
    if x0 >= x1 or y0 >= y1 or set(points) != {
        (x0, y0), (x1, y0), (x1, y1), (x0, y1)
    }:
        raise ValueError("source page transform is not an axis-aligned rectangle")
    return x0, y0, x1, y1


def _measure(shape, dimension):
    if shape.isNull():
        return 0.0
    value = float(getattr(shape, dimension))
    if not math.isfinite(value) or value < 0.0:
        raise ValueError("native text clipping measure is invalid")
    return value


def _inside_page(box, page):
    return (box[0] >= page[0] - 1e-7 and box[1] >= page[1] - 1e-7
            and box[3] <= page[2] + 1e-7 and box[4] <= page[3] + 1e-7)


def _point_bounds(points):
    points = tuple(tuple(float(value) for value in point) for point in points)
    if not points or any(len(point) != 3 or not all(math.isfinite(value) for value in point)
                         for point in points):
        raise ValueError("native containment poles are not finite 3D points")
    return (tuple(min(point[i] for point in points) for i in range(3))
            + tuple(max(point[i] for point in points) for i in range(3)))


def _rational_hull(poles, weights):
    poles, weights = tuple(poles), tuple(float(weight) for weight in weights)
    if len(poles) != len(weights) or not all(math.isfinite(w) and w > 0 for w in weights):
        raise ValueError("native rational containment requires positive finite weights")
    return _point_bounds(poles)


def _finite_domain(values, count):
    values = tuple(float(value) for value in values)
    if len(values) != count or not all(math.isfinite(value) for value in values) or any(
        values[i] >= values[i + 1] for i in range(0, count, 2)
    ):
        raise ValueError("native containment parameter domain is not finite and bounded")
    return values


def _edge_containment(edge, page):
    box = _bounds(edge)
    if _inside_page(box, page):
        return {"method": "native_analytic_bounds", "bounds": list(box)}
    curve = edge.Curve
    kind = type(curve).__name__
    if kind not in {"BSplineCurve", "BezierCurve"}:
        raise ValueError("native edge has no supported conservative containment proof")
    domain = _finite_domain(edge.ParameterRange, 2)
    # FreeCAD's BRepAdaptor-backed Curve/Surface accessors return world geometry.
    # Segment a copy; the source topology, poles, and placement stay untouched.
    trimmed = curve.copy()
    trimmed.segment(*domain)
    segment_domain = _finite_domain((trimmed.FirstParameter, trimmed.LastParameter), 2)
    if kind == "BSplineCurve" and (
        segment_domain[0] > domain[0] or segment_domain[1] < domain[1]
    ):
        raise ValueError("native trimmed curve does not cover the complete edge domain")
    box = _rational_hull(trimmed.getPoles(), trimmed.getWeights())
    if not _inside_page(box, page):
        raise ValueError("native trimmed curve hull crosses the source page")
    return {"method": "positive_rational_trimmed_curve_hull", "curve": kind,
            "parameter_range": list(domain), "bounds": list(box)}


def _surface_hull(surface):
    poles, weights = surface.getPoles(), surface.getWeights()
    if not poles or len(poles) != len(weights) or any(
        not row or len(row) != len(poles[0]) or len(row) != len(weight_row)
        for row, weight_row in zip(poles, weights, strict=True)
    ):
        raise ValueError("native rational surface has inconsistent pole and weight grids")
    return _rational_hull([point for row in poles for point in row],
                          [weight for row in weights for weight in row])


def _restrict_bezier(points, start, end):
    """Exact homogeneous blossom of a degree-at-most-three control polygon."""
    degree = len(points) - 1
    result = []
    for index in range(degree + 1):
        values = points
        for parameter in [start] * (degree - index) + [end] * index:
            values = [tuple((1 - parameter) * a + parameter * b
                            for a, b in zip(left, right, strict=True))
                      for left, right in zip(values[:-1], values[1:], strict=True)]
        result.append(values[0])
    return result


def _single_patch_surface_hull(surface, domain, page):
    """Cover one-ulp native UV overruns without clamping or widening ink limits.

    A clamped, nonperiodic, single-span BSpline is a rational Bezier patch.
    Exact homogeneous restriction covers both its polynomial continuation and
    endpoint-clamped evaluation. Positive new weights prove the whole patch
    lies within its Euclidean pole hull, including the requested overrun.
    """
    if surface.isUPeriodic() or surface.isVPeriodic():
        raise ValueError("periodic surface cannot use a single-patch certificate")
    degrees = (surface.UDegree, surface.VDegree)
    if any(degree not in (1, 2, 3) for degree in degrees):
        raise ValueError("single-patch containment degree exceeds its bounded proof")
    knots = (tuple(surface.getUKnots()), tuple(surface.getVKnots()))
    mults = (tuple(surface.getUMultiplicities()), tuple(surface.getVMultiplicities()))
    ranges = []
    for axis, (axis_knots, multiplicities, degree) in enumerate(zip(knots, mults, degrees, strict=True)):
        support = _finite_domain(axis_knots, 2)
        if multiplicities != (degree + 1, degree + 1):
            raise ValueError("native surface is not a clamped single-span patch")
        first, last = domain[2 * axis:2 * axis + 2]
        if first < math.nextafter(support[0], -math.inf) or last > math.nextafter(support[1], math.inf):
            raise ValueError("native UV overrun exceeds one representable endpoint step")
        # Include endpoint-clamped values too. Usually they are already inside
        # the requested interval; explicitly take their union for either end.
        start = min(first, max(support[0], min(first, support[1])))
        end = max(last, max(support[0], min(last, support[1])))
        low, high = map(Fraction.from_float, support)
        ranges.append(((Fraction.from_float(start) - low) / (high - low),
                       (Fraction.from_float(end) - low) / (high - low)))
    _surface_hull(surface)  # Validate complete finite poles and positive weights.
    poles, weights = surface.getPoles(), surface.getWeights()
    if len(poles) != degrees[0] + 1 or any(len(row) != degrees[1] + 1 for row in poles):
        raise ValueError("native single-patch degree and pole grid disagree")
    homogeneous = []
    for pole_row, weight_row in zip(poles, weights, strict=True):
        row = []
        for point, weight in zip(pole_row, weight_row, strict=True):
            w = Fraction.from_float(float(weight))
            row.append(tuple(Fraction.from_float(float(value)) * w for value in point) + (w,))
        homogeneous.append(row)
    columns = [_restrict_bezier(list(column), *ranges[0]) for column in zip(*homogeneous, strict=True)]
    restricted = [_restrict_bezier(list(row), *ranges[1]) for row in zip(*columns, strict=True)]
    points = []
    for row in restricted:
        for point in row:
            if point[3] <= 0:
                raise ValueError("extended rational patch has a nonpositive weight")
            points.append(tuple(value / point[3] for value in point[:3]))
    exact = (tuple(min(point[i] for point in points) for i in range(3))
             + tuple(max(point[i] for point in points) for i in range(3)))
    limits = tuple(Fraction.from_float(value) for value in
                   (page[0] - 1e-7, page[1] - 1e-7, page[2] + 1e-7, page[3] + 1e-7))
    if exact[0] < limits[0] or exact[1] < limits[1] or exact[3] > limits[2] or exact[4] > limits[3]:
        raise ValueError("exact native single-patch hull crosses the source page")
    box = []
    for index, value in enumerate(exact):
        rounded = float(value)
        if not math.isfinite(rounded):
            raise ValueError("exact native patch has unrepresentable bounds")
        if (index < 3 and Fraction.from_float(rounded) > value
                or index >= 3 and Fraction.from_float(rounded) < value):
            rounded = math.nextafter(rounded, -math.inf if index < 3 else math.inf)
        box.append(rounded)
    return {"method": "exact_homogeneous_single_patch_hull", "surface": "BSplineSurface",
            "parameter_range": list(domain), "support_knots": [list(values) for values in knots],
            "degrees": list(degrees), "covers_endpoint_clamp_and_continuation": True,
            "bounds": box, "exact_bounds": [str(value) for value in exact]}


def _planar_boundary_containment(face, page, domain, support_proof):
    if not face.OuterWire.isClosed() or not face.Edges:
        raise ValueError("native planar face does not have a closed bounded boundary")
    edges = [_edge_containment(edge, page) for edge in face.Edges]
    box = _point_bounds([point for edge in edges for point in
                         (edge["bounds"][:3], edge["bounds"][3:])])
    return {"method": "exact_planar_closed_boundary_hull", "surface": type(face.Surface).__name__,
            "parameter_range": list(domain), **support_proof, "bounds": list(box), "edges": edges}


def _face_containment(face, page):
    box = _bounds(face)
    if _inside_page(box, page):
        return {"method": "native_analytic_bounds", "bounds": list(box)}
    surface = face.Surface
    kind = type(surface).__name__
    domain = _finite_domain(face.ParameterRange, 4)
    if kind == "Plane":
        return _planar_boundary_containment(face, page, domain, {"planarity": "native_plane"})
    if kind not in {"BSplineSurface", "BezierSurface"}:
        raise ValueError("native face has no supported conservative containment proof")
    trimmed = surface.copy()
    trimmed.segment(*domain)
    box = _surface_hull(trimmed)
    # A BSpline segment can snap a boundary knot by an ulp. Its hull is usable
    # only if its domain still covers the entire face's conservative UV bounds.
    segment_domain = _finite_domain(trimmed.bounds(), 4)
    covers_domain = kind == "BezierSurface" or all(
        segment_domain[i] <= domain[i] and segment_domain[i + 1] >= domain[i + 1]
        for i in (0, 2)
    )
    if covers_domain and _inside_page(box, page):
        return {"method": "positive_rational_trimmed_surface_hull", "surface": kind,
                "parameter_range": list(domain), "bounds": list(box)}
    if not covers_domain and kind == "BSplineSurface":
        try:
            return _single_patch_surface_hull(surface, domain, page)
        except (AttributeError, ValueError):
            # An unsupported patch can still have independently proved exact
            # planarity below. Never use its incomplete trimmed domain alone.
            pass
    # The UV rectangle may overcover a trimmed cap. Prove exact planarity from
    # the WHOLE original rational support, not samples or a tolerance plane fit.
    # A bounded planar face lies in the convex hull of its complete boundary.
    support = _surface_hull(surface)
    constant_axes = [axis for axis in range(3) if support[axis] == support[axis + 3]]
    if not constant_axes:
        raise ValueError("native surface hull crosses the source page")
    return _planar_boundary_containment(face, page, domain, {"constant_axes": constant_axes})


def _native_containment(shape, page):
    """Conservative fallback for OCC's baked-in Precision::Confusion box gap.

    No padding is subtracted and no tolerance is enlarged. Positive rational
    weights make the finite control hull contain the complete native geometry.
    Unsupported or insufficient hull proofs retain the original clipping failure.
    """
    if not shape.Edges:
        raise ValueError("native containment requires bounded edges")
    edges = [_edge_containment(edge, page) for edge in shape.Edges]
    faces = [_face_containment(face, page) for face in shape.Faces]
    vertices = _point_bounds([vertex.Point for vertex in shape.Vertexes])
    if not _inside_page(vertices, page):
        raise ValueError("native clipped vertex crosses the source page")
    box = _point_bounds([point for row in edges + faces for point in
                         (row["bounds"][:3], row["bounds"][3:])] + [vertices[:3], vertices[3:]])
    return {"method": "native_curve_surface_containment", "bounds": list(box),
            "edges": edges, "faces": faces}


def clip_native_text_shape(host_obj, page_quad, *, part, vector):
    """Intersect measured native ink with the page prism and verify conservation.

    A character's metric box never authorizes clipping. The actual exact-font
    shape must cross the page, and native common/cut must partition its solid
    volume, filled area, or outline length. The source semantic properties and
    placement remain on the same Part::Feature; no representation fallback occurs.
    """
    page = _page_bounds(page_quad)
    source = host_obj.Shape.copy()
    if source.isNull():
        return None
    before = _bounds(source)
    if (before[0] >= page[0] and before[1] >= page[1]
            and before[3] <= page[2] and before[4] <= page[3]):
        return None
    if str(host_obj.TypeId) != "Part::Feature":
        # A parametric object's next recompute would replace a direct Shape write.
        raise ValueError("source page clipping requires a persistent exact text feature")
    if not source.isValid():
        raise ValueError("source text shape is invalid before page clipping")
    dimension = "Volume" if source.Solids else "Area" if source.Faces else "Length"
    original_measure = _measure(source, dimension)
    if original_measure <= 0.0:
        raise ValueError("source text shape has no measurable ink")
    padding = max(1.0, before[5] - before[2])
    prism = part.makeBox(page[2] - page[0], page[3] - page[1],
                         before[5] - before[2] + 2.0 * padding,
                         vector(page[0], page[1], before[2] - padding))
    kept, removed = source.common(prism), source.cut(prism)
    kept_measure, removed_measure = _measure(kept, dimension), _measure(removed, dimension)
    tolerance = max(1e-9, original_measure * 1e-7)
    if not math.isclose(kept_measure + removed_measure, original_measure,
                        rel_tol=1e-7, abs_tol=1e-9):
        raise ValueError("native page clipping did not conserve source ink")
    if removed_measure == 0.0:
        # OCC bounding boxes may enclose tolerance padding outside the actual ink.
        return None
    if kept_measure == 0.0:
        # OCC may return an empty compound (not a null shape). Zero retained
        # ink has no finite geometric bounds; keep only the semantic object.
        kept = part.Shape()
    after = None
    containment = None
    if not kept.isNull():
        if not kept.isValid():
            raise ValueError("native page clipping produced invalid geometry")
        after = _bounds(kept)
        if not _inside_page(after, page):
            try:
                containment = _native_containment(kept, page)
            except Exception as error:
                raise ValueError("clipped native text still crosses its source page") from error
        if abs(after[2] - before[2]) > 1e-7 or abs(after[5] - before[5]) > 1e-7:
            raise ValueError("page clipping changed text extrusion depth")
        if _measure(kept.cut(prism), dimension) > tolerance:
            raise ValueError("clipped native text retains out-of-page ink")
    placement = host_obj.Placement
    # Shape returns world-placed geometry. Remove that placement for assignment,
    # then restore the unchanged source anchor/rotation on the persistent object.
    local = kept.copy() if not kept.isNull() else kept
    if not local.isNull():
        # Bake only the rigid placement. transformGeometry converts conics to
        # splines and can change their ink; copy=False leaves an OCC location
        # which assigning the object's Placement would overwrite.
        local.transformShape(placement.inverse().toMatrix(), True)
        if not math.isclose(_measure(local, dimension), kept_measure,
                            rel_tol=1e-7, abs_tol=1e-9):
            raise ValueError("rigid placement changed clipped native text ink")
    host_obj.Shape = local
    host_obj.Placement = placement
    actual = host_obj.Shape
    if actual.isNull() != kept.isNull() or not math.isclose(
        _measure(actual, dimension), kept_measure, rel_tol=1e-7, abs_tol=1e-9
    ):
        raise ValueError("host did not preserve the clipped native text shape")
    if after is not None and any(abs(a - b) > 1e-7 for a, b in
                                  zip(_bounds(actual), after, strict=True)):
        raise ValueError("host changed the clipped text placement")
    if after is not None and (
        _measure(actual.cut(kept), dimension) > tolerance
        or _measure(kept.cut(actual), dimension) > tolerance
    ):
        raise ValueError("host changed the clipped native text geometry")
    if after is not None and not _inside_page(_bounds(actual), page):
        try:
            containment = _native_containment(actual, page)
        except Exception as error:
            raise ValueError("persisted clipped native text crosses its source page") from error
    proof = {
        "schema": "bcs.freecad.source_page_text_clip/1",
        "status": "clipped" if kept_measure > 0.0 else "fully_clipped",
        "page_bounds": list(page), "source_ink_bounds": list(before),
        "delivered_ink_bounds": list(after) if after is not None else None,
        "measure": dimension.lower(), "source_measure": original_measure,
        "delivered_measure": kept_measure, "removed_measure": removed_measure,
        "extrusion_depth_preserved": True,
    }
    if containment is not None:
        # Retain the raw native box above. This separate conservative certificate
        # explains why a broad AddOptimal box did not represent out-of-page ink.
        proof["native_containment"] = containment
    if "PDFSourcePageClipJSON" not in host_obj.PropertiesList:
        host_obj.addProperty("App::PropertyString", "PDFSourcePageClipJSON", "PDF Import")
    encoded = json.dumps(proof, sort_keys=True, separators=(",", ":"), allow_nan=False)
    host_obj.PDFSourcePageClipJSON = encoded
    if host_obj.PDFSourcePageClipJSON != encoded:
        raise ValueError("source page clip proof did not persist on the text entity")
    return proof
