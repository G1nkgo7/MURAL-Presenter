# MuralPresenter Main-Figure v2 — Redraw Specification

Status: **proposal for author review**. The files in `approved/` remain the
current paper masters until this direction is approved and redrawn.

## 1. One visual thesis for the figure set

The figures should make one claim visible:

> MuralPresenter does not gain from splitting a workflow into more Agents; it externalizes
> durable deck state and places fresh Agent contexts around bounded, replayable,
> and visually verifiable responsibility units.

The recurring visual grammar is therefore:

- a **horizontal state rail** for durable deck decisions;
- colored **ownership bands** around pages that share one accountable Agent;
- a small **pixel-closure loop** where generated pages are rendered and inspected;
- a **whole-deck closure gate** after groups merge;
- dashed return paths for scoped revision.

The “mural” metaphor should be subtle: groups contribute distinct, compatible
tiles to one continuous deck strip. Do not add a literal painted wall, brush
texture, or decorative fresco behind a technical diagram.

## 2. Recommended figure set and page budget

Use two full-width method figures plus two later pipeline/evaluation figures.

| Figure | Section | Question answered | Recommended footprint |
| --- | --- | --- | --- |
| Fig. 1 | Introduction | Where is cross-slide responsibility closed? | full width, 3 panels |
| Fig. 2 | Method | How does MURAL carry state through generation and revision? | full width, 2-tier composite |
| Fig. 3 | Data | What is synthesized and what survives QC? | 0.8–0.95 width |
| Fig. 4 | Benchmark | What constitutes a long-horizon requirement thread? | full width or 2-column composite |

Do **not** add a third full-width architecture figure unless the page budget grows.
Production-group construction should be a detail inset in Figure 2 rather than a
standalone full-page diagram.

## 3. Figure 1 — Responsibility topology, not a generic workflow

### Caption thesis

**Three page-production topologies and where joint responsibility closes.**
Sequential execution gives one trajectory deck-wide ownership. Per-slide parallel
execution gives each worker one page even when workers receive the same blueprint.
MURAL compiles cross-slide dependencies into Production Groups, so one bounded
trajectory jointly authors and inspects related pages before whole-deck closure.

### Fair comparison

Panels (b) and (c) should share the same compact ending—**assemble → whole-deck
Review**—so the visual isolates the actual difference: page ownership versus
dependency-aligned group ownership. This also aligns Figure 1 with the matched
experimental comparison and prevents Review alone from appearing to explain the
gain.

### Panel layout

```text
(a) One deck trajectory       (b) Per-slide ownership       (c) MURAL group ownership

shared task state             shared deck blueprint         shared deck blueprint
       │                         ├── copy ──┐                   │
       ▼                         ▼    ▼    ▼              dependency compile
   one Agent                  A1    A2    AN             ┌──────┼──────┐
       │                       │     │     │              G1     G2     GK
 S1 → S2 → … → SN             S1    S2    SN             │      │      │
  one evolving trajectory      independent pages       [S1,S6] [S2,S3] [S4,S5]
       │                         └── assemble ─┘           local pixel closure
       ▼                              │                         └── assemble ─┘
 whole-deck output              whole-deck Review               whole-deck Review
```

### What must be visible

- (a) One trajectory ribbon passes through all pages. Label it `one evolving
  trajectory`, not the evaluative phrase `growing burden`.
- (b) The same blueprint is projected to workers, but each ownership bracket
  encloses exactly one page. A gray assembly/review ending is visible.
- (c) At least one group is non-contiguous, e.g. `{01, 08}` for bookends. Each
  ownership band spans multiple page thumbnails and contains one Group Agent.
- A tiny circular glyph inside each MURAL group reads `write → render → inspect →
  revise`; do not repeat four large boxes three times.
- The MURAL panel ends with one deck strip whose slide tiles retain subtle group
  edge colors, then one blue whole-deck Review gate.

### What Figure 1 must omit

- Material, Research, Image, concrete filenames, delivery formats, and the full
  human-edit router.
- A claim that all multi-Agent presentation systems literally use one Agent per
  page. Panel (b) is a controlled topology, not a universal literature taxonomy.
- Mascot costumes. One small canonical glyph per ownership unit is enough.

## 4. Figure 2 — Lifecycle, state, two-level closure, and continuation

