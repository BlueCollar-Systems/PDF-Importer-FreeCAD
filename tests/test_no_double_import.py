"""Regression lock: the report phase must never re-import pages.

The actual_text_entity_types emitter originally called import_pdf_page a
second time per page inside the report-writing phase, doubling every
imported page's geometry in the FreeCAD document. The fix threads
page_text_info out of the single main import loop and reuses one open
PDF document via _import_pdf_page_inner. This guard fails if a second
per-page import call site is ever reintroduced.
"""
import ast
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src" / "PDFImporterCore.py"


def _import_pdf_name_calls(func_name: str) -> list:
    tree = ast.parse(CORE.read_text(encoding="utf-8"))
    import_pdf_node = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "import_pdf"
    )
    return [
        node for node in ast.walk(import_pdf_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == func_name
    ]


def test_single_per_page_import_call_site():
    inner_calls = _import_pdf_name_calls("_import_pdf_page_inner")
    page_calls = _import_pdf_name_calls("import_pdf_page")
    assert len(inner_calls) == 1, (
        f"Expected exactly one _import_pdf_page_inner call in import_pdf, "
        f"found {len(inner_calls)}."
    )
    assert len(page_calls) == 0, (
        f"import_pdf must not call import_pdf_page (reopens/reimports pages); "
        f"found {len(page_calls)} call(s)."
    )


def test_text_entity_info_collected_in_main_loop():
    src = CORE.read_text(encoding="utf-8")
    call = src.index("_import_pdf_page_inner(")
    recompute = src.index("fc_doc.recompute()", call)
    assert call < recompute, (
        "page_text_info must be captured in the main import loop, before "
        "postprocess/report phases."
    )
