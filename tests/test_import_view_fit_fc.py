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




def test_framing_by_the_sheet_leaves_the_selection_untouched(monkeypatch):
    # The camera is placed from the sheet corners. Clearing and re-adding the
    # selection would fire selection events and turn a selected face into a
    # selected object.
    events, _selection, _old, _fitted, heights = _sheet_host(monkeypatch, (1600, 900))
    gui = sys.modules["FreeCADGui"]

    def untouched(*_args):
        raise AssertionError("the selection must not be read or changed")

    gui.Selection = SimpleNamespace(getSelection=untouched, clearSelection=untouched,
                                    addSelection=untouched)
    corners = [(0, 0, 0), (215.9, 0, 0), (215.9, 279.4, 0), (0, 279.4, 0)]
    page = _view_bounds((0, 0, 0, 215.9, 279.4, 0))
    page.Name = "PDF_Page_1"
    page.isDerivedFrom = lambda kind: kind == "App::DocumentObjectGroup"
    core._autofit_import_view(SimpleNamespace(Objects=[page, _paper(corners)]))
    assert heights[-1] == pytest.approx(279.4 * 1.05)
    assert events == [("camera", "Orthographic"), ("top",)]


def _scan(page, centre, size):
    """A page delivered as one image: the plane is drawn centred on its placement."""
    return SimpleNamespace(
        Name="Page_%d_raster" % page, TypeId="Image::ImagePlane", PDFSourceItemId="p%d:page" % page,
        XSize=size[0], YSize=size[1],
        Placement=SimpleNamespace(Base=SimpleNamespace(x=centre[0], y=centre[1])),
        isDerivedFrom=lambda kind: False,
    )


def test_a_scanned_page_is_a_sheet_and_is_framed_edge_to_edge(monkeypatch):
    # A scan has no paper object; its full-page image is the sheet.
    events, _selection, _old, fitted, heights = _sheet_host(monkeypatch, (1600, 900))
    scan = _scan(1, (107.07, 140.76), (214.14, 281.52))
    core._autofit_import_view(SimpleNamespace(Objects=[_page("PDF_Page_1"), scan]))
    node, _viewport, slack = fitted[-1]
    assert slack == 1.0
    assert {tuple(round(value, 6) for value in corner) for corner in node.children[0].values} == {
        (x, y, 0.0) for x in (0.0, 214.14) for y in (0.0, 281.52)
    }
    assert heights[-1] == pytest.approx(281.52 * 1.05)
    assert not [event for event in events if event[0] in ("ViewFit", "all")]


@pytest.mark.parametrize("scan_page", [1, 3])
def test_a_scan_at_either_end_of_a_batch_is_inside_the_framed_box(scan_page):
    letter = [(0, 0, 0), (215.9, 0, 0), (215.9, 279.4, 0), (0, 279.4, 0)]
    offsets = {1: 0.0, 2: -335.28, 3: -670.56}
    objects = []
    for number in (1, 2, 3):
        objects.append(_page("PDF_Page_%d" % number))
        if number == scan_page:
            objects.append(_scan(number, (107.07, offsets[number] + 140.76), (214.14, 281.52)))
        else:
            objects.append(_paper(letter, origin=(0.0, offsets[number])))
    box = core._sheet_view_box(SimpleNamespace(Objects=objects))
    assert box[0] == pytest.approx(0.0) and box[2] == pytest.approx(215.9)
    assert box[1] == pytest.approx(-670.56)
    assert box[3] == pytest.approx(281.52 if scan_page == 1 else 279.4)


@pytest.mark.parametrize("change", [
    {"TypeId": "Part::Feature"},                 # not an image
    {"PDFSourceItemId": "p1:image:3"},           # an embedded picture, not the page
    {"PDFSourceItemId": ""},
    {"XSize": 0.0},
    {"YSize": float("nan")},
    {"Placement": None},
])
def test_only_a_full_page_image_counts_as_a_sheet(change):
    plane = _scan(1, (107.07, 140.76), (214.14, 281.52))
    for name, value in change.items():
        setattr(plane, name, value)
    assert core._sheet_view_box(SimpleNamespace(Objects=[plane])) is None


# ── FreeCAD's own fit after File > Open, File > Import and a drop ─────────

