"""PDF font name -> host font name (PDFHostFonts + core._normalize_pdf_font_name).

Every expectation runs against an INJECTED font index, never against the fonts
of the machine running the tests.  Three worlds are covered: the variant
families are installed, they are not installed, and there is no index at all
(a non-Windows host).  Fixture data is fictional (drawing D042, job 1000-01).
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFHostFonts as host_fonts  # noqa: E402
import PDFImporterCore as core  # noqa: E402


EXACT = "installed_exact_face"
FAMILY = "installed_family"
SYNTHESIZED = "installed_family_style_synthesized"
ALIAS = "substituted_alias"
ALIAS_MISSING = "substituted_alias_not_installed"
NOT_INSTALLED = "not_installed"
UNVERIFIED = "unverified"

_RBIZ = ("Regular", "Bold", "Italic", "Bold Italic")


def _faces(family, postscript_names, styles=_RBIZ):
    return [
        {
            "family": family,
            "style": style,
            "postscript": postscript,
            "full": family if style == "Regular" else "%s %s" % (family, style),
            "path": "X:\\fonts\\%s.ttf" % postscript,
        }
        for style, postscript in zip(styles, postscript_names, strict=True)
    ]


_BASE_FACES = (
    _faces("Arial", ("ArialMT", "Arial-BoldMT", "Arial-ItalicMT", "Arial-BoldItalicMT"))
    + _faces(
        "Times New Roman",
        (
            "TimesNewRomanPSMT", "TimesNewRomanPS-BoldMT",
            "TimesNewRomanPS-ItalicMT", "TimesNewRomanPS-BoldItalicMT",
        ),
    )
    + _faces(
        "Courier New",
        (
            "CourierNewPSMT", "CourierNewPS-BoldMT",
            "CourierNewPS-ItalicMT", "CourierNewPS-BoldItalicMT",
        ),
    )
    + _faces("Calibri", ("Calibri", "Calibri-Bold"), ("Regular", "Bold"))
    + _faces("Verdana", ("Verdana",), ("Regular",))
)
_VARIANT_FACES = (
    _faces(
        "Arial Narrow",
        (
            "ArialNarrow", "ArialNarrow-Bold",
            "ArialNarrow-Italic", "ArialNarrow-BoldItalic",
        ),
    )
    + _faces("Arial Black", ("Arial-Black",), ("Regular",))
    + _faces("Arial Rounded MT Bold", ("ArialRoundedMTBold",), ("Regular",))
    + _faces("Arial Unicode MS", ("ArialUnicodeMS",), ("Regular",))
    + _faces("Calibri Light", ("Calibri-Light",), ("Regular",))
    # The registry calls this font "Swiss 721 Bold BT"; only the name table
    # carries the family GDI knows it by.
    + _faces("Swis721 BT", ("Swiss721BT-Roman", "Swiss721BT-Bold"), ("Roman", "Bold"))
)

INSTALLED = host_fonts.index_from_records(_BASE_FACES + _VARIANT_FACES)
NOT_INSTALLED_WORLD = host_fonts.index_from_records(_BASE_FACES)
NO_INDEX = host_fonts.index_from_records(())


def _face(family, style, full, postscript):
    return {
        "family": family, "style": style, "full": full, "postscript": postscript,
        "path": "X:\\fonts\\%s.ttf" % postscript,
    }


# Shaped like the real name tables of CAD-bundled fonts: name ID 1 is the GDI
# family ("Swis721 Lt BT"), ID 2 a word GDI has no slot for ("Light"), and the
# full name (ID 4) is NOT family + style ("Swiss 721 Light BT").
_CAD_FACES = [
    _face("Swis721 Lt BT", "Light", "Swiss 721 Light BT", "Swiss721BT-Light"),
    _face("Swis721 Lt BT", "Light Italic", "Swiss 721 Light Italic BT",
          "Swiss721BT-LightItalic"),
    _face("Swis721 BlkCn BT", "Black", "Swiss 721 Black Condensed BT",
          "Swiss721BT-BlackCondensed"),
    _face("Swis721 BlkCn BT", "Black Italic", "Swiss 721 Black Condensed Italic BT",
          "Swiss721BT-BlackCondensedItalic"),
    _face("BankGothic Md BT", "Medium", "Bank Gothic Medium BT", "BankGothicBT-Medium"),
    _face("Dutch801 XBd BT", "Extra Bold", "Dutch 801 Extra Bold BT",
          "Dutch801BT-ExtraBold"),
    _face("Swis721 BdOul BT", "Bold", "Swiss 721 Bold Outline BT",
          "Swiss721BT-BoldOutline"),
    _face("Lucida Sans", "Regular", "Lucida Sans Regular", "LucidaSans"),
    _face("Lucida Sans", "Demibold Roman", "Lucida Sans Demibold Roman", "LucidaSans-Demi"),
    _face("Lucida Sans", "Italic", "Lucida Sans Italic", "LucidaSans-Italic"),
    _face("Lucida Sans", "Demibold Italic", "Lucida Sans Demibold Italic",
          "LucidaSans-DemiItalic"),
    _face("Harlow Solid Italic", "Italic", "Harlow Solid Italic", "HarlowSolid"),
    _face("Aharoni", "Bold", "Aharoni Bold", "Aharoni-Bold"),
    _face("Sample_Key", "Sample_Key", "Sample_Key", "Sample_Key"),
]
CAD_FONTS = host_fonts.index_from_records(_CAD_FACES)


def _resolve(name, flags, index):
    result = host_fonts.resolve_host_font(name, flags, index=index)
    return result["host_font"], result["status"]


# ── the mapping table from the owner decision ───────────────────────────
@pytest.mark.parametrize(
    ("name", "flags", "installed", "not_installed", "no_index"),
    [
        # The measured defect: 316 Narrow spans 1.21x too wide, 8 Black 0.75x.
        ("ArialNarrow", 4,
         ("Arial Narrow", EXACT), ("Arial Narrow", NOT_INSTALLED),
         ("Arial Narrow", UNVERIFIED)),
        ("ArialNarrow,Bold", 20,
         ("Arial Narrow Bold", EXACT), ("Arial Narrow Bold", NOT_INSTALLED),
         ("Arial Narrow Bold", UNVERIFIED)),
        ("ArialNarrow,BoldItalic", 22,
         ("Arial Narrow Bold Italic", EXACT),
         ("Arial Narrow Bold Italic", NOT_INSTALLED),
         ("Arial Narrow Bold Italic", UNVERIFIED)),
        ("ArialBlack", 4,
         ("Arial Black", EXACT), ("Arial Black", NOT_INSTALLED),
         ("Arial Black", UNVERIFIED)),
        ("Arial-Black", 4,
         ("Arial Black", EXACT), ("Arial Black", NOT_INSTALLED),
         ("Arial Black", UNVERIFIED)),
        # Installed family is "Swis721 BT"; without an index the raw name
        # passes through exactly as it always did.
        ("Swis721BT,Bold", 20,
         ("Swis721 BT Bold", EXACT), ("Swis721 BT Bold", NOT_INSTALLED),
         ("Swis721BT,Bold", UNVERIFIED)),
        # PostScript style suffixes have no word boundary.
        ("Arial-BoldMT", 16,
         ("Arial Bold", EXACT), ("Arial Bold", EXACT), ("Arial Bold", UNVERIFIED)),
        ("Arial-ItalicMT", 2,
         ("Arial Italic", EXACT), ("Arial Italic", EXACT),
         ("Arial Italic", UNVERIFIED)),
        ("Arial-BoldItalicMT", 18,
         ("Arial Bold Italic", EXACT), ("Arial Bold Italic", EXACT),
         ("Arial Bold Italic", UNVERIFIED)),
        ("Arial,BoldItalic", 18,
         ("Arial Bold Italic", EXACT), ("Arial Bold Italic", EXACT),
         ("Arial Bold Italic", UNVERIFIED)),
        ("TimesNewRomanPS-BoldMT", 20,
         ("Times New Roman Bold", EXACT), ("Times New Roman Bold", EXACT),
         ("Times New Roman Bold", UNVERIFIED)),
        ("Helvetica-BoldOblique", 18,
         ("Arial Bold Italic", ALIAS), ("Arial Bold Italic", ALIAS),
         ("Arial Bold Italic", ALIAS)),
        # Unchanged names stay unchanged.
        ("Arial", 4, ("Arial", EXACT), ("Arial", EXACT), ("Arial", UNVERIFIED)),
        ("ArialMT", 0, ("Arial", EXACT), ("Arial", EXACT), ("Arial", UNVERIFIED)),
        ("Arial,Bold", 20,
         ("Arial Bold", EXACT), ("Arial Bold", EXACT), ("Arial Bold", UNVERIFIED)),
        ("TimesNewRomanPSMT", 4,
         ("Times New Roman", EXACT), ("Times New Roman", EXACT),
         ("Times New Roman", UNVERIFIED)),
        # Standard-14 aliases are substitutions and say so.
        ("Helvetica", 0, ("Arial", ALIAS), ("Arial", ALIAS), ("Arial", ALIAS)),
        ("Helvetica-Narrow", 0,
         ("Arial Narrow", ALIAS), ("Arial Narrow", ALIAS_MISSING),
         ("Arial Narrow", ALIAS)),
        ("Times-Roman", 4,
         ("Times New Roman", ALIAS), ("Times New Roman", ALIAS),
         ("Times New Roman", ALIAS)),
        ("Courier-Bold", 24,
         ("Courier New Bold", ALIAS), ("Courier New Bold", ALIAS),
         ("Courier New Bold", ALIAS)),
        # A weight is part of the family, never folded into the base family.
        ("Calibri-Light", 0,
         ("Calibri Light", EXACT), ("Calibri Light", NOT_INSTALLED),
         ("Calibri-Light", UNVERIFIED)),
        # Family installed, that style is not: the host synthesizes it.
        ("Verdana-Bold", 16,
         ("Verdana Bold", SYNTHESIZED), ("Verdana Bold", SYNTHESIZED),
         ("Verdana-Bold", UNVERIFIED)),
        # "ArialMT,Bold": the stripped name is a face's PostScript name.
        ("ArialMT,Bold", 16,
         ("Arial Bold", FAMILY), ("Arial Bold", FAMILY), ("Arial Bold", UNVERIFIED)),
    ],
)
def test_mapping_table_in_all_three_worlds(
    name, flags, installed, not_installed, no_index
):
    assert _resolve(name, flags, INSTALLED) == installed
    assert _resolve(name, flags, NOT_INSTALLED_WORLD) == not_installed
    assert _resolve(name, flags, NO_INDEX) == no_index


@pytest.mark.parametrize(
    ("name", "expected_installed", "expected_missing"),
    [
        ("ArialRoundedMTBold", "Arial Rounded MT Bold", "Arial Rounded MT Bold"),
        ("ArialUnicodeMS", "Arial Unicode MS", "Arial Unicode MS"),
        ("Marialis", "Marialis", "Marialis"),
        ("HelveticaNeue-Light", "Helvetica Neue Light", "Helvetica Neue Light"),
        ("SegoeUI-Semibold", "Segoe UI Semibold", "Segoe UI Semibold"),
        ("RomanT", "RomanT", "RomanT"),
    ],
)
def test_substring_traps_never_become_a_base_family(
    name, expected_installed, expected_missing
):
    for index, expected in (
        (INSTALLED, expected_installed),
        (NOT_INSTALLED_WORLD, expected_missing),
    ):
        result = host_fonts.resolve_host_font(name, 0, index=index)
        assert result["host_font"] == expected
        assert result["host_font"] not in {"Arial", "Arial Bold", "Times New Roman"}
    # Without an index an unrecognised name passes through untouched.
    assert _resolve(name, 0, NO_INDEX) == (name, UNVERIFIED)


# ── installed faces whose subfamily is not Regular/Bold/Italic/Bold Italic ──
@pytest.mark.parametrize(
    ("name", "flags", "host_font"),
    [
        # Measured in Coin/GDI: "Swis721 Lt BT Light" is drawn as Arial, the
        # family name alone is drawn with the Light face.
        ("Swiss721BT-Light", 4, "Swis721 Lt BT"),
        ("Swiss721BT-LightItalic", 6, "Swis721 Lt BT Italic"),
        ("Swiss721BT-BlackCondensed", 4, "Swis721 BlkCn BT"),
        ("Swiss721BT-BlackCondensedItalic", 6, "Swis721 BlkCn BT Italic"),
        ("BankGothicBT-Medium", 4, "BankGothic Md BT"),
        # A Demibold that shares its family with a Regular is that family's bold.
        ("LucidaSans", 4, "Lucida Sans"),
        ("LucidaSans-Demi", 20, "Lucida Sans Bold"),
        ("LucidaSans-DemiItalic", 22, "Lucida Sans Bold Italic"),
        # A subfamily that is no style at all is never glued onto the family.
        ("Sample_Key", 4, "Sample_Key"),
    ],
)
def test_installed_face_host_name_is_the_family_plus_bold_italic_only(
    name, flags, host_font
):
    result = host_fonts.resolve_host_font(name, flags, index=CAD_FONTS)
    assert (result["host_font"], result["status"]) == (host_font, EXACT)
    assert result["font_file"] == "X:\\fonts\\%s.ttf" % name


def test_every_installed_face_gets_a_name_the_host_knows():
    """family [+ Bold][+ Italic], or the face's own full name - nothing else."""
    for record in _CAD_FACES + _BASE_FACES + _VARIANT_FACES:
        index = CAD_FONTS if record in _CAD_FACES else INSTALLED
        result = host_fonts.resolve_host_font(record["postscript"], 0, index=index)
        assert result["status"] == EXACT, record
        assert result["family"] == record["family"]
        host_font = result["host_font"]
        if host_font == record["full"]:
            assert len(host_font) <= 31  # LOGFONT.lfFaceName
        else:
            assert host_font.startswith(record["family"]), result
            extra_words = host_font[len(record["family"]):].split()
            assert extra_words in ([], ["Bold"], ["Italic"], ["Bold", "Italic"]), result


