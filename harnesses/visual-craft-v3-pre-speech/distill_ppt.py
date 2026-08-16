#!/usr/bin/env python3
"""PPT agentic 蒸馏入口 —— 单条 / 批量并行 rollout,生成 SFT 用的原始轨迹。

一个文件管全部蒸馏:CLI(单条 + batch)+ 调度(并行子进程 + manifest 断点续跑)+ PPT recipe
(seed→brief、编排器装配、拒绝采样验收)。通用 agent 运行时在 core/(agent/tools/trace),
本文件是它唯一认识 "PPT" 的薄外壳——换领域只改本文件 + skills/。

teacher 模型(Claude Opus)按 skills/ppt-skill-html 自主生成整套 HTML 幻灯片,轨迹经**拒绝采样**
落库:编排器只规划+委派(不许写 slides/),并行子 agent 写/渲/自纠每一页。

每条 seed = 一个 sample,跑在**独立子进程 + 独立 run 目录**里,进程级全局/playwright/cwd 永不串台:
    runs/<batch>/<sample_id>/      隔离工作区 + _trace/(orchestrator/ subagents/)
    log/<batch>.manifest.jsonl     每 sample 一条结果,断点续跑唯一依据

用法:
    # 单条(直接传 brief)
    uv run python distill_ppt.py --query "做一份 5 页的人工智能简介" --batch adhoc
    # 批量(jsonl,每行一个 {query, lang?, slide_count?, ...})
    uv run python distill_ppt.py --input briefs.jsonl --batch q1 --workers 64
    uv run python distill_ppt.py --input briefs.jsonl --batch q1 --resume      # 断点续跑
    uv run python distill_ppt.py --input briefs.jsonl --batch q1 --dry-run     # 不调模型,验证骨架
"""
import argparse
import concurrent.futures as cf
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.environ.get("PPT_SKILLS_ROOT") or os.path.join(ROOT, "skills")  # env 可覆盖 skill 树(A/B 版本赛马用);默认 ROOT/skills — hjt 2026-07-02
RUNS = os.path.join(ROOT, "runs")
LOGS = os.path.join(ROOT, "log")                   # 注意:V2 用 log/(单数)

TERMINAL = {"completed"}                            # 续跑跳过的终态;其余(error/缺失/半截)都重跑

# 通用、薄的 base system(PPT 风味只有最后两行)。领域方法在 skills/,按需 read SKILL.md。
BASE_SYSTEM = """\
你是一个自主的创作型 Agent,通过工具与文件系统、无头浏览器、网络交互来完成任务。

工作方式:
- 先理解任务。任务涉及某项专门能力时,`skills/` 下有对应的 SKILL.md —— 先 `read_file` 它,再按其方法执行(渐进式:先读 SKILL.md,需要时再读它引用的文件)。
- **收到工具结果后,先仔细核对其质量、想清下一步再继续:用你的思考基于这些新信息规划与迭代,然后采取最佳的下一步动作。** After receiving tool results, carefully reflect on their quality and determine optimal next steps before proceeding. Use your thinking to plan and iterate based on this new information, and then take the best next action.
- **自主推进**,不要向用户提问、不要中途停下等确认;自行补齐合理假设,做有品味的决定。
- 用工具**实际产出文件**,不要只在文字里描述。
- 全部完成后,用**一段简短文字**总结收尾 —— 这段文字就是你的最终输出。
- **每一回合都必须落到「一个工具调用」或「最终总结文字」上;严禁只输出思考(thinking)、既不调工具也不写文字就结束本回合。**
  还有事做 → 这一回合就去调一个工具(读文件、写文件、委派子 agent…);真的全做完了 → 写一段简短文字总结收尾。思考永远是为了紧接着的动作或结论服务,不能停在思考上。

可用技能:
- ppt-skill-html(`skills/ppt-skill-html/SKILL.md`):生成 HTML 幻灯片演示文稿(每页 1600×900,16:9)。
"""

# 编排器工具:只规划+委派(file 写 plan/base.css + delegation),不直接调研/出图/看图/执行命令。
ORCHESTRATOR_TOOLSETS = ["file", "delegation"]


# ---------------------------------------------------------------- env / 资源

def load_dotenv():
    """加载 ROOT/.env 的 KEY=VALUE 到环境变量(不覆盖已设的)。无依赖、幂等;父子进程都调一次。"""
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def cgroup_cpus():
    """返回 cgroup 真实可用核数(容器配额),读不到则退回 os.cpu_count()。"""
    try:                                     # cgroup v2
        with open("/sys/fs/cgroup/cpu.max") as f:
            q, p = f.read().split()
        if q != "max":
            return max(1, int(int(q) / int(p)))
    except Exception:
        pass
    try:                                     # cgroup v1
        q = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        p = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if q > 0:
            return max(1, int(q / p))
    except Exception:
        pass
    return os.cpu_count() or 1


# ---------------------------------------------------------------- PPT recipe

def seed_to_brief(seed):
    """把一条 seed dict 变成给编排器的初始 brief。"""
    return seed["query"] if isinstance(seed, dict) else str(seed)


