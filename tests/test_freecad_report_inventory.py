"""Truthful FreeCAD host-object accounting and readiness contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402


class HostObject:
    def __init__(
        self,
        name: str,
        type_id: str,
        *,
        representation: str = "",
        source_item_id: str = "",
        shape_nonempty: bool = True,
    ) -> None:
        self.Name = name
        self.TypeId = type_id
        self.PDFRepresentation = representation
        self.PDFSourceItemId = source_item_id
        if type_id.startswith("Part::"):
            self.Shape = Shape(shape_nonempty)

    def isDerivedFrom(self, type_id: str) -> bool:
        return type_id == "App::DocumentObjectGroup" and self.TypeId.startswith(
            "App::DocumentObjectGroup"
        )


class Shape:
    def __init__(self, nonempty: bool) -> None:
        self._nonempty = nonempty

    def isNull(self) -> bool:
        return not self._nonempty


def _aws_objects():
    return [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "PageRaster001",
            "Image::ImagePlane",
            representation="raster",
            source_item_id="p1:page",
        ),
    ]


def _aws_fallback_opts() -> core.ImportOptions:
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
            "evidence": {"visible_source_text_found": False},
            "attempted_types": ladder[: index + 1],
            "attempted_source_results": [
                {
                    "source": "pymupdf_text_dictionary",
                    "outcome": "not_found",
                    "pdf_sha256": pdf_sha256,
                    "page_number": 1,
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
            "evidence": {"host_entity_type": "Image::ImagePlane"},
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
        "evidence": {"visible_source_text_found": False},
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
            "evidence": {"host_entity_type": "App::FeaturePython"},
        }
    )
    if delivered_count:
        opts.text_delivered_counts["native_label"] = 1
    return opts


def _label_objects():
    return [
        HostObject("PDF_Page_1", "App::DocumentObjectGroup"),
        HostObject(
            "Label001",
            "App::FeaturePython",
            representation="labels",
            source_item_id="p1:text:0",
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


def test_inventory_counts_groups_images_and_only_nonempty_geometry() -> None:
    objects = _aws_objects() + [
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
    opts = _aws_fallback_opts()
    inventory = core._build_host_object_inventory(_aws_objects())
    opts._report_extra = {
        "actual_host_object_inventory": inventory,
        "save_reopen_inventory": core._crosscheck_host_object_inventory(
            inventory, _aws_objects()
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
                "evidence": {"host_entity_type": "App::FeaturePython"},
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
