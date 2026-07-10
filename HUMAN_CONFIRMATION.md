# Human Verification — PDF Vector Importer (FreeCAD)

Use **your own shop PDFs** for sign-off. There is no fixed public test matrix.

## Before you start

1. Install the latest workbench release from GitHub Releases.
2. Confirm the workbench loads under **Tools → Addon Manager** or your install path.

## Checklist

For each representative shop drawing you import:

| Check | Pass |
|-------|------|
| **3D Text default** — BOM, dimensions, notes, rotation, and scale visually match the PDF at the same zoom | ☐ |
| **Labels** — editable text remains readable when selected | ☐ |
| **Glyphs/Outlines** — linework and text outlines faithful to the PDF when selected | ☐ |
| Scale plausible vs the source drawing | ☐ |
| Multi-page import behaves as expected | ☐ |

## After each import

- Save `import_report.json` from the import folder
- If something looks wrong: use [Report Doctor](https://bluecollarsystems.com/report-doctor) or **Send Feedback** with screenshots and your report JSON

## Sign-off

| Role | Name | Date | Result |
|------|------|------|--------|
| Shop tester | | | |
| Engineering | | | |

BUILT. NOT BOUGHT.
