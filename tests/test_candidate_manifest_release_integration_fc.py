from __future__ import annotations

import builtins
import hashlib
import importlib.util
import io
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import build_release
from scripts import smoke_release_zip


_REPO_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = _REPO_ROOT / "PDFVectorImporter" / "candidate_manifest.py"
_CONTRACT_SPEC = importlib.util.spec_from_file_location(
    "_task5b_candidate_manifest_contract", _CONTRACT_PATH
)
assert _CONTRACT_SPEC is not None and _CONTRACT_SPEC.loader is not None
candidate_manifest = importlib.util.module_from_spec(_CONTRACT_SPEC)
_CONTRACT_SPEC.loader.exec_module(candidate_manifest)

MANIFEST_MEMBER = candidate_manifest.MANIFEST_MEMBER
SOURCE_COMMIT_A = "1" * 40
SOURCE_COMMIT_B = "2" * 40
PACKAGE_VERSION = "9.8.7"
ZIP_NAME = f"FreeCAD-PDF-Importer_v{PACKAGE_VERSION}.zip"


def _configure_synthetic_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    source_commit: str = SOURCE_COMMIT_A,
    source_bytes: bytes = b"SOURCE = 'captured'\n",
    runtime_bytes: bytes = b"captured runtime",
) -> Path:
    addon = tmp_path / "synthetic-addon"
    addon.mkdir(exist_ok=True)
    (addon / "package.xml").write_text(
        f"<package><version>{PACKAGE_VERSION}</version></package>", encoding="utf-8"
    )
    first_party = (
        (
            Path("package.xml"),
            f"<package><version>{PACKAGE_VERSION}</version></package>".encode(),
        ),
        (Path("module.py"), source_bytes),
    )

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")
    monkeypatch.setattr(build_release, "_load_external_private_denylist", lambda: ())
    monkeypatch.setattr(
        build_release,
        "_require_no_external_private_tracked_repository_data",
        lambda _terms: None,
    )
    monkeypatch.setattr(build_release, "_require_no_linked_addon_content", lambda: None)
    monkeypatch.setattr(
        build_release, "_require_no_linked_runtime_content", lambda _runtime: None
    )
    monkeypatch.setattr(build_release, "_require_no_private_artifacts", lambda: None)
    monkeypatch.setattr(
        build_release, "_require_no_private_content", lambda _terms: None
    )
    monkeypatch.setattr(
        build_release, "_capture_first_party_files", lambda: (first_party, 0)
    )
    monkeypatch.setattr(
        build_release,
        "_capture_source_commit",
        lambda: source_commit,
        raising=False,
    )
    monkeypatch.setattr(
        build_release,
        "_require_commit_bound_sources",
        lambda _snapshot, **_kwargs: {},
    )

    def provide_runtime(*, runtime_dir: Path, **_kwargs) -> dict:
        payload = runtime_dir / "common" / "example" / "payload.bin"
        payload.parent.mkdir(parents=True)
        payload.write_bytes(runtime_bytes)
        return {}

    monkeypatch.setattr(build_release, "ensure_runtime_dependencies", provide_runtime)
    return addon


def _read_zip_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def _manifest_from_zip(path: Path) -> tuple[dict, dict[str, bytes]]:
    members = _read_zip_members(path)
    document, problems = candidate_manifest.parse_candidate_file_manifest(
        members[MANIFEST_MEMBER]
    )
    assert problems == []
    assert document is not None
    return document, members


