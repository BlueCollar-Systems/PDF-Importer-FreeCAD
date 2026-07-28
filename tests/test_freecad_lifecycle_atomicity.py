from __future__ import annotations

import inspect
import hashlib
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore.fitz_loader import PdfOpenError  # noqa: E402
from pdfcadcore.parts_bootstrap import build_parts_bootstrap  # noqa: E402
from pdfcadcore.source_provenance import (  # noqa: E402
    SourceProvenanceObject,
    build_source_provenance,
)


def _blank_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "source.pdf"
    document = core.fitz.open()
    try:
        document.new_page()
        document.save(str(path))
    finally:
        document.close()
    return path


class _HostObject:
    def __init__(self, document, name: str, type_id: str) -> None:
        self.Document = document
        self.Name = name
        self.Label = name
        self.TypeId = type_id

    def isDerivedFrom(self, type_id: str) -> bool:  # noqa: N802 - FreeCAD API
        return self.TypeId.startswith(type_id)


class _Document:
    def __init__(self, *, fail_phase: str | None = None) -> None:
        self.Name = "LifecycleAttempt"
        self.Objects = []
        self.events: list[str] = []
        self.fail_phase = fail_phase
        self.committed = False
        self.aborted = False
        self._recompute_calls = 0

    def addObject(self, kind: str, name: str):  # noqa: N802 - FreeCAD API
        obj = _HostObject(self, f"{name}_{len(self.Objects)}", kind)
        self.Objects.append(obj)
        return obj

    def getObject(self, name: str):  # noqa: N802 - FreeCAD API
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def removeObject(self, name: str) -> None:  # noqa: N802 - FreeCAD API
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def openTransaction(self, _name: str) -> None:  # noqa: N802 - FreeCAD API
        self.events.append("open")

    def commitTransaction(self) -> None:  # noqa: N802 - FreeCAD API
        self.events.append("commit")
        if self.fail_phase == "commit":
            raise RuntimeError("injected commit failure")
        self.committed = True

    def abortTransaction(self) -> None:  # noqa: N802 - FreeCAD API
        self.events.append("abort")
        self.aborted = True

    def recompute(self, *_args) -> None:
        self.events.append("recompute")
        self._recompute_calls += 1
        if self.fail_phase == "recompute" and self._recompute_calls == 1:
            raise RuntimeError("injected recompute failure")


class _Page:
    rect = SimpleNamespace(height=100.0, width=100.0)

    def get_text(self, kind: str):
        if kind in {"rawdict", "dict"}:
            return {"blocks": []}
        return ""


class _Pdf:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def __len__(self) -> int:
        return 1

    def load_page(self, _index: int):
        return _Page()

    def close(self) -> None:
        self.closed = True


def _configure_import(
    monkeypatch: pytest.MonkeyPatch,
    document: _Document,
    *,
    fail_phase: str | None = None,
    report_paths: tuple[Path, Path, Path] | None = None,
) -> None:
    monkeypatch.setattr(core, "_ensure_doc", lambda: document)
    monkeypatch.setattr(
        core,
        "_ensure_doc_with_ownership",
        lambda: (document, False),
        raising=False,
    )
    monkeypatch.setattr(core, "_open_pdf_source_attempt", lambda _opts: _Pdf())
    monkeypatch.setattr(core, "cleanup_temp_files", lambda: None)
    monkeypatch.setattr(core, "_msg", lambda *_args: None)
    monkeypatch.setattr(core, "_warn", lambda *_args: None)
    monkeypatch.setattr(core, "_err", lambda *_args: None)
    monkeypatch.setattr(core, "_autofit_import_view", lambda _doc: document.events.append("autofit"))

    authority = object()

    def capture(opts, _pages):
        opts._page_visual_authority = authority
        opts._page_visual_session_anchor = {"attempt": "test"}
        return authority

    monkeypatch.setattr(core, "_capture_page_visual_runtime_authority", capture)

    def import_page(_pdf, _path, _page, _opts, _doc):
        document.events.append("page")
        document.addObject("Part::Feature", "Attempt")
        if fail_phase == "page":
            raise RuntimeError("injected page failure")
        if fail_phase == "cancel":
            raise core.ImportCancelled("cancelled in injected phase")
        return None, None

    monkeypatch.setattr(core, "_import_pdf_page_inner", import_page)

    def persistence(_doc, _objects, _baseline, *, opts=None):
        document.events.append("persistence")
        if fail_phase == "persistence":
            raise RuntimeError("injected persistence failure")
        return (
            {
                "verified": True,
                "counts": {
                    "vector_primitives": 1,
                    "images": 0,
                },
            },
            {"verified": True},
        )

    monkeypatch.setattr(core, "_build_persistence_host_evidence", persistence)

    def write_report(*, output_path, opts, **_kwargs):
        document.events.append("report")
        report = Path(output_path)
        provenance = report.with_name("source_provenance.json")
        bootstrap = report.with_name("parts_bootstrap.json")
        report.write_bytes(b"new-report")
        bootstrap.write_bytes(b"new-bootstrap")
        if report_paths is not None:
            expected_report, expected_provenance, expected_bootstrap = report_paths
            assert report == expected_report
            assert provenance == expected_provenance
            assert bootstrap == expected_bootstrap
            provenance.write_bytes(b"new-provenance")
        if fail_phase == "report":
            raise RuntimeError("injected report failure")
        ready = fail_phase != "live_gate"
        opts._live_import_report = SimpleNamespace(
            input={"sha256": opts._pdf_sha256},
            extra={"import_contract_ready": {"ready": ready}},
            _page_visual_authority=opts._page_visual_authority,
        )
        return output_path

    monkeypatch.setattr(core, "write_import_report", write_report)


