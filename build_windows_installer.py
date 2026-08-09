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
_OUTPUT_TEMP_PREFIX = ".installer-output-"
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
)
_FILE_ATTRIBUTE_DIRECTORY = getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10)
_WINDOWS_SUPPORTED_FILESYSTEMS = frozenset({"NTFS"})
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
_FILE_SHARE_DELETE = 0x00000004
_FILE_OPEN = 0x00000001
_FILE_CREATE = 0x00000002
_FILE_DIRECTORY_FILE = 0x00000001
_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_FILE_NON_DIRECTORY_FILE = 0x00000040
_FILE_OPEN_REPARSE_POINT = 0x00200000
_FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
_FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
_FILE_NOTIFY_CHANGE_ATTRIBUTES = 0x00000004
_FILE_NOTIFY_CHANGE_SIZE = 0x00000008
_FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
_FILE_NOTIFY_CHANGE_CREATION = 0x00000040
_FILE_NOTIFY_CHANGE_SECURITY = 0x00000100
_STAGE_NOTIFY_FILTER = (
    _FILE_NOTIFY_CHANGE_FILE_NAME
    | _FILE_NOTIFY_CHANGE_DIR_NAME
    | _FILE_NOTIFY_CHANGE_ATTRIBUTES
    | _FILE_NOTIFY_CHANGE_SIZE
    | _FILE_NOTIFY_CHANGE_LAST_WRITE
    | _FILE_NOTIFY_CHANGE_CREATION
    | _FILE_NOTIFY_CHANGE_SECURITY
)
_ERROR_IO_PENDING = 997
_ERROR_IO_INCOMPLETE = 996
_ERROR_OPERATION_ABORTED = 995
_ERROR_NOT_FOUND = 1168
_WAIT_OBJECT_0 = 0
_WAIT_TIMEOUT = 258
_COMMIT = re.compile(r"\A[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_VERSION = re.compile(r"\A(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_OUTPUT_IDENTITY_DOMAIN = b"BCS-FREECAD-COMPILED-INSTALLER-IDENTITY\0v1\0"
_PATH_TYPE = type(Path())
_CAPABILITY_SEAL = object()


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


@dataclass(frozen=True, repr=False)
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

    def __repr__(self) -> str:
        return "InstallerStage(<sealed>)"


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
class _WindowsStageMonitor:
    entry: _WindowsHeldEntry
    event_handle: object
    overlapped: object
    buffer: object
    bytes_returned: object
    queued: bool = False
    active: bool = True
    event_closed: bool = False


_WINDOWS_STAGE_MONITOR_TERMINAL_OWNERS: list[_WindowsStageMonitor] = []


@dataclass
class _WindowsStageHandles:
    stage_parent: Path
    parent_chain: list[_WindowsHeldEntry]
    root: _WindowsHeldEntry
    directories: dict[str, _WindowsHeldEntry]
    files: dict[str, _WindowsHeldEntry]


@dataclass(repr=False)
class _WindowsStageReadLease:
    staged_release: InstallerStage
    parent_chain: list[_WindowsHeldEntry]
    root: _WindowsHeldEntry
    directories: dict[str, _WindowsHeldEntry]
    files: dict[str, _WindowsHeldEntry]
    document: dict
    monitor: _WindowsStageMonitor | None = None
    owns_root: bool = True
    active: bool = True
    capability_token: object | None = None

    def __repr__(self) -> str:
        return "_WindowsStageReadLease(<sealed>)"


@dataclass(repr=False)
class _WindowsOutputLease:
    parent_chain: list[_WindowsHeldEntry]
    leaf_entries: list[_WindowsHeldEntry]
    loose_path: Path
    setup_bytes: bytes
    setup_size: int
    setup_sha256: str
    setup_basename: str
    output_identity_bytes: bytes
    output_identity_sha256: str
    provenance: str
    active: bool = True
    capability_token: object | None = None

    def __repr__(self) -> str:
        return "_WindowsOutputLease(<sealed>)"


@dataclass
class _CapabilityState:
    active: bool = True


@dataclass(frozen=True, repr=False)
class CompiledInstaller:
    staged_release: InstallerStage
    stage_lease: _WindowsStageReadLease
    output_lease: _WindowsOutputLease
    setup_bytes: bytes
    setup_size: int
    setup_sha256: str
    setup_basename: str
    output_identity_bytes: bytes
    output_identity_sha256: str
    toolchain_name: str
    toolchain_version: str
    toolchain_source_sha256: str
    toolchain_manifest_sha256: str
    toolchain_tree_sha256: str
    toolchain_identity_bytes: bytes
    binding_sha256: str
    capability_token: object
    _state: _CapabilityState
    _seal: object

    def __repr__(self) -> str:
        return "CompiledInstaller(<sealed>)"

    def __enter__(self):
        if not _is_active_compiled_installer(self):
            raise RuntimeError("INSTALLER_COMPILER_INPUT_INVALID")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        _consume_compiled_installer(self)
        return False


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


def _exact_lexical_path(value: object) -> Path:
    if type(value) is str:
        raw = value
    elif type(value) is _PATH_TYPE:
        raw = str(value)
    else:
        raise _UnsafePath
    return _absolute_lexical(raw)


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


def _open_windows_anchor_handle(anchor: str, *, add_file: bool = False):
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
        | (_FILE_ADD_FILE if add_file else _FILE_ADD_SUBDIRECTORY)
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


def _open_windows_anchor_read_handle(anchor: str):
    from ctypes import wintypes

    if not re.fullmatch(r"[A-Za-z]:\\", anchor):
        raise _UnsafePath
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if int(kernel32.GetDriveTypeW(wintypes.LPCWSTR(anchor))) != 3:
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
        _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _FILE_TRAVERSE | _SYNCHRONIZE,
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


def _nt_open_directory_read_handle(
    parent_handle,
    name: str,
    *,
    share_write: bool = True,
):
    return _nt_relative_create(
        parent_handle,
        name,
        desired_access=(
            _FILE_LIST_DIRECTORY
            | _FILE_READ_ATTRIBUTES
            | _FILE_TRAVERSE
            | _SYNCHRONIZE
        ),
        share_access=(
            _FILE_SHARE_READ | (_FILE_SHARE_WRITE if share_write else 0)
        ),
        disposition=_FILE_OPEN,
        create_options=(
            _FILE_DIRECTORY_FILE
            | _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_OPEN_REPARSE_POINT
        ),
    )


def _nt_open_directory_monitor_handle(parent_handle, name: str):
    """Open one asynchronous, read-shared directory notification authority."""

    return _nt_relative_create(
        parent_handle,
        name,
        desired_access=(
            _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _FILE_TRAVERSE
        ),
        share_access=_FILE_SHARE_READ,
        disposition=_FILE_OPEN,
        create_options=_FILE_DIRECTORY_FILE | _FILE_OPEN_REPARSE_POINT,
    )


def _nt_open_file_read_handle(
    parent_handle,
    name: str,
    *,
    share_write: bool = False,
    share_delete: bool = False,
):
    share_access = _FILE_SHARE_READ
    if share_write:
        share_access |= _FILE_SHARE_WRITE
    if share_delete:
        share_access |= _FILE_SHARE_DELETE
    return _nt_relative_create(
        parent_handle,
        name,
        desired_access=_FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        share_access=share_access,
        disposition=_FILE_OPEN,
        create_options=(
            _FILE_NON_DIRECTORY_FILE
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


def _query_windows_final_path(handle) -> Path:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    required = int(function(handle, None, 0, 0))
    if required <= 0:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = int(function(handle, buffer, len(buffer), 0))
    if written <= 0 or written >= len(buffer):
        raise ctypes.WinError(ctypes.get_last_error())
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return _absolute_lexical(value)


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


def _duplicate_windows_handle(handle):
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    current = kernel32.GetCurrentProcess()
    duplicate = wintypes.HANDLE()
    function = kernel32.DuplicateHandle
    function.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    function.restype = wintypes.BOOL
    if not function(current, handle, current, ctypes.byref(duplicate), 0, False, 0x2):
        raise ctypes.WinError(ctypes.get_last_error())
    if not duplicate.value:
        raise OSError("native handle duplication returned no handle")
    return duplicate.value


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


def _entry_from_new_handle(handle, **entry_arguments) -> _WindowsHeldEntry:
    """Transfer one raw native handle only after entry validation succeeds."""

    try:
        return _entry_from_handle(handle, **entry_arguments)
    except Exception:
        _force_close_windows_handle(handle)
        raise


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
        anchor_handle = _open_windows_anchor_read_handle(anchor)
        anchor_entry = _entry_from_new_handle(
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
            current_path = current_path / component
            if exists:
                handle = _nt_open_directory_read_handle(
                    parent.handle, component, share_write=True
                )
                chain.append(
                    _entry_from_new_handle(
                        handle,
                        name=component,
                        path=current_path,
                        role="ancestor",
                        parent=parent,
                        expected_type=stat.S_IFDIR,
                    )
                )
                continue

            creator: _WindowsHeldEntry | None = None
            created: _WindowsHeldEntry | None = None
            retained: _WindowsHeldEntry | None = None
            creator_handle = None
            created_handle = None
            retained_handle = None
            try:
                if len(chain) == 1:
                    creator_handle = _open_windows_anchor_handle(anchor)
                    creator_parent = None
                else:
                    creator_parent = chain[-2]
                    creator_handle = _nt_open_directory_handle(
                        creator_parent.handle, parent.name
                    )
                creator = _entry_from_handle(
                    creator_handle,
                    name=parent.name,
                    path=parent.path,
                    role="parent-creator-view",
                    parent=creator_parent,
                    expected_type=stat.S_IFDIR,
                )
                if creator.identity != parent.identity:
                    raise _ChangedPath
                _revalidate_windows_handle_chain(chain)
                if _exact_windows_child_exists(
                    creator.handle, component, allow_absent=True
                ):
                    raise _TempCollision
                created_handle = _nt_create_directory_handle(
                    creator.handle, component, parent_component=True
                )
                created = _entry_from_handle(
                    created_handle,
                    name=component,
                    path=current_path,
                    role="parent-created-view",
                    parent=creator,
                    expected_type=stat.S_IFDIR,
                )
                retained_handle = _nt_open_directory_read_handle(
                    creator.handle, component, share_write=True
                )
                retained = _entry_from_handle(
                    retained_handle,
                    name=component,
                    path=current_path,
                    role="created-parent",
                    parent=parent,
                    expected_type=stat.S_IFDIR,
                )
                if retained.identity != created.identity:
                    raise _ChangedPath
                chain.append(retained)
                _close_windows_entry(created)
                _close_windows_entry(creator)
            except Exception:
                if retained is None and retained_handle is not None:
                    _force_close_windows_handle(retained_handle)
                if created is None and created_handle is not None:
                    _force_close_windows_handle(created_handle)
                if creator is None and creator_handle is not None:
                    _force_close_windows_handle(creator_handle)
                if retained is not None and retained not in chain:
                    _close_windows_entries_nonraising([retained])
                _close_windows_entries_nonraising(
                    [entry for entry in (created, creator) if entry is not None]
                )
                raise
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


def _open_windows_parent_creator_view(
    chain: list[_WindowsHeldEntry],
    *,
    role: str,
    add_file: bool = False,
) -> _WindowsHeldEntry:
    if not chain:
        raise _ChangedPath
    parent = chain[-1]
    raw_handle = None
    try:
        if len(chain) == 1:
            raw_handle = _open_windows_anchor_handle(
                parent.name, add_file=add_file
            )
            creator_parent = None
        else:
            creator_parent = chain[-2]
            if add_file:
                raw_handle = _nt_relative_create(
                    creator_parent.handle,
                    parent.name,
                    desired_access=(
                        _FILE_LIST_DIRECTORY
                        | _FILE_ADD_FILE
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
            else:
                raw_handle = _nt_open_directory_handle(
                    creator_parent.handle, parent.name
                )
        creator = _entry_from_handle(
            raw_handle,
            name=parent.name,
            path=parent.path,
            role=role,
            parent=creator_parent,
            expected_type=stat.S_IFDIR,
        )
        if creator.identity != parent.identity:
            raise _ChangedPath
        _revalidate_windows_handle_chain(chain)
        return creator
    except Exception:
        if raw_handle is not None:
            _force_close_windows_handle(raw_handle)
        raise


def _create_windows_stage_root(
    stage_parent: Path,
    parent_chain: list[_WindowsHeldEntry],
    name: str,
) -> _WindowsStageHandles:
    parent = parent_chain[-1]
    if _exact_windows_child_exists(parent.handle, name, allow_absent=True):
        raise _TempCollision
    creator = _open_windows_parent_creator_view(
        parent_chain, role="stage-parent-creator"
    )
    handle = None
    root: _WindowsHeldEntry | None = None
    try:
        if _exact_windows_child_exists(creator.handle, name, allow_absent=True):
            raise _TempCollision
        handle = _nt_create_directory_handle(creator.handle, name, root=True)
        root = _entry_from_handle(
            handle,
            name=name,
            path=stage_parent / name,
            role="stage-root",
            parent=parent,
            expected_type=stat.S_IFDIR,
        )
        _revalidate_windows_handle_chain(parent_chain)
        _close_windows_entry(creator)
        return _WindowsStageHandles(
            stage_parent=stage_parent,
            parent_chain=parent_chain,
            root=root,
            directories={"": root},
            files={},
        )
    except Exception:
        if handle is not None:
            _force_close_windows_handle(handle)
        _close_windows_entries_nonraising([creator])
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
    if type(document) is not dict:
        raise _ChangedPath
    package_entry = workspace.files.get("PDFVectorImporter/package.xml")
    if (
        package_entry is None
        or package_entry.payload is None
        or _parse_package_version_bytes(package_entry.payload)
        != document.get("package_version")
    ):
        raise _ChangedPath
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


def _validate_installer_stage_descriptor(staged_release: object) -> dict:
    if type(staged_release) is not InstallerStage:
        raise _ChangedPath
    if (
        type(staged_release.version) is not str
        or _VERSION.fullmatch(staged_release.version) is None
        or type(staged_release.source_zip_name) is not str
        or type(staged_release.source_zip_size) is not int
        or staged_release.source_zip_size < 0
        or type(staged_release.source_zip_sha256) is not str
        or _DIGEST.fullmatch(staged_release.source_zip_sha256) is None
        or type(staged_release.candidate_manifest_bytes) is not bytes
        or type(staged_release.installed_manifest_sha256) is not str
        or _DIGEST.fullmatch(staged_release.installed_manifest_sha256) is None
        or type(staged_release.stage_identity_sha256) is not str
        or _DIGEST.fullmatch(staged_release.stage_identity_sha256) is None
        or type(staged_release.stage_root) is not _PATH_TYPE
        or type(staged_release.source_dir) is not _PATH_TYPE
        or type(staged_release.source_zip_snapshot) is not _PATH_TYPE
    ):
        raise _ChangedPath
    stage_root = _exact_lexical_path(staged_release.stage_root)
    expected_name = f"FreeCAD-PDF-Importer_v{staged_release.version}.zip"
    if (
        staged_release.stage_root != stage_root
        or stage_root.name != staged_release.stage_identity_sha256
        or staged_release.source_zip_name != expected_name
        or staged_release.source_dir != stage_root / "PDFVectorImporter"
        or staged_release.source_zip_snapshot
        != stage_root / _STAGE_METADATA_DIRNAME / expected_name
    ):
        raise _ChangedPath
    document, problems = _CANDIDATE_MANIFEST.parse_candidate_file_manifest(
        staged_release.candidate_manifest_bytes
    )
    if problems or type(document) is not dict:
        raise _ChangedPath
    if (
        document.get("package_version") != staged_release.version
        or document.get("artifact_name") != staged_release.source_zip_name
        or _CANDIDATE_MANIFEST.candidate_manifest_sha256(document)
        != staged_release.installed_manifest_sha256
        or _stage_identity(
            staged_release.installed_manifest_sha256,
            staged_release.source_zip_sha256,
        )
        != staged_release.stage_identity_sha256
    ):
        raise _ChangedPath
    return document


def _open_windows_existing_directory_chain(
    path: Path,
    *,
    role_prefix: str,
) -> list[_WindowsHeldEntry]:
    anchor, components = _trusted_local_drive_parts(path)
    chain: list[_WindowsHeldEntry] = []
    try:
        handle = _open_windows_anchor_read_handle(anchor)
        entry = _entry_from_new_handle(
            handle,
            name=anchor,
            path=Path(anchor),
            role=role_prefix + "-anchor",
            parent=None,
            expected_type=stat.S_IFDIR,
        )
        chain.append(entry)
        if _query_windows_filesystem(handle) != "NTFS":
            raise _NativeCapabilityError
        current_path = Path(anchor)
        for index, component in enumerate(components):
            parent = chain[-1]
            if not _exact_windows_child_exists(
                parent.handle, component, allow_absent=False
            ):
                raise _ChangedPath
            handle = _nt_open_directory_read_handle(
                parent.handle,
                component,
                share_write=index != len(components) - 1,
            )
            current_path /= component
            role = (
                role_prefix + "-root"
                if index == len(components) - 1
                else role_prefix + "-ancestor"
            )
            chain.append(
                _entry_from_new_handle(
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
        return chain
    except Exception:
        _close_windows_entries_nonraising(reversed(chain))
        raise


def _stage_expected_payloads(
    staged_release: InstallerStage,
    document: dict,
    snapshot_bytes: bytes,
) -> dict[str, bytes]:
    if (
        type(snapshot_bytes) is not bytes
        or len(snapshot_bytes) != staged_release.source_zip_size
        or hashlib.sha256(snapshot_bytes).hexdigest()
        != staged_release.source_zip_sha256
    ):
        raise _ChangedPath
    members, problems = _release_zip_member_map(
        snapshot_bytes,
        artifact_name=staged_release.source_zip_name,
    )
    if problems or members is None:
        raise _ChangedPath
    if members.get(MANIFEST_MEMBER) != staged_release.candidate_manifest_bytes:
        raise _ChangedPath
    if _CANDIDATE_MANIFEST.validate_candidate_archive_members(document, members):
        raise _ChangedPath
    payloads = {
        _STAGE_METADATA_DIRNAME + "/" + staged_release.source_zip_name: snapshot_bytes
    }
    payloads.update(members)
    return payloads


def _create_windows_stage_monitor_event():
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_event = kernel32.CreateEventW
    create_event.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    create_event.restype = wintypes.HANDLE
    event_handle = create_event(None, True, False, None)
    if not event_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return event_handle


def _queue_windows_stage_monitor(monitor: _WindowsStageMonitor) -> None:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    read_changes = kernel32.ReadDirectoryChangesW
    read_changes.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    read_changes.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    queued = read_changes(
        monitor.entry.handle,
        monitor.buffer,
        ctypes.sizeof(monitor.buffer),
        True,
        _STAGE_NOTIFY_FILTER,
        ctypes.byref(monitor.bytes_returned),
        ctypes.byref(monitor.overlapped),
        None,
    )
    if not queued and ctypes.get_last_error() != _ERROR_IO_PENDING:
        raise ctypes.WinError(ctypes.get_last_error())
    monitor.queued = True


def _cancel_windows_stage_monitor(
    monitor: _WindowsStageMonitor,
) -> tuple[bool, int]:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    cancel = kernel32.CancelIoEx
    cancel.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    cancel.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    cancelled = bool(cancel(monitor.entry.handle, ctypes.byref(monitor.overlapped)))
    return cancelled, 0 if cancelled else int(ctypes.get_last_error())


def _arm_windows_stage_monitor(
    lease: _WindowsStageReadLease,
) -> _WindowsStageMonitor:
    """Arm one recursive NTFS change notification from retained root authority."""

    from ctypes import wintypes

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    if (
        type(lease) is not _WindowsStageReadLease
        or not lease.active
        or not lease.parent_chain
        or lease.monitor is not None
    ):
        raise _ChangedPath
    raw_handle = None
    event_handle = None
    entry: _WindowsHeldEntry | None = None
    monitor: _WindowsStageMonitor | None = None
    try:
        raw_handle = _nt_open_directory_monitor_handle(
            lease.parent_chain[-1].handle, lease.root.name
        )
        entry = _entry_from_handle(
            raw_handle,
            name=lease.root.name,
            path=lease.staged_release.stage_root,
            role="stage-monitor",
            parent=lease.parent_chain[-1],
            expected_type=stat.S_IFDIR,
        )
        if (
            entry.identity != lease.root.identity
            or _query_windows_final_path(entry.handle)
            != lease.staged_release.stage_root
        ):
            raise _ChangedPath

        event_handle = _create_windows_stage_monitor_event()

        overlapped = _Overlapped()
        overlapped.hEvent = event_handle
        buffer = ctypes.create_string_buffer(32 * 1024)
        bytes_returned = wintypes.DWORD()
        monitor = _WindowsStageMonitor(
            entry=entry,
            event_handle=event_handle,
            overlapped=overlapped,
            buffer=buffer,
            bytes_returned=bytes_returned,
        )
        _queue_windows_stage_monitor(monitor)
        _validate_windows_stage_monitor(monitor)
        return monitor
    except Exception:
        if monitor is not None:
            try:
                _release_windows_stage_monitor(monitor)
            except Exception:
                pass
        else:
            if event_handle is not None:
                _force_close_windows_handle(event_handle)
            if entry is not None:
                _close_windows_entries_nonraising([entry])
            elif raw_handle is not None:
                _force_close_windows_handle(raw_handle)
        raise


def _wait_windows_stage_monitor(monitor: _WindowsStageMonitor, timeout: int) -> int:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wait = kernel32.WaitForSingleObject
    wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    wait.restype = wintypes.DWORD
    return int(wait(monitor.event_handle, timeout))


def _windows_stage_monitor_result(monitor: _WindowsStageMonitor) -> tuple[bool, int]:
    from ctypes import wintypes

    transferred = wintypes.DWORD()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_result = kernel32.GetOverlappedResult
    get_result.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
    ]
    get_result.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    succeeded = bool(
        get_result(
            monitor.entry.handle,
            ctypes.byref(monitor.overlapped),
            ctypes.byref(transferred),
            False,
        )
    )
    return succeeded, int(transferred.value if succeeded else ctypes.get_last_error())


def _validate_windows_stage_monitor(monitor: _WindowsStageMonitor) -> None:
    if (
        type(monitor) is not _WindowsStageMonitor
        or not monitor.active
        or not monitor.queued
    ):
        raise _ChangedPath
    wait_result = _wait_windows_stage_monitor(monitor, 0)
    if wait_result == _WAIT_TIMEOUT:
        return
    if wait_result != _WAIT_OBJECT_0:
        raise _NativeCapabilityError
    succeeded, _value = _windows_stage_monitor_result(monitor)
    if succeeded:
        raise _ChangedPath
    raise _NativeCapabilityError


def _retain_windows_stage_monitor_terminal_owner(
    monitor: _WindowsStageMonitor,
) -> None:
    if all(owner is not monitor for owner in _WINDOWS_STAGE_MONITOR_TERMINAL_OWNERS):
        _WINDOWS_STAGE_MONITOR_TERMINAL_OWNERS.append(monitor)


def _discard_windows_stage_monitor_terminal_owner(
    monitor: _WindowsStageMonitor,
) -> None:
    _WINDOWS_STAGE_MONITOR_TERMINAL_OWNERS[:] = [
        owner
        for owner in _WINDOWS_STAGE_MONITOR_TERMINAL_OWNERS
        if owner is not monitor
    ]


def _observe_windows_stage_monitor_terminal(
    monitor: _WindowsStageMonitor,
    *,
    cancellation_expected: bool,
) -> tuple[bool, Exception | None]:
    try:
        succeeded, result = _windows_stage_monitor_result(monitor)
    except Exception:
        return False, _NativeCapabilityError()
    if succeeded:
        return True, _ChangedPath()
    if result == _ERROR_IO_INCOMPLETE:
        return False, _NativeCapabilityError()
    if result == _ERROR_OPERATION_ABORTED and cancellation_expected:
        return True, None
    return True, _NativeCapabilityError()


def _release_windows_stage_monitor(monitor: _WindowsStageMonitor) -> None:
    """Prove overlapped completion before closing one monitor exactly once."""

    if type(monitor) is not _WindowsStageMonitor or not monitor.active:
        return
    monitor.active = False
    first_error: Exception | None = None
    completion_proven = not monitor.queued

    if monitor.queued:
        try:
            wait_before = _wait_windows_stage_monitor(monitor, 0)
        except Exception:
            wait_before = None
            first_error = _NativeCapabilityError()
        if wait_before == _WAIT_OBJECT_0:
            completion_proven, result_error = _observe_windows_stage_monitor_terminal(
                monitor,
                cancellation_expected=False,
            )
            if result_error is not None and first_error is None:
                first_error = result_error
        elif wait_before != _WAIT_TIMEOUT and first_error is None:
            first_error = _NativeCapabilityError()

        if not completion_proven:
            try:
                cancelled, cancel_error = _cancel_windows_stage_monitor(monitor)
            except Exception:
                cancelled, cancel_error = False, 0
            if not cancelled and first_error is None:
                first_error = _NativeCapabilityError()
            try:
                wait_after = _wait_windows_stage_monitor(monitor, 5000)
            except Exception:
                wait_after = None
                if first_error is None:
                    first_error = _NativeCapabilityError()
            if wait_after == _WAIT_OBJECT_0:
                completion_proven, result_error = (
                    _observe_windows_stage_monitor_terminal(
                        monitor,
                        cancellation_expected=cancelled,
                    )
                )
                if result_error is not None and first_error is None:
                    first_error = result_error
            elif first_error is None:
                first_error = _NativeCapabilityError()
            if not cancelled and cancel_error == _ERROR_NOT_FOUND and first_error is None:
                first_error = _NativeCapabilityError()

    if not completion_proven:
        # Closing either handle before the OVERLAPPED request reaches a
        # terminal result would permit native code to outlive its Python
        # buffer. Keep the complete monitor authority alive for process life.
        _retain_windows_stage_monitor_terminal_owner(monitor)
        raise first_error or _NativeCapabilityError()

    monitor.queued = False
    _discard_windows_stage_monitor_terminal_owner(monitor)
    try:
        _close_windows_entry(monitor.entry)
    except Exception as exc:
        if first_error is None:
            first_error = exc
    if not monitor.event_closed:
        try:
            _close_windows_handle(monitor.event_handle)
        except Exception as exc:
            _force_close_windows_handle(monitor.event_handle)
            if first_error is None:
                first_error = exc
        monitor.event_closed = True
    if first_error is not None:
        raise first_error


def _release_windows_stage_monitor_nonraising(
    monitor: _WindowsStageMonitor,
) -> None:
    try:
        _release_windows_stage_monitor(monitor)
    except Exception:
        pass


def _retire_windows_stage_monitor(lease: _WindowsStageReadLease) -> None:
    if (
        type(lease) is not _WindowsStageReadLease
        or not lease.active
        or type(lease.monitor) is not _WindowsStageMonitor
        or not lease.monitor.active
    ):
        raise _ChangedPath
    _validate_windows_stage_monitor(lease.monitor)
    _release_windows_stage_monitor(lease.monitor)


def _handoff_windows_stage_monitor(lease: _WindowsStageReadLease) -> None:
    """Pre-prove shutdown while preserving continuous recursive authority."""

    if (
        type(lease) is not _WindowsStageReadLease
        or not lease.active
        or type(lease.monitor) is not _WindowsStageMonitor
        or not lease.monitor.active
    ):
        raise _ChangedPath
    retiring = lease.monitor
    _validate_windows_stage_monitor(retiring)

    # The old request stays armed while its successor is opened and queued.
    # Releasing the old request proves the fallible cancellation/close path
    # before publication, while the successor remains authoritative through
    # the final non-replacing commit and is consumed non-raising afterward.
    lease.monitor = None
    try:
        successor = _arm_windows_stage_monitor(lease)
    except Exception:
        lease.monitor = retiring
        raise
    successor.entry.role = "stage-monitor-successor"
    lease.monitor = successor
    try:
        _release_windows_stage_monitor(retiring)
    except Exception:
        _release_windows_stage_monitor_nonraising(successor)
        raise


def _open_windows_stage_descendants(
    staged_release: InstallerStage,
    document: dict,
    parent_chain: list[_WindowsHeldEntry],
    root: _WindowsHeldEntry,
    *,
    owns_root: bool,
) -> _WindowsStageReadLease:
    directories: dict[str, _WindowsHeldEntry] = {"": root}
    files: dict[str, _WindowsHeldEntry] = {}
    opened_directories: list[_WindowsHeldEntry] = []
    opened_files: list[_WindowsHeldEntry] = []
    try:
        if root.closed or _query_windows_opened_name(root.handle) != staged_release.stage_root.name:
            raise _ChangedPath
        snapshot_parent_handle = _nt_open_directory_read_handle(
            root.handle, _STAGE_METADATA_DIRNAME, share_write=False
        )
        snapshot_parent = _entry_from_new_handle(
            snapshot_parent_handle,
            name=_STAGE_METADATA_DIRNAME,
            path=staged_release.stage_root / _STAGE_METADATA_DIRNAME,
            role="stage-dir:" + _STAGE_METADATA_DIRNAME,
            parent=root,
            expected_type=stat.S_IFDIR,
        )
        directories[_STAGE_METADATA_DIRNAME] = snapshot_parent
        opened_directories.append(snapshot_parent)
        snapshot_handle = _nt_open_file_read_handle(
            snapshot_parent.handle, staged_release.source_zip_name
        )
        snapshot_entry = _entry_from_new_handle(
            snapshot_handle,
            name=staged_release.source_zip_name,
            path=staged_release.source_zip_snapshot,
            role="stage-file:" + _STAGE_METADATA_DIRNAME + "/zip",
            parent=snapshot_parent,
            expected_type=stat.S_IFREG,
        )
        opened_files.append(snapshot_entry)
        if _query_windows_link_count(snapshot_handle) != 1:
            raise _UnsafePath
        snapshot_entry.payload = _read_windows_file_handle(snapshot_handle)
        snapshot_relative = _STAGE_METADATA_DIRNAME + "/" + staged_release.source_zip_name
        files[snapshot_relative] = snapshot_entry
        payloads = _stage_expected_payloads(
            staged_release, document, snapshot_entry.payload
        )

        expected_directories = {"PDFVectorImporter", _STAGE_METADATA_DIRNAME}
        for relative in payloads:
            parts = relative.split("/")
            for length in range(1, len(parts)):
                expected_directories.add("/".join(parts[:length]))
        for relative in sorted(
            expected_directories - {_STAGE_METADATA_DIRNAME},
            key=lambda value: (value.count("/"), value),
        ):
            parent_key, _, name = relative.rpartition("/")
            parent = directories[parent_key]
            handle = _nt_open_directory_read_handle(
                parent.handle, name, share_write=False
            )
            entry = _entry_from_new_handle(
                handle,
                name=name,
                path=staged_release.stage_root / Path(relative),
                role="stage-dir:" + relative,
                parent=parent,
                expected_type=stat.S_IFDIR,
            )
            directories[relative] = entry
            opened_directories.append(entry)
        for relative, expected in sorted(payloads.items()):
            if relative == snapshot_relative:
                continue
            parent_key, _, name = relative.rpartition("/")
            parent = directories[parent_key]
            handle = _nt_open_file_read_handle(parent.handle, name)
            entry = _entry_from_new_handle(
                handle,
                name=name,
                path=staged_release.stage_root / Path(relative),
                role="stage-file:" + relative,
                parent=parent,
                expected_type=stat.S_IFREG,
            )
            opened_files.append(entry)
            if _query_windows_link_count(handle) != 1:
                raise _UnsafePath
            entry.payload = _read_windows_file_handle(handle)
            if entry.payload != expected:
                raise _ChangedPath
            files[relative] = entry
        lease = _WindowsStageReadLease(
            staged_release=staged_release,
            parent_chain=parent_chain,
            root=root,
            directories=directories,
            files=files,
            document=document,
            owns_root=owns_root,
        )
        _validate_windows_stage_read_lease(lease)
        return lease
    except Exception as original_error:
        close_error: _StageCloseError | None = None
        for entry in [*reversed(opened_files), *reversed(opened_directories)]:
            try:
                _close_windows_entry(entry)
            except Exception:
                if close_error is None:
                    close_error = _StageCloseError(entry.role)
        if owns_root:
            _close_windows_entries_nonraising([root])
            _close_windows_entries_nonraising(reversed(parent_chain))
        if close_error is not None:
            raise close_error from None
        raise original_error


def _acquire_windows_stage_read_lease(
    staged_release: InstallerStage,
    *,
    adopted_root: _WindowsHeldEntry | None = None,
    adopted_parent_chain: list[_WindowsHeldEntry] | None = None,
) -> _WindowsStageReadLease:
    document = _validate_installer_stage_descriptor(staged_release)
    if adopted_root is not None or adopted_parent_chain is not None:
        if (
            type(adopted_root) is not _WindowsHeldEntry
            or type(adopted_parent_chain) is not list
            or not adopted_parent_chain
        ):
            raise _ChangedPath
        return _open_windows_stage_descendants(
            staged_release,
            document,
            adopted_parent_chain,
            adopted_root,
            owns_root=False,
        )
    chain = _open_windows_existing_directory_chain(
        staged_release.stage_root,
        role_prefix="stage",
    )
    root = chain[-1]
    lease = _open_windows_stage_descendants(
        staged_release,
        document,
        chain[:-1],
        root,
        owns_root=True,
    )
    try:
        lease.monitor = _arm_windows_stage_monitor(lease)
        _validate_windows_stage_read_lease(lease)
        return lease
    except Exception:
        _release_windows_stage_read_lease_nonraising(lease)
        raise


def _validate_windows_stage_read_lease(lease: _WindowsStageReadLease) -> dict:
    if type(lease) is not _WindowsStageReadLease or not lease.active:
        raise _ChangedPath
    if lease.monitor is not None:
        _validate_windows_stage_monitor(lease.monitor)
    staged_release = lease.staged_release
    document = _validate_installer_stage_descriptor(staged_release)
    _revalidate_windows_handle_chain([*lease.parent_chain, lease.root])
    payloads = _stage_expected_payloads(
        staged_release,
        document,
        lease.files[
            _STAGE_METADATA_DIRNAME + "/" + staged_release.source_zip_name
        ].payload,
    )
    expected_children: dict[str, set[str]] = {
        relative: set() for relative in lease.directories
    }
    for relative, entry in lease.directories.items():
        if entry.closed or _windows_handle_metadata(entry.handle) != entry.identity:
            raise _ChangedPath
        _assert_safe_windows_identity(entry.identity, expected_type=stat.S_IFDIR)
        if relative:
            parent_key, _, name = relative.rpartition("/")
            if _query_windows_opened_name(entry.handle) != name:
                raise _UnsafePath
            expected_children[parent_key].add(name)
    if set(lease.files) != set(payloads):
        raise _ChangedPath
    for relative, expected in payloads.items():
        entry = lease.files[relative]
        if (
            entry.closed
            or _windows_handle_metadata(entry.handle) != entry.identity
            or _query_windows_link_count(entry.handle) != 1
            or _query_windows_opened_name(entry.handle) != relative.rsplit("/", 1)[-1]
        ):
            raise _ChangedPath
        _assert_safe_windows_identity(entry.identity, expected_type=stat.S_IFREG)
        payload = _read_windows_file_handle(entry.handle)
        if payload != expected or entry.payload != expected:
            raise _ChangedPath
        parent_key, _, name = relative.rpartition("/")
        expected_children[parent_key].add(name)
    for relative, entry in lease.directories.items():
        names = _query_windows_directory_names(entry.handle)
        if len({_name_identity(name) for name in names}) != len(names):
            raise _UnsafePath
        if set(names) != expected_children[relative]:
            raise _ChangedPath
    package_entry = lease.files.get("PDFVectorImporter/package.xml")
    if package_entry is None:
        raise _ChangedPath
    package_bytes = _read_windows_file_handle(package_entry.handle)
    if (
        package_bytes != package_entry.payload
        or _parse_package_version_bytes(package_bytes)
        != document.get("package_version")
        or document.get("package_version") != staged_release.version
        or document.get("artifact_name") != staged_release.source_zip_name
    ):
        raise _ChangedPath
    if lease.monitor is not None:
        _validate_windows_stage_monitor(lease.monitor)
    return document


def _release_windows_stage_read_lease(lease: _WindowsStageReadLease) -> None:
    if type(lease) is not _WindowsStageReadLease or not lease.active:
        return
    lease.active = False
    first_error: Exception | None = None
    if lease.monitor is not None:
        try:
            _release_windows_stage_monitor(lease.monitor)
        except Exception as exc:
            first_error = exc
    for entry in reversed(tuple(lease.files.values())):
        try:
            _close_windows_entry(entry)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    descendants = sorted(
        (entry for relative, entry in lease.directories.items() if relative),
        key=lambda entry: len(entry.path.parts),
        reverse=True,
    )
    for entry in descendants:
        try:
            _close_windows_entry(entry)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if lease.owns_root:
        for entry in [lease.root, *reversed(lease.parent_chain)]:
            try:
                _close_windows_entry(entry)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
    if first_error is not None:
        raise first_error


def _release_windows_stage_read_lease_nonraising(
    lease: _WindowsStageReadLease | object,
) -> None:
    if type(lease) is not _WindowsStageReadLease:
        return
    try:
        _release_windows_stage_read_lease(lease)
    except Exception:
        pass


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


def _compiled_toolchain_identity(value: CompiledInstaller) -> dict[str, str]:
    return _canonical_toolchain_identity(
        {
            "name": value.toolchain_name,
            "version": value.toolchain_version,
            "source_sha256": value.toolchain_source_sha256,
            "manifest_sha256": value.toolchain_manifest_sha256,
            "tree_sha256": value.toolchain_tree_sha256,
        }
    )


def _canonical_compiled_installer_identity_bytes(
    *,
    setup_basename: str,
    setup_sha256: str,
    setup_size: int,
    stage_identity_sha256: str,
    toolchain_identity: dict[str, str],
) -> bytes:
    canonical_toolchain = _canonical_toolchain_identity(toolchain_identity)
    if (
        type(setup_basename) is not str
        or re.fullmatch(
            r"FreeCAD-PDF-Importer-Setup_v(?:0|[1-9][0-9]*)\."
            r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.exe",
            setup_basename,
        )
        is None
        or type(setup_sha256) is not str
        or _DIGEST.fullmatch(setup_sha256) is None
        or type(setup_size) is not int
        or setup_size <= 0
        or type(stage_identity_sha256) is not str
        or _DIGEST.fullmatch(stage_identity_sha256) is None
    ):
        raise _ChangedPath
    return _canonical_json_bytes(
        {
            "schema": "bcs.freecad_compiled_installer/1.0",
            "setup_basename": setup_basename,
            "setup_sha256": setup_sha256,
            "setup_size": setup_size,
            "stage_identity_sha256": stage_identity_sha256,
            "toolchain": canonical_toolchain,
        }
    )


def _prepare_windows_output_parent(path: Path) -> tuple[Path, list[_WindowsHeldEntry]]:
    output, chain = _prepare_windows_stage_parent(path)
    for index, entry in enumerate(chain):
        if index == 0:
            entry.role = "output-anchor"
        elif index == len(chain) - 1:
            entry.role = "output-parent"
        else:
            entry.role = "output-ancestor"
    if _query_windows_filesystem(chain[0].handle) != "NTFS":
        _close_windows_entries_nonraising(reversed(chain))
        raise _NativeCapabilityError
    return output, chain


def _create_windows_output_root(
    output_parent: Path,
    parent_chain: list[_WindowsHeldEntry],
    name: str,
) -> _WindowsHeldEntry:
    parent = parent_chain[-1]
    if _exact_windows_child_exists(parent.handle, name, allow_absent=True):
        raise _TempCollision
    creator = _open_windows_parent_creator_view(
        parent_chain, role="output-parent-creator"
    )
    handle = None
    try:
        if _exact_windows_child_exists(creator.handle, name, allow_absent=True):
            raise _TempCollision
        handle = _nt_relative_create(
            creator.handle,
            name,
            desired_access=(
                _FILE_LIST_DIRECTORY
                | _FILE_ADD_FILE
                | _FILE_READ_ATTRIBUTES
                | _FILE_TRAVERSE
                | _DELETE
                | _SYNCHRONIZE
            ),
            share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
            disposition=_FILE_CREATE,
            create_options=(
                _FILE_DIRECTORY_FILE
                | _FILE_SYNCHRONOUS_IO_NONALERT
                | _FILE_OPEN_REPARSE_POINT
            ),
        )
        entry = _entry_from_handle(
            handle,
            name=name,
            path=output_parent / name,
            role="output-root",
            parent=parent,
            expected_type=stat.S_IFDIR,
        )
        _close_windows_entry(creator)
        return entry
    except Exception:
        if handle is not None:
            _force_close_windows_handle(handle)
        _close_windows_entries_nonraising([creator])
        raise


def _create_windows_output_guard(
    root: _WindowsHeldEntry,
    setup_basename: str,
) -> _WindowsHeldEntry:
    handle = _nt_relative_create(
        root.handle,
        setup_basename,
        desired_access=_FILE_READ_ATTRIBUTES | _DELETE | _SYNCHRONIZE,
        share_access=_FILE_SHARE_READ | _FILE_SHARE_WRITE,
        disposition=_FILE_CREATE,
        create_options=(
            _FILE_NON_DIRECTORY_FILE
            | _FILE_SYNCHRONOUS_IO_NONALERT
            | _FILE_OPEN_REPARSE_POINT
        ),
    )
    try:
        entry = _entry_from_handle(
            handle,
            name=setup_basename,
            path=root.path / setup_basename,
            role="output-guard",
            parent=root,
            expected_type=stat.S_IFREG,
        )
        if _query_windows_link_count(handle) != 1:
            raise _UnsafePath
        return entry
    except Exception:
        _force_close_windows_handle(handle)
        raise


def _open_windows_output_reader(
    root: _WindowsHeldEntry,
    guard: _WindowsHeldEntry,
) -> _WindowsHeldEntry:
    handle = _nt_open_file_read_handle(
        root.handle,
        guard.name,
        share_delete=True,
    )
    try:
        entry = _entry_from_handle(
            handle,
            name=guard.name,
            path=guard.path,
            role="output-reader",
            parent=root,
            expected_type=stat.S_IFREG,
        )
        if entry.identity != guard.identity or _query_windows_link_count(handle) != 1:
            raise _ChangedPath
        return entry
    except Exception:
        _force_close_windows_handle(handle)
        raise


def _duplicate_output_root_entry(root: _WindowsHeldEntry) -> _WindowsHeldEntry:
    handle = _duplicate_windows_handle(root.handle)
    return _WindowsHeldEntry(
        handle=handle,
        identity=root.identity,
        name=root.name,
        path=root.path,
        role="output-root-cleanup",
        parent_handle=root.parent_handle,
    )


def _cleanup_windows_output_candidate(
    root: _WindowsHeldEntry,
    guard: _WindowsHeldEntry | None,
    reader: _WindowsHeldEntry | None,
    *,
    require_empty: bool,
) -> bool:
    duplicate: _WindowsHeldEntry | None = None
    terminal_close = False
    leaf_disposed = guard is None
    try:
        duplicate = _duplicate_output_root_entry(root)
    except Exception:
        terminal_close = True
    if guard is not None and not guard.closed:
        try:
            leaf_disposed = bool(_dispose_windows_file_handle(guard.handle))
        except Exception:
            leaf_disposed = False
    for entry in (reader, guard):
        if entry is None:
            continue
        try:
            _close_windows_entry(entry)
        except Exception:
            terminal_close = True
    try:
        _close_windows_entry(root)
    except Exception:
        terminal_close = True
    root_disposed = False
    if duplicate is not None:
        if not terminal_close and leaf_disposed:
            try:
                root_disposed = bool(_dispose_windows_file_handle(duplicate.handle))
            except Exception:
                root_disposed = False
        try:
            _close_windows_entry(duplicate)
        except Exception:
            terminal_close = True
    if terminal_close:
        return False
    if require_empty:
        return leaf_disposed and root_disposed
    return leaf_disposed


def _release_windows_output_root_after_publication(root: _WindowsHeldEntry) -> None:
    duplicate: _WindowsHeldEntry | None = None
    closed_cleanly = True
    try:
        duplicate = _duplicate_output_root_entry(root)
    except Exception:
        closed_cleanly = False
    try:
        _close_windows_entry(root)
    except Exception:
        closed_cleanly = False
    if duplicate is None:
        return
    if closed_cleanly:
        try:
            _dispose_windows_file_handle(duplicate.handle)
        except Exception:
            pass
    try:
        _close_windows_entry(duplicate)
    except Exception:
        pass


def _open_windows_output_winner(
    output_parent: Path,
    parent_chain: list[_WindowsHeldEntry],
    setup_basename: str,
) -> _WindowsHeldEntry:
    parent = parent_chain[-1]
    if not _exact_windows_child_exists(
        parent.handle, setup_basename, allow_absent=False
    ):
        raise _ChangedPath
    handle = _nt_open_file_read_handle(parent.handle, setup_basename)
    try:
        entry = _entry_from_handle(
            handle,
            name=setup_basename,
            path=output_parent / setup_basename,
            role="output-winner",
            parent=parent,
            expected_type=stat.S_IFREG,
        )
        if _query_windows_link_count(handle) != 1:
            raise _UnsafePath
        return entry
    except Exception:
        _force_close_windows_handle(handle)
        raise


def _output_identity_for_bytes(
    staged_release: InstallerStage,
    setup_basename: str,
    setup_bytes: bytes,
    toolchain_identity: dict[str, str],
) -> tuple[bytes, str, str]:
    if type(setup_bytes) is not bytes or not setup_bytes:
        raise _ChangedPath
    setup_sha256 = hashlib.sha256(setup_bytes).hexdigest()
    encoded = _canonical_compiled_installer_identity_bytes(
        setup_basename=setup_basename,
        setup_sha256=setup_sha256,
        setup_size=len(setup_bytes),
        stage_identity_sha256=staged_release.stage_identity_sha256,
        toolchain_identity=toolchain_identity,
    )
    return encoded, hashlib.sha256(_OUTPUT_IDENTITY_DOMAIN + encoded).hexdigest(), setup_sha256


def _validate_windows_output_namespace(lease: _WindowsOutputLease) -> None:
    if type(lease) is not _WindowsOutputLease or not lease.active:
        raise _ChangedPath
    _revalidate_windows_handle_chain(lease.parent_chain)
    if _query_windows_filesystem(lease.parent_chain[0].handle) != "NTFS":
        raise _UnsafePath
    if type(lease.loose_path) is not _PATH_TYPE:
        raise _UnsafePath
    if lease.loose_path != _exact_lexical_path(lease.loose_path):
        raise _ChangedPath
    if lease.loose_path.name != lease.setup_basename:
        raise _UnsafePath
    if not lease.leaf_entries:
        raise _ChangedPath
    authoritative = lease.leaf_entries[0]
    expected_identity = authoritative.identity
    for entry in lease.leaf_entries:
        if (
            entry.closed
            or entry.identity != expected_identity
            or _windows_handle_metadata(entry.handle) != expected_identity
            or _query_windows_link_count(entry.handle) != 1
            or _query_windows_opened_name(entry.handle) != lease.setup_basename
        ):
            raise _ChangedPath
        _assert_safe_windows_identity(entry.identity, expected_type=stat.S_IFREG)
    if _query_windows_final_path(authoritative.handle) != lease.loose_path:
        raise _ChangedPath


def _validate_windows_output_bytes(lease: _WindowsOutputLease) -> bytes:
    _validate_windows_output_namespace(lease)
    payload = _read_windows_file_handle(lease.leaf_entries[0].handle)
    if (
        type(payload) is not bytes
        or payload != lease.setup_bytes
        or len(payload) != lease.setup_size
        or hashlib.sha256(payload).hexdigest() != lease.setup_sha256
    ):
        raise _ChangedPath
    return payload


def _compiled_binding_sha256(
    stage_identity_sha256: str,
    output_identity_sha256: str,
    toolchain_identity_bytes: bytes,
) -> str:
    return hashlib.sha256(
        b"BCS-FREECAD-COMPILED-CAPABILITY\0v1\0"
        + stage_identity_sha256.encode("ascii")
        + output_identity_sha256.encode("ascii")
        + toolchain_identity_bytes
    ).hexdigest()


def _is_active_compiled_installer(value: object) -> bool:
    if type(value) is not CompiledInstaller:
        return False
    try:
        return bool(
            value._seal is _CAPABILITY_SEAL
            and type(value._state) is _CapabilityState
            and value._state.active
            and type(value.capability_token) is object
            and value.stage_lease.capability_token is value.capability_token
            and value.output_lease.capability_token is value.capability_token
        )
    except Exception:
        return False


def _validate_compiled_installer_binding(value: CompiledInstaller) -> None:
    if not _is_active_compiled_installer(value):
        raise _ChangedPath
    if (
        value.staged_release is not value.stage_lease.staged_release
        or value.output_lease.setup_bytes != value.setup_bytes
        or value.output_lease.setup_size != value.setup_size
        or value.output_lease.setup_sha256 != value.setup_sha256
        or value.output_lease.setup_basename != value.setup_basename
        or value.output_lease.output_identity_bytes != value.output_identity_bytes
        or value.output_lease.output_identity_sha256 != value.output_identity_sha256
    ):
        raise _ChangedPath
    canonical_toolchain = _compiled_toolchain_identity(value)
    toolchain_bytes = _canonical_json_bytes(canonical_toolchain)
    if toolchain_bytes != value.toolchain_identity_bytes:
        raise _ChangedPath
    expected_identity = _canonical_compiled_installer_identity_bytes(
        setup_basename=value.setup_basename,
        setup_sha256=value.setup_sha256,
        setup_size=value.setup_size,
        stage_identity_sha256=value.staged_release.stage_identity_sha256,
        toolchain_identity=canonical_toolchain,
    )
    if (
        expected_identity != value.output_identity_bytes
        or hashlib.sha256(_OUTPUT_IDENTITY_DOMAIN + expected_identity).hexdigest()
        != value.output_identity_sha256
        or _compiled_binding_sha256(
            value.staged_release.stage_identity_sha256,
            value.output_identity_sha256,
            toolchain_bytes,
        )
        != value.binding_sha256
    ):
        raise _ChangedPath


def _release_windows_output_lease_nonraising(lease: object) -> None:
    if type(lease) is not _WindowsOutputLease or not lease.active:
        return
    lease.active = False
    for entry in lease.leaf_entries:
        try:
            _close_windows_entry(entry)
        except Exception:
            pass
    for entry in reversed(lease.parent_chain):
        try:
            _close_windows_entry(entry)
        except Exception:
            pass


def _consume_compiled_installer(value: object) -> None:
    if type(value) is not CompiledInstaller:
        return
    try:
        if type(value._state) is not _CapabilityState or not value._state.active:
            return
        value._state.active = False
    except Exception:
        return
    _release_windows_output_lease_nonraising(value.output_lease)
    _release_windows_stage_read_lease_nonraising(value.stage_lease)


def finalize_compiled_installer(compiled_installer: CompiledInstaller) -> Path:
    if not _is_active_compiled_installer(compiled_installer):
        raise RuntimeError("INSTALLER_COMPILER_INPUT_INVALID") from None
    token: str | None = None
    result: Path | None = None
    try:
        try:
            _validate_windows_stage_read_lease(compiled_installer.stage_lease)
        except Exception:
            token = "INSTALLER_COMPILER_INPUT_INVALID"
        if token is None:
            try:
                _validate_windows_output_namespace(compiled_installer.output_lease)
            except Exception:
                token = "INSTALLER_SETUP_UNSAFE"
        if token is None:
            try:
                _validate_windows_output_bytes(compiled_installer.output_lease)
            except Exception:
                token = "INSTALLER_SETUP_CHANGED"
        if token is None:
            try:
                _validate_compiled_installer_binding(compiled_installer)
            except Exception:
                token = "INSTALLER_COMPILER_INPUT_INVALID"
        if token is None:
            try:
                _retire_windows_stage_monitor(compiled_installer.stage_lease)
            except Exception:
                token = "INSTALLER_COMPILER_INPUT_INVALID"
        if token is None:
            result = compiled_installer.output_lease.loose_path
    finally:
        _consume_compiled_installer(compiled_installer)
    if token is not None:
        raise RuntimeError(token) from None
    assert result is not None
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


def _prepare_windows_attestation_parent(
    path: Path,
) -> tuple[Path, list[_WindowsHeldEntry]]:
    parent, chain = _prepare_windows_output_parent(path)
    for index, entry in enumerate(chain):
        if index == 0:
            entry.role = "attestation-anchor"
        elif index == len(chain) - 1:
            entry.role = "attestation-parent"
        else:
            entry.role = "attestation-ancestor"
    return parent, chain


def _create_windows_attestation_temp(
    parent: Path,
    parent_chain: list[_WindowsHeldEntry],
    name: str,
) -> tuple[_WindowsHeldEntry, _WindowsHeldEntry]:
    if _exact_windows_child_exists(
        parent_chain[-1].handle, name, allow_absent=True
    ):
        raise _TempCollision
    creator = _open_windows_parent_creator_view(
        parent_chain,
        role="attestation-parent-creator",
        add_file=True,
    )
    handle = None
    work_handle = None
    try:
        if _exact_windows_child_exists(creator.handle, name, allow_absent=True):
            raise _TempCollision
        handle = _nt_relative_create(
            creator.handle,
            name,
            desired_access=(
                _FILE_READ_DATA
                | _FILE_WRITE_DATA
                | _FILE_READ_ATTRIBUTES
                | _FILE_WRITE_ATTRIBUTES
                | _DELETE
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
        entry = _entry_from_handle(
            handle,
            name=name,
            path=parent / name,
            role="attestation-commit",
            parent=parent_chain[-1],
            expected_type=stat.S_IFREG,
        )
        file_id = _query_windows_file_id(handle)
        file_attributes, reparse_tag = _query_windows_attribute_tag(handle)
        direct_identity = _WindowsHandleIdentity(
            *file_id,
            file_type=(
                stat.S_IFDIR
                if file_attributes & _FILE_ATTRIBUTE_DIRECTORY
                else stat.S_IFREG
            ),
            file_attributes=file_attributes,
            reparse_tag=reparse_tag,
        )
        if entry.identity != direct_identity or _query_windows_link_count(handle) != 1:
            raise _UnsafePath
        work_handle = _duplicate_windows_handle(handle)
        work_entry = _WindowsHeldEntry(
            handle=work_handle,
            identity=entry.identity,
            name=entry.name,
            path=entry.path,
            role="attestation-temp",
            parent_handle=entry.parent_handle,
        )
        _close_windows_entry(creator)
        return entry, work_entry
    except Exception:
        if work_handle is not None:
            _force_close_windows_handle(work_handle)
        if handle is not None:
            _force_close_windows_handle(handle)
        _close_windows_entries_nonraising([creator])
        raise


def _cleanup_windows_attestation_temp(entry: _WindowsHeldEntry | None) -> bool:
    if entry is None:
        return True
    duplicate: _WindowsHeldEntry | None = None
    try:
        duplicate = _WindowsHeldEntry(
            handle=_duplicate_windows_handle(entry.handle),
            identity=entry.identity,
            name=entry.name,
            path=entry.path,
            role="attestation-temp-cleanup",
            parent_handle=entry.parent_handle,
        )
    except Exception:
        duplicate = None
    close_succeeded = True
    try:
        _close_windows_entry(entry)
    except Exception:
        close_succeeded = False
    disposed = False
    if duplicate is not None:
        if close_succeeded:
            try:
                disposed = bool(_dispose_windows_file_handle(duplicate.handle))
            except Exception:
                disposed = False
        try:
            _close_windows_entry(duplicate)
        except Exception:
            close_succeeded = False
    return close_succeeded and disposed


def _open_windows_attestation_winner(
    destination: Path,
    parent_chain: list[_WindowsHeldEntry],
) -> _WindowsHeldEntry:
    if not _exact_windows_child_exists(
        parent_chain[-1].handle, destination.name, allow_absent=False
    ):
        raise _ChangedPath
    handle = _nt_open_file_read_handle(parent_chain[-1].handle, destination.name)
    try:
        entry = _entry_from_handle(
            handle,
            name=destination.name,
            path=destination,
            role="attestation-winner",
            parent=parent_chain[-1],
            expected_type=stat.S_IFREG,
        )
        if (
            _query_windows_link_count(handle) != 1
            or _query_windows_final_path(handle) != destination
        ):
            raise _UnsafePath
        return entry
    except Exception:
        _force_close_windows_handle(handle)
        raise


def write_attestation(
    output_path: str | Path,
    *,
    compiled_installer: CompiledInstaller | None = None,
    **legacy_arguments,
) -> Path:
    """Publish canonical attestation bytes from one retained compiled capability."""

    if legacy_arguments:
        if _is_active_compiled_installer(compiled_installer):
            _consume_compiled_installer(compiled_installer)
        raise RuntimeError("INSTALLER_ATTESTATION_INPUT_INVALID") from None
    if not _is_active_compiled_installer(compiled_installer):
        raise RuntimeError("INSTALLER_ATTESTATION_INPUT_INVALID") from None
    try:
        destination = _exact_lexical_path(output_path)
        _validate_windows_component_name(destination.name)
    except Exception:
        _consume_compiled_installer(compiled_installer)
        raise RuntimeError("INSTALLER_ATTESTATION_INPUT_INVALID") from None

    token: str | None = None
    document: dict | None = None
    try:
        document = _validate_windows_stage_read_lease(compiled_installer.stage_lease)
    except Exception:
        token = "INSTALLER_ATTESTATION_INPUT_INVALID"
    if token is None:
        try:
            _validate_windows_output_namespace(compiled_installer.output_lease)
        except Exception:
            token = "INSTALLER_SETUP_UNSAFE"
    if token is None:
        try:
            _validate_windows_output_bytes(compiled_installer.output_lease)
        except Exception:
            token = "INSTALLER_SETUP_CHANGED"
    if token is None:
        try:
            _validate_compiled_installer_binding(compiled_installer)
        except Exception:
            token = "INSTALLER_ATTESTATION_INPUT_INVALID"
    if token is not None:
        _consume_compiled_installer(compiled_installer)
        raise RuntimeError(token) from None

    assert document is not None
    verified_setup = compiled_installer.output_lease.loose_path
    staged_release = compiled_installer.staged_release
    encoded = _canonical_json_bytes(
        {
            "schema": "bcs.freecad_installer_attestation/1.1",
            "source_commit": document["source_commit"],
            "stage_identity_sha256": staged_release.stage_identity_sha256,
            "source_zip": {
                "name": staged_release.source_zip_name,
                "size": staged_release.source_zip_size,
                "sha256": staged_release.source_zip_sha256,
            },
            "installer": {
                "name": compiled_installer.setup_basename,
                "size": compiled_installer.setup_size,
                "sha256": compiled_installer.setup_sha256,
            },
            "payload_manifest": {
                "schema": document["schema"],
                "member": MANIFEST_MEMBER,
                "sha256": staged_release.installed_manifest_sha256,
            },
            "toolchain": _compiled_toolchain_identity(compiled_installer),
        }
    )
    try:
        parent, parent_chain = _prepare_windows_attestation_parent(destination.parent)
    except Exception:
        _consume_compiled_installer(compiled_installer)
        raise RuntimeError("INSTALLER_ATTESTATION_IO_ERROR") from None

    temporary: _WindowsHeldEntry | None = None
    temporary_work: _WindowsHeldEntry | None = None
    try:
        temporary, temporary_work = _create_windows_attestation_temp(
            parent,
            parent_chain,
            _ATTESTATION_TEMP_PREFIX + uuid.uuid4().hex,
        )
        _write_windows_file_handle(temporary_work.handle, encoded)
        _flush_windows_file_handle(temporary_work.handle)
        if (
            _windows_handle_metadata(temporary_work.handle) != temporary.identity
            or _query_windows_link_count(temporary_work.handle) != 1
            or _read_windows_file_handle(temporary_work.handle) != encoded
        ):
            raise _ChangedPath
        if (
            _windows_handle_metadata(temporary_work.handle) != temporary.identity
            or _query_windows_link_count(temporary_work.handle) != 1
            or _read_windows_file_handle(temporary_work.handle) != encoded
        ):
            raise _ChangedPath
    except Exception:
        auxiliary_close_failed = False
        if temporary_work is not None:
            try:
                _close_windows_entry(temporary_work)
            except Exception:
                auxiliary_close_failed = True
        if auxiliary_close_failed:
            # Terminal auxiliary-close failure preserves the current temp name;
            # it authorizes no later disposition, rename, or quarantine.
            _close_windows_entries_nonraising(
                [temporary] if temporary is not None else []
            )
        else:
            _cleanup_windows_attestation_temp(temporary)
        _close_windows_entries_nonraising(reversed(parent_chain))
        _consume_compiled_installer(compiled_installer)
        raise RuntimeError("INSTALLER_ATTESTATION_IO_ERROR") from None
    try:
        _close_windows_entry(temporary_work)
    except Exception:
        _close_windows_entries_nonraising(
            [temporary] if temporary is not None else []
        )
        _close_windows_entries_nonraising(reversed(parent_chain))
        _consume_compiled_installer(compiled_installer)
        raise RuntimeError("INSTALLER_ATTESTATION_IO_ERROR") from None

    try:
        _validate_windows_stage_read_lease(compiled_installer.stage_lease)
        _handoff_windows_stage_monitor(compiled_installer.stage_lease)
        _validate_windows_stage_read_lease(compiled_installer.stage_lease)
    except Exception:
        cleaned = _cleanup_windows_attestation_temp(temporary)
        _close_windows_entries_nonraising(reversed(parent_chain))
        _consume_compiled_installer(compiled_installer)
        token = (
            "INSTALLER_ATTESTATION_INPUT_INVALID"
            if cleaned
            else "INSTALLER_ATTESTATION_IO_ERROR"
        )
        raise RuntimeError(token) from None

    try:
        _rename_windows_handle(
            temporary.handle,
            parent_chain[-1].handle,
            destination,
        )
    except FileExistsError:
        winner: _WindowsHeldEntry | None = None
        exact = False
        try:
            winner = _open_windows_attestation_winner(destination, parent_chain)
            exact = _read_windows_file_handle(winner.handle) == encoded
            if exact:
                exact = (
                    _windows_handle_metadata(winner.handle) == winner.identity
                    and _query_windows_link_count(winner.handle) == 1
                )
        except Exception:
            exact = False
        cleaned = _cleanup_windows_attestation_temp(temporary)
        if exact and cleaned and winner is not None:
            try:
                if (
                    _windows_handle_metadata(winner.handle) != winner.identity
                    or _query_windows_link_count(winner.handle) != 1
                    or _read_windows_file_handle(winner.handle) != encoded
                    or _query_windows_final_path(winner.handle) != destination
                ):
                    raise _ChangedPath
            except Exception:
                _consume_compiled_installer(compiled_installer)
                _close_windows_entries_nonraising([winner])
                _close_windows_entries_nonraising(reversed(parent_chain))
                raise RuntimeError("INSTALLER_ATTESTATION_PUBLISH_ERROR") from None
            try:
                _validate_windows_stage_read_lease(compiled_installer.stage_lease)
                _retire_windows_stage_monitor(compiled_installer.stage_lease)
            except Exception:
                _consume_compiled_installer(compiled_installer)
                _close_windows_entries_nonraising([winner])
                _close_windows_entries_nonraising(reversed(parent_chain))
                raise RuntimeError("INSTALLER_ATTESTATION_INPUT_INVALID") from None
            _consume_compiled_installer(compiled_installer)
            _close_windows_entries_nonraising([winner])
            _close_windows_entries_nonraising(reversed(parent_chain))
            return verified_setup
        _consume_compiled_installer(compiled_installer)
        _close_windows_entries_nonraising([winner] if winner is not None else [])
        _close_windows_entries_nonraising(reversed(parent_chain))
        if exact and not cleaned:
            raise RuntimeError("INSTALLER_ATTESTATION_IO_ERROR") from None
        raise RuntimeError("INSTALLER_ATTESTATION_PUBLISH_ERROR") from None
    except Exception:
        _cleanup_windows_attestation_temp(temporary)
        _close_windows_entries_nonraising(reversed(parent_chain))
        _consume_compiled_installer(compiled_installer)
        raise RuntimeError("INSTALLER_ATTESTATION_PUBLISH_ERROR") from None

    # Publication precedes the physically required terminal notification
    # decision. If that decision observes a stage event or cannot prove native
    # completion, delete only this newly owned attestation through its retained
    # handle; a pre-existing winner is never dispositioned here.
    try:
        _retire_windows_stage_monitor(compiled_installer.stage_lease)
    except Exception:
        cleaned = _cleanup_windows_attestation_temp(temporary)
        _close_windows_entries_nonraising(reversed(parent_chain))
        _consume_compiled_installer(compiled_installer)
        token = (
            "INSTALLER_ATTESTATION_INPUT_INVALID"
            if cleaned
            else "INSTALLER_ATTESTATION_IO_ERROR"
        )
        raise RuntimeError(token) from None

    _close_windows_entries_nonraising([temporary])
    _close_windows_entries_nonraising(reversed(parent_chain))
    _consume_compiled_installer(compiled_installer)
    return verified_setup


def _parse_package_version_bytes(package_xml_bytes: bytes) -> str:
    if type(package_xml_bytes) is not bytes:
        raise _ChangedPath
    try:
        text = package_xml_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise _ChangedPath from None
    matches = re.findall(r"<version>(.*?)</version>", text)
    if len(matches) != 1:
        raise _ChangedPath
    version = matches[0].strip()
    if not version:
        raise _ChangedPath
    return version


def _read_package_version(package_xml: Path) -> str:
    if not package_xml.exists():
        raise FileNotFoundError(f"Missing package metadata: {package_xml}")

    return _parse_package_version_bytes(package_xml.read_bytes())


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

    try:
        validated_source = (
            None if source_zip is None else _exact_lexical_path(source_zip)
        )
        output_root = _exact_lexical_path(
            dist_dir if dist_dir is not None else DIST_DIR
        )
    except Exception:
        raise RuntimeError("INSTALLER_SOURCE_UNSAFE") from None
    try:
        validated_stage_parent = _exact_lexical_path(
            stage_dir if stage_dir is not None else STAGE_DIR
        )
    except Exception:
        raise RuntimeError("INSTALLER_STAGE_UNSAFE") from None

    version = read_version()
    expected_name = f"FreeCAD-PDF-Importer_v{version}.zip"
    if validated_source is None:
        try:
            zip_path = _exact_lexical_path(build_release.build(output_root))
        except Exception:
            raise RuntimeError("INSTALLER_SOURCE_IO_ERROR") from None
    else:
        zip_path = validated_source
    if zip_path.name != expected_name:
        raise RuntimeError("INSTALLER_SOURCE_UNSAFE")

    try:
        stage_parent, parent_chain = _prepare_windows_stage_parent(
            validated_stage_parent
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
    except OSError:
        _raise_with_windows_stage_cleanup(
            "INSTALLER_STAGE_IO_ERROR",
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

    try:
        _close_windows_stage_descendants(workspace)
    except _StageCloseError:
        _close_windows_stage_authority(workspace)
        raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None
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
        winner_lease: _WindowsStageReadLease | None = None
        try:
            winner_lease = _acquire_windows_stage_read_lease(final_stage)
        except Exception:
            _raise_with_windows_stage_cleanup(
                "INSTALLER_STAGE_CONFLICT",
                cleanup_token="INSTALLER_STAGE_IO_ERROR",
                workspace=workspace,
            )
        if not _cleanup_windows_stage(workspace):
            _release_windows_stage_read_lease_nonraising(winner_lease)
            raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None
        try:
            _validate_windows_stage_read_lease(winner_lease)
            _release_windows_stage_read_lease(winner_lease)
        except Exception:
            _release_windows_stage_read_lease_nonraising(winner_lease)
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
        winner_lease: _WindowsStageReadLease | None = None
        winner_is_exact = False
        try:
            winner_lease = _acquire_windows_stage_read_lease(final_stage)
            winner_is_exact = True
        except Exception:
            winner_is_exact = False
        if winner_is_exact:
            if not _cleanup_windows_stage(workspace):
                _release_windows_stage_read_lease_nonraising(winner_lease)
                raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None
            try:
                _validate_windows_stage_read_lease(winner_lease)
                _release_windows_stage_read_lease(winner_lease)
            except Exception:
                _release_windows_stage_read_lease_nonraising(winner_lease)
                raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None
            return final_stage
        _release_windows_stage_read_lease_nonraising(winner_lease)
        _cleanup_windows_stage(workspace)
        raise RuntimeError("INSTALLER_STAGE_CONFLICT") from None
    except Exception:
        _cleanup_windows_stage(workspace)
        raise RuntimeError("INSTALLER_STAGE_PUBLISH_ERROR") from None

    workspace.root.name = stage_identity_sha256
    workspace.root.path = final_root
    try:
        adopted = _acquire_windows_stage_read_lease(
            final_stage,
            adopted_root=workspace.root,
            adopted_parent_chain=parent_chain,
        )
    except _StageCloseError:
        _close_windows_stage_authority(workspace)
        raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None
    except _UnsafePath:
        _raise_with_windows_stage_cleanup(
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
    try:
        _release_windows_stage_read_lease(adopted)
    except Exception:
        _close_windows_stage_authority(workspace)
        raise RuntimeError("INSTALLER_STAGE_IO_ERROR") from None
    _close_windows_stage_authority(workspace)
    return final_stage


def compile_installer(
    iscc: Path,
    staged_release: InstallerStage,
    *,
    toolchain_identity: dict,
    output_dir: str | Path | None = None,
) -> CompiledInstaller:
    """Compile under retained input/output authority and return one sealed capability."""

    try:
        compiler_path = _exact_lexical_path(iscc)
        output_path = _exact_lexical_path(
            output_dir if output_dir is not None else DIST_DIR
        )
        canonical_toolchain = _canonical_toolchain_identity(toolchain_identity)
        toolchain_bytes = _canonical_json_bytes(canonical_toolchain)
    except Exception:
        raise RuntimeError("INSTALLER_COMPILER_INPUT_INVALID") from None
    try:
        stage_lease = _acquire_windows_stage_read_lease(staged_release)
    except Exception:
        raise RuntimeError("INSTALLER_COMPILER_INPUT_INVALID") from None
    try:
        output_root, output_chain = _prepare_windows_output_parent(output_path)
    except _ChangedPath:
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_INPUT_INVALID") from None
    except Exception:
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None

    base_name = f"FreeCAD-PDF-Importer-Setup_v{staged_release.version}"
    setup_basename = base_name + ".exe"
    temporary_name = _OUTPUT_TEMP_PREFIX + uuid.uuid4().hex
    try:
        root = _create_windows_output_root(output_root, output_chain, temporary_name)
    except Exception:
        _close_windows_entries_nonraising(reversed(output_chain))
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None
    guard: _WindowsHeldEntry | None = None
    reader: _WindowsHeldEntry | None = None
    try:
        guard = _create_windows_output_guard(root, setup_basename)
    except Exception:
        _close_windows_entries_nonraising([root])
        _close_windows_entries_nonraising(reversed(output_chain))
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None

    cmd = [
        str(compiler_path),
        str(INNO_SCRIPT),
        f"/DMyAppVersion={staged_release.version}",
        f"/DSourceDir={staged_release.source_dir}",
        f"/O{root.path}",
        f"/F{base_name}",
    ]
    print("Running pinned Inno Setup compiler")
    try:
        subprocess.run(cmd, check=True, cwd=REPO_ROOT, close_fds=True)
    except (subprocess.CalledProcessError, OSError):
        _cleanup_windows_output_candidate(
            root, guard, None, require_empty=True
        )
        _close_windows_entries_nonraising(reversed(output_chain))
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None

    try:
        reader = _open_windows_output_reader(root, guard)
        if (
            _windows_handle_metadata(root.handle) != root.identity
            or _windows_handle_metadata(guard.handle) != guard.identity
            or _query_windows_link_count(guard.handle) != 1
            or _query_windows_link_count(reader.handle) != 1
            or set(_query_windows_directory_names(root.handle)) != {setup_basename}
        ):
            raise _ChangedPath
        setup_bytes = _read_windows_file_handle(reader.handle)
        if not setup_bytes:
            raise _ChangedPath
        if (
            _read_windows_file_handle(reader.handle) != setup_bytes
            or _windows_handle_metadata(reader.handle) != reader.identity
            or _windows_handle_metadata(guard.handle) != guard.identity
            or reader.identity != guard.identity
            or set(_query_windows_directory_names(root.handle)) != {setup_basename}
        ):
            raise _ChangedPath
    except Exception:
        _cleanup_windows_output_candidate(
            root, guard, reader, require_empty=False
        )
        _close_windows_entries_nonraising(reversed(output_chain))
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None

    try:
        _validate_windows_stage_read_lease(stage_lease)
    except Exception:
        cleaned = _cleanup_windows_output_candidate(
            root, guard, reader, require_empty=True
        )
        _close_windows_entries_nonraising(reversed(output_chain))
        _release_windows_stage_read_lease_nonraising(stage_lease)
        token = (
            "INSTALLER_COMPILER_INPUT_INVALID"
            if cleaned
            else "INSTALLER_COMPILER_FAILED"
        )
        raise RuntimeError(token) from None

    try:
        identity_bytes, identity_sha256, setup_sha256 = _output_identity_for_bytes(
            staged_release,
            setup_basename,
            setup_bytes,
            canonical_toolchain,
        )
    except Exception:
        _cleanup_windows_output_candidate(
            root, guard, reader, require_empty=False
        )
        _close_windows_entries_nonraising(reversed(output_chain))
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None

    loose_path = output_root / setup_basename
    provenance = "owned"
    leaf_entries: list[_WindowsHeldEntry]
    try:
        _rename_windows_handle(guard.handle, output_chain[-1].handle, loose_path)
    except FileExistsError:
        winner: _WindowsHeldEntry | None = None
        try:
            winner = _open_windows_output_winner(
                output_root, output_chain, setup_basename
            )
            winner_bytes = _read_windows_file_handle(winner.handle)
            winner_identity_bytes, winner_identity_sha256, winner_sha256 = (
                _output_identity_for_bytes(
                    staged_release,
                    setup_basename,
                    winner_bytes,
                    canonical_toolchain,
                )
            )
            if (
                winner_bytes != setup_bytes
                or winner_sha256 != setup_sha256
                or winner_identity_bytes != identity_bytes
                or winner_identity_sha256 != identity_sha256
            ):
                raise _ChangedPath
        except Exception:
            if winner is not None:
                _close_windows_entries_nonraising([winner])
            _cleanup_windows_output_candidate(
                root, guard, reader, require_empty=False
            )
            _close_windows_entries_nonraising(reversed(output_chain))
            _release_windows_stage_read_lease_nonraising(stage_lease)
            raise RuntimeError("INSTALLER_COMPILER_FAILED") from None
        if not _cleanup_windows_output_candidate(
            root, guard, reader, require_empty=True
        ):
            _close_windows_entries_nonraising([winner])
            _close_windows_entries_nonraising(reversed(output_chain))
            _release_windows_stage_read_lease_nonraising(stage_lease)
            raise RuntimeError("INSTALLER_COMPILER_FAILED") from None
        provenance = "winner"
        leaf_entries = [winner]
    except Exception:
        _cleanup_windows_output_candidate(
            root, guard, reader, require_empty=False
        )
        _close_windows_entries_nonraising(reversed(output_chain))
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None
    else:
        guard.path = loose_path
        reader.path = loose_path
        _release_windows_output_root_after_publication(root)
        leaf_entries = [reader, guard]

    capability_token = object()
    stage_lease.capability_token = capability_token
    output_lease = _WindowsOutputLease(
        parent_chain=output_chain,
        leaf_entries=leaf_entries,
        loose_path=loose_path,
        setup_bytes=setup_bytes,
        setup_size=len(setup_bytes),
        setup_sha256=setup_sha256,
        setup_basename=setup_basename,
        output_identity_bytes=identity_bytes,
        output_identity_sha256=identity_sha256,
        provenance=provenance,
        capability_token=capability_token,
    )
    try:
        _validate_windows_output_namespace(output_lease)
        _validate_windows_output_bytes(output_lease)
    except Exception:
        _release_windows_output_lease_nonraising(output_lease)
        _release_windows_stage_read_lease_nonraising(stage_lease)
        raise RuntimeError("INSTALLER_COMPILER_FAILED") from None
    binding_sha256 = _compiled_binding_sha256(
        staged_release.stage_identity_sha256,
        identity_sha256,
        toolchain_bytes,
    )
    return CompiledInstaller(
        staged_release=staged_release,
        stage_lease=stage_lease,
        output_lease=output_lease,
        setup_bytes=setup_bytes,
        setup_size=len(setup_bytes),
        setup_sha256=setup_sha256,
        setup_basename=setup_basename,
        output_identity_bytes=identity_bytes,
        output_identity_sha256=identity_sha256,
        toolchain_name=canonical_toolchain["name"],
        toolchain_version=canonical_toolchain["version"],
        toolchain_source_sha256=canonical_toolchain["source_sha256"],
        toolchain_manifest_sha256=canonical_toolchain["manifest_sha256"],
        toolchain_tree_sha256=canonical_toolchain["tree_sha256"],
        toolchain_identity_bytes=toolchain_bytes,
        binding_sha256=binding_sha256,
        capability_token=capability_token,
        _state=_CapabilityState(),
        _seal=_CAPABILITY_SEAL,
    )


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
    compiled_installer = compile_installer(
        iscc,
        staged_release,
        toolchain_identity=toolchain_identity,
        output_dir=args.output_dir,
    )
    if args.attestation:
        installer_exe = write_attestation(
            args.attestation,
            compiled_installer=compiled_installer,
        )
    else:
        installer_exe = finalize_compiled_installer(compiled_installer)

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
