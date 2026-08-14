# Fonts and type

Use this reference when the deck needs a deliberate font pairing, expressive
display type, or a stronger Chinese/Latin hierarchy.

## Contents

- Role model
- Available font families
- Pairing recipes
- CSS tokens and classes
- Chinese and bilingual handling
- Display-font safety
- Visual review

## Role model

A rich typographic system does not mean random font switching. Give each family one
job:

1. **Display** — cover, divider, hero statement, or one decisive number.
2. **Reading** — page titles, body copy, tables, charts, and captions.
3. **Accent** — quotation, cultural note, handwritten annotation, or editorial cue.
4. **Metadata** — page numbers, dates, coordinates, and compact audience-facing labels.

Most decks need only a small set of families. An expressive cultural or editorial
deck may use more when every family has a stable role.

## Activate the roles explicitly

The font vocabulary is already present in `base.css`, but it has no effect until the
Orchestrator assigns deck-wide roles:

```css
:root {
  --font-display: var(--font-serif);
  --font-body: var(--font-sans);
}
```

Do not leave `--font-display: var(--font-sans)` merely because it is the safe
default. Keep it only when neutral sans is an intentional part of the design brief.
Write the chosen display, reading, accent, and metadata roles once in the
`plan/deck.md` visual contract.

Typography follows the audience and setting before the topic label:

- formal research, policy, legal, or institutional decks: a restrained family set;
- expressive culture, exhibition, teaching, or magazine decks: a slightly richer
  set of stable roles;
- body, tables, axes, and captions: always use the reading face;
- brush, cursive, playful, or ultra-heavy faces: only for short display events.

Across a long deck, vary scale and composition before adding another family. A
divider, quote, chart, and evidence page can feel different while sharing the same
display and reading roles.

## Available font families

The current rendering environment contains these local families. Every CSS stack in
`base.css` ends with a CJK-safe fallback.

| Token | Primary families | Suitable role |
|---|---|---|
| `--font-sans` | Noto Sans SC | body, charts, tables |
| `--font-serif` | Noto Serif SC | editorial title, quotation |
| `--font-grotesque` | Archivo, Manrope | Latin title, metric |
| `--font-display-serif` | Fraunces, Spectral | magazine-style hero |
| `--font-kai` | LXGW WenKai | humanities, education, calm annotation |
| `--font-heavy` | Smiley Sans | oversized Chinese hero |
| `--font-brush` | Ma Shan Zheng | cultural hero, large artistic phrase |
| `--font-hand` | 段宁硬笔楷书粗体 | readable handwritten note |
| `--font-cursive` | 叶根友钢笔行书简体 | large expressive quotation only |
| `--font-playful` | ZCOOL KuaiLe, Lovely Little Jelly | youth, consumer, playful brand |
| `--font-mono` | IBM Plex Mono | identifiers, numbers, metadata |

When moving the Skill to a different renderer, verify these families there before
using an expressive token. The Noto-based reading stacks remain the safe fallback.

## Pairing recipes

### Research or institutional

- display: `--font-serif` or `--font-sans`;
- reading: `--font-sans`;
- metadata: `--font-mono`;
- optional accent: `--font-kai`, used once or twice.

### Technology or data

- display: `--font-heavy` for Chinese or `--font-grotesque` for Latin;
- reading: `--font-sans`;
- numbers and metadata: `--font-grotesque` plus `--font-mono`.

Keep the technical effect in scale, alignment, and annotation—not glowing text.

### Editorial or magazine

- display: `--font-display-serif`;
- Chinese title fallback: `--font-serif`;
- reading: `--font-sans` or `--font-serif` for short passages;
- metadata: `--font-grotesque` or `--font-mono`.

### Culture or humanities

- main title: `--font-serif`, `--font-kai`, or one large `--font-brush` phrase;
- reading: `--font-sans`;
- annotation: `--font-hand`;
- metadata: `--font-mono`.

### Youth or playful brand

- display: `--font-playful`;
- reading: `--font-sans`;
- optional handwritten cue: `--font-hand`;
- metadata: `--font-grotesque`.

Do not use a playful display face for paragraphs or data labels.

## Match type color to type personality

Font choice and font color are one system:

- editorial serif or kai on paper-like fields: use carbon, aubergine, forest ink,
  or another chromatic dark; avoid default navy unless the subject supports it;
- heavy grotesk on dark fields: use a softly tinted high-contrast ink, then reserve
  the saturated pigment for one word, number, or section marker;
- brush, hand, and cursive accents: borrow one pigment from the subject image or
  artifact; do not render every cultural title in generic gold;
- charts, captions, and tables: stay in the reading face and stable ink
  hierarchy even when the display title is expressive;
- image overlays: choose text from the light or dark side of the deck palette after
  inspecting the actual crop, not from a universal off-white label recipe.

Choose the display role from the subject's voice, not from a ranked list of
“premium” fonts. A catalog, classroom, incident review, research talk, and consumer
launch should not all resolve to the same serif-title/sans-body combination.

## CSS tokens and classes

Set the deck-wide roles once:

```css
:root {
  --font-display: var(--font-heavy);
  --font-body: var(--font-sans);
}
```

Then use semantic classes:

```html
<h1 class="type-hero">把复杂项目讲清楚</h1>
<p class="body-copy">一句支持主张的简短说明。</p>
<span class="type-mono">PROJECT REVIEW · 2026</span>
<strong class="metric type-grotesque">42%</strong>
```

Expressive utility classes such as `.type-brush` or `.type-display-serif` should be
local exceptions. Do not attach a different type class to every element.

Use the shared `--fs-*` tokens from `base.css`. Adjust the variables once for the
deck instead of scattering one-off sizes.

## Chinese and bilingual handling

- Keep Chinese display tracking near zero. Negative tracking that works for Latin
  often makes bold Chinese feel cramped.
- Reduce the size of long Chinese titles rather than forcing a fixed giant size.
- Use `text-wrap: balance` for titles and `text-wrap: pretty` for short body copy.
- Keep names, numbers with units, and short technical terms together with `.nowrap`.
- For bilingual titles, make one language primary. The second language should be
  smaller, lighter, or spatially separate—not an equal duplicate.
- Use a Latin display face only where it has glyphs; the CJK fallback must remain in
  the same stack.
- Tables, axes, and legends stay in the reading face.

## Display-font safety

Use brush, cursive, playful, and ultra-heavy fonts only where their silhouette is
part of the message.

- brush or cursive: normally 48px or larger;
- handwritten annotation: short and clearly readable;
- ultra-heavy Chinese: generous line height and no negative tracking;
- decorative Latin serif: verify punctuation, numerals, and CJK fallback;
- long paragraph: always use `--font-sans` or `--font-serif`.

If the font is visually distinctive but difficult to read, reduce its role rather
than adding outlines, shadows, or glow.

## Visual review

Inspect the rendered PNG and ask:

1. Can the audience identify the title, argument, and evidence immediately?
2. Does each font have one stable responsibility?
3. Is the Chinese title comfortable rather than compressed?
4. Does Latin sit naturally beside Chinese in weight and scale?
5. Are data labels and captions readable at projection distance?
6. Would removing one font make the system clearer? If yes, remove it.
7. Did the chosen display role actually appear on covers, dividers, or hero pages,
   or did every page silently fall back to neutral sans?
8. Do repeated page types use the same font roles, size ladder, and label tracking?
