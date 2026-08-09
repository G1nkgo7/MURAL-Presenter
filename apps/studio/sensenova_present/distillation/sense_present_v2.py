#!/usr/bin/env python3
"""Direct adapters for the upstream SenseNova Static and Dynamic Skills.

The upstream repository is a Skill pack rather than a standalone service.  This
module supplies the small host contract they need: one canonical deck directory,
the Studio model/tools, resumable task state, and the common worker result.  It
deliberately does not invoke the upstream umbrella ``sn-ppt-entry`` Skill.
"""
from __future__ import annotations

import glob
import json
import os
import re
import traceback
from datetime import datetime, timezone
from pathlib import Path

import agent_loop
import tools


ROOT = Path(__file__).resolve().parent
PACK_ROOT = Path(
    os.environ.get(
        "SENSENOVA_PPT_V2_ROOT",
        ROOT.parent / "vendor" / "sense-present-v2",
    )
).resolve()
SKILLS_ROOT = (PACK_ROOT / "skills").resolve()

SYSTEM = """\
你是 SenseNova Present 的 PPT 制作 Agent。你在 Web Demo 的一个隔离任务目录中工作。

这是 Web Demo 对两个出口 Skill 的直接适配，不经过 sn-ppt-entry。宿主已经完成入口选择并创建
task_pack.json 和 info_pack.json。若 outline.md 尚不存在，先读取 `skills/sn-ppt-story/SKILL.md`
在同一目录生成公共大纲；随后直接读取宿主指定的 sn-ppt-standard 或 sn-ppt-dazzle，执行到交付。
不得读取、调用或转回 sn-ppt-entry，不得询问或等待确认。Web Demo 当前工作区就是唯一 DECK_DIR；
不得创建 ppt_decks、第二个 deck 目录、task_pack_v2.json 或任何副本。后续修改也必须在同一
DECK_DIR 原地完成。

工具名适配：Skill 文档中的 read_file=read、write_file=write、patch=edit、terminal=bash；
普通搜索=web_search/web_fetch，图片生成=image_generate，视觉检查=vision_analyze，子任务使用
delegate_task。所有 Skill 安装目录只读，所有交付物只写入 DECK_DIR。

Web Demo 已经表示用户同意生成和当前大纲方向，因此不要停在“等待用户确认大纲”。若可选能力
不可用，按 Skill 的降级规则继续并如实记录。每个阶段边界及时原地更新 task_pack.json，让 UI
可以实时展示进度。静态任务的 PPTX 兼容文件由 Studio 在用户点击导出时根据最终渲染图生成，
因此 `static_postprocess=[]` 时不要运行 Skill 内置的 Node PPTX 转换器；`present.html` 仍必须生成。
只有真实交付物已经生成并验证后才结束。
"""


def load_dotenv():
    """Studio owns credentials; the vendored Skill pack owns none."""
    return None


def build_config(args):
    return {
        "batch": getattr(args, "batch", "studio"),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "model": os.environ.get("MODEL", "claude-opus-4-7"),
        "anthropic_base_url": os.environ.get(
            "ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com"
        ),
        "openai_base_url": os.environ.get(
            "OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1"
        ),
        "image_model": os.environ.get("IMAGE_MODEL", "gpt-image-2"),
        "serper_base_url": os.environ.get(
            "SERPER_BASE_URL", "https://google.serper.dev"
        ),
        "max_turns": int(os.environ.get("SENSENOVA_PPT_V2_MAX_TURNS", "240")),
        "child_max_turns": int(os.environ.get("SUBAGENT_MAX_TURNS", "0")),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "32000")),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _output(seed: dict) -> str:
    value = str(seed.get("ppt_output") or seed.get("output") or "static_html")
    return value if value in {"static_html", "dynamic_html"} else "static_html"


def _richness(seed: dict) -> str:
    explicit = str(seed.get("design_richness") or "")
    if explicit in {"restrained", "rich", "high_creative"}:
        return explicit
    combined = " ".join(str(seed.get(k) or "") for k in ("query", "style", "theme"))
    if any(word in combined.lower() for word in ("炫酷", "创意", "发布会", "high creative")):
        return "high_creative"
    if any(word in combined.lower() for word in ("简洁", "克制", "学术", "政务", "restrained")):
        return "restrained"
    return "rich"


