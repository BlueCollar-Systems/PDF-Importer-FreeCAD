"""Every import ends with a plain message, and Cancel leaves no empty tab.

The real toolbar command (ImportPDFVectorCommand.Activated), the File > Open
handler and the headless handler run here against fake Qt and FreeCAD modules:
  * a failed import shows exactly one "Import Failed" box naming the reason,
    the page and a report file under <user data>/PDF Import Reports;
  * a finished import with stepped-down items shows one warning box with the
    count and the report; a clean import shows no box;
  * File > Open followed by Cancel closes the empty document it created;
  * headless runs never pop a box and print the reason and the report.
Fixture data is fictional (job D042).
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
for folder in (ROOT, ROOT / "PDFVectorImporter", ROOT / "PDFVectorImporter/src", ROOT / "tests"):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

import PDFVectorImporter.src as importer_src  # noqa: E402
from PDFVectorImporter.src import PDFImporterCore as core  # noqa: E402
from pdfcadcore.fitz_loader import PdfOpenError as HandlerPdfOpenError  # noqa: E402
from test_source_fill_boundary_fc import Document  # noqa: E402


# ── fake Qt ──────────────────────────────────────────────────────────────

class MessageBox:
    """Records every box: instance boxes (outcome) and static ones (legacy)."""
    Critical, Warning, ActionRole, Ok, Yes, Cancel = "critical", "warning", "action", "ok", 1, 0
    shown = []
    answer = 1

    def __init__(self, parent=None):
        self.parent, self.icon, self.title, self.text, self.buttons = parent, None, "", "", []
        self.click = None

    def setIcon(self, icon):
        self.icon = icon

    def setWindowTitle(self, title):
        self.title = title

    def setText(self, text):
        self.text = text

    def addButton(self, *args):
        button = SimpleNamespace(args=args)
        self.buttons.append(button)
        return button

    def exec(self):
        MessageBox.shown.append(("box", self.icon, self.title, self.text, [b.args[0] for b in self.buttons]))
        return 0

    def clickedButton(self):
        return self.click

    @staticmethod
    def question(*_args):
        return MessageBox.answer

    @staticmethod
    def warning(_parent, title, text):
        MessageBox.shown.append(("static", "warning", title, text, []))

    @staticmethod
    def critical(_parent, title, text):
        MessageBox.shown.append(("static", "critical", title, text, []))


class ProgressDialog:
    def __init__(self, *_args):
        pass

    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: False


def qt_modules():
    widgets = SimpleNamespace(
        QMessageBox=MessageBox, QProgressDialog=ProgressDialog,
        QDialog=type("QDialog", (), {"Accepted": 1}),
        QApplication=SimpleNamespace(processEvents=lambda: None))
    opened = []
    gui = SimpleNamespace(QDesktopServices=SimpleNamespace(openUrl=lambda url: opened.append(url) or True))
    qtcore = SimpleNamespace(QUrl=SimpleNamespace(fromLocalFile=lambda path: path))
    return SimpleNamespace(QtWidgets=widgets, QtGui=gui, QtCore=qtcore), opened


@pytest.fixture
def ui(tmp_path, monkeypatch):
    """The real command module and handler over one fake FreeCAD and fake Qt."""
    MessageBox.shown = []
    MessageBox.answer = 1
    pyside, opened = qt_modules()
    console = {"message": [], "warning": [], "error": []}
    documents = []

    def new_document(name):
        doc = SimpleNamespace(Name=name, Objects=[])
        documents.append(doc)
        return doc

    host = SimpleNamespace(
        GuiUp=True, Version=lambda: ("1", "1", "4"),
        getUserAppDataDir=lambda: str(tmp_path / "user"),
        getResourceDir=lambda: str(tmp_path / "resource"),
        Console=SimpleNamespace(PrintMessage=console["message"].append,
                                PrintWarning=console["warning"].append,
                                PrintError=console["error"].append),
        newDocument=Mock(side_effect=new_document), setActiveDocument=Mock(),
        getDocument=Mock(return_value=None), closeDocument=Mock())
    with patch.dict("sys.modules", {"FreeCAD": host, "PySide6": pyside}):
        spec = importlib.util.spec_from_file_location(
            "isolated_pdf_importer_cmd", ROOT / "PDFVectorImporter/src/PDFImporterCmd.py")
        command = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(command)
        spec = importlib.util.spec_from_file_location(
            "isolated_pdf_import_handler_outcome", ROOT / "PDFVectorImporter/PDFImportHandler.py")
        handler = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(handler)
    monkeypatch.setitem(sys.modules, "PDFImporterCmd", command)
    monkeypatch.setitem(sys.modules, "PySide6", pyside)
    monkeypatch.setattr(core, "FreeCAD", host)
    return SimpleNamespace(command=command, handler=handler, host=host, console=console,
                           documents=documents, opened=opened, tmp=tmp_path)


def accepted_dialog(command, pdf, opts, monkeypatch, *, accept=True):
    field = SimpleNamespace(value=str(pdf))
    field.text = lambda: field.value
    field.setText = lambda value: setattr(field, "value", value)
    dialog = SimpleNamespace(
        file_edit=field, page_edit=SimpleNamespace(setPlaceholderText=lambda _text: None),
        build_options=lambda: opts, exec=lambda: 1 if accept else 0)
    monkeypatch.setattr(command, "ImportPDFDialog", lambda: dialog)
    return dialog


def sample_pdf(tmp_path, pages=3):
    pdf = tmp_path / "D042 sample.pdf"
    with core.fitz.open() as source:
        for _ in range(pages):
            source.new_page(width=100, height=100)
        source.save(pdf)
    return pdf


def real_core_failing_on_page_2(monkeypatch, error):
    """The real import_pdf over a fake document; page 2's worker raises."""
    document = Document()
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)

    def page_worker(_pdf, _path, page, _opts, doc):
        if page == 2:
            doc.addObject("Part::Feature", "Partial_Page_2")
            raise error
        return doc.addObject("App::DocumentObjectGroup", "PDF_Page_%d" % page), None

    monkeypatch.setattr(core, "_import_pdf_page_inner", page_worker)
    wrapper = SimpleNamespace(
        ImportCancelled=core.ImportCancelled, ImportOptions=core.ImportOptions,
        estimate_import_work=lambda *_args: {"pages": [], "total_units": 0},
        find_resumable_import_session=lambda *_args: None,
        import_pdf=core.import_pdf)
    monkeypatch.setitem(sys.modules, "PDFVectorImporter.src.PDFImporterCore", wrapper)
    monkeypatch.setattr(importer_src, "PDFImporterCore", wrapper)
    return document