@pytest.mark.parametrize(
    ("name", "flags", "host_font", "status"),
    [
        # "Bold" asked on top of a family whose only face is bold makes GDI
        # embolden it again (measured: the text gets wider), and the host does
        # not know "Dutch801 XBd BT Bold" as a face: the full name is used.
        ("Dutch801BT-ExtraBold", 20, "Dutch 801 Extra Bold BT", EXACT),
        ("Dutch801XBdBT,Bold", 20, "Dutch 801 Extra Bold BT", EXACT),
        ("Dutch801XBdBT", 20, "Dutch 801 Extra Bold BT", FAMILY),
        ("Swiss721BT-BoldOutline", 20, "Swiss 721 Bold Outline BT", EXACT),
        ("Swis721BdOulBT,Bold", 20, "Swiss 721 Bold Outline BT", EXACT),
        # A full name longer than a GDI face name: the bare family returns the
        # family's only weight.
        ("Swiss721BT-BoldCondensedOutline", 20, "Swis721 BdCnOul BT", EXACT),
        ("Swis721BdCnOulBT,Bold", 20, "Swis721 BdCnOul BT", EXACT),
        # The family already ends in the style word: never "... Italic Italic".
        ("HarlowSolid", 6, "Harlow Solid Italic", EXACT),
        ("HarlowSolidItalic", 6, "Harlow Solid Italic", EXACT),
        # family + style IS this face's full name: the host finds it as such,
        # and it stays right on a machine that has the regular face as well.
        ("Aharoni-Bold", 20, "Aharoni Bold", EXACT),
        ("Aharoni,Bold", 20, "Aharoni Bold", EXACT),
        # A non-bold request for a family with only a bold face stays loud: the
        # regular file may simply be missing on this machine.
        ("Aharoni", 4, "Aharoni", SYNTHESIZED),
        ("Dutch801XBdBT", 4, "Dutch801 XBd BT", SYNTHESIZED),
    ],
)
def test_a_style_the_whole_family_has_is_not_requested_twice(
    name, flags, host_font, status
):
    index = host_fonts.index_from_records(_CAD_FACES + [
        _face("Swis721 BdCnOul BT", "Bold Outline",
              "Swiss 721 Bold Condensed Outline BT", "Swiss721BT-BoldCondensedOutline"),
    ])
    result = host_fonts.resolve_host_font(name, flags, index=index)
    assert (result["host_font"], result["status"]) == (host_font, status)
    assert "Bold Bold" not in result["host_font"]
    assert "Italic Italic" not in result["host_font"]


