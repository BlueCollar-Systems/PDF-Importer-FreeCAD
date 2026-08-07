from __future__ import annotations

import copy
import hashlib
import itertools
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from PDFVectorImporter import candidate_manifest
from PDFVectorImporter.candidate_manifest import (
    CONTRACT_VERSION,
    MANIFEST_MEMBER,
    MANIFEST_SCHEMA,
    PAYLOAD_ROOT,
    PRODUCT_REPOSITORY,
    build_candidate_file_manifest,
    candidate_manifest_sha256,
    canonical_candidate_manifest_bytes,
    parse_candidate_file_manifest,
    validate_candidate_archive_members,
    validate_candidate_file_manifest,
    validate_installed_candidate_tree,
)


SOURCE_COMMIT = "a" * 40
PACKAGE_VERSION = "7.8.9"
ARTIFACT_NAME = f"FreeCAD-PDF-Importer_v{PACKAGE_VERSION}.zip"
ERROR_CODES = {
    "MANIFEST_INVALID_DOCUMENT",
    "MANIFEST_INVALID_IDENTITY",
    "MANIFEST_INVALID_UTF8",
    "MANIFEST_DUPLICATE_KEY",
    "MANIFEST_INVALID_JSON",
    "MANIFEST_NONFINITE_NUMBER",
    "MANIFEST_NONCANONICAL_BYTES",
    "MANIFEST_INVALID_PATH",
    "MANIFEST_MEMBER_COLLISION",
    "MANIFEST_MEMBER_SET_MISMATCH",
    "MANIFEST_MEMBER_SIZE_MISMATCH",
    "MANIFEST_MEMBER_DIGEST_MISMATCH",
    "MANIFEST_TREE_UNSAFE",
    "MANIFEST_IO_ERROR",
}


class _ExplodingMapping(Mapping[str, bytes]):
    def __init__(self, error: Exception) -> None:
        self.error = error

    def __getitem__(self, key: str) -> bytes:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 1

    def items(self):
        raise self.error


class _ExplodingDocument(dict):
    def get(self, _key, _default=None):
        raise RuntimeError("sensitive-token")


class _ExplodingPath:
    def __fspath__(self) -> str:
        raise RuntimeError("sensitive-token")


def _members() -> dict[str, bytes]:
    return {
        f"{PAYLOAD_ROOT}/Init.py": b"# init\n",
        f"{PAYLOAD_ROOT}/src/engine.py": b"VALUE = 1\n",
        f"{PAYLOAD_ROOT}/src/lib/cp310/runtime.pyd": b"runtime-bytes",
    }


def _build(members: dict[str, bytes] | None = None) -> dict:
    return build_candidate_file_manifest(
        _members() if members is None else members,
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ARTIFACT_NAME,
    )


def _archive(document: dict, members: dict[str, bytes] | None = None) -> dict[str, bytes]:
    archive = dict(_members() if members is None else members)
    archive[MANIFEST_MEMBER] = canonical_candidate_manifest_bytes(document)
    return archive


