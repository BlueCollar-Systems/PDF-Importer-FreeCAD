from __future__ import annotations

import copy
import hashlib
import io
import inspect
import json
import math
import os
import sys
from pathlib import Path

import pytest
from PIL import Image


REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402


class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)


class _Rotation:
    def __init__(self, _axis=None, angle=0.0):
        self.angle = float(angle)


class _Placement:
    def __init__(self, base=None, rotation=None):
        self.Base = base or _Vector()
        self.Rotation = rotation or _Rotation()


class _HostObject:
    def __init__(self, document, name, type_id):
        self.Document = document
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.Placement = _Placement()
        self.PropertiesList = []
        self._property_types = {}

    def addProperty(self, kind, name, _group):  # noqa: N802 - FreeCAD API
        if name not in self.PropertiesList:
            self.PropertiesList.append(name)
        self._property_types[name] = kind

    def getTypeIdOfProperty(self, name):  # noqa: N802 - FreeCAD API
        return self._property_types.get(name, "")


class _Document:
    def __init__(self):
        self.Objects = []

    def addObject(self, kind, name):  # noqa: N802 - FreeCAD API
        obj = _HostObject(self, "%s_%d" % (name, len(self.Objects)), kind)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):  # noqa: N802 - FreeCAD API
        return next((obj for obj in self.Objects if obj.Name == name), None)

    def removeObject(self, name):  # noqa: N802 - FreeCAD API
        self.Objects = [obj for obj in self.Objects if obj.Name != name]

    def recompute(self, *_args):
        return None


class _Group:
    def __init__(self):
        self.Group = []

    def addObject(self, obj):  # noqa: N802 - FreeCAD API
        if obj not in self.Group:
            self.Group.append(obj)

    def removeObject(self, obj):  # noqa: N802 - FreeCAD API
        if obj in self.Group:
            self.Group.remove(obj)


def _install_image_host(monkeypatch):
    document = _Document()
    group = _Group()
    monkeypatch.setattr(core, "Vector", _Vector)
    monkeypatch.setattr(core, "Rotation", _Rotation)
    monkeypatch.setattr(core, "Placement", _Placement)
    return document, group


def _make_text_pdf(
    path: Path,
    *,
    rotated: bool = False,
    intersecting_neighbor: bool = False,
) -> None:
    document = core.fitz.open()
    page = document.new_page(width=300, height=200)
    if rotated:
        page.insert_text((250, 150), "TARGET", fontsize=18, rotate=90, fontname="helv")
        page.draw_line((210, 130), (275, 130), color=(1, 0, 0), width=2)
        page.insert_text((20, 30), "NEIGHBOR", fontsize=12, fontname="helv")
    else:
        page.insert_text((40, 70), "TARGET", fontsize=24, fontname="helv")
        neighbor_x = 110 if intersecting_neighbor else 170
        page.insert_text((neighbor_x, 70), "NEIGHBOR", fontsize=16, fontname="helv")
        page.draw_line((20, 60), (240, 60), color=(1, 0, 0), width=2)
    document.save(str(path))
    document.close()


