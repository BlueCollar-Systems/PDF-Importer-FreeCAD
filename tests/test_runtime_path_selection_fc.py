from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import types

import pytest


def _write_runtime_layout(addon_root: Path) -> None:
    lib_root = addon_root / "src" / "lib"
    for relative in ("common", "cp310", "cp311", "bin"):
        (lib_root / relative).mkdir(parents=True, exist_ok=True)
    (lib_root / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "schema": "bcs.freecad.runtime-matrix/1.0",
                "platform": "win_amd64",
                "common": {
                    "path": "common",
                    "dependencies": {"PyMuPDF": "1.28.0"},
                    "wheel": "pymupdf-1.28.0-cp310-abi3-win_amd64.whl",
                    "wheel_tag": "cp310-abi3-win_amd64",
                },
                "runtimes": {
                    "cp310": {
                        "path": "cp310",
                        "dependencies": {"fonttools": "4.63.0"},
                        "wheel": "fonttools-4.63.0-cp310-cp310-win_amd64.whl",
                        "wheel_tag": "cp310-cp310-win_amd64",
                    },
                    "cp311": {
                        "path": "cp311",
                        "dependencies": {"fonttools": "4.63.0"},
                        "wheel": "fonttools-4.63.0-cp311-cp311-win_amd64.whl",
                        "wheel_tag": "cp311-cp311-win_amd64",
                    },
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("python_version", "expected_tag"),
    [((3, 10), "cp310"), ((3, 11), "cp311")],
)
def test_resolver_selects_only_the_matching_windows_runtime(
    tmp_path: Path, python_version: tuple[int, int], expected_tag: str
) -> None:
    """A wrong-ABI sibling must never be eligible for dependency imports."""
    from PDFVectorImporter.runtime_paths import resolve_bundled_runtime

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)

    selected = resolve_bundled_runtime(
        addon_root,
        python_version=python_version,
        platform_name="win32",
        machine="AMD64",
    )

    assert selected.runtime_tag == expected_tag
    assert selected.dependency_paths == (
        addon_root / "src" / "lib" / expected_tag,
        addon_root / "src" / "lib" / "common",
    )
    assert selected.bin_dir == addon_root / "src" / "lib" / "bin"


@pytest.mark.parametrize(
    ("python_version", "platform_name", "machine"),
    [((3, 12), "win32", "AMD64"), ((3, 11), "linux", "x86_64")],
)
def test_resolver_rejects_an_unbundled_runtime_instead_of_using_a_sibling(
    tmp_path: Path,
    python_version: tuple[int, int],
    platform_name: str,
    machine: str,
) -> None:
    """Unsupported hosts must use system/on-demand deps, never another ABI tree."""
    from PDFVectorImporter.runtime_paths import (
        BundledRuntimeUnavailable,
        resolve_bundled_runtime,
    )

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)

    with pytest.raises(BundledRuntimeUnavailable):
        resolve_bundled_runtime(
            addon_root,
            python_version=python_version,
            platform_name=platform_name,
            machine=machine,
        )


def test_path_activation_is_idempotent_and_removes_flat_and_sibling_vendor_paths(
    tmp_path: Path,
) -> None:
    """Reactivation must not leave an older ABI or the legacy flat tree importable."""
    from PDFVectorImporter.runtime_paths import (
        activate_runtime_paths,
        resolve_bundled_runtime,
    )

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)
    selected = resolve_bundled_runtime(
        addon_root,
        python_version=(3, 10),
        platform_name="win32",
        machine="AMD64",
    )
    lib_root = addon_root / "src" / "lib"
    search_path = [
        "system-first",
        str(lib_root),
        str(lib_root / "cp311"),
        str(lib_root / "cp311").upper(),
        str(lib_root / "common"),
    ]

    activate_runtime_paths(selected, search_path=search_path)
    activate_runtime_paths(selected, search_path=search_path)

    assert search_path[:2] == [
        str(lib_root / "cp310"),
        str(lib_root / "common"),
    ]
    assert search_path.count(str(lib_root / "cp310")) == 1
    assert search_path.count(str(lib_root / "common")) == 1
    assert str(lib_root) not in search_path
    assert str(lib_root / "cp311") not in search_path
    assert str(lib_root / "cp311").upper() not in search_path
    assert "system-first" in search_path


