"""Truthful FreeCAD host-object accounting and readiness contracts."""

from __future__ import annotations

import json
import hashlib
import os
import stat
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (
    REPO_ROOT / "PDFVectorImporter" / "src",
    REPO_ROOT / "PDFVectorImporter",
    REPO_ROOT / "tests",
):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore import import_report as report_contract  # noqa: E402
from pdfcadcore.fitz_loader import import_fitz  # noqa: E402
import test_freecad_representation_contract as representation_fixtures  # noqa: E402


class HostObject:
    def __init__(
        self,
        name: str,
        type_id: str,
        *,
        representation: str = "",
        source_item_id: str = "",
        parent_source_item_id: str = "",
        shape_nonempty: bool = True,
        proxy_type: str = "",
        text=None,
        custom_text=None,
        image_file: str = "",
        string=None,
        base=None,
        view_style_verified: bool = False,
        source_ink_classification: str = "",
        source_ink_digest: str = "",
        source_text_property=None,
        text_visibility=None,
        pdf_source_sha256: str = "",
        declared_raster_sha256: str = "",
        font_delivery=None,
    ) -> None:
        self.Name = name
        self.TypeId = type_id
        self.PDFRepresentation = representation
        self.PDFSourceItemId = source_item_id
        self.PDFParentSourceItemId = parent_source_item_id
        if proxy_type:
            self.Proxy = type("Proxy", (), {"Type": proxy_type})()
        if text is not None:
            self.Text = text
        if custom_text is not None:
            self.CustomText = custom_text
        if image_file:
            self.ImageFile = image_file
        if string is not None:
            self.String = string
        if base is not None:
            self.Base = base
        if view_style_verified:
            self.ViewObject = type(
                "ViewObject",
                (),
                {
                    "FontName": "Arial",
                    "FontSize": 10.0,
                    "Justification": "Left",
                    "Visibility": False if text_visibility is False else True,
                },
            )()
        if source_ink_classification:
            self.PDFSourceInkClassification = source_ink_classification
        if source_ink_digest:
            self.PDFSourceInkEvidenceSHA256 = source_ink_digest
        if source_text_property is not None:
            self.PDFSourceText = source_text_property
        if type(text_visibility) is bool:
            self.PDFTextVisibility = text_visibility
        if pdf_source_sha256:
            self.PDFSourceSHA256 = pdf_source_sha256
        if declared_raster_sha256:
            self.PDFRasterSHA256 = declared_raster_sha256
        for property_name, value in dict(font_delivery or {}).items():
            setattr(self, property_name, value)
        if type_id.startswith("Part::"):
            self.Shape = Shape(shape_nonempty)

    def isDerivedFrom(self, type_id: str) -> bool:
        return type_id == "App::DocumentObjectGroup" and self.TypeId.startswith(
            "App::DocumentObjectGroup"
        )


class ShapePoint:
    def __init__(self, x: float, y: float, z: float = 0.0) -> None:
        self.x = x
        self.y = y
        self.z = z


class ShapeVertex:
    def __init__(self, x: float, y: float, z: float = 0.0) -> None:
        self.Point = ShapePoint(x, y, z)


class ShapeEdge:
    Length = 1.0

    def __init__(self) -> None:
        self.Vertexes = [ShapeVertex(0.0, 0.0), ShapeVertex(1.0, 0.0)]

    def discretize(self, **_kwargs):
        return [ShapePoint(0.0, 0.0), ShapePoint(1.0, 0.0)]


class Shape:
    def __init__(self, nonempty: bool) -> None:
        self._nonempty = nonempty
        self.Vertexes = (
            [ShapeVertex(0.0, 0.0), ShapeVertex(1.0, 0.0)] if nonempty else []
        )
        self.Edges = [ShapeEdge()] if nonempty else []

    def isNull(self) -> bool:
        return not self._nonempty


def _write_fcstd_archive(
    path: Path,
    shape_entries: dict[str, tuple[str, bytes]],
    *,
    extra_entries: list[tuple[str, bytes]] | None = None,
    document_xml: bytes | None = None,
) -> None:
    """Write the small FCStd subset used by archive-evidence contract tests."""

    if document_xml is None:
        objects = []
        for entity_id, (entry_name, _payload) in shape_entries.items():
            objects.append(
                '<Object name="%s"><Properties>'
                '<Property name="Shape" type="Part::PropertyPartShape">'
                '<Part file="%s"/></Property>'
                "</Properties></Object>" % (entity_id, entry_name)
            )
        document_xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            "<Document><ObjectData>%s</ObjectData></Document>" % "".join(objects)
        ).encode("utf-8")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Document.xml", document_xml)
            for _entity_id, (entry_name, payload) in shape_entries.items():
                archive.writestr(entry_name, payload)
            for entry_name, payload in list(extra_entries or []):
                archive.writestr(entry_name, payload)


def _corrupt_deflated_fcstd_member(path: Path, member_name: str) -> None:
    """Replace a raw-DEFLATE block header without changing ZIP metadata."""

    with zipfile.ZipFile(path, "r") as archive:
        info = archive.getinfo(member_name)
        assert info.compress_type == zipfile.ZIP_DEFLATED
        assert info.compress_size > 0
        data_offset = (
            info.header_offset
            + 30
            + len(info.filename.encode("utf-8"))
            + len(info.extra)
        )
    archive_bytes = bytearray(path.read_bytes())
    archive_bytes[data_offset] = 0xFF
    path.write_bytes(archive_bytes)


def _zero_ink_source_evidence(
    source_item_id: str,
    source_text: str = "   ",
    pdf_sha256: str = "a" * 64,
) -> dict:
    source_font_sha256 = "c" * 64
    usable_font_sha256 = "d" * 64
    font_binding = {
        "asset_id": "sha256:" + usable_font_sha256,
        "source_xref": 1,
        "source_font_sha256": source_font_sha256,
        "usable_font_sha256": usable_font_sha256,
        "base_font_name": "TestFont",
        "span_font_name": "TestFont",
        "source_format": "ttf",
        "usable_format": "ttf",
        "source_origin": "test_exact_font",
    }
    evidence = {
        "schema": "pdf_source_ink_evidence_v1",
        "authority": "pymupdf_rawdict_texttrace_exact_font",
        "pdf_sha256": pdf_sha256,
        "page_number": 1,
        "source_item_id": source_item_id,
        "source_text": source_text,
        "source_text_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "font_identity": {"raw_name": "TestFont", "normalized_key": "testfont"},
        "classification": "zero_visible_ink",
        "zero_ink_characters_layout_only": True,
        "all_characters_physically_resolved": True,
        "font_asset_bindings": [font_binding],
        "glyph_id_sequence": [1 for _character in source_text],
        "characters": [
            {
                "authority": "exact_pdf_font_glyph_bounds",
                "character": character,
                "synthetic": False,
                "glyph_id": 1,
                "glyph_name": "space",
                "glyph_bounds": None,
                "advance_width": 600.0,
                "font_asset_binding": font_binding,
                "source_font_sha256": source_font_sha256,
                "usable_font_sha256": usable_font_sha256,
                "trace_type": 0,
                "opacity": 1.0,
                "zero_visible_ink": True,
                "layout_only_zero_ink": True,
                "physically_resolved": True,
                "source_index": index,
            }
            for index, character in enumerate(source_text)
        ],
    }
    evidence["evidence_sha256"] = core._source_ink_evidence_digest(evidence)
    return evidence


def _visible_3d_font_delivery(tmp_path: Path) -> tuple[dict, dict]:
    source_font_identity = {
        "raw_name": "MissingSourceFont-Regular",
        "normalized_key": "missingsourcefontregular",
    }
    fixture_item = {
        "text": "3D",
        "font_identity": source_font_identity,
        "pdf_sha256": "a" * 64,
        "page_number": 1,
    }
    fixture_path, _fixture_result = representation_fixtures._found_font_resolution(
        fixture_item
    )
    payload = Path(fixture_path).read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    source_path = tmp_path / "installed-font.ttf"
    source_path.write_bytes(payload)
    stage_root = tmp_path / "delivery-assets"
    stage_root.mkdir(exist_ok=True)
    staged_path = stage_root / (digest + ".ttf")
    staged_path.write_bytes(payload)
    os.chmod(staged_path, stat.S_IREAD)
    internal_identity = core._font_internal_identity_evidence(
        payload,
        source_font_identity,
    )
    coverage_evidence = core._font_bytes_glyph_coverage(payload, "3D")
    evidence = {
        "font_path": str(staged_path.resolve()),
        "source_text": "3D",
        "source_font_identity": source_font_identity,
        "source_font_equivalence": False,
        "font_substitution_applied": True,
        "font_identity_verified": False,
        "glyph_coverage_verified": True,
        "delivered_font_sha256": digest,
        "font_candidate_source": "installed_no_cost_substitute",
        "source_font_asset_path": str(source_path.resolve()),
        "source_font_asset_sha256": digest,
        "staged_font_path": str(staged_path.resolve()),
        "staged_font_sha256": digest,
        "staged_asset_verified": True,
        "staged_asset_read_only": True,
        "font_internal_identity_evidence": internal_identity,
        "font_internal_identity_sha256": internal_identity["evidence_sha256"],
        "font_coverage_evidence": coverage_evidence,
        "font_coverage_evidence_sha256": coverage_evidence[
            "coverage_evidence_sha256"
        ],
    }
    properties = {
        "PDFSourceFontEquivalence": evidence["source_font_equivalence"],
        "PDFFontSubstitutionApplied": evidence["font_substitution_applied"],
        "PDFFontIdentityVerified": evidence["font_identity_verified"],
        "PDFFontGlyphCoverageVerified": evidence["glyph_coverage_verified"],
        "PDFDeliveredFontSHA256": evidence["delivered_font_sha256"],
        "PDFFontCandidateSource": evidence["font_candidate_source"],
        "PDFDeliveredFontPath": evidence["font_path"],
        "PDFSourceFontAssetPath": evidence["source_font_asset_path"],
        "PDFSourceFontAssetSHA256": evidence["source_font_asset_sha256"],
        "PDFStagedFontPath": evidence["staged_font_path"],
        "PDFStagedFontSHA256": evidence["staged_font_sha256"],
        "PDFStagedFontVerified": evidence["staged_asset_verified"],
        "PDFStagedFontReadOnly": evidence["staged_asset_read_only"],
        "PDFFontInternalIdentitySHA256": evidence[
            "font_internal_identity_sha256"
        ],
        "PDFFontCoverageEvidenceSHA256": evidence[
            "font_coverage_evidence_sha256"
        ],
        "PDFSourceFontIdentityJSON": json.dumps(
            evidence["source_font_identity"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ),
    }
    return evidence, properties


def _write_minimal_pdf(path: Path) -> None:
    fitz = import_fitz()
    document = fitz.open()
    document.new_page()
    document.save(str(path))
    document.close()


def _minimal_pdf_bytes() -> bytes:
    fitz = import_fitz()
    document = fitz.open()
    try:
        document.new_page()
        return bytes(document.tobytes())
    finally:
        document.close()


_TEST_PDF_BYTES = _minimal_pdf_bytes()
_TEST_PDF_SHA256 = hashlib.sha256(_TEST_PDF_BYTES).hexdigest()


def _bind_test_page_authority(
    opts: core.ImportOptions,
    *,
    source_path: Path | None = None,
) -> str:
    if source_path is not None:
        core._initialize_pdf_source_attempt(str(source_path), opts)
    else:
        with tempfile.TemporaryDirectory(prefix="fc_inventory_v2_") as td:
            path = Path(td) / "source.pdf"
            path.write_bytes(_TEST_PDF_BYTES)
            core._initialize_pdf_source_attempt(str(path), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    return opts._pdf_sha256


def _aws_objects(
    image_file: str = "aws-page-1.png",
    pdf_sha256: str = _TEST_PDF_SHA256,
):
    raster_sha256 = (
        hashlib.sha256(Path(image_file).read_bytes()).hexdigest()
        if image_file and Path(image_file).is_file()
        else "f" * 64
    )
    return [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "PageRaster001",
            "Image::ImagePlane",
            representation="raster",
            source_item_id="p1:page",
            image_file=image_file,
            pdf_source_sha256=pdf_sha256,
            declared_raster_sha256=raster_sha256,
        ),
    ]


def _aws_fallback_opts(
    raster_path: str = "",
    pdf_sha256: str = "",
    *,
    source_path: Path | None = None,
) -> core.ImportOptions:
    opts = core.ImportOptions(import_mode="auto", import_text=True, text_mode="labels")
    bound_digest = _bind_test_page_authority(opts, source_path=source_path)
    if pdf_sha256 and pdf_sha256 != bound_digest:
        raise ValueError("test PDF digest is not bound to the supplied source")
    pdf_sha256 = bound_digest
    raster_sha256 = (
        hashlib.sha256(Path(raster_path).read_bytes()).hexdigest()
        if raster_path and Path(raster_path).is_file()
        else "f" * 64
    )
    core._record_no_source_text_page_fallback(
        opts,
        page_num=1,
        pdf_sha256=pdf_sha256,
        raw_tdict={"blocks": [{"type": 1, "bbox": (0.0, 0.0, 100.0, 100.0)}]},
        raster_result={
            "outcome": "verified",
            "created_entity_ids": ["PageRaster001"],
            "evidence": {
                "host_entity_type": "Image::ImagePlane",
                "source_page_number": 1,
                "pdf_sha256": pdf_sha256,
                "source_asset_sha256": raster_sha256,
                "raster_content_verified": True,
                "raster_file_included": True,
            },
        },
    )
    return opts


def _direct_label_opts(*, delivered_count: bool = True) -> core.ImportOptions:
    opts = core.ImportOptions(import_mode="vector", import_text=True, text_mode="labels")
    opts.text_delivery_obligation_source_item_ids.append("p1:text:0")
    opts.text_delivery_attempts.append(
        {
            "source_item_id": "p1:text:0",
            "requested_type": "labels",
            "attempted_type": "labels",
            "final_type": "labels",
            "outcome": "verified",
            "created_entity_ids": ["Label001"],
            "delivery_entity_ids": ["Label001"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "attempted_types": ["labels"],
            "proof_chain": [],
            "evidence": {
                "host_entity_type": "App::FeaturePython",
                "host_proxy_type": "Label",
                "source_text": "LABEL",
                "source_text_preserved": True,
                "view_style_verified": True,
                "label_marker_absent": True,
            },
        }
    )
    if delivered_count:
        opts.text_delivered_counts["native_label"] = 1
    return opts


def _direct_text_opts(*, view_style_verified: bool = True) -> core.ImportOptions:
    opts = core.ImportOptions(import_mode="vector", import_text=True, text_mode="text")
    opts.text_delivery_obligation_source_item_ids.append("p1:text:0")
    opts.text_delivery_attempts.append(
        {
            "source_item_id": "p1:text:0",
            "requested_type": "text",
            "attempted_type": "text",
            "final_type": "text",
            "outcome": "verified",
            "created_entity_ids": ["Text001"],
            "delivery_entity_ids": ["Text001"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 1,
            "attempted_types": ["text"],
            "proof_chain": [],
            "evidence": {
                "host_entity_type": "App::FeaturePython",
                "host_proxy_type": "Text",
                "source_text": "SOURCE",
                "source_text_preserved": True,
                "view_style_verified": view_style_verified,
            },
        }
    )
    opts.text_delivered_counts["native_text"] = 1
    return opts


def _label_objects():
    return [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "Label001",
            "App::FeaturePython",
            representation="labels",
            source_item_id="p1:text:0",
            proxy_type="Label",
            text=["LABEL"],
            custom_text=["LABEL"],
            view_style_verified=True,
        ),
    ]


def _attach_inventory(opts: core.ImportOptions, objects) -> None:
    inventory = core._build_host_object_inventory(objects)
    opts._report_extra = {
        "actual_host_object_inventory": inventory,
        "save_reopen_inventory": core._crosscheck_host_object_inventory(
            inventory, objects
        ),
        "result_status": "success",
    }


def test_inventory_counts_groups_images_and_only_nonempty_geometry(tmp_path) -> None:
    raster_path = tmp_path / "inventory-page.png"
    raster_path.write_bytes(b"inventory raster bytes")
    objects = _aws_objects(str(raster_path)) + [
        HostObject("Line001", "Part::Feature", shape_nonempty=True),
        HostObject("EmptyPart", "Part::Feature", shape_nonempty=False),
        HostObject(
            "Glyph001",
            "Part::Feature",
            representation="glyphs",
            source_item_id="p1:b0:l0:s0:g0",
        ),
    ]

    inventory = core._build_host_object_inventory(objects)

    assert inventory["verified"] is True
    assert inventory["counts"] == {
        "total": 5,
        "containers": 1,
        "images": 1,
        "vector_primitives": 1,
        "text_representation_objects": 1,
        "unclassified": 1,
    }
    assert inventory["categories"]["containers"] == ["PDF_Page_1"]
    assert inventory["categories"]["images"] == ["PageRaster001"]
    assert inventory["categories"]["vector_primitives"] == ["Line001"]
    assert inventory["categories"]["text_representation_objects"] == ["Glyph001"]


def test_saved_inventory_crosscheck_rejects_type_or_representation_drift() -> None:
    expected = core._build_host_object_inventory(_aws_objects())
    changed = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "PageRaster001",
            "Part::Feature",
            representation="geometry",
            source_item_id="p1:page",
        ),
    ]

    crosscheck = core._crosscheck_host_object_inventory(expected, changed)

    assert crosscheck["verified"] is False
    assert crosscheck["missing_entity_ids"] == []
    assert crosscheck["mismatched_entities"] == [
        {
            "entity_id": "PageRaster001",
            "expected_type_id": "Image::ImagePlane",
            "actual_type_id": "Part::Feature",
            "expected_representation": "raster",
            "actual_representation": "geometry",
        }
    ]


