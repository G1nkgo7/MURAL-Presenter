from __future__ import annotations

import json
import base64
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS))

from core import environment_doctor  # noqa: E402
from core import run_batch  # noqa: E402
from core.runtime_capabilities import detect_runtime_capabilities  # noqa: E402


def test_multimodal_probe_png_is_decodable() -> None:
    from PIL import Image

    with Image.open(io.BytesIO(base64.b64decode(environment_doctor._TINY_PNG))) as image:
        image.load()
        assert image.size == (16, 16)
        assert image.getpixel((8, 8))[0] > 200


@pytest.mark.parametrize(
    ("content", "expected"),
    [("red", True), ("The image is 红色", True), ("green", False)],
)
def test_openai_multimodal_probe_requires_observed_color(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        environment_doctor,
        "_post_json",
        lambda *args, **kwargs: (
            SimpleNamespace(ok=True, status_code=200),
            {"choices": [{"message": {"content": content}}]},
            1,
        ),
    )
    ok, _, _ = environment_doctor._openai_multimodal_probe({
        "model_key": "",
        "model": "pptagent",
        "model_base_url": "http://example/v1",
    })
    assert ok is expected


@pytest.mark.parametrize(
    ("content", "expected"),
    [("red", True), ("主色是红色", True), ("blue", False)],
)
def test_anthropic_multimodal_probe_requires_observed_color(
    monkeypatch: pytest.MonkeyPatch,
    content: str,
    expected: bool,
) -> None:
    monkeypatch.setattr(
        environment_doctor,
        "_post_json",
        lambda *args, **kwargs: (
            SimpleNamespace(ok=True, status_code=200),
            {"content": [{"type": "text", "text": content}]},
            1,
        ),
    )
    ok, _, _ = environment_doctor._anthropic_multimodal_probe({
        "model_key": "test-key",
        "model": "pptagent",
        "model_base_url": "http://example/v1",
    })
    assert ok is expected


def _fonts() -> dict:
    return {
        "ready": True,
        "noto_sans_sc": True,
        "noto_serif_sc": True,
        "noto_sans_sc_path": "/tmp/NotoSansSC.ttf",
        "noto_serif_sc_path": "/tmp/NotoSerifSC.ttf",
        "noto_sans_sc_weight": {"valid_delivery_weight": True},
        "noto_serif_sc_weight": {"valid_delivery_weight": True},
    }


def _modules() -> dict[str, dict]:
    return {
        key: {"available": True, "version": "test"}
        for key in (
            "playwright",
            "fonttools",
            "pymupdf",
            "pillow",
            "python_docx",
            "python_pptx",
            "openpyxl",
            "rapidocr",
        )
    }


@pytest.fixture
def doctor_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MODEL_BACKEND", "openai")
    monkeypatch.setenv("STUDENT_BASE_URL", "http://pptagent.example/v1")
    monkeypatch.setenv("STUDENT_MODEL", "pptagent")
    monkeypatch.setenv("MODEL", "pptagent")
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.delenv("IMAGE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(
        environment_doctor.runtime_capabilities,
        "_browser_executable",
        lambda: "/bin/true",
    )
    monkeypatch.setattr(
        environment_doctor.runtime_capabilities,
        "_font_sources",
        _fonts,
    )
    monkeypatch.setattr(environment_doctor, "_module_matrix", _modules)
    monkeypatch.setattr(
        environment_doctor,
        "_renderer_probe",
        lambda browser: (True, f"rendered with {browser}", 3),
    )


def test_doctor_persists_unchecked_then_checked_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    doctor_env,
) -> None:
    report_path = tmp_path / "doctor.json"

    def inspect_transition(browser: str):
        checking = json.loads(report_path.read_text(encoding="utf-8"))
        assert checking["checked"] is False
        assert checking["ready"] is False
        assert checking["status"] == "checking"
        return True, f"rendered with {browser}", 2

    monkeypatch.setattr(environment_doctor, "_renderer_probe", inspect_transition)
    report = environment_doctor.run_environment_doctor(
        report_path=report_path,
        live=False,
        force=True,
    )
    persisted = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["checked"] is True
    assert report["ready"] is True
    assert persisted["checked"] is True
    assert persisted["config_fingerprint"] == report["config_fingerprint"]


def test_doctor_blocks_before_run_when_renderer_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    doctor_env,
) -> None:
    monkeypatch.setattr(
        environment_doctor.runtime_capabilities,
        "_browser_executable",
        lambda: "",
    )
    report = environment_doctor.run_environment_doctor(
        report_path=tmp_path / "doctor.json",
        live=False,
        force=True,
    )
    assert report["checked"] is True
    assert report["ready"] is False
    assert report["status"] == "blocked"
    assert any("renderer" in item for item in report["fatal_errors"])


def test_doctor_blocks_unsupported_attachment_before_ingestion(
    tmp_path: Path,
    doctor_env,
) -> None:
    report = environment_doctor.run_environment_doctor(
        report_path=tmp_path / "doctor.json",
        required_attachment_suffixes=[".xls"],
        live=False,
        force=True,
    )
    assert report["ready"] is False
    assert any("unsupported_attachment_type" in item for item in report["fatal_errors"])


