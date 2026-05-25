# Host Compatibility — PDF Vector Importer (FreeCAD)

Modes are extraction **strategy** (Auto / Vector / Raster / Hybrid), not quality tiers.

## FreeCAD

| FreeCAD | Python | PyMuPDF | Status |
|---------|--------|---------|--------|
| 1.1.x | 3.11+ | >=1.24,<2.0 | ⚠️ Expected |
| 1.0.x | 3.11+ | >=1.24,<2.0 | ⚠️ Expected |
| 0.21.x | 3.10+ | >=1.24,<2.0 | ⚠️ Expected |
| 0.19–0.20 | 3.8–3.9 | legacy pin | ⚠️ Expected only after legacy branch testing |
| 0.18 and earlier | | | ❌ Not supported |

`package.xml` declares `<freecadmin>0.21</freecadmin>` — the maintained add-on baseline.
Adapter code uses PEP 604 union syntax and targets **Python 3.10+** as bundled with FreeCAD 0.21+.

### Text rendering

| Option | FreeCAD result |
|--------|----------------|
| **Labels** | Draft / native text objects |
| **3D Text** | ShapeString / extruded text |
| **Glyphs** | Vector glyph geometry |
| **Geometry** | pdftocairo outlines (non-editable) |

## CI coverage

GitHub Actions: Python matrix + FreeCAD-oriented pytest where applicable.