def test_gdi_style_names_of_light_black_medium_faces_are_not_substitutions():
    names = [
        ("Swis721LtBT", 4, "Swis721 Lt BT"),
        ("Swis721LtBT,Italic", 6, "Swis721 Lt BT Italic"),
        ("Swis721BlkCnBT", 4, "Swis721 BlkCn BT"),
        ("Swis721BlkCnBT,Italic", 6, "Swis721 BlkCn BT Italic"),
        ("BankGothicMdBT", 4, "BankGothic Md BT"),
        ("LucidaSans,Bold", 20, "Lucida Sans Bold"),
        ("LucidaSans,BoldItalic", 22, "Lucida Sans Bold Italic"),
        ("LucidaSans-Bold", 20, "Lucida Sans Bold"),
    ]
    attempts = []
    for position, (name, flags, host_font) in enumerate(names):
        result = host_fonts.resolve_host_font(name, flags, index=CAD_FONTS)
        assert result["host_font"] == host_font
        assert result["status"] in host_fonts.SOURCE_FONT_EQUIVALENT_STATUSES, result
        assert result["font_file"]
        attempts.append({
            "outcome": "verified", "source_item_id": "EX%03d" % position,
            "evidence": {
                "source_font": name, "font_name": result["host_font"],
                "font_status": result["status"],
            },
        })
    # The host draws every one of them with the source font: no warning.
    summary = host_fonts.summarize_host_fonts(attempts)
    assert len(summary["host_font_map"]) == len(names)
    assert summary["host_font_substitutions"] == {}
    assert summary["note"] == ""
    assert summary["console_warnings"] == []
    # A style the family really lacks is still reported.
    assert _resolve("Swis721LtBT,Bold", 20, CAD_FONTS) == (
        "Swis721 Lt BT Bold", SYNTHESIZED
    )
    assert _resolve("BankGothicMdBT,Italic", 6, CAD_FONTS) == (
        "BankGothic Md BT Italic", SYNTHESIZED
    )


