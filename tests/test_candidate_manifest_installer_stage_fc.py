from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import io
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


def _valid_toolchain_identity() -> dict[str, str]:
    return {
        "name": "Inno Setup",
        "version": "6.7.1",
        "source_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "tree_sha256": "c" * 64,
    }


def test_valid_zip_is_snapshotted_validated_and_staged_once(monkeypatch, tmp_path):
    source_zip, document, members = _valid_zip(tmp_path)
    original_bytes = source_zip.read_bytes()
    calls: list[tuple[str, object]] = []
    real_validate = smoke_release_zip.validate_release_zip_manifest_bytes
    real_read_package = build_windows_installer._read_package_version

    def validate(snapshot_bytes: bytes, *, artifact_name: str) -> list[str]:
        calls.append(("validate", artifact_name))
        assert type(snapshot_bytes) is bytes
        assert snapshot_bytes == original_bytes
        assert artifact_name == ZIP_NAME
        return real_validate(snapshot_bytes, artifact_name=artifact_name)

    def read_package(path: Path) -> str:
        calls.append(("package", path))
        return real_read_package(path)

    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        validate,
        raising=False,
    )
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
    original_validate = smoke_release_zip.validate_release_zip_manifest_bytes

    def replace_caller_then_validate(
        snapshot_bytes: bytes, *, artifact_name: str
    ) -> list[str]:
        source_zip.write_bytes(b"private replacement bytes")
        return original_validate(snapshot_bytes, artifact_name=artifact_name)

    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        replace_caller_then_validate,
        raising=False,
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
        "validate_release_zip_manifest_bytes",
        lambda _snapshot_bytes, *, artifact_name: list(reversed(codes)),
        raising=False,
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
        "validate_release_zip_manifest_bytes",
        lambda _snapshot_bytes, *, artifact_name: [
            "C:/private/account/owner.pdf"
        ],
        raising=False,
    )
    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip)
    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_SOURCE_ZIP_INVALID: RELEASE_ZIP_IO_ERROR"
    )

    def raise_private(_snapshot_bytes, *, artifact_name):
        raise OSError("C:/private/account/owner.pdf")

    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        raise_private,
        raising=False,
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
            toolchain_identity=_valid_toolchain_identity(),
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


def _write_variant_release_zip(
    path: Path, *, marker: bytes, source_commit: str
) -> tuple[dict, dict[str, bytes]]:
    members = _release_members()
    members["PDFVectorImporter/module.py"] = marker
    document = candidate_manifest.build_candidate_file_manifest(
        members,
        source_commit=source_commit,
        package_version=PACKAGE_VERSION,
        artifact_name=ZIP_NAME,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            _regular_info(MANIFEST_MEMBER),
            candidate_manifest.canonical_candidate_manifest_bytes(document),
        )
        for name, payload in members.items():
            archive.writestr(_regular_info(name), payload)
    assert smoke_release_zip.validate_release_zip_manifest(path) == []
    return document, members


def _mixed_stage_from_zip_a_and_tree_b(monkeypatch, tmp_path):
    zip_a = tmp_path / "candidate-a" / ZIP_NAME
    zip_b = tmp_path / "candidate-b" / ZIP_NAME
    _document_a, _members_a = _write_variant_release_zip(
        zip_a, marker=b"CANDIDATE = 'A'\n", source_commit="a" * 40
    )
    document_b, _members_b = _write_variant_release_zip(
        zip_b, marker=b"CANDIDATE = 'B'\n", source_commit="b" * 40
    )
    stage_b = _stage(
        monkeypatch,
        tmp_path / "stage-b-work",
        zip_b,
        stage_parent=tmp_path / "stage-b",
    )
    manifest_b = candidate_manifest.canonical_candidate_manifest_bytes(document_b)
    manifest_digest_b = candidate_manifest.candidate_manifest_sha256(document_b)
    zip_a_bytes = zip_a.read_bytes()
    zip_a_digest = hashlib.sha256(zip_a_bytes).hexdigest()
    mixed_identity = build_windows_installer._stage_identity(
        manifest_digest_b, zip_a_digest
    )
    root = tmp_path / "mixed" / mixed_identity
    shutil.copytree(stage_b.source_dir, root / "PDFVectorImporter")
    metadata = root / ".installer-source"
    metadata.mkdir()
    snapshot = metadata / ZIP_NAME
    snapshot.write_bytes(zip_a_bytes)
    mixed = build_windows_installer.InstallerStage(
        version=PACKAGE_VERSION,
        stage_root=root.absolute(),
        source_dir=(root / "PDFVectorImporter").absolute(),
        source_zip_snapshot=snapshot.absolute(),
        source_zip_name=ZIP_NAME,
        source_zip_size=len(zip_a_bytes),
        source_zip_sha256=zip_a_digest,
        candidate_manifest_bytes=manifest_b,
        installed_manifest_sha256=manifest_digest_b,
        stage_identity_sha256=mixed_identity,
    )
    return mixed


def test_compiler_blocks_independently_valid_zip_a_manifest_tree_b_cross_binding(
    monkeypatch, tmp_path
):
    mixed = _mixed_stage_from_zip_a_and_tree_b(monkeypatch, tmp_path)
    calls: list[object] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(object()),
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe", mixed, output_dir=tmp_path / "out"
        )

    assert calls == []


