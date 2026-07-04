"""
Regression guardrails for PDFImporterCore style fixes.

Covers:
  - FC-2: ShapeColor assigned for filled paths (fill color not lost)
  - Lineweight conversion: pt->px (96/72) with 1px floor, no 2px inflation
  - Rotation-corrected page_h: 90/270 rotated pages use mediabox width
  - _apply_style signature: 5 positional args (stroke_rgb, fill_rgb, width, dashes, opts)
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

_CORE = Path(__file__).resolve().parents[1] / "src" / "PDFImporterCore.py"


def _source() -> str:
    return _CORE.read_text(encoding="utf-8")


class TestShapeColorAssigned(unittest.TestCase):
    """FC-2: fill_rgb must be applied to vo.ShapeColor in _apply_style."""

    def test_shapecolor_set_in_apply_style(self) -> None:
        src = _source()
        # The fix: vo.ShapeColor = fill_rgb must appear in _apply_style
        self.assertIn("vo.ShapeColor = fill_rgb", src,
                      "_apply_style must assign ShapeColor for filled faces")

    def test_apply_style_has_fill_rgb_parameter(self) -> None:
        src = _source()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_apply_style":
                args = [a.arg for a in node.args.args]
                self.assertIn("fill_rgb", args,
                              "_apply_style must accept fill_rgb parameter")
                return
        self.fail("_apply_style function not found in PDFImporterCore.py")


class TestLineweightConversion(unittest.TestCase):
    """Lineweight must use pt->px (96/72) conversion with 1px floor, not 2px."""

    def test_pt_to_px_conversion_present(self) -> None:
        src = _source()
        self.assertIn("96.0 / 72.0", src,
                      "Lineweight must convert PDF points to px at 96 dpi")

    def test_no_hardcoded_2px_floor(self) -> None:
        src = _source()
        # The old bug: max(2.0, lw) inflated all hairlines to 2px
        self.assertNotIn("max(2.0, lw)", src,
                         "2px floor must be removed; use 1px floor instead")

    def test_1px_floor_present(self) -> None:
        src = _source()
        self.assertIn("max(1.0,", src,
                      "1px minimum floor must be present in lineweight logic")


class TestRotationCorrectedPageHeight(unittest.TestCase):
    """page_h must be computed from rotation-corrected mediabox, not page.rect.height."""

    def test_page_rect_height_not_used_directly(self) -> None:
        src = _source()
        # The old bug: page_h = page.rect.height — ignores /Rotate
        # After fix this assignment must not exist
        self.assertNotIn(
            "page_h = page.rect.height",
            src,
            "page_h must not be assigned from page.rect.height (ignores /Rotate)",
        )

    def test_rotation_mediabox_swap_present(self) -> None:
        src = _source()
        # The fix uses _fc_rot in (90, 270) to swap width/height
        self.assertIn("_fc_rot in (90, 270)", src,
                      "Rotation-corrected page_h must swap mediabox dims for 90/270")

    def test_mediabox_used_for_page_height(self) -> None:
        src = _source()
        self.assertIn("_fc_mb_w if _fc_rot in (90, 270) else _fc_mb_h", src,
                      "page_h must be set from rotation-corrected mediabox")


class TestApplyStyleCallSites(unittest.TestCase):
    """All _apply_style call sites must pass fill_rgb (5-arg form)."""

    def test_all_call_sites_pass_fill_rgb(self) -> None:
        src = _source()
        tree = ast.parse(src)
        bad_calls = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "_apply_style"
            ):
                n_args = len(node.args)
                if n_args < 5:
                    bad_calls.append(
                        f"line {node.lineno}: only {n_args} positional args"
                    )
        self.assertEqual(
            bad_calls, [],
            f"All _apply_style calls must pass 5 positional args; found: {bad_calls}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
