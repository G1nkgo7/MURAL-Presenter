# Materials and images

## Material extraction

Extract only what the presentation can use:

- facts and claims;
- numbers with units and time periods;
- tables and chart data;
- quotations with locations;
- images and captions;
- conflicts, missing definitions, and uncertainty.

Keep source locations precise enough for verification.

## Order of work

1. Parse supplied materials.
2. Decide what questions remain.
3. Research those questions.
4. Reuse material imagery.
5. Source or generate only missing imagery.

This prevents repeated searching and contradictory evidence.

## Image selection

Accept an image when:

- it supports a planned message;
- resolution is adequate for the intended crop;
- visual style fits the deck;
- source and usage are recorded;
- no visible watermark or accidental text appears.

For generated images, inspect the actual output. Do not trust the prompt alone.

## Source decision

Choose the method from the information job, not from convenience:

| Information job | Preferred source | Examples |
|---|---|---|
| Preserve supplied evidence | material reuse | attachment photo, screenshot, scanned page, supplied chart |
| Show a real identity or factual scene | sourced real image | named person, artwork, place, building, product, document, event |
| Establish a controlled non-specific mood | image generation | conceptual illustration, generic scene, atmosphere, metaphor, coordinated visual series |
| Explain data or relationships | SVG/CSS/HTML by Slide | chart, process, timeline, architecture, arrows, labels, icon system |

If the user asks for a real person or real scene, use a real sourced image. Do not
generate a documentary substitute for a named person, artwork, event, product, or
place. Do not generate charts or diagrams as raster images.

Generated images should follow one deck-level art direction and leave readable
text, labels, dates, and logos to HTML. Real images may receive a CSS tint, scrim,
duotone, or gradient overlay so they belong to the deck without losing identity.
When no raster improves the message, prefer an authored code visual for a real
relationship or an intentional typographic composition. Use none only when that
restraint strengthens the page, not by default because the request supplied no
images. Never add decoration merely to occupy space.

## Identity-bearing images

When a page is about a known person, artwork, place, product, document, or physical
object, the image carries identity rather than decoration.

- Prefer a recognizable, sourceable image over a generic silhouette or icon.
- Do not generate a documentary-looking portrait of a real person.
- Record the crop-safe region so the subject remains recognizable in the slide.
- An explicit author/person profile uses a real sourced portrait when a credible
  image is available; a generic SVG bust is not an equivalent substitute.
- Reuse one high-resolution file across several assigned slides when their crops
  remain valid; keep one catalog entry and one local path instead of downloading
  duplicate originals or derived tiles.
- If a suitable image is unavailable, report the gap and let the Orchestrator
  change the composition instead of silently inserting a placeholder.

## Catalog

Record in `assets/catalog.md`:

```markdown
## example
- slides: 4, 7
- kind: real
- path: assets/example.png
- source: https://example.com/source-page
- purpose: identity-bearing evidence for the named subject
- crop: subject right; title-safe left
- expect_transparent: false
```

`kind` is `material`, `real`, or `generated`. Slide receives the catalog and local
paths; it does not search again.
