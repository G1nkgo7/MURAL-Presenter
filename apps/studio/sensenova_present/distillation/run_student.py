#!/usr/bin/env python3
"""单条 query 跑 **部署的 student 模型**(MODEL_BACKEND=openai),走完整 ppt-skill 流程,
产出一套 HTML + 渲染图,方便人工看效果。

  uv run python run_student.py --query "12页 xxx 主题的PPT" --lang zh --slides 12
  STUDENT_BASE_URL=... STUDENT_MODEL=... uv run python run_student.py --query "..."

说明:LLM 走 student 端点;配图(gpt-image-2)/检索(serper)仍用 .env 里的工具端点。
产物在 runs/student_test/<id>/(slides/ renders/ plan/ + _trace/)。
"""
import argparse, json, os, shutil, sys, time

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_dotenv():
    p = os.path.join(ROOT, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", required=True, help="PPT 需求(brief)")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--slides", type=int, default=None, help="目标页数(可选)")
    ap.add_argument(
        "--base",
        default=os.environ.get("STUDENT_BASE_URL", ""),
        help="OpenAI-compatible student endpoint (or set STUDENT_BASE_URL)",
    )
    ap.add_argument("--model", default="pptagent_v1.0_preview")
    ap.add_argument("--backend", default="openai", choices=["openai", "anthropic"],
                    help="openai=本地部署 student(vLLM);anthropic=teacher(Opus,走 .env 的 ANTHROPIC_*)")
    ap.add_argument("--id", default=None, help="run id(默认按时间戳)")
    ap.add_argument("--max-turns", type=int, default=120,
                    help="与训练时 MAX_TURNS=120 对齐;调低会让大 deck 的编排器复审被硬切断")
    args = ap.parse_args()

    load_dotenv()
    args.base = args.base or os.environ.get("STUDENT_BASE_URL", "")
    if args.backend == "openai":
        if not args.base:
            ap.error("--base or STUDENT_BASE_URL is required for the openai backend")
        # 切到 student 后端(Agent.__init__ 据此选 OpenAIShim)
        os.environ["MODEL_BACKEND"] = "openai"
        os.environ["STUDENT_BASE_URL"] = args.base
        os.environ["STUDENT_MODEL"] = args.model
    else:
        # teacher 路径:不设 MODEL_BACKEND,Agent 走 anthropic.Anthropic;
        # model/ANTHROPIC_*/THINKING 由 .env 提供(与训练蒸馏口径一致)
        os.environ.pop("MODEL_BACKEND", None)

    from agent_loop import run_sample

    sid = args.id or f"stu_{int(time.time())}"
    run_dir = os.path.join(ROOT, "runs", "student_test", sid)
    if os.path.exists(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(run_dir, exist_ok=False)

    seed = {"query": args.query, "lang": args.lang, "source": "student_test"}
    if args.slides:
        seed["slide_count"] = args.slides

    config = {
        "batch": "student_test",
        "dry_run": False,
        "model": args.model,
        "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://tokenhub.sensetime.com/v1"),
        "image_model": os.environ.get("IMAGE_MODEL", "gpt-image-2"),
        "skill_dir": os.path.join(ROOT, "skills", "ppt-skill"),
        "max_turns": args.max_turns,
        "max_tokens": int(os.environ.get("MAX_TOKENS", "32000")),
    }

    print(f"▶ student={args.model} @ {args.base}")
    print(f"▶ query: {args.query}")
    print(f"▶ run_dir: {run_dir}\n")
    t0 = time.time()
    try:
        res = run_sample(sid, seed, run_dir, config)
    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"\n❌ 运行异常: {type(e).__name__}: {e}")
        sys.exit(1)

    dur = time.time() - t0
    slides = sorted(__import__("glob").glob(os.path.join(run_dir, "slides", "slide_*.html")))
    renders = sorted(__import__("glob").glob(os.path.join(run_dir, "renders", "slide_*.png")))
    print("\n" + "=" * 50)
    print(f"状态: {res.get('status') if isinstance(res, dict) else res}  | 用时 {dur:.0f}s")
    print(f"原因: {res.get('reason') if isinstance(res, dict) else '-'}")
    print(f"HTML 页: {len(slides)}  | 渲染图: {len(renders)}")
    print(f"产物目录: {run_dir}")
    print(f"  slides/  renders/  plan/  _trace/")
    if renders:
        print(f"看图: {renders[0]} ...")


if __name__ == "__main__":
    main()