def _bound_text_context(path: Path, requested: str = "raster"):
    opts = core.ImportOptions(
        import_text=True,
        text_mode=requested,
        scale_to_mm=False,
        user_scale=1.0,
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(path), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    document = core._open_pdf_source_attempt(opts)
    page = document.load_page(0)
    raw = page.get_text("rawdict")
    items = list(core._iter_text_source_items(raw, 1, opts._pdf_sha256, requested))
    items = core._bind_page_text_source_ink_evidence(page, raw, items, opts=opts)
    target = next(item for item in items if item["text"] == "TARGET")
    return opts, document, page, target


def _deliver_direct_raster(item, opts, page, document, group):
    return core._run_text_item_fallback_ladder(
        item,
        "raster",
        {
            "raster": lambda source_item, attempted, state: (
                core._deliver_text_item_raster(
                    source_item,
                    attempted,
                    state,
                    page=page,
                    page_h=float(page.rect.height),
                    scale=1.0,
                    fc_doc=document,
                    parent_group=group,
                )
            )
        },
        opts,
    )


@pytest.mark.parametrize("rotated", [False, True])
def test_text_raster_is_exact_source_delta_with_transparent_unchanged_pixels(
    monkeypatch,
    tmp_path,
    rotated,
):
    source = tmp_path / ("rotated.pdf" if rotated else "crossing.pdf")
    _make_text_pdf(source, rotated=rotated)
    opts, source_document, source_page, item = _bound_text_context(source)
    host_document, group = _install_image_host(monkeypatch)
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path / "assets")
    try:
        result = _deliver_direct_raster(
            item,
            opts,
            source_page,
            host_document,
            group,
        )
    finally:
        source_document.close()
        core._dispose_pdf_source_attempt(opts)

    evidence = result["evidence"]
    host = host_document.getObject(result["created_entity_ids"][0])
    asset = Path(host.PDFRasterFile)
    assert evidence["isolation_method"] == "exact_source_original_minus_target_redacted"
    assert evidence["unchanged_pixels_transparent"] is True
    assert evidence["unchanged_pixel_count"] > 0
    assert evidence["changed_pixel_count"] > 0
    assert evidence["other_text_intersection_count"] == 0
    assert evidence["source_quad"]
    assert evidence["source_line_direction"] == pytest.approx(item["line_direction"])
    assert evidence["source_rotation_deg"] == pytest.approx(item["rotation_deg"])
    assert evidence["transparent_alpha_preserved"] is True
    assert host.Placement.Base.z == pytest.approx(0.0)
    assert asset.stem.endswith(evidence["source_asset_sha256"])
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == evidence["source_asset_sha256"]


def test_text_raster_rejects_intersecting_neighbor_before_asset_or_host(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "neighbor-overlap.pdf"
    _make_text_pdf(source, intersecting_neighbor=True)
    opts, source_document, source_page, item = _bound_text_context(source)
    host_document, group = _install_image_host(monkeypatch)
    asset_dir = tmp_path / "assets"
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: asset_dir)
    try:
        with pytest.raises(core.TextRepresentationFailure) as raised:
            _deliver_direct_raster(item, opts, source_page, host_document, group)
    finally:
        source_document.close()
        core._dispose_pdf_source_attempt(opts)

    assert raised.value.attempt["reason"] == "raster_item_isolation_unproven"
    assert raised.value.attempt["cleanup_complete"] is True
    assert host_document.Objects == []
    assert not asset_dir.exists() or list(asset_dir.iterdir()) == []


def test_text_raster_rejects_wrong_document_page_before_asset_or_host(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "source.pdf"
    wrong = tmp_path / "wrong.pdf"
    _make_text_pdf(source)
    _make_text_pdf(wrong, rotated=True)
    opts, source_document, _source_page, item = _bound_text_context(source)
    wrong_document = core.fitz.open(str(wrong))
    host_document, group = _install_image_host(monkeypatch)
    asset_dir = tmp_path / "assets"
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: asset_dir)
    try:
        with pytest.raises(core.TextRepresentationFailure) as raised:
            _deliver_direct_raster(item, opts, wrong_document[0], host_document, group)
    finally:
        source_document.close()
        wrong_document.close()
        core._dispose_pdf_source_attempt(opts)

    assert raised.value.attempt["reason"] == "raster_source_page_identity_mismatch"
    assert host_document.Objects == []
    assert not asset_dir.exists() or list(asset_dir.iterdir()) == []


def test_direct_full_page_raster_is_exact_dpi_content_addressed_and_z_zero(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "page.pdf"
    _make_text_pdf(source)
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=False,
        text_mode="none",
        scale_to_mm=False,
        user_scale=1.0,
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    source_document = core._open_pdf_source_attempt(opts)
    source_page = source_document[0]
    host_document, group = _install_image_host(monkeypatch)
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path / "assets")
    try:
        result = core._import_page_as_raster(
            source_document,
            source_page,
            1,
            float(source_page.rect.height),
            opts,
            1.0,
            group,
            host_document,
        )
    finally:
        source_document.close()
        core._dispose_pdf_source_attempt(opts)

    evidence = result["evidence"]
    host = host_document.getObject(result["created_entity_ids"][0])
    asset = Path(host.PDFRasterFile)
    assert evidence["requested_dpi"] == 144
    assert evidence["delivered_dpi"] == 144
    assert evidence["dpi_degraded"] is False
    assert evidence["source_page_observation_verified"] is True
    assert evidence["direct_requested_raster"] is True
    assert evidence["raster_content_variant"] == "full_page_original"
    assert host.PDFRasterContentVariant == "full_page_original"
    assert (
        host.getTypeIdOfProperty("PDFRasterContentVariant")
        == "App::PropertyString"
    )
    variant_snapshot = core._host_page_text_suppression_snapshot(host)
    assert variant_snapshot == {
        "raster_content_variant": "full_page_original",
        "raster_content_variant_property_verified": True,
    }
    assert host.Placement.Base.z == pytest.approx(0.0)
    assert asset.stem.endswith(evidence["source_asset_sha256"])
    assert hashlib.sha256(asset.read_bytes()).hexdigest() == evidence["source_asset_sha256"]