def test_aws_image_only_fallback_report_is_truthful_and_ready(tmp_path) -> None:
    raster_path = tmp_path / "aws-page-1.png"
    raster_path.write_bytes(b"real persisted AWS raster bytes")
    pdf_path = tmp_path / "AWSWeldSymbolchart.pdf"
    _write_minimal_pdf(pdf_path)
    pdf_sha256 = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    objects = _aws_objects(str(raster_path), pdf_sha256)
    opts = _aws_fallback_opts(
        str(raster_path),
        pdf_sha256,
        source_path=pdf_path,
    )
    inventory = core._build_host_object_inventory(objects)
    opts._report_extra = {
        "actual_host_object_inventory": inventory,
        "save_reopen_inventory": core._crosscheck_host_object_inventory(
            inventory, objects
        ),
        "result_status": "success",
    }
    report_path = tmp_path / "aws.import_report.json"

    core.write_import_report(
        pdf_path=str(pdf_path),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        primitive_count=0,
        text_count=0,
        image_count=1,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["result"]["primitives"] == 0
    assert report["result"]["text_entities"] == 0
    assert report["result"]["images"] == 1
    assert report["extra"]["actual_host_object_inventory"]["counts"]["containers"] == 1
    assert report["extra"]["actual_text_entity_types"]["raster_text_patch"] == 1
    assert report["extra"]["text_representation_delivery"]["verified"] is True
    assert opts._live_import_report.extra["import_contract_ready"]["ready"] is True
    assert report["extra"]["import_contract_ready"]["ready"] is False


def test_zero_source_contract_requires_an_explicit_verified_empty_projection(
    tmp_path,
) -> None:
    opts = core.ImportOptions(import_mode="vector", import_text=True, text_mode="labels")
    inventory = core._build_host_object_inventory(
        [HostObject("PDF_Page_1", "App::DocumentObjectGroup")]
    )
    opts._report_extra = {
        "actual_host_object_inventory": inventory,
        "save_reopen_inventory": core._crosscheck_host_object_inventory(
            inventory,
            [HostObject("PDF_Page_1", "App::DocumentObjectGroup")],
        ),
        "result_status": "success",
    }
    report_path = tmp_path / "unattempted.import_report.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "fixture.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
    )

    extra = json.loads(report_path.read_text(encoding="utf-8"))["extra"]
    assert extra["actual_text_entity_types"]["count"] == 0
    assert extra["text_delivery_attempts"] == []
    assert extra["text_delivery_obligations"]["required"] is False
    assert extra["text_representation_delivery"]["required"] is False
    assert extra["text_representation_delivery"]["verified"] is True
    assert extra["import_contract_ready"]["checks"]["text_delivery"] is True
    assert extra["import_contract_ready"]["ready"] is True


def test_generic_failure_cannot_be_hidden_before_a_verified_fallback() -> None:
    opts = _aws_fallback_opts()
    opts.text_delivery_attempts[0]["outcome"] = "failed"

    delivery = core._build_text_representation_delivery(
        opts,
        opts.text_delivery_attempts,
    )

    assert delivery["verified"] is False
    assert any(
        "lacks impossibility proof" in reason
        for reason in delivery["invalid_reasons"]
    )


def test_delivery_summary_references_one_canonical_attempt_ledger() -> None:
    opts = _direct_label_opts()
    ledger = [dict(attempt) for attempt in opts.text_delivery_attempts]

    delivery = core._build_text_representation_delivery(opts, ledger)

    assert delivery["schema"] == "bcs.text_representation_delivery/1.1"
    assert set(delivery) == {
        "schema",
        "required",
        "requested_type",
        "verified",
        "attempt_count",
        "source_item_count",
        "delivered_item_count",
        "failed_item_count",
        "items",
        "invalid_reasons",
    }
    assert "terminal_attempts" not in delivery
    assert "terminal_attempt_indexes" not in delivery
    assert "source_item_ids" not in delivery
    assert delivery["items"] == [
        {
            "source_item_id": "p1:text:0",
            "terminal_attempt_index": 0,
            "final_type": "labels",
            "verified": True,
        }
    ]
    assert report_contract._freecad_delivery_terminal_attempts(
        delivery,
        ledger,
    ) == [ledger[0]]

    tampered_ledger = json.loads(json.dumps(ledger))
    tampered_ledger[0]["source_item_id"] = "p1:different"
    assert report_contract._freecad_delivery_terminal_attempts(
        delivery,
        tampered_ledger,
    ) is None

    tampered_delivery = json.loads(json.dumps(delivery))
    tampered_delivery["items"][0]["terminal_attempt_index"] = 99
    assert report_contract._freecad_delivery_terminal_attempts(
        tampered_delivery,
        ledger,
    ) is None


def test_serialized_attempt_evidence_sentinel_occurs_exactly_once(tmp_path) -> None:
    opts = _direct_label_opts()
    sentinel = "ONE_CANONICAL_LEDGER_SENTINEL_7ef9432c"
    opts.text_delivery_attempts[0]["evidence"]["serialization_sentinel"] = sentinel
    _attach_inventory(opts, _label_objects())
    report_path = tmp_path / "canonical-ledger.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    serialized = report_path.read_text(encoding="utf-8")
    assert serialized.count(sentinel) == 1
    extra = json.loads(serialized)["extra"]
    assert "terminal_attempts" not in extra["text_representation_delivery"]
    assert extra["text_representation_delivery"]["items"][0][
        "terminal_attempt_index"
    ] == 0


def test_ready_rejects_delivery_ids_absent_from_reopened_inventory(tmp_path) -> None:
    opts = _aws_fallback_opts()
    reopened_objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "DifferentRaster001",
            "Image::ImagePlane",
            representation="raster",
            source_item_id="p1:page",
        ),
    ]
    inventory = core._build_host_object_inventory(reopened_objects)
    opts._report_extra = {
        "actual_host_object_inventory": inventory,
        "save_reopen_inventory": core._crosscheck_host_object_inventory(
            inventory, reopened_objects
        ),
        "result_status": "success",
    }
    report_path = tmp_path / "mismatched-delivery-id.import_report.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "AWSWeldSymbolchart.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        image_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_ready_rejects_self_contradictory_save_reopen_claim(tmp_path) -> None:
    opts = _direct_label_opts()
    objects = _label_objects()
    _attach_inventory(opts, objects)
    opts._report_extra["save_reopen_inventory"].update(
        {
            "verified": True,
            "expected_entity_ids": ["Other001"],
            "missing_entity_ids": ["Label001"],
            "duplicate_actual_entity_ids": ["Other001"],
            "unexpected_entity_ids": ["Unexpected001"],
            "mismatched_entities": [{"entity_id": "Label001"}],
            "counts_match": False,
        }
    )
    report_path = tmp_path / "contradictory-reopen.import_report.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["save_reopen_inventory"] is False
    assert ready["ready"] is False


def test_ready_reconciles_reported_text_types_and_counts_to_terminals(tmp_path) -> None:
    for reported in (
        {"entity_type": "none", "count": 0},
        {"entity_type": "labels", "count": 99, "native_label": 99},
    ):
        opts = _direct_label_opts(delivered_count=False)
        _attach_inventory(opts, _label_objects())
        opts._report_extra["actual_text_entity_types"] = reported
        report_path = tmp_path / ("bad-types-%s.json" % reported["count"])

        core.write_import_report(
            pdf_path=str(tmp_path / "drawing.pdf"),
            output_path=str(report_path),
            opts=opts,
            pages_imported=1,
            total_pages=1,
            text_count=0,
        )

        ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
            "import_contract_ready"
        ]
        assert ready["checks"]["actual_text_entity_types"] is False
        assert ready["ready"] is False


def test_delivery_aggregate_rejects_terminal_type_drift() -> None:
    opts = _direct_label_opts()
    terminal = opts.text_delivery_attempts[0]
    terminal["final_type"] = "text"
    opts.text_mode_fallbacks.append(
        {
            "requested": "labels",
            "delivered": "text",
            "count": 1,
            "source_item_ids": ["p1:text:0"],
            "proof": {"item_specific_proven_impossible": True},
        }
    )

    delivery = core._build_text_representation_delivery(
        opts, opts.text_delivery_attempts
    )

    assert delivery["verified"] is False
    assert any(
        "terminal final_type mismatch" in reason
        for reason in delivery["invalid_reasons"]
    )


def test_delivery_aggregate_rejects_leaked_prior_attempt() -> None:
    opts = _aws_fallback_opts()
    prior = opts.text_delivery_attempts[0]
    prior["created_entity_ids"] = ["LeakedLabel"]
    prior["removed_entity_ids"] = []
    prior["proof"]["created_entity_ids"] = ["LeakedLabel"]
    prior["proof"]["removed_entity_ids"] = []

    delivery = core._build_text_representation_delivery(
        opts, opts.text_delivery_attempts
    )

    assert delivery["verified"] is False
    assert any(
        "cleanup ownership mismatch" in reason
        for reason in delivery["invalid_reasons"]
    )


def test_empty_or_unexpected_reopened_inventory_is_not_verified() -> None:
    empty = core._build_host_object_inventory([])
    assert empty["verified"] is False

    expected_objects = [HostObject("PDF_Page_1", "App::DocumentObjectGroup")]
    expected = core._build_host_object_inventory(expected_objects)
    actual = expected_objects + [HostObject("UnexpectedLine", "Part::Feature")]
    crosscheck = core._crosscheck_host_object_inventory(expected, actual)
    assert crosscheck["verified"] is False
    assert crosscheck["unexpected_entity_ids"] == ["UnexpectedLine"]


def test_post_baseline_filter_uses_identity_and_stable_names() -> None:
    original = HostObject("Existing", "Part::Feature")
    rematerialized = HostObject("Existing", "Part::Feature")
    imported = HostObject("Imported", "Part::Feature")

    post_baseline = core._post_baseline_document_objects(
        [rematerialized, imported],
        {id(original)},
        {"Existing"},
    )

    assert post_baseline == [imported]


