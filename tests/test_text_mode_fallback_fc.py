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


class FakeDraft:
    """make_shapestring raises for configured texts; make_text always works."""

    def __init__(self, fail_texts=(), fail_all_shapestrings=False):
        self.fail_texts = set(fail_texts)
        self.fail_all = bool(fail_all_shapestrings)
        self.shapestrings = []
        self.labels = []

    def make_shapestring(self, text, font_path):
        if self.fail_all or text in self.fail_texts:
            raise RuntimeError(f"ShapeString failed for {text!r}")
        obj = FakeObj("shapestring", text)
        self.shapestrings.append(obj)
        return obj

    def make_text(self, texts, placement=None):
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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
