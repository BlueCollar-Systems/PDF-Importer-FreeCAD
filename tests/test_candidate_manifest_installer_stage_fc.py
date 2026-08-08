from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path

import pytest

import build_windows_installer
from scripts import smoke_release_zip


REPO_ROOT = Path(build_windows_installer.REPO_ROOT)
CONTRACT_PATH = REPO_ROOT / "PDFVectorImporter" / "candidate_manifest.py"
CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "_task5c_candidate_manifest_contract", CONTRACT_PATH
)
assert CONTRACT_SPEC is not None and CONTRACT_SPEC.loader is not None
candidate_manifest = importlib.util.module_from_spec(CONTRACT_SPEC)
CONTRACT_SPEC.loader.exec_module(candidate_manifest)

PACKAGE_VERSION = "9.8.7"
SOURCE_COMMIT = "1" * 40
ZIP_NAME = f"FreeCAD-PDF-Importer_v{PACKAGE_VERSION}.zip"
MANIFEST_MEMBER = candidate_manifest.MANIFEST_MEMBER


def _regular_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_STORED
    return info


def _release_members(*, package_version: str = PACKAGE_VERSION) -> dict[str, bytes]:
    return {
        "PDFVectorImporter/package.xml": (
            f"<package><version>{package_version}</version></package>"
        ).encode("utf-8"),
        "PDFVectorImporter/module.py": b"VALUE = 'captured'\n",
        "PDFVectorImporter/data/payload.bin": b"exact payload bytes\x00\xff",
    }


def _write_release_zip(
    path: Path,
    *,
    package_version: str = PACKAGE_VERSION,
    source_commit: str = SOURCE_COMMIT,
    artifact_name: str | None = None,
    extra_entries: tuple[tuple[zipfile.ZipInfo, bytes], ...] = (),
) -> tuple[dict, dict[str, bytes]]:
    members = _release_members(package_version=package_version)
    document = candidate_manifest.build_candidate_file_manifest(
        members,
        source_commit=source_commit,
        package_version=package_version,
        artifact_name=artifact_name
        or f"FreeCAD-PDF-Importer_v{package_version}.zip",
    )
    manifest_bytes = candidate_manifest.canonical_candidate_manifest_bytes(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(_regular_info(MANIFEST_MEMBER), manifest_bytes)
        for name, payload in members.items():
            archive.writestr(_regular_info(name), payload)
        for info, payload in extra_entries:
            archive.writestr(info, payload)
    return document, members


def _valid_zip(tmp_path: Path) -> tuple[Path, dict, dict[str, bytes]]:
    path = tmp_path / "input" / ZIP_NAME
    document, members = _write_release_zip(path)
    assert smoke_release_zip.validate_release_zip_manifest(path) == []
    return path, document, members


def _stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    source_zip: Path | None = None,
    *,
    stage_parent: Path | None = None,
):
    if source_zip is None:
        source_zip, _document, _members = _valid_zip(tmp_path)
    monkeypatch.setattr(build_windows_installer, "read_version", lambda: PACKAGE_VERSION)
    return build_windows_installer.stage_release(
        source_zip,
        dist_dir=tmp_path / "dist",
        stage_dir=stage_parent or tmp_path / "stages",
    )


def _parse_stage_manifest(staged_release) -> dict:
    document, problems = candidate_manifest.parse_candidate_file_manifest(
        staged_release.candidate_manifest_bytes
    )
    assert problems == []
    assert document is not None
    return document


def _copy_clean_addon(staged_release, destination: Path) -> Path:
    addon = destination / "PDFVectorImporter"
    destination.mkdir(parents=True)
    shutil.copytree(staged_release.source_dir, addon, copy_function=shutil.copyfile)
    return addon


def _assert_pathless(exc: BaseException, tmp_path: Path) -> str:
    message = str(exc)
    assert str(tmp_path) not in message
    assert "Rowdy Payton" not in message
    assert "Traceback" not in message
    return message


