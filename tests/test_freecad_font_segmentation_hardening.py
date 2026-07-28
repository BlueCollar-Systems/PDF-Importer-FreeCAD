from __future__ import annotations

import copy
import hashlib
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

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
import test_freecad_representation_contract as representation_fixtures  # noqa: E402
import test_freecad_source_ink_contract as source_ink_fixtures  # noqa: E402


def _font_bytes(*, empty_a: bool, visible_b: bool = False) -> bytes:
    return source_ink_fixtures._test_font_bytes(
        empty_a=empty_a,
        visible_b=visible_b,
    )


def _renamed_font_bytes(
    *,
    family: str,
    style: str,
    empty_a: bool = False,
) -> bytes:
    """Return a tiny real font with controlled paired names and style bits."""

    from fontTools.ttLib import TTFont

    font = TTFont(io.BytesIO(_font_bytes(empty_a=empty_a)))
    try:
        full_name = family if style.casefold() == "regular" else f"{family} {style}"
        ps_family = "".join(character for character in family if character.isalnum())
        postscript_name = f"{ps_family}-{style}"
        replacements = {
            1: family,
            2: style,
            4: full_name,
            6: postscript_name,
            16: family,
            17: style,
        }
        name_table = font["name"]
        for record in name_table.names:
            if record.nameID in replacements:
                name_table.setName(
                    replacements[record.nameID],
                    record.nameID,
                    record.platformID,
                    record.platEncID,
                    record.langID,
                )
        is_bold = style.casefold() in {"bold", "bold italic"}
        is_italic = style.casefold() in {"italic", "bold italic"}
        font["OS/2"].fsSelection = (
            (0x20 if is_bold else 0)
            | (0x01 if is_italic else 0)
            | (0 if is_bold or is_italic else 0x40)
        )
        font["head"].macStyle = (
            (0x01 if is_bold else 0) | (0x02 if is_italic else 0)
        )
        output = io.BytesIO()
        font.save(output)
        return output.getvalue()
    finally:
        font.close()


def _exact_results(item: dict, font_path: Path) -> tuple[str, list[dict]]:
    payload = font_path.read_bytes()
    return str(font_path), [
        {
            "source": "embedded_font",
            "outcome": "found",
            "font_identity": copy.deepcopy(item["font_identity"]),
            "path": str(font_path),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "pdf_sha256": item["pdf_sha256"],
            "page_number": item["page_number"],
            "staging_complete": True,
            "approved_font_root": str(font_path.parent.resolve()),
        }
    ]


def _font_misses(item: dict) -> tuple[None, list[dict]]:
    return None, [
        {
            "source": source,
            "outcome": "not_found",
            "font_identity": copy.deepcopy(item["font_identity"]),
            "pdf_sha256": item["pdf_sha256"],
            "page_number": item["page_number"],
            "staging_complete": True,
        }
        for source in ("embedded_font", "system_font")
    ]


def test_visible_cmap_entry_without_renderable_outline_is_not_coverage() -> None:
    result = core._font_bytes_glyph_coverage(_font_bytes(empty_a=True), "A")

    assert result["glyph_coverage_verified"] is False
    assert result["required_codepoints"] == [ord("A")]
    assert result["empty_outline_codepoints"] == [ord("A")]
    assert result["nonempty_outline_codepoints"] == []


def test_exact_empty_outline_font_continues_to_covering_same_rung_substitute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exact_font = tmp_path / "exact-empty.ttf"
    exact_font.write_bytes(_font_bytes(empty_a=True))
    candidate_root = tmp_path / "approved-fonts"
    candidate_root.mkdir()
    covering_font = candidate_root / "covering.ttf"
    covering_font.write_bytes(_font_bytes(empty_a=False))
    stage_root = tmp_path / "attempt-fonts"

    document, draft, group = representation_fixtures._install_host(monkeypatch)
    item = representation_fixtures._canonical_3d_item(
        "A",
        font="PhysicalInkTest-Regular",
    )
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _exact_results(item, exact_font),
    )
    monkeypatch.setattr(
        core,
        "_no_cost_font_candidate_roots",
        lambda: [("installed_no_cost_substitute", candidate_root)],
    )
    monkeypatch.setattr(core, "_shapestring_font_cache_dir", lambda: stage_root)

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        core.ImportOptions(text_mode="3d_text"),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    evidence = result["evidence"]
    staged_path = Path(draft.calls[0][1])
    assert result["final_type"] == "3d_text"
    assert evidence["font_substitution_applied"] is True
    assert evidence["source_font_equivalence"] is False
    assert evidence["source_font_asset_path"] == str(covering_font.resolve())
    assert staged_path != covering_font
    assert staged_path.parent == (stage_root / "delivery-assets").resolve()
    assert staged_path.read_bytes() == covering_font.read_bytes()
    assert evidence["staged_font_path"] == str(staged_path)
    assert evidence["staged_font_sha256"] == hashlib.sha256(
        staged_path.read_bytes()
    ).hexdigest()
    assert evidence["staged_asset_verified"] is True
    assert evidence["staged_asset_read_only"] is True
    assert document.Objects


