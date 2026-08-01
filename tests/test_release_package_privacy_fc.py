from pathlib import Path

import build_release
import pytest


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


@pytest.mark.parametrize(
    "relative_path",
    [
        "private.pdf",
        "private.dxf",
        "private.dwg",
        "private.skp",
        "private.FCStd",
        "private.blend",
        "private.zip",
        "result_import_report.json",
        "Imported Evidence/result.json",
        "test-corpus/fixture.txt",
    ],
)
def test_release_build_fails_closed_on_private_artifact_candidates(
    monkeypatch, tmp_path, relative_path
):
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    candidate = addon / relative_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"private validation artifact")

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    # This test isolates the privacy boundary from the separate tracked-source
    # boundary. ``raising=False`` keeps the test RED before that boundary exists.
    monkeypatch.setattr(
        build_release, "_require_commit_bound_sources", lambda: None, raising=False
    )

    with pytest.raises(RuntimeError, match="private corpus artifact"):
        build_release.build(tmp_path / "out")
