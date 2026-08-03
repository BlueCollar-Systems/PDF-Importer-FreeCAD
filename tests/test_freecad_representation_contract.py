from __future__ import annotations

import sys
import math
import copy
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


class FakeVector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)

    def __mul__(self, value):
        return FakeVector(self.x * value, self.y * value, self.z * value)

    __rmul__ = __mul__

    def __add__(self, other):
        return FakeVector(self.x + other.x, self.y + other.y, self.z + other.z)


class FakeRotation:
    def __init__(self, *args):
        self.args = args
        self.angle = float(args[1]) if len(args) > 1 else 0.0


class FakePlacement:
    def __init__(self, base=None, rotation=None):
        self.Base = base
        self.Rotation = rotation


class FakeBoundBox:
    def __init__(self, width=12.0, height=5.0, depth=1.0):
        self.XLength = float(width)
        self.YLength = float(height)
        self.ZLength = float(depth)


class FakeVertex:
    def __init__(self, x, y):
        self.Point = FakeVector(x, y, 0.0)


class FakeShape:
    def __init__(self, *, solid=False, width=12.0, height=5.0, angle=0.0):
        self.Faces = [object()]
        self.Wires = []
        self.Solids = [object()] if solid else []
        self.Volume = 10.0 if solid else 0.0
        radians = math.radians(float(angle))
        cos_a, sin_a = math.cos(radians), math.sin(radians)
        points = []
        for x, y in ((0.0, 0.0), (width, 0.0), (width, height), (0.0, height)):
            points.append((x * cos_a - y * sin_a, x * sin_a + y * cos_a))
        xs = [point[0] for point in points]
        ys = [point[1] for point in points]
        self.BoundBox = FakeBoundBox(max(xs) - min(xs), max(ys) - min(ys))
        self.Vertexes = [FakeVertex(x, y) for x, y in points]

    def isNull(self):
        return False


class FakeZeroGeometryShape:
    Faces = []
    Wires = []

    def isNull(self):
        return False


class AttrSink:
    Visibility = True


class FakeHostObject:
    def _set_host_name(self, name):
        self._name = name
        self._deleted = False

    @property
    def Name(self):
        if self._deleted:
            raise RuntimeError("Cannot access attribute 'Name' of deleted object")
        return self._name


class FakeShapeString(FakeHostObject):
    def __init__(self, document, name, text):
        self.Document = document
        self._set_host_name(name)
        self.Label = name
        self.TypeId = "Part::Part2DObjectPython"
        self.String = text
        self.Shape = FakeShape(solid=False)
        self.ViewObject = AttrSink()
        self._placement = None

    @property
    def Placement(self):
        return self._placement

    @Placement.setter
    def Placement(self, value):
        self._placement = value
        angle = getattr(getattr(value, "Rotation", None), "angle", 0.0)
        self.Shape = FakeShape(width=12.0, height=5.0, angle=angle)


class FakeShapeStringClone(FakeShapeString):
    def __init__(self, document, name, source):
        super().__init__(document, name, source.String)
        self.Objects = [source]
        self._scale = FakeVector(1.0, 1.0, 1.0)

    @property
    def Scale(self):
        return self._scale

    @Scale.setter
    def Scale(self, value):
        self._scale = value
        angle = getattr(getattr(self.Placement, "Rotation", None), "angle", 0.0)
        self.Shape = FakeShape(
            width=12.0 * value.x,
            height=5.0 * value.y,
            angle=angle,
        )


class FakeExtrusion(FakeHostObject):
    def __init__(self, document, name):
        self.Document = document
        self._set_host_name(name)
        self.Label = name
        self.TypeId = "Part::Extrusion"
        self.ViewObject = AttrSink()
        self.Shape = FakeShape(solid=True)
        self.Base = None
        self.Dir = None
        self.Solid = False


class FakeDocument:
    def __init__(self):
        self.Objects = []
        self.removed = []

    def addObject(self, kind, name):
        assert kind == "Part::Extrusion"
        obj = FakeExtrusion(self, f"{name}_{len(self.Objects)}")
        self.Objects.append(obj)
        return obj

    def removeObject(self, name):
        self.removed.append(name)
        survivors = []
        for obj in self.Objects:
            if obj.Name == name:
                if hasattr(obj, "_deleted"):
                    obj._deleted = True
            else:
                survivors.append(obj)
        self.Objects = survivors

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def recompute(self, *args):
        return None


class TransientObjectsDocument(FakeDocument):
    def __init__(self):
        self._objects = []
        self.removed = []
        self.object_reads = 0
        self.fail_object_reads = set()

    @property
    def Objects(self):
        self.object_reads += 1
        if self.object_reads in self.fail_object_reads:
            raise RuntimeError("synthetic transient Objects enumeration failure")
        return self._objects

    @Objects.setter
    def Objects(self, value):
        self._objects = value


class FakeGroup:
    def __init__(self, document):
        self.Document = document
        self.Name = "Text"
        self.objects = []

    def addObject(self, obj):
        if obj not in self.objects:
            self.objects.append(obj)


class FakeDraft:
    def __init__(self, document, fail_texts=()):
        self.document = document
        self.fail_texts = set(fail_texts)
        self.calls = []
        self.label_calls = []

    def make_shapestring(self, text, font_path):
        self.calls.append((text, font_path))
        if text in self.fail_texts:
            raise RuntimeError("synthetic ShapeString failure")
        obj = FakeShapeString(
            self.document,
            f"ShapeString_{len(self.document.Objects)}",
            text,
        )
        self.document.Objects.append(obj)
        return obj

    def make_text(self, texts, placement=None):
        self.label_calls.append((texts, placement))
        raise AssertionError("3D Text failure must not silently create Labels")

    def clone(self, source):
        obj = FakeShapeStringClone(
            self.document,
            f"CalibratedShapeString_{len(self.document.Objects)}",
            source,
        )
        obj.Placement = source.Placement
        self.document.Objects.append(obj)
        return obj


def _span(text, x=10.0):
    return {
        "text": text,
        "font": "ArialMT",
        "size": 10.0,
        "origin": (x, 50.0),
        "bbox": (x, 40.0, x + 30.0, 52.0),
        "ascender": 0.8,
        "descender": -0.2,
    }


def _tdict(*texts, direction=(1.0, 0.0)):
    return {
        "blocks": [{
            "type": 0,
            "lines": [{
                "dir": direction,
                "bbox": (10.0, 40.0, 200.0, 52.0),
                "spans": [_span(text, 10.0 + index * 50.0) for index, text in enumerate(texts)],
            }],
        }],
    }


def _install_host(monkeypatch, *, fail_texts=()):
    document = FakeDocument()
    draft, group = _install_document_host(
        monkeypatch, document, fail_texts=fail_texts
    )
    return document, draft, group


