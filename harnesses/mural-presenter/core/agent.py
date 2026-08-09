#!/usr/bin/env python3
"""Mural Presenter 的 agent 运行时与委派适配层。

- `Agent`:单 agent 的状态 + 沙箱(safe/read_path/writable)+ 模型客户端 + 一个 Trace。
- `run_loop(agent)`:**纯** ReAct 循环(模型 → 工具 → 模型),模型完成角色合同后收尾；默认不按固定回合数截断，也不替模型伪造产物。
- `delegate_task(parent, …)`:并行起一批子 agent,各在独立上下文里干活,只把结构化交接返回父级。

模型只提交 `tasks[].goal`。Harness 从稳定前缀推导角色、轨迹名和内部工具白名单；
例如 Slide/Review 始终实际获得 Vision，模型不能自行扩张权限。

进程/线程模型:sample 之间 = 进程(distill_ppt.py 调度);一个 sample 内的子 agent = 线程
(每次 delegate_task 起一个**本地** ThreadPoolExecutor,用完即关 → 不跨 sample 串台)。
"""
import base64
import concurrent.futures as cf
import copy
import glob
import hashlib
import itertools
import json
import os
from pathlib import Path
import random
import re
import threading
import time

import anthropic

from . import _final_contract, stage_memory, tools
from . import nova_bridge
from .trace import Trace

# 子 agent 并发上限(所有 delegate_task 调用共用一个父级信号量)。
MAX_CONCURRENT_CHILDREN = int(os.environ.get("MAX_CONCURRENT_CHILDREN",
                              os.environ.get("SLIDE_CONCURRENCY", "4")))
# 同回合内**纯 IO 叶子工具**(无 agent 状态记账、落盘内容寻址不撞名)并行执行——主要打掉 image subagent
# 里多张 image_generate 的串行;单独限并发 TURN_TOOL_PARALLEL 防出图/搜索网关过载。可用 env 覆盖。
PARALLEL_LEAF_TOOLS = set(x for x in os.environ.get("PARALLEL_LEAF_TOOLS", "image_generate,web_search,web_extract").split(",") if x)
TURN_TOOL_PARALLEL = int(os.environ.get("TURN_TOOL_PARALLEL", "6"))
WORKER_TIMEOUT = tools.parse_optional_timeout(os.environ.get("WORKER_TIMEOUT"), None)
IMAGE_WORKER_TIMEOUT = tools.parse_optional_timeout(
    os.environ.get("IMAGE_WORKER_TIMEOUT"), None
)
MATERIAL_WORKER_TIMEOUT = tools.parse_optional_timeout(
    os.environ.get("MATERIAL_WORKER_TIMEOUT"), None
)
# 正常运行不设置 Harness 级子任务墙钟。部署方若因资源治理显式设置 base，
# 才按页数扩展；cap 同样是可选运维保护，不是 Skill 的完成预算。
SLIDE_WORKER_TIMEOUT_BASE = tools.parse_optional_timeout(
    os.environ.get("SLIDE_WORKER_TIMEOUT_BASE"), None
)
SLIDE_WORKER_TIMEOUT_PER_EXTRA_PAGE = int(os.environ.get("SLIDE_WORKER_TIMEOUT_PER_EXTRA_PAGE", "180"))
SLIDE_WORKER_TIMEOUT_CAP = tools.parse_optional_timeout(
    os.environ.get("SLIDE_WORKER_TIMEOUT_CAP"), None
)
REVIEW_WORKER_TIMEOUT_BASE = tools.parse_optional_timeout(
    os.environ.get("REVIEW_WORKER_TIMEOUT_BASE"), None
)
REVIEW_WORKER_TIMEOUT_PER_PAGE = int(os.environ.get("REVIEW_WORKER_TIMEOUT_PER_PAGE", "45"))# 每页 +45s(逐页看图)
REVIEW_WORKER_TIMEOUT_CAP = tools.parse_optional_timeout(
    os.environ.get("REVIEW_WORKER_TIMEOUT_CAP"), None
)
MATERIAL_WORKER_TIMEOUT_PER_PAGE = int(os.environ.get("MATERIAL_WORKER_TIMEOUT_PER_PAGE", "30"))  # material 大 deck 每页 +30s
MATERIAL_WORKER_TIMEOUT_CAP = tools.parse_optional_timeout(
    os.environ.get("MATERIAL_WORKER_TIMEOUT_CAP"), None
)

# 活跃上下文只保留近期工具细节。完整原始消息仍在 Trace 中，这里只压缩
# 已经被后续回合消费的早期文本/工具结果，避免长 Research、Material、Review 越跑越慢。
HISTORY_COMPACT_AFTER_CHARS = int(os.environ.get("HISTORY_COMPACT_AFTER_CHARS", "120000"))
HISTORY_KEEP_RECENT_MESSAGES = int(os.environ.get("HISTORY_KEEP_RECENT_MESSAGES", "10"))
HISTORY_TOOL_RESULT_MAX_CHARS = int(os.environ.get("HISTORY_TOOL_RESULT_MAX_CHARS", "1400"))
HISTORY_TEXT_MAX_CHARS = int(os.environ.get("HISTORY_TEXT_MAX_CHARS", "1000"))
# Individual-result compaction above is useful but it leaves every old tool_use,
# signature and wrapper in the active prompt.  A recoverable stage checkpoint
# therefore archives complete old message pairs once canonical stage state exists.
HISTORY_CHECKPOINT_AFTER_CHARS = int(os.environ.get(
    "HISTORY_CHECKPOINT_AFTER_CHARS", "90000"
))
HISTORY_CHECKPOINT_KEEP_MESSAGES = int(os.environ.get(
    "HISTORY_CHECKPOINT_KEEP_MESSAGES", "8"
))
ROLE_CARD_TOOL_RESULT_CAP = max(
    8000, int(os.environ.get("ROLE_CARD_TOOL_RESULT_CAP", "28000"))
)


def _deck_n_slides(parent):
    """粗数当前工作区已产出的页 HTML(给 review/material 超时按页数缩放用;数不到返 0=退回基础预算)。"""
    try:
        import glob as _g
        ws = getattr(parent, "ws", "") or ""
        return len(_g.glob(os.path.join(ws, "slides", "slide_*.html")))
    except Exception:
        return 0


def _scaled_timeout(base, per_unit, units, cap=None):
    """Return an operator-configured wall clock, or ``None`` for no deadline.

    Time scales only when a deployment explicitly enables a base timeout.  This
    keeps ordinary long decks from inheriting a hidden completion budget while
    preserving an emergency control for constrained installations.
    """
    if base is None:
        return None
    value = float(base) + max(0, int(units or 0)) * max(0, int(per_unit or 0))
    return min(value, float(cap)) if cap is not None else value


SUBAGENT_MAX_TOKENS = int(os.environ.get("SUBAGENT_MAX_TOKENS", "16000"))
MAX_SPAWN_DEPTH = int(os.environ.get("MAX_SPAWN_DEPTH", "1"))           # 委派深度上限(1 = 只有顶层能委派)
_ROLE_WRITE_BOUNDARIES = {
    "slide": ["plan", "base.css", "research", "speech.md", "materials", "assets"],
    "image": ["plan", "base.css", "research", "speech.md", "materials", "slides"],
    "research": ["plan", "base.css", "speech.md", "materials", "assets", "slides"],
    "material": ["plan", "base.css", "research", "speech.md", "assets", "slides"],
    "review": ["plan", "research", "materials", "assets"],
}
# Vision 不设“单 Agent 累计看图数”上限。一张图在紧随的模型回合被看到后，
# 便从**活跃模型上下文**里释放；真实像素仍由 Trace 快照永久保留。因此 36/80
# 页 Review 可以分批连续看完，无需因历史图片累积而 blocked，也无需重派 Review。
RESAMPLE_THINKING_ONLY = int(os.environ.get("RESAMPLE_THINKING_ONLY", "2"))  # >0: 传输层重采 thinking-only 退化空采样(丢弃不入轨迹);0=纯 loop 原行为。2026-07-10 默认 0→2:stopped_no_text 失败 159 中 92% 是 thinking-only/空退化,重采可吃掉主体
# 每个 tool_result 后追加一条引导思考的 text 块(=在工具结果 user 回合尾部拼一句),把一次性 system nudge
# 升级成"每轮工具后强制提醒",提高交错思考(reasoning summary + signature)触发率。默认关。
# ⚠️ 该 text 会进 messages/轨迹 → 若不想让 nudge 落进训练数据,pack 时按 THINK_NUDGE_TEXT 首句剥离(见 trace_to_openai)。
THINK_NUDGE_EACH_TOOL = os.environ.get("THINK_NUDGE_EACH_TOOL", "0") == "1"
THINK_NUDGE_TEXT = os.environ.get("THINK_NUDGE_TEXT",
    "请先核对上述工具结果的完整性和可信度，再选择最合适的下一步；"
    "不要重复无效动作。")
# 限流/过载(429/529/503/500/502)专用重试:网关并发突发时 review 子 agent 易吃 429。
# 这类是**瞬时可恢复**的传输层错误,默认 4 次 ~30s 退避在持续突发下不够 → 拉长。
# 指数退避 + 抖动,封顶 RETRY_BACKOFF_CAP 秒;尊重响应里的 Retry-After。非替模型兜底,纯传输韧性。
API_MAX_RETRIES = int(os.environ.get("API_MAX_RETRIES", "8"))       # 限流类最多重试次数
RETRY_BACKOFF_CAP = int(os.environ.get("RETRY_BACKOFF_CAP", "45"))  # 单次退避上限(秒)
# 触发"加长退避"的瞬时状态码(其余非 400 异常仍走原 4 次短退避)
_TRANSIENT_STATUS = {429, 500, 502, 503, 529}


def _runtime_time_context(started_epoch, language):
    """Return one authoritative timestamp shared by the whole agent tree."""
    started_utc = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(float(started_epoch))
    )
    if language == "zh":
        return (
            f"权威时间上下文：本任务开始于 {started_utc}。判断已发生、当前、未来和"
            "资料日期时以此为准，不使用模型记忆中的当前日期。"
        )
    return (
        f"Authoritative time context: this task started at {started_utc}. "
        "Use this timestamp for past/current/future and source-date judgments; "
        "do not rely on a remembered current date."
    )


def _infer_prompt_language(text):
    """Infer the primary language of the current user request."""
    value = str(text or "")
    cjk_chars = len(re.findall(r"[\u3400-\u9fff]", value))
    latin_chars = len(re.findall(r"[A-Za-z]", value))
    if not cjk_chars:
        return "en"
    if not latin_chars:
        return "zh"
    # CJK encodes a word in far fewer characters than Latin script; weight it so
    # common English technical terms do not flip an otherwise Chinese request.
    return "zh" if cjk_chars * 4 >= latin_chars else "en"


def _visible_response_language_context(language):
    if language == "zh":
        return (
            "回复语言：中文。过程说明、可见思考、工具调用前说明和最终总结均使用中文；"
            "代码、路径、引文和专有名词可保留原文。屏显文案和讲稿语言仍以用户要求为准。"
        )
    return (
        "Visible response language: use English for progress notes, any visible "
        "reasoning/thinking, tool-call "
        "preambles, and final summaries. Code, paths, quotations, and proper nouns "
        "may remain in their original language. Deck and speech language still "
        "follows the user's delivery requirement."
    )


def _child_language_contract(language):
    """Make the parent's response language explicit in every delegated task.

    The system prompt remains the authority, but weaker models often follow the
    task body more reliably than a distant system paragraph during long tool
    loops.  Delivery language stays plan-owned because it may intentionally
    differ from the raw query language.
    """
    if language == "zh":
        return (
            "回复语言：中文。所有可见思考、进度说明、工具调用前说明和最终总结使用中文；"
            "代码、路径、引文和专有名词可保留原文。\n"
            "交付语言：先遵循本次 goal 中的 deliverable_language；未提供时再读取已有的 "
            "plan/deck.md，仍未确定时跟随用户 query。不要从角色卡语言或模型默认语言推断。\n\n"
        )
    return (
        "Response language: English. Use English for all visible reasoning/thinking, "
        "progress notes, tool-call preambles, and final summaries; code, paths, "
        "quotations, and proper nouns may remain in their original language.\n"
        "Deliverable language: first follow deliverable_language in this task's goal; "
        "if absent, use an existing plan/deck.md, then fall back to the user query. "
        "Never infer it from the role-card language or model default.\n\n"
    )


def _generation_preferences_context(preferences, role, language):
    """Expose product inputs only to the orchestrator that resolves the deck plan."""
    values = dict(preferences or {})
    if role != "orchestrator" or not values:
        return ""
    settings = json.dumps(values, ensure_ascii=False, sort_keys=True)
    if language == "zh":
        text = (
            "\n产品界面已确认以下运行输入。它们只约束对应字段；"
            "写入 plan/deck.md 后由下游读取，不扩大任何子角色的职责。"
            "同一字段与 query 冲突时，以界面中的明确选择为用户最新指令；"
            f"不得静默丢弃：{settings}\n"
        )
        if int(values.get("attachment_count") or 0):
            text += (
                "\n本任务包含已挂载附件。严格执行 Skill 的 Material 阶段："
                "把已挂载路径交给 Material，收到全部 ready/complete coverage 合同后，"
                "逐份读完 materials/summaries/ 下的正式摘要；再把摘要与原始 query 合并，"
                "决定是否需要 Research 以及具体核验什么。Material 与 Research 不得同波启动；"
                "编排器不得自行读取原附件并以临时摘要替代该阶段。\n"
            )
        return text
    text = (
        "\nThe product UI confirmed the following runtime inputs. They constrain "
        "only their matching fields and must be recorded in plan/deck.md for downstream "
        "roles; they do not expand any role's responsibility. If the same field conflicts "
        "with the query, treat the explicit UI selection as the user's latest instruction. "
        f"Do not silently drop it: {settings}\n"
    )
    if int(values.get("attachment_count") or 0):
        text += (
            "\nThis run contains staged user attachments. Follow the selected Skill's "
            "Material stage exactly: delegate all staged attachment paths to Material "
            "sub-agents, wait for every ready/complete coverage contract, and read every "
            "formal summary under materials/summaries/ to EOF. Only then combine those "
            "summaries with the raw query and decide whether Research is needed and what "
            "it must verify. Material and Research must not run in the same wave. Do not "
            "replace Material by summarizing the attachments yourself.\n"
        )
    return text


def blocks_to_dicts(content):
    """把 anthropic SDK 的 content blocks 转成可序列化 dict(供 messages 列表与轨迹复用)。"""
    out = []
    for b in content:
        if b.type == "text":
            out.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif b.type == "thinking":
            sig = getattr(b, "signature", None)
            # 无签名的 thinking 块(签名被上游代理剥离,或 max_tokens 在 thinking 中途截断未生成签名)
            # 一旦发回 API 必触发 400 "signature: Field required" → 不重试 → api_failed → 整条 deck 被拒。
            # 这种块本就不可回传(API 强制要签名),丢弃它严格不劣于现状:常规有签名块完全不变,
            # 同一 assistant 轮里的 tool_use/text 仍保留(不会产生空 content);只有被拒轨迹会少一段截断思考。
            if not sig:
                continue
            out.append({"type": "thinking", "thinking": b.thinking, "signature": sig})
        elif b.type == "redacted_thinking":
            out.append({"type": "redacted_thinking", "data": b.data})
    return out


def _canonical_agent_label(role, label):
    """Canonical public identity for logs; keep trace directory names separate."""
    value = str(label or "").strip()
    role_name = str(role or "").strip().lower()
    if role_name == "orchestrator" or value.lower() in {"orch", "orchestrator"}:
        return "orch"
    return value