The current four equal columns should become a **two-tier composite**. The upper
tier is the forward lifecycle. The lower tier explains group compilation and
revision routing. This makes labels larger and prevents the revision logic from
being compressed into the final quarter of the page.

### Figure 2a — Forward lifecycle backbone (top ~62%)

```text
Brief + attachments
        │
        ▼
┌────────────────┐   ┌─────────────────────────┐   ┌──────────────────┐   ┌────────────────────┐
│ Ground if needed│→ │ Compile shared deck state│→ │ Group authoring  │→ │ Deck closure       │
│ Material /      │   │ grounded knowledge       │   │ one Agent/group  │   │ assemble + contact │
│ Research        │   │ Style Lock + base.css    │   │ page-local loop   │   │ whole-deck Review  │
└────────────────┘   │ deck + page contracts     │   │ group overview    │   │ final build        │
                     │ Production Groups          │   └──────────────────┘   │ delivery           │
                     │ optional assets/catalog    │                          └────────────────────┘
                     └─────────────────────────┘
```

Use a continuous artifact rail below the stages:

```text
research/*.md → grounded-knowledge.md → design-brief.md / deck.md / slide_NN.md / base.css
              → assets/catalog.json → slides/*.html → renders/*.png → present.html + package
```

The rail is more important than the robots: it is the persistent state that lets
fresh contexts start with bounded inputs.

### Conditional roles and tools

- Material and Research use dotted borders and the label `if needed`.
- Image is downstream of frozen page plans and upstream of Slide Groups. It is
  conditional and writes verified asset IDs to `assets/catalog.json`.
- Material, Research, Image, Group, and Review may use the canonical Agent glyph.
- `stage`, `prepare`, `render`, `contact`, and `build` are small gray gear nodes,
  never robots. This visually encodes the paper's delegation criterion.
- Replace `assemble & finalize` before Review with `assemble & render`. Place
  `final build` only after Review locks final pixels, and label it `non-mutating`.

### Figure 2b — Production Group compiler (bottom-left ~20%)

Draw the deck as 10 small indexed tiles. Overlay three edge types:

- teal solid: narrative/setup–response relation;
- amber dashed: shared design DNA or special-page relation;
- gray bracket: compatible production medium/load.

The Orchestrator converts these relations into complete, non-overlapping ownership
bands. Show one non-contiguous group. A compact group brief contains:

```text
pages · design DNA · page variation · boundary handoff · asset IDs
```

Label the output **Production Groups**, not `semantic groups`. The method uses a
Skill-defined planning procedure; the figure must not imply a trained graph
partitioner or an optimal clustering algorithm.

### Figure 2c — Scope-aware revision continuation (bottom-right ~18%)

Review findings and human edits enter the same impact decision, but the executors
must respect the current editing contract:

```text
Review finding / user edit
           │
       impact scope
     ┌─────┼──────────────┐
     ▼     ▼              ▼
 bounded local       affected group(s)    fact / narrative / global style / structure
 Review quick-fix    reactivate Group     return to Orchestrator planning/grounding
     │                    │                              │
     └──────── fresh render + whole-deck re-verification ┘
```

Do not label the first branch only `local patch`, because that can imply the
Orchestrator edits a page directly. Use `Review quick-fix` or `targeted Review`.

### Delivery

One quiet final box contains `HTML · PPTX · PDF · Images`. It is an output, not a
fifth Agent. Avoid large vendor-style file icons; four compact text/icon pairs are
enough.

## 5. Figure 3 — Long-horizon data synthesis

Use no more than one Agent glyph. Most nodes are data artifacts and model calls.

```text
domain pool + audience/speaker + style pool + constraint templates
                              │
                        Query Generator
                              │
                     long-horizon query specs
                         ┌────┴────┐
                  rollout family A  rollout family B
                         └────┬────┘
                    render–inspect–refine traces
                              │
        QC: task validity · artifact quality · trajectory integrity · diversity
                              │
               accepted agentic SFT examples + rejection ledger
```

Use a funnel only at QC; do not turn the whole figure into an upward marketing
pipeline. Counts remain `[TBD]` until frozen. The output must distinguish final
artifact supervision from complete lifecycle trajectories.

## 6. Figure 4 — THREAD-Bench anatomy

The figure should explain a requirement thread rather than show a generic benchmark
leaderboard.

