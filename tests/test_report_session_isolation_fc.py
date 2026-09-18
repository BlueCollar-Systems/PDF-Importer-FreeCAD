from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "PDFVectorImporter", ROOT / "PDFVectorImporter/src"):
    sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402


def test_parallel_same_drawing_reports_and_sidecars_cannot_overwrite(tmp_path, monkeypatch):
    monkeypatch.setattr(core.tempfile, "tempdir", str(tmp_path))

    def reserve_and_write(index):
        target = Path(core._default_import_report_path("same-drawing.pdf"))
        target.write_text(str(index))
        (target.parent / "parts_bootstrap.json").write_text(str(index))
        return index, target

    with ThreadPoolExecutor(max_workers=8) as pool:
        outputs = list(pool.map(reserve_and_write, range(32)))

    assert len({path.parent for _, path in outputs}) == 32
    for index, path in outputs:
        assert path.parent.parent == tmp_path
        assert path.parent.name.startswith("bcs-freecad-import-")
        assert path.read_text() == str(index)
        assert (path.parent / "parts_bootstrap.json").read_text() == str(index)


def test_terminal_report_preserves_explicit_operator_path(tmp_path, monkeypatch):
    target = str(tmp_path / "operator-selected.json")
    opts = core.ImportOptions(import_report_path=target)
    captured = {}

    def forbidden_default(_pdf):
        raise AssertionError("Explicit report path must not reserve another directory")

    def write_report(**kwargs):
        captured.update(kwargs)
        return kwargs["output_path"]

    monkeypatch.setattr(core, "_default_import_report_path", forbidden_default)
    monkeypatch.setattr(core, "write_import_report", write_report)
    result = core._write_terminal_representation_failure_report(
        pdf_path="same-drawing.pdf", opts=opts, total_pages=1, pages_imported=0,
        elapsed_ms=1.0, failure=core.TextRepresentationFailure("test failure", {}),
    )
    assert result == captured["output_path"] == target
