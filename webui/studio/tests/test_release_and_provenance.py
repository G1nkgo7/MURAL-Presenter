"""K1 · WebUI observability: error classification + Skill provenance card.

Two independent WebUI-only gaps:
1. A release-guard rejection (verify_runtime_release hash mismatch) must classify
   as ``release_hash_mismatch``, NOT ``api_failed`` — the harness rejects the task
   before the first model request, so it is not a Provider/API failure.
2. The main SKILL.md + role cards are injected as system prompt (no model
   read_file), so the WebUI shows a provenance card from real on-disk
   _trace/skill-provenance.json — safe metadata only, never secrets/thinking.
"""
import json

from app import engine, main


# The bare error the runner records for deck 195 (real result.json).
_BARE_EXIT_ERROR = "MURAL v1 exited with code 1"
# The real release-guard traceback tail from webui/studio/data/jobs/195.log.
_RELEASE_LOG = (
    "Traceback (most recent call last):\n"
    '  File ".../runtime/release_guard.py", line 217, in verify_runtime_release\n'
    '    raise RuntimeError(f"frozen MURAL release hash mismatch: {details}")\n'
    "RuntimeError: frozen MURAL release hash mismatch: "
    "skill expected=c717e0a8 actual=3544ac21; harness expected=ca0b7ffd actual=83acb25b\n"
)
# A genuine Provider failure episode from a job log.
_API_LOG = (
    "[provider] request started\n"
    "[api failed] 503 upstream channel unavailable\n"
    "agent loop did not complete: api_failed\n"
)


def _write_log(deck_id: int, text: str) -> None:
    engine.log_path(deck_id).write_text(text, encoding="utf-8")


def test_release_hash_mismatch_classifies_explicitly(tmp_path) -> None:
    # The release-guard signature lives in the job log, not the bare error.
    deck_id = 990001
    _write_log(deck_id, _RELEASE_LOG)
    kind = main._classify_deck_error(str(tmp_path), deck_id, _BARE_EXIT_ERROR)
    assert kind == "release_hash_mismatch"
    engine.log_path(deck_id).unlink()


def test_genuine_api_failure_stays_api_failed(tmp_path) -> None:
    deck_id = 990002
    _write_log(deck_id, _API_LOG)
    kind = main._classify_deck_error(str(tmp_path), deck_id, "generation error")
    assert kind == "api_failed"
    engine.log_path(deck_id).unlink()


def test_release_mismatch_in_error_string_without_log(tmp_path) -> None:
    # Signature may also arrive directly in the result.json error text.
    deck_id = 990003
    kind = main._classify_deck_error(
        str(tmp_path), deck_id,
        "engine: frozen MURAL release hash mismatch: skill expected=... actual=...",
    )
    assert kind == "release_hash_mismatch"


def test_unclassifiable_failure_returns_none(tmp_path) -> None:
    deck_id = 990004
    _write_log(deck_id, "some unrelated rendering warning\n")
    assert main._classify_deck_error(str(tmp_path), deck_id, "生成失败(无成稿)") is None
    engine.log_path(deck_id).unlink()


def test_disabled_account_projection_corrects_old_paused_state_without_rewriting_trace():
    progress = {"status": "paused_recoverable", "error": "上游暂时不可用，可继续生成"}
    main._apply_recovery_info(progress, {
        "eligible": False, "exit_reason": "api_failed", "provider_access_disabled": True,
    })
    assert progress["status"] == "failed"
    assert progress["error_kind"] == "provider_access_disabled"
    assert not progress["resume_eligible"]
    assert "Usage Policy" in progress["error"]
    assert "供应商" in progress["error"]
    assert "可继续" not in progress["error"]


def test_regular_recovery_projection_keeps_original_status_and_reason():
    progress = {"status": "paused_recoverable", "error": "first byte timeout"}
    main._apply_recovery_info(progress, {"eligible": True, "exit_reason": "api_failed"})
    assert progress["resume_eligible"]
    assert progress["status"] == "paused_recoverable"
    assert progress["error"] == "first byte timeout"
    assert "error_kind" not in progress


