"""Image rendering sync preserves native content and owns only its callback links."""
from __future__ import annotations

import importlib.util
import importlib
import itertools
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src" / "PDFImageRenderSync.py"
POINTERS = itertools.count(100)


class _Name:
    def __init__(self, value):
        self.value = value

    def getString(self):
        return self.value


class _Type:
    def __init__(self, name):
        self.name = name

    def getName(self):
        return _Name(self.name)


class _Node:
    def __init__(self, kind, children=()):
        self.kind = kind
        self.children = list(children)
        self.this = next(POINTERS)
        self.refs = 0
        self.ref_events = []
        self.callback = None
        self.name = ""

    def getTypeId(self):
        return _Type(self.kind)

    def isOfType(self, kind):
        return self.kind == kind or kind == "Group" and self.kind in {"Separator", "Switch", "Root"}

    def getName(self):
        return _Name(self.name)

    def setName(self, name):
        self.name = name

    def getNumChildren(self):
        return len(self.children)

    def getChild(self, index):
        return self.children[index]

    def insertChild(self, node, index):
        self.children.insert(index, node)

    def removeChild(self, index):
        self.children.pop(index)

    def findChild(self, node):
        return next((i for i, child in enumerate(self.children) if int(child.this) == int(node.this)), -1)

    def setCallback(self, callback, data=None):
        self.callback = (callback, data)

    def ref(self):
        self.refs += 1
        self.ref_events.append("ref")

    def unref(self):
        assert self.refs > 0, "No borrowed/native baseline reference may be released"
        self.refs -= 1
        self.ref_events.append("unref")


def _class(kind):
    return SimpleNamespace(getClassTypeId=lambda: kind)


