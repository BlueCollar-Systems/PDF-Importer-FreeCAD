#!/usr/bin/env python3
"""Build a Windows Setup.exe installer for PDFVectorImporter using Inno Setup."""

from __future__ import annotations

import argparse
import ctypes
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
import time
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import build_release
from scripts.smoke_release_zip import validate_release_zip_manifest_bytes


REPO_ROOT = Path(__file__).parent.resolve()
ADDON_DIR = REPO_ROOT / "PDFVectorImporter"
DIST_DIR = REPO_ROOT / "dist"
STAGE_DIR = DIST_DIR / "installer_stage"
INNO_SCRIPT = REPO_ROOT / "installer" / "PDFVectorImporter.iss"
INNO_TOOLCHAIN_MANIFEST = REPO_ROOT / "installer" / "inno-toolchain-6.7.1.json"
_STAGE_METADATA_DIRNAME = ".installer-source"
_STAGE_TEMP_PREFIX = ".installer-stage-"
_STAGE_QUARANTINE_PREFIX = ".installer-quarantine-"
_ATTESTATION_TEMP_PREFIX = ".installer-attestation-"
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
)
_FILE_ATTRIBUTE_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
_WINDOWS_SUPPORTED_FILESYSTEMS = frozenset({"NTFS", "REFS"})
_FILE_LIST_DIRECTORY = 0x00000001
_FILE_ADD_FILE = 0x00000002
_FILE_ADD_SUBDIRECTORY = 0x00000004
_FILE_TRAVERSE = 0x00000020
_FILE_READ_DATA = 0x00000001
_FILE_WRITE_DATA = 0x00000002
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_WRITE_ATTRIBUTES = 0x00000100
_DELETE = 0x00010000
_SYNCHRONIZE = 0x00100000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_OPEN = 0x00000001
_FILE_CREATE = 0x00000002
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_REPARSE_POINT = 0x00200000
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")