def test_doctor_requires_office_preview_for_pptx_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    doctor_env,
) -> None:
    monkeypatch.setattr(environment_doctor.shutil, "which", lambda name: None)
    report = environment_doctor.run_environment_doctor(
        report_path=tmp_path / "doctor.json",
        required_attachment_suffixes=[".pptx"],
        live=False,
        force=True,
    )
    assert report["ready"] is False
    assert any("libreoffice_page_preview" in item for item in report["fatal_errors"])


def test_configured_but_broken_optional_service_blocks_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    doctor_env,
) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "search-secret")
    monkeypatch.setenv("IMAGE_API_KEY", "image-secret")
    monkeypatch.setenv("ENABLE_IMAGE_GEN", "1")
    monkeypatch.setattr(
        environment_doctor,
        "_model_probe",
        lambda resolved: (True, "model ok", 4),
    )
    monkeypatch.setattr(
        environment_doctor,
        "_search_probe",
        lambda resolved: (False, "search 401", 5),
    )
    monkeypatch.setattr(
        environment_doctor,
        "_image_probe",
        lambda resolved: (False, "image 429", 6),
    )
    report = environment_doctor.run_environment_doctor(
        {"enable_image_gen": True},
        report_path=tmp_path / "doctor.json",
        live=True,
        force=True,
    )
    assert report["ready"] is False
    assert report["capabilities"]["web_search"] is False
    assert report["capabilities"]["image_generate"] is False
    assert "configured search service is unavailable" in report["fatal_errors"]
    assert "configured image generation service is unavailable" in report["fatal_errors"]


def test_missing_optional_keys_degrade_without_blocking(
    tmp_path: Path,
    doctor_env,
) -> None:
    report = environment_doctor.run_environment_doctor(
        report_path=tmp_path / "doctor.json",
        live=False,
        force=True,
    )
    assert report["ready"] is True
    assert report["status"] == "degraded"
    assert report["capabilities"]["web_search"] is False
    assert report["capabilities"]["image_generate"] is False


def test_runtime_capabilities_consume_doctor_service_truth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "configured-but-doctor-disabled")
    monkeypatch.setenv("IMAGE_API_KEY", "configured-but-doctor-disabled")
    profile = detect_runtime_capabilities({
        "model": "pptagent",
        "model_base_url": "http://pptagent.example/v1",
        "enable_image_gen": True,
        "_raw_user_query": "Create a factual presentation",
        "_environment_doctor": {
            "checked": True,
            "ready": True,
            "status": "degraded",
            "config_fingerprint": "test",
            "capabilities": {"web_search": False, "image_generate": False},
        },
    }, plan_only=True)
    assert profile["tools"]["web_search"] is False
    assert profile["tools"]["image_generate"] is False
    assert profile["environment"]["doctor_checked"] is True


def test_fingerprint_changes_with_model_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    doctor_env,
) -> None:
    first = environment_doctor.environment_fingerprint()
    monkeypatch.setenv("STUDENT_BASE_URL", "http://pptagent-2.example/v1")
    second = environment_doctor.environment_fingerprint()
    assert first != second


def test_fingerprint_is_stable_for_identical_environment(doctor_env) -> None:
    first = environment_doctor.environment_fingerprint(
        required_attachment_suffixes=[".xlsx", ".pdf", ".pdf"]
    )
    second = environment_doctor.environment_fingerprint(
        required_attachment_suffixes=[".pdf", ".xlsx"]
    )
    assert first == second


def test_worker_reuses_ready_batch_doctor_for_attachment_suffix_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "schema": environment_doctor.SCHEMA,
        "checked": True,
        "ready": True,
        "status": "ready",
        "required_attachment_suffixes": [".pdf", ".xlsx"],
        "capabilities": {"renderer": True},
    }
    config = {"_environment_doctor": report}
    seed = {"materials": [{"path": "/fixtures/reference.pdf"}]}
    monkeypatch.setattr(
        environment_doctor,
        "run_environment_doctor",
        lambda *args, **kwargs: pytest.fail(
            "worker re-probed despite ready batch suffix superset"
        ),
    )
    assert run_batch._doctor_for_worker(config, seed) == report


def test_worker_persists_blocked_doctor_before_material_or_model_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = {
        "schema": environment_doctor.SCHEMA,
        "checked": True,
        "ready": False,
        "status": "blocked",
        "config_fingerprint": "blocked-test",
        "required_attachment_suffixes": [],
        "fatal_errors": ["renderer: missing"],
    }
    run_dir = tmp_path / "deck"

    def blocked_doctor(config, seed):
        checking = json.loads(
            (run_dir / "_trace/environment-doctor.json").read_text(encoding="utf-8")
        )
        assert checking["checked"] is False
        assert checking["ready"] is False
        assert checking["status"] == "checking"
        return report

    monkeypatch.setattr(run_batch, "_doctor_for_worker", blocked_doctor)
    monkeypatch.setattr(
        run_batch,
        "stage_materials",
        lambda *args, **kwargs: pytest.fail("Material ran before the doctor gate"),
    )
    result = run_batch.worker({
        "sample_id": "doctor-blocked",
        "run_dir": str(run_dir),
        "config": {},
        "seed": {"query": "test"},
    })
    assert result["status"] == "environment_blocked"
    persisted = json.loads(
        (run_dir / "_trace/environment-doctor.json").read_text(encoding="utf-8")
    )
    assert persisted["checked"] is True
    assert persisted["ready"] is False