class _Loop:
    """Fake Qt: single-shot timers fired by the test, and the event-loop depth."""

    def __init__(self, monkeypatch, level=1):
        self.timers = []
        self.level = level
        self.modal = None
        thread = SimpleNamespace(loopLevel=lambda: self.level)
        qt_core = SimpleNamespace(
            QTimer=SimpleNamespace(singleShot=lambda delay, callback: self.timers.append((delay, callback))),
            QThread=SimpleNamespace(currentThread=lambda: thread),
        )
        qt_widgets = SimpleNamespace(QApplication=SimpleNamespace(activeModalWidget=lambda: self.modal))
        monkeypatch.setitem(sys.modules, "PySide6",
                            SimpleNamespace(QtCore=qt_core, QtWidgets=qt_widgets))
        monkeypatch.setattr(core, "_host_import_commands", [])
        monkeypatch.setattr(core, "_pending_view_reframes", {})

    def fire(self):
        delay, callback = self.timers.pop(0)
        callback()
        return delay


def _named_doc(name, objects):
    doc = SimpleNamespace(Name=name, Objects=objects)
    sys.modules["FreeCADGui"].ActiveDocument.Document = doc
    return doc


FRAMED = [("camera", "Orthographic"), ("top",)]


def test_sheets_are_framed_again_when_the_host_command_is_back_in_its_event_loop(monkeypatch):
    # FreeCAD sends "ViewFit" (fit the whole document) when Open, Import or a
    # drop returns. With view animation on that fit runs in an event loop of
    # its own. The sheet framing is applied once that loop is gone.
    events, selection, old_selection = _host(monkeypatch)
    loop = _Loop(monkeypatch, level=1)
    pages = [_page("PDF_Page_1")]
    doc = _named_doc("Sheet", pages)
    once = FRAMED + [("ViewSelection", tuple(pages))]

    with core.host_import_command():
        core._autofit_import_view(doc)
        assert events == once
        assert loop.timers == []                    # nothing is scheduled inside the command
    assert [delay for delay, _callback in loop.timers] == [0]

    # The host's animated fit: its event loop is one level deeper.
    loop.level = 2
    events.append(("ViewFit", ("host",)))
    assert loop.fire() == 0
    assert loop.fire() == core._VIEW_REFRAME_POLL_MS
    assert events == once + [("ViewFit", ("host",))]     # still waiting
    # Back in the loop the command was started from.
    loop.level = 1
    loop.fire()
    assert events == once + [("ViewFit", ("host",))] + once
    assert loop.timers == []                        # framed once, then nothing more
    assert selection == [old_selection]


def test_no_reframe_without_a_host_command(monkeypatch):
    # The toolbar command and scripts are not followed by a host fit.
    _host(monkeypatch)
    loop = _Loop(monkeypatch)
    core._autofit_import_view(_named_doc("Sheet", [_page("PDF_Page_1")]))
    assert loop.timers == []


@pytest.mark.parametrize("busy", ["dialog", "animation"])
def test_reframe_waits_for_an_open_dialog_and_a_running_view_animation(monkeypatch, busy):
    events, _selection, _old = _host(monkeypatch)
    loop = _Loop(monkeypatch)
    doc = _named_doc("Sheet", [_page("PDF_Page_1")])
    view = sys.modules["FreeCADGui"].ActiveDocument.ActiveView
    animating = {"on": busy == "animation"}
    view.isAnimating = lambda: animating["on"]
    if busy == "dialog":
        loop.modal = object()
    with core.host_import_command():
        core._autofit_import_view(doc)
    count = len(events)
    loop.fire()
    assert len(events) == count and len(loop.timers) == 1
    loop.modal = None
    animating["on"] = False
    loop.fire()
    assert len(events) > count and loop.timers == []


def test_without_a_known_loop_depth_the_camera_has_to_hold_still(monkeypatch):
    events, _selection, _old = _host(monkeypatch)
    loop = _Loop(monkeypatch)
    sys.modules["PySide6"].QtCore.QThread = None        # no loopLevel on this host
    doc = _named_doc("Sheet", [_page("PDF_Page_1")])
    camera = {"height": 293.37}
    view = sys.modules["FreeCADGui"].ActiveDocument.ActiveView
    vector = SimpleNamespace(getValue=lambda: (0.0, 0.0, 1.0))
    rotation = SimpleNamespace(getValue=lambda: (0.0, 0.0, 0.0, 1.0))
    view.getCameraNode = lambda: SimpleNamespace(
        position=SimpleNamespace(getValue=lambda: vector),
        orientation=SimpleNamespace(getValue=lambda: rotation),
        height=SimpleNamespace(getValue=lambda: camera["height"]),
    )
    with core.host_import_command():
        core._autofit_import_view(doc)
    count = len(events)
    for height in (293.37, 5000.0, 20000.0, 50010.78):   # the host's fit, step by step
        camera["height"] = height
        loop.fire()
        assert len(events) == count and len(loop.timers) == 1
    loop.fire()                                          # unchanged since the last look
    assert len(events) > count and loop.timers == []