def test_unclosed_mixed_item_fallback_is_not_report_ready(tmp_path) -> None:
    opts = _direct_label_opts()
    source_id = "p1:text:1"
    opts.text_delivery_obligation_source_item_ids.append(source_id)
    proof = {
        "item_specific_proven_impossible": True,
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": "b" * 64,
        "page_number": 1,
        "source_item_id": source_id,
        "requested_type": "labels",
        "attempted_type": "labels",
        "reason_code": "native_label_unavailable",
        "evidence": {"native_label_available": False},
        "attempted_source_results": [
            {"source": "freecad_label_api", "outcome": "not_found"}
        ],
        "attempted_sources_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }
    opts.text_delivery_attempts.extend(
        [
            {
                "source_item_id": source_id,
                "requested_type": "labels",
                "attempted_type": "labels",
                "final_type": None,
                "outcome": "proven_impossible",
                "reason_code": "native_label_unavailable",
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
                "proof": proof,
            },
            {
                "source_item_id": source_id,
                "requested_type": "labels",
                "attempted_type": "text",
                "final_type": "text",
                "outcome": "verified",
                "created_entity_ids": ["Text001"],
                "delivery_entity_ids": ["Text001"],
                "support_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
                "attempted_types": ["labels", "text"],
                "proof_chain": [proof],
                "evidence": {
                    "host_entity_type": "App::FeaturePython",
                    "host_proxy_type": "Text",
                    "source_text": "FALLBACK TEXT",
                    "source_text_preserved": True,
                    "view_style_verified": True,
                },
            },
        ]
    )
    fallback_proof = dict(proof)
    fallback_proof.update(
        {
            "attempted_types": ["labels", "text"],
            "proof_chain": [proof],
            "transition_chain": [{"from": "labels", "to": "text"}],
        }
    )
    opts.text_mode_fallbacks.append(
        {
            "requested": "labels",
            "delivered": "text",
            "reason": "proof_gated:labels:native_label_unavailable",
            "count": 1,
            "source_item_ids": [source_id],
            "proof": fallback_proof,
        }
    )
    opts.text_delivered_counts.update({"native_label": 1, "native_text": 1})
    objects = _label_objects() + [
        HostObject(
            "Text001",
            "App::FeaturePython",
            representation="text",
            source_item_id=source_id,
            proxy_type="Text",
            text=["FALLBACK TEXT"],
            view_style_verified=True,
        )
    ]
    _attach_inventory(opts, objects)
    report_path = tmp_path / "mixed.import_report.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=2,
    )

    extra = json.loads(report_path.read_text(encoding="utf-8"))["extra"]
    assert "representation_contract_violation" not in extra
    assert extra["import_contract_ready"]["checks"]["text_delivery"] is False
    assert extra["import_contract_ready"]["ready"] is False


@pytest.mark.parametrize(
    ("representation", "child_suffix", "delivery_count", "bucket"),
    (
        ("glyphs", ":g0", 1, "outline_curve_or_mesh"),
        ("geometry", ":geometry", 7, "raw_geometry_edges"),
    ),
)
def test_svg_child_delivery_binds_through_its_persisted_parent_source_id(
    tmp_path,
    representation,
    child_suffix,
    delivery_count,
    bucket,
) -> None:
    """Legitimate SVG children must not be blocked by their child-level IDs."""

    parent_source_id = "p1:b0:l0:s0"
    entity_id = "SVGChild001"
    opts = core.ImportOptions(
        import_mode="vector",
        import_text=True,
        text_mode=representation,
    )
    opts.text_delivery_obligation_source_item_ids.append(parent_source_id)
    opts.text_delivery_attempts.append(
        {
            "source_item_id": parent_source_id,
            "requested_type": representation,
            "attempted_type": representation,
            "final_type": representation,
            "outcome": "verified",
            "created_entity_ids": [entity_id],
            "delivery_entity_ids": [entity_id],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": delivery_count,
            "evidence": {"child_source_item_ids": [parent_source_id + child_suffix]},
        }
    )
    opts.text_delivered_counts[bucket] = delivery_count
    objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            entity_id,
            "Part::Feature",
            representation=representation,
            source_item_id=parent_source_id + child_suffix,
            parent_source_item_id=parent_source_id,
        ),
    ]
    _attach_inventory(opts, objects)
    report_path = tmp_path / (representation + "-child-binding.json")

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=delivery_count,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is True
    assert ready["ready"] is True


def test_ready_rejects_prior_attempt_entity_that_still_exists_after_reopen(
    tmp_path,
) -> None:
    """A removed ID cannot remain in the actual persisted host inventory."""

    opts = _aws_fallback_opts()
    leaked_id = "LeakedLabel001"
    first_attempt = opts.text_delivery_attempts[0]
    first_attempt["created_entity_ids"] = [leaked_id]
    first_attempt["removed_entity_ids"] = [leaked_id]
    first_attempt["proof"]["created_entity_ids"] = [leaked_id]
    first_attempt["proof"]["removed_entity_ids"] = [leaked_id]
    fallback_proof = opts.text_mode_fallbacks[0]["proof"]
    fallback_proof["created_entity_ids"] = [leaked_id]
    fallback_proof["removed_entity_ids"] = [leaked_id]
    objects = _aws_objects() + [
        HostObject(
            leaked_id,
            "App::FeaturePython",
            representation="labels",
            source_item_id="p1:page",
        )
    ]
    _attach_inventory(opts, objects)
    report_path = tmp_path / "leaked-cleanup-entity.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "AWSWeldSymbolchart.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        image_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_ready_rejects_terminal_count_not_bound_to_delivery_host_ids(tmp_path) -> None:
    """A logical count cannot certify host entities absent from the inventory."""

    opts = _direct_label_opts()
    opts.text_delivery_attempts[0]["delivery_count"] = 2
    opts.text_delivered_counts["native_label"] = 2
    _attach_inventory(opts, _label_objects())
    report_path = tmp_path / "inflated-terminal-count.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=2,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["actual_text_entity_types"] is False
    assert ready["ready"] is False


@pytest.mark.parametrize("proof_page", (1.5, 2))
def test_ready_rejects_inexact_page_identity_in_item_fallback_proof(
    tmp_path,
    proof_page,
) -> None:
    """Proof page identity must be exact and agree with the source item ID."""

    opts = _aws_fallback_opts()
    for attempt in opts.text_delivery_attempts[:-1]:
        proof = attempt["proof"]
        proof["page_number"] = proof_page
    fallback_proof = opts.text_mode_fallbacks[0]["proof"]
    fallback_proof["page_number"] = proof_page
    _attach_inventory(opts, _aws_objects())
    report_path = tmp_path / "fractional-proof-page.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "AWSWeldSymbolchart.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        image_count=1,
    )

    delivery = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "text_representation_delivery"
    ]
    assert delivery["verified"] is False
    assert any("page" in reason for reason in delivery["invalid_reasons"])


def test_no_source_fallback_rejects_visible_canonical_text() -> None:
    """The page helper must derive absence instead of trusting its caller."""

    opts = core.ImportOptions(import_mode="vector", import_text=True, text_mode="labels")
    _bind_test_page_authority(opts)
    raw_tdict = {
        "blocks": [
            {
                "type": 0,
                "lines": [
                    {
                        "dir": (1.0, 0.0),
                        "spans": [
                            {
                                "text": "VISIBLE",
                                "bbox": (0.0, 0.0, 20.0, 10.0),
                                "origin": (0.0, 8.0),
                                "size": 10.0,
                            }
                        ],
                    }
                ],
            }
        ]
    }

    with pytest.raises(ValueError, match="canonical source"):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256=opts._pdf_sha256,
            raw_tdict=raw_tdict,
            raster_result={
                "outcome": "verified",
                "created_entity_ids": ["PageRaster001"],
                "evidence": {"host_entity_type": "Image::ImagePlane"},
            },
        )


@pytest.mark.parametrize("page_num", (True, 1.5, "1"))
def test_explicit_raster_page_identity_is_not_coerced(page_num) -> None:
    opts = core.ImportOptions(import_mode="raster", import_text=True, text_mode="raster")

    with pytest.raises(ValueError, match="context"):
        core._record_explicit_page_raster_delivery(
            opts,
            page_num=page_num,
            raster_result={
                "outcome": "verified",
                "created_entity_ids": ["PageRaster001"],
                "evidence": {"host_entity_type": "Image::ImagePlane"},
            },
        )


def test_reopen_crosscheck_rejects_non_raster_shape_that_became_empty() -> None:
    """Representation metadata cannot hide lost Glyphs/Geometry/3D geometry."""

    expected = core._build_host_object_inventory(
        [
            HostObject(
                "Geometry001",
                "Part::Feature",
                representation="geometry",
                source_item_id="p1:b0:l0:s0:geometry",
                parent_source_item_id="p1:b0:l0:s0",
                shape_nonempty=True,
            )
        ]
    )
    reopened = [
        HostObject(
            "Geometry001",
            "Part::Feature",
            representation="geometry",
            source_item_id="p1:b0:l0:s0:geometry",
            parent_source_item_id="p1:b0:l0:s0",
            shape_nonempty=False,
        )
    ]

    crosscheck = core._crosscheck_host_object_inventory(expected, reopened)

    assert crosscheck["verified"] is False
    assert crosscheck["mismatched_entities"]


def test_reopen_crosscheck_rejects_label_text_content_drift() -> None:
    """A reopened Label with the right metadata but wrong text is not delivery."""

    expected = core._build_host_object_inventory(
        [
            HostObject(
                "Label001",
                "App::FeaturePython",
                representation="labels",
                source_item_id="p1:b0:l0:s0",
                proxy_type="Label",
                text=["SOURCE"],
                custom_text=["SOURCE"],
            )
        ]
    )
    reopened = [
        HostObject(
            "Label001",
            "App::FeaturePython",
            representation="labels",
            source_item_id="p1:b0:l0:s0",
            proxy_type="Label",
            text=["CHANGED"],
            custom_text=["CHANGED"],
        )
    ]

    crosscheck = core._crosscheck_host_object_inventory(expected, reopened)

    assert crosscheck["verified"] is False
    assert crosscheck["mismatched_entities"]


def test_reopen_crosscheck_rejects_raster_that_lost_its_image_content() -> None:
    expected = core._build_host_object_inventory(
        [
            HostObject(
                "Raster001",
                "Image::ImagePlane",
                representation="raster",
                source_item_id="p1:page",
                image_file="page.png",
            )
        ]
    )
    reopened = [
        HostObject(
            "Raster001",
            "Image::ImagePlane",
            representation="raster",
            source_item_id="p1:page",
        )
    ]

    crosscheck = core._crosscheck_host_object_inventory(expected, reopened)

    assert crosscheck["verified"] is False
    assert crosscheck["mismatched_entities"]


def test_reopen_crosscheck_accepts_relocated_identical_raster_cache(
    tmp_path,
) -> None:
    """FreeCAD rematerializes embedded image assets under a new cache path."""

    expected_dir = tmp_path / "before"
    actual_dir = tmp_path / "after"
    expected_dir.mkdir()
    actual_dir.mkdir()
    expected_image = expected_dir / "page-source-p1.png"
    actual_image = actual_dir / "freecad-rematerialized-cache-8842.bin"
    expected_image.write_bytes(b"same persisted raster content")
    actual_image.write_bytes(expected_image.read_bytes())
    expected = core._build_host_object_inventory(
        [
            HostObject(
                "Raster001",
                "Image::ImagePlane",
                representation="raster",
                source_item_id="p1:page",
                image_file=str(expected_image),
            )
        ]
    )
    reopened = [
        HostObject(
            "Raster001",
            "Image::ImagePlane",
            representation="raster",
            source_item_id="p1:page",
            image_file=str(actual_image),
        )
    ]

    crosscheck = core._crosscheck_host_object_inventory(expected, reopened)

    assert crosscheck["verified"] is True


def test_ready_rejects_metadata_only_label_without_native_label_content(
    tmp_path,
) -> None:
    opts = _direct_label_opts()
    ghost_objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "Label001",
            "App::FeaturePython",
            representation="labels",
            source_item_id="p1:text:0",
        ),
    ]
    _attach_inventory(opts, ghost_objects)
    report_path = tmp_path / "metadata-only-label.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_ready_rejects_headless_label_visual_evidence_as_final(tmp_path) -> None:
    """Headless metadata is pending evidence, not visual verification."""

    opts = _direct_label_opts()
    terminal_evidence = opts.text_delivery_attempts[0]["evidence"]
    terminal_evidence["view_style_verified"] = False
    terminal_evidence["label_marker_absent"] = False
    terminal_evidence["style_verification"] = "headless_app_metadata"
    _attach_inventory(opts, _label_objects())
    report_path = tmp_path / "headless-label-style.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["text_delivery"] is False
    assert ready["ready"] is False


def test_ready_rejects_fallback_event_with_unbound_items_or_inflated_count(
    tmp_path,
) -> None:
    """One valid item proof cannot authorize extra items or deliveries."""

    opts = _aws_fallback_opts()
    event = opts.text_mode_fallbacks[0]
    event["source_item_ids"] = ["p1:page", "p2:ghost"]
    event["count"] = 99
    _attach_inventory(opts, _aws_objects())
    report_path = tmp_path / "inflated-fallback-event.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "AWSWeldSymbolchart.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        image_count=1,
    )

    extra = json.loads(report_path.read_text(encoding="utf-8"))["extra"]
    assert extra["text_representation_delivery"]["verified"] is False
    assert extra["import_contract_ready"]["ready"] is False


