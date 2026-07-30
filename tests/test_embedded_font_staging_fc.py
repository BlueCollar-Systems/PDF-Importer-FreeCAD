from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402
from PDFVectorImporter.pdfcadcore import embedded_fonts  # noqa: E402


PDF_SHA_A = "a" * 64
PDF_SHA_B = "b" * 64


def test_cmap_repair_adds_host_safe_names_to_anonymous_subset_font():
    """Missing name IDs must not reach native host font loaders."""
    builder = FontBuilder(1000, isTTF=True)
    builder.setupGlyphOrder([".notdef", "A"])
    pen = TTGlyphPen(None)
    empty_glyph = pen.glyph()
    builder.setupGlyf({".notdef": empty_glyph, "A": empty_glyph})
    builder.setupHorizontalMetrics({".notdef": (500, 0), "A": (600, 0)})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupCharacterMap({65: "A"})
    builder.setupOS2()
    builder.setupNameTable(
        {
            "familyName": "Disposable fixture",
            "styleName": "Regular",
            "uniqueFontIdentifier": "Disposable fixture Regular",
            "fullName": "Disposable fixture Regular",
            "psName": "DisposableFixture-Regular",
        }
    )
    builder.setupPost()
    builder.setupMaxp()
    del builder.font["cmap"]
    del builder.font["name"]
    source = io.BytesIO()
    builder.font.save(source, reorderTables=False)

    usable_format, usable_bytes, cmap_installed = embedded_fonts._usable_font(
        source.getvalue(),
        "ttf",
        "OCR Exact / Anonymous",
        {65: 1},
    )

    assert usable_format == "ttf"
    assert cmap_installed is True
    font = TTFont(io.BytesIO(usable_bytes), lazy=False)
    try:
        assert font.getBestCmap() == {65: "A"}
        names = {
            int(record.nameID): record.toUnicode()
            for record in font["name"].names
            if int(record.nameID) in {1, 2, 3, 4, 5, 6}
        }
        assert names[1] == "OCR Exact / Anonymous"
        assert names[2] == "Regular"
        assert names[4] == "OCR Exact / Anonymous"
        assert names[6] == "OCR-Exact-Anonymous"
    finally:
        font.close()


def test_shapestring_font_cache_uses_temp_when_user_mod_root_is_unwritable(
    tmp_path, monkeypatch,
):
    blocked_root = tmp_path / "blocked-user-root"
    blocked_root.write_text("not a directory", encoding="utf-8")
    fallback_root = tmp_path / "fallback-temp"
    fallback_root.mkdir()

    class FakeFreeCAD:
        @staticmethod
        def getUserAppDataDir():
            return str(blocked_root)

    monkeypatch.setattr(core, "FreeCAD", FakeFreeCAD)
    monkeypatch.setattr(core.tempfile, "gettempdir", lambda: str(fallback_root))

    cache_dir = core._shapestring_font_cache_dir()

    assert cache_dir == fallback_root / "bc_fc_pdf_font_cache"
    assert cache_dir.is_dir()


def test_raster_asset_dir_uses_temp_when_user_mod_root_is_unwritable(
    tmp_path, monkeypatch,
):
    blocked_root = tmp_path / "blocked-raster-user-root"
    blocked_root.write_text("not a directory", encoding="utf-8")
    fallback_root = tmp_path / "raster-fallback-temp"
    fallback_root.mkdir()

    class FakeFreeCAD:
        @staticmethod
        def getUserAppDataDir():
            return str(blocked_root)

    monkeypatch.setattr(core, "FreeCAD", FakeFreeCAD)
    monkeypatch.setattr(core.tempfile, "gettempdir", lambda: str(fallback_root))

    cache_dir = core._raster_asset_dir()

    assert cache_dir == fallback_root / "bc_fc_pdf_raster_cache"
    assert cache_dir.is_dir()


