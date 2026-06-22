"""Pytest bootstrap: redirect cache away from locked dirs; skip bundled lib scans."""
from __future__ import annotations

import os
from pathlib import Path

if os.environ.get("PYTEST_CACHE_DIR") is None:
    cache = Path(__file__).resolve().parent / ".pytest_tmp" / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    os.environ["PYTEST_CACHE_DIR"] = str(cache)
