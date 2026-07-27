"""P1-4 (week-plan PH2-FC-2): raster-delivery lock on a raster-only fixture.

End-to-end auto-mode import of the corpus ``tier2/pdf-type-matrix/
raster_only.pdf`` fixture through a real headless FreeCAD: a scanned page
must DELIVER a persistent ``Image::ImagePlane`` whose image file exists on
disk, and the written import report must say so honestly (auto resolved to
raster with the recorded reason, raster page count, fallback marked used,
importer version attributed) — requested-vs-delivered evidence, never a
silent relabel.

R4-2: every unmet precondition skips VISIBLY with the concrete reason
(corpus root unset, fixture absent, FreeCADCmd unavailable).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_FREECADCMD_CANDIDATES = (
    os.environ.get("BCS_FREECADCMD", ""),
    r"C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe",
    r"C:\Program Files\FreeCAD 1.0\bin\FreeCADCmd.exe",
)


def _find_freecadcmd() -> str:
    for candidate in _FREECADCMD_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return shutil.which("FreeCADCmd") or ""


PROBE_SOURCE = r"""
import json, os, sys, traceback
out_path = sys.argv[-3]
report_path = sys.argv[-2]
pdf_path = sys.argv[-1]
result = {"ok": False}
try:
    import FreeCAD
    repo = %(repo)r
    for p in (os.path.join(repo, "PDFVectorImporter", "src"),
              os.path.join(repo, "PDFVectorImporter")):
        sys.path.insert(0, p)
    import PDFImporterCore as core
    doc = FreeCAD.newDocument("rasterfixture")
    opts = core.ImportOptions()
    opts.verbose = False
    opts.import_report_path = report_path
    core.import_pdf(pdf_path, opts)
    planes = []
    for obj in doc.Objects:
        if obj.TypeId != "Image::ImagePlane":
            continue
        image_file = str(getattr(obj, "ImageFile", "") or "")
        planes.append({
            "image_file": image_file,
            "image_file_exists": os.path.isfile(image_file),
            "x_size": float(obj.XSize),
            "y_size": float(obj.YSize),
        })
    result["image_planes"] = planes
    result["n_objects"] = len(doc.Objects)
    result["ok"] = True
except Exception:
    result["error"] = traceback.format_exc()
with open(out_path, "w") as handle:
    json.dump(result, handle)
"""


def test_raster_only_fixture_delivers_image_plane_with_honest_report(tmp_path):
    corpus_root = os.environ.get("BCS_CORPUS_ROOT", "")
    if not corpus_root:
        pytest.skip("BCS_CORPUS_ROOT not set — raster-only fixture unavailable")
    fixture = Path(corpus_root) / "tier2" / "pdf-type-matrix" / "raster_only.pdf"
    if not fixture.is_file():
        pytest.skip(f"raster-only fixture missing: {fixture}")
    freecadcmd = _find_freecadcmd()
    if not freecadcmd:
        pytest.skip("FreeCADCmd.exe not found — raster delivery not exercised")

    probe_path = tmp_path / "raster_fixture_probe.py"
    probe_path.write_text(PROBE_SOURCE % {"repo": str(REPO_ROOT)}, encoding="utf-8")
    out_path = tmp_path / "raster_fixture_result.json"
    report_path = tmp_path / "raster_fixture_import_report.json"

    completed = subprocess.run(
        [freecadcmd, str(probe_path), str(out_path), str(report_path), str(fixture)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert out_path.is_file(), (
        "probe produced no result (rc=%s)\nstdout tail: %s\nstderr tail: %s"
        % (completed.returncode, completed.stdout[-500:], completed.stderr[-500:])
    )
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result.get("ok"), "probe failed: %s" % result.get("error", "")

    # --- delivery: the raster rung actually produced a persistent host image ---
    planes = result.get("image_planes") or []
    assert planes, "raster-only page delivered no Image::ImagePlane"
    for plane in planes:
        assert plane["image_file_exists"], (
            "delivered ImagePlane's image file is missing on disk: %s"
            % plane["image_file"]
        )
        assert plane["x_size"] > 0.0 and plane["y_size"] > 0.0

    # --- honesty: the written report attributes the raster path truthfully ---
    assert report_path.is_file(), "import report was not written"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    extra = report.get("extra") or {}
    fallback = report.get("fallback") or {}

    assert extra.get("auto_resolved_mode") == "raster"
    assert str(extra.get("auto_reason") or "").strip(), (
        "auto mode must record WHY it resolved to raster"
    )
    assert int(extra.get("raster_page_count") or 0) >= 1
    reasons = extra.get("raster_fallback_reasons") or []
    assert reasons and all(str(r).strip() for r in reasons)
    assert fallback.get("used") is True
    assert str(fallback.get("reason") or "").strip()

    # No fabricated vector/text success on a scanned page.
    result_block = report.get("result") or {}
    assert int(result_block.get("text_entities") or 0) == 0

    # Attribution: the report must say which importer produced the evidence.
    version = str(((report.get("importer") or {}).get("version")) or "").strip()
    assert version and version.lower() != "unknown"
