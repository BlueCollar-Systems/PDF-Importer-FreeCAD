from __future__ import annotations

import ast
import inspect
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402


class _Object:
    def __init__(self, name: str, type_id: str, representation: str | None = None):
        self.Name = name
        self.TypeId = type_id
        if representation is not None:
            self.PDFRepresentation = representation


class _Document:
    _FULL = object()

    def __init__(self, objects, *, reject_targeted: bool = False):
        self.Objects = list(objects)
        self.reject_targeted = reject_targeted
        self.recompute_calls = []

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def recompute(self, targets=_FULL):
        if targets is not self._FULL and self.reject_targeted:
            raise TypeError("targeted recompute is unavailable")
        self.recompute_calls.append(
            None if targets is self._FULL else [obj.Name for obj in targets]
        )


def _opts(**kwargs):
    opts = core.ImportOptions(**kwargs)
    core._reset_import_run_state(opts)
    return opts


def test_import_pdf_uses_one_proof_finalizer_and_no_direct_document_recompute():
    tree = ast.parse(inspect.getsource(core.import_pdf))
    helper_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_finalize_import_recompute"
    ]
    direct_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "fc_doc"
        and node.func.attr == "recompute"
    ]

    assert len(helper_calls) == 1
    assert direct_calls == []


def test_semantic_model_creation_precedes_final_recompute_policy():
    source = inspect.getsource(core.import_pdf)

    assert source.index("_create_semantic_model3d_members(") < source.index(
        "_finalize_import_recompute("
    )


def test_property_and_shape_complete_import_skips_final_document_recompute():
    existing = _Object("Existing", "Part::Feature")
    label = _Object("PDF_Label", "App::FeaturePython", "labels")
    wire = _Object("Wire", "Part::Feature")
    document = _Document([existing, label, wire])
    opts = _opts(text_mode="labels", model3d_mode="off")
    opts.text_delivered_counts["native_label"] = 1

    telemetry = core._finalize_import_recompute(
        document,
        opts,
        baseline_object_ids={id(existing)},
        baseline_object_names={existing.Name},
    )

    assert document.recompute_calls == []
    assert telemetry["schema"] == "bc.freecad.final-recompute.v1"
    assert telemetry["strategy"] == "skipped"
    assert telemetry["reason"] == "property_or_shape_complete"
    assert telemetry["post_baseline_object_count"] == 2
    assert telemetry["target_entity_ids"] == []
    assert telemetry["fallback_full"] is False
    assert opts._report_extra["final_recompute"] == telemetry
    assert opts.phase_timings_ms["final_recompute_ms"] >= 0.0


def test_parametric_3d_text_recomputes_only_exact_new_ledger_objects():
    existing = _Object("Existing", "Part::Feature")
    shape_string = _Object("ShapeString", "Part::Part2DObjectPython", "3d_text")
    calibrated = _Object("Clone2D", "Part::Feature", "3d_text")
    extrusion = _Object("PDF_3D_Text", "Part::Extrusion", "3d_text")
    document = _Document([existing, shape_string, calibrated, extrusion])
    opts = _opts(text_mode="3d_text", model3d_mode="off")
    opts.text_delivery_attempts.append(
        {
            "source_item_id": "p1:b0:l0:s0",
            "requested_type": "3d_text",
            "attempted_type": "3d_text",
            "final_type": "3d_text",
            "outcome": "verified",
            "created_entity_ids": ["ShapeString", "Clone2D", "PDF_3D_Text"],
            "delivery_entity_ids": ["PDF_3D_Text"],
            "support_entity_ids": ["ShapeString", "Clone2D"],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {"implementation": "parametric_shapestring_fallback_v1"},
        }
    )

    telemetry = core._finalize_import_recompute(
        document,
        opts,
        baseline_object_ids={id(existing)},
        baseline_object_names={existing.Name},
    )

    assert document.recompute_calls == [["ShapeString", "Clone2D", "PDF_3D_Text"]]
    assert telemetry["strategy"] == "targeted"
    assert telemetry["reason"] == "dependent_3d_text_objects"
    assert telemetry["target_entity_ids"] == [
        "ShapeString",
        "Clone2D",
        "PDF_3D_Text",
    ]
    assert telemetry["fallback_full"] is False


