#!/usr/bin/env python3
"""Smoke-test the shipped FreeCAD release ZIP."""
from __future__ import annotations

import argparse
import glob
import importlib.util
import io
import json
import stat
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
from pathlib import Path


REQUIRED_MEMBERS = {
    "PDFVectorImporter/Init.py",
    "PDFVectorImporter/InitGui.py",
    "PDFVectorImporter/package.xml",
    "PDFVectorImporter/src/PDFImporterCore.py",
    "PDFVectorImporter/pdfcadcore/fitz_loader.py",
    "PDFVectorImporter/src/lib/runtime-manifest.json",
    "PDFVectorImporter/src/lib/common/pymupdf/__init__.py",
    "PDFVectorImporter/src/lib/cp310/fontTools/__init__.py",
    "PDFVectorImporter/src/lib/cp311/fontTools/__init__.py",
}

REQUIRED_WINDOWS_RUNTIME = {
    "PDFVectorImporter/src/lib/common/pymupdf/_extra.pyd",
    "PDFVectorImporter/src/lib/common/pymupdf/_mupdf.pyd",
}


def _load_candidate_manifest_contract():
    """Load the pinned checkout contract without importing the addon package."""
    contract_path = (
        Path(__file__).resolve().parents[1]
        / "PDFVectorImporter"
        / "candidate_manifest.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_freecad_smoke_candidate_manifest", contract_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate manifest contract unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CANDIDATE_MANIFEST = _load_candidate_manifest_contract()
MANIFEST_MEMBER = _CANDIDATE_MANIFEST.MANIFEST_MEMBER

_WINDOWS_RESERVED_ARTIFACT_STEMS = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        "conin$",
        "conout$",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        *(f"com{number}" for number in "¹²³"),
        *(f"lpt{number}" for number in "¹²³"),
    }
)


def _unsafe_zip_member_name(name: str) -> bool:
    if (
        not name
        or "\\" in name
        or name.startswith("/")
        or not name.startswith("PDFVectorImporter/")
        or ":" in name
        or unicodedata.normalize("NFC", name) != name
        or any(ord(character) < 32 for character in name)
    ):
        return True
    parts = name.rstrip("/").split("/")
    return any(not part or part in {".", ".."} for part in parts)


def _contract_rejects_member_name(name: str) -> bool:
    if name == MANIFEST_MEMBER:
        return False
    try:
        _CANDIDATE_MANIFEST.build_candidate_file_manifest(
            {name: b""},
            source_commit="0" * 40,
            package_version="0.0.0",
            artifact_name="FreeCAD-PDF-Importer_v0.0.0.zip",
        )
    except ValueError:
        return True
    return False


