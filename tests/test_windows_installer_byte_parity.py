from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import build_windows_installer


def _write_release_zip(path: Path, *, package_version: str = "4.0.72") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "PDFVectorImporter/package.xml",
            f"<package><version>{package_version}</version></package>",
        )
        archive.writestr("PDFVectorImporter/payload.bin", b"canonical-release-bytes")


def test_stage_release_uses_canonical_zip_without_rebuilding(monkeypatch, tmp_path):
    version = "4.0.72"
    dist_dir = tmp_path / "dist"
    stage_dir = dist_dir / "installer_stage"
    source_zip = dist_dir / f"FreeCAD-PDF-Importer_v{version}.zip"
    dist_dir.mkdir()
    _write_release_zip(source_zip)
    source_hash_before = source_zip.read_bytes()

    monkeypatch.setattr(build_windows_installer, "DIST_DIR", dist_dir)
    monkeypatch.setattr(build_windows_installer, "STAGE_DIR", stage_dir)
    monkeypatch.setattr(build_windows_installer, "read_version", lambda: version)

    def fail_rebuild(*_args, **_kwargs):
        raise AssertionError("canonical release ZIP must not be rebuilt")

    monkeypatch.setattr(build_windows_installer.build_release, "build", fail_rebuild)

    actual_version, source_dir, actual_zip = (
        build_windows_installer.stage_release(source_zip)
    )

    assert actual_version == version
    assert actual_zip == source_zip.resolve()
    assert source_zip.read_bytes() == source_hash_before
    assert (source_dir / "payload.bin").read_bytes() == b"canonical-release-bytes"


def test_stage_release_rejects_payload_with_wrong_package_version(
    monkeypatch, tmp_path
):
    version = "4.0.72"
    source_zip = tmp_path / f"FreeCAD-PDF-Importer_v{version}.zip"
    _write_release_zip(source_zip, package_version="4.0.71")
    monkeypatch.setattr(build_windows_installer, "DIST_DIR", tmp_path / "dist")
    monkeypatch.setattr(
        build_windows_installer,
        "STAGE_DIR",
        tmp_path / "dist" / "installer_stage",
    )
    monkeypatch.setattr(build_windows_installer, "read_version", lambda: version)

    with pytest.raises(RuntimeError, match="Staged package version mismatch"):
        build_windows_installer.stage_release(source_zip)


def test_auto_release_publishes_zip_and_installer_atomically():
    workflow = (
        Path(build_windows_installer.REPO_ROOT)
        / ".github"
        / "workflows"
        / "auto-release.yml"
    ).read_text(encoding="utf-8")

    install_inno = workflow.index("- name: Install Inno Setup")
    build_installer = workflow.index(
        'build_windows_installer.py --source-zip "${ZIP}"'
    )
    publish = workflow.index("gh release create")
    assert install_inno < build_installer < publish
    assert 'SETUP="dist/FreeCAD-PDF-Importer-Setup_v' in workflow
    assert '"${ZIP}" "${SETUP}"' in workflow
    assert "gh workflow run windows-release.yml" not in workflow


def test_obsolete_second_stage_windows_release_workflow_is_removed():
    workflow = (
        Path(build_windows_installer.REPO_ROOT)
        / ".github"
        / "workflows"
        / "windows-release.yml"
    )
    assert not workflow.exists(), (
        "Setup.exe is published atomically by auto-release; a second-stage "
        "publisher is redundant and incompatible with immutable releases"
    )

    readme = (Path(build_windows_installer.REPO_ROOT) / "README.md").read_text(
        encoding="utf-8"
    )
    assert "workflow `windows-release`" not in readme
    assert "`auto-release` workflow builds and publishes both artifacts" in readme
