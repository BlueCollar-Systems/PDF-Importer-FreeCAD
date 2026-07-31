"""R-C (WS-WEEK-V ruling): hybrid full-page underlay double-render dedupe.

Behavior contract, RED-first against the v4.0.71 hybrid path:

Hybrid pages deliver vectors and text NATIVELY — the raster layer may only
carry the page's genuine embedded images, one ``Image::ImagePlane`` per
embedded image instance, bbox-matched to that image block. A full-page
``page.get_pixmap`` underlay rasterizes text + linework + images and makes
every item exist twice (double-render, confirmed in-host on the owner
Welding chart: coverage_frac 1.0 with text ink in the underlay PNG).

Locks (per the accepted dedupe spec):
1. hybrid fixture (1 embedded image + text + vectors) → exactly one
   ImagePlane; none page-sized (coverage_frac < 0.9); bbox-matched to the
   image block, CENTER-anchored (Image::ImagePlane renders centered on its
   Placement — verified in the live FreeCAD GUI viewport 2026-07-27).
2. underlay pixel lock: no raster ink for natively delivered spans — a span
   away from the image is covered by NO plane; a span physically on top of
   the image leaves no dark pixels in the patch (images-only render).
3. raster-only pages keep the full-page render (existing raster-delivery
   fixture test remains the lock — unchanged by this contract).
4. report honesty: ``result.images`` counts the per-image placements and
   ``extra.auto_reason`` states the no-full-page-underlay delivery.

Persistence: planes must survive save/reopen with their embedded raster
asset (App::PropertyFileIncluded), per the current-state evidence rules.

R4-2: every unmet precondition skips VISIBLY with the concrete reason.
"""
from __future__ import annotations

import json
import hashlib
import math
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MM_PER_PT = 25.4 / 72.0

PAGE_W_PT = 300.0
PAGE_H_PT = 200.0
IMAGE_RECT_PT = (30.0, 120.0, 130.0, 180.0)  # x0, y0, x1, y1 (PDF, y-down)
SPAN_AWAY_TEXT = "DEDUPEALPHA"
SPAN_OVER_TEXT = "OVER"
DENSE_IMAGE_COUNT = 65

_FREECADCMD_CANDIDATES = (
    os.environ.get("BCS_FREECADCMD", ""),
    r"C:\Program Files\FreeCAD 1.1\bin\FreeCADCmd.exe",
    r"C:\Program Files\FreeCAD 1.0\bin\FreeCADCmd.exe",
)

_EMBED_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\verdana.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def _find_freecadcmd() -> str:
    for candidate in _FREECADCMD_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return shutil.which("FreeCADCmd") or ""


def _find_embed_font() -> str:
    for candidate in _EMBED_FONT_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return ""


def _build_hybrid_fixture(pdf_path: Path, font_file: str) -> None:
    """One embedded LIGHT-GRAY image + stroked vectors + two embedded-font spans.

    The image is uniformly light (no dark pixels) so any dark pixel inside a
    plane's raster asset is, by construction, ink duplicated from natively
    delivered text or vectors.
    """
    import fitz

    gray = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40))
    gray.clear_with(210)  # uniform light gray
    img_bytes = gray.tobytes("png")

    doc = fitz.open()
    page = doc.new_page(width=PAGE_W_PT, height=PAGE_H_PT)
    page.insert_image(
        fitz.Rect(*IMAGE_RECT_PT), stream=img_bytes, keep_proportion=False)
    # Stroked vectors well away from the image block.
    page.draw_line(fitz.Point(150, 30), fitz.Point(280, 30), width=1.0)
    page.draw_rect(fitz.Rect(150, 50, 280, 90), width=1.0)
    # Embedded-font text: one span away from the image, one span ON the image.
    page.insert_text(
        fitz.Point(150, 115), SPAN_AWAY_TEXT,
        fontname="F0", fontfile=font_file, fontsize=12,
    )
    page.insert_text(
        fitz.Point(55, 155), SPAN_OVER_TEXT,
        fontname="F0", fontfile=font_file, fontsize=14,
    )
    # Align the embedded BaseFont with the span-reported PostScript name so
    # the fixture exercises the hybrid dedupe, not the separate
    # embedded-font identity-mismatch defect (fitz authors "Family Style"
    # BaseFont names while spans report the PS name).
    import re

    span_font = ""
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                span_font = str(span.get("font") or "")
                break
    assert span_font, "fixture text produced no span font name"
    for entry in page.get_fonts(full=True):
        xref = int(entry[0])
        doc.xref_set_key(xref, "BaseFont", "/%s" % span_font)
        kind, value = doc.xref_get_key(xref, "DescendantFonts")
        if kind == "array":
            for ref in re.findall(r"(\d+) 0 R", value):
                doc.xref_set_key(int(ref), "BaseFont", "/%s" % span_font)
                fd_kind, fd_value = doc.xref_get_key(int(ref), "FontDescriptor")
                if fd_kind == "xref":
                    fd_xref = int(fd_value.split()[0])
                    doc.xref_set_key(fd_xref, "FontName", "/%s" % span_font)
    doc.save(str(pdf_path))
    doc.close()


