"""Boolean paint cells must reach refinement without sewing their boundaries."""
import copy
import json
from types import SimpleNamespace

import pytest

from test_regular_compound_source_fill_fc import fill
import test_regular_compound_source_fill_fc as page_fixture


class Edge:
    def __init__(self, token, placement=(0, 0, 0)):
        self.token, self.placement = token, placement

    def isSame(self, other):
        return self.token == other.token and self.placement == other.placement

    def isNull(self):
        return False

    def isValid(self):
        return True


class Face:
    def __init__(self, token, area, edges, placement=(0, 0, 0)):
        self.token, self.Area, self.Edges, self.placement = token, area, edges, placement
        self.Faces = [self]

    def isSame(self, other):
        return self.token == other.token and self.placement == other.placement

    def isNull(self):
        return False

    def isValid(self):
        return True


class Shape:
    def __init__(self, faces, kind="Compound"):
        self.Faces, self.ShapeType = list(faces), kind
        self.Area = sum(face.Area for face in faces)

    def isNull(self):
        return False

    def isValid(self):
        return True


class Kernel:
    def __init__(self):
        self.shells, self.compounds = [], []

    def makeShell(self, faces):
        self.shells.append(list(faces))
        return Shape(faces, "Shell")

    def makeCompound(self, shapes):
        self.compounds.append(list(shapes))
        return Shape([face for shape in shapes for face in shape.Faces])


def winding_cells():
    # Three positive cells of two overlapping rectangles. Shared native edges
    # retain the same identity even when Python hands out a different wrapper.
    return [Face("first", 30000, [Edge("left"), Edge("bottom"), Edge("shared-a")]),
            Face("overlap", 10000, [Edge("shared-a"), Edge("shared-b")]),
            Face("last", 30000, [Edge("shared-b"), Edge("right"), Edge("top")])]


def test_three_winding_cells_become_one_shell_with_original_faces_and_edges():
    faces = winding_cells()
    original = Shape(faces)
    kernel = Kernel()
    result = fill._shell_connected_paint_faces(original, kernel, lambda: None)
    assert result.ShapeType == "Shell" and result.Area == 70000
    assert result.Faces == faces and kernel.shells == [faces]
    assert original.Faces == faces and not kernel.compounds


def test_separate_region_is_not_sewn_to_the_connected_component():
    faces = winding_cells() + [Face("island", 25, [Edge("island-outline")])]
    kernel = Kernel()
    result = fill._shell_connected_paint_faces(Shape(faces), kernel, lambda: None)
    assert kernel.shells == [faces[:3]]
    assert result.Faces == faces and result.Area == 70025
    assert len(kernel.compounds) == 1
    assert kernel.compounds[0][1] is faces[3]


def test_singleton_hole_and_touching_vertices_do_not_request_shells():
    kernel = Kernel()
    for faces in ([Face("ring", 239600, [Edge("outer"), Edge("hole")])],
                  [Face("a", 10, [Edge("same-geometry-a")]),
                   Face("b", 10, [Edge("same-geometry-b")])],
                  [Face("a", 10, [Edge("shared", (0, 0, 0))]),
                   Face("b", 10, [Edge("shared", (1, 0, 0))])]):
        original = Shape(faces)
        assert fill._shell_connected_paint_faces(original, kernel, lambda: None) is original
    assert not kernel.shells and not kernel.compounds


@pytest.mark.parametrize("fault", ["duplicate_face", "duplicate_edge", "three_owners"])
def test_ambiguous_or_nonmanifold_paint_refuses_before_shell_creation(fault):
    faces = winding_cells()
    if fault == "duplicate_face":
        faces.append(faces[0])
    elif fault == "duplicate_edge":
        faces[0].Edges.append(Edge("shared-a"))
    else:
        faces[2].Edges.append(Edge("shared-a"))
    kernel = Kernel()
    with pytest.raises(fill.SourceFillError):
        fill._shell_connected_paint_faces(Shape(faces), kernel, lambda: None)
    assert not kernel.shells


