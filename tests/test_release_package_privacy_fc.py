from pathlib import Path

import build_release


def test_release_filter_excludes_addon_test_packages():
    assert build_release._should_exclude(
        Path("tests") / "test_fc_style_fixes.py"
    )
    assert not build_release._should_exclude(
        Path("src") / "PDFImporterCore.py"
    )


def test_release_filter_excludes_environment_bound_dependency_metadata():
    assert build_release._should_exclude(
        Path("src") / "lib" / "bin" / "pymupdf.exe"
    )
    assert build_release._should_exclude(
        Path("src") / "lib" / "pymupdf-1.28.0.dist-info" / "RECORD"
    )