def _install_document_host(monkeypatch, document, *, fail_texts=()):
    draft = FakeDraft(document, fail_texts=fail_texts)
    monkeypatch.setattr(core, "Draft", draft)
    monkeypatch.setattr(core, "Vector", FakeVector)
    monkeypatch.setattr(core, "Placement", FakePlacement)
    monkeypatch.setattr(core, "Rotation", FakeRotation)
    monkeypatch.setattr(core, "_resolve_shapestring_font_path", lambda *args: "C:/fonts/arial.ttf")
    return draft, FakeGroup(document)


def _canonical_3d_item(
    text="TEXT",
    font="ArialMT",
    *,
    span_index=0,
    requested_type="3d_text",
    page_number=1,
    line_direction=(1.0, 0.0),
):
    from PDFEmbeddedFonts import normalize_font_key

    span = _span(text)
    span["font"] = font
    return {
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": "a" * 64,
        "page_number": page_number,
        "source_item_id": f"p{page_number}:b0:l0:s{span_index}",
        "requested_type": requested_type,
        "text": text,
        "font_identity": {
            "raw_name": font,
            "normalized_key": normalize_font_key(font),
        },
        "bbox": tuple(span["bbox"]),
        "origin": tuple(span["origin"]),
        "line_direction": tuple(line_direction),
        "rotation_deg": core._line_angle_deg({"dir": line_direction}),
        "span": copy.deepcopy(span),
        "block_index": 0,
        "line_index": 0,
        "span_index": span_index,
    }


def _found_font_resolution(item, path="C:/fonts/arial.ttf"):
    return path, [{
        "source": "embedded_font",
        "outcome": "found",
        "font_identity": dict(item["font_identity"]),
        "path": path,
        "sha256": "b" * 64,
        "pdf_sha256": item["pdf_sha256"],
        "page_number": item["page_number"],
        "staging_complete": True,
    }]


def _force_zero_shapestring_geometry(document):
    original_recompute = document.recompute

    def recompute(*args):
        original_recompute(*args)
        for host_obj in document.Objects:
            if str(getattr(host_obj, "TypeId", "")) == "Part::Part2DObjectPython":
                host_obj.Shape = FakeZeroGeometryShape()

    document.recompute = recompute


def _absent_font_resolution(item):
    identity = dict(item["font_identity"])
    return None, [
        {
            "source": "embedded_font",
            "outcome": "not_found",
            "font_identity": dict(identity),
            "pdf_sha256": item["pdf_sha256"],
            "page_number": item["page_number"],
            "staging_complete": True,
        },
        {
            "source": "system_font",
            "outcome": "not_found",
            "font_identity": dict(identity),
            "pdf_sha256": item["pdf_sha256"],
            "page_number": item["page_number"],
            "staging_complete": True,
        },
    ]


def test_page_rotation_matrix_is_applied_exactly_once_before_y_flip(monkeypatch):
    monkeypatch.setattr(core, "Vector", FakeVector)
    opts = core.ImportOptions(scale_to_mm=False, user_scale=1.0, flip_y=True)
    opts._page_rotation_matrix = (0.0, 1.0, -1.0, 0.0, 80.0, 0.0)

    point = core._to_fc((10.0, 50.0), 160.0, opts, 1.0)

    assert (point.x, point.y) == pytest.approx((30.0, 150.0))


def test_page_rotation_changes_text_direction_inside_requested_type(monkeypatch):
    monkeypatch.setattr(core, "Vector", FakeVector)
    opts = core.ImportOptions(scale_to_mm=False, user_scale=1.0, flip_y=True)
    opts._page_rotation_matrix = (0.0, 1.0, -1.0, 0.0, 80.0, 0.0)

    angle = core._line_angle_deg({"dir": (1.0, 0.0)}, opts)

    assert angle == pytest.approx(-90.0)


def test_3d_text_creates_a_verified_parametric_extrusion(monkeypatch):
    document, draft, group = _install_host(monkeypatch)
    opts = core.ImportOptions(text_mode="3d_text")

    count = core._render_text_spans_3d(_tdict("TEXT"), group, 100.0, opts, 1.0)

    assert count == 1
    supports = [obj for obj in document.Objects if obj.TypeId == "Part::Part2DObjectPython"]
    delivered = [obj for obj in document.Objects if obj.TypeId == "Part::Extrusion"]
    assert len(supports) == 2
    assert len(delivered) == 1
    assert delivered[0].Base is supports[1]
    assert supports[1].Objects == [supports[0]]
    expected_advance = core._source_span_advance_fc(
        _span("TEXT"), {"dir": (1.0, 0.0)}, 1.0
    )
    assert supports[1].Scale.x == pytest.approx(expected_advance / 12.0)
    assert supports[1].Shape.BoundBox.XLength == pytest.approx(expected_advance)
    assert delivered[0].Solid is True
    assert delivered[0].Shape.Solids
    assert delivered[0].Shape.Volume > 0
    assert all(support.ViewObject.Visibility is False for support in supports)
    assert delivered[0] in group.objects
    assert draft.label_calls == []
    assert opts.text_delivered_counts == {"native_3d_text": 1}


def test_generic_shapestring_exception_stops_and_rolls_back_without_fallback(monkeypatch):
    document, draft, group = _install_host(monkeypatch, fail_texts={"FAIL"})
    unrelated = SimpleNamespace(Name="UserObject", TypeId="Part::Feature")
    document.Objects.append(unrelated)
    group.objects.append(unrelated)
    opts = core.ImportOptions(text_mode="3d_text")

    with pytest.raises(RuntimeError, match="3D Text"):
        core._render_text_spans_3d(
            _tdict("KEEP", "FAIL"), group, 100.0, opts, 1.0
        )

    assert document.Objects == [unrelated]
    assert unrelated not in [obj for obj in document.Objects if obj.Name in document.removed]
    assert draft.label_calls == []
    assert opts.text_delivered_counts == {}
    attempts = list(getattr(opts, "text_delivery_attempts", []) or [])
    assert attempts[-1]["requested_type"] == "3d_text"
    assert attempts[-1]["attempted_type"] == "3d_text"
    assert attempts[-1]["outcome"] == "failed"
    assert attempts[-1]["cleanup_complete"] is True


def test_3d_text_preserves_source_whitespace_and_rotation_even_in_quantity_column(monkeypatch):
    document, draft, group = _install_host(monkeypatch)
    opts = core.ImportOptions(text_mode="3d_text")
    quantity_context = {"quan_headers": [{"cx": 25.0, "cy": 10.0, "width": 20.0}]}

    count = core._render_text_spans_3d(
        _tdict(" 1 ", direction=(0.0, 1.0)),
        group,
        100.0,
        opts,
        1.0,
        layout_context=quantity_context,
    )

    assert count == 1
    assert draft.calls[0][0] == " 1 "
    support = next(obj for obj in document.Objects if obj.TypeId == "Part::Part2DObjectPython")
    assert support.Placement.Rotation.angle == pytest.approx(-90.0)


