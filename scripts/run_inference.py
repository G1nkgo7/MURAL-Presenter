"""使用当前部署配置启动冻结的 0919 推理入口。"""

import os
from pathlib import Path
import subprocess
import sys

from runtime_environment import prepare_environment


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    prepare_environment(root)
    python = os.environ.get("PPTAGENT_ENGINE_PYTHON", sys.executable)
    entry = root / "harnesses/mural-presenter-v1/infer.py"
    return subprocess.call([python, str(entry), *sys.argv[1:]], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
