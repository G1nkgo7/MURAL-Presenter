"""真实脚本参数与 Harness 单一登记的接线回归。"""
import base64
from contextlib import nullcontext
from dataclasses import replace
from io import BytesIO
import json
import os
import subprocess
import shlex
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from runtime.production import IMAGE_HISTORY_DIAGNOSTIC_RECORDS, ProductionToolServices
from runtime.tools import ToolContext, ToolContractError, ToolExecutionError, WriteScope
from state.artifacts import ArtifactStateError, ArtifactStore
from state.workspace import WorkspaceState
from geometry_fixture import geometry, write_geometry


SKILL = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"


def services_for(root):
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = WorkspaceState(root)
    services.artifacts = ArtifactStore(root, workspace_state=services.workspace)
    services.settings = SimpleNamespace(command_timeout_s=30)
    services.run_spec = SimpleNamespace(authoring_profile="general")
    services.snapshot = SimpleNamespace(name=SKILL.name, target=SKILL, tree_sha256="test-command-contract",
        resolve_skill_path=lambda logical: SKILL / Path(logical).relative_to(f"skills/{SKILL.name}"),
        capture_runtime_fonts=lambda: None)
    services._command_environment = lambda: dict(os.environ)
    return services


def image_context():
    return ToolContext("image-owner", "image", read_scopes=(WriteScope("inputs", recursive=True), WriteScope("assets")),
        write_scopes=(WriteScope("assets", recursive=True),))


def test_reviewed_register_requires_consumed_current_pixels_and_keeps_one_identity(tmp_path):
    from state.workspace import file_sha256
    services = services_for(tmp_path)
    context = image_context()
    path = tmp_path / "assets/hero.png"
    Image.new("RGB", (40, 30), "navy").save(path)
    notifications = []
    services.image_handoff_sink = notifications.append
    command = ("python skills/mural-presenter-v1/scripts/image.py register . --id hero "
               "--path assets/hero.png --kind generated --slides 1 --purpose 'opening subject'")
    services.terminal(context, {"command": command})
    assert notifications == []
    for source in ([], [{"owner_id": "other", "path": "assets/hero.png", "sha256": file_sha256(path)}],
                   [{"owner_id": context.owner_id, "path": "assets/hero.png", "sha256": "old-hash"}]):
        with pytest.raises(ToolContractError, match="提前交接") as error:
            services.terminal(replace(context, metadata={"consumed_visual_sources": source}),
                              {"command": command + " --reviewed"})
        assert error.value.code == "image_review_required"
        assert notifications == []
    seen = replace(context, metadata={"consumed_visual_sources": [
        {"owner_id": context.owner_id, "path": "assets/hero.png", "sha256": file_sha256(path)}]})
    services.terminal(seen, {"command": command + " --reviewed"})
    assert len(notifications) == 1
    record = services.artifacts.artifact("hero")
    assert record.image_metadata["reviewed_sha256"] == record.sha256 == file_sha256(path)
    assert record.image_metadata["reviewed_by"] == context.owner_id
    assert len(services.artifacts.catalog()["images"]) == 1
    Image.new("RGB", (40, 30), "red").save(path)
    with pytest.raises(ToolContractError, match="提前交接"):
        services.terminal(seen, {"command": command + " --reviewed"})
    services.terminal(context, {"command": command})
    assert "reviewed_sha256" not in services.artifacts.artifact("hero").image_metadata
    assert len(notifications) == 1


@pytest.mark.parametrize("arguments", ["--status unavailable --reason missing --slides 1", ""])
def test_reviewed_registration_rejects_unavailable_or_missing_consumers(tmp_path, arguments):
    services = services_for(tmp_path)
    Image.new("RGB", (40, 30), "navy").save(tmp_path / "assets/hero.png")
    command = ("python skills/mural-presenter-v1/scripts/image.py register . --id hero "
               "--path assets/hero.png --kind generated --reviewed " + arguments)
    with pytest.raises(ToolExecutionError):
        services.terminal(image_context(), {"command": command})


