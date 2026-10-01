"""Native font staging must bind the original item, never a family-name winner."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import sys

import pymupdf as fitz
import pytest
from fontTools.ttLib import TTFont

from test_source_font_program_binding import _build_deterministic_test_font, subset_pdf

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "PDFVectorImporter", ROOT / "PDFVectorImporter" / "src"):
    sys.path.insert(0, str(path))
import PDFEmbeddedFonts as fonts  # noqa: E402
import PDFImporterCore as core  # noqa: E402


@pytest.fixture
def deterministic_exact_font(tmp_path):
    path = tmp_path / "deterministic.ttf"
    _build_deterministic_test_font(path)
    return path


def items_for(page):
    return list(core._iter_text_source_items(core._page_text_dict(page), 1, "a" * 64, "3d_text"))


def test_same_family_subsets_stage_distinct_actual_source_programs(tmp_path, deterministic_exact_font):
    with subset_pdf(deterministic_exact_font) as pdf:
        items = items_for(pdf[0])
        records = fonts.stage_bound_page_fonts(pdf[0], items, tmp_path / "cache")
        paths = [fonts.verify_bound_item_font(item, records[item["source_item_id"]]) for item in items]
        assert len(set(paths)) == 2
        assert len({item["span"]["font"] for item in items}) == 1
        for item, path in zip(items, paths, strict=True):
            record = records[item["source_item_id"]]
            assert record["source_binding_method"] == "original_textpage_character_program_sha256"
            assert len(record["original_characters"]) == len(item["text"])
            assert sha256(Path(path).read_bytes()).hexdigest() == record["sha256"]
            font = TTFont(path)
            name = font.getBestCmap()[ord(item["text"][0])]
            assert font["glyf"][name].numberOfContours > 0
            font.close()
        with pytest.raises(ValueError, match="another source item"):
            fonts.verify_bound_item_font(items[0], records[items[1]["source_item_id"]])


@pytest.mark.parametrize("field,value", [("pdf_sha256", "b" * 64), ("page_number", 2),
                                        ("text", "changed"), ("origin", (99, 99))])
def test_bound_font_rejects_different_source_identity(tmp_path, deterministic_exact_font, field, value):
    with subset_pdf(deterministic_exact_font) as pdf:
        items = items_for(pdf[0])
        record = fonts.stage_bound_page_fonts(pdf[0], items, tmp_path)[items[0]["source_item_id"]]
        item = {**items[0], field: value}
        with pytest.raises(ValueError, match="another source item"):
            fonts.verify_bound_item_font(item, record)


def test_changed_staged_bytes_and_changed_occurrences_are_not_replaced_by_family_lookup(tmp_path, deterministic_exact_font):
    with subset_pdf(deterministic_exact_font) as pdf:
        items = items_for(pdf[0])
        records = fonts.stage_bound_page_fonts(pdf[0], items, tmp_path)
        record = records[items[0]["source_item_id"]]
        Path(record["path"]).write_bytes(b"changed after staging")
        with pytest.raises(ValueError, match="staging changed"):
            fonts.verify_bound_item_font(items[0], record)
        changed = deepcopy(items[0])
        changed["origin"] = (100, 100)
        rejected = fonts.stage_bound_page_fonts(pdf[0], [changed], tmp_path)[changed["source_item_id"]]
        assert rejected["legacy_allowed"] is False
        with pytest.raises(ValueError, match="original character occurrence"):
            fonts.verify_bound_item_font(changed, rejected)


def test_annotation_base14_font_is_staged_from_actual_appearance(tmp_path):
    with fitz.open() as pdf:
        page = pdf.new_page(width=240, height=160)
        page.insert_text((20, 30), "Page", fontname="cour")
        annotation = page.add_freetext_annot(fitz.Rect(20, 50, 180, 100), "NOTE", fontsize=16, fontname="helv")
        annotation.update()
        original = pdf.tobytes()
    with fitz.open(stream=original, filetype="pdf") as pdf:
        items = items_for(pdf[0])
        item = next(i for i in items if i["text"] == "NOTE")
        assert not any(row[3] == "Helvetica" for row in pdf[0].get_fonts(full=True))
        record = fonts.stage_bound_page_fonts(pdf[0], items, tmp_path)[item["source_item_id"]]
        assert record["source_origin"] == "pdf_base14_renderer_font"
        assert fonts.verify_bound_item_font(item, record)
        assert record["source_program_candidates"]


def test_genuine_nonembedded_absence_keeps_existing_exact_system_font_route(tmp_path):
    with fitz.open() as pdf:
        page = pdf.new_page(width=160, height=120)
        page.insert_text((20, 40), "Example", fontname="helv")
        pdf.xref_set_key(page.get_fonts()[0][0], "BaseFont", "/UnembeddedFixtureSans")
        original = pdf.tobytes()
    with fitz.open(stream=original, filetype="pdf") as pdf:
        items = items_for(pdf[0])
        record = fonts.stage_bound_page_fonts(pdf[0], items, tmp_path)[items[0]["source_item_id"]]
        assert record["legacy_allowed"] is True
        assert "path" not in record
        assert fonts.verify_bound_item_font(items[0], record) is None


def test_real_item_orchestrator_forwards_each_complementary_program(
        tmp_path, deterministic_exact_font, monkeypatch):
    from test_text_item_degrade_fc import _verified

    forwarded = []
    monkeypatch.setattr(core, "_stage_page_shapestring_fonts", lambda *a, **k: None)
    monkeypatch.setattr(core, "_shapestring_font_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(core, "_to_fc", lambda p, *a: SimpleNamespace(x=p[0], y=p[1]))

    def deliver(item, attempted, state, **arguments):
        record = arguments["source_font_binding"]
        path = fonts.verify_bound_item_font(item, record)
        assert arguments["raw_source_dict"] is not None
        assert len(arguments["source_page_quad"]) == 4
        forwarded.append((item["source_item_id"], path))
        return _verified(item, attempted, "Native_" + item["source_item_id"])

    monkeypatch.setattr(core, "_deliver_text_item_3d", deliver)
    with subset_pdf(deterministic_exact_font) as pdf:
        result = core._render_canonical_text_items(
            pdf_doc=pdf, page=pdf[0], pdf_path="synthetic.pdf", page_num=1,
            page_h=160.0, page_w=240.0, scale=1.0,
            fc_doc=SimpleNamespace(Objects=[]), parent_group=SimpleNamespace(),
            opts=core.ImportOptions(text_mode="3d_text"), pdf_sha256="a" * 64)
    assert result["count"] == 2
    assert len(forwarded) == 2
    assert len({path for _item, path in forwarded}) == 2


def test_real_3d_deliverer_passes_bound_program_to_native_builder(
        tmp_path, deterministic_exact_font, monkeypatch):
    class ReachedNativeBuilder(BaseException):
        pass

    def reject_legacy_lookup(*args, **kwargs):
        pytest.fail("bound item must not return to a same-family font lookup")

    monkeypatch.setattr(core, "_resolve_shapestring_font_path_with_evidence", reject_legacy_lookup)
    monkeypatch.setattr(core, "Draft", SimpleNamespace())
    monkeypatch.setattr(core, "Vector", lambda *values: values)
    monkeypatch.setattr(core, "Rotation", lambda *values: values)
    monkeypatch.setattr(core, "Placement", lambda *values: values)
    doc = SimpleNamespace(Objects=[])
    group = SimpleNamespace(Document=doc)
    with subset_pdf(deterministic_exact_font) as pdf:
        items = items_for(pdf[0])
        records = fonts.stage_bound_page_fonts(pdf[0], items, tmp_path)
        called = []

        def native_builder(_doc, **arguments):
            called.append((arguments["source_text"], arguments["font_path"]))
            raise ReachedNativeBuilder

        monkeypatch.setattr(core, "_create_verified_compound_text3d_entity", native_builder)
        for item in items:
            with pytest.raises(ReachedNativeBuilder):
                core._deliver_text_item_3d(
                    item, "3d_text", core.ImportOptions(text_mode="3d_text"),
                    text_group=group, page_h=160.0, scale=1.0,
                    source_font_binding=records[item["source_item_id"]])
        assert called == [(item["text"], records[item["source_item_id"]]["path"])
                          for item in items]
        assert len({path for _text, path in called}) == 2
