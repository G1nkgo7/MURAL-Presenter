"""Per-user OpenAI-compatible multimodal model configurations.

API keys are encrypted at rest with a server-local Fernet key. Public payloads
never contain either the encrypted value or its plaintext form.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from .db import DATA_DIR
from .service_config import model_effort_capability

KEY_PREFIX = "custom:"
_KEY_FILE = DATA_DIR / ".custom-model.key"
_MODEL_KEY_RE = re.compile(r"^custom:([1-9][0-9]*)$")


def key_for(model_id: int) -> str:
    return f"{KEY_PREFIX}{int(model_id)}"


def id_from_key(value: str | None) -> int | None:
    match = _MODEL_KEY_RE.fullmatch(str(value or ""))
    return int(match.group(1)) if match else None


def _fernet() -> Fernet:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        key = _KEY_FILE.read_bytes().strip()
    except FileNotFoundError:
        key = Fernet.generate_key()
        try:
            fd = os.open(_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = _KEY_FILE.read_bytes().strip()
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(key + b"\n")
    try:
        os.chmod(_KEY_FILE, 0o600)
    except OSError:
        pass
    return Fernet(key)


def encrypt_api_key(value: str) -> str:
    value = str(value or "").strip()
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii") if value else ""


def decrypt_api_key(value: str) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise RuntimeError("自定义模型 API Key 无法解密，请删除后重新添加") from exc


def normalize_base_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        raise ValueError("Base URL 必须是完整的 http:// 或 https:// 地址")
    if parts.username or parts.password or parts.fragment:
        raise ValueError("Base URL 不能包含账号密码或锚点")
    if parts.path.rstrip("/").endswith("/responses"):
        raise ValueError(
            "当前仅支持 OpenAI Chat Completions；Responses API 尚未实现"
        )
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), parts.query, ""))


def list_rows(con, user_id: int, *, active_only: bool = True):
    sql = "SELECT * FROM custom_models WHERE user_id = ?"
    params: list[object] = [user_id]
    if active_only:
        sql += " AND is_active = 1"
    sql += " ORDER BY id"
    return con.execute(sql, params).fetchall()


def get_owned(con, user_id: int, model_key: str, *, active_only: bool = True):
    model_id = id_from_key(model_key)
    if model_id is None:
        return None
    sql = "SELECT * FROM custom_models WHERE id = ? AND user_id = ?"
    params: list[object] = [model_id, user_id]
    if active_only:
        sql += " AND is_active = 1"
    return con.execute(sql, params).fetchone()


def create(con, user_id: int, display_name: str, model_id: str, base_url: str, api_key: str):
    display_name = str(display_name or "").strip()
    model_id = str(model_id or "").strip()
    if not (1 <= len(display_name) <= 80):
        raise ValueError("模型名称需为 1–80 个字符")
    if not (1 <= len(model_id) <= 180):
        raise ValueError("Model ID 需为 1–180 个字符")
    base_url = normalize_base_url(base_url)
    if len(base_url) > 500:
        raise ValueError("Base URL 过长")
    if len(api_key or "") > 4096:
        raise ValueError("API Key 过长")
    now = int(time.time())
    cur = con.execute(
        "INSERT INTO custom_models(user_id,display_name,model_id,base_url,api_key_enc,"
        "vision_enabled,is_active,created_at,updated_at) VALUES(?,?,?,?,?,1,1,?,?)",
        (user_id, display_name, model_id, base_url, encrypt_api_key(api_key), now, now),
    )
    con.commit()
    return get_owned(con, user_id, key_for(cur.lastrowid))


def delete(con, user_id: int, model_key: str) -> bool:
    model_id = id_from_key(model_key)
    if model_id is None:
        return False
    cur = con.execute(
        "UPDATE custom_models SET is_active = 0, updated_at = ? WHERE id = ? AND user_id = ?",
        (int(time.time()), model_id, user_id),
    )
    con.commit()
    return cur.rowcount > 0


def public_payload(row) -> dict:
    thinking = thinking_capability(row)
    vision = vision_capability(row)
    return {
        "key": key_for(row["id"]),
        "name": row["display_name"],
        "model_id": row["model_id"],
        "base_url": row["base_url"],
        "has_api_key": bool(row["api_key_enc"]),
        "multimodal": vision,
        "thinking_toggle": thinking["toggle"],
        "thinking_effort": thinking["effort"],
        "thinking_transport": thinking["transport"],
        "effort_levels": list(thinking["effort_levels"]),
        "effort_select": thinking["effort_select"],
    }


def vision_capability(row) -> bool:
    """DeepSeek V4 chat endpoints are text-only, regardless of UI legacy flags."""
    model_id = str(row["model_id"] or "").strip().lower()
    if model_id in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        return False
    return bool(row["vision_enabled"])


def thinking_capability(row) -> dict:
    """Return the explicit reasoning contract for known custom endpoints."""
    model_id = str(row["model_id"] or "").strip().lower()
    base_url = str(row["base_url"] or "").strip().lower()
    # Official V4 variants share the same OpenAI-compatible thinking contract.
    # This includes the native multimodal experimental endpoint used by the
    # MURAL paper profile; it must not be silently forced to thinking=false.
    deepseek_v4 = model_id in {"deepseek-v4-flash", "deepseek-v4-pro"} or (
        model_id.startswith("deepseek-v4-")
        and model_id.endswith("-vision-exp")
    )
    if base_url.endswith("/responses"):
        return {
            "mode": "none", "transport": "", "toggle": False,
            "effort": "", "effort_levels": (), "effort_select": False,
        }
    if deepseek_v4:
        return {
            "mode": "toggle",
            "transport": "deepseek",
            "toggle": True,
            "effort": "",
            "effort_levels": (),
            "effort_select": False,
        }
    if "qwen" in model_id or "qwq" in model_id:
        effort = model_effort_capability(model_id)
        return {
            "mode": "toggle",
            "transport": "chat_template_kwargs",
            "toggle": True,
            "effort": effort["default"],
            "effort_levels": effort["levels"],
            "effort_select": bool(effort["levels"]),
        }
    if model_id == "kimi-k3":
        return {
            "mode": "toggle",
            "transport": "chat_template_kwargs",
            "toggle": True,
            "effort": "",
            "effort_levels": (),
            "effort_select": False,
        }
    if model_id.startswith(("gpt-5", "o1", "o3", "o4")):
        return {
            "mode": "always",
            "transport": "reasoning_effort",
            "toggle": False,
            "effort": "medium",
            "effort_levels": ("low", "medium", "high"),
            "effort_select": True,
        }
    return {
        "mode": "none", "transport": "", "toggle": False,
        "effort": "", "effort_levels": (), "effort_select": False,
    }


def runtime_config(row) -> dict:
    thinking = thinking_capability(row)
    return {
        "label": row["display_name"],
        "backend": "openai",
        "engine_model": row["model_id"],
        "base_url": row["base_url"],
        "api_key": decrypt_api_key(row["api_key_enc"]),
        "slide_concurrency": 4,
        "thinking_mode": thinking["mode"],
        "thinking_transport": thinking["transport"],
        "thinking_effort": thinking["effort"],
        "thinking_effort_levels": thinking["effort_levels"],
        "multimodal": vision_capability(row),
        "custom": True,
    }
