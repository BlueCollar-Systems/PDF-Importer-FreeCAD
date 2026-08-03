# PDFVectorImporter — FreeCAD Workbench
# BlueCollar Systems — BUILT. NOT BOUGHT.
import os
import sys

try:
    _base = os.path.dirname(os.path.abspath(__file__))
except NameError:
    # __file__ not defined (shouldn't happen for __init__.py, but be safe)
    import FreeCAD
    _base = os.path.join(FreeCAD.getUserAppDataDir(), "Mod", "PDFVectorImporter")

_src = os.path.join(_base, "src")

for _p in (_base, _src):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

from .runtime_paths import activate_bundled_runtime_if_available

_bundled_runtime = activate_bundled_runtime_if_available(_base)