def _generation_preferences(seed):
    if not isinstance(seed, dict):
        return {}
    out = {
        "page_count": int(seed.get("slide_count") or seed.get("pages_hint") or 0),
        "content_theme": str(seed.get("theme") or "").strip(),
        "visual_style": str(seed.get("style") or "").strip(),
        "color_scheme": str(seed.get("scheme") or "").strip(),
        "attachment_count": len(seed.get("attachments") or []),
        "attachment_paths": [
            f"materials/_raw/{os.path.basename(str((item.get('name') or item.get('stored_name') or item.get('path')) if isinstance(item, dict) else item))}"
            for item in (seed.get("attachments") or [])
        ],
    }
    return {key: value for key, value in out.items() if value not in ("", 0, [], {}, None)}


def _link_skills(run_dir):
    """工作区根放一个指向只读 skill 树的符号链接,让 bash(cwd=工作区)能用相对路径
    `skills/ppt-skill-html/scripts/render.py` 跑脚本——和 read('skills/...') 的路径一致。"""
    link = os.path.join(run_dir, "skills")
    if not os.path.lexists(link):
        try:
            os.symlink(SKILLS_DIR, link)
        except Exception:
            pass


# —— 材料挂载(附件能力):有 seed["attachments"] 时,只把原件拷进 materials/_raw/ + 写 attachments.json;
#    解析/光栅化/catalog 全部交给 material 子代理跑 skill 的 scripts/stage_materials.py(harness 越薄越好、
#    skill 端到端自包含,不在 harness 内联解析/硬编码解析 venv 路径)。


def _stage_materials(run_dir, seed):
    """有附件时,只把用户材料**原件拷进** runs/<sid>/materials/_raw/ + 写 attachments.json 清单。
    **解析/光栅化/catalog 全部交给 material 子代理**跑 skill 的 `scripts/stage_materials.py`
    (2026-07-09 改:harness 越薄越好、skill 端到端自包含;agent timeout 已调高应对解析耗时)。
    无附件直接 return——对普通 deck 零影响。"""
    atts = (seed or {}).get("attachments") if isinstance(seed, dict) else None
    if not atts:
        return
    mdir = os.path.join(run_dir, "materials")
    raw = os.path.join(mdir, "_raw")
    os.makedirs(raw, exist_ok=True)
    manifest = []                                    # 拷贝清单 → attachments.json(给 agent 的 stage_materials.py 读)
    for a in atts:
        src = a.get("path") if isinstance(a, dict) else a
        name = (a.get("name") if isinstance(a, dict) else None) or (os.path.basename(src) if src else "unknown")
        if not src or not os.path.exists(src):
            manifest.append({"name": name, "status": "missing"})
            continue
        try:
            shutil.copy2(src, os.path.join(raw, name))
            manifest.append({"name": name, "raw": f"materials/_raw/{name}"})
        except Exception as e:
            manifest.append({"name": name, "status": "failed", "note": f"copy: {e}"})
    with open(os.path.join(mdir, "attachments.json"), "w", encoding="utf-8") as f:
        json.dump({"attachments": manifest}, f, ensure_ascii=False, indent=2)
    # 不再解析/光栅化/写 catalog.json —— material 子代理会跑 skill 的 stage_materials.py 生成 catalog。


def _missing_attachments(seed):
    """返回 seed 里**路径不存在**的附件路径列表(无附件或全在 → 空)。派发前逐样本预检用:
    附件缺失的 seed 直接跳过、不派给 worker —— 否则 material 子代理会因附件缺失让编排器阻塞、
    每条白烧一份「解析+走到阻塞」的 API(2026-07-10 事故:zhenxi rerun 挪走附件源,整批静默 rejected 空烧一小时)。
    只 stat 不读内容,零成本;每次派发都重算,故附件哪天回来 --resume 会自动再跑(skipped 非终态)。"""
    atts = (seed or {}).get("attachments") if isinstance(seed, dict) else None
    if not atts:
        return []
    miss = []
    for a in atts:
        p = a.get("path") if isinstance(a, dict) else a
        if p and not os.path.exists(p):
            miss.append(p)
    return miss


MIN_RENDER_BYTES = int(os.environ.get("MIN_RENDER_BYTES", "26000"))   # 退路:观测到的纯色空白图 ~21KB
BLANK_LUMA_RANGE = int(os.environ.get("BLANK_LUMA_RANGE", "24"))      # 灰度跨度小于此 = 近乎纯色 = 空白/破渲染


def _render_ok(png):
    """判断一张渲染图不是空白/破图:优先用 PIL 看灰度跨度(纯色页跨度≈0);没装 PIL 退回字节下限。"""
    try:
        from PIL import Image
        with Image.open(png) as im:
            lo, hi = im.convert("L").getextrema()
        return (hi - lo) >= BLANK_LUMA_RANGE
    except Exception:
        return os.path.getsize(png) >= MIN_RENDER_BYTES


# 只认规范页文件 `slide_<纯数字>.html`;skill 可能生成 slide_07.bak.html / slide_07.html.bak 之类的备份,
# 它们**不算正式页**——否则 .bak.html 会被 glob 当成无渲染的页,误判整条 deck 拒收。
_SLIDE_RE = re.compile(r"^slide_\d+\.html$")


def _slide_htmls(ws):
    """工作区里**规范的**页 HTML(slide_<数字>.html),排除 .bak 等备份文件。"""
    return sorted(p for p in glob.glob(os.path.join(ws, "slides", "slide_*.html"))
                  if _SLIDE_RE.match(os.path.basename(p)))