def test_system_filename_is_not_exact_equivalence_without_internal_name_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fonts = tmp_path / "Fonts"
    fonts.mkdir()
    # The filename looks like Arial, but the internal family/style is deliberately
    # Physical Ink Test Regular.
    (fonts / "arial.ttf").write_bytes(_font_bytes(empty_a=False))
    monkeypatch.setenv("WINDIR", str(tmp_path))

    resolved, results = core._resolve_shapestring_font_path_with_evidence(
        "ArialMT",
        core.ImportOptions(),
        _allow_unbound_compat=True,
    )

    assert resolved is None
    assert results[-1]["source"] == "system_font"
    assert results[-1]["outcome"] == "invalid"
    assert results[-1]["reason"] == "system_font_internal_identity_mismatch"
    assert results[-1]["font_identity_verified"] is False
    assert results[-1]["font_sha256"] == hashlib.sha256(
        (fonts / "arial.ttf").read_bytes()
    ).hexdigest()


def test_subset_prefix_normalization_requires_six_ascii_uppercase_letters() -> None:
    assert core._canonical_font_identity(
        "ABCDEF+PhysicalInkTest-Regular"
    )["normalized_key"] == "physicalinktestregular"
    assert core._canonical_font_identity(
        "A1BCDE+PhysicalInkTest-Regular"
    )["normalized_key"] == "a1bcdephysicalinktestregular"


def test_internal_identity_does_not_treat_bare_family_as_regular_or_accept_bold() -> None:
    bold_payload = _renamed_font_bytes(family="Physical Ink Test", style="Bold")

    evidence = core._font_internal_identity_evidence(
        bold_payload,
        core._canonical_font_identity("Physical Ink Test"),
    )

    assert evidence["font_identity_verified"] is False
    assert evidence["source_style_explicit"] is False
    assert evidence["os2_fs_selection"] & 0x20
    assert evidence["head_mac_style"] & 0x01


def test_embedded_source_label_does_not_bypass_internal_family_style_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    exact_font = tmp_path / "mislabeled-exact.ttf"
    exact_font.write_bytes(
        _renamed_font_bytes(family="Different Embedded Family", style="Regular")
    )
    candidate_root = tmp_path / "approved-fonts"
    candidate_root.mkdir()
    candidate = candidate_root / "covering.ttf"
    candidate.write_bytes(_font_bytes(empty_a=False))
    stage_root = tmp_path / "attempt-fonts"
    _document, draft, group = representation_fixtures._install_host(monkeypatch)
    item = representation_fixtures._canonical_3d_item(
        "A",
        font="PhysicalInkTest-Regular",
    )
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _exact_results(item, exact_font),
    )
    monkeypatch.setattr(
        core,
        "_no_cost_font_candidate_roots",
        lambda: [("installed_no_cost_substitute", candidate_root)],
    )
    monkeypatch.setattr(core, "_shapestring_font_cache_dir", lambda: stage_root)

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        core.ImportOptions(text_mode="3d_text"),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["final_type"] == "3d_text"
    assert result["evidence"]["source_font_equivalence"] is False
    assert result["evidence"]["font_substitution_applied"] is True
    assert result["evidence"]["source_font_asset_path"] == str(candidate.resolve())
    assert Path(draft.calls[0][1]).read_bytes() == candidate.read_bytes()


