# MURAL Presenter v0.1 Harness

This Harness pairs exactly two behaviorally equivalent instruction editions:

- Chinese: `../../skills/mural-presenter-v0.1/mural-presenter-v0-1-zh`
- English: `../../skills/mural-presenter-v0.1/mural-presenter-v0-1-en`

By default `CLEAN_MODEL_SELECT_SKILL=1`: both paths are exposed to the
Orchestrator, which selects one by its first `SKILL.md` read and then locks that
edition for the Deck. The selected instruction language does not determine the
presentation's delivery language.

Run a standalone job with:

```bash
python infer.py --query "制作一份 8 页演示" --batch demo --workers 1
```

Set `CLEAN_FORCE_SKILL_LANGUAGE=zh` or `en` only for an explicit fixed-language
Harness path. WebUI jobs leave it unset so the Agent routes itself.
