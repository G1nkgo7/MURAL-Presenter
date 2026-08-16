#!/usr/bin/env python3
"""通用 agent 运行时 —— "一个 agent 怎么想 / 动 / 记账",零领域知识。

- `Agent`:单 agent 的状态 + 沙箱(safe/read_path/writable)+ 模型客户端 + 一个 Trace。
- `run_loop(agent)`:**纯** ReAct 循环(模型 → 工具 → 模型),模型不再调工具即收尾,或到 max_turns;不替模型兜底。
- `delegate_task(parent, …)`:并行起一批子 agent,各在独立上下文里干活,只把文字小结返回父级。

子 agent 不是预定义类型:由 `goal`(干啥)+ `toolsets`(给哪些能力)在调用时拼出。领域(PPT)的
system 提示词、只读 skill 根、只读写前缀、初始 brief、拒绝采样验收,全部由 distill_ppt.py 注入/在外面做。

进程/线程模型:sample 之间 = 进程(distill_ppt.py 调度);一个 sample 内的子 agent = 线程
(每次 delegate_task 起一个**本地** ThreadPoolExecutor,用完即关 → 不跨 sample 串台)。
"""
import base64
import concurrent.futures as cf
import copy
import json
import os
import random
import re
import threading
import time

import anthropic

from . import tools
from .trace import Trace

# 子 agent 并发上限(所有 delegate_task 调用共用一个父级信号量)。
MAX_CONCURRENT_CHILDREN = int(os.environ.get("MAX_CONCURRENT_CHILDREN",
                              os.environ.get("SLIDE_CONCURRENCY", "4")))
# 同回合内**纯 IO 叶子工具**(无 agent 状态记账、落盘内容寻址不撞名)并行执行——主要打掉 image subagent
# 里多张 image_generate 的串行;单独限并发 TURN_TOOL_PARALLEL 防出图/搜索网关过载。可用 env 覆盖。
PARALLEL_LEAF_TOOLS = set(x for x in os.environ.get("PARALLEL_LEAF_TOOLS", "image_generate,web_search,web_extract").split(",") if x)
TURN_TOOL_PARALLEL = int(os.environ.get("TURN_TOOL_PARALLEL", "6"))
WORKER_TIMEOUT = int(os.environ.get("WORKER_TIMEOUT", "1500"))           # 普通子 agent 硬超时(秒)。900→1500(2026-07-17):高并发下 API 429 退避把每 turn 拉到~37s,v2.1 slide 需24-32turn→撞900s被杀→无summary→orch判未通过→整deck被拒(611次超时事故)。抬到1500给足墙钟;真正治竞争靠降WK
IMAGE_WORKER_TIMEOUT = int(os.environ.get("IMAGE_WORKER_TIMEOUT", "1800"))  # 含 image_gen 的子 agent 硬超时
MATERIAL_WORKER_TIMEOUT = int(os.environ.get("MATERIAL_WORKER_TIMEOUT", "1500"))  # material 子 agent:自己跑解析脚本(pdf/office解析+扫描PDF光栅化)吃时间,给更高预算(2026-07-09 解析交 agent)
# 2026-07-10 失败根因:timeout 240(review 127+slide 86 主导),review 逐页 vision_analyze、与页数强相关
# (review-timeout deck 中位 24 页 vs 全体 12);slide 是 patch↔render↔vision 自纠环。→ 按角色/页数弹性放大超时。
SLIDE_WORKER_TIMEOUT = int(os.environ.get("SLIDE_WORKER_TIMEOUT", "900"))          # slide:自纠环给 600→900 缓冲
REVIEW_WORKER_TIMEOUT_BASE = int(os.environ.get("REVIEW_WORKER_TIMEOUT_BASE", "900"))       # review 起步预算
REVIEW_WORKER_TIMEOUT_PER_PAGE = int(os.environ.get("REVIEW_WORKER_TIMEOUT_PER_PAGE", "45"))# 每页 +45s(逐页看图)
REVIEW_WORKER_TIMEOUT_CAP = int(os.environ.get("REVIEW_WORKER_TIMEOUT_CAP", "1800"))        # 封顶
MATERIAL_WORKER_TIMEOUT_PER_PAGE = int(os.environ.get("MATERIAL_WORKER_TIMEOUT_PER_PAGE", "30"))  # material 大 deck 每页 +30s
MATERIAL_WORKER_TIMEOUT_CAP = int(os.environ.get("MATERIAL_WORKER_TIMEOUT_CAP", "3600"))          # material 封顶(2026-07-15 2700→3600:超重附件集消化 >45min 会 timeout→clean=False 误杀;给足时间自然收尾)


