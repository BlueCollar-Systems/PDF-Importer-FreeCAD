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

# Ensure our source and ABI-matched runtime directories are importable.
_candidates = []
for root in (FreeCAD.getUserAppDataDir(), FreeCAD.getResourceDir()):
    d = os.path.join(root, "Mod", "PDFVectorImporter")
    if os.path.isdir(d):
        _candidates.append(d)
        break

for _base in _candidates:
    _src = os.path.join(_base, "src")
    for _p in (os.path.dirname(_base), _base, _src):
        if not _p:
            continue
        try:
            while _p in sys.path:
                sys.path.remove(_p)
        except (AttributeError, ValueError):
            pass
        sys.path.insert(0, _p)
    from PDFVectorImporter.runtime_paths import activate_bundled_runtime_if_available

    activate_bundled_runtime_if_available(_base)


def open(filename, docname=None):
    """Called by FreeCAD when a PDF is opened (drag-drop, File→Open).
    Creates a new document and imports into it."""
    if not _check_fitz():
        return

    # Create a new document named after the PDF
    basename = os.path.splitext(os.path.basename(filename))[0]
    doc = FreeCAD.newDocument(docname or basename)
    FreeCAD.setActiveDocument(doc.Name)

    if not _do_import(filename):
        # Cancelled, or failed and rolled back: do not leave an empty tab
        # behind. A document that holds anything (kept pages) stays open.
        _close_if_empty(doc)


def insert(filename, docname):
    """Called by FreeCAD when a PDF is imported into an existing document."""
    if not _check_fitz():
        return

    try:
        doc = FreeCAD.getDocument(docname)
    except (NameError, RuntimeError, TypeError, ValueError):
        doc = None

    if doc is None:
        doc = FreeCAD.newDocument(docname or "PDF_Import")
    FreeCAD.setActiveDocument(doc.Name)

    _do_import(filename)


def _close_if_empty(doc):
    """Close a document this handler created when nothing ended up in it."""
    try:
        if list(getattr(doc, "Objects", []) or []):
            return False
        FreeCAD.closeDocument(doc.Name)
        return True
    except (AttributeError, NameError, RuntimeError, TypeError, ValueError):
        return False


def _outcome_helpers():
    """The shared outcome messages, when the command module provides them."""
    try:
        import PDFImporterCmd as command
    except ImportError:
        return None, None
    return (getattr(command, "show_import_failure", None),
            getattr(command, "show_import_result", None))


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
    """Run the import — show dialog if GUI is up, otherwise use defaults.

    Returns True only when something was imported.
    """
    if FreeCAD.GuiUp:
        return bool(_import_with_dialog(filename))
    return bool(_import_headless(filename))


def _import_with_dialog(filename):
    """Show the options dialog pre-filled with the dropped file."""
    try:
        from PDFImporterCmd import ImportPDFDialog, run_interactive_import
        import PDFVectorImporter.src.PDFImporterCore as core
    except ImportError:
        # Fallback: try direct import
        try:
            import PDFImporterCore as core
            from PDFImporterCmd import ImportPDFDialog, run_interactive_import
        except ImportError as e:
            FreeCAD.Console.PrintError(f"Cannot load importer: {e}\n")
            return False

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
        return False

    # The user can choose a different PDF in the prefilled dialog.
    filename = dlg.file_edit.text().strip()
    opts = dlg.build_options()
    show_failure, show_result = _outcome_helpers()
    try:
        completed = run_interactive_import(core, filename, opts)
    except Exception as e:  # the outermost UI layer: every failure gets one plain box
        from pdfcadcore.fitz_loader import PdfOpenError

        if isinstance(e, PdfOpenError):
            FreeCAD.Console.PrintError(f"Import failed: {e}\n")
            try:
                from PySide6 import QtWidgets
            except ImportError:
                from PySide2 import QtWidgets
            QtWidgets.QMessageBox.warning(None, "PDF Import", str(e))
            return False

        # Provide targeted titles for common failure modes
        msg = str(e)
        if "encrypt" in msg.lower():
            title = "Encrypted PDF"
        elif "fitz" in msg.lower() or "pymupdf" in msg.lower():
            title = "PyMuPDF Error"
        else:
            title = "Import Failed"
        if show_failure is not None:
            show_failure(e, opts, title=title)
            return False
        import traceback
        FreeCAD.Console.PrintError(f"Import failed: {e}\n{traceback.format_exc()}")
        try:
            from PySide6 import QtWidgets
        except ImportError:
            from PySide2 import QtWidgets
        QtWidgets.QMessageBox.critical(None, title, msg)
        return False
    if not completed:
        return False
    FreeCAD.Console.PrintMessage("PDF import complete.\n")
    if show_result is not None:
        show_result(opts)
    # Keep the core's final top-orthographic fit to the imported sheets.
    return True


def _import_headless(filename):
    """Import with default options (no GUI)."""
    try:
        import PDFVectorImporter.src.PDFImporterCore as core
    except ImportError:
        import PDFImporterCore as core

    opts = core.ImportOptions()
    try:
        completed = core.import_pdf(filename, opts)
    except Exception as e:  # headless: never a box, always the reason and the report
        import traceback
        report = (getattr(e, "bcs_report_path", "")
                  or getattr(opts, "_last_import_report_path", "") or "not written")
        FreeCAD.Console.PrintError(
            f"Import failed: {e}. Report: {report}\n{traceback.format_exc()}")
        return False
    if not completed:
        return False
    FreeCAD.Console.PrintMessage("PDF import complete.\n")
    _, show_result = _outcome_helpers()
    if show_result is not None:
        try:
            show_result(opts, quiet=True)
        except Exception as e:  # a summary line must never fail a finished import
            FreeCAD.Console.PrintWarning(f"Import summary could not be printed: {e}\n")
    return True