def _set_completed_font_session(
    opts,
    *,
    pdf_sha256=PDF_SHA_A,
    page_number=1,
    records=None,
    failures=None,
    staging_complete=True,
):
    session = {
        "pdf_sha256": pdf_sha256,
        "page_number": page_number,
        "staging_complete": staging_complete,
        "records": {} if records is None else records,
        "failures": [] if failures is None else failures,
    }
    opts._shapestring_font_staging_sessions = [session]
    return session


def _proof_item(font="Siwa-Regular", *, pdf_sha256=PDF_SHA_A, page_number=1):
    from PDFEmbeddedFonts import normalize_font_key

    span = {
        "text": "TEXT",
        "font": font,
        "size": 10.0,
        "origin": (10.0, 50.0),
        "bbox": (10.0, 40.0, 40.0, 52.0),
    }
    return {
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": pdf_sha256,
        "page_number": page_number,
        "source_item_id": f"p{page_number}:b0:l0:s0",
        "requested_type": "3d_text",
        "text": "TEXT",
        "font_identity": {
            "raw_name": font,
            "normalized_key": normalize_font_key(font),
        },
        "bbox": span["bbox"],
        "origin": span["origin"],
        "line_direction": (1.0, 0.0),
        "rotation_deg": 0.0,
        "span": span,
        "block_index": 0,
        "line_index": 0,
        "span_index": 0,
    }


def _raw_cff_fixture() -> bytes:
    builder = FontBuilder(1000, isTTF=False)
    glyph_order = [".notdef", "A", "fi"]
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap({65: "A", 0xFB01: "fi"})
    builder.setupHorizontalMetrics(
        {".notdef": (600, 0), "A": (600, 0), "fi": (600, 0)}
    )
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({
        "familyName": "Test CFF",
        "styleName": "Regular",
        "uniqueFontIdentifier": "TestCFF-Regular",
        "fullName": "Test CFF Regular",
        "psName": "TestCFF-Regular",
    })
    builder.setupOS2(
        sTypoAscender=800,
        sTypoDescender=-200,
        usWinAscent=800,
        usWinDescent=200,
    )
    builder.setupPost()

    char_strings = {}
    pen = T2CharStringPen(600, None)
    char_strings[".notdef"] = pen.getCharString()
    pen = T2CharStringPen(600, None)
    pen.moveTo((50, 0))
    pen.lineTo((300, 700))
    pen.lineTo((550, 0))
    pen.closePath()
    char_strings["A"] = pen.getCharString()
    pen = T2CharStringPen(600, None)
    pen.moveTo((100, 0))
    pen.lineTo((100, 700))
    pen.lineTo((500, 700))
    pen.lineTo((500, 0))
    pen.closePath()
    char_strings["fi"] = pen.getCharString()
    builder.setupCFF(
        "TestCFF-Regular",
        {"FullName": "Test CFF Regular", "FamilyName": "Test CFF", "Weight": "Regular"},
        char_strings,
        {},
    )

    otf = io.BytesIO()
    builder.font.save(otf)
    font = TTFont(io.BytesIO(otf.getvalue()))
    return font["CFF "].compile(font)


