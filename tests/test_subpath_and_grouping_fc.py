"""Disconnected PDF subpaths and the Grouping dropdown."""
from __future__ import annotations

from types import SimpleNamespace

from PDFVectorImporter.src.PDFImporterCore import (
    SUBPATH_BREAK_MM,
    _subpath_break,
    resolve_grouping,
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


def test_unset_grouping_keeps_the_automatic_mix():
    choice = resolve_grouping(SimpleNamespace())
    assert choice["mode"] == "auto"
    assert choice["single_root"] is False
    assert choice["layers_at_document_root"] is False


def test_dialog_grouping_choices_map_to_distinct_folder_rules():
    per_page = resolve_grouping(SimpleNamespace(grouping_mode="per_page"))
    per_color = resolve_grouping(SimpleNamespace(grouping_mode="per_color"))
    per_layer = resolve_grouping(SimpleNamespace(grouping_mode="per_layer"))
    nested_layer = resolve_grouping(SimpleNamespace(grouping_mode="nested_page_layer"))
    nested_weight = resolve_grouping(SimpleNamespace(grouping_mode="nested_page_lineweight"))
    single = resolve_grouping(SimpleNamespace(grouping_mode="single"))

    assert per_page["mode"] == "flat" and per_page["single_root"] is False
    assert per_color["use_color"] is True and per_color["use_layers"] is False
    assert per_layer["use_layers"] is True and per_layer["layers_at_document_root"] is True
    assert nested_layer["use_layers"] is True and nested_layer["layers_at_document_root"] is False
    assert nested_weight["use_lineweight"] is True
    assert single["single_root"] is True and single["mode"] == "flat"
    assert per_layer != nested_layer
