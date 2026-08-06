"""atomic_write_bytes must not turn an environment fault into a bare OSError.

This helper publishes evidence artifacts on the delivery path, and its callers
treat it as infallible -- in LibreCAD the font-asset staging calls sit outside
any guard, so an OSError from here escapes to export_to_dxf, which rolls the
transaction back and re-raises. A path-length or permission difference on a
customer's machine therefore fails the whole import rather than descending a
rung.

Two concrete amplifiers lived in the helper itself:

1. No Windows extended-length (\\\\?\\) prefix, so writes died at MAX_PATH.
2. The temp sibling was named ".{full name}.{32-hex}.tmp", adding 38 characters
   on top of an already-long name. Staged font assets are 64-hex digests, so the
   temp file was ~106 characters where the real file was 68 -- the helper pushed
   its own callers over the limit.

CAREFUL WITH THE LENGTH TEST: this development machine has LongPathsEnabled=1,
so a test that waits for Windows to refuse a long path passes here for the wrong
reason and stays green forever. The length assertions below are therefore made
against the constructed path itself, not against the OS's willingness to open
it.

Both directions are covered: a fault must raise the typed error the callers can
convert into a descent, and the ordinary short-path case must still round-trip
byte-identically with no orphan temp file left behind.
"""
from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "PDFVectorImporter"))

from pdfcadcore import atomic_io  # noqa: E402


# --- ordinary behaviour must be preserved ------------------------------------


def test_short_path_round_trips_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "report.json"
    payload = b'{"ok": true}\n'

    returned = atomic_io.atomic_write_bytes(target, payload)

    assert Path(returned) == target
    assert target.read_bytes() == payload
    siblings = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert siblings == [], f"orphan temp file left behind: {siblings}"


def test_existing_target_is_replaced_not_appended(tmp_path):
    target = tmp_path / "report.json"
    target.write_bytes(b"stale-and-longer-than-the-replacement")

    atomic_io.atomic_write_bytes(target, b"fresh")

    assert target.read_bytes() == b"fresh"


def test_text_helper_encodes_and_round_trips(tmp_path):
    target = tmp_path / "note.txt"
    atomic_io.atomic_write_text(target, "caf\u00e9", encoding="utf-8")
    assert target.read_bytes() == "caf\u00e9".encode("utf-8")


def test_parent_directories_are_created(tmp_path):
    target = tmp_path / "deep" / "nested" / "out.bin"
    atomic_io.atomic_write_bytes(target, b"x")
    assert target.read_bytes() == b"x"


# --- the temp sibling must stop amplifying path length -----------------------


def test_temp_sibling_is_far_shorter_than_the_target_name():
    """The helper must not be the reason a caller crosses MAX_PATH."""
    digest_name = "a" * 64 + ".png"          # a staged font/raster asset
    temp_name = atomic_io._temporary_name(digest_name)

    assert temp_name.endswith(".tmp")
    assert len(temp_name) <= 40, (
        f"temp name {temp_name!r} is {len(temp_name)} chars; it used to add 38 "
        "characters to an already-long digest name and pushed callers past "
        "MAX_PATH"
    )
    assert len(temp_name) < len(digest_name), (
        "the temp sibling must never be longer than the file it stands in for"
    )


def test_temp_names_are_unique():
    name = "asset.png"
    generated = {atomic_io._temporary_name(name) for _ in range(200)}
    assert len(generated) == 200, "temp names must not collide"


# --- long paths --------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="extended-length prefix is Windows-only")
def test_absolute_windows_paths_get_the_extended_length_prefix():
    prefixed = atomic_io._extended_path(Path(r"C:\some\deep\place\file.bin"))
    assert str(prefixed).startswith("\\\\?\\"), (
        "without this prefix the Win32 API refuses paths over MAX_PATH"
    )


@pytest.mark.skipif(os.name != "nt", reason="extended-length prefix is Windows-only")
def test_unc_paths_use_the_unc_form_of_the_prefix():
    prefixed = atomic_io._extended_path(Path(r"\\server\share\file.bin"))
    assert str(prefixed).startswith("\\\\?\\UNC\\"), (
        r"\\?\ applied naively to a UNC path produces an invalid path"
    )


@pytest.mark.skipif(os.name != "nt", reason="extended-length prefix is Windows-only")
def test_already_prefixed_path_is_not_double_prefixed():
    once = atomic_io._extended_path(Path(r"C:\x\y.bin"))
    twice = atomic_io._extended_path(once)
    assert str(twice) == str(once)


def test_relative_paths_are_left_alone():
    relative = Path("out") / "file.bin"
    assert atomic_io._extended_path(relative) == relative


@pytest.mark.skipif(os.name != "nt", reason="MAX_PATH is a Windows limit")
def test_atomic_write_survives_max_path(tmp_path):
    """A target beyond MAX_PATH must be written, not refused.

    Asserts the constructed path really is over the limit rather than trusting
    this machine's LongPathsEnabled setting to make the case interesting.
    """
    deep = tmp_path
    while len(str(deep)) < 200:
        deep = deep / ("segment_" + "d" * 20)
    target = deep / ("f" * 64 + ".bin")
    assert len(str(target)) > 260, (
        f"test is not exercising the limit: path is {len(str(target))} chars"
    )

    payload = b"long-path-payload"
    atomic_io.atomic_write_bytes(target, payload)

    assert atomic_io.read_bytes(target) == payload


# --- faults must be typed, never a bare OSError ------------------------------


def test_write_failure_raises_the_typed_error(tmp_path, monkeypatch):
    """Callers convert AtomicWriteError into a ladder descent.

    A bare OSError is indistinguishable from a programming fault, so it
    propagated as a generic failure and aborted the whole import.
    """
    target = tmp_path / "out.bin"

    def _explode(*args, **kwargs):
        raise OSError(errno.ENAMETOOLONG, "File name too long")

    monkeypatch.setattr(atomic_io.Path, "open", _explode)

    with pytest.raises(atomic_io.AtomicWriteError) as caught:
        atomic_io.atomic_write_bytes(target, b"x")
    assert "out.bin" in str(caught.value), "the error must name the target"


def test_unwritable_parent_raises_the_typed_error(tmp_path, monkeypatch):
    target = tmp_path / "sub" / "out.bin"

    def _explode(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Access is denied")

    monkeypatch.setattr(atomic_io.Path, "mkdir", _explode)

    with pytest.raises(atomic_io.AtomicWriteError):
        atomic_io.atomic_write_bytes(target, b"x")


def test_typed_error_is_still_an_oserror(tmp_path, monkeypatch):
    """Existing `except OSError` handlers must keep working."""
    assert issubclass(atomic_io.AtomicWriteError, OSError)


def test_failed_write_leaves_no_partial_target(tmp_path, monkeypatch):
    target = tmp_path / "out.bin"
    target.write_bytes(b"original")

    real_replace = atomic_io.Path.replace

    def _explode(self, *args, **kwargs):
        raise OSError(errno.EIO, "I/O error")

    monkeypatch.setattr(atomic_io.Path, "replace", _explode)

    with pytest.raises(atomic_io.AtomicWriteError):
        atomic_io.atomic_write_bytes(target, b"replacement")

    monkeypatch.setattr(atomic_io.Path, "replace", real_replace)
    assert target.read_bytes() == b"original", (
        "a failed publish must leave the previous artifact intact"
    )
    siblings = [p.name for p in tmp_path.iterdir() if p.name != target.name]
    assert siblings == [], f"orphan temp file left behind: {siblings}"
