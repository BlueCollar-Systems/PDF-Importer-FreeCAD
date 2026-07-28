"""Exact, fail-closed FCStd persistence evidence for imported raster assets."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import stat
import zlib
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree


SCHEMA = "bcs.freecad_fcstd_raster_archive/1.0"
METHOD = "fcstd_property_file_included_sha256"
_HASH_CHUNK_BYTES = 1024 * 1024
_SUPPORTED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_PAGE_RASTER_VARIANTS = {
    "full_page_original",
    "text_suppressed_page_background",
    "original_page_no_canonical_text",
}
_SUPPRESSION_VARIANTS = {
    "text_suppressed_page_background",
    "original_page_no_canonical_text",
}
_SUPPRESSION_CONTENT_KEYS = (
    "text_suppression_schema",
    "text_suppression_method",
    "text_suppression_evidence_json",
    "text_suppression_evidence_sha256",
    "text_suppression_source_item_ids_json",
    "text_suppression_source_item_ids_sha256",
    "text_suppression_source_item_count",
    "text_suppression_delivered_item_ids_json",
    "text_suppression_delivered_item_ids_sha256",
    "text_suppression_delivery_bound",
    "text_suppression_verified",
)
_SUPPRESSION_PROPERTY_SPECS = (
    ("text_suppression_schema", "PDFTextSuppressionSchema", "App::PropertyString", "String"),
    ("text_suppression_method", "PDFTextSuppressionMethod", "App::PropertyString", "String"),
    ("text_suppression_evidence_json", "PDFTextSuppressionEvidenceJSON", "App::PropertyString", "String"),
    ("text_suppression_evidence_sha256", "PDFTextSuppressionEvidenceSHA256", "App::PropertyString", "String"),
    ("text_suppression_source_item_ids_json", "PDFTextSuppressionSourceItemIDsJSON", "App::PropertyString", "String"),
    ("text_suppression_source_item_ids_sha256", "PDFTextSuppressionSourceItemIDsSHA256", "App::PropertyString", "String"),
    ("text_suppression_source_item_count", "PDFTextSuppressionSourceItemCount", "App::PropertyInteger", "Integer"),
    ("text_suppression_delivered_item_ids_json", "PDFTextSuppressionDeliveredItemIDsJSON", "App::PropertyString", "String"),
    ("text_suppression_delivered_item_ids_sha256", "PDFTextSuppressionDeliveredItemIDsSHA256", "App::PropertyString", "String"),
    ("text_suppression_delivery_bound", "PDFTextSuppressionDeliveryBound", "App::PropertyBool", "Bool"),
    ("text_suppression_verified", "PDFTextSuppressionVerified", "App::PropertyBool", "Bool"),
)


class EvidenceError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = str(reason)


class _CancellationPassthrough(BaseException):
    def __init__(self, original: BaseException):
        super().__init__(str(original))
        self.original = original


def _checkpoint(cancel_check: Optional[Callable[[], Any]]) -> None:
    if not callable(cancel_check):
        return
    try:
        cancel_check()
    except BaseException as exc:
        raise _CancellationPassthrough(exc) from exc


def evidence_digest(evidence: Any) -> str:
    if not isinstance(evidence, dict):
        return ""
    payload = {key: value for key, value in evidence.items() if key != "evidence_digest"}
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def inventory_digest(inventory: Any) -> str:
    if not isinstance(inventory, dict):
        return ""
    payload = {
        key: value for key, value in inventory.items() if key != "inventory_digest"
    }
    try:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return ""
    return hashlib.sha256(encoded).hexdigest()


def failure(reason: str) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "method": METHOD,
        "verified": False,
        "reason": str(reason or "fcstd_raster_archive_evidence_failed"),
    }


def required_included_rasters(inventory: Any) -> Dict[str, Dict[str, Any]]:
    """Derive obligations from independently captured live-host content."""

    if not isinstance(inventory, dict):
        raise EvidenceError("raster_inventory_invalid")
    required: Dict[str, Dict[str, Any]] = {}
    for record in list(inventory.get("objects") or []):
        if not isinstance(record, dict):
            continue
        type_id = str(record.get("type_id") or "")
        representation = str(record.get("representation") or "")
        if representation != "raster" and not type_id.startswith("Image::"):
            continue
        entity_id = str(record.get("entity_id") or "")
        content = record.get("content")
        if not entity_id or entity_id in required or not isinstance(content, dict):
            raise EvidenceError("raster_inventory_identity_invalid")
        sha256 = str(content.get("declared_raster_sha256") or "").lower()
        pdf_source_sha256 = str(content.get("pdf_source_sha256") or "").lower()
        byte_count = content.get("included_image_bytes")
        if (
            re.fullmatch(r"[0-9a-f]{64}", sha256) is None
            or re.fullmatch(r"[0-9a-f]{64}", pdf_source_sha256) is None
            or type(byte_count) is not int
            or byte_count <= 0
            or content.get("raster_asset_binding_verified") is not True
        ):
            raise EvidenceError("raster_inventory_binding_invalid")

        source_item_id = str(record.get("source_item_id") or "")
        page_number = content.get("embedded_image_page_number")
        if type(page_number) is not int or page_number <= 0:
            match = re.match(r"^p([1-9][0-9]*):", source_item_id)
            page_number = int(match.group(1)) if match else None
        if type(page_number) is not int or page_number <= 0:
            raise EvidenceError("raster_page_identity_invalid")
        page_scope = re.fullmatch(r"p[1-9][0-9]*:page", source_item_id) is not None
        variant = str(content.get("raster_content_variant") or "")
        suppression_present = variant in _SUPPRESSION_VARIANTS
        if page_scope and (
            variant not in _PAGE_RASTER_VARIANTS
            or content.get("raster_content_variant_property_verified") is not True
        ):
            raise EvidenceError("page_raster_content_variant_invalid")
        if not page_scope and variant:
            raise EvidenceError("nonpage_raster_content_variant_invalid")
        if variant == "full_page_original" and any(
            key in content for key in _SUPPRESSION_CONTENT_KEYS
        ):
            raise EvidenceError("original_page_raster_has_suppression_metadata")
        if suppression_present:
            _validate_suppression_content(content, variant)
        source_xref = content.get("embedded_image_source_xref")
        if source_xref is not None and (
            type(source_xref) is not int or source_xref < 0
        ):
            raise EvidenceError("raster_xref_identity_invalid")

        occurrence_index = content.get("image_occurrence_index")
        occurrence_json = str(content.get("image_occurrence_json") or "")
        occurrence_sha256 = str(
            content.get("image_occurrence_evidence_sha256") or ""
        ).lower()
        source_has_mask = content.get("image_source_has_mask")
        occurrence_present = any(
            value is not None and value != ""
            for value in (
                occurrence_index,
                occurrence_json,
                occurrence_sha256,
                source_has_mask,
            )
        )
        if occurrence_present and (
            type(occurrence_index) is not int
            or occurrence_index < 0
            or not occurrence_json
            or re.fullmatch(r"[0-9a-f]{64}", occurrence_sha256) is None
            or hashlib.sha256(occurrence_json.encode("utf-8")).hexdigest()
            != occurrence_sha256
            or type(source_has_mask) is not bool
            or source_xref is None
        ):
            raise EvidenceError("raster_occurrence_binding_invalid")

        required[entity_id] = {
            "entity_id": entity_id,
            "sha256": sha256,
            "bytes": byte_count,
            "pdf_source_sha256": pdf_source_sha256,
            "page_number": page_number,
            "source_xref": source_xref,
            "occurrence_index": occurrence_index if occurrence_present else None,
            "occurrence_evidence_sha256": (
                occurrence_sha256 if occurrence_present else ""
            ),
            "occurrence_json": occurrence_json if occurrence_present else "",
            "source_has_mask": source_has_mask if occurrence_present else None,
            "source_item_id": source_item_id,
            "representation": representation,
            "x_size": content.get("raster_x_size", content.get("x_size")),
            "y_size": content.get("raster_y_size", content.get("y_size")),
            "anchor_xyz": copy.deepcopy(
                content.get("raster_anchor_xyz", content.get("anchor_xyz"))
            ),
        }
        if page_scope:
            required[entity_id].update(
                {
                    "raster_content_variant": variant,
                    "text_suppression_present": suppression_present,
                }
            )
            if suppression_present:
                required[entity_id].update(
                    {
                        key: copy.deepcopy(content.get(key))
                        for key in _SUPPRESSION_CONTENT_KEYS
                    }
                )
    return required


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_suppression_content(content: Dict[str, Any], variant: str) -> None:
    if (
        content.get("page_text_suppression_binding_verified") is not True
        or content.get("text_suppression_delivery_bound") is not True
        or content.get("text_suppression_verified") is not True
    ):
        raise EvidenceError("page_text_suppression_binding_invalid")
    evidence_json = content.get("text_suppression_evidence_json")
    source_json = content.get("text_suppression_source_item_ids_json")
    delivered_json = content.get("text_suppression_delivered_item_ids_json")
    if not all(isinstance(value, str) and value for value in (evidence_json, source_json, delivered_json)):
        raise EvidenceError("page_text_suppression_json_invalid")
    try:
        evidence = json.loads(evidence_json)
        source_ids = json.loads(source_json)
        delivered_ids = json.loads(delivered_json)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EvidenceError("page_text_suppression_json_invalid") from exc
    if (
        not isinstance(evidence, dict)
        or not isinstance(source_ids, list)
        or not isinstance(delivered_ids, list)
        or any(not isinstance(value, str) or not value for value in source_ids)
        or any(not isinstance(value, str) or not value for value in delivered_ids)
        or len(set(source_ids)) != len(source_ids)
        or source_ids != delivered_ids
        or _canonical_json(evidence) != evidence_json
        or _canonical_json(source_ids) != source_json
        or _canonical_json(delivered_ids) != delivered_json
    ):
        raise EvidenceError("page_text_suppression_json_invalid")
    evidence_digest = str(content.get("text_suppression_evidence_sha256") or "")
    evidence_payload = {
        key: value for key, value in evidence.items() if key != "evidence_sha256"
    }
    source_digest = str(content.get("text_suppression_source_item_ids_sha256") or "")
    delivered_digest = str(content.get("text_suppression_delivered_item_ids_sha256") or "")
    if (
        re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is None
        or hashlib.sha256(_canonical_json(evidence_payload).encode("utf-8")).hexdigest()
        != evidence_digest
        or evidence.get("evidence_sha256") != evidence_digest
        or re.fullmatch(r"[0-9a-f]{64}", source_digest) is None
        or hashlib.sha256(source_json.encode("utf-8")).hexdigest() != source_digest
        or re.fullmatch(r"[0-9a-f]{64}", delivered_digest) is None
        or hashlib.sha256(delivered_json.encode("utf-8")).hexdigest()
        != delivered_digest
        or evidence.get("schema") != content.get("text_suppression_schema")
        or evidence.get("text_suppression_method")
        != content.get("text_suppression_method")
        or evidence.get("raster_content_variant") != variant
        or evidence.get("source_text_item_ids") != source_ids
        or evidence.get("source_text_item_ids_sha256") != source_digest
        or evidence.get("source_text_item_count")
        != content.get("text_suppression_source_item_count")
        or evidence.get("source_text_item_count") != len(source_ids)
        or evidence.get("delivered_source_text_item_ids") != delivered_ids
        or evidence.get("delivered_source_text_item_ids_sha256") != delivered_digest
        or evidence.get("delivery_source_item_ids_bound") is not True
        or evidence.get("verified") is not True
    ):
        raise EvidenceError("page_text_suppression_digest_invalid")


def _canonical_entry_name(value: Any, *, allow_directory: bool) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise EvidenceError("unsafe_archive_entry_name")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise EvidenceError("unsafe_archive_entry_name")
    if "\\" in value or ":" in value or value.startswith("/"):
        raise EvidenceError("unsafe_archive_entry_name")
    is_directory = value.endswith("/")
    if is_directory and not allow_directory:
        raise EvidenceError("unsafe_archive_entry_name")
    candidate = value[:-1] if is_directory else value
    if not candidate or "//" in candidate:
        raise EvidenceError("unsafe_archive_entry_name")
    parts = candidate.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise EvidenceError("unsafe_archive_entry_name")
    normalized = PurePosixPath(candidate).as_posix()
    if normalized != candidate or PurePosixPath(candidate).is_absolute():
        raise EvidenceError("unsafe_archive_entry_name")
    return value


def _zip_info_is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK((int(info.external_attr) >> 16) & 0xFFFF)


def _read_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    maximum_bytes: int,
    capture: bool,
    cancel_check: Optional[Callable[[], Any]] = None,
) -> Tuple[str, int, bytes]:
    declared_size = int(info.file_size)
    if declared_size < 0 or declared_size > maximum_bytes:
        raise EvidenceError("archive_entry_size_invalid")
    digest = hashlib.sha256()
    observed_size = 0
    chunks: List[bytes] = []
    with archive.open(info, "r") as stream:
        while True:
            _checkpoint(cancel_check)
            chunk = stream.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            observed_size += len(chunk)
            if observed_size > maximum_bytes:
                raise EvidenceError("archive_entry_size_invalid")
            digest.update(chunk)
            if capture:
                chunks.append(chunk)
    if observed_size != declared_size:
        raise EvidenceError("archive_entry_size_mismatch")
    return digest.hexdigest(), observed_size, b"".join(chunks) if capture else b""


def _hash_file(
    path: Path,
    *,
    cancel_check: Optional[Callable[[], Any]] = None,
) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_HASH_CHUNK_BYTES), b""):
            _checkpoint(cancel_check)
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _local_name(tag: Any) -> str:
    return str(tag or "").rsplit("}", 1)[-1]


def _document_scalar_property(
    properties: List[Any],
    *,
    name: str,
    property_type: str,
    value_tag: str,
) -> Any:
    matches = [
        node
        for node in properties
        if _local_name(node.tag) == "Property" and node.attrib.get("name") == name
    ]
    if len(matches) != 1 or matches[0].attrib.get("type") != property_type:
        raise EvidenceError("raster_metadata_property_missing_or_invalid")
    values = [child for child in list(matches[0]) if _local_name(child.tag) == value_tag]
    if len(values) != 1 or "value" not in values[0].attrib:
        raise EvidenceError("raster_metadata_property_value_invalid")
    raw = values[0].attrib["value"]
    if property_type == "App::PropertyInteger":
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise EvidenceError("raster_metadata_property_value_invalid") from exc
    if property_type == "App::PropertyBool":
        normalized = str(raw).strip().lower()
        if normalized in {"1", "true"}:
            return True
        if normalized in {"0", "false"}:
            return False
        raise EvidenceError("raster_metadata_property_value_invalid")
    return str(raw)


def _validate_document_raster_metadata(
    properties: List[Any],
    obligation: Dict[str, Any],
) -> None:
    variant = str(obligation.get("raster_content_variant") or "")
    if variant:
        observed_variant = _document_scalar_property(
            properties,
            name="PDFRasterContentVariant",
            property_type="App::PropertyString",
            value_tag="String",
        )
        if observed_variant != variant:
            raise EvidenceError("page_raster_content_variant_mismatch")
    suppression_names = {spec[1] for spec in _SUPPRESSION_PROPERTY_SPECS}
    if obligation.get("text_suppression_present") is True:
        for obligation_key, name, property_type, value_tag in _SUPPRESSION_PROPERTY_SPECS:
            if _document_scalar_property(
                properties,
                name=name,
                property_type=property_type,
                value_tag=value_tag,
            ) != obligation.get(obligation_key):
                raise EvidenceError("page_text_suppression_property_mismatch")
    elif variant == "full_page_original" and any(
        _local_name(node.tag) == "Property"
        and node.attrib.get("name") in suppression_names
        for node in properties
    ):
        raise EvidenceError("original_page_raster_has_suppression_metadata")


def read_archive_evidence(
    fcstd_path: Any,
    inventory: Any,
    *,
    cancel_check: Optional[Callable[[], Any]] = None,
    full_document_object_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Follow Document.xml to every exact PDFRasterFile archive member."""

    try:
        path = Path(fcstd_path)
        obligations = required_included_rasters(inventory)
        inventory_object_count = len(list(inventory.get("objects") or []))
        document_object_count = (
            inventory_object_count
            if full_document_object_count is None
            else full_document_object_count
        )
        if (
            type(document_object_count) is not int
            or document_object_count < inventory_object_count
            or document_object_count < 0
            or document_object_count > 1_000_000
        ):
            raise EvidenceError("full_document_object_count_invalid")
        maximum_archive_entries = max(
            64,
            min(2_000_000, 64 + document_object_count * 32),
        )
        maximum_document_xml_bytes = max(
            1024 * 1024,
            min(
                512 * 1024 * 1024,
                1024 * 1024 + document_object_count * 256 * 1024,
            ),
        )
        if not path.is_file() or path.stat().st_size <= 0:
            raise EvidenceError("fcstd_file_missing_or_empty")
        _checkpoint(cancel_check)
        initial_digest, initial_size = _hash_file(
            path,
            cancel_check=cancel_check,
        )
        if initial_size <= 0:
            raise EvidenceError("fcstd_file_missing_or_empty")

        with zipfile.ZipFile(path, "r") as archive:
            infos = list(archive.infolist())
            if not infos or len(infos) > maximum_archive_entries:
                raise EvidenceError("archive_entry_count_invalid")
            info_by_name: Dict[str, zipfile.ZipInfo] = {}
            canonical_keys = set()
            for info in infos:
                _checkpoint(cancel_check)
                canonical_name = _canonical_entry_name(
                    info.filename, allow_directory=True
                )
                key = canonical_name.casefold()
                if key in canonical_keys:
                    raise EvidenceError("duplicate_archive_entry")
                canonical_keys.add(key)
                info_by_name[canonical_name] = info
                if int(info.flag_bits) & 0x1:
                    raise EvidenceError("encrypted_archive_entry")
                if _zip_info_is_symlink(info):
                    raise EvidenceError("symlink_archive_entry")
                if (
                    not info.is_dir()
                    and info.compress_type not in _SUPPORTED_COMPRESSION
                ):
                    raise EvidenceError("unsupported_archive_compression")

            document_info = info_by_name.get("Document.xml")
            if document_info is None or document_info.is_dir():
                raise EvidenceError("document_xml_missing")
            try:
                document_digest, document_size, document_xml = _read_member(
                    archive,
                    document_info,
                    maximum_bytes=maximum_document_xml_bytes,
                    capture=True,
                    cancel_check=cancel_check,
                )
            except (zipfile.BadZipFile, zlib.error) as exc:
                raise EvidenceError("document_xml_crc_or_read_error") from exc
            if document_size <= 0:
                raise EvidenceError("document_xml_empty")
            if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", document_xml, re.IGNORECASE):
                raise EvidenceError("document_xml_unsafe_declaration")
            try:
                root = ElementTree.fromstring(document_xml)
            except ElementTree.ParseError as exc:
                raise EvidenceError("document_xml_malformed") from exc
            if _local_name(root.tag) != "Document":
                raise EvidenceError("document_xml_root_invalid")
            object_data_nodes = [
                child for child in list(root) if _local_name(child.tag) == "ObjectData"
            ]
            if len(object_data_nodes) != 1:
                raise EvidenceError("document_xml_object_data_invalid")

            object_names = set()
            mapping: Dict[str, str] = {}
            for object_node in list(object_data_nodes[0]):
                if _local_name(object_node.tag) != "Object":
                    continue
                object_name = str(object_node.attrib.get("name") or "")
                if not object_name:
                    raise EvidenceError("document_object_name_missing")
                if object_name in object_names:
                    raise EvidenceError("duplicate_document_object_name")
                object_names.add(object_name)
                if object_name not in obligations:
                    continue
                properties_nodes = [
                    child
                    for child in list(object_node)
                    if _local_name(child.tag) == "Properties"
                ]
                properties = [
                    child
                    for properties_node in properties_nodes
                    for child in list(properties_node)
                    if _local_name(child.tag) == "Property"
                ]
                _validate_document_raster_metadata(
                    properties,
                    obligations[object_name],
                )
                raster_properties = [
                    child for child in properties if child.attrib.get("name") == "PDFRasterFile"
                ]
                if not raster_properties:
                    continue
                if len(raster_properties) > 1:
                    raise EvidenceError("duplicate_raster_property")
                raster_property = raster_properties[0]
                if raster_property.attrib.get("type") != "App::PropertyFileIncluded":
                    raise EvidenceError("raster_property_type_invalid")
                file_nodes = [
                    child
                    for child in list(raster_property)
                    if _local_name(child.tag) == "FileIncluded"
                ]
                if len(file_nodes) != 1:
                    raise EvidenceError("pdf_raster_file_mapping_invalid")
                mapping[object_name] = _canonical_entry_name(
                    file_nodes[0].attrib.get("file"), allow_directory=False
                )

            if len(object_names) != document_object_count:
                raise EvidenceError("document_object_count_mismatch")
            if set(mapping) != set(obligations):
                raise EvidenceError("expected_raster_unmapped")
            raster_entries: List[Dict[str, Any]] = []
            total_bytes = 0
            for entity_id in sorted(obligations):
                _checkpoint(cancel_check)
                obligation = obligations[entity_id]
                entry_name = mapping[entity_id]
                info = info_by_name.get(entry_name)
                if info is None:
                    raise EvidenceError("raster_entry_missing")
                expected_bytes = obligation["bytes"]
                if info.is_dir() or int(info.file_size) <= 0:
                    raise EvidenceError("raster_entry_empty")
                if int(info.file_size) != expected_bytes:
                    raise EvidenceError("raster_entry_size_mismatch")
                try:
                    entry_digest, entry_size, _unused = _read_member(
                        archive,
                        info,
                        maximum_bytes=expected_bytes,
                        capture=False,
                        cancel_check=cancel_check,
                    )
                except (zipfile.BadZipFile, zlib.error) as exc:
                    raise EvidenceError("raster_entry_crc_or_read_error") from exc
                if (
                    entry_digest != obligation["sha256"]
                    or entry_size != obligation["bytes"]
                ):
                    raise EvidenceError("raster_entry_digest_mismatch")
                total_bytes += entry_size
                raster_entries.append(
                    {
                        **copy.deepcopy(obligation),
                        "property_name": "PDFRasterFile",
                        "property_type": "App::PropertyFileIncluded",
                        "entry_name": entry_name,
                        "sha256": entry_digest,
                        "bytes": entry_size,
                        "compression_method": int(info.compress_type),
                        "crc32": int(info.CRC),
                    }
                )

        fcstd_digest, fcstd_size = _hash_file(
            path,
            cancel_check=cancel_check,
        )
        if fcstd_digest != initial_digest or fcstd_size != initial_size:
            raise EvidenceError("fcstd_archive_changed_during_evidence")
        evidence: Dict[str, Any] = {
            "schema": SCHEMA,
            "method": METHOD,
            "verified": True,
            "fcstd_sha256": fcstd_digest,
            "fcstd_bytes": fcstd_size,
            "document_xml_sha256": document_digest,
            "document_xml_bytes": document_size,
            "archive_entry_count": len(infos),
            "document_object_count": len(object_names),
            "full_document_object_count": document_object_count,
            "expected_raster_count": len(obligations),
            "total_expected_raster_bytes": total_bytes,
            "raster_entries": raster_entries,
        }
        evidence["evidence_digest"] = evidence_digest(evidence)
        return evidence
    except _CancellationPassthrough as exc:
        raise exc.original
    except EvidenceError as exc:
        return failure(exc.reason)
    except zipfile.BadZipFile:
        return failure("fcstd_archive_invalid")
    except (
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        NotImplementedError,
        zlib.error,
    ):
        return failure("fcstd_archive_read_error")


