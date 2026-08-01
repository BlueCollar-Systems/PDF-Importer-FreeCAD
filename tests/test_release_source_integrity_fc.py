from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import build_release


def test_release_rejects_untracked_shippable_source(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (addon / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "add", "PDFVectorImporter/package.xml", "PDFVectorImporter/tracked.py"],
        check=True,
    )
    (addon / "untracked.py").write_text("SURPRISE = True\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path(sys.executable)
    )

    with pytest.raises(RuntimeError, match="untracked shippable file"):
        build_release.build(tmp_path / "out")


def test_release_rejects_tracked_source_that_differs_from_index(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    tracked = addon / "tracked.py"
    tracked.write_text("VALUE = 1\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)
    tracked.write_text("VALUE = 2\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path(sys.executable)
    )

    with pytest.raises(RuntimeError, match="differs from the Git index"):
        build_release.build(tmp_path / "out")


def test_release_rejects_dependency_lock_that_differs_from_index(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (repo / "build_release.py").write_text("# builder\n", encoding="utf-8")
    lock = repo / "requirements-release.lock"
    lock.write_text("reviewed hash\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    lock.write_text("uncommitted replacement hash\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release,
        "RELEASE_BUILD_INPUTS",
        (Path("build_release.py"), Path("requirements-release.lock")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="release build input differs"):
        build_release._require_commit_bound_sources()


def test_default_vendoring_replaces_stale_ignored_runtime(
    monkeypatch, tmp_path
):
    vendored = tmp_path / "src" / "lib"
    vendored.mkdir(parents=True)
    stale_marker = vendored / "stale-unreviewed-module.py"
    stale_marker.write_text("STALE = True\n", encoding="utf-8")
    lock = tmp_path / "requirements-release.lock"
    lock.write_text("locked wheel hashes\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", vendored)
    monkeypatch.setattr(build_release, "RUNTIME_DEPENDENCY_LOCK", lock)
    monkeypatch.setattr(
        build_release, "_candidate_freecad_pythons", lambda: [Path(sys.executable)]
    )
    monkeypatch.setattr(build_release, "_python_version", lambda _python: (3, 11))
    monkeypatch.setattr(
        build_release,
        "_lib_has_runtime_dependencies",
        lambda _python, _lib: True,
    )

    def simulate_locked_install(command, **_kwargs):
        assert "--require-hashes" in command
        assert str(lock) in command
        vendored.mkdir(parents=True, exist_ok=True)
        (vendored / "installed-from-lock.txt").write_text(
            "fresh\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_release.subprocess, "run", simulate_locked_install)

    build_release.ensure_runtime_dependencies()

    assert not stale_marker.exists()
    assert (vendored / "installed-from-lock.txt").is_file()


def test_no_vendor_mode_rejects_unattested_ignored_runtime(
    monkeypatch, tmp_path
):
    vendored = tmp_path / "src" / "lib"
    vendored.mkdir(parents=True)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", vendored)
    monkeypatch.setattr(
        build_release, "_candidate_freecad_pythons", lambda: [Path(sys.executable)]
    )
    monkeypatch.setattr(
        build_release,
        "_lib_has_runtime_dependencies",
        lambda _python, _lib: True,
    )

    with pytest.raises(RuntimeError, match="lock-bound"):
        build_release.ensure_runtime_dependencies(vendor=False)


def test_build_packages_isolated_locked_runtime_not_stale_checkout_runtime(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    stale_runtime = addon / "src" / "lib"
    stale_runtime.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (addon / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    (stale_runtime / "stale-unreviewed-module.py").write_text(
        "STALE = True\n", encoding="utf-8"
    )

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "add",
            "PDFVectorImporter/package.xml",
            "PDFVectorImporter/module.py",
        ],
        check=True,
    )

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", stale_runtime)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())

    def simulate_locked_vendoring(*, vendor=True, runtime_dir=None):
        if runtime_dir is not None:
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "locked-runtime.pyd").write_bytes(b"reviewed wheel bytes")
        return Path(sys.executable)

    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", simulate_locked_vendoring
    )

    archive = build_release.build(tmp_path / "out")

    with zipfile.ZipFile(archive) as package:
        names = set(package.namelist())
    assert "PDFVectorImporter/src/lib/locked-runtime.pyd" in names
    assert "PDFVectorImporter/src/lib/stale-unreviewed-module.py" not in names
    assert (stale_runtime / "stale-unreviewed-module.py").is_file()