def _write_installed_tree(root: Path, document: dict, members: dict[str, bytes]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for member, content in members.items():
        relative = member.removeprefix(f"{PAYLOAD_ROOT}/")
        destination = root / Path(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    manifest_path = root / Path(MANIFEST_MEMBER).name
    manifest_path.write_bytes(canonical_candidate_manifest_bytes(document))


def test_manifest_constants_are_closed_public_identity() -> None:
    assert MANIFEST_SCHEMA == "bcs.freecad_candidate_file_manifest/1.0"
    assert CONTRACT_VERSION == "1.0"
    assert PRODUCT_REPOSITORY == "BlueCollar-Systems/PDF-Importer-FreeCAD"
    assert PAYLOAD_ROOT == "PDFVectorImporter"
    assert MANIFEST_MEMBER == "PDFVectorImporter/candidate-file-manifest.json"


def test_build_is_deterministic_across_mapping_order_and_sorts_paths_by_utf8() -> None:
    members = _members()
    documents = [
        _build(dict(order)) for order in itertools.permutations(members.items())
    ]
    first = documents[0]

    assert all(document == first for document in documents)
    assert len({canonical_candidate_manifest_bytes(document) for document in documents}) == 1
    assert len({candidate_manifest_sha256(document) for document in documents}) == 1
    assert [entry["path"] for entry in first["files"]] == sorted(
        (name.removeprefix(f"{PAYLOAD_ROOT}/") for name in members),
        key=lambda value: value.encode("utf-8"),
    )
    assert set(first) == {
        "schema",
        "contract_version",
        "repository",
        "source_commit",
        "package_version",
        "artifact_name",
        "payload_root",
        "manifest_member",
        "files",
    }


def test_canonical_bytes_have_one_exact_compact_utf8_encoding() -> None:
    document = build_candidate_file_manifest(
        {f"{PAYLOAD_ROOT}/a.py": b"x"},
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ARTIFACT_NAME,
    )

    expected = (
        '{"artifact_name":"FreeCAD-PDF-Importer_v7.8.9.zip",'
        '"contract_version":"1.0","files":[{"path":"a.py",'
        '"sha256":"2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",'
        '"size":1}],"manifest_member":"PDFVectorImporter/candidate-file-manifest.json",'
        '"package_version":"7.8.9","payload_root":"PDFVectorImporter",'
        '"repository":"BlueCollar-Systems/PDF-Importer-FreeCAD",'
        '"schema":"bcs.freecad_candidate_file_manifest/1.0",'
        f'"source_commit":"{SOURCE_COMMIT}"}}\n'
    ).encode("utf-8")

    assert canonical_candidate_manifest_bytes(document) == expected
    assert not expected.startswith(b"\xef\xbb\xbf")
    assert expected.endswith(b"\n") and not expected.endswith(b"\n\n")
    assert b": " not in expected and b", " not in expected

    reordered = dict(reversed(list(document.items())))
    assert canonical_candidate_manifest_bytes(reordered) == expected


def test_canonical_bytes_emit_nfc_as_utf8_not_json_unicode_escapes() -> None:
    document = build_candidate_file_manifest(
        {f"{PAYLOAD_ROOT}/caf\u00e9.py": b"x"},
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ARTIFACT_NAME,
    )

    payload = canonical_candidate_manifest_bytes(document)

    assert "café.py".encode("utf-8") in payload
    assert b"caf\\u00e9.py" not in payload
    assert payload.decode("utf-8").encode("utf-8") == payload


def test_parser_accepts_only_strict_canonical_manifest_bytes() -> None:
    document = _build()
    payload = canonical_candidate_manifest_bytes(document)

    parsed, problems = parse_candidate_file_manifest(payload)

    assert problems == []
    assert parsed == document


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        (b"\xff", "MANIFEST_INVALID_UTF8"),
        (b"\xef\xbb\xbf{}", "MANIFEST_INVALID_UTF8"),
        (b'{"schema":"one","schema":"two"}\n', "MANIFEST_DUPLICATE_KEY"),
        (b'{"value":NaN}\n', "MANIFEST_NONFINITE_NUMBER"),
        (b"{not-json}\n", "MANIFEST_INVALID_JSON"),
    ],
)
def test_parser_rejects_ambiguous_or_invalid_json(payload: bytes, expected_code: str) -> None:
    parsed, problems = parse_candidate_file_manifest(payload)

    assert parsed is None
    assert expected_code in problems
    assert problems == sorted(set(problems))


def test_parser_rejects_noncanonical_but_semantically_equal_json() -> None:
    document = _build()
    pretty = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    parsed, problems = parse_candidate_file_manifest(pretty)

    assert parsed is None
    assert "MANIFEST_NONCANONICAL_BYTES" in problems


