#!/usr/bin/env python3
# 从 runs/q260606 随机抽 1000 个单页样本(render 截图 + 单页 md + 用户 query),
# 每条数据一个文件夹,输出到 biaozhu_20260609/
import os, glob, json, random, shutil, re

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(ROOT, "runs", "q260606")
OUT  = os.path.join(ROOT, "biaozhu_20260609")
N    = 1000
SEED = 20260609

os.makedirs(OUT, exist_ok=True)

# 1) 收集所有合法的 (run, slide) 单页对:render png 与 plan md 都存在,且该 run 有 query
print("扫描所有单页对 ...", flush=True)
pairs = []          # (sample_id, "slide_NN")
query_cache = {}    # sample_id -> query
runs = sorted(d for d in os.listdir(RUNS)
              if d.startswith("q260606_") and os.path.isdir(os.path.join(RUNS, d)))
for r in runs:
    rd = os.path.join(RUNS, r)
    cfg = os.path.join(rd, "_trace", "orchestrator", "config.json")
    try:
        q = json.load(open(cfg)).get("task")
    except Exception:
        q = None
    if not q:
        continue
    query_cache[r] = q
    for png in glob.glob(os.path.join(rd, "renders", "slide_*.png")):
        nn = os.path.basename(png)[:-4]              # slide_NN
        if os.path.exists(os.path.join(rd, "plan", nn + ".md")):
            pairs.append((r, nn))

print(f"合法单页对总数: {len(pairs)}", flush=True)

# 2) 随机抽 1000(固定种子,可复现)
random.seed(SEED)
sample = random.sample(pairs, min(N, len(pairs)))
sample.sort()
print(f"抽取: {len(sample)}", flush=True)

# 3) 逐条导出,每条一个文件夹
manifest = open(os.path.join(OUT, "manifest.jsonl"), "w")
ok = 0
for sid, nn in sample:
    rd = os.path.join(RUNS, sid)
    src_png = os.path.join(rd, "renders", nn + ".png")
    src_md  = os.path.join(rd, "plan",    nn + ".md")
    folder  = os.path.join(OUT, f"{sid}_{nn}")
    os.makedirs(folder, exist_ok=True)
    shutil.copy2(src_png, os.path.join(folder, nn + ".png"))
    shutil.copy2(src_md,  os.path.join(folder, nn + ".md"))
    with open(os.path.join(folder, "query.txt"), "w") as f:
        f.write(query_cache[sid].rstrip() + "\n")
    manifest.write(json.dumps({
        "folder": os.path.basename(folder),
        "sample_id": sid,
        "slide": nn,
        "image": nn + ".png",
        "md": nn + ".md",
        "src_image": os.path.relpath(src_png, ROOT),
        "src_md": os.path.relpath(src_md, ROOT),
    }, ensure_ascii=False) + "\n")
    ok += 1
    if ok % 100 == 0:
        print(f"  已导出 {ok}/{len(sample)}", flush=True)
manifest.close()
print(f"完成: 导出 {ok} 条到 {OUT}", flush=True)