def options(**changes):
    opts = core.ImportOptions(import_text=False, import_mode="vector", model3d_mode="off", verbose=False)
    for name, value in changes.items():
        setattr(opts, name, value)
    return opts


def failure_boxes():
    return [box for box in MessageBox.shown if box[1] == "critical"]


def warning_boxes():
    return [box for box in MessageBox.shown if box[1] == "warning"]


# (a) and (b): the toolbar command, a geometry failure and an unexpected KeyError

@pytest.mark.parametrize("error", [core.DrawingGeometryFailure("Native drawing face is null or invalid"),
                                   KeyError("seqno")])
def test_toolbar_import_failure_shows_one_plain_box_with_page_and_report(ui, monkeypatch, error):
    pdf = sample_pdf(ui.tmp)
    opts = options()
    real_core_failing_on_page_2(monkeypatch, error)
    accepted_dialog(ui.command, pdf, opts, monkeypatch)

    ui.command.ImportPDFVectorCommand().Activated()      # returns normally: nothing escapes

    (box,) = failure_boxes()
    assert box[2] == "Import Failed" and not warning_boxes()
    text = box[3]
    assert text.startswith("This PDF could not be imported. Nothing was added to your drawing.")
    assert "Page: 2" in text and "Reason: " in text
    report_path = Path(text.split("Details: ", 1)[1].strip())
    assert report_path.is_file() and report_path.parent == ui.tmp / "user" / "PDF Import Reports"
    assert json.loads(report_path.read_text(encoding="utf-8"))["extra"]["result_status"] == "failed"
    assert box[4] == ["Open report folder", "ok"]
    assert any("Import failed" in line and str(report_path) in line for line in ui.console["error"])


