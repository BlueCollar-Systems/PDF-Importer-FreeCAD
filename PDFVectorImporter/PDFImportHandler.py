# -*- coding: utf-8 -*-
# PDFImportHandler.py — FreeCAD file import handler for .pdf
# BlueCollar Systems — BUILT. NOT BOUGHT.
#
# FreeCAD calls open() when a .pdf is opened (drag-drop, File→Open)
# and insert() when File→Import is used into an existing document.
#
# Both paths can either:
#   - Show the full options dialog (if GUI is up)
#   - Import silently with defaults (headless / batch mode)
import os
import sys

import FreeCAD

# Ensure our src and lib directories are importable
_candidates = []
for root in (FreeCAD.getUserAppDataDir(), FreeCAD.getResourceDir()):
    d = os.path.join(root, "Mod", "PDFVectorImporter")
    if os.path.isdir(d):
        _candidates.append(d)
        break

for _base in _candidates:
    _src = os.path.join(_base, "src")
    _lib = os.path.join(_src, "lib")
    for _p in (os.path.dirname(_base), _base, _src):
        if not _p:
            continue
        try:
            while _p in sys.path:
                sys.path.remove(_p)
        except (AttributeError, ValueError):
            pass
        sys.path.insert(0, _p)
    if os.path.isdir(_lib):
        try:
            while _lib in sys.path:
                sys.path.remove(_lib)
        except (AttributeError, ValueError):
            pass
        sys.path.insert(0, _lib)


def open(filename, docname=None):
    """Called by FreeCAD when a PDF is opened (drag-drop, File→Open).
    Creates a new document and imports into it."""
    if not _check_fitz():
        return

    # Create a new document named after the PDF
    basename = os.path.splitext(os.path.basename(filename))[0]
    doc = FreeCAD.newDocument(docname or basename)
    FreeCAD.setActiveDocument(doc.Name)

    _do_import(filename)


def insert(filename, docname):
    """Called by FreeCAD when a PDF is imported into an existing document."""
    if not _check_fitz():
        return

    try:
        doc = FreeCAD.getDocument(docname)
    except (RuntimeError, TypeError, ValueError):
        doc = None

    if doc is None:
        doc = FreeCAD.newDocument(docname or "PDF_Import")
    FreeCAD.setActiveDocument(doc.Name)

    _do_import(filename)


def _check_fitz():
    """Verify PyMuPDF is available; show install prompt if not."""
    try:
        try:
            import pymupdf as fitz  # noqa: F401  # PyMuPDF >= 1.24 preferred name
        except ImportError:
            import fitz  # noqa: F401  # Legacy fallback
        return True
    except ImportError:
        pass

    FreeCAD.Console.PrintError(
        "PyMuPDF is not installed. Switch to the PDF Vector Importer "
        "workbench to install it automatically.\n")

    if FreeCAD.GuiUp:
        try:
            from PySide6 import QtWidgets
        except ImportError:
            from PySide2 import QtWidgets
        QtWidgets.QMessageBox.warning(
            None, "PyMuPDF Required",
            "PyMuPDF is not installed yet.\n\n"
            "Switch to the PDF Vector Importer workbench\n"
            "and it will install automatically.")
    return False


def _do_import(filename):
    """Run the import — show dialog if GUI is up, otherwise use defaults."""
    if FreeCAD.GuiUp:
        _import_with_dialog(filename)
    else:
        _import_headless(filename)


def _import_with_dialog(filename):
    """Show the options dialog pre-filled with the dropped file."""
    try:
        from PDFImporterCmd import ImportPDFDialog
        import PDFVectorImporter.src.PDFImporterCore as core
    except ImportError:
        # Fallback: try direct import
        try:
            import PDFImporterCore as core
            from PDFImporterCmd import ImportPDFDialog
        except ImportError as e:
            FreeCAD.Console.PrintError(f"Cannot load importer: {e}\n")
            return

    dlg = ImportPDFDialog()
    dlg.file_edit.setText(filename)

    # Pre-populate page count
    try:
        from pdfcadcore.fitz_loader import PdfOpenError, safe_open

        with safe_open(filename) as doc:
            page_count = doc.page_count
        dlg._page_count = page_count
        dlg.page_edit.setPlaceholderText(
            f"1-{page_count}  (PDF has {page_count} pages)")
    except PdfOpenError as exc:
        FreeCAD.Console.PrintWarning(f"{exc}\n")
    except (ImportError, OSError, RuntimeError, ValueError):
        pass

    try:
        from PySide6 import QtWidgets
    except ImportError:
        from PySide2 import QtWidgets

    exec_fn = getattr(dlg, "exec", None) or getattr(dlg, "exec_", None)
    if exec_fn is None or exec_fn() != QtWidgets.QDialog.Accepted:
        return

    opts = dlg.build_options()
    try:
        core.import_pdf(filename, opts)
        FreeCAD.Console.PrintMessage("PDF import complete.\n")

        # Fit view
        try:
            import FreeCADGui
            if FreeCADGui.ActiveDocument and FreeCADGui.ActiveDocument.ActiveView:
                FreeCADGui.ActiveDocument.ActiveView.fitAll()
        except (ImportError, AttributeError, RuntimeError):
            pass
    except (RuntimeError, ValueError, TypeError, OSError, AttributeError, ImportError) as e:
        from pdfcadcore.fitz_loader import PdfOpenError

        if isinstance(e, PdfOpenError):
            FreeCAD.Console.PrintError(f"Import failed: {e}\n")
            try:
                from PySide6 import QtWidgets
            except ImportError:
                from PySide2 import QtWidgets
            QtWidgets.QMessageBox.warning(None, "PDF Import", str(e))
            return
        import traceback
        FreeCAD.Console.PrintError(f"Import failed: {e}\n{traceback.format_exc()}")
        try:
            from PySide6 import QtWidgets
        except ImportError:
            from PySide2 import QtWidgets

        # Provide targeted error messages for common failure modes
        msg = str(e)
        if "encrypt" in msg.lower():
            title = "Encrypted PDF"
        elif "fitz" in msg.lower() or "pymupdf" in msg.lower():
            title = "PyMuPDF Error"
        else:
            title = "Import Failed"
        QtWidgets.QMessageBox.critical(None, title, msg)


def _import_headless(filename):
    """Import with default options (no GUI)."""
    try:
        import PDFVectorImporter.src.PDFImporterCore as core
    except ImportError:
        import PDFImporterCore as core

    opts = core.ImportOptions()
    try:
        core.import_pdf(filename, opts)
        FreeCAD.Console.PrintMessage("PDF import complete.\n")
    except (RuntimeError, ValueError, TypeError, OSError, AttributeError, ImportError) as e:
        import traceback
        FreeCAD.Console.PrintError(f"Import failed: {e}\n{traceback.format_exc()}")


