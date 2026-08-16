# Implementation Boundary Audit

Date: 2026-07-30

Frozen implementation:
`168791d20585d3d7a667540215736c4b639a2458`.

The audit was performed against a clean archive of the commit, not against the
repository's uncommitted worktree.

## What the frozen runtime enforces

- `core/tools.py::agent_tools` assembles a fixed model-facing tool schema for
  each semantic role. The public model surface contains one `delegate_task`
  adapter; there is no current `runtime-capabilities.json`, `delegate_tasks`, or
  `delegate_slides` interface.
- The Image schema contains standard read/write/terminal/vision and web
  search/extract tools plus optional `image_generate`. It contains no custom
  `fetch_image`; real-image acquisition is delegated to the versioned
  `deck.py fetch-images` Skill command.
- `_model_write_error` enforces direct model-write ownership. A Slide may write
  only its assigned `slides/slide_NN.html`; Material only
  `research/material.md`; Research only `research/knowledge-brief.md`; Image
  model text only `assets/catalog.md`. Review may patch slides, slide plans,
  `plan/deck.md`, `base.css`, or `speech.md`.
- Material, Research, Image and Review are singleton deck-level roles. Slide
  tasks are page-indexed and executed through a bounded thread pool.
- Each synchronous child-task wave rereads the live concurrency target and
  clamps it to the pool maximum and task count. Changes apply only to later
  waves. Remote web/image calls and the fixed six-worker downloader use
  separate controls.
- Delivery acceptance requires the plan/page set, completed Research, required
  image use, render manifest/contact sheet, Review completion, and a final
  post-patch pixel view.
- The page renderer records distinct HTML/CSS states and refuses more than five
  for one page. The Slide role card is stricter than that ceiling: form one
  complete draft, aggregate visible defects into one `must_fix` list, apply one
  coordinated repair, and stop after a clean recheck.

## What is specified and audited rather than path-enforced

- A Slide is instructed to read `plan/deck.md`, its own
  `plan/slide_NN.md`, its own skeleton, assigned assets, and only the relevant
  CSS fragment when a rendered fault requires it.
- `Agent.read_path` confines ordinary reads to the workspace and selected Skill
  tree but does not implement a page-specific workspace allowlist. Peer-page
  reads must therefore be measured from traces; “bounded context” is a declared
  and audited behavior, not hard filesystem isolation.
- One Image Agent should complete raster dependencies before dependent Slides.
  The runtime has ordering checks and the build requires real referenced assets,
  but the release is not a general dynamic artifact DAG.

## Skill snapshot boundary

The Harness copies the selected Skill into each Deck workspace, and
deterministic scripts execute from that copy. Model reads whose path begins with
`skills/` currently resolve through global `config.SKILLS_DIR`. The release is
versionable and its selected Skill is restricted, but the per-Deck copy alone
does not prove immutable model instructions. Formal experiments therefore pin
the code commit and both Skill hashes. The packages remain
`ppt-skill-html-clean-zh` and `ppt-skill-html-clean-en`; “Hermes-compatible”
describes the model-facing tool interface, not a renamed Skill. Default routing
uses an explicit selection, task metadata or query language. Optional
model-first-read routing locks the edition on the first `SKILL.md` read; the
instruction edition does not determine the requested deck language.

## Corrected artifact lifecycle

The current planning source is `plan/deck.md` plus
`plan/slide_NN.md`. Orchestrator writes these Markdown files; then
`validate-plans` and `scaffold-from-plans` apply Theme Tokens, create lightweight
HTML skeletons and synchronize `speech.md`. The release does not use
`plan/pages.json` or `assets.json`, but it does contain a deterministic scaffold
stage.

## Research-to-Image handoff

Research writes the single `research/knowledge-brief.md`. During its evidence
search it preserves stable source pages and visible direct image URLs for
credible candidates already encountered, but it does not run another search
round solely to obtain a direct URL and does not download or validate the final
asset.

Image first rechecks those clues and searches only unresolved gaps. For a real
image it drafts `assets/catalog.md` with a stable `source`, transient
`download`, and local `path`, then invokes `deck.py fetch-images .`. The fixed
command validates public HTTP(S), downloads with a bounded six-worker pool,
caps file size, decodes and EXIF-transposes, resizes large inputs, and commits
files atomically. `assets-finalize` validates the result, rewrites the canonical
catalog without transient download fields, and creates the asset contact sheet.
Generated images remain controlled `image_generate` outputs. Because these
scripts mutate the workspace outside the model text-write guard, E5 records
their command and pre/post file state separately.

## Inference and trace controls

The release records the selected Skill edition, language hints, effective
child-wave concurrency, thinking flag and output effort. With thinking enabled,
the model request uses summarized adaptive thinking and retains returned
thinking blocks/signatures. These defaults are implementation facts rather
than comparison settings; E0 must pin their actual values for every system.

## Resolved E0 discrepancies

The two inconsistencies recorded on 2026-07-28 are no longer present:

- Material no longer asks to create extracted asset files and now exposes only
  reusable local image paths in `research/material.md`.
- Review's runtime allowlist now matches the role card's authority to patch
  deck and slide plans.

## Verification

From the clean commit archive:

```text
compileall: PASS
runtime regression tests: 43 passed
special-page smoke: PASS, 6 pages
```

Critical-file hashes are recorded in
`plan/release-snapshot-168791d.sha256`.

## Manuscript consequence

The paper may claim a frozen six-role topology, role-indexed model tools,
guarded direct source ownership, bounded parallel fan-out, deterministic plan
compilation/finalization, visible-defect stop policy, and whole-deck pixel
review. The visual path may be described as Research clues → Image catalog →
deterministic fetch/validation, and the runtime as Hermes-compatible, but not as
a custom model downloader or a “Hermes Clean Skill.” It must qualify local
reads, per-Deck Skill immutability, terminal/script-induced mutations,
image-dependency scheduling and the three separate concurrency controls as
described above. Quality, scaling and efficiency advantages remain pending
controlled E1–E6 evidence.
