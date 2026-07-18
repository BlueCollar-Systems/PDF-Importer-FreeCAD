from __future__ import annotations

from PDFVectorImporter.adapters import freecad_harness
from PDFVectorImporter.pdfcadcore.import_config import ImportConfig


class _Core:
    class ImportOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)


def test_harness_maps_canonical_lineweight_config_to_core_linewidth_option():
    config = ImportConfig.vector()

    options = freecad_harness._build_import_options(_Core, config, [1])

    assert not hasattr(config, "assign_linewidth")
    assert options.assign_linewidth is config.assign_lineweight
    assert options.text_mode == "3d_text"
    assert options.pages == [1]
