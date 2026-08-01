import pytest
from types import SimpleNamespace

from PDFVectorImporter.adapters import freecad_adapter, freecad_harness
from PDFVectorImporter.pdfcadcore.import_config import ImportConfig
from PDFVectorImporter.src import PDFImporterCore as core


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Core:
    ImportOptions = _Options


def test_harness_maps_config_lineweight_to_core_linewidth():
    config = ImportConfig.auto()
    config.assign_lineweight = False
    config.arc_fit_tol_mm = 0.037
    config.min_seg_len = 0.013
    config.raster_dpi = 288

    options = freecad_harness.build_import_options(_Core, config, [1, 3])

    assert options.pages == [1, 3]
    assert options.assign_linewidth is False
    assert not hasattr(config, "assign_linewidth")

    direct_fields = {
        "scale_to_mm",
        "user_scale",
        "flip_y",
        "join_tol",
        "min_seg_len",
        "curve_step_mm",
        "make_faces",
        "import_text",
        "text_mode",
        "strict_text_fidelity",
        "group_by_color",
        "map_dashes",
        "verbose",
        "create_top_group",
        "hatch_to_faces",
        "hatch_mode",
        "ignore_images",
        "raster_fallback",
        "raster_dpi",
        "import_mode",
        "model3d_mode",
        "model3d_depth_mm",
        "max_bezier_segments",
        "detect_arcs",
        "arc_fit_tol_mm",
        "min_arc_angle_deg",
        "arc_sampling_pts",
        "layer_mode",
        "compound_batch_size",
        "heavy_page_threshold",
    }
    for field_name in direct_fields:
        assert getattr(options, field_name) == getattr(config, field_name)


def test_harness_preserves_config_evidence_and_semantic_model_intent():
    config = ImportConfig.auto()
    config.auto_resolved_mode = "hybrid"
    config.auto_reason = "synthetic evidence reason"
    config.model3d_semantic = True

    options = freecad_harness.build_import_options(
        core,
        config,
        [2],
        max_page_complexity_units=12345,
    )

    assert options.auto_resolved_mode == "hybrid"
    assert options.auto_reason == "synthetic evidence reason"
    assert options.model3d_semantic is True
    assert options.max_page_complexity_units == 12345


@pytest.mark.parametrize("status", ["FAIL", "INCOMPLETE", "UNKNOWN", "", None])
def test_harness_result_status_cannot_exit_zero(status):
    """A written non-PASS result must make the FreeCADCmd harness fail closed."""

    assert freecad_harness.result_exit_code({"status": status}) != 0


def test_harness_pass_is_the_only_zero_exit_status():
    assert freecad_harness.result_exit_code({"status": "PASS"}) == 0


@pytest.mark.parametrize("status", ["FAIL", "INCOMPLETE", "UNKNOWN", "", None])
def test_adapter_cannot_turn_a_non_pass_result_into_success(status):
    """The outer adapter must inspect result truth, not only process return code."""

    assert freecad_adapter.result_exit_code({"status": status}, 0) != 0


def test_adapter_preserves_child_failure_and_accepts_only_pass():
    assert freecad_adapter.result_exit_code({"status": "PASS"}, 0) == 0
    assert freecad_adapter.result_exit_code({"status": "PASS"}, 7) == 7


@pytest.mark.parametrize("core_result", [None, False, 0, ""])
def test_harness_requires_explicit_true_from_core(core_result):
    status, _message = freecad_harness.import_result_status(core_result)
    assert status == "FAIL"


def test_harness_accepts_explicit_true_from_core():
    status, message = freecad_harness.import_result_status(True)
    assert status == "PASS"
    assert message == "Import completed."


