"""Judge transport, response parsing, and presentation artifact checks."""

from __future__ import annotations

import base64
import io
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


OPENAI_JSON_MODE = {"type": "json_object"}
JUDGE_IMAGE_MAX_DIMENSION = int(
    os.environ.get("JUDGE_IMAGE_MAX_DIMENSION", "1600")
)
JUDGE_IMAGE_JPEG_QUALITY = int(
    os.environ.get("JUDGE_IMAGE_JPEG_QUALITY", "85")
)
BROWSER_RUNTIME_LIBRARIES = (
    "libnspr4.so",
    "libnss3.so",
    "libnssutil3.so",
    "libatk-1.0.so.0",
    "libatk-bridge-2.0.so.0",
    "libXcomposite.so.1",
    "libXdamage.so.1",
    "libXfixes.so.3",
    "libXrandr.so.2",
    "libgbm.so.1",
    "libxkbcommon.so.0",
    "libatspi.so.0",
)
MULTI_ELIGIBILITY_CONTRACT_VERSION = "multi_page_generation_eligibility_v1"
SLIDE_HTML_RE = re.compile(r"^slide_([0-9]+)\.html$")
SLIDE_PNG_RE = re.compile(r"^slide_([0-9]+)\.png$")


class RunnerError(ValueError):
    """Raised when Judge inputs or runtime behavior are invalid."""


