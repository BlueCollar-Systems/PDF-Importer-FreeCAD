"""FreeCAD GUI contract: professional single-flow import and Advanced strategy."""
from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CMD_PY = REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCmd.py"


class TestFcGuiProfessionalImport(unittest.TestCase):
    """GUI hides strategy on main form; Auto unless Advanced is checked."""

    def setUp(self) -> None:
        self.source = CMD_PY.read_text(encoding="utf-8")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
