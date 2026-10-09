"""自然分页文稿的版本投影、显式事实修订与真实结构事务。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image
import pytest

from owner.registry import OwnerRegistry
from runtime.shared_objects import SharedSkillStore
from state.artifacts import ArtifactStore
from state.revisions import RevisionTransaction
from state.workspace import WorkspaceState


SKILL = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"


@pytest.fixture
def natural_deck(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(SKILL / "scripts"))
    from _internal.deck_core import _load_plans
    from _internal.deck_state import compile_deck_state

    workspace = WorkspaceState(tmp_path)
    SharedSkillStore.for_workspace(tmp_path).pin(workspace.paths, SKILL, skill_name=SKILL.name)
    workspace.register_pages([1, 2, 3])
    (tmp_path / "config.json").write_text(json.dumps({
        "working_language": "zh", "presentation_language": "en",
        "target_page_count": 3,
    }), encoding="utf-8")
    (tmp_path / "base.css").write_text(
        ":root { --canvas-w: 1600px; --canvas-h: 900px; }\n", encoding="utf-8")
    (tmp_path / "deck.md").write_text(
        "# Audience goal\n\n## 整体设计规范\n\n保留当前网格与字体。\n\n"
        "## Pages\n\n分页正文在 briefs，标题不承担机器配置。\n\n",
        encoding="utf-8",
    )
    (tmp_path / "briefs").mkdir(exist_ok=True)
    owners = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    for number, title in enumerate(("P03 — Audience-facing example", "Comparison", "Decision"), 1):
        brief = (
            f"# {title}\n\n"
            "## 本页任务\n\n说明这一页如何推进论证。\n\n"
            "## 页面内容与证据\n\n共同来源 https://example.org/report，Table-1。\n\n"
            "## 视觉表达与约束\n\n保留数据口径。\n\n"
            f"## 讲稿\n\n本页讲述 {number}。\n\n- 保留列表。\n- 保留限定。\n\n"
        )
        (tmp_path / "briefs" / f"slide_{number:02d}.md").write_text(brief, encoding="utf-8")
        workspace.paths.page_html(number).write_text(
            f'<main id="slide-{number:02d}" data-slide="{number:02d}">{title}</main>',
            encoding="utf-8",
        )
        record = owners.accept(role="slide", pages=[number], group_id=f"owner-page-{number}")
        owners.transition(record.owner_id, attempt=1, status="running")
        owners.transition(record.owner_id, attempt=1, status="completed")
    compile_deck_state(tmp_path, *_load_plans(tmp_path, 3))
    return workspace, ArtifactStore(tmp_path, workspace_state=workspace), owners


def _read(root: Path, relative: str) -> dict:
    return json.loads((root / relative).read_text(encoding="utf-8"))


def _prepare(deck, operations):
    from _internal.revision_router import compile_revision_impact

    root = deck[0].paths.root
    (root / "revision-operations.json").write_text(json.dumps({
        "schema": "mural.edit-operations.v1", "base_deck_version": 1,
        "revision_id": "natural-revision", "operations": operations,
    }), encoding="utf-8")
    return compile_revision_impact(root)


def test_graph_has_registry_identity_without_prose_or_fake_group_projections(natural_deck):
    workspace = natural_deck[0]
    index = _read(workspace.paths.root, "_state/page-index.json")
    graph = _read(workspace.paths.root, "_state/deck-graph.json")
    assert [row["page_uid"] for row in index["pages"]] == [page.page_uid for page in workspace.pages()]
    assert "production_groups" not in graph
    for row in [*index["pages"], *graph["nodes"].values()]:
        assert not {"page_family", "production_group", "identity_key"} & row.keys()
    assert {edge["type"] for edge in graph["edges"]} == {"narrative", "shared_system"}
    assert graph["global_contracts"]["creation_config"] == _read(workspace.paths.root, "config.json")
    assert index["pages"][0]["title"] == "P03 — Audience-facing example"


@pytest.mark.parametrize("field,value", [
    ("working_language", "en"), ("presentation_language", "zh"),
    ("target_page_count", 4), ("speech_language", "zh"),
])
def test_current_config_changes_plan_fingerprint_without_rewriting_page_sources(natural_deck, field, value):
    from _internal.deck_core import _load_plans
    from _internal.deck_state import compile_deck_state

    root = natural_deck[0].paths.root
    before = _read(root, "_state/page-index.json")
    config = _read(root, "config.json")
    config[field] = value
    (root / "config.json").write_text(json.dumps(config), encoding="utf-8")
    compile_deck_state(root, *_load_plans(root, 3))
    after = _read(root, "_state/page-index.json")
    assert after["plan_fingerprint"] != before["plan_fingerprint"]
    assert after["plan_epoch"] == before["plan_epoch"] + 1
    assert after["pages"] == before["pages"]


@pytest.mark.parametrize("positions", [[2], [2, 3]])
def test_section_scope_uses_only_explicit_targets_not_page_intent(natural_deck, positions):
    from _internal.deck_core import _load_plans
    from _internal.deck_state import compile_deck_state

    workspace = natural_deck[0]
    root = workspace.paths.root
    path = root / "briefs/slide_02.md"
    path.write_text(path.read_text(encoding="utf-8").replace(
        "# Comparison\n", "# Comparison\n本页是转场，后面进入下一章。\n- page_type: section-divider\n", 1), encoding="utf-8")
    compile_deck_state(root, *_load_plans(root, 3))
    uids = [page.page_uid for page in workspace.pages()]
    assert not any(edge["type"] == "section" for edge in _read(root, "_state/deck-graph.json")["edges"])
    assert all("page_type" not in row for row in _read(root, "_state/page-index.json")["pages"])
    impact = _prepare(natural_deck, [{
        "operation_id": "section-change", "kind": "section", "target_positions": positions,
    }])
    assert set(impact["affected_pages"]) == {uids[position - 1] for position in positions}


@pytest.mark.parametrize("kind", ["evidence", "data", "terminology", "visual_encoding"])
def test_fact_revisions_keep_explicit_multi_page_targets_and_original_owners(natural_deck, kind):
    workspace, _, owners = natural_deck
    uids = [page.page_uid for page in workspace.pages()]
    impact = _prepare(natural_deck, [{
        "operation_id": "fix-fact", "kind": kind, "targets": [uids[0], uids[2]],
    }])
    assert set(impact["affected_pages"]) == {uids[0], uids[2]}
    assert len(impact["assignments"]) == 2
    assert {row["owner_id"] for row in impact["assignments"]} == {
        owners.page_owner(1).owner_id, owners.page_owner(3).owner_id,
    }
    assert all(row["owner_policy"] == "reuse_owner" for row in impact["assignments"])


@pytest.mark.parametrize("kind", ["insert_page", "move_page", "delete_page"])
def test_structure_preserves_natural_titles_speech_config_and_explicit_assets(natural_deck, monkeypatch, kind):
    from geometry_fixture import require_browser
    from _internal.deck_plan import read_deck, speech_section
    from _internal import revision_structure

    workspace, artifacts, owners = natural_deck
    root = workspace.paths.root
    require_browser(root / "_runtime/tmp")
    original = [page.page_uid for page in workspace.pages()]
    source = {
        uid: (root / "briefs" / f"slide_{number:02d}.md").read_bytes()
        for number, uid in enumerate(original, 1)
    }
    shared_before = {name: (root / name).read_bytes() for name in ("deck.md", "config.json", "base.css")}
    picture = root / "assets/shared.png"
    Image.new("RGB", (4, 3), "navy").save(picture)
    image = artifacts.record_image_result(
        artifact_id="shared-figure", owner_id="orchestrator", path=picture,
        status="available", image_metadata={"slides": [1, 3], "notes": "explicit consumers"},
    )
    image_hash = image.sha256
    operation = {"operation_id": "change-order", "kind": kind}
    if kind == "insert_page":
        operation.update(after_page_uid=original[0], new_page_uid="pg-added")
        expected = [original[0], "pg-added", original[1], original[2]]
    elif kind == "move_page":
        operation.update(targets=[original[0]], after_page_uid=original[2])
        expected = [original[1], original[2], original[0]]
    else:
        operation.update(targets=[original[1]])
        expected = [original[0], original[2]]
    _prepare(natural_deck, [operation])

    def execute(token, _lock_fd):
        with monkeypatch.context() as patch:
            patch.setenv("MURAL_REVISION_TRANSACTION", token)
            flags = {"revision_id": "natural-revision", "operation_id": "change-order"}
            if kind == "insert_page":
                return revision_structure.insert_page(root, new_uid="pg-added", after_uid=original[0], **flags)
            if kind == "move_page":
                return revision_structure.move_page(root, page_uid=original[0], after_uid=original[2], **flags)
            return revision_structure.delete_page(root, page_uid=original[1], **flags)

    RevisionTransaction(workspace, artifacts, owners).run(execute)
    assert [page.page_uid for page in workspace.pages()] == expected
    assert {name: (root / name).read_bytes() for name in shared_before} == shared_before
    _, plans = read_deck(root, len(expected))
    index = _read(root, "_state/page-index.json")
    for position, uid in enumerate(expected, 1):
        actual = (root / "briefs" / f"slide_{position:02d}.md").read_bytes()
        if uid in source:
            assert actual == source[uid]
            assert plans[position - 1]["speech"] == speech_section(source[uid].decode("utf-8"))
            assert index["pages"][position - 1]["source_plan_sha256"] == hashlib.sha256(
                plans[position - 1]["text"].encode("utf-8")
            ).hexdigest()
        else:
            assert actual.decode("utf-8").startswith("# Inserted page\n")
            assert [line for line in actual.decode("utf-8").splitlines() if line.startswith("## ")] == [
                "## 本页任务", "## 页面内容与证据", "## 视觉表达与约束", "## 讲稿",
            ]
            assert "page_type" not in plans[position - 1]
            assert plans[position - 1]["speech"] == ""
    updated_image = artifacts.artifact("shared-figure")
    assert updated_image.image_metadata["slides"] == sorted(expected.index(original[n - 1]) + 1 for n in (1, 3))
    assert updated_image.sha256 == image_hash
    assert updated_image.image_metadata["notes"] == "explicit consumers"
