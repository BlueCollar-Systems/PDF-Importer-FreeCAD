"""Persist requested/evaluated pages after genuine-PDF transaction failures."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for directory in (ROOT, ROOT / "PDFVectorImporter", ROOT / "PDFVectorImporter/src", ROOT / "tests"):
    sys.path.insert(0, str(directory))
import PDFImporterCore as core  # noqa: E402
from test_source_fill_boundary_fc import Document  # noqa: E402


def setup_import(monkeypatch, tmp_path):
    pdf = tmp_path / "three requested pages.pdf"
    with core.fitz.open() as source:
        for _ in range(3):
            source.new_page(width=100, height=100)
        source.save(pdf)
    document = Document()
    existing = document.addObject("Part::Feature", "Existing")
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(
        GuiUp=False, Version=lambda: ("1", "1", "4"),
        Console=SimpleNamespace(PrintMessage=lambda _: None,
                                PrintWarning=lambda _: None, PrintError=lambda _: None)))
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_err", lambda *args: None)
    report = tmp_path / "failed import.json"
    options = core.ImportOptions(pages=[1, 2, 3], import_text=False,
                                 import_mode="vector", model3d_mode="off",
                                 import_report_path=str(report), verbose=False)
    return pdf, document, existing, report, options


@pytest.mark.parametrize("error_type", [core.DrawingGeometryFailure, RuntimeError, ValueError])
@pytest.mark.parametrize("failed_page", [1, 2])
def test_failed_requested_page_survives_transaction_rollback(monkeypatch, tmp_path, error_type, failed_page):
    pdf, document, existing, report, options = setup_import(monkeypatch, tmp_path)
    failure = error_type("original page construction refused")
    calls = []

    def page_import(_pdf, _path, page, _options, doc):
        calls.append(page)
        if page == failed_page:
            doc.addObject("Part::Feature", "Partial_Failed_Page")
            raise failure
        return doc.addObject("App::DocumentObjectGroup", f"PDF_Page_{page}"), None

    monkeypatch.setattr(core, "_import_pdf_page_inner", page_import)
    with pytest.raises(error_type) as caught:
        core.import_pdf(str(pdf), options)
    assert caught.value is failure and calls == list(range(1, failed_page + 1))
    assert document.aborted and not document.committed and document.Objects == [existing]
    saved = json.loads(report.read_text(encoding="utf-8"))
    extra = saved["extra"]
    assert saved["input"]["pages"] == 0  # Imported pages, not requested/source count.
    assert extra["result_status"] == "failed"
    assert extra["terminal_failure"]["type"] == error_type.__name__
    assert extra["page_failure"] == {
        "schema": "bcs.freecad.page_failure/1", "requested_pages": [1, 2, 3],
        "last_evaluated_page": failed_page, "phase": "import_transaction",
        "type": error_type.__name__, "message": str(failure),
        "rollback_cleanup_complete": True,
    }
    scope = extra["representation_contract_scope"]
    assert scope["requested_pages"] == [1, 2, 3]
    assert scope["evaluated_pages"] == scope["rolled_back_pages"] == calls
    assert scope["session_completed_pages"] == scope["current_invocation_completed_pages"] == []
    assert extra["rollback"]["cleanup_complete"] is True
    assert extra["import_contract_ready"]["ready"] is False
    assert extra["import_contract_ready"]["checks"]["result_succeeded"] is False


def test_failed_rollback_stops_and_persists_failed_page(monkeypatch, tmp_path):
    pdf, document, existing, report, options = setup_import(monkeypatch, tmp_path)
    failure = core.DrawingGeometryFailure("original source face failure")
    calls = []
    original_remove = document.removeObject

    def remove(name):
        if name == "Partial_Failed_Page":
            raise RuntimeError("native object removal refused")
        return original_remove(name)

    document.removeObject = remove

    def page_import(_pdf, _path, page, _options, doc):
        calls.append(page)
        doc.addObject("Part::Feature", "Partial_Failed_Page")
        raise failure

    monkeypatch.setattr(core, "_import_pdf_page_inner", page_import)
    with pytest.raises(RuntimeError, match="rollback was incomplete") as caught:
        core.import_pdf(str(pdf), options)
    assert caught.value.__cause__ is failure and calls == [1]
    assert existing in document.Objects and document.aborted and not document.committed
    extra = json.loads(report.read_text(encoding="utf-8"))["extra"]
    assert extra["rollback"]["cleanup_complete"] is False
    assert extra["page_failure"]["last_evaluated_page"] == 1
    assert extra["page_failure"]["rollback_cleanup_complete"] is False
    assert extra["representation_contract_scope"]["requested_pages"] == [1, 2, 3]
    assert extra["import_contract_ready"]["ready"] is False


def test_report_io_failure_keeps_original_exception_and_page_telemetry(monkeypatch, tmp_path):
    pdf, document, existing, report, options = setup_import(monkeypatch, tmp_path)
    failure = core.DrawingGeometryFailure("exact original failure")

    def page_import(*args):
        document.addObject("Part::Feature", "Partial_Failed_Page")
        raise failure

    def refuse_report(**kwargs):
        raise OSError("report storage unavailable")

    monkeypatch.setattr(core, "_import_pdf_page_inner", page_import)
    monkeypatch.setattr(core, "_write_terminal_representation_failure_report", refuse_report)
    with pytest.raises(core.DrawingGeometryFailure) as caught:
        core.import_pdf(str(pdf), options)
    assert caught.value is failure and document.Objects == [existing]
    assert options.import_status == "failed"
    assert options._report_extra["page_failure"]["last_evaluated_page"] == 1
    assert options._report_extra["representation_contract_scope"]["requested_pages"] == [1, 2, 3]
    assert not report.exists()  # No claim of persistence when IO was refused.


def test_failure_before_first_page_keeps_unknown_page_explicit(monkeypatch, tmp_path):
    pdf, document, existing, report, options = setup_import(monkeypatch, tmp_path)
    options.resume_session_name = "Missing_Owned_Session"
    monkeypatch.setattr(core, "_import_pdf_page_inner", lambda *args: pytest.fail("No page may start"))
    with pytest.raises(ValueError, match="was not found"):
        core.import_pdf(str(pdf), options)
    assert document.Objects == [existing]
    extra = json.loads(report.read_text(encoding="utf-8"))["extra"]
    assert extra["page_failure"]["last_evaluated_page"] is None
    assert extra["page_failure"]["requested_pages"] == [1, 2, 3]
    assert extra["representation_contract_scope"]["evaluated_pages"] == []
    assert extra["import_contract_ready"]["ready"] is False


def test_failed_resume_preserves_prior_certified_pages_in_saved_report(monkeypatch, tmp_path):
    pdf, document, existing, report, options = setup_import(monkeypatch, tmp_path)

    def cancelled_page(_pdf, _path, page, _options, doc):
        if page == 2:
            raise core.ImportCancelled("cancel after first certified page")
        group = doc.addObject("App::DocumentObjectGroup", f"PDF_Page_{page}")
        group.isDerivedFrom = lambda kind: kind == group.TypeId
        return group, None

    monkeypatch.setattr(core, "_import_pdf_page_inner", cancelled_page)
    assert core.import_pdf(str(pdf), options) is False
    session = next(obj for obj in document.Objects if getattr(obj, "PDFImportSessionSchema", ""))
    before = list(document.Objects)
    failure = core.DrawingGeometryFailure("resumed page2 source boundary refused")
    calls = []

    def failed_page(_pdf, _path, page, _options, doc):
        calls.append(page)
        doc.addObject("Part::Feature", "Partial_Failed_Resume_Page")
        raise failure

    monkeypatch.setattr(core, "_import_pdf_page_inner", failed_page)
    resumed = core.ImportOptions(pages=[1, 2, 3], import_text=False,
        import_mode="vector", model3d_mode="off", verbose=False,
        import_report_path=str(tmp_path / "resumed failure.json"), resume_session_name=session.Name)
    with pytest.raises(core.DrawingGeometryFailure) as caught:
        core.import_pdf(str(pdf), resumed)
    assert caught.value is failure and calls == [2] and document.Objects == before
    saved = json.loads(Path(resumed.import_report_path).read_text(encoding="utf-8"))
    assert saved["input"]["pages"] == 1
    extra = saved["extra"]
    assert extra["page_failure"]["last_evaluated_page"] == 2
    scope = extra["representation_contract_scope"]
    assert scope["requested_pages"] == [1, 2, 3]
    assert scope["evaluated_pages"] == scope["rolled_back_pages"] == [2]
    assert scope["session_completed_pages"] == scope["previously_certified_pages_excluded"] == [1]
    assert scope["current_invocation_completed_pages"] == []
    assert extra["import_contract_ready"]["ready"] is False
