"""指定 PresentBench 案例的薄接线：正式批次制作、按需 PDF、独立官方评审。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BENCH = Path(__file__).resolve().parents[1]
REPO = BENCH.parents[1]
HARNESS = REPO / "harnesses/mural-presenter-v1"
SOURCE = Path("/mnt/afs/gaozhenxi/pptagent_static/benchmark/PresentBench")
JUDGE_PYTHON = REPO / "webui/inference/.venv/bin/python"
ENDPOINTS = (
    ("http://10.119.22.194:8000/v1", "qwen3.8-27b", True),
    ("http://10.119.22.198:8000/v1", "qwen3.8-27b", True),
    ("http://10.119.22.209:8000/v1", "qwen3.8-27b", True),
    ("http://10.119.19.159:8000/v1", "Qwen3.8-27B", False),
)
PRESENTBENCH_AUTHORING_PROFILE = "general"
AUTHORING_POLICY = Path(__file__).with_name("authoring-policy.md")
sys.path.insert(0, str(HARNESS))

from runtime.batch import BatchArgs, _atomic_json, make_sample_id, run_batch
from runtime.config import ProviderSettings, RunMode, read_environment_file


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preserve(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != text:
            raise ValueError(f"Benchmark input changed; use a new batch: {path}")
    else:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(text)


def prepare(args):
    from runpy import run_path
    authoring_policy = AUTHORING_POLICY.read_text(encoding="utf-8").strip()
    preserve(args.output / "inputs/authoring-policy.md", authoring_policy + "\n")
    find_materials = run_path(str(BENCH / "utils/material_utils.py"))["find_material_files"]
    cases = [line.strip() for line in args.cases_file.read_text().splitlines() if line.strip()]
    if not cases or len(cases) != len(set(cases)):
        raise ValueError("Case selection is empty or contains duplicates")
    rows, metadata = [], []
    for index, case in enumerate(cases):
        folder = (args.data_root / case).resolve()
        if not folder.is_relative_to(args.data_root.resolve()):
            raise ValueError(f"Case escapes dataset: {case}")
        instructions = folder / "generation_task/instructions.md"
        materials = find_materials(folder)
        if not materials:
            raise ValueError(f"Case has no material: {case}")
        endpoint, model, token_ids = ENDPOINTS[index % len(ENDPOINTS)]
        qid = f"pb{index + 1:02d}"
        query = instructions.read_text(encoding="utf-8") + "\n\n" + authoring_policy
        rows.append({"query_id": qid, "query": query,
                     "attachments": [{"path": str(path)} for path in materials]})
        metadata.append({"query_id": qid, "case": case, "endpoint": endpoint,
            "model": model, "return_token_ids": token_ids,
            "instructions_sha256": sha(instructions),
            "generation_query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
            "materials": [{"path": str(path), "sha256": sha(path)} for path in materials]})
    preserve(args.output / "inputs/queries.jsonl", "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    report = {"batch": args.batch, "release": str(args.release), "mode": "synthesis",
        "authoring_profile": PRESENTBENCH_AUTHORING_PROFILE,
        "thinking": True, "effort": "xhigh", "streaming": True,
        "cases_file": str(args.cases_file), "cases_sha256": sha(args.cases_file),
        "data_root": str(args.data_root), "cases": metadata,
        "judge_model": "gemini-3-flash-preview", "judge_temperature": 0,
        "authoring_policy_sha256": sha(AUTHORING_POLICY),
        "judge_prompts_in_generation": False, "training_dataset_member": False}
    preserve(args.output / "inputs/preparation.json", json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return rows, metadata


def environment(args):
    for key, value in read_environment_file(REPO / "webui/.env").items():
        os.environ.setdefault(key, value)
    sys.path.insert(0, str(REPO / "webui/studio"))
    from app import engine, service_config
    env = {**read_environment_file(HARNESS / "runtime.env"), **os.environ,
           **service_config.system_runtime_env(), **engine.external_visual_critic_env(True, "synthesis")}
    for key in ("MODEL_API_KEYS", "MODEL_API_KEY_FALLBACKS", "MODEL_POOL_ENV_PATH", "MODEL_CHANNEL_LABELS"):
        env.pop(key, None)
    env.update(MODEL_API_FORMAT="openai_chat_compatible", MODEL_API_KEY="EMPTY",
        MODEL=ENDPOINTS[0][1], MODEL_BASE_URL=ENDPOINTS[0][0], MODEL_THINKING="1",
        MODEL_REASONING_EFFORT="xhigh", MODEL_REASONING_TRANSPORT="chat_template_kwargs",
        MODEL_STREAMING="1", MODEL_RETURN_TOKEN_IDS="1", MODEL_TIMEOUT_S="0",
        MURAL_RELEASE_MANIFEST=str(args.release), MURAL_RELEASE_POLICY="production",
        MURAL_ALLOW_DIRTY_RELEASE="0", CLEAN_ARTIFACT_ROOT=str(args.output),
        CLEAN_RUNS_DIR=str(args.output / "runs"), CLEAN_LOGS_DIR=str(args.output / "logs"),
        PPTAGENT_ENGINE_PYTHON=str(REPO / "webui/inference/.venv/bin/python"), PYTHONDONTWRITEBYTECODE="1")
    scratch = args.output / "tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    env.update(dict.fromkeys(("TMPDIR", "TMP", "TEMP"), str(scratch)))
    return engine.render_env(env)


def judge_case(args, item, workspace, env):
    from export.delivery import export_on_demand
    result_dir = args.output / "results" / item["case"] / "generation_task/results"
    result_dir.mkdir(parents=True, exist_ok=True)
    status_path = result_dir / "evaluation.json"
    started = time.time()
    status = {"case": item["case"], "workspace": str(workspace), "started_at": started, "status": "exporting"}
    _atomic_json(status_path, status)
    try:
        record = export_on_demand(workspace, "pdf")
        pdf = workspace / record["path"]
        target = result_dir / "slides.pdf"
        if target.exists() and sha(target) != sha(pdf):
            raise ValueError("Judged PDF differs; use a separate evaluation directory")
        if not target.exists():
            shutil.copyfile(pdf, target)
        status.update(status="judging", pdf_sha256=sha(target), export=record)
        _atomic_json(status_path, status)
        folder = args.data_root / item["case"]
        domain = args.data_root / Path(item["case"]).parts[0]
        secret = read_environment_file(REPO / ".secrets/.env")
        judge_env = {**env, "GENAI_API_KEY": secret["GEMINI_API_KEY"],
                     "GENAI_BASE_URL": "https://tokenhub.sensetime.com"}
        # 独立子进程使用基准自己的运行环境；不让评审输入进入创作轨迹。
        judge_env.pop("PYTHONPATH", None)
        judge_env.pop("VIRTUAL_ENV", None)
        command = [str(JUDGE_PYTHON), str(BENCH / "judge.py"), "--api_type", "gemini_inline",
            "--model", "gemini-3-flash-preview", "--temperature", "0", "--retry", "5",
            "--slides", str(target), "--material", *[row["path"] for row in item["materials"]],
            "--judge_prompt", str(folder / "generation_task/judge_prompt.json"),
            "--common_judge_prompt", str(domain / "common_judge_prompt.json"),
            "--weights_path", str(domain / "judge_weights.yaml"), "--output_dir", str(result_dir)]
        with (result_dir / "judge.log").open("a", encoding="utf-8") as log:
            process = subprocess.run(command, cwd=BENCH, env=judge_env, stdout=log, stderr=subprocess.STDOUT)
        # 官方评审可能保留未答完的结果；有 score 文件仍需检查每项回执。
        sys.path.insert(0, str(BENCH))
        import yaml
        from utils.judge_utils import is_result_complete, load_judge_prompt
        reports = sorted(result_dir.glob("gemini-3-flash-preview_*.yaml"))
        reports = [path for path in reports if not path.name.endswith("_score.yaml")]
        if not reports:
            raise ValueError(f"Official judge returned no report (exit {process.returncode}); see judge.log")
        report = reports[-1]
        answers = yaml.safe_load(report.read_text())
        checklists = load_judge_prompt(str(folder / "generation_task/judge_prompt.json"),
                                      str(domain / "common_judge_prompt.json"), str(target))
        if not all(is_result_complete(checklist, answers.get(section)) for checklist, section in
                   zip(checklists, ("material_independent", "material_dependent"))):
            status["judge_errors"] = {
                f"{section}/{item_id}": {key: value for key, value in item.items()
                    if key in {"log", "error_type", "status_code", "retryable"}}
                for section in ("material_independent", "material_dependent")
                for group in (answers.get(section) or {}).values() if isinstance(group, dict)
                for item_id, item in group.items() if isinstance(item, dict) and not item.get("answer")
            }
            raise ValueError("Official judge has unanswered items; score is not final")
        if process.returncode:
            raise ValueError(f"Official judge exited {process.returncode}; see judge.log")
        score_path = report.with_name(report.stem + "_score.yaml")
        score = yaml.safe_load(score_path.read_text())
        status.update(status="completed", score=score["total"]["weighted_arithmetic_mean_percent"],
                      score_file=str(score_path), judge_file=str(report))
    except Exception as exc:
        status.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    status["elapsed_s"] = round(time.time() - started, 3)
    _atomic_json(status_path, status)
    print(json.dumps({key: status.get(key) for key in ("case", "status", "score", "error")}), flush=True)
    return status


def main():
    os.umask(0o022)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "judge"))
    parser.add_argument("--cases-file", type=Path, default=SOURCE / "results/small10_cases.txt")
    parser.add_argument("--data-root", type=Path, default=SOURCE / "data")
    parser.add_argument("--batch", default="presentbench-small10-qwen38-xhigh-r45-20260913")
    parser.add_argument("--release", type=Path, default=REPO / "configs/releases/mural-paper-v1-stable-20260913-r45.json")
    parser.add_argument("--workers", type=int, default=10)
    parser.add_argument("--judge-workers", type=int, default=2)
    parser.add_argument("--max-attempts", type=int, default=3)
    args = parser.parse_args()
    from runtime.batch import _identifier
    _identifier(args.batch)
    if min(args.workers, args.judge_workers, args.max_attempts) < 1:
        parser.error("Worker/attempt counts must be positive")
    args.output = REPO / "artifacts/batches" / args.batch
    rows, items = prepare(args)
    if args.action == "prepare":
        print(json.dumps({"cases": len(rows), "output": str(args.output), "routes": items}, ensure_ascii=False))
        return 0
    env = environment(args)
    # 浏览器及导出辅助程序读取进程环境；所有 Deck 的 Provider 路由仍由显式 env 传入。
    os.environ.update(env)
    from runtime.release_guard import verify_runtime_release
    release = verify_runtime_release("synthesis", manifest_path=args.release)
    from infer import _run_single_deck
    seen = {}
    mapping = {make_sample_id(args.batch, row, seen): item for row, item in zip(rows, items)}
    with ThreadPoolExecutor(max_workers=args.judge_workers) as reviewers:
        pending = {}
        def run_deck(spec, shared_env):
            item = mapping[spec.output_workspace.name]
            route = {**shared_env, "MODEL": item["model"], "MODEL_BASE_URL": item["endpoint"],
                     "MODEL_RETURN_TOKEN_IDS": "1" if item["return_token_ids"] else "0"}
            spec = replace(spec, provider=ProviderSettings.from_environment(route))
            outcome = _run_single_deck(spec, route, mode=RunMode.SYNTHESIS, release_verification=release)
            pending[spec.output_workspace.name] = reviewers.submit(judge_case, args, item, spec.output_workspace, route)
            return outcome
        if args.action == "run":
            run_batch(BatchArgs(queries=args.output / "inputs/queries.jsonl", batch=args.batch,
                workers=args.workers, max_attempts=args.max_attempts, resume=True,
                authoring_profile=PRESENTBENCH_AUTHORING_PROFILE),
                run_deck=run_deck, environment=env)
        for sid, item in mapping.items():
            workspace = args.output / "runs" / args.batch / sid
            result = workspace / "result.json"
            if sid not in pending and result.is_file() and json.loads(result.read_text()).get("status") == "completed":
                pending[sid] = reviewers.submit(judge_case, args, item, workspace, env)
        evaluated = [future.result() for future in pending.values()]
    complete = [row for row in evaluated if row["status"] == "completed"]
    report = {"case_count": len(items), "evaluated_count": len(complete), "cases": evaluated,
              "complete": len(complete) == len(items),
              "mean_score": sum(row["score"] for row in complete) / len(complete) if complete else None,
              "mean_denominator": len(complete), "judge_model": "gemini-3-flash-preview"}
    _atomic_json(args.output / "scores.json", report)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0 if report["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