def _deck_n_slides(parent):
    """粗数当前工作区已产出的页 HTML(给 review/material 超时按页数缩放用;数不到返 0=退回基础预算)。"""
    try:
        import glob as _g
        ws = getattr(parent, "ws", "") or ""
        return len(_g.glob(os.path.join(ws, "slides", "slide_*.html")))
    except Exception:
        return 0
SUBAGENT_MAX_TOKENS = int(os.environ.get("SUBAGENT_MAX_TOKENS", "16000"))  # 叶子子 agent per-回合上限(orch 才需大值;子 agent 大值只拖慢踩超时)
# ★slide 硬回合上限★:仅作**松弛 backstop 拦真死循环**,不做激进优化。
# 2026-07-17 教训:cap=24 太狠→v2.1 slide 常需 24-32 轮才清完硬伤,被截断即带硬伤返回→orch 反复重派(r2_r2_r3 级联)→整 deck 被拒 + 成本爆炸。
# 故默认放到 60(远超 v2.1 自然峰值 ~32,正常页不受影响、自然跑完即通过;只拦 >60 的病态死循环)。真正的省钱靠暖缓存 + 输出瘦身 + slide.md 软上限,不靠硬截断。
SLIDE_MAX_TURNS = int(os.environ.get("SLIDE_MAX_TURNS", "28"))  # 2026-07-18:3轮render-revise上限(初稿~6 + 3×~6≈24,留缓冲=28);配合"放宽门"(slide机检干净即过)→撞顶不再拒、纯控成本
MAX_SPAWN_DEPTH = int(os.environ.get("MAX_SPAWN_DEPTH", "1"))           # 委派深度上限(1 = 只有顶层能委派)
# 单个子 agent 累计能注入 context 的图片张数硬上限:超出后 vision_analyze 不再往 context 加新图,
# 只回一段文字提示"已达上限,基于已看的图决定"。机制层根治"逐张看十几张候选 → ContextWindowExceeded
# → api_failed → clean=False 拖垮整条 deck"(模型即兴造的"汇总映射"子代理就是这么撑爆的)。
MAX_VISION_IMAGES = int(os.environ.get("MAX_VISION_IMAGES", "8"))
RESAMPLE_THINKING_ONLY = int(os.environ.get("RESAMPLE_THINKING_ONLY", "2"))  # >0: 传输层重采 thinking-only 退化空采样(丢弃不入轨迹);0=纯 loop 原行为。2026-07-10 默认 0→2:stopped_no_text 失败 159 中 92% 是 thinking-only/空退化,重采可吃掉主体
# 每个 tool_result 后追加一条引导思考的 text 块(=在工具结果 user 回合尾部拼一句),把一次性 system nudge
# 升级成"每轮工具后强制提醒",提高交错思考(reasoning summary + signature)触发率。默认关。
# ⚠️ 该 text 会进 messages/轨迹 → 若不想让 nudge 落进训练数据,pack 时按 THINK_NUDGE_TEXT 首句剥离(见 trace_to_openai)。
THINK_NUDGE_EACH_TOOL = os.environ.get("THINK_NUDGE_EACH_TOOL", "0") == "1"
THINK_NUDGE_TEXT = os.environ.get("THINK_NUDGE_TEXT",
    "After receiving the tool result(s) above, carefully reflect on their quality and "
    "determine optimal next steps before proceeding. Use your thinking to plan and iterate "
    "based on this new information, and then take the best next action.")
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
            "可见回复语言：过程说明、工具调用前说明和最终总结使用中文；"
            "代码、路径、原文与专有名词可保留原文。"
            "PPT 屏显和讲稿语言仍服从用户的交付要求。"
        )
    return (
        "Visible response language: use English for progress notes, tool-call "
        "preambles, and final summaries. Code, paths, quotations, and proper nouns "
        "may remain in their original language. Deck and speech language still "
        "follows the user's delivery requirement."
    )


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


