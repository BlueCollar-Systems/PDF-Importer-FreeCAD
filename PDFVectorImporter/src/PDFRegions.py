# -*- coding: utf-8 -*-
# DEPRECATED compatibility shim — use pdfcadcore.regions instead.
from __future__ import annotations
import warnings as _warnings
_warnings.warn(
    "PDFRegions is deprecated; import from pdfcadcore.regions instead.",
    DeprecationWarning,
    stacklevel=2,
)
try:
    from PDFVectorImporter.pdfcadcore.regions import *  # noqa: F401,F403
except ImportError:
    from pdfcadcore.regions import *  # noqa: F401,F403
