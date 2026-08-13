# MURAL Presenter v0.2 Harness

This Harness pairs exactly two behaviorally equivalent instruction editions:

- Chinese: `../../skills/mural-presenter-v0.2/mural-presenter-v0-2-zh`
- English: `../../skills/mural-presenter-v0.2/mural-presenter-v0-2-en`

By default `CLEAN_MODEL_SELECT_SKILL=1`: both paths are exposed to the
Orchestrator, which selects one by its first `SKILL.md` read and then locks that
edition for the Deck. The selected instruction language does not determine the
presentation's delivery language.

v0.2 keeps the v0.1 training-compatible topology: every `Slide NN:` is an
independent child task and SlideGroup is rejected. It adds deterministic PDF
text/page extraction, optional OCR for scanned pages, guarded paper-figure
cropping, correct image-service routing, and a recoverable visual-repair ceiling.
The Harness does not introduce additional model-authored ledgers or contracts.

Run a standalone job with:

```bash
python infer.py --query "制作一份 8 页演示" --batch demo --workers 1
```

Set `CLEAN_FORCE_SKILL_LANGUAGE=zh` or `en` only for an explicit fixed-language
Harness path. WebUI jobs leave it unset so the Agent routes itself.

Default runtime limits are 4 parallel child Agents, 240/80 main/child turns,
40,960 output tokens per request, 600 seconds per model request, and 2,400
seconds of child wall time. All remain environment-overridable.
