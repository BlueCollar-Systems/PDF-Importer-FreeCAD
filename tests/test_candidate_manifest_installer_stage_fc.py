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


def _write_release_zip_with_descriptor_package_mismatch(
    path: Path,
) -> tuple[dict, dict[str, bytes]]:
    members = _release_members(package_version="0.0.1")
    document = candidate_manifest.build_candidate_file_manifest(
        members,
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ZIP_NAME,
    )
    manifest_bytes = candidate_manifest.canonical_candidate_manifest_bytes(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(_regular_info(MANIFEST_MEMBER), manifest_bytes)
        for name, payload in members.items():
            archive.writestr(_regular_info(name), payload)
    assert smoke_release_zip.validate_release_zip_manifest(path) == []
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


def _synthetic_compiler_runner(
    payload: bytes,
    calls: list[list[str]],
    *,
    after_write=None,
):
    """Write the precreated Setup leaf through a compatible native handle."""

    def run(command, **_kwargs):
        command = list(command)
        calls.append(command)
        output_root = Path(next(item[2:] for item in command if item.startswith("/O")))
        basename = next(item[2:] for item in command if item.startswith("/F"))
        setup = output_root / (basename + ".exe")
        assert setup.is_file(), "compiler output must be atomically precreated"
        handle = build_windows_installer._open_windows_no_follow(
            setup,
            build_windows_installer._FILE_READ_DATA
            | build_windows_installer._FILE_WRITE_DATA
            | build_windows_installer._FILE_READ_ATTRIBUTES
            | build_windows_installer._FILE_WRITE_ATTRIBUTES
            | build_windows_installer._SYNCHRONIZE,
            share_delete=True,
        )
        try:
            build_windows_installer._write_windows_file_handle(handle, payload)
            build_windows_installer._flush_windows_file_handle(handle)
        finally:
            build_windows_installer._close_windows_handle(handle)
        if after_write is not None:
            after_write(setup)
        return subprocess.CompletedProcess(command, 0)

    return run


def _compile_capability(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    staged_release=None,
    *,
    output_dir: Path | None = None,
    payload: bytes = b"synthetic setup",
    toolchain_identity: dict[str, str] | None = None,
    after_write=None,
):
    if staged_release is None:
        staged_release = _stage(monkeypatch, tmp_path / "stage-work")
    if output_dir is None:
        output_dir = tmp_path / "compiler-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        _synthetic_compiler_runner(payload, calls, after_write=after_write),
    )
    capability = build_windows_installer.compile_installer(
        tmp_path / "ISCC.exe",
        staged_release,
        toolchain_identity=(
            _valid_toolchain_identity()
            if toolchain_identity is None
            else toolchain_identity
        ),
        output_dir=output_dir,
    )
    return capability, calls


def test_valid_zip_is_snapshotted_validated_and_staged_once(monkeypatch, tmp_path):
    source_zip, document, members = _valid_zip(tmp_path)
    original_bytes = source_zip.read_bytes()
    calls: list[tuple[str, object]] = []
    real_validate = smoke_release_zip.validate_release_zip_manifest_bytes
    real_parse_package = build_windows_installer._parse_package_version_bytes

    def validate(snapshot_bytes: bytes, *, artifact_name: str) -> list[str]:
        calls.append(("validate", artifact_name))
        assert type(snapshot_bytes) is bytes
        assert snapshot_bytes == original_bytes
        assert artifact_name == ZIP_NAME
        return real_validate(snapshot_bytes, artifact_name=artifact_name)

    def parse_package(payload: bytes) -> str:
        calls.append(("package", payload))
        assert type(payload) is bytes
        return real_parse_package(payload)

    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        validate,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer, "_parse_package_version_bytes", parse_package
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_read_package_version",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("stage validation must not reopen package.xml by pathname")
        ),
    )
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
    real_open = getattr(build_windows_installer, "_nt_open_directory_handle", None)
    real_attribute_tag = getattr(
        build_windows_installer, "_query_windows_attribute_tag", None
    )
    stage_handles: set[int] = set()

    def capture_stage_parent(parent_handle, name, *args, **kwargs):
        assert real_open is not None
        handle = real_open(parent_handle, name, *args, **kwargs)
        if str(name) == stage_parent.name:
            stage_handles.add(_handle_key(handle))
        return handle

    def injected_attribute_tag(handle):
        assert real_attribute_tag is not None
        attributes, tag = real_attribute_tag(handle)
        if _handle_key(handle) in stage_handles:
            return attributes | build_windows_installer._FILE_ATTRIBUTE_REPARSE_POINT, 0xA0000003
        return attributes, tag

    monkeypatch.setattr(
        build_windows_installer,
        "_nt_open_directory_handle",
        capture_stage_parent,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_query_windows_attribute_tag",
        injected_attribute_tag,
        raising=False,
    )
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
    calls = 0

    def fail_stage_publish(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise OSError("C:/private/publish failure")

    monkeypatch.setattr(
        build_windows_installer,
        "_publish_windows_directory_handle",
        fail_stage_publish,
        raising=False,
    )
    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip, stage_parent=stage_parent)

    assert calls == 1
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_PUBLISH_ERROR"
    assert not any(path.name.startswith(".installer-stage-") for path in stage_parent.iterdir())
    assert source_zip.is_file()


def test_prepublication_mutation_is_detected_before_final_handle_rename(
    monkeypatch, tmp_path
):
    source_zip, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    real_validate = getattr(
        build_windows_installer, "_validate_windows_stage_handles", None
    )
    publish_calls = 0
    injected = False

    def mutate_before_validation(*args, **kwargs):
        nonlocal injected
        temporary = next(
            stage_parent.glob(build_windows_installer._STAGE_TEMP_PREFIX + "*")
        )
        (temporary / "PDFVectorImporter" / "late.bin").write_bytes(b"late")
        injected = True
        assert real_validate is not None
        return real_validate(*args, **kwargs)

    def record_publish(*_args, **_kwargs):
        nonlocal publish_calls
        publish_calls += 1
        raise AssertionError("invalid stage must not be renamed")

    monkeypatch.setattr(
        build_windows_installer,
        "_validate_windows_stage_handles",
        mutate_before_validation,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_publish_windows_directory_handle",
        record_publish,
        raising=False,
    )
    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source_zip, stage_parent=stage_parent)

    assert injected is True
    assert publish_calls == 0
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_TREE_INVALID"


def test_compiler_revalidates_stage_and_uses_exact_source_directory(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    output = tmp_path / "compiler-output"
    capability, calls = _compile_capability(
        monkeypatch, tmp_path, staged, output_dir=output
    )
    result = build_windows_installer.finalize_compiled_installer(capability)

    assert result.read_bytes() == b"synthetic setup"
    assert len(calls) == 1
    assert f"/DSourceDir={staged.source_dir}" in calls[0]

    (staged.source_dir / "module.py").write_bytes(b"tampered")
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=output,
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
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            mutated,
            toolchain_identity=_valid_toolchain_identity(),
        )
    assert calls == []

    def fail_compiler(command, **_kwargs):
        raise subprocess.CalledProcessError(7, command, stderr="C:/private/owner.pdf")

    monkeypatch.setattr(build_windows_installer.subprocess, "run", fail_compiler)
    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
        )
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_COMPILER_FAILED"


