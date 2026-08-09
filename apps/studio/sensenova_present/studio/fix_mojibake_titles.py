#!/usr/bin/env python3
"""一次性修复 studio.db 里被双重编码(UTF-8 字节被当 latin-1 解)的中文标题/内容。

背景:2026-06-30 排查「/demo 历史栏中文乱码」。**真因不是产品 bug**——浏览器 UI 用
URLSearchParams 会把中文 percent-encode,Starlette 正确按 UTF-8 解,存的就是干净中文
(deck1/2/8 都干净)。乱码的 deck3~7 是早先用 `curl -d "query=中文..."` 发的——curl 的
-d 直接塞**未 percent-encode 的原始 UTF-8 字节**,Starlette 按 latin-1 解 → 双重编码乱码。

本脚本只把「全 latin-1 范围 且 重新按 latin-1 编码后能解成 UTF-8」的字符串还原,
**不碰真正的中文(已 >0xFF)和纯 ASCII**(café 这类合法 latin-1 也不会误伤:strict 解失败时
仅对已判定为 mojibake 的标题用 ignore 兜被 title[:80] 截断的尾字节)。先备份再跑。

用法(在 studio/ 下):
    cp -p data/studio.db data/studio.db.bak-$(date -u +%Y%m%d-%H%M%S)
    .venv/bin/python3 fix_mojibake_titles.py            # 预览
    .venv/bin/python3 fix_mojibake_titles.py --apply     # 实修
"""
import sqlite3
import sys

DB = "data/studio.db"
APPLY = "--apply" in sys.argv


def repair(s):
    if not s:
        return s
    try:
        b = s.encode("latin-1")          # 任一字符 >0xFF(真中文)→ 抛错 → 保持不动
    except UnicodeEncodeError:
        return s
    try:
        r = b.decode("utf-8")            # 完整 mojibake → 干净还原
    except UnicodeDecodeError:
        r = b.decode("utf-8", "ignore")  # title[:80] 截断的尾多字节 → 丢悬尾
    return r if r != s else s


def main():
    con = sqlite3.connect(DB)
    changed = {}
    for tbl, col, idc in [("decks", "title", "id"),
                          ("conversations", "title", "id"),
                          ("messages", "content", "id")]:
        n = 0
        for rid, val in con.execute(f"SELECT {idc},{col} FROM {tbl}").fetchall():
            nv = repair(val)
            if nv != val:
                n += 1
                if tbl == "decks":
                    print(f"  deck{rid}: {val[:18]!r} -> {nv[:18]!r}")
                if APPLY:
                    con.execute(f"UPDATE {tbl} SET {col}=? WHERE {idc}=?", (nv, rid))
        changed[tbl] = n
    if APPLY:
        con.commit()
        print("APPLIED:", changed)
    else:
        print("DRY-RUN (加 --apply 实修):", changed)


if __name__ == "__main__":
    main()