def test_ready_rejects_delivery_whose_live_support_objects_are_absent(
    tmp_path,
) -> None:
    """3D Text support ownership must bind to reopened objects too."""

    source_item_id = "p1:b0:l0:s0"
    opts = core.ImportOptions(
        import_mode="vector",
        import_text=True,
        text_mode="3d_text",
    )
    opts.text_delivery_attempts.append(
        {
            "source_item_id": source_item_id,
            "requested_type": "3d_text",
            "attempted_type": "3d_text",
            "final_type": "3d_text",
            "outcome": "verified",
            "created_entity_ids": ["ShapeString001", "ScaleSupport001", "Extrusion001"],
            "delivery_entity_ids": ["Extrusion001"],
            "support_entity_ids": ["ShapeString001", "ScaleSupport001"],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {
                "source_text": "3D",
                "source_text_preserved": True,
                "solid_count": 1,
                "volume": 1.0,
                "view_style_verified": True,
            },
        }
    )
    opts.text_delivered_counts["native_3d_text"] = 1
    reopened_objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "Extrusion001",
            "Part::Extrusion",
            representation="3d_text",
            source_item_id=source_item_id,
        ),
    ]
    _attach_inventory(opts, reopened_objects)
    report_path = tmp_path / "missing-3d-support.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("pdf_sha256", "b" * 64),
        ("importer_identity", "different.importer"),
    ),
)
def test_ready_rejects_item_proof_bound_to_other_source_authority(
    tmp_path,
    field,
    value,
) -> None:
    opts = _aws_fallback_opts()
    for attempt in opts.text_delivery_attempts[:-1]:
        attempt["proof"][field] = value
    opts.text_mode_fallbacks[0]["proof"][field] = value
    _attach_inventory(opts, _aws_objects())
    report_path = tmp_path / ("wrong-proof-" + field + ".json")

    core.write_import_report(
        pdf_path=str(tmp_path / "AWSWeldSymbolchart.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        image_count=1,
    )

    extra = json.loads(report_path.read_text(encoding="utf-8"))["extra"]
    assert extra["text_representation_delivery"]["verified"] is False
    assert extra["import_contract_ready"]["ready"] is False


def test_ready_does_not_truncate_fractional_delivered_count(tmp_path) -> None:
    opts = _direct_label_opts()
    opts.text_delivered_counts["native_label"] = 1.5
    _attach_inventory(opts, _label_objects())
    report_path = tmp_path / "fractional-delivered-count.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["actual_text_entity_types"] is False
    assert ready["ready"] is False


@pytest.mark.parametrize("count", (True, 1.5, "1"))
def test_fallback_record_count_is_not_coerced(count) -> None:
    opts = core.ImportOptions(import_mode="vector", import_text=True, text_mode="labels")

    with pytest.raises(ValueError, match="count"):
        core._record_text_mode_fallback(
            opts,
            requested="labels",
            delivered="text",
            reason="proof_gated:labels:native_label_unavailable",
            count=count,
            source_item_id="p1:b0:l0:s0",
            proof={
                "item_specific_proven_impossible": True,
                "evidence": {"native_label_available": False},
                "attempted_types": ["labels", "text"],
            },
        )


def test_terminal_delivery_must_explicitly_preserve_final_representation() -> None:
    opts = _direct_label_opts()
    opts.text_delivery_attempts[0].pop("final_type")

    delivery = core._build_text_representation_delivery(
        opts,
        opts.text_delivery_attempts,
    )

    assert delivery["verified"] is False
    assert any("final_type" in reason for reason in delivery["invalid_reasons"])


@pytest.mark.parametrize("source_item_id", (True, 1, 1.5))
def test_terminal_source_item_identity_is_not_coerced(
    tmp_path,
    source_item_id,
) -> None:
    opts = _direct_label_opts()
    opts.text_delivery_attempts[0]["source_item_id"] = source_item_id
    objects = _label_objects()
    objects[-1].PDFSourceItemId = str(source_item_id).lower() if source_item_id is True else str(source_item_id)
    _attach_inventory(opts, objects)
    report_path = tmp_path / "numeric-source-item-id.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    delivery = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "text_representation_delivery"
    ]
    assert delivery["verified"] is False


def test_ready_rejects_stably_persisted_label_text_that_disagrees_with_source(
    tmp_path,
) -> None:
    """Before/reopen equality cannot replace source-to-host content binding."""

    opts = _direct_label_opts()
    wrong_objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "Label001",
            "App::FeaturePython",
            representation="labels",
            source_item_id="p1:text:0",
            proxy_type="Label",
            text=["WRONG"],
            custom_text=["WRONG"],
        ),
    ]
    _attach_inventory(opts, wrong_objects)
    report_path = tmp_path / "wrong-but-stable-label.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_ready_rejects_metadata_only_native_text_without_source_content(
    tmp_path,
) -> None:
    """PDFRepresentation metadata is not native Text content."""

    opts = _direct_text_opts()
    ghost_objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "Text001",
            "App::FeaturePython",
            representation="text",
            source_item_id="p1:text:0",
            proxy_type="Text",
        ),
    ]
    _attach_inventory(opts, ghost_objects)
    report_path = tmp_path / "metadata-only-native-text.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_ready_keeps_headless_native_text_view_style_pending(tmp_path) -> None:
    """Headless app metadata is not visual verification for native Text."""

    opts = _direct_text_opts(view_style_verified=False)
    objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "Text001",
            "App::FeaturePython",
            representation="text",
            source_item_id="p1:text:0",
            proxy_type="Text",
            text=["SOURCE"],
            custom_text=["SOURCE"],
        ),
    ]
    _attach_inventory(opts, objects)
    report_path = tmp_path / "headless-native-text.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["text_delivery"] is False
    assert ready["ready"] is False


def test_ready_rejects_raster_path_without_persisted_image_bytes(tmp_path) -> None:
    """A stable filename string cannot certify a missing/empty Raster asset."""

    opts = _aws_fallback_opts()
    missing_objects = _aws_objects()
    _attach_inventory(opts, missing_objects)
    report_path = tmp_path / "missing-raster-bytes.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "AWSWeldSymbolchart.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        image_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_svg_child_delivery_rejects_unrelated_child_source_identity(
    tmp_path,
) -> None:
    """Parent binding cannot excuse a child ID outside terminal child evidence."""

    parent_source_id = "p1:b0:l0:s0"
    opts = core.ImportOptions(
        import_mode="vector", import_text=True, text_mode="glyphs"
    )
    opts.text_delivery_attempts.append(
        {
            "source_item_id": parent_source_id,
            "requested_type": "glyphs",
            "attempted_type": "glyphs",
            "final_type": "glyphs",
            "outcome": "verified",
            "created_entity_ids": ["Glyph001"],
            "delivery_entity_ids": ["Glyph001"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 1,
            "evidence": {
                "child_source_item_ids": [parent_source_id + ":g0"]
            },
        }
    )
    opts.text_delivered_counts["outline_curve_or_mesh"] = 1
    objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "Glyph001",
            "Part::Feature",
            representation="glyphs",
            source_item_id="p1:other-source:g0",
            parent_source_item_id=parent_source_id,
        ),
    ]
    _attach_inventory(opts, objects)
    report_path = tmp_path / "unrelated-child-source.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_no_source_fallback_rejects_unbound_source_result_authority(
    tmp_path,
) -> None:
    """A generic nonempty result cannot prove the exact PyMuPDF source absent."""

    opts = _aws_fallback_opts()
    malformed_result = {"source": "unrelated_probe", "outcome": "not_checked"}
    for attempt in opts.text_delivery_attempts[:-1]:
        attempt["proof"]["attempted_source_results"] = [dict(malformed_result)]
    fallback_proof = opts.text_mode_fallbacks[0]["proof"]
    fallback_proof["attempted_source_results"] = [dict(malformed_result)]
    _attach_inventory(opts, _aws_objects())
    report_path = tmp_path / "unbound-no-source-proof.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "AWSWeldSymbolchart.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        image_count=1,
    )

    delivery = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "text_representation_delivery"
    ]
    assert delivery["verified"] is False


def test_reopen_crosscheck_snapshots_each_actual_shape_once() -> None:
    """One reopened inventory pass must supply both comparison and report records."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    expected = core._build_host_object_inventory([before])
    edge = after.Shape.Edges[0]
    original_discretize = edge.discretize
    calls = []

    def counting_discretize(**kwargs):
        calls.append(dict(kwargs))
        return original_discretize(**kwargs)

    edge.discretize = counting_discretize

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is True
    assert calls == [{"Number": core._SHAPE_FINGERPRINT_EDGE_SAMPLES}]


def test_sampled_snapshot_does_not_also_export_brep() -> None:
    """The tolerant fallback must not pay for serialization after choosing samples."""

    geometry = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )

    def reject_brep_export():
        raise AssertionError("sampled snapshots must not export BREP")

    geometry.Shape.exportBrepToString = reject_brep_export

    inventory = core._build_host_object_inventory([geometry])

    assert inventory["verified"] is True
    assert inventory["objects"][0]["content"]["shape_fingerprint_verified"] is True


def test_identical_shape_digest_skips_tolerant_alignment(monkeypatch) -> None:
    """Exact raw evidence is already a stronger match than tolerant alignment."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    expected = core._build_host_object_inventory([before])
    alignment_calls = []

    def reject_tolerant_alignment(left, right):
        alignment_calls.append((left, right))
        return {"verified": False, "max_delta": None}

    monkeypatch.setattr(
        report_contract,
        "_freecad_shape_geometry_comparison",
        reject_tolerant_alignment,
    )

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is True
    assert alignment_calls == []
    assert crosscheck["geometry_comparisons"][0]["max_delta"] == 0.0


def test_fcstd_archive_evidence_hashes_unique_shape_entries(tmp_path) -> None:
    archive_path = tmp_path / "persisted.FCStd"
    _write_fcstd_archive(
        archive_path,
        {
            "Geometry001": ("Geometry001.Shape.brp", b"geometry-brep"),
            "Baseline001": ("Baseline001.Shape.brp", b"baseline-brep"),
        },
    )

    evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        ["Geometry001"],
    )

    assert evidence["verified"] is True
    assert evidence["method"] == "fcstd_brep_archive_sha256"
    assert evidence["fcstd_bytes"] == archive_path.stat().st_size
    assert evidence["shape_mapping_count"] == 2
    assert evidence["expected_shape_count"] == 1
    assert evidence["expected_nonempty_shape_count"] == 1
    assert evidence["expected_zero_ink_shape_count"] == 0
    assert evidence["shape_entries"] == [
        {
            "entity_id": "Geometry001",
            "entry_name": "Geometry001.Shape.brp",
            "sha256": hashlib.sha256(b"geometry-brep").hexdigest(),
            "bytes": len(b"geometry-brep"),
            "compression_method": zipfile.ZIP_DEFLATED,
            "crc32": evidence["shape_entries"][0]["crc32"],
            "zero_visible_ink": False,
        }
    ]
    assert evidence["evidence_digest"] == core._fcstd_archive_evidence_digest(
        evidence
    )


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("malformed_xml", "document_xml_malformed"),
        ("dtd", "document_xml_unsafe_declaration"),
        ("missing_document", "document_xml_missing"),
        ("duplicate_document", "duplicate_archive_entry"),
        ("duplicate_shape_entry", "duplicate_archive_entry"),
        ("traversal", "unsafe_archive_entry_name"),
        ("backslash", "unsafe_archive_entry_name"),
        ("missing_shape", "shape_entry_missing"),
        ("empty_shape", "shape_entry_empty"),
        ("duplicate_object", "duplicate_document_object_name"),
        ("shared_shape_entry", "duplicate_shape_entry_mapping"),
    ],
)
def test_fcstd_archive_evidence_rejects_malformed_or_ambiguous_inputs(
    tmp_path,
    case,
    reason,
) -> None:
    archive_path = tmp_path / (case + ".FCStd")
    valid_xml = (
        b"<Document><ObjectData><Object name='Geometry001'><Properties>"
        b"<Property name='Shape'><Part file='Geometry001.Shape.brp'/></Property>"
        b"</Properties></Object></ObjectData></Document>"
    )
    shape_entries = {"Geometry001": ("Geometry001.Shape.brp", b"brep")}
    extra_entries = []
    document_xml = valid_xml
    if case == "malformed_xml":
        document_xml = b"<Document><ObjectData>"
    elif case == "dtd":
        document_xml = b"<!DOCTYPE x [<!ENTITY y 'z'>]>" + valid_xml
    elif case == "missing_document":
        document_xml = None
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("Geometry001.Shape.brp", b"brep")
    elif case == "duplicate_document":
        extra_entries = [("Document.xml", valid_xml)]
    elif case == "duplicate_shape_entry":
        extra_entries = [("Geometry001.Shape.brp", b"second")]
    elif case == "traversal":
        document_xml = valid_xml.replace(
            b"Geometry001.Shape.brp", b"../Geometry001.Shape.brp"
        )
        shape_entries = {
            "Geometry001": ("../Geometry001.Shape.brp", b"brep")
        }
    elif case == "backslash":
        document_xml = valid_xml.replace(
            b"Geometry001.Shape.brp", b"folder\\Geometry001.Shape.brp"
        )
        shape_entries = {
            "Geometry001": ("folder\\Geometry001.Shape.brp", b"brep")
        }
    elif case == "missing_shape":
        shape_entries = {}
    elif case == "empty_shape":
        shape_entries = {"Geometry001": ("Geometry001.Shape.brp", b"")}
    elif case == "duplicate_object":
        document_xml = valid_xml.replace(
            b"</ObjectData>",
            b"<Object name='Geometry001'><Properties/></Object></ObjectData>",
        )
    elif case == "shared_shape_entry":
        document_xml = valid_xml.replace(
            b"</ObjectData>",
            b"<Object name='Geometry002'><Properties><Property name='Shape'>"
            b"<Part file='Geometry001.Shape.brp'/></Property></Properties></Object>"
            b"</ObjectData>",
        )

    if case != "missing_document":
        _write_fcstd_archive(
            archive_path,
            shape_entries,
            extra_entries=extra_entries,
            document_xml=document_xml,
        )

    evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        ["Geometry001"],
    )

    assert evidence["verified"] is False
    assert evidence["reason"] == reason


