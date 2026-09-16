# -*- coding: utf-8 -*-
# PDFScaleTool.py — SketchUp-style "Measure & Scale" tool
# BlueCollar-Systems — BUILT. NOT BOUGHT.
"""
Scale imported PDF geometry by picking two reference points and typing the
known real-world dimension.  Works like SketchUp's tape-measure scale:

  Method 1 (Selection-based):
    1. Select two vertices or edge endpoints in the 3D view
    2. Click "Scale by Reference..."
    3. Dialog shows measured distance — type the real dimension
    4. Everything scales

  Method 2 (Dialog-based):
    1. Click "Scale by Reference..." with nothing selected
    2. Enter coordinates of two known points manually
    3. Type the real dimension
    4. Everything scales

Accepts unit suffixes: mm, cm, m, in, ft, ', "
Accepts compound: 5'-6" or 5' 6 1/2"
"""
from __future__ import annotations

import math
import re

try:
    import FreeCAD
    import FreeCADGui
    import Part
    from FreeCAD import Vector
except ImportError:
    FreeCAD = FreeCADGui = Part = Vector = None

try:
    from PySide6 import QtWidgets, QtCore, QtGui
except ImportError:
    try:
        from PySide2 import QtWidgets, QtCore, QtGui
    except ImportError:
        QtWidgets = QtCore = QtGui = None


# ──────────────────────────────────────────────────────────────────────
# Unit parsing — proper sequential parser, not one monster regex
# ──────────────────────────────────────────────────────────────────────
_UNIT_MM = {
    "mm": 1.0, "millimeter": 1.0, "millimeters": 1.0, "millimetre": 1.0,
    "cm": 10.0, "centimeter": 10.0, "centimeters": 10.0,
    "m": 1000.0, "meter": 1000.0, "meters": 1000.0, "metre": 1000.0, "metres": 1000.0,
    "in": 25.4, "inch": 25.4, "inches": 25.4,
    "ft": 304.8, "foot": 304.8, "feet": 304.8,
    "yd": 914.4, "yard": 914.4, "yards": 914.4,
}

# Step 1: Feet-inches compound patterns
_RE_FT_IN = re.compile(
    r"""^\s*(\d+(?:\.\d+)?)\s*(?:'|ft|feet)\s*[-–]?\s*
        (\d+(?:\.\d+)?)?\s*
        (?:(\d+)\s*/\s*(\d+))?\s*
        (?:"|in|inch|inches)?\s*$""",
    re.VERBOSE | re.IGNORECASE)

# Step 2: Mixed number + unit:  "1 1/2 in"  "3 3/4 ft"
_RE_MIXED = re.compile(
    r"""^\s*(\d+(?:\.\d+)?)\s+(\d+)\s*/\s*(\d+)\s*
        ([a-zA-Z"']+)?\s*$""",
    re.VERBOSE)

# Step 3: Fraction + unit:  "1/2 in"  "3/8"
_RE_FRAC = re.compile(
    r"""^\s*(\d+)\s*/\s*(\d+)\s*
        ([a-zA-Z"']+)?\s*$""",
    re.VERBOSE)

# Step 4: Decimal + unit:  "406.4 mm"  "4.92 in"  "120"
_RE_DECIMAL = re.compile(
    r"""^\s*(\d+(?:\.\d+)?)\s*
        ([a-zA-Z"']+)?\s*$""",
    re.VERBOSE)

# Step 5: Feet-inches with dash shorthand:  "1'-4"  "5'-6 1/2"
_RE_FT_DASH = re.compile(
    r"""^\s*(\d+(?:\.\d+)?)\s*'\s*-?\s*
        (\d+(?:\.\d+)?)\s*
        (?:(\d+)\s*/\s*(\d+))?\s*
        "?\s*$""",
    re.VERBOSE)


