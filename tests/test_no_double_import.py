"""Regression lock: the report phase must never re-import pages.

The actual_text_entity_types emitter originally called import_pdf_page a
second time per page inside the report-writing phase, doubling every
imported page's geometry in the FreeCAD document. The fix threads
page_text_info out of the single main import loop. This guard fails if a
second per-page import call site is ever reintroduced.
"""
from pathlib import Path

CORE = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src" / "PDFImporterCore.py"


def test_single_per_page_import_call_site():
    src = CORE.read_text(encoding="utf-8")
    calls = src.count("import_pdf_page(pdf_path, page_num=p")
    assert calls == 1, (
        f"Expected exactly one per-page import_pdf_page call site in the "
        f"multi-page loop, found {calls}. A second call re-imports pages "
        f"during report writing and doubles document geometry."
    )


def test_text_entity_info_collected_in_main_loop():
    src = CORE.read_text(encoding="utf-8")
    call = src.index("import_pdf_page(pdf_path, page_num=p")
    recompute = src.index("fc_doc.recompute()", call)
    assert call < recompute, (
        "page_text_info must be captured in the main import loop, before "
        "postprocess/report phases."
    )