def test_attestation_blocks_zip_a_manifest_tree_b_and_preserves_seeded_bytes(
    monkeypatch, tmp_path
):
    mixed = _mixed_stage_from_zip_a_and_tree_b(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    output = tmp_path / "attestation.json"
    seeded = b"seeded attestation"
    output.write_bytes(seeded)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"):
        build_windows_installer.write_attestation(
            output,
            staged_release=mixed,
            installer_exe=setup,
            toolchain_identity=_valid_toolchain_identity(),
        )

    assert output.read_bytes() == seeded


def test_snapshot_substitution_after_byte_validation_cannot_publish_mixed_stage(
    monkeypatch, tmp_path
):
    source_a = tmp_path / "source-a" / ZIP_NAME
    source_b = tmp_path / "source-b" / ZIP_NAME
    _write_variant_release_zip(
        source_a, marker=b"CANDIDATE = 'A'\n", source_commit="a" * 40
    )
    _write_variant_release_zip(
        source_b, marker=b"CANDIDATE = 'B'\n", source_commit="b" * 40
    )
    replacement = source_b.read_bytes()
    stage_parent = tmp_path / "stages"
    real_validate = smoke_release_zip.validate_release_zip_manifest_bytes
    calls = 0

    def substitute_after_validation(payload: bytes, *, artifact_name: str):
        nonlocal calls
        calls += 1
        result = real_validate(payload, artifact_name=artifact_name)
        if calls == 1:
            snapshot = next(
                stage_parent.glob(f"{build_windows_installer._STAGE_TEMP_PREFIX}*/.installer-source/{ZIP_NAME}")
            )
            snapshot.write_bytes(replacement)
        return result

    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        substitute_after_validation,
        raising=False,
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(
            monkeypatch,
            tmp_path,
            source_a,
            stage_parent=stage_parent,
        )

    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_SOURCE_CHANGED"
    assert not any(re.fullmatch(r"[0-9a-f]{64}", child.name) for child in stage_parent.iterdir())


def test_source_metadata_mutation_during_streaming_is_source_changed(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    original_open = Path.open
    original_stat = source.stat()
    mutated = False

    class MutatingReader:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def fileno(self):
            return self.inner.fileno()

        def read(self, size=-1):
            nonlocal mutated
            payload = self.inner.read(size)
            if payload and not mutated:
                mutated = True
                os.utime(
                    source,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
                )
            return payload

    def injected_open(path: Path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if Path(path) == source and mode == "rb":
            return MutatingReader(stream)
        return stream

    monkeypatch.setattr(Path, "open", injected_open)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_SOURCE_CHANGED$"):
        _stage(monkeypatch, tmp_path, source)


def test_stage_temp_collision_before_ownership_is_preserved_and_unsafe(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    token = "1" * 32
    temporary = stage_parent / (build_windows_installer._STAGE_TEMP_PREFIX + token)
    marker = temporary / "winner.bin"
    real_mkdir = os.mkdir

    class FixedUUID:
        hex = token

    def collide_mkdir(path, mode=0o777):
        if Path(path) == temporary:
            real_mkdir(path, mode)
            marker.write_bytes(b"concurrent winner")
            raise FileExistsError("synthetic collision")
        return real_mkdir(path, mode)

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUUID())
    monkeypatch.setattr(build_windows_installer.os, "mkdir", collide_mkdir)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert marker.read_bytes() == b"concurrent winner"


def test_attestation_temp_collision_before_ownership_is_preserved_and_io_error(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"seeded")
    token = "2" * 32
    temporary = tmp_path / (build_windows_installer._ATTESTATION_TEMP_PREFIX + token)
    original_open = Path.open

    class FixedUUID:
        hex = token

    def collide_open(path: Path, mode="r", *args, **kwargs):
        if Path(path) == temporary and mode == "xb":
            with original_open(path, "wb") as stream:
                stream.write(b"concurrent temp")
            raise FileExistsError("synthetic collision")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUUID())
    monkeypatch.setattr(Path, "open", collide_open)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_IO_ERROR$"):
        build_windows_installer.write_attestation(
            output,
            staged_release=staged,
            installer_exe=setup,
            toolchain_identity=_valid_toolchain_identity(),
        )

    assert temporary.read_bytes() == b"concurrent temp"
    assert output.read_bytes() == b"seeded"


def test_cleanup_never_descends_at_former_lstat_scandir_junction_boundary(
    monkeypatch, tmp_path
):
    parent = tmp_path / "parent"
    root = parent / "owned"
    child = root / "child"
    child.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside sentinel")
    parent_chain = build_windows_installer._directory_chain(parent)
    owned_identity = build_windows_installer._stat_identity(os.lstat(root))
    real_scandir = os.scandir

    def forbid_owned_scan(path):
        candidate = Path(path)
        if candidate == root or candidate.is_relative_to(root):
            raise AssertionError("cleanup followed the owned pathname")
        return real_scandir(path)

    monkeypatch.setattr(build_windows_installer.os, "scandir", forbid_owned_scan)

    assert build_windows_installer._cleanup_owned(
        root, parent_chain, owned_identity
    ) is True
    assert sentinel.read_bytes() == b"outside sentinel"


def test_lost_root_ownership_before_quarantine_preserves_replacement_and_token(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    collision_bytes = b"unowned collision"
    real_validate = smoke_release_zip.validate_release_zip_manifest_bytes

    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        lambda payload, *, artifact_name: ["RELEASE_ZIP_CORRUPT"],
        raising=False,
    )

    def lose_ownership(path, _parent_chain, _owned_identity):
        path = Path(path)
        displaced = path.with_name(path.name + "-displaced")
        os.replace(path, displaced)
        path.mkdir()
        (path / "collision.bin").write_bytes(collision_bytes)
        return False

    monkeypatch.setattr(build_windows_installer, "_cleanup_owned", lose_ownership)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_IO_ERROR$"):
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    replacement = next(stage_parent.glob(".installer-stage-*/collision.bin"))
    assert replacement.read_bytes() == collision_bytes


@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("stage-prepublish", "INSTALLER_STAGE_IO_ERROR"),
        ("stage-publish", "INSTALLER_STAGE_PUBLISH_ERROR"),
        ("attestation-prepublish", "INSTALLER_ATTESTATION_IO_ERROR"),
        ("attestation-publish", "INSTALLER_ATTESTATION_PUBLISH_ERROR"),
    ],
)
def test_cleanup_failure_overrides_with_exact_phase_token(
    monkeypatch, tmp_path, phase, expected
):
    monkeypatch.setattr(build_windows_installer, "_cleanup_owned", lambda *_args: False)
    if phase.startswith("stage"):
        source, _document, _members = _valid_zip(tmp_path)
        if phase == "stage-prepublish":
            monkeypatch.setattr(
                build_windows_installer,
                "validate_release_zip_manifest_bytes",
                lambda payload, *, artifact_name: ["RELEASE_ZIP_CORRUPT"],
                raising=False,
            )
        else:
            original_replace = os.replace

            def fail_publish(source_path, destination_path):
                if Path(source_path).name.startswith(
                    build_windows_installer._STAGE_TEMP_PREFIX
                ):
                    raise OSError("synthetic publish failure")
                return original_replace(source_path, destination_path)

            monkeypatch.setattr(build_windows_installer.os, "replace", fail_publish)
        with pytest.raises(RuntimeError) as caught:
            _stage(monkeypatch, tmp_path, source)
    else:
        staged = _stage(monkeypatch, tmp_path)
        setup = tmp_path / "Setup.exe"
        setup.write_bytes(b"setup")
        output = tmp_path / "attestation.json"
        output.write_bytes(b"seeded")
        if phase == "attestation-prepublish":
            real_capture = build_windows_installer._capture_regular_file

            def fail_temp_read(path):
                if Path(path).name.startswith(
                    build_windows_installer._ATTESTATION_TEMP_PREFIX
                ):
                    raise OSError("synthetic readback failure")
                return real_capture(path)

            monkeypatch.setattr(
                build_windows_installer, "_capture_regular_file", fail_temp_read
            )
        else:
            original_replace = os.replace

            def fail_publish(source_path, destination_path):
                if Path(destination_path) == output:
                    raise OSError("synthetic publish failure")
                return original_replace(source_path, destination_path)

            monkeypatch.setattr(build_windows_installer.os, "replace", fail_publish)
        with pytest.raises(RuntimeError) as caught:
            build_windows_installer.write_attestation(
                output,
                staged_release=staged,
                installer_exe=setup,
                toolchain_identity=_valid_toolchain_identity(),
            )
    assert _assert_pathless(caught.value, tmp_path) == expected


