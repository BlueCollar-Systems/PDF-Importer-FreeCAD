#!/usr/bin/env python3
"""
Cross-repository feature matrix generator.

Compares SketchUp, FreeCAD, Blender, LibreCAD importers plus the steel app,
then writes machine-readable JSON and a Markdown matrix.
"""
from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Set


REPO_KEYS = ("SU", "FC", "BL", "LC", "APP")


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    pattern: str
    applies_to: Set[str]


FEATURES: List[Feature] = [
    Feature("vector_primitives", "Vector primitive extraction", r"\b(polyline|closed_loop|circle|arc|primitive)\b", {"SU", "FC", "BL", "LC"}),
    Feature("arc_rebuild", "Arc/circle reconstruction", r"\b(arc_fit|circle fit|kasa|rebuild arcs|arc_mode)\b", {"SU", "FC", "BL", "LC"}),
    Feature("dash_mapping", "Dash pattern preservation", r"\b(dash_pattern|map_dashes|linetype)\b", {"SU", "FC", "BL", "LC"}),
    Feature("text_labels", "Text as labels", r"\btext_mode\b.{0,40}\blabels\b|\blabels\b.{0,40}\btext_mode\b", {"SU", "FC", "BL", "LC"}),
    Feature("text_geometry", "Text as geometry", r"\btext_mode\b.{0,40}\bgeometry\b|\bgeometry\b.{0,40}\btext_mode\b", {"SU", "FC", "BL", "LC"}),
    Feature("strict_text", "Strict text fidelity mode", r"\bstrict_text_fidelity\b|strict glyph fidelity", {"SU", "FC", "BL", "LC"}),
    Feature("embedded_images", "Embedded image extraction", r"\b(get_images|insert_image|import_images|image extraction)\b", {"SU", "FC", "BL", "LC"}),
    Feature("raster_only", "Raster-only mode", r"\b(raster_only|Raster Only|import_mode.{0,30}raster)\b", {"SU", "FC", "BL", "LC"}),
    Feature("hybrid_mode", "Hybrid raster+vector mode", r"\b(hybrid|raster_vector|Raster \+ Vectors)\b", {"SU", "FC", "BL", "LC"}),
    Feature("hatch_modes", "Hatch handling modes", r"\b(hatch_mode|hatching)\b", {"SU", "FC", "BL", "LC"}),
    Feature("ocg_layers", "OCG/layer mapping", r"\b(OCG|optional content group|layer_name)\b", {"SU", "FC", "BL", "LC"}),
    Feature("grouping_modes", "Advanced grouping modes", r"\b(grouping_mode|Nested: page|nested_page_)\b", {"SU", "FC", "BL", "LC"}),
    Feature("lineweight_modes", "Lineweight handling modes", r"\b(lineweight_mode|lineweight)\b", {"SU", "FC", "BL", "LC"}),
    Feature("doc_profiling", "Document profiling/classification", r"\b(document_profiler|profile_page|primary_type)\b", {"SU", "FC", "BL", "LC"}),
    Feature("scale_reference", "Scale by reference tooling", r"\b(Scale by Reference|scale tool|reference_real_mm|PDFScaleTool)\b", {"SU", "FC", "BL", "LC"}),
    Feature("cli_surface", "CLI surface", r"\b(argparse|OptionParser|cli\.(?:py|rb)|pdf2dxf\.py)\b", {"SU", "FC", "BL", "LC"}),
    Feature("gui_surface", "GUI surface", r"\b(HtmlDialog|QDialog|bpy\.types\.Operator|--gui|Tkinter)\b", {"SU", "FC", "BL", "LC"}),
    Feature("batch_import", "Batch import workflows", r"\b(Batch Import|batch import|BatchImportCommand)\b", {"SU", "FC", "BL", "LC"}),
    Feature("qa_automation", "Automated QA harness", r"\b(run_pdf_vector_importer_tests|qa_config|pytest|smoke_test|qa_smoke)\b", {"SU", "FC", "BL", "LC"}),
    Feature("all_pages_default", "Default imports all pages", r"spec is None:\s*return list\(range\(1,\s*page_count \+ 1\)\)", {"BL", "LC"}),
    Feature("app_aisc", "AISC dataset integration", r"\bAISC\b", {"APP"}),
    Feature("app_measurement_engine", "Measurement/unit parsing engine", r"\b(length_parser|1/16|unit conversions|fractional)\b", {"APP"}),
    Feature("app_svg_blueprints", "SVG blueprint rendering", r"\b(SVG|flutter_svg|shape_image_map)\b", {"APP"}),
]


