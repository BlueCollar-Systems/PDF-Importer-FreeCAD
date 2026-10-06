"""Exercise real dialog methods with PDF files and lightweight widget adapters."""
from __future__ import annotations

import ast
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from PDFVectorImporter.pdfcadcore.fitz_loader import PdfOpenError, safe_open


class Field:
    def __init__(self, value=""):
        self.value = value
        self.placeholder = ""

    def text(self):
        return self.value

    def setPlaceholderText(self, value):
        self.placeholder = value


@pytest.fixture
def dialog():
    path = Path(__file__).resolve().parents[1] / "PDFVectorImporter/src/PDFImporterCmd.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name == "ImportPDFDialog")
    cls.bases = []
    cls.body = [node for node in cls.body if isinstance(node, ast.FunctionDef)
                and node.name != "__init__" or isinstance(node, ast.Assign)]
    namespace = {"os": os, "safe_open": safe_open, "PdfOpenError": PdfOpenError,
                 "QtWidgets": SimpleNamespace(QMessageBox=SimpleNamespace(warning=Mock()))}
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(path), "exec"), namespace)
    instance = namespace["ImportPDFDialog"]()
    instance._page_count = None
    instance.file_edit = Field()
    instance.page_edit = Field("All")
    instance.accept = Mock()
    instance._save_settings = Mock()
    return instance, namespace


def _pdf(path, pages):
    with fitz.open() as document:
        for _ in range(pages):
            document.new_page()
        document.save(str(path))


def test_typing_multipage_pdf_and_all_validates_every_page(dialog, tmp_path):
    instance, namespace = dialog
    path = tmp_path / "typed.pdf"
    _pdf(path, 4)
    instance.file_edit.value = str(path)

    instance._validate_and_accept()

    assert instance._parse_pages() == [1, 2, 3, 4]
    instance.accept.assert_called_once()
    namespace["QtWidgets"].QMessageBox.warning.assert_not_called()


def test_switching_file_replaces_stale_page_count_before_validation(dialog, tmp_path):
    instance, namespace = dialog
    old = tmp_path / "old.pdf"
    selected = tmp_path / "selected.pdf"
    _pdf(old, 8)
    _pdf(selected, 2)
    instance._page_count = 8
    instance.file_edit.value = str(selected)
    instance.page_edit.value = "3"

    instance._validate_and_accept()

    instance.accept.assert_not_called()
    assert instance._page_count == 2
    warning = namespace["QtWidgets"].QMessageBox.warning
    warning.assert_called_once()
    assert "out of range" in warning.call_args.args[2]


def test_invalid_pdf_never_accepts_a_stale_selection(dialog, tmp_path):
    instance, namespace = dialog
    path = tmp_path / "not-a-pdf.pdf"
    path.write_text("This is not a PDF", encoding="utf-8")
    instance.file_edit.value = str(path)
    instance._page_count = 4

    instance._validate_and_accept()

    instance.accept.assert_not_called()
    assert instance._page_count is None
    namespace["QtWidgets"].QMessageBox.warning.assert_called_once()


def test_unknown_page_count_keeps_all_selection_for_the_core(dialog):
    instance, _namespace = dialog
    assert instance._parse_pages() == []


def test_out_of_range_selection_is_rejected_before_allocating_the_range(dialog):
    instance, namespace = dialog
    instance._page_count = 4
    instance.page_edit.value = "1-999999999999"
    namespace["range"] = Mock(side_effect=AssertionError("must validate before expanding"))

    with pytest.raises(ValueError, match="out of range"):
        instance._parse_pages()

    namespace["range"].assert_not_called()
