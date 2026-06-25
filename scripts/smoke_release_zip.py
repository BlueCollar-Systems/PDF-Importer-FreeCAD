#!/usr/bin/env python3
"""Smoke-test the shipped FreeCAD release ZIP."""
from __future__ import annotations

import argparse
import glob
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
    "PDFVectorImporter/src/lib/pymupdf/__init__.py",
}

REQUIRED_WINDOWS_RUNTIME = {
    "PDFVectorImporter/src/lib/pymupdf/_extra.pyd",
    "PDFVectorImporter/src/lib/pymupdf/_mupdf.pyd",
}


def _resolve_zip(pattern: str) -> Path:
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise SystemExit(f"No release ZIP matched {pattern!r}")
    return Path(matches[-1]).resolve()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", help="Release ZIP path or glob pattern")
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

        if sys.platform == "win32":
            with tempfile.TemporaryDirectory(prefix="fc_release_zip_") as tmp:
                zf.extractall(tmp)
                lib_dir = Path(tmp) / "PDFVectorImporter" / "src" / "lib"
                code = (
                    "import sys; "
                    f"sys.path.insert(0, r'{lib_dir}'); "
                    "import pymupdf as fitz; "
                    "assert callable(getattr(fitz, 'open', None)); "
                    "print(getattr(fitz, '__version__', '') or getattr(fitz, 'VersionBind', ''))"
                )
                proc = subprocess.run(
                    [sys.executable, "-c", code],
                    capture_output=True,
                    text=True,
                )
                if proc.returncode != 0 or not proc.stdout.strip():
                    raise SystemExit(
                        "Vendored PyMuPDF import failed from release ZIP: "
                        + (proc.stderr.strip() or proc.stdout.strip())
                    )

    print(f"Release ZIP smoke passed: {zip_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
