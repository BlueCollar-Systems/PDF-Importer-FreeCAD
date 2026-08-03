from __future__ import annotations

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

import PDFImporterCore as core  # noqa: E402
from PDFVectorImporter.src import PDFImportSession as session  # noqa: E402


class HostObject:
    def __init__(self, document, name, type_id):
        self.Document = document
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.Group = [] if type_id == "App::DocumentObjectGroup" else None
        self.Placement = SimpleNamespace(Base=SimpleNamespace(y=0.0))
        self.PropertiesList = []

    def addProperty(self, _kind, name, _group):
        self.PropertiesList.append(name)

    def isDerivedFrom(self, type_id):
        return self.TypeId == type_id


class Document:
    def __init__(self):
        self.Objects = []
        self.commits = 0
        self.aborts = 0

    def addObject(self, kind, name):
        host = HostObject(self, f"{name}_{len(self.Objects)}", kind)
        self.Objects.append(host)
        return host

    def getObject(self, name):
        return next((host for host in self.Objects if host.Name == name), None)

    def removeObject(self, name):
        self.Objects = [host for host in self.Objects if host.Name != name]

    def openTransaction(self, _name):
        return None

    def commitTransaction(self):
        self.commits += 1

    def abortTransaction(self):
        self.aborts += 1

    def recompute(self, *_args):
        return None


class Page:
    rect = SimpleNamespace(height=100.0, width=80.0)

    def get_text(self, kind):
        if kind in {"dict", "rawdict"}:
            return {
                "blocks": [
                    {
                        "type": 0,
                        "lines": [{"spans": [{"text": "abcd"}]}],
                    }
                ]
            }
        return ""


class Pdf:
    def __init__(self, pages=2):
        self.pages = pages
        self.closed = False

    def __len__(self):
        return self.pages

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def load_page(self, _index):
        return Page()

    def close(self):
        self.closed = True


def _install_import_fakes(monkeypatch, document, page_importer, reports):
    from pdfcadcore import fitz_loader

    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: Pdf())
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_pdf_file_sha256", lambda _path: "a" * 64)
    monkeypatch.setattr(core, "_import_pdf_page_inner", page_importer)
    monkeypatch.setattr(core, "_create_semantic_model3d_members", lambda *_args: None)
    monkeypatch.setattr(core, "_finalize_import_recompute", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "_autofit_import_view", lambda *_args: None)

    def capture_report(**kwargs):
        reports.append(
            {
                "pages_imported": kwargs["pages_imported"],
                "extra": dict(kwargs["opts"]._report_extra),
                "text_mode_fallbacks": list(kwargs["opts"].text_mode_fallbacks),
                "text_delivered_counts": dict(
                    kwargs["opts"].text_delivered_counts
                ),
                "text_delivery_attempts": list(
                    kwargs["opts"].text_delivery_attempts
                ),
            }
        )
        return kwargs["output_path"]

    monkeypatch.setattr(core, "write_import_report", capture_report)


def test_progress_callback_receives_structured_event_and_cancels_truthfully():
    events = []
    opts = core.ImportOptions(progress_callback=lambda event: events.append(event) or False)
    opts._active_page_index = 2
    opts._active_page_total = 4

    with pytest.raises(core.ImportCancelled, match="cancelled by user"):
        core._emit_progress(
            opts,
            page_number=3,
            stage="text",
            label="Importing text 4/20",
            page_percent=42,
            completed_units=11,
            total_units=30,
        )

    assert events == [
        {
            "schema": "bcs.freecad_import_progress/1.0",
            "page_number": 3,
            "page_index": 2,
            "total_pages": 4,
            "stage": "text",
            "label": "Importing text 4/20",
            "page_percent": 42,
            "completed_units": 11,
            "total_units": 30,
        }
    ]


def test_work_estimate_uses_real_page_inventory_units(monkeypatch):
    from pdfcadcore import fitz_loader

    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: Pdf(pages=1))
    monkeypatch.setattr(
        core,
        "_page_visual_inventory",
        lambda _page, _mode: ([{"items": [1, 2, 3]}], 2),
    )
    plan = core.estimate_import_work(
        "fixture.pdf", core.ImportOptions(pages=[1], import_text=True)
    )

    assert plan == {
        "pages": [
            {
                "page_number": 1,
                "drawing_operations": 3,
                "text_characters": 4,
                "image_instances": 2,
                "total_units": 9,
                "risk": "normal",
            }
        ],
        "total_units": 9,
        "highest_risk": "normal",
    }


