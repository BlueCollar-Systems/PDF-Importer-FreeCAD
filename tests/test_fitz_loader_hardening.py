from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore import fitz_loader  # noqa: E402


def _module(
    *,
    origin: Path,
    version: object = "1.27.2.3",
    missing_api: str | None = None,
) -> ModuleType:
    module = ModuleType("pymupdf")
    module.__file__ = str(origin)
    module.VersionBind = version
    module.open = lambda *args, **kwargs: None
    for name in ("Rect", "Point", "Matrix", "Quad"):
        setattr(module, name, type(name, (), {}))
    if missing_api is not None:
        delattr(module, missing_api)
    return module


def _module_source(*, version: str, source: str, missing_api: str = "") -> str:
    lines = [
        f"VersionBind = {version!r}",
        f"SOURCE = {source!r}",
        "def open(*args, **kwargs): return None",
        "class Rect: pass",
        "class Point: pass",
        "class Matrix: pass",
        "class Quad: pass",
    ]
    if missing_api:
        lines.append(f"del {missing_api}")
    return "\n".join(lines)


def _write_module(
    root: Path,
    name: str,
    *,
    version: str = "1.27.2.3",
    source: str = "preferred",
    missing_api: str = "",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{name}.py").write_text(
        _module_source(version=version, source=source, missing_api=missing_api),
        encoding="utf-8",
    )


def _run_loader_script(tmp_path: Path, script: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT / "PDFVectorImporter")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("cached_version", ["1.23.9", "1.27.2.3"])
def test_preferred_root_replaces_cached_outside_alias_families(
    tmp_path: Path,
    cached_version: str,
) -> None:
    preferred = tmp_path / "preferred"
    outside = tmp_path / "outside"
    _write_module(preferred, "pymupdf")
    result = _run_loader_script(
        tmp_path,
        f"""
        import sys
        from pathlib import Path
        from types import ModuleType
        from pdfcadcore.fitz_loader import import_fitz

        def cached(name):
            module = ModuleType(name)
            module.__file__ = {str(outside / 'pymupdf.py')!r}
            module.VersionBind = {cached_version!r}
            module.open = lambda *args, **kwargs: None
            for api in ('Rect', 'Point', 'Matrix', 'Quad'):
                setattr(module, api, type(api, (), {{}}))
            return module

        old_pymupdf = cached('pymupdf')
        old_fitz = cached('fitz')
        sys.modules['pymupdf'] = old_pymupdf
        sys.modules['pymupdf.extra'] = cached('pymupdf.extra')
        sys.modules['fitz'] = old_fitz
        sys.modules['fitz.extra'] = cached('fitz.extra')
        before = list(sys.path)
        loaded = import_fitz(prefer_lib_dir={str(preferred)!r})
        assert loaded is not old_pymupdf
        assert loaded.SOURCE == 'preferred'
        assert Path(loaded.__file__).resolve().is_relative_to(
            Path({str(preferred)!r}).resolve()
        )
        assert sys.modules['pymupdf'] is loaded
        assert 'pymupdf.extra' not in sys.modules
        assert 'fitz' not in sys.modules
        assert 'fitz.extra' not in sys.modules
        assert sys.path == before
        """,
    )

    assert result.returncode == 0, result.stderr


def test_existing_preferred_root_is_fail_closed(tmp_path: Path) -> None:
    preferred = tmp_path / "preferred"
    outside = tmp_path / "outside"
    _write_module(preferred, "pymupdf", version="1.23.9")
    _write_module(preferred, "fitz", version="1.23.9")
    _write_module(outside, "pymupdf", source="outside")
    _write_module(outside, "fitz", source="outside")
    result = _run_loader_script(
        tmp_path,
        f"""
        import sys
        from pdfcadcore.fitz_loader import import_fitz

        sys.path.insert(0, {str(outside)!r})
        before = list(sys.path)
        try:
            import_fitz(prefer_lib_dir={str(preferred)!r})
        except ImportError:
            pass
        else:
            raise AssertionError('invalid preferred runtime fell back outside root')
        assert sys.path == before
        assert 'pymupdf' not in sys.modules
        assert 'fitz' not in sys.modules
        """,
    )

    assert result.returncode == 0, result.stderr


def test_preferred_import_exception_purges_partial_modules_and_restores_path(
    tmp_path: Path,
) -> None:
    preferred = tmp_path / "preferred"
    preferred.mkdir()
    (preferred / "pymupdf.py").write_text(
        "\n".join(
            (
                "import sys",
                "from types import ModuleType",
                "partial = ModuleType('pymupdf.partial')",
                "partial.__file__ = __file__",
                "sys.modules['pymupdf.partial'] = partial",
                "raise RuntimeError('native bootstrap failed')",
            )
        ),
        encoding="utf-8",
    )
    result = _run_loader_script(
        tmp_path,
        f"""
        import sys
        from pdfcadcore.fitz_loader import import_fitz

        before = list(sys.path)
        try:
            import_fitz(prefer_lib_dir={str(preferred)!r})
        except ImportError:
            pass
        else:
            raise AssertionError('preferred import exception was not normalized')
        assert sys.path == before
        assert 'pymupdf' not in sys.modules
        assert 'pymupdf.partial' not in sys.modules
        assert 'fitz' not in sys.modules
        """,
    )

    assert result.returncode == 0, result.stderr


def test_missing_preferred_root_preserves_global_development_fallback(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    missing = tmp_path / "missing"
    _write_module(outside, "pymupdf", source="outside")
    result = _run_loader_script(
        tmp_path,
        f"""
        import sys
        from pdfcadcore.fitz_loader import import_fitz

        sys.path.insert(0, {str(outside)!r})
        before = list(sys.path)
        loaded = import_fitz(prefer_lib_dir={str(missing)!r})
        assert loaded.SOURCE == 'outside'
        assert sys.path == before
        """,
    )

    assert result.returncode == 0, result.stderr


def test_preferred_root_origin_check_uses_resolved_path_boundaries(
    tmp_path: Path,
) -> None:
    preferred = tmp_path / "lib"
    sibling = tmp_path / "lib-escape"
    preferred.mkdir()
    sibling.mkdir()

    assert fitz_loader._module_origin_within(  # noqa: SLF001
        _module(origin=preferred / "pymupdf.py"),
        preferred,
    )
    assert not fitz_loader._module_origin_within(  # noqa: SLF001
        _module(origin=sibling / "pymupdf.py"),
        preferred,
    )


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.24.0", True),
        ("1.27.2.3", True),
        ("1.23.99", False),
        ("2.0.0", False),
        ("1.24", False),
        ("1.27.2.post1", False),
        (" 1.27.2", False),
        (1.27, False),
    ],
)
def test_loader_validates_exact_versionbind(
    tmp_path: Path,
    version: object,
    expected: bool,
) -> None:
    assert (
        fitz_loader._module_is_supported(  # noqa: SLF001
            _module(origin=tmp_path / "pymupdf.py", version=version)
        )
        is expected
    )


@pytest.mark.parametrize("missing_api", ["open", "Rect", "Point", "Matrix", "Quad"])
def test_loader_rejects_missing_required_api(
    tmp_path: Path,
    missing_api: str,
) -> None:
    assert not fitz_loader._module_is_supported(  # noqa: SLF001
        _module(origin=tmp_path / "pymupdf.py", missing_api=missing_api)
    )