def test_review_hash_is_checked_at_atomic_artifact_commit(tmp_path):
    from state.workspace import file_sha256
    services = services_for(tmp_path)
    path = tmp_path / "assets/hero.png"
    Image.new("RGB", (8, 8), "navy").save(path)
    old = file_sha256(path)
    services.artifacts.record_image_result(artifact_id="hero", owner_id="image-owner", path="assets/hero.png", status="available")
    Image.new("RGB", (8, 8), "red").save(path)
    with pytest.raises(ArtifactStateError, match="hash changed"):
        services.artifacts.record_image_result(artifact_id="hero", owner_id="image-owner", path="assets/hero.png",
            status="available", reviewed_sha256=old)
    assert "reviewed_sha256" not in services.artifacts.artifact("hero").image_metadata


@pytest.mark.parametrize("role", ["orchestrator", "image"])
def test_generated_image_handoff_reuses_identity_in_real_register(tmp_path, monkeypatch, role):
    services = services_for(tmp_path)
    services.environment = {"IMAGE_API_KEY": "test-only", "IMAGE_BASE_URL": "https://image.test/v1"}
    services.settings.image_generation_max_failure_streak = 2
    services.settings.image_generation_timeout_s = 5
    services._image_failures = {}
    services.slots = SimpleNamespace(acquire=lambda *_a, **_kw: nullcontext())
    encoded = BytesIO()
    Image.new("RGB", (40, 30), "navy").save(encoded, format="PNG")
    requests = []
    def generated(*args, **kwargs):
        requests.append(args)
        return SimpleNamespace(raise_for_status=lambda: None,
            json=lambda: {"data": [{"b64_json": base64.b64encode(encoded.getvalue()).decode()}]})
    monkeypatch.setattr("requests.post", generated)
    context = ToolContext(role, role, read_scopes=(WriteScope("assets"),), write_scopes=(WriteScope("assets"),))
    result = services.image_generate(context, {"prompt": "abstract signal field"})
    target = tmp_path / result["path"]
    original = target.read_bytes()
    assert not result["reused"] and result["status"] == "committed"
    assert result["registration_note"] and result["owner_id"] == role
    assert "registration_note" not in services.artifacts.artifact(result["artifact_id"]).to_dict()

    def registration(asset_id):
        return shlex.join(["python", f"skills/{SKILL.name}/scripts/image.py", "register", ".",
            "--id", asset_id, "--path", result["path"], "--kind", "generated", "--slides", "1",
            "--purpose", "封面 AI 信号场", "--subject-region", "全幅背景",
            "--text-safe-region", "左下区域", "--notes", "概念示意，不冒充实验数据"])

    with pytest.raises(ToolContractError) as conflict:
        services.terminal(context, {"command": registration("cover-ai-signal-field")})
    assert conflict.value.code == "asset_write_conflict"
    assert result["artifact_id"] in str(conflict.value)
    assert services.terminal(context, {"command": registration(result["artifact_id"])})["returncode"] == 0
    stored = services.artifacts.catalog()["images"]
    assert len(stored) == 1 and stored[0]["artifact_id"] == result["artifact_id"]
    assert stored[0]["owner_id"] == role and stored[0]["sha256"] == result["sha256"]
    assert stored[0]["image_metadata"]["purpose"] == "封面 AI 信号场"
    assert stored[0]["image_metadata"]["slides"] == [1]
    assert stored[0]["image_metadata"]["subject_region"] == "全幅背景"
    assert stored[0]["image_metadata"]["text_safe_region"] == "左下区域"
    assert stored[0]["image_metadata"]["notes"] == "概念示意，不冒充实验数据"
    cached = services.image_generate(context, {"prompt": "abstract signal field"})
    assert cached["reused"] and cached["registration_note"] == result["registration_note"]
    assert cached["artifact_id"] == result["artifact_id"] and cached["image_metadata"] == stored[0]["image_metadata"]

    other = ToolContext("other-owner", "image", read_scopes=context.read_scopes, write_scopes=context.write_scopes)
    reused = services.image_generate(other, {"prompt": "abstract signal field"})
    assert reused["owner_id"] == role and reused["artifact_id"] == result["artifact_id"]
    assert reused["registration_note"] == result["registration_note"]
    with pytest.raises(ToolContractError) as denied:
        services.terminal(other, {"command": registration(result["artifact_id"])})
    assert denied.value.code == "asset_owner_conflict"
    assert target.read_bytes() == original and len(requests) == 1
    assert services.artifacts.catalog()["images"] == stored
    assert all(row["status"] != "open" for row in json.loads(services.artifacts.path.read_text())["transactions"].values())


