#!/usr/bin/env python3
"""build_release.py — BlueCollar Systems
Produces a clean PDFVectorImporter release zip suitable for FreeCAD Addon Manager
distribution and manual install.

Excluded:
  - __pycache__/ and *.pyc
  - .ruff_cache/
  - .github/
  - .git/
  - test PDFs, QA configs, and internal harness files
  - this script itself

Usage:
  python build_release.py
  python build_release.py --out /path/to/output_dir

Output:
  FreeCAD-PDF-Importer_v<VERSION>.zip  (next to this script, or --out dir)
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.resolve()
ADDON_DIR = REPO_ROOT / "PDFVectorImporter"

# Files / dirs to always exclude (matched against each path component)
EXCLUDE_DIRS = {
    "__pycache__",
    ".ruff_cache",
    ".github",
    ".git",
    "_archived",
    "qa_runs",
    "adapters",  # CLI test harnesses — not needed at FreeCAD runtime
    "tests",
    "temp",
}

EXCLUDE_FILES = {
    ".gitignore",
    ".gitattributes",
    "build_release.py",
    "qa_config_example.json",
    "qa_config_template.json",
    "fc_smoke_payload.json",
    "fc_check_fitz.py",
    "run_pdf_vector_importer_tests.py",
    "su_manual_verification_checklist.md",
    "qa_config_local_live.json",
}

EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".pdf",       # test PDFs should not ship
    ".bak",
    ".swp",
}

PYMUPDF_SPEC = "PyMuPDF>=1.24,<2.0"
FONTTOOLS_SPEC = "fonttools>=4.50,<5.0"
RUNTIME_DEPENDENCY_SPECS = (PYMUPDF_SPEC, FONTTOOLS_SPEC)
VENDORED_LIB_DIR = ADDON_DIR / "src" / "lib"
DETERMINISTIC_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _should_exclude(rel: Path) -> bool:
    parts = rel.parts
    # ``pip --target`` creates Windows console launchers whose shebang embeds
    # the build machine's absolute Python path.  The importer never calls these
    # command-line wrappers, so shipping them is both non-portable and
    # non-reproducible.  Wheel RECORD files contain hashes for those wrappers
    # and are likewise unnecessary for runtime imports.
    if len(parts) >= 3 and parts[:3] == ("src", "lib", "bin"):
        return True
    if rel.name == "RECORD" and any(part.endswith(".dist-info") for part in parts):
        return True
    for part in rel.parts:
        if part in EXCLUDE_DIRS:
            return True
    if rel.name in EXCLUDE_FILES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def _write_deterministic_file(
    archive: zipfile.ZipFile, source: Path, archive_name: Path
) -> None:
    """Write one regular file without leaking host timestamps or permissions."""
    info = zipfile.ZipInfo(
        archive_name.as_posix(), date_time=DETERMINISTIC_ZIP_TIMESTAMP
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    archive.writestr(info, source.read_bytes())


def _read_version() -> str:
    pkg_xml = ADDON_DIR / "package.xml"
    if pkg_xml.exists():
        text = pkg_xml.read_text(encoding="utf-8")
        m = re.search(r"<version>(.*?)</version>", text)
        if m:
            return m.group(1).strip()
    return "0.0.0"


def _candidate_freecad_pythons() -> list[Path]:
    candidates: list[Path] = []
    for key in ("FREECAD_PYTHON", "FREECAD_PYTHON_EXE"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    for pattern in (
        r"C:\Program Files\FreeCAD 1.1\bin\python.exe",
        r"C:\Program Files\FreeCAD*\bin\python.exe",
        r"C:\Program Files (x86)\FreeCAD*\bin\python.exe",
    ):
        if "*" in pattern:
            candidates.extend(Path("C:/").glob(pattern.replace("C:\\", "").replace("\\", "/")))
        else:
            candidates.append(Path(pattern))
    candidates.append(Path(sys.executable))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen and candidate.exists():
            seen.add(key)
            unique.append(candidate)
    return unique


def _python_version(python_exe: Path) -> tuple[int, int]:
    code = "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    proc = subprocess.run(
        [str(python_exe), "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    major, minor = proc.stdout.strip().split(".", 1)
    return int(major), int(minor)


def _lib_has_runtime_dependencies(python_exe: Path, lib_dir: Path) -> bool:
    if not lib_dir.is_dir():
        return False
    lib_root = lib_dir.resolve()
    code = (
        "import os, pathlib, sys\n"
        f"root = pathlib.Path({str(lib_root)!r}).resolve()\n"
        "sys.path.insert(0, str(root))\n"
        "import pymupdf as fitz\n"
        "import fontTools\n"
        "def local(module):\n"
        "    origin = pathlib.Path(module.__file__).resolve()\n"
        "    try:\n"
        "        origin.relative_to(root)\n"
        "    except ValueError:\n"
        "        raise SystemExit(3)\n"
        "    return origin\n"
        "local(fitz); local(fontTools)\n"
        "def version_tuple(value):\n"
        "    parts = []\n"
        "    for token in str(value).split('.'):\n"
        "        digits = ''.join(ch for ch in token if ch.isdigit())\n"
        "        if not digits: break\n"
        "        parts.append(int(digits))\n"
        "    return tuple(parts)\n"
        "fitz_version = version_tuple(getattr(fitz, '__version__', '') "
        "or getattr(fitz, 'VersionBind', ''))\n"
        "font_version = version_tuple(getattr(fontTools, 'version', '') "
        "or getattr(fontTools, '__version__', ''))\n"
        "if not ((1, 24) <= fitz_version < (2, 0)):\n"
        "    raise SystemExit(4)\n"
        "if not ((4, 50) <= font_version < (5, 0)):\n"
        "    raise SystemExit(5)\n"
        "print('OK')\n"
    )
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", code],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "OK"


def _prune_vendored_pymupdf() -> None:
    """Remove PyMuPDF development files that are not needed at runtime."""
    for rel in (
        Path("pymupdf") / "mupdf-devel",
    ):
        path = VENDORED_LIB_DIR / rel
        if path.exists():
            shutil.rmtree(path)


def ensure_runtime_dependencies(*, vendor: bool = True) -> Path:
    """Ensure the release tree has all private PDF/text runtime dependencies."""
    candidates = _candidate_freecad_pythons()
    if not candidates:
        raise RuntimeError("No Python executable found for dependency verification.")

    preferred = candidates[0]
    for python_exe in candidates:
        if _lib_has_runtime_dependencies(python_exe, VENDORED_LIB_DIR):
            _prune_vendored_pymupdf()
            print(f"Vendored runtime dependencies OK for {python_exe}: {VENDORED_LIB_DIR}")
            return python_exe

    if not vendor:
        raise RuntimeError(
            "PyMuPDF and fonttools are not bundled in PDFVectorImporter/src/lib. "
            "Run build_release.py without --no-vendor-deps or populate src/lib first."
        )

    py_version = _python_version(preferred)
    if py_version < (3, 10):
        raise RuntimeError(
            f"{preferred} is Python {py_version[0]}.{py_version[1]}; "
            "PyMuPDF wheels require Python 3.10+."
        )

    if VENDORED_LIB_DIR.exists():
        shutil.rmtree(VENDORED_LIB_DIR)
    VENDORED_LIB_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Vendoring {', '.join(RUNTIME_DEPENDENCY_SPECS)} into {VENDORED_LIB_DIR}")
    print(f"Using Python: {preferred}")
    subprocess.run(
        [
            str(preferred),
            "-m",
            "pip",
            "install",
            "--upgrade",
            "--only-binary",
            ":all:",
            "--target",
            str(VENDORED_LIB_DIR),
            *RUNTIME_DEPENDENCY_SPECS,
        ],
        check=True,
    )
    _prune_vendored_pymupdf()

    if not _lib_has_runtime_dependencies(preferred, VENDORED_LIB_DIR):
        raise RuntimeError(
            f"Runtime dependency install completed but import failed from {VENDORED_LIB_DIR}"
        )
    return preferred


def build(out_dir: Path, *, vendor_deps: bool = True) -> Path:
    version = _read_version()
    zip_name = f"FreeCAD-PDF-Importer_v{version}.zip"
    zip_path = out_dir / zip_name

    out_dir.mkdir(parents=True, exist_ok=True)
    ensure_runtime_dependencies(vendor=vendor_deps)

    file_count = 0
    skipped = 0

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for abs_path in sorted(ADDON_DIR.rglob("*")):
            if not abs_path.is_file():
                continue
            rel = abs_path.relative_to(ADDON_DIR)
            if _should_exclude(rel):
                skipped += 1
                continue
            # Archive path: PDFVectorImporter/<rel>
            arc_name = Path("PDFVectorImporter") / rel
            _write_deterministic_file(zf, abs_path, arc_name)
            file_count += 1

    print(f"Built: {zip_path}")
    print(f"  {file_count} files included, {skipped} excluded")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PDFVectorImporter release zip")
    parser.add_argument(
        "--out", default=str(REPO_ROOT),
        help="Output directory (default: repo root)"
    )
    parser.add_argument(
        "--no-vendor-deps",
        action="store_true",
        help="Fail if runtime dependencies are not already present instead of installing them.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    zip_path = build(out_dir, vendor_deps=not args.no_vendor_deps)
    print(f"\nRelease ready: {zip_path}")


if __name__ == "__main__":
    main()