def test_faces_that_share_one_family_and_style_slot():
    records = [
        _face("Sample Grotesk", "Regular", "Sample Grotesk", "SampleGrotesk-Regular"),
        _face("Sample Grotesk", "Medium", "Sample Grotesk Medium", "SampleGrotesk-Medium"),
        _face("Sample Grotesk", "Extra Light Condensed",
              "Sample Grotesk Extra Light Condensed", "SampleGrotesk-ExtraLightCond"),
    ]
    assert len(records[2]["full"]) > 31  # longer than a GDI face name can be
    for ordered in (records, records[::-1]):
        index = host_fonts.index_from_records(ordered)
        # The plain Regular owns the family name whatever the listing order...
        assert _resolve("SampleGrotesk-Regular", 4, index) == ("Sample Grotesk", EXACT)
        assert _resolve("SampleGrotesk", 4, index) == ("Sample Grotesk", EXACT)
        assert host_fonts.resolve_host_font("SampleGrotesk", 4, index=index)[
            "font_file"
        ].endswith("SampleGrotesk-Regular.ttf")
        # ...the Medium is requested by its own full name...
        assert _resolve("SampleGrotesk-Medium", 4, index) == (
            "Sample Grotesk Medium", EXACT
        )
        # ...and a face no host name reaches is a substitution, not "exact".
        assert _resolve("SampleGrotesk-ExtraLightCond", 4, index) == (
            "Sample Grotesk", SYNTHESIZED
        )


def test_subset_prefix_is_stripped_before_lookup():
    for name in ("KDMOOQ+ArialNarrow,Bold", "ABCDEF+ArialNarrow-Bold"):
        result = host_fonts.resolve_host_font(name, 20, index=INSTALLED)
        assert result["source_font"] == name
        assert result["host_font"] == "Arial Narrow Bold"
        assert result["status"] == EXACT
        assert result["font_file"] == "X:\\fonts\\ArialNarrow-Bold.ttf"
    # Seven letters, or lowercase, is not a subset tag.
    assert host_fonts.strip_subset_prefix("ABCDEFG+Arial") == "ABCDEFG+Arial"
    assert host_fonts.strip_subset_prefix("abcdef+Arial") == "abcdef+Arial"
    assert host_fonts.font_key("QWERTY+Arial-BoldMT") == "arialboldmt"


