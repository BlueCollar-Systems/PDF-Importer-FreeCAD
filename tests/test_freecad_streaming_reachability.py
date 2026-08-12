import ast
from pathlib import Path


def test_freecad_streaming_reachability():
    tree = ast.parse(
        Path("PDFVectorImporter/src/PDFImporterCore.py").read_text(encoding="utf-8")
    )
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "import_pdf"
    )
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Name, ast.Attribute))
    }
    assert "iter_pages" not in called
    assert "StageTimer" not in called
    print("FREECAD_STREAMING_REACHABILITY_OK route=compatibility_only")
