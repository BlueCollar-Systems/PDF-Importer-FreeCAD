from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import release_bookkeeping


def test_bookkeeping_record_is_canonical_and_subject_always_skips_release(tmp_path):
    ledger = tmp_path / "release-bookkeeping"
    assets = [
        {"name": "setup.exe", "size": 9, "sha256": "b" * 64},
        {"name": "payload.zip", "size": 7, "sha256": "a" * 64},
    ]
    output = release_bookkeeping.write_record(
        ledger,
        tag="v4.0.80",
        target="c" * 40,
        assets=assets,
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["assets"][0]["name"] == "payload.zip"
    assert output.read_bytes().endswith(b"\n")
    assert release_bookkeeping.commit_subject("v4.0.80") == (
        "chore(release): record v4.0.80 artifact digests [skip release]"
    )


def test_bookkeeping_rejects_paths_outside_ledger(tmp_path):
    with pytest.raises(ValueError, match="release-bookkeeping"):
        release_bookkeeping.validate_ledger_path(tmp_path / "elsewhere" / "v.json", tmp_path)


def test_auto_release_ignores_bookkeeping_path_and_keeps_message_guard():
    repo = Path(__file__).resolve().parents[1]
    workflow = (repo / ".github" / "workflows" / "auto-release.yml").read_text(
        encoding="utf-8"
    )
    assert '"release-bookkeeping/**"' in workflow
    assert "[skip release]" in workflow
    assert "steps.mint.outputs.release_verified == 'true'" in workflow
    assert "python scripts/release_bookkeeping.py record" in workflow
    assert 'git add -- "$RECORD"' in workflow
    assert "token: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "secrets.RELEASE_BUMP_TOKEN" not in workflow
    assert 'BRANCH="automation/release-bookkeeping-${VERSION}"' in workflow
    assert 'git push origin "HEAD:refs/heads/$BRANCH"' in workflow
    assert 'gh pr create --base main --head "$BRANCH"' in workflow
