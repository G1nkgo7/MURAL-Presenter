"""装配当前 Skill 快照、生产依赖、角色基础工具和受控脚本。

本模块是 Harness 的机械宿主层：它不写页面 prompt，也不做叙事或审美决定。所有创作方法来自
工作区固定引用的 Skill object；这里仅提供文件、网络、图片、Renderer、资源槽和
环境预检等可复现能力。
"""
from __future__ import annotations

import argparse

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
import base64
import copy
import errno
import fcntl
import hashlib
import importlib
import io
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Iterator, Mapping, Sequence
from urllib.parse import urlsplit
import uuid

from state.artifacts import ArtifactStateError, ArtifactStore, RenderBackend, UncommittedPageSourceError, _process_start_id
from state.locking import page_render_lock
from state.workspace import PageRepairError, WorkspaceState, WorkspaceStateError, file_sha256
from owner.registry import OwnerRegistry, OwnerRegistryError

from .attachments import attachment_content_view, attachment_excerpt_media, materialize_page_image
from .config import RunSpec, load_runtime_config, model_effort_capability, validate_reasoning_effort
from .diagnostics import CommandFailed, CommandResult, command_identity, run_command
from .recovery import ResourceSlotTimeout
from .shared_fonts import SharedFontError, SharedFontStore
from .shared_objects import SharedSkillStore, load_skill_module, workspace_skill_module
from .tools import (
    ToolContext,
    ToolContractError,
    ToolExecutionError,
    ToolHandler,
    WriteScope,
    normalize_workspace_path,
)
from .workspace_paths import WorkspacePaths
from .vision import MAX_CONTEXT_IMAGES


PROVENANCE_SCHEMA = "mural.skill-provenance.v1"
DOCTOR_SCHEMA = "mural.environment-doctor.v1"
ROLE_SCRIPT_ENTRIES = {
    "orchestrator": "orchestrator.py",
    "image": "image.py",
    "slide": "slide.py",
}
IMAGE_HISTORY_STDOUT_CHARS = 2400
IMAGE_HISTORY_STDERR_CHARS = 1600
IMAGE_HISTORY_DIAGNOSTIC_RECORDS = 24
READ_FILE_HISTORY_BYTES = 48 * 1024
ATTACHMENT_CONTENT_HISTORY_BYTES = 32 * 1024
ATTACHMENT_READ_MAX_IMAGES = 4
WEB_EXTRACT_HISTORY_CHARS = 12000
WEB_SCREENSHOT_MAX_BYTES = 16 * 1024 * 1024
RESEARCH_WEB_SOFT_CALLS = 10
RESEARCH_WEB_MAX_CALLS = 16
RESEARCH_SUCCESSFUL_EXTRACT_TARGET = 3
RESEARCH_NO_NEW_SOURCE_LIMIT = 2


def role_script_entries(role: str) -> dict[str, str]:
    roles = ("orchestrator", "image") if role == "orchestrator" else (role,)
    return {name: ROLE_SCRIPT_ENTRIES[name] for name in roles if name in ROLE_SCRIPT_ENTRIES}


def _history_excerpt(text: object, limit: int) -> str:
    """给模型历史保留可读摘要；完整输出只由 Harness 留档。"""
    value = str(text or "")
    if len(value) <= limit:
        return value
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    omitted = len(value) - head - tail
    return value[:head] + f"\n...[omitted {omitted} chars; full output retained by Harness]...\n" + value[-tail:]


def _compact_image_command_diagnostics(value: object) -> object:
    """限制 Image 命令回包体积；诊断文件位置不进入 Agent 历史。"""
    if not isinstance(value, Mapping):
        return copy.deepcopy(value)
    diagnostics = copy.deepcopy(dict(value))
    diagnostics.pop("ref", None)
    diagnostics.pop("diagnostic_ref", None)
    records = diagnostics.get("records")
    if isinstance(records, list) and len(records) > IMAGE_HISTORY_DIAGNOSTIC_RECORDS:
        keep_head = IMAGE_HISTORY_DIAGNOSTIC_RECORDS // 2
        keep_tail = IMAGE_HISTORY_DIAGNOSTIC_RECORDS - keep_head
        diagnostics["records"] = [*records[:keep_head], *records[-keep_tail:]]
        diagnostics["history_records_omitted"] = len(records) - IMAGE_HISTORY_DIAGNOSTIC_RECORDS
    diagnostics["history_compacted"] = True
    return diagnostics


def _compact_image_terminal_payload(payload: Mapping[str, object], *, images: Sequence[object] = ()) -> dict[str, object]:
    """Image Owner 的模型历史只需要摘要和结构化素材回执。"""
    result = copy.deepcopy(dict(payload))
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    result["stdout"] = _history_excerpt(stdout, IMAGE_HISTORY_STDOUT_CHARS)
    result["stderr"] = _history_excerpt(stderr, IMAGE_HISTORY_STDERR_CHARS)
    if len(stdout) > IMAGE_HISTORY_STDOUT_CHARS:
        result["stdout_omitted_chars"] = len(stdout) - len(result["stdout"])
    if len(stderr) > IMAGE_HISTORY_STDERR_CHARS:
        result["stderr_omitted_chars"] = len(stderr) - len(result["stderr"])
    result["diagnostics"] = _compact_image_command_diagnostics(result.get("diagnostics") or {})
    if images and "images" not in result:
        result["images"] = copy.deepcopy(list(images))
    result["history_compacted"] = True
    return result


def _web_extract_prompt_payload(payload: Mapping[str, object], *, source_ref: str) -> dict[str, object]:
    """Keep source evidence useful while hiding private cache implementation."""
    visible_keys = (
        "url", "effective_url", "fetch_method", "format", "title", "page_count", "textless_pages",
        "extraction_scope", "extraction_notice", "content", "truncated", "cached",
        "inline_binary_omitted", "prompt_window_clamped", "requested_max_chars",
    )
    result = {
        key: copy.deepcopy(payload[key])
        for key in visible_keys
        if key in payload
    }
    result["source_ref"] = source_ref
    result["continuation"] = (
        "Use read_file with this source_ref and a narrow query or offset/limit "
        "to read additional cached source text."
    )
    return result


def _web_extract_download_error(returncode: int, stderr: bytes) -> tuple[str, str]:
    """Classify target HTTP rejection without echoing returned URL parameters."""
    raw = stderr.decode("utf-8", errors="replace")
    status = re.search(r"(?:error:\s*|HTTP\s*)([45]\d{2})", raw, re.IGNORECASE)
    curl_codes = {
        5: "web_extract_network_failed",
        6: "web_extract_dns_failed",
        7: "web_extract_network_failed",
        18: "web_extract_incomplete",
        28: "web_extract_timeout",
        35: "web_extract_ssl_failed",
        47: "web_extract_redirect_failed",
        51: "web_extract_ssl_failed",
        52: "web_extract_network_failed",
        56: "web_extract_network_failed",
        58: "web_extract_ssl_failed",
        60: "web_extract_ssl_failed",
    }
    code = (
        f"web_extract_http_{status.group(1)}"
        if status
        else curl_codes.get(int(returncode), "web_extract_failed")
    )
    lines = []
    for line in raw.splitlines():
        cleaned = re.sub(r"https?://\S+", "<url omitted>", line).strip()
        if cleaned:
            lines.append(cleaned)
    detail = "\n".join(lines)[:500] or "web source download failed"
    if status and status.group(1) in {"401", "403", "404", "405", "410", "451"}:
        detail += " This response is deterministic for the current source; do not retry the same URL unchanged. Use another public source."
    elif status and status.group(1) == "429":
        detail += " The source is rate limited; retry at most once after a delay, then use another public source."
    return code, detail


def _web_extract_error_receipt(
    code: str,
    detail: str,
    *,
    cached_failure: bool = False,
) -> dict[str, object]:
    """Return a small actionable failure contract instead of raw command logs."""
    match = re.fullmatch(r"web_extract_http_(\d{3})", code)
    status_code = int(match.group(1)) if match else None
    retry_once = (
        code in {
            "web_extract_timeout", "web_extract_network_failed", "web_extract_dns_failed",
            "web_extract_incomplete", "web_extract_browser_failed",
            "web_extract_dependency_failed",
        }
        or status_code in {408, 425, 429}
        or status_code is not None and status_code >= 500
    )
    if status_code in {401, 403, 451}:
        status = "access_denied"
    elif status_code in {404, 410}:
        status = "not_found"
    elif status_code == 429:
        status = "rate_limited"
    elif code == "web_extract_timeout":
        status = "timeout"
    elif code in {"web_extract_ssl_failed", "web_extract_redirect_failed"}:
        status = "source_connection_rejected"
    elif code in {"web_extract_empty", "web_extract_pdf", "web_extract_too_large"}:
        status = "source_unusable"
    else:
        status = "network_error"
    recommended_action = (
        "retry once, then switch to another accessible source"
        if retry_once
        else "use another accessible source or a reliable search-result summary"
    )
    return {
        "status": status,
        "error_code": code,
        "retry_recommended": "once" if retry_once else False,
        "recommended_action": recommended_action,
        "cached_failure": bool(cached_failure),
        "detail": str(detail)[:700],
    }


class ProductionError(RuntimeError):
    """表示生产宿主无法满足已声明的机械能力。"""


@dataclass(frozen=True)
class ProductionSettings:
    """保存与 Provider 格式无关的生产运行参数。"""

    skill_source: Path
    max_turns: int = 240
    owner_max_turns: int | None = None
    max_tokens: int = 40960
    owner_max_tokens: int = 32768
    orchestrator_max_tokens: int = 16384
    slide_owner_max_tokens: int = 20000
    research_owner_max_tokens: int = 12000
    image_owner_max_tokens: int = 8192
    critic_max_tokens: int = 8192
    # 合成样本输入与答案的总预算；独立于 Provider 窗口，训练前仍按目标模型实际编码验证。
    training_sample_max_tokens: int = 131072
    compact_trigger_ratio: float = 0.90
    compact_target_tokens: int = 131072
    compact_summary_target_tokens: int = 8192
    thinking: bool = True
    reasoning_effort: str = "medium"
    command_timeout_s: int = 1800
    model_concurrency: int = 1
    web_search_concurrency: int = 2
    image_generation_concurrency: int = 2
    render_concurrency: int = 1
    web_extract_timeout_s: int = 30
    web_extract_max_bytes: int = 4 * 1024 * 1024
    image_generation_timeout_s: int = 300
    image_generation_download_timeout_s: int = 120
    image_generation_max_failure_streak: int = 2
    vision_context_images: int = 4
    # 唯一布尔开关：是否启用隔离外部视觉 Critic（并行第二视角）。
    # 推理与合成都只在显式启用时调用，默认关闭。
    enable_external_visual_critic: bool = False
    critic_concurrency: int = 1
    critic_timeout_s: int = 120

    def __post_init__(self) -> None:
        """规范 Skill 路径并拒绝无效预算。"""
        source = self.skill_source.expanduser().resolve()
        positive = {
            "max_turns": self.max_turns,
            "max_tokens": self.max_tokens,
            "owner_max_tokens": self.owner_max_tokens,
            "orchestrator_max_tokens": self.orchestrator_max_tokens,
            "slide_owner_max_tokens": self.slide_owner_max_tokens,
            "research_owner_max_tokens": self.research_owner_max_tokens,
            "image_owner_max_tokens": self.image_owner_max_tokens,
            "critic_max_tokens": self.critic_max_tokens,
            "training_sample_max_tokens": self.training_sample_max_tokens,
            "compact_target_tokens": self.compact_target_tokens,
            "compact_summary_target_tokens": self.compact_summary_target_tokens,
            "command_timeout_s": self.command_timeout_s,
            "model_concurrency": self.model_concurrency,
            "web_search_concurrency": self.web_search_concurrency,
            "image_generation_concurrency": self.image_generation_concurrency,
            "render_concurrency": self.render_concurrency,
            "web_extract_timeout_s": self.web_extract_timeout_s,
            "web_extract_max_bytes": self.web_extract_max_bytes,
            "image_generation_timeout_s": self.image_generation_timeout_s,
            "image_generation_download_timeout_s": (
                self.image_generation_download_timeout_s
            ),
            "image_generation_max_failure_streak": (
                self.image_generation_max_failure_streak
            ),
        }
        invalid = [name for name, value in positive.items() if int(value) <= 0]
        if self.owner_max_turns is not None and int(self.owner_max_turns) <= 0:
            invalid.append("owner_max_turns")
        if invalid:
            raise ValueError(f"生产参数必须大于零：{invalid}")
        if not math.isfinite(self.compact_trigger_ratio) or not 0 < self.compact_trigger_ratio < 1:
            raise ValueError("COMPACT_TRIGGER_RATIO must be finite and between 0 and 1")
        if self.compact_summary_target_tokens >= self.compact_target_tokens:
            raise ValueError("COMPACT_SUMMARY_TARGET_TOKENS must be smaller than COMPACT_TARGET_TOKENS")
        if not 0 < self.vision_context_images <= MAX_CONTEXT_IMAGES:
            raise ValueError(
                f"vision_context_images must be between 1 and {MAX_CONTEXT_IMAGES}"
            )
        if int(self.critic_concurrency) <= 0 or int(self.critic_timeout_s) <= 0:
            raise ValueError("critic concurrency and timeout must be positive")
        object.__setattr__(self, "skill_source", source)

    def orchestrator_request_max_tokens(self) -> int:
        """Bound routine orchestration turns without changing the global hard ceiling."""
        return min(int(self.max_tokens), int(self.orchestrator_max_tokens))

    def owner_request_max_tokens(self, role: str) -> int:
        """Role-specific output windows; Slide keeps enough room for full-page HTML."""
        role_limits = {
            "slide": self.slide_owner_max_tokens,
            "research": self.research_owner_max_tokens,
            "image": self.image_owner_max_tokens,
        }
        return min(int(self.owner_max_tokens), int(role_limits.get(str(role).lower(), self.owner_max_tokens)))

    def critic_request_max_tokens(self) -> int:
        """External critic should be concise; full runs keep the raw trace on disk."""
        return min(int(self.max_tokens), int(self.critic_max_tokens))

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "ProductionSettings":
        """从中立环境变量读取生产参数，不接受 ``CLEAN_*`` 别名。"""
        env = environment if environment is not None else os.environ
        resources = load_runtime_config(env)["resources"]
        repository = Path(__file__).resolve().parents[3]
        skill_root = Path(str(env.get("MURAL_SKILL_DIR") or repository / "skills/mural-presenter-v1"))
        model = str(env.get("MODEL") or "")
        capability = model_effort_capability(model)
        default_effort = capability["default"] or "medium"
        effort = validate_reasoning_effort(model,
            env.get("MODEL_REASONING_EFFORT") or capability["default"] or default_effort,
            thinking=_boolean(env, "MODEL_THINKING", True))
        max_tokens = _positive_int(env, "AGENT_MAX_TOKENS", 40960)
        owner_max_tokens = _positive_int(env, "OWNER_MAX_TOKENS", 32768)
        return cls(
            skill_source=skill_root,
            max_turns=_positive_int(env, "AGENT_MAX_TURNS", 240),
            owner_max_turns=_positive_int(env, "OWNER_MAX_TURNS", _positive_int(env, "AGENT_MAX_TURNS", 240)),
            max_tokens=max_tokens,
            owner_max_tokens=owner_max_tokens,
            orchestrator_max_tokens=_positive_int(env, "ORCHESTRATOR_MAX_TOKENS", min(max_tokens, 16384)),
            slide_owner_max_tokens=_positive_int(env, "SLIDE_OWNER_MAX_TOKENS", min(owner_max_tokens, 20000)),
            research_owner_max_tokens=_positive_int(env, "RESEARCH_OWNER_MAX_TOKENS", min(owner_max_tokens, 12000)),
            image_owner_max_tokens=_positive_int(env, "IMAGE_OWNER_MAX_TOKENS", min(owner_max_tokens, 8192)),
            critic_max_tokens=_positive_int(env, "CRITIC_MAX_TOKENS", min(max_tokens, 8192)),
            training_sample_max_tokens=_positive_int(env, "TRAINING_SAMPLE_MAX_TOKENS", 131072),
            compact_trigger_ratio=float(env.get("COMPACT_TRIGGER_RATIO", "0.90")),
            compact_target_tokens=_positive_int(env, "COMPACT_TARGET_TOKENS", 131072),
            compact_summary_target_tokens=_positive_int(env, "COMPACT_SUMMARY_TARGET_TOKENS", 8192),
            thinking=_boolean(env, "MODEL_THINKING", True),
            reasoning_effort=effort,
            command_timeout_s=_positive_int(env, "SKILL_COMMAND_TIMEOUT_S", 1800),
            model_concurrency=resources["provider"],
            web_search_concurrency=resources["web_search"],
            image_generation_concurrency=resources["image_generation"],
            render_concurrency=resources["render"],
            web_extract_timeout_s=_positive_int(env, "WEB_EXTRACT_TIMEOUT_S", 30),
            web_extract_max_bytes=_positive_int(
                env, "WEB_EXTRACT_MAX_BYTES", 4 * 1024 * 1024
            ),
            image_generation_timeout_s=_positive_int(
                env, "IMAGE_GENERATION_TIMEOUT_S", 300
            ),
            image_generation_download_timeout_s=_positive_int(
                env, "IMAGE_GENERATION_DOWNLOAD_TIMEOUT_S", 120
            ),
            image_generation_max_failure_streak=_positive_int(
                env, "IMAGE_GENERATION_MAX_FAILURE_STREAK", 2
            ),
            vision_context_images=_positive_int(env, "VISION_CONTEXT_IMAGES", 4),
            enable_external_visual_critic=_boolean(
                env, "ENABLE_EXTERNAL_VISUAL_CRITIC", False
            ),
            critic_concurrency=_positive_int(env, "CRITIC_CONCURRENCY", 1),
            critic_timeout_s=_positive_int(env, "CRITIC_TIMEOUT_S", 120),
        )

    def effective_external_visual_critic(self, run_mode: object) -> bool:
        """所有运行模式使用同一个显式开关；保留参数以兼容现有调用。"""
        return bool(self.enable_external_visual_critic)