def test_shape_complete_compound_3d_text_skips_final_recompute():
    compound = _Object("PDF_3D_Text", "Part::Feature", "3d_text")
    document = _Document([compound])
    opts = _opts(text_mode="3d_text", model3d_mode="off")
    opts.text_delivery_attempts.append(
        {
            "attempted_type": "3d_text",
            "final_type": "3d_text",
            "outcome": "verified",
            "delivery_entity_ids": ["PDF_3D_Text"],
            "support_entity_ids": [],
            "removed_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {"implementation": "exact_glyph_solid_compound_v1"},
        }
    )

    telemetry = core._finalize_import_recompute(
        document,
        opts,
        baseline_object_ids=set(),
        baseline_object_names=set(),
    )

    assert document.recompute_calls == []
    assert telemetry["strategy"] == "skipped"
    assert telemetry["reason"] == "property_or_shape_complete"
    assert telemetry["target_entity_ids"] == []


def test_unprovable_3d_target_fails_safe_to_full_document_recompute():
    existing = _Object("Existing", "Part::Feature")
    extrusion = _Object("PDF_3D_Text", "Part::Extrusion", "3d_text")
    document = _Document([existing, extrusion])
    opts = _opts(text_mode="3d_text", model3d_mode="off")
    opts.text_delivery_attempts.append(
        {
            "attempted_type": "3d_text",
            "final_type": "3d_text",
            "outcome": "verified",
            "delivery_entity_ids": ["PDF_3D_Text"],
            "support_entity_ids": ["MissingShapeString"],
            "cleanup_complete": True,
            "evidence": {"implementation": "parametric_shapestring_fallback_v1"},
        }
    )

    telemetry = core._finalize_import_recompute(
        document,
        opts,
        baseline_object_ids={id(existing)},
        baseline_object_names={existing.Name},
    )

    assert document.recompute_calls == [None]
    assert telemetry["strategy"] == "full"
    assert telemetry["reason"] == "unprovable_3d_text_targets"
    assert telemetry["fallback_full"] is True
    assert telemetry["unresolved_target_entity_ids"] == ["MissingShapeString"]


def test_targeted_recompute_api_failure_falls_back_to_full_document_recompute():
    extrusion = _Object("PDF_3D_Text", "Part::Extrusion", "3d_text")
    document = _Document([extrusion], reject_targeted=True)
    opts = _opts(text_mode="3d_text", model3d_mode="off")
    opts.text_delivery_attempts.append(
        {
            "attempted_type": "3d_text",
            "final_type": "3d_text",
            "outcome": "verified",
            "delivery_entity_ids": ["PDF_3D_Text"],
            "support_entity_ids": [],
            "cleanup_complete": True,
            "evidence": {"implementation": "parametric_shapestring_fallback_v1"},
        }
    )

    telemetry = core._finalize_import_recompute(
        document,
        opts,
        baseline_object_ids=set(),
        baseline_object_names=set(),
    )

    assert document.recompute_calls == [None]
    assert telemetry["strategy"] == "full"
    assert telemetry["reason"] == "targeted_recompute_unavailable"
    assert telemetry["fallback_full"] is True
    assert telemetry["targeted_error"].startswith("TypeError:")


def test_created_model3d_objects_require_full_document_recompute():
    solid = _Object("PDF_3D_Solid", "Part::Feature")
    document = _Document([solid])
    opts = _opts(model3d_mode="on")
    opts._model3d_solids = 1

    telemetry = core._finalize_import_recompute(
        document,
        opts,
        baseline_object_ids=set(),
        baseline_object_names=set(),
    )

    assert document.recompute_calls == [None]
    assert telemetry["strategy"] == "full"
    assert telemetry["reason"] == "model3d_objects_created"
    assert telemetry["model3d_object_count"] == 1
    assert telemetry["fallback_full"] is False


def test_unrecognized_post_baseline_object_type_requires_full_recompute():
    unknown = _Object("Unknown", "PartDesign::FeaturePython")
    document = _Document([unknown])
    opts = _opts(model3d_mode="off")

    telemetry = core._finalize_import_recompute(
        document,
        opts,
        baseline_object_ids=set(),
        baseline_object_names=set(),
    )

    assert document.recompute_calls == [None]
    assert telemetry["strategy"] == "full"
    assert telemetry["reason"] == "unproven_post_baseline_object_types"
    assert telemetry["unproven_type_ids"] == ["PartDesign::FeaturePython"]
    assert telemetry["fallback_full"] is False
