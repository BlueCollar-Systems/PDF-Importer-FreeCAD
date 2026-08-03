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


def test_synthetic_bom_rows_detect_plates_and_members():
    # Deliberately synthetic fabrication rows; private validation identifiers
    # must remain outside the public repository.
    rows = [
        'p9001 PL1/2"X5 1/2"',
        'p9002 PL5/8"X8 1/4"',
        'p9003 PL1"X9"',
        "w9004 W6X9 4'-2 1/2\"",
        "w9005 W10X22 11'-6 1/2\"",
        "a9006 L2X2X1/4",
        "9007FR9 W10X22",
    ]
    intent = analyze_model3d_intent(rows)
    assert intent.feasible is True

    plates = {p.callout: p for p in intent.plates}
    assert plates['PL1/2"X51/2"'.upper()].thickness_in == 0.5
    assert plates['PL1/2"X51/2"'.upper()].width_in == 5.5
    assert plates['PL1/2"X51/2"'.upper()].mark == "p9001"
    assert plates['PL1"X9"'].thickness_in == 1.0

    members = {m.designation: m for m in intent.members}
    assert members["W6X9"].family == "W"
    assert abs(members["W6X9"].length_in - 50.5) < 1e-6
    assert members["W10X22"].count == 2  # beam row + synthetic assembly row
    assert members["L2X2X1/4"].family == "L"


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