def test_optional_activation_leaves_system_paths_untouched_on_an_unsupported_host(
    tmp_path: Path,
) -> None:
    """Source installs on other platforms must remain able to use system packages."""
    from PDFVectorImporter.runtime_paths import (
        activate_bundled_runtime_if_available,
    )

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)
    search_path = ["system-packages"]

    selected = activate_bundled_runtime_if_available(
        addon_root,
        python_version=(3, 12),
        platform_name="win32",
        machine="AMD64",
        search_path=search_path,
    )

    assert selected is None
    assert search_path == ["system-packages"]


def test_resolver_rejects_manifest_path_traversal(tmp_path: Path) -> None:
    """Manifest-controlled paths must never escape the bundled lib root."""
    from PDFVectorImporter.runtime_paths import BundledRuntimeIntegrityError, resolve_bundled_runtime

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)
    outside = tmp_path / "outside"
    outside.mkdir()
    manifest_path = addon_root / "src" / "lib" / "runtime-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["common"]["path"] = "../../../../outside"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BundledRuntimeIntegrityError, match="incompatible"):
        resolve_bundled_runtime(
            addon_root,
            python_version=(3, 10),
            platform_name="win32",
            machine="AMD64",
        )


def test_resolver_rejects_linked_runtime_payload(tmp_path: Path) -> None:
    """A symlinked ABI directory must not redirect imports outside the bundle."""
    from PDFVectorImporter.runtime_paths import BundledRuntimeIntegrityError, resolve_bundled_runtime

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)
    abi_dir = addon_root / "src" / "lib" / "cp310"
    abi_dir.rmdir()
    outside = tmp_path / "outside-cp310"
    outside.mkdir()
    try:
        abi_dir.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    with pytest.raises(BundledRuntimeIntegrityError, match="linked"):
        resolve_bundled_runtime(
            addon_root,
            python_version=(3, 10),
            platform_name="win32",
            machine="AMD64",
        )


@pytest.mark.parametrize("failure", ["missing_manifest", "missing_payload", "corrupt_manifest"])
def test_supported_host_fails_closed_when_bundle_is_incomplete(
    tmp_path: Path, failure: str
) -> None:
    """A present Windows bundle is never allowed to degrade to system packages."""
    from PDFVectorImporter.runtime_paths import (
        BundledRuntimeIntegrityError,
        activate_bundled_runtime_if_available,
    )

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)
    lib_root = addon_root / "src" / "lib"
    if failure == "missing_manifest":
        (lib_root / "runtime-manifest.json").unlink()
    elif failure == "missing_payload":
        (lib_root / "cp310").rmdir()
    else:
        (lib_root / "runtime-manifest.json").write_text("not-json", encoding="utf-8")
    search_path = ["system-packages"]

    with pytest.raises(BundledRuntimeIntegrityError):
        activate_bundled_runtime_if_available(
            addon_root,
            python_version=(3, 10),
            platform_name="win32",
            machine="AMD64",
            search_path=search_path,
        )
    assert search_path == ["system-packages"]


def test_supported_source_install_without_a_bundle_preserves_system_fallback(
    tmp_path: Path,
) -> None:
    """Only a wholly absent lib tree denotes an intentional source install."""
    from PDFVectorImporter.runtime_paths import activate_bundled_runtime_if_available

    addon_root = tmp_path / "PDFVectorImporter"
    addon_root.mkdir()
    search_path = ["system-packages"]

    selected = activate_bundled_runtime_if_available(
        addon_root,
        python_version=(3, 10),
        platform_name="win32",
        machine="AMD64",
        search_path=search_path,
    )

    assert selected is None
    assert search_path == ["system-packages"]


def test_unsupported_host_does_not_inspect_a_corrupt_windows_bundle(
    tmp_path: Path,
) -> None:
    """Unsupported hosts retain system resolution even beside corrupt Windows bytes."""
    from PDFVectorImporter.runtime_paths import activate_bundled_runtime_if_available

    addon_root = tmp_path / "PDFVectorImporter"
    manifest_path = addon_root / "src" / "lib" / "runtime-manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("not-json", encoding="utf-8")
    search_path = ["system-packages"]

    selected = activate_bundled_runtime_if_available(
        addon_root,
        python_version=(3, 12),
        platform_name="win32",
        machine="AMD64",
        search_path=search_path,
    )

    assert selected is None
    assert search_path == ["system-packages"]


