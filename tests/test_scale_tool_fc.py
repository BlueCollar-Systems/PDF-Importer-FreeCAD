"""Scale by Reference and Quick Scale resize everything on the drawing.

Pictures (Image::ImagePlane) and the white paper sheet used to keep their old
size while the lines grew, and Quick Scale read the title-block scale "1:50"
as "shrink 50 times". These tests drive the real PDFScaleTool module against a
small fake FreeCAD so they run without FreeCAD installed.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"


class Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)

    def __add__(self, other):
        return Vector(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other):
        return Vector(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, factor):
        return Vector(self.x * factor, self.y * factor, self.z * factor)

    def __neg__(self):
        return Vector(-self.x, -self.y, -self.z)

    def __iter__(self):
        return iter((self.x, self.y, self.z))

    def __repr__(self):
        return f"Vector({self.x}, {self.y}, {self.z})"


class Matrix:
    """Uniform scale + translation, composed like FreeCAD's Matrix (M' = op * M)."""

    def __init__(self):
        self.s = 1.0
        self.t = Vector()

    def move(self, v):
        self.t = self.t + v

    def scale(self, sx, sy=None, sz=None):
        assert sy in (None, sx) and sz in (None, sx)
        self.s *= sx
        self.t = self.t * sx

    def apply(self, p):
        return p * self.s + self.t


class Box:
    def __init__(self, x0, y0, x1, y1):
        self.XMin, self.YMin, self.XMax, self.YMax = x0, y0, x1, y1

    def as_tuple(self):
        return tuple(round(v, 6) for v in (self.XMin, self.YMin, self.XMax, self.YMax))


class Shape:
    def __init__(self, box):
        self.BoundBox = box
        self.matrices = []

    def isNull(self):
        return False

    def transformGeometry(self, mat):
        lo = mat.apply(Vector(self.BoundBox.XMin, self.BoundBox.YMin))
        hi = mat.apply(Vector(self.BoundBox.XMax, self.BoundBox.YMax))
        out = Shape(Box(min(lo.x, hi.x), min(lo.y, hi.y), max(lo.x, hi.x), max(lo.y, hi.y)))
        out.matrices = self.matrices + [mat]
        return out


class Placement:
    def __init__(self, base):
        self.Base = base


class Obj:
    def __init__(self, name, type_id, **values):
        self.Name = name
        self.TypeId = type_id
        self.ViewObject = None
        self.__dict__.update(values)

    def isDerivedFrom(self, kind):
        return kind == self.TypeId


class Doc:
    def __init__(self, objects):
        self.Objects = list(objects)
        self.log = []
        self.UndoMode = 1

    def getObject(self, name):
        return next((o for o in self.Objects if o.Name == name), None)

    def openTransaction(self, name):
        self.log.append(("open", name))

    def commitTransaction(self):
        self.log.append(("commit",))

    def abortTransaction(self):
        self.log.append(("abort",))

    def recompute(self):
        self.log.append(("recompute",))


@pytest.fixture
def tool(monkeypatch):
    console = types.SimpleNamespace(
        PrintMessage=lambda *_a: None, PrintWarning=lambda *_a: None,
        PrintError=lambda *_a: None)
    freecad = types.ModuleType("FreeCAD")
    freecad.Vector = Vector
    freecad.Matrix = Matrix
    freecad.Console = console
    freecad.GuiUp = False
    freecad.ActiveDocument = None
    gui = types.ModuleType("FreeCADGui")
    gui.Selection = types.SimpleNamespace(getSelection=lambda: [])
    gui.ActiveDocument = None
    part = types.ModuleType("Part")

    class _Widget:
        def __init__(self, *_a, **_k):
            pass

    qtw = types.SimpleNamespace(QDialog=_Widget)
    pyside = types.ModuleType("PySide6")
    pyside.QtWidgets = qtw
    pyside.QtCore = types.SimpleNamespace()
    pyside.QtGui = types.SimpleNamespace()
    for name, module in (("FreeCAD", freecad), ("FreeCADGui", gui), ("Part", part),
                         ("PySide6", pyside)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.syspath_prepend(str(SRC))
    spec = importlib.util.spec_from_file_location("PDFScaleTool_under_test",
                                                  SRC / "PDFScaleTool.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _picture(name="PDF_Image_p1_i1", size=(100.0, 50.0), centre=(60.0, 35.0)):
    return Obj(name, "Image::ImagePlane", XSize=size[0], YSize=size[1],
               Placement=Placement(Vector(*centre)))


def _frame(box=(10.0, 10.0, 110.0, 60.0)):
    return Obj("Frame", "Part::Feature", Shape=Shape(Box(*box)),
               Placement=Placement(Vector()))


def _extent(pic):
    b = pic.Placement.Base
    return tuple(round(v, 6) for v in (b.x - pic.XSize / 2, b.y - pic.YSize / 2,
                                       b.x + pic.XSize / 2, b.y + pic.YSize / 2))


# ── parse_drawing_scale ─────────────────────────────────────────────────


@pytest.mark.parametrize("text,factor", [
    ("1:50", 50.0),
    ("1:1", 1.0),
    ("2:1", 0.5),
    ("1 : 100", 100.0),
    ('1/4"=1\'-0"', 48.0),
    ('3"=1\'-0"', 4.0),
    ('1-1/2"=1\'-0"', 8.0),
    ('1 1/2" = 1\'-0"', 8.0),
    ('3/16"=1\'-0"', 64.0),
    ('1/8"=1\'', 96.0),
    ('1"=10\'-0"', 120.0),
    ("1/4\u201d=1\u2019-0\u201d", 48.0),
    ("2", 2.0),
    ("0.5", 0.5),
])
def test_parse_drawing_scale(tool, text, factor):
    assert tool.parse_drawing_scale(text) == pytest.approx(factor)


@pytest.mark.parametrize("text", ["0", "-1", "1:0", "0:1", "-1:50", "abc", "", "nan",
                                  "inf", '0"=1\'-0"', "1:"])
def test_parse_drawing_scale_rejects_nonsense_in_plain_words(tool, text):
    with pytest.raises(ValueError) as caught:
        tool.parse_drawing_scale(text)
    assert "1:50" in str(caught.value)


@pytest.mark.parametrize("label", [
    "1:1", "1:2", "1:5", "1:10", "1:20", "1:25", "1:50", "1:100", "1:200",
    '3"=1\'-0"', '1-1/2"=1\'-0"', '1"=1\'-0"', '3/4"=1\'-0"', '1/2"=1\'-0"',
    '3/8"=1\'-0"', '1/4"=1\'-0"', '3/16"=1\'-0"', '1/8"=1\'-0"',
])
def test_every_preset_parses(tool, label):
    assert label in tool.DRAWING_SCALE_PRESETS
    assert tool.parse_drawing_scale(label) >= 1.0


def test_confirmation_says_which_way_the_drawing_changes(tool):
    assert tool.quick_scale_question("1:50", 50.0) == (
        "Enlarge the drawing 50x (1:50 to full size)?")
    assert "Shrink" in tool.quick_scale_question("2:1", 0.5)
    assert "custom factor" in tool.quick_scale_question("2", 2.0)


# ── pictures, paper and labels follow the lines ─────────────────────────


@pytest.mark.parametrize("origin,factor", [
    ((0.0, 0.0), 2.0),
    ((10.0, 10.0), 2.0),
    ((0.0, 0.0), 0.5),
])
def test_picture_stays_on_its_frame(tool, origin, factor):
    pic = _picture()
    frame = _frame()
    assert _extent(pic) == frame.Shape.BoundBox.as_tuple()
    doc = Doc([pic, frame])
    tool.FreeCAD.ActiveDocument = doc

    tool.apply_scale(100.0, 100.0 * factor, origin=Vector(*origin))

    assert pic.XSize == pytest.approx(100.0 * factor)
    assert pic.YSize == pytest.approx(50.0 * factor)
    assert _extent(pic) == pytest.approx(frame.Shape.BoundBox.as_tuple())
    assert doc.log[0] == ("open", "Scale by Reference")
    assert doc.log[-1] == ("commit",)


def test_picture_doubles_about_global_zero(tool):
    pic = _picture()
    frame = _frame()
    tool.FreeCAD.ActiveDocument = Doc([pic, frame])

    tool.apply_scale(100.0, 200.0, origin=Vector(0, 0, 0))

    assert (pic.XSize, pic.YSize) == (200.0, 100.0)
    assert (pic.Placement.Base.x, pic.Placement.Base.y) == (120.0, 70.0)
    assert _extent(pic) == (20.0, 20.0, 220.0, 120.0)
    assert frame.Shape.BoundBox.as_tuple() == (20.0, 20.0, 220.0, 120.0)


def test_paper_sheet_grows_with_the_drawing(tool):
    import PDFPaperDisplay as paper  # src is on sys.path via the fixture

    corners = [[0.0, 0.0, 0.0], [216.0, 0.0, 0.0], [216.0, 279.0, 0.0], [0.0, 279.0, 0.0]]
    data = {"schema": paper.SCHEMA, "page": 1, "source_sha256": "a" * 64,
            "corners_mm": corners, "display_z_mm": -1.0}
    sheet = Obj("PDF_Paper", "App::FeaturePython", Placement=Placement(Vector(0, -340, 0)))
    setattr(sheet, paper.PROPERTY, json.dumps(data, sort_keys=True))
    tool.FreeCAD.ActiveDocument = Doc([sheet])

    tool.apply_scale(10.0, 20.0, origin=Vector(0, 0, 0))

    after = paper.validate(json.loads(getattr(sheet, paper.PROPERTY)))
    assert after["corners_mm"] == [[2 * x, 2 * y, z] for x, y, z in corners]
    assert after["display_z_mm"] == -1.0
    assert (sheet.Placement.Base.x, sheet.Placement.Base.y) == (0.0, -680.0)
    box = paper.placed_sheet_box(after["corners_mm"],
                                 (sheet.Placement.Base.x, sheet.Placement.Base.y))
    assert box == (0.0, -680.0, 432.0, -122.0)


def test_label_leader_target_follows_the_drawing(tool):
    label = Obj("PDF_Label", "App::FeaturePython",
                Proxy=types.SimpleNamespace(Type="Label"),
                Placement=Placement(Vector(10, 20, 0)),
                TargetPoint=Vector(30, 40, 0),
                Points=[Vector(10, 20, 0), Vector(30, 40, 0)])
    tool.FreeCAD.ActiveDocument = Doc([label])

    tool.apply_scale(1.0, 3.0, origin=Vector(0, 0, 0))

    assert (label.Placement.Base.x, label.Placement.Base.y) == (30.0, 60.0)
    assert (label.TargetPoint.x, label.TargetPoint.y) == (90.0, 120.0)
    assert [(p.x, p.y) for p in label.Points] == [(30.0, 60.0), (90.0, 120.0)]


# ── Quick Scale command ─────────────────────────────────────────────────


def _quick_scale(tool, monkeypatch, typed, confirm=True, selection=()):
    asked = {}

    def get_item(_parent, title, label, items, current, editable):
        asked.update(title=title, label=label, items=list(items),
                     current=current, editable=editable)
        return typed, True

    def question(_parent, title, text, *_rest):
        asked["question"] = text
        return box.Yes if confirm else box.No

    box = types.SimpleNamespace(
        Yes=1, No=2, question=question,
        warning=lambda *a: asked.setdefault("warning", a[2]),
        information=lambda *a: asked.setdefault("info", a[2]))
    monkeypatch.setattr(tool, "QtWidgets", types.SimpleNamespace(
        QInputDialog=types.SimpleNamespace(getItem=get_item), QMessageBox=box))
    tool.FreeCADGui.Selection = types.SimpleNamespace(getSelection=lambda: list(selection))
    tool.QuickScaleCommand().Activated()
    return asked


def test_quick_scale_1_to_50_enlarges_fifty_times_in_one_undo_step(tool, monkeypatch):
    frame = _frame((0.0, 0.0, 2.0, 1.0))
    pic = _picture(size=(2.0, 1.0), centre=(1.0, 0.5))
    doc = Doc([frame, pic])
    tool.FreeCAD.ActiveDocument = doc

    asked = _quick_scale(tool, monkeypatch, "1:50")

    assert asked["editable"] is True
    assert "1:50" in asked["items"] and '1/4"=1\'-0"' in asked["items"]
    assert "title block" in asked["title"]
    assert asked["question"].startswith("Enlarge the drawing 50x (1:50 to full size)?")
    assert frame.Shape.BoundBox.as_tuple() == (0.0, 0.0, 100.0, 50.0)
    assert (pic.XSize, pic.YSize) == (100.0, 50.0)
    assert doc.log == [("open", "Quick Scale"), ("recompute",), ("commit",)]


def test_quick_scale_declined_changes_nothing(tool, monkeypatch):
    frame = _frame()
    doc = Doc([frame])
    tool.FreeCAD.ActiveDocument = doc

    _quick_scale(tool, monkeypatch, "1:50", confirm=False)

    assert frame.Shape.BoundBox.as_tuple() == (10.0, 10.0, 110.0, 60.0)
    assert doc.log == []


def test_quick_scale_bad_entry_warns_and_changes_nothing(tool, monkeypatch):
    frame = _frame()
    doc = Doc([frame])
    tool.FreeCAD.ActiveDocument = doc

    asked = _quick_scale(tool, monkeypatch, "fifty")

    assert "1:50" in asked["warning"]
    assert frame.Shape.BoundBox.as_tuple() == (10.0, 10.0, 110.0, 60.0)
    assert doc.log == []


def test_quick_scale_tooltip_explains_the_direction(tool):
    resources = tool.QuickScaleCommand().GetResources()
    assert "full size" in resources["ToolTip"]
    assert "1:50" in resources["ToolTip"]
    assert "Quick Scale" in resources["MenuText"]
