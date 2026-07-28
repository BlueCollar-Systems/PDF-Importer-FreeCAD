from __future__ import annotations

import inspect
import json
import pickle
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "PDFVectorImporter"))

from pdfcadcore import page_visual as pv  # noqa: E402


try:
    import fitz as _REAL_FITZ
except ImportError:
    _REAL_FITZ = None

IMPORTER = "tests/page-visual-v2-hardening"


def _require_real_fitz():
    if _REAL_FITZ is None:
        pytest.skip("real PyMuPDF integration dependency is unavailable")
    return _REAL_FITZ


def _pdf_bytes(*, text: bool = False, link: bool = False, annotation: bool = False) -> bytes:
    fitz = _require_real_fitz()
    document = fitz.open()
    try:
        page = document.new_page(width=200, height=100)
        if text:
            page.insert_text((20, 30), "SOURCE TEXT")
        if link:
            page.insert_link(
                {
                    "kind": fitz.LINK_URI,
                    "from": fitz.Rect(20, 35, 100, 50),
                    "uri": "https://example.com",
                }
            )
        if annotation:
            page.add_text_annot((120, 30), "review note")
        return document.tobytes(garbage=4, deflate=True)
    finally:
        document.close()


def _image_mask_pdf_bytes() -> bytes:
    content = b"q 20 0 0 20 10 10 cm /Im1 Do Q\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] "
            b"/Resources << /XObject << /Im1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"endstream",
        (
            b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 "
            b"/ImageMask true /BitsPerComponent 1 /Decode [0 1] /Length 1 >>\n"
            b"stream\n\x80\nendstream"
        ),
    ]
    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _authority(payload: bytes):
    return pv.capture_fresh_page_visual_authority(
        payload,
        importer_identity=IMPORTER,
        page_scope_ids={1: "page_visual:1"},
    )


class _FakeGeometry(tuple):
    def __new__(cls, *values):
        if len(values) == 1 and type(values[0]) in {list, tuple}:
            values = tuple(values[0])
        return super().__new__(cls, values)


class _FakeRect(_FakeGeometry):
    pass


class _FakePoint(_FakeGeometry):
    pass


class _FakeMatrix(_FakeGeometry):
    pass


class _FakeQuad(_FakeGeometry):
    pass


class _FakeObject:
    def __init__(self, xref: int) -> None:
        self.xref = xref
        self.type = (0, "Text")
        self.rect = _FakeRect(1, 2, 3, 4)
        self.info = {"content": "note"}
        self.field_name = "field"
        self.field_type = 1
        self.field_value = "value"


class _FakePage:
    rotation = 0

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.xref = 0 if "primary_failure" in mode else 4
        self.mediabox = _FakeRect(0, 0, 200, 100)
        self.cropbox = _FakeRect(0, 0, 200, 100)
        self.rect = _FakeRect(0, 0, 200, 100)
        self.calls = {"text": 0, "images": 0, "bboxlog": 0}

    def get_text(self, _kind: str, *, sort: bool):
        assert sort is False
        self.calls["text"] += 1
        if self.mode == "text_typeerror_once" and self.calls["text"] == 1:
            raise TypeError("known API rejected")
        blocks = [{"type": 0, "lines": []}] if "text" in self.mode else []
        return {"width": 200.0, "height": 100.0, "blocks": blocks}

    def get_drawings(self):
        return []

    def get_image_info(self, *, hashes: bool, xrefs: bool):
        assert hashes is True and xrefs is True
        self.calls["images"] += 1
        if self.mode == "images_typeerror_once" and self.calls["images"] == 1:
            raise TypeError("known API rejected")
        return []

    def get_bboxlog(self, *, layers: bool):
        assert layers is True
        self.calls["bboxlog"] += 1
        if self.mode == "bboxlog_typeerror_once" and self.calls["bboxlog"] == 1:
            raise TypeError("known API rejected")
        entries = []
        if "text" in self.mode:
            entries.append(("fill-text", (10.0, 10.0, 20.0, 20.0), ""))
        if "shade" in self.mode:
            entries.append(("fill-shade", (0.0, 0.0, 50.0, 50.0), ""))
        return entries

    def get_contents(self):
        return []

    def read_contents(self):
        return b""

    def annots(self):
        return iter([_FakeObject(7)]) if "annotation" in self.mode else None

    def widgets(self):
        return None

    def get_links(self):
        return (
            [{"kind": 2, "xref": 8, "uri": "https://example.com"}]
            if "link" in self.mode
            else []
        )

    def annot_xrefs(self):
        if "annotation" in self.mode:
            return [(7, 0, "annot"), (9, 16, "popup")]
        if "link" in self.mode:
            return [(8, 1, "link")]
        if "unclassified" in self.mode:
            return [(10, 99, "future")]
        return []


