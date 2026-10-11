"""Utility entry points validate the actual PDF before scheduling any work."""
from pathlib import Path

import pytest
import pymupdf as fitz

from PDFVectorImporter import PDFTools


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "pages.pdf"
    with fitz.open() as document:
        for _ in range(4):
            document.new_page()
        document.save(str(path))
    return str(path)


@pytest.mark.parametrize("spec", ["0", "9", "banana", "1,banana", "1-9", "0-2", "1,,2"])
def test_invalid_utility_selection_is_rejected(pdf, spec):
    with pytest.raises(ValueError):
        PDFTools._parse_page_spec(spec, pdf)


@pytest.mark.parametrize("spec,expected", [
    ("all", [1, 2, 3, 4]), ("first", [1]), ("1,3-4,3", [1, 3, 4]),
    ("4-2", [2, 3, 4]), (" 2 - 3 ", [2, 3]),
])
def test_valid_utility_selection_is_normalized(pdf, spec, expected):
    assert PDFTools._parse_page_spec(spec, pdf) == expected


def test_broken_pdf_does_not_schedule_9999_phantom_pages(tmp_path):
    path = tmp_path / "broken.pdf"
    path.write_text("Not a PDF", encoding="utf-8")
    with pytest.raises(ValueError, match="PDF"):
        PDFTools._parse_page_spec("all", str(path))


def test_huge_out_of_range_selection_is_rejected_before_expansion(pdf, monkeypatch):
    def unexpected_range(*_args):
        raise AssertionError("validate endpoints before allocating the range")

    monkeypatch.setattr(PDFTools, "range", unexpected_range, raising=False)
    with pytest.raises(ValueError):
        PDFTools._parse_page_spec("1-999999999999", pdf)


def test_recursive_batch_prunes_its_own_evidence_directories(tmp_path: Path):
    source = tmp_path / "source.PDF"
    source.write_bytes(b"%PDF-1.4\n")
    for directory in ("Imported Evidence", "imported_evidence", ".bc_host_jobs"):
        generated = tmp_path / directory / "nested" / "copy.pdf"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(source.read_bytes())
    assert PDFTools._collect_pdf_files(str(tmp_path), recurse=True) == [str(source)]
