"""Bridge from Studio to the presentation inference runtime.

Builds the per-deck job spec and the subprocess command. The engine runs under
inference's own uv env (it needs anthropic/playwright), as a separate OS
process per deck (isolation: cwd / playwright / process globals never cross).
"""
import json
import hashlib
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .db import DATA_DIR, WORKSPACES_DIR
from . import service_config
from .service_config import read_env_file as _read_env_file

STUDIO_DIR = Path(__file__).resolve().parent.parent          # .../studio
INFERENCE_DIR = Path(os.environ.get(
    "PPTAGENT_INFERENCE_ROOT",
    STUDIO_DIR.parent / ("inference" if (STUDIO_DIR.parent / "inference").is_dir() else "inference"),
)).expanduser().resolve()
PROJECT_ROOT = INFERENCE_DIR.parent
SERVE_ONE = INFERENCE_DIR / "serve_one.py"
JOBS_DIR = DATA_DIR / "jobs"
SENSE_PRESENT_V2_ROOT = INFERENCE_DIR.parent / "vendor" / "sense-present-v2"
SENSE_PRESENT_V2_SKILLS_ROOT = SENSE_PRESENT_V2_ROOT / "skills"
LOCAL_PIPELINE_ROOT = INFERENCE_DIR.parent / "vendor" / "static_ppt-clean-current"
CLEAN_PIPELINE_ROOT = Path(
    os.environ.get(
        "PPTAGENT_CLEAN_PIPELINE_ROOT",
        LOCAL_PIPELINE_ROOT,
    )
)
CLEAN_SKILLS_ROOT = CLEAN_PIPELINE_ROOT / "skills"
LONG_HORIZON_SKILLS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_LONG_HORIZON_SKILLS_ROOT",
        PROJECT_ROOT / "vendor" / "long-horizon-presenter" / "skills",
    )
)
VISUAL_CRAFT_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_VISUAL_CRAFT_SKILL_ROOT",
        PROJECT_ROOT / "vendor" / "visual-craft" / "skills" / "ppt-skill-html",
    )
)
VISUAL_CRAFT_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_VISUAL_CRAFT_HARNESS_ROOT",
        PROJECT_ROOT / "vendor" / "visual-craft",
    )
)
VISUAL_CRAFT_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_VISUAL_CRAFT_HARNESS_ENTRY", "presenter.py"
)
VISUAL_CRAFT_MOUNT_ROOT = DATA_DIR / "skill-mounts" / "visual-craft"
_BUNDLED_SN_PPT_WEB_SUITE_ROOT = (
    PROJECT_ROOT / "bundled" / "static-ppt-skill-suite"
)
_MURAL_REPOSITORY_ROOT = PROJECT_ROOT.parent
_MURAL_RESEARCH_ROOT = PROJECT_ROOT.parent / "research" / "mural-next"
MURAL_V1_RUNTIME_ENV_PATH = Path(
    os.environ.get(
        "MURAL_V1_RUNTIME_ENV_PATH",
        _MURAL_REPOSITORY_ROOT
        / "harnesses"
        / "mural-presenter-v1"
        / "runtime.env",
    )
).expanduser().resolve()
MURAL_V10_RUNTIME_ENV_PATH = Path(
    os.environ.get(
        "MURAL_V10_RUNTIME_ENV_PATH",
        _MURAL_REPOSITORY_ROOT
        / "harnesses"
        / "mural-presenter-v1.0"
        / "runtime.env",
    )
).expanduser().resolve()
MURAL_V1_RELEASE_MANIFEST_PATH = Path(
    os.environ.get(
        "MURAL_V1_RELEASE_MANIFEST_PATH",
        _MURAL_REPOSITORY_ROOT / "configs" / "releases" / "mural-paper-v1.json",
    )
).expanduser().resolve()
MURAL_V10_RELEASE_MANIFEST_PATH = Path(
    os.environ.get(
        "MURAL_V10_RELEASE_MANIFEST_PATH",
        _MURAL_REPOSITORY_ROOT
        / "configs"
        / "releases"
        / "mural-paper-v1.0-rollback-20260903-r43.json",
    )
).expanduser().resolve()
MURAL_LEGACY_ARCHIVE_ROOT = (
    _MURAL_REPOSITORY_ROOT / "archive" / "mural-presenter-pre-v0.6"
)


# --- external visual critic switch (mural-presenter-v1) ---
# The v1 harness gates the independent visual critic behind
# ENABLE_EXTERNAL_VISUAL_CRITIC ("1"/"0"), read by
# harnesses/mural-presenter-v1/runtime/production.py. Both inference and
# synthesis honor the same explicit opt-in (default off).
def external_visual_critic_env(requested: bool, run_mode: str) -> dict:
    """Return the child-env fragment toggling the independent visual critic.

    ``run_mode`` remains in the signature for callers that pin it in run metadata.
    """
    return {"ENABLE_EXTERNAL_VISUAL_CRITIC": "1" if bool(requested) else "0"}


def _mural_v1_runtime_defaults(
    path: Path = MURAL_V1_RUNTIME_ENV_PATH,
) -> dict[str, str]:
    """Read the canonical non-secret v1 behavior profile."""
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values
SENSE_PRESENT_SUITE_ROOT = Path(
    os.environ.get(
        "PPTAGENT_SENSE_PRESENT_SUITE_ROOT",
        _MURAL_REPOSITORY_ROOT / "skills" / "sense-present-ppt-skill",
    )
)
SENSE_PRESENT_SUITE_SKILLS_ROOT = SENSE_PRESENT_SUITE_ROOT / "skills"
SENSE_PRESENT_SUITE_HARNESS_ENTRY = "sense_present_suite.py"
VISUAL_CRAFT_V3_PRE_SPEECH_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_VISUAL_CRAFT_V3_PRE_SPEECH_SKILL_ROOT",
        _MURAL_REPOSITORY_ROOT / "skills" / "visual-craft-v3-pre-speech",
    )
)
VISUAL_CRAFT_V3_PRE_SPEECH_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_VISUAL_CRAFT_V3_PRE_SPEECH_HARNESS_ROOT",
        _MURAL_REPOSITORY_ROOT / "harnesses" / "visual-craft-v3-pre-speech",
    )
)
VISUAL_CRAFT_V3_SPEECH_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_VISUAL_CRAFT_V3_SPEECH_SKILL_ROOT",
        _MURAL_REPOSITORY_ROOT / "skills" / "visual-craft-v3-speech",
    )
)
VISUAL_CRAFT_V3_SPEECH_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_VISUAL_CRAFT_V3_SPEECH_HARNESS_ROOT",
        _MURAL_REPOSITORY_ROOT / "harnesses" / "visual-craft-v3-speech",
    )
)
VISUAL_CRAFT_V3_HARNESS_ENTRY = "distill_ppt.py"
VISUAL_CRAFT_V3_MOUNT_ROOT = DATA_DIR / "skill-mounts"
# Legacy compatibility only. Keep this path local so a consolidated checkout
# never silently reaches into another editable suite on AFS.
_DEFAULT_SN_PPT_WEB_SUITE_ROOT = _BUNDLED_SN_PPT_WEB_SUITE_ROOT
SN_PPT_WEB_SUITE_ROOT = Path(
    os.environ.get(
        "PPTAGENT_SN_PPT_WEB_SUITE_ROOT",
        _DEFAULT_SN_PPT_WEB_SUITE_ROOT,
    )
)
SN_PPT_WEB_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_SN_PPT_WEB_SKILL_ROOT",
        SN_PPT_WEB_SUITE_ROOT / "skills" / "sn-ppt-web",
    )
)
SN_PPT_WEB_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_SN_PPT_WEB_HARNESS_ROOT",
        SN_PPT_WEB_SUITE_ROOT / "harnesses" / "sn-ppt-web",
    )
)
SN_PPT_WEB_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_SN_PPT_WEB_HARNESS_ENTRY", "presenter.py"
)
MURAL_PRESENTER_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_PRESENTER_SKILL_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "skills" / "mural-presenter",
    )
)
MURAL_PRESENTER_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_PRESENTER_HARNESS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "harnesses" / "mural-presenter",
    )
)
MURAL_PRESENTER_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_PRESENTER_HARNESS_ENTRY", "infer.py"
)
MURAL_V02_SKILLS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V02_SKILLS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "skills" / "mural-presenter-v0.2",
    )
)
MURAL_V02_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V02_HARNESS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "harnesses" / "mural-presenter-v0.2",
    )
)
MURAL_V02_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_V02_HARNESS_ENTRY", "infer.py"
)
MURAL_V03_SKILLS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V03_SKILLS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "skills" / "mural-presenter-v0.3",
    )
)
MURAL_V03_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V03_HARNESS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "harnesses" / "mural-presenter-v0.3",
    )
)
MURAL_V03_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_V03_HARNESS_ENTRY", "infer.py"
)
MURAL_V04_SKILLS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V04_SKILLS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "skills" / "mural-presenter-v0.4",
    )
)
MURAL_V04_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V04_HARNESS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "harnesses" / "mural-presenter-v0.4",
    )
)
MURAL_V04_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_V04_HARNESS_ENTRY", "infer.py"
)
# v0.4-thin: fork of v0.4 with every in-flight process gate stripped
# (20+ tools.py gates + 13 pre-dispatch elif branches + review_required_view_gap
# stale-hash branch). Skill package is unchanged — the thin harness reuses the
# same v0.4 skill, only the harness behavior differs. Data synthesis (student
# distillation, seed generation) should default here; the v0.4 main harness is
# kept as the historical baseline for regression comparisons.
MURAL_V04_THIN_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V04_THIN_HARNESS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "harnesses" / "mural-presenter-v0.4-thin",
    )
)
# v0.5: visual-first iteration. New skill package (flex-column base.css, arch-*
# layout primitives, Style Lock, higher slide autonomy) but REUSES the v0.4
# harness verbatim — the harness's current-variant gating derives from
# CLEAN_SKILL_NAME, so pointing it at mural-presenter-v0-5 auto-follows.
MURAL_V05_SKILLS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V05_SKILLS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "skills" / "mural-presenter-v0.5",
    )
)
MURAL_V05_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V05_HARNESS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "harnesses" / "mural-presenter-v0.4",
    )
)
# v1.0 — paper/data-synthesis release with thin runtime policy, native
# in-context vision, persistent semantic Owners, and complete synthesis traces.
# The previous v0.6 pair remains on disk as an immutable rollback snapshot.
MURAL_V10_SKILLS_ROOT = Path(
    os.environ.get("PPTAGENT_MURAL_V10_SKILLS_ROOT")
    or os.environ.get("PPTAGENT_MURAL_V06_SKILLS_ROOT")
    or _MURAL_REPOSITORY_ROOT / "skills"
)
MURAL_V10_HARNESS_ROOT = Path(
    os.environ.get("PPTAGENT_MURAL_V10_HARNESS_ROOT")
    or os.environ.get("PPTAGENT_MURAL_V06_HARNESS_ROOT")
    or _MURAL_REPOSITORY_ROOT / "harnesses" / "mural-presenter-v1.0"
)
MURAL_V1_HARNESS_ROOT = Path(
    os.environ.get("PPTAGENT_MURAL_V1_HARNESS_ROOT")
    or _MURAL_REPOSITORY_ROOT / "harnesses" / "mural-presenter-v1"
)
MURAL_V05_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_V05_HARNESS_ENTRY", "infer.py"
)
MURAL_V10_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_V10_HARNESS_ENTRY",
    os.environ.get("PPTAGENT_MURAL_V06_HARNESS_ENTRY", "infer.py"),
)
MURAL_V1_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_V1_HARNESS_ENTRY", "infer.py"
)

# Ablation: single-agent (solo) skill. Same harness + same references as v0.4;
# only SKILL.md/roles differ (no delegation). Solo behaviour is driven by
# CLEAN_SOLO_MODE=1 in its runtime_env.
MURAL_V04_SOLO_SKILLS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V04_SOLO_SKILLS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "skills" / "mural-presenter-v0.4-solo",
    )
)
# Ablation: no-skill (nk) — same harness + files present for readiness, but the
# SKILL.md forbids reading design references. Used as the second axis of a 2×2
# matrix (with-skill vs no-skill) × (multi-agent vs single-agent solo).
MURAL_V04_NK_SKILLS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_V04_NK_SKILLS_ROOT",
        MURAL_LEGACY_ARCHIVE_ROOT / "skills" / "mural-presenter-v0.4-nk",
    )
)
LONG_HORIZON_PRESENTER_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_LONG_HORIZON_PRESENTER_SKILL_ROOT",
        MURAL_PRESENTER_SKILL_ROOT,
    )
)
LONG_HORIZON_PRESENTER_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_LONG_HORIZON_PRESENTER_HARNESS_ROOT",
        MURAL_PRESENTER_HARNESS_ROOT,
    )
)
LONG_HORIZON_PRESENTER_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_LONG_HORIZON_PRESENTER_HARNESS_ENTRY", "infer.py"
)
MURAL_NEXT_SKILL_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_NEXT_SKILL_ROOT",
        _MURAL_RESEARCH_ROOT / "skills" / "mural-presenter",
    )
)
MURAL_NEXT_HARNESS_ROOT = Path(
    os.environ.get(
        "PPTAGENT_MURAL_NEXT_HARNESS_ROOT",
        _MURAL_RESEARCH_ROOT / "harnesses" / "mural-presenter",
    )
)
MURAL_NEXT_HARNESS_ENTRY = os.environ.get(
    "PPTAGENT_MURAL_NEXT_HARNESS_ENTRY", "distill_ppt.py"
)
ENABLE_EXPERIMENTAL_SKILLS = (
    os.environ.get("PPTAGENT_ENABLE_EXPERIMENTAL", "0") == "1"
)
ENGINE_SITE_PACKAGES = Path(
    os.environ.get(
        "PPTAGENT_ENGINE_SITE_PACKAGES",
        DATA_DIR / "engine-runtime" / "site-packages",
    )
)
CHROMIUM_DEPS_DIR = Path(os.environ.get("PPTAGENT_CHROMIUM_DEPS", DATA_DIR / "chromium-deps"))
# vLLM serve 把当前端点写进哨兵文件(serve 重起→IP 变→文件自动更新)。studio 派发时优先读它,
# 读不到/不合法再退回 MODELS 里硬编码的 base_url。根目录可用 PPTAGENT_SERVE_SENTINEL_DIR 覆盖。
SERVE_SENTINEL_DIR = Path(
    os.environ.get("PPTAGENT_SERVE_SENTINEL_DIR", DATA_DIR / "serve-sentinels")
)


def _hash_runtime_tree(digest: "hashlib._Hash", root: Path, *, prefix: str = "") -> None:
    """Hash runtime sources without walking generated runs or caches.

    Harness roots contain large synthesis workspaces.  They are outputs, not
    executable inputs; hashing them on every catalog refresh both changes the
    source revision for the wrong reason and can block Studio requests on AFS.
    """
    ignored_dirs = {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "artifacts",
        "canary",
        "logs",
        "node_modules",
        "outputs",
        "runs",
        "tests",
    }
    files: list[Path] = []
    for current, directories, names in os.walk(root, topdown=True):
        directories[:] = sorted(name for name in directories if name not in ignored_dirs)
        base = Path(current)
        files.extend(
            base / name
            for name in sorted(names)
            if name not in ignored_dirs and not name.endswith(".pyc")
        )
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        digest.update(prefix.encode("utf-8"))
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())

def _clean_skill(language: str, label: str) -> dict:
    name = f"ppt-skill-html-clean-{language}"
    return {
        "label": label,
        "path": str(CLEAN_SKILLS_ROOT / name),
        "name": name,
        "language": language,
        "mode": "clean-bilingual",
        "status": "current",
        "ready": True,
        "pipeline": "infer",
        "harness_path": str(CLEAN_PIPELINE_ROOT),
        "harness_entry": "infer.py",
        "pairing": "clean-current",
        "caps": ["attachments", "revision"],
    }


# Auto 是 Studio 的建任务策略：main.py 会在入队前按 query/JSONL lang 解析成 zh/en。
# 这里保留未绑定语言目录的定义，仅用于兼容历史数据库中的 auto 任务。
def _auto_skill() -> dict:
    return {
        "label": "Auto（自动选择）",
        "path": str(CLEAN_SKILLS_ROOT),
        "name": None,
        "language": "auto",
        "mode": "clean-bilingual",
        "status": "current",
        "ready": True,
        "pipeline": "infer",
        "harness_path": str(CLEAN_PIPELINE_ROOT),
        "harness_entry": "infer.py",
        "pairing": "clean-current",
        "caps": ["attachments"],
    }


