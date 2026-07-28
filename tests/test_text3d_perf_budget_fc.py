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
    loaded_core = sys.modules.get("PDFImporterCore")
    loaded_path = os.path.abspath(getattr(loaded_core, "__file__", ""))
    repo_core = os.path.abspath(
        os.path.join(repo, "PDFVectorImporter", "src", "PDFImporterCore.py")
    )
    if loaded_core is not None and os.path.normcase(loaded_path) != os.path.normcase(repo_core):
        del sys.modules["PDFImporterCore"]
    for module_name, module in list(sys.modules.items()):
        if module_name == "pdfcadcore" or module_name.startswith("pdfcadcore."):
            module_path = os.path.abspath(getattr(module, "__file__", ""))
            if module_path and not os.path.normcase(module_path).startswith(
                os.path.normcase(os.path.abspath(repo) + os.sep)
            ):
                del sys.modules[module_name]
    import PDFImporterCore as core
    import pdfcadcore.import_report as report_contract
    captured_warnings = []
    original_warn = core._warn
    def capture_warning(message):
        captured_warnings.append(str(message))
        return original_warn(message)
    core._warn = capture_warning
    result["core_path"] = os.path.abspath(core.__file__)
    result["report_contract_path"] = os.path.abspath(report_contract.__file__)
    if os.path.normcase(result["core_path"]) != os.path.normcase(repo_core):
        raise RuntimeError(
            "performance probe loaded stale PDFImporterCore: %%s (expected %%s)"
            %% (result["core_path"], repo_core)
        )
    doc = FreeCAD.newDocument("perfbudget")
    opts = core.ImportOptions()
    opts.verbose = False
    opts.import_report_path = out_path + ".import_report.json"
    t0 = time.perf_counter()
    core.import_pdf(pdf_path, opts)
    result["elapsed_s"] = round(time.perf_counter() - t0, 2)
    result["delivered"] = dict(getattr(opts, "text_delivered_counts", {}) or {})
    result["cache_stats"] = dict(getattr(opts, "wirestring_cache_stats", {}) or {})
    result["phase_timings_ms"] = dict(getattr(opts, "phase_timings_ms", {}) or {})
    report_extra = dict(getattr(opts, "_report_extra", {}) or {})
    result["report_extra_keys"] = sorted(report_extra)
    result["save_reopen"] = {
        key: value
        for key, value in dict(report_extra.get("save_reopen_inventory") or {}).items()
        if key in {
            "schema", "verified", "reason", "inventory_digest",
            "reopened_inventory_digest", "shape_archive_evidence_digest",
            "archive_unchanged_after_open", "phase_timings_ms",
        }
    }
    result["warnings"] = captured_warnings
    report_path = opts.import_report_path
    result["report_path"] = report_path
    if os.path.isfile(report_path):
        result["report_bytes"] = os.path.getsize(report_path)
        with open(report_path, "r", encoding="utf-8") as report_handle:
            report_data = json.load(report_handle)
        report_extra = dict(report_data.get("extra") or {})
        inventory = dict(report_extra.get("actual_host_object_inventory") or {})
        save_reopen = dict(report_extra.get("save_reopen_inventory") or {})
        delivery = dict(report_extra.get("text_representation_delivery") or {})
        result["report_contract_ready"] = dict(
            report_extra.get("import_contract_ready") or {}
        )
        result["report_inventory_verified"] = inventory.get("verified") is True
        result["report_inventory_objects"] = len(inventory.get("objects") or [])
        result["report_save_reopen_verified"] = save_reopen.get("verified") is True
        result["report_delivery_verified"] = delivery.get("verified") is True
        result["report_delivery_items"] = len(delivery.get("items") or [])
        result["report_payload_compact"] = not any(
            key in save_reopen
            for key in (
                "expected_objects", "actual_objects", "geometry_comparisons",
                "shape_archive_evidence",
            )
        ) and "terminal_attempts" not in delivery
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
    assert Path(result["core_path"]).resolve() == (
        REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCore.py"
    ).resolve()
    assert Path(result["report_contract_path"]).resolve() == (
        REPO_ROOT / "PDFVectorImporter" / "pdfcadcore" / "import_report.py"
    ).resolve()
    assert result.get("report_contract_ready", {}).get("ready") is True, result.get(
        "report_contract_ready"
    )
    assert result.get("report_inventory_verified") is True
    assert result.get("report_save_reopen_verified") is True
    assert result.get("report_delivery_verified") is True
    assert result.get("report_payload_compact") is True

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
