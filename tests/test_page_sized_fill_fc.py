"""Only a fill that paints the page is left out as a page background.

A path was dropped whenever its bounding box exceeded 95 percent of the page
area, strokes included: a border near the sheet edge and corner-to-corner
diagonals vanished while the import reported success. The rule protects against
a sheet-sized opaque face over the drawing, so it now looks at what the fill
paints. Fixture geometry is synthetic.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "PDFVectorImporter" / "src", REPO_ROOT / "PDFVectorImporter"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
import PDFLatePaint  # noqa: E402
import PDFPaperDisplay  # noqa: E402

PAGE = (0, 0, 200, 100)
PAGE_AREA = 200.0 * 100.0
BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)
BLUE = (0.0, 0.0, 1.0)


def rect(*bounds):
    return core.fitz.Rect(*bounds)


def point(x, y):
    return core.fitz.Point(x, y)


def lines(*points):
    return [("l", point(*a), point(*b)) for a, b in zip(points, points[1:])]


def path(kind, seqno, bounds, items, *, fill=None, color=None, even_odd=False, close=False):
    row = {"type": kind, "level": 0, "seqno": seqno, "rect": rect(*bounds), "items": items,
           "even_odd": even_odd, "closePath": close}
    if fill is not None:
        row.update(fill=fill, fill_opacity=1.0)
    if color is not None:
        row.update(color=color, width=1.0, stroke_opacity=1.0)
    return row


def background(seqno=0, fill=WHITE):
    return path("f", seqno, PAGE, [("re", rect(*PAGE), 1)], fill=fill)


def small_stroke(seqno):
    return path("s", seqno, (150, 40, 160, 50), lines((150, 40), (160, 50)), color=BLACK)


# ── what the fill paints ────────────────────────────────────────────────

def kind(items, **keys):
    return core._page_sized_fill_kind(dict({"items": items}, **keys), PAGE_AREA)


def test_a_rectangle_over_the_page_is_a_background():
    assert kind([("re", rect(*PAGE), 1)]) == "background"
    assert kind([("re", rect(1, 1, 199, 99), -1)], even_odd=True) == "background"


def test_a_page_outline_drawn_with_lines_or_curves_is_a_background():
    # PDF closes a fill by itself: three lines describe the whole page.
    assert kind(lines((0, 100), (200, 100), (200, 0), (0, 0))) == "background"
    rounded = lines((10, 0), (190, 0)) + [("c", point(190, 0), point(200, 0), point(200, 0), point(200, 10))] \
        + lines((200, 10), (200, 100), (0, 100), (0, 10)) \
        + [("c", point(0, 10), point(0, 0), point(0, 0), point(10, 0))]
    assert kind(rounded) == "background"


def test_an_even_odd_frame_is_one_fill_with_an_opening():
    frame = [("re", rect(1, 1, 199, 99), 1), ("re", rect(4, 4, 196, 96), 1)]
    assert kind(frame, even_odd=True) == "holes"
    # Without the even-odd rule two outlines turning the same way paint the whole sheet.
    assert kind(frame, even_odd=False) == "background"


def test_a_nonzero_frame_needs_the_inner_outline_to_turn_the_other_way():
    outer = ("re", rect(1, 1, 199, 99), 1)          # anti-clockwise on the page
    clockwise = lines((4, 96), (4, 4), (196, 4), (196, 96), (4, 96))
    assert kind([outer] + clockwise) == "holes"
    assert kind([outer] + list(reversed([("l", b, a) for _kind, a, b in clockwise]))) == "background"
    assert kind([outer, ("re", rect(4, 4, 196, 96), -1)]) == "holes"


def test_a_plate_with_a_small_opening_still_paints_the_page():
    plate = [("re", rect(*PAGE), 1), ("re", rect(90, 45, 110, 55), 1)]
    assert kind(plate, even_odd=True) == "background"


def test_separate_pieces_with_sheet_sized_bounds_are_ordinary_fills():
    bars = [("re", rect(1, 1, 199, 3), 1), ("re", rect(1, 97, 199, 99), 1),
            ("re", rect(1, 1, 3, 99), 1), ("re", rect(197, 1, 199, 99), 1)]
    assert kind(bars) == "parts"
    band = [("qu", core.fitz.Quad(point(0, 2), point(2, 0), point(198, 100), point(200, 98)))]
    assert kind(band) == "parts"
    # Two halves that together cover the sheet are a background drawn in two pieces.
    assert kind([("re", rect(0, 0, 100, 100), 1), ("re", rect(100, 0, 200, 100), 1)]) == "background"


def test_a_fill_that_cannot_be_measured_is_treated_as_before():
    assert kind([("re", rect(*PAGE), 1), ("z", 1, 2)]) == "background"
    assert kind([]) == "background"
    assert kind([("l", point(0, 0), point(float("nan"), 5)), ("l", point(1, 5), point(3, 3)),
                 ("l", point(3, 3), point(0, 0))]) == "background"


def test_more_pieces_than_can_be_sorted_are_judged_by_their_total_area():
    count = core.PAGE_FILL_CONTOUR_LIMIT + 1
    dots = [("re", rect(index * 2, 0, index * 2 + 1, 1), 1) for index in range(count)]
    assert kind(dots + [("re", rect(198, 98, 200, 100), 1)]) == "parts"
    tiles = [("re", rect(0, 0, 200, 100.0 * (index + 1) / count), 1) for index in range(count)]
    assert kind(tiles) == "background"


# ── the page loop ───────────────────────────────────────────────────────

class Vector:
    def __init__(self, x, y, z=0):
        self.x, self.y, self.z = x, y, z


class Wire:
    def __init__(self, edges):
        self.edges = edges

    def isClosed(self):
        a, b = self.edges[0][1][0], self.edges[-1][1][-1]
        return (a.x, a.y) == (b.x, b.y)

    def isNull(self):
        return not self.edges

    def isValid(self):
        return bool(self.edges)

    def corners(self):
        return {(edge[1][0].x, edge[1][0].y) for edge in self.edges} | {
            (edge[1][-1].x, edge[1][-1].y) for edge in self.edges}


class Compound(tuple):
    def __new__(cls, shapes):
        return tuple.__new__(cls, ("compound", list(shapes)))

    def isNull(self):
        return not self[1]

    def isValid(self):
        return bool(self[1])


class HostObject:
    def __init__(self, name, type_id):
        self.Name = self.Label = name
        self.TypeId = type_id
        self.Group = []
        self.Placement = SimpleNamespace(Base=SimpleNamespace(y=0.0))

    def addProperty(self, *_args):
        return self

    def addObject(self, child):
        self.Group.append(child)

    def isDerivedFrom(self, type_id):
        return self.TypeId == type_id


class Document:
    def __init__(self):
        self.Objects = []
        self.removed = []

    def addObject(self, type_id, name):
        host = HostObject(f"{name}_{len(self.Objects) + len(self.removed)}", type_id)
        self.Objects.append(host)
        return host

    def removeObject(self, name):
        self.removed.append(name)
        self.Objects = [host for host in self.Objects if host.Name != name]

    def getObject(self, name):
        return next((host for host in self.Objects if host.Name == name), None)

    def recompute(self, *_args):
        return None

    def named(self, prefix):
        return [host for host in self.Objects if host.Name.startswith(prefix)]


class Page:
    rotation = 0

    def __init__(self, rows):
        self.rows = rows
        self.rect = rect(*PAGE)

    def get_drawings(self, *, extended):
        assert extended is True
        return self.rows

    def get_images(self, full=True):
        return []

    def get_text(self, kind, **_kwargs):
        return {"blocks": []} if kind in {"dict", "rawdict"} else []


class Pdf:
    is_encrypted = False

    def __init__(self, page):
        self.page = page

    def __len__(self):
        return 1

    def load_page(self, _index):
        return self.page


@pytest.fixture
def host(monkeypatch):
    """The FreeCAD surface the vector page path touches: warnings and paint styles."""
    seen = {"warning": [], "styles": {}, "unbuildable": False}
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(
        GuiUp=False,
        Console=SimpleNamespace(PrintMessage=lambda _text: None, PrintWarning=seen["warning"].append,
                                PrintError=lambda _text: None),
    ))

    def make_face(wires, _maker):
        if seen["unbuildable"]:
            raise RuntimeError("face maker found no valid wires")
        face = SimpleNamespace(normalAt=lambda *_args: Vector(0, 0, 1))
        return SimpleNamespace(Faces=[face], isValid=lambda: True, wires=list(wires))

    monkeypatch.setattr(core, "Part", SimpleNamespace(
        LineSegment=lambda a, b: SimpleNamespace(toShape=lambda: ("l", [a, b])),
        Wire=Wire, makeFace=make_face, makeCompound=Compound,
    ))
    monkeypatch.setattr(core, "_to_fc", lambda xy, page_h, opts, scale: Vector(xy[0], page_h - xy[1]))

    def style(obj, stroke_rgb, fill_rgb, *_args, **_kwargs):
        seen["styles"][obj.Name] = (stroke_rgb, fill_rgb)

    monkeypatch.setattr(core, "_apply_style", style)
    monkeypatch.setattr(PDFLatePaint, "apply_final_paints", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(PDFPaperDisplay, "create_paper", lambda *_args, **_kwargs: None)
    return seen


def import_page(rows):
    opts = core.ImportOptions(
        import_mode="vector", import_text=False, ignore_images=True, raster_fallback=False,
        layer_mode="none", detect_arcs=False, make_faces=False, hatch_to_faces=False, verbose=False,
    )
    core._reset_import_run_state(opts)
    opts._pdf_sha256 = "a" * 64
    document = Document()
    group, _text = core._import_pdf_page_inner(Pdf(Page(rows)), "D042.pdf", 1, opts, document)
    return document, opts, group


def wires(document, host, stroke_rgb, fill_rgb):
    """Every wire delivered with this paint, as corner sets."""
    return [wire.corners() for batch in document.named("Batch")
            if host["styles"][batch.Name] == (stroke_rgb, fill_rgb) for wire in batch.Shape[1]]


def test_a_border_near_the_sheet_edge_and_corner_diagonals_are_delivered(host):
    border = path("s", 0, (1, 1, 199, 99), [("re", rect(1, 1, 199, 99), 1)], color=BLACK)
    falling = path("s", 1, (1, 1, 199, 99), lines((1, 1), (199, 99)), color=BLACK)
    rising = path("s", 2, (1, 1, 199, 99), lines((199, 1), (1, 99)), color=BLACK)
    document, opts, group = import_page([border, falling, rising, small_stroke(3)])
    delivered = wires(document, host, BLACK, None)
    assert len(delivered) == 4
    assert {(1, 99), (199, 99), (199, 1), (1, 1)} in delivered            # the border, all four corners
    assert {(1, 99), (199, 1)} in delivered and {(199, 99), (1, 1)} in delivered
    assert group is not None and host["warning"] == []


def test_a_stroke_that_runs_past_the_sheet_is_delivered(host):
    # Its bounds are larger than the page; it is still a stroke, not a background.
    runaway = path("s", 0, (50, 50, 900, 400), lines((50, 50), (900, 400)), color=BLACK)
    document, _opts, _group = import_page([runaway, small_stroke(1)])
    assert len(wires(document, host, BLACK, None)) == 2


@pytest.mark.parametrize("row", [
    background(),
    background(fill=BLUE),
    path("f", 0, PAGE, lines((0, 100), (200, 100), (200, 0), (0, 0)), fill=WHITE),
    dict(background(), fill_opacity=0.4),
    path("f", 0, (-10, -10, 210, 110), [("re", rect(-10, -10, 210, 110), 1)], fill=WHITE),
])
def test_a_page_sized_background_fill_is_still_left_out(host, row):
    document, _opts, group = import_page([row, small_stroke(1)])
    assert group is not None
    assert [batch.Name for batch in document.named("Batch")] == ["Batch_1_1"]
    assert host["styles"]["Batch_1_1"] == (BLACK, None)                  # the small stroke only
    assert len(document.named("Batch")[0].Shape[1]) == 1
    assert document.named("Face") == [] and host["warning"] == []


def test_a_filled_and_stroked_page_rectangle_keeps_its_outline_only(host):
    sheet = path("fs", 0, (1, 1, 199, 99), [("re", rect(1, 1, 199, 99), 1)], fill=WHITE, color=BLACK)
    document, _opts, _group = import_page([sheet, small_stroke(1)])
    # One paint style, stroke without fill: the outline and the small stroke.
    assert set(host["styles"].values()) == {(BLACK, None)}
    assert len(wires(document, host, BLACK, None)) == 2
    assert document.named("Face") == []


def test_a_frame_drawn_as_one_fill_keeps_its_opening(host):
    frame = path("f", 0, (1, 1, 199, 99), [("re", rect(1, 1, 199, 99), 1), ("re", rect(4, 4, 196, 96), 1)],
                 fill=BLUE, even_odd=True)
    document, _opts, group = import_page([frame, small_stroke(1)])
    (face,) = document.named("Face")
    assert host["styles"][face.Name] == (None, BLUE)
    assert sorted(len(wire.corners()) for wire in face.Shape.wires) == [4, 4]   # outer outline and opening
    assert any(face in child.Group for child in document.Objects) or face in group.Group
    # Not also as two independent filled outlines, which would cover the sheet.
    assert wires(document, host, None, BLUE) == []
    assert len(wires(document, host, BLACK, None)) == 1


def test_a_filled_and_stroked_frame_delivers_the_fill_once_and_both_outlines(host):
    frame = path("fs", 0, (1, 1, 199, 99), [("re", rect(1, 1, 199, 99), 1), ("re", rect(4, 4, 196, 96), 1)],
                 fill=BLUE, color=BLACK, even_odd=True)
    document, _opts, _group = import_page([frame])
    assert len(document.named("Face")) == 1
    assert len(wires(document, host, BLACK, None)) == 2
    assert wires(document, host, BLACK, BLUE) == []


def test_a_frame_the_host_cannot_build_is_left_out_and_reported(host):
    host["unbuildable"] = True
    frame = path("f", 7, (1, 1, 199, 99), [("re", rect(1, 1, 199, 99), 1), ("re", rect(4, 4, 196, 96), 1)],
                 fill=BLUE, even_odd=True)
    document, _opts, group = import_page([frame, small_stroke(8)])
    assert group is not None and document.named("Face") == []
    assert wires(document, host, None, BLUE) == []                        # never drawn without its opening
    assert len(wires(document, host, BLACK, None)) == 1                   # the page continues
    (line,) = host["warning"]
    assert "drawing order 7" in line and "left out" in line and "face maker found no valid wires" in line


def test_a_frame_made_of_four_bars_is_delivered_as_its_bars(host):
    bars = path("f", 0, (1, 1, 199, 99), [
        ("re", rect(1, 1, 199, 3), 1), ("re", rect(1, 97, 199, 99), 1),
        ("re", rect(1, 1, 3, 99), 1), ("re", rect(197, 1, 199, 99), 1)], fill=BLACK)
    document, _opts, _group = import_page([bars])
    assert len(wires(document, host, None, BLACK)) == 4
    assert document.named("Face") == []


def test_a_fill_below_the_page_limit_is_not_examined(host, monkeypatch):
    def unexpected(*_args):
        raise AssertionError("only sheet-sized fills are measured")

    monkeypatch.setattr(core, "_page_sized_fill_kind", unexpected)
    panel = path("f", 0, (0, 0, 190, 100), [("re", rect(0, 0, 190, 100), 1)], fill=BLUE)   # exactly 95 percent
    border = path("s", 1, (0, 0, 200, 100), [("re", rect(*PAGE), 1)], color=BLACK)
    document, _opts, _group = import_page([panel, border])
    assert len(wires(document, host, None, BLUE)) == 1
    assert len(wires(document, host, BLACK, None)) == 1
