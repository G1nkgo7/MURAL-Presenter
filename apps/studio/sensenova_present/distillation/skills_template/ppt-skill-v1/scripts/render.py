#!/usr/bin/env python3
"""HTML -> PNG 渲染脚本(ppt-skill 自带,可移植)。

任何有 shell / 代码执行能力的脚手架都能直接跑它来渲染一页幻灯片,**不依赖宿主提供 render 工具**:

    python render.py <html路径> <输出png路径> [宽=1600] [高=900]

成功时把输出 png 的绝对路径打到 stdout;失败打到 stderr 并以非 0 退出。

自带依赖处理(尽量自包含):
- 用 playwright 的 chromium 渲染(需先 `playwright install chromium`)。
- headless chromium 缺的系统 .so 自动从 `~/pwdeps/lib`(再退 `~/cdeps/lib`)补进 LD_LIBRARY_PATH。
- 中文字体需先装到 `~/.fonts`(Noto Sans SC / Noto Serif SC)并 `fc-cache`,否则中文渲染成豆腐块。
渲染时等 `document.fonts.ready` 再截图(防豆腐块 / 截到半截);device_scale_factor=2 出高清图。
"""
import os
import sys
import time

# chromium 稳定性参数:--disable-dev-shm-usage 让它用 /tmp 而非小的 /dev/shm(高并发关键),
# 关 GPU / 软渲染省内存,降低高并发下崩溃(TargetClosed)。
LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
               "--disable-software-rasterizer", "--disable-extensions"]


def _setup_libs():
    """把本地依赖库目录加进 LD_LIBRARY_PATH —— 在 chromium 子进程启动前设置即可生效。"""
    extra = [d for d in (os.path.expanduser("~/pwdeps/lib"), os.path.expanduser("~/cdeps/lib"))
             if os.path.isdir(d)]
    if extra:
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(
            extra + [os.environ.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)


def _render_once(p, html, out, w, h):
    """起一个全新 chromium 渲染一次(崩了由外层重试整体重来)。"""
    b = p.chromium.launch(args=LAUNCH_ARGS)
    try:
        pg = b.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
        try:
            try:
                pg.goto("file://" + html, wait_until="networkidle", timeout=30000)
            except Exception:
                pass
            try:
                pg.evaluate("document.fonts && document.fonts.ready")
            except Exception:
                pass
            pg.wait_for_timeout(900)   # 给 ECharts / 渐变 / 布局定帧
            pg.screenshot(path=out)
        finally:
            pg.close()
    finally:
        b.close()


def main():
    if len(sys.argv) < 3:
        print("用法: render.py <html> <out.png> [宽] [高]", file=sys.stderr)
        sys.exit(2)
    html = os.path.abspath(sys.argv[1])
    out = os.path.abspath(sys.argv[2])
    w = int(sys.argv[3]) if len(sys.argv) > 3 else 1600
    h = int(sys.argv[4]) if len(sys.argv) > 4 else 900
    if not os.path.exists(html):
        print(f"渲染失败:找不到 HTML {html}", file=sys.stderr)
        sys.exit(1)

    _setup_libs()
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    from playwright.sync_api import sync_playwright
    last_err = None
    for attempt in range(3):                 # 高并发下 chromium 偶发崩(TargetClosed),重试 + 退避
        try:
            with sync_playwright() as p:
                _render_once(p, html, out, w, h)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                print(out)
                return
        except Exception as e:
            last_err = e
        time.sleep(1.5 * (attempt + 1))      # 退避,顺带错峰,缓解 sibling 同时起 chromium
    print(f"渲染失败(重试 3 次): {last_err}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
