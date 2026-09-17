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