class Agent:
    """通用单 Agent 循环(Claude + 工具);Agent 对象 = 状态,循环逻辑在下面的 run_loop。

    编排器和子 agent 共用这个类,区别只在:工具集、初始任务、system、只读写前缀,以及编排器
    额外注册了 delegate_task。父子**共享同一个工作区 ws**(协作产出同一套产物),但各写各的轨迹。"""

    def __init__(self, role, sid, ws, sub_dir, tools_schema, config, initial_user, label,
                 system, skills_root, forbid_write_prefixes=None, extra_tools=None,
                 parent_main_trajectory_id="", root_main_trajectory_id="",
                 delegation_depth=0):
        self.role = role
        self.sid = sid
        self.ws = os.path.abspath(ws)                 # 工作区 = run_dir
        self.skills_root = os.path.normpath(os.path.abspath(skills_root)) if skills_root else None
        self.forbid_write_prefixes = list(forbid_write_prefixes or [])
        self.initial_user = initial_user
        # ``orchestrator`` is the durable role/trace-directory name, while the
        # public event stream uses one canonical identity: ``orch``.  Normalize
        # here so fresh logs, config snapshots and revision runs never re-create
        # the historical two-name split.
        self.label = _canonical_agent_label(role, label)
        self.extra_tools = extra_tools or {}
        self.tools = tools_schema
        self.cfg = config or {}
        self.protected_runtime_paths = list(self.cfg.get("_protected_runtime_paths") or [])
        self.started = time.time()
        self.task_started_epoch = float(
            self.cfg.get("_task_started_epoch", self.started)
        )
        prompt_language = str(self.cfg.get("_prompt_language") or "").lower()
        if prompt_language not in {"zh", "en"}:
            prompt_language = _infer_prompt_language(initial_user)
        self.prompt_language = prompt_language
        self.generation_preferences = dict(self.cfg.get("_generation_preferences") or {})
        self.base_system = system.rstrip()
        self.system = (
            f"{self.base_system}\n\n"
            f"{_runtime_time_context(self.task_started_epoch, prompt_language)}\n"
            f"{_visible_response_language_context(prompt_language)}\n"
        )
        self.system += _generation_preferences_context(
            self.generation_preferences, self.role, prompt_language
        )
        trace_root = os.path.join(self.ws, "_trace")
        trace_namespace = str(self.cfg.get("_trace_namespace") or "").strip("/")
        if trace_namespace:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*", trace_namespace):
                raise ValueError(f"非法 trace namespace: {trace_namespace!r}")
            trace_root = os.path.join(trace_root, trace_namespace)
        self.trace = Trace(os.path.join(trace_root, sub_dir))

        # —— tools.py 依赖的上下文字段 ——
        self.serper = os.environ.get("SERPER_API_KEY")
        self.img_base = self.cfg.get("openai_base_url",
                                     os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1")).rstrip("/")
        self.img_key = os.environ.get("OPENAI_API_KEY", "")
        self.image_model = self.cfg.get("image_model", os.environ.get("IMAGE_MODEL", "gpt-image-2-pro-all"))
        self.img_n = 0

        # —— 运行状态 ——
        self.last_shot = None       # 最近一次成功 render 的工作区相对路径
        self.n_renders = 0
        self._usage_acc = {"sum_input": 0, "sum_cache_read": 0, "sum_cache_create": 0, "sum_output": 0, "n_turns": 0}  # ① cache 记账
        self.n_vision_imgs = 0      # 本 agent 累计成功看过的直接图片（仅指标，不是配额）
        self.n_vision_calls = 0     # 成功取得像素/视觉分析的调用数；B 视觉后端同样计数
        self.vision_paths = []      # 成功看过的工作区图片路径；用于核验 Image 派生资产
        self._created_asset_paths = set()  # 本 Image worker 真正落地的候选，不混入并发 worker
        self._asset_activity_lock = threading.Lock()
        # Review regression evidence.  tools.py fills these from automatic
        # pre-edit snapshots and BEFORE/AFTER pixel inspections.
        self._review_modified_pages = set()
        self._review_comparison_pages = set()
        self._review_global_visual_change = False
        self._review_global_comparison = False
        self.final_text = ""
        self.exit_reason = None
        self._stage_memory_context = ""
        self._context_checkpoint_index = 0
        self.worker_recs = []       # 仅编排器:每个子 agent 的小结
        self._spawn_count = {}
        self._role_spawn_count = {}  # Research / Review 是任务级单例，失败不得用 _rN 绕过
        self._slide_page_owners = {}  # page -> 唯一 Production Group label
        self._spawn_lock = threading.Lock()
        self._child_sem = threading.Semaphore(MAX_CONCURRENT_CHILDREN)   # 父级并发闸
        self._delegate_depth = 0
        self.run_mode = str(self.cfg.get("run_mode") or "inference").strip().lower()
        if self.run_mode not in {"inference", "synthesis"}:
            raise ValueError(f"unsupported Mural run mode: {self.run_mode}")
        self.trace_mode_status = {
            "mode": self.run_mode, "complete": True, "image_count": 0,
        }
        # Audit-only lineage.  Orchestrator and every Subagent remain
        # independent Hermes main trajectories; the fields only expose their
        # delegation tree and never merge conversations into one training row.
        self.parent_main_trajectory_id = str(parent_main_trajectory_id or "")
        self.root_main_trajectory_id = str(root_main_trajectory_id or "")
        self.delegation_depth = max(0, int(delegation_depth or 0))

        # —— 模型客户端 ——
        self.model = self.cfg.get("model", os.environ.get("MODEL", "claude-opus-4-7-thinking"))
        self.a_base = self.cfg.get("anthropic_base_url",
                                   os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com"))
        # 0 means progress-bounded rather than turn-bounded. Repetition guards,
        # role contracts and pixel freshness still stop non-progress loops.
        self.max_turns = int(self.cfg.get("max_turns", 0))
        self.max_tokens = int(self.cfg.get("max_tokens", 16000))
        self.requested_thinking = os.environ.get(
            "STUDIO_REQUESTED_THINKING", os.environ.get("THINKING", "0")
        ) != "0"
        self.effective_thinking = os.environ.get(
            "STUDIO_EFFECTIVE_THINKING", os.environ.get("THINKING", "0")
        ) != "0"
        self.thinking_transport = os.environ.get("STUDIO_THINKING_TRANSPORT", "")
        self.thinking = self.effective_thinking
        self.think_effort = os.environ.get("THINK_EFFORT", "high")
        # 网关上带 `-thinking` 后缀的模型思考是**内置**的:纯调用即自带 thinking 块,反而传
        # anthropic 的 thinking/output_config 参数会把思考压掉。故对这类模型一律走纯调用
        # (thinking 块照样回来并被 blocks_to_dicts 记进轨迹)。
        self.native_thinking = "thinking" in self.model.lower()
        if self.native_thinking:
            self.thinking = False
            self.effective_thinking = True
        # beta 头与 cache_control 解耦(2026-06-30):
        #   - 官方现代法 = 只在 system 块挂 cache_control,**不需 legacy beta 头**(SDK/上游已 GA)。
        #   - A key(sk-bpAc)上游=AWS Bedrock 还会**拒** beta 头(400 invalid beta flag)。
        #   ⟹ beta 头默认**不发**(CACHE_BETA_HEADER=1 才发,只为个别老上游);cache_control 仍由 PROMPT_CACHE 控(默认开)。
        _default_headers = ({"anthropic-beta": "prompt-caching-2024-07-31"}
                            if os.environ.get("CACHE_BETA_HEADER", "0") != "0" else {})
        if os.environ.get("MODEL_BACKEND", "").lower() == "openai":
            # 学生模型(vLLM,OpenAI 兼容):鸭子化 anthropic 客户端,让 v1.2 引擎原样驱动 9B/27B。
            # 纯加法,只在 MODEL_BACKEND=openai 时生效;Opus 走下面 else,行为不变。
            from . import openai_backend
            self.model = self.cfg.get("model") or os.environ.get("STUDENT_MODEL", self.model)
            self.thinking = False   # OpenAI 兼容模型由请求体 transport 控制，不传 Anthropic 参数
            self.client = openai_backend.OpenAIShim(
                base=os.environ["STUDENT_BASE_URL"],
                model=self.model,
                key=os.environ.get("STUDENT_API_KEY", "EMPTY"),
                timeout=tools.parse_optional_timeout(
                    os.environ.get("OPENAI_REQUEST_TIMEOUT"), None
                ),
            )
        else:
            request_timeout = tools.parse_optional_timeout(
                os.environ.get("ANTHROPIC_REQUEST_TIMEOUT"), None
            )
            self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=self.a_base,
                                              default_headers=_default_headers,
                                              timeout=request_timeout)

        self.nova_raw = nova_bridge.create_recorder(self)
        self.nova_precheck = None

        if any(t.get("name") == "delegate_task" for t in self.tools):
            self.extra_tools.setdefault("delegate_task", lambda **a: delegate_task(self, **a))

    def log(self, m):
        print(f"[{self.sid}/{self.label}] {m}", flush=True)

    # —— 沙箱:写/渲染限定在 ws 内;读还可读只读的 skills_root 树 ——
    def safe(self, path):
        p = os.path.normpath(os.path.join(self.ws, path))
        if p != os.path.normpath(self.ws) and not p.startswith(os.path.normpath(self.ws) + os.sep):
            raise ValueError(f"路径越出工作区: {path}")
        return p

    def writable(self, path):
        """写入策略:命中 forbid_write_prefixes 的路径只读(如编排器不许写 slides/)。"""
        p = os.path.normpath(os.path.join(self.ws, path))
        for pre in self.forbid_write_prefixes + self.protected_runtime_paths:
            d = os.path.normpath(os.path.join(self.ws, pre))
            if p == d or p.startswith(d + os.sep):
                return False
        return True

    def read_path(self, path):
        """可读路径:ws 内,或只读的 skills_root 树(路径以 'skills' 开头时映射过去)。"""
        if self.skills_root and (path == "skills" or path.startswith("skills/")):
            rel = path[len("skills"):].lstrip("/")
            p = os.path.normpath(os.path.join(self.skills_root, rel))
            if p != self.skills_root and not p.startswith(self.skills_root + os.sep):  # 防同前缀越界
                raise ValueError("路径越出 skills")
            return p
        return self.safe(path)

    def config_snapshot(self):
        tool_names = [str(tool.get("name") or "") for tool in self.tools]
        return {
            "role": self.role, "sample_id": self.sid, "label": self.label,
            "task": self.initial_user, "model": self.model, "anthropic_base_url": self.a_base,
            "image_model": self.image_model, "max_tokens": self.max_tokens, "max_turns": self.max_turns,
            "serper": bool(self.serper), "pid": os.getpid(),
            "thinking": (
                {"type": self.thinking_transport, "enabled": self.effective_thinking}
                if self.thinking_transport else
                ("native" if self.native_thinking else
                 ({"type": "adaptive", "display": "summarized", "effort": self.think_effort} if self.effective_thinking else False))
            ),
            "requested_thinking": self.requested_thinking,
            "effective_thinking": self.effective_thinking,
            "thinking_transport": self.thinking_transport,
            "generation_preferences": self.generation_preferences,
            "authoritative_task_started_at": time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.task_started_epoch)
            ),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tools": tool_names,
            "vision_available": "vision_analyze" in tool_names,
            "vision_backend": (
                "nova_auxiliary_agent_loop"
                if os.environ.get("NOVA_RAW_V2", "0") == "1"
                else os.environ.get("VISION_BACKEND", "main_model") or "main_model"
            ),
            "vision_model": (
                os.environ.get("NOVA_VISION_MODEL")
                if os.environ.get("NOVA_RAW_V2", "0") == "1"
                else os.environ.get("VISION_ONESHOT_MODEL")
            ),
            "run_mode": self.run_mode,
            "active_image_policy": "consume-once",
            "trace_image_policy": (
                "immutable-shot-manifest" if self.run_mode == "synthesis"
                else "debug-shot-reference"
            ),
            "stage_memory": {
                "enabled": True,
                "convergence": "progress_lease",
                "checkpoint_after_chars": HISTORY_CHECKPOINT_AFTER_CHARS,
                "keep_recent_messages": HISTORY_CHECKPOINT_KEEP_MESSAGES,
                "ledger": "stage-state.json",
            },
        }

    def run(self):
        try:
            return run_loop(self)
        except Exception as exc:
            nova_bridge.abort_agent(self, exc)
            raise


# ===================== 循环(自由函数,操作一个 agent) =====================

def _anthropic_tool(t):
    """把 hermes 风格的 {name, description, parameters} 适配成 Anthropic Messages API 要的
    {name, description, input_schema}。schema 内容(name/描述/参数)与 hermes 逐字一致,只换外层键。"""
    return {"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}


# prompt caching 开关(默认开;PROMPT_CACHE=0 关)。把 cache_control 挂在**两个位置**:
#   ① system 改成 list-of-blocks、在 block 上挂 cache_control —— 缓存 [tools + system] 这段稳定前缀
#      (Anthropic 缓存前缀顺序为 tools→system→messages,故 system 断点也覆盖其前的 tools)。
#   ② 顶层 cache_control —— 走 SDK 的 extra_body 透传进请求体(SDK 无此具名参数,直接传 kwarg 会 TypeError)。
# ⚠️ tokenhub cache 命中随渠道路由在变(0622 全 0 → 2026-07-03 平台B实测已命中且无需 beta 头:
#    全新前缀 create=14062→read=14062)。故 cache_control 默认发(PROMPT_CACHE=1),落到缓存渠道即省;
#    命中率看真 run 的 usage.json cache_read(单次会飘,别当永久)。beta 头默认不发(见下,Bedrock 上游会 400 拒)。
PROMPT_CACHE = os.environ.get("PROMPT_CACHE", "1") != "0"
_EPHEMERAL = {"type": "ephemeral"}

# 多轮对话历史缓存(2026-07-09,三方A/Bedrock 实测 +40pt):在**发送用副本**的最后一条 message 的
# 最后一个 content block 上再挂一个 cache_control 断点,把 "system + 到当前为止的全部历史" 变成可复用前缀。
# 下一轮请求的前缀 = 上一轮缓存过的内容 → 命中(隔离实验 sys-only 47.5% → sys+last 87.7%)。
#   · 只加断点、不改 agent 持有的 messages(不污染下轮/落盘);str content 先转成 text block。
#   · system 断点保留 → 共 2 个断点(Bedrock 上限 4)。CACHE_MESSAGES=0 可关(回退旧行为)。
#   · Bedrock 只拒**顶层** cache_control,不拒 message block 级(已实测 create→read 稳定命中)。
CACHE_MESSAGES = os.environ.get("CACHE_MESSAGES", "1") != "0"
MODEL_TIMING_LOG = os.environ.get("MODEL_TIMING_LOG", "1") != "0"


def _msgs_with_cache_bp(messages):
    """返回 messages 的浅层安全副本,在最后一条 message 的最后一个 content block 挂 cache_control。
    只深拷最后一条(其余共享引用,省开销);content 为 str 时转成 [{"type":"text",...}]。失败则原样返回。"""
    if not messages:
        return messages
    try:
        out = list(messages)                       # 浅拷列表
        last = copy.deepcopy(out[-1])              # 只深拷最后一条,避免改到 agent 持有对象
        c = last.get("content")
        if isinstance(c, str):
            last["content"] = [{"type": "text", "text": c, "cache_control": _EPHEMERAL}]
        elif isinstance(c, list) and c:
            # 找最后一个 dict block(tool_result/text/...)挂断点;跳过非 dict
            for blk in reversed(c):
                if isinstance(blk, dict):
                    blk["cache_control"] = _EPHEMERAL
                    break
        else:
            return messages                        # 空/异常 content,不动
        out[-1] = last
        return out
    except Exception:
        return messages



def _acc_usage(agent, resp):
    """累加一次模型调用的 usage 到 agent._usage_acc(cache_* 可能为 None,(x or 0) 兜底)。非破坏:只读 resp.usage。"""
    try:
        u = getattr(resp, "usage", None)
        if u is None:
            return
        acc = agent._usage_acc
        acc["sum_input"]        += getattr(u, "input_tokens", 0) or 0
        acc["sum_cache_read"]   += getattr(u, "cache_read_input_tokens", 0) or 0
        acc["sum_cache_create"] += getattr(u, "cache_creation_input_tokens", 0) or 0
        acc["sum_output"]       += getattr(u, "output_tokens", 0) or 0
        acc["n_turns"]          += 1
    except Exception:
        pass


def _finish_model_call(agent, started_at, response, status):
    """Record wall latency so slow phases are diagnosable from one run log."""
    elapsed = max(0.0, time.monotonic() - started_at)
    try:
        acc = agent._usage_acc
        acc["sum_model_wall_seconds"] = round(
            float(acc.get("sum_model_wall_seconds", 0.0)) + elapsed, 3
        )
        acc["max_model_wall_seconds"] = round(
            max(float(acc.get("max_model_wall_seconds", 0.0)), elapsed), 3
        )
    except Exception:
        pass
    if MODEL_TIMING_LOG:
        agent.log(f"[model_timing] status={status} wall={elapsed:.1f}s")
    return response


def _write_usage(agent):
    """收尾把 usage 聚合写到 _trace/<role>/usage.json。容错:失败不影响轨迹。"""
    try:
        acc = agent._usage_acc
        # 命中率 = 读缓存 / 总输入。总输入 = 新输入 + 读缓存 + **写缓存**(cache_create 也是被处理的输入 token,
        # 之前漏掉它 → 分母偏小 → 命中率被严重高估,如 53776/(53776+12)=99.98% 实则 53776/95500=56.3%)。
        denom = acc["sum_cache_read"] + acc["sum_input"] + acc["sum_cache_create"]
        out = dict(acc)
        out["hit_rate"] = (acc["sum_cache_read"] / denom) if denom else None
        with open(os.path.join(agent.trace.sub_dir, "usage.json"), "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
    except Exception as e:
        agent.log(f"[usage.json 写失败,忽略] {str(e)[:120]}")


def _model_call(agent, messages):
    """一次模型调用,带有限的传输层重试(B 类:网络抖动,非替模型兜底)。工具恒挂。"""
    started_at = time.monotonic()
    memory_context = str(getattr(agent, "_stage_memory_context", "") or "").strip()
    effective_system = agent.system
    if memory_context:
        effective_system = (
            f"{effective_system.rstrip()}\n\n<stage_memory>\n"
            f"{memory_context}\n</stage_memory>"
        )
    system = effective_system
    extra_body = None
    if PROMPT_CACHE and system:
        system = [{"type": "text", "text": effective_system, "cache_control": _EPHEMERAL}]  # 位置②:system block
        extra_body = {}
        if os.environ.get("CACHE_TOPLEVEL", "1") != "0":
            extra_body["cache_control"] = _EPHEMERAL
        if os.environ.get("CACHE_USER_ID", ""):
            extra_body["metadata"] = {"user_id": os.environ["CACHE_USER_ID"]}
        extra_body = extra_body or None                                       # 位置①:顶层
    # 位置③:messages 历史断点(多轮命中率优化,见 _msgs_with_cache_bp)
    send_messages = _msgs_with_cache_bp(messages) if (PROMPT_CACHE and CACHE_MESSAGES) else messages
    kwargs = dict(model=agent.model, max_tokens=agent.max_tokens, system=system, messages=send_messages,
                  tools=[_anthropic_tool(t) for t in agent.tools])
    if extra_body:
        kwargs["extra_body"] = extra_body
    if agent.thinking:
        # Opus 4.7/4.8:effort 属于 output_config;display 必须显式 "summarized" 才能把(摘要版)推理写进轨迹。
        kwargs["thinking"] = {"type": "adaptive", "display": "summarized"}
        kwargs["output_config"] = {"effort": agent.think_effort}
    if agent.nova_raw is not None:
        response = nova_bridge.call_main(agent, kwargs)
        if response is not None:
            _acc_usage(agent, response)
        return _finish_model_call(
            agent, started_at, response, "ok" if response is not None else "nova_failed"
        )
    attempt = 0
    while True:
        try:
            # 大 max_tokens 非流式触发 SDK >10min 守卫;流式规避,get_final_message 同型返回
            with agent.client.messages.stream(**kwargs) as _stream:
                _resp = _stream.get_final_message()
            _acc_usage(agent, _resp)   # ① 累加本回合 usage
            return _finish_model_call(agent, started_at, _resp, "ok")
        except anthropic.BadRequestError as e:
            agent.log(f"[api 400 不重试] {str(e)[:300]}")   # 400 是请求本身的问题,重试不会变好
            return _finish_model_call(agent, started_at, None, "bad_request")
        except Exception as e:
            status = _status_code(e)
            transient = status in _TRANSIENT_STATUS
            # 限流/过载:用更宽的次数 + 指数退避(尊重 Retry-After);其余抖动错误保持原 4 次短退避。
            cap = API_MAX_RETRIES if transient else 4
            if attempt >= cap - 1:
                tag = f"{status} 限流/过载" if transient else "传输错误"
                agent.log(f"[api err {attempt} 放弃·{tag}] {str(e)[:160]}")
                return _finish_model_call(agent, started_at, None, "transport_failed")
            if transient:
                ra = _retry_after(e)
                if ra is not None:
                    wait = ra                                    # 服务器明确要求等多久就等多久(优先)
                elif attempt == 0:
                    wait = 0                                     # 第一次马上重试(瞬时抖动,可能是网络一闪)
                else:
                    wait = min(RETRY_BACKOFF_CAP, 2 ** attempt)  # 第二次起指数退避(4,8,16…封顶45s)
                wait += random.uniform(0, 1.0)                   # 抖动:错开多 worker 同时重试,避免再次撞墙
                agent.log(f"[api err {attempt}·{status} 退避 {wait:.1f}s] {str(e)[:140]}")
            else:
                wait = (0 if attempt == 0 else 3 * attempt) + random.uniform(0, 0.8)  # 传输错误:首次马上,其后 3,6,9s
                agent.log(f"[api err {attempt} 退避 {wait:.1f}s] {str(e)[:160]}")
            time.sleep(wait)
            attempt += 1


def _status_code(e):
    """从 anthropic SDK 异常里取 HTTP 状态码(取不到返回 None)。"""
    for attr in ("status_code",):
        v = getattr(e, attr, None)
        if isinstance(v, int):
            return v
    resp = getattr(e, "response", None)
    sc = getattr(resp, "status_code", None)
    return sc if isinstance(sc, int) else None


def _retry_after(e):
    """读响应 Retry-After 头(秒)。取不到/非法返回 None。封顶 RETRY_BACKOFF_CAP。"""
    resp = getattr(e, "response", None)
    headers = getattr(resp, "headers", None)
    if not headers:
        return None
    try:
        v = headers.get("retry-after") or headers.get("Retry-After")
        if v is None:
            return None
        return min(RETRY_BACKOFF_CAP, max(0.0, float(v)))
    except Exception:
        return None


def _lossless_tool_text(agent, tool_use, value, preview_cap):
    """Return a bounded preview while preserving the exact result for continuation.

    Large tool output used to be silently sliced, leaving the model unable to
    recover the omitted tail.  The exact text now lives beside the immutable
    trace and the preview contains a read_file continuation path.
    """
    text = str(value or "")
    if len(text) <= preview_cap:
        return text
    result_dir = os.path.join(agent.trace.sub_dir, "tool-results")
    os.makedirs(result_dir, exist_ok=True)
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tool_use.id or "tool"))
    path = os.path.join(result_dir, f"{token}.txt")
    temporary = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    relative = os.path.relpath(path, agent.ws).replace(os.sep, "/")
    reserve = 420
    head = text[:max(1000, preview_cap - reserve)].rsplit("\n", 1)[0]
    language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
    note = (
        f"\n\n[Tool output continues: exact {len(text)} characters were saved to {relative}. "
        "Use read_file on that path and follow every continuation offset until EOF before "
        "acting on the result.]"
        if language == "en" else
        f"\n\n[工具结果尚未结束：完整 {len(text)} 字符已保存到 {relative}。"
        "请用 read_file 读取该路径，并按每次返回的续读 offset 读到文件末尾后再据此行动。]"
    )
    return head + note


