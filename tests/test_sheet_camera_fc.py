"""Face-on orthographic frame for an imported sheet."""
from __future__ import annotations

from PDFVectorImporter.sheet_camera import apply_straight_on_view, orthographic_sheet_frame


class _Value:
    def __init__(self, value):
        self.value = value

    def getValue(self):
        return self.value

    def setValue(self, *args):
        self.value = args[0] if len(args) == 1 else args


class _Camera:
    def __init__(self, aspect):
        self.aspectRatio = _Value(aspect)
        self.position = _Value(None)
        self.height = _Value(None)
        self.focalDistance = _Value(None)
        self.nearDistance = _Value(None)
        self.farDistance = _Value(None)


class _View:
    def __init__(self, aspect=1.0):
        self.calls = []
        self.camera = _Camera(aspect)

    def viewTop(self):
        self.calls.append("viewTop")

    def setCameraType(self, kind):
        self.calls.append(kind)

    def fitAll(self):
        self.calls.append("fitAll")

    def getCameraNode(self):
        return self.camera


def test_landscape_sheet_height_covers_width_in_a_narrow_window():
    # 17 in wide, 11 in tall, window taller than it is wide.
    _cx, _cy, _z, height = orthographic_sheet_frame(0, 0, 431.8, 279.4, aspect=0.6)
    assert height >= 431.8 / 0.6


def test_view_top_then_orthographic_and_no_fit_all_when_the_sheet_is_known():
    view = _View(aspect=1.6)
    assert apply_straight_on_view(view, (0.0, 0.0, 431.8, 279.4)) is True
    assert view.calls[:2] == ["viewTop", "Orthographic"]
    assert "fitAll" not in view.calls
    assert view.camera.height.value >= 279.4
    # Camera sits above the sheet center, looking down. Not a perspective orbit.
    pos = view.camera.position.value
    assert abs(pos[0] - 215.9) < 1.0
    assert abs(pos[1] - 139.7) < 1.0
    assert pos[2] > 0.0


def test_window_size_frames_a_wide_sheet_when_the_camera_aspect_stays_square():
    view = _View(aspect=1.0)
    def window_size():
        return (1600, 900)
    view.getSize = window_size
    assert apply_straight_on_view(view, (0.0, 0.0, 431.8, 279.4)) is True
    _cx, _cy, _z, wide = orthographic_sheet_frame(0.0, 0.0, 431.8, 279.4, aspect=1600.0 / 900.0)
    _cx, _cy, _z, square = orthographic_sheet_frame(0.0, 0.0, 431.8, 279.4, aspect=1.0)
    assert abs(view.camera.height.value - wide) < 0.01
    assert view.camera.height.value < square


def test_missing_camera_node_still_finishes_orthographic():
    class Bare(_View):
        def getCameraNode(self):
            raise RuntimeError("no gui camera")

    view = Bare()
    assert apply_straight_on_view(view, (0, 0, 100, 50)) is True
    assert view.calls[0] == "viewTop"
    assert view.calls[-1] == "Orthographic"
    assert "fitAll" in view.calls

def test_getSize_overrides_a_stuck_aspectRatio_of_one():
    """FreeCAD reports aspectRatio 1.0; the live viewport still shapes the frame."""

    class Sized(_View):
        def getSize(self):
            return (1600, 900)

    view = Sized(aspect=1.0)
    assert apply_straight_on_view(view, (0.0, 0.0, 431.8, 279.4)) is True
    # 17x11 sheet in a 16:9 window: ortho height = max(279.4, 431.8 / (16/9)) * 1.05
    expected = max(279.4, 431.8 / (1600 / 900)) * 1.05
    assert abs(view.camera.height.value - expected) < 1e-6
