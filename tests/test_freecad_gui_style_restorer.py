from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import PDFGuiStyleRestorer as restorer  # noqa: E402


class _TextView:
    def __init__(self):
        self.Visibility = False
        self.FontSize = 3.0
        self.FontName = "Wrong Font"
        self.Justification = "Right"
        self.TextColor = (1.0, 0.0, 0.0)
        self.DisplayMode = "Screen"
        self.ScaleMultiplier = 2.0
        self.LineSpacing = 3.0


class _LabelView(_TextView):
    def __init__(self):
        super().__init__()
        self.Line = True
        self.Frame = "Rectangle"
        self.ArrowTypeStart = "Dot"
        self.TextAlignment = "Top"
        self.MaxChars = 12


class _ExpiringTextView(_TextView):
    def __init__(self):
        super().__init__()
        self._visibility_reads = 0

    def __getattribute__(self, name):
        if name == "Visibility":
            reads = object.__getattribute__(self, "_visibility_reads")
            object.__setattr__(self, "_visibility_reads", reads + 1)
            if reads >= 2:
                raise RuntimeError("view provider was deleted")
        return object.__getattribute__(self, name)


class _RejectingColorView(_TextView):
    def __init__(self):
        self._text_color = (1.0, 0.0, 0.0)
        super().__init__()

    @property
    def TextColor(self):
        return self._text_color

    @TextColor.setter
    def TextColor(self, value):
        if tuple(value) == pytest.approx((0.2, 0.4, 0.6)):
            raise RuntimeError("host rejected color")
        self._text_color = value


class _HostObject:
    def __init__(
        self,
        representation="text",
        proxy_type="Text",
        view=None,
        *,
        name="PDFText",
        document_name="PDFDocument",
    ):
        self.Name = name
        self.Document = SimpleNamespace(Name=document_name)
        self.TypeId = "App::FeaturePython"
        self.Proxy = SimpleNamespace(Type=proxy_type)
        self.PDFSourceItemId = "p1:b0:l0:s0"
        self.PDFRepresentation = representation
        self.PDFTextFontName = "Arial"
        self.PDFTextFontSize = 12.5
        self.PDFTextJustification = "Left"
        self.PDFTextColorRGB = "0.2,0.4,0.6"
        self.PDFTextVisibility = True
        self.PropertiesList = [
            "PDFSourceItemId",
            "PDFRepresentation",
            "PDFTextFontName",
            "PDFTextFontSize",
            "PDFTextJustification",
            "PDFTextColorRGB",
            "PDFTextVisibility",
        ]
        self.ViewObject = view if view is not None else _TextView()
        if representation == "labels":
            self.Points = [object(), object()]


def _view_state(view):
    return dict(vars(view))


def test_restores_exact_requested_style_on_importer_owned_text():
    host = _HostObject()

    assert restorer.restore_importer_text_object(host) is True

    assert host.ViewObject.Visibility is True
    assert host.ViewObject.FontSize == pytest.approx(12.5)
    assert host.ViewObject.FontName == "Arial"
    assert host.ViewObject.Justification == "Left"
    assert host.ViewObject.TextColor == pytest.approx((0.2, 0.4, 0.6))
    assert host.ViewObject.DisplayMode == "World"
    assert host.ViewObject.ScaleMultiplier == pytest.approx(1.0)
    assert host.ViewObject.LineSpacing == pytest.approx(1.0)


def test_restores_label_style_without_leader_frame_or_target_marker():
    host = _HostObject(
        representation="labels",
        proxy_type="Label",
        view=_LabelView(),
    )

    assert restorer.restore_importer_text_object(host) is True

    assert host.ViewObject.Visibility is True
    assert host.ViewObject.FontSize == pytest.approx(12.5)
    assert host.ViewObject.FontName == "Arial"
    assert host.ViewObject.Justification == "Left"
    assert host.ViewObject.TextColor == pytest.approx((0.2, 0.4, 0.6))
    assert host.ViewObject.Line is False
    assert host.ViewObject.Frame == "None"
    assert host.ViewObject.ArrowTypeStart == "None"
    assert host.ViewObject.TextAlignment == "Bottom"
    assert host.ViewObject.MaxChars == 0
    assert host.Points == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda host: host.PropertiesList.remove("PDFSourceItemId"),
        lambda host: setattr(host, "PDFRepresentation", "geometry"),
        lambda host: setattr(host.Proxy, "Type", "Label"),
        lambda host: setattr(host, "PDFTextFontSize", float("nan")),
        lambda host: setattr(host, "PDFTextColorRGB", "1.2,0.4,0.6"),
    ],
)
def test_ignores_unowned_mismatched_or_malformed_objects_without_partial_changes(mutate):
    host = _HostObject()
    mutate(host)
    before = _view_state(host.ViewObject)

    assert restorer.restore_importer_text_object(host) is False
    assert _view_state(host.ViewObject) == before


