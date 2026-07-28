from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MOD_ROOT = REPO_ROOT / "PDFVectorImporter"
SRC_DIR = MOD_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"
for path in (MOD_ROOT, SRC_DIR, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import PDFImporterCore as core  # noqa: E402
from pdfcadcore import import_report as report_contract  # noqa: E402
import test_freecad_representation_contract as representation_fixtures  # noqa: E402
import test_freecad_source_ink_contract as source_ink_fixtures  # noqa: E402


_BOUND_FIELDS = (
    "source_font_equivalence",
    "font_substitution_applied",
    "font_identity_verified",
    "glyph_coverage_verified",
    "delivered_font_sha256",
    "font_candidate_source",
    "source_font_asset_path",
    "source_font_asset_sha256",
    "staged_font_path",
    "staged_font_sha256",
    "staged_asset_verified",
    "staged_asset_read_only",
    "font_internal_identity_sha256",
    "font_coverage_evidence_sha256",
)
_INVENTORY_ONLY_FIELDS = (
    "delivered_font_path",
    "source_font_identity_json",
    "staged_asset_actual_sha256",
    "staged_asset_digest_matches",
)


def _font_delivery_records(tmp_path: Path) -> tuple[dict, dict, Path]:
    payload = source_ink_fixtures._test_font_bytes(empty_a=False)
    digest = hashlib.sha256(payload).hexdigest()
    source_path = tmp_path / "installed-font.ttf"
    source_path.write_bytes(payload)
    stage_root = tmp_path / "delivery-assets"
    stage_root.mkdir()
    staged_path = stage_root / (digest + ".ttf")
    staged_path.write_bytes(payload)
    os.chmod(staged_path, stat.S_IREAD)
    source_font_identity = {
        "raw_name": "MissingSourceFont-Regular",
        "normalized_key": "missingsourcefontregular",
    }
    internal_identity = core._font_internal_identity_evidence(
        payload,
        source_font_identity,
    )
    coverage_evidence = core._font_bytes_glyph_coverage(payload, "A")
    terminal = {
        "font_path": str(staged_path.resolve()),
        "source_text": "A",
        "source_font_identity": source_font_identity,
        "source_font_equivalence": False,
        "font_substitution_applied": True,
        "font_identity_verified": False,
        "glyph_coverage_verified": True,
        "delivered_font_sha256": digest,
        "font_candidate_source": "installed_no_cost_substitute",
        "source_font_asset_path": str(source_path.resolve()),
        "source_font_asset_sha256": digest,
        "staged_font_path": str(staged_path.resolve()),
        "staged_font_sha256": digest,
        "staged_asset_verified": True,
        "staged_asset_read_only": True,
        "font_internal_identity_evidence": internal_identity,
        "font_internal_identity_sha256": internal_identity["evidence_sha256"],
        "font_coverage_evidence": coverage_evidence,
        "font_coverage_evidence_sha256": coverage_evidence[
            "coverage_evidence_sha256"
        ],
    }
    content = {
        "font_delivery": {
            field: terminal[field]
            for field in _BOUND_FIELDS
        }
    }
    content["font_delivery"]["delivered_font_path"] = terminal["font_path"]
    content["font_delivery"]["source_font_identity_json"] = json.dumps(
        terminal["source_font_identity"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    content["font_delivery"]["staged_asset_actual_sha256"] = digest
    content["font_delivery"]["staged_asset_digest_matches"] = True
    return terminal, content, staged_path


def test_visible_3d_font_delivery_binds_complete_terminal_and_inventory_metadata(
    tmp_path: Path,
) -> None:
    terminal, content, _staged_path = _font_delivery_records(tmp_path)

    assert report_contract._freecad_font_delivery_inventory_binding_verified(
        terminal,
        content,
    ) is True


@pytest.mark.parametrize("location", ["terminal", "inventory"])
@pytest.mark.parametrize("field", _BOUND_FIELDS)
def test_visible_3d_font_delivery_rejects_every_omitted_bound_field(
    tmp_path: Path,
    location: str,
    field: str,
) -> None:
    terminal, content, _staged_path = _font_delivery_records(tmp_path)
    if location == "terminal":
        terminal.pop(field)
    else:
        content["font_delivery"].pop(field)

    assert report_contract._freecad_font_delivery_inventory_binding_verified(
        terminal,
        content,
    ) is False


@pytest.mark.parametrize("field", _INVENTORY_ONLY_FIELDS)
def test_visible_3d_font_delivery_rejects_every_omitted_inventory_proof_field(
    tmp_path: Path,
    field: str,
) -> None:
    terminal, content, _staged_path = _font_delivery_records(tmp_path)
    content["font_delivery"].pop(field)

    assert report_contract._freecad_font_delivery_inventory_binding_verified(
        terminal,
        content,
    ) is False


def test_visible_3d_font_delivery_rejects_inventory_metadata_mutation(
    tmp_path: Path,
) -> None:
    terminal, content, _staged_path = _font_delivery_records(tmp_path)
    content["font_delivery"]["font_coverage_evidence_sha256"] = "d" * 64

    assert report_contract._freecad_font_delivery_inventory_binding_verified(
        terminal,
        content,
    ) is False


def test_visible_3d_font_delivery_rejects_opaque_forged_proof_digests(
    tmp_path: Path,
) -> None:
    terminal, content, _staged_path = _font_delivery_records(tmp_path)
    terminal["font_internal_identity_sha256"] = "b" * 64
    terminal["font_coverage_evidence_sha256"] = "c" * 64
    content["font_delivery"]["font_internal_identity_sha256"] = "b" * 64
    content["font_delivery"]["font_coverage_evidence_sha256"] = "c" * 64

    assert report_contract._freecad_font_delivery_inventory_binding_verified(
        terminal,
        content,
    ) is False


def test_visible_3d_font_delivery_rejects_self_consistent_forged_source_identity(
    tmp_path: Path,
) -> None:
    terminal, content, _staged_path = _font_delivery_records(tmp_path)
    content["font_delivery"]["source_font_identity_json"] = (
        '{"normalized_key":"forged","raw_name":"Forged"}'
    )

    assert report_contract._freecad_font_delivery_inventory_binding_verified(
        terminal,
        content,
    ) is False


def test_visible_3d_font_delivery_rejects_staged_asset_mutation(
    tmp_path: Path,
) -> None:
    terminal, content, staged_path = _font_delivery_records(tmp_path)
    os.chmod(staged_path, stat.S_IWRITE | stat.S_IREAD)
    staged_path.write_bytes(staged_path.read_bytes() + b"mutated")
    content = copy.deepcopy(content)

    assert report_contract._freecad_font_delivery_inventory_binding_verified(
        terminal,
        content,
    ) is False


def test_mixed_3d_visible_and_zero_ink_children_bind_report_inventory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    windows_fonts = tmp_path / "Fonts"
    windows_fonts.mkdir()
    (windows_fonts / "covers_ab.ttf").write_bytes(
        source_ink_fixtures._test_font_bytes(empty_a=False, visible_b=True)
    )
    monkeypatch.setenv("WINDIR", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    document, _draft, group = representation_fixtures._install_host(monkeypatch)

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
    item = source_ink_fixtures._bound_pdf_item(
        tmp_path,
        "3d_text",
        "mixed_glyphs",
    )
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
    opts = core.ImportOptions(text_mode="3d_text", import_text=True)
    result = core._deliver_text_item_3d(
        item,
        "3d_text",
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

    assert report_contract._freecad_delivery_inventory_binding_verified(
        delivery,
        core._public_report_value(opts.text_delivery_attempts),
        inventory,
        item["pdf_sha256"],
    ) is True