@dataclass(frozen=True)
class SkillSnapshot:
    """表示一个内容寻址、不可变的共享 Skill object 的固定引用。"""

    source: Path
    target: Path
    name: str
    tree_sha256: str
    main_sha256: str
    role_sha256: Mapping[str, str]
    paths: WorkspacePaths
    release: str = ""
    font_store: SharedFontStore | None = None
    preparation_timings_ms: Mapping[str, int] = field(default_factory=dict)

    @classmethod
    def stage(
        cls,
        workspace: str | Path,
        source: str | Path,
        environment: Mapping[str, str] | None = None,
    ) -> "SkillSnapshot":
        """固定共享 Skill object 并记录精确引用；不在工作区内创建 skills/。"""
        started = time.monotonic()
        root = Path(workspace).expanduser().resolve()
        source_path = Path(source).expanduser().resolve()
        paths = WorkspaceState(root).paths
        env = dict(environment or os.environ)
        expected_tree = None
        release_path = paths.state / "release.json"
        if not paths.skill_ref.is_file() and release_path.is_file():
            approved = json.loads(release_path.read_text(encoding="utf-8"))
            if approved.get("policy") in {"prod", "production"}:
                expected_tree = str((approved.get("actual") or {}).get("skill") or "")
        reference = SharedSkillStore.for_workspace(root, env).pin(
            paths,
            source_path,
            skill_name=source_path.name,
            environment=env,
            expected_tree_sha256=expected_tree,
        )
        target = reference.object_root
        tree_hash = reference.tree_sha256
        release = reference.release
        if not (target / "SKILL.md").is_file():
            raise ProductionError(f"固定 Skill object 不完整：{target}")
        main_hash = file_sha256(target / "SKILL.md")
        role_hashes = {
            role: file_sha256(target / "roles" / f"{role}.md")
            for role in ("research", "image", "slide")
        }
        if not all(role_hashes.values()):
            raise ProductionError("工作区 Skill 缺少一个或多个生产角色卡")
        skill_ms = round((time.monotonic() - started) * 1000)
        font_started = time.monotonic()
        font_store = SharedFontStore(root, env)
        font_store.pin_workspace(
            paths,
            skill_root=target,
            skill_tree_sha256=tree_hash,
        )
        snapshot = cls(
            source=source_path,
            target=target,
            name=reference.skill_name,
            tree_sha256=tree_hash,
            main_sha256=main_hash,
            role_sha256=role_hashes,
            paths=paths,
            release=release,
            font_store=font_store,
            preparation_timings_ms={"skill_object": skill_ms,
                                    "font_pin": round((time.monotonic() - font_started) * 1000)},
        )
        trace = paths.trace / "skill-provenance.json"
        value = {
            "schema": PROVENANCE_SCHEMA,
            "skill_name": snapshot.name,
            "workspace_path": f"skills/{snapshot.name}",
            "layout_version": paths.layout_version,
            "release": release,
            "tree_sha256": snapshot.tree_sha256,
            "main_sha256": snapshot.main_sha256,
            "role_sha256": dict(snapshot.role_sha256),
            "runtime_prompt_suffix": snapshot.prompt_suffix,
            "source_path": str(source_path),
        }
        if trace.is_file():
            existing = json.loads(trace.read_text(encoding="utf-8"))
            if not isinstance(existing, dict) or existing.get("tree_sha256") != snapshot.tree_sha256:
                raise ProductionError("工作区 Skill provenance 与当前快照不一致")
        else:
            _atomic_json_write(trace, value)
        snapshot.prepare_workspace(root)
        return snapshot

    def prepare_workspace(self, workspace: str | Path) -> None:
        """创建 canonical v2 目录和基础文件；绝不创建 legacy 目录。"""
        self.paths.initialize_directories()
        base = self.paths.path("base_css")
        if not base.exists():
            shutil.copy2(self.target / "assets" / "base.css", base)

    def resolve_skill_path(self, logical: str) -> Path:
        """把逻辑 `skills/<name>/...` 直接解析到固定 object。"""
        prefix = f"skills/{self.name}/"
        normalized = str(logical).replace("\\", "/")
        if not normalized.startswith(prefix):
            raise ProductionError(f"不是 {self.name} 的逻辑路径：{logical}")
        relative = Path(normalized.removeprefix(prefix))
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ProductionError(f"Skill 逻辑路径非法：{logical}")
        path = (self.target / relative).resolve()
        try:
            path.relative_to(self.target.resolve())
        except ValueError as exc:
            raise ProductionError(f"Skill 逻辑路径逃逸 object：{logical}") from exc
        return path

    def capture_runtime_fonts(self) -> None:
        """把真实 subset 回执追加到字体固定清单。"""
        if self.font_store is not None:
            try:
                self.font_store.capture_runtime_bundle(self.paths)
            except SharedFontError as exc:
                raise ToolExecutionError("font_integrity_failed", (
                    f"{exc}. Fixed font validation failed; pause this workspace and resume after "
                    "the pinned font environment is repaired. Changing page content, switching render "
                    "commands or regenerating images cannot repair this integrity failure."
                )) from exc

    def main_text(self) -> str:
        """完整源文加可区分的运行时路径提示；源文 hash 不包含该提示。"""
        return (self.target / "SKILL.md").read_text(encoding="utf-8") + self.prompt_suffix

    @property
    def prompt_suffix(self) -> str:
        root = f"skills/{self.name}/"
        return (
            f"\n\n[Harness: selected Skill logical root = {root}]\n"
            "read_file.path 使用工作区相对路径，例如 deck.md。Skill 的 Markdown 链接则相对链接所在文档目录，"
            "不是工作区目录；主文件是 SKILL.md，角色卡是 roles/<role>.md。调用前将链接解析为当前 Skill 的完整逻辑路径，"
            "在该 Skill 内消去 . / .. 并去掉 #章节锚点。例如：\n"
            f"- SKILL.md 中 references/planning/plan-contract.md → {root}references/planning/plan-contract.md\n"
            f"- roles/slide.md 中 ../references/page-index.md → {root}references/page-index.md\n"
            f"- references/style-index.md 中 style/editorial.md#style-03 → {root}references/style/editorial.md\n"
            "使用上述固定 Skill 逻辑路径，不用源码或 object 的绝对路径；路径仍须满足当前角色的读域。\n"
        )

    def role_text(self, role: str) -> str:
        """读取固定 object 中的一个正式生产角色卡。"""
        normalized = str(role).strip().lower()
        if normalized not in self.role_sha256:
            raise ValueError(f"未知生产角色：{normalized}")
        return (self.target / "roles" / f"{normalized}.md").read_text(
            encoding="utf-8"
        ) + self.prompt_suffix


