from __future__ import annotations

import copy
import hashlib
import io
import math
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402
from pdfcadcore.fitz_loader import import_fitz  # noqa: E402


REQUESTED_MODES = ("text", "labels", "3d_text", "glyphs", "geometry", "raster")


def _test_font_bytes(*, empty_a: bool, visible_b: bool = False) -> bytes:
    builder = FontBuilder(1000, isTTF=True)
    glyph_order = [".notdef", "A"] + (["B"] if visible_b else [])
    builder.setupGlyphOrder(glyph_order)
    character_map = {0x41: "A"}
    if visible_b:
        character_map[0x42] = "B"
    builder.setupCharacterMap(character_map)
    builder.setupHorizontalMetrics({name: (600, 0) for name in glyph_order})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable(
        {
            "familyName": "Physical Ink Test",
            "styleName": "Regular",
            "uniqueFontIdentifier": "PhysicalInkTest-Regular",
            "fullName": "Physical Ink Test Regular",
            "psName": "PhysicalInkTest-Regular",
        }
    )
    builder.setupOS2(
        sTypoAscender=800,
        sTypoDescender=-200,
        usWinAscent=800,
        usWinDescent=200,
    )
    builder.setupPost()
    glyphs = {}
    for glyph_name in glyph_order:
        pen = TTGlyphPen(None)
        should_draw = glyph_name == "A" and not empty_a
        should_draw = should_draw or (glyph_name == "B" and visible_b)
        if should_draw:
            pen.moveTo((100, 0))
            pen.lineTo((100, 700))
            pen.lineTo((500, 700))
            pen.lineTo((500, 0))
            pen.closePath()
        glyphs[glyph_name] = pen.glyph()
    builder.setupGlyf(glyphs)
    builder.setupMaxp()
    output = io.BytesIO()
    builder.font.save(output)
    return output.getvalue()


def _bound_pdf_item(tmp_path: Path, requested_mode: str, scenario: str) -> dict:
    fitz = import_fitz()
    document = fitz.open()
    page = document.new_page()
    if scenario == "invisible_render_mode":
        page.insert_text(
            (72, 72),
            "A",
            fontname="helv",
            fontsize=12,
            render_mode=3,
        )
    elif scenario == "zero_opacity":
        page.insert_text(
            (72, 72),
            "A",
            fontname="helv",
            fontsize=12,
            fill_opacity=0,
        )
    elif scenario in {"empty_glyph", "mixed_glyphs"}:
        font_path = tmp_path / (scenario + ".ttf")
        font_path.write_bytes(
            _test_font_bytes(
                empty_a=True,
                visible_b=scenario == "mixed_glyphs",
            )
        )
        page.insert_text(
            (72, 72),
            "AB" if scenario == "mixed_glyphs" else "A",
            fontname="PhysicalInkTest",
            fontfile=str(font_path),
            fontsize=12,
        )
    elif scenario == "layout_space":
        page.insert_text((72, 72), "A B", fontname="helv", fontsize=12)
    else:  # pragma: no cover - helper guard
        raise AssertionError("unknown source-ink scenario")

    payload = document.tobytes()
    document.close()
    document = fitz.open(stream=payload, filetype="pdf")
    try:
        page = document[0]
        raw_tdict = page.get_text("rawdict")
        items = list(
            core._iter_text_source_items(
                raw_tdict,
                1,
                hashlib.sha256(payload).hexdigest(),
                requested_mode,
            )
        )
        assert len(items) == 1
        [bound] = core._bind_page_text_source_ink_evidence(page, raw_tdict, items)
        return bound
    finally:
        document.close()


@pytest.mark.parametrize("requested_mode", REQUESTED_MODES)
@pytest.mark.parametrize(
    "scenario",
    ("invisible_render_mode", "zero_opacity", "empty_glyph"),
)
def test_nonwhitespace_zero_ink_is_physically_classified_for_every_requested_mode(
    tmp_path: Path,
    requested_mode: str,
    scenario: str,
) -> None:
    item = _bound_pdf_item(tmp_path, requested_mode, scenario)

    evidence = item["source_ink_evidence"]
    assert item["requested_type"] == requested_mode
    assert item["text"] == "A"
    assert evidence["classification"] == "zero_visible_ink"
    assert evidence["all_characters_physically_resolved"] is True
    assert evidence["font_identity"] == item["font_identity"]
    assert evidence["font_asset_bindings"]
    assert evidence["glyph_id_sequence"] == [evidence["characters"][0]["glyph_id"]]
    assert type(evidence["characters"][0]["glyph_id"]) is int