def test_explicit_receipt_keeps_multiline_metadata_and_never_reads_markdown(tmp_path):
    services = services_for(tmp_path)
    Image.new("RGB", (32, 24), "orange").save(tmp_path / "assets/hero.png")
    catalog = tmp_path / "assets/catalog.md"
    catalog.write_text("# Notes\n- crop: |\n  user notes must not be parsed\n")
    metadata = {"kind": "user", "slides": [1, 3], "crop": "left subject\nretain hat",
        "subject_region": "0.32,0.12,0.48,0.28", "text_safe_region": "none",
        "notes": "crop command failed\nuse this evaluated alternative"}
    receipt = {"artifact_id": "hero", "path": "assets/hero.png", "status": "available",
        "image_metadata": metadata}
    before = catalog.read_bytes()
    services._ingest_image_receipts(image_context(), "MURAL_ASSET " + json.dumps(receipt))
    assert catalog.read_bytes() == before
    result = services.artifacts.catalog()["images"][0]
    assert result["image_metadata"] == metadata
    assert (result["pixel_width"], result["pixel_height"]) == (32, 24)
    services.artifacts.commit(owner_id="image-owner", artifact_ids=["hero"], source_hashes={})
    resumed = ArtifactStore(tmp_path, workspace_state=services.workspace)
    assert resumed.catalog()["images"][0]["image_metadata"] == metadata


def test_pending_image_registration_uses_existing_artifact_store(tmp_path):
    services = services_for(tmp_path)
    metadata = {"kind": "real", "download": "https://example.invalid/hero.png", "slides": [1]}
    record = services.artifacts.record_image_result(artifact_id="hero", owner_id="image-owner",
        path="assets/hero.png", status="pending", image_metadata=metadata)
    assert record.status.value == "declared"
    Image.new("RGB", (16, 12), "navy").save(tmp_path / "assets/hero.png")
    services.artifacts.record_image_result(artifact_id="hero", owner_id="image-owner",
        path="assets/hero.png", status="available")
    assert services.artifacts.catalog()["images"][0]["image_metadata"] == metadata


def test_image_receipt_cannot_register_outside_write_scope(tmp_path):
    services = services_for(tmp_path)
    receipt = {"artifact_id": "bad", "path": "inputs/private.png", "status": "pending",
        "image_metadata": {}}
    with pytest.raises(ToolContractError, match="不能登记"):
        services._ingest_image_receipts(image_context(), "MURAL_ASSET " + json.dumps(receipt))