def _exec_one_tool(agent, tu):
    """执行单个 tool_use,返回它的 tool_result(含 render 记账 / 图像快照 / is_error)。
    render 记账与图像快照会改 agent 状态,所以这些工具只在 _run_tools 里**顺序**调;
    delegate_task 不碰这些 parent 状态(子 agent 各写各的),可安全并行。"""
    args = dict(tu.input) if isinstance(tu.input, dict) else {}
    if tu.name == "vision_analyze":
        args["_parent_tool_use_id"] = str(tu.id or "")
    err = False
    observer = getattr(agent, "_stage_observer", None)
    gate_error = observer.preflight_tool(tu.name, args) if observer else ""
    if gate_error:
        res = gate_error
        err = True
    else:
        try:
            res = tools.dispatch(agent, tu.name, args)
        except Exception as e:
            res = f"{tu.name} 崩溃: {e}"
            err = True
    if tu.name == "vision_analyze":
        # 只有真正得到图像或视觉后端分析才计数；路径不存在、格式错误和能力不可用不算。
        vision_ok = (
            isinstance(res, dict)
            and bool(res.get("image_b64") or res.get("vision_analysis"))
        )
        if vision_ok:
            agent.n_vision_calls += 1
            source_path = str(args.get("image_url") or "").strip()
            if source_path:
                agent.vision_paths.append(source_path)
                try:
                    fp = agent.read_path(source_path)
                    with open(fp, "rb") as stream:
                        digest = hashlib.sha256(stream.read()).hexdigest()
                    evidence = getattr(agent, "vision_evidence", None)
                    if not isinstance(evidence, dict):
                        evidence = {}
                        agent.vision_evidence = evidence
                    evidence[source_path.replace("\\", "/")] = {
                        "sha256": digest,
                        "mtime_ns": os.stat(fp).st_mtime_ns,
                    }
                except OSError:
                    pass
    # 通用 render 记账:terminal 命令 stdout 里的 .png 路径 → 当作渲染产出记账。
    # render.py 在 png 路径后还会打 ✓ RENDER_OK / ⚠ 版式警告,末行未必是 png,
    # 故从后往前找最后一个以 .png 结尾的行(兼容"路径后有诊断输出";取末行会漏记每次成功渲染)。
    if tu.name == "terminal" and isinstance(res, str) and not res.startswith("terminal 错误"):
        line = next((l.strip() for l in reversed(res.strip().splitlines())
                     if l.strip().endswith(".png")), "") if res.strip() else ""
        if line:
            agent.n_renders += 1
            try:
                agent.last_shot = os.path.relpath(line, agent.ws) if os.path.isabs(line) else line
            except Exception:
                agent.last_shot = line
    if isinstance(res, dict) and "image_b64" in res:
        tcid = tu.id
        # 快照照常存(轨迹完整,不受上限影响)
        try:
            agent.trace.snapshot_image(tcid, base64.b64decode(res["image_b64"]))
        except Exception as e:
            agent.log(f"WARN: 快照图像失败 {e}")
        agent.n_vision_imgs += 1
        return {"type": "tool_result", "tool_use_id": tcid, "content": [
            {"type": "text", "text": res.get("summary", "")},
            {"type": "image", "source": {"type": "base64", "media_type": res.get("media_type", "image/png"),
                                         "data": res["image_b64"]}}]}
    if isinstance(res, dict) and "vision_analysis" in res:
        return {
            "type": "tool_result",
            "tool_use_id": tu.id,
            "content": _lossless_tool_text(
                agent, tu, res.get("vision_analysis") or "", 16000
            ),
        }
    # delegate_task 已经把子轨迹压成结构化交接；不能再在工具适配层静默截断，
    # 否则父级会因缺字段转而读取整个 messages.json。其它叶子工具在活跃上下文中
    # 使用有界预览，完整结果会落盘并可按路径继续读取。
    if tu.name == "delegate_task":
        content = str(res)
    else:
        result_cap = 8000
        if tu.name == "read_file" and re.fullmatch(
            r"skills/mural-presenter/roles/(?:research|material|image|slide|review)\.md",
            str(args.get("path") or "").replace("\\", "/").lstrip("./"),
            re.I,
        ):
            result_cap = ROLE_CARD_TOOL_RESULT_CAP + 1024
        content = _lossless_tool_text(agent, tu, res, result_cap)
    tr = {"type": "tool_result", "tool_use_id": tu.id, "content": content}
    if err:
        tr["is_error"] = True     # 工具抛错 → 标 is_error,导出时据此把该 tool 消息标失败
    return tr


def _release_consumed_vision_images(messages):
    """释放已被模型看过的旧图，避免长 Deck 把历史 base64 一直带入后续请求。

    调用时机是“模型已对当前 messages 产生响应之后”，因此不会让本轮看图
    失效。Trace 在工具执行时已保存原始像素；模型若需要复看，可对该路径再次
    调用 vision_analyze。返回释放的图片数。
    """
    released = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            result_content = block.get("content")
            if not isinstance(result_content, list):
                continue
            next_content = []
            changed = False
            for item in result_content:
                if isinstance(item, dict) and item.get("type") == "image":
                    released += 1
                    changed = True
                    continue
                next_content.append(item)
            if changed:
                next_content.append({
                    "type": "text",
                    "text": (
                        "[该图像已在上一模型回合中查看，已从活跃上下文释放；"
                        "原始像素快照仍保留在轨迹中，上一回合的视觉结论继续有效。"
                        "不要因这条上下文维护提示重新查看同一未变化文件；"
                        "仅在图片被重生、替换或修图后检查新像素。]"
                    ),
                })
                block["content"] = next_content
    return released


def _role_contract_closeout_issues(agent, kind, contract):
    """Return missing/invalid fields that the same child can still restate.

    This does not decide quality and never converts a blocked result to ready.
    It only prevents a worker that already stopped from losing an otherwise
    valid run because its final machine-readable handoff omitted required keys.
    """
    status = str(contract.get("status") or "").strip().lower()
    issues = []
    allowed_statuses = {
        "research": {"ready", "partial", "blocked"},
        "material": {"ready", "blocked"},
        "image": {"ready", "blocked"},
        "slide": {"ready", "blocked"},
        "review": {"ready", "blocked"},
    }.get(kind, {"ready", "blocked"})
    if status not in allowed_statuses:
        issues.append("status: " + " | ".join(sorted(allowed_statuses)))
    if status == "blocked":
        return issues
    if kind == "material":
        if not str(contract.get("coverage") or "").strip().lower().startswith("complete"):
            issues.append("coverage: complete")
    elif kind == "research":
        if status == "partial" and str(contract.get("unresolved") or "").strip().lower() in {
                "", "none", "n/a", "not-applicable"}:
            issues.append("unresolved")
        if not str(contract.get("output") or "").strip():
            issues.append("output")
    elif kind == "review":
        expected_mode = str(getattr(agent, "_expected_review_mode", "") or "final_review")
        if str(contract.get("mode") or "").strip() != expected_mode:
            issues.append(f"mode: {expected_mode}")
        if str(contract.get("content_fidelity") or "").strip() not in {
                "pass", "not-applicable", "fail"}:
            issues.append("content_fidelity")
        if expected_mode == "final_review" and str(
                contract.get("diagnosed_pages") or "").strip() != "all":
            issues.append("diagnosed_pages: all")
        if str(contract.get("final_pixels_inspected") or "").strip() not in {"yes", "no"}:
            issues.append("final_pixels_inspected")
        if str(contract.get("regression_checked") or "").strip() not in {"yes", "no"}:
            issues.append("regression_checked")
        if not str(contract.get("regressed_pages") or "").strip():
            issues.append("regressed_pages")
        if str(contract.get("speech_aligned") or "").strip() not in {"yes", "no"}:
            issues.append("speech_aligned")
        if not str(contract.get("remaining") or "").strip():
            issues.append("remaining")
        try:
            int(str(contract.get("refine_rounds") or ""))
        except (TypeError, ValueError):
            issues.append("refine_rounds")
    return issues


def _history_tool_names(messages):
    """Return tool_use id -> name for compact summaries without changing tool pairing."""
    names = {}
    for message in messages:
        for block in (message.get("content") if isinstance(message, dict) else []) or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names[str(block.get("id") or "")] = str(block.get("name") or "tool")
    return names


def _archive_history_result(agent, tool_use_id, value, suffix=""):
    """Persist exact text before active-context compaction and return its path."""
    if agent is None:
        return ""
    result_dir = os.path.join(agent.trace.sub_dir, "history-results")
    os.makedirs(result_dir, exist_ok=True)
    token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(tool_use_id or "tool"))
    path = os.path.join(result_dir, f"{token}{suffix}.txt")
    if not os.path.isfile(path):
        temporary = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            stream.write(str(value or ""))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    return os.path.relpath(path, agent.ws).replace(os.sep, "/")


def _compact_result_text(text, tool_name, archive_path=""):
    """Keep actionable evidence from an old tool result, not its full transport payload."""
    value = str(text or "")
    if len(value) <= HISTORY_TOOL_RESULT_MAX_CHARS or "[历史工具结果已压缩]" in value:
        return value
    # delegate_task is already a compact, structured handoff.  Keeping it whole avoids
    # sending the parent back into child traces to recover a dropped contract field.
    if tool_name == "delegate_task":
        return value
    paths = []
    for path in re.findall(
        r"(?:^|[\s'\"`])((?:plan|research|materials|assets|slides|renders|_trace)/[^\s'\"`]+)",
        value,
        re.I,
    ):
        clean = path.rstrip(".,;:)。，；：）")
        if clean not in paths:
            paths.append(clean)
    signals = []
    for line in value.splitlines():
        if re.search(
            r"(?i)\b(status|error|failed|failure|warning|blocked|timeout|exit|coverage|unresolved)\b|"
            r"错误|失败|警告|阻塞|超时|未解决|覆盖",
            line,
        ):
            stripped = line.strip()
            if stripped and stripped not in signals:
                signals.append(stripped[:300])
            if len(signals) >= 6:
                break
    head = value[:500].strip()
    tail = value[-350:].strip() if len(value) > 850 else ""
    parts = [f"[历史工具结果已压缩] tool={tool_name}; original_chars={len(value)}"]
    if archive_path:
        parts.append(f"exact_result: {archive_path}（需要细节时用 read_file 读到 EOF）")
    if paths:
        parts.append("artifacts: " + ", ".join(paths[:12]))
    if signals:
        parts.append("signals:\n" + "\n".join(signals))
    if head:
        parts.append("excerpt_start: " + head)
    if tail and tail != head:
        parts.append("excerpt_end: " + tail)
    return "\n".join(parts)


def _compact_active_history(messages, agent=None):
    """Compact early ordinary text/tool output while leaving the immutable Trace untouched.

    Tool-use blocks and signed thinking blocks are never removed or rewritten.  This keeps
    Anthropic tool pairing valid and preserves reasoning signatures while preventing old
    searches, terminal errors and large read_file results from dominating every later call.
    """
    try:
        size = len(json.dumps(messages, ensure_ascii=False, default=str))
    except Exception:
        return 0
    if size <= HISTORY_COMPACT_AFTER_CHARS:
        return 0
    names = _history_tool_names(messages)
    cutoff = max(1, len(messages) - HISTORY_KEEP_RECENT_MESSAGES)
    saved = 0
    for index, message in enumerate(messages[:cutoff]):
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block_index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tool_use_id = str(block.get("tool_use_id") or "")
                tool_name = names.get(tool_use_id, "tool")
                old = block.get("content")
                if isinstance(old, str):
                    archive = ""
                    if (len(old) > HISTORY_TOOL_RESULT_MAX_CHARS
                            and "[历史工具结果已压缩]" not in old):
                        archive = _archive_history_result(agent, tool_use_id, old)
                    new = _compact_result_text(old, tool_name, archive)
                    saved += max(0, len(old) - len(new))
                    block["content"] = new
                elif isinstance(old, list):
                    for item_index, item in enumerate(old):
                        if isinstance(item, dict) and item.get("type") == "text":
                            prior = str(item.get("text") or "")
                            archive = ""
                            if (len(prior) > HISTORY_TOOL_RESULT_MAX_CHARS
                                    and "[历史工具结果已压缩]" not in prior):
                                archive = _archive_history_result(
                                    agent, tool_use_id, prior, f"_{item_index}"
                                )
                            new = _compact_result_text(prior, tool_name, archive)
                            saved += max(0, len(prior) - len(new))
                            item["text"] = new
            elif block.get("type") == "text":
                old = str(block.get("text") or "")
                if (len(old) > HISTORY_TEXT_MAX_CHARS
                        and "[早期助手文本已压缩]" not in old
                        and "[早期消息文本已压缩]" not in old):
                    role = str(message.get("role") or "message")
                    archive = _archive_history_result(
                        agent, f"{role}_{index}_{block_index}", old
                    )
                    new = (
                        f"[早期消息文本已压缩]\noriginal_chars: {len(old)}\n"
                        f"exact_result: {archive}（需要细节时用 read_file 读到 EOF）\n"
                        "excerpt: "
                        + old[:700].strip()
                        + (" … " + old[-220:].strip() if len(old) > 920 else "")
                    )
                    saved += max(0, len(old) - len(new))
                    block["text"] = new
    return saved


def _serialized_history_chars(messages, extra_content=None):
    """Approximate the active request size without mutating it."""
    payload = messages
    if extra_content is not None:
        payload = list(messages) + [{"role": "user", "content": extra_content}]
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str))
    except Exception:
        return 0


def _history_checkpoint_needed(messages, extra_content=None):
    return (
        HISTORY_CHECKPOINT_AFTER_CHARS > 0
        and _serialized_history_chars(messages, extra_content) > HISTORY_CHECKPOINT_AFTER_CHARS
    )


