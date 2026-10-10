"""Every Grouping choice in the import dialog must change the model tree it names.

The dialog used to list six grouping choices that set an option nothing read,
so every choice produced the same tree. These tests run the real dialog code
(compiled from PDFImporterCmd.py without the Qt base class) and check that each
remaining label reaches the core as a distinct ``layer_mode`` the core builds,
that saved choices from older versions are carried over, and that the Text Mode
tooltip tells the user which choices stay editable as words.
"""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from PDFVectorImporter.src import PDFImporterCore as core

CMD_PATH = Path(__file__).resolve().parents[1] / "PDFVectorImporter/src/PDFImporterCmd.py"
RECOMMENDED = "Page > PDF layers, else colors (recommended)"


class _FakeParams:
    def __init__(self, strings=None):
        self.strings = dict(strings or {})
        self.saved = {}

    def GetString(self, name, default=""):
        return self.strings.get(name, default)

    def GetBool(self, name, default=False):
        return default

    def GetFloat(self, name, default=0.0):
        return default

    def SetString(self, name, value):
        self.saved[name] = value

    def SetBool(self, name, value):
        self.saved[name] = value

    def SetFloat(self, name, value):
        self.saved[name] = value


class _Widget:
    def __init__(self, text="", checked=True, value=0.0):
        self.text = text
        self.checked = checked
        self.number = value

    def currentText(self):
        return self.text

    def setCurrentText(self, text):
        self.text = text

    def isChecked(self):
        return self.checked

    def setChecked(self, checked):
        self.checked = bool(checked)

    def setEnabled(self, _enabled):
        pass

    def value(self):
        return self.number

    def setValue(self, number):
        self.number = number


def _dialog_class(params=None):
    module = ast.parse(CMD_PATH.read_text(encoding="utf-8"))
    dialog = next(n for n in module.body if isinstance(n, ast.ClassDef)
                  and n.name == "ImportPDFDialog")
    dialog.bases = []
    dialog.body = [
        n for n in dialog.body
        if isinstance(n, ast.Assign)
        or isinstance(n, ast.FunctionDef) and n.name != "__init__"
    ]
    fake_freecad = SimpleNamespace(ParamGet=lambda _path: params)
    namespace = {"SHAPE_EXTRUSION_UI_ENABLED": False, "FreeCAD": fake_freecad}
    exec(compile(ast.Module(body=[dialog], type_ignores=[]), str(CMD_PATH), "exec"),
         namespace)
    return namespace["ImportPDFDialog"]


def _dialog(grouping_label, params=None):
    dialog = _dialog_class(params)()
    dialog.advanced_group = _Widget(checked=False)
    dialog.mode_combo = _Widget("Auto")
    dialog.import_text_chk = _Widget(checked=True)
    dialog.text_combo = _Widget("3D Text")
    dialog.scale_spin = _Widget(value=25.4 / 72)
    dialog.grouping_combo = _Widget(grouping_label)
    dialog.page_arrangement_combo = _Widget("Spread (20% gap)")
    dialog.model3d_combo = _Widget("Off")
    dialog.model3d_depth_spin = _Widget(value=3.175)
    dialog._parse_pages = lambda: [1]
    return dialog


def _combo_items(source, widget):
    """Return the literal item list given to ``<widget>.addItems([...])``."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "addItems"
                and isinstance(node.func.value, ast.Attribute)
                and node.func.value.attr == widget):
            return [ast.literal_eval(item) for item in node.args[0].elts]
    raise AssertionError(f"{widget}.addItems([...]) not found")


def _layer_mode_map():
    return _dialog_class()._GROUPING_LAYER_MODE


def test_dropdown_lists_exactly_the_choices_the_core_builds():
    items = _combo_items(CMD_PATH.read_text(encoding="utf-8"), "grouping_combo")
    assert items == list(_layer_mode_map())
    assert items[0] == RECOMMENDED


@pytest.mark.parametrize("label,expected", [
    (RECOMMENDED, "auto"),
    ("Page > colors", "color"),
    ("Page > PDF layers", "ocg"),
    ("Page only (no layer or color sub-groups)", "none"),
])
def test_each_grouping_label_reaches_the_core_as_its_layer_mode(label, expected):
    options = _dialog(label).build_options()
    assert isinstance(options, core.ImportOptions)
    assert options.layer_mode == expected
    # The dead option nothing reads is gone, so it can no longer pretend.
    assert not hasattr(options, "grouping_mode")


def test_every_choice_is_distinct():
    modes = [_dialog(label).build_options().layer_mode for label in _layer_mode_map()]
    assert sorted(modes) == ["auto", "color", "none", "ocg"]


def test_recommended_default_keeps_todays_output():
    options = _dialog(RECOMMENDED).build_options()
    defaults = core.ImportOptions()
    assert options.layer_mode == defaults.layer_mode == "auto"
    assert options.group_by_color is True
    assert options.create_top_group is True


@pytest.mark.parametrize("saved,expected", [
    ("Per Color", "Page > colors"),
    ("Per Layer", "Page > PDF layers"),
    ("Nested Page>Layer", "Page > PDF layers"),
    ("Single", RECOMMENDED),
    ("Per Page", RECOMMENDED),
    ("Nested Page>Lineweight", RECOMMENDED),
    ("something unknown", RECOMMENDED),
    ("", RECOMMENDED),
    ("Page > colors", "Page > colors"),
    ("Page only (no layer or color sub-groups)", "Page only (no layer or color sub-groups)"),
])
def test_saved_grouping_from_older_versions_is_carried_over(saved, expected):
    params = _FakeParams({"LastGroupingMode": saved})
    dialog = _dialog(RECOMMENDED, params)
    dialog.grouping_combo.text = "stale"

    dialog._restore_settings()

    assert dialog.grouping_combo.currentText() == expected
    assert dialog.grouping_combo.currentText() in _layer_mode_map()


def test_save_settings_stores_the_new_label():
    params = _FakeParams()
    dialog = _dialog("Page > colors", params)
    dialog._save_settings()
    assert params.saved["LastGroupingMode"] == "Page > colors"


def test_unbuilt_groupings_are_not_offered():
    # create_top_group=False crashes _make_group and no lineweight grouping
    # exists in the core, so neither may come back as a choice.
    labels = " ".join(_layer_mode_map())
    assert "Single" not in labels
    assert "Lineweight" not in labels


def test_grouping_tooltip_explains_pdfs_without_layers():
    source = CMD_PATH.read_text(encoding="utf-8")
    assert "A PDF without layers gets no layer sub-groups" in source
    for old in ("Single = everything in one group", "Nested Page>Lineweight"):
        assert old not in source


def test_text_mode_tooltip_says_3d_text_cannot_be_retyped():
    source = CMD_PATH.read_text(encoding="utf-8")
    assert "cannot be retyped" in source
    assert "choose Text or Labels to edit words" in source
    assert "Labels — FreeCAD Draft labels, editable" in source