def test_open_report_folder_button_opens_the_folder(ui, tmp_path):
    report = tmp_path / "user" / "PDF Import Reports" / "D042_import_report.json"
    report.parent.mkdir(parents=True)
    report.write_text("{}", encoding="utf-8")
    original_exec = MessageBox.exec

    def click_open(self):
        original_exec(self)
        self.click = self.buttons[0]

    with patch.object(MessageBox, "exec", click_open):
        ui.command._show_outcome_box("warning", "PDF Import", "text", str(report))
    assert ui.opened == [str(report.parent)]


def test_incomplete_cleanup_is_said_plainly(ui):
    opts = options()
    opts._report_extra = {"rollback": {"cleanup_complete": False}}
    text = ui.command.show_import_failure(RuntimeError("rollback was incomplete"), opts)
    assert "could not remove everything it had started" in text
    assert "Nothing was added" not in text


# (c) and (d): finished imports

def written_report(tmp_path, extra, pages=1):
    path = tmp_path / "user" / "PDF Import Reports" / "D042_import_report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"input": {"pages": pages}, "extra": extra}), encoding="utf-8")
    return str(path)


def successful_core(monkeypatch, extra, report_path):
    def import_pdf(_path, opts):
        opts._report_extra = dict(extra)
        opts._last_import_report_path = report_path
        opts.auto_resolved_mode = None
        return True

    wrapper = SimpleNamespace(
        ImportCancelled=core.ImportCancelled,
        estimate_import_work=lambda *_args: {"pages": [], "total_units": 0},
        find_resumable_import_session=lambda *_args: None, import_pdf=import_pdf)
    monkeypatch.setitem(sys.modules, "PDFVectorImporter.src.PDFImporterCore", wrapper)
    monkeypatch.setattr(importer_src, "PDFImporterCore", wrapper)


def test_finished_import_with_undelivered_text_shows_one_warning(ui, monkeypatch):
    extra = {"text_items_degraded": {"total": 3, "dropped": 3, "delivered_at_lower_rung": 0, "pages": [1]}}
    report = written_report(ui.tmp, extra)
    successful_core(monkeypatch, extra, report)
    accepted_dialog(ui.command, sample_pdf(ui.tmp, 1), options(), monkeypatch)

    ui.command.ImportPDFVectorCommand().Activated()

    (box,) = warning_boxes()
    assert not failure_boxes()
    assert "Imported 1 page." in box[3]
    assert "3 text items could not be drawn as requested (page 1)" in box[3]
    assert "0 drawn as outlines or pictures instead, 3 left out" in box[3]
    assert report in box[3] and box[4] == ["Open report folder", "ok"]
    assert any("3 text items" in line for line in ui.console["warning"])


def test_finished_import_with_a_stepped_down_drawing_item_says_so(ui):
    extra = {"geometry_items_degraded": {"total": 1, "delivered_as_outline": 1, "partly_delivered": 0,
                                         "skipped": 0, "pages": [2]}}
    opts = options()
    opts._last_import_report_path = written_report(ui.tmp, extra, pages=3)
    text = ui.command.show_import_result(opts)
    assert text.startswith("Imported 3 pages. 1 drawing item could not be built as requested (page 2)")
    assert len(warning_boxes()) == 1


def test_finished_import_with_a_picture_left_out_says_so(ui):
    extra = {"picture_items_degraded": {"total": 1, "skipped": 1, "pages": [1]}}
    opts = options()
    opts._last_import_report_path = written_report(ui.tmp, extra)
    text = ui.command.show_import_result(opts)
    assert "1 picture could not be placed (page 1) and was left out." in text
    assert len(warning_boxes()) == 1


def test_clean_import_shows_no_box(ui, monkeypatch):
    extra = {"scale_crosscheck": {"level": "warn", "reasons": ["no_scale_detected"],
                                  "banner": "Check scale"}}
    report = written_report(ui.tmp, extra)
    successful_core(monkeypatch, extra, report)
    accepted_dialog(ui.command, sample_pdf(ui.tmp, 1), options(), monkeypatch)

    ui.command.ImportPDFVectorCommand().Activated()

    assert MessageBox.shown == []
    assert "PDF import complete.\n" in ui.console["message"]


