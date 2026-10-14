"""A name-keyed source CFF in a Type0 PDF stays exact through host staging."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sys

from fontTools.cffLib import CFFFontSet
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont
import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "PDFVectorImporter", ROOT / "PDFVectorImporter" / "src"):
    sys.path.insert(0, str(path))
import PDFEmbeddedFonts as staging  # noqa: E402
import PDFImporterCore as core  # noqa: E402
from PDFVectorImporter.pdfcadcore.primitive_extractor import extract_page  # noqa: E402


def test_public_cid_label_stages_original_program_glyphs_without_family_lookup(tmp_path):
    with fitz.open() as document:
        page = document.new_page(width=240, height=160)
        original = fitz.Font("helv").buffer
        page.insert_font(fontname="SyntheticControlFont", fontbuffer=original)
        page.insert_text((15, 130), "BLENDER CONTROL", fontname="SyntheticControlFont", fontsize=10)
        record = page.get_fonts(full=True)[0]
        extracted = document.extract_font(record[0])[3]
        digest = sha256(extracted).hexdigest()
        item = extract_page(page, 1).text_items[0]
        assert item.font_asset.source_bytes == extracted
        assert item.font_asset.source_sha256 == digest
        assert item.font_asset.source_format == "cff"
        assert item.font_asset.source_inventory_format == "cid"
        assert item.font_asset.source_inventory_type == "Type0"
        items = list(core._iter_text_source_items(core._page_text_dict(page), 1, "a" * 64, "3d_text"))
        assert len(items) == 1
        staged = staging.stage_bound_page_fonts(page, items, tmp_path)[items[0]["source_item_id"]]
        path = staging.verify_bound_item_font(items[0], staged)
        assert staged["legacy_allowed"] is False
        assert staged["source_sha256"] == digest
        assert len(staged["original_characters"]) == 15
        assert sha256(Path(path).read_bytes()).hexdigest() == staged["sha256"]
        cff = CFFFontSet()
        cff.decompile(BytesIO(extracted), None, isCFF2=False)
        top = cff.topDictIndex[0]
        with TTFont(path, lazy=False) as font:
            assert font.getGlyphOrder() == list(top.charset)
            for char in item.source_char_layout:
                assert char.source_font_binding_verified
                assert char.source_font_program_sha256 == digest
                glyph = font.getBestCmap()[ord(char.text)]
                assert font.getGlyphID(glyph) == char.glyph_id
                original_pen = RecordingPen()
                top.CharStrings[glyph].draw(original_pen)
                staged_pen = RecordingPen()
                font.getGlyphSet()[glyph].draw(staged_pen)
                assert staged_pen.value == original_pen.value
