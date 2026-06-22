# -*- coding: utf-8 -*-
"""BCS-ARCH-001 Phase 5 — FreeCAD clean-break verification.

Asserts that the FreeCAD workbench has fully migrated from the old 7-preset
model to the 4-mode model. Any remaining ``--preset`` arg, preset classmethod,
or preset-named lookup is a regression.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKBENCH_DIR = REPO_ROOT / "PDFVectorImporter"
SRC_DIR = WORKBENCH_DIR / "src"
ADAPTERS_DIR = WORKBENCH_DIR / "adapters"
EMBEDDED_CORE_CONFIG = WORKBENCH_DIR / "pdfcadcore" / "import_config.py"
IMPORTER_CORE = SRC_DIR / "PDFImporterCore.py"

for p in (str(SRC_DIR), str(WORKBENCH_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)


class TestImportConfigCleanBreak(unittest.TestCase):
    """The local workbench ImportConfig must have 4 modes, no presets."""

    def setUp(self):
        # Local workbench import_config (src/PDFImportConfig.py).
        from PDFImportConfig import ImportConfig  # noqa: WPS433
        self.ImportConfig = ImportConfig

    def test_four_mode_classmethods_exist(self):
        for name in ("auto", "vector", "raster", "hybrid"):
            self.assertTrue(
                hasattr(self.ImportConfig, name),
                f"ImportConfig.{name}() missing — BCS-ARCH-001 mode constructor.",
            )

    def test_old_presets_removed(self):
        deleted = (
            "fast",
            "general_vector",
            "technical_drawing",
            "shop_drawing",
            "full",
            "max_fidelity",
        )
        for name in deleted:
            self.assertFalse(
                hasattr(self.ImportConfig, name),
                f"ImportConfig.{name}() still exists — clean break violated.",
            )

    def test_mode_classmethods_set_import_mode(self):
        self.assertEqual(self.ImportConfig.auto().import_mode, "auto")
        self.assertEqual(self.ImportConfig.vector().import_mode, "vector")
        self.assertEqual(self.ImportConfig.raster().import_mode, "raster")
        self.assertEqual(self.ImportConfig.hybrid().import_mode, "hybrid")

    def test_raster_mode_disables_text_and_arcs(self):
        cfg = self.ImportConfig.raster()
        self.assertFalse(cfg.import_text)
        self.assertFalse(cfg.detect_arcs)
        self.assertFalse(cfg.make_faces)

    def test_consolidated_defaults(self):
        # BCS-ARCH-001 parameter table — tightest correct values.
        cfg = self.ImportConfig()
        self.assertAlmostEqual(cfg.curve_step_mm, 0.2)
        self.assertAlmostEqual(cfg.join_tol, 0.05)
        self.assertAlmostEqual(cfg.arc_fit_tol_mm, 0.05)
        self.assertEqual(cfg.cleanup_level, "balanced")
        self.assertTrue(cfg.detect_arcs)
        self.assertTrue(cfg.map_dashes)
        self.assertTrue(cfg.make_faces)
        self.assertTrue(cfg.import_text)
        self.assertEqual(cfg.text_mode, "3d_text")
        self.assertTrue(cfg.strict_text_fidelity)
        self.assertEqual(cfg.hatch_mode, "group")
        self.assertEqual(cfg.lineweight_mode, "preserve")
        self.assertEqual(cfg.grouping_mode, "per_page")
        self.assertEqual(cfg.raster_dpi, 300)


class TestDialogCleanBreak(unittest.TestCase):
    """The GUI dialog must expose 4 modes and 4 text-mode options.

    We can't launch FreeCAD here, so the check is structural: scan the
    source file for the expected constants and the absence of old preset
    names.
    """

    PRESET_NAMES = (
        "Fast",
        "Balanced",
        "Full",
        "Max Fidelity",
        "Raster Image",
        "Custom...",
        "Fast Preview",
        "General Vector",
        "Technical Drawing",
        "Shop Drawing",
        "Raster + Vectors",
        "Raster Only",
        "Max Fidelity",
    )

    def setUp(self):
        self.source = (SRC_DIR / "PDFImporterCmd.py").read_text(encoding="utf-8")

    def test_modes_dict_present(self):
        self.assertIn("MODES = {", self.source,
                      "Dialog must declare MODES = {...} (BCS-ARCH-001).")
        for mode in ('"Auto"', '"Vector"', '"Raster"', '"Hybrid"'):
            self.assertIn(mode, self.source,
                          f"Dialog MODES is missing {mode}.")

    def test_no_legacy_presets_dict(self):
        self.assertNotIn("PRESETS = {", self.source,
                         "Dialog still defines legacy PRESETS = {...}.")

    def test_no_legacy_preset_labels(self):
        for label in self.PRESET_NAMES:
            self.assertNotIn(
                f'"{label}"', self.source,
                f"Dialog still references legacy preset label {label!r}.",
            )

    def test_text_mode_combo_has_four_entries(self):
        # The text-mode selector must list exactly the BCS-ARCH-001 options.
        self.assertIn('["Labels", "3D Text", "Glyphs", "Geometry"]', self.source,
                      "Dialog text-mode combo must list Labels/3D Text/Glyphs/Geometry.")

    def test_text_default_is_scale_stable(self):
        self.assertIn('self.text_combo.setCurrentText("3D Text")', self.source)
        self.assertNotIn('self.text_combo.setCurrentText("Labels")', self.source)
        self.assertIn('"TextDefaultMigratedV407"', self.source)
        self.assertNotIn("fast, editable", self.source)

    def test_import_text_checkbox_present(self):
        self.assertIn("import_text_chk", self.source,
                      "Dialog must have a separate import-text QCheckBox.")

    def test_mode_combo_in_advanced_only(self):
        self.assertIn("mode_combo", self.source)
        self.assertIn("advanced_group", self.source)
        self.assertNotIn('form.addRow("Mode:", self.mode_combo)', self.source)
        self.assertIn("Professional import", self.source)
        # The old secondary Import Mode combo had a "Vectors Only" label.
        self.assertNotIn('"Vectors Only"', self.source,
                         "Dialog still has the legacy 'Vectors Only' label.")

    # BCS-ARCH-001 Rule 5 sweep: every quality-tier dial must be gone.
    REMOVED_WIDGETS = (
        "arc_mode_combo",
        "cleanup_combo",
        "lineweight_combo",
        "hatch_combo",
        "dpi_combo",
        "strict_text_chk",
    )

    def test_quality_tier_widgets_removed(self):
        for w in self.REMOVED_WIDGETS:
            self.assertNotIn(
                w, self.source,
                f"Dialog still defines quality-tier widget {w!r} (BCS-ARCH-001 Rule 5).")


class TestTextDefaults(unittest.TestCase):
    """Core config defaults must not silently return to Labels."""

    def test_embedded_core_config_defaults_to_3d_text(self):
        source = EMBEDDED_CORE_CONFIG.read_text(encoding="utf-8")
        self.assertIn('text_mode: str = "3d_text"', source)
        self.assertNotIn('text_mode: str = "labels"', source)

    def test_glyphs_mode_uses_vector_glyph_renderer(self):
        source = IMPORTER_CORE.read_text(encoding="utf-8")
        self.assertIn('if opts.text_mode in ("glyphs", "geometry"):', source)
        self.assertIn('label = "text geometry" if opts.text_mode == "geometry" else "text glyphs"', source)


class TestBlenderAdapterCleanBreak(unittest.TestCase):
    """QA adapter for the BL CLI must pass --mode, not --preset."""

    def setUp(self):
        self.source = (ADAPTERS_DIR / "blender_adapter.py").read_text(encoding="utf-8")

    def test_uses_mode_flag(self):
        self.assertIn('"--mode"', self.source,
                      "blender_adapter must pass --mode to the BL CLI.")

    def test_no_preset_flag(self):
        self.assertNotIn('"--preset"', self.source,
                         "blender_adapter still passes --preset to the BL CLI.")

    def test_mode_default_is_auto(self):
        self.assertIn('default="auto"', self.source,
                      "blender_adapter --mode default must be 'auto'.")


class TestSketchUpHarnessCleanBreak(unittest.TestCase):
    """SketchUp QA harness must use modes, not legacy preset tables."""

    def setUp(self):
        self.source = (ADAPTERS_DIR / "sketchup_harness.rb").read_text(encoding="utf-8")

    def test_uses_modes_table(self):
        self.assertIn("ImportDialog::MODES", self.source)
        self.assertNotIn("ImportDialog::PRESETS", self.source)

    def test_result_reports_mode(self):
        self.assertIn('result["mode"]', self.source)
        self.assertNotIn('result["preset"]', self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