def test_valid_zip_is_snapshotted_validated_and_staged_once(monkeypatch, tmp_path):
    source_zip, document, members = _valid_zip(tmp_path)
    original_bytes = source_zip.read_bytes()
    calls: list[tuple[str, Path]] = []
    real_validate = smoke_release_zip.validate_release_zip_manifest
    real_read_package = build_windows_installer._read_package_version

    def validate(snapshot: Path) -> list[str]:
        calls.append(("validate", snapshot))
        assert snapshot.name == ZIP_NAME
        assert snapshot != source_zip
        return real_validate(snapshot)

    def read_package(path: Path) -> str:
        calls.append(("package", path))
        return real_read_package(path)

    monkeypatch.setattr(build_windows_installer, "validate_release_zip_manifest", validate)
    monkeypatch.setattr(build_windows_installer, "_read_package_version", read_package)
    staged = _stage(monkeypatch, tmp_path, source_zip)

    assert isinstance(staged, build_windows_installer.InstallerStage)
    assert calls[0][0] == "validate"
    assert next(index for index, call in enumerate(calls) if call[0] == "package") > 0
    assert staged.version == PACKAGE_VERSION
    assert staged.source_zip_snapshot.read_bytes() == original_bytes
    assert staged.source_zip_name == ZIP_NAME
    assert staged.source_zip_size == len(original_bytes)
    assert staged.source_zip_sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert staged.candidate_manifest_bytes == candidate_manifest.canonical_candidate_manifest_bytes(
        document
    )
    assert staged.installed_manifest_sha256 == candidate_manifest.candidate_manifest_sha256(
        document
    )
    expected_identity = hashlib.sha256(
        build_windows_installer._canonical_json_bytes(
            {
                "installed_manifest_sha256": staged.installed_manifest_sha256,
                "source_zip_sha256": staged.source_zip_sha256,
            }
        )
    ).hexdigest()
    assert staged.stage_identity_sha256 == expected_identity
    assert staged.stage_root.name == expected_identity
    assert staged.source_dir == staged.stage_root / "PDFVectorImporter"
    assert staged.source_zip_snapshot.is_relative_to(staged.stage_root)
    assert set(path.name for path in staged.source_dir.rglob("*") if path.is_file()) == {
        "candidate-file-manifest.json",
        "package.xml",
        "module.py",
        "payload.bin",
    }
    assert (staged.source_dir / "module.py").read_bytes() == members[
        "PDFVectorImporter/module.py"
    ]
    top_level = {path.name for path in staged.stage_root.iterdir()}
    assert top_level == {
        "PDFVectorImporter",
        staged.source_zip_snapshot.relative_to(staged.stage_root).parts[0],
    }


def test_caller_replacement_after_snapshot_cannot_change_staged_bytes(
    monkeypatch, tmp_path
):
    source_zip, _document, members = _valid_zip(tmp_path)
    original_validate = smoke_release_zip.validate_release_zip_manifest

    def replace_caller_then_validate(snapshot: Path) -> list[str]:
        source_zip.write_bytes(b"private replacement bytes")
        return original_validate(snapshot)

    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest",
        replace_caller_then_validate,
    )
    staged = _stage(monkeypatch, tmp_path, source_zip)

    assert source_zip.read_bytes() == b"private replacement bytes"
    assert (staged.source_dir / "module.py").read_bytes() == members[
        "PDFVectorImporter/module.py"
    ]
    assert staged.source_zip_snapshot.read_bytes() != source_zip.read_bytes()


@pytest.mark.parametrize(
    "codes",
    [
        ["RELEASE_ZIP_CORRUPT"],
        ["MANIFEST_INVALID_JSON", "RELEASE_ZIP_UNSAFE_MEMBER"],
    ],
)
def test_task5b_failure_codes_block_staging_pathlessly(monkeypatch, tmp_path, codes):
    source_zip, _document, _members = _valid_zip(tmp_path)
    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest",
        lambda _snapshot: list(reversed(codes)),
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip)

    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_SOURCE_ZIP_INVALID: " + ", ".join(sorted(set(codes)))
    )
    assert not any(re.fullmatch(r"[0-9a-f]{64}", path.name) for path in (tmp_path / "stages").iterdir())


