"""Keep native PDF images safe on Windows Coin releases before 4.0.8.

Coin issue #609 / fix #611 documents an Intel driver returning a null
GL_VERSION during texture display-list creation. The upstream fix is in Coin
4.0.8; glFinish is a demonstrated workaround on affected older runtimes. On
older Windows runtimes only, an image-local callback synchronizes queued
graphics work immediately before native SoTexture2 traversal. Native
pixels, material, geometry, visibility and user view settings stay untouched.

Host imports are lazy.  No OpenGL context is created or made current here.
"""
from __future__ import annotations

import re
import sys


_BINDINGS = {}
_OBSERVER = None
_OBSERVER_HOST = None
_GUI_OBSERVER = None
_GUI_OBSERVER_HOST = None
_NOTES = set()
_SERIAL = 0
_MAX_NODES = 256


def _warn(note):
    if note in _NOTES:
        return
    _NOTES.add(note)
    try:
        import FreeCAD
        FreeCAD.Console.PrintWarning("PDF image render sync: %s\n" % note)
    except Exception:
        pass


def _key(obj):
    return (str(obj.Document.Name), str(obj.Name))


def _pointer(node):
    return int(node.this)


def _children(parent):
    count = int(parent.getNumChildren())
    if count < 0 or count > _MAX_NODES:
        raise ValueError("native image child count exceeds inspection limit")
    return [parent.getChild(index) for index in range(count)]


def _texture_parents(root, coin):
    """Read the small native image graph without applying a scene action."""
    pending, visited, parents, textures = [root], set(), [], set()
    group_type = coin.SoGroup.getClassTypeId()
    texture_type = coin.SoTexture2.getClassTypeId()
    separator_type = coin.SoSeparator.getClassTypeId()
    while pending:
        node = pending.pop()
        pointer = _pointer(node)
        if pointer in visited:
            continue
        visited.add(pointer)
        if len(visited) > _MAX_NODES:
            raise ValueError("native image graph exceeds inspection limit")
        if not node.isOfType(group_type):
            continue
        for child in _children(node):
            if child.isOfType(texture_type):
                if not node.isOfType(separator_type):
                    raise ValueError("native image texture has an unsupported parent")
                parents.append((node, child))
                textures.add(_pointer(child))
            pending.append(child)
    if len(textures) != 1 or not 1 <= len(parents) <= 4:
        raise ValueError("native image must have one texture in its mode separators")
    if len({_pointer(parent) for parent, _ in parents}) != len(parents):
        raise ValueError("native image contains repeated texture edges")
    return parents


class _Binding:
    def __init__(self, root, parents, node, token, coin, qtgui):
        self.root_pointer = _pointer(root)
        self.parents = parents
        self.node = node
        self.token = token
        self.coin = coin
        self.qtgui = qtgui
        self.retained_parents = []
        self.retained_node = False
        self.active = False

    def linked(self):
        if not self.active:
            return False
        own = _pointer(self.node)
        for parent, texture in self.parents:
            children = [_pointer(child) for child in _children(parent)]
            target = _pointer(texture)
            if children.count(own) != 1 or children.count(target) != 1:
                return False
            if children.index(own) + 1 != children.index(target):
                return False
        return True

    def release(self):
        """Unlink only our edges before releasing borrowed native parents."""
        self.active = False
        errors = []
        own = _pointer(self.node)
        # Inspect every attempted parent, including insertChild that mutated
        # before throwing. Strong native refs keep these borrowed wrappers safe.
        for parent in list(self.retained_parents):
            try:
                children = [_pointer(child) for child in _children(parent)]
                for index in reversed(range(len(children))):
                    if children[index] == own:
                        parent.removeChild(index)
            except Exception as exc:
                errors.append(str(exc))
                continue
            try:
                parent.unref()
                self.retained_parents.remove(parent)
            except Exception as exc:
                errors.append(str(exc))
        if not self.retained_parents and self.retained_node:
            try:
                self.node.unref()
                self.retained_node = False
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("image callback cleanup incomplete: " + "; ".join(errors))


