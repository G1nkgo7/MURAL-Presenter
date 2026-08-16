#!/usr/bin/env python3
"""distill_monitor/audit.py —— 大批量蒸馏的随机抽查质检(机械检查,不调模型)。

从某批次的 manifest 里随机抽 K 条 **completed** deck,逐条做硬性质检,聚合后写一份**带时间戳**的
markdown 报告到 `distill_monitor/<batch>_<时间>.md`,并打印摘要。配合 `watch.sh` 每 2h 跑一次,
就能在大批量蒸馏跑几小时/几天时及时发现系统性问题(渲染坏、奶油色一边倒、末页不是结束页、"聆听"套话)。

检查项(每条抽样 deck):
  - 渲染完整性:每页都有非空白渲染图(灰度跨度);
  - 末页 = 结束页(读 plan 最后一页的"页型"行,验证强制结束页约定);
  - "聆听"套话:slides/*.html 里出现即违规;
  - 封面色调:采样 slide_01 平均色,分 dark/light/mid(防奶油色一边倒)。
整体进度取自 manifest(累计通过率 + 近窗口通过率)。

用法:
  uv run python distill_monitor/audit.py --batch q1            # 抽 8 条
  uv run python distill_monitor/audit.py --batch q1 --n 16     # 抽 16 条
"""
import argparse
import glob
import json
import os
import random
import re
import shutil
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = os.path.join(ROOT, "runs")
LOGS = os.path.join(ROOT, "log")
OUT = os.path.dirname(os.path.abspath(__file__))
BLANK_LUMA_RANGE = 24                              # 灰度跨度 < 此 = 近乎纯色 = 空白/破渲染