@pytest.mark.parametrize(
    "variant",
    [
        "missing_lf",
        "double_lf",
        "crlf",
        "top_level_order",
        "nested_record_order",
        "escaped_unicode",
    ],
)
def test_parser_rejects_every_noncanonical_encoding_variant(variant: str) -> None:
    document = build_candidate_file_manifest(
        {f"{PAYLOAD_ROOT}/caf\u00e9.py": b"x"},
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ARTIFACT_NAME,
    )
    canonical = canonical_candidate_manifest_bytes(document)
    if variant == "missing_lf":
        payload = canonical[:-1]
    elif variant == "double_lf":
        payload = canonical + b"\n"
    elif variant == "crlf":
        payload = canonical[:-1] + b"\r\n"
    elif variant == "top_level_order":
        payload = (
            json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    elif variant == "nested_record_order":
        reordered = {key: document[key] for key in sorted(document)}
        record = document["files"][0]
        reordered["files"] = [
            {"size": record["size"], "sha256": record["sha256"], "path": record["path"]}
        ]
        payload = (
            json.dumps(reordered, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
    else:
        payload = (
            json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")

    parsed, problems = parse_candidate_file_manifest(payload)

    assert parsed is None
    assert "MANIFEST_NONCANONICAL_BYTES" in problems


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        "manifest",
        {1: "non-string-key"},
        {"files": [{"path": object()}]},
    ],
)
def test_malformed_python_documents_never_raise_or_leak_values(document: object) -> None:
    first = validate_candidate_file_manifest(document)
    second = validate_candidate_file_manifest(document)

    assert first
    assert first == second == sorted(set(first))
    assert "non-string-key" not in " ".join(first)
    assert canonical_candidate_manifest_bytes(document) == b""
    assert candidate_manifest_sha256(document) == ""


def test_cyclic_python_document_never_raises() -> None:
    cyclic: dict[str, object] = {}
    cyclic["files"] = cyclic

    problems = validate_candidate_file_manifest(cyclic)

    assert problems == sorted(set(problems))
    assert "MANIFEST_INVALID_DOCUMENT" in problems
    assert canonical_candidate_manifest_bytes(cyclic) == b""


def test_hostile_dict_subclass_never_escapes_an_exception_or_value() -> None:
    document = _ExplodingDocument(_build())

    first = validate_candidate_file_manifest(document)
    second = validate_candidate_file_manifest(document)

    assert first == second == ["MANIFEST_INVALID_DOCUMENT"]
    assert "sensitive-token" not in " ".join(first)
    assert canonical_candidate_manifest_bytes(document) == b""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema", "bcs.other/1.0"),
        ("contract_version", "2.0"),
        ("repository", "Example/Other"),
        ("source_commit", "A" * 40),
        ("source_commit", "a" * 39),
        ("package_version", "01.2.3"),
        ("package_version", "1.2"),
        ("artifact_name", "renamed.zip"),
        ("payload_root", "OtherRoot"),
        ("manifest_member", "PDFVectorImporter/other.json"),
    ],
)
def test_identity_fields_are_exact(field: str, value: object) -> None:
    document = _build()
    document[field] = value

    assert "MANIFEST_INVALID_IDENTITY" in validate_candidate_file_manifest(document)
    assert canonical_candidate_manifest_bytes(document) == b""


@pytest.mark.parametrize(
    "mutation",
    [
        {"unknown": True},
        {"files": []},
        {"files": [{"path": "a.py", "size": True, "sha256": "a" * 64}]},
        {"files": [{"path": "a.py", "size": 1.0, "sha256": "a" * 64}]},
        {"files": [{"path": "a.py", "size": -1, "sha256": "a" * 64}]},
        {"files": [{"path": "a.py", "size": 1, "sha256": "A" * 64}]},
        {"files": [{"path": "a.py", "size": 1, "sha256": "a" * 63}]},
        {"files": [{"path": "a.py", "size": 1, "sha256": "a" * 64, "extra": 1}]},
    ],
)
def test_document_and_file_records_are_closed_and_exact(mutation: dict) -> None:
    document = _build()
    document.update(mutation)

    assert "MANIFEST_INVALID_DOCUMENT" in validate_candidate_file_manifest(document)