def test_owned_temp_reparse_is_stage_unsafe_and_outside_sentinel_is_preserved(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"preserve")
    real_detector = build_windows_installer._is_link_or_reparse

    def inject_temp_reparse(path, metadata=None):
        candidate = Path(path)
        if candidate.parent == stage_parent and candidate.name.startswith(
            build_windows_installer._STAGE_TEMP_PREFIX
        ):
            return True
        return real_detector(path, metadata)

    monkeypatch.setattr(
        build_windows_installer, "_is_link_or_reparse", inject_temp_reparse
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert sentinel.read_bytes() == b"preserve"


def test_changed_stage_ancestor_before_publish_is_unsafe_and_preserves_source(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    original = source.read_bytes()
    real_revalidate = build_windows_installer._revalidate_chain
    calls = 0

    def change_on_publish(chain):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise build_windows_installer._ChangedPath
        return real_revalidate(chain)

    monkeypatch.setattr(build_windows_installer, "_revalidate_chain", change_on_publish)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path, source)

    assert source.read_bytes() == original


def test_concurrent_exact_winner_is_reused_and_conflicting_winner_preserved(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    baseline = _stage(monkeypatch, tmp_path / "baseline", source)
    expected_tree = {
        path.relative_to(baseline.stage_root).as_posix(): path.read_bytes()
        for path in baseline.stage_root.rglob("*")
        if path.is_file()
    }
    target_parent = tmp_path / "concurrent"
    original_replace = os.replace
    injected = False

    def exact_winner_then_fail(source_path, destination_path):
        nonlocal injected
        destination_path = Path(destination_path)
        if (
            not injected
            and destination_path.parent == target_parent
            and re.fullmatch(r"[0-9a-f]{64}", destination_path.name)
        ):
            injected = True
            shutil.copytree(baseline.stage_root, destination_path)
            raise OSError("lost publication race")
        return original_replace(source_path, destination_path)

    monkeypatch.setattr(build_windows_installer.os, "replace", exact_winner_then_fail)
    winner = _stage(
        monkeypatch, tmp_path / "winner-work", source, stage_parent=target_parent
    )
    assert winner.stage_root.parent == target_parent.absolute()
    assert {
        path.relative_to(winner.stage_root).as_posix(): path.read_bytes()
        for path in winner.stage_root.rglob("*")
        if path.is_file()
    } == expected_tree

    conflict_parent = tmp_path / "conflict-race"
    marker = b"conflicting winner"
    injected = False

    def conflict_then_fail(source_path, destination_path):
        nonlocal injected
        destination_path = Path(destination_path)
        if (
            not injected
            and destination_path.parent == conflict_parent
            and re.fullmatch(r"[0-9a-f]{64}", destination_path.name)
        ):
            injected = True
            destination_path.mkdir()
            (destination_path / "winner.bin").write_bytes(marker)
            raise OSError("lost publication race")
        return original_replace(source_path, destination_path)

    monkeypatch.setattr(build_windows_installer.os, "replace", conflict_then_fail)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_PUBLISH_ERROR$"):
        _stage(
            monkeypatch,
            tmp_path / "conflict-work",
            source,
            stage_parent=conflict_parent,
        )
    assert next(conflict_parent.glob("*/winner.bin")).read_bytes() == marker


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "9.8.8"),
        ("source_zip_size", 1),
        ("source_zip_sha256", "0" * 64),
        ("installed_manifest_sha256", "1" * 64),
        ("stage_identity_sha256", "2" * 64),
        ("candidate_manifest_bytes", b"{}\n"),
    ],
)
def test_compiler_blocks_each_mutated_stage_identity_field_without_subprocess(
    monkeypatch, tmp_path, field, value
):
    staged = _stage(monkeypatch, tmp_path)
    mutated = dataclasses.replace(staged, **{field: value})
    calls: list[object] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(object()),
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe", mutated, output_dir=tmp_path / "output"
        )
    assert calls == []


