#!/usr/bin/env python3
"""Verify vendored pdfcadcore and repo_context_builder_core.py stay in sync.

FC/BL/LC embed byte-identical copies of pdfcadcore. FC is canonical. This script
fails CI when any core file drifts from the canonical hash manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = SCRIPT_DIR / "pdfcadcore_sync_manifest.json"
VALID_REPOS: Tuple[str, ...] = ("FC", "BL", "LC")

# The checker itself ships as three byte-identical copies (FC canonical +
# BL/LC) and drifted twice on 2026-06-09 under concurrent edits, so it is in
# its own manifest exactly like repo_context_builder_core.py (board Q-09-c).
# Workflow: edit FC's copy, then name each intended peer explicitly.
SELF_NAME = "pdfcadcore_sync_check.py"

# No intentional divergences: all repos must match the canonical manifest exactly.
# A real per-repo difference must be recorded with its own expected hash, never a blind skip.
KNOWN_DIVERGENCES: Dict[str, Tuple[str, ...]] = {}


def sha256_file(path: Path) -> str:
    """Hash file content with line endings normalized (CRLF -> LF).

    The repos store LF via .gitattributes, but local tools sometimes leave
    CRLF working copies while CI checkouts are LF. Hashing normalized bytes
    keeps the manifest stable across both, so EOL churn can never read as
    core drift (same lesson as the corpus-level checker, 2026-06-08).
    """
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest().lower()


def load_manifest() -> Dict[str, str]:
    if not MANIFEST_PATH.is_file():
        raise SystemExit(f"Missing manifest: {MANIFEST_PATH}")
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {str(k): str(v).lower() for k, v in data.items()}


def repo_core_dir(root: Path, repo: str) -> Path:
    if repo == "FC":
        return root / "PDFVectorImporter" / "pdfcadcore"
    if repo == "BL":
        return root / "pdf_vector_importer" / "pdfcadcore"
    if repo == "LC":
        return root / "pdfcadcore"
    raise ValueError(f"unknown repository code: {repo}")


def repo_layouts_at(root: Path) -> List[str]:
    layouts: List[str] = []
    if repo_core_dir(root, "FC").is_dir():
        layouts.append("FC")
    if repo_core_dir(root, "BL").is_dir():
        layouts.append("BL")
    if repo_core_dir(root, "LC").is_dir() and (root / "dxf_builder.py").is_file():
        layouts.append("LC")
    return layouts


def detect_repo_at(root: Path) -> Optional[str]:
    layouts = repo_layouts_at(root)
    return layouts[0] if len(layouts) == 1 else None


def detect_local_repo() -> Optional[str]:
    return detect_repo_at(SCRIPT_DIR)


def local_core_dir(repo: str) -> Path:
    """Return the detected checkout's core, never a same-host main checkout."""
    return repo_core_dir(SCRIPT_DIR, repo)


