"""A gapped two-point stroke is its own subpath, not a failed wire."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


def _pt(x, y):
    return SimpleNamespace(x=x, y=y)


def test_page_sized_x_is_two_strokes_not_one_wire():
    # `0 612 m 792 0 l 0 0 m 792 612 l S` arrives as two-point lines with no
    # moveto between them. One wire across that gap used to fail the page and
    # roll the whole file back.
    first_end = _pt(792.0, 0.0)
    second_start = _pt(0.0, 0.0)
    assert core._disconnected_stroke_starts_new_subpath(first_end, second_start)


def test_a_continuous_polyline_stays_one_subpath():
    end = _pt(10.0, 4.0)
    assert core._disconnected_stroke_starts_new_subpath(end, _pt(10.0, 4.0)) is False
    assert core._disconnected_stroke_starts_new_subpath(end, _pt(10.0 + 1e-7, 4.0)) is False


def test_the_first_segment_has_nothing_to_break_from():
    assert core._disconnected_stroke_starts_new_subpath(None, _pt(0.0, 0.0)) is False
