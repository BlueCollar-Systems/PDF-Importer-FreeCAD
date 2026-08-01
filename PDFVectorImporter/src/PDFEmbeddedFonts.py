# -*- coding: utf-8 -*-
"""Stage exact PDF-embedded fonts for native FreeCAD ShapeString text."""

from __future__ import annotations

import hashlib
import io
import math
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Dict, List, Optional, Tuple


class EmbeddedFontInventoryError(RuntimeError):
    """The PDF page's font inventory could not be enumerated reliably."""


def normalize_font_key(name: str) -> str:
    raw = str(name or "").strip()
    if "+" in raw:
        prefix, remainder = raw.split("+", 1)
        if len(prefix) == 6 and prefix.isupper():
            raw = remainder
    return re.sub(r"[^a-z0-9]", "", raw.lower())


def _finite_number(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def convert_cff_to_otf(cff_bytes: bytes, family_hint: str = "Embedded PDF Font") -> bytes:
    """Wrap a raw Type1 CFF program in a valid OpenType container.

    PyMuPDF returns bare CFF bytes for embedded Type1 fonts.  FreeCAD's
    ShapeString rejects bare CFF but accepts OpenType/CFF, so this builds the
    missing SFNT tables without changing the original outlines or widths.
    """
    from fontTools.agl import toUnicode
    from fontTools.cffLib import CFFFontSet
    from fontTools.fontBuilder import FontBuilder
    from fontTools.pens.basePen import NullPen
    from fontTools.ttLib import TTFont

    if not isinstance(cff_bytes, (bytes, bytearray)) or not cff_bytes:
        raise ValueError("empty CFF payload")

    cff = CFFFontSet()
    cff.decompile(io.BytesIO(bytes(cff_bytes)), None)
    if not cff.topDictIndex:
        raise ValueError("CFF payload has no Top DICT")
    cff.desubroutinize()
    top = cff.topDictIndex[0]
    glyph_order = list(getattr(top, "charset", []) or [])
    if not glyph_order or glyph_order[0] != ".notdef":
        if ".notdef" in glyph_order:
            glyph_order.remove(".notdef")
        glyph_order.insert(0, ".notdef")

    matrix = list(getattr(top, "FontMatrix", []) or [])
    matrix_scale = abs(_finite_number(matrix[0], 0.001)) if matrix else 0.001
    units_per_em = int(round(1.0 / matrix_scale)) if matrix_scale > 1e-12 else 1000
    units_per_em = max(16, min(16384, units_per_em))

    char_strings = {}
    metrics = {}
    for glyph_name in glyph_order:
        char_string = top.CharStrings[glyph_name]
        char_string.draw(NullPen())
        width = int(round(_finite_number(getattr(char_string, "width", 0), 0.0)))
        metrics[glyph_name] = (max(0, width), 0)
        char_strings[glyph_name] = char_string

    cmap: Dict[int, str] = {}
    encoding = getattr(top, "Encoding", None)
    if isinstance(encoding, (list, tuple)):
        for codepoint, glyph_name in enumerate(encoding):
            if glyph_name in char_strings and glyph_name != ".notdef":
                cmap[int(codepoint)] = glyph_name
    for glyph_name in glyph_order:
        if glyph_name == ".notdef" or glyph_name in cmap.values():
            continue
        try:
            unicode_text = toUnicode(glyph_name)
        except (KeyError, TypeError, ValueError):
            unicode_text = ""
        if len(unicode_text) == 1:
            cmap.setdefault(ord(unicode_text), glyph_name)

    raw_family = str(family_hint or "Embedded PDF Font").strip()
    if "+" in raw_family:
        prefix, remainder = raw_family.split("+", 1)
        if len(prefix) == 6 and prefix.isupper():
            raw_family = remainder
    family = re.sub(r"[-_]+", " ", raw_family).strip() or "Embedded PDF Font"
    ps_name = re.sub(r"[^A-Za-z0-9-]", "", raw_family.replace(" ", "-"))
    ps_name = ps_name or "EmbeddedPDFFont-Regular"

    bbox = list(getattr(top, "FontBBox", []) or [0, -200, units_per_em, units_per_em])
    while len(bbox) < 4:
        bbox.append(0)
    ascent = int(math.ceil(_finite_number(bbox[3], units_per_em * 0.8)))
    descent = int(math.floor(_finite_number(bbox[1], -units_per_em * 0.2)))
    if ascent <= 0:
        ascent = int(units_per_em * 0.8)
    if descent > 0:
        descent = -int(units_per_em * 0.2)

    builder = FontBuilder(units_per_em, isTTF=False)
    builder.setupGlyphOrder(glyph_order)
    builder.setupCharacterMap(cmap)
    builder.setupHorizontalMetrics(metrics)
    builder.setupHorizontalHeader(ascent=ascent, descent=descent)
    builder.setupNameTable(
        {
            "familyName": family,
            "styleName": "Regular",
            "uniqueFontIdentifier": ps_name,
            "fullName": family,
            "psName": ps_name,
        }
    )
    builder.setupOS2(
        sTypoAscender=ascent,
        sTypoDescender=descent,
        usWinAscent=max(0, ascent),
        usWinDescent=max(0, -descent),
    )
    builder.setupPost()
    builder.setupCFF(
        ps_name,
        {"FullName": family, "FamilyName": family, "Weight": "Regular"},
        char_strings,
        {},
    )
    if "head" in builder.font:
        # FontBuilder otherwise stamps the current second, producing a new
        # content-addressed cache file on identical imports. Use fontTools'
        # lower bound for a valid OpenType timestamp (1970-01-01) so readers
        # do not reinterpret a zero value as a Unix timestamp.
        deterministic_timestamp = 0x7C259DC0
        builder.font["head"].created = deterministic_timestamp
        builder.font["head"].modified = deterministic_timestamp
    builder.font.recalcTimestamp = False
    output = io.BytesIO()
    builder.font.save(output, reorderTables=False)
    payload = output.getvalue()

    # Reject malformed output before it can become a ShapeString dependency.
    validated = TTFont(io.BytesIO(payload), lazy=False)
    if "CFF " not in validated or not validated.getGlyphOrder():
        raise ValueError("converted OpenType font failed validation")
    return payload


def _xref_reference(pdf_doc, xref: int, key: str) -> Optional[int]:
    try:
        _kind, value = pdf_doc.xref_get_key(int(xref), str(key))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    match = re.search(r"(?:\[\s*)?(\d+)\s+0\s+R", str(value or ""))
    if not match:
        return None
    reference = int(match.group(1))
    return reference if reference > 0 else None


def _xref_name(pdf_doc, xref: int, key: str) -> str:
    try:
        _kind, value = pdf_doc.xref_get_key(int(xref), str(key))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""
    return str(value or "").strip()


def _xref_contained_reference(pdf_doc, xref: int) -> Optional[int]:
    try:
        value = pdf_doc.xref_object(int(xref), compressed=False)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return None
    match = re.search(r"(?:\[\s*)?(\d+)\s+0\s+R", str(value or ""))
    if not match:
        return None
    reference = int(match.group(1))
    return reference if reference > 0 else None


def _unicode_scalar_from_hex(value: str) -> Optional[int]:
    try:
        raw = bytes.fromhex(str(value or ""))
        decoded = raw.decode("utf-16-be")
    except (UnicodeDecodeError, ValueError):
        return None
    if len(decoded) != 1:
        return None
    return ord(decoded)


def _parse_tounicode_cmap(payload: bytes) -> Dict[int, int]:
    """Return PDF character-code/CID to Unicode-scalar mappings."""
    text = bytes(payload or b"").decode("latin1", errors="ignore")
    result: Dict[int, int] = {}

    for block in re.findall(r"beginbfchar(.*?)endbfchar", text, flags=re.I | re.S):
        for source, destination in re.findall(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", block
        ):
            scalar = _unicode_scalar_from_hex(destination)
            if scalar is not None:
                result[int(source, 16)] = scalar

    for block in re.findall(r"beginbfrange(.*?)endbfrange", text, flags=re.I | re.S):
        line_pattern = re.compile(
            r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(<([0-9A-Fa-f]+)>|\[(.*?)\])"
        )
        for match in line_pattern.finditer(block):
            start = int(match.group(1), 16)
            end = int(match.group(2), 16)
            if end < start:
                continue
            if match.group(4):
                base = _unicode_scalar_from_hex(match.group(4))
                if base is None:
                    continue
                for offset, source in enumerate(range(start, end + 1)):
                    scalar = base + offset
                    if 0 <= scalar <= 0x10FFFF:
                        result[source] = scalar
                continue
            destinations = re.findall(r"<([0-9A-Fa-f]+)>", match.group(5) or "")
            for source, destination in zip(
                range(start, end + 1), destinations, strict=False
            ):
                scalar = _unicode_scalar_from_hex(destination)
                if scalar is not None:
                    result[source] = scalar
    return result


def _install_unicode_cmap(font, cmap: Dict[int, str]) -> None:
    from fontTools.ttLib import newTable
    from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

    unicode_map = {
        int(codepoint): str(glyph_name)
        for codepoint, glyph_name in cmap.items()
        if 0 <= int(codepoint) <= 0x10FFFF and str(glyph_name or "")
    }
    table = newTable("cmap")
    table.tableVersion = 0
    table.tables = []

    bmp_map = {codepoint: name for codepoint, name in unicode_map.items() if codepoint <= 0xFFFF}
    if bmp_map:
        bmp = CmapSubtable.newSubtable(4)
        bmp.platformID = 3
        bmp.platEncID = 1
        bmp.language = 0
        bmp.cmap = bmp_map
        table.tables.append(bmp)

    full = CmapSubtable.newSubtable(12)
    full.platformID = 3
    full.platEncID = 10
    full.language = 0
    full.cmap = unicode_map
    table.tables.append(full)
    font["cmap"] = table


def _repair_type0_identity_truetype_cmap(
    payload: bytes,
    pdf_doc,
    font_xref: int,
    pdf_encoding: str,
) -> Tuple[bytes, Dict[str, Any]]:
    """Restore an omitted subset cmap from the PDF's authoritative maps.

    Type0 Identity-H fonts map content codes directly to CIDs.  When the
    descendant declares CIDToGIDMap /Identity, those CIDs are also exact glyph
    IDs in the embedded TrueType program.  Combining that identity with the
    parent ToUnicode CMap restores Unicode lookup without substituting a font
    or changing any glyph outlines.
    """
    from fontTools.ttLib import TTFont

    if str(pdf_encoding or "").strip().lower() != "identity-h":
        return bytes(payload), {}
    to_unicode_xref = _xref_reference(pdf_doc, font_xref, "ToUnicode")
    descendant_xref = _xref_reference(pdf_doc, font_xref, "DescendantFonts")
    if not to_unicode_xref or not descendant_xref:
        return bytes(payload), {}
    cid_to_gid = _xref_name(pdf_doc, descendant_xref, "CIDToGIDMap").lower()
    if cid_to_gid != "/identity":
        contained = _xref_contained_reference(pdf_doc, descendant_xref)
        if contained:
            descendant_xref = contained
            cid_to_gid = _xref_name(pdf_doc, descendant_xref, "CIDToGIDMap").lower()
    if cid_to_gid != "/identity":
        return bytes(payload), {}
    try:
        unicode_by_cid = _parse_tounicode_cmap(pdf_doc.xref_stream(to_unicode_xref))
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return bytes(payload), {}
    if not unicode_by_cid:
        return bytes(payload), {}

    font = TTFont(io.BytesIO(bytes(payload)), lazy=False)
    glyph_order = list(font.getGlyphOrder())
    existing: Dict[int, str] = {}
    if "cmap" in font:
        existing = dict(font.getBestCmap() or {})
    restored_gids = {
        unicode_scalar: cid
        for cid, unicode_scalar in unicode_by_cid.items()
        if 0 <= cid < len(glyph_order)
    }
    restored = {
        unicode_scalar: glyph_order[cid]
        for unicode_scalar, cid in restored_gids.items()
    }
    if not restored:
        return bytes(payload), {}
    existing.update(restored)
    _install_unicode_cmap(font, existing)
    font.recalcTimestamp = False
    output = io.BytesIO()
    font.save(output, reorderTables=False)
    repaired = output.getvalue()
    validated = TTFont(io.BytesIO(repaired), lazy=False)
    validated_cmap = dict(validated.getBestCmap() or {})
    if any(
        codepoint not in validated_cmap
        or validated.getGlyphID(validated_cmap[codepoint]) != glyph_id
        for codepoint, glyph_id in restored_gids.items()
    ):
        raise ValueError("repaired TrueType cmap failed validation")
    return repaired, {
        "cmap_source": "pdf_tounicode_identity_h",
        "cmap_entries": len(restored),
    }


def _atomic_content_addressed_write(cache_dir: Path, payload: bytes, extension: str) -> Path:
    digest = hashlib.sha256(payload).hexdigest()
    suffix = "." + re.sub(r"[^a-z0-9]", "", extension.lower().lstrip("."))
    if suffix == ".":
        suffix = ".otf"
    target = cache_dir / (digest + suffix)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
        return target

    descriptor, temp_path = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(cache_dir)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        try:
            os.remove(temp_path)
        except OSError:
            pass
    if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise OSError("font cache verification failed after atomic write")
    return target


def stage_page_fonts(
    pdf_doc,
    page,
    cache_dir,
    *,
    failures: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Extract exact embedded page fonts into a content-addressed local cache.

    Each embedded font is an independent source item.  A malformed font is
    recorded and skipped without preventing other exact embedded fonts on the
    page from being staged.  The caller still gets an explicit terminal error
    if a later text item needs the failed font.
    """
    from fontTools.ttLib import TTFont

    cache_path = Path(cache_dir)
    staged: Dict[str, Dict[str, Any]] = {}
    try:
        rows = list(page.get_fonts(full=True) or [])
    except Exception as exc:
        raise EmbeddedFontInventoryError(
            "embedded font inventory failed: %s: %s"
            % (exc.__class__.__name__, exc)
        ) from exc
    def record_inventory_result(
        *,
        xref,
        font: str,
        outcome: str,
        reason: str,
        exception: str = "",
        **details: Any,
    ) -> None:
        if failures is None:
            return
        result = {
            "xref": xref,
            "font": str(font or "").strip(),
            "outcome": outcome,
            "reason": reason,
            "exception": exception,
        }
        result.update(details)
        failures.append(result)

    for row in rows:
        if not isinstance(row, (tuple, list)) or len(row) < 4:
            record_inventory_result(
                xref=None,
                font="",
                outcome="failed",
                reason="embedded_font_inventory_row_invalid",
            )
            continue
        font_type_hint = str(row[2] or "").strip()
        font_hint = str(row[3] or "").strip()
        resource_hint = str(row[4] or "").strip() if len(row) > 4 else ""
        if font_type_hint.lower() == "type3":
            # A Type3 font never has an extractable font program: its glyphs are
            # PDF content streams referenced by the font dictionary, not a TTF or
            # CFF payload. That is true whether or not the inventory row carries
            # an xref, and real documents DO carry one -- measured on the owner's
            # corpus, all 14 Type3 rows on `Alvord TX - Garden Plan . Final_OCR`
            # have xrefs (32, 33, 42, ...) with an empty BaseFont name and only a
            # resource name (R104, R110, ...). Gating this branch on
            # `row[0] is None` meant those rows fell through to the empty-name
            # check below and were recorded as `embedded_font_inventory_row_invalid`
            # -- reported as corrupt data, which aborted the whole import. Type3
            # affects 4 of the 60 corpus PDFs, up to 56 of 88 fonts on a page.
            observed_names = []
            # PyMuPDF names an unnamed Type3 font synthetically as
            # "Type3 (<xref> 0 R)" and that is the name that arrives on the text
            # span. The inventory row only carries the resource name (R47), so
            # without this alias the span's font identity never matches any
            # recorded observation, the resolver reports
            # `font_not_observed_in_completed_inventory`, and the import aborts.
            synthetic_type3_name = (
                "Type3 (%d 0 R)" % row[0] if isinstance(row[0], int) else ""
            )
            for observed_name in (synthetic_type3_name, font_hint, resource_hint):
                if observed_name and observed_name not in observed_names:
                    observed_names.append(observed_name)
            if not observed_names:
                record_inventory_result(
                    xref=None,
                    font="",
                    outcome="failed",
                    reason="embedded_font_inventory_row_invalid",
                )
                continue
            inventory_xref = row[0] if isinstance(row[0], int) else None
            for observed_name in observed_names:
                record_inventory_result(
                    xref=0,
                    font=observed_name,
                    outcome="not_embedded",
                    reason="embedded_type3_font_program_unavailable",
                    # Report the real inventory xref when the PDF has one rather
                    # than a blanket None; the absence being proven is of a font
                    # PROGRAM, not of the font object.
                    inventory_xref=inventory_xref,
                    font_type=font_type_hint,
                )
            continue
        if not font_hint:
            record_inventory_result(
                xref=None,
                font="",
                outcome="failed",
                reason="embedded_font_inventory_row_invalid",
            )
            continue
        if type(row[0]) is not int:
            record_inventory_result(
                xref=None,
                font=font_hint,
                outcome="failed",
                reason="embedded_font_inventory_xref_invalid",
            )
            continue
        xref = row[0]
        if xref < 0:
            record_inventory_result(
                xref=xref,
                font=font_hint,
                outcome="failed",
                reason="embedded_font_inventory_xref_invalid",
            )
            continue
        if xref == 0:
            observed_names = [font_hint]
            if len(row) > 4:
                observed_names.append(resource_hint)
            seen_names = set()
            for observed_name in observed_names:
                normalized_name = normalize_font_key(observed_name)
                if not normalized_name or normalized_name in seen_names:
                    continue
                seen_names.add(normalized_name)
                record_inventory_result(
                    xref=0,
                    font=observed_name,
                    outcome="not_embedded",
                    reason="embedded_font_not_present",
                )
            if not seen_names:
                record_inventory_result(
                    xref=0,
                    font="",
                    outcome="failed",
                    reason="embedded_font_inventory_row_invalid",
                )
            continue
        try:
            extracted_name, extension, font_type, payload = pdf_doc.extract_font(xref)
        except (AttributeError, RuntimeError, TypeError, ValueError, OSError) as exc:
            if failures is not None:
                failures.append({
                    "xref": xref,
                    "font": font_hint,
                    "outcome": "failed",
                    "reason": "embedded_font_extraction_failed",
                    "exception": f"{exc.__class__.__name__}: {exc}",
                })
            continue
        if not payload:
            record_inventory_result(
                xref=xref,
                font=str(extracted_name or font_hint),
                outcome="failed",
                reason="embedded_font_payload_empty",
            )
            continue
        try:
            extension = str(extension or "").strip().lower()
            output_payload = bytes(payload)
            output_extension = extension
            repair_metadata: Dict[str, Any] = {}
            if (
                extension in {"cff", "cid", "type1"}
                or str(font_type or "").lower() == "type1"
            ):
                output_payload = convert_cff_to_otf(
                    output_payload, str(extracted_name or font_hint)
                )
                output_extension = "otf"
            elif extension not in {"ttf", "otf", "ttc"}:
                record_inventory_result(
                    xref=xref,
                    font=str(extracted_name or font_hint),
                    outcome="failed",
                    reason="embedded_font_format_unsupported",
                )
                continue
            elif str(font_type or row[2] or "").strip().lower() == "type0":
                output_payload, repair_metadata = _repair_type0_identity_truetype_cmap(
                    output_payload,
                    pdf_doc,
                    xref,
                    str(row[5] if len(row) > 5 else ""),
                )

            # Validate every staged font, not just converted CFF assets.
            validated = TTFont(io.BytesIO(output_payload), lazy=False)
            if "cmap" not in validated or not validated.getBestCmap():
                if failures is not None:
                    failures.append({
                        "xref": xref,
                        "font": str(extracted_name or font_hint),
                        "outcome": "failed",
                        "reason": "embedded_font_has_no_unicode_cmap",
                        "exception": "",
                    })
                continue
            target = _atomic_content_addressed_write(
                cache_path, output_payload, output_extension
            )
            digest = hashlib.sha256(output_payload).hexdigest()
        except Exception as exc:
            # fontTools exposes several dependency-specific parse exceptions.
            # Record the exact one; never treat it as authority to change the
            # requested text representation or substitute another font.
            if failures is not None:
                failures.append({
                    "xref": xref,
                    "font": str(extracted_name or font_hint),
                    "outcome": "failed",
                    "reason": "embedded_font_staging_failed",
                    "exception": f"{exc.__class__.__name__}: {exc}",
                })
            continue
        record = {
            "path": str(target),
            "sha256": digest,
            "source": "pdf_embedded",
            "xref": xref,
            "pdf_extension": extension,
            "staged_extension": output_extension,
        }
        record.update(repair_metadata)
        names = [
            extracted_name,
            row[3] if len(row) > 3 else "",
            row[4] if len(row) > 4 else "",
        ]
        for name in names:
            key = normalize_font_key(str(name or ""))
            if key:
                staged[key] = dict(record)
    return staged


__all__ = [
    "EmbeddedFontInventoryError",
    "convert_cff_to_otf",
    "normalize_font_key",
    "stage_page_fonts",
]
