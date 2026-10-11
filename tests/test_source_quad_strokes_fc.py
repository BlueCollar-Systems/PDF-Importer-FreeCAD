"""Genuine MuPDF quadrilateral strokes must survive zero fill and native errors.

The document/Part surface is a facade. These tests establish the production
page-worker route and source geometry; native persistence is tested separately.
"""
import copy
from types import SimpleNamespace

import pytest

import test_regular_compound_source_fill_fc as source

core = source.core


def quad_row(quad, *, kind="s"):
    return dict(type=kind, items=[("qu", quad)], color=(1., 0., 0.),
                stroke_opacity=1., fill=None, width=3., dashes="[3 2] 0",
                closePath=False, seqno=0)


def host(monkeypatch):
    return source.page_host(monkeypatch, [source.rectangle(10, 10, 20, 20)])


def points(wire):
    return [(p.x, p.y) for edge in wire.Shape.Edges for p in edge.points]


@pytest.mark.parametrize("make_faces", [False, True])
@pytest.mark.parametrize("detect_arcs", [False, True])
def test_source_quad_stroke_remains_four_original_straight_edges(monkeypatch, make_faces, detect_arcs):
    styles = host(monkeypatch)
    quad = core.fitz.Quad((10, 10), (35, 15), (5, 40), (30, 50))
    row = quad_row(quad)
    original = copy.deepcopy(row)
    if detect_arcs:
        monkeypatch.setattr(core, "_polyline_edges_to_arcs", lambda *args: pytest.fail("A source quad is straight"))
    opts = source.fixture.options(make_faces=make_faces, hatch_to_faces=False,
                                  compound_batch_size=0, scale_to_mm=False, detect_arcs=detect_arcs)
    doc, _, _ = source.fixture.import_page([row], opts)
    wire, = doc.named("Wire")
    assert not doc.named("Face") and row == original
    assert points(wire) == [(10, 90), (35, 85), (35, 85), (30, 50),
                            (30, 50), (5, 60), (5, 60), (10, 90)]
    assert styles[-1][1:] == ((1., 0., 0.), None, 3., [3., 2.])


def test_genuine_pdf_split_zero_fill_retains_both_quad_strokes(monkeypatch, tmp_path):
    # MuPDF emits separate f + s rows for this real B* operation, and optimizes
    # the stroke rectangles to qu items. It does not emit the mocked fs/re row.
    pdf = core.fitz.open()
    page = pdf.new_page(width=200, height=100)
    xref = pdf.get_new_xref()
    pdf.update_object(xref, "<<>>")
    pdf.update_stream(xref, b"0 1 0 rg 1 0 0 RG 3 w [3 2] 0 d\n"
                      b"10 10 m 30 10 l 30 30 l 10 30 l 10 10 l h\n"
                      b"10 10 m 30 10 l 30 30 l 10 30 l 10 10 l h B*\n")
    page.set_contents(xref)
    filename = tmp_path / "split-fill-stroke.pdf"
    pdf.save(filename)
    pdf.close()
    with core.fitz.open(filename) as original:
        rows = original[0].get_drawings(extended=True)
    assert [row["type"] for row in rows] == ["f", "s"]
    assert len(rows[1]["items"]) == 2 and all(item[0] == "qu" for item in rows[1]["items"])
    contours = [source.rectangle(10, 70, 20, 20)] * 2
    styles = source.page_host(monkeypatch, contours)
    opts = source.fixture.options(make_faces=True, hatch_to_faces=True,
                                  compound_batch_size=0, scale_to_mm=False)
    doc, opts, _ = source.fixture.import_page(rows, opts)
    assert not doc.named("Face") and not doc.named("SourceCompoundFill")
    wires = doc.named("Wire")
    assert len(wires) == 2
    expected = [(10, 10), (30, 10), (30, 10), (30, 30),
                (30, 30), (10, 30), (10, 30), (10, 10)]
    # MuPDF may reverse a stroke-only quad's perimeter while optimizing it.
    # Require one exact original cycle in either direction, without sorting
    # edges or accepting a different corner, diagonal or duplicated segment.
    reverse = [(10, 10), (10, 30), (10, 30), (30, 30),
               (30, 30), (30, 10), (30, 10), (10, 10)]
    assert all(points(wire) in (expected, reverse) for wire in wires)
    assert [row[1:] for row in styles] == [((1., 0., 0.), None, 3., [3., 2.])] * 2
    delivery, = opts._report_extra["source_compound_fill_delivery"]
    assert delivery["outcome"] == "verified_zero_ink" and not delivery["created_entity_ids"]


@pytest.mark.parametrize("quad", [SimpleNamespace(),
                                  SimpleNamespace(ul=(0, 0), ur=(float("nan"), 1), lr=(2, 2), ll=(0, 2)),
                                  SimpleNamespace(ul=(0, 0), ur=(float("inf"), 1), lr=(2, 2), ll=(0, 2))])
def test_malformed_quad_is_left_out_and_listed_and_the_page_survives(monkeypatch, quad):
    host(monkeypatch)
    good = quad_row(core.fitz.Quad((10, 10), (35, 15), (5, 40), (30, 50)))
    good["seqno"] = 1
    opts = source.fixture.options(compound_batch_size=0, scale_to_mm=False, detect_arcs=False)
    doc, opts, group = source.fixture.import_page([quad_row(quad), good], opts)
    assert group is not None and len(doc.named("Wire")) == 1      # the good quad still arrives
    entry, = opts._report_extra["geometry_items_degraded"]["items"]
    assert (entry["kind"], entry["delivered"], entry["source_paint_order"]) == ("quad", "skipped", 0)
    assert "quadrilateral" in entry["reason"] and entry["created_entity_ids"] == []


def test_unbuilt_source_edge_is_left_out_and_listed(monkeypatch):
    host(monkeypatch)
    monkeypatch.setattr(core, "_edge_line", lambda *args: None)
    doc, opts, group = source.fixture.import_page([quad_row(core.fitz.Quad((0, 0), (1, 0), (0, 1), (1, 1)))])
    assert group is not None and not doc.named("Wire") and not doc.named("StepDownOutline")
    block = opts._report_extra["geometry_items_degraded"]
    assert (block["total"], block["skipped"], block["pages"]) == (1, 1, [1])
    assert "Quadrilateral source edge was not built" in block["items"][0]["reason"]