def test_long_text_loop_checks_cancellation_at_bounded_intervals(monkeypatch):
    items = [
        {
            "source_item_id": f"p1:b0:l0:s{index}",
            "page_number": 1,
            "pdf_sha256": "a" * 64,
            "bbox": (0.0, 0.0, 1.0, 1.0),
            "text": "x",
        }
        for index in range(51)
    ]
    delivered = []
    events = []

    monkeypatch.setattr(core, "_iter_text_source_items", lambda *_args: iter(items))
    monkeypatch.setattr(core, "_cache_canonical_text_metadata", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "_prepare_native_text_object_index", lambda *_args, **_kwargs: None)

    def deliver(item, *_args):
        delivered.append(item["source_item_id"])
        return {
            "source_item_id": item["source_item_id"],
            "final_type": "labels",
            "created_entity_ids": [item["source_item_id"]],
            "delivery_entity_ids": [item["source_item_id"]],
            "delivery_count": 1,
        }

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", deliver)

    def cancel_on_second_checkpoint(event):
        events.append(event)
        return len(events) < 2

    opts = core.ImportOptions(text_mode="labels", progress_callback=cancel_on_second_checkpoint)
    opts._active_page_index = 1
    opts._active_page_total = 1
    opts._active_page_profile = {
        "drawing_operations": 0,
        "text_characters": 51,
        "image_instances": 0,
        "total_units": 51,
    }

    with pytest.raises(core.ImportCancelled):
        core._render_canonical_text_items(
            pdf_doc=object(),
            page=SimpleNamespace(get_text=lambda _kind: {"blocks": []}),
            pdf_path="fixture.pdf",
            page_num=1,
            page_h=100.0,
            page_w=80.0,
            scale=1.0,
            fc_doc=object(),
            parent_group=object(),
            opts=opts,
            pdf_sha256="a" * 64,
            raw_tdict={"blocks": []},
        )

    assert len(delivered) == 25
    assert [event["label"] for event in events] == [
        "Importing text 0/51",
        "Importing text 25/51",
    ]


def test_cancel_commits_certified_pages_and_exact_resume_imports_only_remainder(
    monkeypatch, tmp_path
):
    document = Document()
    reports = []
    calls = []

    def first_import(_pdf, _path, page_number, page_opts, doc):
        calls.append(page_number)
        if page_number == 1:
            page_opts.text_delivered_counts["native_label"] = 1
            page_opts.text_delivery_attempts.append(
                {"source_item_id": "p1:kept", "outcome": "success"}
            )
            group = doc.addObject("App::DocumentObjectGroup", "PDF_Page_1")
            return group, {
                "entity_type": "labels",
                "count": 1,
                "source_item_count": 1,
                "source_item_ids": ["p1:kept"],
                "examples": [],
            }
        page_opts.text_delivered_counts["raster_fallback"] = 1
        page_opts.text_delivery_attempts.append(
            {"source_item_id": "p2:rolled-back", "outcome": "success"}
        )
        page_opts.text_mode_fallbacks.append(
            {
                "source_item_id": "p2:rolled-back",
                "requested_type": "labels",
                "delivered_type": "raster",
            }
        )
        page_opts._report_extra["active_page_only"] = "p2:rolled-back"
        doc.addObject("Part::Feature", "Partial_Page_2")
        raise core.ImportCancelled("PDF import cancelled by user")

    _install_import_fakes(monkeypatch, document, first_import, reports)
    opts = core.ImportOptions(
        pages=[1, 2],
        import_text=False,
        model3d_mode="off",
        import_report_path=str(tmp_path / "cancelled.json"),
    )

    assert core.import_pdf("fixture.pdf", opts) is False
    assert calls == [1, 2]
    assert opts.import_status == "cancelled"
    assert document.commits == 1
    assert document.aborts == 0
    assert all(not host.Name.startswith("Partial_Page_2") for host in document.Objects)

    session_host = next(
        host
        for host in document.Objects
        if getattr(host, "PDFImportSessionSchema", "") == session.SESSION_SCHEMA
    )
    cancelled = session.read_session_object(session_host)
    assert cancelled["completed_pages"] == [1]
    assert cancelled["status"] == "cancelled"
    assert reports[-1]["pages_imported"] == 1
    assert reports[-1]["extra"]["result_status"] == "cancelled"
    assert reports[-1]["extra"]["import_session"]["remaining_pages"] == [2]
    assert reports[-1]["text_delivered_counts"] == {"native_label": 1}
    assert reports[-1]["text_delivery_attempts"] == [
        {"source_item_id": "p1:kept", "outcome": "success"}
    ]
    assert reports[-1]["text_mode_fallbacks"] == []
    assert reports[-1]["extra"]["actual_text_entity_types"] == {
        "entity_type": "labels",
        "count": 1,
        "source_item_count": 1,
        "source_item_ids": ["p1:kept"],
        "examples": [],
    }
    assert "p2:rolled-back" not in repr(reports[-1])
    assert reports[-1]["extra"]["representation_contract_scope"] == {
        "schema": "bcs.representation_contract_scope/1.0",
        "scope": "current_invocation",
        "coverage_status": "full_session_to_date",
        "requested_pages": [1, 2],
        "evaluated_pages": [1, 2],
        "current_invocation_completed_pages": [1],
        "rolled_back_pages": [2],
        "previously_certified_pages_excluded": [],
        "session_completed_pages": [1],
        "complete_session_telemetry": True,
    }

    calls.clear()

    def resumed_import(_pdf, _path, page_number, opts, doc):
        calls.append(page_number)
        opts._scale_cached_pages.add(page_number)
        group = doc.addObject("App::DocumentObjectGroup", f"PDF_Page_{page_number}")
        return group, None

    monkeypatch.setattr(core, "_import_pdf_page_inner", resumed_import)
    resumed_opts = core.ImportOptions(
        pages=[1, 2],
        import_text=False,
        model3d_mode="off",
        import_report_path=str(tmp_path / "complete.json"),
        resume_session_name=session_host.Name,
    )

    assert core.import_pdf("fixture.pdf", resumed_opts) is True
    assert calls == [2]
    assert resumed_opts.import_status == "success"
    completed = session.read_session_object(session_host)
    assert completed["completed_pages"] == [1, 2]
    assert completed["status"] == "complete"
    assert reports[-1]["pages_imported"] == 2
    assert reports[-1]["extra"]["result_status"] == "success"
    assert reports[-1]["extra"]["representation_contract_scope"] == {
        "schema": "bcs.representation_contract_scope/1.0",
        "scope": "current_invocation",
        "coverage_status": "current_invocation_only",
        "requested_pages": [1, 2],
        "evaluated_pages": [2],
        "current_invocation_completed_pages": [2],
        "rolled_back_pages": [],
        "previously_certified_pages_excluded": [1],
        "session_completed_pages": [1, 2],
        "complete_session_telemetry": False,
    }


