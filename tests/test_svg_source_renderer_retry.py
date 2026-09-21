"""Recover source vectors without changing representation or ownership checks."""
from pathlib import Path

import pytest

from test_svg_text_representation_fc import (
    FakeDocument,
    FakeGroup,
    SVG,
    _install_renderer,
    _source_item,
    core,
    renderer,
)


# Public synthetic Cairo-style composition graph. The glyphs are painted via
# feImage references, not direct body uses. Definition scanning would incorrectly
# include the unused third glyph and lose the composition transforms.
COMPOSITE_SVG = """
<svg xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100">
 <defs>
  <g id="glyph-0-1"><path d="M0 0L5 0L5 5Z"/></g>
  <g id="compositing-group-1">
   <use xlink:href="#glyph-0-1" x="10" y="20"/>
   <use xlink:href="#glyph-0-1" x="30" y="20"/>
  </g>
  <g id="unused"><use xlink:href="#glyph-0-1" x="80" y="80"/></g>
  <filter id="filter-0">
   <feImage xlink:href="#compositing-group-1" result="source"/>
   <feBlend in="source" in2="BackgroundImage" mode="multiply"/>
  </filter>
 </defs>
 <g filter="url(#filter-0)"><rect width="100" height="100"/></g>
</svg>
"""


def setup_retry(monkeypatch, replacement=SVG):
    _install_renderer(monkeypatch)
    calls = []
    monkeypatch.setattr(renderer, "find_pdftocairo", lambda: "pdftocairo")

    def cairo(_exe, path, page):
        calls.append(("pdftocairo", path, page, Path(path).read_bytes()))
        return COMPOSITE_SVG

    def mupdf(path, page):
        calls.append(("pymupdf", path, page, Path(path).read_bytes()))
        return replacement

    monkeypatch.setattr(renderer, "_render_svg_with_pdftocairo", cairo)
    monkeypatch.setattr(renderer, "_render_svg_with_pymupdf", mupdf)
    return calls


def deliver(doc, group, item, cache):
    return renderer.render_text(
        "fixture.pdf", 1, 100.0, 1.0, page_w=100.0,
        fc_doc=doc, parent_group=group,
        representation=item["requested_type"], source_item=item,
        requested_representation=item["requested_type"], render_cache=cache,
    )


@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
def test_composition_graph_recovers_same_mode_once_and_preserves_item_claims(monkeypatch, mode):
    calls = setup_retry(monkeypatch)
    assert renderer._parse_all_glyph_defs(COMPOSITE_SVG)
    assert renderer._parse_use_placements(COMPOSITE_SVG) == []
    items = [
        _source_item(bbox=(x, 18, x + 10, 27), requested_type=mode,
                     source_item_id=f"p1:b0:l0:s{i}")
        for i, x in enumerate((8, 28))
    ]
    cache = {"source_item_manifest": [
        {"source_order": i, **{k: item[k] for k in (
            "source_item_id", "page_number", "pdf_sha256", "bbox", "text"
        )}} for i, item in enumerate(items)
    ]}
    doc, group = FakeDocument(), FakeGroup()
    results = [deliver(doc, group, item, cache) for item in items]
    assert [call[0] for call in calls] == ["pdftocairo", "pymupdf"]
    assert calls[0][1:] == calls[1][1:]
    assert calls[0][1] != "fixture.pdf"
    assert not Path(calls[0][1]).exists(), "owned immutable snapshot must be cleaned"
    assert cache["claimed_placement_indices"] == {0, 1}
    for i, result in enumerate(results):
        assert result["entity_type"] == mode
        assert result["renderer"] == "pymupdf"
        assert result["item_filter"]["matched_placement_indices"] == [i]
        evidence = result["delivery_attempts"][0]["evidence"]
        assert evidence["renderer"] == "pymupdf"
        assert evidence["renderer_recovery"]["representation_preserved"] is True
        assert evidence["renderer_recovery"]["from_renderer"] == "pdftocairo"


@pytest.mark.parametrize("replacement,reason", [
    (None, "svg_renderer_recovery_failed"),
    (COMPOSITE_SVG, "svg_source_glyph_placements_unverified"),
])
def test_failed_recovery_does_not_certify_item_impossibility(monkeypatch, replacement, reason):
    setup_retry(monkeypatch, replacement)
    doc, group = FakeDocument(), FakeGroup()
    item = _source_item(bbox=(8, 18, 18, 27))
    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        deliver(doc, group, item, {})
    assert caught.value.reason == reason
    assert reason not in core.CLOSED_SVG_ITEM_IMPOSSIBILITY_REASONS
    assert caught.value.evidence["created_entity_ids"] == []
    assert caught.value.evidence["cleanup_complete"] is True
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]
    assert group.objects == []


def test_recovered_vectors_cannot_be_relabelled_to_unrelated_source_item(monkeypatch):
    setup_retry(monkeypatch)
    doc, group = FakeDocument(), FakeGroup()
    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        deliver(doc, group, _source_item(bbox=(70, 70, 80, 80)), {})
    assert caught.value.reason == "svg_item_filter_empty"
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]
    assert group.objects == []


def test_readable_cairo_vectors_do_not_request_second_renderer(monkeypatch):
    setup_retry(monkeypatch)
    monkeypatch.setattr(renderer, "_render_svg_with_pdftocairo", lambda *_args: SVG)
    monkeypatch.setattr(renderer, "_render_svg_with_pymupdf",
                        lambda *_args: pytest.fail("readable Cairo SVG must stay on Cairo"))
    result = deliver(FakeDocument(), FakeGroup(), _source_item(bbox=(8, 18, 18, 27)), {})
    assert result["renderer"] == "pdftocairo"
    assert result["renderer_recovery"] is None


def test_source_snapshot_change_between_renderers_stops_before_retry(monkeypatch):
    calls = setup_retry(monkeypatch)
    signature = renderer._pdf_file_signature
    counter = 0

    def changed(path):
        nonlocal counter
        current = dict(signature(path))
        if calls:
            counter += 1
            current["size"] += 1
        return current

    monkeypatch.setattr(renderer, "_pdf_file_signature", changed)
    doc, group = FakeDocument(), FakeGroup()
    with pytest.raises(renderer.TextRepresentationRenderError) as caught:
        deliver(doc, group, _source_item(bbox=(8, 18, 18, 27)), {})
    assert caught.value.reason == "svg_source_snapshot_mutated"
    assert [call[0] for call in calls] == ["pdftocairo"]
    assert counter > 0
    assert [obj.Name for obj in doc.Objects] == ["UserObject"]