def _truetype_without_cmap_fixture() -> bytes:
    builder = FontBuilder(1000, isTTF=True)
    glyph_order = [".notdef", "glyph00001", "glyph00002"]
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap({0x31: "glyph00001", 0x03A6: "glyph00002"})
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({
        "familyName": "Subset Arial",
        "styleName": "Regular",
        "uniqueFontIdentifier": "SubsetArial-Regular",
        "fullName": "Subset Arial Regular",
        "psName": "SubsetArial-Regular",
    })
    builder.setupOS2(
        sTypoAscender=800,
        sTypoDescender=-200,
        usWinAscent=800,
        usWinDescent=200,
    )
    builder.setupPost()
    glyphs = {}
    for index, name in enumerate(glyph_order):
        pen = TTGlyphPen(None)
        if index:
            pen.moveTo((100, 0))
            pen.lineTo((100, 700))
            pen.lineTo((500, 700))
            pen.lineTo((500, 0))
            pen.closePath()
        glyphs[name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupMaxp()
    del builder.font["cmap"]
    output = io.BytesIO()
    builder.font.save(output)
    return output.getvalue()


def test_raw_cff_is_converted_to_valid_otf_with_unicode_cmap():
    import PDFEmbeddedFonts as embedded

    raw = _raw_cff_fixture()
    converted = embedded.convert_cff_to_otf(raw, "Test CFF")
    repeated = embedded.convert_cff_to_otf(raw, "Test CFF")

    font = TTFont(io.BytesIO(converted))
    assert converted == repeated
    assert "CFF " in font
    assert font.getBestCmap()[65] == "A"
    assert font.getBestCmap()[0xFB01] == "fi"
    assert font["head"].created == 0x7C259DC0
    assert font["head"].modified == 0x7C259DC0


def test_page_embedded_cff_is_staged_content_addressed_and_resolved_exactly(tmp_path):
    import PDFEmbeddedFonts as embedded

    raw = _raw_cff_fixture()

    class FakePage:
        @staticmethod
        def get_fonts(full=True):
            assert full is True
            return [(5, "cff", "Type1", "ABCDEF+TestCFF-Regular", "T1_0", "WinAnsiEncoding", 0)]

    class FakePdf:
        @staticmethod
        def extract_font(xref):
            assert xref == 5
            return ("ABCDEF+TestCFF-Regular", "cff", "Type1", raw)

    staged = embedded.stage_page_fonts(FakePdf(), FakePage(), tmp_path)
    opts = core.ImportOptions()
    opts._shapestring_font_paths = staged

    resolved = core._resolve_shapestring_font_path("ABCDEF+TestCFF-Regular", opts)

    assert resolved
    path = Path(resolved)
    assert path.is_file()
    assert path.parent == tmp_path
    assert path.suffix == ".otf"
    assert TTFont(path).getBestCmap()[65] == "A"
    record = next(iter(staged.values()))
    assert record["source"] == "pdf_embedded"
    assert record["xref"] == 5
    assert record["sha256"] in path.name


def test_type0_identity_truetype_without_cmap_is_repaired_from_pdf_tounicode(tmp_path):
    import PDFEmbeddedFonts as embedded

    raw = _truetype_without_cmap_fixture()
    to_unicode = b"""\
/CIDInit /ProcSet findresource begin
begincmap
2 beginbfchar
<0001> <0031>
<0002> <03A6>
endbfchar
endcmap
"""

    class FakePage:
        @staticmethod
        def get_fonts(full=True):
            assert full is True
            return [(7, "ttf", "Type0", "ABCDEF+ArialMT", "C2_0", "Identity-H", 0)]

    class FakePdf:
        @staticmethod
        def extract_font(xref):
            assert xref == 7
            return ("ABCDEF+ArialMT", "ttf", "Type0", raw)

        @staticmethod
        def xref_get_key(xref, key):
            values = {
                (7, "ToUnicode"): ("xref", "61 0 R"),
                (7, "DescendantFonts"): ("array", "[60 0 R]"),
                (69, "CIDToGIDMap"): ("name", "/Identity"),
            }
            return values.get((xref, key), ("null", "null"))

        @staticmethod
        def xref_stream(xref):
            assert xref == 61
            return to_unicode

        @staticmethod
        def xref_object(xref, compressed=False):
            assert xref == 60
            assert compressed is False
            return "[ 69 0 R ]"

    staged = embedded.stage_page_fonts(FakePdf(), FakePage(), tmp_path)
    record = staged["arialmt"]
    repaired = TTFont(record["path"])

    assert repaired.getBestCmap()[0x31] == "glyph00001"
    assert repaired.getBestCmap()[0x03A6] == "glyph00002"
    assert record["cmap_source"] == "pdf_tounicode_identity_h"
    assert record["cmap_entries"] == 2


def test_custom_font_never_self_certifies_via_arial_substitution(monkeypatch):
    monkeypatch.delenv("BC_PDF_SHAPESTRING_FONT", raising=False)
    opts = core.ImportOptions()
    opts._shapestring_font_paths = {}

    assert core._resolve_shapestring_font_path("Siwa-Regular", opts) is None


def test_core_stages_page_fonts_before_native_3d_text(monkeypatch, tmp_path):
    import PDFEmbeddedFonts as embedded

    expected = {
        "siwaregular": {
            "path": str(tmp_path / "font.otf"),
            "sha256": "a" * 64,
            "source": "pdf_embedded",
            "xref": 5,
        }
    }
    def fake_stage(*_args, failures):
        failures.append({
            "xref": 9,
            "font": "Broken",
            "outcome": "failed",
            "reason": "embedded_font_staging_failed",
            "exception": "ValueError: broken",
        })
        return expected

    monkeypatch.setattr(embedded, "stage_page_fonts", fake_stage)
    monkeypatch.setattr(core, "_shapestring_font_cache_dir", lambda: tmp_path)
    opts = core.ImportOptions(text_mode="3d_text")

    staged = core._stage_page_shapestring_fonts(
        object(),
        object(),
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=4,
    )

    assert staged == expected
    assert opts._shapestring_font_paths == expected
    assert opts._font_stage_failures == [{
        "xref": 9,
        "font": "Broken",
        "outcome": "failed",
        "reason": "embedded_font_staging_failed",
        "exception": "ValueError: broken",
    }]
    assert opts._shapestring_font_staging_sessions == [{
        "pdf_sha256": PDF_SHA_A,
        "page_number": 4,
        "staging_complete": True,
        "records": expected,
        "failures": opts._font_stage_failures,
    }]


def test_one_malformed_embedded_font_does_not_block_other_exact_fonts(tmp_path):
    import PDFEmbeddedFonts as embedded

    good = _raw_cff_fixture()

    class FakePage:
        @staticmethod
        def get_fonts(full=True):
            assert full is True
            return [
                (5, "cff", "Type1", "ABCDEF+Broken-Regular", "T1_0", "", 0),
                (6, "cff", "Type1", "ABCDEF+TestCFF-Regular", "T1_1", "", 0),
            ]

    class FakePdf:
        @staticmethod
        def extract_font(xref):
            if xref == 5:
                return ("ABCDEF+Broken-Regular", "cff", "Type1", b"not-a-font")
            assert xref == 6
            return ("ABCDEF+TestCFF-Regular", "cff", "Type1", good)

    failures = []
    staged = embedded.stage_page_fonts(
        FakePdf(), FakePage(), tmp_path, failures=failures
    )

    assert "testcffregular" in staged
    assert "brokenregular" not in staged
    assert len(failures) == 1
    assert failures[0]["xref"] == 5
    assert failures[0]["font"] == "ABCDEF+Broken-Regular"
    assert failures[0]["outcome"] == "failed"
    assert failures[0]["reason"] == "embedded_font_staging_failed"
    assert failures[0]["exception"]


def test_exact_font_resolver_reports_deterministic_embedded_and_system_absence(
    monkeypatch, tmp_path
):
    import PDFEmbeddedFonts as embedded

    monkeypatch.setenv("WINDIR", str(tmp_path))
    opts = core.ImportOptions()
    def stage_nonembedded_helvetica(*_args, failures):
        failures.append({
            "xref": 0,
            "font": "Helvetica",
            "outcome": "not_embedded",
            "reason": "embedded_font_not_present",
            "exception": "",
        })
        return {}

    monkeypatch.setattr(
        embedded,
        "stage_page_fonts",
        stage_nonembedded_helvetica,
    )
    core._stage_page_shapestring_fonts(
        object(),
        object(),
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Helvetica",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    identity = {"raw_name": "Helvetica", "normalized_key": "helvetica"}
    assert path is None
    assert results == [
        {
            "source": "embedded_font",
            "outcome": "not_found",
            "font_identity": identity,
            "inventory_observation": {
                "xref": 0,
                "font": "Helvetica",
                "outcome": "not_embedded",
                "reason": "embedded_font_not_present",
                "exception": "",
            },
            "pdf_sha256": PDF_SHA_A,
            "page_number": 1,
            "staging_complete": True,
        },
        {
            "source": "system_font",
            "outcome": "not_found",
            "font_identity": identity,
            "pdf_sha256": PDF_SHA_A,
            "page_number": 1,
            "staging_complete": True,
        },
    ]
    assert core._resolve_shapestring_font_path("Helvetica", opts) is None


def test_completed_inventory_without_exact_observation_is_invalid_not_absence():
    opts = core.ImportOptions()
    _set_completed_font_session(opts)

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert path is None
    assert len(results) == 1
    assert results[0]["source"] == "embedded_font"
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "font_not_observed_in_completed_inventory"
    assert results[0]["staging_complete"] is True


@pytest.mark.parametrize(
    ("rows", "extracted", "expected_reason"),
    [
        ([()], None, "embedded_font_inventory_row_invalid"),
        (
            [("not-an-xref", "", "Type1", "Siwa-Regular")],
            None,
            "embedded_font_inventory_xref_invalid",
        ),
        (
            [("0", "", "Type1", "Siwa-Regular")],
            None,
            "embedded_font_inventory_xref_invalid",
        ),
        (
            [(-3, "", "Type1", "Siwa-Regular")],
            None,
            "embedded_font_inventory_xref_invalid",
        ),
        (
            [(5, "ttf", "TrueType", "Siwa-Regular")],
            ("Siwa-Regular", "ttf", "TrueType", b""),
            "embedded_font_payload_empty",
        ),
        (
            [(5, "woff", "TrueType", "Siwa-Regular")],
            ("Siwa-Regular", "woff", "TrueType", b"font-payload"),
            "embedded_font_format_unsupported",
        ),
    ],
)
def test_normal_return_inventory_gaps_are_recorded_and_terminal_for_exact_span(
    monkeypatch,
    tmp_path,
    rows,
    extracted,
    expected_reason,
):
    class FakePage:
        @staticmethod
        def get_fonts(full=True):
            assert full is True
            return rows

    class FakePdf:
        @staticmethod
        def extract_font(xref):
            assert extracted is not None
            assert xref == 5
            return extracted

    monkeypatch.setattr(core, "_shapestring_font_cache_dir", lambda: tmp_path)
    opts = core.ImportOptions(text_mode="3d_text")
    staged = core._stage_page_shapestring_fonts(
        FakePdf(),
        FakePage(),
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert staged == {}
    assert opts._shapestring_font_staging_sessions[0]["staging_complete"] is True
    failures = opts._shapestring_font_staging_sessions[0]["failures"]
    assert failures
    assert failures[0]["outcome"] == "failed"
    assert failures[0]["reason"] == expected_reason

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert path is None
    assert len(results) == 1
    assert results[0]["source"] == "embedded_font"
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == expected_reason


def test_zero_xref_inventory_row_is_explicit_exact_nonembedded_observation(
    monkeypatch, tmp_path
):
    class FakePage:
        @staticmethod
        def get_fonts(full=True):
            assert full is True
            return [(0, "", "Type1", "Helvetica", "F1", "WinAnsiEncoding", 0)]

    class FakePdf:
        @staticmethod
        def extract_font(_xref):
            raise AssertionError("xref zero must never be extracted as an embedded font")

    monkeypatch.setattr(core, "_shapestring_font_cache_dir", lambda: tmp_path)
    monkeypatch.setenv("WINDIR", str(tmp_path))
    opts = core.ImportOptions(text_mode="3d_text")
    staged = core._stage_page_shapestring_fonts(
        FakePdf(),
        FakePage(),
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=2,
    )

    assert staged == {}
    observations = opts._shapestring_font_staging_sessions[0]["failures"]
    assert observations[0] == {
        "xref": 0,
        "font": "Helvetica",
        "outcome": "not_embedded",
        "reason": "embedded_font_not_present",
        "exception": "",
    }

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Helvetica",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=2,
    )

    assert path is None
    assert [result["source"] for result in results] == [
        "embedded_font",
        "system_font",
    ]
    assert [result["outcome"] for result in results] == ["not_found", "not_found"]
    assert results[0]["inventory_observation"] == observations[0]


def test_staging_never_ran_is_invalid_and_terminal_not_proven_absence(tmp_path):
    item = _proof_item()
    opts = core.ImportOptions(text_mode="3d_text")

    path, results = core._resolve_shapestring_font_path_with_evidence(
        item["font_identity"]["raw_name"],
        opts,
        pdf_sha256=item["pdf_sha256"],
        page_number=item["page_number"],
    )

    assert path is None
    assert len(results) == 1
    assert results[0]["source"] == "embedded_font"
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "font_staging_session_missing"
    assert results[0]["pdf_sha256"] == item["pdf_sha256"]
    assert results[0]["page_number"] == item["page_number"]
    assert results[0]["staging_complete"] is False
    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            opts,
            text_group=None,
            page_h=100.0,
            scale=1.0,
        )
    assert raised.value.attempt["reason"] == "exact_font_resolution_invalid"


def test_explicit_none_staged_record_is_invalid_not_absence():
    opts = core.ImportOptions()
    _set_completed_font_session(
        opts,
        records={"siwaregular": None},
    )

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert path is None
    assert len(results) == 1
    assert results[0]["source"] == "embedded_font"
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "malformed_staged_font_record"


def test_incomplete_staging_session_is_invalid():
    opts = core.ImportOptions()
    _set_completed_font_session(opts, staging_complete=False)

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert path is None
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "font_staging_session_incomplete"
    assert results[0]["staging_complete"] is False


def test_failed_staging_call_records_only_a_bound_incomplete_session(monkeypatch):
    import PDFEmbeddedFonts as embedded

    def fail_stage(*_args, **_kwargs):
        raise RuntimeError("synthetic staging crash")

    monkeypatch.setattr(embedded, "stage_page_fonts", fail_stage)
    opts = core.ImportOptions()

    with pytest.raises(RuntimeError, match="staging crash"):
        core._stage_page_shapestring_fonts(
            object(),
            object(),
            opts,
            pdf_sha256=PDF_SHA_A,
            page_number=1,
        )

    sessions = getattr(opts, "_shapestring_font_staging_sessions", [])
    assert len(sessions) == 1
    assert sessions[0]["pdf_sha256"] == PDF_SHA_A
    assert sessions[0]["page_number"] == 1
    assert sessions[0]["staging_complete"] is False
    assert sessions[0]["records"] == {}
    assert sessions[0]["failures"] == []
    assert sessions[0]["page_failure"]["reason"] == "embedded_font_page_staging_failed"
    assert "RuntimeError: synthetic staging crash" in sessions[0]["page_failure"]["exception"]


def test_page_font_inventory_failure_invalidates_stale_completion_and_is_not_absence(
    tmp_path,
):
    import PDFEmbeddedFonts as embedded

    class BrokenFontInventoryPage:
        def get_fonts(self, *, full):
            assert full is True
            raise RuntimeError("synthetic page font inventory failure")

    opts = core.ImportOptions()
    stale = _set_completed_font_session(
        opts,
        records={
            "siwaregular": {
                "path": str(tmp_path / "stale.otf"),
                "sha256": "c" * 64,
                "source": "pdf_embedded",
                "xref": 9,
            }
        },
    )
    assert stale["staging_complete"] is True

    with pytest.raises(embedded.EmbeddedFontInventoryError, match="font inventory"):
        core._stage_page_shapestring_fonts(
            object(),
            BrokenFontInventoryPage(),
            opts,
            pdf_sha256=PDF_SHA_A,
            page_number=1,
        )

    sessions = opts._shapestring_font_staging_sessions
    assert len(sessions) == 1
    failed = sessions[0]
    assert failed["pdf_sha256"] == PDF_SHA_A
    assert failed["page_number"] == 1
    assert failed["staging_complete"] is False
    assert failed["records"] == {}
    assert failed["failures"] == []
    page_failure = failed["page_failure"]
    assert page_failure["reason"] == "embedded_font_inventory_failed"
    assert page_failure["pdf_sha256"] == PDF_SHA_A
    assert page_failure["page_number"] == 1
    assert "RuntimeError: synthetic page font inventory failure" in page_failure["exception"]

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert path is None
    assert len(results) == 1
    assert results[0]["source"] == "embedded_font"
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "embedded_font_inventory_failed"
    assert results[0]["pdf_sha256"] == PDF_SHA_A
    assert results[0]["page_number"] == 1
    assert results[0]["staging_complete"] is False
    assert results[0]["page_failure"] == page_failure


def test_page_font_inventory_failure_is_terminal_for_the_exact_span(tmp_path):
    import PDFEmbeddedFonts as embedded

    class BrokenFontInventoryPage:
        def get_fonts(self, *, full):
            assert full is True
            raise OSError("font inventory unavailable")

    item = _proof_item(font="Siwa-Regular", pdf_sha256=PDF_SHA_A, page_number=3)
    opts = core.ImportOptions(text_mode="3d_text")
    with pytest.raises(embedded.EmbeddedFontInventoryError):
        core._stage_page_shapestring_fonts(
            object(),
            BrokenFontInventoryPage(),
            opts,
            pdf_sha256=item["pdf_sha256"],
            page_number=item["page_number"],
        )

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            opts,
            text_group=None,
            page_h=100.0,
            scale=1.0,
        )

    failure = raised.value
    assert not isinstance(failure, core.TextItemImpossible)
    assert failure.attempt["source_item_id"] == item["source_item_id"]
    assert failure.attempt["requested_type"] == "3d_text"
    assert failure.attempt["attempted_type"] == "3d_text"
    assert failure.attempt["outcome"] == "failed"
    assert failure.attempt["cleanup_complete"] is True
    results = failure.attempt["evidence"]["attempted_source_results"]
    assert len(results) == 1
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "embedded_font_inventory_failed"
    assert results[0]["pdf_sha256"] == item["pdf_sha256"]
    assert results[0]["page_number"] == item["page_number"]
    assert results[0]["staging_complete"] is False
    assert opts.text_mode_fallbacks == []


