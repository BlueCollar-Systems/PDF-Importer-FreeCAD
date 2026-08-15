# -*- coding: utf-8 -*-
"""import_report text_mode parity (bcs.import_report/1.1)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "PDFVectorImporter"))

from pdfcadcore.import_report import (
    build_actual_text_entity_types,
    build_fallback_transitions,
    build_font_embedding_hints,
    build_import_contract_ready,
    build_import_report,
    build_pdf_interactive_note,
    build_text_mode_fallback,
)


def test_clean_scale_evaluation_is_explicit_and_contract_ready():
    report = build_import_report(
        host_app="freecad",
        importer_version="4.0.83",
        pdf_path="drawing.pdf",
        mode="vector",
        pages=1,
        primitive_count=40,
        import_text=False,
        text_mode="none",
        extra={
            "resolved_scale": {
                "factor": 24.0,
                "notation": '1/2" = 1\'-0"',
                "source": "titleblock",
                "confidence": 0.98,
                "fallback_reason": "",
            },
            "scale_hints": {
                "title_block_detected": True,
                "dimension_count": 4,
                "alternate_scale_factors": [24.0],
            },
        },
    )

    extra = report.to_dict()["extra"]
    assert extra["scale_crosscheck"] == {
        "level": "ok",
        "reasons": [],
        "messages": [],
    }
    assert extra["import_contract_ready"]["ready"] is True
    assert extra["import_contract_ready"]["checks"]["scale_crosscheck"] is True


def test_malformed_scale_evaluation_remains_fail_closed():
    report = build_import_report(
        host_app="freecad",
        importer_version="4.0.83",
        pdf_path="drawing.pdf",
        mode="vector",
        primitive_count=40,
        import_text=False,
        text_mode="none",
    )
    report.extra["scale_crosscheck"] = None

    ready = build_import_contract_ready(report)

    assert ready["ready"] is False
    assert ready["checks"]["scale_crosscheck"] is False


def test_build_import_report_includes_text_mode_in_extra():
    report = build_import_report(
        host_app="freecad",
        pdf_path="sample.pdf",
        mode="auto",
        import_text=True,
        text_mode="3d_text",
    )
    data = report.to_dict()
    assert data["mode"] == "auto"
    assert data["extra"]["text_mode"] == "3d_text"
    assert data["extra"]["import_text"] is True


def test_build_import_report_geometry_mode():
    report = build_import_report(
        host_app="blender",
        pdf_path="sample.pdf",
        mode="vector",
        import_text=True,
        text_mode="geometry",
        performance_phases={"geometry_ms": 10.5},
        helper_timings_ms={"svg_renderer_ms": 2.0},
        text_source_spans=4,
        text_glyph_estimate=18,
        extra={"curves": 12},
    )
    data = report.to_dict()
    assert data["extra"]["text_mode"] == "geometry"
    assert data["extra"]["curves"] == 12
    assert data["extra"]["text_source_spans"] == 4
    assert data["extra"]["text_glyph_estimate"] == 18
    assert data["performance"]["phases"]["geometry_ms"] == 10.5
    assert data["performance"]["helpers_ms"]["svg_renderer_ms"] == 2.0
    diagnostics = data["extra"]["diagnostics"]
    assert diagnostics["quality_level"] == "empty"
    assert "text_mode_geometry" in diagnostics["signals"]
    assert diagnostics["recommended_actions"]


@pytest.mark.parametrize(
    "text_mode",
    ["labels", "3d_text", "glyphs", "geometry"],
)
def test_all_text_modes_round_trip(text_mode: str):
    report = build_import_report(
        host_app="librecad",
        pdf_path="drawing.pdf",
        text_mode=text_mode,
        import_text=text_mode != "geometry",
    )
    assert report.to_dict()["extra"]["text_mode"] == text_mode


def test_import_report_diagnostics_for_fallback_and_dense_text():
    report = build_import_report(
        host_app="freecad",
        pdf_path="scan.pdf",
        mode="auto",
        primitive_count=0,
        text_count=0,
        layer_count=0,
        warnings=2,
        fallback_used=True,
        fallback_reason="raster_fallback_1_pages",
        import_text=True,
        text_mode="glyphs",
        text_source_spans=14,
        text_glyph_estimate=1200,
    )
    diagnostics = report.to_dict()["extra"]["diagnostics"]
    assert diagnostics["quality_level"] == "empty"
    assert "fallback_used" in diagnostics["signals"]
    assert "warnings_present" in diagnostics["signals"]
    assert "source_text_seen_but_no_text_entities_created" in diagnostics["signals"]
    assert "dense_text_glyph_workload" in diagnostics["signals"]
    assert not any(
        "Vector or Hybrid" in action
        or "another text mode" in action.lower()
        or "Outlines" in action
        for action in diagnostics["recommended_actions"]
    )
    assert any(
        "requested representation unchanged" in action
        for action in diagnostics["recommended_actions"]
    )


def test_text_mode_fallback_block_carries_requested_delivered_reason():
    """TEXTMODE-1 lock: a text-mode substitution is loud, never silent."""
    report = build_import_report(
        host_app="freecad",
        pdf_path="sample.pdf",
        mode="auto",
        import_text=True,
        text_mode="3d_text",
        text_fallback={
            "requested": "3d_text",
            "delivered": "labels",
            "reason": "shapestring_failed",
            "count": 3,
        },
    )
    data = report.to_dict()
    fallback = data["fallback"]
    assert fallback["used"] is True
    assert fallback["text"] == {
        "requested": "3d_text",
        "delivered": "labels",
        "reason": "shapestring_failed",
        "count": 3,
    }
    assert "text_mode_fallback" in fallback["reason"]
    diagnostics = data["extra"]["diagnostics"]
    assert "text_mode_fallback" in diagnostics["signals"]
    assert any("'3d_text'" in action and "'labels'" in action
               for action in diagnostics["recommended_actions"])


def test_text_mode_fallback_keeps_host_fallback_reason():
    """A raster fallback reason is not clobbered by the text block."""
    report = build_import_report(
        host_app="freecad",
        pdf_path="sample.pdf",
        fallback_used=True,
        fallback_reason="scanned/raster page",
        text_fallback={
            "requested": "glyphs",
            "delivered": "labels",
            "reason": "svg_renderer_failed",
            "count": 2,
        },
    )
    fallback = report.to_dict()["fallback"]
    assert fallback["used"] is True
    assert fallback["reason"] == "scanned/raster page"
    assert fallback["text"]["requested"] == "glyphs"
    assert fallback["text"]["delivered"] == "labels"
    assert fallback["text"]["reason"] == "svg_renderer_failed"


def test_requested_equals_delivered_emits_no_fallback_text():
    """No substitution -> no fallback.text block and no signal."""
    report = build_import_report(
        host_app="freecad",
        pdf_path="sample.pdf",
        import_text=True,
        text_mode="labels",
        text_fallback={
            "requested": "labels",
            "delivered": "labels",
            "reason": "not_a_substitution",
        },
    )
    data = report.to_dict()
    assert data["fallback"] == {"used": False, "reason": None}
    assert "text_mode_fallback" not in data["extra"]["diagnostics"]["signals"]


def test_build_text_mode_fallback_normalizes_and_rejects_non_substitutions():
    assert build_text_mode_fallback(
        requested=" 3D_Text ", delivered="Labels", reason="", count="4"
    ) == {
        "requested": "3d_text",
        "delivered": "labels",
        "reason": "unspecified",
        "count": 4,
    }
    assert build_text_mode_fallback(requested="labels", delivered="labels", reason="x") is None
    assert build_text_mode_fallback(requested="", delivered="labels", reason="x") is None


def test_actual_text_entity_types_accepts_delivered_counts():
    """TEXTMODE-1: buckets reflect DELIVERED entities when the host reports them."""
    payload = build_actual_text_entity_types(
        host_app="freecad",
        text_mode="mixed",
        count=0,
        delivered_counts={
            "native_text": 363,
            "native_label": 6,
            "native_3d_text": 1,
            "raster_text_patch": 2,
        },
    )
    assert payload["native_text"] == 363
    assert payload["native_3d_text"] == 1
    assert payload["native_label"] == 6
    assert payload["raster_text_patch"] == 2
    assert payload["count"] == 372
    assert payload["entity_type"] == "mixed"


def test_actual_text_entity_types_defaults_to_requested_mode_derivation():
    """Backward compatible: without delivered info the mode string derives buckets."""
    payload = build_actual_text_entity_types(
        host_app="freecad",
        text_mode="3d_text",
        count=5,
    )
    assert payload["native_3d_text"] == 5
    assert payload["native_text"] == 0
    assert payload["native_label"] == 0
    assert payload["raster_text_patch"] == 0

    empty_delivered = build_actual_text_entity_types(
        host_app="freecad",
        text_mode="labels",
        count=4,
        delivered_counts={},
    )
    assert empty_delivered["native_label"] == 0
    assert empty_delivered["count"] == 0
    assert empty_delivered["entity_type"] == "none"

    native_text = build_actual_text_entity_types(
        host_app="freecad",
        text_mode="text",
        count=3,
    )
    assert native_text["native_text"] == 3
    assert native_text["font_rendered"] is True

    raster_text = build_actual_text_entity_types(
        host_app="freecad",
        text_mode="raster",
        count=2,
    )
    assert raster_text["raster_text_patch"] == 2
    assert raster_text["font_rendered"] is False


def test_font_embedding_hints_uses_extension_not_referencer():
    class Page:
        def get_fonts(self, full=True):
            assert full is True
            return [
                (8, "otf", "Type0", "AAAAAA+EmbeddedFont", "F0", "Identity-H", 0),
                (9, "n/a", "Type1", "Helvetica-Bold", "F1", "", 0),
            ]

    class Doc:
        def __len__(self):
            return 1

        def __getitem__(self, index):
            assert index == 0
            return Page()

    hints = build_font_embedding_hints(Doc())
    assert hints["non_embedded_fonts"] == ["Helvetica-Bold"]
    assert "parent-native font substitution" in hints["font_substitution_note"]
    assert "source-font non-equivalent" in hints["font_substitution_note"]


def test_build_import_report_always_emits_fallback_transitions_list():
    report = build_import_report(
        host_app="freecad",
        pdf_path="sample.pdf",
        primitive_count=1840,
        import_text=True,
        text_mode="text",
    )
    extra = report.to_dict()["extra"]
    assert extra["fallback_transitions"] == []
    assert extra["text_mode"] == "text"


def test_build_fallback_transitions_expands_item_scoped_text_mode_fallbacks():
    transitions = build_fallback_transitions(
        {
            "text_mode_fallbacks": [
                {
                    "requested": "text",
                    "delivered": "3d_text",
                    "reason": "host_representation_unsupported",
                    "count": 1,
                    "source_item_ids": ["p1:b0:l0:s0"],
                    "proof": {
                        "item_specific_proven_impossible": True,
                        "page_number": 1,
                        "importer_identity": "bluecollarsystems.freecad.pdf_vector_importer",
                        "cleanup_complete": True,
                    },
                }
            ]
        }
    )
    assert transitions == [
        {
            "source_span_id": "p1:b0:l0:s0",
            "from_mode": "text",
            "to_mode": "3d_text",
            "reason_code": "host_representation_unsupported",
            "page_number": 1,
            "page": 1,
            "importer_id": "bluecollarsystems.freecad.pdf_vector_importer",
            "affirmative_impossibility": True,
            "generic_failure": False,
            "cleanup_outcome": "verified",
        }
    ]


def test_build_fallback_transitions_does_not_invent_unproven_delivery_divergence():
    transitions = build_fallback_transitions(
        {
            "text_delivery": {
                "items": [
                    {
                        "source_span_id": "p1:b0:l0:s1",
                        "requested_representation": "text",
                        "final_representation": "glyphs",
                        "fallback_used": False,
                        "verified": True,
                    }
                ]
            }
        }
    )
    assert transitions == []


def test_build_fallback_transitions_keeps_an_explicit_empty_ledger():
    assert build_fallback_transitions({"fallback_transitions": []}) == []


def test_pdf_interactive_note_ignores_null_catalog_keys():
    class Doc:
        def pdf_catalog(self):
            return 1

        def xref_get_key(self, xref, key):
            return ("null", "null")

        def xref_length(self):
            return 2

    assert build_pdf_interactive_note(Doc()) == {}


def test_pdf_interactive_note_detects_javascript_action():
    class Doc:
        def pdf_catalog(self):
            return 1

        def xref_get_key(self, xref, key):
            if xref == 2 and key == "S":
                return ("name", "/JavaScript")
            return ("null", "null")

        def xref_length(self):
            return 3

    note = build_pdf_interactive_note(Doc())
    assert note["pdf_interactive_flags"] == ["JavaScript"]
    assert "scripts are not executed" in note["pdf_interactive_note"]