def test_fetch_partial_failure_still_commits_successful_receipts(tmp_path, monkeypatch):
    services = services_for(tmp_path)
    for asset_id in ("hero", "second"):
        services.artifacts.record_image_result(artifact_id=asset_id, owner_id="image-owner",
            path=f"assets/{asset_id}.png", status="pending", image_metadata={"kind": "real", "download": "https://example.test/a.png"})
    receipt = {"artifact_id": "hero", "path": "assets/hero.png", "status": "available",
        "image_metadata": {"kind": "real", "slides": [1]}}
    def fail(*args, **kwargs):
        Image.new("RGB", (20, 10), "navy").save(tmp_path / "assets/hero.png")
        kwargs["on_stdout_line"]("MURAL_ASSET " + json.dumps(receipt))
        raise ToolExecutionError("skill_command_failed", "second asset failed",
            diagnostics={"stdout": "MURAL_ASSET " + json.dumps(receipt)})
    monkeypatch.setattr("runtime.production._run_command", fail)
    with pytest.raises(ToolExecutionError, match="second asset"):
        services.terminal(image_context(), {"command": f"python skills/{SKILL.name}/scripts/image.py fetch ."})
    assert services.artifacts.catalog()["images"][0]["sha256"]
    transactions = json.loads(services.artifacts.path.read_text())["transactions"]
    assert {row["status"] for row in transactions.values()} == {"committed", "aborted"}


def test_image_terminal_success_returns_compact_history_payload(tmp_path, monkeypatch):
    services = services_for(tmp_path)
    services.artifacts.record_image_result(artifact_id="hero", owner_id="image-owner",
        path="assets/hero.png", status="pending",
        image_metadata={"kind": "real", "download": "https://example.test/hero.png"})
    receipt = {"artifact_id": "hero", "path": "assets/hero.png", "status": "available",
        "image_metadata": {"kind": "real", "slides": [1]}}
    def succeed(*_args, **kwargs):
        Image.new("RGB", (20, 10), "navy").save(tmp_path / "assets/hero.png")
        kwargs["on_stdout_line"]("MURAL_ASSET " + json.dumps(receipt))
        return SimpleNamespace(returncode=0, stdout="download log\n" + "x" * 9000,
            stderr="warning\n" + "e" * 5000,
            diagnostics={"ref": "_trace/tool-output/fixture.jsonl",
                         "records": [{"asset_id": f"asset-{index}"} for index in range(40)]})
    monkeypatch.setattr("runtime.production._run_command", succeed)
    result = services.terminal(image_context(), {"command": f"python skills/{SKILL.name}/scripts/image.py fetch ."})
    assert result["images"][0]["artifact_id"] == "hero"
    assert "full_output_ref" not in result and "ref" not in result["diagnostics"]
    assert result["history_compacted"] is True
    assert "omitted" in result["stdout"] and "omitted" in result["stderr"]
    assert len(result["diagnostics"]["records"]) == IMAGE_HISTORY_DIAGNOSTIC_RECORDS


@pytest.mark.parametrize("role", ["orchestrator", "research", "image", "slide", "material"])
def test_removed_material_cli_cannot_execute_in_any_role(tmp_path, monkeypatch, role):
    services = services_for(tmp_path)
    monkeypatch.setattr("runtime.production._run_command",
        lambda *_a, **_k: pytest.fail("removed Material CLI must never execute"))
    command = f"python skills/{SKILL.name}/scripts/material.py parse . --attachment att-test"
    with pytest.raises(ToolContractError) as error:
        services.terminal(ToolContext("owner", role), {"command": command})
    assert error.value.code == "terminal_role_denied"
    assert not (SKILL / "scripts/material.py").exists()


