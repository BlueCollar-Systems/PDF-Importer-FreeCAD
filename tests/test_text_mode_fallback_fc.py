# -*- coding: utf-8 -*-
"""TEXTMODE-1 FC ladder locks — failed spans are delivered, never dropped.

Owner directive 2026-07-13: the requested text mode is the delivered text
mode; substitution is a loud, reported fallback down the documented FC
ladder. These tests exercise the real renderers with a fake Draft API.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


# ── Fake FreeCAD/Draft API ──────────────────────────────────────────────
class FakeVector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __mul__(self, k):
        return FakeVector(self.x * k, self.y * k, self.z * k)

    __rmul__ = __mul__

    def __add__(self, other):
        return FakeVector(self.x + other.x, self.y + other.y, self.z + other.z)


class FakePlacement:
    def __init__(self, *args, **kwargs):
        self.args = args


class FakeRotation:
    def __init__(self, *args, **kwargs):
        self.args = args


class _AttrSink:
    """Absorbs arbitrary attribute assignment (ViewObject stand-in)."""


class FakeObj:
    def __init__(self, kind, text):
        self.kind = kind
        self.text = text
        self.ViewObject = _AttrSink()
        self.Name = f"{kind}_{text}"


class FakeGroup:
    def __init__(self):
        self.objects = []
        self.Name = "Text"

    def addObject(self, obj):
        self.objects.append(obj)


class FakeDocumentGroup:
    """Document group stand-in for the production page-import path."""

    def __init__(self, name):
        self.Name = name
        self.Group = []

    def addObject(self, obj):
        self.Group.append(obj)

    def removeObject(self, obj):
        self.Group.remove(obj)

    def isDerivedFrom(self, kind):
        return kind == "App::DocumentObjectGroup"


class FakeDocument:
    def __init__(self):
        self.Objects = []

    def addObject(self, kind, name):
        assert kind == "App::DocumentObjectGroup"
        obj = FakeDocumentGroup(name)
        self.Objects.append(obj)
        return obj

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self):
        return None


class FakePage:
    def __init__(self, tdict):
        self.rotation = 0
        self.mediabox = SimpleNamespace(width=200.0, height=100.0)
        self.rect = SimpleNamespace(width=200.0, height=100.0)
        self._tdict = tdict

    def get_drawings(self):
        return []

    def get_images(self, full=True):
        return []

    def get_text(self, kind):
        assert kind == "dict"
        return self._tdict


class FakePdfDocument:
    is_encrypted = False

    def __init__(self, page):
        self.page = page

    def __len__(self):
        return 1

    def load_page(self, index):
        assert index == 0
        return self.page


class FakeFreeCAD:
    GuiUp = False

    class Console:
        PrintMessage = staticmethod(lambda _message: None)
        PrintWarning = staticmethod(lambda _message: None)
        PrintError = staticmethod(lambda _message: None)


class FakeDraft:
    """make_shapestring raises for configured texts; make_text always works."""

    def __init__(self, fail_texts=(), fail_all_shapestrings=False):
        self.fail_texts = set(fail_texts)
        self.fail_all = bool(fail_all_shapestrings)
        self.shapestrings = []
        self.labels = []
        self.calls = []  # ordered spy: ("shapestring"|"label", text)

    def make_shapestring(self, text, font_path):
        self.calls.append(("shapestring", text))
        if self.fail_all or text in self.fail_texts:
            raise RuntimeError(f"ShapeString failed for {text!r}")
        obj = FakeObj("shapestring", text)
        self.shapestrings.append(obj)
        return obj

    def make_text(self, texts, placement=None):
        self.calls.append(("label", str(texts[0])))
        obj = FakeObj("label", str(texts[0]))
        self.labels.append(obj)
        return obj


def _span(text: str, x: float) -> dict:
    return {
        "text": text,
        "size": 10.0,
        "font": "Helvetica",
        "origin": (x, 50.0),
        "bbox": (x, 40.0, x + 30.0, 52.0),
        "descender": -0.2,
        "ascender": 0.8,
    }


def _tdict(*texts: str) -> dict:
    spans = [_span(text, 10.0 + 60.0 * i) for i, text in enumerate(texts)]
    return {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {"dir": (1.0, 0.0), "bbox": (10.0, 40.0, 200.0, 52.0), "spans": spans}
                ],
            }
        ]
    }


@pytest.fixture()
def fake_freecad(monkeypatch: pytest.MonkeyPatch):
    def _install(draft: FakeDraft):
        monkeypatch.setattr(core, "Draft", draft)
        monkeypatch.setattr(core, "Vector", FakeVector)
        monkeypatch.setattr(core, "Placement", FakePlacement)
        monkeypatch.setattr(core, "Rotation", FakeRotation)
        monkeypatch.setattr(
            core, "_resolve_shapestring_font_path", lambda name: "C:/fonts/fake.ttf"
        )
        return draft

    return _install


# ── Item 3 (P0): failed ShapeString spans are DELIVERED, not dropped ────
def test_failed_shapestring_span_is_delivered_via_labels_rung(fake_freecad):
    draft = fake_freecad(FakeDraft(fail_texts={"FAIL"}))
    opts = core.ImportOptions(text_mode="3d_text")
    group = FakeGroup()

    count = core._render_text_spans_3d(
        _tdict("KEEP", "FAIL"), group, 100.0, opts, 1.0, layout_context={}
    )

    # BOTH spans delivered: one ShapeString + one rescued Draft label.
    assert count == 2
    assert [obj.text for obj in draft.shapestrings] == ["KEEP"]
    assert [obj.text for obj in draft.labels] == ["FAIL"]
    assert len(group.objects) == 2

    # Telemetry still counted, substitution recorded loudly.
    assert opts.shapestring_skips["shapestring_failed"] == 1
    assert opts.text_mode_fallbacks == [
        {
            "requested": "3d_text",
            "delivered": "labels",
            "reason": "shapestring_failed",
            "count": 1,
        }
    ]
    assert opts.text_delivered_counts == {"native_3d_text": 1, "native_label": 1}

    # Degraded provenance: the rescued span records the DELIVERED type.
    provenance = list(getattr(opts, "_source_provenance_objects", []) or [])
    assert [p.created_entity_type for p in provenance] == ["native_label"]


def test_all_spans_failing_still_deliver_labels(fake_freecad):
    draft = fake_freecad(FakeDraft(fail_all_shapestrings=True))
    opts = core.ImportOptions(text_mode="3d_text")
    group = FakeGroup()

    count = core._render_text_spans_3d(
        _tdict("ONE", "TWO"), group, 100.0, opts, 1.0, layout_context={}
    )

    assert count == 2
    assert draft.shapestrings == []
    assert sorted(obj.text for obj in draft.labels) == ["ONE", "TWO"]
    assert opts.text_mode_fallbacks == [
        {
            "requested": "3d_text",
            "delivered": "labels",
            "reason": "shapestring_failed",
            "count": 2,
        }
    ]
    assert opts.text_delivered_counts == {"native_label": 2}


def test_import_report_carries_fallback_text_after_span_rescue(fake_freecad):
    fake_freecad(FakeDraft(fail_texts={"FAIL"}))
    opts = core.ImportOptions(text_mode="3d_text")
    group = FakeGroup()

    count = core._render_text_spans_3d(
        _tdict("KEEP", "FAIL"), group, 100.0, opts, 1.0, layout_context={}
    )
    assert count == 2

    # Mirror the host dispatch: entity info summarized for the report.
    opts._report_extra = {
        "actual_text_entity_types": {
            "entity_type": "3d_text",
            "count": count,
            "font_rendered": True,
            "examples": [],
        }
    }

    with tempfile.TemporaryDirectory(prefix="fc_textmode1_") as tmp:
        report_path = Path(tmp) / "import_report.json"
        core.write_import_report(
            pdf_path=str(Path(tmp) / "sample.pdf"),
            output_path=str(report_path),
            opts=opts,
            pages_imported=1,
            total_pages=1,
            primitive_count=0,
            text_count=count,
            elapsed_ms=5.0,
        )
        data = json.loads(report_path.read_text(encoding="utf-8"))

    assert data["fallback"]["used"] is True
    assert data["fallback"]["text"] == {
        "requested": "3d_text",
        "delivered": "labels",
        "reason": "shapestring_failed",
        "count": 1,
    }
    assert "text_mode_fallback" in data["extra"]["diagnostics"]["signals"]
    actual = data["extra"]["actual_text_entity_types"]
    assert actual["native_3d_text"] == 1
    assert actual["native_label"] == 1
    assert data["extra"]["shapestring_skips"]["shapestring_failed"] == 1


# ── Item 8 (P1): glyphs/geometry SVG failure walks 3D Text before labels ─
def test_svg_failure_walks_shapestring_rung_first(fake_freecad):
    draft = fake_freecad(FakeDraft())
    opts = core.ImportOptions(text_mode="glyphs")
    group = FakeGroup()

    count, info, pending = core._walk_glyph_svg_failure_rungs(
        _tdict("ONE", "TWO"), group, 100.0, opts, 1.0, {}, "svg_renderer_failed"
    )

    # The CLOSER 3D Text rung was attempted (and delivered) — not labels.
    assert count == 2
    assert pending is None
    assert [kind for kind, _ in draft.calls] == ["shapestring", "shapestring"]
    assert draft.labels == []
    assert info["entity_type"] == "3d_text"
    assert opts.text_mode_fallbacks == [
        {
            "requested": "glyphs",
            "delivered": "3d_text",
            "reason": "svg_renderer_failed",
            "count": 2,
        }
    ]


def test_svg_failure_then_shapestring_failure_delivers_labels(fake_freecad):
    draft = fake_freecad(FakeDraft(fail_all_shapestrings=True))
    opts = core.ImportOptions(text_mode="geometry")
    group = FakeGroup()

    count, info, pending = core._walk_glyph_svg_failure_rungs(
        _tdict("ONE", "TWO"), group, 100.0, opts, 1.0, {}, "svg_renderer_failed"
    )

    # ShapeString attempted FIRST, then the labels rung delivered every span.
    assert count == 2
    assert pending is None
    assert [kind for kind, _ in draft.calls] == [
        "shapestring", "shapestring", "label", "label",
    ]
    assert sorted(obj.text for obj in draft.labels) == ["ONE", "TWO"]
    assert info["entity_type"] == "labels"
    # Honest at each rung: the delivering rung is the one reported.
    assert opts.text_mode_fallbacks == [
        {
            "requested": "geometry",
            "delivered": "labels",
            "reason": "shapestring_failed",
            "count": 2,
        }
    ]


def test_svg_failure_with_no_shapestring_font_defers_to_label_rungs(
    fake_freecad, monkeypatch
):
    draft = fake_freecad(FakeDraft())
    monkeypatch.setattr(core, "_resolve_shapestring_font_path", lambda name: None)
    opts = core.ImportOptions(text_mode="glyphs")
    group = FakeGroup()

    count, info, pending = core._walk_glyph_svg_failure_rungs(
        _tdict("ONE",), group, 100.0, opts, 1.0, {}, "svg_renderer_failed"
    )

    assert count == 0
    assert info is None
    assert pending == {"requested": "glyphs", "reason": "svg_renderer_failed"}
    assert draft.calls == []
    assert opts.shapestring_skips.get("no_ttf_font") == 1


def test_report_fallback_text_honest_when_svg_falls_to_3d_text(fake_freecad):
    fake_freecad(FakeDraft())
    opts = core.ImportOptions(text_mode="glyphs")
    group = FakeGroup()

    count, info, _pending = core._walk_glyph_svg_failure_rungs(
        _tdict("ONE", "TWO"), group, 100.0, opts, 1.0, {}, "svg_renderer_failed"
    )
    opts._report_extra = {"actual_text_entity_types": dict(info)}

    with tempfile.TemporaryDirectory(prefix="fc_textmode1_") as tmp:
        report_path = Path(tmp) / "import_report.json"
        core.write_import_report(
            pdf_path=str(Path(tmp) / "sample.pdf"),
            output_path=str(report_path),
            opts=opts,
            pages_imported=1,
            total_pages=1,
            text_count=count,
            elapsed_ms=5.0,
        )
        data = json.loads(report_path.read_text(encoding="utf-8"))

    assert data["fallback"]["used"] is True
    assert data["fallback"]["text"] == {
        "requested": "glyphs",
        "delivered": "3d_text",
        "reason": "svg_renderer_failed",
        "count": 2,
    }
    assert data["extra"]["actual_text_entity_types"]["native_3d_text"] == 2


@pytest.mark.parametrize("empty_svg_result", [None, {}, {"glyphs": 0}])
def test_production_path_empty_svg_walks_3d_rung_and_reports_fallback(
    fake_freecad, monkeypatch, empty_svg_result
):
    """None/empty/zero-glyph SVG output is failure, not a Labels shortcut."""
    import PDFVectorImporter.src.PDFSvgTextRenderer as svg_renderer

    draft = fake_freecad(FakeDraft())
    monkeypatch.setattr(core, "FreeCAD", FakeFreeCAD)
    monkeypatch.setattr(
        svg_renderer, "render_text", lambda *args, **kwargs: empty_svg_result
    )

    opts = core.ImportOptions(
        text_mode="glyphs",
        import_mode="vector",
        layer_mode="none",
        raster_fallback=False,
        ignore_images=True,
        verbose=False,
    )
    page = FakePage(_tdict("ONE", "TWO"))

    _group, info = core._import_pdf_page_inner(
        FakePdfDocument(page), "sample.pdf", 1, opts, FakeDocument()
    )

    # Production dispatch must attempt the closer 3D Text rung first. A direct
    # Labels fallback would put label calls here (the bug this test catches).
    assert draft.calls == [("shapestring", "ONE"), ("shapestring", "TWO")]
    assert info["entity_type"] == "3d_text"
    assert info["count"] == 2
    assert opts.text_mode_fallbacks == [
        {
            "requested": "glyphs",
            "delivered": "3d_text",
            "reason": "svg_renderer_empty",
            "count": 2,
        }
    ]

    opts._report_extra = {"actual_text_entity_types": dict(info)}
    with tempfile.TemporaryDirectory(prefix="fc_textmode1_empty_svg_") as tmp:
        report_path = Path(tmp) / "import_report.json"
        core.write_import_report(
            pdf_path="sample.pdf",
            output_path=str(report_path),
            opts=opts,
            pages_imported=1,
            total_pages=1,
            text_count=2,
            elapsed_ms=1.0,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report["fallback"]["text"] == {
        "requested": "glyphs",
        "delivered": "3d_text",
        "reason": "svg_renderer_empty",
        "count": 2,
    }
    assert report["extra"]["actual_text_entity_types"]["native_3d_text"] == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
