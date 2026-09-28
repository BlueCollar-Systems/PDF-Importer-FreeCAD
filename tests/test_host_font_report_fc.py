"""Host font honesty: native text evidence, import report, 3D alias rows.

The font index is always injected, so nothing here depends on the fonts of the
machine running the tests.  Fixture data is fictional (drawing D042, "SAMPLE",
job 1000-01).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFHostFonts as host_fonts  # noqa: E402
import PDFImporterCore as core  # noqa: E402


def _face(family, style, postscript):
    full = family if style == "Regular" else "%s %s" % (family, style)
    return {"family": family, "style": style, "postscript": postscript,
            "full": full, "path": "X:\\fonts\\%s.ttf" % postscript}


_ARIAL = [
    _face("Arial", "Regular", "ArialMT"),
    _face("Arial", "Bold", "Arial-BoldMT"),
]
_NARROW = [
    _face("Arial Narrow", "Regular", "ArialNarrow"),
    _face("Arial Narrow", "Bold", "ArialNarrow-Bold"),
]
INSTALLED = host_fonts.index_from_records(_ARIAL + _NARROW)
NARROW_MISSING = host_fonts.index_from_records(_ARIAL)


@pytest.fixture
def font_world(monkeypatch):
    def install(index):
        monkeypatch.setattr(host_fonts, "_INDEX", index)
        monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})

    return install


# ── fake FreeCAD host (document objects with a GUI view) ────────────────
class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


class _Rotation:
    def __init__(self, _axis=None, angle=0.0):
        self.angle = float(angle)


class _Placement:
    def __init__(self, base=None, rotation=None):
        self.Base = base or _Vector()
        self.Rotation = rotation or _Rotation()


class _View:
    def __init__(self):
        self.FontSize = 0.0
        self.FontName = ""
        self.Justification = ""
        self.TextColor = (1.0, 1.0, 1.0)


class _HostObject:
    def __init__(self, document, name, type_id):
        self.Document = document
        self.Name = self.Label = name
        self.TypeId = type_id
        self.ViewObject = _View()
        self.Placement = _Placement()
        self.PropertiesList = []

    def addProperty(self, _kind, name, _group):
        if name not in self.PropertiesList:
            self.PropertiesList.append(name)


class _Document:
    def __init__(self):
        self.Objects = []

    def addObject(self, kind, name):
        obj = _HostObject(self, "%s_%d" % (name, len(self.Objects)), kind)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self, *_args):
        return None


class _Group:
    def __init__(self, document):
        self.Document = document
        self.objects = []

    def addObject(self, obj):
        if obj not in self.objects:
            self.objects.append(obj)

    def removeObject(self, obj):
        if obj in self.objects:
            self.objects.remove(obj)


class _Draft:
    def __init__(self, document):
        self.document = document

    def make_text(self, texts, placement=None):
        obj = self.document.addObject("App::FeaturePython", "PDF_Text")
        obj.Text = list(texts)
        obj.Placement = placement
        obj.Proxy = SimpleNamespace(Type="Text")
        return obj


def _install_native_host(monkeypatch):
    document = _Document()
    monkeypatch.setattr(core, "Draft", _Draft(document))
    monkeypatch.setattr(core, "Vector", _Vector)
    monkeypatch.setattr(core, "Rotation", _Rotation)
    monkeypatch.setattr(core, "Placement", _Placement)
    return document, _Group(document)


def _text_item(font, flags, text="SAMPLE D042"):
    span = {
        "text": text, "font": font, "flags": flags, "size": 12.0,
        "bbox": (10.0, 20.0, 90.0, 32.0), "origin": (10.0, 30.0), "color": 0,
    }
    return {
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": "a" * 64,
        "page_number": 1,
        "source_item_id": "p1:b0:l0:s0",
        "requested_type": "text",
        "text": text,
        "font_identity": core._canonical_font_identity(font),
        "bbox": span["bbox"],
        "origin": span["origin"],
        "line_direction": (1.0, 0.0),
        "rotation_deg": 0.0,
        "span": span,
        "block_index": 0,
        "line_index": 0,
        "span_index": 0,
    }


def _deliver(monkeypatch, font, flags):
    document, group = _install_native_host(monkeypatch)
    opts = core.ImportOptions(
        text_mode="text", import_text=True, scale_to_mm=False, user_scale=1.0
    )
    result = core._deliver_text_item_native(
        _text_item(font, flags), "text", opts,
        text_group=group, page_h=100.0, scale=1.0,
    )
    return document.getObject(result["created_entity_ids"][0]), result


# ── native text evidence ────────────────────────────────────────────────
def test_native_text_carries_the_true_variant_font_everywhere(monkeypatch, font_world):
    font_world(INSTALLED)

    host, result = _deliver(monkeypatch, "ArialNarrow,Bold", 20)

    assert result["outcome"] == "verified"
    # One string in all three places keeps PDFTextLayout's source layout active
    # and lets PDFStyleRestore re-apply the same font when the GUI opens.
    assert host.ViewObject.FontName == "Arial Narrow Bold"
    assert host.PDFTextFontName == "Arial Narrow Bold"
    evidence = result["evidence"]
    assert evidence["font_name"] == "Arial Narrow Bold"
    assert evidence["source_font"] == "ArialNarrow,Bold"
    assert evidence["font_status"] == "installed_exact_face"
    # The text height is the source height; the font fix never touches it.
    assert host.ViewObject.FontSize == pytest.approx(evidence["font_size"])


def test_native_text_missing_variant_keeps_true_name_height_and_says_so(
    monkeypatch, font_world
):
    font_world(INSTALLED)
    _host, installed = _deliver(monkeypatch, "ArialNarrow", 4)
    font_world(NARROW_MISSING)
    host, missing = _deliver(monkeypatch, "ArialNarrow", 4)

    assert host.ViewObject.FontName == "Arial Narrow"
    assert host.PDFTextFontName == "Arial Narrow"
    assert missing["evidence"]["font_name"] == "Arial Narrow"
    assert missing["evidence"]["font_status"] == "not_installed"
    # Honest fallback: the height is never changed to fake a width.
    assert missing["evidence"]["font_size"] == installed["evidence"]["font_size"]


def test_native_text_span_flags_restore_a_truncated_style(monkeypatch, font_world):
    font_world(host_fonts.index_from_records(()))  # no index: a non-Windows host

    host, result = _deliver(monkeypatch, "TimesNewRomanPS-BoldItal", 22)

    assert host.ViewObject.FontName == "Times New Roman Bold Italic"
    assert result["evidence"]["font_status"] == "unverified"


# ── import report ───────────────────────────────────────────────────────
def _attempt(index, source_font, flags, world):
    resolved = host_fonts.resolve_host_font(source_font, flags, index=world)
    return {
        "source_item_id": "p1:b0:l%d:s0" % index,
        "requested_type": "text",
        "attempted_type": "text",
        "final_type": "text",
        "outcome": "verified",
        "created_entity_ids": ["PDF_Text_%d" % index],
        "evidence": {
            "source_text": "EX%03d" % index,
            "font_name": resolved["host_font"],
            "source_font": source_font,
            "font_status": resolved["status"],
        },
    }


def _write_report(tmp_path, attempts, pdf_path=None):
    opts = core.ImportOptions(import_mode="vector", text_mode="text", import_text=True)
    opts.text_delivery_attempts = list(attempts)
    report_path = tmp_path / "import_report.json"
    core.write_import_report(
        pdf_path=str(pdf_path or tmp_path / "D042.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        primitive_count=60,
        text_count=len(attempts),
        elapsed_ms=5.0,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def test_report_lists_every_font_and_stays_quiet_when_nothing_is_substituted(tmp_path):
    attempts = [_attempt(i, "ArialNarrow", 4, INSTALLED) for i in range(5)]
    attempts.append(_attempt(5, "Arial-BoldMT", 16, INSTALLED))

    data = _write_report(tmp_path, attempts)

    assert data["extra"]["host_font_map"] == {
        "ArialNarrow": {
            "source_font": "ArialNarrow",
            "host_font": "Arial Narrow",
            "status": "installed_exact_face",
            "span_count": 5,
            "example_source_item_ids": ["p1:b0:l0:s0", "p1:b0:l1:s0", "p1:b0:l2:s0"],
        },
        "Arial-BoldMT": {
            "source_font": "Arial-BoldMT",
            "host_font": "Arial Bold",
            "status": "installed_exact_face",
            "span_count": 1,
            "example_source_item_ids": ["p1:b0:l5:s0"],
        },
    }
    assert data["extra"]["host_font_substitutions"] == {}
    assert data["result"]["warnings"] == 0
    assert "warnings_present" not in data["extra"]["diagnostics"]["signals"]
    assert "Host font substitution" not in data["extra"]["human_summary"]


def test_report_counts_and_names_every_substituted_font(tmp_path, monkeypatch):
    warned = []
    monkeypatch.setattr(core, "_warn", warned.append)
    attempts = (
        [_attempt(i, "ArialNarrow", 4, NARROW_MISSING) for i in range(4)]
        + [_attempt(4, "Helvetica", 0, NARROW_MISSING)]
        + [_attempt(5, "Arial", 4, NARROW_MISSING)]
        # Unverified attempts and non-native evidence never enter the font map.
        + [{"source_item_id": "p1:b0:l9:s0", "outcome": "failed",
            "evidence": {"source_font": "Ghost", "font_status": "not_installed"}},
           {"source_item_id": "p1:b0:l8:s0", "outcome": "verified",
            "evidence": {"solid_count": 1}}]
    )

    data = _write_report(tmp_path, attempts)

    extra = data["extra"]
    assert set(extra["host_font_map"]) == {"ArialNarrow", "Helvetica", "Arial"}
    assert extra["host_font_substitutions"] == {
        "ArialNarrow": {
            "source_font": "ArialNarrow",
            "host_font": "Arial Narrow",
            "status": "not_installed",
            "span_count": 4,
            "example_source_item_ids": ["p1:b0:l0:s0", "p1:b0:l1:s0", "p1:b0:l2:s0"],
            "source_font_equivalent": False,
        },
        "Helvetica": {
            "source_font": "Helvetica",
            "host_font": "Arial",
            "status": "substituted_alias",
            "span_count": 1,
            "example_source_item_ids": ["p1:b0:l4:s0"],
            "source_font_equivalent": False,
        },
    }
    assert data["result"]["warnings"] == 2
    assert "warnings_present" in extra["diagnostics"]["signals"]
    note = extra["font_substitution_note"]
    assert "Host font substitution: 2 PDF fonts (5 text items)" in note
    assert "ArialNarrow -> Arial Narrow [not installed]" in note
    assert "Helvetica -> Arial [substituted alias]" in note
    assert "height is unchanged" in note
    summary = extra["human_summary"]
    assert "2 warnings recorded" in summary
    assert "Host font substitution: 2 PDF fonts" in summary
    # One console warning per distinct substituted font, none for Arial.
    assert len(warned) == 2
    assert any("'ArialNarrow'" in line and "'Arial Narrow'" in line for line in warned)
    assert any("'Helvetica'" in line and "'Arial'" in line for line in warned)


def test_host_note_is_appended_after_the_core_pdf_font_audit(tmp_path, monkeypatch):
    """The shared core rewrites font_substitution_note inside build_import_report."""
    monkeypatch.setattr(core, "_warn", lambda _line: None)
    pdf_path = tmp_path / "D042.pdf"
    document = core.fitz.open()
    page = document.new_page(width=200, height=100)
    page.insert_text((20, 50), "SAMPLE 1000-01", fontname="helv", fontsize=10)
    document.save(str(pdf_path))
    document.close()

    data = _write_report(
        tmp_path, [_attempt(0, "Helvetica", 0, INSTALLED)], pdf_path=pdf_path
    )

    note = data["extra"]["font_substitution_note"]
    assert note.startswith("Non-embedded PDF fonts detected (Helvetica)")
    assert note.endswith("width may differ from the PDF")
    assert "Host font substitution: 1 PDF font (1 text item)" in note
    assert "Host font substitution: 1 PDF font" in data["extra"]["human_summary"]
    assert data["result"]["warnings"] == 1


def test_a_host_with_no_font_index_says_so_once_and_does_not_warn_per_font(tmp_path, monkeypatch):
    # Linux and macOS have no font index: nothing is known either way, so calling every
    # font - Arial -> Arial included - a substitution would be wrong and would cry wolf
    # on every import.
    monkeypatch.setattr(host_fonts, "_is_windows", lambda: False)
    monkeypatch.setattr(host_fonts, "_INDEX", None)
    monkeypatch.setattr(host_fonts, "_RESOLVE_CACHE", {})
    attempts = [_attempt(i, name, 0, None) for i, name in enumerate(("Arial", "Arial,Bold", "ArialNarrow"))]
    assert {a["evidence"]["font_status"] for a in attempts} == {"unverified"}

    data = _write_report(tmp_path, attempts)

    extra = data["extra"]
    assert {entry["status"] for entry in extra["host_font_map"].values()} == {"unverified"}
    assert extra["host_font_substitutions"] == {}
    assert extra["host_font_unverified"] == ["Arial", "Arial,Bold", "ArialNarrow"]
    assert data["result"]["warnings"] == 0
    assert "warnings_present" not in extra["diagnostics"]["signals"]
    note = extra["font_substitution_note"]
    assert "Host fonts not checked: 3 PDF fonts (3 text items)" in note
    assert "not drawn with the source font" not in note
    assert "Host fonts not checked" in extra["human_summary"]


def test_report_without_native_text_still_emits_an_empty_font_map(tmp_path):
    data = _write_report(tmp_path, [])

    assert data["extra"]["host_font_map"] == {}
    assert data["extra"]["host_font_substitutions"] == {}
    assert data["result"]["warnings"] == 0


def test_same_pdf_name_with_two_styles_keeps_both_mappings():
    attempts = [
        _attempt(0, "Helvetica", 0, INSTALLED),
        _attempt(1, "Helvetica", 16, INSTALLED),
        _attempt(2, "Helvetica", 16, INSTALLED),
    ]

    summary = host_fonts.summarize_host_fonts(attempts)

    assert summary["host_font_map"]["Helvetica"]["host_font"] == "Arial"
    assert summary["host_font_map"]["Helvetica -> Arial Bold"]["span_count"] == 2
    assert len(summary["console_warnings"]) == 2
    assert host_fonts.summarize_host_fonts(None)["note"] == ""
    assert host_fonts.summarize_host_fonts([None, 7, {"outcome": "verified"}]) == {
        "host_font_map": {}, "host_font_substitutions": {}, "host_font_unverified": [],
        "note": "", "console_warnings": [],
    }


# ── 3D Text exact system-font rows ──────────────────────────────────────
@pytest.mark.parametrize(
    ("font", "file_name"),
    [
        ("ArialNarrow", "ARIALN.TTF"),
        ("ArialNarrow,Bold", "ARIALNB.TTF"),
        ("ArialNarrow-Italic", "ARIALNI.TTF"),
        ("ArialNarrow,BoldItalic", "ARIALNBI.TTF"),
        ("ArialBlack", "ariblk.ttf"),
        ("Arial-Black", "ariblk.ttf"),
        # Guard rail: plain Arial still resolves to Arial, never to a variant.
        ("Arial", "arial.ttf"),
    ],
)
def test_3d_text_resolves_arial_variants_to_their_own_files(
    monkeypatch, tmp_path, font, file_name
):
    fonts_dir = tmp_path / "Fonts"
    fonts_dir.mkdir()
    for name in ("ARIALN.TTF", "ARIALNB.TTF", "ARIALNI.TTF", "ARIALNBI.TTF",
                 "ariblk.ttf", "arial.ttf"):
        (fonts_dir / name).write_bytes(b"\x00\x01\x00\x00")
    monkeypatch.setenv("WINDIR", str(tmp_path))
    opts = core.ImportOptions()
    opts._shapestring_font_staging_sessions = [{
        "pdf_sha256": "a" * 64, "page_number": 1, "staging_complete": True,
        "records": {}, "failures": [],
    }]

    path, results = core._resolve_shapestring_font_path_with_evidence(
        font, opts, pdf_sha256="a" * 64, page_number=1
    )

    assert path is not None
    assert Path(path).name.lower() == file_name.lower()
    assert Path(path).parent == fonts_dir
    assert results[-1]["source"] == "system_font"
    assert results[-1]["outcome"] == "found"


def test_3d_text_still_refuses_to_map_helvetica_to_a_system_font(monkeypatch, tmp_path):
    (tmp_path / "Fonts").mkdir()
    (tmp_path / "Fonts" / "arial.ttf").write_bytes(b"\x00\x01\x00\x00")
    monkeypatch.setenv("WINDIR", str(tmp_path))
    opts = core.ImportOptions()
    opts._shapestring_font_staging_sessions = [{
        "pdf_sha256": "a" * 64, "page_number": 1, "staging_complete": True,
        "records": {}, "failures": [],
    }]

    path, results = core._resolve_shapestring_font_path_with_evidence(
        "Helvetica-Narrow", opts, pdf_sha256="a" * 64, page_number=1
    )

    assert path is None
    assert results[-1]["source"] == "system_font"
    assert results[-1]["outcome"] == "not_found"
