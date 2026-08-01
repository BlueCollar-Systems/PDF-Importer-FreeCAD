#!/usr/bin/env python3
"""
FreeCAD adapter for PDF Vector Importer QA.

Purpose:
- Provides a stable command-line interface for the external test runner.
- Can launch FreeCAD in GUI or console mode.
- Hands off a payload JSON to a Python harness script that performs the import
  and writes a result JSON file.

Typical use:
python adapters/freecad_adapter.py --config qa_config.json --test-id FC-GEO-001 --input "C:/path/to/your-drawing.pdf" --mode vector --dry-run
"""
from __future__ import annotations

from typing import Optional, Tuple

import argparse
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path


def result_exit_code(result: dict, process_returncode: int) -> int:
    """Preserve child failures and reject any terminal result other than PASS."""

    if process_returncode:
        return int(process_returncode)
    return 0 if str(result.get("status") or "").upper() == "PASS" else 1


def load_json(path: Optional[str]) -> dict:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize_path(value: Optional[str], base_dir: Optional[str] = None) -> Optional[str]:
    if value is None:
        return None
    raw = os.path.expandvars(str(value))
    p = Path(raw).expanduser()
    if not p.is_absolute() and base_dir:
        p = Path(base_dir) / p
    return str(p.resolve())


def build_payload(args: argparse.Namespace, cfg: dict, result_path: str, config_dir: Optional[str]) -> dict:
    freecad_cfg = cfg.get("freecad", {})
    return {
        "adapter": "freecad",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "test_id": args.test_id,
        "platform": "FC",
        "input_pdf": normalize_path(args.input, config_dir),
        "mode": args.mode,
        "page_range": args.page_range,
        "page_budget": max(0, int(getattr(args, "page_budget", 0) or 0)),
        "max_page_complexity_units": max(
            0, int(getattr(args, "complexity_budget", 0) or 0)
        ),
        "resume_checkpoint": normalize_path(
            getattr(args, "resume_checkpoint", None), config_dir
        ),
        "package_sha256": str(getattr(args, "package_sha256", "") or "").strip().lower(),
        "output_dir": normalize_path(args.output_dir, config_dir) if args.output_dir else None,
        "result_json": result_path,
        "mod_dir": normalize_path(freecad_cfg.get("mod_dir"), config_dir),
        "python_harness": normalize_path(freecad_cfg.get("python_harness"), config_dir),
        "expected": {
            "layers_min_populated": args.layers_min_populated,
            "runtime_cap_seconds": args.runtime_cap_seconds,
        },
        "notes": args.notes or "",
    }


def launch_freecad_console(freecadcmd_exe: str, harness: str, payload_path: str,
                           timeout: int = 300) -> Tuple[int, str, str]:
    env = os.environ.copy()
    env["BC_PDF_QA_PAYLOAD"] = payload_path

    cmd = [freecadcmd_exe, harness]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return (
            1,
            "",
            f"FreeCAD process timed out after {timeout} seconds. "
            "Consider increasing the timeout or checking if the harness is stuck.",
        )
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    return proc.returncode, stdout, stderr


def main() -> int:
    parser = argparse.ArgumentParser(description="FreeCAD adapter for PDF Vector Importer QA.")
    parser.add_argument("--config", help="Path to qa_config.json", required=False)
    parser.add_argument("--test-id", required=True, help="Test ID, e.g. FC-GEO-001")
    parser.add_argument("--input", required=True, help="Input PDF path")
    parser.add_argument("--mode", default="auto",
                        choices=["auto", "vector", "raster", "hybrid"],
                        help="Import mode (auto|vector|raster|hybrid)")
    parser.add_argument("--page-range", default="1", help="Page range string")
    parser.add_argument("--output-dir", help="Optional output directory")
    parser.add_argument("--notes", help="Optional notes")
    parser.add_argument("--layers-min-populated", type=int, default=0)
    parser.add_argument("--runtime-cap-seconds", type=int, default=0)
    parser.add_argument(
        "--page-budget",
        type=int,
        default=0,
        help="Maximum pages in this QA cell; 0 keeps the explicit selection unbounded.",
    )
    parser.add_argument(
        "--complexity-budget",
        type=int,
        default=0,
        help="Fail before host-object creation above this page work-unit limit; 0 disables.",
    )
    parser.add_argument(
        "--resume-checkpoint",
        help="Persistent page-cell checkpoint JSON used to select the next unfinished pages.",
    )
    parser.add_argument(
        "--package-sha256",
        default="",
        help="SHA-256 of the published package when acceptance tests packaged bytes.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not launch FreeCAD; emit a starter result only.")
    args = parser.parse_args()

    cfg = load_json(args.config)
    config_dir = str(Path(args.config).expanduser().resolve().parent) if args.config else os.getcwd()
    freecad_cfg = cfg.get("freecad", {})

    with tempfile.TemporaryDirectory(prefix="bc_pdf_fc_qa_") as td:
        payload_path = os.path.join(td, f"{args.test_id}_payload.json")
        result_path = os.path.join(td, f"{args.test_id}_result.json")

        payload = build_payload(args, cfg, result_path, config_dir)
        with open(payload_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        if args.dry_run:
            result = {
                "test_id": args.test_id,
                "platform": "FC",
                "status": "DRY_RUN",
                "message": "FreeCAD adapter payload created successfully.",
                "payload_json": payload_path,
                "result_json_expected": result_path,
            }
            print(json.dumps(result, indent=2))
            return 0

        freecadcmd_exe = normalize_path(freecad_cfg.get("freecadcmd_exe"), config_dir)
        harness = normalize_path(freecad_cfg.get("python_harness"), config_dir)
        if not freecadcmd_exe or not os.path.isfile(freecadcmd_exe):
            print(json.dumps({
                "test_id": args.test_id,
                "platform": "FC",
                "status": "ERROR",
                "message": f"FreeCADCmd executable not found: {freecadcmd_exe}",
                "payload_json": payload_path,
            }, indent=2))
            return 2
        if not harness or not os.path.isfile(harness):
            print(json.dumps({
                "test_id": args.test_id,
                "platform": "FC",
                "status": "ERROR",
                "message": f"FreeCAD harness not found: {harness}",
                "payload_json": payload_path,
            }, indent=2))
            return 2

        timeout = args.runtime_cap_seconds if args.runtime_cap_seconds > 0 else 300
        returncode, stdout, stderr = launch_freecad_console(freecadcmd_exe, harness, payload_path, timeout=timeout)

        # Harness should write result JSON; if not, provide best-effort fallback.
        if os.path.isfile(result_path):
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    result = json.load(f)
                print(json.dumps(result, indent=2))
                return result_exit_code(result, returncode)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

        print(json.dumps({
            "test_id": args.test_id,
            "platform": "FC",
            "status": "ERROR" if returncode else "UNKNOWN",
            "message": "Harness did not produce result JSON.",
            "payload_json": payload_path,
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
        }, indent=2))
        return returncode if returncode else 3


if __name__ == "__main__":
    raise SystemExit(main())
