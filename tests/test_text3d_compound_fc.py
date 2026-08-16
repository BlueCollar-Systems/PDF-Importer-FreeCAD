from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_font_kern_probe_cache():
    core._clear_font_kern_probe_cache()
    yield
    core._clear_font_kern_probe_cache()


class FakeShape:
    def __init__(self, *, solid_count=2, volume=12.5, width=30.0):
        self.Solids = [object() for _ in range(solid_count)]
        self.Volume = float(volume)
        self.Vertexes = [
            SimpleNamespace(Point=SimpleNamespace(x=0.0, y=0.0, z=0.0)),
            SimpleNamespace(Point=SimpleNamespace(x=float(width), y=0.0, z=1.0)),
        ]

    def isNull(self):
        return False

    def copy(self):
        return FakeShape(
            solid_count=len(self.Solids),
            volume=self.Volume,
            width=self.Vertexes[-1].Point.x,
        )


class FakeHost:
    def __init__(self, document, name, type_id):
        self.Document = document
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.PropertiesList = []
        self.Shape = None
        self.Placement = None
        self.ViewObject = SimpleNamespace()

    def addProperty(self, property_kind, name, group):
        self.PropertiesList.append(name)


class FakeDocument:
    def __init__(self):
        self.Objects = []
        self.removed = []
        self.recompute_calls = 0

    def addObject(self, kind, name):
        host = FakeHost(self, "%s_%d" % (name, len(self.Objects)), kind)
        self.Objects.append(host)
        return host

    def removeObject(self, name):
        self.removed.append(name)
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self, *args):
        self.recompute_calls += 1


class FakeGroup:
    def __init__(self, document):
        self.Document = document
        self.objects = []

    def addObject(self, obj):
        self.objects.append(obj)


class BoundedGeometry:
    def __init__(
        self,
        x_min,
        x_max,
        *,
        face_count=0,
        solid_count=0,
        volume=0.0,
    ):
        self.BoundBox = SimpleNamespace(
            XMin=float(x_min),
            XMax=float(x_max),
            XLength=float(x_max) - float(x_min),
        )
        self.Faces = [object() for _ in range(face_count)]
        self.Solids = [object() for _ in range(solid_count)]
        self.Volume = float(volume)

    def isNull(self):
        return False

    def copy(self):
        return BoundedGeometry(
            self.BoundBox.XMin,
            self.BoundBox.XMax,
            face_count=len(self.Faces),
            solid_count=len(self.Solids),
            volume=self.Volume,
        )

    def transformGeometry(self, matrix):
        scale_x = float(matrix.A11)
        scale_y = float(getattr(matrix, "A22", 1.0) or 1.0)
        scale_z = float(getattr(matrix, "A33", 1.0) or 1.0)
        return BoundedGeometry(
            self.BoundBox.XMin * scale_x,
            self.BoundBox.XMax * scale_x,
            face_count=len(self.Faces),
            solid_count=len(self.Solids),
            volume=float(self.Volume) * abs(scale_x * scale_y * scale_z),
        )

    def extrude(self, _vector):
        return BoundedGeometry(
            self.BoundBox.XMin,
            self.BoundBox.XMax,
            face_count=len(self.Faces),
            solid_count=1,
            volume=12.5,
        )


class BoundedPart:
    @staticmethod
    def Compound(shapes):
        shapes = list(shapes)
        return BoundedGeometry(
            min(shape.BoundBox.XMin for shape in shapes),
            max(shape.BoundBox.XMax for shape in shapes),
            face_count=sum(len(getattr(shape, "Faces", [])) for shape in shapes),
            solid_count=sum(len(getattr(shape, "Solids", [])) for shape in shapes),
            volume=sum(float(getattr(shape, "Volume", 0.0)) for shape in shapes),
        )


