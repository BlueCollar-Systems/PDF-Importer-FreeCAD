"""R8-A semantic member planning from model_3d_intent."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "PDFVectorImporter"))

from pdfcadcore.semantic_members import plan_semantic_members  # noqa: E402

CORPUS = os.environ.get("BCS_PRIVATE_VALIDATION_ROOT", r"__private_validation_assets_not_configured__")
CATALOG = os.path.join(CORPUS, "profiles", "aisc_v16_profiles.json")

needs_catalog = pytest.mark.skipif(
    not os.path.isfile(CATALOG),
    reason=f"VISIBLE SKIP: profile catalog missing at {CATALOG} "
           "(regenerate via corpus tools/generate_aisc_profiles.py)",
)


@needs_catalog
def test_plan_semantic_members_from_synthetic_intent():
    intent = {
        "feasible": True,
        "members": [
            {"designation": "W10X22", "family": "W", "mark": "w9005", "length_in": 138.5, "count": 1},
            {"designation": "W6X9", "family": "W", "mark": "w9004", "length_in": 50.5, "count": 1},
            {"designation": "L2X2X1/4", "family": "L", "mark": "a9006", "count": 1},
        ],
        "plates": [],
    }
    result = plan_semantic_members(intent, enabled=True)
    assert result.enabled is True
    assert result.members_created >= 2
    assert len(result.plans) == 3
    wide_flange = next(p for p in result.plans if p.designation == "W10X22")
    assert wide_flange.profile_found is True
    assert wide_flange.profile is not None
    assert wide_flange.profile.get("family") == "W"


def test_semantic_members_off_by_default():
    result = plan_semantic_members({"feasible": True, "members": [{"designation": "W10X22"}]}, enabled=False)
    assert result.enabled is False
    assert result.members_created == 0


def test_unknown_designation_skipped():
    intent = {
        "feasible": True,
        "members": [{"designation": "W999X999", "family": "W", "count": 1}],
        "plates": [],
    }
    result = plan_semantic_members(intent, enabled=True)
    assert result.members_skipped == 1
    assert result.plans[0].skip_reason == "designation not in AISC catalog"
