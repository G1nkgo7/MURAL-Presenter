#!/usr/bin/env python3
"""salvage_probe.py —— 试验:把 reject 的轨迹"从断点续跑"救回来(不重头跑)。

机制:重建编排器 Agent,用保存的 _trace/orchestrator/messages.json 作为起始 messages,
继续 ReAct 循环直到自然收尾(text_response),再做结构化 _accept。

只处理"编排器被打断"型 reject(api_failed / stopped_no_text):这类是基建打断、不是质量问题,
续跑产出的轨迹是连贯的。子agent失败型(质量)不在此脚本,另议。

在**本地快盘**的副本上跑(不动 -failed 归档、且避开 FUSE 慢 I/O)。
用法: uv run python distill_monitor/salvage_probe.py <sid_or_tail> [<sid2> ...]
"""
import os, sys, json, time, shutil, glob, re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import distill_ppt as D
D.load_dotenv()
from core import tools
from core.agent import Agent, _model_call, _run_tools, blocks_to_dicts

BATCH = "synth_query_260617_skillsv1_1"
WORK = "/tmp/shared-storage/ppt_salvage"            # 本地快盘
MANIFEST = os.path.join(ROOT, "log", f"{BATCH}.manifest.jsonl")
SLIDE_RE = re.compile(r"^slide_\d+\.html$")


def find_failed_dir(sid_tail):
    """按 sid 尾段在 manifest 找 rejected 记录 -> -failed 目录。"""
    rec = None
    with open(MANIFEST) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("status") == "rejected" and r.get("sample_id", "").endswith(sid_tail):
                rec = r
    if not rec:
        return None, None, None
    rd = rec["run_dir"].replace(f"/{BATCH}/", f"/{BATCH}-failed/")
    return rec["sample_id"], rd, rec


def rendered_pages(ws):
    sl = [p for p in glob.glob(os.path.join(ws, "slides", "slide_*.html")) if SLIDE_RE.match(os.path.basename(p))]
    ok = 0
    for s in sl:
        png = os.path.join(ws, "renders", os.path.splitext(os.path.basename(s))[0] + ".png")
        if os.path.exists(png):
            ok += 1
    return len(sl), ok


def strip_thinking(messages):
    """API 重放历史时去掉 thinking/redacted_thinking 块(避免重放思考块报错);保留 text+tool_use+tool_result。"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            c = [b for b in c if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))]
        out.append({"role": m["role"], "content": c})
    return out


def continue_orchestrator(sid, failed_dir, rec):
    cfgp = os.path.join(failed_dir, "_trace", "orchestrator", "config.json")
    msgp = os.path.join(failed_dir, "_trace", "orchestrator", "messages.json")
    if not os.path.exists(msgp):
        return {"sid": sid, "ok": False, "why": "无 messages.json"}
    cfg = json.load(open(cfgp)) if os.path.exists(cfgp) else {}
    brief = cfg.get("task", "")
    saved = json.load(open(msgp))

    # 本地副本(不动归档)
    work = os.path.join(WORK, os.path.basename(failed_dir))
    if os.path.exists(work):
        shutil.rmtree(work)
    os.makedirs(os.path.dirname(work), exist_ok=True)
    t0 = time.time()
    shutil.copytree(failed_dir, work, symlinks=True, ignore=shutil.ignore_patterns("skills"))
    D._link_skills(work)
    copy_s = time.time() - t0

    pre_html, pre_png = rendered_pages(work)

    agent = Agent(role="orchestrator", sid=sid, ws=work, sub_dir="orchestrator",
                  tools_schema=tools.resolve_toolsets(D.ORCHESTRATOR_TOOLSETS), config={},
                  initial_user=brief, label="orch", system=D.BASE_SYSTEM,
                  skills_root=D.SKILLS_DIR, forbid_write_prefixes=["slides"])

    # 起始 messages = 保存的历史(去 thinking)。若末条是 assistant(stopped_no_text 的空收尾),弹掉让模型重试该回合。
    messages = strip_thinking(saved)
    while messages and messages[-1]["role"] == "assistant":
        messages.pop()
    if not messages:
        return {"sid": sid, "ok": False, "why": "历史为空/无法续"}
    seed_n = len(messages)

    # ---- 续跑循环(= run_loop 主体,但从 seeded messages 起) ----
    tool_log = []
    agent.trace.snapshot_inputs(agent.system, agent.tools, agent.config_snapshot())
    t1 = time.time()
    for turn in range(agent.max_turns):
        resp = _model_call(agent, messages)
        if resp is None:
            agent.exit_reason = "api_failed"
            break
        messages.append({"role": "assistant", "content": blocks_to_dicts(resp.content)})
        turn_text = ""
        for b in resp.content:
            if b.type == "text" and b.text.strip():
                turn_text = b.text.strip(); agent.final_text = turn_text
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            agent.exit_reason = "text_response" if turn_text else "stopped_no_text"
            break
        messages.append({"role": "user", "content": _run_tools(agent, tool_uses, turn, tool_log)})
    else:
        agent.exit_reason = "max_turns"
    cont_s = time.time() - t1
    agent.trace.write(messages, tool_log)

    ok, reason = D._accept(agent)
    post_html, post_png = rendered_pages(work)
    return {"sid": sid, "ok": ok, "why": reason, "exit": agent.exit_reason,
            "seed_msgs": seed_n, "cont_turns": len(tool_log),
            "pages_before": f"{pre_html}html/{pre_png}png", "pages_after": f"{post_html}html/{post_png}png",
            "orig_reason": str(rec.get("reason"))[:40], "copy_s": round(copy_s, 1),
            "cont_s": round(cont_s, 1), "work": work}


def main():
    tails = sys.argv[1:]
    if not tails:
        print("用法: salvage_probe.py <sid_tail> [...]"); return
    for tail in tails:
        sid, fd, rec = find_failed_dir(tail)
        if not sid:
            print(f"[{tail}] 找不到 rejected 记录"); continue
        if not os.path.isdir(fd):
            print(f"[{tail}] -failed 目录不在: {fd}"); continue
        print(f"\n===== 救 {sid[-12:]}  (原因: {str(rec.get('reason'))[:46]}) =====", flush=True)
        try:
            r = continue_orchestrator(sid, fd, rec)
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"  ✗ 异常: {e}"); continue
        verdict = "✅ 救回(可 accept)" if r.get("ok") else f"❌ 仍拒: {r.get('why')}"
        print(f"  {verdict}")
        print(f"  exit={r.get('exit')}  seed_msgs={r.get('seed_msgs')} 续跑turns={r.get('cont_turns')}  "
              f"页 {r.get('pages_before')} -> {r.get('pages_after')}  (copy {r.get('copy_s')}s, 续跑 {r.get('cont_s')}s)")


if __name__ == "__main__":
    main()
