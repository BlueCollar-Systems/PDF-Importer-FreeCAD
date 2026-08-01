from __future__ import annotations

import hashlib
import json
import re
import zipfile
from pathlib import Path

import pytest

import build_windows_installer


TOOLCHAIN_MANIFEST = (
    Path(build_windows_installer.REPO_ROOT)
    / "installer"
    / "inno-toolchain-6.7.1.json"
)


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

    install_inno = workflow.index("- name: Install pinned Inno Setup toolchain")
    build_installer = workflow.index("build_windows_installer.py")
    publish = workflow.index("publish_release.py")
    assert install_inno < build_installer < publish
    assert 'SETUP="dist/FreeCAD-PDF-Importer-Setup_v' in workflow
    assert 'ATTESTATION="dist/FreeCAD-PDF-Importer-Setup_v' in workflow
    assert '--asset "${ZIP}" --asset "${SETUP}" --asset "${ATTESTATION}"' in workflow
    assert "gh workflow run windows-release.yml" not in workflow


def test_inno_toolchain_manifest_pins_vendor_distribution_and_full_tree():
    manifest = json.loads(TOOLCHAIN_MANIFEST.read_text(encoding="utf-8"))
    assert manifest["schema"] == "bcs.inno_toolchain/1.0"
    assert manifest["version"] == "6.7.1"
    assert manifest["source_url"] == (
        "https://github.com/jrsoftware/issrc/releases/download/"
        "is-6_7_1/innosetup-6.7.1.exe"
    )
    assert manifest["source_sha256"] == (
        "4d11e8050b6185e0d49bd9e8cc661a7a59f44959a621d31d11033124c4e8a7b0"
    )
    assert manifest["source_authenticode_subject"].startswith("CN=Pyrsys B.V.")
    assert manifest["source_authenticode_thumbprint"] == (
        "e0ab19c8d38cbf9c44709925122a7a02f8c70cb7"
    )
    files = manifest["files"]
    assert len(files) >= 40, "the complete portable compiler tree must be attested"
    names = [item["path"] for item in files]
    assert len(names) == len(set(names))
    assert {"ISCC.exe", "ISCmplr.dll", "ISPP.dll", "Setup.e32", "SetupLdr.e32"} <= set(names)
    assert all(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) for item in files)


def test_toolchain_verification_is_exact_and_fails_closed(tmp_path):
    root = tmp_path / "inno"
    root.mkdir()
    files = {
        "ISCC.exe": b"compiler",
        "ISCmplr.dll": b"engine",
        "Setup.e32": b"stub",
    }
    entries = []
    for name, payload in files.items():
        (root / name).write_bytes(payload)
        entries.append(
            {
                "path": name,
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "bcs.inno_toolchain/1.0",
                "name": "Inno Setup",
                "version": "test",
                "source_url": "https://example.invalid/inno.exe",
                "source_sha256": "0" * 64,
                "files": entries,
            }
        ),
        encoding="utf-8",
    )

    identity = build_windows_installer.verify_inno_toolchain(
        root / "ISCC.exe", manifest_path
    )
    assert identity["version"] == "test"
    assert re.fullmatch(r"[0-9a-f]{64}", identity["tree_sha256"])

    (root / "ISCmplr.dll").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match="toolchain .* mismatch"):
        build_windows_installer.verify_inno_toolchain(root / "ISCC.exe", manifest_path)


def test_attestation_is_canonical_and_binds_zip_setup_toolchain(tmp_path):
    release_zip = tmp_path / "payload.zip"
    setup = tmp_path / "setup.exe"
    release_zip.write_bytes(b"zip")
    setup.write_bytes(b"setup")
    identity = {
        "name": "Inno Setup",
        "version": "6.7.1",
        "source_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "tree_sha256": "c" * 64,
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    build_windows_installer.write_attestation(
        first,
        release_zip=release_zip,
        installer_exe=setup,
        toolchain_identity=identity,
        source_commit="d" * 40,
    )
    build_windows_installer.write_attestation(
        second,
        release_zip=release_zip,
        installer_exe=setup,
        toolchain_identity=identity,
        source_commit="d" * 40,
    )

    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert payload["schema"] == "bcs.freecad_installer_attestation/1.0"
    assert payload["source_zip"]["sha256"] == hashlib.sha256(b"zip").hexdigest()
    assert payload["installer"]["sha256"] == hashlib.sha256(b"setup").hexdigest()
    assert payload["toolchain"] == identity


def test_workflow_bootstraps_exact_toolchain_and_compares_two_builds():
    workflow = (
        Path(build_windows_installer.REPO_ROOT)
        / ".github"
        / "workflows"
        / "auto-release.yml"
    ).read_text(encoding="utf-8")
    assert "choco install innosetup" not in workflow
    assert "scripts/install_inno_toolchain.ps1" in workflow
    assert "installer/inno-toolchain-6.7.1.json" in workflow
    install_script = (
        Path(build_windows_installer.REPO_ROOT)
        / "scripts"
        / "install_inno_toolchain.ps1"
    ).read_text(encoding="utf-8")
    assert "Get-AuthenticodeSignature" in install_script
    assert "source_authenticode_thumbprint" in install_script
    assert workflow.count("build_windows_installer.py") >= 2
    assert "Compare-Object" in workflow or "Get-FileHash" in workflow
    compare_at = max(workflow.index("Compare-Object") if "Compare-Object" in workflow else 0,
                     workflow.index("Get-FileHash") if "Get-FileHash" in workflow else 0)
    publish_at = workflow.index("publish_release.py")
    assert compare_at < publish_at


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


def test_completion_message_never_blocks_silent_installers():
    script = (
        Path(build_windows_installer.REPO_ROOT)
        / "installer"
        / "PDFVectorImporter.iss"
    ).read_text(encoding="utf-8")
    event = re.search(
        r"procedure\s+CurStepChanged\s*\([^)]*\);(?P<body>.*?)\nend;",
        script,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert event is not None, "installer completion event is missing"
    body = event.group("body")
    completion_condition = re.search(
        r"if\s*\(\s*CurStep\s*=\s*ssPostInstall\s*\)\s*"
        r"and\s*\(?\s*not\s+WizardSilent\s*\)?\s*then",
        body,
        flags=re.IGNORECASE,
    )
    assert completion_condition is not None, (
        "interactive completion UI must be gated by not WizardSilent so "
        "/SILENT and /VERYSILENT installs can terminate unattended"
    )
    assert body.count("MsgBox(") == 1
    prompt_offset = body.index("MsgBox(")
    assert completion_condition.end() < prompt_offset, (
        "the scripted completion prompt must remain inside the non-silent gate"
    )


def test_installer_payload_does_not_embed_staging_timestamps():
    script = (
        Path(build_windows_installer.REPO_ROOT)
        / "installer"
        / "PDFVectorImporter.iss"
    ).read_text(encoding="utf-8")
    payload_entry = next(
        line
        for line in script.splitlines()
        if line.startswith('Source: "{#SourceDir}\\*";')
    )
    flags = payload_entry.split("Flags:", 1)[1].split()
    assert "notimestamp" in flags, (
        "installer payload entries must omit extraction-time metadata so two "
        "canonical ZIP stages compile to identical Setup.exe bytes"
    )
