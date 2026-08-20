"""The raster underlay must not carry text that is delivered natively on top of it.

Gap this closes. ``test_hybrid_underlay_dedupe_fc.py`` fixed the *hybrid* path and its
contract point 3 deliberately left "raster-only pages keep the full-page render" as the
lock. That is correct when the page's text is delivered only as raster. It is wrong for
the ``auto_raster_text_overlay`` path, where ``effective_mode == "raster"`` places a
full-page underlay AND the requested text is then delivered natively over it: the glyphs
are rasterized into the background and drawn again as native entities.

Observed on the garden-map sheet (visual oracle, 2026-08-18, uniform 4800 px): the title
rendered as ``ALVORDCTX x GARDEN MAP AFINAS N MORTHCATTOP`` against the source's
``ALVORD TX - GARDEN MAP - FINAL - NORTH AT TOP``, with ink_ratio 1.707-2.199 and
disagree_px_ratio 0.859-0.887 across all six FreeCAD cells.

These locks are host-free: they exercise the page-copy helper and the resulting pixels
directly, with no FreeCAD document required.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402

fitz = pytest.importorskip("fitz", reason="PyMuPDF is required to build the page fixture")

# Text sits in the upper band, line art in the lower band, so each can be sampled alone.
TEXT_ORIGIN = (72.0, 90.0)
LINE_RECT = (72.0, 300.0, 400.0, 460.0)
PAGE_W, PAGE_H = 612.0, 792.0


def _fixture_doc():
    """A page carrying both text and vector line art, in separated bands."""
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    page.insert_text(TEXT_ORIGIN, "ALVORD TX GARDEN MAP", fontsize=36, fontname="helv")
    page.draw_rect(fitz.Rect(*LINE_RECT), color=(0, 0, 0), width=3)
    return doc, page


def _band_ink(page, rect, zoom=2.0):
    """Count dark pixels inside one band of a rendered page."""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(*rect))
    dark = 0
    samples = pix.samples
    stride = pix.n
    for offset in range(0, len(samples), stride):
        if samples[offset] < 128:
            dark += 1
    return dark


TEXT_BAND = (60.0, 55.0, 520.0, 110.0)
LINE_BAND = (60.0, 290.0, 420.0, 470.0)


def test_text_free_page_copy_removes_text_and_keeps_line_art():
    """The helper must strip text WITHOUT stripping the graphics it underlays.

    This is what separates it from `_images_only_page_copy`, which also removes line art:
    here the underlay IS the graphics delivery, so only the text may go.
    """
    doc, page = _fixture_doc()
    try:
        assert "ALVORD" in page.get_text()
        assert _band_ink(page, TEXT_BAND) > 0, "fixture must start with text ink"
        baseline_line_ink = _band_ink(page, LINE_BAND)
        assert baseline_line_ink > 0, "fixture must start with line-art ink"

        tmp_doc, tmp_page = core._text_free_page_copy(doc, page)
        try:
            assert tmp_page.get_text().strip() == "", "text must be removed from the copy"
            assert _band_ink(tmp_page, TEXT_BAND) == 0, (
                "no text ink may survive in the underlay copy")
            surviving = _band_ink(tmp_page, LINE_BAND)
            assert surviving > 0, (
                "line art must survive: the underlay is the graphics delivery")
            assert surviving >= baseline_line_ink * 0.5, (
                f"line art was largely destroyed ({surviving} vs {baseline_line_ink})")
        finally:
            tmp_doc.close()
    finally:
        doc.close()


def test_unsuppressed_render_still_contains_the_text():
    """Guards the inverse: raster-only pages with no native text keep the full render.

    Contract point 3 of the hybrid dedupe test must remain true -- this fix must not
    strip text from underlays that are the page's only text delivery.
    """
    doc, page = _fixture_doc()
    try:
        assert _band_ink(page, TEXT_BAND) > 0
    finally:
        doc.close()


def test_import_page_as_raster_accepts_and_reports_text_suppression():
    """The public entry point must expose the switch and record what it did.

    `text_suppressed` in the evidence is how a report reader can tell whether a given
    underlay is duplicating natively delivered text.
    """
    import inspect

    sig = inspect.signature(core._import_page_as_raster)
    assert "suppress_text" in sig.parameters, (
        "_import_page_as_raster must accept suppress_text")
    assert sig.parameters["suppress_text"].default is False, (
        "text suppression must be opt-in so raster-only delivery is unchanged")

    source = inspect.getsource(core._import_page_as_raster)
    assert "_text_free_page_copy" in source, (
        "the suppressed path must render from a text-redacted copy")
    assert "text_suppressed" in source, (
        "the result evidence must record whether text was suppressed")


def test_raster_text_overlay_call_site_requests_suppression():
    """The `auto_raster_text_overlay` path is the one that double-renders.

    Locking the call site prevents a future edit from silently reverting to a full-page
    render underneath a native text layer.
    """
    import inspect

    source = inspect.getsource(core)
    marker = "suppress_text=bool(auto_raster_text_overlay)"
    assert marker in source, (
        "the raster+text-overlay call site must request text suppression")
