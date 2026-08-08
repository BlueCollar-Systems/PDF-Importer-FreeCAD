#!/usr/bin/env python3
"""build_release.py — BlueCollar Systems
Produces a clean PDFVectorImporter release zip suitable for FreeCAD Addon Manager
distribution and manual install.

Excluded:
  - __pycache__/ and *.pyc
  - .ruff_cache/
  - .github/
  - .git/
  - test PDFs, QA configs, and internal harness files
  - this script itself

Usage:
  python build_release.py --python310 <python310.exe> --python311 <python311.exe>
  python build_release.py --out /path/to/output_dir --python310 ... --python311 ...

Output:
  FreeCAD-PDF-Importer_v<VERSION>.zip  (next to this script, or --out dir)
"""

import argparse
import base64
import codecs
import importlib.util
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.resolve()
ADDON_DIR = REPO_ROOT / "PDFVectorImporter"


def _load_candidate_manifest_contract():
    """Load the product contract without executing the addon initializer."""
    contract_path = Path(__file__).parent / "PDFVectorImporter" / "candidate_manifest.py"
    spec = importlib.util.spec_from_file_location(
        "_freecad_release_candidate_manifest", contract_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Release blocked: candidate manifest contract is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CANDIDATE_MANIFEST = _load_candidate_manifest_contract()
MANIFEST_MEMBER = _CANDIDATE_MANIFEST.MANIFEST_MEMBER
_COMMIT_OID = re.compile(r"\A[0-9a-f]{40}\Z")

# Files / dirs to always exclude (matched against each path component)
EXCLUDE_DIRS = {
    "__pycache__",
    ".ruff_cache",
    ".github",
    ".git",
    "_archived",
    "qa_runs",
    "adapters",  # CLI test harnesses — not needed at FreeCAD runtime
    "tests",
    "temp",
}

EXCLUDE_FILES = {
    ".gitignore",
    ".gitattributes",
    "build_release.py",
    "qa_config_example.json",
    "qa_config_template.json",
    "fc_smoke_payload.json",
    "fc_check_fitz.py",
    "run_pdf_vector_importer_tests.py",
    "su_manual_verification_checklist.md",
    "qa_config_local_live.json",
}

EXCLUDE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".pdf",       # test PDFs should not ship
    ".bak",
    ".swp",
}

# Private validation inputs and generated CAD/model evidence are never valid
# FreeCAD addon payload.  Reject them before exclusions are applied: silently
# dropping a private file would hide repository contamination from the release
# operator instead of failing closed.
PRIVATE_ARTIFACT_SUFFIXES = {
    ".pdf",
    ".dxf",
    ".dwg",
    ".skp",
    ".fcstd",
    ".fcstd1",
    ".blend",
    ".blend1",
}
PRIVATE_ARCHIVE_SUFFIXES = {
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".tgz",
    ".rbz",
}
PRIVATE_REPORT_SUFFIXES = {".json", ".csv", ".html", ".htm", ".png"}

# Environment-bound paths and private-workspace markers must not appear in any
# first-party file selected for the release archive.  The public gate stays
# intentionally generic: private fixture names belong only in the external QA
# environment and must never be copied into this repository as a denylist.
_PRIVATE_ID_SEPARATOR = rb"(?:[ \t_.-]|\xe2\x80[\x90-\x95]|\xe2\x88\x92)"
_PRIVATE_ID_SEPARATORS = _PRIVATE_ID_SEPARATOR + rb"+"
_PRIVATE_ID_SEPARATORS_OPTIONAL = _PRIVATE_ID_SEPARATOR + rb"*"
_PDF_TEST_FILES_MARKER = (
    rb"pdf"
    + _PRIVATE_ID_SEPARATORS_OPTIONAL
    + rb"test"
    + _PRIVATE_ID_SEPARATORS_OPTIONAL
    + rb"files"
)
PRIVATE_CONTENT_PATTERNS = (
    (
        "absolute user-profile path",
        re.compile(
            rb"(?<![a-z0-9])(?:[a-z]:[\\/]+users[\\/]+"
            rb"[^\\/\r\n'\"<>]+[\\/]|/users/[^/\r\n'\"<>]+/|"
            rb"/home/[^/\r\n'\"<>]+/)",
            re.IGNORECASE,
        ),
    ),
    (
        "local PDFTest Files workspace",
        re.compile(
            rb"(?:[\\/]"
            + _PDF_TEST_FILES_MARKER
            + rb"\b|\b"
            + _PDF_TEST_FILES_MARKER
            + rb"[\\/])",
            re.IGNORECASE,
        ),
    ),
    (
        "local PDF test-corpus repository",
        re.compile(
            rb"(?<![a-z0-9])1pdf"
            + _PRIVATE_ID_SEPARATORS_OPTIONAL
            + rb"test"
            + _PRIVATE_ID_SEPARATORS_OPTIONAL
            + rb"corpus(?![a-z0-9])",
            re.IGNORECASE,
        ),
    ),
    (
        "local Imported Evidence workspace",
        re.compile(
            rb"\bimported" + _PRIVATE_ID_SEPARATORS + rb"evidence[/\\]+",
            re.IGNORECASE,
        ),
    ),
)
EXTERNAL_PRIVATE_DENYLIST_ENV = "BCS_PRIVATE_RELEASE_DENYLIST_B64"
EXTERNAL_PRIVATE_DENYLIST_SCHEMA = "bcs.private-release-denylist/1.0"

PYMUPDF_SPEC = "PyMuPDF==1.28.0"
FONTTOOLS_SPEC = "fonttools==4.63.0"
RUNTIME_DEPENDENCY_SPECS = (PYMUPDF_SPEC, FONTTOOLS_SPEC)
COMMON_RUNTIME_DEPENDENCY_LOCK = REPO_ROOT / "requirements-release-common.lock"
RUNTIME_DEPENDENCY_LOCKS = {
    "cp310": REPO_ROOT / "requirements-release-cp310.lock",
    "cp311": REPO_ROOT / "requirements-release-cp311.lock",
}
EXPECTED_RUNTIME_WHEELS = {
    "common": "pymupdf-1.28.0-cp310-abi3-win_amd64.whl",
    "cp310": "fonttools-4.63.0-cp310-cp310-win_amd64.whl",
    "cp311": "fonttools-4.63.0-cp311-cp311-win_amd64.whl",
}
VENDORED_LIB_DIR = ADDON_DIR / "src" / "lib"
DETERMINISTIC_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
RELEASE_BUILD_INPUTS = (
    Path("build_release.py"),
    Path("requirements-release-common.lock"),
    Path("requirements-release-cp310.lock"),
    Path("requirements-release-cp311.lock"),
)


