# -*- coding: utf-8 -*-
"""import_report text_mode parity (bcs.import_report/1.1)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "PDFVectorImporter"))

from pdfcadcore.import_report import build_import_report


def test_build_import_report_includes_text_mode_in_extra():
    report = build_import_report(
        host_app="freecad",
        pdf_path="sample.pdf",
        mode="auto",
        import_text=True,
        text_mode="3d_text",
    )
    data = report.to_dict()
    assert data["mode"] == "auto"
    assert data["extra"]["text_mode"] == "3d_text"
    assert data["extra"]["import_text"] is True


def test_build_import_report_geometry_mode():
    report = build_import_report(
        host_app="blender",
        pdf_path="sample.pdf",
        mode="vector",
        import_text=True,
        text_mode="geometry",
        performance_phases={"geometry_ms": 10.5},
        helper_timings_ms={"svg_renderer_ms": 2.0},
        text_source_spans=4,
        text_glyph_estimate=18,
        extra={"curves": 12},
    )
    data = report.to_dict()
    assert data["extra"]["text_mode"] == "geometry"
    assert data["extra"]["curves"] == 12
    assert data["extra"]["text_source_spans"] == 4
    assert data["extra"]["text_glyph_estimate"] == 18
    assert data["performance"]["phases"]["geometry_ms"] == 10.5
    assert data["performance"]["helpers_ms"]["svg_renderer_ms"] == 2.0


@pytest.mark.parametrize(
    "text_mode",
    ["labels", "3d_text", "glyphs", "geometry"],
)
def test_all_text_modes_round_trip(text_mode: str):
    report = build_import_report(
        host_app="librecad",
        pdf_path="drawing.pdf",
        text_mode=text_mode,
        import_text=text_mode != "geometry",
    )
    assert report.to_dict()["extra"]["text_mode"] == text_mode
