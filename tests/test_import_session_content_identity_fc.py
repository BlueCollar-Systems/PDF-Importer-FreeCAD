"""A same-version code update cannot resume an older certified checkpoint."""
from dataclasses import dataclass
import os

import pytest

from PDFVectorImporter.src import PDFImportSession as session
from test_import_session_fc import FakeDocument


@dataclass
class Options:
    pages: list[int]
    text_mode: str = "3d_text"


def identity(root):
    return session.build_identity(source_sha256="a" * 64, source_name="drawing.pdf",
        opts=Options([1, 2]), importer_version="4.0.111", requested_pages=[1, 2],
        importer_code_sha256=session.importer_content_sha256(root))


def test_same_version_changed_core_rejects_resume_without_touching_completed_geometry(tmp_path):
    root = tmp_path / "PDFVectorImporter"
    code = root / "src" / "PDFImporterCore.py"
    code.parent.mkdir(parents=True)
    code.write_text("value = 1\n", encoding="utf-8")
    original = identity(root)
    document = FakeDocument()
    host = session.create_session_object(document, original)
    page = document.addObject("App::DocumentObjectGroup", "PDF_Page_1")
    session.update_session_object(host, status="cancelled", completed_pages=[1], page_groups={1: page.Name})
    assert session.find_matching_session(document, identity(root)) is host
    before = code.stat()
    code.write_text("value = 2\n", encoding="utf-8")
    os.utime(code, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed = identity(root)
    assert changed["importer_version"] == original["importer_version"]
    assert changed["importer_content_sha256"] != original["importer_content_sha256"]
    snapshot = list(document.Objects)
    with pytest.raises(ValueError, match="[Cc]ode.*changed|[Cc]hanged.*code"):
        session.find_matching_session(document, changed)
    assert document.Objects == snapshot
    assert session.read_session_object(host)["completed_pages"] == [1]


def test_runtime_caches_and_noncode_files_do_not_change_content_identity(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "PDFImporterCore.py").write_text("value = 1\n")
    expected = session.importer_content_sha256(tmp_path)
    (tmp_path / "src" / "lib").mkdir()
    (tmp_path / "src" / "lib" / "runtime.py").write_text("foreign_runtime = 1\n")
    (tmp_path / "README.md").write_text("new instructions\n")
    (tmp_path / "src" / "__pycache__").mkdir()
    (tmp_path / "src" / "__pycache__" / "cached.py").write_text("cache = 1\n")
    assert session.importer_content_sha256(tmp_path) == expected


def test_legacy_checkpoint_is_rejected_clearly_and_existing_output_is_kept(tmp_path):
    (tmp_path / "engine.py").write_text("value = 1\n")
    current = identity(tmp_path)
    document = FakeDocument()
    host = session.create_session_object(document, current)
    host.PDFImportSessionSchema = "bcs.freecad_import_session/1.0"
    page = document.addObject("App::DocumentObjectGroup", "PDF_Page_1")
    session.update_session_object(host, status="cancelled", completed_pages=[1], page_groups={1: page.Name})
    snapshot = list(document.Objects)
    with pytest.raises(ValueError, match="older|[Oo]ld|identity"):
        session.read_session_object(host)
    with pytest.raises(ValueError, match="older|[Oo]ld|identity"):
        session.find_matching_session(document, current)
    assert document.Objects == snapshot


def test_content_identity_is_bounded_and_rejects_empty_package(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="code|Python"):
        session.importer_content_sha256(tmp_path)
    (tmp_path / "engine.py").write_text("value = 1\n")
    monkeypatch.setattr(session, "MAX_IMPORTER_CODE_BYTES", 2)
    with pytest.raises(ValueError, match="limit|bound"):
        session.importer_content_sha256(tmp_path)
