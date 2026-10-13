"""Exercise real style and failed-item cleanup; synthetic host, no OCC claim."""
import copy
import json
from types import SimpleNamespace

import pytest

import test_clip_fill_degrade_fc as fixture
import test_source_repeated_endpoint_fill_fc as closed
import test_transaction_failure_page_report_fc as transaction

core = fixture.core
REAL_STYLE = core._apply_style


class View:
    def __init__(self, refusal=None):
        self.refusal = refusal
        self._display = "Flat Lines"
        self.DrawStyle = "Solid"

    @property
    def DisplayMode(self):
        return self._display

    @DisplayMode.setter
    def DisplayMode(self, value):
        if value == "Shaded" and self.refusal == "throws":
            raise RuntimeError("neutral view provider refused Shaded")
        if value != "Shaded" or self.refusal != "silent_noop":
            self._display = value


def real_style_host(monkeypatch, refusal=None):
    closed.page_host(monkeypatch)
    add = fixture.Document.addObject

    def add_view(document, kind, name):
        obj = add(document, kind, name)
        obj.ViewObject = View(refusal)
        return obj

    monkeypatch.setattr(fixture.Document, "addObject", add_view)
    monkeypatch.setattr(core, "_apply_style", REAL_STYLE)


@pytest.mark.parametrize("kind", ["f", "fs"])
def test_actual_style_shades_source_fill_only_boundary_on_import_and_restore(monkeypatch, kind):
    real_style_host(monkeypatch)
    opts = fixture.options(hatch_to_faces=True, compound_batch_size=0,
                           assign_linewidth=True, map_dashes=True)
    doc, opts, _ = fixture.import_page([closed.triangle(kind=kind)], opts)
    face, = doc.named("Face")
    assert face.ViewObject.DisplayMode == "Shaded"
    assert opts._report_extra.get("geometry_items_degraded") is None
    metadata = {name: copy.deepcopy(getattr(face, name)) for name in
                ("PDFStrokeRGB", "PDFFillRGB", "PDFLineWidthPt", "PDFDashPattern",
                 "PDFSourceFillBoundaryJSON")}
    face.ViewObject.DisplayMode = "Flat Lines"
    REAL_STYLE(face, None, (.2, .3, .4), None, None, opts, persist_metadata=False)
    assert face.ViewObject.DisplayMode == "Shaded"
    assert {name: getattr(face, name) for name in metadata} == metadata
    if kind == "fs":
        wire, = doc.named("Wire")
        assert wire.ViewObject.DrawStyle == "Dashed"
        assert wire.ViewObject.LineColor == (.8, .55, .6)  # Original 50% source stroke over white.
        assert json.loads(wire.PDFSourceFillBoundaryJSON)["policy"] == "retain_open_pdf_stroke"


@pytest.mark.parametrize("refusal", ["silent_noop", "throws"])
def test_actual_import_refused_shaded_display_removes_face_before_fallback(monkeypatch, refusal):
    real_style_host(monkeypatch, refusal)
    doc, opts, _ = fixture.import_page([closed.triangle()],
        fixture.options(hatch_to_faces=True, compound_batch_size=0))
    assert not doc.named("Face") and len(doc.named("StepDownOutline")) == 1
    degraded = opts._report_extra["geometry_items_degraded"]
    assert degraded["total"] == 1 and "display" in degraded["items"][0]["reason"]


@pytest.mark.parametrize("refusal", ["silent_noop", "throws"])
@pytest.mark.parametrize("policy", ["close_connected_pdf_fill", "retain_exact_closed_pdf_fill"])
def test_actual_restore_never_silently_certifies_refused_fill_display(refusal, policy):
    obj = SimpleNamespace(ViewObject=View(refusal),
                          PDFSourceFillBoundaryJSON=json.dumps({"policy": policy}))
    with pytest.raises(core.DrawingGeometryFailure, match="display"):
        REAL_STYLE(obj, None, (.2, .3, .4), None, None, fixture.options(), persist_metadata=False)


