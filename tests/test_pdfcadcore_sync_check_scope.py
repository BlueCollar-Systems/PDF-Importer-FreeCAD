from __future__ import annotations

import json
from pathlib import Path

import pytest

import pdfcadcore_sync_check as sync_check


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_skip_cross_repo_manifest_write_stays_in_current_worktree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    local_core = local_root / "PDFVectorImporter" / "pdfcadcore"
    _write(local_core / "sample.py", "VALUE = 1\n")
    _write(local_root / "repo_context_builder_core.py", "CONTEXT = 1\n")

    external_bl = tmp_path / "main-blender"
    external_manifest = external_bl / "pdfcadcore_sync_manifest.json"
    _write(external_bl / "pdf_vector_importer" / "pdfcadcore" / "sample.py", "OLD = 1\n")
    _write(external_manifest, "sentinel\n")

    monkeypatch.setattr(sync_check, "SCRIPT_DIR", local_root)
    monkeypatch.setattr(
        sync_check,
        "MANIFEST_PATH",
        local_root / "pdfcadcore_sync_manifest.json",
    )
    monkeypatch.setattr(
        sync_check,
        "REPO_CORE_DIRS",
        {
            "FC": tmp_path / "main-freecad" / "PDFVectorImporter" / "pdfcadcore",
            "BL": external_bl / "pdf_vector_importer" / "pdfcadcore",
            "LC": tmp_path / "main-librecad" / "pdfcadcore",
        },
    )
    monkeypatch.setattr(sync_check, "detect_local_repo", lambda: "FC")

    assert sync_check.main(["--write-manifest", "--skip-cross-repo"]) == 0

    manifest = json.loads(sync_check.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["sample.py"] == sync_check.sha256_file(local_core / "sample.py")
    assert external_manifest.read_text(encoding="utf-8") == "sentinel\n"


def test_skip_cross_repo_validation_uses_only_current_worktree(
    tmp_path: Path,
    monkeypatch,
) -> None:
    local_root = tmp_path / "active-fc-worktree"
    local_core = local_root / "PDFVectorImporter" / "pdfcadcore"
    local_module = local_core / "sample.py"
    local_context = local_root / "repo_context_builder_core.py"
    _write(local_module, "VALUE = 1\n")
    _write(local_context, "CONTEXT = 1\n")

    external_core = tmp_path / "main-freecad" / "PDFVectorImporter" / "pdfcadcore"
    external_context = tmp_path / "main-freecad" / "repo_context_builder_core.py"
    external_checker = tmp_path / "main-freecad" / sync_check.SELF_NAME
    _write(external_core / "sample.py", "VALUE = 'stale'\n")
    _write(external_context, "CONTEXT = 'stale'\n")
    _write(external_checker, "CHECKER = 'stale'\n")

    manifest = {
        "sample.py": sync_check.sha256_file(local_module),
        "repo_context_builder_core.py": sync_check.sha256_file(local_context),
        sync_check.SELF_NAME: sync_check.sha256_file(Path(sync_check.__file__).resolve()),
    }
    manifest_path = local_root / "pdfcadcore_sync_manifest.json"
    _write(manifest_path, json.dumps(manifest, indent=2) + "\n")

    monkeypatch.setattr(sync_check, "SCRIPT_DIR", local_root)
    monkeypatch.setattr(sync_check, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        sync_check,
        "REPO_CORE_DIRS",
        {
            "FC": external_core,
            "BL": tmp_path / "main-blender" / "pdf_vector_importer" / "pdfcadcore",
            "LC": tmp_path / "main-librecad" / "pdfcadcore",
        },
    )
    monkeypatch.setattr(
        sync_check,
        "REPO_CONTEXT_BUILDER_PATHS",
        (external_context,),
    )
    monkeypatch.setattr(sync_check, "SELF_COPY_PATHS", (external_checker,))
    monkeypatch.setattr(sync_check, "detect_local_repo", lambda: "FC")

    assert sync_check.main(["--skip-cross-repo"]) == 0


@pytest.mark.parametrize(
    ("repo", "core_relative"),
    [
        ("BL", Path("pdf_vector_importer") / "pdfcadcore"),
        ("LC", Path("pdfcadcore")),
    ],
)
def test_skip_cross_repo_manifest_uses_non_freecad_worktree_core(
    tmp_path: Path,
    monkeypatch,
    repo: str,
    core_relative: Path,
) -> None:
    local_root = tmp_path / f"active-{repo.lower()}-worktree"
    local_core = local_root / core_relative
    local_module = local_core / "sample.py"
    _write(local_module, f"HOST = {repo!r}\n")
    if repo == "LC":
        _write(local_root / "dxf_builder.py", "# layout marker\n")
    _write(local_root / "repo_context_builder_core.py", "CONTEXT = 1\n")

    stale_fc = tmp_path / "main-freecad" / "PDFVectorImporter" / "pdfcadcore"
    _write(stale_fc / "sample.py", "HOST = 'stale-main'\n")

    monkeypatch.setattr(sync_check, "SCRIPT_DIR", local_root)
    monkeypatch.setattr(
        sync_check,
        "MANIFEST_PATH",
        local_root / "pdfcadcore_sync_manifest.json",
    )
    monkeypatch.setattr(
        sync_check,
        "REPO_CORE_DIRS",
        {
            "FC": stale_fc,
            "BL": tmp_path / "main-blender" / "pdf_vector_importer" / "pdfcadcore",
            "LC": tmp_path / "main-librecad" / "pdfcadcore",
        },
    )
    monkeypatch.setattr(sync_check, "detect_local_repo", lambda: repo)

    assert sync_check.main(["--write-manifest", "--skip-cross-repo"]) == 0

    manifest = json.loads(sync_check.MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["sample.py"] == sync_check.sha256_file(local_module)