def test_explicit_raster_dpi_never_silently_caps_or_retries(monkeypatch, tmp_path):
    source = tmp_path / "large.pdf"
    document = core.fitz.open()
    document.new_page(width=2000, height=2000)
    document.save(str(source))
    document.close()
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=False,
        raster_dpi=600,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    source_document = core._open_pdf_source_attempt(opts)
    host_document, group = _install_image_host(monkeypatch)
    monkeypatch.setenv("BC_FC_RASTER_PIXEL_BUDGET", "10000000")
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path / "assets")
    try:
        with pytest.raises(RuntimeError, match="explicit raster DPI"):
            core._import_page_as_raster(
                source_document,
                source_document[0],
                1,
                2000.0,
                opts,
                1.0,
                group,
                host_document,
            )
    finally:
        source_document.close()
        core._dispose_pdf_source_attempt(opts)

    assert host_document.Objects == []


def test_v2_page_obligation_can_authorize_complete_page_raster_ladder(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "page-obligation.pdf"
    document = core.fitz.open()
    document.new_page(width=200, height=100)
    document.save(str(source))
    document.close()
    opts = core.ImportOptions(
        import_mode="vector",
        import_text=True,
        text_mode="labels",
        scale_to_mm=False,
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    core._register_text_delivery_obligations(opts, ["p1:page"])
    authorization = core._authorize_page_raster_fallback(
        opts,
        page_number=1,
        requested_type="labels",
    )
    source_document = core._open_pdf_source_attempt(opts)
    host_document, group = _install_image_host(monkeypatch)
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path / "assets")
    try:
        result = core._import_page_as_raster(
            source_document,
            source_document[0],
            1,
            100.0,
            opts,
            1.0,
            group,
            host_document,
        )
        info = core._record_no_source_text_page_fallback(
            opts,
            page_num=1,
            pdf_sha256=opts._pdf_sha256,
            raw_tdict={"blocks": []},
            raster_result=result,
        )
    finally:
        source_document.close()
        core._dispose_pdf_source_attempt(opts)

    assert result["evidence"]["direct_requested_raster"] is False
    assert result["evidence"]["page_fallback_authorized"] is True
    assert result["evidence"]["attempted_types"] == list(
        core.TEXT_ITEM_FALLBACK_LADDERS["labels"]
    )
    assert result["evidence"]["proof_chain"] == authorization["proof_chain"]
    assert info["entity_type"] == "raster"
    assert [attempt["attempted_type"] for attempt in opts.text_delivery_attempts] == list(
        core.TEXT_ITEM_FALLBACK_LADDERS["labels"]
    )


def test_content_addressed_pixmap_rejects_mutation_and_rollback_keeps_shared_asset(
    monkeypatch,
    tmp_path,
):
    class Pixmap:
        def __init__(self, payload):
            self.payload = payload

        def save(self, path):
            Path(path).write_bytes(self.payload)

    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path)
    accepted = core.ImportOptions()
    path, evidence = core._persist_content_addressed_pixmap(
        Pixmap(b"immutable-png"),
        accepted,
        asset_kind="test",
    )
    core._accept_attempt_paths(accepted)
    assert path.name == "test_%s.png" % evidence["source_asset_sha256"]

    os.chmod(path, 0o600)
    path.write_bytes(b"mutated")
    second = core.ImportOptions()
    with pytest.raises(core.ImportLifecycleError, match="corrupt"):
        core._persist_content_addressed_pixmap(
            Pixmap(b"immutable-png"),
            second,
            asset_kind="test",
        )
    assert path.read_bytes() == b"mutated"

    fresh = core.ImportOptions()
    fresh_path, _ = core._persist_content_addressed_pixmap(
        Pixmap(b"rollback-png"),
        fresh,
        asset_kind="test",
    )
    assert fresh_path.is_file()
    rollback = core._rollback_attempt_paths(fresh)
    assert rollback["cleanup_complete"] is True
    assert fresh_path.read_bytes() == b"rollback-png"


