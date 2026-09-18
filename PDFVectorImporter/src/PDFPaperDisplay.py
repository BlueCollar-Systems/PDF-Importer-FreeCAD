"""Toggleable PDF paper in the view, with no exported CAD geometry or theme edits."""
from __future__ import annotations

import json
import math
import re

PROPERTY = "PDFPaperDisplayJSON"
SCHEMA = "bcs.freecad.paper-display/1"


def validate(data):
    if data.get("schema") != SCHEMA or re.fullmatch(r"[0-9a-f]{64}", data.get("source_sha256", "")) is None:
        raise ValueError("PDF paper has no source identity")
    points = data.get("corners_mm", ())
    if len(points) != 4 or any(len(point) != 3 or not all(math.isfinite(v) for v in point) for point in points):
        raise ValueError("PDF paper bounds are invalid")
    if any(abs(point[2]) > 1e-9 for point in points):
        raise ValueError("PDF paper source is not planar")
    twice_area = sum(points[i][0] * points[(i + 1) % 4][1] - points[(i + 1) % 4][0] * points[i][1] for i in range(4))
    if abs(twice_area) < 1e-12 or not math.isfinite(data.get("display_z_mm", float("nan"))) or data["display_z_mm"] >= 0:
        raise ValueError("PDF paper has degenerate bounds or invalid display depth")
    return data


class ViewProviderPaper:
    def __init__(self, view):
        view.Proxy = self

    def attach(self, view):
        from pivy import coin
        self.view = view
        self.node = coin.SoSeparator()
        self.node.setName("BCSPDFPaperDisplay")
        self.transform = coin.SoTransform(); self.node.addChild(self.transform)
        pick = coin.SoPickStyle(); pick.style = coin.SoPickStyle.UNPICKABLE
        self.node.addChild(pick)
        material = coin.SoMaterial()
        material.diffuseColor.setValue(0, 0, 0)
        material.ambientColor.setValue(0, 0, 0)
        material.specularColor.setValue(0, 0, 0)
        material.emissiveColor.setValue(1, 1, 1)
        material.setOverride(True)
        self.node.addChild(material)
        style = coin.SoDrawStyle(); style.style = coin.SoDrawStyle.FILLED; style.setOverride(True)
        self.node.addChild(style)
        self.coords = coin.SoCoordinate3(); self.node.addChild(self.coords)
        face = coin.SoFaceSet(); face.numVertices.setValues([4]); self.node.addChild(face)
        view.addDisplayMode(self.node, "Paper")
        self.updateData(view.Object, PROPERTY)
        self.updateData(view.Object, "Placement")

    def updateData(self, obj, prop):
        if prop == "Placement" and hasattr(self, "transform") and hasattr(obj, "Placement"):
            placement = obj.Placement
            self.transform.translation.setValue(tuple(placement.Base))
            self.transform.rotation.setValue(tuple(placement.Rotation.Q))
        if prop == PROPERTY and hasattr(self, "coords"):
            data = validate(json.loads(getattr(obj, PROPERTY)))
            self.coords.point.setValues([(x, y, data["display_z_mm"]) for x, y, _z in data["corners_mm"]])

    def getDisplayModes(self, _view):
        return ["Paper"]

    def getDefaultDisplayMode(self):
        return "Paper"

    def setDisplayMode(self, mode):
        return mode

    def onChanged(self, _view, _prop):
        pass

    def __getstate__(self):
        return None

    def __setstate__(self, _state):
        return None


def restore_document_paper(doc):
    count = 0
    for obj in getattr(doc, "Objects", ()):
        if getattr(obj, PROPERTY, None) and getattr(obj, "ViewObject", None) is not None:
            validate(json.loads(getattr(obj, PROPERTY)))
            if not isinstance(getattr(obj.ViewObject, "Proxy", None), ViewProviderPaper):
                ViewProviderPaper(obj.ViewObject)
            count += 1
    return count


def create_paper(doc, parent, *, page_number, source_sha256, corners, display_z=-1.):
    data = validate({"schema": SCHEMA, "page": page_number, "source_sha256": source_sha256,
                     "corners_mm": [list(p) for p in corners], "display_z_mm": display_z})
    obj = doc.addObject("App::FeaturePython", "PDF_Paper")
    obj.Label = "PDF paper (toggle with Space)"
    obj.addProperty("App::PropertyString", PROPERTY, "PDF source display")
    obj.addProperty("App::PropertyPlacement", "Placement", "PDF source display")
    setattr(obj, PROPERTY, json.dumps(data, sort_keys=True))
    obj.setEditorMode(PROPERTY, 1)
    if parent is not doc and hasattr(parent, "addObject"):
        parent.addObject(obj)
    if obj.ViewObject is not None:
        ViewProviderPaper(obj.ViewObject)
    return obj