def _should_exclude(rel: Path) -> bool:
    parts = rel.parts
    # ``pip --target`` creates Windows console launchers whose shebang embeds
    # the build machine's absolute Python path.  The importer never calls these
    # command-line wrappers, so shipping them is both non-portable and
    # non-reproducible.  Wheel RECORD files contain hashes for those wrappers
    # and are likewise unnecessary for runtime imports.
    if (
        len(parts) >= 3
        and parts[:2] == ("src", "lib")
        and (
            parts[2] == "bin"
            or (len(parts) >= 4 and parts[3] == "bin")
        )
    ):
        return True
    if rel.name == "RECORD" and any(part.endswith(".dist-info") for part in parts):
        return True
    for part in rel.parts:
        if part in EXCLUDE_DIRS:
            return True
    if rel.name in EXCLUDE_FILES:
        return True
    if rel.suffix.lower() in EXCLUDE_SUFFIXES:
        return True
    return False


def _require_no_private_artifacts() -> None:
    """Fail when the addon tree contains private inputs or derived evidence."""
    violations: list[str] = []
    for path in sorted(ADDON_DIR.rglob("*")):
        rel = path.relative_to(ADDON_DIR)
        parts = [part.casefold() for part in rel.parts]
        if any(
            part == "imported evidence"
            or part.startswith("pdftest")
            or "test-corpus" in part
            or part.endswith("_assets")
            for part in parts
        ):
            violations.append(rel.as_posix())
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.casefold()
        name = path.name.casefold()
        if (
            suffix in PRIVATE_ARTIFACT_SUFFIXES
            or suffix in PRIVATE_ARCHIVE_SUFFIXES
            or ("import_report" in name and suffix in PRIVATE_REPORT_SUFFIXES)
        ):
            violations.append(rel.as_posix())

    if violations:
        preview = ", ".join(violations[:10])
        remainder = len(violations) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: private corpus artifact or generated counterpart "
            f"found under the shippable addon tree: {preview}"
        )


def _pattern_label(content: bytes) -> str | None:
    for label, pattern in PRIVATE_CONTENT_PATTERNS:
        if pattern.search(content):
            return label
    return None


def _plausible_utf16_text(content: bytes) -> bytes | None:
    """Return UTF-8 scan bytes when content is plausibly UTF-16 text."""
    if content.startswith(codecs.BOM_UTF16_LE):
        encoded_text = content[len(codecs.BOM_UTF16_LE) :]
        encoding = "utf-16-le"
    elif content.startswith(codecs.BOM_UTF16_BE):
        encoded_text = content[len(codecs.BOM_UTF16_BE) :]
        encoding = "utf-16-be"
    else:
        if len(content) < 4 or len(content) % 2:
            return None
        even = content[0::2]
        odd = content[1::2]
        even_zero_ratio = even.count(0) / len(even)
        odd_zero_ratio = odd.count(0) / len(odd)
        if odd_zero_ratio >= 0.60 and even_zero_ratio <= 0.20:
            encoded_text = content
            encoding = "utf-16-le"
        elif even_zero_ratio >= 0.60 and odd_zero_ratio <= 0.20:
            encoded_text = content
            encoding = "utf-16-be"
        else:
            return None

    try:
        text = encoded_text.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return None

    # Binary formats can have alternating zero bytes by chance. Reject decoded
    # buffers containing binary control characters before applying text regexes.
    if any(
        (ord(character) < 32 and character not in "\t\n\r\f")
        or 127 <= ord(character) <= 159
        for character in text
    ):
        return None
    return text.encode("utf-8")


def _load_external_private_denylist() -> tuple[str, ...]:
    """Load private identifiers from a secret environment value.

    The public repository must never contain the owner's private drawing names.
    Release automation therefore supplies a base64-encoded JSON document through
    a masked secret.  Validation errors intentionally never echo its contents.
    """
    encoded = os.environ.get(EXTERNAL_PRIVATE_DENYLIST_ENV, "")
    try:
        decoded = base64.b64decode(encoded, validate=True)
        payload = json.loads(decoded.decode("utf-8"))
        terms = payload["terms"]
        if payload.get("schema") != EXTERNAL_PRIVATE_DENYLIST_SCHEMA:
            raise ValueError("schema")
        if not isinstance(terms, list) or not 1 <= len(terms) <= 4096:
            raise ValueError("terms")
        normalized: list[str] = []
        seen: set[str] = set()
        for term in terms:
            if not isinstance(term, str) or any(
                ord(character) < 32 or (character.isspace() and character != " ")
                for character in term
            ):
                raise ValueError("term")
            value = unicodedata.normalize("NFKC", term).casefold().strip(" ")
            if not 4 <= len(value) <= 512:
                raise ValueError("term")
            if value not in seen:
                normalized.append(value)
                seen.add(value)
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Release blocked: a valid external private release denylist is required."
        ) from exc
    return tuple(normalized)