def refuse_original_stroke_object(monkeypatch):
    original = core._make_shape_obj
    calls = []

    def build(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 2:
            raise core.DrawingGeometryFailure("neutral original stroke object refused after fill")
        return original(*args, **kwargs)

    monkeypatch.setattr(core, "_make_shape_obj", build)
    return calls


def test_actual_item_cleanup_removes_partial_fill_then_delivers_fallback_and_next_item(monkeypatch):
    closed.page_host(monkeypatch)
    refuse_original_stroke_object(monkeypatch)
    bad = closed.triangle(kind="fs")
    good = closed.source([(60., 10.), (85., 10.), (70., 28.), (60., 10.)], seq=74)
    doc, opts, group = fixture.import_page([bad, good],
        fixture.options(hatch_to_faces=True, compound_batch_size=0))
    face, = doc.named("Face")
    assert json.loads(face.PDFSourceFillBoundaryJSON)["source_paint_order"] == 74
    assert len(doc.named("StepDownOutline")) == 1
    assert all(child in doc.Objects for child in group.Group)
    assert opts._report_extra["geometry_items_degraded"]["total"] == 1


@pytest.mark.parametrize("refusal", ["silent_noop", "throws"])
@pytest.mark.parametrize("persistent", [False, True])
def test_actual_transaction_escalates_uncertain_item_cleanup_and_preserves_existing(
        monkeypatch, tmp_path, refusal, persistent):
    closed.page_host(monkeypatch)
    pdf, doc, existing, report, opts = transaction.setup_import(monkeypatch, tmp_path)
    opts.pages = [1]
    monkeypatch.setattr(existing.__class__, "isDerivedFrom",
                        lambda obj, kind: obj.TypeId == kind, raising=False)
    opts.hatch_to_faces = True
    opts.compound_batch_size = 0
    opts.raster_fallback = False
    opts.ignore_images = True
    refuse_original_stroke_object(monkeypatch)
    remove = doc.removeObject
    attempts = []

    def refuse_remove(name):
        if name.startswith("Face") and (persistent or not attempts):
            attempts.append(name)
            if refusal == "throws":
                raise RuntimeError("neutral native object removal refused")
            return None
        return remove(name)

    monkeypatch.setattr(doc, "removeObject", refuse_remove)
    page_inner = core._import_pdf_page_inner
    pages = []

    def synthetic_page(_pdf, path, page, options, document):
        pages.append(page)
        return page_inner(fixture.Pdf(fixture.Page([closed.triangle(kind="fs")])),
                          path, page, options, document)

    monkeypatch.setattr(core, "_import_pdf_page_inner", synthetic_page)
    with pytest.raises(RuntimeError, match="cleanup|rollback") as caught:
        core.import_pdf(str(pdf), opts)
    assert pages == [1] and doc.aborted and not doc.committed
    assert existing in doc.Objects and not any(o.Name.startswith("StepDownOutline") for o in doc.Objects)
    extra = json.loads(report.read_text(encoding="utf-8"))["extra"]
    assert extra["result_status"] == "failed" and extra["import_contract_ready"]["ready"] is False
    assert extra["page_failure"]["last_evaluated_page"] == 1
    assert extra["page_failure"]["rollback_cleanup_complete"] is (not persistent)
    assert extra["page_failure"]["type"] == "DrawingCleanupFailure"
    if persistent:
        assert caught.value.__cause__.__class__.__name__ == "DrawingCleanupFailure"
        assert any(o.Name.startswith("Face") for o in doc.Objects)
    else:
        assert doc.Objects == [existing] and caught.value.__class__.__name__ == "DrawingCleanupFailure"


def test_item_cleanup_refuses_name_rebound_to_an_unowned_original():
    doc = fixture.Document()
    owned = doc.addObject("Part::Feature", "Face")
    replacement = fixture.HostObject(owned.Name, "Part::Feature")
    doc.Objects = [replacement]
    with pytest.raises(RuntimeError, match="cleanup"):
        core._discard_host_objects(doc, [owned])
    assert doc.Objects == [replacement] and not doc.removed


def test_item_cleanup_requires_successful_absence_readback(monkeypatch):
    doc = fixture.Document()
    owned = doc.addObject("Part::Feature", "Face")
    original_get = doc.getObject

    def get(name):
        if doc.removed:
            raise RuntimeError("neutral absence query refused")
        return original_get(name)

    monkeypatch.setattr(doc, "getObject", get)
    with pytest.raises(RuntimeError, match="cleanup"):
        core._discard_host_objects(doc, [owned])
    assert not doc.Objects  # Absence is observed by the test, not certified by the failed API.


@pytest.mark.parametrize("refusal", ["silent_noop", "throws"])
def test_item_cleanup_checks_parent_membership_and_still_attempts_owned_document_removal(refusal):
    doc = fixture.Document()
    owned = doc.addObject("Part::Feature", "Face")
    parent = fixture.HostObject("Owned_Page", "App::DocumentObjectGroup")
    parent.addObject(owned)
    def remove(_obj):
        if refusal == "throws":
            raise RuntimeError("neutral parent object removal refused")
    parent.removeObject = remove
    with pytest.raises(RuntimeError, match="cleanup"):
        core._discard_host_objects(doc, [owned], parent)
    assert not doc.Objects and parent.Group == [owned]
