#!/usr/bin/env python3
"""把蒸馏出的 Anthropic 原生轨迹转成「中间格式 / OpenAI 格式」SFT 数据集。

源(每个 deck 在 runs/<batch>/<sid>/_trace/ 下):
    orchestrator/{messages.json, tools.json, system_prompt.md, config.json, images/}
    subagents/slide_NN/{同上}
messages.json 是清洗过的 Anthropic 原生格式:
    user(str) / assistant([thinking,text,tool_use*]) / user([tool_result*])
    图像块已被换成 {type:image, shot:"images/view_NN.png"}(相对各自 trace_dir)

产物:
    <out>/<batch>/data.jsonl     每行一条轨迹(编排器 1 条 + 每个 slide subagent 1 条)
    图片不拷贝:image[] 直接放原图(runs/ 下 view_NN.png)的**绝对路径**。

OpenAI 中间格式每条:
    {status, total_steps, enable_thinking, messages[], tools[], image[], metadata{}}
    - assistant: {role, content, reasoning_content?, tool_calls?[{id,type:function,function:{name,arguments}}]}
    - tool:      {role:tool, name, tool_call_id, content, success}
    - <image> 占位写进 content,原图绝对路径按出现顺序放进 image[](长度=标签数)

沿用拒绝采样落地的脏数据红线:只收 manifest 里 completed 的 deck;轨迹结构不合格 / 引用截图缺失 /
deck 出现"谢谢聆听"类收尾套话 → 整条(或整 deck)丢弃,不修不补。

用法:
    uv run python to_openai.py --batch q260606
    uv run python to_openai.py --batch q260606 --out data_openai --include-subagents false
"""
import argparse
import glob
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
RUNS = os.path.join(ROOT, "runs")
LOGS = os.path.join(ROOT, "logs")

# 收尾套话黑名单(可见正文出现即丢整条 deck);只禁套话,不禁裸"聆听"。
BANNED_CLICHE = ["谢谢聆听", "感谢聆听", "谢谢您的聆听", "感谢您的聆听",
                 "谢谢大家的聆听", "谢谢各位的聆听"]
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_TAG = re.compile(r"<[^>]+>")


# ----------------------------- 读取 / 脏数据闸 -----------------------------
def _read_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_manifest(batch):
    mpath = os.path.join(LOGS, f"{batch}.manifest.jsonl")
    if not os.path.exists(mpath):
        raise SystemExit(f"找不到 manifest: {mpath}")
    done = {}
    for line in open(mpath, encoding="utf-8"):
        line = line.strip()
        if line:
            try:
                r = json.loads(line)
                done[r["sample_id"]] = r
            except Exception:
                pass
    return done


def deck_cliche_hit(run_dir):
    for h in sorted(glob.glob(os.path.join(run_dir, "slides", "slide_*.html"))):
        try:
            vis = _RE_TAG.sub(" ", _RE_COMMENT.sub(" ", open(h, encoding="utf-8", errors="ignore").read()))
        except Exception:
            continue
        for b in BANNED_CLICHE:
            if b in vis:
                return f"{os.path.basename(h)}:{b}"
    return None


def dedup_latest(sub_dirs):
    """同一页多次委派(slide_NN, slide_NN_r2…)只留最后一次。"""
    best = {}
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
    return sorted(v[1] for v in best.values())


