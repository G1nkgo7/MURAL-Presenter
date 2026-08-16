#!/usr/bin/env python3
"""叶子工具实现 + 工具 schema(通用,零领域知识)。

⚠️ 本文件的工具 **schema 严格对齐 hermes-agent**(tools/*.py):工具名称、描述、parameters
逐字照搬 hermes 的 `{name, description, parameters}`(OpenAI 风格 parameters,**不是** Anthropic
的 input_schema)。core/agent.py 在调 Anthropic Messages API 时把 `parameters` 适配成
`input_schema`(见 agent._anthropic_tool)。这样模型所见 / SFT 落库的工具定义与 hermes 完全一致。

工具是**自由函数**,操作一个 `agent` 上下文对象(状态)。循环里调 `dispatch(agent, name, args)`。
子 agent 的"类型"由 `goal` + `toolsets` 在调用时拼出(见 delegate_task)。`delegate_task` 需要
递归跑子循环,**实现在 core/agent.py 里**,通过 `agent.extra_tools` 注册;dispatch 先查 extra_tools
再查 BUILTINS。它的 schema(DELEGATE_TASK_SCHEMA)放本文件,和其它 schema 一处。

------------------------------------------------------------------ agent 上下文契约
    agent.ws / agent.safe(rel) / agent.read_path(rel) / agent.writable(rel)
    agent.serper / agent.img_base / agent.img_key / agent.image_model / agent.img_n
    agent.extra_tools
所有 key 从环境变量读(.env 注入,绝不写进仓库)。
"""
import base64
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.request

import requests

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
FOREGROUND_MAX_TIMEOUT = int(os.environ.get("TERMINAL_MAX_FOREGROUND_TIMEOUT", "600"))  # 对齐 hermes 默认 600


# =============================================================== 叶子工具实现
# 实现保留本仓库的沙箱/行为,仅在**参数名/工具名**上跟随 hermes schema;hermes 独有但本仓库
# 未支持的能力(terminal 的 background/pty/notify、patch 的 V4A 模式)参数照样接收,运行时忽略或报不支持。

def read_file(agent, path, offset=1, limit=500, **_extra):
    """读文本文件,按 hermes 输出 'LINE_NUM|CONTENT'。图片用 vision_analyze。可读 ws 内或只读 skill 树。"""
    if os.path.splitext(path)[1].lower() in IMG_EXT:
        return f"read_file 错误:'{path}' 是图片,请用 vision_analyze 查看。"
    fp = agent.read_path(path)
    if os.path.isdir(fp):
        entries = sorted(os.listdir(fp))
        if not entries:
            return f"{path}/ (空目录)"
        listing = [e + ("/" if os.path.isdir(os.path.join(fp, e)) else "") for e in entries]
        return f"{path}/ 下的条目:\n" + "\n".join(listing)
    with open(fp, encoding="utf-8") as f:
        all_lines = f.read().splitlines()
    total = len(all_lines)
    start = max(1, int(offset or 1))
    lim = int(limit or 500)
    sel = all_lines[start - 1:start - 1 + lim]
    numbered = "\n".join(f"{start + i}|{ln}" for i, ln in enumerate(sel))
    CAP = 7800
    if len(numbered) > CAP:
        cut = numbered[:CAP].rsplit("\n", 1)[0]
        last = start + cut.count("\n")
        numbered = cut + f"\n\n[… 截断:已显示第 {start}–{last} 行(共 {total} 行);续读 offset={last + 1}]"
    return numbered


def write_file(agent, path, content, cross_profile=False, **_extra):
    if not getattr(agent, "writable", lambda p: True)(path):
        return (f"write_file 错误:当前角色不允许写 {path}(只读路径)。改写其它路径,"
                f"或通过 delegate_task 委派有权限的子 agent。")
    fp = agent.safe(path)
    os.makedirs(os.path.dirname(fp), exist_ok=True)
    with open(fp, "w", encoding="utf-8") as f:
        f.write(content)
    return f"已写入 {len(content.encode())} 字节到 {path}"


def patch(agent, mode="replace", path=None, old_string=None, new_string=None,
          replace_all=False, patch=None, cross_profile=False, **_extra):
    """find-and-replace(mode='replace')。mode='patch'(V4A 多文件)本仓库未实现,会报不支持。"""
    if mode == "patch":
        return ("patch 错误:本环境未实现 V4A patch 模式(mode='patch')。"
                "请用 mode='replace' + path/old_string/new_string 做定点替换。")
    if not path or old_string is None or new_string is None:
        return "patch 错误:mode='replace' 需要 path、old_string、new_string。"
    if not getattr(agent, "writable", lambda p: True)(path):
        return f"patch 错误:当前角色不允许改 {path}(只读路径)。"
    fp = agent.safe(path)
    with open(fp, encoding="utf-8") as f:
        s = f.read()
    n = s.count(old_string)
    if n == 0:
        return f"patch 错误:在 {path} 里找不到 old_string"
    if n > 1 and not replace_all:
        return f"patch 错误:old_string 出现了 {n} 次(不唯一);确认全改请传 replace_all=true"
    with open(fp, "w", encoding="utf-8") as f:
        f.write(s.replace(old_string, new_string))
    return f"已编辑 {path}"