@pytest.mark.parametrize("requested_mode", REQUESTED_MODES)
def test_mixed_physical_ink_is_explicit_and_does_not_change_requested_mode(
    tmp_path: Path,
    requested_mode: str,
) -> None:
    item = _bound_pdf_item(tmp_path, requested_mode, "mixed_glyphs")

    evidence = item["source_ink_evidence"]
    assert item["requested_type"] == requested_mode
    assert item["text"] == "AB"
    assert evidence["classification"] == "mixed_visible_and_zero_ink"
    assert evidence["zero_ink_characters_layout_only"] is False
    assert core._mixed_source_ink_requires_fallback(evidence) is False
    assert [record["zero_visible_ink"] for record in evidence["characters"]] == [
        True,
        False,
    ]
    if requested_mode != "raster":
        manifest = core._build_source_ink_segments(item)
        assert [segment["requested_type"] for segment in manifest["segments"]] == [
            requested_mode,
            requested_mode,
        ]


def test_exact_space_glyph_is_layout_only_and_does_not_force_fallback(
    tmp_path: Path,
) -> None:
    item = _bound_pdf_item(tmp_path, "3d_text", "layout_space")
    evidence = item["source_ink_evidence"]

    assert evidence["classification"] == "mixed_visible_and_zero_ink"
    assert evidence["zero_ink_characters_layout_only"] is True
    assert core._mixed_source_ink_requires_fallback(evidence) is False
    zero_records = [
        record for record in evidence["characters"] if record["zero_visible_ink"]
    ]
    assert len(zero_records) == 1
    assert zero_records[0]["layout_only_zero_ink"] is True
    assert zero_records[0]["glyph_name"].lower() == "space"


def _synthetic_character_item(value: str) -> dict:
    span = {
        "font": "TestFont",
        "size": 12.0,
        "bbox": (10.0, 10.0, 16.0, 22.0),
        "origin": (10.0, 20.0),
        "chars": [
            {
                "origin": (10.0, 20.0),
                "bbox": (10.0, 10.0, 16.0, 22.0),
                "c": value,
                "synthetic": True,
            }
        ],
    }
    raw_tdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [{"dir": (1.0, 0.0), "spans": [span]}],
            }
        ]
    }
    [item] = list(core._iter_text_source_items(raw_tdict, 1, "a" * 64, "text"))
    [bound] = core._bind_page_text_source_ink_evidence(
        SimpleNamespace(get_texttrace=lambda: [], get_fonts=lambda full=True: []),
        raw_tdict,
        [item],
    )
    return bound


def _shared_validate(evidence: dict, item: dict) -> bool:
    from pdfcadcore.source_ink import source_ink_evidence_verified

    return source_ink_evidence_verified(
        evidence,
        expected_pdf_sha256=item["pdf_sha256"],
        expected_page_number=item["page_number"],
        expected_source_item_id=item["source_item_id"],
        expected_source_text=item["text"],
        expected_font_identity=item["font_identity"],
        expected_font_asset_bindings=item["source_font_asset_bindings"],
        expected_glyph_id_sequence=item["source_glyph_id_sequence"],
    )


@pytest.mark.parametrize("value", [" ", "\u200b", "\u2060", "A"])
def test_synthetic_rawdict_character_never_claims_physical_ink(value: str) -> None:
    item = _synthetic_character_item(value)

    assert "source_ink_evidence" not in item
    assert "source_font_asset_bindings" not in item
    assert "source_glyph_id_sequence" not in item


@pytest.mark.parametrize("record_scope", ["top", "character", "font_binding"])
def test_exact_source_ink_schemas_reject_redigested_extra_fields(
    tmp_path: Path,
    record_scope: str,
) -> None:
    item = _bound_pdf_item(tmp_path, "3d_text", "invisible_render_mode")
    evidence = copy.deepcopy(item["source_ink_evidence"])
    if record_scope == "top":
        evidence["contradictory_extra_field"] = True
    elif record_scope == "character":
        evidence["characters"][0]["contradictory_extra_field"] = True
    else:
        evidence["font_asset_bindings"][0]["contradictory_extra_field"] = True
    evidence["evidence_sha256"] = core._source_ink_evidence_digest(evidence)

    assert _shared_validate(evidence, item) is False


