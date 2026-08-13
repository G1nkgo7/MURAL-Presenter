# Charts and diagrams

Read only the section needed by the assigned page. A chart or diagram must answer
the page title; it is not a decorative illustration.

## Shared rules

- Give the visual enough authority and space to answer the page title.
- Highlight one conclusion with `--accent`; keep context in ink or muted tones.
- Put labels close to the marks they describe.
- Use real units, time ranges, and baselines; carry source citations in `speech.md`.
- Prefer direct SVG and HTML over screenshots when the data is known.
- Keep line, corner, arrow, and node treatment consistent across the deck.
- Make the visual surface a tonal member of the deck field. Prefer transparent or
  token-derived backgrounds; do not place a default-white chart rectangle inside
  a tinted or dark slide.
- State the intended center of gravity and support rail in the page plan. A chart
  that technically fits but sits as a small island in a large dead region is not
  finished.

## Choose the form

| Question | Prefer |
|---|---|
| Which item is larger or smaller? | sorted bar or dot plot |
| How did a measure change? | line chart |
| Where are clusters or outliers? | scatter plot |
| What is the composition of one total? | donut for a small set of parts; stacked bar for comparison |
| What reconciles a start value to an end value? | waterfall |
| Which dimensions are relatively strong or weak? | radar with a shared scale |
| Where does volume or eligibility narrow? | funnel |
| What narrows to one gate and then expands into outputs? | hourglass |
| What exact value must be found? | table |
| What happens in order? | process or timeline |
| What must happen across time and work streams? | roadmap lanes |
| What categories contribute to one observed problem? | root-cause / fishbone |
| What depends on what? | flow or architecture diagram |
| What differs between two options? | matched comparison |

If the audience cannot state the question the visual answers, the form is not ready.

## Bar and column

Use for ranked or discrete comparison. Sort unless chronology matters. Start a
quantitative axis at zero when bar length encodes magnitude. Direct-label the
important bars; remove the legend if labels make it redundant.

## Line and area

Use for change over a continuous axis. Keep one dominant series and only the
supporting series needed for the comparison. Annotate the inflection, peak, or decision point rather than
labeling every sample.

## Scatter

Use for relationship, clusters, and outliers. State what each axis means. Emphasize
the few points that support the claim and de-emphasize the cloud.

## Donut, stacked composition, and waterfall

Use a donut for one stable total with a small, legible set of parts. Put the total or the page
claim in the center, direct-label the segments, and reserve accent for the segment
that matters. Use a 100% stacked bar when composition must be compared across
periods or groups. Use a waterfall when signed drivers reconcile a start value to
an end value. Do not use several pies where aligned bars would compare more clearly.

## Radar and quadrant

Use radar only when several dimensions share the same scale and the series remain
easy to distinguish. Pair it with a few direct readouts so the audience does not have to
estimate every vertex. Use a scatter or quadrant when the decision depends on two
independent axes; label the implication of each region rather than merely drawing
crosshairs.

## Table

Use when exact lookup matters. Right-align numbers, align decimal meaning, group
rows, and highlight one decision row or column. If the audience only needs the
pattern, convert it to a chart.

## Process

Choose one reading direction. Make stages visually unequal when their importance is
unequal. Place short explanations beside nodes. Use arrows only where direction or
dependency is real.

## Timeline and roadmap

Separate elapsed time from work streams. Mark the current point, decision gate, or
uncertainty. Do not turn every milestone into an identical card.

Choose the geometry intentionally:

- a single track for chronology;
- parallel lanes for work streams or owners;
- a staircase for capability maturity;
- `now / next / later` for directional commitments without false dates.

Vary milestone weight, connect only real dependencies, and leave room for one
decision or outcome at the end of each lane.

## Architecture

Show layers, ownership, and data flow before implementation detail. Group components
spatially, label boundaries, and keep connector crossings rare. A large box should
mean a real system boundary, not “something needed to fill space.”

## Funnel, cycle, and hierarchy

Use a funnel only when quantity or eligibility narrows at each stage. Use a cycle
only when the last stage materially feeds the first. Use a hierarchy or pyramid only
when levels differ in authority, abstraction, or dependency. Do not choose these
shapes because their silhouettes look “strategic.”

For a funnel, show the value and conversion at each narrowing step; a sequence of
equal-width chevrons is a process, not a funnel. For a cycle, use a compact set of nodes,
one reading direction, and a visible return edge. Keep labels horizontal. A central
outcome may anchor the cycle, but it must not replace the causal loop.

Use an hourglass only for a real two-sided transformation: several inputs narrow
to one decision, bottleneck, synthesis, or model, then expand into several
outputs. Label the waist explicitly and keep the upper and lower stages causally
paired. A decorative bow-tie silhouette is not evidence.

## Root-cause and flow diagrams

Use a root-cause or fishbone diagram for one observed effect with a legible set of cause
families and concise causes under each. Make the effect the strongest
label and keep branch angles and spacing regular. Use Sankey or weighted flow only
when quantities actually move between stages; otherwise use a simpler architecture
or process diagram.

## Small multiples and mixed charts

Use small multiples when the same measure must be compared across several groups
without overlapping lines. Use a mixed chart only when the series share a real
business relationship. A bar-plus-line view should state both axes and visually
separate actuals, rates, estimates, and forecasts.

## Presentation composition

- Let the chart or diagram carry the page rather than placing a detailed figure in
  a card-sized corner.
- Use a conclusion-led title, then a clear chart stage and one compact interpretation
  rail. Keep only the summary numbers that help the audience reach the conclusion.
- Keep a common chart shell across the deck—legend style, axis treatment, and
  semantic colors—while varying the data form.
- Across a long deck, vary the information mechanism, not the decorative frame.
  A ranked bar, roadmap, radar, image-led split, and root-cause page can all share
  the same field, typography, footer, and accent.
- In the rendered PNG, verify mark size, label size, occupied area, and balance.
  Expand or recompose the chart before adding decorative filler.

## SVG craft

- Set a real `viewBox` and let the SVG scale with its container.
- Use shared CSS variables for strokes, fills, and text.
- Keep labels as live text when possible.
- Use one connector weight and a small arrowhead.
- Reserve accent for the active path or conclusion.
- Avoid text on curves, tiny labels, and connector crossings.
- Check every variable inside the rendered PNG; invalid CSS variables can make
  important marks silently disappear.

## Data integrity

- Do not invent values, precision, dates, sources, or units.
- Preserve zero when bar length encodes magnitude; explain any intentionally
  truncated continuous axis.
- Distinguish observed values, estimates, forecasts, and scenarios.
- Keep the source next to the corresponding page in `speech.md`; do not expose raw
  citations or URLs on the slide unless the user explicitly asks for them.
- If data is unavailable, change the composition instead of showing fake placeholders.

## Comparison

Use the same measures and baseline on both sides. Highlight the deciding difference.
Matched columns, a shared axis, or a central trade-off rail are usually clearer than
two unrelated card piles.

## Common visual failures

- chart too small to read from projection distance;
- decorative gridlines stronger than the data;
- a rainbow palette without categorical meaning;
- arrows that do not encode flow;
- boxes with no hierarchy;
- unsupported precision or invented data;
- an insight repeated as both chart, bullets, and a large number on one page.
