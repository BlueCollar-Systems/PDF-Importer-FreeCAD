"""Genuine PDF source, controlled Document/kernel faults, real import failure path.

These tests do not load OCC/FreeCAD or claim spontaneous native faults. The page
worker is explicit; transaction abort, original owned-object cleanup and final
report persistence run through the production import_pdf implementation.
"""
import copy
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "PDFVectorImporter", ROOT / "PDFVectorImporter/src", ROOT / "tests"):
    sys.path.insert(0, str(directory))
import PDFImporterCore as core  # noqa: E402
import PDFSourceFill as source_fill  # noqa: E402
import test_regular_compound_source_fill_fc as reference  # noqa: E402
from test_source_fill_boundary_fc import Document  # noqa: E402


def genuine_source(tmp_path):
    path = tmp_path / "three genuine structural source pages.pdf"
    with core.fitz.open() as pdf:
        for page_number in range(1, 4):
            page = pdf.new_page(width=600, height=400)
            page.draw_rect(core.fitz.Rect(0, 0, 600, 400), color=None, fill=(1, 1, 1))
            shape = page.new_shape()
            shape.draw_rect(core.fitz.Rect(10, 10, 30, 30))
            shape.draw_rect(core.fitz.Rect(15, 15, 25, 25))
            shape.finish(color=None, fill=(0, 1, 0), even_odd=True)
            shape.commit()
            page.insert_text((45, 45), "MAIN PART 10 PAGE %d QTY 18" % page_number)
        pdf.save(path)
    return path


def install_case(monkeypatch, tmp_path, kind, abort_throws, remove_mode):
    pdf = genuine_source(tmp_path)
    document = Document()
    baseline = document.addObject("Part::Feature", "Existing_Unrelated")
    report = tmp_path / "terminal import report.json"
    observations = {"pages": [], "abort_calls": 0, "remove_calls": [], "cut_calls": 0,
                    "original_failure": None}
    original_abort, original_remove = document.abortTransaction, document.removeObject

    def abort():
        observations["abort_calls"] += 1
        if abort_throws:
            raise RuntimeError("CONTROLLED_DOCUMENT_ABORT_FAILURE")
        return original_abort()  # Original native API shape is void, never a bool check.

    def remove(name):
        observations["remove_calls"].append(name)
        count = observations["remove_calls"].count("OwnedPartialPage2")
        if name == "OwnedPartialPage2" and (remove_mode == "always" or (remove_mode == "once" and count == 1)):
            raise RuntimeError("CONTROLLED_OWNED_OBJECT_REMOVAL_FAILURE")
        return original_remove(name)

    class RefusingRegion:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        def cut(self, other):
            observations["cut_calls"] += 1
            raise RuntimeError("CONTROLLED_SOURCE_BOOLEAN_FAILURE")

    class Kernel(reference.Kernel):
        def Face(self, wire):
            return RefusingRegion(super().Face(wire))

    def page_worker(pdf_doc, _path, page_number, options, target):
        observations["pages"].append(page_number)
        page = pdf_doc.load_page(page_number - 1)
        assert list(page.rect) == [0, 0, 600, 400]
        rows = core.get_clip_aware_drawings(page)
        source, = [row for row in rows if row.get("fill") == (0, 1, 0)]
        contours = source_fill.source_contours(source, core._parse_rect)
        assert len(contours) == 2 and source["even_odd"] is True
        assert "MAIN PART 10 PAGE %d" % page_number in page.get_text()
        target.addObject("Part::Feature", "OwnedPartialPage%d" % page_number)
        if page_number != 2:
            group = target.addObject("App::DocumentObjectGroup", "PDF_Page_%d" % page_number)
            group.isDerivedFrom = lambda value: value == group.TypeId
            return group, None
        if kind == "generic":
            try:
                source_fill.build_compound_fill(contours, source["even_odd"], Kernel(contours),
                                                reference.vector, 1.0, lambda: None)
            except source_fill.SourceFillError as error:
                failure = core.DrawingGeometryFailure("Compound source fill delivery failed: %s" % error)
                observations["original_failure"] = failure
                raise failure from error
            raise AssertionError("Controlled Boolean failure was not exercised")
        if kind == "text":
            failure = core.TextRepresentationFailure("CONTROLLED_SOURCE_TEXT_DELIVERY_FAILURE", {
                "source_item_id": "genuine-page2-span0", "source_text": page.get_text().strip(),
                "page_number": 2, "requested_type": "3d_text", "attempted_type": "3d_text",
                "created_entity_ids": ["OwnedPartialPage2"], "removed_entity_ids": [],
                "cleanup_complete": False})
        else:
            failure = core.ImportCancelled("CONTROLLED_CANCELLATION_ON_SOURCE_PAGE2")
        observations["original_failure"] = failure
        raise failure

    document.abortTransaction, document.removeObject = abort, remove
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(
        GuiUp=False, Version=lambda: ("1", "1", "4"),
        Console=SimpleNamespace(PrintMessage=lambda _: None, PrintWarning=lambda _: None,
                                PrintError=lambda _: None)))
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_err", lambda *_: None)
    monkeypatch.setattr(core, "_import_pdf_page_inner", page_worker)
    options = core.ImportOptions(pages=[1, 2, 3], import_text=kind == "text", text_mode="3d_text",
                                 import_mode="vector", model3d_mode="off", verbose=False,
                                 import_report_path=str(report))
    return pdf, document, baseline, report, options, observations