def test_labels_preserve_source_rotation_and_text_even_in_quantity_column(monkeypatch):
    calls = []

    class LabelDraft:
        @staticmethod
        def make_text(texts, placement=None):
            name = f"Label_{len(document.Objects)}"
            obj = SimpleNamespace(
                Name=name,
                Label=name,
                TypeId="App::FeaturePython",
                Document=document,
                Text=list(texts),
                Placement=placement,
                ViewObject=SimpleNamespace(),
            )
            document.Objects.append(obj)
            calls.append(obj)
            return obj

    monkeypatch.setattr(core, "Draft", LabelDraft)
    monkeypatch.setattr(core, "Vector", FakeVector)
    monkeypatch.setattr(core, "Placement", FakePlacement)
    monkeypatch.setattr(core, "Rotation", FakeRotation)
    document = FakeDocument()
    group = FakeGroup(document)
    tdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [{
                    "dir": (1.0, 0.0),
                    "spans": [{
                        "text": "QUAN",
                        "font": "ArialMT",
                        "size": 8.0,
                        "origin": (100.0, 30.0),
                        "bbox": (100.0, 20.0, 124.0, 32.0),
                    }],
                }],
            },
            {
                "type": 0,
                "lines": [{
                    "dir": (0.0, 1.0),
                    "spans": [{
                        "text": " 1 ",
                        "font": "ArialMT",
                        "size": 8.0,
                        "origin": (109.0, 90.4),
                        "bbox": (106.0, 78.0, 112.0, 92.0),
                        "descender": -0.2,
                    }],
                }],
            },
        ],
    }
    opts = core.ImportOptions(text_mode="labels", scale_to_mm=False)

    count = core._render_text_spans_exact_labels(
        tdict,
        group,
        100.0,
        opts,
        1.0,
        layout_context={"quan_headers": [{"cx": 112.0, "cy": 26.0, "width": 24.0}]},
    )

    assert count == 2
    quantity = next(obj for obj in calls if obj.Text == [" 1 "])
    assert quantity.Placement.Rotation.angle == pytest.approx(-90.0)


def test_partial_label_failure_rolls_back_instead_of_silently_dropping_text(monkeypatch):
    document = FakeDocument()

    class LabelObject(FakeHostObject):
        def __init__(self, text):
            self.Document = document
            self._set_host_name(f"Label_{len(document.Objects)}")
            self.Label = self.Name
            self.TypeId = "App::FeaturePython"
            self.Text = [text]
            self.ViewObject = SimpleNamespace()

        def addProperty(self, _kind, name, _group):
            setattr(self, name, "")

    class PartialLabelDraft:
        @staticmethod
        def make_text(texts, placement=None):
            text = texts[0]
            if text == "FAIL":
                raise RuntimeError("synthetic label failure")
            obj = LabelObject(text)
            obj.Placement = placement
            document.Objects.append(obj)
            return obj

    monkeypatch.setattr(core, "Draft", PartialLabelDraft)
    monkeypatch.setattr(core, "Vector", FakeVector)
    monkeypatch.setattr(core, "Placement", FakePlacement)
    monkeypatch.setattr(core, "Rotation", FakeRotation)
    group = FakeGroup(document)
    opts = core.ImportOptions(text_mode="labels", scale_to_mm=False)

    with pytest.raises(core.TextRepresentationFailure, match="Labels failed"):
        core._render_text_spans_exact_labels(
            _tdict("KEEP", "FAIL"), group, 100.0, opts, 1.0
        )

    assert document.Objects == []
    assert opts.text_delivered_counts == {}
    assert opts.text_delivery_attempts[0]["outcome"] == "rolled_back"
    assert opts.text_delivery_attempts[-1]["outcome"] == "failed"
    assert opts.text_delivery_attempts[-1]["attempted_type"] == "labels"


def test_missing_exact_font_is_explicit_failure_not_labels(monkeypatch):
    document, draft, group = _install_host(monkeypatch)
    monkeypatch.setattr(core, "_resolve_shapestring_font_path", lambda *args: None)
    opts = core.ImportOptions(text_mode="3d_text")

    with pytest.raises(RuntimeError, match="exact source font"):
        core._render_text_spans_3d(_tdict("TEXT"), group, 100.0, opts, 1.0)

    assert document.Objects == []
    assert draft.calls == []
    assert draft.label_calls == []
    assert opts.text_delivered_counts == {}


@pytest.mark.parametrize(
    ("requested", "bucket", "entity_count"),
    [
        ("glyphs", "outline_curve_or_mesh", 2),
        ("geometry", "raw_geometry_edges", 4),
    ],
)
def test_svg_text_mode_records_only_the_requested_verified_representation(
    monkeypatch, requested, bucket, entity_count
):
    from PDFVectorImporter.src import PDFSvgTextRenderer as renderer

    captured = {}

    def fake_render(*_args, **kwargs):
        captured.update(kwargs)
        return {
            "outcome": "verified",
            "entity_type": requested,
            "glyphs": 2,
            "entities": entity_count,
            "raw_edges": 4 if requested == "geometry" else 0,
            "created_entity_ids": [f"Entity{index}" for index in range(entity_count)],
            "delivery_attempts": [{
                "source_item_id": "p1:g0",
                "requested_type": requested,
                "attempted_type": requested,
                "final_type": requested,
                "outcome": "verified",
                "created_entity_ids": ["Entity0"],
            }],
        }

    monkeypatch.setattr(renderer, "render_text", fake_render)
    opts = core.ImportOptions(text_mode=requested)

    result, info = core._render_requested_svg_text(
        "fixture.pdf", 1, 100.0, 200.0, 1.0, object(), object(), opts
    )

    assert captured["representation"] == requested
    assert result["entity_type"] == requested
    assert info == {
        "entity_type": requested,
        "count": 2 if requested == "glyphs" else 4,
        "font_rendered": False,
        "examples": [],
    }
    assert opts.text_delivered_counts == {bucket: 2 if requested == "glyphs" else 4}
    assert opts.text_delivery_attempts[-1]["final_type"] == requested


def test_svg_generic_failure_is_terminal_and_never_walks_another_text_mode(monkeypatch):
    from PDFVectorImporter.src import PDFSvgTextRenderer as renderer

    def fail_render(*_args, **_kwargs):
        raise renderer.TextRepresentationRenderError(
            "svg_renderer_unavailable",
            {
                "requested_type": "glyphs",
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
            },
        )

    monkeypatch.setattr(renderer, "render_text", fail_render)
    opts = core.ImportOptions(text_mode="glyphs")

    with pytest.raises(core.TextRepresentationFailure, match="glyphs"):
        core._render_requested_svg_text(
            "fixture.pdf", 1, 100.0, 200.0, 1.0, object(), object(), opts
        )

    assert opts.text_delivered_counts == {}
    assert opts.text_delivery_attempts[-1]["attempted_type"] == "glyphs"
    assert opts.text_delivery_attempts[-1]["final_type"] is None
    assert opts.text_delivery_attempts[-1]["outcome"] == "failed"


