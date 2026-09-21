"""Deliver these spans at the requested representation instead of degrading them.

Three measured root causes made corpus sheets fail before the fallback ladder
was ever consulted. Each is fixed conservatively: never draw a wrong glyph, and
never turn a missing measurement into a claim about the font.

Every fixture here is synthetic and deterministic.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
import PDFText3DLayout as layout  # noqa: E402


# ── 1. An undecodable hmtx is missing measurement data ────────────────────


class _Font:
    """A font file fontTools opens but whose named tables refuse to decode."""

    def __init__(self, *, cmap, tables, undecodable=()):
        self._cmap = cmap
        self._tables = tables
        self._undecodable = set(undecodable)

    def getBestCmap(self):
        if "cmap" in self._undecodable:
            raise ValueError("not enough 'cmap' table data")
        return dict(self._cmap)

    def __contains__(self, table_name):
        return table_name in self._tables or table_name in self._undecodable

    def __getitem__(self, table_name):
        if table_name in self._undecodable:
            raise ValueError("not enough %r table data" % table_name)
        return self._tables[table_name]

    def close(self):
        return None


@pytest.fixture
def clean_probe_cache():
    core._clear_font_kern_probe_cache()
    yield
    core._clear_font_kern_probe_cache()


def _install_font(monkeypatch, font):
    import fontTools.ttLib as ttlib

    monkeypatch.setattr(ttlib, "TTFont", lambda _path, **_kwargs: font)


def test_undecodable_hmtx_reports_absent_metrics_instead_of_raising(
    monkeypatch, clean_probe_cache
):
    """A PDF subsetter's truncated hmtx must not veto a font FreeType draws."""
    _install_font(
        monkeypatch,
        _Font(cmap={65: "A", 77: "M"}, tables={}, undecodable=("hmtx",)),
    )

    cmap, kern_tables, hmtx = core._font_kern_probe_tables("D042-subset.ttf")

    assert cmap == {65: "A", 77: "M"}
    assert kern_tables == []
    assert hmtx == {}
    # The caller's own "no metrics" path takes over: measure from the outlines.
    assert core._font_units_string_advance("A", "D042-subset.ttf") is None
    # ...and the character map is still usable, so the wire probe can run.
    assert core._text3d_zero_kern_probe_candidates("A", "D042-subset.ttf") == ["A", "M"]


def test_readable_hmtx_still_measures_advances(monkeypatch, clean_probe_cache):
    _install_font(
        monkeypatch,
        _Font(
            cmap={65: "A"},
            tables={"hmtx": SimpleNamespace(metrics={"A": (500, 10)})},
        ),
    )

    assert core._font_units_string_advance("AA", "D042.ttf") == pytest.approx(1000.0)


def test_unreadable_cmap_is_never_mistaken_for_a_missing_glyph(
    monkeypatch, clean_probe_cache
):
    """An unreadable character map says nothing about any one glyph."""
    _install_font(
        monkeypatch, _Font(cmap={}, tables={}, undecodable=("cmap",))
    )

    cmap, _kern, _hmtx = core._font_kern_probe_tables("D042-nocmap.ttf")
    assert cmap == {}

    with pytest.raises(RuntimeError, match="character map could not be verified"):
        core._text3d_zero_kern_probe_candidates("A", "D042-nocmap.ttf")


def test_codepoint_absent_from_a_readable_cmap_is_named_exactly(
    monkeypatch, clean_probe_cache
):
    _install_font(monkeypatch, _Font(cmap={65: "A"}, tables={}))

    with pytest.raises(RuntimeError, match=r"no glyph for source character U\+001A"):
        core._text3d_zero_kern_probe_candidates("\x1a", "D042.ttf")


class _GlyfGlyph:
    def __init__(self, box):
        self._box = box

    def draw(self, pen, glyf_table):
        assert glyf_table is not None
        x0, y0, x1, y1 = self._box
        pen.moveTo((x0, y0))
        pen.lineTo((x1, y0))
        pen.lineTo((x1, y1))
        pen.closePath()


class _GlyfTable(dict):
    pass


def test_em_ink_is_measured_from_outlines_when_advance_metrics_are_undecodable():
    """The em-ink measurement needs outlines and unitsPerEm, never advances."""

    class GlyphSetFont:
        def __init__(self, glyf):
            self._glyf = glyf

        def getGlyphSet(self):
            raise ValueError("not enough 'hmtx' table data")

        def __contains__(self, table_name):
            return table_name == "glyf"

        def __getitem__(self, table_name):
            return self._glyf

    glyf = _GlyfTable({"A": _GlyfGlyph((0, 0, 600, 1400))})
    outlines = core._text3d_source_glyph_outlines(GlyphSetFont(glyf))

    from fontTools.pens.boundsPen import BoundsPen

    pen = BoundsPen(outlines)
    outlines["A"].draw(pen)
    assert pen.bounds == (0, 0, 600, 1400)


