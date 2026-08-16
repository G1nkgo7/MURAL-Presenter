<!-- Reference owner: Presenter. Allowed consumers: Presenter only. -->

# Narrative and speech rules

## Narrative spine

Write one thesis, then represent every page as:

```text
slide_NN — handoff:<previous takeaway → this page> | claim:<what becomes true here> | takeaway:<what remains with the audience>
```

The next page's handoff must use the previous page's takeaway. Remove duplicated claims, premature conclusions, and section dividers that do not change the audience's question.

## Co-writing rule

Write `plan/slide_NN.md` and the corresponding `speech.md` section in the same planning pass. A page plan without its speech beat is incomplete; a speech section without an on-screen anchor is unsupported.

For each page, the speech should:

- take two to five sentences unless the page is intentionally a brief anchor;
- state the page's claim in spoken language rather than reading every label;
- point attention to the intended focal information;
- explain necessary context that should not crowd the screen;
- end with a transition that creates the next page's question.

Closing has no forward transition. It should resolve the opening promise and leave one memorable final thought.

## Timing and cadence

- Record an estimated duration per page and total duration.
- Give dense evidence pages more explanation time and anchor pages less.
- Use terminology consistently; define specialist terms before relying on them.
- Keep tone appropriate to the audience and occasion.
- Do not add factual claims in speech that are absent from the knowledge brief and content contract.

## Speech-to-deck alignment

At BUILD, align the plan and draft speech before Slide work begins. At AUDIT, review the rendered deck and refine speech against what is actually visible:

- the first spoken emphasis must match the first visual emphasis;
- spoken numbers and labels must match the rendered values exactly;
- the speaker must not refer to an element that is absent or unreadable;
- transitions must still work after page-level patches;
- a slide patch that changes meaning requires a Presenter plan/memory revision before re-rendering.

## Content memory

Freeze audience and intent, thesis and acts, per-slide beats, factual invariants, narrative prohibitions, cadence, and asset bindings as `C-xx` constraints. Record every post-gate change with a revision reason and affected slides.

