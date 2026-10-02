"""The final batch view must frame imported sheets, not distant user objects."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "PDFVectorImporter" / "src"))
import PDFImporterCore as core  # noqa: E402


def _host(monkeypatch, *, selected_fit_error=False):
    events = []
    old_selection = object()
    selection = [old_selection]
    view = SimpleNamespace(
        setCameraType=lambda value: events.append(("camera", value)),
        viewTop=lambda: events.append(("top",)),
        fitAll=lambda: events.append(("all",)),
    )

    def fit_selected(message):
        events.append((message, tuple(selection)))
        if selected_fit_error:
            raise RuntimeError("selection fit unavailable")

    gui = SimpleNamespace(
        updateGui=lambda: None,
        ActiveDocument=SimpleNamespace(ActiveView=view),
        SendMsgToActiveView=fit_selected,
        Selection=SimpleNamespace(
            getSelection=lambda: list(selection),
            clearSelection=lambda: selection.clear(),
            addSelection=lambda obj: selection.append(obj),
        ),
    )
    monkeypatch.setitem(sys.modules, "FreeCADGui", gui)
    return events, selection, old_selection


def _page(name):
    return SimpleNamespace(
        Name=name,
        isDerivedFrom=lambda kind: kind == "App::DocumentObjectGroup",
    )


def test_batch_fits_all_imported_pages_after_top_orthographic(monkeypatch):
    events, selection, old_selection = _host(monkeypatch)
    pages = [_page("PDF_Page_1"), _page("PDF_Page_2")]
    unrelated = SimpleNamespace(Name="ExistingRemoteBuilding")

    core._autofit_import_view(SimpleNamespace(Objects=pages + [unrelated]))

    assert events == [
        ("camera", "Orthographic"), ("top",), ("ViewSelection", tuple(pages)),
    ]
    assert selection == [old_selection]


@pytest.mark.parametrize("with_pages", [False, True])
def test_fallback_fit_remains_orthographic_and_restores_selection(monkeypatch, with_pages):
    events, selection, old_selection = _host(monkeypatch, selected_fit_error=True)
    core._autofit_import_view(SimpleNamespace(Objects=[_page("PDF_Page_1")] if with_pages else []))
    assert events[:2] == [("camera", "Orthographic"), ("top",)]
    assert events[-1] == ("all",)
    assert selection == [old_selection]


def test_incomplete_page_selection_uses_full_fit_instead_of_clipping_the_batch(monkeypatch):
    events, selection, old_selection = _host(monkeypatch)
    pages = [_page("PDF_Page_1"), _page("PDF_Page_2")]
    gui = sys.modules["FreeCADGui"]

    def select(obj):
        if obj is pages[1]:
            return  # Some host adapters report no exception when selection fails.
        selection.append(obj)

    gui.Selection.addSelection = select
    core._autofit_import_view(SimpleNamespace(Objects=pages))
    assert events == [("camera", "Orthographic"), ("top",), ("all",)]
    assert selection == [old_selection]


def _view_bounds(values, *, visible=True, children=()):
    box = SimpleNamespace(**dict(zip(
        ("XMin", "YMin", "ZMin", "XMax", "YMax", "ZMax"), values, strict=True
    )))
    return SimpleNamespace(
        ViewObject=SimpleNamespace(Visibility=visible, getBoundingBox=lambda: box),
        Group=list(children),
    )


@pytest.mark.parametrize("size", [(1600, 900), (600, 1200)])
def test_direct_fit_uses_every_visible_descendant_without_selection_limit(monkeypatch, size):
    events, selection, old_selection = _host(monkeypatch)
    captured = []

    class Coordinate:
        def __init__(self):
            self.values = []
            self.point = SimpleNamespace(setValues=self.set_values)

        def set_values(self, start, count, values):
            assert start == 0 and count == len(values)
            self.values = values

    class Node:
        def __init__(self):
            self.children = []

        def addChild(self, node):
            self.children.append(node)

    coin = SimpleNamespace(SoCoordinate3=Coordinate, SoPointSet=SimpleNamespace,
                           SoSeparator=Node, SbViewportRegion=lambda w, h: (w, h))
    monkeypatch.setitem(sys.modules, "pivy", SimpleNamespace(coin=coin))
    view = sys.modules["FreeCADGui"].ActiveDocument.ActiveView
    view.getSize = lambda: size
    view.getCameraNode = lambda: SimpleNamespace(
        viewAll=lambda node, viewport, slack: captured.append((node, viewport, slack))
    )
    # Native plain groups have an unset bounding box. The last child must not
    # disappear when the user's MaxViewSelections preference is smaller than301.
    children = [_view_bounds((0, i * 10, 0, 200, i * 10 + 5, 0)) for i in range(301)]
    children.append(_view_bounds((100000, 100000, 0, 100001, 100001, 1), visible=False))
    page = _view_bounds((1e38, 1e38, 1e38, -1e38, -1e38, -1e38), children=children)
    page.Name = "PDF_Page_1"
    page.isDerivedFrom = lambda kind: kind == "App::DocumentObjectGroup"
    # A group reference cycle must not prevent camera completion.
    children[0].Group = [page]
    unrelated = _view_bounds((1e6, 1e6, 0, 1e6 + 1, 1e6 + 1, 1))
    unrelated.Name = "UnrelatedBuilding"

    core._autofit_import_view(SimpleNamespace(Objects=[page, unrelated]))

    assert events == [("camera", "Orthographic"), ("top",)]
    assert selection == [old_selection]
    assert len(captured) == 1
    node, viewport, slack = captured[0]
    assert viewport == size
    assert slack == 1.1
    assert set(node.children[0].values) == {
        (x, y, 0.0) for x in (0.0, 200.0) for y in (0.0, 3005.0)
    }
    assert node.children[1].numPoints == 8


def test_unset_or_nonfinite_descendant_bounds_do_not_claim_a_fit():
    view = SimpleNamespace()
    roots = [_view_bounds((1e38, 1e38, 1e38, -1e38, -1e38, -1e38)),
             _view_bounds((0, 0, 0, float("inf"), 10, 0))]
    assert core._fit_import_descendant_bounds(view, roots) is False


def _paper(corners, origin=(0.0, 0.0)):
    import json

    return SimpleNamespace(
        Name="PDF_Paper",
        PDFPaperDisplayJSON=json.dumps({"corners_mm": corners}),
        Placement=SimpleNamespace(Base=SimpleNamespace(x=origin[0], y=origin[1])),
        isDerivedFrom=lambda kind: False,
    )


def _sheet_host(monkeypatch, size):
    """A view whose camera records the corner fit and the orthographic height."""
    events, selection, old_selection = _host(monkeypatch)
    fitted = []

    class Coordinate:
        def __init__(self):
            self.values = []
            self.point = SimpleNamespace(setValues=self.set_values)

        def set_values(self, start, count, values):
            assert start == 0 and count == len(values)
            self.values = list(values)

    class Node:
        def __init__(self):
            self.children = []

        def addChild(self, node):
            self.children.append(node)

    coin = SimpleNamespace(SoCoordinate3=Coordinate, SoPointSet=SimpleNamespace,
                           SoSeparator=Node, SbViewportRegion=lambda w, h: (w, h))
    monkeypatch.setitem(sys.modules, "pivy", SimpleNamespace(coin=coin))
    heights = []
    camera = SimpleNamespace(
        viewAll=lambda node, viewport, slack: fitted.append((node, viewport, slack)),
        height=SimpleNamespace(setValue=heights.append),
    )
    view = sys.modules["FreeCADGui"].ActiveDocument.ActiveView
    view.getSize = lambda: size
    view.getCameraNode = lambda: camera
    return events, selection, old_selection, fitted, heights


@pytest.mark.parametrize("size,expected_height", [
    # Letter portrait sheet, 215.9 x 279.4 mm.
    ((1600, 900), 279.4 * 1.05),            # wide window: the sheet height decides
    ((600, 1200), 215.9 * 1.05),            # tall window: Coin widens by 1/aspect
    ((900, 1000), 279.4 * 0.9 * 1.05),      # slightly tall window: the sheet height decides
])
def test_sheet_fit_places_the_camera_on_the_paper_without_a_document_fit(
        monkeypatch, size, expected_height):
    events, selection, old_selection, fitted, heights = _sheet_host(monkeypatch, size)
    corners = [(0, 0, 0), (215.9, 0, 0), (215.9, 279.4, 0), (0, 279.4, 0)]
    page = _page("PDF_Page_1")
    # An off-sheet stroke and an unrelated object far away must not set the zoom.
    stray = _view_bounds((-900.0, 100.0, 0.0, 215.9, 100.0, 0.0))
    stray.Name = "PDF_Stray"
    stray.isDerivedFrom = lambda kind: False
    unrelated = _view_bounds((1e6, 1e6, 0, 1e6 + 1, 1e6 + 1, 1))
    unrelated.Name = "ExistingRemoteBuilding"
    unrelated.isDerivedFrom = lambda kind: False

    def no_new_objects(*_args, **_kwargs):
        raise AssertionError("the view fit must not add a document object")

    doc = SimpleNamespace(Objects=[page, _paper(corners), stray, unrelated],
                          addObject=no_new_objects, removeObject=no_new_objects)
    core._autofit_import_view(doc)

    # The fit-all message is never sent: it would frame the stray stroke and
    # the unrelated object. The operator's selection is back as it was.
    assert not [event for event in events if event[0] in ("ViewFit", "all")]
    assert selection == [old_selection]
    node, viewport, slack = fitted[-1]
    assert viewport == size and slack == 1.0
    assert set(node.children[0].values) == {
        (x, y, 0.0) for x in (0.0, 215.9) for y in (0.0, 279.4)
    }
    assert node.children[1].numPoints == 4
    assert heights[-1] == pytest.approx(expected_height)


def test_sheet_fit_covers_every_placed_sheet_of_a_batch(monkeypatch):
    events, _selection, _old, fitted, heights = _sheet_host(monkeypatch, (1600, 900))
    letter = [(0, 0, 0), (215.9, 0, 0), (215.9, 279.4, 0), (0, 279.4, 0)]
    tabloid = [(0, 0, 0), (431.8, 0, 0), (431.8, 279.4, 0), (0, 279.4, 0)]
    doc = SimpleNamespace(Objects=[
        _page("PDF_Page_1"), _paper(letter), _page("PDF_Page_2"), _paper(tabloid, origin=(0.0, -335.28)),
    ])
    core._autofit_import_view(doc)
    node, _viewport, _slack = fitted[-1]
    assert set(node.children[0].values) == {
        (x, y, 0.0) for x in (0.0, 431.8) for y in (-335.28, 279.4)
    }
    # Both sheets, edge to edge: the stack is taller than the window is wide.
    assert heights[-1] == pytest.approx((279.4 + 335.28) * 1.05)
    assert not [event for event in events if event[0] == "ViewFit"]


def test_sheet_fit_reports_failure_instead_of_fitting_the_document(monkeypatch):
    # No Coin bindings: nothing may fall back to a whole-document fit when the
    # imported pages were already fitted by selection.
    events, selection, old_selection = _host(monkeypatch)
    monkeypatch.setitem(sys.modules, "pivy", None)
    corners = [(0, 0, 0), (215.9, 0, 0), (215.9, 279.4, 0), (0, 279.4, 0)]
    pages = [_page("PDF_Page_1")]
    core._autofit_import_view(SimpleNamespace(Objects=pages + [_paper(corners)]))
    assert events == [
        ("camera", "Orthographic"), ("top",), ("ViewSelection", tuple(pages)),
    ]
    assert selection == [old_selection]


@pytest.mark.parametrize("sheet", [
    None, (), (0, 0, 0), (0, 0, 0, 10), (10, 0, 0, 10), (0, 0, float("nan"), 10),
    (0, 0, float("inf"), 10), ("a", 0, 1, 1),
])
def test_sheet_fit_rejects_a_box_that_is_not_a_rectangle(sheet):
    assert core._fit_view_to_sheet(SimpleNamespace(), sheet) is False


@pytest.mark.parametrize("enabled", [True, False])
def test_view_animation_is_off_while_the_view_is_framed_and_restored_after(monkeypatch, enabled):
    events, _selection, _old = _host(monkeypatch)
    view = sys.modules["FreeCADGui"].ActiveDocument.ActiveView
    state = {"enabled": enabled}
    view.isAnimationEnabled = lambda: state["enabled"]

    def set_enabled(value):
        state["enabled"] = bool(value)
        events.append(("animation", bool(value)))

    view.setAnimationEnabled = set_enabled
    core._autofit_import_view(SimpleNamespace(Objects=[_page("PDF_Page_1")]))
    if enabled:
        # Off before the turn to the top view, back on after the last fit.
        assert events[0] == ("animation", False)
        assert events[1:3] == [("camera", "Orthographic"), ("top",)]
        assert events[-1] == ("animation", True)
    else:
        assert not [event for event in events if event[0] == "animation"]
    assert state["enabled"] is enabled


class _Pose:
    def __init__(self, value=None):
        self.value = value

    def getValue(self):
        return self.value

    def setValue(self, *args):
        self.value = args[0] if len(args) == 1 else args


class _StraightCamera:
    def __init__(self, aspect):
        self.aspectRatio = _Pose(aspect)
        self.position = _Pose(None)
        self.height = _Pose(None)
        self.focalDistance = _Pose(None)
        self.nearDistance = _Pose(None)
        self.farDistance = _Pose(None)


def test_capable_camera_uses_the_straight_on_helper(monkeypatch):
    """The GUI calls the sheet helper when the camera can hold a pose.

    viewTop() restores the navigation camera, so orthographic has to come
    after it. Hosts that cannot take that pose keep the older order.
    """
    import PDFVectorImporter.sheet_camera as sheet_camera

    events, selection, old_selection = _host(monkeypatch)
    view = sys.modules["FreeCADGui"].ActiveDocument.ActiveView
    camera = _StraightCamera(aspect=1.6)
    view.getCameraNode = lambda: camera
    corners = [(0, 0, 0), (431.8, 0, 0), (431.8, 279.4, 0), (0, 279.4, 0)]
    stray = SimpleNamespace(Name="PDF_Stray", isDerivedFrom=lambda kind: False)
    doc = SimpleNamespace(Objects=[_page("PDF_Page_1"), _paper(corners), stray])

    core._autofit_import_view(doc)

    assert events[:2] == [("top",), ("camera", "Orthographic")]
    assert "all" not in [event[0] for event in events]
    assert "ViewFit" not in [event[0] for event in events]
    assert "ViewSelection" not in [event[0] for event in events]
    assert selection == [old_selection]
    _cx, _cy, _z, height = sheet_camera.orthographic_sheet_frame(
        0, 0, 431.8, 279.4, aspect=1.6,
    )
    pos = camera.position.value
    assert abs(pos[0] - 215.9) < 1.0
    assert abs(pos[1] - 139.7) < 1.0
    assert pos[2] > 0.0
    assert camera.height.value == pytest.approx(height)


def test_sheet_without_a_pose_camera_keeps_orthographic_then_top(monkeypatch):
    """Call-order contract: a camera the helper cannot pose is not reordered."""
    events, _selection, _old = _host(monkeypatch)
    corners = [(0, 0, 0), (215.9, 0, 0), (215.9, 279.4, 0), (0, 279.4, 0)]
    doc = SimpleNamespace(Objects=[_page("PDF_Page_1"), _paper(corners)])
    core._autofit_import_view(doc)
    assert events[:2] == [("camera", "Orthographic"), ("top",)]