def test_fcstd_archive_evidence_crc_reads_shape_payload(tmp_path) -> None:
    archive_path = tmp_path / "crc.FCStd"
    document_xml = (
        b"<Document><ObjectData><Object name='Geometry001'><Properties>"
        b"<Property name='Shape'><Part file='Geometry001.Shape.brp'/></Property>"
        b"</Properties></Object></ObjectData></Document>"
    )
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("Document.xml", document_xml)
        archive.writestr("Geometry001.Shape.brp", b"stable-shape-bytes")
    raw = archive_path.read_bytes()
    assert raw.count(b"stable-shape-bytes") == 1
    archive_path.write_bytes(raw.replace(b"stable-shape-bytes", b"tamper-shape-bytes"))

    evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        ["Geometry001"],
    )

    assert evidence["verified"] is False
    assert evidence["reason"] == "shape_entry_crc_or_read_error"


def test_fcstd_archive_evidence_rejects_invalid_deflated_document_xml(
    tmp_path,
) -> None:
    archive_path = tmp_path / "invalid-document-deflate.FCStd"
    _write_fcstd_archive(
        archive_path,
        {"Geometry001": ("Geometry001.Shape.brp", b"persisted-brep")},
    )
    _corrupt_deflated_fcstd_member(archive_path, "Document.xml")

    evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        ["Geometry001"],
    )

    assert evidence["verified"] is False
    assert evidence["reason"] == "document_xml_crc_or_read_error"


def test_fcstd_archive_evidence_rejects_invalid_deflated_shape_member(
    tmp_path,
) -> None:
    archive_path = tmp_path / "invalid-shape-deflate.FCStd"
    _write_fcstd_archive(
        archive_path,
        {
            "Geometry001": (
                "Geometry001.Shape.brp",
                bytes(range(256)) * 1000,
            )
        },
    )
    _corrupt_deflated_fcstd_member(archive_path, "Geometry001.Shape.brp")

    evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        ["Geometry001"],
    )

    assert evidence["verified"] is False
    assert evidence["reason"] == "shape_entry_crc_or_read_error"


def test_archive_persistence_skips_live_brep_and_sampled_shape_passes(
    tmp_path,
) -> None:
    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    for host_object in (before, after):
        host_object.Shape.exportBrepToString = lambda: (_ for _ in ()).throw(
            AssertionError("production persistence must not serialize live BREP")
        )
        host_object.Shape.Edges[0].discretize = lambda **_kwargs: (
            (_ for _ in ()).throw(
                AssertionError("production persistence must not sample live edges")
            )
        )
    archive_path = tmp_path / "persisted.FCStd"
    _write_fcstd_archive(
        archive_path,
        {"Geometry001": ("Geometry001.Shape.brp", b"persisted-brep")},
    )
    expected = core._build_host_object_inventory(
        [before], shape_evidence_mode="cheap"
    )
    evidence = core._read_fcstd_shape_archive_evidence(
        archive_path, ["Geometry001"]
    )
    assert core._bind_fcstd_shape_archive_evidence(expected, evidence) is True

    crosscheck = core._crosscheck_host_object_inventory(
        expected,
        [after],
        shape_evidence_mode="cheap",
        archive_evidence=evidence,
    )

    assert crosscheck["verified"] is True
    assert crosscheck["geometry_comparisons"] == []
    assert expected["objects"][0]["content"]["shape_digest"] == evidence[
        "shape_entries"
    ][0]["sha256"]


def test_archive_persistence_does_not_treat_live_topology_as_persisted_authority(
    tmp_path,
) -> None:
    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    archive_path = tmp_path / "drift.FCStd"
    _write_fcstd_archive(
        archive_path,
        {"Geometry001": ("Geometry001.Shape.brp", b"persisted-brep")},
    )
    evidence = core._read_fcstd_shape_archive_evidence(
        archive_path, ["Geometry001"]
    )

    topology_drift = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    topology_drift.Shape.Edges.append(ShapeEdge())
    expected = core._build_host_object_inventory(
        [before], shape_evidence_mode="cheap"
    )
    assert core._crosscheck_host_object_inventory(
        expected,
        [topology_drift],
        shape_evidence_mode="cheap",
        archive_evidence=evidence,
    )["verified"] is True

    before.Shape.Length = 1.0
    metric_drift = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    metric_drift.Shape.Length = 2.0
    expected = core._build_host_object_inventory(
        [before], shape_evidence_mode="cheap"
    )
    assert core._crosscheck_host_object_inventory(
        expected,
        [metric_drift],
        shape_evidence_mode="cheap",
        archive_evidence=evidence,
    )["verified"] is True


def test_archive_persistence_rejects_3d_text_base_link_drift(tmp_path) -> None:
    expected_base = HostObject("CloneExpected", "Part::Feature")
    changed_base = HostObject("CloneChanged", "Part::Feature")
    before = HostObject(
        "Extrusion001",
        "Part::Extrusion",
        representation="3d_text",
        source_item_id="p1:text:0:3d",
        parent_source_item_id="p1:text:0",
        string="3D TEXT",
        base=expected_base,
    )
    after = HostObject(
        "Extrusion001",
        "Part::Extrusion",
        representation="3d_text",
        source_item_id="p1:text:0:3d",
        parent_source_item_id="p1:text:0",
        string="3D TEXT",
        base=changed_base,
    )
    archive_path = tmp_path / "base-drift.FCStd"
    _write_fcstd_archive(
        archive_path,
        {"Extrusion001": ("Extrusion001.Shape.brp", b"persisted-extrusion")},
    )
    evidence = core._read_fcstd_shape_archive_evidence(
        archive_path, ["Extrusion001"]
    )
    expected = core._build_host_object_inventory(
        [before], shape_evidence_mode="cheap"
    )

    crosscheck = core._crosscheck_host_object_inventory(
        expected,
        [after],
        shape_evidence_mode="cheap",
        archive_evidence=evidence,
    )

    assert crosscheck["verified"] is False
    assert crosscheck["mismatched_entities"][0]["entity_id"] == "Extrusion001"


def test_save_reopen_fails_before_open_when_archive_is_malformed(
    monkeypatch,
) -> None:
    geometry = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    geometry.Shape.exportBrepToString = lambda: (_ for _ in ()).throw(
        AssertionError("failure must not fall back to live BREP")
    )
    geometry.Shape.Edges[0].discretize = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("failure must not fall back to sampled geometry")
    )

    class Document:
        Name = "Original"

        @staticmethod
        def saveCopy(path):
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("Document.xml", b"<Document><ObjectData>")
            return True

    open_calls = []
    fake_freecad = type(
        "FakeFreeCAD",
        (),
        {
            "openDocument": staticmethod(
                lambda *_args: open_calls.append(_args)
            ),
            "closeDocument": staticmethod(lambda *_args: None),
            "setActiveDocument": staticmethod(lambda *_args: None),
        },
    )
    monkeypatch.setattr(core, "FreeCAD", fake_freecad)
    inventory = core._build_host_object_inventory(
        [geometry], shape_evidence_mode="cheap"
    )

    result = core._save_reopen_host_object_inventory(Document(), inventory)

    assert result["verified"] is False
    assert result["reason"] == "document_xml_malformed"
    assert open_calls == []
    assert result["phase_timings_ms"]["cheap_inventory_ms"] == 0.0


def test_save_reopen_rejects_fcstd_changed_during_open(monkeypatch) -> None:
    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )

    class Document:
        Name = "Original"

        @staticmethod
        def saveCopy(path):
            _write_fcstd_archive(
                Path(path),
                {"Geometry001": ("Geometry001.Shape.brp", b"persisted-brep")},
            )
            return True

    reopened = type("Reopened", (), {"Name": "Reopened", "Objects": [after]})()

    def tampering_open(path, *_args):
        with Path(path).open("ab") as stream:
            stream.write(b"changed-after-archive-hash")
        return reopened

    fake_freecad = type(
        "FakeFreeCAD",
        (),
        {
            "openDocument": staticmethod(tampering_open),
            "closeDocument": staticmethod(lambda *_args: None),
            "setActiveDocument": staticmethod(lambda *_args: None),
        },
    )
    monkeypatch.setattr(core, "FreeCAD", fake_freecad)
    inventory = core._build_host_object_inventory(
        [before], shape_evidence_mode="cheap"
    )

    result = core._save_reopen_host_object_inventory(Document(), inventory)

    assert result["verified"] is False
    assert result["reason"] == "fcstd_archive_changed_after_open"
    assert result["archive_unchanged_after_open"] is False


def test_import_evidence_builder_never_dereferences_live_shape(monkeypatch) -> None:
    class MetadataOnlyPart:
        Name = "Geometry001"
        TypeId = "Part::Feature"
        PDFRepresentation = "geometry"
        PDFSourceItemId = "p1:b0:l0:s0:geometry"
        PDFParentSourceItemId = "p1:b0:l0:s0"
        PDFSourceText = "SOURCE"

        @property
        def Shape(self):
            raise AssertionError("production inventory must not dereference Shape")

        @staticmethod
        def isDerivedFrom(_type_id):
            return False

    before = MetadataOnlyPart()
    observed = {}

    def capture_save_reopen(fc_doc, inventory, baseline_entity_names=None):
        observed["fc_doc"] = fc_doc
        observed["inventory"] = inventory
        observed["baseline"] = baseline_entity_names
        return {"verified": True}

    monkeypatch.setattr(
        core,
        "_save_reopen_host_object_inventory",
        capture_save_reopen,
    )
    document = object()

    inventory, save_reopen = core._build_persistence_host_evidence(
        document,
        [before],
        {"Baseline"},
    )

    content = inventory["objects"][0]["content"]
    assert content["shape_snapshot_method"] == "fcstd_archive_pending"
    assert "shape_topology_counts" not in content
    assert "shape_metrics" not in content
    assert "shape_bounds" not in content
    assert "shape_fingerprint_geometry" not in content
    assert "shape_brep_sha256" not in content
    assert save_reopen == {"verified": True}
    assert observed == {
        "fc_doc": document,
        "inventory": inventory,
        "baseline": {"Baseline"},
    }


def test_save_reopen_emits_compact_digest_references_without_object_copies(
    monkeypatch,
) -> None:
    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )

    class Document:
        Name = "Original"

        @staticmethod
        def saveCopy(path):
            _write_fcstd_archive(
                Path(path),
                {"Geometry001": ("Geometry001.Shape.brp", b"persisted-brep")},
            )
            return True

    reopened = type("Reopened", (), {"Name": "Reopened", "Objects": [after]})()
    fake_freecad = type(
        "FakeFreeCAD",
        (),
        {
            "openDocument": staticmethod(lambda *_args: reopened),
            "closeDocument": staticmethod(lambda *_args: None),
            "setActiveDocument": staticmethod(lambda *_args: None),
        },
    )
    monkeypatch.setattr(core, "FreeCAD", fake_freecad)
    inventory = core._build_host_object_inventory(
        [before],
        shape_evidence_mode="cheap",
    )

    result = core._save_reopen_host_object_inventory(Document(), inventory)

    assert result["verified"] is True
    assert result["schema"] == "bcs.freecad_save_reopen_inventory/1.0"
    assert result["inventory_digest"] == inventory["inventory_digest"]
    assert result["reopened_inventory_digest"] == inventory["inventory_digest"]
    assert result["shape_archive_evidence_digest"] == inventory[
        "shape_archive_evidence"
    ]["evidence_digest"]
    assert "expected_objects" not in result
    assert "actual_objects" not in result
    assert "geometry_comparisons" not in result
    assert "shape_archive_evidence" not in result
    public_inventory = core._public_report_value(inventory)
    public_result = core._public_report_value(result)
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_inventory,
        public_result,
    ) is True


def test_reopen_crosscheck_rejects_nonempty_shape_topology_drift() -> None:
    """Both shapes being nonempty does not prove the same persisted geometry."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    before.Shape.Edges = [object(), object(), object(), object()]
    after.Shape.Edges = [object()]
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is False
    assert crosscheck["mismatched_entities"]


def test_report_rejects_structural_shape_without_verified_fingerprint() -> None:
    """A valid-looking summary digest cannot replace canonical geometry proof."""

    glyph = HostObject(
        "Glyph001",
        "Part::Feature",
        representation="glyphs",
        source_item_id="p1:b0:l0:s0:g0",
        parent_source_item_id="p1:b0:l0:s0",
    )
    inventory = core._build_host_object_inventory([glyph])
    content = inventory["objects"][0]["content"]
    content["shape_fingerprint_verified"] = False
    content["shape_fingerprint_schema"] = ""

    verified = report_contract._freecad_host_inventory_verified(
        inventory,
        {"primitives": 0, "images": 0},
    )

    assert verified is False


def test_reopen_crosscheck_rejects_geometry_drift_with_equal_summary_counts() -> None:
    """Equal counts and bounds cannot certify different persisted contours."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )

    def vertex(x, y, z=0.0):
        return type("Vertex", (), {"Point": type("Point", (), {"x": x, "y": y, "z": z})()})()

    before.Shape.Vertexes = [
        vertex(0.0, 0.0),
        vertex(1.0, 0.0),
        vertex(1.0, 1.0),
        vertex(0.0, 1.0),
    ]
    after.Shape.Vertexes = [
        vertex(0.0, 0.0),
        vertex(1.0, 0.0),
        vertex(1.0, 1.0),
        vertex(0.25, 1.0),
    ]
    before.Shape.Edges = [object(), object(), object(), object()]
    after.Shape.Edges = [object(), object(), object(), object()]
    bound_box = type(
        "BoundBox",
        (),
        {
            "XMin": 0.0,
            "YMin": 0.0,
            "ZMin": 0.0,
            "XMax": 1.0,
            "YMax": 1.0,
            "ZMax": 0.0,
        },
    )
    before.Shape.BoundBox = bound_box
    after.Shape.BoundBox = bound_box
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is False


