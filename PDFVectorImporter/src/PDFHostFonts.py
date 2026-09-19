# -*- coding: utf-8 -*-
# PDFHostFonts.py — PDF span font name -> host (Draft Text / Label) font name
# BlueCollar Systems — BUILT. NOT BOUGHT.
"""Resolve the font name a PDF span carries to the name handed to the host.

Draft Text and Labels are drawn by Coin from a *name* (``ViewObject.FontName``);
the importer never gives them a font file.  A wrong name is therefore a silent
wrong font: "ArialNarrow" collapsed to "Arial" makes every label 1.21x too wide.

``resolve_host_font`` answers two questions for one PDF font name:

* which host font name reproduces it ("ArialNarrow,Bold" -> "Arial Narrow Bold"),
* how sure that is (``status``), so the import report can say when the host
  will substitute instead of staying silent.

Resolution order: strip the ``ABCDEF+`` subset prefix -> exact installed face
(PostScript name / full name / family + style) -> strip PS/MT/style words and
OR in the PDF font flags -> installed family -> standard-14 alias -> otherwise
KEEP the true variant name.  A variant is never collapsed to its base family.

The host name of an installed face is its GDI family (name ID 1) plus at most
" Bold" / " Italic".  Other style words ("Light", "Black", "Medium") are never
appended: the host cannot find "Swis721 Lt BT Light" and draws its default font.
A face that family + Bold/Italic cannot single out is requested by its full
name (ID 4) instead, and one no name reaches is reported as a substitution.

Standard library only (no FreeCAD, no fontTools) and it never raises: corrupt
or unreadable font files, collections, per-user fonts and non-Windows hosts all
degrade to a smaller index or to status ``unverified``.
"""
from __future__ import annotations

import os
import re
import struct
import threading
from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple

STATUS_EXACT_FACE = "installed_exact_face"
STATUS_FAMILY = "installed_family"
STATUS_FAMILY_STYLE_SYNTHESIZED = "installed_family_style_synthesized"
STATUS_ALIAS = "substituted_alias"
STATUS_ALIAS_NOT_INSTALLED = "substituted_alias_not_installed"
STATUS_NOT_INSTALLED = "not_installed"
STATUS_UNVERIFIED = "unverified"
STATUS_EMPTY = "empty"

# The host draws the source font itself only for these two outcomes.  Every
# other status is reported as a substitution - except ``unverified``: with no
# font index (any non-Windows host) nothing is known either way, so it is said
# once in the report note and is not a warning per font on every import.
# ``empty`` is a span that carried no font name at all; the host default draws it.
SOURCE_FONT_EQUIVALENT_STATUSES = frozenset({STATUS_EXACT_FACE, STATUS_FAMILY})

# PyMuPDF span flags.
_FLAG_ITALIC = 2
_FLAG_BOLD = 16
# MuPDF caps the font name reported on a span at 24 characters.
_MUPDF_SPAN_FONT_NAME_LIMIT = 24

_FONT_EXTENSIONS = (".ttf", ".otf", ".ttc", ".otc")
_FONTS_REGISTRY_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
_MAX_COLLECTION_FACES = 256
_MAX_NAME_TABLE_BYTES = 8 * 1024 * 1024
_REGULAR_STYLES = frozenset({"", "regular", "roman", "normal", "book", "plain"})
# The only subfamily (name ID 2) words GDI's four-face family model knows.
_RIBBI_STYLE_WORDS = _REGULAR_STYLES | frozenset(
    {"bold", "italic", "oblique", "bolditalic", "boldoblique"}
)
# LOGFONT.lfFaceName holds 31 characters: a longer face name cannot be requested.
_GDI_FACE_NAME_LIMIT = 31

# PDF standard-14 / base PostScript families Windows does not ship, mapped to
# their metric-compatible Windows families.  Deliberately short: anything not
# listed keeps its own name.
_ALIAS_FAMILIES = {
    "helvetica": "Arial",
    "helveticanarrow": "Arial Narrow",
    "times": "Times New Roman",
    "timesroman": "Times New Roman",
    "courier": "Courier New",
}

