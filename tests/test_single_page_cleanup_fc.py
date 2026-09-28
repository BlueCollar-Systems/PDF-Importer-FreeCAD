"""Direct single-page failures must not leave a plausible partial drawing."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "PDFVectorImporter" / "src",
             REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402


class Document:
    def __init__(self):
        self.objects = []
        self.read_error = False
        self.refuse_remove = False

    @property
    def Objects(self):
        if self.read_error:
            raise RuntimeError("object inventory unavailable")
        return list(self.objects)

    def addObject(self, name):
        obj = SimpleNamespace(Name=name)
        self.objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.objects if obj.Name == name), None)

    def removeObject(self, name):
        if self.refuse_remove:
            raise RuntimeError("object removal refused")
        self.objects = [obj for obj in self.objects if obj.Name != name]

    def recompute(self):
        pass

    def openTransaction(self, *_args):
        raise AssertionError("the caller owns transaction boundaries")

    commitTransaction = abortTransaction = openTransaction


@pytest.fixture
def host(monkeypatch):
    from pdfcadcore import fitz_loader

    document = Document()
    existing = document.addObject("ExistingPart")
    pdf = SimpleNamespace(closed=False)

    def close():
        pdf.closed = True

    pdf.close = close
    monkeypatch.setattr(fitz_loader, "safe_open", lambda _path: pdf)
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_autofit_import_view", lambda _doc: None)
    monkeypatch.setattr(core, "_warn", lambda _message: None)
    return document, existing, pdf


@pytest.mark.parametrize("failure", [
    RuntimeError("page builder failed"),
    core.ImportCancelled("cancelled by user"),
    core.TextRepresentationFailure(
        "text delivery contract failed",
        {"source_item_id": "p1:b0:l0:s0", "outcome": "failed",
         "reason": "delivery_contract", "cleanup_complete": True},
    ),
])
def test_direct_page_failure_cleans_objects_and_delivery_evidence(
    host, monkeypatch, failure
):
    document, existing, pdf = host
    opts = core.ImportOptions()

    def fail(*_args):
        document.addObject("PDF_Page_1")
        partial = document.addObject("PartialGlyph")
        opts.text_delivered_counts["native_3d_text"] = 1
        opts.text_delivery_attempts.append({"outcome": "verified"})
        opts._report_extra["text_items_degraded"] = {"count": 1}
        opts._native_text_object_index = {"object": partial}
        raise failure

    monkeypatch.setattr(core, "_import_pdf_page_inner", fail)
    with pytest.raises(type(failure)) as caught:
        core.import_pdf_page("D042.pdf", opts=opts)

    assert caught.value is failure
    assert document.Objects == [existing]
    assert pdf.closed
    assert opts.text_delivered_counts == {}
    assert opts._native_text_object_index is None
    assert "text_items_degraded" not in opts._report_extra
    assert opts._report_extra["rollback"]["cleanup_complete"] is True
    assert opts.import_status == (
        "cancelled" if isinstance(failure, core.ImportCancelled) else "failed"
    )
    if isinstance(failure, core.TextRepresentationFailure):
        assert opts.text_delivery_attempts == [failure.attempt]
    else:
        assert opts.text_delivery_attempts == []


@pytest.mark.parametrize("failure", [
    RuntimeError("page builder failed"),
    core.ImportCancelled("cancelled by user"),
])
def test_incomplete_cleanup_is_loud_and_keeps_original_cause(
    host, monkeypatch, failure
):
    document, existing, pdf = host
    opts = core.ImportOptions()
    document.refuse_remove = True

    def fail(*_args):
        document.addObject("PartialGlyph")
        raise failure

    monkeypatch.setattr(core, "_import_pdf_page_inner", fail)
    with pytest.raises(RuntimeError, match="cleanup was incomplete") as caught:
        core.import_pdf_page("D042.pdf", opts=opts)

    assert caught.value.__cause__ is failure
    assert existing in document.Objects
    assert pdf.closed
    assert opts.import_status == "failed"
    assert opts._report_extra["rollback"]["cleanup_complete"] is False
    assert opts._report_extra["rollback"]["live_post_baseline_entity_ids"] == [
        "PartialGlyph"
    ]


def test_success_preserves_result_and_does_not_touch_caller_transaction(
    host, monkeypatch
):
    document, existing, pdf = host
    opts = core.ImportOptions()
    result = (document.addObject("CompletedPage"), {"native_3d_text": 1})

    def complete(*_args):
        opts.text_delivered_counts["native_3d_text"] = 1
        return result

    monkeypatch.setattr(core, "_import_pdf_page_inner", complete)
    assert core.import_pdf_page("D042.pdf", opts=opts) is result
    assert document.Objects == [existing, result[0]]
    assert pdf.closed
    assert opts.import_status == "success"
    assert opts.text_delivered_counts == {"native_3d_text": 1}
    assert "rollback" not in opts._report_extra


def test_unreadable_inventory_cannot_be_reported_as_complete_cleanup():
    document = Document()
    existing = document.addObject("ExistingPart")
    document.addObject("PartialGlyph")
    document.read_error = True

    cleanup = core._remove_post_baseline_document_objects(
        document, {id(existing)}, {"ExistingPart"}
    )

    assert cleanup["cleanup_complete"] is False
    assert any("object inventory unavailable" in error
               for error in cleanup["cleanup_errors"])
    assert document.objects[0] is existing


def test_preflight_failure_never_recomputes_the_existing_model(host, monkeypatch):
    from pdfcadcore import fitz_loader

    document, existing, _pdf = host
    opts = core.ImportOptions()

    def fail_open(_path):
        raise ValueError("PDF could not be opened")

    def unexpected(*_args):
        raise AssertionError("preflight must not modify the document")

    monkeypatch.setattr(fitz_loader, "safe_open", fail_open)
    monkeypatch.setattr(document, "recompute", unexpected)
    monkeypatch.setattr(core, "_import_pdf_page_inner", unexpected)
    with pytest.raises(ValueError, match="PDF could not be opened"):
        core.import_pdf_page("D042.pdf", opts=opts)
    assert document.Objects == [existing]
    assert opts.import_status == "failed"
    assert "rollback" not in opts._report_extra


def test_failed_final_inventory_cannot_certify_cleanup():
    document = Document()
    existing = document.addObject("ExistingPart")
    document.addObject("PartialGlyph")
    remove = document.removeObject

    def remove_then_lose_inventory(name):
        remove(name)
        document.read_error = True

    document.removeObject = remove_then_lose_inventory
    cleanup = core._remove_post_baseline_document_objects(
        document, {id(existing)}, {"ExistingPart"}
    )
    assert document.objects == [existing]
    assert cleanup["cleanup_complete"] is False
    assert any("objects after cleanup" in error
               for error in cleanup["cleanup_errors"])
