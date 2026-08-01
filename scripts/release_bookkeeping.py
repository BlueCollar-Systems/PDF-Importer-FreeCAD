#!/usr/bin/env python3
"""Create canonical post-release digest records and safe commit subjects."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Sequence


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TARGET_RE = re.compile(r"^[0-9a-f]{40}$")


def commit_subject(tag: str) -> str:
    if not re.fullmatch(r"v\d+\.\d+\.\d+", str(tag)):
        raise ValueError(f"invalid release tag: {tag!r}")
    return f"chore(release): record {tag} artifact digests [skip release]"


def validate_ledger_path(path: str | Path, repo_root: str | Path) -> Path:
    root = Path(repo_root).resolve()
    ledger = (root / "release-bookkeeping").resolve()
    candidate = Path(path).resolve()
    try:
        candidate.relative_to(ledger)
    except ValueError as exc:
        raise ValueError("bookkeeping output must stay under release-bookkeeping") from exc
    return candidate


def write_record(
    ledger_root: str | Path,
    *,
    tag: str,
    target: str,
    assets: Sequence[dict],
) -> Path:
    commit_subject(tag)
    if not TARGET_RE.fullmatch(str(target).lower()):
        raise ValueError("release target must be an exact commit SHA")
    normalized = []
    for asset in assets:
        name = str(asset.get("name") or "")
        digest = str(asset.get("sha256") or "").lower()
        size = int(asset.get("size", -1))
        if not name or Path(name).name != name:
            raise ValueError(f"invalid asset name: {name!r}")
        if size < 0 or not SHA256_RE.fullmatch(digest):
            raise ValueError(f"invalid asset identity: {name!r}")
        normalized.append({"name": name, "sha256": digest, "size": size})
    normalized.sort(key=lambda item: item["name"])

    ledger = Path(ledger_root).resolve()
    output = validate_ledger_path(ledger / f"{tag}.json", ledger.parent)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "assets": normalized,
        "schema": "bcs.release_bookkeeping/1.0",
        "tag": tag,
        "target": str(target).lower(),
    }
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output


def _parse_asset(value: str) -> dict:
    try:
        name, size, sha256 = value.split(":", 2)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("asset must be NAME:SIZE:SHA256") from exc
    return {"name": name, "size": int(size), "sha256": sha256}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    subject = sub.add_parser("subject")
    subject.add_argument("--tag", required=True)
    record = sub.add_parser("record")
    record.add_argument("--ledger-root", default="release-bookkeeping")
    record.add_argument("--tag", required=True)
    record.add_argument("--target", required=True)
    record.add_argument("--asset", action="append", type=_parse_asset, required=True)
    args = parser.parse_args(argv)
    if args.command == "subject":
        print(commit_subject(args.tag))
        return 0
    output = write_record(
        args.ledger_root,
        tag=args.tag,
        target=args.target,
        assets=args.asset,
    )
    print(output)
    print(commit_subject(args.tag))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