def test_actual_crop_register_finalize_roundtrip_preserves_multiline_notes(tmp_path):
    services = services_for(tmp_path)
    write_geometry(tmp_path, geometry(width_px=1600, height_px=900))
    source = tmp_path / "inputs/jumia-channel.png"
    source.parent.mkdir(exist_ok=True)
    Image.new("RGB", (1440, 960), "orange").save(source)
    prefix = f"python skills/{SKILL.name}/scripts/image.py"
    context = image_context()
    crop = services.terminal(context, {"command": prefix +
        " crop . --source inputs/jumia-channel.png --output assets/hero.png --box 0.10,0.10,0.90,0.70"})
    asset_id = crop["images"][0]["artifact_id"]
    with Image.open(tmp_path / "assets/hero.png") as cropped:
        assert cropped.size == (1152, 576)
    notes = "crop was independently inspected\nsubject kept on left\n" + "long handoff " * 700
    services.terminal(context, {"command": prefix +
        f" register . --id {asset_id} --path assets/hero.png --kind material --slides 1,3"
        " --crop 'left subject\nkeep orange hat' --subject-region left --text-safe-region none"
        f" --notes '{notes}'"})
    before = services.artifacts.path.read_bytes()
    services.terminal(context, {"command": prefix + " finalize ."})
    assert services.artifacts.path.read_bytes() == before
    result = services.artifacts.catalog()["images"][0]
    assert result["image_metadata"]["notes"] == notes
    assert result["image_metadata"]["crop"] == "left subject\nkeep orange hat"
    catalog = (tmp_path / "assets/catalog.md").read_text()
    assert "keep orange hat" in catalog and "subject kept on left" in catalog
    from infer import build_inspect_handlers
    from runtime.tools import ToolCall, ToolRuntime, register_tools
    from test_native_inspect import _analyzer
    sheets = list((tmp_path / "assets").glob("contact-sheet-*.png"))
    assert len(sheets) == 1
    assert not sheets[0].is_symlink()
    assert sheets[0].read_bytes() == (tmp_path / "_trace/image" / sheets[0].name).read_bytes()
    runtime = ToolRuntime()
    register_tools(runtime, {"inspect": build_inspect_handlers(
        services.workspace, services.artifacts, None, _analyzer)[0]}, require_complete=False)
    preview = runtime.execute(context, ToolCall("own-sheet", "inspect", {
        "path": sheets[0].relative_to(tmp_path).as_posix(), "question": "Check my material batch"}))
    assert preview.ok, preview.content
    assert not json.loads(preview.content)["deck_page_inspection"]


def test_streamed_receipts_are_exact_despite_excerpt_limits(tmp_path):
    from runtime.diagnostics import OutputCapture
    services = services_for(tmp_path)
    capture = OutputCapture(on_stdout_line=lambda line: services._ingest_image_receipts(image_context(), line))
    capture.feed("stdout", b'Authorization: Bearer private-fixture-token\nprompt: normal source description\n')
    metadata = {"kind": "real", "slides": [1], "download": "https://example.invalid/a?token=private"}
    for number in range(40):
        line = ("MURAL_ASSET " + json.dumps({"artifact_id": f"image-{number}",
            "path": f"assets/{number}.png", "status": "pending", "image_metadata": metadata}) + "\n").encode()
        capture.feed("stdout", line[:100])
        capture.feed("stdout", line[100:])
    capture.finish(0, 1, "exited")
    state = json.loads(services.artifacts.path.read_text())
    assert len(state["artifacts"]) == 40
    assert all(record["image_metadata"] == metadata for record in state["artifacts"].values())
    assert "private-fixture-token" in capture.excerpts["stdout"]
    assert "prompt: normal source description" in capture.excerpts["stdout"]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_jsonl_receipts_preserve_unicode_separators(tmp_path, separator):
    from runtime.diagnostics import OutputCapture
    services = services_for(tmp_path)
    capture = OutputCapture(on_stdout_line=lambda line: services._ingest_image_receipts(image_context(), line))
    notes = "first" + separator + "second"
    line = "MURAL_ASSET " + json.dumps({"artifact_id": "hero", "path": "assets/hero.png",
        "status": "pending", "image_metadata": {"notes": notes}}, ensure_ascii=False) + "\n"
    encoded = line.encode()
    for offset in range(0, len(encoded), 7):
        capture.feed("stdout", encoded[offset:offset + 7])
    capture.finish(0, 1, "exited")
    record = json.loads(services.artifacts.path.read_text())["artifacts"]["hero"]
    assert record["image_metadata"]["notes"] == notes


