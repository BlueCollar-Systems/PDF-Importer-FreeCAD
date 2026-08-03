"""Select the bundled dependency tree that matches FreeCAD's Python ABI."""
from __future__ import annotations

import json
import os
import platform
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import MutableSequence, Optional, Tuple


RUNTIME_MANIFEST_SCHEMA = "bcs.freecad.runtime-matrix/1.0"
SUPPORTED_RUNTIME_TAGS = {(3, 10): "cp310", (3, 11): "cp311"}
EXPECTED_RUNTIME_WHEELS = {
    "common": "pymupdf-1.28.0-cp310-abi3-win_amd64.whl",
    "cp310": "fonttools-4.63.0-cp310-cp310-win_amd64.whl",
    "cp311": "fonttools-4.63.0-cp311-cp311-win_amd64.whl",
}


class BundledRuntimeUnavailable(RuntimeError):
    """The release does not contain a dependency tree for this host runtime."""


class BundledRuntimeIntegrityError(RuntimeError):
    """A runtime bundle is present but cannot be trusted or activated safely."""


@dataclass(frozen=True)
class BundledRuntime:
    runtime_tag: str
    dependency_paths: Tuple[Path, Path]
    bin_dir: Path
    manifest_path: Path


def _normalized_machine(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BundledRuntimeIntegrityError(
            "bundled runtime payload could not be inspected"
        ) from exc
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _require_runtime_entry(path: Path, *, directory: bool) -> Path:
    if _is_link_or_reparse(path):
        raise BundledRuntimeIntegrityError(
            "bundled runtime contains linked or reparse-point content"
        )
    if directory:
        valid = path.is_dir()
    else:
        valid = path.is_file()
    if not valid:
        raise BundledRuntimeIntegrityError("bundled runtime payload is missing")
    return path


def _require_loaded_module_provenance(runtime: "BundledRuntime") -> None:
    expected = (
        ("pymupdf", runtime.dependency_paths[1], "1.28.0"),
        ("fontTools", runtime.dependency_paths[0], "4.63.0"),
    )
    for module_name, expected_root, expected_version in expected:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        origin_value = getattr(module, "__file__", None)
        try:
            origin = Path(str(origin_value)).resolve(strict=True)
            origin.relative_to(expected_root.resolve(strict=True))
        except (OSError, TypeError, ValueError) as exc:
            raise BundledRuntimeIntegrityError(
                f"{module_name} is already loaded from outside the selected bundled "
                "runtime; restart FreeCAD before using PDF Vector Importer"
            ) from exc
        if module_name == "pymupdf":
            version = getattr(module, "__version__", "") or getattr(
                module, "VersionBind", ""
            )
        else:
            version = getattr(module, "__version__", "") or getattr(
                module, "version", ""
            )
        if str(version) != expected_version:
            raise BundledRuntimeIntegrityError(
                f"{module_name} {version or 'unknown'} is already loaded instead of "
                f"the selected bundled version {expected_version}; restart FreeCAD "
                "before using PDF Vector Importer"
            )


def resolve_bundled_runtime(
    addon_root: Path | str,
    *,
    python_version: Optional[tuple[int, int]] = None,
    platform_name: Optional[str] = None,
    machine: Optional[str] = None,
) -> BundledRuntime:
    """Return exact bundled dependency roots for one supported Windows host."""
    version = python_version or (sys.version_info.major, sys.version_info.minor)
    host_platform = str(platform_name or sys.platform).strip().lower()
    host_machine = _normalized_machine(machine or platform.machine())
    runtime_tag = SUPPORTED_RUNTIME_TAGS.get(tuple(version))
    if (
        runtime_tag is None
        or host_platform != "win32"
        or host_machine not in {"amd64", "x86_64"}
    ):
        raise BundledRuntimeUnavailable(
            "bundled Windows runtime requires CPython 3.10 or 3.11 on AMD64"
        )

    root = Path(addon_root)
    lib_root = root / "src" / "lib"
    manifest_path = lib_root / "runtime-manifest.json"
    if not os.path.lexists(lib_root):
        raise BundledRuntimeUnavailable("bundled runtime payload is not installed")
    _require_runtime_entry(lib_root, directory=True)
    _require_runtime_entry(manifest_path, directory=False)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        common = manifest["common"]
        runtime = manifest["runtimes"][runtime_tag]
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise BundledRuntimeIntegrityError(
            "bundled runtime manifest is incomplete"
        ) from exc

    if (
        manifest.get("schema") != RUNTIME_MANIFEST_SCHEMA
        or manifest.get("platform") != "win_amd64"
        or common.get("wheel") != EXPECTED_RUNTIME_WHEELS["common"]
        or common.get("wheel_tag") != "cp310-abi3-win_amd64"
        or common.get("path") != "common"
        or runtime.get("wheel") != EXPECTED_RUNTIME_WHEELS[runtime_tag]
        or runtime.get("wheel_tag")
        != f"{runtime_tag}-{runtime_tag}-win_amd64"
        or runtime.get("path") != runtime_tag
    ):
        raise BundledRuntimeIntegrityError("bundled runtime manifest is incompatible")

    common_dir = _require_runtime_entry(lib_root / "common", directory=True)
    abi_dir = _require_runtime_entry(lib_root / runtime_tag, directory=True)
    try:
        resolved_lib_root = lib_root.resolve(strict=True)
        common_dir.resolve(strict=True).relative_to(resolved_lib_root)
        abi_dir.resolve(strict=True).relative_to(resolved_lib_root)
    except (OSError, ValueError) as exc:
        raise BundledRuntimeIntegrityError(
            "bundled runtime payload escapes its library root"
        ) from exc

    return BundledRuntime(
        runtime_tag=runtime_tag,
        dependency_paths=(abi_dir, common_dir),
        bin_dir=lib_root / "bin",
        manifest_path=manifest_path,
    )


def activate_runtime_paths(
    runtime: BundledRuntime,
    *,
    search_path: Optional[MutableSequence[str]] = None,
) -> None:
    """Prepend only the selected ABI and shared dependency roots."""
    target = search_path if search_path is not None else sys.path
    lib_root = runtime.manifest_path.parent

    def windows_path_identity(path: Path | str) -> str:
        # Bundled runtimes are Windows-only. Keep Windows' case-insensitive
        # path identity even when this contract is exercised by Linux CI.
        return os.path.abspath(str(path)).replace("\\", "/").casefold()

    vendor_candidates = {
        windows_path_identity(path)
        for path in (
            lib_root,
            lib_root / "common",
            lib_root / "cp310",
            lib_root / "cp311",
        )
    }
    target[:] = [
        entry
        for entry in target
        if windows_path_identity(entry) not in vendor_candidates
    ]
    for dependency_path in reversed(runtime.dependency_paths):
        target.insert(0, str(dependency_path))


def activate_bundled_runtime_if_available(
    addon_root: Path | str,
    *,
    python_version: Optional[tuple[int, int]] = None,
    platform_name: Optional[str] = None,
    machine: Optional[str] = None,
    search_path: Optional[MutableSequence[str]] = None,
) -> Optional[BundledRuntime]:
    """Activate a compatible bundled runtime, or preserve system resolution."""
    try:
        runtime = resolve_bundled_runtime(
            addon_root,
            python_version=python_version,
            platform_name=platform_name,
            machine=machine,
        )
    except BundledRuntimeUnavailable:
        return None

    _require_loaded_module_provenance(runtime)
    activate_runtime_paths(runtime, search_path=search_path)
    return runtime