class ResourceSlots:
    """同一全局资源池使用原子链接占位；不假设共享盘 flock 能跨主机互斥。"""

    def __init__(
        self,
        workspace: str | Path,
        settings: ProductionSettings,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        """绑定槽目录和每种资源的并发上限。"""
        env = environment if environment is not None else os.environ
        self.environment = dict(env)
        runtime_config = load_runtime_config(env)
        self.root = Path(runtime_config["resources"]["directory"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.host = {"hostname": socket.gethostname(),
                     "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                     "pid_namespace": os.readlink("/proc/self/ns/pid")}
        self.host_key = hashlib.sha256(json.dumps(self.host, sort_keys=True).encode()).hexdigest()[:20]
        self.limits = {
            "deck": int(runtime_config["decks"]["total"]),
            "model": settings.model_concurrency,
            "web_search": settings.web_search_concurrency,
            "image_generation": settings.image_generation_concurrency,
            "render": settings.render_concurrency,
            "critic": settings.critic_concurrency,
        }
        hot_path = str(env.get("MURAL_RESOURCE_LIMITS_FILE") or "").strip()
        self.hot_limits_path = Path(hot_path).expanduser().resolve() if hot_path else None
        self._hot_limits_fingerprint = None
        self._hot_limits_lock = threading.Lock()
        self._refresh_limits()

    def _refresh_limits(self) -> None:
        if self.hot_limits_path is None:
            return
        with self._hot_limits_lock:
            try:
                info = self.hot_limits_path.stat()
                fingerprint = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)
                if fingerprint == self._hot_limits_fingerprint:
                    return
                config = load_runtime_config({"MURAL_RUNTIME_CONFIG": str(self.hot_limits_path)})
                resources = config["resources"]
                if Path(resources["directory"]) != self.root:
                    raise ValueError("hot resource updates cannot change the shared pool directory")
                limits = {"deck": int(config["decks"]["total"]),
                          "model": int(resources["provider"]),
                          "render": int(resources["render"]),
                          "web_search": int(resources["web_search"]),
                          "image_generation": int(resources["image_generation"])}
            except (OSError, ValueError) as exc:
                raise ProductionError(f"Invalid operator hot resource configuration: {exc}") from exc
            self.limits = {**self.limits, **limits}
            self._hot_limits_fingerprint = fingerprint

    @contextmanager
    def acquire(
        self, resource: str, *, deadline_monotonic: float | None = None, model: str = "",
        stop_event: threading.Event | None = None,
    ) -> Iterator[float]:
        """保留 FIFO、取消与截止时间；无法确认的远端持有者不按 TTL 抢占。"""
        name = str(resource).strip().lower()
        if name == "critic":
            name = "model"
        limit = int(self.limits.get(name) or 0)
        if limit < 1:
            raise ValueError(f"未知资源槽：{name}")
        endpoint_key = {"web_search": "SERPER_BASE_URL",
                        "image_generation": "IMAGE_BASE_URL"}.get(name)
        identity = str(self.environment.get(endpoint_key, "") or "").rstrip("/") if endpoint_key else name
        pool = hashlib.sha256(identity.encode()).hexdigest()[:16]
        started = time.monotonic()
        queue = self.root / f"{name}-{pool}.queue"
        queue.mkdir(exist_ok=True)
        ticket = None
        predecessor = None
        handle = None
        claim = self.root / f".claim-{uuid.uuid4().hex}"
        record = {"host": self.host, "pid": os.getpid(), "start": _process_start_id(os.getpid()),
                  "token": claim.name}
        with claim.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            with self._queue_guard(queue, deadline_monotonic, stop_event, claim):
                waiting = self._ticket_order(queue)
                if not waiting:
                    self._refresh_limits()
                    for candidate in self._slot_candidates(name, pool, int(self.limits[name])):
                        self._check_wait(deadline_monotonic, stop_event)
                        if self._link_claim(candidate, claim):
                            handle = candidate
                            break
                if handle is None:
                    number = int(waiting[-1].stem) + 1 if waiting else 1
                    predecessor = waiting[-1] if waiting else None
                    ticket = queue / f"{number:020d}.ticket"
                    os.link(claim, ticket)
            retry_delay = .002
            next_dead_check = time.monotonic() + .25
            while handle is None:
                self._check_wait(deadline_monotonic, stop_event)
                now = time.monotonic()
                should_check_head = predecessor is None or not predecessor.exists()
                if not should_check_head and now >= next_dead_check:
                    should_check_head = self._local_claim_is_dead(predecessor)
                    next_dead_check = now + .25
                if should_check_head:
                    with self._queue_guard(queue, deadline_monotonic, stop_event, claim):
                        waiting = self._ticket_order(queue)
                        if waiting and waiting[0] == ticket:
                            predecessor = None
                        else:
                            earlier = [item for item in waiting if item.name < ticket.name]
                            predecessor = earlier[-1] if earlier else None
                        if predecessor is None and waiting and waiting[0] == ticket:
                            self._refresh_limits()
                            limit = int(self.limits[name])
                            for candidate in self._slot_candidates(name, pool, limit):
                                self._check_wait(deadline_monotonic, stop_event)
                                if not self._link_claim(candidate, claim):
                                    continue
                                handle = candidate
                                ticket.unlink()
                                ticket = None
                                break
                if handle is None:
                    self._wait_before_retry(retry_delay, deadline_monotonic, stop_event)
                    retry_delay = min(.1, retry_delay * 1.5)
            yield max(0.0, time.monotonic() - started)
        finally:
            if ticket is not None:
                self._release_claim(ticket, record)
            if handle is not None:
                self._release_claim(handle, record)
            claim.unlink(missing_ok=True)

    def _slot_candidates(self, name: str, pool: str, limit: int) -> list[Path]:
        """Prefer vacant slots; only inspect occupied claims when vacancies fail."""
        occupied = {path.name for path in self.root.glob(f"{name}-{pool}-*.lease")}
        if self.hot_limits_path is not None and len(occupied) >= limit:
            # A lower cap drains existing holders, including indices above the new cap.
            occupied = {filename for filename in occupied if self._live_claim(self.root / filename)}
            if len(occupied) >= limit:
                return []
        candidates = [self.root / f"{name}-{pool}-{index:03d}.lease" for index in range(limit)]
        # Listing is only a hint; _link_claim still provides atomic ownership and safe recovery.
        return ([path for path in candidates if path.name not in occupied]
                + [path for path in candidates if path.name in occupied])

    @staticmethod
    def _check_wait(deadline, stop_event):
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("owner_interrupted: resource queue cancelled")
        if deadline is not None and time.monotonic() >= deadline:
            raise ResourceSlotTimeout("等待资源槽超过截止时间；未抢占无法确认已退出的远端持有者")

    def _wait_before_retry(self, delay, deadline, stop_event):
        self._check_wait(deadline, stop_event)
        if deadline is not None:
            delay = min(delay, max(0.0, deadline - time.monotonic()))
        if stop_event is None:
            time.sleep(delay)
        else:
            stop_event.wait(delay)
        self._check_wait(deadline, stop_event)

    @contextmanager
    def _local_guard(self, deadline=None, stop_event=None):
        # flock 只串行化同一主机/进程命名空间的死亡回收，跨主机互斥由原子 link 保证。
        with (self.root / f".host-{self.host_key}.guard").open("a+") as guard:
            while True:
                self._check_wait(deadline, stop_event)
                try:
                    fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.01)
            try:
                yield
            finally:
                fcntl.flock(guard.fileno(), fcntl.LOCK_UN)

    def _live_claim(self, path):
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            return False
        if not isinstance(record, dict) or not record.get("host") or not record.get("start"):
            raise ProductionError(f"resource claim is invalid; preserve and inspect {path}")
        if record["host"] != self.host:
            return True
        current = _process_start_id(int(record["pid"]))
        if current is None or current == record["start"]:
            return True
        path.unlink(missing_ok=True)
        return False

    def _local_claim_is_dead(self, path):
        """无锁识别确定死亡的本机前驱；远端或无法确认的 claim 始终保留。"""
        try:
            record = json.loads(path.read_text())
        except FileNotFoundError:
            return True
        if not isinstance(record, dict) or not record.get("host") or not record.get("start"):
            raise ProductionError(f"resource claim is invalid; preserve and inspect {path}")
        if record["host"] != self.host:
            return False
        current = _process_start_id(int(record["pid"]))
        return current is not None and current != record["start"]

    def _try_claim(self, path, claim, deadline, stop_event):
        with self._local_guard(deadline, stop_event):
            return self._link_claim(path, claim)

    def _link_claim(self, path, claim):
        # 调用方已持本机回收锁或全局队列锁；每个槽不再重复开关文件锁。
        if self._live_claim(path):
            return False
        try:
            os.link(claim, path)
        except FileExistsError:
            return False
        return True

    @staticmethod
    def _release_claim(path, record):
        try:
            if json.loads(path.read_text()).get("token") == record["token"]:
                path.unlink()
        except FileNotFoundError:
            pass

    @contextmanager
    def _queue_guard(self, queue: Path, deadline: float | None, stop_event, claim) -> Iterator[None]:
        """票号与队首检查只持短锁；等待元数据锁也计入同一排队预算。"""
        guard = queue / "guard.lease"
        retry_delay = .002
        while not self._try_claim(guard, claim, deadline, stop_event):
            self._wait_before_retry(retry_delay, deadline, stop_event)
            retry_delay = min(.05, retry_delay * 1.5)
        try:
            yield
        finally:
            self._release_claim(guard, {"token": claim.name})

    def _ticket_order(self, queue: Path) -> list[Path]:
        """只回收连续死前缀；首个 live ticket 后不再读取队列 JSON。"""
        with self._local_guard():
            tickets = sorted(queue.glob("*.ticket"))
            while tickets and not self._live_claim(tickets[0]):
                tickets.pop(0)
            return tickets


class EnvironmentDoctor:
    """在第一次模型调用前检查 Skill、凭证和本地生产依赖。"""

    def __init__(
        self,
        run_spec: RunSpec,
        snapshot: SkillSnapshot | None,
        environment: Mapping[str, str],
    ) -> None:
        """绑定本次运行事实和已冻结 Skill。"""
        self.run_spec = run_spec
        self.snapshot = snapshot
        self.environment = environment

    def run(self, *, raise_on_error: bool = True, report_path: Path | None = None,
            full: bool = False) -> dict[str, object]:
        """执行非破坏预检、写入 trace，并在必需项失败时阻止模型启动。"""
        started = time.monotonic()
        checked_epoch = time.time()
        checks: list[dict[str, object]] = []

        def add(name: str, ok: bool, detail: str, required: bool = True) -> None:
            """向报告追加一项稳定检查结果。"""
            checks.append(
                {
                    "name": name,
                    "ok": bool(ok),
                    "required": bool(required),
                    "detail": str(detail),
                }
            )

        configured_engine = str(
            self.environment.get("PPTAGENT_ENGINE_PYTHON") or sys.executable
        )
        configured_normalizer = str(
            self.environment.get("NORMALIZE_PY") or configured_engine
        )
        runtime_aligned = _same_interpreter(
            sys.executable, configured_engine
        ) and _same_interpreter(configured_engine, configured_normalizer)
        add(
            "formal_inference_runtime",
            runtime_aligned,
            (
                f"process={sys.executable}; engine={configured_engine}; "
                f"attachments={configured_normalizer}"
            ),
        )
        skill_root = self.snapshot.target if self.snapshot else ProductionSettings.from_environment(self.environment).skill_source
        add("skill_snapshot" if self.snapshot else "skill_source",
            bool(self.snapshot.main_sha256) if self.snapshot else (skill_root / "SKILL.md").is_file(),
            self.snapshot.tree_sha256 if self.snapshot else str(skill_root))
        add(
            "skill_scripts",
            all(
                (skill_root / "scripts" / name).is_file()
                for name in ("orchestrator.py", "image.py", "slide.py")
            ),
            "orchestrator.py / image.py / slide.py",
        )
        key_configured = bool(
            str(self.environment.get("MODEL_API_KEY") or "").strip()
            or str(self.environment.get("MODEL_API_KEYS") or "").strip()
            or str(self.environment.get("MODEL_POOL_ENV_PATH") or "").strip()
            or str(self.environment.get("MODEL_ROUTE_POOL_PATH") or "").strip()
        )
        add("model_credentials", key_configured, "统一 MODEL_* 凭证池")
        playwright_ok = _module_importable("playwright")
        add("playwright", playwright_ok, "Python Playwright", required=True)
        add("pillow", _module_importable("PIL"), "Pillow", required=True)
        suffixes = {path.suffix.lower() for path in self.run_spec.attachments}
        office_needed = bool(suffixes & {".docx", ".pptx", ".xlsx"})
        pdf_needed = ".pdf" in suffixes or office_needed
        # 普通启动只导入本任务需要的能力；显式 Doctor 仍执行全量环境体检。
        for name, modules, detail, needed in (
            ("anthropic", ("anthropic",), "Anthropic Provider SDK", self.run_spec.provider.api_format == "anthropic"),
            ("pymupdf", ("pymupdf", "fitz"), "PDF native text and page rasterization", pdf_needed),
            ("requests", ("requests",), "MinerU service client", pdf_needed),
        ):
            if full or needed:
                add(name, any(_module_importable(module) for module in modules), detail, required=needed)
        live_probes = False
        if full:
            add("libreoffice", bool(shutil.which("libreoffice") or shutil.which("soffice")),
                "按需 Office 原页预览；不是全文解析后端", required=False)
            from . import mineru
            try:
                endpoints = load_runtime_config(self.environment)["mineru"]["endpoints"]
                for index, endpoint in enumerate(endpoints):
                    try:
                        live_probes = True
                        mineru.service_info(endpoint)
                        add("mineru", True, f"endpoint_index={index}; version={mineru.SERVICE_VERSION}", required=pdf_needed)
                        break
                    except mineru.MinerUError as exc:
                        if index == 0 and exc.infrastructure:
                            continue
                        raise
            except mineru.MinerUError as exc:
                add("mineru", False, str(exc), required=pdf_needed)
        if full:
            add("curl", bool(shutil.which("curl")), "有界网页抽取", required=False)
        report = {
            "schema": DOCTOR_SCHEMA,
            "checked": True,
            "checked_epoch": checked_epoch,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "mode": "local_and_services" if live_probes else "local",
            "scope": "full" if full else "task",
            "live_probes": live_probes,
            "status": (
                "pass"
                if not [item for item in checks if item["required"] and not item["ok"]]
                else "blocked"
            ),
            "checks": checks,
            "environment": {
                "python": sys.executable,
                "engine_python": configured_engine,
                "normalizer_python": configured_normalizer,
                "runtime_aligned": runtime_aligned,
            },
        }
        _atomic_json_write(
            report_path if report_path is not None else self.run_spec.output_workspace / "_trace" / "environment-doctor.json",
            report,
        )
        blockers = [
            str(item["name"])
            for item in checks
            if item["required"] and not item["ok"]
        ]
        if blockers and raise_on_error:
            raise ProductionError(f"生产环境预检失败：{blockers}")
        return report


class SkillRenderer:
    """通过冻结 Skill 的 Slide 脚本渲染单页并返回资源回执。"""

    def __init__(
        self,
        workspace: str | Path,
        settings: ProductionSettings,
        environment: Mapping[str, str],
        slots: ResourceSlots,
        snapshot: SkillSnapshot,
    ) -> None:
        """绑定工作区、受控环境、Renderer 槽和固定 Skill object。"""
        self.root = Path(workspace).expanduser().resolve()
        self.settings = settings
        self.environment = dict(environment)
        self.slots = slots
        self.snapshot = snapshot
        self._browser_host = None
        self._browser_lock = threading.Lock()
        self._config_module = None
        self._local = threading.local()
        self._live_trace = None
        self.last_stdout = ""

    def bind_live_trace(self, journal) -> None:
        """Optional live-events sink; missing journal must not affect render."""
        self._live_trace = journal

    def _emit_render_completed(self, page: int, timings: Mapping[str, float], browser_info: Mapping[str, object]) -> None:
        journal = self._live_trace
        if journal is None:
            return
        identity = command_identity()
        try:
            turn = int(identity.get("turn") or 0)
        except (TypeError, ValueError):
            turn = 0
        try:
            journal.append(
                event_type="page_render_completed",
                agent_id=str(identity.get("owner_id") or "renderer"),
                role=str(identity.get("role") or "harness"),
                turn=turn,
                segment_id=str(identity.get("segment_id") or ""),
                payload={
                    "page": page,
                    "slot_wait_s": round(float(timings.get("slot_wait") or 0), 6),
                    "browser_host_ready_s": round(float(timings.get("browser_host_ready") or 0), 6),
                    "skill_command_s": round(float(timings.get("skill_command") or 0), 6),
                    "total_s": round(float(timings.get("total") or 0), 6),
                    "browser_reused": bool(browser_info.get("reused")),
                },
            )
        except Exception:
            return

    @property
    def last_stdout(self) -> str:
        return getattr(self._local, "stdout", "")

    @last_stdout.setter
    def last_stdout(self, value: str) -> None:
        self._local.stdout = value

    @staticmethod
    def cache_receipt_valid(receipt: Mapping[str, object]) -> bool:
        diagnostics = receipt.get("diagnostics", {})
        return bool(receipt.get("input_hashes")) and isinstance(diagnostics, Mapping) and all(
            isinstance(diagnostics.get(key), list)
            for key in ("visible_images", "rendered_text")
        )

    def __call__(
        self, _html_path: Path, _png_path: Path, _canvas: Mapping[str, object]
    ) -> Mapping[str, object]:
        match = re.fullmatch(r"slide_(\d+)\.html", _html_path.name)
        if match is None:
            raise ArtifactStateError(f"无法从页面路径取得页码：{_html_path}")
        with page_render_lock(self.root, int(match.group(1))):
            return self._render_locked(_html_path, _png_path, _canvas)

    def _render_locked(
        self, _html_path: Path, _png_path: Path, _canvas: Mapping[str, object]
    ) -> Mapping[str, object]:
        """从 canonical HTML 路径取得页码，并调用一次正式 render。"""
        match = re.fullmatch(r"slide_(\d+)\.html", _html_path.name)
        if match is None:
            raise ArtifactStateError(f"无法从页面路径取得页码：{_html_path}")
        page = int(match.group(1))
        command = [
            sys.executable,
            str(self.snapshot.resolve_skill_path(f"skills/{self.snapshot.name}/scripts/slide.py")),
            "render",
            str(self.root),
            "--page",
            str(page),
        ]
        before_sha256 = file_sha256(_html_path)
        started = time.monotonic()
        deadline = time.monotonic() + self.settings.command_timeout_s
        timings = {}
        browser_info = {}
        with self.slots.acquire("render", deadline_monotonic=deadline):
            timings["slot_wait"] = time.monotonic() - started
            ready_started = time.monotonic()
            environment = self._render_environment(page=page)
            lock_fd = page_render_lock(self.root, page).handle.fileno()
            environment["MURAL_PAGE_RENDER_LOCK_FD"] = str(lock_fd)
            # 固定旧 Skill 未声明浏览器宿主能力时仍运行其自身 CLI，不借用当前可变源码。
            if (self.snapshot.target / "scripts/_internal/browser_host.py").is_file():
                with self._browser_lock:
                    if self._browser_host is None:
                        module = load_skill_module(self.snapshot.target, self.snapshot.tree_sha256, "browser_host")
                        self._browser_host = module.BrowserHost(self.root, environment)
                browser_info = self._browser_host.acquire(deadline)
                environment["MURAL_RENDER_BROWSER_ENDPOINT"] = browser_info["endpoint"]
            timings["browser_host_ready"] = time.monotonic() - ready_started
            command_started = time.monotonic()
            remaining = deadline - command_started
            if remaining <= 0:
                raise ToolExecutionError("render_timeout", "render deadline exceeded before Skill command")
            completed = _run_command(
                command,
                cwd=self.root,
                timeout_s=remaining,
                environment=environment,
                pass_fds=(lock_fd,),
            )
            timings["skill_command"] = time.monotonic() - command_started
        self.last_stdout = completed.stdout
        receipt_started = time.monotonic()
        self.snapshot.capture_runtime_fonts()
        timings["font_receipt"] = time.monotonic() - receipt_started
        decode_started = time.monotonic()
        after_sha256 = file_sha256(_html_path)
        geometry_receipt = json.loads(
            (self.root / "_trace" / "render-cache" / f"page_{page:02d}.json").read_text(encoding="utf-8"))
        # 单页 CLI 把当前 fragment 隔离成一个预览文档，回执内页号固定为 1。
        rendered_pages = geometry_receipt.get("pages", [])
        if len(rendered_pages) != 1 or rendered_pages[0].get("page") != 1:
            raise ArtifactStateError(f"page {page} has no unambiguous single-page render evidence")
        evidence = rendered_pages[0]
        if self._config_module is None:
            self._config_module = workspace_skill_module(self.root, "deck_config")
        presentation_language = self._config_module.read_config(
            self.root, require_complete=True)["presentation_language"]
        if evidence.get("presentation_language") != presentation_language:
            raise ArtifactStateError(f"page {page} render presentation language is missing or changed after capture")
        input_hashes = evidence.get("input_hashes")
        if (not isinstance(input_hashes, dict)
                or input_hashes.get(f"slides/slide_{page:02d}.html") != after_sha256
                or not input_hashes.get("base.css")
                or not evidence.get("png_sha256")
                or evidence.get("png_sha256") != file_sha256(_png_path)):
            raise ArtifactStateError(f"page {page} render source/pixel evidence is missing or changed after capture")
        page_geometry = rendered_pages[0].get("geometry", {})
        timings["receipt_decode"] = time.monotonic() - decode_started
        timings["total"] = time.monotonic() - started
        self._emit_render_completed(page, timings, browser_info)
        receipt: dict[str, object] = {
            "geometry": geometry_receipt["canvas"],
            "loaded_resources": rendered_pages[0]["loaded_resources"],
            "failed_resources": rendered_pages[0]["failed_resources"],
            "input_hashes": input_hashes,
            "png_sha256": evidence["png_sha256"],
            "presentation_config": {"presentation_language": evidence["presentation_language"]},
            "diagnostics": {"stdout": completed.stdout, "stderr": completed.stderr,
                            **completed.diagnostics,
                            "timings_s": {key: round(value, 6) for key, value in timings.items()},
                            "renderer_timings_s": geometry_receipt.get("timings_s", {}),
                            "browser_reused": bool(browser_info.get("reused")),
                            "browser_pid": browser_info.get("browser_pid"),
                            **({"visible_images": [*page_geometry["images"],
                                *(row for row in page_geometry.get("media", []) if row.get("background"))]}
                               if "images" in page_geometry else {}),
                            **({"rendered_text": [row["text"] for row in page_geometry["text_boxes"]]}
                               if "text_boxes" in page_geometry else {})},
        }
        if before_sha256 != after_sha256:
            receipt["canonical_html_normalization"] = {
                "producer": self.snapshot.name,
                "before_sha256": before_sha256,
                "after_sha256": after_sha256,
            }
        return receipt

    def _render_environment(self, *, page: int | None = None) -> dict[str, str]:
        """把 Renderer 锁和 broker 路径约束在当前工作区。"""
        environment = _render_environment(self.root, self.settings, self.environment)
        environment.pop("MURAL_RENDER_BROWSER_ENDPOINT", None)
        identity = command_identity()
        environment.pop("MURAL_RENDER_ATTEMPT_ID", None)
        if identity.get("role") and identity.get("owner_id"):
            prefix = identity["role"]
            if (prefix == "orchestrator" and page is not None
                    and WorkspaceState(self.root).is_page_author(page, str(identity["owner_id"]))):
                prefix = "author-orchestrator"
            environment["MURAL_RENDER_ATTEMPT_ID"] = (
                f"{prefix}-{identity['owner_id']}-attempt-{int(identity.get('attempt') or 1)}")
        return environment

    def close(self) -> None:
        with self._browser_lock:
            if self._browser_host is not None:
                self._browser_host.close()
                self._browser_host = None


class ProductionToolServices:
    """实现角色受限的文件、网络、图片和 Skill 脚本工具。"""

    def __init__(
        self,
        run_spec: RunSpec,
        snapshot: SkillSnapshot,
        settings: ProductionSettings,
        environment: Mapping[str, str],
        workspace: WorkspaceState,
        artifacts: ArtifactStore,
        renderer: SkillRenderer,
        slots: ResourceSlots,
    ) -> None:
        """绑定工具实现需要的全部机械状态。"""
        self.run_spec = run_spec
        self.snapshot = snapshot
        self.settings = settings
        self.environment = dict(environment)
        self.workspace = workspace
        self.artifacts = artifacts
        self.renderer = renderer
        self.slots = slots
        self.owners = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id)
        self._search_lock = threading.Lock()
        self._web_results: dict[str, dict[str, object]] = {}
        self._web_extract_failures: dict[str, dict[str, object]] = {}
        self._web_extract_host_denials: dict[str, set[str]] = {}
        self._research_web_progress: dict[str, dict[str, object]] = {}
        self._image_failures: dict[str, int] = {}
        self.image_handoff_sink: Callable[[Mapping[str, object]], None] | None = None

    def prepare_slide_submission(self, tasks) -> dict[str, object]:
        """在任务接受前补齐本批缺失页；只使用固定 Skill，不登记页面 Owner。"""
        pages = sorted({page for task in tasks if task["role"] == "slide" for page in task["pages"]})
        if not pages:
            return {"created_pages": []}
        with self.workspace._lock:
            paths = {page: self.workspace.paths.resolve(f"slides/slide_{page:02d}.html") for page in pages}
            for page, path in paths.items():
                canonical = self.workspace.paths.page_html(page)
                if canonical.absolute() != path or (path.exists() and not path.is_file()):
                    raise ToolExecutionError("slide_preparation_failed",
                        f"本批新任务尚未接受。页面路径必须是工作区内的普通文件：slides/slide_{page:02d}.html")
            missing = [page for page, path in paths.items() if not path.is_file()]
            records = self.workspace._read_state().get("pages", {})
            unregistered = [page for page in pages if str(page) not in records]
            prepare_pages = sorted(set(missing + unregistered))
            if not prepare_pages:
                return {"created_pages": []}
            # 补骨架前必须完成共享字体预检并写入 authoring @font-face；已有页面也不再拒绝这次写入。
            module = load_skill_module(self.snapshot.target, self.snapshot.tree_sha256, "deck_core")
            try:
                module.scaffold_from_plans(self.workspace.paths.root, pages=prepare_pages,
                                          update_shared_css=True)
            except (ValueError, OSError, RuntimeError) as exc:
                detail = ("本批新任务尚未接受，未登记 Owner 或启动执行。自动补齐骨架失败："
                    f"{exc}。修正当前计划或共享准备问题后，可使用原 task_id 再次提交。")
                raise ToolExecutionError("slide_preparation_failed", detail,
                    diagnostics={"detail": detail, "preparation_error": str(exc)}) from exc
            # 中断可能发生在 HTML 落盘之后、机械状态登记之前；恢复时只补登记，不重写文件。
            for page in prepare_pages:
                self.workspace.register_scaffold(page)
            return {"created_pages": missing, "registered_pages": prepare_pages,
                    "paths": [f"slides/slide_{page:02d}.html" for page in missing]}

    def may_write(self, context: ToolContext, relative: str) -> bool:
        """公开写域由当前归属决定；实际写入持同一锁再检查，不能靠启动快照授权。"""
        if relative == "config.json":
            return context.role == "orchestrator" and context.owner_id == "orchestrator"
        match = re.fullmatch(r"slides/slide_(\d+)\.html", relative)
        if match:
            page = int(match.group(1))
            owner = self.owners.page_owner(page)
            if context.role == "orchestrator" and context.owner_id == "orchestrator":
                with self.workspace._lock:
                    registered = self.workspace._read_state().get("pages", {}).get(str(page))
                return (relative == f"slides/slide_{page:02d}.html"
                        and not (registered or {}).get("retired")
                        and (owner is None or owner.owner_id == context.owner_id))
            return (owner is not None and owner.owner_id == context.owner_id
                    and any(scope.permits(relative) for scope in context.write_scopes))
        if relative == "knowledge.md":
            return context.role == "orchestrator" and context.owner_id == "orchestrator"
        if relative.startswith("research/"):
            target = self.workspace.paths.root / relative
            if any(path.is_symlink() for path in (target, *target.parents)
                   if self.workspace.paths.root in path.parents):
                return False
            return any(item.role == "research" and item.owner_id == context.owner_id
                       and item.output_path == relative and item.active for item in self.owners.list_current())
        if relative == "assets/catalog.md":
            return False  # 可读投影只由显式素材登记生成。
        if not any(scope.permits(relative) for scope in context.write_scopes):
            return False
        if relative.startswith("assets/"):
            return all(record["owner_id"] == context.owner_id for record in self.artifacts.catalog()["images"]
                       if record["path"] == relative)
        return True

    def _orchestrator_direct_authoring_advisory(
        self, context: ToolContext, page: int,
    ) -> dict[str, object] | None:
        """Attach a non-blocking ownership guardrail without using a page-count threshold."""
        if context.role != "orchestrator" or context.owner_id != "orchestrator":
            return None
        if self.owners.page_owner(page) is not None:
            return None
        return {
            "code": "orchestrator_direct_authoring_guardrail",
            "message": (
                f"Orchestrator 已兜底写入未分配页面 slides/slide_{page:02d}.html；这适合短稿、"
                "快速初版、小修或明确判断直接制作更快的场景。若后续页面可独立开工、需要消费 Research/Image "
                "产出、涉及复杂布局/SVG/图表/素材摆放，或来自 inspect/critic 的页面级返修，应优先用 "
                "delegate_submit 交给 Slide Owner 并保持原 ownership。已由 Slide Owner 接管的页面仍受写入边界保护。"
            ),
        }

    def base_tool_handlers(self) -> dict[str, ToolHandler]:
        """返回生产基础工具的 Handler Adapter 映射；契约定义在 runtime.tools。"""
        return {
            "read_file": self.read_file,
            "write_file": self.write_file,
            "patch": self.patch,
            "terminal": self.terminal,
            "web_search": self.web_search,
            "web_extract": self.web_extract,
            "web_screenshot": self.web_screenshot,
            "image_generate": self.image_generate,
        }

    def read_file(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> object:
        """读取授权文本片段，并返回总行数和下一偏移。"""
        relative = normalize_workspace_path(str(arguments["path"]))
        if not context.may_read(relative):
            denied_path = (
                "<private runtime path>"
                if relative in {"_state", "_trace"}
                or relative.startswith(("_state/", "_trace/"))
                else relative
            )
            raise ToolContractError(
                "read_scope_denied",
                f"owner {context.owner_id!r} may not read {denied_path!r}. "
                "Allowed read scopes: " + json.dumps([
                    scope.path + ("/**" if scope.recursive else "") for scope in context.read_scopes
                ], ensure_ascii=False) + ". A path mentioned in a task goal does not grant access; do not bypass this with terminal.",
            )
        web_source = relative.startswith("sources/web/")
        if relative.startswith("skills/"):
            path = self.snapshot.resolve_skill_path(relative)
        elif web_source:
            from .web_extract import WebExtractError, resolve_web_source_ref

            try:
                path = resolve_web_source_ref(self.workspace.paths.root, relative)
            except WebExtractError as exc:
                raise ToolExecutionError(exc.code, str(exc)) from exc
        else:
            path = self.workspace.paths.resolve(relative)
        if not path.is_file():
            raise ToolExecutionError("read_file_missing", f"文件不存在：{relative}")
        content_view = (
            None
            if web_source
            else attachment_content_view(self.workspace.paths.root, relative)
        )
        try:
            text = content_view["content"] if content_view else path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            guidance = (
                "Slide 请用 inspect(page=[页码], question=具体检查问题) 查看当前页；不要用 terminal 读取图片。"
                if context.role == "slide" else "图片请使用 inspect(path=图片路径)；read_file 只接受 UTF-8 文本。"
            )
            raise ToolExecutionError(
                "read_file_binary", guidance
            ) from exc
        lines = (text.split("\n") if text else []) if content_view else text.splitlines()
        search_lines = content_view.pop("_search_lines") if content_view else lines
        offset = int(arguments.get("offset") or 1)
        limit = int(arguments.get("limit") or (40 if content_view else 500))
        query = str(arguments.get("query") or "").strip().casefold()
        if query:
            hits = [i for i in range(max(0, offset - 3), len(lines)) if query in search_lines[i].casefold()]
            indices = sorted({j for i in hits for j in range(max(offset - 1, i - 2), min(len(lines), i + 3))})
        else:
            indices = list(range(offset - 1, len(lines)))
        candidate = indices[:limit]
        budget = ATTACHMENT_CONTENT_HISTORY_BYTES if content_view else READ_FILE_HISTORY_BYTES
        selected: list[int] = []
        rendered: list[str] = []
        content_bytes = 0
        single_line_truncated = False
        for index in candidate:
            line = lines[index] if content_view else f"{index + 1}|{lines[index]}"
            encoded_size = len(line.encode("utf-8")) + (1 if rendered else 0)
            if rendered and content_bytes + encoded_size > budget:
                break
            if not rendered and encoded_size > budget:
                head_chars = max(1, budget // 2)
                tail_chars = max(1, budget - head_chars - 200)
                omitted = max(0, len(line) - head_chars - tail_chars)
                line = (line[:head_chars]
                    + f"\n...[read_file omitted {omitted} chars from one overlong line; narrow the query or source]...\n"
                    + line[-tail_chars:])
                single_line_truncated = True
                encoded_size = len(line.encode("utf-8"))
            selected.append(index)
            rendered.append(line)
            content_bytes += encoded_size
            if single_line_truncated:
                break
        body = "\n".join(rendered)
        history_truncated = bool(single_line_truncated or len(selected) < len(candidate))
        next_offset = selected[-1] + 2 if selected and (history_truncated or len(indices) > len(selected)) else None
        attachment_media = [] if web_source else attachment_excerpt_media(
            self.workspace.paths.root,
            relative,
            selected_indices=selected,
            excerpt=body,
        )
        attached = attachment_media[:ATTACHMENT_READ_MAX_IMAGES]
        return {
            "path": (
                relative
                if relative.startswith("skills/") or web_source
                else self.workspace.paths.relative(path)
            ),
            "logical_path": relative,
            "offset": offset,
            "line_count": len(lines),
            "mode": "search_snippets" if query else "line_range",
            "returned_line_count": len(selected),
            "content_bytes": len(body.encode("utf-8")),
            "content_limit_bytes": budget,
            "content_truncated_for_prompt": history_truncated,
            **({"_multimodal_images": attached} if attached else {}),
            **({"multimodal_images_omitted": len(attachment_media) - len(attached)}
               if len(attachment_media) > len(attached) else {}),
            **({"single_line_truncated_for_prompt": True} if single_line_truncated else {}),
            **({"history_note": "read_file returned a prompt-bounded excerpt; continue with next_offset or a narrower query if more exact text is needed."}
               if history_truncated else {}),
            **({key: value for key, value in content_view.items() if key != "content"} if content_view else {}),
            **({"note": (content_view["note"] + " " if content_view else "") +
               "Literal match excerpts only; next_offset=null means no more excerpts, "
                "not a complete file or section read. Use offset/limit without query to read the section."}
               if query else {}),
            "next_offset": next_offset,
            "content": body,
        }

    def write_file(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> object:
        """原子写入完整文本，并把 Slide HTML 自动提交为当前产物版本。"""
        relative = str(arguments["path"])
        path = self.workspace.paths.resolve(relative)
        advisory = None
        match = re.fullmatch(r"slides/slide_(\d+)\.html", relative)
        if match and context.role == "orchestrator" and context.owner_id == "orchestrator":
            page = int(match.group(1))
            advisory = self._orchestrator_direct_authoring_advisory(context, page)
        self._write_then_commit(context, path, relative, str(arguments["content"]),
            repair_reason=str(arguments.get("repair_reason") or ""))
        return {
            "path": self.workspace.paths.relative(path),
            "logical_path": relative,
            "sha256": file_sha256(path),
            "bytes": path.stat().st_size,
            **({"parallelization_advisory": advisory} if advisory else {}),
        }

    def patch(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> object:
        """精确替换旧文本，数量不符时拒绝写入并保留原文件。"""
        relative = str(arguments["path"])
        path = self.workspace.paths.resolve(relative)
        with self.workspace._lock:
            if not path.is_file():
                raise ToolExecutionError("patch_missing", f"文件不存在：{relative}")
            current = path.read_text(encoding="utf-8")
            old = str(arguments["old_text"])
            expected = int(arguments.get("expected_replacements") or 1)
            actual = current.count(old)
            if actual != expected:
                raise ToolExecutionError(
                    "patch_match_count",
                    f"期望替换 {expected} 处，实际匹配 {actual} 处：{relative}。文件未修改。"
                    "请用 read_file 重新读取该文件的相关原文；query 可定位，offset/limit 可读取连续段落。"
                    "old_text 不含展示行号，须保留原文的 Markdown 标记、空格、换行、引号和反斜杠；"
                    "JSON 只转义一次。复制准确原文或扩大上下文后重新提交，不自动忽略差异或选择某一处匹配。",
                )
            updated = current.replace(old, str(arguments["new_text"]))
            self._write_then_commit(context, path, relative, updated,
                repair_reason=str(arguments.get("repair_reason") or ""))
        return {
            "path": self.workspace.paths.relative(path),
            "logical_path": relative,
            "replacements": actual,
            "sha256": file_sha256(path),
        }

    def _write_then_commit(
        self, context: ToolContext, path: Path, relative: str, content: str,
        *, repair_reason: str = "",
    ) -> None:
        """把 canonical 写入与 artifact-commit 绑成原子边界。

        先记录写入前的磁盘状态，写入后再尝试自动提交；若提交因权限或
        Artifact 状态失败，回滚 canonical 文件到写入前样子，确保失败时不会遗留任何
        未登记的 canonical 改动（must-fix #5）。
        """
        relative = self.workspace.paths.relative(path)
        match = re.fullmatch(r"slides/slide_(\d+)\.html", relative)
        if match and context.role == "orchestrator" and context.owner_id == "orchestrator":
            page = int(match.group(1))
            with self.workspace._lock:
                if not self.may_write(context, relative):
                    raise ToolContractError("write_scope_denied", f"Owner {context.owner_id} 不能写入 {relative}")
                try:
                    self.prepare_slide_submission([{"role": "slide", "pages": [page]}])
                except ToolExecutionError as exc:
                    detail = ("本次写入尚未执行，未取得页面责任。修正本页计划或共享准备后重新提交 write_file："
                        + str((exc.diagnostics or {}).get("preparation_error", str(exc))))
                    raise ToolExecutionError(exc.code, detail,
                        diagnostics={**(exc.diagnostics or {}), "detail": detail}) from exc
        revision = getattr(getattr(self, "run_spec", None), "revision_request", None)
        revision_key = (hashlib.sha256(json.dumps(dict(revision), ensure_ascii=False,
            sort_keys=True).encode()).hexdigest() if revision is not None else "initial")
        edit = (self.workspace.page_edit(int(match.group(1)),
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            repair_reason=repair_reason, revision_key=revision_key) if match else nullcontext())
        try:
            with self.workspace._lock, edit:
                if not self.may_write(context, relative):
                    raise ToolContractError("write_scope_denied", f"Owner {context.owner_id} 不能写入 {relative}")
                if relative == "config.json":
                    try:
                        module = load_skill_module(self.snapshot.target, self.snapshot.tree_sha256, "deck_config")
                        module.validate_config(json.loads(content))
                    except (ValueError, TypeError) as exc:
                        raise ToolExecutionError("creation_config_invalid", str(exc)) from exc
                prior_bytes = path.read_bytes() if path.is_file() else None
                if (match and context.owner_id == "orchestrator" and prior_bytes == content.encode("utf-8")
                        and self.owners.page_owner(int(match.group(1))) is None):
                    return
                prior_state = ((self.workspace._read_state(), self.artifacts._read_state(), self.owners._read_state())
                               if match else None)
                try:
                    _atomic_text_write(path, content)
                    self._commit_slide_if_needed(context, relative)
                    if match and context.owner_id == "orchestrator":
                        self.owners.claim_orchestrator_page(int(match.group(1)))
                except BaseException:
                    if prior_bytes is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_bytes_write(path, prior_bytes)
                    if prior_state is not None:
                        self.workspace._write_state(prior_state[0])
                        self.artifacts._write_state(prior_state[1])
                        self.owners._write_state(prior_state[2])
                    raise
        except PageRepairError as exc:
            raise ToolExecutionError(exc.code, str(exc), diagnostics=exc.diagnostics) from exc

    def terminal(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> object:
        """执行受限目录查看、文本检索或角色 Skill 命令；不启动 shell。"""
        command = str(arguments["command"])
        argv, script_role, action, request = self._validated_command(context.role, command)
        if script_role == "ls":
            return self._list_directories(context, request)
        if script_role == "search":
            return self._search_files(context, request)
        if script_role == "orchestrator" and action == "help" and argv[2:] == ["--help"]:
            module = load_skill_module(self.snapshot.target, self.snapshot.tree_sha256, "cli")
            parser = module.build_parser("orchestrator", authoring_only=True)
            parser.prog = ROLE_SCRIPT_ENTRIES["orchestrator"]
            return {"returncode": 0, "stdout": parser.format_help(), "stderr": ""}
        if script_role == "image" and action == "finalize":
            if request.owner_id and request.owner_id != context.owner_id:
                raise ToolContractError("asset_owner_conflict", "素材 finalize 只能检查当前作者的素材")
            if not request.owner_id:
                argv.extend(["--owner-id", context.owner_id])
        if script_role == "image" and action == "fetch":
            self._ensure_external_allowed(context)
            with self.workspace._lock:
                images = self.artifacts.catalog()["images"]
                requested = set(request.asset or [])
                if requested and any(not any(row["artifact_id"] == asset_id and row["owner_id"] == context.owner_id
                                             for row in images) for asset_id in requested):
                    raise ToolContractError("asset_owner_conflict", "只能下载/替换当前 Owner 已登记的素材 ID")
                if not requested:
                    requested = {row["artifact_id"] for row in images if row["owner_id"] == context.owner_id
                                 and (row.get("image_metadata") or {}).get("kind") == "real"}
                    if not requested:
                        return {"returncode": 0, "stdout": "status:PASS\nimages:0 registered real assets for this Owner", "stderr": ""}
                    for asset_id in sorted(requested):
                        argv.extend(["--asset", asset_id])
                request.asset = sorted(requested)
        if script_role == "image" and action in {"crop", "crop-material", "extract-subject", "register-user"}:
            source = request.source
            if source:
                relative = self.workspace.paths.relative(self.workspace.paths.resolve(source))
                roots = ("inputs/",) if action == "register-user" else ("inputs/", "assets/")
                if not relative.startswith(roots) or not context.may_read(relative):
                    raise ToolContractError("read_scope_denied", f"Image cannot read {relative}")
                if relative.startswith("inputs/"):
                    materialize_page_image(self.workspace.paths.root, relative)
        timeout_s = min(
            int(arguments.get("timeout_s") or self.settings.command_timeout_s),
            self.settings.command_timeout_s,
        )
        if script_role == "orchestrator" and action == "prepare-revision":
            operations_path = normalize_workspace_path(str(request.operations))
            if operations_path != "revision-operations.json" or not context.may_read(operations_path):
                raise ToolContractError(
                    "read_scope_denied",
                    "prepare-revision 只读取公开控制输入 revision-operations.json",
                )
        if script_role == "orchestrator" and action in {"audit", "finalize"}:
            from orchestration.manager import OrchestrationError, assert_tasks_terminal
            state = self.workspace.paths.state / "orchestration.json"
            try:
                assert_tasks_terminal(json.loads(state.read_text()) if state.is_file() else {})
            except OrchestrationError as exc:
                raise ToolExecutionError("unfinished_tasks", str(exc)) from exc
            close_renderer = getattr(self.renderer, "close", None)
            if close_renderer is not None:
                close_renderer()
        if script_role == "slide" and action in {"render", "render-group"}:
            group = ""
            if action == "render":
                if request.page < 1:
                    raise ToolContractError("terminal_argument_invalid", "--page 必须为正整数")
                pages = [request.page]
            else:
                group = request.group.strip().lower().replace("_", "-")
                tokens = [token.strip() for token in request.pages.split(",")]
                if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", group) or not all(
                    token.isdigit() and int(token) > 0 for token in tokens
                ):
                    raise ToolContractError("terminal_argument_invalid", "--group 必须为安全组名，--pages 必须为逗号分隔的正整数")
                pages = [int(token) for token in tokens]
                if len(set(pages)) != len(pages):
                    raise ToolContractError("terminal_argument_invalid", "--pages 不允许重复页码")
            owned = {int(item) for item in context.metadata.get("pages", [])}
            for page in pages:
                relative = f"slides/slide_{page:02d}.html"
                if page not in owned:
                    raise ToolContractError("slide_page_scope_denied", f"Owner 不拥有页面 {page}")
                if not context.may_read(relative):
                    raise ToolContractError("read_scope_denied", f"Owner 不能读取 {relative}")
                if not context.may_write(relative):
                    raise ToolContractError("write_scope_denied", f"Owner 不能制作 {relative}")
            stdout: list[str] = []

            def render_once(
                html_path: Path, png_path: Path, canvas: Mapping[str, object]
            ) -> Mapping[str, object]:
                """让 ArtifactStore 通过正式 Renderer 获取一次同源回执。"""
                receipt = self.renderer(html_path, png_path, canvas)
                stdout.append(self.renderer.last_stdout)
                return receipt

            try:
                snapshots = self.artifacts.render_pages(pages, render_once)
            except UncommittedPageSourceError as exc:
                raise ToolExecutionError("render_source_uncommitted", str(exc)) from exc
            result = {
                "returncode": 0,
                "stdout": "\n".join(item for item in stdout if item)[-12000:],
                "pages": [item.to_dict() for item in snapshots],
            }
            if group:
                module = load_skill_module(self.snapshot.target, self.snapshot.tree_sha256, "render_deck")
                renderer = module.DeckRenderer(self.workspace.paths.root / "present.html",
                    self.workspace.paths.renders, workspace=self.workspace.paths.root, motion_check=False)
                sheet = renderer._contact_sheet([self.workspace.paths.page_png(page) for page in pages],
                    name=f"contact-sheet-group-{group}.png", labels=pages)
                if sheet is None:
                    raise ToolExecutionError("group_contact_sheet_failed", f"Group {group} 联系表生成失败；单页渲染回执已保存")
                result["contact_sheet"] = self.workspace.paths.relative(sheet)
            return result

        if script_role == "orchestrator" and action in {"validate-plans", "scaffold", "sync-speech", "finalize"}:
            plan = load_skill_module(self.snapshot.target, self.snapshot.tree_sha256, "deck_plan")
            _, sections = plan.read_deck(self.workspace.paths.root)
            numbers = [section["number"] for section in sections]
            self.workspace.register_pages(numbers)

        image_leases = {}
        image_results = []

        def receive_line(line):
            if script_role == "image":
                image_results.extend(self._ingest_image_receipts(context, line, image_leases))

        def execute(transaction_id="", lock_fd=None):
            env = self._command_environment()
            if transaction_id:
                env["MURAL_REVISION_TRANSACTION"] = transaction_id
            return _run_command(argv, cwd=self.workspace.paths.root, timeout_s=timeout_s,
                                environment=env, pass_fds=() if lock_fd is None else (lock_fd,),
                                **({"on_stdout_line": receive_line}
                                   if script_role == "image" and action in {"register", "fetch"} else {}))

        try:
            if script_role == "orchestrator" and action in {"delete-page", "insert-page", "move-page", "commit-revision"}:
                from owner.registry import OwnerRegistry
                from state.revisions import RevisionTransaction
                registry = OwnerRegistry(self.workspace.paths.root, deck_id=self.workspace.deck_id)
                completed = RevisionTransaction(self.workspace, self.artifacts, registry).run(
                    execute, structural=action != "commit-revision")
            else:
                if script_role == "image":
                    with self._validate_image_write(context, action, request) as image_leases:
                        completed = execute()
                        if completed.returncode == 0 and action in {
                            "crop", "crop-material", "extract-subject", "register-user", "remove-checkerboard"
                        }:
                            for asset_id, transaction_id in list(image_leases.items()):
                                current = self.artifacts.artifact(asset_id)
                                metadata = current.image_metadata or {
                                    "kind": "user" if action == "register-user" else "material"
                                }
                                record = self.artifacts.record_image_result(
                                    artifact_id=asset_id, owner_id=context.owner_id, path=current.path,
                                    status="available", source_url=current.source_url,
                                    image_metadata=metadata, transaction_id=transaction_id)
                                image_results.append(record.to_dict())
                                del image_leases[asset_id]
                else:
                    completed = execute()
        except ToolExecutionError as exc:
            if script_role != "image":
                raise
            payload = _compact_image_terminal_payload(
                exc.diagnostics if isinstance(exc.diagnostics, Mapping) else {"detail": str(exc)},
                images=image_results)
            raise ToolExecutionError(exc.code, str(exc), diagnostics=payload) from exc
        if script_role == "orchestrator" and action == "scaffold":
            self._register_scaffolded_pages()
        if script_role == "image" and action == "finalize":
            self.snapshot.capture_runtime_fonts()
        result = {
            "returncode": completed.returncode,
            "diagnostics": completed.diagnostics,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            **({"images": image_results} if image_results else {}),
        }
        return _compact_image_terminal_payload(result, images=image_results) if script_role == "image" else result

    @contextmanager
    def _validate_image_write(self, context: ToolContext, action: str, request):
        """写入前按唯一真实路径预留现有素材 lease；慢操作在工作区锁外执行。"""
        leases = {}
        try:
            with self.workspace._lock:
                images = self.artifacts.catalog()["images"]
                if action == "fetch":
                    targets = [(row["path"], row["artifact_id"]) for row in images
                               if row["artifact_id"] in set(request.asset or [])]
                else:
                    target = (request.path if action == "register" else request.asset if action == "remove-checkerboard"
                              else request.output if action in {"crop", "crop-material", "extract-subject", "register-user", "generate", "web-screenshot"} else "")
                    targets = [(target, request.id if action == "register" else "")] if target else []
                for target, requested_id in targets:
                    relative = self.workspace.paths.relative(self.workspace.paths.resolve(target))
                    if not self.may_write(context, relative):
                        raise ToolContractError("asset_owner_conflict", f"Owner {context.owner_id} 不能改写素材 {relative}")
                    current = next((row for row in images if row["path"] == relative), None)
                    if action != "register" and current is None and self.workspace.paths.resolve(relative).exists():
                        raise ToolContractError("asset_registration_required", "替换已有图片前先用 image.py register 显式登记责任；或使用新的输出路径")
                    asset_id = requested_id or (current["artifact_id"] if current else
                        "image-" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20])
                    record = self.artifacts.declare(artifact_id=asset_id, kind="image", path=relative,
                        owner_id=context.owner_id, replace_unavailable=action == "web-screenshot"
                        or action == "register" and request.status == "available")
                    leases[record.artifact_id] = self.artifacts.begin_transaction(
                        context.owner_id, [record.artifact_id], external_process=action not in {"generate", "web-screenshot"})
            yield leases
        except ArtifactStateError as exc:
            raise ToolContractError("asset_write_conflict", str(exc)) from exc
        finally:
            for transaction_id in leases.values():
                self.artifacts.abort_transaction(transaction_id, owner_id=context.owner_id)

    def web_search(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> object:
        """调用 Serper 搜索，并保持图片直链和来源页两个字段。"""
        self._ensure_external_allowed(context)
        self._research_web_begin(context, "web_search")
        key = str(self.environment.get("SERPER_API_KEY") or "").strip()
        if not key:
            raise ToolExecutionError("search_unconfigured", "SERPER_API_KEY 未配置")
        query = re.sub(r"\s+", " ", str(arguments["query"])).strip()
        cache_key = self._web_cache_key("search", {**arguments, "query": query})
        if cached := self._web_cached(cache_key):
            advisory = self._research_web_success(context, "web_search", cached)
            return {**cached, **advisory}
        import requests

        search_type = str(arguments.get("search_type") or "search")
        endpoint = "images" if search_type == "images" else "search"
        base = str(
            self.environment.get("SERPER_BASE_URL") or "https://google.serper.dev"
        ).rstrip("/")
        url = base if base.endswith(f"/{endpoint}") else f"{base}/{endpoint}"
        locale = {key: str(arguments[arg]).strip() for arg, key in (("language", "hl"), ("region", "gl"))
                  if str(arguments.get(arg) or "").strip().lower() not in {"", "auto"}}
        deadline = time.monotonic() + 45
        with self.slots.acquire("web_search", deadline_monotonic=deadline):
            response = requests.post(
                url,
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={
                    "q": query,
                    "num": int(arguments.get("limit") or 5),
                    **locale,
                },
                timeout=max(1.0, deadline - time.monotonic()),
            )
        response.raise_for_status()
        payload = response.json()
        raw_rows = payload.get("images" if endpoint == "images" else "organic") or []
        rows: list[dict[str, object]] = []
        for item in raw_rows[: int(arguments.get("limit") or 5)]:
            if not isinstance(item, Mapping):
                continue
            if endpoint == "images":
                rows.append(
                    {
                        "title": item.get("title"),
                        "url": item.get("imageUrl") or item.get("link"),
                        "source": item.get("link") or item.get("source"),
                        "source_name": item.get("source"),
                        "width": item.get("imageWidth"),
                        "height": item.get("imageHeight"),
                    }
                )
            else:
                rows.append(
                    {
                        "title": item.get("title"),
                        "url": item.get("link"),
                        "source": item.get("source"),
                        "snippet": item.get("snippet"),
                    }
                )
        result = self._web_store(cache_key, {"query": query, "locale": locale, "results": rows})
        return {**result, **self._research_web_success(context, "web_search", result)}

    def web_extract(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> object:
        """有界获取完整正文，展示片段与可续读来源分离。"""
        self._ensure_external_allowed(context)
        url = str(arguments["url"]).strip().split("#", 1)[0]
        if not re.match(r"^https?://", url, re.I):
            raise ToolExecutionError("web_extract_url", "只接受 http/https 来源 URL")
        if re.search(r"\.(?:png|jpe?g|gif|webp|docx?|pptx?|xlsx?)(?:$|[?#])", url, re.I):
            raise ToolExecutionError(
                "web_extract_binary",
                "web_extract 处理 HTML / PDF 文本；图片由 Image 获取，Office 请使用公开 HTML 来源页",
            )

        self._research_web_begin(context, "web_extract")
        render_js = bool(arguments.get("render_js", False))
        prior_failure = self._cached_web_extract_failure(url, render_js)
        if prior_failure is not None:
            code = str(prior_failure.get("error_code") or "web_extract_failed")
            detail = (
                "This source already had a deterministic extraction failure in this run; "
                "do not retry it unchanged. Use another accessible source."
            )
            convergence = self._research_web_failure(context)
            raise ToolExecutionError(
                code,
                detail,
                diagnostics={
                    **prior_failure,
                    "cached_failure": True,
                    "retry_recommended": False,
                    "recommended_action": (
                        "use another accessible source or a reliable search-result summary"
                    ),
                    "detail": detail,
                    **convergence,
                },
            )

        def download() -> bytes | tuple[bytes, str]:
            if render_js:
                deadline = time.monotonic() + self.settings.web_extract_timeout_s
                with self.slots.acquire("render", deadline_monotonic=deadline):
                    self.workspace.paths.runtime_tmp.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="web-", dir=self.workspace.paths.runtime_tmp) as temporary:
                        output = Path(temporary) / "page.html"
                        remaining = max(0.1, deadline - time.monotonic())
                        errors = []
                        try:
                            _run_command([sys.executable, str(Path(__file__).with_name("web_browser.py")),
                                url, str(output), "--timeout", str(remaining),
                                "--max-bytes", str(self.settings.web_extract_max_bytes)],
                                cwd=self.workspace.paths.root, timeout_s=remaining,
                                environment=self._command_environment(),
                                on_stdout_line=lambda line: errors.append(json.loads(line)))
                        except ToolExecutionError as exc:
                            if exc.code == "skill_command_failed" and errors:
                                error = errors[-1]
                                raise ToolExecutionError(error["error_code"], error["detail"],
                                    diagnostics={**exc.diagnostics, "error_code": error["error_code"],
                                                 "detail": error["detail"]}) from exc
                            raise
                        return output.read_bytes()
            curl = shutil.which("curl")
            if not curl:
                raise ToolExecutionError("web_extract_unavailable", "curl 未安装")
            try:
                completed = subprocess.run(
                    [curl, "--location", "--silent", "--show-error", "--fail",
                     "--max-time", str(self.settings.web_extract_timeout_s),
                     "--connect-timeout", str(min(5, self.settings.web_extract_timeout_s)),
                     "--max-filesize", str(self.settings.web_extract_max_bytes),
                     "--write-out", "%{stderr}\n%{url_effective}",
                     "--user-agent", "Mozilla/5.0 MURALPresenterResearch/1.0", "--", url],
                    capture_output=True, timeout=self.settings.web_extract_timeout_s + 3,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                detail = "Web source download exceeded the configured timeout."
                raise ToolExecutionError(
                    "web_extract_timeout",
                    detail,
                    diagnostics=_web_extract_error_receipt("web_extract_timeout", detail),
                ) from exc
            except OSError as exc:
                detail = f"Web source download could not start: {type(exc).__name__}."
                raise ToolExecutionError(
                    "web_extract_network_failed",
                    detail,
                    diagnostics=_web_extract_error_receipt(
                        "web_extract_network_failed", detail
                    ),
                ) from exc
            if completed.returncode != 0:
                code, detail = _web_extract_download_error(
                    completed.returncode, completed.stderr
                )
                raise ToolExecutionError(
                    code,
                    detail,
                    diagnostics=_web_extract_error_receipt(code, detail),
                )
            effective_url = completed.stderr.rsplit(b"\n", 1)[-1].decode("utf-8")
            return completed.stdout, effective_url

        from .web_extract import WebExtractError, extract_web_document, web_source_ref

        try:
            requested_max_chars = int(arguments.get("max_chars") or WEB_EXTRACT_HISTORY_CHARS)
            max_chars = min(requested_max_chars, WEB_EXTRACT_HISTORY_CHARS)
            result = extract_web_document(self.workspace.paths.root, url,
                max_chars=max_chars,
                max_bytes=self.settings.web_extract_max_bytes, download=download, render_js=render_js)
            if requested_max_chars > max_chars:
                result["requested_max_chars"] = requested_max_chars
                result["prompt_window_clamped"] = True
            prompt_result = _web_extract_prompt_payload(
                result,
                source_ref=web_source_ref(result),
            )
            return {
                **prompt_result,
                **self._research_web_success(context, "web_extract", prompt_result),
            }
        except ToolExecutionError as exc:
            self._remember_web_extract_failure(
                url, render_js, exc.code, str(exc)
            )
            diagnostics = exc.diagnostics
            if not (
                isinstance(diagnostics, Mapping)
                and diagnostics.get("error_code") == exc.code
                and "retry_recommended" in diagnostics
                and "recommended_action" in diagnostics
            ):
                diagnostics = _web_extract_error_receipt(exc.code, str(exc))
            convergence = self._research_web_failure(context)
            raise ToolExecutionError(
                exc.code,
                str(exc),
                diagnostics={**dict(diagnostics), **convergence},
            ) from exc
        except WebExtractError as exc:
            detail = str(exc)
            self._remember_web_extract_failure(
                url, render_js, exc.code, detail
            )
            convergence = self._research_web_failure(context)
            raise ToolExecutionError(
                exc.code,
                detail,
                diagnostics={**_web_extract_error_receipt(exc.code, detail), **convergence},
            ) from exc

    def web_screenshot(self, context: ToolContext, arguments: Mapping[str, object]) -> object:
        """Capture a public webpage viewport or an original PDF page with an image lease."""
        if context.role not in {"orchestrator", "image"}:
            raise ToolContractError("tool_role_denied", "Web screenshots are acquired by Orchestrator or Image.")
        self._ensure_external_allowed(context)
        url = str(arguments["url"]).strip()
        try:
            parsed = urlsplit(url)
        except ValueError as exc:
            raise ToolExecutionError("web_screenshot_url", "Use a valid public HTTP(S) URL.") from exc
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None):
            raise ToolExecutionError("web_screenshot_url", "Use a public HTTP(S) URL without credentials.")
        query = str(arguments.get("query") or "").strip()
        if len(query) > 500:
            raise ToolExecutionError("invalid_arguments", "Use a literal query of at most 500 characters.")
        digest = hashlib.sha256(f"{url}\0{query}".encode("utf-8")).hexdigest()[:16]
        relative = f"assets/web-capture-{digest}.png"
        target = self.workspace.paths.resolve(relative)
        audit_path = self.workspace.paths.trace / "image" / f"web-capture-{digest}.json"
        if not context.may_read(relative):
            raise ToolContractError("read_scope_denied", "Owner cannot read the acquired screenshot.")

        def validate_capture(capture):
            if (not isinstance(capture, Mapping) or capture.get("url") != url or capture.get("query") != query
                    or capture.get("viewport") != {"width": 1280, "height": 900}
                    or capture.get("capture_scope") not in {"viewport", "pdf_page"} or not isinstance(capture.get("title"), str)
                    or not isinstance(capture.get("effective_url"), str)):
                raise ValueError("unexpected browser capture receipt")
            source = urlsplit(capture["effective_url"])
            if (source.scheme not in {"http", "https"} or not source.hostname
                    or source.username is not None or source.password is not None):
                raise ValueError("unexpected browser source URL")
            warnings = capture.get("resource_warnings", [])
            if not isinstance(warnings, list) or len(warnings) > 8 or any(not isinstance(item, str) for item in warnings):
                raise ValueError("unexpected browser resource warnings")
            if capture["capture_scope"] == "pdf_page":
                page, count = capture.get("source_page"), capture.get("source_page_count")
                if type(page) is not int or type(count) is not int or not 1 <= page <= count:
                    raise ValueError("unexpected PDF physical page receipt")

        def receipt(record, capture, *, reused):
            return {"artifact_id": record.artifact_id, "path": relative, "owner_id": record.owner_id,
                "source_url": capture["effective_url"], "title": capture["title"],
                "width": 1280, "height": 900, "capture_scope": capture["capture_scope"],
                **({key: capture[key] for key in ("source_page", "source_page_count")}
                   if capture["capture_scope"] == "pdf_page" else {}),
                "captured_at": capture["captured_at"], "reused": reused,
                **({"resource_warnings": capture["resource_warnings"]} if capture.get("resource_warnings") else {}),
                "registration_note": (
                    ("真实 PDF 原件页图，source_page 是从 1 开始的物理页码，未 OCR 或重绘。"
                     if capture["capture_scope"] == "pdf_page" else
                     "真实网页视口截图，非整页扫描，未登录或重排原文。")
                    + "先用 inspect(path=...) 核验所需内容、遮挡与清晰度；"
                    "再由 owner_id 指定的原素材作者沿用 artifact_id 和 path，以 --kind real、--source source_url 补齐用途与消费者。"
                    "截图获取成功不等于用户要求或视觉验收已经通过。"
                )}

        with self.workspace._lock:
            row = next((item for item in self.artifacts.catalog()["images"] if item["path"] == relative), None)
            if row is not None and row["status"] == "committed":
                try:
                    self.artifacts._validate_transaction(self.artifacts._read_state(), row["owner_id"],
                        [row["artifact_id"]], "")
                    capture = json.loads(audit_path.read_text(encoding="utf-8"))
                    validate_capture(capture)
                    if (capture["url"] != url or capture["query"] != query
                            or capture["sha256"] != file_sha256(target) or row["sha256"] != capture["sha256"]):
                        raise ValueError("source or pixel fingerprint mismatch")
                    return receipt(self.artifacts.artifact(row["artifact_id"]), capture, reused=True)
                except (ArtifactStateError, OSError, ValueError, KeyError, TypeError) as exc:
                    raise ToolExecutionError("web_screenshot_cache_invalid",
                        "The saved screenshot or source receipt changed; preserve it for owner review.") from exc

        prior = self._cached_web_extract_failure(url, True)
        if prior:
            code = str(prior.get("error_code") or "web_extract_failed").replace("web_extract_", "web_screenshot_", 1)
            raise ToolExecutionError(code, "This source already failed under the same access conditions; choose another source.")
        started = time.monotonic()
        total_deadline = started + self.settings.command_timeout_s
        queue_deadline = min(total_deadline, started + max(120, self.settings.web_extract_timeout_s))
        with self._validate_image_write(context, "web-screenshot", argparse.Namespace(output=relative)) as leases:
            asset_id, transaction_id = next(iter(leases.items()))
            try:
                with self.slots.acquire("render", deadline_monotonic=queue_deadline):
                    # Queue contention must not consume the source's browser budget.
                    deadline = min(total_deadline, time.monotonic() + self.settings.web_extract_timeout_s)
                    self.workspace.paths.runtime_tmp.mkdir(parents=True, exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix="web-capture-", dir=self.workspace.paths.runtime_tmp) as temporary:
                        output = Path(temporary) / "capture.png"
                        remaining = max(.1, deadline - time.monotonic())
                        capture_records = []
                        try:
                            _run_command([sys.executable, str(Path(__file__).with_name("web_browser.py")),
                                url, str(output), "--timeout", str(remaining),
                                "--max-bytes", str(WEB_SCREENSHOT_MAX_BYTES), "--screenshot", "--query", query,
                                "--resource-module", str(self.snapshot.resolve_skill_path(
                                    f"skills/{self.snapshot.name}/scripts/_internal/remote_resources.py"))],
                                cwd=self.workspace.paths.root, timeout_s=remaining,
                                environment=self._command_environment(),
                                on_stdout_line=lambda line: capture_records.append(json.loads(line)))
                        except ToolExecutionError as exc:
                            if capture_records and capture_records[-1].get("error_code"):
                                error = capture_records[-1]
                                raise ToolExecutionError(str(error["error_code"]).replace("web_extract_", "web_screenshot_", 1),
                                    str(error.get("detail") or "Source capture failed.")) from exc
                            raise ToolExecutionError("web_screenshot_timeout" if time.monotonic() >= deadline
                                else "web_screenshot_failed", "The browser capture did not complete within its limits.") from exc
                        if not capture_records:
                            raise ValueError("Browser capture did not provide a source receipt")
                        # CommandResult stdout is a bounded diagnostic excerpt, not the source receipt.
                        capture = capture_records[-1]
                        validate_capture(capture)
                        payload = output.read_bytes()
                        if len(payload) > WEB_SCREENSHOT_MAX_BYTES:
                            raise ToolExecutionError("web_screenshot_too_large", "Screenshot exceeds the byte limit.")
                        from PIL import Image
                        with Image.open(io.BytesIO(payload)) as image:
                            if image.format != "PNG" or image.size != (1280, 900):
                                raise ValueError("unexpected screenshot format or dimensions")
                            image.verify()
                        capture.update(sha256=hashlib.sha256(payload).hexdigest(),
                            captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                        _atomic_json_write(audit_path, capture)
                        _atomic_bytes_write(target, payload)
                record = self.artifacts.record_image_result(artifact_id=asset_id, owner_id=context.owner_id,
                    path=relative, status="available", source_url=capture["effective_url"],
                    image_metadata={"kind": "real", "source": capture["effective_url"],
                        "notes": (f"Original public PDF physical page {capture['source_page']}; acquisition is not visual verification."
                                  if capture["capture_scope"] == "pdf_page" else
                                  "Original public webpage viewport; acquisition is not visual verification.")},
                    transaction_id=transaction_id)
                del leases[asset_id]
                return receipt(record, capture, reused=False)
            except Exception as exc:
                resource_busy = isinstance(exc, ResourceSlotTimeout)
                code = ("web_screenshot_resource_busy" if resource_busy else
                    exc.code if isinstance(exc, ToolExecutionError) else "web_screenshot_failed")
                if code.startswith("web_screenshot_http_"):
                    self._remember_web_extract_failure(url, True, code.replace("web_screenshot_", "web_extract_", 1), str(exc))
                self.artifacts.record_image_result(artifact_id=asset_id, owner_id=context.owner_id,
                    path=relative, status="unavailable", source_url=url, reason=code, transaction_id=transaction_id)
                del leases[asset_id]
                detail = ("Screenshot deferred while waiting for render capacity; the source was not accessed."
                    if resource_busy else str(exc)[:300])
                raise ToolExecutionError(code, detail, diagnostics={
                    "status": "deferred" if resource_busy else "unavailable",
                    "error_code": code, "detail": detail,
                    **({"source_access": "not_attempted", "retry_recommended": True} if resource_busy else {}),
                    "recommended_action": (
                        "Retry this URL after outstanding rendering finishes; do not treat it as an inaccessible source "
                        "or recreate the evidence." if resource_busy else
                        "Choose another permitted source or case; do not recreate source evidence.")}) from exc

    def image_generate(
        self, context: ToolContext, arguments: Mapping[str, object]
    ) -> object:
        """调用 OpenAI-compatible Images API，并按 prompt 内容寻址保存结果。"""
        self._ensure_external_allowed(context)
        owner_id = context.owner_id
        failures = self._image_failures.get(owner_id, 0)
        key = str(
            self.environment.get("IMAGE_API_KEY")
            or self.environment.get("MODEL_API_KEY")
            or ""
        ).strip()
        prompt = str(arguments["prompt"])
        ratio = str(arguments.get("aspect_ratio") or "landscape")
        size = {
            "landscape": str(
                self.environment.get("IMAGE_GENERATION_LANDSCAPE_SIZE") or "1536x1024"
            ),
            "portrait": str(
                self.environment.get("IMAGE_GENERATION_PORTRAIT_SIZE") or "1024x1536"
            ),
            "square": str(
                self.environment.get("IMAGE_GENERATION_SQUARE_SIZE") or "1024x1024"
            ),
        }[ratio]
        digest = hashlib.sha256(f"{ratio}\0{prompt}".encode("utf-8")).hexdigest()[:16]
        relative = f"assets/generated-{digest}.png"
        target = self.workspace.paths.resolve(relative)
        registration_note = (
            "图片已自动登记；artifact_id、path、owner_id 是现有素材身份。"
            "后续 image.py register 由该 owner_id 沿用返回的 artifact_id 和 path，补充用途、消费者及区域说明。"
            "业务名称写入 purpose，不为同一路径另起 ID、复制文件或重新生图。登记成功不等于视觉验收。"
        )
        with self.workspace._lock:
            existing = next((row for row in self.artifacts.catalog()["images"] if row["path"] == relative), None)
            if existing is not None and existing["status"] == "committed" and target.is_file():
                try:
                    self.artifacts._validate_transaction(self.artifacts._read_state(), existing["owner_id"],
                        [existing["artifact_id"]], "")
                except ArtifactStateError as exc:
                    raise ToolContractError("asset_write_conflict", str(exc)) from exc
                if not context.may_read(relative):
                    raise ToolContractError("read_scope_denied", f"Owner 不能读取 {relative}")
                if file_sha256(target) != existing["sha256"]:
                    raise ToolExecutionError("asset_content_changed", "缓存图片已改变，须交回原 Owner 核对并重新登记")
                return {**existing, "reused": True, "registration_note": registration_note}
        if failures >= self.settings.image_generation_max_failure_streak:
            raise ToolExecutionError("provider_circuit_open", "生图供应商连续失败上限已达到")
        if not key:
            raise ToolExecutionError("image_generation_unconfigured", "IMAGE_API_KEY 未配置")
        import requests

        base = str(
            self.environment.get("IMAGE_BASE_URL")
            or self.environment.get("MODEL_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        model = str(self.environment.get("IMAGE_MODEL") or "gpt-image-1")
        deadline = time.monotonic() + self.settings.image_generation_timeout_s
        with self._validate_image_write(context, "generate", argparse.Namespace(output=relative)) as leases:
            asset_id, transaction_id = next(iter(leases.items()))
            reused = target.is_file()
            if not reused:
                try:
                    with self.slots.acquire("image_generation", deadline_monotonic=deadline):
                        response = requests.post(
                            f"{base}/images/generations",
                            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                            json={"model": model, "prompt": prompt, "size": size, "n": 1},
                            timeout=max(1.0, deadline - time.monotonic()),
                        )
                    response.raise_for_status()
                    value = response.json()
                    rows = value.get("data") if isinstance(value, dict) else None
                    if not isinstance(rows, list) or not rows:
                        raise ProductionError("图片供应商没有返回 data")
                    item = rows[0]
                    if not isinstance(item, Mapping):
                        raise ProductionError("图片供应商 data[0] 不是对象")
                    if item.get("b64_json"):
                        payload = base64.b64decode(str(item["b64_json"]), validate=True)
                    elif item.get("url"):
                        downloaded = requests.get(
                            str(item["url"]),
                            timeout=self.settings.image_generation_download_timeout_s,
                        )
                        downloaded.raise_for_status()
                        payload = downloaded.content
                    else:
                        raise ProductionError("图片供应商既未返回 b64_json 也未返回 url")
                    from PIL import Image

                    with Image.open(io.BytesIO(payload)) as image:
                        image.load()
                        encoded = io.BytesIO()
                        if image.mode not in {"RGB", "RGBA", "L", "LA", "P", "1", "I", "I;16"}:
                            image = image.convert("RGB")
                        image.save(encoded, format="PNG")
                        payload = encoded.getvalue()
                    _atomic_bytes_write(target, payload)
                    self._image_failures[owner_id] = 0
                except Exception as exc:
                    self._image_failures[owner_id] = failures + 1
                    if isinstance(exc, ToolExecutionError):
                        raise
                    detail = f"{type(exc).__name__}: {str(exc)[:800]}"
                    if isinstance(exc, requests.HTTPError) and exc.response is not None:
                        try:
                            error = exc.response.json().get("error", {})
                        except (ValueError, AttributeError):
                            error = {}
                        if isinstance(error, dict):
                            # 仅公开供应商错误码/说明，不保存整份响应或敏感请求。
                            fields = {name: error[name] for name in ("code", "type", "message") if name in error}
                            detail += " " + json.dumps(fields, ensure_ascii=False)
                    raise ToolExecutionError(
                        "image_generation_failed", detail
                    ) from exc
            current = self.artifacts.artifact(asset_id)
            record = self.artifacts.record_image_result(artifact_id=asset_id, owner_id=owner_id, path=relative,
                status="available", source_url=current.source_url, image_metadata=current.image_metadata or {"kind": "generated"},
                transaction_id=transaction_id)
            del leases[asset_id]
            return {**record.to_dict(), "reused": reused, "registration_note": registration_note}

    def native_vision(
        self, path: Path, _question: str, _scope: str, _canvas: Mapping[str, object]
    ) -> Mapping[str, object]:
        """只返回像素与检查回执元数据；真实图片由 Agent loop 放回原会话。"""
        relative = self.workspace.paths.relative(path)
        if not path.is_file() and path.with_suffix(".json").is_file():
            path = materialize_page_image(self.workspace.paths.root, relative)
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = file_sha256(path)
        return {
            "inspection_id": f"inspection-{digest[:20]}",
            "path": relative,
            "sha256": digest,
            "native_multimodal": True,
        }

    def _validated_command(
        self, role: str, command: str
    ) -> tuple[list[str], str, str, object]:
        """校验解释器、脚本、动作和工作区根参数，拒绝 shell 包装。"""
        normalized_role = str(role).strip().lower()
        entry = ROLE_SCRIPT_ENTRIES.get(normalized_role)
        expected = f"skills/{self.snapshot.name}/scripts/{entry}" if entry else None
        guidance = (f"使用 python {expected} --help 查看当前角色的命令。" if expected
                    else "当前角色没有 terminal 命令。")
        try:
            # 后续仅用 argv 启动固定脚本、不经过 shell；引号内或转义的标点属于参数内容。
            argv = shlex.split(command)
        except ValueError as exc:
            raise ToolContractError("terminal_parse_failed", f"{exc}. JSON 内容请用 write_file 写入文件，"
                "不要内嵌到命令引号中；命令只传文件路径。使用当前角色脚本的 --help 核对用法。") from exc
        if argv and argv[0] in {"grep", "rg"}:
            if expected is None:
                raise ToolContractError("terminal_role_denied", guidance)
            return argv, "search", "search", self._parse_search(command)
        if argv and (argv[0] == "ls" or re.match(r"^\s*ls[;|]", command)):
            if expected is None:
                raise ToolContractError("terminal_role_denied", guidance)
            return argv, "ls", "list", self._parse_listing(command)
        if len(argv) < 3:
            raise ToolContractError("terminal_command_invalid", "terminal 支持受限 ls、grep/rg 固定文本检索和角色 Skill 命令；文件内容请用 read_file。" + guidance)
        interpreter = Path(argv[0]).name
        if interpreter not in {"python", "python3", Path(sys.executable).name}:
            raise ToolContractError("terminal_interpreter_denied", "脚本只允许当前 Python 解释器；另支持内置 ls、grep/rg，不支持任意 shell。" + guidance)
        scripts = {f"skills/{self.snapshot.name}/scripts/{entry}": script_role
                   for script_role, entry in role_script_entries(normalized_role).items()}
        if argv[1] not in scripts:
            raise ToolContractError(
                "terminal_role_denied", f"角色 {normalized_role!r} 不能执行 {argv[1]!r}"
            )
        normalized_role = scripts[argv[1]]
        expected = argv[1]
        action = argv[2]
        if normalized_role == "orchestrator" and action in {"audit", "scaffold", "sync-speech", "finalize"}:
            raise ToolContractError(
                "terminal_role_denied",
                "此命令属于 Harness 后台生命周期或诊断，不是 Agent 创作入口。"
                "继续页面工作及必要的像素检查；工作完成后自然结束，由后台执行机械验收和交付。",
            )
        resolved_script = str(self.snapshot.resolve_skill_path(expected))
        if argv[2:] == ["--help"]:
            return [sys.executable, resolved_script, "--help"], normalized_role, "help", None
        module = load_skill_module(self.snapshot.target, self.snapshot.tree_sha256, "cli")
        parser = (module.build_parser(normalized_role, authoring_only=True)
                  if normalized_role == "orchestrator" else module.build_parser(normalized_role))

        def invalid(detail):
            raise ToolContractError("terminal_argument_invalid", f"{detail}。使用 python {expected} --help 核对参数。")

        def silence(current):
            current._print_message = lambda *_args, **_kwargs: None
            current.error = invalid
            for action in current._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for child in action.choices.values():
                        silence(child)

        silence(parser)
        try:
            request = parser.parse_args(argv[2:])
        except SystemExit as exc:
            if exc.code == 0:
                return [sys.executable, resolved_script, *argv[2:]], normalized_role, "help", None
            raise ToolContractError("terminal_argument_invalid", f"命令参数不合法；使用 python {expected} {action} --help") from exc
        root = Path(request.root).expanduser()
        selected_root = (self.workspace.paths.root / root).resolve() if not root.is_absolute() else root.resolve()
        if selected_root != self.workspace.paths.root:
            raise ToolContractError(
                "terminal_root_denied", "Skill 命令只能访问当前工作区；不会将其他路径改写为当前路径。",
            )
        argv[0] = sys.executable
        argv[1] = resolved_script
        return argv, normalized_role, action, request

    @staticmethod
    def _head_limit(arguments: Sequence[str]) -> int:
        if not arguments:
            return 10
        count = (arguments[1] if len(arguments) == 2 and arguments[0] == "-n" else
                 arguments[0][2:] if len(arguments) == 1 and arguments[0].startswith("-n") else
                 arguments[0][1:] if len(arguments) == 1 and arguments[0].startswith("-") else "")
        if not count.isascii() or not count.isdigit() or len(count) > 9 or int(count) < 1:
            raise ToolContractError("terminal_argument_invalid", "仅支持 head、head -N 或 head -n N，N 必须为正整数。")
        return min(200, int(count))

    @staticmethod
    def _parse_listing(command: str) -> argparse.Namespace:
        """先校验完整只读组合；保留引号/转义边界，不把操作符误当路径。"""
        def invalid(detail):
            raise ToolContractError("terminal_argument_invalid",
                f"{detail}。支持 ls [-al] [相对目录 ...]，可用 ; 连接 ls、2>/dev/null 和末尾 | head -N；不执行 shell。")

        tokens = list(re.finditer(r'''(?:[^\s'"\\;&|<>()`]|\\[\s\S]|"(?:\\[\s\S]|[^"\\])*"|'[^']*')+|[;&|<>]+|[^\s]|\n''', command))
        parser = argparse.ArgumentParser(prog="ls", add_help=False, allow_abbrev=False)
        parser.add_argument("-a", action="store_true")
        parser.add_argument("-l", action="store_true")
        parser.add_argument("paths", nargs="*")
        parser.error = invalid
        commands, words, head, suppress_stderr = [], [], None, False
        index = 0
        while index <= len(tokens):
            raw = tokens[index].group() if index < len(tokens) else ";"
            if raw == ";":
                if not words or words[0] != "ls":
                    invalid("组合中的每条命令都必须是 ls")
                request = parser.parse_intermixed_args(words[1:])
                request.paths = request.paths or ["."]
                request.limit = 200 if head is None else ProductionToolServices._head_limit(head)
                request.suppress_stderr = suppress_stderr
                commands.append(request)
                if sum(len(row.paths) for row in commands) > 100:
                    invalid("一次最多指定 100 个目录")
                words, head, suppress_stderr = [], None, False
                if index >= len(tokens) - 1:
                    break
            elif (raw == "2" and index + 2 < len(tokens) and tokens[index + 1].group() == ">"
                  and tokens[index].end() == tokens[index + 1].start()):
                if suppress_stderr or shlex.split(tokens[index + 2].group()) != ["/dev/null"] or head is not None:
                    invalid("仅允许 ls 的 2>/dev/null，不支持其他重定向")
                suppress_stderr = True
                index += 2
            elif raw == "|" and head is None and index + 1 < len(tokens) and tokens[index + 1].group() == "head":
                head = []
                index += 1
            elif all(char in ";|&<>" for char in raw) or raw in {"(", ")", "`", "\n"}:
                invalid("仅允许 ls 之间的 ; 和末尾 head 截断")
            else:
                word = shlex.split(raw)
                if len(word) != 1:
                    invalid("目录参数无法解析")
                (words if head is None else head).append(word[0])
            index += 1
        return argparse.Namespace(commands=commands)

    def _list_directories(self, context: ToolContext, request: argparse.Namespace) -> dict[str, object]:
        """一笔事务共享输出预算；失败目录不掩盖已成功读取的目录。"""
        results = []
        remaining = 200
        for number, command in enumerate(request.commands, 1):
            command_remaining = min(remaining, command.limit)
            for path in command.paths:
                try:
                    result = self._list_directory(context, argparse.Namespace(path=path, a=command.a, l=command.l),
                                                  limit=command_remaining)
                    remaining -= result["returned_count"]
                    command_remaining -= result["returned_count"]
                except (ToolContractError, ToolExecutionError) as exc:
                    result = {"path": path, "returncode": 1, "error_code": exc.code, "error": str(exc),
                              "stdout": "", "returned_count": 0, "truncated": False}
                if len(request.commands) > 1:
                    result["command_index"] = number
                if command.suppress_stderr:
                    result["stderr_suppressed"] = True
                results.append(result)
        failures = [row for row in results if row["returncode"]]
        output = {"returncode": int(bool(failures)), "directories": results,
                  "returned_count": 200 - remaining, "truncated": any(row["truncated"] for row in results),
                  "stdout": "\n\n".join(
                      (json.dumps(row["path"], ensure_ascii=False) + ":\n" if len(results) > 1 else "")
                      + (("" if row.get("stderr_suppressed") else f"{row['error_code']}: {row['error']}") if row["returncode"] else row["stdout"])
                      for row in results)}
        if len(results) == 1:
            output["path"] = results[0]["path"]
        if failures:
            code = failures[0]["error_code"] if len(results) == 1 else "terminal_list_partial_failure"
            raise ToolExecutionError(code, "部分或全部目录无法读取", diagnostics=output)
        return output

    @staticmethod
    def _parse_search(command: str) -> argparse.Namespace:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        argv = list(lexer)

        def invalid(detail):
            raise ToolContractError("terminal_argument_invalid", f"{detail}。支持 grep/rg [-rniF] [--max-results N] 固定文本 [相对路径 ...]；仅允许末尾 | head -N 或 | head -n N，不执行 shell/正则。")

        limit = 200
        # 仅识别一个截断后缀；绝不把用户文本交给 shell 或 head 进程。
        for size in (4, 3, 2):
            tail = argv[-size:]
            if len(tail) == size and tail[:2] == ["|", "head"]:
                limit = ProductionToolServices._head_limit(tail[2:])
                argv = argv[:-size]
                break
        parser = argparse.ArgumentParser(prog=argv[0], add_help=False, allow_abbrev=False)
        parser.add_argument("-r", "--recursive", action="store_true", default=argv[0] == "rg")
        parser.add_argument("-n", "--line-number", action="store_true")
        parser.add_argument("-i", "--ignore-case", action="store_true")
        parser.add_argument("-F", "--fixed-strings", action="store_true")
        parser.add_argument("--max-results", type=int, default=200)
        parser.add_argument("pattern")
        parser.add_argument("paths", nargs="*")
        parser.error = invalid
        request = parser.parse_intermixed_args(argv[1:])
        if not request.pattern or "\x00" in request.pattern:
            invalid("搜索文本不能为空或包含 NUL")
        if not 1 <= request.max_results <= 200:
            invalid("--max-results 范围为 1–200")
        if any(token in {";", "|", "||", "&&", "&", "<", ">", ">>", "<<"} for token in request.paths):
            invalid("不支持其他管道、重定向或组合命令")
        request.max_results = min(limit, request.max_results)
        request.paths = request.paths or ["."]
        if len(request.paths) > 100:
            invalid("一次最多指定 100 个路径")
        return request

    def _search_files(self, context: ToolContext, request: argparse.Namespace) -> dict[str, object]:
        """有界 UTF-8 字面检索；目录、文件和固定 Skill 全部沿用现有读域。"""
        matches, errors, skipped, roots = [], [], [], []
        visited, seen, scanned, read_bytes, output_bytes = 0, set(), 0, 0, 0
        reason = ""
        needle = request.pattern.casefold() if request.ignore_case else request.pattern
        deadline = time.monotonic() + 10
        skill_root = f"skills/{self.snapshot.name}"
        for path in request.paths:
            try:
                relative = normalize_workspace_path(path)
                if not context.may_read(relative) and not any(
                    relative == "." or scope.path.startswith(relative + "/") for scope in context.read_scopes
                ):
                    raise ToolContractError("read_scope_denied", f"Owner 无权检索路径 {relative!r}")
                roots.append(relative)
            except ToolContractError as exc:
                errors.append({"path": path, "error_code": exc.code, "error": str(exc)})
        pending = list(reversed(roots))
        while pending:
            if visited >= 1000 or read_bytes >= 16 * 1024 * 1024 or time.monotonic() >= deadline:
                reason = "scan_budget"
                break
            relative = pending.pop()
            if relative in seen:
                continue
            seen.add(relative)
            visited += 1
            try:
                directory = relative in {".", "skills", skill_root}
                data = None
                if not directory:
                    with self._open_read_path(relative) as fd:
                        info = os.fstat(fd)
                        directory = stat.S_ISDIR(info.st_mode)
                        if not directory:
                            if not context.may_read(relative):
                                raise ToolContractError("read_scope_denied", f"Owner 无权读取 {relative!r}")
                            if not stat.S_ISREG(info.st_mode):
                                raise ToolContractError("not_a_regular_file", f"检索不读取特殊文件：{relative!r}")
                            available = min(2 * 1024 * 1024, 16 * 1024 * 1024 - read_bytes)
                            if info.st_size > available:
                                skipped.append({"path": relative, "reason": "file_byte_limit"})
                                continue
                            with os.fdopen(os.dup(fd), "rb") as handle:
                                data = handle.read(available + 1)
                            read_bytes += len(data)
                            if len(data) > available:
                                skipped.append({"path": relative, "reason": "file_byte_limit"})
                                continue
                if directory:
                    if not request.recursive:
                        raise ToolContractError("search_directory_requires_recursive", f"检索目录 {relative!r} 请加 -r 或使用 rg")
                    entries = self._directory_entries(context, relative)
                    pending.extend(path for path, _ in sorted(entries, reverse=True))
                    continue
                try:
                    text = data.decode("utf-8")
                    if "\x00" in text:
                        raise UnicodeError("binary content")
                except UnicodeError:
                    skipped.append({"path": relative, "reason": "not_utf8_text"})
                    continue
                scanned += 1
                for number, line in enumerate(text.splitlines(), 1):
                    if needle not in (line.casefold() if request.ignore_case else line):
                        continue
                    match = {"path": relative, "line": number, "text": line[:1000], "line_truncated": len(line) > 1000}
                    size = len(json.dumps(match, ensure_ascii=False).encode("utf-8"))
                    if len(matches) >= request.max_results or output_bytes + size > 64 * 1024:
                        reason = "result_limit"
                        break
                    matches.append(match)
                    output_bytes += size
                if reason:
                    break
            except (ToolContractError, ToolExecutionError) as exc:
                errors.append({"path": relative, "error_code": exc.code, "error": str(exc)})
            except OSError as exc:
                code = {errno.ENOENT: "path_not_found", errno.ENOTDIR: "not_a_directory",
                        errno.ELOOP: "symlink_forbidden", errno.EACCES: "read_permission_denied",
                        errno.EPERM: "read_permission_denied"}.get(exc.errno, "read_io_error")
                errors.append({"path": relative, "error_code": code, "error": f"无法检索 {relative!r}：{exc.strerror}"})
        result = {"returncode": int(bool(errors)), "mode": "fixed_text", "pattern": request.pattern,
                  "matches": matches, "returned_count": len(matches), "max_results": request.max_results,
                  "files_scanned": scanned, "bytes_read": read_bytes, "errors": errors, "skipped": skipped,
                  "truncated": bool(reason), "truncation_reason": reason, "search_complete": not reason and not errors and not skipped,
                  "stdout": "\n".join(f"{row['path']}:" + (f"{row['line']}:" if request.line_number else "")
                                      + row["text"] for row in matches),
                  "note": "仅检索授权 UTF-8 文本，固定文本匹配，不执行正则/系统命令。默认跳过隐藏子项；最多 200 条、1000 个路径、单文件 2 MiB、总读取 16 MiB、10 秒；跳过项/截断不代表已读完，请缩小范围或用 read_file 续读。"}
        if errors:
            raise ToolExecutionError(errors[0]["error_code"] if len(request.paths) == 1 and len(errors) == 1 else "terminal_search_partial_failure",
                                     "部分或全部路径无法检索", diagnostics=result)
        return result

    @contextmanager
    def _open_read_path(self, relative: str, *, directory: bool = False):
        """固定根下逐层打开，不跟随链接；文件句柄避免检查后替换路径逃逸。"""
        skill_root = f"skills/{self.snapshot.name}"
        if relative == skill_root or relative.startswith(skill_root + "/"):
            base, parts = self.snapshot.target, Path(relative).relative_to(skill_root).parts
        else:
            base, parts = self.workspace.paths.root, Path(relative).parts
        fd = None
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(base, flags | os.O_DIRECTORY)
            for index, part in enumerate(parts):
                try:
                    child = os.open(part, flags | (os.O_DIRECTORY if directory or index < len(parts) - 1 else 0), dir_fd=fd)
                except OSError as exc:
                    if exc.errno in {errno.ELOOP, errno.ENOTDIR} and stat.S_ISLNK(
                        os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode
                    ):
                        raise ToolContractError("symlink_forbidden", f"路径 {relative!r} 含软链接；只读工具不跟随软链接") from exc
                    raise
                os.close(fd)
                fd = child
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    def _directory_entries(self, context: ToolContext, relative: str, *, hidden: bool = False):
        """目录查看和检索共用读域；祖先可导航，不暴露未授权兄弟项。"""
        skill_root = f"skills/{self.snapshot.name}"

        def visible(path):
            return context.may_read(path) or any(
                path == "." or scope.path.startswith(path + "/") for scope in context.read_scopes
            )

        if not visible(relative):
            raise ToolContractError("read_scope_denied", f"Owner 无权查看目录 {relative!r}")
        rows = []
        if relative == "skills":
            if visible(skill_root):
                rows.append((skill_root, None))
        else:
            try:
                with self._open_read_path(relative, directory=True) as fd, os.scandir(fd) as entries:
                    for entry in entries:
                        if not hidden and entry.name.startswith("."):
                            continue
                        logical = (Path(relative) / entry.name).as_posix()
                        if logical == "skills" or not visible(logical):
                            continue
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and context.may_read(logical)):
                            rows.append((logical, info))
            except OSError as exc:
                code = {errno.ENOENT: "path_not_found", errno.ENOTDIR: "not_a_directory",
                        errno.ELOOP: "symlink_forbidden", errno.EACCES: "directory_permission_denied",
                        errno.EPERM: "directory_permission_denied"}.get(exc.errno, "directory_io_error")
                raise ToolExecutionError(code,
                    f"无法列出 {relative!r}：{exc.strerror}。ls 仅查看存在的真实目录，不跟随软链接；读取文件请用 read_file。") from exc
            if relative == "." and visible(skill_root):
                rows.append(("skills", None))
        return rows

    def _list_directory(self, context: ToolContext, request: argparse.Namespace, *, limit: int = 200) -> dict[str, object]:
        relative = normalize_workspace_path(request.path)
        rows = self._directory_entries(context, relative, hidden=request.a)
        lines = []
        for logical, info in sorted(rows)[:limit]:
            directory = info is None or stat.S_ISDIR(info.st_mode)
            label = json.dumps(logical + ("/" if directory else ""), ensure_ascii=False)
            if request.l:
                size = info.st_size if not directory else "-"
                modified = (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(info.st_mtime))
                            if info is not None and context.may_read(logical) else "-")
                label = f"{'dir' if directory else 'file'}\t{size}\t{modified}\t{label}"
            lines.append(label)
        truncated = len(rows) > limit
        if truncated:
            lines.append(f"目录结果已截断（当前显示上限 {limit} 项，整次调用最多 200 项），请指定更具体的子目录。")
        return {"returncode": 0, "stdout": "\n".join(lines), "path": relative,
                "returned_count": min(len(rows), limit), "truncated": truncated}

    def _command_environment(self) -> dict[str, str]:
        """生成不含旧前缀且把临时/锁目录限制在工作区的脚本环境。"""
        env = _render_environment(
            self.workspace.paths.root,
            self.settings,
            self.environment,
        )
        env["ASSET_DOWNLOAD_WORKERS"] = str(
            _positive_int(self.environment, "ASSET_DOWNLOAD_WORKERS", 1)
        )
        return env

    def _register_scaffolded_pages(self) -> None:
        """把 Skill scaffold 产生的 HTML 记录为 scaffolded 而非 authored。"""
        present: list[int] = []
        for path in sorted(self.workspace.paths.slides.glob("slide_*.html")):
            match = re.fullmatch(r"slide_(\d+)\.html", path.name)
            if match is None:
                continue
            page = int(match.group(1))
            present.append(page)
            self.workspace.register_pages([page])
            self.workspace.register_scaffold(page)

    def _commit_slide_if_needed(self, context: ToolContext, relative: str) -> None:
        """把当前 Owner 写入的 canonical Slide HTML 自动提交，无需 Agent 额外工具。"""
        match = re.fullmatch(r"slides/slide_(\d+)\.html", relative)
        if match is None:
            return
        page = int(match.group(1))
        if not self.may_write(context, relative):
            raise ToolContractError(
                "slide_page_scope_denied", f"Owner 不拥有页面 {page}"
            )
        self.workspace.register_pages([page])
        artifact_id = f"page-html-{page:02d}"
        try:
            self.artifacts.declare(
                artifact_id=artifact_id,
                kind="page_html",
                path=relative,
                owner_id=context.owner_id,
                page=page,
            )
            self.artifacts.commit(
                owner_id=context.owner_id,
                artifact_ids=[artifact_id],
                source_hashes={},
            )
        except (ArtifactStateError, WorkspaceStateError) as exc:
            raise ToolExecutionError("slide_artifact_commit_failed", str(exc)) from exc

    def _ingest_image_receipts(self, context: ToolContext, stdout: str, leases: dict | None = None) -> list[dict]:
        """只接收固定脚本的显式登记，不从可读目录反推机器状态。"""
        results = []
        for line in stdout.split("\n"):
            if not line.startswith("MURAL_ASSET "):
                continue
            try:
                entry = json.loads(line.removeprefix("MURAL_ASSET "))
                path = self.workspace.paths.relative(self.workspace.paths.resolve(entry["path"]))
                with self.workspace._lock:
                    if not self.may_write(context, path):
                        raise ToolContractError("write_scope_denied", f"Image 不能登记 {path}")
                    asset_id = entry["artifact_id"]
                    if leases is not None and (asset_id not in leases or self.artifacts.artifact(asset_id).path != path):
                        raise ToolContractError("asset_receipt_mismatch", "素材回执不对应本次预留的 ID 和路径")
                    reviewed_sha256 = str(entry.get("reviewed_sha256") or "")
                    if reviewed_sha256 and not any(
                        source.get("owner_id") == context.owner_id and source.get("path") == path
                        and source.get("sha256") == reviewed_sha256
                        for source in context.metadata.get("consumed_visual_sources", [])
                    ):
                        raise ToolContractError("image_review_required", (
                            "提前交接前须用 inspect(path=图片路径) 打开当前图片，并在收到图片后的下一轮确认可用，再 register --reviewed。"
                            "仅登记、同轮看图或旧 hash 不算已看过；不需要重新生成图片。"))
                    record = self.artifacts.record_image_result(
                        artifact_id=asset_id, owner_id=context.owner_id, path=path,
                        status=entry["status"], source_url=str(entry.get("source_url") or ""),
                        reason=str(entry.get("reason") or ""), image_metadata=entry["image_metadata"],
                        reviewed_sha256=reviewed_sha256,
                        transaction_id=leases[asset_id] if leases is not None else "")
                    if leases is not None:
                        del leases[asset_id]
                    results.append(record.to_dict())
                # 不持有工作区锁进入调度锁；登记事务已经提交，Image 仍可继续处理其他素材。
                sink = getattr(self, "image_handoff_sink", None)
                if reviewed_sha256 and context.role == "image" and sink is not None:
                    sink(record.to_dict())
            except ToolContractError:
                raise
            except (ArtifactStateError, KeyError, ValueError, TypeError) as exc:
                raise ToolExecutionError("image_registration_failed", str(exc)) from exc
        return results

    def _ensure_external_allowed(self, context: ToolContext) -> None:
        """External tools stay available; source limits are task instructions."""
        return None

    @staticmethod
    def _web_cache_key(kind: str, arguments: Mapping[str, object]) -> str:
        return json.dumps([kind, dict(arguments)], sort_keys=True, ensure_ascii=False)

    @contextmanager
    def _research_web_progress_state(self, owner_id: str) -> Iterator[dict[str, object]]:
        """Reserve web calls durably before I/O; restore sets without exposing the ledger."""
        workspace = getattr(self, "workspace", None)
        lock = getattr(self, "_search_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._search_lock = lock
        progress = getattr(self, "_research_web_progress", None)
        if progress is None:
            progress = {}
            self._research_web_progress = progress
        with workspace._lock if workspace is not None else lock:
            durable = workspace._read_state() if workspace is not None else None
            if durable is not None:
                if "research_web_progress" not in durable:
                    durable["research_web_progress"] = self._rebuild_research_web_progress()
                    workspace._write_state(durable)
                progress = durable["research_web_progress"]
            state = {"calls": 0, "seen_sources": set(), "successful_extracts": set(),
                     "no_new_source_streak": 0, "failures": 0,
                     **copy.deepcopy(progress.get(owner_id, {}))}
            for key in ("seen_sources", "successful_extracts"):
                state[key] = set(state.get(key) or ())
            yield state
            progress[owner_id] = {
                key: sorted(value) if isinstance(value, set) else value
                for key, value in state.items()
            }
            if durable is not None:
                workspace._write_state(durable)

    def _rebuild_research_web_progress(self) -> dict[str, dict[str, object]]:
        """Bootstrap older workspaces once from completed, uniquely paired web results."""
        from .live_trace import iter_jsonl_events

        progress: dict[str, dict[str, object]] = {}
        seen: set[tuple[str, str]] = set()
        for event in iter_jsonl_events(self.workspace.paths.trace / "live-events.jsonl"):
            payload = event.get("payload")
            if (event.get("event_type") != "tool_result" or event.get("role") != "research"
                    or not isinstance(payload, Mapping) or payload.get("name") not in {"web_search", "web_extract"}):
                continue
            error = str(payload.get("error_code") or "")
            if not payload.get("ok") and (not error.startswith("web_") or error == "research_web_budget_reached"):
                continue
            owner = str(event.get("agent_id") or "")
            tool_id = str(payload.get("tool_use_id") or "")
            if not owner or not tool_id or (owner, tool_id) in seen:
                continue
            seen.add((owner, tool_id))
            state = progress.setdefault(owner, {"calls": 0, "failures": 0,
                                                "seen_sources": [], "successful_extracts": []})
            state["calls"] += 1
            if not payload.get("ok"):
                state["failures"] += 1
                continue
            result = payload.get("result")
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except ValueError:
                    continue
            if not isinstance(result, Mapping):
                continue
            if payload["name"] == "web_search":
                rows = result.get("results") or []
                sources = [str(row.get("url") or row.get("source") or "").split("#", 1)[0]
                           for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
            else:
                sources = [str(result.get("effective_url") or result.get("url") or "").split("#", 1)[0]]
                state["successful_extracts"] = sorted(set(state["successful_extracts"]) | {url for url in sources if url})
            state["seen_sources"] = sorted(set(state["seen_sources"]) | {url for url in sources if url})
        return progress

    def _research_web_begin(self, context: ToolContext, kind: str) -> None:
        """Bound one narrow Research Owner without constraining other roles."""
        if context.role != "research":
            return
        with self._research_web_progress_state(context.owner_id) as state:
            if int(state["calls"]) >= RESEARCH_WEB_MAX_CALLS:
                raise ToolExecutionError(
                    "research_web_budget_reached",
                    (
                        "当前 Research Owner 已达到有界联网检索预算。不要改写近义词继续搜索；"
                        "请用已有可访问证据写入 research 任务稿并交接。若仍有会改变结论的明确缺口，"
                        "由 Orchestrator 另行委派一个更窄的 Research 任务。"
                    ),
                    diagnostics={
                        "status": "evidence_budget_reached",
                        "retry_recommended": False,
                        "recommended_action": "write the current research handoff or delegate one narrower unresolved question",
                    },
                )
            state["calls"] = int(state["calls"]) + 1
            state["last_kind"] = kind

    def _research_web_success(
        self,
        context: ToolContext,
        kind: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        """Expose only an actionable convergence cue when evidence has stopped growing."""
        if context.role != "research":
            return {}
        candidates: set[str] = set()
        if kind == "web_search":
            rows = payload.get("results")
            if isinstance(rows, list):
                candidates = {
                    str(row.get("url") or row.get("source") or "").split("#", 1)[0]
                    for row in rows
                    if isinstance(row, Mapping) and (row.get("url") or row.get("source"))
                }
        else:
            source = str(payload.get("effective_url") or payload.get("url") or "").split("#", 1)[0]
            if source:
                candidates.add(source)
        with self._research_web_progress_state(context.owner_id) as state:
            seen = state.setdefault("seen_sources", set())
            new_sources = candidates - seen
            seen.update(candidates)
            if new_sources:
                state["no_new_source_streak"] = 0
            else:
                state["no_new_source_streak"] = int(state.get("no_new_source_streak") or 0) + 1
            if kind == "web_extract" and candidates:
                state.setdefault("successful_extracts", set()).update(candidates)
            successful_extracts = len(state.setdefault("successful_extracts", set()))
            calls = int(state.get("calls") or 0)
            no_new = int(state.get("no_new_source_streak") or 0)
            reasons = []
            if successful_extracts >= RESEARCH_SUCCESSFUL_EXTRACT_TARGET:
                reasons.append("multiple accessible source texts are already available")
            if no_new >= RESEARCH_NO_NEW_SOURCE_LIMIT:
                reasons.append("recent web calls added no new source")
            if calls >= RESEARCH_WEB_SOFT_CALLS:
                reasons.append("the focused web pass is already substantial")
        if not reasons:
            return {}
        return {
            "research_convergence": {
                "status": "sufficient_for_handoff",
                "reason": "; ".join(reasons),
                "next_action": (
                    "Stop broad browsing and write the research task file from current evidence. "
                    "Continue only for one named conflict or missing fact that would change the deck."
                ),
            }
        }

    def _research_web_failure(self, context: ToolContext) -> dict[str, object]:
        if context.role != "research":
            return {}
        with self._research_web_progress_state(context.owner_id) as state:
            state["failures"] = int(state.get("failures") or 0) + 1
            state["no_new_source_streak"] = int(state.get("no_new_source_streak") or 0) + 1
            stalled = (
                int(state["no_new_source_streak"]) >= RESEARCH_NO_NEW_SOURCE_LIMIT
                or int(state.get("calls") or 0) >= RESEARCH_WEB_SOFT_CALLS
            )
        if not stalled:
            return {}
        return {
            "research_convergence": {
                "status": "source_access_stalled",
                "next_action": (
                    "Do not retry denied or unusable sources. Use accessible evidence/search summaries, "
                    "write the current research task file, and state the remaining boundary."
                ),
            }
        }

    def _web_cached(self, key: str) -> dict[str, object] | None:
        with self._search_lock:
            result = self._web_results.get(key)
            return {**copy.deepcopy(result), "cached": True} if result is not None else None

    def _web_store(self, key: str, result: dict[str, object]) -> dict[str, object]:
        with self._search_lock:
            self._web_results[key] = copy.deepcopy(result)
        return result

    def _web_extract_failure_state(self) -> tuple[threading.Lock, dict[str, dict[str, object]]]:
        """Lazily support lightweight test hosts while sharing failures in production."""
        lock = getattr(self, "_search_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._search_lock = lock
        failures = getattr(self, "_web_extract_failures", None)
        if failures is None:
            failures = {}
            self._web_extract_failures = failures
        return lock, failures

    @staticmethod
    def _web_extract_failure_keys(url: str, render_js: bool) -> tuple[str, str]:
        return f"url:{url}", f"mode:{int(render_js)}:{url}"

    def _cached_web_extract_failure(
        self, url: str, render_js: bool
    ) -> dict[str, object] | None:
        lock, failures = self._web_extract_failure_state()
        general, mode = self._web_extract_failure_keys(url, render_js)
        host = (urlsplit(url).hostname or "").lower()
        with lock:
            value = failures.get(general) or failures.get(mode) or failures.get(f"host:{host}")
            return copy.deepcopy(value) if value is not None else None

    def _remember_web_extract_failure(
        self,
        url: str,
        render_js: bool,
        code: str,
        detail: str,
    ) -> None:
        """Share only deterministic source failures; transient failures remain retryable once."""
        status = re.fullmatch(r"web_extract_http_(\d{3})", code)
        http_status = int(status.group(1)) if status else None
        general_codes = {401, 403, 404, 405, 410, 451}
        mode_codes = {"web_extract_empty", "web_extract_pdf", "web_extract_too_large"}
        if http_status not in general_codes and code not in mode_codes:
            return
        general, mode = self._web_extract_failure_keys(url, render_js)
        key = general if http_status in general_codes else mode
        receipt = _web_extract_error_receipt(code, detail)
        lock, failures = self._web_extract_failure_state()
        with lock:
            failures[key] = receipt
            if http_status in {401, 403, 451}:
                denials = getattr(self, "_web_extract_host_denials", None)
                if denials is None:
                    denials = {}
                    self._web_extract_host_denials = denials
                host = (urlsplit(url).hostname or "").lower()
                denied_urls = denials.setdefault(host, set())
                denied_urls.add(url)
                if host and len(denied_urls) >= 2:
                    failures[f"host:{host}"] = _web_extract_error_receipt(
                        code,
                        (
                            "Multiple pages on this host denied scripted access in this run. "
                            "Do not probe more URLs on the same host; use another accessible source."
                        ),
                    )


def read_scopes_for_role(
    role: str, *, skill_name: str, pages: Sequence[int] = (), attachment_paths: Sequence[str] = (),
    reference_pages: Sequence[int] = (),
) -> tuple[WriteScope, ...]:
    """固定 Skill 知识可只读；工作区数据仍按职责和所属页隔离。"""
    normalized = str(role).strip().lower()
    skill_context = (
        WriteScope("config.json", recursive=False),
        WriteScope(f"skills/{skill_name}/references", recursive=True),
        WriteScope(f"skills/{skill_name}/roles", recursive=True),
    )
    scripts = tuple(WriteScope(f"skills/{skill_name}/scripts/{script}", recursive=False)
                    for script in role_script_entries(normalized).values())
    attachments = tuple(WriteScope(path, recursive=False) for path in attachment_paths if path) + tuple(
        WriteScope(f"inputs/extracted/{match.group(1)}", recursive=True)
        for path in attachment_paths
        if (match := re.fullmatch(r"inputs/originals/(att-[0-9a-f]{12})/[^/]+", path))
    )
    if normalized == "orchestrator":
        return (
            *attachments,
            *scripts,
            WriteScope(f"skills/{skill_name}/SKILL.md", recursive=False),
            *skill_context,
            WriteScope("research", recursive=True),
            WriteScope("deck.md", recursive=False),
            WriteScope("briefs", recursive=True),
            WriteScope("base.css", recursive=False),
            WriteScope("knowledge.md", recursive=False),
            WriteScope("slides", recursive=True),
            WriteScope("assets", recursive=True),
            WriteScope("renders", recursive=True),
            WriteScope("font-intent.json", recursive=False),
            WriteScope("speech.md", recursive=False),
            WriteScope("result.json", recursive=False),
            WriteScope("revision-operations.json", recursive=False),
            WriteScope("sources/web", recursive=True),
        )
    if normalized == "research":
        return (
            *attachments,
            *skill_context,
            WriteScope("research", recursive=True),
            WriteScope("knowledge.md", recursive=False),
            WriteScope("sources/web", recursive=True),
        )
    if normalized == "image":
        return (
            *scripts,
            *skill_context,
            WriteScope("deck.md", recursive=False),
            WriteScope("base.css", recursive=False),
            WriteScope("knowledge.md", recursive=False),
            WriteScope("assets", recursive=True),
            *attachments,
        )
    if normalized == "slide":
        scopes = [
            *scripts,
            *skill_context,
            WriteScope("deck.md", recursive=False),
            WriteScope("base.css", recursive=False),
            WriteScope("knowledge.md", recursive=False),
            WriteScope("assets", recursive=True),
        ]
        for page in pages:
            scopes.append(WriteScope(f"briefs/slide_{int(page):02d}.md", recursive=False))
            scopes.append(WriteScope(f"slides/slide_{int(page):02d}.html", recursive=False))
            scopes.append(WriteScope(f"renders/slide_{int(page):02d}.png", recursive=False))
        scopes.extend(WriteScope(f"slides/slide_{page:02d}.html", recursive=False)
                      for page in reference_pages if page not in pages)
        return tuple(scopes)
    return ()


def write_scopes_for_role(
    role: str, *, pages: Sequence[int] = (), output_path: str = ""
) -> tuple[WriteScope, ...]:
    """按角色和真实 ownership 生成写域。"""
    normalized = str(role).strip().lower()
    if normalized == "orchestrator":
        return (
            WriteScope("deck.md", recursive=False),
            WriteScope("config.json", recursive=False),
            WriteScope("knowledge.md", recursive=False),
            WriteScope("assets", recursive=True),
            WriteScope("briefs", recursive=True),
            WriteScope("base.css", recursive=False),
            WriteScope("speech.md", recursive=False),
            WriteScope("revision-operations.json", recursive=False),
            WriteScope("font-intent.json", recursive=False),
        )
    if normalized == "research":
        return (WriteScope(output_path, recursive=False),) if output_path else ()
    if normalized == "image":
        return (
            WriteScope("assets/catalog.md", recursive=False),
            WriteScope("assets", recursive=True),
        )
    if normalized == "slide":
        return tuple(
            WriteScope(f"slides/slide_{int(page):02d}.html", recursive=False)
            for page in pages
        )
    return ()


def _run_command(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_s: int,
    environment: Mapping[str, str],
    pass_fds: tuple[int, ...] = (),
    on_stdout_line=None,
) -> CommandResult:
    """不经过 shell 执行受控命令，并把非零退出转换为稳定异常。"""
    # 共享父目录即使为空，也可能已被另一任务选作 TMPDIR；只由子进程清理自己的临时文件。
    try:
        return run_command(argv, cwd=cwd, timeout_s=timeout_s, pass_fds=pass_fds,
                           environment={str(key): str(value) for key, value in environment.items()},
                           **({"on_stdout_line": on_stdout_line} if on_stdout_line is not None else {}))
    except CommandFailed as exc:
        result = exc.result
        payload = {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                   "diagnostics": result.diagnostics}
        if result.returncode == 2 and result.diagnostics["termination"] == "exited":
            body_error = next((line for line in result.stdout.splitlines()
                               if line.startswith("page_body_unfilled: ")), "")
            if body_error:
                raise ToolExecutionError("page_body_unfilled", body_error,
                    diagnostics={**payload, "error_code": "page_body_unfilled", "detail": body_error}) from exc
        raise ToolExecutionError(
            "handler_error" if result.diagnostics["termination"] in {"timeout", "spawn_failed"} else "skill_command_failed",
            str(exc), diagnostics=payload,
        ) from exc


def _render_environment(
    root: Path,
    settings: ProductionSettings,
    environment: Mapping[str, str],
) -> dict[str, str]:
    """构造 Renderer 与正式 Skill 命令共享的工作区内锁环境。"""
    env = dict(environment)
    env.pop("MURAL_RENDER_BROWSER_ENDPOINT", None)
    # 受控子进程仍需系统 PATH/HOME 才能发现 pyftsubset、libreoffice、curl 等工具，
    # 以及 HOME 下的字体目录。生产入口已带这些变量；缺失时从当前进程补齐，绝不泄露密钥。
    for name in ("PATH", "HOME", "LANG", "LC_ALL"):
        if not env.get(name):
            ambient = os.environ.get(name)
            if ambient:
                env[name] = ambient
    paths = WorkspacePaths.from_root(root)
    system_tmp = paths.runtime_tmp / "system"
    system_tmp.mkdir(parents=True, exist_ok=True)
    env["TMPDIR"] = str(system_tmp)
    env["TMP"] = str(system_tmp)
    env["TEMP"] = str(system_tmp)
    env["PPT_RENDER_LOCK_PATH"] = str(paths.state / "render.lock")
    env["PPT_RENDER_LOCK_DIR"] = str(paths.state / "render-slots")
    env["PPT_RENDER_CONCURRENCY"] = str(settings.render_concurrency)
    env["MURAL_WORKSPACE_LAYOUT_VERSION"] = str(paths.layout_version)
    return env


def _module_importable(name: str) -> bool:
    """Import a module so a missing shared library cannot false-pass."""
    try:
        importlib.import_module(name)
    except (ImportError, OSError):
        return False
    return True


def _same_interpreter(first: str, second: str) -> bool:
    """Compare executable identities while preserving virtualenv paths."""
    left = Path(first).expanduser()
    right = Path(second).expanduser()
    try:
        return left.samefile(right)
    except (FileNotFoundError, OSError):
        return left.absolute() == right.absolute()


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    """读取正整数环境配置。"""
    value = int(str(environment.get(name, default) or default).strip())
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _boolean(environment: Mapping[str, str], name: str, default: bool) -> bool:
    """读取常见布尔环境配置。"""
    raw = str(environment.get(name, "") or "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be boolean")


def _atomic_json_write(path: Path, value: Mapping[str, object]) -> None:
    """原子写入排序 JSON。"""
    _atomic_text_write(
        path, json.dumps(dict(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    )


def _atomic_text_write(path: Path, value: str) -> None:
    """原子写入共享可读 UTF-8 文本。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(str(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def _atomic_bytes_write(path: Path, payload: bytes) -> None:
    """原子写入共享可读二进制文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


__all__ = [
    "EnvironmentDoctor",
    "ProductionError",
    "ProductionSettings",
    "ProductionToolServices",
    "ResourceSlotTimeout",
    "ResourceSlots",
    "SkillRenderer",
    "SkillSnapshot",
    "read_scopes_for_role",
    "write_scopes_for_role",
]
