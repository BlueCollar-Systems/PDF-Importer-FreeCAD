"""The help pages describe only what the shipped add-on really does.

New users followed a "Recommended" Addon Manager route that does not exist,
looked for features that are not there (SketchUp bridge, hatch choices, scale
presets, menu items that are not on any menu), read that 3D Text is editable
ShapeString text, and were told FreeCAD 0.21 works. These checks keep the
README, INSTALL, COMPATIBILITY pages and the packaged README honest.
"""
from __future__ import annotations

import ast
import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
README = REPO_ROOT / "README.md"
INSTALL = REPO_ROOT / "INSTALL.md"
COMPATIBILITY = REPO_ROOT / "COMPATIBILITY.md"
PACKAGED_README = REPO_ROOT / "PDFVectorImporter" / "README.md"
PACKAGE_XML = REPO_ROOT / "PDFVectorImporter" / "package.xml"
CHANGELOG = REPO_ROOT / "docs" / "CHANGELOG.md"
DOCS = (README, INSTALL, COMPATIBILITY, PACKAGED_README)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _name(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _table_rows(text: str):
    """Markdown table rows (a folder-tree line like '|-- x.py' is not one)."""
    return [line for line in text.splitlines()
            if line.strip().startswith("|") and line.strip().endswith("|")]


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = text.find("\n## ", start + len(heading))
    return text[start:] if following == -1 else text[start:following]


def _workbench_menu_commands() -> set:
    """Command names that InitGui really puts on a menu or toolbar."""
    tree = ast.parse(_text(REPO_ROOT / "PDFVectorImporter" / "InitGui.py"))
    lists = {}
    placed = set()

    def evaluate(node):
        if isinstance(node, (ast.List, ast.Tuple)):
            return [elt.value for elt in node.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)]
        if isinstance(node, ast.Name):
            return list(lists.get(node.id, []))
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return evaluate(node.left) + evaluate(node.right)
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(
            node.targets[0], ast.Name
        ):
            values = evaluate(node.value)
            if values:
                lists[node.targets[0].id] = values
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"appendMenu", "appendToolbar"} and len(node.args) >= 2):
            placed.update(evaluate(node.args[1]))
    return placed


def test_no_page_recommends_an_addon_manager_listing_that_does_not_exist():
    for path in (README, INSTALL, PACKAGED_README):
        text = _text(path)
        assert "Addon Manager (Recommended)" not in text, _name(path)
        for line in text.splitlines():
            if "Addon Manager" in line:
                assert "not listed" in line.lower(), (
                    "%s still sends users to the Addon Manager: %r" % (_name(path), line)
                )
    assert "## FreeCAD Addon Manager" not in _text(INSTALL)


def test_readme_first_screen_is_the_setup_exe_install():
    lines = _text(README).splitlines()
    first_screen = "\n".join(lines[:40])
    assert "## Install" in first_screen
    assert "Setup" in first_screen and ".exe" in first_screen
    assert "/releases" in first_screen
    assert "Run as administrator" in first_screen  # told NOT to
    assert "## Recent fixes" not in _text(README)
    assert "docs/CHANGELOG.md" in _text(README)


def test_change_notes_moved_to_the_changelog_intact():
    changelog = _text(CHANGELOG)
    assert "## Recent fixes (v4.0.107)" in changelog
    assert "## Recent fixes (v4.0.78)" in changelog
    assert "## Recent fixes (unreleased)" in changelog


def test_packaged_readme_lists_no_removed_or_imaginary_features():
    text = _text(PACKAGED_README)
    assert "SKP Bridge" not in text
    assert "Import, Group, or Skip" not in text
    scale_source = _text(REPO_ROOT / "PDFVectorImporter" / "src" / "PDFScaleTool.py")
    if "preset" not in scale_source.lower():
        for row in _table_rows(text):
            if "Quick Scale" in row:
                assert "preset" not in row.lower(), row
    assert "PDFScaleTool.py" in text and "Quick Scale" in text


def test_docs_name_no_menu_item_that_is_not_on_a_menu():
    placed = _workbench_menu_commands()
    assert "ImportPDFVector" in placed, "menu parser lost the main import command"
    for path in DOCS:
        text = _text(path)
        if "PDF_InstallPyMuPDF" not in placed:
            assert "PDF Vector Importer > Install / Update" not in text, _name(path)
            assert "Workbench **Install / Update PDF Dependencies**" not in text, _name(path)
        if "PDF_BatchImport" not in placed:
            assert "PDF Vector Importer > Batch" not in text, _name(path)
            for row in _table_rows(text):
                if "Batch" in row:
                    assert "File > Import" in row or "File → Import" in row, row


def test_3d_text_is_described_as_solid_shapes_not_editable_shapestring():
    for path in DOCS:
        for row in _table_rows(_text(path)):
            if "3d text" in row.lower():
                assert "ShapeString" not in row, "%s: %s" % (_name(path), row)
    text_table = _section(_text(README), "## Text Rendering")
    rows = {re.sub(r"[*_]", "", row.split("|")[1]).strip(): row
            for row in _table_rows(text_table)[2:]}
    for name in ("Text", "Labels", "3D Text (default)", "Glyphs", "Geometry", "Raster"):
        assert name in rows, "README text table is missing %s: %s" % (name, sorted(rows))
    assert "not re-typeable" in rows["3D Text (default)"]
    assert "solid letter shapes" in rows["3D Text (default)"].lower()
    install = _text(INSTALL)
    assert "**Labels** / **Text** = editable FreeCAD text" in install
    assert "**3D Text** = solid letter shapes, not editable as words" in install


def test_version_tables_claim_only_what_was_tested():
    for path in DOCS:
        lowered = _text(path).lower()
        assert "legacy branch" not in lowered, _name(path)
        assert "legacy pin" not in lowered, _name(path)
    assert "Verified (Windows installer smoke)" not in _text(COMPATIBILITY)
    compat = _section(_text(README), "## Compatibility")
    assert "tested by hand on 1.1.4" in compat
    assert "Python 3.8" in compat and "not supported yet" in compat
    assert "26." in compat and "not yet tested" in compat
    for path, line_number in ((README, 7), (PACKAGED_README, 5)):
        badge = _text(path).splitlines()[line_number - 1]
        assert "Platform" in badge and "FreeCAD 1.0+ (Python 3.10+)" in badge, badge
        assert "0.21" not in badge


def test_package_xml_does_not_exclude_calendar_versioned_freecad():
    root = ET.parse(PACKAGE_XML).getroot()
    namespace = {"fc": "https://wiki.freecad.org/Package_Metadata"}
    for node in root.findall(".//fc:freecadmax", namespace):
        major = int(str(node.text or "0").strip().split(".")[0])
        assert major >= 26, "freecadmax %s skips FreeCAD 26.x" % node.text
    assert root.find(".//fc:freecadmin", namespace) is not None


def test_release_script_still_finds_the_version_badge_on_readme_line_6():
    spec = importlib.util.spec_from_file_location(
        "prepare_release_for_docs_test", REPO_ROOT / "scripts" / "prepare_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    targets = {Path(path).resolve(): pattern for path, pattern, _template in module.TARGETS}
    readme = _text(README)
    match = targets[README.resolve()].search(readme)
    assert match is not None
    assert readme.count("\n", 0, match.start()) == 5, "Version badge left line 6"
    assert targets[PACKAGED_README.resolve()].search(_text(PACKAGED_README)) is not None
