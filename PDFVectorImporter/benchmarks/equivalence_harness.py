#!/usr/bin/env python3
"""Phase 2 equivalence and timing harness for pdfcadcore.

This script extracts the host-neutral PageData IR for every workload in the
corpus manifest and records:

- exact ordered canonical page IR (as a stable digest)
- extract_ms and host_build_ms from pdfcadcore.stage_timing.StageTimer
- wall time, page count, primitive count, text count
- any extraction failure with traceback

The output is a JSON report. Two reports can be diffed with the companion
``diff_reports.py`` to establish equivalence before benchmarking.
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# PDFVectorImporter root is PDFVectorImporter/; repo root is one above.
REPO_ROOT = Path(__file__).resolve().parents[2]
PDFCADCORE_ROOT = REPO_ROOT / "PDFVectorImporter" / "pdfcadcore"
if str(PDFCADCORE_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PDFCADCORE_ROOT.parent))

from pdfcadcore.primitives import PageData, Primitive, NormalizedText
from pdfcadcore.stage_timing import StageTimer
from pdfcadcore.streaming import iter_pages


SCHEMA = "bcs.pdfcadcore.equivalence_harness/1.0"


def _canonical_float(value: float, ndigits: int = 9) -> float:
    """Round a float to a fixed precision for stable canonical digests."""
    try:
        return round(float(value), ndigits)
    except (TypeError, ValueError):
        return 0.0


def _normalize(value: Any) -> Any:
    """Recursively normalize values for canonical comparison."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items())}
    if isinstance(value, float):
        return _canonical_float(value)
    if isinstance(value, (int, str, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _page_data_to_dict(page_data: PageData) -> Dict[str, Any]:
    """Convert a PageData to a normalized dict, preserving identity."""
    return dataclasses.asdict(page_data)


def _page_digest(page_data: PageData) -> str:
    """Stable SHA-256 digest of a canonical PageData representation."""
    data = _page_data_to_dict(page_data)
    normalized = _normalize(data)
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _summarize(page_data: PageData) -> Dict[str, Any]:
    """Lightweight summary for a page."""
    return {
        "primitive_count": len(page_data.primitives),
        "text_count": len(page_data.text_items),
        "width": _canonical_float(page_data.width),
        "height": _canonical_float(page_data.height),
    }


def extract_workload(
    workload_id: str,
    pdf_path: Path,
    *,
    category: str = "short",
    notes: str = "",
) -> Dict[str, Any]:
    """Extract all pages from a single workload and return a report record."""
    record: Dict[str, Any] = {
        "workload_id": workload_id,
        "pdf_path": str(pdf_path),
        "category": category,
        "notes": notes,
        "sha256_pdf": _sha256_file(pdf_path),
        "status": "pending",
    }

    if not pdf_path.is_file():
        record["status"] = "missing"
        return record

    timer = StageTimer()
    wall_start = time.perf_counter()
    pages: List[Dict[str, Any]] = []
    try:
        for page_number, page_data in iter_pages(str(pdf_path), stage_timing=timer):
            pages.append(
                {
                    "page_number": page_number,
                    "digest": _page_digest(page_data),
                    "summary": _summarize(page_data),
                }
            )
        record["status"] = "ok"
    except Exception as exc:
        record["status"] = "error"
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()

    wall_elapsed = time.perf_counter() - wall_start
    record["wall_ms"] = _canonical_float(wall_elapsed * 1000.0, 6)
    record["extract_ms"] = _canonical_float(timer.get("extract_ms"), 6)
    record["host_build_ms"] = _canonical_float(timer.get("host_build_ms"), 6)
    record["pages"] = pages
    record["total_pages"] = len(pages)
    record["total_primitives"] = sum(p["summary"]["primitive_count"] for p in pages)
    record["total_text"] = sum(p["summary"]["text_count"] for p in pages)
    return record


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run_harness(manifest_path: Path, output_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    workloads = manifest.get("workloads", [])

    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "host_profile_id": manifest.get("host_profile_id", "unknown"),
        "corpus_schema": manifest.get("schema", "unknown"),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workloads": [],
    }

    for wl in workloads:
        record = extract_workload(
            wl["workload_id"],
            Path(wl["path"]),
            category=wl.get("category", "short"),
            notes=wl.get("notes", ""),
        )
        report["workloads"].append(record)
        print(f"  {record['workload_id']}: {record['status']} ({record.get('total_pages', 0)} pages)")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"Wrote {output_path}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).with_name("corpus_manifest.json"),
        help="Corpus manifest JSON.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("baseline_report.json"),
        help="Output report JSON path.",
    )
    args = parser.parse_args(argv)
    run_harness(args.manifest, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
