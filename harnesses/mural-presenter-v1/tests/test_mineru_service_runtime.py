"""统一 JSON 到 MinerU 原生并发与真实物理核约束的薄转译。"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest


@pytest.fixture
def launcher():
    path = Path(__file__).resolve().parents[3] / "configs/mineru/service_runtime.py"
    spec = importlib.util.spec_from_file_location("mineru_service_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_physical_core_selection_does_not_count_smt_twice(tmp_path, launcher):
    for cpu, package, core in [(0, 0, 0), (1, 0, 1), (2, 1, 0), (64, 0, 0), (65, 0, 1), (66, 1, 0)]:
        path = tmp_path / f"cpu{cpu}/topology"
        path.mkdir(parents=True)
        (path / "physical_package_id").write_text(str(package))
        (path / "core_id").write_text(str(core))
    assert launcher.physical_cpus({0, 1, 2, 64, 65, 66}, 3, tmp_path) == [0, 1, 2]
    with pytest.raises(ValueError, match="physical_core_budget_unavailable"):
        launcher.physical_cpus({0, 1, 2, 64, 65, 66}, 4, tmp_path)


def test_runtime_json_overrides_legacy_native_thread_and_concurrency_environment(monkeypatch, launcher):
    config = launcher.load_runtime_config({})
    config["mineru"]["cpu"] = {"concurrency": 4, "window": 16, "cores": 48}
    monkeypatch.setattr(launcher, "load_runtime_config", lambda: config)
    for name, value in (("MINERU_API_MAX_CONCURRENT_REQUESTS", "99"), ("MINERU_PROCESSING_WINDOW_SIZE", "999"),
                        ("OMP_NUM_THREADS", "99"), ("MKL_NUM_THREADS", "99"), ("OPENBLAS_NUM_THREADS", "99"), ("NUMEXPR_NUM_THREADS", "99")):
        monkeypatch.setenv(name, value)
    applied = []
    monkeypatch.setattr(launcher, "physical_cpus", lambda *_: list(range(48)))
    monkeypatch.setattr(launcher.os, "sched_setaffinity", lambda _pid, cpus: applied.append(cpus))
    monkeypatch.setattr(launcher.os, "sched_getaffinity", lambda _pid: set(range(48)))
    values = {}
    torch = SimpleNamespace(set_num_threads=lambda n: values.update(intra=n),
        set_num_interop_threads=lambda n: values.update(inter=n), get_num_threads=lambda: values["intra"],
        get_num_interop_threads=lambda: values["inter"])
    cv2 = SimpleNamespace(setNumThreads=lambda n: values.update(cv=n), getNumThreads=lambda: values["cv"])
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(
        InferenceSession=type("NativeSession", (), {}), SessionOptions=SimpleNamespace))
    result = launcher.configure("cpu")
    assert applied == [list(range(48))] and values == {"intra": 12, "inter": 1, "cv": 12}
    assert launcher.os.environ["MINERU_API_MAX_CONCURRENT_REQUESTS"] == "4"
    assert launcher.os.environ["MINERU_PROCESSING_WINDOW_SIZE"] == "16"
    assert launcher.os.environ["OMP_NUM_THREADS"] == "12"
    assert launcher.os.environ["MINERU_INTRA_OP_NUM_THREADS"] == "12"
    assert launcher.os.environ["MINERU_INTER_OP_NUM_THREADS"] == "1"
    assert result["onnx_intra_threads"] == 12 and result["onnx_inter_threads"] == 1
    assert result["cores"] == 48 and result["page_batch"] == "mineru_auto"


def test_gpu_uses_native_window_and_concurrency_without_cpu_reconfiguration(monkeypatch, launcher):
    config = launcher.load_runtime_config({})
    monkeypatch.setattr(launcher, "load_runtime_config", lambda: config)
    monkeypatch.setenv("MINERU_API_MAX_CONCURRENT_REQUESTS", "99")
    monkeypatch.setenv("MINERU_PROCESSING_WINDOW_SIZE", "999")
    monkeypatch.setattr(launcher.os, "sched_setaffinity", lambda *_: pytest.fail("GPU affinity changed"))
    result = launcher.configure("gpu")
    assert result["concurrency"] == 4 and result["window"] == 64 and "cpu_affinity" not in result


def test_cpu_onnx_options_preserve_caller_settings_and_constructor_arguments(monkeypatch, launcher):
    calls = []
    class NativeSession:
        def __init__(self, source, options, *args, **kwargs):
            calls.append((source, options, args, kwargs))
    ort = SimpleNamespace(InferenceSession=NativeSession, SessionOptions=SimpleNamespace)
    monkeypatch.setitem(sys.modules, "onnxruntime", ort)
    launcher.configure_onnx_threads(12)
    options = SimpleNamespace(intra_op_num_threads=0, inter_op_num_threads=0,
                              graph_optimization_level="caller-value", custom_marker={"keep": True})
    session = ort.InferenceSession("explicit.onnx", options, ["CPUExecutionProvider"], provider_options=[{}])
    assert isinstance(session, NativeSession)
    assert calls[0] == ("explicit.onnx", options, (["CPUExecutionProvider"],), {"provider_options": [{}]})
    assert options.intra_op_num_threads == 12 and options.inter_op_num_threads == 1
    assert options.graph_optimization_level == "caller-value" and options.custom_marker == {"keep": True}
    ort.InferenceSession("default.onnx", providers=["CPUExecutionProvider"])
    assert calls[1][1].intra_op_num_threads == 12 and calls[1][1].inter_op_num_threads == 1
    assert calls[1][3] == {"providers": ["CPUExecutionProvider"]}