def test_hostile_or_raising_zip_validator_cannot_leak_private_diagnostics(
    monkeypatch, tmp_path
):
    source_zip, _document, _members = _valid_zip(tmp_path)
    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest",
        lambda _snapshot: ["C:/private/account/owner.pdf"],
    )
    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip)
    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_SOURCE_ZIP_INVALID: RELEASE_ZIP_IO_ERROR"
    )

    def raise_private(_snapshot):
        raise OSError("C:/private/account/owner.pdf")

    monkeypatch.setattr(
        build_windows_installer, "validate_release_zip_manifest", raise_private
    )
    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip)
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_SOURCE_IO_ERROR"


@pytest.mark.parametrize(
    ("bad_name", "expected_code"),
    [
        ("../escape.bin", "RELEASE_ZIP_UNSAFE_MEMBER"),
        ("C:/absolute.bin", "RELEASE_ZIP_UNSAFE_MEMBER"),
        ("PDFVectorImporter/payload:ads", "RELEASE_ZIP_UNSAFE_MEMBER"),
        ("PDFVectorImporter/con.txt", "RELEASE_ZIP_UNSAFE_MEMBER"),
        ("PDFVectorImporter/Caf\N{LATIN SMALL LETTER E WITH ACUTE}.txt", "MANIFEST_MEMBER_SET_MISMATCH"),
    ],
)
def test_unsafe_archive_names_are_rejected_before_filesystem_writes(
    monkeypatch, tmp_path, bad_name, expected_code
):
    source = tmp_path / "input" / ZIP_NAME
    bad_info = _regular_info(bad_name)
    _write_release_zip(source, extra_entries=((bad_info, b"unsafe"),))

    def forbid_extractall(*_args, **_kwargs):
        raise AssertionError("extractall must never be called")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", forbid_extractall)
    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source)

    message = _assert_pathless(caught.value, tmp_path)
    assert message.startswith("INSTALLER_SOURCE_ZIP_INVALID:")
    assert expected_code in message
    assert not (tmp_path / "escape.bin").exists()


def test_duplicate_and_nonregular_archive_entries_are_rejected(monkeypatch, tmp_path):
    source = tmp_path / "input" / ZIP_NAME
    _write_release_zip(source)
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr(_regular_info("PDFVectorImporter/module.py"), b"duplicate")
        directory = zipfile.ZipInfo("PDFVectorImporter/directory/")
        directory.create_system = 3
        directory.external_attr = (stat.S_IFDIR | 0o755) << 16
        archive.writestr(directory, b"")

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source)

    message = _assert_pathless(caught.value, tmp_path)
    assert "RELEASE_ZIP_DUPLICATE_MEMBER" in message
    assert "RELEASE_ZIP_NONREGULAR_MEMBER" in message


def test_safe_absent_stage_parent_is_created_component_by_component(monkeypatch, tmp_path):
    source_zip, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "absent" / "nested" / "stages"

    staged = _stage(
        monkeypatch,
        tmp_path,
        source_zip,
        stage_parent=stage_parent,
    )

    assert staged.stage_root.parent == stage_parent.absolute()
    assert staged.stage_root.is_dir()


