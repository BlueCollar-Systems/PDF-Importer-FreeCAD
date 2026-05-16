# -*- coding: utf-8 -*-
# PDFImporterCmd.py — GUI command to import PDF with options dialog
# BlueCollar Systems — BUILT. NOT BOUGHT.
#
# BCS-ARCH-001 Rule 5 sweep: this dialog exposes only the user-facing
# controls — Mode, Text rendering, Import text — plus legitimate workflow
# controls (pages, scale, grouping, page arrangement). Every quality-tier
# dial (arc_mode, cleanup_level, lineweight_mode, hatch_mode, raster_dpi,
# bezier_segments, merge_tolerance, detect_arcs, make_faces, map_dashes,
# strict_text_fidelity, force_raster, raster_fallback) is removed. Internal
# defaults from ImportConfig apply universally.
from __future__ import annotations

import os
import sys

_lib_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")
if _lib_dir not in sys.path:
    sys.path.insert(0, _lib_dir)

try:
    import pymupdf as fitz  # PyMuPDF >= 1.24 preferred name
except ImportError:
    try:
        import fitz  # Legacy fallback
    except ImportError:
        fitz = None

try:
    from PySide6 import QtWidgets
except ImportError:
    from PySide2 import QtWidgets

import FreeCAD


# ──────────────────────────────────────────────────────────────────────
# Options dialog
# ──────────────────────────────────────────────────────────────────────
class ImportPDFDialog(QtWidgets.QDialog):
    """Mode-based options dialog for the PDF vector importer (BCS-ARCH-001)."""

    # Four modes only per BCS-ARCH-001. No presets. Auto is default.
    # Per-parameter tuning comes from ImportConfig classmethods, not
    # dialog-level tuples. Every mode targets identical quality —
    # modes differ by strategy for different input types, not quality tier.
    MODES = {
        "Auto":   {"label": "Auto",   "tooltip": "Analyze and pick Vector/Raster/Hybrid automatically"},
        "Vector": {"label": "Vector", "tooltip": "Extract all vector geometry faithfully"},
        "Raster": {"label": "Raster", "tooltip": "Place as high-DPI image (scanned PDFs)"},
        "Hybrid": {"label": "Hybrid", "tooltip": "Vectors where clean, raster where lossy"},
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Import PDF — BlueCollar Systems")
        self.setMinimumWidth(420)
        self._page_count = None

        # ── File row ──
        self.file_edit = QtWidgets.QLineEdit()
        self.file_edit.setPlaceholderText("Choose a PDF file…")
        browse_btn = QtWidgets.QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse)
        file_row = QtWidgets.QHBoxLayout()
        file_row.addWidget(self.file_edit, 1)
        file_row.addWidget(browse_btn, 0)

        # ── Mode (BCS-ARCH-001) ──
        self.mode_combo = QtWidgets.QComboBox()
        for key, meta in self.MODES.items():
            self.mode_combo.addItem(meta["label"], userData=key)
        self.mode_combo.setCurrentText("Auto")
        # Concatenate each mode's tooltip for the dropdown itself.
        self.mode_combo.setToolTip(
            "\n".join(f"{k} — {v['tooltip']}" for k, v in self.MODES.items())
        )

        # ── Pages ──
        self.page_edit = QtWidgets.QLineEdit("All")
        self.page_edit.setToolTip("1, 1-5, 1,3,7, or All")

        # ── Scale ──
        self.scale_spin = QtWidgets.QDoubleSpinBox()
        self.scale_spin.setDecimals(6)
        self.scale_spin.setRange(1e-6, 100000.0)
        self.scale_spin.setValue(25.4 / 72.0)
        self.scale_spin.setToolTip("Default converts PDF points to mm (25.4/72)")

        # ── Import Text toggle ──
        self.import_text_chk = QtWidgets.QCheckBox("Import text")
        self.import_text_chk.setChecked(True)
        self.import_text_chk.setToolTip(
            "When unchecked, text is not imported at all (equivalent to the\n"
            "old 'No text' option). When checked, the text mode below\n"
            "controls how text is rendered.")

        # ── Text Mode (orthogonal selector — BCS-ARCH-001) ──
        self.text_combo = QtWidgets.QComboBox()
        self.text_combo.addItems(["Labels", "3D Text", "Glyphs", "Geometry"])
        self.text_combo.setCurrentText("3D Text")
        self.text_combo.setToolTip(
            "How text is rendered when Import text is enabled:\n"
            "Labels — FreeCAD Draft Text labels, editable\n"
            "3D Text — extruded 3D letterforms\n"
            "Glyphs — exact glyph geometry from the PDF font\n"
            "Geometry — fall back to reconstructed line geometry")

        # Enable/disable text mode based on the toggle.
        self.import_text_chk.toggled.connect(self.text_combo.setEnabled)

        # ── Grouping (workflow — kept) ──
        self.grouping_combo = QtWidgets.QComboBox()
        self.grouping_combo.addItems([
            "Single", "Per Page", "Per Layer", "Per Color",
            "Nested Page>Layer", "Nested Page>Lineweight"])
        self.grouping_combo.setCurrentText("Per Page")
        self.grouping_combo.setToolTip(
            "How imported objects are grouped in the model tree:\n"
            "Single = everything in one group\n"
            "Per Page = one group per PDF page\n"
            "Per Layer = one group per PDF layer (OCG)\n"
            "Per Color = one group per stroke/fill color\n"
            "Nested Page>Layer = pages containing layer sub-groups\n"
            "Nested Page>Lineweight = pages containing lineweight sub-groups")

        # ── Page arrangement (workflow — kept) ──
        self.page_arrangement_combo = QtWidgets.QComboBox()
        self.page_arrangement_combo.addItems([
            "Spread (20% gap)",
            "Compact gap",
            "Touching pages",
            "Overlay pages",
        ])
        self.page_arrangement_combo.setCurrentText("Spread (20% gap)")
        self.page_arrangement_combo.setToolTip(
            "How multi-page imports are laid out in model space.\n"
            "Spread (20% gap) keeps pages readable by default.")

        # ── Restore last-used settings ──
        self._restore_settings()

        # ── Layout ──
        form = QtWidgets.QFormLayout()
        form.addRow("PDF file:", file_row)
        form.addRow("Mode:", self.mode_combo)
        form.addRow("Pages:", self.page_edit)
        form.addRow("Scale (mm/pt):", self.scale_spin)
        form.addRow("", self.import_text_chk)
        form.addRow("Text Mode:", self.text_combo)
        form.addRow("Grouping:", self.grouping_combo)
        form.addRow("Page Layout:", self.page_arrangement_combo)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._validate_and_accept)
        btns.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(btns)

    def _browse(self):
        # Remember last folder across sessions
        try:
            grp = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/Mod/PDFVectorImporter")
            last_dir = grp.GetString("LastImportDir", "")
        except (AttributeError, RuntimeError, ValueError):
            last_dir = ""

        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select PDF File", last_dir, "PDF Files (*.pdf)")
        if path:
            # Save this folder for next time
            try:
                grp = FreeCAD.ParamGet("User parameter:BaseApp/Preferences/Mod/PDFVectorImporter")
                grp.SetString("LastImportDir", os.path.dirname(path))
            except (AttributeError, RuntimeError, ValueError):
                pass
            self.file_edit.setText(path)
            if fitz:
                try:
                    with fitz.open(path) as doc:
                        self._page_count = doc.page_count
                    self.page_edit.setPlaceholderText(
                        f"1-{self._page_count}  (PDF has {self._page_count} pages)")
                except (RuntimeError, OSError, ValueError):
                    pass

    _PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/PDFVectorImporter"

    def _restore_settings(self):
        """Load last-used dialog settings from FreeCAD preferences."""
        try:
            grp = FreeCAD.ParamGet(self._PARAM_PATH)
            mode = grp.GetString("LastMode", "")
            if mode and mode in self.MODES:
                self.mode_combo.setCurrentText(mode)
            text_mode = grp.GetString("LastTextMode", "")
            if text_mode == "Labels" and not grp.GetBool("TextDefaultMigratedV407", False):
                text_mode = "3D Text"
                grp.SetString("LastTextMode", text_mode)
                grp.SetBool("TextDefaultMigratedV407", True)
            if text_mode and text_mode in ("Labels", "3D Text", "Glyphs", "Geometry"):
                self.text_combo.setCurrentText(text_mode)
            # Import-text toggle is a separate pref (BCS-ARCH-001 orthogonal control).
            import_text = grp.GetBool("LastImportText", True)
            self.import_text_chk.setChecked(bool(import_text))
            self.text_combo.setEnabled(self.import_text_chk.isChecked())
            scale = grp.GetFloat("LastScale", 0.0)
            if scale > 0:
                self.scale_spin.setValue(scale)
            grouping = grp.GetString("LastGroupingMode", "")
            if grouping:
                self.grouping_combo.setCurrentText(grouping)
            page_arrangement = grp.GetString("LastPageArrangement", "")
            if page_arrangement:
                self.page_arrangement_combo.setCurrentText(page_arrangement)
        except (AttributeError, RuntimeError, ValueError):
            pass  # First run or corrupted prefs — use defaults

    def _save_settings(self):
        """Persist current dialog settings to FreeCAD preferences."""
        try:
            grp = FreeCAD.ParamGet(self._PARAM_PATH)
            grp.SetString("LastMode", self.mode_combo.currentText())
            grp.SetString("LastTextMode", self.text_combo.currentText())
            grp.SetBool("LastImportText", self.import_text_chk.isChecked())
            grp.SetFloat("LastScale", self.scale_spin.value())
            grp.SetString("LastGroupingMode", self.grouping_combo.currentText())
            grp.SetString("LastPageArrangement", self.page_arrangement_combo.currentText())
        except (AttributeError, RuntimeError, ValueError):
            pass

    # Mapping tables for combo dropdowns (internal value -> UI label)
    _GROUPING_MAP = {
        "single": "Single", "per_page": "Per Page", "per_layer": "Per Layer",
        "per_color": "Per Color", "nested_page_layer": "Nested Page>Layer",
        "nested_page_lineweight": "Nested Page>Lineweight",
    }
    _PAGE_ARRANGEMENT_MAP = {
        "spread": "Spread (20% gap)",
        "compact": "Compact gap",
        "touch": "Touching pages",
        "overlay": "Overlay pages",
    }

    # UI label -> canonical BCS-ARCH-001 mode value.
    _MODE_VALUE_MAP = {
        "Auto":   "auto",
        "Vector": "vector",
        "Raster": "raster",
        "Hybrid": "hybrid",
    }

    def _validate_and_accept(self):
        path = self.file_edit.text().strip()
        if not path or not os.path.isfile(path):
            QtWidgets.QMessageBox.warning(
                self, "Missing File", "Please select a valid PDF file.")
            return
        try:
            self._parse_pages()
        except ValueError as e:
            QtWidgets.QMessageBox.warning(
                self, "Invalid Page Selection", str(e))
            return
        self._save_settings()
        self.accept()

    # ── Parse page specification ──
    def _parse_pages(self) -> list:
        text = self.page_edit.text().strip().lower()
        if not text or text in ("all", "*"):
            if self._page_count:
                return list(range(1, self._page_count + 1))
            return [1]

        pages = set()
        bad_parts = []
        for raw_part in text.split(','):
            part = raw_part.strip()
            if not part:
                continue
            if '-' in part:
                a_s, b_s = part.split('-', 1)
                if not (a_s.strip().isdigit() and b_s.strip().isdigit()):
                    bad_parts.append(part)
                    continue
                a = int(a_s)
                b = int(b_s)
                if a <= 0 or b <= 0:
                    bad_parts.append(part)
                    continue
                start, end = sorted((a, b))
                for p in range(start, end + 1):
                    pages.add(p)
            elif part.isdigit():
                p = int(part)
                if p <= 0:
                    bad_parts.append(part)
                    continue
                pages.add(p)
            else:
                bad_parts.append(part)

        if bad_parts:
            raise ValueError(
                "Invalid page entry: {}\nUse examples like 1, 1-5, 1,3,7, or all.".format(
                    ", ".join(bad_parts)
                )
            )
        if not pages:
            raise ValueError("No valid pages selected.")
        if self._page_count:
            over = [p for p in sorted(pages) if p > self._page_count]
            if over:
                raise ValueError(
                    f"Page(s) out of range for this PDF: {', '.join(map(str, over))}. "
                    f"This file has {self._page_count} page(s)."
                )
        return sorted(pages)

    # ── Build ImportOptions from dialog state ──
    def build_options(self):
        import PDFVectorImporter.src.PDFImporterCore as core

        # BCS-ARCH-001 mode — single source of truth for strategy.
        mode_label = self.mode_combo.currentText()
        import_mode = self._MODE_VALUE_MAP.get(mode_label, "auto")

        # Text controls (orthogonal per BCS-ARCH-001).
        import_text = self.import_text_chk.isChecked()
        text_ui = self.text_combo.currentText()
        # Map the 4-option text combo to ImportOptions.text_mode internal values.
        text_mode_map = {
            "Labels":   "labels",
            "3D Text":  "3d_text",
            "Glyphs":   "glyphs",
            "Geometry": "geometry",
        }
        text_mode = text_mode_map.get(text_ui, "3d_text")
        if not import_text:
            # Orthogonal toggle takes precedence — text_mode is irrelevant
            # when text is off.
            text_mode = "none"

        # Reverse-map UI labels to internal values for workflow controls
        _grp_rev = {v: k for k, v in self._GROUPING_MAP.items()}
        _arr_rev = {v: k for k, v in self._PAGE_ARRANGEMENT_MAP.items()}

        # Consolidated defaults per BCS-ARCH-001 parameter table.
        # Quality-tier dials are hardcoded — no UI exposure.
        # Hatching is always "group" (least destructive).
        # Raster DPI is always 300. Strict text fidelity is always True.
        # Lineweight handling is always "preserve". Arc reconstruction
        # is always enabled in non-raster modes via the Auto cleanup level.
        opts = core.ImportOptions(
            pages=self._parse_pages(),
            scale_to_mm=False,
            user_scale=float(self.scale_spin.value()),
            flip_y=True,
            join_tol=0.05,
            curve_step_mm=0.2,
            make_faces=(import_mode != "raster"),
            import_text=import_text,
            text_mode=text_mode,
            strict_text_fidelity=True,
            hatch_mode="group",
            group_by_color=True,
            assign_linewidth=True,
            map_dashes=(import_mode != "raster"),
            detect_arcs=(import_mode != "raster"),
            ignore_images=(import_mode == "raster"),
            raster_fallback=(import_mode in ("auto", "hybrid")),
            raster_dpi=300,
            import_mode=import_mode,
            create_top_group=True,
            verbose=True,
            page_arrangement=_arr_rev.get(self.page_arrangement_combo.currentText(), "spread"),
            page_gap_ratio=0.20,
        )

        # Workflow / consolidated quality dials attached as extra attrs.
        opts.arc_mode = "auto"
        opts.cleanup_level = "balanced"
        opts.lineweight_mode = "preserve"
        opts.grouping_mode = _grp_rev.get(self.grouping_combo.currentText(), "per_page")

        return opts


