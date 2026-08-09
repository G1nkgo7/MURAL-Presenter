#!/usr/bin/env python3
"""定时质量审计:随机抽样已完成的 deck,统计拒绝原因 + 渲染质量(背景色分布、违禁词、
主色协调等),把发现写成报告。供 cron 每 ~2h 调一次,保证每轮口径一致。

用法:
    uv run python audit_sample.py --batch q260606 --n 14 --out log/<时间戳>
不修改任何数据;只读 runs/ 与 manifest,产出一份 report.md。
"""
import argparse, glob, json, os, re, random, time
from collections import Counter

try:
    from PIL import Image
except Exception:
    Image = None


# ---------------- manifest 统计 + 拒绝归因 ----------------
def load_manifest(mpath):
    last = {}
    if not os.path.exists(mpath):
        return last
    for line in open(mpath):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("sample_id"):
            last[o["sample_id"]] = o
    return last


def _dur(sd):
    fs = glob.glob(os.path.join(sd, "**"), recursive=True)
    ts = [os.path.getmtime(f) for f in fs if os.path.isfile(f)]
    return (max(ts) - min(ts)) if len(ts) >= 2 else 0


def rejection_taxonomy(last):
    rej = [o for o in last.values() if o.get("status") == "rejected"]
    cat = Counter()
    for o in rej:
        reason = o.get("reason", "")
        if "编排器未自然收尾" in reason:
            cat["编排器API中断"] += 1
            continue
        if "没有成功渲染" in reason or "空白" in reason or "没有产出" in reason:
            cat["渲染缺失或空白"] += 1
            continue
        if "subagent 未通过" in reason:
            rd = o.get("run_dir") or ""
            labs = re.findall(r"slide_\d+(?:_r\d)?", reason)
            timeout = apifail = False
            for lab in labs:
                sd = os.path.join(rd, "_trace", "subagents", lab)
                mp = os.path.join(sd, "messages.json")
                if not os.path.exists(mp):
                    continue
                try:
                    msgs = json.load(open(mp))
                except Exception:
                    continue
                if msgs and msgs[-1]["role"] == "assistant" and _dur(sd) >= 600:
                    timeout = True
                else:
                    apifail = True
            if timeout:
                cat["子代理超时(>600s)"] += 1
            elif apifail:
                cat["子代理API中断/空回复"] += 1
            else:
                cat["其他(子代理)"] += 1
            continue
        cat["其他"] += 1
    return len(rej), cat


