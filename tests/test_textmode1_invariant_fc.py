# -*- coding: utf-8 -*-
"""TEXTMODE-1 invariant lock (FC leg, fix-list item 13).

For every text-mode x forced-failure combination the import report must
satisfy:

    (delivered mode == requested mode)
    OR (fallback.text records requested/delivered/reason)

— NEVER neither (owner directive 2026-07-13). The scenarios below drive the
real FC renderers and ladder helpers with a fake Draft API and assert the
invariant on the report produced by the real write_import_report().
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
from test_text_mode_fallback_fc import (  # noqa: E402
    FakeDraft,
    FakeGroup,
    FakePlacement,
    FakeRotation,
    FakeVector,
    _tdict,
)

_MODE_ALIASES = {"label": "labels", "text3d": "3d_text", "outlines": "glyphs"}
# Glyphs and Geometry are one peer family in every host (identical rendering
# engine) — the audit's FINAL ladders treat them as a single rung.
_PEER_FAMILY = {"glyphs", "geometry"}


def _normalize_mode(mode: str) -> str:
    mode = str(mode or "").strip().lower()
    return _MODE_ALIASES.get(mode, mode)


def _modes_equivalent(requested: str, delivered: str) -> bool:
    requested = _normalize_mode(requested)
    delivered = _normalize_mode(delivered)
    if requested == delivered:
        return True
    return requested in _PEER_FAMILY and delivered in _PEER_FAMILY


def assert_textmode1_invariant(report: dict) -> None:
    """(delivered == requested) OR fallback.text{requested,delivered,reason}."""
    extra = report.get("extra") or {}
    requested = _normalize_mode(extra.get("text_mode"))
    entity = extra.get("actual_text_entity_types") or {}
    delivered = _normalize_mode(entity.get("entity_type"))
    delivered_count = int(entity.get("count") or 0)

    if not requested or requested == "none" or delivered_count <= 0:
        return  # no text requested or none delivered — nothing to pin here

    if _modes_equivalent(requested, delivered):
        return

    fallback = report.get("fallback") or {}
    text_block = fallback.get("text")
    assert isinstance(text_block, dict), (
        f"TEXTMODE-1 violated: requested={requested!r} delivered={delivered!r} "
        "with no fallback.text block — silent substitution"
    )
    for key in ("requested", "delivered", "reason"):
        assert str(text_block.get(key) or "").strip(), (
            f"fallback.text missing {key!r}: {text_block!r}"
        )
    assert fallback.get("used") is True


@pytest.fixture()
def fake_freecad(monkeypatch: pytest.MonkeyPatch):
    def _install(draft: FakeDraft, font_path: str = "C:/fonts/fake.ttf"):
        monkeypatch.setattr(core, "Draft", draft)
        monkeypatch.setattr(core, "Vector", FakeVector)
        monkeypatch.setattr(core, "Placement", FakePlacement)
        monkeypatch.setattr(core, "Rotation", FakeRotation)
        monkeypatch.setattr(
            core, "_resolve_shapestring_font_path", lambda name: font_path
        )
        return draft

    return _install


def _write_report(opts, entity_info):
    if entity_info:
        opts._report_extra = {"actual_text_entity_types": dict(entity_info)}
    with tempfile.TemporaryDirectory(prefix="fc_textmode1_inv_") as tmp:
        report_path = Path(tmp) / "import_report.json"
        core.write_import_report(
            pdf_path=str(Path(tmp) / "sample.pdf"),
            output_path=str(report_path),
            opts=opts,
            pages_imported=1,
            total_pages=1,
            text_count=int((entity_info or {}).get("count") or 0),
            elapsed_ms=1.0,
        )
        return json.loads(report_path.read_text(encoding="utf-8"))


def _entity(mode: str, count: int) -> dict:
    return {
        "entity_type": mode,
        "count": count,
        "font_rendered": True,
        "examples": [],
    }


# ── 3D Text requested ───────────────────────────────────────────────────
def test_3d_text_success_requested_equals_delivered(fake_freecad):
    fake_freecad(FakeDraft())
    opts = core.ImportOptions(text_mode="3d_text")
    count = core._render_text_spans_3d(
        _tdict("A", "B"), FakeGroup(), 100.0, opts, 1.0, layout_context={}
    )
    report = _write_report(opts, _entity("3d_text", count))
    assert_textmode1_invariant(report)
    assert "text" not in report["fallback"]


@pytest.mark.parametrize("fail_texts,fail_all", [({"B"}, False), (set(), True)])
def test_3d_text_forced_shapestring_failures(fake_freecad, fail_texts, fail_all):
    fake_freecad(FakeDraft(fail_texts=fail_texts, fail_all_shapestrings=fail_all))
    opts = core.ImportOptions(text_mode="3d_text")
    count = core._render_text_spans_3d(
        _tdict("A", "B"), FakeGroup(), 100.0, opts, 1.0, layout_context={}
    )
    assert count == 2  # every span delivered
    report = _write_report(opts, _entity("3d_text", count))
    assert_textmode1_invariant(report)
    # Mixed/whole failure delivered labels somewhere -> event recorded.
    assert opts.text_mode_fallbacks


def test_3d_text_whole_mode_degrade_to_labels(fake_freecad):
    """No TTF font: 0 spans from the 3D rung; the labels rung delivers."""
    fake_freecad(FakeDraft(), font_path=None)
    opts = core.ImportOptions(text_mode="3d_text")
    count = core._render_text_spans_3d(
        _tdict("A", "B"), FakeGroup(), 100.0, opts, 1.0, layout_context={}
    )
    assert count == 0
    labels = core._render_text_spans_exact_labels(
        _tdict("A", "B"), FakeGroup(), 100.0, opts, 1.0, layout_context={}
    )
    assert labels == 2
    report = _write_report(opts, _entity("labels", labels))
    assert_textmode1_invariant(report)
    assert report["fallback"]["text"]["requested"] == "3d_text"
    assert report["fallback"]["text"]["delivered"] == "labels"


# ── Glyphs / Geometry requested ─────────────────────────────────────────
@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
def test_glyph_family_svg_success_requested_equals_delivered(mode):
    opts = core.ImportOptions(text_mode=mode)
    core._record_text_delivery(opts, "outline_curve_or_mesh", 6)
    report = _write_report(opts, {
        "entity_type": mode,
        "count": 6,
        "font_rendered": False,
        "examples": [],
    })
    assert_textmode1_invariant(report)
    assert "text" not in report["fallback"]


@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
@pytest.mark.parametrize("shapestring_works", [True, False])
def test_glyph_family_svg_failure_walks_ladder(fake_freecad, mode, shapestring_works):
    fake_freecad(FakeDraft(fail_all_shapestrings=not shapestring_works))
    opts = core.ImportOptions(text_mode=mode)
    count, info, pending = core._walk_glyph_svg_failure_rungs(
        _tdict("A", "B"), FakeGroup(), 100.0, opts, 1.0, {}, "svg_renderer_failed"
    )
    assert count == 2 and pending is None
    report = _write_report(opts, info)
    assert_textmode1_invariant(report)
    assert report["fallback"]["used"] is True


@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
def test_glyph_family_svg_and_rung_unavailable_labels_deliver(fake_freecad, mode):
    fake_freecad(FakeDraft(), font_path=None)
    opts = core.ImportOptions(text_mode=mode)
    count, info, pending = core._walk_glyph_svg_failure_rungs(
        _tdict("A", "B"), FakeGroup(), 100.0, opts, 1.0, {}, "svg_renderer_failed"
    )
    assert count == 0 and pending is not None
    labels = core._render_text_spans_exact_labels(
        _tdict("A", "B"), FakeGroup(), 100.0, opts, 1.0, layout_context={}
    )
    assert labels == 2
    report = _write_report(opts, _entity("labels", labels))
    assert_textmode1_invariant(report)
    assert report["fallback"]["text"]["requested"] == mode
    assert report["fallback"]["text"]["delivered"] == "labels"


# ── Labels requested ────────────────────────────────────────────────────
def test_labels_success_requested_equals_delivered(fake_freecad):
    fake_freecad(FakeDraft())
    opts = core.ImportOptions(text_mode="labels")
    count = core._render_text_spans_exact_labels(
        _tdict("A", "B"), FakeGroup(), 100.0, opts, 1.0, layout_context={}
    )
    assert count == 2
    report = _write_report(opts, _entity("labels", count))
    assert_textmode1_invariant(report)
    assert "text" not in report["fallback"]


# ── The invariant checker itself refuses silent substitution ────────────
def test_invariant_checker_rejects_silent_substitution():
    opts = core.ImportOptions(text_mode="3d_text")
    report = _write_report(opts, _entity("labels", 3))
    # write_import_report's safety net reports this one honestly...
    assert_textmode1_invariant(report)
    # ...but a hand-built silent report must FAIL the invariant.
    silent = json.loads(json.dumps(report))
    silent["fallback"] = {"used": False, "reason": None}
    with pytest.raises(AssertionError):
        assert_textmode1_invariant(silent)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