def _checkpoint_active_history(messages, agent, observer):
    """Archive complete old message pairs and retain a compact stage memory.

    The first user request is preserved byte-for-byte.  The retained tail always
    starts with an assistant message, so Anthropic tool_use/tool_result pairing
    remains valid.  Exact removed messages stay readable under the Agent trace.
    """
    if not _history_checkpoint_needed(messages) or not observer.checkpoint_ready():
        return 0, ""
    # Never archive the exact context that explains an unfinished paginated
    # read.  The continuation gate remains authoritative, and the next request
    # must still see the preceding excerpt until EOF is reached.
    if getattr(agent, "_pending_read_continuations", None):
        return 0, ""
    keep = max(4, HISTORY_CHECKPOINT_KEEP_MESSAGES)
    candidate = max(2, len(messages) - keep)
    # Prefer the preceding assistant boundary.  Its following user tool_result,
    # when present, is retained with it.
    while candidate > 1 and str(messages[candidate].get("role") or "") != "assistant":
        candidate -= 1
    if candidate <= 1:
        return 0, ""
    removed = copy.deepcopy(messages[1:candidate])
    if not removed:
        return 0, ""

    agent._context_checkpoint_index = int(
        getattr(agent, "_context_checkpoint_index", 0) or 0
    ) + 1
    checkpoint_dir = os.path.join(agent.trace.sub_dir, "context-checkpoints")
    os.makedirs(checkpoint_dir, exist_ok=True)
    path = os.path.join(
        checkpoint_dir,
        f"checkpoint_{agent._context_checkpoint_index:03d}.json",
    )
    payload = {
        "schema_version": 1,
        "checkpoint": agent._context_checkpoint_index,
        "removed_messages": removed,
        "stage_state": copy.deepcopy(observer.state),
    }
    temporary = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)

    messages[:] = [messages[0]] + messages[candidate:]
    relative = os.path.relpath(path, agent.ws).replace(os.sep, "/")
    agent._stage_memory_context = observer.memory_text(relative)
    return len(removed), relative


def _run_tools(agent, tool_uses, turn, tool_log):
    """执行一个回合的所有 tool_use;每个 tool_use **一定**配一个 tool_result(即使失败)。
    同一回合里:**多个 delegate_task 并行**;**多个纯 IO 叶子工具(PARALLEL_LEAF_TOOLS,如 image_generate)
    也并行**(单独限并发 TURN_TOOL_PARALLEL 防出图/搜索网关过载);其余工具(会改 agent 记账 / 需保序)顺序执行。
    并行工具均无 agent 状态记账、内容寻址落盘不撞名 → 线程安全;结果按原 index 回填,顺序不乱。"""
    for tu in tool_uses:
        args = tu.input if isinstance(tu.input, dict) else {}
        agent.log(f"🔧 {tu.name}({json.dumps(args, ensure_ascii=False)[:130]})")
        tool_log.append({"turn": turn, "name": tu.name, "args": args})
    results = [None] * len(tool_uses)
    deleg = [i for i, tu in enumerate(tool_uses) if tu.name == "delegate_task"]
    leaf = [i for i, tu in enumerate(tool_uses) if tu.name in PARALLEL_LEAF_TOOLS]
    par = set(deleg) | set(leaf)
    for i, tu in enumerate(tool_uses):                 # 非并行工具:顺序(含会改 agent 状态的记账)
        if i not in par:
            results[i] = _exec_one_tool(agent, tu)
    if len(deleg) == 1:                                # delegate:并行(并发由父级 _child_sem 统一卡)
        results[deleg[0]] = _exec_one_tool(agent, tool_uses[deleg[0]])
    elif len(deleg) > 1:
        with cf.ThreadPoolExecutor(max_workers=len(deleg), thread_name_prefix="deleg") as ex:
            futs = {ex.submit(_exec_one_tool, agent, tool_uses[i]): i for i in deleg}
            for f in cf.as_completed(futs):
                results[futs[f]] = f.result()
    if len(leaf) == 1:                                 # 纯 IO 叶子:单个直接跑
        results[leaf[0]] = _exec_one_tool(agent, tool_uses[leaf[0]])
    elif len(leaf) > 1:                                # 多个纯 IO 叶子:并行,单独限并发防网关过载
        with cf.ThreadPoolExecutor(max_workers=min(len(leaf), TURN_TOOL_PARALLEL), thread_name_prefix="leaf") as ex:
            futs = {ex.submit(_exec_one_tool, agent, tool_uses[i]): i for i in leaf}
            for f in cf.as_completed(futs):
                results[futs[f]] = f.result()
    return results


def run_loop(agent):
    """纯 ReAct 循环:模型 → 工具 → 模型,直到模型不再调用工具并完成合同。
    默认不按回合数截断；显式 ``max_turns>0`` 仅作为部署方紧急保护。只对无 text/tool 的退化采样做有限重采，
    并在子角色遗漏最终合同或像素闭环时，按问题是否变化给同一角色收口机会。
    Harness 不会伪造 ready。
    同时保留传输/进程层安全（API 重试、显式部署墙钟、子线程崩溃捕获）。写原始轨迹；
    返回 finished_clean。"""
    agent.trace.snapshot_inputs(agent.system, agent.tools, agent.config_snapshot())
    observer_kind = (
        "orchestrator" if getattr(agent, "role", "") == "orchestrator"
        else _task_kind(getattr(agent, "label", ""), agent.initial_user)
    )
    observer = stage_memory.StageObserver(agent, observer_kind)
    agent._stage_observer = observer
    messages = [{"role": "user", "content": agent.initial_user}]
    # 轨迹使用独立的轻量历史：保留每次看图的 shot 引用，不保留 base64。
    # 活跃 messages 则在图像被消费后释放像素，使长 Deck 能持续分批审查。
    trace_messages = [copy.deepcopy(messages[0])]
    tool_log = []
    contract_closeout_seen = {}
    for turn in itertools.count():
        if agent.max_turns > 0 and turn >= agent.max_turns:
            agent.exit_reason = "max_turns"
            agent.log(
                f"到达部署方显式 max_turns({agent.max_turns})，停止；"
                "默认配置不启用该墙，正常收口依赖进展检测与角色合同。"
            )
            break
        # 传输层有限重采: native-thinking 偶发
        # "只出 thinking 就 end_turn"的退化空采样(有 thinking、无 text、无 tool),当失败采样**丢弃并重采**
        # 最多 K 次，退化轮不 append、不进轨迹。无 tool 且无 text
        # 不可能是有效角色合同；无论是 thinking-only 还是真空轮都重采。
        resp = None
        for _rs in range(RESAMPLE_THINKING_ONLY + 1):
            resp = _model_call(agent, messages)
            if resp is None:
                break
            _has_tool = any(b.type == "tool_use" for b in resp.content)
            _has_text = any(b.type == "text" and b.text.strip() for b in resp.content)
            _has_think = any(b.type == "thinking" and b.thinking.strip() for b in resp.content)
            if _has_tool or _has_text:
                break
            if _rs < RESAMPLE_THINKING_ONLY:
                flavor = "thinking-only" if _has_think else "empty"
                agent.log(f"[{turn}] ⟲ {flavor} 退化采样,丢弃重采 {_rs+1}/{RESAMPLE_THINKING_ONLY}")
        if resp is None:
            agent.exit_reason = "api_failed"
            agent.log("API 连续失败,放弃")
            break

        assistant_message = {"role": "assistant", "content": blocks_to_dicts(resp.content)}
        messages.append(assistant_message)
        trace_messages.append(copy.deepcopy(assistant_message))
        # 上一 user 回合中的图片已被这次响应消费；在执行新工具前就释放，
        # 下一次模型请求只携带新的视觉批次。
        # Context maintenance and training retention are separate.  The live
        # prompt consumes each bitmap once in both modes; trace_messages keeps
        # the immutable shot reference, and synthesis mode additionally writes
        # a hash-verified multimodal manifest for training export.
        released = _release_consumed_vision_images(messages)
        if released:
            agent.log(f"[视觉上下文] 已释放 {released} 张已消费图像，轨迹快照仍保留")
        turn_text = ""
        for b in resp.content:
            if b.type == "thinking" and b.thinking.strip():
                agent.log(f"[{turn}] 🧠 {b.thinking.strip()[:140]}")
            if b.type == "text" and b.text.strip():
                agent.log(f"[{turn}] 💬 {b.text.strip()[:180]}")
                turn_text = b.text.strip()
                agent.final_text = turn_text
        tool_uses = [b for b in resp.content if b.type == "tool_use"]

        response_truncated = str(getattr(resp, "stop_reason", "") or "") in {
            "max_tokens", "length"
        }
        if not tool_uses and response_truncated:
            language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
            continuation = (
                "The previous response reached the per-response output window. Continue from "
                "the last complete action; do not restart or rewrite completed files. Split any "
                "large write into smaller canonical files or patches. This was not a task finish, "
                "so return the role contract only after the real artifacts are complete."
                if language == "en" else
                "上一回复到达单次输出窗口，并不代表任务结束。请从最后一个完整动作继续，"
                "不要从头重写已经完成的文件；把过大的写入拆成较小的正式文件或局部 patch。"
                "真实产物完成后再返回角色合同。"
            )
            continuation_message = {"role": "user", "content": continuation}
            messages.append(continuation_message)
            trace_messages.append(copy.deepcopy(continuation_message))
            agent.log("模型回复因 max_tokens 截断：保留已完成上下文并续跑，不误判为自然收尾")
            continue

        if not tool_uses:
            kind = _task_kind(getattr(agent, "label", ""), agent.initial_user)
            contract = _final_contract(turn_text)
            contract_issues = _role_contract_closeout_issues(agent, kind, contract)
            needs_contract = (
                getattr(agent, "role", "") != "orchestrator"
                and kind in {"material", "research", "image", "slide", "review"}
                and bool(contract_issues)
            )
            pixel_state = None
            if kind == "slide" and getattr(agent, "role", "") != "orchestrator":
                pixel_state = _slide_pixel_state(
                    agent, getattr(agent, "_assigned_pages", [])
                )
            elif kind == "review" and getattr(agent, "role", "") != "orchestrator":
                pixel_state = _review_pixel_state(agent)
            needs_pixel_closeout = bool(
                pixel_state
                and (
                    pixel_state["missing_pages"]
                    or pixel_state["stale_pages"]
                    or pixel_state["dirty_sources"]
                )
            )
            closeout_signature = ""
            if needs_contract or needs_pixel_closeout:
                closeout_signature = json.dumps({
                    "kind": kind,
                    "contract_issues": sorted(contract_issues),
                    "missing_pages": sorted((pixel_state or {}).get("missing_pages") or []),
                    "stale_pages": sorted((pixel_state or {}).get("stale_pages") or []),
                    "dirty_sources": sorted((pixel_state or {}).get("dirty_sources") or []),
                }, ensure_ascii=False, sort_keys=True)
                contract_closeout_seen[closeout_signature] = (
                    int(contract_closeout_seen.get(closeout_signature, 0)) + 1
                )
            # Every changed issue set receives another closeout chance.  An
            # identical issue set stopping twice is no progress and exits as a
            # real failure instead of consuming an arbitrary number of retries.
            if ((needs_contract or needs_pixel_closeout)
                    and contract_closeout_seen.get(closeout_signature, 0) == 1
                    and (agent.max_turns <= 0 or turn + 1 < agent.max_turns)):
                language = str(
                    getattr(agent, "prompt_language", "zh") or "zh"
                ).lower()
                if needs_pixel_closeout:
                    missing = ",".join(
                        f"{page:02d}" for page in pixel_state["missing_pages"]
                    ) or "none"
                    stale = ",".join(
                        f"{page:02d}" for page in pixel_state["stale_pages"]
                    ) or "none"
                    dirty = ",".join(pixel_state["dirty_sources"][:8]) or "none"
                    if kind == "review" and language == "en":
                        reminder = (
                            "Final Review stopped before its delivery pixel proof was current. "
                            f"Missing final coverage: {missing}; pixels changed after inspection: "
                            f"{stale}; unrendered visual sources: {dirty}. Inspect the current "
                            "review contact groups and open only the listed changed or ambiguous "
                            "pages at full resolution. Then run only the pixel-immutable build. Do "
                            "not edit, prepare, or render after that final inspection. Return the "
                            "exact Review contract only when build succeeds without changing pixels."
                        )
                    elif kind == "review":
                        reminder = (
                            "最终 Review 在交付像素证据完整前停止了。"
                            f"缺少最终覆盖：{missing}；检查后像素发生变化：{stale}；"
                            f"尚未重渲源文件：{dirty}。请查看当前 review 联系表批次，并只对上述"
                            "变化页或总览无法判断的页面打开最终单页；随后只能运行不改变像素的 build。"
                            "最终检查后不得再修改、prepare 或渲染；只有 build 成功且像素未变时，"
                            "才按 Review 角色卡返回准确合同。"
                        )
                    elif language == "en":
                        reminder = (
                            "Your Slide Group stopped before its final pixel proof was current. "
                            f"Pages missing fresh pixel coverage: {missing}; stale pixels: {stale}; "
                            f"unrendered visual sources: {dirty}. Render every affected page, then "
                            "inspect one fresh focus contact sheet covering the whole group. Open only "
                            "special, complex, flagged, or ambiguous pages individually. Make no edit "
                            "after the final covering inspection, then return the exact Slide role "
                            "contract. Do not claim ready until this is complete."
                        )
                    else:
                        reminder = (
                            "你的 Slide Group 在最终像素证据完整前停止了。"
                            f"缺少新鲜像素覆盖：{missing}；像素已过期：{stale}；"
                            f"尚未重渲源文件：{dirty}。请重渲所有受影响页面，再用一张覆盖整组的"
                            "新 focus 联系表完成总览检查；仅对特殊页、复杂页、标红页或总览无法判断的"
                            "页面打开单页。最终覆盖检查后不要再改 HTML/CSS，然后按 Slide 角色卡返回"
                            "准确合同。闭环完成前不得返回 ready。"
                        )
                elif language == "en":
                    reminder = (
                        "Your structured role contract is incomplete or invalid "
                        f"({'; '.join(contract_issues)}). If the assigned work "
                        "is incomplete, continue with the necessary tools now. If it is complete, "
                        "return the exact role-card contract with a standalone status field. Do "
                        "not describe planned work as completed."
                    )
                else:
                    reminder = (
                        "你刚才停止调用工具，但结构化角色合同缺失或无效（"
                        f"{'；'.join(contract_issues)}）。若任务尚未完成，请现在继续调用"
                        "必要工具；若已经完成，请按角色卡返回准确合同，至少包含独立的 status 字段。"
                        "不得把计划动作描述成已完成。"
                    )
                closeout_message = {"role": "user", "content": reminder}
                messages.append(closeout_message)
                trace_messages.append(copy.deepcopy(closeout_message))
                reason = "最终像素证据未闭环" if needs_pixel_closeout else "缺少结构化合同"
                agent.log(f"{kind} {reason}：问题集合有变化，继续同角色收口")
                continue
            # 相同问题集合再次出现代表没有进展；不代写产物或伪造 ready。
            agent.exit_reason = "text_response" if turn_text else "stopped_no_text"
            agent.log(f"模型停止调用工具,收尾于回合 {turn}(stop={resp.stop_reason}, exit={agent.exit_reason})")
            break

        _tool_content = _run_tools(agent, tool_uses, turn, tool_log)
        stage_changed, stage_hints = observer.observe(turn, tool_uses)
        if stage_changed or observer.progress_renewed:
            agent._blocked_no_progress = 0
            # Repetition is evaluated within one no-progress epoch, not over the
            # lifetime of a long task.  A real canonical artifact or child
            # handoff starts a fresh epoch.
            agent._repeatable_tool_calls = {}
        if (stage_changed or observer.progress_renewed
                or any(tu.name == "delegate_task" for tu in tool_uses)):
            agent._stage_memory_context = observer.memory_text()
        if stage_hints and isinstance(_tool_content, list):
            _tool_content = _tool_content + [
                {"type": "text", "text": hint} for hint in stage_hints
            ]
        if response_truncated and isinstance(_tool_content, list):
            language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
            _tool_content = _tool_content + [{
                "type": "text",
                "text": (
                    "The tool calls above completed, but the assistant response reached the "
                    "per-response output window. Continue with the next unfinished action; do "
                    "not repeat successful tools or treat this as completion."
                    if language == "en" else
                    "上述工具调用已经完成，但本次助手回复到达单次输出窗口。请继续下一个未完成动作，"
                    "不要重复已成功工具，也不要把本轮当成任务完成。"
                ),
            }]
        blocked_no_progress = sum(
            1
            for result in (_tool_content or [])
            if isinstance(result, dict)
            and (
                "vision_analyze 已阻止" in str(result.get("content") or "")
                or "阶段进展门已阻止" in str(result.get("content") or "")
                or "Stage progress gate blocked" in str(result.get("content") or "")
            )
        )
        if blocked_no_progress:
            agent._blocked_no_progress = int(
                getattr(agent, "_blocked_no_progress", 0) or 0
            ) + 1

        # 同一证据获取或写入动作反复出现不会产生新进展。第三次参数完全相同的调用后
        # 停止当前子任务，避免编排器反复读取同一个文件，或 Research/Material 在
        # 一个错误路线中跑满数小时。read_file 的 offset/limit 属于签名的一部分，
        # 因而正常的分段续读不会被误判。
        repeatable = {
            "read_file", "web_search", "web_extract",
            "write_file", "patch",
        }
        signatures = getattr(agent, "_repeatable_tool_calls", None)
        if not isinstance(signatures, dict):
            signatures = {}
            agent._repeatable_tool_calls = signatures
        repeated_calls = 0
        for tool_use in tool_uses:
            if tool_use.name not in repeatable:
                continue
            raw = json.dumps(tool_use.input if isinstance(tool_use.input, dict) else {},
                             ensure_ascii=False, sort_keys=True, default=str)
            signature = f"{tool_use.name}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"
            signatures[signature] = int(signatures.get(signature, 0)) + 1
            if signatures[signature] >= 3:
                repeated_calls += 1
        stalled = int(getattr(agent, "_blocked_no_progress", 0) or 0) >= 2 or repeated_calls
        finalization_only = bool(getattr(agent, "_finalization_only", False))
        label_lower = str(getattr(agent, "label", "") or "").lower()
        finalization_role = next(
            (kind for kind in ("material", "research", "image", "review", "slide")
             if label_lower.startswith(kind)),
            "",
        )
        role_can_finalize = bool(finalization_role)
        stop_after_results = False
        if stalled and role_can_finalize and not finalization_only:
            agent._finalization_only = True
            agent._finalization_role = finalization_role
            if finalization_role in {"review", "slide"}:
                # A visually stalled worker may report what remains, but it may not
                # turn a missing final pixel proof into a synthetic ready verdict.
                agent._stall_forced_status = "blocked"
            agent._blocked_no_progress = 0
            language = str(getattr(agent, "prompt_language", "zh") or "zh").lower()
            if finalization_role == "review":
                instruction = (
                    "停滞保护已关闭继续看图、渲染和页面修改。只把已发现问题写入唯一 "
                    "_trace/review-issues.md，然后返回 status: blocked；不得返回 ready。"
                    if language != "en" else
                    "Stall protection has closed further vision, rendering, and page edits. "
                    "Write the known issues to _trace/review-issues.md, then return status: "
                    "blocked. You may not return ready."
                )
            elif finalization_role == "slide":
                instruction = (
                    "停滞保护已关闭继续看图、渲染和修改。用已有证据返回 status: blocked，"
                    "列明未解决页面；不得再写页面或返回 ready。"
                    if language != "en" else
                    "Stall protection has closed further vision, rendering, and edits. Return "
                    "status: blocked with the unresolved pages; do not write pages or return ready."
                )
            else:
                if finalization_role == "research":
                    instruction = (
                        "停滞保护已关闭继续检索/看图。现在只允许把已经取得的证据写入 "
                        "research/research.md，然后返回 ready、带明确 unresolved 的 partial，"
                        "或 blocked；不得继续调用证据工具。"
                        if language != "en" else
                        "Stall protection has closed further search and vision. Write the evidence "
                        "already obtained to research/research.md, then return ready, partial with "
                        "explicit unresolved items, or blocked. Do not call more evidence tools."
                    )
                elif finalization_role == "image":
                    instruction = (
                        "停滞保护已关闭继续搜图、生图、下载和看图。现在只允许把已有候选"
                        "写回 assets/catalog.json 的准确状态；全部必需素材已 ready 才返回 "
                        "ready，否则列明 missing 并返回 blocked。"
                        if language != "en" else
                        "Stall protection has closed further search, generation, download, and "
                        "vision. Update assets/catalog.json with accurate decisions for the "
                        "existing candidates. Return ready only when every required asset is "
                        "ready; otherwise list missing assets and return blocked."
                    )
                else:
                    instruction = (
                        "停滞保护已关闭继续解析/看图。现在只允许把已经取得的证据写入指定的 "
                        "Material 摘要；覆盖完整则返回 ready，否则返回 blocked。Material 不返回 partial，"
                        "也不得继续调用证据工具。"
                        if language != "en" else
                        "Stall protection has closed further parsing and vision. Write the evidence "
                        "already obtained to the assigned Material summary; return ready only for "
                        "complete coverage, otherwise blocked. Material must not return partial or "
                        "call more evidence tools."
                    )
            if isinstance(_tool_content, list):
                _tool_content = _tool_content + [{"type": "text", "text": instruction}]
            agent.log("停滞保护触发：进入一次受控收口，不再允许继续检索或看图。")
        elif finalization_only and stalled:
            # Closeout has no fixed number of write turns. Stop only when the
            # same closeout operation itself repeats without new evidence.
            stop_after_results = True
        elif stalled:
            stop_after_results = True
        if THINK_NUDGE_EACH_TOOL and isinstance(_tool_content, list):
            # 在 tool_result 块之后追加一句引导思考的 text(合法:user 回合可 tool_result+text 并存)
            _tool_content = _tool_content + [{"type": "text", "text": THINK_NUDGE_TEXT}]
        if (_history_checkpoint_needed(messages, _tool_content)
                and not observer.checkpoint_ready()
                and isinstance(_tool_content, list)):
            checkpoint_hint = observer.checkpoint_hint()
            if checkpoint_hint:
                _tool_content = _tool_content + [{"type": "text", "text": checkpoint_hint}]
        tool_message = {"role": "user", "content": _tool_content}
        messages.append(tool_message)
        # clean() 将图像替换为 shot 路径，避免轨迹内存持有第二份 base64。
        trace_messages.append(agent.trace.clean(tool_message))
        compacted = _compact_active_history(messages, agent=agent)
        if compacted:
            agent.log(f"[上下文维护] 已压缩早期普通文本/工具结果约 {compacted} 字符；原始 Trace 未改变")
        checkpointed, checkpoint_path = _checkpoint_active_history(
            messages, agent, observer
        )
        if checkpointed:
            agent.log(
                f"[阶段记忆] 已归档 {checkpointed} 条旧消息到 {checkpoint_path}；"
                "活跃上下文只保留原始任务、阶段状态与近期完整工具对"
            )
        if stop_after_results:
            agent.exit_reason = "stalled_repetition"
            agent.log(
                "停滞保护触发：相同工具路线或未变化像素被重复请求；"
                "停止当前子任务，保留已有产物并避免继续膨胀上下文。"
            )
            break
    # 显式紧急上限不会被伪装成成功；默认路径没有固定回合截止。
    finished_clean = agent.exit_reason == "text_response"
    observer.finish(agent.exit_reason, _final_contract(agent.final_text))
    agent.trace_mode_status = agent.trace.write(
        trace_messages, tool_log, getattr(agent, "run_mode", "inference")
    )
    _write_usage(agent)   # ① 落 usage.json
    # Training/raw export receives the full immutable trace, never the pruned
    # active prompt used for later model calls.
    agent.nova_precheck = nova_bridge.finalize_agent(
        agent, trace_messages, finished_clean
    )
    agent.log(f"轨迹已写: turns={len(tool_log)} renders={agent.n_renders} exit={agent.exit_reason}")
    return finished_clean


