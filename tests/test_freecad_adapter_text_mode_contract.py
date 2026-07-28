from __future__ import annotations

import argparse

import pytest

from PDFVectorImporter.adapters import freecad_adapter


def _args(text_mode):
    return argparse.Namespace(
        test_id="FC-TEXT-MODE",
        input="drawing.pdf",
        mode="vector",
        text_mode=text_mode,
        page_range="1",
        output_dir=None,
        layers_min_populated=0,
        runtime_cap_seconds=0,
        notes=None,
    )


@pytest.mark.parametrize(
    "text_mode",
    ("text", "labels", "glyphs", "3d_text", "geometry", "raster"),
)
def test_adapter_payload_preserves_explicit_canonical_text_mode(
    tmp_path,
    text_mode,
):
    payload = freecad_adapter.build_payload(
        _args(text_mode),
        {},
        str(tmp_path / "result.json"),
        str(tmp_path),
    )

    assert payload["text_mode"] == text_mode


@pytest.mark.parametrize(
    "text_mode",
    (None, "", " Labels", "labels ", "LABELS", "native_text", 3),
)
def test_adapter_payload_rejects_missing_or_noncanonical_text_mode(
    tmp_path,
    text_mode,
):
    with pytest.raises(ValueError, match="text_mode"):
        freecad_adapter.build_payload(
            _args(text_mode),
            {},
            str(tmp_path / "result.json"),
            str(tmp_path),
        )