def test_outer_spaces_use_zero_kern_probe_for_full_pen_advance(monkeypatch):
    import fontTools.ttLib as ttlib

    fixtures = {
        " A ": [[], [BoundedGeometry(3.0, 5.0)], []],
        " A A": [
            [],
            [BoundedGeometry(3.0, 5.0)],
            [],
            [BoundedGeometry(9.0, 11.0)],
        ],
        " A M": [
            [],
            [BoundedGeometry(3.0, 5.0)],
            [],
            [BoundedGeometry(11.0, 13.0)],
        ],
        "A": [[BoundedGeometry(0.5, 2.5)]],
        "M": [[BoundedGeometry(2.0, 4.0)]],
    }

    class FakePart(BoundedPart):
        @staticmethod
        def makeWireString(source_text, font_path, size, tracking):
            assert font_path == "C:/fonts/source.ttf"
            assert (size, tracking) == (1.0, 0)
            return fixtures[source_text]

    class FakeFont:
        def getBestCmap(self):
            return {32: "space", 65: "A", 77: "M"}

        def __contains__(self, table_name):
            return table_name in {"cmap", "kern"}

        def __getitem__(self, table_name):
            assert table_name == "kern"
            return SimpleNamespace(
                kernTables=[
                    SimpleNamespace(
                        kernTable={
                            ("space", "A"): -50,
                            ("space", "M"): 0,
                        }
                    )
                ]
            )

        def close(self):
            return None

    monkeypatch.setattr(core, "Part", FakePart)
    monkeypatch.setattr(ttlib, "TTFont", lambda *_args, **_kwargs: FakeFont())
    monkeypatch.setattr(
        core,
        "_text3d_faces_for_outlines",
        lambda outlines: [
            BoundedGeometry(
                min(outline.BoundBox.XMin for outline in outlines),
                max(outline.BoundBox.XMax for outline in outlines),
                face_count=1,
            )
        ],
    )

    face_template, native_advance, visible_count = (
        core._build_exact_text3d_outline_template(
            " A ",
            "C:/fonts/source.ttf",
        )
    )

    assert visible_count == 1
    assert face_template.BoundBox.XMin == pytest.approx(3.0)
    assert face_template.BoundBox.XMax == pytest.approx(5.0)
    assert native_advance == pytest.approx(9.0)


def test_compound_scales_pen_advance_without_stretching_inset_ink(monkeypatch):
    class FakeMatrix:
        def __init__(self):
            self.A11 = 1.0
            self.A22 = 1.0
            self.A33 = 1.0

    monkeypatch.setattr(core, "Part", BoundedPart)
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(Matrix=FakeMatrix))
    monkeypatch.setattr(core, "Vector", lambda x, y, z: (x, y, z))
    monkeypatch.setattr(
        core,
        "_build_exact_text3d_outline_template",
        lambda *_args: (BoundedGeometry(2.0, 6.0, face_count=1), 10.0, 1),
    )

    compound, horizontal_scale, native_advance, verified_advance = (
        core._build_exact_text3d_compound_shape(
            source_text=" A ",
            font_path="C:/fonts/source.ttf",
            font_size_fc=3.0,
            depth=0.3,
            target_advance_fc=15.0,
        )
    )

    assert horizontal_scale == pytest.approx(0.5)
    assert native_advance == pytest.approx(30.0)
    assert compound.BoundBox.XMin == pytest.approx(3.0)
    assert compound.BoundBox.XMax == pytest.approx(9.0)
    assert verified_advance == pytest.approx(15.0)


def test_compound_reuses_baked_solid_only_for_identical_dimensions(monkeypatch):
    extrude_calls = []
    original_extrude = BoundedGeometry.extrude

    def counting_extrude(self, vector):
        extrude_calls.append(tuple(vector))
        return original_extrude(self, vector)

    class FakeMatrix:
        def __init__(self):
            self.A11 = 1.0
            self.A22 = 1.0
            self.A33 = 1.0

    monkeypatch.setattr(BoundedGeometry, "extrude", counting_extrude)
    monkeypatch.setattr(core, "Part", BoundedPart)
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(Matrix=FakeMatrix))
    monkeypatch.setattr(core, "Vector", lambda x, y, z: (x, y, z))
    monkeypatch.setattr(
        core,
        "_build_exact_text3d_outline_template",
        lambda *_args: (BoundedGeometry(2.0, 6.0, face_count=1), 10.0, 1),
    )

    memo = core._Text3DOutlineMemo()
    prior = core._ACTIVE_TEXT3D_OUTLINE_MEMO
    core._ACTIVE_TEXT3D_OUTLINE_MEMO = memo
    try:
        first = core._build_exact_text3d_compound_shape(
            source_text="A36",
            font_path="C:/fonts/source.ttf",
            font_size_fc=3.0,
            depth=0.3,
            target_advance_fc=15.0,
        )
        second = core._build_exact_text3d_compound_shape(
            source_text="A36",
            font_path="C:/fonts/source.ttf",
            font_size_fc=3.0,
            depth=0.3,
            target_advance_fc=15.0,
        )
        third = core._build_exact_text3d_compound_shape(
            source_text="A36",
            font_path="C:/fonts/source.ttf",
            font_size_fc=6.0,
            depth=0.6,
            target_advance_fc=30.0,
        )
    finally:
        core._ACTIVE_TEXT3D_OUTLINE_MEMO = prior

    assert extrude_calls == [(0.0, 0.0, 0.3), (0.0, 0.0, 0.6)]
    assert memo.solid_hits == 1
    assert memo.solid_misses == 2
    assert first[0] is not second[0]
    assert first[0].BoundBox.XMin == pytest.approx(3.0)
    assert second[0].BoundBox.XMin == pytest.approx(3.0)
    assert third[0].BoundBox.XMin == pytest.approx(6.0)