@pytest.fixture
def host(monkeypatch):
    """No CAD, Qt, OpenGL or Pivy binary is imported by these tests."""
    events = []
    observers = []
    gui_observers = []
    context = SimpleNamespace(isValid=lambda: True)
    context.functions = lambda: SimpleNamespace(glFinish=lambda: events.append("finish"))
    qtgui = SimpleNamespace(
        QOpenGLContext=SimpleNamespace(currentContext=lambda: context, functions=lambda: None),
        QOpenGLFunctions=SimpleNamespace(glFinish=lambda: None),
    )
    coin = SimpleNamespace(
        COIN_VERSION="4.0.3",
        COIN_MAJOR_VERSION=4, COIN_MINOR_VERSION=0, COIN_MICRO_VERSION=3,
        SoDB=SimpleNamespace(getVersion=lambda: "Coin 4.0.3"),
        SoNode=_class("Node"), SoGroup=_class("Group"),
        SoTexture2=_class("Texture2"), SoSeparator=_class("Separator"),
        SoGLRenderAction=_class("GLRenderAction"),
        SoCallback=lambda: _Node("Callback"),
        SoGLCacheContextElement=SimpleNamespace(get=lambda state: state.cache_id),
    )
    app = SimpleNamespace(
        GuiUp=True,
        addDocumentObserver=lambda observer: observers.append(observer),
        removeDocumentObserver=lambda observer: observers.remove(observer),
        Console=SimpleNamespace(PrintWarning=lambda value: events.append(("warning", value))),
    )
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "FreeCAD", app)
    gui = SimpleNamespace(
        addDocumentObserver=lambda observer: gui_observers.append(observer),
        removeDocumentObserver=lambda observer: gui_observers.remove(observer),
    )
    monkeypatch.setitem(sys.modules, "FreeCADGui", gui)
    monkeypatch.setitem(sys.modules, "pivy", SimpleNamespace(coin=coin))
    monkeypatch.setitem(sys.modules, "pivy.coin", coin)
    for binding in ("PySide6", "PySide2", "PySide"):
        monkeypatch.setitem(sys.modules, binding, SimpleNamespace(QtGui=qtgui))
        monkeypatch.setitem(sys.modules, binding + ".QtGui", qtgui)
    spec = importlib.util.spec_from_file_location("_image_render_sync_test", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    coord = _Node("Coordinate3")
    uv = _Node("TextureCoordinate2")
    texture = _Node("Texture2")
    texture.image = object()
    texture.model = 8448
    texture.wrapS = texture.wrapT = 10497
    material = _Node("Material")
    face = _Node("FaceSet")
    shading = _Node("Separator", (coord, uv, texture, _Node("ShapeHints"), material, face))
    unlit = _Node("Separator", (coord, uv, texture, _Node("ShapeHints"), material, _Node("LightModel"), face))
    modes = _Node("Switch", (shading, unlit))
    modes.name = "ModeSwitch"
    root = _Node("Root", (modes,))
    doc = SimpleNamespace(Name="Drawing")
    obj = SimpleNamespace(
        Name="PDF_Image_p1_i1", TypeId="Image::ImagePlane", Document=doc,
        PDFRepresentation="raster", PDFSourceItemId="p1:img1", ImageFile="original.png",
        XSize=54.1867, YSize=33.9513, Placement=object(),
        ViewObject=SimpleNamespace(RootNode=root, Visibility=True, DisplayMode="No shading"),
    )
    obj.ViewObject.Object = obj
    doc.Objects = [obj]
    doc.getObject = lambda name: obj if name == obj.Name else None
    return SimpleNamespace(
        module=module, coin=coin, app=app, qtgui=qtgui, context=context, events=events,
        observers=observers, gui=gui, gui_observers=gui_observers,
        obj=obj, doc=doc, root=root, parents=(shading, unlit), texture=texture,
    )


def _callbacks(host):
    result = []
    for parent in host.parents:
        result.extend(child for child in parent.children if child.kind == "Callback")
    return result


def _render(node, *, GL=True, cache_id=0):
    action = SimpleNamespace(
        isOfType=lambda kind: GL and kind == "GLRenderAction",
        getState=lambda: SimpleNamespace(cache_id=cache_id),
    )
    node.callback[0](node.callback[1], action)


def test_affected_image_gets_one_shared_pretexture_callback_without_content_changes(host):
    prior = [list(parent.children) for parent in host.parents]
    properties = dict(vars(host.obj)); view = dict(vars(host.obj.ViewObject))
    ok, note = host.module.ensure_image_render_sync(host.obj)
    assert ok, note
    callback, other = _callbacks(host)
    assert callback is other
    assert callback.refs == 1
    for parent, original in zip(host.parents, prior, strict=True):
        assert parent.children == original[:2] + [callback] + original[2:]
    assert vars(host.obj) == properties and vars(host.obj.ViewObject) == view


def test_idempotence_preserves_foreign_nodes_and_does_not_add_refs(host):
    foreign = _Node("Callback")
    host.parents[0].insertChild(foreign, 1)
    assert host.module.ensure_image_render_sync(host.obj)[0]
    prior = [list(parent.children) for parent in host.parents]
    refs = [list(parent.ref_events) for parent in host.parents]
    assert host.module.ensure_image_render_sync(host.obj)[0]
    assert [parent.children for parent in host.parents] == prior
    assert [parent.ref_events for parent in host.parents] == refs
    assert host.parents[0].children[1] is foreign


def test_only_glrender_actions_sync_every_pass_even_same_context(host):
    assert host.module.ensure_image_render_sync(host.obj)[0]
    callback = _callbacks(host)[0]
    _render(callback, GL=False)
    assert host.events == []
    _render(callback); _render(callback); _render(callback, cache_id=7)
    assert host.events == ["finish", "finish", "finish"]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_nonwindows_hosts_leave_native_graph_untouched(host, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    before = [list(parent.children) for parent in host.parents]
    host.module.ensure_image_render_sync(host.obj)
    assert [parent.children for parent in host.parents] == before
    assert not host.observers


@pytest.mark.parametrize("version", ["4.0.8", "4.0.10", "4.1.0", "5.0.0", "SIM Coin 4.0.8"])
def test_fixed_coin_versions_are_not_patched(host, version):
    host.coin.COIN_VERSION = version
    numeric = version.removeprefix("SIM Coin ")
    major, minor, micro = map(int, numeric.split("."))
    host.coin.COIN_MAJOR_VERSION, host.coin.COIN_MINOR_VERSION, host.coin.COIN_MICRO_VERSION = major, minor, micro
    host.coin.SoDB.getVersion = lambda: version if version.startswith("SIM Coin ") else "Coin " + version
    before = [list(parent.children) for parent in host.parents]
    host.module.ensure_image_render_sync(host.obj)
    assert [parent.children for parent in host.parents] == before


def test_actual_coin403_sodb_version_prefix_attaches_workaround(host):
    host.coin.SoDB.getVersion = lambda: "SIM Coin 4.0.3"
    ok, note = host.module.ensure_image_render_sync(host.obj)
    assert ok, note
    _render(_callbacks(host)[0])
    assert host.events == ["finish"]


def test_headless_image_is_not_patched(host):
    host.app.GuiUp = False
    host.module.ensure_image_render_sync(host.obj)
    assert not _callbacks(host)


def test_nonimage_object_is_not_patched(host):
    host.obj.TypeId = "Part::Feature"
    host.module.ensure_image_render_sync(host.obj)
    assert not _callbacks(host)


@pytest.mark.parametrize("missing", ["SoCallback", "SoGLRenderAction"])
def test_absent_coin_api_returns_without_changing_graph(host, missing):
    delattr(host.coin, missing)
    ok, note = host.module.ensure_image_render_sync(host.obj)
    assert not ok and note
    assert not _callbacks(host)


@pytest.mark.parametrize("fault", ["missing_context", "invalid_context", "missing_functions", "finish_throws"])
def test_callback_api_faults_never_escape_into_coin(host, fault):
    assert host.module.ensure_image_render_sync(host.obj)[0]
    if fault == "missing_context":
        host.qtgui.QOpenGLContext.currentContext = lambda: None
    elif fault == "invalid_context":
        host.context.isValid = lambda: False
    elif fault == "missing_functions":
        host.context.functions = lambda: SimpleNamespace()
    else:
        def broken():
            raise RuntimeError("native call failed")
        host.context.functions = lambda: SimpleNamespace(glFinish=broken)
    _render(_callbacks(host)[0])
    assert "finish" not in host.events


@pytest.mark.parametrize("delete", ["object", "document"])
def test_deletion_removes_only_own_edges_and_releases_parent_refs(host, delete):
    foreign = _Node("Callback")
    host.parents[0].insertChild(foreign, 0)
    original = [list(parent.children) for parent in host.parents]
    assert host.module.ensure_image_render_sync(host.obj)[0]
    callback = next(child for child in host.parents[1].children if child.kind == "Callback")
    assert callback.refs == 1
    assert len(host.observers) == 1
    observer = host.observers[0]
    if delete == "object":
        observer.slotDeletedObject(host.obj)
        observer.slotDeletedObject(host.obj)
    else:
        observer.slotDeletedDocument(host.doc)
        observer.slotDeletedDocument(host.doc)
    assert [parent.children for parent in host.parents] == original
    assert all(parent.refs == 0 for parent in host.parents)
    assert callback.refs == 0


def test_other_document_deletion_does_not_remove_live_callback(host):
    assert host.module.ensure_image_render_sync(host.obj)[0]
    before = [list(parent.children) for parent in host.parents]
    host.observers[0].slotDeletedDocument(SimpleNamespace(Name="Other"))
    assert [parent.children for parent in host.parents] == before


def test_second_parent_native_mutate_then_throw_rolls_back_own_edges(host):
    before = [list(parent.children) for parent in host.parents]
    def fail_after_mutation(node, index):
        host.parents[1].children.insert(index, node)
        raise RuntimeError("native insertion failed after mutation")
    host.parents[1].insertChild = fail_after_mutation
    ok, note = host.module.ensure_image_render_sync(host.obj)
    assert not ok and note
    assert [parent.children for parent in host.parents] == before
    assert all(parent.refs == 0 for parent in host.parents)


@pytest.mark.parametrize("version", ["unknown", "Coin 4.0", "Coin 4.0.8-custom"])
def test_unknown_runtime_version_is_reported_without_mutating_native_graph(host, version):
    host.coin.SoDB.getVersion = lambda: version
    before = [list(parent.children) for parent in host.parents]
    ok, note = host.module.ensure_image_render_sync(host.obj)
    assert not ok and "version" in note
    assert [parent.children for parent in host.parents] == before
    assert not host.module._BINDINGS


def test_missing_qt_context_api_refuses_before_native_graph_changes(host):
    del host.qtgui.QOpenGLContext.currentContext
    before = [list(parent.children) for parent in host.parents]
    ok, note = host.module.ensure_image_render_sync(host.obj)
    assert not ok and "API" in note
    assert [parent.children for parent in host.parents] == before
    assert not host.module._BINDINGS


def test_qt5_binding_remains_supported_when_qt6_is_absent(host, monkeypatch):
    monkeypatch.setitem(sys.modules, "PySide6", None)
    assert host.module.ensure_image_render_sync(host.obj)[0]
    _render(_callbacks(host)[0])
    assert host.events == ["finish"]


def test_replaced_native_root_releases_old_parents_before_reattaching(host):
    assert host.module.ensure_image_render_sync(host.obj)[0]
    old_callback = _callbacks(host)[0]
    old_parents = host.parents
    new_parents = tuple(_Node("Separator", [child for child in parent.children if child is not old_callback]) for parent in old_parents)
    new_root = _Node("Root", (_Node("Switch", new_parents),))
    host.obj.ViewObject.RootNode = new_root
    host.parents = new_parents
    assert host.module.ensure_image_render_sync(host.obj)[0]
    assert all(parent.refs == 0 and old_callback not in parent.children for parent in old_parents)
    new_callback = _callbacks(host)[0]
    assert new_callback is not old_callback
    _render(old_callback)
    assert host.events == []
    _render(new_callback)
    assert host.events == ["finish"]


def test_missing_own_edge_is_repaired_without_removing_foreign_nodes(host):
    assert host.module.ensure_image_render_sync(host.obj)[0]
    old_callback = _callbacks(host)[0]
    foreign = _Node("Callback")
    host.parents[0].children.remove(old_callback)
    host.parents[0].insertChild(foreign, 0)
    assert host.module.ensure_image_render_sync(host.obj)[0]
    assert foreign in host.parents[0].children
    for parent in host.parents:
        assert old_callback not in parent.children
        assert parent.ref_events == ["ref", "unref", "ref"]


def test_parent_ref_failure_rolls_back_prior_edges_and_refs(host):
    before = [list(parent.children) for parent in host.parents]
    def fail_ref():
        raise RuntimeError("native ref unavailable")
    host.parents[1].ref = fail_ref
    ok, note = host.module.ensure_image_render_sync(host.obj)
    assert not ok and "ref" in note
    assert [parent.children for parent in host.parents] == before
    assert all(parent.refs == 0 for parent in host.parents)
    assert not host.module._BINDINGS


def test_cleanup_failure_retains_only_failed_parent_and_allows_later_retry(host):
    before = [list(parent.children) for parent in host.parents]
    assert host.module.ensure_image_render_sync(host.obj)[0]
    callback = _callbacks(host)[0]
    original_remove = host.parents[0].removeChild
    def fail_remove(index):
        raise RuntimeError("native unlink unavailable")
    host.parents[0].removeChild = fail_remove
    host.module.release_object(host.obj)
    assert len(host.module._BINDINGS) == 1
    assert host.parents[0].refs == 1 and host.parents[1].refs == 0
    assert callback.refs == 1
    assert host.parents[1].children == before[1]
    host.parents[0].removeChild = original_remove
    host.module.release_object(host.obj)
    assert not host.module._BINDINGS
    assert [parent.children for parent in host.parents] == before
    assert all(parent.refs == 0 for parent in host.parents)
    assert callback.refs == 0


def test_unregister_preserves_observer_until_failed_native_unlink_can_retry(host):
    assert host.module.ensure_image_render_sync(host.obj)[0]
    original_remove = host.parents[0].removeChild
    def fail_remove(index):
        raise RuntimeError("native unlink unavailable")
    host.parents[0].removeChild = fail_remove
    host.module.unregister_document_observer()
    assert host.module._BINDINGS and len(host.observers) == 1
    host.parents[0].removeChild = original_remove
    host.module.unregister_document_observer()
    assert not host.module._BINDINGS and not host.observers
    assert all(parent.refs == 0 for parent in host.parents)


def test_transient_callback_error_does_not_suppress_later_valid_traversal(host):
    assert host.module.ensure_image_render_sync(host.obj)[0]
    callback = _callbacks(host)[0]
    host.qtgui.QOpenGLContext.currentContext = lambda: None
    _render(callback)
    assert "finish" not in host.events
    host.qtgui.QOpenGLContext.currentContext = lambda: host.context
    _render(callback)
    assert host.events[-1] == "finish"


@pytest.mark.parametrize("missing", ["context_functions", "glFinish"])
def test_missing_static_qt_function_api_refuses_before_native_graph_changes(host, missing):
    if missing == "context_functions":
        del host.qtgui.QOpenGLContext.functions
    else:
        del host.qtgui.QOpenGLFunctions.glFinish
    before = [list(parent.children) for parent in host.parents]
    ok, note = host.module.ensure_image_render_sync(host.obj)
    assert not ok and note
    assert [parent.children for parent in host.parents] == before
    assert not host.module._BINDINGS


def _canonical_hooks(host, monkeypatch):
    monkeypatch.syspath_prepend(str(SOURCE.parent))
    monkeypatch.setitem(sys.modules, "PDFImageRenderSync", host.module)
    def wrong_alias(_obj):
        raise AssertionError("A second package helper alias must not own another callback registry")
    monkeypatch.setitem(
        sys.modules, "PDFVectorImporter.src.PDFImageRenderSync",
        SimpleNamespace(ensure_image_render_sync=wrong_alias),
    )
    return (
        importlib.import_module("PDFImporterCore"),
        importlib.import_module("PDFStyleRestore"),
    )


def test_fresh_import_hook_and_gui_restore_share_one_canonical_binding(host, monkeypatch):
    core, restore = _canonical_hooks(host, monkeypatch)
    host.obj.PDFRepresentation = ""
    host.obj.PDFSourceItemId = ""
    host.obj.ViewObject.DisplayMode = "Shading"
    core._annotate_text_host_object(host.obj, "p1:img1", "raster")
    assert host.obj.PDFRepresentation == "raster" and host.obj.PDFSourceItemId == "p1:img1"
    assert host.obj.ViewObject.DisplayMode == "No shading"
    callback = _callbacks(host)[0]
    binding = next(iter(host.module._BINDINGS.values()))
    # A user subsequently changes native styles; reopening restores only the
    # runtime callback, using the same canonical module as the fresh import.
    host.obj.ViewObject.DisplayMode = "Shading"
    host.obj.ViewObject.Visibility = False
    host.obj.ViewObject.ShapeColor = (0.25, 0.5, 0.75)
    before = dict(vars(host.obj.ViewObject))
    restore.restore_document_display_nodes(host.doc)
    assert vars(host.obj.ViewObject) == before
    assert next(iter(host.module._BINDINGS.values())) is binding
    assert _callbacks(host) == [callback, callback]
    assert len(host.observers) == 1
    _render(callback)
    assert host.events == ["finish"]


def test_gui_saved_display_hook_reattaches_without_reapplying_saved_user_styles(host, monkeypatch, tmp_path):
    import zipfile
    _, restore = _canonical_hooks(host, monkeypatch)
    saved = tmp_path / "GUI-saved.FCStd"
    with zipfile.ZipFile(saved, "w") as archive:
        archive.writestr("GuiDocument.xml", "<GuiDocument/>")
    host.doc.FileName = str(saved)
    host.obj.ViewObject.DisplayMode = "Shading"
    host.obj.ViewObject.Visibility = False
    host.obj.ViewObject.Lighting = "Two side"
    host.obj.ViewObject.ShapeColor = (0.8, 0.2, 0.4)
    host.obj.ViewObject.Transparency = 41
    view = dict(vars(host.obj.ViewObject)); properties = dict(vars(host.obj))
    needed, _ = restore.document_needs_style_restore(host.doc)
    assert not needed  # GUI-saved files must not be forced back to PDF styles.
    restore.restore_document_display_nodes(host.doc)
    assert vars(host.obj.ViewObject) == view and vars(host.obj) == properties
    callback, same = _callbacks(host)
    assert callback is same and callback.refs == 1 and len(host.observers) == 1
    _render(callback)
    assert host.events == ["finish"]


@pytest.mark.parametrize("prop", ["Visibility", "DisplayMode"])
def test_saved_gui_before_change_attaches_while_global_restore_is_still_active(host, prop):
    host.app.isRestoring = lambda: True
    host.doc.Restoring = True
    host.obj.ViewObject.Visibility = False
    host.obj.ViewObject.ShapeColor = (0.1, 0.3, 0.9)
    props = dict(vars(host.obj)); view = dict(vars(host.obj.ViewObject))
    observer = host.module.register_gui_document_observer(host.gui)
    observer.slotBeforeChangeObject(host.obj.ViewObject, prop)
    # Simulate the next native show/mode update: the first texture traversal
    # already has the guard, without waiting for global/style restoration.
    _render(_callbacks(host)[0])
    assert host.events == ["finish"]
    assert vars(host.obj) == props and vars(host.obj.ViewObject) == view


def test_gui_observer_and_late_restore_share_one_binding_without_duplicate_edges(host, monkeypatch):
    _, restore = _canonical_hooks(host, monkeypatch)
    observer = host.module.register_gui_document_observer(host.gui)
    assert host.module.register_gui_document_observer(host.gui) is observer
    observer.slotBeforeChangeObject(host.obj.ViewObject, "Visibility")
    binding = next(iter(host.module._BINDINGS.values()))
    callback = binding.node
    observer.slotBeforeChangeObject(host.obj.ViewObject, "DisplayMode")
    restore.restore_document_display_nodes(host.doc)
    assert next(iter(host.module._BINDINGS.values())) is binding
    assert _callbacks(host) == [callback, callback]
    assert len(host.gui_observers) == len(host.observers) == 1


@pytest.mark.parametrize("case", ["foreign_type", "native_nonimporter", "no_object", "other_property"])
def test_gui_before_change_leaves_unrelated_views_untouched(host, case):
    view = host.obj.ViewObject
    prop = "Visibility"
    if case == "foreign_type":
        host.obj.TypeId = "Part::Feature"
    elif case == "native_nonimporter":
        host.obj.PDFRepresentation = ""
    elif case == "no_object":
        view = SimpleNamespace()
    else:
        prop = "ShapeAppearance"
    observer = host.module.register_gui_document_observer(host.gui)
    observer.slotBeforeChangeObject(view, prop)
    assert not host.module._BINDINGS and not _callbacks(host)
    assert not host.events


@pytest.mark.parametrize("unsupported", ["fixed_coin", "non_windows"])
def test_early_hook_keeps_unsupported_runtime_rendering_unchanged(host, monkeypatch, unsupported):
    if unsupported == "non_windows":
        monkeypatch.setattr(sys, "platform", "linux")
    else:
        host.coin.SoDB.getVersion = lambda: "SIM Coin 4.0.8"
    observer = host.module.register_gui_document_observer(host.gui)
    if observer is not None:
        observer.slotBeforeChangeObject(host.obj.ViewObject, "Visibility")
    assert not host.module._BINDINGS and not _callbacks(host)
    assert not host.events


@pytest.mark.parametrize("missing", ["addDocumentObserver", "removeDocumentObserver"])
def test_legacy_gui_without_observer_api_keeps_late_attachment_available(host, missing):
    delattr(host.gui, missing)
    assert host.module.register_gui_document_observer(host.gui) is None
    assert not host.gui_observers
    assert host.module.ensure_image_render_sync(host.obj)[0]
    _render(_callbacks(host)[0])
    assert host.events[-1] == "finish"


def test_gui_callback_error_cannot_abort_native_property_restoration(host):
    observer = host.module.register_gui_document_observer(host.gui)
    class DeletedView:
        @property
        def Object(self):
            raise RuntimeError("provider was deleted")
    observer.slotBeforeChangeObject(DeletedView(), "Visibility")
    observer.slotBeforeChangeObject(DeletedView(), "Visibility")
    assert not host.module._BINDINGS
    assert len(host.events) == 1 and "provider was deleted" in host.events[0][1]


def test_gui_registration_failure_is_nonfatal_and_can_retry(host):
    original = host.gui.addDocumentObserver
    def failed(_observer):
        raise RuntimeError("observer API refused registration")
    host.gui.addDocumentObserver = failed
    assert host.module.register_gui_document_observer(host.gui) is None
    assert host.module._GUI_OBSERVER is None and not host.gui_observers
    host.gui.addDocumentObserver = original
    assert host.module.register_gui_document_observer(host.gui) is not None


def test_shutdown_releases_native_links_and_both_observers_and_can_register_again(host):
    observer = host.module.register_gui_document_observer(host.gui)
    observer.slotBeforeChangeObject(host.obj.ViewObject, "Visibility")
    binding = next(iter(host.module._BINDINGS.values()))
    host.module.unregister_document_observer()
    assert not host.gui_observers and not host.observers and not host.module._BINDINGS
    assert not binding.active and not binding.retained_node and not binding.retained_parents
    assert host.module._GUI_OBSERVER_HOST is None
    assert host.module.register_gui_document_observer() is not observer


def test_gui_remove_failure_retains_observer_identity_for_cleanup_retry(host):
    observer = host.module.register_gui_document_observer(host.gui)
    remove = host.gui.removeDocumentObserver
    def failed(_observer):
        raise RuntimeError("GUI observer removal unavailable")
    host.gui.removeDocumentObserver = failed
    host.module.unregister_document_observer()
    assert host.module._GUI_OBSERVER is observer and host.gui_observers == [observer]
    host.gui.removeDocumentObserver = remove
    host.module.unregister_document_observer()
    assert host.module._GUI_OBSERVER is None and not host.gui_observers


def test_gui_host_replacement_removes_only_old_observer_without_detaching_image(host):
    old = host.module.register_gui_document_observer(host.gui)
    old.slotBeforeChangeObject(host.obj.ViewObject, "Visibility")
    binding = next(iter(host.module._BINDINGS.values()))
    new_observers = []
    other_gui = SimpleNamespace(
        addDocumentObserver=new_observers.append,
        removeDocumentObserver=new_observers.remove,
    )
    new = host.module.register_gui_document_observer(other_gui)
    assert new is not old and not host.gui_observers and new_observers == [new]
    assert host.module._GUI_OBSERVER_HOST is other_gui
    assert next(iter(host.module._BINDINGS.values())) is binding and binding.linked()


def test_document_deletion_keeps_startup_observer_ready_for_the_next_restore(host):
    observer = host.module.register_gui_document_observer(host.gui)
    observer.slotBeforeChangeObject(host.obj.ViewObject, "Visibility")
    binding = next(iter(host.module._BINDINGS.values()))
    host.observers[0].slotDeletedDocument(host.doc)
    assert not host.module._BINDINGS and not binding.retained_node
    assert host.gui_observers == [observer]
    observer.slotBeforeChangeObject(host.obj.ViewObject, "Visibility")
    assert next(iter(host.module._BINDINGS.values())) is not binding
    assert host.module._BINDINGS and len(host.observers) == 1


@pytest.mark.parametrize("style_hook_fails", [False, True])
def test_gui_startup_registers_canonical_helper_without_loading_core(host, monkeypatch, tmp_path, style_hook_fails):
    import ast
    import os
    addon = tmp_path / "Mod" / "PDFVectorImporter"
    addon.mkdir(parents=True)
    host.app.getUserAppDataDir = lambda: str(tmp_path)
    host.app.getResourceDir = lambda: str(tmp_path / "unused")
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setitem(sys.modules, "PDFImageRenderSync", host.module)
    monkeypatch.delitem(sys.modules, "PDFImporterCore", raising=False)
    def register_style(_app):
        if style_hook_fails:
            raise RuntimeError("style hook unavailable")
        return object()
    monkeypatch.setitem(sys.modules, "PDFStyleRestore", SimpleNamespace(register_document_observer=register_style))
    def wrong_alias(_gui):
        raise AssertionError("GUI startup must share the canonical image helper")
    monkeypatch.setitem(sys.modules, "PDFVectorImporter.src.PDFImageRenderSync", SimpleNamespace(register_gui_document_observer=wrong_alias))
    source = SOURCE.parents[1] / "InitGui.py"
    tree = ast.parse(source.read_text(encoding="utf8"))
    startup = tree.body[-1]
    assert isinstance(startup, ast.Try)
    code = compile(ast.Module(body=[startup], type_ignores=[]), str(source), "exec")
    namespace = {"os": os, "sys": sys, "FreeCAD": host.app, "FreeCADGui": host.gui}
    exec(code, namespace)
    exec(code, namespace)
    assert len(host.gui_observers) == 1
    assert "PDFImporterCore" not in sys.modules
    host.gui_observers[0].slotBeforeChangeObject(host.obj.ViewObject, "Visibility")
    assert host.module._BINDINGS and len(host.observers) == 1