@pytest.mark.parametrize(
    ("pdf_sha256", "page_number"),
    [(PDF_SHA_B, 1), (PDF_SHA_A, 2)],
)
def test_mismatched_pdf_or_page_staging_session_is_invalid(pdf_sha256, page_number):
    opts = core.ImportOptions()
    _set_completed_font_session(opts)

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=pdf_sha256,
        page_number=page_number,
    )

    assert path is None
    assert len(results) == 1
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "font_staging_session_mismatch"
    assert results[0]["pdf_sha256"] == pdf_sha256
    assert results[0]["page_number"] == page_number
    assert results[0]["staging_complete"] is False


def test_stale_prior_page_record_cannot_mask_current_page_staging_failure(tmp_path):
    payload = b"prior-page-font"
    prior_path = tmp_path / "prior.otf"
    prior_path.write_bytes(payload)
    prior_record = {
        "path": str(prior_path),
        "sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "source": "pdf_embedded",
        "xref": 7,
    }
    opts = core.ImportOptions()
    opts._shapestring_font_paths = {"siwaregular": prior_record}
    _set_completed_font_session(
        opts,
        page_number=2,
        records={},
        failures=[{
            "font": "Siwa-Regular",
            "reason": "embedded_font_staging_failed",
            "exception": "ValueError: current page font is broken",
        }],
    )

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=2,
    )

    assert path is None
    assert len(results) == 1
    assert results[0]["source"] == "embedded_font"
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "embedded_font_staging_failed"
    assert results[0]["page_number"] == 2


