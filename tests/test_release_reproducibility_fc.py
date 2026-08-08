import base64
import os
import zipfile
from pathlib import Path

import build_release


def _encoded_synthetic_private_denylist() -> str:
    term = "PRIVATE-SYNTHETIC-" + "DRAWING-ALPHA"
    return base64.b64encode(
        (f'{{"schema":"bcs.private-release-denylist/1.0","terms":["{term}"]}}').encode(
            "utf-8"
        )
    ).decode("ascii")


def test_release_omits_environment_bound_launchers_and_is_reproducible(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "BCS_PRIVATE_RELEASE_DENYLIST_B64",
        _encoded_synthetic_private_denylist(),
    )
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    module = addon / "module.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )

    def provide_locked_runtime(*, runtime_dir, **_kwargs):
        (runtime_dir / "bin").mkdir(parents=True)
        for runtime_tag in ("common", "cp310", "cp311"):
            launcher_dir = runtime_dir / runtime_tag / "bin"
            launcher_dir.mkdir(parents=True)
            (launcher_dir / f"{runtime_tag}-tool.exe").write_bytes(
                b"#!C:\\build-host\\python.exe\n"
            )
            package_dir = runtime_dir / runtime_tag / "example_package"
            package_dir.mkdir()
            (package_dir / "__init__.py").write_text(
                f"RUNTIME = {runtime_tag!r}\n", encoding="utf-8"
            )
        dist_info = runtime_dir / "example-1.0.dist-info"
        dist_info.mkdir(parents=True)
        (runtime_dir / "bin" / "tool.exe").write_bytes(
            b"#!C:\\build-host\\python.exe\n"
        )
        (dist_info / "RECORD").write_text(
            "environment-specific\n", encoding="utf-8"
        )
        (dist_info / "METADATA").write_text("Name: example\n", encoding="utf-8")
        return Path(os.devnull)

    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", provide_locked_runtime
    )
    monkeypatch.setattr(
        build_release,
        "_require_commit_bound_sources",
        lambda _snapshot, *, source_commit: None,
    )

    first = build_release.build(tmp_path / "first")
    os.utime(module, (module.stat().st_atime + 3600, module.stat().st_mtime + 3600))
    second = build_release.build(tmp_path / "second")

    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert "PDFVectorImporter/module.py" in names
        assert "PDFVectorImporter/src/lib/example-1.0.dist-info/METADATA" in names
        assert all(
            f"PDFVectorImporter/src/lib/{runtime_tag}/example_package/__init__.py"
            in names
            for runtime_tag in ("common", "cp310", "cp311")
        )
        assert not any("/bin/" in name for name in names)
        assert not any(name.endswith(".dist-info/RECORD") for name in names)
        assert all(
            member.compress_type == zipfile.ZIP_STORED for member in archive.infolist()
        )


def test_release_archives_verified_runtime_snapshot_not_later_mutation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv(
        "BCS_PRIVATE_RELEASE_DENYLIST_B64",
        _encoded_synthetic_private_denylist(),
    )
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release,
        "_require_commit_bound_sources",
        lambda _snapshot, *, source_commit: None,
    )

    def provide_runtime(*, runtime_dir, **_kwargs):
        package = runtime_dir / "common" / "example_package"
        package.mkdir(parents=True)
        (package / "payload.bin").write_bytes(b"verified runtime bytes")
        return {}

    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", provide_runtime
    )
    capture = getattr(build_release, "_capture_runtime_files", None)
    assert callable(capture), "release builder must snapshot verified runtime bytes"

    def capture_then_mutate(runtime_dir, external_terms):
        snapshot = capture(runtime_dir, external_terms)
        (runtime_dir / "common" / "example_package" / "payload.bin").write_bytes(
            b"late unverified mutation"
        )
        return snapshot

    monkeypatch.setattr(
        build_release, "_capture_runtime_files", capture_then_mutate
    )

    archive = build_release.build(tmp_path / "out")

    with zipfile.ZipFile(archive) as package:
        assert package.read(
            "PDFVectorImporter/src/lib/common/example_package/payload.bin"
        ) == b"verified runtime bytes"
