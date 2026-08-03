from __future__ import annotations

from pathlib import Path
import json
import subprocess

import pytest

import build_release


def test_runtime_python_resolution_returns_one_exact_interpreter_per_shipped_abi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A release must not silently build both payloads with one interpreter."""
    py310 = tmp_path / "python310.exe"
    py311 = tmp_path / "python311.exe"
    py310.touch()
    py311.touch()
    versions = {py310: (3, 10), py311: (3, 11)}
    monkeypatch.setattr(build_release, "_python_version", versions.__getitem__)

    resolved = build_release._resolve_runtime_pythons(
        python310=py310, python311=py311
    )

    assert resolved == {"cp310": py310, "cp311": py311}


@pytest.mark.parametrize(
    ("python310_version", "python311_version"),
    [((3, 11), (3, 11)), ((3, 10), (3, 10)), ((3, 10), (3, 12))],
)
def test_runtime_python_resolution_rejects_missing_or_wrong_abi_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    python310_version: tuple[int, int],
    python311_version: tuple[int, int],
) -> None:
    """Wrong-version interpreter inputs must stop before any wheel install."""
    py310 = tmp_path / "python310.exe"
    py311 = tmp_path / "python311.exe"
    py310.touch()
    py311.touch()
    versions = {py310: python310_version, py311: python311_version}
    monkeypatch.setattr(build_release, "_python_version", versions.__getitem__)

    with pytest.raises(RuntimeError, match=r"CPython 3\.(10|11)"):
        build_release._resolve_runtime_pythons(python310=py310, python311=py311)


def test_runtime_python_resolution_requires_both_interpreters(tmp_path: Path) -> None:
    """A single-ABI release would violate the advertised offline support matrix."""
    py310 = tmp_path / "python310.exe"
    py310.touch()

    with pytest.raises(RuntimeError, match=r"3\.11 interpreter"):
        build_release._resolve_runtime_pythons(python310=py310, python311=None)


def test_vendoring_builds_shared_and_exact_abi_trees_with_a_manifest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Each native wheel must be installed only into the tree its tag permits."""
    py310 = tmp_path / "python310.exe"
    py311 = tmp_path / "python311.exe"
    py310.touch()
    py311.touch()
    runtime_dir = tmp_path / "lib"
    common_lock = tmp_path / "common.lock"
    cp310_lock = tmp_path / "cp310.lock"
    cp311_lock = tmp_path / "cp311.lock"
    for lock in (common_lock, cp310_lock, cp311_lock):
        lock.write_text("reviewed hashes\n", encoding="utf-8")

    monkeypatch.setattr(
        build_release,
        "_resolve_runtime_pythons",
        lambda **_kwargs: {"cp310": py310, "cp311": py311},
    )
    monkeypatch.setattr(build_release, "COMMON_RUNTIME_DEPENDENCY_LOCK", common_lock)
    monkeypatch.setattr(
        build_release,
        "RUNTIME_DEPENDENCY_LOCKS",
        {"cp310": cp310_lock, "cp311": cp311_lock},
    )
    monkeypatch.setattr(
        build_release,
        "_runtime_has_runtime_dependencies",
        lambda _python, _abi, _common: True,
    )
    monkeypatch.setattr(
        build_release,
        "_require_installed_wheel_tag",
        lambda _target, _project, _tag: None,
    )
    commands: list[list[str]] = []

    def simulate_locked_install(command: list[str], **_kwargs: object):
        commands.append(command)
        target = Path(command[command.index("--target") + 1])
        target.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_release.subprocess, "run", simulate_locked_install)

    resolved = build_release.ensure_runtime_dependencies(
        runtime_dir=runtime_dir,
        python310=py310,
        python311=py311,
    )

    installs = {
        Path(command[command.index("--target") + 1]).name: (
            Path(command[0]),
            Path(command[command.index("--requirement") + 1]),
        )
        for command in commands
    }
    assert installs == {
        "common": (py310, common_lock),
        "cp310": (py310, cp310_lock),
        "cp311": (py311, cp311_lock),
    }
    assert resolved == {"cp310": py310, "cp311": py311}

    manifest = json.loads(
        (runtime_dir / "runtime-manifest.json").read_text(encoding="utf-8")
    )
    assert manifest == {
        "schema": "bcs.freecad.runtime-matrix/1.0",
        "platform": "win_amd64",
        "common": {
            "path": "common",
            "wheel": "pymupdf-1.28.0-cp310-abi3-win_amd64.whl",
            "wheel_tag": "cp310-abi3-win_amd64",
        },
        "runtimes": {
            "cp310": {
                "path": "cp310",
                "wheel": "fonttools-4.63.0-cp310-cp310-win_amd64.whl",
                "wheel_tag": "cp310-cp310-win_amd64",
            },
            "cp311": {
                "path": "cp311",
                "wheel": "fonttools-4.63.0-cp311-cp311-win_amd64.whl",
                "wheel_tag": "cp311-cp311-win_amd64",
            },
        },
    }


def test_installed_wheel_tag_gate_rejects_an_incompatible_native_wheel(
    tmp_path: Path,
) -> None:
    dist_info = tmp_path / "fonttools-4.63.0.dist-info"
    dist_info.mkdir()
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nTag: cp311-cp311-win_amd64\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="cp310-cp310-win_amd64"):
        build_release._require_installed_wheel_tag(
            tmp_path,
            "fonttools",
            "cp310-cp310-win_amd64",
        )


def test_release_build_inputs_retire_the_obsolete_single_abi_lock() -> None:
    expected = {
        Path("build_release.py"),
        Path("requirements-release-common.lock"),
        Path("requirements-release-cp310.lock"),
        Path("requirements-release-cp311.lock"),
    }

    assert set(build_release.RELEASE_BUILD_INPUTS) == expected
    assert not (build_release.REPO_ROOT / "requirements-release.lock").exists()