def _build_dense_image_fixture(pdf_path: Path) -> None:
    """A vector-marked page with enough image instances to require compaction."""
    import fitz

    pixels = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 12, 8))
    pixels.clear_with(90)
    image_bytes = pixels.tobytes("png")
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W_PT, height=PAGE_H_PT)
    for index in range(DENSE_IMAGE_COUNT):
        column, row = index % 13, index // 13
        x0 = 8.0 + column * 21.0
        y0 = 8.0 + row * 24.0
        page.insert_image(
            fitz.Rect(x0, y0, x0 + 14.0, y0 + 10.0),
            stream=image_bytes,
            keep_proportion=False,
        )
    # This line must not appear in the transparent images-only composite.
    page.draw_line(fitz.Point(5, 190), fitz.Point(295, 190), width=3.0)
    doc.save(str(pdf_path))
    doc.close()


def _expected_images_only_samples(pdf_path: Path, dpi: int):
    import fitz

    with fitz.open(str(pdf_path)) as source:
        images_only = fitz.open()
        images_only.insert_pdf(source, from_page=0, to_page=0)
        page = images_only[0]
        page.add_redact_annot(page.rect)
        try:
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                text=fitz.PDF_REDACT_TEXT_REMOVE,
            )
        except TypeError:
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        pixmap = page.get_pixmap(
            matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0), alpha=True)
        result = (pixmap.width, pixmap.height, pixmap.n, bytes(pixmap.samples))
        images_only.close()
        return result


def _fixture_spans(pdf_path: Path):
    import fitz

    spans = {}
    with fitz.open(str(pdf_path)) as doc:
        for block in doc[0].get_text("dict").get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = (span.get("text") or "").strip()
                    if text:
                        spans[text] = tuple(float(v) for v in span["bbox"])
    return spans


def _pdf_bbox_to_model(bbox):
    """PDF pts (y-down) → model mm (y-up), matching the importer transform."""
    x0, y0, x1, y1 = bbox
    return (
        x0 * MM_PER_PT,
        (PAGE_H_PT - y1) * MM_PER_PT,
        x1 * MM_PER_PT,
        (PAGE_H_PT - y0) * MM_PER_PT,
    )


def _plane_model_bbox(plane):
    """Model-space AABB of a CENTER-anchored ImagePlane."""
    cx, cy = float(plane["base"][0]), float(plane["base"][1])
    w, h = float(plane["x_size"]), float(plane["y_size"])
    return (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)


def _bbox_intersection(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, y0, x1, y1)


