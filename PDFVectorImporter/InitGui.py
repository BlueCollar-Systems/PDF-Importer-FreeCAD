# -*- coding: utf-8 -*-
# InitGui.py — FreeCAD workbench registration
# PDF Vector Importer — BlueCollar Systems — BUILT. NOT BOUGHT.
#
# FreeCAD exec()s this file in a restricted namespace.
# ALL logic must be inside the class or fully inline.
# NO module-level helper functions — they vanish from scope.
import os
import sys

import FreeCAD
import FreeCADGui


class PDFVectorImporterWorkbench(FreeCADGui.Workbench):

    MenuText = "PDF Vector Importer"
    ToolTip  = ("Import vector PDF drawings with arc detection, text, "
                "images, and SketchUp-style reference scaling.")

    def __init__(self):
        # Find workbench root — inline, no helper function
        base = ""
        for root in (FreeCAD.getUserAppDataDir(), FreeCAD.getResourceDir()):
            d = os.path.join(root, "Mod", "PDFVectorImporter")
            if os.path.isdir(d):
                base = d
                break
        icon = os.path.join(base, "resources", "ImportPDFVector.svg") if base else ""
        self.__class__.Icon = icon if os.path.isfile(icon) else ""

    def _prioritize_paths(self, paths):
        """Ensure local workbench paths win over stale/duplicate installs."""
        for p in (x for x in reversed(paths) if x):
            try:
                while p in sys.path:
                    sys.path.remove(p)
            except (AttributeError, ValueError):
                pass
            sys.path.insert(0, p)

    def _evict_stale_modules(self, base):
        """Drop already-imported modules that came from another install path."""
        stale_names = (
            "PDFImporterCmd",
            "PDFScaleTool",
            "PDFTools",
            "PDFImporterCore",
            "PDFImportHandler",
            "PDFVectorImporter.src.PDFImporterCmd",
            "PDFVectorImporter.src.PDFScaleTool",
            "PDFVectorImporter.src.PDFImporterCore",
        )
        for name in stale_names:
            mod = sys.modules.get(name)
            mod_path = str(getattr(mod, "__file__", "") or "")
            if mod and mod_path and not mod_path.startswith(base):
                try:
                    del sys.modules[name]
                except KeyError:
                    pass

    def Initialize(self):
        # Find workbench root again (Initialize runs in a different context)
        base = ""
        for root in (FreeCAD.getUserAppDataDir(), FreeCAD.getResourceDir()):
            d = os.path.join(root, "Mod", "PDFVectorImporter")
            if os.path.isdir(d):
                base = d
                break

        if not base:
            FreeCAD.Console.PrintError("PDFVectorImporter: cannot find workbench folder\n")
            return

        src = os.path.join(base, "src")
        lib = os.path.join(src, "lib")

        # Ensure paths are importable
        self._prioritize_paths([os.path.dirname(base), base, src])
        if os.path.isdir(lib):
            self._prioritize_paths([lib])
        self._evict_stale_modules(base)

        # Register commands — each in its own try/except
        try:
            from PDFImporterCmd import ImportPDFVectorCommand
            FreeCADGui.addCommand("ImportPDFVector", ImportPDFVectorCommand())
        except (ImportError, AttributeError, RuntimeError, ValueError, SyntaxError) as e:
            FreeCAD.Console.PrintError("PDF Import cmd: " + str(e) + "\n")

        try:
            from PDFScaleTool import ScaleByReferenceCommand, QuickScaleCommand
            FreeCADGui.addCommand("PDF_ScaleByReference", ScaleByReferenceCommand())
            FreeCADGui.addCommand("PDF_QuickScale", QuickScaleCommand())
        except (ImportError, AttributeError, RuntimeError, ValueError, SyntaxError) as e:
            FreeCAD.Console.PrintError("PDF Scale cmds: " + str(e) + "\n")

        try:
            from PDFTools import (
                CheckEnvironmentCommand,
                ImportViaConsoleCommand,
                BatchImportCommand,
                InstallPyMuPDFCommand,
            )
            FreeCADGui.addCommand("PDF_CheckEnv", CheckEnvironmentCommand())
            FreeCADGui.addCommand("PDF_ImportViaConsole", ImportViaConsoleCommand())
            FreeCADGui.addCommand("PDF_BatchImport", BatchImportCommand())
            FreeCADGui.addCommand("PDF_InstallPyMuPDF", InstallPyMuPDFCommand())
        except (ImportError, AttributeError, RuntimeError, ValueError, SyntaxError) as e:
            FreeCAD.Console.PrintError("PDF Utility cmds: " + str(e) + "\n")

        # Toolbars & menus
        import_cmds = ["ImportPDFVector"]
        scale_cmds  = ["PDF_ScaleByReference", "PDF_QuickScale"]
        try:
            self.appendToolbar("PDF Import", import_cmds + scale_cmds)
        except (AttributeError, RuntimeError):
            pass
        try:
            self.appendMenu("PDF Vector Importer",
                            import_cmds + scale_cmds)
        except (AttributeError, RuntimeError):
            pass

        FreeCAD.Console.PrintMessage(
            "PDF Vector Importer ready — BlueCollar Systems\n")

    def Activated(self):
        # Ensure paths every time (in case this is the first activation)
        base = ""
        for root in (FreeCAD.getUserAppDataDir(), FreeCAD.getResourceDir()):
            d = os.path.join(root, "Mod", "PDFVectorImporter")
            if os.path.isdir(d):
                base = d
                break
        if base:
            src = os.path.join(base, "src")
            lib = os.path.join(src, "lib")
            self._prioritize_paths([os.path.dirname(base), base, src])
            if os.path.isdir(lib):
                self._prioritize_paths([lib])
            self._evict_stale_modules(base)

        # Check every dependency required for exact PDF text/font delivery.
        has_fitz = False
        try:
            try:
                import pymupdf as fitz  # noqa: F401  # PyMuPDF >= 1.24 preferred name
            except ImportError:
                import fitz  # noqa: F401  # Legacy fallback
            has_fitz = True
        except ImportError:
            pass

        has_fonttools = False
        try:
            import fontTools  # noqa: F401
            has_fonttools = True
        except ImportError:
            pass

        missing = []
        if not has_fitz:
            missing.append("PyMuPDF")
        if not has_fonttools:
            missing.append("fonttools")
        if missing:
            self._offer_install(base, missing)

    def _offer_install(self, base, missing):
        try:
            from PySide6 import QtWidgets
        except ImportError:
            from PySide2 import QtWidgets

        reply = QtWidgets.QMessageBox.question(
            None,
            "PDF Vector Importer — First-Time Setup",
            "Required PDF dependencies are missing: " + ", ".join(missing) + ".\n\n"
            "Install them now?  (no admin needed)\n\n"
            "This only happens once.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.Yes,
        )
        if reply != QtWidgets.QMessageBox.Yes:
            return

        import subprocess

        target = os.path.join(base, "src", "lib") if base else ""
        if not target:
            return
        os.makedirs(target, exist_ok=True)

        # Find real python.exe (sys.executable may be freecad.exe)
        py = sys.executable
        bindir = os.path.dirname(py)
        for name in ("python.exe", "python3.exe", "python"):
            candidate = os.path.join(bindir, name)
            if os.path.isfile(candidate):
                py = candidate
                break

        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

        FreeCAD.Console.PrintMessage("Installing PDF runtime dependencies to " + target + "...\n")
        try:
            try:
                subprocess.check_call(
                    [py, "-m", "ensurepip", "--upgrade"],
                    timeout=120, **kw)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
                pass

            subprocess.check_call(
                [py, "-m", "pip", "install", "--upgrade",
                 "--only-binary", ":all:", "--target", target,
                 "PyMuPDF>=1.24,<2.0", "fonttools>=4.50,<5.0"],
                timeout=300, **kw)

            if target not in sys.path:
                sys.path.insert(0, target)

            # Test if it loaded
            try:
                try:
                    import pymupdf as fitz  # noqa: F401  # PyMuPDF >= 1.24 preferred name
                except ImportError:
                    import fitz  # noqa: F401  # Legacy fallback
                import fontTools  # noqa: F401
                QtWidgets.QMessageBox.information(
                    None, "Ready to Go!",
                    "PDF dependencies installed!\n\nYou can now import PDFs.")
                FreeCAD.Console.PrintMessage("PDF dependencies installed and loaded.\n")
            except ImportError:
                QtWidgets.QMessageBox.information(
                    None, "Almost There",
                    "PDF dependencies installed to:\n" + target + "\n\n"
                    "Please restart FreeCAD to activate it.")

        except subprocess.CalledProcessError as e:
            FreeCAD.Console.PrintError("pip failed: " + str(e) + "\n")
            QtWidgets.QMessageBox.critical(
                None, "Install Failed",
                "Automatic install failed.\n\n"
                "Try manually in a terminal:\n"
                '  "' + py + '" -m pip install --target "' + target
                + '" "PyMuPDF>=1.24,<2.0" "fonttools>=4.50,<5.0"')
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as e:
            FreeCAD.Console.PrintError("Install error: " + str(e) + "\n")

    def GetClassName(self):
        return "Gui::PythonWorkbench"


FreeCADGui.addWorkbench(PDFVectorImporterWorkbench())

# Install at GUI module load, not lazy workbench activation, so documents
# opened before this workbench is selected still receive their stored style.
try:
    from PDFVectorImporter.src.PDFGuiStyleRestorer import register_gui_style_restorer

    _PDF_GUI_STYLE_RESTORER = register_gui_style_restorer()
except (ImportError, AttributeError, RuntimeError, TypeError, ValueError) as e:
    FreeCAD.Console.PrintError("PDF GUI style restorer: " + str(e) + "\n")