def test_reopen_crosscheck_rejects_curve_drift_with_equal_endpoints() -> None:
    """A changed curve interior cannot hide behind stable endpoint summaries."""

    before = HostObject(
        "Glyph001",
        "Part::Feature",
        representation="glyphs",
        source_item_id="p1:b0:l0:s0:g0",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Glyph001",
        "Part::Feature",
        representation="glyphs",
        source_item_id="p1:b0:l0:s0:g0",
        parent_source_item_id="p1:b0:l0:s0",
    )

    def point(x, y, z=0.0):
        return type("Point", (), {"x": x, "y": y, "z": z})()

    def vertex(x, y, z=0.0):
        return type("Vertex", (), {"Point": point(x, y, z)})()

    class SampledEdge:
        Length = 1.25

        def __init__(self, samples):
            self._samples = list(samples)
            self.Vertexes = [vertex(0.0, 0.0), vertex(1.0, 0.0)]

        def discretize(self, **_kwargs):
            return [point(x, y) for x, y in self._samples]

    before.Shape.Vertexes = [vertex(0.0, 0.0), vertex(1.0, 0.0)]
    after.Shape.Vertexes = [vertex(0.0, 0.0), vertex(1.0, 0.0)]
    before.Shape.Edges = [
        SampledEdge([(0.0, 0.0), (0.5, 0.25), (1.0, 0.0)])
    ]
    after.Shape.Edges = [
        SampledEdge([(0.0, 0.0), (0.5, -0.25), (1.0, 0.0)])
    ]
    bound_box = type(
        "BoundBox",
        (),
        {
            "XMin": 0.0,
            "YMin": -1.0,
            "ZMin": 0.0,
            "XMax": 1.0,
            "YMax": 1.0,
            "ZMax": 0.0,
        },
    )()
    before.Shape.BoundBox = bound_box
    after.Shape.BoundBox = bound_box
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is False


def test_reopen_crosscheck_tolerates_vertex_and_edge_enumeration_order() -> None:
    """Host enumeration order is not persisted geometry identity."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )

    def vertex(x, y, z=0.0):
        return type("Vertex", (), {"Point": type("Point", (), {"x": x, "y": y, "z": z})()})()

    first = [vertex(0.0, 0.0), vertex(1.0, 0.0), vertex(0.0, 1.0)]
    second = list(reversed(first))
    before.Shape.Vertexes = first
    after.Shape.Vertexes = second
    before.Shape.Edges = [ShapeEdge(), ShapeEdge()]
    after.Shape.Edges = list(reversed(before.Shape.Edges))
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is True


def test_reopen_crosscheck_uses_stable_topology_not_volatile_brep_bytes() -> None:
    """OCCT may reserialize an identical dynamic shape with different BREP bytes."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    before.Shape.exportBrepToString = lambda: "volatile serialization A"
    after.Shape.exportBrepToString = lambda: "volatile serialization B"
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is True


def test_reopen_crosscheck_tolerates_real_freecad_bound_box_roundoff() -> None:
    """A legitimate OCCT save/reopen must survive sub-nanometre bbox noise.

    FreeCAD 1.1.1 reproduced these exact values for a persisted native 3D Text
    ShapeString.  Its topology counts and shape metrics were unchanged.
    """

    before = HostObject(
        "ShapeString001",
        "Part::Part2DObjectPython",
        representation="3d_text",
        source_item_id="p1:text:0",
        string="3D",
    )
    after = HostObject(
        "ShapeString001",
        "Part::Part2DObjectPython",
        representation="3d_text",
        source_item_id="p1:text:0",
        string="3D",
    )
    before.Shape.BoundBox = type(
        "BoundBox",
        (),
        {
            "XMin": 9.99999999474e-08,
            "YMin": -0.163057324841,
            "ZMin": 0.0,
            "XMax": 15.4343950045,
            "YMax": 9.40297239915,
            "ZMax": 0.0,
        },
    )()
    after.Shape.BoundBox = type(
        "BoundBox",
        (),
        {
            "XMin": 9.99999996143e-08,
            "YMin": -0.163057324841,
            "ZMin": 0.0,
            "XMax": 15.4343950045,
            "YMax": 9.40297239915,
            "ZMax": 0.0,
        },
    )()
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is True


def test_reopen_crosscheck_tolerates_roundoff_across_quantization_boundary() -> None:
    """A tolerance cannot become discontinuous at an arbitrary hash bin edge."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    before.Shape.Vertexes = [
        ShapeVertex(1.000000049999, 0.0),
        ShapeVertex(2.0, 0.0),
    ]
    after.Shape.Vertexes = [
        ShapeVertex(1.000000050001, 0.0),
        ShapeVertex(2.0, 0.0),
    ]
    before.Shape.Edges = []
    after.Shape.Edges = []
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is True


def test_reopen_crosscheck_tolerates_closed_edge_roundoff_across_hash_boundary() -> None:
    """Closed-edge truth must use continuous tolerance, not rounded-bin equality."""

    class BoundaryEdge:
        Length = 2.0

        def __init__(self, seam_x: float) -> None:
            self._seam_x = seam_x

        def discretize(self, **_kwargs):
            return [
                ShapePoint(0.0, 0.0),
                ShapePoint(1.0, 0.0),
                ShapePoint(self._seam_x, 0.0),
            ]

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    before.Shape.Edges = [BoundaryEdge(0.000000049999)]
    after.Shape.Edges = [BoundaryEdge(0.000000050001)]
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is True


def test_structural_shape_fingerprint_rejects_an_unsampled_edge() -> None:
    """Every claimed edge needs observable geometry, not merely other vertices."""

    class UnsampledEdge:
        Length = 1.0
        Vertexes = [ShapeVertex(0.0, 0.0), ShapeVertex(1.0, 0.0)]

        def discretize(self, **_kwargs):
            raise RuntimeError("edge sampling unavailable")

    glyph = HostObject(
        "Glyph001",
        "Part::Feature",
        representation="glyphs",
        source_item_id="p1:b0:l0:s0:g0",
        parent_source_item_id="p1:b0:l0:s0",
    )
    glyph.Shape.Edges = [UnsampledEdge()]

    inventory = core._build_host_object_inventory([glyph])
    content = inventory["objects"][0]["content"]

    assert content["shape_fingerprint_edge_count"] == 1
    assert content["shape_fingerprint_sampled_edge_count"] == 0
    assert content["shape_fingerprint_verified"] is False


def test_structural_shape_fingerprint_rejects_a_one_point_edge_sample() -> None:
    """One point cannot observe a claimed nonzero-length edge."""

    class OnePointEdge:
        Length = 1.0
        Vertexes = [ShapeVertex(0.0, 0.0), ShapeVertex(1.0, 0.0)]

        def discretize(self, **_kwargs):
            return [ShapePoint(0.0, 0.0)]

    glyph = HostObject(
        "Glyph001",
        "Part::Feature",
        representation="glyphs",
        source_item_id="p1:b0:l0:s0:g0",
        parent_source_item_id="p1:b0:l0:s0",
    )
    glyph.Shape.Edges = [OnePointEdge()]

    content = core._build_host_object_inventory([glyph])["objects"][0]["content"]

    assert content["shape_fingerprint_sampled_edge_count"] == 0
    assert content["shape_fingerprint_verified"] is False


def test_structural_shape_fingerprint_allows_a_one_point_degenerate_edge() -> None:
    """A single point completely observes an actually zero-length edge."""

    class OnePointDegenerateEdge:
        Length = 0.0
        Vertexes = [ShapeVertex(0.0, 0.0)]

        def discretize(self, **_kwargs):
            return [ShapePoint(0.0, 0.0)]

    glyph = HostObject(
        "Glyph001",
        "Part::Feature",
        representation="glyphs",
        source_item_id="p1:b0:l0:s0:g0",
        parent_source_item_id="p1:b0:l0:s0",
    )
    glyph.Shape.Vertexes = [ShapeVertex(0.0, 0.0)]
    glyph.Shape.Edges = [OnePointDegenerateEdge()]

    content = core._build_host_object_inventory([glyph])["objects"][0]["content"]

    assert content["shape_fingerprint_sampled_edge_count"] == 1
    assert content["shape_fingerprint_verified"] is True


def test_reopen_crosscheck_does_not_treat_an_open_near_loop_as_cyclic() -> None:
    """Near-coincident raw endpoints do not override explicit open topology."""

    class ExplicitlyOpenEdge:
        Length = 2.0

        def __init__(self, x_coordinates) -> None:
            self._x_coordinates = x_coordinates
            self.Vertexes = [
                ShapeVertex(x_coordinates[0], 0.0),
                ShapeVertex(x_coordinates[-1], 0.0),
            ]

        def isClosed(self) -> bool:
            return False

        def discretize(self, **_kwargs):
            return [ShapePoint(x, 0.0) for x in self._x_coordinates]

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    before.Shape.Edges = [ExplicitlyOpenEdge((0.0, 4e-8, 1.0, 8e-8))]
    after.Shape.Edges = [ExplicitlyOpenEdge((4e-8, 1.0, 8e-8, 0.0))]
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is False


def test_reopen_crosscheck_honors_authoritative_closed_edge_topology() -> None:
    """A closed topological edge may persist with a different sampling seam."""

    class ExplicitlyClosedEdge:
        Length = 3.0

        def __init__(self, x_coordinates) -> None:
            self._x_coordinates = x_coordinates

        def isClosed(self) -> bool:
            return True

        def discretize(self, **_kwargs):
            return [ShapePoint(x, 0.0) for x in self._x_coordinates]

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    before.Shape.Edges = [ExplicitlyClosedEdge((0.0, 1.0, 2.0))]
    after.Shape.Edges = [ExplicitlyClosedEdge((1.0, 2.0, 0.0))]
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert crosscheck["verified"] is True


def test_shape_metric_comparison_uses_raw_continuous_tolerance() -> None:
    """Pre-rounded metrics must not hide a delta above the advertised tolerance."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    before.Shape.Area = 0.51e-7
    after.Shape.Area = 2.49e-7
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert after.Shape.Area - before.Shape.Area > 1e-7
    assert crosscheck["verified"] is False


def test_shape_bound_comparison_uses_raw_continuous_tolerance() -> None:
    """Bounds use the same one-time absolute comparison as shape metrics."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    before.Shape.BoundBox = type("BoundBox", (), {"XMin": 0.51e-7})()
    after.Shape.BoundBox = type("BoundBox", (), {"XMin": 2.49e-7})()
    expected = core._build_host_object_inventory([before])

    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    assert after.Shape.BoundBox.XMin - before.Shape.BoundBox.XMin > 1e-7
    assert crosscheck["verified"] is False


def test_public_shape_certificate_rejects_a_tampered_shape_digest() -> None:
    """A declared content digest remains part of the certificate's integrity."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    private_expected = core._build_host_object_inventory([before])
    public_expected = core._public_report_value(private_expected)
    public_crosscheck = core._public_report_value(
        core._crosscheck_host_object_inventory(private_expected, [after])
    )
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected, public_crosscheck
    ) is True

    tampered = json.loads(json.dumps(public_crosscheck))
    content = tampered["actual_objects"][0]["content"]
    content["shape_digest"] = "f" * 64

    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected, tampered
    ) is False


def test_public_shape_certificate_binds_both_retained_content_records() -> None:
    """The public certificate is independently checkable from retained evidence."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
    )
    private_expected = core._build_host_object_inventory([before])
    public_expected = core._public_report_value(private_expected)
    public_crosscheck = core._public_report_value(
        core._crosscheck_host_object_inventory(private_expected, [after])
    )
    certificate = public_crosscheck["geometry_comparisons"][0]
    expected_content = public_expected["objects"][0]["content"]
    actual_content = public_crosscheck["actual_objects"][0]["content"]

    def content_digest(content) -> str:
        return hashlib.sha256(
            json.dumps(
                content,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()

    assert certificate["expected_content_digest"] == content_digest(
        expected_content
    )
    assert certificate["actual_content_digest"] == content_digest(actual_content)


def test_unordered_alignment_does_not_fail_at_python_recursion_depth() -> None:
    """Ambiguous one-to-one shape matching must not recurse once per entity."""

    item_count = sys.getrecursionlimit() + 5
    items = list(range(item_count))

    def adjacent(left_index, right_index) -> bool:
        if left_index == item_count - 1:
            return right_index in (0, item_count - 1)
        return right_index in (left_index, left_index + 1)

    alignment = report_contract._freecad_unordered_alignment(
        items, items, adjacent
    )

    assert alignment is not None
    assert len(alignment) == item_count


def test_unordered_alignment_has_a_linear_fast_path_for_aligned_ambiguity() -> None:
    """Already aligned ambiguous input must not materialize every possible pair."""

    item_count = sys.getrecursionlimit() + 5
    items = list(range(item_count))
    predicate_calls = 0

    def ambiguous(_left_index, _right_index) -> bool:
        nonlocal predicate_calls
        predicate_calls += 1
        return True

    alignment = report_contract._freecad_unordered_alignment(
        items, items, ambiguous
    )

    assert alignment is not None
    assert len(alignment) == item_count
    assert predicate_calls <= item_count * 2


def test_unordered_alignment_repairs_a_greedy_choice_without_recursion() -> None:
    """The bounded fallback still finds a perfect one-to-one assignment."""

    def compatible(left_index, right_index) -> bool:
        if left_index == 0:
            return right_index in (0, 1)
        return right_index == 0

    alignment = report_contract._freecad_unordered_alignment(
        [0, 1], [0, 1], compatible
    )

    assert alignment == {0: 1, 1: 0}


def test_unordered_alignment_enforces_its_pair_budget_during_the_fast_path(
    monkeypatch,
) -> None:
    """A late greedy failure cannot spend beyond the fallback pair budget."""

    monkeypatch.setattr(
        report_contract,
        "_FREECAD_UNORDERED_FALLBACK_PAIR_LIMIT",
        4,
    )
    predicate_calls = 0

    def compatible(left_index, right_index) -> bool:
        nonlocal predicate_calls
        predicate_calls += 1
        return (left_index, right_index) in {(0, 2), (1, 3)}

    alignment = report_contract._freecad_unordered_alignment(
        [0, 1, 2, 3], [0, 1, 2, 3], compatible
    )

    assert alignment is None
    assert predicate_calls <= 4


def test_tolerated_shape_roundoff_emits_a_bound_public_certificate() -> None:
    """Raw samples stay transient while the report binds both observations."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    before.Shape.Vertexes[0] = ShapeVertex(1.000000049999, 0.0)
    after.Shape.Vertexes[0] = ShapeVertex(1.000000050001, 0.0)
    expected = core._build_host_object_inventory([before])
    crosscheck = core._crosscheck_host_object_inventory(expected, [after])

    public_expected = core._public_report_value(expected)
    public_crosscheck = core._public_report_value(crosscheck)
    encoded = json.dumps(
        {"inventory": public_expected, "save_reopen": public_crosscheck},
        sort_keys=True,
    )

    assert crosscheck["verified"] is True
    assert len(crosscheck["geometry_comparisons"]) == 1
    assert "_shape_comparison_geometry" not in encoded
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected, public_crosscheck
    ) is True

    tampered_delta = json.loads(json.dumps(public_crosscheck))
    tampered_delta["geometry_comparisons"][0]["max_delta"] = 0.0
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected, tampered_delta
    ) is False

    tampered_digest = json.loads(json.dumps(public_crosscheck))
    tampered_digest["actual_objects"][0]["content"][
        "shape_fingerprint_geometry"
    ]["sample_digest"] = "f" * 64
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected, tampered_digest
    ) is False

    tampered_sample_count = json.loads(json.dumps(public_crosscheck))
    tampered_sample_count["actual_objects"][0]["content"][
        "shape_fingerprint_geometry"
    ]["sampled_point_count"] += 1
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected, tampered_sample_count
    ) is False

    missing_certificate = json.loads(json.dumps(public_crosscheck))
    missing_certificate["geometry_comparisons"] = []
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected, missing_certificate
    ) is False


