from __future__ import annotations

import importlib.util
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


def test_diagnostics_reports_bundled_pymupdf(capsys) -> None:
    module = _load_preflight_module()

    result = module.main(["--diagnostics"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert result == 0
    assert "bundled PyMuPDF import OK" in output
