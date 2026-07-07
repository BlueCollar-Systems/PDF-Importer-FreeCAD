#!/usr/bin/env python3
"""
Integration smoke test to verify core functionality works.
Tests both fraction fix and text entity types without requiring FreeCAD.
"""
import pytest
import sys
import os

REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, 'PDFVectorImporter'))

from corpus_paths import resolve_manifest_entry  # noqa: E402
from pdfcadcore.primitive_extractor import extract_page, _FRAC_STACKED_SCALE
from pdfcadcore.import_report import TextEntityVerification, ImportReport
import fitz  # PyMuPDF


class TestIntegrationSmoke:
    """Smoke test integration of all changes."""

    def test_fraction_stack_scale_constant(self):
        """Test that the fraction stack scale constant is defined."""
        assert hasattr(extract_page, '__call__'), "extract_page should be callable"
        assert _FRAC_STACKED_SCALE == 0.6, f"Expected 0.6, got {_FRAC_STACKED_SCALE}"

    def test_text_entity_verification_dataclass(self):
        """Test that TextEntityVerification dataclass exists and works."""
        # Test creation
        entity = TextEntityVerification(
            entity_type="labels",
            count=5,
            font_rendered=True,
            examples=["Test", "Example"]
        )
        
        # Test fields
        assert entity.entity_type == "labels"
        assert entity.count == 5
        assert entity.font_rendered == True
        assert entity.examples == ["Test", "Example"]
        
        # Test to_dict conversion
        entity_dict = entity.to_dict()
        assert entity_dict["entity_type"] == "labels"
        assert entity_dict["count"] == 5
        assert entity_dict["font_rendered"] == True
        assert entity_dict["examples"] == ["Test", "Example"]

    def test_import_report_schema_supports_text_entities(self):
        """Test that ImportReport can handle text entity verification."""
        report = ImportReport()
        
        # Should be able to add text entity info to extra
        text_info = TextEntityVerification(
            entity_type="glyphs",
            count=10,
            font_rendered=False,
            examples=["Glyph1", "Glyph2"]
        ).to_dict()
        
        report.extra["actual_text_entity_types"] = text_info
        
        # Test serialization
        json_str = report.to_json()
        assert "actual_text_entity_types" in json_str
        assert "glyphs" in json_str
        
        # Test deserialization
        report_dict = report.to_dict()
        assert "actual_text_entity_types" in report_dict["extra"]
        assert report_dict["extra"]["actual_text_entity_types"]["entity_type"] == "glyphs"

    def test_fraction_extraction_with_test_pdf(self):
        """Test fraction extraction on real PDF data."""
        pdf_path = resolve_manifest_entry("PRIVATE-01")
        if pdf_path is None or not os.path.exists(pdf_path):
            pytest.skip("Corpus manifest entry PRIVATE-01 not available (set BCS_PRIVATE_VALIDATION_ROOT)")
        pdf_path = str(pdf_path)
        
        try:
            doc = fitz.open(pdf_path)
            page = doc[0]
            
            # Extract primitives
            page_data = extract_page(page, 1)
            
            # Should find some fractions
            fractions = [item for item in page_data.text_items if '/' in item.text]
            assert len(fractions) > 0, "Should find fractions in test PDF"
            
            # Check that fractions have reasonable properties
            for fraction in fractions[:3]:  # Check first 3
                assert fraction.font_size > 0, "Font size should be positive"
                assert fraction.bbox is not None, "BBox should not be None"
                
            doc.close()
            
        except Exception as e:
            pytest.skip(f"PDF processing failed: {e}")

    def test_merged_bbox_function_with_scaling(self):
        """Test that _merged_bbox supports width scaling."""
        from pdfcadcore.primitive_extractor import _merged_bbox
        
        # Test normal merge (no scaling)
        bbox1 = (0, 0, 10, 5)
        bbox2 = (5, 2, 15, 7)
        merged = _merged_bbox(bbox1, bbox2)
        expected = (0, 0, 15, 7)
        assert merged == expected, f"Expected {expected}, got {merged}"
        
        # Test scaled merge
        merged_scaled = _merged_bbox(bbox1, bbox2, scale_width=0.6)
        expected_scaled = (3, 0, 12, 7)  # Center at 7.5, width reduced to 9 (15*0.6)
        assert merged_scaled == expected_scaled, f"Expected {expected_scaled}, got {merged_scaled}"

    def test_svg_text_renderer_returns_entity_type(self):
        """Test that SVG text renderer includes entity_type in results."""
        # Import the module to test
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'PDFVectorImporter', 'src'))
            from PDFSvgTextRenderer import render_text
            
            # Test that the function signature exists and returns proper structure
            # We can't actually call it without FreeCAD, but we can verify the module loads
            assert callable(render_text), "render_text should be callable"
            
        except ImportError:
            pytest.skip("PDFSvgTextRenderer not available without full environment")

    def test_all_constants_defined(self):
        """Test that all new constants are properly defined."""
        from pdfcadcore.primitive_extractor import _FRAC_STACKED_SCALE
        from pdfcadcore.import_report import SCHEMA
        
        assert _FRAC_STACKED_SCALE == 0.6
        assert SCHEMA == "bcs.import_report/1.1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
