from copy import deepcopy
import json
import math
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

SRC = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"
sys.path.insert(0, str(SRC if SRC.exists() else Path(__file__).resolve().parent))
import PDFTextLayout as layout


def _source():
    chars = [{"c": c, "origin": (10+x, 20.), "bbox": (10+x, 17., 12+x, 21.)}
             for c, x in zip("AV Ω ", (0., 2.5, 7., 8.5, 12.))]
    span = {"font": "Arial", "size": 3., "origin": (10., 20.), "bbox": (10., 17., 24., 21.),
            "chars": chars}
    item = {"pdf_sha256": "a"*64, "page_number": 1, "block_index": 0,
            "line_index": 0, "span_index": 0, "source_item_id": "p1:b0:l0:s0",
            "text": "AV Ω ", "span": {"font": "Arial", "size": 3.}, "origin": span["origin"],
            "bbox": span["bbox"], "line_direction": (1., 0.)}
    raw = {"blocks": [{"type": 0, "lines": [{"dir": (1., 0.), "spans": [span]}]}]}
    return item, raw


def _payload():
    item, raw = _source()
    return layout.build_source_layout(item, raw, scale=2., font_size=6., font_name="Arial",
                                      host_rotation_deg=0.)


def test_character_layout_preserves_nonuniform_positions_unicode_and_spaces():
    p = _payload()
    assert "".join(c["text"] for c in p["characters"]) == "AV Ω "
    assert [c["local_origin"][0] for c in p["characters"]] == [0., 5., 14., 17., 24.]
    assert len(p["characters"]) == 5


@pytest.mark.parametrize("angle", [30., 90., 270.])
def test_source_world_offsets_are_inverse_rotated_into_object_coordinates(angle):
    item, raw = _source()
    p = layout.build_source_layout(item, raw, scale=2., font_size=6., font_name="Arial",
                                  host_rotation_deg=angle)
    x, y, z = p["characters"][1]["local_origin"]
    radians = math.radians(angle)
    assert (x*math.cos(radians)-y*math.sin(radians),
            x*math.sin(radians)+y*math.cos(radians), z) == pytest.approx((5., 0., 0.))


@pytest.mark.parametrize("fault", ["text", "font", "origin", "bbox", "direction", "partial",
                                   "nonfinite", "identity", "indices", "size"])
def test_incomplete_or_mismatched_raw_inventory_is_a_failure(fault):
    item, raw = _source()
    span = raw["blocks"][0]["lines"][0]["spans"][0]
    if fault == "text": span["chars"][0]["c"] = "X"
    if fault == "font": span["font"] = "Other"
    if fault == "size": span["size"] = 4.
    if fault == "origin": span["origin"] = (0., 20.)
    if fault == "bbox": span["bbox"] = (0., 17., 24., 21.)
    if fault == "direction": raw["blocks"][0]["lines"][0]["dir"] = (0., 1.)
    if fault == "partial": span["chars"].pop()
    if fault == "nonfinite": span["chars"][1]["origin"] = (float("nan"), 20.)
    if fault == "identity": item["source_item_id"] = "p2:b0:l0:s0"
    if fault == "indices": item["span_index"] = -1
    with pytest.raises(ValueError):
        layout.build_source_layout(item, raw, scale=2., font_size=6., font_name="Arial",
                                   host_rotation_deg=0.)


def test_page_rotation_and_translation_are_applied_before_local_projection():
    item, raw = _source()
    p = layout.build_source_layout(item, raw, scale=2., font_size=6., font_name="Arial",
                                  host_rotation_deg=-90., page_matrix=(0., 1., -1., 0., 500., 600.))
    assert p["characters"][1]["local_origin"] == pytest.approx((5., 0., 0.))


class Field:
    def __init__(self, value=None): self.value = value
    def setValue(self, *value): self.value = value[0] if len(value) == 1 else value
    def setValues(self, value): self.value = list(value)
    def getValue(self): return self
    def connectFrom(self, other): self.value = other.value


class VectorField(Field):
    def getValue(self): return SimpleNamespace(getValue=lambda: self.value)


class Group:
    def __init__(self): self.children = []
    def addChild(self, child): self.children.append(child)
    def findChild(self, child): return self.children.index(child) if child in self.children else -1
    def replaceChild(self, index, child): self.children[index] = child


