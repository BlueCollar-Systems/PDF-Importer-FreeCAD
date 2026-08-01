# BlueCollar Systems — BUILT. NOT BOUGHT.
#
# FreeCAD QA harness for PDF Vector Importer.
#
# Run with:
#   FreeCADCmd.exe adapters/freecad_harness.py
#
# Reads ENV["BC_PDF_QA_PAYLOAD"], performs the import, writes result JSON.

from __future__ import annotations

import importlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import List, Optional, Set


def result_exit_code(result: dict) -> int:
    """Return success only for a terminal PASS result."""

    return 0 if str(result.get("status") or "").upper() == "PASS" else 1


def import_result_status(core_result) -> tuple[str, str]:
    """Translate the core contract without treating ``None`` as success."""

    if core_result is True:
        return "PASS", "Import completed."
    return "FAIL", "core.import_pdf did not return explicit success."


def _page_range_spec(pages) -> str:
    values = sorted({int(page) for page in pages if int(page) > 0})
    if not values:
        return ""
    ranges = []
    start = previous = values[0]
    for page in values[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def plan_page_cell(
    requested_pages,
    *,
    page_budget: int,
    source_pdf_sha256: str,
    checkpoint: Optional[dict] = None,
) -> dict:
    """Plan one bounded QA cell without claiming unfinished pages complete."""

    requested = sorted({int(page) for page in requested_pages if int(page) > 0})
    digest = str(source_pdf_sha256 or "").strip().lower()
    prior = checkpoint if isinstance(checkpoint, dict) else {}
    checkpoint_reused = bool(
        prior
        and str(prior.get("source_pdf_sha256") or "").strip().lower() == digest
        and list(prior.get("requested_pages") or []) == requested
    )
    completed = []
    if checkpoint_reused:
        completed = sorted(
            {
                int(page)
                for page in (prior.get("completed_pages") or [])
                if int(page) in requested
            }
        )
    remaining = [page for page in requested if page not in set(completed)]
    try:
        limit = int(page_budget)
    except (TypeError, ValueError):
        limit = 0
    active = remaining if limit <= 0 else remaining[:limit]
    return {
        "schema": "bcs.freecad_page_progress/1.0",
        "source_pdf_sha256": digest,
        "requested_pages": requested,
        "page_budget": max(0, limit),
        "checkpoint_reused": checkpoint_reused,
        "completed_pages": completed,
        "active_pages": active,
        "remaining_pages": remaining,
        "next_page_range": _page_range_spec(active),
        "status": "pending",
        "run_complete": not remaining,
    }


def complete_page_cell(progress: dict) -> dict:
    """Return a new checkpoint with the active cell durably completed."""

    completed = sorted(
        {
            int(page)
            for page in list(progress.get("completed_pages") or [])
            + list(progress.get("active_pages") or [])
        }
    )
    requested = [int(page) for page in (progress.get("requested_pages") or [])]
    remaining = [page for page in requested if page not in set(completed)]
    updated = dict(progress)
    updated.update(
        completed_pages=completed,
        active_pages=[],
        remaining_pages=remaining,
        next_page_range=_page_range_spec(remaining[: max(1, int(progress.get("page_budget") or len(remaining) or 1))]),
        status="complete" if not remaining else "cell_pass",
        run_complete=not remaining,
    )
    return updated


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_page_checkpoint(path: Optional[str]) -> dict:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as source:
        value = json.load(source)
    if not isinstance(value, dict):
        raise ValueError("page checkpoint root must be a JSON object")
    return value


def _write_page_checkpoint(path: Optional[str], progress: dict) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    updated = dict(progress)
    updated["updated_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
    )
    temporary = target.with_name(target.name + ".tmp")
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(updated, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, target)


def _git_output(source_root: str, args) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", source_root, *list(args)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return ""
    return (completed.stdout or "").strip() if completed.returncode == 0 else ""


def build_acceptance_identity(
    core,
    package_or_source_root: Optional[str],
    *,
    package_sha256: str = "",
) -> dict:
    """Bind host evidence to the exact engine and package/source under test."""

    engine_file = str(Path(str(core.__file__)).resolve())
    source_root = str(
        Path(package_or_source_root or Path(engine_file).parent).resolve()
    )
    supplied_package_sha = str(package_sha256 or "").strip().lower()
    if supplied_package_sha and re.fullmatch(r"[0-9a-f]{64}", supplied_package_sha) is None:
        raise ValueError("package_sha256 must be a 64-character hexadecimal digest")
    version_reader = getattr(core, "_importer_version", None)
    importer_version = str(version_reader() or "") if callable(version_reader) else ""
    return {
        "importer_version": importer_version,
        "git_commit": _git_output(source_root, ["rev-parse", "HEAD"]),
        "git_tag": _git_output(source_root, ["describe", "--tags", "--exact-match"]),
        "package_or_source_root": source_root,
        "engine_module_file": engine_file,
        "engine_module_sha256": _file_sha256(engine_file),
        "package_sha256": supplied_package_sha,
    }


def resolve_path(value: Optional[str], base_dir: Optional[str] = None) -> Optional[str]:
    if value is None:
        return None
    raw = os.path.expandvars(str(value))
    p = Path(raw).expanduser()
    if not p.is_absolute() and base_dir:
        p = Path(base_dir) / p
    return str(p.resolve())


def parse_pages(spec: Optional[str], pdf_path: str):
    s = (spec or "").strip()
    if not s:
        return [1]

    if s.lower() == "all":
        try:
            try:
                import pymupdf as fitz  # type: ignore  # PyMuPDF >= 1.24 preferred name
            except ImportError:
                import fitz  # type: ignore  # Legacy fallback

            doc = fitz.open(pdf_path)
            total = len(doc)
            doc.close()
            return list(range(1, total + 1))
        except (ImportError, OSError, RuntimeError):
            return [1]

    pages: Set[int] = set()
    for part in [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]:
        if "-" in part:
            a, b = [x.strip() for x in part.split("-", 1)]
            if a.isdigit() and b.isdigit():
                start, end = sorted((int(a), int(b)))
                for n in range(start, end + 1):
                    if n > 0:
                        pages.add(n)
        elif part.isdigit():
            n = int(part)
            if n > 0:
                pages.add(n)
    return sorted(pages) if pages else [1]


def setup_import_paths(payload: dict, payload_dir: str) -> None:
    mod_dir = resolve_path(payload.get("mod_dir"), payload_dir)
    if mod_dir and os.path.isdir(mod_dir):
        p = Path(mod_dir)
        if p.name.lower() == "pdfvectorimporter":
            sys.path.insert(0, str(p.parent))
            sys.path.insert(0, str(p))
            src = p / "src"
            if src.is_dir():
                sys.path.insert(0, str(src))
        else:
            sys.path.insert(0, str(p))
            pkg = p / "PDFVectorImporter"
            if pkg.is_dir():
                sys.path.insert(0, str(pkg.parent))
                src = pkg / "src"
                if src.is_dir():
                    sys.path.insert(0, str(src))


def import_core_module():
    errors = {}
    for name in (
        "PDFVectorImporter.src.PDFImporterCore",
        "src.PDFImporterCore",
        "PDFImporterCore",
    ):
        try:
            return importlib.import_module(name)
        except (ImportError, ModuleNotFoundError, AttributeError, RuntimeError, ValueError, SyntaxError) as exc:
            errors[name] = f"{exc.__class__.__name__}: {exc}"
            continue
    raise RuntimeError(
        "Could not import PDFImporterCore module from configured mod_dir. "
        f"Attempts: {errors}"
    )


def build_import_options(
    core,
    cfg_obj,
    pages,
    *,
    max_page_complexity_units: int = 0,
):
    """Translate the shared ImportConfig vocabulary to the FreeCAD core."""
    options = core.ImportOptions(
        pages=pages,
        scale_to_mm=bool(cfg_obj.scale_to_mm),
        user_scale=float(cfg_obj.user_scale),
        flip_y=bool(cfg_obj.flip_y),
        join_tol=float(cfg_obj.join_tol),
        min_seg_len=float(cfg_obj.min_seg_len),
        curve_step_mm=float(cfg_obj.curve_step_mm),
        make_faces=bool(cfg_obj.make_faces),
        import_text=bool(cfg_obj.import_text),
        text_mode=str(cfg_obj.text_mode),
        strict_text_fidelity=bool(cfg_obj.strict_text_fidelity),
        group_by_color=bool(cfg_obj.group_by_color),
        # ImportConfig calls this assign_lineweight while ImportOptions calls
        # it assign_linewidth. Keep the host-boundary adapter explicit.
        assign_linewidth=bool(cfg_obj.assign_lineweight),
        map_dashes=bool(cfg_obj.map_dashes),
        verbose=bool(cfg_obj.verbose),
        create_top_group=bool(cfg_obj.create_top_group),
        hatch_to_faces=bool(cfg_obj.hatch_to_faces),
        hatch_mode=str(cfg_obj.hatch_mode),
        ignore_images=bool(cfg_obj.ignore_images),
        raster_fallback=bool(cfg_obj.raster_fallback),
        raster_dpi=int(cfg_obj.raster_dpi),
        import_mode=str(cfg_obj.import_mode),
        model3d_mode=str(cfg_obj.model3d_mode),
        model3d_depth_mm=float(cfg_obj.model3d_depth_mm),
        max_bezier_segments=int(cfg_obj.max_bezier_segments),
        detect_arcs=bool(cfg_obj.detect_arcs),
        arc_fit_tol_mm=float(cfg_obj.arc_fit_tol_mm),
        min_arc_angle_deg=float(cfg_obj.min_arc_angle_deg),
        arc_sampling_pts=int(cfg_obj.arc_sampling_pts),
        layer_mode=str(cfg_obj.layer_mode),
        compound_batch_size=int(cfg_obj.compound_batch_size),
        heavy_page_threshold=int(cfg_obj.heavy_page_threshold),
        max_page_complexity_units=max(0, int(max_page_complexity_units or 0)),
        auto_resolved_mode=cfg_obj.auto_resolved_mode,
        auto_reason=cfg_obj.auto_reason,
    )
    # This compatibility field is intentionally dynamic on ImportOptions, but
    # the core consumes it to decide whether semantic 3D generation runs.
    options.model3d_semantic = bool(cfg_obj.model3d_semantic)
    return options


def _looks_like_python(path_or_name: str) -> bool:
    name = Path(path_or_name).name.lower()
    return name.startswith("python") or name in ("py", "py.exe")


def _candidate_python_interpreters() -> List[str]:
    candidates: List[str] = []

    env_py = os.environ.get("BC_PDF_QA_PYTHON", "").strip()
    if env_py:
        candidates.append(env_py)

    exe = sys.executable or ""
    if exe:
        candidates.append(exe)
        try:
            bindir = Path(exe).resolve().parent
        except (OSError, RuntimeError, ValueError):
            bindir = Path(exe).parent

        for folder in (bindir, bindir / "Scripts", bindir.parent, bindir.parent / "Scripts"):
            for name in ("python.exe", "python3.exe", "python", "py.exe"):
                p = folder / name
                if p.is_file():
                    candidates.append(str(p))

    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    out: List[str] = []
    seen = set()
    for c in candidates:
        if not c:
            continue
        norm = os.path.normcase(os.path.abspath(os.path.expandvars(c)))
        if norm in seen:
            continue
        seen.add(norm)
        out.append(c)
    return out


def _run_cmd(cmd: List[str], timeout_s: int = 300) -> dict:
    kwargs = {
        "capture_output": True,
        "text": True,
        "timeout": timeout_s,
    }
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

    proc = subprocess.run(cmd, **kwargs)
    return {
        "cmd": cmd,
        "returncode": int(proc.returncode),
        "stdout_tail": (proc.stdout or "")[-1500:],
        "stderr_tail": (proc.stderr or "")[-1500:],
    }


def ensure_pymupdf(mod_dir: Optional[str]) -> dict:
    try:
        try:
            import pymupdf as fitz  # type: ignore  # noqa: F401  # PyMuPDF >= 1.24 preferred name
        except ImportError:
            import fitz  # type: ignore  # noqa: F401  # Legacy fallback
        return {"status": "available", "installed_now": False}
    except ImportError as first_exc:
        if not mod_dir:
            raise RuntimeError(f"PyMuPDF missing and mod_dir not provided: {first_exc}") from first_exc

        p = Path(mod_dir)
        if p.name.lower() == "pdfvectorimporter":
            lib_dir = p / "src" / "lib"
        else:
            lib_dir = p / "PDFVectorImporter" / "src" / "lib"

        lib_dir.mkdir(parents=True, exist_ok=True)
        if str(lib_dir) not in sys.path:
            sys.path.insert(0, str(lib_dir))

        # Try once more in case it already exists in bundled lib.
        try:
            try:
                import pymupdf as fitz  # type: ignore  # noqa: F401  # PyMuPDF >= 1.24 preferred name
            except ImportError:
                import fitz  # type: ignore  # noqa: F401  # Legacy fallback
            return {"status": "available_from_lib_dir", "installed_now": False, "lib_dir": str(lib_dir)}
        except ImportError:
            pass

        attempts = []
        interpreters = _candidate_python_interpreters()
        for py in interpreters:
            if not _looks_like_python(py):
                attempts.append({
                    "python": py,
                    "status": "skipped_non_python_exe",
                })
                continue

            step_results = []

            pip_check = None
            try:
                pip_check = _run_cmd([py, "-m", "pip", "--version"], timeout_s=120)
                step_results.append(pip_check)
            except (subprocess.SubprocessError, OSError, ValueError, RuntimeError) as exc:
                step_results.append({"cmd": [py, "-m", "pip", "--version"], "error": str(exc)})

            if not isinstance(pip_check, dict) or pip_check.get("returncode") != 0:
                try:
                    step_results.append(_run_cmd([py, "-m", "ensurepip", "--upgrade"], timeout_s=180))
                except (subprocess.SubprocessError, OSError, ValueError, RuntimeError) as exc:
                    step_results.append({"cmd": [py, "-m", "ensurepip", "--upgrade"], "error": str(exc)})

            install_result = None
            try:
                install_result = _run_cmd(
                    [
                        py,
                        "-m",
                        "pip",
                        "install",
                        "--only-binary",
                        ":all:",
                        "--disable-pip-version-check",
                        "--target",
                        str(lib_dir),
                        "PyMuPDF>=1.24,<2.0",
                    ],
                    timeout_s=600,
                )
                step_results.append(install_result)
            except (subprocess.SubprocessError, OSError, ValueError, RuntimeError) as exc:
                step_results.append(
                    {
                        "cmd": [py, "-m", "pip", "install", "--target", str(lib_dir), "PyMuPDF>=1.24,<2.0"],
                        "error": str(exc),
                    }
                )

            importlib.invalidate_caches()
            if str(lib_dir) not in sys.path:
                sys.path.insert(0, str(lib_dir))

            try:
                try:
                    import pymupdf as fitz  # type: ignore  # noqa: F401  # PyMuPDF >= 1.24 preferred name
                except ImportError:
                    import fitz  # type: ignore  # noqa: F401  # Legacy fallback
                return {
                    "status": "installed_to_lib_dir",
                    "installed_now": True,
                    "lib_dir": str(lib_dir),
                    "python": py,
                    "attempts": attempts + [{"python": py, "steps": step_results}],
                }
            except ImportError as exc:
                attempts.append(
                    {
                        "python": py,
                        "status": "import_failed_after_install",
                        "final_error": f"{exc.__class__.__name__}: {exc}",
                        "steps": step_results,
                        "install_returncode": (
                            install_result.get("returncode")
                            if isinstance(install_result, dict)
                            else None
                        ),
                    }
                )

        raise RuntimeError(
            f"PyMuPDF unavailable after install attempts. lib_dir={lib_dir}. "
            f"first_error={first_exc}. attempts={attempts}"
        ) from first_exc


def count_doc_objects(doc) -> dict:
    objects = list(getattr(doc, "Objects", []) or [])
    out = {
        "objects_total": len(objects),
        "groups": 0,
        "wires": 0,
        "faces": 0,
        "texts": 0,
    }
    for obj in objects:
        t = getattr(obj, "TypeId", "") or ""
        if "DocumentObjectGroup" in t:
            out["groups"] += 1
        if "Part::Feature" in t and hasattr(obj, "Shape"):
            try:
                sh = obj.Shape
                if getattr(sh, "Wires", None):
                    out["wires"] += len(sh.Wires)
                if getattr(sh, "Faces", None):
                    out["faces"] += len(sh.Faces)
            except (AttributeError, TypeError, RuntimeError):
                pass
        if "Text" in t:
            out["texts"] += 1
    return out


def main() -> int:
    payload_path = os.environ.get("BC_PDF_QA_PAYLOAD", "").strip()
    if not payload_path:
        raise RuntimeError("BC_PDF_QA_PAYLOAD not set")

    with open(payload_path, "r", encoding="utf-8") as f:
        payload = json.load(f)

    payload_dir = str(Path(payload_path).resolve().parent)
    result_path = payload["result_json"]
    start = time.time()

    result = {
        "test_id": payload.get("test_id"),
        "platform": "FC",
        "status": "FAIL",
        "message": "",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start)),
    }
    page_progress = None
    checkpoint_path = None

    try:
        setup_import_paths(payload, payload_dir)
        resolved_mod_dir = resolve_path(payload.get("mod_dir"), payload_dir)
        result["mod_dir"] = resolved_mod_dir
        result["sys_path_head"] = list(sys.path[:12])
        result["pymupdf"] = ensure_pymupdf(resolved_mod_dir)

        import FreeCAD  # type: ignore

        core = import_core_module()

        input_pdf = resolve_path(payload.get("input_pdf"), payload_dir)
        if not input_pdf or not os.path.isfile(input_pdf):
            raise FileNotFoundError(f"Input PDF not found: {input_pdf}")

        result.update(
            build_acceptance_identity(
                core,
                resolved_mod_dir,
                package_sha256=payload.get("package_sha256") or "",
            )
        )

        source_sha256_before = _file_sha256(input_pdf)
        requested_pages = parse_pages(payload.get("page_range"), input_pdf)
        checkpoint_path = resolve_path(payload.get("resume_checkpoint"), payload_dir)
        checkpoint = _read_page_checkpoint(checkpoint_path)
        page_progress = plan_page_cell(
            requested_pages,
            page_budget=int(payload.get("page_budget") or 0),
            source_pdf_sha256=source_sha256_before,
            checkpoint=checkpoint,
        )
        pages = list(page_progress["active_pages"])
        page_progress["status"] = "running" if pages else "complete"
        _write_page_checkpoint(checkpoint_path, page_progress)
        mode_name = (payload.get("mode") or "auto").strip().lower()
        if mode_name not in ("auto", "vector", "raster", "hybrid"):
            mode_name = "auto"
        # ImportConfig classmethod gives the consolidated parameters per
        # BCS-ARCH-001. Parameters come from the single source of truth
        # instead of obsolete per-preset dicts. Import via the same
        # multi-name strategy used for PDFImporterCore so the harness works
        # regardless of how the mod_dir is laid out on disk.
        cfg_errors = {}
        ImportConfig = None
        for _mod_name in (
            "PDFVectorImporter.src.PDFImportConfig",
            "src.PDFImportConfig",
            "PDFImportConfig",
        ):
            try:
                _cfg_mod = importlib.import_module(_mod_name)
                ImportConfig = _cfg_mod.ImportConfig
                break
            except (ImportError, ModuleNotFoundError, AttributeError) as _exc:
                cfg_errors[_mod_name] = f"{_exc.__class__.__name__}: {_exc}"
                continue
        if ImportConfig is None:
            raise RuntimeError(
                "Could not import ImportConfig from configured mod_dir. "
                f"Attempts: {cfg_errors}"
            )
        cfg_obj = getattr(ImportConfig, mode_name)()
        opts = build_import_options(
            core,
            cfg_obj,
            pages,
            max_page_complexity_units=int(
                payload.get("max_page_complexity_units") or 0
            ),
        )

        doc = FreeCAD.ActiveDocument or FreeCAD.newDocument("BC_QA")
        before = count_doc_objects(doc)

        if pages:
            ok = core.import_pdf(input_pdf, opts)
            doc.recompute()
        else:
            ok = True

        after = count_doc_objects(doc)
        delta = {k: after.get(k, 0) - before.get(k, 0) for k in after.keys()}

        source_sha256_after = _file_sha256(input_pdf)
        if source_sha256_after != source_sha256_before:
            ok = False
            result["status"] = "FAIL"
            result["message"] = "Source PDF changed during the acceptance cell."
            page_progress["status"] = "failed"
        else:
            result["status"], result["message"] = import_result_status(ok)
            if ok is True:
                page_progress = complete_page_cell(page_progress)
                if not page_progress["run_complete"]:
                    result["message"] = (
                        "Page cell completed; resume with pages "
                        f"{page_progress['next_page_range']}."
                    )
            else:
                page_progress["status"] = "failed"
        _write_page_checkpoint(checkpoint_path, page_progress)
        result["input_pdf"] = input_pdf
        result["mode"] = payload.get("mode")
        result["page_range"] = payload.get("page_range")
        result["pages"] = pages
        result["requested_pages"] = requested_pages
        result["page_progress"] = page_progress
        result["source_pdf_sha256_before"] = source_sha256_before
        result["source_pdf_sha256_after"] = source_sha256_after
        result["counts_before"] = before
        result["counts_after"] = after
        result["counts_delta"] = delta
    except (RuntimeError, OSError, ValueError, TypeError, AttributeError, ImportError) as exc:
        result["status"] = "FAIL"
        result["message"] = f"{exc.__class__.__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
        if isinstance(page_progress, dict):
            page_progress["status"] = "failed"
            page_progress["failure"] = result["message"]
            result["page_progress"] = page_progress
            try:
                _write_page_checkpoint(checkpoint_path, page_progress)
            except (OSError, ValueError, TypeError) as checkpoint_error:
                result["checkpoint_write_error"] = (
                    f"{checkpoint_error.__class__.__name__}: {checkpoint_error}"
                )
    finally:
        finish = time.time()
        result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(finish))
        result["runtime_seconds"] = round(finish - start, 3)
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)

    return result_exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
elif os.environ.get("BC_PDF_QA_PAYLOAD"):
    # FreeCADCmd may execute script files with a non-"__main__" module name.
    # In that case still run the harness when payload is explicitly provided.
    raise SystemExit(main())