@pytest.mark.parametrize(
    "relative_path",
    [
        "../escape.py",
        "src/../../escape.py",
        "src\\module.py",
        "/absolute.py",
        "Q:/drive.py",
        "Q:drive.py",
        "//server/share.py",
        "\\\\?\\Q:\\device.py",
        "\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\file.py",
        "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\file.py",
        "\\\\.\\PIPE\\channel",
        "\\??\\Q:\\device.py",
        "src:stream.py",
        "a//b.py",
        "a/./b.py",
        "a/../b.py",
        "a/",
        "trailing./file.py",
        "trailing /file.py",
        "bad\x1f.py",
        "wild*.py",
        "wild?.py",
        'quote".py',
        "angle<.py",
        "pipe|.py",
        "CON",
        "con.txt",
        "AUX.py",
        "NUL.dat",
        "COM1.dll",
        "LPT9",
        "COM\u00b9.txt",
        "candidate-file-manifest.json",
        "drawing.pdf",
        "drawing.dxf",
        "drawing.dwg",
        "drawing.skp",
        "drawing.fcstd",
        "drawing.blend",
        "drawing.3dm",
        "drawing.step",
        "drawing.iges",
        "archive/model.stp/data.bin",
        "_runs/result.json",
        "_legacy/result.json",
        "scratch/result.json",
        "archive/result.json",
        "users/result.json",
        "desktop/result.json",
        "private-case/result.json",
        "status.before_old/result.json",
    ],
)
def test_builder_rejects_nonportable_private_or_reserved_paths(relative_path: str) -> None:
    with pytest.raises(ValueError, match="MANIFEST_INVALID_PATH"):
        _build({f"{PAYLOAD_ROOT}/{relative_path}": b"x"})


@pytest.mark.parametrize(
    "member_name",
    [
        "OtherRoot/a.py",
        PAYLOAD_ROOT,
        MANIFEST_MEMBER,
        MANIFEST_MEMBER.upper(),
        7,
    ],
)
def test_builder_rejects_invalid_member_names(member_name: object) -> None:
    with pytest.raises(ValueError) as raised:
        _build({member_name: b"x"})  # type: ignore[dict-item]
    assert str(raised.value) in ERROR_CODES


@pytest.mark.parametrize("members", [{}, {f"{PAYLOAD_ROOT}/a.py": "not-bytes"}])
def test_builder_rejects_empty_or_nonbyte_member_maps(members: dict) -> None:
    with pytest.raises(ValueError) as raised:
        _build(members)  # type: ignore[arg-type]
    assert str(raised.value) in ERROR_CODES


@pytest.mark.parametrize("error", [RuntimeError("sensitive-token"), OSError("sensitive-token")])
def test_builder_converts_hostile_mapping_failures_to_closed_value_error(
    error: Exception,
) -> None:
    with pytest.raises(ValueError) as raised:
        build_candidate_file_manifest(
            _ExplodingMapping(error),
            source_commit=SOURCE_COMMIT,
            package_version=PACKAGE_VERSION,
            artifact_name=ARTIFACT_NAME,
        )

    assert str(raised.value) in ERROR_CODES
    assert "sensitive-token" not in str(raised.value)


@pytest.mark.parametrize("error", [RuntimeError("sensitive-token"), OSError("sensitive-token")])
def test_archive_validator_converts_hostile_mapping_failures_to_codes(
    error: Exception,
) -> None:
    problems = validate_candidate_archive_members(_build(), _ExplodingMapping(error))

    assert "MANIFEST_INVALID_DOCUMENT" in problems
    assert problems == sorted(set(problems))
    assert "sensitive-token" not in " ".join(problems)


