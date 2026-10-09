import json
from pathlib import Path


from studio.app import trace as TRACE


def test_final_image_handoff_overrides_earlier_incomplete_exit(tmp_path: Path):
    trace = tmp_path / "_trace"
    image_dir = trace / "subagents" / "image"
    image_dir.mkdir(parents=True)
    (image_dir / "config.json").write_text(
        json.dumps({
            "label": "image",
            "exit_reason": "completed_with_fallback",
            "image_execution_status": "completed_with_fallback",
        }),
        encoding="utf-8",
    )
    (trace / "image-handoff.json").write_text(
        json.dumps({
            "schema": "mural.image-handoff.v2",
            "execution_status": "completed",
            "status": "completed_with_fallback",
            "assets": [
                {"id": "poster", "status": "unavailable", "pages": [1, 7]},
                {"id": "still", "status": "ready", "pages": [7]},
            ],
            "ready_pages": [7],
            "failed_pages": [1],
        }),
        encoding="utf-8",
    )
    timings = {
        "image": {
            "status": "failed",
            "exit_reason": "incomplete_closure",
        }
    }

    TRACE._promote_image_terminal(timings, str(tmp_path), {})

    assert timings["image"]["status"] == "complete"
    assert timings["image"]["completion_status"] == "completed_with_fallback"
    assert timings["image"]["exit_reason"] == "completed_with_fallback"


def test_legacy_partial_handoff_is_shown_as_completed_fallback(tmp_path: Path):
    trace = tmp_path / "_trace"
    trace.mkdir()
    (trace / "image-handoff.json").write_text(
        json.dumps({
            "schema": "mural.image-handoff.v1",
            "ready_pages": [1, 7],
            "failed_pages": [9],
        }),
        encoding="utf-8",
    )
    timings = {"image": {"status": "failed"}}

    TRACE._promote_image_terminal(timings, str(tmp_path), {})

    assert timings["image"]["status"] == "complete"
    assert timings["image"]["completion_status"] == "completed_with_fallback"
