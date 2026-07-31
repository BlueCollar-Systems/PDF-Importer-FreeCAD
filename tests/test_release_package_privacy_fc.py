from pathlib import Path

import build_release


def test_release_filter_excludes_addon_test_packages():
    assert build_release._should_exclude(
        Path("tests") / "test_fc_style_fixes.py"
    )
    assert not build_release._should_exclude(
        Path("src") / "PDFImporterCore.py"
    )
