"""Headless style persistence + GUI restore contract for FreeCAD imports.

Root cause (1011 side-by-side triage, 2026-08-16): the importer stored every
geometry style only on ``obj.ViewObject`` (``_apply_style``) and text style on
the view plus App metadata.  A FreeCADCmd (headless) save has no view
providers and writes no ``GuiDocument.xml``; on GUI open Draft recreates the
Text/Label view providers at its defaults (FontSize 3.5 mm, default font, a 1 mm
"Dot" arrow on every Label) and every Batch/Face opens Solid / LineWidth 2 /
default colours, so 1,409 dashed hidden and centre lines render solid and the
0.24/0.6/0.84/1.32 pt weight hierarchy is flattened.

The fix has two halves that these tests pin host-free:

* ``_apply_style`` persists the intended style App-side on every object it
  touches (``PDFStrokeRGB`` / ``PDFFillRGB`` / ``PDFLineWidthPt`` /
  ``PDFDashPattern``) even when there is no ViewObject.
* ``PDFStyleRestore`` re-applies the App-side contract to the view providers
  when a document opens in the GUI (visibility, Draft text style, Label arrow
  removal, geometry style via the same ``_apply_style`` rule), idempotently and
  without raising on partial data.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402
import PDFStyleRestore as restore  # noqa: E402


# ---------------------------------------------------------------------------
# Fakes: the smallest FreeCAD surface the code under test touches.
# ---------------------------------------------------------------------------


class _FakeView:
    """A view provider that records every property write."""

    def __init__(self, **props):
        object.__setattr__(self, "writes", [])
        for name, value in props.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        self.writes.append((name, value))
        object.__setattr__(self, name, value)


class _FakeObject:
    def __init__(self, name, type_id="Part::Feature", view=None, **props):
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.ViewObject = view
        self.Visibility = True
        self.InList = []
        self.PropertiesList = []
        for key, value in props.items():
            self.PropertiesList.append(key)
            setattr(self, key, value)

    def addProperty(self, _kind, name, _group):
        if name not in self.PropertiesList:
            self.PropertiesList.append(name)
        return self


class _FakeDocument:
    def __init__(self, objects, restoring=False):
        self.Objects = list(objects)
        self.Restoring = restoring
        self.Name = "FakeDoc"


def _text_view(**overrides):
    props = dict(
        FontSize=3.5,
        FontName="",
        Justification="Center",
        TextColor=(0.0, 0.0, 0.0, 1.0),
        Visibility=False,
    )
    props.update(overrides)
    return _FakeView(**props)


def _label_view(**overrides):
    props = dict(
        FontSize=3.5,
        FontName="",
        Justification="Center",
        TextColor=(0.0, 0.0, 0.0, 1.0),
        LineColor=(0.1, 0.1, 0.1, 1.0),
        ArrowTypeStart="Dot",
        Line=True,
        Visibility=False,
    )
    props.update(overrides)
    return _FakeView(**props)


def _geometry_view(**overrides):
    props = dict(
        LineColor=(0.1, 0.1, 0.1, 1.0),
        PointColor=(0.1, 0.1, 0.1, 1.0),
        ShapeColor=(0.8, 0.8, 0.8, 1.0),
        LineWidth=2.0,
        DrawStyle="Solid",
        Visibility=False,
    )
    props.update(overrides)
    return _FakeView(**props)


# ---------------------------------------------------------------------------
# (a) headless creation persists the geometry style App-side
# ---------------------------------------------------------------------------


class TestApplyStylePersistsAppMetadata:
    def test_headless_object_without_view_gets_app_style_properties(self):
        obj = _FakeObject("Batch_1", view=None)
        opts = core.ImportOptions()

        core._apply_style(obj, (0.0, 0.0, 0.0), None, 0.6, [6.0, 6.0], opts)

        assert obj.PDFStrokeRGB == "0,0,0"
        assert obj.PDFFillRGB == ""
        assert obj.PDFLineWidthPt == pytest.approx(0.6)
        assert obj.PDFDashPattern == "6,6"
        for name in ("PDFStrokeRGB", "PDFFillRGB", "PDFLineWidthPt", "PDFDashPattern"):
            assert name in obj.PropertiesList

    def test_fill_and_dashdot_round_trip_through_metadata(self):
        obj = _FakeObject("Face", view=None)
        core._apply_style(
            obj, (0.2, 0.4, 0.6), (1.0, 0.0, 0.0), 1.32,
            [19.2, 4.8, 2.4, 4.8], core.ImportOptions(),
        )

        assert obj.PDFStrokeRGB == "0.2,0.4,0.6"
        assert obj.PDFFillRGB == "1,0,0"
        assert obj.PDFLineWidthPt == pytest.approx(1.32)
        assert restore.parse_rgb(obj.PDFStrokeRGB) == pytest.approx((0.2, 0.4, 0.6))
        assert restore.parse_rgb(obj.PDFFillRGB) == pytest.approx((1.0, 0.0, 0.0))
        assert restore.parse_dashes(obj.PDFDashPattern) == pytest.approx(
            [19.2, 4.8, 2.4, 4.8]
        )

    def test_disabled_linewidth_and_dash_options_persist_the_effective_intent(self):
        obj = _FakeObject("Batch_2", view=None)
        opts = core.ImportOptions(assign_linewidth=False, map_dashes=False)

        core._apply_style(obj, (0.0, 0.0, 0.0), None, 0.84, [6.0, 6.0], opts)

        # 0.0 means "no source width -> 1 px floor" and "" means "no dash".
        assert obj.PDFLineWidthPt == 0.0
        assert obj.PDFDashPattern == ""

    def test_gui_object_gets_both_view_style_and_app_metadata(self):
        view = _geometry_view()
        obj = _FakeObject("Batch_3", view=view)
        opts = core.ImportOptions()

        core._apply_style(obj, (0.0, 0.0, 0.0), None, 0.6, [6.0, 6.0], opts)

        assert obj.PDFLineWidthPt == pytest.approx(0.6)
        assert view.LineColor == (0.0, 0.0, 0.0)
        assert view.LineWidth == 1.0  # 0.6 pt * 96/72 = 0.8 -> 1 px floor
        assert view.DrawStyle == "Dashed"

    def test_report_payload_is_honest_about_headless_versus_gui(self):
        headless = core.ImportOptions()
        core._apply_style(_FakeObject("a", view=None), (0, 0, 0), None, 0.6, None, headless)
        core._apply_style(_FakeObject("b", view=None), (0, 0, 0), None, 0.6, None, headless)
        payload = core._geometry_style_report_payload(headless)
        assert payload["app_metadata_objects"] == 2
        assert payload["view_styled_objects"] == 0
        assert payload["style_verification"] == "headless_app_metadata"
        assert payload["view_style_verified"] is False

        gui = core.ImportOptions()
        core._apply_style(
            _FakeObject("c", view=_geometry_view()), (0, 0, 0), None, 0.6, None, gui
        )
        payload = core._geometry_style_report_payload(gui)
        assert payload["app_metadata_objects"] == 1
        assert payload["view_styled_objects"] == 1
        assert payload["style_verification"] == "gui_view_and_app_metadata"
        assert payload["view_style_verified"] is True

        assert core._geometry_style_report_payload(core.ImportOptions()) == {
            "app_metadata_objects": 0,
            "view_styled_objects": 0,
            "style_verification": "no_geometry_style_applied",
            "view_style_verified": False,
        }

    def test_persistence_never_raises_on_hosts_without_addproperty(self):
        class Bare:
            ViewObject = None

        core._apply_style(Bare(), (0, 0, 0), None, 0.6, [6, 6], core.ImportOptions())


# ---------------------------------------------------------------------------
# (b) restore hook applies visibility + text style + geometry style + arrow None
# ---------------------------------------------------------------------------


def _build_document():
    text_view = _text_view()
    text = _FakeObject(
        "PDF_Text", "App::FeaturePython", view=text_view,
        PDFSourceItemId="p1:b0:l0:s0", PDFRepresentation="text",
        PDFTextFontName="RomanT", PDFTextFontSize=11.52,
        PDFTextJustification="Left", PDFTextColorRGB="0.2,0.4,0.6",
    )
    label_view = _label_view()
    label = _FakeObject(
        "PDF_Label", "App::FeaturePython", view=label_view,
        PDFSourceItemId="p1:b0:l1:s0", PDFRepresentation="labels",
        PDFTextFontName="Arial", PDFTextFontSize=2.02,
        PDFTextJustification="Left", PDFTextColorRGB="0,0,0",
    )
    batch_view = _geometry_view()
    batch = _FakeObject(
        "Batch_1", view=batch_view,
        PDFStrokeRGB="0,0,0", PDFFillRGB="", PDFLineWidthPt=0.6, PDFDashPattern="6,6",
    )
    face_view = _geometry_view()
    face = _FakeObject(
        "Face", view=face_view,
        PDFStrokeRGB="0,0,0", PDFFillRGB="0,0,0", PDFLineWidthPt=1.32,
        PDFDashPattern="19.2,4.8,2.4,4.8",
    )
    support_view = _geometry_view()
    support = _FakeObject(
        "ShapeString", "Part::Part2DObjectPython", view=support_view,
        PDFSourceItemId="p1:b0:l2:s0", PDFRepresentation="3d_text",
        PDFTextFontName="Arial", PDFTextFontSize=4.0,
        PDFTextJustification="Left", PDFTextColorRGB="0,0,0",
    )
    support.Visibility = False  # importer hides 3D-text supports App-side
    unrelated_view = _geometry_view()
    unrelated = _FakeObject("UserSketch", view=unrelated_view)
    group_view = _FakeView(Visibility=False)
    group = _FakeObject("PDF_Page_1", "App::DocumentObjectGroup", view=group_view)
    group.Group = [text, label, batch, face, support]
    for child in group.Group:
        child.InList = [group]
    doc = _FakeDocument([group, text, label, batch, face, support, unrelated])
    return doc, {
        "text": text, "label": label, "batch": batch, "face": face,
        "support": support, "unrelated": unrelated, "group": group,
    }


class TestRestoreDocumentStyles:
    def test_restores_visibility_text_style_geometry_style_and_arrow(self):
        doc, objs = _build_document()

        summary = restore.restore_document_styles(doc)

        # Visibility follows the App property: shown objects come back, the
        # deliberately hidden 3D-text support stays hidden.
        assert objs["text"].ViewObject.Visibility is True
        assert objs["label"].ViewObject.Visibility is True
        assert objs["batch"].ViewObject.Visibility is True
        assert objs["face"].ViewObject.Visibility is True
        assert objs["group"].ViewObject.Visibility is True
        assert objs["support"].ViewObject.Visibility is False
        # Draft Text: size / font / justification / colour from App metadata.
        tv = objs["text"].ViewObject
        assert tv.FontSize == pytest.approx(11.52)
        assert tv.FontName == "RomanT"
        assert tv.Justification == "Left"
        assert tv.TextColor == pytest.approx((0.2, 0.4, 0.6))
        # Draft Label: same, plus no arrow marker and no leader.
        lv = objs["label"].ViewObject
        assert lv.FontSize == pytest.approx(2.02)
        assert lv.FontName == "Arial"
        assert lv.ArrowTypeStart == "None"
        assert lv.Line is False
        # Already black in the fake view: compare-then-set leaves it untouched.
        assert tuple(lv.TextColor[:3]) == pytest.approx((0.0, 0.0, 0.0))
        assert ("TextColor", (0.0, 0.0, 0.0)) not in lv.writes
        # Geometry: the _apply_style rule (96/72 px, 1 px floor, dash mapping).
        bv = objs["batch"].ViewObject
        assert bv.LineColor == (0.0, 0.0, 0.0)
        assert bv.PointColor == (0.0, 0.0, 0.0)
        assert bv.LineWidth == 1.0
        assert bv.DrawStyle == "Dashed"
        fv = objs["face"].ViewObject
        assert fv.ShapeColor == (0.0, 0.0, 0.0)
        assert fv.LineWidth == pytest.approx(1.8)
        assert fv.DrawStyle == "Dashdot"
        # 3D text carries its source colour onto the shape.
        assert objs["support"].ViewObject.ShapeColor == pytest.approx((0.0, 0.0, 0.0))
        # Objects the importer did not create are never touched.
        uv = objs["unrelated"].ViewObject
        assert uv.writes == []
        assert uv.Visibility is False
        assert uv.LineWidth == 2.0

        assert summary["errors"] == []
        assert summary["objects_scanned"] == 7
        assert summary["text_restored"] == 2
        assert summary["geometry_restored"] == 2
        assert summary["visibility_restored"] >= 5

    def test_second_call_is_idempotent_and_writes_nothing_new_to_text_views(self):
        doc, objs = _build_document()
        restore.restore_document_styles(doc)
        snapshot = {
            name: dict(vars(obj.ViewObject))
            for name, obj in objs.items()
        }
        for obj in objs.values():
            obj.ViewObject.writes.clear()

        second = restore.restore_document_styles(doc)

        assert second["errors"] == []
        for name, obj in objs.items():
            after = dict(vars(obj.ViewObject))
            after.pop("writes", None)
            before = dict(snapshot[name])
            before.pop("writes", None)
            assert after == before, name
        # Text, label, group and visibility paths compare before writing.
        assert objs["text"].ViewObject.writes == []
        assert objs["label"].ViewObject.writes == []
        assert objs["group"].ViewObject.writes == []
        assert objs["unrelated"].ViewObject.writes == []

    def test_partial_or_corrupt_metadata_never_raises_and_does_not_block_others(self):
        good_view = _geometry_view()
        good = _FakeObject(
            "Batch_ok", view=good_view,
            PDFStrokeRGB="0,0,0", PDFFillRGB="", PDFLineWidthPt=0.6, PDFDashPattern="",
        )
        broken_geometry = _FakeObject(
            "Batch_bad", view=_geometry_view(),
            PDFStrokeRGB="not,a,colour", PDFFillRGB=None, PDFLineWidthPt="x",
            PDFDashPattern="6,,x",
        )
        no_view = _FakeObject(
            "Batch_headless", view=None,
            PDFStrokeRGB="0,0,0", PDFFillRGB="", PDFLineWidthPt=0.6, PDFDashPattern="",
        )
        text_missing_size = _FakeObject(
            "PDF_Text_partial", "App::FeaturePython", view=_text_view(),
            PDFRepresentation="text", PDFTextFontName="Arial",
        )
        text_bad_size = _FakeObject(
            "PDF_Text_bad", "App::FeaturePython", view=_text_view(),
            PDFRepresentation="text", PDFTextFontName="Arial",
            PDFTextFontSize="tall", PDFTextColorRGB="0.5",
        )

        class ExplodingView:
            def __getattr__(self, name):
                raise RuntimeError("view provider is gone")

            def __setattr__(self, name, value):
                raise RuntimeError("view provider is gone")

        exploding = _FakeObject(
            "Batch_exploding", view=ExplodingView(),
            PDFStrokeRGB="0,0,0", PDFFillRGB="", PDFLineWidthPt=0.6, PDFDashPattern="",
        )
        doc = _FakeDocument([
            broken_geometry, no_view, text_missing_size, text_bad_size, exploding, good,
        ])

        summary = restore.restore_document_styles(doc)

        assert good_view.LineColor == (0.0, 0.0, 0.0)
        assert good_view.LineWidth == 1.0
        assert good_view.Visibility is True
        # Partial text metadata: the font that IS present is applied, the rest skipped.
        assert text_missing_size.ViewObject.FontName == "Arial"
        assert text_missing_size.ViewObject.FontSize == 3.5
        assert summary["objects_scanned"] == 6
        assert isinstance(summary["errors"], list)

    def test_font_file_path_is_preferred_over_font_name_when_it_exists(self, tmp_path):
        font_file = tmp_path / "romant.ttf"
        font_file.write_bytes(b"\0")
        view = _text_view()
        obj = _FakeObject(
            "PDF_Text", "App::FeaturePython", view=view,
            PDFRepresentation="text", PDFTextFontName="RomanT",
            PDFTextFontSize=5.0, PDFFontFile=str(font_file),
        )
        restore.restore_document_styles(_FakeDocument([obj]))
        assert view.FontName == str(font_file)

        missing = _FakeObject(
            "PDF_Text2", "App::FeaturePython", view=_text_view(),
            PDFRepresentation="text", PDFTextFontName="RomanT",
            PDFTextFontSize=5.0, PDFFontFile=str(tmp_path / "gone.ttf"),
        )
        restore.restore_document_styles(_FakeDocument([missing]))
        assert missing.ViewObject.FontName == "RomanT"

    def test_geometry_style_uses_the_importer_apply_style_rule(self):
        # The restore path must reuse PDFImporterCore._apply_style rather than
        # carry a second copy of the px / floor / dash mapping.
        assert restore._geometry_style_applier() is core._apply_style


# ---------------------------------------------------------------------------
# Observer wiring: deferred until the restore is finished, registered once.
# ---------------------------------------------------------------------------


class TestObserver:
    def test_created_document_is_restored_after_restore_finishes(self):
        doc, objs = _build_document()
        doc.Restoring = True
        scheduled = []

        def schedule(callback, delay_ms):
            scheduled.append((callback, delay_ms))
            return True

        restored = []
        observer = restore.PDFStyleRestoreObserver(
            schedule=schedule,
            restore=lambda d: restored.append(d) or {"errors": []},
        )

        observer.slotCreatedDocument(doc)
        assert len(scheduled) == 1 and restored == []
        # Still restoring: the callback re-arms instead of touching the doc.
        scheduled[0][0]()
        assert restored == [] and len(scheduled) == 2
        doc.Restoring = False
        scheduled[1][0]()
        assert restored == [doc]
        assert len(scheduled) == 2

    def test_restore_gives_up_after_bounded_retries_without_raising(self):
        doc = _FakeDocument([], restoring=True)
        scheduled = []
        observer = restore.PDFStyleRestoreObserver(
            schedule=lambda cb, ms: scheduled.append(cb) or True,
            restore=lambda d: pytest.fail("must not restore a document still restoring"),
            max_retries=3,
        )
        observer.slotCreatedDocument(doc)
        while scheduled:
            scheduled.pop(0)()
        # bounded: initial + 3 retries, then silence

    def test_slot_never_raises_when_scheduling_is_unavailable(self):
        observer = restore.PDFStyleRestoreObserver(schedule=lambda cb, ms: False)
        observer.slotCreatedDocument(_FakeDocument([]))
        observer.slotDeletedDocument(_FakeDocument([]))

    def test_register_document_observer_is_idempotent(self):
        class FakeFreeCAD:
            def __init__(self):
                self.added = []
                self.removed = []
                self.GuiUp = True

            def addDocumentObserver(self, obs):
                self.added.append(obs)

            def removeDocumentObserver(self, obs):
                self.removed.append(obs)

        fake = FakeFreeCAD()
        restore.unregister_document_observer(freecad_module=fake)
        first = restore.register_document_observer(freecad_module=fake)
        second = restore.register_document_observer(freecad_module=fake)
        try:
            assert first is second
            assert fake.added == [first]
        finally:
            restore.unregister_document_observer(freecad_module=fake)
        assert fake.removed == [first]


class TestInitGuiRegistersAtModuleLevel:
    def test_initgui_registers_the_restore_observer_outside_the_workbench_class(self):
        source = (MOD_ROOT / "InitGui.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        top_level_calls = set()
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                continue
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    func = call.func
                    if isinstance(func, ast.Attribute):
                        top_level_calls.add(func.attr)
                    elif isinstance(func, ast.Name):
                        top_level_calls.add(func.id)
        assert "register_document_observer" in top_level_calls, (
            "InitGui.py must register PDFStyleRestore's document observer at "
            "module level so headless-saved documents regain their style on GUI "
            "open even when the workbench is never activated"
        )
        assert "PDFStyleRestore" in source


class TestLabelGuiCreationHasNoArrow:
    def test_gui_branch_source_sets_arrow_none_for_labels(self):
        # Behavioural check lives in test_freecad_production_contract; this pins
        # that the creation path and the restore path agree on the contract.
        assert restore.LABEL_ARROW_TYPE == "None"
        assert core.LABEL_ARROW_TYPE == restore.LABEL_ARROW_TYPE