def test_mupdf_24_character_truncation_is_rescued():
    truncated = "TimesNewRomanPS-BoldItal"  # ...icMT was cut off by MuPDF
    assert len(truncated) == 24
    # Installed: exactly one face extends the cut-off name.
    assert _resolve(truncated, 22, INSTALLED) == (
        "Times New Roman Bold Italic", EXACT
    )
    # No index: the cut-off style word is dropped and the flags supply it.
    assert _resolve(truncated, 22, NO_INDEX) == (
        "Times New Roman Bold Italic", UNVERIFIED
    )
    cut_foundry = "CourierNewPS-BoldItalicM"
    assert len(cut_foundry) == 24
    assert _resolve(cut_foundry, 26, NO_INDEX) == (
        "Courier New Bold Italic", UNVERIFIED
    )


def test_flags_supply_the_style_a_truncated_name_lost():
    # One letter of the style survived; only the span flags know it is bold.
    # ("-B" extends to both the Bold and the Black face, so the name alone is
    # ambiguous and the unique-face rescue correctly stands aside.)
    name = "SampleGrotesqueDisplay-B"
    assert len(name) == 24
    index = host_fonts.index_from_records(
        _faces("Sample Grotesque Display", ("SampleGrotesqueDisp-Rg",), ("Regular",))
        + _faces("Sample Grotesque Display", ("SampleGrotesqueDisp-Bd",), ("Bold",))
        + _faces("Sample Grotesque Display Black", ("SampleGrotesqueDisp-Blk",), ("Regular",))
    )
    assert _resolve(name, 16, index) == ("Sample Grotesque Display Bold", FAMILY)
    assert _resolve(name, 0, index) == ("Sample Grotesque Display", FAMILY)
    assert _resolve(name, 18, index) == (
        "Sample Grotesque Display Bold Italic", SYNTHESIZED
    )


def test_flags_restore_a_style_suffix_that_was_cut_off_entirely():
    # "SampleHumanistCondensedX-Bold" reaches the span as its first 24
    # characters: the family alone, which IS an installed (regular) face.
    name = "SampleHumanistCondensedX-Bold"[:24]
    assert name == "SampleHumanistCondensedX"
    index = host_fonts.index_from_records(
        _faces(
            "Sample Humanist Condensed X",
            ("SampleHumanistCondensedX", "SampleHumanistCondensedX-Bold"),
            ("Regular", "Bold"),
        )
    )
    assert _resolve(name, 16, index) == ("Sample Humanist Condensed X Bold", FAMILY)
    assert _resolve(name, 0, index) == ("Sample Humanist Condensed X", EXACT)
    # A real, untruncated 24-character face keeps its own style.
    assert len("TimesNewRomanPS-ItalicMT") == 24
    assert _resolve("TimesNewRomanPS-ItalicMT", 6, INSTALLED) == (
        "Times New Roman Italic", EXACT
    )


def test_truncation_rescue_refuses_an_ambiguous_or_untruncated_name():
    index = host_fonts.index_from_records(
        _faces("Sample Sans", ("SampleSansSerifText-BoldCondensed",), ("Bold",))
        + _faces("Sample Sans Wide", ("SampleSansSerifText-BoldExtended",), ("Bold",))
    )
    ambiguous = "SampleSansSerifText-Bold"
    assert len(ambiguous) == 24
    assert _resolve(ambiguous, 16, index)[1] == NOT_INSTALLED
    # A short name is never prefix-matched: "Arial" must not find Arial Narrow.
    narrow_only = host_fonts.index_from_records(_VARIANT_FACES)
    assert _resolve("Arial", 0, narrow_only) == ("Arial", NOT_INSTALLED)


def test_span_flags_pick_among_the_faces_of_one_family_that_extend_a_cut_off_name():
    # MuPDF cuts "Swiss721BT-BlackCondensed" and "...CondensedItalic" to the
    # same 24 characters; both faces are installed, only the flags differ.
    name = "Swiss721BT-BlackCondensedItalic"[:24]
    assert name == "Swiss721BT-BlackCondensed"[:24] == "Swiss721BT-BlackCondense"
    assert _resolve(name, 4, CAD_FONTS) == ("Swis721 BlkCn BT", EXACT)
    assert _resolve(name, 6, CAD_FONTS) == ("Swis721 BlkCn BT Italic", EXACT)
    upright = host_fonts.resolve_host_font(name, 4, index=CAD_FONTS)
    assert upright["font_file"].endswith("Swiss721BT-BlackCondensed.ttf")
    # No face of the family has that style, or there are no flags at all: the
    # name stays ambiguous and is reported, never guessed.
    assert _resolve(name, 20, CAD_FONTS)[1] == NOT_INSTALLED
    assert _resolve(name, None, CAD_FONTS)[1] == NOT_INSTALLED