def test_build_embeds_one_canonical_complete_manifest(monkeypatch, tmp_path):
    _configure_synthetic_build(monkeypatch, tmp_path)

    archive_path = build_release.build(tmp_path / "out")
    document, members = _manifest_from_zip(archive_path)

    assert list(members).count(MANIFEST_MEMBER) == 1
    assert members[MANIFEST_MEMBER] == candidate_manifest.canonical_candidate_manifest_bytes(
        document
    )
    assert document["source_commit"] == SOURCE_COMMIT_A
    assert document["package_version"] == PACKAGE_VERSION
    assert document["artifact_name"] == archive_path.name == ZIP_NAME
    assert document["files"] == [
        {
            "path": name.removeprefix("PDFVectorImporter/"),
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        for name, content in sorted(
            ((name, content) for name, content in members.items() if name != MANIFEST_MEMBER),
            key=lambda item: item[0].encode("utf-8"),
        )
    ]
    assert candidate_manifest.validate_candidate_archive_members(document, members) == []


def test_build_is_byte_reproducible_and_manifest_binds_bytes_and_commit(
    monkeypatch, tmp_path
):
    _configure_synthetic_build(monkeypatch, tmp_path)
    first = build_release.build(tmp_path / "first")
    second = build_release.build(tmp_path / "second")
    first_manifest = _read_zip_members(first)[MANIFEST_MEMBER]

    assert first.read_bytes() == second.read_bytes()

    _configure_synthetic_build(
        monkeypatch, tmp_path, source_bytes=b"SOURCE = 'changed'\n"
    )
    changed_source = build_release.build(tmp_path / "changed-source")
    _configure_synthetic_build(
        monkeypatch, tmp_path, runtime_bytes=b"changed runtime"
    )
    changed_runtime = build_release.build(tmp_path / "changed-runtime")
    _configure_synthetic_build(
        monkeypatch, tmp_path, source_commit=SOURCE_COMMIT_B
    )
    changed_commit = build_release.build(tmp_path / "changed-commit")

    changed_manifests = {
        _read_zip_members(path)[MANIFEST_MEMBER]
        for path in (changed_source, changed_runtime, changed_commit)
    }
    assert len(changed_manifests) == 3
    assert first_manifest not in changed_manifests


def test_build_writes_only_captured_source_and_runtime_bytes(monkeypatch, tmp_path):
    addon = _configure_synthetic_build(monkeypatch, tmp_path)
    live_source = addon / "module.py"
    live_source.write_bytes(b"live source before capture")
    original_runtime_capture = build_release._capture_runtime_files

    def capture_then_mutate(runtime_dir: Path, external_terms: tuple[str, ...]):
        captured = original_runtime_capture(runtime_dir, external_terms)
        (runtime_dir / "common" / "example" / "payload.bin").write_bytes(
            b"late runtime mutation"
        )
        live_source.write_bytes(b"late source mutation")
        return captured

    monkeypatch.setattr(build_release, "_capture_runtime_files", capture_then_mutate)
    archive_path = build_release.build(tmp_path / "out")
    document, members = _manifest_from_zip(archive_path)

    assert members["PDFVectorImporter/module.py"] == b"SOURCE = 'captured'\n"
    assert members[
        "PDFVectorImporter/src/lib/common/example/payload.bin"
    ] == b"captured runtime"
    assert candidate_manifest.validate_candidate_archive_members(document, members) == []


def test_commit_binding_uses_only_the_explicit_captured_oid(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    package_bytes = b"<package><version>9.8.7</version></package>"
    (addon / "package.xml").write_bytes(package_bytes)
    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    monkeypatch.setattr(build_release, "_git_blob_oid", lambda _content: "a" * 40)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        command = [str(value) for value in command]
        commands.append(command)
        if "ls-tree" in command:
            stdout = (
                b"100644 blob "
                + b"a" * 40
                + b"\tPDFVectorImporter/package.xml\0"
            )
        elif "rev-parse" in command:
            stdout = (SOURCE_COMMIT_A + "\n").encode("ascii")
        else:
            stdout = b""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(build_release.subprocess, "run", fake_run)
    build_release._require_commit_bound_sources(
        ((Path("package.xml"), package_bytes),), source_commit=SOURCE_COMMIT_A
    )

    tree_and_index = [
        command
        for command in commands
        if "ls-tree" in command or ("diff" in command and "--cached" in command)
    ]
    assert tree_and_index
    assert all(SOURCE_COMMIT_A in command for command in tree_and_index)
    assert all("HEAD" not in command and "HEAD^{commit}" not in command for command in commands)


@pytest.mark.parametrize("bad_oid", ["A" * 40, "1" * 39, "g" * 40])
def test_build_rejects_malformed_source_oid_before_opening_zip(
    monkeypatch, tmp_path, bad_oid
):
    _configure_synthetic_build(monkeypatch, tmp_path, source_commit=bad_oid)
    final_path = tmp_path / "out" / ZIP_NAME

    with pytest.raises(RuntimeError, match="commit"):
        build_release.build(tmp_path / "out")

    assert not final_path.exists()


def test_build_rejects_head_move_before_opening_or_replacing_zip(monkeypatch, tmp_path):
    _configure_synthetic_build(monkeypatch, tmp_path)
    commits = iter((SOURCE_COMMIT_A, SOURCE_COMMIT_B))
    monkeypatch.setattr(build_release, "_capture_source_commit", lambda: next(commits))
    final_path = tmp_path / "out" / ZIP_NAME
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"preexisting final")

    with pytest.raises(RuntimeError, match="commit"):
        build_release.build(tmp_path / "out")

    assert final_path.read_bytes() == b"preexisting final"
    assert list(final_path.parent.glob(f".{ZIP_NAME}.*.tmp")) == []


@pytest.mark.parametrize(
    "extra_paths",
    [
        (Path("../escape.py"),),
        (Path("private-drawing.pdf"),),
        (Path("Alpha.py"), Path("alpha.py")),
        (Path("caf\N{LATIN SMALL LETTER E WITH ACUTE}.py"), Path("cafe\N{COMBINING ACUTE ACCENT}.py")),
    ],
    ids=["parent", "private", "case-alias", "nfc-alias"],
)
def test_builder_rejects_unsafe_or_aliased_composition_without_touching_final(
    monkeypatch, tmp_path, extra_paths
):
    _configure_synthetic_build(monkeypatch, tmp_path)
    package = (
        Path("package.xml"),
        f"<package><version>{PACKAGE_VERSION}</version></package>".encode(),
    )
    snapshot = (package, *((path, b"synthetic") for path in extra_paths))
    monkeypatch.setattr(build_release, "_capture_first_party_files", lambda: (snapshot, 0))
    final_path = tmp_path / "out" / ZIP_NAME
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"preexisting final")

    with pytest.raises(RuntimeError):
        build_release.build(tmp_path / "out")

    assert final_path.read_bytes() == b"preexisting final"
    assert list(final_path.parent.glob(f".{ZIP_NAME}.*.tmp")) == []