# ============= 委派子 agent(自由函数,操作一个 agent) =============

def _normalize_task(t):
    """Derive all internal routing from one Hermes-visible ``goal`` string."""
    goal = str(t.get("goal") or "")
    label = _label_from_goal(goal) or ""
    kind = _task_kind(label, goal)
    toolsets_by_role = {
        "research": ["file", "web"],
        "material": ["file", "terminal", "vision"],
        "image": ["file", "terminal", "web", "image_gen", "vision"],
        "slide": ["file", "terminal", "vision"],
        "review": ["file", "terminal", "vision"],
    }
    return {
        "goal": goal,
        "context": "",
        "toolsets": list(toolsets_by_role.get(kind, [])),
        "role": "leaf",
        "label": label,
    }


def _task_kind(label, goal):
    """Resolve role identity before inspecting incidental words in task prose."""
    label_text = str(label or "").strip().lower()
    goal_text = str(goal or "")
    text = goal_text.lower()
    for prefix, kind in (
        ("slide", "slide"), ("image", "image"), ("research", "research"),
        ("material", "material"), ("review", "review"),
    ):
        if label_text.startswith(prefix):
            return kind
    tagged = re.search(
        r"\[\s*(research|material|image|slide|review)(?:[\s_-]+[^\]]+)?\s*\]",
        goal_text, re.I,
    )
    if tagged:
        return tagged.group(1).lower()
    if re.search(r"\bslides?(?:\s+group|\s+\d|\s*:)", text):
        return "slide"
    if re.search(r"(?:^|\n)\s*image\s*:", text):
        return "image"
    if re.search(r"(?:^|\n)\s*research\s*:", text):
        return "research"
    if re.search(r"(?:^|\n)\s*material\s*:", text):
        return "material"
    if (re.search(r"(?:^|\n)\s*(?:final\s+)?review\s*:", text)
            or re.search(r"\bmode\s*=\s*(?:final_review|simple_edit)\b", text)):
        return "review"
    return "other"


_ROLE_CANONICAL_OUTPUTS = {
    "research": "research/research.md",
    "image": "assets/catalog.json",
}
_OUTPUT_DECLARATION_RE = re.compile(
    r"(?i)(?:output|deliverable|artifact|write|produce|save|"
    r"产出|输出|写入|写到|保存|唯一产物|正式产物)"
)
_MARKDOWN_WORKSPACE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:(?:plan|research)/[A-Za-z0-9._/-]+\.md|"
    r"materials/summaries/[A-Za-z0-9._-]+\.md|"
    r"assets/[A-Za-z0-9._/-]+\.(?:md|json)))\b",
    re.I,
)


def _role_output_contract_error(tasks):
    """Reject a task goal that tries to rename a role's canonical artifact."""
    for task in tasks:
        kind = _task_kind(task.get("label"), task.get("goal"))
        canonical = _ROLE_CANONICAL_OUTPUTS.get(kind)
        text = f"{task.get('goal') or ''}\n{task.get('context') or ''}"
        declared = set()
        for line in text.splitlines():
            if not _OUTPUT_DECLARATION_RE.search(line):
                continue
            if re.search(r"(?i)不得|不要|禁止|不能|do\s+not|must\s+not|never", line):
                continue
            declared.update(
                match.replace("\\", "/").removeprefix("./")
                for match in _MARKDOWN_WORKSPACE_PATH_RE.findall(line)
            )
        if kind == "material":
            conflicts = sorted(
                path for path in declared
                if not re.fullmatch(r"materials/summaries/[A-Za-z0-9._-]+\.md", path)
            )
            if conflicts:
                return (
                    "Material 的正式产物必须位于 "
                    "materials/summaries/<assignment_id>.md；"
                    f"任务正文不得改写为 {', '.join(conflicts)}"
                )
            continue
        if not canonical:
            continue
        conflicts = sorted(path for path in declared if path != canonical)
        if conflicts:
            return (
                f"{kind} 的正式产物固定为 {canonical}；"
                f"任务正文不得改写为 {', '.join(conflicts)}"
            )
    return None


def _role_card_context(skills_root, kind, prompt_language="zh"):
    """Point a child at exactly one role card; the child reads it autonomously."""
    if kind not in {"research", "material", "image", "slide", "review"}:
        return ""
    path = f"skills/mural-presenter/roles/{kind}.md"
    if str(prompt_language or "").lower() == "en":
        return (
            f"Your only role-card path is {path}. Before taking any task action, "
            "read the whole file yourself with read_file. If the result provides a "
            "continuation offset, keep reading until it is no longer truncated. Read "
            "only the references routed by that role card; do not scan unrelated files. "
            "The runtime validates only the final contract and does not inject the card "
            "body here.\n\n"
        )
    return (
        f"你的唯一角色卡路径是 {path}。"
        "开始任何任务动作前，必须自行用 read_file 完整读取该文件；若返回续读 offset，"
        "继续读取到不再截断。只按角色卡给出的路由读取必要 reference，不通读无关文件。"
        "系统只校验最终合同，不会在此重复角色卡正文。\n\n"
    )


def _label_from_goal(goal):
    """Parse the canonical role prefix into one stable internal trace label."""
    text = str(goal or "").strip()
    low = text.lower()
    if re.match(r"slide\s+group\b", low):
        match = re.match(r"slide\s+group\s+([^\s:\[]+)", text, re.I)
        suffix = re.sub(r"[^a-zA-Z0-9_-]+", "-", match.group(1)).strip("-") if match else "group"
        return f"slide_group_{suffix.lower()}"
    if re.match(r"slides?(?:\s+\d|\s*:)", low):
        match = re.search(r"\b(\d{1,3})\b", text)
        return f"slide_{int(match.group(1)):02d}" if match else "slide"
    for prefix in ("material", "image"):
        match = re.match(rf"{prefix}\s+([^\s:\[]+)\s*:", text, re.I)
        if match:
            suffix = re.sub(r"[^a-zA-Z0-9._-]+", "-", match.group(1)).strip("-_").lower()
            return f"{prefix}_{suffix}" if suffix else prefix
    if re.match(r"research\s*:", text, re.I):
        return "research"
    if re.match(r"review\s*:", text, re.I):
        return "review"
    return None


def _parse_page_list(raw):
    pages = set()
    value = re.sub(r"(?i)\b(?:p|page)\s*(?=\d)", "", str(raw or ""))
    value = re.sub(r"\s*(?:至|到|~|〜)\s*", "-", value)
    for part in re.split(r"\s*[,，、/;；]\s*|\s+", value.strip()):
        if not part:
            continue
        span = re.fullmatch(r"0?(\d{1,3})\s*[-–—]\s*0?(\d{1,3})", part)
        if span:
            start, end = map(int, span.groups())
            if 0 < start <= end <= 999:
                pages.update(range(start, end + 1))
        elif re.fullmatch(r"0?\d{1,3}", part) and int(part) > 0:
            pages.add(int(part))
    return pages


def _slide_group_pages(task):
    """从 Slide / Slide Group 委派文本中读取明确负责的页码。

    Skill 的标准标题是 ``Slide Group <group_id> [01, 02, ...]``。若标题缺失，
    再从明确的 ``plan/slide_NN.md`` 交接路径恢复；不从字号、颜色或尺寸中猜数字。
    """
    text = f"{task.get('goal') or ''}\n{task.get('context') or ''}"
    pages = set()
    explicit_patterns = (
        r"slide\s+group\b[^\[\n]*\[\s*([0-9pP\s,，、/;；\-–—~〜至到]+)\s*\]",
        r"(?i)\bpages?\s*[:：]?\s*([0-9pP\s,，、/;；\-–—~〜至到]+)",
        r"(?:页码|负责页面|处理页面)\s*[:：]?\s*([0-9pP\s,，、/;；\-–—~〜至到]+)",
        r"(?:负责|处理|制作|完成)\s*第?\s*([0-9pP\s,，、/;；\-–—~〜至到]+)\s*页(?:面)?",
        r"第\s*([0-9pP\s,，、/;；\-–—~〜至到]+)\s*页(?:面)?",
    )
    for pattern in explicit_patterns:
        match = re.search(pattern, text, re.I)
        if match:
            pages.update(_parse_page_list(match.group(1)))
            if pages:
                break
    if not pages:
        pages.update(int(n) for n in re.findall(r"plan/slide_(\d{1,3})\.md\b", text, re.I))
    if not pages:
        single = re.search(r"\bslide\s+0?(\d{1,3})\b", text, re.I)
        if single and int(single.group(1)) > 0:
            pages.add(int(single.group(1)))
    return sorted(pages)


def _slide_group_page_count(task):
    """Return the number of explicitly assigned pages for budget scaling."""
    pages = _slide_group_pages(task)
    return max(1, len(pages))