def test_em_ink_fallback_refuses_a_variable_or_cff_font():
    """A variable font's default instance is not the instance the page uses."""

    class Font:
        def __init__(self, tables):
            self._tables = tables

        def getGlyphSet(self):
            raise ValueError("not enough 'hmtx' table data")

        def __contains__(self, table_name):
            return table_name in self._tables

        def __getitem__(self, table_name):
            return self._tables[table_name]

    with pytest.raises(ValueError, match="hmtx"):
        core._text3d_source_glyph_outlines(Font({"glyf": {}, "gvar": {}}))
    with pytest.raises(ValueError, match="hmtx"):
        core._text3d_source_glyph_outlines(Font({"CFF ": {}}))


def test_malformed_kern_values_stay_fail_closed(monkeypatch, clean_probe_cache):
    """Kerning is positional truth, not a measurement that may be missing."""
    _install_font(
        monkeypatch,
        _Font(
            cmap={65: "A"},
            tables={"kern": SimpleNamespace(kernTables="not-a-list")},
        ),
    )

    with pytest.raises(RuntimeError, match="classic kerning"):
        core._font_kern_probe_tables("D042-badkern.ttf")


# ── 3. A zero-advance combining mark or variation selector ────────────────


def _span_source(characters):
    """One span whose characters carry explicit quads and origins."""
    chars = []
    for character, x, width in characters:
        y = 20.0
        quad = ((x, y - 3.0), (x + width, y - 3.0), (x + width, y + 1.0), (x, y + 1.0))
        chars.append(
            {
                "c": character,
                "origin": (x, y),
                "quad": quad,
                "bbox": (x, y - 3.0, x + width, y + 1.0),
                "source_font_metrics": dict(
                    schema="mupdf_original_font_metrics/1",
                    text=character,
                    origin=(x, y),
                    quad=quad,
                    writing_mode=0,
                    size=3.0,
                    ascender=1.0,
                    descender=-1.0 / 3.0,
                ),
            }
        )
    bbox = (
        min(entry["bbox"][0] for entry in chars),
        min(entry["bbox"][1] for entry in chars),
        max(entry["bbox"][2] for entry in chars),
        max(entry["bbox"][3] for entry in chars),
    )
    text = "".join(entry["c"] for entry in chars)
    span = {
        "font": "Arial",
        "size": 3.0,
        "origin": chars[0]["origin"],
        "bbox": bbox,
        "chars": chars,
    }
    item = {
        "pdf_sha256": "a" * 64,
        "page_number": 1,
        "block_index": 0,
        "line_index": 0,
        "span_index": 0,
        "source_item_id": "p1:b0:l0:s0",
        "text": text,
        "span": {"font": "Arial", "size": 3.0},
        "origin": chars[0]["origin"],
        "bbox": bbox,
        "line_direction": (1.0, 0.0),
    }
    return item, {
        "blocks": [{"type": 0, "lines": [{"dir": (1.0, 0.0), "spans": [span]}]}]
    }


def _build(item, raw):
    return layout.build_source_character_layout(
        item,
        raw,
        scale=1.0,
        font_size=3.0,
        font_name="ExactArial",
        host_rotation_deg=0.0,
    )


@pytest.mark.parametrize(
    "mark", ["️", "́", "⃣", "\U000e0101", "᠋"]
)
def test_zero_advance_marks_keep_their_true_position(mark):
    """A combining mark or variation selector has no advance by definition."""
    item, raw = _span_source([("A", 10.0, 2.0), (mark, 12.0, 0.0)])

    result = _build(item, raw)

    first, second = result["characters"]
    assert first["advance"] == pytest.approx(2.0)
    assert second["text"] == mark
    assert second["advance"] == 0.0
    # The baseline direction comes from the line, never from a zero-length quad.
    assert second["baseline_axis"] == [1.0, 0.0]
    assert second["local_origin"][0] == pytest.approx(2.0)
    assert math.isfinite(second["up_scale"])


def test_a_zero_advance_letter_is_still_a_failure_and_names_the_character():
    """The escape is for characters that have no width, not for broken data."""
    item, raw = _span_source([("A", 10.0, 2.0), ("B", 12.0, 0.0)])

    with pytest.raises(ValueError, match="advance is degenerate") as caught:
        _build(item, raw)

    assert caught.value.source_character_index == 1
    assert caught.value.source_character_codepoint == "U+0042"


def test_zero_advance_predicate_is_conservative():
    # Zero-width joiner and other format characters are NOT claimed here: the
    # escape covers marks and variation selectors only.
    assert layout._has_no_source_advance("‍") is False
    assert layout._has_no_source_advance("A") is False
    assert layout._has_no_source_advance("") is False
    assert layout._has_no_source_advance(" ") is True
    assert layout._has_no_source_advance("️") is True