class Agent:
    """通用单 Agent 循环(Claude + 工具);Agent 对象 = 状态,循环逻辑在下面的 run_loop。

    编排器和子 agent 共用这个类,区别只在:工具集、初始任务、system、只读写前缀,以及编排器
    额外注册了 delegate_task。父子**共享同一个工作区 ws**(协作产出同一套产物),但各写各的轨迹。"""

    def __init__(self, role, sid, ws, sub_dir, tools_schema, config, initial_user, label,
                 system, skills_root, forbid_write_prefixes=None, extra_tools=None):
        self.role = role
        self.sid = sid
        self.ws = os.path.abspath(ws)                 # 工作区 = run_dir
        self.skills_root = os.path.normpath(os.path.abspath(skills_root)) if skills_root else None
        self.forbid_write_prefixes = list(forbid_write_prefixes or [])
        self.initial_user = initial_user
        self.label = label
        self.extra_tools = extra_tools or {}
        self.tools = tools_schema
        self.cfg = config or {}
        self.started = time.time()
        self.task_started_epoch = float(
            self.cfg.get("_task_started_epoch", self.started)
        )
        prompt_language = str(self.cfg.get("_prompt_language") or "").lower()
        if prompt_language not in {"zh", "en"}:
            prompt_language = _infer_prompt_language(initial_user)
        self.prompt_language = prompt_language
        self.base_system = system.rstrip()
        self.system = (
            f"{self.base_system}\n\n"
            f"{_runtime_time_context(self.task_started_epoch, prompt_language)}\n"
            f"{_visible_response_language_context(prompt_language)}\n"
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
        self.n_vision_imgs = 0      # 本 agent 已注入 context 的图片张数(vision_analyze 计数,硬上限见 MAX_VISION_IMAGES)
        self.final_text = ""
        self.exit_reason = None
        self.worker_recs = []       # 仅编排器:每个子 agent 的小结
        self._spawn_count = {}
        self._spawn_lock = threading.Lock()
        self._child_sem = threading.Semaphore(MAX_CONCURRENT_CHILDREN)   # 父级并发闸
        self._delegate_depth = 0

        # —— 模型客户端 ——
        self.model = self.cfg.get("model", os.environ.get("MODEL", "claude-opus-4-7-thinking"))
        self.a_base = self.cfg.get("anthropic_base_url",
                                   os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com"))
        self.max_turns = int(self.cfg.get("max_turns", 120))
        self.max_tokens = int(self.cfg.get("max_tokens", 16000))
        self.thinking = os.environ.get("THINKING", "0") != "0"
        self.think_effort = os.environ.get("THINK_EFFORT", "high")
        # 网关上带 `-thinking` 后缀的模型思考是**内置**的:纯调用即自带 thinking 块,反而传
        # anthropic 的 thinking/output_config 参数会把思考压掉。故对这类模型一律走纯调用
        # (thinking 块照样回来并被 blocks_to_dicts 记进轨迹)。
        self.native_thinking = "thinking" in self.model.lower()
        if self.native_thinking:
            self.thinking = False
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
            self.thinking = False   # 学生不走 anthropic 的 thinking/output_config
            self.client = openai_backend.OpenAIShim(
                base=os.environ["STUDENT_BASE_URL"],
                model=self.model,
                key=os.environ.get("STUDENT_API_KEY", "EMPTY"))
        else:
            self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=self.a_base,
                                              default_headers=_default_headers)

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
        for pre in self.forbid_write_prefixes:
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
        return {
            "role": self.role, "sample_id": self.sid, "label": self.label,
            "task": self.initial_user, "model": self.model, "anthropic_base_url": self.a_base,
            "image_model": self.image_model, "max_tokens": self.max_tokens, "max_turns": self.max_turns,
            "serper": bool(self.serper), "pid": os.getpid(),
            "thinking": ("native" if self.native_thinking else
                         ({"type": "adaptive", "display": "summarized", "effort": self.think_effort} if self.thinking else False)),
            "authoritative_task_started_at": time.strftime(
                "%Y-%m-%d %H:%M:%S UTC", time.gmtime(self.task_started_epoch)
            ),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def run(self):
        return run_loop(self)


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
    system = agent.system
    extra_body = None
    if PROMPT_CACHE and system:
        system = [{"type": "text", "text": agent.system, "cache_control": _EPHEMERAL}]  # 位置②:system block
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
    attempt = 0
    while True:
        try:
            # 大 max_tokens 非流式触发 SDK >10min 守卫;流式规避,get_final_message 同型返回
            with agent.client.messages.stream(**kwargs) as _stream:
                _resp = _stream.get_final_message()
            _acc_usage(agent, _resp)   # ① 累加本回合 usage
            return _resp
        except anthropic.BadRequestError as e:
            agent.log(f"[api 400 不重试] {str(e)[:300]}")   # 400 是请求本身的问题,重试不会变好
            return None
        except Exception as e:
            status = _status_code(e)
            transient = status in _TRANSIENT_STATUS
            # 限流/过载:用更宽的次数 + 指数退避(尊重 Retry-After);其余抖动错误保持原 4 次短退避。
            cap = API_MAX_RETRIES if transient else 4
            if attempt >= cap - 1:
                tag = f"{status} 限流/过载" if transient else "传输错误"
                agent.log(f"[api err {attempt} 放弃·{tag}] {str(e)[:160]}")
                return None
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


def _exec_one_tool(agent, tu):
    """执行单个 tool_use,返回它的 tool_result(含 render 记账 / 图像快照 / is_error)。
    render 记账与图像快照会改 agent 状态,所以这些工具只在 _run_tools 里**顺序**调;
    delegate_task 不碰这些 parent 状态(子 agent 各写各的),可安全并行。"""
    args = tu.input if isinstance(tu.input, dict) else {}
    err = False
    try:
        res = tools.dispatch(agent, tu.name, args)
    except Exception as e:
        res = f"{tu.name} 崩溃: {e}"
        err = True
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
    if isinstance(res, dict) and res.get("b_vision"):
        # ── B 结构(2026-07-14)：gemini 看图。快照真渲染图 + 登记待落盘重写，
        #   但**活循环里仍把 gemini 文字作为 tool_result 返回**(瞎眼老师据此驱动，行为不变)；
        #   图与"分析挪成模型输出"在 trace._b_rewrite 落盘时成型。图不进老师上下文(不占 n_vision_imgs)。
        tcid = tu.id
        try:
            agent.trace.snapshot_image(tcid, base64.b64decode(res["image_b64"]))
            agent.trace.register_b_vision(tcid, res.get("path", ""), res.get("analysis", ""))
        except Exception as e:
            agent.log(f"WARN: b_vision 快照/登记失败 {e}")
        return {"type": "tool_result", "tool_use_id": tcid,
                "content": f"[gemini 看图分析 · {res.get('path', '')}]\n{res.get('analysis', '')}"[:8000]}
    if isinstance(res, dict) and "image_b64" in res:
        tcid = tu.id
        # 快照照常存(轨迹完整,不受上限影响)
        try:
            agent.trace.snapshot_image(tcid, base64.b64decode(res["image_b64"]))
        except Exception as e:
            agent.log(f"WARN: 快照图像失败 {e}")
        # —— 上下文护栏:单 agent 注入图片数硬上限,超出不再往 context 加图(机制层防 ContextWindowExceeded)——
        if agent.n_vision_imgs >= MAX_VISION_IMAGES:
            agent.log(f"⛔ vision 图片数已达上限 {MAX_VISION_IMAGES},拒绝再加图({res.get('path','')})")
            return {"type": "tool_result", "tool_use_id": tcid, "content": (
                f"vision_analyze 已达本任务图片上限({MAX_VISION_IMAGES} 张),这张({res.get('path','')})不再加载进上下文——"
                f"再加会撑爆上下文导致整条任务失败。请**基于你已经看过的图**做决定;若图位太多,应在返回里告诉编排器"
                f"把任务拆成多个并行子任务(每个只负责少数图位),而不是一个子任务里看完所有候选。")}
        agent.n_vision_imgs += 1
        return {"type": "tool_result", "tool_use_id": tcid, "content": [
            {"type": "text", "text": res.get("summary", "")},
            {"type": "image", "source": {"type": "base64", "media_type": res.get("media_type", "image/png"),
                                         "data": res["image_b64"]}}]}
    tr = {"type": "tool_result", "tool_use_id": tu.id, "content": str(res)[:8000]}
    if err:
        tr["is_error"] = True     # 工具抛错 → 标 is_error,导出时据此把该 tool 消息标失败
    return tr


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
    """纯 ReAct 循环:模型 → 工具 → 模型,直到模型不再调用工具(自然收尾)或到 max_turns。
    **不做任何替模型兜底**:无空/截断回合自愈、无 max_turns 强制总结——模型怎么收(或没收)就怎么记,
    真实暴露其能力。仅保留传输/进程层安全(API 重试、子 agent 超时、子线程崩溃捕获)。写原始轨迹;返回 finished_clean。"""
    agent.trace.snapshot_inputs(agent.system, agent.tools, agent.config_snapshot())
    messages = [{"role": "user", "content": agent.initial_user}]
    tool_log = []
    for turn in range(agent.max_turns):
        # 传输层重采(默认关:RESAMPLE_THINKING_ONLY=0 时下方只跑一次=原纯 loop):native-thinking 偶发
        # "只出 thinking 就 end_turn"的退化空采样(有 thinking、无 text、无 tool),当失败采样**丢弃并重采**
        # 最多 K 次,退化轮不 append、不进轨迹。只重采 thinking-only;带 tool 的轮、真空轮(无 thinking)不重采。
        resp = None
        for _rs in range(RESAMPLE_THINKING_ONLY + 1):
            resp = _model_call(agent, messages)
            if resp is None:
                break
            _has_tool = any(b.type == "tool_use" for b in resp.content)
            _has_text = any(b.type == "text" and b.text.strip() for b in resp.content)
            _has_think = any(b.type == "thinking" and b.thinking.strip() for b in resp.content)
            if _has_tool or _has_text or not _has_think:
                break
            if _rs < RESAMPLE_THINKING_ONLY:
                agent.log(f"[{turn}] ⟲ thinking-only 退化采样,丢弃重采 {_rs+1}/{RESAMPLE_THINKING_ONLY}")
        if resp is None:
            agent.exit_reason = "api_failed"
            agent.log("API 连续失败,放弃")
            break

        messages.append({"role": "assistant", "content": blocks_to_dicts(resp.content)})
        turn_text = ""
        for b in resp.content:
            if b.type == "thinking" and b.thinking.strip():
                agent.log(f"[{turn}] 🧠 {b.thinking.strip()[:140]}")
            if b.type == "text" and b.text.strip():
                agent.log(f"[{turn}] 💬 {b.text.strip()[:180]}")
                turn_text = b.text.strip()
                agent.final_text = turn_text
        tool_uses = [b for b in resp.content if b.type == "tool_use"]

        if not tool_uses:
            # 模型不再调用工具 = 收尾。**不自愈、不补刀**(纯 loop):有文字=text_response,无文字=stopped_no_text。
            agent.exit_reason = "text_response" if turn_text else "stopped_no_text"
            agent.log(f"模型停止调用工具,收尾于回合 {turn}(stop={resp.stop_reason}, exit={agent.exit_reason})")
            break

        _tool_content = _run_tools(agent, tool_uses, turn, tool_log)
        if THINK_NUDGE_EACH_TOOL and isinstance(_tool_content, list):
            # 在 tool_result 块之后追加一句引导思考的 text(合法:user 回合可 tool_result+text 并存)
            _tool_content = _tool_content + [{"type": "text", "text": THINK_NUDGE_TEXT}]
        messages.append({"role": "user", "content": _tool_content})
    else:
        agent.exit_reason = "max_turns"
        agent.log(f"到达 max_turns({agent.max_turns}),停止")

    # 纯 loop:到 max_turns 直接停,**不做强制总结**——模型怎么收(或没收)就怎么记,真实暴露其能力。
    finished_clean = agent.exit_reason == "text_response"
    agent.trace.write(messages, tool_log)
    _write_usage(agent)   # ① 落 usage.json
    agent.log(f"轨迹已写: turns={len(tool_log)} renders={agent.n_renders} exit={agent.exit_reason}")
    return finished_clean


# ============= 委派子 agent(自由函数,操作一个 agent) =============

def _normalize_task(t):
    """把一条 delegate task 归一成 {goal, context, toolsets, role, label}。纯 goal 驱动,无领域特例。"""
    if not isinstance(t, dict):
        t = {"goal": str(t)}
    return {"goal": t.get("goal", ""), "context": t.get("context", ""),
            "toolsets": list(t.get("toolsets") or ["file", "terminal", "vision"]),
            "role": t.get("role", "leaf"), "label": t.get("label")}


def _build_child(parent, task):
    """从 parent 构造(不运行)一个子 agent。`task` = 归一后的 {goal, context, toolsets, role, label}。
    每次尝试拿到唯一的 sub_dir + label(重做 → `<label>_r2`…),重做不覆盖上次的轨迹/快照。"""
    base = task.get("label")
    with parent._spawn_lock:
        if not base:
            parent._child_seq = getattr(parent, "_child_seq", 0) + 1
            base = f"child_{parent._child_seq:02d}"
        c = parent._spawn_count.get(base, 0) + 1
        parent._spawn_count[base] = c
    name = base + ("" if c == 1 else f"_r{c}")

    toolsets = list(task.get("toolsets") or ["file", "terminal", "vision"])
    # role=orchestrator 且深度还允许再派时,才补 delegation 能力。
    if task.get("role") == "orchestrator" and parent._delegate_depth + 1 < MAX_SPAWN_DEPTH \
            and "delegation" not in toolsets:
        toolsets = toolsets + ["delegation"]
    schema = tools.resolve_toolsets(toolsets)
    have = {s["name"] for s in schema}             # 基础工具(read)默认并入
    schema = [tools.SCHEMAS[n] for n in tools.BASE_TOOL_NAMES if n not in have] + schema

    context = task.get("context") or ""
    # 自报身份(双管之一·训练信号):让子 agent 知道自己叫什么,并在收尾首句声明身份+产出。
    prompt_language = str(
        parent.cfg.get("_prompt_language") or getattr(parent, "prompt_language", "")
    ).lower()
    if prompt_language == "en":
        ident = (
            f"Your identity is {name}. Begin your final summary by naming your "
            "identity and deliverable.\n\n"
        )
        context_label = "Context"
    else:
        ident = f"你的身份是 {name};完成后在总结首句声明你的身份与产出。\n\n"
        context_label = "背景"
    initial = ident + task["goal"] + (
        f"\n\n{context_label}:\n{context}" if context else ""
    )
    # 叶子子 agent(无 delegation)不需大 max_tokens——降回省时,避免 32k 拖慢踩 WORKER_TIMEOUT(orch/可委派 child 保持大值)
    child_cfg = parent.cfg
    if "delegation" not in toolsets:
        _cap = min(int(parent.cfg.get("max_tokens", 16000)), SUBAGENT_MAX_TOKENS)
        child_cfg = dict(parent.cfg); child_cfg["max_tokens"] = _cap
    if name.startswith("slide"):           # ★slide 硬回合封顶(见 SLIDE_MAX_TURNS)★:防单页烧几十轮,治成本
        child_cfg = dict(child_cfg); child_cfg["max_turns"] = SLIDE_MAX_TURNS
    child = Agent(role="subagent", sid=parent.sid, ws=parent.ws,
                  sub_dir=f"subagents/{name}", tools_schema=schema,
                  config=child_cfg, initial_user=initial, label=name,
                  system=parent.base_system, skills_root=parent.skills_root,
                  forbid_write_prefixes=None)        # 子 agent 默认无写禁区(它们才是真正写产物的)
    child._delegate_depth = parent._delegate_depth + 1
    return child, name


def _run_child(parent, task, ticket):
    """构造 + 跑完一个子 agent,把小结挂到 parent,返回紧凑结果(不让子轨迹/图像穿透到 parent)。
    `ticket` 是父子共享小状态:父超时放弃时置 abandoned,迟到线程结束后不再写 worker_recs。"""
    child, name = _build_child(parent, task)
    ticket["label"] = name
    with parent._child_sem:                  # 父级并发闸:跨多个并发的 delegate_task 调用统一限并发
        fin = child.run()
    rec = {"label": name, "clean": fin, "renders": child.n_renders,
           "shot": child.last_shot, "exit_reason": child.exit_reason}
    with parent._spawn_lock:
        if not ticket.get("abandoned"):
            parent.worker_recs.append(rec)
            ticket["recorded"] = True
    return {"status": "ok" if fin else "issues",
            "renders": child.n_renders, "shot": child.last_shot,
            "summary": f"[{name}] " + (child.final_text or "").replace("\n", " ")[:240]}


def delegate_task(parent, goal=None, context=None, toolsets=None, role=None,
                  label=None, tasks=None, **_extra):
    """并行起一批子 agent 跑任务,返回 `{"results":[...]}` 的 JSON 字符串。

    两种形态:顶层单个 `{goal,context?,toolsets?,role?,label?}`,或 `tasks` 数组批量。每次调用用一个
    **本地** ThreadPoolExecutor(用完即关,不留全局池)。单个子 agent 有硬超时:超时记一条 clean=False
    (让验收拒收)+ 标记 abandoned(迟到线程丢弃自己的记录)。"""
    if isinstance(tasks, str):        # 健壮化:某些模型把 tasks 数组二次编码成 JSON 字符串
        try:
            tasks = json.loads(tasks)
        except Exception:
            tasks = None
    if isinstance(tasks, dict):       # 单个任务被当对象(而非单元素数组)传进来
        tasks = [tasks]
    if tasks is None:
        if goal or label or toolsets:
            tasks = [{"goal": goal, "context": context, "toolsets": toolsets,
                      "role": role, "label": label}]
        else:
            tasks = []
    if not isinstance(tasks, list) or not tasks:
        return json.dumps({"error": "delegate_task 需要 goal 或非空 tasks[]"}, ensure_ascii=False)
    if parent._delegate_depth >= MAX_SPAWN_DEPTH:
        return json.dumps({"error": "已到委派深度上限(叶子子 agent 不能再委派)"}, ensure_ascii=False)

    norm = [_normalize_task(t) for t in tasks]

    def run_one(nt, ticket):
        try:
            return _run_child(parent, nt, ticket)
        except Exception as e:
            return {"status": "error",
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
                to = min(MATERIAL_WORKER_TIMEOUT + MATERIAL_WORKER_TIMEOUT_PER_PAGE * _deck_n_slides(parent),
                         MATERIAL_WORKER_TIMEOUT_CAP)
            elif any(k in _lbl for k in ("review", "audience", "listener", "audit", "gate")):
                # review/audience/audit/gate 都要读**整册**逐页核对(designer_audit/presenter_audit/designer_gate…),
                # 超时与页数强相关 → base + k×页数。用子串匹配:designer_audit 不 startswith "audit",
                # 旧 startswith 漏判 → 掉 600 默认档被误杀(2026-07-16 修:designer_audit 11 + designer_gate 6 超时)
                to = min(REVIEW_WORKER_TIMEOUT_BASE + REVIEW_WORKER_TIMEOUT_PER_PAGE * _deck_n_slides(parent),
                         REVIEW_WORKER_TIMEOUT_CAP)
            elif _lbl.startswith("slide"):      # slide 自纠环缓冲
                to = SLIDE_WORKER_TIMEOUT
            else:
                to = WORKER_TIMEOUT
            try:
                out.append(f.result(timeout=to))
            except cf.TimeoutError:
                lbl = ticket.get("label") or "child"
                parent.log(f"子 agent {lbl} 超过 {to}s,放弃")
                with parent._spawn_lock:
                    if not ticket.get("recorded"):     # 子可能恰好已记真实结果,别覆盖
                        ticket["abandoned"] = True
                        parent.worker_recs.append({"label": lbl, "clean": False, "renders": 0,
                                                   "shot": None, "exit_reason": "timeout"})
                out.append({"status": "timeout", "renders": 0,
                            "shot": None, "summary": f"子 agent 超过 {to}s 被放弃"})
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return json.dumps({"results": out}, ensure_ascii=False)
