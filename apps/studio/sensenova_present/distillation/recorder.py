#!/usr/bin/env python3
"""把通过验收的原始轨迹收集成训练用 SFT 数据集(拒绝采样落地)。

agent_loop 已经把每个 Agent 的**原始轨迹**写在各 sample 的 `_trace/` 下:
    runs/<batch>/<sid>/_trace/orchestrator/{messages.json, tools.json, system_prompt.md, config.json, images/}
    runs/<batch>/<sid>/_trace/subagents/slide_NN/{同上}
recorder 做三件事:
  1. **拒绝采样**:只收 manifest 里 status==completed 的 sample(rejected/error 直接丢)。
  2. **格式化**:每个 Agent 轨迹 = 一条 SFT 样本 {system, tools, messages, meta};一个 deck 产出
     1 条编排器样本 + N 条 slide subagent 样本。
  3. **自包含**:把 messages 里引用的截图复制进 data/<batch>/images/,并把图像块的 shot 路径
     改写成数据集相对路径,整份数据集可独立搬走。

每条轨迹还做一次轻量结构校验(非空、以 assistant 文字结尾、每个 tool_use 都有 tool_result),
不合格的跳过并记一笔——再防一层脏数据。

用法:
    uv run python recorder.py --batch smoke
    uv run python recorder.py --batch smoke --include-subagents false   # 只收编排器轨迹
"""
import argparse
import glob
import json
import os
import re
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(ROOT, "runs")
LOGS = os.path.join(ROOT, "logs")
DATA = os.path.join(ROOT, "data")

# 收尾套话黑名单:致谢页若出现这些**渲染可见**的陈词滥调,整条 deck 丢弃(脏数据直接丢,不打补丁)。
# 只禁"谢谢/感谢…聆听"这类套话,不禁裸"聆听"(正常动词"聆听讲解"合法)。注释里的自我提醒不算。
BANNED_CLICHE = ["谢谢聆听", "感谢聆听", "谢谢您的聆听", "感谢您的聆听",
                 "谢谢大家的聆听", "谢谢各位的聆听"]
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_TAG = re.compile(r"<[^>]+>")