def test_compound_3d_text_is_one_verified_host_object_without_recompute(monkeypatch):
    document = FakeDocument()
    group = FakeGroup(document)
    placement = object()
    shape = FakeShape(width=30.0)
    monkeypatch.setattr(
        core,
        "_build_exact_text3d_compound_shape",
        lambda **kwargs: (shape, 0.5, 60.0, 30.0),
    )
    configured = []

    entity, horizontal_scale, native_advance, verified_advance = (
        core._create_verified_compound_text3d_entity(
            document,
            source_text="W12x30",
            font_path="C:/fonts/source.ttf",
            font_size_fc=2.5,
            depth=0.3,
            target_advance_fc=30.0,
            placement=placement,
            text_group=group,
            configure_host=lambda obj: configured.append(obj),
        )[:4]
    )

    assert document.Objects == [entity]
    assert entity.TypeId == "Part::Feature"
    assert entity.Shape is shape
    assert entity.Placement is placement
    assert entity.Shape.Solids
    assert entity.Shape.Volume > 0.0
    assert configured == [entity]
    assert group.objects == [entity]
    assert document.recompute_calls == 0
    assert horizontal_scale == pytest.approx(0.5)
    assert native_advance == pytest.approx(60.0)
    assert verified_advance == pytest.approx(30.0)


def test_compound_3d_text_failure_removes_half_built_host(monkeypatch):
    document = FakeDocument()
    group = FakeGroup(document)
    monkeypatch.setattr(
        core,
        "_build_exact_text3d_compound_shape",
        lambda **kwargs: (FakeShape(solid_count=0, volume=0.0), 1.0, 30.0, 30.0),
    )

    with pytest.raises(RuntimeError, match="verified solid"):
        core._create_verified_compound_text3d_entity(
            document,
            source_text="TEXT",
            font_path="C:/fonts/source.ttf",
            font_size_fc=2.5,
            depth=0.3,
            target_advance_fc=30.0,
            placement=object(),
            text_group=group,
        )

    assert document.Objects == []
    assert document.removed == ["PDF_3D_Text_0"]
    assert group.objects == []


def test_compound_3d_text_metadata_preserves_editable_source_provenance():
    document = FakeDocument()
    host = document.addObject("Part::Feature", "PDF_3D_Text")

    core._persist_text3d_source_metadata(
        host,
        source_text=" W12x30 ",
        font_path="C:/fonts/source.ttf",
        font_sha256="b" * 64,
        depth=0.3,
        target_advance_fc=30.0,
        horizontal_scale=0.5,
    )

    assert host.PDFSourceText == " W12x30 "
    assert host.PDFSourceTextSHA256 == core.hashlib.sha256(
        b" W12x30 "
    ).hexdigest()
    assert host.PDFFontFile == "C:/fonts/source.ttf"
    assert host.PDFFontFileSHA256 == "b" * 64
    assert host.PDFExtrusionDepth == pytest.approx(0.3)
    assert host.PDFTargetAdvance == pytest.approx(30.0)
    assert host.PDFHorizontalScale == pytest.approx(0.5)
    assert host.PDFGeometryEncoding == "exact_glyph_solid_compound_v1"


