from __future__ import annotations

import sys
import unittest
from pathlib import Path


SRC_DIR = Path(__file__).resolve().parents[1] / "PDFVectorImporter" / "src"
CORE_PATH = SRC_DIR / "PDFImporterCore.py"
sys.path.insert(0, str(SRC_DIR))

from PDFImporterCore import (  # noqa: E402
    MM_PER_PT,
    _build_text_layout_context,
    _effective_descender,
    _fit_font_size_to_span_bbox,
    _horizontal_quantity_origin_pdf,
    _is_bom_quantity_span,
    _norm_color,
    _reconstruct_line_text,
    _repair_fraction_artifact_runs,
)
from pdfcadcore.text_scale import effective_span_font_size_pt  # noqa: E402


class TestPdfImporterTextReconstruction(unittest.TestCase):
    def test_repair_fraction_artifact_runs(self) -> None:
        self.assertEqual(
            _repair_fraction_artifact_runs("19/163 7/161 9/16"),
            "1 9/16 3 7/16 1 9/16",
        )
        self.assertEqual(
            _repair_fraction_artifact_runs("5/16"),
            "5/16",
        )

    def test_reconstruct_line_text_repairs_run_on_fraction(self) -> None:
        spans = [
            {"text": "19", "size": 8.0, "flags": 0},
            {"text": "/", "size": 8.0, "flags": 0},
            {"text": "16", "size": 8.0, "flags": 0},
            {"text": "3", "size": 12.0, "flags": 0},
        ]
        self.assertEqual(_reconstruct_line_text(spans), "1 9/16 3")

    def test_descender_correction_targets_only_real_descenders(self) -> None:
        self.assertAlmostEqual(_effective_descender("p1052", -0.2), -0.2)
        self.assertAlmostEqual(_effective_descender("W12X30", -0.2), -0.004)

    def test_shapestring_3d_text_uses_geometry_size_property(self) -> None:
        source = CORE_PATH.read_text(encoding="utf-8")
        self.assertIn("ss.Size = font_size_fc", source)
        self.assertIn("ss.ScaleToSize = True", source)
        self.assertIn("ss.MakeFace = True", source)
        self.assertNotIn("ss.ViewObject.FontSize = font_size_fc", source)

    def test_raster_background_uses_effective_import_scale(self) -> None:
        source = CORE_PATH.read_text(encoding="utf-8")
        self.assertIn("w_units = page.rect.width * scale", source)
        self.assertIn("h_units = page.rect.height * scale", source)
        self.assertNotIn("w_mm = page.rect.width * MM_PER_PT", source)
        self.assertNotIn("h_mm = page.rect.height * MM_PER_PT", source)

    def test_exact_label_path_applies_baseline_anchor_correction(self) -> None:
        source = CORE_PATH.read_text(encoding="utf-8")
        self.assertIn("callouts do not drift", source)
        self.assertIn(
            "offset_fc = _effective_descender(txt, desc) * font_size_fc * 0.35",
            source,
        )

    def test_span_bbox_fit_keeps_normal_text_size(self) -> None:
        span = {"bbox": (10.0, 20.0, 80.0, 32.0)}
        size = 10.0 * MM_PER_PT

        fitted = _fit_font_size_to_span_bbox("W12X30", size, span, MM_PER_PT, 0.0)

        self.assertAlmostEqual(fitted, size)

    def test_span_bbox_fit_shrinks_oversized_horizontal_text(self) -> None:
        span = {"bbox": (10.0, 20.0, 34.0, 32.0)}
        size = 12.0 * MM_PER_PT

        fitted = _fit_font_size_to_span_bbox("LONG CALLOUT TEXT", size, span, MM_PER_PT, 0.0)

        self.assertLess(fitted, size)

    def test_span_bbox_fit_shrinks_oversized_vertical_text_normal_axis(self) -> None:
        span = {"bbox": (10.0, 20.0, 16.0, 70.0)}
        size = 18.0 * MM_PER_PT

        fitted = _fit_font_size_to_span_bbox("3 3/8", size, span, MM_PER_PT, 90.0)

        self.assertLess(fitted, size)

    def test_span_bbox_fit_grows_undersized_tf_relative_to_bbox(self) -> None:
        span = {"bbox": (10.0, 20.0, 70.0, 34.0), "size": 6.0}
        size = effective_span_font_size_pt(span, 0.0) * MM_PER_PT

        fitted = _fit_font_size_to_span_bbox("W12X30", size, span, MM_PER_PT, 0.0)

        self.assertGreaterEqual(fitted, size * 0.95)

    def test_packed_integer_text_color_decodes_to_rgb(self) -> None:
        rgb = _norm_color(0x66AA33)

        self.assertAlmostEqual(rgb[0], 0x66 / 255.0, places=4)
        self.assertAlmostEqual(rgb[1], 0xAA / 255.0, places=4)
        self.assertAlmostEqual(rgb[2], 0x33 / 255.0, places=4)

    def test_bom_quantity_span_is_kept_horizontal_from_quan_context(self) -> None:
        tdict = {
            "blocks": [{
                "type": 0,
                "lines": [{
                    "spans": [{"text": "QUAN", "bbox": (100.0, 20.0, 124.0, 32.0)}],
                }],
            }],
        }
        quantity_span = {
            "text": "1",
            "bbox": (106.0, 78.0, 112.0, 92.0),
            "size": 8.0,
            "descender": -0.2,
        }
        ctx = _build_text_layout_context(tdict)

        self.assertTrue(_is_bom_quantity_span(quantity_span, "1", ctx))
        origin = _horizontal_quantity_origin_pdf(quantity_span, "1", 8.0 * MM_PER_PT, MM_PER_PT)

        self.assertIsNotNone(origin)
        self.assertLess(origin[0], 109.0)
        self.assertAlmostEqual(origin[1], 90.4, places=2)

if __name__ == "__main__":
    unittest.main(verbosity=2)
