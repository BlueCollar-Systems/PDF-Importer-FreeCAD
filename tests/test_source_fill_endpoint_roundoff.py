"""OCC may nudge a line vertex by a few double ulps. That is still the point."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "PDFVectorImporter" / "src"))
from PDFSourceFill import SourceFillError, _edge_matches, _same_requested_point


def _point(x, y, z=0.0):
    return SimpleNamespace(x=x, y=y, z=z)


class Line:
    pass


def _line(start, end):
    return SimpleNamespace(
        isNull=lambda: False,
        isValid=lambda: True,
        Vertexes=[SimpleNamespace(Point=_point(*start)), SimpleNamespace(Point=_point(*end))],
        Curve=Line(),
    )


def test_nanometer_vertex_drift_matches_the_requested_line():
    edge = _line((19.843749999999996, 276.5777777777777, 0.0), (19.84375, 44.95, 0.0))
    _edge_matches(
        edge,
        "l",
        [_point(19.843749999999996, 276.5777777777777), _point(19.84375, 44.95000000000001)],
    )
    assert _same_requested_point((1.0, 2.0, 0.0), (1.0 + 1e-12, 2.0, 0.0))


def test_a_tenth_of_a_micron_is_a_different_endpoint():
    edge = _line((0.0, 0.0, 0.0), (10.0, 0.0, 0.0))
    with pytest.raises(SourceFillError, match="endpoints"):
        _edge_matches(edge, "l", [_point(0.0, 0.0), _point(10.0 + 1e-6, 0.0)])