def search_files(agent, pattern, target="content", path=".", file_glob=None,
                 limit=50, offset=0, output_mode="content", context=0, **_extra):
    """内容搜索(target='content',正则)或按名找文件(target='files',glob)。替代 grep/find/ls。"""
    target = {"grep": "content", "find": "files"}.get(target, target)
    base = agent.read_path(path) if path else agent.ws
    limit, offset, context = int(limit or 50), int(offset or 0), int(context or 0)

    def rel(p):
        try:
            return os.path.relpath(p, agent.ws)
        except Exception:
            return p

    if target == "files":
        matches = []
        for root, _dirs, files in os.walk(base):
            for fn in files:
                if fnmatch.fnmatch(fn, pattern):
                    matches.append(os.path.join(root, fn))
        matches.sort(key=lambda p: -os.path.getmtime(p))
        sel = matches[offset:offset + limit]
        return "\n".join(rel(p) for p in sel) or "(无匹配文件)"

    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"search_files 错误:正则无效 {e}"
    files = [base] if os.path.isfile(base) else [
        os.path.join(r, fn) for r, _d, fs in os.walk(base) for fn in fs
        if not file_glob or fnmatch.fnmatch(fn, file_glob)]
    results, count_by_file = [], {}
    for fp in files:
        try:
            with open(fp, encoding="utf-8", errors="ignore") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        for i, ln in enumerate(lines):
            if rx.search(ln):
                count_by_file[fp] = count_by_file.get(fp, 0) + 1
                if output_mode == "content":
                    block = [f"{rel(fp)}:{j + 1}:{lines[j]}"
                             for j in range(max(0, i - context), min(len(lines), i + context + 1))]
                    results.append("\n".join(block))
    if output_mode == "files_only":
        fl = sorted(count_by_file, key=lambda p: -count_by_file[p])
        return "\n".join(rel(p) for p in fl[offset:offset + limit]) or "(无匹配)"
    if output_mode == "count":
        items = [f"{rel(p)}: {c}" for p, c in count_by_file.items()]
        return "\n".join(items[offset:offset + limit]) or "(无匹配)"
    return "\n\n".join(results[offset:offset + limit]) or "(无匹配)"


def terminal(agent, command, background=False, timeout=None, workdir=None,
             pty=False, notify_on_complete=False, watch_patterns=None, **_extra):
    """在工作区下执行 shell 命令(前台)。本环境不支持 background/pty/notify/watch,这些参数被忽略。
    主要用途:跑 skill 自带脚本——渲染某页 HTML 成 PNG(成功时 stdout 末行是 PNG 路径)。"""
    if not isinstance(command, str) or not command.strip():
        return "terminal 错误:command 不能为空"
    # 护栏:渲染环境(playwright/chromium/字体/greenlet)已预装且可用,禁止子 agent 安装/重装/调试。
    # 实测弱模型撞 render 报警后会疯狂 pip install --force-reinstall playwright(单批 300+ 次),
    # 烧光 deck 预算 + 污染共享 venv 引发雪崩;物理禁掉。(2026-07-05 应 Master 要求)
    _norm = " ".join(command.lower().split())
    _forbidden = ("pip install", "pip3 install", "pip uninstall", "uv pip install", "uv add",
                  "conda install", "poetry add", "apt install", "apt-get install", "apt install",
                  "npm install", "yarn add", "pnpm add", "playwright install", "-m playwright",
                  "force-reinstall")
    if any(f in _norm for f in _forbidden):
        return ("terminal 拒绝:渲染环境(playwright/chromium/字体)已预装且可用,禁止安装/重装/调试它。"
                "render.py 报错的真实原因几乎都是你的 HTML/CSS 不合法或资源没加载——请重试一次,"
                "仍失败就简化/修正 HTML,绝不要 pip install / playwright install。")
    to = int(timeout) if timeout else 180
    to = min(to, FOREGROUND_MAX_TIMEOUT)
    cwd = agent.safe(workdir) if workdir else agent.ws
    notes = []
    if background:
        notes.append("[注] 本环境不支持 background,已前台执行")
    try:
        r = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True, timeout=to)
    except subprocess.TimeoutExpired:
        return f"terminal 错误:命令超过 {to}s 超时"
    except Exception as e:
        return f"terminal 错误:{e}"
    out = (r.stdout or "")
    if r.stderr:
        out += ("\n[stderr] " + r.stderr)
    out = out.strip()
    body = out[:8000] if out else f"(命令完成,退出码 {r.returncode},无输出)"
    return ("\n".join(notes) + "\n" + body) if notes else body


MAX_VISION_EDGE = int(os.environ.get("MAX_VISION_EDGE", "1536"))   # 送模型的图最长边上限

# =============================================================== vision 后端开关(实验,纯加法)
# VISION_BACKEND 控制 vision_analyze 的"看图"由谁来做:
#   未设 / "anthropic"(默认)= 现行为**完全不变**:把原图读成 base64 dict 返回,由 core/agent.py
#                              注入老师(Opus)自己的上下文,老师下一回合亲自读像素。
#   "gemini"                  = 把 图 + question 发给 gemini-3.5-flash,拿回一段"这页哪里不对"的
#                              **文字分析**作为工具结果字符串返回给老师(老师据此改页);此路径**不再**
#                              往老师上下文注入原图(=真把"看图"外包给 gemini)。任何失败/超时 → 降级回
#                              注入原图(返回默认 dict,summary 前缀 "[gemini降级] "),绝不让整条 deck 崩。
# 复用 judge_v2/judge 的 gemini 调用约定(OpenAI 兼容 /chat/completions + Bearer GEMINI_API_KEY +
# gemini-3.5-flash + data:image/jpeg;base64),**不新造 key/平台/base_url**。
VISION_BACKEND = os.environ.get("VISION_BACKEND", "").strip().lower()
_GEMINI_VISION_MODEL = os.environ.get("VISION_GEMINI_MODEL", "gemini-3.5-flash")
_GEMINI_VISION_MAX_TOKENS = int(os.environ.get("VISION_GEMINI_MAX_TOKENS", "8000"))  # gemini 烧思考 token,给足否则正文被截成空
_GEMINI_VISION_TIMEOUT = int(os.environ.get("VISION_GEMINI_TIMEOUT", "120"))
_GEMINI_VISION_MAX_WIDTH = int(os.environ.get("VISION_GEMINI_MAX_WIDTH", "1024"))     # 对齐 judge.encode_image
# gemini 的 key 优先从进程环境读(harness 已把 .env 灌进 os.environ);缺失时兜底读 judge 用的同一批 env 文件,
# 避免另配一套 key。ENV 文件与 judge_v2 保持一致:distillation/.env + ~/.secrets/tokenhub_keys.env。
_VISION_ENV_FILES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
    os.path.expanduser("~/.secrets/tokenhub_keys.env"),
]


