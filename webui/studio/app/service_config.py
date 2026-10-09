"""Per-user external service configuration stored in one encrypted JSON file.

The public API never returns API key ciphertext or plaintext.  Runtime callers
receive a small environment/config mapping that can be injected into one job,
so one user's credentials never become process-global defaults for another.
"""
from __future__ import annotations

import json
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

from .db import DATA_DIR

CONFIG_VERSION = 5
CONFIG_DIR = DATA_DIR / "user_configs"
DEFAULT_MAX_TOKENS = 40960
DEFAULT_ORCHESTRATOR_MAX_TOKENS = 16384
DEFAULT_SLIDE_OWNER_MAX_TOKENS = 20000
DEFAULT_RESEARCH_OWNER_MAX_TOKENS = 12000
DEFAULT_IMAGE_OWNER_MAX_TOKENS = 8192
DEFAULT_CRITIC_MAX_TOKENS = 8192
DEFAULT_STATIC_MAX_TURNS = 240
DEFAULT_STATIC_SUBAGENT_MAX_TURNS = 240
DEFAULT_DYNAMIC_MAX_TURNS = 4096
# 默认非流式:opus-5 超长 adaptive thinking 下流式会从网关拿到畸形分块(空 thinking/空参
# tool),非流式返回完整消息更稳(大 max_tokens 的 SDK 10min 守卫由 harness 调用处显式
# timeout 绕过)。需要实时预览时用户可在生成设置里单独开流式。
DEFAULT_STREAMING_ENABLED = False
DEFAULT_TITLE_TASK_MODEL = (
    os.environ.get("STUDIO_TITLE_TASK_MODEL_KEY", "").strip()
    or os.environ.get("STUDIO_TASK_MODEL_KEY", "").strip()
    or "deployment-model"
)
IMAGE_PROVIDERS = {"openai_images", "sensenova_u1"}
DEFAULT_IMAGE_PROVIDER = "openai_images"
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_runtime_spec = importlib.util.spec_from_file_location(
    "mural_studio_runtime_config",
    REPOSITORY_ROOT / "harnesses/mural-presenter-v1/runtime/config.py",
)
_runtime_config = importlib.util.module_from_spec(_runtime_spec)
sys.modules[_runtime_spec.name] = _runtime_config
_runtime_spec.loader.exec_module(_runtime_config)
load_runtime_config = _runtime_config.load_runtime_config
EFFORT_LEVELS = _runtime_config.EFFORT_LEVELS
model_effort_capability = _runtime_config.model_effort_capability
resolve_api_format = _runtime_config.resolve_api_format
validate_reasoning_effort = _runtime_config.validate_reasoning_effort
PROVIDER_ENV_PATH = Path(os.environ.get(
    "MURAL_SECRETS_ENV_PATH", REPOSITORY_ROOT / ".secrets" / ".env"
)).expanduser().resolve()


def read_env_file(path: str | Path) -> dict[str, str]:
    """读取部署配置，不把凭证合并进进程全局环境。"""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    values = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, candidate = line.split("=", 1)
        values[key.strip()] = candidate.strip().strip('"').strip("'")
    return values


def system_runtime_env() -> dict[str, str]:
    """系统 Provider 只读共享文件；用户显式配置仅覆盖自己的任务。"""
    shared = read_env_file(PROVIDER_ENV_PATH)
    env: dict[str, str] = {}
    image_url = shared.get("IMAGE_BASE_URL", "")
    image_key = shared.get("IMAGE_API_KEY", "")
    image_model = shared.get("IMAGE_MODEL", "")
    image_provider = shared.get("IMAGE_PROVIDER") or DEFAULT_IMAGE_PROVIDER
    if image_provider not in IMAGE_PROVIDERS:
        image_provider = DEFAULT_IMAGE_PROVIDER
    if image_url:
        env["IMAGE_BASE_URL"] = image_url.rstrip("/")
    if image_key:
        env["IMAGE_API_KEY"] = image_key
    if image_model:
        env["IMAGE_MODEL"] = image_model
    if image_url or image_key or image_model:
        env["IMAGE_PROVIDER"] = image_provider

    search_url = shared.get("SERPER_BASE_URL", "")
    search_key = shared.get("SERPER_API_KEY", "")
    if search_url:
        env["SERPER_BASE_URL"] = search_url.rstrip("/")
    if search_key:
        env["SERPER_API_KEY"] = search_key
    return env


