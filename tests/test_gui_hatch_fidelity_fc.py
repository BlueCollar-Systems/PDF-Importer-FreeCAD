"""Exercise the real operator option builder without creating a native GUI."""
from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from PDFVectorImporter.adapters import freecad_harness
from PDFVectorImporter.pdfcadcore.import_config import ImportConfig
from PDFVectorImporter.src import PDFImporterCore as core


def _dialog_class():
    # Compile the actual class constants and option builder. Only the Qt base,
    # widget construction, and other unrelated GUI methods are omitted.
    path = Path(__file__).resolve().parents[1] / "PDFVectorImporter/src/PDFImporterCmd.py"
    module = ast.parse(path.read_text(encoding="utf8"))
    dialog = next(n for n in module.body if isinstance(n, ast.ClassDef)
                  and n.name == "ImportPDFDialog")
    dialog.bases = []
    dialog.body = [n for n in dialog.body if isinstance(n, ast.Assign)
                   or isinstance(n, ast.FunctionDef) and n.name == "build_options"]
    namespace = {"SHAPE_EXTRUSION_UI_ENABLED": False}
    exec(compile(ast.Module(body=[dialog], type_ignores=[]), str(path), "exec"), namespace)
    return namespace["ImportPDFDialog"]


@pytest.mark.parametrize("label,expected", [
    ("Text", "text"), ("Labels", "labels"), ("3D Text", "3d_text"),
    ("Glyphs", "glyphs"), ("Geometry", "geometry"), ("Raster", "raster"),
])
@pytest.mark.parametrize("strategy", [None, "Vector", "Hybrid", "Raster"])
def test_operator_keeps_visible_hatching_for_every_requested_text_mode(label, expected, strategy):
    dialog = _dialog_class()()
    dialog.advanced_group = SimpleNamespace(isChecked=lambda: strategy is not None)
    dialog.mode_combo = SimpleNamespace(currentText=lambda: strategy or "Auto")
    dialog.import_text_chk = SimpleNamespace(isChecked=lambda: True)
    dialog.text_combo = SimpleNamespace(currentText=lambda: label)
    dialog.scale_spin = SimpleNamespace(value=lambda: 25.4 / 72)
    dialog.grouping_combo = SimpleNamespace(currentText=lambda: "Per Page")
    dialog.page_arrangement_combo = SimpleNamespace(currentText=lambda: "Spread (20% gap)")
    dialog._parse_pages = lambda: [1, 3]

    options = dialog.build_options()

    # This is the real ImportOptions object delivered by the UI to import_pdf.
    # Before the correction all 24 cases selected the hidden-hatch path.
    assert isinstance(options, core.ImportOptions)
    assert options.hatch_mode == "import"
    assert options.text_mode == expected
    assert options.pages == [1, 3]
    assert options.import_mode == (strategy.lower() if strategy else "auto")
    assert options.make_faces is (strategy != "Raster")
    assert options.ignore_images is (strategy == "Raster")


@pytest.mark.parametrize("explicit_mode", ["import", "group", "skip"])
def test_scripted_hatch_choice_is_not_overridden_by_operator_default(explicit_mode):
    config = ImportConfig.auto()
    config.hatch_mode = explicit_mode
    options = freecad_harness.build_import_options(core, config, [1])
    assert options.hatch_mode == explicit_mode
    assert config.hatch_mode == explicit_mode
