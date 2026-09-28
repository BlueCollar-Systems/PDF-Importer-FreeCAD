"""Source character positions for editable FreeCAD Draft Text and Labels.

The App object keeps its normal Draft proxy and editable content. Only its
world-text Coin node is replaced, using the same native SoAsciiText renderer
and font. Stock rendering returns when content/font/alignment is edited.
Nothing imports FreeCAD or Coin until a GUI layout is installed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re


PROPERTY = "PDFTextLayoutJSON"
SCHEMA = "bcs.freecad.source_text_layout/1"
AFFINE_SCHEMA = "mupdf_character_font_matrix/1"
_observers = None


def _finite(value, length):
    if not isinstance(value, (tuple, list)) or len(value) != length:
        raise ValueError("source text coordinates are incomplete")
    values = tuple(float(v) for v in value)
    if not all(math.isfinite(v) for v in values):
        raise ValueError("source text coordinates are not finite")
    return values


def _positive(value):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("source text scale is invalid")
    return value


def build_source_layout(item, raw_dict, *, scale, font_size, font_name,
                        host_rotation_deg, flip_y=True,
                        page_matrix=(1., 0., 0., 1., 0., 0.)):
    """Join a canonical span to its complete rawdict character inventory.

    Callers may cache the rawdict once per page. No spatial nearest-neighbor
    matching, character splitting, ligature normalization or width fitting is
    allowed: the raw text/font/origin/bbox/direction must match this exact item.
    """
    scale, font_size = _positive(scale), _positive(font_size)
    a, b, c, d, _e, _f = _finite(page_matrix, 6)
    angle = math.radians(float(host_rotation_deg))
    if not math.isfinite(angle):
        raise ValueError("source text rotation is invalid")
    page = item.get("page_number")
    indices = tuple(item.get(key) for key in ("block_index", "line_index", "span_index"))
    if type(page) is not int or page < 1 or any(type(i) is not int or i < 0 for i in indices):
        raise ValueError("source text identity is invalid")
    source_id = "p%d:b%d:l%d:s%d" % ((page,) + indices)
    source_hash = item.get("pdf_sha256", "")
    if item.get("source_item_id") != source_id or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None:
        raise ValueError("source text identity is not bound")
    try:
        block = raw_dict["blocks"][indices[0]]
        line = block["lines"][indices[1]]
        span = line["spans"][indices[2]]
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError("source character inventory is unavailable") from exc
    text = item.get("text")
    chars = span.get("chars")
    if (not isinstance(text, str) or not text or not isinstance(chars, list)
            or any(not isinstance(c, dict) or not isinstance(c.get("c"), str)
                   or len(c["c"]) != 1 for c in chars)
            or "".join(c["c"] for c in chars) != text):
        raise ValueError("source character inventory is not a complete text bijection")
    origin = _finite(item.get("origin"), 2)
    bbox = _finite(item.get("bbox"), 4)
    direction = _finite(item.get("line_direction"), 2)
    if (block.get("type") != 0 or span.get("font") != item["span"].get("font")
            or _positive(span.get("size")) != _positive(item["span"].get("size"))
            or _finite(span.get("origin"), 2) != origin
            or _finite(span.get("bbox"), 4) != bbox
            or _finite(line.get("dir"), 2) != direction):
        raise ValueError("source character inventory does not match the canonical span")
    cosine, sine = math.cos(angle), math.sin(angle)
    placements = []
    for char in chars:
        x, y = _finite(char.get("origin"), 2)
        char_bbox = _finite(char.get("bbox"), 4)
        dx, dy = x-origin[0], y-origin[1]
        dx, dy = (a*dx+c*dy)*scale, (b*dx+d*dy)*scale*(-1 if flip_y else 1)
        placements.append({"text": char["c"], "source_origin": [x, y],
                           "source_bbox": list(char_bbox),
                           "local_origin": [cosine*dx+sine*dy, -sine*dx+cosine*dy, 0.]})
    payload = {"schema": SCHEMA, "source_pdf_sha256": source_hash,
               "source_item_id": source_id, "source_text": text,
               "font_name": str(font_name), "font_size": font_size,
               "characters": placements}
    _validate(payload)
    return payload


def build_source_affine_layout(item, raw_dict, *, scale, font_size, font_name,
                               host_rotation_deg, flip_y=True,
                               page_matrix=(1., 0., 0., 1., 0., 0.)):
    """Preserve source glyph stretch/shear as well as character placement.

    MuPDF char.size is sqrt(abs(det(original text matrix))). Together with the
    independently bound font-Y vector and both axis directions, it determines
    the font-X magnitude. This reconstructs the source matrix, not a fit to
    either a native glyph's ink box or the PDF's declared advance width.
    """
    # Local import avoids recursion: the 3D builder calls only the base
    # positions-only build_source_layout above.
    from PDFText3DLayout import build_source_character_layout

    payload = build_source_character_layout(
        item, raw_dict, scale=scale, font_size=font_size, font_name=font_name,
        host_rotation_deg=host_rotation_deg, flip_y=flip_y, page_matrix=page_matrix)
    payload.update(schema=SCHEMA, source_affine=AFFINE_SCHEMA,
                   affine_basis="original_font_metrics_and_mupdf_matrix_expansion")
    _validate(payload)
    return payload


def _validate(payload):
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("unsupported source text layout")
    if re.fullmatch(r"[0-9a-f]{64}", payload.get("source_pdf_sha256", "")) is None:
        raise ValueError("source text layout has no PDF identity")
    if re.fullmatch(r"p[1-9][0-9]*:b[0-9]+:l[0-9]+:s[0-9]+", payload.get("source_item_id", "")) is None:
        raise ValueError("source text layout has no item identity")
    _positive(payload.get("font_size"))
    text, chars = payload.get("source_text"), payload.get("characters")
    affine = payload.get("source_affine")
    if affine is not None and affine != AFFINE_SCHEMA:
        raise ValueError("unsupported source text affine geometry")
    if (not isinstance(text, str) or not text or not isinstance(chars, list)
            or len(chars) != len(text) or not payload.get("font_name")):
        raise ValueError("source text layout is incomplete")
    for expected, char in zip(text, chars, strict=True):
        if not isinstance(char, dict) or char.get("text") != expected:
            raise ValueError("source text layout lost a character")
        _finite(char.get("local_origin"), 3)
        _finite(char.get("source_origin"), 2)
        _finite(char.get("source_bbox"), 4)
        if affine:
            baseline, up = (_finite(char.get(key), 2) for key in ("baseline_axis", "up_axis"))
            _positive(char.get("baseline_scale")); _positive(char.get("up_scale"))
            if (any(not math.isclose(math.hypot(*axis), 1., rel_tol=1e-10, abs_tol=1e-10)
                    for axis in (baseline, up))
                    or abs(baseline[0]*up[1]-baseline[1]*up[0]) <= 1e-12):
                raise ValueError("source text affine axes are invalid")


def persist_source_layout(obj, payload):
    """Persist the source contract; install native nodes immediately in a GUI."""
    _validate(payload)
    if str(getattr(obj, "PDFSourceItemId", "")) != payload["source_item_id"]:
        raise ValueError("host object is not bound to the source layout")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if PROPERTY not in getattr(obj, "PropertiesList", ()):
        obj.addProperty("App::PropertyString", PROPERTY, "PDF Import")
    setattr(obj, PROPERTY, encoded)
    if getattr(obj, "ViewObject", None) is not None:
        return restore_object_layout(obj)
    return {"persisted": True, "native_nodes_installed": False, "reason": "headless"}


def _content(obj):
    prop = "CustomText" if getattr(obj, "PDFRepresentation", "") == "labels" else "Text"
    value = getattr(obj, prop, None)
    if isinstance(value, (tuple, list)):
        return "\n".join(value)
    return str(value or "")


def _eligible(obj, view, payload):
    return (_content(obj) == payload["source_text"]
            and str(getattr(obj, "LabelType", "Custom")) == "Custom"
            and str(getattr(view, "FontName", "")) == payload["font_name"]
            and str(getattr(view, "DisplayMode", "World")) == "World"
            and str(getattr(view, "TextAlignment", "Bottom")) == "Bottom"
            and str(getattr(view, "Justification", "Left")) == "Left"
            and int(getattr(view, "MaxChars", 0) or 0) == 0
            and str(getattr(view, "Frame", "None")) == "None")


def _quat_product(a, b):
    ax, ay, az, aw = a; bx, by, bz, bw = b
    return (aw*bx+ax*bw+ay*bz-az*by, aw*by-ax*bz+ay*bw+az*bx,
            aw*bz+ax*by-ay*bx+az*bw, aw*bw-ax*bx-ay*by-az*bz)


def _rotate(q, v):
    inv = (-q[0], -q[1], -q[2], q[3])
    return _quat_product(_quat_product(q, (*v, 0.)), inv)[:3]


def _label_correction(obj, proxy, node):
    """Remove Draft's automatic label margin while retaining user Placement."""
    if not hasattr(proxy, "textpos"):
        return
    textpos = proxy.textpos
    current = _finite(textpos.translation.getValue().getValue(), 3)
    q = _finite(textpos.rotation.getValue().getValue(), 4)
    inverse = (-q[0], -q[1], -q[2], q[3])
    placement = obj.Placement
    base = (float(placement.Base.x), float(placement.Base.y), float(placement.Base.z))
    delta = tuple(a-b for a, b in zip(base, current, strict=True))
    node.translation.setValue(_rotate(inverse, delta))
    node.rotation.setValue(_quat_product(inverse, tuple(placement.Rotation.Q)))


