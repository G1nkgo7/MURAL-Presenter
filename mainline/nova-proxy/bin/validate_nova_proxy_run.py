#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


EXPECTED_GATE_COMMIT = "4a067c75e4db7ea5ba81c264a1dc0ddac24f411e"


def jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for index, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise RuntimeError(f"{path}:{index} is not a JSON object")
        rows.append(value)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    args = parser.parse_args()
    root = args.run_root.resolve()
    failures: list[str] = []

    provenance_path = root / "gate-runtime" / "source-provenance.json"
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"missing/unreadable Gate provenance: {exc}")
        provenance = {}
    if provenance.get("commit") != EXPECTED_GATE_COMMIT:
        failures.append(
            f"Gate commit={provenance.get('commit')!r}, expected {EXPECTED_GATE_COMMIT}"
        )
    if provenance.get("exported_from_git_archive") is not True:
        failures.append("Gate runtime was not exported from git archive")

    launch_profile_path = root / "launch_profile.json"
    readiness_path = root / "readiness.start.json"
    try:
        launch_profile = json.loads(launch_profile_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"missing/unreadable launch_profile.json: {exc}")
        launch_profile = {}
    contract = launch_profile.get("contract") if isinstance(launch_profile, dict) else {}
    expected_contract = {
        "agent_payload_format": "anthropic",
        "agent_prompt_protocol": "anthropic_native",
        "vision_backend": "agent",
        "thinking_policy": "force_on",
        "anthropic_agent_thinking_gate": "visible_signed",
        "builtin_trace_format": "natural",
        "strict_tool_output": True,
        "internal_trace_enabled": True,
        "agent_vision_input_enabled": False,
    }
    for key, value in expected_contract.items():
        if not isinstance(contract, dict) or contract.get(key) != value:
            failures.append(
                f"launch profile contract {key}={contract.get(key) if isinstance(contract, dict) else None!r}, "
                f"expected {value!r}"
            )
    try:
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        failures.append(f"missing/unreadable readiness.start.json: {exc}")
        readiness = {}
    if readiness.get("ok") is not True:
        failures.append(f"readiness probe did not pass: {readiness.get('error')!r}")

    trace_counts: dict[str, int] = {}
    for name in (
        "user_side_requests.jsonl",
        "agent_model_requests.jsonl",
        "vision_model_requests.jsonl",
    ):
        path = root / "proxy_traces" / name
        try:
            trace_counts[name] = len(jsonl(path))
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name} missing/unreadable: {exc}")
            trace_counts[name] = 0
    for name, count in trace_counts.items():
        if count < 1:
            failures.append(f"{name} contains no completed requests")

    attempt_counts: Counter[str] = Counter()
    selected_aux_ok = 0
    task_count = 0
    for task_dir in sorted((root / "raw" / "tasks").glob("*")):
        if not task_dir.is_dir():
            continue
        task_count += 1
        path = task_dir / "attempts.jsonl"
        try:
            attempts = jsonl(path)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{task_dir.name}: attempts unreadable: {exc}")
            continue
        for row in attempts:
            kind = str(row.get("request_kind") or "unknown")
            status = str(row.get("status") or "unknown")
            selected = row.get("selected") is True
            attempt_counts[f"{kind}:{status}:{'selected' if selected else 'unselected'}"] += 1
            if selected and status != "ok":
                failures.append(
                    f"{task_dir.name}/{row.get('attempt_id')}: selected attempt is {status}"
                )
            if selected and status == "ok" and kind == "vision_analyze_aux":
                selected_aux_ok += 1
            for field in ("request", "response", "trace"):
                relative = str(row.get(field) or "")
                candidate = (task_dir / relative).resolve() if relative else None
                if (
                    candidate is None
                    or task_dir.resolve() not in candidate.parents
                    or not candidate.is_file()
                ):
                    failures.append(
                        f"{task_dir.name}/{row.get('attempt_id')}: missing {field} evidence"
                    )
    if task_count < 1:
        failures.append("raw/tasks contains no trajectories")
    if selected_aux_ok < 1:
        failures.append("no selected successful vision_analyze_aux attempt")

    summary = {
        "schema": "mural.nova-proxy-run-validation.v1",
        "ok": not failures,
        "run_root": str(root),
        "gate_commit": provenance.get("commit"),
        "launch_model": launch_profile.get("model") if isinstance(launch_profile, dict) else None,
        "readiness_ok": readiness.get("ok") if isinstance(readiness, dict) else None,
        "proxy_trace_counts": trace_counts,
        "raw_task_count": task_count,
        "selected_aux_ok": selected_aux_ok,
        "attempt_counts": dict(sorted(attempt_counts.items())),
        "failures": failures,
    }
    report = root / "gate-reports" / "nova-proxy-summary.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