def _vision_env(name, default=None):
    """读 env(先进程环境,后兜底 judge 的 env 文件);对齐 judge_v2._clean_value:去行内 # 注释 + 去引号。"""
    v = os.environ.get(name)
    if v:
        return v
    for path in _VISION_ENV_FILES:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, val = line.split("=", 1)
                    if k.strip() == name:
                        val = re.split(r"\s+#", val, maxsplit=1)[0].strip().strip('"').strip("'")
                        if val:
                            return val
        except OSError:
            continue
    return default


def _gemini_vision_critique(fp, question):
    """把一页渲染图发给 gemini-3.5-flash,拿回"这页哪里不对"的文字分析(成功返回非空字符串;任何问题抛异常)。
    调用约定逐字复用 judge.call_openai / judge.encode_image(见 research/self-elvo/.../judge/judge.py):
    OpenAI 兼容 {OPENAI_BASE_URL}/chat/completions,Bearer GEMINI_API_KEY,图片降采样成 JPEG(<=宽)后 data URL。"""
    key = _vision_env("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("no GEMINI_API_KEY in env")
    base = _vision_env("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1")
    import io
    from PIL import Image
    with Image.open(fp) as im:               # 图片编码对齐 judge.encode_image:RGB→<=宽 MAX_WIDTH→JPEG q80→base64
        im = im.convert("RGB")
        if im.width > _GEMINI_VISION_MAX_WIDTH:
            im = im.resize((_GEMINI_VISION_MAX_WIDTH, round(im.height * _GEMINI_VISION_MAX_WIDTH / im.width)))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=80)
        b64 = base64.b64encode(buf.getvalue()).decode()
    q = (question or "").strip() or "请检查这一页幻灯片有没有问题。"
    prompt = (
        "你是严格的演示文稿版式审校。下面是一页**已渲染的**幻灯片截图。请仔细看图,指出这一页"
        "**哪里不对 / 可以改进**:排版是否失衡、文字是否溢出 / 裁切 / 重叠、留白是否失当、层级是否清楚、"
        "配色 / 字体是否协调、有没有缺字豆腐块(□/▯)、配图是否贴题、有没有破图 / 占位图 / 渲染缺陷。"
        "只描述你在图里**真实看到**的问题,别臆测看不到的东西;若这一页看起来没明显问题,就明确说没问题。"
        "用简洁中文分点回答。\n\n"
        f"提出这次检查的人关心的问题:{q}"
    )
    content = [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    payload = {
        "model": _GEMINI_VISION_MODEL,
        "messages": [{"role": "user", "content": content}],
        "max_tokens": _GEMINI_VISION_MAX_TOKENS,
        "temperature": 0.2,
    }
    r = requests.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=_GEMINI_VISION_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    txt = (r.json()["choices"][0]["message"]["content"] or "").strip()
    if not txt:
        raise RuntimeError("empty content (likely truncated on reasoning tokens)")
    return txt


def _gemini_vision_try(agent, fp, path, question):
    """VISION_BACKEND=gemini 时的看图外包:成功返回 gemini 的**原始分析文字**;失败返回 None(调用方降级回注入原图)。"""
    try:
        analysis = _gemini_vision_critique(fp, question)
    except Exception as e:
        if callable(getattr(agent, "log", None)):
            agent.log(f"vision_analyze: gemini 后端失败,降级回注入原图 — {type(e).__name__}: {str(e)[:160]}")
        return None
    return analysis


def _load_png_b64(fp):
    """读图 → 最长边 <=MAX_VISION_EDGE → PNG → base64(供把真渲染图快照进轨迹用);失败返回 None。
    与 vision_analyze 默认路径同一套图像参数,保证快照的图与老师当年若注入的图一致。"""
    try:
        import io
        from PIL import Image
        with Image.open(fp) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = MAX_VISION_EDGE / max(w, h)
            if scale < 1:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


def vision_analyze(agent, image_url, question=None, **_extra):
    """把一张图加载进对话让模型能看见。image_url 接受 ws 内文件路径(本环境走沙箱,不放行任意绝对路径/URL)。
    默认(VISION_BACKEND 未设 / anthropic)行为不变;VISION_BACKEND=gemini 时改由 gemini-3.5-flash 看图返回文字分析(见上文开关注释)。"""
    path = image_url
    fp = agent.read_path(path)              # 一律走沙箱
    if not os.path.exists(fp) or os.path.isdir(fp):
        return f"vision_analyze 错误:没有这张图 {path}"
    # —— 实验开关:gemini 后端把看图外包出去,成功即返回文字分析(不注入原图);失败 → _degraded 标记后落默认路径 ——
    # ⚠️ 作用域限定:除 material 外的看图(slide 改页 / image 核配图 / audience 收尾复审 / player / research…)
    #    都走 gemini,让学生学 gemini 的视觉分析 taste;唯 material* 子 agent(读用户附件抠字/数字=deck 事实
    #    底座,SKILL 硬规「原文为准、不改编不杜撰」)保持默认 Claude 注入原图——更小的 gemini 抠密集文档/扫描页
    #    易读糙,读错数字会静默污染事实底座。判据 = label 以 "material" 开头(与 harness 的 material* 豁免同源)。
    _degraded = False
    _is_material = str(getattr(agent, "label", "") or "").startswith("material")
    if VISION_BACKEND == "gemini" and not _is_material:
        analysis = _gemini_vision_try(agent, fp, path, question)
        if analysis is not None:
            # ── B 结构(2026-07-14)：gemini 只做合成期的"眼睛"，看图分析本身是被训模型要学的能力。
            #   故 gemini 成功时不再只回文字：把**真渲染图**一并带出(快照进轨迹)，分析文字留待
            #   落盘时(trace._b_rewrite)挪成模型自己的 assistant 输出。这样训练数据里模型是"看真图→自己产出分析"。
            #   活循环里老师(瞎眼 Opus)仍拿到 gemini 文字驱动(见 agent._exec_one_tool)，行为不变；
            #   图只进最终训练数据、不进老师上下文 → 保留 gemini 省 context 的好处。
            b64 = _load_png_b64(fp)
            if b64 is not None:
                return {"b_vision": True, "image_b64": b64, "media_type": "image/png",
                        "path": path, "summary": f"正在查看 {path}。", "analysis": analysis}
            # 图读不出的极少数情况 → 退回旧的纯文字(不致命)
            return f"[gemini 看图分析 · {path}]\n{analysis}"
        _degraded = True
    # ==== 默认(anthropic)路径:与改动前逐字一致;唯一新增 = summary 的降级前缀(_degraded=False 时为空,输出不变)====
    try:
        import io
        from PIL import Image
        with Image.open(fp) as im:
            im = im.convert("RGB")
            w, h = im.size
            scale = MAX_VISION_EDGE / max(w, h)
            if scale < 1:
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            data = buf.getvalue()
    except ImportError:
        with open(fp, "rb") as f:
            data = f.read()
    except Exception as e:
        return (f"vision_analyze 错误:{path} 不是可解析的图片({type(e).__name__})。"
                f"只能查看 PNG/JPG 等图片;HTML 页请先渲染成 PNG 再看。")
    _prefix = "[gemini降级] " if _degraded else ""   # 默认路径 _degraded=False → 前缀空 → 输出与改动前逐字一致
    return {"image_b64": base64.b64encode(data).decode(), "media_type": "image/png",
            "path": os.path.relpath(fp, agent.ws), "summary": f"{_prefix}正在查看 {path}。"}


def web_search(agent, query, limit=5, search_type="search", **_extra):
    """联网搜索(serper)。search_type="search"(默认)走网页搜索 /search,返回标题 / 链接 / 摘要;
    search_type="images" 走图搜 /images,返回真实图片的直链 URL / 标题 / 来源(配 deck 真图时用这个)。
    注:serper 的网页搜索端点几乎不返图,真要搜图必须显式传 search_type="images"。"""
    if not agent.serper:
        return "web_search 不可用(未配置 serper key)"
    n = min(int(limit or 5), 100)
    images = str(search_type).lower() == "images"
    endpoint = "https://google.serper.dev/images" if images else "https://google.serper.dev/search"
    try:
        r = requests.post(endpoint,
                          headers={"X-API-KEY": agent.serper, "Content-Type": "application/json"},
                          json={"q": query, "num": min(n, 10)}, timeout=30).json()
    except Exception as e:
        return f"web_search 错误:{e}"
    out = []
    if images:
        for it in r.get("images", [])[:n]:
            url = it.get("imageUrl") or it.get("link")
            out.append(f"- [图] {it.get('title', '')}\n  {url}\n  来源:{it.get('source', '')}")
        return "\n".join(out) or "(无图片结果)"
    for it in r.get("organic", [])[:n]:
        out.append(f"- {it.get('title')}\n  {it.get('link')}\n  {it.get('snippet', '')}")
    return "\n".join(out) or "(无结果)"


def _fetch_one(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as e:
        return f"fetch 错误:{e}"
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()[:5000]


def web_extract(agent, urls, **_extra):
    """抓取若干 URL,抽正文为(近似)markdown 文本。最多 5 个 URL。"""
    if isinstance(urls, str):
        urls = [urls]
    if not isinstance(urls, list) or not urls:
        return "web_extract 错误:需要非空 urls 数组"
    parts = []
    for u in urls[:5]:
        parts.append(f"## {u}\n\n{_fetch_one(u)}")
    return "\n\n".join(parts)


_ASPECT_SIZE = {"landscape": "1536x1024", "portrait": "1024x1536", "square": "1024x1024"}


def _image_gen_one(agent, prompt, size):
    """单张出图(退避重试 + 落盘 assets/,内容寻址命名)。返回相对路径或错误串。"""
    # 配图后端偶发瞬时不可用(503/超时/空 data),工具层退避重试,别让子 agent 几次手动重试就放弃丢图。
    # 配图是 deck 质量关键(用图率),这里多扛几次比丢一张 hero 图划算。
    d, last_err = None, ""
    for attempt in range(4):
        try:
            d = requests.post(f"{agent.img_base}/images/generations",
                              headers={"Authorization": f"Bearer {agent.img_key}",
                                       "Content-Type": "application/json"},
                              json={"model": agent.image_model, "prompt": prompt, "size": size, "n": 1},
                              timeout=180).json()
        except Exception as e:
            last_err = str(e)[:160]
            d = None
        if d is not None and "data" in d and d["data"]:
            break
        if d is not None and "data" not in d:
            last_err = json.dumps(d, ensure_ascii=False)[:160]
        if attempt < 3:
            time.sleep(3 * (attempt + 1))   # 3/6/9s 退避
    if d is None or "data" not in d or not d["data"]:
        return f"image_generate 错误(重试 4 次仍失败):{last_err}"
    it = d["data"][0]
    if it.get("b64_json"):
        data = base64.b64decode(it["b64_json"])
    elif it.get("url"):
        try:
            data = requests.get(it["url"], timeout=120).content
        except Exception as e:
            return f"image_generate 错误:下载图片失败 {e}"
    else:
        return "image_generate 错误:没有返回图片"
    # hermes schema 无 out_path:用 prompt 的内容寻址名,保证并行子 agent 不撞名(同 prompt→同文件,无碍)。
    name = f"img_{hashlib.sha1(prompt.encode('utf-8')).hexdigest()[:10]}.png"
    rel = f"assets/{name}"
    os.makedirs(agent.safe("assets"), exist_ok=True)
    with open(agent.safe(rel), "wb") as f:
        f.write(data)
    return rel


def image_generate(agent, prompt, aspect_ratio="landscape", **_extra):
    """根据文本提示生成图片(照片/插画/主视觉,不用于数据图表)。存到 assets/,返回相对路径。
    aspect_ratio:landscape(16:9 宽)/portrait(16:9 高)/square(1:1)。"""
    return _image_gen_one(agent, prompt, _ASPECT_SIZE.get(aspect_ratio, "1536x1024"))


def fetch_image(agent, url, **_extra):
    """下载一张网图到 assets/ 本地（带浏览器 UA + 按 host 自动 Referer，绕常见防盗链/热链保护）。
    返回 assets/ 下相对路径；失败返回错误字符串（可改用 image_generate 兜底）。
    web_search(search_type="images") 搜到的真图直链必须先用本工具落地，才能被 vision_analyze 核对、
    被 HTML/pptx 稳定引用（远程直链常因防盗链在回填后裂图）。"""
    if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
        return f"fetch_image 错误：需要 http(s) 图片直链，收到 {url!r}"
    try:
        from urllib.parse import urlparse
        u = urlparse(url)
        headers = {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            "Referer": f"{u.scheme}://{u.netloc}/",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200 or not resp.content:
            return f"fetch_image 错误：HTTP {resp.status_code}/空内容，跳过该图（可改用 image_generate 兜底）"
        data = resp.content
    except Exception as e:
        return f"fetch_image 错误：下载失败 {type(e).__name__}: {e}（可改用 image_generate 兜底）"
    try:
        import io
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "JPEG").lower()
        ext = {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif"}.get(fmt, ".jpg")
    except Exception:
        return "fetch_image 错误：下载内容不是可解析图片（可能防盗链返回了占位页），改用 image_generate 兜底"
    name = f"web_{hashlib.sha1(url.encode('utf-8')).hexdigest()[:10]}{ext}"
    rel = f"assets/{name}"
    os.makedirs(agent.safe("assets"), exist_ok=True)
    with open(agent.safe(rel), "wb") as f:
        f.write(data)
    return rel


# =============================================================== 工具 schema(逐字对齐 hermes,parameters 风格)

READ_FILE_SCHEMA = {
    "name": "read_file",
    "description": "Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. Use offset and limit for large files. Reads exceeding ~100K characters are rejected; use offset and limit to read specific sections of large files. NOTE: Cannot read images or binary files — use vision_analyze for images.",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to read (absolute, relative, or ~/path)"},
            "offset": {"type": "integer", "description": "Line number to start reading from (1-indexed, default: 1)", "default": 1, "minimum": 1},
            "limit": {"type": "integer", "description": "Maximum number of lines to read (default: 500, max: 2000)", "default": 500, "maximum": 2000},
        },
        "required": ["path"],
    },
}

WRITE_FILE_SCHEMA = {
    "name": "write_file",
    "description": "Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for targeted edits. Auto-runs syntax checks on .py/.json/.yaml/.toml and other linted languages; only NEW errors introduced by this write are surfaced (pre-existing errors are filtered out).",
    "parameters": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to the file to write (will be created if it doesn't exist, overwritten if it does)"},
            "content": {"type": "string", "description": "Complete content to write to the file"},
            "cross_profile": {
                "type": "boolean",
                "description": "Opt out of the cross-profile soft guard. Defaults to false. Set true ONLY after explicit user direction to edit another Hermes profile's skills/plugins/cron/memories — by default these writes are blocked with a warning because they affect a different profile than the one this session is running under.",
                "default": False,
            },
        },
        "required": ["path", "content"],
    },
}