def _set_active(state, active):
    parent, stock, group = state["parent"], state["stock"], state["group"]
    present = parent.findChild(group)
    if active and present < 0:
        index = parent.findChild(stock)
        if index < 0:
            raise RuntimeError("native Draft text node disappeared")
        parent.replaceChild(index, group)
    elif not active and present >= 0:
        parent.replaceChild(present, stock)
    state["active"] = active


def _refresh(obj, state):
    view, payload = obj.ViewObject, state["payload"]
    active = _eligible(obj, view, payload)
    if active:
        ratio = _positive(float(view.FontSize)) * _positive(float(getattr(view, "ScaleMultiplier", 1.))) / payload["font_size"]
        state["scale"].scaleFactor.setValue((ratio, ratio, ratio))
        _label_correction(obj, view.Proxy, state["correction"])
    _set_active(state, active)


def _observed_state(obj):
    try:
        view = getattr(obj, "ViewObject", None)
        return getattr(getattr(view, "Proxy", None), "_bcs_source_layout", None)
    except Exception:
        # FreeCAD emits lifecycle signals while a view is not attached yet or
        # is being destroyed. There is no owned, live scene to update then.
        return None


def _gui_observed_object(view):
    try:
        if getattr(getattr(view, "Proxy", None), "_bcs_source_layout", None) is not None:
            return view.Object
    except Exception:
        # Do not read Object on unbound native view providers during restore.
        pass
    return None