def _long_horizon_skill(
    name: str = "long-horizon-html-ppt",
    label: str = "Long-horizon HTML PPT",
    pairing: str = "long-horizon-current",
    skills_root: Path = LONG_HORIZON_SKILLS_ROOT,
    inline_image: bool = False,
) -> dict:
    path = skills_root / name
    # These files form the minimum runnable contract.  The revision below is
    # intentionally calculated from the *entire* source tree so newly added
    # role/reference/script files are picked up without another Studio change.
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/base.css",
        "scripts/deck.py",
        "scripts/font_bundle.py",
        "scripts/image_background.py",
        "scripts/render_deck.py",
        "scripts/workspace_policy.py",
        "roles/material.md",
        "roles/research.md",
        "roles/slide.md",
        "roles/review.md",
        "references/materials-and-images.md",
        "references/html-contract.md",
        "references/fonts-and-type.md",
        "references/charts-and-diagrams.md",
        "references/plan-contract.md",
        "references/page-patterns.md",
        "references/visual-direction.md",
    ) + (
        # The current Grouped Skill deliberately keeps the root Orchestrator
        # contract in SKILL.md (its "唯一根工作流") instead of duplicating it
        # in a role card. Other Long-horizon variants still use the separate
        # role file and must continue to validate it.
        () if name == "long-horizon-html-ppt-grouped"
        else ("roles/orchestrator.md",)
    ) + (
        ("references/slide-image-routing.md",)
        if inline_image
        else ("roles/image.md",)
    )
    missing_files = [relative for relative in required_files if not (path / relative).is_file()]
    source_files = sorted(
        file.relative_to(path).as_posix()
        for file in path.rglob("*")
        if file.is_file()
        and file.suffix != ".pyc"
        and "__pycache__" not in file.parts
        and not any(part.startswith(".") for part in file.relative_to(path).parts)
    ) if path.is_dir() else []
    digest = hashlib.sha256()
    if not missing_files:
        for relative in source_files:
            digest.update(relative.encode("utf-8"))
            digest.update((path / relative).read_bytes())
    ready = not missing_files
    return {
        "label": label,
        "path": str(path),
        "skills_root": str(skills_root),
        "name": name,
        # Skill instructions are Chinese, while the delivered deck follows the query language.
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "long-horizon",
        "status": "current",
        "ready": ready,
        "unavailable_reason": (
            f"{name} 缺少：" + ", ".join(missing_files)
            if missing_files else ""
        ),
        "required_files": list(required_files),
        "source_files": source_files,
        "source_file_count": len(source_files),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "infer",
        "harness_path": str(CLEAN_PIPELINE_ROOT),
        "harness_entry": "infer.py",
        "pairing": pairing,
        "caps": ["attachments"],
    }


def _visual_craft_mount(skill_root: Path = VISUAL_CRAFT_SKILL_ROOT) -> tuple[Path, str]:
    """Expose the direct snapshot as ``skills/ppt-skill-html`` for its Harness.

    The legacy catalog stores one Skill at a path named ``skill`` while the
    paired Harness deliberately resolves ``PPT_SKILLS_ROOT/ppt-skill-html``.
    A tiny Studio-owned symlink preserves that contract without copying or
    modifying the externally owned snapshot.
    """
    mount_root = VISUAL_CRAFT_MOUNT_ROOT
    link = mount_root / "ppt-skill-html"
    try:
        mount_root.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(link):
            if link.is_symlink() and link.resolve(strict=False) == skill_root.resolve(strict=False):
                return mount_root, ""
            if link.is_symlink():
                # This is a Studio-owned compatibility mount. The configured
                # canonical snapshot may change, so update only this symlink;
                # never replace a real directory or file at the mount point.
                link.unlink()
                link.symlink_to(skill_root, target_is_directory=True)
                return mount_root, ""
            return mount_root, f"Skill 挂载点已被其他文件占用：{link}"
        link.symlink_to(skill_root, target_is_directory=True)
        return mount_root, ""
    except OSError as exc:
        return mount_root, f"无法创建 Skill 挂载点：{exc}"


def _visual_craft_pipeline_contract(
    harness_root: Path = VISUAL_CRAFT_HARNESS_ROOT,
    entry: str = VISUAL_CRAFT_HARNESS_ENTRY,
) -> tuple[bool, str, list[str]]:
    required = (
        entry,
        "core/__init__.py",
        "core/agent.py",
        "core/tools.py",
        "core/trace.py",
    )
    if not os.access(harness_root, os.R_OK | os.X_OK):
        return False, f"Visual Craft Harness 目录无读取/遍历权限：{harness_root}", list(required)
    missing = [relative for relative in required if not (harness_root / relative).is_file()]
    if missing:
        return False, "Visual Craft Harness 缺少或无权读取：" + ", ".join(missing), list(required)
    return True, "", list(required)


def _visual_craft_skill(
    skill_root: Path = VISUAL_CRAFT_SKILL_ROOT,
    harness_root: Path = VISUAL_CRAFT_HARNESS_ROOT,
) -> dict:
    """Register the legacy visual-first Skill under a product-facing name."""
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "references/base-template.css",
        "references/design-rules.md",
        "references/design-styles.md",
        "references/fonts.md",
        "references/layout-patterns.md",
        "references/quality-checklist.md",
        "scripts/render.py",
        "scripts/font_bundle.py",
        "scripts/build_player.py",
        "scripts/stage_materials.py",
        "subagents/research.md",
        "subagents/material.md",
        "subagents/image.md",
        "subagents/slide.md",
        "subagents/review.md",
    )
    missing_skill = [relative for relative in required_files if not (skill_root / relative).is_file()]
    harness_ready, harness_reason, harness_required = _visual_craft_pipeline_contract(harness_root)
    mount_root, mount_reason = _visual_craft_mount(skill_root)
    reasons = []
    if missing_skill:
        reasons.append("Visual Craft Skill 缺少或无权读取：" + ", ".join(missing_skill))
    if harness_reason:
        reasons.append(harness_reason)
    if mount_reason:
        reasons.append(mount_reason)

    snapshot = {}
    try:
        snapshot = json.loads((skill_root / ".snapshot.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    digest = hashlib.sha256()
    if not missing_skill:
        for path in sorted(
            (item for item in skill_root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(skill_root).as_posix(),
        ):
            relative = path.relative_to(skill_root).as_posix()
            if any(
                part in {".venv", "__pycache__", ".pytest_cache"}
                for part in path.parts
            ) or path.suffix == ".pyc":
                continue
            digest.update(relative.encode("utf-8"))
            digest.update(path.read_bytes())
    ready = not missing_skill and harness_ready and not mount_reason
    return {
        "label": "Visual Craft HTML PPT",
        "path": str(skill_root),
        "skills_root": str(mount_root),
        "name": "ppt-skill-html",
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "visual-craft",
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": list(required_files),
        "source_revision": (
            str(snapshot.get("tree_hash") or "")[:12]
            or digest.hexdigest()[:12]
        ),
        "snapshot_id": str(snapshot.get("id") or ""),
        "pipeline": "visual-craft-harness",
        "harness_path": str(harness_root),
        "harness_entry": VISUAL_CRAFT_HARNESS_ENTRY,
        "harness_required_files": harness_required,
        "pairing": "visual-craft-paired",
        "caps": ["attachments", "revision"],
    }


def _visual_craft_v3_mount(skill_key: str, skill_root: Path) -> tuple[Path, str]:
    """Mount one V3 snapshot at the legacy Harness path ``ppt-skill-html``."""
    mount_root = VISUAL_CRAFT_V3_MOUNT_ROOT / skill_key
    link = mount_root / "ppt-skill-html"
    try:
        mount_root.mkdir(parents=True, exist_ok=True)
        if os.path.lexists(link):
            if link.is_symlink() and link.resolve(strict=False) == skill_root.resolve(strict=False):
                return mount_root, ""
            if not link.is_symlink():
                return mount_root, f"Skill 挂载点已被其他文件占用：{link}"
            link.unlink()
        link.symlink_to(skill_root, target_is_directory=True)
        return mount_root, ""
    except OSError as exc:
        return mount_root, f"无法创建 Skill 挂载点：{exc}"


def _visual_craft_v3_skill(*, speech: bool) -> dict:
    """Register the paired pre-speech or speech V3 snapshot."""
    skill_key = "visual-craft-v3-speech" if speech else "visual-craft-v3-pre-speech"
    label = "Visual Craft V3 · Speech" if speech else "Visual Craft V3 · Pre-Speech"
    skill_root = (
        VISUAL_CRAFT_V3_SPEECH_SKILL_ROOT
        if speech else VISUAL_CRAFT_V3_PRE_SPEECH_SKILL_ROOT
    )
    harness_root = (
        VISUAL_CRAFT_V3_SPEECH_HARNESS_ROOT
        if speech else VISUAL_CRAFT_V3_PRE_SPEECH_HARNESS_ROOT
    )
    required_skill = [
        "SKILL.md",
        "agents/openai.yaml",
        "references/base-template.css",
        "references/design-rules.md",
        "references/design-styles.md",
        "references/fonts.md",
        "references/layout-patterns.md",
        "references/quality-checklist.md",
        "scripts/build_player.py",
        "scripts/render.py",
        "scripts/stage_materials.py",
        "subagents/research.md",
        "subagents/material.md",
        "subagents/image.md",
        "subagents/slide.md",
        "subagents/review.md",
    ]
    if speech:
        required_skill.extend([
            "scripts/build_review_contact.py",
            "scripts/font_bundle.py",
            "scripts/sync_speech.py",
        ])
    required_harness = [
        VISUAL_CRAFT_V3_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent.py",
        "core/openai_backend.py",
        "core/tools.py",
        "core/trace.py",
    ]
    missing_skill = [path for path in required_skill if not (skill_root / path).is_file()]
    missing_harness = [path for path in required_harness if not (harness_root / path).is_file()]
    mount_root, mount_reason = _visual_craft_v3_mount(skill_key, skill_root)
    reasons = []
    if missing_skill:
        reasons.append("Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("Harness 缺少：" + ", ".join(missing_harness))
    if mount_reason:
        reasons.append(mount_reason)
    digest = hashlib.sha256()
    if not missing_skill:
        for path in sorted(
            (item for item in skill_root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(skill_root).as_posix(),
        ):
            if any(part in {".venv", "__pycache__", ".pytest_cache"} for part in path.parts):
                continue
            relative = path.relative_to(skill_root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(path.read_bytes())
    ready = not missing_skill and not missing_harness and not mount_reason
    return {
        "label": label,
        "path": str(skill_root),
        "skills_root": str(mount_root),
        "name": "ppt-skill-html",
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": skill_key,
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": required_skill,
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": f"{skill_key}-harness",
        "harness_path": str(harness_root),
        "harness_entry": VISUAL_CRAFT_V3_HARNESS_ENTRY,
        "harness_required_files": required_harness,
        "pairing": f"{skill_key}-paired",
        "caps": ["attachments", "revision", "static_html"] + (["speech"] if speech else []),
        # PresentBench compares V3 against MURAL under the same operational
        # concurrency. V3 reads the legacy variable names while MURAL v0.4
        # reads CLEAN_CHILD_CONCURRENCY/CLEAN_REMOTE_TOOL_CONCURRENCY.
        "runtime_env": {
            "MAX_CONCURRENT_CHILDREN": "12",
            "TURN_TOOL_PARALLEL": "4",
        },
    }


def _sn_ppt_web_skill(
    skill_root: Path = SN_PPT_WEB_SKILL_ROOT,
    harness_root: Path = SN_PPT_WEB_HARNESS_ROOT,
) -> dict:
    """Register the renamed static high-design Skill with its paired Harness."""
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/licenses/OFL-1.1.txt",
        "assets/vendor/echarts.min.js",
        "references/base-template.css",
        "references/design-rules.md",
        "references/design-styles.md",
        "references/editing-contract.md",
        "references/fonts.md",
        "references/layout-patterns.md",
        "references/planning-contract.md",
        "references/quality-checklist.md",
        "scripts/deck.py",
        "scripts/font_bundle.py",
        "scripts/install.sh",
        "scripts/image_cutout.py",
        "scripts/render.py",
        "scripts/stage_materials.py",
        "subagents/research.md",
        "subagents/material.md",
        "subagents/image.md",
        "subagents/slide.md",
        "subagents/review.md",
    )
    harness_required = (
        SN_PPT_WEB_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent.py",
        "core/tools.py",
        "core/trace.py",
    )
    missing_skill = [relative for relative in required_files if not (skill_root / relative).is_file()]
    for frozen_name in ("sn-ppt-web-zh", "sn-ppt-web-en"):
        if not (skill_root.parent / frozen_name / "SKILL.md").is_file():
            missing_skill.append(f"../{frozen_name}/SKILL.md")
    missing_harness = [relative for relative in harness_required if not (harness_root / relative).is_file()]
    reasons = []
    if missing_skill:
        reasons.append("sn-ppt-web Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("sn-ppt-web Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skill_root)
        _hash_runtime_tree(digest, skill_root.parent / "sn-ppt-web-zh", prefix="sn-ppt-web-zh/")
        _hash_runtime_tree(digest, skill_root.parent / "sn-ppt-web-en", prefix="sn-ppt-web-en/")
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": os.environ.get(
            "PPTAGENT_SN_PPT_WEB_DISPLAY_NAME",
            "sn-ppt-web",
        ),
        "path": str(skill_root),
        "skills_root": str(skill_root.parent),
        "name": "sn-ppt-web",
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "sn-ppt-web",
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": list(required_files),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "sn-ppt-web-harness",
        "harness_path": str(harness_root),
        "harness_entry": SN_PPT_WEB_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": "sn-ppt-web-paired",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
    }


def _long_horizon_presenter_skill(
    skill_root: Path = LONG_HORIZON_PRESENTER_SKILL_ROOT,
    harness_root: Path = LONG_HORIZON_PRESENTER_HARNESS_ROOT,
) -> dict:
    """Register the paired Long-Horizon Presenter Skill and Harness."""
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "references/base-template.css",
        "references/design-rules.md",
        "references/design-styles.md",
        "references/editing-contract.md",
        "references/fonts.md",
        "references/layout-patterns.md",
        "references/planning-contract.md",
        "references/quality-checklist.md",
        "scripts/deck.py",
        "scripts/font_bundle.py",
        "scripts/image_cutout.py",
        "scripts/render.py",
        "scripts/stage_materials.py",
        "subagents/image.md",
        "subagents/material.md",
        "subagents/research.md",
        "subagents/review.md",
        "subagents/slide.md",
    )
    harness_required = (
        LONG_HORIZON_PRESENTER_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent.py",
        "core/tools.py",
        "core/trace.py",
    )
    missing_skill = [relative for relative in required_files if not (skill_root / relative).is_file()]
    missing_harness = [relative for relative in harness_required if not (harness_root / relative).is_file()]
    reasons = []
    if missing_skill:
        reasons.append("long-horizon-presenter Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("long-horizon-presenter Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skill_root)
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": os.environ.get(
            "PPTAGENT_LONG_HORIZON_PRESENTER_DISPLAY_NAME",
            "Long-Horizon Presenter",
        ),
        "path": str(skill_root),
        "skills_root": str(skill_root.parent),
        "name": "long-horizon-presenter",
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "long-horizon-presenter",
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": list(required_files),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "long-horizon-presenter-harness",
        "harness_path": str(harness_root),
        "harness_entry": LONG_HORIZON_PRESENTER_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": "long-horizon-presenter-paired",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
    }


def _mural_presenter_skill(
    skill_root: Path = MURAL_PRESENTER_SKILL_ROOT,
    harness_root: Path = MURAL_PRESENTER_HARNESS_ROOT,
) -> dict:
    """Register the frozen paper/release MURAL Presenter pair."""
    required_files = (
        "SKILL.md",
        "SKILL.en.md",
        "SKILL.creative.md",
        "SKILL.creative.en.md",
        "agents/openai.yaml",
        "references/base-template.css",
        "references/design-rules.md",
        "references/design-styles.md",
        "references/editing-contract.md",
        "references/fonts.md",
        "references/layout-patterns.md",
        "references/planning-contract.md",
        "references/quality-checklist.md",
        "references/creative-direction.md",
        "references/creative-direction.en.md",
        "scripts/deck.py",
        "scripts/font_bundle.py",
        "scripts/image_cutout.py",
        "scripts/render.py",
        "scripts/stage_materials.py",
        "subagents/image.md",
        "subagents/image.en.md",
        "subagents/material.md",
        "subagents/material.en.md",
        "subagents/research.md",
        "subagents/research.en.md",
        "subagents/review.md",
        "subagents/review.en.md",
        "subagents/review.creative.md",
        "subagents/review.creative.en.md",
        "subagents/slide.md",
        "subagents/slide.en.md",
        "subagents/slide.creative.md",
        "subagents/slide.creative.en.md",
    )
    harness_required = (
        MURAL_PRESENTER_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent.py",
        "core/tools.py",
        "core/trace.py",
    )
    missing_skill = [relative for relative in required_files if not (skill_root / relative).is_file()]
    missing_harness = [relative for relative in harness_required if not (harness_root / relative).is_file()]
    reasons = []
    if missing_skill:
        reasons.append("mural-presenter Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("mural-presenter Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skill_root)
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": os.environ.get(
            "PPTAGENT_MURAL_PRESENTER_DISPLAY_NAME",
            "MURAL Presenter",
        ),
        "path": str(skill_root),
        "skills_root": str(skill_root.parent),
        "name": "mural-presenter",
        # The MURAL Harness owns first-turn language detection internally.
        # Keep this compatibility field concrete so Studio does not invoke the
        # unrelated clean-Harness ``select_skill`` adapter.
        "language": "zh",
        "supported_languages": ["zh", "en"],
        "agent_language_routing": "query-auto",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "mural-presenter",
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": list(required_files),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "mural-presenter-harness",
        "harness_path": str(harness_root),
        "harness_entry": MURAL_PRESENTER_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": "mural-presenter-paper-v1",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
    }