def test_cancel_paths_raise_a_dedicated_signal_and_never_return_partial_groups() -> None:
    source = inspect.getsource(core._import_pdf_page_inner)

    assert issubclass(core.ImportCancelled, Exception)
    assert "_progress_check_cancel" not in source
    assert "progress.wasCanceled()" not in source
    assert source.count("_raise_if_import_cancelled(") >= 4


def test_snapshot_is_private_read_only_and_verified_around_external_consumers(
    tmp_path: Path,
) -> None:
    source = _blank_pdf(tmp_path)
    opts = core.ImportOptions()
    snapshot = Path(core._initialize_pdf_source_attempt(str(source), opts))

    assert stat.S_IMODE(snapshot.stat().st_mode) & stat.S_IWUSR == 0
    assert opts._pdf_source_provenance["snapshot_read_only"] is True

    with pytest.raises(ValueError, match="snapshot.*digest"):
        with core._verified_pdf_snapshot_consumer(opts, "mutation-test") as bound_path:
            os.chmod(bound_path, stat.S_IWRITE | stat.S_IREAD)
            Path(bound_path).write_bytes(b"mutated")


def test_source_cleanup_failure_is_retryable_and_retains_owner_and_path(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "attempt.pdf"
    snapshot.write_bytes(b"payload")

    class Owner:
        def cleanup(self) -> None:
            raise OSError("locked")

    owner = Owner()
    authority = object()
    live_report = SimpleNamespace(_page_visual_authority=authority)
    opts = core.ImportOptions()
    opts._pdf_source_snapshot_owner = owner
    opts._pdf_source_snapshot_path = str(snapshot)
    opts._pdf_source_bytes = b"payload"
    opts._pdf_sha256 = "a" * 64
    opts._pdf_source_provenance = {"snapshot_path": str(snapshot)}
    opts._page_visual_authority = authority
    opts._live_import_report = live_report

    with pytest.raises(core.ImportCleanupError) as failure:
        core._dispose_pdf_source_attempt(opts)

    assert failure.value.retryable is True
    assert opts._pdf_source_snapshot_owner is owner
    assert opts._pdf_source_snapshot_path == str(snapshot)
    assert opts._pdf_source_bytes == b"payload"
    assert opts._page_visual_authority is None
    assert live_report._page_visual_authority is None
    assert opts._source_cleanup_error["retryable"] is True


def test_source_disposal_fails_closed_when_live_authority_cannot_detach(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "attempt.pdf"
    snapshot.write_bytes(b"payload")
    authority = object()

    class Owner:
        def cleanup(self) -> None:
            snapshot.unlink(missing_ok=True)

    class LockedReport:
        @property
        def _page_visual_authority(self):
            return authority

        @_page_visual_authority.setter
        def _page_visual_authority(self, _value):
            raise RuntimeError("authority is locked")

    owner = Owner()
    opts = core.ImportOptions()
    opts._pdf_source_snapshot_owner = owner
    opts._pdf_source_snapshot_path = str(snapshot)
    opts._pdf_source_bytes = b"payload"
    opts._pdf_sha256 = "a" * 64
    opts._pdf_source_provenance = {"snapshot_path": str(snapshot)}
    opts._page_visual_authority = authority
    opts._live_import_report = LockedReport()

    with pytest.raises(core.ImportCleanupError, match="retried"):
        core._dispose_pdf_source_attempt(opts)

    assert opts._pdf_source_snapshot_owner is owner
    assert opts._pdf_source_snapshot_path == str(snapshot)
    assert opts._pdf_source_bytes == b"payload"
    assert opts._live_import_report._page_visual_authority is authority


def test_attempt_path_rollback_restores_preexisting_bytes_and_removes_new_files(
    tmp_path: Path,
) -> None:
    existing = tmp_path / "existing.json"
    created = tmp_path / "created.json"
    existing.write_bytes(b"old")
    opts = core.ImportOptions()

    core._journal_attempt_path(opts, existing)
    core._journal_attempt_path(opts, created)
    existing.write_bytes(b"new")
    created.write_bytes(b"created")

    result = core._rollback_attempt_paths(opts)

    assert result["cleanup_complete"] is True
    assert existing.read_bytes() == b"old"
    assert not created.exists()


def test_attempt_path_cleanup_failure_is_recorded_and_retained_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = tmp_path / "created.json"
    opts = core.ImportOptions()
    core._journal_attempt_path(opts, created)
    created.write_bytes(b"created")
    real_unlink = Path.unlink

    def fail_target(path: Path, *args, **kwargs):
        if path == created:
            raise PermissionError("locked")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_target)
    result = core._rollback_attempt_paths(opts)

    assert result["cleanup_complete"] is False
    assert result["cleanup_errors"]
    assert created.exists()
    assert opts._attempt_path_journal


def test_success_keeps_transaction_open_through_live_gate_and_commits_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    report = tmp_path / "report.json"
    document = _Document()
    _configure_import(monkeypatch, document)
    original_gate = core._require_live_import_contract_ready

    def gate(opts):
        document.events.append("live_gate")
        return original_gate(opts)

    monkeypatch.setattr(core, "_require_live_import_contract_ready", gate)
    opts = core.ImportOptions(
        pages=[1],
        import_text=False,
        import_report_path=str(report),
    )

    result = core.import_pdf(str(source), opts)

    assert result is True
    assert document.committed is True
    assert document.aborted is False
    assert document.events.index("recompute") < document.events.index("persistence")
    assert document.events.index("persistence") < document.events.index("report")
    assert document.events.index("report") < document.events.index("live_gate")
    assert document.events.index("live_gate") < document.events.index("commit")
    assert document.events.index("commit") < document.events.index("autofit")
    assert document.events[-1] == "autofit"
    assert opts._pdf_source_snapshot_owner is None
    assert opts._pdf_source_snapshot_path is None
    assert opts._page_visual_authority is None
    assert opts._live_import_report._page_visual_authority is None


def test_live_gate_requires_authority_digest_and_both_persistence_inventories() -> None:
    authority = object()
    opts = core.ImportOptions()
    opts._pdf_sha256 = "a" * 64
    opts._page_visual_authority = authority
    opts._live_import_report = SimpleNamespace(
        input={"sha256": "b" * 64},
        extra={"import_contract_ready": {"ready": True}},
        _page_visual_authority=authority,
    )
    opts._report_extra = {
        "actual_host_object_inventory": {"verified": True},
        "save_reopen_inventory": {"verified": True},
    }

    with pytest.raises(core.ImportLifecycleError, match="digest"):
        core._require_live_import_contract_ready(opts)

    opts._live_import_report.input["sha256"] = opts._pdf_sha256
    del opts._report_extra["save_reopen_inventory"]
    with pytest.raises(core.ImportLifecycleError, match="save/reopen"):
        core._require_live_import_contract_ready(opts)


@pytest.mark.parametrize(
    "phase",
    ("page", "recompute", "persistence", "report", "live_gate", "commit"),
)
def test_every_post_open_boundary_failure_aborts_objects_and_publications(
    phase: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    report = tmp_path / "report.json"
    provenance = tmp_path / "source_provenance.json"
    bootstrap = tmp_path / "parts_bootstrap.json"
    report.write_bytes(b"old-report")
    document = _Document(fail_phase=phase)
    existing = document.addObject("Part::Feature", "Existing")
    _configure_import(
        monkeypatch,
        document,
        fail_phase=phase,
        report_paths=(report, provenance, bootstrap),
    )
    opts = core.ImportOptions(
        pages=[1],
        import_text=False,
        import_report_path=str(report),
    )

    with pytest.raises(RuntimeError):
        core.import_pdf(str(source), opts)

    assert document.aborted is True
    assert document.committed is False
    assert document.Objects == [existing]
    assert report.read_bytes() == b"old-report"
    assert not provenance.exists()
    assert not bootstrap.exists()
    assert opts._pdf_source_snapshot_owner is None
    assert opts._pdf_source_snapshot_path is None
    assert "autofit" not in document.events


def test_cancellation_returns_exact_false_and_never_leaves_partial_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    report = tmp_path / "report.json"
    document = _Document()
    existing = document.addObject("Part::Feature", "Existing")
    _configure_import(monkeypatch, document, fail_phase="cancel")
    opts = core.ImportOptions(pages=[1], import_text=False, import_report_path=str(report))

    result = core.import_pdf(str(source), opts)

    assert result is False
    assert type(result) is bool
    assert document.aborted is True
    assert document.committed is False
    assert document.Objects == [existing]
    assert not report.exists()
    assert opts._pdf_source_snapshot_owner is None


def test_source_mutation_during_page_consumer_aborts_before_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    report = tmp_path / "report.json"
    document = _Document()
    existing = document.addObject("Part::Feature", "Existing")
    _configure_import(monkeypatch, document)

    def mutate_page(_pdf, snapshot_path, _page, _opts, _doc):
        document.addObject("Part::Feature", "Attempt")
        os.chmod(snapshot_path, stat.S_IWRITE | stat.S_IREAD)
        Path(snapshot_path).write_bytes(b"mutated while consumed")
        return None, None

    monkeypatch.setattr(core, "_import_pdf_page_inner", mutate_page)
    opts = core.ImportOptions(pages=[1], import_text=False, import_report_path=str(report))

    with pytest.raises(ValueError, match="snapshot.*digest"):
        core.import_pdf(str(source), opts)

    assert document.aborted is True
    assert document.committed is False
    assert document.Objects == [existing]
    assert not report.exists()


def test_early_read_and_open_failures_raise_typed_errors_and_never_return_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = _Document()
    monkeypatch.setattr(
        core,
        "_ensure_doc_with_ownership",
        lambda: (document, False),
    )
    opts = core.ImportOptions(pages=[1])

    with pytest.raises(PdfOpenError):
        core.import_pdf(str(tmp_path / "missing.pdf"), opts)

    source = _blank_pdf(tmp_path)
    monkeypatch.setattr(
        core,
        "_open_pdf_source_attempt",
        lambda _opts: (_ for _ in ()).throw(PdfOpenError("open", "injected open failure")),
    )
    with pytest.raises(PdfOpenError, match="injected open failure"):
        core.import_pdf(str(source), opts)

    assert "open" not in document.events
    assert "recompute" not in document.events
    assert opts._pdf_source_snapshot_owner is None


def test_invalid_selected_pages_are_a_failure_not_empty_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    document = _Document()
    _configure_import(monkeypatch, document)
    opts = core.ImportOptions(pages=[9], import_text=False)

    with pytest.raises(ValueError, match="selected page"):
        core.import_pdf(str(source), opts)

    assert document.committed is False
    assert document.Objects == []


def test_gui_only_announces_completion_after_an_exact_true_result() -> None:
    source = (
        REPO_ROOT / "PDFVectorImporter" / "src" / "PDFImporterCmd.py"
    ).read_text(encoding="utf-8")

    assignment = source.index("result = core.import_pdf(")
    cancelled_guard = source.index("if result is False:", assignment)
    invalid_guard = source.index("if result is not True:", cancelled_guard)
    success_message = source.index("PDF import complete", invalid_guard)
    assert assignment < cancelled_guard < invalid_guard < success_message


class _PixmapPayload:
    width = 16
    height = 12
    alpha = False
    n = 3
    colorspace = SimpleNamespace(n=3)

    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def save(self, path: str) -> None:
        Path(path).write_bytes(self.payload)


def test_embedded_image_asset_is_immutable_across_interleaved_attempts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"embedded-image-payload"
    image_digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path)
    source_bytes = b"interleaved-attempt-source"
    source_digest = hashlib.sha256(source_bytes).hexdigest()

    first_opts = core.ImportOptions()
    first_opts._pdf_sha256 = source_digest
    first_opts._pdf_source_bytes = source_bytes
    first_opts._pdf_source_provenance = {
        "pdf_sha256": source_digest,
        "size_bytes": len(source_bytes),
    }
    expected_first = tmp_path / ("embedded_%s.png" % image_digest)
    first_path, first_evidence = core._persist_embedded_image_asset(
        _PixmapPayload(payload),
        first_opts,
        page_number=1,
        xref=7,
    )

    second_opts = core.ImportOptions()
    second_opts._pdf_sha256 = source_digest
    second_opts._pdf_source_bytes = source_bytes
    second_opts._pdf_source_provenance = {
        "pdf_sha256": source_digest,
        "size_bytes": len(source_bytes),
    }
    second_path, second_evidence = core._persist_embedded_image_asset(
        _PixmapPayload(payload),
        second_opts,
        page_number=1,
        xref=7,
    )

    assert first_path == expected_first.resolve()
    assert first_path == second_path
    assert first_path.read_bytes() == payload
    assert second_path.read_bytes() == payload
    assert first_evidence["source_asset_sha256"] == image_digest
    assert first_evidence["pdf_sha256"] == source_digest
    assert first_evidence["page_number"] == 1
    assert first_evidence["source_xref"] == 7
    assert second_evidence["pdf_sha256"] == source_digest

    # Simulate attempt B accepting the shared immutable asset before attempt A
    # fails.  A's rollback must never delete B's accepted bytes.
    core._accept_attempt_paths(second_opts)
    first_rollback = core._rollback_attempt_paths(first_opts)
    assert first_rollback["cleanup_complete"] is True
    assert second_path.read_bytes() == payload
    assert first_evidence["asset_content_addressed"] is True
    assert first_evidence["asset_atomic_publish"] is True
    assert first_evidence["asset_shared_immutable"] is True


def test_shared_raster_survives_reuser_rollback_before_publisher_accepts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"shared-raster-inverse-order"
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path)

    publisher = core.ImportOptions()
    published_path, published_evidence = core._persist_content_addressed_pixmap(
        _PixmapPayload(payload),
        publisher,
        asset_kind="embedded",
    )
    reuser = core.ImportOptions()
    reused_path, reused_evidence = core._persist_content_addressed_pixmap(
        _PixmapPayload(payload),
        reuser,
        asset_kind="embedded",
    )

    assert reused_path == published_path
    assert published_evidence["asset_published_by_attempt"] is True
    assert reused_evidence["asset_published_by_attempt"] is False
    reuser_rollback = core._rollback_attempt_paths(reuser)
    assert reuser_rollback["cleanup_complete"] is True
    assert published_path.read_bytes() == payload

    core._accept_attempt_paths(publisher)
    assert published_path.read_bytes() == payload