def _slide_pixel_state(agent, assigned_pages):
    """Return machine-derived final-pixel coverage for one Slide worker.

    A page is current when the worker inspected either that exact final PNG or
    a focus contact sheet whose immutable sidecar snapshots that PNG.  In both
    cases neither the page HTML nor shared CSS may be newer than the render.
    This lets a Slide Group establish full coverage with one overview while
    still opening suspicious pages individually.
    """
    assigned = sorted({int(page) for page in (assigned_pages or []) if int(page) > 0})
    evidence = getattr(agent, "vision_evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}

    def evidence_for(relative_path):
        normalized = relative_path.replace("\\", "/")
        return next(
            (value for path, value in evidence.items()
             if str(path).replace("\\", "/").endswith(normalized)),
            None,
        )

    def current_file(relative_path, item):
        path = os.path.join(agent.ws, relative_path)
        try:
            with open(path, "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            return False, 0
        valid = (
            isinstance(item, dict)
            and item.get("sha256") == digest
            and int(item.get("mtime_ns") or 0) == mtime
        )
        return valid, mtime

    def current_page(page, snapshot=None):
        rel = f"renders/slide_{page:02d}.png"
        png = os.path.join(agent.ws, rel)
        html = os.path.join(agent.ws, "slides", f"slide_{page:02d}.html")
        css = os.path.join(agent.ws, "base.css")
        try:
            if not os.path.isfile(html):
                return False
            with open(png, "rb") as stream:
                current_digest = hashlib.sha256(stream.read()).hexdigest()
            current_mtime = os.stat(png).st_mtime_ns
            source_mtime = max(
                (os.stat(path).st_mtime_ns for path in (html, css) if os.path.isfile(path)),
                default=0,
            )
        except OSError:
            return False
        if source_mtime > current_mtime:
            return False
        if snapshot is None:
            snapshot = evidence_for(rel)
        return (
            isinstance(snapshot, dict)
            and snapshot.get("sha256") == current_digest
            and int(snapshot.get("mtime_ns") or 0) == current_mtime
        )

    page_checks = {}
    vision_paths = [str(path).replace("\\", "/")
                    for path in getattr(agent, "vision_paths", [])]
    for path in vision_paths:
        match = re.search(r"(?:^|/)renders/slide_(\d+)\.png$", path, re.I)
        if not match:
            continue
        page = int(match.group(1))
        if assigned and page not in assigned:
            continue
        page_checks.setdefault(page, []).append(current_page(page))

    for path in vision_paths:
        match = re.search(
            r"(?:^|/)(renders/contact-sheet-focus-[^/]+)\.png$", path, re.I,
        )
        if not match:
            continue
        sheet_rel = match.group(1) + ".png"
        sheet_ok, sheet_mtime = current_file(sheet_rel, evidence_for(sheet_rel))
        if not sheet_ok:
            continue
        sidecar = os.path.join(agent.ws, match.group(1) + ".json")
        try:
            payload = json.loads(Path(sidecar).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        snapshots = {
            int(item.get("page")): item
            for item in payload.get("evidence", [])
            if isinstance(item, dict) and str(item.get("page") or "").isdigit()
        }
        for page in payload.get("pages", []):
            try:
                page = int(page)
            except (TypeError, ValueError):
                continue
            if assigned and page not in assigned:
                continue
            snapshot = snapshots.get(page)
            snapshot_mtime = int((snapshot or {}).get("mtime_ns") or 0)
            page_checks.setdefault(page, []).append(
                current_page(page, snapshot) and snapshot_mtime <= sheet_mtime
            )

    inspected = sorted(page_checks)
    stale = sorted(page for page, checks in page_checks.items() if not any(checks))
    dirty = sorted(
        os.path.relpath(path, agent.ws).replace(os.sep, "/")
        for path in getattr(agent, "_dirty_visual_sources", set())
    )
    return {
        "inspected_pages": inspected,
        "missing_pages": sorted(set(assigned) - set(inspected)),
        "stale_pages": sorted(set(stale)),
        "dirty_sources": dirty,
    }


def _review_pixel_state(agent):
    """Return current final-review coverage before the Review worker may stop.

    ``deck.py build`` is pixel-immutable: prepare has already frozen fonts and
    runtime assets, while Review has already rendered and inspected the final
    pages.  Freshness therefore follows content hashes and also requires every
    current PNG to be newer than its HTML and shared styles.  Any changed PNG
    invalidates the earlier visual judgment instead of asking build to repair it.
    """
    workspace = str(getattr(agent, "ws", "") or "")
    if not workspace:
        return {
            "inspected_pages": [], "missing_pages": [],
            "stale_pages": [], "dirty_sources": [],
        }
    manifest_path = os.path.join(workspace, "renders", "review-contact.json")
    try:
        full = (json.loads(Path(manifest_path).read_text(encoding="utf-8"))
                .get("full") or {})
    except (OSError, ValueError, TypeError, AttributeError):
        return {
            "inspected_pages": [], "missing_pages": [],
            "stale_pages": [], "dirty_sources": [],
        }

    expected_set = set()
    for raw_page in full.get("pages") or []:
        try:
            page = int(raw_page)
        except (TypeError, ValueError):
            continue
        if page > 0:
            expected_set.add(page)
    expected = sorted(expected_set)
    if not expected:
        return {
            "inspected_pages": [], "missing_pages": [],
            "stale_pages": [], "dirty_sources": [],
        }
    groups = {
        str(item.get("path") or "").replace("\\", "/").lstrip("./"): {
            int(page) for page in item.get("pages") or []
        }
        for item in full.get("groups") or []
        if isinstance(item, dict) and item.get("path")
    }
    evidence = getattr(agent, "vision_evidence", {})
    if not isinstance(evidence, dict):
        evidence = {}

    def normalize(path):
        raw = str(path or "")
        if os.path.isabs(raw):
            raw = os.path.relpath(raw, workspace)
        return raw.replace("\\", "/").lstrip("./")

    def evidence_for(relative_path):
        wanted = normalize(relative_path)
        return next(
            (item for path, item in evidence.items() if normalize(path) == wanted),
            None,
        )

    def digest_matches(relative_path, item):
        try:
            with open(os.path.join(workspace, relative_path), "rb") as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
        except OSError:
            return False
        return isinstance(item, dict) and item.get("sha256") == digest

    shared_sources = [
        os.path.join(workspace, "base.css"),
        os.path.join(workspace, "plan", "theme.css"),
    ]

    def page_sources_are_rendered(page):
        png = os.path.join(workspace, "renders", f"slide_{page:02d}.png")
        html = os.path.join(workspace, "slides", f"slide_{page:02d}.html")
        try:
            png_mtime = os.stat(png).st_mtime_ns
            source_mtime = max(
                (os.stat(path).st_mtime_ns for path in [html, *shared_sources]
                 if os.path.isfile(path)),
                default=0,
            )
        except OSError:
            return False
        return source_mtime <= png_mtime

    page_checks = {}
    vision_paths = [normalize(path) for path in getattr(agent, "vision_paths", [])]
    for path in vision_paths:
        match = re.search(r"(?:^|/)renders/slide_(\d+)\.png$", path, re.I)
        if not match:
            continue
        page = int(match.group(1))
        if page not in expected:
            continue
        page_checks.setdefault(page, []).append(
            page_sources_are_rendered(page)
            and digest_matches(f"renders/slide_{page:02d}.png", evidence_for(path))
        )

    for path in vision_paths:
        pages = groups.get(path)
        if not pages:
            continue
        sheet_current = digest_matches(path, evidence_for(path))
        for page in pages:
            if page in expected:
                page_checks.setdefault(page, []).append(
                    sheet_current and page_sources_are_rendered(page)
                )

    # Focus sheets are also valid Review evidence when their immutable sidecar
    # proves that every included page matches the current PNG bytes.
    for path in vision_paths:
        match = re.search(
            r"(?:^|/)(renders/contact-sheet-focus-[^/]+)\.png$", path, re.I,
        )
        if not match or not digest_matches(path, evidence_for(path)):
            continue
        try:
            payload = json.loads(Path(
                os.path.join(workspace, match.group(1) + ".json")
            ).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        snapshots = {
            int(item.get("page")): item
            for item in payload.get("evidence", [])
            if isinstance(item, dict) and str(item.get("page") or "").isdigit()
        }
        for raw_page in payload.get("pages", []):
            try:
                page = int(raw_page)
            except (TypeError, ValueError):
                continue
            if page not in expected:
                continue
            page_checks.setdefault(page, []).append(
                page_sources_are_rendered(page)
                and digest_matches(
                    f"renders/slide_{page:02d}.png", snapshots.get(page)
                )
            )

    inspected = sorted(page_checks)
    stale = sorted(page for page, checks in page_checks.items() if not any(checks))
    dirty = []
    for page in expected:
        if page_sources_are_rendered(page):
            continue
        dirty.append(f"slides/slide_{page:02d}.html")
    return {
        "inspected_pages": inspected,
        "missing_pages": sorted(set(expected) - set(inspected)),
        "stale_pages": stale,
        "dirty_sources": dirty,
    }


def _slide_timeout_budget(task):
    pages = _slide_group_page_count(task)
    return _scaled_timeout(
        SLIDE_WORKER_TIMEOUT_BASE,
        SLIDE_WORKER_TIMEOUT_PER_EXTRA_PAGE,
        pages - 1,
        SLIDE_WORKER_TIMEOUT_CAP,
    )


def _build_child(parent, task):
    """从 parent 构造子 Agent；内部路由字段均由 goal 前缀派生。
    每次尝试拿到唯一的 sub_dir + label(重做 → `<label>_r2`…),重做不覆盖上次的轨迹/快照。"""
    base = task.get("label")
    with parent._spawn_lock:
        if not base:
            parent._child_seq = getattr(parent, "_child_seq", 0) + 1
            base = f"child_{parent._child_seq:02d}"
        c = parent._spawn_count.get(base, 0) + 1
        parent._spawn_count[base] = c
    name = base + ("" if c == 1 else f"_r{c}")

    toolsets = tools.normalize_toolset_names(
        task.get("toolsets") or ["file", "terminal", "vision"]
    )
    # role=orchestrator 且深度还允许再派时,才补 delegation 能力。
    if task.get("role") == "orchestrator" and parent._delegate_depth + 1 < MAX_SPAWN_DEPTH \
            and "delegation" not in toolsets:
        toolsets = toolsets + ["delegation"]
    schema = tools.resolve_toolsets(toolsets)
    have = {s["name"] for s in schema}             # 基础工具(read)默认并入
    schema = [tools.SCHEMAS[n] for n in tools.BASE_TOOL_NAMES if n not in have] + schema

    context = task.get("context") or ""
    # 角色标签只用于范围识别和轨迹展示；最终回复完全遵循角色卡合同。
    prompt_language = str(
        parent.cfg.get("_prompt_language") or getattr(parent, "prompt_language", "")
    ).lower()
    if prompt_language == "en":
        ident = f"Your role label is {name}. Do not repeat it outside the role-card contract.\n\n"
        context_label = "Context"
    else:
        ident = f"你的角色标签是 {name}；最终回复只遵循角色卡合同，不在合同外复述标签。\n\n"
        context_label = "背景"
    kind = _task_kind(name, task.get("goal"))
    role_card = _role_card_context(parent.skills_root, kind, prompt_language)
    initial = ident + _child_language_contract(prompt_language) + role_card + task["goal"] + (
        f"\n\n{context_label}:\n{context}" if context else ""
    )
    # 叶子子 agent 单回合无需生成整册长文；若输出窗口不足，run_loop 会从已完成动作续跑。
    child_cfg = parent.cfg
    if "delegation" not in toolsets:
        _cap = min(int(parent.cfg.get("max_tokens", 16000)), SUBAGENT_MAX_TOKENS)
        child_cfg = dict(parent.cfg); child_cfg["max_tokens"] = _cap
    # Slide owns only its assigned page HTML and render evidence.  Review may
    # fix slides/base.css/speech, but neither role may rewrite the truth sources
    # it is supposed to consume or audit.
    child = Agent(role="subagent", sid=parent.sid, ws=parent.ws,
                  sub_dir=f"subagents/{name}", tools_schema=schema,
                  config=child_cfg, initial_user=initial, label=name,
                  system=getattr(parent, "child_system", parent.base_system),
                  skills_root=parent.skills_root,
                  forbid_write_prefixes=_ROLE_WRITE_BOUNDARIES.get(kind, []),
                  parent_main_trajectory_id=(
                      parent.nova_raw.main_trajectory_id
                      if parent.nova_raw is not None else ""
                  ),
                  root_main_trajectory_id=(
                      parent.nova_raw.root_main_trajectory_id
                      if parent.nova_raw is not None else ""
                  ),
                  delegation_depth=getattr(parent, "delegation_depth", 0) + 1)
    child._delegate_depth = parent._delegate_depth + 1
    child._required_role_card_path = (
        f"skills/mural-presenter/roles/{kind}.md"
        if kind in {"research", "material", "image", "slide", "review"}
        else ""
    )
    child._assigned_pages = _slide_group_pages(task) if kind == "slide" else []
    if kind == "review":
        review_text = f"{task.get('goal') or ''}\n{task.get('context') or ''}"
        child._expected_review_mode = (
            "simple_edit" if re.search(r"\bmode\s*=\s*simple_edit\b", review_text, re.I)
            else "final_review"
        )
    return child, name


_TRANSPARENCY_REQUEST_RE = re.compile(
    r"(?i)(?:subject[_ -]?only\s*[:=]\s*true|expect[_ -]?transparent\s*[:=]\s*true|"
    r"transparent(?:[-_ ]background)?|alpha\s+channel|透明背景|主体透明|透明元素|抠图|去背)"
)
_TRANSPARENT_ASSETS_RE = re.compile(
    r"(?im)^\s*transparent_assets\s*:\s*(.+?)\s*$"
)


def _image_transparency_error(ws, task, name, contract, final_text, vision_paths):
    """Reject an Image ``ready`` that did not deliver verified Alpha assets.

    This is deliberately narrow: it only activates when the original Image task
    explicitly requests a transparent subject.  The paired Skill owns the image
    semantics; the Harness merely verifies the claimed local artifact and the
    fact that Image actually inspected that exact derived file.
    """
    if not str(name).lower().startswith("image"):
        return None
    request = f"{task.get('goal') or ''}\n{task.get('context') or ''}"
    if not _TRANSPARENCY_REQUEST_RE.search(request):
        return None
    if str(contract.get("status") or "").lower() != "ready":
        return None
    match = _TRANSPARENT_ASSETS_RE.search(final_text or "")
    if match:
        raw_paths = [
            value.strip().strip("`'\"")
            for value in re.split(r"\s*[,，;；]\s*", match.group(1))
            if value.strip()
        ]
    else:
        # 弱模型偶尔会在自然语言里明确交付唯一 cutout，却漏了键名。
        # 允许从最终回复提取路径，但下方 Alpha/存在性/精确 Vision 校验一项不少。
        raw_paths = sorted(set(re.findall(
            r"(?<![A-Za-z0-9_.-])(assets/[A-Za-z0-9_./-]+-cutout\.png)",
            final_text or "",
        )))
        if not raw_paths:
            return "缺少 transparent_assets；需要透明主体的任务不能仅返回普通 RGB path"
    if not raw_paths or any(not path.endswith("-cutout.png") for path in raw_paths):
        return "transparent_assets 必须全部是 assets/*-cutout.png 派生文件"

    root = os.path.realpath(ws)
    inspected = {
        os.path.normpath(str(path).strip())
        for path in (vision_paths or [])
    }
    from PIL import Image
    for relative in raw_paths:
        normalized = os.path.normpath(relative)
        if normalized.startswith("../") or not normalized.startswith("assets/"):
            return f"透明素材路径不在 assets/：{relative}"
        absolute = os.path.realpath(os.path.join(root, normalized))
        if not absolute.startswith(root + os.sep) or not os.path.isfile(absolute):
            return f"透明素材不存在：{relative}"
        try:
            with Image.open(absolute) as image:
                if image.format != "PNG" or "A" not in image.getbands():
                    return f"透明素材不是带 Alpha 的 PNG：{relative}"
                alpha = image.getchannel("A")
                low, high = alpha.getextrema()
                if low >= 250 or high <= 16:
                    return f"透明素材没有有效的前景/透明区域：{relative}"
        except OSError as error:
            return f"透明素材无法解析：{relative}（{type(error).__name__}）"
        if normalized not in inspected and f"./{normalized}" not in inspected:
            return f"Image 未用 vision_analyze 查看最终透明素材：{relative}"
    return None


_HANDOFF_ARTIFACT_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:research|plan|slides|renders|assets|materials)/"
    r"[^\s`'\"<>]+",
    re.I,
)


def _child_handoff(parent, child, name, clean, contract):
    """Persist the exact final response, but return only a lossless compact handoff.

    The parent needs verdict fields and artifact locations, not the child's full
    reasoning/tool history.  Keeping the exact final response in handoff.json
    avoids the old 240-character truncation without pushing messages.json back
    into the Orchestrator context.
    """
    final_text = child.final_text or ""
    artifacts = []
    for match in _HANDOFF_ARTIFACT_RE.finditer(final_text):
        value = match.group(0).rstrip(".,;，；:：)]}>")
        if value not in artifacts:
            artifacts.append(value)
    trace_rel = os.path.relpath(child.trace.sub_dir, parent.ws).replace(os.sep, "/")
    handoff_rel = f"{trace_rel}/handoff.json"
    payload = {
        "label": name,
        "clean": bool(clean),
        "exit_reason": child.exit_reason,
        "contract": contract,
        "artifacts": artifacts,
        "final_response": final_text,
    }
    handoff_path = os.path.join(child.trace.sub_dir, "handoff.json")
    temporary = handoff_path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, handoff_path)
    return {
        "label": name,
        "status": "ok" if clean else "issues",
        "exit_reason": child.exit_reason,
        "contract": contract,
        "artifacts": artifacts,
        "handoff_path": handoff_rel,
        "renders": child.n_renders,
        "vision_calls": child.n_vision_calls,
        "shot": child.last_shot,
    }


_PRODUCTION_GROUP_RE = re.compile(
    r"(?mi)^\s*[-*+]\s*(?:\*\*)?production_group(?:\*\*)?\s*[:：]\s*`?([A-Za-z0-9._-]+)"
)


def _planned_production_groups(ws):
    """Return the frozen page sets from plan/slide_NN.md.

    Older/revision workspaces with no group field remain readable.  Once any
    plan declares the field, every page must declare exactly one group so the
    dispatcher cannot silently turn a multi-page group into per-page workers.
    """
    pages = []
    for path in sorted(glob.glob(os.path.join(ws, "plan", "slide_*.md"))):
        match = re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)
        if not match:
            continue
        with open(path, encoding="utf-8") as stream:
            group_matches = _PRODUCTION_GROUP_RE.findall(stream.read())
        pages.append((int(match.group(1)), sorted(set(group_matches))))
    if not pages or not any(groups for _, groups in pages):
        return {}
    missing = [page for page, groups in pages if not groups]
    ambiguous = {page: groups for page, groups in pages if len(groups) != 1}
    if missing or ambiguous:
        raise ValueError(
            "每份逐页计划必须声明唯一 production_group；"
            f"missing={missing[:20]} ambiguous={dict(list(ambiguous.items())[:8])}"
        )
    result = {}
    for page, groups in pages:
        result.setdefault(groups[0], set()).add(page)
    return result


def _group_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _task_group_id(task, planned_groups):
    keyed = {}
    for group_id in planned_groups:
        keyed.setdefault(_group_key(group_id), []).append(group_id)
    candidates = []
    label = str(task.get("label") or "")
    for prefix in ("slide_group_", "slide-group-"):
        if label.lower().startswith(prefix):
            candidates.append(label[len(prefix):])
    goal = str(task.get("goal") or "")
    match = re.search(r"slide\s+group\s+([A-Za-z0-9._-]+)", goal, re.I)
    if match:
        candidates.append(match.group(1))
    for candidate in candidates:
        matches = keyed.get(_group_key(candidate), [])
        if len(matches) == 1:
            return matches[0]
    page_set = set(_slide_group_pages(task))
    exact = [group_id for group_id, pages in planned_groups.items() if page_set == set(pages)]
    return exact[0] if len(exact) == 1 else None


def _expected_slide_tasks(ws):
    try:
        groups = _planned_production_groups(ws)
    except ValueError:
        return []
    return [
        {
            "goal": (
                f"Slide Group {group_id} "
                f"[{','.join(f'{page:02d}' for page in sorted(pages))}]: "
                "execute the frozen Production Group"
            ),
        }
        for group_id, pages in sorted(groups.items(), key=lambda item: min(item[1]))
    ]