@pytest.mark.parametrize("failure_stage", ["write", "reopen", "replace"])
def test_atomic_publish_preserves_existing_final_and_cleans_owned_temp(
    monkeypatch, tmp_path, failure_stage
):
    _configure_synthetic_build(monkeypatch, tmp_path)
    final_path = tmp_path / "out" / ZIP_NAME
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"known-good-final")

    if failure_stage == "write":
        monkeypatch.setattr(
            build_release,
            "_write_deterministic_bytes",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("write failed")),
        )
    elif failure_stage == "reopen":
        monkeypatch.setattr(
            build_release,
            "_validate_written_release_zip",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("reopen failed")),
            raising=False,
        )
    else:
        monkeypatch.setattr(
            build_release.os,
            "replace",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("replace failed")),
        )

    with pytest.raises(RuntimeError):
        build_release.build(tmp_path / "out")

    assert final_path.read_bytes() == b"known-good-final"
    assert list(final_path.parent.glob(f".{ZIP_NAME}.*.tmp")) == []


@pytest.mark.parametrize("drift", ["missing", "extra", "changed"])
def test_postwrite_byte_drift_blocks_publish_and_preserves_final(
    monkeypatch, tmp_path, drift
):
    _configure_synthetic_build(monkeypatch, tmp_path)
    validator = getattr(build_release, "_validate_written_release_zip", None)
    assert callable(validator)

    def tamper_then_validate(path: Path, expected_members: dict[str, bytes], document: dict):
        members = _read_zip_members(path)
        if drift == "missing":
            members.pop("PDFVectorImporter/module.py")
        elif drift == "extra":
            members["PDFVectorImporter/extra.py"] = b"extra"
        else:
            members["PDFVectorImporter/module.py"] = b"changed"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in members.items():
                archive.writestr(name, content)
        return validator(path, expected_members, document)

    monkeypatch.setattr(build_release, "_validate_written_release_zip", tamper_then_validate)
    final_path = tmp_path / "out" / ZIP_NAME
    final_path.parent.mkdir(parents=True)
    final_path.write_bytes(b"known-good-final")

    with pytest.raises(RuntimeError):
        build_release.build(tmp_path / "out")

    assert final_path.read_bytes() == b"known-good-final"
    assert list(final_path.parent.glob(f".{ZIP_NAME}.*.tmp")) == []


def _regular_info(
    name: str,
    *,
    mode: int = stat.S_IFREG | 0o644,
    compression: int = zipfile.ZIP_STORED,
) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def _write_dos_directory_manifest_zip(
    path: Path,
) -> tuple[Path, dict[str, bytes], dict]:
    path = path / ZIP_NAME
    members = {
        "PDFVectorImporter/payload.bin": b"alpha",
        "PDFVectorImporter/dos-directory": b"",
    }
    document = candidate_manifest.build_candidate_file_manifest(
        members,
        source_commit=SOURCE_COMMIT_A,
        package_version=PACKAGE_VERSION,
        artifact_name=ZIP_NAME,
    )
    archive_members = {
        **members,
        MANIFEST_MEMBER: candidate_manifest.canonical_candidate_manifest_bytes(
            document
        ),
    }
    dos_directory = zipfile.ZipInfo(
        "PDFVectorImporter/dos-directory", date_time=(1980, 1, 1, 0, 0, 0)
    )
    dos_directory.compress_type = zipfile.ZIP_STORED
    dos_directory.create_system = 0
    dos_directory.external_attr = 0x10
    path.parent.mkdir(parents=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            _regular_info("PDFVectorImporter/payload.bin"),
            members["PDFVectorImporter/payload.bin"],
        )
        archive.writestr(dos_directory, members["PDFVectorImporter/dos-directory"])
        archive.writestr(_regular_info(MANIFEST_MEMBER), archive_members[MANIFEST_MEMBER])
    return path, archive_members, document


def _write_manifest_zip(
    path: Path,
    *,
    payload: bytes = b"alpha",
    artifact_name: str | None = None,
    manifest_payload: bytes | None = None,
    include_payload: bool = True,
    extras: tuple[tuple[zipfile.ZipInfo, bytes], ...] = (),
) -> Path:
    path = path.parent / path.stem / ZIP_NAME
    member_name = "PDFVectorImporter/payload.bin"
    document = candidate_manifest.build_candidate_file_manifest(
        {member_name: payload},
        source_commit=SOURCE_COMMIT_A,
        package_version=PACKAGE_VERSION,
        artifact_name=artifact_name or ZIP_NAME,
    )
    canonical = candidate_manifest.canonical_candidate_manifest_bytes(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
        if include_payload:
            archive.writestr(_regular_info(member_name), payload)
        archive.writestr(
            _regular_info(MANIFEST_MEMBER),
            canonical if manifest_payload is None else manifest_payload,
        )
        for info, content in extras:
            archive.writestr(info, content)
    return path


def test_smoke_accepts_minimal_exact_manifest_zip(tmp_path):
    path = _write_manifest_zip(tmp_path / "minimal.zip")
    assert smoke_release_zip.validate_release_zip_manifest(path) == []


def test_smoke_rejects_missing_and_duplicate_manifest_or_member(tmp_path):
    missing = tmp_path / "missing.zip"
    with zipfile.ZipFile(missing, "w") as archive:
        archive.writestr(_regular_info("PDFVectorImporter/payload.bin"), b"alpha")
    assert smoke_release_zip.validate_release_zip_manifest(missing) == [
        "RELEASE_ZIP_MANIFEST_MISSING"
    ]

    duplicate_manifest = _write_manifest_zip(tmp_path / "duplicate-manifest.zip")
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(duplicate_manifest, "a") as archive:
            archive.writestr(_regular_info(MANIFEST_MEMBER), b"duplicate")
    assert smoke_release_zip.validate_release_zip_manifest(duplicate_manifest) == [
        "RELEASE_ZIP_DUPLICATE_MEMBER",
        "RELEASE_ZIP_MANIFEST_DUPLICATE",
    ]

    duplicate_member = _write_manifest_zip(tmp_path / "duplicate-member.zip")
    with pytest.warns(UserWarning):
        with zipfile.ZipFile(duplicate_member, "a") as archive:
            archive.writestr(_regular_info("PDFVectorImporter/payload.bin"), b"alpha")
    assert smoke_release_zip.validate_release_zip_manifest(duplicate_member) == [
        "RELEASE_ZIP_DUPLICATE_MEMBER"
    ]


@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("PDFVectorImporter/PAYLOAD.bin", ["RELEASE_ZIP_MEMBER_ALIAS"]),
        (
            "PDFVectorImporter/cafe\N{COMBINING ACUTE ACCENT}.bin",
            ["RELEASE_ZIP_MEMBER_ALIAS"],
        ),
    ],
)
def test_smoke_rejects_case_and_nfc_aliases(tmp_path, alias, expected):
    if "cafe" in alias:
        canonical = "PDFVectorImporter/caf\N{LATIN SMALL LETTER E WITH ACUTE}.bin"
        extras = (
            (_regular_info(canonical), b"one"),
            (_regular_info(alias), b"two"),
        )
    else:
        extras = ((_regular_info(alias), b"alias"),)
    path = _write_manifest_zip(tmp_path / "alias.zip", extras=extras)
    assert smoke_release_zip.validate_release_zip_manifest(path) == expected


