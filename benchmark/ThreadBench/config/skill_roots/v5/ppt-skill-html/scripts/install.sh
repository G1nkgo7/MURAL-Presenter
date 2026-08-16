#!/usr/bin/env bash
# ============================================================================
# ppt-skill-html · 环境安装脚本(让 skill 自包含:一键装齐运行所需的全部依赖)
#
# 装什么(四块,缺一不可):
#   1) normalize venv  —— 附件解析(markitdown/pdfminer/openpyxl/…) parse_*.py 用
#   2) pymupdf(fitz)   —— 扫描 PDF rasterize 成页图(交 vision 兜底);装进渲染环境
#   3) 中文字体        —— Noto Sans/Serif SC 到 ~/.fonts,否则中文渲染成豆腐块
#   4) Playwright Chromium —— render.py 无头渲染 HTML→PNG
#   (系统 .so 缺失时 render.py 会自动从 ~/pwdeps/lib 补,见 §5 提示)
#
# 用法:
#   bash scripts/install.sh                 # 全装
#   bash scripts/install.sh normalize       # 只装解析 venv
#   bash scripts/install.sh fonts chromium  # 只装字体+浏览器
#
# 产物路径(可用环境变量覆盖):
#   NORMALIZE_VENV   默认 ~/.cache/ppt-skill-html/venv-normalize
#   FONTS_DIR        默认 ~/.fonts
#   PLAYWRIGHT_BROWSERS_PATH 默认 ~/.cache/ms-playwright
# 装完打印一行 `export NORMALIZE_PY=...`,喂给蒸馏 harness(distill_ppt 的 NORMALIZE_PY)。
# ============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

NORMALIZE_VENV="${NORMALIZE_VENV:-$HOME/.cache/ppt-skill-html/venv-normalize}"
FONTS_DIR="${FONTS_DIR:-$HOME/.fonts}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
PYBIN="${PYBIN:-python3}"

log(){ echo "[install] $*"; }

install_normalize(){
  log "1) normalize venv → $NORMALIZE_VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$NORMALIZE_VENV" >/dev/null 2>&1 || true
    uv pip install --python "$NORMALIZE_VENV/bin/python" -r "$HERE/requirements-normalize.txt"
  else
    "$PYBIN" -m venv "$NORMALIZE_VENV"
    "$NORMALIZE_VENV/bin/python" -m pip install -q --upgrade pip
    "$NORMALIZE_VENV/bin/python" -m pip install -q -r "$HERE/requirements-normalize.txt"
  fi
  # 冒烟:import 三个关键库
  "$NORMALIZE_VENV/bin/python" - <<'PY' && log "  normalize venv OK"
import markitdown, pdfminer, openpyxl
print("  imports ok:", markitdown.__name__, pdfminer.__name__, openpyxl.__name__)
PY
}

install_pymupdf(){
  log "2) pymupdf(fitz) → 当前渲染解释器 ($PYBIN)"
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$PYBIN" pymupdf >/dev/null 2>&1 || "$PYBIN" -m pip install -q pymupdf
  else
    "$PYBIN" -m pip install -q pymupdf
  fi
  "$PYBIN" -c "import fitz; print('  fitz ok', fitz.__doc__[:30])" 2>/dev/null && log "  pymupdf OK" || log "  ⚠️ pymupdf 装失败(扫描PDF rasterize 兜底不可用)"
}

install_fonts(){
  log "3) 中文字体 → $FONTS_DIR"
  mkdir -p "$FONTS_DIR"
  # 优先复用宿主已有 Noto SC；额外共享目录可通过 MURAL_SHARED_FONT_DIR 指定。
  local found=0
  local sources=("$HOME/.fonts" "/usr/share/fonts")
  [ -n "${MURAL_SHARED_FONT_DIR:-}" ] && sources+=("$MURAL_SHARED_FONT_DIR")
  for src in "${sources[@]}"; do
    for f in "$src"/*Noto*SC* "$src"/**/*Noto*SC*; do
      [ -f "$f" ] && { cp -n "$f" "$FONTS_DIR/" 2>/dev/null && found=1; }
    done
  done
  if command -v fc-cache >/dev/null 2>&1; then fc-cache -f "$FONTS_DIR" >/dev/null 2>&1; fi
  if fc-list 2>/dev/null | grep -qi "Noto.*SC"; then
    log "  中文字体 OK (Noto SC 已就位)"
  else
    log "  ⚠️ 未发现 Noto Sans/Serif SC。请手动放置到 $FONTS_DIR 后 fc-cache -f(离线节点无法自动下载)。"
  fi
}

install_chromium(){
  log "4) Playwright Chromium → $PLAYWRIGHT_BROWSERS_PATH"
  local PW="$NORMALIZE_VENV/bin/python"
  [ -x "$PW" ] || PW="$PYBIN"
  "$PYBIN" -m pip install -q playwright 2>/dev/null || true
  "$PYBIN" -m playwright install chromium 2>/dev/null && log "  Chromium OK" \
    || log "  ⚠️ playwright install chromium 失败;若宿主已有 chromium 设 PLAYWRIGHT_BROWSERS_PATH 复用。"
}

TARGETS=("$@"); [ ${#TARGETS[@]} -eq 0 ] && TARGETS=(normalize pymupdf fonts chromium)
for t in "${TARGETS[@]}"; do
  case "$t" in
    normalize) install_normalize ;;
    pymupdf)   install_pymupdf ;;
    fonts)     install_fonts ;;
    chromium)  install_chromium ;;
    *) log "未知目标: $t (可选 normalize|pymupdf|fonts|chromium)";;
  esac
done

echo
log "完成。把下面这行喂给蒸馏 harness(distill_ppt.py 读 NORMALIZE_PY):"
echo "  export NORMALIZE_PY=\"$NORMALIZE_VENV/bin/python\""
echo
log "系统 .so 缺失(headless chromium 报缺库)时:render.py 会自动从 ~/pwdeps/lib 补;"
log "若无 pwdeps,用 micromamba 免 root 装到 ~/pwdeps(见 SKILL.md 渲染依赖段)。"