def test_a_cut_off_style_word_does_not_outvote_the_span_flags():
    # "...-Book" reaches the span as "...-Bo": that is not evidence of Bold.
    name = "SampleHumanistDisplay-Book"[:24]
    assert name == "SampleHumanistDisplay-Bo"
    book = host_fonts.resolve_host_font(name, 4, index=NOT_INSTALLED_WORLD)
    assert (book["host_font"], book["bold"]) == ("Sample Humanist Display", False)
    bold = host_fonts.resolve_host_font(name, 20, index=NOT_INSTALLED_WORLD)
    assert (bold["host_font"], bold["bold"]) == ("Sample Humanist Display Bold", True)
    # Without span flags the cut-off word is the only evidence there is.
    guess = host_fonts.resolve_host_font(name, None, index=NOT_INSTALLED_WORLD)
    assert (guess["host_font"], guess["bold"]) == ("Sample Humanist Display Bold", True)
    # Three letters are a style word by themselves, whatever the flags carry.
    stump = "SampleGrotesqueTexts-BoldItalic"[:24]
    assert stump == "SampleGrotesqueTexts-Bol"
    named = host_fonts.resolve_host_font(stump, 4, index=NOT_INSTALLED_WORLD)
    assert (named["host_font"], named["bold"]) == ("Sample Grotesque Texts Bold", True)
    # Installed family, two families extend the name: the flags decide the style.
    index = host_fonts.index_from_records(
        _faces("Sample Humanist Display", ("SampleHumanistDisp-Rg", "SampleHumanistDisp-Bd"),
               ("Regular", "Bold"))
        + _faces("Sample Humanist Display Bowl", ("SampleHumanistDisp-Bowl",), ("Regular",))
    )
    assert _resolve(name, 4, index) == ("Sample Humanist Display", FAMILY)
    assert _resolve(name, 20, index) == ("Sample Humanist Display Bold", FAMILY)


def test_exact_face_ignores_flags_but_stripped_names_use_them():
    # The name identifies an installed face: a stray flag does not restyle it.
    assert _resolve("ArialNarrow", 20, INSTALLED) == ("Arial Narrow", EXACT)
    assert _resolve("Verdana", 16, INSTALLED) == ("Verdana", EXACT)
    assert _resolve("ArialMT", 20, INSTALLED) == ("Arial", EXACT)
    # ...and the same complete face name is not restyled without an index.
    assert _resolve("Arial", 18, NO_INDEX) == ("Arial", UNVERIFIED)
    assert _resolve("ArialNarrow", 20, NO_INDEX) == ("Arial Narrow", UNVERIFIED)
    assert _resolve("ArialMT", 20, NO_INDEX) == ("Arial", UNVERIFIED)
    assert _resolve("TimesNewRomanPSMT", 20, NO_INDEX) == ("Times New Roman", UNVERIFIED)
    assert _resolve("Arial-BoldMT", 0, NO_INDEX) == ("Arial Bold", UNVERIFIED)
    assert _resolve("Arial-Regular", 16, NO_INDEX) == ("Arial Bold", UNVERIFIED)
    assert _resolve("Arial-Regular", 16, INSTALLED) == ("Arial Bold", FAMILY)
    # Not an installed face by itself: the flags supply the style.
    assert _resolve("Helvetica", 18, INSTALLED) == ("Arial Bold Italic", ALIAS)
    # Garbage flags are ignored, never raised.
    for flags in (None, "", "bold", 2.0, object(), [16], 10 ** 40):
        assert host_fonts.resolve_host_font("Arial", flags, index=INSTALLED)["host_font"]


def test_result_shape_and_input_is_never_mutated():
    result = host_fonts.resolve_host_font("ArialNarrow,Bold", 20, index=INSTALLED)
    assert set(result) == {
        "source_font", "host_font", "family", "bold", "italic", "status", "font_file",
    }
    assert result["family"] == "Arial Narrow"
    assert (result["bold"], result["italic"]) == (True, False)
    result["host_font"] = "tampered"
    again = host_fonts.resolve_host_font("ArialNarrow,Bold", 20, index=INSTALLED)
    assert again["host_font"] == "Arial Narrow Bold"


@pytest.mark.parametrize("empty", ["", "   ", None, 0])
def test_empty_input_returns_empty_host_font(empty, monkeypatch):
    monkeypatch.setattr(host_fonts, "_INDEX", INSTALLED)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})
    assert host_fonts.resolve_host_font(empty, index=INSTALLED)["host_font"] == ""
    assert core._normalize_pdf_font_name(empty) == ""


@pytest.mark.parametrize(
    "name",
    ["+++", "-", ",Bold", "Bold", "MT", "CIDFont+F1", "T3Font_0", "ＭＳ ゴシック",
     "ABCDEF+", "A", "Italic-Bold", "x" * 300],
)
def test_non_empty_input_never_returns_empty(name, monkeypatch):
    for index in (INSTALLED, NOT_INSTALLED_WORLD, NO_INDEX):
        assert host_fonts.resolve_host_font(name, 0, index=index)["host_font"]
        monkeypatch.setattr(host_fonts, "_INDEX", index)
        monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})
        assert core._normalize_pdf_font_name(name)
        assert core._normalize_pdf_font_name(name, 18)


def test_malformed_injected_index_never_raises():
    for index in ({}, {"faces": None, "families": None, "available": True},
                  {"faces": {"arial": ("Arial",)}, "families": {}, "available": True}):
        result = host_fonts.resolve_host_font("Arial-BoldMT", 16, index=index)
        assert result["host_font"]
        assert result["status"]


def test_names_that_are_not_text_never_raise():
    class NoStr:
        def __str__(self):
            raise RuntimeError("no str for you")

    class NoBool:
        def __bool__(self):
            raise RuntimeError("no bool")

    for hostile in (NoStr(), NoBool()):
        result = host_fonts.resolve_host_font(hostile, 4, index=INSTALLED)
        assert (result["host_font"], result["status"]) == ("", "empty")
    # A bytes name is decoded, not written to the host as "b'ArialNarrow'".
    assert _resolve(b"ArialNarrow", 4, INSTALLED) == ("Arial Narrow", EXACT)
    assert _resolve(bytearray(b"KDMOOQ+Arial-BoldMT"), 16, INSTALLED) == ("Arial Bold", EXACT)
    undecodable = host_fonts.resolve_host_font(b"\xff\xfeSample", 4, index=INSTALLED)
    assert undecodable["host_font"] and "b'" not in undecodable["host_font"]


