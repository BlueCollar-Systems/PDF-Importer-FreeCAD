"""Dense-page 3D Text timing budget (week-plan PH1-FC-2 test lock).

Runs a real headless FreeCAD import of a LOCAL dense-text PDF through
FreeCADCmd and asserts wall time stays inside budget. The fixture arrives via
environment (never a path inside the repo — PRIV-1): point
``BCS_FC_PERF_BUDGET_PDF`` at a dense drawing whose text uses embedded
simple TrueType/CFF fonts (Type0/CID pages cannot deliver exact-font 3D Text
on this source line yet, so they cannot serve as the perf fixture).

Budget rationale (measured on the reference dense chart, 369 spans):
- 4.0.67 shipped behavior: 75s/page but wrong fonts and widths.
- raw 4.0.70 (fonts + widths correct): 490s/page.
- 4.0.70 + perf levers P1-P4: 185s/page.
The default 240s budget fails the 490s regression class (double
tessellation, per-character face building, post-recompute write re-touch)
while passing the levered pipeline. Override with ``BCS_FC_PERF_BUDGET_S``;
``BCS_FC_PERF_BUDGET_SPANS`` sets the minimum delivered-span floor.

R4-2: every unmet precondition skips VISIBLY with the concrete reason.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BUDGET_S = 240.0
DEFAULT_MIN_SPANS = 300

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
import json, os, sys, time, traceback
out_path = sys.argv[-2]
pdf_path = sys.argv[-1]
result = {"ok": False}
try:
    import FreeCAD
    repo = %(repo)r
    for p in (os.path.join(repo, "PDFVectorImporter", "src"),
              os.path.join(repo, "PDFVectorImporter")):
        sys.path.insert(0, p)
    import PDFImporterCore as core
    doc = FreeCAD.newDocument("perfbudget")
    opts = core.ImportOptions()
    opts.verbose = False
    t0 = time.perf_counter()
    core.import_pdf(pdf_path, opts)
    result["elapsed_s"] = round(time.perf_counter() - t0, 2)
    result["delivered"] = dict(getattr(opts, "text_delivered_counts", {}) or {})
    result["cache_stats"] = dict(getattr(opts, "wirestring_cache_stats", {}) or {})
    result["ok"] = True
except Exception:
    result["error"] = traceback.format_exc()
with open(out_path, "w") as handle:
    json.dump(result, handle)
"""


def test_dense_page_3d_text_stays_inside_budget(tmp_path):
    if os.environ.get("BCS_SKIP_FC_PERF_BUDGET") == "1":
        pytest.skip("BCS_SKIP_FC_PERF_BUDGET=1 — perf budget not measured on this runner")
    pdf_path = os.environ.get("BCS_FC_PERF_BUDGET_PDF", "")
    if not pdf_path:
        pytest.skip(
            "BCS_FC_PERF_BUDGET_PDF not set — dense-page perf budget not measured "
            "(point it at a local dense PDF with embedded simple fonts)"
        )
    if not Path(pdf_path).is_file():
        pytest.skip("BCS_FC_PERF_BUDGET_PDF does not exist — perf budget not measured")
    freecadcmd = _find_freecadcmd()
    if not freecadcmd:
        pytest.skip("FreeCADCmd.exe not found — dense-page perf budget not measured")

    probe_path = tmp_path / "perf_budget_probe.py"
    probe_path.write_text(PROBE_SOURCE % {"repo": str(REPO_ROOT)}, encoding="utf-8")
    out_path = str(tmp_path / "perf_budget_result.json")

    completed = subprocess.run(
        [freecadcmd, str(probe_path), out_path, pdf_path],
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert Path(out_path).is_file(), (
        "probe produced no result (rc=%s)\nstdout tail: %s\nstderr tail: %s"
        % (completed.returncode, completed.stdout[-500:], completed.stderr[-500:])
    )
    result = json.loads(Path(out_path).read_text(encoding="utf-8"))
    assert result.get("ok"), "probe failed: %s" % result.get("error", "")

    min_spans = int(os.environ.get("BCS_FC_PERF_BUDGET_SPANS", DEFAULT_MIN_SPANS))
    delivered = result.get("delivered", {})
    assert int(delivered.get("native_3d_text") or 0) >= min_spans, (
        "budget fixture must deliver at least %d spans as requested 3D Text "
        "(got %r) — a sparse fixture cannot lock dense-page performance"
        % (min_spans, delivered)
    )

    budget_s = float(os.environ.get("BCS_FC_PERF_BUDGET_S", DEFAULT_BUDGET_S))
    elapsed = float(result["elapsed_s"])
    assert elapsed < budget_s, (
        "dense page took %.1fs (budget %.0fs, %r delivered) — perf lever "
        "regression (double tessellation / per-char faces / post-recompute "
        "write re-touch)" % (elapsed, budget_s, delivered)
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q", "-rs"]))