# ---------------- 渲染质量分析 ----------------
def classify_bg(png):
    """从边缘像素估计背景主色,粗分类:dark / cream / white / light-tint / colorful。"""
    if Image is None:
        return "no_pil"
    try:
        with Image.open(png) as im:
            im = im.convert("RGB").resize((160, 90))
            px = im.load()
            w, h = im.size
            edge = []
            for x in range(0, w, 4):
                edge.append(px[x, 0]); edge.append(px[x, h - 1])
            for y in range(0, h, 4):
                edge.append(px[0, y]); edge.append(px[w - 1, y])
    except Exception:
        return "err"
    # 边缘众数色(取最常见的量化色)
    q = Counter((r // 16, g // 16, b // 16) for r, g, b in edge)
    (qr, qg, qb), _ = q.most_common(1)[0]
    r, g, b = qr * 16 + 8, qg * 16 + 8, qb * 16 + 8
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    mx, mn = max(r, g, b), min(r, g, b)
    sat = (mx - mn)
    if lum < 75:
        return "dark"
    if lum > 232 and sat < 14:
        return "white"
    # 偏暖且浅 = 奶油/米黄:R 最大,B 明显低,整体亮
    if lum > 205 and r >= g >= b and (r - b) >= 12 and sat < 46:
        return "cream"
    if sat < 22:
        return "light-gray"
    return "colorful"


def deck_bg(rdir):
    """整个 deck 的主背景模式:多数页的背景类别。"""
    cls = [classify_bg(p) for p in sorted(glob.glob(os.path.join(rdir, "renders", "slide_*.png")))]
    cls = [c for c in cls if c not in ("no_pil", "err")]
    if not cls:
        return None, []
    return Counter(cls).most_common(1)[0][0], cls


def live_metrics():
    """实时运行诊断。内存按 cgroup v2 memory.stat 区分 anon(真实占用)vs slab/file(可回收),
    避免把 inode 缓存误当成内存危机。"""
    m = {}
    try:
        m["chromium"] = int(os.popen("pgrep -f -c '[c]hrome' 2>/dev/null").read().strip() or 0)
    except Exception:
        m["chromium"] = None
    try:
        m["distill_alive"] = int(os.popen("pgrep -f -c '[d]istill.py'").read().strip() or 0) > 0
    except Exception:
        m["distill_alive"] = None
    try:
        m["load1"] = float(open("/proc/loadavg").read().split()[0])
    except Exception:
        m["load1"] = None
    stat = {}
    try:
        for line in open("/sys/fs/cgroup/memory.stat"):
            k, v = line.split()[:2]
            stat[k] = int(v)
        g = 1024 ** 3
        m["mem_anon_g"] = round(stat.get("anon", 0) / g, 1)
        m["mem_slab_g"] = round(stat.get("slab", 0) / g, 1)
        m["mem_file_g"] = round(stat.get("file", 0) / g, 1)
        m["mem_max_g"] = round(int(open("/sys/fs/cgroup/memory.max").read()) / g)
    except Exception:
        pass
    try:
        ev = dict(l.split() for l in open("/sys/fs/cgroup/memory.events"))
        m["oom_kill"] = int(ev.get("oom_kill", 0))
        m["mem_max_hit"] = int(ev.get("max", 0))
    except Exception:
        pass
    return m


def recent_rates(last):
    """从 finished_at 算最近 1h/2h 瞬时完成率(比累计率更能反映当下健康度)。"""
    def parse(t):
        try:
            return time.mktime(time.strptime(str(t).split(".")[0], "%Y-%m-%d %H:%M:%S"))
        except Exception:
            return None
    now = time.time()
    out = {}
    for win, lbl in [(3600, "1h"), (7200, "2h")]:
        c = r = 0
        for o in last.values():
            t = parse(o.get("finished_at"))
            if t and t > now - win:
                if o.get("status") == "completed":
                    c += 1
                elif o.get("status") == "rejected":
                    r += 1
        out[lbl] = {"completed": c, "rejected": r,
                    "rate": round(c / (c + r), 3) if (c + r) else None}
    return out


SUMMARY_RE = re.compile(r"<!--\s*SUMMARY\s+(\{.*?\})\s*-->")


def prev_summary(md_path):
    """上一轮 audit 的摘要 —— 读单文件 .md 里最后一条 `<!-- SUMMARY {...} -->` 注释(渲染不可见),
    用于算增量。整个任务只占这一个 .md 文件。"""
    if not os.path.exists(md_path):
        return None
    txt = open(md_path, encoding="utf-8").read()
    ms = SUMMARY_RE.findall(txt)
    if not ms:
        return None
    try:
        return json.loads(ms[-1])
    except Exception:
        return None


# 只禁"收尾套话"(致谢页的陈词滥调),不禁裸"聆听"——后者是正常动词("聆听讲解"完全合法)。
BANNED_CLICHE = ["谢谢聆听", "感谢聆听", "谢谢您的聆听", "感谢您的聆听",
                 "谢谢大家的聆听", "谢谢各位的聆听", "感谢您的聆听"]
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_TAG = re.compile(r"<[^>]+>")


def _visible_text(html):
    """剥掉 HTML 注释 + 标签,只留**渲染可见**的文本。模型常在注释里写'绝不用谢谢聆听'之类的
    自我提醒,绝不能把这些算成违规。"""
    return _RE_TAG.sub(" ", _RE_COMMENT.sub(" ", html))


def scan_html_issues(rdir):
    issues = []
    htmls = sorted(glob.glob(os.path.join(rdir, "slides", "slide_*.html")))
    for h in htmls:
        try:
            txt = open(h, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        vis = _visible_text(txt)
        for b in BANNED_CLICHE:
            if b in vis:
                issues.append(f"{os.path.basename(h)} 收尾套话「{b}」")
    return issues, len(htmls)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--n", type=int, default=14)
    ap.add_argument("--out", default="log/quality_audit.md",
                    help="本任务唯一的日志文件(单个 .md);每轮按天分节追加")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    # 整个审计任务只占一个 .md 文件,内部按天分节,每轮追加。
    day = time.strftime("%Y-%m-%d", time.gmtime())
    hm = time.strftime("%H:%M", time.gmtime())
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    mpath = os.path.join("logs", f"{args.batch}.manifest.jsonl")
    last = load_manifest(mpath)
    statuses = Counter(o.get("status", "?") for o in last.values())
    comp = statuses.get("completed", 0)
    rej_n, taxo = rejection_taxonomy(last)

    completed = [o for o in last.values() if o.get("status") == "completed" and o.get("run_dir") and os.path.isdir(o["run_dir"])]
    rnd = random.Random(args.seed if args.seed is not None else int(time.time()))
    sample = rnd.sample(completed, min(args.n, len(completed)))

    bg_modes = Counter()
    per_deck = []
    all_html_issues = []
    for o in sample:
        rd = o["run_dir"]
        mode, cls = deck_bg(rd)
        if mode:
            bg_modes[mode] += 1
        hiss, n_html = scan_html_issues(rd)
        all_html_issues += [(os.path.basename(rd), x) for x in hiss]
        per_deck.append((os.path.basename(rd), o.get("n_slides"), mode, Counter(cls)))

    metrics = live_metrics()
    rates = recent_rates(last)
    prev = prev_summary(args.out)   # 读本 .md 里最后一条 SUMMARY 注释

    # ---- 自动判问题:只标真问题,避免误报 ----
    problems = []
    sb_tmp = sum(bg_modes.values()) or 1
    cream_ratio = bg_modes.get("cream", 0) / sb_tmp
    # cream 用两级判据,避免小样本(16条)误报:本轮 >0.40 才**升级到大样本复核**,
    # 复核(随机 60 deck)仍 >0.35 才判回归(总体真实 cream 约 23%,σ≈10% 时 16 抽 44% 是常见噪声)。
    cream_confirm = None
    if cream_ratio > 0.40:
        cc = Counter()
        for o in rnd.sample(completed, min(60, len(completed))):
            m, _ = deck_bg(o["run_dir"])
            if m:
                cc[m] += 1
        tc = sum(cc.values()) or 1
        cream_confirm = cc.get("cream", 0) / tc
        if cream_confirm > 0.35:
            problems.append(f"奶油/米黄背景占主导(本轮{cream_ratio*100:.0f}%,复核{cream_confirm*100:.0f}%>35%),需在 SKILL 收紧 anti-cream")
    # 「聆听」收尾套话:recorder.py 已在收集时按 deck 丢弃(脏数据直接丢)。总体率约 1.2%,所以
    # 16 抽里偶尔命中 1 个 deck 属预期噪声,只记信息、不当问题;只有命中 ≥2 个 deck(率明显升高,
    # 疑似 SKILL 回归)才升级为需处理的问题。
    cliche_decks = {rd for rd, _ in all_html_issues}
    if len(cliche_decks) >= 2:
        problems.append(f"「聆听」收尾套话命中 {len(cliche_decks)} 个 deck(率偏高,疑 SKILL 禁令回归),需加固 SKILL 收尾页规则")
    r1 = rates.get("1h", {}).get("rate")
    if r1 is not None and r1 < 0.55:
        problems.append(f"最近 1h 瞬时完成率 {r1*100:.0f}% 偏低(<55%),查 API 抖动/超时是否系统性")
    # 真实内存风险只看两点:① 真的杀进程了(oom_kill>0);② 不可回收的 anon 逼近上限(>80%)。
    # 单纯"触顶(max事件)"在有大量可回收 slab/file 缓存时属内核正常回收,不杀进程、不伤吞吐,不算问题。
    mx = metrics.get("mem_max_g") or 128
    anon = metrics.get("mem_anon_g") or 0
    if metrics.get("oom_kill", 0) > 0:
        problems.append(f"cgroup 发生 OOM 杀进程(oom_kill={metrics.get('oom_kill')}),需降 workers")
    elif anon > 0.80 * mx:
        problems.append(f"真实内存(anon {anon}G)逼近上限 {mx}G(>80%),有 OOM 风险,需降 workers")
    if metrics.get("distill_alive") is False:
        problems.append("distill 进程不在运行!需排查是否崩溃,必要时 --resume 重启")

    total = sum(statuses.values())
    fin = comp + rej_n
    sb = sum(bg_modes.values()) or 1
    verdict = "✅ 无问题" if not problems else f"⚠️ {len(problems)} 个问题"

    # ---- 写报告(本轮 = 当天分节下的一小节,带粗体小标题;追加到唯一的 .md)----
    lines = []
    P = lines.append
    P(f"### {hm} UTC — {verdict}"
      + (f" · 累计 {comp/fin*100:.1f}%" if fin else "")
      + (f" · 最近1h {int(rates['1h']['rate']*100)}%" if rates.get('1h', {}).get('rate') is not None else ""))
    P("")
    if problems:
        P("**结论:发现问题**")
        for x in problems:
            P(f"- ⚠️ {x}")
    else:
        P("**结论:** ✅ 本轮未发现需要修改的问题(质量/资源/进程均正常)")
    P("")
    P(f"**总量** — 唯一样本 {total}:" + " / ".join(f"{k} {v}" for k, v in statuses.most_common()))
    if fin:
        P(f"- 累计完成率 {comp}/{fin} = **{comp/fin*100:.1f}%**")
    if prev:
        d_comp = comp - prev.get("completed", 0)
        d_rej = rej_n - prev.get("rejected", 0)
        d_fin = d_comp + d_rej
        P(f"- 距上轮({prev.get('ts_utc','?')}): 新完成 {d_comp} / 新拒绝 {d_rej}"
          + (f"  → 区间完成率 **{d_comp/d_fin*100:.0f}%**" if d_fin > 0 else ""))
    for lbl in ("1h", "2h"):
        rr = rates.get(lbl, {})
        if rr.get("rate") is not None:
            P(f"- 最近{lbl}瞬时: 完成 {rr['completed']} / 拒绝 {rr['rejected']} → {rr['rate']*100:.0f}%")
    P("")
    P(f"**运行诊断** — distill存活 {metrics.get('distill_alive')} | chromium {metrics.get('chromium')} | load1 {metrics.get('load1')}")
    P(f"- 内存 anon(真实) {metrics.get('mem_anon_g')}G / slab(可回收) {metrics.get('mem_slab_g')}G / "
      f"file(缓存) {metrics.get('mem_file_g')}G / 上限 {metrics.get('mem_max_g')}G;"
      f" oom_kill={metrics.get('oom_kill')} 触顶={metrics.get('mem_max_hit')}（只看 anon+oom_kill 判内存风险）")
    P("")
    P("**拒绝归因**")
    if rej_n:
        for k, v in taxo.most_common():
            P(f"- {k}: {v} ({v/rej_n*100:.1f}%)")
    else:
        P("- (暂无拒绝样本)")
    P("")
    P(f"**随机抽样 {len(sample)} 条** — 背景模式:" + ", ".join(
        f"{k} {v}({v/sb*100:.0f}%){'⚠️偏多' if (k=='cream' and v/sb>0.40) else ''}"
        for k, v in bg_modes.most_common())
      + (f"(cream 触发大样本复核=**{cream_confirm*100:.0f}%**)" if cream_confirm is not None else ""))
    if all_html_issues:
        P(f"- 收尾套话:命中 {len(cliche_decks)} 个 deck(已由 recorder 收集时丢弃,脏数据不入库):")
        for rd, x in all_html_issues:
            P(f"  - {rd}: {x}")
    else:
        P("- 收尾套话扫描:✅ 未发现「谢谢/感谢聆听」类")
    P("<details><summary>抽样明细</summary>")
    P("")
    for name, ns, mode, cc in per_deck:
        P(f"- {name}: {ns}页, 主背景={mode}, 各页={dict(cc)}")
    P("</details>")
    P("")

    # 机器可读摘要(藏成 HTML 注释,渲染不可见;供下一轮算增量)
    summary = {
        "batch": args.batch,
        "ts_utc": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "total": total,
        "completed": comp,
        "rejected": rej_n,
        "completion_rate": round(comp / fin, 4) if fin else None,
        "rejection_taxonomy": dict(taxo),
        "sample_n": len(sample),
        "bg_modes": dict(bg_modes),
        "cream_ratio": round(bg_modes.get("cream", 0) / sb, 3),
        "cream_confirm": round(cream_confirm, 3) if cream_confirm is not None else None,
        "dark_ratio": round(bg_modes.get("dark", 0) / sb, 3),
        "html_issues": [f"{rd}:{x}" for rd, x in all_html_issues],
        "recent_rates": rates,
        "metrics": metrics,
        "problems": problems,
    }
    lines.append(f"<!-- SUMMARY {json.dumps(summary, ensure_ascii=False)} -->")

    # 整个任务只写这一个 .md:文件首次创建写大标题;每天首轮写一个 `## 日期` 分节标题;本轮追加一小节。
    existing = open(args.out, encoding="utf-8").read() if os.path.exists(args.out) else ""
    with open(args.out, "a", encoding="utf-8") as f:
        if not existing:
            f.write(f"# 数据质量审计日志 · batch={args.batch} (UTC)\n\n")
        if f"## {day}" not in existing:
            f.write(f"## {day}\n\n")
        f.write("\n".join(lines))
        f.write("\n\n")

    print(args.out)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
