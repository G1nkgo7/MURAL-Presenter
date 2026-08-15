# Workspace implementation layout

This repository has two intentionally separate implementation lines:

| Line | Purpose | Skill | Harness |
| --- | --- | --- | --- |
| MURAL Presenter | Frozen paper/release baseline, derived from the stable Long-Horizon Presenter implementation | `skills/mural-presenter/` | `harnesses/mural-presenter/` |
| MURAL Next | Private, unpublished successor experiments | `research/mural-next/skills/mural-presenter/` | `research/mural-next/harnesses/mural-presenter/` |

`webui/` does not carry an editable Skill or Harness copy. By default it resolves
the frozen pair at the repository root; MURAL Next is registered only when
`PPTAGENT_ENABLE_EXPERIMENTAL=1` and `mural-next` is explicitly made public.

Historical duplicates from `mural-work/` and `webui/bundled/` are inactive and
recoverable only from the archives under `research/mural-next/lineage/`.

The frozen baseline identity, source lineage, test counts, and content hashes are
recorded in `configs/releases/mural-paper-v1.json`.
