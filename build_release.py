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
import tempfile
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

# Private validation inputs and generated CAD/model evidence are never valid
# FreeCAD addon payload.  Reject them before exclusions are applied: silently
# dropping a private file would hide repository contamination from the release
# operator instead of failing closed.
PRIVATE_ARTIFACT_SUFFIXES = {
    ".pdf",
    ".dxf",
    ".dwg",
    ".skp",
    ".fcstd",
    ".fcstd1",
    ".blend",
    ".blend1",
}
PRIVATE_ARCHIVE_SUFFIXES = {
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".tgz",
    ".rbz",
}
PRIVATE_REPORT_SUFFIXES = {".json", ".csv", ".html", ".htm", ".png"}

PYMUPDF_SPEC = "PyMuPDF==1.28.0"
FONTTOOLS_SPEC = "fonttools==4.63.0"
RUNTIME_DEPENDENCY_SPECS = (PYMUPDF_SPEC, FONTTOOLS_SPEC)
RUNTIME_DEPENDENCY_LOCK = REPO_ROOT / "requirements-release.lock"
VENDORED_LIB_DIR = ADDON_DIR / "src" / "lib"
DETERMINISTIC_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
RELEASE_BUILD_INPUTS = (
    Path("build_release.py"),
    Path("requirements-release.lock"),
)


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


def _require_no_private_artifacts() -> None:
    """Fail when the addon tree contains private inputs or derived evidence."""
    violations: list[str] = []
    for path in sorted(ADDON_DIR.rglob("*")):
        rel = path.relative_to(ADDON_DIR)
        parts = [part.casefold() for part in rel.parts]
        if any(
            part == "imported evidence"
            or part.startswith("pdftest")
            or "test-corpus" in part
            or part.endswith("_assets")
            for part in parts
        ):
            violations.append(rel.as_posix())
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        name = path.name.casefold()
        if (
            suffix in PRIVATE_ARTIFACT_SUFFIXES
            or suffix in PRIVATE_ARCHIVE_SUFFIXES
            or ("import_report" in name and suffix in PRIVATE_REPORT_SUFFIXES)
        ):
            violations.append(rel.as_posix())

    if violations:
        preview = ", ".join(violations[:10])
        remainder = len(violations) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: private corpus artifact or generated counterpart "
            f"found under the shippable addon tree: {preview}"
        )


def _require_commit_bound_sources() -> None:
    """Reject shippable addon source that is not bound to the Git index."""
    try:
        addon_rel = ADDON_DIR.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(
            f"Addon source is outside the release repository: {ADDON_DIR}"
        ) from exc

    release_pathspecs = [path.as_posix() for path in RELEASE_BUILD_INPUTS]
    pathspecs = [addon_rel.as_posix(), *release_pathspecs]
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "ls-files",
                "-z",
                "--",
                *pathspecs,
            ],
            check=True,
            capture_output=True,
        )
        modified_proc = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "ls-files",
                "-m",
                "-z",
                "--",
                *pathspecs,
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Release source must be a Git checkout so package bytes can be "
            "bound to a committed tree."
        ) from exc

    prefix = addon_rel.as_posix().rstrip("/") + "/"
    all_tracked = {
        name
        for name in proc.stdout.decode("utf-8", errors="surrogateescape").split("\0")
        if name
    }
    tracked = {
        name[len(prefix) :]
        for name in all_tracked
        if name.startswith(prefix)
    }
    all_modified = {
        name
        for name in modified_proc.stdout.decode(
            "utf-8", errors="surrogateescape"
        ).split("\0")
        if name
    }
    missing_build_inputs = sorted(set(release_pathspecs) - all_tracked)
    if missing_build_inputs:
        raise RuntimeError(
            "Release blocked: release build input is not tracked in Git: "
            + ", ".join(missing_build_inputs)
        )
    modified_build_inputs = sorted(set(release_pathspecs) & all_modified)
    if modified_build_inputs:
        raise RuntimeError(
            "Release blocked: release build input differs from the Git index: "
            + ", ".join(modified_build_inputs)
        )

    modified = {
        name[len(prefix) :]
        for name in all_modified
        if name.startswith(prefix)
    }
    modified_shippable = sorted(
        name
        for name in modified
        if not _should_exclude(Path(name))
        and Path(name).parts[:2] != ("src", "lib")
    )
    if modified_shippable:
        preview = ", ".join(modified_shippable[:10])
        remainder = len(modified_shippable) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: shippable source differs from the Git index: "
            f"{preview}"
        )

    violations: list[str] = []
    for path in sorted(ADDON_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ADDON_DIR)
        if _should_exclude(rel):
            continue
        # The Windows runtime is intentionally generated from the committed,
        # hash-locked wheel requirements immediately before every build.
        if rel.parts[:2] == ("src", "lib"):
            continue
        if rel.as_posix() not in tracked:
            violations.append(rel.as_posix())

    if violations:
        preview = ", ".join(violations[:10])
        remainder = len(violations) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: untracked shippable file is not bound to the "
            f"Git index: {preview}"
        )


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
        "if fitz_version != (1, 28, 0):\n"
        "    raise SystemExit(4)\n"
        "if font_version != (4, 63, 0):\n"
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