def _external_private_content_match(
    content: bytes, external_terms: tuple[str, ...]
) -> bool:
    candidate_texts: list[str] = []
    try:
        candidate_texts.append(content.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        # Keep searchable text around invalid byte sequences without assuming
        # that an opaque binary payload is wholly textual.
        candidate_texts.append(content.decode("utf-8", errors="ignore"))
    utf16_content = _plausible_utf16_text(content)
    if utf16_content is not None:
        candidate_texts.append(utf16_content.decode("utf-8", errors="strict"))
    normalized_candidates = tuple(
        unicodedata.normalize("NFKC", text).casefold() for text in candidate_texts
    )
    return any(
        term in candidate
        for term in external_terms
        for candidate in normalized_candidates
    )


def _external_private_text_match(text: str, external_terms: tuple[str, ...]) -> bool:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return any(term in normalized for term in external_terms)


def _raise_external_private_match(scope: str) -> None:
    """Raise a fixed diagnostic that cannot disclose the match or its path."""
    raise RuntimeError(f"Release blocked: external private denylist matched {scope}.")


def _tracked_index_entries() -> tuple[tuple[str, str | None], ...]:
    """Return every tracked index path and its blob ID when it has one."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--stage", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Release source must be a Git checkout so tracked repository data "
            "can be privacy-checked."
        ) from exc

    entries: list[tuple[str, str | None]] = []
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        metadata, encoded_path = record.split(b"\t", 1)
        mode, object_id, _stage = metadata.decode("ascii").split()
        path = encoded_path.decode("utf-8", errors="surrogateescape")
        entries.append((path, None if mode == "160000" else object_id))
    return tuple(entries)


def _tracked_blob_contents(object_ids: tuple[str, ...]) -> dict[str, bytes]:
    """Read tracked blobs in one Git batch without exposing repository paths."""
    unique_ids = tuple(dict.fromkeys(object_ids))
    if not unique_ids:
        return {}
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "cat-file", "--batch"],
            input=("\n".join(unique_ids) + "\n").encode("ascii"),
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Release source must be a Git checkout so tracked repository data "
            "can be privacy-checked."
        ) from exc

    contents: dict[str, bytes] = {}
    output = proc.stdout
    offset = 0
    try:
        for requested_id in unique_ids:
            header_end = output.index(b"\n", offset)
            object_id, object_type, encoded_size = output[offset:header_end].split()
            size = int(encoded_size)
            content_start = header_end + 1
            content_end = content_start + size
            if object_type != b"blob" or output[content_end : content_end + 1] != b"\n":
                raise ValueError("unexpected object")
            contents[requested_id] = output[content_start:content_end]
            if object_id.decode("ascii") != requested_id:
                raise ValueError("unexpected object id")
            offset = content_end + 1
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(
            "Release blocked: tracked repository data could not be privacy-checked."
        ) from exc
    return contents


def _eligible_tracked_text(content: bytes) -> bool:
    """Apply Git-like binary rejection while retaining plausible UTF-16 text."""
    return b"\0" not in content[:8000] or _plausible_utf16_text(content) is not None


def _tracked_worktree_content(path_name: str) -> bytes | None:
    """Read a tracked working-tree entry without following a final symlink."""
    try:
        resolved_root = REPO_ROOT.resolve(strict=True)
        candidate = REPO_ROOT / Path(path_name)
        resolved_parent = candidate.parent.resolve(strict=True)
        resolved_parent.relative_to(resolved_root)
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            return os.readlink(candidate).encode("utf-8", errors="surrogateescape")
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return candidate.read_bytes()
    except FileNotFoundError:
        # The staged blob is still scanned even when the worktree file is absent.
        return None
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            "Release blocked: tracked repository data could not be privacy-checked."
        ) from exc


def _require_no_external_private_tracked_repository_data(
    external_terms: tuple[str, ...],
) -> None:
    """Scan tracked names plus eligible index and working-tree text."""
    entries = _tracked_index_entries()
    if any(
        _external_private_text_match(path, external_terms)
        for path, _object_id in entries
    ):
        _raise_external_private_match("tracked repository data")

    blobs = _tracked_blob_contents(
        tuple(object_id for _path, object_id in entries if object_id is not None)
    )
    if any(
        _eligible_tracked_text(content)
        and _external_private_content_match(content, external_terms)
        for content in blobs.values()
    ):
        _raise_external_private_match("tracked repository data")

    for path, _object_id in entries:
        content = _tracked_worktree_content(path)
        if (
            content is not None
            and _eligible_tracked_text(content)
            and _external_private_content_match(content, external_terms)
        ):
            _raise_external_private_match("tracked repository data")


def _private_content_label(
    content: bytes, external_terms: tuple[str, ...] = ()
) -> str | None:
    """Return the private identifier label in ASCII, UTF-8, or UTF-16 text."""
    if external_terms and _external_private_content_match(content, external_terms):
        return "external private denylist"
    # Preserve the exact byte boundary used for existing ASCII/UTF-8 checks,
    # including identifiers embedded in otherwise opaque byte buffers.
    label = _pattern_label(content)
    if label is not None:
        return label
    utf16_content = _plausible_utf16_text(content)
    if utf16_content is None:
        return None
    return _pattern_label(utf16_content)


_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
)


def _is_link_or_reparse(path: Path) -> bool:
    """Inspect a path itself without following its target."""
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError(
            f"Release blocked: could not inspect addon path without dereference: {path}"
        ) from exc
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _require_no_linked_addon_content() -> None:
    """Reject selected add-on entries that redirect to another filesystem path."""
    violations: list[str] = []
    for path in sorted(ADDON_DIR.rglob("*")):
        rel = path.relative_to(ADDON_DIR)
        if _should_exclude(rel) or rel.parts[:2] == ("src", "lib"):
            continue
        if _is_link_or_reparse(path):
            violations.append(rel.as_posix())

    if violations:
        preview = ", ".join(violations[:10])
        remainder = len(violations) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: linked or reparse-point content is not allowed "
            f"in selected addon files: {preview}"
        )


def _require_no_linked_runtime_content(runtime_dir: Path) -> None:
    """Reject selected generated-runtime entries that redirect elsewhere."""
    violations: list[str] = []
    for path in sorted(runtime_dir.rglob("*")):
        rel = Path("src") / "lib" / path.relative_to(runtime_dir)
        if _should_exclude(rel):
            continue
        if _is_link_or_reparse(path):
            violations.append(rel.as_posix())

    if violations:
        preview = ", ".join(violations[:10])
        remainder = len(violations) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: linked or reparse-point content is not allowed "
            f"in selected runtime files: {preview}"
        )


def _capture_first_party_files() -> tuple[tuple[tuple[Path, bytes], ...], int]:
    """Capture deterministic first-party archive inputs and excluded-file count."""
    snapshot: list[tuple[Path, bytes]] = []
    skipped = 0
    for path in sorted(ADDON_DIR.rglob("*")):
        rel = path.relative_to(ADDON_DIR)
        if _should_exclude(rel) or rel.parts[:2] == ("src", "lib"):
            if path.is_file():
                skipped += 1
            continue
        if _is_link_or_reparse(path):
            raise RuntimeError(
                "Release blocked: linked or reparse-point content is not "
                f"allowed in selected addon files: {rel.as_posix()}"
            )
        if not path.is_file():
            continue
        snapshot.append((rel, path.read_bytes()))
    return tuple(snapshot), skipped


def _capture_runtime_files(
    runtime_dir: Path, external_terms: tuple[str, ...]
) -> tuple[tuple[tuple[Path, bytes], ...], int]:
    """Capture verified generated-runtime bytes before archive construction."""
    snapshot: list[tuple[Path, bytes]] = []
    skipped = 0
    for path in sorted(runtime_dir.rglob("*")):
        runtime_rel = path.relative_to(runtime_dir)
        rel = Path("src") / "lib" / runtime_rel
        if _should_exclude(rel):
            if path.is_file():
                skipped += 1
            continue
        if _is_link_or_reparse(path):
            raise RuntimeError(
                "Release blocked: linked or reparse-point content is not "
                f"allowed in selected runtime files: {rel.as_posix()}"
            )
        if not path.is_file():
            continue
        content = path.read_bytes()
        private_label = _private_content_label(content, external_terms)
        if private_label is not None:
            if private_label == "external private denylist":
                _raise_external_private_match("generated runtime data")
            raise RuntimeError(
                "Release blocked: private corpus content found in generated "
                f"runtime files: {rel.as_posix()} ({private_label})"
            )
        snapshot.append((runtime_rel, content))
    return tuple(snapshot), skipped


def _require_no_private_content(external_terms: tuple[str, ...]) -> None:
    """Reject private validation identifiers in first-party archive inputs."""
    violations: list[str] = []
    for path in sorted(ADDON_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ADDON_DIR)
        if _should_exclude(rel) or rel.parts[:2] == ("src", "lib"):
            continue
        content = path.read_bytes()
        label = _private_content_label(content, external_terms)
        if label is not None:
            if label == "external private denylist":
                _raise_external_private_match("shippable addon data")
            violations.append(f"{rel.as_posix()} ({label})")

    if violations:
        preview = ", ".join(violations[:10])
        remainder = len(violations) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: private corpus content found in shippable "
            f"addon files: {preview}"
        )


def _git_blob_oid(content: bytes) -> str:
    """Return the repository-format Git blob ID for exact in-memory bytes."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "hash-object", "--stdin"],
            input=content,
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Release source must be a Git checkout so package bytes can be "
            "bound to a committed tree."
        ) from exc
    return proc.stdout.decode("ascii").strip()