def _finish_before_texture(token, action):
    """Coin callback: never retain an action/context or raise into the renderer."""
    try:
        binding = _BINDINGS.get(tuple(token[:2]))
        if binding is None or binding.token != token:
            return
        if not action.isOfType(binding.coin.SoGLRenderAction.getClassTypeId()):
            return
        if not binding.active:
            return
        context = binding.qtgui.QOpenGLContext.currentContext()
        if context is None or not context.isValid():
            raise RuntimeError("no valid current Qt OpenGL context")
        # Every actual texture traversal, including reuploads after eviction.
        # Cache replay does not traverse this node or recreate the texture.
        context.functions().glFinish()
    except BaseException as exc:
        try:
            _warn("graphics synchronization unavailable: %s" % exc)
        except BaseException:
            pass


def release_object(obj):
    """Document-observer cleanup; never changes the image's saved properties."""
    try:
        key = _key(obj)
        binding = _BINDINGS.get(key)
        if binding is not None:
            binding.release()
            _BINDINGS.pop(key, None)
    except Exception as exc:
        _warn(str(exc))


def release_document(doc):
    try:
        name = str(doc.Name)
        for key, binding in list(_BINDINGS.items()):
            if key[0] == name:
                try:
                    binding.release()
                    _BINDINGS.pop(key, None)
                except Exception as exc:
                    _warn(str(exc))
    except Exception as exc:
        _warn(str(exc))


class _CleanupObserver:
    def slotDeletedObject(self, obj):
        release_object(obj)

    def slotDeletedDocument(self, doc):
        release_document(doc)


class _ImageRestoreObserver:
    def slotBeforeChangeObject(self, view, prop):
        # GuiDocument.xml restores visibility while App.isRestoring() is still
        # true.  Install the render-only guard before show/setActiveMode, not
        # from the deferred worker that restores PDF styles after loading.
        if prop not in ("Visibility", "DisplayMode"):
            return
        try:
            obj = getattr(view, "Object", None)
            if obj is not None and str(getattr(obj, "TypeId", "")) == "Image::ImagePlane" and str(
                getattr(obj, "PDFRepresentation", "")
            ) == "raster":
                ensure_image_render_sync(obj)
        except Exception as exc:
            _warn("early image attachment: " + str(exc))


def _unregister_gui_observer():
    global _GUI_OBSERVER, _GUI_OBSERVER_HOST
    if _GUI_OBSERVER is not None:
        try:
            _GUI_OBSERVER_HOST.removeDocumentObserver(_GUI_OBSERVER)
        except Exception as exc:
            _warn("GUI image observer cleanup: " + str(exc))
            return False
    _GUI_OBSERVER, _GUI_OBSERVER_HOST = None, None
    return True


def register_gui_document_observer(gui=None):
    """Watch native image visibility before saved GUI properties expose it."""
    global _GUI_OBSERVER, _GUI_OBSERVER_HOST
    if sys.platform != "win32":
        return None
    try:
        if gui is None:
            import FreeCADGui as gui
        if not callable(getattr(gui, "addDocumentObserver", None)) or not callable(
            getattr(gui, "removeDocumentObserver", None)
        ):
            _warn("GUI image observer API unavailable; deferred attachment retained")
            return None
        if _GUI_OBSERVER is not None and _GUI_OBSERVER_HOST is gui:
            return _GUI_OBSERVER
        if not _unregister_gui_observer():
            return None
        observer = _ImageRestoreObserver()
        gui.addDocumentObserver(observer)
        _GUI_OBSERVER, _GUI_OBSERVER_HOST = observer, gui
        return observer
    except Exception as exc:
        _warn("GUI image observer registration: " + str(exc))
        return None


def _register_observer(host):
    global _OBSERVER, _OBSERVER_HOST
    if _OBSERVER is not None and _OBSERVER_HOST is host:
        return
    if _OBSERVER is not None:
        unregister_document_observer()
        if _BINDINGS:
            raise RuntimeError("old image callback cleanup incomplete")
    observer = _CleanupObserver()
    host.addDocumentObserver(observer)
    _OBSERVER, _OBSERVER_HOST = observer, host