def test_nfc_and_windows_casefold_aliases_never_overwrite() -> None:
    with pytest.raises(ValueError, match="MANIFEST_MEMBER_COLLISION"):
        _build(
            {
                f"{PAYLOAD_ROOT}/Module.py": b"one",
                f"{PAYLOAD_ROOT}/module.py": b"two",
            }
        )

    with pytest.raises(ValueError):
        _build(
            {
                f"{PAYLOAD_ROOT}/R\u00e9sum\u00e9.py": b"one",
                f"{PAYLOAD_ROOT}/Re\u0301sume\u0301.py": b"two",
            }
        )

    with pytest.raises(ValueError, match="MANIFEST_INVALID_PATH"):
        _build({f"{PAYLOAD_ROOT}/Re\u0301sume\u0301.py": b"one"})


def test_manifest_digest_binds_every_identity_and_member_byte() -> None:
    baseline = _build()
    baseline_digest = candidate_manifest_sha256(baseline)

    changed_byte = _members()
    changed_byte[f"{PAYLOAD_ROOT}/Init.py"] = b"# changed\n"
    assert candidate_manifest_sha256(_build(changed_byte)) != baseline_digest

    changed_commit = build_candidate_file_manifest(
        _members(),
        source_commit="b" * 40,
        package_version=PACKAGE_VERSION,
        artifact_name=ARTIFACT_NAME,
    )
    assert candidate_manifest_sha256(changed_commit) != baseline_digest

    changed_version = build_candidate_file_manifest(
        _members(),
        source_commit=SOURCE_COMMIT,
        package_version="7.8.10",
        artifact_name="FreeCAD-PDF-Importer_v7.8.10.zip",
    )
    assert candidate_manifest_sha256(changed_version) != baseline_digest

    with pytest.raises(ValueError, match="MANIFEST_INVALID_IDENTITY"):
        build_candidate_file_manifest(
            _members(),
            source_commit=SOURCE_COMMIT,
            package_version=PACKAGE_VERSION,
            artifact_name="different.zip",
        )

    one = build_candidate_file_manifest(
        {f"{PAYLOAD_ROOT}/one.py": b"one"},
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ARTIFACT_NAME,
    )
    changed_size = copy.deepcopy(one)
    changed_size["files"][0]["size"] += 1
    changed_path = copy.deepcopy(one)
    changed_path["files"][0]["path"] = "renamed.py"
    assert candidate_manifest_sha256(changed_size) != candidate_manifest_sha256(one)
    assert candidate_manifest_sha256(changed_path) != candidate_manifest_sha256(one)

    renamed_member = build_candidate_file_manifest(
        {f"{PAYLOAD_ROOT}/renamed.py": b"one"},
        source_commit=SOURCE_COMMIT,
        package_version=PACKAGE_VERSION,
        artifact_name=ARTIFACT_NAME,
    )
    assert candidate_manifest_sha256(renamed_member) != candidate_manifest_sha256(one)


def test_file_records_must_remain_in_canonical_utf8_path_order() -> None:
    document = _build()
    document["files"] = list(reversed(document["files"]))

    assert "MANIFEST_INVALID_DOCUMENT" in validate_candidate_file_manifest(document)


