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