PATCH_SCHEMA = {
    "name": "patch",
    "description": (
        "Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal. "
        "Uses fuzzy matching (9 strategies) so minor whitespace/indentation differences won't break it. "
        "Returns a unified diff. Auto-runs syntax checks after editing.\n\n"
        "REPLACE MODE (mode='replace', default): find a unique string and replace it. "
        "REQUIRED PARAMETERS: mode, path, old_string, new_string.\n"
        "PATCH MODE (mode='patch'): apply V4A multi-file patches for bulk changes. "
        "REQUIRED PARAMETERS: mode, patch."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["replace", "patch"],
                "description": "Edit mode. 'replace' (default): requires path + old_string + new_string. 'patch': requires patch content only.",
                "default": "replace",
            },
            "path": {"type": "string", "description": "REQUIRED when mode='replace'. File path to edit."},
            "old_string": {"type": "string", "description": "REQUIRED when mode='replace'. Exact text to find and replace. Must be unique in the file unless replace_all=true. Include surrounding context lines to ensure uniqueness."},
            "new_string": {"type": "string", "description": "REQUIRED when mode='replace'. Replacement text. Pass empty string '' to delete the matched text."},
            "replace_all": {"type": "boolean", "description": "Replace all occurrences instead of requiring a unique match (default: false)", "default": False},
            "patch": {"type": "string", "description": "REQUIRED when mode='patch'. V4A format patch content. Format:\n*** Begin Patch\n*** Update File: path/to/file\n@@ context hint @@\n context line\n-removed line\n+added line\n*** End Patch"},
            "cross_profile": {"type": "boolean", "description": "Opt out of the cross-profile soft guard. Defaults to false. Set true ONLY after explicit user direction to edit another Hermes profile's skills/plugins/cron/memories.", "default": False},
        },
        "required": ["mode"],
    },
}

