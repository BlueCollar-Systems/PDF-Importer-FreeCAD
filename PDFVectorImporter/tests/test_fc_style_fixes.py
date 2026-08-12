"""
Regression guardrails for PDFImporterCore style fixes.

Covers:
  - FC-2: ShapeColor assigned for filled paths (fill color not lost)
  - Lineweight conversion: pt->px (96/72) with 1px floor, no 2px inflation
  - Rotated pages (/Rotate 90, 270) import at the rotation-corrected height
  - _apply_style signature: 5 positional args (stroke_rgb, fill_rgb, width, dashes, opts)

Note on style: most guards here match literal substrings of the source. That is
fragile in two directions -- a literal can go stale when the implementation is
legitimately replaced (it went red for four weeks that way, see
TestRotatedPageGeometry) and it can pass vacuously when a wrapper defeats the match.
Prefer a behavioural assertion, or an AST check when the property really is about code
structure. Do not add new assertIn-on-source guards without that consideration.
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

try:  # PyMuPDF exposes `pymupdf` from 1.24; `fitz` is the legacy alias.
    import pymupdf as _fitz
except ImportError:  # pragma: no cover - depends on the installed PyMuPDF
    try:
        import fitz as _fitz
    except ImportError:  # pragma: no cover
        _fitz = None

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
        # Was: assertIn("max(1.0,", src). That matched anywhere in a 10,700-line file,
        # and two of its three matches are unrelated area clamps
        # (max(1.0, page_area_pt2), max(1.0, clip_area_pt2)) -- so the guard would have
        # stayed green if the lineweight floor itself were deleted. Bind it to the
        # LineWidth assignment instead, structurally.
        floors = []
        for node in ast.walk(ast.parse(_source())):
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(t, ast.Attribute) and t.attr == "LineWidth"
                for t in node.targets
            ):
                continue
            for call in ast.walk(node.value):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "max"
                ):
                    floors.extend(
                        arg.value for arg in call.args
                        if isinstance(arg, ast.Constant)
                        and isinstance(arg.value, (int, float))
                    )
        self.assertTrue(
            floors,
            "no LineWidth assignment clamps its value with max(); the minimum "
            "lineweight floor is missing from the lineweight logic",
        )
        self.assertIn(
            1.0, floors,
            f"LineWidth must be floored at 1.0 px, found floors {floors}. A 2.0 floor "
            "inflates every hairline; no floor lets sub-pixel widths vanish.",
        )


class TestRotatedPageGeometry(unittest.TestCase):
    """A /Rotate 90 or 270 page must import at its rotation-corrected height.

    This replaces an earlier source-text class that asserted the literals
    ``_fc_rot in (90, 270)`` and ``_fc_mb_w if _fc_rot in (90, 270) else _fc_mb_h``.
    That manual mediabox swap was removed in 77796ca (2026-07-17) in favour of
    PyMuPDF's own rotation handling, so those two guards sat red for four weeks while
    the product code was correct -- they pinned an implementation, not a behaviour. A
    third guard passed *vacuously*: it forbade the literal ``page_h = page.rect.height``
    while the source reads ``page_h = float(page.rect.height)``, so the ``float(``
    wrapper alone defeated the substring match and the test protected nothing while
    showing green.

    The replacements assert the behaviour correctness actually rests on, in two halves
    that are only sufficient together:

    * ``page.rect`` really is rotation-corrected and ``page.mediabox`` is not -- the
      PyMuPDF assumption that makes ``float(page.rect.height)`` right. If a future
      PyMuPDF changed that, these fail.
    * the importer really derives ``page_h`` from ``page.rect`` -- checked through the
      AST, not by substring, so no wrapper or reformatting can hide it.

    Do not "restore" ``_fc_rot``: a manual swap on top of an already-corrected
    ``page.rect`` would apply the rotation twice.
    """

    # Deliberately non-square: a swap that did not happen is then unmistakable.
    PAGE_W_PT = 612.0
    PAGE_H_PT = 396.0
    UNROTATED = (0, 180)
    ROTATED = (90, 270)

    def setUp(self) -> None:
        if _fitz is None:
            self.skipTest("PyMuPDF is not importable; rotation behaviour is unverifiable")

    def _page(self, doc, rotation):
        page = doc.new_page(width=self.PAGE_W_PT, height=self.PAGE_H_PT)
        page.set_rotation(rotation)
        return page

    def test_page_rect_is_rotation_corrected_while_mediabox_is_not(self) -> None:
        for rotation in self.UNROTATED + self.ROTATED:
            with self.subTest(rotation=rotation):
                doc = _fitz.open()
                try:
                    page = self._page(doc, rotation)
                    # mediabox is the raw box and must never swap ...
                    self.assertAlmostEqual(float(page.mediabox.width), self.PAGE_W_PT, places=3)
                    self.assertAlmostEqual(float(page.mediabox.height), self.PAGE_H_PT, places=3)
                    # ... while page.rect is the displayed page and must swap at 90/270.
                    if rotation in self.ROTATED:
                        self.assertAlmostEqual(float(page.rect.width), self.PAGE_H_PT, places=3)
                        self.assertAlmostEqual(float(page.rect.height), self.PAGE_W_PT, places=3)
                    else:
                        self.assertAlmostEqual(float(page.rect.width), self.PAGE_W_PT, places=3)
                        self.assertAlmostEqual(float(page.rect.height), self.PAGE_H_PT, places=3)
                finally:
                    doc.close()

    def test_importer_page_height_expression_is_rotation_correct(self) -> None:
        # Mirrors PDFImporterCore's own page_w / page_h expressions. Paired with the
        # AST test below this is not circular: the AST test proves the product uses
        # page.rect, and this proves page.rect carries the rotation.
        for rotation in self.UNROTATED + self.ROTATED:
            with self.subTest(rotation=rotation):
                doc = _fitz.open()
                try:
                    page = self._page(doc, rotation)
                    page_w = float(page.rect.width)
                    page_h = float(page.rect.height)
                    if rotation in self.ROTATED:
                        self.assertAlmostEqual(page_h, self.PAGE_W_PT, places=3)
                        self.assertAlmostEqual(page_w, self.PAGE_H_PT, places=3)
                        # The regression this class exists to prevent: a rotated page
                        # importing at the unrotated (mediabox) height.
                        self.assertNotAlmostEqual(
                            page_h, float(page.mediabox.height), places=3,
                            msg="rotated page height must not equal the raw mediabox height",
                        )
                    else:
                        self.assertAlmostEqual(page_h, self.PAGE_H_PT, places=3)
                        self.assertAlmostEqual(page_w, self.PAGE_W_PT, places=3)
                finally:
                    doc.close()

    def test_rotation_matrix_is_a_six_tuple_and_reflects_rotation(self) -> None:
        # PDFImporterCore converts page.rotation_matrix with
        # tuple(float(v) for v in page.rotation_matrix); this pins that it stays a
        # 6-element numeric sequence and is not the identity for a rotated page.
        identity = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
        for rotation in self.UNROTATED + self.ROTATED:
            with self.subTest(rotation=rotation):
                doc = _fitz.open()
                try:
                    page = self._page(doc, rotation)
                    values = tuple(float(v) for v in page.rotation_matrix)
                    self.assertEqual(len(values), 6)
                    if rotation != 0:
                        self.assertNotEqual(values, identity)
                finally:
                    doc.close()

    def _page_inner_function(self) -> ast.FunctionDef:
        for node in ast.walk(ast.parse(_source())):
            if isinstance(node, ast.FunctionDef) and node.name == "_import_pdf_page_inner":
                return node
        self.fail("_import_pdf_page_inner not found in PDFImporterCore")

    def test_page_height_is_derived_from_page_rect_not_mediabox(self) -> None:
        # Structural, not textual: this is what the vacuous predecessor could not do.
        # float(page.rect.height), page.rect.height and any future wrapper all pass;
        # anything reaching for mediabox fails.
        func = self._page_inner_function()
        assignments = [
            node for node in ast.walk(func)
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "page_h" for t in node.targets)
        ]
        self.assertTrue(assignments, "no page_h assignment found in _import_pdf_page_inner")
        for node in assignments:
            attrs = {n.attr for n in ast.walk(node.value) if isinstance(n, ast.Attribute)}
            self.assertIn(
                "rect", attrs,
                "page_h must be derived from page.rect, which PyMuPDF reports "
                "rotation-corrected",
            )
            self.assertNotIn(
                "mediabox", attrs,
                "page_h must not be derived from page.mediabox: it is the raw box and "
                "ignores /Rotate, so rotated pages would import at the wrong height",
            )

    def test_page_rotation_matrix_is_taken_from_the_host_page(self) -> None:
        func = self._page_inner_function()
        sources = [
            node for node in ast.walk(func)
            if isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Attribute) and t.attr == "_page_rotation_matrix"
                for t in node.targets
            )
        ]
        self.assertTrue(
            sources,
            "_import_pdf_page_inner must set opts._page_rotation_matrix so drawing "
            "coordinates are rotated once, authoritatively",
        )
        self.assertTrue(
            any(
                "rotation_matrix" in {
                    n.attr for n in ast.walk(node.value) if isinstance(n, ast.Attribute)
                }
                for node in sources
            ),
            "_page_rotation_matrix must be derived from the page's own rotation_matrix",
        )


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