def deck_cliche_hit(run_dir):
    """deck 任一页的渲染可见文本含收尾套话 → 返回命中串,否则 None。"""
    for h in sorted(glob.glob(os.path.join(run_dir, "slides", "slide_*.html"))):
        try:
            txt = open(h, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        vis = _RE_TAG.sub(" ", _RE_COMMENT.sub(" ", txt))
        for b in BANNED_CLICHE:
            if b in vis:
                return f"{os.path.basename(h)}:{b}"
    return None


def _read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_manifest(batch):
    """读 manifest,返回 {sample_id: 最终记录}(后写覆盖先写)。"""
    mpath = os.path.join(LOGS, f"{batch}.manifest.jsonl")
    if not os.path.exists(mpath):
        raise SystemExit(f"找不到 manifest: {mpath}(先用 distill.py 跑这个 batch)")
    done = {}
    with open(mpath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                done[r["sample_id"]] = r
    return done


def check_trajectory(messages):
    """轻量结构校验。返回 (ok, 原因)。"""
    if not messages:
        return False, "messages 为空"
    last = messages[-1]
    if last.get("role") != "assistant":
        return False, "未以 assistant 收尾"
    # 最后一条要有真正的文字
    has_text = any(isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
                   for b in last.get("content", []) if isinstance(last.get("content"), list))
    if not has_text:
        return False, "结尾 assistant 没有文字"
    # 每个 tool_use 都要有对应的 tool_result
    used, got = set(), set()
    for m in messages:
        for b in (m.get("content") or []) if isinstance(m.get("content"), list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                used.add(b.get("id"))
            elif b.get("type") == "tool_result":
                got.add(b.get("tool_use_id"))
    missing = used - got
    if missing:
        return False, f"{len(missing)} 个 tool_use 没有 tool_result"
    return True, "ok"


def collect_images(messages, trace_dir, img_out_dir, prefix):
    """把 messages 里图像块引用的截图复制进数据集 images/,并把 shot 改写成数据集相对路径。
    原地修改 messages(已是从磁盘读出的副本)。返回 (复制数, 缺失/不可解析数)。
    缺失数 > 0 说明该轨迹的图像不完整(快照写失败等),调用方应据"脏数据直接丢"原则丢弃整条。"""
    imgs = [y for m in messages if isinstance(m.get("content"), list)
            for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_result"
            and isinstance(b.get("content"), list)
            for y in b["content"] if isinstance(y, dict) and y.get("type") == "image"]
    # 先校验全部图像可解析;有缺失就不复制(避免给将被丢弃的轨迹留孤儿图)。
    missing = sum(1 for y in imgs
                  if not y.get("shot") or not os.path.exists(os.path.join(trace_dir, y["shot"])))
    if missing:
        return 0, missing
    for y in imgs:
        newname = f"{prefix}__{os.path.basename(y['shot'])}"
        shutil.copyfile(os.path.join(trace_dir, y["shot"]), os.path.join(img_out_dir, newname))
        y["shot"] = os.path.join("images", newname)   # 数据集相对路径
    return len(imgs), 0


def dedup_latest(sub_dirs):
    """同一页若被多次委派(slide_NN, slide_NN_r2, …),只保留**最后一次**(磁盘上幸存、且其
    render 是最终态的那版),丢弃被取代的早期尝试——避免把过时/被覆盖的轨迹喂进 SFT。
    返回 (保留的目录列表, 被丢弃的 superseded 数)。"""
    best = {}   # slide_key -> (attempt, dir)
    for d in sub_dirs:
        name = os.path.basename(d)
        if "_r" in name:
            key, _, a = name.rpartition("_r")
            try:
                attempt = int(a)
            except ValueError:
                key, attempt = name, 1
        else:
            key, attempt = name, 1
        if key not in best or attempt > best[key][0]:
            best[key] = (attempt, d)
    kept = sorted(v[1] for v in best.values())
    return kept, len(sub_dirs) - len(kept)


def build_record(trace_dir, sid, batch, manifest_rec, img_out_dir):
    """从一个轨迹目录构造一条 SFT 样本;不合格返回 (None, 原因)。"""
    mpath = os.path.join(trace_dir, "messages.json")
    if not os.path.exists(mpath):
        return None, "没有 messages.json"
    messages = _read_json(mpath)
    ok, why = check_trajectory(messages)
    if not ok:
        return None, why
    cfg = _read_json(os.path.join(trace_dir, "config.json")) if \
        os.path.exists(os.path.join(trace_dir, "config.json")) else {}
    tools = _read_json(os.path.join(trace_dir, "tools.json")) if \
        os.path.exists(os.path.join(trace_dir, "tools.json")) else []
    system = ""
    spath = os.path.join(trace_dir, "system_prompt.md")
    if os.path.exists(spath):
        with open(spath, encoding="utf-8") as f:
            system = f.read()

    role = cfg.get("role", "orchestrator")
    label = cfg.get("label", os.path.basename(trace_dir))
    # exit_reason:编排器取 manifest.orch_exit,subagent 从 manifest.workers 里按 label 匹配
    exit_reason = manifest_rec.get("orch_exit")
    if role == "subagent":
        for w in manifest_rec.get("workers", []):
            if w.get("label") == label:
                exit_reason = w.get("exit_reason")
                break

    prefix = f"{sid}__{label}"
    n_img, n_missing = collect_images(messages, trace_dir, img_out_dir, prefix)
    if n_missing:
        return None, f"{n_missing} 张引用的截图缺失(快照不完整),丢弃整条轨迹"
    return {
        "sample_id": sid,
        "batch": batch,
        "role": role,
        "label": label,
        "model": cfg.get("model"),
        "system": system,
        "tools": tools,
        "messages": messages,
        "meta": {
            "task": cfg.get("task"),
            "exit_reason": exit_reason,
            "n_images": n_img,
            "thinking": cfg.get("thinking"),
        },
    }, "ok"


def main():
    ap = argparse.ArgumentParser(description="把通过验收的轨迹收集成 SFT 数据集。")
    ap.add_argument("--batch", required=True, help="批次名(对应 logs/<batch>.manifest.jsonl)")
    ap.add_argument("--include-subagents", default="true",
                    help="是否收 slide subagent 轨迹(true/false,默认 true)")
    args = ap.parse_args()
    include_sub = str(args.include_subagents).lower() not in ("false", "0", "no")

    manifest = load_manifest(args.batch)
    completed = {sid: r for sid, r in manifest.items() if r.get("status") == "completed"}

    out_dir = os.path.join(DATA, args.batch)
    img_out_dir = os.path.join(out_dir, "images")
    os.makedirs(img_out_dir, exist_ok=True)
    out_path = os.path.join(DATA, f"{args.batch}.sft.jsonl")

    n_rec = n_skip = n_img = superseded = dropped_decks = 0
    skips = []
    with open(out_path, "w", encoding="utf-8") as out:
        for sid, rec in completed.items():
            run_dir = rec.get("run_dir") or os.path.join(RUNS, args.batch, sid)
            # deck 级脏数据闸:任一页致谢出现"谢谢聆听"类收尾套话 → 整条 deck 丢弃。
            hit = deck_cliche_hit(run_dir)
            if hit:
                dropped_decks += 1
                skips.append(f"{sid}: 收尾套话 {hit},丢弃整条 deck")
                continue
            trace_root = os.path.join(run_dir, "_trace")
            trace_dirs = [os.path.join(trace_root, "orchestrator")]
            if include_sub:
                kept, dropped = dedup_latest(sorted(glob.glob(os.path.join(trace_root, "subagents", "*"))))
                superseded += dropped
                trace_dirs += kept
            for td in trace_dirs:
                if not os.path.isdir(td):
                    continue
                record, why = build_record(td, sid, args.batch, rec, img_out_dir)
                if record is None:
                    n_skip += 1
                    skips.append(f"{sid}/{os.path.basename(td)}: {why}")
                    continue
                out.write(json.dumps(record, ensure_ascii=False) + "\n")
                n_rec += 1
                n_img += record["meta"]["n_images"]

    print(f"batch={args.batch}  completed_samples={len(completed)}/{len(manifest)}")
    print(f"SFT 样本={n_rec}(编排器+subagent)  跳过={n_skip}  收尾套话丢弃整deck={dropped_decks}  被取代丢弃={superseded}  图片={n_img}")
    print(f"数据集: {out_path}")
    print(f"图片目录: {img_out_dir}")
    if skips:
        print("跳过明细:")
        for s in skips[:20]:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
