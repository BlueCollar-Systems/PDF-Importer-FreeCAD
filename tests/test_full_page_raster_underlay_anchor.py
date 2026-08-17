"""The full-page raster underlay (auto/hybrid and raster modes) must sit under the
vectors it underlays. Image::ImagePlane renders centred on its Placement, and the
page's vectors/text span (0..W, 0..H): anchored at the origin the raster sat half a
page down-left of the text (visual oracle on the garden-map sheet, 2026-08-16 — the
FreeCAD render showed two copies of the page offset by (W/2, H/2))."""
from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src" / "PDFImporterCore.py"


def _load_anchor_fn():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_full_page_raster_anchor":
            mod = ast.Module(body=[node], type_ignores=[])
            ns: dict = {"Tuple": tuple}
            exec(compile(mod, str(SRC), "exec"), ns)  # noqa: S102 - our own source
            return ns["_full_page_raster_anchor"]
    raise AssertionError("_full_page_raster_anchor missing")


def test_full_page_raster_is_anchored_at_the_page_centre_behind_the_vectors():
    anchor = _load_anchor_fn()
    assert anchor(558.8, 863.6) == (279.4, 431.8, -0.1)
    assert anchor(0.0, 0.0) == (0.0, 0.0, -0.1)


def test_import_page_as_raster_uses_the_centre_anchor_for_placement_and_verification():
    text = SRC.read_text(encoding="utf-8")
    body = text.split("def _import_page_as_raster(")[1].split("\ndef ")[0]
    assert "anchor_xyz = _full_page_raster_anchor(w_units, h_units)" in body
    assert "ip.Placement = Placement(_v(*anchor_xyz), Rotation())" in body
    assert "for index, expected in enumerate(anchor_xyz)" in body
    assert not re.search(r"Placement\(_v\(0, 0, -0\.1\)", body)