def test_new_import_run_clears_prior_representation_evidence_without_changing_request():
    opts = core.ImportOptions(text_mode="geometry", import_text=True, user_scale=2.5)
    opts.text_delivery_attempts.append({"source_item_id": "old"})
    opts.text_delivered_counts["raw_geometry_edges"] = 8
    opts.text_mode_fallbacks.append({"requested": "geometry"})
    opts.raster_page_count = 2
    opts.raster_fallback_reasons.append("old")
    opts.auto_resolved_mode = "raster"
    opts.auto_reason = "old"
    opts.phase_timings_ms["old"] = 1.0
    opts.shapestring_skips["old"] = 1
    opts._font_stage_failures = [{"font": "old"}]
    opts._shapestring_font_paths = {"old": {"path": "old.ttf"}}
    opts._shapestring_font_staging_sessions = [{"page_number": 99}]
    opts._report_extra = {"old": True}

    core._reset_import_run_state(opts)

    assert opts.text_mode == "geometry"
    assert opts.import_text is True
    assert opts.user_scale == 2.5
    assert opts.text_delivery_attempts == []
    assert opts.text_delivered_counts == {}
    assert opts.text_mode_fallbacks == []
    assert opts.raster_page_count == 0
    assert opts.raster_fallback_reasons == []
    assert opts.auto_resolved_mode is None
    assert opts.auto_reason is None
    assert opts.phase_timings_ms == {}
    assert opts.shapestring_skips == {}
    assert opts._font_stage_failures == []
    assert opts._shapestring_font_paths == {}
    assert opts._shapestring_font_staging_sessions == []
    assert opts._report_extra == {}
    assert opts._svg_source_snapshot_cache == {}


def test_source_items_preserve_original_identity_and_transform():
    source_span = {
        "text": " ITEM ",
        "font": "  ABCDEF+Siwa-Regular  ",
        "size": 9.5,
        "origin": (15, 25),
        "bbox": (14, 16, 42, 28),
        "color": 0x123456,
        "ascender": 0.8,
        "descender": -0.2,
    }
    tdict = {
        "blocks": [
            {"type": 1, "lines": [{"spans": [{"text": "image"}]}]},
            {
                "type": 0,
                "lines": [
                    {"dir": (1, 0), "spans": [{"text": "   "}]},
                    {
                        "dir": (0, 1),
                        "spans": [
                            {"text": "\t"},
                            source_span,
                        ],
                    },
                ],
            },
        ]
    }

    items = list(core._iter_text_source_items(
        tdict, 2, "A" * 64, " 3D Text "
    ))

    assert len(items) == 1
    item = items[0]
    assert item["importer_identity"] == core.FREECAD_TEXT_IMPORTER_IDENTITY
    assert item["pdf_sha256"] == "a" * 64
    assert item["page_number"] == 2
    assert item["source_item_id"] == "p2:b1:l1:s1"
    assert item["requested_type"] == "3d_text"
    assert item["text"] == " ITEM "
    assert item["font_identity"] == {
        "raw_name": "ABCDEF+Siwa-Regular",
        "normalized_key": "siwaregular",
    }
    assert item["bbox"] == (14.0, 16.0, 42.0, 28.0)
    assert item["origin"] == (15.0, 25.0)
    assert item["line_direction"] == (0.0, 1.0)
    assert item["rotation_deg"] == pytest.approx(-90.0)
    assert (item["block_index"], item["line_index"], item["span_index"]) == (1, 1, 1)
    assert type(item["span"]) is dict
    assert item["span"]["text"] == " ITEM "
    assert item["span"]["font"] == "  ABCDEF+Siwa-Regular  "

    source_span["origin"] = (999, 999)
    source_span["text"] = "mutated source"
    assert item["origin"] == (15.0, 25.0)
    assert item["span"]["text"] == " ITEM "


@pytest.mark.parametrize(
    ("field_name", "oversized"),
    [
        ("bbox", (10.0, 40.0, 40.0, 52.0, 99.0)),
        ("origin", (10.0, 50.0, 99.0)),
        ("line_direction", (1.0, 0.0, 99.0)),
    ],
)
def test_source_items_reject_oversized_transform_tuples(field_name, oversized):
    tdict = _tdict("TEXT")
    span = tdict["blocks"][0]["lines"][0]["spans"][0]
    if field_name == "line_direction":
        tdict["blocks"][0]["lines"][0]["dir"] = oversized
    else:
        span[field_name] = oversized

    with pytest.raises(ValueError, match="must contain"):
        list(core._iter_text_source_items(tdict, 1, "a" * 64, "3d_text"))


def test_exact_font_exhaustion_raises_fully_bound_text_item_impossible(
    monkeypatch, tmp_path
):
    import PDFEmbeddedFonts as embedded

    item = _canonical_3d_item(font="ABCDEF+Siwa-Regular")
    opts = core.ImportOptions(text_mode="3d_text")
    monkeypatch.setenv("WINDIR", str(tmp_path))

    def stage_exact_nonembedded_font(*_args, failures):
        failures.append({
            "xref": 0,
            "font": item["font_identity"]["raw_name"],
            "outcome": "not_embedded",
            "reason": "embedded_font_not_present",
            "exception": "",
        })
        return {}

    monkeypatch.setattr(
        embedded,
        "stage_page_fonts",
        stage_exact_nonembedded_font,
    )
    core._stage_page_shapestring_fonts(
        object(),
        object(),
        opts,
        pdf_sha256=item["pdf_sha256"],
        page_number=item["page_number"],
    )

    with pytest.raises(core.TextItemImpossible) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            opts,
            text_group=None,
            page_h=100.0,
            scale=1.0,
        )

    impossible = raised.value
    proof = impossible.proof
    attempt = impossible.attempt
    assert proof["item_specific_proven_impossible"] is True
    assert proof["importer_identity"] == core.FREECAD_TEXT_IMPORTER_IDENTITY
    assert proof["pdf_sha256"] == item["pdf_sha256"]
    assert proof["page_number"] == item["page_number"]
    assert proof["source_item_id"] == item["source_item_id"]
    assert proof["requested_type"] == "3d_text"
    assert proof["attempted_type"] == "3d_text"
    assert proof["reason_code"] == "exact_font_unavailable"
    assert proof["font_identity"] == item["font_identity"]
    assert [result["source"] for result in proof["attempted_source_results"]] == [
        "embedded_font",
        "system_font",
    ]
    assert all(
        result["outcome"] == "not_found"
        and result["font_identity"] == item["font_identity"]
        and result["pdf_sha256"] == item["pdf_sha256"]
        and result["page_number"] == item["page_number"]
        and result["staging_complete"] is True
        for result in proof["attempted_source_results"]
    )
    assert proof["attempted_sources_complete"] is True
    assert proof["created_entity_ids"] == []
    assert proof["removed_entity_ids"] == []
    assert proof["cleanup_complete"] is True
    assert attempt == {
        "source_item_id": item["source_item_id"],
        "requested_type": "3d_text",
        "attempted_type": "3d_text",
        "final_type": None,
        "outcome": "proven_impossible",
        "reason_code": "exact_font_unavailable",
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }
    assert core._validate_item_impossibility_proof(
        item, "3d_text", "3d_text", proof
    ) == proof