def test_public_inventory_does_not_serialize_transient_edge_samples() -> None:
    """Detailed live comparison data must not scale the report per edge sample."""

    geometry = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    geometry.Shape.Edges = [ShapeEdge() for _ in range(100)]
    inventory = core._build_host_object_inventory([geometry])

    public_inventory = core._public_report_value(inventory)
    raw_bytes = len(json.dumps(inventory, sort_keys=True).encode("utf-8"))
    public_json = json.dumps(public_inventory, sort_keys=True)

    assert "_shape_comparison_geometry" not in public_json
    assert len(public_json.encode("utf-8")) < 5_000
    assert raw_bytes > len(public_json.encode("utf-8")) * 5


def test_public_compact_fcstd_inventory_is_bound_and_tamper_evident(
    tmp_path,
) -> None:
    """One manifest binds the FCStd, XML, exact BREP, inventory, and reopen."""

    before = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    after = HostObject(
        "Geometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id="p1:b0:l0:s0:geometry",
        parent_source_item_id="p1:b0:l0:s0",
    )
    archive_path = tmp_path / "persisted.FCStd"
    _write_fcstd_archive(
        archive_path,
        {"Geometry001": ("Geometry001.Shape.brp", b"persisted exact BREP")},
    )
    private_expected = core._build_host_object_inventory(
        [before], shape_evidence_mode="cheap"
    )
    archive_evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        ["Geometry001"],
    )
    assert core._bind_fcstd_shape_archive_evidence(
        private_expected,
        archive_evidence,
    ) is True
    private_crosscheck = core._compact_crosscheck_host_object_inventory(
        private_expected,
        [after],
        archive_evidence,
    )
    private_crosscheck.update(
        {
            "method": "temporary_fcstd_save_copy_archive_reopen",
            "archive_unchanged_after_open": True,
            "phase_timings_ms": {
                "save_ms": 1.0,
                "archive_hash_ms": 2.0,
                "open_ms": 3.0,
                "cheap_inventory_ms": 4.0,
            },
        }
    )
    public_expected = core._public_report_value(private_expected)
    public_crosscheck = core._public_report_value(private_crosscheck)

    assert report_contract._freecad_host_inventory_verified(
        public_expected,
        {"primitives": 0, "images": 0},
    ) is True
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected,
        public_crosscheck,
    ) is True

    tampered_manifest = json.loads(json.dumps(public_expected))
    tampered_manifest["shape_archive_evidence"]["fcstd_sha256"] = "f" * 64
    assert report_contract._freecad_save_reopen_inventory_verified(
        tampered_manifest,
        public_crosscheck,
    ) is False

    tampered_inventory_digest = json.loads(json.dumps(public_crosscheck))
    tampered_inventory_digest["inventory_digest"] = "f" * 64
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected,
        tampered_inventory_digest,
    ) is False

    tampered_archive_digest = json.loads(json.dumps(public_crosscheck))
    tampered_archive_digest["shape_archive_evidence_digest"] = "f" * 64
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected,
        tampered_archive_digest,
    ) is False

    duplicated_payload = json.loads(json.dumps(public_crosscheck))
    duplicated_payload["actual_objects"] = public_expected["objects"]
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected,
        duplicated_payload,
    ) is False

    tampered_timing = json.loads(json.dumps(public_crosscheck))
    tampered_timing["phase_timings_ms"]["archive_hash_ms"] = -1.0
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected,
        tampered_timing,
    ) is False

    tampered_toc = json.loads(json.dumps(public_crosscheck))
    tampered_toc["archive_unchanged_after_open"] = False
    assert report_contract._freecad_save_reopen_inventory_verified(
        public_expected,
        tampered_toc,
    ) is False


def test_ready_rejects_unrecognized_delivered_count_bucket(tmp_path) -> None:
    """Unknown integer buckets must not disappear from exact accounting."""

    opts = _direct_label_opts()
    opts.text_delivered_counts["invented_host_type"] = 7
    _attach_inventory(opts, _label_objects())
    report_path = tmp_path / "unrecognized-delivered-count.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["actual_text_entity_types"] is False
    assert ready["ready"] is False


def test_ready_rejects_unowned_duplicate_text_object_in_reopened_inventory(
    tmp_path,
) -> None:
    """Every reopened requested-representation object needs one terminal owner."""

    opts = _direct_label_opts()
    objects = _label_objects() + [
        HostObject(
            "LabelDuplicate",
            "App::FeaturePython",
            representation="labels",
            source_item_id="p1:text:0",
            proxy_type="Label",
            text=["LABEL"],
            custom_text=["LABEL"],
        )
    ]
    _attach_inventory(opts, objects)
    report_path = tmp_path / "unowned-duplicate-label.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_ready_rejects_removed_entity_identity_claimed_by_two_sources(
    tmp_path,
) -> None:
    """Cleanup IDs are owned identities, not reusable aggregate counters."""

    opts = core.ImportOptions(
        import_mode="vector", import_text=True, text_mode="labels"
    )
    objects = [HostObject("PDF_Page_1", "App::DocumentObjectGroup")]
    for index in range(2):
        source_id = "p1:text:%d" % index
        entity_id = "Label%03d" % index
        opts.text_delivery_attempts.append(
            {
                "source_item_id": source_id,
                "requested_type": "labels",
                "attempted_type": "labels",
                "final_type": "labels",
                "outcome": "verified",
                "created_entity_ids": ["SharedTransient", entity_id],
                "delivery_entity_ids": [entity_id],
                "support_entity_ids": [],
                "removed_entity_ids": ["SharedTransient"],
                "cleanup_complete": True,
                "delivery_count": 1,
                "evidence": {
                    "host_entity_type": "App::FeaturePython",
                    "host_proxy_type": "Label",
                    "source_text": "LABEL-%d" % index,
                    "source_text_preserved": True,
                    "view_style_verified": True,
                    "label_marker_absent": True,
                },
            }
        )
        objects.append(
            HostObject(
                entity_id,
                "App::FeaturePython",
                representation="labels",
                source_item_id=source_id,
                proxy_type="Label",
                text=["LABEL-%d" % index],
                custom_text=["LABEL-%d" % index],
            )
        )
    opts.text_delivered_counts["native_label"] = 2
    _attach_inventory(opts, objects)
    report_path = tmp_path / "shared-removed-identity.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=2,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_ready_rejects_3d_text_that_omits_required_support_structure(
    tmp_path,
) -> None:
    """One annotated nonempty Part is not verified native 3D Text."""

    source_id = "p1:text:0"
    opts = core.ImportOptions(
        import_mode="vector", import_text=True, text_mode="3d_text"
    )
    opts.text_delivery_obligation_source_item_ids.append(source_id)
    opts.text_delivery_attempts.append(
        {
            "source_item_id": source_id,
            "requested_type": "3d_text",
            "attempted_type": "3d_text",
            "final_type": "3d_text",
            "outcome": "verified",
            "created_entity_ids": ["FlatPart001"],
            "delivery_entity_ids": ["FlatPart001"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 1,
            "evidence": {
                "source_text": "3D",
                "source_text_preserved": True,
                "solid_count": 1,
                "volume": 1.0,
                "view_style_verified": True,
            },
        }
    )
    opts.text_delivered_counts["native_3d_text"] = 1
    objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "FlatPart001",
            "Part::Feature",
            representation="3d_text",
            source_item_id=source_id,
        ),
    ]
    _attach_inventory(opts, objects)
    report_path = tmp_path / "3d-text-without-support.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is False
    assert ready["ready"] is False


def test_legitimate_3d_text_delivery_with_support_objects_remains_ready(
    tmp_path,
) -> None:
    """Strict ownership must retain the real ShapeString/support/extrusion form."""

    source_id = "p1:text:0"
    opts = core.ImportOptions(
        import_mode="vector", import_text=True, text_mode="3d_text"
    )
    font_evidence, font_properties = _visible_3d_font_delivery(tmp_path)
    opts.text_delivery_obligation_source_item_ids.append(source_id)
    opts.text_delivery_attempts.append(
        {
            "source_item_id": source_id,
            "requested_type": "3d_text",
            "attempted_type": "3d_text",
            "final_type": "3d_text",
            "outcome": "verified",
            "created_entity_ids": [
                "ShapeString001",
                "ScaleSupport001",
                "Extrusion001",
            ],
            "delivery_entity_ids": ["Extrusion001"],
            "support_entity_ids": ["ShapeString001", "ScaleSupport001"],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 1,
            "evidence": {
                "source_text": "3D",
                "source_text_preserved": True,
                "solid_count": 1,
                "volume": 1.0,
                "view_style_verified": True,
                **font_evidence,
            },
        }
    )
    opts.text_delivered_counts["native_3d_text"] = 1
    shape_string = HostObject(
        "ShapeString001",
        "Part::Part2DObjectPython",
        representation="3d_text",
        source_item_id=source_id,
        string="3D",
        font_delivery=font_properties,
    )
    scale_support = HostObject(
        "ScaleSupport001",
        "Part::Part2DObjectPython",
        representation="3d_text",
        source_item_id=source_id,
        string="3D",
        font_delivery=font_properties,
    )
    extrusion = HostObject(
        "Extrusion001",
        "Part::Extrusion",
        representation="3d_text",
        source_item_id=source_id,
        base=scale_support,
        font_delivery=font_properties,
    )
    objects = [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        shape_string,
        scale_support,
        extrusion,
    ]
    _attach_inventory(opts, objects)
    report_path = tmp_path / "valid-3d-text-structure.json"

    core.write_import_report(
        pdf_path=str(tmp_path / "drawing.pdf"),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["delivery_inventory_binding"] is True
    assert ready["ready"] is True


def test_compact_fcstd_archive_3d_text_delivery_binds_to_exact_shape_entries(
    tmp_path,
) -> None:
    """Compact BREP evidence must satisfy the same native 3D Text contract."""

    source_id = "p1:text:0"
    font_evidence, font_properties = _visible_3d_font_delivery(tmp_path)
    attempt = {
        "source_item_id": source_id,
        "requested_type": "3d_text",
        "attempted_type": "3d_text",
        "final_type": "3d_text",
        "outcome": "verified",
        "created_entity_ids": [
            "ShapeString001",
            "ScaleSupport001",
            "Extrusion001",
        ],
        "delivery_entity_ids": ["Extrusion001"],
        "support_entity_ids": ["ShapeString001", "ScaleSupport001"],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "delivery_count": 1,
        "evidence": {
            "source_text": "3D",
            "source_text_preserved": True,
            "solid_count": 1,
            "volume": 1.0,
            "view_style_verified": True,
            **font_evidence,
        },
    }
    opts = core.ImportOptions(
        import_mode="vector",
        import_text=True,
        text_mode="3d_text",
    )
    opts.text_delivery_obligation_source_item_ids.append(source_id)
    opts.text_delivery_attempts.append(attempt)
    shape_string = HostObject(
        "ShapeString001",
        "Part::Part2DObjectPython",
        representation="3d_text",
        source_item_id=source_id,
        string="3D",
        font_delivery=font_properties,
    )
    scale_support = HostObject(
        "ScaleSupport001",
        "Part::Part2DObjectPython",
        representation="3d_text",
        source_item_id=source_id,
        font_delivery=font_properties,
    )
    extrusion = HostObject(
        "Extrusion001",
        "Part::Extrusion",
        representation="3d_text",
        source_item_id=source_id,
        base=scale_support,
        font_delivery=font_properties,
    )
    inventory = core._build_host_object_inventory(
        [shape_string, scale_support, extrusion],
        shape_evidence_mode="cheap",
    )
    archive_path = tmp_path / "compact-3d-text.FCStd"
    _write_fcstd_archive(
        archive_path,
        {
            "ShapeString001": ("ShapeString001.Shape.brp", b"shape-string"),
            "ScaleSupport001": ("ScaleSupport001.Shape.brp", b"scaled-shape"),
            "Extrusion001": ("Extrusion001.Shape.brp", b"extruded-solid"),
        },
    )
    archive_evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        ["ShapeString001", "ScaleSupport001", "Extrusion001"],
    )
    assert core._bind_fcstd_shape_archive_evidence(
        inventory,
        archive_evidence,
    ) is True

    public_inventory = core._public_report_value(inventory)
    delivery = core._public_report_value(
        core._build_text_representation_delivery(opts, opts.text_delivery_attempts)
    )
    public_attempts = core._public_report_value(opts.text_delivery_attempts)
    assert report_contract._freecad_delivery_inventory_binding_verified(
        delivery,
        public_attempts,
        public_inventory,
        "a" * 64,
    ) is True

    tampered = json.loads(json.dumps(public_inventory))
    tampered["shape_archive_evidence"]["shape_entries"][0]["sha256"] = "f" * 64
    assert report_contract._freecad_delivery_inventory_binding_verified(
        delivery,
        public_attempts,
        tampered,
        "a" * 64,
    ) is False