def test_failed_receipt_callback_is_not_reentered_during_process_cleanup(tmp_path):
    from runtime.diagnostics import command_scope, run_command
    calls = []
    def fail(line):
        calls.append(line)
        raise RuntimeError("primary registration failure")
    with command_scope("failed-registration", {"workspace_root": str(tmp_path)}, lambda _: None):
        with pytest.raises(RuntimeError, match="primary registration failure"):
            run_command([sys.executable, "-c", "import os; os.write(1, b'first\\npartial')"],
                cwd=tmp_path, timeout_s=10, environment=dict(os.environ), on_stdout_line=fail)
    assert calls == ["first\n"]
    records = [json.loads(line) for path in (tmp_path / "_trace/tool-output").glob("*.jsonl")
        for line in path.read_text().splitlines()]
    assert records[-1]["record"]["termination"] == "interrupted"


@pytest.mark.parametrize("owner", ["image-owner", "orchestrator"])
def test_image_path_cannot_be_declared_under_a_second_id(tmp_path, owner):
    services = services_for(tmp_path)
    services.artifacts.declare(artifact_id="hero", kind="image", path="assets/hero.png", owner_id="image-owner")
    with pytest.raises(ArtifactStateError, match="reuse that artifact_id"):
        services.artifacts.declare(artifact_id="alias", kind="image", path="assets/hero.png", owner_id=owner)
    assert len(services.artifacts.catalog()["images"]) == 1


@pytest.mark.parametrize("status", ["pending", "unavailable", "available"])
def test_image_receipt_obeys_lease_for_every_result_status(tmp_path, status):
    services = services_for(tmp_path)
    store = services.artifacts
    store.declare(artifact_id="hero", kind="image", path="assets/hero.png", owner_id="image-owner")
    transaction = store.begin_transaction("image-owner", ["hero"])
    Image.new("RGB", (20, 10)).save(tmp_path / "assets/hero.png")
    arguments = dict(artifact_id="hero", owner_id="image-owner", path="assets/hero.png", status=status, reason="request result")
    with pytest.raises(ArtifactStateError, match="leased artifacts"):
        store.record_image_result(**arguments)
    store.record_image_result(**arguments, transaction_id=transaction)
    assert json.loads(store.path.read_text())["transactions"][transaction]["status"] == "committed"


@pytest.mark.parametrize("second_owner", ["image-owner", "orchestrator"])
def test_crop_reserves_path_before_io_without_holding_workspace_lock(tmp_path, monkeypatch, second_owner):
    services = services_for(tmp_path)
    Image.new("RGB", (20, 10)).save(tmp_path / "inputs/source.png")
    started, finish = threading.Event(), threading.Event()
    def write(*_args, **_kwargs):
        started.set()
        assert finish.wait(5), "slow image operation must not keep the workspace lock"
        Image.new("RGB", (10, 10)).save(tmp_path / "assets/hero.png")
        return SimpleNamespace(returncode=0, stdout="crop complete", stderr="", diagnostics={})
    monkeypatch.setattr("runtime.production._run_command", write)
    command = f"python skills/{SKILL.name}/scripts/image.py crop . --source inputs/source.png --output assets/hero.png --box 0,0,1,1"
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(services.terminal, image_context(), {"command": command})
        try:
            assert started.wait(5)
            pending = services.artifacts.catalog()["images"][0]
            assert pending["owner_id"] == "image-owner" and pending["status"] == "declared"
            assert not (tmp_path / "assets/hero.png").exists()
            context = ToolContext(second_owner, "image" if second_owner == "image-owner" else "orchestrator",
                read_scopes=image_context().read_scopes, write_scopes=image_context().write_scopes)
            second = pool.submit(services.terminal, context, {"command": command})
            with pytest.raises(ToolContractError) as failure:
                second.result(timeout=3)
            assert failure.value.code in {"asset_owner_conflict", "asset_write_conflict"}
        finally:
            finish.set()
        produced = first.result(timeout=5)["images"][0]
    assert produced["artifact_id"] == pending["artifact_id"] and produced["sha256"]
    assert len(services.artifacts.catalog()["images"]) == 1


