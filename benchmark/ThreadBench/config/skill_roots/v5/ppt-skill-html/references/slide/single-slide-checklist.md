<!-- Reference owner: Slide. Allowed consumers: Slide only. -->

# Single-slide checklist

Check only the assigned page. Cross-slide system consistency belongs to Designer; content/narrative ownership belongs to Presenter.

## Contract alignment

- On-screen wording, values, source line, and emphasis match `plan/slide_NN.md` and its speech beat.
- Palette, typography, imagery treatment, motif, and layout grammar match the frozen `D-xx` constraints.
- No raw Designer reference has been reinterpreted into a second local style.

## Expression and reading path

- The page communicates one primary claim and completes its semantic page role.
- The first visual focus supports the claim; supporting information follows a clear reading order.
- Alignment, spacing, and grouping make hierarchy obvious without decorative noise.
- The main visual, chart, or diagram occupies enough of the body to carry the page; it is not an isolated thumbnail.
- Cards or peer items share height, alignment, internal rhythm, and semantic styling.

## Mechanical hard gate

Resolve deterministic HARD codes before passing:

- `OVERFLOW`, `FOOTER-PUSHED`, `TEXT-OVERFLOW-BOX`, `CROWDED`: content is clipped, pushes the footer, or does not fit its container.
- `OVERLAP`, `DECOR-OVERLAP`, `FOOTER-COVER`: unintended collision or unreadable layering.
- `CUSTOM-BODY`, `ABS-LAYOUT`: the fixed skeleton or flow-layout contract is broken.
- `SVG-LABEL-OVERLAP`: diagram labels collide deterministically.
- `CONTRAST`: measured text on a solid background is below the deterministic threshold.

Also reject broken images/charts, tofu glyphs, placeholders, missing axes/labels, raw hex outside the design system, default ECharts colors, severe image crop/stretch, unreadably small copy, or an incomplete page footer.

Treat `WIDOW-LINE`, `INNER-GAP`, `IMG-LONELY`, `SVG-SMALL`, `SPARSE`, `VBALANCE`, `COVER-OOB`, and `ON-IMG-NOSCRIM` as advisory until the one required screenshot inspection confirms a clearly visible hard defect. Advisory alone never consumes another render round.

## Craft check

- Title is a short one- or two-line phrase and stays on the shared title anchor.
- Chinese and English punctuation follow the language of the sentence.
- Units are subordinate to the main number and consistent across related values.
- Text on imagery has a local scrim, plate, or outline without globally muddying the image.
- The final Closing page is a true low-density closing frame and contains no generic thanks-for-listening wording.

Pass only after rendering and visually inspecting the PNG. Code inspection alone is insufficient.
