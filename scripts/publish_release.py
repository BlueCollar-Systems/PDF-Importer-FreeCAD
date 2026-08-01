#!/usr/bin/env python3
"""Publish an immutable release as a convergent, fail-closed state machine."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ReleaseDigestPending(RuntimeError):
    """GitHub accepted an asset but has not populated its digest yet."""


@dataclass(frozen=True)
class PublishConfig:
    repo: str
    tag: str
    target: str
    title: str
    notes: str
    assets: list[Path]
    latest: bool = True


@dataclass(frozen=True)
class PublishResult:
    minted: bool
    release_verified: bool


def _run(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(args), capture_output=True, text=True, check=False)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _local_assets(paths: Sequence[Path]) -> dict[str, dict]:
    assets: dict[str, dict] = {}
    for raw_path in paths:
        path = Path(raw_path).resolve()
        if not path.is_file():
            raise RuntimeError(f"release asset is missing: {path}")
        if path.name in assets:
            raise RuntimeError(f"duplicate release asset name: {path.name}")
        assets[path.name] = {
            "path": path,
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
    if not assets:
        raise RuntimeError("release asset set is empty")
    return assets


def _read_release(config: PublishConfig, run: Callable) -> dict | None:
    result = run(
        ["gh", "api", f"repos/{config.repo}/releases/tags/{config.tag}"]
    )
    if result.returncode == 0:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            raise RuntimeError("GitHub release response is not an object")
        return payload
    if "404" in (result.stderr or ""):
        return None
    raise RuntimeError(f"unable to inspect release {config.tag}: {result.stderr.strip()}")


def _read_tag_target(tag: str, run: Callable) -> str | None:
    direct = f"refs/tags/{tag}"
    peeled = direct + "^{}"
    result = run(["git", "ls-remote", "origin", direct, peeled])
    if result.returncode != 0:
        raise RuntimeError(f"unable to inspect tag {tag}: {result.stderr.strip()}")
    refs: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if len(fields) == 2:
            refs[fields[1]] = fields[0].lower()
    target = refs.get(peeled) or refs.get(direct)
    if target is not None and not SHA_RE.fullmatch(target):
        raise RuntimeError(f"invalid target returned for tag {tag}: {target!r}")
    return target


def _verify_release(
    config: PublishConfig,
    release: dict,
    local_assets: dict[str, dict],
    run: Callable,
) -> None:
    if str(release.get("tag_name") or "") != config.tag:
        raise RuntimeError("release tag mismatch")
    if bool(release.get("draft")):
        raise RuntimeError("release remains a draft")
    tag_target = _read_tag_target(config.tag, run)
    if tag_target != config.target.lower():
        raise RuntimeError(
            f"tag {config.tag} is at {tag_target}, not release target {config.target}"
        )

    remote_assets = release.get("assets") or []
    by_name = {str(item.get("name") or ""): item for item in remote_assets}
    if set(by_name) != set(local_assets):
        raise RuntimeError(
            "release asset name mismatch: "
            f"expected={sorted(local_assets)} actual={sorted(by_name)}"
        )
    for name, expected in local_assets.items():
        actual = by_name[name]
        if int(actual.get("size", -1)) != expected["size"]:
            raise RuntimeError(f"release asset size mismatch: {name}")
        digest = str(actual.get("digest") or "").lower()
        if not digest:
            raise ReleaseDigestPending(f"release asset digest is pending: {name}")
        if digest != "sha256:" + expected["sha256"]:
            raise RuntimeError(f"release asset digest mismatch: {name}")


def _wait_for_verified_release(
    config: PublishConfig,
    local_assets: dict[str, dict],
    run: Callable,
    sleep: Callable[[float], object],
    *,
    attempts: int = 6,
) -> None:
    pending_reason = "release is not visible yet"
    for attempt in range(attempts):
        release = _read_release(config, run)
        if release is not None:
            try:
                _verify_release(config, release, local_assets, run)
                return
            except ReleaseDigestPending as exc:
                pending_reason = str(exc)
        if attempt + 1 < attempts:
            sleep(2.0)
    raise RuntimeError(
        f"release did not reach a verifiable exact state after {attempts} reads: "
        f"{pending_reason}"
    )


def _create_command(
    config: PublishConfig,
    local_assets: dict[str, dict],
    *,
    exact_tag_exists: bool,
) -> list[str]:
    command = [
        "gh",
        "release",
        "create",
        config.tag,
        "--repo",
        config.repo,
        "--title",
        config.title,
        "--notes",
        config.notes,
    ]
    if exact_tag_exists:
        command.append("--verify-tag")
    else:
        command.extend(["--target", config.target])
    if config.latest:
        command.append("--latest")
    command.extend(str(local_assets[path.name]["path"]) for path in config.assets)
    return command


def publish(
    config: PublishConfig,
    *,
    run: Callable = _run,
    sleep: Callable[[float], object] = time.sleep,
) -> PublishResult:
    if not SHA_RE.fullmatch(config.target.lower()):
        raise ValueError("release target must be an exact 40-character commit SHA")
    local_assets = _local_assets(config.assets)

    existing = _read_release(config, run)
    if existing is not None:
        try:
            _verify_release(config, existing, local_assets, run)
        except ReleaseDigestPending:
            _wait_for_verified_release(config, local_assets, run, sleep)
        return PublishResult(minted=False, release_verified=True)

    tag_target = _read_tag_target(config.tag, run)
    if tag_target is not None and tag_target != config.target.lower():
        raise RuntimeError(
            f"tag {config.tag} is at {tag_target}, not release target {config.target}"
        )

    command = _create_command(
        config, local_assets, exact_tag_exists=(tag_target == config.target.lower())
    )
    created = run(command)
    if created.returncode != 0:
        # Another actor may have won after our read. Re-read and accept only the
        # exact immutable state we intended; otherwise preserve the failure.
        raced_release = _read_release(config, run)
        if raced_release is None:
            raise RuntimeError(
                f"release creation failed and no converged release exists: "
                f"{created.stderr.strip()}"
            )
        try:
            _verify_release(config, raced_release, local_assets, run)
        except ReleaseDigestPending:
            _wait_for_verified_release(config, local_assets, run, sleep)
        return PublishResult(minted=False, release_verified=True)

    _wait_for_verified_release(config, local_assets, run, sleep)
    return PublishResult(minted=True, release_verified=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--asset", action="append", required=True)
    parser.add_argument("--no-latest", action="store_true")
    parser.add_argument("--github-output")
    args = parser.parse_args(argv)
    result = publish(
        PublishConfig(
            repo=args.repo,
            tag=args.tag,
            target=args.target,
            title=args.title,
            notes=args.notes,
            assets=[Path(item) for item in args.asset],
            latest=not args.no_latest,
        )
    )
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"minted={'true' if result.minted else 'false'}\n")
            stream.write("release_verified=true\n")
    print(json.dumps({"minted": result.minted, "release_verified": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
