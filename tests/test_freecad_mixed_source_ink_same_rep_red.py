from __future__ import annotations

import copy
import hashlib
import re
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "PDFVectorImporter" / "src"
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
TESTS_DIR = Path(__file__).resolve().parent
for path in (str(REPO_ROOT), str(SRC_DIR), str(MOD_ROOT), str(TESTS_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

import PDFImporterCore as core  # noqa: E402
from pdfcadcore import import_report as report_contract  # noqa: E402
import test_freecad_production_contract as production_fixtures  # noqa: E402
import test_freecad_representation_contract as representation_fixtures  # noqa: E402
import test_freecad_source_ink_contract as source_ink_fixtures  # noqa: E402


STRUCTURAL_MODES = ("text", "labels", "3d_text", "glyphs", "geometry")


@pytest.mark.parametrize("requested_mode", STRUCTURAL_MODES)
def test_mixed_source_ink_builds_deterministic_same_representation_segments(
    tmp_path: Path,
    requested_mode: str,
) -> None:
    """A mixed span is one obligation with same-rung visible/empty children."""

    item = source_ink_fixtures._bound_pdf_item(
        tmp_path,
        requested_mode,
        "mixed_glyphs",
    )

    first = core._build_source_ink_segments(item)
    second = core._build_source_ink_segments(copy.deepcopy(item))

    assert first == second
    assert first["schema"] == "bcs.freecad_text_source_segments/1.0"
    assert first["parent_source_item_id"] == item["source_item_id"]
    assert first["requested_type"] == requested_mode
    assert (
        first["parent_source_ink_evidence_sha256"]
        == item["source_ink_evidence"]["evidence_sha256"]
    )
    assert first["source_text"] == item["text"] == "AB"
    assert first["source_text_sha256"] == hashlib.sha256(b"AB").hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", first["manifest_sha256"])

    segments = first["segments"]
    assert [segment["child_source_item_id"] for segment in segments] == [
        item["source_item_id"] + ":seg0",
        item["source_item_id"] + ":seg1",
    ]
    assert [segment["requested_type"] for segment in segments] == [
        requested_mode,
        requested_mode,
    ]
    assert [segment["source_index_start"] for segment in segments] == [0, 1]
    assert [segment["source_index_end"] for segment in segments] == [1, 2]
    assert [segment["source_text"] for segment in segments] == ["A", "B"]
    assert [segment["physical_role"] for segment in segments] == [
        "zero_visible_ink",
        "visible",
    ]
    assert "".join(segment["source_text"] for segment in segments) == item["text"]
    assert all(segment["parent_source_item_id"] == item["source_item_id"] for segment in segments)


def _obsolete_mixed_ink_proof(item: dict, attempted_type: str) -> dict:
    evidence = item["source_ink_evidence"]
    zero_indexes = [
        index
        for index, record in enumerate(evidence["characters"])
        if record["zero_visible_ink"]
    ]
    visible_indexes = [
        index
        for index, record in enumerate(evidence["characters"])
        if not record["zero_visible_ink"]
    ]
    reason = "mixed_source_ink_not_exactly_representable"
    return {
        "item_specific_proven_impossible": True,
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": item["pdf_sha256"],
        "page_number": item["page_number"],
        "source_item_id": item["source_item_id"],
        "requested_type": item["requested_type"],
        "attempted_type": attempted_type,
        "reason_code": reason,
        "font_identity": copy.deepcopy(item["font_identity"]),
        "source_ink_evidence_sha256": evidence["evidence_sha256"],
        "evidence": {
            "classification": "mixed_visible_and_zero_ink",
            "source_text_sha256": evidence["source_text_sha256"],
            "source_ink_evidence_sha256": evidence["evidence_sha256"],
            "zero_character_indexes": zero_indexes,
            "visible_character_indexes": visible_indexes,
        },
        "attempted_source_results": [
            {
                "source": "physical_source_ink_evidence",
                "outcome": "proven_impossible",
                "reason_code": reason,
                "pdf_sha256": item["pdf_sha256"],
                "page_number": item["page_number"],
                "source_item_id": item["source_item_id"],
                "source_ink_evidence_sha256": evidence["evidence_sha256"],
            }
        ],
        "attempted_sources_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }


def test_obsolete_mixed_source_ink_proof_cannot_authorize_fallback(
    tmp_path: Path,
) -> None:
    item = source_ink_fixtures._bound_pdf_item(
        tmp_path,
        "3d_text",
        "mixed_glyphs",
    )
    proof = _obsolete_mixed_ink_proof(item, "3d_text")

    with pytest.raises(ValueError):
        core._validate_item_impossibility_proof(
            item,
            "3d_text",
            "3d_text",
            proof,
        )


@pytest.mark.parametrize("requested_mode", ("text", "labels"))
def test_mixed_native_item_delivers_contiguous_children_on_one_parent_obligation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    requested_mode: str,
) -> None:
    document, group = production_fixtures._install_native_host(monkeypatch)
    item = source_ink_fixtures._bound_pdf_item(
        tmp_path,
        requested_mode,
        "mixed_glyphs",
    )

    result = core._deliver_text_item_native(
        item,
        requested_mode,
        core.ImportOptions(text_mode=requested_mode, import_text=True),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["source_item_id"] == item["source_item_id"]
    assert result["requested_type"] == requested_mode
    assert result["attempted_type"] == requested_mode
    assert result["final_type"] == requested_mode
    assert result["outcome"] == "verified"
    assert result["evidence"]["source_segment_manifest"]["manifest_sha256"]
    assert result["evidence"]["child_source_item_ids"] == [
        item["source_item_id"] + ":seg0",
        item["source_item_id"] + ":seg1",
    ]
    assert [host.PDFSourceItemId for host in document.Objects] == result["evidence"][
        "child_source_item_ids"
    ]
    assert all(
        host.PDFParentSourceItemId == item["source_item_id"]
        and host.PDFRepresentation == requested_mode
        for host in document.Objects
    )
    assert [host.PDFTextVisibility for host in document.Objects] == [False, True]
    assert [host.ViewObject.Visibility for host in document.Objects] == [False, True]
    assert result["delivery_count"] == 2


def test_mixed_native_segment_delivery_binds_to_persisted_report_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document, group = production_fixtures._install_native_host(monkeypatch)
    item = source_ink_fixtures._bound_pdf_item(tmp_path, "text", "mixed_glyphs")
    opts = core.ImportOptions(text_mode="text", import_text=True)
    result = core._deliver_text_item_native(
        item,
        "text",
        opts,
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )
    opts.text_delivery_obligation_source_item_ids.append(item["source_item_id"])
    opts.text_delivery_attempts.append(result)

    delivery = core._public_report_value(
        core._build_text_representation_delivery(opts, opts.text_delivery_attempts)
    )
    inventory = core._public_report_value(
        core._build_host_object_inventory(document.Objects)
    )

    assert delivery["verified"] is True, delivery
    assert report_contract._freecad_delivery_inventory_binding_verified(
        delivery,
        core._public_report_value(opts.text_delivery_attempts),
        inventory,
        item["pdf_sha256"],
    ) is True


def test_mixed_3d_text_keeps_zero_ink_empty_while_visible_child_uses_substitute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    windows_fonts = tmp_path / "Fonts"
    windows_fonts.mkdir()
    covering_font = windows_fonts / "covers_ab.ttf"
    covering_font.write_bytes(
        source_ink_fixtures._test_font_bytes(empty_a=False, visible_b=True)
    )
    monkeypatch.setenv("WINDIR", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    document, draft, group = representation_fixtures._install_host(monkeypatch)

    class EmptyShape:
        Vertexes = []
        Edges = []
        Faces = []
        Solids = []
        Volume = 0.0

        @staticmethod
        def isNull():
            return True

    class EmptyHost(representation_fixtures.FakeHostObject):
        def __init__(self, name: str):
            self.Document = document
            self._set_host_name(name)
            self.Label = name
            self.TypeId = "Part::Feature"
            self.ViewObject = representation_fixtures.AttrSink()
            self.PropertiesList = []

        def addProperty(self, _kind, name, _group):
            if name not in self.PropertiesList:
                self.PropertiesList.append(name)

    original_add_object = document.addObject

    def add_object(kind, name):
        if kind == "Part::Feature":
            host = EmptyHost("%s_%d" % (name, len(document.Objects)))
            document.Objects.append(host)
            return host
        return original_add_object(kind, name)

    monkeypatch.setattr(document, "addObject", add_object)
    monkeypatch.setattr(core, "Part", type("Part", (), {"Shape": EmptyShape}))

    item = source_ink_fixtures._bound_pdf_item(tmp_path, "3d_text", "mixed_glyphs")
    misses = [
        {
            "source": source,
            "outcome": "not_found",
            "font_identity": dict(item["font_identity"]),
            "pdf_sha256": item["pdf_sha256"],
            "page_number": item["page_number"],
            "staging_complete": True,
        }
        for source in ("embedded_font", "system_font")
    ]
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: (None, misses),
    )

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        core.ImportOptions(text_mode="3d_text", import_text=True),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["source_item_id"] == item["source_item_id"]
    assert result["final_type"] == "3d_text"
    assert result["evidence"]["child_source_item_ids"] == [
        item["source_item_id"] + ":seg0",
        item["source_item_id"] + ":seg1",
    ]
    assert draft.calls[0][0] == "B"
    assert Path(draft.calls[0][1]) != covering_font
    visible_delivery = result["evidence"]["segment_deliveries"][1]["evidence"]
    assert visible_delivery["source_font_asset_path"] == str(covering_font.resolve())
    assert all(
        host.PDFParentSourceItemId == item["source_item_id"]
        and host.PDFRepresentation == "3d_text"
        for host in document.Objects
    )
    zero_host = document.getObject(result["delivery_entity_ids"][0])
    assert zero_host.PDFSourceItemId.endswith(":seg0")
    assert zero_host.PDFTextVisibility is False
    assert zero_host.Shape.isNull() is True
    assert result["delivery_count"] == 2
