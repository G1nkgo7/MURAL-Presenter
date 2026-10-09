"""Doctor 与生成共用本地检查，但不创建 Deck、调用模型或复用旧报告。"""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import infer
from runtime import mineru, production


@pytest.fixture
def doctor_env(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime.env"
    runtime.write_text("")
    config = production.load_runtime_config({})
    config["mineru"]["endpoints"] = ["http://127.0.0.1:8200", "http://127.0.0.1:8201"]
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("MURAL_RUNTIME_CONFIG", str(config_path))
    monkeypatch.setenv("MODEL_API_KEY", "doctor-test-secret")
    monkeypatch.setattr(mineru, "service_info", Mock(return_value={"version": "3.4.5", "protocol_version": 2}))
    monkeypatch.setattr(production, "_module_importable", lambda _: True)
    monkeypatch.setattr(production.shutil, "which", lambda _: "/installed/tool")
    monkeypatch.setattr(infer, "_run_single_deck", lambda *_a, **_k: pytest.fail("Doctor started generation"))
    monkeypatch.setattr(production.SkillSnapshot, "stage", lambda *_a, **_k: pytest.fail("Doctor staged a Deck"))
    return ["--doctor", "--runtime-env", str(runtime), "--json", str(tmp_path / "doctor.json")]


def test_doctor_cli_runs_without_query_or_workspace(tmp_path, doctor_env, capsys):
    assert infer.main(doctor_env) == 0
    report = json.loads((tmp_path / "doctor.json").read_text())
    assert report["checked"] and report["status"] == "pass"
    assert report["live_probes"] and report["mode"] == "local_and_services"
    mineru.service_info.assert_called_once_with("http://127.0.0.1:8200")
    assert report["scope"] == "full"
    assert {c["name"] for c in report["checks"]} >= {"skill_source", "model_credentials", "anthropic", "mineru", "libreoffice"}
    assert "doctor-test-secret" not in json.dumps(report) + capsys.readouterr().out
    assert not (tmp_path / "_state").exists() and not (tmp_path / "_trace").exists()


def test_doctor_cli_force_rechecks_and_preserves_structured_blocker(tmp_path, doctor_env, monkeypatch):
    assert infer.main(doctor_env) == 0
    with pytest.raises(SystemExit):
        infer.main(doctor_env)
    monkeypatch.setattr(production, "_module_importable", lambda name: name != "playwright")
    assert infer.main([*doctor_env, "--force"]) == 1
    report = json.loads((tmp_path / "doctor.json").read_text())
    assert report["checked"] and report["status"] == "blocked"
    assert next(c for c in report["checks"] if c["name"] == "playwright")["ok"] is False


def test_generation_preflight_still_blocks(tmp_path, doctor_env, monkeypatch):
    monkeypatch.setattr(production, "_module_importable", lambda name: name != "playwright")
    spec = infer.RunSpec.from_inputs(raw_user_query="test", output_workspace=tmp_path)
    with pytest.raises(production.ProductionError, match="playwright"):
        production.EnvironmentDoctor(spec, None, {"MODEL_API_KEY": "test"}).run()
    assert json.loads((tmp_path / "_trace/environment-doctor.json").read_text())["status"] == "blocked"


@pytest.mark.parametrize("suffix, expected", [
    ("", set()),
    (".txt", set()),
    (".pdf", {"pymupdf", "requests"}),
    (".docx", {"pymupdf", "requests"}),
    (".pptx", {"pymupdf", "requests"}),
    (".xlsx", {"pymupdf", "requests"}),
])
def test_generation_doctor_only_imports_applicable_dependencies(tmp_path, doctor_env, monkeypatch, suffix, expected):
    imports = []
    monkeypatch.setattr(production, "_module_importable", lambda name: imports.append(name) or True)
    attachments = []
    if suffix:
        attachment = tmp_path / ("input" + suffix)
        attachment.write_text("local fixture")
        attachments.append(attachment)
    spec = infer.RunSpec.from_inputs(raw_user_query="test", output_workspace=tmp_path, attachments=attachments,
                                   environment={"MODEL_API_FORMAT": "openai_chat_compatible"})
    report = production.EnvironmentDoctor(spec, None, {"MODEL_API_KEY": "test"}).run()
    assert report["scope"] == "task" and report["status"] == "pass"
    assert set(imports) == {"playwright", "PIL"} | expected
    assert len(imports) == len(set(imports))
    if not expected:
        assert not {"rapidocr", "pymupdf", "python_docx", "libreoffice"} & {c["name"] for c in report["checks"]}


def test_pdf_doctor_never_imports_ocr_or_probes_services_on_deck_start(tmp_path, doctor_env, monkeypatch):
    attachment = tmp_path / "input.pdf"
    attachment.write_bytes(b"local fixture")
    spec = infer.RunSpec.from_inputs(raw_user_query="test", output_workspace=tmp_path, attachments=[attachment])
    doctor = production.EnvironmentDoctor(spec, None, {"MODEL_API_KEY": "test"})
    from runtime import mineru
    monkeypatch.setattr(mineru, "service_info", lambda *_: pytest.fail("startup service probe"))
    def available(name):
        assert name not in {"rapidocr_onnxruntime", "rapidocr", "cv2", "docx", "pptx", "openpyxl"}
        return True
    monkeypatch.setattr(production, "_module_importable", available)
    assert doctor.run()["status"] == "pass"


def test_full_doctor_reports_actual_service_probe_and_infra_failover(tmp_path, doctor_env, monkeypatch):
    from runtime import mineru
    spec = infer.RunSpec.from_inputs(raw_user_query="test", output_workspace=tmp_path)
    calls = []
    def probe(endpoint):
        calls.append(endpoint)
        if endpoint.endswith(":8200"):
            raise mineru.MinerUError("mineru_http_503", infrastructure=True)
        return {"version": "3.4.5", "protocol_version": 2}
    monkeypatch.setattr(mineru, "service_info", probe)
    report = production.EnvironmentDoctor(spec, None, {"MODEL_API_KEY": "test",
        "MURAL_RUNTIME_CONFIG": str(tmp_path / "runtime.json")}).run(full=True)
    assert report["live_probes"] and report["mode"] == "local_and_services"
    assert calls == ["http://127.0.0.1:8200", "http://127.0.0.1:8201"]
    assert next(row for row in report["checks"] if row["name"] == "mineru")["ok"]


def test_anthropic_generation_doctor_blocks_missing_selected_sdk(tmp_path, doctor_env, monkeypatch):
    spec = infer.RunSpec.from_inputs(raw_user_query="test", output_workspace=tmp_path,
                                   environment={"MODEL_API_FORMAT": "anthropic"})
    monkeypatch.setattr(production, "_module_importable", lambda name: name != "anthropic")
    with pytest.raises(production.ProductionError, match="anthropic"):
        production.EnvironmentDoctor(spec, None, {"MODEL_API_KEY": "test"}).run()


@pytest.mark.parametrize("args", [["--doctor"], ["--doctor", "--query", "test"], ["--query", "test", "--force"]])
def test_doctor_rejects_incomplete_or_mixed_commands(args):
    with pytest.raises(SystemExit):
        infer.main(args)