def test_valid_archive_is_exactly_manifest_plus_listed_member_bytes() -> None:
    members = _members()
    document = _build(members)
    archive = _archive(document, members)

    assert validate_candidate_archive_members(document, archive) == []
    assert set(archive) == {MANIFEST_MEMBER, *members}
    assert all(entry["path"] != Path(MANIFEST_MEMBER).name for entry in document["files"])
    assert hashlib.sha256(archive[MANIFEST_MEMBER]).hexdigest() == candidate_manifest_sha256(
        document
    )


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_file", "MANIFEST_MEMBER_SET_MISMATCH"),
        ("extra_file", "MANIFEST_MEMBER_SET_MISMATCH"),
        ("renamed_file", "MANIFEST_MEMBER_SET_MISMATCH"),
        ("changed_size", "MANIFEST_MEMBER_SIZE_MISMATCH"),
        ("changed_digest", "MANIFEST_MEMBER_DIGEST_MISMATCH"),
        ("missing_manifest", "MANIFEST_MEMBER_SET_MISMATCH"),
        ("noncanonical_manifest", "MANIFEST_NONCANONICAL_BYTES"),
        ("mismatched_manifest", "MANIFEST_MEMBER_DIGEST_MISMATCH"),
        ("nonbyte_member", "MANIFEST_INVALID_DOCUMENT"),
    ],
)
def test_archive_closure_fails_closed_for_every_member_drift(
    mutation: str, expected_code: str
) -> None:
    members = _members()
    document = _build(members)
    archive: dict = _archive(document, members)
    target = f"{PAYLOAD_ROOT}/Init.py"

    if mutation == "missing_file":
        archive.pop(target)
    elif mutation == "extra_file":
        archive[f"{PAYLOAD_ROOT}/extra.py"] = b"extra"
    elif mutation == "renamed_file":
        archive[f"{PAYLOAD_ROOT}/renamed.py"] = archive.pop(target)
    elif mutation == "changed_size":
        archive[target] += b"longer"
    elif mutation == "changed_digest":
        archive[target] = b"# nooo\n"
        assert len(archive[target]) == len(members[target])
    elif mutation == "missing_manifest":
        archive.pop(MANIFEST_MEMBER)
    elif mutation == "noncanonical_manifest":
        archive[MANIFEST_MEMBER] = (
            json.dumps(document, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
    elif mutation == "mismatched_manifest":
        other = copy.deepcopy(document)
        other["source_commit"] = "b" * 40
        archive[MANIFEST_MEMBER] = canonical_candidate_manifest_bytes(other)
    elif mutation == "nonbyte_member":
        archive[target] = "not-bytes"

    problems = validate_candidate_archive_members(document, archive)

    assert expected_code in problems
    assert problems == sorted(set(problems))


def test_archive_rejects_case_alias_even_when_expected_members_exist() -> None:
    members = _members()
    document = _build(members)
    archive = _archive(document, members)
    archive[f"{PAYLOAD_ROOT}/init.py"] = archive[f"{PAYLOAD_ROOT}/Init.py"]

    problems = validate_candidate_archive_members(document, archive)

    assert "MANIFEST_MEMBER_COLLISION" in problems
    assert "MANIFEST_MEMBER_SET_MISMATCH" in problems


def test_archive_validation_never_raises_for_hostile_document_subclass() -> None:
    problems = validate_candidate_archive_members(_ExplodingDocument(_build()), {})

    assert problems == ["MANIFEST_INVALID_DOCUMENT"]


def test_installed_tree_matches_the_archive_derived_manifest(tmp_path: Path) -> None:
    members = _members()
    document = _build(members)
    root = tmp_path / PAYLOAD_ROOT
    _write_installed_tree(root, document, members)

    assert validate_installed_candidate_tree(document, root) == []


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing", "MANIFEST_MEMBER_SET_MISMATCH"),
        ("extra", "MANIFEST_MEMBER_SET_MISMATCH"),
        ("renamed", "MANIFEST_MEMBER_SET_MISMATCH"),
        ("changed_size", "MANIFEST_MEMBER_SIZE_MISMATCH"),
        ("changed_digest", "MANIFEST_MEMBER_DIGEST_MISMATCH"),
        ("pycache", "MANIFEST_MEMBER_SET_MISMATCH"),
        ("manifest", "MANIFEST_MEMBER_DIGEST_MISMATCH"),
    ],
)
def test_installed_tree_rejects_missing_extra_renamed_or_changed_files(
    tmp_path: Path, mutation: str, expected_code: str
) -> None:
    members = _members()
    document = _build(members)
    root = tmp_path / PAYLOAD_ROOT
    _write_installed_tree(root, document, members)
    target = root / "Init.py"

    if mutation == "missing":
        target.unlink()
    elif mutation == "extra":
        (root / "extra.py").write_bytes(b"extra")
    elif mutation == "renamed":
        target.rename(root / "renamed.py")
    elif mutation == "changed_size":
        target.write_bytes(b"longer than before")
    elif mutation == "changed_digest":
        target.write_bytes(b"# nooo\n")
        assert target.stat().st_size == len(members[f"{PAYLOAD_ROOT}/Init.py"])
    elif mutation == "pycache":
        cache = root / "__pycache__"
        cache.mkdir()
        (cache / "engine.pyc").write_bytes(b"cache")
    elif mutation == "manifest":
        (root / Path(MANIFEST_MEMBER).name).write_bytes(b"{}\n")

    problems = validate_installed_candidate_tree(document, root)

    assert expected_code in problems
    assert problems == sorted(set(problems))


