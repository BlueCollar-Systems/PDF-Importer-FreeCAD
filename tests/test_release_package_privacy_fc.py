import base64
import codecs
import json
import os
import subprocess
import zipfile
from pathlib import Path

import build_release
import pytest


_PRIVATE_DENYLIST_ENV = "BCS_PRIVATE_RELEASE_DENYLIST_B64"


def _synthetic_private_term(suffix: str) -> str:
    # Keep the complete synthetic test term out of tracked source so the
    # repository-wide scanner can exercise real behavior without matching its
    # own test fixture declaration.
    return "PRIVATE-SYNTHETIC-" + f"DRAWING-{suffix}"


def _encoded_private_denylist(*terms: str) -> str:
    payload = {
        "schema": "bcs.private-release-denylist/1.0",
        "terms": list(terms),
    }
    return base64.b64encode(json.dumps(payload, sort_keys=True).encode("utf-8")).decode(
        "ascii"
    )


@pytest.fixture(autouse=True)
def _synthetic_private_release_denylist(monkeypatch):
    monkeypatch.setenv(
        _PRIVATE_DENYLIST_ENV,
        _encoded_private_denylist(_synthetic_private_term("ALPHA")),
    )


def test_external_private_denylist_is_required_and_validated(monkeypatch):
    monkeypatch.delenv(_PRIVATE_DENYLIST_ENV)
    with pytest.raises(RuntimeError, match="external private release denylist"):
        build_release._load_external_private_denylist()

    monkeypatch.setenv(_PRIVATE_DENYLIST_ENV, "not-base64")
    with pytest.raises(RuntimeError, match="external private release denylist"):
        build_release._load_external_private_denylist()


def test_external_private_denylist_blocks_bare_identifier_without_echoing_it(
    monkeypatch, tmp_path
):
    private_identifier = _synthetic_private_term("OMEGA")
    monkeypatch.setenv(
        _PRIVATE_DENYLIST_ENV,
        _encoded_private_denylist(private_identifier),
    )
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (addon / "module.py").write_text(
        f"FIXTURE = {private_identifier!r}\n", encoding="utf-8"
    )
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    monkeypatch.setattr(
        build_release,
        "_require_commit_bound_sources",
        lambda _snapshot: None,
    )

    with pytest.raises(RuntimeError) as raised:
        build_release.build(tmp_path / "out")
    assert "external private denylist" in str(raised.value)
    assert private_identifier not in str(raised.value)


def test_external_private_denylist_scans_tracked_root_excluded_text(
    monkeypatch, tmp_path
):
    private_identifier = _synthetic_private_term("ROOT-CONTENT")
    monkeypatch.setenv(
        _PRIVATE_DENYLIST_ENV,
        _encoded_private_denylist(private_identifier),
    )
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    excluded = repo / "review-notes.txt"
    excluded.write_text("Public release notes\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _commit_fixture(repo)
    excluded.write_text(
        f"Internal validation reference: {private_identifier}\n", encoding="utf-8"
    )

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )

    with pytest.raises(RuntimeError) as raised:
        build_release.build(tmp_path / "out")
    message = str(raised.value)
    assert "external private denylist" in message
    assert private_identifier not in message
    assert str(excluded) not in message
    assert excluded.as_posix() not in message


