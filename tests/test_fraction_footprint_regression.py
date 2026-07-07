#!/usr/bin/env python3
"""Portable regression tests for stacked-fraction footprint behavior."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore.primitive_extractor import _FRAC_STACKED_SCALE, _merge_stacked_fractions  # noqa: E402
from pdfcadcore.primitives import NormalizedText, next_id, reset_ids  # noqa: E402


VECTOR_FILE = Path(
    os.environ.get(
        "BCS_STACKED_FRACTION_VECTORS",
        r"__private_validation_assets_not_configured__\conformance-vectors\stacked-fraction-merge-vectors.json",
    )
)
FALLBACK_VECTORS = [
    {
        "id": "false_merge_guard_2_1_4",
        "input": {
            "spans": [
                {"text": "2", "x": 5.0, "y": 20.0, "font_size": 6.0, "font_name": "Arial"},
                {"text": "1", "x": 13.0, "y": 22.0, "font_size": 3.78, "font_name": "Arial"},
                {"text": "/", "x": 13.0, "y": 20.0, "font_size": 3.78, "font_name": "Arial"},
                {"text": "4", "x": 13.0, "y": 18.0, "font_size": 3.78, "font_name": "Arial"},
            ]
        },
        "expected": {
            "merged_text": "1/4",
            "effective_font_size": 2.268,
            "bbox_width_scaled": True,
            "should_merge": True,
        },
    },
    {
        "id": "size_ratio_guard",
        "input": {
            "spans": [
                {"text": "1", "x": 10.0, "y": 20.0, "font_size": 2.0, "font_name": "Arial"},
                {"text": "/", "x": 13.0, "y": 20.0, "font_size": 5.0, "font_name": "Arial"},
                {"text": "2", "x": 16.0, "y": 20.0, "font_size": 2.0, "font_name": "Arial"},
            ]
        },
        "expected": {
            "merged_text": None,
            "effective_font_size": None,
            "bbox_width_scaled": False,
            "should_merge": False,
        },
    },
    {
        "id": "invalid_denominator",
        "input": {
            "spans": [
                {"text": "1", "x": 10.0, "y": 20.0, "font_size": 3.78, "font_name": "Arial"},
                {"text": "/", "x": 13.0, "y": 20.0, "font_size": 3.78, "font_name": "Arial"},
                {"text": "7", "x": 16.0, "y": 20.0, "font_size": 3.78, "font_name": "Arial"},
            ]
        },
        "expected": {
            "merged_text": None,
            "effective_font_size": None,
            "bbox_width_scaled": False,
            "should_merge": False,
        },
    },
]


def _bbox(span: dict) -> tuple[float, float, float, float]:
    x = float(span["x"])
    y = float(span["y"])
    size = float(span["font_size"])
    width = size * max(len(str(span["text"])), 1) * 0.5
    return (x, y, x + width, y + size)


def _items(vector: dict) -> list[NormalizedText]:
    reset_ids()
    out = []
    for span in vector["input"]["spans"]:
        text = str(span["text"])
        out.append(
            NormalizedText(
                id=next_id(),
                text=text,
                normalized=text.upper().strip(),
                insertion=(float(span["x"]), float(span["y"])),
                bbox=_bbox(span),
                font_size=float(span["font_size"]),
                font_name=str(span.get("font_name") or ""),
                page_number=1,
            )
        )
    return out


def _vectors() -> list[dict]:
    if not VECTOR_FILE.is_file():
        return FALLBACK_VECTORS
    data = json.loads(VECTOR_FILE.read_text(encoding="utf-8"))
    return list(data["vectors"])


def test_stacked_fraction_font_size_reduction() -> None:
    for vector in _vectors():
        expected = vector["expected"]
        if not expected["should_merge"]:
            continue
        merged = _merge_stacked_fractions(_items(vector))
        item = next(text for text in merged if text.text == expected["merged_text"])
        assert item.font_size == expected["effective_font_size"]


def test_stacked_fraction_bbox_width_reduction() -> None:
    for vector in _vectors():
        expected = vector["expected"]
        if not expected["bbox_width_scaled"]:
            continue
        source_boxes = [_bbox(span) for span in vector["input"]["spans"]]
        source_width = max(box[2] for box in source_boxes) - min(box[0] for box in source_boxes)
        merged = _merge_stacked_fractions(_items(vector))
        item = next(text for text in merged if text.text == expected["merged_text"])
        merged_width = item.bbox[2] - item.bbox[0]
        assert merged_width <= source_width * (_FRAC_STACKED_SCALE + 1e-6)


def test_fraction_no_overrun_with_adjacent_digits() -> None:
    vector = next(v for v in _vectors() if v["id"] == "false_merge_guard_2_1_4")
    merged = _merge_stacked_fractions(_items(vector))
    texts = [item.text for item in merged]
    assert "2" in texts
    assert "1/4" in texts
    assert "2/4" not in texts


def test_invalid_denominator_and_size_ratio_do_not_merge() -> None:
    for vector_id in ("size_ratio_guard", "invalid_denominator"):
        vector = next(v for v in _vectors() if v["id"] == vector_id)
        before = _items(vector)
        after = _merge_stacked_fractions(before)
        assert [item.text for item in after] == [item.text for item in before]
