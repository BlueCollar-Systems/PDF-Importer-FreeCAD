from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


def _drawings(group_count: int, items_per_group: int):
    return [
        {"items": [("l", index)] * items_per_group}
        for index in range(group_count)
    ]


def test_item_dense_page_engages_heavy_safe_mode_below_group_threshold():
    drawings = _drawings(2526, 14)

    assert core._drawing_work_item_count(drawings) == 35364
    assert core._is_heavy_vector_page(drawings, threshold=3000) is True


def test_group_dense_page_still_engages_heavy_safe_mode():
    drawings = _drawings(3001, 1)

    assert core._is_heavy_vector_page(drawings, threshold=3000) is True


def test_ordinary_page_and_disabled_threshold_do_not_engage_heavy_safe_mode():
    drawings = _drawings(2526, 1)

    assert core._is_heavy_vector_page(drawings, threshold=3000) is False
    assert core._is_heavy_vector_page(drawings, threshold=0) is False