class Text:
    LEFT = 0
    def __init__(self): self.string = Field(); self.justification = 0


class Font:
    def __init__(self): self.name, self.size = Field("Arial"), Field(6.)


class Transform:
    def __init__(self):
        self.translation = VectorField((0., 0., 0.))
        self.rotation = VectorField((0., 0., 0., 1.))


COIN = SimpleNamespace(SoSeparator=Group, SoTransform=Transform,
    SoScale=lambda: SimpleNamespace(scaleFactor=Field()), SoFont=Font,
    SoTranslation=lambda: SimpleNamespace(translation=Field()), SoAsciiText=Text)


def _object(*, label=False):
    obj = SimpleNamespace(Text=["AV Ω "], CustomText=["AV Ω "],
                          PDFSourceItemId="p1:b0:l0:s0", PDFRepresentation="labels" if label else "text")
    setattr(obj, layout.PROPERTY, json.dumps(_payload()))
    obj.Placement = SimpleNamespace(Base=SimpleNamespace(x=20., y=30., z=0.),
                                    Rotation=SimpleNamespace(Q=(0., 0., 0., 1.)))
    view = SimpleNamespace(Object=obj, FontSize=6., ScaleMultiplier=1., FontName="Arial",
                           Justification="Left", Frame="None", MaxChars=0)
    obj.ViewObject = view
    proxy = SimpleNamespace(font=Font(), text_wld=Text(), node_wld=Group())
    proxy.text_wld.string.setValues(obj.Text)
    proxy.node_wld.addChild(proxy.font); proxy.node_wld.addChild(proxy.text_wld)
    if label:
        proxy.node_wld_txt = proxy.node_wld
        proxy.textpos = Transform()
        proxy.textpos.translation.setValue((20.6, 24., 0.))
    def update(target, prop):
        proxy.text_wld.string.setValues(target.Text if not label else target.CustomText)
    proxy.updateData = update
    proxy.onChanged = lambda target, prop: None
    view.Proxy = proxy
    return obj


def _change_data(obj, prop, value):
    # FreeCAD caches the original bound callback; the module must not depend
    # on monkeypatching that method after the view provider was attached.
    callback = obj.ViewObject.Proxy.updateData
    observer = layout._AppObserver()
    observer.slotBeforeChangeObject(obj, prop)
    setattr(obj, prop, value)
    callback(obj, prop)
    observer.slotChangedObject(obj, prop)


def _change_view(obj, prop, value):
    view = obj.ViewObject
    callback = view.Proxy.onChanged
    observer = layout._GuiObserver()
    observer.slotBeforeChangeObject(view, prop)
    setattr(view, prop, value)
    callback(view, prop)
    observer.slotChangedObject(view, prop)


def test_native_nodes_stay_in_same_editable_draft_object_and_preserve_content():
    obj = _object(); proxy = obj.ViewObject.Proxy
    original = list(obj.Text)
    callbacks = (proxy.updateData, proxy.onChanged)
    result = layout.restore_object_layout(obj, coin_module=COIN)
    state = proxy._bcs_source_layout
    assert result["native_nodes_installed"]
    assert obj.Text == original
    assert proxy.node_wld.findChild(proxy.text_wld) == -1
    assert [t.string.value[0] for _, t in state["nodes"]] == list("AV Ω ")
    assert [t.translation.value[0] for t, _ in state["nodes"]] == [0., 5., 14., 17., 24.]
    assert obj.ViewObject.Proxy is proxy
    assert (proxy.updateData, proxy.onChanged) == callbacks


@pytest.mark.parametrize("label", [False, True])
def test_actual_text_edit_restores_native_renderer_and_undo_reenables_source(label):
    obj = _object(label=label); proxy = obj.ViewObject.Proxy
    layout.restore_object_layout(obj, coin_module=COIN)
    prop = "CustomText" if label else "Text"
    _change_data(obj, prop, ["Edited content"])
    assert proxy.node_wld.findChild(proxy.text_wld) >= 0
    assert proxy.text_wld.string.value == ["Edited content"]
    _change_data(obj, prop, ["AV Ω "])
    assert proxy._bcs_source_layout["active"]