def test_content_addressed_publish_race_loser_never_owns_winner_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b"shared-raster-race-winner"
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path)
    real_link = os.link
    raced: list[Path] = []

    def competing_link(source: str, target: str) -> None:
        if not raced:
            raced.append(Path(target))
            Path(target).write_bytes(Path(source).read_bytes())
            raise FileExistsError(target)
        real_link(source, target)

    monkeypatch.setattr(os, "link", competing_link)
    loser = core.ImportOptions()
    target, evidence = core._persist_content_addressed_pixmap(
        _PixmapPayload(payload),
        loser,
        asset_kind="embedded",
    )

    assert raced == [target]
    assert evidence["asset_published_by_attempt"] is False
    assert target.read_bytes() == payload
    rollback = core._rollback_attempt_paths(loser)
    assert rollback["cleanup_complete"] is True
    assert target.read_bytes() == payload


def test_embedded_image_bind_failure_removes_exact_new_plane_and_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset = tmp_path / "embedded.png"
    asset.write_bytes(b"asset")
    existing = SimpleNamespace(Name="Existing")

    class Plane:
        Name = "Image_1"

    plane = Plane()

    class Document:
        Objects = [existing]

        def addObject(self, kind, name):  # noqa: N802 - FreeCAD API
            assert (kind, name) == ("Image::ImagePlane", "Image")
            self.Objects.append(plane)
            return plane

        def getObject(self, name):  # noqa: N802 - FreeCAD API
            return next((obj for obj in self.Objects if obj.Name == name), None)

        def removeObject(self, name):  # noqa: N802 - FreeCAD API
            self.Objects = [obj for obj in self.Objects if obj.Name != name]

    class Group:
        Group: list[object] = []

        def addObject(self, obj):  # noqa: N802 - FreeCAD API
            self.Group.append(obj)

        def removeObject(self, obj):  # noqa: N802 - FreeCAD API
            self.Group = [candidate for candidate in self.Group if candidate is not obj]

    document = Document()
    group = Group()
    monkeypatch.setattr(
        core,
        "_bind_embedded_image_host_asset",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("injected embedded image bind failure")
        ),
    )

    with pytest.raises(RuntimeError, match="bind failure"):
        core._create_bound_embedded_image_plane(
            fc_doc=document,
            image_group=group,
            image_path=asset,
            image_asset_evidence={
                "pdf_sha256": "a" * 64,
                "source_asset_sha256": hashlib.sha256(b"asset").hexdigest(),
            },
            page_number=1,
            xref=7,
            x_size=10.0,
            y_size=20.0,
            placement=object(),
        )

    assert document.Objects == [existing]
    assert group.Group == []
    image_region = inspect.getsource(core._import_pdf_page_inner).split(
        "# ── Embedded images ──", 1
    )[1].split("# ── Final cleanup / placement ──", 1)[0]
    assert "_warn(" not in image_region


