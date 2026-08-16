#!/usr/bin/env python3
"""Materialize the auditable CCI app spec after the smoke gate passes."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path


IMAGE = "registry.ms-sc-01.maoshanwangtech.com/lepton-trainingjob/nvidia24.04-ubuntu22.04-py3.10-cuda12.4-cudnn9.1-torch2.3.0-transformerengine1.5:v1.0.0-20241130-nvdia-base-image"
ENTRY = Path(
    os.environ.get("MURAL_CLEAN_BULK_ENTRY", "")
    or Path(__file__).with_name("run_clean_bulk_cci.sh")
).resolve()


def write_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    temporary.write_text(text, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--app-name", required=True)
    parser.add_argument("--queries", required=True, type=Path)
    parser.add_argument("--initial-concurrency", type=int, default=32)
    args = parser.parse_args()
    root = args.run_root.resolve()
    command = (
        f"RUN_ROOT={root} QUERIES={args.queries.resolve()} "
        f"INITIAL_TOTAL_CONCURRENCY={args.initial_concurrency} "
        f"bash {ENTRY}"
    )
    spec = f"""display_name: {args.app_name}
resource_pool:
  name: design
  available_zone: ms-sc-01a
  vpc_id: 0b3cee6d-c363-11ee-a5f9-9e29792dec2f
replicas: 1
scheduling:
  priority: NORMAL
  quota_type: RESERVED
template:
  resource_spec:
    name: N6lS.Iu.I80.32c128g
  containers:
    - name: mural-rollout
      image_path: {IMAGE}
      resource_request:
        cpu: '32'
        memory: 128GiB
        nvidia.com/gpu: '0'
      volume_mounts:
        - type: PV_AFS
          id: ce3b1174-f6eb-11ee-a372-82d352e10aed
          mount_path: /mnt/afs
          subdir: /
          zone: ms-sc-01a
      command:
        - bash
        - -lc
        - '{command}'
"""
    write_atomic(root / "cci" / "app.yaml", spec)
    manifest = {
        "schema": "mural.nova-clean-cci-launch.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "app_name": args.app_name,
        "spec": "N6lS.Iu.I80.32c128g",
        "gpu": 0,
        "initial_deck_concurrency": args.initial_concurrency,
        "route_allocations": [11, 11, 10],
        "route_cap": 22,
        "query_shards": [334, 333, 333],
        "teacher_models": {
            "cloud1": "claude-opus-4-7-thinking",
            "cloud2": "claude-opus-4-7-thinking",
            "cloud3": "claude-opus-4-7-thinking",
        },
        "child_initial": 2,
        "child_cap": 4,
        "queries": str(args.queries.resolve()),
        "run_root": str(root),
        "dynamic_control": str(root / "live" / "total-concurrency"),
        "setter": str(Path(ENTRY).with_name("set_clean_bulk_concurrency.py")),
        "formal_data_boundary": str(root / "FORMAL_DATA_BOUNDARY.json"),
        "data_status_index": str(root / "data-status" / "index.jsonl"),
        "direct_use_allowlist": str(
            root / "data-status" / "direct-use.sample_ids.txt"
        ),
        "rerun_required_list": str(
            root / "data-status" / "rerun-required.sample_ids.txt"
        ),
    }
    write_atomic(
        root / "cci" / "launch-manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    print(root / "cci" / "app.yaml")


if __name__ == "__main__":
    main()
