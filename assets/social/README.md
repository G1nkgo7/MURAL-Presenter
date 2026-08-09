# Social card

`mural-og-1200x630.png` is the earlier deterministic Open Graph card and
`mural-og-fallback-1200x630.png` is the current reproducible fallback emitted by
`tools/build_logo_assets.py`. The approved launch card lives at
`assets/logo/exports/mural-social-card.png` and is copied to `site/public/og.png`; rebuilding the
logo system deliberately does not overwrite that approved raster. `mural-og-source.png` is retained
as an exploration source.

## Original exploration prompt

Create a polished 1.91:1 Open Graph launch card for an academic open-source
project called “MURAL”. Use the project mascot: a coordinated team of small white muralist robots
with dark rounded faces, cyan eyes, and warm amber painting tools, standing on
the right beside a three-panel wall whose negative space forms a subtle letter
M. Keep the left side editorial and spacious. Use an off-white paper background,
deep charcoal typography, restrained teal and amber accents, crisp flat-vector
forms with a light tactile print texture, and no gradients or photorealism.
Include exactly this text: “MURAL”; “A presentation is not a stack of slides.”;
“RESEARCH PREVIEW”. Make all lettering large, correctly spelled, and highly
legible at social-card size. No extra copy, logos, watermarks, or decorative
characters.

The image-generation output is raster artwork. Repository diagrams and wordmark
lockups are produced separately so that their labels remain deterministic.
