#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# preflight_check.py — one-click pre-import guidance for FreeCAD PDF Importer
# Copyright (c) 2024-2026 BlueCollar Systems — BUILT. NOT BOUGHT.
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ADDON_ROOT = ROOT / "PDFVectorImporter"
VENDORED_LIB = ADDON_ROOT / "src" / "lib"

sys.path.insert(0, str(ADDON_ROOT))

from pdfcadcore.preflight_copy import preflight_paragraph  # noqa: E402


def _pymupdf_status() -> tuple[bool, str]:
    """Return whether bundled PyMuPDF imports from the add-on vendor path."""

    if not VENDORED_LIB.is_dir():
        return False, f"vendored library folder missing: {VENDORED_LIB}"

    inserted = False
    vendor_path = str(VENDORED_LIB)
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)
        inserted = True
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore
        version = getattr(fitz, "__version__", "") or getattr(fitz, "VersionBind", "")
        return True, f"bundled PyMuPDF import OK ({version or 'version unknown'})"
    except Exception as exc:  # noqa: BLE001 - diagnostic command should report exact import failure.
        return False, f"bundled PyMuPDF import failed: {exc}"
    finally:
        if inserted:
            try:
                sys.path.remove(vendor_path)
            except ValueError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print FreeCAD PDF Importer pre-import guidance")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="Print pre-import guidance and exit (alias for default behavior)",
    )
    parser.add_argument(
        "--diagnostics",
        action="store_true",
        help="Also verify bundled PyMuPDF imports from PDFVectorImporter/src/lib",
    )
    args = parser.parse_args(argv)

    print(preflight_paragraph("freecad"))

    if args.diagnostics:
        ok, message = _pymupdf_status()
        stream = sys.stdout if ok else sys.stderr
        print(f"[PDF Vector Importer] {message}", file=stream)
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