@pytest.mark.parametrize("fault", ["lost_face", "duplicate_face", "moved_face", "lost_edge",
                                  "changed_edge", "moved_edge", "wrong_area", "invalid",
                                  "not_shell", "null", "throws"])
def test_native_shell_constructor_must_preserve_face_and_edge_bijection(fault):
    class FaultKernel(Kernel):
        def makeShell(self, faces):
            if fault == "throws":
                raise RuntimeError("native constructor failed")
            result = Shape(copy.deepcopy(faces), "Shell")
            if fault == "lost_face":
                result.Faces.pop()
            elif fault == "duplicate_face":
                result.Faces[-1] = result.Faces[0]
            elif fault == "moved_face":
                result.Faces[0].placement = (1, 0, 0)
            elif fault == "lost_edge":
                result.Faces[0].Edges.pop()
            elif fault == "changed_edge":
                result.Faces[0].Edges[0].token = "changed"
            elif fault == "moved_edge":
                result.Faces[0].Edges[0].placement = (1, 0, 0)
            elif fault == "wrong_area":
                result.Area += 1
            elif fault == "invalid":
                result.isValid = lambda: False
            elif fault == "not_shell":
                result.ShapeType = "Compound"
            elif fault == "null":
                result.isNull = lambda: True
            return result
    with pytest.raises(fill.SourceFillError):
        fill._shell_connected_paint_faces(Shape(winding_cells()), FaultKernel(), lambda: None)


def test_recombined_components_cannot_silently_lose_a_face():
    class FaultKernel(Kernel):
        def makeCompound(self, shapes):
            return Shape(shapes[0].Faces)
    faces = winding_cells() + [Face("island", 25, [Edge("island-outline")])]
    with pytest.raises(fill.SourceFillError):
        fill._shell_connected_paint_faces(Shape(faces), FaultKernel(), lambda: None)


@pytest.mark.parametrize("at", [1, 2, 4, 8, 12, 15, 16])
def test_cancellation_remains_exact_and_never_publishes_native_objects(at):
    error = KeyboardInterrupt("cancelled at owned geometry boundary")
    calls = []
    def cancel():
        calls.append(None)
        if len(calls) == at:
            raise error
    with pytest.raises(KeyboardInterrupt) as caught:
        fill._shell_connected_paint_faces(Shape(winding_cells()), Kernel(), cancel)
    assert caught.value is error


@pytest.mark.parametrize("result", [1, None, "true"])
def test_identity_boolean_is_typed(result):
    left = SimpleNamespace(isSame=lambda right: result)
    with pytest.raises(fill.SourceFillError):
        fill._native_same(left, left)


def test_shell_refusal_rolls_back_page_and_persists_failed_nonready_report(monkeypatch, tmp_path):
    contours = [page_fixture.rectangle(10, 10, 20, 20),
                page_fixture.rectangle(15, 15, 10, 10)]
    page_fixture.page_host(monkeypatch, contours)
    failure = fill.SourceFillError("Native source fill shell identity changed")
    def refuse(*args):
        raise failure
    monkeypatch.setattr(fill, "_shell_connected_paint_faces", refuse)
    opts = page_fixture.fixture.options(hatch_to_faces=True, compound_batch_size=0, scale_to_mm=False)
    document = page_fixture.fixture.Document()
    with pytest.raises(page_fixture.core.DrawingGeometryFailure) as caught:
        page_fixture.run_page_wrapper(monkeypatch, tmp_path, contours, opts, document)
    assert not document.Objects and opts.import_status == "failed"
    assert not opts._report_extra.get("source_compound_fill_delivery")
    report = tmp_path / "failure-report.json"
    opts.import_report_path = str(report)
    page_fixture.core._write_terminal_representation_failure_report(
        pdf_path=str(tmp_path / "source.pdf"), opts=opts, pages_imported=0,
        total_pages=1, elapsed_ms=1., failure=caught.value)
    result = json.loads(report.read_text())
    assert result["extra"]["result_status"] == "failed"
    assert result["extra"]["import_contract_ready"]["ready"] is False