@pytest.mark.parametrize(
    ("module_name", "version_attribute", "version"),
    [("pymupdf", "__version__", "1.28.0"), ("fontTools", "__version__", "4.63.0")],
)
def test_activation_rejects_preloaded_dependency_from_wrong_origin(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module_name: str,
    version_attribute: str,
    version: str,
) -> None:
    """A matching version from an unselected path still requires a restart."""
    from PDFVectorImporter.runtime_paths import (
        BundledRuntimeIntegrityError,
        activate_bundled_runtime_if_available,
    )

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)
    wrong_module_path = tmp_path / "system-packages" / module_name / "__init__.py"
    wrong_module_path.parent.mkdir(parents=True)
    wrong_module_path.touch()
    module = types.ModuleType(module_name)
    module.__file__ = os.fspath(wrong_module_path)
    setattr(module, version_attribute, version)
    monkeypatch.setitem(sys.modules, module_name, module)
    search_path = ["system-packages"]

    with pytest.raises(BundledRuntimeIntegrityError, match="restart FreeCAD"):
        activate_bundled_runtime_if_available(
            addon_root,
            python_version=(3, 10),
            platform_name="win32",
            machine="AMD64",
            search_path=search_path,
        )
    assert search_path == ["system-packages"]


@pytest.mark.parametrize(
    ("module_name", "runtime_dir", "loaded_version"),
    [("pymupdf", "common", "1.27.0"), ("fontTools", "cp310", "4.62.0")],
)
def test_activation_rejects_preloaded_dependency_with_wrong_version(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module_name: str,
    runtime_dir: str,
    loaded_version: str,
) -> None:
    """Selected-path provenance is insufficient when the loaded version differs."""
    from PDFVectorImporter.runtime_paths import (
        BundledRuntimeIntegrityError,
        activate_bundled_runtime_if_available,
    )

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)
    # This test isolates one deliberately preloaded dependency.  A full-suite
    # run may already have imported the other dependency from the developer's
    # system environment; leaving that unrelated module in ``sys.modules``
    # would correctly trigger the production fail-closed provenance check and
    # invalidate the premise being exercised here.
    monkeypatch.delitem(sys.modules, "pymupdf", raising=False)
    monkeypatch.delitem(sys.modules, "fontTools", raising=False)
    module_path = addon_root / "src" / "lib" / runtime_dir / module_name / "__init__.py"
    module_path.parent.mkdir(parents=True)
    module_path.touch()
    module = types.ModuleType(module_name)
    module.__file__ = os.fspath(module_path)
    module.__version__ = loaded_version
    monkeypatch.setitem(sys.modules, module_name, module)
    search_path = ["system-packages"]

    with pytest.raises(BundledRuntimeIntegrityError, match="restart FreeCAD"):
        activate_bundled_runtime_if_available(
            addon_root,
            python_version=(3, 10),
            platform_name="win32",
            machine="AMD64",
            search_path=search_path,
        )
    assert search_path == ["system-packages"]


@pytest.mark.parametrize(
    ("module_name", "runtime_dir", "loaded_version"),
    [("pymupdf", "common", "1.28.0"), ("fontTools", "cp310", "4.63.0")],
)
def test_activation_accepts_preloaded_dependency_from_selected_exact_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    module_name: str,
    runtime_dir: str,
    loaded_version: str,
) -> None:
    """An exact already-loaded bundled module remains valid and activation is idempotent."""
    from PDFVectorImporter.runtime_paths import activate_bundled_runtime_if_available

    addon_root = tmp_path / "PDFVectorImporter"
    _write_runtime_layout(addon_root)
    monkeypatch.delitem(sys.modules, "pymupdf", raising=False)
    monkeypatch.delitem(sys.modules, "fontTools", raising=False)
    module_path = addon_root / "src" / "lib" / runtime_dir / module_name / "__init__.py"
    module_path.parent.mkdir(parents=True)
    module_path.touch()
    module = types.ModuleType(module_name)
    module.__file__ = os.fspath(module_path)
    module.__version__ = loaded_version
    monkeypatch.setitem(sys.modules, module_name, module)
    search_path = ["system-packages"]

    selected = activate_bundled_runtime_if_available(
        addon_root,
        python_version=(3, 10),
        platform_name="win32",
        machine="AMD64",
        search_path=search_path,
    )

    assert selected is not None
    assert selected.runtime_tag == "cp310"
    assert search_path[:2] == [
        str(addon_root / "src" / "lib" / "cp310"),
        str(addon_root / "src" / "lib" / "common"),
    ]