def parse_dimension_mm(text: str) -> float:
    """Parse a dimension string into millimeters.

    Parse order (most specific first):
      1. Feet-inches compound:  5'-6"  5' 6 1/2"  5ft 6in
      2. Feet-dash shorthand:   1'-4   5'-6 1/2
      3. Mixed number + unit:   1 1/2 in   3 3/4 ft
      4. Fraction + unit:       1/2 in   3/8
      5. Decimal + unit:        406.4 mm   4.92 in   120
      6. Bare number → mm
    """
    text = text.strip()
    if not text:
        raise ValueError("Empty dimension")

    # Normalize curly quotes and dashes
    text = text.replace('\u2019', "'").replace('\u201D', '"')
    text = text.replace('\u2013', '-').replace('\u2014', '-')

    # 1. Feet-inches compound:  5'-6"  5' 6 1/2"  5ft 6in
    m = _RE_FT_IN.match(text)
    if m:
        ft = float(m.group(1))
        inches = float(m.group(2) or 0)
        if m.group(3) and m.group(4):
            inches += int(m.group(3)) / int(m.group(4))
        return (ft * 12.0 + inches) * 25.4

    # 2. Feet-dash shorthand:  1'-4   5'-6 1/2
    m = _RE_FT_DASH.match(text)
    if m:
        ft = float(m.group(1))
        inches = float(m.group(2))
        if m.group(3) and m.group(4):
            inches += int(m.group(3)) / int(m.group(4))
        return (ft * 12.0 + inches) * 25.4

    # 3. Mixed number + unit:  1 1/2 in   3 3/4 ft
    m = _RE_MIXED.match(text)
    if m:
        whole = float(m.group(1))
        frac = int(m.group(2)) / int(m.group(3))
        unit = (m.group(4) or "").strip().strip("'\"").lower()
        factor = _UNIT_MM.get(unit, 1.0)
        return (whole + frac) * factor

    # 4. Fraction + unit:  1/2 in   3/8
    m = _RE_FRAC.match(text)
    if m:
        frac = int(m.group(1)) / int(m.group(2))
        unit = (m.group(3) or "").strip().strip("'\"").lower()
        factor = _UNIT_MM.get(unit, 1.0)
        return frac * factor

    # 5. Decimal + unit:  406.4 mm   4.92 in   120
    m = _RE_DECIMAL.match(text)
    if m:
        value = float(m.group(1))
        unit = (m.group(2) or "").strip().strip("'\"").lower()
        # Handle ' and " as unit suffixes directly from input
        raw_unit = (m.group(2) or "").strip()
        if raw_unit == "'":
            return value * 304.8
        if raw_unit == '"':
            return value * 25.4
        factor = _UNIT_MM.get(unit, 1.0)
        return value * factor

    # 6. Bare number fallback → mm
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"Cannot parse dimension: '{text}'") from None


# ──────────────────────────────────────────────────────────────────────
# Get selected points from FreeCAD selection
# ──────────────────────────────────────────────────────────────────────
def _get_selected_points() -> list:
    """Extract 3D points from the current FreeCAD selection.
    
    Tries multiple strategies:
    1. Sub-element vertices (user clicked a specific vertex)
    2. Edge endpoints (user clicked a line/wire)
    3. Picked points on faces/edges (click position on geometry)
    4. Shape bounding box centers (user selected whole objects)
    """
    points = []
    sel = FreeCADGui.Selection.getSelectionEx()

    for selobj in sel:
        # Strategy 1 & 2: Sub-element selections
        for sub in selobj.SubObjects:
            if hasattr(sub, "Point"):
                points.append(sub.Point)
            elif hasattr(sub, "Vertexes") and sub.Vertexes:
                for v in sub.Vertexes:
                    points.append(v.Point)

        # Strategy 3: Picked points (where the user actually clicked)
        if hasattr(selobj, "PickedPoints") and selobj.PickedPoints:
            for pp in selobj.PickedPoints:
                points.append(pp)

        # Strategy 4: If we still have nothing, try the object's shape
        if not points and selobj.Object:
            obj = selobj.Object
            if hasattr(obj, "Shape") and hasattr(obj.Shape, "Vertexes"):
                verts = obj.Shape.Vertexes
                if verts:
                    # Use first and last vertex of the shape
                    points.append(verts[0].Point)
                    if len(verts) > 1:
                        points.append(verts[-1].Point)
            elif hasattr(obj, "Placement"):
                points.append(obj.Placement.Base)

    # Deduplicate very close points
    if len(points) > 2:
        unique = [points[0]]
        for p in points[1:]:
            if all(math.hypot(p.x - u.x, p.y - u.y) > 0.001 for u in unique):
                unique.append(p)
        points = unique

    return points



def _collect_scale_targets(objects):
    """Flatten groups and filter duplicate children so objects are only scaled once."""
    seen = set()
    out = []

    def walk(obj):
        if obj is None:
            return
        name = getattr(obj, "Name", None)
        if name and name in seen:
            return
        if name:
            seen.add(name)
        if hasattr(obj, "Group") and obj.isDerivedFrom("App::DocumentObjectGroup"):
            for child in obj.Group:
                walk(child)
            return
        out.append(obj)

    for obj in objects or []:
        walk(obj)
    return out


