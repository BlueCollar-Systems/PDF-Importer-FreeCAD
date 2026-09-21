"""Review fixes for the text-item degrade: reachability, honesty, resume.

Three independent reviews attacked the degrade landed on this branch. What
they found, and what each test here pins:

* A page-scoped setup step that raises inside a deliverer - the source
  character read and the embedded-font staging - still cost the whole
  document, because the ladder synthesized a cleanup record it could never
  satisfy. Neither step owns a host object, so it now says so exactly and the
  item walks the ladder.
* A page whose text degraded was recorded as a certified completed page, so
  resuming the session re-certified the document. The degrade is now persisted
  with the session and repeated on every later invocation.
* Honesty polish: the console line count, the note channel, the host font map,
  the fallback flag, the "mixed" entity type on an empty page, and a degrade
  recorded without counted source spans.

Every fixture is synthetic, deterministic and fictional.
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


def _verified(item, mode, entity_id, evidence=None):
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
        "evidence": dict(evidence or {"host_entity_verified": True}),
    }


def _render(opts, items, doc=None):
    return core._render_canonical_text_items(
        pdf_doc=SimpleNamespace(),
        page=SimpleNamespace(),
        pdf_path="D042.pdf",
        page_num=1,
        page_h=100.0,
        page_w=100.0,
        scale=1.0,
        fc_doc=doc if doc is not None else SimpleNamespace(),
        parent_group=SimpleNamespace(),
        opts=opts,
        pdf_sha256=PDF_SHA256,
        raw_tdict={"blocks": []},
    )


def _install_items(monkeypatch, items):
    monkeypatch.setattr(
        core, "_iter_text_source_items", lambda *_args: iter(list(items))
    )


def _write_report(tmp_path, opts, entity_info=None, *, text_count=0):
    if entity_info is not None:
        opts._report_extra["actual_text_entity_types"] = dict(entity_info)
    report_path = tmp_path / "import_report.json"
    core.write_import_report(
        pdf_path=str(tmp_path / "D042.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=int(text_count),
        elapsed_ms=1.0,
    )
    return json.loads(report_path.read_text(encoding="utf-8"))


def _degraded_entry(index=0, page_number=1, delivered=False):
    return {
        "source_item_id": "p%d:b0:l%d:s0" % (page_number, index),
        "page_number": page_number,
        "source_text": "EX%03d" % index,
        "requested_type": "text",
        "attempted_types": ["text", "raster"],
        "rung_outcomes": [],
        "proof_class": "unproven_failure",
        "final_representation": "raster" if delivered else None,
        "delivered": delivered,
    }


# ── Finding 1: a page-scoped setup failure costs its items, not the sheet ──


def test_page_setup_failure_is_an_exact_attempt_that_owns_no_host_object():
    item = _source_item("p1:b0:l0:s0", "3d_text")
    failure = core._page_text_setup_failure(
        item,
        "3d_text",
        "3d_text",
        "page_shapestring_fonts",
        OSError("font cache verification failed after atomic write"),
    )

    # The classification is exactly what this failure has always produced.
    assert failure.attempt["reason"] == "generic_exception:OSError"
    assert failure.attempt["outcome"] == "failed"
    assert failure.attempt["attempted_type"] == "3d_text"
    # And it states, exactly, that it left nothing behind - which is the only
    # thing that lets the ladder walk past it.
    assert failure.attempt["created_entity_ids"] == []
    assert failure.attempt["removed_entity_ids"] == []
    assert failure.attempt["cleanup_complete"] is True
    assert failure.attempt["evidence"]["stage"] == "page_shapestring_fonts"
    assert "OSError" in failure.attempt["evidence"]["exception"]


def test_font_staging_failure_degrades_its_items_instead_of_the_document(
    monkeypatch,
):
    """A locked or unwritable font cache must not empty the sheet."""
    items = [
        _source_item("p1:b0:l0:s0", "3d_text", "EX001"),
        _source_item("p1:b0:l1:s0", "3d_text", "EX002"),
    ]
    _install_items(monkeypatch, items)
    opts = core.ImportOptions(text_mode="3d_text", import_text=True)
    monkeypatch.setattr(core, "_warn", lambda *_a: None)
    monkeypatch.setattr(core, "_prepare_native_text_object_index", lambda *_a, **_k: {})

    staging_calls = []

    def locked_font_cache(*_args, **_kwargs):
        staging_calls.append(1)
        raise OSError("font cache verification failed after atomic write")

    monkeypatch.setattr(core, "_stage_page_shapestring_fonts", locked_font_cache)
    monkeypatch.setattr(
        core,
        "_deliver_text_item_svg",
        lambda item, attempted, _opts, **_kwargs: _verified(
            item, attempted, "Outline_" + item["source_item_id"][-1]
        ),
    )

    result = _render(opts, items)

    # Both items are drawn, at the outline rung, and the sheet survives.
    assert result["count"] == 2
    assert result["entity_type"] == "glyphs"
    block = opts._report_extra["text_items_degraded"]
    assert block["total"] == 2
    assert block["delivered_at_lower_rung"] == 2
    assert [entry["final_representation"] for entry in block["items"]] == [
        "glyphs",
        "glyphs",
    ]
    # The failing page-scoped step is probed once per page, not once per span.
    assert len(staging_calls) == 1
    staged = [
        attempt
        for attempt in opts.text_delivery_attempts
        if attempt.get("reason") == "generic_exception:OSError"
    ]
    assert len(staged) == 2
    assert all(attempt["cleanup_complete"] is True for attempt in staged)


def test_unreadable_source_character_geometry_degrades_its_items(monkeypatch):
    """A pure source read owns no host object, so it cannot cost the sheet."""
    items = [_source_item("p1:b0:l0:s0", "text", "EX001")]
    _install_items(monkeypatch, items)
    opts = core.ImportOptions(text_mode="text", import_text=True)
    monkeypatch.setattr(core, "_warn", lambda *_a: None)
    monkeypatch.setattr(core, "_prepare_native_text_object_index", lambda *_a, **_k: {})
    monkeypatch.setattr(core, "_stage_page_shapestring_fonts", lambda *_a, **_k: {})

    import PDFText3DLayout as layout

    def unreadable(_page):
        raise ValueError("source character geometry is unreadable")

    monkeypatch.setattr(layout, "read_source_character_geometry", unreadable)
    monkeypatch.setattr(
        core,
        "_deliver_text_item_3d",
        lambda item, attempted, _opts, **_kwargs: _verified(item, attempted, "Solid001"),
    )

    result = _render(opts, items)

    assert result["count"] == 1
    assert result["entity_type"] == "3d_text"
    block = opts._report_extra["text_items_degraded"]
    assert block["total"] == 1
    assert [outcome["attempted_type"] for outcome in block["items"][0]["rung_outcomes"]] == [
        "text",
        "labels",
    ]
    assert block["items"][0]["rung_outcomes"][0]["evidence"]["stage"] == (
        "source_character_geometry"
    )


def test_an_exception_that_escapes_a_builder_still_stops_the_document():
    """Unknown host state is still the document's problem, not one item's."""
    item = _source_item("p1:b0:l0:s0", "text")
    opts = core.ImportOptions(text_mode="text")
    calls = []

    def escaping(_item, mode, _opts):
        calls.append(mode)
        raise RuntimeError("synthetic host failure")

    def next_rung(_item, mode, _opts):
        calls.append(mode)
        return _verified(item, mode, "Text001")

    with pytest.raises(core.TextRepresentationFailure, match="without a validated"):
        core._run_text_item_fallback_ladder(
            item, "text", {"text": escaping, "labels": next_rung}, opts
        )

    assert calls == ["text"]
    assert opts.text_delivery_attempts[-1]["reason"] == "generic_exception:RuntimeError"
    assert opts.text_delivery_attempts[-1]["cleanup_complete"] is False


# ── Findings 2, 7, 13: a degraded page is never certified, ever again ──────


def test_session_object_persists_and_returns_degraded_pages():
    from PDFImportSession import (
        create_session_object,
        read_session_object,
        update_session_object,
    )

    from PDFImportSession import build_identity

    host = SimpleNamespace()
    host.addProperty = lambda _kind, name, _group: setattr(host, name, "")
    document = SimpleNamespace(addObject=lambda _kind, _name: host)
    identity = build_identity(
        source_sha256="a" * 64,
        source_name="D042.pdf",
        opts=core.ImportOptions(pages=[1, 2]),
        importer_version="4.0.0",
        requested_pages=[1, 2],
    )
    create_session_object(document, identity)

    assert read_session_object(host)["degraded_pages"] == []

    update_session_object(
        host,
        status="running",
        completed_pages=[1, 2],
        page_groups={1: "PDF_Page_1", 2: "PDF_Page_2"},
        degraded_pages=[2],
    )
    state = read_session_object(host)
    assert state["completed_pages"] == [1, 2]
    assert state["degraded_pages"] == [2]

    # Omitting the argument leaves the persisted record alone rather than
    # quietly certifying the page.
    update_session_object(
        host,
        status="complete",
        completed_pages=[1, 2],
        page_groups={1: "PDF_Page_1", 2: "PDF_Page_2"},
    )
    assert read_session_object(host)["degraded_pages"] == [2]


def test_a_previously_degraded_page_is_never_called_previously_certified():
    scope = core._representation_contract_scope(
        [1, 2],
        [1],
        [2],
        [2],
        [1, 2],
        previously_degraded_pages=[1],
    )

    assert scope["previously_certified_pages_excluded"] == []
    assert scope["previously_degraded_pages_excluded"] == [1]
    assert scope["uncertified_degraded_pages"] == [1]
    assert scope["coverage_status"] == "current_invocation_only"
    assert scope["complete_session_telemetry"] is False


def test_a_session_degrade_keeps_a_later_invocation_uncertified(tmp_path):
    """The resumed run degraded nothing itself - the session still did."""
    opts = core.ImportOptions(text_mode="text", import_text=True)
    opts._report_extra = {
        "text_source_spans": 2,
        "session_text_items_degraded": {
            "schema": "bcs.session_text_items_degraded/1.0",
            "scope": "session",
            "pages": [1],
            "note": "an earlier invocation left text items degraded",
        },
    }

    report = _write_report(tmp_path, opts, text_count=2)
    extra = report["extra"]

    assert "text_items_degraded" not in extra
    # And the resumed report does not read as a clean run.
    assert report["result"]["warnings"] == 1
    assert extra["text_representation_delivery"]["verified"] is False
    assert extra["text_representation_delivery"]["session_degraded_pages"] == [1]
    assert extra["import_contract_ready"]["checks"]["text_delivery"] is False
    assert extra["import_contract_ready"]["ready"] is False
    assert "page 1" in extra["text_degrade_note"]
    assert "not certified" in extra["human_summary"]


def test_a_session_degrade_closes_the_gate_even_with_no_text_of_its_own(tmp_path):
    """A resumed page with no text must not make the gate vacuous."""
    opts = core.ImportOptions(text_mode="text", import_text=True)
    opts._report_extra = {
        "session_text_items_degraded": {
            "schema": "bcs.session_text_items_degraded/1.0",
            "scope": "session",
            "pages": [1, 3],
            "note": "an earlier invocation left text items degraded",
        }
    }

    extra = _write_report(tmp_path, opts)["extra"]

    assert extra["text_source_spans"] == 2
    assert extra["text_representation_delivery"]["verified"] is False
    assert extra["import_contract_ready"]["ready"] is False


def test_the_qa_harness_carries_a_degraded_cell_into_every_later_cell():
    sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter" / "adapters"))
    import freecad_harness as harness

    digest = "b" * 64
    first = harness.plan_page_cell(
        [1, 2], page_budget=1, source_pdf_sha256=digest, checkpoint=None
    )
    assert first["active_pages"] == [1]
    first = harness.complete_page_cell(first, 3)
    assert first["status"] == "cell_degraded"
    assert first["degraded_text_items"] == 3
    assert first["degraded_pages"] == [1]

    second = harness.plan_page_cell(
        [1, 2], page_budget=1, source_pdf_sha256=digest, checkpoint=first
    )
    assert second["active_pages"] == [2]
    # The second cell degrades nothing of its own.
    second = harness.complete_page_cell(second, 0)
    assert second["run_complete"] is True
    assert second["degraded_text_items"] == 3
    assert second["degraded_pages"] == [1]
    assert harness.import_result_status(True, second["degraded_text_items"])[0] == (
        "DEGRADED"
    )
    assert harness.result_exit_code({"status": "DEGRADED"}) == 1

    # A clean run still passes, and a clean checkpoint carries no degrade.
    clean = harness.plan_page_cell(
        [1], page_budget=1, source_pdf_sha256=digest, checkpoint=None
    )
    clean = harness.complete_page_cell(clean, 0)
    assert clean["degraded_text_items"] == 0
    assert clean["status"] == "complete"
    assert harness.import_result_status(True, clean["degraded_text_items"])[0] == "PASS"


# ── Findings 3, 11: the console is bounded too ────────────────────────────


def test_per_item_console_lines_are_capped_and_the_rest_are_named(monkeypatch):
    limit = core.TEXT_ITEM_DEGRADE_CONSOLE_LIMIT
    total = limit + 5
    items = [_source_item("p1:b0:l%d:s0" % index, "text") for index in range(total)]
    _install_items(monkeypatch, items)
    opts = core.ImportOptions(text_mode="text", import_text=True)
    warnings = []
    monkeypatch.setattr(core, "_warn", warnings.append)
    monkeypatch.setattr(core, "_prepare_native_text_object_index", lambda *_a, **_k: {})

    def executor(item, requested, _deliverers, executor_opts):
        record = core._degraded_text_item_record(item, requested, ["text"], [])
        core._append_text_item_attempt(executor_opts, record)
        return record

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", executor)
    _render(opts, items)

    assert opts._report_extra["text_items_degraded"]["total"] == total
    assert len(warnings) == limit
    overflow = core._text_degrade_console_overflow_line(opts)
    assert overflow.startswith("PDF import: ... and 5 more text items")
    assert "text_items_degraded" in overflow

    clean = core.ImportOptions(text_mode="text")
    clean._report_extra = {}
    assert core._text_degrade_console_overflow_line(clean) == ""


# ── Finding 4: the degrade sentence has its own channel ───────────────────


def test_the_degrade_sentence_is_not_published_as_a_font_substitution(tmp_path):
    opts = core.ImportOptions(text_mode="text", import_text=True)
    opts._report_extra = {"text_source_spans": 2}
    core._record_degraded_text_item(opts, _degraded_entry())

    extra = _write_report(tmp_path, opts, text_count=1)["extra"]

    assert extra["host_font_substitutions"] == {}
    assert "could not be delivered" not in str(
        extra.get("font_substitution_note") or ""
    )
    assert "could not be delivered" in extra["text_degrade_note"]
    assert "not certified" in extra["human_summary"]


# ── Finding 6: nothing delivered is "none", not "mixed" ───────────────────


def test_a_page_where_everything_degraded_reports_no_delivered_type(monkeypatch):
    items = [_source_item("p1:b0:l%d:s0" % index, "text") for index in range(2)]
    _install_items(monkeypatch, items)
    opts = core.ImportOptions(text_mode="text", import_text=True)
    monkeypatch.setattr(core, "_warn", lambda *_a: None)
    monkeypatch.setattr(core, "_prepare_native_text_object_index", lambda *_a, **_k: {})

    def executor(item, requested, _deliverers, executor_opts):
        record = core._degraded_text_item_record(item, requested, ["text"], [])
        core._append_text_item_attempt(executor_opts, record)
        return record

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", executor)
    result = _render(opts, items)

    assert result["entity_type"] == "none"
    assert result["count"] == 0
    assert result["source_item_count"] == 2
    assert result["font_rendered"] is False


# ── Finding 9: a degraded item is not a delivery of the requested type ────


def test_a_degraded_delivery_stays_out_of_the_host_font_map(tmp_path):
    item = _source_item("p1:b0:l0:s0", "3d_text")
    opts = core.ImportOptions(text_mode="3d_text", import_text=True)
    opts._report_extra = {"text_source_spans": 2}
    clean = _verified(
        item,
        "3d_text",
        "Solid001",
        {"source_font": "Helvetica", "font_name": "Arial", "font_status": "substituted_family"},
    )
    degraded = _verified(
        dict(item, source_item_id="p1:b0:l1:s0"),
        "text",
        "Text001",
        {"source_font": "Courier", "font_name": "Arial", "font_status": "substituted_family"},
    )
    degraded["representation_degraded"] = True
    opts.text_delivery_attempts.extend([clean, degraded])
    core._record_degraded_text_item(opts, _degraded_entry(index=1, delivered=True))

    report = _write_report(tmp_path, opts, text_count=2)
    extra = report["extra"]

    assert sorted(extra["host_font_map"]) == ["Helvetica"]
    assert sorted(extra["host_font_substitutions"]) == ["Helvetica"]
    # One host-font warning, one degraded item - the degraded row contributes
    # the second, never both.
    assert report["result"]["warnings"] == 2


# ── Finding 12: the top-level fallback flag matches the degrade block ─────


def test_the_fallback_block_states_that_text_items_were_degraded(tmp_path):
    opts = core.ImportOptions(text_mode="text", import_text=True)
    opts._report_extra = {"text_source_spans": 3}
    core._record_degraded_text_item(opts, _degraded_entry(delivered=True))

    report = _write_report(tmp_path, opts, text_count=3)

    assert report["fallback"]["used"] is True
    assert report["fallback"]["reason"] == "text_items_degraded"
    # The proof-gated channel stays empty: that is what keeps
    # representation_contract_violation firing for an unproven degrade.
    assert report["extra"]["fallback_transitions"] == []
    assert report["extra"].get("text_mode_fallbacks") in (None, [])


def test_a_clean_sheet_still_reports_no_fallback(tmp_path):
    opts = core.ImportOptions(text_mode="text", import_text=True)
    opts._report_extra = {"text_source_spans": 3}

    report = _write_report(
        tmp_path,
        opts,
        {
            "entity_type": "text",
            "count": 3,
            "source_item_count": 3,
            "source_item_ids": ["p1:b0:l%d:s0" % index for index in range(3)],
            "font_rendered": True,
            "examples": [],
        },
        text_count=3,
    )

    assert report["fallback"]["used"] is False
    assert report["fallback"]["reason"] is None
    assert "text_degrade_note" not in report["extra"]
    assert report["extra"]["import_contract_ready"]["ready"] is True


# ── Finding 15: one index refresh, and only after a real rollback ─────────


def test_the_native_index_is_refreshed_only_for_a_rung_that_rolled_back(
    monkeypatch,
):
    item = _source_item("p1:b0:l0:s0", "text")
    refreshes = []
    monkeypatch.setattr(
        core,
        "_refresh_native_text_index_after_item_rollback",
        lambda _opts: refreshes.append(1),
    )

    def failure(mode, removed):
        return core.TextRepresentationFailure(
            "%s failed" % mode,
            {
                "source_item_id": item["source_item_id"],
                "requested_type": "text",
                "attempted_type": mode,
                "final_type": None,
                "outcome": "failed",
                "reason": "%s_synthetic_failure" % mode,
                "created_entity_ids": list(removed),
                "removed_entity_ids": list(removed),
                "cleanup_complete": True,
            },
        )

    def deliver(_item, mode, _opts):
        if mode == "text":
            # Built a host object and then removed it: FreeCAD will hand that
            # name straight to the next item.
            raise failure(mode, ["Text001"])
        if mode == "labels":
            raise failure(mode, [])
        return _verified(item, mode, "Solid001")

    opts = core.ImportOptions(text_mode="text")
    result = core._run_text_item_fallback_ladder(
        item,
        "text",
        {mode: deliver for mode in core.TEXT_ITEM_FALLBACK_LADDERS["text"]},
        opts,
    )

    assert result["final_type"] == "3d_text"
    assert len(refreshes) == 1


def test_the_per_item_call_site_does_not_rebuild_the_index_again(monkeypatch):
    items = [_source_item("p1:b0:l0:s0", "text")]
    _install_items(monkeypatch, items)
    opts = core.ImportOptions(text_mode="text", import_text=True)
    monkeypatch.setattr(core, "_warn", lambda *_a: None)
    prepares = []
    monkeypatch.setattr(
        core,
        "_prepare_native_text_object_index",
        lambda *_a, **kwargs: prepares.append(kwargs) or {},
    )

    def executor(item, requested, _deliverers, executor_opts):
        record = core._degraded_text_item_record(item, requested, ["text"], [])
        core._append_text_item_attempt(executor_opts, record)
        return record

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", executor)
    _render(opts, items)

    # Exactly one: the page-start build. The ladder owns the rollback refresh.
    assert len(prepares) == 1


# ── Finding 16: the gate can never be vacuous while a degrade is recorded ─


def test_a_degrade_with_no_counted_source_spans_still_fails_certification(tmp_path):
    opts = core.ImportOptions(text_mode="text", import_text=True)
    opts._report_extra = {}
    core._record_degraded_text_item(opts, _degraded_entry())
    core._record_degraded_text_item(opts, _degraded_entry(index=1))

    extra = _write_report(tmp_path, opts)["extra"]

    assert extra["text_source_spans"] == 2
    assert extra["text_representation_delivery"]["verified"] is False
    assert extra["import_contract_ready"]["ready"] is False


def test_a_counted_roster_is_never_inflated_by_the_degrade_it_carries(tmp_path):
    opts = core.ImportOptions(text_mode="text", import_text=True)
    opts._report_extra = {"text_source_spans": 9}
    core._record_degraded_text_item(opts, _degraded_entry())

    extra = _write_report(tmp_path, opts, text_count=8)["extra"]

    assert extra["text_source_spans"] == 9
    assert extra["text_representation_delivery"]["source_spans"] == 9
    assert extra["text_representation_delivery"]["degraded_items"] == 1