def test_external_private_denylist_scans_every_tracked_path_name(monkeypatch, tmp_path):
    private_identifier = _synthetic_private_term("ROOT-FILENAME")
    monkeypatch.setenv(
        _PRIVATE_DENYLIST_ENV,
        _encoded_private_denylist(private_identifier),
    )
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    private_path = repo / "notes" / f"{private_identifier}.txt"
    private_path.parent.mkdir()
    private_path.write_text("public placeholder\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _commit_fixture(repo)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )

    with pytest.raises(RuntimeError) as raised:
        build_release.build(tmp_path / "out")
    message = str(raised.value)
    assert "external private denylist" in message
    assert private_identifier not in message
    assert str(private_path) not in message
    assert private_path.as_posix() not in message


def test_external_private_denylist_scans_archive_member_name_before_write(
    monkeypatch, tmp_path
):
    private_identifier = _synthetic_private_term("MEMBER-NAME")
    monkeypatch.setenv(
        _PRIVATE_DENYLIST_ENV,
        _encoded_private_denylist(private_identifier),
    )
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    _commit_fixture(repo)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")

    def provide_runtime(*, runtime_dir, **_kwargs):
        runtime_dir.mkdir(parents=True)
        (runtime_dir / f"{private_identifier}.txt").write_text(
            "public runtime bytes\n", encoding="utf-8"
        )
        return Path("python")

    monkeypatch.setattr(build_release, "ensure_runtime_dependencies", provide_runtime)

    with pytest.raises(RuntimeError) as raised:
        build_release.build(tmp_path / "out")
    message = str(raised.value)
    assert "external private denylist" in message
    assert private_identifier not in message
    assert "fc-release-runtime-" not in message
    assert not (tmp_path / "out" / "FreeCAD-PDF-Importer_v9.9.9.zip").exists()


def test_external_private_denylist_strips_edges_and_dedupes_after_normalization(
    monkeypatch,
):
    private_identifier = _synthetic_private_term("WHITESPACE")
    monkeypatch.setenv(
        _PRIVATE_DENYLIST_ENV,
        _encoded_private_denylist(
            f"  {private_identifier}  ",
            private_identifier.lower(),
        ),
    )

    assert build_release._load_external_private_denylist() == (
        private_identifier.casefold(),
    )


def test_external_private_denylist_rejects_noncanonical_unicode_whitespace(
    monkeypatch,
):
    private_identifier = _synthetic_private_term("UNICODE-WHITESPACE")
    monkeypatch.setenv(
        _PRIVATE_DENYLIST_ENV,
        _encoded_private_denylist(private_identifier.replace("-", "\u00a0", 1)),
    )

    with pytest.raises(RuntimeError, match="external private release denylist"):
        build_release._load_external_private_denylist()


def test_external_private_denylist_preserves_control_character_rejection(
    monkeypatch,
):
    private_identifier = _synthetic_private_term("CONTROL")
    monkeypatch.setenv(
        _PRIVATE_DENYLIST_ENV,
        _encoded_private_denylist(f"{private_identifier}\x00suffix"),
    )

    with pytest.raises(RuntimeError, match="external private release denylist"):
        build_release._load_external_private_denylist()


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


def test_release_filter_excludes_addon_test_packages():
    assert build_release._should_exclude(
        Path("tests") / "test_fc_style_fixes.py"
    )
    assert not build_release._should_exclude(
        Path("src") / "PDFImporterCore.py"
    )


def test_release_filter_excludes_environment_bound_dependency_metadata():
    assert build_release._should_exclude(
        Path("src") / "lib" / "bin" / "pymupdf.exe"
    )
    assert build_release._should_exclude(
        Path("src") / "lib" / "cp310" / "bin" / "fonttools.exe"
    )
    assert build_release._should_exclude(
        Path("src") / "lib" / "cp311" / "bin" / "ttx.exe"
    )
    assert build_release._should_exclude(
        Path("src") / "lib" / "pymupdf-1.28.0.dist-info" / "RECORD"
    )


@pytest.mark.parametrize(
    "relative_path",
    [
        "private.pdf",
        "private.dxf",
        "private.dwg",
        "private.skp",
        "private.FCStd",
        "private.blend",
        "private.zip",
        "result_import_report.json",
        "Imported Evidence/result.json",
        "test-corpus/fixture.txt",
    ],
)
def test_release_build_fails_closed_on_private_artifact_candidates(
    monkeypatch, tmp_path, relative_path
):
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    candidate = addon / relative_path
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"private validation artifact")

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    # This test isolates the privacy boundary from the separate tracked-source
    # boundary. ``raising=False`` keeps the test RED before that boundary exists.
    monkeypatch.setattr(
        build_release,
        "_require_commit_bound_sources",
        lambda _snapshot: None,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="private corpus artifact"):
        build_release.build(tmp_path / "out")