def test_interrupted_crop_preserves_identity_and_releases_lease_for_retry(tmp_path, monkeypatch):
    services = services_for(tmp_path)
    Image.new("RGB", (20, 10)).save(tmp_path / "inputs/source.png")
    calls = []
    def write(*_args, **_kwargs):
        Image.new("RGB", (10, 10)).save(tmp_path / "assets/裁图.png")
        calls.append(True)
        if len(calls) == 1:
            raise KeyboardInterrupt("interrupted after bytes were produced")
        return SimpleNamespace(returncode=0, stdout="crop complete", stderr="", diagnostics={})
    monkeypatch.setattr("runtime.production._run_command", write)
    command = f"python skills/{SKILL.name}/scripts/image.py crop . --source inputs/source.png --output assets/裁图.png --box 0,0,1,1"
    with pytest.raises(KeyboardInterrupt):
        services.terminal(image_context(), {"command": command})
    declared = services.artifacts.catalog()["images"][0]
    assert declared["artifact_id"].isascii() and declared["status"] == "declared"
    resumed = services_for(tmp_path)
    produced = resumed.terminal(image_context(), {"command": command})["images"][0]
    assert produced["artifact_id"] == declared["artifact_id"] and produced["owner_id"] == "image-owner"
    assert produced["revision"] == 1
    assert {row["status"] for row in json.loads(resumed.artifacts.path.read_text())["transactions"].values()} == {"aborted", "committed"}


def test_crop_retains_predeclared_asset_id(tmp_path, monkeypatch):
    services = services_for(tmp_path)
    services.artifacts.declare(artifact_id="chosen-hero", kind="image", path="assets/hero.png", owner_id="image-owner")
    Image.new("RGB", (20, 10)).save(tmp_path / "inputs/source.png")
    def write(*_args, **_kwargs):
        Image.new("RGB", (10, 10)).save(tmp_path / "assets/hero.png")
        return SimpleNamespace(returncode=0, stdout="crop complete", stderr="", diagnostics={})
    monkeypatch.setattr("runtime.production._run_command", write)
    command = f"python skills/{SKILL.name}/scripts/image.py crop . --source inputs/source.png --output assets/hero.png --box 0,0,1,1"
    assert services.terminal(image_context(), {"command": command})["images"][0]["artifact_id"] == "chosen-hero"


def test_fetch_scopes_default_selection_to_current_owner(tmp_path, monkeypatch):
    services = services_for(tmp_path)
    for asset_id, owner in (("hero", "image-owner"), ("root-image", "orchestrator")):
        services.artifacts.record_image_result(artifact_id=asset_id, owner_id=owner, path=f"assets/{asset_id}.png",
            status="pending", image_metadata={"kind": "real", "download": "https://example.test/a.png"})
    def fetch(argv, **kwargs):
        assert argv[-2:] == ["--asset", "hero"]
        kwargs["on_stdout_line"]("MURAL_ASSET " + json.dumps({"artifact_id": "hero", "path": "assets/hero.png",
            "status": "unavailable", "reason": "http 404", "image_metadata": {"kind": "real"}}))
        return SimpleNamespace(returncode=0, stdout="failure receipt", stderr="", diagnostics={})
    monkeypatch.setattr("runtime.production._run_command", fetch)
    command = f"python skills/{SKILL.name}/scripts/image.py fetch ."
    result = services.terminal(image_context(), {"command": command})
    assert result["images"][0]["status"] == "unavailable"
    assert services.artifacts.artifact("root-image").revision == 1
    assert len(json.loads(services.artifacts.path.read_text())["transactions"]) == 1


def test_legacy_material_profile_does_not_restore_removed_fetch_gate(tmp_path):
    services = services_for(tmp_path)
    context = ToolContext("image-owner", "image", metadata={"authoring_profile": "material-grounded"})
    before = services.artifacts.path.read_bytes()
    result = services.terminal(
        context,
        {"command": f"python skills/{SKILL.name}/scripts/image.py fetch ."},
    )
    assert result["returncode"] == 0
    assert services.artifacts.path.read_bytes() == before


