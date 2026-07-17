from __future__ import annotations

import importlib.util
import sys
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_fonttools_is_declared_for_python_and_freecad_addon_manager():
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["dependencies"]
    assert any(value.lower().startswith("fonttools>=") for value in dependencies)

    root = ET.parse(REPO_ROOT / "PDFVectorImporter" / "package.xml").getroot()
    namespace = {"fc": "https://wiki.freecad.org/Package_Metadata"}
    declared = {
        str(node.text or "").strip().lower()
        for node in root.findall(".//fc:depend", namespace)
    }
    assert "fonttools" in declared


def test_release_builder_verifies_and_vendors_both_runtime_dependencies():
    spec = importlib.util.spec_from_file_location(
        "freecad_build_release", REPO_ROOT / "build_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert module.RUNTIME_DEPENDENCY_SPECS == (
        "PyMuPDF>=1.24,<2.0",
        "fonttools>=4.50,<5.0",
    )
    source = (REPO_ROOT / "build_release.py").read_text(encoding="utf-8")
    assert "import fontTools" in source
    assert "*RUNTIME_DEPENDENCY_SPECS" in source


def test_both_interactive_setup_paths_install_and_verify_fonttools():
    init_source = (REPO_ROOT / "PDFVectorImporter" / "InitGui.py").read_text(
        encoding="utf-8"
    )
    tools_source = (REPO_ROOT / "PDFVectorImporter" / "PDFTools.py").read_text(
        encoding="utf-8"
    )

    assert "import fontTools" in init_source
    assert '"fonttools>=4.50,<5.0"' in init_source
    assert "import fontTools" in tools_source
    assert '"fonttools>=4.50,<5.0"' in tools_source


def test_installation_docs_name_the_complete_runtime_and_current_menu_command():
    for relative in ("README.md", "PDFVectorImporter/README.md"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "fonttools" in source.lower()
        assert "Install / Update PDF Dependencies" in source
        assert "Install / Update PyMuPDF" not in source