```text
source requirement / anchor slide ───── dependency span d ───── target slide(s)
              │                                                │
              ├── factual / terminology / narrative / design / task requirement
              │                                                │
              └──────── observable closure rule ───────────────┘

initial generation → revision 1 → revision 2 → ... → revision T
          process trace checks + final-deck checks + preservation checks
```

Use one real Gold Case as the center example only after its canonical fields are
frozen. The long horizontal thread is the signature visual; the reviewer/judge can
be a small blue Review glyph at the far right, not a row of mascot judges.

## 7. Canonical Agent visual system

### Decision

Keep a robot for brand continuity, but redesign it as a **paper-native flat glyph**.
Do not use a fully yellow robot as the universal Agent.

Why:

1. Amber/yellow is already useful for planning, selection, and the per-slide
   baseline; making every MURAL Agent yellow blurs the topology comparison.
2. Full 3D mascots, suits, hats, and different clothing become visual noise at
   two-column paper scale and make one system look like unrelated characters.
3. MURAL's core ownership color is teal; cream + teal connects the paper figures to
   the existing logo without turning the architecture into a poster.

### Base glyph

- same cream shell, black visor, cyan eyes, teal ear/hand accents;
- flat 2.5D shading: one highlight and one shadow only;
- fixed head/body ratio and fixed three-quarter viewing angle;
- no hat, suit, scarf, coat, or role-specific costume;
- transparent background and strong 1.5–2 px dark outline at source scale;
- four poses maximum: coordinate, inspect/read, author, review.

### Role encoding

| Role | Accent | One accessory | Figure use |
| --- | --- | --- | --- |
| Orchestrator | amber `#D99A2B` | route/baton glyph | one per lifecycle figure |
| Material / Research / Image | muted neutral + role icon | pages / magnifier / image card | only when the capability is active |
| Group Agent | teal `#168B84` | laptop + grouped slide stack | repeated identical glyph |
| Review | blue `#4D76B8` | magnifier/check | one whole-deck gate |
| Deterministic tool | gray `#758286` | gear only, no body | scripts and build steps |

Baseline workers in Figure 1(b) use amber jackets/chips or monochrome amber accents;
MURAL Group Agents use teal. Red never colors an Agent.

## 8. Color, type, and connector semantics

### Palette

| Meaning | Color |
| --- | --- |
| ink / outlines | `#26383D` |
| paper background | `#FBFAF6` |
| MURAL ownership / persistent state | `#168B84` |
| planning / selection / attention | `#D99A2B` |
| review / verification | `#4D76B8` |
| revision / unresolved return only | `#D85F50` |
| deterministic / neutral | `#758286` |
| cream robot shell | `#F2E7CF` |
| visor | `#142B31` |
| eyes | `#6FE3DF` |

Use very light panel tints (4–7% saturation), not gray gradients. Saturation belongs
in ownership bands, role chips, and return arrows.

### Connectors

- charcoal solid arrow: forward execution;
- teal solid rail/band: durable state or ownership;
- blue solid gate: validation;
- coral dashed arrow: replay or revision return;
- gray dotted edge: conditional stage.

### Typography

- one sans family across every figure (Inter, Archivo, or IBM Plex Sans);
- sentence case for stage names; no handwritten labels in the paper;
- minimum apparent size after placement: 8 pt for essential labels, 7 pt only for
  filenames inside the artifact rail;
- labels name objects or actions; qualifications stay in the caption.

## 9. Production format

- Editable master: PPTX or SVG with separate layers/groups for panels, artifacts,
  agents, connectors, and labels.
- Agent glyphs: one shared transparent sprite sheet; do not regenerate each pose
  independently.
- Paper exports: vector PDF/SVG plus 300 dpi PNG preview.
- Export test: place at `0.95\textwidth`, rasterize the paper page, and inspect the
  final-size labels rather than only the source canvas.

## 10. Redraw order

1. Approve the Figure 1 responsibility topology and the canonical Agent body.
2. Draw one role-sheet/contact sheet and test it at final paper size.
3. Redraw Figure 1; validate the distinction between (b) and (c).
4. Redraw Figure 2 using the same glyphs, artifact rail, and connector semantics.
5. Update bilingual captions only after the pixels are approved.
6. Then derive Figure 3 and Figure 4 from the same visual system.

Until step 3 is approved, retain the current author-approved masters and their
byte-identical distribution copies.