def _plane_dark_frac(plane, model_bbox) -> float:
    """Dark-pixel fraction of the plane's raster asset inside a model bbox."""
    import fitz

    pm = fitz.Pixmap(plane["pixels_png"] or plane["image_file"])
    px0, py0, px1, py1 = _plane_model_bbox(plane)
    w_model = px1 - px0
    h_model = py1 - py0
    dark = total = 0
    ix0 = max(0, int((model_bbox[0] - px0) / w_model * pm.width))
    ix1 = min(pm.width, int(math.ceil((model_bbox[2] - px0) / w_model * pm.width)))
    # model y-up → image row from top
    iy0 = max(0, int((py1 - model_bbox[3]) / h_model * pm.height))
    iy1 = min(pm.height, int(math.ceil((py1 - model_bbox[1]) / h_model * pm.height)))
    for yy in range(iy0, iy1):
        for xx in range(ix0, ix1):
            pixel = pm.pixel(xx, yy)
            lum = sum(pixel[:3]) / 3.0 if len(pixel) >= 3 else pixel[0]
            total += 1
            if lum < 128:
                dark += 1
    return (dark / total) if total else 0.0


PROBE_SOURCE = r"""
import json, os, shutil, sys, traceback
out_path = sys.argv[-3]
report_path = sys.argv[-2]
pdf_path = sys.argv[-1]
result = {"ok": False}


def _planes_of(doc, copy_prefix=None):
    planes = []
    for obj in doc.Objects:
        if obj.TypeId != "Image::ImagePlane":
            continue
        base = obj.Placement.Base
        raster_file = str(getattr(obj, "PDFRasterFile", "") or "")
        image_file = str(getattr(obj, "ImageFile", "") or "")
        pixels_png = ""
        if copy_prefix and image_file and os.path.isfile(image_file):
            # Doc-transient copies vanish when this probe exits; keep the
            # pixels alongside the result for the pytest side to sample.
            pixels_png = os.path.join(
                os.path.dirname(out_path),
                copy_prefix + "_" + obj.Name + ".png")
            shutil.copyfile(image_file, pixels_png)
        planes.append({
            "name": obj.Name,
            "image_file": image_file,
            "pixels_png": pixels_png,
            "raster_file": raster_file,
            "raster_file_exists": bool(raster_file) and os.path.isfile(raster_file),
            "x_size": float(obj.XSize),
            "y_size": float(obj.YSize),
            "base": [float(base.x), float(base.y), float(base.z)],
            "source_item_id": str(getattr(obj, "PDFSourceItemId", "") or ""),
            "representation": str(getattr(obj, "PDFRepresentation", "") or ""),
            "source_sha256": str(getattr(obj, "PDFSourceSHA256", "") or ""),
            "raster_sha256": str(getattr(obj, "PDFRasterSHA256", "") or ""),
            "embedded_image_instance_count": int(getattr(
                obj, "PDFEmbeddedImageInstanceCount", 0) or 0),
            "embedded_image_manifest_sha256": str(getattr(
                obj, "PDFEmbeddedImageManifestSHA256", "") or ""),
        })
    return planes


try:
    import FreeCAD
    repo = %(repo)r
    for p in (os.path.join(repo, "PDFVectorImporter", "src"),
              os.path.join(repo, "PDFVectorImporter")):
        sys.path.insert(0, p)
    import PDFImporterCore as core
    doc = FreeCAD.newDocument("hybriddedupe")
    opts = core.ImportOptions()
    opts.verbose = False
    opts.import_report_path = report_path
    with core.fitz.open(pdf_path) as source_doc:
        result["probe_image_info"] = [
            {
                "bbox": list(info.get("bbox") or []),
                "xref": int(info.get("xref") or 0),
                "width": int(info.get("width") or 0),
                "height": int(info.get("height") or 0),
            }
            for info in source_doc[0].get_image_info(xrefs=True)
        ]
    original_image_import = core._import_embedded_images_as_planes
    def observed_image_import(pdf_doc, page, *args, **kwargs):
        result["image_import_called"] = True
        result["image_info_at_import"] = [
            {
                "bbox": list(info.get("bbox") or []),
                "xref": int(info.get("xref") or 0),
            }
            for info in page.get_image_info(xrefs=True)
        ]
        try:
            placed = original_image_import(pdf_doc, page, *args, **kwargs)
        except Exception as exc:
            result["image_import_exception"] = "%%s: %%s" %% (
                exc.__class__.__name__, exc)
            raise
        result["image_import_result"] = int(placed)
        return placed
    core._import_embedded_images_as_planes = observed_image_import
    core.import_pdf(pdf_path, opts)
    result["image_planes"] = _planes_of(doc, copy_prefix="plane")
    result["ignore_images"] = bool(getattr(opts, "ignore_images", False))
    result["image_plane_count"] = int(
        getattr(opts, "image_plane_count", 0) or 0)
    result["document_object_types"] = [
        str(getattr(obj, "TypeId", "") or "") for obj in doc.Objects
    ]
    result["n_extrusions"] = sum(
        1 for obj in doc.Objects if obj.TypeId == "Part::Extrusion")
    result["text_delivered_counts"] = dict(
        getattr(opts, "text_delivered_counts", {}) or {})
    save_path = os.path.join(os.path.dirname(out_path), "hybrid_dedupe.FCStd")
    doc.saveAs(save_path)
    doc_name = doc.Name
    FreeCAD.closeDocument(doc_name)
    reopened = FreeCAD.open(save_path)
    result["reopened_planes"] = _planes_of(reopened)
    result["ok"] = True
except Exception as exc:
    result["error"] = traceback.format_exc()
    result["attempt"] = getattr(exc, "attempt", None)
with open(out_path, "w") as handle:
    json.dump(result, handle)
"""

