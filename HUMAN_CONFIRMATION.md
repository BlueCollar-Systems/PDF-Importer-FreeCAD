# Human Confirmation — PDF Vector Importer (FreeCAD)

**Prep:** 2026-06-24 · See `Desktop/PDFTest Files/Q&A/QA-2026-06-24_human-confirmation-script.md`

## Setup

1. Workbench **v4.0.45+** installed.
2. `$env:BCS_CORPUS_ROOT = 'C:\1pdf-test-corpus'`
3. `python C:\1pdf-test-corpus\tools\list_tier1.py --host FC --resolved`

## Tier-1 matrix

Import each resolved Tier-1 PDF with **Labels**, **Glyphs**, and **ShapeString/3D text** where applicable. Save `import_report.json` each time.

| PDF | Labels | Glyphs | 3D text | Notes |
|-----|--------|--------|---------|-------|
| 1017 - Rev 0 | ☐ | ☐ | ☐ | Fab steel reference |
| Welding-Symbol-Chart | ☐ | ☐ | n/a | Symbol fidelity |
| hello_world_rotated | ☐ | ☐ | ☐ | Rotation |
| text_only_fontsNotEmbedded | ☐ | ☐ | ☐ | Font fallback |
| Simple PDF 2.0 | ☐ | ☐ | ☐ | Vector smoke |

## Automated

```powershell
python -m pytest tests/test_import_report_human_summary.py -q
python C:\1pdf-test-corpus\tools\list_tier1.py --host FC --resolved
```

BUILT. NOT BOUGHT.
