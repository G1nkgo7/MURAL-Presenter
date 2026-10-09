"""WebUI must surface structured agent failure data, not just "exited with code 1".

``main.py`` cannot be imported here (no fastapi in the harness test env), so we
extract the pure helper ``_agent_failure_payload`` by AST and exercise it, and
assert the deck-progress endpoint wires it into the error payload the WebUI reads.
The frontend (app.js) is owned elsewhere; this only guarantees the DATA is present.
"""
from __future__ import annotations

import ast
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN_PY = ROOT / "studio" / "app" / "main.py"


def _load_helper():
    src = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    func = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_agent_failure_payload"
    )
    namespace: dict = {"json": json, "Path": Path}
    exec(compile(ast.Module([func], []), str(MAIN_PY), "exec"), namespace)
    return namespace["_agent_failure_payload"]


def _write_state(run_dir: Path, state: dict) -> None:
    (run_dir / "_state").mkdir(parents=True, exist_ok=True)
    (run_dir / "_state" / "skill-runtime.json").write_text(
        json.dumps(state), encoding="utf-8"
    )


def test_agent_failure_payload_exposes_repeated_tool_failure_fields() -> None:
    helper = _load_helper()
    run_dir = Path(tempfile.mkdtemp())
    _write_state(
        run_dir,
        {
            "schema": "mural.skill-runtime.v1",
            "status": "failed",
            "error": "RepeatedToolFailureError: ...",
            "agent_failed": {
                "error_type": "RepeatedToolFailureError",
                "error_summary": "repeated failing read_file",
                "last_failed_tool": "read_file",
                "last_failed_error_code": "read_scope_denied",
                "arguments_summary": '{"path":"skills/x/scripts/y.py"}',
                "repeat_count": 3,
            },
        },
    )
    payload = helper(str(run_dir))
    assert payload is not None
    assert payload["error_type"] == "RepeatedToolFailureError"
    assert payload["error_summary"]
    assert payload["last_failed_tool"] == "read_file"
    assert payload["last_failed_error_code"] == "read_scope_denied"
    assert payload["arguments_summary"]
    assert payload["repeat_count"] == 3


def test_agent_failure_payload_is_none_without_structured_data() -> None:
    helper = _load_helper()
    missing = Path(tempfile.mkdtemp())
    assert helper(str(missing)) is None
    plain = Path(tempfile.mkdtemp())
    _write_state(plain, {"status": "failed"})
    assert helper(str(plain)) is None


def test_deck_progress_wires_agent_failed_into_the_error_payload() -> None:
    # Structural guarantee: the failed-status branch reads the helper into `prog`.
    src = MAIN_PY.read_text(encoding="utf-8")
    assert "_agent_failure_payload" in src
    assert 'prog["agent_failed"]' in src
