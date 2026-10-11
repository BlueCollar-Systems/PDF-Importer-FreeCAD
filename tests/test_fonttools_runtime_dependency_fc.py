from __future__ import annotations

import importlib.util
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dependency_contract_test_uses_only_python_310_stdlib():
    source = Path(__file__).read_text(encoding="utf-8")
    assert "\nimport tomllib\n" not in source


def test_fonttools_is_declared_for_python_and_freecad_addon_manager():
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'(?mi)^\s*"fonttools>=4\.50,<5\.0",\s*$', pyproject)
    assert '"PyMuPDF>=1.28.2,<2.0"' in pyproject

    root = ET.parse(REPO_ROOT / "PDFVectorImporter" / "package.xml").getroot()
    namespace = {"fc": "https://wiki.freecad.org/Package_Metadata"}
    declared = {
        str(node.text or "").strip().lower()
        for node in root.findall(".//fc:depend", namespace)
    }
    assert "fonttools" in declared


def test_ci_and_release_gates_install_declared_project_dependencies():
    workflows = (
        ".github/workflows/fc-pdfimporter-ci.yml",
        ".github/workflows/auto-release.yml",
    )

    for relative in workflows:
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "python -m pip install --editable . pytest" in source, relative
        assert 'pip install "PyMuPDF>=1.24,<2.0" pytest' not in source, relative


def test_release_builder_verifies_and_vendors_both_runtime_dependencies():
    spec = importlib.util.spec_from_file_location(
        "freecad_build_release", REPO_ROOT / "build_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    assert module.RUNTIME_DEPENDENCY_SPECS == (
        "PyMuPDF==1.28.2",
        "fonttools==4.63.0",
    )
    common_lock = (REPO_ROOT / "requirements-release-common.lock").read_text(
        encoding="utf-8"
    )
    cp310_lock = (REPO_ROOT / "requirements-release-cp310.lock").read_text(
        encoding="utf-8"
    )
    cp311_lock = (REPO_ROOT / "requirements-release-cp311.lock").read_text(
        encoding="utf-8"
    )
    assert "PyMuPDF==1.28.2" in common_lock
    assert "sha256:ebd244918798502d7b4504c90410d1711a4d7675a32584ca30f1bab419ecbffe" in common_lock
    assert "fonttools==4.63.0" in cp310_lock
    assert "sha256:0c18358a155d75034911c5ee397a5b44cd19dd325dbb8b35fb60bf421d6a72ac" in cp310_lock
    assert "fonttools==4.63.0" in cp311_lock
    assert "sha256:063e08bd17bd5a90127a14123de0d6a952dbc847695fd98b63c043d58057f90c" in cp311_lock
    assert not (REPO_ROOT / "requirements-release.lock").exists()
    source = (REPO_ROOT / "build_release.py").read_text(encoding="utf-8")
    assert "import fontTools" in source
    assert '"--require-hashes"' in source
    assert '"--only-binary"' in source
    assert "RUNTIME_DEPENDENCY_LOCKS" in source
    assert "cp310-cp310-win_amd64" in source
    assert "cp311-cp311-win_amd64" in source


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
    assert '"PyMuPDF>=1.28.2,<2.0"' in init_source
    assert '"PyMuPDF>=1.28.2,<2.0"' in tools_source
    harness_source = (
        REPO_ROOT / "PDFVectorImporter" / "adapters" / "freecad_harness.py"
    ).read_text(encoding="utf-8")
    assert '"PyMuPDF>=1.28.2,<2.0"' in harness_source
    assert '"PyMuPDF>=1.24,<2.0"' not in harness_source


def test_installation_docs_name_the_complete_runtime_and_current_menu_command():
    for relative in ("README.md", "PDFVectorImporter/README.md"):
        source = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "fonttools" in source.lower()
        assert "Install / Update PDF Dependencies" in source
        assert "Install / Update PyMuPDF" not in source
    # The command the docs name must really be on a workbench menu.
    import ast

    init_tree = ast.parse(
        (REPO_ROOT / "PDFVectorImporter" / "InitGui.py").read_text(encoding="utf-8")
    )
    menu_commands = set()
    for node in ast.walk(init_tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "appendMenu" and len(node.args) == 2
                and isinstance(node.args[1], ast.List)):
            menu_commands.update(
                elt.value for elt in node.args[1].elts if isinstance(elt, ast.Constant)
            )
    assert "PDF_InstallPyMuPDF" in menu_commands