def _v_pass(html_path, ws):
    """保守机核 V:对最终 HTML 重跑 render.py,读它打印的『机检结论』行判 4 项 blocking
    (off_canvas / broken_image / cjk_tofu / placeholder)。非破坏:渲到临时 PNG,不覆盖 renders/ 成品图。
    判定策略保守(paper:宁漏勿误杀):只在明确读到『机检结论: 不过』时判 fail;
    脚本缺失 / 渲染异常 / 无结论行,一律 fail-open(不阻断)——页面完整性已由上面 missing/blank 兜底。
    返回 (ok, output)。"""
    render_py = os.path.join(SKILLS_DIR, "ppt-skill-html", "scripts", "render.py")
    if not os.path.exists(render_py):
        return True, "(render.py 不在,跳过 V)"
    tmp = tempfile.NamedTemporaryFile(prefix="vcheck_", suffix=".png", delete=False)
    tmp.close()
    try:
        proc = subprocess.run([sys.executable, render_py, html_path, tmp.name],
                              cwd=ws, capture_output=True, text=True, timeout=180)
        out = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if "机检结论: 不过" in out:
            return False, out
        return True, out                       # 通过 / 无结论行 / 渲染噪声 → 不阻断
    except Exception as e:
        return True, f"(V 重渲异常 {type(e).__name__}: {e} —— 不阻断)"
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _accept(orch):
    """结构化拒绝采样 —— 只有干净轨迹才提交。返回 (ok, 原因)。"""
    if orch.exit_reason != "text_response":
        return False, f"编排器未自然收尾(exit={orch.exit_reason})"
    slides = _slide_htmls(orch.ws)
    if not slides:
        return False, "没有产出任何 slide"
    missing, blank = [], []
    for s in slides:
        png = os.path.join(orch.ws, "renders", os.path.splitext(os.path.basename(s))[0] + ".png")
        if not os.path.exists(png):
            missing.append(os.path.basename(s))
        elif not _render_ok(png):
            blank.append(os.path.basename(s))
    if missing:
        return False, f"{len(missing)} 页没有成功渲染: {missing[:5]}"
    if blank:
        return False, f"{len(blank)} 页渲染疑似空白/破图(近乎纯色): {blank[:5]}"
    with orch._spawn_lock:                      # 与子 agent 线程的写竞争,快照后再判
        recs = list(orch.worker_recs)
    # 编排器对失败子 agent 会重派(slide_NN → slide_NN_r2/_r3),重派即"对失败的修复"。
    # 旧失败 attempt 不应再拖垮整条 deck:按基名(去掉 _rN 后缀)只认**最新一次** attempt 是否 clean
    # (worker_recs 按完成顺序追加,重派必晚于原 attempt → 同基名后者覆盖前者 = 最新)。
    # 最终页面产物已由上面 missing/blank 渲染检查兜底,故这里放行"已重派成功"的页是安全的。
    latest_attempt = {}
    for w in recs:
        base = re.sub(r"_r\d+$", "", w["label"])
        latest_attempt[base] = w
    # 所有子 agent 一律要求干净收尾(clean = exit_reason=='text_response')。
    # 历史上 research*/material* 曾被豁免此门:它们撞单子代理墙钟 timeout → clean=False → 误杀整条 deck。
    # 但实测(matq5k 4658 册)确认那批非 clean 主因是 **timeout**(消化超重附件超墙钟),**非 max_turns**——
    # 且 timeout 的 material 其 materials.md 其实都已写全(23–76KB)。故治本 = 抬 material timeout cap 到 3600s
    # (core/agent.py),让超时的 material 有足够时间跑完 + 留文字总结 → 自然 clean;按角色的豁免遂多余、已删除。
    # 删豁免后新判废的只剩 stopped_no_text / api_failed —— 那本就是真失败(没留总结 / 传输挂),该判废。
    # —— 2026-07-15 应 Master 决定:先抬 cap 再删豁免(见 doc);取代 2026-07-05 的 research/material 豁免。
    # 2026-07-18 应 Master:slide 只认机检(下方保守机核 V 逐页验 off_canvas/broken_image/cjk_tofu/placeholder),
    # 不再因 slide 子代理"没干净收尾"(撞 max_turns / 自评剩软问题 / 截断)拒整 deck —— 大砍重派浪费 + 提通过率,
    # 且质量不降(软问题本 advisory,硬机检仍由 Gate5 兜)。非 slide 子代理(orch/research/designer/review/image/player)仍须 clean(它们无逐页机检兜底)。
    bad = [base for base, w in latest_attempt.items() if not w["clean"] and not base.startswith("slide")]
    if bad:
        return False, f"{len(bad)} 个子 agent 未通过: {bad[:5]}"
    # 保守机核 V（v2.5，对齐 paper §3）:每页最终 HTML 重跑 render.py，任一页明确『机检结论: 不过』
    # (off_canvas / broken_image / cjk_tofu / placeholder)→ 整条 deck 判废。其余 advisory 信号不接进门。
    v_failed = [os.path.basename(s) for s in slides if not _v_pass(s, orch.ws)[0]]
    if v_failed:
        return False, f"{len(v_failed)} 页未过保守机核 V(越界/裂图/豆腐块/占位): {v_failed[:5]}"
    return True, "ok"