def _valid_portable_artifact_name(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if (
        value in {".", ".."}
        or unicodedata.normalize("NFC", value) != value
        or value.endswith((".", " "))
        or any(character in '/\\:<>"|?*' for character in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    stem = value.split(".", 1)[0].casefold()
    return stem not in _WINDOWS_RESERVED_ARTIFACT_STEMS


def validate_release_zip_manifest_bytes(
    zip_bytes: bytes,
    *,
    artifact_name: str,
) -> list[str]:
    """Validate one immutable ZIP byte stream; return sorted pathless codes."""
    if not _valid_portable_artifact_name(artifact_name):
        return ["RELEASE_ZIP_ARTIFACT_NAME_MISMATCH"]
    if type(zip_bytes) is not bytes:
        return ["RELEASE_ZIP_IO_ERROR"]
    try:
        structural: set[str] = set()
        members: dict[str, bytes] = {}
        identities: set[str] = set()
        manifest_count = 0
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as archive:
            infos = archive.infolist()
            seen_names: set[str] = set()
            for info in infos:
                name = info.filename
                if name == MANIFEST_MEMBER:
                    manifest_count += 1
                if name in seen_names:
                    structural.add("RELEASE_ZIP_DUPLICATE_MEMBER")
                seen_names.add(name)
                identity = unicodedata.normalize("NFC", name).casefold()
                is_alias = identity in identities and name not in members
                if is_alias:
                    structural.add("RELEASE_ZIP_MEMBER_ALIAS")
                identities.add(identity)

                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                is_nonregular = (
                    info.is_dir()
                    or name.endswith("/")
                    or (info.create_system == 0 and bool(info.external_attr & 0x10))
                    or (info.create_system == 3 and file_type not in (0, stat.S_IFREG))
                )
                if is_nonregular:
                    structural.add("RELEASE_ZIP_NONREGULAR_MEMBER")
                if info.flag_bits & 0x1:
                    structural.add("RELEASE_ZIP_ENCRYPTED_MEMBER")
                if info.compress_type != zipfile.ZIP_STORED:
                    structural.add("RELEASE_ZIP_UNSUPPORTED_COMPRESSION")
                if _unsafe_zip_member_name(name) and not (
                    is_alias and unicodedata.normalize("NFC", name) != name
                ):
                    structural.add("RELEASE_ZIP_UNSAFE_MEMBER")
                if (
                    not is_nonregular
                    and not is_alias
                    and _contract_rejects_member_name(name)
                ):
                    structural.add("RELEASE_ZIP_UNSAFE_MEMBER")
                if name not in members and not structural:
                    members[name] = archive.read(info)

            if manifest_count == 0:
                structural.add("RELEASE_ZIP_MANIFEST_MISSING")
            elif manifest_count > 1:
                structural.add("RELEASE_ZIP_MANIFEST_DUPLICATE")
            if structural:
                return sorted(structural)

        manifest_payload = members.get(MANIFEST_MEMBER)
        if manifest_payload is None:
            return ["RELEASE_ZIP_MANIFEST_MISSING"]
        document, problems = _CANDIDATE_MANIFEST.parse_candidate_file_manifest(
            manifest_payload
        )
        if problems:
            return sorted(set(problems))
        assert document is not None
        result: set[str] = set()
        if document.get("artifact_name") != artifact_name:
            result.add("RELEASE_ZIP_ARTIFACT_NAME_MISMATCH")
        result.update(
            _CANDIDATE_MANIFEST.validate_candidate_archive_members(document, members)
        )
        return sorted(result)
    except zipfile.BadZipFile:
        return ["RELEASE_ZIP_CORRUPT"]
    except (OSError, ValueError, RuntimeError, EOFError):
        return ["RELEASE_ZIP_IO_ERROR"]
    except Exception:
        return ["RELEASE_ZIP_IO_ERROR"]


def validate_release_zip_manifest(zip_path: Path) -> list[str]:
    """Capture one path snapshot and validate it without reopening the path."""
    try:
        artifact_name = zip_path.name
        captured = zip_path.read_bytes()
        if type(captured) is not bytes:
            return ["RELEASE_ZIP_IO_ERROR"]
    except Exception:
        return ["RELEASE_ZIP_IO_ERROR"]
    try:
        return validate_release_zip_manifest_bytes(
            captured,
            artifact_name=artifact_name,
        )
    except Exception:
        return ["RELEASE_ZIP_IO_ERROR"]


def _resolve_zip(pattern: str) -> Path:
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise SystemExit(f"No release ZIP matched {pattern!r}")
    return Path(matches[-1]).resolve()


def _smoke_runtime(python_exe: Path, addon_root: Path, runtime_tag: str) -> None:
    expected = {"cp310": (3, 10), "cp311": (3, 11)}[runtime_tag]
    code = (
        "import pathlib, sys; "
        f"assert sys.version_info[:2] == {expected!r}; "
        f"root = pathlib.Path({str(addon_root)!r}); "
        "sys.path.insert(0, str(root)); "
        "from PDFVectorImporter.runtime_paths import "
        "activate_bundled_runtime_if_available; "
        "runtime = activate_bundled_runtime_if_available(root / "
        "'PDFVectorImporter'); "
        f"assert runtime is not None and runtime.runtime_tag == {runtime_tag!r}; "
        "import pymupdf as fitz, fontTools; "
        "assert callable(getattr(fitz, 'open', None)); "
        "print(getattr(fitz, '__version__', '') or "
        "getattr(fitz, 'VersionBind', ''), fontTools.__version__)"
    )
    proc = subprocess.run(
        [str(python_exe), "-c", code],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise SystemExit(
            f"{runtime_tag} bundled runtime import failed: "
            + (proc.stderr.strip() or proc.stdout.strip())
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", help="Release ZIP path or glob pattern")
    parser.add_argument("--python310", type=Path)
    parser.add_argument("--python311", type=Path)
    args = parser.parse_args()

    zip_path = _resolve_zip(args.zip_path)
    manifest_problems = validate_release_zip_manifest(zip_path)
    if manifest_problems:
        print(
            "Release ZIP manifest failed: "
            + zip_path.name
            + ": "
            + ", ".join(manifest_problems)
        )
        return 1
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = set(zf.namelist())
        missing = sorted(REQUIRED_MEMBERS - names)
        missing_runtime = sorted(REQUIRED_WINDOWS_RUNTIME - names)
        linux_binaries = sorted(
            name
            for name in names
            if name.startswith("PDFVectorImporter/src/lib/") and name.endswith(".so")
        )
        if missing:
            raise SystemExit(
                "Release ZIP is missing required FreeCAD members: "
                + ", ".join(missing)
            )
        if missing_runtime:
            raise SystemExit(
                "Release ZIP is missing required Windows PyMuPDF runtime files: "
                + ", ".join(missing_runtime)
            )
        if linux_binaries:
            raise SystemExit(
                "Release ZIP contains Linux shared objects in the vendored runtime: "
                + ", ".join(linux_binaries[:8])
            )

        manifest = json.loads(
            zf.read("PDFVectorImporter/src/lib/runtime-manifest.json")
        )
        if set(manifest.get("runtimes", {})) != {"cp310", "cp311"}:
            raise SystemExit("Release ZIP runtime manifest is missing cp310 or cp311")

        if sys.platform == "win32":
            if args.python310 is None or args.python311 is None:
                raise SystemExit(
                    "Windows release smoke requires --python310 and --python311"
                )
            with tempfile.TemporaryDirectory(prefix="fc_release_zip_") as tmp:
                zf.extractall(tmp)
                extracted_root = Path(tmp)
                _smoke_runtime(args.python310, extracted_root, "cp310")
                _smoke_runtime(args.python311, extracted_root, "cp311")

    print(f"Release ZIP smoke passed: {zip_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
