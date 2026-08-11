# -*- coding: utf-8 -*-
# Guard: circle_fit's accumulators must stay as builtin sum() calls.
# BlueCollar Systems — BUILT. NOT BOUGHT.
#
# Why a source-level guard rather than a numeric assertion: the accuracy
# difference this protects only EXISTS on CPython >= 3.12, where sum() applies
# Neumaier compensated summation to floats (gh-100425). FreeCAD 0.21 bundles
# Python 3.11, where sum() is uncompensated and a manual loop is numerically
# identical -- so a numeric test would silently pass on the FreeCAD host while
# the regression shipped to Blender (3.13) and LibreCAD. The source check fires
# on every interpreter, which is the only way this actually holds.
import inspect
import re

from pdfcadcore import geometry_cleanup

ACCUMULATORS = ("sx", "sy", "sx2", "sy2", "sxy", "sz", "sxz", "syz")


def _circle_fit_body() -> str:
    return inspect.getsource(geometry_cleanup.circle_fit)


def test_every_accumulator_uses_builtin_sum():
    body = _circle_fit_body()
    for name in ACCUMULATORS:
        assert re.search(rf"^\s*{re.escape(name)}\s*=\s*sum\(", body, re.M), (
            f"circle_fit accumulator {name!r} is no longer a builtin sum(). "
            "On CPython >= 3.12 sum() is compensated (Neumaier) and an explicit "
            "+= loop is measurably less accurate, which perturbs the arc/circle "
            "promotion thresholds in promote_circular_primitives(). Held on "
            "owner decision 2026-08-11 -- re-decide before changing."
        )


def test_no_manual_accumulation_loop_reintroduced():
    body = _circle_fit_body()
    chained = re.search(r"^\s*sx\s*=\s*sy\s*=", body, re.M)
    assert not chained, (
        "circle_fit re-introduced the single-pass accumulation rewrite "
        "(chained zero-init of the accumulators). See the docstring: this is an "
        "accuracy regression on CPython >= 3.12, not a free speed win."
    )
    for name in ACCUMULATORS:
        assert not re.search(rf"^\s*{re.escape(name)}\s*\+=", body, re.M), (
            f"circle_fit accumulates {name!r} with += instead of sum(); "
            "that loses Neumaier compensation on CPython >= 3.12."
        )


def test_still_fits_an_exact_circle():
    # Characterization guard so the summation rule cannot be satisfied by a
    # function that no longer fits circles at all.
    import math

    cx, cy, radius = 12.5, -3.25, 7.0
    points = [
        (cx + radius * math.cos(i * math.tau / 16),
         cy + radius * math.sin(i * math.tau / 16))
        for i in range(16)
    ]
    got = geometry_cleanup.circle_fit(points)
    assert got is not None
    fx, fy, fr, rms = got
    assert abs(fx - cx) < 1e-9
    assert abs(fy - cy) < 1e-9
    assert abs(fr - radius) < 1e-9
    assert rms < 1e-9


def test_returns_none_below_three_points():
    assert geometry_cleanup.circle_fit([(0.0, 0.0), (1.0, 1.0)]) is None