def _capture_source_commit() -> str:
    """Resolve and validate one stable commit identity for this build attempt."""
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "rev-parse",
                "--verify",
                "--quiet",
                "HEAD^{commit}",
            ],
            check=True,
            capture_output=True,
        )
        source_commit = proc.stdout.decode("ascii").strip()
    except (OSError, UnicodeError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Release blocked: source repository must have a real HEAD commit."
        ) from exc
    if _COMMIT_OID.fullmatch(source_commit) is None:
        raise RuntimeError(
            "Release blocked: source repository returned an invalid commit identity."
        )
    return source_commit


def _require_commit_bound_sources(
    first_party_snapshot: tuple[tuple[Path, bytes], ...],
    *,
    source_commit: str,
) -> dict[Path, bytes]:
    """Bind every selected first-party path and byte buffer to one commit."""
    if _COMMIT_OID.fullmatch(source_commit) is None:
        raise RuntimeError("Release blocked: invalid source commit identity.")
    try:
        addon_rel = ADDON_DIR.resolve().relative_to(REPO_ROOT.resolve())
    except ValueError as exc:
        raise RuntimeError(
            "Release blocked: addon source is outside the release repository."
        ) from exc

    release_pathspecs = [path.as_posix() for path in RELEASE_BUILD_INPUTS]
    pathspecs = [addon_rel.as_posix(), *release_pathspecs]
    try:
        verified_proc = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{source_commit}^{{commit}}",
            ],
            check=True,
            capture_output=True,
        )
        if verified_proc.stdout.decode("ascii").strip() != source_commit:
            raise RuntimeError(
                "Release blocked: source commit verification changed identity."
            )
    except (OSError, UnicodeError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Release blocked: source repository must have a real HEAD commit."
        ) from exc

    try:
        tree_proc = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "ls-tree",
                "-r",
                "-z",
                "--full-tree",
                source_commit,
                "--",
                *pathspecs,
            ],
            check=True,
            capture_output=True,
        )
        staged_proc = subprocess.run(
            [
                "git",
                "-C",
                str(REPO_ROOT),
                "diff",
                "--cached",
                "--name-only",
                "-z",
                source_commit,
                "--",
                *pathspecs,
            ],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            "Release source must be a Git checkout so package bytes can be "
            "bound to a committed tree."
        ) from exc

    head_entries: dict[str, tuple[str, str]] = {}
    for record in tree_proc.stdout.split(b"\0"):
        if not record:
            continue
        metadata, encoded_path = record.split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split()
        if object_type != "blob":
            continue
        path = encoded_path.decode("utf-8", errors="surrogateescape")
        head_entries[path] = (mode, object_id)

    prefix = addon_rel.as_posix().rstrip("/") + "/"
    head_addon_entries = {
        name[len(prefix) :]: entry
        for name, entry in head_entries.items()
        if name.startswith(prefix)
        and not _should_exclude(Path(name[len(prefix) :]))
        and Path(name[len(prefix) :]).parts[:2] != ("src", "lib")
    }
    snapshot = {rel.as_posix(): content for rel, content in first_party_snapshot}

    uncommitted_paths = sorted(set(snapshot) - set(head_addon_entries))
    if uncommitted_paths:
        preview = ", ".join(uncommitted_paths[:10])
        remainder = len(uncommitted_paths) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: untracked shippable file is not present in HEAD: "
            f"{preview}"
        )

    missing_snapshot_paths = sorted(set(head_addon_entries) - set(snapshot))
    if missing_snapshot_paths:
        preview = ", ".join(missing_snapshot_paths[:10])
        remainder = len(missing_snapshot_paths) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: committed shippable file is missing from the "
            f"captured addon snapshot: {preview}"
        )

    modified_snapshot_paths = sorted(
        path
        for path, content in snapshot.items()
        if _git_blob_oid(content) != head_addon_entries[path][1]
    )
    if modified_snapshot_paths:
        preview = ", ".join(modified_snapshot_paths[:10])
        remainder = len(modified_snapshot_paths) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: shippable source content differs from HEAD: "
            f"{preview}"
        )

    missing_build_inputs = sorted(set(release_pathspecs) - set(head_entries))
    if missing_build_inputs:
        raise RuntimeError(
            "Release blocked: release build input is not present in HEAD: "
            + ", ".join(missing_build_inputs)
        )

    build_input_snapshot: dict[Path, bytes] = {}
    modified_build_inputs: list[str] = []
    for path in release_pathspecs:
        source_path = REPO_ROOT / Path(path)
        try:
            content = source_path.read_bytes()
        except OSError:
            modified_build_inputs.append(path)
            continue
        if _git_blob_oid(content) != head_entries[path][1]:
            modified_build_inputs.append(path)
            continue
        build_input_snapshot[Path(path)] = content
    if modified_build_inputs:
        raise RuntimeError(
            "Release blocked: release build input differs from HEAD: "
            + ", ".join(modified_build_inputs)
        )

    staged = {
        name
        for name in staged_proc.stdout.decode(
            "utf-8", errors="surrogateescape"
        ).split("\0")
        if name
    }
    staged_build_inputs = sorted(set(release_pathspecs) & staged)
    if staged_build_inputs:
        raise RuntimeError(
            "Release blocked: release build input has staged changes relative "
            "to HEAD: "
            + ", ".join(staged_build_inputs)
        )

    staged_shippable = sorted(
        name[len(prefix) :]
        for name in staged
        if name.startswith(prefix)
        and not _should_exclude(Path(name[len(prefix) :]))
        and Path(name[len(prefix) :]).parts[:2] != ("src", "lib")
    )
    if staged_shippable:
        preview = ", ".join(staged_shippable[:10])
        remainder = len(staged_shippable) - 10
        if remainder > 0:
            preview += f", ... (+{remainder} more)"
        raise RuntimeError(
            "Release blocked: shippable source has staged changes relative to HEAD: "
            f"{preview}"
        )
    return build_input_snapshot


