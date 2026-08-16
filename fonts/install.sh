#!/usr/bin/env bash
# 把本字体库装到 ~/.fonts 并刷新 fontconfig 缓存,渲染用的 headless chrome 经此找到这些展示/手写字体。
# 幂等:可反复跑;不覆盖已存在的同名文件。
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$HOME/.fonts"
n=0
for f in "$HERE"/*.ttf "$HERE"/*.otf; do
  [ -s "$f" ] || continue
  cp -n "$f" "$HOME/.fonts/" 2>/dev/null && n=$((n+1)) || true
done
fc-cache -f >/dev/null 2>&1 || true
echo "已安装/确认 $n 个字体到 ~/.fonts 并刷新 fontconfig 缓存。"
echo "验证: fc-list | grep -iE '软萌|加油|楷书|钢笔'"
