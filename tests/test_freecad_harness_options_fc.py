from types import SimpleNamespace

from PDFVectorImporter.adapters import freecad_harness


class _Options:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _Core:
    ImportOptions = _Options


def test_harness_maps_config_lineweight_to_core_linewidth():
    config = SimpleNamespace(
        join_tol=0.05,
        curve_step_mm=0.25,
        make_faces=True,
        import_text=True,
        text_mode="labels",
        strict_text_fidelity=True,
        hatch_mode="solid",
        group_by_color=True,
        assign_lineweight=False,
        map_dashes=True,
        detect_arcs=True,
        ignore_images=False,
        raster_fallback=True,
        import_mode="auto",
    )

    options = freecad_harness.build_import_options(_Core, config, [1, 3])

    assert options.pages == [1, 3]
    assert options.assign_linewidth is False
    assert not hasattr(config, "assign_linewidth")
