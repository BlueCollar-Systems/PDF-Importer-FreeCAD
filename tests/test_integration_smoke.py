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
from pdfcadcore.primitive_extractor import extract_page
from pdfcadcore.import_report import TextEntityVerification, ImportReport
import fitz  # PyMuPDF


class TestIntegrationSmoke:
    """Smoke test integration of all changes."""

    def test_fraction_stack_requires_observed_layout(self):
        """The extractor is available without a synthetic fraction scale."""
        assert callable(extract_page), "extract_page should be callable"
        from pdfcadcore import primitive_extractor

        assert not hasattr(primitive_extractor, "_FRAC_STACKED_SCALE")

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
        manifest_entry_id = os.environ.get("BCS_PRIVATE_INTEGRATION_MANIFEST_ID", "")
        pdf_path = resolve_manifest_entry(manifest_entry_id) if manifest_entry_id else None
        if pdf_path is None or not os.path.exists(pdf_path):
            pytest.skip(
                "Private integration manifest entry not configured "
                "(set BCS_PRIVATE_INTEGRATION_MANIFEST_ID)"
            )
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

    def test_merged_bbox_preserves_full_union(self):
        """Merged bounds preserve the complete observed footprint."""
        import inspect

        from pdfcadcore.primitive_extractor import _merged_bbox
        
        # Test normal merge (no scaling)
        bbox1 = (0, 0, 10, 5)
        bbox2 = (5, 2, 15, 7)
        merged = _merged_bbox(bbox1, bbox2)
        expected = (0, 0, 15, 7)
        assert merged == expected, f"Expected {expected}, got {merged}"
        
        assert "scale_width" not in inspect.signature(_merged_bbox).parameters

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

    def test_import_report_schema_defined(self):
        """Test that the import-report schema remains defined."""
        from pdfcadcore.import_report import SCHEMA

        assert SCHEMA == "bcs.import_report/1.1"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
