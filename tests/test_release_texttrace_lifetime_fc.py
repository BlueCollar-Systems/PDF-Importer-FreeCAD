"""Release runtime probes must cover repeated getters and natural process exit."""
from pathlib import Path
import subprocess
import sys

import pytest

import build_release


def runtime_fixture(tmp_path: Path, *, version="1.28.2", fail_after=None):
    common = tmp_path / "common"
    abi = tmp_path / "cp311"
    common.mkdir()
    abi.mkdir()
    counter = tmp_path / "calls.txt"
    (abi / "fontTools.py").write_text("__version__ = '4.63.0'\n", encoding="utf-8")
    (common / "pymupdf.py").write_text(
        f"__version__ = {version!r}\n"
        "from pathlib import Path\n"
        f"counter = Path({str(counter)!r})\n"
        "calls = 0\n"
        "class Document:\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *args): return False\n"
        "    def new_page(self): return self\n"
        "    def insert_text(self, *args): pass\n"
        "    def get_texttrace(self):\n"
        "        global calls\n"
        "        calls += 1\n"
        "        counter.write_text(str(calls))\n"
        f"        if {fail_after!r} is not None and calls > ({fail_after!r} or 0):\n"
        "            raise RuntimeError('repeated getter failure')\n"
        "        return [{} for _ in range(10)]\n"
        "def open(): return Document()\n", encoding="utf-8"
    )
    return abi, common, counter


def test_release_probe_disposes_many_getter_results_and_exits_cleanly(tmp_path):
    abi, common, counter = runtime_fixture(tmp_path)
    assert build_release._runtime_has_runtime_dependencies(Path(sys.executable), abi, common)
    assert int(counter.read_text()) == 2048


def test_release_probe_rejects_failure_after_successful_early_getters(tmp_path):
    abi, common, counter = runtime_fixture(tmp_path, fail_after=1024)
    assert not build_release._runtime_has_runtime_dependencies(Path(sys.executable), abi, common)
    assert int(counter.read_text()) == 1025


@pytest.mark.parametrize("version", ["1.28.0", "1.28.1", "1.28.20"])
def test_release_probe_requires_exact_pinned_version(tmp_path, version):
    abi, common, counter = runtime_fixture(tmp_path, version=version)
    assert not build_release._runtime_has_runtime_dependencies(Path(sys.executable), abi, common)
    assert not counter.exists()


def test_release_probe_rejects_success_marker_with_failed_natural_exit(tmp_path, monkeypatch):
    monkeypatch.setattr(build_release.subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess(a, 1, "OK\n", "fatal finalizer"))
    assert not build_release._runtime_has_runtime_dependencies(Path(sys.executable), tmp_path, tmp_path)


def test_release_probe_timeout_is_failure(tmp_path, monkeypatch):
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == 60
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])
    monkeypatch.setattr(build_release.subprocess, "run", timeout)
    assert not build_release._runtime_has_runtime_dependencies(Path(sys.executable), tmp_path, tmp_path)
