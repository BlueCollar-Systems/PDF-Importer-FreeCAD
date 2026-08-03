from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from scripts import build_steel_shapes_release


def _fixture(root: Path) -> Path:
    source = root / "steel_shapes"
    (source / "dxf").mkdir(parents=True)
    (source / "source").mkdir()
    (source / "README.md").write_text("Synthetic steel pack\n", encoding="utf-8")
    (source / "dxf" / "shape.dxf").write_text("0\nEOF\n", encoding="ascii")
    (source / "source" / "shape.csv").write_text("name,area\nW1,1\n", encoding="ascii")
    return source


def test_build_is_byte_reproducible_and_publishes_exact_checksums(tmp_path: Path) -> None:
    source = _fixture(tmp_path)
    first = build_steel_shapes_release.build(source, tmp_path / "out-a", "steel-v1.0.1")
    second = build_steel_shapes_release.build(source, tmp_path / "out-b", "steel-v1.0.1")

    assert first.versioned.read_bytes() == second.versioned.read_bytes()
    assert first.versioned.read_bytes() == first.latest.read_bytes()
    digest = hashlib.sha256(first.versioned.read_bytes()).hexdigest()
    assert first.checksums.read_text(encoding="ascii") == (
        f"{digest}  {first.versioned.name}\n"
        f"{digest}  {first.latest.name}\n"
    )

    with zipfile.ZipFile(first.versioned) as archive:
        assert archive.namelist() == ["README.md", "dxf/shape.dxf", "source/shape.csv"]
        assert all(member.date_time == (1980, 1, 1, 0, 0, 0) for member in archive.infolist())
        assert all((member.external_attr >> 16) == 0o100644 for member in archive.infolist())


def test_build_normalizes_text_line_endings_across_checkout_platforms(
    tmp_path: Path,
) -> None:
    lf_source = _fixture(tmp_path / "lf")
    crlf_source = _fixture(tmp_path / "crlf")
    canonical = {
        "README.md": b"Synthetic steel pack\n",
        "dxf/shape.dxf": b"0\nEOF\n",
        "source/shape.csv": b"name,area\nW1,1\n",
    }
    for relative, content in canonical.items():
        (lf_source / relative).write_bytes(content)
        (crlf_source / relative).write_bytes(content.replace(b"\n", b"\r\n"))

    lf = build_steel_shapes_release.build(
        lf_source, tmp_path / "lf-out", "steel-v1.0.1"
    )
    crlf = build_steel_shapes_release.build(
        crlf_source, tmp_path / "crlf-out", "steel-v1.0.1"
    )

    assert lf.versioned.read_bytes() == crlf.versioned.read_bytes()
    with zipfile.ZipFile(crlf.versioned) as archive:
        assert archive.read("README.md") == b"Synthetic steel pack\n"
        assert archive.read("dxf/shape.dxf") == b"0\nEOF\n"


def test_build_rejects_private_or_machine_bound_payloads(tmp_path: Path) -> None:
    source = _fixture(tmp_path)
    (source / "dxf" / "shape.dxf").write_bytes(b"C:\\Users\\Example\\Desktop\\shape.dxf")
    with pytest.raises(RuntimeError, match="machine-bound path"):
        build_steel_shapes_release.build(source, tmp_path / "path-out", "steel-v1.0.1")

    (source / "dxf" / "shape.dxf").write_bytes(b"0\nEOF\n")
    (source / "customer.pdf").write_bytes(b"%PDF-1.4\n")
    with pytest.raises(RuntimeError, match="private CAD/PDF artifact extension"):
        build_steel_shapes_release.build(source, tmp_path / "pdf-out", "steel-v1.0.1")


@pytest.mark.parametrize("tag", ["v1.0.1", "steel-latest", "steel-v1.0"])
def test_build_rejects_noncanonical_tag(tmp_path: Path, tag: str) -> None:
    with pytest.raises(ValueError, match="canonical steel release tag"):
        build_steel_shapes_release.build(_fixture(tmp_path), tmp_path / "out", tag)


def test_workflow_uses_exact_convergent_nonlatest_publisher() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "steel-shapes-release.yml").read_text(
        encoding="utf-8"
    )
    assert "scripts/build_steel_shapes_release.py" in workflow
    assert "scripts/publish_release.py" in workflow
    assert '--target "$GITHUB_SHA"' in workflow
    assert "--no-latest" in workflow
    assert "softprops/action-gh-release" not in workflow
    assert "overwrite_files" not in workflow