# ── core delegation ─────────────────────────────────────────────────────
def test_core_normalize_delegates_with_flags(monkeypatch):
    monkeypatch.setattr(host_fonts, "_INDEX", INSTALLED)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})

    assert core._normalize_pdf_font_name("ArialNarrow") == "Arial Narrow"
    assert core._normalize_pdf_font_name("ArialNarrow,Bold", 20) == "Arial Narrow Bold"
    assert core._normalize_pdf_font_name("ArialBlack", 4) == "Arial Black"
    assert core._normalize_pdf_font_name("Swis721BT,Bold", 20) == "Swis721 BT Bold"
    assert core._normalize_pdf_font_name("Arial-BoldMT", 16) == "Arial Bold"
    assert core._normalize_pdf_font_name("ArialRoundedMTBold") == "Arial Rounded MT Bold"
    assert core._normalize_pdf_font_name("Marialis") == "Marialis"
    assert core._normalize_pdf_font_name("TimesNewRomanPS-BoldItal", 22) == (
        "Times New Roman Bold Italic"
    )
    # Guard rail kept by tests/test_freecad_production_contract.py.
    assert core._normalize_pdf_font_name("ArialMT") == "Arial"


def test_core_normalize_keeps_the_true_name_when_the_variant_is_missing(monkeypatch):
    monkeypatch.setattr(host_fonts, "_INDEX", NOT_INSTALLED_WORLD)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})

    assert core._normalize_pdf_font_name("ArialNarrow", 4) == "Arial Narrow"
    assert core._resolve_pdf_host_font("ArialNarrow", 4)["status"] == NOT_INSTALLED
    assert core._normalize_pdf_font_name("ArialMT") == "Arial"


def test_default_index_results_are_cached_per_name_and_flags(monkeypatch):
    calls = []

    def counting_index(refresh=False):
        calls.append(refresh)
        return INSTALLED

    monkeypatch.setattr(host_fonts, "installed_font_index", counting_index)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})
    for _ in range(50):
        assert host_fonts.resolve_host_font("ArialNarrow", 4)["host_font"] == "Arial Narrow"
    assert len(calls) == 1
    assert host_fonts.resolve_host_font("ArialNarrow", 4 | 1)["host_font"] == "Arial Narrow"
    assert len(calls) == 1  # only the bold/italic bits are part of the identity


# ── installed-font index: must be unbreakable ───────────────────────────
def _name_table(names):
    records, storage = [], b""
    for name_id, text in sorted(names.items()):
        data = text.encode("utf-16-be")
        records.append(struct.pack(">6H", 3, 1, 0x409, name_id, len(data), len(storage)))
        storage += data
    return struct.pack(">HHH", 0, len(records), 6 + 12 * len(records)) + b"".join(records) + storage


def _sfnt(names, tag=b"\x00\x01\x00\x00", base_offset=0):
    table = _name_table(names)
    header = tag + struct.pack(">HHHH", 1, 16, 0, 0)
    directory = struct.pack(">4sIII", b"name", 0, base_offset + 28, len(table))
    return header + directory + table


def _names(family, style, postscript):
    full = family if style == "Regular" else "%s %s" % (family, style)
    return {1: family, 2: style, 4: full, 6: postscript}


def test_index_reads_ttf_otf_and_ttc_name_tables(tmp_path):
    ttf = tmp_path / "sample_narrow_bold.ttf"
    ttf.write_bytes(_sfnt(_names("Sample Narrow", "Bold", "SampleNarrow-Bold")))
    otf = tmp_path / "sample_display.otf"
    otf.write_bytes(_sfnt(_names("Sample Display", "Regular", "SampleDisplay"), tag=b"OTTO"))
    first = _sfnt(_names("Sample Mono", "Regular", "SampleMono"), base_offset=20)
    second = _sfnt(
        _names("Sample Mono", "Italic", "SampleMono-Italic"),
        base_offset=20 + len(first),
    )
    ttc = tmp_path / "sample_mono.ttc"
    ttc.write_bytes(
        b"ttcf" + struct.pack(">III", 0x00010000, 2, 20)
        + struct.pack(">I", 20 + len(first)) + first + second
    )

    index = host_fonts.build_font_index([str(ttf), str(otf), str(ttc)])

    assert index["available"] is True
    assert _resolve("SampleNarrow,Bold", 16, index) == ("Sample Narrow Bold", EXACT)
    assert _resolve("SampleDisplay", 0, index) == ("Sample Display", EXACT)
    assert _resolve("SampleMono-Italic", 2, index) == ("Sample Mono Italic", EXACT)
    assert host_fonts.resolve_host_font("SampleMono", 0, index=index)["font_file"] == str(ttc)