# Families recognised without an installed-font index (non-Windows hosts), so
# those hosts keep the family names this importer has always written.
_KNOWN_FAMILIES = {
    "arial": "Arial",
    "arialnarrow": "Arial Narrow",
    "arialblack": "Arial Black",
    "timesnewroman": "Times New Roman",
    "couriernew": "Courier New",
    "calibri": "Calibri",
}

# (token, bold, italic, kind) — longest first so "bolditalic" wins over "bold".
_NAME_TOKENS: Tuple[Tuple[str, bool, bool, str], ...] = (
    ("boldoblique", True, True, "style"),
    ("bolditalic", True, True, "style"),
    ("oblique", False, True, "style"),
    ("regular", False, False, "style"),
    ("italic", False, True, "style"),
    ("normal", False, False, "style"),
    ("bold", True, False, "style"),
    ("psmt", False, False, "ps"),
    ("bd", True, False, "style"),
    ("it", False, True, "style"),
    ("mt", False, False, "foundry"),
    ("ps", False, False, "ps"),
)

_KEY_STRIP = re.compile(r"[^a-z0-9]")
_CHUNK_SPLIT = re.compile(r"[-_, ]+")
_WORD_SPLIT = re.compile(
    r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|(?<=[0-9])(?=[A-Z])"
)
_CLEAN_NAME = re.compile(r"^[A-Za-z0-9 ,_\-]+$")

_INDEX: Optional[Dict[str, Any]] = None
_INDEX_LOCK = threading.Lock()
_RESOLVE_CACHE: Dict[Tuple[str, Optional[int]], Dict[str, Any]] = {}


# ──────────────────────────────────────────────────────────────────────
# Keys
# ──────────────────────────────────────────────────────────────────────
def _alnum_key(text: Any) -> str:
    return _KEY_STRIP.sub("", str(text or "").lower())


def strip_subset_prefix(font_name: Any) -> str:
    """Drop the six-letter ``ABCDEF+`` tag PDF writers put on subset fonts."""
    raw = str(font_name or "").strip()
    if "+" in raw:
        prefix, rest = raw.split("+", 1)
        if len(prefix) == 6 and prefix.isupper():
            return rest.strip()
    return raw


def font_key(font_name: Any) -> str:
    """Lowercase alphanumeric identity of a font name, subset prefix removed."""
    return _alnum_key(strip_subset_prefix(font_name))


# ──────────────────────────────────────────────────────────────────────
# Installed-font index
# ──────────────────────────────────────────────────────────────────────
def _decode_name(platform_id: int, language_id: int, raw: bytes) -> Tuple[int, str]:
    """Return (preference rank, text); rank 0 means "ignore this record"."""
    if platform_id == 3:
        if language_id == 0x409:
            rank = 4
        elif language_id & 0x3FF == 0x09:
            rank = 3
        else:
            rank = 1
        return rank, raw.decode("utf-16-be", "replace")
    if platform_id == 0:
        return 2, raw.decode("utf-16-be", "replace")
    if platform_id == 1 and language_id == 0:
        try:
            return 2, raw.decode("mac_roman", "replace")
        except LookupError:
            return 2, raw.decode("latin-1", "replace")
    return 0, ""


