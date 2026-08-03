#!/usr/bin/env python3
"""Smoke-test the shipped FreeCAD release ZIP."""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


REQUIRED_MEMBERS = {
    "PDFVectorImporter/Init.py",
    "PDFVectorImporter/InitGui.py",
    "PDFVectorImporter/package.xml",
    "PDFVectorImporter/src/PDFImporterCore.py",
    "PDFVectorImporter/pdfcadcore/fitz_loader.py",
    "PDFVectorImporter/src/lib/runtime-manifest.json",
    "PDFVectorImporter/src/lib/common/pymupdf/__init__.py",
    "PDFVectorImporter/src/lib/cp310/fontTools/__init__.py",
    "PDFVectorImporter/src/lib/cp311/fontTools/__init__.py",
}

REQUIRED_WINDOWS_RUNTIME = {
    "PDFVectorImporter/src/lib/common/pymupdf/_extra.pyd",
    "PDFVectorImporter/src/lib/common/pymupdf/_mupdf.pyd",
}


def _resolve_zip(pattern: str) -> Path:
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise SystemExit(f"No release ZIP matched {pattern!r}")
    return Path(matches[-1]).resolve()


def _smoke_runtime(python_exe: Path, addon_root: Path, runtime_tag: str) -> None:
    expected = {"cp310": (3, 10), "cp311": (3, 11)}[runtime_tag]
    code = (
        "import pathlib, sys; "
        f"assert sys.version_info[:2] == {expected!r}; "
        f"root = pathlib.Path({str(addon_root)!r}); "
        "sys.path.insert(0, str(root)); "
        "from PDFVectorImporter.runtime_paths import "
        "activate_bundled_runtime_if_available; "
        "runtime = activate_bundled_runtime_if_available(root / "
        "'PDFVectorImporter'); "
        f"assert runtime is not None and runtime.runtime_tag == {runtime_tag!r}; "
        "import pymupdf as fitz, fontTools; "
        "assert callable(getattr(fitz, 'open', None)); "
        "print(getattr(fitz, '__version__', '') or "
        "getattr(fitz, 'VersionBind', ''), fontTools.__version__)"
    )
    proc = subprocess.run(
        [str(python_exe), "-c", code],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(
            f"{runtime_tag} bundled runtime import failed: "
            + (proc.stderr.strip() or proc.stdout.strip())
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", help="Release ZIP path or glob pattern")
    parser.add_argument("--python310", type=Path)
    parser.add_argument("--python311", type=Path)
    args = parser.parse_args()

    zip_path = _resolve_zip(args.zip_path)
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        missing = sorted(REQUIRED_MEMBERS - names)
        missing_runtime = sorted(REQUIRED_WINDOWS_RUNTIME - names)
        linux_binaries = sorted(
            name
            for name in names
            if name.startswith("PDFVectorImporter/src/lib/") and name.endswith(".so")
        )
        if missing:
            raise SystemExit(
                "Release ZIP is missing required FreeCAD members: "
                + ", ".join(missing)
            )
        if missing_runtime:
            raise SystemExit(
                "Release ZIP is missing required Windows PyMuPDF runtime files: "
                + ", ".join(missing_runtime)
            )
        if linux_binaries:
            raise SystemExit(
                "Release ZIP contains Linux shared objects in the vendored runtime: "
                + ", ".join(linux_binaries[:8])
            )

        manifest = json.loads(
            zf.read("PDFVectorImporter/src/lib/runtime-manifest.json")
        )
        if set(manifest.get("runtimes", {})) != {"cp310", "cp311"}:
            raise SystemExit("Release ZIP runtime manifest is missing cp310 or cp311")

        if sys.platform == "win32":
            if args.python310 is None or args.python311 is None:
                raise SystemExit(
                    "Windows release smoke requires --python310 and --python311"
                )
            with tempfile.TemporaryDirectory(prefix="fc_release_zip_") as tmp:
                zf.extractall(tmp)
                extracted_root = Path(tmp)
                _smoke_runtime(args.python310, extracted_root, "cp310")
                _smoke_runtime(args.python311, extracted_root, "cp311")

    print(f"Release ZIP smoke passed: {zip_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