def test_page_budget_resume_advances_to_page_two_without_claiming_it_complete():
    checkpoint = {
        "source_pdf_sha256": "a" * 64,
        "requested_pages": [1, 2, 3],
        "completed_pages": [1],
    }

    progress = freecad_harness.plan_page_cell(
        [1, 2, 3],
        page_budget=1,
        source_pdf_sha256="a" * 64,
        checkpoint=checkpoint,
    )

    assert progress["checkpoint_reused"] is True
    assert progress["completed_pages"] == [1]
    assert progress["active_pages"] == [2]
    assert progress["remaining_pages"] == [2, 3]
    assert progress["next_page_range"] == "2"
    assert progress["status"] == "pending"


def test_page_cell_completion_records_only_the_active_cell_and_next_resume_page():
    progress = freecad_harness.plan_page_cell(
        [1, 2, 3, 4],
        page_budget=2,
        source_pdf_sha256="b" * 64,
    )

    completed = freecad_harness.complete_page_cell(progress)

    assert completed["completed_pages"] == [1, 2]
    assert completed["active_pages"] == []
    assert completed["remaining_pages"] == [3, 4]
    assert completed["next_page_range"] == "3-4"
    assert completed["status"] == "cell_pass"
    assert completed["run_complete"] is False


def test_page_checkpoint_for_another_source_is_never_reused():
    progress = freecad_harness.plan_page_cell(
        [1, 2],
        page_budget=1,
        source_pdf_sha256="c" * 64,
        checkpoint={
            "source_pdf_sha256": "d" * 64,
            "requested_pages": [1, 2],
            "completed_pages": [1],
        },
    )

    assert progress["checkpoint_reused"] is False
    assert progress["completed_pages"] == []
    assert progress["active_pages"] == [1]


def test_unbounded_page_cell_preserves_explicit_page_selection():
    progress = freecad_harness.plan_page_cell(
        [2, 4, 7],
        page_budget=0,
        source_pdf_sha256="e" * 64,
    )

    assert progress["active_pages"] == [2, 4, 7]
    assert progress["remaining_pages"] == [2, 4, 7]


def test_adapter_payload_carries_explicit_budget_and_checkpoint_controls(tmp_path):
    checkpoint = tmp_path / "resume.json"
    args = SimpleNamespace(
        test_id="FC-DENSE-P2",
        input=str(tmp_path / "drawing.pdf"),
        mode="vector",
        page_range="1-4",
        output_dir=str(tmp_path),
        layers_min_populated=0,
        runtime_cap_seconds=120,
        notes="synthetic",
        page_budget=2,
        complexity_budget=50000,
        resume_checkpoint=str(checkpoint),
        package_sha256="f" * 64,
    )

    payload = freecad_adapter.build_payload(
        args,
        {"freecad": {}},
        str(tmp_path / "result.json"),
        str(tmp_path),
    )

    assert payload["page_budget"] == 2
    assert payload["max_page_complexity_units"] == 50000
    assert payload["resume_checkpoint"] == str(checkpoint.resolve())
    assert payload["package_sha256"] == "f" * 64


def test_acceptance_identity_binds_version_commit_engine_and_package_bytes(
    tmp_path,
    monkeypatch,
):
    engine = tmp_path / "PDFImporterCore.py"
    engine.write_text("# synthetic engine\n", encoding="utf-8")
    fake_core = SimpleNamespace(
        __file__=str(engine),
        _importer_version=lambda: "9.8.7",
    )
    monkeypatch.setattr(
        freecad_harness,
        "_git_output",
        lambda _root, args: "abc123" if args[-1] == "HEAD" else "v9.8.7",
    )

    identity = freecad_harness.build_acceptance_identity(
        fake_core,
        str(tmp_path),
        package_sha256="f" * 64,
    )

    assert identity["importer_version"] == "9.8.7"
    assert identity["git_commit"] == "abc123"
    assert identity["git_tag"] == "v9.8.7"
    assert identity["package_or_source_root"] == str(tmp_path.resolve())
    assert identity["engine_module_file"] == str(engine.resolve())
    assert len(identity["engine_module_sha256"]) == 64
    assert identity["package_sha256"] == "f" * 64
