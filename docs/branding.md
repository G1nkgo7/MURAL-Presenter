# MURAL brand system

MURAL uses two complementary marks.

## Primary mascot

The primary mark is the **muralist robot** painting a three-panel capital M. The robot makes the
system approachable; the M-shaped page mosaic connects the character to presentation authoring;
the raised amber paint roller represents revision as a first-class action rather than an afterthought.

Use the mascot for the GitHub avatar, project site, launch material, and large-format identity.
The transparent master is `assets/logo/mural-logo-mark-v2.png`.

## Compact mark

The compact mark is a living M assembled from page-like tiles. It retains the visor and one amber
revision tile but removes the full character. Use it below 128 px, for favicons, or where the mascot
would lose legibility. The transparent master is `assets/logo/mural-logo-mark-v1.png`.

## Palette

| Role | Hex |
| --- | --- |
| MURAL teal | `#137F7B` |
| Ink | `#263238` |
| Revision amber | `#D89A45` |
| Canvas | `#F2F8F6` |
| Dark canvas | `#172428` |

## Usage rules

- Keep clear space of at least one robot-eye width around the mark.
- Do not recolor the cyan eyes or use amber as the dominant color.
- Do not place text over the robot face, roller, or M silhouette.
- Use the compact mark for small UI surfaces instead of shrinking the mascot indefinitely.
- Do not use the mascot as evidence inside technical diagrams; the paper figures use a simplified
  line-art face derived from the same character.
- Rebuild exports with `python tools/build_logo_assets.py` after changing a source mark.

