"""Exercise the GUI utility commands' real orchestration and cancel transport."""
from types import SimpleNamespace

import pytest

from PDFVectorImporter import PDFTools
from PDFVectorImporter.src import PDFImporterCore as core


class Progress:
    def __init__(self, *_args):
        self.cancelled = False
        self.closed = False
        self.values = []

    def setWindowTitle(self, _title):
        pass

    def setMinimumDuration(self, _duration):
        pass

    def setAutoClose(self, _enabled):
        pass

    def setAutoReset(self, _enabled):
        pass

    def setValue(self, value):
        self.values.append(value)

    def setLabelText(self, _text):
        pass

    def wasCanceled(self):
        return self.cancelled

    def close(self):
        self.closed = True


@pytest.fixture
def workflow(monkeypatch):
    progress = Progress()
    messages = []
    monkeypatch.setattr(PDFTools, "FreeCAD", SimpleNamespace(ActiveDocument=object()))
    monkeypatch.setattr(PDFTools, "QtWidgets", SimpleNamespace(
        QFileDialog=SimpleNamespace(getExistingDirectory=lambda *_args: "inputs",
                                    getOpenFileName=lambda *_args: ("valid.pdf", "")),
        QInputDialog=SimpleNamespace(getText=lambda *_args, **_kwargs: ("all", True)),
        QMessageBox=SimpleNamespace(Yes=1, No=2, question=lambda *_args: 2),
        QProgressDialog=lambda *_args: progress,
        QApplication=SimpleNamespace(processEvents=lambda: None),
    ))
    for name in ("_msg", "_warn", "_err"):
        monkeypatch.setattr(PDFTools, name, messages.append)
    monkeypatch.setattr(PDFTools, "_collect_pdf_files", lambda *_args: ["broken.pdf", "failed.pdf", "valid.pdf"])
    monkeypatch.setattr(core, "ImportOptions", lambda **kwargs: SimpleNamespace(**kwargs))
    return progress, messages


def test_bad_file_is_isolated_and_false_result_never_counts_as_completed(workflow, monkeypatch):
    progress, messages = workflow
    calls = []

    def parse(_spec, path):
        if path == "broken.pdf":
            raise ValueError("Cannot open PDF")
        return [1, 2]

    def importer(path, opts):
        calls.append((path, opts.pages))
        return True if path == "valid.pdf" else None

    monkeypatch.setattr(PDFTools, "_parse_page_spec", parse)
    monkeypatch.setattr(core, "import_pdf", importer)
    PDFTools.BatchImportCommand().Activated()

    assert calls == [("failed.pdf", [1, 2]), ("valid.pdf", [1, 2])]
    assert progress.closed
    assert "2 page(s) completed, 2 failed input(s)" in messages[-1]


def test_batch_cancel_reaches_core_and_stops_remaining_work(workflow, monkeypatch):
    progress, messages = workflow
    calls = []
    options = []
    monkeypatch.setattr(PDFTools, "_parse_page_spec", lambda *_args: [1, 2])

    def importer(path, opts):
        calls.append((path, opts.pages))
        options.append(opts)
        progress.cancelled = True
        assert opts.progress_callback({"page_index": 2, "page_percent": 20,
                                       "label": "Creating objects"}) is False
        opts.import_status = "cancelled"
        opts._report_extra = {"import_session": {"completed_pages": [1]}}
        return False

    monkeypatch.setattr(core, "import_pdf", importer)
    PDFTools.BatchImportCommand().Activated()

    assert calls == [("broken.pdf", [1, 2])]
    assert options[0].progress_callback is None
    assert progress.closed
    assert "Batch cancelled: 1 page(s) completed, 0 failed input(s)" in messages[-1]


def test_console_false_result_never_prints_success(workflow, monkeypatch):
    _progress, messages = workflow
    monkeypatch.setattr(PDFTools, "_parse_page_spec", lambda *_args: [1])
    monkeypatch.setattr(core, "import_pdf", lambda *_args: None)
    PDFTools.ImportViaConsoleCommand().Activated()

    assert "Import completed." not in messages
    assert any("did not complete" in message for message in messages)
