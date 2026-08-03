from __future__ import annotations

import base64
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import build_release


def _encoded_synthetic_private_denylist() -> str:
    term = "PRIVATE-SYNTHETIC-" + "DRAWING-ALPHA"
    return base64.b64encode(
        (f'{{"schema":"bcs.private-release-denylist/1.0","terms":["{term}"]}}').encode(
            "utf-8"
        )
    ).decode("ascii")


@pytest.fixture(autouse=True)
def _synthetic_private_release_denylist(monkeypatch):
    monkeypatch.setenv(
        "BCS_PRIVATE_RELEASE_DENYLIST_B64",
        _encoded_synthetic_private_denylist(),
    )


def _commit_fixture(repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "config", "core.autocrlf", "false"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "add", "--renormalize", "."],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Release Test",
            "-c",
            "user.email=release-test@example.invalid",
            "commit",
            "-qm",
            "fixture baseline",
        ],
        check=True,
    )


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
    _commit_fixture(repo)
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


def test_release_rejects_tracked_source_that_differs_from_head(
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
    _commit_fixture(repo)
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

    with pytest.raises(RuntimeError, match="differs from HEAD"):
        build_release.build(tmp_path / "out")


def test_release_requires_a_real_head_commit(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    snapshot, _skipped = build_release._capture_first_party_files()

    with pytest.raises(RuntimeError, match="HEAD commit"):
        build_release._require_commit_bound_sources(snapshot)


def test_release_rejects_staged_source_that_differs_from_head(
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
    _commit_fixture(repo)
    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    snapshot, _skipped = build_release._capture_first_party_files()

    with pytest.raises(RuntimeError, match="differs from HEAD"):
        build_release._require_commit_bound_sources(snapshot)


def test_release_rejects_staged_only_change_when_snapshot_matches_head(
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
    _commit_fixture(repo)
    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "restore",
            "--worktree",
            "--source=HEAD",
            "--",
            "PDFVectorImporter/tracked.py",
        ],
        check=True,
    )

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    snapshot, _skipped = build_release._capture_first_party_files()

    with pytest.raises(RuntimeError, match="staged changes relative to HEAD"):
        build_release._require_commit_bound_sources(snapshot)


def test_release_rejects_staged_new_source_absent_from_head(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)
    _commit_fixture(repo)
    (addon / "staged_new.py").write_text("VALUE = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    snapshot, _skipped = build_release._capture_first_party_files()

    with pytest.raises(RuntimeError, match="not present in HEAD"):
        build_release._require_commit_bound_sources(snapshot)


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
    lock = repo / "requirements-release-common.lock"
    lock.write_text("reviewed hash\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _commit_fixture(repo)
    lock.write_text("uncommitted replacement hash\n", encoding="utf-8")

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release,
        "RELEASE_BUILD_INPUTS",
        (Path("build_release.py"), Path("requirements-release-common.lock")),
        raising=False,
    )

    snapshot, _skipped = build_release._capture_first_party_files()
    with pytest.raises(RuntimeError, match="release build input differs"):
        build_release._require_commit_bound_sources(snapshot)


def test_build_vendors_from_verified_immutable_dependency_lock_snapshot(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (repo / "build_release.py").write_text("# builder\n", encoding="utf-8")
    common_lock = repo / "requirements-release-common.lock"
    cp310_lock = repo / "requirements-release-cp310.lock"
    cp311_lock = repo / "requirements-release-cp311.lock"
    original_lock_bytes = {
        common_lock: b"reviewed common hash\n",
        cp310_lock: b"reviewed cp310 hash\n",
        cp311_lock: b"reviewed cp311 hash\n",
    }
    for lock_path, content in original_lock_bytes.items():
        lock_path.write_bytes(content)

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _commit_fixture(repo)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release,
        "RELEASE_BUILD_INPUTS",
        tuple(
            Path(path.name)
            for path in (
                repo / "build_release.py",
                common_lock,
                cp310_lock,
                cp311_lock,
            )
        ),
    )
    monkeypatch.setattr(
        build_release, "COMMON_RUNTIME_DEPENDENCY_LOCK", common_lock
    )
    monkeypatch.setattr(
        build_release,
        "RUNTIME_DEPENDENCY_LOCKS",
        {"cp310": cp310_lock, "cp311": cp311_lock},
    )

    original_verifier = build_release._require_commit_bound_sources

    def verify_then_replace_worktree_locks(snapshot):
        captured = original_verifier(snapshot)
        for lock_path in original_lock_bytes:
            lock_path.write_bytes(b"late unreviewed replacement hash\n")
        return captured

    monkeypatch.setattr(
        build_release,
        "_require_commit_bound_sources",
        verify_then_replace_worktree_locks,
    )
    observed = {}

    def observe_vendoring_inputs(*, runtime_dir, **kwargs):
        observed["common"] = Path(
            kwargs.get("common_lock", common_lock)
        ).read_bytes()
        observed["cp310"] = Path(
            kwargs.get("runtime_locks", {}).get("cp310", cp310_lock)
        ).read_bytes()
        observed["cp311"] = Path(
            kwargs.get("runtime_locks", {}).get("cp311", cp311_lock)
        ).read_bytes()
        runtime_dir.mkdir(parents=True)
        return {}

    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", observe_vendoring_inputs
    )

    build_release.build(tmp_path / "out")

    assert observed == {
        "common": original_lock_bytes[common_lock],
        "cp310": original_lock_bytes[cp310_lock],
        "cp311": original_lock_bytes[cp311_lock],
    }


def test_default_vendoring_replaces_stale_ignored_runtime(
    monkeypatch, tmp_path
):
    vendored = tmp_path / "src" / "lib"
    vendored.mkdir(parents=True)
    stale_marker = vendored / "stale-unreviewed-module.py"
    stale_marker.write_text("STALE = True\n", encoding="utf-8")
    common_lock = tmp_path / "requirements-release-common.lock"
    cp310_lock = tmp_path / "requirements-release-cp310.lock"
    cp311_lock = tmp_path / "requirements-release-cp311.lock"
    for lock in (common_lock, cp310_lock, cp311_lock):
        lock.write_text("locked wheel hashes\n", encoding="utf-8")
    py310 = tmp_path / "python310.exe"
    py311 = tmp_path / "python311.exe"
    py310.touch()
    py311.touch()

    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", vendored)
    monkeypatch.setattr(
        build_release, "COMMON_RUNTIME_DEPENDENCY_LOCK", common_lock
    )
    monkeypatch.setattr(
        build_release, "RUNTIME_DEPENDENCY_LOCKS", {
            "cp310": cp310_lock,
            "cp311": cp311_lock,
        }
    )
    monkeypatch.setattr(
        build_release, "_resolve_runtime_pythons", lambda **_kwargs: {
            "cp310": py310,
            "cp311": py311,
        }
    )
    monkeypatch.setattr(
        build_release, "_runtime_has_runtime_dependencies", lambda *_args: True
    )
    monkeypatch.setattr(
        build_release, "_require_installed_wheel_tag", lambda *_args: None
    )

    def simulate_locked_install(command, **_kwargs):
        assert "--require-hashes" in command
        assert any(str(lock) in command for lock in (common_lock, cp310_lock, cp311_lock))
        target = Path(command[command.index("--target") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "installed-from-lock.txt").write_text(
            "fresh\n", encoding="utf-8"
        )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(build_release.subprocess, "run", simulate_locked_install)

    build_release.ensure_runtime_dependencies(python310=py310, python311=py311)

    assert not stale_marker.exists()
    assert (vendored / "common" / "installed-from-lock.txt").is_file()
    assert (vendored / "cp310" / "installed-from-lock.txt").is_file()
    assert (vendored / "cp311" / "installed-from-lock.txt").is_file()


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
    _commit_fixture(repo)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", stale_runtime)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())

    def simulate_locked_vendoring(*, vendor=True, runtime_dir=None, **_kwargs):
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
