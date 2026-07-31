import os
import zipfile
from pathlib import Path

import build_release


def test_release_omits_environment_bound_launchers_and_is_reproducible(
    monkeypatch, tmp_path
):
    addon = tmp_path / "PDFVectorImporter"
    (addon / "src" / "lib" / "bin").mkdir(parents=True)
    dist_info = addon / "src" / "lib" / "example-1.0.dist-info"
    dist_info.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    module = addon / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    (addon / "src" / "lib" / "bin" / "tool.exe").write_bytes(
        b"#!C:\\build-host\\python.exe\n"
    )
    (dist_info / "RECORD").write_text("environment-specific\n", encoding="utf-8")
    (dist_info / "METADATA").write_text("Name: example\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release,
        "ensure_runtime_dependencies",
        lambda **_kwargs: Path(os.devnull),
    )

    first = build_release.build(tmp_path / "first")
    os.utime(module, (module.stat().st_atime + 3600, module.stat().st_mtime + 3600))
    second = build_release.build(tmp_path / "second")

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert "PDFVectorImporter/module.py" in names
        assert "PDFVectorImporter/src/lib/example-1.0.dist-info/METADATA" in names
        assert not any("/src/lib/bin/" in name for name in names)
        assert not any(name.endswith(".dist-info/RECORD") for name in names)
