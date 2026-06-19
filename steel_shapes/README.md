# BlueCollar Structural Steel DXF/DWG Shapes

Free structural steel DXF/DWG family packs generated from AISC v16.0 data.

> **Repository location:** this pack now lives in
> [PDF-Importer-FreeCAD](https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD)
> under `steel_shapes/`. See [ATTRIBUTION.md](ATTRIBUTION.md) for merge history.

## Folder Layout

| Path | Description |
|------|-------------|
| `dxf/` | 14 family DXF packs |
| `dwg/` | 14 family DWG packs |
| `source/` | AISC CSV and generation scripts |
| `CHECKSUMS.md` | SHA-256 values for release ZIPs |
| `LICENSE` | CC0-1.0 |

## Included Families

- 2L
- C
- HP
- HSS RECT
- HSS ROUND
- L
- M
- MC
- MT
- PIPE
- S
- ST
- W
- WT

## Files

- Family DXF packs in `dxf/`
- Family DWG packs in `dwg/`
- Source generation scripts and CSV in `source/`

## License

This repository is dedicated to the public domain under **CC0-1.0**.
You may use these files for personal, commercial, educational, and derivative work with no attribution requirement.

## Accuracy Note

These files were generated to match AISC v16.0 dimensions as closely as possible.
Use professional engineering judgment and verify final fabrication/construction decisions independently.

## Download

- Browse `dxf/` and `dwg/` for family files, or use scripts in `source/` to regenerate.
- Or download a versioned ZIP from
  [PDF-Importer-FreeCAD Releases](https://github.com/BlueCollar-Systems/PDF-Importer-FreeCAD/releases)
  (assets named `Structural-Steel-DXF-DWG-Shapes-*.zip`, tagged `steel-v*`).

## Integrity Verification

- Checksums are published in [CHECKSUMS.md](CHECKSUMS.md).
- New tagged releases (`v*`) automatically publish ZIP + `SHA256SUMS.txt` via GitHub Actions.