class _FakeDocument:
    page_count = 1
    needs_pass = False
    is_encrypted = False

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.page = _FakePage(mode)
        self.close_calls = 0

    def __getitem__(self, index: int):
        assert index == 0
        return self.page

    def xref_stream_raw(self, _xref: int):
        return b""

    def xref_stream(self, _xref: int):
        return b""

    def close(self) -> None:
        self.close_calls += 1
        if "close_failure" in self.mode:
            raise RuntimeError("close failure")


class _FakeFitz:
    Rect = _FakeRect
    Point = _FakePoint
    Matrix = _FakeMatrix
    Quad = _FakeQuad

    def __init__(self, mode: str = "blank", version: object = "1.28.0") -> None:
        self.mode = mode
        self.VersionBind = version
        self.open_calls = 0
        self.document = _FakeDocument(mode)

    def open(self, *args, **kwargs):
        self.open_calls += 1
        if self.mode == "open_typeerror_once" and self.open_calls == 1:
            raise TypeError("known API rejected")
        assert not args
        assert kwargs["filetype"] == "pdf"
        assert isinstance(kwargs["stream"], bytes)
        return self.document


def _capture_fake(monkeypatch: pytest.MonkeyPatch, module: _FakeFitz):
    monkeypatch.setattr(
        pv,
        "_load_page_visual_fitz",
        lambda: module,
        raising=False,
    )
    return pv.capture_fresh_page_visual_authority(
        b"%PDF-fake-v2-hardening",
        importer_identity=IMPORTER,
        page_scope_ids={1: "page_visual:1"},
    )


def test_capture_public_api_has_no_extractor_injection() -> None:
    assert "fitz_module" not in inspect.signature(
        pv.capture_fresh_page_visual_authority
    ).parameters


def test_real_capture_uses_the_pinned_production_loader() -> None:
    authority = _authority(_pdf_bytes())
    version = authority.observation(1)["extractor"]["version"]

    assert tuple(map(int, version.split("."))) >= (1, 24, 0)
    assert tuple(map(int, version.split("."))) < (2, 0, 0)


@pytest.mark.parametrize(
    "version",
    ["1.23.9", "2.0.0", "1.24", " 1.28.0", 1.28],
)
def test_capture_rejects_nonexact_or_unsupported_pymupdf_versions(
    monkeypatch: pytest.MonkeyPatch,
    version: object,
) -> None:
    with pytest.raises(ValueError, match="VersionBind"):
        _capture_fake(monkeypatch, _FakeFitz(version=version))


def test_capture_accepts_four_component_numeric_versionbind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz(version="1.27.2.3"))

    assert authority.observation(1)["extractor"]["version"] == "1.27.2.3"


def test_capture_rejects_missing_required_pymupdf_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _FakeFitz()
    module.Quad = None

    with pytest.raises(ValueError, match="required PyMuPDF API"):
        _capture_fake(monkeypatch, module)


def test_authority_retains_immutable_source_snapshot_across_path_mutation(
    tmp_path: Path,
) -> None:
    first_payload = _pdf_bytes()
    second_payload = _pdf_bytes(text=True)
    source = tmp_path / "source.pdf"
    source.write_bytes(first_payload)
    authority = pv.capture_fresh_page_visual_authority(
        source,
        importer_identity=IMPORTER,
        page_scope_ids={1: "page_visual:1"},
    )

    snapshot = authority.source_bytes_snapshot()
    source.write_bytes(second_payload)
    replacement = pv.capture_fresh_page_visual_authority(
        source,
        importer_identity=IMPORTER,
        page_scope_ids={1: "page_visual:1"},
    )

    assert type(snapshot) is bytes
    assert snapshot == first_payload
    assert authority.source_bytes_snapshot() == first_payload
    assert authority.pdf_sha256 != replacement.pdf_sha256
    assert replacement.source_bytes_snapshot() == second_payload
    with pytest.raises(TypeError):
        snapshot[0] = 0  # type: ignore[index]
    serialized = json.dumps(
        {"observations": authority.observations(), "anchor": authority.manifest()}
    )
    assert first_payload.hex() not in serialized


def test_authority_capability_is_runtime_only_and_not_pickle_serializable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz())

    with pytest.raises(TypeError, match="runtime-only"):
        pickle.dumps(authority)


def test_primary_capture_error_survives_a_secondary_close_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _FakeFitz("primary_failure_close_failure")

    with pytest.raises(ValueError, match="page xref"):
        _capture_fake(monkeypatch, module)
    assert module.document.close_calls == 1


