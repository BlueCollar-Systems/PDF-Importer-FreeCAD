# -*- coding: utf-8 -*-
# PDFTools.py — utility commands: Check Env, Batch Import, Install PyMuPDF
# BlueCollar Systems — BUILT. NOT BOUGHT.
from __future__ import annotations

import os
import sys
import traceback

try:
    import FreeCAD
    import FreeCADGui
except ImportError:
    FreeCAD = FreeCADGui = None

try:
    from PySide6 import QtWidgets, QtCore
except ImportError:
    try:
        from PySide2 import QtWidgets, QtCore
    except ImportError:
        QtWidgets = QtCore = None


def _msg(s):
    if FreeCAD:
        FreeCAD.Console.PrintMessage(s + "\n")
    else:
        print(s)

def _warn(s):
    if FreeCAD:
        FreeCAD.Console.PrintWarning(s + "\n")
    else:
        print("WARN:", s)

def _err(s):
    if FreeCAD:
        FreeCAD.Console.PrintError(s + "\n")
    else:
        print("ERR:", s)


def _find_python() -> str:
    """Find the real python.exe — sys.executable may point to freecad.exe."""
    exe = sys.executable
    bindir = os.path.dirname(exe)
    # FreeCAD 1.0+ (conda-based) puts python.exe alongside freecad.exe
    for name in ("python.exe", "python3.exe", "python"):
        candidate = os.path.join(bindir, name)
        if os.path.isfile(candidate):
            return candidate
    # Check parent directories
    for name in ("python.exe", "python3.exe", "python"):
        candidate = os.path.join(os.path.dirname(bindir), name)
        if os.path.isfile(candidate):
            return candidate
    # Last resort
    return exe


def _wb_base() -> str:
    """Return workbench root directory."""
    for root in (FreeCAD.getUserAppDataDir(), FreeCAD.getResourceDir()):
        candidate = os.path.join(root, "Mod", "PDFVectorImporter")
        if os.path.isdir(candidate):
            return candidate
    return ""


# ──────────────────────────────────────────────────────────────────────
class CheckEnvironmentCommand:
    def GetResources(self):
        return {
            "Pixmap": "",
            "MenuText": "Check Environment",
            "ToolTip": "Print Python paths, library versions, and FreeCAD module status.",
        }

    def IsActive(self):
        return True

    def Activated(self):
        _msg("\n════════════════════════════════════════════════════")
        _msg("  PDF Vector Importer — Environment Check")
        _msg("════════════════════════════════════════════════════")

        _msg(f"  Workbench dir: {_wb_base()}")

        # Python
        _msg(f"  sys.executable: {sys.executable}")
        _msg(f"  Real python:    {_find_python()}")

        # PyMuPDF
        try:
            try:
                import pymupdf as fitz  # PyMuPDF >= 1.24 preferred name
            except ImportError:
                import fitz  # Legacy fallback
            ver = getattr(fitz, "version", ("?", "?", "?"))
            ver_str = ".".join(str(v) for v in ver) if isinstance(ver, (tuple, list)) else str(ver)
            _msg(f"  PyMuPDF:  version={ver_str}  file={getattr(fitz, '__file__', '?')}")
        except (ImportError, AttributeError, OSError, RuntimeError) as e:
            _err(f"  PyMuPDF:  IMPORT FAILED — {e}")

        # Workbench package
        try:
            import PDFVectorImporter
            base = os.path.dirname(getattr(PDFVectorImporter, "__file__", "") or "")
            _msg(f"  Workbench package: {base or '(unknown path)'}")
        except (ImportError, AttributeError, OSError, RuntimeError) as e:
            _err(f"  Workbench package: NOT FOUND — {e}")

        # Core modules
        for mod in ("Draft", "Part"):
            try:
                __import__(mod)
                _msg(f"  {mod}: OK")
            except (ImportError, ModuleNotFoundError) as e:
                _warn(f"  {mod}: MISSING — {e}")

        # Image WB
        try:
            import ImageGui  # noqa: F401
            _msg("  Image Workbench: available")
        except ImportError:
            _warn("  Image Workbench: NOT available (embedded image import disabled)")

        # PySide version
        try:
            from PySide6 import __version__ as pv
            _msg(f"  PySide6: {pv}")
        except ImportError:
            try:
                from PySide2 import __version__ as pv
                _msg(f"  PySide2: {pv}")
            except ImportError:
                _warn("  PySide: NOT FOUND")

        # Optional SVG/font helper executables
        try:
            from PDFSvgTextRenderer import find_pdftocairo
        except ImportError:
            find_pdftocairo = None

        pdftocairo = find_pdftocairo() if find_pdftocairo else None
        if pdftocairo:
            _msg(f"  pdftocairo: OK  ({pdftocairo})")
        else:
            _warn("  pdftocairo: MISSING — SVG/glyph text uses bundled PyMuPDF fallback")
            _msg("    Download Poppler: https://github.com/oschwartz10612/poppler-windows/releases/latest")
            _msg("    Or place pdftocairo.exe in PDFVectorImporter/src/lib/bin/ for Poppler-first SVG rendering")

        gs_found = False
        try:
            import glob
            import sys as _sys
            if _sys.platform == "win32":
                for pat in (
                    r"C:\Program Files\gs\gs*\bin\gswin64c.exe",
                    r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe",
                ):
                    matches = sorted(glob.glob(pat))
                    if matches:
                        _msg(f"  Ghostscript: OK  ({matches[-1]})")
                        gs_found = True
                        break
        except (OSError, RuntimeError):
            pass
        if not gs_found:
            _warn("  Ghostscript: MISSING — non-embedded font repair disabled")
            _msg("    Download: https://ghostscript.com/releases/gsdnld.html")

        _msg(f"  sys.path ({len(sys.path)} entries):")
        for p in sys.path:
            _msg(f"    {p}")
        _msg("════════════════════════════════════════════════════\n")


