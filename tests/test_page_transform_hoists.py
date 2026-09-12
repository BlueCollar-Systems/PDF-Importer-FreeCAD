"""Per-page constants stay constant across the geometry loop.

On a 550k-path sheet the page loop read ``page.rect`` twice per path group and
re-tupled the page rotation matrix once per transformed point: 1.1 million PyMuPDF
Rect constructions (15 s) and 1.1 million tuple rebuilds (3 s) for values that do
not change within a page.  These tests pin that the memoized matrix values are
exactly the converted floats, follow a re-installed matrix, and that the page-area
gate no longer touches ``page.rect`` per item.
"""

from __future__ import annotations

import ast
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


def test_page_matrix_values_are_the_converted_floats_and_memoized():
    opts = core.ImportOptions()
    opts._page_rotation_matrix = (0, 1, -1, 0, 100, 0)
    first = core._page_matrix_values(opts)
    assert first == (0.0, 1.0, -1.0, 0.0, 100.0, 0.0)
    assert all(isinstance(value, float) for value in first)
    assert core._page_matrix_values(opts) is first  # served from the memo


def test_page_matrix_values_follow_a_reinstalled_matrix_and_none():
    opts = core.ImportOptions()
    opts._page_rotation_matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    assert core._page_matrix_values(opts) == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    opts._page_rotation_matrix = (0.0, -1.0, 1.0, 0.0, 0.0, 200.0)  # next page, rotated
    assert core._page_matrix_values(opts) == (0.0, -1.0, 1.0, 0.0, 0.0, 200.0)
    opts._page_rotation_matrix = None  # cleared after the page
    assert core._page_matrix_values(opts) == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    opts._page_rotation_matrix = ("x", 0, 0, 1, 0, 0)  # unreadable falls back
    assert core._page_matrix_values(opts) == (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


class _FakeVector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = float(x), float(y), float(z)


def test_to_fc_uses_the_memoized_matrix_unchanged(monkeypatch):
    monkeypatch.setattr(core, "Vector", _FakeVector)
    opts = core.ImportOptions(scale_to_mm=False, user_scale=1.0, flip_y=True)
    opts._page_rotation_matrix = (0.0, 1.0, -1.0, 0.0, 100.0, 0.0)
    first = core._to_fc((10.0, 50.0), 160.0, opts, 2.0)
    second = core._to_fc((10.0, 50.0), 160.0, opts, 2.0)
    # x' = a*x + c*y + e = -50 + 100 = 50 ; y' = b*x + d*y + f = 10 ; flip: 160 - 10 = 150 ; x2
    assert (first.x, first.y, first.z) == (100.0, 300.0, 0.0)
    assert (second.x, second.y, second.z) == (first.x, first.y, first.z)
    assert opts._page_matrix_values_cache[0] is opts._page_rotation_matrix


def test_page_area_gate_does_not_read_page_rect_per_path_group():
    source = (SRC_DIR / "PDFImporterCore.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    inner = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_import_pdf_page_inner"
    )
    loops = [node for node in ast.walk(inner) if isinstance(node, ast.For)]
    offending = []
    for loop in loops:
        for node in ast.walk(loop):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "rect"
                and isinstance(node.value, ast.Name)
                and node.value.id == "page"
            ):
                offending.append(node.lineno)
    assert offending == [], f"page.rect read inside a loop at lines {offending}"
