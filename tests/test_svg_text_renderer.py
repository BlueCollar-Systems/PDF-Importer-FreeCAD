# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "PDFVectorImporter"))

renderer = importlib.import_module("PDFVectorImporter.src.PDFSvgTextRenderer")


def test_parse_pdftocairo_and_pymupdf_glyph_definitions():
    svg = """
      <svg viewBox="0 0 100 100">
        <defs>
          <g id="glyph-0-1"><path d="M0 0 L10 0"/></g>
          <path id="font_0001" d="M1 1 L3 1"/>
          <path id="not-text" d="M2 2 L4 2"/>
        </defs>
      </svg>
    """

    glyphs = renderer._parse_glyph_defs(svg)

    assert "glyph-0-1" in glyphs
    assert "font_0001" in glyphs
    assert "not-text" not in glyphs


def test_parse_use_placements_accepts_svg_text_reference_families():
    svg = """
      <svg>
        <use href="#glyph-0-1" x="1" y="2"/>
        <use xlink:href="#font_0001" transform="matrix(1 0 0 1 3 4)"/>
        <use href="#image_1" x="9" y="9"/>
      </svg>
    """

    placements = renderer._parse_use_placements(svg)

    assert [p[0] for p in placements] == ["glyph-0-1", "font_0001"]
    assert placements[0][1:3] == (1.0, 2.0)
    assert placements[1][3] == [1.0, 0.0, 0.0, 1.0, 3.0, 4.0]


def test_svg_payload_guard_defaults_and_env(monkeypatch):
    monkeypatch.delenv("BC_FC_SVG_TEXT_MAX_BYTES", raising=False)
    assert renderer._max_svg_text_bytes() == 50_000_000

    monkeypatch.setenv("BC_FC_SVG_TEXT_MAX_BYTES", "5")
    assert renderer._svg_too_large("123456")
    assert not renderer._svg_too_large("12345")

    monkeypatch.setenv("BC_FC_SVG_TEXT_MAX_BYTES", "-1")
    assert renderer._max_svg_text_bytes() == 50_000_000


def test_load_fitz_uses_validated_loader(monkeypatch):
    calls = []
    fake_module = types.ModuleType("pdfcadcore.fitz_loader")

    def fake_import_fitz(*, prefer_lib_dir=None):
        calls.append(prefer_lib_dir)
        return "fitz-module"

    fake_module.import_fitz = fake_import_fitz
    monkeypatch.setitem(sys.modules, "pdfcadcore.fitz_loader", fake_module)
    monkeypatch.delitem(sys.modules, "PDFVectorImporter.pdfcadcore.fitz_loader", raising=False)

    assert renderer._load_fitz() == "fitz-module"
    assert calls
    assert str(calls[0]).endswith(str(Path("PDFVectorImporter") / "src" / "lib"))
