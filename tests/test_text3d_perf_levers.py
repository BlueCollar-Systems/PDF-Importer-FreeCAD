"""Locks for the dense-page 3D Text performance levers (week-plan PH1-FC-2).

P1: a span tessellates exactly once — the ShapeString host is constructed
without the Draft factory's default-Size recompute, and every custom property
write lands BEFORE the object's only recompute so the page-end document
recompute has nothing left to re-execute (post-recompute writes re-touch
objects and cause another tessellation pass).
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
    monkeypatch.setattr(core, "_text3d_source_em_scale", lambda *_args: 1.0)
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


def _entity_kwargs(group, **overrides):
    kwargs = dict(
        font_size_fc=2.5,
        depth=0.3,
        target_advance_fc=6.0,
        baseline_angle_deg=0.0,
        text_group=group,
    )
    kwargs.update(overrides)
    return kwargs


def _fake_clone_env(monkeypatch, doc, log, wires_only=False):
    monkeypatch.setattr(core, "_text3d_source_em_scale", lambda *_args: 1.0)
    class FakeClone(FakeHost):
        @property
        def Scale(self):
            return self._scale

        @Scale.setter
        def Scale(self, value):
            self._scale = value
            self.Shape = FakeShape(width=12.0 * float(value.x))
            if wires_only:
                self.Shape.Faces = []
                self.Shape.Wires = [object()]

    def clone(source):
        host = FakeClone(doc, "Clone", "Part::Part2DObjectPython")
        doc.Objects.append(host)
        log.events.append(("add", host))
        return host

    monkeypatch.setattr(core, "Draft", types.SimpleNamespace(clone=clone))
    monkeypatch.setattr(
        core, "Vector", lambda x, y, z: types.SimpleNamespace(x=x, y=y, z=z)
    )


def test_parametric_3d_support_uses_source_em_vertical_scale(monkeypatch):
    log = EventLog()
    doc = FakeDoc(log)
    group = FakeGroup(doc)
    shape_string = FakeHost(doc, "ShapeString", "Part::Part2DObjectPython")
    shape_string.String = "AB"
    shape_string.FontFile = "source.ttf"
    doc.Objects.append(shape_string)
    _fake_clone_env(monkeypatch, doc, log)
    calls = []

    def measured_scale(text, font, shape, native_size):
        assert shape is shape_string.Shape
        assert native_size == pytest.approx(2.5)
        calls.append((text, font))
        return 0.7

    monkeypatch.setattr(core, "_text3d_source_em_scale", measured_scale)
    _extrusion, calibrated, x_scale, advance = core._create_verified_text3d_entity(
        shape_string, **_entity_kwargs(group)
    )
    assert calls == [("AB", "source.ttf")]
    assert calibrated.Scale.y == pytest.approx(0.7)
    assert calibrated.Scale.x == pytest.approx(x_scale)
    assert advance == pytest.approx(6)
    assert shape_string.Size == pytest.approx(2.5)


def test_shapestring_baseline_is_wires_only(monkeypatch):
    """P2: MakeFace must be False when the ShapeString tessellates."""
    log = EventLog()
    doc = FakeDoc(log)
    group = FakeGroup(doc)
    _fake_clone_env(monkeypatch, doc, log)

    shape_string = FakeHost(doc, "ShapeString", "Part::Part2DObjectPython")
    doc.Objects.append(shape_string)
    make_face_at_recompute = []
    original_recompute = doc.recompute

    def recording_recompute(objs=None):
        for obj in objs or []:
            if obj is shape_string:
                make_face_at_recompute.append(getattr(obj, "MakeFace", None))
        return original_recompute(objs)

    monkeypatch.setattr(doc, "recompute", recording_recompute)

    core._create_verified_text3d_entity(shape_string, **_entity_kwargs(group))

    assert make_face_at_recompute == [False], (
        "ShapeString must tessellate wires-only; faces are built once by "
        "Part::Extrusion(Solid=True)"
    )


def test_wires_only_supports_pass_verification(monkeypatch):
    """P2: face-less (wire) support geometry is valid for extrusion."""
    log = EventLog()
    doc = FakeDoc(log)
    group = FakeGroup(doc)
    _fake_clone_env(monkeypatch, doc, log, wires_only=True)

    shape_string = FakeHost(doc, "ShapeString", "Part::Part2DObjectPython")
    shape_string.Shape.Faces = []
    shape_string.Shape.Wires = [object()]
    doc.Objects.append(shape_string)

    extrusion, calibrated, x_scale, advance = core._create_verified_text3d_entity(
        shape_string, **_entity_kwargs(group)
    )
    assert extrusion.Solid is True
    assert x_scale == pytest.approx(0.5)
    assert advance == pytest.approx(6.0)


def test_empty_support_geometry_still_fails_closed(monkeypatch):
    """No faces and no wires yields the typed same-mode zero-geometry proof."""
    log = EventLog()
    doc = FakeDoc(log)
    group = FakeGroup(doc)
    _fake_clone_env(monkeypatch, doc, log)

    shape_string = FakeHost(doc, "ShapeString", "Part::Part2DObjectPython")
    shape_string.Shape.Faces = []
    shape_string.Shape.Wires = []
    doc.Objects.append(shape_string)

    with pytest.raises(core.Text3DExactFontOutlinesUnavailable) as raised:
        core._create_verified_text3d_entity(
            shape_string, **_entity_kwargs(group)
        )
    assert raised.value.evidence == {
        "implementation": "draft_shapestring",
        "outcome": "zero_geometry",
        "recompute_completed": True,
        "shape_present": True,
        "shape_is_null": False,
        "face_count": 0,
        "wire_count": 0,
    }


class FakeWire:
    def __init__(self, tag):
        self.tag = tag
        self.translated = False

    def copy(self):
        return FakeWire(self.tag)


class FakePartModule:
    def __init__(self):
        self.calls = []

        def makeWireString(string, font_file, size, tracking):
            self.calls.append((string, font_file, size, tracking))
            return [[FakeWire("%s:%d" % (string, index))] for index in range(2)]

        self.makeWireString = makeWireString


def test_wirestring_memo_dedupes_identical_tessellations():
    """P3: identical (text, font, size, tracking) calls tessellate once."""
    part = FakePartModule()
    memo = core._WireStringMemo(part)
    memo.install()
    try:
        first = part.makeWireString("1/4", "font.otf", 2.5, 0)
        second = part.makeWireString("1/4", "font.otf", 2.5, 0)
        third = part.makeWireString("M", "font.otf", 2.5, 0)
        part.makeWireString("M", "font.otf", 2.5, 0)
    finally:
        memo.restore()

    assert len(part.calls) == 2, "only unique keys reach FreeType"
    assert memo.hits == 2 and memo.misses == 2
    assert first is not second
    assert all(
        hit_wire is not miss_wire
        for hit_char, miss_char in zip(second, first, strict=True)
        for hit_wire, miss_wire in zip(hit_char, miss_char, strict=True)
    ), "cache hits must return fresh copies, never shared wire objects"
    assert third[0][0].tag.startswith("M:")


def test_wirestring_memo_is_immune_to_caller_mutation():
    """ShapeString.execute translates returned wires; the cache stays pristine."""
    part = FakePartModule()
    memo = core._WireStringMemo(part)
    memo.install()
    try:
        first = part.makeWireString("AB", "font.otf", 3.0, 0)
        first[0][0].translated = True  # caller mutates its result in place
        second = part.makeWireString("AB", "font.otf", 3.0, 0)
    finally:
        memo.restore()
    assert second[0][0].translated is False


def test_wirestring_memo_bypasses_unexpected_signatures():
    part = FakePartModule()
    original = part.makeWireString
    memo = core._WireStringMemo(part)
    memo.install()
    try:
        with pytest.raises(TypeError):
            part.makeWireString("AB", "font.otf", 3.0)  # 3-arg: passthrough
    finally:
        memo.restore()
    assert part.makeWireString is original
    assert memo.hits == 0 and memo.misses == 0


def test_wirestring_memo_scope_installs_and_restores(monkeypatch):
    part = FakePartModule()
    original = part.makeWireString
    monkeypatch.setattr(core, "Part", part)
    opts = types.SimpleNamespace()
    with core._wirestring_memo_scope(opts) as memo:
        assert isinstance(part.makeWireString, core._WireStringMemo)
        part.makeWireString("AB", "font.otf", 3.0, 0)
        part.makeWireString("AB", "font.otf", 3.0, 0)
        assert memo is not None
    assert part.makeWireString is original
    assert opts.wirestring_cache_stats == {"hits": 1, "misses": 1}


def test_wirestring_memo_scope_without_part_is_inert(monkeypatch):
    monkeypatch.setattr(core, "Part", None)
    with core._wirestring_memo_scope(None) as memo:
        assert memo is None



# --- P5: one linear transform + proof per glyph key, translation per char ---
#
# The positioned 3D Text builder splits each character's PDF matrix into its
# linear part (font scale, rotation, shear) and its origin. The glyph solid
# under one linear part is transformed and proven once per import; every
# character then gets an independent translated copy. These fakes count the
# expensive OCC calls.


class _GlyphLog:
    def __init__(self):
        self.transform_geometry = []
        self.translations = []
        self.bakes = []


def _matrix():
    return types.SimpleNamespace(A11=1.0, A12=0.0, A14=0.0, A21=0.0, A22=1.0,
                                 A24=0.0, A34=0.0)


class CountingGlyph:
    """Fake glyph solid: corner points, one solid, plain affine arithmetic."""

    def __init__(self, points, log, volume=1.0, solids=1):
        self.points = [tuple(float(v) for v in point) for point in points]
        self.log = log
        self.Volume = float(volume)
        self.solid_count = int(solids)

    def countElement(self, kind):
        assert kind == "Solid"
        return self.solid_count

    @property
    def Vertexes(self):
        return [types.SimpleNamespace(Point=types.SimpleNamespace(x=x, y=y, z=z))
                for x, y, z in self.points]

    @property
    def Solids(self):
        n = len(self.points)
        center = types.SimpleNamespace(**{axis: sum(p[i] for p in self.points) / n
                                          for i, axis in enumerate("xyz")})
        return [types.SimpleNamespace(Volume=self.Volume, CenterOfMass=center)]

    @property
    def BoundBox(self):
        lows = [min(p[i] for p in self.points) for i in range(3)]
        highs = [max(p[i] for p in self.points) for i in range(3)]
        return types.SimpleNamespace(XMin=lows[0], YMin=lows[1], ZMin=lows[2],
                                     XMax=highs[0], YMax=highs[1], ZMax=highs[2])

    def isNull(self):
        return False

    def _apply(self, m):
        return CountingGlyph([(m.A11 * x + m.A12 * y + m.A14,
                               m.A21 * x + m.A22 * y + m.A24, z + m.A34)
                              for x, y, z in self.points], self.log,
                             self.Volume * abs(m.A11 * m.A22 - m.A12 * m.A21),
                             self.solid_count)

    def transformGeometry(self, m):
        self.log.transform_geometry.append((m.A11, m.A21, m.A12, m.A22, m.A14, m.A24, m.A34))
        return self._apply(m)

    def transformed(self, m, copy=False):
        assert copy is True, "each character must get an independent copy"
        assert (m.A11, m.A21, m.A12, m.A22) == (1.0, 0.0, 0.0, 1.0)
        self.log.translations.append((m.A14, m.A24, m.A34))
        return self._apply(m)


@pytest.fixture
def glyph_host(monkeypatch):
    log = _GlyphLog()

    def bake(**kwargs):
        log.bakes.append((kwargs["source_text"], kwargs["font_size_fc"]))
        size = float(kwargs["font_size_fc"])
        return (CountingGlyph([(0.0, 0.0, 0.0), (size * 0.6, size, kwargs["depth"])], log),
                1.0, 1.0, 1.0)

    def compound(shapes):
        result = CountingGlyph([p for shape in shapes for p in shape.points], log,
                               solids=sum(shape.solid_count for shape in shapes))
        result.Volume = sum(shape.Volume for shape in shapes)
        return result

    monkeypatch.setattr(core, "_build_exact_text3d_compound_shape", bake)
    monkeypatch.setattr(core, "_source_em_text3d_pen_advance", lambda *_a: 2.0)
    monkeypatch.setattr(core, "FreeCAD", types.SimpleNamespace(
        Matrix=_matrix, Vector=lambda x, y, z: types.SimpleNamespace(x=x, y=y, z=z)))
    monkeypatch.setattr(core, "Part", types.SimpleNamespace(Compound=compound))
    monkeypatch.setattr(core, "_ACTIVE_TEXT3D_OUTLINE_MEMO", None)
    return log


def _char(text, x, y, baseline_scale=1.0, up_scale=1.0, baseline=(1.0, 0.0), up=(0.0, 1.0)):
    return dict(text=text, local_origin=[x, y, 0.0], advance=2.0,
                baseline_axis=list(baseline), up_axis=list(up),
                baseline_scale=baseline_scale, up_scale=up_scale)


def _build_span(chars, size=3.5):
    return core._build_positioned_text3d_compound_shape(
        source_text="".join(row["text"] for row in chars), font_path="exact.ttf",
        font_size_fc=size, depth=0.42, target_advance_fc=10.0,
        source_character_layout={"characters": chars})


def test_same_linear_transform_runs_transform_geometry_once(glyph_host):
    nonuniform = dict(baseline_scale=0.9985797026172729, up_scale=1.0014223174965449)
    _compound, _stretch, _na, _va, volume, solids = _build_span(
        [_char("A", 0.0, 0.0, **nonuniform), _char("A", 2.5, 0.0, **nonuniform),
         _char("A", 5.0, -1.0, **nonuniform)])
    assert len(glyph_host.transform_geometry) == 1
    assert glyph_host.transform_geometry[0][4:] == (0.0, 0.0, 0.0), (
        "the shared linear shape must not carry any character's origin")
    assert len(glyph_host.bakes) == 1
    assert len(glyph_host.translations) == 3
    assert solids == 3
    assert volume == pytest.approx(3 * 0.9985797026172729 * 1.0014223174965449)


def test_identity_linear_characters_share_one_geometry_conversion(glyph_host):
    # FreeCAD's transformGeometry also turns the glyph faces into BSpline
    # surfaces, which is what the importer has always delivered (and what
    # the page-edge clip proof accepts). Identity characters therefore still
    # get that conversion, but only once per glyph key, never per character.
    _build_span([_char("E", 0.0, 0.0), _char("E", 3.0, 0.0), _char("E", 6.0, 0.0),
                 _char("E", 9.0, 0.0)])
    assert glyph_host.transform_geometry == [(1.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)]
    assert len(glyph_host.translations) == 4


def test_each_character_translation_equals_its_local_origin(glyph_host):
    chars = [_char("B", 5.0, -2.0, baseline_scale=2.0),
             _char("B", 7.25, 1.5, baseline_scale=2.0),
             _char("C", -3.0, 4.0, baseline=(0.0, 1.0), up=(-0.8, 0.6))]
    compound = _build_span(chars)[0]
    assert glyph_host.translations == [(5.0, -2.0, 0.0), (7.25, 1.5, 0.0), (-3.0, 4.0, 0.0)]
    # Same result as one full-matrix transform per character.
    assert compound.points[0] == (5.0, -2.0, 0.0)
    assert compound.points[3] == pytest.approx((7.25 + 2.0 * 3.5 * 0.6, 1.5 + 3.5, 0.42))
    assert compound.points[5] == pytest.approx((-3.0 - 0.8 * 3.5, 4.0 + 2.1 + 0.6 * 3.5, 0.42))


def test_font_size_change_is_a_separate_key_and_memo_spans_items(glyph_host, monkeypatch):
    memo = core._Text3DOutlineMemo()
    monkeypatch.setattr(core, "_ACTIVE_TEXT3D_OUTLINE_MEMO", memo)
    _build_span([_char("D", 0.0, 0.0)], size=3.5)
    _build_span([_char("D", 0.0, 0.0)], size=3.6)
    assert len(glyph_host.transform_geometry) == 2
    assert memo.linear_misses == 2 and memo.linear_hits == 0
    # A later item with the same character, font, size and matrix reuses it.
    _build_span([_char("D", 4.0, 1.0)], size=3.5)
    assert len(glyph_host.transform_geometry) == 2
    assert memo.linear_hits == 1
    assert memo.snapshot_stats()["linear_hits"] == 1
    memo.clear()
    assert memo._linear_cache == {}


def test_lost_character_translation_is_rejected(glyph_host, monkeypatch):
    def ignore_translation(self, _m, copy=False):
        return CountingGlyph(self.points, self.log, self.Volume, self.solid_count)

    monkeypatch.setattr(CountingGlyph, "transformed", ignore_translation)
    with pytest.raises(RuntimeError, match="affine coordinates"):
        _build_span([_char("F", 0.0, 0.0), _char("F", 3.0, 0.5)])


def test_host_check_skips_volume_reintegration_only_for_the_same_geometry():
    class Host:
        volume_reads = 0

        def __init__(self, partner):
            self.partner = partner

        def isPartner(self, other):
            return other is self.partner

        @property
        def Volume(self):
            Host.volume_reads += 1
            return 5.0

    verified = object()
    assert core._shape_keeps_verified_volume(Host(verified), verified, 5.0)
    assert Host.volume_reads == 0
    assert core._shape_keeps_verified_volume(Host(object()), verified, 5.0)
    assert Host.volume_reads == 1
    assert not core._shape_keeps_verified_volume(Host(object()), verified, 7.0)


def test_3d_text_progress_ticks_on_time_with_a_plain_label(monkeypatch):
    import time as real_time

    class Clock:
        now = 0.0

        def monotonic(self):
            return Clock.now

        def __getattr__(self, name):
            return getattr(real_time, name)

    items = [{"source_item_id": "p1:b0:l0:s%d" % index, "page_number": 1,
              "pdf_sha256": "a" * 64, "bbox": (0.0, 0.0, 1.0, 1.0), "text": "x"}
             for index in range(4)]
    events = []
    monkeypatch.setattr(core, "time", Clock())
    monkeypatch.setattr(core, "_iter_text_source_items", lambda *_args: iter(items))
    monkeypatch.setattr(core, "_cache_canonical_text_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(core, "_prepare_native_text_object_index", lambda *_a, **_k: None)

    def deliver(item, *_args):
        Clock.now += 0.3  # each solid-letter item takes a noticeable moment
        return {"source_item_id": item["source_item_id"], "final_type": "3d_text",
                "created_entity_ids": [item["source_item_id"]],
                "delivery_entity_ids": [item["source_item_id"]], "delivery_count": 1}

    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", deliver)
    opts = core.ImportOptions(text_mode="3d_text", progress_callback=events.append)
    opts._active_page_index = 1
    opts._active_page_total = 1
    opts._active_page_profile = {"drawing_operations": 0, "text_characters": 4,
                                 "image_instances": 0, "total_units": 4}
    core._render_canonical_text_items(
        pdf_doc=object(), page=types.SimpleNamespace(get_text=lambda _kind: {"blocks": []}),
        pdf_path="fixture.pdf", page_num=1, page_h=100.0, page_w=80.0, scale=1.0,
        fc_doc=object(), parent_group=object(), opts=opts, pdf_sha256="a" * 64,
        raw_tdict={"blocks": []})
    assert [event["label"] for event in events] == [
        "Building 3D text 0/4", "Building 3D text 1/4", "Building 3D text 2/4",
        "Building 3D text 3/4", "Building 3D text 4/4"]


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