def run_sample(sample_id, seed, run_dir, config):
    """子进程入口契约。跑编排器(它会并行委派子 agent),做结构化验收,返回状态 dict。
    在子进程里 lazy-import core(保持父调度进程轻量,不提前 import anthropic)。"""
    from core import tools
    from core.agent import Agent

    _link_skills(run_dir)
    try:
        _stage_materials(run_dir, seed)             # 有附件则拷原件进 materials/_raw/ + 写 attachments.json(解析交给 material 子代理;无附件即空转)
    except Exception as e:
        print(f"[stage_materials] {sample_id}: {type(e).__name__}: {e}", file=sys.stderr)
    run_config = dict(config or {})
    run_config["_generation_preferences"] = _generation_preferences(seed)
    orch = Agent(role="orchestrator", sid=sample_id, ws=run_dir, sub_dir="orchestrator",
                 tools_schema=tools.resolve_toolsets(ORCHESTRATOR_TOOLSETS), config=run_config,
                 initial_user=seed_to_brief(seed), label="orch",
                 system=BASE_SYSTEM, skills_root=SKILLS_DIR,
                 forbid_write_prefixes=["slides"])   # 红线:编排器不许写 slides/ 页面 HTML
    orch.run()
    ok, reason = _accept(orch)
    slides = _slide_htmls(run_dir)
    with orch._spawn_lock:
        workers = list(orch.worker_recs)
    return {
        "status": "completed" if ok else "rejected",
        "reason": reason, "n_slides": len(slides), "n_workers": len(workers),
        "orch_exit": orch.exit_reason, "workers": workers, "pid": os.getpid(),
        # 覆盖率可见性:新演讲稿/叙事脊柱产物是否落盘(非门控,仅统计 rollout 覆盖率)
        "has_speech": os.path.exists(os.path.join(run_dir, "speech.md")),
        "has_narrative": os.path.exists(os.path.join(run_dir, "plan", "narrative.md")),
    }


# ---------------------------------------------------------------- seeds / manifest

def load_seeds(path):
    """读 jsonl,每行一个 seed dict。容错:跳过空行,坏行报错并指出行号。"""
    seeds = []
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit(f"seed 文件第 {ln} 行不是合法 JSON: {e}")
            if not isinstance(obj, dict) or not obj.get("query"):
                raise SystemExit(f"seed 文件第 {ln} 行缺少 query 字段: {line[:120]}")
            seeds.append(obj)
    return seeds


def _seed_id_canon(seed):
    """稳定身份规范化:把 attachments[].path 收敛成 basename 再参与哈希。
    背景(2026-07-11 踩坑):sid 原来哈希整条 seed(含附件**绝对路径**),附件目录一挪
    (zhenxi 07-10 改路径)→ query 没变但 path 变了 → 全批 sid 变 → harness 当成全新样本
    从头重跑一遍,~4571 个种子被跑两遍、588 个已完成 deck 作废重做(~¥46万双付)。
    改成只认文件名(name/basename 稳定)后,附件挪窝/换机器都不再 re-key。
    可用 SID_ATTACH_BASENAME=0 回退旧行为(仅为兼容未迁移的旧 batch)。"""
    if os.environ.get("SID_ATTACH_BASENAME", "1") != "1":
        return seed
    atts = seed.get("attachments")
    if not isinstance(atts, list) or not any(isinstance(a, dict) and "path" in a for a in atts):
        return seed
    norm = dict(seed)
    norm["attachments"] = [
        ({**a, "path": os.path.basename(str(a["path"]))} if isinstance(a, dict) and "path" in a else a)
        for a in atts
    ]
    return norm


def make_sample_id(batch, seed, seen):
    """稳定且唯一的 sample_id,**与 seed 在文件里的位置无关**(对整条 seed 规范化哈希),
    过滤/重排 seed 文件后 --resume 仍映射到同一目录。完全相同的 seed 用出现次数消歧。
    ⚠️ 附件只认文件名不认绝对路径(见 _seed_id_canon):附件目录挪动不再让整批 re-key。"""
    canon = json.dumps(_seed_id_canon(seed), sort_keys=True, ensure_ascii=False)
    h = hashlib.sha1(canon.encode("utf-8")).hexdigest()[:12]
    base = f"{batch}_{h}"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}_{seen[base]}"


def load_manifest(mpath):
    """读 manifest → {sample_id: last_record}(后写覆盖先写)。每条注入 `_attempts`(出现次数)。"""
    done, attempts = {}, {}
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                sid = r.get("sample_id")
                if not sid:
                    continue
                attempts[sid] = attempts.get(sid, 0) + 1
                done[sid] = r
    for sid, r in done.items():
        r["_attempts"] = attempts[sid]
    return done