def test_embedded_image_temporary_cleanup_failure_remains_owned_for_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path)
    opts = core.ImportOptions()
    source_bytes = b"cleanup-attempt-source"
    opts._pdf_sha256 = hashlib.sha256(source_bytes).hexdigest()
    opts._pdf_source_bytes = source_bytes
    opts._pdf_source_provenance = {
        "pdf_sha256": opts._pdf_sha256,
        "size_bytes": len(source_bytes),
    }
    real_unlink = Path.unlink
    failed_paths: list[Path] = []

    def fail_first_embedded_unlink(path: Path, *args, **kwargs):
        if not failed_paths and path.parent == tmp_path and path.name.startswith("embedded."):
            failed_paths.append(path)
            raise PermissionError("injected temporary cleanup lock")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_first_embedded_unlink)

    with pytest.raises(core.ImportCleanupError, match="temporary raster asset"):
        core._persist_embedded_image_asset(
            _PixmapPayload(b"cleanup-owned-image"),
            opts,
            page_number=1,
            xref=9,
        )

    assert len(failed_paths) == 1
    failed_path = failed_paths[0]
    assert failed_path.exists()
    assert any(
        Path(entry["path"]) == failed_path and entry["existed"] is False
        for entry in opts._attempt_path_journal.values()
    )

    rollback = core._rollback_attempt_paths(opts)
    assert rollback["cleanup_complete"] is True
    assert not failed_path.exists()