def test_a_real_scale_disagreement_is_shown(ui):
    extra = {"scale_crosscheck": {"level": "warn", "reasons": ["conflicting_scale_notations"],
                                  "banner": "Verify the drawing scale before takeoff."}}
    opts = options()
    opts._last_import_report_path = written_report(ui.tmp, extra)
    assert "Scale note: Verify the drawing scale" in ui.command.show_import_result(opts)


def test_quiet_mode_never_pops_a_box(ui):
    extra = {"text_items_degraded": {"total": 2, "dropped": 1, "delivered_at_lower_rung": 1, "pages": [4]}}
    opts = options()
    opts._last_import_report_path = written_report(ui.tmp, extra)
    assert ui.command.show_import_result(opts, quiet=True)
    assert ui.command.show_import_failure(ValueError("bad page"), opts, quiet=True)
    assert MessageBox.shown == []
    assert ui.console["warning"] and ui.console["error"]


# (e) File > Open with the dialog: the same outcomes, and the PdfOpenError branch

def test_file_open_failure_shows_one_box_and_closes_the_empty_document(ui, monkeypatch):
    pdf = sample_pdf(ui.tmp)
    real_core_failing_on_page_2(monkeypatch, core.DrawingGeometryFailure("Native drawing stroke construction failed"))
    accepted_dialog(ui.command, pdf, options(), monkeypatch)

    ui.handler.open(str(pdf))

    (box,) = failure_boxes()
    assert "Page: 2" in box[3] and "Details: " in box[3]
    created = ui.documents[0]
    ui.host.closeDocument.assert_called_once_with(created.Name)


def test_file_open_finished_with_warnings_shows_one_warning(ui, monkeypatch):
    extra = {"text_items_degraded": {"total": 3, "dropped": 3, "delivered_at_lower_rung": 0, "pages": [1]}}
    report = written_report(ui.tmp, extra)
    successful_core(monkeypatch, extra, report)
    pdf = sample_pdf(ui.tmp, 1)
    accepted_dialog(ui.command, pdf, options(), monkeypatch)
    assert ui.handler._import_with_dialog(str(pdf)) is True
    (box,) = warning_boxes()
    assert "3 text items" in box[3] and report in box[3]


def test_file_open_unreadable_pdf_shows_the_pdf_open_warning(ui, monkeypatch):
    def unreadable(*_args):
        raise HandlerPdfOpenError("malformed", "This file is not a readable PDF.")

    wrapper = SimpleNamespace(ImportCancelled=core.ImportCancelled, estimate_import_work=unreadable,
                              find_resumable_import_session=lambda *_args: None, import_pdf=unreadable)
    monkeypatch.setitem(sys.modules, "PDFVectorImporter.src.PDFImporterCore", wrapper)
    monkeypatch.setattr(importer_src, "PDFImporterCore", wrapper)
    accepted_dialog(ui.command, ui.tmp / "D042.pdf", options(), monkeypatch)
    assert ui.handler._import_with_dialog(str(ui.tmp / "D042.pdf")) is False
    assert MessageBox.shown == [("static", "warning", "PDF Import", "This file is not a readable PDF.", [])]


# (f) Cancel: File > Open closes its empty tab; File > Import never closes the user's document

def test_file_open_then_cancel_closes_the_new_empty_document(ui, monkeypatch):
    accepted_dialog(ui.command, ui.tmp / "D042.pdf", options(), monkeypatch, accept=False)
    ui.handler.open(str(ui.tmp / "D042.pdf"))
    created = ui.documents[0]
    ui.host.closeDocument.assert_called_once_with(created.Name)
    assert MessageBox.shown == []


def test_estimate_prompt_cancel_also_closes_the_new_empty_document(ui, monkeypatch):
    successful_core(monkeypatch, {}, "")
    MessageBox.answer = 0                                      # "Cancel" on the work estimate
    pdf = sample_pdf(ui.tmp, 1)
    accepted_dialog(ui.command, pdf, options(), monkeypatch)
    ui.handler.open(str(pdf))
    ui.host.closeDocument.assert_called_once_with(ui.documents[0].Name)


def test_a_document_that_kept_pages_is_never_closed(ui, monkeypatch):
    accepted_dialog(ui.command, ui.tmp / "D042.pdf", options(), monkeypatch, accept=False)
    ui.host.newDocument.side_effect = lambda name: ui.documents.append(
        SimpleNamespace(Name=name, Objects=["PDF_Page_1"])) or ui.documents[-1]
    ui.handler.open(str(ui.tmp / "D042.pdf"))
    ui.host.closeDocument.assert_not_called()