def append_manifest(mpath, rec):
    with open(mpath, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()


# ---------------------------------------------------------------- worker(子进程)

def _local_work_dir(batch, sid):
    """WORK_ROOT 置时返回本地快盘工作目录(如 /workspace/ppt_work/<batch>/<sid>);未置返回 None(老行为:
    直接在 FUSE 的 run_dir 跑)。本地盘小文件 I/O 比 /mnt/afs FUSE 快 ~3000×,避开渲染/页写的慢盘 churn 与 slab 膨胀。"""
    root = os.environ.get("WORK_ROOT", "").strip()
    return os.path.join(root, batch, sid) if root else None


def _persist_back(work_dir, dest):
    """把本地工作目录原子搬回持久 run_dir(FUSE)。先 copytree 到 dest+'.partial' 再 os.rename:
    dest 一出现即完整(半截不会被 audit/resume 当成完整)。跳过 skills 符号链接、到 dest 再重建。"""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".partial"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(work_dir, tmp, symlinks=True, ignore=shutil.ignore_patterns("skills"))
    shutil.rmtree(dest, ignore_errors=True)        # 清掉可能的脏残留(被 --resume 重跑的旧目录)
    os.rename(tmp, dest)                            # 同盘 rename 原子;dest 出现即完整
    _link_skills(dest)


def _requires_anthropic_key():
    """Only Anthropic-compatible model backends require ANTHROPIC_API_KEY."""
    return os.environ.get("MODEL_BACKEND", "anthropic").strip().lower() != "openai"


def worker(task):
    """在**子进程**里跑一条 sample。WORK_ROOT 置时:在本地快盘跑、完事原子搬回持久 run_dir(避 FUSE 慢盘)。
    manifest 始终记持久 run_dir(FUSE),故 audit/visual/resume/salvage 全不变。"""
    load_dotenv()                       # 子进程也加载密钥(spawn 安全)
    # ── 多渠道分流(2026-07-09):防打崩单渠道(如三方A Bedrock 限流)。设 SLOT_POOL="A E" 即启用:
    #    每 worker 按 pid 轮选一个渠道 → 设该渠道 key(TOKENHUB_CLAUDE_KEY_<X>)。总并发 WK 均摊到各渠道,
    #    单渠道压力 = WK/渠道数。不设 SLOT_POOL 时走原单 slot(cci_env 已设的 ANTHROPIC_API_KEY),向后兼容。
    _pool = os.environ.get("SLOT_POOL", "").split()
    _chosen_slot = None
    if _pool:
        _chosen_slot = _pool[os.getpid() % len(_pool)]
        _k = os.environ.get(f"TOKENHUB_CLAUDE_KEY_{_chosen_slot}")
        if _k:
            os.environ["ANTHROPIC_API_KEY"] = _k
            # A/E 等 Bedrock 渠道拒顶层 cache_control → 关(与 cci_env 对 slot A 的处理一致)
            os.environ.setdefault("CACHE_TOPLEVEL", "0")
    # ── prompt cache 亲和(2026-07-09):三方A/E(Bedrock)背后多区域,靠 metadata.user_id 把同一 worker 的
    #    请求亲和到同一区域,缓存(system+tools 前缀)才稳定命中(否则随机分流→每次 create=0)。
    #    设 CACHE_USER_ID_BASE 即启用:每 worker = base + 渠道 + pid(单 worker 内所有请求同 id、同区域;
    #    多 worker 不同 id→分散区域不过载)。core/agent.py 读 CACHE_USER_ID 传 extra_body.metadata.user_id。
    _uid_base = os.environ.get("CACHE_USER_ID_BASE", "")
    if _uid_base and not os.environ.get("CACHE_USER_ID"):
        _suffix = f"{_chosen_slot}_" if _chosen_slot else ""
        os.environ["CACHE_USER_ID"] = f"{_uid_base}_{_suffix}w{os.getpid()}"
    sid, run_dir = task["sample_id"], task["run_dir"]   # run_dir = 持久(FUSE)目标 + manifest 记录值
    config, seed = task["config"], task["seed"]

    if config.get("dry_run"):
        try:
            return _dry_worker(sid, run_dir, seed)
        except Exception as e:
            return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                    "error": f"{type(e).__name__}: {e}"}

    if _requires_anthropic_key() and not os.environ.get("ANTHROPIC_API_KEY"):
        # Anthropic 缺 key 不占坑（否则重跑会被当 skipped_exists）。OpenAI-compatible
        # PPTAgent 使用 STUDENT_API_KEY，不应被这道 Anthropic 专属门拦截。
        return {"sample_id": sid, "status": "error", "run_dir": run_dir,
                "error": "子进程环境里没有 ANTHROPIC_API_KEY"}

    local = _local_work_dir(config["batch"], sid)   # None=老行为(直接在 FUSE 跑);否则本地快盘跑
    work_dir = local or run_dir
    try:
        os.makedirs(work_dir, exist_ok=False)    # 并行安全:拒绝覆盖已存在目录
    except FileExistsError:
        shutil.rmtree(work_dir, ignore_errors=True)   # 脏残留(上次被中断)→ 清掉重建
        try:
            os.makedirs(work_dir, exist_ok=False)
        except FileExistsError:
            return {"sample_id": sid, "run_dir": run_dir, "status": "error",
                    "error": "工作目录反复无法创建(疑似有进程正在写它),跳过"}
    try:
        res = run_sample(sid, seed, work_dir, config)
        status = res.get("status", "completed") if isinstance(res, dict) else "completed"
        out = {"sample_id": sid, "run_dir": run_dir, "status": status}
        if isinstance(res, dict):
            out.update({k: v for k, v in res.items() if k not in out})
    except Exception as e:
        out = {"sample_id": sid, "run_dir": run_dir, "status": "error",
               "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-1500:]}

    if local:                                    # 本地→FUSE 持久化(完成/拒收/出错都搬,保留 audit/quarantine/salvage)
        try:
            _persist_back(local, run_dir)
        except Exception as e:                   # 持久化失败 → 降级 error,让 --resume 重跑(不留"假完成")
            out = {"sample_id": sid, "run_dir": run_dir, "status": "error",
                   "error": f"persist_back 失败: {type(e).__name__}: {e}"}
        shutil.rmtree(local, ignore_errors=True)  # 清本地(本地 rmtree ~ms)
    return out


def _dry_worker(sid, run_dir, seed):
    """--dry-run:不调模型,造 run_dir + 假轨迹,确定性分配 completed/rejected/error,
    验证进度条 / 断点续跑 / 隔离目录这些骨架。"""
    os.makedirs(run_dir, exist_ok=True)
    n = int(seed.get("slide_count") or 6)
    roll = int(hashlib.sha1(sid.encode()).hexdigest(), 16) % 10
    time.sleep(0.2 + (roll % 5) * 0.1)
    with open(os.path.join(run_dir, "dry.json"), "w", encoding="utf-8") as f:
        json.dump({"query": seed.get("query"), "slides": n}, f, ensure_ascii=False)
    if roll == 0:
        raise RuntimeError("dry-run 模拟错误")
    status = "rejected" if roll == 1 else "completed"
    return {"sample_id": sid, "run_dir": run_dir, "status": status, "slides": n, "dry": True}


def build_config(args):
    return {
        "batch": args.batch,
        "dry_run": args.dry_run,
        "model": os.environ.get("MODEL", "claude-opus-4-7-thinking"),
        "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", "https://tokenhub.sensetime.com"),
        "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1"),
        "image_model": os.environ.get("IMAGE_MODEL", "gpt-image-2"),
        "max_turns": int(os.environ.get("MAX_TURNS", "120")),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "16000")),
    }


