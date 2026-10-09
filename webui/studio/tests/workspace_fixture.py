"""测试直接取 Harness 的唯一映射，不维护第二套生产路径表。"""
import importlib.util
import json
from pathlib import Path
import sys

_SOURCE = Path(__file__).resolve().parents[3] / "harnesses/mural-presenter-v1/runtime/workspace_paths.py"
_SPEC = importlib.util.spec_from_file_location("studio_test_workspace_paths", _SOURCE)
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def write_workspace_manifest(root):
    path = Path(root) / "_state/workspace.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"layout_version": 2,
        "paths_schema": _MODULE.WORKSPACE_PATHS_SCHEMA, "paths": _MODULE._v2_paths()}))