def test_a_second_import_leaves_one_check_running_and_frames_once(monkeypatch):
    events, _selection, _old = _host(monkeypatch)
    loop = _Loop(monkeypatch)
    pages = [_page("PDF_Page_1")]
    doc = _named_doc("Sheet", pages)
    once = FRAMED + [("ViewSelection", tuple(pages))]
    with core.host_import_command():
        core._autofit_import_view(doc)
    with core.host_import_command():
        core._autofit_import_view(doc)
    count = len(events)
    assert len(loop.timers) == 2
    loop.fire()                                          # the older check has been replaced
    assert len(events) == count and len(loop.timers) == 1
    loop.fire()
    assert events[count:] == once and loop.timers == []


def test_the_check_waits_while_the_command_imports_its_next_file(monkeypatch):
    # File > Open with several files: FreeCAD opens them one after the other
    # and fits after each. Nothing is framed in between.
    events, _selection, _old = _host(monkeypatch)
    loop = _Loop(monkeypatch)
    doc = _named_doc("Sheet", [_page("PDF_Page_1")])
    with core.host_import_command():
        core._autofit_import_view(doc)
    count = len(events)
    with core.host_import_command():
        loop.fire()                                      # an event pump inside the next import
        assert len(events) == count and len(loop.timers) == 1
    loop.fire()
    assert len(events) > count and loop.timers == []


def test_every_file_of_one_command_is_framed_in_its_own_view(monkeypatch):
    _host(monkeypatch)
    loop = _Loop(monkeypatch)
    gui = sys.modules["FreeCADGui"]
    framed = []
    documents = {}

    def gui_document(name):
        view = SimpleNamespace(
            setCameraType=lambda value, name=name: framed.append((name, value)),
            viewTop=lambda: None, fitAll=lambda: None,
        )
        doc = SimpleNamespace(Name=name, Objects=[])
        documents[name] = SimpleNamespace(Document=doc, ActiveView=view)
        return doc

    first, second, third = (gui_document(name) for name in ("First", "Second", "Third"))
    gui.getDocument = lambda name: documents[name]
    for doc in (first, second, third):
        gui.ActiveDocument = documents[doc.Name]
        with core.host_import_command():
            core._autofit_import_view(doc)
    assert framed == [("First", "Orthographic"), ("Second", "Orthographic"), ("Third", "Orthographic")]
    del documents["Second"]                              # closed before the command returned
    del framed[:]
    while loop.timers:
        loop.fire()
    # The last document is the active one; the first is framed in its own view all the same.
    assert framed == [("First", "Orthographic"), ("Third", "Orthographic")]
    assert core._pending_view_reframes == {}


@pytest.mark.parametrize("change", ["closed", "other_document"])
def test_reframe_leaves_another_document_alone(monkeypatch, change):
    events, _selection, _old = _host(monkeypatch)
    loop = _Loop(monkeypatch)
    doc = _named_doc("Sheet", [_page("PDF_Page_1")])
    with core.host_import_command():
        core._autofit_import_view(doc)
    count = len(events)
    gui = sys.modules["FreeCADGui"]
    if change == "closed":
        gui.ActiveDocument = None
    else:
        gui.ActiveDocument.Document = SimpleNamespace(Name="Other", Objects=[])
    loop.fire()
    assert len(events) == count
    assert loop.timers == []


def test_reframe_gives_up_after_its_last_check(monkeypatch):
    _host(monkeypatch)
    loop = _Loop(monkeypatch)
    monkeypatch.setattr(core, "_VIEW_REFRAME_MAX_POLLS", 3)
    doc = _named_doc("Sheet", [_page("PDF_Page_1")])
    with core.host_import_command():
        core._autofit_import_view(doc)
    loop.level = 2                                       # never returns to the base loop
    fired = 0
    while loop.timers:
        loop.fire()
        fired += 1
    assert fired == 3


def test_a_failed_or_cancelled_host_import_schedules_nothing(monkeypatch):
    _host(monkeypatch)
    loop = _Loop(monkeypatch)
    with pytest.raises(RuntimeError):
        with core.host_import_command():
            raise RuntimeError("import failed before any view was framed")
    with core.host_import_command():
        pass
    assert loop.timers == []
    assert core._host_import_commands == []