def test_substitute_scan_never_accepts_candidate_escaped_from_approved_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    approved_root = tmp_path / "approved"
    approved_root.mkdir()
    outside = tmp_path / "outside.ttf"
    outside.write_bytes(_font_bytes(empty_a=False))
    monkeypatch.setattr(
        core,
        "_no_cost_font_candidate_roots",
        lambda: [("installed_no_cost_substitute", approved_root)],
    )
    with pytest.raises(ValueError, match="escaped"):
        core._read_stable_font_asset(outside, approved_root=approved_root)

    # A monkeypatched legacy recursive enumerator cannot inject the escaped path;
    # the bounded scanner does not use it.
    monkeypatch.setattr(Path, "rglob", lambda _self, _pattern: iter([outside]))

    selected, evidence = core._select_no_cost_font_substitute("A")

    assert selected is None
    assert evidence["attempted_candidate_count"] == 0
    assert evidence["scan_bounded"] is True
    assert evidence["outside_root_rejection_count"] == 0


def test_bounded_candidate_scan_never_reports_more_files_than_its_cap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    approved_root = tmp_path / "approved"
    approved_root.mkdir()
    (approved_root / "a.ttf").write_bytes(_font_bytes(empty_a=False))
    (approved_root / "b.ttf").write_bytes(_font_bytes(empty_a=False))
    monkeypatch.setattr(
        core,
        "_no_cost_font_candidate_roots",
        lambda: [("installed_no_cost_substitute", approved_root)],
    )
    monkeypatch.setattr(core, "_FONT_SCAN_MAX_FILES", 1)

    candidates, evidence = core._bounded_font_candidates()

    assert len(candidates) == 1
    assert evidence["scan_capped"] is True
    assert evidence["scanned_file_count"] == 1
    assert evidence["scanned_file_count"] <= evidence["scan_limits"]["max_files"]


def test_substitute_coverage_cache_is_keyed_by_bytes_not_mutable_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    payload = _font_bytes(empty_a=False)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_font = first_root / "first.ttf"
    second_font = second_root / "second.ttf"
    first_font.write_bytes(payload)
    second_font.write_bytes(payload)
    active_root = [first_root]
    monkeypatch.setattr(
        core,
        "_no_cost_font_candidate_roots",
        lambda: [("installed_no_cost_substitute", active_root[0])],
    )
    original_coverage = core._font_bytes_glyph_coverage
    coverage_call_count = 0

    def counted_coverage(font_bytes: bytes, source_text: str) -> dict:
        nonlocal coverage_call_count
        coverage_call_count += 1
        return original_coverage(font_bytes, source_text)

    core._FONT_COVERAGE_CACHE.clear()
    monkeypatch.setattr(core, "_font_bytes_glyph_coverage", counted_coverage)

    first_selected, _first_evidence = core._select_no_cost_font_substitute("A")
    active_root[0] = second_root
    second_selected, second_evidence = core._select_no_cost_font_substitute("A")

    assert first_selected == str(first_font.resolve())
    assert second_selected == str(second_font.resolve())
    assert second_evidence["source_font_asset_sha256"] == hashlib.sha256(
        payload
    ).hexdigest()
    assert coverage_call_count == 1


def test_substitute_staging_uses_selected_immutable_bytes_after_candidate_path_swap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    approved_root = tmp_path / "approved"
    approved_root.mkdir()
    candidate = approved_root / "covering.ttf"
    selected_payload = _font_bytes(empty_a=False)
    candidate.write_bytes(selected_payload)
    monkeypatch.setattr(
        core,
        "_no_cost_font_candidate_roots",
        lambda: [("installed_no_cost_substitute", approved_root)],
    )
    stage_root = tmp_path / "attempt-fonts"
    monkeypatch.setattr(core, "_shapestring_font_cache_dir", lambda: stage_root)
    core._FONT_COVERAGE_CACHE.clear()

    selected_path, evidence = core._select_no_cost_font_substitute("A")
    candidate.write_bytes(_renamed_font_bytes(family="Path Swap", style="Regular"))

    staged_path, staged_evidence = core._stage_shapestring_font_asset(
        selected_path,
        evidence,
        core.ImportOptions(text_mode="3d_text"),
    )

    assert Path(staged_path).read_bytes() == selected_payload
    assert staged_evidence["source_font_asset_sha256"] == hashlib.sha256(
        selected_payload
    ).hexdigest()
    assert "_selected_font_bytes" not in staged_evidence
    assert "_selected_font_approved_root" not in staged_evidence