def test_image_receipt_cannot_commit_a_different_reserved_path(tmp_path, monkeypatch):
    services = services_for(tmp_path)
    Image.new("RGB", (10, 10)).save(tmp_path / "assets/hero.png")
    def wrong(*_args, **kwargs):
        kwargs["on_stdout_line"]("MURAL_ASSET " + json.dumps({"artifact_id": "hero", "path": "assets/other.png",
            "status": "available", "image_metadata": {}}))
    monkeypatch.setattr("runtime.production._run_command", wrong)
    with pytest.raises(ToolContractError) as failure:
        services.terminal(image_context(), {"command": f"python skills/{SKILL.name}/scripts/image.py register . --id hero --path assets/hero.png --kind generated --slides 1"})
    assert failure.value.code == "asset_receipt_mismatch"
    assert {row["status"] for row in json.loads(services.artifacts.path.read_text())["transactions"].values()} == {"aborted"}


@pytest.mark.parametrize("state", ["exited", "pid-reused"])
def test_image_lease_recovery_requires_proven_process_exit(tmp_path, state):
    services = services_for(tmp_path)
    store = services.artifacts
    store.declare(artifact_id="hero", kind="image", path="assets/hero.png", owner_id="image-owner")
    if state == "exited":
        child = subprocess.run([sys.executable, "-c",
            "import sys; from state.artifacts import ArtifactStore; "
            "print(ArtifactStore(sys.argv[1]).begin_transaction('image-owner', ['hero']))", str(tmp_path)],
            cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            capture_output=True, text=True, check=True)
        old_transaction = child.stdout.strip()
    else:
        old_transaction = store.begin_transaction("image-owner", ["hero"])
        saved = json.loads(store.path.read_text())
        saved["transactions"][old_transaction]["process"]["start"] = "different-process-start"
        store.path.write_text(json.dumps(saved))
    resumed = ArtifactStore(tmp_path)
    new_transaction = resumed.begin_transaction("image-owner", ["hero"])
    saved = json.loads(store.path.read_text())
    assert saved["transactions"][old_transaction]["status"] == "aborted"
    assert saved["transactions"][new_transaction]["status"] == "open"
    assert any(event.get("reason") == "lease_process_exited_or_replaced" for event in saved["events"])
    assert resumed.artifact("hero").owner_id == "image-owner"
    resumed.abort_transaction(new_transaction, owner_id="image-owner")


@pytest.mark.parametrize("state", ["alive", "other-host", "unknown", "unconfirmed", "external-process"])
def test_image_lease_is_not_stolen_without_exit_evidence(tmp_path, monkeypatch, state):
    services = services_for(tmp_path)
    store = services.artifacts
    store.declare(artifact_id="hero", kind="image", path="assets/hero.png", owner_id="image-owner")
    transaction = store.begin_transaction("image-owner", ["hero"])
    saved = json.loads(store.path.read_text())
    if state == "other-host":
        saved["transactions"][transaction]["process"]["host"] = "different-machine"
    elif state == "unknown":
        del saved["transactions"][transaction]["process"]
    elif state == "unconfirmed":
        monkeypatch.setattr("state.artifacts._process_start_id", lambda _pid: None)
    elif state == "external-process":
        saved["transactions"][transaction]["process"]["start"] = "different-process-start"
        saved["transactions"][transaction]["process"]["external_process"] = True
    store.path.write_text(json.dumps(saved))
    before = store.path.read_bytes()
    with pytest.raises(ArtifactStateError, match="already leased"):
        ArtifactStore(tmp_path).begin_transaction("image-owner", ["hero"])
    assert store.path.read_bytes() == before
    store.abort_transaction(transaction, owner_id="image-owner")
