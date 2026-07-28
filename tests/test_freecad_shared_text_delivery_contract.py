"""Shared text-delivery projection and independent obligation binding in FreeCAD."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore import import_report as report_contract  # noqa: E402
from test_freecad_report_inventory import _aws_fallback_opts  # noqa: E402


SUMMARY_FIELDS = {
    "schema",
    "required",
    "requested_type",
    "verified",
    "attempt_count",
    "source_item_count",
    "delivered_item_count",
    "failed_item_count",
    "items",
    "invalid_reasons",
}


def _label_attempt(source_item_id: str, sentinel: str = "") -> dict:
    evidence = {
        "host_entity_type": "App::FeaturePython",
        "host_proxy_type": "Label",
        "source_text": "LABEL",
        "source_text_preserved": True,
        "view_style_verified": True,
        "label_marker_absent": True,
    }
    if sentinel:
        evidence["sentinel"] = sentinel
    return {
        "source_item_id": source_item_id,
        "requested_type": "labels",
        "attempted_type": "labels",
        "final_type": "labels",
        "outcome": "verified",
        "created_entity_ids": ["Label001"],
        "delivery_entity_ids": ["Label001"],
        "support_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "attempted_types": ["labels"],
        "proof_chain": [],
        "evidence": evidence,
    }


def test_report_emits_shared_summary_one_strict_ledger_and_obligations(tmp_path) -> None:
    sentinel = "ONE_FREECAD_SHARED_LEDGER_SENTINEL"
    source_item_id = "p1:text:0"
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    opts.text_delivery_obligation_source_item_ids = [source_item_id]
    opts.text_delivery_attempts.append(_label_attempt(source_item_id, sentinel))
    opts.text_delivered_counts["native_label"] = 1
    output_path = tmp_path / "report.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "source.pdf"),
        output_path=str(output_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    serialized = output_path.read_text(encoding="utf-8")
    assert serialized.count(sentinel) == 1
    extra = json.loads(serialized)["extra"]
    assert extra["text_delivery_obligations"] == {
        "schema": "bcs.text_delivery_obligations/1.0",
        "required": True,
        "requested_type": "labels",
        "source_item_ids": [source_item_id],
    }
    summary = extra["text_representation_delivery"]
    assert set(summary) == SUMMARY_FIELDS
    assert summary["schema"] == "bcs.text_representation_delivery/1.1"
    assert summary["verified"] is True
    ledger = extra["text_delivery_attempts"]
    assert len(ledger) == 1
    assert all(
        field in ledger[0]
        for field in (
            "created_entity_ids",
            "removed_entity_ids",
            "delivery_entity_ids",
            "support_entity_ids",
            "referenced_entity_ids",
            "reused_entity_ids",
            "record_verified",
            "type_verified",
            "visual_verified",
            "ownership_verified",
        )
    )
    assert report_contract._freecad_delivery_terminal_attempts(summary, ledger) == ledger


def test_verified_zero_obligation_projection_is_the_only_empty_success(tmp_path) -> None:
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    opts.text_delivery_obligation_source_item_ids = []
    output_path = tmp_path / "blank-report.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "blank.pdf"),
        output_path=str(output_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
    )

    extra = json.loads(output_path.read_text(encoding="utf-8"))["extra"]
    assert extra["text_delivery_obligations"]["required"] is False
    assert extra["text_delivery_obligations"]["source_item_ids"] == []
    assert extra["text_representation_delivery"]["required"] is False
    assert extra["text_representation_delivery"]["verified"] is True
    assert extra["import_contract_ready"]["checks"]["text_delivery"] is True


def test_readiness_recomputes_ledger_and_binds_independent_obligations(tmp_path) -> None:
    source_item_id = "p1:text:0"
    opts = core.ImportOptions(import_text=True, text_mode="labels")
    opts.text_delivery_obligation_source_item_ids = [source_item_id]
    opts.text_delivery_attempts.append(_label_attempt(source_item_id))
    opts.text_delivered_counts["native_label"] = 1
    output_path = tmp_path / "report.json"
    core.write_import_report(
        pdf_path=str(tmp_path / "source.pdf"),
        output_path=str(output_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))

    payload["extra"]["text_delivery_attempts"][0]["outcome"] = "failed"
    report = report_contract.ImportReport.from_dict(payload)
    ready = report_contract.build_import_contract_ready(report)
    assert ready["checks"]["text_delivery"] is False
    assert ready["ready"] is False

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    payload["extra"]["text_delivery_obligations"]["source_item_ids"] = [
        "p1:text:different"
    ]
    report = report_contract.ImportReport.from_dict(payload)
    ready = report_contract.build_import_contract_ready(report)
    assert ready["checks"]["text_delivery"] is False
    assert ready["ready"] is False


def test_malformed_prior_array_cannot_be_sanitized_into_verified_delivery() -> None:
    opts = _aws_fallback_opts()
    opts.text_delivery_attempts[0]["referenced_entity_ids"] = [None]
    ledger = list(opts.text_delivery_attempts)

    delivery = core._build_text_representation_delivery(
        opts,
        ledger,
        expected_source_item_ids=["p1:page"],
        required=True,
    )

    assert delivery["verified"] is False
    assert delivery["items"][0]["verified"] is False
