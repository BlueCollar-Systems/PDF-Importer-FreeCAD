# -*- coding: utf-8 -*-
"""Face-on orthographic camera for an imported PDF sheet.

FreeCAD's viewTop() aims at the XY sheet, then puts back the navigation
camera. That camera is often perspective, so a print opens as an orbit
instead of a sheet you can read. Orthographic is applied after viewTop,
and the height comes from the sheet spans so Fit All cannot be pulled
off the page by unrelated document geometry.
"""
from __future__ import annotations

from typing import Optional, Sequence, Tuple


def orthographic_sheet_frame(
    min_x: float,
    min_y: float,
    max_x: float,
    max_y: float,
    aspect: float = 1.0,
    margin: float = 1.05,
) -> Tuple[float, float, float, float]:
    """Return center x, center y, eye Z, and orthographic height.

    ``aspect`` is viewport width / height. The height covers the sheet's
    vertical span and, when the window is narrow, its width as well.
    """
    width = max(float(max_x) - float(min_x), 1.0e-6)
    height = max(float(max_y) - float(min_y), 1.0e-6)
    try:
        aspect_value = float(aspect)
    except (TypeError, ValueError):
        aspect_value = 1.0
    if aspect_value <= 0.0:
        aspect_value = 1.0
    ortho_h = max(height, width / aspect_value) * float(margin)
    center_x = (float(min_x) + float(max_x)) * 0.5
    center_y = (float(min_y) + float(max_y)) * 0.5
    eye_z = max(width, height, ortho_h) * 2.0
    return center_x, center_y, eye_z, ortho_h


def viewport_aspect(view, camera) -> float:
    """Width/height of the window.

    FreeCAD's orthographic camera keeps ``aspectRatio`` at 1.0 and applies
    the viewport while drawing. Using that default frames a wide sheet as
    if the window were square, so the page opens short of the view.
    """
    try:
        size = view.getSize()
        width = float(size[0])
        height = float(size[1])
        if width > 0.0 and height > 0.0:
            return width / height
    except (AttributeError, RuntimeError, TypeError, ValueError, IndexError):
        pass
    try:
        aspect = float(camera.aspectRatio.getValue())
        if aspect > 0.0:
            return aspect
    except (AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return 1.0


def apply_straight_on_view(view, bounds: Optional[Sequence[float]]) -> bool:
    """Look straight down on the sheet, orthographic, zoomed to the page."""
    if view is None:
        return False
    try:
        view.viewTop()
    except (AttributeError, RuntimeError):
        pass
    try:
        view.setCameraType("Orthographic")
    except (AttributeError, RuntimeError):
        return False

    frame = None
    if bounds is not None and len(bounds) >= 4:
        try:
            frame = orthographic_sheet_frame(*[float(v) for v in bounds[:4]])
        except (TypeError, ValueError):
            frame = None

    cam = None
    aspect = 1.0
    if frame is not None:
        try:
            cam = view.getCameraNode()
            aspect = viewport_aspect(view, cam)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            cam = None
            aspect = 1.0
        if aspect != 1.0:
            frame = orthographic_sheet_frame(
                float(bounds[0]), float(bounds[1]), float(bounds[2]), float(bounds[3]),
                aspect=aspect,
            )

    if cam is not None and frame is not None:
        center_x, center_y, eye_z, ortho_h = frame
        try:
            cam.position.setValue(center_x, center_y, eye_z)
            cam.height.setValue(ortho_h)
            cam.focalDistance.setValue(eye_z)
            cam.nearDistance.setValue(max(eye_z / 1000.0, 0.01))
            cam.farDistance.setValue(max(eye_z * 20.0, 1000.0))
            return True
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass

    try:
        view.fitAll()
    except (AttributeError, RuntimeError):
        pass
    # fitAll can restore a perspective navigation camera. Put orthographic back.
    try:
        view.setCameraType("Orthographic")
    except (AttributeError, RuntimeError):
        return False
    return True
