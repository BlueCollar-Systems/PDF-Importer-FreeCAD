"""Deterministic identity for one complete FreeCAD candidate file tree.

This module is deliberately product-owned and standard-library-only.  It
describes byte identity; it does not claim that an import, validator, campaign,
or release has passed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any


MANIFEST_SCHEMA = "bcs.freecad_candidate_file_manifest/1.0"
CONTRACT_VERSION = "1.0"
PRODUCT_REPOSITORY = "BlueCollar-Systems/PDF-Importer-FreeCAD"
PAYLOAD_ROOT = "PDFVectorImporter"
MANIFEST_MEMBER = "PDFVectorImporter/candidate-file-manifest.json"


_MANIFEST_RELATIVE_PATH = MANIFEST_MEMBER.removeprefix(PAYLOAD_ROOT + "/")
_DOCUMENT_FIELDS = {
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
_FILE_FIELDS = {"path", "size", "sha256"}
_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_VERSION = re.compile(r"\A(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_WINDOWS_INVALID_CHARACTERS = frozenset('<>:"|?*')
_WINDOWS_DEVICES = {
    "con",
    "prn",
    "aux",
    "nul",
    "conin$",
    "conout$",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
    *(f"com{number}" for number in ("¹", "²", "³")),
    *(f"lpt{number}" for number in ("¹", "²", "³")),
}
_RESERVED_COMPONENTS = {
    "_runs",
    "_legacy",
    "scratch",
    "archive",
    "private",
    "user",
    "users",
    "desktop",
    "documents",
    "downloads",
    "appdata",
}
_PRIVATE_SOURCE_SUFFIXES = {
    ".pdf",
    ".dxf",
    ".dwg",
    ".skp",
    ".fcstd",
    ".blend",
    ".3dm",
    ".step",
    ".stp",
    ".iges",
    ".igs",
}
_ERROR_CODES = {
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
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
)


class _DuplicateKey(ValueError):
    pass


class _NonfiniteNumber(ValueError):
    pass


def _codes(values: set[str] | list[str] | tuple[str, ...]) -> list[str]:
    """Return the closed, deterministic diagnostic vocabulary only."""

    return sorted({value for value in values if value in _ERROR_CODES})


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _reject_nonfinite(_value: str) -> None:
    raise _NonfiniteNumber


def _path_identity(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def _component_is_private(component: str) -> bool:
    folded = component.casefold()
    if (
        folded in _RESERVED_COMPONENTS
        or folded.startswith("private-")
        or folded.startswith("status.before_")
    ):
        return True
    return any(folded.endswith(suffix) for suffix in _PRIVATE_SOURCE_SUFFIXES)


def _path_is_valid(path: object) -> bool:
    try:
        if not isinstance(path, str) or not path:
            return False
        if path != unicodedata.normalize("NFC", path):
            return False
        path.encode("utf-8", errors="strict")
        if path.startswith("/") or "\\" in path or ":" in path:
            return False
        if path.casefold() == _MANIFEST_RELATIVE_PATH.casefold():
            return False

        components = path.split("/")
        if any(component in {"", ".", ".."} for component in components):
            return False
        for component in components:
            if component.endswith((".", " ")):
                return False
            if any(
                ord(character) < 32
                or ord(character) == 127
                or character in _WINDOWS_INVALID_CHARACTERS
                for character in component
            ):
                return False
            device_stem = component.casefold().split(".", 1)[0]
            if device_stem in _WINDOWS_DEVICES or _component_is_private(component):
                return False
        return True
    except Exception:
        return False


def _validate_identity(document: dict[str, Any], problems: set[str]) -> None:
    version = document.get("package_version")
    expected_artifact = (
        f"FreeCAD-PDF-Importer_v{version}.zip" if isinstance(version, str) else None
    )
    if (
        document.get("schema") != MANIFEST_SCHEMA
        or document.get("contract_version") != CONTRACT_VERSION
        or document.get("repository") != PRODUCT_REPOSITORY
        or not isinstance(document.get("source_commit"), str)
        or _COMMIT.fullmatch(document["source_commit"]) is None
        or not isinstance(version, str)
        or _VERSION.fullmatch(version) is None
        or document.get("artifact_name") != expected_artifact
        or document.get("payload_root") != PAYLOAD_ROOT
        or document.get("manifest_member") != MANIFEST_MEMBER
    ):
        problems.add("MANIFEST_INVALID_IDENTITY")


def validate_candidate_file_manifest(document: object) -> list[str]:
    """Validate closed identity, records, and path grammar without raising."""

    problems: set[str] = set()
    try:
        if not isinstance(document, dict):
            return ["MANIFEST_INVALID_DOCUMENT"]
        if set(document) != _DOCUMENT_FIELDS or any(
            not isinstance(key, str) for key in document
        ):
            problems.add("MANIFEST_INVALID_DOCUMENT")
        _validate_identity(document, problems)

        files = document.get("files")
        if not isinstance(files, list) or not files:
            problems.add("MANIFEST_INVALID_DOCUMENT")
            return _codes(problems)

        paths: list[str] = []
        identities: set[str] = set()
        for record in files:
            if not isinstance(record, dict):
                problems.add("MANIFEST_INVALID_DOCUMENT")
                continue
            if set(record) != _FILE_FIELDS or any(
                not isinstance(key, str) for key in record
            ):
                problems.add("MANIFEST_INVALID_DOCUMENT")

            path = record.get("path")
            if not _path_is_valid(path):
                problems.add("MANIFEST_INVALID_PATH")
            else:
                assert isinstance(path, str)
                paths.append(path)
                identity = _path_identity(path)
                if identity in identities:
                    problems.add("MANIFEST_MEMBER_COLLISION")
                identities.add(identity)

            size = record.get("size")
            digest = record.get("sha256")
            if type(size) is not int or size < 0:
                problems.add("MANIFEST_INVALID_DOCUMENT")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                problems.add("MANIFEST_INVALID_DOCUMENT")

        if len(paths) == len(files) and paths != sorted(
            paths, key=lambda value: value.encode("utf-8")
        ):
            problems.add("MANIFEST_INVALID_DOCUMENT")
    except Exception:
        problems.add("MANIFEST_INVALID_DOCUMENT")
    return _codes(problems)


def canonical_candidate_manifest_bytes(document: object) -> bytes:
    """Return canonical NFC UTF-8 JSON, or empty bytes for an invalid document."""

    if validate_candidate_file_manifest(document):
        return b""
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        normalized = unicodedata.normalize("NFC", encoded)
        return (normalized + "\n").encode("utf-8", errors="strict")
    except Exception:
        return b""


def candidate_manifest_sha256(document: object) -> str:
    """Return the Task 1 installed-manifest digest, or empty on invalid input."""

    payload = canonical_candidate_manifest_bytes(document)
    return hashlib.sha256(payload).hexdigest() if payload else ""


def parse_candidate_file_manifest(payload: bytes) -> tuple[dict | None, list[str]]:
    """Strictly parse canonical bytes without accepting ambiguous JSON."""

    if not isinstance(payload, bytes):
        return None, ["MANIFEST_INVALID_DOCUMENT"]
    if payload.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return None, ["MANIFEST_INVALID_UTF8"]
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None, ["MANIFEST_INVALID_UTF8"]
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_nonfinite,
        )
    except _DuplicateKey:
        return None, ["MANIFEST_DUPLICATE_KEY"]
    except _NonfiniteNumber:
        return None, ["MANIFEST_NONFINITE_NUMBER"]
    except (json.JSONDecodeError, RecursionError, TypeError, ValueError):
        return None, ["MANIFEST_INVALID_JSON"]

    problems = validate_candidate_file_manifest(parsed)
    if problems:
        return None, problems
    canonical = canonical_candidate_manifest_bytes(parsed)
    if not canonical or canonical != payload:
        return None, ["MANIFEST_NONCANONICAL_BYTES"]
    assert isinstance(parsed, dict)
    return parsed, []


def _builder_error(code: str) -> ValueError:
    return ValueError(code if code in _ERROR_CODES else "MANIFEST_INVALID_DOCUMENT")


def _build_candidate_file_manifest(
    members: Mapping[str, bytes],
    *,
    source_commit: str,
    package_version: str,
    artifact_name: str,
) -> dict:
    """Build a manifest from one already captured archive-member byte map."""

    if not isinstance(members, Mapping):
        raise _builder_error("MANIFEST_INVALID_DOCUMENT")
    try:
        items = list(members.items())
    except Exception:
        raise _builder_error("MANIFEST_INVALID_DOCUMENT") from None
    if not items:
        raise _builder_error("MANIFEST_INVALID_DOCUMENT")

    records: list[dict[str, object]] = []
    identities: set[str] = set()
    prefix = PAYLOAD_ROOT + "/"
    for member_name, content in items:
        if not isinstance(member_name, str) or not member_name.startswith(prefix):
            raise _builder_error("MANIFEST_INVALID_PATH")
        if not isinstance(content, bytes):
            raise _builder_error("MANIFEST_INVALID_DOCUMENT")
        relative = member_name[len(prefix) :]
        if not _path_is_valid(relative):
            raise _builder_error("MANIFEST_INVALID_PATH")
        identity = _path_identity(relative)
        if identity in identities:
            raise _builder_error("MANIFEST_MEMBER_COLLISION")
        identities.add(identity)
        records.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    records.sort(key=lambda record: str(record["path"]).encode("utf-8"))
    document = {
        "schema": MANIFEST_SCHEMA,
        "contract_version": CONTRACT_VERSION,
        "repository": PRODUCT_REPOSITORY,
        "source_commit": source_commit,
        "package_version": package_version,
        "artifact_name": artifact_name,
        "payload_root": PAYLOAD_ROOT,
        "manifest_member": MANIFEST_MEMBER,
        "files": records,
    }
    problems = validate_candidate_file_manifest(document)
    if problems:
        raise _builder_error(problems[0])
    return document


def build_candidate_file_manifest(
    members: Mapping[str, bytes],
    *,
    source_commit: str,
    package_version: str,
    artifact_name: str,
) -> dict:
    """Build from captured bytes and expose only closed producer failures."""

    try:
        return _build_candidate_file_manifest(
            members,
            source_commit=source_commit,
            package_version=package_version,
            artifact_name=artifact_name,
        )
    except ValueError as exc:
        if str(exc) in _ERROR_CODES:
            raise
        raise _builder_error("MANIFEST_INVALID_DOCUMENT") from None
    except Exception:
        raise _builder_error("MANIFEST_INVALID_DOCUMENT") from None


def _mapping_items(members: object) -> tuple[list[tuple[object, object]], set[str]]:
    problems: set[str] = set()
    if not isinstance(members, Mapping):
        return [], {"MANIFEST_INVALID_DOCUMENT"}
    try:
        return list(members.items()), problems
    except Exception:
        return [], {"MANIFEST_INVALID_DOCUMENT"}


def _validate_candidate_archive_members(
    document: object, members: Mapping[str, bytes]
) -> list[str]:
    """Verify exact manifest-plus-listed-member closure for archive bytes."""

    problems = set(validate_candidate_file_manifest(document))
    if problems:
        return _codes(problems)
    assert isinstance(document, dict)

    items, mapping_problems = _mapping_items(members)
    problems.update(mapping_problems)
    actual: dict[str, bytes] = {}
    identities: set[str] = set()
    prefix = PAYLOAD_ROOT + "/"
    for member_name, content in items:
        if not isinstance(member_name, str):
            problems.add("MANIFEST_INVALID_DOCUMENT")
            continue
        identity = _path_identity(member_name)
        if identity in identities:
            problems.add("MANIFEST_MEMBER_COLLISION")
        identities.add(identity)

        if member_name != MANIFEST_MEMBER:
            if not member_name.startswith(prefix) or not _path_is_valid(
                member_name[len(prefix) :]
            ):
                problems.add("MANIFEST_INVALID_PATH")
        if not isinstance(content, bytes):
            problems.add("MANIFEST_INVALID_DOCUMENT")
            continue
        actual[member_name] = content

    expected_records = {
        prefix + record["path"]: record for record in document["files"]
    }
    expected_names = {MANIFEST_MEMBER, *expected_records}
    if set(actual) != expected_names:
        problems.add("MANIFEST_MEMBER_SET_MISMATCH")

    expected_manifest = canonical_candidate_manifest_bytes(document)
    manifest_payload = actual.get(MANIFEST_MEMBER)
    if isinstance(manifest_payload, bytes):
        _parsed, parse_problems = parse_candidate_file_manifest(manifest_payload)
        problems.update(parse_problems)
        if manifest_payload != expected_manifest:
            problems.add("MANIFEST_MEMBER_DIGEST_MISMATCH")

    for member_name, record in expected_records.items():
        content = actual.get(member_name)
        if not isinstance(content, bytes):
            continue
        if len(content) != record["size"]:
            problems.add("MANIFEST_MEMBER_SIZE_MISMATCH")
        if hashlib.sha256(content).hexdigest() != record["sha256"]:
            problems.add("MANIFEST_MEMBER_DIGEST_MISMATCH")
    return _codes(problems)


def validate_candidate_archive_members(
    document: object, members: Mapping[str, bytes]
) -> list[str]:
    """Verify archive closure and convert hostile protocol objects to codes."""

    try:
        return _validate_candidate_archive_members(document, members)
    except Exception:
        return ["MANIFEST_INVALID_DOCUMENT"]


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _installed_regular_files(
    root: Path, problems: set[str]
) -> dict[str, tuple[Path, int]]:
    files: dict[str, tuple[Path, int]] = {}
    identities: set[str] = set()

    def visit(directory: Path, components: tuple[str, ...]) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(
                    iterator, key=lambda item: item.name.encode("utf-8")
                )
        except Exception:
            problems.add("MANIFEST_IO_ERROR")
            return
        for entry in entries:
            path = Path(entry.path)
            relative_components = (*components, entry.name)
            relative = "/".join(relative_components)
            try:
                if _is_link_or_reparse(path):
                    problems.add("MANIFEST_TREE_UNSAFE")
                    continue
                metadata = entry.stat(follow_symlinks=False)
            except Exception:
                problems.add("MANIFEST_IO_ERROR")
                continue
            if (
                relative != _MANIFEST_RELATIVE_PATH
                and not _path_is_valid(relative)
            ):
                problems.add("MANIFEST_INVALID_PATH")
                continue
            if stat.S_ISDIR(metadata.st_mode):
                visit(path, relative_components)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                problems.add("MANIFEST_TREE_UNSAFE")
                continue
            identity = _path_identity(relative)
            if identity in identities:
                problems.add("MANIFEST_MEMBER_COLLISION")
            identities.add(identity)
            files[relative] = (path, int(metadata.st_size))

    visit(root, ())
    return files


def _validate_installed_candidate_tree(
    document: object, addon_root: Path
) -> list[str]:
    """Stream-verify the exact installed regular-file tree without path leaks."""

    problems = set(validate_candidate_file_manifest(document))
    if problems:
        return _codes(problems)
    assert isinstance(document, dict)
    try:
        root = Path(addon_root)
        if _is_link_or_reparse(root):
            return ["MANIFEST_TREE_UNSAFE"]
        if not root.is_dir():
            return ["MANIFEST_IO_ERROR"]
    except Exception:
        return ["MANIFEST_IO_ERROR"]

    files = _installed_regular_files(root, problems)
    expected_records = {record["path"]: record for record in document["files"]}
    expected_names = {_MANIFEST_RELATIVE_PATH, *expected_records}
    if set(files) != expected_names:
        problems.add("MANIFEST_MEMBER_SET_MISMATCH")

    manifest_entry = files.get(_MANIFEST_RELATIVE_PATH)
    if manifest_entry is not None:
        manifest_path, _manifest_size = manifest_entry
        try:
            manifest_payload = manifest_path.read_bytes()
        except OSError:
            problems.add("MANIFEST_IO_ERROR")
        else:
            _parsed, parse_problems = parse_candidate_file_manifest(manifest_payload)
            problems.update(parse_problems)
            if manifest_payload != canonical_candidate_manifest_bytes(document):
                problems.add("MANIFEST_MEMBER_DIGEST_MISMATCH")

    for relative, record in expected_records.items():
        installed = files.get(relative)
        if installed is None:
            continue
        path, actual_size = installed
        if actual_size != record["size"]:
            problems.add("MANIFEST_MEMBER_SIZE_MISMATCH")
        try:
            actual_digest = _sha256_path(path)
        except OSError:
            problems.add("MANIFEST_IO_ERROR")
            continue
        if actual_digest != record["sha256"]:
            problems.add("MANIFEST_MEMBER_DIGEST_MISMATCH")
    return _codes(problems)


def validate_installed_candidate_tree(
    document: object, addon_root: Path
) -> list[str]:
    """Verify installed closure and convert filesystem/protocol failures to codes."""

    try:
        return _validate_installed_candidate_tree(document, addon_root)
    except Exception:
        return ["MANIFEST_IO_ERROR"]


__all__ = [
    "CONTRACT_VERSION",
    "MANIFEST_MEMBER",
    "MANIFEST_SCHEMA",
    "PAYLOAD_ROOT",
    "PRODUCT_REPOSITORY",
    "build_candidate_file_manifest",
    "candidate_manifest_sha256",
    "canonical_candidate_manifest_bytes",
    "parse_candidate_file_manifest",
    "validate_candidate_archive_members",
    "validate_candidate_file_manifest",
    "validate_installed_candidate_tree",
]