def test_source_ink_digest_rejects_nan_and_verifier_fails_closed(
    tmp_path: Path,
) -> None:
    item = _bound_pdf_item(tmp_path, "3d_text", "invisible_render_mode")
    evidence = copy.deepcopy(item["source_ink_evidence"])
    evidence["characters"][0]["opacity"] = math.nan

    with pytest.raises(ValueError, match="non-finite"):
        core._source_ink_evidence_digest(evidence)
    evidence["evidence_sha256"] = "0" * 64
    assert _shared_validate(evidence, item) is False


def test_source_ink_digest_rejects_cycles_and_verifier_is_total(
    tmp_path: Path,
) -> None:
    item = _bound_pdf_item(tmp_path, "3d_text", "invisible_render_mode")
    evidence = copy.deepcopy(item["source_ink_evidence"])
    evidence["characters"].append(evidence)

    with pytest.raises(ValueError, match="cycle"):
        core._source_ink_evidence_digest(evidence)
    assert _shared_validate(evidence, item) is False


@pytest.mark.parametrize(
    "mutation",
    (
        "wrong_pdf",
        "wrong_page",
        "wrong_source_item",
        "wrong_font_identity",
        "wrong_font_asset",
        "wrong_glyph_id",
        "bool_glyph_id",
        "missing_glyph_id",
        "integer_zero_flag",
    ),
)
def test_shared_validator_rejects_redigested_source_replay_and_authority_tamper(
    tmp_path: Path,
    mutation: str,
) -> None:
    item = _bound_pdf_item(tmp_path, "3d_text", "invisible_render_mode")
    evidence = copy.deepcopy(item["source_ink_evidence"])
    assert _shared_validate(evidence, item) is True

    record = evidence["characters"][0]
    if mutation == "wrong_pdf":
        evidence["pdf_sha256"] = "b" * 64
    elif mutation == "wrong_page":
        evidence["page_number"] = 999
    elif mutation == "wrong_source_item":
        evidence["source_item_id"] = "p1:b9:l9:s9"
    elif mutation == "wrong_font_identity":
        evidence["font_identity"] = {
            "raw_name": "WrongFont",
            "normalized_key": "wrongfont",
        }
    elif mutation == "wrong_font_asset":
        evidence["font_asset_bindings"][0]["usable_font_sha256"] = "c" * 64
    elif mutation == "wrong_glyph_id":
        record["glyph_id"] += 1
    elif mutation == "bool_glyph_id":
        record["glyph_id"] = True
    elif mutation == "missing_glyph_id":
        record.pop("glyph_id")
    elif mutation == "integer_zero_flag":
        record["zero_visible_ink"] = 1
    evidence["evidence_sha256"] = core._source_ink_evidence_digest(evidence)

    assert _shared_validate(evidence, item) is False


def test_report_validator_requires_independent_pdf_page_font_asset_and_glyph_binding(
    tmp_path: Path,
) -> None:
    from pdfcadcore import import_report

    item = _bound_pdf_item(tmp_path, "3d_text", "invisible_render_mode")
    evidence = item["source_ink_evidence"]
    expected = {
        "expected_pdf_sha256": item["pdf_sha256"],
        "expected_page_number": item["page_number"],
        "expected_font_identity": item["font_identity"],
        "expected_font_asset_bindings": item["source_font_asset_bindings"],
        "expected_glyph_id_sequence": item["source_glyph_id_sequence"],
    }

    assert import_report._freecad_source_ink_evidence_verified(
        evidence,
        item["source_item_id"],
        item["text"],
        **expected,
    ) is True

    for key, wrong_value in (
        ("expected_pdf_sha256", "f" * 64),
        ("expected_page_number", 999),
        (
            "expected_font_identity",
            {"raw_name": "WrongFont", "normalized_key": "wrongfont"},
        ),
        (
            "expected_font_asset_bindings",
            [
                {
                    **item["source_font_asset_bindings"][0],
                    "usable_font_sha256": "e" * 64,
                    "asset_id": "sha256:" + "e" * 64,
                }
            ],
        ),
        ("expected_glyph_id_sequence", [evidence["characters"][0]["glyph_id"] + 1]),
    ):
        tampered_expected = copy.deepcopy(expected)
        tampered_expected[key] = wrong_value
        assert import_report._freecad_source_ink_evidence_verified(
            evidence,
            item["source_item_id"],
            item["text"],
            **tampered_expected,
        ) is False