def test_embedded_image_host_includes_and_verifies_exact_asset_bytes(
    tmp_path: Path,
) -> None:
    payload = b"included-embedded-image"
    asset = tmp_path / (hashlib.sha256(payload).hexdigest() + ".png")
    asset.write_bytes(payload)

    class Host:
        PropertiesList: list[str] = []
        ImageFile = str(asset)
        _property_types: dict[str, str] = {}

        def addProperty(self, kind: str, name: str, _group: str) -> None:  # noqa: N802
            self.PropertiesList = list(self.PropertiesList) + [name]
            self._property_types = dict(self._property_types)
            self._property_types[name] = kind
            setattr(self, name, "")

        def getTypeIdOfProperty(self, name: str) -> str:  # noqa: N802
            return self._property_types.get(name, "")

    host = Host()
    evidence = core._bind_embedded_image_host_asset(
        host,
        asset,
        pdf_sha256="c" * 64,
        page_number=2,
        xref=11,
    )

    assert host.PDFRasterFile == str(asset.resolve())
    assert host.PDFSourceSHA256 == "c" * 64
    assert host.PDFRasterSHA256 == hashlib.sha256(payload).hexdigest()
    assert host.PDFImagePageNumber == 2
    assert host.PDFImageSourceXRef == 11
    assert evidence["raster_content_verified"] is True
    assert evidence["included_file_path_sha256"] == host.PDFRasterSHA256
    persisted = core._host_object_content_snapshot(
        host,
        "Image::ImagePlane",
        "",
        shape_evidence_mode="cheap",
    )
    assert persisted["image_sha256"] == host.PDFRasterSHA256
    assert persisted["included_image_sha256"] == host.PDFRasterSHA256
    assert persisted["embedded_image_page_number"] == 2
    assert persisted["embedded_image_source_xref"] == 11
    assert persisted["raster_asset_binding_verified"] is True