# ---------------------------------------------------------------- 进度条

class Progress:
    """优先用 rich 渲染实时进度条;没装 rich 就退化成单行打印,功能不变。"""

    def __init__(self, total):
        self.total = total
        self.ok = self.rej = self.err = 0
        self.start = time.time()
        self._rich = None
        try:
            from rich.console import Console
            from rich.progress import (Progress as RP, SpinnerColumn, BarColumn,
                                       TextColumn, MofNCompleteColumn,
                                       TimeElapsedColumn, TimeRemainingColumn)
            self._console = Console()
            self._rich = RP(
                SpinnerColumn(), TextColumn("[bold cyan]蒸馏中[/]"), BarColumn(bar_width=None),
                MofNCompleteColumn(),
                TextColumn("[green]✓{task.fields[ok]}[/] [yellow]⊘{task.fields[rej]}[/] [red]✗{task.fields[err]}[/]"),
                TextColumn("·"), TimeElapsedColumn(), TextColumn("剩余"), TimeRemainingColumn(),
                console=self._console,
            )
            self._tid = self._rich.add_task("run", total=total, ok=0, rej=0, err=0)
            self._rich.start()
        except Exception:
            self._rich = None  # 退化模式

    def update(self, rec):
        st = rec.get("status")
        if st == "completed":
            self.ok += 1
        elif st == "rejected":
            self.rej += 1
        else:
            self.err += 1
        done = self.ok + self.rej + self.err
        if self._rich:
            self._rich.update(self._tid, advance=1, ok=self.ok, rej=self.rej, err=self.err)
            tag = {"completed": "[green]✓[/]", "rejected": "[yellow]⊘[/]"}.get(st, "[red]✗[/]")
            line = f"  {tag} {rec['sample_id']}  [dim]{st}[/]"
            if rec.get("error"):
                line += f"  [red]{str(rec['error'])[:80]}[/]"
            self._console.log(line)
        else:
            extra = f"  {rec.get('error','')[:80]}" if rec.get("error") else ""
            print(f"  [{done}/{self.total}] {rec['sample_id']}: {st}{extra}", flush=True)

    def close(self):
        if self._rich:
            self._rich.stop()
        print(f"\n完成。✓{self.ok} 通过  ⊘{self.rej} 丢弃  ✗{self.err} 失败  "
              f"用时 {time.time() - self.start:.0f}s", flush=True)


# ---------------------------------------------------------------- main

