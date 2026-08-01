#!/usr/bin/env python3
"""Build a Windows Setup.exe installer for PDFVectorImporter using Inno Setup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import build_release


REPO_ROOT = Path(__file__).parent.resolve()
ADDON_DIR = REPO_ROOT / "PDFVectorImporter"
DIST_DIR = REPO_ROOT / "dist"
STAGE_DIR = DIST_DIR / "installer_stage"
INNO_SCRIPT = REPO_ROOT / "installer" / "PDFVectorImporter.iss"
INNO_TOOLCHAIN_MANIFEST = REPO_ROOT / "installer" / "inno-toolchain-6.7.1.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def verify_inno_toolchain(
    iscc: str | Path,
    manifest_path: str | Path = INNO_TOOLCHAIN_MANIFEST,
) -> dict:
    """Verify that *iscc* belongs to the exact committed portable toolchain."""

    compiler = Path(iscc).resolve()
    manifest_file = Path(manifest_path).resolve()
    manifest_bytes = manifest_file.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("schema") != "bcs.inno_toolchain/1.0":
        raise RuntimeError("Inno Setup toolchain manifest schema mismatch")
    if compiler.name.lower() != "iscc.exe":
        raise RuntimeError(f"Inno Setup toolchain compiler name mismatch: {compiler.name}")

    root = compiler.parent
    expected_paths: set[str] = set()
    tree_records: list[dict] = []
    for entry in manifest.get("files") or []:
        relative = str(entry.get("path") or "").replace("\\", "/")
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise RuntimeError(f"Inno Setup toolchain manifest path mismatch: {relative!r}")
        expected_paths.add(relative.casefold())
        candidate = root / Path(relative)
        if not candidate.is_file():
            raise RuntimeError(f"Inno Setup toolchain file mismatch: missing {relative}")
        actual_size = candidate.stat().st_size
        actual_hash = _sha256_file(candidate)
        if actual_size != int(entry.get("size", -1)):
            raise RuntimeError(f"Inno Setup toolchain size mismatch: {relative}")
        if actual_hash != str(entry.get("sha256") or "").lower():
            raise RuntimeError(f"Inno Setup toolchain hash mismatch: {relative}")
        tree_records.append(
            {"path": relative, "size": actual_size, "sha256": actual_hash}
        )

    actual_paths = {
        path.relative_to(root).as_posix().casefold()
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise RuntimeError(
            "Inno Setup toolchain tree mismatch: "
            f"missing={missing!r} unexpected={unexpected!r}"
        )

    tree_records.sort(key=lambda item: item["path"].casefold())
    return {
        "name": str(manifest.get("name") or "Inno Setup"),
        "version": str(manifest.get("version") or ""),
        "source_sha256": str(manifest.get("source_sha256") or "").lower(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "tree_sha256": hashlib.sha256(_canonical_json_bytes(tree_records)).hexdigest(),
    }


def write_attestation(
    output_path: str | Path,
    *,
    release_zip: str | Path,
    installer_exe: str | Path,
    toolchain_identity: dict,
    source_commit: str,
) -> Path:
    """Write a deterministic artifact/toolchain binding for one Setup.exe."""

    zip_path = Path(release_zip).resolve()
    setup_path = Path(installer_exe).resolve()
    payload = {
        "schema": "bcs.freecad_installer_attestation/1.0",
        "source_commit": str(source_commit or "unknown"),
        "source_zip": {
            "name": zip_path.name,
            "size": zip_path.stat().st_size,
            "sha256": _sha256_file(zip_path),
        },
        "installer": {
            "name": setup_path.name,
            "size": setup_path.stat().st_size,
            "sha256": _sha256_file(setup_path),
        },
        "toolchain": dict(toolchain_identity),
    }
    destination = Path(output_path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(_canonical_json_bytes(payload))
    return destination


def _read_package_version(package_xml: Path) -> str:
    if not package_xml.exists():
        raise FileNotFoundError(f"Missing package metadata: {package_xml}")

    text = package_xml.read_text(encoding="utf-8")
    match = re.search(r"<version>(.*?)</version>", text)
    if not match:
        raise RuntimeError("Could not determine version from package.xml")
    return match.group(1).strip()


def read_version() -> str:
    return _read_package_version(ADDON_DIR / "package.xml")


def find_iscc(explicit_path: str | None) -> Path:
    candidates: list[Path] = []

    if explicit_path:
        candidates.append(Path(explicit_path))

    for name in ("iscc", "ISCC.exe"):
        on_path = shutil.which(name)
        if on_path:
            candidates.append(Path(on_path))

    for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_var)
        if not root:
            continue
        base = Path(root)
        candidates.append(base / "Inno Setup 6" / "ISCC.exe")
        candidates.append(base / "Inno Setup 5" / "ISCC.exe")

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(
        "Inno Setup compiler (ISCC.exe) was not found.\n"
        "Install Inno Setup 6 from https://jrsoftware.org/isinfo.php "
        "or pass --iscc C:\\path\\to\\ISCC.exe."
    )


def stage_release(
    source_zip: str | Path | None = None,
    *,
    dist_dir: str | Path | None = None,
    stage_dir: str | Path | None = None,
) -> tuple[str, Path, Path]:
    version = read_version()
    output_root = Path(dist_dir).resolve() if dist_dir is not None else DIST_DIR
    stage_root = Path(stage_dir).resolve() if stage_dir is not None else STAGE_DIR
    output_root.mkdir(parents=True, exist_ok=True)

    if source_zip is None:
        zip_path = build_release.build(output_root)
    else:
        zip_path = Path(source_zip).resolve()
        expected_name = f"FreeCAD-PDF-Importer_v{version}.zip"
        if not zip_path.is_file():
            raise FileNotFoundError(f"Canonical release ZIP not found: {zip_path}")
        if zip_path.name != expected_name:
            raise RuntimeError(
                f"Canonical release ZIP must be named {expected_name}, got {zip_path.name}"
            )

    if stage_root.exists():
        shutil.rmtree(stage_root)
    stage_root.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(stage_root)

    source_dir = stage_root / "PDFVectorImporter"
    if not source_dir.is_dir():
        raise RuntimeError(f"Expected staged addon folder at {source_dir}")
    staged_version = _read_package_version(source_dir / "package.xml")
    if staged_version != version:
        raise RuntimeError(
            "Staged package version mismatch: "
            f"repository={version} archive={staged_version}"
        )

    return version, source_dir, zip_path


def compile_installer(
    iscc: Path,
    version: str,
    source_dir: Path,
    *,
    output_dir: str | Path | None = None,
) -> Path:
    output_root = Path(output_dir).resolve() if output_dir is not None else DIST_DIR
    output_root.mkdir(parents=True, exist_ok=True)
    base_name = f"FreeCAD-PDF-Importer-Setup_v{version}"
    cmd = [
        str(iscc),
        str(INNO_SCRIPT),
        f"/DMyAppVersion={version}",
        f"/DSourceDir={source_dir}",
        f"/O{output_root}",
        f"/F{base_name}",
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)

    installer_exe = output_root / f"{base_name}.exe"
    if not installer_exe.exists():
        raise RuntimeError(
            "Inno Setup completed but installer was not found at "
            f"{installer_exe}"
        )
    return installer_exe


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build PDFVectorImporter Windows installer (.exe)"
    )
    parser.add_argument(
        "--iscc",
        default=None,
        help="Path to ISCC.exe (Inno Setup compiler). Optional if ISCC is on PATH.",
    )
    parser.add_argument(
        "--toolchain-manifest",
        default=str(INNO_TOOLCHAIN_MANIFEST),
        help="Exact portable Inno Setup toolchain manifest.",
    )
    parser.add_argument(
        "--verify-toolchain-only",
        action="store_true",
        help="Verify the selected compiler tree and exit without building.",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--stage-dir", default=None)
    parser.add_argument(
        "--attestation",
        default=None,
        help="Write deterministic installer/toolchain attestation JSON here.",
    )
    parser.add_argument(
        "--source-commit",
        default=os.environ.get("GITHUB_SHA", "unknown"),
        help="Exact source commit bound into the attestation.",
    )
    parser.add_argument(
        "--source-zip",
        default=None,
        help=(
            "Use this already-published release ZIP as the installer payload. "
            "The ZIP is validated and never rebuilt or modified."
        ),
    )
    args = parser.parse_args()

    iscc = find_iscc(args.iscc)
    toolchain_identity = verify_inno_toolchain(iscc, args.toolchain_manifest)
    if args.verify_toolchain_only:
        print(json.dumps(toolchain_identity, sort_keys=True))
        return 0

    version, source_dir, zip_path = stage_release(
        args.source_zip,
        dist_dir=args.output_dir,
        stage_dir=args.stage_dir,
    )
    installer_exe = compile_installer(
        iscc,
        version,
        source_dir,
        output_dir=args.output_dir,
    )
    if args.attestation:
        write_attestation(
            args.attestation,
            release_zip=zip_path,
            installer_exe=installer_exe,
            toolchain_identity=toolchain_identity,
            source_commit=args.source_commit,
        )

    print("")
    print(f"Release zip: {zip_path}")
    print(f"Installer:   {installer_exe}")
    print(f"Stage dir:   {source_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - entrypoint safety
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
