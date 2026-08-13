#!/usr/bin/env python3
"""Diff two equivalence-harness reports for correctness and timing change.

This is the first step in Phase 2: before any timing claim is accepted,
the canonical page IR of the candidate must exactly equal the baseline.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List


def load_report(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def diff_reports(baseline_path: Path, candidate_path: Path) -> List[str]:
    base = load_report(baseline_path)
    cand = load_report(candidate_path)

    errors: List[str] = []
    if base.get("corpus_schema") != cand.get("corpus_schema"):
        errors.append(
            f"corpus_schema mismatch: {base.get('corpus_schema')} vs {cand.get('corpus_schema')}"
        )

    base_wl = {w["workload_id"]: w for w in base.get("workloads", [])}
    cand_wl = {w["workload_id"]: w for w in cand.get("workloads", [])}

    for wid in sorted(set(base_wl) | set(cand_wl)):
        if wid not in base_wl:
            errors.append(f"workload {wid}: missing in baseline")
            continue
        if wid not in cand_wl:
            errors.append(f"workload {wid}: missing in candidate")
            continue

        b = base_wl[wid]
        c = cand_wl[wid]

        if b.get("status") != c.get("status"):
            errors.append(
                f"{wid}: status mismatch {b.get('status')} vs {c.get('status')}"
            )
            continue

        if b.get("status") != "ok":
            continue

        if b.get("sha256_pdf") != c.get("sha256_pdf"):
            errors.append(f"{wid}: source PDF hash differs (file changed between runs?)")
            continue

        b_pages = {p["page_number"]: p for p in b.get("pages", [])}
        c_pages = {p["page_number"]: p for p in c.get("pages", [])}
        if set(b_pages) != set(c_pages):
            errors.append(
                f"{wid}: page number set differs {set(b_pages)} vs {set(c_pages)}"
            )
            continue

        for page_number in sorted(b_pages):
            bd = b_pages[page_number].get("digest")
            cd = c_pages[page_number].get("digest")
            if bd != cd:
                errors.append(
                    f"{wid} page {page_number}: canonical IR digest differs"
                )
                b_sum = b_pages[page_number].get("summary", {})
                c_sum = c_pages[page_number].get("summary", {})
                if b_sum != c_sum:
                    errors.append(
                        f"  summaries: {json.dumps(b_sum)} vs {json.dumps(c_sum)}"
                    )

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path, help="Baseline report JSON")
    parser.add_argument("candidate", type=Path, help="Candidate report JSON")
    args = parser.parse_args()

    errors = diff_reports(args.baseline, args.candidate)
    if errors:
        print("EQUIVALENCE MISMATCH:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("EQUIVALENCE OK: canonical page IR is identical.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
