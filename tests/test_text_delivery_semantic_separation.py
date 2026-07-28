"""Semantic text recognition must never rewrite physical delivery spans."""

from __future__ import annotations

import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore.primitive_extractor import semantic_text_projection  # noqa: E402
from pdfcadcore.document_profiler import profile  # noqa: E402
from pdfcadcore.generic_recognizer import analyze  # noqa: E402
from pdfcadcore.primitives import (  # noqa: E402
    NormalizedText,
    PageData,
    next_id,
    reset_ids,
    text_items_for_analysis,
)
from pdfcadcore.resolved_scale import resolve_page_scale  # noqa: E402


def _item(
    span_id: int,
    text: str,
    *,
    x: float,
    y: float,
    font_size: float,
) -> NormalizedText:
    width = max(len(text), 1) * font_size * 0.5
    return NormalizedText(
        id=span_id,
        text=text,
        normalized=text.upper(),
        insertion=(x, y),
        bbox=(x, y, x + width, y + font_size),
        font_size=font_size,
        font_name="ExactPDF",
        page_number=1,
        source_span_ids=(span_id,),
    )


def test_stacked_fraction_semantics_do_not_mutate_delivery_spans() -> None:
    # The source encodes a full-size whole number plus a stacked 1/4.
    delivery = [
        _item(1, "2", x=5.0, y=20.0, font_size=6.0),
        _item(2, "1", x=13.0, y=22.0, font_size=3.78),
        _item(3, "/", x=13.0, y=20.0, font_size=3.78),
        _item(4, "4", x=13.0, y=18.0, font_size=3.78),
    ]
    delivery_snapshot = [
        (
            item.id,
            item.text,
            item.insertion,
            item.bbox,
            item.font_size,
            item.source_span_ids,
            item.semantic_projection,
        )
        for item in delivery
    ]

    semantic = semantic_text_projection(delivery)
    page = PageData(
        page_number=1,
        width=100.0,
        height=100.0,
        text_items=delivery,
        semantic_text_items=semantic,
    )

    assert [item.text for item in page.text_items] == ["2", "1", "/", "4"]
    assert [
        (
            item.id,
            item.text,
            item.insertion,
            item.bbox,
            item.font_size,
            item.source_span_ids,
            item.semantic_projection,
        )
        for item in page.text_items
    ] == delivery_snapshot
    assert all(item.semantic_projection is False for item in page.text_items)

    analysis = text_items_for_analysis(page)
    assert analysis is page.semantic_text_items
    assert [item.text for item in analysis] == ["2", "1/4"]
    merged = next(item for item in analysis if item.text == "1/4")
    assert merged.semantic_projection is True
    assert merged.source_span_ids == (2, 3, 4)
    assert merged.requires_individual_positioning is False


def test_semantic_projection_does_not_consume_physical_identity_allocator() -> None:
    delivery = [
        _item(1, "1", x=1.0, y=4.0, font_size=3.0),
        _item(2, "/", x=1.0, y=3.0, font_size=3.0),
        _item(3, "4", x=1.0, y=2.0, font_size=3.0),
    ]
    reset_ids()
    try:
        semantic = semantic_text_projection(delivery)

        assert [item.text for item in semantic] == ["1/4"]
        assert next_id() == 1
    finally:
        reset_ids()


def test_analysis_falls_back_to_delivery_text_when_no_projection_is_present() -> None:
    delivery = [_item(1, "UNCHANGED", x=1.0, y=2.0, font_size=3.0)]
    page = PageData(page_number=1, width=10.0, height=10.0, text_items=delivery)

    assert text_items_for_analysis(page) is delivery


def test_profiler_uses_semantic_projection_without_rewriting_delivery() -> None:
    delivery = [_item(1, "SOURCE", x=1.0, y=2.0, font_size=3.0)]
    semantic = [_item(1, "1/4", x=1.0, y=2.0, font_size=3.0)]
    semantic[0].semantic_projection = True
    semantic[0].generic_tags.append("dimension_like")
    page = PageData(
        page_number=1,
        width=10.0,
        height=10.0,
        text_items=delivery,
        semantic_text_items=semantic,
    )

    result = profile(page)

    assert result.has_dimensions is True
    assert [item.text for item in page.text_items] == ["SOURCE"]


def test_scale_resolution_uses_semantic_projection_without_rewriting_delivery() -> None:
    delivery = [_item(1, "SOURCE", x=60.0, y=10.0, font_size=3.0)]
    semantic = [_item(1, "SCALE 1:25", x=60.0, y=10.0, font_size=3.0)]
    semantic[0].normalized = "SCALE 1:25"
    semantic[0].semantic_projection = True
    semantic[0].generic_tags.extend(("scale_like", "titleblock_like"))
    page = PageData(
        page_number=1,
        width=100.0,
        height=100.0,
        text_items=delivery,
        semantic_text_items=semantic,
    )

    result = resolve_page_scale(page)

    assert result.factor == 25.0
    assert result.notation == "SCALE 1:25"
    assert [item.text for item in page.text_items] == ["SOURCE"]


def test_dimension_recognizer_uses_semantic_projection_and_reports_source_spans() -> None:
    delivery = [
        _item(1, "1", x=1.0, y=4.0, font_size=3.0),
        _item(2, "/", x=1.0, y=3.0, font_size=3.0),
        _item(3, "4", x=1.0, y=2.0, font_size=3.0),
    ]
    semantic = semantic_text_projection(delivery)
    page = PageData(
        page_number=1,
        width=10.0,
        height=10.0,
        text_items=delivery,
        semantic_text_items=semantic,
    )

    result = analyze(page)

    association = next(item for item in result.dimension_assocs if item["text"] == "1/4")
    assert association["semantic_projection"] is True
    assert association["source_span_ids"] == [1, 2, 3]
    assert [item.text for item in page.text_items] == ["1", "/", "4"]
