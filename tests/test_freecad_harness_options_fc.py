from PDFVectorImporter.adapters import freecad_harness
from PDFVectorImporter.pdfcadcore.import_config import ImportConfig


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