SEARCH_FILES_SCHEMA = {
    "name": "search_files",
    "description": "Search file contents or find files by name. Use this instead of grep/rg/find/ls in terminal. Ripgrep-backed, faster than shell equivalents.\n\nContent search (target='content'): Regex search inside files. Output modes: full matches with line numbers, file paths only, or match counts.\n\nFile search (target='files'): Find files by glob pattern (e.g., '*.py', '*config*'). Also use this instead of ls — results sorted by modification time.",
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern for content search, or glob pattern (e.g., '*.py') for file search"},
            "target": {"type": "string", "enum": ["content", "files"], "description": "'content' searches inside file contents, 'files' searches for files by name", "default": "content"},
            "path": {"type": "string", "description": "Directory or file to search in (default: current working directory)", "default": "."},
            "file_glob": {"type": "string", "description": "Filter files by pattern in grep mode (e.g., '*.py' to only search Python files)"},
            "limit": {"type": "integer", "description": "Maximum number of results to return (default: 50)", "default": 50},
            "offset": {"type": "integer", "description": "Skip first N results for pagination (default: 0)", "default": 0},
            "output_mode": {"type": "string", "enum": ["content", "files_only", "count"], "description": "Output format for grep mode: 'content' shows matching lines with line numbers, 'files_only' lists file paths, 'count' shows match counts per file", "default": "content"},
            "context": {"type": "integer", "description": "Number of context lines before and after each match (grep mode only)", "default": 0},
        },
        "required": ["pattern"],
    },
}

