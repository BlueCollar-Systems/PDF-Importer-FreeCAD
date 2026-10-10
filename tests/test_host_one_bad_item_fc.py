"""Real FreeCADCmd: one drawing item OCC refuses costs that item, not the import.

Opt-in: set BCS_FREECADCMD to FreeCADCmd.exe. The probe is FreeCADCmd's ONLY
argument; every path travels by environment variable, because FreeCAD opens a
.pdf argument itself after the script with default options.

Fixtures are fictional (job D042, mark EX101), built from raw content streams:
  * three sheets, sheet 2 holds "60 200 m 300 200 l 60 190 m 300 190 l S"
    (two lines in one stroke that OCC cannot join into one wire);
  * two sheets, sheet 2 holds a self-crossing filled outline (bow tie) whose
    face OCC refuses.
Before the step-down both imports ended with an empty document.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FREECADCMD = os.environ.get("BCS_FREECADCMD", "")

pytestmark = pytest.mark.skipif(
    not FREECADCMD or not Path(FREECADCMD).is_file(),
    reason="set BCS_FREECADCMD to FreeCADCmd.exe to run the real-host step-down check",
)

GAP_STROKE = b"q 0 0 0 RG 1 w 60 200 m 300 200 l 60 190 m 300 190 l S Q"
BOW_TIE_FILL = b"q 1 0 0 rg 100 250 m 200 330 l 200 250 l 100 330 l h f Q"

PROBE = r'''
import json, os, sys, traceback
repo = os.environ["BCS_PROBE_REPO"]
for folder in (os.path.join(repo, "PDFVectorImporter", "src"),
               os.path.join(repo, "PDFVectorImporter"), repo):
    sys.path.insert(0, folder)
result = {"ok": False}
try:
    import FreeCAD
    import PDFImporterCore as core
    result["core_file"] = core.__file__
    doc = FreeCAD.newDocument("D042")
    opts = core.ImportOptions()
    opts.verbose = False
    opts.import_report_path = os.environ["BCS_PROBE_REPORT"]
    try:
        result["returned"] = core.import_pdf(os.environ["BCS_PROBE_PDF"], opts)
        result["raised"] = None
    except BaseException as exc:
        result["raised"] = "%s: %s" % (type(exc).__name__, exc)
    result["import_status"] = getattr(opts, "import_status", None)
    result["object_count"] = len(doc.Objects)
    result["page_groups"] = sorted(o.Name for o in doc.Objects if o.Name.startswith("PDF_Page_"))
    result["outlines"] = [
        {"name": o.Name, "edges": len(o.Shape.Edges), "valid": bool(o.Shape.isValid())}
        for o in doc.Objects if o.Name.startswith("StepDownOutline")]
    result["ok"] = True
except BaseException:
    result["error"] = traceback.format_exc()[-3000:]
with open(os.environ["BCS_PROBE_OUT"], "w", encoding="utf-8") as handle:
    json.dump(result, handle, indent=1, default=str)
'''


def _make_pdf(path: Path, total: int, bad_page: int, raw: bytes) -> None:
    import fitz

    doc = fitz.open()
    for number in range(1, total + 1):
        page = doc.new_page(width=612, height=396)
        page.insert_text((50, 50), "JOB D042 MARK EX101 SHEET %d OF %d" % (number, total),
                         fontsize=12, fontname="helv")
        page.draw_line((50, 80), (300, 80), color=(0, 0, 0), width=1)
        page.draw_rect(fitz.Rect(320, 70, 400, 120), color=(0, 0, 1), width=1)
        if number == bad_page:
            page.clean_contents()
            xref = page.get_contents()[0]
            doc.update_stream(xref, doc.xref_stream(xref) + b"\n" + raw + b"\n")
    doc.save(str(path))
    doc.close()


def _run_probe(tmp_path: Path, pdf: Path):
    probe = tmp_path / "one_bad_item_probe.py"
    probe.write_text(PROBE, encoding="utf-8")
    out = tmp_path / "probe_result.json"
    report = tmp_path / "probe_import_report.json"
    work = tmp_path / "bc_temp"
    work.mkdir()
    home = tempfile.mkdtemp(prefix="bcfc-")
    env = dict(os.environ, BCS_PROBE_REPO=str(REPO_ROOT), BCS_PROBE_PDF=str(pdf),
               BCS_PROBE_OUT=str(out), BCS_PROBE_REPORT=str(report),
               BC_PDF_TEMP_DIR=str(work), FREECAD_USER_HOME=home)
    completed = subprocess.run([FREECADCMD, str(probe)], env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=600)
    assert out.is_file(), "probe wrote nothing (rc=%s)\n%s\n%s" % (
        completed.returncode, completed.stdout[-2000:], completed.stderr[-2000:])
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result.get("ok"), result.get("error")
    assert Path(result["core_file"]).resolve().is_relative_to(REPO_ROOT)
    saved = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    return result, saved, completed.stdout + completed.stderr


def test_a_stroke_occ_cannot_join_costs_that_stroke_not_the_three_sheets(tmp_path):
    pdf = tmp_path / "D042_gap_3_sheets.pdf"
    _make_pdf(pdf, 3, 2, GAP_STROKE)
    result, report, log = _run_probe(tmp_path, pdf)
    assert result["raised"] is None, log[-3000:]
    assert result["import_status"] == "success"
    assert result["page_groups"] == ["PDF_Page_1", "PDF_Page_2", "PDF_Page_3"]
    block = report["extra"]["geometry_items_degraded"]
    assert (block["total"], block["pages"], block["delivered_as_outline"]) == (1, [2], 1)
    (outline,) = result["outlines"]
    assert outline["edges"] == 2 and outline["valid"] is True
    assert report["fallback"]["used"] is True
    assert "1 drawing item on page 2 could not be built as requested" in report["extra"]["human_summary"]


def test_a_fill_occ_cannot_face_is_drawn_as_its_outline_and_both_sheets_arrive(tmp_path):
    pdf = tmp_path / "D042_bow_tie_2_sheets.pdf"
    _make_pdf(pdf, 2, 2, BOW_TIE_FILL)
    result, report, log = _run_probe(tmp_path, pdf)
    assert result["raised"] is None, log[-3000:]
    assert result["page_groups"] == ["PDF_Page_1", "PDF_Page_2"]
    block = report["extra"]["geometry_items_degraded"]
    assert (block["total"], block["pages"]) == (1, [2])
    (entry,) = block["items"]
    assert entry["delivered"] == "outline" and "face" in entry["reason"].lower()
    assert result["outlines"] and all(row["valid"] for row in result["outlines"])
    assert "1 drawing item on page 2 could not be built as requested" in report["extra"]["human_summary"]
    assert report["extra"]["import_contract_ready"]["ready"] is False