@pytest.mark.parametrize(
    "private_content",
    [
        r"local_note = 'C:\Users\Example User\Documents\release-note.txt'",
        "local_note = '/Users/example/Projects/release-note.txt'",
        "local_note = '/home/example/projects/release-note.txt'",
        "validation_root = r'C:\\private\\PDFTest Files'",
        "corpus_root = r'C:\\1pdf-test-corpus'",
        "evidence_root = 'Imported Evidence/FreeCAD'",
    ],
)
def test_release_build_fails_closed_on_private_source_content(
    monkeypatch, tmp_path, private_content
):
    """A project-owned corpus identifier in a shipped source file blocks release."""
    addon = tmp_path / "PDFVectorImporter"
    source = addon / "src" / "PDFEmbeddedFonts.py"
    source.parent.mkdir(parents=True)
    source.write_text(private_content, encoding="utf-8")
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    monkeypatch.setattr(
        build_release, "_require_commit_bound_sources", lambda _snapshot: None
    )

    with pytest.raises(RuntimeError, match="private corpus content"):
        build_release.build(tmp_path / "out")


def test_release_content_gate_allows_generic_public_corpus_documentation(
    monkeypatch, tmp_path
):
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (addon / "README.md").write_text(
        "The public synthetic conformance corpus contains 7 PDFs.\n"
        "The public pdf-test-corpus repository contains synthetic fixtures.\n"
        "Imported Evidence is a generic report-category phrase.\n"
        "Users may select PDF test files from any folder.\n"
        "Public test drawings should use descriptive fixture names.\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    monkeypatch.setattr(
        build_release, "_require_commit_bound_sources", lambda _snapshot: None
    )

    archive = build_release.build(tmp_path / "out")
    assert archive.is_file()


@pytest.mark.parametrize("late_path", ["tracked.py", "late_untracked.py"])
def test_release_validates_late_source_bytes_as_they_are_archived(
    monkeypatch, tmp_path, late_path
):
    """Content introduced after the source gates must not enter the archive."""
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    tracked = addon / "tracked.py"
    tracked.write_text("VALUE = 'public'\n", encoding="utf-8")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)
    _commit_fixture(repo)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())

    def mutate_after_source_gates(**_kwargs):
        (addon / late_path).write_text(
            r"local_note = 'C:\Users\Example User\Documents\release-note.txt'"
            "\n",
            encoding="utf-8",
        )
        return Path("python")

    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", mutate_after_source_gates
    )

    with pytest.raises(RuntimeError, match="private corpus content"):
        build_release.build(tmp_path / "out")


def test_release_archives_captured_bytes_after_late_tracked_mutation(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    tracked = addon / "tracked.py"
    tracked.write_bytes(b"VALUE = 'captured'\n")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)
    _commit_fixture(repo)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())

    def mutate_after_source_binding(**_kwargs):
        tracked.write_bytes(b"VALUE = 'late mutation'\n")
        return Path("python")

    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", mutate_after_source_binding
    )

    archive = build_release.build(tmp_path / "out")

    assert tracked.read_bytes() == b"VALUE = 'late mutation'\n"
    with zipfile.ZipFile(archive) as package:
        assert package.read("PDFVectorImporter/tracked.py") == b"VALUE = 'captured'\n"


def test_release_omits_untracked_file_created_after_source_binding(
    monkeypatch, tmp_path
):
    repo = tmp_path / "repo"
    addon = repo / "PDFVectorImporter"
    addon.mkdir(parents=True)
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (addon / "tracked.py").write_bytes(b"VALUE = 'captured'\n")

    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "PDFVectorImporter"], check=True)
    _commit_fixture(repo)

    monkeypatch.setattr(build_release, "REPO_ROOT", repo)
    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(
        build_release, "VENDORED_LIB_DIR", addon / "src" / "lib"
    )
    monkeypatch.setattr(build_release, "RELEASE_BUILD_INPUTS", ())
    late_untracked = addon / "late_untracked.py"

    def create_after_source_binding(**_kwargs):
        late_untracked.write_text("VALUE = 'late untracked'\n", encoding="utf-8")
        return Path("python")

    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", create_after_source_binding
    )

    archive = build_release.build(tmp_path / "out")

    assert late_untracked.is_file()
    with zipfile.ZipFile(archive) as package:
        assert "PDFVectorImporter/late_untracked.py" not in package.namelist()