def test_raster_ladder_context_must_be_executor_owned_exact_prefix(tmp_path):
    source = tmp_path / "ladder.pdf"
    _make_text_pdf(source)
    opts, source_document, _source_page, item = _bound_text_context(source, "labels")
    try:
        forged = copy.deepcopy(item)
        forged["_raster_fallback_context"] = {
            "requested_type": "labels",
            "attempted_types": ["raster"],
            "proof_chain": [],
        }
        with pytest.raises(ValueError, match="ladder prefix"):
            core._validated_raster_ladder_context(forged, "raster")
    finally:
        source_document.close()
        core._dispose_pdf_source_attempt(opts)


@pytest.mark.parametrize("text_mode", ["text", "labels", "3d_text", "glyphs", "geometry"])
def test_structural_text_preserves_explicit_raster_with_suppressed_background(text_mode):
    opts = core.ImportOptions(import_mode="raster", import_text=True, text_mode=text_mode)
    assert core._resolve_raster_text_contract_mode("raster", 1, opts) == (
        "raster",
        True,
    )


def test_sparse_textless_auto_page_is_not_heuristically_page_rasterized():
    assert core._auto_sparse_page_mode(0, 0, 1) == "hybrid"
    assert core._auto_sparse_page_mode(4, 0, 0) == "vector"


def test_no_canonical_text_items_mean_zero_text_obligations():
    opts = core.ImportOptions(import_text=True, text_mode="3d_text")
    opts.text_delivery_obligation_source_item_ids = []
    delivery = core._validate_freecad_text_representation_delivery(opts, [])
    assert delivery["required"] is False
    assert delivery["verified"] is True
    assert delivery["invalid_reasons"] == []


def _transparent_png_bytes() -> bytes:
    image = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    for x_value in range(5, 35):
        for y_value in range(3, 17):
            image.putpixel((x_value, y_value), (0, 100, 255, 128))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_image_only_page_reuses_one_exact_rotated_alpha_occurrence(
    monkeypatch,
    tmp_path,
):
    source = tmp_path / "image-only.pdf"
    document = core.fitz.open()
    page = document.new_page(width=300, height=200)
    page.insert_image(
        core.fitz.Rect(50, 30, 150, 100),
        stream=_transparent_png_bytes(),
        rotate=90,
    )
    document.save(str(source))
    document.close()
    opts = core.ImportOptions(
        import_mode="auto",
        import_text=True,
        text_mode="labels",
        scale_to_mm=False,
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    assert opts.page_visual_source_observations["p1:page"]["classification"] == "image_only"
    host_document, group = _install_image_host(monkeypatch)
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path / "assets")
    try:
        result = core._import_embedded_image_occurrences(
            opts=opts,
            page_number=1,
            page_h=200.0,
            scale=1.0,
            fc_doc=host_document,
            image_group=group,
        )
    finally:
        core._dispose_pdf_source_attempt(opts)

    assert result["count"] == 1
    assert len(group.Group) == 1
    occurrence = result["occurrences"][0]
    transform = occurrence["source_transform"]
    assert transform[0] == pytest.approx(0.0)
    assert transform[1] != pytest.approx(0.0)
    assert occurrence["source_has_mask"] is True
    assert occurrence["transparent_alpha_preserved"] is True
    assert occurrence["unchanged_pixels_transparent"] is True
    assert occurrence["source_occurrence_index"] == 0
    assert group.Group[0].Placement.Base.z == pytest.approx(0.0)
    assert math.isclose(group.Group[0].XSize, occurrence["x_size"], abs_tol=1e-7)
    assert math.isclose(group.Group[0].YSize, occurrence["y_size"], abs_tol=1e-7)


