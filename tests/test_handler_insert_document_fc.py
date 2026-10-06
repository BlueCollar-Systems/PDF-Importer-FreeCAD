"""Exercise the registered handler against the host's document lookup results."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


@pytest.fixture
def handler(tmp_path):
    host = SimpleNamespace(
        getUserAppDataDir=lambda: str(tmp_path),
        getResourceDir=lambda: str(tmp_path),
        getDocument=Mock(),
        newDocument=Mock(return_value=SimpleNamespace(Name="CreatedPDF")),
        setActiveDocument=Mock(),
    )
    path = Path(__file__).resolve().parents[1] / "PDFVectorImporter/PDFImportHandler.py"
    spec = importlib.util.spec_from_file_location("isolated_pdf_import_handler", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict("sys.modules", {"FreeCAD": host}):
        spec.loader.exec_module(module)
    module._check_fitz = Mock(return_value=True)
    module._do_import = Mock()
    return module, host


@pytest.mark.parametrize("error", [NameError, RuntimeError, TypeError, ValueError])
def test_missing_named_target_creates_before_import(handler, error):
    module, host = handler
    host.getDocument.side_effect = error("Unknown document 'Unnamed'")

    module.insert("drawing.pdf", "Unnamed")

    host.getDocument.assert_called_once_with("Unnamed")
    host.newDocument.assert_called_once_with("Unnamed")
    host.setActiveDocument.assert_called_once_with("CreatedPDF")
    module._do_import.assert_called_once_with("drawing.pdf")


def test_existing_target_is_preserved(handler):
    module, host = handler
    host.getDocument.return_value = SimpleNamespace(Name="ExistingShopDrawing")

    module.insert("drawing.pdf", "ExistingShopDrawing")

    host.newDocument.assert_not_called()
    host.setActiveDocument.assert_called_once_with("ExistingShopDrawing")
    module._do_import.assert_called_once_with("drawing.pdf")


def test_absent_unnamed_target_uses_default(handler):
    module, host = handler
    host.getDocument.return_value = None

    module.insert("drawing.pdf", "")

    host.newDocument.assert_called_once_with("PDF_Import")
    module._do_import.assert_called_once_with("drawing.pdf")


def test_unexpected_lookup_failure_is_not_hidden(handler):
    module, host = handler
    host.getDocument.side_effect = OSError("Document service unavailable")

    with pytest.raises(OSError, match="Document service unavailable"):
        module.insert("drawing.pdf", "ExistingShopDrawing")

    host.newDocument.assert_not_called()
    host.setActiveDocument.assert_not_called()
    module._do_import.assert_not_called()


def test_dependency_failure_never_creates_or_imports(handler):
    module, host = handler
    module._check_fitz.return_value = False

    module.insert("drawing.pdf", "Unnamed")

    host.getDocument.assert_not_called()
    host.newDocument.assert_not_called()
    module._do_import.assert_not_called()


def test_dialog_imports_the_pdf_selected_after_drag_and_drop(handler, monkeypatch):
    module, host = handler
    host.Console = SimpleNamespace(PrintMessage=Mock(), PrintError=Mock(), PrintWarning=Mock())
    file_field = SimpleNamespace(value="")
    file_field.setText = lambda value: setattr(file_field, "value", value)
    file_field.text = lambda: file_field.value
    options = SimpleNamespace()
    dialog = SimpleNamespace(
        file_edit=file_field,
        page_edit=SimpleNamespace(setPlaceholderText=Mock()),
        build_options=Mock(return_value=options),
    )

    def accept_selected_pdf():
        file_field.value = "  chosen.pdf  "
        return 1

    dialog.exec = accept_selected_pdf
    importer = Mock(return_value=True)
    monkeypatch.setitem(sys.modules, "PDFImporterCmd", SimpleNamespace(
        ImportPDFDialog=lambda: dialog,
        run_interactive_import=importer,
    ))
    monkeypatch.setitem(sys.modules, "PySide6", SimpleNamespace(
        QtWidgets=SimpleNamespace(QDialog=SimpleNamespace(Accepted=1))
    ))

    module._import_with_dialog("dropped.pdf")

    importer.assert_called_once()
    assert importer.call_args.args[1:] == ("chosen.pdf", options)
