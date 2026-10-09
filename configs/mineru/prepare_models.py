"""下载固定公开模型快照，并仅写入 MinerU 官方本地模型配置。"""

from __future__ import annotations

import importlib.metadata
import json
import sys
from pathlib import Path

from huggingface_hub import snapshot_download


MODELS = {
    "vlm": {
        "repo_id": "opendatalab/MinerU2.5-Pro-2605-1.2B",
        "revision": "bff20d4ae2bf202df9f45284b4d43681555a97ed",
    },
    "pipeline": {
        "repo_id": "opendatalab/PDF-Extract-Kit-1.0",
        "revision": "ed6b654c018d742e65a17671e379c5e6ecc87ec9",
        "allow_patterns": [
            "models/OCR/paddleocr_torch/*",
            "models/Layout/PP-DocLayoutV2/*",
            "models/MFR/unimernet_hf_small_2503/*",
            "models/MFR/pp_formulanet_plus_m/*",
            "models/TabRec/SlanetPlus/slanet-plus.onnx",
            "models/TabRec/UnetStructure/unet.onnx",
            "models/TabCls/paddle_table_cls/PP-LCNet_x1_0_table_cls.onnx",
        ],
    },
}


def download_models(root: Path) -> None:
    config = {"config_version": "1.3.2", "models-dir": {}}
    receipt = root / "evidence" / "model-revisions.json"
    if receipt.exists() and json.loads(receipt.read_text()) == MODELS:
        config = json.loads((root / "mineru.json").read_text())
        for location in config["models-dir"].values():
            assert Path(location).is_dir(), location
        print("Reusing completed fixed model snapshots", flush=True)
        return
    for mode, spec in MODELS.items():
        print(f"Downloading {mode}: {spec['repo_id']} @ {spec['revision']}", flush=True)
        location = snapshot_download(
            **spec, local_dir=str(root / "models" / mode), max_workers=4
        )
        config["models-dir"][mode] = location
    (root / "mineru.json").write_text(json.dumps(config, indent=2) + "\n")
    receipt.write_text(json.dumps(MODELS, indent=2) + "\n")


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    versions = {name: importlib.metadata.version(name) for name in ("mineru", "vllm")}
    assert versions == {"mineru": "3.4.5", "vllm": "0.19.1"}, versions
    installation = {"env_prefix": sys.prefix, "versions": versions}
    for marker in (
        Path(sys.prefix) / ".installation-complete.json",
        root / "evidence" / "installation-complete.json",
    ):
        marker.write_text(json.dumps(installation, indent=2) + "\n")
    download_models(root)


if __name__ == "__main__":
    main()
