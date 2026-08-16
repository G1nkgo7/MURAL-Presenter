<!-- Reference owner: Audience. Allowed consumers: Audience only. -->

# Audience reception checklist

Review the finished rendered deck as the target listener after Designer and Presenter audits pass. Do not repeat their professional lanes.

## Per-page reception

- Can I state the page's message in one sentence after a short look?
- Does the first visual emphasis match what the speaker emphasizes first?
- Can I read and absorb the page within the allotted speaking time?
- Are terminology, examples, tone, and assumed knowledge appropriate for me?
- Is any important claim weakened by an obvious visual distraction or overload?

## Whole-deck reception

- What single message do I remember?
- Where did I lose the thread, misunderstand the point, or stop paying attention?
- Which evidence or example was most convincing?
- If the purpose is a decision or action, do I know what to decide or do, and why?
- Does the ending resolve the opening promise and leave the intended emphasis?

## Mechanical fallback

Report only defects a listener can directly see: clipping, overlap, broken image/chart, tofu glyph, unreadable contrast, placeholder, missing page, or clearly incomplete content. Cite lint evidence when available. Do not pixel-audit alignment or second-guess a passed design-memory constraint without new reception evidence.

## Severity and routing

- `must_fix`: visible mechanical defect or a key message that cannot be received.
- `should_fix`: clarity, emphasis, audience fit, or would-convince failure.
- `optional`: a nonessential improvement.

Route local implementation to Slide, global visual-system causes to Designer, and message/sequence/speech causes to Presenter. Recommend the smallest affected patch, never a default full rebuild.

