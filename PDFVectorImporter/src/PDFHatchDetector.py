# -*- coding: utf-8 -*-
# DEPRECATED compatibility shim — use pdfcadcore.hatch_detector instead.
from __future__ import annotations
import warnings as _warnings
_warnings.warn(
    "PDFHatchDetector is deprecated; import from pdfcadcore.hatch_detector instead.",
    DeprecationWarning,
    stacklevel=2,
)
try:
    from PDFVectorImporter.pdfcadcore.hatch_detector import *  # noqa: F401,F403
except ImportError:
    from pdfcadcore.hatch_detector import *  # noqa: F401,F403
