"""Shared fixtures for the tests in this folder."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def freecad_user_home():
    """A short, private FreeCAD user folder for one real-FreeCAD probe run.

    FreeCAD keeps its per-document temp files, this add-on's raster cache and
    a copy of every import report under its user folder. Inside pytest's
    tmp_path those paths passed the 260-character Windows path limit whenever
    ``--basetemp`` was long, so the folder is made directly under %TEMP%
    instead (``bcfc-`` plus 8 characters). It is private, so the developer's
    own FreeCAD profile and its "PDF Import Reports" folder are never
    touched, and it is removed when the test ends.
    """
    base = os.environ.get("TEMP") or os.environ.get("TMP") or None
    home = tempfile.mkdtemp(prefix="bcfc-", dir=base)
    try:
        yield Path(home)
    finally:
        shutil.rmtree(home, ignore_errors=True)