def _material_before_research_error(parent, tasks):
    """Require attachment understanding before an attachment-grounded Research.

    Material workers may parse independent attachment shards in parallel.  Research
    is a later evidence-gap stage: it may start only after every active Material
    handoff is ready, the staged attachment catalog is complete, and the Orchestrator
    has read each canonical summary to EOF.
    """
    kinds = {_task_kind(task.get("label"), task.get("goal")) for task in tasks}
    if "research" not in kinds:
        return None
    has_attachments = bool(
        int((getattr(parent, "generation_preferences", {}) or {}).get("attachment_count") or 0)
    )
    if not has_attachments:
        return None
    if "material" in kinds:
        return (
            "存在用户附件时，Material 与 Research 不能同批委派。"
            "先并行完成全部 Material 分片并读取正式摘要，再决定 Research 缺口。"
        )

    spawn_lock = getattr(parent, "_spawn_lock", None)
    if spawn_lock is None:
        workers = list(getattr(parent, "worker_recs", []) or [])
    else:
        with spawn_lock:
            workers = list(getattr(parent, "worker_recs", []) or [])
    latest = {}
    for worker in workers:
        kind = str(worker.get("kind") or "").lower()
        label = str(worker.get("label") or "").lower()
        if kind != "material" and not label.startswith("material"):
            continue
        key = re.sub(r"_r\d+$", "", label or "material")
        latest[key] = worker
    if not latest:
        return "存在用户附件，但 Material 尚未完成；Research 不能提前启动。"

    outputs = []
    bad = []
    for label, worker in sorted(latest.items()):
        contract = worker.get("contract") or {}
        status = str(contract.get("status") or "").strip().lower()
        coverage = str(contract.get("coverage") or "").strip().lower()
        output = str(contract.get("output") or "").strip().replace("\\", "/")
        output = output.removeprefix("./")
        valid_output = bool(re.fullmatch(
            r"materials/summaries/[A-Za-z0-9._-]+\.md", output
        ))
        output_path = os.path.abspath(os.path.join(parent.ws, output)) if output else ""
        within_ws = bool(output_path) and os.path.commonpath(
            [os.path.abspath(parent.ws), output_path]
        ) == os.path.abspath(parent.ws)
        if (
            not worker.get("clean")
            or status != "ready"
            or not coverage.startswith("complete")
            or not valid_output
            or not within_ws
            or not os.path.isfile(output_path)
        ):
            bad.append(label)
            continue
        outputs.append(output)
    if bad:
        return f"Material 尚未 ready/complete 或正式摘要无效: {bad[:5]}"

    manifest_path = os.path.join(parent.ws, "materials", "attachments.json")
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        expected = {
            str(item.get("name") or "")
            for item in (manifest.get("attachments") or [])
            if item.get("name")
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return "附件清单 materials/attachments.json 尚未形成或无法读取。"
    if not expected:
        return "附件清单为空，不能确认 Material 已覆盖用户输入。"

    covered = set()
    for catalog_path in glob.glob(
        os.path.join(parent.ws, "materials", "_work", "*", "catalog.json")
    ):
        try:
            catalog = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(catalog, list):
            continue
        for entry in catalog:
            name = str(entry.get("name") or "")
            if name not in expected:
                continue
            coverage = entry.get("coverage") or {}
            if (
                str(entry.get("status") or "").lower() == "ok"
                and str(coverage.get("status") or "").lower() == "complete"
            ):
                covered.add(name)
    missing_attachments = sorted(expected - covered)
    if missing_attachments:
        return "Material 尚未完整覆盖附件: " + ", ".join(missing_attachments[:5])

    completed_reads = set(getattr(parent, "_completed_read_paths", set()) or set())
    unread = [path for path in sorted(set(outputs)) if tools._read_key(path) not in completed_reads]
    if unread:
        return (
            "启动 Research 前，编排器必须把全部 Material 正式摘要读到末尾；未完成: "
            + ", ".join(unread)
        )

    for task in tasks:
        if _task_kind(task.get("label"), task.get("goal")) != "research":
            continue
        goal = str(task.get("goal") or "")
        has_raw_query = bool(re.search(
            r"(?i)(?:raw\s+user\s+query|原始\s*(?:user\s*)?query|用户原文)\s*[:：]",
            goal,
        ))
        has_gap = bool(re.search(
            r"(?i)(?:evidence\s+needed|evidence\s+gaps?|证据缺口|待核命题)\s*[:：]",
            goal,
        ))
        missing_paths = [path for path in sorted(set(outputs)) if path not in goal]
        if not has_raw_query or not has_gap or missing_paths:
            return (
                "Research goal 必须包含原始 query、明确证据缺口和全部 Material 摘要路径；"
                f"missing_paths={missing_paths[:5]}"
            )
    return None


def _planning_before_slide_error(parent, tasks):
    """Require the frozen per-page plan and prepare outputs before Slide work.

    A deck-level map or a verbose delegation prompt is not a substitute for the
    canonical ``plan/slide_NN.md`` handoffs.  This gate prevents a weak
    Orchestrator from silently asking one Slide worker to invent missing pages.
    """
    if not any(
        _task_kind(task.get("label"), task.get("goal")) == "slide"
        for task in tasks
    ):
        return None
    preferences = getattr(parent, "generation_preferences", {}) or {}
    try:
        expected = int(preferences.get("page_count") or 0)
    except (TypeError, ValueError):
        expected = 0
    revision_mode = bool((getattr(parent, "cfg", {}) or {}).get("_revision_mode"))
    required = ["plan/design-brief.md", "plan/deck.md", "base.css", "speech.md"]
    if not revision_mode:
        required.append("plan/theme.css")
    missing = [path for path in required if not os.path.isfile(os.path.join(parent.ws, path))]
    plans = []
    for path in glob.glob(os.path.join(parent.ws, "plan", "slide_*.md")):
        match = re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)
        if match and os.path.getsize(path) > 0:
            plans.append(int(match.group(1)))
    if expected > 0:
        expected_pages = set(range(1, expected + 1))
        actual_pages = set(plans)
        if actual_pages != expected_pages:
            missing_pages = sorted(expected_pages - actual_pages)
            extra_pages = sorted(actual_pages - expected_pages)
            detail = []
            if missing_pages:
                detail.append("missing_pages=" + ",".join(f"{page:02d}" for page in missing_pages))
            if extra_pages:
                detail.append("extra_pages=" + ",".join(f"{page:02d}" for page in extra_pages))
            missing.append("plan/slide_NN.md (" + "; ".join(detail) + ")")
    elif not plans:
        missing.append("plan/slide_NN.md")
    if missing:
        return (
            "Slide Group 启动前规划尚未冻结：" + ", ".join(missing[:20])
            + "。先完成全部逐页计划并运行一次 deck.py prepare；"
              "不得用委派 prompt 临时替代逐页交接。"
        )
    if not revision_mode:
        try:
            groups = _planned_production_groups(parent.ws)
        except ValueError as error:
            return str(error)
        # Short does not mean serial.  From four pages onward the cover/closing
        # relationship and the content-making problem already form at least
        # two independent units.  This is a scheduling invariant rather than
        # an aesthetic page cap; each group may still contain any sensible
        # number of related pages.
        if len(set(plans)) >= 4 and len(groups) < 2:
            return (
                "四页以上的新建 Deck 至少需要两个互不重叠的 production_group，"
                "以便同批并行制作；常见拆法是 bookends 与 content。"
                "不要把整册塞给一个 Slide Group，也不要机械拆成每页一个 Agent。"
            )
    return None


def _canonicalize_slide_tasks(parent, tasks):
    """Resolve Slide ownership from the frozen plan, not repeated prose.

    Common legacy page expressions are accepted.  Per-page tasks that together
    cover one frozen group are deterministically coalesced into one worker so a
    weak Orchestrator cannot silently discard its own grouping decision.
    """
    if bool((getattr(parent, "cfg", {}) or {}).get("_revision_mode")):
        return tasks, None
    try:
        planned_groups = _planned_production_groups(parent.ws)
    except ValueError as error:
        return tasks, str(error)
    if not planned_groups:
        return tasks, None
    slide_tasks = [
        task for task in tasks
        if _task_kind(task.get("label"), task.get("goal")) == "slide"
    ]
    if not slide_tasks:
        return tasks, None
    page_to_group = {
        page: group_id
        for group_id, pages in planned_groups.items()
        for page in pages
    }
    # Resolve every represented task to its frozen group before checking the
    # scheduling wave below.  The first Slide dispatch must contain all groups;
    # later calls are reserved for resuming an already recorded failed owner.
    buckets = {}
    for task in slide_tasks:
        parsed_pages = set(_slide_group_pages(task))
        group_id = _task_group_id(task, planned_groups)
        resolved_by_id = group_id is not None
        if group_id is None:
            groups = {page_to_group.get(page) for page in parsed_pages}
            if not parsed_pages or None in groups or len(groups) != 1:
                return tasks, (
                    "Slide 委派无法映射到唯一冻结 production_group；"
                    f"task={task.get('label') or 'slide'} parsed_pages={sorted(parsed_pages)}"
                )
            group_id = next(iter(groups))
        planned_pages = set(planned_groups[group_id])
        if parsed_pages and not parsed_pages.issubset(planned_pages):
            return tasks, (
                f"Slide 委派的组 ID 与页码冲突: group={group_id} "
                f"parsed={sorted(parsed_pages)} planned={sorted(planned_pages)}"
            )
        buckets.setdefault(group_id, []).append((task, parsed_pages, resolved_by_id))

    spawn_lock = getattr(parent, "_spawn_lock", None)
    if spawn_lock is not None:
        with spawn_lock:
            prior_workers = list(getattr(parent, "worker_recs", []) or [])
    else:
        prior_workers = list(getattr(parent, "worker_recs", []) or [])
    has_prior_slide = any(
        str(worker.get("kind") or "").lower() == "slide"
        or str(worker.get("label") or "").lower().startswith("slide")
        for worker in prior_workers
    )
    represented = set(buckets)
    missing_first_wave = sorted(set(planned_groups) - represented)
    if not has_prior_slide and missing_first_wave:
        return tasks, (
            "首次 Slide 派发必须在同一个 delegate_task 中包含全部冻结 Production Groups，"
            "由 Harness 按并发槽位同时启动或排队；缺少："
            + ", ".join(missing_first_wave)
        )

    canonical = []
    for group_id, entries in sorted(buckets.items(), key=lambda item: min(planned_groups[item[0]])):
        planned_pages = set(planned_groups[group_id])
        parsed_union = set().union(*(pages for _, pages, _ in entries))
        # Even a legacy per-page request is expanded to the complete frozen
        # group.  This preserves the plan's grouping decision and prevents a
        # later worker from independently redesigning another page in the same
        # group.
        page_list = ",".join(f"{page:02d}" for page in sorted(planned_pages))
        original_goals = [str(task.get("goal") or "").strip() for task, _, _ in entries]
        original_contexts = [str(task.get("context") or "").strip() for task, _, _ in entries]
        merged_toolsets = []
        for task, _, _ in entries:
            for toolset in task.get("toolsets") or []:
                if toolset not in merged_toolsets:
                    merged_toolsets.append(toolset)
        canonical.append({
            "label": "slide_group_" + re.sub(
                r"[^A-Za-z0-9_.-]+", "-", group_id
            ).strip("-").lower(),
            "goal": (
                f"Slide Group {group_id} [{page_list}]: complete every page in this "
                "frozen Production Group.\n\nOriginal directives:\n"
                + "\n---\n".join(goal for goal in original_goals if goal)
            ),
            "context": "\n\n".join(value for value in original_contexts if value),
            "toolsets": merged_toolsets or ["file", "terminal", "vision"],
            "role": "leaf",
        })
    non_slides = [
        task for task in tasks
        if _task_kind(task.get("label"), task.get("goal")) != "slide"
    ]
    return non_slides + canonical, None


def _grounding_before_downstream_error(parent, tasks):
    """Keep evidence handoff ordered before Image/Slide/Review production.

    This is a narrow state invariant, not a presentation rule: when the run has
    attachments or has executed Material/Research, downstream workers must read
    the one canonical grounding file.  Rejecting the premature dispatch gives
    the Orchestrator an immediate, local correction instead of failing only at
    the very end of an otherwise expensive deck.
    """
    kinds = {_task_kind(task.get("label"), task.get("goal")) for task in tasks}
    downstream = kinds.intersection({"image", "slide", "review"})
    if not downstream:
        return None
    if kinds.intersection({"material", "research"}):
        return (
            "Material/Research 与下游 Image/Slide/Review 不能同批委派；"
            "先回收证据合同，写入并验证 plan/grounded-knowledge.md，再继续。"
        )
    with parent._spawn_lock:
        workers = list(parent.worker_recs)
    evidence_workers = [
        worker for worker in workers
        if str(worker.get("kind") or "").lower() in {"material", "research"}
        or str(worker.get("label") or "").lower().startswith(("material", "research"))
    ]
    has_attachments = bool(
        int((getattr(parent, "generation_preferences", {}) or {}).get("attachment_count") or 0)
    )
    revision_mode = bool((getattr(parent, "cfg", {}) or {}).get("_revision_mode"))
    if has_attachments and not any(
            str(worker.get("kind") or "").lower() == "material"
            or str(worker.get("label") or "").lower().startswith("material")
            for worker in evidence_workers) and not revision_mode:
        return "存在用户附件，但 Material 尚未完成；不能提前委派下游制作。"
    if not evidence_workers and not has_attachments:
        return None
    failed = [
        str(worker.get("label") or "evidence")
        for worker in evidence_workers
        if not worker.get("clean")
    ]
    if failed:
        return f"Material/Research 未干净完成: {failed[:5]}"
    grounded = os.path.join(parent.ws, "plan", "grounded-knowledge.md")
    try:
        valid = os.path.isfile(grounded) and os.path.getsize(grounded) >= 40
    except OSError:
        valid = False
    if not valid:
        return (
            "Material/Research 已回收，但缺少有效 plan/grounded-knowledge.md。"
            "请立即综合用户事实、外部核验、假设与 unresolved，read_file 验证后再委派。"
        )
    return None


_PLANNED_ASSET_ID_RE = re.compile(
    r"(?i)\basset[_ -]?id\b\s*[:=]\s*[`'\"]?([A-Za-z0-9._-]+)"
)
_PLANNED_IMAGE_NEED_RE = re.compile(
    r"(?im)^\s*[-*]?\s*image_opportunity\s*:\s*(?!none\b|code(?:_only)?\b|no\b)(.+)$"
)


def _planned_image_dependencies(ws):
    asset_ids, needs_image = set(), False
    for plan in glob.glob(os.path.join(ws, "plan", "slide_*.md")):
        try:
            text = Path(plan).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        asset_ids.update(_PLANNED_ASSET_ID_RE.findall(text))
        needs_image = needs_image or bool(_PLANNED_IMAGE_NEED_RE.search(text))
    return asset_ids, needs_image


def _image_before_slide_error(parent, tasks):
    """Keep the Skill's asset-first production order executable.

    Image and Slide cannot share one synchronous child wave. When plans name
    bitmap assets, every referenced ID must already resolve to a local ready
    catalog entry before any Slide Group starts. Revision runs may reuse a
    previously accepted catalog; new runs must also have a clean Image record.
    """
    kinds = {_task_kind(task.get("label"), task.get("goal")) for task in tasks}
    if "slide" not in kinds:
        return None
    if "image" in kinds:
        return (
            "Image 与 Slide Group 不能同批委派；先完成全部图片分组并回收 ready 合同，"
            "再启动页面组。"
        )
    asset_ids, needs_image = _planned_image_dependencies(parent.ws)
    if needs_image and not asset_ids:
        return "逐页计划声明需要图片，但尚未分配稳定 asset_id。"
    if not asset_ids:
        return None
    with parent._spawn_lock:
        workers = list(parent.worker_recs)
    image_workers = [
        worker for worker in workers
        if str(worker.get("kind") or "").lower() == "image"
        or str(worker.get("label") or "").lower().startswith("image")
    ]
    revision_mode = bool((getattr(parent, "cfg", {}) or {}).get("_revision_mode"))
    if not image_workers and not revision_mode:
        return "逐页计划引用图片资产，但 Image 尚未完成。"
    bad = []
    for worker in image_workers:
        contract = worker.get("contract") or {}
        status = str(contract.get("status") or "").strip().lower()
        if not worker.get("clean") or status != "ready":
            bad.append(f"{worker.get('label') or 'image'}:{status or 'missing'}")
    if bad:
        return "Image 分组尚未全部 ready: " + ", ".join(bad[:8])
    catalog_path = os.path.join(parent.ws, "assets", "catalog.json")
    try:
        payload = json.loads(Path(catalog_path).read_text(encoding="utf-8"))
        entries = payload.get("assets") or []
    except (OSError, ValueError, TypeError, AttributeError) as error:
        return f"启动 Slide Group 前无法读取 assets/catalog.json: {type(error).__name__}"
    by_id = {
        str(item.get("asset_id")): item for item in entries
        if isinstance(item, dict) and item.get("asset_id")
    }
    unresolved = []
    for asset_id in sorted(asset_ids):
        entry = by_id.get(asset_id)
        relative = str((entry or {}).get("path") or "").replace("\\", "/")
        absolute = os.path.abspath(os.path.join(parent.ws, relative)) if relative else ""
        in_workspace = bool(
            absolute
            and os.path.commonpath([os.path.abspath(parent.ws), absolute])
            == os.path.abspath(parent.ws)
        )
        if (not entry or entry.get("status") != "ready" or not in_workspace
                or not os.path.isfile(absolute)):
            unresolved.append(asset_id)
    if unresolved:
        return "Slide Group 的图片依赖尚未 ready: " + ", ".join(unresolved[:12])
    return None


def _reserve_singleton_roles(parent, tasks):
    """Reserve task-level singleton roles and unique Slide page ownership."""
    requested = {}
    slide_owners = {}
    for task in tasks:
        kind = _task_kind(task.get("label"), task.get("goal"))
        if kind in {"research", "review"}:
            requested[kind] = requested.get(kind, 0) + 1
        if kind == "slide":
            label = str(task.get("label") or "slide")
            pages = _slide_group_pages(task)
            if not pages:
                return f"{label} 未声明负责页码；Slide Group 必须显式列出唯一 pages"
            for page in pages:
                if page in slide_owners:
                    return (
                        f"Slide 页码 {page:02d} 在同一批次重复归属："
                        f"{slide_owners[page]} 与 {label}"
                    )
                slide_owners[page] = label
    duplicate = next((kind for kind, count in requested.items() if count > 1), None)
    if duplicate:
        return f"{duplicate} 是任务级单例；同一批次不能创建多个实例"
    with parent._spawn_lock:
        for kind in requested:
            if int(parent._role_spawn_count.get(kind, 0) or 0) >= 1:
                return (
                    f"{kind} 已执行过；失败、超时或 blocked 必须让当前任务如实失败，"
                    "不得创建 _r2/_r3 绕过原结果"
                )
        existing_owners = getattr(parent, "_slide_page_owners", {})
        latest_slide_attempts = {}
        for worker in getattr(parent, "worker_recs", []) or []:
            worker_kind = str(worker.get("kind") or "").lower()
            worker_label = str(worker.get("label") or "")
            if worker_kind != "slide" and not worker_label.lower().startswith("slide"):
                continue
            base = re.sub(r"_r\d+$", "", worker_label)
            latest_slide_attempts[base] = worker
        resumed = {}
        for page, label in slide_owners.items():
            if page in existing_owners:
                owner = str(existing_owners[page])
                previous = latest_slide_attempts.get(label)
                # A failed worker may be continued only by the exact same
                # canonical Production Group owner.  This keeps page ownership
                # stable while giving an incomplete group a recoverable path;
                # a different label can never overwrite the page.
                if owner == label and previous and not previous.get("clean"):
                    resumed[label] = str(previous.get("label") or label)
                    continue
                if owner == label and previous and previous.get("clean"):
                    return f"Slide 页码 {page:02d} 的 owner {label} 已完成；不得重复执行"
                return (
                    f"Slide 页码 {page:02d} 已归属 {owner}；"
                    f"不得再交给 {label} 并发覆盖"
                )
        parent_cfg = getattr(parent, "cfg", {}) or {}
        if slide_owners and not bool(parent_cfg.get("_revision_mode")):
            planned = {
                int(match.group(1))
                for path in glob.glob(os.path.join(parent.ws, "plan", "slide_*.md"))
                for match in [re.fullmatch(r"slide_(\d+)\.md", os.path.basename(path), re.I)]
                if match
            }
            if planned and planned != set(range(1, max(planned) + 1)):
                return (
                    "逐页计划页码必须从 01 连续到末页；"
                    f"actual={sorted(planned)[:30]}"
                )
            try:
                planned_groups = _planned_production_groups(parent.ws)
            except ValueError as error:
                return str(error)
            if planned_groups:
                expected_sets = {
                    frozenset(pages): group_id
                    for group_id, pages in planned_groups.items()
                }
                for task in tasks:
                    if _task_kind(task.get("label"), task.get("goal")) != "slide":
                        continue
                    pages = frozenset(_slide_group_pages(task))
                    if pages not in expected_sets:
                        return (
                            "Slide 委派必须与已冻结 production_group 逐组一致；"
                            f"task={task.get('label') or 'slide'} pages={sorted(pages)} "
                            f"planned={{{', '.join(f'{key}:{sorted(value)}' for key, value in planned_groups.items())}}}"
                        )
        for kind, count in requested.items():
            parent._role_spawn_count[kind] = int(parent._role_spawn_count.get(kind, 0) or 0) + count
        for task in tasks:
            label = str(task.get("label") or "")
            if label in resumed:
                task["_resume_of"] = resumed[label]
        existing_owners.update(slide_owners)
        parent._slide_page_owners = existing_owners
    return None