def test_compact_fcstd_archive_zero_ink_3d_text_binds_empty_shape(
    tmp_path,
) -> None:
    """An exact empty BREP is the persisted shape proof for layout-only text."""

    source_id = "p1:b0:l0:s0"
    source_text = "   "
    pdf_sha256 = "a" * 64
    source_evidence = _zero_ink_source_evidence(
        source_id,
        source_text,
        pdf_sha256,
    )
    entity_id = "PDF_Empty_3D_Text"
    attempt = {
        "source_item_id": source_id,
        "requested_type": "3d_text",
        "attempted_type": "3d_text",
        "final_type": "3d_text",
        "outcome": "verified",
        "created_entity_ids": [entity_id],
        "delivery_entity_ids": [entity_id],
        "support_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "delivery_count": 1,
        "evidence": {
            "source_text": source_text,
            "source_text_preserved": True,
            "source_ink_evidence": source_evidence,
            "source_ink_evidence_persisted": True,
            "source_pdf_sha256": pdf_sha256,
            "source_page_number": 1,
            "source_font_identity": source_evidence["font_identity"],
            "source_font_asset_bindings": source_evidence["font_asset_bindings"],
            "source_glyph_id_sequence": source_evidence["glyph_id_sequence"],
            "physical_visibility": False,
            "vertex_count": 0,
            "edge_count": 0,
            "face_count": 0,
            "solid_count": 0,
            "volume": 0.0,
            "shape_is_null": True,
            "zero_visible_ink_verified": True,
        },
    }
    opts = core.ImportOptions(
        import_mode="vector",
        import_text=True,
        text_mode="3d_text",
    )
    opts.text_delivery_obligation_source_item_ids.append(source_id)
    opts.text_delivery_attempts.append(attempt)
    inventory = core._build_host_object_inventory(
        [
            HostObject(
                entity_id,
                "Part::Feature",
                representation="3d_text",
                source_item_id=source_id,
                shape_nonempty=False,
                string=source_text,
                text_visibility=False,
                source_ink_classification="zero_visible_ink",
                source_ink_digest=source_evidence["evidence_sha256"],
            )
        ],
        shape_evidence_mode="cheap",
    )
    archive_path = tmp_path / "compact-zero-ink-3d-text.FCStd"
    _write_fcstd_archive(
        archive_path,
        {entity_id: (entity_id + ".Shape.brp", b"")},
    )
    archive_evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        [],
        expected_zero_ink_shape_entity_ids=[entity_id],
    )
    assert core._bind_fcstd_shape_archive_evidence(
        inventory,
        archive_evidence,
    ) is True

    assert report_contract._freecad_delivery_inventory_binding_verified(
        core._public_report_value(
            core._build_text_representation_delivery(opts, opts.text_delivery_attempts)
        ),
        core._public_report_value(opts.text_delivery_attempts),
        core._public_report_value(inventory),
        pdf_sha256,
    ) is True


def test_zero_ink_inventory_binds_exact_source_text_and_physical_evidence() -> None:
    source_id = "p1:b0:l0:s0"
    source_text = "   "
    evidence = _zero_ink_source_evidence(source_id, source_text)
    before = HostObject(
        "EmptyGeometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id=source_id,
        shape_nonempty=False,
        source_ink_classification="zero_visible_ink",
        source_ink_digest=evidence["evidence_sha256"],
        source_text_property=source_text,
    )
    inventory = core._build_host_object_inventory([before])
    content = inventory["objects"][0]["content"]

    assert content["source_text"] == source_text
    assert content["source_ink_classification"] == "zero_visible_ink"
    assert content["source_ink_evidence_sha256"] == evidence["evidence_sha256"]

    after = HostObject(
        "EmptyGeometry001",
        "Part::Feature",
        representation="geometry",
        source_item_id=source_id,
        shape_nonempty=False,
        source_ink_classification="zero_visible_ink",
        source_ink_digest="f" * 64,
        source_text_property=source_text,
    )
    assert core._crosscheck_host_object_inventory(inventory, [after])["verified"] is False


def test_zero_ink_cheap_inventory_is_bound_to_exact_empty_fcstd_shape(
    tmp_path,
) -> None:
    """All-space 3D text remains valid when its persisted shape is exactly empty."""

    source_id = "p1:b0:l0:s0"
    source_text = "   "
    evidence = _zero_ink_source_evidence(source_id, source_text)
    entity_id = "PDF_Empty_3D_Text"
    host_object = HostObject(
        entity_id,
        "Part::Feature",
        representation="3d_text",
        source_item_id=source_id,
        shape_nonempty=False,
        string=source_text,
        text_visibility=False,
        source_ink_classification="zero_visible_ink",
        source_ink_digest=evidence["evidence_sha256"],
    )
    inventory = core._build_host_object_inventory(
        [host_object],
        shape_evidence_mode="cheap",
    )
    archive_path = tmp_path / "zero-ink.FCStd"
    _write_fcstd_archive(
        archive_path,
        {entity_id: (entity_id + ".Shape.brp", b"")},
    )

    archive_evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        [],
        expected_zero_ink_shape_entity_ids=[entity_id],
    )

    assert archive_evidence["verified"] is True
    assert archive_evidence["shape_entries"] == [
        {
            "entity_id": entity_id,
            "entry_name": entity_id + ".Shape.brp",
            "sha256": hashlib.sha256(b"").hexdigest(),
            "bytes": 0,
            "compression_method": zipfile.ZIP_DEFLATED,
            "crc32": 0,
            "zero_visible_ink": True,
        }
    ]
    assert core._bind_fcstd_shape_archive_evidence(
        inventory,
        archive_evidence,
    ) is True
    assert report_contract._freecad_host_inventory_verified(
        core._public_report_value(inventory),
        {"primitives": 0, "images": 0},
    ) is True


def test_zero_ink_cheap_inventory_rejects_nonempty_or_mismatched_archive_entry(
    tmp_path,
) -> None:
    source_id = "p1:b0:l0:s0"
    source_text = " "
    source_evidence = _zero_ink_source_evidence(source_id, source_text)
    entity_id = "PDF_Empty_3D_Text"
    inventory = core._build_host_object_inventory(
        [
            HostObject(
                entity_id,
                "Part::Feature",
                representation="3d_text",
                source_item_id=source_id,
                shape_nonempty=False,
                string=source_text,
                source_ink_classification="zero_visible_ink",
                source_ink_digest=source_evidence["evidence_sha256"],
            )
        ],
        shape_evidence_mode="cheap",
    )
    archive_path = tmp_path / "not-empty.FCStd"
    _write_fcstd_archive(
        archive_path,
        {entity_id: (entity_id + ".Shape.brp", b"unexpected geometry")},
    )
    archive_evidence = core._read_fcstd_shape_archive_evidence(
        archive_path,
        [],
        expected_zero_ink_shape_entity_ids=[entity_id],
    )
    assert archive_evidence["verified"] is False
    assert archive_evidence["reason"] == "zero_ink_shape_entry_nonempty"

    empty_path = tmp_path / "empty.FCStd"
    _write_fcstd_archive(
        empty_path,
        {entity_id: (entity_id + ".Shape.brp", b"")},
    )
    archive_evidence = core._read_fcstd_shape_archive_evidence(
        empty_path,
        [],
        expected_zero_ink_shape_entity_ids=[entity_id],
    )
    assert core._bind_fcstd_shape_archive_evidence(inventory, archive_evidence) is True
    public_inventory = core._public_report_value(inventory)
    public_inventory["objects"][0]["content"]["shape_archive_entry"] = (
        "different.Shape.brp"
    )
    assert report_contract._freecad_host_inventory_verified(
        public_inventory,
        {"primitives": 0, "images": 0},
    ) is False


def test_synthetic_source_character_cannot_be_relabelled_as_visible_ink() -> None:
    source_id = "p1:b0:l0:s0"
    source_text = " "
    evidence = _zero_ink_source_evidence(source_id, source_text)
    evidence["characters"][0]["authority"] = (
        "pymupdf_rawdict_synthetic_character"
    )
    evidence["characters"][0]["synthetic"] = True
    evidence["characters"][0]["glyph_id"] = None
    evidence["glyph_id_sequence"] = [None]
    evidence["classification"] = "visible_ink"
    evidence["characters"][0]["zero_visible_ink"] = False
    evidence["evidence_sha256"] = core._source_ink_evidence_digest(evidence)

    assert report_contract._freecad_source_ink_evidence_verified(
        evidence,
        source_id,
        source_text,
        expected_pdf_sha256=evidence["pdf_sha256"],
        expected_page_number=evidence["page_number"],
        expected_font_identity=evidence["font_identity"],
        expected_font_asset_bindings=evidence["font_asset_bindings"],
        expected_glyph_id_sequence=evidence["glyph_id_sequence"],
    ) is False


@pytest.mark.parametrize("representation", ["labels", "text", "3d_text", "glyphs", "geometry"])
def test_physically_zero_ink_requested_representation_is_report_ready(
    tmp_path,
    representation: str,
) -> None:
    source_id = "p1:b0:l0:s0"
    source_text = "   "
    pdf_path = tmp_path / "drawing.pdf"
    _write_minimal_pdf(pdf_path)
    source_evidence = _zero_ink_source_evidence(
        source_id,
        source_text,
        hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
    )
    digest = source_evidence["evidence_sha256"]
    entity_id = {
        "labels": "Label001",
        "text": "Text001",
        "3d_text": "ShapeString001",
        "glyphs": "EmptyGlyph001",
        "geometry": "EmptyGeometry001",
    }[representation]
    type_id = {
        "labels": "App::FeaturePython",
        "text": "App::FeaturePython",
        "3d_text": "Part::Part2DObjectPython",
        "glyphs": "Part::Feature",
        "geometry": "Part::Feature",
    }[representation]
    terminal_evidence = {
        "source_text": source_text,
        "source_text_preserved": True,
        "source_ink_evidence": source_evidence,
        "source_ink_evidence_persisted": True,
        "source_pdf_sha256": source_evidence["pdf_sha256"],
        "source_page_number": source_evidence["page_number"],
        "source_font_identity": source_evidence["font_identity"],
        "source_font_asset_bindings": source_evidence["font_asset_bindings"],
            "source_glyph_id_sequence": source_evidence["glyph_id_sequence"],
            "physical_visibility": False,
    }
    if representation in {"labels", "text"}:
        terminal_evidence.update(
            {
                "host_entity_type": type_id,
                "host_proxy_type": "Label" if representation == "labels" else "Text",
                "view_style_verified": True,
            }
        )
        if representation == "labels":
            terminal_evidence["label_marker_absent"] = True
    else:
        terminal_evidence.update(
            {
                "vertex_count": 0,
                "edge_count": 0,
                "face_count": 0,
                "solid_count": 0,
                "volume": 0.0,
                "shape_is_null": True,
                "zero_visible_ink_verified": True,
            }
        )

    opts = core.ImportOptions(
        import_mode="vector",
        import_text=True,
        text_mode=representation,
    )
    opts.text_delivery_obligation_source_item_ids.append(source_id)
    opts.text_delivery_attempts.append(
        {
            "source_item_id": source_id,
            "requested_type": representation,
            "attempted_type": representation,
            "final_type": representation,
            "outcome": "verified",
            "created_entity_ids": [entity_id],
            "delivery_entity_ids": [entity_id],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "delivery_count": 1,
            "evidence": terminal_evidence,
        }
    )
    opts.text_delivered_counts[
        {
            "labels": "native_label",
            "text": "native_text",
            "3d_text": "native_3d_text",
            "glyphs": "outline_curve_or_mesh",
            "geometry": "raw_geometry_edges",
        }[representation]
    ] = 1
    host_object = HostObject(
        entity_id,
        type_id,
        representation=representation,
        source_item_id=source_id,
        shape_nonempty=False,
        proxy_type=(
            "Label" if representation == "labels" else "Text" if representation == "text" else ""
        ),
        text=[source_text] if representation in {"labels", "text"} else None,
        custom_text=[source_text] if representation == "labels" else None,
        string=source_text if representation == "3d_text" else None,
        view_style_verified=representation in {"labels", "text"},
        text_visibility=(
            False if representation in {"labels", "text", "3d_text"} else None
        ),
        source_ink_classification="zero_visible_ink",
        source_ink_digest=digest,
        source_text_property=(
            source_text if representation in {"glyphs", "geometry"} else None
        ),
    )
    _attach_inventory(
        opts,
        [HostObject("PDF_Page_1", "App::DocumentObjectGroup"), host_object],
    )
    report_path = tmp_path / (representation + "-zero-ink.json")

    core.write_import_report(
        pdf_path=str(pdf_path),
        output_path=str(report_path),
        opts=opts,
        pages_imported=1,
        total_pages=1,
        text_count=1,
    )

    ready = json.loads(report_path.read_text(encoding="utf-8"))["extra"][
        "import_contract_ready"
    ]
    assert ready["checks"]["host_object_inventory"] is True
    assert ready["checks"]["save_reopen_inventory"] is True
    assert ready["checks"]["delivery_inventory_binding"] is True
    assert ready["ready"] is True