def _transform_shape_about_origin(shape, factor: float, origin: "Vector"):
    """Scale a FreeCAD shape about an arbitrary origin without double-moving it."""
    if shape is None or shape.isNull():
        return shape
    mat = FreeCAD.Matrix()
    mat.move(-origin)
    mat.scale(factor, factor, factor)
    mat.move(origin)
    return shape.transformGeometry(mat)


# ──────────────────────────────────────────────────────────────────────
# Scale Dialog
# ──────────────────────────────────────────────────────────────────────
class ScaleDialog(QtWidgets.QDialog):
    """Shows measured distance and asks for the real-world dimension."""

    def __init__(self, measured_mm: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scale by Reference — BlueCollar-Systems")
        self.setMinimumWidth(440)

        self._measured = measured_mm
        self._result_mm = None

        measured_in = measured_mm / 25.4

        lbl_info = QtWidgets.QLabel(
            f"Measured distance between your two picks:\n"
            f"  {measured_mm:.4f} mm   ({measured_in:.4f} in)")
        lbl_info.setStyleSheet("font-weight: bold; font-size: 11pt;")

        lbl_prompt = QtWidgets.QLabel(
            "Enter the REAL dimension this distance should be:\n"
            "  Examples:  120 mm  ·  4.92 in  ·  5'-6\"  ·  1/2 in  ·  3.5 ft")

        self.dim_edit = QtWidgets.QLineEdit()
        self.dim_edit.setPlaceholderText("e.g. 1'-4  or  16 in  or  406.4 mm")
        self.dim_edit.setStyleSheet("font-size: 14pt; padding: 4px;")
        self.dim_edit.setFocus()
        self.dim_edit.textChanged.connect(self._update_preview)

        self.preview_label = QtWidgets.QLabel("")

        # Target group selector
        self.group_combo = QtWidgets.QComboBox()
        self._populate_groups()

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)

        form = QtWidgets.QFormLayout()
        form.addRow(lbl_info)
        form.addRow(lbl_prompt)
        form.addRow("Real dimension:", self.dim_edit)
        form.addRow(self.preview_label)
        form.addRow("Scale target:", self.group_combo)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(btns)

    def _populate_groups(self):
        self.group_combo.addItem("Entire document", None)
        doc = FreeCAD.ActiveDocument
        if doc:
            for obj in doc.Objects:
                if obj.isDerivedFrom("App::DocumentObjectGroup"):
                    self.group_combo.addItem(obj.Label, obj.Name)

    def _update_preview(self, text):
        try:
            real = parse_dimension_mm(text)
            ratio = real / self._measured
            self.preview_label.setText(
                f"Scale factor: {ratio:.6f}  ({real:.2f} mm)")
            self.preview_label.setStyleSheet("color: green; font-weight: bold;")
        except (ValueError, ZeroDivisionError):
            self.preview_label.setText("(enter a valid dimension)")
            self.preview_label.setStyleSheet("color: gray;")

    def _on_ok(self):
        try:
            self._result_mm = parse_dimension_mm(self.dim_edit.text())
            self.accept()
        except (ValueError, ZeroDivisionError) as e:
            QtWidgets.QMessageBox.warning(
                self, "Invalid Input", f"Cannot parse dimension:\n{e}")

    @property
    def real_mm(self) -> float:
        return self._result_mm or self._measured

    @property
    def target_group_name(self) -> str:
        return self.group_combo.currentData()


