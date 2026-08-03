from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
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
    common = tmp_path / "common"
    abi = tmp_path / "cp311"
    pymupdf_pkg = common / "pymupdf"
    fonttools_pkg = abi / "fontTools"
    pymupdf_pkg.mkdir(parents=True)
    fonttools_pkg.mkdir(parents=True)
    (pymupdf_pkg / "__init__.py").write_text("__version__ = 'test-vendored'\n", encoding="utf-8")
    (fonttools_pkg / "__init__.py").write_text("__version__ = 'test-fonttools'\n", encoding="utf-8")

    @dataclass
    class Runtime:
        runtime_tag: str = "cp311"

    def activate(_root):
        sys.path[:0] = [str(abi), str(common)]
        return Runtime()

    monkeypatch.setattr(module, "activate_bundled_runtime_if_available", activate)
    monkeypatch.delitem(sys.modules, "pymupdf", raising=False)
    monkeypatch.delitem(sys.modules, "fitz", raising=False)
    monkeypatch.delitem(sys.modules, "fontTools", raising=False)

    result = module.main(["--diagnostics"])

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert result == 0
    assert "bundled cp311 runtime import OK" in output