def _raise_complete_zero_outline_impossibility(monkeypatch):
    document, draft, group = _install_host(monkeypatch)
    _force_zero_shapestring_geometry(document)
    item = _canonical_3d_item("UNMAPPED")
    resolution = _found_font_resolution(item)
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: resolution,
    )

    class ZeroOutlinePart:
        @staticmethod
        def makeWireString(source_text, font_path, size, tracking):
            assert font_path == resolution[0]
            assert (size, tracking) == (1.0, 0)
            return [[] for _character in source_text]

    monkeypatch.setattr(core, "Part", ZeroOutlinePart)
    monkeypatch.setattr(
        core,
        "_create_verified_compound_text3d_entity",
        lambda _doc, **kwargs: core._build_exact_text3d_outline_template(
            kwargs["source_text"], kwargs["font_path"]
        ),
    )

    with pytest.raises(core.TextItemImpossible) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    return item, resolution, raised.value, document, draft


def test_zero_part_outlines_use_exact_shapestring_before_cross_mode_fallback(
    monkeypatch,
):
    document, draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT")
    resolution = _found_font_resolution(item)
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: resolution,
    )

    class ZeroOutlinePart:
        @staticmethod
        def makeWireString(source_text, _font_path, _size, _tracking):
            return [[] for _character in source_text]

    monkeypatch.setattr(core, "Part", ZeroOutlinePart)
    monkeypatch.setattr(
        core,
        "_create_verified_compound_text3d_entity",
        lambda _doc, **kwargs: core._build_exact_text3d_outline_template(
            kwargs["source_text"], kwargs["font_path"]
        ),
    )

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        core.ImportOptions(text_mode="3d_text"),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["outcome"] == "verified"
    assert result["final_type"] == "3d_text"
    assert result["evidence"]["implementation"] == (
        "parametric_shapestring_fallback_v1"
    )
    assert draft.calls == [(item["text"], resolution[0])]
    assert len(document.Objects) == 3


def test_complete_zero_outline_exact_font_item_has_a_bound_private_proof(monkeypatch):
    item, resolution, impossible, document, draft = (
        _raise_complete_zero_outline_impossibility(monkeypatch)
    )
    proof = impossible.proof

    assert impossible.attempt["outcome"] == "proven_impossible"
    assert impossible.attempt["reason_code"] == (
        "text3d_exact_font_outlines_unavailable"
    )
    assert proof["reason_code"] == "text3d_exact_font_outlines_unavailable"
    assert proof["importer_identity"] == core.FREECAD_TEXT_IMPORTER_IDENTITY
    assert proof["pdf_sha256"] == item["pdf_sha256"]
    assert proof["page_number"] == item["page_number"]
    assert proof["source_item_id"] == item["source_item_id"]
    assert proof["requested_type"] == "3d_text"
    assert proof["attempted_type"] == "3d_text"
    assert proof["font_identity"] == item["font_identity"]
    assert len(proof["attempted_source_results"]) == 1
    assert proof["attempted_source_results"][0]["source"] == "embedded_font"
    assert proof["attempted_source_results"][0]["outcome"] == "found"
    assert proof["attempted_source_results"][0]["sha256"] == "b" * 64
    assert "path" not in proof["attempted_source_results"][0]
    assert proof["attempted_sources_complete"] is True
    assert proof["created_entity_ids"] == ["ShapeString_0"]
    assert proof["removed_entity_ids"] == ["ShapeString_0"]
    assert proof["cleanup_complete"] is True
    evidence = proof["evidence"]
    assert evidence["source_text_sha256"] == core.hashlib.sha256(
        b"UNMAPPED"
    ).hexdigest()
    assert evidence["font_sha256"] == "b" * 64
    assert evidence["font_source"] == "embedded_font"
    assert [result["implementation"] for result in evidence["exact_3d_path_results"]] == [
        "part_make_wire_string",
        "draft_shapestring",
    ]
    assert all(
        result["outcome"] == "zero_geometry"
        for result in evidence["exact_3d_path_results"]
    )
    assert evidence["exact_3d_path_results"][1]["recompute_completed"] is True
    assert evidence["exact_3d_path_results"][1]["face_count"] == 0
    assert evidence["exact_3d_path_results"][1]["wire_count"] == 0
    assert item["text"] not in repr(proof)
    assert resolution[0] not in repr(proof)
    assert "font_path" not in repr(proof)
    assert core._validate_item_impossibility_proof(
        item, "3d_text", "3d_text", proof
    ) == proof
    assert document.Objects == []
    assert draft.calls == [(item["text"], resolution[0])]


def test_system_font_zero_outline_proof_hashes_once_and_binds_that_digest(monkeypatch):
    document, draft, group = _install_host(monkeypatch)
    _force_zero_shapestring_geometry(document)
    item = _canonical_3d_item("UNMAPPED")
    identity = dict(item["font_identity"])
    font_path = "C:/fonts/arial.ttf"
    resolution = (
        font_path,
        [
            {
                "source": "embedded_font",
                "outcome": "not_found",
                "font_identity": dict(identity),
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "staging_complete": True,
            },
            {
                "source": "system_font",
                "outcome": "found",
                "font_identity": dict(identity),
                "path": font_path,
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "staging_complete": True,
            },
        ],
    )
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: resolution,
    )
    hash_calls = []
    monkeypatch.setattr(
        core,
        "_path_sha256",
        lambda path: hash_calls.append(path) or "c" * 64,
    )

    class ZeroOutlinePart:
        @staticmethod
        def makeWireString(source_text, _font_path, _size, _tracking):
            return [[] for _character in source_text]

    monkeypatch.setattr(core, "Part", ZeroOutlinePart)
    monkeypatch.setattr(
        core,
        "_create_verified_compound_text3d_entity",
        lambda _doc, **kwargs: core._build_exact_text3d_outline_template(
            kwargs["source_text"], kwargs["font_path"]
        ),
    )

    with pytest.raises(core.TextItemImpossible) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    proof = raised.value.proof
    assert hash_calls == [Path(font_path)]
    assert proof["evidence"]["font_sha256"] == "c" * 64
    assert proof["evidence"]["font_source"] == "system_font"
    assert proof["attempted_source_results"][-1]["sha256"] == "c" * 64
    assert "path" not in proof["attempted_source_results"][-1]
    assert font_path not in repr(proof)
    assert core._validate_item_impossibility_proof(
        item, "3d_text", "3d_text", proof
    ) == proof
    assert document.Objects == []
    assert draft.calls == [(item["text"], font_path)]


