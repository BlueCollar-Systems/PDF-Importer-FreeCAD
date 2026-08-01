"""FreeCAD GUI contract: professional single-flow import and Advanced strategy."""
from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CMD_PY = REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCmd.py"
HANDLER_PY = REPO_ROOT / "PDFVectorImporter" / "PDFImportHandler.py"


class TestFcGuiProfessionalImport(unittest.TestCase):
    """GUI hides strategy on main form; Auto unless Advanced is checked."""

    def setUp(self) -> None:
        self.source = CMD_PY.read_text(encoding="utf-8")
        self.handler_source = HANDLER_PY.read_text(encoding="utf-8")

    def test_professional_import_tagline(self) -> None:
        self.assertIn("Professional import", self.source)

    def test_main_form_has_no_mode_row(self) -> None:
        self.assertNotIn('form.addRow("Mode:", self.mode_combo)', self.source)

    def test_advanced_group_holds_mode_combo(self) -> None:
        self.assertIn("advanced_group", self.source)
        self.assertIn('advanced_form.addRow("Import strategy:", self.mode_combo)', self.source)

    def test_build_options_defaults_to_auto(self) -> None:
        self.assertIn('import_mode = "auto"', self.source)
        self.assertIn("self.advanced_group.isChecked()", self.source)

    def test_text_modes_include_freecad_capabilities(self) -> None:
        self.assertIn(
            '["Text", "Labels", "3D Text", "Glyphs", "Geometry", "Raster"]',
            self.source,
        )

    def test_modes_dict_retained_for_advanced(self) -> None:
        for mode in ('"Auto"', '"Vector"', '"Raster"', '"Hybrid"'):
            self.assertIn(mode, self.source)

    def test_interactive_flow_estimates_work_and_uses_one_cancel_transport(self) -> None:
        self.assertIn("def run_interactive_import(", self.source)
        self.assertIn("core.estimate_import_work(pdf_path, opts)", self.source)
        self.assertIn("opts.progress_callback = progress", self.source)
        self.assertIn("core.find_resumable_import_session(pdf_path, opts)", self.source)

    def test_cancelled_import_never_prints_success(self) -> None:
        self.assertIn("completed = run_interactive_import(core, pdf_path, opts)", self.source)
        self.assertIn("if not completed:\n                return", self.source)
        self.assertIn(
            "completed = run_interactive_import(core, filename, opts)",
            self.handler_source,
        )
        self.assertIn("if not completed:\n            return", self.handler_source)

    def test_complexity_summary_names_every_work_unit(self) -> None:
        for label in (
            "drawing operations",
            "text characters",
            "image instances",
            "total work units",
            "Complexity risk",
        ):
            self.assertIn(label, self.source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
