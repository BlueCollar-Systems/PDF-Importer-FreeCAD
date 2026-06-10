# -*- coding: utf-8 -*-
# DEPRECATED compatibility shim — use pdfcadcore.dimension_parser instead.
from __future__ import annotations
import warnings as _warnings
_warnings.warn(
    "PDFDimensionParser is deprecated; import from pdfcadcore.dimension_parser instead.",
    DeprecationWarning,
    stacklevel=2,
)
try:
    from PDFVectorImporter.pdfcadcore.dimension_parser import *  # noqa: F401,F403
except ImportError:
    from pdfcadcore.dimension_parser import *  # noqa: F401,F403
