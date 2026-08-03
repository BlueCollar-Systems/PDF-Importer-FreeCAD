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


def test_page_placements_expand_only_rendered_reusable_uses_inside_svg_defs() -> None:
    svg = (
        '<svg viewBox="0 0 200 100"><defs>'
        '<g id="glyph-0-1"><path d="M0 0L1 0Z"/></g>'
        '<g id="source-1"><use href="#glyph-0-1" x="1" y="2"/></g>'
        "</defs>"
        '<use href="#source-1" transform="matrix(1,0,0,1,20,30)"/>'
        '<use href="#glyph-0-1" x="23" y="34"/>'
        "</svg>"
    )

    assert renderer._parse_use_placements(svg) == [
        ("glyph-0-1", 0.0, 0.0, [1.0, 0.0, 0.0, 1.0, 21.0, 32.0]),
        ("glyph-0-1", 23.0, 34.0, None)
    ]


def test_parses_rendered_raster_source_placements_without_definition_internals() -> None:
    svg = (
        '<svg viewBox="0 0 400 300"><defs>'
        '<image id="source-17" x="0" y="0" width="23" height="24" '
        'href="data:image/png;base64,AA=="/>'
        '<g id="source-18"><use href="#glyph-0-1" x="1" y="2"/></g>'
        "</defs>"
        '<use href="#source-17" transform="matrix(.48,0,0,.48,251.52,51.84)"/>'
        '<use href="#source-18" transform="matrix(1,0,0,1,20,30)"/>'
        "</svg>"
    )

    placements = renderer._parse_raster_source_placements(svg)

    assert len(placements) == 1
    assert placements[0][0] == "source-17"
    assert placements[0][1] == pytest.approx((251.52, 51.84, 262.56, 63.36))


def test_flattens_rendered_poppler_vector_source_into_page_glyph_placements() -> None:
    svg = (
        '<svg xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 200 100">'
        "<defs>"
        '<g id="glyph-0-1"><path d="M0 0L1 0Z"/></g>'
        '<g id="source-22"><g><use xlink:href="#glyph-0-1" x="1" y="2"/></g></g>'
        "</defs>"
        '<use xlink:href="#source-22" transform="matrix(.5,0,0,.5,20,30)"/>'
        "</svg>"
    )

    assert renderer._parse_use_placements(svg) == [
        ("glyph-0-1", 0.0, 0.0, [0.5, 0.0, 0.0, 0.5, 20.5, 31.0])
    ]


def test_poppler_empty_glyph_group_is_recorded_as_intentional_empty_outline() -> None:
    svg = (
        '<svg viewBox="0 0 200 100"><defs>'
        '<g id="glyph-0-1"><path d="M0 0L1 0Z"/></g>'
        '<g id="glyph-0-2">\n</g>'
        "</defs>"
        '<use href="#glyph-0-1" x="3" y="4"/>'
        '<use href="#glyph-0-2" x="5" y="4"/>'
        "</svg>"
    )

    assert renderer._parse_all_glyph_defs(svg) == {
        "glyph-0-1": "M0 0L1 0Z",
        "glyph-0-2": "",
    }


def test_svg_size_guard_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BC_FC_SVG_TEXT_MAX_BYTES", "8")
    assert renderer._svg_too_large("012345678")
    assert not renderer._svg_too_large("01234567")

    monkeypatch.setenv("BC_FC_SVG_TEXT_MAX_BYTES", "bad")
    assert not renderer._svg_too_large("012345678")


def test_pdftocairo_oversized_temp_file_is_rejected_before_read_and_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svg_path = tmp_path / "oversized.svg"

    def make_temp(**_kwargs):
        fd = os.open(str(svg_path), os.O_CREAT | os.O_RDWR)
        return fd, str(svg_path)

    def render_file(_cmd, **_kwargs):
        svg_path.write_bytes(b"012345678")

    monkeypatch.setenv("BC_FC_SVG_TEXT_MAX_BYTES", "8")
    monkeypatch.setattr(renderer.tempfile, "mkstemp", make_temp)
    monkeypatch.setattr(renderer.subprocess, "run", render_file)
    monkeypatch.setattr(
        renderer,
        "open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("oversized SVG must not be read")
        ),
        raising=False,
    )

    with pytest.raises(renderer.TextRepresentationRenderError) as raised:
        renderer._render_svg_with_pdftocairo("pdftocairo", "fixture.pdf", 1)

    assert raised.value.reason == "svg_payload_too_large"
    assert raised.value.evidence == {
        "renderer": "pdftocairo",
        "svg_bytes": 9,
        "max_svg_bytes": 8,
    }
    assert not svg_path.exists()


def test_pdftocairo_temp_file_at_exact_limit_is_read_and_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    svg_path = tmp_path / "at-limit.svg"

    def make_temp(**_kwargs):
        fd = os.open(str(svg_path), os.O_CREAT | os.O_RDWR)
        return fd, str(svg_path)

    def render_file(_cmd, **_kwargs):
        svg_path.write_bytes(b"01234567")

    monkeypatch.setenv("BC_FC_SVG_TEXT_MAX_BYTES", "8")
    monkeypatch.setattr(renderer.tempfile, "mkstemp", make_temp)
    monkeypatch.setattr(renderer.subprocess, "run", render_file)

    assert renderer._render_svg_with_pdftocairo(
        "pdftocairo", "fixture.pdf", 1
    ) == "01234567"
    assert not svg_path.exists()


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
