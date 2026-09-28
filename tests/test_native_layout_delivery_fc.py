"""A failed native layout never leaves an unreported successful host object."""
from types import SimpleNamespace
from pathlib import Path
import sys

import pytest
for path in (Path(__file__).resolve().parents[1] / 'PDFVectorImporter',
             Path(__file__).resolve().parents[1] / 'PDFVectorImporter/src'):
    sys.path.insert(0, str(path))
import PDFImporterCore as core
import PDFTextLayout as layout


@pytest.mark.parametrize('failure', ['invalid_inventory', 'native_install', 'inactive_display'])
def test_failed_layout_removes_only_its_owned_native_objects(monkeypatch, failure):
    host = SimpleNamespace(Name='NewText', ViewObject=object())
    existing = SimpleNamespace(Name='ExistingPart')
    objects = [existing, host]
    group = SimpleNamespace(objects=[host])
    doc = SimpleNamespace(Objects=objects,
        getObject=lambda name: next((o for o in objects if o.Name == name), None),
        removeObject=lambda name: objects.remove(next(o for o in objects if o.Name == name)))
    result = dict(source_item_id='p1:b0:l0:s0', requested_type='text', attempted_type='text',
                  final_type='text', outcome='verified', created_entity_ids=['NewText'],
                  evidence=dict(font_size=3., font_name='Arial', rotation_deg=0.))
    def fail(*_a, **_kw):
        raise ValueError('invalid source fixture')
    monkeypatch.setattr(layout, 'build_source_affine_layout', fail if failure == 'invalid_inventory' else lambda *_a, **_kw: {})
    monkeypatch.setattr(layout, 'persist_source_layout', fail if failure == 'native_install' else lambda *_a: {'native_nodes_installed':False})
    with pytest.raises(core.TextRepresentationFailure) as caught:
        core._bind_native_source_layout({}, result, {}, opts=core.ImportOptions(), scale=1., doc=doc, group=group)
    assert objects == [existing]
    assert group.objects == []
    assert caught.value.attempt['created_entity_ids'] == caught.value.attempt['removed_entity_ids'] == ['NewText']
    assert caught.value.attempt['cleanup_complete'] is True
    assert caught.value.attempt['outcome'] == 'failed'
    assert caught.value.attempt['reason'] == 'native_source_layout_failed'