def test_zero_outline_impossibility_rejects_every_mutated_binding(monkeypatch):
    item, _resolution, impossible, _document, _draft = (
        _raise_complete_zero_outline_impossibility(monkeypatch)
    )
    mutations = [
        lambda proof: proof["evidence"].__setitem__("source_text_sha256", "c" * 64),
        lambda proof: proof["evidence"].__setitem__("source_text_length", 7),
        lambda proof: proof["evidence"].__setitem__(
            "non_whitespace_character_count", 7
        ),
        lambda proof: proof["evidence"].__setitem__("glyph_inventory_length", 7),
        lambda proof: proof["evidence"].__setitem__("glyph_outline_count", 1),
        lambda proof: proof["evidence"].__setitem__("font_source", "system_font"),
        lambda proof: proof["evidence"].__setitem__("font_sha256", "c" * 64),
        lambda proof: proof["evidence"]["exact_3d_path_results"].pop(),
        lambda proof: proof["attempted_source_results"][-1].__setitem__(
            "outcome", "not_found"
        ),
        lambda proof: proof.__setitem__("cleanup_complete", False),
    ]

    for mutate in mutations:
        changed = copy.deepcopy(impossible.proof)
        mutate(changed)
        with pytest.raises(ValueError):
            core._validate_item_impossibility_proof(
                item, "3d_text", "3d_text", changed
            )


def test_nonclosed_compound_failure_plus_zero_shapestring_is_terminal(monkeypatch):
    document, draft, group = _install_host(monkeypatch)
    _force_zero_shapestring_geometry(document)
    item = _canonical_3d_item("TEXT")
    resolution = _found_font_resolution(item)
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: resolution,
    )
    monkeypatch.setattr(
        core,
        "_create_verified_compound_text3d_entity",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("source glyph inventory does not match source text")
        ),
    )

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    assert raised.value.attempt["outcome"] == "failed"
    assert raised.value.attempt["reason"] == "calibration_extrusion_failed"
    assert draft.calls == [(item["text"], resolution[0])]
    assert document.Objects == []


def test_zero_outline_proof_advances_to_glyphs_without_relabeling_requested_mode(
    monkeypatch,
):
    item, _resolution, impossible, _document, _draft = (
        _raise_complete_zero_outline_impossibility(monkeypatch)
    )
    opts = core.ImportOptions(text_mode="3d_text")
    calls = []

    def impossible_3d(_item, attempted_type, _opts):
        calls.append(attempted_type)
        raise core.TextItemImpossible(
            str(impossible),
            attempt=impossible.attempt,
            proof=impossible.proof,
        )

    def verified_glyphs(_item, attempted_type, _opts):
        calls.append(attempted_type)
        return {
            "outcome": "verified",
            "source_item_id": item["source_item_id"],
            "attempted_type": "glyphs",
            "final_type": "glyphs",
            "created_entity_ids": ["Glyphs001"],
            "delivery_entity_ids": ["Glyphs001"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {"host_entity_verified": True},
        }

    result = core._run_text_item_fallback_ladder(
        item,
        "3d_text",
        {
            "3d_text": impossible_3d,
            "glyphs": verified_glyphs,
        },
        opts,
    )

    assert calls == ["3d_text", "glyphs"]
    assert result["requested_type"] == "3d_text"
    assert result["attempted_type"] == "glyphs"
    assert result["final_type"] == "glyphs"
    assert result["attempted_types"] == ["3d_text", "glyphs"]
    assert [proof["reason_code"] for proof in result["proof_chain"]] == [
        "text3d_exact_font_outlines_unavailable"
    ]
    assert len(opts.text_mode_fallbacks) == 1
    fallback = opts.text_mode_fallbacks[0]
    assert fallback["requested"] == "3d_text"
    assert fallback["delivered"] == "glyphs"
    assert fallback["reason"] == (
        "proof_gated:3d_text:text3d_exact_font_outlines_unavailable"
    )


def test_later_3d_rung_success_preserves_original_requested_type(monkeypatch):
    document, _draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT", requested_type="labels")
    resolution = _found_font_resolution(item)
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: resolution,
    )

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        core.ImportOptions(text_mode="labels"),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["requested_type"] == "labels"
    assert result["attempted_type"] == "3d_text"
    assert result["final_type"] == "3d_text"
    assert set(result["created_entity_ids"]) == {obj.Name for obj in document.Objects}


def test_successful_system_font_3d_delivery_does_not_hash_font_per_item(monkeypatch):
    document, _draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT")
    identity = dict(item["font_identity"])
    resolution = (
        "C:/fonts/arial.ttf",
        [
            {
                "source": "embedded_font",
                "outcome": "not_found",
                "font_identity": dict(identity),
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "staging_complete": True,
            },
            {
                "source": "system_font",
                "outcome": "found",
                "font_identity": dict(identity),
                "path": "C:/fonts/arial.ttf",
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "staging_complete": True,
            },
        ],
    )
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: resolution,
    )
    hash_calls = []
    monkeypatch.setattr(
        core,
        "_path_sha256",
        lambda path: hash_calls.append(path) or "c" * 64,
    )

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        core.ImportOptions(text_mode="3d_text"),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["outcome"] == "verified"
    assert hash_calls == []
    assert document.Objects


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
def test_shapestring_runtime_error_is_unproven_terminal_and_cleans_only_current_item(
    monkeypatch, error_type
):
    document, draft, group = _install_host(monkeypatch)
    prior = SimpleNamespace(Name="PriorVerified", TypeId="Part::Extrusion")
    document.Objects.append(prior)
    group.objects.append(prior)
    item = _canonical_3d_item("FAIL")

    def create_then_fail(text, font_path):
        draft.calls.append((text, font_path))
        partial = FakeShapeString(
            document,
            f"ShapeString_{len(document.Objects)}",
            text,
        )
        document.Objects.append(partial)
        raise error_type("synthetic ShapeString failure after host creation")

    draft.make_shapestring = create_then_fail
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _found_font_resolution(item),
    )
    opts = core.ImportOptions(text_mode="3d_text")

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            opts,
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    attempt = raised.value.attempt
    assert attempt["outcome"] == "failed"
    assert attempt["reason"] == "shapestring_creation_failed"
    assert len(attempt["created_entity_ids"]) == 1
    assert set(attempt["created_entity_ids"]) == set(attempt["removed_entity_ids"])
    assert attempt["cleanup_complete"] is True
    assert document.Objects == [prior]
    assert group.objects == [prior]
    assert "PriorVerified" not in document.removed
    assert draft.label_calls == []


