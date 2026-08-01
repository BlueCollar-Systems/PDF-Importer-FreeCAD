from pathlib import Path

from PDFVectorImporter import PDFTools


def test_batch_pdf_enumeration_ignores_directories_with_pdf_suffix(tmp_path: Path):
    real_pdf = tmp_path / "Garden—Map.pdf"
    real_pdf.write_bytes(b"%PDF-1.4\n")
    (tmp_path / "false-positive.pdf").mkdir()

    assert PDFTools._collect_pdf_files(str(tmp_path), recurse=False) == [
        str(real_pdf)
    ]


def test_recursive_batch_pdf_enumeration_keeps_only_files(tmp_path: Path):
    nested = tmp_path / "nested"
    nested.mkdir()
    real_pdf = nested / "page-2.pdf"
    real_pdf.write_bytes(b"%PDF-1.4\n")
    (nested / "directory.pdf").mkdir()

    assert PDFTools._collect_pdf_files(str(tmp_path), recurse=True) == [
        str(real_pdf)
    ]