def _child_contract_error(parent, child, kind, contract):
    """Return the machine-readable contract error for one completed worker."""
    status = str(contract.get("status") or "").strip().lower()
    if kind in {"material", "image", "review", "slide"} and status != "ready":
        return f"{kind} 必须返回 status: ready，得到 {status or 'missing'}"
    if kind == "research":
        if status not in {"ready", "partial"}:
            return f"research 必须返回 ready 或可传播的 partial，得到 {status or 'missing'}"
        output = str(contract.get("output") or "").strip().replace("\\", "/")
        output = output.removeprefix("./")
        if output != _ROLE_CANONICAL_OUTPUTS["research"]:
            return (
                "Research output 必须是 research/research.md，"
                f"得到 {output or 'missing'}"
            )
        output_path = os.path.abspath(os.path.join(parent.ws, output))
        if (os.path.commonpath([os.path.abspath(parent.ws), output_path])
                != os.path.abspath(parent.ws)
                or not os.path.isfile(output_path)):
            return f"Research 正式产物不存在: {output}"
        unresolved = str(contract.get("unresolved") or "").strip().lower()
        if status == "partial" and unresolved in {"", "none", "n/a", "not-applicable"}:
            return "Research partial 必须明确 unresolved，供 grounded-knowledge 传播"
    if kind == "material" and status == "ready":
        if not str(contract.get("coverage") or "").strip().lower().startswith("complete"):
            return "Material ready 必须返回 coverage: complete"
        output = str(contract.get("output") or "").strip().replace("\\", "/")
        output = output.removeprefix("./")
        if not re.fullmatch(r"materials/summaries/[A-Za-z0-9._-]+\.md", output):
            return (
                "Material output 必须是 materials/summaries/<assignment_id>.md，"
                f"得到 {output or 'missing'}"
            )
        output_path = os.path.abspath(os.path.join(parent.ws, output))
        if (os.path.commonpath([os.path.abspath(parent.ws), output_path])
                != os.path.abspath(parent.ws)
                or not os.path.isfile(output_path)):
            return f"Material 正式产物不存在: {output}"
    if kind == "review" and status == "ready":
        if str(contract.get("regression_checked") or "").strip().lower() != "yes":
            return "Review ready 必须返回 regression_checked: yes"
        if str(contract.get("regressed_pages") or "").strip().lower() != "none":
            return "Review 仍有退化页面时不得 ready"
        modified = set(getattr(child, "_review_modified_pages", set()) or set())
        compared = set(getattr(child, "_review_comparison_pages", set()) or set())
        missing = sorted(modified - compared)
        if missing:
            return (
                "Review 修改页缺少 BEFORE | AFTER 像素对比: "
                + ",".join(f"{page:02d}" for page in missing)
            )
        if (getattr(child, "_review_global_visual_change", False)
                and not getattr(child, "_review_global_comparison", False)):
            return "Review 修改 base.css 后缺少全册 BEFORE | AFTER 联系表对比"
    forced = str(getattr(child, "_stall_forced_status", "") or "").lower()
    if forced and status != forced:
        return f"停滞收口后的 {kind} 只能返回 status: {forced}，不得返回 {status or 'missing'}"
    return ""


def _run_child(parent, task, ticket):
    """构造 + 跑完一个子 agent,把小结挂到 parent,返回紧凑结果(不让子轨迹/图像穿透到 parent)。
    `ticket` 是父子共享小状态:只有部署方显式设置墙钟且触发时才置 abandoned；
    默认等待子任务按进展检测和角色合同自然收口。"""
    child, name = _build_child(parent, task)
    ticket["label"] = name
    with parent._child_sem:                  # 父级并发闸:跨多个并发的 delegate_task 调用统一限并发
        fin = child.run()
    contract = _final_contract(child.final_text)
    kind = _task_kind(name, task.get("goal"))
    nova_precheck = getattr(child, "nova_precheck", None)
    if nova_precheck is not None and not nova_precheck.get("ok", False):
        fin = False
        contract["validation_error"] = (
            "Nova exact-raw precheck failed: "
            + "; ".join(str(item) for item in nova_precheck.get("errors", [])[:5])
        )
    transparency_error = _image_transparency_error(
        parent.ws, task, name, contract, child.final_text, child.vision_paths
    )
    if transparency_error:
        fin = False
        contract["validation_error"] = transparency_error
    assigned_pages = _slide_group_pages(task) if kind == "slide" else []
    if kind == "slide":
        pixel_state = _slide_pixel_state(child, assigned_pages)
    elif kind == "review":
        pixel_state = _review_pixel_state(child)
    else:
        pixel_state = {
            "inspected_pages": [], "missing_pages": [], "stale_pages": [],
            "dirty_sources": [],
        }
    inspected_pages = pixel_state["inspected_pages"]
    missing_pixel_pages = pixel_state["missing_pages"]
    stale_pixel_pages = pixel_state["stale_pages"]
    if missing_pixel_pages:
        fin = False
        contract["validation_error"] = (
            ("Review 缺少最终交付像素覆盖: " if kind == "review"
             else "Slide Group 缺少逐页最终像素自检: ")
            + ",".join(f"{page:02d}" for page in missing_pixel_pages)
        )
    if stale_pixel_pages:
        fin = False
        contract["validation_error"] = (
            ("Review 最终交付像素证据已过期: " if kind == "review"
             else "Slide Group 最终像素证据已过期: ")
            + ",".join(f"{page:02d}" for page in stale_pixel_pages)
        )
    dirty_sources = pixel_state["dirty_sources"]
    if dirty_sources:
        fin = False
        contract["validation_error"] = (
            "视觉源文件最后修改后尚未完成对应重渲: " + ",".join(dirty_sources[:8])
        )
    contract_error = _child_contract_error(parent, child, kind, contract)
    if contract_error:
        fin = False
        contract["validation_error"] = contract_error
    rec = {"label": name, "kind": kind, "clean": fin, "renders": child.n_renders,
           "vision_calls": child.n_vision_calls,
           "vision_paths": list(child.vision_paths),
           "vision_evidence": dict(getattr(child, "vision_evidence", {}) or {}),
           "assigned_pages": assigned_pages,
           "inspected_pages": inspected_pages,
           "stale_pixel_pages": stale_pixel_pages,
           "dirty_visual_sources": dirty_sources,
           "machine_refine_rounds": int(getattr(child, "_review_refine_rounds", 0) or 0),
           "review_modified_pages": sorted(getattr(child, "_review_modified_pages", set()) or set()),
           "review_comparison_pages": sorted(getattr(child, "_review_comparison_pages", set()) or set()),
           "review_global_visual_change": bool(getattr(child, "_review_global_visual_change", False)),
           "review_global_comparison": bool(getattr(child, "_review_global_comparison", False)),
           "trace_mode": dict(getattr(child, "trace_mode_status", {}) or {}),
           "shot": child.last_shot, "exit_reason": child.exit_reason,
           "nova_raw_precheck": nova_precheck,
           "contract": contract}
    resume_of = str(task.get("_resume_of") or "").strip()
    if resume_of:
        rec["resume_of"] = resume_of
    with parent._spawn_lock:
        if not ticket.get("abandoned"):
            if fin and resume_of:
                for previous in reversed(parent.worker_recs):
                    if str(previous.get("label") or "") == resume_of:
                        previous["superseded_by"] = name
                        previous["recovered"] = True
                        break
            parent.worker_recs.append(rec)
            ticket["recorded"] = True
    if transparency_error:
        contract["validation_error"] = transparency_error
    return _child_handoff(parent, child, name, fin, contract)


def _record_worker_failure(parent, task, ticket, exit_reason, detail):
    """Persist every crashed/timed-out child as a first-class acceptance record."""
    with parent._spawn_lock:
        if ticket.get("recorded"):
            return next(
                (item for item in reversed(parent.worker_recs)
                 if item.get("label") == ticket.get("label")),
                {"contract": {"status": "blocked", "validation_error": str(detail)}},
            )
    label = str(ticket.get("label") or task.get("label") or "child")
    kind = _task_kind(label, task.get("goal"))
    assigned_pages = _slide_group_pages(task) if kind == "slide" else []
    message = str(detail or exit_reason).strip()[:1600]
    contract = {"status": "blocked", "validation_error": message}
    rec = {
        "label": label,
        "kind": kind,
        "clean": False,
        "renders": 0,
        "vision_calls": 0,
        "vision_paths": [],
        "vision_evidence": {},
        "assigned_pages": assigned_pages,
        "inspected_pages": [],
        "stale_pixel_pages": assigned_pages,
        "dirty_visual_sources": [],
        "machine_refine_rounds": 0,
        "shot": None,
        "exit_reason": exit_reason,
        "contract": contract,
    }
    trace_root = os.path.dirname(parent.trace.sub_dir)
    failure_dir = os.path.join(trace_root, "worker-failures")
    os.makedirs(failure_dir, exist_ok=True)
    failure_path = os.path.join(
        failure_dir, re.sub(r"[^A-Za-z0-9_.-]+", "_", label) + ".json"
    )
    temporary = failure_path + f".{os.getpid()}.{threading.get_ident()}.tmp"
    payload = {
        "label": label,
        "clean": False,
        "exit_reason": exit_reason,
        "contract": contract,
        "artifacts": [],
        "final_response": "",
    }
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, failure_path)
    with parent._spawn_lock:
        if not ticket.get("recorded"):
            ticket["abandoned"] = True
            ticket["recorded"] = True
            parent.worker_recs.append(rec)
    return rec


def delegate_task(parent, tasks=None):
    """Run the one Hermes-clean delegation shape: ``tasks[].goal`` only."""
    if not isinstance(tasks, list) or not tasks:
        return json.dumps({"error": "delegate_task 需要非空 tasks[]"}, ensure_ascii=False)
    if parent._delegate_depth >= MAX_SPAWN_DEPTH:
        return json.dumps({"error": "已到委派深度上限(叶子子 agent 不能再委派)"}, ensure_ascii=False)

    for index, task in enumerate(tasks, start=1):
        if not isinstance(task, dict) or set(task) != {"goal"}:
            return json.dumps({
                "error": f"task {index} 只能包含 goal",
                "code": "invalid_delegate_shape",
            }, ensure_ascii=False)
        if not isinstance(task.get("goal"), str) or not task["goal"].strip():
            return json.dumps({
                "error": f"task {index} 缺少非空 goal",
                "code": "invalid_delegate_shape",
            }, ensure_ascii=False)

    norm = [_normalize_task(t) for t in tasks]
    invalid = [
        (index, task.get("goal", ""))
        for index, task in enumerate(norm, start=1)
        if _task_kind(task.get("label"), task.get("goal")) == "other"
    ]
    if invalid:
        return json.dumps({
            "error": (
                f"task {invalid[0][0]} 的 goal 必须以 Material <assignment_id>:、Research:、"
                "Image <group_id>:、Slide Group <group_id> [页码]: 或 Review: 开头"
            ),
            "code": "invalid_role_prefix",
        }, ensure_ascii=False)
    role_contract_error = _role_output_contract_error(norm)
    if role_contract_error:
        return json.dumps({
            "error": role_contract_error,
            "code": "invalid_role_output_contract",
            "canonical_outputs": _ROLE_CANONICAL_OUTPUTS,
            "retry": "保留角色卡规定的正式产物路径，只在 goal 中描述任务范围。",
        }, ensure_ascii=False)
    material_order_error = _material_before_research_error(parent, norm)
    if material_order_error:
        return json.dumps({
            "error": material_order_error,
            "code": "material_required_before_research",
            "retry": (
                "先完成全部 Material，逐份 read_file 到 EOF；再把原始 query、"
                "materials/summaries 路径和证据缺口写入唯一 Research goal。"
            ),
        }, ensure_ascii=False)
    planning_error = _planning_before_slide_error(parent, norm)
    if planning_error:
        return json.dumps({
            "error": planning_error,
            "code": "planning_required",
            "retry": "补齐 plan/slide_NN.md，运行 deck.py prepare，再按冻结 production_group 委派。",
        }, ensure_ascii=False)
    norm, canonical_error = _canonicalize_slide_tasks(parent, norm)
    if canonical_error:
        return json.dumps({
            "error": canonical_error,
            "code": "invalid_slide_assignment",
            "expected_slide_tasks": _expected_slide_tasks(parent.ws),
            "retry": "使用 expected_slide_tasks 中的 goal 重试，不要再用叙述性页码。",
        }, ensure_ascii=False)
    grounding_error = _grounding_before_downstream_error(parent, norm)
    if grounding_error:
        return json.dumps({
            "error": grounding_error,
            "code": "grounding_required",
            "retry": "先写入并 read_file 验证 plan/grounded-knowledge.md，再重试原委派。",
        }, ensure_ascii=False)
    image_dependency_error = _image_before_slide_error(parent, norm)
    if image_dependency_error:
        return json.dumps({
            "error": image_dependency_error,
            "code": "image_dependencies_required",
            "retry": "先完成 Image 并确认 assets/catalog.json 中所有计划 asset_id 为 ready，再重试 Slide Group。",
        }, ensure_ascii=False)
    singleton_error = _reserve_singleton_roles(parent, norm)
    if singleton_error:
        payload = {"error": singleton_error}
        if "Slide" in singleton_error or "production_group" in singleton_error:
            payload.update({
                "code": "invalid_slide_assignment",
                "expected_slide_tasks": _expected_slide_tasks(parent.ws),
                "retry": "使用 expected_slide_tasks 中的 goal 重试。",
            })
        return json.dumps(payload, ensure_ascii=False)

    def run_one(nt, ticket):
        try:
            return _run_child(parent, nt, ticket)
        except Exception as e:
            rec = _record_worker_failure(
                parent, nt, ticket, "crashed", f"{type(e).__name__}: {e}"
            )
            return {"status": "error",
                    "exit_reason": "crashed", "contract": rec["contract"],
                    "renders": 0, "shot": None, "summary": f"子 agent 崩溃: {e}"}

    ex = cf.ThreadPoolExecutor(max_workers=MAX_CONCURRENT_CHILDREN, thread_name_prefix="child")
    try:
        triples = []
        for nt in norm:
            ticket = {"abandoned": False, "label": nt.get("label")}
            triples.append((nt, ticket, ex.submit(run_one, nt, ticket)))
        out = []
        for nt, ticket, f in triples:
            _ts = nt.get("toolsets") or []
            _lbl = (nt.get("label") or "")
            if "image_gen" in _ts:
                to = IMAGE_WORKER_TIMEOUT
            elif _lbl.startswith("material"):   # material 自己跑解析脚本 + 大 deck 忠实抄录,按页数放大
                to = _scaled_timeout(
                    MATERIAL_WORKER_TIMEOUT,
                    MATERIAL_WORKER_TIMEOUT_PER_PAGE,
                    _deck_n_slides(parent),
                    MATERIAL_WORKER_TIMEOUT_CAP,
                )
            elif any(k in _lbl for k in ("review", "audience", "listener", "audit", "gate")):
                # review/audience/audit/gate 都要读**整册**逐页核对(designer_audit/presenter_audit/designer_gate…),
                # 超时与页数强相关 → base + k×页数。用子串匹配:designer_audit 不 startswith "audit",
                # 旧 startswith 漏判 → 掉 600 默认档被误杀(2026-07-16 修:designer_audit 11 + designer_gate 6 超时)
                to = _scaled_timeout(
                    REVIEW_WORKER_TIMEOUT_BASE,
                    REVIEW_WORKER_TIMEOUT_PER_PAGE,
                    _deck_n_slides(parent),
                    REVIEW_WORKER_TIMEOUT_CAP,
                )
            elif _lbl.startswith("slide"):
                to = _slide_timeout_budget(nt)
            else:
                to = WORKER_TIMEOUT
            try:
                out.append(f.result(timeout=to))
            except cf.TimeoutError:
                lbl = ticket.get("label") or "child"
                parent.log(f"子 agent {lbl} 触发部署方显式墙钟 {to}s，记录 blocked")
                rec = _record_worker_failure(
                    parent, nt, ticket, "timeout", f"worker exceeded {to}s"
                )
                out.append({"status": "timeout", "renders": 0,
                            "shot": None, "contract": rec["contract"],
                            "summary": f"子 agent 超过 {to}s 被放弃"})
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return json.dumps({"results": out}, ensure_ascii=False)
