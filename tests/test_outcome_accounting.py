# -*- coding: utf-8 -*-
"""Auto outcome accounting: the six binding laws must be mechanically enforced.

These tests exist because the estate has already shipped the failure they guard
against: 278 corpus cells recorded PASS on a return code with no delivery
evidence. An axis schema that is merely *documented* repeats that -- a roll-up
somewhere will quietly coerce "we produced a picture" into "we delivered the
representation you asked for". So every law is a test with an adversarial
negative case, and the validator has to reject the coercion, not just describe
it.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_DIR))

from PDFVectorImporter.pdfcadcore.outcome_accounting import (  # noqa: E402
    EDITABLE_REPRESENTATIONS,
    SCHEMA,
    canonical_json,
    completion_class,
    derive_cell_status,
    new_outcome,
    validate_outcome,
)


def _recovery_outcome(**overrides):
    """A legal recovery-only page: picture certified, requested text NOT delivered."""
    record = new_outcome(
        requested_page_strategy="auto",
        effective_page_strategy="visual_recovery",
        requested_representation="3d_text",
        structural_status="not_certified",
        visual_status="pass",
        requested_representation_status="fail",
        cell_status="fail",
        visual_recovery="certified",
        native_peer_count=0,
        visual_proof_digest="a" * 64,
    )
    record.update(overrides)
    return record


def _native_outcome(**overrides):
    record = new_outcome(
        requested_page_strategy="auto",
        effective_page_strategy="native",
        requested_representation="3d_text",
        structural_status="pass",
        visual_status="pass",
        requested_representation_status="pass",
        cell_status="pass",
        visual_recovery="absent",
        native_peer_count=42,
        visual_proof_digest="b" * 64,
    )
    record.update(overrides)
    return record


class TestSchemaAndShape(unittest.TestCase):
    def test_schema_is_versioned(self) -> None:
        self.assertEqual(SCHEMA, "bcs.auto_outcome_accounting/1.0")
        self.assertEqual(_native_outcome()["schema"], SCHEMA)

    def test_legal_records_validate_clean(self) -> None:
        self.assertEqual([], validate_outcome(_native_outcome()))
        self.assertEqual([], validate_outcome(_recovery_outcome()))

    def test_unknown_axis_value_is_rejected(self) -> None:
        bad = _native_outcome(effective_page_strategy="raster")
        self.assertTrue(any("effective_page_strategy" in v for v in validate_outcome(bad)))

    def test_canonical_json_is_stable_and_sorted(self) -> None:
        first = canonical_json(_recovery_outcome())
        second = canonical_json(_recovery_outcome())
        self.assertEqual(first, second)
        self.assertLess(first.index('"cell_status"'), first.index('"visual_status"'))
        self.assertNotIn("\r", first)


class TestLaw2RecoveryIsNotDelivery(unittest.TestCase):
    """A recovery image must never populate delivery or satisfy the request."""

    def test_recovery_cannot_set_requested_representation_pass(self) -> None:
        bad = _recovery_outcome(requested_representation_status="pass")
        self.assertTrue(
            any("requested_representation_status" in v for v in validate_outcome(bad)),
            "a page picture must never satisfy a requested editable representation",
        )

    def test_recovery_cannot_carry_delivered_representation(self) -> None:
        bad = _recovery_outcome()
        bad["delivered_representation"] = "3d_text"
        self.assertTrue(any("delivered_representation" in v for v in validate_outcome(bad)))

    def test_recovery_cannot_carry_item_transition_edges(self) -> None:
        bad = _recovery_outcome()
        bad["item_transitions"] = [{"from": "3d_text", "to": "raster"}]
        self.assertTrue(any("item_transitions" in v for v in validate_outcome(bad)))


class TestLaw3RecoveryPageAxes(unittest.TestCase):
    def test_recovery_forces_structural_not_certified(self) -> None:
        bad = _recovery_outcome(structural_status="pass")
        self.assertTrue(any("structural_status" in v for v in validate_outcome(bad)))

    def test_editable_request_on_recovery_page_must_fail(self) -> None:
        for mode in EDITABLE_REPRESENTATIONS:
            bad = _recovery_outcome(
                requested_representation=mode,
                requested_representation_status="not_applicable",
            )
            self.assertTrue(
                any("not_applicable" in v for v in validate_outcome(bad)),
                "%s was requested, so the axis cannot be not_applicable" % mode,
            )

    def test_not_applicable_is_legal_only_when_nothing_was_requested(self) -> None:
        ok = _recovery_outcome(
            requested_representation="none",
            requested_representation_status="not_applicable",
        )
        self.assertEqual([], validate_outcome(ok))

    def test_visual_pass_requires_candidate_bound_proof(self) -> None:
        bad = _recovery_outcome(visual_proof_digest="")
        self.assertTrue(any("visual_proof_digest" in v for v in validate_outcome(bad)))


class TestLaw4CellStatusCannotBeCoerced(unittest.TestCase):
    """The 278-unearned-PASS guard: a certified picture is not a passing cell."""

    def test_certified_recovery_cannot_make_the_cell_pass(self) -> None:
        bad = _recovery_outcome(cell_status="pass")
        self.assertTrue(
            any("cell_status" in v for v in validate_outcome(bad)),
            "visual pass + certified recovery must not coerce a cell PASS",
        )

    def test_derive_cell_status_refuses_pass_on_recovery(self) -> None:
        self.assertEqual("fail", derive_cell_status(_recovery_outcome()))

    def test_derive_cell_status_passes_a_fully_delivered_native_page(self) -> None:
        self.assertEqual("pass", derive_cell_status(_native_outcome()))

    def test_cell_pass_requires_structural_pass(self) -> None:
        bad = _native_outcome(structural_status="not_certified")
        self.assertTrue(any("cell_status" in v for v in validate_outcome(bad)))

    def test_cell_pass_requires_visual_pass(self) -> None:
        bad = _native_outcome(visual_status="unproved")
        self.assertTrue(any("cell_status" in v for v in validate_outcome(bad)))


class TestLaw5RequestedRasterIsADifferentOutcome(unittest.TestCase):
    def test_recovery_label_is_illegal_when_raster_was_requested(self) -> None:
        bad = new_outcome(
            requested_page_strategy="auto",
            effective_page_strategy="visual_recovery",
            requested_representation="raster",
            structural_status="not_certified",
            visual_status="pass",
            requested_representation_status="fail",
            cell_status="fail",
            visual_recovery="certified",
            native_peer_count=0,
            visual_proof_digest="c" * 64,
        )
        self.assertTrue(
            any("raster" in v for v in validate_outcome(bad)),
            "requested Raster certifies through the Raster contract, not visual_recovery",
        )


class TestLaw6FailedRecoveryLeavesNoPeers(unittest.TestCase):
    def test_failed_recovery_cannot_report_visual_pass(self) -> None:
        bad = _recovery_outcome(visual_recovery="failed", visual_status="pass")
        self.assertTrue(any("visual_status" in v for v in validate_outcome(bad)))

    def test_recovery_page_cannot_retain_native_peers(self) -> None:
        bad = _recovery_outcome(native_peer_count=17)
        self.assertTrue(
            any("native_peer_count" in v for v in validate_outcome(bad)),
            "the zero-peer law is the whole point of recovery",
        )


class TestCompletionClass(unittest.TestCase):
    def test_recovery_wording_never_claims_delivery(self) -> None:
        text = completion_class(_recovery_outcome())
        self.assertEqual(
            "visual recovery created; requested representation not certified", text
        )
        self.assertNotIn("delivered", text)

    def test_native_wording_reports_delivery(self) -> None:
        self.assertIn("delivered", completion_class(_native_outcome()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