# ──────────────────────────────────────────────────────────────────────
class ImportViaConsoleCommand:
    """Pick a file and pages, import via console (no full dialog)."""

    def GetResources(self):
        return {
            "Pixmap": "",
            "MenuText": "Import via Console…",
            "ToolTip": "Quick file + page picker, runs import with default settings.",
        }

    def IsActive(self):
        return QtWidgets is not None

    def Activated(self):
        pdf, _ = QtWidgets.QFileDialog.getOpenFileName(
            None, "Select PDF", "", "PDF Files (*.pdf)")
        if not pdf:
            return
        pages_str, ok = QtWidgets.QInputDialog.getText(
            None, "Pages", "Pages (e.g. 1  or  1,3-5  or  all):", text="1")
        if not ok:
            return
        try:
            import PDFVectorImporter.src.PDFImporterCore as core
            pages = _parse_page_spec(pages_str, pdf)
            opts = core.ImportOptions(pages=pages)
            core.import_pdf(pdf, opts)
            _msg("Import completed.")
        except (RuntimeError, ValueError, TypeError, OSError, AttributeError, ImportError) as e:
            _err(f"Console import failed: {e}")
            _msg(traceback.format_exc())


# ──────────────────────────────────────────────────────────────────────
class BatchImportCommand:
    """Import all PDFs in a folder."""

    def GetResources(self):
        return {
            "Pixmap": "",
            "MenuText": "Batch Import…",
            "ToolTip": "Import all PDF files in a folder (optionally recursive).",
        }

    def IsActive(self):
        return QtWidgets is not None

    def Activated(self):
        import PDFVectorImporter.src.PDFImporterCore as core

        folder = QtWidgets.QFileDialog.getExistingDirectory(None, "Select folder")
        if not folder:
            return

        pages_str, ok = QtWidgets.QInputDialog.getText(
            None, "Pages", "all | first | 1,3-5", text="all")
        if not ok:
            return

        recurse = (
            QtWidgets.QMessageBox.question(
                None, "Recurse?", "Include subfolders?",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)
            == QtWidgets.QMessageBox.Yes)

        pdfs = []
        if recurse:
            for root, _, files in os.walk(folder):
                for f in files:
                    if f.lower().endswith(".pdf"):
                        pdfs.append(os.path.join(root, f))
        else:
            for f in os.listdir(folder):
                if f.lower().endswith(".pdf"):
                    pdfs.append(os.path.join(folder, f))

        if not pdfs:
            _warn("No PDF files found in the selected folder.")
            return

        ok_count = fail_count = 0
        _msg(f"\n═══ Batch Import: {len(pdfs)} PDF files ═══")
        for pdf_path in sorted(pdfs):
            pages = _parse_page_spec(pages_str, pdf_path)
            for pg in pages:
                try:
                    if FreeCAD.ActiveDocument is None:
                        FreeCAD.newDocument()
                    opts = core.ImportOptions(pages=[pg])
                    core.import_pdf(pdf_path, opts)
                    ok_count += 1
                except (RuntimeError, ValueError, TypeError, OSError, AttributeError) as e:
                    fail_count += 1
                    _err(f"  FAIL: {pdf_path} page {pg} — {e}")
        _msg(f"═══ Batch done: {ok_count} ok, {fail_count} failed ═══\n")


