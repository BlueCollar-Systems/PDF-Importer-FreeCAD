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

sys.path.insert(0, str(ADDON_ROOT))

from pdfcadcore.preflight_copy import preflight_paragraph  # noqa: E402
from PDFVectorImporter.runtime_paths import (  # noqa: E402
    activate_bundled_runtime_if_available,
)


def _pymupdf_status() -> tuple[bool, str]:
    """Return whether the exact ABI-matched bundled runtime imports."""
    saved_path = list(sys.path)
    try:
        runtime = activate_bundled_runtime_if_available(ADDON_ROOT)
        if runtime is None:
            return False, "no compatible bundled runtime for this Python host"
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore
        import fontTools  # type: ignore
        version = getattr(fitz, "__version__", "") or getattr(fitz, "VersionBind", "")
        return True, (
            f"bundled {runtime.runtime_tag} runtime import OK "
            f"(PyMuPDF {version or 'version unknown'}, "
            f"fontTools {getattr(fontTools, '__version__', 'version unknown')})"
        )
    except Exception as exc:  # noqa: BLE001 - diagnostic command should report exact import failure.
        return False, f"bundled runtime import failed: {exc}"
    finally:
        sys.path[:] = saved_path


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
        help="Also verify the ABI-matched bundled PyMuPDF/fontTools runtime",
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