def test_smoke_rejects_noncanonical_and_mismatched_manifest_bytes(tmp_path):
    canonical_path = _write_manifest_zip(tmp_path / "source.zip")
    canonical = _read_zip_members(canonical_path)[MANIFEST_MEMBER]
    noncanonical = _write_manifest_zip(
        tmp_path / "noncanonical.zip", manifest_payload=canonical + b"\n"
    )
    assert smoke_release_zip.validate_release_zip_manifest(noncanonical) == [
        "MANIFEST_NONCANONICAL_BYTES"
    ]

    other_doc = candidate_manifest.build_candidate_file_manifest(
        {"PDFVectorImporter/payload.bin": b"bravo"},
        source_commit=SOURCE_COMMIT_A,
        package_version=PACKAGE_VERSION,
        artifact_name=ZIP_NAME,
    )
    mismatched = _write_manifest_zip(
        tmp_path / "mismatched.zip",
        manifest_payload=candidate_manifest.canonical_candidate_manifest_bytes(other_doc),
    )
    assert smoke_release_zip.validate_release_zip_manifest(mismatched) == [
        "MANIFEST_MEMBER_DIGEST_MISMATCH"
    ]


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("missing", ["MANIFEST_MEMBER_SET_MISMATCH"]),
        ("extra", ["MANIFEST_MEMBER_SET_MISMATCH"]),
        ("renamed", ["MANIFEST_MEMBER_SET_MISMATCH"]),
        ("changed", ["MANIFEST_MEMBER_DIGEST_MISMATCH"]),
    ],
)
def test_smoke_rejects_member_set_and_byte_drift(tmp_path, kind, expected):
    if kind == "missing":
        path = _write_manifest_zip(tmp_path / "drift.zip", include_payload=False)
    elif kind == "extra":
        path = _write_manifest_zip(
            tmp_path / "drift.zip",
            extras=((_regular_info("PDFVectorImporter/extra.bin"), b"extra"),),
        )
    elif kind == "renamed":
        path = _write_manifest_zip(
            tmp_path / "drift.zip",
            include_payload=False,
            extras=((_regular_info("PDFVectorImporter/renamed.bin"), b"alpha"),),
        )
    else:
        source = _write_manifest_zip(tmp_path / "source-drift.zip")
        manifest_payload = _read_zip_members(source)[MANIFEST_MEMBER]
        path = _write_manifest_zip(
            tmp_path / "drift.zip", payload=b"omega", manifest_payload=manifest_payload
        )
    assert smoke_release_zip.validate_release_zip_manifest(path) == expected


@pytest.mark.parametrize(
    "mode",
    [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFSOCK, stat.S_IFCHR, stat.S_IFBLK],
    ids=["symlink", "fifo", "socket", "char-device", "block-device"],
)
def test_smoke_rejects_directories_and_posix_nonregular_members(tmp_path, mode):
    path = _write_manifest_zip(
        tmp_path / f"mode-{mode}.zip",
        extras=((_regular_info("PDFVectorImporter/nonregular", mode=mode | 0o644), b"x"),),
    )
    assert smoke_release_zip.validate_release_zip_manifest(path) == [
        "RELEASE_ZIP_NONREGULAR_MEMBER"
    ]

    directory = _write_manifest_zip(
        tmp_path / f"directory-{mode}.zip",
        extras=((_regular_info("PDFVectorImporter/directory/", mode=stat.S_IFDIR | 0o755), b""),),
    )
    assert smoke_release_zip.validate_release_zip_manifest(directory) == [
        "RELEASE_ZIP_NONREGULAR_MEMBER"
    ]


def test_smoke_rejects_dos_directory_attribute_without_trailing_slash(tmp_path):
    path, _members, _document = _write_dos_directory_manifest_zip(
        tmp_path / "smoke-dos-directory"
    )

    assert smoke_release_zip.validate_release_zip_manifest(path) == [
        "RELEASE_ZIP_NONREGULAR_MEMBER"
    ]