def test_successful_capture_fails_when_document_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _FakeFitz("close_failure")

    with pytest.raises(RuntimeError, match="close failure"):
        _capture_fake(monkeypatch, module)
    assert module.document.close_calls == 1


@pytest.mark.parametrize(
    "value",
    [
        {1, 2},
        (item for item in [1, 2]),
        type("FauxRect", (), {"x0": 0, "y0": 0, "x1": 1, "y1": 1})(),
    ],
)
def test_source_normalization_rejects_arbitrary_iterables_and_duck_types(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="unsupported value"):
        pv._plain_source_value(value, pymupdf_module=_FakeFitz())


def test_source_normalization_accepts_only_explicit_pymupdf_geometry_types() -> None:
    fitz = _require_real_fitz()
    values = [
        fitz.Rect(1, 2, 3, 4),
        fitz.Point(1, 2),
        fitz.Matrix(1, 2, 3, 4, 5, 6),
        fitz.Quad((0, 0), (1, 0), (0, 1), (1, 1)),
    ]

    normalized = [
        pv._plain_source_value(value, pymupdf_module=fitz) for value in values
    ]

    assert all(type(value) is list for value in normalized)


@pytest.mark.parametrize(
    ("mode", "channel", "counter"),
    [
        ("text_typeerror_once", "raw_text", "text"),
        ("images_typeerror_once", "images", "images"),
        ("bboxlog_typeerror_once", "display_list", "bboxlog"),
    ],
)
def test_known_pymupdf_channel_calls_do_not_retry_typeerror(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    channel: str,
    counter: str,
) -> None:
    module = _FakeFitz(mode)
    authority = _capture_fake(monkeypatch, module)

    assert authority.observation(1)[channel]["status"] == "error"
    assert module.document.page.calls[counter] == 1


def test_known_pymupdf_open_call_does_not_retry_typeerror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _FakeFitz("open_typeerror_once")

    with pytest.raises(TypeError, match="known API rejected"):
        _capture_fake(monkeypatch, module)
    assert module.open_calls == 1


@pytest.mark.parametrize(
    "requested_type",
    ["label", "Labels", "3d-text", "raw geometry", "raster", "unknown"],
)
def test_v2_proof_builder_rejects_noncanonical_requested_representation(
    monkeypatch: pytest.MonkeyPatch,
    requested_type: str,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz())
    observation = authority.observation(1)

    with pytest.raises(ValueError, match="requested_type"):
        pv.build_page_visual_fallback_proof_v2(
            observation=observation,
            authority=authority,
            requested_type=requested_type,
            attempted_type="labels",
        )


@pytest.mark.parametrize(
    "attempted_type",
    ["label", "Labels", "3D TEXT", "glyph", "raster", "unknown"],
)
def test_v2_proof_builder_rejects_noncanonical_or_terminal_attempt(
    monkeypatch: pytest.MonkeyPatch,
    attempted_type: str,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz())
    observation = authority.observation(1)

    with pytest.raises(ValueError, match="attempted_type"):
        pv.build_page_visual_fallback_proof_v2(
            observation=observation,
            authority=authority,
            requested_type="labels",
            attempted_type=attempted_type,
        )


@pytest.mark.parametrize(
    "requested_type",
    ["labels", "text", "3d_text", "glyphs", "geometry"],
)
def test_v2_proof_builder_accepts_only_finite_canonical_requested_enum(
    monkeypatch: pytest.MonkeyPatch,
    requested_type: str,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz())
    observation = authority.observation(1)

    proof = pv.build_page_visual_fallback_proof_v2(
        observation=observation,
        authority=authority,
        requested_type=requested_type,
        attempted_type="labels",
    )

    assert proof["requested_type"] == requested_type


@pytest.mark.parametrize("wrong_page_number", [True, 1.0])
def test_v2_proof_verifier_requires_exact_integer_page_number(
    monkeypatch: pytest.MonkeyPatch,
    wrong_page_number: object,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz())
    observation = authority.observation(1)
    proof = pv.build_page_visual_fallback_proof_v2(
        observation=observation,
        authority=authority,
        requested_type="labels",
        attempted_type="labels",
    )
    proof["page_number"] = wrong_page_number
    proof["proof_sha256"] = pv.page_visual_fallback_proof_v2_digest(proof)

    assert not pv.page_visual_fallback_proof_v2_verified(
        proof,
        observation=observation,
        authority=authority,
        expected_requested_type="labels",
        expected_attempted_type="labels",
    )


def test_v2_source_proof_builder_rejects_any_mutation_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz())
    observation = authority.observation(1)

    with pytest.raises(ValueError, match="pre-mutation"):
        pv.build_page_visual_fallback_proof_v2(
            observation=observation,
            authority=authority,
            requested_type="labels",
            attempted_type="labels",
            created_entity_ids=["host:entity:1"],
            removed_entity_ids=["host:entity:1"],
        )