def test_missing_staged_font_file_is_invalid_and_terminal(tmp_path):
    missing = tmp_path / "missing.otf"
    opts = core.ImportOptions(text_mode="3d_text")
    records = {
        "siwaregular": {
            "path": str(missing),
            "sha256": "b" * 64,
            "source": "pdf_embedded",
            "xref": 7,
        }
    }
    _set_completed_font_session(opts, records=records)
    item = _proof_item()

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert path is None
    assert len(results) == 1
    assert results[0]["source"] == "embedded_font"
    assert results[0]["outcome"] == "invalid"
    assert results[0]["font_identity"] == item["font_identity"]
    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            opts,
            text_group=None,
            page_h=100.0,
            scale=1.0,
        )
    assert raised.value.attempt["reason"] == "exact_font_resolution_invalid"
    assert raised.value.attempt["outcome"] == "failed"
    assert raised.value.attempt["created_entity_ids"] == []
    assert raised.value.attempt["removed_entity_ids"] == []
    assert raised.value.attempt["cleanup_complete"] is True


def test_unreadable_staged_font_file_is_invalid(monkeypatch, tmp_path):
    payload = b"font"
    staged_path = tmp_path / "font.otf"
    staged_path.write_bytes(payload)
    record = {
        "path": str(staged_path),
        "sha256": __import__("hashlib").sha256(payload).hexdigest(),
        "source": "pdf_embedded",
        "xref": 7,
    }
    opts = core.ImportOptions()
    _set_completed_font_session(opts, records={"siwaregular": record})
    original_read_bytes = Path.read_bytes

    def unreadable(path):
        if path == staged_path:
            raise OSError("synthetic unreadable font")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", unreadable)

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert path is None
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "staged_font_file_unreadable"


