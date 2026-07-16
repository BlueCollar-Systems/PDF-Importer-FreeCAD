"""Round 9: optional 3D model generation wiring."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE = REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCore.py"
CMD = REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCmd.py"

if str(REPO_ROOT / "PDFVectorImporter") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore.import_config import ImportConfig  # noqa: E402


def test_import_config_carries_model3d_options():
    # RB-11: value lock, not a source-wording lock. The shipped defaults are
    # the contract; the dataclass may be refactored freely as long as a
    # default-constructed config still carries them.
    cfg = ImportConfig()
    assert cfg.model3d_mode == "off"
    assert cfg.model3d_depth_mm == 3.175


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