def test_font_size_and_scale_edits_scale_characters_without_rewriting_source():
    obj = _object(); view = obj.ViewObject; proxy = view.Proxy
    encoded = getattr(obj, layout.PROPERTY)
    layout.restore_object_layout(obj, coin_module=COIN)
    _change_view(obj, "FontSize", 12.)
    _change_view(obj, "ScaleMultiplier", 3.)
    assert proxy._bcs_source_layout["scale"].scaleFactor.value == (6., 6., 6.)
    assert view.FontSize == 12. and view.ScaleMultiplier == 3.
    assert getattr(obj, layout.PROPERTY) == encoded


@pytest.mark.parametrize("prop,value", [("FontName", "Other"), ("Justification", "Center"),
                                        ("MaxChars", 5), ("Frame", "Rectangle"),
                                        ("DisplayMode", "Screen"), ("TextAlignment", "Top")])
def test_view_edits_use_normal_native_behavior(prop, value):
    obj = _object(); view = obj.ViewObject
    layout.restore_object_layout(obj, coin_module=COIN)
    _change_view(obj, prop, value)
    assert not view.Proxy._bcs_source_layout["active"]
    assert getattr(view, prop) == value


def test_label_margin_is_cancelled_and_new_placement_is_respected():
    obj = _object(label=True); proxy = obj.ViewObject.Proxy
    layout.restore_object_layout(obj, coin_module=COIN)
    correction = proxy._bcs_source_layout["correction"]
    assert correction.translation.value == pytest.approx((-.6, 6., 0.))
    obj.Placement.Base.x += 5.
    layout._AppObserver().slotRecomputedObject(obj)
    assert correction.translation.value == pytest.approx((4.4, 6., 0.))


def test_fresh_view_provider_restores_persisted_layout_without_touching_gui_settings():
    obj = _object(); encoded = getattr(obj, layout.PROPERTY)
    obj.ViewObject.FontSize = 9.
    result = layout.restore_object_layout(obj, coin_module=COIN)
    assert result["native_nodes_installed"]
    assert obj.ViewObject.FontSize == 9.
    assert getattr(obj, layout.PROPERTY) == encoded
    group = obj.ViewObject.Proxy._bcs_source_layout["group"]
    layout.restore_object_layout(obj, coin_module=COIN)
    assert obj.ViewObject.Proxy._bcs_source_layout["group"] is group


def test_corrupt_layout_is_rejected_before_changing_visible_nodes():
    obj = _object(); stock = obj.ViewObject.Proxy.text_wld
    payload = _payload(); payload["characters"].pop()
    setattr(obj, layout.PROPERTY, json.dumps(payload))
    with pytest.raises(ValueError): layout.restore_object_layout(obj, coin_module=COIN)
    assert obj.ViewObject.Proxy.node_wld.findChild(stock) >= 0


def test_observers_ignore_unrelated_objects_and_register_once(monkeypatch):
    monkeypatch.setattr(layout, "_observers", None)
    registered = []
    app = SimpleNamespace(addDocumentObserver=lambda obs: registered.append(("app", obs)))
    gui = SimpleNamespace(addDocumentObserver=lambda obs: registered.append(("gui", obs)))
    layout._ensure_observers(app, gui)
    layout._ensure_observers(app, gui)
    assert len(registered) == 2
    obj = SimpleNamespace(ViewObject=SimpleNamespace(Proxy=None))
    registered[0][1].slotBeforeChangeObject(obj, "Placement")
    registered[0][1].slotChangedObject(obj, "Placement")


def test_label_type_edit_returns_to_draft_generated_text():
    obj = _object(label=True)
    layout.restore_object_layout(obj, coin_module=COIN)
    _change_data(obj, "LabelType", "Label")
    assert not obj.ViewObject.Proxy._bcs_source_layout["active"]


def test_observer_does_not_dereference_unattached_or_destroyed_view():
    class Unattached:
        Proxy = None
        @property
        def Object(self):
            raise AssertionError("native Object is not attached yet")
    class Destroyed:
        @property
        def ViewObject(self):
            raise RuntimeError("wrapped C++ object already deleted")
    gui = layout._GuiObserver()
    gui.slotBeforeChangeObject(Unattached(), "Visibility")
    gui.slotChangedObject(Unattached(), "Visibility")
    layout._AppObserver().slotChangedObject(Destroyed(), "Visibility")