@pytest.mark.parametrize(
    "payload",
    [
        r"local_note = 'C:\Users\Example User\release-note.txt'".encode(
            "utf-16-le"
        ),
        r"local_note = 'C:\Users\Example User\release-note.txt'".encode(
            "utf-16-be"
        ),
        codecs.BOM_UTF16_LE
        + r"local_note = 'C:\Users\Example User\release-note.txt'".encode(
            "utf-16-le"
        ),
        codecs.BOM_UTF16_BE
        + r"local_note = 'C:\Users\Example User\release-note.txt'".encode(
            "utf-16-be"
        ),
    ],
    ids=["utf16-le", "utf16-be", "utf16-le-bom", "utf16-be-bom"],
)
def test_release_content_gate_detects_private_utf16_text(
    monkeypatch, tmp_path, payload
):
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (addon / "notes.txt").write_bytes(payload)

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    monkeypatch.setattr(
        build_release, "_require_commit_bound_sources", lambda _snapshot: None
    )

    with pytest.raises(RuntimeError, match="private corpus content"):
        build_release.build(tmp_path / "out")


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_release_content_gate_allows_generic_utf16_documentation(
    monkeypatch, tmp_path, encoding
):
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    (addon / "notes.txt").write_bytes(
        "Users may select PDF test files from any folder.".encode(encoding)
    )

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    monkeypatch.setattr(
        build_release, "_require_commit_bound_sources", lambda _snapshot: None
    )

    assert build_release.build(tmp_path / "out").is_file()


def test_release_content_gate_does_not_treat_control_heavy_binary_as_utf16_text(
    monkeypatch, tmp_path
):
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    payload = (
        b"\x01\x00\x02\x00"
        + r"C:\Users\Example User\release-note.txt".encode("utf-16-le")
        + b"\x03\x00\x04\x00"
    )
    (addon / "asset.bin").write_bytes(payload)

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    monkeypatch.setattr(
        build_release, "_require_commit_bound_sources", lambda _snapshot: None
    )

    assert build_release.build(tmp_path / "out").is_file()


def test_release_rejects_selected_file_symlink_before_archive(
    monkeypatch, tmp_path
):
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    target = tmp_path / "outside.txt"
    target.write_text("outside bytes\n", encoding="utf-8")
    link = addon / "linked.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    monkeypatch.setattr(
        build_release, "_require_commit_bound_sources", lambda _snapshot: None
    )

    with pytest.raises(RuntimeError, match="linked or reparse"):
        build_release.build(tmp_path / "out")


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are Windows-only")
def test_release_rejects_selected_directory_junction_before_archive(
    monkeypatch, tmp_path
):
    addon = tmp_path / "PDFVectorImporter"
    addon.mkdir()
    (addon / "package.xml").write_text(
        "<package><version>9.9.9</version></package>", encoding="utf-8"
    )
    target = tmp_path / "outside-dir"
    target.mkdir()
    (target / "outside.txt").write_text("outside bytes\n", encoding="utf-8")
    junction = addon / "linked-dir"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"directory junctions unavailable: {created.stderr}")

    monkeypatch.setattr(build_release, "ADDON_DIR", addon)
    monkeypatch.setattr(build_release, "VENDORED_LIB_DIR", addon / "src" / "lib")
    monkeypatch.setattr(
        build_release, "ensure_runtime_dependencies", lambda **_kwargs: Path("python")
    )
    monkeypatch.setattr(
        build_release, "_require_commit_bound_sources", lambda _snapshot: None
    )

    with pytest.raises(RuntimeError, match="linked or reparse"):
        build_release.build(tmp_path / "out")