def test_substitute_total_byte_cap_is_enforced_before_reading_next_candidate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    approved_root = tmp_path / "approved"
    approved_root.mkdir()
    first = approved_root / "a.ttf"
    second = approved_root / "b.ttf"
    first_payload = _font_bytes(empty_a=True)
    second_payload = _font_bytes(empty_a=False)
    first.write_bytes(first_payload)
    second.write_bytes(second_payload)
    monkeypatch.setattr(
        core,
        "_no_cost_font_candidate_roots",
        lambda: [("installed_no_cost_substitute", approved_root)],
    )
    monkeypatch.setattr(
        core,
        "_FONT_SCAN_MAX_TOTAL_BYTES",
        len(first_payload) + len(second_payload) - 1,
    )
    original_read = core._read_stable_font_asset
    read_paths: list[Path] = []

    def record_read(path, **kwargs):
        read_paths.append(Path(path).resolve())
        return original_read(path, **kwargs)

    core._FONT_COVERAGE_CACHE.clear()
    monkeypatch.setattr(core, "_read_stable_font_asset", record_read)

    selected, evidence = core._select_no_cost_font_substitute("A")

    assert selected is None
    assert read_paths == [first.resolve()]
    assert evidence["scan_capped"] is True
    assert evidence["scanned_candidate_bytes"] == len(first_payload)
    assert evidence["scanned_candidate_bytes"] <= evidence["scan_limits"][
        "max_total_bytes"
    ]


def test_staged_font_digest_mutation_after_shapestring_is_terminal_and_cleans(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    candidate_root = tmp_path / "approved-fonts"
    candidate_root.mkdir()
    candidate = candidate_root / "covering.ttf"
    candidate.write_bytes(_font_bytes(empty_a=False))
    stage_root = tmp_path / "attempt-fonts"
    document, _draft, group = representation_fixtures._install_host(monkeypatch)
    item = representation_fixtures._canonical_3d_item(
        "A",
        font="MissingSourceFont-Regular",
    )
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: _font_misses(item),
    )
    monkeypatch.setattr(
        core,
        "_no_cost_font_candidate_roots",
        lambda: [("installed_no_cost_substitute", candidate_root)],
    )
    monkeypatch.setattr(core, "_shapestring_font_cache_dir", lambda: stage_root)
    original_make = core._make_shapestring_host

    def make_then_mutate(doc, source_text, font_path):
        host = original_make(doc, source_text, font_path)
        staged = Path(font_path)
        os.chmod(staged, stat_mode_writeable())
        staged.write_bytes(staged.read_bytes() + b"mutated")
        return host

    monkeypatch.setattr(core, "_make_shapestring_host", make_then_mutate)

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_text_item_3d(
            item,
            "3d_text",
            core.ImportOptions(text_mode="3d_text"),
            text_group=group,
            page_h=100.0,
            scale=1.0,
        )

    assert raised.value.attempt["reason"] == "staged_font_digest_changed"
    assert raised.value.attempt["cleanup_complete"] is True
    assert document.Objects == []


def stat_mode_writeable() -> int:
    # Owner read/write is portable and sufficient to simulate post-stage mutation.
    return 0o600


def _minimal_verified_child(child: dict, entity_id: str) -> dict:
    return {
        "source_item_id": child["source_item_id"],
        "requested_type": child["requested_type"],
        "attempted_type": child["requested_type"],
        "final_type": child["requested_type"],
        "outcome": "verified",
        "created_entity_ids": [entity_id],
        "delivery_entity_ids": [entity_id],
        "support_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
        "delivery_count": 1,
        "evidence": {
            "source_text": child["text"],
            "source_text_preserved": True,
            "source_ink_evidence": copy.deepcopy(child["source_ink_evidence"]),
            "source_ink_evidence_persisted": True,
            **core._source_ink_delivery_binding_fields(
                child,
                child["source_ink_evidence"],
            ),
        },
    }


