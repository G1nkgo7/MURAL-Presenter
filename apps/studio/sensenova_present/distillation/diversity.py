#!/usr/bin/env python3
"""数据多样性分析:从多个维度看 q260606 已完成 deck 的分布。
- 领域(class)/语言/页数:用 seed 元数据,覆盖全部 completed(便宜)。
- 页型/设计风格/色调/主色/背景色:抽样若干 deck 读 plan 与渲染图(贵)。
用法:uv run python diversity.py --batch q260606 --sample 250
"""
import argparse, glob, json, os, re, random
from collections import Counter

import distill  # 复用 make_sample_id / load_seeds
try:
    from PIL import Image
except Exception:
    Image = None

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_completed(batch):
    last = {}
    for line in open(os.path.join(ROOT, "logs", f"{batch}.manifest.jsonl")):
        line = line.strip()
        if line:
            try:
                o = json.loads(line)
                last[o["sample_id"]] = o
            except Exception:
                pass
    return {s: o for s, o in last.items() if o.get("status") == "completed"}


def seed_map(batch, seeds):
    seen = {}
    m = {}
    for sd in seeds:
        sid = distill.make_sample_id(batch, sd, seen)
        m[sid] = sd
    return m


def top(counter, k=12, total=None):
    total = total or sum(counter.values()) or 1
    return [(name, n, n / total * 100) for name, n in counter.most_common(k)]


# ---- 抽样维度:从 deck 文件提取 ----
_RE_TYPE = re.compile(r"页型\W*[:：]\s*([^\s（(]+)")
_RE_STYLE = re.compile(r"设计风格\W*[:：]\s*(.+)")
_RE_TONE = re.compile(r"色调\W*[:：]\s*(.+)")
_RE_ACCENT = re.compile(r"主色\W*[:：]\s*(.+)")


def _first_name(s):
    """从 '12 手绘插画(...) + 2 极简' 取主名 '手绘插画'。"""
    s = s.split("+")[0]
    s = re.split(r"[（(]", s)[0]
    s = re.sub(r"^\s*\d+\s*", "", s)      # 去前导编号
    return s.strip().strip("*：: ")[:16]


def classify_bg(png):
    if Image is None:
        return None
    try:
        with Image.open(png) as im:
            im = im.convert("RGB").resize((120, 68)); px = im.load(); w, h = im.size
            edge = [px[x, 0] for x in range(0, w, 4)] + [px[x, h - 1] for x in range(0, w, 4)] \
                 + [px[0, y] for y in range(0, h, 4)] + [px[w - 1, y] for y in range(0, h, 4)]
    except Exception:
        return None
    q = Counter((r // 16, g // 16, b // 16) for r, g, b in edge)
    (qr, qg, qb), _ = q.most_common(1)[0]
    r, g, b = qr * 16 + 8, qg * 16 + 8, qb * 16 + 8
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    sat = max(r, g, b) - min(r, g, b)
    if lum < 75: return "深底"
    if lum > 232 and sat < 14: return "纯白"
    if lum > 205 and r >= g >= b and (r - b) >= 12 and sat < 46: return "奶油/暖白"
    if sat < 22: return "浅灰"
    return "彩色"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", default="q260606")
    ap.add_argument("--sample", type=int, default=250)
    args = ap.parse_args()

    completed = load_completed(args.batch)
    print(f"completed deck 数: {len(completed)}\n")

    # ---------- 全量:领域 / 语言 / 页数(从 seed) ----------
    seeds = distill.load_seeds(os.path.join(ROOT, "..", "queryGeneration", "training_query", "query_260606.jsonl"))
    smap = seed_map(args.batch, seeds)
    cls, big_cls, lang, scount = Counter(), Counter(), Counter(), Counter()
    matched = 0
    for sid in completed:
        sd = smap.get(sid)
        if not sd:
            continue
        matched += 1
        c = sd.get("class", "?")
        cls[c] += 1
        big_cls[c.split("-")[0]] += 1            # 一级领域
        lang[sd.get("lang", "?")] += 1
        sc = sd.get("slide_count")
        if sc:
            scount[int(sc)] += 1

    print(f"=== 领域多样性(一级类目;匹配到 seed 的 {matched} 个 deck)===")
    print(f"不同一级领域数: {len(big_cls)} | 不同细分 class 数: {len(cls)}")
    for name, n, p in top(big_cls, 12, matched):
        print(f"  {name}: {n} ({p:.0f}%)")
    print(f"\n=== 语言 ===")
    for name, n in lang.most_common():
        print(f"  {name}: {n} ({n/matched*100:.0f}%)")
    print(f"\n=== 页数分布 ===")
    for sc in sorted(scount):
        bar = "█" * round(scount[sc] / max(scount.values()) * 24)
        print(f"  {sc:2d}页: {scount[sc]:4d} {bar}")

    # ---------- 抽样:页型 / 风格 / 色调 / 主色 / 背景 ----------
    rnd = random.Random(20260609)
    samp = rnd.sample(list(completed.values()), min(args.sample, len(completed)))
    ptype, style, tone, accent, bg = Counter(), Counter(), Counter(), Counter(), Counter()
    for o in samp:
        rd = o.get("run_dir") or os.path.join(ROOT, "runs", args.batch, "")
        for mp in glob.glob(os.path.join(rd, "plan", "slide_*.md")):
            try:
                t = _RE_TYPE.search(open(mp, encoding="utf-8", errors="ignore").read())
                if t:
                    ptype[t.group(1)] += 1
            except Exception:
                pass
        dm = os.path.join(rd, "plan", "deck.md")
        if os.path.exists(dm):
            txt = open(dm, encoding="utf-8", errors="ignore").read()
            for rx, cnt in ((_RE_STYLE, style), (_RE_TONE, tone), (_RE_ACCENT, accent)):
                m = rx.search(txt)
                if m:
                    cnt[_first_name(m.group(1))] += 1
        for png in glob.glob(os.path.join(rd, "renders", "slide_*.png")):
            c = classify_bg(png)
            if c:
                bg[c] += 1

    print(f"\n=== 抽样 {len(samp)} 个 deck 的设计维度 ===")
    print(f"页型 covered {len(ptype)}/14 种:")
    for name, n, p in top(ptype, 14):
        print(f"  {name}: {n} ({p:.0f}%)")
    print(f"\n设计风格 covered {len(style)} 种,top:")
    for name, n, p in top(style, 12):
        print(f"  {name}: {n} ({p:.0f}%)")
    print(f"\n色调 covered {len(tone)} 种,top:")
    for name, n, p in top(tone, 10):
        print(f"  {name}: {n} ({p:.0f}%)")
    print(f"\n主色 covered {len(accent)} 种,top:")
    for name, n, p in top(accent, 12):
        print(f"  {name}: {n} ({p:.0f}%)")
    print(f"\n背景模式(逐页):")
    for name, n, p in top(bg, 6):
        print(f"  {name}: {n} ({p:.0f}%)")


if __name__ == "__main__":
    main()