class _AppObserver:
    """Document signals run even when FreeCAD has cached Draft's callbacks.

    Never replace the user's Draft proxy or its methods. Before native updates,
    expose Draft's stock string for its margin/frame measurements. Afterwards,
    rebuild only our scene transformation. No document objects are retained.
    """
    def slotBeforeChangeObject(self, obj, prop):
        state = _observed_state(obj)
        if state is not None:
            _set_active(state, False)

    def slotChangedObject(self, obj, prop):
        state = _observed_state(obj)
        if state is not None:
            _refresh(obj, state)

    def slotRecomputedObject(self, obj):
        self.slotChangedObject(obj, "")


class _GuiObserver:
    def slotBeforeChangeObject(self, view, prop):
        obj = _gui_observed_object(view)
        if obj is not None:
            _AppObserver.slotBeforeChangeObject(self, obj, prop)

    def slotChangedObject(self, view, prop):
        obj = _gui_observed_object(view)
        if obj is not None:
            _AppObserver.slotChangedObject(self, obj, prop)


def _ensure_observers(app=None, gui=None):
    global _observers
    if _observers is not None:
        return
    if app is None:
        import FreeCAD as app
    if gui is None:
        import FreeCADGui as gui
    app_observer, gui_observer = _AppObserver(), _GuiObserver()
    app.addDocumentObserver(app_observer)
    try:
        gui.addDocumentObserver(gui_observer)
    except Exception:
        app.removeDocumentObserver(app_observer)
        raise
    _observers = (app_observer, gui_observer)


