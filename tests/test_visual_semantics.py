# -*- coding: utf-8 -*-
"""Phase 2 increment 1: page source semantics profile, observe-only.

Two things these tests exist to prevent.

First, honesty about what the profiler is. The design's closed-world contract says a
page may only route native when EVERY invoked occurrence is accounted for. This
increment does not implement the full operator lexer, so it must never emit
``closed_world: true`` or a ``complete`` scan status -- otherwise a later consumer
would read partial feature detection as a completeness proof, which is precisely
the false-confidence bug the whole design exists to kill.

Second, the re-tuned limits. The original limits forced ~4% of real corpus pages to
recovery for resource reasons alone, with the resolved-object cap exceeded 7.1x on
ordinary map sheets. The caps here are the measured replacements, and they are
asserted so a future edit cannot quietly reintroduce the squeeze.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

from PDFVectorImporter.pdfcadcore.visual_semantics import (  # noqa: E402
    ACCOUNTING_FEATURE_DETECTION,
    CAP_ANNOTATIONS,
    CAP_DECODED_BYTES,
    CAP_FORM_DEPTH,
    CAP_OPERATOR_TOKENS,
    CAP_RESOLVED_OBJECTS,
    SCHEMA,
    VISIBLE_FEATURE_CODES,
    canonical_profile_json,
    new_profile,
    profile_is_native_eligible,
    record_limit_breaches,
    validate_profile,
)


class TestRetunedLimits(unittest.TestCase):
    """The measured caps. Changing these changes how often the product rasterizes."""

    def test_resolved_object_cap_is_the_retuned_value(self) -> None:
        # Measured max across 270 corpus pages was 904 (recursive, depth 8).
        # 128 was exceeded 7.1x by ordinary map sheets.
        self.assertEqual(4096, CAP_RESOLVED_OBJECTS)
        self.assertGreater(CAP_RESOLVED_OBJECTS, 904 * 4)

    def test_decoded_byte_cap_has_real_headroom(self) -> None:
        # Measured max 7,329,808 -- the old 8 MiB cap left only 12.6% margin.
        self.assertEqual(16 * 1024 * 1024, CAP_DECODED_BYTES)
        self.assertGreater(CAP_DECODED_BYTES, 7_329_808 * 2)

    def test_token_and_annotation_caps_are_unchanged(self) -> None:
        # Measured max 753,271 tokens (operands excluded) and 1 annotation:
        # both caps were correctly sized, so they must NOT drift.
        self.assertEqual(1_000_000, CAP_OPERATOR_TOKENS)
        self.assertEqual(2048, CAP_ANNOTATIONS)
        self.assertEqual(8, CAP_FORM_DEPTH)


class TestProfileHonesty(unittest.TestCase):
    """The profiler must not overstate what it knows."""

    def test_schema_is_versioned(self) -> None:
        self.assertEqual("bcs.page_source_semantics/2.0", SCHEMA)
        self.assertEqual(SCHEMA, new_profile()["schema"])

    def test_accounting_mode_is_feature_detection_not_closed_world(self) -> None:
        p = new_profile()
        self.assertEqual(ACCOUNTING_FEATURE_DETECTION, p["accounting_mode"])
        self.assertFalse(p["closed_world"])

    def test_scan_status_can_never_be_complete_in_this_increment(self) -> None:
        p = new_profile(scan_status="complete")
        self.assertTrue(
            any("complete" in v for v in validate_profile(p)),
            "a feature-detection profile must not claim a complete closed-world scan",
        )

    def test_closed_world_claim_is_rejected(self) -> None:
        p = new_profile()
        p["accounting_mode"] = "closed_world"
        self.assertTrue(any("closed_world" in v for v in validate_profile(p)))

    def test_partial_profile_is_never_native_eligible(self) -> None:
        """Fail closed: without closed-world accounting nothing may route native."""
        p = new_profile(scan_status="partial", visible_feature_codes=[])
        self.assertFalse(
            profile_is_native_eligible(p),
            "absence of detected features is not proof of completeness",
        )

    def test_observe_only_carries_no_routing_decision(self) -> None:
        p = new_profile()
        for banned in ("effective_strategy", "terminal_raster", "delivery_scope"):
            self.assertNotIn(banned, p, "the source profile must not decide routing")

    def test_routing_field_smuggled_in_is_rejected(self) -> None:
        p = new_profile()
        p["effective_strategy"] = "native"
        self.assertTrue(any("effective_strategy" in v for v in validate_profile(p)))


class TestProfileShape(unittest.TestCase):
    def test_feature_codes_match_the_design_vocabulary(self) -> None:
        for code in ("pdf_shading_paint", "pdf_soft_mask_composite",
                     "pdf_non_normal_blend", "pdf_transparency_group",
                     "pdf_visible_annotation_appearance", "pdf_type3_glyph_program"):
            self.assertIn(code, VISIBLE_FEATURE_CODES)

    def test_unknown_feature_code_is_rejected(self) -> None:
        p = new_profile(visible_feature_codes=["pdf_not_a_real_code"])
        self.assertTrue(any("pdf_not_a_real_code" in v for v in validate_profile(p)))

    def test_feature_codes_are_sorted_and_unique(self) -> None:
        p = new_profile(visible_feature_codes=["pdf_shading_paint", "pdf_shading_paint",
                                               "pdf_non_normal_blend"])
        self.assertEqual(["pdf_non_normal_blend", "pdf_shading_paint"],
                         p["visible_feature_codes"])

    def test_canonical_json_is_stable_and_lf(self) -> None:
        a = canonical_profile_json(new_profile(page_index=3))
        b = canonical_profile_json(new_profile(page_index=3))
        self.assertEqual(a, b)
        self.assertNotIn("\r", a)

    def test_legal_profile_validates_clean(self) -> None:
        self.assertEqual([], validate_profile(new_profile()))
        self.assertEqual([], validate_profile(
            new_profile(visible_feature_codes=["pdf_shading_paint"])))


class TestReasonCodeSplit(unittest.TestCase):
    """A budget breach must be distinguishable from a semantic one, or reports
    conflate 'could not afford to look' with 'this page needs recovery'."""

    def test_limit_breach_is_recorded_as_a_resource_reason(self) -> None:
        p = new_profile(scan_status="incomplete", status_code="resource_budget_incomplete")
        self.assertEqual([], validate_profile(p))
        self.assertNotEqual(p["status_code"], "semantic_scan_incomplete")

    def test_record_limit_breaches_flags_the_right_axis(self) -> None:
        p = new_profile(resolved_objects=CAP_RESOLVED_OBJECTS + 1)
        record_limit_breaches(p)
        self.assertEqual(["resolved_objects"], p["limit_breaches"])
        self.assertEqual("resource_budget_incomplete", p["status_code"])
        self.assertEqual("incomplete", p["scan_status"])
        self.assertEqual([], validate_profile(p))

    def test_within_budget_page_is_not_flagged(self) -> None:
        # The measured corpus maxima must all sit inside the re-tuned caps.
        p = new_profile(resolved_objects=904, decoded_bytes=7_329_808,
                        operator_tokens=753_271, annotation_entries=1)
        record_limit_breaches(p)
        self.assertEqual([], p["limit_breaches"],
                         "the re-tuned caps must clear every measured corpus page")
        self.assertEqual("partial", p["scan_status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