def test_builder_reopen_rejects_dos_directory_attribute_without_trailing_slash(
    tmp_path,
):
    path, members, document = _write_dos_directory_manifest_zip(
        tmp_path / "builder-dos-directory"
    )

    with pytest.raises(RuntimeError, match="non-file members"):
        build_release._validate_written_release_zip(path, members, document)


def _mark_first_entry_encrypted(path: Path) -> None:
    payload = bytearray(path.read_bytes())
    local = payload.index(b"PK\x03\x04")
    central = payload.index(b"PK\x01\x02")
    payload[local + 6 : local + 8] = (
        int.from_bytes(payload[local + 6 : local + 8], "little") | 1
    ).to_bytes(2, "little")
    payload[central + 8 : central + 10] = (
        int.from_bytes(payload[central + 8 : central + 10], "little") | 1
    ).to_bytes(2, "little")
    path.write_bytes(payload)


def test_smoke_rejects_encryption_unsafe_paths_and_compression(tmp_path):
    encrypted = _write_manifest_zip(tmp_path / "encrypted.zip")
    _mark_first_entry_encrypted(encrypted)
    assert smoke_release_zip.validate_release_zip_manifest(encrypted) == [
        "RELEASE_ZIP_ENCRYPTED_MEMBER"
    ]

    unsafe = _write_manifest_zip(
        tmp_path / "unsafe.zip",
        extras=((_regular_info("../private-source-name.txt"), b"private text"),),
    )
    assert smoke_release_zip.validate_release_zip_manifest(unsafe) == [
        "RELEASE_ZIP_UNSAFE_MEMBER"
    ]

    compressed = _write_manifest_zip(
        tmp_path / "compressed.zip",
        extras=((_regular_info("PDFVectorImporter/compressed.bin", compression=zipfile.ZIP_DEFLATED), b"x"),),
    )
    assert smoke_release_zip.validate_release_zip_manifest(compressed) == [
        "RELEASE_ZIP_UNSUPPORTED_COMPRESSION"
    ]


def test_smoke_never_raises_and_reports_corrupt_io_or_artifact_name(tmp_path):
    corrupt = tmp_path / "corrupt.zip"
    corrupt.write_bytes(b"not a zip; account-name private-filename private-text")
    assert smoke_release_zip.validate_release_zip_manifest(corrupt) == [
        "RELEASE_ZIP_CORRUPT"
    ]
    assert smoke_release_zip.validate_release_zip_manifest(tmp_path) == [
        "RELEASE_ZIP_IO_ERROR"
    ]
    mismatch = _write_manifest_zip(tmp_path / "mismatch-source.zip")
    renamed_mismatch = tmp_path / "actual.zip"
    mismatch.replace(renamed_mismatch)
    mismatch = renamed_mismatch
    assert smoke_release_zip.validate_release_zip_manifest(mismatch) == [
        "RELEASE_ZIP_ARTIFACT_NAME_MISMATCH"
    ]


class _BytesSubclass(bytes):
    pass


class _StringSubclass(str):
    pass


class _HostileProtocolValue:
    def __init__(self) -> None:
        self.touched = False

    def _reject(self):
        self.touched = True
        raise RuntimeError("SENSITIVE_PROTOCOL_SENTINEL")

    def __bytes__(self):
        return self._reject()

    def __fspath__(self):
        return self._reject()

    def __iter__(self):
        return self._reject()

    def __repr__(self):
        return self._reject()

    def __str__(self):
        return self._reject()

    def read(self, *_args, **_kwargs):
        return self._reject()


def _validate_zip_bytes(payload: bytes, *, artifact_name: str = ZIP_NAME) -> list[str]:
    return smoke_release_zip.validate_release_zip_manifest_bytes(
        payload, artifact_name=artifact_name
    )


def test_smoke_exposes_immutable_release_zip_byte_validator(tmp_path):
    validator = getattr(smoke_release_zip, "validate_release_zip_manifest_bytes", None)
    assert callable(validator), "missing immutable release ZIP byte validator"
    path = _write_manifest_zip(tmp_path / "immutable-api.zip")

    assert validator(path.read_bytes(), artifact_name=path.name) == []
    assert validator(path.read_bytes(), artifact_name="different.zip") == [
        "RELEASE_ZIP_ARTIFACT_NAME_MISMATCH"
    ]


