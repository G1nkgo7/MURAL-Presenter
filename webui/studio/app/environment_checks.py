"""按需调用当前 Harness 的 EnvironmentDoctor，并展示其实际检查范围。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import uuid

from . import engine, service_config
from .db import DATA_DIR


REPORT_DIR = DATA_DIR / "environment-checks"
_CHECK_LOCK = asyncio.Lock()
_TIMEOUT_S = max(30, int(os.environ.get("STUDIO_ENVIRONMENT_CHECK_TIMEOUT", "180")))


def _paths(user_id: int) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    prefix = REPORT_DIR / f"user-{int(user_id)}"
    return prefix.with_suffix(".doctor.json"), prefix.with_suffix(".json")


def _same_interpreter(first: str, second: str) -> bool:
    left = Path(first).expanduser()
    right = Path(second).expanduser()
    try:
        return left.samefile(right)
    except (FileNotFoundError, OSError):
        return left.absolute() == right.absolute()


def _public_report(
    report: dict,
    *,
    model_key: str,
    model_label: str,
    runtime_python: str = "",
    normalizer_python: str = "",
) -> dict:
    checks = []
    for item in report.get("checks") or []:
        if not isinstance(item, dict):
            continue
        checks.append({
            "id": str(item.get("name") or "unknown"),
            "ok": item.get("ok") is True,
            "required": item.get("required") is True,
            "detail": str(item.get("detail") or ""),
            "latency_ms": int(item.get("latency_ms") or 0),
        })
    reported_environment = report.get("environment") or {}
    if not runtime_python and isinstance(reported_environment, dict):
        runtime_python = str(reported_environment.get("python") or "")
    normalizer_python = normalizer_python or runtime_python
    runtime_aligned = bool(
        runtime_python
        and normalizer_python
        and _same_interpreter(runtime_python, normalizer_python)
        and reported_environment.get("runtime_aligned") is not False
    )
    if (runtime_python or normalizer_python) and not any(c["id"] == "formal_inference_runtime" for c in checks):
        checks.insert(0, {
            "id": "formal_inference_runtime",
            "ok": runtime_aligned,
            "required": True,
            "detail": f"engine={runtime_python}; attachments/OCR={normalizer_python}",
            "latency_ms": 0,
        })
    ready = report.get("status") == "pass" and runtime_aligned
    warnings = [f"{item['id']}: {item['detail']}" for item in checks if not item["required"] and not item["ok"]]
    status = ("degraded" if warnings else "ready") if ready else "blocked"
    if report.get("checked") is True and not runtime_aligned:
        status = "blocked"
    fatal_errors = [f"{item['id']}: {item['detail']}" for item in checks if item["required"] and not item["ok"]]
    if report.get("checked") is True and not runtime_aligned:
        fatal_errors.append(
            "formal_inference_runtime: engine and attachment/OCR interpreters differ"
        )
    return {
        "checked": report.get("checked") is True,
        "ready": ready,
        "status": status,
        "mode": str(report.get("mode") or "local"),
        "model": model_key,
        "model_label": model_label,
        "checked_epoch": float(report.get("checked_epoch") or 0),
        "duration_ms": int(report.get("duration_ms") or 0),
        "live_probes": report.get("live_probes") is True,
        "runtime_python": runtime_python,
        "normalizer_python": normalizer_python,
        "runtime_aligned": runtime_aligned,
        "checks": checks,
        "fatal_errors": fatal_errors,
        "warnings": warnings,
    }


def _save_public(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def latest(user_id: int) -> dict:
    _, public_path = _paths(user_id)
    try:
        payload = json.loads(public_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return {"checked": False, "ready": False, "status": "not_run", "checks": []}
    return payload if isinstance(payload, dict) else {
        "checked": False,
        "ready": False,
        "status": "not_run",
        "checks": [],
    }


async def run(
    *,
    user_id: int,
    model_key: str,
    model_label: str,
    pipeline_key: str,
    skill_key: str,
    model_config: dict | None = None,
) -> dict:
    """Run the paired Harness Doctor once and return a credential-free result."""
    raw_path, public_path = _paths(user_id)
    attempt_path = raw_path.with_name(
        f".{raw_path.name}.{os.getpid()}.{uuid.uuid4().hex}.attempt"
    )
    pipeline = engine.PIPELINES[pipeline_key]
    doctor_root = Path(pipeline["path"])
    harness_entry = doctor_root / pipeline["entry"]
    python = engine._engine_python()
    if not python or not harness_entry.is_file():
        raise RuntimeError("环境检验所需的 Harness 或 Python 运行环境不存在")

    drop = {
        "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
        "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "IMAGE_API_KEY", "IMAGE_BASE_URL", "IMAGE_MODEL", "IMAGE_PROVIDER",
        "SERPER_API_KEY", "SERPER_BASE_URL",
        "STUDIO_MAX_PER_MODEL", "STUDIO_MAX_CONCURRENT", "MURAL_RESOURCE_SLOTS_DIR",
        "MODEL_CONCURRENCY", "SLIDE_CONCURRENCY", "RENDER_CONCURRENCY", "PPT_RENDER_CONCURRENCY",
    }
    child_env = {key: value for key, value in os.environ.items()
                 if key not in drop and not key.startswith("MURAL_MINERU_")}
    child_env.update(engine.selection_env(
        model_key,
        pipeline_key,
        skill_key,
        model_config=model_config,
    ))
    child_env.update(service_config.system_runtime_env())
    child_env.update(service_config.runtime_env(user_id))
    child_env = engine.render_env(child_env)
    runtime_python = str(child_env.get("PPTAGENT_ENGINE_PYTHON") or python)
    normalizer_python = str(child_env.get("NORMALIZE_PY") or runtime_python)

    command = [
        runtime_python,
        str(harness_entry),
        "--doctor",
        "--json",
        str(attempt_path),
        "--force",
    ]
    async with _CHECK_LOCK:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(doctor_root),
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            attempt_path.unlink(missing_ok=True)
            raise RuntimeError(f"环境检验超过 {_TIMEOUT_S} 秒，已停止") from exc

    if process.returncode not in (0, 1):
        attempt_path.unlink(missing_ok=True)
        detail = output.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(
            f"环境检验进程失败（exit={process.returncode}）：{detail}"
        )
    try:
        report = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        attempt_path.unlink(missing_ok=True)
        detail = output.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(
            f"环境检验未生成有效报告（exit={process.returncode}）：{detail}"
        ) from exc
    if report.get("checked") is not True:
        attempt_path.unlink(missing_ok=True)
        detail = output.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"环境检验未完成：{detail}")
    os.replace(attempt_path, raw_path)

    payload = _public_report(
        report,
        model_key=model_key,
        model_label=model_label,
        runtime_python=runtime_python,
        normalizer_python=normalizer_python,
    )
    _save_public(public_path, payload)
    return payload
