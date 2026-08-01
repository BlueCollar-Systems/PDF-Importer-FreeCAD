from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass

import pytest

from PDFVectorImporter.src import PDFImportSession as session


@dataclass
class Options:
    pages: list[int]
    import_mode: str = "auto"
    text_mode: str = "3d_text"
    user_scale: float = 1.0
    page_arrangement: str = "spread"
    page_gap_ratio: float = 0.2
    verbose: bool = True
    import_report_path: str | None = None
    progress_callback: object = None
    auto_resolved_mode: str | None = None
    _runtime_cache: object = None


class FakeObject:
    def __init__(self, document, name: str, type_id: str):
        self.Document = document
        self.Name = name
        self.Label = name
        self.TypeId = type_id
        self.properties = []

    def addProperty(self, kind, name, group):
        self.properties.append((kind, name, group))

    def isDerivedFrom(self, type_id):
        return self.TypeId == type_id


class FakeDocument:
    def __init__(self):
        self.Objects = []

    def addObject(self, _kind, name):
        obj = FakeObject(self, f"{name}_{len(self.Objects)}", _kind)
        self.Objects.append(obj)
        return obj

    def getObject(self, name):
        return next((obj for obj in self.Objects if obj.Name == name), None)


def test_option_identity_is_canonical_and_excludes_runtime_fields():
    first = Options(pages=[3, 1], progress_callback=lambda _event: True)
    second = Options(
        pages=[1, 3],
        verbose=False,
        import_report_path="elsewhere.json",
        progress_callback=object(),
        auto_resolved_mode="hybrid",
        _runtime_cache={"host": "state"},
    )
    assert session.content_options(first) == session.content_options(second)
    assert session.options_sha256(first) == session.options_sha256(second)

    second.user_scale = 2.0
    assert session.options_sha256(first) != session.options_sha256(second)


def test_session_round_trip_and_exact_match_survive_host_property_persistence():
    document = FakeDocument()
    options = Options(pages=[1, 2, 4])
    identity = session.build_identity(
        source_sha256="a" * 64,
        source_name="drawing.pdf",
        opts=options,
        importer_version="4.0.80",
        requested_pages=[1, 2, 4],
    )
    host = session.create_session_object(document, identity)
    page_one = document.addObject("App::DocumentObjectGroup", "PDF_Page_1")
    page_two = document.addObject("App::DocumentObjectGroup", "PDF_Page_2")
    session.update_session_object(
        host,
        status="cancelled",
        completed_pages=[1, 2],
        page_groups={1: page_one.Name, 2: page_two.Name},
    )

    # A reopened FreeCAD document gives us a fresh Python wrapper over the same
    # persisted properties; no transient Python state may be required.
    reopened = FakeDocument()
    reopened.Objects.extend([host, page_one, page_two])
    match = session.find_matching_session(reopened, identity)
    state = session.read_session_object(match)
    assert state["status"] == "cancelled"
    assert state["completed_pages"] == [1, 2]
    assert state["page_groups"] == {"1": page_one.Name, "2": page_two.Name}
    assert session.remaining_pages(state) == [4]


@pytest.mark.parametrize(
    "changed",
    [
        {"source_sha256": "b" * 64},
        {
            "options_json": '{"user_scale":2.0}',
            "options_sha256": hashlib.sha256(b'{"user_scale":2.0}').hexdigest(),
        },
        {"importer_version": "4.0.81"},
        {"requested_pages": [1, 4]},
    ],
)
def test_resume_requires_exact_source_options_package_and_pages(changed):
    document = FakeDocument()
    identity = {
        "schema": session.SESSION_SCHEMA,
        "source_sha256": "a" * 64,
        "source_name": "drawing.pdf",
        "options_json": "{}",
        "options_sha256": hashlib.sha256(b"{}").hexdigest(),
        "importer_version": "4.0.80",
        "requested_pages": [1, 2, 4],
    }
    session.create_session_object(document, identity)
    candidate = dict(identity)
    candidate.update(changed)
    assert session.find_matching_session(document, candidate) is None


def test_invalid_persisted_session_fails_closed():
    document = FakeDocument()
    host = document.addObject("App::FeaturePython", "PDF_Import_Session")
    host.PDFImportSessionSchema = session.SESSION_SCHEMA
    host.PDFSourceSHA256 = "not-a-digest"
    host.PDFOptionsJSON = "{}"
    host.PDFOptionsSHA256 = "a" * 64
    host.PDFImporterVersion = "4.0.80"
    host.PDFRequestedPagesJSON = "[1]"
    host.PDFCompletedPagesJSON = "[]"
    host.PDFPageGroupsJSON = "{}"
    host.PDFImportStatus = "cancelled"
    with pytest.raises(ValueError, match="source SHA-256"):
        session.read_session_object(host)


def test_matching_session_requires_each_certified_page_group_to_still_exist():
    document = FakeDocument()
    options = Options(pages=[1, 2])
    identity = session.build_identity(
        source_sha256="a" * 64,
        source_name="drawing.pdf",
        opts=options,
        importer_version="4.0.80",
        requested_pages=[1, 2],
    )
    host = session.create_session_object(document, identity)
    page_one = document.addObject("App::DocumentObjectGroup", "PDF_Page_1")
    session.update_session_object(
        host,
        status="cancelled",
        completed_pages=[1],
        page_groups={1: page_one.Name},
    )
    document.Objects.remove(page_one)

    with pytest.raises(ValueError, match="certified page group"):
        session.validate_completed_page_groups(document, session.read_session_object(host))
    assert session.find_matching_session(document, identity) is None


def test_resumed_page_offsets_derive_from_original_requested_order():
    offsets = session.page_stack_offsets(
        [1, 2, 4],
        {1: 100.0, 2: 200.0, 4: 300.0},
        arrangement="spread",
        gap_ratio=0.2,
    )
    assert offsets == {1: 0.0, 2: -240.0, 4: -600.0}
    assert session.page_stack_offsets(
        [1, 2], {1: 100.0, 2: 200.0}, arrangement="overlay", gap_ratio=0.2
    ) == {1: 0.0, 2: 0.0}


def test_work_plan_uses_transparent_units_and_risk_labels():
    plan = session.build_work_plan(
        [
            {"page_number": 1, "drawing_operations": 20, "text_characters": 4, "image_instances": 1},
            {"page_number": 3, "drawing_operations": 12_000, "text_characters": 500, "image_instances": 0},
        ]
    )
    assert plan["total_units"] == 12_525
    assert plan["pages"][0]["risk"] == "normal"
    assert plan["pages"][1]["risk"] == "very_high"
    assert json.loads(session.canonical_json(plan))["total_units"] == 12_525