def test_text3d_outline_memo_returns_fresh_shapes_and_tracks_hits():
    memo = core._Text3DOutlineMemo()
    calls = []

    def build():
        calls.append("build")
        return FakeShape(width=4.0), 4.0, 3

    first = memo.get_or_build(("TEXT", "font.ttf"), build)
    second = memo.get_or_build(("TEXT", "font.ttf"), build)
    third = memo.get_or_build(("OTHER", "font.ttf"), build)

    assert calls == ["build", "build"]
    assert memo.hits == 1
    assert memo.misses == 2
    assert first[0] is not second[0]
    assert first[1:] == second[1:] == (4.0, 3)
    assert third[1:] == (4.0, 3)


def test_text3d_outline_memo_holds_a_dense_page_of_unique_strings():
    memo = core._Text3DOutlineMemo()
    unique_strings = 800

    for index in range(unique_strings):
        memo.get_or_build(
            ("SPAN-%d" % index, "C:/fonts/source.ttf"),
            lambda: (FakeShape(width=4.0), 4.0, 1),
        )

    assert memo.evictions == 0
    assert memo.misses == unique_strings
    assert memo.hits == 0


def test_complete_nonspace_item_with_zero_exact_font_outlines_is_typed_and_private(
    monkeypatch,
):
    class ZeroOutlinePart:
        @staticmethod
        def makeWireString(source_text, font_path, size, tracking):
            assert (source_text, font_path, size, tracking) == (
                "AB",
                "C:/fonts/source.ttf",
                1.0,
                0,
            )
            return [[], []]

    monkeypatch.setattr(core, "Part", ZeroOutlinePart)

    with pytest.raises(RuntimeError) as raised:
        core._build_exact_text3d_outline_template("AB", "C:/fonts/source.ttf")

    assert raised.value.__class__.__name__ == (
        "Text3DExactFontOutlinesUnavailable"
    )
    assert raised.value.evidence == {
        "implementation": "part_make_wire_string",
        "outcome": "zero_geometry",
        "source_text_sha256": core.hashlib.sha256(b"AB").hexdigest(),
        "source_text_length": 2,
        "non_whitespace_character_count": 2,
        "glyph_inventory_length": 2,
        "glyph_outline_count": 0,
        "wire_string_completed": True,
    }
    assert "AB" not in str(raised.value)
    assert "AB" not in repr(raised.value.evidence)


def test_partial_exact_font_outline_loss_remains_an_unproven_runtime_failure(
    monkeypatch,
):
    class PartialOutlinePart:
        @staticmethod
        def makeWireString(*_args):
            return [[object()], []]

    monkeypatch.setattr(core, "Part", PartialOutlinePart)

    with pytest.raises(RuntimeError, match="source glyph produced no outline geometry") as raised:
        core._build_exact_text3d_outline_template("AB", "C:/fonts/source.ttf")

    assert raised.value.__class__ is RuntimeError


@pytest.mark.parametrize(
    "wire_result,error_message",
    [
        ([[]], "source glyph inventory does not match source text"),
        (RuntimeError("synthetic font API failure"), "synthetic font API failure"),
    ],
)
def test_malformed_or_failed_exact_font_api_is_never_closed_impossibility(
    monkeypatch,
    wire_result,
    error_message,
):
    class BrokenPart:
        @staticmethod
        def makeWireString(*_args):
            if isinstance(wire_result, Exception):
                raise wire_result
            return wire_result

    monkeypatch.setattr(core, "Part", BrokenPart)

    with pytest.raises(RuntimeError, match=error_message) as raised:
        core._build_exact_text3d_outline_template("AB", "C:/fonts/source.ttf")

    assert raised.value.__class__ is RuntimeError


class ClosedWire:
    def __init__(self, name="wire"):
        self.name = name
        self.Edges = [object()]
        self.Wires = [self]
        self.ShapeType = "Wire"

    def isClosed(self):
        return True


class OpenWire:
    def __init__(self):
        self.Edges = [object()]
        self.Wires = []
        self.ShapeType = "Compound"

    def isClosed(self):
        return False


