"""P1-5 (week-plan PH2-FC-1): declared-vs-achieved text size crosscheck telemetry.

FreeCAD report-extra parity with SketchUp's ``text_width_crosscheck`` /
``text_height_crosscheck`` blocks. Values are locked by value (RB-11), never
by source text: the width factors must be the achieved/declared baseline
advance of every DELIVERED native 3D Text span, heights the faithful nominal
targets, and failed attempts must be counted without inventing samples.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


def _verified_3d_attempt(
    declared: float,
    achieved: float,
    nominal_height: float,
    *,
    advance_key: str = "target_advance",
) -> dict:
    return {
        "attempted_type": "3d_text",
        "outcome": "verified",
        "evidence": {
            advance_key: declared,
            "verified_advance": achieved,
            "nominal_height": nominal_height,
        },
    }


def _opts_with_ledger(ledger) -> core.ImportOptions:
    opts = core.ImportOptions()
    opts.text_delivery_attempts = list(ledger)
    return opts


def test_width_factors_are_achieved_over_declared_for_delivered_spans():
    opts = _opts_with_ledger([
        _verified_3d_attempt(10.0, 10.1, 3.0),
        _verified_3d_attempt(20.0, 19.8, 4.0, advance_key="source_advance"),
        _verified_3d_attempt(8.0, 8.0, 2.5),
    ])

    blocks = core._build_text_size_crosschecks(opts)

    width = blocks["text_width_crosscheck"]
    assert width["sample_count"] == 3
    assert width["min_factor"] == 0.99
    assert width["median_factor"] == 1.0
    assert width["max_factor"] == 1.01
    assert width["out_of_tolerance_count"] == 0
    assert width["failed_attempt_count"] == 0
    assert width["policy"] == "verified_over_declared_baseline_advance"

    height = blocks["text_height_crosscheck"]
    assert height["sample_count"] == 3
    assert height["min_mm"] == 2.5
    assert height["median_mm"] == 3.0
    assert height["max_mm"] == 4.0
    assert height["policy"] == "nominal_pdf_text_matrix_height_mm"


def test_out_of_tolerance_and_failed_attempts_are_counted_not_hidden():
    opts = _opts_with_ledger([
        # 10% wide: outside max(0.05, 3%) — must be flagged, not dropped.
        _verified_3d_attempt(10.0, 11.0, 3.0),
        # Failed 3D Text attempt: counted, contributes no samples.
        {
            "attempted_type": "3d_text",
            "outcome": "failed",
            "evidence": {"target_advance": 5.0},
        },
        # Other representation modes never leak into the 3D Text blocks.
        {
            "attempted_type": "labels",
            "outcome": "verified",
            "evidence": {"target_advance": 7.0, "verified_advance": 7.0},
        },
    ])

    blocks = core._build_text_size_crosschecks(opts)

    width = blocks["text_width_crosscheck"]
    assert width["sample_count"] == 1
    assert width["out_of_tolerance_count"] == 1
    assert width["failed_attempt_count"] == 1
    assert width["min_factor"] == width["max_factor"] == 1.1


def test_failed_attempts_alone_still_produce_an_honest_width_block():
    opts = _opts_with_ledger([
        {"attempted_type": "3d_text", "outcome": "failed", "evidence": {}},
    ])

    blocks = core._build_text_size_crosschecks(opts)

    width = blocks["text_width_crosscheck"]
    assert width["sample_count"] == 0
    assert width["failed_attempt_count"] == 1
    assert "text_height_crosscheck" not in blocks


def test_no_3d_text_activity_emits_no_blocks():
    assert core._build_text_size_crosschecks(_opts_with_ledger([])) == {}

    opts = _opts_with_ledger([
        {"attempted_type": "labels", "outcome": "verified", "evidence": {}},
    ])
    assert core._build_text_size_crosschecks(opts) == {}


def test_non_finite_and_malformed_evidence_is_skipped_without_error():
    opts = _opts_with_ledger([
        _verified_3d_attempt(float("nan"), 10.0, 3.0),
        _verified_3d_attempt(0.0, 10.0, -1.0),
        {"attempted_type": "3d_text", "outcome": "verified", "evidence": None},
        "not-a-dict",
        _verified_3d_attempt(4.0, 4.0, 2.0),
    ])

    blocks = core._build_text_size_crosschecks(opts)

    assert blocks["text_width_crosscheck"]["sample_count"] == 1
    assert blocks["text_width_crosscheck"]["min_factor"] == 1.0
    # A verified span whose advance was malformed still has a real nominal
    # height — heights are counted independently of width usability.
    assert blocks["text_height_crosscheck"]["sample_count"] == 2
    assert blocks["text_height_crosscheck"]["min_mm"] == 2.0
    assert blocks["text_height_crosscheck"]["max_mm"] == 3.0