def test_font_asset_declared_hashes_must_match_exact_bytes(monkeypatch) -> None:
    font_bytes = _test_font_bytes(empty_a=False)
    asset = SimpleNamespace(
        usable_sha256="e" * 64,
        source_sha256="f" * 64,
        source_bytes=font_bytes,
        usable_bytes=font_bytes,
        asset_id="sha256:" + "e" * 64,
        source_xref=7,
        source_format="ttf",
        usable_format="ttf",
        source_origin="embedded_pdf_font",
        base_font_name="TestFont",
        span_font_name="TestFont",
    )
    catalog = SimpleNamespace(for_span=lambda _font: asset)
    from pdfcadcore import embedded_fonts

    monkeypatch.setattr(
        embedded_fonts.EmbeddedFontCatalog,
        "from_page",
        lambda *_args, **_kwargs: catalog,
    )
    span = {
        "font": "TestFont",
        "size": 12.0,
        "bbox": (10.0, 10.0, 16.0, 22.0),
        "origin": (10.0, 20.0),
        "chars": [
            {
                "origin": (10.0, 20.0),
                "bbox": (10.0, 10.0, 16.0, 22.0),
                "c": " ",
                "synthetic": False,
            }
        ],
    }
    raw_tdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [{"dir": (1.0, 0.0), "spans": [span]}],
            }
        ]
    }
    [item] = list(core._iter_text_source_items(raw_tdict, 1, "a" * 64, "text"))
    page = SimpleNamespace(
        get_texttrace=lambda: [
            {
                "font": "TestFont",
                "type": 0,
                "opacity": 1.0,
                "chars": ((32, 1, (10.0, 20.0), (10.0, 10.0, 16.0, 22.0)),),
            }
        ]
    )

    [bound] = core._bind_page_text_source_ink_evidence(page, raw_tdict, [item])

    assert "source_ink_evidence" not in bound


@pytest.mark.parametrize("requested_mode", REQUESTED_MODES[:-1])
def test_obsolete_mixed_item_impossibility_cannot_advance_the_ladder(
    tmp_path: Path,
    requested_mode: str,
) -> None:
    item = _bound_pdf_item(tmp_path, requested_mode, "mixed_glyphs")
    opts = core.ImportOptions(text_mode=requested_mode, import_text=True)

    def structural_deliverer(bound_item, attempted_mode, _opts):
        core._raise_mixed_source_ink_impossible(bound_item, attempted_mode)

    def raster_deliverer(bound_item, attempted_mode, _opts):
        return {
            "source_item_id": bound_item["source_item_id"],
            "requested_type": bound_item["requested_type"],
            "attempted_type": attempted_mode,
            "final_type": attempted_mode,
            "outcome": "verified",
            "created_entity_ids": ["Raster001"],
            "delivery_entity_ids": ["Raster001"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 1,
            "evidence": {
                "host_entity_type": "Image::ImagePlane",
                "source_asset_sha256": "f" * 64,
                "raster_content_verified": True,
            },
        }

    deliverers = {
        mode: structural_deliverer for mode in core.TEXT_ITEM_FALLBACK_LADDERS[requested_mode]
    }
    deliverers["raster"] = raster_deliverer

    with pytest.raises(core.TextRepresentationFailure):
        core._run_text_item_fallback_ladder(
            item,
            requested_mode,
            deliverers,
            opts,
        )

    assert item["requested_type"] == requested_mode
    assert [attempt["attempted_type"] for attempt in opts.text_delivery_attempts] == [
        requested_mode
    ]
    assert opts.text_delivery_attempts[0]["outcome"] == "failed"
    assert opts.text_mode_fallbacks == []


def test_production_source_ink_paths_have_no_unicode_whitespace_gate() -> None:
    import inspect
    import PDFSvgTextRenderer as renderer

    for helper in (
        core._bind_page_text_source_ink_evidence,
        core._deliver_text_item_native,
        core._deliver_text_item_3d,
        core._deliver_text_item_svg,
        renderer._source_text_units,
        renderer._build_global_placement_assignments,
    ):
        assert ".isspace()" not in inspect.getsource(helper)
