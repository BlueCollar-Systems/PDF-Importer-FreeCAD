#!/usr/bin/env python3
"""List Tier-1 (or Tier-N) corpus entries from manifest.json, optionally per host."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def corpus_root() -> Path:
    env = os.environ.get("BCS_CORPUS_ROOT") or os.environ.get("PDF_TEST_CORPUS")
    if env:
        return Path(env).expanduser()
    return Path("C:/1pdf-test-corpus")


def desktop_mirror(name: str) -> Path | None:
    desktop = Path.home() / "Desktop" / "PDFTest Files" / name
    return desktop if desktop.is_file() else None


def resolve_entry(root: Path, entry: dict) -> Path | None:
    local = entry.get("local_path")
    if local:
        candidate = root / local.replace("/", os.sep)
        if candidate.is_file():
            return candidate
    fallback = entry.get("desktop_fallback")
    if fallback:
        return desktop_mirror(fallback)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="List BlueCollar PDF test corpus entries")
    parser.add_argument("--tier", type=int, default=1, help="Tier number (default 1)")
    parser.add_argument("--host", default="", help="Filter hosts: SU, FC, LC, BL, app")
    parser.add_argument("--resolved", action="store_true", help="Only entries with PDF on disk")
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()

    root = corpus_root()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise SystemExit(f"manifest not found: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    host = args.host.strip().upper()
    rows = []
    for entry in manifest.get("entries", []):
        if int(entry.get("tier", 0)) != args.tier:
            continue
        hosts = [h.upper() for h in entry.get("hosts", [])]
        if host and host not in hosts:
            continue
        path = resolve_entry(root, entry)
        if args.resolved and not path:
            continue
        rows.append(
            {
                "id": entry.get("id"),
                "name": entry.get("name"),
                "acquisition": entry.get("acquisition"),
                "license": entry.get("license"),
                "tests": entry.get("tests", []),
                "hosts": hosts,
                "oracle_id": entry.get("oracle_id"),
                "path": str(path) if path else None,
                "url": entry.get("url"),
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        for row in rows:
            status = row["path"] or row["url"] or "missing"
            print(f"{row['id']}\t{row['name']}\t{status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
