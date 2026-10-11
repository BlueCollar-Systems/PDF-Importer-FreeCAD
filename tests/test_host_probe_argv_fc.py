"""Real-FreeCAD tests must run FreeCADCmd with the probe script as its ONLY argument.

FreeCADCmd treats every extra command-line argument as a file to open once the
script finishes. When a test passed ``out.json report.json drawing.pdf`` after
the probe, FreeCAD handed the JSON files to the FEM mesh importer ("Exception
while processing file") and the PDF to whatever copy of this importer is
installed on that PC, importing it a second time with code that is not under
test. The probes read their paths from environment variables instead
(``BCS_PROBE_OUT`` / ``BCS_PROBE_REPORT`` / ``BCS_PROBE_PDF``).

This is a source scan, so it runs everywhere without FreeCAD.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = REPO_ROOT / "tests"

# The real-host tests known to spawn FreeCADCmd. Each must still contain at
# least one FreeCADCmd launch, so the scan can never pass vacuously.
HOST_TEST_FILES = (
    "test_text3d_perf_budget_fc.py",
    "test_embedded_font_staging_fc.py",
    "test_hybrid_underlay_dedupe_fc.py",
    "test_raster_delivery_fixture_fc.py",
)

_SPAWN_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}


def _is_subprocess_spawn(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in _SPAWN_FUNCS:
        return isinstance(func.value, ast.Name) and func.value.id == "subprocess"
    return False


def _first_element_names_freecadcmd(argv: ast.AST) -> bool:
    if not isinstance(argv, (ast.List, ast.Tuple)) or not argv.elts:
        return False
    head = argv.elts[0]
    text = ast.unparse(head).lower()
    return "freecadcmd" in text


def _freecadcmd_launches(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_subprocess_spawn(node):
            continue
        argv = node.args[0] if node.args else None
        if argv is None:
            for keyword in node.keywords:
                if keyword.arg == "args":
                    argv = keyword.value
        if argv is not None and _first_element_names_freecadcmd(argv):
            yield node, argv


def _probe_sources_reading_argv(path: Path):
    """String constants that look like FreeCAD probe scripts and read sys.argv."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value
            if "import FreeCAD" in text and "sys.argv" in text:
                yield node.lineno


def _all_host_test_files():
    names = set(HOST_TEST_FILES)
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        if path.name == Path(__file__).name:
            continue
        if any(True for _ in _freecadcmd_launches(path)):
            names.add(path.name)
    return [TESTS_DIR / name for name in sorted(names)]


def test_known_host_tests_still_launch_freecadcmd():
    for name in HOST_TEST_FILES:
        path = TESTS_DIR / name
        assert path.is_file(), "real-host test file is missing: %s" % name
        launches = list(_freecadcmd_launches(path))
        assert launches, (
            "%s no longer launches FreeCADCmd through subprocess; update "
            "HOST_TEST_FILES so this scan keeps guarding real launches" % name
        )


def test_freecadcmd_argv_is_only_the_probe_script():
    offenders = []
    for path in _all_host_test_files():
        for node, argv in _freecadcmd_launches(path):
            if len(argv.elts) > 2:
                offenders.append(
                    "%s:%d passes %d arguments: %s"
                    % (path.name, node.lineno, len(argv.elts), ast.unparse(argv))
                )
    assert not offenders, (
        "FreeCADCmd opens every argument after the probe script as a file "
        "(the PDF gets imported a second time by the installed add-on). Pass "
        "paths through env vars instead:\n" + "\n".join(offenders)
    )


def test_freecadcmd_launches_pass_paths_through_env():
    missing_env = []
    for path in _all_host_test_files():
        for node, _argv in _freecadcmd_launches(path):
            if not any(keyword.arg == "env" for keyword in node.keywords):
                missing_env.append("%s:%d" % (path.name, node.lineno))
    assert not missing_env, (
        "FreeCADCmd launches without env=; the probe cannot receive its "
        "output/report/PDF paths: " + ", ".join(missing_env)
    )


def test_probe_scripts_read_paths_from_environment_not_argv():
    offenders = []
    for path in _all_host_test_files():
        for lineno in _probe_sources_reading_argv(path):
            offenders.append("%s:%d" % (path.name, lineno))
    assert not offenders, (
        "probe scripts still read sys.argv (paths must come from "
        "BCS_PROBE_OUT / BCS_PROBE_REPORT / BCS_PROBE_PDF): " + ", ".join(offenders)
    )