class JudgeTransportError(RunnerError):
    """Raised for a Judge transport failure with explicit retry semantics."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class SlideArtifact:
    index: int
    html_path: Path
    image_path: Path

    @property
    def label(self) -> str:
        return f"page_{self.index:02d}"


def require_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise RunnerError(f"{label} does not exist or is not a directory: {path}")


def safe_component(value: str, label: str) -> str:
    if not value or value in {".", ".."} or Path(value).name != value:
        raise RunnerError(f"{label} must be one directory name: {value!r}")
    return value


def sanitized_component(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not result:
        raise RunnerError(f"cannot derive a path component from {value!r}")
    return result


def configure_browser_library_path() -> str | None:
    """Expose Conda-provided Chromium libraries to Playwright child processes."""
    candidates: list[Path] = []
    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        candidates.append(Path(conda_prefix) / "lib")
    candidates.append(Path(sys.prefix) / "lib")

    for library_dir in candidates:
        if not all(
            (library_dir / library).is_file()
            for library in BROWSER_RUNTIME_LIBRARIES
        ):
            continue
        value = str(library_dir.resolve())
        current = [
            item
            for item in os.environ.get("LD_LIBRARY_PATH", "").split(":")
            if item
        ]
        if value not in current:
            os.environ["LD_LIBRARY_PATH"] = ":".join([value, *current])
        return value
    return None


def complete_truncated_json_containers(text: str) -> str | None:
    """Close structurally open JSON containers without inventing content."""
    stack: list[str] = []
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            stack.append("}")
        elif character == "[":
            stack.append("]")
        elif character in "}]":
            if not stack or stack[-1] != character:
                return None
            stack.pop()
    if in_string or not stack:
        return None
    return text + "".join(reversed(stack))


def parse_json_response(raw: str, label: str) -> dict[str, Any]:
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.I | re.S)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
    except json.JSONDecodeError as original_error:
        start, end = text.find("{"), text.rfind("}")
        if start < 0:
            raise RunnerError(f"{label} did not return a JSON object") from None
        try:
            if end <= start:
                raise json.JSONDecodeError("missing closing object", text, len(text))
            value = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            completed = complete_truncated_json_containers(text[start:])
            if completed is not None:
                try:
                    value = json.loads(completed)
                except json.JSONDecodeError:
                    completed = None
            if completed is not None:
                if not isinstance(value, dict):
                    raise RunnerError(f"{label} response must be a JSON object")
                return value
            try:
                value, consumed = json.JSONDecoder().raw_decode(text[start:])
            except json.JSONDecodeError as exc:
                raise RunnerError(f"{label} returned invalid JSON: {exc}") from exc
            trailing = text[start + consumed :].strip()
            if trailing and re.fullmatch(r"}+(?:\s*```)?", trailing) is None:
                raise RunnerError(
                    f"{label} returned invalid JSON: {original_error}"
                ) from original_error
    if not isinstance(value, dict):
        raise RunnerError(f"{label} response must be a JSON object")
    return value


def openai_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base if base.endswith("/chat/completions") else base + "/chat/completions"


def anthropic_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    return base + "/messages" if base.endswith("/v1") else base + "/v1/messages"


def http_json(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: int,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise JudgeTransportError(
                f"judge returned invalid transport JSON: {exc}",
                retryable=True,
            ) from exc
        if not isinstance(value, dict):
            raise JudgeTransportError(
                "judge transport response must be a JSON object",
                retryable=True,
            )
        return value
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2000]
        retryable = exc.code in {408, 429} or 500 <= exc.code < 600
        raise JudgeTransportError(
            f"judge HTTP {exc.code}: {detail}",
            retryable=retryable,
        ) from exc
    except urllib.error.URLError as exc:
        raise JudgeTransportError(
            f"judge request failed: {exc}", retryable=True
        ) from exc
    except TimeoutError as exc:
        raise JudgeTransportError(
            f"judge request timed out: {exc}", retryable=True
        ) from exc


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0


def normalize_judge_token_usage(usage: Any) -> dict[str, int] | None:
    """Normalize OpenAI-compatible and Anthropic usage payloads."""
    if not isinstance(usage, dict):
        return None
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        input_tokens = _nonnegative_int(usage.get("prompt_tokens"))
        output_tokens = _nonnegative_int(usage.get("completion_tokens"))
        prompt_details = usage.get("prompt_tokens_details")
        cache_read = (
            _nonnegative_int(prompt_details.get("cached_tokens"))
            if isinstance(prompt_details, dict)
            else 0
        )
        total_tokens = _nonnegative_int(usage.get("total_tokens"))
        if total_tokens == 0 and (input_tokens or output_tokens):
            total_tokens = input_tokens + output_tokens
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": 0,
        }

    uncached_input = _nonnegative_int(usage.get("input_tokens"))
    output_tokens = _nonnegative_int(usage.get("output_tokens"))
    cache_read = _nonnegative_int(usage.get("cache_read_input_tokens"))
    cache_creation = _nonnegative_int(usage.get("cache_creation_input_tokens"))
    cache_creation_details = usage.get("cache_creation")
    if cache_creation == 0 and isinstance(cache_creation_details, dict):
        cache_creation = sum(
            _nonnegative_int(cache_creation_details.get(key))
            for key in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        )
    if not any((uncached_input, output_tokens, cache_read, cache_creation)):
        return None
    input_tokens = uncached_input + cache_read + cache_creation
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }


def response_indicates_output_truncation(
    response_meta: dict[str, Any], max_tokens: int
) -> bool:
    finish_reason = str(response_meta.get("finish_reason") or "").lower()
    stop_reason = str(response_meta.get("stop_reason") or "").lower()
    if finish_reason in {"length", "max_tokens", "max_output_tokens"}:
        return True
    if stop_reason in {"length", "max_tokens", "max_output_tokens"}:
        return True
    usage = normalize_judge_token_usage(response_meta.get("usage"))
    if usage is None or max_tokens < 1:
        return False
    margin = max(32, int(max_tokens * 0.01))
    return usage["output_tokens"] >= max_tokens - margin


def aggregate_judge_token_usage(metadata: dict[str, Any]) -> dict[str, Any]:
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    api_attempts = 0
    usage_records = 0
    for task in metadata.values():
        if not isinstance(task, dict):
            continue
        attempts = task.get("attempts")
        if not isinstance(attempts, list):
            continue
        api_attempts += len(attempts)
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            normalized = normalize_judge_token_usage(attempt.get("usage"))
            if normalized is None:
                continue
            usage_records += 1
            for key in totals:
                totals[key] += normalized[key]
    return {
        "available": usage_records > 0,
        **totals,
        "api_attempts": api_attempts,
        "usage_records": usage_records,
    }


def image_data(path: Path) -> tuple[str, str]:
    guessed_mime = mimetypes.guess_type(path.name)[0] or ""
    if os.environ.get("DIMENSION_JUDGE_BOUNDED_IMAGE_MODE") != "1":
        return (
            guessed_mime or "image/png",
            base64.b64encode(path.read_bytes()).decode("ascii"),
        )
    if guessed_mime == "application/pdf":
        return guessed_mime, base64.b64encode(path.read_bytes()).decode("ascii")
    if JUDGE_IMAGE_MAX_DIMENSION < 1:
        raise RunnerError("JUDGE_IMAGE_MAX_DIMENSION must be positive")
    if not 1 <= JUDGE_IMAGE_JPEG_QUALITY <= 100:
        raise RunnerError("JUDGE_IMAGE_JPEG_QUALITY must be in [1, 100]")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise RunnerError(
            "Pillow is required to encode Judge image attachments"
        ) from exc
    try:
        with Image.open(path) as source:
            source.seek(0)
            rgba = source.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            preview = background.convert("RGB")
            preview.thumbnail(
                (JUDGE_IMAGE_MAX_DIMENSION, JUDGE_IMAGE_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            buffer = io.BytesIO()
            preview.save(
                buffer,
                format="JPEG",
                quality=JUDGE_IMAGE_JPEG_QUALITY,
                optimize=True,
            )
    except (OSError, UnidentifiedImageError) as exc:
        raise RunnerError(
            f"failed to decode Judge image attachment {path}: {exc}"
        ) from exc
    return "image/jpeg", base64.b64encode(buffer.getvalue()).decode("ascii")


VisualAttachment = Path | list[Path] | tuple[Path, ...] | None


def json_schema_from_template(value: Any) -> dict[str, Any]:
    """Build the strict structural schema required by Anthropic JSON output.

    Judge task mock responses already encode the exact response shape accepted by
    the corresponding validator.  Values remain unconstrained so the model still
    performs the rubric judgment; only JSON syntax and field structure are fixed.
    """
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {
                str(key): json_schema_from_template(item)
                for key, item in value.items()
            },
            "required": [str(key) for key in value],
            "additionalProperties": False,
        }
    if isinstance(value, list):
        schema: dict[str, Any] = {
            "type": "array",
            "items": (
                json_schema_from_template(value[0])
                if value
                else {"type": "string"}
            ),
        }
        if value:
            schema["minItems"] = 1
        return schema
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    return {"type": "string"}


def attachment_paths(attachment: VisualAttachment) -> list[Path]:
    if attachment is None:
        return []
    if isinstance(attachment, Path):
        return [attachment]
    paths = list(attachment)
    if not paths:
        raise RunnerError("visual attachment list must not be empty")
    if not all(isinstance(path, Path) for path in paths):
        raise RunnerError("visual attachment list must contain only Path values")
    return paths


def openai_visual_content(
    user_prompt: str,
    attachment: VisualAttachment,
) -> str | list[dict[str, Any]]:
    paths = attachment_paths(attachment)
    if not paths:
        return user_prompt
    if len(paths) == 1:
        mime, encoded = image_data(paths[0])
        if mime == "application/pdf":
            visual_block = {
                "type": "file",
                "file": {
                    "filename": paths[0].name,
                    "file_data": f"data:{mime};base64,{encoded}",
                },
            }
        elif mime.startswith("image/"):
            visual_block = {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        else:
            raise RunnerError(f"unsupported visual attachment MIME type: {mime}")
        return [{"type": "text", "text": user_prompt}, visual_block]

    width = max(2, len(str(len(paths))))
    content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for index, path in enumerate(paths, start=1):
        mime, encoded = image_data(path)
        if not mime.startswith("image/"):
            raise RunnerError(
                "ordered multi-attachment input supports images only; "
                f"got {mime} for {path}"
            )
        label = f"SLIDE_{index:0{width}d}_OF_{len(paths):0{width}d}"
        content.extend(
            [
                {
                    "type": "text",
                    "text": (
                        f"{label}. The immediately following image is this slide. "
                        "Use this explicit index for all page-level evidence."
                    ),
                },
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                },
            ]
        )
    return content


def anthropic_visual_content(
    user_prompt: str,
    attachment: VisualAttachment,
) -> str | list[dict[str, Any]]:
    paths = attachment_paths(attachment)
    if not paths:
        return user_prompt
    if len(paths) == 1:
        mime, encoded = image_data(paths[0])
        if mime == "application/pdf":
            attachment_type = "document"
        elif mime.startswith("image/"):
            attachment_type = "image"
        else:
            raise RunnerError(f"unsupported visual attachment MIME type: {mime}")
        return [
            {"type": "text", "text": user_prompt},
            {
                "type": attachment_type,
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": encoded,
                },
            },
        ]

    width = max(2, len(str(len(paths))))
    content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
    for index, path in enumerate(paths, start=1):
        mime, encoded = image_data(path)
        if not mime.startswith("image/"):
            raise RunnerError(
                "ordered multi-attachment input supports images only; "
                f"got {mime} for {path}"
            )
        label = f"SLIDE_{index:0{width}d}_OF_{len(paths):0{width}d}"
        content.extend(
            [
                {
                    "type": "text",
                    "text": (
                        f"{label}. The immediately following image is this slide. "
                        "Use this explicit index for all page-level evidence."
                    ),
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": mime,
                        "data": encoded,
                    },
                },
            ]
        )
    return content


def call_openai_compatible(
    model: str,
    base_url: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    image_path: VisualAttachment,
    max_tokens: int,
    timeout: int,
) -> tuple[str, dict[str, Any]]:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "response_format": OPENAI_JSON_MODE,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": openai_visual_content(user_prompt, image_path),
            },
        ],
    }
    data = http_json(
        openai_endpoint(base_url),
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        body,
        timeout,
    )
    try:
        choice = data["choices"][0]
        content = choice["message"].get("content", "")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise JudgeTransportError(
            f"OpenAI-compatible Judge returned an invalid response shape: {exc}",
            retryable=True,
        ) from exc
    if isinstance(content, list):
        content = "".join(
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict)
        )
    return str(content), {
        "response_id": data.get("id"),
        "usage": data.get("usage"),
        "finish_reason": choice.get("finish_reason"),
        "response_format": OPENAI_JSON_MODE,
    }


def call_anthropic(
    model: str,
    base_url: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    image_path: VisualAttachment,
    max_tokens: int,
    timeout: int,
    response_schema_template: dict[str, Any] | None = None,
    response_json_schema: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system_prompt,
        "messages": [
            {
                "role": "user",
                "content": anthropic_visual_content(user_prompt, image_path),
            }
        ],
    }
    if response_json_schema is not None or response_schema_template is not None:
        body["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": (
                    response_json_schema
                    if response_json_schema is not None
                    else json_schema_from_template(response_schema_template)
                ),
            }
        }
    data = http_json(
        anthropic_endpoint(base_url),
        {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        body,
        timeout,
    )
    content_items = data.get("content", [])
    if not isinstance(content_items, list):
        raise JudgeTransportError(
            "Anthropic Judge response content must be an array",
            retryable=True,
        )
    raw = "".join(
        str(item.get("text", ""))
        for item in content_items
        if isinstance(item, dict) and item.get("type") == "text"
    )
    return raw, {
        "response_id": data.get("id"),
        "usage": data.get("usage"),
        "stop_reason": data.get("stop_reason"),
    }


def call_judge(
    *,
    judge_id: str,
    provider: str,
    model: str,
    base_url: str,
    api_key: str,
    system_prompt: str,
    user_prompt: str,
    image_path: VisualAttachment,
    max_tokens: int,
    timeout: int,
    response_schema_template: dict[str, Any] | None = None,
    response_json_schema: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    if provider == "openai":
        return call_openai_compatible(
            model,
            base_url,
            api_key,
            system_prompt,
            user_prompt,
            image_path,
            max_tokens,
            timeout,
        )
    if provider == "anthropic":
        return call_anthropic(
            model,
            base_url,
            api_key,
            system_prompt,
            user_prompt,
            image_path,
            max_tokens,
            timeout,
            response_schema_template,
            response_json_schema,
        )
    raise RunnerError(f"unsupported provider for {judge_id}: {provider}")


def choose_run(model_dir: Path, requested: str | None) -> Path:
    require_directory(model_dir, "generation model directory")
    if requested:
        run_dir = model_dir / safe_component(requested, "--gen-run")
        require_directory(run_dir, "generation run directory")
        return run_dir
    runs = sorted(
        path
        for path in model_dir.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    )
    if not runs:
        raise RunnerError(f"no generation runs found in {model_dir}")
    if len(runs) > 1:
        names = ", ".join(path.name for path in runs)
        raise RunnerError(
            f"multiple runs found in {model_dir}; select one with --gen-run: {names}"
        )
    return runs[0]


def _indexed_files(
    directory: Path,
    pattern: re.Pattern[str],
) -> tuple[dict[int, Path], list[str]]:
    if not directory.is_dir():
        return {}, []
    indexed: dict[int, Path] = {}
    noncanonical: list[str] = []
    for path in sorted(item for item in directory.iterdir() if item.is_file()):
        match = pattern.fullmatch(path.name)
        if match is None:
            if path.name.startswith("slide_"):
                noncanonical.append(path.name)
            continue
        index = int(match.group(1))
        expected = f"slide_{index:02d}{path.suffix}"
        if index < 1 or path.name != expected or index in indexed:
            noncanonical.append(path.name)
            continue
        indexed[index] = path
    return indexed, noncanonical


def discover_slide_artifacts(run_dir: Path) -> list[SlideArtifact]:
    """Return strictly paired, consecutive HTML/PNG artifacts."""
    html_by_index, bad_html = _indexed_files(run_dir / "slides", SLIDE_HTML_RE)
    png_by_index, bad_png = _indexed_files(run_dir / "renders", SLIDE_PNG_RE)
    if bad_html or bad_png:
        raise RunnerError(
            "noncanonical multi-page artifacts found; "
            f"html={bad_html}, renders={bad_png}"
        )
    html_indices = set(html_by_index)
    png_indices = set(png_by_index)
    if html_indices != png_indices:
        raise RunnerError(
            "multi-page HTML/PNG indices do not match; "
            f"missing_renders={sorted(html_indices - png_indices)}, "
            f"renders_without_html={sorted(png_indices - html_indices)}"
        )
    if not html_indices:
        raise RunnerError("evaluation requires at least one paired HTML/PNG page")
    expected = set(range(1, max(html_indices) + 1))
    if html_indices != expected:
        raise RunnerError(
            "multi-page artifacts must be consecutive from slide_01; "
            f"missing_indices={sorted(expected - html_indices)}"
        )
    return [
        SlideArtifact(index, html_by_index[index], png_by_index[index])
        for index in sorted(html_indices)
    ]


def assess_multi_generation_eligibility(
    run_dir: Path,
    slides: list[SlideArtifact],
) -> dict[str, Any]:
    invalid_html: list[int] = []
    invalid_png: list[int] = []
    for slide in slides:
        prefix = slide.html_path.read_text(
            encoding="utf-8", errors="ignore"
        )[:4096].lower()
        if slide.html_path.stat().st_size == 0 or not (
            "<html" in prefix or "<!doctype html" in prefix
        ):
            invalid_html.append(slide.index)
        if slide.image_path.stat().st_size <= 8:
            invalid_png.append(slide.index)
        else:
            with slide.image_path.open("rb") as handle:
                if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                    invalid_png.append(slide.index)

    warnings: list[str] = []
    metadata_path = run_dir / "generation_metadata.json"
    metadata: dict[str, Any] | None = None
    if metadata_path.is_file():
        try:
            value = json.loads(metadata_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                metadata = value
            else:
                warnings.append(f"generation metadata must be an object: {metadata_path}")
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"invalid generation metadata {metadata_path}: {exc}")
    if metadata and metadata.get("generation_contract_satisfied") is False:
        warnings.append(
            "generation_metadata reports generation_contract_satisfied=false; "
            "multi-page judge admission is based on complete canonical paired artifacts"
        )
    if len(slides) < 2:
        warnings.append(
            "only one page was generated; page-count compliance is left to rubric judging"
        )
    eligible = bool(slides) and not invalid_html and not invalid_png
    return {
        "contract_version": MULTI_ELIGIBILITY_CONTRACT_VERSION,
        "eligible": eligible,
        "decision": "eligible" if eligible else "ineligible",
        "eligibility_basis": "canonical_paired_artifacts" if eligible else None,
        "checks": {
            "minimum_two_pages": len(slides) >= 2,
            "paired_consecutive_artifacts": True,
            "all_html_valid": not invalid_html,
            "all_png_valid": not invalid_png,
        },
        "page_count": len(slides),
        "invalid_html_indices": invalid_html,
        "invalid_png_indices": invalid_png,
        "warnings": warnings,
        "source": str(metadata_path) if metadata_path.is_file() else None,
    }