def test_transient_baseline_snapshot_failure_never_claims_prior_objects(monkeypatch):
    document = TransientObjectsDocument()
    draft, group = _install_document_host(monkeypatch, document)
    prior = SimpleNamespace(Name="PriorVerified", TypeId="Part::Extrusion")
    document._objects.append(prior)
    group.objects.append(prior)
    item = _canonical_3d_item("TEXT")
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _found_font_resolution(item),
    )
    document.object_reads = 0
    document.fail_object_reads = {1}

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    attempt = raised.value.attempt
    assert attempt["reason"] == "freecad_3d_text_document_snapshot_failed"
    assert attempt["created_entity_ids"] == []
    assert attempt["removed_entity_ids"] == []
    assert attempt["cleanup_complete"] is True
    assert document._objects == [prior]
    assert document.removed == []
    assert draft.calls == []


def test_factory_returned_baseline_object_is_rejected_and_never_claimed_or_removed(
    monkeypatch,
):
    document, draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT")
    prior = FakeShapeString(document, "PriorShapeString", item["text"])
    document.Objects.append(prior)
    group.objects.append(prior)

    def return_prior(text, font_path):
        draft.calls.append((text, font_path))
        return prior

    draft.make_shapestring = return_prior
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _found_font_resolution(item),
    )

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    attempt = raised.value.attempt
    assert attempt["reason"] == "shapestring_creation_failed"
    assert "pre-existing baseline object" in attempt["evidence"]["exception"]
    assert attempt["created_entity_ids"] == []
    assert attempt["removed_entity_ids"] == []
    assert attempt["cleanup_complete"] is True
    assert document.Objects == [prior]
    assert group.objects == [prior]
    assert document.removed == []
    assert prior.Name == "PriorShapeString"
    assert prior.Placement is None
    assert not hasattr(prior, "PDFSourceItemId")


def test_clone_factory_baseline_return_is_rejected_before_mutation(monkeypatch):
    document, draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT")
    prior_source = FakeShapeString(document, "PriorCloneSource", "PRIOR")
    prior = FakeShapeStringClone(document, "PriorCalibratedSupport", prior_source)
    prior.Scale = FakeVector(0.75, 1.25, 1.0)
    prior.Label = "Prior calibrated support"
    original_scale = (prior.Scale.x, prior.Scale.y, prior.Scale.z)
    document.Objects.append(prior)

    draft.clone = lambda _source: prior
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _found_font_resolution(item),
    )

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    attempt = raised.value.attempt
    assert attempt["reason"] == "calibration_extrusion_failed"
    assert "pre-existing baseline object" in attempt["evidence"]["exception"]
    assert set(attempt["created_entity_ids"]) == set(attempt["removed_entity_ids"])
    assert attempt["cleanup_complete"] is True
    assert document.Objects == [prior]
    assert group.objects == []
    assert prior.Name == "PriorCalibratedSupport"
    assert prior.Label == "Prior calibrated support"
    assert (prior.Scale.x, prior.Scale.y, prior.Scale.z) == original_scale
    assert not hasattr(prior, "PDFSourceItemId")


def test_extrusion_factory_baseline_return_is_rejected_before_mutation(monkeypatch):
    document, _draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT")
    prior = FakeExtrusion(document, "PriorExtrusion")
    prior.Label = "Prior extrusion"
    prior.Base = "sentinel-base"
    prior.Dir = "sentinel-direction"
    prior.Solid = False
    document.Objects.append(prior)

    def return_prior(kind, name):
        assert kind == "Part::Extrusion"
        assert name == "PDF_3D_Text"
        return prior

    document.addObject = return_prior
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _found_font_resolution(item),
    )

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    attempt = raised.value.attempt
    assert attempt["reason"] == "calibration_extrusion_failed"
    assert "pre-existing baseline object" in attempt["evidence"]["exception"]
    assert set(attempt["created_entity_ids"]) == set(attempt["removed_entity_ids"])
    assert attempt["cleanup_complete"] is True
    assert document.Objects == [prior]
    assert group.objects == []
    assert prior.Name == "PriorExtrusion"
    assert prior.Label == "Prior extrusion"
    assert prior.Base == "sentinel-base"
    assert prior.Dir == "sentinel-direction"
    assert prior.Solid is False
    assert not hasattr(prior, "PDFSourceItemId")


def test_collection_error_after_creation_is_incomplete_and_detects_unknown_object(
    monkeypatch,
):
    document = TransientObjectsDocument()
    draft, group = _install_document_host(monkeypatch, document)
    prior = SimpleNamespace(Name="PriorVerified", TypeId="Part::Extrusion")
    document._objects.append(prior)
    group.objects.append(prior)
    item = _canonical_3d_item("FAIL")
    partial = FakeShapeString(document, "PartialShapeString", item["text"])

    def create_then_fail(_text, _font_path):
        document._objects.append(partial)
        raise RuntimeError("synthetic failure after unknown object creation")

    draft.make_shapestring = create_then_fail
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _found_font_resolution(item),
    )
    document.object_reads = 0
    document.fail_object_reads = {2}

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    attempt = raised.value.attempt
    assert attempt["reason"] == "shapestring_creation_failed"
    assert attempt["cleanup_complete"] is False
    assert attempt["created_entity_ids"] == []
    assert attempt["removed_entity_ids"] == []
    assert attempt["evidence"]["ownership_collection_error"]
    assert attempt["evidence"]["unknown_post_baseline_entity_ids"] == [
        "PartialShapeString"
    ]
    assert document._objects == [prior, partial]
    assert document.removed == []


def test_prior_verified_item_is_not_removed_when_later_item_is_impossible(monkeypatch):
    document, _draft, group = _install_host(monkeypatch)
    first = _canonical_3d_item("FIRST", span_index=0)
    second = _canonical_3d_item("SECOND", "Siwa-Regular", span_index=1)

    def resolve(font_name, _opts, **_context):
        if font_name == first["font_identity"]["raw_name"]:
            return _found_font_resolution(first)
        return _absent_font_resolution(second)

    monkeypatch.setattr(core, "_resolve_shapestring_font_path_with_evidence", resolve)
    opts = core.ImportOptions(text_mode="3d_text")

    verified = core._deliver_text_item_3d(
        first,
        "3d_text",
        opts,
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )
    with pytest.raises(core.TextItemImpossible):
        core._deliver_text_item_3d(
            second,
            "3d_text",
            opts,
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    live_ids = {obj.Name for obj in document.Objects}
    assert set(verified["created_entity_ids"]) == live_ids
    assert len(document.Objects) == 3
    assert document.removed == []


@pytest.mark.parametrize(
    ("flip_y", "expected_angle"),
    [(True, -90.0), (False, 90.0)],
)
def test_item_3d_applies_page_matrix_and_flip_to_anchor_and_rotation(
    monkeypatch, flip_y, expected_angle
):
    document, _draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT", line_direction=(1.0, 0.0))
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _found_font_resolution(item),
    )
    opts = core.ImportOptions(text_mode="3d_text", flip_y=flip_y)
    opts._page_rotation_matrix = (0.0, 1.0, -1.0, 0.0, 80.0, 0.0)
    expected_pos = core._to_fc(item["origin"], 160.0, opts, 1.0)

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        opts,
        text_group=group,
        page_h=160.0,
        scale=1.0,
    )

    support = document.Objects[0]
    calibrated = document.Objects[1]
    assert support.Placement.Base.x == pytest.approx(expected_pos.x)
    assert support.Placement.Base.y == pytest.approx(expected_pos.y)
    assert support.Placement.Rotation.angle == pytest.approx(expected_angle)
    assert calibrated.Placement.Base.x == pytest.approx(expected_pos.x)
    assert calibrated.Placement.Base.y == pytest.approx(expected_pos.y)
    assert calibrated.Placement.Rotation.angle == pytest.approx(expected_angle)
    assert result["evidence"]["rotation_deg"] == pytest.approx(expected_angle)
    assert result["evidence"]["verified_anchor_xyz"] == pytest.approx(
        (expected_pos.x, expected_pos.y, expected_pos.z)
    )


