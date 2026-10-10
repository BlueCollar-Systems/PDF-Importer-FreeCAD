"""One picture this host cannot place costs that picture, never the page or file.

A display-only composite whose pixels FreeCAD cannot copy into its document
cache (an OSError, as on a 36x24 in sheet imported under a long temp path) used
to roll back the whole run to an empty drawing. Now the picture is left out,
the editable lines under it stay, every other item and page arrives, and the
picture is listed in extra.picture_items_degraded, counted as a warning,
explained in human_summary and kept out of certification. Embedded pictures a
page could not place are listed the same way instead of only a console line.
Fixture data is fictional (job D042).
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import test_clip_fill_degrade_fc as fixture  # puts the importer sources on sys.path

import PDFNonTextComposite  # noqa: E402
import PDFNonTextCompositeProof  # noqa: E402
import PDFStrokeFootprint  # noqa: E402

core = fixture.core
host = fixture.host

CAPSULE = {"area": 4.0, "kind": "fictional round-cap dot"}


class InkPage(fixture.Page):
    """A page whose one short round-cap stroke qualifies for a source composite."""

    def get_svg_image(self, **_kwargs):
        return "<svg/>"


def composite_page(monkeypatch, failing):
    """Wire the real page path to one multiply-blend composite candidate."""
    monkeypatch.setattr(fixture.HostObject, "ViewObject", None, raising=False)
    monkeypatch.setattr(PDFStrokeFootprint, "short_round_stroke",
                        lambda row: CAPSULE if row.get("seqno") == 1 else None)
    monkeypatch.setattr(PDFStrokeFootprint, "unclipped_capsules", lambda *_args: [])
    monkeypatch.setattr(PDFStrokeFootprint, "bind_similarity_strokes", lambda *_args: {1: {
        "capsule": CAPSULE, "clip_bounds": [0, 0, 200, 100], "source_blend_modes": ["Multiply"],
        "source_svg_stroke": {}}})
    monkeypatch.setattr(PDFStrokeFootprint, "native_face", lambda capsule, mapper, part: SimpleNamespace(
        Area=capsule["area"] * scale_of(fixture.options()) ** 2))
    monkeypatch.setattr(PDFNonTextCompositeProof, "multiply_modes", lambda modes: "Multiply" in (modes or []))
    calls = []

    def apply_composites(page, proofs, *, undelivered=None, **kwargs):
        calls.append(sorted(proofs))
        if failing:
            undelivered.append({"page_number": kwargs["page_number"], "source_paint_order": 1,
                                "kind": "nontext_composite", "delivered": "skipped",
                                "reason": "picture not delivered: OSError: [Errno 2] path too long",
                                "editable_geometry_kept": True, "canonical_object": "PDF_Stroke_Ink"})
            return []
        return [{"recipe": {"source_paint_order": 1}, "pixels": {"width": 2, "height": 2}}]

    monkeypatch.setattr(PDFNonTextComposite, "apply_composites", apply_composites)
    return calls


def scale_of(opts):
    return (core.MM_PER_PT if opts.scale_to_mm else 1.0) * opts.user_scale


def dot_row(seqno=1):
    return {"type": "s", "level": 0, "seqno": seqno, "rect": (10, 10, 12, 12), "color": (0.0, 0.0, 0.0),
            "width": 2.0, "items": [("l", core.fitz.Point(10, 10), core.fitz.Point(10.5, 10))]}


def import_ink_page(rows, opts=None):
    opts = opts or fixture.options()
    document = fixture.Document()
    group, _text = core._import_pdf_page_inner(fixture.Pdf(InkPage(rows)), "D042.pdf", 1, opts, document)
    return document, opts, group


def test_a_picture_the_document_cache_refuses_is_left_out_and_the_page_survives(host, monkeypatch):
    calls = composite_page(monkeypatch, failing=True)
    document, opts, group = import_ink_page([dot_row(), fixture.stroke(2, 150)])
    assert calls == [[1]] and group is not None
    assert document.named("PDF_Stroke_Ink")                   # the editable capsule stays
    assert document.named("Batch")                            # and every other item
    block = opts._report_extra["picture_items_degraded"]
    assert (block["total"], block["skipped"], block["pages"]) == (1, 1, [1])
    (entry,) = block["items"]
    assert entry["kind"] == "nontext_composite" and entry["reason"].startswith("picture not delivered")
    # Listed once, with its own reason, not also as an unsupported composite.
    assert opts._report_extra["unsupported_nontext_composites"] == []
    (line,) = [text for text in host["warning"] if "picture" in text]
    assert "left out" in line and "editable lines under it are kept" in line


def test_a_placed_picture_is_not_listed(host, monkeypatch):
    composite_page(monkeypatch, failing=False)
    _document, opts, _group = import_ink_page([dot_row()])
    assert "picture_items_degraded" not in opts._report_extra


def test_embedded_pictures_a_page_could_not_place_are_listed(host, monkeypatch):
    def refuse(*_args):
        raise OSError("could not copy the picture into the document cache")

    monkeypatch.setattr(core, "_import_embedded_images_as_planes", refuse)
    document, opts, group = fixture.import_page([fixture.stroke(0, 150)], fixture.options(ignore_images=False))
    assert group is not None and document.named("Batch")
    (entry,) = opts._report_extra["picture_items_degraded"]["items"]
    assert entry["kind"] == "embedded_images" and "could not copy" in entry["reason"]
    assert any("Image import failed" in text for text in host["warning"])


def test_the_report_counts_explains_and_does_not_certify_a_missing_picture(host, monkeypatch, tmp_path):
    composite_page(monkeypatch, failing=True)
    _document, opts, _group = import_ink_page([dot_row()])
    path = tmp_path / "D042_import_report.json"
    core.write_import_report(pdf_path=str(tmp_path / "D042.pdf"), output_path=str(path), opts=opts,
                             pages_imported=1, total_pages=1, elapsed_ms=1.0)
    report = json.loads(path.read_text(encoding="utf-8"))
    assert report["result"]["warnings"] >= 1
    assert report["fallback"] == {"used": True, "reason": "picture_items_degraded"}
    assert "1 picture on page 1 could not be placed and was left out" in report["extra"]["human_summary"]
    assert report["extra"]["import_contract_ready"]["ready"] is False
    assert report["extra"]["import_contract_ready"]["checks"]["picture_delivery"] is False
    assert core._geometry_degraded_pages(opts) == [1]          # the session never certifies the page
