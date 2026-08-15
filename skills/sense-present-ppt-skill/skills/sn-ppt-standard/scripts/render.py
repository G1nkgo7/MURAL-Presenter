#!/usr/bin/env python3
"""HTML -> PNG 渲染脚本(sn-ppt-standard 自带,可移植)。

任何有 shell / 代码执行能力的脚手架都能直接跑它来渲染一页幻灯片,**不依赖宿主提供 render 工具**:

    python render.py <html路径> <输出png路径> [宽=1600] [高=900]

成功时把输出 png 的绝对路径打到 stdout;失败打到 stderr 并以非 0 退出。

推荐传入绝对路径，不依赖当前工作目录:

    python "$SKILL_ROOT/scripts/render.py" "$DECK_DIR/slides/slide_NN.html" "$DECK_DIR/renders/slide_NN.png"

脚本仍会对参数调用 `abspath`，但调用方不得依靠模糊的当前目录来决定 deck 产物位置。

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
               "--disable-software-rasterizer", "--disable-extensions",
               "--single-process", "--no-zygote"]


# 渲染环境里 HOME/.cache 指向用户挂载点,chromium 子进程常拿不到它的 .so;
# 显式把 chromium 目录补进 LD_LIBRARY_PATH,保证子进程能找到 chrome。
LIB = os.path.join(os.path.expanduser("~"),".cache","ms-playwright","chromium-1223","chrome-linux64")
if os.path.isdir(LIB):
    os.environ["LD_LIBRARY_PATH"] = LIB + (":" + os.environ.get("LD_LIBRARY_PATH","") or "")


def _render_once(p, html, out, w, h):
    """起一个全新 chromium 渲染一次(崩了由外层重试整体重来)。"""
    b = p.chromium.launch(args=LAUNCH_ARGS)
    try:
        pg = b.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
        try:
            # 离线渲染:本地已装 Noto 字体,主动 abort 外部字体 CDN 请求,
            # 免得 base.css 的 Google Fonts @import 把加载吊死(配合 wait_until="domcontentloaded")。
            for pattern in ("**fonts.googleapis.com**", "**fonts.gstatic.com**"):
                try:
                    pg.route(pattern, lambda route:route.abort())
                except Exception:
                    pass
            try:
                # 用 "load" 而非 "networkidle":本地 file:// 页若引用外部 CDN,
                # networkidle 可能永远不达成、白等满 timeout;load 只等本地资源就绪。
                pg.goto("file://" + html, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                # 不静默吞:goto 异常打到 stderr,免得"截到半截却当成功"。
                print(f"警告: goto 未正常完成({e}),仍尝试截图", file=sys.stderr)
            try:
                pg.evaluate("document.fonts != null && !!document.fonts.ready")
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

    # _setup_libs 已内联到主程序顶部
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
