"""One text item that cannot be delivered costs that item, not the document.

Owner directive 2026-09-19: the failure CLASSIFICATION each builder produces is
unchanged and stays in the report as evidence; only the CONSEQUENCE is
different. The degrade is loud and certification stays strict - a sheet with a
degraded item still fails ``import_contract_ready``.

Every fixture here is synthetic and deterministic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402


PDF_SHA256 = "d" * 64


def _span(text: str) -> dict:
    return {
        "text": text,
        "font": "ArialMT",
        "size": 12.0,
        "bbox": (10.0, 20.0, 18.0, 32.0),
        "origin": (10.0, 30.0),
        "color": 0,
    }


def _raw_tdict(texts) -> dict:
    return {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {"dir": (1.0, 0.0), "spans": [_span(text)]}
                    for text in texts
                ],
            }
        ]
    }


def _source_item(source_item_id: str, requested: str, text: str = "SAMPLE") -> dict:
    span = _span(text)
    return {
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": PDF_SHA256,
        "page_number": 1,
        "source_item_id": source_item_id,
        "requested_type": requested,
        "text": text,
        "font_identity": {"raw_name": "ArialMT", "normalized_key": "arialmt"},
        "bbox": span["bbox"],
        "origin": span["origin"],
        "line_direction": (1.0, 0.0),
        "rotation_deg": 0.0,
        "span": span,
        "block_index": 0,
        "line_index": 0,
        "span_index": 0,
    }


def _verified(item, mode, entity_id):
    return {
        "source_item_id": item["source_item_id"],
        "requested_type": item["requested_type"],
        "attempted_type": mode,
        "final_type": mode,
        "outcome": "verified",
        "created_entity_ids": [entity_id],
        "delivery_entity_ids": [entity_id],
        "support_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "evidence": {"host_entity_verified": True},
    }


def _rung_failure(item, mode, reason, *, cleanup_complete=True, evidence=None):
    return core.TextRepresentationFailure(
        "%s failed for %s: %s" % (mode, item["source_item_id"], reason),
        {
            "source_item_id": item["source_item_id"],
            "requested_type": item["requested_type"],
            "attempted_type": mode,
            "final_type": None,
            "outcome": "failed",
            "reason": reason,
            "created_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": cleanup_complete,
            "evidence": dict(evidence or {}),
        },
    )


def _render(opts, items, doc=None, page_num=1):
    """Drive the real per-item orchestrator over a fixed source roster."""
    return core._render_canonical_text_items(
        pdf_doc=SimpleNamespace(),
        page=SimpleNamespace(),
        pdf_path="D042.pdf",
        page_num=page_num,
        page_h=100.0,
        page_w=100.0,
        scale=1.0,
        fc_doc=doc if doc is not None else SimpleNamespace(),
        parent_group=SimpleNamespace(),
        opts=opts,
        pdf_sha256=PDF_SHA256,
        raw_tdict=_raw_tdict([item["text"] for item in items]),
    )


def _install_items(monkeypatch, items):
    monkeypatch.setattr(
        core, "_iter_text_source_items", lambda *_args: iter(list(items))
    )


def _write_report(tmp_path, opts, entity_info, *, text_count=None):
    opts._report_extra["actual_text_entity_types"] = dict(entity_info)
    report_path = tmp_path / "import_report.json"
    core.write_import_report(
        pdf_path=str(tmp_path / "D042.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=int(
            entity_info.get("count", 0) if text_count is None else text_count
        ),
        elapsed_ms=1.0,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


# ── The ladder ────────────────────────────────────────────────────────────


def test_exhausted_ladder_returns_a_degraded_record_instead_of_raising():
    item = _source_item("p1:b0:l0:s0", "3d_text")
    opts = core.ImportOptions(text_mode="3d_text")
    calls = []

    def always_fails(delivered_item, mode, _opts):
        calls.append(mode)
        raise _rung_failure(delivered_item, mode, "%s_synthetic_failure" % mode)

    result = core._run_text_item_fallback_ladder(
        item,
        "3d_text",
        {mode: always_fails for mode in core.TEXT_ITEM_FALLBACK_LADDERS["3d_text"]},
        opts,
    )

    # The SVG renderer is not replayed once a rolled-back attempt claimed it.
    assert calls == ["3d_text", "glyphs", "text", "labels", "raster"]
    assert result["outcome"] == "degraded"
    assert result["final_type"] is None
    assert result["verified"] is False
    assert result["proof_class"] == "unproven_failure"
    assert result["attempted_types"] == calls
    assert [entry["reason"] for entry in result["rung_outcomes"]] == [
        "3d_text_synthetic_failure",
        "glyphs_synthetic_failure",
        "svg_rung_not_replayed_after_rollback",
        "text_synthetic_failure",
        "labels_synthetic_failure",
        "raster_synthetic_failure",
    ]
    assert opts.text_delivery_attempts[-1] is not result
    assert opts.text_delivery_attempts[-1]["outcome"] == "degraded"


def test_degraded_record_reports_proven_impossible_when_every_rung_proved_it():
    item = _source_item("p1:b0:l0:s0", "glyphs")
    opts = core.ImportOptions(text_mode="glyphs")

    def proof_for(mode):
        return {
            "item_specific_proven_impossible": True,
            "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
            "pdf_sha256": PDF_SHA256,
            "page_number": 1,
            "source_item_id": item["source_item_id"],
            "requested_type": "glyphs",
            "attempted_type": mode,
            "reason_code": "svg_item_raster_source_only",
            "evidence": {"renderer": "synthetic"},
            "attempted_source_results": [
                {
                    "source": "svg_item_renderer",
                    "outcome": "proven_impossible",
                    "reason_code": "svg_item_raster_source_only",
                    "pdf_sha256": PDF_SHA256,
                    "page_number": 1,
                    "source_item_id": item["source_item_id"],
                }
            ],
            "attempted_sources_complete": True,
            "created_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
        }

    def deliver(delivered_item, mode, _opts):
        proof = proof_for(mode)
        raise core.TextItemImpossible(
            "proven impossible",
            attempt={
                "source_item_id": delivered_item["source_item_id"],
                "requested_type": "glyphs",
                "attempted_type": mode,
                "final_type": None,
                "outcome": "proven_impossible",
                "reason_code": proof["reason_code"],
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
            proof=proof,
        )

    def raster_fails(delivered_item, mode, _opts):
        raise _rung_failure(delivered_item, mode, "raster_text_render_failed")

    result = core._run_text_item_fallback_ladder(
        item,
        "glyphs",
        {"glyphs": deliver, "geometry": deliver, "raster": raster_fails},
        opts,
    )

    # Both outline rungs proved impossibility; the terminal raster crop then
    # failed without proof, so the item as a whole is an unproven failure.
    assert result["outcome"] == "degraded"
    assert result["proof_class"] == "unproven_failure"
    assert [entry["outcome"] for entry in result["rung_outcomes"]] == [
        "proven_impossible",
        "proven_impossible",
        "failed",
    ]

    # ``proven_impossible`` needs every attempted rung to have proved it. The
    # terminal raster rung cannot today - _validate_item_impossibility_proof
    # refuses a raster proof outright - so this classification is exercised
    # directly on the record builder.
    proven = core._degraded_text_item_record(
        item,
        "glyphs",
        ["glyphs", "geometry", "raster"],
        [
            core._text_item_rung_outcome(
                mode, "proven_impossible", "svg_item_raster_source_only"
            )
            for mode in ("glyphs", "geometry", "raster")
        ],
    )
    assert proven["proof_class"] == "proven_impossible"
    assert proven["verified"] is False


def test_cancellation_propagates_out_of_the_ladder_untouched():
    item = _source_item("p1:b0:l0:s0", "text")
    opts = core.ImportOptions(text_mode="text")

    def cancel(_item, _mode, _opts):
        raise core.ImportCancelled("PDF import cancelled by user")

    with pytest.raises(core.ImportCancelled):
        core._run_text_item_fallback_ladder(
            item, "text", {"text": cancel}, opts
        )


def test_programming_error_rungs_still_stop_the_document():
    item = _source_item("p1:b0:l0:s0", "text")
    opts = core.ImportOptions(text_mode="text")

    # A deliverer that is simply missing is an importer bug, not an item fault.
    with pytest.raises(core.TextRepresentationFailure, match="deliverer is unavailable"):
        core._run_text_item_fallback_ladder(item, "text", {}, opts)

    # So is a result the item contract cannot verify.
    opts = core.ImportOptions(text_mode="text")
    with pytest.raises(core.TextRepresentationFailure, match="unverified result"):
        core._run_text_item_fallback_ladder(
            item,
            "text",
            {"text": lambda _i, _m, _o: {"outcome": "created"}},
            opts,
        )


# ── The per-item call site ────────────────────────────────────────────────


def test_one_unrescuable_item_still_imports_every_other_item(monkeypatch):
    items = [
        _source_item("p1:b0:l0:s0", "text", "EX001"),
        _source_item("p1:b0:l1:s0", "text", "EX002"),
        _source_item("p1:b0:l2:s0", "text", "EX003"),
    ]
    _install_items(monkeypatch, items)
    opts = core.ImportOptions(text_mode="text", import_text=True)
    warnings = []
    monkeypatch.setattr(core, "_warn", warnings.append)

    def executor(item, requested, _deliverers, executor_opts):
        if item["source_item_id"] == "p1:b0:l1:s0":
            record = core._degraded_text_item_record(
                item,
                requested,
                ["text", "labels", "raster"],
                [
                    core._text_item_rung_outcome(
                        "text",
                        "failed",
                        "native_source_layout_failed",
                        {
                            "evidence": {
                                "source_character_index": 1,
                                "source_character_codepoint": "U+FE0F",
                                "font_name": "Arial",
                            }
                        },
                    ),
                    core._text_item_rung_outcome(
                        "raster", "failed", "raster_text_render_failed"
                    ),
                ],
            )
            core._append_text_item_attempt(executor_opts, record)
            return record
        return _verified(item, "text", "Text_" + item["source_item_id"][-1])

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", executor)

    result = _render(opts, items)

    assert result["count"] == 2
    assert opts.text_delivered_counts == {"native_text": 2}
    # The page's full source roster, degraded item included.
    assert result["source_item_ids"] == [
        "p1:b0:l0:s0",
        "p1:b0:l1:s0",
        "p1:b0:l2:s0",
    ]
    assert result["source_item_count"] == 3

    block = opts._report_extra["text_items_degraded"]
    assert block["total"] == 1
    assert block["dropped"] == 1
    assert block["delivered_at_lower_rung"] == 0
    assert block["pages"] == [1]
    entry = block["items"][0]
    assert entry["source_item_id"] == "p1:b0:l1:s0"
    assert entry["source_text"] == "EX002"
    assert entry["requested_type"] == "text"
    assert entry["delivered"] is False
    assert entry["font_identity"] == {
        "raw_name": "ArialMT",
        "normalized_key": "arialmt",
    }
    assert entry["rung_outcomes"][0]["evidence"] == {
        "source_character_index": 1,
        "source_character_codepoint": "U+FE0F",
        "font_name": "Arial",
    }
    assert len(warnings) == 1
    assert "p1:b0:l1:s0" in warnings[0]
    assert "nothing was drawn for it" in warnings[0]
    assert len(warnings[0]) <= 400


def test_item_delivered_at_a_lower_rung_without_proof_is_reported_as_degraded(
    monkeypatch,
):
    items = [_source_item("p1:b0:l0:s0", "3d_text", "MXT-100")]
    _install_items(monkeypatch, items)
    opts = core.ImportOptions(text_mode="3d_text", import_text=True)
    monkeypatch.setattr(core, "_warn", lambda *_a: None)

    def executor(item, requested, _deliverers, _opts):
        delivered = _verified(item, "text", "Text001")
        delivered["attempted_types"] = ["3d_text", "text"]
        delivered["representation_degraded"] = True
        delivered["proof_class"] = "unproven_failure"
        delivered["rung_outcomes"] = [
            core._text_item_rung_outcome(
                "3d_text",
                "failed",
                "positioned_3d_text_failed",
                {"evidence": {"font_path": "C:/fonts/subset.ttf", "stage": "compound"}},
            )
        ]
        return delivered

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", executor)

    result = _render(opts, items)

    # It was drawn, so it still counts as a delivery of what was drawn.
    assert result["count"] == 1
    assert opts.text_delivered_counts == {"native_text": 1}
    block = opts._report_extra["text_items_degraded"]
    assert block["total"] == 1
    assert block["delivered_at_lower_rung"] == 1
    assert block["dropped"] == 0
    assert block["items"][0]["final_representation"] == "text"
    assert block["items"][0]["rung_outcomes"][0]["evidence"]["font_path"] == (
        "C:/fonts/subset.ttf"
    )


def test_degraded_item_rollback_refreshes_the_native_text_object_index(monkeypatch):
    """One item-scoped rollback must not poison every later native item."""
    reused = SimpleNamespace(Name="Text001")
    document = SimpleNamespace(Objects=[reused])
    opts = core.ImportOptions(text_mode="text", import_text=True)
    # Stale entry for a host object name FreeCAD has already handed back out.
    opts._native_text_object_index = {
        "document": document,
        "object_ids": {id(object())},
        "entity_ids": {"Text001", "Text_stale"},
    }

    core._refresh_native_text_index_after_item_rollback(opts)

    index = opts._native_text_object_index
    assert index["entity_ids"] == {"Text001"}
    assert index["object_ids"] == {id(reused)}


def test_source_span_count_is_stated_for_every_text_import(monkeypatch):
    items = [_source_item("p1:b0:l0:s0", "text"), _source_item("p1:b0:l1:s0", "text")]
    _install_items(monkeypatch, items)
    opts = core.ImportOptions(text_mode="text", import_text=True)
    monkeypatch.setattr(
        core,
        "_run_text_item_fallback_ladder",
        lambda item, *_a: _verified(item, "text", "T" + item["source_item_id"][-1]),
    )

    _render(opts, items)

    assert opts._report_extra["text_source_spans"] == 2


# ── The report, the warning sum and the certification gate ────────────────


def _degraded_opts(tmp_path, *, degraded=1, text_mode="text"):
    opts = core.ImportOptions(text_mode=text_mode, import_text=True)
    opts._report_extra = {"text_source_spans": 3}
    for index in range(degraded):
        core._record_degraded_text_item(
            opts,
            {
                "source_item_id": "p1:b0:l%d:s0" % index,
                "page_number": 1,
                "source_text": "EX00%d" % index,
                "requested_type": text_mode,
                "attempted_types": [text_mode, "raster"],
                "rung_outcomes": [],
                "proof_class": "unproven_failure",
                "final_representation": None,
                "delivered": False,
            },
        )
    return opts


def test_degraded_sheet_is_not_import_contract_ready(tmp_path):
    opts = _degraded_opts(tmp_path)
    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "text",
            "count": 2,
            "source_item_count": 3,
            "source_item_ids": ["p1:b0:l0:s0", "p1:b0:l1:s0", "p1:b0:l2:s0"],
            "font_rendered": True,
            "examples": [],
        },
    )

    extra = report["extra"]
    assert extra["text_source_spans"] == 3
    assert extra["text_representation_delivery"]["required"] is True
    assert extra["text_representation_delivery"]["verified"] is False
    assert extra["text_representation_delivery"]["degraded_items"] == 1
    assert extra["import_contract_ready"]["checks"]["text_delivery"] is False
    assert extra["import_contract_ready"]["ready"] is False
    # The sheet imported: this is not a terminal failure.
    assert extra["result_status"] != "failed" if "result_status" in extra else True
    assert extra.get("terminal_failure") is None
    assert "text_items_degraded" in extra
    assert extra["text_items_degraded"]["total"] == 1
    # The degraded item stays in the page's roster.
    assert "p1:b0:l2:s0" in extra["actual_text_entity_types"]["source_item_ids"]
    assert report["result"]["warnings"] == 1
    assert "not certified" in extra["human_summary"]


def test_a_sheet_with_no_degraded_item_stays_ready_and_additive_only(tmp_path):
    opts = core.ImportOptions(text_mode="text", import_text=True)
    opts._report_extra = {"text_source_spans": 2}
    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "text",
            "count": 2,
            "source_item_count": 2,
            "source_item_ids": ["p1:b0:l0:s0", "p1:b0:l1:s0"],
            "font_rendered": True,
            "examples": [],
        },
    )

    extra = report["extra"]
    assert "text_items_degraded" not in extra
    assert extra["text_representation_delivery"] == {
        "required": True,
        "verified": True,
        "requested_type": "text",
        "source_spans": 2,
        "degraded_items": 0,
    }
    assert extra["import_contract_ready"]["ready"] is True
    assert report["result"]["warnings"] == 0
    assert "representation_contract_violation" not in extra


def test_report_shape_is_unchanged_apart_from_additive_keys(tmp_path):
    """A sheet that never degrades must be byte-identical bar the new keys."""

    def build(with_spans):
        opts = core.ImportOptions(text_mode="text", import_text=True)
        opts._report_extra = {"text_source_spans": 2} if with_spans else {}
        return _write_report(
            tmp_path,
            opts,
            {
                "entity_type": "text",
                "count": 2,
                "source_item_count": 2,
                "source_item_ids": ["p1:b0:l0:s0", "p1:b0:l1:s0"],
                "font_rendered": True,
                "examples": [],
            },
        )

    without = build(False)
    with_spans = build(True)
    added = set(with_spans["extra"]) - set(without["extra"])
    assert added == {"text_source_spans", "text_representation_delivery"}
    for key in without["extra"]:
        assert with_spans["extra"][key] == without["extra"][key]
    assert with_spans["result"] == without["result"]


def test_warning_count_is_a_three_way_sum(tmp_path):
    opts = _degraded_opts(tmp_path, degraded=2)
    opts._report_extra["clip_fill_delivery"] = dict(
        core._new_clip_fill_delivery(), dropped=3, approximated=1
    )
    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "text",
            "count": 1,
            "source_item_count": 3,
            "source_item_ids": ["p1:b0:l0:s0", "p1:b0:l1:s0", "p1:b0:l2:s0"],
            "font_rendered": True,
            "examples": [],
        },
    )

    # 4 clipped fills + 0 host-font substitutions + 2 degraded text items.
    assert report["result"]["warnings"] == 6


def test_degraded_item_list_is_capped_with_a_total_and_a_truncated_flag(tmp_path):
    opts = _degraded_opts(tmp_path, degraded=core.TEXT_ITEM_DEGRADE_REPORT_LIMIT + 5)
    block = opts._report_extra["text_items_degraded"]

    assert block["total"] == core.TEXT_ITEM_DEGRADE_REPORT_LIMIT + 5
    assert len(block["items"]) == core.TEXT_ITEM_DEGRADE_REPORT_LIMIT
    assert block["items_truncated"] is True

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "text",
            "count": 0,
            "source_item_count": 3,
            "source_item_ids": ["p1:b0:l0:s0"],
            "font_rendered": False,
            "examples": [],
        },
    )
    assert report["result"]["warnings"] == core.TEXT_ITEM_DEGRADE_REPORT_LIMIT + 5


def test_degraded_pages_are_named_as_uncertified_in_the_contract_scope():
    scope = core._representation_contract_scope(
        [1, 2], [], [1, 2], [1, 2], [1, 2], degraded_pages=[2]
    )
    assert scope["uncertified_degraded_pages"] == [2]
    assert scope["session_completed_pages"] == [1, 2]

    clean = core._representation_contract_scope([1, 2], [], [1, 2], [1, 2], [1, 2])
    assert "uncertified_degraded_pages" not in clean


# ── The QA harness ────────────────────────────────────────────────────────


def test_qa_harness_calls_a_degraded_sheet_degraded_not_pass():
    sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter" / "adapters"))
    import freecad_harness as harness

    assert harness.import_result_status(True, 0) == ("PASS", "Import completed.")
    status, message = harness.import_result_status(True, 7)
    assert status == "DEGRADED"
    assert "7 text item(s)" in message
    assert harness.result_exit_code({"status": status}) == 1
    assert harness.import_result_status(None, 0)[0] == "FAIL"

    opts = core.ImportOptions(text_mode="text")
    opts._report_extra = {"text_items_degraded": {"total": 4}}
    assert harness.degraded_text_item_count(opts) == 4
    assert harness.degraded_text_item_count(core.ImportOptions()) == 0
