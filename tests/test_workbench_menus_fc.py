"""Every workbench command a user is told about can be reached from a menu.

InitGui.py is executed the way FreeCAD runs it (exec of the file) against stub
FreeCAD/FreeCADGui modules, so the registered commands, menus and toolbars are
the ones the real workbench builds. Check Environment's picture check and the
too-old-FreeCAD message are tested the same way, without FreeCAD installed.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INIT_GUI = REPO_ROOT / "PDFVectorImporter" / "InitGui.py"

# Registered on purpose but kept off the menus, each for a stated reason.
OFF_MENU = {
    # Imports every page of every file into one document, stacked at the same
    # spot (audit FC-ui-03 / FC-11). Goes on the menu once Batch is fixed.
    "PDF_BatchImport",
    # Same job as the main Import dialog; kept for scripted use only.
    "PDF_ImportViaConsole",
}


class _Command:
    def __init__(self, text):
        self.text = text

    def GetResources(self):
        return {"MenuText": self.text}


def _command_module(name, **classes):
    module = types.ModuleType(name)
    for class_name, text in classes.items():
        setattr(module, class_name, lambda text=text: _Command(text))
    return module


@pytest.fixture
def workbench(monkeypatch, tmp_path):
    """Exec InitGui.py with stubs and return (workbench, log)."""
    log = {"commands": [], "menus": [], "toolbars": [], "console": [], "dialogs": []}
    (tmp_path / "Mod" / "PDFVectorImporter").mkdir(parents=True)

    fc = types.ModuleType("FreeCAD")
    fc.getUserAppDataDir = lambda: str(tmp_path)
    fc.getResourceDir = lambda: str(tmp_path / "none")
    fc.GuiUp = False
    fc.ActiveDocument = None

    class _Console:
        def __getattr__(self, name):
            return lambda text: log["console"].append((name, text.strip()))

    fc.Console = _Console()

    gui = types.ModuleType("FreeCADGui")

    class Workbench:
        def appendToolbar(self, name, cmds):
            log["toolbars"].append((name, list(cmds)))

        def appendMenu(self, name, cmds):
            log["menus"].append((name, list(cmds)))

    gui.Workbench = Workbench
    gui.addCommand = lambda name, obj: log["commands"].append(
        (name, obj.GetResources()["MenuText"]))
    added = []
    gui.addWorkbench = added.append

    for name, module in (
        ("FreeCAD", fc),
        ("FreeCADGui", gui),
        ("PDFImporterCmd", _command_module(
            "PDFImporterCmd", ImportPDFVectorCommand="Import PDF Vector…")),
        ("PDFScaleTool", _command_module(
            "PDFScaleTool", ScaleByReferenceCommand="Scale by Reference…",
            QuickScaleCommand="Quick Scale (drawing scale)…")),
        ("PDFTools", _command_module(
            "PDFTools", CheckEnvironmentCommand="Check Environment",
            ImportViaConsoleCommand="Import via Console…",
            BatchImportCommand="Batch Import…",
            InstallPyMuPDFCommand="Install / Update PDF Dependencies")),
        # Module-level style-restore hook: not under test here.
        ("PDFStyleRestore", types.SimpleNamespace(
            register_document_observer=lambda _fc: object())),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(sys, "path", list(sys.path))

    namespace = {"__name__": "InitGui"}
    exec(compile(INIT_GUI.read_text(encoding="utf-8"), str(INIT_GUI), "exec"), namespace)
    wb = added[0]
    wb._activate_bundled_runtime = lambda _base: None
    return wb, log, fc


def _on_ui(log):
    found = set()
    for _name, cmds in log["menus"] + log["toolbars"]:
        found.update(cmds)
    return found


def test_every_registered_command_is_on_a_menu_or_toolbar(workbench):
    wb, log, _fc = workbench
    wb.Initialize()

    registered = {name for name, _text in log["commands"]}
    assert {"ImportPDFVector", "PDF_ScaleByReference", "PDF_QuickScale",
            "PDF_CheckEnv", "PDF_InstallPyMuPDF"} <= registered
    missing = registered - _on_ui(log) - OFF_MENU
    assert not missing, f"registered but unreachable: {sorted(missing)}"
    assert not [c for c in log["console"] if c[0] == "PrintError"]


def test_tools_submenu_holds_install_and_check_environment(workbench):
    wb, log, _fc = workbench
    wb.Initialize()

    tools = [cmds for name, cmds in log["menus"]
             if name == ["PDF Vector Importer", "Tools"]]
    assert tools == [["PDF_InstallPyMuPDF", "PDF_CheckEnv"]]
    texts = dict(log["commands"])
    assert texts["PDF_InstallPyMuPDF"] == "Install / Update PDF Dependencies"
    assert texts["PDF_CheckEnv"] == "Check Environment"
    # Menu only: the toolbar keeps the everyday buttons.
    for _name, cmds in log["toolbars"]:
        assert "PDF_InstallPyMuPDF" not in cmds
        assert "PDF_CheckEnv" not in cmds


@pytest.mark.parametrize("version,expected", [
    ((3, 8, 10), "This FreeCAD uses Python 3.8. PDF Vector Importer needs "
                 "FreeCAD 1.0 or newer (Python 3.10+)."),
    ((3, 9, 0), "This FreeCAD uses Python 3.9. PDF Vector Importer needs "
                "FreeCAD 1.0 or newer (Python 3.10+)."),
    ((3, 10, 0), None),
    ((3, 11, 14), None),
    ((3, 13, 1), None),
])
def test_old_python_message(workbench, version, expected):
    wb, _log, _fc = workbench
    assert wb._too_old_python_message(version) == expected


def test_old_python_gets_plain_message_and_no_install_attempt(workbench, monkeypatch):
    wb, log, _fc = workbench
    monkeypatch.setattr(sys, "version_info", (3, 8, 10, "final", 0))
    offered = []
    wb._offer_install = lambda *a: offered.append(a)

    wb.Initialize()
    wb.Activated()

    errors = [text for kind, text in log["console"] if kind == "PrintError"]
    assert any("needs FreeCAD 1.0 or newer" in text for text in errors)
    assert offered == []
    # Nothing that would crash on this Python is put on the menus.
    assert log["commands"] == []
    assert log["menus"] == [] and log["toolbars"] == []


def test_current_python_still_offers_missing_dependencies(workbench, monkeypatch):
    wb, _log, _fc = workbench
    offered = []
    wb._offer_install = lambda base, missing: offered.append(missing)
    monkeypatch.setitem(sys.modules, "pymupdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)

    wb.Activated()

    assert offered and "PyMuPDF" in offered[0]


# ── Check Environment: picture support on FreeCAD 1.x ─────────────────


def _tools(monkeypatch, fc):
    monkeypatch.setitem(sys.modules, "FreeCAD", fc)
    monkeypatch.setitem(sys.modules, "FreeCADGui", types.ModuleType("FreeCADGui"))
    monkeypatch.delitem(sys.modules, "PDFTools", raising=False)
    monkeypatch.syspath_prepend(str(REPO_ROOT / "PDFVectorImporter"))
    import importlib

    tools = importlib.import_module("PDFTools")
    monkeypatch.setitem(sys.modules, "PDFTools", tools)
    return tools


class _Doc:
    def __init__(self, types_list):
        self.types_list = types_list
        self.Name = "Probe"

    def supportedTypes(self):
        return list(self.types_list)


def test_image_plane_supported_from_the_active_document(workbench, monkeypatch):
    _wb, _log, fc = workbench
    fc.ActiveDocument = _Doc(["Part::Feature", "Image::ImagePlane"])
    tools = _tools(monkeypatch, fc)
    monkeypatch.setitem(sys.modules, "ImageGui", None)  # FreeCAD 1.x has none
    assert tools._image_plane_supported() is True


def test_image_plane_missing_is_reported_false(workbench, monkeypatch):
    _wb, _log, fc = workbench
    fc.ActiveDocument = _Doc(["Part::Feature"])
    tools = _tools(monkeypatch, fc)
    monkeypatch.setitem(sys.modules, "Image", None)
    monkeypatch.setitem(sys.modules, "ImageGui", None)
    assert tools._image_plane_supported() is False


def test_image_plane_probe_uses_a_temporary_document_and_closes_it(workbench, monkeypatch):
    _wb, _log, fc = workbench
    fc.ActiveDocument = None
    made, closed = [], []

    def new_document(name, *args, **kwargs):
        made.append((name, args, kwargs))
        return _Doc(["Image::ImagePlane"])

    fc.newDocument = new_document
    fc.closeDocument = closed.append
    tools = _tools(monkeypatch, fc)
    monkeypatch.setitem(sys.modules, "ImageGui", None)

    assert tools._image_plane_supported() is True
    assert len(made) == 1 and closed == ["Probe"]


# ── Importer core: options-less imports keep pictures on FreeCAD 1.x ─────


def test_core_image_import_check_uses_document_types(monkeypatch):
    from PDFVectorImporter.src import PDFImporterCore as core

    fake_fc = types.SimpleNamespace(ActiveDocument=None)
    monkeypatch.setattr(core, "FreeCAD", fake_fc)
    monkeypatch.setattr(core, "_IMAGE_IMPORT_AVAILABLE", None)
    monkeypatch.setitem(sys.modules, "ImageGui", None)  # FreeCAD 1.x has none

    assert core._image_import_available(_Doc(["Image::ImagePlane"])) is True
    # Worked out once per session.
    assert core._image_import_available(_Doc([])) is True


def test_core_image_import_check_reports_missing_type(monkeypatch):
    from PDFVectorImporter.src import PDFImporterCore as core

    fake_fc = types.SimpleNamespace(ActiveDocument=_Doc(["Part::Feature"]))
    monkeypatch.setattr(core, "FreeCAD", fake_fc)
    monkeypatch.setattr(core, "_IMAGE_IMPORT_AVAILABLE", None)
    monkeypatch.setitem(sys.modules, "Image", None)
    monkeypatch.setitem(sys.modules, "ImageGui", None)

    assert core._image_import_available() is False


def test_core_has_no_import_time_image_flag():
    from PDFVectorImporter.src import PDFImporterCore as core

    assert not hasattr(core, "IMAGE_WB")
    source = (REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCore.py").read_text(
        encoding="utf-8")
    assert "ignore_images=not IMAGE_WB" not in source
    assert source.count("ImportOptions(ignore_images=not _image_import_available(") == 3