def unregister_document_observer():
    """Release owned nodes and observer during module/test shutdown."""
    global _OBSERVER, _OBSERVER_HOST
    for key, binding in list(_BINDINGS.items()):
        try:
            binding.release()
            _BINDINGS.pop(key, None)
        except Exception as exc:
            _warn(str(exc))
    if _BINDINGS:
        return  # Keep the cleanup observer/callable alive if unlinking failed.
    if _OBSERVER is not None:
        try:
            _OBSERVER_HOST.removeDocumentObserver(_OBSERVER)
        except Exception:
            return
    _OBSERVER, _OBSERVER_HOST = None, None
    _unregister_gui_observer()


def ensure_image_render_sync(obj):
    """Return ``(attached, note)``; unsupported hosts retain native behavior."""
    global _SERIAL
    try:
        if sys.platform != "win32":
            return False, "not required on this platform"
        if str(getattr(obj, "TypeId", "")) != "Image::ImagePlane" or str(
            getattr(obj, "PDFRepresentation", "")
        ) != "raster":
            return False, "not an importer raster image"
        import FreeCAD
        if not getattr(FreeCAD, "GuiUp", False):
            return False, "GUI unavailable"
        from pivy import coin
        version = str(coin.SoDB.getVersion())
        # SoDB::getVersion returns "SIM Coin <version>" in official Coin 4.
        match = re.fullmatch(r"(?:(?:SIM\s+)?Coin\s+)?(\d+)\.(\d+)\.(\d+)", version.strip())
        if match is None:
            raise RuntimeError("unrecognized runtime Coin version: %s" % version)
        if tuple(map(int, match.groups())) >= (4, 0, 8):
            return False, "runtime Coin includes the upstream texture fix"
        try:
            from PySide6 import QtGui
        except ImportError:
            try:
                from PySide2 import QtGui
            except ImportError:
                from PySide import QtGui
        if not callable(getattr(QtGui.QOpenGLContext, "currentContext", None)):
            raise RuntimeError("Qt current-context API unavailable")
        if not callable(getattr(QtGui.QOpenGLContext, "functions", None)) or not callable(
            getattr(getattr(QtGui, "QOpenGLFunctions", None), "glFinish", None)
        ):
            raise RuntimeError("Qt graphics synchronization API unavailable")
        # Resolve the required Coin entry points before changing the graph.
        coin.SoGLRenderAction.getClassTypeId()
        node_factory = coin.SoCallback
        root = obj.ViewObject.RootNode
        key = _key(obj)
        old = _BINDINGS.get(key)
        if old is not None:
            if old.root_pointer == _pointer(root) and old.linked():
                return True, "already attached"
            old.release()
            _BINDINGS.pop(key, None)
        parents = _texture_parents(root, coin)
        _register_observer(FreeCAD)
        node = node_factory()
        node.setName("PDFImageRenderSync")
        _SERIAL += 1
        token = key + (_SERIAL,)
        binding = _Binding(root, parents, node, token, coin, QtGui)
        # Hold an explicit native reference through the last parent's unlink.
        # Userdata contains only immutable names and a serial, never an image,
        # provider, action or bound method cycle.
        node.setCallback(_finish_before_texture, token)
        _BINDINGS[key] = binding
        try:
            node.ref()
            binding.retained_node = True
            for parent, texture in parents:
                parent.ref()  # getChild returns a borrowed native wrapper.
                binding.retained_parents.append(parent)
                children = [_pointer(child) for child in _children(parent)]
                parent.insertChild(node, children.index(_pointer(texture)))
            binding.active = True
            if not binding.linked():
                raise RuntimeError("image callback insertion did not preserve texture adjacency")
        except Exception:
            binding.release()
            _BINDINGS.pop(key, None)
            raise
        return True, "attached for Windows Coin " + version
    except Exception as exc:
        note = str(exc)
        _warn(note)
        return False, note