def test_compiler_blocks_zip_snapshot_byte_mutation_without_subprocess(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    staged.source_zip_snapshot.write_bytes(b"mutated snapshot")
    calls: list[object] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(object()),
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.compile_installer(tmp_path / "ISCC.exe", staged)
    assert calls == []


def test_attestation_stage_or_zip_mutation_preserves_seeded_output(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    staged.source_zip_snapshot.write_bytes(b"mutated snapshot")
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"seeded")

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"):
        build_windows_installer.write_attestation(
            output,
            staged_release=staged,
            installer_exe=setup,
            toolchain_identity=_valid_toolchain_identity(),
        )
    assert output.read_bytes() == b"seeded"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("temp-mkdir", "INSTALLER_STAGE_IO_ERROR"),
        ("source-copy", "INSTALLER_SOURCE_IO_ERROR"),
        ("source-fsync", "INSTALLER_SOURCE_IO_ERROR"),
        ("source-close", "INSTALLER_SOURCE_IO_ERROR"),
        ("member-write", "INSTALLER_STAGE_IO_ERROR"),
        ("package-read", "INSTALLER_STAGE_IO_ERROR"),
    ],
)
def test_stage_injected_io_failures_have_exact_phase_tokens_and_preserve_source(
    monkeypatch, tmp_path, failure, expected
):
    source, _document, _members = _valid_zip(tmp_path)
    source_bytes = source.read_bytes()
    stage_parent = tmp_path / "stages"
    if failure == "temp-mkdir":
        real_mkdir = os.mkdir

        def fail_temp_mkdir(path, mode=0o777):
            if Path(path).parent == stage_parent and Path(path).name.startswith(
                build_windows_installer._STAGE_TEMP_PREFIX
            ):
                raise OSError("synthetic mkdir failure")
            return real_mkdir(path, mode)

        monkeypatch.setattr(build_windows_installer.os, "mkdir", fail_temp_mkdir)
    elif failure == "source-copy":
        monkeypatch.setattr(
            build_windows_installer,
            "_copy_source_zip_snapshot",
            lambda *_args: (_ for _ in ()).throw(OSError("synthetic copy failure")),
        )
    elif failure == "source-fsync":
        monkeypatch.setattr(
            build_windows_installer.os,
            "fsync",
            lambda _fd: (_ for _ in ()).throw(OSError("synthetic fsync failure")),
        )
    elif failure == "source-close":
        original_open = Path.open

        class CloseFailure:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, *args):
                self.stream.__exit__(*args)
                raise OSError("synthetic close failure")

            def __getattr__(self, name):
                return getattr(self.stream, name)

        def fail_snapshot_close(path: Path, mode="r", *args, **kwargs):
            stream = original_open(path, mode, *args, **kwargs)
            if mode == "xb" and Path(path).name == ZIP_NAME:
                return CloseFailure(stream)
            return stream

        monkeypatch.setattr(Path, "open", fail_snapshot_close)
    elif failure == "member-write":
        monkeypatch.setattr(
            build_windows_installer,
            "_write_owned_member",
            lambda *_args: (_ for _ in ()).throw(OSError("synthetic member write")),
        )
    else:
        monkeypatch.setattr(
            build_windows_installer,
            "_read_package_version",
            lambda _path: (_ for _ in ()).throw(OSError("synthetic package read")),
        )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert _assert_pathless(caught.value, tmp_path) == expected
    assert source.read_bytes() == source_bytes
    assert not any(re.fullmatch(r"[0-9a-f]{64}", path.name) for path in stage_parent.iterdir())


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ("renamed", ["MANIFEST_MEMBER_SET_MISMATCH"]),
        ("case-renamed", ["MANIFEST_MEMBER_SET_MISMATCH"]),
        ("symlink", ["MANIFEST_TREE_UNSAFE"]),
        ("junction", ["MANIFEST_TREE_UNSAFE"]),
        ("reparse", ["MANIFEST_TREE_UNSAFE"]),
        ("nonregular", ["MANIFEST_TREE_UNSAFE"]),
        ("unreadable", ["MANIFEST_IO_ERROR"]),
    ],
)
def test_synthetic_installed_tree_mutations_return_exact_sorted_task5a_codes(
    monkeypatch, tmp_path, mutation, expected
):
    staged = _stage(monkeypatch, tmp_path)
    document = _parse_stage_manifest(staged)
    addon = _copy_clean_addon(staged, tmp_path / f"{mutation}-mod")
    module = addon / "module.py"
    if mutation == "renamed":
        module.rename(addon / "renamed.py")
    elif mutation == "case-renamed":
        intermediate = addon / "case-temporary.py"
        module.rename(intermediate)
        intermediate.rename(addon / "MODULE.py")
    elif mutation in {"symlink", "junction", "reparse"}:
        real_detector = build_windows_installer._is_link_or_reparse
        monkeypatch.setattr(
            build_windows_installer,
            "_is_link_or_reparse",
            lambda path, metadata=None: Path(path) == module
            or real_detector(path, metadata),
        )
    elif mutation == "nonregular":
        real_lstat = os.lstat
        real_metadata = real_lstat(module)

        def fifo_lstat(path, *args, **kwargs):
            metadata = real_lstat(path, *args, **kwargs)
            if isinstance(path, (str, os.PathLike)) and Path(path) == module:
                class NonregularMetadata:
                    st_mode = stat.S_IFIFO | 0o600
                    st_file_attributes = 0
                    st_nlink = 1

                return NonregularMetadata()
            return metadata

        monkeypatch.setattr(build_windows_installer.os, "lstat", fifo_lstat)
    else:
        original_open = Path.open

        def unreadable_open(path: Path, mode="r", *args, **kwargs):
            if Path(path) == module and mode == "rb":
                raise PermissionError("synthetic unreadable member")
            return original_open(path, mode, *args, **kwargs)

        monkeypatch.setattr(Path, "open", unreadable_open)

    assert build_windows_installer.validate_installer_payload_tree(
        document, addon
    ) == expected


