# -*- coding: utf-8 -*-
# PDFStyleRestore.py — re-apply importer styles when a document opens in the GUI
# PDF Vector Importer — BlueCollar Systems — BUILT. NOT BOUGHT.
"""Restore the importer's view style contract from App-side metadata.

Why this exists
---------------
FreeCAD keeps *look* (FontSize, LineWidth, DrawStyle, colours, visibility) on
the ViewObject and saves it in ``GuiDocument.xml``.  A document produced by
FreeCADCmd (headless) has no ViewObjects and no ``GuiDocument.xml`` at all, so
when it is opened in the GUI every Draft Text/Label comes back at Draft's
defaults (3.5 mm, default font, a 1 mm "Dot" arrow marker on Labels) and every
Batch/Face at Part defaults (Solid, LineWidth 2, default colours).  Dashed and
centre lines render solid and the line-weight hierarchy is flattened.

The importer already persists the *intended* style on the App object
(``PDFTextFontSize`` / ``PDFTextFontName`` / ``PDFTextColorRGB`` /
``PDFTextJustification`` for text, ``PDFStrokeRGB`` / ``PDFFillRGB`` /
``PDFLineWidthPt`` / ``PDFDashPattern`` for geometry).  This module reads that
contract back onto the view providers.

Contract
--------
* Host-free import: nothing here imports FreeCAD at module import time, so the
  logic is unit-testable and the GUI startup path stays cheap.
* Idempotent: text / label / visibility writes compare before setting.  A second
  run over the same document writes nothing to those views.  Geometry reuses
  ``PDFImporterCore._apply_style`` (one copy of the px / floor / dash rule).
* Never raises on partial data: every object is handled independently; failures
  are collected in the returned summary.
* Registered from ``InitGui.py`` at module level (GUI startup), so it works
  without the workbench ever being activated.
"""
from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


TEXT_REPRESENTATIONS = frozenset({"text", "labels"})
LABEL_REPRESENTATION = "labels"
LABEL_ARROW_TYPE = "None"

GEOMETRY_MARKER = "PDFLineWidthPt"
GEOMETRY_PROPERTIES = ("PDFStrokeRGB", "PDFFillRGB", "PDFLineWidthPt", "PDFDashPattern")
TEXT_MARKER = "PDFTextFontSize"
# Any of these on an App object identifies it as importer-owned.
MARKER_PROPERTIES = frozenset({
    "PDFRepresentation",
    "PDFSourceItemId",
    "PDFTextFontSize",
    "PDFLineWidthPt",
    "PDFRasterFile",
    "PDFSourceText",
})
COLOR_VIEW_PROPERTIES = ("TextColor", "ShapeColor", "LineColor", "PointColor")
FONT_VIEW_PROPERTIES = ("FontName", "Font")

_COLOR_TOLERANCE = 1.5 / 255.0
_FLOAT_TOLERANCE = 1e-6
_RESTORE_RETRY_DELAY_MS = 50
_RESTORE_MAX_RETRIES = 600  # 600 x 50 ms = 30 s of "still restoring" patience


# ──────────────────────────────────────────────────────────────────────
# Metadata parsing (mirrors PDFImporterCore._format_color_metadata)
# ──────────────────────────────────────────────────────────────────────


def parse_rgb(text: Any) -> Optional[Tuple[float, float, float]]:
    """Parse ``"r,g,b"`` metadata into a colour tuple; ``None`` when absent/invalid."""
    if not isinstance(text, str) or not text.strip():
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) < 3:
        return None
    try:
        channels = tuple(float(part) for part in parts[:3])
    except ValueError:
        return None
    if any(channel != channel or channel < 0.0 or channel > 1.0 for channel in channels):
        return None
    return channels  # type: ignore[return-value]