def iter_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []

    if root.name == "1PDF-Importer-SketchUp":
        globs = ["README.md", "test/*.rb", "extracted/sketchup_ext/**/*.rb"]
    elif root.name == "1PDF-Importer-FreeCAD":
        globs = ["README.md", "PDFVectorImporter/**/*.py", "PDFVectorImporter/**/*.json", "PDFVectorImporter/**/*.rb", "tests/**/*.py"]
    elif root.name == "1PDF-Importer-Blender":
        globs = ["README.md", "blender_pdf_vector_importer/**/*.py", "pdf_vector_importer/**/*.py", "tests/**/*.py"]
    elif root.name == "1PDF-Importer-LibreCAD":
        globs = ["README.md", "pdfcadcore/**/*.py", "librecad_pdf_importer/**/*.py", "*.py", "tests/**/*.py"]
    else:
        globs = ["README.md", "lib/**/*.dart", "docs/**/*"]

    seen: Set[Path] = set()
    for pattern in globs:
        for p in root.glob(pattern):
            if p.is_file() and p not in seen:
                seen.add(p)
                yield p


def load_repo_text(root: Path, max_chars_per_file: int = 200_000) -> str:
    parts: List[str] = []
    for path in iter_files(root):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if len(text) > max_chars_per_file:
            text = text[:max_chars_per_file]
        parts.append(f"\n\n# FILE: {path}\n{text}")
    return "\n".join(parts)


def yes_no_na(value: str) -> str:
    if value == "yes":
        return "Yes"
    if value == "no":
        return "No"
    return "N/A"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a cross-repo feature matrix report.")
    parser.add_argument("--su", required=True, help="SketchUp importer repo path")
    parser.add_argument("--fc", required=True, help="FreeCAD importer repo path")
    parser.add_argument("--bl", required=True, help="Blender importer repo path")
    parser.add_argument("--lc", required=True, help="LibreCAD importer repo path")
    parser.add_argument("--app", required=True, help="Structural steel app repo path")
    parser.add_argument("--outdir", required=True, help="Directory to write reports")
    args = parser.parse_args()

    roots: Dict[str, Path] = {
        "SU": Path(args.su).expanduser().resolve(),
        "FC": Path(args.fc).expanduser().resolve(),
        "BL": Path(args.bl).expanduser().resolve(),
        "LC": Path(args.lc).expanduser().resolve(),
        "APP": Path(args.app).expanduser().resolve(),
    }

    blobs = {key: load_repo_text(path) for key, path in roots.items()}

    rows = []
    summary = {k: {"applicable": 0, "yes": 0, "no": 0} for k in REPO_KEYS}
    for feature in FEATURES:
        row = {"key": feature.key, "label": feature.label, "status": {}}
        rx = re.compile(feature.pattern, flags=re.IGNORECASE | re.DOTALL)
        for repo in REPO_KEYS:
            if repo not in feature.applies_to:
                row["status"][repo] = "na"
                continue
            hit = bool(rx.search(blobs[repo]))
            row["status"][repo] = "yes" if hit else "no"
            summary[repo]["applicable"] += 1
            if hit:
                summary[repo]["yes"] += 1
            else:
                summary[repo]["no"] += 1
        rows.append(row)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    json_payload = {
        "generated_at_local": timestamp,
        "repos": {k: str(v) for k, v in roots.items()},
        "summary": summary,
        "features": rows,
    }
    json_path = outdir / "feature_matrix.json"
    json_path.write_text(json.dumps(json_payload, indent=2), encoding="utf-8")

    md_lines = [
        "# Cross-Repo Feature Matrix",
        "",
        f"Generated: {timestamp}",
        "",
        "| Feature | SU | FC | BL | LC | App |",
        "|---|---|---|---|---|---|",
    ]
    for row in rows:
        s = row["status"]
        md_lines.append(
            f"| {row['label']} | {yes_no_na(s['SU'])} | {yes_no_na(s['FC'])} | {yes_no_na(s['BL'])} | {yes_no_na(s['LC'])} | {yes_no_na(s['APP'])} |"
        )

    md_lines.extend(
        [
            "",
            "## Coverage Summary",
            "",
            "| Repo | Applicable | Yes | No | Coverage |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for repo in REPO_KEYS:
        applicable = max(1, summary[repo]["applicable"])
        coverage = 100.0 * float(summary[repo]["yes"]) / float(applicable)
        md_lines.append(
            f"| {repo} | {summary[repo]['applicable']} | {summary[repo]['yes']} | {summary[repo]['no']} | {coverage:.1f}% |"
        )

    md_path = outdir / "feature_matrix.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")

    print(json.dumps({"json": str(json_path), "markdown": str(md_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