def _read_sfnt_names(handle, offset: int) -> Dict[int, str]:
    """Name IDs 1/2/4/6 of the sfnt font that starts at ``offset``."""
    handle.seek(offset)
    header = handle.read(12)
    if len(header) < 12:
        return {}
    table_count = struct.unpack(">H", header[4:6])[0]
    directory = handle.read(16 * table_count)
    name_table = None
    for position in range(len(directory) // 16):
        tag, _checksum, table_offset, table_length = struct.unpack(
            ">4sIII", directory[16 * position:16 * position + 16]
        )
        if tag == b"name":
            name_table = (table_offset, table_length)
            break
    if name_table is None:
        return {}
    handle.seek(name_table[0])
    data = handle.read(min(name_table[1], _MAX_NAME_TABLE_BYTES))
    if len(data) < 6:
        return {}
    _format, record_count, strings_offset = struct.unpack(">HHH", data[:6])
    best: Dict[int, Tuple[int, str]] = {}
    for position in range(record_count):
        record = data[6 + 12 * position:18 + 12 * position]
        if len(record) < 12:
            break
        platform_id, _encoding_id, language_id, name_id, length, start = struct.unpack(
            ">6H", record
        )
        if name_id not in (1, 2, 4, 6):
            continue
        raw = data[strings_offset + start:strings_offset + start + length]
        rank, text = _decode_name(platform_id, language_id, raw)
        text = text.replace("\x00", "").strip()
        if rank and text and best.get(name_id, (0, ""))[0] < rank:
            best[name_id] = (rank, text)
    return {name_id: value[1] for name_id, value in best.items()}


def read_font_names(path: str) -> List[Dict[int, str]]:
    """Name records of every face in a .ttf/.otf/.ttc file; ``[]`` on any fault."""
    try:
        with open(path, "rb") as handle:
            header = handle.read(12)
            if len(header) < 12:
                return []
            offsets: Iterable[int] = (0,)
            if header[:4] == b"ttcf":
                face_count = min(
                    struct.unpack(">I", header[8:12])[0], _MAX_COLLECTION_FACES
                )
                raw_offsets = handle.read(4 * face_count)
                offsets = struct.unpack(
                    ">%dI" % (len(raw_offsets) // 4),
                    raw_offsets[:4 * (len(raw_offsets) // 4)],
                )
            faces = []
            for offset in offsets:
                try:
                    names = _read_sfnt_names(handle, int(offset))
                except Exception:
                    continue
                if names:
                    faces.append(names)
            return faces
    except Exception:
        # Locked, truncated, corrupt or foreign files are simply not indexed.
        return []


class _Face(NamedTuple):
    """One installed face; ``host_font`` is the name the host can request it by."""

    family: str
    style: str
    path: Optional[str]
    host_font: str
    bold: bool
    italic: bool
    full: str


def _style_bits(style: str) -> Tuple[bool, bool, bool]:
    """``(bold, italic, plain)`` of a name-table subfamily (name ID 2).

    The host addresses a face as family + Bold/Italic and nothing else, so
    "Light", "Black", "Medium" and "Roman" are all a family's upright,
    non-bold face.  ``plain`` is False when the subfamily carries such a word.
    """
    words = [word for word in _CHUNK_SPLIT.split(style.lower()) if word]
    bold = any(word.startswith("bold") or word.endswith("bold") for word in words)
    italic = any("italic" in word or "oblique" in word for word in words)
    return bold, italic, all(word in _RIBBI_STYLE_WORDS for word in words)


def index_from_records(
    records: Iterable[Dict[str, Any]], available: bool = True
) -> Dict[str, Any]:
    """Build an index from ``{family, style, postscript, full, path}`` records.

    ``faces`` maps a key to a ``_Face``; ``families`` maps a key to the family
    name.  Tests inject an index built here so results never depend on the
    fonts of the machine that runs them.

    Name ID 1 already is the family GDI knows ("Swis721 Lt BT"), so the words
    of ID 2 are never glued onto it: "Swis721 Lt BT Light" is a name no host
    can find, and it is drawn as the default font without any signal.
    """
    parsed = []
    families: Dict[str, str] = {}
    family_bits: Dict[str, List[Tuple[bool, bool]]] = {}
    for record in records or ():
        try:
            family = str(record.get("family") or "").strip()
            style = str(record.get("style") or "").strip()
            path = record.get("path")
            family_key = _alnum_key(family)
            if not family or not family_key:
                continue
            bold, italic, plain = _style_bits(style)
            parsed.append((
                family, style, str(path) if path else None, bold, italic, plain,
                str(record.get("postscript") or ""), str(record.get("full") or ""),
            ))
            families.setdefault(family_key, family)
            family_bits.setdefault(family_key, []).append((bold, italic))
        except Exception:
            continue

    claims: Dict[str, Tuple[int, _Face]] = {}
    for family, style, path, bold, italic, plain, postscript, full in parsed:
        bits = family_bits[_alnum_key(family)]
        only_bold = bold and all(bit[0] for bit in bits)
        only_italic = italic and all(bit[1] for bit in bits)
        host_font = styled_name = _host_name(family, bold, italic)
        if (only_bold or only_italic) and _alnum_key(styled_name) != _alnum_key(full):
            # The style is the family's only one and the host does not know
            # family + style as a face name ("Dutch801 XBd BT" / Extra Bold,
            # "Harlow Solid Italic" / Italic).  Asking for "Bold" on top of the
            # family makes GDI embolden a face that is bold already, so the
            # face is requested by its full name; one too long for that goes
            # by the bare family name, which returns the family's only weight.
            if _alnum_key(full) and len(full) <= _GDI_FACE_NAME_LIMIT:
                host_font = full
            else:
                host_font = _host_name(
                    family, bold and not only_bold, italic and not only_italic
                )
        face = _Face(family, style, path, host_font, bold, italic, full)
        # A face's own names win a contested key; then a plain R/B/I/BI face
        # beats a "Medium" that shares its family ("Roboto").  First come wins
        # a tie, and machine-wide fonts are listed before per-user ones.
        style_rank = 1 if plain else 2
        for rank, name in (
            (0, postscript), (0, full), (style_rank, styled_name), (style_rank, host_font),
        ):
            key = _alnum_key(name)
            if key and (key not in claims or rank < claims[key][0]):
                claims[key] = (rank, face)
    faces = {key: claim[1] for key, claim in claims.items()}
    return {
        "faces": faces,
        "families": families,
        "available": bool(available) and bool(faces),
    }


def build_font_index(font_files: Iterable[str]) -> Dict[str, Any]:
    """Index the given font files by reading only their ``name`` tables."""
    records = []
    for path in font_files or ():
        for names in read_font_names(str(path)):
            records.append({
                "family": names.get(1, ""),
                "style": names.get(2, ""),
                "full": names.get(4, ""),
                "postscript": names.get(6, ""),
                "path": str(path),
            })
    return index_from_records(records)


def _is_windows() -> bool:
    return os.name == "nt"


def _installed_font_files() -> List[str]:
    """Font files Windows has registered, machine-wide and per-user."""
    if not _is_windows():
        return []
    windows_dir = (
        os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    )
    fonts_dir = os.path.join(windows_dir, "Fonts")
    local_app_data = os.environ.get("LOCALAPPDATA") or ""
    user_fonts_dir = (
        os.path.join(local_app_data, "Microsoft", "Windows", "Fonts")
        if local_app_data
        else ""
    )
    files: List[str] = []
    seen = set()

    def add(value: Any) -> None:
        path = str(value or "").strip()
        if not path.lower().endswith(_FONT_EXTENSIONS):
            return
        if not os.path.isabs(path):
            path = os.path.join(fonts_dir, path)
        marker = os.path.normcase(os.path.normpath(path))
        if marker not in seen:
            seen.add(marker)
            files.append(path)

    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        for hive_name in ("HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER"):
            try:
                with winreg.OpenKey(
                    getattr(winreg, hive_name), _FONTS_REGISTRY_KEY
                ) as key:
                    for position in range(winreg.QueryInfoKey(key)[1]):
                        try:
                            add(winreg.EnumValue(key, position)[1])
                        except OSError:
                            continue
            except Exception:
                continue
    registered = bool(files)
    # Per-user fonts live outside %WINDIR%\Fonts.  The system folder is walked
    # only when the registry gave nothing (it is the registry's own content).
    for directory in (user_fonts_dir, "" if registered else fonts_dir):
        if not directory:
            continue
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    add(entry.path)
        except OSError:
            continue
    return files


def installed_font_index(refresh: bool = False) -> Dict[str, Any]:
    """Lazy, process-cached index of the fonts installed on this host.

    ``available`` is False when nothing could be indexed (non-Windows host, no
    readable font): callers then report ``unverified`` instead of guessing.
    """
    global _INDEX
    if _INDEX is not None and not refresh:
        return _INDEX
    with _INDEX_LOCK:
        if _INDEX is not None and not refresh:
            return _INDEX
        try:
            index = build_font_index(_installed_font_files())
        except Exception:
            index = index_from_records(())
        _RESOLVE_CACHE.clear()
        _INDEX = index
        return index


# ──────────────────────────────────────────────────────────────────────
# Name parsing
# ──────────────────────────────────────────────────────────────────────
def _host_name(family: str, bold: bool, italic: bool) -> str:
    return family + (" Bold" if bold else "") + (" Italic" if italic else "")


def _face_delivery(faces: Dict[str, Any], face: _Face, status: str) -> Tuple[str, str]:
    """``(host_font, status)`` for an installed face the PDF font resolved to.

    ``status`` stands only when the host can really request that face.  When a
    sibling owns the family + Bold/Italic name ("Roboto" Regular and Medium
    share one family) the face's full name is used instead; a face no name
    reaches is reported as a substitution, never as the source font.
    """
    if faces.get(_alnum_key(face.host_font)) == face:
        return face.host_font, status
    if (
        face.full
        and len(face.full) <= _GDI_FACE_NAME_LIMIT
        and faces.get(_alnum_key(face.full)) == face
    ):
        return face.full, status
    return face.host_font, STATUS_FAMILY_STYLE_SYNTHESIZED


def _split_words(base: str) -> List[str]:
    """"TimesNewRomanPS-BoldMT" -> [Times, New, Roman, PS, Bold, MT]."""
    words: List[str] = []
    for chunk in _CHUNK_SPLIT.split(base):
        words.extend(word for word in _WORD_SPLIT.split(chunk) if word)
    return words


def _tokenize_style_word(
    word: str, allow_partial: bool
) -> Optional[List[Tuple[str, bool, bool, str]]]:
    """Read ``word`` as style/PS/foundry tokens only, else ``None``.

    "BoldItalicMT" is three tokens; "Semibold" is not a style word, so a weight
    stays part of the family name.  ``allow_partial`` accepts a cut-off last
    token ("Ital", "M") on a name MuPDF truncated.
    """
    rest = word.lower()
    tokens: List[Tuple[str, bool, bool, str]] = []
    while rest:
        whole = next((t for t in _NAME_TOKENS if t[0] == rest), None)
        if whole is not None:
            tokens.append(whole)
            break
        # A cut-off tail is checked before the short tokens: "Ital" is a
        # truncated "Italic", not the token "It" followed by junk.
        candidates = (
            [t for t in _NAME_TOKENS if t[0].startswith(rest)] if allow_partial else []
        )
        if candidates:
            decisive = len(rest) >= 2
            tokens.append((
                rest,
                decisive and all(t[1] for t in candidates),
                decisive and all(t[2] for t in candidates),
                "partial",
            ))
            break
        head = next((t for t in _NAME_TOKENS if rest.startswith(t[0])), None)
        if head is None:
            return None
        tokens.append(head)
        rest = rest[len(head[0]):]
    return tokens or None


def _name_levels(
    words: List[str], truncated: bool, trust_partial: bool = True
) -> List[Tuple[List[str], bool, bool, List[str]]]:
    """Progressively strip trailing style words.

    Level 0 is the whole name; each further level drops one more trailing word
    that is purely style/PS/foundry tokens.  The first word is never dropped.
    Each level is ``(family words, bold, italic, kinds dropped so far)``.
    ``trust_partial`` is False when the span flags are known: a two-letter stump
    ("-Bo" may be "Book" as well as "Bold") then leaves the style to the flags,
    while three letters ("-Bol", "-Ita") name a style word by themselves.
    """
    levels = [(list(words), False, False, [])]
    remaining = list(words)
    bold = italic = False
    kinds: List[str] = []
    last = True
    while len(remaining) > 1:
        tokens = _tokenize_style_word(remaining[-1], allow_partial=truncated and last)
        last = False
        if tokens is None:
            break
        remaining = remaining[:-1]
        styled = [
            token for token in tokens
            if trust_partial or token[3] != "partial" or len(token[0]) > 2
        ]
        bold = bold or any(token[1] for token in styled)
        italic = italic or any(token[2] for token in styled)
        # Words are dropped right-to-left, so record a word's tokens that way too.
        kinds = kinds + [token[3] for token in reversed(tokens)]
        levels.append((list(remaining), bold, italic, list(kinds)))
    return levels


def _display_family(words: List[str]) -> str:
    """Join words, gluing a one-letter word back on ("RomanT" stays "RomanT")."""
    merged: List[str] = []
    for word in words:
        if merged and (len(word) == 1 or len(merged[-1]) == 1):
            merged[-1] += word
        else:
            merged.append(word)
    return " ".join(merged)


def _guess_family(levels) -> Tuple[str, bool, bool]:
    """Best portable family name for a font this machine does not know.

    PostScript order is Family[PS][-Style][MT]: a trailing "MT" is the foundry
    tag and goes, but an "MT" found *under* a style word belongs to the family
    ("GillSansMT-Bold" is family "Gill Sans MT").
    """
    chosen = levels[0]
    for level in levels[1:]:
        kinds = level[3]
        if kinds[-1] == "foundry" and "style" in kinds[:-1]:
            break
        chosen = level
    return _display_family(chosen[0]), chosen[1], chosen[2]


# ──────────────────────────────────────────────────────────────────────
# Resolver
# ──────────────────────────────────────────────────────────────────────
def _styled_face(faces: Dict[str, Any], family: str, bold: bool, italic: bool):
    """The installed face of ``family`` in this style (an oblique counts as italic).

    Every face is keyed by family + Bold/Italic as read from its style words,
    so "Swis721 Lt BT" finds its Light face and "Lucida Sans" + bold its Demibold.
    """
    return faces.get(_alnum_key(_host_name(family, bold, italic)))


def _flag_bits(flags: Any) -> Optional[int]:
    """Bold/italic bits of the span flags; ``None`` when there are no usable flags."""
    if flags is None:
        return None
    try:
        return int(flags) & (_FLAG_BOLD | _FLAG_ITALIC)
    except Exception:
        return None


def _resolve(
    raw: str, flag_bits: int, index: Dict[str, Any], flags_known: bool = True
) -> Dict[str, Any]:
    faces = index.get("faces") or {}
    families = index.get("families") or {}
    available = bool(index.get("available"))
    base = strip_subset_prefix(raw)
    key = _alnum_key(base)
    result: Dict[str, Any] = {
        "source_font": raw,
        "host_font": base or raw,
        "family": base or raw,
        "bold": False,
        "italic": False,
        "status": STATUS_NOT_INSTALLED if available else STATUS_UNVERIFIED,
        "font_file": None,
    }
    if not key:
        return result

    flag_bold = bool(flag_bits & _FLAG_BOLD)
    flag_italic = bool(flag_bits & _FLAG_ITALIC)
    truncated = _MUPDF_SPAN_FONT_NAME_LIMIT in (len(raw), len(base))
    face = faces.get(key)
    if face is None and truncated:
        # A cut-off name is proven only when exactly one installed face extends it.
        extended = {faces[k] for k in faces if k.startswith(key)}
        if len(extended) > 1 and flags_known and len({f.family for f in extended}) == 1:
            # "Swiss721BT-BlackCondense" is that face or its italic: within one
            # family the span flags say which.
            extended = {
                f for f in extended if (f.bold, f.italic) == (flag_bold, flag_italic)
            }
        if len(extended) == 1:
            face = extended.pop()
    if face is not None:
        # An untruncated name that is an installed face wins over the flags.  A
        # name cut at 24 characters may have lost its whole style suffix, so
        # there the flags may still add the style (handled as a family below).
        style_was_cut = truncated and (
            (flag_bold and not face.bold) or (flag_italic and not face.italic)
        )
        if not style_was_cut:
            host_font, status = _face_delivery(faces, face, STATUS_EXACT_FACE)
            result.update(
                host_font=host_font, family=face.family, bold=face.bold,
                italic=face.italic, status=status, font_file=face.path,
            )
            return result

    levels = _name_levels(_split_words(base), truncated, trust_partial=not flags_known)

    if available:
        for words, bold, italic, _kinds in levels:
            level_key = _alnum_key("".join(words))
            family = families.get(level_key)
            if family is None and level_key in faces:
                # "ArialMT,Bold": the stripped name is itself a face's PostScript name.
                level_face = faces[level_key]
                family = level_face.family
                bold, italic = bold or level_face.bold, italic or level_face.italic
            if family is None:
                continue
            bold, italic = bold or flag_bold, italic or flag_italic
            styled = _styled_face(faces, family, bold, italic)
            if styled is not None:
                host_font, status = _face_delivery(faces, styled, STATUS_FAMILY)
            else:
                host_font = _host_name(family, bold, italic)
                status = STATUS_FAMILY_STYLE_SYNTHESIZED
            result.update(
                host_font=host_font, family=family, bold=bold, italic=italic,
                status=status, font_file=styled.path if styled is not None else None,
            )
            return result
    else:
        for words, bold, italic, kinds in levels:
            family = _KNOWN_FAMILIES.get(_alnum_key("".join(words)))
            if family is not None:
                if truncated or "style" in kinds or "partial" in kinds:
                    # Same rule as above: a complete, untruncated face name
                    # ("Arial", "ArialMT") is not restyled by a stray flag.
                    bold, italic = bold or flag_bold, italic or flag_italic
                result.update(
                    host_font=_host_name(family, bold, italic), family=family,
                    bold=bold, italic=italic, status=STATUS_UNVERIFIED,
                )
                return result

    for words, bold, italic, _kinds in levels:
        alias = _ALIAS_FAMILIES.get(_alnum_key("".join(words)))
        if alias is None:
            continue
        bold, italic = bold or flag_bold, italic or flag_italic
        styled = _styled_face(faces, alias, bold, italic)
        alias_installed = not available or _alnum_key(alias) in families
        result.update(
            host_font=_host_name(alias, bold, italic), family=alias,
            bold=bold, italic=italic,
            status=STATUS_ALIAS if alias_installed else STATUS_ALIAS_NOT_INSTALLED,
            font_file=styled.path if styled is not None else None,
        )
        return result

    # Unknown to this machine.  Keep the true variant; never collapse it.
    family, bold, italic = _guess_family(levels)
    bold, italic = bold or flag_bold, italic or flag_italic
    result.update(bold=bold, italic=italic)
    if available and family and _CLEAN_NAME.match(base):
        result.update(host_font=_host_name(family, bold, italic), family=family)
    # Without an index the raw name is passed through, as it always was.
    return result


def resolve_host_font(
    font_name: Any, flags: Any = None, index: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Resolve one PDF span font name (+ PyMuPDF span ``flags``) for the host.

    Returns ``{source_font, host_font, family, bold, italic, status,
    font_file}``.  Empty input gives ``host_font == ""``; non-empty input never
    does.  ``index`` defaults to the lazily built installed-font index.
    """
    try:
        if isinstance(font_name, (bytes, bytearray)):
            try:
                font_name = bytes(font_name).decode("utf-8")
            except UnicodeDecodeError:
                font_name = bytes(font_name).decode("latin-1")
        raw = str(font_name or "").strip()
    except Exception:
        raw = ""  # a name that cannot even be read as text is no name
    if not raw:
        return {
            "source_font": "", "host_font": "", "family": "", "bold": False,
            "italic": False, "status": STATUS_EMPTY, "font_file": None,
        }
    flag_bits = _flag_bits(flags)
    cache_key = (raw, flag_bits)
    if index is None:
        cached = _RESOLVE_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)
    try:
        active = installed_font_index() if index is None else index
        result = _resolve(
            raw, flag_bits or 0, active if isinstance(active, dict) else {},
            flags_known=flag_bits is not None,
        )
    except Exception:
        result = {
            "source_font": raw, "host_font": strip_subset_prefix(raw) or raw,
            "family": strip_subset_prefix(raw) or raw, "bold": False,
            "italic": False, "status": STATUS_UNVERIFIED, "font_file": None,
        }
    if not result.get("host_font"):
        result["host_font"] = raw
    if index is None:
        _RESOLVE_CACHE[cache_key] = dict(result)
    return result


# ──────────────────────────────────────────────────────────────────────
# Import-report summary
# ──────────────────────────────────────────────────────────────────────
def summarize_host_fonts(attempts: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarise the fonts of delivered native text for the import report.

    Reads the per-item delivery ledger (``evidence.source_font`` /
    ``font_name`` / ``font_status``).  ``host_font_map`` lists every source
    font; ``host_font_substitutions`` is the subset the host does not draw with
    the source font itself.  ``host_font_unverified`` names the fonts that could
    not be checked against this host at all; they are neither substitutions nor
    warnings.  ``note`` is the human wording ("" when clean) and
    ``console_warnings`` holds one line per substituted font.
    """
    font_map: Dict[str, Dict[str, Any]] = {}
    for attempt in attempts or ():
        if not isinstance(attempt, dict) or attempt.get("outcome") != "verified":
            continue
        evidence = attempt.get("evidence")
        if not isinstance(evidence, dict):
            continue
        status = str(evidence.get("font_status") or "").strip()
        if not status:
            continue
        source_font = str(evidence.get("source_font") or "").strip() or "(no font name)"
        host_font = str(evidence.get("font_name") or "")
        key = source_font
        entry = font_map.get(key)
        if entry is not None and (
            entry["host_font"] != host_font or entry["status"] != status
        ):
            # Same PDF name, different span flags: keep both mappings visible.
            key = "%s -> %s" % (source_font, host_font)
            entry = font_map.get(key)
        if entry is None:
            entry = font_map[key] = {
                # The key carries " -> host font" when one PDF name maps twice.
                "source_font": source_font,
                "host_font": host_font,
                "status": status,
                "span_count": 0,
                "example_source_item_ids": [],
            }
        entry["span_count"] += 1
        source_item_id = str(attempt.get("source_item_id") or "")
        examples = entry["example_source_item_ids"]
        if source_item_id and len(examples) < 3 and source_item_id not in examples:
            examples.append(source_item_id)

    unverified = {
        key: entry for key, entry in font_map.items()
        if entry["status"] == STATUS_UNVERIFIED
    }
    substitutions = {
        key: dict(entry, source_font_equivalent=False)
        for key, entry in font_map.items()
        if entry["status"] not in SOURCE_FONT_EQUIVALENT_STATUSES and key not in unverified
    }
    console_warnings = [
        "PDF font '%s' is delivered as host font '%s' (%s, %d span%s): "
        "source-font non-equivalent, text width may differ; height is unchanged."
        % (
            key.split(" -> ", 1)[0], entry["host_font"] or "host default",
            entry["status"], entry["span_count"],
            "" if entry["span_count"] == 1 else "s",
        )
        for key, entry in substitutions.items()
    ]
    note = ""
    if substitutions:
        shown = [
            "%s -> %s [%s]" % (
                key.split(" -> ", 1)[0], entry["host_font"] or "host default",
                entry["status"].replace("_", " "),
            )
            for key, entry in list(substitutions.items())[:5]
        ]
        if len(substitutions) > 5:
            shown.append("+%d more" % (len(substitutions) - 5))
        spans = sum(entry["span_count"] for entry in substitutions.values())
        note = (
            "Host font substitution: %d PDF font%s (%d text item%s) not drawn "
            "with the source font itself (%s); text height is unchanged but "
            "width may differ from the PDF"
            % (
                len(substitutions), "" if len(substitutions) == 1 else "s",
                spans, "" if spans == 1 else "s", "; ".join(shown),
            )
        )
    if unverified:
        spans = sum(entry["span_count"] for entry in unverified.values())
        unchecked = (
            "Host fonts not checked: %d PDF font%s (%d text item%s) could not be "
            "compared with the fonts installed on this host; text height is "
            "unchanged, width depends on the font the host picks"
            % (
                len(unverified), "" if len(unverified) == 1 else "s",
                spans, "" if spans == 1 else "s",
            )
        )
        note = ". ".join(part for part in (note, unchecked) if part)
    return {
        "host_font_map": font_map,
        "host_font_substitutions": substitutions,
        "host_font_unverified": sorted({entry["source_font"] for entry in unverified.values()}),
        "note": note,
        "console_warnings": console_warnings,
    }


__all__ = [
    "SOURCE_FONT_EQUIVALENT_STATUSES",
    "build_font_index",
    "font_key",
    "index_from_records",
    "installed_font_index",
    "read_font_names",
    "resolve_host_font",
    "strip_subset_prefix",
    "summarize_host_fonts",
]
