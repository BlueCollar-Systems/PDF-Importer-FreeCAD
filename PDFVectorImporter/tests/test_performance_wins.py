# -*- coding: utf-8 -*-
# tests/test_performance_wins.py — speed optimizations without accuracy loss
"""Unit tests for the performance-only changes (adaptive Bezier, IR cache,
parallel extraction). These tests verify correctness; speed wins are measured
in the benchmark script, not here.
"""
from __future__ import annotations

import math
import os
import tempfile
from pathlib import Path

import pytest

from pdfcadcore import ir_cache
from pdfcadcore.primitive_extractor import _append_linearized_cubic
from pdfcadcore.streaming import iter_pages


class TestAdaptiveBezier:
    """Adaptive cubic Bezier subdivision preserves end points and stays
    within flatness tolerance while using fewer segments for flat curves."""

    def test_straight_line_becomes_single_segment(self):
        pts = []
        _append_linearized_cubic(pts, (0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0))
        assert len(pts) == 2
        assert pts[0] == (0.0, 0.0)
        assert pts[-1] == (3.0, 0.0)

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
        max_err = max(abs(math.hypot(x, y) - r) for x, y in pts)
        assert max_err < 0.05

    def test_max_samples_safety_valve(self):
        # Pathological loopback curve should not explode.
        pts = []
        _append_linearized_cubic(pts, (0.0, 0.0), (100.0, 0.0), (0.0, 100.0), (0.0, 0.0), max_samples=8)
        assert len(pts) <= 9


class TestIRCache:
    """Persistent PageData cache keyed by source SHA + extraction params."""

    def test_cache_key_changes_with_params(self):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(b"not a real pdf but stable bytes")
            path = f.name
        try:
            k1 = ir_cache.cache_key(path, 1)
            k2 = ir_cache.cache_key(path, 2)
            assert k1 and k2 and k1 != k2
        finally:
            os.unlink(path)

    def test_save_and_load_roundtrip(self):
        from pdfcadcore.primitives import PageData, Primitive

        page = PageData(
            page_number=3,
            width=210.0,
            height=297.0,
            primitives=[
                Primitive(
                    id=1,
                    type="line",
                    points=[(0.0, 0.0), (10.0, 10.0)],
                    bbox=(0.0, 0.0, 10.0, 10.0),
                )
            ],
        )
        key = "test_roundtrip_" + os.urandom(8).hex()
        ir_cache.save_ir(key, page)
        loaded = ir_cache.load_ir(key)
        assert loaded == page
        ir_cache.cache_path(key).unlink(missing_ok=True)


class TestStreamingCache:
    """iter_pages can use the IR cache to skip re-extraction."""

    def test_use_cache_never_changes_page_data(self):
        # A tiny real PDF is required; use an empty one-page PDF via PyMuPDF.
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
            assert seen == [1]

            # Second pass should still yield the same data from cache.
            seen.clear()
            pages = list(iter_pages(str(path), parallel=False, use_cache=True, progress=progress))
            assert seen == [1]
            assert pages[0][0] == 1
            assert pages[0][1].width == pytest.approx(100.0 * 25.4 / 72.0)

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
            for (pn_s, pd_s), (pn_p, pd_p) in zip(seq, par):
                assert pn_s == pn_p
                assert len(pd_s.primitives) == len(pd_p.primitives)
                assert len(pd_s.text_items) == len(pd_p.text_items)
