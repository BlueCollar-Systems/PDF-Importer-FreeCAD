"""R8-A semantic member planning from model_3d_intent."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "PDFVectorImporter"))

from pdfcadcore.semantic_members import plan_semantic_members  # noqa: E402

CORPUS = os.environ.get("BCS_CORPUS_ROOT", r"C:\1pdf-test-corpus")
CATALOG = os.path.join(CORPUS, "profiles", "aisc_v16_profiles.json")

needs_catalog = pytest.mark.skipif(
    not os.path.isfile(CATALOG),
    reason=f"VISIBLE SKIP: profile catalog missing at {CATALOG} "
           "(regenerate via corpus tools/generate_aisc_profiles.py)",
)


@needs_catalog
def test_plan_semantic_members_from_1017_intent():
    intent = {
        "feasible": True,
        "members": [
            {"designation": "W12X30", "family": "W", "mark": "w1025", "length_in": 167.25, "count": 1},
            {"designation": "W8X15", "family": "W", "mark": "w1023", "length_in": 47.75, "count": 1},
            {"designation": "L3X3X3/8", "family": "L", "mark": "a1005", "count": 1},
        ],
        "plates": [],
    }
    result = plan_semantic_members(intent, enabled=True)
    assert result.enabled is True
    assert result.members_created >= 2
    assert len(result.plans) == 3
    w12 = next(p for p in result.plans if p.designation == "W12X30")
    assert w12.profile_found is True
    assert w12.profile is not None
    assert w12.profile.get("family") == "W"


def test_semantic_members_off_by_default():
    result = plan_semantic_members({"feasible": True, "members": [{"designation": "W12X30"}]}, enabled=False)
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
