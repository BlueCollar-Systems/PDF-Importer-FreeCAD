from copy import deepcopy
import math
from pathlib import Path
import sys

import pytest

SRC = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"
sys.path.insert(0, str(SRC))
import PDFText3DLayout as layout


def source(text="14", origins=((10., 20.), (16., 24.)), shear=.25):
    chars = []
    for character, (x, y) in zip(text, origins, strict=True):
        quad = ((x+shear*.75, y-3.), (x+2.+shear*.75, y-3.),
                (x+2.-shear*.25, y+1.), (x-shear*.25, y+1.))
        chars.append({"c": character, "origin": (x, y), "quad": quad,
                      "bbox": (x-shear*.25, y-3., x+2.+shear*.75, y+1.)})
        chars[-1]["source_font_metrics"] = dict(
            schema="mupdf_original_font_metrics/1", text=character,
            origin=(x, y), quad=quad, writing_mode=0,
            size=3., ascender=1., descender=-1./3.)
    bbox = (min(c["bbox"][0] for c in chars), min(c["bbox"][1] for c in chars),
            max(c["bbox"][2] for c in chars), max(c["bbox"][3] for c in chars))
    span = {"font": "Arial", "size": 3., "origin": origins[0], "bbox": bbox, "chars": chars}
    item = {"pdf_sha256": "a"*64, "page_number": 1, "block_index": 0,
            "line_index": 0, "span_index": 0, "source_item_id": "p1:b0:l0:s0",
            "text": text, "span": {"font": "Arial", "size": 3.},
            "origin": origins[0], "bbox": bbox, "line_direction": (1., 0.)}
    return item, {"blocks": [{"type": 0, "lines": [{"dir": (1., 0.), "spans": [span]}]}]}


def build(item, raw, **extra):
    options = dict(scale=2., font_size=6., font_name="ExactArial", host_rotation_deg=0.)
    options.update(extra)
    return layout.build_source_character_layout(item, raw, **options)


def test_positioned_fraction_characters_keep_true_origins_advance_and_shear():
    item, raw = source()
    originals = deepcopy((item, raw))
    result = build(item, raw)
    assert result["noncollinear"]
    assert result["max_baseline_offset"] == 8.
    first, second = result["characters"]
    assert first["local_origin"] == [0., 0., 0.]
    assert second["local_origin"] == [12., -8., 0.]
    assert second["advance"] == 4.
    assert second["baseline_axis"] == [1., 0.]
    assert second["up_axis"] == pytest.approx((.25/math.hypot(.25, 4.), 4./math.hypot(.25, 4.)))
    assert second["source_quad"] == [list(p) for p in raw["blocks"][0]["lines"][0]["spans"][0]["chars"][1]["quad"]]
    assert result["source_item_id"] == item["source_item_id"]
    assert result["source_pdf_sha256"] == item["pdf_sha256"]
    assert (item, raw) == originals


def test_all_collinear_characters_still_get_real_kerning_and_spaces():
    item, raw = source("AV Ω ", ((10., 20.), (12.5, 20.), (17., 20.), (18.5, 20.), (22., 20.)), shear=0.)
    result = build(item, raw)
    assert not result["noncollinear"]
    assert [char["local_origin"][0] for char in result["characters"]] == [0., 5., 14., 17., 24.]
    assert "".join(char["text"] for char in result["characters"]) == "AV Ω "
    assert [char["source_character_index"] for char in result["characters"]] == list(range(5))


@pytest.mark.parametrize("flip", [True, False])
@pytest.mark.parametrize("rotation", [0., 37., 90., 270.])
def test_inverse_host_rotation_reconstructs_transformed_source_vectors(flip, rotation):
    item, raw = source()
    result = build(item, raw, page_matrix=(0., 1., -1., 0., 500., 600.),
                   host_rotation_deg=rotation, flip_y=flip)
    char = result["characters"][1]
    cosine, sine = math.cos(math.radians(rotation)), math.sin(math.radians(rotation))
    x, y, z = char["local_origin"]
    assert (cosine*x-sine*y, sine*x+cosine*y, z) == pytest.approx((-8., -12. if flip else 12., 0.))
    x, y = char["baseline_axis"]
    assert (cosine*x-sine*y, sine*x+cosine*y) == pytest.approx((0., -1. if flip else 1.))


def test_nonuniform_affine_page_matrix_keeps_actual_advance_without_height_fitting():
    item, raw = source(shear=0.)
    result = build(item, raw, page_matrix=(3., 0., .5, 2., 500., 600.), font_size=17.)
    char = result["characters"][1]
    assert char["local_origin"] == [40., -16., 0.]
    assert char["advance"] == 12.
    assert char["up_axis"] == pytest.approx((-2./math.hypot(2., 8.), 8./math.hypot(2., 8.)))
    assert result["font_size"] == 17.  # Explicit text-scale/nominal height preserved.
    assert result["nominal_font_size_preserved"]


