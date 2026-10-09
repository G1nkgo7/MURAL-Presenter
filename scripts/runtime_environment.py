"""在部署边界解析本机路径，保持 0919 Skill/Harness 原始字节不变。"""

import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def prepare_environment(root: Path = ROOT) -> Path:
    explicit = os.environ.get("MURAL_RUNTIME_CONFIG", "").strip()
    if explicit:
        config = Path(explicit).expanduser().resolve()
        if not config.is_file():
            raise FileNotFoundError(f"MURAL_RUNTIME_CONFIG does not exist: {config}")
    else:
        config = root / "configs/runtime.local.json"
        if not config.exists():
            value = json.loads((root / "configs/runtime.json").read_text())
            directory = Path(value["resources"]["directory"])
            if not directory.is_absolute():
                value["resources"]["directory"] = str((root / directory).resolve())
            with config.open("x", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
    os.environ["MURAL_RUNTIME_CONFIG"] = str(config)
    os.environ.setdefault("MURAL_SKILL_DIR", str(root / "skills/mural-presenter-v1"))
    os.environ.setdefault("MURAL_RELEASE_MANIFEST", str(root / "configs/releases/mural-paper-v1.json"))
    return config