def test_retired_account_projection_offers_manual_resume_with_current_pool():
    progress = {"status": "failed"}
    main._apply_recovery_info(progress, {
        "eligible": True, "exit_reason": "api_failed", "provider_access_disabled": True,
        "provider_channel_retired": True,
    })
    assert progress["resume_eligible"]
    assert progress["status"] == "paused_recoverable"
    assert "手动继续" in progress["error"]
    assert "不会再请求已停用账号" in progress["error"]


def test_resumed_job_is_not_projected_as_its_previous_provider_failure():
    for status in ("waiting", "queued", "running"):
        progress = {"status": status, "error": None}
        main._apply_recovery_info(progress, {
            "eligible": True, "exit_reason": "api_failed", "provider_access_disabled": True,
            "provider_channel_retired": True,
        })
        assert progress["status"] == status
        assert not progress["resume_eligible"]
        assert progress["error"] is None


def _write_provenance(run_dir, payload) -> None:
    trace_dir = run_dir / "_trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    (trace_dir / "skill-provenance.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# Shape mirrors a real deck 196 _trace/skill-provenance.json.
_REAL_PROVENANCE = {
    "main_sha256": "aca9a1bc8925355f120758f1ace23319eb64bb9a611fdd87d7424e2293024915",
    "role_sha256": {
        "image": "8d97333db6f24f8e5a1cd7f7d706af9205ec8b65c09fcfdacbb536a4490a6a31",
        "material": "7c607990251f9219c14661cf96f3026d33d82c4cb43885cf6525b48d58ca71fd",
        "research": "56bd693a0bd35229bafb72277b178b5f02b426d85ef4ec6c00dffc3d6d26b465",
        "slide": "1d5beefd6fa2d8ebe1cc3fcba6f2f5d39fdcaed1d67ea871f611196945c8335b",
    },
    "schema": "mural.skill-provenance.v1",
    "skill_name": "mural-presenter-v1",
    "source_path": "/abs/secret/path/skills/mural-presenter-v1",
    "tree_sha256": "e67303a5235c115743f2143f492becae8bee7a42b65e6bc912a42e2a9476acfd",
    "workspace_path": "skills/mural-presenter-v1",
}


def test_provenance_projects_safe_metadata(tmp_path) -> None:
    _write_provenance(tmp_path, _REAL_PROVENANCE)
    payload = main._skill_provenance_payload(str(tmp_path))
    assert payload is not None
    assert payload["skill_name"] == "mural-presenter-v1"
    assert payload["main_path"] == "skills/mural-presenter-v1/SKILL.md"
    # Only a short hash prefix, never the full sha.
    assert payload["main_sha256"] == "aca9a1bc8925"
    assert len(payload["main_sha256"]) == 12
    roles = {r["role"]: r for r in payload["roles"]}
    assert roles["slide"]["path"] == "roles/slide.md"
    assert roles["slide"]["sha256"] == "1d5beefd6fa2"
    assert payload["source"] == "trace/skill-provenance.json"


def test_provenance_never_leaks_secrets_or_thinking(tmp_path) -> None:
    leaky = dict(_REAL_PROVENANCE)
    # Even if the file grew hostile fields, the projection must drop them.
    leaky["system_prompt"] = "SYSTEM PROMPT BODY"
    leaky["thinking"] = "hidden chain of thought"
    leaky["thinking_signature"] = "sig-abc"
    leaky["api_key"] = "sk-secret"
    _write_provenance(tmp_path, leaky)
    blob = json.dumps(main._skill_provenance_payload(str(tmp_path)))
    for banned in ("SYSTEM PROMPT BODY", "hidden chain of thought", "sig-abc",
                   "sk-secret", "/abs/secret/path"):
        assert banned not in blob
    # Full (64-char) hashes must never be surfaced either.
    assert _REAL_PROVENANCE["main_sha256"] not in blob


def test_missing_provenance_degrades_gracefully(tmp_path) -> None:
    # Old-format workspaces without the file: no card, no crash.
    assert main._skill_provenance_payload(str(tmp_path)) is None


def test_empty_provenance_returns_none(tmp_path) -> None:
    _write_provenance(tmp_path, {})
    assert main._skill_provenance_payload(str(tmp_path)) is None
