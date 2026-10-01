"""Zero-ink controls retain source semantics without fictitious native objects."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pymupdf as fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "PDFVectorImporter", ROOT / "PDFVectorImporter" / "src"):
    sys.path.insert(0, str(path))
import PDFEmbeddedFonts as fonts  # noqa: E402
import PDFImporterCore as core  # noqa: E402
import PDFSvgTextRenderer as svg_renderer  # noqa: E402


def control_pdf(path):
    with fitz.open() as pdf:
        page = pdf.new_page(width=160, height=120)
        page.insert_text((30, 60), "X", fontname="cour", fontsize=11)
        pdf.update_stream(page.get_contents()[-1], b"BT /cour 11 Tf 30 60 Td <0a> Tj ET")
        pdf.save(path)


def render(path, monkeypatch, mode="text"):
    group = SimpleNamespace(PropertiesList=[])
    group.addProperty = lambda kind, name, section: group.PropertiesList.append(name)
    opts = core.ImportOptions(text_mode=mode)
    monkeypatch.setattr(core, "_to_fc", lambda p, *a: SimpleNamespace(x=p[0], y=p[1]))
    with fitz.open(path) as pdf:
        result = core._render_canonical_text_items(
            pdf_doc=pdf, page=pdf[0], pdf_path=str(path), page_num=1,
            page_h=120.0, page_w=160.0, scale=1.0,
            fc_doc=SimpleNamespace(Objects=[]), parent_group=group,
            opts=opts, pdf_sha256=core._pdf_file_sha256(str(path)))
    return result, opts, group


def mixed_control_pdf(path):
    with fitz.open() as pdf:
        page = pdf.new_page(width=160, height=120)
        page.insert_text((30, 20), "A", fontname="cour", fontsize=11)
        page.insert_text((30, 50), "X", fontname="cour", fontsize=11)
        pdf.update_stream(page.get_contents()[-1], b"BT /cour 11 Tf 30 70 Td <0a> Tj ET")
        page.insert_text((30, 80), "B", fontname="cour", fontsize=11)
        pdf.save(path)


def capture_svg_manifest(path, monkeypatch, mode):
    captured = {}

    def deliver(item, attempted, state, **kwargs):
        captured["manifest"] = kwargs["render_cache"]["source_item_manifest"]
        return {"source_item_id": item["source_item_id"], "outcome": "verified",
                "final_type": attempted, "created_entity_ids": ["test_" + item["source_item_id"]]}

    monkeypatch.setattr(core, "_deliver_text_item_svg", deliver)
    monkeypatch.setattr(core, "_run_text_item_fallback_ladder",
                        lambda item, requested, deliverers, opts: deliverers[requested](item, requested, None))
    result, opts, group = render(path, monkeypatch, mode)
    return captured["manifest"], result, opts, group


def assign_manifest(manifest, digest):
    # Independent bounded ink boxes exercise the real global assignment and
    # strict manifest validator; this test does not claim native shape delivery.
    shapes = []
    for index, entry in enumerate(manifest):
        x0, y0, x1, y1 = entry["bbox"]
        bounds = SimpleNamespace(XMin=x0 + 0.1, YMin=y0 + 0.1,
                                 XMax=x1 - 0.1, YMax=y1 - 0.1)
        shapes.append((index, "test_glyph", SimpleNamespace(BoundBox=bounds)))
    return svg_renderer._build_global_placement_assignments(
        shapes, manifest, page_num=1, pdf_sha256=digest,
        page_rotation_matrix=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
        vb_min_x=0, vb_min_y=0, vb_w=160, vb_h=120,
        page_w=160, page_h=120, x_unit_to_mm=1, y_unit_to_mm=1, flip_y=False)


@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
def test_verified_middle_control_is_not_a_visible_svg_assignment_item(tmp_path, monkeypatch, mode):
    path = tmp_path / "mixed.pdf"
    mixed_control_pdf(path)
    manifest, result, opts, group = capture_svg_manifest(path, monkeypatch, mode)
    persisted = json.loads(group.PDFSourceZeroInkControlsJSON)
    assert len(persisted) == 1 and persisted[0]["source_text"] == "\n"
    control_id = persisted[0]["source_item_id"]
    assert result["source_item_count"] == opts._report_extra["text_source_spans"] == 3
    assert result["source_item_ids"][1] == control_id
    assert [entry["source_item_id"] for entry in manifest] == [
        result["source_item_ids"][0], result["source_item_ids"][2]]
    assert [entry["text"] for entry in manifest] == ["A", "B"]
    assert [entry["source_order"] for entry in manifest] == [0, 1]
    assert persisted == opts._report_extra["source_zero_ink_controls"]
    assert opts._verified_source_zero_ink_controls[control_id] == persisted[0]
    assert persisted[0]["created_entity_ids"] == [] and persisted[0]["final_type"] is None
    assignments, _, unmatched = assign_manifest(manifest, core._pdf_file_sha256(str(path)))
    assert unmatched == []
    assert assignments == {entry["source_item_id"]: [i] for i, entry in enumerate(manifest)}
    assert result["host_entity_count"] == 2


@pytest.mark.parametrize("mode", ["glyphs", "geometry"])
def test_unproved_middle_control_remains_in_svg_manifest_and_fails_strict_validation(tmp_path, monkeypatch, mode):
    path = tmp_path / "unproved.pdf"
    mixed_control_pdf(path)
    monkeypatch.setattr(fonts, "original_control_omission_records", lambda *args: {})
    manifest, result, opts, group = capture_svg_manifest(path, monkeypatch, mode)
    assert len(manifest) == result["source_item_count"] == 3
    assert [entry["text"] for entry in manifest] == ["A", "\n", "B"]
    assert [entry["source_order"] for entry in manifest] == [0, 1, 2]
    assert "source_zero_ink_controls" not in opts._report_extra
    assert "PDFSourceZeroInkControlsJSON" not in group.PropertiesList
    with pytest.raises(ValueError, match="SVG source item manifest entry is invalid"):
        assign_manifest(manifest, core._pdf_file_sha256(str(path)))


@pytest.mark.parametrize("mode", ["text", "labels", "3d_text", "glyphs", "geometry", "raster"])
def test_original_control_is_accounted_for_with_no_created_or_delivered_type(tmp_path, monkeypatch, mode):
    path = tmp_path / "control.pdf"
    control_pdf(path)
    monkeypatch.setattr(core, "_run_text_item_fallback_ladder",
                        lambda *a, **k: pytest.fail("proven invisible source needs no visible substitute"))
    result, opts, group = render(path, monkeypatch, mode)
    assert result["source_item_count"] == 1
    assert result["entity_type"] == "none"
    assert result["count"] == result["host_entity_count"] == 0
    assert result["attempts"] == opts.text_delivery_attempts == []
    assert opts._report_extra["text_source_spans"] == 1
    persisted = json.loads(group.PDFSourceZeroInkControlsJSON)
    assert persisted == opts._report_extra["source_zero_ink_controls"]
    receipt = persisted[0]
    assert receipt["source_item_id"] == result["source_item_ids"][0]
    assert receipt["source_text"] == "\n" and receipt["requested_type"] == mode
    assert receipt["final_type"] is None and receipt["created_entity_ids"] == []
    assert receipt["no_visible_ink"] is True
    proof = receipt["original_control_zero_ink"]
    assert proof["original_glyph_bounds"] is None and proof["glyph_bounds"] is None
    assert proof["characters"][0]["codepoint"] == 10
    assert proof["characters"][0]["trace_codepoint"] == 65533
    assert proof["characters"][0]["advance_width"] > 0
    assert receipt["pdf_sha256"] == core._pdf_file_sha256(str(path))


def test_unproven_control_reaches_normal_delivery_instead_of_whitespace_drop(tmp_path, monkeypatch):
    path = tmp_path / "control.pdf"
    control_pdf(path)
    monkeypatch.setattr(fonts, "original_control_omission_records", lambda *a: {})
    class ReachedNormalDelivery(Exception):
        pass
    def deliver(item, *args):
        assert item["text"] == "\n"
        raise ReachedNormalDelivery
    monkeypatch.setattr(core, "_run_text_item_fallback_ladder", deliver)
    with pytest.raises(ReachedNormalDelivery):
        render(path, monkeypatch)


def test_changed_source_is_rejected_after_positive_ink_proof(tmp_path, monkeypatch):
    path = tmp_path / "control.pdf"
    control_pdf(path)
    original = fonts.original_control_omission_records
    def changed(page, items):
        records = original(page, items)
        assert records
        path.write_bytes(path.read_bytes() + b"\n% source changed\n")
        return records
    monkeypatch.setattr(fonts, "original_control_omission_records", changed)
    with pytest.raises(ValueError, match="original PDF changed"):
        render(path, monkeypatch)


@pytest.mark.parametrize("proven", [False, True])
def test_persisted_report_distinguishes_proven_empty_control_from_undelivered_control(tmp_path, monkeypatch, proven):
    path = tmp_path / "control.pdf"
    control_pdf(path)
    if not proven:
        monkeypatch.setattr(fonts, "original_control_omission_records", lambda *a: {})
        monkeypatch.setattr(core, "_run_text_item_fallback_ladder", lambda item, *a: {
            "source_item_id": item["source_item_id"], "requested_type": "text",
            "outcome": "degraded", "final_type": None, "proof_class": "unproven_failure",
        })
    result, opts, group = render(path, monkeypatch)
    opts._report_extra["actual_text_entity_types"] = result
    report_path = tmp_path / "report.json"
    core.write_import_report(pdf_path=str(path), output_path=str(report_path), opts=opts,
                             pages_imported=1, total_pages=1, text_count=0)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    extra = report["extra"]
    assert extra["text_representation_delivery"]["verified"] is proven
    assert extra["text_representation_delivery"]["source_spans"] == 1
    assert extra["actual_text_entity_types"]["source_item_count"] == 1
    assert extra["actual_text_entity_types"]["count"] == 0
    missing_text = "source_text_seen_but_no_text_entities_created"
    assert (missing_text in extra["diagnostics"]["signals"]) is not proven
    missing_action = "Treat missing delivered text entities as a failed import;"
    assert any(action.startswith(missing_action)
               for action in extra["diagnostics"]["recommended_actions"]) is not proven
    if proven:
        assert extra["source_zero_ink_controls"] == json.loads(group.PDFSourceZeroInkControlsJSON)
    else:
        assert "source_zero_ink_controls" not in extra
        assert extra["text_items_degraded"]["total"] == 1


@pytest.mark.parametrize("corruption", ["duplicate", "missing", "unknown", "changed_proof",
                                        "no_verified_copy", "source_count", "changed_source"])
def test_incomplete_or_changed_control_coverage_keeps_missing_text_warning(tmp_path, monkeypatch, corruption):
    path = tmp_path / "control.pdf"
    control_pdf(path)
    result, opts, _ = render(path, monkeypatch)
    opts._report_extra["actual_text_entity_types"] = result
    records = opts._report_extra["source_zero_ink_controls"]
    if corruption == "duplicate":
        records.append(dict(records[0]))
    elif corruption == "missing":
        records.clear()
    elif corruption == "unknown":
        records[0]["source_item_id"] = "unrequested-control"
    elif corruption == "changed_proof":
        records[0]["original_control_zero_ink"]["glyph_bounds"] = [0, 0, 1, 1]
    elif corruption == "no_verified_copy":
        del opts._verified_source_zero_ink_controls
    elif corruption == "source_count":
        result["source_item_count"] += 1
    elif corruption == "changed_source":
        path.write_bytes(path.read_bytes() + b"\n% changed after delivery\n")
    report_path = tmp_path / "report.json"
    core.write_import_report(pdf_path=str(path), output_path=str(report_path), opts=opts,
                             pages_imported=1, total_pages=1, text_count=0)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert "source_text_seen_but_no_text_entities_created" in report["extra"]["diagnostics"]["signals"]
    assert report["extra"]["actual_text_entity_types"]["count"] == 0