def _write_deterministic_bytes(
    archive: zipfile.ZipFile,
    content: bytes,
    archive_name: Path,
    external_terms: tuple[str, ...] = (),
) -> None:
    """Write exact bytes without leaking host timestamps or permissions."""
    if external_terms and _external_private_text_match(
        archive_name.as_posix(), external_terms
    ):
        _raise_external_private_match("an archive member name")
    info = zipfile.ZipInfo(
        archive_name.as_posix(), date_time=DETERMINISTIC_ZIP_TIMESTAMP
    )
    # Stored entries avoid zlib/backend-dependent bytes across build toolchains.
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    archive.writestr(info, content)


def _write_deterministic_file(
    archive: zipfile.ZipFile,
    source: Path,
    archive_name: Path,
    external_terms: tuple[str, ...] = (),
) -> None:
    """Write one regular file without leaking host timestamps or permissions."""
    _write_deterministic_bytes(
        archive, source.read_bytes(), archive_name, external_terms
    )


def _read_version() -> str:
    pkg_xml = ADDON_DIR / "package.xml"
    if pkg_xml.exists():
        text = pkg_xml.read_text(encoding="utf-8")
        m = re.search(r"<version>(.*?)</version>", text)
        if m:
            return m.group(1).strip()
    return "0.0.0"


def _snapshot_package_version(
    first_party_snapshot: tuple[tuple[Path, bytes], ...]
) -> str:
    """Read the package identity from the same captured bytes that will ship."""
    for relative_path, content in first_party_snapshot:
        if relative_path.as_posix() != "package.xml":
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeError as exc:
            raise RuntimeError("Release blocked: package version is invalid.") from exc
        match = re.search(r"<version>(.*?)</version>", text)
        if match:
            return match.group(1).strip()
    return "0.0.0"


def _compose_release_members(
    first_party_snapshot: tuple[tuple[Path, bytes], ...],
    runtime_snapshot: tuple[tuple[Path, bytes], ...],
    *,
    source_commit: str,
    package_version: str,
    artifact_name: str,
) -> tuple[dict[str, bytes], dict]:
    """Create and validate one closed mapping from already captured bytes."""
    members: dict[str, bytes] = {}
    captured = (
        (
            (Path("PDFVectorImporter") / relative_path).as_posix(),
            content,
        )
        for relative_path, content in first_party_snapshot
    )
    runtime = (
        (
            (
                Path("PDFVectorImporter")
                / "src"
                / "lib"
                / relative_path
            ).as_posix(),
            content,
        )
        for relative_path, content in runtime_snapshot
    )
    try:
        for member_name, content in (*captured, *runtime):
            if member_name in members:
                raise ValueError("MANIFEST_MEMBER_COLLISION")
            members[member_name] = content
        document = _CANDIDATE_MANIFEST.build_candidate_file_manifest(
            members,
            source_commit=source_commit,
            package_version=package_version,
            artifact_name=artifact_name,
        )
        manifest_bytes = _CANDIDATE_MANIFEST.canonical_candidate_manifest_bytes(
            document
        )
        if not manifest_bytes or MANIFEST_MEMBER in members:
            raise ValueError("MANIFEST_MEMBER_COLLISION")
        members[MANIFEST_MEMBER] = manifest_bytes
        problems = _CANDIDATE_MANIFEST.validate_candidate_archive_members(
            document, members
        )
        if problems:
            raise ValueError(problems[0])
    except ValueError as exc:
        code = str(exc)
        if not code.startswith("MANIFEST_"):
            code = "MANIFEST_INVALID_DOCUMENT"
        raise RuntimeError(
            f"Release blocked: candidate archive composition failed ({code})."
        ) from None
    return members, document


