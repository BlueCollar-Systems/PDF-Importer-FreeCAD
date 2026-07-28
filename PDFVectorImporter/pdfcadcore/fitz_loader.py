# -*- coding: utf-8 -*-
"""Import PyMuPDF with validation (skip namespace-only pymupdf stubs)."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import re
import sys
from typing import Any, List, Optional


class PdfOpenError(Exception):
    """Typed rejection for malformed, empty, or encrypted PDFs at open time."""

    def __init__(self, reason: str, message: str) -> None:
        self.reason = reason
        super().__init__(message)


_PYMUPDF_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2,}")
_PYMUPDF_MIN_VERSION = (1, 24, 0)
_PYMUPDF_MAX_VERSION = (2, 0, 0)
_PYMUPDF_TYPE_APIS = ("Rect", "Point", "Matrix", "Quad")
_PYMUPDF_MODULE_NAMES = ("pymupdf", "fitz")


def _module_has_open(mod: Any) -> bool:
    return mod is not None and callable(getattr(mod, "open", None))


def _module_is_supported(mod: Any) -> bool:
    version = getattr(mod, "VersionBind", None)
    if type(version) is not str or _PYMUPDF_VERSION_RE.fullmatch(version) is None:
        return False
    version_tuple = tuple(int(part) for part in version.split(".")[:3])
    return bool(
        _PYMUPDF_MIN_VERSION <= version_tuple < _PYMUPDF_MAX_VERSION
        and _module_has_open(mod)
        and all(
            isinstance(getattr(mod, name, None), type)
            for name in _PYMUPDF_TYPE_APIS
        )
    )


def _resolved_path(value: Any) -> Path:
    return Path(os.path.realpath(os.path.abspath(os.fspath(value))))


def _path_within(path: Any, root: Path) -> bool:
    try:
        resolved = os.path.normcase(str(_resolved_path(path)))
        resolved_root = os.path.normcase(str(_resolved_path(root)))
        return os.path.commonpath((resolved, resolved_root)) == resolved_root
    except (OSError, TypeError, ValueError):
        return False


def _module_filesystem_origins(mod: Any) -> List[str]:
    origins: List[str] = []
    module_file = getattr(mod, "__file__", None)
    if type(module_file) is str and module_file:
        origins.append(module_file)
    spec = getattr(mod, "__spec__", None)
    spec_origin = getattr(spec, "origin", None)
    if (
        type(spec_origin) is str
        and spec_origin
        and spec_origin not in {"built-in", "frozen"}
        and spec_origin not in origins
    ):
        origins.append(spec_origin)
    return origins


def _module_origin_within(mod: Any, root: Path) -> bool:
    origins = _module_filesystem_origins(mod)
    return bool(origins and all(_path_within(origin, root) for origin in origins))


def _is_fitz_family_name(name: str) -> bool:
    return any(name == root or name.startswith(f"{root}.") for root in _PYMUPDF_MODULE_NAMES)


def _purge_fitz_modules() -> None:
    for name in tuple(sys.modules):
        if _is_fitz_family_name(name):
            sys.modules.pop(name, None)


def _loaded_fitz_origins_within(root: Path) -> bool:
    for name, module in tuple(sys.modules.items()):
        if not _is_fitz_family_name(name) or module is None:
            continue
        origins = _module_filesystem_origins(module)
        if origins and not all(_path_within(origin, root) for origin in origins):
            return False
    return True


def _preferred_cached_module(root: Path) -> Any:
    for name in _PYMUPDF_MODULE_NAMES:
        module = sys.modules.get(name)
        if (
            _module_is_supported(module)
            and _module_origin_within(module, root)
            and _loaded_fitz_origins_within(root)
        ):
            return module
    return None


def _import_preferred_fitz(root: Path) -> Any:
    cached = _preferred_cached_module(root)
    if cached is not None:
        return cached
    _purge_fitz_modules()
    saved = list(sys.path)
    last_exc: Optional[BaseException] = None
    try:
        sys.path[:] = [path for path in sys.path if not _path_within(path, root)]
        sys.path.insert(0, str(root))
        importlib.invalidate_caches()
        for name in _PYMUPDF_MODULE_NAMES:
            try:
                module = importlib.import_module(name)
                if (
                    _module_is_supported(module)
                    and _module_origin_within(module, root)
                    and _loaded_fitz_origins_within(root)
                ):
                    return module
            except Exception as exc:
                last_exc = exc
            _purge_fitz_modules()
            importlib.invalidate_caches()
    finally:
        sys.path[:] = saved
    message = "PyMuPDF (fitz) preferred runtime is invalid or unavailable"
    if last_exc is not None:
        raise ImportError(message) from last_exc
    raise ImportError(message)


def import_fitz(*, prefer_lib_dir: Optional[str] = None) -> Any:
    """Return a validated PyMuPDF module, honoring an existing preferred root."""
    if prefer_lib_dir:
        preferred_root = _resolved_path(prefer_lib_dir)
        if preferred_root.exists():
            if not preferred_root.is_dir():
                raise ImportError("PyMuPDF preferred runtime root is not a directory")
            return _import_preferred_fitz(preferred_root)
    last_exc: Optional[BaseException] = None
    for name in _PYMUPDF_MODULE_NAMES:
        try:
            module = importlib.import_module(name)
            if _module_is_supported(module):
                return module
        except Exception as exc:
            last_exc = exc
        _purge_fitz_modules()
        importlib.invalidate_caches()

    msg = "PyMuPDF (fitz) is not available"
    if last_exc is not None:
        raise ImportError(msg) from last_exc
    raise ImportError(msg)


def _classify_open_failure(exc: BaseException) -> PdfOpenError:
    name = type(exc).__name__
    msg = str(exc).lower()
    if "password" in msg or "encrypt" in msg:
        return PdfOpenError(
            "password_protected",
            "This PDF is password-protected; supply credentials to import.",
        )
    if name in {"EmptyFileError"}:
        return PdfOpenError("empty_file", "File is empty — not a valid PDF.")
    if name in {"FileDataError", "FileNotFoundError"}:
        return PdfOpenError("not_a_pdf", "This file is not a valid PDF.")
    return PdfOpenError("not_a_pdf", "This file is not a valid PDF.")


def safe_open(path: str, *, prefer_lib_dir: Optional[str] = None) -> Any:
    """Open a PDF with clean typed errors instead of raw PyMuPDF tracebacks."""
    pdf_path = str(path)
    if not os.path.exists(pdf_path):
        raise PdfOpenError("empty_file", f"File not found: {pdf_path}")
    if os.path.getsize(pdf_path) == 0:
        raise PdfOpenError("empty_file", "File is empty — not a valid PDF.")
    try:
        with open(pdf_path, "rb") as handle:
            header = handle.read(1024)
    except OSError as exc:
        raise _classify_open_failure(exc) from exc
    if b"%PDF-" not in header:
        raise PdfOpenError("not_a_pdf", "This file is not a valid PDF.")

    fitz = import_fitz(prefer_lib_dir=prefer_lib_dir)
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:  # noqa: BLE001 — normalize host-facing open failures
        raise _classify_open_failure(exc) from exc

    if getattr(doc, "needs_pass", False) or getattr(doc, "is_encrypted", False):
        doc.close()
        raise PdfOpenError(
            "password_protected",
            "This PDF is password-protected; supply credentials to import.",
        )
    if int(getattr(doc, "page_count", 0) or 0) == 0:
        doc.close()
        raise PdfOpenError("corrupt", "PDF has no readable pages.")
    return doc


def safe_open_bytes(source_bytes: Any, *, prefer_lib_dir: Optional[str] = None) -> Any:
    """Open one immutable PDF byte snapshot through the validated runtime."""

    if not isinstance(source_bytes, (bytes, bytearray, memoryview)):
        raise PdfOpenError("not_a_pdf", "PDF source must be an immutable byte snapshot.")
    payload = bytes(source_bytes)
    if not payload:
        raise PdfOpenError("empty_file", "File is empty — not a valid PDF.")
    if b"%PDF-" not in payload[:1024]:
        raise PdfOpenError("not_a_pdf", "This file is not a valid PDF.")

    fitz = import_fitz(prefer_lib_dir=prefer_lib_dir)
    try:
        doc = fitz.open(stream=payload, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 — normalize host-facing open failures
        raise _classify_open_failure(exc) from exc

    if getattr(doc, "needs_pass", False) or getattr(doc, "is_encrypted", False):
        doc.close()
        raise PdfOpenError(
            "password_protected",
            "This PDF is password-protected; supply credentials to import.",
        )
    if int(getattr(doc, "page_count", 0) or 0) == 0:
        doc.close()
        raise PdfOpenError("corrupt", "PDF has no readable pages.")
    return doc


def sample_process_mb() -> float:
    """Best-effort working-set sample for import_report peak_mb telemetry."""
    try:
        if sys.platform == "win32":
            import ctypes

            class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = PROCESS_MEMORY_COUNTERS_EX()
            counters.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            ):
                return round(counters.WorkingSetSize / (1024.0 * 1024.0), 2)
        elif os.path.isfile("/proc/self/status"):
            with open("/proc/self/status", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        return round(kb / 1024.0, 2)
    except Exception:
        pass
    return 0.0
