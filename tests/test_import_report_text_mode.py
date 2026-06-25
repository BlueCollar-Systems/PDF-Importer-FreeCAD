# -*- coding: utf-8 -*-
"""import_report text_mode parity (bcs.import_report/1.1)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "PDFVectorImporter"))

from pdfcadcore.import_report import (
    build_font_embedding_hints,
    build_import_report,
    build_pdf_interactive_note,
)


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
    diagnostics = data["extra"]["diagnostics"]
    assert diagnostics["quality_level"] == "empty"
    assert "text_mode_geometry" in diagnostics["signals"]
    assert diagnostics["recommended_actions"]


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


def test_import_report_diagnostics_for_fallback_and_dense_text():
    report = build_import_report(
        host_app="freecad",
        pdf_path="scan.pdf",
        mode="auto",
        primitive_count=0,
        text_count=0,
        layer_count=0,
        warnings=2,
        fallback_used=True,
        fallback_reason="raster_fallback_1_pages",
        import_text=True,
        text_mode="glyphs",
        text_source_spans=14,
        text_glyph_estimate=1200,
    )
    diagnostics = report.to_dict()["extra"]["diagnostics"]
    assert diagnostics["quality_level"] == "empty"
    assert "fallback_used" in diagnostics["signals"]
    assert "warnings_present" in diagnostics["signals"]
    assert "source_text_seen_but_no_text_entities_created" in diagnostics["signals"]
    assert "dense_text_glyph_workload" in diagnostics["signals"]
    assert any("Vector or Hybrid" in action for action in diagnostics["recommended_actions"])


def test_font_embedding_hints_uses_extension_not_referencer():
    class Page:
        def get_fonts(self, full=True):
            assert full is True
            return [
                (8, "otf", "Type0", "AAAAAA+EmbeddedFont", "F0", "Identity-H", 0),
                (9, "n/a", "Type1", "Helvetica-Bold", "F1", "", 0),
            ]

    class Doc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return Page()

    hints = build_font_embedding_hints(Doc())
    assert hints["non_embedded_fonts"] == ["Helvetica-Bold"]
    assert "Labels mode may substitute" in hints["font_substitution_note"]


def test_pdf_interactive_note_ignores_null_catalog_keys():
    class Doc:
        def pdf_catalog(self):
            return 1

        def xref_get_key(self, xref, key):
            return ("null", "null")

        def xref_length(self):
            return 2

    assert build_pdf_interactive_note(Doc()) == {}


def test_pdf_interactive_note_detects_javascript_action():
    class Doc:
        def pdf_catalog(self):
            return 1

        def xref_get_key(self, xref, key):
            if xref == 2 and key == "S":
                return ("name", "/JavaScript")
            return ("null", "null")

        def xref_length(self):
            return 3

    note = build_pdf_interactive_note(Doc())
    assert note["pdf_interactive_flags"] == ["JavaScript"]
    assert "scripts are not executed" in note["pdf_interactive_note"]