def test_reparse_stage_parent_is_rejected_without_touching_outside_sentinel(
    monkeypatch, tmp_path
):
    source_zip, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    stage_parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"preserve me")
    real_detector = build_windows_installer._is_link_or_reparse

    def injected_detector(path: Path, metadata=None) -> bool:
        if Path(path) == stage_parent:
            return True
        return real_detector(path, metadata)

    monkeypatch.setattr(build_windows_installer, "_is_link_or_reparse", injected_detector)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path, source_zip, stage_parent=stage_parent)

    assert sentinel.read_bytes() == b"preserve me"
    assert list(stage_parent.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows alias behavior")
def test_case_alias_stage_parent_is_rejected_and_preserved(monkeypatch, tmp_path):
    container = tmp_path / "container"
    container.mkdir()
    alias = container / "Stages"
    alias.mkdir()
    marker = alias / "keep.bin"
    marker.write_bytes(b"keep")
    source_zip, _document, _members = _valid_zip(tmp_path)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(
            monkeypatch,
            tmp_path,
            source_zip,
            stage_parent=container / "stages",
        )

    assert marker.read_bytes() == b"keep"


def test_validate_installer_payload_tree_accepts_clean_copy_and_rejects_mutations(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    document = _parse_stage_manifest(staged)
    clean = _copy_clean_addon(staged, tmp_path / "clean-mod")
    assert build_windows_installer.validate_installer_payload_tree(document, clean) == []

    missing = _copy_clean_addon(staged, tmp_path / "missing-mod")
    (missing / "module.py").unlink()
    assert "MANIFEST_MEMBER_SET_MISMATCH" in (
        build_windows_installer.validate_installer_payload_tree(document, missing)
    )

    extra = _copy_clean_addon(staged, tmp_path / "extra-mod")
    (extra / "__pycache__").mkdir()
    (extra / "__pycache__" / "x.pyc").write_bytes(b"cache")
    assert "MANIFEST_MEMBER_SET_MISMATCH" in (
        build_windows_installer.validate_installer_payload_tree(document, extra)
    )

    changed = _copy_clean_addon(staged, tmp_path / "changed-mod")
    (changed / "module.py").write_bytes(b"changed")
    assert {
        "MANIFEST_MEMBER_SIZE_MISMATCH",
        "MANIFEST_MEMBER_DIGEST_MISMATCH",
    } <= set(build_windows_installer.validate_installer_payload_tree(document, changed))

    noncanonical = _copy_clean_addon(staged, tmp_path / "noncanonical-mod")
    manifest_path = noncanonical / "candidate-file-manifest.json"
    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
    assert "MANIFEST_NONCANONICAL_BYTES" in (
        build_windows_installer.validate_installer_payload_tree(document, noncanonical)
    )


def test_validate_installer_payload_tree_rejects_hardlinks_and_never_raises(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    document = _parse_stage_manifest(staged)
    addon = _copy_clean_addon(staged, tmp_path / "hardlink-mod")
    linked = addon / "module-link.py"
    try:
        os.link(addon / "module.py", linked)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    assert "MANIFEST_TREE_UNSAFE" in (
        build_windows_installer.validate_installer_payload_tree(document, addon)
    )

    class HostilePath:
        def __fspath__(self):
            raise RuntimeError("C:/private/account/owner.pdf")

    assert build_windows_installer.validate_installer_payload_tree(
        document, HostilePath()
    ) == ["MANIFEST_IO_ERROR"]


def test_exact_stage_is_reused_without_rewriting_and_conflict_is_preserved(
    monkeypatch, tmp_path
):
    source_zip, _document, _members = _valid_zip(tmp_path)
    first = _stage(monkeypatch, tmp_path, source_zip)
    before = {
        path.relative_to(first.stage_root).as_posix(): path.read_bytes()
        for path in first.stage_root.rglob("*")
        if path.is_file()
    }
    second = _stage(monkeypatch, tmp_path, source_zip)
    after = {
        path.relative_to(second.stage_root).as_posix(): path.read_bytes()
        for path in second.stage_root.rglob("*")
        if path.is_file()
    }
    assert second == first
    assert after == before

    conflict = first.stage_root / "unexpected.bin"
    conflict.write_bytes(b"preserve conflict")
    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_CONFLICT$"):
        _stage(monkeypatch, tmp_path, source_zip)
    assert conflict.read_bytes() == b"preserve conflict"


def test_publish_failure_is_atomic_and_leaves_no_owned_temporary(
    monkeypatch, tmp_path
):
    source_zip, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    original_replace = os.replace

    def fail_stage_publish(source, destination):
        if Path(source).parent == stage_parent:
            raise OSError("C:/private/publish failure")
        return original_replace(source, destination)

    monkeypatch.setattr(build_windows_installer.os, "replace", fail_stage_publish)
    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip, stage_parent=stage_parent)

    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_PUBLISH_ERROR"
    assert not any(path.name.startswith(".installer-stage-") for path in stage_parent.iterdir())
    assert source_zip.is_file()


def test_post_publication_mutation_is_detected_before_return(monkeypatch, tmp_path):
    source_zip, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    original_replace = os.replace

    def mutate_after_publish(source, destination):
        result = original_replace(source, destination)
        destination = Path(destination)
        if destination.parent == stage_parent and re.fullmatch(r"[0-9a-f]{64}", destination.name):
            (destination / "PDFVectorImporter" / "late.bin").write_bytes(b"late")
        return result

    monkeypatch.setattr(build_windows_installer.os, "replace", mutate_after_publish)
    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip, stage_parent=stage_parent)

    assert _assert_pathless(caught.value, tmp_path) in {
        "INSTALLER_STAGE_CONFLICT",
        "INSTALLER_STAGE_TREE_INVALID: MANIFEST_MEMBER_SET_MISMATCH",
    }


def test_compiler_revalidates_stage_and_uses_exact_source_directory(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    output = tmp_path / "compiler-output"
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        output.mkdir(parents=True, exist_ok=True)
        (output / f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe").write_bytes(
            b"synthetic setup"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_windows_installer.subprocess, "run", fake_run)
    result = build_windows_installer.compile_installer(
        tmp_path / "ISCC.exe", staged, output_dir=output
    )

    assert result.read_bytes() == b"synthetic setup"
    assert len(calls) == 1
    assert f"/DSourceDir={staged.source_dir}" in calls[0]

    (staged.source_dir / "module.py").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe", staged, output_dir=output
        )
    assert len(calls) == 1


def test_compiler_rejects_mutated_immutable_stage_values_and_maps_failures(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    mutated = dataclasses.replace(
        staged,
        candidate_manifest_bytes=staged.candidate_manifest_bytes + b" ",
    )
    calls: list[object] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(object()),
    )
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.compile_installer(tmp_path / "ISCC.exe", mutated)
    assert calls == []

    def fail_compiler(command, **_kwargs):
        raise subprocess.CalledProcessError(7, command, stderr="C:/private/owner.pdf")

    monkeypatch.setattr(build_windows_installer.subprocess, "run", fail_compiler)
    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.compile_installer(tmp_path / "ISCC.exe", staged)
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_COMPILER_FAILED"


def test_attestation_1_1_binds_stable_setup_zip_toolchain_and_manifest(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"stable setup bytes")
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
        staged_release=staged,
        installer_exe=setup,
        toolchain_identity=identity,
    )
    build_windows_installer.write_attestation(
        second,
        staged_release=staged,
        installer_exe=setup,
        toolchain_identity=identity,
    )

    assert first.read_bytes() == second.read_bytes()
    payload = json.loads(first.read_text(encoding="utf-8"))
    document = _parse_stage_manifest(staged)
    assert payload == {
        "schema": "bcs.freecad_installer_attestation/1.1",
        "source_commit": SOURCE_COMMIT,
        "stage_identity_sha256": staged.stage_identity_sha256,
        "source_zip": {
            "name": ZIP_NAME,
            "size": staged.source_zip_size,
            "sha256": staged.source_zip_sha256,
        },
        "installer": {
            "name": "Setup.exe",
            "size": len(b"stable setup bytes"),
            "sha256": hashlib.sha256(b"stable setup bytes").hexdigest(),
        },
        "payload_manifest": {
            "schema": document["schema"],
            "member": MANIFEST_MEMBER,
            "sha256": staged.installed_manifest_sha256,
        },
        "toolchain": identity,
    }