# ──────────────────────────────────────────────────────────────────────
# Manual Point Entry Dialog (fallback when nothing is selected)
# ──────────────────────────────────────────────────────────────────────
class PointEntryDialog(QtWidgets.QDialog):
    """Enter two reference point coordinates manually."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Scale by Reference — Pick Two Points")
        self.setMinimumWidth(400)

        info = QtWidgets.QLabel(
            "Enter coordinates of two points on a known dimension.\n\n"
            "TIP: Hover over vertices in FreeCAD — the coordinates\n"
            "show in the status bar at the bottom of the window.\n"
            "Or select two vertices/edges BEFORE clicking this button.")
        info.setStyleSheet("font-size: 10pt;")

        self.x1 = self._spin(); self.y1 = self._spin()
        self.x2 = self._spin(); self.y2 = self._spin()

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)

        form = QtWidgets.QFormLayout()
        form.addRow(info)
        form.addRow("Point 1 — X:", self.x1)
        form.addRow("Point 1 — Y:", self.y1)
        form.addRow("", QtWidgets.QLabel(""))
        form.addRow("Point 2 — X:", self.x2)
        form.addRow("Point 2 — Y:", self.y2)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(btns)

    def _spin(self):
        s = QtWidgets.QDoubleSpinBox()
        s.setRange(-1e9, 1e9)
        s.setDecimals(4)
        s.setSuffix(" mm")
        return s

    def get_points(self):
        return (Vector(self.x1.value(), self.y1.value(), 0),
                Vector(self.x2.value(), self.y2.value(), 0))


# ──────────────────────────────────────────────────────────────────────
# Scaling logic
# ──────────────────────────────────────────────────────────────────────
def _scale_objects(objects, factor: float, origin: "Vector"):
    """Scale a list of FreeCAD objects uniformly about an origin point.
    
    Uses direct shape transformation instead of Draft.scale() for speed.
    Draft.scale() copies/recreates each object individually — painfully slow
    for 6000+ objects. Direct transform modifies geometry in-place.
    """
    n = len(objects)
    progress = None

    if FreeCAD.GuiUp and QtWidgets and n > 20:
        try:
            progress = QtWidgets.QProgressDialog(
                "Scaling geometry...", "Cancel", 0, n)
            progress.setWindowTitle("Scale by Reference")
            progress.setMinimumDuration(300)
            progress.setWindowModality(QtCore.Qt.WindowModal)
        except (AttributeError, RuntimeError):
            progress = None

    # Build a scale matrix once when scaling about global zero
    mat = None
    if origin is None or (abs(origin.x) < 1e-12 and abs(origin.y) < 1e-12 and abs(origin.z) < 1e-12):
        mat = FreeCAD.Matrix()
        mat.scale(factor, factor, factor)

    scaled = 0
    for i, obj in enumerate(objects):
        if progress and i % 200 == 0:
            progress.setValue(i)
            progress.setLabelText(f"Scaling... {i}/{n}")
            if progress.wasCanceled():
                FreeCAD.Console.PrintWarning("Scale cancelled.\n")
                break
            try:
                QtWidgets.QApplication.processEvents()
            except (AttributeError, RuntimeError):
                pass

        try:
            has_shape = hasattr(obj, "Shape") and not obj.Shape.isNull()
            has_font = (hasattr(obj, "ViewObject") 
                        and hasattr(obj.ViewObject, "FontSize"))

            if has_shape:
                # Shape objects: transformGeometry handles all coordinates
                # Do NOT also move Placement — that would double-move
                obj.Shape = _transform_shape_about_origin(obj.Shape, factor, origin) if mat is None else obj.Shape.transformGeometry(mat)
                scaled += 1
            elif has_font:
                # Text/annotation objects: scale position + font size
                if hasattr(obj, "Placement"):
                    pos = obj.Placement.Base
                    obj.Placement.Base = origin + (pos - origin) * factor
                try:
                    obj.ViewObject.FontSize = obj.ViewObject.FontSize * factor
                except (AttributeError, TypeError):
                    pass
                scaled += 1
            else:
                # Other objects (groups, etc.): just scale placement
                if hasattr(obj, "Placement"):
                    pos = obj.Placement.Base
                    obj.Placement.Base = origin + (pos - origin) * factor
        except (AttributeError, TypeError, ValueError, RuntimeError):
            pass

    if progress:
        progress.setValue(n)
        progress.close()

    FreeCAD.Console.PrintMessage(f"Transformed {scaled} shapes.\n")


def apply_scale(measured_mm: float, real_mm: float, target_group_name=None, origin=None):
    """Scale geometry by ratio real/measured about the picked origin."""
    if measured_mm <= 0:
        return
    factor = real_mm / measured_mm
    doc = FreeCAD.ActiveDocument
    if not doc:
        return

    if target_group_name:
        grp = doc.getObject(target_group_name)
        if grp and hasattr(grp, "Group"):
            objects = _collect_scale_targets(grp.Group[:])
        else:
            objects = _collect_scale_targets(doc.Objects[:])
    else:
        objects = _collect_scale_targets(doc.Objects[:])

    FreeCAD.Console.PrintMessage(
        f"Scaling {len(objects)} objects by {factor:.6f}...\n")

    # Wrap in undo transaction so the user can Ctrl+Z to undo the scale
    doc.openTransaction("Scale by Reference")

    if origin is None:
        origin = Vector(0, 0, 0)
    _scale_objects(objects, factor, origin)
    doc.recompute()
    doc.commitTransaction()

    # Fit view to new extents
    try:
        FreeCADGui.ActiveDocument.ActiveView.fitAll()
    except (AttributeError, RuntimeError):
        pass

    FreeCAD.Console.PrintMessage(
        f"Scaled by {factor:.6f}  ({real_mm:.2f} mm / {measured_mm:.2f} mm)\n")


# ──────────────────────────────────────────────────────────────────────
# FreeCAD Commands
# ──────────────────────────────────────────────────────────────────────
class ScaleByReferenceCommand:
    """Scale by picking two reference points on a known dimension."""

    def GetResources(self):
        return {
            "Pixmap": "",
            "MenuText": "Scale by Reference…",
            "ToolTip": ("Select two vertices or an edge, then click this.\n"
                        "Type the real-world measurement and everything scales.\n"
                        "Like SketchUp's tape-measure scale."),
        }

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        # Try to get points from the current selection first
        points = _get_selected_points()

        if len(points) >= 2:
            p1, p2 = points[0], points[1]
            measured = math.hypot(p2.x - p1.x, p2.y - p1.y)
            if measured < 1e-9:
                QtWidgets.QMessageBox.warning(
                    None, "Scale Tool",
                    "Selected points are identical — cannot measure distance.")
                return
            FreeCAD.Console.PrintMessage(
                f"Scale Tool: measured {measured:.4f} mm between selected points\n")
            self._show_scale_dialog(measured, points[0])
        else:
            # Nothing useful selected — guide the user
            reply = QtWidgets.QMessageBox.question(
                None, "Scale by Reference",
                "No geometry selected.\n\n"
                "To use this tool:\n"
                "  1. Click on a LINE (wire edge) with a known length\n"
                "  2. Then click 'Scale by Reference' again\n\n"
                "Or click 'Yes' to enter coordinates manually.",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No)

            if reply == QtWidgets.QMessageBox.Yes:
                dlg = PointEntryDialog()
                exec_fn = getattr(dlg, "exec", None) or getattr(dlg, "exec_", None)
                if exec_fn and exec_fn() == QtWidgets.QDialog.Accepted:
                    p1, p2 = dlg.get_points()
                    measured = math.hypot(p2.x - p1.x, p2.y - p1.y)
                    if measured < 1e-9:
                        QtWidgets.QMessageBox.warning(
                            None, "Scale Tool", "Points are identical.")
                        return
                    self._show_scale_dialog(measured, p1)

    def _show_scale_dialog(self, measured_mm: float, origin=None):
        dlg = ScaleDialog(measured_mm)
        exec_fn = getattr(dlg, "exec", None) or getattr(dlg, "exec_", None)
        if exec_fn and exec_fn() == QtWidgets.QDialog.Accepted:
            apply_scale(measured_mm, dlg.real_mm, dlg.target_group_name, origin=origin)


class QuickScaleCommand:
    """Scale by a typed factor or ratio."""

    def GetResources(self):
        return {
            "Pixmap": "",
            "MenuText": "Quick Scale Factor…",
            "ToolTip": "Enter a scale factor (2.0 = double) or ratio (1:50).",
        }

    def IsActive(self):
        return FreeCAD.ActiveDocument is not None

    def Activated(self):
        text, ok = QtWidgets.QInputDialog.getText(
            None, "Quick Scale",
            "Enter scale factor (e.g. 2.0) or ratio (e.g. 1:50):",
            text="1.0")
        if not ok or not text.strip():
            return
        try:
            t = text.strip()
            if ":" in t:
                a, b = t.split(":", 1)
                factor = float(a) / float(b)
            else:
                factor = float(t)
        except (ValueError, ZeroDivisionError):
            QtWidgets.QMessageBox.warning(
                None, "Invalid", f"Cannot parse '{text}' as a scale factor.")
            return

        doc = FreeCAD.ActiveDocument
        sel = FreeCADGui.Selection.getSelection()
        objects = sel if sel else doc.Objects
        _scale_objects(_collect_scale_targets(list(objects)), factor, Vector(0, 0, 0))
        doc.recompute()

        try:
            FreeCADGui.ActiveDocument.ActiveView.fitAll()
        except (AttributeError, RuntimeError):
            pass

        FreeCAD.Console.PrintMessage(f"Quick scaled by {factor:.6f}\n")