def test_public_single_page_import_uses_every_common_acceptance_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    report = tmp_path / "report.json"
    document = _Document()
    _configure_import(monkeypatch, document)
    original_gate = core._require_live_import_contract_ready

    def gate(opts):
        document.events.append("live_gate")
        return original_gate(opts)

    monkeypatch.setattr(core, "_require_live_import_contract_ready", gate)
    opts = core.ImportOptions(
        import_text=False,
        import_report_path=str(report),
    )

    result = core.import_pdf_page(str(source), 1, opts, autofit=False)

    assert result is True
    assert type(result) is bool
    assert opts.pages == [1]
    assert document.committed is True
    assert document.events.index("persistence") < document.events.index("report")
    assert document.events.index("report") < document.events.index("live_gate")
    assert document.events[-1] == "commit"


def test_attempt_created_document_closes_on_cancel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    document = _Document()
    _configure_import(monkeypatch, document, fail_phase="cancel")
    monkeypatch.setattr(
        core,
        "_ensure_doc_with_ownership",
        lambda: (document, True),
        raising=False,
    )
    closed: list[str] = []
    monkeypatch.setattr(
        core,
        "FreeCAD",
        SimpleNamespace(closeDocument=lambda name: closed.append(name)),
    )

    result = core.import_pdf(
        str(source),
        core.ImportOptions(pages=[1], import_text=False),
    )

    assert result is False
    assert closed == [document.Name]


def test_attempt_created_document_closes_when_baseline_identity_capture_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    document = _Document()
    document.addObject("Part::Feature", "Initial")
    _configure_import(monkeypatch, document)
    monkeypatch.setattr(
        core,
        "_ensure_doc_with_ownership",
        lambda: (document, True),
        raising=False,
    )
    real_host_object_id = core._host_object_id
    calls = 0

    def fail_first_identity(host_obj):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected baseline identity failure")
        return real_host_object_id(host_obj)

    monkeypatch.setattr(core, "_host_object_id", fail_first_identity)
    closed: list[str] = []
    monkeypatch.setattr(
        core,
        "FreeCAD",
        SimpleNamespace(closeDocument=lambda name: closed.append(name)),
    )

    with pytest.raises(RuntimeError, match="baseline identity failure"):
        core.import_pdf(str(source), core.ImportOptions(pages=[1], import_text=False))

    assert closed == [document.Name]


def test_autofit_frames_without_changing_projection_or_orientation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _HostObject(None, "PDF_Page_1", "App::DocumentObjectGroup")
    document = SimpleNamespace(Objects=[root], recompute=lambda: None)
    events: list[object] = []
    prior = object()

    class View:
        def fitAll(self):  # noqa: N802 - FreeCAD API
            events.append("fit")

        def setCameraType(self, value):  # noqa: N802 - FreeCAD API
            events.append(("projection", value))

        def viewTop(self):  # noqa: N802 - FreeCAD API
            events.append("top")

    class Selection:
        selected = [prior]

        @classmethod
        def getSelection(cls):  # noqa: N802 - FreeCAD API
            return list(cls.selected)

        @classmethod
        def clearSelection(cls):  # noqa: N802 - FreeCAD API
            cls.selected = []

        @classmethod
        def addSelection(cls, obj):  # noqa: N802 - FreeCAD API
            cls.selected.append(obj)

    gui = SimpleNamespace(
        ActiveDocument=SimpleNamespace(ActiveView=View()),
        Selection=Selection,
        SendMsgToActiveView=lambda message: events.append(message),
        updateGui=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "FreeCADGui", gui)

    core._autofit_import_view(document)

    assert events == ["ViewSelection", "fit"]
    assert Selection.selected == [prior]


