from __future__ import annotations

import hashlib
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
import test_freecad_representation_contract as representation_fixtures  # noqa: E402
import test_freecad_source_ink_contract as source_ink_fixtures  # noqa: E402


def _exact_font_miss_results(item: dict) -> list[dict]:
    return [
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


def test_exact_font_miss_selects_local_glyph_covering_substitute_and_keeps_3d_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exact identity loss is fidelity evidence, not 3D Text impossibility."""

    windows_fonts = tmp_path / "Fonts"
    windows_fonts.mkdir()
    missing_glyph_font = windows_fonts / "000_missing_b.ttf"
    covering_font = windows_fonts / "999_covers_ab.ttf"
    missing_glyph_font.write_bytes(
        source_ink_fixtures._test_font_bytes(empty_a=False, visible_b=False)
    )
    covering_font.write_bytes(
        source_ink_fixtures._test_font_bytes(empty_a=False, visible_b=True)
    )
    monkeypatch.setenv("WINDIR", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))

    document, draft, group = representation_fixtures._install_host(monkeypatch)
    item = representation_fixtures._canonical_3d_item(
        "AB",
        font="MissingSourceFont-Regular",
    )
    misses = _exact_font_miss_results(item)
    monkeypatch.setattr(
        core,
        "_resolve_shapestring_font_path_with_evidence",
        lambda *_args, **_kwargs: (None, misses),
    )

    result = core._deliver_text_item_3d(
        item,
        "3d_text",
        core.ImportOptions(text_mode="3d_text"),
        text_group=group,
        page_h=100.0,
        scale=1.0,
    )

    assert result["requested_type"] == "3d_text"
    assert result["attempted_type"] == "3d_text"
    assert result["final_type"] == "3d_text"
    assert result["outcome"] == "verified"
    evidence = result["evidence"]
    assert draft.calls[0][0] == "AB"
    assert Path(draft.calls[0][1]) != covering_font
    assert evidence["source_font_asset_path"] == str(covering_font.resolve())

    assert evidence["source_font_equivalence"] is False
    assert evidence["font_substitution_applied"] is True
    assert evidence["font_identity_verified"] is False
    assert evidence["glyph_coverage_verified"] is True
    assert evidence["font_candidate_source"] == "installed_no_cost_substitute"
    assert evidence["delivered_font_sha256"] == hashlib.sha256(
        covering_font.read_bytes()
    ).hexdigest()

    for host_obj in document.Objects:
        assert host_obj.PDFSourceFontEquivalence is False
        assert host_obj.PDFFontSubstitutionApplied is True
        assert host_obj.PDFFontIdentityVerified is False
        assert host_obj.PDFFontGlyphCoverageVerified is True


def _obsolete_exact_font_proof(item: dict) -> dict:
    return {
        "item_specific_proven_impossible": True,
        "importer_identity": core.FREECAD_TEXT_IMPORTER_IDENTITY,
        "pdf_sha256": item["pdf_sha256"],
        "page_number": item["page_number"],
        "source_item_id": item["source_item_id"],
        "requested_type": "3d_text",
        "attempted_type": "3d_text",
        "reason_code": "exact_font_unavailable",
        "font_identity": dict(item["font_identity"]),
        "evidence": {"normalized_key": item["font_identity"]["normalized_key"]},
        "attempted_source_results": _exact_font_miss_results(item),
        "attempted_sources_complete": True,
        "created_entity_ids": [],
        "removed_entity_ids": [],
        "cleanup_complete": True,
    }


def test_obsolete_exact_font_absence_proof_cannot_authorize_fallback() -> None:
    item = representation_fixtures._canonical_3d_item(
        "AB",
        font="MissingSourceFont-Regular",
    )
    proof = _obsolete_exact_font_proof(item)

    with pytest.raises(ValueError):
        core._validate_item_impossibility_proof(
            item,
            "3d_text",
            "3d_text",
            proof,
        )
