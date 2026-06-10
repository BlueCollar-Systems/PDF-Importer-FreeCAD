# -*- coding: utf-8 -*-
# DEPRECATED compatibility shim — use pdfcadcore.document_profiler instead.
from __future__ import annotations
import warnings as _warnings
_warnings.warn(
    "PDFDocumentProfiler is deprecated; import from pdfcadcore.document_profiler instead.",
    DeprecationWarning,
    stacklevel=2,
)
try:
    from PDFVectorImporter.pdfcadcore.document_profiler import *  # noqa: F401,F403
except ImportError:
    from pdfcadcore.document_profiler import *  # noqa: F401,F403
