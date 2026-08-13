# Page composition guidance

These are starting points, not templates. Orchestrator chooses a coherent visual
language for the deck; Slide interprets that language for the actual evidence and
copy on one page.

## Start from the communication job

| Intent | Useful page families |
|---|---|
| establish the subject and promise | cover, image-hero, editorial-poster |
| map the argument | agenda, path, chapter-map |
| make one conclusion memorable | statement, hero-metric, quote |
| explain evidence | content, image-evidence, document-evidence |
| compare choices | comparison, matrix, matched-columns |
| explain sequence or dependency | process, timeline, roadmap, architecture |
| answer a quantitative question | data-chart, hero-metric, compact-table |
| connect situation, action, result | case-study |
| turn evidence into a decision | recommendation |
| mark a narrative turn | section-divider |
| leave a final thought or next step | closing |

The family name describes the communication job, not a finished component tree.
Avoid repeating one card grid simply because it is easy to implement. Neighboring
pages should have distinct narrative work; visual variation follows that work.

## From page family to first-draft composition

The family states the communication job; `composition` states its initial
reading path:

| Page job | Useful composition | Primary / secondary region |
|---|---|---|
| authentic scene, case, document, or product | `visual-split` | large image/evidence object / explanation and conclusion |
| data, map, mechanism, or relationship | `data-focus` | dominant chart or mechanism / concise annotation |
| two choices or states | `comparison` | peer A / peer B at the same scale |
| timeline, process, or path | `sequence` | continuous relationship / conclusion or boundary |
| checklist, matrix, or precise lookup | `matrix` | structured body / explanatory takeaway |
| strong claim, case narrative, or quote | `editorial` | claim/narrative / evidence or visual counterweight |

Do not stop at “image left, text right” or “three cards.” The Composition
blueprint names the first-glance focal point, reading path, and information job
of each region. The scaffold is an editable default ratio: Slide may reverse,
overlap, widen, or compress it, but the first draft still needs one dominant
visual or evidence event.

When the request is terse, derive page jobs from the audience questions and
evidence rather than repeating the few nouns in the prompt. Build a visual rhythm
across the deck: a strong opening Hero, authentic scenes or objects where
identity matters, diagrams or data where relationships matter, and occasional
intentional typographic rests. Variety comes from the argument, not from randomly
switching templates.

## Color and visual character

Start with a palette premise, not an industry stereotype. Name the evidence cue
behind the color—night light, local pigment, landscape, product material,
archive paper, safety signal, seasonal produce, or another subject-native
source—and assign roles for canvas, surface, ink, emphasis, data, and special
pages. A content canvas may be saturated, tinted, dark, or materially textured
when readability supports it. Do not repeatedly equate professionalism with
white/cream plus navy.

Useful directions include:

- high-chroma editorial: bold color fields, hard crops, oversized type, sharp
  image/type collisions;
- documentary field notes: authentic imagery, captions, maps, stamps, restrained
  but chromatic annotation;
- tactile print or collage: pigment, paper, halftone, cut edges, layered evidence;
- luminous technical: graphite or chromatic dark fields, signal colors, precise
  diagrams, light used as information rather than decoration;
- illustrated learning: friendly shapes, authored diagrams, warm or playful
  color relationships, clear reading order;
- typographic modernism: decisive grids, large text, asymmetric whitespace, color
  blocks carrying structure.

These are vocabulary, not presets or topic mappings. Choose one dominant
character and, when useful, one compatible supporting character. Vary crop,
scale, density, diagram form, and surface treatment inside that system; do not
use every direction in one deck or change palettes merely to make adjacent pages
look different.

## Cover

The cover title belongs to Orchestrator because it establishes the promise of the
whole deck. It may have a short subtitle when that clarifies audience, scope, or
point of view.

Choose a composition after seeing the actual title and available visual evidence:

- short title: it may be centered, but give it a meaningful image, object, SVG
  gesture, texture, or typographic counterweight so the canvas does not feel empty;
- medium title: split, lower-third, editorial, or asymmetric image/type layouts
  usually create a stronger hierarchy than a default top-left block;
- long title: break it by meaning, vary scale or weight across phrases, and reserve
  enough width. Do not simply enlarge a multi-line sentence until its lines collide.

A cover normally has one dominant Hero. Prefer an authentic subject image for
known people, places, products, works, events, or venues; use generated imagery for
an authored concept; use SVG/type when abstraction is the real subject. Pure text
is valid only when the typography itself provides sufficient visual intent.

Keep supporting metadata subordinate. A physical page number, internal code,
source line, KPI wall, or production note does not belong on the cover.

## Section divider

A divider is a pause and a promise for the next chapter, not a compressed content
page. Usually it needs:

- a numeric chapter index when the deck uses chapter numbering;
- one chapter label or eyebrow;
- a concise chapter title;
- one bridge line;
- one semantic motif or image.

Within a deck, dividers share numbering grammar, type roles, color logic, motif
treatment, and spatial character. They do not need to be pixel-identical. A deck
may alternate two related compositions—such as left-heavy and image-split—when
the variation still feels like one authored system. Different decks should derive
different divider systems from their subject and visual contract.

`section_index` is the short numeric display (`02`). A complete label such as
`CHAPTER 02` appears at most once. Do not repeat the same chapter text in the
number, eyebrow, title, motif, and footer.

The motif should have enough scale to carry the transition. Empty space is useful
when it creates tension or focus; it is not useful when all copy and imagery have
retreated to one corner. Do not place body arguments, KPI matrices, or dense
explanations on a divider.

## Closing

Unless the user explicitly asks otherwise, make the last page unmistakably an
ending. It can use a concise conclusion, thanks, Q&A invitation, or a truthful
next action. It may visually echo the cover through image, color, or motif, but
never state production language such as “echoes the cover” on the canvas.

Do not introduce a new argument, fake contact information, a dense action matrix,
or a prominent physical page number. Put recommendations or a multi-item action
set on a preceding content page; the closing may echo one established takeaway.
Sources remain in speaker notes.

## Ordinary content pages

Ordinary pages share the deck-level `--content-canvas`, header rhythm, safe area,
and footer. Slide may freely vary charts, diagrams, images, collage papers, local
panels, emphasis, and typography inside that system.

A full-canvas content background change is a narrative event, not a local styling
impulse. Declare a named `canvas_variant` in the plan when a deliberate chapter or
peak-page shift requires it. Otherwise keep the canvas continuous and vary local
surfaces.

Useful composition questions:

- What one thing should the audience understand after this page?
- Which evidence makes that conclusion credible?
- Is the relationship best seen as an image, comparison, sequence, quantity, or
  document?
- Where is the primary focus, and what can be removed or subordinated?
- Does emphasis have a visible semantic reason?

Data pages should answer one quantitative question. Comparisons need matched
dimensions. Processes need visible direction and dependency. Profiles need an
identity-bearing image and only the facts that establish relevance. Tables are for
exact lookup; if the audience only needs the pattern, prefer a chart or diagram.

Visible copy is for the audience. Keep raw URLs, source IDs, evidence codes, file
names, confidentiality filler, and design commentary in `speech.md` or trace.