def _validate_written_release_zip(
    temporary_path: Path,
    expected_members: dict[str, bytes],
    document: dict,
) -> None:
    """Reopen a temporary ZIP and prove exact regular-file byte closure."""
    actual: dict[str, bytes] = {}
    identities: set[str] = set()
    with zipfile.ZipFile(temporary_path, "r") as archive:
        infos = archive.infolist()
        for info in infos:
            name = info.filename
            if name in actual:
                raise RuntimeError("Release blocked: temporary ZIP has duplicate members.")
            identity = unicodedata.normalize("NFC", name).casefold()
            if identity in identities:
                raise RuntimeError("Release blocked: temporary ZIP has aliased members.")
            identities.add(identity)
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if (
                info.is_dir()
                or name.endswith("/")
                or (info.create_system == 3 and file_type not in (0, stat.S_IFREG))
            ):
                raise RuntimeError("Release blocked: temporary ZIP has non-file members.")
            if info.flag_bits & 0x1:
                raise RuntimeError("Release blocked: temporary ZIP has encrypted members.")
            if info.compress_type != zipfile.ZIP_STORED:
                raise RuntimeError(
                    "Release blocked: temporary ZIP has unsupported compression."
                )
            actual[name] = archive.read(info)

    if set(actual) != set(expected_members):
        raise RuntimeError("Release blocked: temporary ZIP member set drifted.")
    if any(actual[name] != content for name, content in expected_members.items()):
        raise RuntimeError("Release blocked: temporary ZIP member bytes drifted.")
    problems = _CANDIDATE_MANIFEST.validate_candidate_archive_members(document, actual)
    if problems:
        raise RuntimeError(
            "Release blocked: temporary ZIP manifest closure failed ("
            + problems[0]
            + ")."
        )