def test_synthetic_installed_tree_rejects_decomposed_unicode_rename_exactly(
    monkeypatch, tmp_path
):
    source = tmp_path / "unicode" / ZIP_NAME
    members = _release_members()
    members.pop("PDFVectorImporter/data/payload.bin")
    composed = "PDFVectorImporter/data/caf\N{LATIN SMALL LETTER E WITH ACUTE}.bin"
    members[composed] = b"unicode payload"
    document = candidate_manifest.build_candidate_file_manifest(
        members,
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ZIP_NAME,
    )
    source.parent.mkdir(parents=True)
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            _regular_info(MANIFEST_MEMBER),
            candidate_manifest.canonical_candidate_manifest_bytes(document),
        )
        for name, content in members.items():
            archive.writestr(_regular_info(name), content)
    staged = _stage(monkeypatch, tmp_path, source)
    addon = _copy_clean_addon(staged, tmp_path / "unicode-mod")
    composed_path = addon / "data" / "caf\N{LATIN SMALL LETTER E WITH ACUTE}.bin"
    decomposed_path = addon / "data" / "cafe\N{COMBINING ACUTE ACCENT}.bin"
    composed_path.rename(decomposed_path)

    assert build_windows_installer.validate_installer_payload_tree(
        _parse_stage_manifest(staged), addon
    ) == ["MANIFEST_INVALID_PATH", "MANIFEST_MEMBER_SET_MISMATCH"]


class _ToolchainDictSubclass(dict):
    pass


class _HostileToolchainValue:
    def __str__(self):
        raise RuntimeError("SENSITIVE_PROTOCOL_SENTINEL")

    def __repr__(self):
        raise RuntimeError("SENSITIVE_PROTOCOL_SENTINEL")


@pytest.mark.parametrize(
    "identity",
    [
        pytest.param({**_valid_toolchain_identity(), "name": object()}, id="non-json"),
        pytest.param(_ToolchainDictSubclass(_valid_toolchain_identity()), id="dict-subclass"),
        pytest.param({**_valid_toolchain_identity(), "name": _HostileToolchainValue()}, id="hostile-value"),
        pytest.param({key: value for key, value in _valid_toolchain_identity().items() if key != "version"}, id="missing-key"),
        pytest.param({**_valid_toolchain_identity(), "extra": "value"}, id="extra-key"),
        pytest.param({**_valid_toolchain_identity(), "source_sha256": "A" * 64}, id="uppercase-digest"),
        pytest.param({**_valid_toolchain_identity(), "manifest_sha256": "0" * 63}, id="short-digest"),
        pytest.param({**_valid_toolchain_identity(), "tree_sha256": "g" * 64}, id="nonhex-digest"),
    ],
)
def test_attestation_rejects_noncanonical_toolchain_inputs_pathlessly_and_atomically(
    monkeypatch, tmp_path, identity
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"seeded attestation")

    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.write_attestation(
            output,
            staged_release=staged,
            installer_exe=setup,
            toolchain_identity=identity,
        )

    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_ATTESTATION_INPUT_INVALID"
    )
    assert output.read_bytes() == b"seeded attestation"
    assert not any(
        path.name.startswith(build_windows_installer._ATTESTATION_TEMP_PREFIX)
        for path in tmp_path.iterdir()
    )


