"""Locks for the dense-page 3D Text performance levers (week-plan PH1-FC-2).

P1: a span tessellates exactly once — the ShapeString host is constructed
without the Draft factory's default-Size recompute, and every custom property
write lands BEFORE the object's only recompute so the page-end document
recompute has nothing left to re-execute (post-recompute writes re-touch
objects; measured as a full second tessellation pass on the owner's dense
chart).
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


class EventLog:
    def __init__(self):
        self.events = []

    def index(self, kind, obj):
        for position, (event_kind, event_obj) in enumerate(self.events):
            if event_kind == kind and event_obj is obj:
                return position
        raise AssertionError("event %s for %r not recorded" % (kind, obj))


class FakeVertex:
    def __init__(self, x, y):
        self.Point = types.SimpleNamespace(x=float(x), y=float(y), z=0.0)


class FakeShape:
    def __init__(self, width=12.0, solid=False):
        self.Faces = [object()]
        self.Solids = [object()] if solid else []
        self.Volume = 4.0 if solid else 0.0
        self.Vertexes = [FakeVertex(0.0, 0.0), FakeVertex(width, 0.0)]

    def isNull(self):
        return False


class FakeHost:
    def __init__(self, doc, name, type_id):
        self.Document = doc
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.PropertiesList = ["Visibility"]
        self.Visibility = True
        self.ViewObject = None
        self.Shape = FakeShape()

    def addProperty(self, kind, name, group):
        self.PropertiesList.append(name)


class FakeDoc:
    def __init__(self, log):
        self.log = log
        self.Objects = []

    def addObject(self, kind, name):
        host = FakeHost(self, "%s_%d" % (name, len(self.Objects)), kind)
        if kind == "Part::Extrusion":
            host.Shape = FakeShape(solid=True)
            host.Base = None
            host.Dir = None
            host.Solid = False
        self.Objects.append(host)
        self.log.events.append(("add", host))
        return host

    def removeObject(self, name):
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self, objs=None):
        for obj in objs or []:
            self.log.events.append(("recompute", obj))
        return len(objs or [])


class FakeGroup:
    def __init__(self, doc):
        self.Document = doc
        self.objects = []

    def addObject(self, obj):
        self.objects.append(obj)


class FakeShapeStringProxy:
    """Stands in for draftobjects.shapestring.ShapeString (no recompute)."""

    def __init__(self, obj):
        obj.Proxy = self
        obj.String = ""
        obj.FontFile = ""
        obj.Tracking = 0


@pytest.fixture()
def fake_draft_module(monkeypatch):
    module = types.ModuleType("draftobjects.shapestring")
    module.ShapeString = FakeShapeStringProxy
    package = types.ModuleType("draftobjects")
    package.shapestring = module
    monkeypatch.setitem(sys.modules, "draftobjects", package)
    monkeypatch.setitem(sys.modules, "draftobjects.shapestring", module)
    return module


def test_make_shapestring_host_defers_tessellation(fake_draft_module, monkeypatch):
    log = EventLog()
    doc = FakeDoc(log)
    factory_calls = []
    monkeypatch.setattr(
        core,
        "Draft",
        types.SimpleNamespace(
            make_shapestring=lambda *a: factory_calls.append(a)
        ),
    )

    host = core._make_shapestring_host(doc, "W12x30", "font.otf")

    assert host in doc.Objects
    assert host.TypeId == "Part::Part2DObjectPython"
    assert host.String == "W12x30"
    assert host.FontFile == "font.otf"
    assert host.Tracking == 0
    assert not factory_calls, "direct construction must bypass the Draft factory"
    recomputes = [event for event in log.events if event[0] == "recompute"]
    assert not recomputes, "construction must not tessellate (no recompute)"


def test_make_shapestring_host_falls_back_to_draft_factory(monkeypatch):
    for name in ("draftobjects", "draftobjects.shapestring"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    sentinel = object()
    calls = []

    def factory(text, font):
        calls.append((text, font))
        return sentinel

    monkeypatch.setattr(
        core, "Draft", types.SimpleNamespace(make_shapestring=factory)
    )
    host = core._make_shapestring_host(None, "AB", "font.ttf")
    assert host is sentinel
    assert calls == [("AB", "font.ttf")]


def test_make_shapestring_host_without_any_api_raises(monkeypatch):
    for name in ("draftobjects", "draftobjects.shapestring"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setattr(core, "Draft", types.SimpleNamespace())
    with pytest.raises(AttributeError):
        core._make_shapestring_host(None, "AB", "font.ttf")


def test_configure_host_runs_before_every_recompute(monkeypatch):
    """Each host object's custom writes land before its only recompute."""
    log = EventLog()
    doc = FakeDoc(log)
    group = FakeGroup(doc)

    shape_string = FakeHost(doc, "ShapeString", "Part::Part2DObjectPython")
    doc.Objects.append(shape_string)

    class FakeClone(FakeHost):
        @property
        def Scale(self):
            return self._scale

        @Scale.setter
        def Scale(self, value):
            self._scale = value
            self.Shape = FakeShape(width=12.0 * float(value.x))

    def clone(source):
        host = FakeClone(doc, "Clone", "Part::Part2DObjectPython")
        doc.Objects.append(host)
        log.events.append(("add", host))
        return host

    monkeypatch.setattr(core, "Draft", types.SimpleNamespace(clone=clone))
    monkeypatch.setattr(
        core, "Vector", lambda x, y, z: types.SimpleNamespace(x=x, y=y, z=z)
    )

    configured = []

    def configure_host(host_obj):
        configured.append(host_obj)
        log.events.append(("configure", host_obj))

    extrusion, calibrated, x_scale, advance = core._create_verified_text3d_entity(
        shape_string,
        font_size_fc=2.5,
        depth=0.3,
        target_advance_fc=6.0,
        baseline_angle_deg=0.0,
        text_group=group,
        configure_host=configure_host,
    )

    assert configured == [shape_string, calibrated, extrusion]
    for host_obj in (shape_string, calibrated, extrusion):
        recompute_count = sum(
            1
            for kind, event_obj in log.events
            if kind == "recompute" and event_obj is host_obj
        )
        assert recompute_count == 1, "each host object recomputes exactly once"
        assert log.index("configure", host_obj) < log.index(
            "recompute", host_obj
        ), "custom writes must precede the object's recompute"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