def check_structure(messages):
    """轻量结构校验:非空、以 assistant 文字收尾、每个 tool_use 有 tool_result。"""
    if not messages:
        return False, "messages 为空"
    last = messages[-1]
    if last.get("role") != "assistant":
        return False, "未以 assistant 收尾"
    if not any(isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()
               for b in (last.get("content") or []) if isinstance(last.get("content"), list)):
        return False, "结尾 assistant 没有文字"
    used, got = set(), set()
    for m in messages:
        for b in (m.get("content") or []) if isinstance(m.get("content"), list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                used.add(b.get("id"))
            elif b.get("type") == "tool_result":
                got.add(b.get("tool_use_id"))
    if used - got:
        return False, f"{len(used - got)} 个 tool_use 缺 tool_result"
    return True, "ok"


# ----------------------------- 格式转换 -----------------------------
def convert_tools(anthropic_tools):
    """Anthropic {name,description,input_schema} → OpenAI {type:function,function:{...,parameters}}。"""
    out = []
    for t in anthropic_tools or []:
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return out


def _tool_result_to_content(tr, trace_dir, image_list):
    """把一个 tool_result 转成 (content_str, success)。图像块→插 <image> 占位,并把原图的
    **绝对路径**(不拷贝、直接引用 runs/ 下的 view_NN.png)按序收进 image_list。
    返回 (content, success, missing) —— missing=True 表示引用的截图缺失(调用方据此丢整条)。"""
    is_error = bool(tr.get("is_error"))
    success = not is_error
    cc = tr.get("content")
    if isinstance(cc, str):
        return cc, success, False
    if not isinstance(cc, list):
        return ("" if cc is None else str(cc)), success, False
    parts, missing = [], False
    for y in cc:
        if not isinstance(y, dict):
            parts.append(str(y))
        elif y.get("type") == "text":
            parts.append(y.get("text", ""))
        elif y.get("type") == "image":
            shot = y.get("shot")
            src = os.path.join(trace_dir, shot) if shot else None
            if not src or not os.path.exists(src):
                missing = True
                continue
            image_list.append(os.path.abspath(src))   # 原图绝对路径,不拷贝
            parts.append("<image>")
        # 其它类型块忽略
    return "\n".join(p for p in parts if p != ""), success, missing


def convert_messages(messages, system, enable_thinking, trace_dir):
    """Anthropic 原生 messages → OpenAI messages[] + image[]。返回 (oai_msgs, image_list, missing)。"""
    oai, image_list = [], []
    name_by_id = {}
    missing_any = False
    if system:
        oai.append({"role": "system", "content": system})
    for m in messages:
        role, content = m.get("role"), m.get("content")
        # ---- 纯文本 user / assistant(content 是字符串)----
        if isinstance(content, str):
            if role == "assistant":
                msg = {"role": "assistant", "content": content}
                oai.append(msg)
            else:
                oai.append({"role": "user", "content": content})
            continue
        if not isinstance(content, list):
            continue
        # ---- assistant:thinking→reasoning_content, text→content, tool_use→tool_calls ----
        if role == "assistant":
            texts, thinks, calls = [], [], []
            for b in content:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t == "text":
                    texts.append(b.get("text", ""))
                elif t == "thinking":
                    thinks.append(b.get("thinking", ""))
                elif t == "tool_use":
                    name_by_id[b.get("id")] = b.get("name")
                    calls.append({
                        "id": b.get("id"),
                        "type": "function",
                        "function": {"name": b.get("name"), "arguments": b.get("input", {})},
                    })
            msg = {"role": "assistant", "content": "\n".join(t for t in texts if t)}
            if enable_thinking:
                rc = "\n".join(t for t in thinks if t)
                if rc:
                    msg["reasoning_content"] = rc
            if calls:
                msg["tool_calls"] = calls
            oai.append(msg)
        # ---- user(tool_result):每个 tool_result 拆成一条 role=tool ----
        else:
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tcid = b.get("tool_use_id")
                cstr, success, miss = _tool_result_to_content(b, trace_dir, image_list)
                missing_any = missing_any or miss
                oai.append({
                    "role": "tool",
                    "name": name_by_id.get(tcid, ""),
                    "tool_call_id": tcid,
                    "content": cstr,
                    "success": success,
                })
    return oai, image_list, missing_any


def build_trajectory(trace_dir, sid, batch, manifest_rec):
    """一个 trace 目录 → 一条 OpenAI 格式轨迹 dict;不合格返回 (None, 原因)。"""
    messages = _read_json(os.path.join(trace_dir, "messages.json"))
    if not messages:
        return None, "无 messages.json"
    ok, why = check_structure(messages)
    if not ok:
        return None, why
    cfg = _read_json(os.path.join(trace_dir, "config.json"), {}) or {}
    tools = convert_tools(_read_json(os.path.join(trace_dir, "tools.json"), []) or [])
    spath = os.path.join(trace_dir, "system_prompt.md")
    system = open(spath, encoding="utf-8").read() if os.path.exists(spath) else ""

    role = cfg.get("role", "orchestrator")
    label = cfg.get("label", os.path.basename(trace_dir))
    enable_thinking = bool(cfg.get("thinking"))

    oai_msgs, image_list, missing = convert_messages(
        messages, system, enable_thinking, trace_dir)
    if missing:
        return None, "引用截图缺失(快照不完整),丢弃整条"

    # exit_reason:编排器取 manifest.orch_exit,subagent 从 workers 里按 label 匹配
    exit_reason = manifest_rec.get("orch_exit")
    if role == "subagent":
        for w in manifest_rec.get("workers", []):
            if w.get("label") == label:
                exit_reason = w.get("exit_reason")
                break

    total_steps = sum(1 for m in oai_msgs if m.get("role") == "assistant")
    rec = {
        "status": "completed",
        "total_steps": total_steps,
        "enable_thinking": enable_thinking,
        "messages": oai_msgs,
        "tools": tools,
        "metadata": {
            "sample_id": sid,
            "batch": batch,
            "role": role,
            "label": label,
            "model": cfg.get("model"),
            "exit_reason": exit_reason,
            "task": cfg.get("task"),
        },
    }
    if image_list:
        rec["image"] = image_list
    return rec, "ok"


# ----------------------------- 主流程 -----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--out", default="data_openai", help="输出根目录(默认 data_openai/)")
    ap.add_argument("--include-subagents", default="true")
    args = ap.parse_args()
    include_sub = str(args.include_subagents).lower() not in ("false", "0", "no")

    manifest = load_manifest(args.batch)
    completed = {sid: r for sid, r in manifest.items() if r.get("status") == "completed"}

    out_dir = os.path.join(ROOT, args.out, args.batch)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "data.jsonl")

    n_traj = n_skip = n_img = dropped_decks = 0
    skips = []
    with open(out_path, "w", encoding="utf-8") as out:
        for sid, rec in completed.items():
            run_dir = rec.get("run_dir") or os.path.join(RUNS, args.batch, sid)
            hit = deck_cliche_hit(run_dir)
            if hit:
                dropped_decks += 1
                skips.append(f"{sid}: 收尾套话 {hit},丢整 deck")
                continue
            trace_root = os.path.join(run_dir, "_trace")
            trace_dirs = [os.path.join(trace_root, "orchestrator")]
            if include_sub:
                trace_dirs += dedup_latest(sorted(glob.glob(os.path.join(trace_root, "subagents", "*"))))
            for td in trace_dirs:
                if not os.path.isdir(td):
                    continue
                traj, why = build_trajectory(td, sid, args.batch, rec)
                if traj is None:
                    n_skip += 1
                    skips.append(f"{sid}/{os.path.basename(td)}: {why}")
                    continue
                out.write(json.dumps(traj, ensure_ascii=False) + "\n")
                n_traj += 1
                n_img += len(traj.get("image", []))

    print(f"batch={args.batch}  completed_decks={len(completed)}")
    print(f"轨迹={n_traj}(编排器+subagent)  跳过={n_skip}  收尾套话丢整deck={dropped_decks}  图片引用={n_img}")
    print(f"数据集: {out_path}")
    print(f"image 字段为原图绝对路径(不拷贝,直接引用 runs/ 下截图)")
    if skips:
        print("丢弃明细(前 20):")
        for s in skips[:20]:
            print(f"  - {s}")


if __name__ == "__main__":
    main()
