"""Round 9: optional 3D model generation wiring."""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE = REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCore.py"
CMD = REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCmd.py"
CONFIG = REPO_ROOT / "PDFVectorImporter" / "pdfcadcore" / "import_config.py"


def test_import_config_carries_model3d_options():
    source = CONFIG.read_text(encoding="utf-8")
    assert 'model3d_mode: str = "off"' in source
    assert "model3d_depth_mm: float = 3.175" in source


def test_freecad_gui_exposes_model3d_controls():
    source = CMD.read_text(encoding="utf-8")
    assert "SHAPE_EXTRUSION_UI_ENABLED" in source
    assert "opts.model3d_mode" in source
    assert "opts.model3d_depth_mm" in source
    assert "Auto (if drawing has 3D evidence)" in source
    assert "Extrude closed shapes" in source


def test_freecad_core_builds_and_reports_model3d_solids():
    source = CORE.read_text(encoding="utf-8")
    assert "def _model3d_should_extrude" in source
    assert '"PDF_3D_Solid"' in source
    assert "Shape.extrude(Vector(0, 0, depth))" in source
    assert '"model_3d_intent"' in source
    assert '"model_3d"' in source
