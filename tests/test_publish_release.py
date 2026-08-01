from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts import publish_release


TARGET = "a" * 40


def _assets(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / "payload.zip", tmp_path / "setup.exe", tmp_path / "attestation.json"]
    for index, path in enumerate(paths, start=1):
        path.write_bytes((path.name + str(index)).encode("utf-8"))
    return paths


class FakeRemote:
    def __init__(self, *, tag_target=None, release=None, race=False, pending_digest_reads=0):
        self.tag_target = tag_target
        self.release = release
        self.race = race
        self.pending_digest_reads = pending_digest_reads
        self.commands: list[list[str]] = []

    def _completed(self, args, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    def __call__(self, args):
        args = list(args)
        self.commands.append(args)
        if args[:2] == ["gh", "api"]:
            if self.release is None:
                return self._completed(args, 1, stderr="HTTP 404")
            payload = self.release
            if self.pending_digest_reads:
                self.pending_digest_reads -= 1
                payload = json.loads(json.dumps(self.release))
                for asset in payload.get("assets", []):
                    asset["digest"] = None
            return self._completed(args, stdout=json.dumps(payload))
        if args[:3] == ["git", "ls-remote", "origin"]:
            if self.tag_target is None:
                return self._completed(args, stdout="")
            tag = args[-2].removeprefix("refs/tags/")
            return self._completed(args, stdout=f"{self.tag_target}\trefs/tags/{tag}\n")
        if args[:3] == ["gh", "release", "create"]:
            if self.race:
                self.race = False
                self.tag_target = TARGET
                self.release = self._release_payload(args)
                return self._completed(args, 1, stderr="already exists")
            self.tag_target = TARGET
            self.release = self._release_payload(args)
            return self._completed(args)
        raise AssertionError(f"unexpected command: {args!r}")

    @staticmethod
    def _release_payload(args):
        asset_paths = [Path(item) for item in args if Path(item).suffix in {".zip", ".exe", ".json"}]
        return {
            "tag_name": "v9.9.9",
            "draft": False,
            "assets": [
                {
                    "name": path.name,
                    "size": path.stat().st_size,
                    "digest": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in asset_paths
            ],
        }


def _config(tmp_path: Path) -> publish_release.PublishConfig:
    return publish_release.PublishConfig(
        repo="owner/repo",
        tag="v9.9.9",
        target=TARGET,
        title="v9.9.9",
        notes="notes",
        assets=_assets(tmp_path),
        latest=True,
    )


def test_absent_tag_creates_complete_release_in_one_command(tmp_path):
    config = _config(tmp_path)
    remote = FakeRemote()
    result = publish_release.publish(config, run=remote)

    assert result.minted is True
    create = next(cmd for cmd in remote.commands if cmd[:3] == ["gh", "release", "create"])
    assert [str(path) for path in config.assets] == create[-3:]
    assert create[create.index("--target") + 1] == TARGET
    assert "--verify-tag" not in create


def test_exact_orphan_tag_is_recovered_without_rewrite(tmp_path):
    config = _config(tmp_path)
    remote = FakeRemote(tag_target=TARGET)
    result = publish_release.publish(config, run=remote)

    assert result.minted is True
    create = next(cmd for cmd in remote.commands if cmd[:3] == ["gh", "release", "create"])
    assert "--verify-tag" in create
    assert "--target" not in create
    assert not any("delete" in cmd for command in remote.commands for cmd in command)


def test_mismatched_existing_tag_fails_closed(tmp_path):
    config = _config(tmp_path)
    remote = FakeRemote(tag_target="b" * 40)
    with pytest.raises(RuntimeError, match="not release target"):
        publish_release.publish(config, run=remote)
    assert not any(cmd[:3] == ["gh", "release", "create"] for cmd in remote.commands)


def test_existing_exact_release_is_idempotent_and_verified(tmp_path):
    config = _config(tmp_path)
    remote = FakeRemote(tag_target=TARGET)
    remote.release = remote._release_payload([str(path) for path in config.assets])
    result = publish_release.publish(config, run=remote)
    assert result.minted is False
    assert result.release_verified is True
    assert not any(cmd[:3] == ["gh", "release", "create"] for cmd in remote.commands)


def test_existing_release_with_wrong_digest_fails_closed(tmp_path):
    config = _config(tmp_path)
    remote = FakeRemote(tag_target=TARGET)
    remote.release = remote._release_payload([str(path) for path in config.assets])
    remote.release["assets"][0]["digest"] = "sha256:" + "0" * 64
    with pytest.raises(RuntimeError, match="asset digest mismatch"):
        publish_release.publish(config, run=remote)


def test_create_race_converges_by_re_reading_exact_release(tmp_path):
    config = _config(tmp_path)
    remote = FakeRemote(race=True)
    result = publish_release.publish(config, run=remote)
    assert result.minted is False
    assert result.release_verified is True


def test_new_release_waits_for_github_asset_digests_to_settle(tmp_path):
    config = _config(tmp_path)
    remote = FakeRemote(pending_digest_reads=1)
    waits = []
    result = publish_release.publish(config, run=remote, sleep=waits.append)
    assert result.minted is True
    assert result.release_verified is True
    assert waits == [2.0]


def test_publish_source_contains_no_mutating_recovery_primitives():
    source = Path(publish_release.__file__).read_text(encoding="utf-8")
    assert "release upload" not in source
    assert "release delete" not in source
    assert "tag --delete" not in source
    assert "--clobber" not in source