def test_staged_font_sha_mismatch_is_invalid(tmp_path):
    staged_path = tmp_path / "font.otf"
    staged_path.write_bytes(b"actual")
    record = {
        "path": str(staged_path),
        "sha256": "f" * 64,
        "source": "pdf_embedded",
        "xref": 7,
    }
    opts = core.ImportOptions()
    _set_completed_font_session(opts, records={"siwaregular": record})

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )

    assert path is None
    assert results[0]["outcome"] == "invalid"
    assert results[0]["reason"] == "staged_font_sha256_mismatch"


def test_staged_record_lookup_exception_is_invalid_and_compatibility_wrapper_is_safe():
    class ExplodingRecord(dict):
        def get(self, *_args, **_kwargs):
            raise OSError("synthetic staged-record lookup failure")

    opts = core.ImportOptions()
    opts._shapestring_font_paths = {"siwaregular": ExplodingRecord()}
    _set_completed_font_session(
        opts,
        records={"siwaregular": ExplodingRecord()},
    )
    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Siwa-Regular",
        opts,
        pdf_sha256=PDF_SHA_A,
        page_number=1,
    )
    assert path is None
    assert len(results) == 1
    assert results[0]["source"] == "embedded_font"
    assert results[0]["outcome"] == "invalid"
    assert core._resolve_shapestring_font_path("Siwa-Regular", opts) is None


def test_malformed_resolver_results_are_terminal(monkeypatch):
    item = _proof_item()
    opts = core.ImportOptions(text_mode="3d_text")
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: (None, [object()]),
    )
    with pytest.raises(core.TextRepresentationFailure) as malformed:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            opts,
            text_group=None,
            page_h=100.0,
            scale=1.0,
        )
    assert malformed.value.attempt["reason"] == "exact_font_resolution_invalid"
