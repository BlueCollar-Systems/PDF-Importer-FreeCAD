"""Native final paints require source order/style proof and exact crop exclusion."""
from pathlib import Path
import sys
import pymupdf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"))
from PDFLatePaint import final_rows, subtract_rectangles, paint_rectangles
from PDFPaintProof import final_svg_rectangles


def test_embedded_source_image_is_not_a_completed_page_text_crop(monkeypatch):
    from types import SimpleNamespace
    import PDFLatePaint as paint
    row = dict(rect=(0, 0, 10, 10), fill=(0, 1, 1), fill_opacity=.3)
    monkeypatch.setattr(paint, 'final_rows', lambda _page: [row])
    # This source image has no text-crop checksum/placement contract. It must
    # neither be rejected as a bad text crop nor cut a hole in later paint.
    image = SimpleNamespace(PropertiesList=[], PDFRepresentation='raster', PDFSourceItemId='p1:img1')
    assert paint.apply_final_paints(None, page_number=1, pdf_sha256='a'*64,
        doc=None, parent=None, objects=[image], scale=1,
        mapper=lambda point: SimpleNamespace(x=point[0],y=point[1])) == []


def test_only_final_unclipped_translucent_rectangles_are_eligible():
    with pymupdf.open() as doc:
        page = doc.new_page(width=200, height=150)
        page.draw_rect((10, 10, 60, 30), fill=(0, 1, 1), color=(0, 1, 1), fill_opacity=.3)
        page.insert_text((10, 50), "Later native text")
        page.draw_rect((10, 40, 60, 60), fill=(0, 1, 1), color=(0, 1, 1), fill_opacity=.3)
        eligible = final_rows(page)
        assert len(eligible) == 1
        assert eligible[0]["seqno"] == page.get_drawings()[-1]["seqno"]
        page.draw_line((5, 20), (100, 20))
        assert final_rows(page) == []


def test_unknown_source_read_does_not_invent_overlay_authority():
    class UnknownPage:
        def get_drawings(self, **_):
            raise RuntimeError("renderer unavailable")
    assert final_rows(UnknownPage()) == []


def test_final_crop_subtraction_preserves_area_without_overlap():
    original = (0, 0, 10, 10)
    cutters = [(2, 2, 6, 8), (4, 4, 8, 6), (20, 20, 30, 30)]
    remaining = subtract_rectangles(original, cutters)
    area = lambda r: (r[2]-r[0])*(r[3]-r[1])
    assert sum(map(area, remaining)) == pytest.approx(72)
    for i, a in enumerate(remaining):
        for b in cutters + remaining[i+1:]:
            assert min(a[2], b[2]) <= max(a[0], b[0]) or min(a[3], b[3]) <= max(a[1], b[1])
    assert subtract_rectangles(original, [(-1, -1, 11, 11)]) == []


def test_renderer_proof_rejects_unknown_clip_width_and_miter():
    with pymupdf.open() as doc:
        page = doc.new_page(width=200, height=150)
        page.draw_rect((10, 10, 60, 30), fill=(0, 1, 1), color=(0, 1, 1), fill_opacity=.3)
        svg, rows = page.get_svg_image(), page.get_drawings()
    assert final_svg_rectangles(svg, rows)
    for bad in [svg.replace('stroke-miterlimit="10"', ''),
                svg.replace('stroke-miterlimit="10"', 'stroke-miterlimit="nan"'),
                svg.replace('stroke-miterlimit="10"', 'stroke-miterlimit="1"'),
                svg.replace('stroke-width="1"', 'stroke-width="2"'),
                svg.replace('<path ', '<path clip-path="url(#unknown)" ', 1),
                svg.replace('fill-opacity=".3"', 'fill-opacity=".7"')]:
        assert not final_svg_rectangles(bad, rows)


def test_exact_miter_ring_has_source_width_and_disjoint_paint_area():
    paints = paint_rectangles((10, 20, 40, 50), 2)
    assert paints[0] == (11, 21, 39, 49)
    area = lambda r: (r[2]-r[0])*(r[3]-r[1])
    assert sum(map(area, paints)) == 32*32
    assert sum(map(area, paints[1:])) == 32*32-28*28
    for i, a in enumerate(paints):
        for b in paints[i+1:]:
            assert min(a[2], b[2]) <= max(a[0], b[0]) or min(a[3], b[3]) <= max(a[1], b[1])


def test_persisted_display_nodes_use_exact_alpha_and_never_change_shape(monkeypatch):
    import json
    from types import SimpleNamespace
    from PDFLatePaint import restore_display
    class Field:
        def setValue(self, *value): self.value = value
    class Node:
        def __init__(self):
            for name in ("translation", "diffuseColor", "ambientColor", "specularColor", "emissiveColor", "shininess", "transparency"):
                setattr(self, name, Field())
        def setName(self, value): self.name = value
        def getName(self): return self.name
        def setOverride(self, value): self.override = value
    class Root:
        def __init__(self): self.children = []
        def getNumChildren(self): return len(self.children)
        def getChild(self, i): return self.children[i]
        def removeChild(self, i): self.children.pop(i)
        def insertChild(self, node, i): self.children.insert(i, node)
    monkeypatch.setitem(sys.modules, "pivy", SimpleNamespace(coin=SimpleNamespace(SoTranslation=Node, SoMaterial=Node)))
    root = Root()
    shape = object()
    obj = SimpleNamespace(Shape=shape, ViewObject=SimpleNamespace(RootNode=root),
        PDFDisplayPaintJSON=json.dumps(dict(rgb=[.01569, 1., 1.], opacity=.300002992, display_z_mm=1.02)))
    assert restore_display(obj)
    assert restore_display(obj)
    assert obj.Shape is shape and root.getNumChildren() == 2
    assert root.children[0].translation.value == (0, 0, 1.02)
    assert root.children[1].transparency.value[0] == pytest.approx(1-.300002992)
    assert root.children[1].emissiveColor.value == (.01569, 1., 1.)
    assert root.children[1].override is True