def test_terminal_failure_reports_fresh_invocation_pages_as_rolled_back(
    monkeypatch, tmp_path
):
    document = Document()
    reports = []

    def page_importer(_pdf, _path, page_number, _opts, doc):
        if page_number == 1:
            return doc.addObject("App::DocumentObjectGroup", "PDF_Page_1"), None
        raise core.TextRepresentationFailure(
            "terminal representation failure",
            {"page_number": page_number, "outcome": "failed"},
        )

    _install_import_fakes(monkeypatch, document, page_importer, reports)
    opts = core.ImportOptions(
        pages=[1, 2],
        import_text=False,
        model3d_mode="off",
        import_report_path=str(tmp_path / "failed.json"),
    )

    with pytest.raises(core.TextRepresentationFailure):
        core.import_pdf("fixture.pdf", opts)

    assert document.Objects == []
    assert reports[-1]["pages_imported"] == 0
    assert reports[-1]["extra"]["result_status"] == "failed"
    assert reports[-1]["extra"]["representation_contract_scope"] == {
        "schema": "bcs.representation_contract_scope/1.0",
        "scope": "current_invocation",
        "coverage_status": "full_session_to_date",
        "requested_pages": [1, 2],
        "evaluated_pages": [1, 2],
        "current_invocation_completed_pages": [],
        "rolled_back_pages": [1, 2],
        "previously_certified_pages_excluded": [],
        "session_completed_pages": [],
        "complete_session_telemetry": True,
    }


