"""把唯一 runtime.json 转译为固定 MinerU 的原生参数，并约束 CPU 物理核。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "harnesses/mural-presenter-v1"))

from runtime.config import load_runtime_config


def physical_cpus(allowed: set[int], budget: int, topology: Path = Path("/sys/devices/system/cpu")) -> list[int]:
    groups = {}
    for cpu in sorted(allowed):
        base = topology / ("cpu%d" % cpu) / "topology"
        key = (int((base / "physical_package_id").read_text()), int((base / "core_id").read_text()))
        groups.setdefault(key, cpu)
    if len(groups) < budget:
        raise ValueError("mineru_cpu_physical_core_budget_unavailable")
    return [groups[key] for key in sorted(groups)[:budget]]


def configure_onnx_threads(threads: int) -> None:
    """仅 CPU 启动进程注入原生 SessionOptions，阻止 ORT 默认按整机核数重绑线程。"""
    import onnxruntime as ort

    native = ort.InferenceSession
    class CpuInferenceSession(native):
        def __init__(self, path_or_bytes, sess_options=None, *args, **kwargs):
            options = sess_options if sess_options is not None else ort.SessionOptions()
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
            super().__init__(path_or_bytes, options, *args, **kwargs)
    ort.InferenceSession = CpuInferenceSession


def configure(kind: str) -> dict:
    runtime = load_runtime_config()
    selected = runtime["mineru"][kind]
    os.environ["MINERU_API_MAX_CONCURRENT_REQUESTS"] = str(selected["concurrency"])
    os.environ["MINERU_PROCESSING_WINDOW_SIZE"] = str(selected["window"])
    evidence = {"kind": kind, **selected, "config_effective_sha256": hashlib.sha256(
        json.dumps(runtime, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "page_batch": "mineru_auto"}
    if kind == "cpu":
        cpus = physical_cpus(os.sched_getaffinity(0), selected["cores"])
        os.sched_setaffinity(0, cpus)
        threads = max(1, selected["cores"] // selected["concurrency"])
        for name in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
            os.environ[name] = str(threads)
        os.environ["MINERU_INTRA_OP_NUM_THREADS"] = str(threads)
        os.environ["MINERU_INTER_OP_NUM_THREADS"] = "1"
        import torch
        import cv2

        torch.set_num_threads(threads)
        torch.set_num_interop_threads(1)
        cv2.setNumThreads(threads)
        configure_onnx_threads(threads)
        evidence.update(cpu_affinity=sorted(os.sched_getaffinity(0)),
            torch_intra_threads=torch.get_num_threads(), torch_inter_threads=torch.get_num_interop_threads(),
            opencv_threads=cv2.getNumThreads(), onnx_intra_threads=threads, onnx_inter_threads=1,
            onnx_scope="cpu_launcher_before_mineru_import")
    return evidence


def main():
    kind = sys.argv.pop(1)
    if kind not in {"gpu", "cpu"}:
        raise ValueError("mineru_service_kind_invalid")
    print("MURAL_MINERU_RUNTIME " + json.dumps(configure(kind), sort_keys=True), flush=True)
    if sys.argv[1:] == ["--check"]:
        return
    from mineru.cli.fast_api import main as serve
    serve()


if __name__ == "__main__":
    main()