def _make_text_over_underlay_pair(
    baseline_path: Path,
    source_path: Path,
    *,
    rotated: bool,
) -> None:
    document = core.fitz.open()
    page = document.new_page(width=300, height=200)
    page.draw_rect(
        core.fitz.Rect(30, 35, 270, 165),
        color=(0.0, 0.2, 0.8),
        fill=(0.7, 0.85, 1.0),
        width=3,
    )
    page.insert_image(
        core.fitz.Rect(75, 55, 225, 145),
        stream=_transparent_png_bytes(),
    )
    page.draw_line((35, 100), (265, 100), color=(1.0, 0.0, 0.0), width=4)
    document.save(str(baseline_path))
    document.close()

    document = core.fitz.open(str(baseline_path))
    page = document.load_page(0)
    if rotated:
        page.insert_text(
            (155, 145),
            "ROTATED OVERLAP",
            fontsize=18,
            rotate=90,
            color=(0.0, 0.0, 0.0),
            fontname="helv",
        )
    else:
        page.insert_text(
            (70, 107),
            "TEXT OVER LINE + IMAGE",
            fontsize=18,
            color=(0.0, 0.0, 0.0),
            fontname="helv",
        )
    document.save(str(source_path))
    document.close()


@pytest.mark.parametrize("rotated", [False, True])
def test_explicit_raster_structural_text_background_removes_only_text_paint(
    tmp_path: Path,
    rotated: bool,
) -> None:
    baseline = tmp_path / ("baseline-rotated.pdf" if rotated else "baseline.pdf")
    source = tmp_path / ("source-rotated.pdf" if rotated else "source.pdf")
    _make_text_over_underlay_pair(baseline, source, rotated=rotated)
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=True,
        text_mode="3d_text",
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    try:
        with core._open_pdf_source_attempt(opts) as document:
            page = document.load_page(0)
            background, evidence = core._render_text_suppressed_page_background(
                page,
                opts,
                page_number=1,
                delivered_dpi=144,
            )
        with core.fitz.open(str(baseline)) as baseline_document:
            redacted = baseline_document.load_page(0).get_pixmap(
                matrix=core.fitz.Matrix(2.0, 2.0),
                alpha=True,
            )
        with core.fitz.open(str(source)) as source_document:
            original = source_document.load_page(0).get_pixmap(
                matrix=core.fitz.Matrix(2.0, 2.0),
                alpha=True,
            )
    finally:
        core._dispose_pdf_source_attempt(opts)

    original_samples = bytes(original.samples)
    redacted_samples = bytes(redacted.samples)
    expected_samples = bytearray(len(original_samples))
    changed_pixels = 0
    for offset in range(0, len(expected_samples), 4):
        original_pixel = original_samples[offset : offset + 4]
        redacted_pixel = redacted_samples[offset : offset + 4]
        if original_pixel == redacted_pixel:
            expected_samples[offset : offset + 4] = original_pixel
        else:
            changed_pixels += 1
    assert changed_pixels > 0
    assert bytes(background.samples) == bytes(expected_samples)
    assert (
        evidence["text_suppression_method"]
        == "exact_source_original_with_all_text_delta_transparent"
    )
    assert evidence["redaction_method"] == "exact_span_quads_text_only_redaction"
    assert evidence["source_text_item_count"] == 1
    assert evidence["remaining_printable_character_count"] == 0
    assert evidence["graphics_preserved"] is True
    assert evidence["images_preserved"] is True
    assert evidence["underlay_structure_preserved"] is True
    assert evidence["unchanged_pixels_preserved"] is True
    assert evidence["changed_pixels_transparent"] is True
    assert evidence["changed_pixel_count"] == changed_pixels
    assert evidence["changed_pixels_within_text_regions"] is True
    assert evidence["rotated_text_item_count"] == (1 if rotated else 0)
    assert evidence["evidence_sha256"] == core._page_text_suppression_evidence_digest(
        evidence
    )


def _suppression_result(source_ids: list[str]) -> dict:
    suppression = {
        "schema": "bcs.freecad_page_text_suppression/1.0",
        "verified": True,
        "page_number": 1,
        "text_suppression_method": (
            "exact_source_original_with_all_text_delta_transparent"
        ),
        "raster_content_variant": "text_suppressed_page_background",
        "source_text_item_ids": list(source_ids),
        "source_text_item_ids_sha256": core._page_manifest_digest(source_ids),
        "source_text_item_count": len(source_ids),
    }
    digest = core._page_text_suppression_evidence_digest(suppression)
    suppression["evidence_sha256"] = digest
    return {
        "outcome": "verified",
        "evidence": {
            "raster_content_variant": "text_suppressed_page_background",
            "page_text_suppression": suppression,
            "page_text_suppression_evidence_sha256": digest,
        },
    }