TERMINAL_TOOL_DESCRIPTION = """Execute shell commands on a Linux environment. Filesystem usually persists between calls.

Do NOT use cat/head/tail to read files — use read_file instead.
Do NOT use grep/rg/find to search — use search_files instead.
Do NOT use ls to list directories — use search_files(target='files') instead.
Do NOT use sed/awk to edit files — use patch instead.
Do NOT use echo/cat heredoc to create files — use write_file instead.
Reserve terminal for: builds, installs, git, processes, scripts, network, package managers, and anything that needs a shell.

Foreground (default): Commands return INSTANTLY when done, even if the timeout is high. Set timeout=300 for long builds/scripts — you'll still get the result in seconds if it's fast. Prefer foreground for short commands.
Background: Set background=true to get a session_id. Almost always pair with notify_on_complete=true — bg without notify runs SILENTLY and you have no way to learn it finished short of calling process(action='poll') yourself. Two legitimate uses:
  (1) Long-lived processes that never exit (servers, watchers, daemons) — silent is correct, there's no exit to notify on.
  (2) Long-running bounded tasks (tests, builds, deploys, CI pollers, batch jobs) — MUST set notify_on_complete=true. Without it you'll either forget to poll or sit blocked waiting for the user to surface the result.
For servers/watchers, do NOT use shell-level background wrappers (nohup/disown/setsid/trailing '&') in foreground mode. Use background=true so Hermes can track lifecycle and output.
After starting a server, verify readiness with a health check or log signal, then run tests in a separate terminal() call. Avoid blind sleep loops.
Use process(action="poll") for progress checks, process(action="wait") to block until done.
Working directory: Use 'workdir' for per-command cwd.
PTY mode: Set pty=true for interactive CLI tools (Codex, Claude Code, Python REPL).

Do NOT use vim/nano/interactive tools without pty=true — they hang without a pseudo-terminal. Pipe git output to cat if it might page.
"""

TERMINAL_SCHEMA = {
    "name": "terminal",
    "description": TERMINAL_TOOL_DESCRIPTION,
    "parameters": {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The command to execute on the VM"},
            "background": {"type": "boolean", "description": "Run the command in the background. Almost always pair with notify_on_complete=true — without it, the process runs silently and you'll have no way to learn it finished short of calling process(action='poll') yourself (easy to forget, leading to silent blindness on long jobs). Two legitimate patterns: (1) Long-lived processes that never exit (servers, watchers, daemons) — these stay silent because there's no exit to notify on. (2) Long-running bounded tasks (tests, builds, deploys, CI pollers, batch jobs) — these MUST set notify_on_complete=true. For short commands, prefer foreground with a generous timeout instead.", "default": False},
            "timeout": {"type": "integer", "description": f"Max seconds to wait (default: 180, foreground max: {FOREGROUND_MAX_TIMEOUT}). Returns INSTANTLY when command finishes — set high for long tasks, you won't wait unnecessarily. Foreground timeout above {FOREGROUND_MAX_TIMEOUT}s is rejected; use background=true for longer commands.", "minimum": 1},
            "workdir": {"type": "string", "description": "Working directory for this command (absolute path). Defaults to the session working directory."},
            "pty": {"type": "boolean", "description": "Run in pseudo-terminal (PTY) mode for interactive CLI tools like Codex, Claude Code, or Python REPL. Only works with local and SSH backends. Default: false.", "default": False},
            "notify_on_complete": {"type": "boolean", "description": "When true (and background=true), you'll be automatically notified exactly once when the process finishes. **This is the right choice for almost every long-running task** — tests, builds, deployments, multi-item batch jobs, anything that takes over a minute and has a defined end. Use this and keep working on other things; the system notifies you on exit. MUTUALLY EXCLUSIVE with watch_patterns — when both are set, watch_patterns is dropped.", "default": False},
            "watch_patterns": {"type": "array", "items": {"type": "string"}, "description": "Strings to watch for in background process output. HARD RATE LIMIT: at most 1 notification per 15 seconds per process — matches arriving inside the cooldown are dropped. After 3 consecutive 15-second windows with dropped matches, watch_patterns is automatically disabled for that process and promoted to notify_on_complete behavior (one notification on exit, no more mid-process spam). USE ONLY for truly rare, one-shot mid-process signals on LONG-LIVED processes that will never exit on their own — e.g. ['Application startup complete'] on a server so you know when to hit its endpoint, or ['migration done'] on a daemon. DO NOT use for: (1) end-of-run markers like 'DONE'/'PASS' — use notify_on_complete instead; (2) error patterns like 'ERROR'/'Traceback' in loops or multi-item batch jobs — they fire on every iteration and you'll hit the strike limit fast; (3) anything you'd ever combine with notify_on_complete. When in doubt, choose notify_on_complete. MUTUALLY EXCLUSIVE with notify_on_complete — set one, not both."},
        },
        "required": ["command"],
    },
}

