import json
import os
from pathlib import Path

from app import trajectory_monitor


def _sample(root: Path, run_name: str, sample_name: str, query: str, model: str, stamp: int) -> None:
    sample = root / run_name / sample_name
    (sample / "_trace").mkdir(parents=True)
    (sample / "_state").mkdir()
    (sample / "_trace" / "live-events.jsonl").write_text("{}\n", encoding="utf-8")
    (sample / "_state" / "run.json").write_text(
        json.dumps({"raw_user_query": query, "provider": {"model": model}}),
        encoding="utf-8",
    )
    os.utime(sample, (stamp, stamp))


def test_extra_query_keeps_cross_model_runs_separate_and_legacy_link_resolves(
    tmp_path: Path, monkeypatch
) -> None:
    query = "same comparison query"
    _sample(tmp_path, "gpt-run", "gpt-sample", query, "gpt-6-astra/azure/sfa", 100)
    _sample(tmp_path, "opus-run", "opus-sample", query, "claude-opus-5", 200)
    missing_catalog = tmp_path / "missing-catalog.jsonl"

    monkeypatch.setattr(trajectory_monitor, "configured_roots", lambda: [tmp_path])
    monkeypatch.setattr(trajectory_monitor, "query_catalog_path", lambda: missing_catalog)
    trajectory_monitor._CATALOG_RECORD_CACHE = None
    trajectory_monitor._QUERY_CACHE = None

    records = [record for record in trajectory_monitor._catalog_records() if record["query"] == query]

    assert len(records) == 2
    assert {record["config"]["model"] for record in records} == {
        "gpt-6-astra/azure/sfa",
        "claude-opus-5",
    }
    assert len({record["id"] for record in records}) == 2

    legacy_id = trajectory_monitor._catalog_deck_id(trajectory_monitor._legacy_extra_key(query))
    assert trajectory_monitor._resolve_catalog_deck(legacy_id)["config"]["model"] == "claude-opus-5"