def test_attestation_1_1_binds_stable_setup_zip_toolchain_and_manifest(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    identity = {
        "name": "Inno Setup",
        "version": "6.7.1",
        "source_sha256": "a" * 64,
        "manifest_sha256": "b" * 64,
        "tree_sha256": "c" * 64,
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first_capability, _first_calls = _compile_capability(
        monkeypatch,
        tmp_path / "first-build",
        staged,
        output_dir=tmp_path / "first-output",
        payload=b"stable setup bytes",
        toolchain_identity=identity,
    )
    second_capability, _second_calls = _compile_capability(
        monkeypatch,
        tmp_path / "second-build",
        staged,
        output_dir=tmp_path / "second-output",
        payload=b"stable setup bytes",
        toolchain_identity=identity,
    )

    build_windows_installer.write_attestation(
        first,
        compiled_installer=first_capability,
    )
    build_windows_installer.write_attestation(
        second,
        compiled_installer=second_capability,
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
            "name": f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe",
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


def test_compiler_rejects_hardlinked_loose_winner_and_preserves_existing_bytes(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    output_dir = tmp_path / "compiler-output"
    output_dir.mkdir()
    setup = output_dir / f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe"
    setup.write_bytes(b"setup")
    other = output_dir / "Setup-copy.exe"
    try:
        os.link(setup, other)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    output = tmp_path / "attestation.json"
    output.write_bytes(b"preserve existing attestation")

    calls: list[list[str]] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        _synthetic_compiler_runner(b"setup", calls),
    )
    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=output_dir,
        )

    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_COMPILER_FAILED"
    assert output.read_bytes() == b"preserve existing attestation"


def test_attestation_detects_setup_path_replacement_before_publication(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    output_dir = tmp_path / "compiler-output"
    capability, _calls = _compile_capability(
        monkeypatch,
        tmp_path / "build",
        staged,
        output_dir=output_dir,
        payload=b"original setup",
    )
    setup = output_dir / f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe"
    replacement = tmp_path / "replacement.exe"
    replacement.write_bytes(b"replacement setup")
    output = tmp_path / "attestation.json"
    with pytest.raises(OSError):
        os.replace(replacement, setup)

    expected_setup = capability.output_lease.loose_path
    result = build_windows_installer.write_attestation(
        output,
        compiled_installer=capability,
    )

    assert result == expected_setup
    assert setup.read_bytes() == b"original setup"
    assert replacement.read_bytes() == b"replacement setup"


def test_attestation_publication_failure_preserves_existing_bytes_and_cleans_temp(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    capability, _calls = _compile_capability(
        monkeypatch, tmp_path / "build", staged, payload=b"setup"
    )
    output = tmp_path / "attestation.json"
    output.write_bytes(b"preserve existing attestation")
    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.write_attestation(
            output,
            compiled_installer=capability,
        )

    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_ATTESTATION_PUBLISH_ERROR"
    )
    assert output.read_bytes() == b"preserve existing attestation"
    assert not any(path.name.startswith(".installer-attestation-") for path in tmp_path.iterdir())


def test_main_retains_source_commit_as_equality_only_and_reuses_one_stage(
    monkeypatch, tmp_path, capsys
):
    staged = _stage(monkeypatch, tmp_path)
    setup = tmp_path / "Setup.exe"
    setup.write_bytes(b"setup")
    compile_calls: list[object] = []
    attest_calls: list[object] = []
    compiled_capability = object()
    monkeypatch.setattr(sys, "argv", ["build_windows_installer.py", "--source-commit", SOURCE_COMMIT, "--attestation", str(tmp_path / "a.json")])
    monkeypatch.setattr(build_windows_installer, "find_iscc", lambda _path: tmp_path / "ISCC.exe")
    monkeypatch.setattr(build_windows_installer, "verify_inno_toolchain", lambda *_args: {"version": "synthetic"})
    monkeypatch.setattr(build_windows_installer, "stage_release", lambda *_args, **_kwargs: staged)

    def compile_same(_iscc, staged_release, *, toolchain_identity, **_kwargs):
        compile_calls.append((staged_release, toolchain_identity))
        return compiled_capability

    def attest_same(_output, *, compiled_installer):
        attest_calls.append(compiled_installer)
        return setup

    monkeypatch.setattr(build_windows_installer, "compile_installer", compile_same)
    monkeypatch.setattr(build_windows_installer, "write_attestation", attest_same)
    assert build_windows_installer.main() == 0
    assert compile_calls == [(staged, {"version": "synthetic"})]
    assert attest_calls == [compiled_capability]
    assert f"Installer:   {setup}" in capsys.readouterr().out

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
            tmp_path / "ISCC.exe",
            mixed,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=tmp_path / "out",
        )

    assert calls == []


def test_attestation_blocks_zip_a_manifest_tree_b_and_preserves_seeded_bytes(
    monkeypatch, tmp_path
):
    mixed = _mixed_stage_from_zip_a_and_tree_b(monkeypatch, tmp_path)
    output = tmp_path / "attestation.json"
    seeded = b"seeded attestation"
    output.write_bytes(seeded)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"):
        build_windows_installer.write_attestation(
            output,
            staged_release=mixed,
            installer_exe=tmp_path / "Setup.exe",
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
    attempted = False
    blocked = False
    original = source_a.read_bytes()
    temporary = None
    real_create_directory = getattr(
        build_windows_installer, "_nt_create_directory_handle", None
    )

    def capture_temporary_root(parent_handle, name, *args, **kwargs):
        nonlocal temporary
        assert real_create_directory is not None
        handle = real_create_directory(parent_handle, name, *args, **kwargs)
        if str(name).startswith(build_windows_installer._STAGE_TEMP_PREFIX):
            temporary = stage_parent / str(name)
        return handle

    def substitute_after_validation(payload: bytes, *, artifact_name: str):
        nonlocal calls, attempted, blocked
        calls += 1
        result = real_validate(payload, artifact_name=artifact_name)
        if calls == 1:
            assert temporary is not None
            snapshot = temporary / ".installer-source" / ZIP_NAME
            attempted = True
            try:
                snapshot.write_bytes(replacement)
            except OSError:
                blocked = True
        return result

    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_directory_handle",
        capture_temporary_root,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        substitute_after_validation,
        raising=False,
    )

    staged = _stage(
        monkeypatch,
        tmp_path,
        source_a,
        stage_parent=stage_parent,
    )

    assert attempted is True
    assert blocked is True
    assert staged.source_zip_snapshot.read_bytes() == original
    assert staged.source_zip_snapshot.read_bytes() != replacement


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
    real_create = getattr(
        build_windows_installer, "_nt_create_directory_handle", None
    )
    calls = 0

    class FixedUUID:
        hex = token

    def collide_create(parent_handle, name, *args, **kwargs):
        nonlocal calls
        if str(name) == temporary.name:
            calls += 1
            temporary.mkdir()
            marker.write_bytes(b"concurrent winner")
            raise FileExistsError("synthetic collision")
        assert real_create is not None
        return real_create(parent_handle, name, *args, **kwargs)

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUUID())
    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_directory_handle",
        collide_create,
        raising=False,
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert calls == 1
    assert marker.read_bytes() == b"concurrent winner"


def test_attestation_temp_collision_before_ownership_is_preserved_and_io_error(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    capability, _calls = _compile_capability(
        monkeypatch, tmp_path / "build", staged, payload=b"setup"
    )
    output = tmp_path / "attestation.json"
    output.write_bytes(b"seeded")
    token = "2" * 32
    temporary = tmp_path / (build_windows_installer._ATTESTATION_TEMP_PREFIX + token)
    class FixedUUID:
        hex = token

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUUID())
    temporary.write_bytes(b"concurrent temp")

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_IO_ERROR$"):
        build_windows_installer.write_attestation(
            output,
            compiled_installer=capability,
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


def test_retained_root_blocks_name_change_before_quarantine_failure_token(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    attempted = False
    blocked = False

    monkeypatch.setattr(
        build_windows_installer,
        "validate_release_zip_manifest_bytes",
        lambda payload, *, artifact_name: ["RELEASE_ZIP_CORRUPT"],
        raising=False,
    )

    def fail_after_name_change_attempt(handle, parent_handle, destination):
        nonlocal attempted, blocked
        attempted = True
        temporary = next(
            stage_parent.glob(build_windows_installer._STAGE_TEMP_PREFIX + "*")
        )
        displaced = temporary.with_name(temporary.name + "-displaced")
        try:
            os.replace(temporary, displaced)
        except OSError:
            blocked = True
        else:
            os.replace(displaced, temporary)
        return False

    monkeypatch.setattr(
        build_windows_installer,
        "_quarantine_windows_handle",
        fail_after_name_change_attempt,
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_IO_ERROR$"):
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert attempted is True
    assert blocked is True
    assert next(stage_parent.glob(".installer-stage-*"), None) is not None
    assert not list(stage_parent.glob("*-displaced"))


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
    if phase.startswith("stage"):
        source, _document, _members = _valid_zip(tmp_path)
        monkeypatch.setattr(
            build_windows_installer,
            "_quarantine_windows_handle",
            lambda *_args, **_kwargs: False,
        )
        if phase == "stage-prepublish":
            monkeypatch.setattr(
                build_windows_installer,
                "validate_release_zip_manifest_bytes",
                lambda payload, *, artifact_name: ["RELEASE_ZIP_CORRUPT"],
                raising=False,
            )
        else:
            monkeypatch.setattr(
                build_windows_installer,
                "_publish_windows_directory_handle",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    OSError("synthetic publish failure")
                ),
                raising=False,
            )
        with pytest.raises(RuntimeError) as caught:
            _stage(monkeypatch, tmp_path, source)
    else:
        staged = _stage(monkeypatch, tmp_path)
        capability, _calls = _compile_capability(
            monkeypatch, tmp_path / "build", staged, payload=b"setup"
        )
        output = tmp_path / "attestation.json"
        output.write_bytes(b"seeded")
        monkeypatch.setattr(
            build_windows_installer,
            "_dispose_windows_file_handle",
            lambda *_args, **_kwargs: False,
        )
        if phase == "attestation-prepublish":
            real_read = build_windows_installer._read_windows_file_handle

            def fail_temp_read(handle):
                if build_windows_installer._query_windows_opened_name(handle).startswith(
                    build_windows_installer._ATTESTATION_TEMP_PREFIX
                ):
                    raise OSError("synthetic readback failure")
                return real_read(handle)

            monkeypatch.setattr(
                build_windows_installer, "_read_windows_file_handle", fail_temp_read
            )
        else:
            real_rename = build_windows_installer._rename_windows_handle

            def fail_publish(handle, parent_handle, destination_path):
                if Path(destination_path) == output.absolute():
                    raise OSError("synthetic publish failure")
                return real_rename(handle, parent_handle, destination_path)

            monkeypatch.setattr(
                build_windows_installer, "_rename_windows_handle", fail_publish
            )
        with pytest.raises(RuntimeError) as caught:
            build_windows_installer.write_attestation(
                output,
                compiled_installer=capability,
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
    real_create = getattr(
        build_windows_installer, "_nt_create_directory_handle", None
    )
    real_attribute_tag = getattr(
        build_windows_installer, "_query_windows_attribute_tag", None
    )
    root_handles: set[int] = set()

    def capture_root(parent_handle, name, *args, **kwargs):
        assert real_create is not None
        handle = real_create(parent_handle, name, *args, **kwargs)
        if str(name).startswith(build_windows_installer._STAGE_TEMP_PREFIX):
            root_handles.add(_handle_key(handle))
        return handle

    def inject_temp_reparse(handle):
        assert real_attribute_tag is not None
        attributes, tag = real_attribute_tag(handle)
        if _handle_key(handle) in root_handles:
            return attributes | build_windows_installer._FILE_ATTRIBUTE_REPARSE_POINT, 0xA0000003
        return attributes, tag

    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_directory_handle",
        capture_root,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_query_windows_attribute_tag",
        inject_temp_reparse,
        raising=False,
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert sentinel.read_bytes() == b"preserve"


def test_changed_stage_ancestor_before_publish_is_unsafe_and_preserves_source(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    original = source.read_bytes()
    real_revalidate = getattr(
        build_windows_installer, "_revalidate_windows_handle_chain", None
    )
    calls = 0

    def change_on_publish(chain):
        nonlocal calls
        calls += 1
        if calls >= 2:
            raise build_windows_installer._ChangedPath
        assert real_revalidate is not None
        return real_revalidate(chain)

    monkeypatch.setattr(
        build_windows_installer,
        "_revalidate_windows_handle_chain",
        change_on_publish,
        raising=False,
    )

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
    real_publish = getattr(
        build_windows_installer, "_publish_windows_directory_handle", None
    )
    injected = False

    def exact_winner_then_fail(handle, parent_handle, destination_path, *args, **kwargs):
        nonlocal injected
        destination_path = Path(destination_path)
        if (
            not injected
            and destination_path.parent == target_parent
            and re.fullmatch(r"[0-9a-f]{64}", destination_path.name)
        ):
            injected = True
            shutil.copytree(baseline.stage_root, destination_path)
            raise FileExistsError("lost publication race")
        assert real_publish is not None
        return real_publish(handle, parent_handle, destination_path, *args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer,
        "_publish_windows_directory_handle",
        exact_winner_then_fail,
        raising=False,
    )
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

    def conflict_then_fail(handle, parent_handle, destination_path, *args, **kwargs):
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
            raise FileExistsError("lost publication race")
        assert real_publish is not None
        return real_publish(handle, parent_handle, destination_path, *args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer,
        "_publish_windows_directory_handle",
        conflict_then_fail,
        raising=False,
    )
    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_CONFLICT$"):
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
            tmp_path / "ISCC.exe",
            mutated,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=tmp_path / "output",
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
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
        )
    assert calls == []


def test_compiled_stage_blocks_zip_mutation_through_attestation(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    capability, _calls = _compile_capability(
        monkeypatch, tmp_path / "build", staged, payload=b"setup"
    )
    output = tmp_path / "attestation.json"

    with pytest.raises(OSError):
        staged.source_zip_snapshot.write_bytes(b"mutated snapshot")

    expected_setup = capability.output_lease.loose_path
    assert build_windows_installer.write_attestation(
        output,
        compiled_installer=capability,
    ) == expected_setup


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("temp-mkdir", "INSTALLER_STAGE_IO_ERROR"),
        ("source-copy", "INSTALLER_SOURCE_IO_ERROR"),
        ("source-fsync", "INSTALLER_SOURCE_IO_ERROR"),
        ("source-close", "INSTALLER_STAGE_IO_ERROR"),
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
        real_create = getattr(
            build_windows_installer, "_nt_create_directory_handle", None
        )

        def fail_temp_mkdir(parent_handle, name, *args, **kwargs):
            if str(name).startswith(
                build_windows_installer._STAGE_TEMP_PREFIX
            ):
                raise OSError("synthetic mkdir failure")
            assert real_create is not None
            return real_create(parent_handle, name, *args, **kwargs)

        monkeypatch.setattr(
            build_windows_installer,
            "_nt_create_directory_handle",
            fail_temp_mkdir,
            raising=False,
        )
    elif failure == "source-copy":
        real_create = getattr(build_windows_installer, "_nt_create_file_handle", None)

        def fail_snapshot_create(parent_handle, name, *args, **kwargs):
            if str(name) == ZIP_NAME:
                raise OSError("synthetic copy failure")
            assert real_create is not None
            return real_create(parent_handle, name, *args, **kwargs)

        monkeypatch.setattr(
            build_windows_installer,
            "_nt_create_file_handle",
            fail_snapshot_create,
            raising=False,
        )
    elif failure == "source-fsync":
        monkeypatch.setattr(
            build_windows_installer,
            "_flush_windows_file_handle",
            lambda _handle: (_ for _ in ()).throw(OSError("synthetic fsync failure")),
            raising=False,
        )
    elif failure == "source-close":
        real_create = getattr(build_windows_installer, "_nt_create_file_handle", None)
        real_close = build_windows_installer._close_windows_handle
        snapshot_handles: set[int] = set()

        def capture_snapshot(parent_handle, name, *args, **kwargs):
            assert real_create is not None
            handle = real_create(parent_handle, name, *args, **kwargs)
            if str(name) == ZIP_NAME:
                snapshot_handles.add(_handle_key(handle))
            return handle

        def fail_snapshot_close(handle):
            if _handle_key(handle) in snapshot_handles:
                raise OSError("synthetic close failure")
            return real_close(handle)

        monkeypatch.setattr(
            build_windows_installer,
            "_nt_create_file_handle",
            capture_snapshot,
            raising=False,
        )
        monkeypatch.setattr(
            build_windows_installer, "_close_windows_handle", fail_snapshot_close
        )
    elif failure == "member-write":
        real_create = getattr(build_windows_installer, "_nt_create_file_handle", None)
        real_write = getattr(build_windows_installer, "_write_windows_file_handle", None)
        member_handles: set[int] = set()

        def capture_member(parent_handle, name, *args, **kwargs):
            assert real_create is not None
            handle = real_create(parent_handle, name, *args, **kwargs)
            if str(name) == "payload.bin":
                member_handles.add(_handle_key(handle))
            return handle

        def fail_member_write(handle, payload):
            if _handle_key(handle) in member_handles:
                raise OSError("synthetic member write")
            assert real_write is not None
            return real_write(handle, payload)

        monkeypatch.setattr(
            build_windows_installer,
            "_nt_create_file_handle",
            capture_member,
            raising=False,
        )
        monkeypatch.setattr(
            build_windows_installer,
            "_write_windows_file_handle",
            fail_member_write,
            raising=False,
        )
    else:
        real_read = build_windows_installer._read_windows_file_handle

        def fail_retained_package_read(handle):
            if build_windows_installer._query_windows_opened_name(handle) == "package.xml":
                raise OSError("synthetic retained package read")
            return real_read(handle)

        monkeypatch.setattr(
            build_windows_installer,
            "_read_windows_file_handle",
            fail_retained_package_read,
        )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert _assert_pathless(caught.value, tmp_path) == expected
    assert source.read_bytes() == source_bytes
    assert not any(re.fullmatch(r"[0-9a-f]{64}", path.name) for path in stage_parent.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows terminal construction close law")
@pytest.mark.parametrize("role", ["source-file", "member-file", "owned-dir"])
def test_construction_descendant_close_failure_preserves_namespace_without_quarantine(
    monkeypatch, tmp_path, role
):
    source, _document, _members = _valid_zip(tmp_path / "source")
    stage_parent = tmp_path / "stages"
    real_close = build_windows_installer._close_windows_entry
    real_quarantine = build_windows_installer._quarantine_windows_handle
    failures: list[str] = []
    quarantines: list[Path] = []

    def fail_selected(entry):
        if not failures and entry.role == role:
            failures.append(role)
            build_windows_installer._force_close_windows_handle(entry.handle)
            entry.closed = True
            raise OSError("synthetic construction descendant close failure")
        return real_close(entry)

    def record_quarantine(handle, parent_handle, destination):
        quarantines.append(Path(destination))
        return real_quarantine(handle, parent_handle, destination)

    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", fail_selected)
    monkeypatch.setattr(
        build_windows_installer, "_quarantine_windows_handle", record_quarantine
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_IO_ERROR$"):
        _stage(monkeypatch, tmp_path / "work", source, stage_parent=stage_parent)

    assert failures == [role]
    assert quarantines == []
    preserved = [
        child
        for child in stage_parent.iterdir()
        if child.name.startswith(build_windows_installer._STAGE_TEMP_PREFIX)
    ]
    assert len(preserved) == 1
    assert (preserved[0] / "PDFVectorImporter" / "package.xml").is_file()
    assert not any(
        child.name.startswith(build_windows_installer._STAGE_QUARANTINE_PREFIX)
        for child in stage_parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows partial adopted close law")
def test_partial_adopted_close_failure_preserves_published_stage_without_quarantine(
    monkeypatch, tmp_path
):
    source, document, _members = _valid_zip(tmp_path / "source")
    stage_parent = tmp_path / "stages"
    expected_identity = build_windows_installer._stage_identity(
        candidate_manifest.candidate_manifest_sha256(document),
        hashlib.sha256(source.read_bytes()).hexdigest(),
    )
    real_read = build_windows_installer._read_windows_file_handle
    real_close = build_windows_installer._close_windows_entry
    real_quarantine = build_windows_installer._quarantine_windows_handle
    module_reads = 0
    close_failures: list[str] = []
    quarantines: list[Path] = []

    def fail_during_adopted_open(handle):
        nonlocal module_reads
        payload = real_read(handle)
        if build_windows_installer._query_windows_opened_name(handle) == "module.py":
            module_reads += 1
            if module_reads >= 2:
                return b"VALUE = 'foreign adopted payload'\n"
        return payload

    def fail_partial_close(entry):
        if (
            not close_failures
            and entry.role == "stage-file:PDFVectorImporter/module.py"
        ):
            close_failures.append(entry.role)
            build_windows_installer._force_close_windows_handle(entry.handle)
            entry.closed = True
            raise OSError("synthetic partial adopted close failure")
        return real_close(entry)

    def record_quarantine(handle, parent_handle, destination):
        quarantines.append(Path(destination))
        return real_quarantine(handle, parent_handle, destination)

    monkeypatch.setattr(build_windows_installer, "_read_windows_file_handle", fail_during_adopted_open)
    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", fail_partial_close)
    monkeypatch.setattr(
        build_windows_installer, "_quarantine_windows_handle", record_quarantine
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_IO_ERROR$"):
        _stage(monkeypatch, tmp_path / "work", source, stage_parent=stage_parent)

    assert close_failures == ["stage-file:PDFVectorImporter/module.py"]
    assert quarantines == []
    final_root = stage_parent / expected_identity
    assert final_root.is_dir()
    assert (final_root / "PDFVectorImporter" / "module.py").is_file()
    assert not any(
        child.name.startswith(build_windows_installer._STAGE_QUARANTINE_PREFIX)
        for child in stage_parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows retained package version authority")
def test_adopted_stage_cross_binds_retained_package_xml_without_pathname_authority(
    monkeypatch, tmp_path
):
    source = tmp_path / "source" / ZIP_NAME
    _write_release_zip_with_descriptor_package_mismatch(source)
    pathname_reads: list[Path] = []

    def forged_pathname_version(path):
        pathname_reads.append(Path(path))
        return PACKAGE_VERSION

    monkeypatch.setattr(build_windows_installer, "read_version", lambda: PACKAGE_VERSION)
    monkeypatch.setattr(
        build_windows_installer, "_read_package_version", forged_pathname_version
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_TREE_INVALID$"):
        build_windows_installer.stage_release(
            source,
            dist_dir=tmp_path / "dist",
            stage_dir=tmp_path / "stages",
        )

    assert pathname_reads == []
    assert not any(
        re.fullmatch(r"[0-9a-f]{64}", child.name)
        for child in (tmp_path / "stages").iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows ordinary package version authority")
def test_ordinary_stage_lease_rejects_descriptor_package_version_mismatch(
    tmp_path,
):
    source = tmp_path / "source" / ZIP_NAME
    document, members = _write_release_zip_with_descriptor_package_mismatch(source)
    source_bytes = source.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    manifest_bytes = candidate_manifest.canonical_candidate_manifest_bytes(document)
    manifest_sha256 = candidate_manifest.candidate_manifest_sha256(document)
    identity = build_windows_installer._stage_identity(manifest_sha256, source_sha256)
    stage_root = tmp_path / "stages" / identity
    snapshot = stage_root / build_windows_installer._STAGE_METADATA_DIRNAME / ZIP_NAME
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(source_bytes)
    for member, payload in {
        MANIFEST_MEMBER: manifest_bytes,
        **members,
    }.items():
        destination = stage_root / Path(member)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    staged = build_windows_installer.InstallerStage(
        version=PACKAGE_VERSION,
        stage_root=stage_root,
        source_dir=stage_root / "PDFVectorImporter",
        source_zip_snapshot=snapshot,
        source_zip_name=ZIP_NAME,
        source_zip_size=len(source_bytes),
        source_zip_sha256=source_sha256,
        candidate_manifest_bytes=manifest_bytes,
        installed_manifest_sha256=manifest_sha256,
        stage_identity_sha256=identity,
    )

    with pytest.raises(build_windows_installer._ChangedPath):
        build_windows_installer._acquire_windows_stage_read_lease(staged)


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
def test_compiler_rejects_noncanonical_toolchain_inputs_pathlessly_and_atomically(
    monkeypatch, tmp_path, identity
):
    staged = _stage(monkeypatch, tmp_path)
    output = tmp_path / "compiler-output"
    output.mkdir()
    marker = output / "foreign.bin"
    marker.write_bytes(b"seeded output")
    calls: list[object] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(object()),
    )

    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=identity,
            output_dir=output,
        )

    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_COMPILER_INPUT_INVALID"
    )
    assert calls == []
    assert marker.read_bytes() == b"seeded output"
    assert not any(
        path.name.startswith(".installer-output-") for path in output.iterdir()
    )


@pytest.mark.parametrize("failure", ["write", "close", "readback"])
def test_attestation_temp_failures_are_atomic_io_errors(
    monkeypatch, tmp_path, failure
):
    staged = _stage(monkeypatch, tmp_path)
    capability, _calls = _compile_capability(
        monkeypatch, tmp_path / "build", staged, payload=b"setup"
    )
    output = tmp_path / "attestation.json"
    output.write_bytes(b"seeded")
    real_write = build_windows_installer._write_windows_file_handle
    real_read = build_windows_installer._read_windows_file_handle
    real_close_entry = build_windows_installer._close_windows_entry

    def fault_write(handle, payload):
        if failure == "write":
            raise OSError("synthetic write failure")
        return real_write(handle, payload)

    def fault_read(handle):
        if failure == "readback" and build_windows_installer._query_windows_opened_name(
            handle
        ).startswith(build_windows_installer._ATTESTATION_TEMP_PREFIX):
            raise OSError("synthetic readback failure")
        return real_read(handle)

    def fault_close(entry):
        if failure == "close" and entry.role == "attestation-temp":
            build_windows_installer._force_close_windows_handle(entry.handle)
            entry.closed = True
            raise OSError("synthetic close failure")
        return real_close_entry(entry)

    monkeypatch.setattr(build_windows_installer, "_write_windows_file_handle", fault_write)
    monkeypatch.setattr(build_windows_installer, "_read_windows_file_handle", fault_read)
    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", fault_close)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_IO_ERROR$"):
        build_windows_installer.write_attestation(
            output,
            compiled_installer=capability,
        )

    assert output.read_bytes() == b"seeded"


def test_successful_attestation_replace_is_final_without_post_commit_capture(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path)
    capability, _calls = _compile_capability(
        monkeypatch, tmp_path / "build", staged, payload=b"setup"
    )
    output = tmp_path / "attestation.json"
    real_rename = build_windows_installer._rename_windows_handle
    real_read = build_windows_installer._read_windows_file_handle
    committed = False
    post_commit_captures = 0

    def mark_commit(handle, parent_handle, destination):
        nonlocal committed
        result = real_rename(handle, parent_handle, destination)
        if Path(destination) == output.absolute():
            committed = True
        return result

    def forbid_post_commit_capture(handle):
        nonlocal post_commit_captures
        if committed:
            post_commit_captures += 1
            raise OSError("post-commit capture forbidden")
        return real_read(handle)

    monkeypatch.setattr(build_windows_installer, "_rename_windows_handle", mark_commit)
    monkeypatch.setattr(
        build_windows_installer,
        "_read_windows_file_handle",
        forbid_post_commit_capture,
    )

    expected_setup = capability.output_lease.loose_path
    result = build_windows_installer.write_attestation(
        output,
        compiled_installer=capability,
    )

    assert result == expected_setup
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
    real_open = getattr(build_windows_installer, "_nt_open_directory_handle", None)
    real_read_open = getattr(
        build_windows_installer, "_nt_open_directory_read_handle", None
    )
    real_attribute_tag = getattr(
        build_windows_installer, "_query_windows_attribute_tag", None
    )
    target_handles: set[int] = set()

    def capture_target(parent_handle, name, *args, **kwargs):
        assert real_open is not None
        handle = real_open(parent_handle, name, *args, **kwargs)
        if str(name) == staged.stage_root.name:
            target_handles.add(_handle_key(handle))
        return handle

    def capture_read_target(parent_handle, name, *args, **kwargs):
        assert real_read_open is not None
        handle = real_read_open(parent_handle, name, *args, **kwargs)
        if str(name) == staged.stage_root.name:
            target_handles.add(_handle_key(handle))
        return handle

    def inject_target_reparse(handle):
        assert real_attribute_tag is not None
        attributes, tag = real_attribute_tag(handle)
        if _handle_key(handle) in target_handles:
            return attributes | build_windows_installer._FILE_ATTRIBUTE_REPARSE_POINT, 0xA0000003
        return attributes, tag

    monkeypatch.setattr(
        build_windows_installer,
        "_nt_open_directory_handle",
        capture_target,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_nt_open_directory_read_handle",
        capture_read_target,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_query_windows_attribute_tag",
        inject_target_reparse,
        raising=False,
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
        "_validate_windows_stage_handles",
        lambda *_args, **_kwargs: ["MANIFEST_IO_ERROR"],
        raising=False,
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
def test_stage_root_collision_never_becomes_owned_or_quarantined(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    token = "3" * 32
    temporary = stage_parent / (build_windows_installer._STAGE_TEMP_PREFIX + token)
    marker = temporary / "foreign-marker.bin"
    stage_parent.mkdir()
    temporary.mkdir()
    marker.write_bytes(b"foreign replacement")
    original_identity = build_windows_installer._stat_identity(os.lstat(temporary))
    real_create = getattr(
        build_windows_installer, "_nt_create_directory_handle", None
    )
    calls = 0

    class FixedUUID:
        hex = token

    def observe_collision(parent_handle, name, *args, **kwargs):
        nonlocal calls
        if str(name) == temporary.name:
            calls += 1
        assert real_create is not None
        return real_create(parent_handle, name, *args, **kwargs)

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUUID())
    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_directory_handle",
        observe_collision,
        raising=False,
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_STAGE_UNSAFE$"):
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert calls == 0
    assert marker.read_bytes() == b"foreign replacement"
    assert build_windows_installer._stat_identity(os.lstat(temporary)) == original_identity


@pytest.mark.skipif(os.name != "nt", reason="Windows handle quarantine behavior")
def test_real_cleanup_quarantine_seam_blocks_name_change_then_moves_original(
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
    attempted = False
    blocked = False

    def attempt_name_change_then_quarantine(handle, parent_handle, destination):
        nonlocal attempted, blocked
        if not attempted:
            attempted = True
            displaced = root.with_name(root.name + "-displaced")
            try:
                os.replace(root, displaced)
            except OSError:
                blocked = True
            else:
                os.replace(displaced, root)
        return real_quarantine(handle, parent_handle, destination)

    monkeypatch.setattr(
        build_windows_installer,
        "_quarantine_windows_handle",
        attempt_name_change_then_quarantine,
    )

    assert build_windows_installer._cleanup_owned(
        root, parent_chain, owned_identity
    ) is True
    assert attempted is True
    assert blocked is True
    assert not root.exists()
    assert next(parent.glob(".installer-quarantine-*/owned.bin")).read_bytes() == (
        b"owned"
    )
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
    real_create = getattr(
        build_windows_installer, "_nt_create_directory_handle", None
    )
    attempted = False
    blocked = False
    delete_attempted = False
    delete_blocked = False

    def attempt_swap_before_child_create(parent_handle, name, *args, **kwargs):
        nonlocal attempted, blocked, delete_attempted, delete_blocked
        if str(name) == "nested" and not attempted:
            attempted = True
            try:
                _replace_directory_with_test_link(swapped, outside)
            except OSError:
                blocked = True
            delete_attempted = True
            try:
                os.rmdir(swapped)
            except OSError:
                delete_blocked = True
        assert real_create is not None
        return real_create(parent_handle, name, *args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_directory_handle",
        attempt_swap_before_child_create,
        raising=False,
    )

    staged = _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert attempted is True
    assert blocked is True
    assert delete_attempted is True
    assert delete_blocked is True
    assert staged.stage_root.is_dir()
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
    real_create = getattr(build_windows_installer, "_nt_create_file_handle", None)
    real_create_directory = getattr(
        build_windows_installer, "_nt_create_directory_handle", None
    )
    attempted = False
    blocked = False
    retained_paths: dict[int, Path] = {}

    def retain_directory_path(parent_handle, name, *args, **kwargs):
        assert real_create_directory is not None
        handle = real_create_directory(parent_handle, name, *args, **kwargs)
        parent_path = retained_paths.get(_handle_key(parent_handle))
        if str(name).startswith(build_windows_installer._STAGE_TEMP_PREFIX):
            candidate = stage_parent / str(name)
        elif parent_path is not None:
            candidate = parent_path / str(name)
        else:
            candidate = None
        if candidate is not None:
            retained_paths[_handle_key(handle)] = candidate
        return handle

    def is_boundary(candidate: Path) -> bool:
        if boundary == "metadata":
            return candidate.name == build_windows_installer._STAGE_METADATA_DIRNAME
        if boundary == "source-root":
            return candidate.name == "PDFVectorImporter"
        if boundary == "nested":
            return candidate.name == "data" and candidate.parent.name == "PDFVectorImporter"
        return candidate.name == "parent" and candidate.parent.name == "deep"

    def attempt_swap_before_leaf_create(parent_handle, name, *args, **kwargs):
        nonlocal attempted, blocked
        if str(name) == outside_leaf and not attempted:
            attempted = True
            candidate = retained_paths[_handle_key(parent_handle)]
            assert is_boundary(candidate)
            try:
                _replace_directory_with_test_link(candidate, outside)
            except OSError:
                blocked = True
        assert real_create is not None
        return real_create(parent_handle, name, *args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_directory_handle",
        retain_directory_path,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_file_handle",
        attempt_swap_before_leaf_create,
        raising=False,
    )

    staged = _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert attempted is True
    assert blocked is True
    assert staged.stage_root.is_dir()
    assert sentinel.read_bytes() == b"outside sentinel"
    assert not (outside / outside_leaf).exists()


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
    retained_paths: dict[int, Path] = {}
    real_create_directory = getattr(
        build_windows_installer, "_nt_create_directory_handle", None
    )

    def retain_directory_path(parent_handle, name, *args, **kwargs):
        assert real_create_directory is not None
        handle = real_create_directory(parent_handle, name, *args, **kwargs)
        parent_path = retained_paths.get(_handle_key(parent_handle))
        if str(name).startswith(build_windows_installer._STAGE_TEMP_PREFIX):
            candidate = stage_parent / str(name)
        elif parent_path is not None:
            candidate = parent_path / str(name)
        else:
            candidate = None
        if candidate is not None:
            retained_paths[_handle_key(handle)] = candidate
        return handle

    def collide(parent_handle, name, *args, **kwargs):
        nonlocal collision_identity, collision_path
        calls.append(name)
        if name == "payload.bin":
            collision_path = retained_paths[_handle_key(parent_handle)] / name
            assert real_create is not None
            collision_handle = real_create(parent_handle, name, *args, **kwargs)
            try:
                build_windows_installer._write_windows_file_handle(
                    collision_handle, b"foreign leaf collision"
                )
                build_windows_installer._flush_windows_file_handle(collision_handle)
                collision_identity = build_windows_installer._windows_handle_metadata(
                    collision_handle
                )
            finally:
                build_windows_installer._close_windows_handle(collision_handle)
            raise FileExistsError("synthetic foreign leaf collision")
        assert real_create is not None
        return real_create(parent_handle, name, *args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_directory_handle",
        retain_directory_path,
        raising=False,
    )
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
    check_handle = build_windows_installer._open_windows_no_follow(
        collision_path,
        build_windows_installer._FILE_READ_ATTRIBUTES
        | build_windows_installer._SYNCHRONIZE,
        share_delete=False,
    )
    try:
        assert build_windows_installer._windows_handle_metadata(check_handle) == (
            collision_identity
        )
    finally:
        build_windows_installer._close_windows_handle(check_handle)


@pytest.mark.skipif(os.name != "nt", reason="Windows native stage capabilities")
@pytest.mark.parametrize(
    ("seam", "expected"),
    [
        ("_open_windows_anchor_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_query_windows_filesystem", "INSTALLER_STAGE_IO_ERROR"),
        ("_nt_open_directory_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_nt_create_directory_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_ntstatus_to_winerror", "INSTALLER_STAGE_IO_ERROR"),
        ("_query_windows_file_id", "INSTALLER_STAGE_IO_ERROR"),
        ("_query_windows_attribute_tag", "INSTALLER_STAGE_IO_ERROR"),
        ("_query_windows_opened_name", "INSTALLER_STAGE_IO_ERROR"),
        ("_windows_handle_metadata", "INSTALLER_STAGE_IO_ERROR"),
        ("_nt_create_file_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_write_windows_file_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_flush_windows_file_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_revalidate_windows_handle_chain", "INSTALLER_STAGE_IO_ERROR"),
        ("_close_windows_handle", "INSTALLER_STAGE_IO_ERROR"),
        ("_publish_windows_directory_handle", "INSTALLER_STAGE_PUBLISH_ERROR"),
    ],
)
def test_missing_native_stage_capability_fails_closed_without_path_fallback(
    monkeypatch, tmp_path, seam, expected
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"

    monkeypatch.setattr(
        build_windows_installer, seam, None, raising=False
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert getattr(build_windows_installer, seam) is None
    assert _assert_pathless(caught.value, tmp_path) == expected
    assert source.is_file()
    assert not stage_parent.exists() or not any(
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
        assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_CONFLICT"
        assert next(target_parent.glob("*/winner.bin")).read_bytes() == conflict_marker
    assert injected is True


@pytest.mark.parametrize("failure", ["second-read-oserror", "second-read-mismatch"])
def test_final_attestation_temp_readback_failure_is_atomic_io_error(
    monkeypatch, tmp_path, failure
):
    staged = _stage(monkeypatch, tmp_path)
    capability, _calls = _compile_capability(
        monkeypatch, tmp_path / "build", staged, payload=b"setup"
    )
    output = tmp_path / "attestation.json"
    seeded = b"seeded attestation bytes"
    output.write_bytes(seeded)
    real_read = build_windows_installer._read_windows_file_handle
    real_rename = build_windows_installer._rename_windows_handle
    temp_captures = 0
    final_replace_calls = 0

    def fail_second_temp_capture(handle):
        nonlocal temp_captures
        if build_windows_installer._query_windows_opened_name(handle).startswith(
            build_windows_installer._ATTESTATION_TEMP_PREFIX
        ):
            temp_captures += 1
            if temp_captures == 2:
                if failure == "second-read-oserror":
                    raise OSError("C:/private/final attestation readback")
                return real_read(handle) + b"altered"
        return real_read(handle)

    def record_final_replace(handle, parent_handle, destination_path):
        nonlocal final_replace_calls
        if Path(destination_path) == output.absolute():
            final_replace_calls += 1
        return real_rename(handle, parent_handle, destination_path)

    monkeypatch.setattr(
        build_windows_installer, "_read_windows_file_handle", fail_second_temp_capture
    )
    monkeypatch.setattr(build_windows_installer, "_rename_windows_handle", record_final_replace)

    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.write_attestation(
            output,
            compiled_installer=capability,
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


def _expected_handle_identity(
    *,
    volume_serial: int = 17,
    file_id: bytes = b"\x00" * 16,
    file_type: int = stat.S_IFDIR,
    file_attributes: int = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10),
    reparse_tag: int = 0,
):
    identity_type = getattr(build_windows_installer, "_WindowsHandleIdentity", None)
    assert identity_type is not None, "full 128-bit Windows identity type is required"
    return identity_type(
        volume_serial=volume_serial,
        file_id=file_id,
        file_type=file_type,
        file_attributes=file_attributes,
        reparse_tag=reparse_tag,
    )


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            {"file_id": b"A" * 8 + b"B" * 8},
            {"file_id": b"C" * 8 + b"B" * 8},
        ),
        (
            {"volume_serial": 1, "file_id": b"D" * 16},
            {"volume_serial": 2, "file_id": b"D" * 16},
        ),
        (
            {"file_type": stat.S_IFDIR},
            {"file_type": stat.S_IFREG},
        ),
        (
            {"file_attributes": getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)},
            {
                "file_attributes": (
                    getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
                    | build_windows_installer._FILE_ATTRIBUTE_REPARSE_POINT
                ),
                "reparse_tag": 0xA0000003,
            },
        ),
    ],
)
def test_windows_handle_identity_never_truncates_or_ignores_type_or_reparse(
    left, right
):
    defaults = {
        "volume_serial": 17,
        "file_id": b"Z" * 16,
        "file_type": stat.S_IFDIR,
        "file_attributes": getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10),
        "reparse_tag": 0,
    }
    left_identity = _expected_handle_identity(**(defaults | left))
    right_identity = _expected_handle_identity(**(defaults | right))

    assert len(left_identity.file_id) == 16
    assert len(right_identity.file_id) == 16
    assert left_identity != right_identity


def test_cross_volume_parent_child_identity_is_rejected_before_stage_mutation():
    verifier = getattr(
        build_windows_installer, "_assert_same_windows_volume", None
    )
    assert verifier is not None, "cross-volume identity verifier is required"
    parent = _expected_handle_identity(volume_serial=11, file_id=b"P" * 16)
    child = _expected_handle_identity(volume_serial=12, file_id=b"C" * 16)

    with pytest.raises(build_windows_installer._UnsafePath):
        verifier(parent, child)


@pytest.mark.parametrize(
    "anchor",
    [
        r"\\server\share\stage",
        r"\\?\UNC\server\share\stage",
        r"\\.\C:\stage",
        r"\??\C:\stage",
        r"relative\stage",
    ],
)
def test_untrusted_or_nonlocal_anchor_forms_are_rejected_without_opening(anchor):
    parser = getattr(build_windows_installer, "_trusted_local_drive_parts", None)
    assert parser is not None, "trusted local drive anchor parser is required"

    with pytest.raises(build_windows_installer._UnsafePath):
        parser(anchor)


@pytest.mark.skipif(os.name != "nt", reason="Windows native stage capabilities")
def test_unknown_filesystem_fails_closed_before_owned_root_creation(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    root_creations = 0

    def unknown_filesystem(_handle):
        return "FAT32"

    def record_root_creation(*_args, **_kwargs):
        nonlocal root_creations
        root_creations += 1
        raise AssertionError("owned root creation must not follow unknown filesystem")

    monkeypatch.setattr(
        build_windows_installer,
        "_query_windows_filesystem",
        unknown_filesystem,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_nt_create_directory_handle",
        record_root_creation,
        raising=False,
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_IO_ERROR"
    assert root_creations == 0
    assert source.is_file()
    assert not stage_parent.exists() or not any(
        child.name.startswith(build_windows_installer._STAGE_TEMP_PREFIX)
        for child in stage_parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows retained ancestor behavior")
def test_changed_retained_ancestor_fails_closed_before_publication(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    calls = 0

    def changed_chain(_chain):
        nonlocal calls
        calls += 1
        raise build_windows_installer._ChangedPath

    monkeypatch.setattr(
        build_windows_installer,
        "_revalidate_windows_handle_chain",
        changed_chain,
        raising=False,
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert calls >= 1
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_UNSAFE"
    assert source.is_file()
    assert not stage_parent.exists() or not any(
        re.fullmatch(r"[0-9a-f]{64}", child.name)
        for child in stage_parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows exact opened-name behavior")
@pytest.mark.parametrize("alias", ["STAGES", unicodedata.normalize("NFD", "stages\N{COMBINING ACUTE ACCENT}")])
def test_case_or_nfc_alias_from_relative_open_is_rejected_before_mutation(
    monkeypatch, tmp_path, alias
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "stages"
    real_query = getattr(build_windows_installer, "_query_windows_opened_name", None)
    assert real_query is not None, "exact opened-name query is required"
    calls = 0

    def aliased_name(handle):
        nonlocal calls
        calls += 1
        actual = real_query(handle)
        if actual.casefold() == "stages":
            return alias
        return actual

    monkeypatch.setattr(
        build_windows_installer, "_query_windows_opened_name", aliased_name
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert calls >= 1
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_UNSAFE"
    assert source.is_file()
    assert not any(
        re.fullmatch(r"[0-9a-f]{64}", child.name)
        for child in stage_parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound publication")
def test_rename_collision_exact_winner_cleanup_failure_is_stage_io_error(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    baseline = _stage(monkeypatch, tmp_path / "baseline", source)
    target_parent = tmp_path / "winner-parent"
    publish_calls = 0
    cleanup_calls = 0

    def collide_with_exact_winner(_handle, _parent_handle, destination, *_args, **_kwargs):
        nonlocal publish_calls
        publish_calls += 1
        shutil.copytree(baseline.stage_root, Path(destination))
        raise FileExistsError("synthetic exact winner")

    def fail_owned_quarantine(*_args, **_kwargs):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return False

    monkeypatch.setattr(
        build_windows_installer,
        "_publish_windows_directory_handle",
        collide_with_exact_winner,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_quarantine_windows_handle",
        fail_owned_quarantine,
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(
            monkeypatch,
            tmp_path / "winner-work",
            source,
            stage_parent=target_parent,
        )

    assert publish_calls == 1
    assert cleanup_calls >= 1
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_IO_ERROR"
    assert baseline.stage_root.is_dir()
    assert any(
        child.name.startswith(build_windows_installer._STAGE_TEMP_PREFIX)
        for child in target_parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound publication")
@pytest.mark.parametrize("cleanup_succeeds", [True, False])
def test_noncollision_handle_rename_failure_is_publish_error_even_if_cleanup_fails(
    monkeypatch, tmp_path, cleanup_succeeds
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / ("publish-failure-" + str(cleanup_succeeds))
    real_quarantine = getattr(
        build_windows_installer, "_quarantine_windows_handle", None
    )
    publish_calls = 0
    cleanup_calls = 0

    def fail_publish(*_args, **_kwargs):
        nonlocal publish_calls
        publish_calls += 1
        raise OSError("C:/private/noncollision rename failure")

    def cleanup(handle, parent_handle, destination):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if not cleanup_succeeds:
            return False
        assert real_quarantine is not None
        return real_quarantine(handle, parent_handle, destination)

    monkeypatch.setattr(
        build_windows_installer,
        "_publish_windows_directory_handle",
        fail_publish,
        raising=False,
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_quarantine_windows_handle",
        cleanup,
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path, source, stage_parent=stage_parent)

    assert publish_calls == 1
    assert cleanup_calls >= 1
    assert _assert_pathless(caught.value, tmp_path) == (
        "INSTALLER_STAGE_PUBLISH_ERROR"
    )
    assert source.is_file()
    assert not any(
        re.fullmatch(r"[0-9a-f]{64}", child.name)
        for child in stage_parent.iterdir()
    )


def _handle_key(handle) -> int:
    value = getattr(handle, "value", handle)
    return int(value)


def _install_stage_lifecycle_recorder(monkeypatch):
    required = (
        "_open_windows_anchor_handle",
        "_open_windows_anchor_read_handle",
        "_nt_open_directory_handle",
        "_nt_open_directory_read_handle",
        "_nt_open_directory_monitor_handle",
        "_nt_open_file_read_handle",
        "_nt_create_directory_handle",
        "_nt_create_file_handle",
        "_validate_windows_stage_handles",
        "_close_windows_handle",
        "_quarantine_windows_handle",
        "_publish_windows_directory_handle",
        "_create_windows_stage_monitor_event",
    )
    missing = [
        name for name in required if not callable(getattr(build_windows_installer, name, None))
    ]
    assert missing == [], "missing lifecycle seams: " + ", ".join(missing)

    events: list[tuple[str, str, int | None]] = []
    active: dict[int, dict[str, object]] = {}
    opened: dict[int, int] = {}
    closed: dict[int, int] = {}

    def register(event, role, handle, parent=None):
        key = _handle_key(handle)
        parent_key = None if parent is None else _handle_key(parent)
        events.append((event, role, key))
        active[key] = {"role": role, "parent": parent_key}
        opened[key] = opened.get(key, 0) + 1
        return handle

    real_anchor = build_windows_installer._open_windows_anchor_handle
    real_read_anchor = build_windows_installer._open_windows_anchor_read_handle
    real_open_dir = build_windows_installer._nt_open_directory_handle
    real_open_read_dir = build_windows_installer._nt_open_directory_read_handle
    real_open_monitor = build_windows_installer._nt_open_directory_monitor_handle
    real_open_read_file = build_windows_installer._nt_open_file_read_handle
    real_create_dir = build_windows_installer._nt_create_directory_handle
    real_create_file = build_windows_installer._nt_create_file_handle
    real_validate = build_windows_installer._validate_windows_stage_handles
    real_close = build_windows_installer._close_windows_handle
    real_quarantine = build_windows_installer._quarantine_windows_handle
    real_publish = build_windows_installer._publish_windows_directory_handle
    real_create_monitor_event = build_windows_installer._create_windows_stage_monitor_event

    def open_anchor(*args, **kwargs):
        return register("open", "anchor", real_anchor(*args, **kwargs))

    def open_read_anchor(*args, **kwargs):
        return register(
            "open", "lease-anchor", real_read_anchor(*args, **kwargs)
        )

    def open_dir(parent_handle, name, *args, **kwargs):
        return register(
            "open",
            "existing-dir:" + str(name),
            real_open_dir(parent_handle, name, *args, **kwargs),
            parent_handle,
        )

    def open_read_dir(parent_handle, name, *args, **kwargs):
        return register(
            "open",
            "lease-dir:" + str(name),
            real_open_read_dir(parent_handle, name, *args, **kwargs),
            parent_handle,
        )

    def open_monitor(parent_handle, name, *args, **kwargs):
        return register(
            "open",
            "stage-monitor",
            real_open_monitor(parent_handle, name, *args, **kwargs),
            parent_handle,
        )

    def create_monitor_event(*args, **kwargs):
        return register(
            "open",
            "stage-monitor-event",
            real_create_monitor_event(*args, **kwargs),
        )

    def open_read_file(parent_handle, name, *args, **kwargs):
        return register(
            "open",
            "lease-file:" + str(name),
            real_open_read_file(parent_handle, name, *args, **kwargs),
            parent_handle,
        )

    def create_dir(parent_handle, name, *args, **kwargs):
        if str(name).startswith(build_windows_installer._STAGE_TEMP_PREFIX):
            role = "stage-root"
        elif kwargs.get("parent_component"):
            role = "parent-created:" + str(name)
        else:
            role = "owned-dir:" + str(name)
        return register(
            "create", role, real_create_dir(parent_handle, name, *args, **kwargs), parent_handle
        )

    def create_file(parent_handle, name, *args, **kwargs):
        return register(
            "create",
            "owned-file:" + str(name),
            real_create_file(parent_handle, name, *args, **kwargs),
            parent_handle,
        )

    def validate(*args, **kwargs):
        events.append(("validate", "retained-stage", None))
        assert any(info["role"] == "stage-root" for info in active.values())
        assert any(str(info["role"]).startswith("owned-dir:") for info in active.values())
        assert any(str(info["role"]).startswith("owned-file:") for info in active.values())
        return real_validate(*args, **kwargs)

    def close(handle):
        key = _handle_key(handle)
        role = str(active.get(key, {}).get("role", "unknown"))
        events.append(("close", role, key))
        closed[key] = closed.get(key, 0) + 1
        try:
            return real_close(handle)
        finally:
            active.pop(key, None)

    def quarantine(handle, parent_handle, destination):
        key = _handle_key(handle)
        parent_key = _handle_key(parent_handle)
        events.append(("quarantine", str(Path(destination).name), key))
        assert active[key]["role"] == "stage-root"
        assert parent_key in active
        assert not any(
            info["role"] != "stage-root"
            and (
                str(info["role"]).startswith("owned-dir:")
                or str(info["role"]).startswith("owned-file:")
            )
            for info in active.values()
        )
        return real_quarantine(handle, parent_handle, destination)

    def publish(handle, parent_handle, destination, *args, **kwargs):
        key = _handle_key(handle)
        parent_key = _handle_key(parent_handle)
        events.append(("rename", str(Path(destination).name), key))
        assert active[key]["role"] == "stage-root"
        assert parent_key in active
        assert not any(
            str(info["role"]).startswith("owned-dir:")
            or str(info["role"]).startswith("owned-file:")
            for info in active.values()
        )
        return real_publish(handle, parent_handle, destination, *args, **kwargs)

    monkeypatch.setattr(build_windows_installer, "_open_windows_anchor_handle", open_anchor)
    monkeypatch.setattr(
        build_windows_installer, "_open_windows_anchor_read_handle", open_read_anchor
    )
    monkeypatch.setattr(build_windows_installer, "_nt_open_directory_handle", open_dir)
    monkeypatch.setattr(
        build_windows_installer, "_nt_open_directory_read_handle", open_read_dir
    )
    monkeypatch.setattr(
        build_windows_installer, "_nt_open_directory_monitor_handle", open_monitor
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_create_windows_stage_monitor_event",
        create_monitor_event,
    )
    monkeypatch.setattr(
        build_windows_installer, "_nt_open_file_read_handle", open_read_file
    )
    monkeypatch.setattr(build_windows_installer, "_nt_create_directory_handle", create_dir)
    monkeypatch.setattr(build_windows_installer, "_nt_create_file_handle", create_file)
    monkeypatch.setattr(build_windows_installer, "_validate_windows_stage_handles", validate)
    monkeypatch.setattr(build_windows_installer, "_close_windows_handle", close)
    monkeypatch.setattr(build_windows_installer, "_quarantine_windows_handle", quarantine)
    monkeypatch.setattr(build_windows_installer, "_publish_windows_directory_handle", publish)
    return events, active, opened, closed


@pytest.mark.skipif(os.name != "nt", reason="Windows retained handle lifecycle")
@pytest.mark.parametrize(
    ("outcome", "expected_token", "terminal_event"),
    [
        ("success", None, "rename"),
        ("prepublication", "INSTALLER_STAGE_TREE_INVALID", "quarantine"),
        ("exact-winner", None, "quarantine"),
        ("conflict-winner", "INSTALLER_STAGE_CONFLICT", "quarantine"),
        ("publish-failure", "INSTALLER_STAGE_PUBLISH_ERROR", "quarantine"),
    ],
)
def test_stage_handle_lifecycle_is_leaf_to_root_exactly_once_and_leak_free(
    monkeypatch, tmp_path, outcome, expected_token, terminal_event
):
    source, _document, _members = _valid_zip(tmp_path)
    baseline = None
    if outcome in {"exact-winner", "conflict-winner"}:
        baseline = _stage(monkeypatch, tmp_path / "baseline", source)

    events, active, opened, closed = _install_stage_lifecycle_recorder(monkeypatch)
    target_parent = tmp_path / ("lifecycle-" + outcome)

    if outcome == "prepublication":
        real_validate = build_windows_installer._validate_windows_stage_handles

        def fail_after_retained_validation(*args, **kwargs):
            real_validate(*args, **kwargs)
            raise build_windows_installer._ChangedPath

        monkeypatch.setattr(
            build_windows_installer,
            "_validate_windows_stage_handles",
            fail_after_retained_validation,
        )
    elif outcome in {"exact-winner", "conflict-winner"}:
        real_publish = build_windows_installer._publish_windows_directory_handle
        injected = False

        def concurrent_winner(handle, parent_handle, destination, *args, **kwargs):
            nonlocal injected
            if not injected:
                injected = True
                destination = Path(destination)
                if outcome == "exact-winner":
                    assert baseline is not None
                    shutil.copytree(baseline.stage_root, destination)
                else:
                    destination.mkdir()
                    (destination / "foreign.bin").write_bytes(b"foreign")
                raise FileExistsError("synthetic rename collision")
            return real_publish(handle, parent_handle, destination, *args, **kwargs)

        monkeypatch.setattr(
            build_windows_installer,
            "_publish_windows_directory_handle",
            concurrent_winner,
        )
    elif outcome == "publish-failure":
        monkeypatch.setattr(
            build_windows_installer,
            "_publish_windows_directory_handle",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("C:/private/noncollision publication failure")
            ),
        )

    if expected_token is None:
        staged = _stage(
            monkeypatch, tmp_path / "work", source, stage_parent=target_parent
        )
        assert staged.stage_root.is_dir()
    else:
        with pytest.raises(RuntimeError) as caught:
            _stage(monkeypatch, tmp_path / "work", source, stage_parent=target_parent)
        assert _assert_pathless(caught.value, tmp_path) == expected_token

    assert active == {}
    assert opened
    assert opened == closed
    validate_index = next(
        index for index, event in enumerate(events) if event[0] == "validate"
    )
    terminal_index = max(
        index for index, event in enumerate(events) if event[0] == terminal_event
    )
    descendant_closes = [
        index
        for index, event in enumerate(events)
        if event[0] == "close"
        and (
            event[1].startswith("owned-file:")
            or event[1].startswith("owned-dir:")
        )
    ]
    assert descendant_closes
    assert validate_index < min(descendant_closes)
    assert max(descendant_closes) < terminal_index

    if outcome == "success":
        post_publication = events[terminal_index + 1 :]
        assert any(
            event[0] == "open" and event[1].startswith("lease-")
            for event in post_publication
        )
        assert all(event[0] in {"open", "close"} for event in post_publication)
        assert all(
            event[1].startswith("lease-")
            for event in post_publication
            if event[0] == "open"
        )


# Task 5C successor RED boundary. Each test below names a concrete authority or
# lifecycle break from the independently approved successor addendum.


@pytest.mark.skipif(os.name != "nt", reason="Windows adopted-stage authority")
def test_published_stage_is_adopted_and_rejects_postrename_byte_mutation(
    monkeypatch, tmp_path
):
    source, _document, _members = _valid_zip(tmp_path)
    stage_parent = tmp_path / "adopted-stages"
    real_publish = build_windows_installer._publish_windows_directory_handle
    injected = False

    def mutate_after_handle_publish(handle, parent_handle, destination, *args, **kwargs):
        nonlocal injected
        result = real_publish(handle, parent_handle, destination, *args, **kwargs)
        module = Path(destination) / "PDFVectorImporter" / "module.py"
        module.write_bytes(b"VALUE = 'post-rename mutation'\n")
        injected = True
        return result

    monkeypatch.setattr(
        build_windows_installer,
        "_publish_windows_directory_handle",
        mutate_after_handle_publish,
    )

    with pytest.raises(RuntimeError) as caught:
        _stage(monkeypatch, tmp_path / "work", source, stage_parent=stage_parent)

    assert injected is True
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_STAGE_TREE_INVALID"
    assert source.is_file()
    assert not any(
        re.fullmatch(r"[0-9a-f]{64}", child.name)
        for child in stage_parent.iterdir()
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows no-create stage lease")
def test_ordinary_stage_lease_is_no_create_and_blocks_live_mutation(
    monkeypatch, tmp_path
):
    acquire = getattr(build_windows_installer, "_acquire_windows_stage_read_lease", None)
    validate = getattr(build_windows_installer, "_validate_windows_stage_read_lease", None)
    release = getattr(build_windows_installer, "_release_windows_stage_read_lease", None)
    assert callable(acquire), "ordinary no-create stage lease acquisition is required"
    assert callable(validate), "ordinary stage lease validation is required"
    assert callable(release), "ordinary stage lease release is required"
    staged = _stage(monkeypatch, tmp_path / "stage")
    lease = acquire(staged)
    module = staged.source_dir / "module.py"
    try:
        with pytest.raises(OSError):
            module.write_bytes(b"tampered while leased")
        assert validate(lease)["package_version"] == PACKAGE_VERSION
    finally:
        release(lease)


@pytest.mark.skipif(os.name != "nt", reason="Windows no-create stage lease")
def test_ordinary_stage_lease_never_creates_a_missing_component(
    monkeypatch, tmp_path
):
    acquire = getattr(build_windows_installer, "_acquire_windows_stage_read_lease", None)
    assert callable(acquire), "ordinary no-create stage lease acquisition is required"
    staged = _stage(monkeypatch, tmp_path / "stage")
    missing_root = staged.stage_root / "missing-root"
    missing = dataclasses.replace(
        staged,
        stage_root=missing_root,
        source_dir=missing_root / "PDFVectorImporter",
        source_zip_snapshot=(
            missing_root / build_windows_installer._STAGE_METADATA_DIRNAME / ZIP_NAME
        ),
    )
    creates = 0
    real_create = build_windows_installer._nt_create_directory_handle

    def record_create(*args, **kwargs):
        nonlocal creates
        creates += 1
        return real_create(*args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer, "_nt_create_directory_handle", record_create
    )

    with pytest.raises((build_windows_installer._UnsafePath, build_windows_installer._ChangedPath)):
        acquire(missing)
    assert creates == 0
    assert not missing_root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows least-privilege stage lease")
def test_ordinary_stage_lease_opens_existing_directories_without_create_or_delete_rights(
    monkeypatch, tmp_path
):
    acquire = getattr(build_windows_installer, "_acquire_windows_stage_read_lease", None)
    release = getattr(build_windows_installer, "_release_windows_stage_read_lease", None)
    assert callable(acquire) and callable(release)
    staged = _stage(monkeypatch, tmp_path / "stage")
    records: list[dict[str, int]] = []
    real_relative = build_windows_installer._nt_relative_create

    def record_relative(parent_handle, name, **kwargs):
        records.append({"name": name, **kwargs})
        return real_relative(parent_handle, name, **kwargs)

    monkeypatch.setattr(build_windows_installer, "_nt_relative_create", record_relative)
    lease = acquire(staged)
    release(lease)

    directory_records = [
        item
        for item in records
        if item["create_options"] & build_windows_installer._FILE_DIRECTORY_FILE
    ]
    assert directory_records
    protected_names = {
        staged.stage_root.name,
        build_windows_installer._STAGE_METADATA_DIRNAME,
        "PDFVectorImporter",
        "data",
    }
    for item in directory_records:
        assert item["disposition"] == build_windows_installer._FILE_OPEN
        expected_share = build_windows_installer._FILE_SHARE_READ
        if item["name"] not in protected_names:
            expected_share |= build_windows_installer._FILE_SHARE_WRITE
        assert item["share_access"] == expected_share
        assert item["desired_access"] & (
            build_windows_installer._FILE_ADD_FILE
            | build_windows_installer._FILE_ADD_SUBDIRECTORY
            | build_windows_installer._DELETE
        ) == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows sealed stage namespace")
def test_compiler_cannot_observe_a_transient_stage_namespace_member(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "output"
    transient = staged.source_dir / "synthetic-extra.py"
    attempts: list[str] = []
    clean_payload = b"compiled only from retained manifest members"
    tainted_payload = b"compiled after observing transient member"

    def run(command, **_kwargs):
        command = list(command)
        output_root = Path(next(item[2:] for item in command if item.startswith("/O")))
        basename = next(item[2:] for item in command if item.startswith("/F"))
        setup = output_root / (basename + ".exe")
        observed = False
        try:
            transient.write_bytes(b"foreign transient input")
            attempts.append("created")
            observed = transient.read_bytes() == b"foreign transient input"
            attempts.append("read")
            transient.unlink()
            attempts.append("removed")
        except OSError:
            attempts.append("blocked")
        handle = build_windows_installer._open_windows_no_follow(
            setup,
            build_windows_installer._FILE_READ_DATA
            | build_windows_installer._FILE_WRITE_DATA
            | build_windows_installer._FILE_READ_ATTRIBUTES
            | build_windows_installer._FILE_WRITE_ATTRIBUTES
            | build_windows_installer._SYNCHRONIZE,
            share_delete=True,
        )
        try:
            build_windows_installer._write_windows_file_handle(
                handle, tainted_payload if observed else clean_payload
            )
            build_windows_installer._flush_windows_file_handle(handle)
        finally:
            build_windows_installer._close_windows_handle(handle)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_windows_installer.subprocess, "run", run)
    with pytest.raises(
        RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"
    ):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=output_dir,
        )

    assert attempts == ["created", "read", "removed"]
    assert not list(output_dir.glob("FreeCAD-PDF-Importer-Setup_*.exe"))
    assert not transient.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sealed stage namespace")
def test_postcompiler_transient_stage_member_invalidates_attestation_capability(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "output"
    destination = tmp_path / "attestation" / "installer-attestation.json"
    transient = staged.source_dir / "synthetic-after-compile.py"
    compiler_calls: list[list[str]] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        _synthetic_compiler_runner(b"synthetic setup", compiler_calls),
    )

    capability = build_windows_installer.compile_installer(
        tmp_path / "ISCC.exe",
        staged,
        toolchain_identity=_valid_toolchain_identity(),
        output_dir=output_dir,
    )
    assert len(compiler_calls) == 1
    transient.write_bytes(b"foreign transient input")
    assert transient.read_bytes() == b"foreign transient input"
    transient.unlink()

    with pytest.raises(
        RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"
    ):
        build_windows_installer.write_attestation(
            destination,
            compiled_installer=capability,
        )

    assert not destination.exists()
    assert not transient.exists()
    with pytest.raises(
        RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"
    ):
        build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows stage monitor lifecycle")
@pytest.mark.parametrize("failure", ["close", "unprovable-cancel"])
@pytest.mark.parametrize(
    ("finalizer", "expected_token"),
    [
        ("plain", "INSTALLER_COMPILER_INPUT_INVALID"),
        ("attestation", "INSTALLER_ATTESTATION_INPUT_INVALID"),
    ],
)
def test_stage_monitor_shutdown_failure_cannot_finalize_a_capability(
    monkeypatch, tmp_path, failure, finalizer, expected_token
):
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "build")
    destination = tmp_path / "attestation" / "installer-attestation.json"
    injections: list[str] = []

    if failure == "close":
        real_close = build_windows_installer._close_windows_entry

        def fail_monitor_close(entry):
            result = real_close(entry)
            if entry.role == "stage-monitor":
                injections.append("close")
                raise OSError("synthetic monitor close failure")
            return result

        monkeypatch.setattr(
            build_windows_installer, "_close_windows_entry", fail_monitor_close
        )
    else:
        real_wait = build_windows_installer._wait_windows_stage_monitor

        def hide_cancel_completion(monitor, timeout):
            if timeout:
                injections.append("unprovable-cancel")
                return build_windows_installer._WAIT_TIMEOUT
            return real_wait(monitor, timeout)

        monkeypatch.setattr(
            build_windows_installer,
            "_wait_windows_stage_monitor",
            hide_cancel_completion,
        )

    with pytest.raises(RuntimeError, match=rf"^{expected_token}$"):
        if finalizer == "attestation":
            build_windows_installer.write_attestation(
                destination,
                compiled_installer=capability,
            )
        else:
            build_windows_installer.finalize_compiled_installer(capability)

    assert injections == [failure]
    assert not destination.exists()
    with pytest.raises(
        RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"
    ):
        build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows least-privilege parent construction")
@pytest.mark.parametrize(
    "preparer_name",
    [
        "_prepare_windows_stage_parent",
        "_prepare_windows_output_parent",
        "_prepare_windows_attestation_parent",
    ],
)
def test_parent_construction_retains_only_minimal_handles_and_closes_creator_views(
    monkeypatch, tmp_path, preparer_name
):
    preparer = getattr(build_windows_installer, preparer_name)
    target = tmp_path / preparer_name.removeprefix("_prepare_windows_") / "leaf"
    calls: list[dict[str, object]] = []
    events: list[tuple[str, str, int | None]] = []
    absence_observations: dict[str, int] = {}
    real_relative = build_windows_installer._nt_relative_create
    real_exists = build_windows_installer._exact_windows_child_exists
    real_broad_anchor = build_windows_installer._open_windows_anchor_handle
    real_minimal_anchor = build_windows_installer._open_windows_anchor_read_handle
    broad_anchor_calls: list[str] = []
    minimal_anchor_calls: list[str] = []

    def record_relative(parent_handle, name, **kwargs):
        events.append(("relative", str(name), kwargs["disposition"]))
        handle = real_relative(parent_handle, name, **kwargs)
        calls.append(
            {
                "name": str(name),
                "identity": build_windows_installer._windows_handle_metadata(handle),
                **kwargs,
            }
        )
        return handle

    def record_exists(parent_handle, name, *, allow_absent):
        result = real_exists(parent_handle, name, allow_absent=allow_absent)
        if allow_absent and not result:
            key = str(name)
            absence_observations[key] = absence_observations.get(key, 0) + 1
            events.append(("absent", key, None))
        return result

    def record_broad_anchor(anchor):
        broad_anchor_calls.append(str(anchor))
        return real_broad_anchor(anchor)

    def record_minimal_anchor(anchor):
        minimal_anchor_calls.append(str(anchor))
        return real_minimal_anchor(anchor)

    monkeypatch.setattr(build_windows_installer, "_nt_relative_create", record_relative)
    monkeypatch.setattr(build_windows_installer, "_exact_windows_child_exists", record_exists)
    monkeypatch.setattr(
        build_windows_installer, "_open_windows_anchor_handle", record_broad_anchor
    )
    monkeypatch.setattr(
        build_windows_installer,
        "_open_windows_anchor_read_handle",
        record_minimal_anchor,
    )
    resolved, chain = preparer(target)
    try:
        assert resolved == target.absolute()
        assert chain
        assert broad_anchor_calls == []
        assert len(minimal_anchor_calls) == 1
        created = [item for item in calls if item["disposition"] == build_windows_installer._FILE_CREATE]
        assert created
        for created_call in created:
            name = created_call["name"]
            assert absence_observations.get(name, 0) >= 2
            retained_views = [
                item
                for item in calls
                if item["name"] == name
                and item["disposition"] == build_windows_installer._FILE_OPEN
                and item["desired_access"]
                & (
                    build_windows_installer._FILE_ADD_FILE
                    | build_windows_installer._FILE_ADD_SUBDIRECTORY
                    | build_windows_installer._DELETE
                )
                == 0
            ]
            assert len(retained_views) == 1
            assert retained_views[0]["identity"] == created_call["identity"]
            assert retained_views[0]["share_access"] == (
                build_windows_installer._FILE_SHARE_READ
                | build_windows_installer._FILE_SHARE_WRITE
            )
            create_index = events.index(
                ("relative", name, build_windows_installer._FILE_CREATE)
            )
            absent_indexes = [
                index
                for index, event in enumerate(events)
                if event == ("absent", name, None)
            ]
            assert len(absent_indexes) >= 2
            assert absent_indexes[0] < absent_indexes[1] < create_index
        retained_identities = {entry.identity for entry in chain}
        assert len(retained_identities) == len(chain)
        creator_views = [
            item
            for item in calls
            if item["disposition"] == build_windows_installer._FILE_OPEN
            and item["desired_access"]
            & build_windows_installer._FILE_ADD_SUBDIRECTORY
        ]
        assert len(creator_views) == len(created)
        for item in calls:
            if (
                item["disposition"] == build_windows_installer._FILE_OPEN
                and item not in creator_views
            ):
                assert item["desired_access"] & (
                    build_windows_installer._FILE_ADD_FILE
                    | build_windows_installer._FILE_ADD_SUBDIRECTORY
                    | build_windows_installer._DELETE
                ) == 0
    finally:
        build_windows_installer._close_windows_entries_nonraising(reversed(chain))


@pytest.mark.skipif(os.name != "nt", reason="Windows ordinary protected parent path")
@pytest.mark.parametrize(
    "preparer_name",
    [
        "_prepare_windows_stage_parent",
        "_prepare_windows_output_parent",
        "_prepare_windows_attestation_parent",
    ],
)
def test_parent_preparers_can_pin_existing_users_directory_without_write_authority(
    preparer_name,
):
    preparer = getattr(build_windows_installer, preparer_name)
    protected_parent = Path.home().parent
    resolved, chain = preparer(protected_parent)
    try:
        assert resolved == protected_parent.absolute()
        assert chain[-1].path == protected_parent.absolute()
        build_windows_installer._revalidate_windows_handle_chain(chain)
    finally:
        build_windows_installer._close_windows_entries_nonraising(reversed(chain))


@pytest.mark.skipif(os.name != "nt", reason="Windows supported filesystem gate")
def test_ordinary_stage_lease_rejects_refs_without_mutation(monkeypatch, tmp_path):
    acquire = getattr(build_windows_installer, "_acquire_windows_stage_read_lease", None)
    assert callable(acquire), "ordinary no-create stage lease acquisition is required"
    staged = _stage(monkeypatch, tmp_path / "stage")
    monkeypatch.setattr(
        build_windows_installer, "_query_windows_filesystem", lambda _handle: "REFS"
    )
    creates = 0

    def forbid_create(*_args, **_kwargs):
        nonlocal creates
        creates += 1
        raise AssertionError("ordinary lease must not create")

    monkeypatch.setattr(
        build_windows_installer, "_nt_create_directory_handle", forbid_create
    )
    with pytest.raises(build_windows_installer._NativeCapabilityError):
        acquire(staged)
    assert creates == 0
    assert staged.stage_root.is_dir()


def test_installer_stage_and_compiled_capabilities_are_exact_pathless_types(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    stage_repr = repr(staged)
    assert "InstallerStage" in stage_repr
    assert str(tmp_path) not in stage_repr
    assert staged.stage_identity_sha256 not in stage_repr
    compiled_type = getattr(build_windows_installer, "CompiledInstaller", None)
    output_type = getattr(build_windows_installer, "_WindowsOutputLease", None)
    stage_lease_type = getattr(build_windows_installer, "_WindowsStageReadLease", None)
    assert all(isinstance(item, type) for item in (compiled_type, output_type, stage_lease_type))


def test_canonical_compiled_output_identity_matches_fixed_vector():
    builder = getattr(
        build_windows_installer,
        "_canonical_compiled_installer_identity_bytes",
        None,
    )
    assert callable(builder), "canonical compiled-output identity builder is required"
    encoded = builder(
        setup_basename="FreeCAD-PDF-Importer-Setup_v1.2.3.exe",
        setup_sha256="e" * 64,
        setup_size=33,
        stage_identity_sha256="d" * 64,
        toolchain_identity={
            "name": "Inno Setup",
            "version": "6.7.1",
            "source_sha256": "a" * 64,
            "manifest_sha256": "b" * 64,
            "tree_sha256": "c" * 64,
        },
    )
    assert type(encoded) is bytes
    assert len(encoded) == 653
    assert hashlib.sha256(
        b"BCS-FREECAD-COMPILED-INSTALLER-IDENTITY\0v1\0" + encoded
    ).hexdigest() == "cb81e114f25e33a78a5f44d8b8cfbb8ea0e76fc1f09318cd7fec0b68cfe5e5eb"
    assert encoded.endswith(b"\n")


@pytest.mark.skipif(os.name != "nt", reason="Windows guarded compiler output")
def test_compile_returns_capability_and_finalizer_returns_bound_loose_path(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "namespace" / "output"
    capability, calls = _compile_capability(
        monkeypatch,
        tmp_path / "build",
        staged,
        output_dir=output_dir,
        payload=b"bound setup bytes",
    )
    compiled_type = getattr(build_windows_installer, "CompiledInstaller", None)
    assert compiled_type is not None and type(capability) is compiled_type
    assert str(tmp_path) not in repr(capability)
    expected = (
        output_dir / f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe"
    ).absolute()
    assert len(calls) == 1
    with pytest.raises(OSError):
        os.replace(output_dir, output_dir.with_name("renamed-output"))

    assert build_windows_installer.finalize_compiled_installer(capability) == expected
    assert expected.read_bytes() == b"bound setup bytes"
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)

    renamed = output_dir.with_name("renamed-output")
    os.replace(output_dir, renamed)
    os.replace(renamed, output_dir)


def _compiled_for_provenance(monkeypatch, tmp_path, provenance: str):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "namespace" / "output"
    if provenance == "winner":
        first, _calls = _compile_capability(
            monkeypatch,
            tmp_path / "first-build",
            staged,
            output_dir=output_dir,
            payload=b"same setup bytes",
        )
        build_windows_installer.finalize_compiled_installer(first)
    capability, calls = _compile_capability(
        monkeypatch,
        tmp_path / "second-build",
        staged,
        output_dir=output_dir,
        payload=b"same setup bytes",
    )
    setup = output_dir / f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe"
    return capability, calls, output_dir, setup


@pytest.mark.skipif(os.name != "nt", reason="Windows retained output namespace")
@pytest.mark.parametrize("provenance", ["owned", "winner"])
def test_both_output_provenances_retain_parent_and_ancestor_path_binding(
    monkeypatch, tmp_path, provenance
):
    capability, calls, output_dir, setup = _compiled_for_provenance(
        monkeypatch, tmp_path / provenance, provenance
    )
    assert len(calls) == 1
    ancestor = output_dir.parent
    with pytest.raises(OSError):
        os.replace(output_dir, output_dir.with_name("direct-parent-renamed"))
    with pytest.raises(OSError):
        os.replace(ancestor, ancestor.with_name("ancestor-renamed"))
    assert setup.resolve(strict=True) == setup.absolute()
    assert build_windows_installer.finalize_compiled_installer(capability) == setup.absolute()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained output namespace")
@pytest.mark.parametrize("provenance", ["owned", "winner"])
@pytest.mark.parametrize("changed_component", ["output", "namespace"])
def test_output_parent_or_ancestor_alias_is_setup_unsafe_for_both_provenances(
    monkeypatch, tmp_path, provenance, changed_component
):
    capability, _calls, output_dir, _setup = _compiled_for_provenance(
        monkeypatch, tmp_path / provenance / changed_component, provenance
    )
    real_query = build_windows_installer._query_windows_opened_name
    target = output_dir.name if changed_component == "output" else output_dir.parent.name
    target_handles = {
        _handle_key(entry.handle)
        for entry in capability.output_lease.parent_chain
        if entry.name == target
    }
    assert target_handles

    def aliased_name(handle):
        actual = real_query(handle)
        if _handle_key(handle) in target_handles:
            return actual.upper() if actual.upper() != actual else actual.lower()
        return actual

    monkeypatch.setattr(build_windows_installer, "_query_windows_opened_name", aliased_name)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_SETUP_UNSAFE$"):
        build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows loose-winner collision")
def test_loose_output_collision_reuses_exact_winner_and_preserves_conflict(
    monkeypatch, tmp_path
):
    capability, calls, output_dir, setup = _compiled_for_provenance(
        monkeypatch, tmp_path / "exact", "winner"
    )
    winner_bytes = setup.read_bytes()
    assert len(calls) == 1
    assert build_windows_installer.finalize_compiled_installer(capability) == setup.absolute()
    assert setup.read_bytes() == winner_bytes
    assert not list(output_dir.glob(".installer-output-*"))

    conflict_stage = _stage(monkeypatch, tmp_path / "conflict-stage")
    conflict_dir = tmp_path / "conflict-output"
    conflict_dir.mkdir()
    conflict = conflict_dir / f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe"
    conflict.write_bytes(b"foreign winner")
    calls = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        _synthetic_compiler_runner(b"candidate bytes", calls),
    )
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_FAILED$"):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            conflict_stage,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=conflict_dir,
        )
    assert calls and conflict.read_bytes() == b"foreign winner"


@pytest.mark.skipif(os.name != "nt", reason="Windows compiled finalization")
@pytest.mark.parametrize(
    ("failures", "expected"),
    [
        (("stage", "namespace", "bytes"), "INSTALLER_COMPILER_INPUT_INVALID"),
        (("namespace", "bytes"), "INSTALLER_SETUP_UNSAFE"),
        (("bytes",), "INSTALLER_SETUP_CHANGED"),
    ],
)
def test_finalize_compiled_installer_uses_exact_failure_precedence_and_consumes(
    monkeypatch, tmp_path, failures, expected
):
    required = (
        "_validate_windows_stage_read_lease",
        "_validate_windows_output_namespace",
        "_validate_windows_output_bytes",
    )
    assert all(callable(getattr(build_windows_installer, name, None)) for name in required)
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "build")

    if "stage" in failures:
        monkeypatch.setattr(
            build_windows_installer,
            "_validate_windows_stage_read_lease",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                build_windows_installer._ChangedPath()
            ),
        )
    if "namespace" in failures:
        monkeypatch.setattr(
            build_windows_installer,
            "_validate_windows_output_namespace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                build_windows_installer._UnsafePath()
            ),
        )
    if "bytes" in failures:
        monkeypatch.setattr(
            build_windows_installer,
            "_validate_windows_output_bytes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                build_windows_installer._ChangedPath()
            ),
        )

    with pytest.raises(RuntimeError, match=rf"^{expected}$"):
        build_windows_installer.finalize_compiled_installer(capability)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows attestation precedence")
@pytest.mark.parametrize(
    ("failures", "expected"),
    [
        (("stage", "namespace", "bytes", "binding"), "INSTALLER_ATTESTATION_INPUT_INVALID"),
        (("namespace", "bytes", "binding"), "INSTALLER_SETUP_UNSAFE"),
        (("bytes", "binding"), "INSTALLER_SETUP_CHANGED"),
        (("binding",), "INSTALLER_ATTESTATION_INPUT_INVALID"),
    ],
)
@pytest.mark.parametrize("provenance", ["owned", "winner"])
def test_attestation_simultaneous_failure_precedence_is_exact(
    monkeypatch, tmp_path, failures, expected, provenance
):
    required = (
        "_validate_windows_stage_read_lease",
        "_validate_windows_output_namespace",
        "_validate_windows_output_bytes",
        "_validate_compiled_installer_binding",
    )
    assert all(callable(getattr(build_windows_installer, name, None)) for name in required)
    capability, _calls, _output_dir, _setup = _compiled_for_provenance(
        monkeypatch, tmp_path / provenance, provenance
    )
    if "stage" in failures:
        monkeypatch.setattr(
            build_windows_installer,
            "_validate_windows_stage_read_lease",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                build_windows_installer._ChangedPath()
            ),
        )
    if "namespace" in failures:
        monkeypatch.setattr(
            build_windows_installer,
            "_validate_windows_output_namespace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                build_windows_installer._UnsafePath()
            ),
        )
    if "bytes" in failures:
        monkeypatch.setattr(
            build_windows_installer,
            "_validate_windows_output_bytes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                build_windows_installer._ChangedPath()
            ),
        )
    if "binding" in failures:
        monkeypatch.setattr(
            build_windows_installer,
            "_validate_compiled_installer_binding",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                build_windows_installer._ChangedPath()
            ),
        )

    with pytest.raises(RuntimeError, match=rf"^{expected}$"):
        build_windows_installer.write_attestation(
            tmp_path / "attestation.json", compiled_installer=capability
        )
    assert not (tmp_path / "attestation.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows output close lifecycle")
@pytest.mark.parametrize("provenance", ["owned", "winner"])
@pytest.mark.parametrize("finalizer", ["plain", "attestation"])
def test_output_parent_close_failure_is_exactly_once_nonoverriding(
    monkeypatch, tmp_path, provenance, finalizer
):
    capability, _calls, output_dir, setup = _compiled_for_provenance(
        monkeypatch, tmp_path / provenance / finalizer, provenance
    )
    real_close = build_windows_installer._close_windows_entry
    failed_roles: list[str] = []

    def fail_one_parent_close(entry):
        if not failed_roles and entry.role in {
            "output-parent",
            "output-ancestor",
            "output-anchor",
        }:
            failed_roles.append(entry.role)
            build_windows_installer._force_close_windows_handle(entry.handle)
            entry.closed = True
            raise OSError("synthetic parent close failure")
        return real_close(entry)

    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", fail_one_parent_close)
    if finalizer == "plain":
        assert build_windows_installer.finalize_compiled_installer(capability) == setup.absolute()
    else:
        attestation = tmp_path / provenance / finalizer / "attestation.json"
        assert build_windows_installer.write_attestation(
            attestation, compiled_installer=capability
        ) == setup.absolute()
    assert len(failed_roles) == 1
    renamed = output_dir.with_name("output-after-close")
    os.replace(output_dir, renamed)
    os.replace(renamed, output_dir)


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-bound attestation")
def test_attestation_temp_cannot_be_path_substituted_before_same_handle_commit(
    monkeypatch, tmp_path
):
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "build")
    output = tmp_path / "attestation.json"
    replacement = tmp_path / "replacement.json"
    replacement.write_bytes(b"attacker bytes")
    real_rename = build_windows_installer._rename_windows_handle
    blocked = False

    def attempt_substitution(handle, parent_handle, destination):
        nonlocal blocked
        if Path(destination) == output.absolute():
            temporary = output.parent / build_windows_installer._query_windows_opened_name(
                handle
            )
            try:
                os.replace(replacement, temporary)
            except OSError:
                blocked = True
            else:
                raise AssertionError("attestation temp pathname was substitutable")
        return real_rename(handle, parent_handle, destination)

    monkeypatch.setattr(build_windows_installer, "_rename_windows_handle", attempt_substitution)
    expected_setup = capability.output_lease.loose_path
    assert build_windows_installer.write_attestation(
        output, compiled_installer=capability
    ) == expected_setup
    assert blocked is True
    assert replacement.read_bytes() == b"attacker bytes"


@pytest.mark.skipif(os.name != "nt", reason="Windows attestation winner handling")
def test_attestation_exact_winner_reuses_and_different_winner_is_preserved(
    monkeypatch, tmp_path
):
    output = tmp_path / "attestation.json"
    first, _calls = _compile_capability(monkeypatch, tmp_path / "first-build")
    first_setup = first.output_lease.loose_path
    assert build_windows_installer.write_attestation(
        output, compiled_installer=first
    ) == first_setup
    exact_bytes = output.read_bytes()

    second, _calls = _compile_capability(monkeypatch, tmp_path / "second-build")
    second_setup = second.output_lease.loose_path
    assert build_windows_installer.write_attestation(
        output, compiled_installer=second
    ) == second_setup
    assert output.read_bytes() == exact_bytes

    output.write_bytes(b"foreign attestation winner")
    third, _calls = _compile_capability(monkeypatch, tmp_path / "third-build")
    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_PUBLISH_ERROR$"):
        build_windows_installer.write_attestation(
            output, compiled_installer=third
        )
    assert output.read_bytes() == b"foreign attestation winner"


def test_hostile_capabilities_and_path_protocols_are_rejected_without_execution(
    tmp_path,
):
    class Hostile:
        def __getattribute__(self, _name):
            raise AssertionError("hostile capability attribute executed")

        def __fspath__(self):
            raise AssertionError("hostile fspath executed")

    hostile = Hostile()
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(hostile)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"):
        build_windows_installer.write_attestation(
            tmp_path / "attestation.json", compiled_installer=hostile
        )
    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"):
        build_windows_installer.write_attestation(
            hostile, compiled_installer=hostile
        )


def test_hostile_iscc_is_rejected_before_stage_protocol_output_mutation_or_subprocess(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output = tmp_path / "output"
    protocols: list[str] = []
    stage_protocols: list[str] = []
    subprocess_calls: list[object] = []
    real_acquire = build_windows_installer._acquire_windows_stage_read_lease

    class HostileCompilerPath:
        def __str__(self):
            protocols.append("str")
            raise RuntimeError("PRIVATE_COMPILER_PATH_PROTOCOL")

        def __fspath__(self):
            protocols.append("fspath")
            raise RuntimeError("PRIVATE_COMPILER_PATH_PROTOCOL")

        def __repr__(self):
            protocols.append("repr")
            raise RuntimeError("PRIVATE_COMPILER_PATH_PROTOCOL")

    def record_acquire(*args, **kwargs):
        stage_protocols.append("acquire")
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(
        build_windows_installer, "_acquire_windows_stage_read_lease", record_acquire
    )
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess_calls.append(object()),
    )

    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.compile_installer(
            HostileCompilerPath(),
            staged,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=output,
        )

    assert str(caught.value) == "INSTALLER_COMPILER_INPUT_INVALID"
    assert protocols == []
    assert stage_protocols == []
    assert subprocess_calls == []
    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("source_zip", "INSTALLER_SOURCE_UNSAFE"),
        ("dist_dir", "INSTALLER_SOURCE_UNSAFE"),
        ("stage_dir", "INSTALLER_STAGE_UNSAFE"),
    ],
)
def test_hostile_stage_public_paths_fail_before_any_protocol_or_filesystem_authority(
    monkeypatch, tmp_path, field, expected
):
    source, _document, _members = _valid_zip(tmp_path / "input")
    protocols: list[str] = []
    captures: list[object] = []
    parent_prepares: list[object] = []

    class HostilePublicPath:
        def __str__(self):
            protocols.append("str")
            raise RuntimeError("PRIVATE_STAGE_PATH_PROTOCOL")

        def __fspath__(self):
            protocols.append("fspath")
            raise RuntimeError("PRIVATE_STAGE_PATH_PROTOCOL")

        def __repr__(self):
            protocols.append("repr")
            raise RuntimeError("PRIVATE_STAGE_PATH_PROTOCOL")

    hostile = HostilePublicPath()
    arguments = {
        "source_zip": source,
        "dist_dir": tmp_path / "dist",
        "stage_dir": tmp_path / "stages",
    }
    arguments[field] = hostile
    real_capture = build_windows_installer._capture_regular_file
    real_prepare = build_windows_installer._prepare_windows_stage_parent

    def record_capture(*args, **kwargs):
        captures.append(object())
        return real_capture(*args, **kwargs)

    def record_prepare(*args, **kwargs):
        parent_prepares.append(object())
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(build_windows_installer, "read_version", lambda: PACKAGE_VERSION)
    monkeypatch.setattr(build_windows_installer, "_capture_regular_file", record_capture)
    monkeypatch.setattr(
        build_windows_installer, "_prepare_windows_stage_parent", record_prepare
    )

    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.stage_release(**arguments)

    assert str(caught.value) == expected
    assert protocols == []
    assert captures == []
    assert parent_prepares == []
    assert not (tmp_path / "dist").exists()
    assert not (tmp_path / "stages").exists()


def test_main_consumes_compiled_capability_without_attestation(monkeypatch, tmp_path):
    staged = _stage(monkeypatch, tmp_path / "stage")
    capability = object()
    setup = tmp_path / "Setup.exe"
    finalize_calls: list[object] = []
    monkeypatch.setattr(sys, "argv", ["build_windows_installer.py"])
    monkeypatch.setattr(build_windows_installer, "find_iscc", lambda _path: tmp_path / "ISCC.exe")
    monkeypatch.setattr(
        build_windows_installer,
        "verify_inno_toolchain",
        lambda *_args: _valid_toolchain_identity(),
    )
    monkeypatch.setattr(build_windows_installer, "stage_release", lambda *_args, **_kwargs: staged)
    monkeypatch.setattr(
        build_windows_installer,
        "compile_installer",
        lambda *_args, **_kwargs: capability,
    )

    def finalize(value):
        finalize_calls.append(value)
        return setup

    monkeypatch.setattr(
        build_windows_installer,
        "finalize_compiled_installer",
        finalize,
        raising=False,
    )
    assert build_windows_installer.main() == 0
    assert finalize_calls == [capability]


@pytest.mark.skipif(os.name != "nt", reason="Windows immutable compiled capability")
def test_attestation_uses_immutable_toolchain_snapshot_after_binding_validation(
    monkeypatch, tmp_path
):
    original = _valid_toolchain_identity()
    staged = _stage(monkeypatch, tmp_path / "stage")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        _synthetic_compiler_runner(b"immutable setup", calls),
    )
    capability = build_windows_installer.compile_installer(
        tmp_path / "ISCC.exe",
        staged,
        toolchain_identity=original,
        output_dir=tmp_path / "output",
    )
    expected_setup = capability.output_lease.loose_path
    expected_toolchain = _valid_toolchain_identity()
    original["name"] = "CALLER MUTATION"
    original["version"] = "0.0.0"
    real_validate = build_windows_installer._validate_compiled_installer_binding
    mutation_succeeded: list[bool] = []

    def mutate_after_validation(value):
        result = real_validate(value)
        mutated = False
        try:
            live = getattr(value, "toolchain_identity")
            live["name"] = "UNBOUND AFTER VALIDATION"
            mutated = True
        except (AttributeError, TypeError):
            pass
        mutation_succeeded.append(mutated)
        return result

    monkeypatch.setattr(
        build_windows_installer,
        "_validate_compiled_installer_binding",
        mutate_after_validation,
    )
    attestation = tmp_path / "attestation.json"

    assert build_windows_installer.write_attestation(
        attestation, compiled_installer=capability
    ) == expected_setup
    published = json.loads(attestation.read_text(encoding="utf-8"))
    assert mutation_succeeded == [False]
    assert published["toolchain"] == expected_toolchain
    assert "UNBOUND AFTER VALIDATION" not in attestation.read_text(encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows terminal attestation close law")
def test_attestation_auxiliary_close_failure_is_terminal_and_preserves_temp_and_destination(
    monkeypatch, tmp_path
):
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "build")
    destination = tmp_path / "attestation.json"
    destination.write_bytes(b"seeded destination")
    fixed_hex = "a" * 32
    temporary = tmp_path / (build_windows_installer._ATTESTATION_TEMP_PREFIX + fixed_hex)
    real_close = build_windows_installer._close_windows_entry
    real_rename = build_windows_installer._rename_windows_handle
    real_dispose = build_windows_installer._dispose_windows_file_handle
    real_quarantine = build_windows_installer._quarantine_windows_handle
    terminal = False
    closes: list[str] = []
    later_actions: list[str] = []

    class FixedUuid:
        hex = fixed_hex

    def fail_aux_close(entry):
        nonlocal terminal
        closes.append(entry.role)
        if not terminal and entry.role == "attestation-temp":
            terminal = True
            build_windows_installer._force_close_windows_handle(entry.handle)
            entry.closed = True
            raise OSError("synthetic terminal auxiliary close failure")
        return real_close(entry)

    def record_rename(*args, **kwargs):
        if terminal:
            later_actions.append("rename")
        return real_rename(*args, **kwargs)

    def record_dispose(*args, **kwargs):
        if terminal:
            later_actions.append("dispose")
        return real_dispose(*args, **kwargs)

    def record_quarantine(*args, **kwargs):
        if terminal:
            later_actions.append("quarantine")
        return real_quarantine(*args, **kwargs)

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", fail_aux_close)
    monkeypatch.setattr(build_windows_installer, "_rename_windows_handle", record_rename)
    monkeypatch.setattr(build_windows_installer, "_dispose_windows_file_handle", record_dispose)
    monkeypatch.setattr(
        build_windows_installer, "_quarantine_windows_handle", record_quarantine
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_IO_ERROR$"):
        build_windows_installer.write_attestation(
            destination, compiled_installer=capability
        )

    assert "attestation-temp" in closes
    assert later_actions == []
    assert destination.read_bytes() == b"seeded destination"
    assert temporary.is_file()
    assert temporary.read_bytes().endswith(b"\n")
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows authentic capability consumption")
@pytest.mark.parametrize(
    "legacy_name", ["staged_release", "installer_exe", "toolchain_identity"]
)
def test_forbidden_legacy_attestation_arguments_consume_authentic_capability_exactly_once(
    monkeypatch, tmp_path, legacy_name
):
    capability, _calls = _compile_capability(
        monkeypatch, tmp_path / legacy_name / "build"
    )
    destination = tmp_path / legacy_name / "attestation.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(b"seeded")
    protocols: list[str] = []
    close_handles: list[int] = []
    real_close = build_windows_installer._close_windows_entry

    class HostileLegacyValue:
        def __str__(self):
            protocols.append("str")
            raise AssertionError("legacy value protocol executed")

        def __fspath__(self):
            protocols.append("fspath")
            raise AssertionError("legacy value protocol executed")

        def __getattribute__(self, name):
            if name.startswith("__"):
                return object.__getattribute__(self, name)
            protocols.append("attribute")
            raise AssertionError("legacy value protocol executed")

    def record_close(entry):
        close_handles.append(_handle_key(entry.handle))
        return real_close(entry)

    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", record_close)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"):
        build_windows_installer.write_attestation(
            destination,
            compiled_installer=capability,
            **{legacy_name: HostileLegacyValue()},
        )
    first_close_count = len(close_handles)
    assert first_close_count > 0
    assert len(close_handles) == len(set(close_handles))
    assert protocols == []
    assert destination.read_bytes() == b"seeded"

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"):
        build_windows_installer.write_attestation(
            destination, compiled_installer=capability
        )
    assert len(close_handles) == first_close_count


@pytest.mark.skipif(os.name != "nt", reason="Windows attestation return contract")
@pytest.mark.parametrize("provenance", ["owned", "winner"])
def test_write_attestation_returns_verified_loose_setup_path_for_both_output_provenances(
    monkeypatch, tmp_path, provenance
):
    capability, _calls, _output_dir, setup = _compiled_for_provenance(
        monkeypatch, tmp_path / provenance, provenance
    )
    attestation = tmp_path / provenance / "attestation.json"
    result = build_windows_installer.write_attestation(
        attestation, compiled_installer=capability
    )

    assert result == setup.absolute()
    assert setup.is_file()
    assert json.loads(attestation.read_text(encoding="utf-8"))["schema"] == (
        "bcs.freecad_installer_attestation/1.1"
    )
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.parametrize("with_attestation", [False, True])
def test_main_calls_exactly_one_selected_finalizer_and_uses_only_its_return(
    monkeypatch, tmp_path, capsys, with_attestation
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    setup = (tmp_path / "verified" / "Setup.exe").absolute()
    field_reads: list[str] = []
    calls: list[tuple[str, object]] = []

    class OpaqueCompiled:
        @property
        def output_lease(self):
            field_reads.append("output_lease")
            raise AssertionError("main read capability internals")

    capability = OpaqueCompiled()
    argv = ["build_windows_installer.py"]
    if with_attestation:
        argv.extend(["--attestation", str(tmp_path / "attestation.json")])
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.delenv("GITHUB_SHA", raising=False)
    monkeypatch.setattr(build_windows_installer, "CompiledInstaller", OpaqueCompiled)
    monkeypatch.setattr(
        build_windows_installer, "find_iscc", lambda _value: tmp_path / "ISCC.exe"
    )
    monkeypatch.setattr(
        build_windows_installer,
        "verify_inno_toolchain",
        lambda *_args: _valid_toolchain_identity(),
    )
    monkeypatch.setattr(
        build_windows_installer, "stage_release", lambda *_args, **_kwargs: staged
    )
    monkeypatch.setattr(
        build_windows_installer,
        "compile_installer",
        lambda *_args, **_kwargs: capability,
    )

    def finalize(value):
        calls.append(("plain", value))
        return setup

    def attest(_output, *, compiled_installer):
        calls.append(("attestation", compiled_installer))
        return setup

    monkeypatch.setattr(
        build_windows_installer, "finalize_compiled_installer", finalize
    )
    monkeypatch.setattr(build_windows_installer, "write_attestation", attest)

    assert build_windows_installer.main() == 0
    assert calls == [
        ("attestation" if with_attestation else "plain", capability)
    ]
    assert field_reads == []
    assert f"Installer:   {setup}" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "nt", reason="Windows compiler handle choreography")
def test_guarded_compiler_uses_exact_native_access_share_matrix(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    setup_name = f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe"
    native_calls: list[tuple[str, int, int, int, int]] = []
    process_calls: list[list[str]] = []
    real_relative_create = build_windows_installer._nt_relative_create

    def record_relative_create(parent_handle, name, **kwargs):
        native_calls.append(
            (
                str(name),
                kwargs["desired_access"],
                kwargs["share_access"],
                kwargs["disposition"],
                kwargs["create_options"],
            )
        )
        return real_relative_create(parent_handle, name, **kwargs)

    synthetic = _synthetic_compiler_runner(b"guarded setup", process_calls)

    def run(command, **kwargs):
        assert kwargs["check"] is True
        assert kwargs["cwd"] == REPO_ROOT
        assert kwargs["close_fds"] is True
        return synthetic(command, **kwargs)

    monkeypatch.setattr(
        build_windows_installer, "_nt_relative_create", record_relative_create
    )
    monkeypatch.setattr(build_windows_installer.subprocess, "run", run)

    capability = build_windows_installer.compile_installer(
        tmp_path / "ISCC.exe",
        staged,
        toolchain_identity=_valid_toolchain_identity(),
        output_dir=output_dir,
    )

    root_call = next(
        call
        for call in native_calls
        if call[0].startswith(build_windows_installer._OUTPUT_TEMP_PREFIX)
    )
    guard_call = next(
        call
        for call in native_calls
        if call[0] == setup_name and call[3] == build_windows_installer._FILE_CREATE
    )
    reader_call = next(
        call
        for call in native_calls
        if call[0] == setup_name and call[3] == build_windows_installer._FILE_OPEN
    )
    assert root_call[1:4] == (0x001100A3, 0x3, build_windows_installer._FILE_CREATE)
    assert guard_call[1:4] == (0x00110080, 0x3, build_windows_installer._FILE_CREATE)
    assert reader_call[1:4] == (0x00100081, 0x5, build_windows_installer._FILE_OPEN)
    assert len(process_calls) == 1
    build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows compiler lease boundary")
def test_compiler_holds_stage_lease_and_binds_completed_writer_bytes(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    mutation_blocked = False
    process_calls: list[list[str]] = []
    synthetic = _synthetic_compiler_runner(b"captured writer bytes", process_calls)

    def run(command, **kwargs):
        nonlocal mutation_blocked
        try:
            (staged.source_dir / "module.py").write_bytes(b"late mutation")
        except OSError:
            mutation_blocked = True
        else:
            raise AssertionError("retained stage lease admitted a late writer")
        return synthetic(command, **kwargs)

    monkeypatch.setattr(build_windows_installer.subprocess, "run", run)
    capability = build_windows_installer.compile_installer(
        tmp_path / "ISCC.exe",
        staged,
        toolchain_identity=_valid_toolchain_identity(),
        output_dir=output_dir,
    )

    assert mutation_blocked is True
    assert capability.setup_bytes == b"captured writer bytes"
    identity = json.loads(capability.output_identity_bytes)
    assert identity["setup_size"] == len(b"captured writer bytes")
    assert identity["setup_sha256"] == hashlib.sha256(
        b"captured writer bytes"
    ).hexdigest()
    assert not ({"pid", "process", "writer", "executable"} & set(identity))
    build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows guarded compiler output")
def test_compiler_temp_root_collision_is_foreign_and_invokes_no_process(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    token = "4" * 32
    collision = output_dir / (build_windows_installer._OUTPUT_TEMP_PREFIX + token)
    collision.mkdir()
    marker = collision / "foreign.bin"
    marker.write_bytes(b"foreign root")
    calls: list[object] = []

    class FixedUUID:
        hex = token

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUUID())
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append(object()),
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_FAILED$"):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=output_dir,
        )
    assert calls == []
    assert marker.read_bytes() == b"foreign root"


@pytest.mark.skipif(os.name != "nt", reason="Windows guarded compiler output")
@pytest.mark.parametrize("failure", ["compiler", "extra-output", "hardlink"])
def test_compiler_output_failures_preserve_only_owned_handle_bound_residue(
    monkeypatch, tmp_path, failure
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    process_calls: list[list[str]] = []
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside sentinel")

    if failure == "compiler":
        def run(command, **_kwargs):
            process_calls.append(list(command))
            raise subprocess.CalledProcessError(1, command)
    else:
        def after_write(setup):
            if failure == "extra-output":
                (setup.parent / "foreign-extra.bin").write_bytes(b"foreign extra")
            else:
                try:
                    os.link(setup, setup.parent / "foreign-hardlink.exe")
                except OSError:
                    pytest.skip("hardlink creation unavailable")

        run = _synthetic_compiler_runner(
            b"candidate setup", process_calls, after_write=after_write
        )
    monkeypatch.setattr(build_windows_installer.subprocess, "run", run)

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_FAILED$"):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=output_dir,
        )
    assert len(process_calls) == 1
    assert outside.read_bytes() == b"outside sentinel"
    assert not list(output_dir.glob(f"FreeCAD-PDF-Importer-Setup_v{PACKAGE_VERSION}.exe"))
    residue = list(output_dir.glob(build_windows_installer._OUTPUT_TEMP_PREFIX + "*"))
    if failure == "compiler":
        assert residue == []
    else:
        assert len(residue) == 1
        assert any(path.name.startswith("foreign-") for path in residue[0].iterdir())


@pytest.mark.skipif(os.name != "nt", reason="Windows compiler close lifecycle")
@pytest.mark.parametrize("role", ["output-guard", "output-root"])
def test_compiler_terminal_output_close_failure_preserves_root_and_stops_rename(
    monkeypatch, tmp_path, role
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    real_close = build_windows_installer._close_windows_entry
    failed: list[str] = []
    rename_calls: list[Path] = []

    def fail_compiler(command, **_kwargs):
        raise subprocess.CalledProcessError(1, command)

    def fail_selected_close(entry):
        if not failed and entry.role == role:
            failed.append(role)
            build_windows_installer._force_close_windows_handle(entry.handle)
            entry.closed = True
            raise OSError("synthetic terminal close failure")
        return real_close(entry)

    def record_rename(handle, parent_handle, destination):
        rename_calls.append(Path(destination))
        return build_windows_installer._rename_windows_handle(
            handle, parent_handle, destination
        )

    monkeypatch.setattr(build_windows_installer.subprocess, "run", fail_compiler)
    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", fail_selected_close)
    original_rename = build_windows_installer._rename_windows_handle
    monkeypatch.setattr(
        build_windows_installer,
        "_rename_windows_handle",
        lambda handle, parent_handle, destination: (
            rename_calls.append(Path(destination)),
            original_rename(handle, parent_handle, destination),
        )[1],
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_FAILED$"):
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=output_dir,
        )
    assert failed == [role]
    assert rename_calls == []
    assert len(list(output_dir.glob(build_windows_installer._OUTPUT_TEMP_PREFIX + "*"))) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows compiler publication residue")
def test_successful_loose_publication_never_recursively_traverses_temp_residue(
    monkeypatch, tmp_path
):
    output_dir = tmp_path / "output"
    real_dispose = build_windows_installer._dispose_windows_file_handle
    recursive_calls: list[object] = []

    def leave_root(handle):
        try:
            if build_windows_installer._query_windows_attribute_tag(handle)[0] & (
                build_windows_installer._FILE_ATTRIBUTE_DIRECTORY
            ):
                return False
        except Exception:
            pass
        return real_dispose(handle)

    monkeypatch.setattr(build_windows_installer, "_dispose_windows_file_handle", leave_root)
    monkeypatch.setattr(
        build_windows_installer.shutil,
        "rmtree",
        lambda *_args, **_kwargs: recursive_calls.append(object()),
    )
    capability, _calls = _compile_capability(
        monkeypatch, tmp_path / "build", output_dir=output_dir
    )
    setup = build_windows_installer.finalize_compiled_installer(capability)

    assert setup.is_file()
    assert recursive_calls == []
    assert len(list(output_dir.glob(build_windows_installer._OUTPUT_TEMP_PREFIX + "*"))) == 1


def test_workflow_and_finalizer_share_the_exact_loose_setup_path_contract():
    workflow = (REPO_ROOT / ".github" / "workflows" / "auto-release.yml").read_text(
        encoding="utf-8"
    )
    assert '$base = "FreeCAD-PDF-Importer-Setup_v$version"' in workflow
    assert "$first = Join-Path 'dist/repro-a' ($base + '.exe')" in workflow
    assert "$second = Join-Path 'dist/repro-b' ($base + '.exe')" in workflow
    assert "--output-dir dist/repro-a" in workflow
    assert "--output-dir dist/repro-b" in workflow
    assert "Copy-Item -LiteralPath $first" in workflow


@pytest.mark.skipif(os.name != "nt", reason="Windows sealed capability lifecycle")
@pytest.mark.parametrize("raise_inside", [False, True])
def test_compiled_capability_context_releases_leaf_parent_chain_then_stage_once(
    monkeypatch, tmp_path, raise_inside
):
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "build")
    events: list[tuple[str, int]] = []
    real_close = build_windows_installer._close_windows_entry

    def record_close(entry):
        events.append((entry.role, _handle_key(entry.handle)))
        return real_close(entry)

    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", record_close)
    if raise_inside:
        with pytest.raises(ValueError, match="caller failure"):
            with capability:
                raise ValueError("caller failure")
    else:
        with capability:
            pass

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)
    output_leaf = min(
        index
        for index, (role, _handle) in enumerate(events)
        if role in {"output-reader", "output-guard", "output-winner"}
    )
    output_parent = min(
        index
        for index, (role, _handle) in enumerate(events)
        if role in {"output-parent", "output-ancestor", "output-anchor"}
    )
    stage = min(
        index
        for index, (role, _handle) in enumerate(events)
        if role.startswith("stage-")
    )
    assert output_leaf < output_parent < stage
    handles = [handle for _role, handle in events]
    assert len(handles) == len(set(handles))


@pytest.mark.skipif(os.name != "nt", reason="Windows capability cross-binding")
def test_mixed_compiled_capability_is_consumed_and_rejected_before_attestation(
    monkeypatch, tmp_path
):
    first, _calls = _compile_capability(monkeypatch, tmp_path / "first")
    second, _calls = _compile_capability(monkeypatch, tmp_path / "second")
    mixed = dataclasses.replace(first, output_lease=second.output_lease)
    output = tmp_path / "mixed.json"

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_INPUT_INVALID$"):
        build_windows_installer.write_attestation(
            output, compiled_installer=mixed
        )
    assert not output.exists()
    build_windows_installer.finalize_compiled_installer(first)
    build_windows_installer.finalize_compiled_installer(second)


@pytest.mark.skipif(os.name != "nt", reason="Windows attestation temp identity")
@pytest.mark.parametrize("identity_failure", ["link-count", "file-id-high-half"])
def test_attestation_temp_requires_full_identity_and_single_link(
    monkeypatch, tmp_path, identity_failure
):
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "build")
    output = tmp_path / "attestation.json"
    real_links = build_windows_installer._query_windows_link_count
    real_metadata = build_windows_installer._windows_handle_metadata

    def is_attestation_temp(handle):
        return build_windows_installer._query_windows_opened_name(handle).startswith(
            build_windows_installer._ATTESTATION_TEMP_PREFIX
        )

    def changed_links(handle):
        if identity_failure == "link-count" and is_attestation_temp(handle):
            return 2
        return real_links(handle)

    def changed_metadata(handle):
        metadata = real_metadata(handle)
        if identity_failure == "file-id-high-half" and is_attestation_temp(handle):
            return dataclasses.replace(
                metadata,
                file_id=metadata.file_id[:8]
                + bytes([metadata.file_id[8] ^ 1])
                + metadata.file_id[9:],
            )
        return metadata

    monkeypatch.setattr(build_windows_installer, "_query_windows_link_count", changed_links)
    monkeypatch.setattr(build_windows_installer, "_windows_handle_metadata", changed_metadata)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_IO_ERROR$"):
        build_windows_installer.write_attestation(
            output, compiled_installer=capability
        )
    assert not output.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows attestation collision cleanup")
@pytest.mark.parametrize(
    ("winner", "cleanup_succeeds", "expected"),
    [
        ("exact", False, "INSTALLER_ATTESTATION_IO_ERROR"),
        ("different", True, "INSTALLER_ATTESTATION_PUBLISH_ERROR"),
        ("different", False, "INSTALLER_ATTESTATION_PUBLISH_ERROR"),
        ("noncollision", True, "INSTALLER_ATTESTATION_PUBLISH_ERROR"),
        ("noncollision", False, "INSTALLER_ATTESTATION_PUBLISH_ERROR"),
    ],
)
def test_attestation_collision_and_publish_tokens_do_not_overwrite(
    monkeypatch, tmp_path, winner, cleanup_succeeds, expected
):
    output = tmp_path / "attestation.json"
    first, _calls = _compile_capability(monkeypatch, tmp_path / "first")
    if winner == "exact":
        build_windows_installer.write_attestation(output, compiled_installer=first)
    else:
        build_windows_installer.finalize_compiled_installer(first)
        if winner == "different":
            output.write_bytes(b"foreign winner")
    original = output.read_bytes() if output.exists() else None
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "candidate")
    real_dispose = build_windows_installer._dispose_windows_file_handle
    real_rename = build_windows_installer._rename_windows_handle

    def dispose(handle):
        if build_windows_installer._query_windows_opened_name(handle).startswith(
            build_windows_installer._ATTESTATION_TEMP_PREFIX
        ):
            return cleanup_succeeds and real_dispose(handle)
        return real_dispose(handle)

    def rename(handle, parent_handle, destination):
        if winner == "noncollision" and Path(destination) == output.absolute():
            raise OSError("synthetic noncollision failure")
        return real_rename(handle, parent_handle, destination)

    monkeypatch.setattr(build_windows_installer, "_dispose_windows_file_handle", dispose)
    monkeypatch.setattr(build_windows_installer, "_rename_windows_handle", rename)

    with pytest.raises(RuntimeError, match=rf"^{expected}$"):
        build_windows_installer.write_attestation(
            output, compiled_installer=capability
        )
    if original is None:
        assert not output.exists()
    else:
        assert output.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Windows attestation winner lifecycle")
def test_attestation_exact_winner_is_held_through_temp_cleanup_and_consumption(
    monkeypatch, tmp_path
):
    output = tmp_path / "attestation.json"
    first, _calls = _compile_capability(monkeypatch, tmp_path / "first")
    build_windows_installer.write_attestation(output, compiled_installer=first)
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "second")
    events: list[str] = []
    real_relative_create = build_windows_installer._nt_relative_create
    real_dispose = build_windows_installer._dispose_windows_file_handle
    real_close = build_windows_installer._close_windows_entry

    def relative_create(parent_handle, name, **kwargs):
        handle = real_relative_create(parent_handle, name, **kwargs)
        if (
            str(name) == output.name
            and kwargs["disposition"] == build_windows_installer._FILE_OPEN
        ):
            events.append("winner-open")
        return handle

    def dispose(handle):
        if build_windows_installer._query_windows_opened_name(handle).startswith(
            build_windows_installer._ATTESTATION_TEMP_PREFIX
        ):
            events.append("temp-dispose")
        return real_dispose(handle)

    def close(entry):
        if entry.role == "attestation-winner":
            events.append("winner-close")
        elif entry.role == "stage-monitor":
            events.append("monitor-proof-close")
        elif entry.role == "stage-monitor-successor":
            events.append("monitor-authority-close")
        elif entry.role.startswith("output-") or entry.role.startswith("stage-"):
            events.append("capability-close")
        return real_close(entry)

    monkeypatch.setattr(build_windows_installer, "_nt_relative_create", relative_create)
    monkeypatch.setattr(build_windows_installer, "_dispose_windows_file_handle", dispose)
    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", close)

    expected_setup = capability.output_lease.loose_path
    assert build_windows_installer.write_attestation(
        output, compiled_installer=capability
    ) == expected_setup
    assert events.count("winner-open") == 1
    assert events.count("winner-close") == 1
    assert events.count("monitor-proof-close") == 1
    assert events.count("monitor-authority-close") == 1
    assert events.index("monitor-proof-close") < events.index("winner-open")
    assert events.index("winner-open") < events.index("temp-dispose")
    assert events.index("temp-dispose") < events.index("monitor-authority-close")
    assert events.index("monitor-authority-close") < events.index("capability-close")
    assert events.index("capability-close") < events.index("winner-close")


# Task 5C independent-review A RED boundary.


@pytest.mark.skipif(os.name != "nt", reason="Windows attestation monitor boundary")
@pytest.mark.parametrize("outcome", ["noncollision", "exact-winner", "different-winner"])
def test_attestation_has_no_monitor_retirement_to_terminal_decision_window(
    monkeypatch, tmp_path, outcome
):
    destination = tmp_path / outcome / "installer-attestation.json"
    destination.parent.mkdir(parents=True)
    if outcome == "exact-winner":
        first, _calls = _compile_capability(monkeypatch, tmp_path / "seed")
        build_windows_installer.write_attestation(
            destination, compiled_installer=first
        )
    elif outcome == "different-winner":
        destination.write_bytes(b"foreign winner")

    capability, _calls = _compile_capability(monkeypatch, tmp_path / "candidate")
    transient = capability.staged_release.source_dir / "retirement-window.py"
    real_retire = build_windows_installer._retire_windows_stage_monitor
    real_rename = build_windows_installer._rename_windows_handle
    real_open_winner = build_windows_installer._open_windows_attestation_winner
    real_read = build_windows_installer._read_windows_file_handle
    real_cleanup = build_windows_installer._cleanup_windows_attestation_temp
    real_consume = build_windows_installer._consume_compiled_installer
    events: list[str] = []
    terminal = False
    winner_key: int | None = None
    winner_reads = 0

    def inject_if_preterminal(boundary):
        if terminal:
            return
        transient.write_bytes(b"foreign transient input")
        assert transient.read_bytes() == b"foreign transient input"
        transient.unlink()
        events.append("injected-before-terminal:" + boundary)

    def retire(lease):
        result = real_retire(lease)
        events.append("monitor-retired")
        inject_if_preterminal("retire")
        return result

    def rename(*args, **kwargs):
        nonlocal terminal
        events.append("rename-attempt")
        try:
            result = real_rename(*args, **kwargs)
        except FileExistsError:
            events.append("rename-collision")
            raise
        terminal = True
        events.append("noncollision-commit")
        return result

    def open_winner(*args, **kwargs):
        nonlocal winner_key
        entry = real_open_winner(*args, **kwargs)
        winner_key = _handle_key(entry.handle)
        events.append("winner-open")
        return entry

    def read(handle):
        nonlocal terminal, winner_reads
        payload = real_read(handle)
        if winner_key is not None and _handle_key(handle) == winner_key:
            winner_reads += 1
            events.append(f"winner-read:{winner_reads}")
            if outcome == "exact-winner" and winner_reads == 2:
                terminal = True
                events.append("exact-winner-verified")
            elif outcome == "different-winner" and winner_reads == 1:
                terminal = True
                events.append("different-winner-classified")
        return payload

    def cleanup(entry):
        result = real_cleanup(entry)
        events.append("temp-cleanup")
        return result

    def consume(value):
        events.append("capability-consume")
        inject_if_preterminal("consume")
        return real_consume(value)

    monkeypatch.setattr(build_windows_installer, "_retire_windows_stage_monitor", retire)
    monkeypatch.setattr(build_windows_installer, "_rename_windows_handle", rename)
    monkeypatch.setattr(
        build_windows_installer, "_open_windows_attestation_winner", open_winner
    )
    monkeypatch.setattr(build_windows_installer, "_read_windows_file_handle", read)
    monkeypatch.setattr(
        build_windows_installer, "_cleanup_windows_attestation_temp", cleanup
    )
    monkeypatch.setattr(build_windows_installer, "_consume_compiled_installer", consume)

    if outcome == "different-winner":
        with pytest.raises(
            RuntimeError, match=r"^INSTALLER_ATTESTATION_PUBLISH_ERROR$"
        ):
            build_windows_installer.write_attestation(
                destination, compiled_installer=capability
            )
        assert destination.read_bytes() == b"foreign winner"
    else:
        expected_setup = capability.output_lease.loose_path
        assert build_windows_installer.write_attestation(
            destination, compiled_installer=capability
        ) == expected_setup
        assert destination.is_file()

    assert not any(event.startswith("injected-before-terminal:") for event in events)
    assert not transient.exists()
    assert "capability-consume" in events
    if outcome == "noncollision":
        assert events.index("noncollision-commit") < events.index("capability-consume")
    elif outcome == "exact-winner":
        assert events.index("exact-winner-verified") < events.index(
            "capability-consume"
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows attestation terminal I/O")
@pytest.mark.parametrize(
    "primary_failure",
    [
        "write",
        "flush",
        "readback-1",
        "readback-2",
        "identity-1",
        "identity-2",
        "link-1",
        "link-2",
    ],
)
def test_attestation_primary_io_failure_plus_aux_close_failure_preserves_temp(
    monkeypatch, tmp_path, primary_failure
):
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "build")
    destination = tmp_path / "installer-attestation.json"
    destination.write_bytes(b"seeded destination")
    fixed_hex = "e" * 32
    temporary_path = tmp_path / (
        build_windows_installer._ATTESTATION_TEMP_PREFIX + fixed_hex
    )
    real_create = build_windows_installer._create_windows_attestation_temp
    real_write = build_windows_installer._write_windows_file_handle
    real_flush = build_windows_installer._flush_windows_file_handle
    real_read = build_windows_installer._read_windows_file_handle
    real_metadata = build_windows_installer._windows_handle_metadata
    real_links = build_windows_installer._query_windows_link_count
    real_close = build_windows_installer._close_windows_entry
    real_dispose = build_windows_installer._dispose_windows_file_handle
    real_rename = build_windows_installer._rename_windows_handle
    real_quarantine = build_windows_installer._quarantine_windows_handle
    work_entry: list[object] = []
    primary_calls = {"readback": 0, "identity": 0, "link": 0}
    primary_triggered: list[str] = []
    close_failures: list[str] = []
    post_terminal_actions: list[str] = []
    terminal = False

    class FixedUuid:
        hex = fixed_hex

    def create(*args, **kwargs):
        temporary, work = real_create(*args, **kwargs)
        work_entry[:] = [work]
        return temporary, work

    def is_work_handle(handle):
        return bool(work_entry) and _handle_key(handle) == _handle_key(
            work_entry[0].handle
        )

    def write(handle, payload):
        if primary_failure == "write" and is_work_handle(handle):
            primary_triggered.append("write")
            raise OSError("synthetic write failure")
        return real_write(handle, payload)

    def flush(handle):
        if primary_failure == "flush" and is_work_handle(handle):
            primary_triggered.append("flush")
            raise OSError("synthetic flush failure")
        return real_flush(handle)

    def read(handle):
        if is_work_handle(handle):
            primary_calls["readback"] += 1
            expected = f"readback-{primary_calls['readback']}"
            if primary_failure == expected:
                primary_triggered.append(expected)
                return b"synthetic mismatched readback"
        return real_read(handle)

    def metadata(handle):
        if is_work_handle(handle):
            primary_calls["identity"] += 1
            expected = f"identity-{primary_calls['identity']}"
            if primary_failure == expected:
                primary_triggered.append(expected)
                raise build_windows_installer._ChangedPath
        return real_metadata(handle)

    def links(handle):
        if is_work_handle(handle):
            primary_calls["link"] += 1
            expected = f"link-{primary_calls['link']}"
            if primary_failure == expected:
                primary_triggered.append(expected)
                return 2
        return real_links(handle)

    def close(entry):
        nonlocal terminal
        if work_entry and entry is work_entry[0]:
            real_close(entry)
            terminal = True
            close_failures.append("attestation-temp")
            raise OSError("synthetic auxiliary close failure")
        return real_close(entry)

    def dispose(*args, **kwargs):
        if terminal:
            post_terminal_actions.append("dispose")
        return real_dispose(*args, **kwargs)

    def rename(*args, **kwargs):
        if terminal:
            post_terminal_actions.append("rename")
        return real_rename(*args, **kwargs)

    def quarantine(*args, **kwargs):
        if terminal:
            post_terminal_actions.append("quarantine")
        return real_quarantine(*args, **kwargs)

    monkeypatch.setattr(build_windows_installer.uuid, "uuid4", lambda: FixedUuid())
    monkeypatch.setattr(
        build_windows_installer, "_create_windows_attestation_temp", create
    )
    monkeypatch.setattr(build_windows_installer, "_write_windows_file_handle", write)
    monkeypatch.setattr(build_windows_installer, "_flush_windows_file_handle", flush)
    monkeypatch.setattr(build_windows_installer, "_read_windows_file_handle", read)
    monkeypatch.setattr(build_windows_installer, "_windows_handle_metadata", metadata)
    monkeypatch.setattr(build_windows_installer, "_query_windows_link_count", links)
    monkeypatch.setattr(build_windows_installer, "_close_windows_entry", close)
    monkeypatch.setattr(build_windows_installer, "_dispose_windows_file_handle", dispose)
    monkeypatch.setattr(build_windows_installer, "_rename_windows_handle", rename)
    monkeypatch.setattr(
        build_windows_installer, "_quarantine_windows_handle", quarantine
    )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_ATTESTATION_IO_ERROR$"):
        build_windows_installer.write_attestation(
            destination, compiled_installer=capability
        )

    assert primary_triggered == [primary_failure]
    assert close_failures == ["attestation-temp"]
    assert post_terminal_actions == []
    assert destination.read_bytes() == b"seeded destination"
    assert temporary_path.is_file()
    assert type(temporary_path.read_bytes()) is bytes
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)


@pytest.mark.skipif(os.name != "nt", reason="Windows stage monitor failure matrix")
def test_stage_monitor_queue_initialization_failure_is_closed_and_pathless(
    monkeypatch, tmp_path
):
    staged = _stage(monkeypatch, tmp_path / "stage")
    queue_monitor = getattr(
        build_windows_installer, "_queue_windows_stage_monitor", None
    )
    assert callable(queue_monitor), "missing explicit monitor queue authority seam"
    process_calls: list[object] = []

    def fail_queue(_monitor):
        raise build_windows_installer._NativeCapabilityError

    monkeypatch.setattr(
        build_windows_installer, "_queue_windows_stage_monitor", fail_queue
    )
    monkeypatch.setattr(
        build_windows_installer.subprocess,
        "run",
        lambda *args, **kwargs: process_calls.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError) as caught:
        build_windows_installer.compile_installer(
            tmp_path / "ISCC.exe",
            staged,
            toolchain_identity=_valid_toolchain_identity(),
            output_dir=tmp_path / "output",
        )
    assert _assert_pathless(caught.value, tmp_path) == "INSTALLER_COMPILER_INPUT_INVALID"
    assert process_calls == []
    assert not list((tmp_path / "output").glob("FreeCAD-PDF-Importer-Setup_*.exe"))


@pytest.mark.skipif(os.name != "nt", reason="Windows stage monitor failure matrix")
@pytest.mark.parametrize(
    "failure",
    [
        "wait",
        "zero-byte-completion",
        "get-result",
        "cancel-hard",
        "cancel-not-found",
        "cancel-timeout",
        "entry-close",
        "event-close",
    ],
)
def test_stage_monitor_failure_matrix_blocks_finalization(
    monkeypatch, tmp_path, failure
):
    capability, _calls = _compile_capability(monkeypatch, tmp_path / "build")
    monitor = capability.stage_lease.monitor
    assert monitor is not None
    injections: list[str] = []

    if failure == "wait":
        monkeypatch.setattr(
            build_windows_installer,
            "_wait_windows_stage_monitor",
            lambda _monitor, _timeout: 0xFFFFFFFF,
        )
    elif failure == "zero-byte-completion":
        monkeypatch.setattr(
            build_windows_installer,
            "_wait_windows_stage_monitor",
            lambda _monitor, _timeout: build_windows_installer._WAIT_OBJECT_0,
        )
        monkeypatch.setattr(
            build_windows_installer,
            "_windows_stage_monitor_result",
            lambda _monitor: (True, 0),
        )
    elif failure == "get-result":
        monkeypatch.setattr(
            build_windows_installer,
            "_wait_windows_stage_monitor",
            lambda _monitor, _timeout: build_windows_installer._WAIT_OBJECT_0,
        )
        monkeypatch.setattr(
            build_windows_installer,
            "_windows_stage_monitor_result",
            lambda _monitor: (False, 5),
        )
    elif failure in {"cancel-hard", "cancel-not-found"}:
        cancel_monitor = getattr(
            build_windows_installer, "_cancel_windows_stage_monitor", None
        )
        assert callable(cancel_monitor), "missing explicit monitor cancellation seam"
        error = (
            build_windows_installer._ERROR_NOT_FOUND
            if failure == "cancel-not-found"
            else 5
        )

        def fail_cancel(_monitor):
            injections.append(failure)
            return False, error

        monkeypatch.setattr(
            build_windows_installer, "_cancel_windows_stage_monitor", fail_cancel
        )
    elif failure == "cancel-timeout":
        real_wait = build_windows_installer._wait_windows_stage_monitor

        def timeout_after_cancel(value, timeout):
            if timeout:
                injections.append(failure)
                return build_windows_installer._WAIT_TIMEOUT
            return real_wait(value, timeout)

        monkeypatch.setattr(
            build_windows_installer, "_wait_windows_stage_monitor", timeout_after_cancel
        )
    elif failure == "entry-close":
        real_close_entry = build_windows_installer._close_windows_entry

        def fail_entry_close(entry):
            result = real_close_entry(entry)
            if entry.role == "stage-monitor":
                injections.append(failure)
                raise OSError("synthetic monitor entry close failure")
            return result

        monkeypatch.setattr(
            build_windows_installer, "_close_windows_entry", fail_entry_close
        )
    else:
        real_close_handle = build_windows_installer._close_windows_handle
        event_key = _handle_key(monitor.event_handle)

        def fail_event_close(handle):
            result = real_close_handle(handle)
            if _handle_key(handle) == event_key:
                injections.append(failure)
                raise OSError("synthetic monitor event close failure")
            return result

        monkeypatch.setattr(
            build_windows_installer, "_close_windows_handle", fail_event_close
        )

    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)
    with pytest.raises(RuntimeError, match=r"^INSTALLER_COMPILER_INPUT_INVALID$"):
        build_windows_installer.finalize_compiled_installer(capability)
    if failure in {
        "cancel-hard",
        "cancel-not-found",
        "cancel-timeout",
        "entry-close",
        "event-close",
    }:
        assert injections


@pytest.mark.skipif(os.name != "nt", reason="Windows raw-handle ownership transfer")
@pytest.mark.parametrize(
    "target",
    [
        "anchor",
        "existing-component",
        "stage-root",
        "metadata-dir",
        "source-dir",
        "nested-dir",
        "snapshot-zip",
        "member-file",
    ],
)
def test_raw_handle_pre_registration_failure_closes_exactly_once_leaf_to_root(
    monkeypatch, tmp_path, target
):
    staged = None
    source = None
    prepare_target = None
    stage_parent = None
    if target in {
        "metadata-dir",
        "source-dir",
        "nested-dir",
        "snapshot-zip",
        "member-file",
    }:
        staged = _stage(monkeypatch, tmp_path / "published")
    elif target == "stage-root":
        source, _document, _members = _valid_zip(tmp_path / "source")
        stage_parent = tmp_path / "stage-parent"
        stage_parent.mkdir(parents=True)
        monkeypatch.setattr(
            build_windows_installer, "read_version", lambda: PACKAGE_VERSION
        )
    else:
        prepare_target = tmp_path / "existing" / "component"
        prepare_target.mkdir(parents=True)

    opened: dict[int, int] = {}
    closed: dict[int, int] = {}
    handles: dict[int, object] = {}
    close_order: list[int] = []
    failed_key: list[int] = []
    failed_path: list[Path] = []
    active_at_failure: list[set[int]] = []
    raw_openers = (
        "_open_windows_anchor_handle",
        "_open_windows_anchor_read_handle",
        "_nt_open_directory_handle",
        "_nt_open_directory_read_handle",
        "_nt_open_file_read_handle",
        "_nt_create_directory_handle",
        "_nt_create_file_handle",
    )

    def count(mapping, key):
        mapping[key] = mapping.get(key, 0) + 1

    for opener_name in raw_openers:
        real_opener = getattr(build_windows_installer, opener_name)

        def make_opener(real_function):
            def open_and_record(*args, **kwargs):
                handle = real_function(*args, **kwargs)
                key = _handle_key(handle)
                count(opened, key)
                handles[key] = handle
                return handle

            return open_and_record

        monkeypatch.setattr(
            build_windows_installer, opener_name, make_opener(real_opener)
        )

    real_close_handle = build_windows_installer._close_windows_handle

    def close_and_record(handle):
        key = _handle_key(handle)
        close_order.append(key)
        count(closed, key)
        return real_close_handle(handle)

    monkeypatch.setattr(
        build_windows_installer, "_close_windows_handle", close_and_record
    )
    real_entry = build_windows_installer._entry_from_handle

    def is_target(role, name):
        return {
            "anchor": role == "anchor",
            "existing-component": role == "ancestor"
            and prepare_target is not None
            and name == prepare_target.name,
            "stage-root": role == "stage-root",
            "metadata-dir": role == "stage-dir:.installer-source",
            "source-dir": role == "stage-dir:PDFVectorImporter",
            "nested-dir": role == "stage-dir:PDFVectorImporter/data",
            "snapshot-zip": role == "stage-file:.installer-source/zip",
            "member-file": role == "stage-file:PDFVectorImporter/module.py",
        }[target]

    def fail_entry(handle, *args, **kwargs):
        role = kwargs["role"]
        name = kwargs["name"]
        if is_target(role, name):
            key = _handle_key(handle)
            failed_key.append(key)
            failed_path.append(Path(kwargs["path"]))
            active_at_failure.append(
                {
                    candidate
                    for candidate, amount in opened.items()
                    if amount > closed.get(candidate, 0)
                }
            )
            raise build_windows_installer._ChangedPath
        return real_entry(handle, *args, **kwargs)

    monkeypatch.setattr(build_windows_installer, "_entry_from_handle", fail_entry)

    caught = None
    try:
        if target in {"anchor", "existing-component"}:
            build_windows_installer._prepare_windows_stage_parent(prepare_target)
        elif target == "stage-root":
            build_windows_installer.stage_release(
                source,
                dist_dir=tmp_path / "dist",
                stage_dir=stage_parent,
            )
        else:
            build_windows_installer._acquire_windows_stage_read_lease(staged)
    except Exception as exc:
        caught = exc

    assert caught is not None
    assert len(failed_key) == 1
    target_key = failed_key[0]
    counts_match = opened == closed
    target_closed_once = closed.get(target_key, 0) == 1
    reverse_order = target_key in close_order and all(
        other == target_key
        or other not in close_order
        or close_order.index(target_key) < close_order.index(other)
        for other in active_at_failure[0]
    )

    rename_proved = True
    original_path = failed_path[0]
    if target != "anchor" and original_path.exists():
        moved_path = original_path.with_name(original_path.name + ".ownership-probe")
        try:
            os.replace(original_path, moved_path)
            os.replace(moved_path, original_path)
        except OSError:
            rename_proved = False

    # A failing implementation can leave a live native handle; release it so
    # the worker-owned pytest directory remains clean even while this RED fails.
    for key, amount in opened.items():
        deficit = amount - closed.get(key, 0)
        for _index in range(max(deficit, 0)):
            try:
                real_close_handle(handles[key])
            except OSError:
                pass

    assert counts_match
    assert target_closed_once
    assert reverse_order
    assert rename_proved
