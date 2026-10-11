"""Disconnected PDF subpaths stay separate wires."""
from __future__ import annotations

from types import SimpleNamespace

from PDFVectorImporter.src.PDFImporterCore import (
    SUBPATH_BREAK_MM,
    _subpath_break,
)


def _pt(x, y):
    return SimpleNamespace(x=x, y=y)


def test_connected_segment_stays_in_the_same_subpath():
    flush, closed = _subpath_break(_pt(0.0, 0.0), _pt(0.0, SUBPATH_BREAK_MM / 2.0), _pt(0.0, 0.0))
    assert flush is False
    assert closed is False


def test_gap_after_a_closed_ring_starts_a_new_closed_subpath():
    """A donut is two circles. The inner circle must not be welded to the outer one."""
    origin = _pt(10.0, 10.0)
    end = _pt(10.0, 10.0 + SUBPATH_BREAK_MM / 10.0)
    inner = _pt(14.0, 10.0)
    flush, closed = _subpath_break(end, inner, origin)
    assert flush is True
    assert closed is True


def test_gap_in_an_open_stroke_starts_a_new_open_subpath():
    flush, closed = _subpath_break(_pt(1.0, 0.0), _pt(5.0, 0.0), _pt(0.0, 0.0))
    assert flush is True
    assert closed is False
