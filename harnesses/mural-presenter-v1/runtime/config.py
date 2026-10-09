"""编译不可变运行配置；版式由模型在共享 HTML/CSS 中决定。"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import re
from types import MappingProxyType
from typing import Mapping, Sequence
from urllib.parse import urlsplit


AUTHORING_PROFILES = frozenset({"general"})
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


def model_effort_capability(model: str) -> dict:
    """明确型号的官方强度合同；Studio 与 Provider 共用，不推广到整个模型系列。"""
    name = str(model or "").strip().lower()
    if name in {
        "gpt-6-astra/azure/sfa",
        "gpt-6-astra/azure_l/qwb",
        "gpt-6-astra/mix_l/sfj",
        "gpt-6-astra/azure/sfh",
    }:
        return {"levels": ("low", "medium", "high", "xhigh", "max"), "default": "xhigh",
                "without_thinking": (), "transport": "reasoning_effort"}
    if re.fullmatch(r"(?:qwen/)?qwen3[.-]8-27b(?:-(?:fp8|bf16))?", name):
        return {"levels": ("low", "medium", "xhigh"), "default": "xhigh",
                "without_thinking": (), "transport": "chat_template_kwargs"}
    if name == "kimi-k3":
        return {"levels": ("low", "high", "max"), "default": "max", "without_thinking": (),
                "transport": "reasoning_effort"}
    if name in {"glm-5.3", "glm-5.3-flash", "glm-5.3-flash/zai_l/sfc"}:
        return {"levels": ("low", "high", "max"), "default": "max", "without_thinking": (),
                "transport": "glm"}
    opus = re.fullmatch(r"claude-opus-(4[.-][678]|5)(?:-thinking)?(?:-\d{8})?", name)
    if opus:
        version = opus.group(1).replace(".", "-")
        levels = tuple(level for level in EFFORT_LEVELS if version != "4-6" or level != "xhigh")
        return {"levels": levels, "default": "high", "transport": "anthropic",
                "without_thinking": levels[:3] if version == "5" else levels}
    return {"levels": (), "default": "", "without_thinking": (), "transport": ""}


def validate_reasoning_effort(model: str, effort: str, *, thinking: bool) -> str:
    """拒绝非法档位和组合，不通过静默降档掩盖配置错误。"""
    value = str(effort or "").strip().lower()
    capability = model_effort_capability(model)
    if (str(model or "").strip().lower() == "kimi-k3" or capability["transport"] == "glm") and not thinking:
        raise ValueError(f"{model} always uses thinking; disabled thinking is not supported")
    levels = capability["levels"] or EFFORT_LEVELS
    if value and value not in levels:
        raise ValueError(f"{model or 'model'} reasoning effort must be one of {', '.join(levels)}; got {value!r}")
    if (value and not thinking and capability["transport"] == "anthropic"
            and value not in capability["without_thinking"]):
        raise ValueError(f"{model} effort={value} requires thinking enabled")
    return value


def maintenance_reasoning(model: str) -> tuple[bool, str]:
    """Always-thinking models use low effort for maintenance requests."""
    return (True, "low") if (str(model or "").strip().lower() == "kimi-k3"
                             or model_effort_capability(model)["transport"] == "glm") else (False, "")


def provider_request_budget_s(settings: ProviderSettings) -> float:
    """Keep one finite decision budget even when the legacy per-attempt limit is disabled."""
    if settings.request_budget_s > 0:
        return float(settings.request_budget_s)
    return float(settings.total_timeout_s) if settings.total_timeout_s > 0 else 600.0


def service_url(value: object) -> str:
    """服务地址不承载凭证、查询参数或重定向目标。"""
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("runtime_config: invalid service URL")
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or "?" in value or "#" in value
            or parts.port == 0):
        raise ValueError("runtime_config: unsafe service URL")
    return value.rstrip("/")


def load_runtime_config(environment: Mapping[str, str] | None = None) -> dict:
    """并发、共享资源与 MinerU 只读同一个 JSON；旧环境参数不参与覆盖。"""
    env = os.environ if environment is None else environment
    repository = Path(__file__).resolve().parents[3]
    path = Path(env.get("MURAL_RUNTIME_CONFIG") or os.environ.get("MURAL_RUNTIME_CONFIG")
                or repository / "configs/runtime.json").expanduser()
    if not path.is_absolute():
        raise ValueError("MURAL_RUNTIME_CONFIG must be an absolute path")
    value = json.loads(path.read_text(encoding="utf-8"))

    def fields(section: object, keys: set[str], name: str) -> dict:
        if not isinstance(section, dict) or set(section) != keys:
            raise ValueError(f"runtime_config: {name} requires exactly {sorted(keys)}")
        return section

    def positive(section: dict, keys: set[str], name: str) -> None:
        for key in keys:
            if type(section[key]) is not int or section[key] <= 0:
                raise ValueError(f"runtime_config: {name}.{key} must be a positive integer")

    fields(value, {"decks", "resources", "mineru"}, "root")
    decks = fields(value["decks"], {"total", "per_model", "owners"}, "decks")
    positive(decks, set(decks), "decks")
    if decks["per_model"] > decks["total"]:
        raise ValueError("runtime_config: decks.per_model exceeds decks.total")
    resources = fields(value["resources"],
        {"directory", "provider", "render", "web_search", "image_generation"}, "resources")
    positive(resources, set(resources) - {"directory"}, "resources")
    if not isinstance(resources["directory"], str) or not Path(resources["directory"]).is_absolute():
        raise ValueError("runtime_config: resources.directory must be an absolute shared path")
    resources["directory"] = str(Path(resources["directory"]).resolve())
    mineru = fields(value["mineru"], {"endpoints", "gpu", "cpu"}, "mineru")
    endpoints = mineru["endpoints"]
    if not isinstance(endpoints, list) or len(endpoints) != 2:
        raise ValueError("runtime_config: mineru.endpoints requires GPU then CPU")
    mineru["endpoints"] = [service_url(endpoint) for endpoint in endpoints]
    if len(set(mineru["endpoints"])) != 2:
        raise ValueError("runtime_config: MinerU endpoints must be distinct")
    for kind, keys in (("gpu", {"concurrency", "window"}), ("cpu", {"concurrency", "window", "cores"})):
        section = fields(mineru[kind], keys, f"mineru.{kind}")
        positive(section, keys, f"mineru.{kind}")
    if mineru["cpu"]["cores"] < mineru["cpu"]["concurrency"]:
        raise ValueError("runtime_config: CPU core budget is smaller than document concurrency")
    return value


class RunMode(str, Enum):
    """区分实时推理与需要完整轨迹的合成运行。"""

    INFERENCE = "inference"
    SYNTHESIS = "synthesis"


@dataclass(frozen=True)
class RunProfile:
    """记录某种运行模式采用的上下文与轨迹策略。"""

    name: RunMode
    compact_active_history: bool
    require_complete_trace: bool


RUN_PROFILES = {
    RunMode.INFERENCE: RunProfile(
        name=RunMode.INFERENCE,
        compact_active_history=True,
        require_complete_trace=False,
    ),
    RunMode.SYNTHESIS: RunProfile(
        name=RunMode.SYNTHESIS,
        compact_active_history=True,
        require_complete_trace=True,
    ),
}


# 三种传输格式;旧部署里的 ``openai`` 值统一解析为自托管 Chat Completions,
# 保持既有行为不变(这是刻意保留的配置别名,不是遗留代码分支)。
API_FORMATS = frozenset({
    "anthropic",
    "openai_responses",
    "openai_chat_compatible",
})
_API_FORMAT_ALIASES = {"openai": "openai_chat_compatible"}


def resolve_api_format(value: object) -> str:
    """把配置里的 API 格式规范为受支持的三值之一(含旧 openai 别名)。"""
    normalized = str(value or "").strip().lower()
    normalized = _API_FORMAT_ALIASES.get(normalized, normalized)
    if normalized not in API_FORMATS:
        raise ValueError(
            "MODEL_API_FORMAT must be 'anthropic', 'openai_responses', or "
            "'openai_chat_compatible' (legacy 'openai' maps to "
            "'openai_chat_compatible')"
        )
    return normalized


@dataclass(frozen=True)
class ProviderSettings:
    """保存三种 API 格式共用的非敏感请求配置。"""

    api_format: str
    model: str
    base_url: str
    total_timeout_s: int = 600
    queue_timeout_s: int = 1800
    connect_timeout_s: int = 30
    idle_timeout_s: int = 120
    channel_cooldown_s: int = 120
    max_attempts: int = 6
    # 默认非流式:流式在 opus-5 超长 adaptive thinking 下会从网关拿到畸形分块(空 thinking +
    # 空参 tool),非流式返回完整消息更稳。非流式对大 max_tokens 会撞 SDK 的 10min 守卫,但
    # AnthropicTransport 在调用处显式传 timeout 已绕过该守卫(见 complete)。需要 WebUI 实时
    # 预览时可用 MODEL_STREAMING=1 单独开。
    streaming: bool = False
    return_token_ids: bool = False
    prompt_cache: bool = True
    prompt_cache_messages: bool = True
    temperature: float = 0.3
    reasoning_transport: str = ""
    # 模型上下文窗口(输入+输出共享)的 token 上限。用于 agent loop 的 pre-send 预算门:
    # 当估算的输入 token + 待生成 max_tokens + 安全余量逼近该窗口时,在闭合工具边界压缩
    # 历史,避免累积上下文撞 Provider 硬性 400(BUG-1)。默认 262144 对齐自托管 27B;
    # 更大窗口的模型(如 opus)应显式调高。
    context_window: int = 262144
    # compact 触发的安全余量(token)。窗口 - max_tokens - safety_buffer 即输入预算上限。
    context_safety_buffer: int = 8192
    # Optional whole-decision budget, including queueing and all transport retries.
    request_budget_s: int = 0

    def __post_init__(self) -> None:
        """规范 API 格式和地址,并拒绝不合理的模型请求参数。"""
        api_format = resolve_api_format(self.api_format)
        if not self.model.strip():
            raise ValueError("provider model must not be empty")
        if not self.base_url.strip():
            raise ValueError("provider base_url must not be empty")
        if self.total_timeout_s < 0:
            raise ValueError("provider total timeout must be non-negative (0 disables the per-attempt response limit)")
        if self.request_budget_s < 0:
            raise ValueError("provider request budget must be non-negative (0 uses the default budget)")
        if self.queue_timeout_s <= 0:
            raise ValueError("model queue timeout must be positive")
        if self.connect_timeout_s <= 0 or self.idle_timeout_s <= 0:
            raise ValueError("provider phase timeouts must be positive")
        if self.channel_cooldown_s < 0:
            raise ValueError("provider channel cooldown must be non-negative")
        if self.max_attempts <= 0:
            raise ValueError("provider max attempts must be positive")
        if self.context_window <= 0:
            raise ValueError("provider context window must be positive")
        if self.context_safety_buffer < 0:
            raise ValueError("provider context safety buffer must be non-negative")
        object.__setattr__(self, "api_format", api_format)
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "reasoning_transport", "reasoning_effort"
                           if self.model.lower() == "kimi-k3" else "glm"
                           if model_effort_capability(self.model)["transport"] == "glm" else self.reasoning_transport.strip())

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "ProviderSettings":
        """只读取统一的 ``MODEL_*`` 参数并生成 Provider 配置。"""
        env = environment if environment is not None else os.environ
        api_format = resolve_api_format(_text(env, "MODEL_API_FORMAT", "anthropic"))
        if api_format == "anthropic":
            default_model, default_url = (
                "claude-opus-4-7-thinking",
                "https://api.anthropic.com",
            )
        elif api_format == "openai_responses":
            default_model, default_url = "gpt-5", "https://api.openai.com"
        else:
            default_model, default_url = "gpt-5", "https://api.openai.com"
        return cls(
            api_format=api_format,
            model=_text(env, "MODEL", default_model),
            base_url=_text(env, "MODEL_BASE_URL", default_url),
            total_timeout_s=_nonnegative_int(env, "MODEL_TIMEOUT_S", 600),
            request_budget_s=_nonnegative_int(env, "MODEL_REQUEST_BUDGET_S", 0),
            queue_timeout_s=_positive_int(env, "MODEL_QUEUE_TIMEOUT_S", 1800),
            connect_timeout_s=_positive_int(env, "MODEL_CONNECT_TIMEOUT_S", 30),
            idle_timeout_s=_positive_int(env, "MODEL_IDLE_TIMEOUT_S", 120),
            channel_cooldown_s=_nonnegative_int(
                env, "MODEL_CHANNEL_COOLDOWN_S", 120
            ),
            max_attempts=_positive_int(env, "MODEL_MAX_ATTEMPTS", 6),
            streaming=_boolean(env, "MODEL_STREAMING", False),
            return_token_ids=_boolean(env, "MODEL_RETURN_TOKEN_IDS", False),
            prompt_cache=_boolean(env, "MODEL_PROMPT_CACHE", True),
            prompt_cache_messages=_boolean(
                env, "MODEL_PROMPT_CACHE_MESSAGES", True
            ),
            temperature=_float(env, "MODEL_TEMPERATURE", 0.3),
            reasoning_transport=_text(env, "MODEL_REASONING_TRANSPORT"),
            context_window=_positive_int(env, "MODEL_CONTEXT_WINDOW", 262144),
            context_safety_buffer=_nonnegative_int(
                env, "MODEL_CONTEXT_SAFETY_BUFFER", 8192
            ),
        )

    def to_manifest(self) -> dict[str, object]:
        """生成不含 API key、token 或 secret path 的运行清单字段。"""
        return {
            "api_format": self.api_format,
            "model": self.model,
            "base_url": self.base_url,
            "total_timeout_s": self.total_timeout_s,
            "queue_timeout_s": self.queue_timeout_s,
            "connect_timeout_s": self.connect_timeout_s,
            "idle_timeout_s": self.idle_timeout_s,
            "channel_cooldown_s": self.channel_cooldown_s,
            "max_attempts": self.max_attempts,
            "streaming": self.streaming,
            "return_token_ids": self.return_token_ids,
            "prompt_cache": self.prompt_cache,
            "prompt_cache_messages": self.prompt_cache_messages,
            "temperature": self.temperature,
            "reasoning_transport": self.reasoning_transport,
            "context_window": self.context_window,
            "context_safety_buffer": self.context_safety_buffer,
        }


@dataclass(frozen=True)
class RunSpec:
    """保存一次运行的原始输入；画布确认结果不属于入口输入。"""

    raw_user_query: str
    attachments: tuple[Path, ...]
    run_mode: RunMode
    authoring_profile: str
    language: str
    provider: ProviderSettings
    output_workspace: Path
    page_count_hint: int | None = None
    revision_request: Mapping[str, object] | None = None
    # 与 ``attachments`` 顺序一一对应的显式用途；空串/None 表示未指定（保守默认）。
    # 留空 tuple 表示整批都未声明用途，行为与历史完全一致。
    attachment_intents: tuple[str | None, ...] = ()

    def __post_init__(self) -> None:
        """校验入口输入并冻结路径及可选 UI 提示的副本。"""
        profile = self.authoring_profile.strip().lower().replace("_", "-")
        language = normalize_language(self.language)
        if not language and str(self.language or "auto").strip().lower() in {"", "auto"}:
            language = "auto"
        workspace = self.output_workspace.expanduser().resolve()
        attachments = tuple(path.expanduser().resolve() for path in self.attachments)
        if not self.raw_user_query.strip():
            raise ValueError("raw_user_query must not be empty")
        if profile not in AUTHORING_PROFILES:
            raise ValueError(
                "authoring_profile must be 'general'"
            )
        if language not in {"zh", "en", "auto"}:
            raise ValueError("initial presentation language must be 'zh', 'en', or 'auto' (chosen later in config.json)")
        missing = [str(path) for path in attachments if not path.is_file()]
        if missing:
            raise ValueError(f"attachment files do not exist: {missing}")
        intents = tuple(
            (str(value).strip() or None) if value is not None else None
            for value in self.attachment_intents
        )
        if intents and len(intents) != len(attachments):
            raise ValueError(
                "attachment_intents must align one-to-one with attachments"
            )
        page_count_hint = (
            None if self.page_count_hint is None else int(self.page_count_hint)
        )
        if page_count_hint is not None and page_count_hint < 1:
            raise ValueError("page_count_hint must be positive")
        revision_request = (
            None
            if self.revision_request is None
            else MappingProxyType(copy.deepcopy(dict(self.revision_request)))
        )
        object.__setattr__(self, "authoring_profile", profile)
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "attachments", attachments)
        object.__setattr__(self, "attachment_intents", intents)
        object.__setattr__(self, "page_count_hint", page_count_hint)
        object.__setattr__(self, "revision_request", revision_request)
        object.__setattr__(self, "output_workspace", workspace)

    @property
    def profile(self) -> RunProfile:
        """返回当前模式对应的上下文与轨迹策略。"""
        return RUN_PROFILES[self.run_mode]

    @classmethod
    def from_inputs(
        cls,
        *,
        raw_user_query: str,
        output_workspace: str | Path,
        attachments: Sequence[str | Path] = (),
        attachment_intents: Sequence[str | None] | None = None,
        run_mode: str | RunMode | None = None,
        authoring_profile: str | None = None,
        page_count_hint: int | None = None,
        revision_request: Mapping[str, object] | None = None,
        language: str = "auto",
        environment: Mapping[str, str] | None = None,
    ) -> "RunSpec":
        """把显式入口参数编译成一个没有默认画布的 ``RunSpec``。"""
        env = environment if environment is not None else os.environ
        mode = resolve_run_mode(run_mode or _text(env, "MURAL_RUN_MODE", "inference"))
        requested_profile = authoring_profile or _text(
            env, "MURAL_AUTHORING_PROFILE", "auto"
        )
        profile = resolve_authoring_profile(requested_profile)
        delivery_language = (language if str(language).strip().lower() != "auto"
                             else _text(env, "MURAL_DELIVERY_LANGUAGE", "auto"))
        return cls(
            raw_user_query=raw_user_query,
            attachments=tuple(Path(value) for value in attachments),
            attachment_intents=(
                tuple(attachment_intents)
                if attachment_intents is not None
                else ()
            ),
            run_mode=mode,
            authoring_profile=profile,
            page_count_hint=page_count_hint,
            revision_request=revision_request,
            language=delivery_language,
            provider=ProviderSettings.from_environment(env),
            output_workspace=Path(output_workspace),
        )

    def to_manifest(self) -> dict[str, object]:
        """生成可审计且不含凭证的运行输入清单。"""
        return {
            "raw_user_query": self.raw_user_query,
            "attachments": [str(path) for path in self.attachments],
            "attachment_intents": list(self.attachment_intents),
            "run_mode": self.run_mode.value,
            "authoring_profile": self.authoring_profile,
            "page_count_hint": self.page_count_hint,
            "revision_request": (
                None
                if self.revision_request is None
                else dict(self.revision_request)
            ),
            "language": self.language,
            "provider": self.provider.to_manifest(),
            "output_workspace": str(self.output_workspace),
        }


def read_environment_file(path: str | Path) -> dict[str, str]:
    """解析 dotenv 风格文件，但不修改 ``os.environ``。"""
    source = Path(path)
    if not source.is_file():
        return {}
    values: dict[str, str] = {}
    for line_number, raw in enumerate(
        source.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment line {line_number} in {source}")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"empty environment key on line {line_number} in {source}")
        values[key] = value.strip().strip('"').strip("'")
    return values


def merge_environment(
    file_values: Mapping[str, str],
    launch_values: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """按启动值优先于文件值合并，且不修改两个输入。"""
    merged = {str(key): str(value) for key, value in file_values.items()}
    source = launch_values if launch_values is not None else os.environ
    merged.update({str(key): str(value) for key, value in source.items()})
    return merged


def resolve_run_mode(value: str | RunMode) -> RunMode:
    """把字符串运行模式转换成受限的 ``RunMode``。"""
    if isinstance(value, RunMode):
        return value
    normalized = str(value).strip().lower()
    try:
        return RunMode(normalized)
    except ValueError as exc:
        raise ValueError(
            f"unsupported MURAL run mode {normalized!r}; expected inference or synthesis"
        ) from exc


def normalize_language(value: object) -> str:
    """把显式语言值规范为 ``zh``、``en`` 或尚未指定的空值。"""
    raw = str(value or "").strip().lower()
    compact = re.sub(r"[^a-z\u3400-\u9fff]", "", raw)
    if compact in {
        "zh",
        "zhcn",
        "cn",
        "chinese",
        "中文",
        "汉语",
        "漢語",
        "简体中文",
        "中文简体",
    }:
        return "zh"
    if compact in {"en", "enus", "english", "英文", "英语", "英語"}:
        return "en"
    return ""


def resolve_authoring_profile(requested_profile: object = "auto") -> str:
    """只解析调用方配置；用户语义与 Research 决策交由模型。"""
    requested = str(requested_profile or "auto").strip().lower().replace("_", "-")
    requested = {
        "": "auto",
        "default": "auto",
        "open": "general",
        "creative": "general",
        "material": "general",
        "material-grounded": "general",
        "closed-material": "general",
    }.get(requested, requested)
    if requested not in {"auto", *AUTHORING_PROFILES}:
        raise ValueError(
            "authoring_profile must be auto or general"
        )
    return "general" if requested == "auto" else requested


def _text(environment: Mapping[str, str], name: str, default: str = "") -> str:
    """读取并去除一个环境字符串两端的空白。"""
    return str(environment.get(name, default) or "").strip()


def _boolean(
    environment: Mapping[str, str], name: str, default: bool
) -> bool:
    """读取常见真假写法，非法值直接报错。"""
    raw = _text(environment, name)
    if not raw:
        return default
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value, got {raw!r}")


def _positive_int(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    """读取严格大于零的整数配置。"""
    value = int(_text(environment, name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _nonnegative_int(
    environment: Mapping[str, str], name: str, default: int
) -> int:
    """读取允许为零的整数配置。"""
    value = int(_text(environment, name, str(default)))
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _float(
    environment: Mapping[str, str], name: str, default: float
) -> float:
    """读取浮点数配置并保留字段名用于错误定位。"""
    try:
        return float(_text(environment, name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


__all__ = [
    "API_FORMATS",
    "ProviderSettings",
    "maintenance_reasoning",
    "provider_request_budget_s",
    "RUN_PROFILES",
    "RunMode",
    "RunProfile",
    "RunSpec",
    "merge_environment",
    "read_environment_file",
    "resolve_api_format",
    "resolve_run_mode",
]
