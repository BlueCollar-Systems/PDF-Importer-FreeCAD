from __future__ import annotations

import json
from pathlib import Path

import pytest

import pdfcadcore_sync_check as sync_check


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_real_canonical_inventory_includes_item_impossibility_module() -> None:
    canonical = sync_check.local_core_dir("FC")
    names = {path.name for path in sync_check.iter_core_files(canonical)}
    assert "item_impossibility.py" in names


def _core_dir(root: Path, host: str) -> Path:
    if host == "FC":
        return root / "PDFVectorImporter" / "pdfcadcore"
    if host == "BL":
        return root / "pdf_vector_importer" / "pdfcadcore"
    if host == "LC":
        return root / "pdfcadcore"
    raise AssertionError(host)


def _make_repo(root: Path, host: str, module_text: str = "VALUE = 1\n") -> Path:
    core = _core_dir(root, host)
    _write(core / "sample.py", module_text)
    if host == "LC":
        _write(root / "dxf_builder.py", "# LibreCAD layout marker\n")
    return core


def _set_local_checkout(monkeypatch, root: Path) -> None:
    monkeypatch.setattr(sync_check, "SCRIPT_DIR", root)
    monkeypatch.setattr(
        sync_check,
        "MANIFEST_PATH",
        root / "pdfcadcore_sync_manifest.json",
    )


def _write_manifest(root: Path, core: Path) -> Path:
    context = root / "repo_context_builder_core.py"
    _write(context, "CONTEXT = 1\n")
    manifest = {
        "sample.py": sync_check.sha256_file(core / "sample.py"),
        "repo_context_builder_core.py": sync_check.sha256_file(context),
        sync_check.SELF_NAME: sync_check.sha256_file(
            Path(sync_check.__file__).resolve()
        ),
    }
    path = root / "pdfcadcore_sync_manifest.json"
    _write(path, json.dumps(manifest, indent=2) + "\n")
    return path


