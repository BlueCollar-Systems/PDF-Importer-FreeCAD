"""Exact FCStd persistence contracts for page, item, and embedded rasters."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import subprocess
import sys
import textwrap
import warnings
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
import PDFRasterPersistence as persistence  # noqa: E402


FREECAD_CMD = Path(r"C:\Program Files\FreeCAD 1.1\bin\freecadcmd.exe")
PDF_SOURCE_SHA256 = "a" * 64
GOOD_RASTER = b"exact-raster-payload"
DECOY_RASTER = b"ImageFile-decoy-must-not-be-selected"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _occurrence_json(
    *,
    occurrence_index: int = 3,
    source_xref: int = 41,
    transform: tuple[float, ...] = (0.0, 1.0, -1.0, 0.0, 144.0, 36.0),
) -> str:
    return json.dumps(
        {
            "source_bbox": [36.0, 18.0, 144.0, 90.0],
            "source_clip": [36.0, 18.0, 144.0, 90.0],
            "source_has_mask": True,
            "source_occurrence_index": occurrence_index,
            "source_transform": list(transform),
            "source_xref": source_xref,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _raster_inventory(
    payload: bytes = GOOD_RASTER,
    *,
    entity_id: str = "Raster001",
    source_item_id: str = "p2:image:3",
    page_number: int = 2,
    source_xref: int = 41,
    occurrence_index: int = 3,
    occurrence_json: str | None = None,
    source_has_mask: bool = True,
) -> dict:
    raster_sha256 = _sha256(payload)
    encoded = occurrence_json or _occurrence_json(
        occurrence_index=occurrence_index,
        source_xref=source_xref,
    )
    occurrence_sha256 = _sha256(encoded.encode("utf-8"))
    return {
        "schema": "bcs.freecad_host_object_inventory/1.1",
        "shape_evidence_mode": "cheap",
        "verified": True,
        "entity_ids": [entity_id],
        "type_counts": {"Image::ImagePlane": 1},
        "counts": {
            "total": 1,
            "containers": 0,
            "images": 1,
            "vector_primitives": 0,
            "text_representation_objects": 0,
            "unclassified": 0,
        },
        "categories": {
            "containers": [],
            "images": [entity_id],
            "vector_primitives": [],
            "text_representation_objects": [],
            "unclassified": [],
        },
        "objects": [
            {
                "entity_id": entity_id,
                "type_id": "Image::ImagePlane",
                "representation": "raster",
                "source_item_id": source_item_id,
                "parent_source_item_id": "",
                "category": "images",
                "content": {
                    "pdf_source_sha256": PDF_SOURCE_SHA256,
                    "declared_raster_sha256": raster_sha256,
                    "image_sha256": raster_sha256,
                    "image_bytes": len(payload),
                    "included_image_sha256": raster_sha256,
                    "included_image_bytes": len(payload),
                    "raster_asset_binding_verified": True,
                    "embedded_image_page_number": page_number,
                    "embedded_image_source_xref": source_xref,
                    "image_occurrence_index": occurrence_index,
                    "image_occurrence_evidence_sha256": occurrence_sha256,
                    "image_occurrence_json": encoded,
                    "image_source_has_mask": source_has_mask,
                    "x_size": "108",
                    "y_size": "72",
                    "anchor_xyz": [36.0, 18.0, 0.0],
                },
            }
        ],
    }


def _document_xml(entity_id: str, property_fragments: list[str]) -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<Document><ObjectData>"
        '<Object name="%s"><Properties>%s</Properties></Object>'
        "</ObjectData></Document>"
        % (entity_id, "".join(property_fragments))
    ).encode("utf-8")


def _included_property(
    member: str,
    *,
    name: str = "PDFRasterFile",
    property_type: str = "App::PropertyFileIncluded",
) -> str:
    return (
        '<Property name="%s" type="%s">'
        '<FileIncluded file="%s"/></Property>'
        % (name, property_type, member)
    )


def _scalar_property(name: str, property_type: str, tag: str, value) -> str:
    if property_type == "App::PropertyBool":
        encoded = "true" if value is True else "false"
    else:
        encoded = str(value)
    return (
        '<Property name="%s" type="%s"><%s value="%s"/></Property>'
        % (name, property_type, tag, html.escape(encoded, quote=True))
    )


def _suppressed_page_inventory(payload: bytes = GOOD_RASTER) -> dict:
    inventory = _raster_inventory(
        payload,
        entity_id="PageRaster",
        source_item_id="p1:page",
        page_number=1,
        source_xref=0,
        occurrence_index=0,
        source_has_mask=False,
    )
    source_ids = ["p1:b0:l0:s0"]
    source_json = json.dumps(source_ids, sort_keys=True, separators=(",", ":"))
    source_digest = _sha256(source_json.encode("utf-8"))
    evidence = {
        "schema": "bcs.freecad_page_text_suppression/1.0",
        "verified": True,
        "page_number": 1,
        "text_suppression_method": (
            "exact_source_original_with_all_text_delta_transparent"
        ),
        "raster_content_variant": "text_suppressed_page_background",
        "source_text_item_ids": source_ids,
        "source_text_item_ids_sha256": source_digest,
        "source_text_item_count": 1,
        "delivered_source_text_item_ids": source_ids,
        "delivered_source_text_item_ids_sha256": source_digest,
        "delivery_source_item_ids_bound": True,
    }
    evidence_digest = _sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    evidence["evidence_sha256"] = evidence_digest
    evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    inventory["objects"][0]["content"].update(
        {
            "raster_content_variant": "text_suppressed_page_background",
            "raster_content_variant_property_verified": True,
            "text_suppression_schema": evidence["schema"],
            "text_suppression_method": evidence["text_suppression_method"],
            "text_suppression_evidence_json": evidence_json,
            "text_suppression_evidence_sha256": evidence_digest,
            "text_suppression_source_item_ids_json": source_json,
            "text_suppression_source_item_ids_sha256": source_digest,
            "text_suppression_source_item_count": 1,
            "text_suppression_delivered_item_ids_json": source_json,
            "text_suppression_delivered_item_ids_sha256": source_digest,
            "text_suppression_delivery_bound": True,
            "text_suppression_verified": True,
            "page_text_suppression_binding_verified": True,
        }
    )
    return inventory


def _page_variant_properties(inventory: dict) -> list[str]:
    content = inventory["objects"][0]["content"]
    properties = [
        _scalar_property(
            "PDFRasterContentVariant",
            "App::PropertyString",
            "String",
            content["raster_content_variant"],
        )
    ]
    mapping = (
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
    for key, name, property_type, tag in mapping:
        if key in content:
            properties.append(_scalar_property(name, property_type, tag, content[key]))
    return properties


def _write_fcstd(
    path: Path,
    document_xml: bytes,
    entries: list[tuple[str, bytes]],
) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Document.xml", document_xml)
            for name, payload in entries:
                archive.writestr(name, payload)


def test_inventory_does_not_alias_included_raster_into_missing_image_file(
    tmp_path: Path,
) -> None:
    included = tmp_path / "included.png"
    included.write_bytes(GOOD_RASTER)
    obj = SimpleNamespace(
        Name="Raster001",
        TypeId="Image::ImagePlane",
        PDFRepresentation="raster",
        PDFSourceItemId="p1:page",
        PDFParentSourceItemId="",
        PDFRasterFile=str(included),
        PDFRasterSHA256=_sha256(GOOD_RASTER),
        PDFSourceSHA256=PDF_SOURCE_SHA256,
    )

    content = core._host_object_content_snapshot(
        obj,
        "Image::ImagePlane",
        "raster",
        shape_evidence_mode="cheap",
    )

    assert content["image_file"] == ""
    assert content["image_sha256"] == ""
    assert content["image_bytes"] == 0
    assert content["included_image_sha256"] == _sha256(GOOD_RASTER)
    assert content["included_image_bytes"] == len(GOOD_RASTER)
    assert content["raster_asset_binding_verified"] is False


def test_fcstd_raster_reader_selects_exact_pdfrasterfile_mapping_not_imagefile(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "exact-raster.FCStd"
    exact_member = "Raster001.PDFRasterFile.png"
    decoy_member = "Raster001.ImageFile.png"
    xml = _document_xml(
        "Raster001",
        [
            _included_property(decoy_member, name="ImageFile"),
            _included_property(exact_member),
        ],
    )
    _write_fcstd(
        archive_path,
        xml,
        [(decoy_member, DECOY_RASTER), (exact_member, GOOD_RASTER)],
    )
    inventory = _raster_inventory()

    evidence = core._read_fcstd_raster_archive_evidence(archive_path, inventory)

    assert evidence["schema"] == "bcs.freecad_fcstd_raster_archive/1.0"
    assert evidence["method"] == "fcstd_property_file_included_sha256"
    assert evidence["verified"] is True
    assert evidence["expected_raster_count"] == 1
    assert evidence["total_expected_raster_bytes"] == len(GOOD_RASTER)
    assert evidence["evidence_digest"] == core._fcstd_archive_evidence_digest(
        evidence
    )
    assert evidence["raster_entries"] == [
        {
            "entity_id": "Raster001",
            "property_name": "PDFRasterFile",
            "property_type": "App::PropertyFileIncluded",
            "entry_name": exact_member,
            "sha256": _sha256(GOOD_RASTER),
            "bytes": len(GOOD_RASTER),
            "compression_method": zipfile.ZIP_DEFLATED,
            "crc32": zipfile.crc32(GOOD_RASTER),
            "pdf_source_sha256": PDF_SOURCE_SHA256,
            "page_number": 2,
            "source_xref": 41,
            "occurrence_index": 3,
            "occurrence_evidence_sha256": _sha256(
                _occurrence_json().encode("utf-8")
            ),
            "occurrence_json": _occurrence_json(),
            "source_has_mask": True,
            "source_item_id": "p2:image:3",
            "representation": "raster",
            "x_size": "108",
            "y_size": "72",
            "anchor_xyz": [36.0, 18.0, 0.0],
        }
    ]
    assert _sha256(DECOY_RASTER) not in json.dumps(evidence, sort_keys=True)

    bound_inventory = copy.deepcopy(inventory)
    assert core._bind_fcstd_raster_archive_evidence(bound_inventory, evidence) is True
    assert bound_inventory["raster_archive_evidence"] == evidence
    assert bound_inventory["inventory_digest"] == core._host_inventory_digest(
        bound_inventory
    )


@pytest.mark.parametrize(
    ("properties", "entries", "reason"),
    [
        (
            [_included_property("decoy.png", name="ImageFile")],
            [("decoy.png", DECOY_RASTER)],
            "expected_raster_unmapped",
        ),
        (
            [
                _included_property(
                    "exact.png",
                    property_type="App::PropertyString",
                )
            ],
            [("exact.png", GOOD_RASTER)],
            "raster_property_type_invalid",
        ),
        (
            [
                _included_property("first.png"),
                _included_property("second.png"),
            ],
            [("first.png", GOOD_RASTER), ("second.png", GOOD_RASTER)],
            "duplicate_raster_property",
        ),
        (
            [_included_property("missing.png")],
            [],
            "raster_entry_missing",
        ),
        (
            [_included_property("empty.png")],
            [("empty.png", b"")],
            "raster_entry_empty",
        ),
        (
            [_included_property("wrong.png")],
            [("wrong.png", b"X" * len(GOOD_RASTER))],
            "raster_entry_digest_mismatch",
        ),
    ],
    ids=[
        "missing-PDFRasterFile",
        "wrong-PDFRasterFile-type",
        "duplicate-PDFRasterFile",
        "missing-member",
        "empty-member",
        "wrong-member-digest",
    ],
)
def test_fcstd_raster_reader_rejects_unproven_included_content(
    tmp_path: Path,
    properties: list[str],
    entries: list[tuple[str, bytes]],
    reason: str,
) -> None:
    archive_path = tmp_path / (reason + ".FCStd")
    _write_fcstd(archive_path, _document_xml("Raster001", properties), entries)

    evidence = core._read_fcstd_raster_archive_evidence(
        archive_path,
        _raster_inventory(),
    )

    assert evidence == {
        "schema": "bcs.freecad_fcstd_raster_archive/1.0",
        "method": "fcstd_property_file_included_sha256",
        "verified": False,
        "reason": reason,
    }


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("pdf_source_sha256", "b" * 64),
        ("page_number", 99),
        ("source_xref", 99),
        ("occurrence_index", 99),
        ("occurrence_evidence_sha256", "c" * 64),
        ("occurrence_json", '{"source_transform":[1,0,0,1,0,0]}'),
        ("source_has_mask", False),
        ("source_item_id", "p99:image:99"),
        ("representation", "geometry"),
        ("x_size", "999"),
        ("y_size", "999"),
        ("anchor_xyz", [0.0, 0.0, -0.1]),
    ],
)
def test_fcstd_raster_binding_rejects_archive_metadata_detached_from_inventory(
    tmp_path: Path,
    field: str,
    replacement,
) -> None:
    archive_path = tmp_path / "metadata-binding.FCStd"
    member = "exact.png"
    _write_fcstd(
        archive_path,
        _document_xml("Raster001", [_included_property(member)]),
        [(member, GOOD_RASTER)],
    )
    inventory = _raster_inventory()
    evidence = core._read_fcstd_raster_archive_evidence(archive_path, inventory)
    assert evidence["verified"] is True
    tampered = copy.deepcopy(evidence)
    tampered["raster_entries"][0][field] = replacement
    tampered["evidence_digest"] = core._fcstd_archive_evidence_digest(tampered)

    detached_inventory = copy.deepcopy(inventory)
    assert (
        core._bind_fcstd_raster_archive_evidence(detached_inventory, tampered)
        is False
    )
    assert "raster_archive_evidence" not in detached_inventory


@pytest.mark.parametrize(
    ("phase", "expected_reason"),
    [
        ("reader", "injected_raster_archive_failure"),
        ("binder", "fcstd_raster_archive_binding_failed"),
    ],
)
def test_production_save_reopen_cannot_bypass_raster_archive_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    expected_reason: str,
) -> None:
    calls = {"reader": 0, "binder": 0, "open": 0}

    class Document:
        Name = ""

        @staticmethod
        def saveCopy(path):  # noqa: N802 - FreeCAD API
            Path(path).write_bytes(b"synthetic-fcstd")
            return True

    def read_raster(path, inventory, **_kwargs):
        assert Path(path).is_file()
        assert inventory is expected_inventory
        calls["reader"] += 1
        if phase == "reader":
            return {
                "schema": "bcs.freecad_fcstd_raster_archive/1.0",
                "method": "fcstd_property_file_included_sha256",
                "verified": False,
                "reason": "injected_raster_archive_failure",
            }
        return {
            "schema": "bcs.freecad_fcstd_raster_archive/1.0",
            "method": "fcstd_property_file_included_sha256",
            "verified": True,
            "evidence_digest": "d" * 64,
        }

    def bind_raster(inventory, evidence):
        assert inventory is expected_inventory
        assert evidence["verified"] is True
        calls["binder"] += 1
        return False

    def forbidden_open(*_args, **_kwargs):
        calls["open"] += 1
        raise AssertionError("reopen must not precede exact raster archive binding")

    expected_inventory = _raster_inventory()
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(openDocument=forbidden_open))
    monkeypatch.setattr(
        core,
        "_read_fcstd_shape_archive_evidence",
        lambda *_args, **_kwargs: {"verified": True, "evidence_digest": "s" * 64},
    )
    monkeypatch.setattr(core, "_bind_fcstd_shape_archive_evidence", lambda *_: True)
    monkeypatch.setattr(core, "_read_fcstd_raster_archive_evidence", read_raster)
    monkeypatch.setattr(core, "_bind_fcstd_raster_archive_evidence", bind_raster)

    result = core._save_reopen_host_object_inventory(Document(), expected_inventory)

    assert result["verified"] is False
    assert result["reason"] == expected_reason
    assert calls["reader"] == 1
    assert calls["binder"] == (1 if phase == "binder" else 0)
    assert calls["open"] == 0


def test_public_live_gate_requires_bound_raster_archive_digest() -> None:
    authority = object()
    opts = core.ImportOptions()
    opts._pdf_sha256 = PDF_SOURCE_SHA256
    opts._page_visual_authority = authority
    report = SimpleNamespace(
        input={"sha256": PDF_SOURCE_SHA256},
        extra={"import_contract_ready": {"ready": True}},
        _page_visual_authority=authority,
    )
    opts._live_import_report = report
    opts._report_extra = {
        "actual_host_object_inventory": _raster_inventory(),
        "save_reopen_inventory": {"verified": True},
    }

    with pytest.raises(core.ImportLifecycleError, match="raster FCStd persistence"):
        core._require_live_import_contract_ready(opts)


def test_raster_declared_size_mismatch_never_opens_raster_member(
    tmp_path: Path,
    monkeypatch,
) -> None:
    inventory = _raster_inventory(GOOD_RASTER)
    member = "Raster001.png"
    archive_path = tmp_path / "size-mismatch.FCStd"
    _write_fcstd(
        archive_path,
        _document_xml("Raster001", [_included_property(member)]),
        [(member, b"short")],
    )
    original_read_member = persistence._read_member
    raster_reads: list[str] = []

    def track_read(archive, info, **kwargs):
        if info.filename == member:
            raster_reads.append(info.filename)
        return original_read_member(archive, info, **kwargs)

    monkeypatch.setattr(persistence, "_read_member", track_read)
    evidence = persistence.read_archive_evidence(archive_path, inventory)

    assert evidence["verified"] is False
    assert evidence["reason"] == "raster_entry_size_mismatch"
    assert raster_reads == []


def test_raster_archive_cancellation_propagates_typed_signal(tmp_path: Path) -> None:
    inventory = _raster_inventory()
    member = "Raster001.png"
    archive_path = tmp_path / "cancel.FCStd"
    _write_fcstd(
        archive_path,
        _document_xml("Raster001", [_included_property(member)]),
        [(member, GOOD_RASTER)],
    )

    with pytest.raises(core.ImportCancelled, match="stop archive proof"):
        persistence.read_archive_evidence(
            archive_path,
            inventory,
            cancel_check=lambda: (_ for _ in ()).throw(
                core.ImportCancelled("stop archive proof")
            ),
        )


def test_production_raster_archive_wrapper_supplies_live_checkpoint_and_count(
    monkeypatch,
) -> None:
    observed: dict = {}

    class Module:
        @staticmethod
        def read_archive_evidence(path, inventory, **kwargs):
            observed.update(kwargs)
            kwargs["cancel_check"]()
            return {"verified": True}

    checkpoints: list[str] = []
    monkeypatch.setattr(core, "_raster_persistence_module", lambda: Module())
    monkeypatch.setattr(
        core,
        "_invoke_import_cancellation_checkpoint",
        lambda _opts, stage: checkpoints.append(stage),
    )
    opts = core.ImportOptions()

    assert core._read_fcstd_raster_archive_evidence(
        "fixture.FCStd",
        {},
        opts=opts,
        full_document_object_count=123,
    ) == {"verified": True}
    assert checkpoints == ["persistence raster archive evidence"]
    assert observed["full_document_object_count"] == 123


def test_full_document_count_prevents_large_baseline_xml_roadblock(
    tmp_path: Path,
) -> None:
    inventory = _raster_inventory()
    member = "Raster001.png"
    filler = "x" * 300_000
    baseline = "".join(
        '<Object name="Baseline%d"><Properties>%s</Properties></Object>'
        % (
            index,
            _scalar_property(
                "Payload", "App::PropertyString", "String", filler
            ),
        )
        for index in range(5)
    )
    raster = (
        '<Object name="Raster001"><Properties>%s</Properties></Object>'
        % _included_property(member)
    )
    document_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        "<Document><ObjectData>%s%s</ObjectData></Document>" % (baseline, raster)
    ).encode("utf-8")
    archive_path = tmp_path / "large-existing-document.FCStd"
    _write_fcstd(archive_path, document_xml, [(member, GOOD_RASTER)])

    without_full_count = persistence.read_archive_evidence(archive_path, inventory)
    with_full_count = persistence.read_archive_evidence(
        archive_path,
        inventory,
        full_document_object_count=6,
    )

    assert without_full_count["verified"] is False
    assert with_full_count["verified"] is True
    assert with_full_count["document_object_count"] == 6
    assert with_full_count["full_document_object_count"] == 6


def test_fcstd_archive_binds_suppression_properties_and_rejects_stripping(
    tmp_path: Path,
) -> None:
    inventory = _suppressed_page_inventory()
    member = "PageRaster.png"
    properties = [_included_property(member), *_page_variant_properties(inventory)]
    archive_path = tmp_path / "suppressed-page.FCStd"
    _write_fcstd(
        archive_path,
        _document_xml("PageRaster", properties),
        [(member, GOOD_RASTER)],
    )

    evidence = persistence.read_archive_evidence(archive_path, inventory)
    assert evidence["verified"] is True
    entry = evidence["raster_entries"][0]
    assert entry["raster_content_variant"] == "text_suppressed_page_background"
    assert entry["text_suppression_present"] is True
    assert entry["text_suppression_delivery_bound"] is True

    stripped_path = tmp_path / "suppressed-page-stripped.FCStd"
    _write_fcstd(
        stripped_path,
        _document_xml("PageRaster", properties[:-1]),
        [(member, GOOD_RASTER)],
    )
    stripped = persistence.read_archive_evidence(stripped_path, inventory)
    assert stripped["verified"] is False
    assert stripped["reason"] == "raster_metadata_property_missing_or_invalid"


def test_original_page_variant_is_persisted_without_suppression_masquerade(
    tmp_path: Path,
) -> None:
    inventory = _raster_inventory(
        entity_id="PageRaster",
        source_item_id="p1:page",
        page_number=1,
        source_xref=0,
        occurrence_index=0,
        source_has_mask=False,
    )
    content = inventory["objects"][0]["content"]
    content["raster_content_variant"] = "full_page_original"
    content["raster_content_variant_property_verified"] = True
    member = "PageRaster.png"
    properties = [
        _included_property(member),
        *_page_variant_properties(inventory),
    ]
    archive_path = tmp_path / "original-page.FCStd"
    _write_fcstd(
        archive_path,
        _document_xml("PageRaster", properties),
        [(member, GOOD_RASTER)],
    )

    evidence = persistence.read_archive_evidence(archive_path, inventory)
    assert evidence["verified"] is True
    assert evidence["raster_entries"][0]["raster_content_variant"] == "full_page_original"
    assert evidence["raster_entries"][0]["text_suppression_present"] is False


def _make_png(path: Path, rgba: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGBA", (3, 2), rgba)
    image.putpixel((1, 0), tuple(255 - value for value in rgba[:3]) + (rgba[3],))
    image.save(path, format="PNG", optimize=False)
    return path.read_bytes()


@pytest.mark.skipif(not FREECAD_CMD.is_file(), reason="FreeCADCmd 1.1 is unavailable")
def test_freecadcmd_fcstd_keeps_exact_page_item_and_embedded_rasters_without_cache(
    tmp_path: Path,
) -> None:
    specs = [
        {
            "name": "PageRaster",
            "source_item_id": "p1:page",
            "page_number": 1,
            "source_xref": 0,
            "occurrence_index": 0,
            "source_has_mask": False,
            "rgba": (225, 40, 30, 255),
            "transform": [1.0, 0.0, 0.0, 1.0, 0.0, 0.0],
            "x_size": 612.0,
            "y_size": 792.0,
            "anchor": [0.0, 0.0, 0.0],
        },
        {
            "name": "ItemRaster",
            "source_item_id": "p1:text:7:raster",
            "page_number": 1,
            "source_xref": 0,
            "occurrence_index": 7,
            "source_has_mask": False,
            "rgba": (35, 185, 75, 196),
            "transform": [0.0, 1.0, -1.0, 0.0, 144.0, 36.0],
            "x_size": 108.0,
            "y_size": 36.0,
            "anchor": [36.0, 18.0, 0.0],
        },
        {
            "name": "EmbeddedRaster",
            "source_item_id": "p2:image:3",
            "page_number": 2,
            "source_xref": 41,
            "occurrence_index": 3,
            "source_has_mask": True,
            "rgba": (45, 80, 230, 128),
            "transform": [-1.0, 0.25, 0.5, 1.0, 300.0, 120.0],
            "x_size": 90.0,
            "y_size": 72.0,
            "anchor": [144.0, 36.0, 0.0],
        },
    ]
    payloads: dict[str, bytes] = {}
    for spec in specs:
        asset_path = tmp_path / (spec["name"] + ".png")
        payload = _make_png(asset_path, spec.pop("rgba"))
        spec["asset_path"] = str(asset_path)
        spec["raster_sha256"] = _sha256(payload)
        occurrence = json.dumps(
            {
                "source_has_mask": spec["source_has_mask"],
                "source_occurrence_index": spec["occurrence_index"],
                "source_transform": spec["transform"],
                "source_xref": spec["source_xref"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        spec["occurrence_json"] = occurrence
        spec["occurrence_sha256"] = _sha256(occurrence.encode("utf-8"))
        if spec["name"] == "PageRaster":
            suppression_content = _suppressed_page_inventory(payload)["objects"][0][
                "content"
            ]
            spec["raster_content_variant"] = suppression_content[
                "raster_content_variant"
            ]
            spec["suppression_content"] = {
                key: suppression_content[key]
                for key in persistence._SUPPRESSION_CONTENT_KEYS
            }
        payloads[spec["name"]] = payload

    fcstd_path = tmp_path / "raster-self-contained.FCStd"
    result_path = tmp_path / "freecad-result.json"
    script_path = tmp_path / "verify_raster_self_containment.py"
    script_path.write_text(
        textwrap.dedent(
            f"""
            import hashlib
            import json
            import os

            import FreeCAD as App
            try:
                import Image  # noqa: F401 - registers Image::ImagePlane
            except Exception:
                Image = None

            specs = json.loads({json.dumps(json.dumps(specs))})
            fcstd_path = {json.dumps(str(fcstd_path))}
            result_path = {json.dumps(str(result_path))}
            pdf_source_sha256 = {json.dumps(PDF_SOURCE_SHA256)}
            suppression_specs = (
                ("App::PropertyString", "PDFTextSuppressionSchema", "text_suppression_schema"),
                ("App::PropertyString", "PDFTextSuppressionMethod", "text_suppression_method"),
                ("App::PropertyString", "PDFTextSuppressionEvidenceJSON", "text_suppression_evidence_json"),
                ("App::PropertyString", "PDFTextSuppressionEvidenceSHA256", "text_suppression_evidence_sha256"),
                ("App::PropertyString", "PDFTextSuppressionSourceItemIDsJSON", "text_suppression_source_item_ids_json"),
                ("App::PropertyString", "PDFTextSuppressionSourceItemIDsSHA256", "text_suppression_source_item_ids_sha256"),
                ("App::PropertyInteger", "PDFTextSuppressionSourceItemCount", "text_suppression_source_item_count"),
                ("App::PropertyString", "PDFTextSuppressionDeliveredItemIDsJSON", "text_suppression_delivered_item_ids_json"),
                ("App::PropertyString", "PDFTextSuppressionDeliveredItemIDsSHA256", "text_suppression_delivered_item_ids_sha256"),
                ("App::PropertyBool", "PDFTextSuppressionDeliveryBound", "text_suppression_delivery_bound"),
                ("App::PropertyBool", "PDFTextSuppressionVerified", "text_suppression_verified"),
            )

            def add_property(obj, kind, name):
                if name not in obj.PropertiesList:
                    obj.addProperty(kind, name, "PDF Import")

            doc = App.newDocument("RasterPersistence")
            created_types = {{}}
            for spec in specs:
                try:
                    obj = doc.addObject("Image::ImagePlane", spec["name"])
                except Exception:
                    obj = doc.addObject("App::FeaturePython", spec["name"])
                    add_property(obj, "App::PropertyFileIncluded", "ImageFile")
                    add_property(obj, "App::PropertyLength", "XSize")
                    add_property(obj, "App::PropertyLength", "YSize")
                created_types[spec["name"]] = obj.TypeId
                obj.ImageFile = spec["asset_path"]
                obj.XSize = spec["x_size"]
                obj.YSize = spec["y_size"]
                obj.Placement = App.Placement(
                    App.Vector(*spec["anchor"]), App.Rotation()
                )
                for kind, name in (
                    ("App::PropertyFileIncluded", "PDFRasterFile"),
                    ("App::PropertyString", "PDFSourceSHA256"),
                    ("App::PropertyString", "PDFRasterSHA256"),
                    ("App::PropertyInteger", "PDFImagePageNumber"),
                    ("App::PropertyInteger", "PDFImageSourceXRef"),
                    ("App::PropertyInteger", "PDFImageOccurrenceIndex"),
                    ("App::PropertyString", "PDFImageOccurrenceJSON"),
                    ("App::PropertyString", "PDFImageOccurrenceEvidenceSHA256"),
                    ("App::PropertyBool", "PDFImageSourceHasMask"),
                    ("App::PropertyString", "PDFSourceItemId"),
                    ("App::PropertyString", "PDFRepresentation"),
                ):
                    add_property(obj, kind, name)
                if spec.get("raster_content_variant"):
                    add_property(obj, "App::PropertyString", "PDFRasterContentVariant")
                    obj.PDFRasterContentVariant = spec["raster_content_variant"]
                for kind, name, key in suppression_specs:
                    if key in spec.get("suppression_content", {{}}):
                        add_property(obj, kind, name)
                        setattr(obj, name, spec["suppression_content"][key])
                obj.PDFRasterFile = spec["asset_path"]
                obj.PDFSourceSHA256 = pdf_source_sha256
                obj.PDFRasterSHA256 = spec["raster_sha256"]
                obj.PDFImagePageNumber = spec["page_number"]
                obj.PDFImageSourceXRef = spec["source_xref"]
                obj.PDFImageOccurrenceIndex = spec["occurrence_index"]
                obj.PDFImageOccurrenceJSON = spec["occurrence_json"]
                obj.PDFImageOccurrenceEvidenceSHA256 = spec["occurrence_sha256"]
                obj.PDFImageSourceHasMask = spec["source_has_mask"]
                obj.PDFSourceItemId = spec["source_item_id"]
                obj.PDFRepresentation = "raster"

            doc.recompute()
            doc.saveAs(fcstd_path)
            App.closeDocument(doc.Name)
            for spec in specs:
                os.remove(spec["asset_path"])
            assert all(not os.path.exists(spec["asset_path"]) for spec in specs)

            reopened = App.openDocument(fcstd_path)
            observed = {{}}
            for spec in specs:
                obj = reopened.getObject(spec["name"])
                included_path = str(obj.PDFRasterFile)
                with open(included_path, "rb") as stream:
                    included = stream.read()
                observed[spec["name"]] = {{
                    "type_id": obj.TypeId,
                    "included_exists": os.path.isfile(included_path),
                    "included_path_was_external": (
                        os.path.normcase(os.path.abspath(included_path))
                        == os.path.normcase(os.path.abspath(spec["asset_path"]))
                    ),
                    "raster_sha256": hashlib.sha256(included).hexdigest(),
                    "raster_bytes": len(included),
                    "pdf_source_sha256": str(obj.PDFSourceSHA256),
                    "declared_raster_sha256": str(obj.PDFRasterSHA256),
                    "page_number": int(obj.PDFImagePageNumber),
                    "source_xref": int(obj.PDFImageSourceXRef),
                    "occurrence_index": int(obj.PDFImageOccurrenceIndex),
                    "occurrence_json": str(obj.PDFImageOccurrenceJSON),
                    "occurrence_evidence_sha256": str(
                        obj.PDFImageOccurrenceEvidenceSHA256
                    ),
                    "source_has_mask": bool(obj.PDFImageSourceHasMask),
                    "source_item_id": str(obj.PDFSourceItemId),
                    "representation": str(obj.PDFRepresentation),
                    "x_size": float(obj.XSize),
                    "y_size": float(obj.YSize),
                    "anchor_xyz": [
                        float(obj.Placement.Base.x),
                        float(obj.Placement.Base.y),
                        float(obj.Placement.Base.z),
                    ],
                    "raster_content_variant": (
                        str(obj.PDFRasterContentVariant)
                        if "PDFRasterContentVariant" in obj.PropertiesList
                        else ""
                    ),
                    "suppression_content": {{
                        key: getattr(obj, name)
                        for _kind, name, key in suppression_specs
                        if name in obj.PropertiesList
                    }},
                }}
            with open(result_path, "w", encoding="utf-8") as result_stream:
                json.dump(
                    {{"created_types": created_types, "observed": observed}},
                    result_stream,
                    sort_keys=True,
                )
            App.closeDocument(reopened.Name)
            """
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(FREECAD_CMD), str(script_path)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert result_path.is_file(), completed.stdout + "\n" + completed.stderr
    assert fcstd_path.is_file()
    assert all(not Path(spec["asset_path"]).exists() for spec in specs)
    result = json.loads(result_path.read_text(encoding="utf-8"))

    for spec in specs:
        observed = result["observed"][spec["name"]]
        assert observed["type_id"] in {"Image::ImagePlane", "App::FeaturePython"}
        assert observed["included_exists"] is True
        assert observed["included_path_was_external"] is False
        assert observed["raster_sha256"] == spec["raster_sha256"]
        assert observed["raster_bytes"] == len(payloads[spec["name"]])
        assert observed["pdf_source_sha256"] == PDF_SOURCE_SHA256
        assert observed["declared_raster_sha256"] == spec["raster_sha256"]
        assert observed["page_number"] == spec["page_number"]
        assert observed["source_xref"] == spec["source_xref"]
        assert observed["occurrence_index"] == spec["occurrence_index"]
        assert observed["occurrence_json"] == spec["occurrence_json"]
        assert (
            observed["occurrence_evidence_sha256"]
            == spec["occurrence_sha256"]
        )
        assert observed["source_has_mask"] is spec["source_has_mask"]
        assert observed["source_item_id"] == spec["source_item_id"]
        assert observed["representation"] == "raster"
        assert observed["x_size"] == pytest.approx(spec["x_size"])
        assert observed["y_size"] == pytest.approx(spec["y_size"])
        assert observed["anchor_xyz"] == pytest.approx(spec["anchor"])
        assert observed["raster_content_variant"] == spec.get(
            "raster_content_variant", ""
        )
        assert observed["suppression_content"] == spec.get(
            "suppression_content", {}
        )

    type_counts: dict[str, int] = {}
    for spec in specs:
        type_id = result["created_types"][spec["name"]]
        type_counts[type_id] = type_counts.get(type_id, 0) + 1

    archive_inventory = {
        **_raster_inventory(),
        "entity_ids": [spec["name"] for spec in specs],
        "type_counts": type_counts,
        "counts": {
            "total": 3,
            "containers": 0,
            "images": 3,
            "vector_primitives": 0,
            "text_representation_objects": 0,
            "unclassified": 0,
        },
        "categories": {
            "containers": [],
            "images": [spec["name"] for spec in specs],
            "vector_primitives": [],
            "text_representation_objects": [],
            "unclassified": [],
        },
        "objects": [],
    }
    for spec in specs:
        template = _raster_inventory(
            payloads[spec["name"]],
            entity_id=spec["name"],
            source_item_id=spec["source_item_id"],
            page_number=spec["page_number"],
            source_xref=spec["source_xref"],
            occurrence_index=spec["occurrence_index"],
            occurrence_json=spec["occurrence_json"],
            source_has_mask=spec["source_has_mask"],
        )["objects"][0]
        template["type_id"] = result["created_types"][spec["name"]]
        template["content"]["x_size"] = format(spec["x_size"], ".12g")
        template["content"]["y_size"] = format(spec["y_size"], ".12g")
        template["content"]["anchor_xyz"] = spec["anchor"]
        if spec.get("raster_content_variant"):
            template["content"].update(
                {
                    "raster_content_variant": result["observed"][spec["name"]][
                        "raster_content_variant"
                    ],
                    "raster_content_variant_property_verified": True,
                    **result["observed"][spec["name"]]["suppression_content"],
                    "page_text_suppression_binding_verified": True,
                }
            )
        archive_inventory["objects"].append(template)

    archive_evidence = core._read_fcstd_raster_archive_evidence(
        fcstd_path,
        archive_inventory,
    )
    assert archive_evidence["verified"] is True
    assert archive_evidence["expected_raster_count"] == 3
    assert {entry["entity_id"] for entry in archive_evidence["raster_entries"]} == {
        "PageRaster",
        "ItemRaster",
        "EmbeddedRaster",
    }
    assert {
        entry["entity_id"]: entry["sha256"]
        for entry in archive_evidence["raster_entries"]
    } == {spec["name"]: spec["raster_sha256"] for spec in specs}
