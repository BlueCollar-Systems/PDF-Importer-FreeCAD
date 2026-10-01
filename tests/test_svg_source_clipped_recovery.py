"""Source-filter recovery and positive, complete off-clip glyph proofs."""
import copy
import math

import pytest

from test_svg_source_renderer_retry import COMPOSITE_SVG, deliver, setup_retry
from test_svg_text_representation_fc import (
    FakeDocument, FakeGroup, FakeShape, SVG, _install_renderer, _source_item,
    glyph_fill, renderer,
)


PARTIAL_COMPOSITE = COMPOSITE_SVG.replace(
    '</svg>', '<use href="#glyph-0-1" x="70" y="70"/></svg>')


@pytest.mark.parametrize('mode', ['glyphs', 'geometry'])
def test_annotation_uses_do_not_hide_unresolved_base_filter_text(monkeypatch, mode):
    calls = setup_retry(monkeypatch)
    monkeypatch.setattr(renderer, '_render_svg_with_pdftocairo', lambda *_args: PARTIAL_COMPOSITE)
    assert len(renderer._parse_use_placements(PARTIAL_COMPOSITE)) == 1
    doc, group = FakeDocument(), FakeGroup()
    item = _source_item(bbox=(8, 18, 18, 27), requested_type=mode)
    result = deliver(doc, group, item, {})
    assert result['renderer'] == 'pymupdf' and result['entity_type'] == mode
    assert [c[0] for c in calls] == ['pymupdf']
    proof = result['delivery_attempts'][0]['evidence']['renderer_recovery']
    assert proof['reason'] == 'source_glyph_filter_graph_unresolved'
    assert proof['filtered_glyph_sources'] == [
        {'filter_id': 'filter-0', 'source_id': 'compositing-group-1', 'glyph_use_count': 2}]
    assert result['item_filter']['matched_placement_indices'] == [0]


def test_unused_filter_and_non_glyph_filter_do_not_trigger_recovery():
    unused = COMPOSITE_SVG.replace(' filter="url(#filter-0)"', '')
    assert renderer._svg_filtered_glyph_sources(unused) == []
    assert renderer._svg_filtered_glyph_sources(SVG) == []
    other = COMPOSITE_SVG.replace('#glyph-0-1', '#ordinary-image')
    assert renderer._svg_filtered_glyph_sources(other) == []


def test_nested_active_filter_graph_reaches_base_glyphs_without_following_cycles():
    nested = PARTIAL_COMPOSITE.replace(
        '</defs>', '<g id="outer-source" filter="url(#filter-0)"><use href="#outer-source"/></g>'
        '<filter id="outer-filter"><feImage href="#outer-source"/></filter></defs>')
    nested = nested.replace('<g filter="url(#filter-0)"><rect', '<g filter="url(#outer-filter)"><rect')
    records = renderer._svg_filtered_glyph_sources(nested)
    assert records == [{'filter_id': 'filter-0', 'source_id': 'compositing-group-1', 'glyph_use_count': 2}]


CLIP = [[0.0, 0.0], [100.0, 0.0], [100.0, 100.0], [0.0, 100.0]]
PAINT = {'fill_rule': 'nonzero', 'clips': [CLIP]}


def proof(bounds, paint=PAINT, **kwargs):
    options = dict(vb_min_x=0., vb_min_y=0., vb_h=100.,
                   x_unit_to_mm=1., y_unit_to_mm=1., flip_y=True)
    options.update(kwargs)
    return renderer._fully_clipped_glyph_proof(FakeShape([], bounds), paint, **options)


@pytest.mark.parametrize('bounds', [(101, 0, 105, 5), (-5, 0, -1, 5),
                                  (0, 101, 5, 105), (0, -5, 5, -1)])
def test_complete_bounds_strictly_outside_are_proved(bounds):
    result = proof(bounds)
    assert result['host_glyph_bbox'] == list(bounds)
    assert result['exact_host_clip_bbox'] == ['0', '0', '100', '100']


@pytest.mark.parametrize('bounds', [(100, 0, 105, 5), (99, 0, 105, 5),
                                  (0, 0, 5, 5), (-1, -1, 101, 101)])
def test_touching_crossing_inside_or_surrounding_bounds_are_not_omitted(bounds):
    assert proof(bounds) is None