def parse_dashes(text: Any) -> Optional[List[float]]:
    """Parse ``"d1,d2,..."`` metadata into a dash list; ``None`` when absent/invalid."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        values = [float(part.strip()) for part in text.split(",") if part.strip()]
    except ValueError:
        return None
    if len(values) < 2 or any(value != value or value < 0.0 for value in values):
        return None
    return values


def _properties(obj: Any) -> set:
    try:
        return set(getattr(obj, "PropertiesList", []) or [])
    except Exception:
        return set()


def _colors_match(current: Any, wanted: Sequence[float]) -> bool:
    try:
        if current is None or len(current) < 3:
            return False
        return all(
            abs(float(current[index]) - float(wanted[index])) <= _COLOR_TOLERANCE
            for index in range(3)
        )
    except (TypeError, ValueError):
        return False


def _floats_match(current: Any, wanted: float) -> bool:
    try:
        return abs(float(current) - float(wanted)) <= _FLOAT_TOLERANCE
    except (TypeError, ValueError):
        return False


def _set_if_changed(
    view: Any,
    name: str,
    value: Any,
    matcher: Optional[Callable[[Any, Any], bool]] = None,
) -> bool:
    """Write ``view.name = value`` only when it differs; return whether written."""
    try:
        current = getattr(view, name)
    except Exception:
        return False
    try:
        if matcher is not None:
            same = matcher(current, value)
        else:
            same = current == value
    except Exception:
        same = False
    if same:
        return False
    try:
        setattr(view, name, value)
    except Exception:
        return False
    return True


def _has_view_property(view: Any, name: str) -> bool:
    try:
        return hasattr(view, name)
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────
# Geometry: reuse the importer's own rule
# ──────────────────────────────────────────────────────────────────────


def _geometry_style_applier() -> Optional[Callable[..., Any]]:
    """Return ``PDFImporterCore._apply_style`` (lazy import), or ``None``.

    PDFImporterCore imports PyMuPDF at module import; keep that off the GUI
    startup path and only pay for it when a document actually needs geometry
    style restored.
    """
    try:
        import PDFImporterCore as core  # type: ignore
    except Exception:
        try:
            from PDFVectorImporter.src import PDFImporterCore as core  # type: ignore
        except Exception:
            return None
    applier = getattr(core, "_apply_style", None)
    return applier if callable(applier) else None


def _geometry_options():
    try:
        import PDFImporterCore as core  # type: ignore
    except Exception:
        try:
            from PDFVectorImporter.src import PDFImporterCore as core  # type: ignore
        except Exception:
            return None
    try:
        return core.ImportOptions()
    except Exception:
        return None


def _restore_geometry(obj: Any, view: Any, applier: Callable[..., Any], opts: Any) -> bool:
    stroke_rgb = parse_rgb(getattr(obj, "PDFStrokeRGB", None))
    fill_rgb = parse_rgb(getattr(obj, "PDFFillRGB", None))
    try:
        width = float(getattr(obj, GEOMETRY_MARKER, 0.0) or 0.0)
    except (TypeError, ValueError):
        width = 0.0
    width_value: Optional[float] = width if width > 0.0 else None
    dashes = parse_dashes(getattr(obj, "PDFDashPattern", None))
    applier(obj, stroke_rgb, fill_rgb, width_value, dashes, opts, persist_metadata=False)
    return True


# ──────────────────────────────────────────────────────────────────────
# Text / labels / 3D text
# ──────────────────────────────────────────────────────────────────────


def _text_font_value(obj: Any) -> str:
    """Prefer a still-present cached font file, else the persisted font name."""
    font_file = getattr(obj, "PDFFontFile", None)
    if isinstance(font_file, str) and font_file.strip():
        try:
            if os.path.isfile(font_file):
                return font_file
        except (OSError, TypeError, ValueError):
            pass
    font_name = getattr(obj, "PDFTextFontName", None)
    return font_name.strip() if isinstance(font_name, str) else ""


def _restore_text(obj: Any, view: Any, representation: str) -> bool:
    """Apply persisted Draft text style; return whether anything was written."""
    written = False
    if _has_view_property(view, "FontSize"):
        try:
            font_size = float(getattr(obj, TEXT_MARKER))
        except (AttributeError, TypeError, ValueError):
            font_size = 0.0
        if font_size == font_size and font_size > 0.0:
            written |= _set_if_changed(view, "FontSize", font_size, _floats_match)
    font_value = _text_font_value(obj)
    if font_value:
        for name in FONT_VIEW_PROPERTIES:
            if _has_view_property(view, name):
                written |= _set_if_changed(view, name, font_value)
    justification = getattr(obj, "PDFTextJustification", None)
    if isinstance(justification, str) and justification and _has_view_property(view, "Justification"):
        written |= _set_if_changed(view, "Justification", justification)
    color = parse_rgb(getattr(obj, "PDFTextColorRGB", None))
    if color is not None:
        for name in COLOR_VIEW_PROPERTIES:
            if _has_view_property(view, name):
                written |= _set_if_changed(view, name, color, _colors_match)
    if representation == LABEL_REPRESENTATION:
        # The importer builds Labels with points=[anchor, anchor]; Draft's
        # default "Dot" arrow then draws a 1 mm marker over the first glyph.
        if _has_view_property(view, "ArrowTypeStart"):
            written |= _set_if_changed(view, "ArrowTypeStart", LABEL_ARROW_TYPE)
        if _has_view_property(view, "Line"):
            written |= _set_if_changed(view, "Line", False)
    return written


def _restore_visibility(obj: Any, view: Any) -> bool:
    try:
        wanted = bool(getattr(obj, "Visibility", True))
    except Exception:
        wanted = True
    if not _has_view_property(view, "Visibility"):
        return False
    return _set_if_changed(view, "Visibility", wanted)


def _is_group(obj: Any) -> bool:
    try:
        return str(getattr(obj, "TypeId", "") or "").startswith("App::DocumentObjectGroup")
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────────
# Public entry points
# ──────────────────────────────────────────────────────────────────────


def restore_object_style(
    obj: Any,
    *,
    geometry_applier: Optional[Callable[..., Any]] = None,
    geometry_opts: Any = None,
) -> Dict[str, Any]:
    """Restore one object.  Returns what happened; never raises."""
    result = {"marked": False, "text": False, "geometry": False, "visibility": False,
              "error": None}
    try:
        properties = _properties(obj)
        if not (properties & MARKER_PROPERTIES):
            return result
        result["marked"] = True
        view = getattr(obj, "ViewObject", None)
        if view is None:
            return result
        representation = str(getattr(obj, "PDFRepresentation", "") or "")
        if TEXT_MARKER in properties or representation in TEXT_REPRESENTATIONS:
            # Draft Text/Label carry the full contract; 3D text only its colour.
            try:
                _restore_text(obj, view, representation)
                result["text"] = representation in TEXT_REPRESENTATIONS
            except Exception as exc:  # pragma: no cover - defensive
                result["error"] = "text: %s: %s" % (exc.__class__.__name__, exc)
        if GEOMETRY_MARKER in properties and geometry_applier is not None:
            try:
                _restore_geometry(obj, view, geometry_applier, geometry_opts)
                result["geometry"] = True
            except Exception as exc:  # pragma: no cover - defensive
                result["error"] = "geometry: %s: %s" % (exc.__class__.__name__, exc)
        try:
            result["visibility"] = _restore_visibility(obj, view)
        except Exception as exc:  # pragma: no cover - defensive
            result["error"] = "visibility: %s: %s" % (exc.__class__.__name__, exc)
    except Exception as exc:  # pragma: no cover - defensive
        result["error"] = "%s: %s" % (exc.__class__.__name__, exc)
    return result


def restore_document_styles(doc: Any, *, log: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Restore every importer-owned object in ``doc``.  Never raises."""
    summary: Dict[str, Any] = {
        "document": "",
        "objects_scanned": 0,
        "objects_marked": 0,
        "text_restored": 0,
        "geometry_restored": 0,
        "visibility_restored": 0,
        "groups_restored": 0,
        "errors": [],
    }
    try:
        summary["document"] = str(getattr(doc, "Name", "") or "")
    except Exception:
        pass
    try:
        objects = list(getattr(doc, "Objects", []) or [])
    except Exception as exc:
        summary["errors"].append("document objects unavailable: %s" % exc)
        return summary
    summary["objects_scanned"] = len(objects)

    applier = None
    opts = None
    for obj in objects:
        if GEOMETRY_MARKER in _properties(obj):
            applier = _geometry_style_applier()
            opts = _geometry_options() if applier is not None else None
            if applier is None or opts is None:
                summary["errors"].append(
                    "geometry style unavailable: PDFImporterCore could not be imported"
                )
                applier = None
            break

    marked_objects: List[Any] = []
    for obj in objects:
        outcome = restore_object_style(obj, geometry_applier=applier, geometry_opts=opts)
        if not outcome["marked"]:
            continue
        summary["objects_marked"] += 1
        marked_objects.append(obj)
        if outcome["text"]:
            summary["text_restored"] += 1
        if outcome["geometry"]:
            summary["geometry_restored"] += 1
        if outcome["visibility"]:
            summary["visibility_restored"] += 1
        if outcome["error"]:
            summary["errors"].append(
                "%s: %s" % (str(getattr(obj, "Name", "?") or "?"), outcome["error"])
            )

    # Parent groups (PDF_Page_N etc.) own the visibility of their children.
    seen = set()
    pending = list(marked_objects)
    while pending:
        current = pending.pop()
        try:
            parents = list(getattr(current, "InList", []) or [])
        except Exception:
            parents = []
        for parent in parents:
            key = id(parent)
            if key in seen or not _is_group(parent):
                continue
            seen.add(key)
            try:
                view = getattr(parent, "ViewObject", None)
                if view is not None and _restore_visibility(parent, view):
                    summary["visibility_restored"] += 1
                summary["groups_restored"] += 1
            except Exception as exc:  # pragma: no cover - defensive
                summary["errors"].append(
                    "%s: group visibility: %s" % (getattr(parent, "Name", "?"), exc)
                )
            pending.append(parent)

    if log is not None and summary["objects_marked"]:
        try:
            log(
                "PDF Vector Importer: restored view style for %d object(s) in %s "
                "(text %d, geometry %d, visibility %d)\n"
                % (
                    summary["objects_marked"],
                    summary["document"] or "document",
                    summary["text_restored"],
                    summary["geometry_restored"],
                    summary["visibility_restored"],
                )
            )
        except Exception:
            pass
    return summary


