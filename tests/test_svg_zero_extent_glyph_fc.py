from __future__ import annotations

import hashlib
import math

import pytest

from test_svg_text_representation_fc import (
    FakeDocument,
    FakeGroup,
    _install_renderer,
    _source_item,
    glyph_fill,
    renderer,
)


SVG = '''<svg xmlns="http://www.w3.org/2000/svg"
 xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 100 100">
<defs><g>
  <g id="glyph-zero"><rect x="0" y="0" width="0" height="0" mask="url(#mask-zero)"/></g>
  <g id="glyph-ink"><path d="M0 0 L5 0 L5 5 Z"/></g>
</g>
<image id="source-zero" x="0" y="0" width="0" height="0"/>
<mask id="mask-zero"><use xlink:href="#source-zero"/></mask>
</defs>
<g fill="black"><use xlink:href="#glyph-zero" x="10" y="20"/>
<use xlink:href="#glyph-ink" x="10" y="20"/>
<use xlink:href="#glyph-zero" x="15" y="20"/></g>
</svg>'''


def test_zero_extent_definition_has_complete_source_chain_and_occurrence_proof():
    definitions = renderer._parse_all_glyph_defs(SVG)
    assert definitions == {"glyph-ink": "M0 0 L5 0 L5 5 Z", "glyph-zero": ""}
    assert "glyph-zero" not in renderer._parse_glyph_defs(SVG)
    placements = renderer._parse_use_placements(SVG)
    rows = renderer._svg_zero_extent_placement_proofs(SVG, placements, "a" * 64, 2)
    assert [row["source_placement_index"] for row in rows] == [0, 2]
    assert [row["source_svg_origin"] for row in rows] == [[10.0, 20.0], [15.0, 20.0]]
    for row in rows:
        assert row["source_svg_sha256"] == hashlib.sha256(SVG.encode()).hexdigest()
        assert row["source_glyph_id"] == "glyph-zero"
        assert row["source_mask_id"] == "mask-zero"
        assert row["source_image_id"] == "source-zero"
        assert row["pdf_sha256"] == "a" * 64 and row["page_number"] == 2
        assert row["native_entity_created"] is False
        assert all(len(row[name]) == 64 for name in ("definition_sha256", "mask_sha256", "image_sha256"))
    assert rows[0]["source_svg_matrix"] is None


@pytest.mark.parametrize("zero", ["-0", "+0.000", ".0", "0e-999", "00.00e+30"])
def test_exact_lexical_zero_does_not_depend_on_float_conversion(zero):
    svg = SVG.replace('width="0"', f'width="{zero}"')
    assert renderer._parse_all_glyph_defs(svg)["glyph-zero"] == ""


@pytest.mark.parametrize("old,new", [
    ('width="0"', 'width="1"'),
    ('width="0"', 'width="1e-999"'),
    ('height="0"', 'height="-1"'),
    ('width="0"', 'width="nan"'),
    ('width="0"', 'width="0px"'),
    ('width="0"', ''),
    ('<rect ', '<rect transform="scale(0)" '),
    ('<rect ', '<rect style="width:1px" '),
    ('<rect ', '<rect onload="paint()" '),
    ('<rect ', '<path '),
    ('<g id="glyph-zero">', '<g id="glyph-zero" transform="translate(1)">'),
    ('<g id="glyph-zero">', '<g id="glyph-zero">visible text'),
    ('mask="url(#mask-zero)"', 'mask="url(external.svg#mask-zero)"'),
    ('<mask id="mask-zero">', '<mask id="mask-zero" maskUnits="userSpaceOnUse">'),
    ('<use xlink:href="#source-zero"/>', '<use xlink:href="#missing"/>'),
    ('<use xlink:href="#source-zero"/>', '<use xlink:href="#source-zero" x="1"/>'),
    ('<use xlink:href="#source-zero"/>', '<use href="#source-zero" xlink:href="#source-zero"/>'),
    ('<image id="source-zero" ', '<image id="source-zero" href="data:image/png;base64,AA==" '),
    ('<image id="source-zero" ', '<image id="source-zero" style="width:1px" '),
    ('<image id="source-zero" ', '<image id="source-zero" width="0" '),
    ('</mask>', '<use href="#source-zero"/></mask>'),
    ('</defs>', '<g id="glyph-zero"/></defs>'),
    ('</defs>', '<style>rect {width:1px}</style></defs>'),
    ('</defs>', '<image id="source-zero" x="0" y="0" width="0" height="0"/></defs>'),
    ('</svg>', ''),
])
def test_nonzero_malformed_unsupported_or_unproved_chain_is_not_empty(old, new):
    svg = SVG.replace(old, new, 1)
    assert renderer._svg_zero_extent_glyph_proofs(svg) == {}
    assert "glyph-zero" not in renderer._parse_all_glyph_defs(svg)


def test_source_image_nonzero_dimension_cannot_hide_behind_zero_rectangle():
    svg = SVG.replace('<image id="source-zero" x="0" y="0" width="0" height="0"/>',
                      '<image id="source-zero" x="0" y="0" width="1" height="1"/>')
    assert renderer._svg_zero_extent_glyph_proofs(svg) == {}


def test_extra_visible_glyph_child_cannot_be_labeled_empty():
    svg = SVG.replace('mask="url(#mask-zero)"/></g>',
                      'mask="url(#mask-zero)"/><path d="M0 0 L2 2"/></g>')
    assert renderer._svg_zero_extent_glyph_proofs(svg) == {}