def test_default_manifest_write_is_local_only_without_legacy_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    local_core = _make_repo(local_root, "FC")
    _write(local_root / "repo_context_builder_core.py", "CONTEXT = 1\n")

    unlisted_root = tmp_path / "unlisted-blender"
    _make_repo(unlisted_root, "BL", "OLD = 1\n")
    unlisted_manifest = unlisted_root / "pdfcadcore_sync_manifest.json"
    _write(unlisted_manifest, "sentinel\n")
    _set_local_checkout(monkeypatch, local_root)

    assert sync_check.main(["--write-manifest"]) == 0

    manifest = json.loads(sync_check.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["sample.py"] == sync_check.sha256_file(local_core / "sample.py")
    assert unlisted_manifest.read_text(encoding="utf-8") == "sentinel\n"


def test_default_validation_is_local_only_without_legacy_flag(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    local_core = _make_repo(local_root, "FC")
    _write_manifest(local_root, local_core)

    # A stale checkout on the same machine is deliberately not listed.
    _make_repo(tmp_path / "stale-main-freecad", "FC", "VALUE = 'stale'\n")
    _set_local_checkout(monkeypatch, local_root)

    assert sync_check.main([]) == 0


def test_deprecated_skip_flag_does_not_suppress_explicit_peer_validation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    local_core = _make_repo(local_root, "FC")
    _write_manifest(local_root, local_core)
    peer_root = tmp_path / "explicit-blender"
    _make_repo(peer_root, "BL", "VALUE = 'drifted'\n")
    _set_local_checkout(monkeypatch, local_root)

    result = sync_check.main(
        ["--skip-cross-repo", "--peer-root", f"BL={peer_root}"]
    )

    assert result == 1


def test_write_manifest_propagates_only_to_explicit_peers(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    _make_repo(local_root, "FC")
    _write(local_root / "repo_context_builder_core.py", "CONTEXT = 1\n")
    explicit_root = tmp_path / "explicit-blender"
    _make_repo(explicit_root, "BL")
    unlisted_root = tmp_path / "unlisted-librecad"
    _make_repo(unlisted_root, "LC")
    explicit_manifest = explicit_root / "pdfcadcore_sync_manifest.json"
    unlisted_manifest = unlisted_root / "pdfcadcore_sync_manifest.json"
    _write(explicit_manifest, "old explicit\n")
    _write(unlisted_manifest, "sentinel\n")
    _set_local_checkout(monkeypatch, local_root)

    assert sync_check.main(
        ["--write-manifest", "--peer-root", f"BL={explicit_root}"]
    ) == 0

    assert explicit_manifest.read_bytes() == sync_check.MANIFEST_PATH.read_bytes()
    assert unlisted_manifest.read_text(encoding="utf-8") == "sentinel\n"


@pytest.mark.parametrize("host", ["BL", "LC"])
def test_write_manifest_uses_detected_non_freecad_local_core(
    tmp_path: Path,
    monkeypatch,
    host: str,
) -> None:
    local_root = tmp_path / f"active-{host.lower()}-worktree"
    local_core = _make_repo(local_root, host, f"HOST = {host!r}\n")
    _write(local_root / "repo_context_builder_core.py", "CONTEXT = 1\n")
    _set_local_checkout(monkeypatch, local_root)

    assert sync_check.main(["--write-manifest", "--skip-cross-repo"]) == 0

    manifest = json.loads(sync_check.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["sample.py"] == sync_check.sha256_file(local_core / "sample.py")


@pytest.mark.parametrize(
    "peer_args",
    [
        ["BL={missing}"],
        ["BL={librecad}"],
        ["BL={blender}", "BL={other_blender}"],
        ["BL={blender}", "LC={blender}"],
        ["FC={local}"],
    ],
    ids=[
        "nonexistent-root",
        "layout-mismatch",
        "duplicate-host",
        "duplicate-root",
        "local-host-is-not-a-peer",
    ],
)
def test_invalid_peer_roots_are_rejected_before_work(
    tmp_path: Path,
    monkeypatch,
    peer_args: list[str],
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    local_core = _make_repo(local_root, "FC")
    _write_manifest(local_root, local_core)
    blender = tmp_path / "blender"
    other_blender = tmp_path / "other-blender"
    librecad = tmp_path / "librecad"
    _make_repo(blender, "BL")
    _make_repo(other_blender, "BL")
    _make_repo(librecad, "LC")
    _set_local_checkout(monkeypatch, local_root)
    values = {
        "missing": tmp_path / "missing",
        "librecad": librecad,
        "blender": blender,
        "other_blender": other_blender,
        "local": local_root,
    }
    argv: list[str] = []
    for peer_arg in peer_args:
        argv.extend(["--peer-root", peer_arg.format(**values)])

    with pytest.raises(SystemExit) as exc_info:
        sync_check.main(argv)

    assert exc_info.value.code == 2


def test_fix_requires_explicit_peer_and_verified_local_freecad(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-blender-worktree"
    local_core = _make_repo(local_root, "BL")
    _write_manifest(local_root, local_core)
    _set_local_checkout(monkeypatch, local_root)

    with pytest.raises(SystemExit) as non_fc:
        sync_check.main(["--fix", "--peer-root", f"FC={tmp_path}"])
    assert non_fc.value.code == 2

    fc_root = tmp_path / "active-freecad-worktree"
    _make_repo(fc_root, "FC")
    _set_local_checkout(monkeypatch, fc_root)
    with pytest.raises(SystemExit) as no_peer:
        sync_check.main(["--fix"])
    assert no_peer.value.code == 2


def test_fix_cannot_be_combined_with_manifest_rewrite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    _make_repo(local_root, "FC")
    local_manifest = local_root / "pdfcadcore_sync_manifest.json"
    _write(local_manifest, "local sentinel\n")
    peer_root = tmp_path / "explicit-blender"
    _make_repo(peer_root, "BL")
    peer_manifest = peer_root / "pdfcadcore_sync_manifest.json"
    _write(peer_manifest, "peer sentinel\n")
    _set_local_checkout(monkeypatch, local_root)

    with pytest.raises(SystemExit) as exc_info:
        sync_check.main(
            [
                "--fix",
                "--write-manifest",
                "--peer-root",
                f"BL={peer_root}",
            ]
        )

    assert exc_info.value.code == 2
    assert local_manifest.read_text(encoding="utf-8") == "local sentinel\n"
    assert peer_manifest.read_text(encoding="utf-8") == "peer sentinel\n"


def test_fix_copies_from_verified_local_freecad_only_to_explicit_peer(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    local_core = _make_repo(local_root, "FC", "VALUE = 'canonical'\n")
    _write_manifest(local_root, local_core)
    peer_root = tmp_path / "explicit-blender"
    peer_core = _make_repo(peer_root, "BL", "VALUE = 'drifted'\n")
    unlisted_root = tmp_path / "unlisted-librecad"
    unlisted_core = _make_repo(unlisted_root, "LC", "VALUE = 'untouched'\n")
    _set_local_checkout(monkeypatch, local_root)

    assert sync_check.main(
        ["--fix", "--peer-root", f"BL={peer_root}"]
    ) == 1

    assert (peer_core / "sample.py").read_bytes() == (
        local_core / "sample.py"
    ).read_bytes()
    assert (unlisted_core / "sample.py").read_text(encoding="utf-8") == (
        "VALUE = 'untouched'\n"
    )


def test_fix_refuses_to_copy_when_local_freecad_is_not_manifest_canonical(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    local_core = _make_repo(local_root, "FC", "VALUE = 'canonical'\n")
    _write_manifest(local_root, local_core)
    _write(local_core / "sample.py", "VALUE = 'unverified edit'\n")
    peer_root = tmp_path / "explicit-blender"
    peer_core = _make_repo(peer_root, "BL", "VALUE = 'peer original'\n")
    _set_local_checkout(monkeypatch, local_root)

    assert sync_check.main(
        ["--fix", "--peer-root", f"BL={peer_root}"]
    ) == 1

    assert (peer_core / "sample.py").read_text(encoding="utf-8") == (
        "VALUE = 'peer original'\n"
    )
