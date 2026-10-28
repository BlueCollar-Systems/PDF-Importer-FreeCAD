"""The committed benchmark baseline stays usable without a source-PDF hash.

The baseline names its workload with a made-up id and leaves ``sha256_pdf``
empty, so the private workload PDF is never fingerprinted in the repository.
``diff_reports`` must still compare the page IR digests against a local run.
"""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "benchmarks"


def _diff_module():
    spec = importlib.util.spec_from_file_location(
        "bench_diff_reports", BENCH_DIR / "diff_reports.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, report: dict) -> Path:
    path.write_text(json.dumps(report), encoding="utf-8")
    return path


def _baseline() -> tuple[Path, dict]:
    manifest = json.loads((BENCH_DIR / "corpus_manifest.json").read_text(encoding="utf-8"))
    (workload,) = manifest["workloads"]
    path = BENCH_DIR / workload["baseline_report"]
    return path, json.loads(path.read_text(encoding="utf-8"))


def test_manifest_and_baseline_agree_and_carry_no_pdf_hash() -> None:
    manifest = json.loads((BENCH_DIR / "corpus_manifest.json").read_text(encoding="utf-8"))
    (workload,) = manifest["workloads"]
    _, report = _baseline()
    (record,) = report["workloads"]
    assert record["workload_id"] == workload["workload_id"]
    assert record["pdf_path"] == workload["path"]
    assert record["sha256_pdf"] is None


def test_local_run_with_a_pdf_hash_matches_the_committed_baseline(tmp_path) -> None:
    baseline_path, report = _baseline()
    candidate = copy.deepcopy(report)
    candidate["workloads"][0]["sha256_pdf"] = "0" * 64
    errors = _diff_module().diff_reports(
        baseline_path, _write(tmp_path / "candidate.json", candidate)
    )
    assert errors == []


def test_changed_page_digest_is_still_reported(tmp_path) -> None:
    baseline_path, report = _baseline()
    candidate = copy.deepcopy(report)
    candidate["workloads"][0]["sha256_pdf"] = "0" * 64
    candidate["workloads"][0]["pages"][0]["digest"] = "f" * 64
    errors = _diff_module().diff_reports(
        baseline_path, _write(tmp_path / "candidate.json", candidate)
    )
    assert any("canonical IR digest differs" in error for error in errors)


def test_a_recorded_baseline_hash_is_still_enforced(tmp_path) -> None:
    _, report = _baseline()
    recorded = copy.deepcopy(report)
    recorded["workloads"][0]["sha256_pdf"] = "1" * 64
    candidate = copy.deepcopy(report)
    candidate["workloads"][0]["sha256_pdf"] = "2" * 64
    errors = _diff_module().diff_reports(
        _write(tmp_path / "baseline.json", recorded),
        _write(tmp_path / "candidate.json", candidate),
    )
    assert any("source PDF hash differs" in error for error in errors)