def test_page_text_suppression_delivery_binding_refreshes_both_digests() -> None:
    result = _suppression_result(["p1:b0:l0:s0"])
    suppression = core._bind_page_text_suppression_delivery_evidence(
        result,
        {"source_item_ids": ["p1:b0:l0:s0"]},
        page_number=1,
    )

    assert suppression["delivery_source_item_ids_bound"] is True
    assert suppression["delivered_source_text_item_ids"] == ["p1:b0:l0:s0"]
    digest = core._page_text_suppression_evidence_digest(suppression)
    assert suppression["evidence_sha256"] == digest
    assert result["evidence"]["page_text_suppression_evidence_sha256"] == digest


def test_page_text_suppression_delivery_binding_persists_and_rereads_host() -> None:
    result = _suppression_result(["p1:b0:l0:s0"])
    suppression = core._bind_page_text_suppression_delivery_evidence(
        result,
        {"source_item_ids": ["p1:b0:l0:s0"]},
        page_number=1,
    )
    document = _Document()
    host = document.addObject("Image::ImagePlane", "Page_1_raster")
    core._annotate_text_host_object(host, "p1:page", "raster")
    result["created_entity_ids"] = [host.Name]

    snapshot = core._persist_page_text_suppression_host_binding(
        document,
        result,
        suppression,
        page_number=1,
    )

    assert snapshot["page_text_suppression_binding_verified"] is True
    assert snapshot["raster_content_variant"] == "text_suppressed_page_background"
    assert json.loads(snapshot["text_suppression_source_item_ids_json"]) == [
        "p1:b0:l0:s0"
    ]
    assert result["evidence"]["page_text_suppression_host_entity_id"] == host.Name
    assert result["evidence"]["page_text_suppression_host_binding_verified"] is True

    host.PDFTextSuppressionDeliveredItemIDsSHA256 = "0" * 64
    tampered = core._host_page_text_suppression_snapshot(host)
    assert tampered["page_text_suppression_binding_verified"] is False


def test_page_text_suppression_delivery_binding_rejects_stale_digest() -> None:
    result = _suppression_result(["p1:b0:l0:s0"])
    result["evidence"]["page_text_suppression"]["source_text_item_count"] = 7

    with pytest.raises(core.ImportLifecycleError, match="digest is stale"):
        core._bind_page_text_suppression_delivery_evidence(
            result,
            {"source_item_ids": ["p1:b0:l0:s0"]},
            page_number=1,
        )


def test_page_text_suppression_delivery_binding_rejects_different_source_ids() -> None:
    result = _suppression_result(["p1:b0:l0:s0"])

    with pytest.raises(core.ImportLifecycleError, match="exactly match"):
        core._bind_page_text_suppression_delivery_evidence(
            result,
            {"source_item_ids": ["p1:b0:l0:s1"]},
            page_number=1,
        )


def test_text_only_redaction_actions_are_named_and_fail_closed(monkeypatch) -> None:
    assert core._validated_text_only_redaction_actions() == (
        core.fitz.PDF_REDACT_IMAGE_NONE,
        core.fitz.PDF_REDACT_LINE_ART_NONE,
        core.fitz.PDF_REDACT_TEXT_REMOVE,
    )

    monkeypatch.setattr(core.fitz, "PDF_REDACT_TEXT_REMOVE", 1)
    with pytest.raises(ValueError, match="unavailable or incompatible"):
        core._validated_text_only_redaction_actions()


def test_explicit_raster_main_path_requests_suppressed_background_without_mode_rewrite() -> None:
    source = inspect.getsource(core._import_pdf_page_inner)

    assert "suppress_structural_text=auto_raster_text_overlay" in source
    assert 'effective_mode = "raster_text_overlay"' not in source
    assert "_bind_page_text_suppression_delivery_evidence(" in source


