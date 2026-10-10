"""A stopped import can be resumed, started over, or left alone.

The resume prompt used to offer only Resume or Cancel, and Cancel imported
nothing, so there was no way to get a clean fresh import. These tests run the
real ``run_interactive_import`` (compiled from PDFImporterCmd.py with fake Qt
widgets) and the real ``discard_import_session`` from the importer core.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from PDFVectorImporter.src import PDFImporterCore as core

CMD_PATH = Path(__file__).resolve().parents[1] / "PDFVectorImporter/src/PDFImporterCmd.py"


# ── fake Qt ─────────────────────────────────────────────────────────────


class _Button:
    def __init__(self, text, role):
        self.text = text
        self.role = role


class FakeMessageBox:
    """Records the custom buttons and 'clicks' the one named by ``answer``."""

    Yes, Cancel, No = 0x4000, 0x400000, 0x10000
    Question = 4
    AcceptRole, RejectRole, DestructiveRole = 0, 1, 2
    answer = None  # button text the test wants clicked
    instances = []
    questions = []

    def __init__(self, parent=None):
        self.buttons = []
        self.default = None
        self.escape = None
        self.clicked = None
        self.title = self.text = ""
        FakeMessageBox.instances.append(self)

    def setWindowTitle(self, title):
        self.title = title

    def setText(self, text):
        self.text = text

    def setIcon(self, _icon):
        pass

    def addButton(self, text, role):
        button = _Button(text, role)
        self.buttons.append(button)
        return button

    def setDefaultButton(self, button):
        self.default = button

    def setEscapeButton(self, button):
        self.escape = button

    def exec(self):
        for button in self.buttons:
            if button.text.startswith(FakeMessageBox.answer):
                self.clicked = button
                return 0
        self.clicked = self.escape
        return 0

    def clickedButton(self):
        return self.clicked

    @staticmethod
    def question(parent, title, text, buttons, default):
        FakeMessageBox.questions.append((title, text))
        return FakeMessageBox.Yes


class FakeProgress:
    def __init__(self, parent=None):
        pass

    def set_plan(self, plan):
        pass

    def __call__(self, event):
        return True

    def close(self):
        pass


class FakeCore:
    class ImportCancelled(Exception):
        pass

    def __init__(self, resumable):
        self.resumable = resumable
        self.calls = []

    def estimate_import_work(self, pdf_path, opts):
        return {"pages": [{"page_number": 1}, {"page_number": 2}], "total_units": 0}

    def find_resumable_import_session(self, pdf_path, opts):
        return self.resumable

    def discard_import_session(self, session_state, fc_doc=None):
        self.calls.append(("discard", session_state["host"].Name))
        return {"removed_objects": 3,
                "page_groups": sorted(session_state["page_groups"].values())}

    def import_pdf(self, pdf_path, opts):
        self.calls.append(("import", getattr(opts, "resume_session_name", None)))
        return True


@pytest.fixture
def interactive():
    tree = ast.parse(CMD_PATH.read_text(encoding="utf-8"))
    wanted = {"format_import_work_summary", "run_interactive_import", "_ask_resume_choice"}
    body = [node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {node.name for node in body} == wanted
    messages = []
    namespace = {
        "QtWidgets": SimpleNamespace(QMessageBox=FakeMessageBox),
        "FreeCAD": SimpleNamespace(Console=SimpleNamespace(
            PrintMessage=messages.append, PrintWarning=messages.append)),
        "ImportProgressController": FakeProgress,
    }
    exec(compile(ast.Module(body=body, type_ignores=[]), str(CMD_PATH), "exec"), namespace)
    FakeMessageBox.instances = []
    FakeMessageBox.questions = []
    return namespace["run_interactive_import"], messages


def _resumable(done=1, requested=2):
    return {
        "completed_pages": list(range(1, done + 1)),
        "requested_pages": list(range(1, requested + 1)),
        "page_groups": {str(p): f"PDF_Page_{p}" for p in range(1, done + 1)},
        "host": SimpleNamespace(Name="PDF_Import_Session"),
    }


def _opts():
    return SimpleNamespace(resume_session_name=None)


def test_prompt_offers_resume_start_over_and_cancel(interactive):
    run, _messages = interactive
    FakeMessageBox.answer = "Resume"
    run(FakeCore(_resumable()), "x.pdf", _opts())

    box = FakeMessageBox.instances[-1]
    texts = [b.text for b in box.buttons]
    assert texts == ["Resume remaining pages",
                     "Start over (replace the 1 page already imported)",
                     "Cancel"]
    assert box.default.text == "Resume remaining pages"
    assert box.escape.text == "Cancel"
    assert "1 of 2 pages" in box.text


def test_resume_continues_the_saved_session(interactive):
    run, _messages = interactive
    FakeMessageBox.answer = "Resume"
    core_ = FakeCore(_resumable())
    opts = _opts()

    assert run(core_, "x.pdf", opts) is True

    assert core_.calls == [("import", "PDF_Import_Session")]
    assert opts.resume_session_name == "PDF_Import_Session"


def test_start_over_removes_the_old_pages_then_imports_fresh(interactive):
    run, messages = interactive
    FakeMessageBox.answer = "Start over"
    core_ = FakeCore(_resumable(done=3, requested=4))
    opts = _opts()

    assert run(core_, "x.pdf", opts) is True

    assert FakeMessageBox.instances[-1].buttons[1].text == (
        "Start over (replace the 3 pages already imported)")
    assert core_.calls == [("discard", "PDF_Import_Session"), ("import", None)]
    assert opts.resume_session_name is None
    assert any("Edit > Undo" in m for m in messages)


def test_cancel_leaves_everything_and_imports_nothing(interactive):
    run, _messages = interactive
    FakeMessageBox.answer = "Cancel"
    core_ = FakeCore(_resumable())
    opts = _opts()

    assert run(core_, "x.pdf", opts) is False

    assert core_.calls == []
    assert opts.import_status == "cancelled"


def test_closing_the_prompt_is_cancel(interactive):
    run, _messages = interactive
    FakeMessageBox.answer = "no such button"
    core_ = FakeCore(_resumable())

    assert run(core_, "x.pdf", _opts()) is False
    assert core_.calls == []


def test_fresh_import_still_asks_yes_or_cancel(interactive):
    run, _messages = interactive
    core_ = FakeCore(None)

    assert run(core_, "x.pdf", _opts()) is True

    assert FakeMessageBox.instances == []
    assert FakeMessageBox.questions[-1][0] == "PDF Import Work Estimate"
    assert core_.calls == [("import", None)]


# ── core: discard_import_session ────────────────────────────────────────


class FakeObj:
    def __init__(self, name, children=None):
        self.Name = name
        self.Label = name
        self.Group = list(children) if children is not None else None
        self.Document = None

    def isDerivedFrom(self, kind):
        return kind == "App::DocumentObjectGroup" and self.Group is not None


class FakeDoc:
    def __init__(self, objects):
        self.objects = {o.Name: o for o in objects}
        for obj in objects:
            obj.Document = self
        self.log = []

    @property
    def Objects(self):
        return list(self.objects.values())

    def getObject(self, name):
        return self.objects.get(name)

    def removeObject(self, name):
        assert self.log and self.log[0][0] == "open", "remove outside the undo step"
        self.log.append(("remove", name))
        del self.objects[name]

    def openTransaction(self, name):
        self.log.append(("open", name))

    def commitTransaction(self):
        self.log.append(("commit",))

    def abortTransaction(self):
        self.log.append(("abort",))


def _document():
    line = FakeObj("Batch_1")
    label = FakeObj("dLabel")
    color = FakeObj("Color_000000", [line])
    text = FakeObj("Text", [label])
    paper = FakeObj("PDF_Paper")
    page1 = FakeObj("PDF_Page_1", [color, text, paper])
    other_line = FakeObj("Batch_2")
    other_page = FakeObj("PDF_Page_7", [other_line])  # a different import
    user_part = FakeObj("MyBracket")  # the user's own work
    session = FakeObj("PDF_Import_Session")
    other_session = FakeObj("PDF_Import_Session001")
    return FakeDoc([line, label, color, text, paper, page1, other_line,
                    other_page, user_part, session, other_session])


def test_discard_removes_only_the_recorded_pages_and_session_in_one_undo_step():
    doc = _document()
    state = {"page_groups": {"1": "PDF_Page_1"},
             "host": doc.getObject("PDF_Import_Session")}

    result = core.discard_import_session(state, doc)

    assert doc.log[0] == ("open", "Start PDF import over")
    assert doc.log[-1] == ("commit",)
    removed = [entry[1] for entry in doc.log if entry[0] == "remove"]
    assert set(removed) == {"Batch_1", "dLabel", "Color_000000", "Text", "PDF_Paper",
                            "PDF_Page_1", "PDF_Import_Session"}
    # Children go before the group that holds them.
    assert removed.index("Batch_1") < removed.index("Color_000000")
    assert removed.index("Color_000000") < removed.index("PDF_Page_1")
    assert sorted(doc.objects) == ["Batch_2", "MyBracket", "PDF_Import_Session001",
                                   "PDF_Page_7"]
    assert result["removed_objects"] == 7
    assert result["page_groups"] == ["PDF_Page_1"]


def test_discard_skips_groups_that_are_already_gone():
    doc = _document()
    state = {"page_groups": {"1": "PDF_Page_1", "2": "PDF_Page_2"},
             "host": doc.getObject("PDF_Import_Session")}

    result = core.discard_import_session(state, doc)

    assert result["page_groups"] == ["PDF_Page_1"]
    assert "PDF_Page_7" in doc.objects


def test_discard_uses_the_session_document_when_none_is_given():
    doc = _document()
    state = {"page_groups": {"1": "PDF_Page_1"},
             "host": doc.getObject("PDF_Import_Session")}

    core.discard_import_session(state)

    assert "PDF_Page_1" not in doc.objects


def test_discard_failure_rolls_the_undo_step_back():
    doc = _document()
    state = {"page_groups": {"1": "PDF_Page_1"},
             "host": doc.getObject("PDF_Import_Session")}

    def broken(name):
        raise RuntimeError("cannot remove " + name)

    doc.removeObject = broken
    with pytest.raises(RuntimeError):
        core.discard_import_session(state, doc)
    assert doc.log == [("open", "Start PDF import over"), ("abort",)]
