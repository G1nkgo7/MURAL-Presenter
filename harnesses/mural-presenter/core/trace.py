#!/usr/bin/env python3
"""轨迹落盘 + 图像快照(从 Agent 拆出来,循环只调它,Agent 保持瘦)。

每个 agent(编排器 / 子 agent)各写各的原始轨迹到 `_trace/<sub_dir>/` 下,每个
(上下文 -> 输出) 对都忠实保留:
    system_prompt.md  tools.json  config.json   —— 输入快照
    messages.json     —— 清洗后的对话(图像块换成轻量 {type:image, shot:<路径>})
    summary.md        —— 最后一条 assistant 文字答复,供上层/模型稳定读取
    tool_log.json     —— 每次工具调用的 {turn,name,args}
    images/view_NN.png —— 看图**当下**的像素快照(因为 render 会覆盖 renders/,只存路径会丢真)
"""
import json
import hashlib
import os


_NO_SUMMARY = "(no assistant text response found in messages.json)\n"


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, default=str)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def _content_text(content):
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, str) and block.strip():
            parts.append(block.strip())
        elif isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            if text:
                parts.append(text)
    return "\n\n".join(parts).strip()


def latest_assistant_text(messages):
    """从清洗后的 messages 中取最后一条 assistant 正文,不把 thinking/tool_use 当总结。"""
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        text = _content_text(msg.get("content"))
        if text:
            return text
    return ""


class Trace:
    """一个 agent 的轨迹记录器。持有 sub_dir 与图像快照状态。"""

    def __init__(self, sub_dir):
        self.sub_dir = sub_dir
        os.makedirs(self.sub_dir, exist_ok=True)
        os.makedirs(os.path.join(self.sub_dir, "images"), exist_ok=True)
        self.shot_by_tcid = {}      # tool_call_id -> 该轨迹内 images/ 下的快照相对路径
        self.view_n = 0             # 看过几张图(给快照按查看顺序编号 view_NN.png)

    def snapshot_inputs(self, system, tools, config):
        with open(os.path.join(self.sub_dir, "system_prompt.md"), "w", encoding="utf-8") as f:
            f.write(system)
        _write_json(os.path.join(self.sub_dir, "tools.json"), tools)
        _write_json(os.path.join(self.sub_dir, "config.json"), config)

    def snapshot_image(self, tcid, raw_bytes):
        """把看到的一张图的字节快照到 images/view_NN.png,记账 tcid -> 相对路径,返回该路径。"""
        self.view_n += 1
        rel = os.path.join("images", f"view_{self.view_n:02d}.png")
        with open(os.path.join(self.sub_dir, rel), "wb") as f:
            f.write(raw_bytes)
        self.shot_by_tcid[tcid] = rel
        return rel

    def clean(self, m):
        """把 messages 里的 tool_result 图像块换成轻量 {type:image, shot:<快照路径>},
        避免把几 MB 的 base64 灌进 messages.json(真正的像素已快照到 images/)。"""
        if not isinstance(m.get("content"), list):
            return m
        c = []
        for x in m["content"]:
            if isinstance(x, dict) and x.get("type") == "tool_result" and isinstance(x.get("content"), list):
                shot = self.shot_by_tcid.get(x.get("tool_use_id"))
                newc = [({"type": "image", "shot": shot}
                         if isinstance(y, dict) and y.get("type") == "image" else y)
                        for y in x["content"]]
                c.append({**x, "content": newc})
            else:
                c.append(x)
        return {**m, "content": c}

    def _multimodal_manifest(self, messages):
        """Hash every reconstructable image snapshot referenced by this trace."""
        shots = {}
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                result = block.get("content")
                if not isinstance(result, list):
                    continue
                for item in result:
                    if not (isinstance(item, dict) and item.get("type") == "image"):
                        continue
                    shot = str(item.get("shot") or "")
                    if shot:
                        shots.setdefault(shot, str(block.get("tool_use_id") or ""))
        for tool_use_id, shot in self.shot_by_tcid.items():
            shots.setdefault(shot, str(tool_use_id or ""))

        images, missing = [], []
        for shot, tool_use_id in sorted(shots.items()):
            path = os.path.join(self.sub_dir, shot)
            if not os.path.isfile(path):
                missing.append(shot)
                continue
            with open(path, "rb") as stream:
                raw = stream.read()
            images.append({
                "shot": shot.replace(os.sep, "/"),
                "tool_use_id": tool_use_id,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
                "media_type": "image/png",
            })
        return {
            "schema": "mural.multimodal-trace.v1",
            "complete": not missing,
            "image_count": len(images),
            "images": images,
            "missing": missing,
            "replay_contract": (
                "messages.json plus the hash-addressed images reconstruct the exact "
                "model-visible multimodal turns"
            ),
        }

    def write(self, messages, tool_log, run_mode="inference"):
        cleaned = [self.clean(m) for m in messages]
        _write_json(os.path.join(self.sub_dir, "messages.json"), cleaned)
        _write_json(os.path.join(self.sub_dir, "tool_log.json"), tool_log)
        _write_text(os.path.join(self.sub_dir, "summary.md"), latest_assistant_text(cleaned) or _NO_SUMMARY)
        status = {"mode": run_mode, "complete": True, "image_count": 0}
        if run_mode == "synthesis":
            manifest = self._multimodal_manifest(cleaned)
            _write_json(
                os.path.join(self.sub_dir, "multimodal-manifest.json"), manifest
            )
            status.update({
                "complete": bool(manifest["complete"]),
                "image_count": int(manifest["image_count"]),
                "missing": list(manifest["missing"]),
            })
        return status
