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
  PDFVectorImporter_v<VERSION>.zip  (next to this script, or --out dir)
"""

import argparse
import re
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


def _should_exclude(rel: Path) -> bool:
    for part in rel.parts:
        if part in EXCLUDE_DIRS:
            return True
    if rel.name in EXCLUDE_FILES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def _read_version() -> str:
    pkg_xml = ADDON_DIR / "package.xml"
    if pkg_xml.exists():
        text = pkg_xml.read_text(encoding="utf-8")
        m = re.search(r"<version>(.*?)</version>", text)
        if m:
            return m.group(1).strip()
    return "0.0.0"


def build(out_dir: Path) -> Path:
    version = _read_version()
    zip_name = f"PDFVectorImporter_v{version}.zip"
    zip_path = out_dir / zip_name

    out_dir.mkdir(parents=True, exist_ok=True)

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
            zf.write(abs_path, arc_name)
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
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    zip_path = build(out_dir)
    print(f"\nRelease ready: {zip_path}")


if __name__ == "__main__":
    main()