def _write_smoke_mutation(tmp_path: Path, kind: str) -> tuple[Path, list[str]]:
    if kind == "valid":
        return _write_manifest_zip(tmp_path / "valid.zip"), []
    if kind == "missing-manifest":
        path = tmp_path / ZIP_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(_regular_info("PDFVectorImporter/payload.bin"), b"alpha")
        return path, ["RELEASE_ZIP_MANIFEST_MISSING"]
    if kind in {"duplicate-manifest", "duplicate-member"}:
        path = _write_manifest_zip(tmp_path / f"{kind}.zip")
        member = MANIFEST_MEMBER if kind == "duplicate-manifest" else "PDFVectorImporter/payload.bin"
        content = b"duplicate" if kind == "duplicate-manifest" else b"alpha"
        with pytest.warns(UserWarning):
            with zipfile.ZipFile(path, "a", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr(_regular_info(member), content)
        expected = ["RELEASE_ZIP_DUPLICATE_MEMBER"]
        if kind == "duplicate-manifest":
            expected.append("RELEASE_ZIP_MANIFEST_DUPLICATE")
        return path, expected
    if kind in {"case-alias", "nfc-alias"}:
        if kind == "case-alias":
            extras = ((_regular_info("PDFVectorImporter/PAYLOAD.bin"), b"alias"),)
        else:
            extras = (
                (_regular_info("PDFVectorImporter/caf\N{LATIN SMALL LETTER E WITH ACUTE}.bin"), b"one"),
                (_regular_info("PDFVectorImporter/cafe\N{COMBINING ACUTE ACCENT}.bin"), b"two"),
            )
        return _write_manifest_zip(tmp_path / f"{kind}.zip", extras=extras), [
            "RELEASE_ZIP_MEMBER_ALIAS"
        ]
    if kind == "noncanonical-manifest":
        source = _write_manifest_zip(tmp_path / "canonical-source.zip")
        canonical = _read_zip_members(source)[MANIFEST_MEMBER]
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip", manifest_payload=canonical + b"\n"
        ), ["MANIFEST_NONCANONICAL_BYTES"]
    if kind == "mismatched-manifest":
        document = candidate_manifest.build_candidate_file_manifest(
            {"PDFVectorImporter/payload.bin": b"bravo"},
            source_commit=SOURCE_COMMIT_A,
            package_version=PACKAGE_VERSION,
            artifact_name=ZIP_NAME,
        )
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip",
            manifest_payload=candidate_manifest.canonical_candidate_manifest_bytes(document),
        ), ["MANIFEST_MEMBER_DIGEST_MISMATCH"]
    if kind == "missing-member":
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip", include_payload=False
        ), ["MANIFEST_MEMBER_SET_MISMATCH"]
    if kind == "extra-member":
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip",
            extras=((_regular_info("PDFVectorImporter/extra.bin"), b"extra"),),
        ), ["MANIFEST_MEMBER_SET_MISMATCH"]
    if kind == "renamed-member":
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip",
            include_payload=False,
            extras=((_regular_info("PDFVectorImporter/renamed.bin"), b"alpha"),),
        ), ["MANIFEST_MEMBER_SET_MISMATCH"]
    if kind == "changed-member":
        source = _write_manifest_zip(tmp_path / "changed-source.zip")
        canonical = _read_zip_members(source)[MANIFEST_MEMBER]
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip", payload=b"omega", manifest_payload=canonical
        ), ["MANIFEST_MEMBER_DIGEST_MISMATCH"]
    if kind == "posix-directory":
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip",
            extras=((_regular_info("PDFVectorImporter/directory/", mode=stat.S_IFDIR | 0o755), b""),),
        ), ["RELEASE_ZIP_NONREGULAR_MEMBER"]
    if kind == "posix-nonregular":
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip",
            extras=((_regular_info("PDFVectorImporter/link", mode=stat.S_IFLNK | 0o644), b"x"),),
        ), ["RELEASE_ZIP_NONREGULAR_MEMBER"]
    if kind == "dos-directory":
        path, _members, _document = _write_dos_directory_manifest_zip(tmp_path / kind)
        return path, ["RELEASE_ZIP_NONREGULAR_MEMBER"]
    if kind == "encrypted":
        path = _write_manifest_zip(tmp_path / f"{kind}.zip")
        _mark_first_entry_encrypted(path)
        return path, ["RELEASE_ZIP_ENCRYPTED_MEMBER"]
    if kind == "unsafe":
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip",
            extras=((_regular_info("../unsafe.txt"), b"unsafe"),),
        ), ["RELEASE_ZIP_UNSAFE_MEMBER"]
    if kind == "compressed":
        return _write_manifest_zip(
            tmp_path / f"{kind}.zip",
            extras=((_regular_info("PDFVectorImporter/compressed.bin", compression=zipfile.ZIP_DEFLATED), b"x"),),
        ), ["RELEASE_ZIP_UNSUPPORTED_COMPRESSION"]
    if kind == "artifact-mismatch":
        path = _write_manifest_zip(tmp_path / "artifact-source.zip")
        renamed = path.with_name("different.zip")
        path.replace(renamed)
        return renamed, ["RELEASE_ZIP_ARTIFACT_NAME_MISMATCH"]
    raise AssertionError(f"unknown synthetic mutation: {kind}")


@pytest.mark.parametrize(
    "kind",
    [
        "valid",
        "missing-manifest",
        "duplicate-manifest",
        "duplicate-member",
        "case-alias",
        "nfc-alias",
        "noncanonical-manifest",
        "mismatched-manifest",
        "missing-member",
        "extra-member",
        "renamed-member",
        "changed-member",
        "posix-directory",
        "posix-nonregular",
        "dos-directory",
        "encrypted",
        "unsafe",
        "compressed",
        "artifact-mismatch",
    ],
)
def test_byte_and_path_apis_have_exact_structural_mutation_parity(tmp_path, kind):
    path, expected = _write_smoke_mutation(tmp_path / kind, kind)
    captured = path.read_bytes()

    assert smoke_release_zip.validate_release_zip_manifest(path) == expected
    assert _validate_zip_bytes(captured, artifact_name=path.name) == expected