def test_v2_source_proof_verifier_rejects_equal_nonempty_mutation_lists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz())
    observation = authority.observation(1)
    proof = pv.build_page_visual_fallback_proof_v2(
        observation=observation,
        authority=authority,
        requested_type="labels",
        attempted_type="labels",
    )
    proof["created_entity_ids"] = ["host:entity:1"]
    proof["removed_entity_ids"] = ["host:entity:1"]
    proof["proof_sha256"] = pv.page_visual_fallback_proof_v2_digest(proof)

    assert not pv.page_visual_fallback_proof_v2_verified(
        proof,
        observation=observation,
        authority=authority,
        expected_requested_type="labels",
        expected_attempted_type="labels",
    )


def test_v2_proof_verifier_rejects_unknown_extra_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz())
    observation = authority.observation(1)
    proof = pv.build_page_visual_fallback_proof_v2(
        observation=observation,
        authority=authority,
        requested_type="labels",
        attempted_type="labels",
    )
    proof["representation_alias"] = "label"
    proof["proof_sha256"] = pv.page_visual_fallback_proof_v2_digest(proof)

    assert not pv.page_visual_fallback_proof_v2_verified(
        proof,
        observation=observation,
        authority=authority,
        expected_requested_type="labels",
        expected_attempted_type="labels",
    )


def test_real_image_mask_is_image_paint_not_unknown_display_content() -> None:
    fitz = _require_real_fitz()
    payload = _image_mask_pdf_bytes()
    document = fitz.open(stream=payload, filetype="pdf")
    try:
        assert document[0].get_bboxlog(layers=True)[0][0] == "fill-imgmask"
    finally:
        document.close()

    authority = _authority(payload)
    observation = authority.observation(1)
    counts = observation["display_list"]["type_counts"]

    assert counts["fill-imgmask"] == 1
    assert counts["unknown"] == 0
    assert observation["classification"] == "image_only"
    assert pv.page_visual_representation_status(observation, "labels") == (
        "proven_impossible"
    )
    assert pv.page_visual_representation_status(observation, "geometry") == (
        "proven_impossible"
    )


@pytest.mark.parametrize(
    ("link", "annotation"),
    [(True, False), (False, True)],
)
def test_real_positive_text_is_reachable_despite_link_or_annotation_metadata(
    link: bool,
    annotation: bool,
) -> None:
    authority = _authority(_pdf_bytes(text=True, link=link, annotation=annotation))
    observation = authority.observation(1)

    assert pv.page_visual_representation_status(observation, "labels") == "reachable"
    assert pv.page_visual_representation_status(observation, "geometry") == "reachable"


@pytest.mark.parametrize(
    ("mode", "text_status", "geometry_status"),
    [
        ("text_link", "reachable", "reachable"),
        ("text_annotation", "reachable", "reachable"),
        ("text_shade", "reachable", "reachable"),
        ("shade_only", "proven_impossible", "reachable"),
        ("link_only", "proven_impossible", "proven_impossible"),
        ("annotation_only", "indeterminate", "indeterminate"),
        ("unclassified_only", "indeterminate", "indeterminate"),
    ],
)
def test_representation_status_uses_positive_evidence_then_relevant_absence(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    text_status: str,
    geometry_status: str,
) -> None:
    authority = _capture_fake(monkeypatch, _FakeFitz(mode))
    observation = authority.observation(1)

    assert pv.page_visual_representation_status(observation, "labels") == text_status
    assert (
        pv.page_visual_representation_status(observation, "geometry")
        == geometry_status
    )


def test_annotation_channel_cross_checks_all_xrefs_and_counts_unclassified_types() -> None:
    authority = _authority(_pdf_bytes(annotation=True))
    observation = authority.observation(1)
    annotations = observation["annotations"]

    assert annotations["annot_xref_count"] >= annotations["annotation_count"]
    assert annotations["unclassified_xref_count"] >= 1

    forged = json.loads(json.dumps(observation))
    forged["annotations"]["annot_xref_count"] = 0
    forged["observation_sha256"] = pv.page_visual_source_observation_v2_digest(
        forged
    )
    assert not pv._page_visual_observation_v2_structure_verified(forged)


def test_links_are_fully_classified_annotation_xrefs_not_absence_blockers() -> None:
    authority = _authority(_pdf_bytes(link=True))
    observation = authority.observation(1)
    annotations = observation["annotations"]

    assert annotations["link_count"] == 1
    assert annotations["annot_xref_count"] == 1
    assert annotations["unclassified_xref_count"] == 0
    assert pv.page_visual_representation_status(observation, "labels") == (
        "proven_impossible"
    )
