# Agent guidance — PDF Vector Importer for FreeCAD

This repository is [BlueCollar-Systems/PDF-Importer-FreeCAD](https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD). It imports PDF vector drawings, text, layers, and images into FreeCAD 1.0+ (Python 3.10+) as native editable Part/Draft objects.

## Worktrees, coordination, and pull requests

- Work in an isolated branch and worktree created from a fresh `origin/main`. Never edit the primary checkout `C:\1PDF-Importer-FreeCAD` directly.
- Coordinate in the owner's Desktop `PDFTest Files\Q&A` folder:
  - Authoritative handoff: `MASTER_HANDOFF_ALL_7_REPOS_2026-10-27.md`
  - Active coordination log: `Achived Q&A\WORKER_STATUS_LOG.md` (append-only; claim before starting and log `DONE` when finished).
- Use pull requests for all changes to `main`. Include `[skip release]` in commit messages for documentation, test, or CI-only changes that do not ship a new add-on version.

## Standing product rules

- **Deliver, step down, report (owner ruling 2026-10-05):** One failing item never costs the page or the file. Deliver the page. When one item cannot be delivered in the requested mode, step that item down along the host's finite closest ladder (`Text -> 3D Text -> Glyphs -> Geometry -> item Raster`; `Labels -> Text -> 3D Text -> Glyphs -> Geometry -> item Raster`) and record every step-down and any item left out in `import_report.json` and the summary.
- **No silent mode switching:** Requested text mode is the deliverable. Fix position, rotation, or scale bugs inside the requested mode rather than switching representations to hide a transform bug.
- **Install & distribution facts:**
  - The Windows installer (`FreeCAD-PDF-Importer-Setup_vX.Y.Z.exe`) runs per-user (`PrivilegesRequired=lowest`, no administrator prompt) and installs into `%APPDATA%\FreeCAD\v1-1\Mod\PDFVectorImporter` (or legacy `%APPDATA%\FreeCAD\Mod\PDFVectorImporter`).
  - The workbench is **not** listed in FreeCAD's Addon Manager.
  - Releases are published via manual `auto-release.yml` workflow dispatch (`publish_release=true`), not on plain pushes to `main`.

## Local verification and locks

Run the test suites locally before opening a pull request:

```powershell
python -m pytest tests/ -v
python -m pytest PDFVectorImporter/tests -v
```

- When launching a real FreeCAD GUI or headless host check on the shared workstation, acquire the `native-cad` lock via `C:\BlueCollar-Shared\bc_lock.py` (`C:\BlueCollar-Shared\locks`, `--ram-gb 0`) and never terminate another worker's CAD process.
- Store validation artifacts and receipts under `G:\BlueCollar-Test-Results`, never on `D:`.

## Privacy gate (mandatory before commit and push)

Never commit or display customer PDFs, CAD drawings, job numbers, piece marks, company names, or personal paths. Use fictional identifiers (such as `D042-10` or `SYNTHETIC-PRIVATE-DRAWING-ID`) in tests and documentation.

Run the private privacy guard before every commit and push:

```powershell
py -3 -B -I C:\BlueCollar-Private\hooks\privacy_guard.py staged
py -3 -B -I C:\BlueCollar-Private\hooks\privacy_guard.py range origin/main..HEAD
```
