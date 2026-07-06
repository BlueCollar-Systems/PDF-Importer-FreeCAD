"""Round 8 slice 1: 3D-intent detection from drawing text (owner directive)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "PDFVectorImporter"))

from pdfcadcore.model3d_intent import (  # noqa: E402
    analyze_model3d_intent,
    parse_fraction_inches,
)


def test_fraction_parsing_exact():
    assert parse_fraction_inches("3/8") == 0.375
    assert parse_fraction_inches("6 7/8") == 6.875
    assert parse_fraction_inches('1.25"') == 1.25
    assert parse_fraction_inches("") is None
    assert parse_fraction_inches("0/0") is None


def test_tier1_user_bom_rows_detect_plates_and_members():
    # Representative BOM rows from Tier-1 user fabrication drawing (T1-01).
    rows = [
        'p1016 PL3/8"X6 7/8"',
        'p1019 PL3/8"X7 3/4"',
        'p1052 PL3/4"X7"',
        "w1023 W8X15 3'-11 3/4\"",
        "w1025 W12X30 13'-11 1/4\"",
        "a1005 L3X3X3/8",
        "1017FR1 W12X30",
    ]
    intent = analyze_model3d_intent(rows)
    assert intent.feasible is True

    plates = {p.callout: p for p in intent.plates}
    assert plates['PL3/8"X67/8"'.upper()].thickness_in == 0.375
    assert plates['PL3/8"X67/8"'.upper()].width_in == 6.875
    assert plates['PL3/8"X67/8"'.upper()].mark == "p1016"
    assert plates['PL3/4"X7"'].thickness_in == 0.75

    members = {m.designation: m for m in intent.members}
    assert members["W8X15"].family == "W"
    assert abs(members["W8X15"].length_in - (3 * 12 + 11.75)) < 1e-6
    assert members["W12X30"].count == 2  # w1025 row + 1017FR1 assembly row
    assert members["L3X3X3/8"].family == "L"


def test_hss_pipe_and_channel_designations():
    intent = analyze_model3d_intent(
        ["HSS6X6X1/4", "PIPE3STD", "C8X11.5", "WT5X22.5"])
    fams = sorted(m.family for m in intent.members)
    assert fams == ["C", "HSS", "PIPE", "WT"]


def test_no_evidence_is_honestly_infeasible():
    intent = analyze_model3d_intent(
        ["SCALE: 1/4\" = 1'-0\"", "SECTION F-F", "1'-6 1/2", "TYP."])
    assert intent.feasible is False
    assert "third-dimension" in intent.skipped_reason
    assert intent.plates == [] and intent.members == []


def test_2d_host_reports_capability_elsewhere():
    intent = analyze_model3d_intent(['PL1/2"X8"'], host_supports_3d=False)
    assert intent.feasible is False
    assert "3D-capable host" in intent.skipped_reason
    assert intent.plates[0].thickness_in == 0.5  # evidence still reported


def test_scale_fractions_do_not_become_plates():
    # '1/4" = 1'-0"' must never read as a plate; dedup counts repeats.
    intent = analyze_model3d_intent(['PL3/8"X7"', 'PL3/8"X7"', '3/8'])
    assert intent.feasible is True
    assert len(intent.plates) == 1
    assert intent.plates[0].count == 2


def test_to_dict_report_shape():
    d = analyze_model3d_intent(['p1 PL3/8"X7"', "W8X15"]).to_dict()
    assert d["feasible"] is True
    assert d["plates"][0]["thickness_in"] == 0.375
    assert d["members"][0]["designation"] == "W8X15"
    assert d["skipped_reason"] is None