VISION_ANALYZE_SCHEMA = {
    "name": "vision_analyze",
    "description": (
        "Load an image into the conversation so you can see it. Accepts a "
        "URL, local file path, or data URL. When your active model has "
        "native vision, the image is attached to your context directly "
        "and you read the pixels yourself on the next turn — call this "
        "any time the user references an image (filepath in their message, "
        "URL in tool output, screenshot from the browser, etc.). For "
        "non-vision models, falls back to an auxiliary vision model that "
        "returns a text description."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "image_url": {"type": "string", "description": "Image URL (http/https), local file path, or data: URL to load."},
            "question": {"type": "string", "description": "Your specific question or request about the image. Optional context the model uses on the next turn after seeing the image."},
        },
        "required": ["image_url", "question"],
    },
}

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web. With search_type='search' (default) returns web results (titles, URLs, descriptions). With search_type='images' returns REAL images from the web — direct image URLs, titles, and source sites — use this to find real photos for slides (prefer real images; image_generate is only a fallback). Query operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."},
            "limit": {"type": "integer", "description": "Maximum number of results to return. Defaults to 5.", "minimum": 1, "maximum": 100, "default": 5},
            "search_type": {"type": "string", "enum": ["search", "images"], "description": "'search' (default) for web pages; 'images' to find real images (returns direct image URLs + source). Use 'images' when you need real photos.", "default": "search"},
        },
        "required": ["query"],
    },
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract content from web page URLs. Returns page content in markdown format. Also works with PDF URLs (arxiv papers, documents, etc.) — pass the PDF link directly and it converts to markdown text. Pages under 5000 chars return full markdown; larger pages are LLM-summarized and capped at ~5000 chars per page. Pages over 2M chars are refused. If a URL fails or times out, use the browser tool to access it instead.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {"type": "array", "items": {"type": "string"}, "description": "List of URLs to extract content from (max 5 URLs per call)", "maxItems": 5},
        },
        "required": ["urls"],
    },
}

IMAGE_GENERATE_SCHEMA = {
    "name": "image_generate",
    "description": (
        "Generate high-quality images from text prompts. The underlying "
        "backend (FAL, OpenAI, etc.) and model are user-configured and not "
        "selectable by the agent. Returns either a URL or an absolute file "
        "path in the `image` field; display it with markdown "
        "![description](url-or-path) and the gateway will deliver it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "The text prompt describing the desired image. Be detailed and descriptive."},
            "aspect_ratio": {"type": "string", "enum": ["landscape", "square", "portrait"], "description": "The aspect ratio of the generated image. 'landscape' is 16:9 wide, 'portrait' is 16:9 tall, 'square' is 1:1.", "default": "landscape"},
        },
        "required": ["prompt"],
    },
}

# 委派子 agent 的能力包列表(本系统可委派的 toolsets,对应 hermes 描述里的 _TOOLSET_LIST_STR)。
# visual 副本(配对基线):恢复 'vision'(视觉模态,看渲染像素)+ web 含 fetch_image(可抓真实图)。
_TOOLSET_LIST_STR = ", ".join(f"'{n}'" for n in ["file", "image_gen", "terminal", "vision", "web"])