def _mural_presenter_creative_skill() -> dict:
    """Expose the strong-model authoring profile on the same paired runtime."""
    skill = dict(_mural_presenter_skill())
    skill.update({
        "label": os.environ.get(
            "PPTAGENT_MURAL_CREATIVE_DISPLAY_NAME",
            "MURAL Presenter · Creative",
        ),
        # The runtime snapshots the canonical mural-presenter tree and selects
        # the creative localized entry from this per-job environment value.
        "name": "mural-presenter",
        "mode": "mural-presenter-creative",
        "pairing": "mural-presenter-creative-v1",
        "runtime_env": {"MURAL_AUTHORING_PROFILE": "creative"},
        "authoring_profile": "creative",
    })
    return skill


def _mural_v02_skill(
    skills_root: Path = MURAL_V02_SKILLS_ROOT,
    harness_root: Path = MURAL_V02_HARNESS_ROOT,
    *,
    grouped: bool = False,
) -> dict:
    """Register one bilingual v0.2 ownership topology on the shared Harness."""
    suffix = "-grouped" if grouped else ""
    names = {
        "zh": f"mural-presenter-v0-2{suffix}-zh",
        "en": f"mural-presenter-v0-2{suffix}-en",
    }
    pipeline_key = (
        "mural-presenter-v0.2-grouped-harness"
        if grouped
        else "mural-presenter-v0.2-harness"
    )
    pairing = (
        "mural-presenter-v0.2-grouped"
        if grouped
        else "mural-presenter-v0.2-single-slide"
    )
    required_per_edition = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/base.css",
        "references/charts-and-diagrams.md",
        "references/fonts-and-type.md",
        "references/html-contract.md",
        "references/materials-and-images.md",
        "references/page-patterns.md",
        "references/plan-contract.md",
        "roles/image.md",
        "roles/material.md",
        "roles/orchestrator.md",
        "roles/research.md",
        "roles/review.md",
        "roles/slide.md",
        "scripts/deck.py",
        "scripts/font_bundle.py",
        "scripts/image_background.py",
        "scripts/render_deck.py",
        "scripts/workspace_policy.py",
        "assets/licenses/OFL-1.1.txt",
    )
    missing_skill = [
        f"{name}/{relative}"
        for name in names.values()
        for relative in required_per_edition
        if not (skills_root / name / relative).is_file()
    ]
    harness_required = (
        MURAL_V02_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent_loop.py",
        "core/config.py",
        "core/language.py",
        "core/model_call.py",
        "core/render_broker.py",
        "core/run_profiles.py",
        "core/run_batch.py",
        "core/trace_mode.py",
        "core/tools.py",
    )
    missing_harness = [
        relative for relative in harness_required
        if not (harness_root / relative).is_file()
    ]
    reasons = []
    if missing_skill:
        reasons.append("MURAL Presenter v0.2 Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("MURAL Presenter v0.2 Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skills_root, prefix="skills/")
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": (
            "MURAL Presenter v0.2 · Grouped"
            if grouped
            else "MURAL Presenter v0.2 · Single"
        ),
        "path": str(skills_root),
        "instruction_paths": {
            language: str(skills_root / name)
            for language, name in names.items()
        },
        "skills_root": str(skills_root),
        "name": "auto",
        "language": "auto",
        "supported_languages": ["zh", "en"],
        "agent_language_routing": "first-skill-read-lock",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": (
            "mural-presenter-v0.2-grouped-bilingual"
            if grouped
            else "mural-presenter-v0.2-bilingual"
        ),
        "status": "candidate" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": [
            f"{name}/{relative}"
            for name in names.values()
            for relative in required_per_edition
        ],
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": pipeline_key,
        "harness_path": str(harness_root),
        "harness_entry": MURAL_V02_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": pairing,
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "runtime_env": {
            "CLEAN_MODEL_SELECT_SKILL": "1",
            "MURAL_RUN_MODE": "inference",
            "CLEAN_SKILL_NAME": f"mural-presenter-v0-2{suffix}",
            "CLEAN_SKILL_NAME_ZH": names["zh"],
            "CLEAN_SKILL_NAME_EN": names["en"],
            "CLEAN_CHILD_CONCURRENCY": "12",
            "CLEAN_CHILD_POOL_MAX_WORKERS": "12",
            "CLEAN_REMOTE_TOOL_CONCURRENCY": "4",
            "CLEAN_MAX_TURNS": "240",
            "CLEAN_CHILD_MAX_TURNS": "80",
            "CLEAN_MAX_TOKENS": "40960",
            "CLEAN_MODEL_TIMEOUT": "600",
            "CLEAN_CHILD_WALL_TIMEOUT": "2400",
        },
    }


def _mural_v03_skill(
    skills_root: Path = MURAL_V03_SKILLS_ROOT,
    harness_root: Path = MURAL_V03_HARNESS_ROOT,
) -> dict:
    """Register the single bilingual v0.3 Skill with adaptive page ownership."""
    name = "mural-presenter-v0-3"
    required = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/base.css",
        "references/creative-direction.md",
        "references/design-rules.md",
        "references/design-styles.md",
        "references/layout-patterns.md",
        "references/quality-checklist.md",
        "references/plan-contract.md",
        "references/materials-and-images.md",
        "references/html-contract.md",
        "roles/image.md",
        "roles/material.md",
        "roles/orchestrator.md",
        "roles/research.md",
        "roles/review.md",
        "roles/slide.md",
        "scripts/orchestrator.py",
        "scripts/image.py",
        "scripts/slide.py",
        "scripts/review.py",
        "scripts/_internal/__init__.py",
        "scripts/_internal/bootstrap.py",
        "scripts/_internal/planning.py",
        "scripts/_internal/delivery.py",
        "scripts/_internal/assets.py",
        "scripts/_internal/renderer.py",
        "scripts/_internal/deck_core.py",
        "scripts/_internal/font_bundle.py",
        "scripts/_internal/image_background.py",
        "scripts/_internal/render_deck.py",
        "scripts/_internal/workspace_policy.py",
    )
    missing_skill = [
        f"{name}/{relative}"
        for relative in required
        if not (skills_root / name / relative).is_file()
    ]
    harness_required = (
        MURAL_V03_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent_loop.py",
        "core/config.py",
        "core/language.py",
        "core/model_call.py",
        "core/render_broker.py",
        "core/run_profiles.py",
        "core/runtime_capabilities.py",
        "core/run_batch.py",
        "core/trace_mode.py",
        "core/tools.py",
    )
    missing_harness = [
        relative for relative in harness_required
        if not (harness_root / relative).is_file()
    ]
    reasons = []
    if missing_skill:
        reasons.append("MURAL Presenter v0.3 Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("MURAL Presenter v0.3 Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skills_root, prefix="skills/")
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": "MURAL Presenter v0.3 · Adaptive Ownership",
        "path": str(skills_root / name),
        "instruction_paths": {
            "zh": str(skills_root / name),
            "en": str(skills_root / name),
        },
        "skills_root": str(skills_root),
        "name": name,
        "language": "auto",
        "supported_languages": ["zh", "en"],
        "agent_language_routing": "resolved-deck-brief",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "mural-presenter-v0.3-adaptive",
        "status": "candidate" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": [f"{name}/{relative}" for relative in required],
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "mural-presenter-v0.3-harness",
        "harness_path": str(harness_root),
        "harness_entry": MURAL_V03_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": "mural-presenter-v0.3-adaptive",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "runtime_env": {
            "CLEAN_MODEL_SELECT_SKILL": "0",
            "MURAL_RUN_MODE": "inference",
            "CLEAN_SKILL_NAME": name,
            "CLEAN_SKILL_NAME_ZH": name,
            "CLEAN_SKILL_NAME_EN": name,
            "CLEAN_CHILD_CONCURRENCY": "12",
            "CLEAN_CHILD_POOL_MAX_WORKERS": "12",
            "CLEAN_REMOTE_TOOL_CONCURRENCY": "4",
            "CLEAN_MAX_TURNS": "240",
            "CLEAN_CHILD_MAX_TURNS": "80",
            "CLEAN_MAX_TOKENS": "40960",
            "CLEAN_MODEL_TIMEOUT": "600",
            "CLEAN_CHILD_WALL_TIMEOUT": "2400",
        },
    }


def _mural_v04_skill(
    skills_root: Path = MURAL_V04_SKILLS_ROOT,
    harness_root: Path = MURAL_V04_HARNESS_ROOT,
    name: str = "mural-presenter-v0-4",
    reference_files: tuple[str, ...] | None = None,
    include_review_role: bool = True,
) -> dict:
    """Register the v0.4 quality/stability iteration of adaptive MURAL.

    ``reference_files`` overrides the ``references/*.md`` contract for iterations
    whose reference set differs (e.g. v0.5 merged design-styles/aesthetic-recipes/
    creative-direction into visual-system.md). Defaults to the v0.4 file set.
    """
    if reference_files is None:
        reference_files = (
            "references/aesthetic-recipes.md",
            "references/charts-and-diagrams.md",
            "references/creative-direction.md",
            "references/design-rules.md",
            "references/design-styles.md",
            "references/layout-patterns.md",
            "references/quality-checklist.md",
            "references/plan-contract.md",
            "references/materials-and-images.md",
            "references/html-contract.md",
        )
    role_files = (
        "roles/image.md",
        "roles/material.md",
        "roles/research.md",
        "roles/slide.md",
    )
    script_files = (
        "scripts/orchestrator.py",
        "scripts/image.py",
        "scripts/slide.py",
    )
    if include_review_role:
        role_files = (*role_files, "roles/review.md")
        script_files = (*script_files, "scripts/review.py")
    required = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/base.css",
        "assets/vendor/echarts.min.js",
        *reference_files,
        *role_files,
        *script_files,
        "scripts/_internal/__init__.py",
        "scripts/_internal/cli.py",
        "scripts/_internal/bootstrap.py",
        "scripts/_internal/planning.py",
        "scripts/_internal/delivery.py",
        "scripts/_internal/assets.py",
        "scripts/_internal/renderer.py",
        "scripts/_internal/deck_core.py",
        "scripts/_internal/font_bundle.py",
        "scripts/_internal/image_background.py",
        "scripts/_internal/render_deck.py",
        "scripts/_internal/workspace_policy.py",
    )
    missing_skill = [
        f"{name}/{relative}"
        for relative in required
        if not (skills_root / name / relative).is_file()
    ]
    harness_required = (
        MURAL_V04_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent_loop.py",
        "core/config.py",
        "core/environment_doctor.py",
        "core/language.py",
        "core/model_call.py",
        "core/render_broker.py",
        "core/run_profiles.py",
        "core/runtime_capabilities.py",
        "core/run_batch.py",
        "core/trace_mode.py",
        "core/tools.py",
    )
    missing_harness = [
        relative for relative in harness_required
        if not (harness_root / relative).is_file()
    ]
    reasons = []
    if missing_skill:
        reasons.append("MURAL Presenter v0.4 Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("MURAL Presenter v0.4 Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skills_root, prefix="skills/")
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": "MURAL Presenter v0.4 · Adaptive Quality (archived baseline)",
        "path": str(skills_root / name),
        "instruction_paths": {
            "zh": str(skills_root / name),
            "en": str(skills_root / name),
        },
        "skills_root": str(skills_root),
        "name": name,
        "language": "auto",
        "supported_languages": ["zh", "en"],
        "agent_language_routing": "resolved-deck-brief",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "mural-presenter-v0.4-adaptive",
        "status": "candidate" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": [f"{name}/{relative}" for relative in required],
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "mural-presenter-v0.4-harness",
        "harness_path": str(harness_root),
        "harness_entry": MURAL_V04_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": "mural-presenter-v0.4-adaptive",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "runtime_env": {
            "CLEAN_MODEL_SELECT_SKILL": "0",
            "MURAL_RUN_MODE": "inference",
            "CLEAN_SKILL_NAME": name,
            "CLEAN_SKILL_NAME_ZH": name,
            "CLEAN_SKILL_NAME_EN": name,
            "CLEAN_CHILD_CONCURRENCY": "12",
            "CLEAN_CHILD_POOL_MAX_WORKERS": "12",
            "CLEAN_REMOTE_TOOL_CONCURRENCY": "4",
            "CLEAN_MAX_TURNS": "240",
            "CLEAN_CHILD_MAX_TURNS": "80",
            "CLEAN_MAX_TOKENS": "40960",
            "CLEAN_MODEL_TIMEOUT": "600",
            "CLEAN_CHILD_WALL_TIMEOUT": "2400",
            "CLEAN_DOCTOR_LIVE": "1",
            "CLEAN_DOCTOR_FAIL_ON_CONFIGURED_OPTIONAL": "1",
            "CLEAN_DOCTOR_CACHE_DIR": str(DATA_DIR / "environment-doctor"),
        },
    }


def _mural_v04_thin_skill() -> dict:
    """Thin-harness variant paired with the v0.5 skill package.

    Data synthesis (student distillation, seed generation) defaults here — the
    thin harness gives the model full autonomy over ordering / repair / timing
    decisions and only enforces file-existence acceptance. v0.4 skill is kept
    on disk as an archived baseline; going forward this variant pairs with
    the v0.5 skill package (Master 2026-08-18)."""
    skill = _mural_v04_skill(
        skills_root=MURAL_V05_SKILLS_ROOT,
        harness_root=MURAL_V04_THIN_HARNESS_ROOT,
        name="mural-presenter-v0-5",
    )
    skill["label"] = "MURAL Presenter · Thin harness + v0.5 skill (data synthesis)"
    skill["mode"] = "mural-presenter-v0.4-thin"
    skill["pipeline"] = "mural-presenter-v0.4-thin-harness"
    skill["pairing"] = "mural-presenter-v0.4-thin"
    skill["harness_path"] = str(MURAL_V04_THIN_HARNESS_ROOT)
    return skill


def _mural_v05_skill() -> dict:
    """Register the v0.5 visual-first iteration of adaptive MURAL.

    v0.5 is a new skill package (flex-column base.css, arch-* layout primitives,
    Style Lock, higher slide autonomy, real/generated image-source separation)
    that reuses the v0.4 harness verbatim. Built by delegating to
    ``_mural_v04_skill`` with the v0.5 root+name, then relabelling — so the
    required-file contract and runtime_env stay in lockstep with v0.4.
    """
    skill = _mural_v04_skill(
        skills_root=MURAL_V05_SKILLS_ROOT,
        harness_root=MURAL_V05_HARNESS_ROOT,
        name="mural-presenter-v0-5",
        # v0.5 merged design-styles + aesthetic-recipes + creative-direction into
        # a single visual-system.md; the reference contract reflects that.
        reference_files=(
            "references/visual-system.md",
            "references/charts-and-diagrams.md",
            "references/design-rules.md",
            "references/layout-patterns.md",
            "references/motion.md",
            "references/quality-checklist.md",
            "references/plan-contract.md",
            "references/materials-and-images.md",
            "references/html-contract.md",
        ),
    )
    skill["label"] = "MURAL Presenter v0.5 · Visual-First"
    skill["mode"] = "mural-presenter-v0.5-adaptive"
    skill["pipeline"] = "mural-presenter-v0.5-harness"
    skill["pairing"] = "mural-presenter-v0.5-adaptive"
    # v0.5 gives each slide a slightly larger self-repair budget so a messy
    # first draft gets 1-2 more iterations before the hard stop (base 3 pixel
    # states × 1.5 ≈ 4-5). Keeps refine load on the slide agent instead of
    # spilling to Review. Only v0.5; v0.4 baseline stays at 1.0.
    runtime_env = dict(skill.get("runtime_env", {}))
    runtime_env["CLEAN_SLIDE_BUDGET_MULT"] = "1.5"
    skill["runtime_env"] = runtime_env
    return skill