def test_attestation_rejects_hardlinked_setup_and_preserves_existing_bytes(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    other = tmp_path / "Setup-copy.exe"
    try:
        os.link(setup, other)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"preserve existing attestation")

    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.write_attestation(
            output,
            staged_release=staged,
            installer_exe=setup,
            toolchain_identity={},
        )

    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_SETUP_UNSAFE"
    assert output.read_bytes() == b"preserve existing attestation"


def test_attestation_detects_setup_path_replacement_before_publication(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"original setup")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"preserve existing attestation")
    real_capture = build_windows_installer._capture_regular_file
    setup_captures = 0

    def replace_after_first_setup_capture(path: Path) -> bytes:
        nonlocal setup_captures
        payload = real_capture(path)
        if Path(path) == setup:
            setup_captures += 1
            if setup_captures == 1:
                setup.unlink()
                setup.write_bytes(b"replacement setup")
        return payload

    monkeypatch.setattr(
        build_windows_installer, "_capture_regular_file", replace_after_first_setup_capture
    )
    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.write_attestation(
            output,
            staged_release=staged,
            installer_exe=setup,
            toolchain_identity={"version": "synthetic"},
        )

    assert setup_captures >= 2
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_SETUP_CHANGED"
    assert output.read_bytes() == b"preserve existing attestation"