def bind_archive_evidence(inventory: Any, evidence: Any) -> bool:
    """Bind an exact archive manifest into every raster inventory record."""

    if (
        not isinstance(inventory, dict)
        or not isinstance(evidence, dict)
        or evidence.get("verified") is not True
        or evidence.get("schema") != SCHEMA
        or evidence.get("method") != METHOD
        or evidence.get("evidence_digest") != evidence_digest(evidence)
    ):
        return False
    try:
        obligations = required_included_rasters(inventory)
    except EvidenceError:
        return False
    entries = evidence.get("raster_entries")
    if not isinstance(entries, list):
        return False
    entry_by_id = {
        str(entry.get("entity_id") or ""): entry
        for entry in entries
        if isinstance(entry, dict)
    }
    if (
        len(entry_by_id) != len(entries)
        or set(entry_by_id) != set(obligations)
        or evidence.get("expected_raster_count") != len(obligations)
    ):
        return False
    compared_keys = (
        "entity_id",
        "sha256",
        "bytes",
        "pdf_source_sha256",
        "page_number",
        "source_xref",
        "occurrence_index",
        "occurrence_evidence_sha256",
        "occurrence_json",
        "source_has_mask",
        "source_item_id",
        "representation",
        "raster_content_variant",
        "text_suppression_present",
        *_SUPPRESSION_CONTENT_KEYS,
        "x_size",
        "y_size",
        "anchor_xyz",
    )
    for entity_id, obligation in obligations.items():
        entry = entry_by_id[entity_id]
        if any(entry.get(key) != obligation.get(key) for key in compared_keys):
            return False
        if (
            entry.get("property_name") != "PDFRasterFile"
            or entry.get("property_type") != "App::PropertyFileIncluded"
            or not str(entry.get("entry_name") or "")
        ):
            return False
    record_by_id = {
        str(record.get("entity_id") or ""): record
        for record in list(inventory.get("objects") or [])
        if isinstance(record, dict)
    }
    if any(entity_id not in record_by_id for entity_id in obligations):
        return False
    inventory["raster_archive_evidence"] = copy.deepcopy(evidence)
    for entity_id in obligations:
        content = record_by_id[entity_id].get("content")
        entry = entry_by_id[entity_id]
        if not isinstance(content, dict):
            return False
        content.update(
            {
                "raster_persistence_method": METHOD,
                "raster_archive_schema": SCHEMA,
                "raster_archive_evidence_sha256": evidence["evidence_digest"],
                "raster_archive_member": entry["entry_name"],
                "raster_archive_member_sha256": entry["sha256"],
                "raster_archive_member_bytes": entry["bytes"],
                "raster_archive_binding_verified": True,
            }
        )
    inventory["inventory_digest"] = inventory_digest(inventory)
    return bool(inventory["inventory_digest"])