DENSE_PROBE_SOURCE = PROBE_SOURCE.replace(
    "import json, os, shutil, sys, traceback",
    "import hashlib, json, os, shutil, sys, traceback",
).replace(
    "    core.import_pdf(pdf_path, opts)",
    """    page_group = doc.addObject(\"App::DocumentObjectGroup\", \"PDF_Page_1\")
    opts._pdf_sha256 = hashlib.sha256(open(pdf_path, \"rb\").read()).hexdigest()
    with core.fitz.open(pdf_path) as source_doc:
        page = source_doc[0]
        result[\"direct_image_import_result\"] = int(
            core._import_embedded_images_as_planes(
                source_doc, page, 1, float(page.rect.height), opts,
                core.MM_PER_PT, page_group, doc))""",
)


def test_hybrid_delivers_per_image_planes_without_full_page_underlay(tmp_path):
    try:
        import fitz  # noqa: F401
    except ImportError:
        pytest.skip("PyMuPDF (fitz) unavailable in the dev environment")
    freecadcmd = _find_freecadcmd()
    if not freecadcmd:
        pytest.skip("FreeCADCmd.exe not found — hybrid delivery not exercised")
    font_file = _find_embed_font()
    if not font_file:
        pytest.skip("no embeddable TTF found for the fixture text spans")

    fixture = tmp_path / "hybrid_dedupe_fixture.pdf"
    _build_hybrid_fixture(fixture, font_file)
    spans = _fixture_spans(fixture)
    assert SPAN_AWAY_TEXT in spans and SPAN_OVER_TEXT in spans, (
        "fixture generation lost its text spans: %s" % sorted(spans)
    )
    import fitz
    with fitz.open(str(fixture)) as fixture_doc:
        fixture_images = fixture_doc[0].get_image_info(xrefs=True)
    assert len(fixture_images) == 1, (
        "fixture generation lost its embedded image: %s" % fixture_images
    )

    probe_path = tmp_path / "hybrid_dedupe_probe.py"
    probe_path.write_text(PROBE_SOURCE % {"repo": str(REPO_ROOT)}, encoding="utf-8")
    out_path = tmp_path / "hybrid_dedupe_result.json"
    report_path = tmp_path / "hybrid_dedupe_import_report.json"

    completed = subprocess.run(
        [freecadcmd, str(probe_path), str(out_path), str(report_path), str(fixture)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out_path.is_file(), (
        "probe produced no result (rc=%s)\nstdout tail: %s\nstderr tail: %s"
        % (completed.returncode, completed.stdout[-500:], completed.stderr[-500:])
    )
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result.get("ok"), "probe failed: %s\nattempt: %s" % (
        result.get("error", ""), result.get("attempt"),
    )

    page_w_mm = PAGE_W_PT * MM_PER_PT
    page_h_mm = PAGE_H_PT * MM_PER_PT
    image_bbox_model = _pdf_bbox_to_model(IMAGE_RECT_PT)

    # ── Lock 1: one plane per embedded image instance, none page-sized ──
    planes = result.get("image_planes") or []
    assert len(planes) == 1, (
        "hybrid must place exactly one ImagePlane for the one embedded image, "
        "got %d: %s\nstdout: %s\nstderr: %s"
        % (
                len(planes),
                [p["name"] for p in planes],
                "\n".join(
                    line for line in completed.stdout.splitlines()
                    if "image" in line.lower() or "warn" in line.lower()
                ),
                completed.stderr[-2000:] + "\nprobe image info: %s"
                % {
                    "probe_image_info": result.get("probe_image_info"),
                    "ignore_images": result.get("ignore_images"),
                    "image_plane_count": result.get("image_plane_count"),
                    "image_import_called": result.get("image_import_called"),
                    "image_info_at_import": result.get("image_info_at_import"),
                    "image_import_result": result.get("image_import_result"),
                    "image_import_exception": result.get(
                        "image_import_exception"),
                    "document_object_types": result.get("document_object_types"),
                },
            )
        )
    plane = planes[0]
    coverage = (plane["x_size"] * plane["y_size"]) / (page_w_mm * page_h_mm)
    assert coverage < 0.9, (
        "hybrid placed a page-sized underlay (coverage_frac=%.3f) — full-page "
        "double-render" % coverage
    )
    want_w = image_bbox_model[2] - image_bbox_model[0]
    want_h = image_bbox_model[3] - image_bbox_model[1]
    assert math.isclose(plane["x_size"], want_w, rel_tol=0.05), (
        "plane width %.2f mm != image block width %.2f mm"
        % (plane["x_size"], want_w)
    )
    assert math.isclose(plane["y_size"], want_h, rel_tol=0.05), (
        "plane height %.2f mm != image block height %.2f mm"
        % (plane["y_size"], want_h)
    )
    want_cx = (image_bbox_model[0] + image_bbox_model[2]) / 2.0
    want_cy = (image_bbox_model[1] + image_bbox_model[3]) / 2.0
    assert abs(plane["base"][0] - want_cx) <= 1.0 and \
        abs(plane["base"][1] - want_cy) <= 1.0, (
        "plane center (%.2f, %.2f) is not the image block center (%.2f, %.2f) "
        "— Image::ImagePlane is CENTER-anchored on its Placement"
        % (plane["base"][0], plane["base"][1], want_cx, want_cy)
    )
    assert plane["base"][2] < 0.0, "image plane must sit behind native vectors"

    # ── Native text delivery is untouched by the dedupe ──
    delivered = result.get("text_delivered_counts") or {}
    assert int(delivered.get("native_3d_text", 0)) == 2, (
        "hybrid page must still deliver both spans natively, got %s" % delivered
    )

    # ── Lock 2: no raster ink under natively delivered spans ──
    away_model = _pdf_bbox_to_model(spans[SPAN_AWAY_TEXT])
    for candidate in planes:
        overlap = _bbox_intersection(_plane_model_bbox(candidate), away_model)
        assert overlap is None, (
            "plane %s covers the natively delivered span %r away from any "
            "image block — underlay double-render" % (candidate["name"], SPAN_AWAY_TEXT)
        )
    over_model = _pdf_bbox_to_model(spans[SPAN_OVER_TEXT])
    for candidate in planes:
        overlap = _bbox_intersection(_plane_model_bbox(candidate), over_model)
        if overlap is None:
            continue
        dark_frac = _plane_dark_frac(candidate, overlap)
        assert dark_frac < 0.02, (
            "image patch %s contains ink (dark_frac=%.4f) inside the natively "
            "delivered span %r — the patch must render IMAGES ONLY"
            % (candidate["name"], dark_frac, SPAN_OVER_TEXT)
        )

    # ── Lock 4: report honesty ──
    assert report_path.is_file(), "import report was not written"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    extra = report.get("extra") or {}
    result_block = report.get("result") or {}
    assert extra.get("auto_resolved_mode") == "hybrid"
    assert "no full-page underlay" in str(extra.get("auto_reason") or ""), (
        "auto_reason must state the per-image delivery, got: %r"
        % extra.get("auto_reason")
    )
    assert int(result_block.get("images") or 0) == 1, (
        "report result.images must count the per-image placements, got %s"
        % result_block.get("images")
    )
    assert int(extra.get("raster_page_count") or 0) == 0, (
        "hybrid dedupe must not record a full-page raster"
    )
    assert int(result_block.get("text_entities") or 0) == 2

    # ── Persistence: planes survive save/reopen with the embedded asset ──
    reopened = result.get("reopened_planes") or []
    assert len(reopened) == 1, (
        "ImagePlane did not survive save/reopen (got %d)" % len(reopened)
    )
    assert reopened[0]["raster_file_exists"], (
        "reopened plane lost its embedded raster asset (PDFRasterFile)"
    )


def test_dense_embedded_images_use_one_exact_persistent_transparent_composite(
    tmp_path,
):
    try:
        import fitz
    except ImportError:
        pytest.skip("PyMuPDF (fitz) unavailable in the dev environment")
    freecadcmd = _find_freecadcmd()
    if not freecadcmd:
        pytest.skip("FreeCADCmd.exe not found — dense image delivery not exercised")

    fixture = tmp_path / "dense_images_fixture.pdf"
    _build_dense_image_fixture(fixture)
    with fitz.open(str(fixture)) as source:
        assert len(source[0].get_image_info(xrefs=True)) == DENSE_IMAGE_COUNT

    probe_path = tmp_path / "dense_images_probe.py"
    probe_path.write_text(
        DENSE_PROBE_SOURCE % {"repo": str(REPO_ROOT)}, encoding="utf-8"
    )
    out_path = tmp_path / "dense_images_result.json"
    report_path = tmp_path / "dense_images_import_report.json"
    completed = subprocess.run(
        [freecadcmd, str(probe_path), str(out_path), str(report_path), str(fixture)],
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out_path.is_file(), (
        "dense probe produced no result (rc=%s)\nstdout: %s\nstderr: %s"
        % (completed.returncode, completed.stdout[-2000:], completed.stderr[-2000:])
    )
    result = json.loads(out_path.read_text(encoding="utf-8"))
    assert result.get("ok"), "dense probe failed: %s" % result.get("error", "")

    planes = result.get("image_planes") or []
    assert len(planes) == 1, (
        "dense page must compact %d source instances into one plane, got %d"
        % (DENSE_IMAGE_COUNT, len(planes))
    )
    plane = planes[0]
    assert plane["source_item_id"] == "p1:images-composite"
    assert plane["representation"] == "raster"
    assert plane["embedded_image_instance_count"] == DENSE_IMAGE_COUNT
    assert len(plane["embedded_image_manifest_sha256"]) == 64
    assert len(plane["source_sha256"]) == 64
    assert len(plane["raster_sha256"]) == 64

    composite_path = Path(plane["pixels_png"] or plane["image_file"])
    assert composite_path.is_file()
    assert hashlib.sha256(composite_path.read_bytes()).hexdigest() == plane["raster_sha256"]
    actual = fitz.Pixmap(str(composite_path))
    expected_w, expected_h, expected_n, expected_samples = (
        _expected_images_only_samples(fixture, dpi=300)
    )
    assert (actual.width, actual.height, actual.n) == (
        expected_w,
        expected_h,
        expected_n,
    )
    assert bytes(actual.samples) == expected_samples
    assert actual.alpha == 1
    # Bottom-left pixels intersect only the vector marker in the source PDF;
    # alpha zero proves vector/text ink was not duplicated into the composite.
    assert actual.pixel(10, actual.height - 42)[-1] == 0

    assert int(result.get("image_plane_count") or 0) == 1
    reopened = result.get("reopened_planes") or []
    assert len(reopened) == 1
    assert reopened[0]["raster_file_exists"]
    assert reopened[0]["source_item_id"] == "p1:images-composite"
    assert reopened[0]["embedded_image_instance_count"] == DENSE_IMAGE_COUNT
    assert (
        reopened[0]["embedded_image_manifest_sha256"]
        == plane["embedded_image_manifest_sha256"]
    )
    assert reopened[0]["raster_sha256"] == plane["raster_sha256"]