@pytest.mark.parametrize("fault", ["missing", "partial", "nan", "infinite", "bbox", "nonaffine",
                                   "zero_width", "zero_height", "collinear", "wrong_text", "wrong_font",
                                   "wrong_identity", "wrong_origin", "unbound_quad", "singular_matrix"])
def test_invalid_or_unbound_geometry_never_authorizes_baseline_fallback(fault):
    item, raw = source(shear=0.)
    char = raw["blocks"][0]["lines"][0]["spans"][0]["chars"][0]
    options = {}
    if fault == "missing": char.pop("quad")
    if fault == "partial": char["quad"] = char["quad"][:3]
    if fault in ("nan", "infinite"):
        char["quad"] = ((float("nan" if fault == "nan" else "inf"), 17.), *char["quad"][1:])
    if fault == "bbox": char["bbox"] = (13., 17., 11., 21.)
    if fault == "unbound_quad": char["quad"] = tuple((x+1., y) for x, y in char["quad"])
    if fault == "nonaffine": char["quad"] = ((10., 17.), (12., 17.), (11., 21.), (10., 21.))
    if fault == "zero_width": char["quad"] = ((10., 17.), (10., 17.), (10., 21.), (10., 21.)); char["bbox"] = (10., 17., 10., 21.)
    if fault == "zero_height": char["quad"] = ((10., 17.), (12., 17.), (12., 17.), (10., 17.)); char["bbox"] = (10., 17., 12., 17.)
    if fault == "collinear": char["quad"] = ((10., 17.), (12., 17.), (14., 17.), (12., 17.)); char["bbox"] = (10., 17., 14., 17.)
    if fault == "wrong_text": char["c"] = "X"
    if fault == "wrong_font": item["span"]["font"] = "Other"
    if fault == "wrong_identity": item["source_item_id"] = "p2:b0:l0:s0"
    if fault == "wrong_origin": item["origin"] = (11., 20.)
    if fault == "singular_matrix": options["page_matrix"] = (0., 0., 0., 0., 0., 0.)
    with pytest.raises(ValueError):
        build(item, raw, **options)


def test_zero_width_space_is_preserved_without_inventing_advance_or_ink():
    item, raw = source("1 ", ((10., 20.), (16., 20.)), shear=0.)
    char = raw["blocks"][0]["lines"][0]["spans"][0]["chars"][1]
    char["quad"] = ((16., 17.), (16., 17.), (16., 21.), (16., 21.))
    char["source_font_metrics"]["quad"] = char["quad"]
    char["bbox"] = (16., 17., 16., 21.)
    result = build(item, raw)
    assert result["characters"][1]["text"] == " "
    assert result["characters"][1]["advance"] == 0.
    assert result["characters"][1]["local_origin"] == [12., 0., 0.]


def test_raw_font_metric_bbox_need_not_equal_actual_character_quad():
    item, raw = source(shear=0.)
    char = raw["blocks"][0]["lines"][0]["spans"][0]["chars"][0]
    char["bbox"] = (10., 16.5, 12., 21.5)
    result = build(item, raw)
    assert result["characters"][0]["source_quad"] == [list(p) for p in char["quad"]]
    assert result["characters"][0]["source_bbox"] == list(char["bbox"])


@pytest.mark.parametrize("fault", ["missing", "text", "origin", "quad", "vertical",
                                   "nan", "infinite", "zero", "size", "baseline"])
def test_missing_invalid_or_misbound_source_metrics_fail_closed(fault):
    item, raw = source(shear=0.)
    char = raw["blocks"][0]["lines"][0]["spans"][0]["chars"][0]
    metrics = char["source_font_metrics"]
    if fault == "missing": char.pop("source_font_metrics")
    if fault == "text": metrics["text"] = "X"
    if fault == "origin": metrics["origin"] = (11., 20.)
    if fault == "quad": metrics["quad"] = metrics["quad"][:3]
    if fault == "vertical": metrics["writing_mode"] = 1
    if fault in ("nan", "infinite"): metrics["ascender"] = float("nan" if fault == "nan" else "inf")
    if fault == "zero": metrics["ascender"] = metrics["descender"]
    if fault == "size": metrics["size"] = 2.
    if fault == "baseline": metrics["ascender"] = 3.
    with pytest.raises(ValueError):
        build(item, raw)