def test_nothing_is_scheduled_without_a_view_or_without_qt(monkeypatch):
    _host(monkeypatch)
    loop = _Loop(monkeypatch)
    sys.modules["FreeCADGui"].ActiveDocument = None
    with core.host_import_command():
        core._autofit_import_view(SimpleNamespace(Name="Sheet", Objects=[]))
    assert loop.timers == []

    _host(monkeypatch)
    for package in ("PySide6", "PySide2", "PySide"):
        monkeypatch.setitem(sys.modules, package, None)
    doc = _named_doc("Sheet", [_page("PDF_Page_1")])
    with core.host_import_command():
        core._autofit_import_view(doc)                   # must not raise


def test_no_view_preference_is_read_or_written():
    # The host's automatic fit is a user preference. It stays as stored: the
    # framing code and the handler never open the parameter store.
    import ast
    import inspect

    for function in (core._HostImportCommand, core._reframe_after_host_fit, core._autofit_import_view,
                     core._frame_import_view, core._fit_view_to_sheet, core._event_loop_level):
        text = inspect.getsource(function)
        assert not [word for word in ("ParamGet", "SetBool", "RemBool", "GetBool") if word in text]
    handler = (REPO / "PDFVectorImporter" / "PDFImportHandler.py").read_text(encoding="utf-8")
    names = {node.attr for node in ast.walk(ast.parse(handler)) if isinstance(node, ast.Attribute)}
    assert not names & {"ParamGet", "SetBool", "RemBool", "GetBool"}


def _load_handler(monkeypatch, gui_up=True):
    """PDFImportHandler with a fake FreeCAD, bound to this test's core module."""
    import importlib.util

    freecad = SimpleNamespace(
        GuiUp=gui_up,
        getUserAppDataDir=lambda: str(REPO / "no-such-profile"),
        getResourceDir=lambda: str(REPO / "no-such-resources"),
        Console=SimpleNamespace(PrintMessage=lambda _t: None, PrintError=lambda _t: None,
                                PrintWarning=lambda _t: None),
        newDocument=lambda name: SimpleNamespace(Name=name),
        setActiveDocument=lambda _name: None,
        getDocument=lambda name: SimpleNamespace(Name=name),
    )
    monkeypatch.setitem(sys.modules, "FreeCAD", freecad)
    package = SimpleNamespace(src=SimpleNamespace(PDFImporterCore=core))
    monkeypatch.setitem(sys.modules, "PDFVectorImporter", package)
    monkeypatch.setitem(sys.modules, "PDFVectorImporter.src", package.src)
    monkeypatch.setitem(sys.modules, "PDFVectorImporter.src.PDFImporterCore", core)
    spec = importlib.util.spec_from_file_location(
        "PDFImportHandler_under_test", REPO / "PDFVectorImporter" / "PDFImportHandler.py")
    handler = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(handler)
    monkeypatch.setattr(handler, "_check_fitz", lambda: True)
    return handler


@pytest.mark.parametrize("entry", ["open", "insert"])
def test_the_host_entry_points_run_the_import_inside_the_command_scope(monkeypatch, entry):
    _host(monkeypatch)
    loop = _Loop(monkeypatch)
    handler = _load_handler(monkeypatch)
    doc = _named_doc("Sheet", [_page("PDF_Page_1")])
    seen = []

    def fake_import(filename):
        seen.append((filename, len(core._host_import_commands)))
        core._autofit_import_view(doc)
        assert loop.timers == []

    monkeypatch.setattr(handler, "_import_with_dialog", fake_import)
    if entry == "open":
        handler.open("D042.pdf")
    else:
        handler.insert("D042.pdf", "Sheet")
    assert seen == [("D042.pdf", 1)]
    assert core._host_import_commands == []
    assert [delay for delay, _callback in loop.timers] == [0]     # armed as the handler returns


def test_the_scope_closes_when_the_import_raises(monkeypatch):
    _host(monkeypatch)
    loop = _Loop(monkeypatch)
    handler = _load_handler(monkeypatch)

    def failing(_filename):
        raise KeyError("unexpected")

    monkeypatch.setattr(handler, "_import_with_dialog", failing)
    with pytest.raises(KeyError):
        handler.open("D042.pdf")
    assert core._host_import_commands == [] and loop.timers == []


def test_the_handler_works_with_a_core_that_has_no_command_scope(monkeypatch):
    _host(monkeypatch)
    _Loop(monkeypatch)
    handler = _load_handler(monkeypatch)
    monkeypatch.delattr(core, "host_import_command")
    called = []
    monkeypatch.setattr(handler, "_import_with_dialog", called.append)
    handler.open("D042.pdf")
    assert called == ["D042.pdf"]