# ──────────────────────────────────────────────────────────────────────
# Document observer
# ──────────────────────────────────────────────────────────────────────


def _qt_schedule(callback: Callable[[], None], delay_ms: int) -> bool:
    """Run ``callback`` from the Qt event loop; False when Qt is unavailable."""
    try:
        try:
            from PySide6 import QtCore  # type: ignore
        except ImportError:
            from PySide2 import QtCore  # type: ignore
    except Exception:
        return False
    try:
        QtCore.QTimer.singleShot(int(delay_ms), callback)
    except Exception:
        return False
    return True


def _console_log(message: str) -> None:
    try:
        import FreeCAD  # type: ignore

        FreeCAD.Console.PrintLog(message)
    except Exception:
        pass


class PDFStyleRestoreObserver:
    """App document observer: restore styles once a created document has loaded.

    ``slotCreatedDocument`` fires from ``App::Application::newDocument`` before
    the file is restored, and FreeCAD's Python observers have no
    "finished restoring" slot; so the work is deferred to the Qt event loop,
    which only resumes after the synchronous open completes.  If the document
    still reports ``Restoring`` (a progress bar pumped events mid-load) the
    callback re-arms itself a bounded number of times.
    """

    def __init__(
        self,
        *,
        schedule: Optional[Callable[[Callable[[], None], int], bool]] = None,
        restore: Optional[Callable[[Any], Dict[str, Any]]] = None,
        log: Optional[Callable[[str], None]] = None,
        max_retries: int = _RESTORE_MAX_RETRIES,
    ):
        self._schedule = schedule or _qt_schedule
        self._restore = restore or (lambda doc: restore_document_styles(doc, log=self._log))
        self._log = log or _console_log
        self._max_retries = int(max_retries)

    # -- FreeCAD slots ---------------------------------------------------
    def slotCreatedDocument(self, doc: Any) -> None:
        try:
            self._arm(doc, attempt=0)
        except Exception:
            pass

    def slotDeletedDocument(self, doc: Any) -> None:  # noqa: D401 - FreeCAD slot
        return None

    # -- internals ---------------------------------------------------------
    def _arm(self, doc: Any, attempt: int) -> None:
        def run() -> None:
            self._run(doc, attempt)

        delay = 0 if attempt == 0 else _RESTORE_RETRY_DELAY_MS
        try:
            self._schedule(run, delay)
        except Exception:
            pass

    def _run(self, doc: Any, attempt: int) -> None:
        try:
            still_restoring = bool(getattr(doc, "Restoring", False))
        except Exception:
            still_restoring = False
        if still_restoring:
            if attempt < self._max_retries:
                self._arm(doc, attempt + 1)
            return
        try:
            self._restore(doc)
        except Exception as exc:
            try:
                self._log("PDF Vector Importer: style restore failed: %s\n" % exc)
            except Exception:
                pass