def _candidate_freecad_pythons() -> list[Path]:
    candidates: list[Path] = []
    for key in ("FREECAD_PYTHON", "FREECAD_PYTHON_EXE"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    for pattern in (
        r"C:\Program Files\FreeCAD 1.1\bin\python.exe",
        r"C:\Program Files\FreeCAD*\bin\python.exe",
        r"C:\Program Files (x86)\FreeCAD*\bin\python.exe",
    ):
        if "*" in pattern:
            candidates.extend(Path("C:/").glob(pattern.replace("C:\\", "").replace("\\", "/")))
        else:
            candidates.append(Path(pattern))
    candidates.append(Path(sys.executable))
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen and candidate.exists():
            seen.add(key)
            unique.append(candidate)
    return unique


def _python_version(python_exe: Path) -> tuple[int, int]:
    code = "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"
    proc = subprocess.run(
        [str(python_exe), "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    major, minor = proc.stdout.strip().split(".", 1)
    return int(major), int(minor)


def _resolve_runtime_pythons(
    *, python310: Path | None, python311: Path | None
) -> dict[str, Path]:
    """Validate the exact interpreters used to build each shipped ABI tree."""
    requested = {
        "cp310": (Path(python310) if python310 is not None else None, (3, 10)),
        "cp311": (Path(python311) if python311 is not None else None, (3, 11)),
    }
    for runtime_tag, (python_exe, expected_version) in requested.items():
        if python_exe is None or not python_exe.is_file():
            raise RuntimeError(
                f"A CPython {expected_version[0]}.{expected_version[1]} interpreter "
                f"is required for the {runtime_tag} release payload."
            )

    resolved: dict[str, Path] = {}
    for runtime_tag, (python_exe, expected_version) in requested.items():
        assert python_exe is not None
        actual_version = _python_version(python_exe)
        if actual_version != expected_version:
            raise RuntimeError(
                f"{python_exe} must be CPython "
                f"{expected_version[0]}.{expected_version[1]}, not "
                f"{actual_version[0]}.{actual_version[1]}."
            )
        resolved[runtime_tag] = python_exe
    return resolved


def _lib_has_runtime_dependencies(python_exe: Path, lib_dir: Path) -> bool:
    if not lib_dir.is_dir():
        return False
    lib_root = lib_dir.resolve()
    code = (
        "import os, pathlib, sys\n"
        f"root = pathlib.Path({str(lib_root)!r}).resolve()\n"
        "sys.path.insert(0, str(root))\n"
        "import pymupdf as fitz\n"
        "import fontTools\n"
        "def local(module):\n"
        "    origin = pathlib.Path(module.__file__).resolve()\n"
        "    try:\n"
        "        origin.relative_to(root)\n"
        "    except ValueError:\n"
        "        raise SystemExit(3)\n"
        "    return origin\n"
        "local(fitz); local(fontTools)\n"
        "def version_tuple(value):\n"
        "    parts = []\n"
        "    for token in str(value).split('.'):\n"
        "        digits = ''.join(ch for ch in token if ch.isdigit())\n"
        "        if not digits: break\n"
        "        parts.append(int(digits))\n"
        "    return tuple(parts)\n"
        "fitz_version = version_tuple(getattr(fitz, '__version__', '') "
        "or getattr(fitz, 'VersionBind', ''))\n"
        "font_version = version_tuple(getattr(fontTools, 'version', '') "
        "or getattr(fontTools, '__version__', ''))\n"
        "if fitz_version != (1, 28, 0):\n"
        "    raise SystemExit(4)\n"
        "if font_version != (4, 63, 0):\n"
        "    raise SystemExit(5)\n"
        "print('OK')\n"
    )
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", code],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "OK"


def _runtime_has_runtime_dependencies(
    python_exe: Path, abi_dir: Path, common_dir: Path
) -> bool:
    """Verify that one interpreter imports only its ABI and shared payloads."""
    abi_root = abi_dir.resolve()
    common_root = common_dir.resolve()
    code = (
        "import pathlib, sys\n"
        f"abi = pathlib.Path({str(abi_root)!r}).resolve()\n"
        f"common = pathlib.Path({str(common_root)!r}).resolve()\n"
        "sys.path[:0] = [str(abi), str(common)]\n"
        "import pymupdf as fitz\n"
        "import fontTools\n"
        "def under(module, root):\n"
        "    try:\n"
        "        pathlib.Path(module.__file__).resolve().relative_to(root)\n"
        "    except ValueError:\n"
        "        raise SystemExit(3)\n"
        "under(fitz, common)\n"
        "under(fontTools, abi)\n"
        "fitz_version = str(getattr(fitz, '__version__', '') or "
        "getattr(fitz, 'VersionBind', ''))\n"
        "font_version = str(getattr(fontTools, 'version', '') or "
        "getattr(fontTools, '__version__', ''))\n"
        "if not fitz_version.startswith('1.28.0'):\n"
        "    raise SystemExit(4)\n"
        "if not font_version.startswith('4.63.0'):\n"
        "    raise SystemExit(5)\n"
        "print('OK')\n"
    )
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", code],
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "OK"


def _prune_vendored_pymupdf(lib_dir: Path = VENDORED_LIB_DIR) -> None:
    """Remove PyMuPDF development files that are not needed at runtime."""
    for rel in (
        Path("pymupdf") / "mupdf-devel",
    ):
        path = lib_dir / rel
        if path.exists():
            shutil.rmtree(path)


def _install_locked_runtime(
    python_exe: Path, *, target_dir: Path, lock_path: Path
) -> None:
    if not lock_path.is_file():
        raise RuntimeError(f"Hashed release dependency lock is missing: {lock_path}")
    target_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(python_exe),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--no-deps",
            "--only-binary",
            ":all:",
            "--target",
            str(target_dir),
            "--requirement",
            str(lock_path),
        ],
        check=True,
    )


def _require_installed_wheel_tag(
    target_dir: Path, project: str, expected_tag: str
) -> None:
    normalized = project.lower().replace("-", "_")
    candidates = [
        path
        for path in target_dir.glob("*.dist-info")
        if path.name.lower().replace("-", "_").startswith(normalized + "_")
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"Expected one installed {project} wheel metadata directory in {target_dir}"
        )
    wheel_path = candidates[0] / "WHEEL"
    try:
        tags = {
            line.partition(":")[2].strip()
            for line in wheel_path.read_text(encoding="utf-8").splitlines()
            if line.lower().startswith("tag:")
        }
    except OSError as exc:
        raise RuntimeError(f"Installed wheel metadata is missing: {wheel_path}") from exc
    if expected_tag not in tags:
        raise RuntimeError(
            f"Installed {project} wheel must carry tag {expected_tag}; "
            f"found {sorted(tags)}"
        )


def _discover_runtime_pythons() -> tuple[Path | None, Path | None]:
    discovered: dict[tuple[int, int], Path] = {}
    for candidate in _candidate_freecad_pythons():
        try:
            version = _python_version(candidate)
        except (OSError, subprocess.SubprocessError, TypeError, ValueError):
            continue
        if version in {(3, 10), (3, 11)} and version not in discovered:
            discovered[version] = candidate
    return discovered.get((3, 10)), discovered.get((3, 11))


def _write_runtime_manifest(runtime_dir: Path) -> None:
    manifest = {
        "schema": "bcs.freecad.runtime-matrix/1.0",
        "platform": "win_amd64",
        "common": {
            "path": "common",
            "wheel": EXPECTED_RUNTIME_WHEELS["common"],
            "wheel_tag": "cp310-abi3-win_amd64",
        },
        "runtimes": {
            "cp310": {
                "path": "cp310",
                "wheel": EXPECTED_RUNTIME_WHEELS["cp310"],
                "wheel_tag": "cp310-cp310-win_amd64",
            },
            "cp311": {
                "path": "cp311",
                "wheel": EXPECTED_RUNTIME_WHEELS["cp311"],
                "wheel_tag": "cp311-cp311-win_amd64",
            },
        },
    }
    (runtime_dir / "runtime-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def ensure_runtime_dependencies(
    *,
    vendor: bool = True,
    runtime_dir: Path | None = None,
    python310: Path | None = None,
    python311: Path | None = None,
    common_lock: Path | None = None,
    runtime_locks: dict[str, Path] | None = None,
) -> dict[str, Path]:
    """Rebuild runtime dependencies from the committed hash-locked wheels."""
    target_dir = Path(runtime_dir) if runtime_dir is not None else VENDORED_LIB_DIR
    if not vendor:
        raise RuntimeError(
            "Existing PDFVectorImporter/src/lib bytes are ignored by Git and "
            "therefore are not lock-bound release evidence. Run build_release.py "
            "without --no-vendor-deps to rebuild them from the release locks."
        )

    if python310 is None and python311 is None:
        python310, python311 = _discover_runtime_pythons()
    runtimes = _resolve_runtime_pythons(
        python310=python310,
        python311=python311,
    )
    common_lock_path = (
        Path(common_lock)
        if common_lock is not None
        else COMMON_RUNTIME_DEPENDENCY_LOCK
    )
    runtime_lock_paths = (
        {tag: Path(path) for tag, path in runtime_locks.items()}
        if runtime_locks is not None
        else RUNTIME_DEPENDENCY_LOCKS
    )
    if set(runtime_lock_paths) != {"cp310", "cp311"}:
        raise RuntimeError(
            "Release runtime locks must contain exactly cp310 and cp311."
        )

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    common_dir = target_dir / "common"
    print(f"Vendoring {PYMUPDF_SPEC} into {common_dir}")
    _install_locked_runtime(
        runtimes["cp310"],
        target_dir=common_dir,
        lock_path=common_lock_path,
    )
    _require_installed_wheel_tag(
        common_dir,
        "PyMuPDF",
        "cp310-abi3-win_amd64",
    )
    _prune_vendored_pymupdf(common_dir)

    for runtime_tag, python_exe in runtimes.items():
        abi_dir = target_dir / runtime_tag
        print(f"Vendoring {FONTTOOLS_SPEC} into {abi_dir}")
        _install_locked_runtime(
            python_exe,
            target_dir=abi_dir,
            lock_path=runtime_lock_paths[runtime_tag],
        )
        _require_installed_wheel_tag(
            abi_dir,
            "fonttools",
            f"{runtime_tag}-{runtime_tag}-win_amd64",
        )
        if not _runtime_has_runtime_dependencies(python_exe, abi_dir, common_dir):
            raise RuntimeError(
                f"Runtime dependency import failed for {runtime_tag} from "
                f"{abi_dir} and {common_dir}"
            )

    _write_runtime_manifest(target_dir)
    return runtimes


def build(
    out_dir: Path,
    *,
    vendor_deps: bool = True,
    python310: Path | None = None,
    python311: Path | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    external_private_terms = _load_external_private_denylist()
    _require_no_external_private_tracked_repository_data(external_private_terms)
    _require_no_linked_addon_content()
    _require_no_private_artifacts()
    _require_no_private_content(external_private_terms)
    first_party_snapshot, first_party_skipped = _capture_first_party_files()
    source_commit = _capture_source_commit()
    if _COMMIT_OID.fullmatch(source_commit) is None:
        raise RuntimeError("Release blocked: invalid source commit identity.")
    verified_build_inputs = _require_commit_bound_sources(
        first_party_snapshot, source_commit=source_commit
    )
    if _capture_source_commit() != source_commit:
        raise RuntimeError(
            "Release blocked: source commit changed during source validation."
        )
    version = _snapshot_package_version(first_party_snapshot)
    zip_name = f"FreeCAD-PDF-Importer_v{version}.zip"
    zip_path = out_dir / zip_name
    with tempfile.TemporaryDirectory(prefix="fc-release-runtime-") as runtime_tmp:
        runtime_tmp_path = Path(runtime_tmp)
        runtime_dir = runtime_tmp_path / "lib"
        immutable_locks: dict[str, Path] | None = None
        immutable_common_lock: Path | None = None
        if verified_build_inputs:
            lock_dir = runtime_tmp_path / "locks"
            lock_dir.mkdir(parents=True)
            immutable_common_lock = lock_dir / COMMON_RUNTIME_DEPENDENCY_LOCK.name
            try:
                immutable_common_lock.write_bytes(
                    verified_build_inputs[
                        COMMON_RUNTIME_DEPENDENCY_LOCK.relative_to(REPO_ROOT)
                    ]
                )
                immutable_locks = {}
                for runtime_tag, source_lock in RUNTIME_DEPENDENCY_LOCKS.items():
                    immutable_path = lock_dir / source_lock.name
                    immutable_path.write_bytes(
                        verified_build_inputs[source_lock.relative_to(REPO_ROOT)]
                    )
                    immutable_locks[runtime_tag] = immutable_path
            except (KeyError, OSError, ValueError) as exc:
                raise RuntimeError(
                    "Release blocked: verified dependency lock snapshot is incomplete."
                ) from exc
        ensure_runtime_dependencies(
            vendor=vendor_deps,
            runtime_dir=runtime_dir,
            python310=python310,
            python311=python311,
            common_lock=immutable_common_lock,
            runtime_locks=immutable_locks,
        )
        _require_no_linked_addon_content()
        _require_no_linked_runtime_content(runtime_dir)
        _require_no_private_artifacts()
        _require_no_private_content(external_private_terms)
        runtime_snapshot, runtime_skipped = _capture_runtime_files(
            runtime_dir, external_private_terms
        )

        skipped = first_party_skipped + runtime_skipped

        archive_names = tuple(
            Path("PDFVectorImporter") / rel for rel, _content in first_party_snapshot
        ) + tuple(
            Path("PDFVectorImporter") / "src" / "lib" / runtime_rel
            for runtime_rel, _content in runtime_snapshot
        )
        if any(
            _external_private_text_match(name.as_posix(), external_private_terms)
            for name in archive_names
        ):
            _raise_external_private_match("an archive member name")
        for rel, content in first_party_snapshot:
            private_label = _private_content_label(content, external_private_terms)
            if private_label is not None:
                if private_label == "external private denylist":
                    _raise_external_private_match("shippable addon data")
                raise RuntimeError(
                    "Release blocked: private corpus content found in shippable "
                    f"addon files: {rel.as_posix()} ({private_label})"
                )

        members, document = _compose_release_members(
            first_party_snapshot,
            runtime_snapshot,
            source_commit=source_commit,
            package_version=version,
            artifact_name=zip_name,
        )
        file_count = len(members)
        owned_temporary: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                dir=out_dir, prefix=f".{zip_name}.", suffix=".tmp"
            )
            owned_temporary = Path(temporary_name)
            os.close(descriptor)
            with zipfile.ZipFile(
                owned_temporary, "w", compression=zipfile.ZIP_STORED
            ) as archive:
                for member_name in sorted(members, key=lambda value: value.encode("utf-8")):
                    _write_deterministic_bytes(
                        archive,
                        members[member_name],
                        Path(member_name),
                        external_private_terms,
                    )
            _validate_written_release_zip(owned_temporary, members, document)
            os.replace(owned_temporary, zip_path)
        except Exception:
            if owned_temporary is not None:
                try:
                    owned_temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            raise RuntimeError(
                "Release blocked: deterministic release ZIP publication failed."
            ) from None

    print(f"Built: {zip_path}")
    print(f"  {file_count} files included, {skipped} excluded")
    return zip_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build PDFVectorImporter release zip")
    parser.add_argument(
        "--out", default=str(REPO_ROOT),
        help="Output directory (default: repo root)"
    )
    parser.add_argument(
        "--no-vendor-deps",
        action="store_true",
        help=(
            "Refuse dependency generation (release builds then fail because ignored "
            "src/lib bytes cannot be trusted without clean hash-locked vendoring)."
        ),
    )
    parser.add_argument(
        "--python310",
        type=Path,
        help="Exact CPython 3.10 executable used for the cp310 payload.",
    )
    parser.add_argument(
        "--python311",
        type=Path,
        help="Exact CPython 3.11 executable used for the cp311 payload.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out).resolve()
    zip_path = build(
        out_dir,
        vendor_deps=not args.no_vendor_deps,
        python310=args.python310,
        python311=args.python311,
    )
    print(f"\nRelease ready: {zip_path}")


if __name__ == "__main__":
    main()