def _generation_preferences(seed: dict) -> dict:
    """Normalize Studio controls without rewriting the original query."""
    if not isinstance(seed, dict):
        return {}
    out = {
        "page_count": int(seed.get("slide_count") or seed.get("pages_hint") or 0),
        "content_theme": str(seed.get("theme") or "").strip(),
        "visual_style": str(seed.get("style") or "").strip(),
        "color_scheme": str(seed.get("scheme") or "").strip(),
        "attachment_mode": str(seed.get("attachment_mode") or "").strip(),
        "attachment_count": len(seed.get("attachments") or seed.get("attachment_paths") or []),
    }
    return {key: value for key, value in out.items() if value not in ("", 0, None)}


def _source_files(run_dir: Path, seed: dict) -> list[str]:
    """Return staged, workspace-relative user files for the upstream pack."""
    paths: list[str] = []
    manifest = run_dir / "attachments" / "manifest.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        for key in ("raw_attachments", "images"):
            for item in payload.get(key) or []:
                if isinstance(item, dict) and item.get("path"):
                    paths.append(str(item["path"]))
    paths.extend(str(path) for path in (seed.get("attachment_paths") or []) if path)
    return list(dict.fromkeys(paths))


def _bootstrap(run_dir: Path, seed: dict) -> dict:
    """Create the v2 state once; revisions preserve and update the same state."""
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "task_pack.json"
    if path.is_file():
        task = json.loads(path.read_text(encoding="utf-8"))
        output = _output(seed)
        task["deck_dir"] = str(run_dir)
        task["workspace_root"] = str(run_dir)
        task["ppt_mode"] = "standard" if output == "static_html" else "dazzle"
        task.setdefault("choices", {})["output"] = output
        task.setdefault("choices", {})["generation_preferences"] = _generation_preferences(seed)
        revision_data = seed.get("_revision") if isinstance(seed.get("_revision"), dict) else {}
        if revision_data.get("instruction"):
            task.setdefault("request", {}).setdefault("revisions", []).append({
                "revision_no": int(revision_data.get("revision_no") or 1),
                "instruction": str(revision_data["instruction"]),
                "created_at": _now(),
            })
        task.setdefault("state", {})["updated_at"] = _now()
        path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
        return task

    output = _output(seed)
    page_count = int(seed.get("slide_count") or seed.get("pages_hint") or 0)
    mode = "standard" if output == "static_html" else "dazzle"
    task = {
        "schema_version": "ppt_task_v2",
        "deck_id": run_dir.name,
        "deck_dir": str(run_dir),
        "workspace_root": str(run_dir),
        "ppt_mode": mode,
        "params": {
            "role": "presenter",
            "audience": "general",
            "scene": "presentation",
            "page_count": page_count,
            "language": seed.get("lang") or "zh-Hans",
            "image_source": "auto",
            "infographic_source": "echarts",
        },
        "request": {
            "query": str(seed.get("query") or ""),
            "source_files": _source_files(run_dir, seed),
        },
        "choices": {
            "execution_depth": str(seed.get("execution_depth") or "standard"),
            "output": output,
            "design_richness": _richness(seed),
            "generation_preferences": _generation_preferences(seed),
            # Studio owns the downloadable compatibility PPTX endpoint.  The
            # upstream Node converter requires Node >=18, while this runtime
            # deliberately stays on the existing system Node; skipping the
            # duplicate post-process keeps the Skill source immutable and the
            # generation state truthful.  HTML remains the effect baseline.
            "static_postprocess": [],
        },
        "state": {
            "status": "preparing",
            "current_stage": "entry",
            "completed_stages": [],
            "research": {"required": False, "executor": None, "mode": None},
            "capabilities": {},
            "artifacts": {},
            "last_error": None,
            "updated_at": _now(),
        },
        "created_at": _now(),
    }
    path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    info = {
        "user_query": task["request"]["query"],
        "user_assets": {
            "reference_images": [],
            "reference_image_captions": {},
            "reference_docs": task["request"]["source_files"],
            "reference_docs_failed": [],
        },
        "document_digest": {
            "topic_summary": "",
            "key_sections": [],
            "key_points": [],
            "data_highlights": [],
            "conflicts": [],
            "open_questions": [],
            "inherited_tables": [],
            "inherited_images": [],
        },
        "raw_documents": None,
        "research_report": None,
    }
    (run_dir / "info_pack.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return task


def _link_skill_tree(run_dir: Path) -> None:
    """Expose the immutable vendored Skills at the paths used by their scripts.

    The Agent's ``read`` tool understands virtual ``skills/...`` paths, while
    shell commands run with ``DECK_DIR`` as cwd.  A read-only symlink keeps both
    views consistent without copying or modifying the vendored source.  Existing
    task-owned entries are preserved so an in-place revision cannot overwrite
    user data or files left by an older run.
    """
    link = run_dir / "skills"
    if link.is_symlink() or link.exists():
        if link.is_dir():
            for name in (
                "sn-ppt-story",
                "sn-ppt-standard",
                "sn-ppt-dazzle",
                "sn-ppt-tools",
            ):
                child = link / name
                target = SKILLS_ROOT / name
                if not child.exists() and not child.is_symlink() and target.is_dir():
                    child.symlink_to(target, target_is_directory=True)
        return
    link.symlink_to(SKILLS_ROOT, target_is_directory=True)


def _initial_prompt(seed: dict, run_dir: Path, revision: bool = False) -> str:
    revision_data = seed.get("_revision") if isinstance(seed.get("_revision"), dict) else {}
    query = str(
        revision_data.get("instruction") if revision else seed.get("query") or ""
    ).strip()
    return query


def _page_count(run_dir: Path, output: str) -> int:
    if output == "static_html":
        return len(glob.glob(str(run_dir / "slides" / "slide_*.html")))
    manifest = run_dir / "deck_manifest.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return int(data.get("n_pages") or data.get("page_count") or 0)
        except (OSError, ValueError, TypeError):
            pass
    render = run_dir / "shots" / "render.json"
    if render.is_file():
        try:
            return int(json.loads(render.read_text(encoding="utf-8")).get("n_pages") or 0)
        except (OSError, ValueError, TypeError):
            pass
    html = run_dir / "deck.html"
    if html.is_file():
        text = html.read_text(encoding="utf-8", errors="ignore")
        return max(len(re.findall(r'class=["\'][^"\']*\bslide\b', text)), 1)
    return 0


def _pixel_acceptance(orch, output: str, n_pages: int) -> tuple[bool, str]:
    """Apply the pixel contract of the selected direct Skill/Harness pair."""
    if orch is None:
        return True, "not-checked"
    if output == "dynamic_html":
        calls = int(getattr(orch, "n_vision_calls", 0) or 0)
        if calls < max(1, n_pages):
            return False, f"Dazzle 逐页像素检查不足: vision {calls}/{n_pages}"
        return True, "ok"

    latest = {}
    for worker in list(getattr(orch, "worker_recs", []) or []):
        if worker.get("superseded"):
            continue
        label = str(worker.get("label") or "").lower()
        base = re.sub(r"_r\d+$", "", label)
        latest[base] = worker
    slides = [worker for label, worker in latest.items() if label.startswith("slide_")]
    if len(slides) < n_pages:
        return False, f"Static Slide 角色覆盖不足: {len(slides)}/{n_pages}"
    blind = [w.get("label") for w in slides if int(w.get("vision_calls") or 0) < 1]
    if blind:
        return False, f"Static Slide 未实际调用 vision_analyze: {blind[:5]}"
    review = latest.get("review")
    if not review or not review.get("clean"):
        return False, "Static Review 缺失或未自然完成"
    if int(review.get("vision_calls") or 0) < 1:
        return False, "Static Review 未实际调用 vision_analyze"
    return True, "ok"


def _accept(run_dir: Path, output: str, orch=None) -> tuple[bool, str, int]:
    n_pages = _page_count(run_dir, output)
    if output == "static_html":
        if not (run_dir / "present.html").is_file():
            return False, "缺少静态播放入口 present.html", n_pages
        if n_pages < 1:
            return False, "缺少静态页面 HTML", n_pages
        renders = glob.glob(str(run_dir / "renders" / "slide_*.png"))
        if len(renders) < n_pages:
            return False, f"仅渲染 {len(renders)}/{n_pages} 页", n_pages
    else:
        if not (run_dir / "deck.html").is_file():
            return False, "缺少动态播放入口 deck.html", n_pages
        if n_pages < 1:
            return False, "动态页数尚未采集", n_pages
        manifest = run_dir / "deck_manifest.json"
        render_meta = run_dir / "shots" / "render.json"
        if not manifest.is_file():
            return False, "缺少动态采集清单 deck_manifest.json", n_pages
        if not render_meta.is_file():
            return False, "缺少动态渲染检查 shots/render.json", n_pages
        try:
            meta = json.loads(render_meta.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False, "动态渲染检查文件不可读", n_pages
        if meta.get("console_errors"):
            return False, "动态演示仍有浏览器 console error", n_pages
        if int(meta.get("n_pages") or 0) != n_pages:
            return False, "动态渲染页数与采集页数不一致", n_pages
        shots = glob.glob(str(run_dir / "shots" / "page_*.png"))
        if len(shots) < n_pages:
            return False, f"仅渲染 {len(shots)}/{n_pages} 个动态页面", n_pages
    pixel_ok, pixel_reason = _pixel_acceptance(orch, output, n_pages)
    if not pixel_ok:
        return False, pixel_reason, n_pages
    return True, "ok", n_pages


def run_sample(sample_id, seed, run_dir, config):
    run_dir = Path(run_dir).resolve()
    direct_skill = str((config or {}).get("direct_skill") or "").strip()
    if direct_skill not in {"sn-ppt-standard", "sn-ppt-dazzle"}:
        raise ValueError("direct_skill must be sn-ppt-standard or sn-ppt-dazzle")
    output = "static_html" if direct_skill == "sn-ppt-standard" else "dynamic_html"
    seed = dict(seed)
    seed["ppt_output"] = output
    _bootstrap(run_dir, seed)
    _link_skill_tree(run_dir)

    # Point the existing runtime at the immutable upstream pack.  Child Agents
    # inherit these module globals and therefore read the exact same revision.
    agent_loop.SKILLS_DIR = str(SKILLS_ROOT)
    agent_loop.PPT_SKILL_DIR = str(SKILLS_ROOT / direct_skill)
    agent_loop.BASE_SYSTEM = SYSTEM
    agent_loop.MAX_SPAWN_DEPTH = max(agent_loop.MAX_SPAWN_DEPTH, 2)

    # Static is a planning-only orchestrator and must delegate all production;
    # Dazzle is intentionally one Agent and performs its own render/vision loop.
    toolsets = (
        ["file", "delegation"]
        if direct_skill == "sn-ppt-standard"
        else ["file", "terminal", "vision", "image_gen", "web"]
    )
    schema = tools.resolve_toolsets(toolsets)
    run_config = dict(config or {})
    run_config["_generation_preferences"] = _generation_preferences(seed)
    run_config["_generation_preferences"]["output_skill"] = direct_skill
    revision_data = seed.get("_revision") if isinstance(seed.get("_revision"), dict) else {}
    if revision_data:
        run_config["trace_namespace"] = (
            f"revisions/revision_{int(revision_data.get('revision_no') or 1):03d}"
        )
    orch = agent_loop.Agent(
        role="orchestrator",
        sid=sample_id,
        ws=str(run_dir),
        sub_dir="orchestrator",
        tools_schema=schema,
        config=run_config,
        initial_user=_initial_prompt(seed, run_dir, bool(seed.get("_revision"))),
        # The runner log label is part of Studio's live-progress protocol:
        # ``orchestrator`` is rendered as the deck-level conversation on page
        # 00, while delegated names (image_01, slide_01, review, ...) keep
        # their own cards.  The direct Skill remains fixed by ``direct_skill``
        # and PPT_SKILL_DIR; do not overload the UI identity with that route.
        label="orchestrator",
    )
    orch.run()
    ok, reason, n_pages = _accept(run_dir, output, orch=orch)
    return {
        "status": "completed" if ok else "rejected",
        "reason": reason,
        "n_slides": n_pages,
        "output": output,
        "entry_skill": direct_skill,
        "output_skill": direct_skill,
        "orch_exit": orch.exit_reason,
        "workers": list(orch.worker_recs),
        "vision_calls": int(getattr(orch, "n_vision_calls", 0) or 0),
        "pid": os.getpid(),
    }


def _worker(task, revision: bool = False):
    load_dotenv()
    sid = task["sample_id"]
    run_dir = Path(task["run_dir"]).resolve()
    seed = dict(task["seed"])
    config = task["config"]
    # Revision metadata is a structured record (instruction, revision number,
    # parent id).  Never collapse it to a boolean: the Agent and task_pack need
    # the original instruction to edit the same Deck in place.
    if revision and "_revision" not in seed:
        seed["_revision"] = {}
    if config.get("dry_run"):
        _bootstrap(run_dir, seed)
        return {
            "sample_id": sid,
            "run_dir": str(run_dir),
            "status": "completed",
            "dry_run": True,
            "output": _output(seed),
            "entry_skill": str(config.get("direct_skill") or ""),
            "output_skill": (
                "sn-ppt-standard" if _output(seed) == "static_html" else "sn-ppt-dazzle"
            ),
        }
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        result = run_sample(sid, seed, run_dir, config)
        return {"sample_id": sid, "run_dir": str(run_dir), **result}
    except Exception as exc:
        return {
            "sample_id": sid,
            "run_dir": str(run_dir),
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "trace": traceback.format_exc()[-3000:],
        }


def worker(task):
    return _worker(task, revision=False)


def revision_worker(task):
    return _worker(task, revision=True)