def _load_candidate_manifest_contract():
    """Load the pinned checkout contract without importing the addon package."""

    contract_path = REPO_ROOT / "PDFVectorImporter" / "candidate_manifest.py"
    spec = importlib.util.spec_from_file_location(
        "_freecad_installer_candidate_manifest", contract_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate manifest contract unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CANDIDATE_MANIFEST = _load_candidate_manifest_contract()
MANIFEST_MEMBER = _CANDIDATE_MANIFEST.MANIFEST_MEMBER
_MANIFEST_ERROR_CODES = frozenset(_CANDIDATE_MANIFEST._ERROR_CODES)
_RELEASE_ZIP_ERROR_CODES = frozenset(
    {
        "RELEASE_ZIP_DUPLICATE_MEMBER",
        "RELEASE_ZIP_MEMBER_ALIAS",
        "RELEASE_ZIP_NONREGULAR_MEMBER",
        "RELEASE_ZIP_ENCRYPTED_MEMBER",
        "RELEASE_ZIP_UNSUPPORTED_COMPRESSION",
        "RELEASE_ZIP_UNSAFE_MEMBER",
        "RELEASE_ZIP_MANIFEST_MISSING",
        "RELEASE_ZIP_MANIFEST_DUPLICATE",
        "RELEASE_ZIP_ARTIFACT_NAME_MISMATCH",
        "RELEASE_ZIP_CORRUPT",
        "RELEASE_ZIP_IO_ERROR",
        *_MANIFEST_ERROR_CODES,
    }
)


@dataclass(frozen=True)
class InstallerStage:
    version: str
    stage_root: Path
    source_dir: Path
    source_zip_snapshot: Path
    source_zip_name: str
    source_zip_size: int
    source_zip_sha256: str
    candidate_manifest_bytes: bytes
    installed_manifest_sha256: str
    stage_identity_sha256: str


class _UnsafePath(Exception):
    pass


class _ChangedPath(Exception):
    pass


class _TempCollision(Exception):
    pass


class _NativeCapabilityError(Exception):
    pass


class _StageCloseError(Exception):
    def __init__(self, role: str):
        super().__init__(role)
        self.role = role


@dataclass(frozen=True)
class _WindowsHandleIdentity:
    volume_serial: int
    file_id: bytes
    file_type: int
    file_attributes: int
    reparse_tag: int

    def __post_init__(self) -> None:
        if type(self.file_id) is not bytes or len(self.file_id) != 16:
            raise ValueError("Windows file ID must contain exactly 128 bits")


@dataclass
class _WindowsHeldEntry:
    handle: object
    identity: _WindowsHandleIdentity
    name: str
    path: Path
    role: str
    parent_handle: object | None = None
    payload: bytes | None = None
    closed: bool = False


@dataclass
class _WindowsStageHandles:
    stage_parent: Path
    parent_chain: list[_WindowsHeldEntry]
    root: _WindowsHeldEntry
    directories: dict[str, _WindowsHeldEntry]
    files: dict[str, _WindowsHeldEntry]


def _closed_release_zip_codes(values: object) -> list[str]:
    try:
        items = list(values)
        codes = {value for value in items if value in _RELEASE_ZIP_ERROR_CODES}
        if any(value not in _RELEASE_ZIP_ERROR_CODES for value in items):
            codes.add("RELEASE_ZIP_IO_ERROR")
        return sorted(codes)
    except Exception:
        return ["RELEASE_ZIP_IO_ERROR"]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _absolute_lexical(path: str | Path) -> Path:
    """Return an absolute path without resolving links or reparses."""

    return Path(os.path.abspath(os.fspath(path)))


def _name_identity(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _is_link_or_reparse(path: Path, metadata=None) -> bool:
    if metadata is None:
        metadata = os.lstat(path)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _stat_identity(metadata) -> tuple[int, int, int]:
    return (
        int(getattr(metadata, "st_dev", 0)),
        int(getattr(metadata, "st_ino", 0)),
        stat.S_IFMT(metadata.st_mode),
    )


def _stable_identity(metadata) -> tuple[int, int, int, int, int]:
    return (
        *_stat_identity(metadata),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", 0)),
    )


def _assert_exact_child(parent: Path, name: str, *, allow_absent: bool) -> bool:
    """Reject Windows-case/NFC aliases and report whether the exact child exists."""

    identity = _name_identity(name)
    exact = False
    matches = 0
    with os.scandir(parent) as entries:
        for entry in entries:
            if _name_identity(entry.name) != identity:
                continue
            matches += 1
            if entry.name == name:
                exact = True
    if matches > 1 or (matches == 1 and not exact):
        raise _UnsafePath
    if not exact and not allow_absent:
        raise _ChangedPath
    return exact


def _directory_chain(path: Path) -> tuple[tuple[Path, tuple[int, int, int]], ...]:
    absolute = _absolute_lexical(path)
    anchor = Path(absolute.anchor)
    if not anchor.anchor:
        raise _UnsafePath
    chain: list[tuple[Path, tuple[int, int, int]]] = []
    current = anchor
    root_metadata = os.lstat(current)
    if _is_link_or_reparse(current, root_metadata) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise _UnsafePath
    chain.append((current, _stat_identity(root_metadata)))
    for component in absolute.parts[1:]:
        if not _assert_exact_child(current, component, allow_absent=False):
            raise _ChangedPath
        current = current / component
        metadata = os.lstat(current)
        if _is_link_or_reparse(current, metadata) or not stat.S_ISDIR(
            metadata.st_mode
        ):
            raise _UnsafePath
        chain.append((current, _stat_identity(metadata)))
    return tuple(chain)


def _prepare_safe_directory(
    path: str | Path,
) -> tuple[Path, tuple[tuple[Path, tuple[int, int, int]], ...]]:
    """Create missing components one at a time without following aliases/links."""

    absolute = _absolute_lexical(path)
    anchor = Path(absolute.anchor)
    if not anchor.anchor:
        raise _UnsafePath
    current = anchor
    root_metadata = os.lstat(current)
    if _is_link_or_reparse(current, root_metadata) or not stat.S_ISDIR(
        root_metadata.st_mode
    ):
        raise _UnsafePath
    for component in absolute.parts[1:]:
        exists = _assert_exact_child(current, component, allow_absent=True)
        candidate = current / component
        if not exists:
            os.mkdir(candidate)
            if not _assert_exact_child(current, component, allow_absent=False):
                raise _ChangedPath
        metadata = os.lstat(candidate)
        if _is_link_or_reparse(candidate, metadata) or not stat.S_ISDIR(
            metadata.st_mode
        ):
            raise _UnsafePath
        current = candidate
    return absolute, _directory_chain(absolute)


def _revalidate_chain(
    expected: tuple[tuple[Path, tuple[int, int, int]], ...]
) -> None:
    if not expected:
        raise _ChangedPath
    actual = _directory_chain(expected[-1][0])
    if actual != expected:
        raise _ChangedPath


def _capture_regular_file(path: Path) -> bytes:
    """Read one regular, single-link file through a stable handle."""

    before = os.lstat(path)
    if (
        _is_link_or_reparse(path, before)
        or not stat.S_ISREG(before.st_mode)
        or int(getattr(before, "st_nlink", 1)) != 1
    ):
        raise _UnsafePath
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _stat_identity(opened) != _stat_identity(before):
            raise _ChangedPath
        chunks: list[bytes] = []
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            chunks.append(chunk)
        after_handle = os.fstat(stream.fileno())
    after_path = os.lstat(path)
    if (
        _stable_identity(before) != _stable_identity(after_handle)
        or _stable_identity(before) != _stable_identity(after_path)
        or _is_link_or_reparse(path, after_path)
    ):
        raise _ChangedPath
    return b"".join(chunks)


def _copy_source_zip_snapshot(source: Path, destination: Path) -> tuple[int, str]:
    """Stream one stable caller file into an exclusively owned snapshot."""

    before = os.lstat(source)
    if (
        _is_link_or_reparse(source, before)
        or not stat.S_ISREG(before.st_mode)
        or int(getattr(before, "st_nlink", 1)) != 1
    ):
        raise _UnsafePath
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader:
        opened = os.fstat(reader.fileno())
        if _stat_identity(opened) != _stat_identity(before):
            raise _ChangedPath
        with destination.open("xb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                writer.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            writer.flush()
            os.fsync(writer.fileno())
        after_handle = os.fstat(reader.fileno())
    after_path = os.lstat(source)
    if (
        _stable_identity(before) != _stable_identity(after_handle)
        or _stable_identity(before) != _stable_identity(after_path)
        or _is_link_or_reparse(source, after_path)
    ):
        raise _ChangedPath
    snapshot = _capture_regular_file(destination)
    if len(snapshot) != size or hashlib.sha256(snapshot).hexdigest() != digest.hexdigest():
        raise _ChangedPath
    return size, digest.hexdigest()


def _hardlink_scan(root: Path) -> set[str]:
    problems: set[str] = set()

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as entries:
                snapshot = list(entries)
        except Exception:
            problems.add("MANIFEST_IO_ERROR")
            return
        for entry in snapshot:
            path = Path(entry.path)
            try:
                metadata = os.lstat(path)
                if _is_link_or_reparse(path, metadata):
                    problems.add("MANIFEST_TREE_UNSAFE")
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    visit(path)
                elif stat.S_ISREG(metadata.st_mode):
                    if int(getattr(metadata, "st_nlink", 1)) != 1:
                        problems.add("MANIFEST_TREE_UNSAFE")
                else:
                    problems.add("MANIFEST_TREE_UNSAFE")
            except Exception:
                problems.add("MANIFEST_IO_ERROR")

    try:
        root_metadata = os.lstat(root)
        if _is_link_or_reparse(root, root_metadata) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            return {"MANIFEST_TREE_UNSAFE"}
        visit(root)
    except Exception:
        return {"MANIFEST_IO_ERROR"}
    return problems


def validate_installer_payload_tree(
    document: object,
    addon_root: Path,
) -> list[str]:
    """Return sorted unique Task 5A codes; never raise or expose a path."""

    try:
        root = Path(addon_root)
        problems = _hardlink_scan(root)
        problems.update(
            _CANDIDATE_MANIFEST.validate_installed_candidate_tree(document, root)
        )
        problems.update(_hardlink_scan(root))
        return sorted(problems)
    except Exception:
        return ["MANIFEST_IO_ERROR"]


def _trusted_local_drive_parts(path: str | Path) -> tuple[str, tuple[str, ...]]:
    """Return one trusted DOS drive anchor and lexical single components."""

    raw = os.fspath(path)
    if not isinstance(raw, str) or raw.startswith(("\\\\", "\\??\\")):
        raise _UnsafePath
    if not Path(raw).is_absolute():
        raise _UnsafePath
    absolute = _absolute_lexical(raw)
    drive = absolute.drive
    if not re.fullmatch(r"[A-Za-z]:", drive) or absolute.anchor != drive + "\\":
        raise _UnsafePath
    components = tuple(absolute.parts[1:])
    for component in components:
        _validate_windows_component_name(component)
    return drive.upper() + "\\", components


def _validate_windows_component_name(name: str) -> None:
    if type(name) is not str or name in {"", ".", ".."}:
        raise _UnsafePath
    if name != unicodedata.normalize("NFC", name):
        raise _UnsafePath
    if any(character in name for character in '\\/:*?"<>|'):
        raise _UnsafePath
    if name[-1:] in {" ", "."}:
        raise _UnsafePath
    stem = name.split(".", 1)[0].casefold()
    if stem in {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }:
        raise _UnsafePath


def _ntstatus_to_winerror(status: int) -> int:
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    function = ntdll.RtlNtStatusToDosError
    function.argtypes = [ctypes.c_long]
    function.restype = ctypes.c_ulong
    return int(function(ctypes.c_long(status)))


def _raise_ntstatus(status: int, name: str) -> None:
    code = _ntstatus_to_winerror(status)
    if code in {2, 3}:
        raise FileNotFoundError(code, "native relative entry absent", name)
    if code in {80, 183}:
        raise FileExistsError(code, "native relative entry exists", name)
    raise OSError(code, "native relative operation failed", name)


def _nt_relative_create(
    parent_handle,
    name: str,
    *,
    desired_access: int,
    share_access: int,
    disposition: int,
    create_options: int,
):
    from ctypes import wintypes

    _validate_windows_component_name(name)

    class _UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class _ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(_UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class _IoStatusBlockUnion(ctypes.Union):
        _fields_ = [("Status", ctypes.c_long), ("Pointer", wintypes.LPVOID)]

    class _IoStatusBlock(ctypes.Structure):
        _anonymous_ = ("value",)
        _fields_ = [("value", _IoStatusBlockUnion), ("Information", ctypes.c_size_t)]

    buffer = ctypes.create_unicode_buffer(name)
    encoded_length = len(name.encode("utf-16-le"))
    unicode_name = _UnicodeString(
        encoded_length,
        encoded_length + ctypes.sizeof(ctypes.c_wchar),
        ctypes.cast(buffer, wintypes.LPWSTR),
    )
    attributes = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent_handle,
        ctypes.pointer(unicode_name),
        0x00000040,
        None,
        None,
    )
    io_status = _IoStatusBlock()
    result = wintypes.HANDLE()
    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    function = ntdll.NtCreateFile
    function.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    ]
    function.restype = ctypes.c_long
    status = int(
        function(
            ctypes.byref(result),
            desired_access,
            ctypes.byref(attributes),
            ctypes.byref(io_status),
            None,
            0x00000080,
            share_access,
            disposition,
            create_options,
            None,
            0,
        )
    )
    if status < 0:
        _raise_ntstatus(status, name)
    if not result.value:
        raise OSError("native relative operation returned no handle")
    return result.value


def _open_windows_anchor_handle(anchor: str):
    from ctypes import wintypes

    if not re.fullmatch(r"[A-Za-z]:\\", anchor):
        raise _UnsafePath
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    drive_type = kernel32.GetDriveTypeW(wintypes.LPCWSTR(anchor))
    if int(drive_type) != 3:
        raise _NativeCapabilityError
    function = kernel32.CreateFileW
    function.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    function.restype = wintypes.HANDLE
    handle = function(
        anchor,
        _FILE_LIST_DIRECTORY
        | _FILE_ADD_SUBDIRECTORY
        | _FILE_READ_ATTRIBUTES
        | _FILE_TRAVERSE
        | _SYNCHRONIZE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        3,
        0x02000000 | _FILE_OPEN_REPARSE_POINT,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _nt_open_directory_handle(parent_handle, name: str):
    return _nt_relative_create(
        parent_handle,
        name,
        desired_access=(
            _FILE_LIST_DIRECTORY
            | _FILE_ADD_SUBDIRECTORY
            | _FILE_READ_ATTRIBUTES
            | _FILE_TRAVERSE
            | _SYNCHRONIZE
        ),
        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
        disposition=_FILE_OPEN,
        create_options=(
            _FILE_DIRECTORY_FILE
            | _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_OPEN_REPARSE_POINT
        ),
    )


def _nt_create_directory_handle(
    parent_handle,
    name: str,
    *,
    root: bool = False,
    parent_component: bool = False,
):
    desired_access = (
        _FILE_LIST_DIRECTORY
        | _FILE_ADD_SUBDIRECTORY
        | _FILE_READ_ATTRIBUTES
        | _FILE_TRAVERSE
        | _SYNCHRONIZE
    )
    if not parent_component:
        desired_access |= _FILE_ADD_FILE
    if root:
        desired_access |= _DELETE
    return _nt_relative_create(
        parent_handle,
        name,
        desired_access=desired_access,
        share_access=(
            (_FILE_SHARE_READ | _FILE_SHARE_WRITE)
            if parent_component
            else 0
        ),
        disposition=_FILE_CREATE,
        create_options=(
            _FILE_DIRECTORY_FILE
            | _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_OPEN_REPARSE_POINT
        ),
    )


def _nt_create_file_handle(parent_handle, name: str):
    return _nt_relative_create(
        parent_handle,
        name,
        desired_access=(
            _FILE_READ_DATA
            | _FILE_WRITE_DATA
            | _FILE_READ_ATTRIBUTES
            | _FILE_WRITE_ATTRIBUTES
            | _SYNCHRONIZE
        ),
        share_access=0,
        disposition=_FILE_CREATE,
        create_options=(
            _FILE_NON_DIRECTORY_FILE
            | _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_OPEN_REPARSE_POINT
        ),
    )


def _query_windows_file_id(handle) -> tuple[int, bytes]:
    from ctypes import wintypes

    class _FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class _FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", _FileId128),
        ]

    information = _FileIdInfo()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileInformationByHandleEx
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    function.restype = wintypes.BOOL
    if not function(handle, 18, ctypes.byref(information), ctypes.sizeof(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(information.VolumeSerialNumber), bytes(information.FileId.Identifier)


def _query_windows_attribute_tag(handle) -> tuple[int, int]:
    from ctypes import wintypes

    class _FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    information = _FileAttributeTagInfo()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileInformationByHandleEx
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    function.restype = wintypes.BOOL
    if not function(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(information.FileAttributes), int(information.ReparseTag)


def _query_windows_link_count(handle) -> int:
    from ctypes import wintypes

    class _FileStandardInfo(ctypes.Structure):
        _fields_ = [
            ("AllocationSize", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("NumberOfLinks", wintypes.DWORD),
            ("DeletePending", wintypes.BOOLEAN),
            ("Directory", wintypes.BOOLEAN),
        ]

    information = _FileStandardInfo()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileInformationByHandleEx
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    function.restype = wintypes.BOOL
    if not function(handle, 1, ctypes.byref(information), ctypes.sizeof(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(information.NumberOfLinks)


def _query_windows_opened_name(handle) -> str:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    capacity = 32768
    buffer = ctypes.create_unicode_buffer(capacity)
    length = int(function(handle, buffer, capacity, 0))
    if length == 0 or length >= capacity:
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value.rstrip("\\")
    return value.rsplit("\\", 1)[-1]


def _query_windows_filesystem(handle) -> str:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = getattr(kernel32, "GetVolumeInformationByHandleW", None)
    if function is None:
        raise _NativeCapabilityError
    filesystem = ctypes.create_unicode_buffer(64)
    function.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    function.restype = wintypes.BOOL
    if not function(handle, None, 0, None, None, None, filesystem, len(filesystem)):
        raise ctypes.WinError(ctypes.get_last_error())
    return filesystem.value.upper()


def _windows_handle_metadata(handle) -> _WindowsHandleIdentity:
    """Return the complete authoritative identity of one retained handle."""

    volume_serial, file_id = _query_windows_file_id(handle)
    file_attributes, reparse_tag = _query_windows_attribute_tag(handle)
    file_type = (
        stat.S_IFDIR if file_attributes & _FILE_ATTRIBUTE_DIRECTORY else stat.S_IFREG
    )
    return _WindowsHandleIdentity(
        volume_serial=volume_serial,
        file_id=file_id,
        file_type=file_type,
        file_attributes=file_attributes,
        reparse_tag=reparse_tag,
    )


def _assert_same_windows_volume(
    parent: _WindowsHandleIdentity, child: _WindowsHandleIdentity
) -> None:
    if parent.volume_serial != child.volume_serial:
        raise _UnsafePath


def _assert_safe_windows_identity(
    identity: _WindowsHandleIdentity, *, expected_type: int
) -> None:
    if (
        identity.file_type != expected_type
        or identity.file_attributes & _FILE_ATTRIBUTE_REPARSE_POINT
        or identity.reparse_tag != 0
    ):
        raise _UnsafePath


def _open_windows_no_follow(
    path: Path, access: int, *, share_delete: bool = True
):
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.CreateFileW
    function.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    function.restype = wintypes.HANDLE
    share_mode = 0x00000001 | 0x00000002
    if share_delete:
        share_mode |= 0x00000004
    handle = function(
        os.fspath(path),
        access,
        share_mode,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def _close_windows_handle(handle) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.CloseHandle
    function.argtypes = [wintypes.HANDLE]
    function.restype = wintypes.BOOL
    if not function(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _force_close_windows_handle(handle) -> None:
    try:
        _close_windows_handle(handle)
    except Exception:
        try:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            function = kernel32.CloseHandle
            function.argtypes = [wintypes.HANDLE]
            function.restype = wintypes.BOOL
            function(handle)
        except Exception:
            pass


def _write_windows_file_handle(handle, payload: bytes) -> None:
    from ctypes import wintypes

    if type(payload) is not bytes:
        raise TypeError("payload must be exact bytes")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_pointer = kernel32.SetFilePointerEx
    set_pointer.argtypes = [
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    ]
    set_pointer.restype = wintypes.BOOL
    if not set_pointer(handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    write = kernel32.WriteFile
    write.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    write.restype = wintypes.BOOL
    offset = 0
    while offset < len(payload):
        chunk = payload[offset : offset + 1024 * 1024]
        buffer = ctypes.create_string_buffer(chunk)
        written = wintypes.DWORD()
        if not write(handle, buffer, len(chunk), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if int(written.value) <= 0:
            raise OSError("native file write made no progress")
        offset += int(written.value)


def _flush_windows_file_handle(handle) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.FlushFileBuffers
    function.argtypes = [wintypes.HANDLE]
    function.restype = wintypes.BOOL
    if not function(handle):
        raise ctypes.WinError(ctypes.get_last_error())


def _read_windows_file_handle(handle) -> bytes:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_pointer = kernel32.SetFilePointerEx
    set_pointer.argtypes = [
        wintypes.HANDLE,
        ctypes.c_longlong,
        ctypes.POINTER(ctypes.c_longlong),
        wintypes.DWORD,
    ]
    set_pointer.restype = wintypes.BOOL
    if not set_pointer(handle, 0, None, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    read = kernel32.ReadFile
    read.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    read.restype = wintypes.BOOL
    chunks: list[bytes] = []
    while True:
        buffer = ctypes.create_string_buffer(1024 * 1024)
        count = wintypes.DWORD()
        if not read(handle, buffer, len(buffer), ctypes.byref(count), None):
            error = ctypes.get_last_error()
            if error == 38:
                break
            raise ctypes.WinError(error)
        if count.value == 0:
            break
        chunks.append(buffer.raw[: count.value])
    return b"".join(chunks)


def _query_windows_directory_names(handle) -> tuple[str, ...]:
    """Enumerate exact child names through the already-retained directory handle."""

    from ctypes import wintypes

    class _FileIdBothDirectoryInfo(ctypes.Structure):
        _fields_ = [
            ("NextEntryOffset", wintypes.DWORD),
            ("FileIndex", wintypes.DWORD),
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("EndOfFile", ctypes.c_longlong),
            ("AllocationSize", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
            ("FileNameLength", wintypes.DWORD),
            ("EaSize", wintypes.DWORD),
            ("ShortNameLength", ctypes.c_byte),
            ("ShortName", wintypes.WCHAR * 12),
            ("FileId", ctypes.c_longlong),
            ("FileName", wintypes.WCHAR * 1),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileInformationByHandleEx
    function.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    function.restype = wintypes.BOOL
    names: list[str] = []
    restart = True
    while True:
        buffer = ctypes.create_string_buffer(64 * 1024)
        information_class = 11 if restart else 10
        restart = False
        if not function(handle, information_class, buffer, len(buffer)):
            error = ctypes.get_last_error()
            if error in {18, 38}:
                break
            raise ctypes.WinError(error)
        offset = 0
        while True:
            entry = _FileIdBothDirectoryInfo.from_buffer(buffer, offset)
            name_offset = offset + _FileIdBothDirectoryInfo.FileName.offset
            name = ctypes.wstring_at(
                ctypes.addressof(buffer) + name_offset,
                int(entry.FileNameLength) // ctypes.sizeof(ctypes.c_wchar),
            )
            if name not in {".", ".."}:
                names.append(name)
            if entry.NextEntryOffset == 0:
                break
            offset += int(entry.NextEntryOffset)
    return tuple(names)


def _dispose_windows_file_handle(handle) -> bool:
    from ctypes import wintypes

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOLEAN)]

    information = _FileDispositionInfo(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.SetFileInformationByHandle
    function.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    function.restype = wintypes.BOOL
    return bool(
        function(
            handle,
            4,
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
    )


def _rename_windows_handle(handle, parent_handle, destination: Path) -> None:
    from ctypes import wintypes

    name = Path(destination).name
    _validate_windows_component_name(name)
    target_name = os.fspath(_absolute_lexical(destination))

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * (len(target_name) + 1)),
        ]

    information = _FileRenameInfo()
    information.ReplaceIfExists = False
    information.RootDirectory = None
    information.FileNameLength = len(target_name.encode("utf-16-le"))
    information.FileName = target_name
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.SetFileInformationByHandle
    function.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    function.restype = wintypes.BOOL
    error = 0
    for attempt in range(80):
        if function(
            handle,
            3,
            ctypes.byref(information),
            ctypes.sizeof(information),
        ):
            return
        error = ctypes.get_last_error()
        if error != 32 or attempt == 79:
            break
        time.sleep(0.025)
    if error in {80, 183}:
        raise FileExistsError(error, "native target exists", name)
    if error:
        raise ctypes.WinError(error)


def _quarantine_windows_handle(handle, parent_handle, destination: Path) -> bool:
    try:
        _rename_windows_handle(handle, parent_handle, destination)
    except Exception:
        return False
    return True


def _publish_windows_directory_handle(
    handle, parent_handle, destination: Path
) -> None:
    _rename_windows_handle(handle, parent_handle, destination)


def _legacy_windows_handle_metadata(handle) -> tuple[int, int, int]:
    """Bridge the pre-existing attestation cleanup contract; never used by staging."""

    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    information = _ByHandleFileInformation()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileInformationByHandle
    function.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleFileInformation)]
    function.restype = wintypes.BOOL
    if not function(handle, ctypes.byref(information)):
        raise ctypes.WinError(ctypes.get_last_error())
    file_id = (int(information.nFileIndexHigh) << 32) | int(
        information.nFileIndexLow
    )
    file_type = (
        stat.S_IFDIR
        if int(information.dwFileAttributes) & _FILE_ATTRIBUTE_DIRECTORY
        else stat.S_IFREG
    )
    return int(information.dwVolumeSerialNumber), file_id, file_type


def _cleanup_owned(
    path: Path,
    parent_chain: tuple[tuple[Path, tuple[int, int, int]], ...],
    owned_identity: tuple[int, int, int],
) -> bool:
    """Remove/quarantine only the exact entry proven through one open handle."""

    if os.name != "nt" or not parent_chain:
        return False
    path = _absolute_lexical(path)
    parent = path.parent
    if parent_chain[-1][0] != parent:
        return False
    try:
        if _directory_chain(parent) != parent_chain:
            return False
        if not _assert_exact_child(parent, path.name, allow_absent=False):
            return False
    except Exception:
        return False

    attempts = 80
    for attempt in range(attempts):
        parent_handle = None
        child_handle = None
        try:
            parent_handle = _open_windows_no_follow(
                parent,
                0x00000001 | 0x00000020 | 0x00000080 | 0x00100000,
                share_delete=False,
            )
            child_handle = _open_windows_no_follow(
                path,
                0x00010000 | 0x00000080 | 0x00100000,
                share_delete=False,
            )
            _parent_volume, parent_file_id, parent_type = _legacy_windows_handle_metadata(
                parent_handle
            )
            _child_volume, child_file_id, child_type = _legacy_windows_handle_metadata(
                child_handle
            )
            if (
                parent_file_id != parent_chain[-1][1][1]
                or parent_type != parent_chain[-1][1][2]
                or child_file_id != owned_identity[1]
                or child_type != owned_identity[2]
            ):
                return False
            if child_type == stat.S_IFREG:
                if _dispose_windows_file_handle(child_handle):
                    return True
            elif child_type == stat.S_IFDIR:
                quarantine = parent / (
                    _STAGE_QUARANTINE_PREFIX + uuid.uuid4().hex
                )
                if _quarantine_windows_handle(
                    child_handle, parent_handle, quarantine
                ):
                    return True
            else:
                return False
        except Exception:
            pass
        finally:
            if child_handle is not None:
                _close_windows_handle(child_handle)
            if parent_handle is not None:
                _close_windows_handle(parent_handle)
        if attempt + 1 < attempts:
            time.sleep(0.025)
    return False


def _create_owned_directory(parent: Path, name: str) -> Path:
    if not _assert_exact_child(parent, name, allow_absent=True):
        path = parent / name
        os.mkdir(path)
    path = parent / name
    metadata = os.lstat(path)
    if _is_link_or_reparse(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise _UnsafePath
    return path


def _write_owned_member(root: Path, relative: str, payload: bytes) -> Path:
    parts = relative.split("/")
    current = root
    for component in parts[:-1]:
        current = _create_owned_directory(current, component)
    leaf = parts[-1]
    if _assert_exact_child(current, leaf, allow_absent=True):
        raise _UnsafePath
    destination = current / leaf
    with destination.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    metadata = os.lstat(destination)
    if (
        _is_link_or_reparse(destination, metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or int(getattr(metadata, "st_nlink", 1)) != 1
    ):
        raise _UnsafePath
    return destination


def _require_windows_stage_capabilities() -> None:
    required = (
        "_open_windows_anchor_handle",
        "_query_windows_filesystem",
        "_nt_open_directory_handle",
        "_nt_create_directory_handle",
        "_ntstatus_to_winerror",
        "_query_windows_file_id",
        "_query_windows_attribute_tag",
        "_query_windows_opened_name",
        "_windows_handle_metadata",
        "_nt_create_file_handle",
        "_write_windows_file_handle",
        "_flush_windows_file_handle",
        "_read_windows_file_handle",
        "_query_windows_directory_names",
        "_query_windows_link_count",
        "_revalidate_windows_handle_chain",
        "_close_windows_handle",
        "_quarantine_windows_handle",
    )
    if os.name != "nt" or any(not callable(globals().get(name)) for name in required):
        raise _NativeCapabilityError


def _exact_windows_child_exists(
    parent_handle, name: str, *, allow_absent: bool
) -> bool:
    _validate_windows_component_name(name)
    identity = _name_identity(name)
    matches = [
        actual
        for actual in _query_windows_directory_names(parent_handle)
        if _name_identity(actual) == identity
    ]
    if len(matches) > 1 or (matches and matches[0] != name):
        raise _UnsafePath
    if not matches and not allow_absent:
        raise _ChangedPath
    return bool(matches)


def _entry_from_handle(
    handle,
    *,
    name: str,
    path: Path,
    role: str,
    parent: _WindowsHeldEntry | None,
    expected_type: int,
) -> _WindowsHeldEntry:
    identity = _windows_handle_metadata(handle)
    _assert_safe_windows_identity(identity, expected_type=expected_type)
    if parent is not None:
        _assert_same_windows_volume(parent.identity, identity)
        if _query_windows_opened_name(handle) != name:
            raise _UnsafePath
    return _WindowsHeldEntry(
        handle=handle,
        identity=identity,
        name=name,
        path=path,
        role=role,
        parent_handle=None if parent is None else parent.handle,
    )


def _close_windows_entry(entry: _WindowsHeldEntry) -> None:
    if entry.closed:
        return
    try:
        _close_windows_handle(entry.handle)
    except Exception:
        _force_close_windows_handle(entry.handle)
        entry.closed = True
        raise
    entry.closed = True


def _close_windows_entries_nonraising(entries) -> None:
    for entry in entries:
        try:
            _close_windows_entry(entry)
        except Exception:
            pass


def _prepare_windows_stage_parent(
    path: str | Path,
) -> tuple[Path, list[_WindowsHeldEntry]]:
    _require_windows_stage_capabilities()
    anchor, components = _trusted_local_drive_parts(path)
    chain: list[_WindowsHeldEntry] = []
    try:
        anchor_handle = _open_windows_anchor_handle(anchor)
        anchor_entry = _entry_from_handle(
            anchor_handle,
            name=anchor,
            path=Path(anchor),
            role="anchor",
            parent=None,
            expected_type=stat.S_IFDIR,
        )
        chain.append(anchor_entry)
        if _query_windows_filesystem(anchor_handle) not in _WINDOWS_SUPPORTED_FILESYSTEMS:
            raise _NativeCapabilityError
        current_path = Path(anchor)
        for component in components:
            parent = chain[-1]
            exists = _exact_windows_child_exists(
                parent.handle, component, allow_absent=True
            )
            if exists:
                handle = _nt_open_directory_handle(parent.handle, component)
                role = "ancestor"
            else:
                handle = _nt_create_directory_handle(
                    parent.handle, component, parent_component=True
                )
                role = "created-parent"
            current_path = current_path / component
            chain.append(
                _entry_from_handle(
                    handle,
                    name=component,
                    path=current_path,
                    role=role,
                    parent=parent,
                    expected_type=stat.S_IFDIR,
                )
            )
        if not components:
            raise _UnsafePath
        return _absolute_lexical(path), chain
    except Exception:
        _close_windows_entries_nonraising(reversed(chain))
        raise


def _revalidate_windows_handle_chain(chain) -> None:
    if not chain:
        raise _ChangedPath
    previous: _WindowsHeldEntry | None = None
    for entry in chain:
        if entry.closed:
            raise _ChangedPath
        identity = _windows_handle_metadata(entry.handle)
        if identity != entry.identity:
            raise _ChangedPath
        _assert_safe_windows_identity(identity, expected_type=stat.S_IFDIR)
        if previous is not None:
            _assert_same_windows_volume(previous.identity, identity)
            if _query_windows_opened_name(entry.handle) != entry.name:
                raise _ChangedPath
        previous = entry


def _create_windows_stage_root(
    stage_parent: Path,
    parent_chain: list[_WindowsHeldEntry],
    name: str,
) -> _WindowsStageHandles:
    parent = parent_chain[-1]
    handle = _nt_create_directory_handle(parent.handle, name, root=True)
    root: _WindowsHeldEntry | None = None
    try:
        root = _entry_from_handle(
            handle,
            name=name,
            path=stage_parent / name,
            role="stage-root",
            parent=parent,
            expected_type=stat.S_IFDIR,
        )
        _revalidate_windows_handle_chain(parent_chain)
        return _WindowsStageHandles(
            stage_parent=stage_parent,
            parent_chain=parent_chain,
            root=root,
            directories={"": root},
            files={},
        )
    except Exception:
        _force_close_windows_handle(handle)
        raise


def _stage_create_directory(
    workspace: _WindowsStageHandles, relative: str
) -> _WindowsHeldEntry:
    relative = relative.strip("/")
    if not relative:
        return workspace.root
    current_key = ""
    current = workspace.root
    for component in relative.split("/"):
        _validate_windows_component_name(component)
        next_key = component if not current_key else current_key + "/" + component
        existing = workspace.directories.get(next_key)
        if existing is not None:
            current_key = next_key
            current = existing
            continue
        handle = _nt_create_directory_handle(current.handle, component)
        entry: _WindowsHeldEntry | None = None
        try:
            entry = _entry_from_handle(
                handle,
                name=component,
                path=current.path / component,
                role="owned-dir",
                parent=current,
                expected_type=stat.S_IFDIR,
            )
        except Exception:
            if entry is None:
                _force_close_windows_handle(handle)
            raise
        workspace.directories[next_key] = entry
        current_key = next_key
        current = entry
    return current


def _stage_write_file(
    workspace: _WindowsStageHandles,
    relative: str,
    payload: bytes,
    *,
    role: str,
) -> _WindowsHeldEntry:
    parts = relative.split("/")
    if not parts or any(not part for part in parts):
        raise _UnsafePath
    parent_key = "/".join(parts[:-1])
    parent = _stage_create_directory(workspace, parent_key)
    name = parts[-1]
    _validate_windows_component_name(name)
    if relative in workspace.files:
        raise _UnsafePath
    handle = _nt_create_file_handle(parent.handle, name)
    entry: _WindowsHeldEntry | None = None
    try:
        entry = _entry_from_handle(
            handle,
            name=name,
            path=parent.path / name,
            role=role,
            parent=parent,
            expected_type=stat.S_IFREG,
        )
        if _query_windows_link_count(handle) != 1:
            raise _UnsafePath
        workspace.files[relative] = entry
        _write_windows_file_handle(handle, payload)
        _flush_windows_file_handle(handle)
        entry.payload = payload
        return entry
    except Exception:
        if entry is None:
            _force_close_windows_handle(handle)
        raise


def _validate_windows_stage_handles(
    workspace: _WindowsStageHandles,
    document: object,
) -> list[str]:
    try:
        _revalidate_windows_handle_chain(workspace.parent_chain)
    except (_UnsafePath, _ChangedPath):
        raise _UnsafePath from None
    if _windows_handle_metadata(workspace.root.handle) != workspace.root.identity:
        raise _ChangedPath
    expected_children: dict[str, set[str]] = {
        relative: set() for relative in workspace.directories
    }
    for relative, entry in workspace.directories.items():
        if entry.closed or _windows_handle_metadata(entry.handle) != entry.identity:
            raise _ChangedPath
        _assert_safe_windows_identity(entry.identity, expected_type=stat.S_IFDIR)
        if relative:
            parent_key, _, name = relative.rpartition("/")
            expected_children[parent_key].add(name)
    for relative, entry in workspace.files.items():
        if entry.closed or entry.payload is None:
            raise _ChangedPath
        identity = _windows_handle_metadata(entry.handle)
        if identity != entry.identity or _query_windows_link_count(entry.handle) != 1:
            raise _ChangedPath
        _assert_safe_windows_identity(identity, expected_type=stat.S_IFREG)
        if _read_windows_file_handle(entry.handle) != entry.payload:
            raise _ChangedPath
        parent_key, _, name = relative.rpartition("/")
        expected_children[parent_key].add(name)
    for relative, entry in workspace.directories.items():
        names = _query_windows_directory_names(entry.handle)
        if len({_name_identity(name) for name in names}) != len(names):
            raise _UnsafePath
        if set(names) != expected_children[relative]:
            raise _ChangedPath
    return []


def _close_windows_stage_descendants(workspace: _WindowsStageHandles) -> None:
    first_error: _StageCloseError | None = None
    for entry in reversed(tuple(workspace.files.values())):
        try:
            _close_windows_entry(entry)
        except Exception:
            if first_error is None:
                first_error = _StageCloseError(entry.role)
    directories = sorted(
        (item for key, item in workspace.directories.items() if key),
        key=lambda entry: len(entry.path.parts),
        reverse=True,
    )
    for entry in directories:
        try:
            _close_windows_entry(entry)
        except Exception:
            if first_error is None:
                first_error = _StageCloseError(entry.role)
    if first_error is not None:
        raise first_error


def _close_windows_stage_authority(workspace: _WindowsStageHandles) -> None:
    _close_windows_entries_nonraising([workspace.root])
    _close_windows_entries_nonraising(reversed(workspace.parent_chain))


def _preserve_windows_stage(workspace: _WindowsStageHandles) -> bool:
    try:
        _close_windows_stage_descendants(workspace)
    except Exception:
        _close_windows_stage_authority(workspace)
        return False
    _close_windows_stage_authority(workspace)
    return True


def _cleanup_windows_stage(workspace: _WindowsStageHandles) -> bool:
    try:
        _close_windows_stage_descendants(workspace)
    except Exception:
        _close_windows_stage_authority(workspace)
        return False
    success = False
    try:
        _revalidate_windows_handle_chain(workspace.parent_chain)
        if _windows_handle_metadata(workspace.root.handle) != workspace.root.identity:
            raise _ChangedPath
        quarantine = workspace.stage_parent / (
            _STAGE_QUARANTINE_PREFIX + uuid.uuid4().hex
        )
        success = bool(
            _quarantine_windows_handle(
                workspace.root.handle,
                workspace.parent_chain[-1].handle,
                quarantine,
            )
        )
    except Exception:
        success = False
    finally:
        _close_windows_stage_authority(workspace)
    return success


def _raise_with_windows_stage_cleanup(
    token: str,
    *,
    cleanup_token: str,
    workspace: _WindowsStageHandles,
) -> None:
    if not _cleanup_windows_stage(workspace):
        token = cleanup_token
    raise RuntimeError(token) from None


def _raise_with_windows_stage_preserve(
    token: str,
    *,
    cleanup_token: str,
    workspace: _WindowsStageHandles,
) -> None:
    if not _preserve_windows_stage(workspace):
        token = cleanup_token
    raise RuntimeError(token) from None


def _stage_identity(manifest_sha256: str, source_zip_sha256: str) -> str:
    payload = {
        "installed_manifest_sha256": manifest_sha256,
        "source_zip_sha256": source_zip_sha256,
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def verify_inno_toolchain(
    iscc: str | Path,
    manifest_path: str | Path = INNO_TOOLCHAIN_MANIFEST,
) -> dict:
    """Verify that *iscc* belongs to the exact committed portable toolchain."""

    compiler = Path(iscc).resolve()
    manifest_file = Path(manifest_path).resolve()
    manifest_bytes = manifest_file.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    if manifest.get("schema") != "bcs.inno_toolchain/1.0":
        raise RuntimeError("Inno Setup toolchain manifest schema mismatch")
    if compiler.name.lower() != "iscc.exe":
        raise RuntimeError(f"Inno Setup toolchain compiler name mismatch: {compiler.name}")

    root = compiler.parent
    expected_paths: set[str] = set()
    tree_records: list[dict] = []
    for entry in manifest.get("files") or []:
        relative = str(entry.get("path") or "").replace("\\", "/")
        if not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise RuntimeError(f"Inno Setup toolchain manifest path mismatch: {relative!r}")
        expected_paths.add(relative.casefold())
        candidate = root / Path(relative)
        if not candidate.is_file():
            raise RuntimeError(f"Inno Setup toolchain file mismatch: missing {relative}")
        actual_size = candidate.stat().st_size
        actual_hash = _sha256_file(candidate)
        if actual_size != int(entry.get("size", -1)):
            raise RuntimeError(f"Inno Setup toolchain size mismatch: {relative}")
        if actual_hash != str(entry.get("sha256") or "").lower():
            raise RuntimeError(f"Inno Setup toolchain hash mismatch: {relative}")
        tree_records.append(
            {"path": relative, "size": actual_size, "sha256": actual_hash}
        )

    actual_paths = {
        path.relative_to(root).as_posix().casefold()
        for path in root.rglob("*")
        if path.is_file()
    }
    if actual_paths != expected_paths:
        missing = sorted(expected_paths - actual_paths)
        unexpected = sorted(actual_paths - expected_paths)
        raise RuntimeError(
            "Inno Setup toolchain tree mismatch: "
            f"missing={missing!r} unexpected={unexpected!r}"
        )

    tree_records.sort(key=lambda item: item["path"].casefold())
    return {
        "name": str(manifest.get("name") or "Inno Setup"),
        "version": str(manifest.get("version") or ""),
        "source_sha256": str(manifest.get("source_sha256") or "").lower(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "tree_sha256": hashlib.sha256(_canonical_json_bytes(tree_records)).hexdigest(),
    }


def _exact_stage_closure(staged_release: InstallerStage) -> None:
    root = staged_release.stage_root
    expected_top = {"PDFVectorImporter", _STAGE_METADATA_DIRNAME}
    with os.scandir(root) as entries:
        actual = {entry.name for entry in entries}
    if actual != expected_top:
        raise _ChangedPath
    metadata_root = root / _STAGE_METADATA_DIRNAME
    metadata = os.lstat(metadata_root)
    if _is_link_or_reparse(metadata_root, metadata) or not stat.S_ISDIR(
        metadata.st_mode
    ):
        raise _UnsafePath
    with os.scandir(metadata_root) as entries:
        snapshot_names = {entry.name for entry in entries}
    if snapshot_names != {staged_release.source_zip_name}:
        raise _ChangedPath


def _release_zip_member_map(
    snapshot_bytes: bytes, *, artifact_name: str
) -> tuple[dict[str, bytes] | None, list[str]]:
    """Validate and read one immutable ZIP value without reopening a pathname."""

    if type(snapshot_bytes) is not bytes:
        return None, ["RELEASE_ZIP_IO_ERROR"]
    problems = _closed_release_zip_codes(
        validate_release_zip_manifest_bytes(
            snapshot_bytes,
            artifact_name=artifact_name,
        )
    )
    if problems:
        return None, problems
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot_bytes), "r") as archive:
            members = {info.filename: archive.read(info) for info in archive.infolist()}
    except zipfile.BadZipFile:
        return None, ["RELEASE_ZIP_CORRUPT"]
    except Exception:
        return None, ["RELEASE_ZIP_IO_ERROR"]
    return members, []


def _validate_stage_exact(
    staged_release: InstallerStage,
    *,
    require_identity_name: bool = True,
) -> dict:
    if not isinstance(staged_release, InstallerStage):
        raise _ChangedPath
    if (
        type(staged_release.candidate_manifest_bytes) is not bytes
        or type(staged_release.version) is not str
        or type(staged_release.source_zip_name) is not str
        or _DIGEST.fullmatch(staged_release.source_zip_sha256) is None
        or _DIGEST.fullmatch(staged_release.installed_manifest_sha256) is None
        or _DIGEST.fullmatch(staged_release.stage_identity_sha256) is None
        or type(staged_release.source_zip_size) is not int
        or staged_release.source_zip_size < 0
    ):
        raise _ChangedPath

    stage_root = _absolute_lexical(staged_release.stage_root)
    expected_source = stage_root / "PDFVectorImporter"
    expected_snapshot = stage_root / _STAGE_METADATA_DIRNAME / staged_release.source_zip_name
    if (
        staged_release.stage_root != stage_root
        or (
            require_identity_name
            and stage_root.name != staged_release.stage_identity_sha256
        )
        or staged_release.source_dir != expected_source
        or staged_release.source_zip_snapshot != expected_snapshot
    ):
        raise _ChangedPath
    _directory_chain(stage_root)
    _exact_stage_closure(staged_release)

    snapshot = _capture_regular_file(expected_snapshot)
    if (
        len(snapshot) != staged_release.source_zip_size
        or hashlib.sha256(snapshot).hexdigest() != staged_release.source_zip_sha256
    ):
        raise _ChangedPath
    members, zip_problems = _release_zip_member_map(
        snapshot,
        artifact_name=staged_release.source_zip_name,
    )
    if zip_problems:
        raise _ChangedPath
    if members is None:
        raise _ChangedPath
    manifest_bytes = members.get(MANIFEST_MEMBER)
    if (
        type(manifest_bytes) is not bytes
        or manifest_bytes != staged_release.candidate_manifest_bytes
    ):
        raise _ChangedPath
    document, problems = _CANDIDATE_MANIFEST.parse_candidate_file_manifest(
        staged_release.candidate_manifest_bytes
    )
    if problems or document is None:
        raise _ChangedPath
    member_problems = _CANDIDATE_MANIFEST.validate_candidate_archive_members(
        document, members
    )
    if member_problems:
        raise _ChangedPath
    manifest_digest = _CANDIDATE_MANIFEST.candidate_manifest_sha256(document)
    identity = _stage_identity(manifest_digest, staged_release.source_zip_sha256)
    if (
        manifest_digest != staged_release.installed_manifest_sha256
        or identity != staged_release.stage_identity_sha256
        or document.get("package_version") != staged_release.version
        or document.get("artifact_name") != staged_release.source_zip_name
    ):
        raise _ChangedPath
    manifest_path = expected_source / MANIFEST_MEMBER.removeprefix(
        "PDFVectorImporter/"
    )
    if _capture_regular_file(manifest_path) != staged_release.candidate_manifest_bytes:
        raise _ChangedPath
    tree_problems = validate_installer_payload_tree(document, expected_source)
    if tree_problems:
        raise _ChangedPath
    if _read_package_version(expected_source / "package.xml") != staged_release.version:
        raise _ChangedPath
    _exact_stage_closure(staged_release)
    return document


def _canonical_toolchain_identity(value: object) -> dict[str, str]:
    keys = (
        "name",
        "version",
        "source_sha256",
        "manifest_sha256",
        "tree_sha256",
    )
    if type(value) is not dict or set(value) != set(keys):
        raise _ChangedPath
    result: dict[str, str] = {}
    for key in keys:
        item = value[key]
        if type(item) is not str:
            raise _ChangedPath
        result[key] = item
    for key in ("source_sha256", "manifest_sha256", "tree_sha256"):
        if _DIGEST.fullmatch(result[key]) is None:
            raise _ChangedPath
    _canonical_json_bytes(result)
    return result


def _raise_with_cleanup(
    token: str,
    *,
    cleanup_token: str,
    path: Path,
    parent_chain: tuple[tuple[Path, tuple[int, int, int]], ...],
    owned_identity: tuple[int, int, int] | None,
) -> None:
    if owned_identity is not None and not _cleanup_owned(
        path, parent_chain, owned_identity
    ):
        token = cleanup_token
    raise RuntimeError(token) from None


def write_attestation(
    output_path: str | Path,
    *,
    staged_release: InstallerStage,
    installer_exe: str | Path,
    toolchain_identity: dict,
) -> Path:
    """Atomically bind Setup, source ZIP, toolchain, and installed manifest."""

    try:
        document = _validate_stage_exact(staged_release)
    except Exception:
        raise RuntimeError("INSTALLER_ATTESTATION_INPUT_INVALID") from None

    try:
        setup_path = _absolute_lexical(installer_exe)
        setup_bytes = _capture_regular_file(setup_path)
        if _capture_regular_file(setup_path) != setup_bytes:
            raise _ChangedPath
    except _UnsafePath:
        raise RuntimeError("INSTALLER_SETUP_UNSAFE") from None
    except Exception:
        raise RuntimeError("INSTALLER_SETUP_CHANGED") from None

    try:
        canonical_toolchain = _canonical_toolchain_identity(toolchain_identity)
        payload = {
            "schema": "bcs.freecad_installer_attestation/1.1",
            "source_commit": document["source_commit"],
            "stage_identity_sha256": staged_release.stage_identity_sha256,
            "source_zip": {
                "name": staged_release.source_zip_name,
                "size": staged_release.source_zip_size,
                "sha256": staged_release.source_zip_sha256,
            },
            "installer": {
                "name": setup_path.name,
                "size": len(setup_bytes),
                "sha256": hashlib.sha256(setup_bytes).hexdigest(),
            },
            "payload_manifest": {
                "schema": document["schema"],
                "member": MANIFEST_MEMBER,
                "sha256": staged_release.installed_manifest_sha256,
            },
            "toolchain": canonical_toolchain,
        }
        encoded = _canonical_json_bytes(payload)
    except Exception:
        raise RuntimeError("INSTALLER_ATTESTATION_INPUT_INVALID") from None

    try:
        destination = _absolute_lexical(output_path)
        parent, parent_chain = _prepare_safe_directory(destination.parent)
        if destination.parent != parent:
            raise _ChangedPath
        if _assert_exact_child(parent, destination.name, allow_absent=True):
            metadata = os.lstat(destination)
            if (
                _is_link_or_reparse(destination, metadata)
                or not stat.S_ISREG(metadata.st_mode)
                or int(getattr(metadata, "st_nlink", 1)) != 1
            ):
                raise _UnsafePath
    except (_UnsafePath, _ChangedPath):
        raise RuntimeError("INSTALLER_ATTESTATION_INPUT_INVALID") from None
    except Exception:
        raise RuntimeError("INSTALLER_ATTESTATION_IO_ERROR") from None

    temporary = parent / (_ATTESTATION_TEMP_PREFIX + uuid.uuid4().hex)
    owned_identity: tuple[int, int, int] | None = None
    try:
        if _assert_exact_child(parent, temporary.name, allow_absent=True):
            raise _TempCollision
        try:
            stream = temporary.open("xb")
        except FileExistsError:
            raise _TempCollision from None
        opened = os.fstat(stream.fileno())
        owned_identity = _stat_identity(opened)
        if (
            stat.S_IFMT(opened.st_mode) != stat.S_IFREG
            or int(getattr(opened, "st_nlink", 1)) != 1
        ):
            raise _UnsafePath
        with stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        if (
            _stat_identity(os.lstat(temporary)) != owned_identity
            or _capture_regular_file(temporary) != encoded
        ):
            raise _ChangedPath
    except _TempCollision:
        raise RuntimeError("INSTALLER_ATTESTATION_IO_ERROR") from None
    except Exception:
        _raise_with_cleanup(
            "INSTALLER_ATTESTATION_IO_ERROR",
            cleanup_token="INSTALLER_ATTESTATION_IO_ERROR",
            path=temporary,
            parent_chain=parent_chain,
            owned_identity=owned_identity,
        )

    try:
        _validate_stage_exact(staged_release)
    except Exception:
        _raise_with_cleanup(
            "INSTALLER_ATTESTATION_INPUT_INVALID",
            cleanup_token="INSTALLER_ATTESTATION_IO_ERROR",
            path=temporary,
            parent_chain=parent_chain,
            owned_identity=owned_identity,
        )

    try:
        if _capture_regular_file(setup_path) != setup_bytes:
            raise _ChangedPath
    except _UnsafePath:
        _raise_with_cleanup(
            "INSTALLER_SETUP_UNSAFE",
            cleanup_token="INSTALLER_ATTESTATION_IO_ERROR",
            path=temporary,
            parent_chain=parent_chain,
            owned_identity=owned_identity,
        )
    except Exception:
        _raise_with_cleanup(
            "INSTALLER_SETUP_CHANGED",
            cleanup_token="INSTALLER_ATTESTATION_IO_ERROR",
            path=temporary,
            parent_chain=parent_chain,
            owned_identity=owned_identity,
        )

    try:
        _revalidate_chain(parent_chain)
        if _assert_exact_child(parent, destination.name, allow_absent=True):
            destination_metadata = os.lstat(destination)
            if (
                _is_link_or_reparse(destination, destination_metadata)
                or not stat.S_ISREG(destination_metadata.st_mode)
                or int(getattr(destination_metadata, "st_nlink", 1)) != 1
            ):
                raise _UnsafePath
        if (
            _stat_identity(os.lstat(temporary)) != owned_identity
            or _capture_regular_file(temporary) != encoded
        ):
            raise _ChangedPath
    except Exception:
        _raise_with_cleanup(
            "INSTALLER_ATTESTATION_INPUT_INVALID",
            cleanup_token="INSTALLER_ATTESTATION_IO_ERROR",
            path=temporary,
            parent_chain=parent_chain,
            owned_identity=owned_identity,
        )

    try:
        os.replace(temporary, destination)
    except Exception:
        _raise_with_cleanup(
            "INSTALLER_ATTESTATION_PUBLISH_ERROR",
            cleanup_token="INSTALLER_ATTESTATION_PUBLISH_ERROR",
            path=temporary,
            parent_chain=parent_chain,
            owned_identity=owned_identity,
        )
    return destination


def _read_package_version(package_xml: Path) -> str:
    if not package_xml.exists():
        raise FileNotFoundError(f"Missing package metadata: {package_xml}")

    text = package_xml.read_text(encoding="utf-8")
    match = re.search(r"<version>(.*?)</version>", text)
    if not match:
        raise RuntimeError("Could not determine version from package.xml")
    return match.group(1).strip()


def read_version() -> str:
    return _read_package_version(ADDON_DIR / "package.xml")


def find_iscc(explicit_path: str | None) -> Path:
    candidates: list[Path] = []

    if explicit_path:
        candidates.append(Path(explicit_path))

    for name in ("iscc", "ISCC.exe"):
        on_path = shutil.which(name)
        if on_path:
            candidates.append(Path(on_path))

    for env_var in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.environ.get(env_var)
        if not root:
            continue
        base = Path(root)
        candidates.append(base / "Inno Setup 6" / "ISCC.exe")
        candidates.append(base / "Inno Setup 5" / "ISCC.exe")

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    raise FileNotFoundError(
        "Inno Setup compiler (ISCC.exe) was not found.\n"
        "Install Inno Setup 6 from https://jrsoftware.org/isinfo.php "
        "or pass --iscc C:\\path\\to\\ISCC.exe."
    )


def stage_release(
    source_zip: str | Path | None = None,
    *,
    dist_dir: str | Path | None = None,
    stage_dir: str | Path | None = None,
) -> InstallerStage:
    """Validate and atomically publish one exact retained-handle stage."""

    version = read_version()
    expected_name = f"FreeCAD-PDF-Importer_v{version}.zip"
    output_root = _absolute_lexical(dist_dir if dist_dir is not None else DIST_DIR)
    if source_zip is None:
        try:
            zip_path = _absolute_lexical(build_release.build(output_root))
        except Exception:
            raise RuntimeError("INSTALLER_SOURCE_IO_ERROR") from None
    else:
        try:
            zip_path = _absolute_lexical(source_zip)
        except Exception:
            raise RuntimeError("INSTALLER_SOURCE_UNSAFE") from None
    if zip_path.name != expected_name:
        raise RuntimeError("INSTALLER_SOURCE_UNSAFE")

    try:
        stage_parent, parent_chain = _prepare_windows_stage_parent(
            stage_dir if stage_dir is not None else STAGE_DIR
        )
    except (_UnsafePath, _ChangedPath):
        raise RuntimeError("INSTALLER_STAGE_UNSAFE") from None
    except Exception:
        raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None

    temporary_name = _STAGE_TEMP_PREFIX + uuid.uuid4().hex
    try:
        workspace = _create_windows_stage_root(
            stage_parent, parent_chain, temporary_name
        )
    except (FileExistsError, _TempCollision):
        _close_windows_entries_nonraising(reversed(parent_chain))
        raise RuntimeError("INSTALLER_STAGE_UNSAFE") from None
    except (_UnsafePath, _ChangedPath):
        _close_windows_entries_nonraising(reversed(parent_chain))
        raise RuntimeError("INSTALLER_STAGE_UNSAFE") from None
    except Exception:
        _close_windows_entries_nonraising(reversed(parent_chain))
        raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None

    try:
        snapshot_bytes = _capture_regular_file(zip_path)
    except _UnsafePath:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_UNSAFE",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    except _ChangedPath:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_CHANGED",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    except Exception:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_IO_ERROR",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    source_size = len(snapshot_bytes)
    source_digest = hashlib.sha256(snapshot_bytes).hexdigest()
    snapshot_relative = _STAGE_METADATA_DIRNAME + "/" + expected_name
    try:
        _stage_write_file(
            workspace,
            snapshot_relative,
            snapshot_bytes,
            role="source-file",
        )
    except FileExistsError:
        _raise_with_windows_stage_preserve(
            "INSTALLER_SOURCE_IO_ERROR",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    except _UnsafePath:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_UNSAFE",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    except _ChangedPath:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_CHANGED",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    except Exception:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_IO_ERROR",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )

    try:
        members, problems = _release_zip_member_map(
            snapshot_bytes, artifact_name=expected_name
        )
    except Exception:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_IO_ERROR",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    if problems:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_ZIP_INVALID: " + ", ".join(sorted(set(problems))),
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    if members is None:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_ZIP_INVALID: RELEASE_ZIP_IO_ERROR",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )

    try:
        manifest_bytes = members[MANIFEST_MEMBER]
        document, parse_problems = _CANDIDATE_MANIFEST.parse_candidate_file_manifest(
            manifest_bytes
        )
        if parse_problems or document is None:
            invalid_codes = sorted(
                set(parse_problems or ["MANIFEST_INVALID_DOCUMENT"])
            )
        else:
            invalid_codes = _CANDIDATE_MANIFEST.validate_candidate_archive_members(
                document, members
            )
            if (
                document.get("package_version") != version
                or document.get("artifact_name") != expected_name
            ):
                invalid_codes = sorted(
                    {*invalid_codes, "MANIFEST_INVALID_IDENTITY"}
                )
    except Exception:
        invalid_codes = ["MANIFEST_IO_ERROR"]
        document = None
    if invalid_codes or document is None:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_SOURCE_ZIP_INVALID: "
            + ", ".join(sorted(set(invalid_codes)),),
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )

    installed_manifest_sha256 = _CANDIDATE_MANIFEST.candidate_manifest_sha256(
        document
    )
    stage_identity_sha256 = _stage_identity(
        installed_manifest_sha256, source_digest
    )
    try:
        _stage_create_directory(workspace, "PDFVectorImporter")
        manifest_relative = MANIFEST_MEMBER.removeprefix("PDFVectorImporter/")
        _stage_write_file(
            workspace,
            "PDFVectorImporter/" + manifest_relative,
            manifest_bytes,
            role="member-file",
        )
        for record in document["files"]:
            relative = record["path"]
            _stage_write_file(
                workspace,
                "PDFVectorImporter/" + relative,
                members["PDFVectorImporter/" + relative],
                role="member-file",
            )
    except FileExistsError:
        _raise_with_windows_stage_preserve(
            "INSTALLER_STAGE_IO_ERROR",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    except _UnsafePath:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_STAGE_UNSAFE",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    except Exception:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_STAGE_IO_ERROR",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )

    try:
        tree_problems = _validate_windows_stage_handles(workspace, document)
    except _UnsafePath:
        _raise_with_windows_stage_preserve(
            "INSTALLER_STAGE_UNSAFE",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    except Exception:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_STAGE_TREE_INVALID",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    if tree_problems:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_STAGE_TREE_INVALID: "
            + ", ".join(sorted(set(tree_problems))),
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )

    temporary = workspace.root.path
    source_dir = temporary / "PDFVectorImporter"
    try:
        _close_windows_stage_descendants(workspace)
    except _StageCloseError as exc:
        token = (
            "INSTALLER_SOURCE_IO_ERROR"
            if exc.role == "source-file"
            else "INSTALLER_STAGE_IO_ERROR"
        )
        _raise_with_windows_stage_cleanup(
            token,
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    try:
        package_version = _read_package_version(source_dir / "package.xml")
    except Exception:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_STAGE_IO_ERROR",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    if package_version != version:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_STAGE_TREE_INVALID",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    try:
        _revalidate_windows_handle_chain(parent_chain)
    except (_UnsafePath, _ChangedPath):
        _raise_with_windows_stage_preserve(
            "INSTALLER_STAGE_UNSAFE",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    try:
        if _windows_handle_metadata(workspace.root.handle) != workspace.root.identity:
            raise _ChangedPath
    except Exception:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_STAGE_TREE_INVALID",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )

    final_root = stage_parent / stage_identity_sha256
    final_stage = InstallerStage(
        version=version,
        stage_root=final_root,
        source_dir=final_root / "PDFVectorImporter",
        source_zip_snapshot=final_root / _STAGE_METADATA_DIRNAME / expected_name,
        source_zip_name=expected_name,
        source_zip_size=source_size,
        source_zip_sha256=source_digest,
        candidate_manifest_bytes=manifest_bytes,
        installed_manifest_sha256=installed_manifest_sha256,
        stage_identity_sha256=stage_identity_sha256,
    )

    try:
        target_exists = _exact_windows_child_exists(
            parent_chain[-1].handle,
            stage_identity_sha256,
            allow_absent=True,
        )
    except Exception:
        _raise_with_windows_stage_preserve(
            "INSTALLER_STAGE_UNSAFE",
            cleanup_token="INSTALLER_STAGE_IO_ERROR",
            workspace=workspace,
        )
    if target_exists:
        target_entry: _WindowsHeldEntry | None = None
        try:
            target_handle = _nt_open_directory_handle(
                parent_chain[-1].handle, stage_identity_sha256
            )
            target_entry = _entry_from_handle(
                target_handle,
                name=stage_identity_sha256,
                path=final_root,
                role="winner",
                parent=parent_chain[-1],
                expected_type=stat.S_IFDIR,
            )
            _validate_stage_exact(final_stage)
        except Exception:
            if target_entry is not None:
                _close_windows_entries_nonraising([target_entry])
            _raise_with_windows_stage_cleanup(
                "INSTALLER_STAGE_CONFLICT",
                cleanup_token="INSTALLER_STAGE_IO_ERROR",
                workspace=workspace,
            )
        _close_windows_entries_nonraising([target_entry])
        if not _cleanup_windows_stage(workspace):
            raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None
        return final_stage

    if not callable(globals().get("_publish_windows_directory_handle")):
        _cleanup_windows_stage(workspace)
        raise RuntimeError("INSTALLER_STAGE_PUBLISH_ERROR") from None
    try:
        _publish_windows_directory_handle(
            workspace.root.handle,
            parent_chain[-1].handle,
            final_root,
        )
    except FileExistsError:
        winner: _WindowsHeldEntry | None = None
        winner_is_exact = False
        try:
            winner_handle = _nt_open_directory_handle(
                parent_chain[-1].handle, stage_identity_sha256
            )
            winner = _entry_from_handle(
                winner_handle,
                name=stage_identity_sha256,
                path=final_root,
                role="winner",
                parent=parent_chain[-1],
                expected_type=stat.S_IFDIR,
            )
            _validate_stage_exact(final_stage)
            winner_is_exact = True
        except Exception:
            winner_is_exact = False
        finally:
            if winner is not None:
                _close_windows_entries_nonraising([winner])
        if winner_is_exact:
            if not _cleanup_windows_stage(workspace):
                raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None
            return final_stage
        _cleanup_windows_stage(workspace)
        raise RuntimeError("INSTALLER_STAGE_CONFLICT") from None
    except Exception:
        _cleanup_windows_stage(workspace)
        raise RuntimeError("INSTALLER_STAGE_PUBLISH_ERROR") from None

    _close_windows_stage_authority(workspace)
    return final_stage


def compile_installer(
    iscc: Path,
    staged_release: InstallerStage,
    *,
    output_dir: str | Path | None = None,
) -> Path:
    """Revalidate the exact stage before invoking the pinned compiler."""

    try:
        _validate_stage_exact(staged_release)
        output_root, _output_chain = _prepare_safe_directory(
            output_dir if output_dir is not None else DIST_DIR
        )
    except Exception:
        raise RuntimeError("INSTALLER_COMPILER_INPUT_INVALID") from None
    base_name = f"FreeCAD-PDF-Importer-Setup_v{staged_release.version}"
    cmd = [
        str(iscc),
        str(INNO_SCRIPT),
        f"/DMyAppVersion={staged_release.version}",
        f"/DSourceDir={staged_release.source_dir}",
        f"/O{output_root}",
        f"/F{base_name}",
    ]
    print("Running pinned Inno Setup compiler")
    try:
        subprocess.run(cmd, check=True, cwd=REPO_ROOT)
    except (subprocess.CalledProcessError, OSError):
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None

    installer_exe = output_root / f"{base_name}.exe"
    try:
        metadata = os.lstat(installer_exe)
        if _is_link_or_reparse(installer_exe, metadata) or not stat.S_ISREG(
            metadata.st_mode
        ):
            raise _UnsafePath
    except Exception:
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None
    return installer_exe


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build PDFVectorImporter Windows installer (.exe)"
    )
    parser.add_argument(
        "--iscc",
        default=None,
        help="Path to ISCC.exe (Inno Setup compiler). Optional if ISCC is on PATH.",
    )
    parser.add_argument(
        "--toolchain-manifest",
        default=str(INNO_TOOLCHAIN_MANIFEST),
        help="Exact portable Inno Setup toolchain manifest.",
    )
    parser.add_argument(
        "--verify-toolchain-only",
        action="store_true",
        help="Verify the selected compiler tree and exit without building.",
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--stage-dir", default=None)
    parser.add_argument(
        "--attestation",
        default=None,
        help="Write deterministic installer/toolchain attestation JSON here.",
    )
    parser.add_argument(
        "--source-commit",
        default=os.environ.get("GITHUB_SHA"),
        help="Optional exact equality assertion for the manifest source commit.",
    )
    parser.add_argument(
        "--source-zip",
        default=None,
        help=(
            "Use this already-published release ZIP as the installer payload. "
            "The ZIP is validated and never rebuilt or modified."
        ),
    )
    args = parser.parse_args()

    iscc = find_iscc(args.iscc)
    toolchain_identity = verify_inno_toolchain(iscc, args.toolchain_manifest)
    if args.verify_toolchain_only:
        print(json.dumps(toolchain_identity, sort_keys=True))
        return 0

    staged_release = stage_release(
        args.source_zip,
        dist_dir=args.output_dir,
        stage_dir=args.stage_dir,
    )
    if args.source_commit is not None:
        document, problems = _CANDIDATE_MANIFEST.parse_candidate_file_manifest(
            staged_release.candidate_manifest_bytes
        )
        if (
            problems
            or document is None
            or _COMMIT.fullmatch(args.source_commit) is None
            or args.source_commit != document.get("source_commit")
        ):
            raise RuntimeError("INSTALLER_COMPILER_INPUT_INVALID")
    installer_exe = compile_installer(
        iscc,
        staged_release,
        output_dir=args.output_dir,
    )
    if args.attestation:
        write_attestation(
            args.attestation,
            staged_release=staged_release,
            installer_exe=installer_exe,
            toolchain_identity=toolchain_identity,
        )

    print("")
    print(f"Release zip: {staged_release.source_zip_snapshot}")
    print(f"Installer:   {installer_exe}")
    print(f"Stage dir:   {staged_release.source_dir}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover - entrypoint safety
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