def test_attestation_publication_failure_preserves_existing_bytes_and_cleans_temp(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"preserve existing attestation")
    original_replace = os.replace

    def fail_attestation_publish(source, destination):
        if Path(destination) == output:
            raise OSError("C:/private/publish")
        return original_replace(source, destination)

    monkeypatch.setattr(build_windows_installer.os, "replace", fail_attestation_publish)
    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.write_attestation(
            output,
            staged_release=staged,
            installer_exe=setup,
            toolchain_identity={"version": "synthetic"},
        )

    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_ATTESTATION_PUBLISH_ERROR"
    )
    assert output.read_bytes() == b"preserve existing attestation"
    assert not any(path.name.startswith(".installer-attestation-") for path in tmp_path.iterdir())


def test_main_retains_source_commit_as_equality_only_and_reuses_one_stage(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    compile_calls: list[object] = []
    attest_calls: list[object] = []
    monkeypatch.setattr(sys, "argv", ["build_windows_installer.py", "--source-commit", SOURCE_COMMIT, "--attestation", str(tmp_path / "a.json")])
    monkeypatch.setattr(build_windows_installer, "find_iscc", lambda _path: tmp_path / "ISCC.exe")
    monkeypatch.setattr(build_windows_installer, "verify_inno_toolchain", lambda *_args: {"version": "synthetic"})
    monkeypatch.setattr(build_windows_installer, "stage_release", lambda *_args, **_kwargs: staged)

    def compile_same(_iscc, staged_release, **_kwargs):
        compile_calls.append(staged_release)
        return setup

    def attest_same(_output, *, staged_release, **_kwargs):
        attest_calls.append(staged_release)
        return tmp_path / "a.json"

    monkeypatch.setattr(build_windows_installer, "compile_installer", compile_same)
    monkeypatch.setattr(build_windows_installer, "write_attestation", attest_same)
    assert build_windows_installer.main() == 0
    assert compile_calls == [staged]
    assert attest_calls == [staged]

    monkeypatch.setattr(sys, "argv", ["build_windows_installer.py", "--source-commit", "2" * 40])
    compile_calls.clear()
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.main()
    assert compile_calls == []


def test_static_inno_source_relation_is_recursive_manifest_bearing_and_notimestamp():
    script = (REPO_ROOT / "installer" / "PDFVectorImporter.iss").read_text(
        encoding="utf-8"
    )
    payload_entry = next(
        line for line in script.splitlines() if line.startswith('Source: "{#SourceDir}\\*";')
    )
    assert "recursesubdirs" in payload_entry
    assert "createallsubdirs" in payload_entry
    assert "notimestamp" in payload_entry
    assert "candidate-file-manifest.json" not in payload_entry
    assert "Excludes:" not in payload_entry


def test_source_hardlink_is_rejected_with_closed_private_safe_diagnostic(
    monkeypatch, tmp_path
):
    source_zip, _document, _members = _valid_zip(tmp_path)
    linked = source_zip.with_name("linked.zip")
    try:
        os.link(source_zip, linked)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip)

    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_SOURCE_UNSAFE"
    assert linked.is_file()


def test_stage_identity_normalizes_only_exact_canonical_json(monkeypatch, tmp_path):
    staged = _stage(monkeypatch, tmp_path)
    document = _parse_stage_manifest(staged)
    expected = hashlib.sha256(
        (json.dumps(
            {
                "installed_manifest_sha256": candidate_manifest.candidate_manifest_sha256(document),
                "source_zip_sha256": staged.source_zip_sha256,
            },
            indent=2,
            sort_keys=True,
        ) + "\n").encode("utf-8")
    ).hexdigest()
    assert staged.stage_identity_sha256 == expected
    assert unicodedata.normalize("NFC", staged.stage_root.name) == staged.stage_root.name