def _corrupt_zip_bytes(valid: bytes, kind: str) -> bytes:
    if kind == "empty":
        return b""
    if kind == "random":
        return b"synthetic non-zip bytes"
    payload = bytearray(valid)
    if kind == "truncated-central-directory":
        return bytes(payload[:-12])
    if kind == "malformed-local-record":
        offset = payload.index(b"PK\x03\x04")
        payload[offset : offset + 4] = b"BAD!"
        return bytes(payload)
    if kind == "malformed-central-record":
        offset = payload.index(b"PK\x01\x02")
        payload[offset : offset + 4] = b"BAD!"
        return bytes(payload)
    if kind == "crc-mismatch":
        offset = payload.index(b"alpha")
        payload[offset] ^= 0x01
        return bytes(payload)
    raise AssertionError(f"unknown corrupt mutation: {kind}")


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "random",
        "truncated-central-directory",
        "malformed-local-record",
        "malformed-central-record",
        "crc-mismatch",
    ],
)
def test_byte_api_maps_each_malformed_zip_exactly_to_corrupt(tmp_path, kind):
    path = _write_manifest_zip(tmp_path / "corrupt-source.zip")
    payload = _corrupt_zip_bytes(path.read_bytes(), kind)

    assert _validate_zip_bytes(payload) == ["RELEASE_ZIP_CORRUPT"]


@pytest.mark.parametrize("stage", ["open", "info", "read"])
@pytest.mark.parametrize("error_type", [OSError, ValueError, RuntimeError, EOFError])
def test_byte_api_maps_in_memory_boundary_failures_exactly_to_io_error(
    monkeypatch, tmp_path, stage, error_type
):
    path = _write_manifest_zip(tmp_path / "io-source.zip")
    payload = path.read_bytes()
    real_zipfile = zipfile.ZipFile

    class FaultingArchive:
        def __init__(self, source, mode="r", *args, **kwargs):
            if stage == "open":
                raise error_type("SENSITIVE_PROTOCOL_SENTINEL")
            self._inner = real_zipfile(source, mode, *args, **kwargs)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, *args):
            return self._inner.__exit__(*args)

        def infolist(self):
            if stage == "info":
                raise error_type("SENSITIVE_PROTOCOL_SENTINEL")
            return self._inner.infolist()

        def read(self, *_args, **_kwargs):
            if stage == "read":
                raise error_type("SENSITIVE_PROTOCOL_SENTINEL")
            return self._inner.read(*_args, **_kwargs)

    monkeypatch.setattr(smoke_release_zip.zipfile, "ZipFile", FaultingArchive)

    assert _validate_zip_bytes(payload) == ["RELEASE_ZIP_IO_ERROR"]


@pytest.mark.parametrize(
    "payload_factory",
    [
        pytest.param(lambda value: bytearray(value), id="bytearray"),
        pytest.param(lambda value: memoryview(value), id="memoryview"),
        pytest.param(lambda value: _BytesSubclass(value), id="bytes-subclass"),
    ],
)
def test_byte_api_rejects_non_builtin_bytes_without_conversion(tmp_path, payload_factory):
    path = _write_manifest_zip(tmp_path / "byte-type-source.zip")

    assert _validate_zip_bytes(payload_factory(path.read_bytes())) == [
        "RELEASE_ZIP_IO_ERROR"
    ]


def test_byte_api_rejects_hostile_non_bytes_without_touching_protocols():
    hostile = _HostileProtocolValue()

    assert _validate_zip_bytes(hostile) == ["RELEASE_ZIP_IO_ERROR"]
    assert hostile.touched is False


_INVALID_ARTIFACT_NAMES = [
    pytest.param("", id="empty"),
    pytest.param(".", id="dot"),
    pytest.param("..", id="dot-dot"),
    pytest.param("folder/name.zip", id="slash"),
    pytest.param("folder\\name.zip", id="backslash"),
    pytest.param("C:name.zip", id="colon-drive-ads"),
    pytest.param("bad<name.zip", id="less-than"),
    pytest.param("bad>name.zip", id="greater-than"),
    pytest.param('bad"name.zip', id="quote"),
    pytest.param("bad|name.zip", id="pipe"),
    pytest.param("bad?name.zip", id="question"),
    pytest.param("bad*name.zip", id="asterisk"),
    pytest.param("bad\x00name.zip", id="c0-nul"),
    pytest.param("bad\x1fname.zip", id="c0-unit-separator"),
    pytest.param("bad\x7fname.zip", id="del"),
    pytest.param("cafe\N{COMBINING ACUTE ACCENT}.zip", id="decomposed-unicode"),
    pytest.param("bad-name.", id="trailing-dot"),
    pytest.param("bad-name ", id="trailing-space"),
    pytest.param("\ud800.zip", id="non-utf8-surrogate"),
    pytest.param("CON.zip", id="con-extension"),
    pytest.param("prn.txt", id="prn-extension"),
    pytest.param("AUX", id="aux"),
    pytest.param("nul.any", id="nul-extension"),
    pytest.param("CONIN$.zip", id="conin-extension"),
    pytest.param("conout$.txt", id="conout-extension"),
    *[
        pytest.param(f"COM{index}.zip", id=f"com{index}-extension")
        for index in range(1, 10)
    ],
    *[
        pytest.param(f"LPT{index}.zip", id=f"lpt{index}-extension")
        for index in range(1, 10)
    ],
    pytest.param("COM\N{SUPERSCRIPT ONE}.zip", id="com-superscript-one"),
    pytest.param("com\N{SUPERSCRIPT TWO}.txt", id="com-superscript-two"),
    pytest.param("COM\N{SUPERSCRIPT THREE}.zip", id="com-superscript-three"),
    pytest.param("LPT\N{SUPERSCRIPT ONE}.zip", id="lpt-superscript-one"),
    pytest.param("lpt\N{SUPERSCRIPT TWO}.txt", id="lpt-superscript-two"),
    pytest.param("LPT\N{SUPERSCRIPT THREE}.zip", id="lpt-superscript-three"),
]


