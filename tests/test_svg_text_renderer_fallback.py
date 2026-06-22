# -*- coding: utf-8 -*-
"""SVG text renderer fallback coverage for clean-PC installs."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
LIB_DIR = SRC_DIR / "lib"

for p in (str(SRC_DIR), str(LIB_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import PDFSvgTextRenderer as renderer  # noqa: E402


def test_parses_pymupdf_font_path_ids_and_placements() -> None:
    svg = (
        '<svg viewBox="0 0 200 100"><defs>'
        '<path id="font_1_34" d="M0 0L1 0Z"/>'
        "</defs>"
        '<use data-text="A" xlink:href="#font_1_34" '
        'transform="matrix(12,0,0,-12,20,50)"/>'
        "</svg>"
    )

    assert renderer._parse_glyph_defs(svg) == {"font_1_34": "M0 0L1 0Z"}
    assert renderer._parse_use_placements(svg) == [
        ("font_1_34", 0.0, 0.0, [12.0, 0.0, 0.0, -12.0, 20.0, 50.0])
    ]


def test_parses_poppler_glyph_group_ids() -> None:
    svg = (
        '<svg viewBox="0 0 200 100"><defs>'
        '<g id="glyph-0-1"><path d="M0 0L1 0Z"/></g>'
        "</defs>"
        '<use xlink:href="#glyph-0-1" x="3" y="4"/>'
        "</svg>"
    )

    assert renderer._parse_glyph_defs(svg) == {"glyph-0-1": "M0 0L1 0Z"}
    assert renderer._parse_use_placements(svg) == [("glyph-0-1", 3.0, 4.0, None)]


def test_svg_size_guard_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BC_FC_SVG_TEXT_MAX_BYTES", "8")
    assert renderer._svg_too_large("012345678")
    assert not renderer._svg_too_large("01234567")

    monkeypatch.setenv("BC_FC_SVG_TEXT_MAX_BYTES", "bad")
    assert not renderer._svg_too_large("012345678")


def test_pymupdf_svg_fallback_exports_text_as_paths(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz")
    pdf_path = tmp_path / "text.pdf"

    doc = fitz.open()
    page = doc.new_page(width=200, height=100)
    page.insert_text((20, 50), "ABC", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    svg = renderer._render_svg_with_pymupdf(str(pdf_path), 1)

    assert svg
    assert "font_" in svg
    assert "<use" in svg
    assert renderer._parse_glyph_defs(svg)
    assert renderer._parse_use_placements(svg)