@pytest.mark.parametrize("placement", [
    ("glyph-zero", math.inf, 0, None),
    ("glyph-zero", 0, 0, [1, 0, 0, 1, 0]),
    ("glyph-zero", 0, 0, [1, 0, 0, 1, math.nan, 0]),
    ("glyph-zero", True, 0, None),
])
def test_empty_occurrence_requires_finite_complete_source_position(placement):
    placements = renderer._parse_use_placements(SVG)
    placements[0] = placement
    with pytest.raises(ValueError, match="source placement"):
        renderer._svg_zero_extent_placement_proofs(SVG, placements, "a" * 64, 1)


@pytest.mark.parametrize("digest,page", [(None, 1), ("abc", 1), ("a" * 64, True), ("a" * 64, 0)])
def test_empty_occurrence_requires_pdf_and_page_identity(digest, page):
    with pytest.raises(ValueError, match="binding"):
        renderer._svg_zero_extent_placement_proofs(SVG, [], digest, page)


def test_empty_occurrence_cannot_reuse_valid_definition_at_a_different_origin():
    placements = renderer._parse_use_placements(SVG)
    placements[0] = ("glyph-zero", 71.0, 29.0, None)
    with pytest.raises(ValueError, match="source placement binding changed"):
        renderer._svg_zero_extent_placement_proofs(SVG, placements, "a" * 64, 1)


@pytest.mark.parametrize("effect", [
    'filter="url(#flood)"', 'style="filter:url(#flood)"',
    'mask="url(#other-mask)"', 'style="mix-blend-mode:multiply"',
])
def test_effectful_occurrence_cannot_be_certified_from_zero_definition(effect):
    svg = SVG.replace('<g fill="black">', f'<g fill="black" {effect}>')
    svg = svg.replace('</defs>', '<filter id="flood" filterUnits="userSpaceOnUse">'
                      '<feFlood flood-color="red"/></filter></defs>')
    assert renderer._svg_zero_extent_glyph_proofs(svg)
    with pytest.raises(ValueError, match="source paint context"):
        renderer._svg_zero_extent_placement_proofs(svg, renderer._parse_use_placements(svg), "a" * 64, 1)


def test_filtered_zero_with_visible_sibling_stops_before_native_creation(monkeypatch):
    real_paint_qualification = glyph_fill.placement_fill_rules
    _install_renderer(monkeypatch)
    monkeypatch.setattr(glyph_fill, "placement_fill_rules", real_paint_qualification)
    svg = SVG.replace('<g fill="black">', '<g fill="black" filter="url(#flood)">')
    svg = svg.replace('</svg>', '<use xlink:href="#glyph-ink" x="70" y="20"/></svg>')
    svg = svg.replace('</defs>', '<filter id="flood" filterUnits="userSpaceOnUse">'
                      '<feFlood flood-color="red"/></filter></defs>')
    monkeypatch.setattr(renderer, "_render_svg_with_pymupdf", lambda *_args: svg)
    doc = FakeDocument()
    baseline = list(doc.Objects)
    with pytest.raises(ValueError, match="source paint context"):
        renderer.render_text("fixture.pdf", 1, 100.0, 1.0, page_w=100.0,
                             fc_doc=doc, parent_group=FakeGroup(), representation="geometry")
    assert doc.Objects == baseline


def test_zero_extent_glyph_does_not_block_or_replace_visible_requested_glyph(monkeypatch):
    _install_renderer(monkeypatch)
    monkeypatch.setattr(renderer, "_render_svg_with_pymupdf", lambda *_args: SVG)
    result = renderer.render_text(
        "fixture.pdf", 1, 100.0, 1.0, page_w=100.0,
        fc_doc=FakeDocument(), parent_group=FakeGroup(), representation="glyphs",
        source_item=_source_item(bbox=(5.0, 15.0, 20.0, 30.0)),
        requested_representation="glyphs",
    )
    assert result["outcome"] == "verified" and result["glyphs"] == 1
    assert result["item_filter"]["empty_placement_indices"] == [0, 2]
    assert result["item_filter"]["matched_placement_indices"] == [1]
    evidence = result["zero_extent_placement_evidence"]
    assert evidence == result["item_filter"]["zero_extent_placement_evidence"]
    assert evidence == result["delivery_attempts"][0]["evidence"]["zero_extent_placement_evidence"]
    assert [row["source_placement_index"] for row in evidence] == [0, 2]
    assert all(row["pdf_sha256"] == "a" * 64 and row["page_number"] == 1 for row in evidence)
    assert all(row["source_svg_sha256"] == hashlib.sha256(SVG.encode()).hexdigest() for row in evidence)
    assert len(result["created_entity_ids"]) == 1


def test_unproved_zero_rectangle_still_blocks_delivery_and_creates_nothing(monkeypatch):
    _install_renderer(monkeypatch)
    monkeypatch.setattr(renderer, "_render_svg_with_pymupdf",
                        lambda *_args: SVG.replace('width="0"', 'width="1e-999"', 1))
    doc = FakeDocument()
    original_objects = list(doc.Objects)
    with pytest.raises(renderer.TextRepresentationRenderError) as raised:
        renderer.render_text("fixture.pdf", 1, 100.0, 1.0, page_w=100.0,
                             fc_doc=doc, parent_group=FakeGroup(), representation="glyphs")
    assert raised.value.reason == "svg_item_placement_unverified"
    assert raised.value.evidence["failed_placement_indices"] == [0, 2]
    assert doc.Objects == original_objects
    assert raised.value.evidence["created_entity_ids"] == []