def _mural_v10_skill() -> dict:
    """Register the paper v1.0 lifecycle contract and thin runtime Harness.

    The active roles are Orchestrator, optional evidence/image Owners, and
    Single/Group Slide Owners. Whole-deck semantic and pixel closure stays in
    the persistent Orchestrator; ``review.md`` remains snapshot compatibility
    only and is absent from the v1.0 delegation schema.
    """
    skill = _mural_v04_skill(
        skills_root=MURAL_V10_SKILLS_ROOT,
        harness_root=MURAL_V10_HARNESS_ROOT,
        name="mural-presenter-v1",
        include_review_role=False,
        # These are readiness files, not an instruction to read them all.
        reference_files=(
            "references/design-routing.md",
            "references/authoring-profiles.md",
            "references/style-index.md",
            "references/style-lenses-editorial.md",
            "references/style-lenses-institutional.md",
            "references/style-lenses-technical.md",
            "references/style-lenses-cultural.md",
            "references/style-lenses-experimental.md",
            "references/typography-routing.md",
            "references/typography-routes.json",
            "references/visual-system.md",
            "references/layout-patterns.md",
            "references/special-pages.md",
            "references/quality-rubric.md",
            "references/html-contract.md",
            "references/materials-and-images.md",
            "references/motion.md",
        ),
    )
    skill["label"] = "MURAL Presenter v1.0 · Rollback"
    skill["mode"] = "mural-presenter-v1.0-adaptive"
    skill["pipeline"] = "mural-presenter-v1.0-harness"
    skill["pairing"] = "mural-presenter-v1.0-adaptive"
    runtime_defaults = _mural_v1_runtime_defaults(MURAL_V10_RUNTIME_ENV_PATH)
    if "runtime.env" not in skill["harness_required_files"]:
        skill["harness_required_files"].append("runtime.env")
    if not runtime_defaults:
        skill["ready"] = False
        reason = f"MURAL Presenter v1.0 Harness 缺少：{MURAL_V10_RUNTIME_ENV_PATH}"
        skill["unavailable_reason"] = "；".join(
            value for value in (skill.get("unavailable_reason", ""), reason) if value
        )
    skill["status"] = "rollback" if skill.get("ready") else "unavailable"
    runtime_env = dict(skill.get("runtime_env", {}))
    runtime_env.update(runtime_defaults)
    runtime_env["MURAL_RELEASE_MANIFEST"] = str(MURAL_V10_RELEASE_MANIFEST_PATH)
    # All shared v1 authoring behavior comes from runtime.env. The WebUI only
    # adds deployment/model capacity and per-user service overrides later.
    skill["runtime_env"] = runtime_env
    return skill


def _mural_v1_skill(*, pinned_skill: dict | None = None) -> dict:
    """从唯一具体 Skill 根目录注册当前共享运行时。"""
    skill_root = Path(
        pinned_skill["object_root"] if pinned_skill is not None else (
            os.environ.get("MURAL_SKILL_DIR")
            or _MURAL_REPOSITORY_ROOT / "skills" / "mural-presenter-v1"
        )
    ).expanduser().resolve()
    required = (
        "SKILL.md",
        "roles/image.md",
        "roles/research.md",
        "roles/slide.md",
        "assets/base.css",
        "assets/vendor/echarts.min.js",
        "scripts/orchestrator.py",
        "scripts/image.py",
        "scripts/slide.py",
        "scripts/_internal/__init__.py",
        "scripts/_internal/cli.py",
        "scripts/_internal/bootstrap.py",
        "scripts/_internal/planning.py",
        "scripts/_internal/delivery.py",
        "scripts/_internal/assets.py",
        "scripts/_internal/renderer.py",
        "scripts/_internal/deck_core.py",
        "scripts/_internal/font_bundle.py",
        "scripts/_internal/image_background.py",
        "scripts/_internal/render_deck.py",
        "scripts/_internal/workspace_policy.py",
    )
    harness_required = (
        MURAL_V1_HARNESS_ENTRY,
        "runtime.env",
        "runtime/config.py",
        "runtime/session.py",
        "runtime/agent.py",
        "runtime/live_trace.py",
        "runtime/attachments.py",
        "runtime/production.py",
        "runtime/skill_runtime.py",
        "orchestration/manager.py",
        "owner/context.py",
        "state/workspace.py",
        "export/delivery.py",
    )
    missing_harness = [
        relative
        for relative in harness_required
        if not (MURAL_V1_HARNESS_ROOT / relative).is_file()
    ]
    missing_skill = [
        relative
        for relative in required
        if not (skill_root / relative).is_file()
    ]
    runtime_defaults = _mural_v1_runtime_defaults()
    reasons = []
    if missing_skill:
        reasons.append("MURAL Presenter v1 Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("MURAL Presenter v1 Harness 缺少：" + ", ".join(missing_harness))
    if not runtime_defaults:
        reasons.append(f"MURAL Presenter v1 Harness 缺少：{MURAL_V1_RUNTIME_ENV_PATH}")
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skill_root, prefix="skills/")
        # 当前 Skill 可共享这两个目录；摘要仍覆盖实际脚本和资源内容。
        for directory in ("scripts", "assets"):
            if (skill_root / directory).is_symlink():
                _hash_runtime_tree(
                    digest, skill_root / directory, prefix=f"skills/{directory}/"
                )
    if not missing_harness:
        _hash_runtime_tree(digest, MURAL_V1_HARNESS_ROOT, prefix="harness/")
    ready = not reasons
    runtime_env = dict(runtime_defaults)
    runtime_env["MURAL_SKILL_DIR"] = str(skill_root)
    runtime_env["MURAL_RUNTIME_CONFIG"] = os.environ.get(
        "MURAL_RUNTIME_CONFIG", str(_MURAL_REPOSITORY_ROOT / "configs/runtime.local.json")
    )
    runtime_env["MURAL_RELEASE_MANIFEST"] = str(MURAL_V1_RELEASE_MANIFEST_PATH)
    return {
        "label": "MURAL Presenter v1 · Shared Runtime",
        "path": str(skill_root),
        "instruction_paths": {"zh": str(skill_root), "en": str(skill_root)},
        "name": pinned_skill["skill_name"] if pinned_skill is not None else skill_root.name,
        "language": "auto",
        "supported_languages": ["zh", "en"],
        "agent_language_routing": "resolved-deck-brief",
        "deck_language": "auto",
        "force_skill_language": "",
        "required_files": list(required),
        "mode": "mural-presenter-v1",
        "pipeline": "mural-presenter-v1-harness",
        "pairing": "mural-presenter-v1",
        "harness_path": str(MURAL_V1_HARNESS_ROOT),
        "harness_entry": MURAL_V1_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "ready": ready,
        "status": "current" if ready else "unavailable",
        "unavailable_reason": "；".join(reasons),
        "caps": [
            "attachments",
            "revision",
            "resume",
            "static_html",
            "custom_fonts",
        ],
        "runtime_env": runtime_env,
    }




def _mural_v04_solo_skill() -> dict:
    """Single-agent (solo) ablation of v0.4.

    Reuses the v0.4 harness and (symlinked) references verbatim; only SKILL.md
    and roles/orchestrator.md differ so one Orchestrator does everything with no
    delegation. Solo behaviour is switched on by CLEAN_SOLO_MODE=1 in runtime_env.
    """
    skill = _mural_v04_skill(
        skills_root=MURAL_V04_SOLO_SKILLS_ROOT,
        name="mural-presenter-v0-4-solo",
    )
    skill["label"] = "MURAL v0.4 · Solo（single-agent 消融）"
    skill["mode"] = "mural-presenter-v0.4-solo"
    skill["pipeline"] = "mural-presenter-v0.4-solo-harness"
    skill["pairing"] = "mural-presenter-v0.4-solo"
    # Ablation: one agent does plan+write+render+review+finalize, no delegation.
    runtime_env = dict(skill.get("runtime_env", {}))
    runtime_env["CLEAN_SOLO_MODE"] = "1"
    runtime_env["CLEAN_SKILL_NAME"] = "mural-presenter-v0-4-solo"
    runtime_env["CLEAN_SKILL_NAME_ZH"] = "mural-presenter-v0-4-solo"
    runtime_env["CLEAN_SKILL_NAME_EN"] = "mural-presenter-v0-4-solo"
    skill["runtime_env"] = runtime_env
    return skill


def _mural_v04_nk_skill() -> dict:
    """No-Skill (nk) ablation: multi-agent path, but SKILL.md forbids reading
    the design references. Files stay symlinked so harness readiness passes;
    the model is instructed not to consume the design knowledge."""
    skill = _mural_v04_skill(
        skills_root=MURAL_V04_NK_SKILLS_ROOT,
        name="mural-presenter-v0-4-nk",
    )
    skill["label"] = "MURAL v0.4 · No-Skill（multi-agent 消融）"
    skill["mode"] = "mural-presenter-v0.4-nk"
    skill["pipeline"] = "mural-presenter-v0.4-nk-harness"
    skill["pairing"] = "mural-presenter-v0.4-nk"
    runtime_env = dict(skill.get("runtime_env", {}))
    runtime_env["CLEAN_SKILL_NAME"] = "mural-presenter-v0-4-nk"
    runtime_env["CLEAN_SKILL_NAME_ZH"] = "mural-presenter-v0-4-nk"
    runtime_env["CLEAN_SKILL_NAME_EN"] = "mural-presenter-v0-4-nk"
    skill["runtime_env"] = runtime_env
    return skill


def _mural_v04_nk_solo_skill() -> dict:
    """No-Skill × Solo cell of the 2×2 ablation matrix."""
    skill = _mural_v04_skill(
        skills_root=MURAL_V04_NK_SKILLS_ROOT,
        name="mural-presenter-v0-4-nk",
    )
    skill["label"] = "MURAL v0.4 · No-Skill · Solo（single-agent 消融）"
    skill["mode"] = "mural-presenter-v0.4-nk-solo"
    skill["pipeline"] = "mural-presenter-v0.4-nk-solo-harness"
    skill["pairing"] = "mural-presenter-v0.4-nk-solo"
    runtime_env = dict(skill.get("runtime_env", {}))
    runtime_env["CLEAN_SOLO_MODE"] = "1"
    runtime_env["CLEAN_SKILL_NAME"] = "mural-presenter-v0-4-nk"
    runtime_env["CLEAN_SKILL_NAME_ZH"] = "mural-presenter-v0-4-nk"
    runtime_env["CLEAN_SKILL_NAME_EN"] = "mural-presenter-v0-4-nk"
    skill["runtime_env"] = runtime_env
    return skill


def _mural_next_skill(
    skill_root: Path = MURAL_NEXT_SKILL_ROOT,
    harness_root: Path = MURAL_NEXT_HARNESS_ROOT,
) -> dict:
    """Register the private successor line when experimental Skills are enabled."""
    required_files = (
        "SKILL.md",
        "agents/openai.yaml",
        "assets/base-template.css",
        "references/charts-and-diagrams.md",
        "references/design-rules.md",
        "references/editing-contract.md",
        "references/fonts.md",
        "references/layout-patterns.md",
        "references/planning-contract.md",
        "references/quality-checklist.md",
        "references/scenario-routing.md",
        "references/shape-grammar.md",
        "references/style-routing.md",
        "scripts/bundle_fonts.py",
        "scripts/cutout_image.py",
        "scripts/deck.py",
        "scripts/render.py",
        "scripts/stage_materials.py",
        "roles/research.md",
        "roles/material.md",
        "roles/image.md",
        "roles/slide.md",
        "roles/review.md",
    )
    harness_required = (
        MURAL_NEXT_HARNESS_ENTRY,
        "core/__init__.py",
        "core/agent.py",
        "core/tools.py",
        "core/trace.py",
    )
    missing_skill = [relative for relative in required_files if not (skill_root / relative).is_file()]
    missing_harness = [relative for relative in harness_required if not (harness_root / relative).is_file()]
    reasons = []
    if missing_skill:
        reasons.append("MURAL Next Skill 缺少：" + ", ".join(missing_skill))
    if missing_harness:
        reasons.append("MURAL Next Harness 缺少：" + ", ".join(missing_harness))
    digest = hashlib.sha256()
    if not missing_skill:
        _hash_runtime_tree(digest, skill_root)
    if not missing_harness:
        _hash_runtime_tree(digest, harness_root, prefix="harness/")
    ready = not missing_skill and not missing_harness
    return {
        "label": os.environ.get(
            "PPTAGENT_MURAL_NEXT_DISPLAY_NAME",
            "MURAL Next · Experimental",
        ),
        "path": str(skill_root),
        "skills_root": str(skill_root.parent),
        "name": "mural-presenter",
        "language": "zh",
        "deck_language": "auto",
        "force_skill_language": "",
        "mode": "mural-next",
        "status": "experimental" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "；".join(reasons),
        "required_files": list(required_files),
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "mural-next-harness",
        "harness_path": str(harness_root),
        "harness_entry": MURAL_NEXT_HARNESS_ENTRY,
        "harness_required_files": list(harness_required),
        "pairing": "mural-next-experimental",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
    }