def test_file_import_then_cancel_never_closes_the_users_document(ui, monkeypatch):
    accepted_dialog(ui.command, ui.tmp / "D042.pdf", options(), monkeypatch, accept=False)
    users = SimpleNamespace(Name="ShopDrawing", Objects=[])
    ui.host.getDocument.return_value = users
    ui.handler.insert(str(ui.tmp / "D042.pdf"), "ShopDrawing")
    ui.host.closeDocument.assert_not_called()


# (g) headless: no box, the reason and the report on the console

def test_headless_failure_prints_reason_and_report_and_closes_the_empty_document(ui, monkeypatch):
    ui.host.GuiUp = False
    pdf = sample_pdf(ui.tmp)
    real_core_failing_on_page_2(monkeypatch, core.DrawingGeometryFailure("Native drawing face is null or invalid"))
    ui.handler.open(str(pdf))
    assert MessageBox.shown == []
    line = next(text for text in ui.console["error"] if text.startswith("Import failed:"))
    assert "Native drawing face is null or invalid" in line and "Report: " in line
    report = Path(line.split("Report: ", 1)[1].splitlines()[0].strip())
    assert report.is_file()
    ui.host.closeDocument.assert_called_once_with(ui.documents[0].Name)


# The core contract the UI relies on

def test_core_failure_carries_page_and_published_report(ui, monkeypatch, tmp_path):
    pdf = sample_pdf(tmp_path)
    failure = core.DrawingGeometryFailure("Native drawing face is null or invalid")
    real_core_failing_on_page_2(monkeypatch, failure)
    opts = options()
    with pytest.raises(core.DrawingGeometryFailure) as caught:
        core.import_pdf(str(pdf), opts)
    assert caught.value.bcs_failed_page == 2
    published = Path(caught.value.bcs_report_path)
    assert published.is_file() and published.parent == tmp_path / "user" / "PDF Import Reports"
    assert opts._last_import_report_path == str(published)
    assert published.name.startswith("D042 sample_") and published.name.endswith("_import_report.json")
    assert any("Import failed on page 2" in line and str(published) in line for line in ui.console["error"])


def test_core_success_always_prints_the_report_path(ui, monkeypatch, tmp_path):
    pdf = sample_pdf(tmp_path, 1)
    document = Document()
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(core, "_import_pdf_page_inner", lambda _pdf, _path, page, _opts, doc: (
        doc.addObject("App::DocumentObjectGroup", "PDF_Page_%d" % page), None))
    opts = options()
    assert core.import_pdf(str(pdf), opts) is True
    published = Path(opts._last_import_report_path)
    assert published.is_file() and published.parent.name == "PDF Import Reports"
    assert "Import report: %s\n" % published in ui.console["message"]


def test_report_folder_keeps_only_the_newest_fifty(ui, tmp_path, monkeypatch):
    source = tmp_path / "work" / "D042_import_report.json"
    source.parent.mkdir()
    source.write_text("{}", encoding="utf-8")
    stamps = iter("20261010-%06d" % index for index in range(core.IMPORT_REPORT_KEEP + 5))
    monkeypatch.setattr(core.time, "strftime", lambda _fmt: next(stamps))
    for _ in range(core.IMPORT_REPORT_KEEP + 5):
        assert core._publish_report_copy(str(source), str(tmp_path / "D042.pdf"))
    folder = tmp_path / "user" / "PDF Import Reports"
    assert len(list(folder.glob("*_import_report.json"))) == core.IMPORT_REPORT_KEEP


def test_a_failed_copy_is_only_a_warning(ui, tmp_path, monkeypatch):
    source = tmp_path / "work" / "D042_import_report.json"
    source.parent.mkdir()
    source.write_text("{}", encoding="utf-8")

    def refuse(*_args):
        raise OSError("read-only folder")

    monkeypatch.setattr(core.shutil, "copyfile", refuse)
    assert core._publish_report_copy(str(source), "D042.pdf") == ""
    assert any("was not made" in line for line in ui.console["warning"])
