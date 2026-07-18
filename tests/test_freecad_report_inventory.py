"""Truthful FreeCAD host-object accounting and readiness contracts."""

from __future__ import annotations

import json
import hashlib
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore import import_report as report_contract  # noqa: E402


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
                },
            )()
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


def _aws_objects(image_file: str = "aws-page-1.png"):
    return [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "PageRaster001",
            "Image::ImagePlane",
            representation="raster",
            source_item_id="p1:page",
            image_file=image_file,
        ),
    ]


def _aws_fallback_opts(raster_path: str = "") -> core.ImportOptions:
    opts = core.ImportOptions(import_mode="auto", import_text=True, text_mode="labels")
    source_id = "p1:page"
    pdf_sha256 = "a" * 64
    proofs = []
    ladder = list(core.TEXT_ITEM_FALLBACK_LADDERS["labels"])
    for index, attempted_type in enumerate(ladder[:-1]):
        proof = {
            "item_specific_proven_impossible": True,
            "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
            "pdf_sha256": pdf_sha256,
            "page_number": 1,
            "source_item_id": source_id,
            "requested_type": "labels",
            "attempted_type": attempted_type,
            "reason_code": "no_source_text_items",
            "evidence": {
                "text_dictionary_present": True,
                "canonical_source_item_count": 0,
                "visible_source_text_found": False,
            },
            "attempted_types": ladder[: index + 1],
            "attempted_source_results": [
                {
                    "source": "pymupdf_text_dictionary",
                    "outcome": "not_found",
                    "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
                    "pdf_sha256": pdf_sha256,
                    "page_number": 1,
                    "source_item_id": source_id,
                    "source_item_ids": [],
                    "canonical_source_item_count": 0,
                    "visible_source_text_found": False,
                }
            ],
            "attempted_sources_complete": True,
            "created_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
        }
        proofs.append(proof)
        opts.text_delivery_attempts.append(
            {
                "source_item_id": source_id,
                "requested_type": "labels",
                "attempted_type": attempted_type,
                "final_type": None,
                "outcome": "proven_impossible",
                "reason_code": "no_source_text_items",
                "created_entity_ids": [],
                "removed_entity_ids": [],
                "cleanup_complete": True,
                "proof": proof,
            }
        )
    opts.text_delivery_attempts.append(
        {
            "source_item_id": source_id,
            "requested_type": "labels",
            "attempted_type": "raster",
            "final_type": "raster",
            "outcome": "verified",
            "created_entity_ids": ["PageRaster001"],
            "delivery_entity_ids": ["PageRaster001"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "attempted_types": ladder,
            "proof_chain": proofs,
            "evidence": {
                "host_entity_type": "Image::ImagePlane",
                **(
                    {
                        "source_asset_sha256": hashlib.sha256(
                            Path(raster_path).read_bytes()
                        ).hexdigest(),
                        "raster_content_verified": True,
                    }
                    if raster_path and Path(raster_path).is_file()
                    else {}
                ),
            },
        }
    )
    fallback_proof = {
        "item_specific_proven_impossible": True,
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": pdf_sha256,
        "page_number": 1,
        "source_item_id": source_id,
        "requested_type": "labels",
        "attempted_type": ladder[-2],
        "reason_code": "no_source_text_items",
        "evidence": {
            "text_dictionary_present": True,
            "canonical_source_item_count": 0,
            "visible_source_text_found": False,
        },
        "attempted_types": ladder,
        "attempted_source_results": proofs[0]["attempted_source_results"],
        "attempted_sources_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "proof_chain": proofs,
        "transition_chain": [
            {"from": left, "to": right}
            for left, right in zip(ladder, ladder[1:])
        ],
    }
    opts.text_mode_fallbacks.append(
        {
            "requested": "labels",
            "delivered": "raster",
            "reason": "no_source_text_items",
            "count": 1,
            "source_item_ids": [source_id],
            "proof": fallback_proof,
        }
    )
    opts.text_delivered_counts["raster_text_patch"] = 1
    return opts


def _direct_label_opts(*, delivered_count: bool = True) -> core.ImportOptions:
    opts = core.ImportOptions(import_mode="vector", import_text=True, text_mode="labels")
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
    objects = _aws_objects(str(raster_path))
    opts = _aws_fallback_opts(str(raster_path))
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
        pdf_path=str(tmp_path / "AWSWeldSymbolchart.pdf"),
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
    assert report["extra"]["import_contract_ready"]["ready"] is True


def test_non_raster_request_with_zero_source_and_delivery_evidence_is_not_ready(
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
    assert extra["text_representation_delivery"]["required"] is True
    assert extra["text_representation_delivery"]["verified"] is False
    assert extra["import_contract_ready"]["checks"]["text_delivery"] is False
    assert extra["import_contract_ready"]["ready"] is False


def test_generic_failure_cannot_be_hidden_before_a_verified_fallback() -> None:
    opts = _aws_fallback_opts()
    opts.text_delivery_attempts[0]["outcome"] = "failed"

    delivery = core._build_text_representation_delivery(
        opts,
        opts.text_delivery_attempts,
    )

    assert delivery["verified"] is False
    assert "p1:page_attempt_0_not_proven_impossible" in delivery["invalid_reasons"]


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
    assert "p1:text:0_terminal_type_drift" in delivery["invalid_reasons"]


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
    assert "p1:page_attempt_0_cleanup_entity_mismatch" in delivery["invalid_reasons"]


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


def test_valid_mixed_item_fallback_is_not_a_representation_violation(tmp_path) -> None:
    opts = _direct_label_opts()
    source_id = "p1:text:1"
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
    assert extra["import_contract_ready"]["ready"] is True


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
        for source_result in proof["attempted_source_results"]:
            source_result["page_number"] = proof_page
    fallback_proof = opts.text_mode_fallbacks[0]["proof"]
    fallback_proof["page_number"] = proof_page
    for source_result in fallback_proof["attempted_source_results"]:
        source_result["page_number"] = proof_page
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

    with pytest.raises(ValueError, match="source text"):
        core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256="a" * 64,
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
    before.Shape.Edges = [object(), object()]
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
    )
    scale_support = HostObject(
        "ScaleSupport001",
        "Part::Part2DObjectPython",
        representation="3d_text",
        source_item_id=source_id,
        string="3D",
    )
    extrusion = HostObject(
        "Extrusion001",
        "Part::Extrusion",
        representation="3d_text",
        source_item_id=source_id,
        base=scale_support,
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
