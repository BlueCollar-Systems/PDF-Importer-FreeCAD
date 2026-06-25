from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_preflight_module():
    module_path = ROOT / "preflight_check.py"
    spec = importlib.util.spec_from_file_location("fc_preflight_check", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_prints_freecad_guidance(capsys) -> None:
    module = _load_preflight_module()

    assert module.main([]) == 0

    out = capsys.readouterr().out
    assert "Professional import" in out
    assert "FreeCAD" in out
    assert "verify one known dimension" in out


def test_diagnostics_reports_bundled_pymupdf(capsys, monkeypatch, tmp_path) -> None:
    module = _load_preflight_module()
    vendored_lib = tmp_path / "lib"
    pymupdf_pkg = vendored_lib / "pymupdf"
    pymupdf_pkg.mkdir(parents=True)
    (pymupdf_pkg / "__init__.py").write_text("__version__ = 'test-vendored'\n", encoding="utf-8")

    monkeypatch.setattr(module, "VENDORED_LIB", vendored_lib)
    monkeypatch.delitem(sys.modules, "pymupdf", raising=False)
    monkeypatch.delitem(sys.modules, "fitz", raising=False)

    result = module.main(["--diagnostics"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert result == 0
    assert "bundled PyMuPDF import OK" in output