@pytest.mark.parametrize("failure", ["write", "close", "readback"])
def test_attestation_temp_failures_are_atomic_io_errors(
    monkeypatch, tmp_path, failure
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"seeded")
    original_open = Path.open
    real_capture = build_windows_installer._capture_regular_file

    class FaultingWriter:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            result = self.stream.__exit__(*args)
            if failure == "close":
                raise OSError("synthetic close failure")
            return result

        def write(self, payload):
            if failure == "write":
                raise OSError("synthetic write failure")
            return self.stream.write(payload)

        def __getattr__(self, name):
            return getattr(self.stream, name)

    def fault_open(path: Path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if mode == "xb" and Path(path).name.startswith(
            build_windows_installer._ATTESTATION_TEMP_PREFIX
        ):
            return FaultingWriter(stream)
        return stream

    def fault_capture(path: Path):
        if failure == "readback" and Path(path).name.startswith(
            build_windows_installer._ATTESTATION_TEMP_PREFIX
        ):
            raise OSError("synthetic readback failure")
        return real_capture(path)

    monkeypatch.setattr(Path, "open", fault_open)
    monkeypatch.setattr(build_windows_installer, "_capture_regular_file", fault_capture)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_IO_ERROR$"):
        build_windows_installer.write_attestation(
            output,
            staged_release=staged,
            installer_exe=setup,
            toolchain_identity=_valid_toolchain_identity(),
        )

    assert output.read_bytes() == b"seeded"


def test_successful_attestation_replace_is_final_without_post_commit_capture(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"seeded")
    real_replace = os.replace
    real_capture = build_windows_installer._capture_regular_file
    committed = False
    post_commit_captures = 0

    def mark_commit(source, destination):
        nonlocal committed
        result = real_replace(source, destination)
        if Path(destination) == output:
            committed = True
        return result

    def forbid_post_commit_capture(path):
        nonlocal post_commit_captures
        if committed and Path(path) == output:
            post_commit_captures += 1
            raise OSError("post-commit capture forbidden")
        return real_capture(path)

    monkeypatch.setattr(build_windows_installer.os, "replace", mark_commit)
    monkeypatch.setattr(
        build_windows_installer, "_capture_regular_file", forbid_post_commit_capture
    )

    result = build_windows_installer.write_attestation(
        output,
        staged_release=staged,
        installer_exe=setup,
        toolchain_identity=_valid_toolchain_identity(),
    )

    assert result == output.absolute()
    assert post_commit_captures == 0
    assert json.loads(output.read_text(encoding="utf-8"))["schema"] == (
        "bcs.freecad_installer_attestation/1.1"
    )


def test_case_and_injected_nfc_digest_aliases_block_publication_and_are_preserved(
    monkeypatch, tmp_path
):
    source, document, _members = _valid_zip(tmp_path)
    zip_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_digest = candidate_manifest.candidate_manifest_sha256(document)
    identity = build_windows_installer._stage_identity(manifest_digest, zip_digest)

    case_parent = tmp_path / "case-alias"
    case_parent.mkdir()
    case_alias = case_parent / identity.upper()
    case_alias.mkdir()
    (case_alias / "marker.bin").write_bytes(b"case alias")
    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path / "case-work", source, stage_parent=case_parent)
    assert (case_alias / "marker.bin").read_bytes() == b"case alias"

    nfc_parent = tmp_path / "nfc-alias"
    nfc_parent.mkdir()
    nfc_alias = nfc_parent / "synthetic-nfc-alias"
    nfc_alias.mkdir()
    (nfc_alias / "marker.bin").write_bytes(b"nfc alias")
    real_identity = build_windows_installer._name_identity

    def injected_identity(name: str) -> str:
        if name == nfc_alias.name:
            return real_identity(identity)
        return real_identity(name)

    monkeypatch.setattr(build_windows_installer, "_name_identity", injected_identity)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path / "nfc-work", source, stage_parent=nfc_parent)
    assert (nfc_alias / "marker.bin").read_bytes() == b"nfc alias"


def test_dangling_stage_parent_link_is_rejected_and_preserved(monkeypatch, tmp_path):
    source, _document, _members = _valid_zip(tmp_path)
    container = tmp_path / "dangling-container"
    container.mkdir()
    stage_parent = container / "stages"
    missing_target = tmp_path / "missing-target"
    injected = False
    try:
        os.symlink(missing_target, stage_parent, target_is_directory=True)
    except OSError:
        stage_parent.write_bytes(b"synthetic dangling-link placeholder")
        injected = True
        real_detector = build_windows_installer._is_link_or_reparse
        monkeypatch.setattr(
            build_windows_installer,
            "_is_link_or_reparse",
            lambda path, metadata=None: Path(path) == stage_parent
            or real_detector(path, metadata),
        )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert os.path.lexists(stage_parent)
    if injected:
        assert stage_parent.read_bytes() == b"synthetic dangling-link placeholder"
    assert not missing_target.exists()


