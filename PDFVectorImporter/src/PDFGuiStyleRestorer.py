# -*- coding: utf-8 -*-
"""Restore durable PDF Text/Label styling after a GUI document is opened.

Draft creates its view providers from GUI preferences when an FCStd was written
headlessly.  The importer therefore keeps source style on the App object and
this GUI-only observer applies it once, after the stock Draft view provider is
available.  It never changes representation, source text, placement, or the
fallback ladder.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple


_GUI_STYLE_RESTORER = None
_INVALID_COLOR = object()
_REQUIRED_METADATA = {
    "PDFSourceItemId",
    "PDFRepresentation",
    "PDFTextFontName",
    "PDFTextFontSize",
    "PDFTextJustification",
    "PDFTextColorRGB",
}


def _object_style(obj) -> Optional[Dict[str, Any]]:
    """Return validated durable style for a genuine native importer object."""
    try:
        properties = set(getattr(obj, "PropertiesList", []) or [])
        representation = str(obj.PDFRepresentation or "")
        source_item_id = str(obj.PDFSourceItemId or "")
        proxy_type = str(getattr(getattr(obj, "Proxy", None), "Type", "") or "")
        type_id = str(getattr(obj, "TypeId", "") or "")
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None

    expected_proxy = {"text": "Text", "labels": "Label"}.get(representation)
    if (
        not _REQUIRED_METADATA.issubset(properties)
        or not source_item_id
        or type_id != "App::FeaturePython"
        or expected_proxy is None
        or proxy_type != expected_proxy
    ):
        return None

    try:
        font_name = str(obj.PDFTextFontName or "")
        font_size = float(obj.PDFTextFontSize)
        justification = str(obj.PDFTextJustification or "")
        raw_color = str(obj.PDFTextColorRGB or "").strip()
        if "PDFTextVisibility" in properties:
            visibility = obj.PDFTextVisibility
            if not isinstance(visibility, bool):
                return None
        else:
            # Compatibility with documents made before visibility metadata was
            # added. Native imported annotations were always intended visible.
            visibility = True
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None

    if (
        not math.isfinite(font_size)
        or font_size <= 0.0
        or justification not in {"Left", "Center", "Right"}
    ):
        return None

    color = None
    if raw_color:
        try:
            channels = tuple(float(value.strip()) for value in raw_color.split(","))
        except (TypeError, ValueError):
            channels = _INVALID_COLOR
        if (
            channels is _INVALID_COLOR
            or len(channels) != 3
            or not all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in channels)
        ):
            return None
        color = channels

    return {
        "representation": representation,
        "font_name": font_name,
        "font_size": font_size,
        "justification": justification,
        "color": color,
        "visibility": visibility,
    }


def _has_attribute(obj, name: str) -> bool:
    try:
        return hasattr(obj, name)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False


def _number_matches(actual, expected: float) -> bool:
    try:
        value = float(actual)
    except (RuntimeError, TypeError, ValueError):
        return False
    return math.isfinite(value) and math.isclose(value, expected, abs_tol=1e-7)


def _color_matches(actual, expected: Tuple[float, float, float]) -> bool:
    try:
        channels = list(actual)
    except (RuntimeError, TypeError, ValueError):
        return False
    return len(channels) >= 3 and all(
        _number_matches(channels[index], expected[index]) for index in range(3)
    )


def _rollback_assignments(changes) -> None:
    for obj, name, value in reversed(changes):
        try:
            setattr(obj, name, value)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass


def _apply_assignments(assignments):
    changes = []
    try:
        for obj, name, value, matches in assignments:
            actual = getattr(obj, name)
            equal = matches(actual, value) if matches is not None else actual == value
            if equal:
                continue
            previous = list(actual) if isinstance(actual, list) else actual
            changes.append((obj, name, previous))
            setattr(obj, name, value)
            applied = getattr(obj, name)
            accepted = matches(applied, value) if matches is not None else applied == value
            if not accepted:
                raise ValueError("host rejected restored view property")
        return changes
    except (AttributeError, RuntimeError, TypeError, ValueError):
        _rollback_assignments(changes)
        return None


def restore_importer_text_object(obj) -> bool:
    """Restore one importer-owned native Text/Label and verify live values.

    ``False`` is a safe no-op: the object was unrelated, malformed, headless,
    or did not expose the stock Draft properties needed for an exact restore.
    """
    style = _object_style(obj)
    if style is None:
        return False
    try:
        view = getattr(obj, "ViewObject", None)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return False
    if view is None:
        return False

    font_properties = [name for name in ("FontName", "Font") if _has_attribute(view, name)]
    if (
        not _has_attribute(view, "Visibility")
        or not _has_attribute(view, "FontSize")
        or not _has_attribute(view, "Justification")
        or (style["font_name"] and not font_properties)
        or (style["color"] is not None and not _has_attribute(view, "TextColor"))
    ):
        return False

    is_label = style["representation"] == "labels"
    arrow_properties = [
        name for name in ("ArrowTypeStart", "ArrowType") if _has_attribute(view, name)
    ]
    if is_label and (
        not _has_attribute(obj, "Points")
        or not _has_attribute(view, "Line")
        or not _has_attribute(view, "Frame")
        or not arrow_properties
    ):
        return False

    assignments = [
        (view, "Visibility", style["visibility"], None),
        (view, "FontSize", style["font_size"], _number_matches),
        (view, "Justification", style["justification"], None),
    ]
    for name, value, matcher in (
        ("DisplayMode", "World", None),
        ("ScaleMultiplier", 1.0, _number_matches),
        ("LineSpacing", 1.0, _number_matches),
    ):
        if _has_attribute(view, name):
            assignments.append((view, name, value, matcher))
    if style["font_name"]:
        assignments.extend(
            (view, name, style["font_name"], None) for name in font_properties
        )
    if style["color"] is not None:
        assignments.append((view, "TextColor", style["color"], _color_matches))

    if is_label:
        assignments.extend(
            [
                (view, "Line", False, None),
                (view, "Frame", "None", None),
            ]
        )
        assignments.extend((view, name, "None", None) for name in arrow_properties)
        for name, value in (("TextAlignment", "Bottom"), ("MaxChars", 0)):
            if _has_attribute(view, name):
                assignments.append((view, name, value, None))
        assignments.append((obj, "Points", [], None))

    changes = _apply_assignments(assignments)
    if changes is None:
        return False

    try:
        checks = [
            bool(getattr(view, "Visibility", False)) == style["visibility"],
            _number_matches(getattr(view, "FontSize", None), style["font_size"]),
            str(getattr(view, "Justification", "") or "") == style["justification"],
        ]
        if style["font_name"]:
            checks.extend(
                str(getattr(view, name, "") or "") == style["font_name"]
                for name in font_properties
            )
        if style["color"] is not None:
            checks.append(_color_matches(getattr(view, "TextColor", ()), style["color"]))
        for name, value, matcher in (
            ("DisplayMode", "World", None),
            ("ScaleMultiplier", 1.0, _number_matches),
            ("LineSpacing", 1.0, _number_matches),
        ):
            if _has_attribute(view, name):
                actual = getattr(view, name, None)
                checks.append(matcher(actual, value) if matcher else actual == value)
        if is_label:
            checks.extend(
                [
                    list(getattr(obj, "Points", []) or []) == [],
                    getattr(view, "Line", True) is False,
                    str(getattr(view, "Frame", "") or "") == "None",
                ]
            )
            checks.extend(
                str(getattr(view, name, "") or "") == "None"
                for name in arrow_properties
            )
            if _has_attribute(view, "TextAlignment"):
                checks.append(str(view.TextAlignment or "") == "Bottom")
            if _has_attribute(view, "MaxChars"):
                checks.append(int(view.MaxChars) == 0)
        verified = all(checks)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        verified = False
    if not verified:
        _rollback_assignments(changes)
    return verified


def restore_importer_text_document(document) -> int:
    """Restore eligible objects in one App or Gui document."""
    try:
        app_document = getattr(document, "Document", document)
        objects = list(getattr(app_document, "Objects", []) or [])
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return 0
    return sum(1 for obj in objects if restore_importer_text_object(obj))


def _default_scheduler(delay_ms: int, callback) -> None:
    try:
        from PySide import QtCore
    except ImportError:
        callback()
        return
    QtCore.QTimer.singleShot(delay_ms, callback)


class PDFGuiStyleRestorer:
    """Bounded, once-per-session GUI document observer."""

    def __init__(self, gui=None, scheduler=None):
        self._gui = gui
        self._scheduler = scheduler or _default_scheduler
        self._pending_documents = {}
        self._pending_objects = {}
        self._restored = set()
        self._flush_scheduled = False

    @staticmethod
    def _document_name(document) -> str:
        try:
            app_document = getattr(document, "Document", document)
            return str(getattr(app_document, "Name", "") or "")
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return ""

    @classmethod
    def _object_key(cls, obj):
        try:
            name = str(getattr(obj, "Name", "") or "")
            document_name = cls._document_name(getattr(obj, "Document", None))
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return ("", "")
        return (document_name, name)

    def _schedule_flush(self) -> None:
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        try:
            self._scheduler(100, self._flush)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            self._flush_scheduled = False

    def _queue_document(self, document) -> None:
        name = self._document_name(document)
        if not name:
            return
        self._pending_documents[name] = document
        self._schedule_flush()

    def _queue_object(self, obj) -> None:
        key = self._object_key(obj)
        if not all(key):
            if restore_importer_text_object(obj):
                self._restored.add(("", str(id(obj))))
            return
        self._pending_objects[key] = obj
        self._schedule_flush()

    def _resolve_document(self, name: str, fallback):
        try:
            getter = getattr(self._gui, "getDocument", None)
            if callable(getter):
                resolved = getter(name)
                if resolved is not None:
                    return resolved
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        return fallback

    def _resolve_object(self, key, fallback):
        document_name, object_name = key
        document = self._resolve_document(document_name, None)
        try:
            app_document = getattr(document, "Document", document)
            getter = getattr(app_document, "getObject", None)
            if callable(getter):
                resolved = getter(object_name)
                if resolved is not None:
                    return resolved
        except (AttributeError, RuntimeError, TypeError, ValueError):
            pass
        return fallback

    def _flush(self) -> None:
        self._flush_scheduled = False
        pending_documents = self._pending_documents
        pending_objects = self._pending_objects
        self._pending_documents = {}
        self._pending_objects = {}

        for name, fallback in pending_documents.items():
            document = self._resolve_document(name, fallback)
            try:
                app_document = getattr(document, "Document", document)
                objects = list(getattr(app_document, "Objects", []) or [])
            except (AttributeError, RuntimeError, TypeError, ValueError):
                continue
            for obj in objects:
                key = self._object_key(obj)
                if key in self._restored:
                    continue
                if restore_importer_text_object(obj):
                    self._restored.add(key)

        for key, fallback in pending_objects.items():
            if key in self._restored:
                continue
            obj = self._resolve_object(key, fallback)
            if restore_importer_text_object(obj):
                self._restored.add(key)

    def slotCreatedObject(self, view_object) -> None:
        try:
            obj = getattr(view_object, "Object", None)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            obj = None
        if obj is not None:
            self._queue_object(obj)

    def slotActivateDocument(self, gui_document) -> None:
        self._queue_document(gui_document)

    def slotDeletedObject(self, view_object) -> None:
        try:
            obj = getattr(view_object, "Object", None)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            obj = None
        key = self._object_key(obj)
        self._pending_objects.pop(key, None)
        self._restored.discard(key)

    def slotDeletedDocument(self, gui_document) -> None:
        name = self._document_name(gui_document)
        self._pending_documents.pop(name, None)
        self._pending_objects = {
            key: value for key, value in self._pending_objects.items() if key[0] != name
        }
        self._restored = {key for key in self._restored if key[0] != name}

    def queue_existing_documents(self) -> None:
        try:
            documents = self._gui.listDocuments()
            values = documents.values() if isinstance(documents, dict) else documents
        except (AttributeError, RuntimeError, TypeError, ValueError):
            return
        for document in list(values or []):
            self._queue_document(document)


def register_gui_style_restorer(*, freecad=None, gui=None, scheduler=None):
    """Install and retain the observer only in a real FreeCAD GUI process."""
    global _GUI_STYLE_RESTORER
    if freecad is None:
        try:
            import FreeCAD as freecad
        except ImportError:
            return None
    if not bool(getattr(freecad, "GuiUp", False)):
        return None
    if gui is None:
        try:
            import FreeCADGui as gui
        except ImportError:
            return None
    add_observer = getattr(gui, "addDocumentObserver", None)
    if not callable(add_observer):
        return None
    if _GUI_STYLE_RESTORER is not None:
        return _GUI_STYLE_RESTORER

    observer = PDFGuiStyleRestorer(gui=gui, scheduler=scheduler)
    try:
        add_observer(observer)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    _GUI_STYLE_RESTORER = observer
    observer.queue_existing_documents()
    return observer
