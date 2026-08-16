#!/usr/bin/env python3
"""Extract final slide HTML to Markdown and a retrieval page index with DeepSeek."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from pathlib import Path

from judge.final_html_content_extraction import (
    canonical_html_content_output_dir,
    extract_final_html_content_with_llm,
)
from judge.judge_transport_and_artifact_runtime import (
    RunnerError,
    assess_multi_generation_eligibility,
    discover_slide_artifacts,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "deepseek-v4-pro"


def resolve(value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()


def load_selected_dotenv(path: Path, names: set[str]) -> None:
    """Load only the literal DeepSeek settings needed by this standalone tool."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in names or os.environ.get(key):
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        os.environ[key] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-dir",
        required=True,
        help=(
            "Generation run directory, absolute or relative to the current working "
            "directory."
        ),
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument(
        "--config-file", default=str(ROOT / "configs/judge.env")
    )
    parser.add_argument("--output-dir")
    parser.add_argument("--max-concurrent", type=int, default=4)
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if (
        args.max_concurrent < 1
        or args.max_tokens < 1
        or args.timeout < 1
        or args.retries < 0
    ):
        raise RunnerError(
            "max-concurrent/max-tokens/timeout must be positive and retries non-negative"
        )
    load_selected_dotenv(
        resolve(args.env_file),
        {"DEEPSEEK_API_KEY"},
    )
    load_selected_dotenv(
        resolve(args.config_file),
        {"DEEPSEEK_BASE_URL"},
    )
    base_url = args.base_url or os.environ.get("DEEPSEEK_BASE_URL", "")
    api_key = args.api_key or os.environ.get("DEEPSEEK_API_KEY", "")
    if not base_url or not api_key:
        raise RunnerError(
            "DEEPSEEK_BASE_URL and DEEPSEEK_API_KEY are required; "
            "configure them in configs/judge.env and .env, respectively, "
            "or pass explicit arguments"
        )

    run_dir = resolve(args.run_dir)
    if not run_dir.is_dir():
        raise RunnerError(f"generation run directory not found: {run_dir}")
    slides = discover_slide_artifacts(run_dir)
    eligibility = assess_multi_generation_eligibility(run_dir, slides)
    if eligibility.get("eligible") is not True:
        raise RunnerError(
            f"multi-page artifacts are not eligible for HTML extraction: {eligibility}"
        )
    output_dir = (
        resolve(args.output_dir)
        if args.output_dir
        else canonical_html_content_output_dir(run_dir)
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / ".extract.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        content, _metadata, reused = extract_final_html_content_with_llm(
            slides,
            model=args.model,
            base_url=base_url,
            api_key=api_key,
            output_path=output_dir / "content.md",
            max_concurrent=args.max_concurrent,
            max_tokens=args.max_tokens,
            timeout=args.timeout,
            retries=args.retries,
            resume=not args.overwrite,
        )
    status = "reused" if reused else "extracted"
    print(
        f"{status}: {content['page_count']} pages -> "
        f"{output_dir / 'content.md'}, {output_dir / 'page_index.json'}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunnerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