@pytest.mark.parametrize('paint', [None, {'fill_rule': None, 'clips': [CLIP]},
    {'error': 'unproved paint', **PAINT}, {'fill_rule': 'evenodd', 'clips': []},
    {'fill_rule': 'nonzero', 'clips': [[[0, 0], [1, 1], [0, 1], [1, 0]]]},
    {'fill_rule': 'nonzero', 'clips': [[[0, 0], [100, 0], [100, math.inf], [0, 100]]]}])
def test_unproved_or_stroked_paint_never_authorizes_omission(paint):
    assert proof((101, 0, 105, 5), paint) is None


def test_nested_source_clip_intersection_can_prove_invisibility():
    paint = {'fill_rule': 'evenodd', 'clips': [CLIP, [[0, 0], [10, 0], [10, 100], [0, 100]]]}
    result = proof((20, 0, 25, 5), paint)
    assert result['active_clip_index'] == 1


def test_affine_clip_bounds_reflection_and_fractional_scale_are_conservative():
    assert proof((40, 10, 42, 12), PAINT, vb_min_x=20., vb_min_y=10.,
                 x_unit_to_mm=0.5, y_unit_to_mm=0.25, flip_y=False) is None
    result = proof((math.nextafter(40., math.inf), 10, 42, 12), PAINT,
                   vb_min_x=20., vb_min_y=10., x_unit_to_mm=0.5,
                   y_unit_to_mm=0.25, flip_y=False)
    assert result is not None
    # A rotated rectangle's AABB is deliberately conservative: no corner-only
    # polygon test can drop a glyph while it intersects this complete bound.
    diamond = {'fill_rule': 'nonzero', 'clips': [[[0, 5], [5, 0], [10, 5], [5, 10]]]}
    assert proof((0, 90, 1, 91), diamond) is None


CLIPPED_SVG = '''<svg viewBox="0 0 100 100">
 <defs><path id="glyph-1" d="M0 0L5 0L5 5Z"/>
 <clipPath id="page"><path d="M0 0H100V100H0Z"/></clipPath></defs>
 <g clip-path="url(#page)"><use href="#glyph-1" x="10" y="20"/>
 <use href="#glyph-1" x="110" y="20"/></g></svg>'''


def test_only_proved_offclip_occurrence_is_removed_before_global_assignment(monkeypatch):
    actual_paints = glyph_fill.placement_fill_rules
    _install_renderer(monkeypatch)
    monkeypatch.setattr(glyph_fill, 'placement_fill_rules', actual_paints)
    monkeypatch.setattr(renderer, '_render_svg_with_pymupdf', lambda *_args: CLIPPED_SVG)
    item = _source_item(bbox=(8, 18, 18, 27), requested_type='geometry')
    cache = {'source_item_manifest': [{'source_order': 0, **{k: item[k] for k in (
        'source_item_id', 'page_number', 'pdf_sha256', 'bbox', 'text')}}]}
    result = deliver(FakeDocument(), FakeGroup(), item, cache)
    assert result['entity_type'] == 'geometry'
    assert result['item_filter']['matched_placement_indices'] == [0]
    assert cache['claimed_placement_indices'] == {0}
    assert cache['unmatched_placement_indices'] == []
    assert len(cache['clipped_placement_evidence']) == 1
    record = result['item_filter']['clipped_placement_evidence'][0]
    assert record['source_placement_index'] == 1 and record['source_glyph_id'] == 'glyph-1'
    assert record['page_number'] == 1 and record['pdf_sha256'] == 'a'*64
    assert len(record['svg_sha256']) == 64
    assert result['clipped_placement_evidence'] == cache['clipped_placement_evidence']
    # A partial crossing cannot acquire this proof or bypass source ownership.
    monkeypatch.setattr(renderer, '_render_svg_with_pymupdf',
                        lambda *_args: CLIPPED_SVG.replace('x="110"', 'x="98"'))
    cache2 = {'source_item_manifest': copy.deepcopy(cache['source_item_manifest'])}
    with pytest.raises(renderer.TextRepresentationRenderError) as error:
        deliver(FakeDocument(), FakeGroup(), item, cache2)
    assert error.value.reason == 'svg_global_assignment_unmatched'
    assert cache2['clipped_placement_evidence'] == []