FETCH_IMAGE_SCHEMA = {
    "name": "fetch_image",
    "description": "Download a REAL image from a direct URL into the local assets/ folder and return its local path. Sends a browser User-Agent and an auto Referer to get past common hotlink / anti-leech protection. ALWAYS localize a real photo you found via web_search(search_type='images') with this BEFORE using it: remote direct links frequently break when embedded into a slide (hotlink protection), and vision_analyze can only read LOCAL files. Workflow: web_search images -> fetch_image(url) -> vision_analyze the returned LOCAL path to check it -> reference that local path in the slide. Returns an error string (then fall back to image_generate) when the link is protected / dead / not an image.",
    "parameters": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Direct image URL (http/https), e.g. one returned by web_search(search_type='images')."},
        },
        "required": ["url"],
    },
}


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect "
        "the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "What the subagent should accomplish. Be specific and self-contained -- the subagent knows nothing about your conversation history."},
            "label": {"type": "string", "description": "Identity name for this subagent (e.g. slide/image/research/presenter/audience/player — see the skill's subagents/*.md). Used to name its trajectory directory and to identify it on return. ALWAYS set this; do not leave it to the default child_NN."},
            "context": {"type": "string", "description": "Background information the subagent needs: file paths, error messages, project structure, constraints. The more specific you are, the better the subagent performs."},
            "toolsets": {"type": "array", "items": {"type": "string"}, "description": ("Toolsets to enable for this subagent. Default: inherits your enabled toolsets. "
                          f"Available toolsets: {_TOOLSET_LIST_STR}. Common patterns: ['terminal', 'file'] for code work, ['web'] for research, ['browser'] for web interaction, ['terminal', 'file', 'web'] for full-stack tasks.")},
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "Task goal"},
                        "label": {"type": "string", "description": "Identity name for this subagent (e.g. slide/image/research/presenter/audience/player — see the skill's subagents/*.md). Used to name its trajectory directory and to identify it on return. ALWAYS set this; do not leave it to the default child_NN."},
                        "context": {"type": "string", "description": "Task-specific context"},
                        "toolsets": {"type": "array", "items": {"type": "string"}, "description": f"Toolsets for this specific task. Available: {_TOOLSET_LIST_STR}. Use 'web' for network access, 'terminal' for shell, 'browser' for web interaction."},
                        "acp_command": {"type": "string", "description": "Per-task ACP command override (e.g. 'copilot'). Overrides the top-level acp_command for this task only. Do NOT set unless the user explicitly told you an ACP CLI is installed."},
                        "acp_args": {"type": "array", "items": {"type": "string"}, "description": "Per-task ACP args override. Leave empty unless acp_command is set."},
                        "role": {"type": "string", "enum": ["leaf", "orchestrator"], "description": "Per-task role override. See top-level 'role' for semantics."},
                    },
                    "required": ["goal"],
                },
                "description": "(rebuilt at get_definitions() time)",
            },
            "role": {"type": "string", "enum": ["leaf", "orchestrator"], "description": "(rebuilt at get_definitions() time)"},
            "acp_command": {"type": "string", "description": ("Override ACP command for child agents (e.g. 'copilot'). When set, children use ACP subprocess transport instead of inheriting the parent's transport. Requires an ACP-compatible CLI (currently GitHub Copilot CLI via 'copilot --acp --stdio'). See agent/copilot_acp_client.py for the implementation. IMPORTANT: Do NOT set this unless the user has explicitly told you a specific ACP-compatible CLI is installed and configured. Leave empty to use the parent's default transport (Hermes subagents).")},
            "acp_args": {"type": "array", "items": {"type": "string"}, "description": ("Arguments for the ACP command (default: ['--acp', '--stdio']). Only used when acp_command is set. Leave empty unless acp_command is explicitly provided.")},
        },
        "required": [],
    },
}


# name -> 实现
BUILTINS = {
    "read_file": read_file, "write_file": write_file, "patch": patch,
    "search_files": search_files, "terminal": terminal, "vision_analyze": vision_analyze,
    "web_search": web_search, "web_extract": web_extract, "image_generate": image_generate,
    "fetch_image": fetch_image,
}

# name -> schema
SCHEMAS = {s["name"]: s for s in (
    READ_FILE_SCHEMA, WRITE_FILE_SCHEMA, PATCH_SCHEMA, SEARCH_FILES_SCHEMA, TERMINAL_SCHEMA,
    VISION_ANALYZE_SCHEMA, WEB_SEARCH_SCHEMA, WEB_EXTRACT_SCHEMA, IMAGE_GENERATE_SCHEMA,
    FETCH_IMAGE_SCHEMA, DELEGATE_TASK_SCHEMA)}


# =============================================================== toolset 注册表(对齐 hermes 命名)
# toolset = 能力包/分组别名,故意与其展开的具体工具名解耦(如 image_gen 展开为 image_generate);
# 二者不必同名。
TOOLSETS = {
    "file":       ["read_file", "write_file", "patch", "search_files"],
    "terminal":   ["terminal"],
    # visual 副本(配对基线):恢复视觉模态与图片联网获取。
    "vision":     ["vision_analyze"],
    "image_gen":  ["image_generate"],
    "web":        ["web_search", "web_extract", "fetch_image"],
    "delegation": ["delegate_task"],
}
BASE_TOOL_NAMES = ["read_file"]        # 基础能力:对所有子 agent 默认并入


def resolve_toolsets(names):
    """toolset 名列表 → 去重展开的 schema 列表。未知名忽略并打 warning。
    健壮化:有的模型把 toolsets 传成字符串(如 '["vision"]' / 'vision,image'),
    先归一成 list,避免被逐字符迭代成一堆未知 toolset。"""
    if isinstance(names, str):
        import json as _json
        s = names.strip()
        parsed = None
        try:
            v = _json.loads(s)
            parsed = v if isinstance(v, list) else ([v] if isinstance(v, str) else None)
        except Exception:
            parsed = None
        if parsed is None:
            parsed = [t.strip().strip('\'"') for t in s.strip('[]').split(',') if t.strip()]
        names = parsed
    seen, out = set(), []
    for n in (names or []):
        ts = TOOLSETS.get(n)
        if ts is None:
            print(f"[resolve_toolsets] 未知 toolset:{n!r},忽略", flush=True)
            continue
        for tool in ts:
            if tool not in seen:
                seen.add(tool)
                out.append(SCHEMAS[tool])
    return out


def dispatch(agent, name, args):
    """按名字找工具——先查 agent 专属的 extra_tools(如 delegate_task),再查 BUILTINS。"""
    if name in agent.extra_tools:
        return agent.extra_tools[name](**args)
    fn = BUILTINS.get(name)
    if not fn:
        return f"未知工具 {name}"
    return fn(agent, **args)