def _prune_vendored_pymupdf(lib_dir: Path = VENDORED_LIB_DIR) -> None:
    """Remove PyMuPDF development files that are not needed at runtime."""
    for rel in (
        Path("pymupdf") / "mupdf-devel",
    ):
        path = lib_dir / rel
        if path.exists():
            shutil.rmtree(path)


def ensure_runtime_dependencies(
    *, vendor: bool = True, runtime_dir: Path | None = None
) -> Path:
    """Rebuild runtime dependencies from the committed hash-locked wheels."""
    target_dir = Path(runtime_dir) if runtime_dir is not None else VENDORED_LIB_DIR
    candidates = _candidate_freecad_pythons()
    if not candidates:
        raise RuntimeError("No Python executable found for dependency verification.")

    preferred = candidates[0]
    if not vendor:
        raise RuntimeError(
            "Existing PDFVectorImporter/src/lib bytes are ignored by Git and "
            "therefore are not lock-bound release evidence. Run build_release.py "
            "without --no-vendor-deps to rebuild them from requirements-release.lock."
        )

    py_version = _python_version(preferred)
    if py_version != (3, 11):
        raise RuntimeError(
            f"{preferred} is Python {py_version[0]}.{py_version[1]}; "
            "the reviewed release wheel lock requires CPython 3.11."
        )

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    if not RUNTIME_DEPENDENCY_LOCK.is_file():
        raise RuntimeError(
            f"Hashed release dependency lock is missing: {RUNTIME_DEPENDENCY_LOCK}"
        )

    print(f"Vendoring {', '.join(RUNTIME_DEPENDENCY_SPECS)} into {target_dir}")
    print(f"Using Python: {preferred}")
    subprocess.run(
        [
            str(preferred),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--no-deps",
            "--only-binary",
            ":all:",
            "--target",
            str(target_dir),
            "--requirement",
            str(RUNTIME_DEPENDENCY_LOCK),
        ],
        check=True,
    )
    _prune_vendored_pymupdf(target_dir)

    if not _lib_has_runtime_dependencies(preferred, target_dir):
        raise RuntimeError(
            f"Runtime dependency install completed but import failed from {target_dir}"
        )
    return preferred


def build(out_dir: Path, *, vendor_deps: bool = True) -> Path:
    version = _read_version()
    zip_name = f"FreeCAD-PDF-Importer_v{version}.zip"
    zip_path = out_dir / zip_name

    out_dir.mkdir(parents=True, exist_ok=True)
    _require_no_private_artifacts()
    _require_commit_bound_sources()
    with tempfile.TemporaryDirectory(prefix="fc-release-runtime-") as runtime_tmp:
        runtime_dir = Path(runtime_tmp) / "lib"
        ensure_runtime_dependencies(
            vendor=vendor_deps, runtime_dir=runtime_dir
        )
        _require_no_private_artifacts()

        file_count = 0
        skipped = 0

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for abs_path in sorted(ADDON_DIR.rglob("*")):
                if not abs_path.is_file():
                    continue
                rel = abs_path.relative_to(ADDON_DIR)
                if _should_exclude(rel) or rel.parts[:2] == ("src", "lib"):
                    skipped += 1
                    continue
                # Archive path: PDFVectorImporter/<rel>
                arc_name = Path("PDFVectorImporter") / rel
                _write_deterministic_file(zf, abs_path, arc_name)
                file_count += 1

            for abs_path in sorted(runtime_dir.rglob("*")):
                if not abs_path.is_file():
                    continue
                runtime_rel = abs_path.relative_to(runtime_dir)
                rel = Path("src") / "lib" / runtime_rel
                if _should_exclude(rel):
                    skipped += 1
                    continue
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
        help=(
            "Refuse dependency generation (release builds then fail because ignored "
            "src/lib bytes cannot be trusted without clean hash-locked vendoring)."
        ),
    )
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    zip_path = build(out_dir, vendor_deps=not args.no_vendor_deps)
    print(f"\nRelease ready: {zip_path}")


if __name__ == "__main__":
    main()