def test_text_suppressed_page_background_rejects_wrong_page_identity(
    tmp_path: Path,
) -> None:
    source = tmp_path / "two-pages.pdf"
    document = core.fitz.open()
    document.new_page(width=200, height=120).insert_text((20, 50), "FIRST PAGE")
    document.new_page(width=200, height=120).insert_text((20, 50), "SECOND PAGE")
    document.save(str(source))
    document.close()
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=True,
        text_mode="labels",
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1, 2])
    try:
        with core._open_pdf_source_attempt(opts) as source_document:
            wrong_page = source_document.load_page(1)
            with pytest.raises((ValueError, core.ImportLifecycleError), match="page"):
                core._render_text_suppressed_page_background(
                    wrong_page,
                    opts,
                    page_number=1,
                    delivered_dpi=144,
                )
    finally:
        core._dispose_pdf_source_attempt(opts)


def test_text_suppressed_background_rejects_later_nontext_occlusion(
    tmp_path: Path,
) -> None:
    source = tmp_path / "later-opaque-paint.pdf"
    document = core.fitz.open()
    page = document.new_page(width=240, height=140)
    page.insert_text((35, 80), "HIDDEN BY LATER PAINT", fontsize=18, fontname="helv")
    page.draw_rect(
        core.fitz.Rect(25, 55, 225, 90),
        color=(0.0, 0.0, 1.0),
        fill=(0.0, 0.0, 1.0),
        width=1,
        overlay=True,
    )
    document.save(str(source))
    document.close()
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=True,
        text_mode="labels",
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    try:
        with core._open_pdf_source_attempt(opts) as source_document:
            with pytest.raises(ValueError, match="occlud"):
                core._render_text_suppressed_page_background(
                    source_document.load_page(0),
                    opts,
                    page_number=1,
                    delivered_dpi=144,
                )
    finally:
        core._dispose_pdf_source_attempt(opts)


def test_structural_text_suppression_accepts_zero_canonical_text_as_original_page(
    tmp_path: Path,
) -> None:
    source = tmp_path / "image-only-page.pdf"
    document = core.fitz.open()
    page = document.new_page(width=240, height=140)
    page.insert_image(core.fitz.Rect(30, 20, 210, 120), stream=_transparent_png_bytes())
    document.save(str(source))
    document.close()
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=True,
        text_mode="labels",
        scale_to_mm=False,
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    try:
        with core._open_pdf_source_attempt(opts) as source_document:
            source_page = source_document.load_page(0)
            original = source_page.get_pixmap(
                matrix=core.fitz.Matrix(2.0, 2.0),
                alpha=True,
            )
            background, evidence = core._render_text_suppressed_page_background(
                source_page,
                opts,
                page_number=1,
                delivered_dpi=144,
            )
    finally:
        core._dispose_pdf_source_attempt(opts)

    assert bytes(background.samples) == bytes(original.samples)
    assert evidence["raster_content_variant"] == "original_page_no_canonical_text"
    assert evidence["source_text_item_ids"] == []
    assert evidence["source_text_item_count"] == 0
    assert evidence["redaction_quad_count"] == 0
    assert evidence["changed_pixel_count"] == 0
    assert evidence["remaining_printable_character_count"] == 0


def test_page_raster_call_path_accepts_zero_canonical_text_with_structural_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "blank-raster-page.pdf"
    document = core.fitz.open()
    document.new_page(width=180, height=100)
    document.save(str(source))
    document.close()
    opts = core.ImportOptions(
        import_mode="raster",
        import_text=True,
        text_mode="3d_text",
        scale_to_mm=False,
        raster_dpi=144,
        raster_dpi_user_set=True,
    )
    core._initialize_pdf_source_attempt(str(source), opts)
    core._capture_page_visual_runtime_authority(opts, [1])
    host_document, group = _install_image_host(monkeypatch)
    monkeypatch.setattr(core, "_raster_asset_dir", lambda: tmp_path / "assets")
    try:
        with core._open_pdf_source_attempt(opts) as source_document:
            source_page = source_document.load_page(0)
            result = core._import_page_as_raster(
                source_document,
                source_page,
                1,
                float(source_page.rect.height),
                opts,
                1.0,
                group,
                host_document,
                suppress_structural_text=True,
            )
    finally:
        core._dispose_pdf_source_attempt(opts)

    assert result["outcome"] == "verified"
    assert result["evidence"]["raster_content_variant"] == (
        "original_page_no_canonical_text"
    )
    assert result["evidence"]["page_text_suppression"]["source_text_item_ids"] == []
