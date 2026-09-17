"""A host unit font must not be mistaken for the PDF's em-sized font."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "PDFVectorImporter" / "src"), str(ROOT / "PDFVectorImporter")]
import PDFImporterCore as core  # noqa: E402


@pytest.fixture(autouse=True)
def clear_font_metrics():
    core._clear_font_kern_probe_cache()
    yield
    core._clear_font_kern_probe_cache()


class NativeInk:
    def __init__(self, height=1):
        self.BoundBox = SimpleNamespace(YLength=height)

    def copy(self):
        return self


def install_metrics(monkeypatch, *, units_per_em=2000, glyph_height=1400):
    import fontTools.ttLib as ttlib

    calls = []

    class Glyph:
        def draw(self, pen):
            pen.moveTo((0, 0))
            pen.lineTo((500, 0))
            pen.lineTo((500, glyph_height))
            pen.lineTo((0, glyph_height))
            pen.closePath()

    class Font:
        def __getitem__(self, table):
            assert table == "head"
            return SimpleNamespace(unitsPerEm=units_per_em)

        def getBestCmap(self):
            return {i: "glyph" for i in range(32, 127)}

        def getGlyphSet(self):
            return {"glyph": Glyph()}

        def close(self):
            calls.append("close")

    def open_font(*_args, **_kwargs):
        calls.append("open")
        return Font()

    monkeypatch.setattr(ttlib, "TTFont", open_font)
    return calls


def test_native_cap_normalization_is_converted_to_source_em(monkeypatch):
    # A font with a 1400-unit cap in a 2000-unit em has a 0.7-em cap.
    # A host normalized to that cap must be reduced by 0.7, not used as-is.
    install_metrics(monkeypatch)
    assert core._text3d_source_em_scale("AB", "source.ttf", NativeInk()) == pytest.approx(0.7)


def test_em_normalized_host_font_is_unchanged(monkeypatch):
    install_metrics(monkeypatch)
    assert core._text3d_source_em_scale("AB", "source.ttf", NativeInk(0.7)) == pytest.approx(1)


def test_source_ink_is_cached_but_each_host_shape_is_measured(monkeypatch):
    calls = install_metrics(monkeypatch)
    assert core._text3d_source_em_scale("AB", "first.ttf", NativeInk(1)) == pytest.approx(0.7)
    assert core._text3d_source_em_scale("AB", "first.ttf", NativeInk(2)) == pytest.approx(0.35)
    assert calls == ["open", "close"]
    core._text3d_source_em_scale("AB", "second.ttf", NativeInk())
    assert calls == ["open", "close", "open", "close"]
    core._clear_font_kern_probe_cache()
    assert not core._FONT_EM_SCALE
    core._text3d_source_em_scale("AB", "first.ttf", NativeInk())
    assert len(calls) == 6


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_em_metrics_fail_without_caching_a_guess(monkeypatch, value):
    install_metrics(monkeypatch, units_per_em=value)
    with pytest.raises(RuntimeError, match="em ink"):
        core._text3d_source_em_scale("AB", "source.ttf", NativeInk())
    assert not core._FONT_EM_SCALE


@pytest.mark.parametrize("value", [0, float("nan"), float("inf")])
def test_invalid_native_ink_fails_without_guessing(monkeypatch, value):
    install_metrics(monkeypatch)
    with pytest.raises(RuntimeError, match="native source font ink"):
        core._text3d_source_em_scale("AB", "source.ttf", NativeInk(value))



def test_compound_uses_calibrated_y_and_preserves_pdf_x_and_depth(monkeypatch):
    matrices = []
    depths = []

    class Geometry:
        Faces = [object()]
        Solids = [object()]
        Volume = 10

        def __init__(self, xmin=2, xmax=6):
            self.BoundBox = SimpleNamespace(XMin=xmin, XMax=xmax, YLength=1)

        def copy(self):
            return self

        def isNull(self):
            return False

        def transformGeometry(self, matrix):
            matrices.append((matrix.A11, matrix.A22))
            return Geometry(self.BoundBox.XMin * matrix.A11, self.BoundBox.XMax * matrix.A11)

        def extrude(self, vector):
            depths.append(vector)
            return self

    install_metrics(monkeypatch)
    monkeypatch.setattr(core, "FreeCAD", SimpleNamespace(Matrix=SimpleNamespace))
    monkeypatch.setattr(core, "Vector", lambda *args: args)
    monkeypatch.setattr(core, "_ACTIVE_TEXT3D_OUTLINE_MEMO", None)
    monkeypatch.setattr(core, "_build_exact_text3d_outline_template", lambda *_: (Geometry(), 10, 2))
    shape, _horizontal, _native, advance = core._bake_exact_text3d_compound_shape(
        source_text="AB", font_path="source.ttf", font_size_fc=3,
        depth=0.3, target_advance_fc=15,
    )
    assert len(matrices) == 1
    assert matrices[0] == pytest.approx((1.5, 2.1))
    assert depths == [(0, 0, 0.3)]
    assert (shape.BoundBox.XMin, shape.BoundBox.XMax) == (3, 9)
    assert advance == 15
