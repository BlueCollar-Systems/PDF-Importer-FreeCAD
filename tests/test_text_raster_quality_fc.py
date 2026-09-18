from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "PDFVectorImporter" / "src"), str(ROOT / "PDFVectorImporter")]
import PDFImporterCore as core  # noqa: E402
import PDFStyleRestore as restore  # noqa: E402


def test_small_page_keeps_one_full_resolution_cached_render(monkeypatch):
    fitz = pytest.importorskip("fitz")
    monkeypatch.setenv("BC_FC_TEXT_RASTER_CACHE_MAX_PIXELS", "16000000")
    pdf = fitz.open()
    try:
        page = pdf.new_page(width=200, height=100)
        page.insert_text((20, 50), "SMALL PAGE", fontsize=12)
        opts = core.ImportOptions(raster_dpi=300)
        first, dpi = core._cached_text_raster_pixmap(
            page, fitz.Rect(15, 35, 80, 55), requested_dpi=300, page_number=1, opts=opts)
        second, second_dpi = core._cached_text_raster_pixmap(
            page, fitz.Rect(90, 35, 150, 55), requested_dpi=300, page_number=1, opts=opts)
        assert dpi == second_dpi == 300
        assert first.width > 250 and second.width > 240
        assert opts._text_raster_page_cache["pixmap"] is not None
        assert opts._text_raster_page_cache["render_count"] == 1
    finally:
        pdf.close()


def test_headless_text_raster_restores_unshaded_source_colors():
    view = SimpleNamespace(DisplayMode="Shading", Visibility=True)
    obj = SimpleNamespace(TypeId="Image::ImagePlane", PDFRepresentation="raster",
                          PropertiesList=["PDFRepresentation"], ViewObject=view)
    result = restore.restore_object_style(obj)
    assert result["error"] is None
    assert view.DisplayMode == "No shading"


@pytest.mark.parametrize("source_id", ["p1:img1", "p1:page", "p1:b0:l0:s0"])
def test_every_new_raster_host_preserves_source_pixel_colors(source_id):
    view = SimpleNamespace(DisplayMode="Shading")
    obj = SimpleNamespace(TypeId="Image::ImagePlane", PropertiesList=[], ViewObject=view)
    core._annotate_text_host_object(obj, source_id, "raster")
    assert obj.PDFSourceItemId == source_id
    assert view.DisplayMode == "No shading"


def test_headless_image_metadata_remains_available_without_a_view():
    obj = SimpleNamespace(TypeId="Image::ImagePlane", PropertiesList=[], ViewObject=None)
    core._annotate_text_host_object(obj, "p1:img1", "raster")
    assert obj.PDFRepresentation == "raster"


@pytest.mark.parametrize("budget", [10000, 16000000])
def test_translucent_source_highlight_is_composited_once(monkeypatch, budget):
    fitz = pytest.importorskip("fitz")
    monkeypatch.setenv("BC_FC_TEXT_RASTER_CACHE_MAX_PIXELS", str(budget))
    pdf = fitz.open()
    try:
        page = pdf.new_page(width=200, height=100)
        page.draw_rect(fitz.Rect(10, 10, 100, 80), color=None,
                       fill=(0, 1, 1), fill_opacity=0.25)
        pix, dpi = core._cached_text_raster_pixmap(
            page, fitz.Rect(20, 20, 40, 40), requested_dpi=300,
            page_number=1, opts=core.ImportOptions())
        assert dpi == 300
        assert pix.alpha == 0
        red, green, blue = pix.pixel(pix.width // 2, pix.height // 2)
        assert 188 <= red <= 194
        assert green == blue == 255
    finally:
        pdf.close()


def test_original_glyph_quad_expands_short_font_bbox_without_changing_identity(monkeypatch):
    from pdfcadcore import primitive_extractor
    fitz = pytest.importorskip("fitz")
    with fitz.open() as doc:
        page = doc.new_page(width=300, height=240)
        page.insert_text((100, 150), "H", fontsize=12)
        raw = primitive_extractor._raw_text_with_source_quads(page)
        span = raw["blocks"][0]["lines"][0]["spans"][0]
        original = tuple(span["bbox"])
        shortened = (original[0], 145., original[2], original[3])
        span["bbox"] = shortened
        item = next(core._iter_text_source_items(page.get_text("dict"), 1, "a"*64, "raster"))
        item["bbox"] = shortened
        item["span"]["bbox"] = shortened
        monkeypatch.setattr(primitive_extractor, "_raw_text_with_source_quads", lambda page: raw)
        coverage = core._raster_source_coverage_bbox(item, page, core.ImportOptions())
        assert coverage[1] < 145.
        assert item["bbox"] == shortened
        pix, _ = core._cached_text_raster_pixmap(page, fitz.Rect(coverage),
            requested_dpi=300, page_number=1, opts=core.ImportOptions())
        rows_above = int(145*300/72) - pix.y
        assert any(v < 128 for v in pix.samples[:rows_above*pix.stride])
        item["text"] = "WRONG"
        with pytest.raises(ValueError, match="not bound"):
            core._raster_source_coverage_bbox(item, page, core.ImportOptions())