# ──────────────────────────────────────────────────────────────────────
# FreeCAD Command
# ──────────────────────────────────────────────────────────────────────
class ImportPDFVectorCommand:
    """FreeCAD GUI command: Import PDF Vector…"""

    def GetResources(self):
        icon_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "resources", "ImportPDFVector.svg")
        return {
            "Pixmap": icon_path if os.path.exists(icon_path) else "",
            "MenuText": "Import PDF Vector…",
            "ToolTip": "Import vector geometry, text, and images from a PDF file.",
        }

    def IsActive(self):
        return True

    def Activated(self):
        # Re-check for fitz each time (may have been installed mid-session)
        try:
            try:
                import pymupdf as fitz  # noqa: F811, F401  # PyMuPDF >= 1.24 preferred name
            except ImportError:
                import fitz  # noqa: F811, F401  # Legacy fallback
        except ImportError:
            QtWidgets.QMessageBox.warning(
                None, "PyMuPDF Not Found",
                "PyMuPDF is not installed yet.\n\n"
                "Switch to the PDF Vector Importer workbench — it will\n"
                "offer to install it automatically.")
            return

        dlg = ImportPDFDialog()
        exec_fn = getattr(dlg, "exec", None) or getattr(dlg, "exec_", None)
        if exec_fn is None or exec_fn() != QtWidgets.QDialog.Accepted:
            return

        pdf_path = dlg.file_edit.text().strip()
        opts = dlg.build_options()

        import PDFVectorImporter.src.PDFImporterCore as core
        try:
            core.import_pdf(pdf_path, opts)
            FreeCAD.Console.PrintMessage("PDF import complete.\n")

            # Auto-switch to orthographic top view
            try:
                import FreeCADGui
                view = FreeCADGui.ActiveDocument.ActiveView
                if view:
                    view.setCameraType("Orthographic")
                    view.viewTop()
                    view.fitAll()
            except (AttributeError, RuntimeError):
                pass

        except (RuntimeError, ValueError, TypeError, OSError, AttributeError, ImportError) as e:
            import traceback
            FreeCAD.Console.PrintError(f"Import failed: {e}\n{traceback.format_exc()}")
            QtWidgets.QMessageBox.critical(
                None, "Import Failed", str(e))