def failed_scope(extra, pdf, expected_evaluated):
    assert extra["result_status"] == "failed"
    assert extra["import_contract_ready"]["ready"] is False
    assert extra["page_failure"]["requested_pages"] == [1, 2, 3]
    assert extra["page_failure"]["last_evaluated_page"] == 2
    scope = extra["representation_contract_scope"]
    assert scope["requested_pages"] == [1, 2, 3]
    assert scope["evaluated_pages"] == scope["rolled_back_pages"] == expected_evaluated
    assert scope["current_invocation_completed_pages"] == []
    assert pdf.exists()


@pytest.mark.parametrize("kind", ["generic", "text", "cancel"])
@pytest.mark.parametrize("abort_throws", [False, True])
@pytest.mark.parametrize("persistent_removal_failure", [False, True])
def test_original_import_stops_cleans_and_persists_abort_failure(
        monkeypatch, tmp_path, kind, abort_throws, persistent_removal_failure):
    remove_mode = "always" if persistent_removal_failure else ("once" if kind == "cancel" else "none")
    pdf, document, baseline, report, options, observed = install_case(
        monkeypatch, tmp_path, kind, abort_throws, remove_mode)
    with pytest.raises((RuntimeError, core.DrawingGeometryFailure)) as caught:
        core.import_pdf(str(pdf), options)
    failure = observed["original_failure"]
    assert failure is not None and observed["pages"] == [1, 2]
    assert observed["abort_calls"] == 1 and "OwnedPartialPage2" in observed["remove_calls"]
    assert baseline in document.Objects and not document.committed
    if kind == "text" or (not abort_throws and not persistent_removal_failure and kind == "generic"):
        assert caught.value is failure
    else:
        assert caught.value.__cause__ is failure
    saved = json.loads(report.read_text(encoding="utf-8"))
    assert saved["input"]["sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert saved["input"]["pages"] == 0
    extra = saved["extra"]
    failed_scope(extra, pdf, [1, 2])
    assert extra["terminal_failure"]["type"] == type(failure).__name__
    assert extra["terminal_failure"]["message"] == str(failure)
    rollback = extra["rollback"]
    if abort_throws:
        assert rollback["transaction_abort_failure"] == {
            "type": "RuntimeError", "message": "CONTROLLED_DOCUMENT_ABORT_FAILURE"}
        assert rollback["cleanup_complete"] is False
        assert rollback["owned_object_cleanup_complete"] is (not persistent_removal_failure)
    else:
        assert "transaction_abort_failure" not in rollback
        assert rollback["cleanup_complete"] is (not persistent_removal_failure)
    if persistent_removal_failure:
        assert document.getObject("OwnedPartialPage2") is not None
        assert rollback["live_post_baseline_entity_ids"] == ["OwnedPartialPage2"]
        assert any("REMOVAL_FAILURE" in error for error in rollback["cleanup_errors"])
    else:
        assert document.Objects == [baseline]
    assert options.import_status == "failed"
    if kind == "text" and (abort_throws or persistent_removal_failure):
        assert extra["terminal_failure"]["attempt"]["rollback"] == rollback
        assert extra["text_delivery_attempts"][-1]["rollback"] == rollback


@pytest.mark.parametrize("kind", ["generic", "text", "cancel"])
def test_actual_atomic_report_io_refusal_preserves_original_exception_and_previous_file(
        monkeypatch, tmp_path, kind):
    from pdfcadcore import atomic_io
    pdf, document, baseline, report, options, observed = install_case(
        monkeypatch, tmp_path, kind, True, "once" if kind == "cancel" else "none")
    previous = b'{"previous_accepted_report":"preserve exact bytes"}\n'
    report.write_bytes(previous)
    real_replace = atomic_io.Path.replace
    refused_writes = []

    def refuse_replace(source, destination):
        # Production atomic IO uses the Windows extended-length spelling.
        destination_text = str(Path(destination).resolve()).removeprefix("\\\\?\\")
        if destination_text.casefold() == str(report.resolve()).casefold():
            refused_writes.append(str(destination))
            raise OSError("CONTROLLED_ATOMIC_REPORT_REPLACE_REFUSAL")
        return real_replace(source, destination)

    monkeypatch.setattr(atomic_io.Path, "replace", refuse_replace)
    with pytest.raises(RuntimeError) as caught:
        core.import_pdf(str(pdf), options)
    failure = observed["original_failure"]
    assert len(refused_writes) == 1
    assert caught.value is failure if kind == "text" else caught.value.__cause__ is failure
    assert report.read_bytes() == previous and document.Objects == [baseline]
    assert options.import_status == "failed"
    assert options._report_extra["rollback"]["cleanup_complete"] is False
    assert options._report_extra["rollback"]["transaction_abort_failure"]["type"] == "RuntimeError"
    assert options._report_extra["page_failure"]["last_evaluated_page"] == 2
    assert options._report_extra["representation_contract_scope"]["requested_pages"] == [1, 2, 3]


@pytest.mark.parametrize("kind", ["generic", "text", "cancel"])
def test_failed_resume_with_throwing_abort_preserves_prior_certified_session(monkeypatch, tmp_path, kind):
    pdf, document, baseline, report, options, observed = install_case(
        monkeypatch, tmp_path, "cancel", False, "none")
    assert core.import_pdf(str(pdf), options) is False
    session = next(obj for obj in document.Objects if getattr(obj, "PDFImportSessionSchema", ""))
    before_objects = list(document.Objects)
    before_session = copy.deepcopy({key: value for key, value in vars(session).items() if key.startswith("PDF")})
    from PDFImportSession import read_session_object
    assert read_session_object(session)["completed_pages"] == [1]
    # The next invocation fails before changing any previously certified page.
    original_worker = core._import_pdf_page_inner
    observed["pages"].clear()
    observed["abort_calls"] = 0
    observed["remove_calls"].clear()
    real_abort = document.abortTransaction

    def abort():
        observed["abort_calls"] += 1
        raise RuntimeError("CONTROLLED_RESUMED_TRANSACTION_ABORT_FAILURE")

    def resumed_worker(pdf_doc, path, page, cfg, target):
        if kind == "cancel":
            return original_worker(pdf_doc, path, page, cfg, target)
        observed["pages"].append(page)
        actual = pdf_doc.load_page(page - 1)
        assert "MAIN PART 10 PAGE 2" in actual.get_text()
        target.addObject("Part::Feature", "OwnedPartialPage2")
        failure = (core.TextRepresentationFailure("CONTROLLED_RESUMED_TEXT_FAILURE", {})
                   if kind == "text" else core.DrawingGeometryFailure("CONTROLLED_RESUMED_GEOMETRY_FAILURE"))
        observed["original_failure"] = failure
        raise failure

    document.abortTransaction = abort
    if kind == "cancel":
        real_remove = document.removeObject

        def refuse_once(name):
            if name == "OwnedPartialPage2" and not observed.get("active_remove_refused"):
                observed["active_remove_refused"] = True
                raise RuntimeError("CONTROLLED_ACTIVE_PAGE_REMOVAL_FAILURE")
            return real_remove(name)
        document.removeObject = refuse_once
    monkeypatch.setattr(core, "_import_pdf_page_inner", resumed_worker)
    resumed = core.ImportOptions(pages=[1, 2, 3], import_text=False,
                                 import_mode="vector", model3d_mode="off", verbose=False,
                                 import_report_path=str(tmp_path / "resumed failed report.json"),
                                 resume_session_name=session.Name)
    with pytest.raises(RuntimeError) as caught:
        core.import_pdf(str(pdf), resumed)
    assert observed["pages"] == [2] and observed["abort_calls"] == 1
    assert document.Objects == before_objects and baseline in document.Objects
    assert {key: value for key, value in vars(session).items() if key.startswith("PDF")} == before_session
    extra = json.loads(Path(resumed.import_report_path).read_text(encoding="utf-8"))["extra"]
    failed_scope(extra, pdf, [2])
    assert extra["representation_contract_scope"]["previously_certified_pages_excluded"] == [1]
    assert extra["representation_contract_scope"]["session_completed_pages"] == [1]
    assert extra["rollback"]["owned_object_cleanup_complete"] is True
    assert extra["rollback"]["cleanup_complete"] is False
    assert caught.value is observed["original_failure"] if kind == "text" else caught.value.__cause__ is observed["original_failure"]
    document.abortTransaction = real_abort