def test_closed_text3d_wires_keep_already_closed_wires_without_reconnect(monkeypatch):
    reconnect_calls = []

    class TrackingPart:
        class Compound:
            def __init__(self, edges):
                reconnect_calls.append(list(edges))

            def connectEdgesToWires(self):
                raise AssertionError("closed wires must not be reconnected")

    monkeypatch.setattr(core, "Part", TrackingPart)
    closed = ClosedWire()

    result = core._closed_text3d_wires([closed])

    assert result == [closed]
    assert reconnect_calls == []


def test_open_text3d_wires_still_reconnect(monkeypatch):
    class Reconnected:
        def __init__(self):
            self.Wires = [ClosedWire("reconnected")]

    class TrackingPart:
        class Compound:
            def __init__(self, edges):
                self.edges = list(edges)

            def connectEdgesToWires(self):
                return Reconnected()

    monkeypatch.setattr(core, "Part", TrackingPart)

    result = core._closed_text3d_wires([OpenWire()])

    assert len(result) == 1
    assert result[0].name == "reconnected"


def test_bake_does_not_wrap_an_already_solid_extrusion_in_compound(monkeypatch):
    compound_calls = []

    class TrackingPart(BoundedPart):
        @staticmethod
        def Compound(shapes):
            compound_calls.append(list(shapes))
            return BoundedPart.Compound(shapes)

    class FakeMatrix:
        def __init__(self):
            self.A11 = 1.0
            self.A22 = 1.0
            self.A33 = 1.0

    monkeypatch.setattr(core, "Part", TrackingPart)
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(Matrix=FakeMatrix))
    monkeypatch.setattr(core, "Vector", lambda x, y, z: (x, y, z))
    monkeypatch.setattr(
        core,
        "_build_exact_text3d_outline_template",
        lambda *_args: (BoundedGeometry(2.0, 6.0, face_count=1), 10.0, 1),
    )

    compound, _horizontal, _native, verified = core._build_exact_text3d_compound_shape(
        source_text="A",
        font_path="C:/fonts/source.ttf",
        font_size_fc=3.0,
        depth=0.3,
        target_advance_fc=15.0,
    )

    assert compound_calls == []
    assert verified == pytest.approx(15.0)
    assert compound.Volume > 0.0


def test_kern_probe_loads_each_font_file_once(monkeypatch):
    font_opens = []

    class FakeFont:
        def getBestCmap(self):
            return {65: "A", 77: "M"}

        def __contains__(self, table_name):
            return table_name in {"cmap", "kern"}

        def __getitem__(self, table_name):
            return SimpleNamespace(kernTables=[SimpleNamespace(kernTable={})])

        def close(self):
            return None

    def fake_ttfont(path, **_kwargs):
        font_opens.append(path)
        return FakeFont()

    import fontTools.ttLib as ttlib

    monkeypatch.setattr(ttlib, "TTFont", fake_ttfont)
    core._clear_font_kern_probe_cache()
    try:
        first = core._text3d_zero_kern_probe_candidates("A", "C:/fonts/source.ttf")
        second = core._text3d_zero_kern_probe_candidates("M", "C:/fonts/source.ttf")
        third = core._text3d_zero_kern_probe_candidates("A", "C:/fonts/other.ttf")
    finally:
        core._clear_font_kern_probe_cache()

    assert first == ["A", "M"]
    assert second == ["M"]
    assert third == ["A", "M"]
    assert font_opens == ["C:/fonts/source.ttf", "C:/fonts/other.ttf"]


def test_compound_host_skips_shape_volume_after_bake(monkeypatch):
    document = FakeDocument()
    group = FakeGroup(document)

    class VolumeTrap:
        def __init__(self):
            self.Solids = [object(), object()]

        def isNull(self):
            return False

        @property
        def Volume(self):
            raise AssertionError("host Shape.Volume must not be read after bake")

    baked = VolumeTrap()
    baked_volume = 12.5
    monkeypatch.setattr(
        core,
        "_build_exact_text3d_compound_shape",
        lambda **kwargs: (baked, 0.5, 60.0, 30.0, baked_volume),
    )

    entity, _hs, _na, _va, volume = core._create_verified_compound_text3d_entity(
        document,
        source_text="W12x30",
        font_path="C:/fonts/source.ttf",
        font_size_fc=2.5,
        depth=0.3,
        target_advance_fc=30.0,
        placement=object(),
        text_group=group,
    )

    assert entity.Shape is baked
    assert volume == pytest.approx(12.5)