def restore_object_layout(obj, *, coin_module=None):
    """Rebuild source-bound native nodes without rewriting user view properties."""
    encoded = str(getattr(obj, PROPERTY, "") or "")
    if not encoded:
        return {"persisted": False, "native_nodes_installed": False, "reason": "unmarked"}
    payload = json.loads(encoded)
    _validate(payload)
    if str(getattr(obj, "PDFSourceItemId", "")) != payload["source_item_id"]:
        raise ValueError("host object/source text layout identity mismatch")
    view = getattr(obj, "ViewObject", None)
    if view is None:
        return {"persisted": True, "native_nodes_installed": False, "reason": "headless"}
    proxy = getattr(view, "Proxy", None)
    if proxy is None or not hasattr(proxy, "text_wld"):
        raise RuntimeError("native Draft world text provider is unavailable")
    digest = hashlib.sha256(encoded.encode("utf8")).hexdigest()
    state = getattr(proxy, "_bcs_source_layout", None)
    if state is not None:
        if state["digest"] != digest:
            raise ValueError("source layout changed after native nodes were bound")
        _refresh(obj, state)
        return {"persisted": True, "native_nodes_installed": state["active"],
                "character_count": len(payload["characters"])}
    if coin_module is None:
        from pivy import coin as coin_module
        _ensure_observers()
    coin = coin_module
    parent = getattr(proxy, "node_wld_txt", None)
    if parent is None:
        parent = getattr(proxy, "node_wld", None)
    stock = proxy.text_wld
    if parent is None or parent.findChild(stock) < 0:
        raise RuntimeError("native Draft text node is not in its expected parent")
    group, correction, scaling = coin.SoSeparator(), coin.SoTransform(), coin.SoScale()
    group.addChild(correction); group.addChild(scaling)
    font = coin.SoFont()
    font.name.connectFrom(proxy.font.name)
    font.size.setValue(payload["font_size"])
    group.addChild(font)
    nodes, affines = [], []
    for char in payload["characters"]:
        child, translation, text = coin.SoSeparator(), coin.SoTranslation(), coin.SoAsciiText()
        translation.translation.setValue(tuple(char["local_origin"]))
        text.string.setValues([char["text"]])
        text.justification = coin.SoAsciiText.LEFT
        child.addChild(translation)
        if payload.get("source_affine"):
            bx, by = (value*char["baseline_scale"] for value in char["baseline_axis"])
            ux, uy = (value*char["up_scale"] for value in char["up_axis"])
            affine = coin.SoMatrixTransform()
            # Coin uses row vectors: first two rows are the local font axes.
            affine.matrix.setValue(coin.SbMatrix(bx, by, 0., 0., ux, uy, 0., 0.,
                                                 0., 0., 1., 0., 0., 0., 0., 1.))
            child.addChild(affine)
            affines.append(affine)
        child.addChild(text); group.addChild(child)
        nodes.append((translation, text))
    state = {"digest": digest, "payload": payload, "parent": parent, "stock": stock,
             "group": group, "scale": scaling, "correction": correction,
             "nodes": nodes, "affines": affines, "active": False}
    proxy._bcs_source_layout = state
    _refresh(obj, state)
    return {"persisted": True, "native_nodes_installed": state["active"],
            "character_count": len(nodes)}


def restore_document_layouts(doc):
    """Called on both GUI-saved and headless-saved documents after restore."""
    results = []
    for obj in getattr(doc, "Objects", ()):
        if getattr(obj, PROPERTY, None):
            results.append(restore_object_layout(obj))
    return results