def test_reparse_at_existing_composite_target_is_conflict_and_preserved(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    staged = _stage(monkeypatch, tmp_path, source)
    snapshot_before = staged.source_zip_snapshot.read_bytes()
    real_detector = build_windows_installer._is_link_or_reparse

    def inject_target_reparse(path, metadata=None):
        if Path(path) == staged.stage_root:
            return True
        return real_detector(path, metadata)

    monkeypatch.setattr(
        build_windows_installer, "_is_link_or_reparse", inject_target_reparse
    )
    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_CONFLICT$"):
        _stage(monkeypatch, tmp_path, source)
    assert staged.source_zip_snapshot.read_bytes() == snapshot_before


def test_stage_tree_validator_failure_uses_exact_task5a_suffix_and_no_publish(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    monkeypatch.setattr(
        build_windows_installer,
        "validate_installer_payload_tree",
        lambda _document, _root: ["MANIFEST_IO_ERROR"],
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source)

    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_STAGE_TREE_INVALID: MANIFEST_IO_ERROR"
    )
    assert not any(
        re.fullmatch(r"[0-9a-f]{64}", child.name)
        for child in (tmp_path / "stages").iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership behavior")
def test_stage_root_creation_handle_blocks_replacement_during_identity_capture(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    real_metadata = build_windows_installer._windows_handle_metadata
    attempted = False
    blocked = False

    def attempt_replacement(handle):
        nonlocal attempted, blocked
        candidates = (
            list(stage_parent.glob(build_windows_installer._STAGE_TEMP_PREFIX + "*"))
            if stage_parent.exists()
            else []
        )
        if candidates and not attempted:
            attempted = True
            temporary = candidates[0]
            displaced = temporary.with_name(temporary.name + "-displaced")
            try:
                os.replace(temporary, displaced)
            except OSError:
                blocked = True
            else:
                os.replace(displaced, temporary)
        return real_metadata(handle)

    monkeypatch.setattr(
        build_windows_installer, "_windows_handle_metadata", attempt_replacement
    )

    staged = _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert attempted is True
    assert blocked is True
    assert staged.stage_root.is_dir()
    assert not list(stage_parent.glob("*-displaced"))


@pytest.mark.skipif(os.name != "nt", reason="Windows handle ownership behavior")
def test_stage_root_swap_after_mkdir_never_becomes_owned_or_quarantined(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    token = "3" * 32
    temporary = stage_parent / (build_windows_installer._STAGE_TEMP_PREFIX + token)
    displaced = temporary.with_name(temporary.name + "-created")
    marker = temporary / "foreign-marker.bin"
    real_mkdir = os.mkdir
    injected = False

    class FixedUUID:
        hex = token

    def replace_created_root(path, mode=0o777):
        nonlocal injected
        result = real_mkdir(path, mode)
        if Path(path) == temporary and not injected:
            injected = True
            os.replace(temporary, displaced)
            real_mkdir(temporary, mode)
            marker.write_bytes(b"foreign replacement")
        return result

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUUID())
    monkeypatch.setattr(build_windows_installer.os, "mkdir", replace_created_root)
    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        lambda _payload, *, artifact_name: ["RELEASE_ZIP_CORRUPT"],
        raising=False,
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_SOURCE_ZIP_INVALID: RELEASE_ZIP_CORRUPT"
    )
    if injected:
        assert marker.read_bytes() == b"foreign replacement"
        assert displaced.is_dir()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle quarantine behavior")
def test_real_cleanup_quarantine_seam_preserves_root_replacement(
    monkeypatch, tmp_path
):
    parent = tmp_path / "parent"
    root = parent / "owned"
    root.mkdir(parents=True)
    (root / "owned.bin").write_bytes(b"owned")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside sentinel")
    parent_chain = build_windows_installer._directory_chain(parent)
    owned_identity = build_windows_installer._stat_identity(os.lstat(root))
    real_quarantine = build_windows_installer._quarantine_windows_handle
    replacement_marker = root / "foreign-marker.bin"
    injected = False

    def replace_before_real_quarantine(handle, parent_handle, destination):
        nonlocal injected
        if not injected:
            injected = True
            displaced = root.with_name(root.name + "-displaced")
            os.replace(root, displaced)
            root.mkdir()
            replacement_marker.write_bytes(b"foreign replacement")
        return real_quarantine(handle, parent_handle, destination)

    monkeypatch.setattr(
        build_windows_installer,
        "_quarantine_windows_handle",
        replace_before_real_quarantine,
    )

    assert build_windows_installer._cleanup_owned(
        root, parent_chain, owned_identity
    ) is True
    assert injected is True
    assert replacement_marker.read_bytes() == b"foreign replacement"
    assert sentinel.read_bytes() == b"outside sentinel"


def _valid_zip_with_deep_member(tmp_path: Path) -> Path:
    source = tmp_path / "input" / ZIP_NAME
    members = _release_members()
    members["PDFVectorImporter/deep/parent/member.bin"] = b"deep member bytes"
    document = candidate_manifest.build_candidate_file_manifest(
        members,
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ZIP_NAME,
    )
    source.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            _regular_info(MANIFEST_MEMBER),
            candidate_manifest.canonical_candidate_manifest_bytes(document),
        )
        for name, payload in members.items():
            archive.writestr(_regular_info(name), payload)
    assert smoke_release_zip.validate_release_zip_manifest(source) == []
    return source


def _replace_directory_with_test_link(path: Path, outside: Path) -> Path:
    displaced = path.with_name(path.name + "-displaced")
    os.replace(path, displaced)
    try:
        os.symlink(outside, path, target_is_directory=True)
    except OSError:
        os.replace(displaced, path)
        pytest.skip("directory-link creation unavailable")
    return displaced


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_absent_stage_parent_component_swap_cannot_create_outside_tree(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    container = tmp_path / "container"
    container.mkdir()
    swapped = container / "new-parent"
    stage_parent = swapped / "nested" / "stages"
    outside = tmp_path / "outside-parent"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside sentinel")
    real_detector = build_windows_installer._is_link_or_reparse
    injected = False

    def swap_after_last_path_check(path, metadata=None):
        nonlocal injected
        result = real_detector(path, metadata)
        if Path(path) == swapped and not injected:
            injected = True
            _replace_directory_with_test_link(swapped, outside)
        return result

    monkeypatch.setattr(
        build_windows_installer, "_is_link_or_reparse", swap_after_last_path_check
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_UNSAFE"
    assert injected is True
    assert sentinel.read_bytes() == b"outside sentinel"
    assert not (outside / "nested").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
@pytest.mark.parametrize(
    ("boundary", "outside_leaf"),
    [
        ("metadata", ZIP_NAME),
        ("source-root", "candidate-file-manifest.json"),
        ("nested", "payload.bin"),
        ("member-parent", "member.bin"),
    ],
)
def test_checked_stage_directory_swap_never_writes_outside_owned_root(
    monkeypatch, tmp_path, boundary, outside_leaf
):
    source = _valid_zip_with_deep_member(tmp_path)
    stage_parent = tmp_path / "stages"
    outside = tmp_path / ("outside-" + boundary)
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside sentinel")
    real_detector = build_windows_installer._is_link_or_reparse
    injected = False

    def is_boundary(candidate: Path) -> bool:
        if boundary == "metadata":
            return candidate.name == build_windows_installer._STAGE_METADATA_DIRNAME
        if boundary == "source-root":
            return candidate.name == "PDFVectorImporter"
        if boundary == "nested":
            return candidate.name == "data" and candidate.parent.name == "PDFVectorImporter"
        return candidate.name == "parent" and candidate.parent.name == "deep"

    def swap_after_last_path_check(path, metadata=None):
        nonlocal injected
        result = real_detector(path, metadata)
        candidate = Path(path)
        if is_boundary(candidate) and not injected:
            injected = True
            _replace_directory_with_test_link(candidate, outside)
        return result

    monkeypatch.setattr(
        build_windows_installer, "_is_link_or_reparse", swap_after_last_path_check
    )

    caught = None
    try:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)
    except RuntimeError as exc:
        caught = exc

    assert sentinel.read_bytes() == b"outside sentinel"
    assert not (outside / outside_leaf).exists()
    if caught is not None:
        assert re.fullmatch(
            r"INSTALLER_(?:SOURCE_CHANGED|STAGE_(?:IO_ERROR|UNSAFE|TREE_INVALID(?:: [A-Z0-9_, ]+)?))",
            _assert_pathless(caught, tmp_path),
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows exclusive leaf behavior")
def test_member_leaf_collision_is_preserved_without_pathname_fallback(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    real_create = getattr(
        build_windows_installer, "_nt_create_file_handle", None
    )
    calls: list[str] = []
    collision_identity = None
    collision_path = None

    def collide(parent_handle, name, *args, **kwargs):
        nonlocal collision_identity, collision_path
        calls.append(name)
        if name == "payload.bin":
            temporary = next(
                stage_parent.glob(build_windows_installer._STAGE_TEMP_PREFIX + "*")
            )
            collision_path = temporary / "PDFVectorImporter" / "data" / name
            collision_path.write_bytes(b"foreign leaf collision")
            collision_identity = build_windows_installer._stat_identity(
                os.lstat(collision_path)
            )
            raise FileExistsError("synthetic foreign leaf collision")
        assert real_create is not None
        return real_create(parent_handle, name, *args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_file_handle",
        collide,
        raising=False,
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_IO_ERROR"
    assert "payload.bin" in calls
    assert collision_path is not None and collision_path.read_bytes() == (
        b"foreign leaf collision"
    )
    assert build_windows_installer._stat_identity(os.lstat(collision_path)) == (
        collision_identity
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows native stage capabilities")
@pytest.mark.parametrize(
    ("seam", "expected"),
    [
        ("_nt_create_directory_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_windows_handle_metadata", "INSTALLER_STAGE_IO_ERROR"),
        ("_nt_create_file_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_publish_windows_directory_handle", "INSTALLER_STAGE_PUBLISH_ERROR"),
    ],
)
def test_missing_native_stage_capability_fails_closed_without_path_fallback(
    monkeypatch, tmp_path, seam, expected
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    calls = 0

    def unavailable(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise OSError("C:/private/native capability unavailable")

    monkeypatch.setattr(
        build_windows_installer, seam, unavailable, raising=False
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert calls >= 1
    assert _assert_pathless(caught.value, tmp_path) == expected
    assert source.is_file()
    assert not any(
        re.fullmatch(r"[0-9a-f]{64}", child.name)
        for child in stage_parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound publication")
@pytest.mark.parametrize("winner_kind", ["exact", "conflict"])
def test_handle_bound_publication_reuses_exact_winner_and_preserves_conflict(
    monkeypatch, tmp_path, winner_kind
):
    source, _document, _members = _valid_zip(tmp_path)
    baseline = _stage(monkeypatch, tmp_path / "baseline", source)
    baseline_tree = {
        path.relative_to(baseline.stage_root).as_posix(): path.read_bytes()
        for path in baseline.stage_root.rglob("*")
        if path.is_file()
    }
    target_parent = tmp_path / ("publish-" + winner_kind)
    real_publish = getattr(
        build_windows_installer, "_publish_windows_directory_handle", None
    )
    injected = False
    conflict_marker = b"foreign conflicting winner"

    def inject_winner(handle, parent_handle, destination, *args, **kwargs):
        nonlocal injected
        destination = Path(destination)
        if not injected and destination.parent == target_parent:
            injected = True
            if winner_kind == "exact":
                shutil.copytree(baseline.stage_root, destination)
            else:
                destination.mkdir()
                (destination / "winner.bin").write_bytes(conflict_marker)
            raise FileExistsError("synthetic concurrent final winner")
        assert real_publish is not None
        return real_publish(handle, parent_handle, destination, *args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer,
        "_publish_windows_directory_handle",
        inject_winner,
        raising=False,
    )

    if winner_kind == "exact":
        staged = _stage(
            monkeypatch,
            tmp_path / "exact-work",
            source,
            stage_parent=target_parent,
        )
        assert {
            path.relative_to(staged.stage_root).as_posix(): path.read_bytes()
            for path in staged.stage_root.rglob("*")
            if path.is_file()
        } == baseline_tree
    else:
        with pytest.raises(RuntimeError) as caught:
            _stage(
                monkeypatch,
                tmp_path / "conflict-work",
                source,
                stage_parent=target_parent,
            )
        assert _assert_pathless(caught.value, tmp_path) == (
            "INSTALLER_STAGE_PUBLISH_ERROR"
        )
        assert next(target_parent.glob("*/winner.bin")).read_bytes() == conflict_marker
    assert injected is True


@pytest.mark.parametrize("failure", ["second-read-oserror", "second-read-mismatch"])
def test_final_attestation_temp_readback_failure_is_atomic_io_error(
    monkeypatch, tmp_path, failure
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    output = tmp_path / "attestation.json"
    seeded = b"seeded attestation bytes"
    output.write_bytes(seeded)
    real_capture = build_windows_installer._capture_regular_file
    real_replace = os.replace
    temp_captures = 0
    final_replace_calls = 0

    def fail_second_temp_capture(path):
        nonlocal temp_captures
        path = Path(path)
        if path.name.startswith(build_windows_installer._ATTESTATION_TEMP_PREFIX):
            temp_captures += 1
            if temp_captures == 2:
                if failure == "second-read-oserror":
                    raise OSError("C:/private/final attestation readback")
                return real_capture(path) + b"altered"
        return real_capture(path)

    def record_final_replace(source_path, destination_path):
        nonlocal final_replace_calls
        if Path(destination_path) == output:
            final_replace_calls += 1
        return real_replace(source_path, destination_path)

    monkeypatch.setattr(
        build_windows_installer, "_capture_regular_file", fail_second_temp_capture
    )
    monkeypatch.setattr(build_windows_installer.os, "replace", record_final_replace)

    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.write_attestation(
            output,
            staged_release=staged,
            installer_exe=setup,
            toolchain_identity=_valid_toolchain_identity(),
        )

    assert temp_captures == 2
    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_ATTESTATION_IO_ERROR"
    )
    assert output.read_bytes() == seeded
    assert final_replace_calls == 0
    assert not any(
        child.name.startswith(build_windows_installer._ATTESTATION_TEMP_PREFIX)
        for child in tmp_path.iterdir()
    )
