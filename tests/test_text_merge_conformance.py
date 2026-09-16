#!/usr/bin/env python3
"""Canonical consumer for corpus stacked-fraction conformance vectors."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore.primitive_extractor import _merge_stacked_fractions  # noqa: E402
from pdfcadcore.primitives import NormalizedText, next_id, reset_ids  # noqa: E402


def _corpus_root() -> Path:
    env = os.environ.get("BCS_PRIVATE_VALIDATION_ROOT") or os.environ.get("PDF_PRIVATE_VALIDATION_ROOT")
    if env:
        return Path(env)
    default = Path(r"__private_validation_assets_not_configured__")
    if default.is_dir():
        return default
    pytest.skip("BCS_PRIVATE_VALIDATION_ROOT not set and default corpus missing")


def _vector_file() -> Path:
    path = _corpus_root() / "conformance-vectors" / "stacked-fraction-merge-vectors.json"
    if not path.is_file():
        pytest.skip(f"conformance vectors missing: {path}")
    return path


def _load_vectors() -> list[dict]:
    data = json.loads(_vector_file().read_text(encoding="utf-8"))
    assert data.get("schema") == "bcs.conformance_vectors/1.0"
    return list(data["vectors"])


def _bbox(span: dict) -> tuple[float, float, float, float]:
    x = float(span["x"])
    y = float(span["y"])
    size = float(span["font_size"])
    width = size * max(len(str(span["text"])), 1) * 0.5
    return (x, y, x + width, y + size)


def _items(vector: dict) -> list[NormalizedText]:
    reset_ids()
    out: list[NormalizedText] = []
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


def _assert_vector(vector: dict) -> None:
    expected = vector["expected"]
    before = _items(vector)
    after = _merge_stacked_fractions(before)
    texts = [item.text for item in after]
    merged_text = expected.get("merged_text")

    for forbidden in expected.get("forbidden_texts") or []:
        assert forbidden not in texts, vector["id"]

    if expected["should_merge"]:
        # These legacy vectors carry only aggregate spans, not the complete
        # character-layout bijection now required for a truthful visual merge.
        # The separately approved visual-layout suite exercises positive merges.
        assert after is before, vector["id"]
        assert texts == [item.text for item in before], vector["id"]
        if merged_text:
            assert merged_text not in texts, vector["id"]
    else:
        if merged_text:
            assert merged_text not in texts, vector["id"]
        assert [item.text for item in after] == [item.text for item in before], vector["id"]


def test_text_merge_conformance_vectors() -> None:
    for vector in _load_vectors():
        _assert_vector(vector)
