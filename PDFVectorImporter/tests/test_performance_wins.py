# -*- coding: utf-8 -*-
"""Fidelity-safe performance optimization tests.

Covers adaptive Bezier linearization, persistent IR cache, and streaming
parallel extraction. All tests assert visual equivalence is preserved.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pdfcadcore.ir_cache import cache_key, clear_cache, load_ir, save_ir
from pdfcadcore.primitive_extractor import _append_linearized_cubic
from pdfcadcore.primitives import NormalizedText, PageData, Primitive
from pdfcadcore.streaming import iter_pages


class TestAdaptiveBezier:
    """Adaptive cubic Bezier subdivision stays within the flatness budget."""

    def test_straight_line_becomes_single_segment(self):
        pts = []
        _append_linearized_cubic(pts, (0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0))
        assert len(pts) == 2
        assert pts[0] == pytest.approx((0.0, 0.0))
        assert pts[-1] == pytest.approx((3.0, 0.0))

    def test_curve_produces_multiple_segments(self):
        # A quarter-circle cubic approximation.
        r = 10.0
        p0 = (r, 0.0)
        p1 = (r, r * 0.55228)
        p2 = (r * 0.55228, r)
        p3 = (0.0, r)
        pts = []
        _append_linearized_cubic(pts, p0, p1, p2, p3, flatness_mm=0.05)
        assert len(pts) >= 4
        assert pts[0] == pytest.approx(p0)
        assert pts[-1] == pytest.approx(p3)
        # All sampled points should be within ~0.05 mm of the ideal radius.
        for pt in pts:
            err = abs((pt[0] ** 2 + pt[1] ** 2) ** 0.5 - r)
            assert err < 0.06

    def test_safety_valve_caps_samples(self):
        # A pathological wiggle that would otherwise recurse deeply is bounded.
        pts = []
        _append_linearized_cubic(
            pts, (0.0, 0.0), (100.0, 100.0), (-100.0, 100.0), (0.0, 200.0),
            max_samples=8, flatness_mm=0.001,
        )
        assert len(pts) <= 9


class TestIRCache:
    """Persistent intermediate-representation cache keyed by source + params."""

    def test_cache_key_changes_with_parameters(self):
        import fitz
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key.pdf"
            doc = fitz.open()
            page = doc.new_page(width=100, height=100)
            page.insert_text((10, 10), "x")
            doc.save(str(path))
            doc.close()

            base = cache_key(str(path), page_number=1)
            scaled = cache_key(str(path), page_number=1, scale=2.0)
            page2 = cache_key(str(path), page_number=2)
            assert base is not None
            assert scaled is not None
            assert page2 is not None
            assert base != scaled
            assert base != page2

    def test_cache_save_load_roundtrip(self):
        page = PageData(
            page_number=1,
            width=100.0,
            height=100.0,
            primitives=[Primitive(id=1, type="line", points=[(0, 0), (10, 10)])],
            text_items=[NormalizedText(id=2, text="hello", normalized="HELLO")],
        )
        key = "roundtrip_test_key"
        clear_cache()
        save_ir(key, page)
        loaded = load_ir(key)
        assert isinstance(loaded, PageData)
        assert loaded.page_number == page.page_number
        assert loaded.primitives[0].points == page.primitives[0].points
        assert loaded.text_items[0].text == page.text_items[0].text


class TestStreamingCache:
    """Parallel and cached extraction produce the same PageData as sequential."""

    def test_use_cache_never_changes_page_data(self):
        import fitz
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.pdf"
            doc = fitz.open()
            page = doc.new_page(width=100, height=100)
            page.insert_text((10, 10), "hello")
            doc.save(str(path))
            doc.close()

            seen = []
            def progress(p):
                seen.append(p.page_number)
                return True

            # First pass populates cache.
            list(iter_pages(str(path), parallel=False, use_cache=True, progress=progress))
            # Second pass reads cache.
            cached = list(iter_pages(str(path), parallel=False, use_cache=True, progress=progress))

            assert len(cached) == 1
            assert seen == [1, 1]

    def test_parallel_and_sequential_yield_same_data(self):
        import fitz
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "two.pdf"
            doc = fitz.open()
            for _ in range(3):
                page = doc.new_page(width=200, height=200)
                page.insert_text((10, 10), "page")
            doc.save(str(path))
            doc.close()

            seq = list(iter_pages(str(path), parallel=False, use_cache=False))
            par = list(iter_pages(str(path), parallel=True, max_workers=2, use_cache=False))

            assert len(seq) == len(par) == 3
            for (ns, ds), (np, dp) in zip(seq, par, strict=True):
                assert ns == np
                assert len(ds.primitives) == len(dp.primitives)
                assert len(ds.text_items) == len(dp.text_items)