def load_manifest(batch):
    mpath = os.path.join(LOGS, f"{batch}.manifest.jsonl")
    rows = []
    if os.path.exists(mpath):
        with open(mpath, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    # 同 sid 取最后一条(后写覆盖)
    last = {}
    for r in rows:
        if r.get("sample_id"):
            last[r["sample_id"]] = r
    return rows, list(last.values())


def render_ok(png):
    """非空白判定:PIL 看灰度跨度。"""
    try:
        from PIL import Image
        with Image.open(png) as im:
            lo, hi = im.convert("L").getextrema()
        return (hi - lo) >= BLANK_LUMA_RANGE
    except Exception:
        return os.path.getsize(png) >= 26000


def cover_tone(png):
    """采样封面平均色,粗分色调:dark / light / mid;并标 cream(暖亮)。"""
    try:
        from PIL import Image
        with Image.open(png) as im:
            r, g, b = im.convert("RGB").resize((1, 1)).getpixel((0, 0))   # 1x1 = 平均色
        lum = 0.299 * r + 0.587 * g + 0.114 * b
        tone = "dark" if lum < 100 else ("light" if lum > 175 else "mid")
        cream = lum > 175 and r > 205 and g > 198 and r >= b      # 暖亮奶油
        return tone, cream, (round(r), round(g), round(b))
    except Exception:
        return "?", False, None


def last_page_type(run_dir):
    """读 plan 里页号最大的 slide_NN.md 的'页型'行,返回该字符串(用于验证末页=结束页)。"""
    plans = sorted(glob.glob(os.path.join(run_dir, "plan", "slide_*.md")))
    if not plans:
        return ""
    try:
        with open(plans[-1], encoding="utf-8") as f:
            lines = [ln.rstrip("\n") for ln in f][:12]      # 只看头部
    except Exception:
        return ""
    # ① 带标签的行:页型 / page type / role / slide type(兼容中英 + 新 skill 的 **Role:** 写法)
    lab = re.compile(r"(页型|page\s*type|slide\s*type|role)[^：:]*[：:]\s*(.+)", re.I)
    for ln in lines:
        m = lab.search(ln)
        if m:
            return m.group(2).replace("*", "").strip()[:40]
    # ② 退回 H1 标题破折号后的部分:`# slide_13 — Closing`
    for ln in lines:
        m = re.match(r"#\s*slide_\d+\s*[—\-–:]\s*(.+)", ln)
        if m:
            return m.group(1).replace("*", "").strip()[:40]
    return ""


def has_linting_word(run_dir):
    """slides/*.html 里是否出现"聆听"套话。"""
    for h in glob.glob(os.path.join(run_dir, "slides", "slide_*.html")):
        try:
            with open(h, encoding="utf-8") as f:
                if "聆听" in f.read():
                    return True
        except Exception:
            pass
    return False


_CANON_HTML = re.compile(r"^slide_\d+\.html$")     # 规范页名,排除 skill 的 .bak.html / .html.bak 备份
_CANON_PNG = re.compile(r"^slide_\d+\.png$")
_ECH_SRC = re.compile(r"""<script[^>]*\bsrc=["']([^"']*echarts[^"']*)["']""", re.I)


def echarts_refs(htmls):
    """扫页 HTML 里 echarts 的 <script src>。返回 (引用页数, [CDN 加载的 (slide, src)])。

    ⚠️ 经实测:**本地 vendor 路径**(如 `../skills/.../vendor/echarts.min.js`)渲染时经 run 目录 `skills`
    符号链接可解析、图表正常渲出;渲完符号链接可能被清,**故不按当前文件系统判本地路径"解析不到"**(会假阳性)。
    **CDN(http)加载**的 echarts:render.py 只 abort 字体 CDN、不拦它,但渲染节点网络不稳时**有加载失败→图表渲空的风险**
    (非必然——网络到了就正常)。这里只把 CDN 引用作为**风险提示**列出,**不判定为坏数据**(真坏要看渲染图,机械判不准)。
    建议 skill 统一改本地 vendor 以根除该风险。"""
    n, cdn = 0, []
    for h in htmls:
        try:
            t = open(h, encoding="utf-8").read()
        except Exception:
            continue
        for src in _ECH_SRC.findall(t):
            n += 1
            if src.startswith(("http://", "https://", "//")):
                cdn.append((os.path.basename(h), src[:48]))
    return n, cdn


def audit_deck(run_dir):
    sid = os.path.basename(run_dir.rstrip("/"))
    htmls = sorted(p for p in glob.glob(os.path.join(run_dir, "slides", "slide_*.html"))
                   if _CANON_HTML.match(os.path.basename(p)))
    pngs = sorted(p for p in glob.glob(os.path.join(run_dir, "renders", "slide_*.png"))
                  if _CANON_PNG.match(os.path.basename(p)))
    n_ech, ech_cdn = echarts_refs(htmls)
    blank = [os.path.basename(p) for p in pngs if not render_ok(p)]
    missing = max(0, len(htmls) - len(pngs))
    lpt = last_page_type(run_dir)
    _low = lpt.lower()                          # 兼容新 skill 的英文/混写标签(Closing / 封底式 / back cover)
    is_end = ("结束页" in lpt or "封底" in lpt or "closing" in _low or "back cover" in _low)
    ting = has_linting_word(run_dir)
    cover = pngs[0] if pngs else None
    tone, cream, rgb = cover_tone(cover) if cover else ("?", False, None)
    return {"sid": sid, "n_html": len(htmls), "n_png": len(pngs), "blank": blank,
            "missing": missing, "last_pt": lpt, "is_end": is_end, "ting": ting,
            "tone": tone, "cream": cream, "rgb": rgb, "n_ech": n_ech, "ech_cdn": ech_cdn}


QUARANTINE_STALE_SEC = int(os.environ.get("QUARANTINE_STALE_SEC", str(20 * 60)))  # 多久没动才算"非在跑"


def _newest_mtime(d):
    """目录树里最新一次修改时间(秒)。失败目录文件不多,代价可接受。"""
    newest = 0.0
    for root, _dirs, files in os.walk(d):
        try:
            newest = max(newest, os.path.getmtime(root))
        except OSError:
            pass
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return newest


def quarantine_failed(batch, last):
    """把 manifest 里已记 rejected/error 的 run 目录,移到 runs/<batch>-failed/(与蒸馏目录同名 + -failed)。

    ⚠️ 并发安全:`--resume` 会**重跑**之前失败的 sid,此刻 manifest 仍是旧 rejected 记录、但目录可能正被新 worker
    写。所以**只移"最近 QUARANTINE_STALE_SEC(默认 20min)没被修改过"的目录**(在跑的目录 mtime 新 → 跳过),
    绝不碰 in-flight。completed 不动(干净数据)。幂等:目录已移走则跳过;同 sid 多次失败用后缀防撞名。
    红利:失败目录移出 runs/<batch>/ 后,下次 --resume 的 FUSE rmtree 残留更少(更快),且这些 sid 仍会被重试。"""
    failed_dir = os.path.join(RUNS, f"{batch}-failed")
    moved, skipped_active = [], 0
    now = time.time()
    for r in last:
        if r.get("status") not in ("rejected", "error"):
            continue
        sid = r.get("sample_id")
        if not sid:
            continue
        src = os.path.join(RUNS, batch, sid)
        if not os.path.isdir(src):
            continue
        if now - _newest_mtime(src) < QUARANTINE_STALE_SEC:   # 可能正被重跑,跳过本轮
            skipped_active += 1
            continue
        os.makedirs(failed_dir, exist_ok=True)
        dst = os.path.join(failed_dir, sid)
        if os.path.exists(dst):
            i = 2
            while os.path.exists(f"{dst}_{i}"):
                i += 1
            dst = f"{dst}_{i}"
        try:
            shutil.move(src, dst)
            moved.append(sid)
        except Exception as e:
            print(f"[move-failed] {sid} 移动失败: {e}", flush=True)
    if skipped_active:
        print(f"[move-failed] 跳过 {skipped_active} 个近期仍在动的失败目录(可能正被重跑)", flush=True)
    return moved, failed_dir


def main():
    ap = argparse.ArgumentParser(description="大批量蒸馏随机抽查质检。")
    ap.add_argument("--batch", required=True, help="批次名(读 log/<batch>.manifest.jsonl)")
    ap.add_argument("--n", type=int, default=8, help="随机抽多少条 completed(默认 8)")
    ap.add_argument("--window", type=int, default=50, help="近窗口通过率的窗口大小(默认 50)")
    ap.add_argument("--move-failed", action="store_true",
                    help="把 manifest 已记 rejected/error 的 run 目录移到 runs/<batch>-failed/(隔离失败轨迹,只移终态失败、不碰 in-flight/completed)")
    args = ap.parse_args()

    rows, last = load_manifest(args.batch)
    if not rows:
        raise SystemExit(f"没有 manifest 或为空: log/{args.batch}.manifest.jsonl")
    from collections import Counter

    moved_failed = []
    if args.move_failed:                       # 隔离失败轨迹到 <batch>-failed/(在抽查质检之前做)
        moved_failed, _fdir = quarantine_failed(args.batch, last)
        if moved_failed:
            print(f"[move-failed] 已隔离 {len(moved_failed)} 条失败轨迹 → {_fdir}", flush=True)

    cum = Counter(r.get("status") for r in last)
    total = len(last)
    completed = [r for r in last if r.get("status") == "completed"]
    win = rows[-args.window:]
    win_ok = sum(1 for r in win if r.get("status") == "completed")
    cum_rate = 100 * len(completed) / total if total else 0
    win_rate = 100 * win_ok / len(win) if win else 0

    k = min(args.n, len(completed))
    sample = random.sample(completed, k) if k else []
    findings = []
    for r in sample:
        rd = r.get("run_dir") or os.path.join(RUNS, args.batch, r["sample_id"])
        if os.path.isdir(rd):
            findings.append(audit_deck(rd))

    # 聚合
    all_blank = [(f["sid"], f["blank"]) for f in findings if f["blank"] or f["missing"]]
    ting_bad = [f["sid"] for f in findings if f["ting"]]
    noend = [(f["sid"], f["last_pt"]) for f in findings if not f["is_end"]]
    tone_dist = Counter(f["tone"] for f in findings)
    cream_n = sum(1 for f in findings if f["cream"])
    ech_using = sum(1 for f in findings if f["n_ech"])
    ech_cdn_decks = [f["sid"] for f in findings if f["ech_cdn"]]

    ts = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    fname = f"{args.batch}_{time.strftime('%Y%m%d-%H%M')}.md"
    fpath = os.path.join(OUT, fname)

    lines = []
    A = lines.append
    A(f"# 抽查报告 — batch `{args.batch}`")
    A(f"> 时间 {ts} · 抽样 {len(findings)}/{len(completed)} 条 completed\n")
    A("## 整体进度(manifest)")
    A(f"- 终态记录 **{total}** 条:completed **{cum.get('completed',0)}** / "
      f"rejected {cum.get('rejected',0)} / error {cum.get('error',0)}")
    A(f"- 累计通过率 **{cum_rate:.0f}%** · 近 {len(win)} 条通过率 **{win_rate:.0f}%**")
    if args.move_failed:
        A(f"- 本次隔离失败轨迹 **{len(moved_failed)}** 条 → `runs/{args.batch}-failed/`")
    A("")
    A(f"## 抽样质量(随机 {len(findings)} 条)")
    A("| sid | 页数(html/png) | 空白 | 末页页型 | 末=结束页 | 聆听 | 封面色调(RGB) |")
    A("|---|---|---|---|---|---|---|")
    for f in findings:
        blank = ("⚠️" + ",".join(f["blank"])) if (f["blank"] or f["missing"]) else "—"
        end = "✓" if f["is_end"] else "✗"
        ting = "⚠️有" if f["ting"] else "—"
        rgb = f"{f['tone']} {f['rgb']}" if f["rgb"] else f["tone"]
        A(f"| {f['sid']} | {f['n_html']}/{f['n_png']} | {blank} | {f['last_pt'] or '—'} | {end} | {ting} | {rgb} |")
    A("\n## 聚合标记")
    A(f"- 空白/缺渲染:**{len(all_blank)}** 条" + (f" → {[s for s,_ in all_blank]}" if all_blank else " ✓"))
    A(f"- 末页非结束页:**{len(noend)}** 条" + (f" → {noend}" if noend else " ✓"))
    A(f"- 「聆听」套话:**{len(ting_bad)}** 条" + (f" → {ting_bad}" if ting_bad else " ✓"))
    A(f"- 封面色调分布:{dict(tone_dist)} · 奶油暖亮 {cream_n}/{len(findings)}"
      + ("  ⚠️ 偏奶油" if findings and cream_n >= 0.6 * len(findings) else ""))
    A(f"- ECharts:引用图表 {ech_using}/{len(findings)} 条(其中 CDN 加载 {len(ech_cdn_decks)} 条)。"
      "新 skill(06-18)已统一固定 CDN jsdelivr@5.5.0 + slide vision 自检兜底;真坏图表看渲染图,机械判不准。")

    problems = []
    if all_blank:
        problems.append(f"{len(all_blank)} 条有空白/缺渲染")
    if noend:
        problems.append(f"{len(noend)} 条末页不是结束页")
    if ting_bad:
        problems.append(f"{len(ting_bad)} 条含'聆听'")
    # 注:CDN echarts 只作风险提示,不进 problems(机械判不出真渲空,实测渲染图正常)
    if findings and cream_n >= 0.6 * len(findings):
        problems.append("封面偏奶油色一边倒")
    if win_rate < 50 and len(win) >= 10:
        problems.append(f"近窗口通过率偏低({win_rate:.0f}%)")
    A("\n## 结论")
    A(("**健康** ✓ 抽样未见系统性问题。" if not problems
       else "**需关注**:" + ";".join(problems) + "。"))

    report = "\n".join(lines)
    with open(fpath, "w", encoding="utf-8") as fp:
        fp.write(report + "\n")

    print(report)
    print(f"\n[报告已写] {fpath}", flush=True)


if __name__ == "__main__":
    main()
