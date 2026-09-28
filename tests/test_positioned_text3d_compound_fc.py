"""Native solid assembly preserves per-character source transforms."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

for path in (Path(__file__).resolve().parents[1] / 'PDFVectorImporter',
             Path(__file__).resolve().parents[1] / 'PDFVectorImporter/src'):
    sys.path.insert(0, str(path))
import PDFImporterCore as core


class Shape:
    def __init__(self, points, solids=1):
        self.points = points
        self.Volume = float(solids)
        self.Vertexes = [SimpleNamespace(Point=SimpleNamespace(x=x, y=y, z=z)) for x, y, z in points]
        center = SimpleNamespace(**dict(zip(('x', 'y', 'z'),
            (sum(p[i] for p in points)/len(points) for i in range(3)), strict=True)))
        self.Solids = [SimpleNamespace(Volume=1., CenterOfMass=center) for _ in range(solids)]

    def isNull(self):
        return False

    def transformGeometry(self, m):
        result = Shape([(m.A11*x+m.A12*y+m.A14,
                       m.A21*x+m.A22*y+m.A24, z+m.A34)
                      for x, y, z in self.points], len(self.Solids))
        result.Volume = self.Volume * abs(m.A11*m.A22 - m.A12*m.A21)
        return result


@pytest.fixture
def host(monkeypatch):
    calls = []
    def bake(**kwargs):
        calls.append(kwargs)
        return Shape([(0., 0., 0.), (kwargs['target_advance_fc'], 1., kwargs['depth'])]), 1., 1., 1.
    monkeypatch.setattr(core, '_build_exact_text3d_compound_shape', bake)
    monkeypatch.setattr(core, '_source_em_text3d_pen_advance', lambda *_a: 2.)
    monkeypatch.setattr(core, 'FreeCAD', SimpleNamespace(Matrix=SimpleNamespace,
        Vector=lambda x, y, z: SimpleNamespace(x=x, y=y, z=z)))
    def compound(shapes):
        result = Shape([p for s in shapes for p in s.points], sum(len(s.Solids) for s in shapes))
        result.Volume = sum(shape.Volume for shape in shapes)
        return result
    monkeypatch.setattr(core, 'Part', SimpleNamespace(Compound=compound))
    monkeypatch.setattr(core, '_ACTIVE_TEXT3D_OUTLINE_MEMO', None)
    return calls


def character(text, x, y, advance=2., baseline=(1., 0.), up=(0., 1.), up_scale=1., baseline_scale=1.):
    return dict(text=text, local_origin=[x, y, 0.], advance=advance,
                baseline_axis=baseline, up_axis=up, up_scale=up_scale,
                baseline_scale=baseline_scale)


def build(chars, text='14'):
    return core._build_positioned_text3d_compound_shape(
        source_text=text, font_path='exact-source.ttf', font_size_fc=3.5,
        depth=.42, target_advance_fc=4.134,
        source_character_layout={'characters': chars})


def test_stacked_fraction_keeps_two_different_source_baselines(host):
    shape, stretch, _, _, volume, solids = build([
        character('1', 0., 0.), character('4', 2.161, -1.3975)])
    assert shape.points[0] == (0., 0., 0.)
    assert shape.points[2] == (2.161, -1.3975, 0.)
    assert stretch == 1.0
    assert solids == volume == 2
    assert [call['source_text'] for call in host] == ['1', '4']
    assert all(call['font_size_fc'] == 3.5 for call in host)


def test_source_rotation_shear_and_nonuniform_advance_are_retained(host):
    shape = build([character('1', 5., -2., 4., baseline=(0., 1.), up=(-.8, .6), baseline_scale=2.),
                   character('4', 8., -5., 1., baseline_scale=.5)])[0]
    assert shape.points[1] == pytest.approx((4.2, 2.6, .42))
    assert [call['target_advance_fc'] for call in host] == [2., 2.]


def test_declared_pdf_advance_does_not_stretch_source_glyph_ink(host):
    shape = build([character('1', 0., 0., advance=20.),
                   character('4', 20., -1., advance=40.)])[0]
    assert shape.points[1][0] == 2.
    assert shape.points[3][0] == 22.
    assert [call['target_advance_fc'] for call in host] == [2., 2.]


def test_expensive_native_solid_mass_properties_are_measured_once_per_check(host, monkeypatch):
    measured = []
    class Solid:
        def __init__(self, original):
            self.volume, self.center = original.Volume, original.CenterOfMass
            self.volume_reads = self.center_reads = 0
            measured.append(self)
        @property
        def Volume(self):
            self.volume_reads += 1
            return self.volume
        @property
        def CenterOfMass(self):
            self.center_reads += 1
            return self.center
    original_init = Shape.__init__
    def make_shape(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.Solids = [Solid(solid) for solid in self.Solids]
    monkeypatch.setattr(Shape, '__init__', make_shape)
    build([character('1', 0., 0.), character('4', 2., -1.)])
    checked = [solid for solid in measured if solid.center_reads]
    assert len(checked) == 4  # Both original and transformed solids, both glyphs.
    assert all(solid.volume_reads == solid.center_reads == 1 for solid in checked)


def test_whitespace_keeps_position_without_manufacturing_solids(host):
    shape = build([character('1', 0., 0.), character(' ', 2., 0.),
                   character('4', 7., -1.)], text='1 4')[0]
    assert len(shape.Solids) == 2
    assert shape.points[2] == (7., -1., 0.)
    assert len(host) == 2


def test_actual_source_vertical_matrix_magnitude_is_not_unitized(host):
    shape = build([character('1', 0., 0., up_scale=2.),
                   character('4', 7., -1., up=(-.6, .8), up_scale=1.25)])[0]
    assert shape.points[1] == pytest.approx((2., 2., .42))
    assert shape.points[3] == pytest.approx((8.25, 0., .42))
    assert shape.Volume == pytest.approx(3.)


def test_compound_cannot_lose_verified_affine_volume(host, monkeypatch):
    original = core.Part.Compound
    def wrong_volume(shapes):
        result = original(shapes)
        result.Volume *= .5
        return result
    monkeypatch.setattr(core.Part, 'Compound', wrong_volume)
    with pytest.raises(RuntimeError, match='compound failed verification'):
        build([character('1', 0., 0.), character('4', 2., -1.)])


def test_missing_character_is_not_an_accepted_compound(host):
    with pytest.raises(ValueError, match='incomplete'):
        build([character('1', 0., 0.)])
    assert host == []


def test_transform_geometry_failure_is_never_silently_flattened(host, monkeypatch):
    monkeypatch.setattr(Shape, 'transformGeometry', lambda *_args: None)
    with pytest.raises(RuntimeError, match='lost solid geometry'):
        build([character('1', 0., 0.), character('4', 2., -1.)])


def test_ignored_character_translation_fails_even_with_preserved_solid_count(host, monkeypatch):
    monkeypatch.setattr(Shape, 'transformGeometry', lambda self, _matrix: self)
    with pytest.raises(RuntimeError, match='affine coordinates'):
        build([character('1', 0., 0.), character('4', 2.161, -1.3975)])


def test_affine_volume_loss_is_not_accepted(host, monkeypatch):
    original = Shape.transformGeometry
    def wrong_volume(self, matrix):
        result = original(self, matrix)
        result.Volume *= .5
        return result
    monkeypatch.setattr(Shape, 'transformGeometry', wrong_volume)
    with pytest.raises(RuntimeError, match='affine volume'):
        build([character('1', 0., 0.), character('4', 2., -1.)])


def test_host_assignment_cannot_drop_the_verified_solids(monkeypatch):
    class DroppingHost:
        Name = 'BadHost'
        TypeId = 'Part::Feature'

        @property
        def Shape(self):
            return self._shape

        @Shape.setter
        def Shape(self, _value):
            self._shape = Shape([(0., 0., 0.)], solids=0)

    objects = []
    host = DroppingHost()
    def add(*_args):
        objects.append(host)
        return host
    document = SimpleNamespace(Objects=objects, addObject=add,
        getObject=lambda name: next((o for o in objects if o.Name == name), None),
        removeObject=lambda _name: objects.remove(host))
    group = SimpleNamespace(objects=[], addObject=lambda obj: None)
    monkeypatch.setattr(core, '_build_positioned_text3d_compound_shape',
        lambda **_kw: (Shape([(0., 0., 0.)], 2), 1., 4.134, 4.134, 2., 2))
    with pytest.raises(RuntimeError, match='changed positioned source glyph geometry'):
        core._create_verified_compound_text3d_entity(document, source_text='14',
            font_path='exact.ttf', font_size_fc=3.5, depth=.42, target_advance_fc=4.134,
            source_character_layout={'characters': []}, placement=None, text_group=group)
    assert objects == []


def test_positioned_delivery_failure_cleans_owned_objects_without_baseline_fallback(monkeypatch):
    import test_freecad_representation_contract as fixture
    document, draft, group = fixture._install_host(monkeypatch)
    item = fixture._canonical_3d_item('14')
    monkeypatch.setattr(core, '_resolve_shapestring_font_path_with_evidence',
                        lambda *_a, **_kw: fixture._found_font_resolution(item))
    monkeypatch.setitem(sys.modules, 'PDFText3DLayout', SimpleNamespace(
        build_source_character_layout=lambda *_a, **_kw: {'characters': [
            character('1', 0., 0.), character('4', 2., -1.)]}))
    def fail_after_creation(doc, **_kwargs):
        doc.Objects.append(fixture.FakeExtrusion(doc, 'Partial3D'))
        raise RuntimeError('source character transform failed')
    monkeypatch.setattr(core, '_create_verified_compound_text3d_entity', fail_after_creation)
    with pytest.raises(core.TextRepresentationFailure) as caught:
        core._deliver_text_item_3d(item, '3d_text', core.ImportOptions(text_mode='3d_text'),
                                  text_group=group, page_h=100., scale=1., raw_source_dict={})
    assert caught.value.attempt['reason'] == 'positioned_3d_text_failed'
    assert caught.value.attempt['cleanup_complete'] is True
    assert caught.value.attempt['created_entity_ids'] == caught.value.attempt['removed_entity_ids']
    assert caught.value.attempt['created_entity_ids']
    assert document.Objects == []
    assert draft.calls == []


@pytest.mark.parametrize('whole_missing', [False, True])
def test_one_zero_outline_cannot_authorize_whole_item_fallback(host, monkeypatch, whole_missing):
    def one_missing(**_kwargs):
        raise core.Text3DExactFontOutlinesUnavailable({'source_text_length': 1})
    def whole_probe(text, font):
        assert (text, font) == ('14', 'exact-source.ttf')
        if whole_missing:
            raise core.Text3DExactFontOutlinesUnavailable({'source_text_length': 2})
        return object()
    monkeypatch.setattr(core, '_build_exact_text3d_compound_shape', one_missing)
    monkeypatch.setattr(core, '_build_exact_text3d_outline_template', whole_probe)
    if whole_missing:
        with pytest.raises(core.Text3DExactFontOutlinesUnavailable) as caught:
            build([character('1', 0., 0.), character('4', 2., -1.)])
        assert caught.value.evidence['source_text_length'] == 2
    else:
        with pytest.raises(RuntimeError, match='isolated source glyph'):
            build([character('1', 0., 0.), character('4', 2., -1.)])