def _pdf_with_matrices(matrices):
    fitz = pytest.importorskip("pymupdf")
    doc = fitz.open()
    for matrix in matrices:
        page = doc.new_page(width=300, height=300)
        page.insert_font(fontname="helv")
        xref = doc.get_new_xref()
        doc.update_object(xref, "<<>>")
        data = ("BT /helv 10 Tf " + " ".join(map(str, matrix)) + " 150 150 Tm (HO) Tj ET").encode()
        doc.update_stream(xref, data)
        page.set_contents(xref)
    return doc


def _canonical_item(page, raw):
    span = raw["blocks"][0]["lines"][0]["spans"][0]
    line = raw["blocks"][0]["lines"][0]
    text = "".join(char["c"] for char in span["chars"])
    return dict(pdf_sha256="a"*64, page_number=page.number+1, block_index=0,
                line_index=0, span_index=0, source_item_id=f"p{page.number+1}:b0:l0:s0",
                text=text, span=span, origin=span["origin"], bbox=span["bbox"],
                line_direction=line["dir"])


@pytest.mark.parametrize("editable", [False, True])
@pytest.mark.parametrize("matrix", [(1, 0, 0, 1), (2, 0, 0, 1), (1, 0, 0, 2),
                                    (1, 0, .5, 1), (0, 1, -1, 0),
                                    (-1, 0, 0, 1), (1, .3, .5, 2)])
def test_original_font_metrics_reconstruct_true_xy_scale_from_actual_pdf(matrix, editable):
    doc = _pdf_with_matrices([matrix])
    page = doc[0]
    raw = layout.read_source_character_geometry(page)
    item = _canonical_item(page, raw)
    fontsize = item["span"]["size"] * 2.
    from PDFTextLayout import build_source_affine_layout
    builder = build_source_affine_layout if editable else layout.build_source_character_layout
    result = builder(item, raw, font_size=fontsize, font_name="Helvetica", scale=2., host_rotation_deg=0.)
    a, b, c, d = matrix
    expected_up = (c*10.*2., d*10.*2.)
    expected_baseline = (a*10.*2., b*10.*2.)
    for char in result["characters"]:
        actual_up = [component*char["up_scale"]*fontsize for component in char["up_axis"]]
        assert actual_up == pytest.approx(expected_up, abs=4e-5)
        actual_baseline = [component*char["baseline_scale"]*fontsize for component in char["baseline_axis"]]
        assert actual_baseline == pytest.approx(expected_baseline, abs=4e-5)
        assert char["source_em_height"] == pytest.approx(math.hypot(*expected_up), abs=4e-5)
    # An intentional user text scale remains multiplicative; do not cancel it
    # by fitting the resulting native glyph to source bounds.
    enlarged = build(item, raw, font_size=fontsize*1.5)
    assert enlarged["characters"][0]["up_scale"] == result["characters"][0]["up_scale"]
    assert enlarged["font_size"] == fontsize*1.5
    doc.close()


def test_pdf_declared_advance_does_not_change_source_glyph_matrix():
    results = []
    for override in (False, True):
        doc = _pdf_with_matrices([(1, 0, 0, 1)])
        page = doc[0]
        if override:
            font = page.get_fonts()[0][0]
            doc.xref_set_key(font, "FirstChar", "72")
            doc.xref_set_key(font, "LastChar", "79")
            doc.xref_set_key(font, "Widths", "[1600 278 500 667 556 833 722 200]")
        raw = layout.read_source_character_geometry(page)
        item = _canonical_item(page, raw)
        results.append(build(item, raw, font_size=20.))
        doc.close()
    normal, override = results
    assert normal["characters"][0]["advance"] == pytest.approx(14.44, abs=4e-5)
    assert override["characters"][0]["advance"] == 32.
    assert override["characters"][1]["local_origin"][0] == 32.
    for before, after in zip(normal["characters"], override["characters"], strict=True):
        assert before["baseline_scale"] == after["baseline_scale"]
        assert before["up_scale"] == after["up_scale"]


def test_page_affine_scale_and_shear_preserve_true_font_y_magnitude():
    doc = _pdf_with_matrices([(1, 0, 0, 1)])
    raw = layout.read_source_character_geometry(doc[0])
    item = _canonical_item(doc[0], raw)
    result = build(item, raw, font_size=20., page_matrix=(3., 0., .5, 2., 0., 0.))
    char = result["characters"][0]
    assert [v*char["up_scale"]*result["font_size"] for v in char["up_axis"]] == pytest.approx((-10., 40.), abs=4e-5)
    doc.close()


def test_reader_does_not_fallback_after_runtime_failure():
    pytest.importorskip("pymupdf")
    class BrokenPage:
        def get_textpage(self, **_kwargs):
            raise RuntimeError("engine inventory unavailable")
        def get_text(self, *_args):
            pytest.fail("runtime error must not authorize another source inventory")
    with pytest.raises(RuntimeError, match="engine inventory unavailable"):
        layout.read_source_character_geometry(BrokenPage())