def test_gui_observer_restores_created_object_and_scans_activated_document():
    created = _HostObject(name="Created", document_name="CreatedDoc")
    activated = _HostObject(
        representation="labels",
        proxy_type="Label",
        view=_LabelView(),
        name="Activated",
        document_name="ActivatedDoc",
    )
    observer = restorer.PDFGuiStyleRestorer()

    observer.slotCreatedObject(SimpleNamespace(Object=created))
    observer.slotActivateDocument(
        SimpleNamespace(Document=SimpleNamespace(Name="Activated", Objects=[activated]))
    )

    assert created.ViewObject.Visibility is True
    assert created.ViewObject.FontName == "Arial"
    assert activated.ViewObject.Visibility is True
    assert activated.ViewObject.Line is False
    assert activated.ViewObject.Frame == "None"
    assert activated.ViewObject.ArrowTypeStart == "None"


def test_observer_applies_once_and_does_not_overwrite_later_user_edits():
    host = _HostObject(name="Owned", document_name="Document")
    document = SimpleNamespace(Name="Document", Objects=[host])
    gui_document = SimpleNamespace(Document=document)
    observer = restorer.PDFGuiStyleRestorer()

    observer.slotActivateDocument(gui_document)
    assert host.ViewObject.FontName == "Arial"

    host.ViewObject.FontName = "User Font"
    observer.slotActivateDocument(gui_document)

    assert host.ViewObject.FontName == "User Font"


def test_headless_restore_and_registration_are_safe_no_ops(monkeypatch):
    host = _HostObject()
    host.ViewObject = None
    gui = SimpleNamespace(addDocumentObserver=lambda _observer: pytest.fail("GUI used"))
    monkeypatch.setattr(restorer, "_GUI_STYLE_RESTORER", None)

    assert restorer.restore_importer_text_object(host) is False
    assert restorer.register_gui_style_restorer(
        freecad=SimpleNamespace(GuiUp=False), gui=gui
    ) is None


def test_deleted_view_provider_during_readback_fails_closed_without_escaping():
    host = _HostObject(view=_ExpiringTextView())

    assert restorer.restore_importer_text_object(host) is False


def test_failed_multi_property_restore_rolls_back_earlier_view_changes():
    host = _HostObject(view=_RejectingColorView())
    before = _view_state(host.ViewObject)

    assert restorer.restore_importer_text_object(host) is False
    assert _view_state(host.ViewObject) == before


def test_owned_legacy_object_without_visibility_metadata_restores_visible():
    host = _HostObject()
    host.PropertiesList.remove("PDFTextVisibility")
    del host.PDFTextVisibility

    assert restorer.restore_importer_text_object(host) is True
    assert host.ViewObject.Visibility is True


def test_gui_observer_registration_is_idempotent(monkeypatch):
    observers = []
    gui = SimpleNamespace(addDocumentObserver=observers.append)
    freecad = SimpleNamespace(GuiUp=True)
    monkeypatch.setattr(restorer, "_GUI_STYLE_RESTORER", None)

    first = restorer.register_gui_style_restorer(freecad=freecad, gui=gui)
    second = restorer.register_gui_style_restorer(freecad=freecad, gui=gui)

    assert first is second
    assert observers == [first]


def test_init_gui_installs_restorer_before_lazy_workbench_activation():
    source = (REPO_ROOT / "PDFVectorImporter" / "InitGui.py").read_text(
        encoding="utf-8"
    )
    registration = source.index("register_gui_style_restorer()")

    assert registration > source.index("FreeCADGui.addWorkbench")
    assert registration > source.rindex("def Activated")
