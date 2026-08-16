#!/usr/bin/env python3
"""Rewrite DeepResearchBench II ``content.task`` text as a PPT query."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"
DEFAULT_PROMPT_FILE = (
    PROJECT_ROOT / "prompts/curation/deepresearch_to_ppt_query.md"
)
DEFAULT_PROVIDER = "claude"
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MODELS = {
    "claude": "claude-opus-4-7",
    "gemini": "gemini-3.1-pro-preview",
}
MAX_OUTPUT_TOKENS = {
    "claude": 8192,
    "gemini": 8192,
}
PROVIDER_ENV = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL"),
    "gemini": ("GEMINI_API_KEY", "GEMINI_BASE_URL", "GEMINI_MODEL"),
}
CASE_METADATA_FIELDS = (
    "reference",
    "domain",
    "scenario",
    "audience",
    "audience_description",
    "audience_knowledge_level",
    "topic",
    "purpose",
    "style",
    "language",
    "complexity",
)
PROFILE_INPUT_FIELDS = (
    "domain",
    "scenario",
    "audience",
    "audience_description",
    "audience_knowledge_level",
    "purpose",
    "style",
    "language",
)

FORBIDDEN_META = re.compile(
    r"deepresearchbench|deepresearch\s*bench|knowledge\s*checklist|"
    r"case\s*rubric|ppt\s*agent|ppt智能体|智能体效果|内部测试|内部评测|"
    r"benchmark|rubric|checklist",
    re.IGNORECASE,
)
PAGE_RANGE = re.compile(
    r"(?<!\d)(\d{1,3})\s*(?:[-—–~～至到]\s*(\d{1,3})\s*)?页"
)
SECTION_ONE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?第一部分[：:]\s*PPT生成需求"
    r"(?:\*\*)?\s*$",
    re.MULTILINE,
)
SECTION_TWO = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*)?第二部分[：:]\s*执行约束"
    r"(?:\*\*)?\s*$",
    re.MULTILINE,
)
CONSTRAINT_FIELDS = {
    "Research": ("必须",),
}
IMAGE_CONSTRAINTS = {
    "图片搜索": re.compile(
        r"(?:图片|图像|照片|影像|视觉素材).{0,12}(?:检索|搜索|搜集|查找|获取)|"
        r"(?:检索|搜索|搜集|查找|获取|搜图).{0,12}"
        r"(?:真实|实拍|纪实)?(?:图片|图像|照片|影像|视觉素材)",
        re.IGNORECASE,
    ),
    "生成图片": re.compile(
        r"(?:生成式?|AI(?:生成)?|人工智能生成)(?:图片|图像|视觉|素材)|生图",
        re.IGNORECASE,
    ),
}


class RewriteValidationError(RuntimeError):
    """The model returned a draft that violates the rewrite contract."""

    def __init__(self, message: str, draft: str) -> None:
        super().__init__(message)
        self.draft = draft


def load_env(path: Path) -> None:
    """Load simple KEY=VALUE entries without overriding exported variables."""
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
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_system_prompt(path: Path) -> str:
    """Read the dedicated DeepResearch-to-PPT system prompt."""
    if not path.is_file():
        raise RuntimeError(f"Prompt 文件不存在：{path}")
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError(f"Prompt 文件为空：{path}")
    return prompt


def messages_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/v1/messages"):
        return base
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


def chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def extract_anthropic_text(response: dict) -> str:
    parts = [
        block.get("text", "")
        for block in response.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text = "\n".join(part for part in parts if part).strip()
    if not text:
        raise RuntimeError("API 返回成功，但响应中没有文本内容")
    return text


def extract_openai_text(response: dict) -> str:
    try:
        content = response["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("API 返回成功，但响应格式不符合 OpenAI 兼容协议") from exc

    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        text = "\n".join(part for part in parts if part).strip()
    else:
        text = ""
    if not text:
        raise RuntimeError("API 返回成功，但响应中没有文本内容")
    return text


def call_llm(
    *,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    content_task: str,
    presentation_profile: dict[str, str] | None = None,
    rewrite_feedback: str | None = None,
    timeout: int,
) -> tuple[str, str | None]:
    """Issue one request with task content and optional delivery context."""
    profile_block = ""
    if presentation_profile:
        profile_json = json.dumps(
            presentation_profile, ensure_ascii=False, indent=2
        )
        profile_block = (
            f"<presentation_profile>\n{profile_json}\n"
            "</presentation_profile>\n\n"
        )
    feedback_block = ""
    if rewrite_feedback:
        feedback_block = (
            "\n\n<rewrite_feedback>\n"
            f"{rewrite_feedback.strip()}\n"
            "</rewrite_feedback>"
        )
    user_content = (
        f"{profile_block}<content.task>\n{content_task.strip()}\n</content.task>"
        f"{feedback_block}"
    )
    if provider == "claude":
        url = messages_url(base_url)
        body = {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS[provider],
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_content}],
        }
        headers = {
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
    elif provider == "gemini":
        url = chat_completions_url(base_url)
        body = {
            "model": model,
            "max_tokens": MAX_OUTPUT_TOKENS[provider],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
        }
        headers = {
            "content-type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }
    else:
        raise RuntimeError(f"不支持的 provider：{provider}")

    request = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"API 请求失败：HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"无法连接 LLM API：{exc.reason}") from exc

    try:
        response_data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError("API 返回的不是有效 JSON") from exc

    if provider == "claude":
        return extract_anthropic_text(response_data), response_data.get("stop_reason")
    try:
        finish_reason = response_data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        finish_reason = None
    return extract_openai_text(response_data), finish_reason


def _ordered_unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip()
        if normalized and normalized.casefold() not in seen:
            seen.add(normalized.casefold())
            result.append(normalized)
    return result


def protected_tokens(content_task: str) -> list[str]:
    """Extract deterministic task details that a rewrite must not silently drop."""
    tokens: list[str] = []
    tokens.extend(re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", content_task))
    tokens.extend(re.findall(r"【([^】]{1,100})】", content_task))
    tokens.extend(
        re.findall(
            r"(?<![A-Za-z0-9])(?:[A-Z]{2,}[A-Za-z0-9]*"
            r"(?:[-/][A-Za-z0-9]+)*|[A-Z][A-Za-z0-9]*[A-Z][A-Za-z0-9]*)"
            r"(?![A-Za-z0-9])",
            content_task,
        )
    )
    tokens.extend(
        match.group(0).replace(" ", "")
        for match in re.finditer(
            r"(?<!\d)\d+(?:\.\d+)?\s*(?:%|％|°C|℃|°F|nm|mm|cm|km|"
            r"平方米|万美元|美元|亿元|万元|万人|页)(?![A-Za-z0-9])",
            content_task,
            re.IGNORECASE,
        )
    )
    return _ordered_unique(tokens)


def missing_protected_tokens(content_task: str, rewritten: str) -> list[str]:
    compact_rewritten = re.sub(r"\s+", "", rewritten).casefold()
    return [
        token
        for token in protected_tokens(content_task)
        if re.sub(r"\s+", "", token).casefold() not in compact_rewritten
    ]


def _constraint_status(section: str, field: str) -> str | None:
    match = re.search(
        rf"^\s*[-*]?\s*(?:\*\*)?{re.escape(field)}\s*[：:]\s*"
        r"(必须|可选|不需要)(?:\*\*)?(?:[。；;，,\s]|$)",
        section,
        re.MULTILINE,
    )
    return match.group(1) if match else None


def _semantic_constraint_status(section: str, pattern: re.Pattern[str]) -> str | None:
    for line in section.splitlines():
        if pattern.search(line):
            status = re.search(r"必须|可选|不需要", line)
            if status:
                return status.group(0)
    return None


def split_sections(text: str) -> tuple[str, str] | None:
    first_matches = list(SECTION_ONE.finditer(text))
    second_matches = list(SECTION_TWO.finditer(text))
    if len(first_matches) != 1 or len(second_matches) != 1:
        return None
    first = first_matches[0]
    second = second_matches[0]
    if first.start() > second.start():
        return None
    first_body = text[first.end() : second.start()].strip()
    second_body = text[second.end() :].strip()
    return first_body, second_body


def validate_rewrite(content_task: str, text: str) -> list[str]:
    problems: list[str] = []
    if len(text) > 3500:
        problems.append("输出超过 3500 字符，应压缩重复内容")
    if FORBIDDEN_META.search(text):
        problems.append("输出包含 benchmark、rubric 或智能体评测等内部语境")

    sections = split_sections(text)
    if not sections:
        problems.append("必须且只能包含“第一部分：PPT生成需求”和“第二部分：执行约束”")
        return problems

    first_body, second_body = sections

    page_mentions = [(int(a), int(b or a)) for a, b in PAGE_RANGE.findall(first_body)]
    if not page_mentions:
        problems.append("第一部分没有明确写出页数")
    elif any(not 20 <= start <= end <= 40 for start, end in page_mentions):
        problems.append("页数必须是 20 到 40 页内的具体页数或范围")

    for field, allowed in CONSTRAINT_FIELDS.items():
        status = _constraint_status(second_body, field)
        if status is None:
            problems.append(f"第二部分缺少“{field}”状态")
        elif status not in allowed:
            problems.append(f"“{field}”状态必须是{'/'.join(allowed)}")

    for requirement, pattern in IMAGE_CONSTRAINTS.items():
        status = _semantic_constraint_status(second_body, pattern)
        if status is None:
            problems.append(f"第二部分缺少“{requirement}”要求及状态")

    if not re.search(r"(?:\*\*)?交付形式\s*[：:]", second_body):
        problems.append("第二部分缺少“交付形式”约束")

    missing = missing_protected_tokens(content_task, text)
    if missing:
        preview = "、".join(missing[:12])
        if len(missing) > 12:
            preview += f"等共{len(missing)}项"
        problems.append(f"原任务中的关键年份、单位、字段或缩写被遗漏：{preview}")
    return problems


def rewrite_content_task(
    content_task: str,
    *,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    presentation_profile: dict[str, str] | None = None,
    timeout: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> str:
    if max_attempts < 1:
        raise RuntimeError("max_attempts 必须至少为 1")

    rewrite_feedback = None
    for attempt in range(1, max_attempts + 1):
        draft, finish_reason = call_llm(
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            system_prompt=system_prompt,
            content_task=content_task,
            presentation_profile=presentation_profile,
            rewrite_feedback=rewrite_feedback,
            timeout=timeout,
        )
        normalized_reason = str(finish_reason or "").lower()
        if normalized_reason in {"length", "max_tokens"}:
            error_message = f"模型输出被截断（finish_reason={finish_reason}）"
        else:
            problems = validate_rewrite(content_task, draft)
            if not problems:
                return draft
            error_message = "模型输出未满足合同：" + "；".join(problems)

        if attempt == max_attempts:
            raise RewriteValidationError(
                f"尝试 {max_attempts} 次后仍失败：{error_message}", draft
            )
        print(
            f"第 {attempt} 次改写未通过校验，正在重试：{error_message}",
            file=sys.stderr,
            flush=True,
        )
        rewrite_feedback = (
            f"上一轮输出未通过校验：{error_message}。"
            "请针对该问题重新生成完整的两部分结果，不要解释修改过程。"
        )

    raise RuntimeError("改写重试状态异常")


def read_single_task(args: argparse.Namespace) -> str:
    if args.input:
        return args.input.read_text(encoding="utf-8-sig").strip()
    if args.task:
        return args.task.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise RuntimeError("请传入 content.task、使用 --input，或通过标准输入提供")


def read_case_inputs(case_dir: Path) -> tuple[str, dict[str, str]]:
    """Read fixed ref/metadata files and expose only approved model inputs."""
    ref_path = case_dir / "ref.json"
    metadata_path = case_dir / "case_metadata.json"
    try:
        ref = json.loads(ref_path.read_text(encoding="utf-8-sig"))
        content_task = ref["content"]["task"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RuntimeError(f"{ref_path} 缺少有效的 content.task") from exc
    if not isinstance(content_task, str) or not content_task.strip():
        raise RuntimeError(f"{ref_path} 的 content.task 为空")

    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{metadata_path} 不是有效 JSON") from exc
    if not isinstance(metadata, dict):
        raise RuntimeError(f"{metadata_path} 必须是 JSON 对象")
    missing = [field for field in CASE_METADATA_FIELDS if field not in metadata]
    extra = sorted(set(metadata) - set(CASE_METADATA_FIELDS))
    if missing or extra:
        details: list[str] = []
        if missing:
            details.append("缺少字段：" + "、".join(missing))
        if extra:
            details.append("包含未定义字段：" + "、".join(extra))
        raise RuntimeError(f"{metadata_path} 字段不符合合同（{'；'.join(details)}）")
    invalid = [
        field
        for field in CASE_METADATA_FIELDS
        if not isinstance(metadata[field], str) or not metadata[field].strip()
    ]
    if invalid:
        raise RuntimeError(
            f"{metadata_path} 字段必须是非空字符串：{'、'.join(invalid)}"
        )
    profile = {field: metadata[field].strip() for field in PROFILE_INPUT_FIELDS}
    return content_task.strip(), profile


def single_output_path(
    *, case_dir: Path | None, explicit_output: Path | None
) -> Path | None:
    if explicit_output is not None:
        return explicit_output
    if case_dir is not None:
        return case_dir / "instruction.md"
    return None


def read_jsonl_tasks(path: Path) -> list[tuple[int, str]]:
    """Read DRB2 JSONL and extract exactly ``content.task`` from every row."""
    tasks: list[tuple[int, str]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            try:
                row = json.loads(raw_line)
                task = row["content"]["task"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise RuntimeError(
                    f"JSONL 第 {line_number} 行缺少有效的 content.task"
                ) from exc
            if not isinstance(task, str) or not task.strip():
                raise RuntimeError(f"JSONL 第 {line_number} 行的 content.task 为空")
            tasks.append((line_number, task.strip()))
    if not tasks:
        raise RuntimeError("JSONL 中没有可处理的 content.task")
    return tasks


def run_jsonl_batch(
    *,
    input_path: Path,
    output_path: Path,
    provider: str,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    timeout: int,
    max_attempts: int,
) -> int:
    if input_path.resolve() == output_path.resolve():
        raise RuntimeError("JSONL 输出不能覆盖输入文件")
    tasks = read_jsonl_tasks(input_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    succeeded = 0
    failed = 0
    with output_path.open("w", encoding="utf-8") as handle:
        for position, (line_number, content_task) in enumerate(tasks, start=1):
            print(
                f"[{position}/{len(tasks)}] 正在改写 JSONL 第 {line_number} 行...",
                file=sys.stderr,
                flush=True,
            )
            record = {
                "line_number": line_number,
                "rewritten_query": "",
                "status": "failed",
                "error": "",
            }
            try:
                record["rewritten_query"] = rewrite_content_task(
                    content_task,
                    provider=provider,
                    api_key=api_key,
                    base_url=base_url,
                    model=model,
                    system_prompt=system_prompt,
                    timeout=timeout,
                    max_attempts=max_attempts,
                )
                record["status"] = "success"
                succeeded += 1
            except RewriteValidationError as exc:
                record["rewritten_query"] = exc.draft
                record["error"] = str(exc)
                failed += 1
            except (OSError, RuntimeError) as exc:
                record["error"] = str(exc)
                failed += 1
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()

    print(
        f"批量完成：成功 {succeeded} 条，失败 {failed} 条，合计 {len(tasks)} 条。",
        file=sys.stderr,
    )
    return 0 if failed == 0 else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "将 DeepResearchBench II 的 content.task 改写为两部分式静态 HTML PPT query。"
        )
    )
    parser.add_argument("task", nargs="?", help="单条 content.task 文本")
    parser.add_argument("-i", "--input", type=Path, help="读取单条 UTF-8 content.task")
    parser.add_argument(
        "--case-dir",
        type=Path,
        help=(
            "读取目录中的 ref.json 与 case_metadata.json；默认写入同目录的 "
            "instruction.md"
        ),
    )
    parser.add_argument(
        "--jsonl",
        type=Path,
        help="读取 DeepResearchBench II JSONL；只提取每行 content.task",
    )
    parser.add_argument("-o", "--output", type=Path, help="写入文本或批量 JSONL")
    parser.add_argument(
        "--prompt-file",
        type=Path,
        default=DEFAULT_PROMPT_FILE,
        help="系统 Prompt 文件",
    )
    parser.add_argument(
        "--env-file", type=Path, default=DEFAULT_ENV_FILE, help="环境变量文件"
    )
    parser.add_argument(
        "--provider",
        choices=("claude", "gemini"),
        default=DEFAULT_PROVIDER,
        help="LLM provider（默认：claude）",
    )
    parser.add_argument("--model-name", help="覆盖具体模型名")
    parser.add_argument("--timeout", type=int, default=180, help="请求超时秒数")
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help=f"模型输出校验失败时的最大尝试次数（默认：{DEFAULT_MAX_ATTEMPTS}）",
    )
    args = parser.parse_args()

    input_count = sum(
        bool(value) for value in (args.task, args.input, args.jsonl, args.case_dir)
    )
    if input_count > 1:
        parser.error("task、--input、--jsonl 和 --case-dir 只能选择一种输入方式")
    if args.jsonl and not args.output:
        parser.error("--jsonl 批量模式必须通过 --output 指定输出 JSONL")
    if args.max_attempts < 1:
        parser.error("--max-attempts 必须至少为 1")
    return args


def main() -> int:
    args = parse_args()
    try:
        load_env(args.env_file)
        system_prompt = load_system_prompt(args.prompt_file)
        api_key_env, base_url_env, model_env = PROVIDER_ENV[args.provider]
        api_key = os.environ.get(api_key_env, "")
        base_url = os.environ.get(base_url_env, "")
        model = (
            args.model_name
            or os.environ.get(model_env)
            or DEFAULT_MODELS[args.provider]
        )
        if not api_key:
            raise RuntimeError(f"缺少 {api_key_env}，请在环境变量或 .env 中配置")
        if not base_url:
            raise RuntimeError(f"缺少 {base_url_env}，请在环境变量或 .env 中配置")

        if args.jsonl:
            return run_jsonl_batch(
                input_path=args.jsonl,
                output_path=args.output,
                provider=args.provider,
                api_key=api_key,
                base_url=base_url,
                model=model,
                system_prompt=system_prompt,
                timeout=args.timeout,
                max_attempts=args.max_attempts,
            )

        presentation_profile = None
        if args.case_dir:
            content_task, presentation_profile = read_case_inputs(args.case_dir)
        else:
            content_task = read_single_task(args)
        if not content_task:
            raise RuntimeError("content.task 不能为空")
        rewritten = rewrite_content_task(
            content_task,
            provider=args.provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
            system_prompt=system_prompt,
            presentation_profile=presentation_profile,
            timeout=args.timeout,
            max_attempts=args.max_attempts,
        )
        output_path = single_output_path(
            case_dir=args.case_dir, explicit_output=args.output
        )
        if output_path:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(rewritten + "\n", encoding="utf-8")
        else:
            print(rewritten)
        return 0
    except (OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