# ──────────────────────────────────────────────────────────────────────
class InstallPyMuPDFCommand:
    """One-click installer for PyMuPDF into the workbench's lib folder."""

    def GetResources(self):
        return {
            "Pixmap": "",
            "MenuText": "Install / Update PyMuPDF",
            "ToolTip": "Download and install PyMuPDF into this workbench (no admin needed).",
        }

    def IsActive(self):
        return True

    def Activated(self):
        import subprocess

        base = _wb_base()
        if not base:
            _err("Cannot find workbench directory!")
            return

        target = os.path.join(base, "src", "lib")
        os.makedirs(target, exist_ok=True)

        py = _find_python()

        _msg(f"Installing PyMuPDF to: {target}")
        _msg(f"Using Python: {py}")

        _kw = {}
        if sys.platform == "win32":
            _kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        try:
            subprocess.check_call(
                [py, "-m", "ensurepip", "--upgrade"],
                timeout=120, **_kw)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            _warn("ensurepip skipped (may already be present)")

        try:
            subprocess.check_call(
                [py, "-m", "pip", "install", "--upgrade",
                 "--only-binary", ":all:", "--target", target, "PyMuPDF>=1.24,<2.0"],
                timeout=300, **_kw)
            _msg("PyMuPDF installed successfully.  Please restart FreeCAD.")
            if QtWidgets:
                QtWidgets.QMessageBox.information(
                    None, "Success",
                    f"PyMuPDF installed to:\n{target}\n\n"
                    "Please restart FreeCAD.")
        except subprocess.CalledProcessError as e:
            _err(f"pip install failed: {e}")
            if QtWidgets:
                QtWidgets.QMessageBox.critical(
                    None, "Install Failed",
                    f"pip install PyMuPDF>=1.24,<2.0 failed:\n{e}\n\n"
                    "Try running manually in a terminal:\n"
                    f'  "{py}" -m pip install --target "{target}" "PyMuPDF>=1.24,<2.0"')
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as e:
            _err(f"Installer error: {e}\n{traceback.format_exc()}")


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _parse_page_spec(spec: str, pdf_path: str) -> list:
    """Parse a page specification string into a list of 1-based page numbers."""
    try:
        try:
            import pymupdf as fitz  # PyMuPDF >= 1.24 preferred name
        except ImportError:
            import fitz  # Legacy fallback
        pdoc = fitz.open(pdf_path)
        n = len(pdoc)
        pdoc.close()
    except (ImportError, OSError, RuntimeError):
        n = 9999

    s = (spec or "1").strip().lower()
    if s in ("all", "a", "*"):
        return list(range(1, n + 1))
    if s in ("first", "1st"):
        return [1]
    out = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = part.split("-", 1)
                out += list(range(int(a), int(b) + 1))
            else:
                out.append(int(part))
        except ValueError:
            _warn(f"  Page spec '{part}' is not a valid page number — skipped.")
            continue
    if not out:
        _warn(f"  Page spec '{spec}' produced no valid pages — defaulting to page 1.")
        return [1]
    return out