def _archive_failed(run_dir, batch, prev):
    """重跑前把上次失败的 run_dir 移到 `<batch>_failed/` 归档(保留已花 token 的轨迹),而非直接删。
    归档失败则兜底删,不卡住重跑。"""
    if not os.path.isdir(run_dir):
        return
    sid = os.path.basename(run_dir)
    st = (prev or {}).get("status", "failed")
    fdir = os.path.join(RUNS, batch + "_failed")
    try:
        os.makedirs(fdir, exist_ok=True)
        base = os.path.join(fdir, f"{sid}.{st}")
        dst, k = base, 1
        while os.path.exists(dst):
            dst = f"{base}.{k}"; k += 1
        shutil.move(run_dir, dst)
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="PPT agentic 蒸馏 —— 单条 / 批量并行 rollout。")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--query", help="单条:直接传一个 brief 跑一条")
    g.add_argument("--input", help="批量:seed jsonl 文件(每行一个 {query, lang?, slide_count?, ...})")
    ap.add_argument("--batch", required=True, help="批次名,决定 runs/<batch>/ 和 manifest 文件名")
    ap.add_argument("--workers", type=int, default=4, help="并行子进程数(单条固定 1)")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条(0=全部)")
    ap.add_argument("--resume", action="store_true", help="断点续跑:跳过已 completed;未完成/出错的清残留后重跑")
    ap.add_argument("--overwrite", action="store_true", help="从头重来:删该批次已有 runs/ 产物和 manifest 再跑(危险)")
    ap.add_argument("--max-attempts", type=int, default=3, help="单 sample 最多尝试几次(含历史),跑满仍未 completed 就放弃")
    ap.add_argument("--dry-run", action="store_true", help="不调模型,模拟跑,验证骨架与进度条")
    args = ap.parse_args()

    if args.resume and args.overwrite:
        raise SystemExit("--resume 和 --overwrite 含义相反,不能同时用。")
    if args.max_attempts < 1:
        raise SystemExit("--max-attempts 必须 >= 1。")

    if args.query:
        seeds = [{"query": args.query, "lang": "zh"}]
        args.workers = 1                                # 单条:固定单 worker
    else:
        seeds = load_seeds(args.input)
    if args.limit:
        seeds = seeds[: args.limit]

    # 护栏:worker 远超真实核数 → chromium 渲染争抢/OOM 崩 → 大量拒绝。
    if not args.dry_run:
        cores = cgroup_cpus()
        if args.workers > 2 * cores:
            print(f"⚠️  --workers={args.workers} 远超本容器真实核数 {cores}(cgroup 配额)。\n"
                  f"    每 worker 起 chromium 渲染,过度并发会 OOM/崩 → 大量拒绝。建议 ≈ {cores}~{2 * cores}"
                  f"(配 SLIDE_CONCURRENCY=2)。", flush=True)

    batch_dir = os.path.join(RUNS, args.batch)
    os.makedirs(LOGS, exist_ok=True)
    mpath = os.path.join(LOGS, f"{args.batch}.manifest.jsonl")

    # 安全闸:批次已有产物又没给 --resume,默认不动(防误删已完成轨迹)。
    has_runs = os.path.isdir(batch_dir) and os.listdir(batch_dir)
    if (os.path.exists(mpath) or has_runs) and not args.resume and not args.overwrite:
        raise SystemExit(f"批次 {args.batch} 已存在产物(manifest: {mpath} 或 runs/{args.batch}/)。\n"
                         f"  续跑:加 --resume   从头重来(删旧产物):加 --overwrite   或换个 --batch")
    if args.overwrite:
        shutil.rmtree(batch_dir, ignore_errors=True)
        if os.path.exists(mpath):
            os.remove(mpath)
    os.makedirs(batch_dir, exist_ok=True)

    done = load_manifest(mpath) if args.resume else {}
    config = build_config(args)
    existing_dirs = set(os.listdir(batch_dir)) if os.path.isdir(batch_dir) else set()

    tasks, skipped, exhausted, skipped_attach, seen, computed_sids = [], 0, 0, 0, {}, set()
    for seed in seeds:
        sid = make_sample_id(args.batch, seed, seen)
        computed_sids.add(sid)
        run_dir = os.path.join(batch_dir, sid)
        prev = done.get(sid, {})
        if prev.get("status") in TERMINAL:
            skipped += 1
            continue
        if prev.get("_attempts", 0) >= args.max_attempts:
            exhausted += 1
            continue
        miss = _missing_attachments(seed)       # 逐样本附件预检:缺失就跳过、不派 worker(否则白烧到阻塞)
        if miss:
            skipped_attach += 1
            append_manifest(mpath, {"sample_id": sid, "run_dir": run_dir,
                                    "status": "skipped_missing_attach",
                                    "n_missing": len(miss), "missing": miss[:5],
                                    "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
            continue
        if sid in existing_dirs:                # 重跑前:归档上次失败目录(保留已花 token 的轨迹)再清位
            _archive_failed(run_dir, args.batch, prev)
        tasks.append({"sample_id": sid, "seed": seed, "run_dir": run_dir, "config": config})

    orphans = set(done) - computed_sids
    if args.resume and done and len(orphans) >= 0.8 * len(done):
        print(f"⚠️  --resume 但已有 manifest 的 {len(orphans)}/{len(done)} 条对不上当前任何 seed。\n"
              f"    seed 文件很可能被改过 → 这次会几乎全量重跑、旧产物变孤儿。若非本意:换个 --batch。", flush=True)

    mode = "  [dry-run]" if args.dry_run else ""
    print(f"batch={args.batch}  seeds={len(seeds)}  待跑={len(tasks)}  "
          f"已完成跳过={skipped}  达重试上限={exhausted}  附件缺失跳过={skipped_attach}  "
          f"workers={args.workers}{mode}", flush=True)
    if not tasks:
        print("没有要跑的。")
        return

    prog = Progress(len(tasks))
    # —— 池容错(BrokenProcessPool resilience）——
    # 单个 worker abrupt death(高并发下 chromium/进程/信号量资源枯竭致 spawn 失败等)会让
    # ProcessPoolExecutor 整池 Broken → 剩余 in-flight 全级联 error(2026-07-16 事故:48×8 崩池、995 假 error)。
    # 这里:捕获崩溃 → 未完成任务重挂新池;每崩一次退一半并发(直接缓解资源枯竭根因);
    # 单任务反复连累崩池(poison)则隔离成 error;崩溃次数封顶,超了把剩余标 error 停(--resume 可再跑)。
    from concurrent.futures.process import BrokenProcessPool
    def _emit(rec):
        rec["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        append_manifest(mpath, rec); prog.update(rec)
    pending = {t["sample_id"]: t for t in tasks}       # sid→task,未出结果的都留着(便于崩池重挂)
    strikes = {}                                        # sid → 连累崩池次数
    MAX_STRIKES  = int(os.environ.get("POOL_TASK_MAX_STRIKES", "3"))
    MAX_REBUILDS = int(os.environ.get("POOL_MAX_REBUILDS", "12"))
    WORKER_FLOOR = int(os.environ.get("POOL_WORKER_FLOOR", "4"))

    # —— 活并发(live scaling,免重启调并发）——
    # 痛点:ProcessPoolExecutor 的 max_workers 启动即焊死,改并发只能杀进程 → 在飞 deck 全废。
    # 解法:池按「大上限 POOL_MAX」建(留头),真实并发由派发循环活控在飞数 ≤ 目标;
    #      目标从「控制文件」实时读 → `echo N > $CONCURRENCY_FILE` 即可在线增/减并发,不杀任何在飞 deck。
    POOL_MAX = int(os.environ.get("POOL_MAX_WORKERS", "0") or "0") or max(args.workers + 48, 128)
    POOL_MAX = max(POOL_MAX, args.workers)
    cfile = os.environ.get("CONCURRENCY_FILE") or os.path.join(
        os.environ.get("WORK_ROOT", "."), "CONCURRENCY")
    POLL = float(os.environ.get("CONCURRENCY_POLL_SEC", "5"))
    def _read_target(default):
        try:
            v = int(open(cfile).read().strip())
            return max(1, min(POOL_MAX, v))
        except Exception:
            return default
    try:                                                # 初值写盘,便于监控/人手改
        if not os.path.exists(cfile):
            os.makedirs(os.path.dirname(cfile) or ".", exist_ok=True)
            with open(cfile, "w") as f: f.write(str(args.workers))
    except Exception: pass
    print(f"[live] 活并发控制文件: {cfile}(当前目标={_read_target(args.workers)}, 池上限 POOL_MAX={POOL_MAX})\n"
          f"[live] 在线调并发: echo N > {cfile} (N∈[1,{POOL_MAX}]),不重启、不打断在飞 deck", flush=True)

    crash_cap = POOL_MAX                                # 崩池时临时压低目标的天花板(治本后极少触发)
    rebuilds = 0
    while pending:
        pool_kw = {"max_workers": POOL_MAX}
        if sys.version_info >= (3, 11):
            pool_kw["max_tasks_per_child"] = 1          # 每 sample 全新子进程,杜绝跨 sample 残留
        broke = False
        in_flight = {}                                  # fut → sid
        try:
            with cf.ProcessPoolExecutor(**pool_kw) as ex:
                while pending or in_flight:
                    # 惰性补位:在飞数 < 目标 就派新 deck(目标实时读控制文件,受崩池天花板夹住)
                    target = min(_read_target(args.workers), crash_cap)
                    inflight_sids = set(in_flight.values())
                    for sid, t in pending.items():
                        if len(in_flight) >= target: break
                        if sid in inflight_sids: continue
                        in_flight[ex.submit(worker, t)] = sid
                        inflight_sids.add(sid)
                    if not in_flight:
                        break                            # pending 空且无在飞 → 收工
                    # 等至少一个完成(带超时,好及时感知控制文件改动/补位)
                    done, _ = cf.wait(list(in_flight), timeout=POLL,
                                      return_when=cf.FIRST_COMPLETED)
                    for fut in done:
                        sid = in_flight.pop(fut)
                        try:
                            rec = fut.result()
                        except BrokenProcessPool:
                            broke = True; break          # 池已死,停止消费,外层重挂
                        except Exception as e:
                            rec = {"sample_id": sid, "run_dir": pending[sid]["run_dir"],
                                   "status": "error", "error": f"子进程崩溃: {type(e).__name__}: {e}"}
                        _emit(rec); pending.pop(sid, None)
                    if broke: break
        except BrokenProcessPool:
            broke = True
        # 崩池时:在飞的 sid 仍留在 pending(未 _emit),下一轮新池自动重派
        if not broke:
            break                                        # 本轮把 pending 跑干净,收工
        rebuilds += 1
        for sid in list(pending):                        # poison 隔离:反复连累崩池的单独标 error
            strikes[sid] = strikes.get(sid, 0) + 1
            if strikes[sid] > MAX_STRIKES:
                _emit({"sample_id": sid, "run_dir": pending[sid]["run_dir"], "status": "error",
                       "error": f"poison: 连续 {strikes[sid]} 次连累崩池,隔离"})
                pending.pop(sid, None)
        old = crash_cap
        crash_cap = max(WORKER_FLOOR, crash_cap // 2)    # 崩一次压一半目标天花板,缓解资源压力
        print(f"⚠️ 池崩溃(第 {rebuilds} 次):剩 {len(pending)} 待跑,并发天花板 {old}→{crash_cap} 重挂。",
              file=sys.stderr, flush=True)
        if rebuilds >= MAX_REBUILDS and pending:
            print(f"❌ 崩溃达上限 {MAX_REBUILDS},剩 {len(pending)} 标 error 停(--resume 可再跑)。", file=sys.stderr)
            for sid in list(pending):
                _emit({"sample_id": sid, "run_dir": pending[sid]["run_dir"], "status": "error",
                       "error": f"pool_broken_giveup after {rebuilds} rebuilds"})
                pending.pop(sid, None)
            break
    prog.close()
    print(f"manifest: {mpath}")
    if prog.err and not args.dry_run:
        print("有失败样本,可加 --resume 重跑未完成的。", file=sys.stderr)


if __name__ == "__main__":
    main()