def test_terminal_failure_preserves_only_prior_certified_resume_pages(
    monkeypatch, tmp_path
):
    document = Document()
    reports = []

    def first_import(_pdf, _path, page_number, _opts, doc):
        if page_number == 1:
            return doc.addObject("App::DocumentObjectGroup", "PDF_Page_1"), None
        raise core.ImportCancelled("PDF import cancelled by user")

    _install_import_fakes(monkeypatch, document, first_import, reports)
    initial_opts = core.ImportOptions(
        pages=[1, 2],
        import_text=False,
        model3d_mode="off",
        import_report_path=str(tmp_path / "cancelled.json"),
    )
    assert core.import_pdf("fixture.pdf", initial_opts) is False
    session_host = next(
        host
        for host in document.Objects
        if getattr(host, "PDFImportSessionSchema", "") == session.SESSION_SCHEMA
    )

    def resumed_failure(_pdf, _path, page_number, _opts, _doc):
        raise core.TextRepresentationFailure(
            "terminal representation failure",
            {"page_number": page_number, "outcome": "failed"},
        )

    monkeypatch.setattr(core, "_import_pdf_page_inner", resumed_failure)
    resumed_opts = core.ImportOptions(
        pages=[1, 2],
        import_text=False,
        model3d_mode="off",
        import_report_path=str(tmp_path / "resume-failed.json"),
        resume_session_name=session_host.Name,
    )

    with pytest.raises(core.TextRepresentationFailure):
        core.import_pdf("fixture.pdf", resumed_opts)

    assert session.read_session_object(session_host)["completed_pages"] == [1]
    assert reports[-1]["pages_imported"] == 1
    assert reports[-1]["extra"]["result_status"] == "failed"
    assert reports[-1]["extra"]["representation_contract_scope"] == {
        "schema": "bcs.representation_contract_scope/1.0",
        "scope": "current_invocation",
        "coverage_status": "current_invocation_only",
        "requested_pages": [1, 2],
        "evaluated_pages": [2],
        "current_invocation_completed_pages": [],
        "rolled_back_pages": [2],
        "previously_certified_pages_excluded": [1],
        "session_completed_pages": [1],
        "complete_session_telemetry": False,
    }


def test_named_resume_fails_closed_when_identity_does_not_match(monkeypatch, tmp_path):
    document = Document()
    reports = []
    mismatched = document.addObject("App::FeaturePython", "PDF_Import_Session")
    identity = session.build_identity(
        source_sha256="b" * 64,
        source_name="other.pdf",
        opts=core.ImportOptions(pages=[1]),
        importer_version=core._importer_version(),
        requested_pages=[1],
    )
    created = session.create_session_object(document, identity)
    mismatched = created

    _install_import_fakes(
        monkeypatch,
        document,
        lambda *_args: pytest.fail("page import must not start"),
        reports,
    )
    opts = core.ImportOptions(
        pages=[1],
        import_text=False,
        import_report_path=str(tmp_path / "never.json"),
        resume_session_name=mismatched.Name,
    )

    with pytest.raises(ValueError, match="does not exactly match"):
        core.import_pdf("fixture.pdf", opts)


def test_committed_import_survives_snapshot_cleanup_failure_and_defers_retry(
    monkeypatch, tmp_path
):
    from PDFVectorImporter.src import PDFSvgTextRenderer as renderer

    document = Document()
    reports = []
    deferred = []

    def import_page(_pdf, _path, page_number, opts, doc):
        opts._svg_source_snapshot_cache.update(
            {"snapshot_path": str(tmp_path / "locked.pdf"), "pdf_sha256": "a" * 64}
        )
        return doc.addObject(
            "App::DocumentObjectGroup", f"PDF_Page_{page_number}"
        ), None

    _install_import_fakes(monkeypatch, document, import_page, reports)
    monkeypatch.setattr(
        renderer,
        "cleanup_pdf_snapshot_cache",
        lambda _cache: (_ for _ in ()).throw(PermissionError("still in use")),
    )
    monkeypatch.setattr(
        renderer,
        "defer_pdf_snapshot_cleanup",
        lambda cache: deferred.append(cache) or True,
    )
    opts = core.ImportOptions(
        pages=[1],
        import_text=False,
        model3d_mode="off",
        import_report_path=str(tmp_path / "complete.json"),
    )

    assert core.import_pdf("fixture.pdf", opts) is True
    assert document.commits == 1
    assert document.aborts == 0
    assert opts.import_status == "success"
    assert deferred == [opts._svg_source_snapshot_cache]
    assert opts._svg_source_snapshot_cache["snapshot_path"].endswith("locked.pdf")
    cleanup = reports[-1]["extra"]["svg_snapshot_cleanup"]
    assert cleanup == {
        "status": "deferred",
        "exception_type": "PermissionError",
        "retry_registered": True,
    }


def test_requested_page_order_is_persisted_for_resume_offsets():
    opts = core.ImportOptions(pages=[3, 1])
    identity = session.build_identity(
        source_sha256="a" * 64,
        source_name="ordered.pdf",
        opts=opts,
        importer_version="4.0.80",
        requested_pages=[3, 1],
    )
    assert identity["requested_pages"] == [3, 1]
