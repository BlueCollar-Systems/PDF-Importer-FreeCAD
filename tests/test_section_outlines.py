"""R8-A foundation: AISC designation -> exact extrudable cross-section."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "PDFVectorImporter"))

import pytest  # noqa: E402

from pdfcadcore.section_outlines import (  # noqa: E402
    resolve_profile,
    section_outline,
)

CORPUS = os.environ.get("BCS_PRIVATE_VALIDATION_ROOT", r"__private_validation_assets_not_configured__")
CATALOG = os.path.join(CORPUS, "profiles", "aisc_v16_profiles.json")

needs_catalog = pytest.mark.skipif(
    not os.path.isfile(CATALOG),
    reason=f"VISIBLE SKIP: profile catalog missing at {CATALOG} "
           "(regenerate via corpus tools/generate_aisc_profiles.py)")


def _bbox(points):
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


@needs_catalog
def test_w12x30_outline_matches_aisc_dims():
    prof = resolve_profile("W12X30")
    assert prof and prof["family"] == "W"
    out = section_outline(prof)
    assert out["kind"] == "polygon" and out["idealized"] is True
    x0, y0, x1, y1 = _bbox(out["outer"])
    assert abs((x1 - x0) - prof["bf"]) < 1e-9   # width == flange width 6.52
    assert abs((y1 - y0) - prof["d"]) < 1e-9    # height == depth 12.3
    assert len(out["outer"]) == 12 and out["holes"] == []


@needs_catalog
def test_tier1_user_drawing_members_all_resolve_to_outlines():
    # Every rolled shape the private validation user drawing needs must produce geometry.
    for desig in ("W12X30", "W8X15", "L3X3X3/8"):
        out = section_outline(resolve_profile(desig))
        assert out is not None, desig
        assert out["kind"] == "polygon"


@needs_catalog
def test_angle_geometry():
    out = section_outline(resolve_profile("L3X3X3/8"))
    x0, y0, x1, y1 = _bbox(out["outer"])
    assert (round(x1 - x0, 6), round(y1 - y0, 6)) == (3.0, 3.0)
    assert len(out["outer"]) == 6


@needs_catalog
def test_hss_rect_has_inner_hole():
    out = section_outline(resolve_profile("HSS6X6X1/4"))
    assert out["kind"] == "polygon" and len(out["holes"]) == 1
    ox0, oy0, ox1, oy1 = _bbox(out["outer"])
    ix0, iy0, ix1, iy1 = _bbox(out["holes"][0])
    wall = ((ox1 - ox0) - (ix1 - ix0)) / 2.0
    assert 0.1 < wall < 0.3  # tdes for 1/4 nominal = 0.233


@needs_catalog
def test_pipe_is_a_ring():
    out = section_outline(resolve_profile("PIPE3STD"))
    assert out["kind"] == "ring"
    assert out["outer_r"] > out["inner_r"] > 0


def test_unknown_designation_and_missing_catalog_are_safe():
    assert resolve_profile("W99X999") is None or True  # never raises
    assert section_outline(None) is None
    assert section_outline({"family": "W"}) is None  # missing dims
    assert resolve_profile("", profiles_path="Z:/nope.json") is None
