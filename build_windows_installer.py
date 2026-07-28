#!/usr/bin/env python3
"""Build a Windows Setup.exe installer for PDFVectorImporter using Inno Setup."""

from __future__ import annotations

import argparse
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


def stage_release(source_zip: str | Path | None = None) -> tuple[str, Path, Path]:
    version = read_version()
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    if source_zip is None:
        zip_path = build_release.build(DIST_DIR)
    else:
        zip_path = Path(source_zip).resolve()
        expected_name = f"FreeCAD-PDF-Importer_v{version}.zip"
        if not zip_path.is_file():
            raise FileNotFoundError(f"Canonical release ZIP not found: {zip_path}")
        if zip_path.name != expected_name:
            raise RuntimeError(
                f"Canonical release ZIP must be named {expected_name}, got {zip_path.name}"
            )

    if STAGE_DIR.exists():
        shutil.rmtree(STAGE_DIR)
    STAGE_DIR.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(STAGE_DIR)

    source_dir = STAGE_DIR / "PDFVectorImporter"
    if not source_dir.is_dir():
        raise RuntimeError(f"Expected staged addon folder at {source_dir}")
    staged_version = _read_package_version(source_dir / "package.xml")
    if staged_version != version:
        raise RuntimeError(
            "Staged package version mismatch: "
            f"repository={version} archive={staged_version}"
        )

    return version, source_dir, zip_path


def compile_installer(iscc: Path, version: str, source_dir: Path) -> Path:
    cmd = [
        str(iscc),
        str(INNO_SCRIPT),
        f"/DMyAppVersion={version}",
        f"/DSourceDir={source_dir}",
    ]
    print("Running:", " ".join(cmd))
    subprocess.run(cmd, check=True, cwd=REPO_ROOT)

    installer_exe = DIST_DIR / f"FreeCAD-PDF-Importer-Setup_v{version}.exe"
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
        "--source-zip",
        default=None,
        help=(
            "Use this already-published release ZIP as the installer payload. "
            "The ZIP is validated and never rebuilt or modified."
        ),
    )
    args = parser.parse_args()

    version, source_dir, zip_path = stage_release(args.source_zip)
    iscc = find_iscc(args.iscc)
    installer_exe = compile_installer(iscc, version, source_dir)

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