def test_preexisting_document_is_never_closed_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    document = _Document()
    _configure_import(monkeypatch, document, fail_phase="page")
    closed: list[str] = []
    monkeypatch.setattr(
        core,
        "FreeCAD",
        SimpleNamespace(closeDocument=lambda name: closed.append(name)),
    )

    with pytest.raises(RuntimeError, match="injected page failure"):
        core.import_pdf(str(source), core.ImportOptions(pages=[1], import_text=False))

    assert closed == []


def test_sidecar_builders_use_prebound_digest_and_original_display_path() -> None:
    digest = "d" * 64
    deleted_snapshot = "C:/deleted/attempt-snapshot.pdf"
    original = "C:/Drawings/original-source.pdf"

    provenance = build_source_provenance(
        import_session_id="session-bound-source",
        pdf_path=deleted_snapshot,
        source_display_path=original,
        source_sha256=digest,
        objects=[
            SourceProvenanceObject(
                object_id="text_span:1:0",
                page=1,
                source_kind="text_span",
                created_entity_type="native_text",
            )
        ],
        host_app="freecad",
        page_count=1,
    ).to_dict()
    bootstrap = build_parts_bootstrap(
        deleted_snapshot,
        source_display_path=original,
        source_sha256=digest,
        page_count=1,
    )

    assert provenance["source_pdf"]["path"] == original
    assert provenance["source_pdf"]["sha256"] == digest
    assert deleted_snapshot not in str(provenance)
    assert bootstrap["source_pdf"]["file"] == "original-source.pdf"
    assert bootstrap["source_pdf"]["path"] == original
    assert bootstrap["source_pdf"]["sha256"] == digest
    assert deleted_snapshot not in str(bootstrap)


def test_snapshot_permission_change_is_detected_even_when_bytes_do_not_change(
    tmp_path: Path,
) -> None:
    source = _blank_pdf(tmp_path)
    opts = core.ImportOptions()
    snapshot = Path(core._initialize_pdf_source_attempt(str(source), opts))

    try:
        with pytest.raises(ValueError, match="snapshot.*protection"):
            with core._verified_pdf_snapshot_consumer(opts, "permission-test"):
                os.chmod(snapshot, stat.S_IREAD | stat.S_IWRITE)
    finally:
        core._dispose_pdf_source_attempt(opts)


def test_snapshot_privacy_claim_is_platform_verified_not_inferred_from_chmod(
    tmp_path: Path,
) -> None:
    source = _blank_pdf(tmp_path)
    opts = core.ImportOptions()
    core._initialize_pdf_source_attempt(str(source), opts)

    try:
        provenance = opts._pdf_source_provenance
        assert provenance["snapshot_read_only"] is True
        assert provenance["snapshot_read_only_verified"] is True
        assert isinstance(provenance["snapshot_protection_method"], str)
        if os.name == "nt":
            assert provenance["snapshot_private"] is False
            assert provenance["snapshot_private_verified"] is False
            assert "chmod" not in provenance["snapshot_privacy_method"]
        else:
            assert provenance["snapshot_private"] is True
            assert provenance["snapshot_private_verified"] is True
    finally:
        core._dispose_pdf_source_attempt(opts)


def test_text_fallback_checks_cancellation_before_each_delivery_attempt() -> None:
    opts = core.ImportOptions(text_mode="text")
    phases: list[str] = []
    opts._import_cancellation_callback = lambda phase: phases.append(phase) or True
    item = {
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "source_item_id": "p1:b0:l0:s0",
        "requested_type": "text",
    }

    def forbidden_delivery(*_args):
        raise AssertionError("delivery ran after cancellation")

    with pytest.raises(core.ImportCancelled):
        core._run_text_item_fallback_ladder(
            item,
            "text",
            {mode: forbidden_delivery for mode in core.TEXT_ITEM_FALLBACK_LADDERS["text"]},
            opts,
        )

    assert phases == ["text fallback: text"]


