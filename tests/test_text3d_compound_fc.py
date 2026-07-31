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
        )
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