def test_segment_wrapper_cleans_prior_and_current_children_on_untyped_exception(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    item = source_ink_fixtures._bound_pdf_item(tmp_path, "text", "mixed_glyphs")
    document = representation_fixtures.FakeDocument()
    group = representation_fixtures.FakeGroup(document)
    calls = 0

    def deliver_child(child: dict) -> dict:
        nonlocal calls
        entity_id = "Segment_%d" % calls
        host = SimpleNamespace(Name=entity_id)
        document.Objects.append(host)
        group.objects.append(host)
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic post-creation child failure")
        return _minimal_verified_child(child, entity_id)

    with pytest.raises(core.TextRepresentationFailure) as raised:
        core._deliver_mixed_source_ink_segments(
            item,
            "text",
            core.ImportOptions(text_mode="text"),
            deliver_child=deliver_child,
            text_group=group,
        )

    assert raised.value.attempt["reason"] == "source_segment_delivery_failed"
    assert raised.value.attempt["cleanup_complete"] is True
    assert set(raised.value.attempt["created_entity_ids"]) == {
        "Segment_0",
        "Segment_1",
    }
    assert set(raised.value.attempt["removed_entity_ids"]) == {
        "Segment_0",
        "Segment_1",
    }
    assert document.Objects == []
    assert group.objects == []


def test_report_rejects_self_rehashed_child_evidence_not_derived_from_parent_slice(
    tmp_path: Path,
) -> None:
    item = source_ink_fixtures._bound_pdf_item(tmp_path, "text", "mixed_glyphs")
    counter = 0

    def deliver_child(child: dict) -> dict:
        nonlocal counter
        entity_id = "Segment_%d" % counter
        counter += 1
        return _minimal_verified_child(child, entity_id)

    terminal = core._deliver_mixed_source_ink_segments(
        item,
        "text",
        core.ImportOptions(text_mode="text"),
        deliver_child=deliver_child,
        text_group=None,
    )
    child_evidence = terminal["evidence"]["segment_deliveries"][0]["evidence"]
    forged_identity = {"raw_name": "Forged-Regular", "normalized_key": "forgedregular"}
    child_evidence["source_font_identity"] = forged_identity
    child_evidence["source_ink_evidence"]["font_identity"] = forged_identity
    child_evidence["source_ink_evidence"]["evidence_sha256"] = (
        core._source_ink_evidence_digest(child_evidence["source_ink_evidence"])
    )

    contexts = report_contract._freecad_segment_delivery_contexts(
        terminal,
        expected_pdf_sha256=item["pdf_sha256"],
        expected_page_number=item["page_number"],
    )

    assert contexts == {}


def test_3d_inventory_binds_complete_staged_font_delivery_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document, _draft, _group = representation_fixtures._install_host(monkeypatch)
    host = representation_fixtures.FakeShapeString(document, "ShapeString", "A")
    host.PDFRepresentation = "3d_text"
    host.PDFSourceItemId = "p1:b0:l0:s0"
    payload = _font_bytes(empty_a=False)
    digest = hashlib.sha256(payload).hexdigest()
    staged = tmp_path / (digest + ".ttf")
    staged.write_bytes(payload)
    os.chmod(staged, 0o400)
    metadata = {
        "PDFSourceFontEquivalence": False,
        "PDFFontSubstitutionApplied": True,
        "PDFFontIdentityVerified": False,
        "PDFFontGlyphCoverageVerified": True,
        "PDFDeliveredFontSHA256": digest,
        "PDFFontCandidateSource": "installed_no_cost_substitute",
        "PDFSourceFontAssetPath": str((tmp_path / "source.ttf").resolve()),
        "PDFSourceFontAssetSHA256": digest,
        "PDFStagedFontPath": str(staged.resolve()),
        "PDFStagedFontSHA256": digest,
        "PDFStagedFontVerified": True,
        "PDFStagedFontReadOnly": True,
        "PDFFontInternalIdentitySHA256": "b" * 64,
        "PDFFontCoverageEvidenceSHA256": "c" * 64,
    }
    for name, value in metadata.items():
        setattr(host, name, value)
    document.Objects.append(host)

    inventory = core._build_host_object_inventory(document.Objects)
    font = inventory["objects"][0]["content"]["font_delivery"]

    assert font["source_font_equivalence"] is False
    assert font["font_substitution_applied"] is True
    assert font["source_font_asset_path"] == metadata["PDFSourceFontAssetPath"]
    assert font["source_font_asset_sha256"] == digest
    assert font["staged_font_path"] == str(staged.resolve())
    assert font["staged_font_sha256"] == digest
    assert font["staged_asset_verified"] is True
    assert font["staged_asset_read_only"] is True
    assert font["staged_asset_digest_matches"] is True