@pytest.mark.parametrize(
    "target_phase,target_event",
    (
        ("before persistence", "persistence"),
        ("before report", "report"),
        ("before live gate", "live_gate"),
        ("before commit", "commit"),
    ),
)
def test_final_acceptance_boundaries_honor_cancellation_and_roll_back(
    target_phase: str,
    target_event: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _blank_pdf(tmp_path)
    document = _Document()
    existing = document.addObject("Part::Feature", "Existing")
    _configure_import(monkeypatch, document)
    original_gate = core._require_live_import_contract_ready

    def gate(opts):
        document.events.append("live_gate")
        return original_gate(opts)

    monkeypatch.setattr(core, "_require_live_import_contract_ready", gate)
    phases: list[str] = []
    opts = core.ImportOptions(pages=[1], import_text=False)
    opts._import_cancellation_callback = (
        lambda phase: phases.append(phase) or phase == target_phase
    )

    result = core.import_pdf(str(source), opts)

    assert result is False
    assert target_phase in phases
    assert target_event not in document.events
    assert document.committed is False
    assert document.Objects == [existing]


def test_all_long_running_page_loops_have_explicit_cancellation_checkpoints() -> None:
    inner = inspect.getsource(core._import_pdf_page_inner)
    renderer = inspect.getsource(core._render_canonical_text_items)
    fallback = inspect.getsource(core._run_text_item_fallback_ladder)
    raster = inspect.getsource(core._import_page_as_raster)

    for phase in (
        "geometry item",
        "hatch item",
        "embedded image",
        "before page persistence",
    ):
        assert phase in inner
    assert "text item" in renderer
    assert "text fallback" in fallback
    assert "raster render retry" in raster


def test_source_ink_preprocessing_honors_cancellation_before_work() -> None:
    opts = core.ImportOptions()
    phases: list[str] = []
    opts._import_cancellation_callback = (
        lambda phase: phases.append(phase) or phase == "source ink preprocessing"
    )
    page = SimpleNamespace(get_texttrace=lambda: [])

    with pytest.raises(core.ImportCancelled):
        core._bind_page_text_source_ink_evidence(
            page,
            {"blocks": []},
            [],
            opts=opts,
        )

    assert phases == ["source ink preprocessing"]


def test_live_inventory_honors_cancellation_inside_object_loop() -> None:
    opts = core.ImportOptions()
    opts._import_cancellation_callback = (
        lambda phase: phase == "persistence inventory object"
    )

    with pytest.raises(core.ImportCancelled):
        core._build_host_object_inventory(
            [object()],
            shape_evidence_mode="cheap",
            opts=opts,
        )


def test_save_reopen_does_not_convert_cancellation_into_failed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path  # pytest-owned temp location is used by the implementation.

    class Document:
        Name = "CancelPersistence"

        @staticmethod
        def saveCopy(path):  # noqa: N802 - FreeCAD API
            Path(path).write_bytes(b"complete-enough-for-pre-hash-cancel")
            return True

    monkeypatch.setattr(
        core,
        "FreeCAD",
        SimpleNamespace(
            setActiveDocument=lambda *_args: None,
            closeDocument=lambda *_args: None,
        ),
    )
    opts = core.ImportOptions()
    opts._import_cancellation_callback = (
        lambda phase: phase == "persistence archive evidence"
    )

    with pytest.raises(core.ImportCancelled):
        core._save_reopen_host_object_inventory(
            Document(),
            {"counts": {}},
            opts=opts,
        )


def test_shape_archive_stream_hash_propagates_cancellation(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "shape-cancel.FCStd"
    archive.write_bytes(b"shape-archive-hash-cancellation")
    opts = core.ImportOptions()
    opts._import_cancellation_callback = (
        lambda phase: phase == "persistence shape archive hash"
    )

    with pytest.raises(core.ImportCancelled):
        core._read_fcstd_shape_archive_evidence(
            archive,
            [],
            opts=opts,
        )


def test_save_reopen_cleanup_failure_does_not_mask_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_paths: list[Path] = []

    class Document:
        Name = "CancelCleanupPersistence"

        @staticmethod
        def saveCopy(path):  # noqa: N802 - FreeCAD API
            created_paths.append(Path(path))
            Path(path).write_bytes(b"cancel-before-archive-proof")
            return True

    monkeypatch.setattr(
        core,
        "FreeCAD",
        SimpleNamespace(
            setActiveDocument=lambda *_args: None,
            closeDocument=lambda *_args: None,
        ),
    )
    original_remove = core.os.remove
    remove_calls = 0

    def fail_final_remove(path):
        nonlocal remove_calls
        remove_calls += 1
        if remove_calls >= 2:
            raise OSError("forced final cleanup failure")
        return original_remove(path)

    monkeypatch.setattr(core.os, "remove", fail_final_remove)
    opts = core.ImportOptions()
    opts._import_cancellation_callback = (
        lambda phase: phase == "persistence archive evidence"
    )

    with pytest.raises(core.ImportCancelled) as raised:
        core._save_reopen_host_object_inventory(
            Document(),
            {"counts": {}},
            opts=opts,
        )

    assert isinstance(getattr(raised.value, "cleanup_error", None), core.ImportCleanupError)
    monkeypatch.setattr(core.os, "remove", original_remove)
    for path in created_paths:
        if path.exists():
            original_remove(path)


def test_report_publication_cancellation_propagates_and_paths_roll_back(
    tmp_path: Path,
) -> None:
    source = _blank_pdf(tmp_path)
    report = tmp_path / "report.json"
    bootstrap = tmp_path / "parts_bootstrap.json"
    opts = core.ImportOptions(import_text=False)
    opts._atomic_import_active = True
    phases: list[str] = []
    opts._import_cancellation_callback = (
        lambda phase: phases.append(phase) or phase == "report publication"
    )

    with pytest.raises(core.ImportCancelled):
        core.write_import_report(
            pdf_path=str(source),
            output_path=str(report),
            opts=opts,
            pages_imported=1,
            total_pages=1,
        )

    rollback = core._rollback_attempt_paths(opts)
    assert rollback["cleanup_complete"] is True
    assert "report construction" in phases
    assert "report publication" in phases
    assert not report.exists()
    assert not bootstrap.exists()