def test_corrupted_calibrated_anchor_is_terminal_and_cleans_current_item(monkeypatch):
    document, _draft, group = _install_host(monkeypatch)
    prior = SimpleNamespace(Name="PriorVerified", TypeId="Part::Extrusion")
    document.Objects.append(prior)
    group.objects.append(prior)
    item = _canonical_3d_item("TEXT")
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _found_font_resolution(item),
    )
    original_create = core._create_verified_text3d_entity

    def corrupt_anchor(*args, **kwargs):
        extrusion, calibrated, horizontal_scale, verified_advance = original_create(
            *args, **kwargs
        )
        calibrated.Placement = FakePlacement(
            FakeVector(999.0, 999.0, 999.0),
            calibrated.Placement.Rotation,
        )
        return extrusion, calibrated, horizontal_scale, verified_advance

    monkeypatch.setattr(core, "_create_verified_text3d_entity", corrupt_anchor)

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    attempt = raised.value.attempt
    assert attempt["reason"] == "host_verification_failed"
    assert attempt["cleanup_complete"] is True
    assert set(attempt["created_entity_ids"]) == set(attempt["removed_entity_ids"])
    assert document.Objects == [prior]
    assert "PriorVerified" not in document.removed


def test_verified_item_3d_result_has_complete_ids_and_evidence(monkeypatch):
    document, draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT")
    resolution = _found_font_resolution(item)
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: resolution,
    )
    opts = core.ImportOptions(text_mode="3d_text")

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        opts,
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["outcome"] == "verified"
    assert result["source_item_id"] == item["source_item_id"]
    assert result["requested_type"] == "3d_text"
    assert result["attempted_type"] == "3d_text"
    assert result["final_type"] == "3d_text"
    assert len(result["created_entity_ids"]) == 3
    assert set(result["created_entity_ids"]) == {obj.Name for obj in document.Objects}
    assert result["delivery_entity_ids"] == [document.Objects[2].Name]
    assert result["support_entity_ids"] == [
        document.Objects[0].Name,
        document.Objects[1].Name,
    ]
    assert result["removed_entity_ids"] == []
    assert result["cleanup_complete"] is True
    evidence = result["evidence"]
    assert evidence["source_text"] == item["text"]
    assert evidence["source_text_preserved"] is True
    assert evidence["source_item_id"] == item["source_item_id"]
    assert evidence["source_item_id_verified"] is True
    assert evidence["entity_type"] == "Part::Extrusion"
    assert evidence["solid_count"] > 0
    assert evidence["volume"] > 0.0
    assert evidence["rotation_deg"] == pytest.approx(item["rotation_deg"])
    assert evidence["verified_anchor_xyz"] == pytest.approx((10.0, 50.0, 0.0))
    assert evidence["target_advance"] > 0.0
    assert evidence["verified_advance"] == pytest.approx(evidence["target_advance"])
    assert evidence["font_path"] == resolution[0]
    assert evidence["font_source_result"] == resolution[1][0]
    support, calibrated, extrusion = document.Objects
    assert support.ViewObject.Visibility is False
    assert calibrated.ViewObject.Visibility is False
    assert calibrated.Placement.Base.x == pytest.approx(support.Placement.Base.x)
    assert calibrated.Placement.Base.y == pytest.approx(support.Placement.Base.y)
    assert calibrated.Placement.Rotation.angle == pytest.approx(
        support.Placement.Rotation.angle
    )
    assert extrusion.Base is calibrated
    assert draft.label_calls == []

    normalized = core._normalize_verified_text_item_result(
        item, "3d_text", "3d_text", result
    )
    assert normalized["delivery_entity_ids"] == [extrusion.Name]
    assert set(normalized["support_entity_ids"]) == {support.Name, calibrated.Name}


def test_verified_item_3d_prefers_one_exact_compound_with_editable_metadata(
    monkeypatch,
):
    document, draft, group = _install_host(monkeypatch)
    item = _canonical_3d_item("TEXT")
    resolution = _found_font_resolution(item)
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: resolution,
    )

    class CompoundHost(FakeHostObject):
        def __init__(self, placement, target_advance):
            self.Document = document
            self._set_host_name("PDF_3D_Text_0")
            self.Label = "PDF 3D Text"
            self.TypeId = "Part::Feature"
            self.PropertiesList = []
            self.ViewObject = None
            self.Placement = placement
            self.Shape = FakeShape(
                solid=True,
                width=target_advance,
                height=5.0,
                angle=0.0,
            )

        def addProperty(self, _kind, name, _group):
            self.PropertiesList.append(name)

    def create_compound(
        _doc,
        *,
        placement,
        target_advance_fc,
        text_group,
        configure_host,
        **_kwargs,
    ):
        host = CompoundHost(placement, target_advance_fc)
        document.Objects.append(host)
        configure_host(host)
        text_group.addObject(host)
        return host, 0.5, target_advance_fc * 2.0, target_advance_fc

    monkeypatch.setattr(
        core, "_create_verified_compound_text3d_entity", create_compound
    )

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        core.ImportOptions(text_mode="3d_text"),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert len(document.Objects) == 1
    host = document.Objects[0]
    assert result["created_entity_ids"] == [host.Name]
    assert result["delivery_entity_ids"] == [host.Name]
    assert result["support_entity_ids"] == []
    assert result["evidence"]["implementation"] == "exact_glyph_solid_compound_v1"
    assert result["evidence"]["host_entity_count"] == 1
    assert result["evidence"]["source_metadata_editable"] is True
    assert host.PDFSourceText == item["text"]
    assert host.PDFSourceItemId == item["source_item_id"]
    assert host.PDFRepresentation == "3d_text"
    assert host.PDFGeometryEncoding == "exact_glyph_solid_compound_v1"
    assert host.PDFFontFile == resolution[0]
    assert host.PDFFontFileSHA256 == resolution[1][0]["sha256"]
    assert draft.calls == []
    assert draft.label_calls == []