def test_installed_tree_rejects_symlinked_content(tmp_path: Path) -> None:
    members = _members()
    document = _build(members)
    root = tmp_path / PAYLOAD_ROOT
    _write_installed_tree(root, document, members)
    target = root / "Init.py"
    outside = tmp_path / "outside.py"
    outside.write_bytes(target.read_bytes())
    target.unlink()
    try:
        target.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    assert "MANIFEST_TREE_UNSAFE" in validate_installed_candidate_tree(document, root)


def test_installed_tree_rejects_injected_reparse_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = _members()
    document = _build(members)
    root = tmp_path / PAYLOAD_ROOT
    _write_installed_tree(root, document, members)
    original = candidate_manifest._is_link_or_reparse

    def injected(path: Path) -> bool:
        return path.name == "runtime.pyd" or original(path)

    monkeypatch.setattr(candidate_manifest, "_is_link_or_reparse", injected)

    assert "MANIFEST_TREE_UNSAFE" in validate_installed_candidate_tree(document, root)


def test_installed_tree_rejects_a_nonregular_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    members = _members()
    document = _build(members)
    root = tmp_path / PAYLOAD_ROOT
    _write_installed_tree(root, document, members)
    original_is_regular = candidate_manifest.stat.S_ISREG

    monkeypatch.setattr(candidate_manifest.stat, "S_ISREG", lambda _mode: False)
    try:
        problems = validate_installed_candidate_tree(document, root)
    finally:
        monkeypatch.setattr(candidate_manifest.stat, "S_ISREG", original_is_regular)

    assert "MANIFEST_TREE_UNSAFE" in problems


def test_installed_tree_io_and_diagnostics_never_expose_the_caller_root(
    tmp_path: Path,
) -> None:
    document = _build()
    root = tmp_path / "sensitive-token" / PAYLOAD_ROOT

    first = validate_installed_candidate_tree(document, root)
    second = validate_installed_candidate_tree(document, root)

    assert first == second == sorted(set(first))
    assert "MANIFEST_IO_ERROR" in first
    assert str(root) not in " ".join(first)
    assert "sensitive-token" not in " ".join(first)


def test_installed_tree_rejects_hostile_pathlike_without_raising() -> None:
    problems = validate_installed_candidate_tree(_build(), _ExplodingPath())  # type: ignore[arg-type]

    assert problems == ["MANIFEST_IO_ERROR"]
    assert "sensitive-token" not in " ".join(problems)


def test_archive_and_tree_validation_of_invalid_documents_never_raise(
    tmp_path: Path,
) -> None:
    malformed = {"files": [{"path": object()}]}

    archive_problems = validate_candidate_archive_members(malformed, {})
    tree_problems = validate_installed_candidate_tree(malformed, tmp_path / "missing")

    assert archive_problems == sorted(set(archive_problems))
    assert tree_problems == sorted(set(tree_problems))
    assert "MANIFEST_INVALID_DOCUMENT" in archive_problems
    assert "MANIFEST_INVALID_DOCUMENT" in tree_problems


def test_error_vocabulary_is_closed() -> None:
    documents = [None, {}, {"files": []}, _build()]
    observed = set()
    for document in documents:
        observed.update(validate_candidate_file_manifest(document))
        observed.update(validate_candidate_archive_members(document, {}))

    assert observed <= ERROR_CODES