@pytest.mark.parametrize("artifact_name", _INVALID_ARTIFACT_NAMES)
def test_invalid_artifact_name_has_precedence_without_touching_zip(artifact_name):
    hostile_zip = _HostileProtocolValue()

    assert _validate_zip_bytes(hostile_zip, artifact_name=artifact_name) == [
        "RELEASE_ZIP_ARTIFACT_NAME_MISMATCH"
    ]
    assert hostile_zip.touched is False


def test_artifact_name_rejects_subclass_and_hostile_object_without_protocols():
    subclass = _StringSubclass(ZIP_NAME)
    hostile = _HostileProtocolValue()

    assert _validate_zip_bytes(b"not a zip", artifact_name=subclass) == [
        "RELEASE_ZIP_ARTIFACT_NAME_MISMATCH"
    ]
    assert _validate_zip_bytes(b"not a zip", artifact_name=hostile) == [
        "RELEASE_ZIP_ARTIFACT_NAME_MISMATCH"
    ]
    assert hostile.touched is False


def test_path_api_captures_once_and_delegates_exact_bytes_and_basename(
    monkeypatch, tmp_path
):
    first = _write_manifest_zip(tmp_path / "first-candidate.zip")
    second = _write_manifest_zip(tmp_path / "second-candidate.zip", payload=b"omega")
    first_bytes = first.read_bytes()
    second_bytes = second.read_bytes()
    original_read_bytes = Path.read_bytes
    original_write_bytes = Path.write_bytes
    reads = 0
    delegated: list[tuple[bytes, str]] = []

    def capture_then_replace(path: Path) -> bytes:
        nonlocal reads
        if path == first:
            reads += 1
            captured = original_read_bytes(path)
            original_write_bytes(path, second_bytes)
            return captured
        return original_read_bytes(path)

    def fake_byte_validator(payload: bytes, *, artifact_name: str) -> list[str]:
        delegated.append((payload, artifact_name))
        original_write_bytes(first, first_bytes)
        return ["DELEGATED_SENTINEL"]

    monkeypatch.setattr(Path, "read_bytes", capture_then_replace)
    monkeypatch.setattr(
        smoke_release_zip,
        "validate_release_zip_manifest_bytes",
        fake_byte_validator,
        raising=False,
    )

    assert smoke_release_zip.validate_release_zip_manifest(first) == [
        "DELEGATED_SENTINEL"
    ]
    assert reads == 1
    assert len(delegated) == 1
    assert type(delegated[0][0]) is bytes
    assert delegated == [(first_bytes, first.name)]
    assert original_read_bytes(first) == first_bytes


def test_byte_api_uses_only_bytesio_and_no_filesystem_or_temp_seam(
    monkeypatch, tmp_path
):
    path = _write_manifest_zip(tmp_path / "memory-only.zip")
    payload = path.read_bytes()
    real_zipfile = zipfile.ZipFile
    opened: list[bytes] = []

    def instrumented_zipfile(source, mode="r", *args, **kwargs):
        assert type(source) is io.BytesIO
        assert mode == "r"
        value = source.getvalue()
        assert type(value) is bytes
        opened.append(value)
        return real_zipfile(source, mode, *args, **kwargs)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("byte validator touched a filesystem or temp seam")

    monkeypatch.setattr(smoke_release_zip.zipfile, "ZipFile", instrumented_zipfile)
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(smoke_release_zip.tempfile, "NamedTemporaryFile", forbidden)
    monkeypatch.setattr(smoke_release_zip.tempfile, "TemporaryFile", forbidden)
    monkeypatch.setattr(smoke_release_zip.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(smoke_release_zip.tempfile, "mkstemp", forbidden)

    assert _validate_zip_bytes(payload) == []
    assert opened == [payload]


def test_main_rejects_old_required_member_zip_before_extraction_or_import(
    monkeypatch, tmp_path, capsys
):
    old_zip = tmp_path / "old-required-only.zip"
    with zipfile.ZipFile(old_zip, "w") as archive:
        for member in smoke_release_zip.REQUIRED_MEMBERS:
            content = (
                b'{"runtimes":{"cp310":{},"cp311":{}}}'
                if member.endswith("runtime-manifest.json")
                else b"synthetic"
            )
            archive.writestr(_regular_info(member), content)
        for member in smoke_release_zip.REQUIRED_WINDOWS_RUNTIME:
            archive.writestr(_regular_info(member), b"synthetic")
    monkeypatch.setattr(sys, "argv", ["smoke_release_zip.py", str(old_zip)])
    monkeypatch.setattr(
        smoke_release_zip,
        "_smoke_runtime",
        lambda *_args: (_ for _ in ()).throw(AssertionError("runtime executed")),
    )

    assert smoke_release_zip.main() == 1
    output = capsys.readouterr().out
    assert old_zip.name in output
    assert "RELEASE_ZIP_MANIFEST_MISSING" in output
    assert str(tmp_path) not in output


def test_loading_builder_and_smoke_contract_does_not_execute_addon_initializer():
    code = f"""
import importlib.util
import pathlib
import sys
before = tuple(sys.path)
for name, path in (
    ('isolated_builder', pathlib.Path({str(_REPO_ROOT / 'build_release.py')!r})),
    ('isolated_smoke', pathlib.Path({str(_REPO_ROOT / 'scripts' / 'smoke_release_zip.py')!r})),
):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
assert 'PDFVectorImporter' not in sys.modules
assert tuple(sys.path) == before
"""
    proc = subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