def _sense_present_skill(name: str, label: str, pipeline: str, entry: str) -> dict:
    if name == "sn-ppt-standard":
        specific = (
            "sn-ppt-standard/SKILL.md",
            "sn-ppt-standard/scripts/render.py",
            "sn-ppt-standard/scripts/build_player.py",
        )
        output = "static_html"
    elif name == "sn-ppt-dazzle":
        specific = (
            "sn-ppt-dazzle/SKILL.md",
            "sn-ppt-dazzle/scripts/init_deck.py",
            "sn-ppt-dazzle/scripts/render_deck.py",
            "sn-ppt-dazzle/scripts/harvest_deck.py",
        )
        output = "dynamic_html"
    else:
        raise ValueError(f"unknown direct Sense Present Skill: {name}")
    required = (
        "sn-ppt-story/SKILL.md",
        "sn-ppt-tools/references/capability-policy.md",
        *specific,
    )
    missing = [name for name in required if not (SENSE_PRESENT_V2_SKILLS_ROOT / name).is_file()]
    upstream = {}
    try:
        upstream = json.loads((SENSE_PRESENT_V2_ROOT / "UPSTREAM.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        pass
    ready = not missing and (INFERENCE_DIR / "sense_present_v2.py").is_file() \
        and (INFERENCE_DIR / entry).is_file()
    return {
        "label": label,
        "path": str(SENSE_PRESENT_V2_SKILLS_ROOT / name),
        "skills_root": str(SENSE_PRESENT_V2_SKILLS_ROOT),
        "name": name,
        "language": "zh",
        "deck_language": "auto",
        "mode": name,
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "缺少：" + ", ".join(missing) if missing else "",
        "source_revision": str(upstream.get("commit") or "8481ba94")[:12],
        "pipeline": pipeline,
        "pairing": f"{name}-alpha.1",
        "output": output,
        "caps": ["attachments", "revision", output],
        "runtime_env": {"SENSENOVA_PPT_V2_ROOT": str(SENSE_PRESENT_V2_ROOT)},
    }


def _sense_present_suite_skill() -> dict:
    """Register the full in-repository suite through its sn-ppt-entry root."""
    required = (
        "sn-ppt-entry/SKILL.md",
        "sn-ppt-story/SKILL.md",
        "sn-ppt-tools/SKILL.md",
        "sn-ppt-tools/references/capability-policy.md",
        "sn-ppt-standard/SKILL.md",
        "sn-ppt-standard/scripts/render.py",
        "sn-ppt-standard/scripts/build_player.py",
        "sn-ppt-standard/subagents/image.md",
        "sn-ppt-standard/subagents/slide.md",
        "sn-ppt-standard/subagents/review.md",
    )
    missing = [
        relative
        for relative in required
        if not (SENSE_PRESENT_SUITE_SKILLS_ROOT / relative).is_file()
    ]
    harness = INFERENCE_DIR / SENSE_PRESENT_SUITE_HARNESS_ENTRY
    if not harness.is_file():
        missing.append(SENSE_PRESENT_SUITE_HARNESS_ENTRY)
    digest = hashlib.sha256()
    if not missing:
        for path in sorted(
            (item for item in SENSE_PRESENT_SUITE_SKILLS_ROOT.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(SENSE_PRESENT_SUITE_SKILLS_ROOT).as_posix(),
        ):
            if any(part in {".git", ".venv", "__pycache__"} for part in path.parts):
                continue
            digest.update(path.relative_to(SENSE_PRESENT_SUITE_SKILLS_ROOT).as_posix().encode())
            digest.update(path.read_bytes())
    ready = not missing
    return {
        "label": "Sense Present PPT Skill Suite",
        "path": str(SENSE_PRESENT_SUITE_SKILLS_ROOT / "sn-ppt-entry"),
        "skills_root": str(SENSE_PRESENT_SUITE_SKILLS_ROOT),
        "name": "sn-ppt-entry",
        "language": "auto",
        "deck_language": "auto",
        "mode": "sense-present-suite",
        "status": "current" if ready else "unavailable",
        "ready": ready,
        "unavailable_reason": "缺少：" + ", ".join(missing) if missing else "",
        "source_revision": digest.hexdigest()[:12] if ready else "",
        "pipeline": "sense-present-ppt-skill-harness",
        "pairing": "sense-present-ppt-skill-suite",
        "output": "static_html",
        "caps": ["attachments", "static_html"],
        "runtime_env": {
            "SENSENOVA_PPT_SUITE_ROOT": str(SENSE_PRESENT_SUITE_ROOT),
            "MAX_CONCURRENT_CHILDREN": "12",
            "MAX_TURNS": "240",
            "SUBAGENT_MAX_TURNS": "80",
            "MAX_TOKENS": "40960",
        },
    }


# 顺序同时决定 UI 顺序。普通 Long-horizon 从 Pipeline 热加载；Grouped 版本在
# Web Demo vendor 中维护独立副本。两者都在任务启动时快照进对应 deck 工作区。
SKILLS = {"mural-presenter-v1": _mural_v1_skill()}
PUBLIC_SKILL_KEYS = ("mural-presenter-v1",)
DEFAULT_SKILL = "mural-presenter-v1"

_SKILL_CATALOG_LOCK = threading.Lock()
try:
    _SKILL_CATALOG_REFRESH_TTL_S = max(
        0.0, float(os.environ.get("PPTAGENT_SKILL_REFRESH_TTL_S", "10"))
    )
except ValueError:
    _SKILL_CATALOG_REFRESH_TTL_S = 10.0
# SKILLS was built from the current source trees immediately above. Treat that
# import-time snapshot as fresh so the first homepage request does not hash all
# Skill files on AFS again.
_SKILL_CATALOG_REFRESHED_AT = time.monotonic()


def refresh_external_skills(*, force: bool = False) -> dict:
    """仅刷新正式 v1 配对；历史注册表不能重新成为运行入口。"""
    global _SKILL_CATALOG_REFRESHED_AT
    with _SKILL_CATALOG_LOCK:
        now = time.monotonic()
        if not force and now - _SKILL_CATALOG_REFRESHED_AT < _SKILL_CATALOG_REFRESH_TTL_S:
            return SKILLS["mural-presenter-v1"]
        refreshed = _mural_v1_skill()
        SKILLS.clear()
        SKILLS["mural-presenter-v1"] = refreshed
        if "PIPELINES" in globals():
            PIPELINES.clear()
            PIPELINES["mural-presenter-v1-harness"] = _mural_v1_pipeline(refreshed)
        _SKILL_CATALOG_REFRESHED_AT = time.monotonic()
        return refreshed


# zh/en 共用同一套 clean Harness；语言选择只决定 Skill，不复制 Pipeline。
def _visual_craft_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("visual-craft") or _visual_craft_skill()
    return {
        "label": "Visual Craft Harness",
        "path": str(VISUAL_CRAFT_HARNESS_ROOT),
        "entry": VISUAL_CRAFT_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "ppt-skill-html",
        "caps": ["attachments", "revision"],
        "pairing": "visual-craft-paired",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _visual_craft_v3_pipeline(skill: dict) -> dict:
    """Return the exact Harness paired with one Visual Craft V3 snapshot."""
    return {
        "label": f"{skill['label']} Harness",
        "path": str(skill["harness_path"]),
        "entry": str(skill["harness_entry"]),
        "supports": ["anthropic", "openai"],
        "skill_mode": "ppt-skill-html",
        "caps": list(skill.get("caps", [])),
        "pairing": str(skill.get("pairing", "")),
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _sn_ppt_web_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("sn-ppt-web") or _sn_ppt_web_skill()
    return {
        "label": f"{skill['label']} harness",
        "path": str(SN_PPT_WEB_HARNESS_ROOT),
        "entry": SN_PPT_WEB_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "sn-ppt-web",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "sn-ppt-web-paired",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _long_horizon_presenter_pipeline(skill: dict | None = None) -> dict:
    skill = (
        skill
        or SKILLS.get("long-horizon-presenter")
        or _long_horizon_presenter_skill()
    )
    return {
        "label": "Long-Horizon Presenter harness",
        "path": str(LONG_HORIZON_PRESENTER_HARNESS_ROOT),
        "entry": LONG_HORIZON_PRESENTER_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "long-horizon-presenter",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "long-horizon-presenter-paired",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_presenter_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter") or _mural_presenter_skill()
    return {
        "label": "MURAL Presenter harness",
        "path": str(MURAL_PRESENTER_HARNESS_ROOT),
        "entry": MURAL_PRESENTER_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-paper-v1",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v02_pipeline(
    skill: dict | None = None,
    *,
    grouped: bool = False,
) -> dict:
    skill_key = "mural-presenter-v0.2-grouped" if grouped else "mural-presenter-v0.2"
    skill = skill or SKILLS.get(skill_key) or _mural_v02_skill(grouped=grouped)
    return {
        "label": (
            "MURAL Presenter v0.2 · Grouped"
            if grouped
            else "MURAL Presenter v0.2 · Single"
        ),
        "path": str(MURAL_V02_HARNESS_ROOT),
        "entry": MURAL_V02_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": (
            "clean-bilingual-grouped"
            if grouped
            else "clean-bilingual-single-slide"
        ),
        "caps": ["attachments", "revision", "static_html"],
        "pairing": (
            "mural-presenter-v0.2-grouped"
            if grouped
            else "mural-presenter-v0.2-single-slide"
        ),
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v03_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v0.3") or _mural_v03_skill()
    return {
        "label": "MURAL Presenter v0.3 · Adaptive Ownership",
        "path": str(MURAL_V03_HARNESS_ROOT),
        "entry": MURAL_V03_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v0.3-adaptive",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-v0.3-adaptive",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v04_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v0.4") or _mural_v04_skill()
    return {
        "label": "MURAL Presenter v0.4 · Adaptive Quality (archived baseline)",
        "path": str(MURAL_V04_HARNESS_ROOT),
        "entry": MURAL_V04_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v0.4-adaptive",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-v0.4-adaptive",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v05_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v0.5") or _mural_v05_skill()
    return {
        "label": "MURAL Presenter v0.5 · Visual-First",
        "path": str(MURAL_V05_HARNESS_ROOT),
        "entry": MURAL_V05_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v0.5-adaptive",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-v0.5-adaptive",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v10_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v1.0") or _mural_v10_skill()
    return {
        "label": "MURAL Presenter v1.0 · Thin / Opus-First",
        "path": str(MURAL_V10_HARNESS_ROOT),
        "entry": MURAL_V10_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v1.0-adaptive",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-v1.0-adaptive",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v1_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v1") or _mural_v1_skill()
    return {
        "label": "MURAL Presenter v1 · Shared Runtime",
        "path": str(MURAL_V1_HARNESS_ROOT),
        "entry": MURAL_V1_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v1",
        "caps": list(skill.get("caps") or []),
        "pairing": "mural-presenter-v1",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }




def _mural_v04_solo_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v0.4-solo") or _mural_v04_solo_skill()
    return {
        "label": "MURAL v0.4 · Solo（single-agent 消融）",
        "path": str(MURAL_V04_HARNESS_ROOT),
        "entry": MURAL_V04_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v0.4-solo",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-v0.4-solo",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v04_nk_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v0.4-nk") or _mural_v04_nk_skill()
    return {
        "label": "MURAL v0.4 · No-Skill（multi-agent 消融）",
        "path": str(MURAL_V04_HARNESS_ROOT),
        "entry": MURAL_V04_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v0.4-nk",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-v0.4-nk",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v04_nk_solo_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v0.4-nk-solo") or _mural_v04_nk_solo_skill()
    return {
        "label": "MURAL v0.4 · No-Skill · Solo（single-agent 消融）",
        "path": str(MURAL_V04_HARNESS_ROOT),
        "entry": MURAL_V04_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v0.4-nk-solo",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-v0.4-nk-solo",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_v04_thin_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-presenter-v0.4-thin") or _mural_v04_thin_skill()
    return {
        "label": "MURAL Presenter v0.4 · Thin (data synthesis)",
        "path": str(MURAL_V04_THIN_HARNESS_ROOT),
        "entry": MURAL_V04_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-presenter-v0.4-thin",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-presenter-v0.4-thin",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


def _mural_next_pipeline(skill: dict | None = None) -> dict:
    skill = skill or SKILLS.get("mural-next") or _mural_next_skill()
    return {
        "label": "MURAL Next experimental harness",
        "path": str(MURAL_NEXT_HARNESS_ROOT),
        "entry": MURAL_NEXT_HARNESS_ENTRY,
        "supports": ["anthropic", "openai"],
        "skill_mode": "mural-next",
        "caps": ["attachments", "revision", "static_html", "custom_fonts"],
        "pairing": "mural-next-experimental",
        "ready": bool(skill.get("ready")),
        "unavailable_reason": skill.get("unavailable_reason", ""),
    }


PIPELINES = {"mural-presenter-v1-harness": _mural_v1_pipeline()}
DEFAULT_PIPELINE = "mural-presenter-v1-harness"
PUBLIC_PIPELINE_KEYS = ("mural-presenter-v1-harness",)

# 可选模型注册表:anthropic 走引擎默认 teacher 路径;openai 走引擎的 student shim
# (agent_loop 按 MODEL_BACKEND=openai + STUDENT_BASE_URL/STUDENT_MODEL 切换)。
# Public V1 intentionally ships without built-in model endpoints.
# Configure SENSENOVA_MODEL_* / SENSENOVA_MODEL2_* or add models from
# User -> Model configuration.
MODELS = {}
DEFAULT_MODEL = "deployment-model"
ENVIRONMENT_MODEL_KEY = "deployment-model"
ENVIRONMENT_MODEL2_KEY = "deployment-model-2"
ENVIRONMENT_MODEL3_KEY = "deployment-model-3"
ENVIRONMENT_MODEL4_KEY = "deployment-model-4"
ENVIRONMENT_MODEL5_KEY = "deployment-model-5"


def _normalize_thinking_transport(value: str | None) -> str:
    """Normalize legacy deployment transport names to the chat API contract."""
    transport = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "openai": "chat_template_kwargs",
        "qwen": "chat_template_kwargs",
        "vllm": "chat_template_kwargs",
        "openai_chat": "reasoning_effort",
    }
    return aliases.get(transport, transport)


def _effort_levels(value=None) -> tuple[str, ...]:
    """Return the supported graded effort values in stable UI order."""
    raw = value if value is not None else ()
    if isinstance(raw, str):
        raw = raw.split(",")
    requested = {str(item).strip().lower() for item in raw if str(item or "").strip()}
    unknown = requested - set(service_config.EFFORT_LEVELS)
    if unknown:
        raise ValueError(f"Unsupported effort levels: {sorted(unknown)}")
    return tuple(level for level in service_config.EFFORT_LEVELS if level in requested)


def _default_openai_thinking_transport(model_name: str) -> str:
    """Infer only well-known Chat Completions reasoning contracts."""
    model_l = str(model_name or "").strip().lower()
    if "deepseek-v4" in model_l:
        return "deepseek"
    if "qwen" in model_l or "qwq" in model_l:
        return "chat_template_kwargs"
    if model_l == "kimi-k3":
        return "reasoning_effort"
    if model_l.startswith(("gpt-5", "gpt-6", "o1", "o3", "o4")):
        return "reasoning_effort"
    return ""


def _environment_model(prefix: str = "SENSENOVA_MODEL") -> dict | None:
    """Build the optional deployment-managed model as its own registry item.

    ``SENSENOVA_MODEL_*`` used to override the first built-in model in
    ``selection_env``.  That made the label, model id and endpoint come from
    different sources.  Treating the environment configuration as a distinct
    model keeps the catalog truthful and lets deployments start with no model
    at all.
    """
    base_url = os.environ.get(f"{prefix}_BASE_URL", "").strip().rstrip("/")
    model_name = os.environ.get(f"{prefix}_NAME", "").strip()
    if not base_url or not model_name:
        return None
    configured_api_format = os.environ.get(f"{prefix}_API_FORMAT", "").strip()
    api_format = (
        service_config.resolve_api_format(configured_api_format)
        if configured_api_format else ""
    )
    configured_transport = os.environ.get(f"{prefix}_THINKING_TRANSPORT", "").strip()
    thinking_transport = _normalize_thinking_transport(configured_transport) if (
        configured_transport
    ) else _default_openai_thinking_transport(model_name)
    capability = service_config.model_effort_capability(model_name)
    if model_name.strip().lower() == "kimi-k3":
        thinking_transport = "reasoning_effort"
    if capability["transport"] == "chat_template_kwargs" and thinking_transport != "chat_template_kwargs":
        raise ValueError(f"{model_name} requires chat_template_kwargs plus reasoning_effort")
    effort_levels = _effort_levels(
        os.environ.get(
            f"{prefix}_EFFORT_LEVELS",
            ",".join(capability["levels"]) or ("low,medium,high" if thinking_transport == "reasoning_effort" else ""),
        )
    )
    model = {
        "label": (
            os.environ.get(f"{prefix}_DISPLAY_NAME", "").strip()
            or model_name
        ),
        "backend": "openai",
        "engine_model": model_name,
        "base_url": base_url,
        "api_key": os.environ.get(f"{prefix}_API_KEY", "").strip(),
        "api_key_env": os.environ.get(f"{prefix}_API_KEY_ENV", "").strip(),
        "slide_concurrency": int(
            os.environ.get(f"{prefix}_SLIDE_CONCURRENCY", "4") or "4"
        ),
        "thinking_mode": (
            "always" if thinking_transport == "reasoning_effort"
            else "toggle" if thinking_transport else "none"
        ),
        "thinking_transport": thinking_transport,
        "thinking_effort": (
            os.environ.get(f"{prefix}_THINKING_EFFORT", "").strip().lower()
            or ((capability["default"] or "medium") if effort_levels else "")
        ),
        "thinking_effort_levels": effort_levels,
        "deployment_managed": True,
    }
    if api_format:
        model["api_format"] = api_format
    route_pool = os.environ.get(f"{prefix}_ROUTE_POOL_PATH", "").strip()
    if route_pool:
        if api_format != "openai_responses" or not model_name.startswith("gpt-6-astra/"):
            raise ValueError("route pools are only supported for GPT-6 Responses models")
        model["route_pool_path"] = str(Path(route_pool).expanduser().resolve())
    # Optional per-model multimodal flag. A multimodal openai student gets
    # VISION_BACKEND=same_model_aux (which the v0.6 harness reinterprets as
    # native in-context vision); the openai shim already serialises image
    # blocks to image_url. Absent -> harness/skill default applies.
    multimodal_env = os.environ.get(f"{prefix}_MULTIMODAL", "").strip().lower()
    if multimodal_env in {"1", "true", "yes", "on"}:
        model["multimodal"] = True
    elif multimodal_env in {"0", "false", "no", "off"}:
        model["multimodal"] = False
    return model


def _anthropic_env_model(
    prefix: str = "SENSENOVA_ANTHROPIC",
    default_name: str = "claude-opus-4-7-thinking",
    default_label: str = "Opus 4.7",
    default_enable: bool = False,
    api_key_env: str = "",
    base_url: str = "",
) -> dict | None:
    """Build the optional anthropic teacher model (e.g. Opus 4.7) as a registry item.

    Unlike ``_environment_model`` (which always emits an OpenAI student shim),
    this registers a native ``anthropic`` backend model that runs the engine's
    default teacher path.  By default base_url / API keys are NOT stored on the
    model — they come from the shared anthropic pool (``_anthropic_pool`` +
    ``resolve_anthropic_key``, with C4 fallback), same as the built-in default.

    ``api_key_env`` / ``base_url`` may still pin one deployment to a specific
    gateway when an operator explicitly configures it. When set, the dispatcher
    resolves the key from that env var and overrides ANTHROPIC_BASE_URL, bypassing
    the shared pool / C4 failover for this model only.

    Enabled when ``<prefix>_ENABLE`` is truthy, a model name is given, or
    ``default_enable`` is True; deployments can still disable a default-on model
    by setting ``<prefix>_ENABLE=0``.
    """
    enable_raw = os.environ.get(f"{prefix}_ENABLE", "").strip().lower()
    if enable_raw:
        enable = enable_raw in {"1", "true", "yes", "on"}
    else:
        enable = default_enable
    model_name = os.environ.get(f"{prefix}_NAME", "").strip()
    if not enable and not model_name:
        return None
    model_name = model_name or default_name
    return {
        "label": (
            os.environ.get(f"{prefix}_DISPLAY_NAME", "").strip() or default_label
        ),
        "backend": "anthropic",
        "engine_model": model_name,
        # vision uses the main model itself (same_model_aux); no external critic.
        "multimodal": True,
        # user-controllable Think switch; transport must be non-empty for
        # thinking_capability() to report toggle=True.
        "thinking_mode": "toggle",
        "thinking_transport": "anthropic",
        "thinking_effort": os.environ.get(
            f"{prefix}_THINKING_EFFORT", "high"
        ).strip() or "high",
        "thinking_effort_levels": service_config.model_effort_capability(model_name)["levels"] or ("low", "medium", "high"),
        "deployment_managed": True,
        # 独立模型凭证入口；可为单 Key 或逗号分隔号池，不混入其他模型的共享账号。
        "api_key_env": os.environ.get(f"{prefix}_API_KEY_ENV", "").strip() or api_key_env,
        "base_url": os.environ.get(f"{prefix}_BASE_URL", "").strip() or base_url,
    }


ANTHROPIC_MODEL_KEY = "opus-4-7"
ANTHROPIC_MODEL2_KEY = "opus-5"
ANTHROPIC_MODEL3_KEY = "opus-4-8"

_ENVIRONMENT_MODEL = _environment_model()
if _ENVIRONMENT_MODEL:
    MODELS[ENVIRONMENT_MODEL_KEY] = _ENVIRONMENT_MODEL
_ENVIRONMENT_MODEL2 = _environment_model("SENSENOVA_MODEL2")
if _ENVIRONMENT_MODEL2:
    MODELS[ENVIRONMENT_MODEL2_KEY] = _ENVIRONMENT_MODEL2
_ENVIRONMENT_MODEL3 = _environment_model("SENSENOVA_MODEL3")
if _ENVIRONMENT_MODEL3:
    MODELS[ENVIRONMENT_MODEL3_KEY] = _ENVIRONMENT_MODEL3
_ENVIRONMENT_MODEL4 = _environment_model("SENSENOVA_MODEL4")
if _ENVIRONMENT_MODEL4:
    MODELS[ENVIRONMENT_MODEL4_KEY] = _ENVIRONMENT_MODEL4
_ENVIRONMENT_MODEL5 = _environment_model("SENSENOVA_MODEL5")
if _ENVIRONMENT_MODEL5:
    MODELS[ENVIRONMENT_MODEL5_KEY] = _ENVIRONMENT_MODEL5

# Stable deployment keys for the three DPO 823 replicas.  Keep the
# human-readable name as the actual registry key; unlike a per-user custom
# model this never leaks an implementation identifier such as ``custom:7``.
DPO823_MODELS = {
    "dpo823-1": "http://10.119.20.24:28027/v1",
    "dpo823-2": "http://10.119.20.24:28029/v1",
    "dpo823-3": "http://10.119.20.24:28038/v1",
}
for _dpo823_key, _dpo823_base_url in DPO823_MODELS.items():
    MODELS[_dpo823_key] = {
        "label": _dpo823_key,
        "backend": "openai",
        "engine_model": "dpskopd160dpo87v1_0823_27",
        "base_url": _dpo823_base_url,
        "api_key": "",
        "slide_concurrency": 4,
        "multimodal": True,
        "thinking_mode": "none",
        "thinking_transport": "",
        "deployment_managed": True,
    }
_ANTHROPIC_MODEL = _anthropic_env_model()
if _ANTHROPIC_MODEL:
    MODELS[ANTHROPIC_MODEL_KEY] = _ANTHROPIC_MODEL
# All Opus variants use the same health-checked, load-balanced teacher pool.
# A deployment may still set SENSENOVA_ANTHROPIC2_API_KEY_ENV explicitly.
_ANTHROPIC_MODEL2 = _anthropic_env_model(
    prefix="SENSENOVA_ANTHROPIC2",
    default_name="claude-opus-5",
    default_label="Opus 5",
    default_enable=True,
)
if _ANTHROPIC_MODEL2:
    MODELS[ANTHROPIC_MODEL2_KEY] = _ANTHROPIC_MODEL2

# Opus 4.8 Thinking is exposed by TokenHub under the API model name below.
# Claude Code's bracketed ``[1m]`` selector is a client-side spelling and is
# not accepted by the gateway's Messages API.
_ANTHROPIC_MODEL3 = _anthropic_env_model(
    prefix="SENSENOVA_ANTHROPIC3",
    default_name="claude-opus-4-8-thinking",
    default_label="Opus 4.8 Thinking",
    default_enable=True,
)
if _ANTHROPIC_MODEL3:
    MODELS[ANTHROPIC_MODEL3_KEY] = _ANTHROPIC_MODEL3

# 老 deck 的 DB 里可能存了历史 model key(label/id 漂移期或旧版本留下的),映射到现行注册表 key,
# 保证老 deck 仍可解析、不报「未知模型」。新建 deck 一律存归一后的现行 key。
ALIASES = {}


def canon(model_key: str):
    """把(可能是旧的)model key 归一到现行注册表 key。空→默认;无法识别→None。"""
    if not model_key:
        return DEFAULT_MODEL
    if model_key in MODELS:
        return model_key
    alias = ALIASES.get(model_key)
    if alias in MODELS:
        return alias
    return None


def model_option_label(model: dict) -> str:
    """下拉框展示注册表里的友好别名。"""
    return (model.get("label") or model.get("engine_model") or "").strip()


def thinking_capability(model_key: str = "", model_config: dict | None = None) -> dict:
    """Return the model's user-controllable thinking contract."""
    model = model_config
    if model is None:
        key = canon(model_key)
        model = MODELS.get(key or "", {})
    mode = str(model.get("thinking_mode") or "none")
    transport = _normalize_thinking_transport(model.get("thinking_transport"))
    if transport not in {
        "anthropic", "deepseek", "chat_template_kwargs", "reasoning_effort",
    }:
        transport = ""
    model_capability = service_config.model_effort_capability(model.get("engine_model", ""))
    kimi = str(model.get("engine_model") or "").strip().lower() == "kimi-k3"
    if kimi:
        mode, transport = "always", "reasoning_effort"
    effort_levels = (model_capability["levels"] if kimi
                     else _effort_levels(model.get("thinking_effort_levels")))
    if model_capability["levels"] and any(level not in model_capability["levels"] for level in effort_levels):
        raise ValueError(f"Unsupported effort levels for {model.get('engine_model')}: {effort_levels}")
    effort = str(model.get("thinking_effort") or "").strip().lower()
    if mode not in {"toggle", "always", "none"}:
        mode = "none"
    if mode in {"toggle", "always"} and not transport:
        mode = "none"
    if effort_levels and effort and effort not in effort_levels:
        raise ValueError(f"Default effort {effort!r} is not in {effort_levels}")
    if not effort and effort_levels:
        preferred = model_capability["default"] or "medium"
        effort = preferred if preferred in effort_levels else effort_levels[0]
    return {
        "mode": mode,
        "toggle": mode == "toggle" and bool(transport),
        "transport": transport if mode in {"toggle", "always"} else "",
        "effort": effort if mode in {"toggle", "always"} else "",
        "effort_levels": effort_levels if mode in {"toggle", "always"} else (),
        "effort_levels_without_thinking": tuple(level for level in effort_levels
            if level in model_capability["without_thinking"]) if mode == "toggle" else (),
        "effort_select": bool(effort_levels) and mode in {"toggle", "always"},
    }


def resolve_thinking(model_key: str, requested: bool,
                     model_config: dict | None = None,
                     requested_effort: str = "") -> dict:
    """Resolve the UI choice into the behavior the backend will actually use."""
    capability = thinking_capability(model_key, model_config=model_config)
    if capability["mode"] == "always":
        effective = True
    elif capability["toggle"]:
        effective = bool(requested)
    else:
        effective = False
    raw_effort = str(requested_effort or "").strip().lower()
    if raw_effort and raw_effort not in capability["effort_levels"]:
        raise ValueError(f"Unsupported effort {raw_effort!r}; supported levels: {', '.join(capability['effort_levels']) or 'none'}")
    levels = capability["effort_levels"] if effective else capability["effort_levels_without_thinking"]
    if raw_effort and levels and raw_effort not in levels:
        raise ValueError(f"effort={raw_effort} requires thinking enabled for this model")
    effective_effort = (raw_effort or capability["effort"]) if levels else ""
    if effective_effort and effective_effort not in levels:
        raise ValueError(f"effort={effective_effort} requires thinking enabled for this model")
    return {
        **capability,
        "requested": bool(requested),
        "effective": effective,
        "requested_effort": raw_effort,
        "effective_effort": effective_effort,
    }


def user_selectable_models():
    """Return deployment-visible built-ins plus optional env models.

    When ``PPTAGENT_PUBLIC_MODEL_KEYS`` is absent, development keeps the
    historical four-model catalog.  Setting it to an empty value intentionally
    exposes no built-ins, which is the release default.  A complete
    ``SENSENOVA_MODEL_BASE_URL`` + ``SENSENOVA_MODEL_NAME`` pair is always
    registered as additional, honestly labelled models.
    """
    development_defaults = (
        "sensenova-flash-lite-v39",
        "sensenova-flash-lite-v39-2",
        "sensenova-flash-lite-v39-4",
        "pptagent-qwen35-27b-ckpt1764",
    )
    configured = os.environ.get("PPTAGENT_PUBLIC_MODEL_KEYS")
    keys = list(development_defaults) if configured is None else [
        key.strip() for key in configured.split(",") if key.strip()
    ]
    for key, model in (
        (ENVIRONMENT_MODEL_KEY, _ENVIRONMENT_MODEL),
        (ENVIRONMENT_MODEL2_KEY, _ENVIRONMENT_MODEL2),
        (ENVIRONMENT_MODEL3_KEY, _ENVIRONMENT_MODEL3),
        (ENVIRONMENT_MODEL4_KEY, _ENVIRONMENT_MODEL4),
        (ENVIRONMENT_MODEL5_KEY, _ENVIRONMENT_MODEL5),
        (ANTHROPIC_MODEL_KEY, _ANTHROPIC_MODEL),
        (ANTHROPIC_MODEL2_KEY, _ANTHROPIC_MODEL2),
        (ANTHROPIC_MODEL3_KEY, _ANTHROPIC_MODEL3),
    ):
        if model and key not in keys:
            keys.append(key)
    return [
        (key, MODELS[key])
        for key in dict.fromkeys(keys)
        if key in MODELS and not MODELS[key].get("ui_hidden")
    ]


def pipeline_for_skill(skill_key: str):
    """Skill 是选择真源；所有静态 Skill 都返回各自配套的 infer Harness。"""
    skill = SKILLS.get(canon_skill(skill_key))
    return skill.get("pipeline") if skill else None


def canon_pipeline(pipeline_key: str, model_key: str = None, skill_key: str = None):
    """有 Skill 时强制使用其配对 Harness;否则规范化显式/默认 pipeline。"""
    if skill_key:
        return pipeline_for_skill(skill_key)
    if not pipeline_key:
        return DEFAULT_PIPELINE
    return pipeline_key if pipeline_key in PIPELINES else None


def canon_skill(skill_key: str, pipeline_key: str = None):
    """空选择使用正式 v1；历史或未知版本明确不可运行。"""
    if not skill_key:
        return DEFAULT_SKILL
    return skill_key if skill_key in SKILLS else None


def default_generation_stack(model_key: str = None) -> dict:
    """UI-facing automatic model -> pipeline/skill mapping.

    The page no longer exposes pipeline selection. Keep this as the single
    adaptation point so future pipeline upgrades only change registry/defaults.
    """
    model_key = canon(model_key) or DEFAULT_MODEL
    skill_key = canon_skill("")
    pipeline_key = pipeline_for_skill(skill_key)
    return {
        "model": model_key,
        "pipeline": pipeline_key,
        "pipeline_label": PIPELINES[pipeline_key]["label"],
        "skill": skill_key,
        "skill_label": SKILLS[skill_key]["label"],
    }


def validate_selection(model_key: str, pipeline_key: str, skill_key: str,
                       model_config: dict | None = None):
    model = model_config or MODELS[canon(model_key) or DEFAULT_MODEL]
    skill = SKILLS.get(skill_key)
    if not skill:
        return "未知 skill"
    if not skill.get("ready", False):
        return skill.get("unavailable_reason") or "该 Skill 当前不可用"
    expected_pipeline = pipeline_for_skill(skill_key)
    if not expected_pipeline:
        return skill.get("unavailable_reason", "该 Skill 没有可运行的 Harness")
    if pipeline_key != expected_pipeline:
        return f"{skill['label']} 必须使用配对 Harness {expected_pipeline}"
    pipe = PIPELINES.get(expected_pipeline)
    if not pipe:
        return "未知 pipeline"
    if not pipe.get("ready", False):
        return pipe.get("unavailable_reason") or "该 Harness 当前不可用"
    if model["backend"] not in set(pipe["supports"]):
        return f"{pipe['label']} 暂不支持 {model['backend']} 模型"
    # v0.4 can decouple authoring from pixel inspection: a text-only main
    # model uses the deployment's isolated Gemini Vision Critic.  Older MURAL
    # snapshots still require the selected main model itself to be multimodal.
    if skill_key in {
        "mural-presenter-v0.2",
        "mural-presenter-v0.2-grouped",
        "mural-presenter-v0.3",
    } and (
        model.get("multimodal") is False
    ):
        return (
            f"{skill['label']} 使用同一主模型完成像素检查；"
            f"{model.get('label') or model.get('engine_model')} 是文本接口，"
            "请改选真正支持图像输入的 PPTAgent/多模态模型。"
        )
    return None


# Provider 配置由 service_config 指定唯一来源；v1 在真实请求中执行主备切换。


def _write_anthropic_override_dotenv(
    key_env_name: str,
    key: str,
    base_url: str,
    model: str,
    *,
    pool_keys: tuple[str, ...] | list[str] = (),
    channel_labels: tuple[str, ...] | list[str] = (),
) -> str:
    """Write a per-model anthropic dotenv and return its path.

    The v0.4/v0.5 harness's run_batch.load_dotenv() OVERRIDES process env from
    CLEAN_DOTENV_PATH. For a per-model key override (Opus 5 on the A-platform
    key) we point CLEAN_DOTENV_PATH at this file so the harness re-applies the
    override key/base/model instead of the shared-pool default.  The override
    must retain the shared tool credentials (search, image generation, judges,
    etc.); otherwise pinning a teacher model silently disables those lifecycle
    roles.  Vision routing is intentionally omitted so the process-level native
    vision setting remains authoritative.  Written 0600 under the git-ignored
    studio data dir; contents are secret and never committed.
    """
    out_dir = DATA_DIR / "anthropic-override"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(
        c if c.isalnum() or c in "-_" else "_"
        for c in (key_env_name or "override")
    )
    # Keep concurrently dispatched model/channel selections immutable between
    # selection and child startup.  The one-way suffix does not expose the key.
    identity = hashlib.sha256(
        f"{key}\0{base_url}\0{model}\0{'|'.join(pool_keys)}".encode("utf-8")
    ).hexdigest()[:12]
    path = out_dir / f"{safe}-{identity}.env"
    lines: list[str] = []
    try:
        with open(service_config.PROVIDER_ENV_PATH, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                name = stripped.split("=", 1)[0].strip().upper()
                if (
                    name == "ANTHROPIC_BASE_URL"
                    or name == "ANTHROPIC_MODEL"
                    or name.startswith("ANTHROPIC_API_KEY")
                    or name == "ANTHROPIC_API_CHANNEL_LABELS"
                    or name == "ANTHROPIC_CHANNEL_BALANCE"
                    or name == "VISION_BACKEND"
                    or name.startswith("VISION_")
                    or name.startswith("TOKENHUB_VISION")
                ):
                    continue
                lines.append(line.rstrip("\n"))
    except OSError:
        pass
    lines.append(f"ANTHROPIC_API_KEY={key}")
    ordered_pool: list[str] = []
    for item in (key, *pool_keys):
        value = str(item or "").strip()
        if value and value not in ordered_pool:
            ordered_pool.append(value)
    if len(ordered_pool) > 1:
        lines.append(f"ANTHROPIC_API_KEYS={','.join(ordered_pool)}")
        lines.append(
            f"ANTHROPIC_API_KEY_FALLBACKS={','.join(ordered_pool[1:])}"
        )
        labels = [str(item or "").strip() for item in channel_labels]
        if len(labels) == len(ordered_pool) and all(labels):
            lines.append(f"ANTHROPIC_API_CHANNEL_LABELS={','.join(labels)}")
        lines.append("ANTHROPIC_CHANNEL_BALANCE=balanced")
    if base_url:
        lines.append(f"ANTHROPIC_BASE_URL={base_url}")
    if model:
        lines.append(f"ANTHROPIC_MODEL={model}")
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return str(path)


def _write_native_vision_override_dotenv(base_dotenv: str) -> str:
    """Write a dotenv that mirrors the shared pool but FORCES native vision.

    The harness's run_batch.load_dotenv() reads CLEAN_DOTENV_PATH and OVERRIDES
    process env, so the shared .secrets/.env line ``VISION_BACKEND=gemini`` (a
    legacy 27B external-critic default) clobbers the ``same_model_aux`` we inject
    for a multimodal openai student — routing v0.6 vision to an external gemini
    critic instead of native in-context. For the native paper profile, copy the
    base dotenv but strip every VISION_BACKEND/VISION_*/TOKENHUB_VISION* line and
    pin VISION_BACKEND=native_incontext, so the post-dotenv effective backend is
    native regardless of model backend. Anthropic key overrides already own their
    own dotenv path; this one is for the openai / shared-pool case.
    """
    out_dir = DATA_DIR / "vision-override"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "native-vision.env"
    kept: list[str] = []
    try:
        with open(base_dotenv, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key = stripped.split("=", 1)[0].strip().upper()
                if key == "VISION_BACKEND" or key.startswith("VISION_") \
                        or key.startswith("TOKENHUB_VISION"):
                    continue
                kept.append(line.rstrip("\n"))
    except OSError:
        pass
    kept.append("VISION_BACKEND=native_incontext")
    tmp = path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return str(path)


_KEY_CACHE: dict[str, dict[str, object]] = {}
_KEY_CACHE_TTL = 120.0            # 探活结果缓存;deck 派发不是高频路径,120s 足够且省探活开销
_REFRESH_LOCK = threading.Lock()
_REFRESHING: set[str] = set()


def _deployment_secret(name: str, file_env: dict | None = None) -> str:
    """Resolve a model credential from safe deployment aliases or .env."""
    namespaced = str(os.environ.get(f"SENSENOVA_{name}") or "").strip()
    if namespaced:
        return namespaced
    return str((file_env or _read_env_file(service_config.PROVIDER_ENV_PATH)).get(name) or "").strip()


def _external_vision_runtime_env() -> dict[str, str]:
    """Resolve the deployment-owned isolated Vision Critic configuration.

    The WebUI historically exposes this service as ``VISION_ONESHOT_*`` while
    the v0.4 Harness names it ``VISION_CRITIC_*``.  Normalize the aliases in
    the per-job environment so a text-only authoring model never receives
    image content and the trace records the effective critic explicitly.
    """
    file_env = _read_env_file(PROJECT_ROOT / ".env")
    file_env.update(_read_env_file(service_config.PROVIDER_ENV_PATH))

    def pick(*names: str, default: str = "") -> str:
        for name in names:
            value = str(
                os.environ.get(f"SENSENOVA_{name}")
                or os.environ.get(name)
                or file_env.get(f"SENSENOVA_{name}")
                or file_env.get(name)
                or ""
            ).strip()
            if value:
                return value
        return default

    env = {
        "VISION_BACKEND": "external_model",
        "VISION_CRITIC_MODEL": pick(
            "VISION_CRITIC_MODEL",
            "VISION_ONESHOT_MODEL",
            "VISION_GEMINI_MODEL",
            default="gemini-3.5-flash",
        ),
        "VISION_CRITIC_BASE_URL": pick(
            "VISION_CRITIC_BASE_URL",
            "VISION_ONESHOT_BASE_URL",
            "VISION_GEMINI_BASE_URL",
            default="https://tokenhub.sensetime.com/v1",
        ).rstrip("/"),
    }
    key = pick(
        "VISION_CRITIC_API_KEY",
        "VISION_ONESHOT_API_KEY",
        "GEMINI_API_KEY",
    )
    if key:
        env["VISION_CRITIC_API_KEY"] = key
    timeout = pick("VISION_CRITIC_TIMEOUT", "VISION_ONESHOT_TIMEOUT")
    if timeout:
        env["VISION_CRITIC_TIMEOUT"] = timeout
    max_tokens = pick("VISION_CRITIC_MAX_TOKENS", "VISION_ONESHOT_MAX_TOKENS")
    if max_tokens:
        env["VISION_CRITIC_MAX_TOKENS"] = max_tokens
    return env


def _anthropic_pool():
    """Return the ordered, deduplicated shared Anthropic channel pool."""
    channel_env = _read_env_file(service_config.PROVIDER_ENV_PATH)
    base = (
        channel_env.get("ANTHROPIC_BASE_URL")
        or "https://tokenhub.sensetime.com"
    ).rstrip("/")
    keys = []
    primary = str(channel_env.get("ANTHROPIC_API_KEY") or "").strip()
    if primary:
        keys.append(primary)
    configured_pool = (
        channel_env.get("ANTHROPIC_API_KEYS")
        or ""
    )
    for k in configured_pool.replace("\n", ",").split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    fallbacks = (
        channel_env.get("ANTHROPIC_API_KEY_FALLBACKS")
        or ""
    )
    for k in fallbacks.split(","):
        k = k.strip()
        if k and k not in keys:
            keys.append(k)
    return base, keys


def _anthropic_pool_labels(keys: list[str]) -> list[str]:
    channel_env = _read_env_file(service_config.PROVIDER_ENV_PATH)
    raw = (
        channel_env.get("ANTHROPIC_API_CHANNEL_LABELS")
        or ""
    )
    labels = [item.strip() for item in raw.split(",")]
    return [
        labels[index] if index < len(labels) and labels[index] else str(index + 1)
        for index, _key in enumerate(keys)
    ]


def shared_provider_channel_retired(model_key: str, channel_id: str) -> bool:
    """仅共享模型池允许在运维移除故障凭证后手动恢复，不替换用户私有账号。"""
    model = MODELS.get(canon(model_key)) or {}
    if model.get("backend") != "anthropic" or model.get("api_key_env"):
        return False
    fingerprint = channel_id.rsplit("-", 1)[-1]
    if not channel_id.startswith("ch-") or len(fingerprint) != 8:
        return False
    _base, keys = _anthropic_pool()
    return bool(keys) and all(
        hashlib.sha256(key.encode("utf-8")).hexdigest()[:8] != fingerprint
        for key in keys
    )


def _probe_key(base_url, key, model="deployment-model", timeout=4) -> bool:
    """极小的 /v1/messages 探活:200=通道+key 都活;503(无可用通道)/401(坏 key)/超时=不可用。"""
    body = json.dumps({"model": model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(
        base_url + "/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code == 200          # 4xx/5xx(含 503 无通道)一律不可用
    except Exception:
        return False                  # 超时 / 连不上 = 不可用


def _refresh_key(model: str = "deployment-model") -> str:
    """探活主→备，按模型缓存首个可用通道。"""
    base, keys = _anthropic_pool()
    if not keys:
        return ""
    chosen = keys[0]                  # 兜底:全挂也用主 key
    for k in keys:
        if _probe_key(base, k, model):
            chosen = k
            break
    _KEY_CACHE[model] = {"key": chosen, "ts": time.time()}
    return chosen


def invalidate_anthropic_key(model: str = "deployment-model") -> None:
    """Forget one model's channel before an explicit task-level resume."""
    with _REFRESH_LOCK:
        _KEY_CACHE.pop(model, None)


def resolve_anthropic_key(model: str = "deployment-model") -> str:
    """Return a model-compatible channel, probing synchronously only once.

    Different gateway keys may expose different model families.  A cold
    server therefore performs one bounded compatibility probe before its first
    expensive Deck; later dispatches use the cache and refresh stale entries
    in the background.
    """
    now = time.time()
    record = _KEY_CACHE.get(model) or {}
    cached = str(record.get("key") or "")
    _base, keys = _anthropic_pool()
    if cached not in keys:
        cached = ""
    if cached and now - float(record.get("ts") or 0.0) < _KEY_CACHE_TTL:
        return cached
    if not cached:
        return _refresh_key(model) or (keys[0] if keys else "")
    if model not in _REFRESHING:                # 过期:每个模型只触发一个后台刷新
        with _REFRESH_LOCK:
            if model not in _REFRESHING:
                _REFRESHING.add(model)

                def _bg():
                    try:
                        _refresh_key(model)
                    finally:
                        with _REFRESH_LOCK:
                            _REFRESHING.discard(model)

                threading.Thread(target=_bg, daemon=True).start()
    if cached:
        return cached                         # 有旧值先用(哪怕刚过期),后台随即更新
    return keys[0] if keys else ""


def resolve_base_url(model: dict) -> str:
    """openai 端点优先取 serve 哨兵文件(serve 重起自动更新),读不到/不合法再退回硬编码 base_url。
    哨兵一行格式:`http://HOST:PORT/v1  (model=ENGINE_MODEL)`。带 model= 注释时校验一致,
    防止哨兵串到别的模型上。"""
    hard = model.get("base_url", "")
    ef = model.get("endpoint_file")
    if not ef:
        return hard
    try:
        line = open(ef, encoding="utf-8").read().strip()
    except OSError:
        return hard
    if not line:
        return hard
    url = line.split()[0].rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        return hard
    if "model=" in line:                      # 哨兵标注了模型 → 校验一致,不一致就别信它
        ann = line.split("model=", 1)[1].strip().rstrip(")").strip()
        if model.get("engine_model") and ann != model["engine_model"]:
            return hard
    return url


def selection_env(model_key: str, pipeline_key: str, skill_key: str,
                  model_config: dict | None = None, *, skill_config: dict | None = None) -> dict:
    """该模型/pipeline/skill 在引擎子进程里需要的额外环境变量。"""
    skill_key = canon_skill(skill_key, pipeline_key)
    if skill_key is None:
        raise ValueError("unsupported_harness: only mural-presenter-v1 is active")
    pipeline_key = pipeline_for_skill(skill_key)
    m = model_config or MODELS[canon(model_key) or DEFAULT_MODEL]
    skill = skill_config if skill_config is not None else SKILLS[skill_key]
    pipe = _mural_v1_pipeline(skill)
    env = {}
    effective_model_name = m["engine_model"]
    if m["backend"] == "openai":
        model_base_url = resolve_base_url(m)
        model_name = m["engine_model"]
        effective_model_name = model_name
        env.update({"MODEL_BACKEND": "openai",
                    "STUDENT_BASE_URL": model_base_url.rstrip("/"),
                    "STUDENT_MODEL": model_name,
                    # prompt caching 是 anthropic 专属:引擎会给 system 挂 cache_control,
                    # openai shim 原样塞进 payload,严格 openai 端点(tokenhub gpt-5.5)直接 400
                    # (Unknown parameter 'cache_control')。vLLM 学生本就忽略它,openai 一律关。
                    "PROMPT_CACHE": "0"})
        if "multimodal" in m:
            if m.get("multimodal"):
                env["VISION_BACKEND"] = "same_model_aux"
            elif skill_key in ("mural-presenter-v0.4", "mural-presenter-v0.4-thin"):
                env.update(_external_vision_runtime_env())
            else:
                env["VISION_BACKEND"] = "disabled"
        thinking = thinking_capability(model_key, model_config=m)
        if thinking["transport"]:
            env["STUDIO_THINKING_TRANSPORT"] = thinking["transport"]
        if thinking["effort"]:
            env["THINK_EFFORT"] = thinking["effort"]
        # 认证型 openai 端点(如 tokenhub gpt-5.5):从本地 .env 读 key → STUDENT_API_KEY。
        # vLLM 学生没有 api_key_env,保持默认 EMPTY,不会把真 key 发到它们那里。
        direct_key = m.get("api_key", "")
        if direct_key:
            env["STUDENT_API_KEY"] = direct_key
        key_env = m.get("api_key_env")
        if key_env and not direct_key:
            key = _deployment_secret(key_env)
            if key:
                env["STUDENT_API_KEY"] = key
    elif m["backend"] == "anthropic":
        # An explicit per-model gateway/key override takes precedence; normal
        # Opus deployments receive the complete shared pool for request-level
        # balancing and immediate transport failover inside the Harness.
        override_key_env = m.get("api_key_env")
        override_key = _deployment_secret(override_key_env) if override_key_env else ""
        if override_key:
            override_keys = list(dict.fromkeys(item.strip() for item in override_key.split(",") if item.strip()))
            override_key = override_keys[0]
            env["ANTHROPIC_API_KEY"] = override_key
            if len(override_keys) > 1:
                env["ANTHROPIC_API_KEYS"] = ",".join(override_keys)
                env["ANTHROPIC_API_KEY_FALLBACKS"] = ",".join(override_keys[1:])
                env["ANTHROPIC_API_CHANNEL_LABELS"] = ",".join(str(i + 1) for i in range(len(override_keys)))
                env["ANTHROPIC_CHANNEL_BALANCE"] = "primary-first"
            if m.get("base_url"):
                env["ANTHROPIC_BASE_URL"] = m["base_url"]
            # The v0.4/v0.5 harness's run_batch.load_dotenv() OVERRIDES env vars
            # from CLEAN_DOTENV_PATH (shared .secrets/.env → default key). That
            # would clobber this per-model key back to the shared-pool key and
            # route Opus 5 to a channel group without opus5 (503). Point
            # CLEAN_DOTENV_PATH at a per-model override file so the harness's
            # override re-applies THIS key instead.
            if skill_key != "mural-presenter-v1":
                env["_ANTHROPIC_OVERRIDE_DOTENV"] = _write_anthropic_override_dotenv(
                    override_key_env, override_key,
                    env.get("ANTHROPIC_BASE_URL", ""), m["engine_model"],
                    pool_keys=override_keys,
                )
            # The env doctor's live multimodal probe sends max_tokens=16 with no
            # thinking control. A thinking model (Opus 5) spends that budget on an
            # (empty) thinking block and returns no text → the probe false-negatives
            # despite HTTP 200. The text probe + real vision (proper budget at
            # generation) work, so skip the flaky live probe for this model only.
            env["CLEAN_DOCTOR_LIVE"] = "0"
        else:
            pool_base, pool_keys = _anthropic_pool()
            key = ((pool_keys[0] if pool_keys else "") if skill_key == "mural-presenter-v1"
                   else resolve_anthropic_key(m["engine_model"]))
            if key:
                env["ANTHROPIC_API_KEY"] = key
                env["ANTHROPIC_BASE_URL"] = pool_base
                source_labels = _anthropic_pool_labels(pool_keys)
                label_by_key = dict(zip(pool_keys, source_labels))
                ordered_keys = [key, *(item for item in pool_keys if item != key)]
                ordered_labels = [
                    label_by_key.get(item, str(index + 1))
                    for index, item in enumerate(ordered_keys)
                ]
                env["ANTHROPIC_API_KEYS"] = ",".join(ordered_keys)
                env["ANTHROPIC_API_KEY_FALLBACKS"] = ",".join(
                    ordered_keys[1:]
                )
                env["ANTHROPIC_API_CHANNEL_LABELS"] = ",".join(ordered_labels)
                env["ANTHROPIC_CHANNEL_BALANCE"] = (
                    _read_env_file(service_config.PROVIDER_ENV_PATH).get("ANTHROPIC_CHANNEL_BALANCE")
                    or "primary-first"
                )
                # CLEAN_DOTENV_PATH intentionally overrides process env inside
                # the Harness.  Persist this health-checked channel selection in
                # a per-dispatch dotenv; otherwise the shared file would replace
                # the selected fallback with the unhealthy primary again.
                if skill_key != "mural-presenter-v1":
                    env["_ANTHROPIC_OVERRIDE_DOTENV"] = _write_anthropic_override_dotenv(
                        f"shared-{m['engine_model']}",
                        key,
                        pool_base,
                        m["engine_model"],
                        pool_keys=ordered_keys,
                        channel_labels=ordered_labels,
                    )
        # anthropic teacher 也支持 selection:vision 用主模型自身、thinking 可控。
        # engine_model 冗余进 ANTHROPIC_MODEL(取值链 MODEL > ANTHROPIC_MODEL 均命中)。
        env["ANTHROPIC_MODEL"] = m["engine_model"]
        if m.get("multimodal"):
            env["VISION_BACKEND"] = "same_model_aux"
        elif skill_key in ("mural-presenter-v0.4", "mural-presenter-v0.4-thin"):
            env.update(_external_vision_runtime_env())
        thinking = thinking_capability(model_key, model_config=m)
        if thinking["effort"]:
            env["THINK_EFFORT"] = thinking["effort"]
    env.update({
        # Keep the generic runtime selector aligned with STUDENT_MODEL when a
        # deployment overrides the built-in default model from .env/shell.
        "MODEL": effective_model_name,
        # 空值是 clean infer 的自动语言选择契约。
        "CLEAN_FORCE_SKILL_LANGUAGE": skill.get(
            "force_skill_language",
            "" if skill["language"] == "auto" else skill["language"],
        ),
        # Reuse the Web Demo's credential file; the clean source tree remains
        # code/Skill/Harness only and never receives copied secrets. A per-model
        # anthropic key override (Opus 5 on the A-platform key) supplies its own
        # dotenv so the harness's load_dotenv override re-applies that key.
        "CLEAN_DOTENV_PATH": env.pop("_ANTHROPIC_OVERRIDE_DOTENV", None)
        or str(service_config.PROVIDER_ENV_PATH),
    })
    if skill.get("agent_language_routing") == "first-skill-read-lock":
        env["CLEAN_MODEL_SELECT_SKILL"] = "1"
    env["PPT_CAPABILITY_PROFILE"] = "visual"
    env.update(skill.get("runtime_env", {}))
    if skill_key == "mural-presenter-v1.0":
        blind = _external_vision_runtime_env()
        blind.pop("VISION_BACKEND", None)
        blind_key = str(blind.get("VISION_CRITIC_API_KEY") or "").strip()
        for key, value in blind.items():
            if value:
                env[key] = value
        env["CLEAN_INDEPENDENT_VISION_CRITIC"] = (
            "1" if blind_key else "0"
        )
        model_identity = " ".join(
            str(m.get(field) or "")
            for field in ("label", "engine_model", "name")
        ).lower()
        if m.get("backend") == "openai" and any(
            marker in model_identity for marker in ("27b", "qwen35", "qwen3.5")
        ):
            # Transport/liveness envelope only: semantic grouping, page
            # contracts and convergence remain identical to teacher runs.
            env["CLEAN_SLIDE_MAX_TOKENS"] = "16384"
            env["IMAGE_MAX_TOKENS"] = "32768"
            env["CLEAN_SLIDE_ACTIVE_RESPONSE_TIMEOUT"] = "240"
    # Group formation remains semantic and independent of provider capacity;
    # these values only cap how many already-formed owners execute at once.
    # 旧 Harness 仍沿用模型级 child 容量；当前 v1 的服务级模型并发只由
    # runtime JSON 定义，避免模型注册表形成第二个覆盖入口。
    slide_slots = m.get("slide_concurrency")
    if slide_slots and skill_key != "mural-presenter-v1":
        slot_count = max(1, int(slide_slots))
        env["SLIDE_CONCURRENCY"] = str(slot_count)
        if skill_key == "mural-presenter-v1.0":
            env["CLEAN_CHILD_CONCURRENCY"] = str(slot_count)
            env["CLEAN_CHILD_POOL_MAX_WORKERS"] = str(slot_count)
    if skill_key == "mural-presenter-v1":
        env["MODEL_API_FORMAT"] = str(m.get("api_format") or m.get("backend") or "").lower()
        env["MODEL"] = effective_model_name
        if m.get("backend") == "openai":
            env["MODEL_BASE_URL"] = str(env.get("STUDENT_BASE_URL") or "").rstrip("/")
            env["MODEL_API_KEY"] = str(env.get("STUDENT_API_KEY") or "EMPTY")
            if m.get("route_pool_path"):
                if env["MODEL_API_FORMAT"] != "openai_responses" or not effective_model_name.startswith("gpt-6-astra/"):
                    raise ValueError("route pools are only supported for GPT-6 Responses models")
                env["MODEL_ROUTE_POOL_PATH"] = str(m["route_pool_path"])
        else:
            base_url = str(env.get("ANTHROPIC_BASE_URL") or "").rstrip("/")
            if not base_url:
                base_url, _pool_keys = _anthropic_pool()
            env["MODEL_BASE_URL"] = base_url
            env["MODEL_API_KEY"] = str(env.get("ANTHROPIC_API_KEY") or "")
            env["MODEL_CHANNEL_BALANCE"] = str(env.get("ANTHROPIC_CHANNEL_BALANCE") or "primary-first")
            if env.get("ANTHROPIC_API_KEYS"):
                env["MODEL_API_KEYS"] = str(env["ANTHROPIC_API_KEYS"])
            if env.get("ANTHROPIC_API_KEY_FALLBACKS"):
                env["MODEL_API_KEY_FALLBACKS"] = str(
                    env["ANTHROPIC_API_KEY_FALLBACKS"]
                )
            if env.get("ANTHROPIC_API_CHANNEL_LABELS"):
                env["MODEL_CHANNEL_LABELS"] = str(
                    env["ANTHROPIC_API_CHANNEL_LABELS"]
                )
    # Native-vision paper profile: the shared .secrets/.env pins
    # VISION_BACKEND=gemini (legacy external critic), and the harness's
    # load_dotenv() OVERRIDES process env from CLEAN_DOTENV_PATH after we set
    # same_model_aux — so without this a multimodal openai student (deepseek)
    # silently routes vision to gemini instead of native in-context. When the
    # skill declares the native paper profile, repoint CLEAN_DOTENV_PATH at a
    # sanitized copy that strips VISION_* and pins native, so the post-dotenv
    # effective backend is native for EVERY backend. Anthropic key overrides
    # already own their own dotenv (which carries no VISION_* line), so only
    # step in when CLEAN_DOTENV_PATH still points at the shared pool file.
    if str(env.get("CLEAN_VISION_PAPER_PROFILE", "")).strip().lower() in {
        "1", "true", "yes", "on"
    } and env.get("CLEAN_DOTENV_PATH") == str(service_config.PROVIDER_ENV_PATH):
        env["CLEAN_DOTENV_PATH"] = _write_native_vision_override_dotenv(
            str(service_config.PROVIDER_ENV_PATH)
        )
        env["VISION_BACKEND"] = "native_incontext"
    # Re-apply the per-model doctor-live override AFTER the skill runtime_env merge
    # (the v0.4 skill hardcodes CLEAN_DOCTOR_LIVE=1, which would otherwise clobber
    # the opus5 skip set in the anthropic branch above). A thinking model on a
    # per-key gateway false-negatives the live 16-token multimodal probe.
    if m["backend"] == "anthropic" and m.get("api_key_env"):
        env["CLEAN_DOCTOR_LIVE"] = "0"
    # Same false-negative for a multimodal openai student that reasons before
    # answering (e.g. deepseek-v4-flash-vision-exp): the doctor's max_tokens=16
    # colour probe gets consumed by reasoning_content and returns empty text
    # even though the model genuinely sees images (verified out-of-band). Skip
    # the flaky live probe for multimodal openai students under the native paper
    # profile; the config-only check still validates endpoint/model/key.
    if (
        m["backend"] == "openai"
        and m.get("multimodal")
        and str(env.get("CLEAN_VISION_PAPER_PROFILE", "")).strip().lower()
        in {"1", "true", "yes", "on"}
    ):
        env["CLEAN_DOCTOR_LIVE"] = "0"
    env.update({
        "PIPELINE_VERSION": pipeline_key,
        "PPT_SKILL_VERSION": skill_key,
        # Catalog sources are frozen inputs; importing a Harness must not
        # create __pycache__ inside the repository-owned snapshot.
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def deck_run_dir(user_id, deck_id) -> Path:
    return WORKSPACES_DIR / str(user_id) / "decks" / str(deck_id)


UPLOADS_DIR = DATA_DIR / "uploads"        # 附件独立存这里:引擎建 run_dir 用 exist_ok=False,不能被 studio 抢先创建


def deck_uploads_dir(deck_id) -> Path:
    return UPLOADS_DIR / str(deck_id)


def _validated_resume_skill(workspace: str | Path) -> dict:
    """由正式运行时只读验证既有 pin，不发布对象，也不读取新选择的源码。"""
    command = (
        "import json, sys\n"
        "from runtime.shared_objects import SharedSkillStore, studio_data_root\n"
        "from runtime.workspace_paths import WorkspacePaths\n"
        "paths = WorkspacePaths.from_root(sys.argv[1])\n"
        "ref = SharedSkillStore(studio_data_root(paths.root), create=False).load_ref(paths)\n"
        "print(json.dumps({'skill_name': ref.skill_name, 'object_root': str(ref.object_root)}))\n"
    )
    python = _engine_python()
    if python is None:
        raise ValueError("无法验证 resume Skill：正式 Python 不可用")
    try:
        result = subprocess.run(
            [python, "-B", "-c", command, str(Path(workspace).expanduser().resolve())],
            cwd=str(MURAL_V1_HARNESS_ROOT), capture_output=True, text=True,
            timeout=30, check=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "固定对象校验失败").strip().splitlines()[-1]
        raise ValueError(f"无法恢复固定 Skill：{detail}") from exc
    return json.loads(result.stdout)


def build_job(sample_id: str, seed: dict, run_dir, dry: bool = False, model_key: str = None,
              pipeline_key: str = None, skill_key: str = None,
              model_config: dict | None = None, *, resume_workspace: bool = False) -> dict:
    model_key = canon(model_key) or DEFAULT_MODEL if model_config is None else model_key
    skill_key = canon_skill(skill_key, pipeline_key)
    if skill_key is None:
        raise ValueError("未知 skill")
    if resume_workspace:
        skill = _mural_v1_skill(pinned_skill=_validated_resume_skill(run_dir))
        selected_skill_name = Path(
            os.environ.get("MURAL_SKILL_DIR")
            or _MURAL_REPOSITORY_ROOT / "skills" / "mural-presenter-v1"
        ).expanduser().name
    else:
        skill = refresh_external_skills(force=True)
        selected_skill_name = skill["name"]
    pipeline_key = skill["pipeline"]
    if not skill.get("ready", False):
        raise ValueError(skill.get("unavailable_reason") or "该 Skill 当前不可用")
    eng_seed = dict(seed)
    deck_language = skill.get("deck_language", skill["language"])
    if deck_language != "auto":
        eng_seed["lang"] = deck_language
    if eng_seed.get("slide_count") and not eng_seed.get("pages_hint"):
        eng_seed["pages_hint"] = int(eng_seed["slide_count"])
    if skill.get("authoring_profile"):
        eng_seed["authoring_profile"] = str(skill["authoring_profile"])
    pipe = _mural_v1_pipeline(skill)
    job = {
        "sample_id": sample_id,
        "seed": eng_seed,
        "run_dir": str(run_dir),
        "dry_run": bool(dry),
        "batch": "studio",
        "pipeline_version": pipeline_key,
        "pipeline": pipe,
        "skill_version": skill_key,
        "skill": skill,
        "selected_skill_name": selected_skill_name,
        "resume_workspace": bool(resume_workspace),
    }
    job["skill_mount_dir"] = skill["path"]
    m = model_config or MODELS[model_key]
    job["model"] = m["engine_model"]      # serve_one 会把它覆盖进 config["model"]
    return job


def write_job_file(deck_id, job: dict) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    p = JOBS_DIR / f"{deck_id}.json"
    p.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def log_path(deck_id) -> Path:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    return JOBS_DIR / f"{deck_id}.log"


def _engine_python() -> str | None:
    env_python = os.environ.get("PPTAGENT_ENGINE_PYTHON") or os.environ.get("ENGINE_PYTHON")
    candidates = []
    if env_python:
        candidates.append(Path(env_python).expanduser())
    # Local deployments keep the heavy generation dependencies in the
    # inference project venv.  Prefer it over the host Python; the latter is
    # only a deployment fallback paired with ENGINE_SITE_PACKAGES.
    candidates.append(INFERENCE_DIR / ".venv" / "bin" / "python")
    candidates.append(INFERENCE_DIR / ".venv" / "Scripts" / "python.exe")
    candidates.append(Path("/usr/bin/python3"))
    for python in candidates:
        if python.is_file() and os.access(python, os.X_OK):
            return str(python)
    return None


def _prepend_path(value: str, *prefixes: Path) -> str:
    parts = [str(p) for p in prefixes if p]
    if value:
        parts.append(value)
    return os.pathsep.join(parts)


def _playwright_revisions() -> dict[str, str]:
    """Read Chromium revisions required by the engine's Playwright package."""
    manifests = sorted(
        (INFERENCE_DIR / ".venv" / "lib").glob(
            "python*/site-packages/playwright/driver/package/browsers.json"
        )
    )
    for manifest in manifests:
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        revisions = {
            str(item.get("name")): str(item.get("revision"))
            for item in payload.get("browsers", [])
            if item.get("name") in {"chromium", "chromium-headless-shell"}
            and item.get("revision")
        }
        if revisions:
            return revisions
    return {}


def _installed_browser(cache: Path) -> Path | None:
    """Return an installed Playwright Chromium executable from *cache*.

    Playwright revisions change with the Python package, so the deployment must
    not pin a revision in Studio. Prefer the lightweight headless shell and
    retain full Chromium as a compatible fallback.
    """
    revisions = _playwright_revisions()
    patterns: list[str] = []
    headless_revision = revisions.get("chromium-headless-shell")
    chromium_revision = revisions.get("chromium")
    if headless_revision:
        patterns.append(
            f"chromium_headless_shell-{headless_revision}/"
            "chrome-headless-shell-linux64/chrome-headless-shell"
        )
    if chromium_revision:
        patterns.append(f"chromium-{chromium_revision}/chrome-linux64/chrome")
    # Custom/older Playwright packages may omit browsers.json. Only in
    # that case is a generic installed Chromium preferable to no browser.
    if not patterns:
        patterns.extend((
            "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
            "chromium-*/chrome-linux64/chrome",
        ))
    for pattern in patterns:
        candidates = sorted(cache.glob(pattern), reverse=True)
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
    return None


def _browser_runtime(env: dict) -> tuple[Path, Path | None]:
    """Resolve a usable Playwright cache without relying on parent-shell env."""
    explicit_cache = env.get("PLAYWRIGHT_BROWSERS_PATH") or os.environ.get(
        "PLAYWRIGHT_BROWSERS_PATH"
    )
    explicit_exe = env.get("PPT_SKILL_BROWSER_EXE") or os.environ.get(
        "PPT_SKILL_BROWSER_EXE"
    )
    if explicit_exe:
        executable = Path(explicit_exe).expanduser()
        if executable.is_file() and os.access(executable, os.X_OK):
            cache = Path(explicit_cache).expanduser() if explicit_cache else executable.parents[2]
            return cache, executable

    cache_candidates: list[Path] = []
    if explicit_cache:
        cache_candidates.append(Path(explicit_cache).expanduser())
    cache_candidates.append(DATA_DIR / "ms-playwright")
    xdg_cache = env.get("XDG_CACHE_HOME") or os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        cache_candidates.append(Path(xdg_cache).expanduser() / "ms-playwright")
    cache_candidates.append(Path.home() / ".cache" / "ms-playwright")

    seen: set[str] = set()
    for cache in cache_candidates:
        cache = cache.resolve(strict=False)
        if str(cache) in seen:
            continue
        seen.add(str(cache))
        executable = _installed_browser(cache)
        if executable:
            return cache, executable

    # Preserve an explicit location for Playwright's own actionable error. If
    # none was supplied, keep the deployment-local bootstrap destination.
    fallback = Path(explicit_cache).expanduser() if explicit_cache else DATA_DIR / "ms-playwright"
    return fallback, None


def _browser_library_dirs(env: dict) -> list[Path]:
    """Find the deployed Chromium shared-library bundles."""
    configured = env.get("PPT_SKILL_BROWSER_LIB_DIRS") or os.environ.get(
        "PPT_SKILL_BROWSER_LIB_DIRS", ""
    )
    candidates = [Path(value).expanduser() for value in configured.split(os.pathsep) if value]
    lib_root = CHROMIUM_DEPS_DIR / "root" / "usr" / "lib"
    candidates.extend((
        lib_root / "x86_64-linux-gnu",
        lib_root,
        Path.home() / "pwdeps" / "lib",
        Path.home() / "cdeps" / "lib",
    ))
    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.resolve(strict=False)
        if not candidate.is_dir() or str(candidate) in seen:
            continue
        seen.add(str(candidate))
        result.append(candidate)
    return result


def render_env(env: dict | None = None) -> dict:
    """Add local Chromium shared libraries for Playwright rendering.

    Catalog skills start Playwright Chromium from slide-agent shell commands.
    CCI images do not always provide the NSS/NSPR/GTK libraries
    Chromium needs, so deploy_gateway.sh bootstraps them into studio/data and
    every engine job inherits that directory through LD_LIBRARY_PATH.
    """
    out = dict(env or {})
    configured_python = out.get("PPTAGENT_ENGINE_PYTHON") or out.get("ENGINE_PYTHON")
    engine_python = None
    if configured_python:
        candidate = Path(configured_python).expanduser().absolute()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            engine_python = str(candidate)
    engine_python = engine_python or _engine_python()
    if engine_python:
        # Preserve a virtualenv's executable path instead of resolving its
        # symlink to the base interpreter.  Python uses the invoked venv path
        # to discover pyvenv.cfg and the environment's installed packages.
        engine_python = str(Path(engine_python).expanduser().absolute())
        out["PPTAGENT_ENGINE_PYTHON"] = engine_python
        out["NORMALIZE_PY"] = engine_python
        out["VIRTUAL_ENV"] = str(Path(engine_python).parent.parent)
        out["PYTHONNOUSERSITE"] = "1"
        out["PATH"] = _prepend_path(
            out.get("PATH") or os.environ.get("PATH", ""),
            Path(engine_python).parent,
        )
    browser_cache, browser_exe = _browser_runtime(out)
    browser_lib_dirs = _browser_library_dirs(out)
    out["PPTAGENT_CHROMIUM_DEPS"] = str(CHROMIUM_DEPS_DIR)
    out["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_cache)
    out["PPTAGENT_ENGINE_SITE_PACKAGES"] = str(ENGINE_SITE_PACKAGES)
    out["PYTHONPATH"] = _prepend_path(
        out.get("PYTHONPATH", ""),
        ENGINE_SITE_PACKAGES,
    )
    if browser_exe:
        out["PPT_SKILL_BROWSER_EXE"] = str(browser_exe)
    else:
        out.pop("PPT_SKILL_BROWSER_EXE", None)
    if browser_lib_dirs:
        out["PPT_SKILL_BROWSER_LIB_DIRS"] = os.pathsep.join(map(str, browser_lib_dirs))
    out["LD_LIBRARY_PATH"] = _prepend_path(
        out.get("LD_LIBRARY_PATH", ""),
        *browser_lib_dirs,
    )
    return out


def runner_cmd(job_path) -> list[str]:
    # Prefer an existing engine Python so deployments do not depend on `uv`
    # being present in PATH. Fall back to uv when no reusable venv exists.
    python = _engine_python()
    if python:
        return [python, str(SERVE_ONE), "--job", str(job_path)]
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--project", str(INFERENCE_DIR),
                "python", str(SERVE_ONE), "--job", str(job_path)]
    raise RuntimeError(
        "No engine runtime found. Set PPTAGENT_ENGINE_PYTHON to a Python with "
        "inference dependencies, or install uv."
    )