def parse_peer_roots(
    specs: Iterable[str],
    local_repo: str,
    parser: argparse.ArgumentParser,
) -> Dict[str, Path]:
    """Parse explicit HOST=PATH peers and reject ambiguity before any writes."""
    peers: Dict[str, Path] = {}
    roots: Dict[Path, str] = {}
    local_root = SCRIPT_DIR.resolve()

    for spec in specs:
        if "=" not in spec:
            parser.error(f"invalid --peer-root {spec!r}; expected HOST=PATH")
        host_text, path_text = spec.split("=", 1)
        host = host_text.strip().upper()
        raw_path = path_text.strip()
        if host not in VALID_REPOS:
            parser.error(
                f"invalid peer host {host_text!r}; expected one of {', '.join(VALID_REPOS)}"
            )
        if not raw_path:
            parser.error(f"invalid --peer-root {spec!r}; PATH is empty")
        if host in peers:
            parser.error(f"duplicate peer host {host}; list each host at most once")
        try:
            root = Path(raw_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            parser.error(f"peer root for {host} does not exist or is inaccessible: {exc}")
        if not root.is_dir():
            parser.error(f"peer root for {host} is not a directory: {root}")
        if root == local_root:
            parser.error(f"peer root for {host} is the local checkout: {root}")
        if root in roots:
            parser.error(
                f"duplicate peer root {root} for {roots[root]} and {host}"
            )
        peers[host] = root
        roots[root] = host

    for host, root in peers.items():
        if host == local_repo:
            parser.error(
                f"peer host {host} duplicates the detected local {local_repo} checkout"
            )
        layouts = repo_layouts_at(root)
        if layouts != [host]:
            found = ", ".join(layouts) if layouts else "none"
            parser.error(
                f"peer root {root} does not match the declared {host} layout "
                f"(detected: {found})"
            )
    return peers


def iter_core_files(core_dir: Path) -> Iterable[Path]:
    for path in sorted(core_dir.glob("*.py")):
        if path.name.startswith("."):
            continue
        yield path


def check_repo_core(
    repo: str,
    core_dir: Path,
    manifest: Dict[str, str],
    fix: bool,
    canonical_dir: Path,
) -> List[str]:
    errors: List[str] = []
    allowed = set(KNOWN_DIVERGENCES.get(repo, ()))

    if not core_dir.is_dir():
        return [f"{repo}: missing core directory {core_dir}"]

    for path in iter_core_files(core_dir):
        name = path.name
        if name not in manifest:
            errors.append(f"{repo}: unexpected core file not in manifest: {name}")
            continue

        actual = sha256_file(path)
        expected = manifest[name]
        if actual == expected:
            continue

        if name in allowed:
            print(f"NOTE: {repo}/{name} differs from canonical (expected divergence)")
            continue

        errors.append(
            f"{repo}/{name}: hash mismatch (expected {expected[:12]}..., got {actual[:12]}...)"
        )
        if fix and repo != "FC":
            src = canonical_dir / name
            if src.is_file():
                shutil.copy2(src, path)
                print(f"FIXED: copied canonical {name} -> {path}")

    # A manifest file absent from the embedded copy is drift too — without
    # this, a newly added core module can silently never ship to a host.
    present = {p.name for p in iter_core_files(core_dir)}
    for name in sorted(set(manifest) - present - {"repo_context_builder_core.py", SELF_NAME}):
        errors.append(f"{repo}: missing core file listed in manifest: {name}")
        if fix and repo != "FC":
            src = canonical_dir / name
            if src.is_file():
                shutil.copy2(src, core_dir / name)
                print(f"FIXED: copied canonical {name} -> {core_dir / name}")

    return errors


def check_repo_context_builder(
    manifest: Dict[str, str],
    paths: Optional[Iterable[Path]] = None,
) -> List[str]:
    key = "repo_context_builder_core.py"
    if key not in manifest:
        return [f"manifest missing {key}"]

    expected = manifest[key]
    existing: List[Tuple[Path, str]] = []
    candidates = paths if paths is not None else (
        SCRIPT_DIR / "repo_context_builder_core.py",
    )
    for path in candidates:
        if path.is_file():
            existing.append((path, sha256_file(path)))

    if not existing:
        print(f"SKIP: no {key} files found (single-repo checkout?)")
        return []

    unique = {digest for _, digest in existing}
    if len(unique) == 1 and unique.pop() == expected:
        print(f"OK: {key} in sync across {len(existing)} repos")
        return []

    errors: List[str] = []
    for path, digest in existing:
        if digest != expected:
            errors.append(
                f"{path}: hash mismatch (expected {expected[:12]}..., got {digest[:12]}...)"
            )
    if len(unique) > 1:
        errors.append(f"{key}: cross-repo drift among {len(existing)} copies")
    return errors


def check_self_copies(
    manifest: Dict[str, str],
    paths: Optional[Iterable[Path]] = None,
) -> List[str]:
    """The checker guards its own copies (board Q-09-c).

    In a single-repo CI checkout only the local copy exists; comparing it
    against the manifest still catches a copy that was edited without
    re-propagating from FC and regenerating the manifest.
    """
    if SELF_NAME not in manifest:
        return [f"manifest missing {SELF_NAME} (rerun --write-manifest)"]
    expected = manifest[SELF_NAME]
    candidates = (
        {p.resolve() for p in paths}
        if paths is not None
        else {Path(__file__).resolve()}
    )
    errors: List[str] = []
    seen = 0
    for path in sorted(candidates):
        if not path.is_file():
            continue
        seen += 1
        digest = sha256_file(path)
        if digest != expected:
            errors.append(
                f"{path}: sync-check copy drift "
                f"(expected {expected[:12]}..., got {digest[:12]}...)"
            )
    if not errors:
        print(f"OK: {SELF_NAME} in sync across {seen} {'copy' if seen == 1 else 'copies'}")
    return errors


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "From a manifest-verified local FC checkout, copy canonical core files "
            "only into explicitly named peers."
        ),
    )
    parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="Rewrite pdfcadcore_sync_manifest.json from the detected local core.",
    )
    parser.add_argument(
        "--skip-cross-repo",
        action="store_true",
        help=(
            "Deprecated no-op; checks are local-only unless --peer-root is supplied."
        ),
    )
    parser.add_argument(
        "--peer-root",
        action="append",
        default=[],
        metavar="HOST=PATH",
        help=(
            "Explicit peer checkout root (HOST is FC, BL, or LC). "
            "Repeat once per intended peer."
        ),
    )
    args = parser.parse_args(argv)
    if args.fix and args.write_manifest:
        parser.error("--fix and --write-manifest cannot be combined")

    local_repo = detect_local_repo()
    if local_repo is None:
        layouts = repo_layouts_at(SCRIPT_DIR)
        detail = f"ambiguous layouts: {', '.join(layouts)}" if layouts else "no known layout"
        parser.error(f"cannot detect the local repository at {SCRIPT_DIR} ({detail})")
    if args.fix and local_repo != "FC":
        parser.error("--fix is allowed only from a detected local FreeCAD checkout")
    peers = parse_peer_roots(args.peer_root, local_repo, parser)
    if args.fix and not peers:
        parser.error("--fix requires at least one explicit --peer-root HOST=PATH")
    if args.skip_cross_repo:
        print(
            "NOTE: --skip-cross-repo is deprecated and has no effect; "
            "only explicit --peer-root values enable peer checks"
        )

    canonical_dir = local_core_dir(local_repo)
    if args.write_manifest:
        manifest: Dict[str, str] = {}
        for path in iter_core_files(canonical_dir):
            manifest[path.name] = sha256_file(path)
        rcb = SCRIPT_DIR / "repo_context_builder_core.py"
        if rcb.is_file():
            manifest["repo_context_builder_core.py"] = sha256_file(rcb)
        manifest[SELF_NAME] = sha256_file(Path(__file__).resolve())
        payload = json.dumps(manifest, indent=2) + "\n"
        MANIFEST_PATH.write_text(payload, encoding="utf-8")
        print(f"Wrote manifest: {MANIFEST_PATH}")
        for repo, repo_root in peers.items():
            dest_manifest = repo_root / "pdfcadcore_sync_manifest.json"
            dest_manifest.write_text(payload, encoding="utf-8")
            print(f"Copied manifest -> {repo}: {dest_manifest}")
        return 0

    manifest = load_manifest()
    errors: List[str] = []

    local_errors = check_repo_core(
        local_repo,
        canonical_dir,
        manifest,
        False,
        canonical_dir,
    )
    errors.extend(local_errors)
    peer_fix = args.fix and not local_errors
    if args.fix and local_errors:
        print(
            "REFUSED: local FreeCAD core does not match the manifest; "
            "no peer files were changed"
        )

    for repo, repo_root in peers.items():
        errors.extend(
            check_repo_core(
                repo,
                repo_core_dir(repo_root, repo),
                manifest,
                peer_fix,
                canonical_dir,
            )
        )

    context_paths = [SCRIPT_DIR / "repo_context_builder_core.py"]
    context_paths.extend(
        repo_root / "repo_context_builder_core.py"
        for repo_root in peers.values()
    )
    self_paths = [Path(__file__).resolve()]
    self_paths.extend(repo_root / SELF_NAME for repo_root in peers.values())
    errors.extend(check_repo_context_builder(manifest, context_paths))
    errors.extend(check_self_copies(manifest, self_paths))

    if errors:
        print("DRIFT DETECTED:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("ALL IN SYNC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