_OBSERVER: Optional[PDFStyleRestoreObserver] = None
_OBSERVER_HOST: Any = None


def register_document_observer(freecad_module: Any = None) -> Optional[PDFStyleRestoreObserver]:
    """Register the observer once with ``FreeCAD.addDocumentObserver``.

    Returns the live observer (or ``None`` when FreeCAD is unavailable).
    Safe to call repeatedly.
    """
    global _OBSERVER, _OBSERVER_HOST
    host = freecad_module
    if host is None:
        try:
            import FreeCAD as host  # type: ignore
        except Exception:
            return None
    if _OBSERVER is not None and _OBSERVER_HOST is host:
        return _OBSERVER
    observer = PDFStyleRestoreObserver()
    try:
        host.addDocumentObserver(observer)
    except Exception:
        return None
    _OBSERVER = observer
    _OBSERVER_HOST = host
    return observer


def unregister_document_observer(freecad_module: Any = None) -> None:
    """Remove a previously registered observer (test/reload hygiene)."""
    global _OBSERVER, _OBSERVER_HOST
    if _OBSERVER is None:
        return
    host = freecad_module if freecad_module is not None else _OBSERVER_HOST
    try:
        host.removeDocumentObserver(_OBSERVER)
    except Exception:
        pass
    _OBSERVER = None
    _OBSERVER_HOST = None
