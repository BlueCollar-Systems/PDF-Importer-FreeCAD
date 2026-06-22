"""Pytest bootstrap: redirect cache away from locked dirs; skip bundled lib scans."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_repo_tmp = Path(__file__).resolve().parent / ".pytest_tmp"
_temp_root = _repo_tmp / "root"
_temp_root.mkdir(parents=True, exist_ok=True)

os.environ.setdefault("PYTEST_DEBUG_TEMPROOT", str(_temp_root))
os.environ.setdefault("TMP", str(_temp_root))
os.environ.setdefault("TEMP", str(_temp_root))
tempfile.tempdir = str(_temp_root)

if os.environ.get("PYTEST_CACHE_DIR") is None:
    cache = _repo_tmp / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_CACHE_DIR"] = str(cache)