def _path(user_id: int) -> Path:
    return CONFIG_DIR / f"{int(user_id)}.json"


def _blank() -> dict:
    return {
        "version": CONFIG_VERSION,
        "image_generation": {
            "enabled": False,
            "provider": DEFAULT_IMAGE_PROVIDER,
            "base_url": "",
            "model": "",
            "api_key_enc": "",
        },
        "web_search": {
            "enabled": False,
            "base_url": "",
            "api_key_enc": "",
        },
        "generation": {
            "max_tokens": DEFAULT_MAX_TOKENS,
            "streaming_enabled": DEFAULT_STREAMING_ENABLED,
            "enable_visual_critic": False,
            "title_task_model": DEFAULT_TITLE_TASK_MODEL,
            # Keep static and dynamic runtimes aligned by default.  An explicit
            # zero remains available as an expert escape hatch that restores
            # each static Harness's own reviewed fallback.
            "static_max_turns": DEFAULT_STATIC_MAX_TURNS,
            # Give every delegated static child Agent one consistent default;
            # 0 remains an explicit expert override that restores the selected
            # Harness's own role-specific limit.
            "static_subagent_max_turns": DEFAULT_STATIC_SUBAGENT_MAX_TURNS,
            "dynamic_max_turns": DEFAULT_DYNAMIC_MAX_TURNS,
        },
    }


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def load(user_id: int) -> dict:
    data = _blank()
    try:
        raw = json.loads(_path(user_id).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        return data
    if not isinstance(raw, dict):
        return data
    for name in ("image_generation", "web_search", "generation"):
        current = data[name]
        stored = _section(raw, name)
        for key in current:
            if key in stored:
                current[key] = stored[key]
    return data


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _normalize_optional_url(value: str) -> str:
    from . import custom_models
    value = str(value or "").strip()
    return custom_models.normalize_base_url(value) if value else ""


def _bounded_int(value, *, label: str, minimum: int, maximum: int, allow_zero: bool = False) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 必须是整数") from exc
    if allow_zero and parsed == 0:
        return 0
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{label} 必须在 {minimum}–{maximum} 之间")
    return parsed


def update(
    user_id: int,
    *,
    image_enabled: bool,
    image_provider: str = DEFAULT_IMAGE_PROVIDER,
    image_base_url: str,
    image_model: str,
    image_api_key: str | None,
    clear_image_api_key: bool,
    search_enabled: bool,
    search_base_url: str,
    search_api_key: str | None,
    clear_search_api_key: bool,
    max_tokens: int | None = None,
    streaming_enabled: bool | None = None,
    enable_visual_critic: bool | None = None,
    static_max_turns: int | None = None,
    static_subagent_max_turns: int | None = None,
    dynamic_max_turns: int | None = None,
    title_task_model: str | None = None,
) -> dict:
    from . import custom_models
    data = load(user_id)
    image = data["image_generation"]
    search = data["web_search"]
    generation = data["generation"]

    image_provider = str(image_provider or DEFAULT_IMAGE_PROVIDER).strip().lower()
    if image_provider not in IMAGE_PROVIDERS:
        raise ValueError("不支持的生图服务类型")
    image_url = _normalize_optional_url(image_base_url)
    search_url = _normalize_optional_url(search_base_url)
    image_model = str(image_model or "").strip()
    if len(image_url) > 500 or len(search_url) > 500:
        raise ValueError("服务 URL 过长")
    if len(image_model) > 180:
        raise ValueError("生图模型名称过长")
    if image_enabled and (not image_url or not image_model):
        raise ValueError("启用个人生图服务时，需要填写 Base URL 和模型名称")
    if search_enabled and not search_url:
        raise ValueError("启用个人搜索服务时，需要填写 Base URL")

    if max_tokens is not None:
        generation["max_tokens"] = _bounded_int(
            max_tokens, label="Max Tokens", minimum=2000, maximum=65536
        )
    if streaming_enabled is not None:
        generation["streaming_enabled"] = bool(streaming_enabled)
    if enable_visual_critic is not None:
        generation["enable_visual_critic"] = bool(enable_visual_critic)
    if static_max_turns is not None:
        generation["static_max_turns"] = _bounded_int(
            static_max_turns, label="静态主 Agent 最大轮次", minimum=1, maximum=8192, allow_zero=True
        )
    if static_subagent_max_turns is not None:
        generation["static_subagent_max_turns"] = _bounded_int(
            static_subagent_max_turns,
            label="静态子 Agent 最大轮次",
            minimum=1,
            maximum=8192,
            allow_zero=True,
        )
    if dynamic_max_turns is not None:
        generation["dynamic_max_turns"] = _bounded_int(
            dynamic_max_turns, label="动态最大轮次", minimum=1, maximum=16384
        )
    if title_task_model is not None:
        task_model = str(title_task_model or "").strip()
        if not task_model or len(task_model) > 180:
            raise ValueError("侧栏标题 Task Model 无效")
        generation["title_task_model"] = task_model

    image.update({
        "enabled": bool(image_enabled),
        "provider": image_provider,
        "base_url": image_url,
        "model": image_model,
    })
    search.update({"enabled": bool(search_enabled), "base_url": search_url})

    if clear_image_api_key:
        image["api_key_enc"] = ""
    elif image_api_key is not None and image_api_key.strip():
        if len(image_api_key) > 4096:
            raise ValueError("生图 API Key 过长")
        image["api_key_enc"] = custom_models.encrypt_api_key(image_api_key)
    if clear_search_api_key:
        search["api_key_enc"] = ""
    elif search_api_key is not None and search_api_key.strip():
        if len(search_api_key) > 4096:
            raise ValueError("搜索 API Key 过长")
        search["api_key_enc"] = custom_models.encrypt_api_key(search_api_key)

    _atomic_write(_path(user_id), data)
    return data


def public_payload(user_id: int) -> dict:
    data = load(user_id)
    system = system_runtime_env()
    image = data["image_generation"]
    search = data["web_search"]
    personal_image_available = bool(
        image.get("enabled") and image.get("base_url")
        and image.get("model") and image.get("api_key_enc")
    )
    system_image_available = bool(
        system.get("IMAGE_BASE_URL") and system.get("IMAGE_API_KEY")
        and system.get("IMAGE_MODEL")
    )
    personal_search_available = bool(
        search.get("enabled") and search.get("base_url") and search.get("api_key_enc")
    )
    system_search_available = bool(
        system.get("SERPER_BASE_URL") and system.get("SERPER_API_KEY")
    )
    return {
        "image_generation": {
            "enabled": bool(image.get("enabled")),
            "provider": str(image.get("provider") or DEFAULT_IMAGE_PROVIDER),
            "base_url": str(image.get("base_url") or ""),
            "model": str(image.get("model") or ""),
            "has_api_key": bool(image.get("api_key_enc")),
            "available": personal_image_available or system_image_available,
            "source": "personal" if personal_image_available else ("deployment" if system_image_available else "none"),
        },
        "web_search": {
            "enabled": bool(search.get("enabled")),
            "base_url": str(search.get("base_url") or ""),
            "has_api_key": bool(search.get("api_key_enc")),
            "available": personal_search_available or system_search_available,
            "source": "personal" if personal_search_available else ("deployment" if system_search_available else "none"),
        },
        "generation": generation_limits(user_id),
    }


def generation_limits(user_id: int) -> dict[str, int | bool | str]:
    """Return validated, non-secret generation limits for one account."""
    generation = load(user_id)["generation"]
    return {
        "max_tokens": int(generation.get("max_tokens") or DEFAULT_MAX_TOKENS),
        "streaming_enabled": bool(
            generation.get("streaming_enabled", DEFAULT_STREAMING_ENABLED)
        ),
        "enable_visual_critic": bool(generation.get("enable_visual_critic", False)),
        "title_task_model": str(
            generation.get("title_task_model") or DEFAULT_TITLE_TASK_MODEL
        ),
        "static_max_turns": int(
            generation.get("static_max_turns")
            if generation.get("static_max_turns") is not None
            else DEFAULT_STATIC_MAX_TURNS
        ),
        "static_subagent_max_turns": int(
            generation.get("static_subagent_max_turns")
            if generation.get("static_subagent_max_turns") is not None
            else DEFAULT_STATIC_SUBAGENT_MAX_TURNS
        ),
        "dynamic_max_turns": int(
            generation.get("dynamic_max_turns") or DEFAULT_DYNAMIC_MAX_TURNS
        ),
    }


def role_token_env(max_tokens: int | str) -> dict[str, str]:
    """Static v1 role budgets."""
    budget = max(1, int(max_tokens))
    return {
        "ORCHESTRATOR_MAX_TOKENS": str(min(budget, DEFAULT_ORCHESTRATOR_MAX_TOKENS)),
        "SLIDE_OWNER_MAX_TOKENS": str(min(budget, DEFAULT_SLIDE_OWNER_MAX_TOKENS)),
        "RESEARCH_OWNER_MAX_TOKENS": str(min(budget, DEFAULT_RESEARCH_OWNER_MAX_TOKENS)),
        "IMAGE_OWNER_MAX_TOKENS": str(min(budget, DEFAULT_IMAGE_OWNER_MAX_TOKENS)),
        "CRITIC_MAX_TOKENS": str(min(budget, DEFAULT_CRITIC_MAX_TOKENS)),
    }


def title_task_model_key(user_id: int) -> str:
    """Return the account-level model used only for WebUI navigation titles."""
    return str(
        generation_limits(user_id).get("title_task_model")
        or DEFAULT_TITLE_TASK_MODEL
    )


def runtime_payload(user_id: int) -> dict:
    """Return decrypted per-job values.  Empty sections mean system fallback."""
    from . import custom_models
    data = load(user_id)
    out: dict[str, dict] = {"generation": generation_limits(user_id)}
    image = data["image_generation"]
    if image.get("enabled"):
        out["image_generation"] = {
            "provider": str(image.get("provider") or DEFAULT_IMAGE_PROVIDER),
            "base_url": str(image.get("base_url") or ""),
            "model": str(image.get("model") or ""),
            "api_key": custom_models.decrypt_api_key(str(image.get("api_key_enc") or "")),
        }
    search = data["web_search"]
    if search.get("enabled"):
        out["web_search"] = {
            "base_url": str(search.get("base_url") or ""),
            "api_key": custom_models.decrypt_api_key(str(search.get("api_key_enc") or "")),
        }
    return out


def runtime_env(user_id: int) -> dict[str, str]:
    payload = runtime_payload(user_id)
    env: dict[str, str] = {}
    generation = payload["generation"]
    token_budget = str(generation["max_tokens"])
    env.update({
        "STUDIO_AGENT_MAX_TOKENS": token_budget,
        "MAX_TOKENS": token_budget,
        "SUBAGENT_MAX_TOKENS": token_budget,
        "CLEAN_MAX_TOKENS": token_budget,
        "STUDIO_MAX_TOKENS": token_budget,
        "AGENT_MAX_TOKENS": token_budget,
        "OWNER_MAX_TOKENS": token_budget,
        "STUDIO_MAX_TURNS": str(generation["dynamic_max_turns"]),
        "MODEL_STREAMING": "1" if generation["streaming_enabled"] else "0",
    })
    env.update(role_token_env(token_budget))
    if generation["static_max_turns"]:
        turns = str(generation["static_max_turns"])
        env.update({
            "MAX_TURNS": turns,
            "CLEAN_MAX_TURNS": turns,
        })
    if generation["static_subagent_max_turns"]:
        child_turns = str(generation["static_subagent_max_turns"])
        env.update({
            "SUBAGENT_MAX_TURNS": child_turns,
            "CLEAN_CHILD_MAX_TURNS": child_turns,
            "OWNER_MAX_TURNS": child_turns,
        })
    image = payload.get("image_generation")
    if image:
        env.update({
            "IMAGE_PROVIDER": image["provider"],
            "IMAGE_BASE_URL": image["base_url"],
            "IMAGE_API_KEY": image["api_key"],
            "IMAGE_MODEL": image["model"],
        })
    search = payload.get("web_search")
    if search:
        env.update({
            "SERPER_BASE_URL": search["base_url"],
            "SERPER_API_KEY": search["api_key"],
        })
    return env
