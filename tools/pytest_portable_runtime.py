"""为冻结配置测试提供便携路径，保留原始参数和断言。"""

import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "harnesses/mural-presenter-v1"


@pytest.fixture(autouse=True)
def portable_frozen_runtime_environment(request, monkeypatch):
    if Path(request.node.path) != HARNESS / "tests/test_config.py":
        return
    request.getfixturevalue("isolated_shared_objects")
    read_environment_file = request.module.read_environment_file

    def read_portable_environment(path):
        environment = read_environment_file(path)
        if Path(path).resolve() == HARNESS / "runtime.env":
            environment["MURAL_RUNTIME_CONFIG"] = os.environ["MURAL_RUNTIME_CONFIG"]
        return environment

    monkeypatch.setattr(request.module, "read_environment_file", read_portable_environment)
