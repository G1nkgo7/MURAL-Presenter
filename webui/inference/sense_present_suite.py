#!/usr/bin/env python3
"""WebUI adapter for the in-repository Sense Present PPT Skill suite.

The suite deliberately owns its ``ppt_decks/<deck_id>`` workspace contract.
Studio, meanwhile, expects final presentation artifacts at the job ``run_dir``.
This adapter keeps both contracts intact: it runs the suite from ``sn-ppt-entry``
and publishes stable links to the completed nested deck only after the root
agent finishes.
"""

from __future__ import annotations

import glob
import json
import os
from datetime import datetime, timezone
from pathlib import Path


SUITE_ROOT = Path(
    os.environ.get(
        "SENSENOVA_PPT_SUITE_ROOT",
        Path(__file__).resolve().parents[2] / "skills" / "sense-present-ppt-skill",
    )
).expanduser().resolve()
SKILLS_ROOT = (SUITE_ROOT / "skills").resolve()

# ``agent_loop`` resolves the read-only Skill tree at import time.
os.environ["SKILLS_DIR"] = str(SKILLS_ROOT)
# The root Agent enters explicitly through ``skills/sn-ppt-entry``. Generic
# production children are still primed with the historical ``ppt-skill`` alias,
# which must therefore resolve to the selected Static HTML output Skill.
os.environ["PPT_SKILL_DIR"] = str(SKILLS_ROOT / "sn-ppt-standard")

import agent_loop  # noqa: E402
import runtime  # noqa: E402
import tools  # noqa: E402
from attachments_runtime import build_initial_user_content  # noqa: E402


load_dotenv = runtime.load_dotenv
build_config = runtime.build_config

_CURRENT_UTC = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
_ROOT_CONTRACT = f"""
This WebUI job runs the Sense Present PPT Skill suite. Start by reading
`skills/sn-ppt-entry/SKILL.md`, then follow its routing contract and only the
suite Skills it names. The current working directory is the approved
`workspace_root`; keep the suite's canonical `ppt_decks/<deck_id>` layout.
The current time is {_CURRENT_UTC}; derive new directory timestamps from this
runtime value and never copy an illustrative timestamp from a Skill example.
The requested output for this catalog entry is Static HTML. Respect an explicit
page count exactly, including a one-page smoke. Work autonomously with the
available tools and finish only after the selected deck has rendered PNGs and
`present.html`. Do not switch to MURAL, Visual Craft, or another PPT Skill.

Routing boundary: you are the persistent root orchestrator for the whole suite.
You may delegate bounded setup, Story, Research, Image, Slide, and Review work,
but MUST NOT delegate the complete `sn-ppt-standard` output stage to one child.
After `outline.md` exists, read `skills/sn-ppt-standard/SKILL.md` yourself and
execute its orchestrator duties in this root context; only its Image / Slide /
Review production units are child tasks. A leaf child cannot recursively launch
the production agents required by the Standard contract.
Before any Slide task, create the canonical `base.css` and `plan/slide_NN.md`
required by Standard. When a page needs a generated or retrieved bitmap, finish
the Image task and publish its exact local path into the page plan before
starting that Slide; do not race an image-dependent Slide against Image.
""".strip()

_PUBLISH_NAMES = (
    "assets",
    "base.css",
    "info_pack.json",
    "outline.md",
    "plan",
    "present.html",
    "renders",
    "research",
    "slides",
    "speech.md",
    "task_pack.json",
)


def _nested_deck(root: Path) -> Path | None:
    """Select the newest canonical deck produced by the suite."""
    if (root / "slides").is_dir():
        return root
    candidates: list[Path] = []
    for task_pack in glob.glob(str(root / "ppt_decks" / "*" / "task_pack.json")):
        deck = Path(task_pack).parent
        if (deck / "slides").is_dir():
            candidates.append(deck)
    if not candidates:
        return None
    return max(candidates, key=lambda item: item.stat().st_mtime_ns)


def _publish(root: Path, deck: Path) -> None:
    """Expose nested delivery artifacts without copying or changing them."""
    if deck == root:
        return
    for name in _PUBLISH_NAMES:
        source = deck / name
        target = root / name
        if not source.exists() or os.path.lexists(target):
            continue
        target.symlink_to(source.relative_to(root), target_is_directory=source.is_dir())
    for source in sorted(deck.glob("*.pptx")):
        target = root / source.name
        if not os.path.lexists(target):
            target.symlink_to(source.relative_to(root))
    trace = root / "_trace"
    trace.mkdir(parents=True, exist_ok=True)
    (trace / "sense-present-suite.json").write_text(
        json.dumps(
            {
                "suite_root": str(SUITE_ROOT),
                "skills_root": str(SKILLS_ROOT),
                "canonical_deck_dir": str(deck),
                "entry_skill": "sn-ppt-entry",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def run_sample(sample_id: str, seed: dict, run_dir: str, config: dict) -> dict:
    """Run the generic multi-agent loop with the suite root contract."""
    agent_loop.SKILLS_DIR = str(SKILLS_ROOT)
    agent_loop.PPT_SKILL_DIR = str(SKILLS_ROOT / "sn-ppt-standard")
    agent_loop._link_skills(run_dir)
    orch = agent_loop.Agent(
        role="orchestrator",
        sid=sample_id,
        ws=run_dir,
        sub_dir="orchestrator",
        tools_schema=tools.orchestrator_tools(),
        config=config,
        initial_user=build_initial_user_content(
            seed, agent_loop._seed_to_brief(seed)
        ),
        label="orch",
    )
    orch.system = f"{orch.system.rstrip()}\n\n{_ROOT_CONTRACT}"
    orch.run()

    root = Path(run_dir).resolve()
    deck = _nested_deck(root)
    if deck is not None:
        _publish(root, deck)
    ok, reason = agent_loop._accept(orch)
    slides = sorted((root / "slides").glob("slide_*.html")) if (root / "slides").is_dir() else []
    with orch._spawn_lock:
        workers = list(orch.worker_recs)
    return {
        "status": "completed" if ok else "rejected",
        "reason": reason,
        "n_slides": len(slides),
        "n_workers": len(workers),
        "orch_exit": orch.exit_reason,
        "workers": workers,
        "skill_name": "sn-ppt-entry",
        "skill_language": "auto",
        "canonical_deck_dir": str(deck) if deck is not None else None,
        "pid": os.getpid(),
    }


# runtime.worker imports ``agent_loop.run_sample`` lazily. Replace only this
# process-local entry; the shared generic Harness implementation stays intact.
agent_loop.run_sample = run_sample
worker = runtime.worker