def test_corrupt_and_unreadable_font_files_are_skipped_never_raised(tmp_path):
    good = tmp_path / "sample_good.ttf"
    good.write_bytes(_sfnt(_names("Sample Good", "Regular", "SampleGood")))
    valid = _sfnt(_names("Sample Cut", "Regular", "SampleCut"))
    broken = {
        "empty.ttf": b"",
        "short.ttf": b"\x00\x01",
        "garbage.ttf": bytes(range(256)) * 8,
        "truncated.ttf": valid[: len(valid) // 2],
        "huge_table_count.ttf": b"\x00\x01\x00\x00" + struct.pack(">HHHH", 65535, 0, 0, 0),
        "name_points_past_eof.ttf": b"\x00\x01\x00\x00" + struct.pack(">HHHH", 1, 0, 0, 0)
        + struct.pack(">4sIII", b"name", 0, 0xFFFFFF00, 0xFFFFFFFF),
        "bad_collection.ttc": b"ttcf" + struct.pack(">II", 0x00010000, 0xFFFFFFFF) + b"\xff" * 64,
        "collection_loop.ttc": b"ttcf" + struct.pack(">III", 0x00010000, 1, 0),
        "bad_string_offsets.ttf": b"\x00\x01\x00\x00" + struct.pack(">HHHH", 1, 0, 0, 0)
        + struct.pack(">4sIII", b"name", 0, 28, 18)
        + struct.pack(">HHH", 0, 1, 60000) + struct.pack(">6H", 3, 1, 0x409, 1, 5000, 5000),
    }
    paths = [str(good)]
    for file_name, payload in broken.items():
        target = tmp_path / file_name
        target.write_bytes(payload)
        paths.append(str(target))
    paths += [str(tmp_path / "missing.ttf"), str(tmp_path), "", "\x00bad\x00path.ttf"]

    index = host_fonts.build_font_index(paths)

    assert sorted(index["families"].values()) == ["Sample Good"]
    assert host_fonts.read_font_names(str(tmp_path / "garbage.ttf")) == []


def test_installed_index_survives_a_hostile_windows_host(monkeypatch, tmp_path):
    """Registry faults, a missing system folder and a per-user font folder."""
    user_fonts = tmp_path / "local" / "Microsoft" / "Windows" / "Fonts"
    user_fonts.mkdir(parents=True)
    (user_fonts / "sample_user.ttf").write_bytes(
        _sfnt(_names("Sample User", "Regular", "SampleUser"))
    )
    (user_fonts / "broken.otf").write_bytes(b"not a font")
    (user_fonts / "notes.txt").write_text("not a font", encoding="utf-8")

    class _Registry:
        HKEY_LOCAL_MACHINE = 1
        HKEY_CURRENT_USER = 2

        @staticmethod
        def OpenKey(*_args):
            raise PermissionError("registry locked")

    monkeypatch.setitem(sys.modules, "winreg", _Registry)
    monkeypatch.setattr(host_fonts, "_is_windows", lambda: True)
    monkeypatch.setenv("WINDIR", str(tmp_path / "no_such_windows"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(host_fonts, "_INDEX", None)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})

    index = host_fonts.installed_font_index(refresh=True)

    assert index["available"] is True
    assert list(index["families"].values()) == ["Sample User"]
    assert host_fonts.resolve_host_font("SampleUser")["status"] == EXACT
    assert host_fonts.resolve_host_font("ArialNarrow")["status"] == NOT_INSTALLED


def test_unreadable_font_folders_give_an_empty_index_not_an_error(monkeypatch, tmp_path):
    not_a_folder = tmp_path / "fonts_is_a_file"
    not_a_folder.write_text("x", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "winreg", None)  # import winreg -> ImportError
    monkeypatch.setattr(host_fonts, "_is_windows", lambda: True)
    monkeypatch.setenv("WINDIR", str(not_a_folder))
    monkeypatch.setenv("LOCALAPPDATA", str(not_a_folder))
    monkeypatch.setattr(host_fonts, "_INDEX", None)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})

    index = host_fonts.installed_font_index(refresh=True)

    assert index == {"faces": {}, "families": {}, "available": False}
    assert _resolve("ArialNarrow,Bold", 20, None) == ("Arial Narrow Bold", UNVERIFIED)


def test_non_windows_host_reports_unverified(monkeypatch):
    monkeypatch.setattr(host_fonts, "_is_windows", lambda: False)
    monkeypatch.setattr(host_fonts, "_INDEX", None)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})

    assert host_fonts.installed_font_index(refresh=True)["available"] is False
    assert _resolve("ArialNarrow", 4, None) == ("Arial Narrow", UNVERIFIED)
    assert _resolve("ArialMT", 0, None) == ("Arial", UNVERIFIED)
    assert _resolve("Helvetica", 0, None) == ("Arial", ALIAS)
    assert core._normalize_pdf_font_name("Swis721BT,Bold", 20) == "Swis721BT,Bold"


def test_index_build_failure_degrades_to_unverified(monkeypatch):
    def explode():
        raise RuntimeError("font enumeration failed")

    monkeypatch.setattr(host_fonts, "_installed_font_files", explode)
    monkeypatch.setattr(host_fonts, "_INDEX", None)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})

    assert _resolve("ArialNarrow", 4, None) == ("Arial Narrow", UNVERIFIED)


def test_module_is_standard_library_only():
    import ast

    tree = ast.parse(Path(host_fonts.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= {"__future__", "os", "re", "struct", "threading", "typing", "winreg"}
