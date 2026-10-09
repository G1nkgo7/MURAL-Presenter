"""纯 v2 结构修订：真实 Skill CLI 与内核状态一起验收，不运行模型。"""
from geometry_fixture import geometry, write_geometry
import json
import os
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from owner.registry import OwnerRegistry
from runtime.config import RunSpec
from runtime.production import ProductionSettings, ProductionToolServices, read_scopes_for_role
from runtime.tools import ToolContext
from state.artifacts import ArtifactStore
from state.revisions import MARKER, RevisionTransaction
from state.workspace import WorkspaceState, PageLifecycle

SKILL = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"


@pytest.fixture
def deck(tmp_path, monkeypatch, request):
    monkeypatch.syspath_prepend(str(SKILL / "scripts"))
    from _internal.deck_core import _load_plans
    from _internal.deck_state import compile_deck_state
    from _internal.revision_structure import _insert_plan
    workspace = WorkspaceState(tmp_path)
    from runtime.shared_objects import SharedSkillStore
    SharedSkillStore.for_workspace(tmp_path).pin(workspace.paths, SKILL, skill_name=SKILL.name)
    write_geometry(tmp_path, geometry(width_px=1600, height_px=1200))
    workspace.register_pages([1, 2, 3])
    (tmp_path / "deck.md").write_text("""# Test
- title: Test Deck
## Brief
- page_count: 3
- language: en
- audience: developers
- image_mode: none
- bitmap_strategy: not-beneficial
- bitmap_rationale: code diagram
- ownership_topology: single
- ownership_rationale: independent pages
- rationale: test revision contract
## Theme Tokens
```css
:root { --content-canvas: #fff; --ink: #111; --accent: #f60; }
```
""")
    briefs = tmp_path / "briefs"
    briefs.mkdir(exist_ok=True)
    for page in (1, 2, 3):
        (briefs / f"slide_{page:02d}.md").write_text(
            _insert_plan().replace("Inserted page", f"Topic {page}"))
    artifacts = ArtifactStore(tmp_path, workspace_state=workspace)
    owners = OwnerRegistry(tmp_path, deck_id=workspace.deck_id)
    direct_pages = set(getattr(request, "param", ()))
    for page in (1, 2, 3):
        if page in direct_pages:
            owner = owners.claim_orchestrator_page(page)
        else:
            owner = owners.accept(role="slide", pages=[page], group_id=f"group-{page}")
            owners.transition(owner.owner_id, attempt=1, status="running")
            owners.transition(owner.owner_id, attempt=1, status="completed")
        workspace.paths.page_html(page).write_text(
            f'<main id="slide-{page:02d}" data-slide="{page:02d}">Topic {page}</main>'
            f'<style>#slide-{page:02d} {{ color: rgb({page * 20}, 0, 0); }}</style>'
            f'<script>window.pageRoot = document.getElementById("slide-{page:02d}");</script>')
        artifacts.declare(artifact_id=f"page-html-{page:02d}", kind="page_html", path=workspace.paths.page_html(page), owner_id=owner.owner_id, page=page)
        artifacts.commit(owner_id=owner.owner_id, artifact_ids=[f"page-html-{page:02d}"], source_hashes={})
    compile_deck_state(tmp_path, *_load_plans(tmp_path, 3))
    snapshot = SimpleNamespace(name=SKILL.name, target=SKILL, tree_sha256="test-command-contract",
        resolve_skill_path=lambda path: SKILL / path.removeprefix("skills/mural-presenter-v1/"))
    service = ProductionToolServices(RunSpec.from_inputs(raw_user_query="three pages", output_workspace=tmp_path, environment={}),
        snapshot, ProductionSettings(skill_source=SKILL), dict(os.environ), workspace, artifacts, None, None)
    return workspace, artifacts, owners, service


def command(deck, action, flags=""):
    return deck[3].terminal(ToolContext(
        "orchestrator",
        "orchestrator",
        read_scopes=read_scopes_for_role(
            "orchestrator", skill_name="mural-presenter-v1"
        ),
    ),
        {"command": f"python skills/mural-presenter-v1/scripts/orchestrator.py {action} . {flags}"})


def prepare(deck, operation):
    root = deck[0].paths.root
    (root / "revision-operations.json").write_text(json.dumps({"schema": "mural.edit-operations.v1", "base_deck_version": 1,
        "revision_id": "revision-1", "operations": [{"operation_id": "op-1", **operation}]}))
    command(deck, "prepare-revision", "--revision-id revision-1")


def ready(deck):
    workspace, artifacts, *_ = deck
    def renderer(_html, png, _canvas):
        Image.new("RGB", (_canvas["width_px"], _canvas["height_px"])).save(png)
        return {"geometry": _canvas, "loaded_resources": [], "failed_resources": []}
    for page in workspace.pages():
        artifacts.render_pages([page.page], renderer)
        artifacts.record_inspection(page.page, inspection_id=f"inspection-{page.page}")
        workspace.mark_ready(page.page)


def register_image(deck, name, **metadata):
    path = deck[0].paths.assets / f"{name}.png"
    Image.new("RGB", (32, 24), "navy").save(path)
    return deck[1].record_image_result(artifact_id=name, owner_id="image-owner", path=path,
        status="available", image_metadata=metadata)


def test_skill_page_index_uses_kernel_identity(deck):
    workspace = deck[0]
    index = json.loads((workspace.paths.state / "page-index.json").read_text())
    assert index["deck_id"] == workspace.deck_id
    assert [row["page_uid"] for row in index["pages"]] == [page.page_uid for page in workspace.pages()]


@pytest.mark.parametrize("dynamic", [False, True])
def test_delete_preserves_image_used_by_undeclared_retained_consumer(deck, dynamic):
    workspace, store, *_ = deck
    image = register_image(deck, "reused", slides=[2])
    html = workspace.paths.page_html(1)
    html.write_text(html.read_text() + ('<script>window.asset="reused";</script>' if dynamic else '<img src="assets/reused.png">'))
    page = store.artifact("page-html-01")
    store.commit(owner_id=page.owner_id, artifact_ids=[page.artifact_id], source_hashes={})
    if dynamic:
        def renderer(_html, png, canvas):
            Image.new("RGB", (canvas["width_px"], canvas["height_px"])).save(png)
            return {"geometry": canvas, "loaded_resources": [image.path], "failed_resources": []}
        store.render_pages([1], renderer)
    ready(deck)
    before = workspace.page(1)
    uid = workspace.page(2).page_uid
    prepare(deck, {"kind": "delete_page", "targets": [uid]})
    command(deck, "delete-page", f"--revision-id revision-1 --operation-id op-1 --uid {uid}")
    assert store.artifact("reused").image_metadata["slides"] == [1]
    assert workspace.page(1).page_uid == before.page_uid
    assert workspace.page(1).lifecycle == PageLifecycle.READY
    assert (workspace.paths.root / image.path).is_file()
    store.validate_render_receipt(1)


def test_delete_syncs_owners_artifacts_and_requires_new_inspection(deck):
    workspace, artifacts, owners, _ = deck
    original = workspace.pages()
    ready(deck)
    prepare(deck, {"kind": "delete_page", "targets": [original[1].page_uid]})
    flags = f"--revision-id revision-1 --operation-id op-1 --uid {original[1].page_uid}"
    command(deck, "delete-page", flags)
    assert [page.page_uid for page in workspace.pages()] == [original[0].page_uid, original[2].page_uid]
    assert workspace.page(1).lifecycle == PageLifecycle.READY
    assert workspace.page(2).lifecycle == PageLifecycle.AUTHORED
    assert "Topic 3" in workspace.paths.page_html(2).read_text()
    assert not workspace.paths.page_html(3).exists()
    assert all(owner.pages for owner in owners.list_current() + owners.list_archived())
    assert any(owner.pages == (3,) for owner in owners.list_archived())
    record = artifacts.artifact("page-html-02")
    assert record.page == 2 and record.sha256 == workspace.page(2).html_sha256
    before = workspace.path.read_bytes()
    command(deck, "delete-page", flags)
    assert workspace.path.read_bytes() == before
    with pytest.raises(ValueError, match="revision commit requires current HTML/PNG inspections"):
        command(deck, "commit-revision", "--revision-id revision-1 --expected-base-version 1")
    ready(deck)
    command(deck, "commit-revision", "--revision-id revision-1 --expected-base-version 1")
    assert json.loads(workspace.path.read_text())["deck_version"] == 2
    archived = next(owner for owner in owners.list_archived() if owner.group_id == "group-3")
    reassigned = owners.accept(role="slide", pages=[2], group_id="group-3")
    assert reassigned.owner_id != archived.owner_id and reassigned.owner_id.endswith("-e2")
    assert sum(2 in owner.pages for owner in owners.list_current()) == 1


@pytest.mark.parametrize("deck", [(1, 3)], indirect=True)
@pytest.mark.parametrize("kind", ["move_page", "delete_page"])
def test_structure_remaps_direct_pages_without_archiving_main_owner(deck, kind):
    workspace, artifacts, owners, _ = deck
    original = workspace.pages()
    main = owners.get("orchestrator")
    ready(deck)
    operation = {"kind": kind, "targets": [original[2 if kind == "move_page" else 0].page_uid]}
    if kind == "move_page":
        operation["before_page_uid"] = original[0].page_uid
    prepare(deck, operation)
    flags = "--revision-id revision-1 --operation-id op-1 --uid " + operation["targets"][0]
    if kind == "move_page":
        flags += " --before " + original[0].page_uid
    command(deck, kind.replace("_", "-"), flags)
    expected = (1, 2) if kind == "move_page" else (2,)
    current = owners.get("orchestrator")
    assert current.pages == expected
    assert (current.owner_id, current.role, current.group_id) == ("orchestrator",) * 3
    assert current.attempt == main.attempt and current.status == main.status
    assert all(owner.role != "orchestrator" for owner in owners.list_archived())
    assert any(owner.group_id == "group-2" for owner in owners.list_archived())
    for page in current.pages:
        assert owners.page_owner(page).owner_id == "orchestrator"
        assert artifacts.artifact(f"page-html-{page:02d}").owner_id == "orchestrator"
        assert workspace.page(page).lifecycle == PageLifecycle.AUTHORED
    with pytest.raises(ValueError, match="revision commit requires current HTML/PNG inspections"):
        command(deck, "commit-revision", "--revision-id revision-1 --expected-base-version 1")
    ready(deck)
    command(deck, "commit-revision", "--revision-id revision-1 --expected-base-version 1")
    resumed = OwnerRegistry(workspace.paths.root, deck_id=workspace.deck_id)
    assert resumed.get("orchestrator") == current


@pytest.mark.parametrize("deck", [(2,)], indirect=True)
def test_deleting_last_direct_page_keeps_main_identity_without_claiming_other_pages(deck):
    workspace, artifacts, owners, _ = deck
    main = owners.get("orchestrator")
    uid = workspace.page(2).page_uid
    prepare(deck, {"kind": "delete_page", "targets": [uid]})
    command(deck, "delete-page", f"--revision-id revision-1 --operation-id op-1 --uid {uid}")
    current = owners.get("orchestrator")
    assert current.pages == () and current.owner_id == main.owner_id and current.attempt == main.attempt
    assert all(record.role != "orchestrator" for record in owners.list_archived())
    assert all(artifacts.artifact(f"page-html-{page.page:02d}").owner_id != "orchestrator"
               for page in workspace.pages())


@pytest.mark.parametrize("deck", [(1,)], indirect=True)
def test_active_child_still_blocks_main_structural_revision(deck):
    workspace, _, owners, _ = deck
    uid = workspace.page(1).page_uid
    prepare(deck, {"kind": "delete_page", "targets": [uid]})
    child = owners.accept(role="slide", pages=[2], group_id="group-2")
    owners.transition(child.owner_id, attempt=child.attempt, status="running")
    before = owners.path.read_bytes()
    with pytest.raises(ValueError, match="active Owners"):
        command(deck, "delete-page", f"--revision-id revision-1 --operation-id op-1 --uid {uid}")
    assert owners.path.read_bytes() == before


@pytest.mark.parametrize("deck", [(1, 3)], indirect=True)
def test_direct_owner_mapping_rolls_back_with_failed_structural_transaction(deck, monkeypatch):
    workspace, _, owners, _ = deck
    uid = workspace.page(1).page_uid
    prepare(deck, {"kind": "delete_page", "targets": [uid]})
    before = owners.path.read_bytes(), workspace.path.read_bytes()
    original = RevisionTransaction._sync

    def fail_after_sync(transaction, *args):
        original(transaction, *args)
        assert owners.get("orchestrator").pages == (2,)
        raise RuntimeError("after_owner_remap")

    monkeypatch.setattr(RevisionTransaction, "_sync", fail_after_sync)
    with pytest.raises(RuntimeError, match="after_owner_remap"):
        command(deck, "delete-page", f"--revision-id revision-1 --operation-id op-1 --uid {uid}")
    assert (owners.path.read_bytes(), workspace.path.read_bytes()) == before
    assert owners.get("orchestrator").pages == (1, 3)
    assert json.loads((workspace.paths.root / MARKER).read_text())["status"] == "rolled_back"


@pytest.mark.parametrize("kind", ["insert_page", "move_page"])
@pytest.mark.parametrize("side", ["after", "before"])
def test_insert_move_keep_identity(deck, kind, side):
    from runtime.skill_runtime import SkillRuntime
    workspace = deck[0]
    old = [page.page_uid for page in workspace.pages()]
    register_image(deck, "shared", slides=[1, 3], notes="Keep the original evidence")
    op = {"kind": kind, f"{side}_page_uid": old[1] if side == "after" else old[0]}
    if kind == "insert_page":
        op["new_page_uid"] = "pg-inserted"
        flags = "--new-uid pg-inserted"
        expected = [old[0], old[1], "pg-inserted", old[2]] if side == "after" else ["pg-inserted", *old]
    else:
        target = old[0] if side == "after" else old[2]
        op["targets"] = [target]
        flags = f"--uid {target}"
        expected = [old[1], old[0], old[2]] if side == "after" else [old[2], old[0], old[1]]
    prepare(deck, op)
    anchor = op[f"{side}_page_uid"]
    assert f"--{side} {anchor}" in (workspace.paths.state/'revision-impact.md').read_text()
    command(deck, kind.replace("_", "-"), f"--revision-id revision-1 --operation-id op-1 --{side} {anchor} {flags}")
    assert [page.page_uid for page in workspace.pages()] == expected
    resumed = WorkspaceState(workspace.paths.root)
    assert [page.page_uid for page in resumed.pages()] == expected
    image = deck[1].artifact("shared")
    assert image.image_metadata["slides"] == sorted(expected.index(old[number - 1]) + 1 for number in (1, 3))
    assert image.image_metadata["notes"] == "Keep the original evidence"
    for number, uid in enumerate(expected, 1):
        refs = SkillRuntime._artifact_refs(
            None,
            SimpleNamespace(artifacts=deck[1]),
            SimpleNamespace(pages=(number,), role="slide", asset_ids=()),
        )
        assert bool(refs) == (uid in {old[0], old[2]})
        if uid in old:
            html = resumed.paths.page_html(number).read_text()
            assert f'#slide-{number:02d} {{ color: rgb({(old.index(uid) + 1) * 20}, 0, 0); }}' in html
            assert f'getElementById("slide-{number:02d}")' in html
    if kind == "insert_page":
        assert resumed.page(3 if side == 'after' else 1).lifecycle == PageLifecycle.MISSING


def test_revision_cli_help_has_no_structural_side_effect(deck):
    before = deck[0].path.read_bytes()
    result = command(deck, 'move-page', '--help')
    assert '--before' in result['stdout'] and '--operation-id' in result['stdout']
    assert deck[0].path.read_bytes() == before
    assert not (deck[0].paths.root/MARKER).exists()
    result = deck[3].terminal(ToolContext('orchestrator', 'orchestrator'),
        {'command': 'python skills/mural-presenter-v1/scripts/orchestrator.py --help'})
    assert 'prepare-revision' in result['stdout'] and 'positional arguments:' in result['stdout']
    assert not result['diagnostics']['truncated']
    result = command(deck, 'prepare-revision', '--help')
    compact_help = "".join(result['stdout'].split())
    assert 'JSONFILEpath' in compact_help and 'revision-operations.json' in compact_help


def test_revision_context_projects_identity_without_internal_paths(deck):
    result = command(deck, "revision-context")
    payload = json.loads(result["stdout"].splitlines()[-1])
    assert payload["schema"] == "mural.revision-context.v1"
    assert payload["deck_version"] == 1
    assert [row["page_uid"] for row in payload["pages"]] == [
        page.page_uid for page in deck[0].pages()
    ]
    assert payload["active_revision"] is None
    assert "_state/" not in result["stdout"] and "_trace/" not in result["stdout"]


def test_prepare_revision_returns_recoverable_public_receipt(deck):
    workspace = deck[0]
    target = workspace.page(3).page_uid
    anchor = workspace.page(1).page_uid
    (workspace.paths.root / "revision-operations.json").write_text(json.dumps({
        "schema": "mural.edit-operations.v1",
        "revision_id": "revision-public-receipt",
        "base_deck_version": 1,
        "operations": [{
            "operation_id": "move-last-first",
            "kind": "move_page",
            "targets": [target],
            "before_page_uid": anchor,
        }],
    }))

    result = command(deck, "prepare-revision")
    payload = json.loads(result["stdout"].splitlines()[-1])
    active = payload["active_revision"]
    assert active["revision_id"] == "revision-public-receipt"
    assert active["operations"][0]["status"] == "pending"
    assert "move-page" in active["operations"][0]["command"]
    assert "_state/" not in result["stdout"] and "_trace/" not in result["stdout"]
    assert not {"deck_id", "operations_sha256", "impact_path", "operations_path"} & active.keys()


@pytest.mark.parametrize('quote', ['"', "'"])
def test_renumber_updates_only_exact_local_root_references(deck, quote):
    from _internal.revision_structure import _renumber_html
    source = (f'<main id={quote}slide-03{quote} data-slide={quote}03{quote}></main>'
              '<style>#slide-03 {color:red} #slide-030 {color:blue} #slide-03-note {color:green}</style>'
              '<a href="#slide-03">jump</a><script>document.getElementById(`slide-03`)</script>')
    updated = _renumber_html(source, 1, 3)
    assert f'id={quote}slide-01{quote}' in updated and f'data-slide={quote}01{quote}' in updated
    assert '#slide-01 {color:red}' in updated and 'href="#slide-01"' in updated
    assert 'getElementById(`slide-01`)' in updated
    assert '#slide-030 {color:blue}' in updated and '#slide-03-note {color:green}' in updated


def test_real_browser_keeps_scoped_style_and_dom_target_after_move(deck, monkeypatch):
    from playwright.sync_api import sync_playwright
    monkeypatch.syspath_prepend(str(SKILL.parents[1] / 'webui/studio'))
    from app.engine import render_env
    workspace = deck[0]
    old = [page.page_uid for page in workspace.pages()]
    original = workspace.paths.page_html(3).read_text()
    prepare(deck, {'kind': 'move_page', 'targets': [old[2]], 'before_page_uid': old[0]})
    command(deck, 'move-page', f'--revision-id revision-1 --operation-id op-1 --uid {old[2]} --before {old[0]}')
    env = render_env({**os.environ, 'TMPDIR': str(workspace.paths.runtime_tmp)})
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=env['PPT_SKILL_BROWSER_EXE'], env=env,
                                    headless=True, args=['--no-sandbox'])
        try:
            page = browser.new_page(viewport={'width': 320, 'height': 160})
            page.set_content(original)
            before = page.screenshot()
            page.set_content(workspace.paths.page_html(1).read_text())
            assert page.screenshot() == before
            assert page.evaluate('window.pageRoot.id') == 'slide-01'
            assert page.locator('#slide-01').evaluate('e => getComputedStyle(e).color') == 'rgb(60, 0, 0)'
        finally:
            browser.close()


def test_revision_file_errors_explain_the_actual_input_contract(deck):
    from runtime.tools import ToolContractError
    for value in ("'{broken'", "@missing.json", "_state/edit-operations.json"):
        with pytest.raises(ToolContractError, match='revision-operations.json'):
            command(deck, 'prepare-revision', f"--operations {value}")
    assert not (deck[0].paths.root/MARKER).exists()


def test_commit_refreshes_late_brief_metadata_without_changing_identity(deck):
    workspace, artifacts, *_ = deck
    old = [page.page_uid for page in workspace.pages()]
    prepare(deck, {'kind': 'move_page', 'targets': [old[2]], 'before_page_uid': old[0]})
    command(deck, 'move-page', f'--revision-id revision-1 --operation-id op-1 --uid {old[2]} --before {old[0]}')
    index_path = workspace.paths.state / 'page-index.json'
    before = json.loads(index_path.read_text())
    for path in (workspace.paths.root / "briefs/slide_01.md", workspace.paths.page_html(1)):
        path.write_text(path.read_text().replace('Topic 3', 'Current title'))
    record = artifacts.artifact('page-html-01')
    artifacts.commit(owner_id=record.owner_id, artifact_ids=[record.artifact_id], source_hashes={})
    ready(deck)
    command(deck, 'commit-revision', '--revision-id revision-1 --expected-base-version 1')
    after = json.loads(index_path.read_text())
    graph = json.loads((workspace.paths.state / 'deck-graph.json').read_text())
    assert after['pages'][0]['title'] == graph['nodes'][old[2]]['title'] == 'Current title'
    assert after['pages'][0]['source_plan_sha256'] != before['pages'][0]['source_plan_sha256']
    assert [p['page_uid'] for p in after['pages']] == [old[2], old[0], old[1]]
    assert after['deck_version'] == 2 and after['plan_epoch'] == before['plan_epoch'] + 1
    command(deck, 'commit-revision', '--revision-id revision-1 --expected-base-version 1')
    assert json.loads(index_path.read_text()) == after


def test_revision_accepts_the_same_freeform_briefs_as_generation(deck, monkeypatch):
    from _internal.deck_core import validate_plans
    workspace = deck[0]
    old = [p.page_uid for p in workspace.pages()]
    deck_path = workspace.paths.root/'deck.md'
    deck_path.write_text(deck_path.read_text().replace('- page_count: 3\n', ''))
    original_brief = (workspace.paths.root / 'briefs/slide_03.md').read_bytes()
    validate_plans(workspace.paths.root, 3)
    prepare(deck, {'kind': 'move_page', 'targets': [old[2]], 'before_page_uid': old[0]})
    command(deck, 'move-page', f'--revision-id revision-1 --operation-id op-1 --uid {old[2]} --before {old[0]}')
    assert [p.page_uid for p in workspace.pages()] == [old[2], old[0], old[1]]
    assert (workspace.paths.root / 'briefs/slide_01.md').read_bytes() == original_brief
    assert '- page_count:' not in deck_path.read_text()


def test_three_operations_keep_original_identity_in_requested_order(deck):
    workspace = deck[0]
    old = [page.page_uid for page in workspace.pages()]
    operations = {
        "schema": "mural.edit-operations.v1",
        "revision_id": "revision-1",
        "base_deck_version": 1,
        "operations": [
            {"operation_id": "move", "kind": "move_page", "targets": [old[2]], "before_page_uid": old[0]},
            {"operation_id": "insert", "kind": "insert_page", "new_page_uid": "pg_new_observer", "after_page_uid": old[2]},
            {"operation_id": "delete", "kind": "delete_page", "targets": [old[1]]},
        ],
    }
    (workspace.paths.root/'revision-operations.json').write_text(json.dumps(operations))
    command(deck, 'prepare-revision')
    command(deck, 'move-page', f'--revision-id revision-1 --operation-id move --uid {old[2]} --before {old[0]}')
    command(deck, 'insert-page', f'--revision-id revision-1 --operation-id insert --new-uid pg_new_observer --after {old[2]}')
    command(deck, 'delete-page', f'--revision-id revision-1 --operation-id delete --uid {old[1]}')
    assert [p.page_uid for p in workspace.pages()] == [old[2], 'pg_new_observer', old[0]]
    assert not (workspace.paths.root/'slides/slide_04.html').exists()
    assert not list((workspace.paths.root/'_runtime/tmp').glob('revision-*'))
    marker = json.loads((workspace.paths.root/MARKER).read_text())
    assert marker['status'] == 'committed'
    assert marker['backup'].startswith('_state/structural-snapshots/')
    assert (workspace.paths.root/marker['backup']/'slides/slide_04.html').is_file()
    from _internal.workspace_policy import audit_workspace
    assert audit_workspace(workspace.paths.root) == []
    context = ToolContext('orchestrator', 'orchestrator', read_scopes_for_role('orchestrator', skill_name='mural-presenter-v1'))
    assert not context.may_read(marker['backup'] + '/slides/slide_04.html')


def test_real_artifact_lease_blocks_structure(deck):
    workspace, artifacts, owners, _ = deck
    uid = workspace.page(1).page_uid
    prepare(deck, {"kind": "delete_page", "targets": [uid]})
    record = artifacts.artifact("page-html-01")
    artifacts.begin_transaction(record.owner_id, [record.artifact_id])
    before = workspace.path.read_bytes()
    with pytest.raises(ValueError, match="leases"):
        command(deck, "delete-page", f"--revision-id revision-1 --operation-id op-1 --uid {uid}")
    assert workspace.path.read_bytes() == before


def test_delete_remaps_asset_consumers_and_issue_pages(deck):
    from qc.content import ContentFinding
    from qc.routing import IssueLedger
    from runtime.skill_runtime import SkillRuntime
    workspace, artifacts, *_ = deck
    guidance = {"crop": "First view\nSecond view", "subject_region": "0.1,0.2,0.8,0.9",
                "notes": "Do not remove the device top"}
    register_image(deck, "shared", slides=[2, 3], **guidance)
    register_image(deck, "moved", slides=[3])
    register_image(deck, "retired", slides=[2])
    register_image(deck, "unassigned", slides=[], notes="Available for later assignment")
    register_image(deck, "unknown", notes="Consumers not yet recorded")
    catalog = workspace.paths.assets / "catalog.md"
    catalog.write_text("# Stale projection\n\n## shared\n- slides: 1\n\n## fabricated\n- slides: 1\n")
    ledger = IssueLedger(workspace.paths.root)
    moved, removed = ledger.record_content_findings([
        ContentFinding("moved","factual_conflict","major",(3,),"retained finding","slide"),
        ContentFinding("removed","factual_conflict","major",(2,),"retired finding","slide"),
    ])
    uid = workspace.page(2).page_uid
    prepare(deck, {"kind":"delete_page", "targets":[uid], "source_issue_ids":[removed.issue_id]})
    impact = json.loads((workspace.paths.state / "revisions/revision-1/impact.json").read_text())
    assert removed.issue_id in impact["source_issue_ids"]
    command(deck, "delete-page", f"--revision-id revision-1 --operation-id op-1 --uid {uid}")
    assert artifacts.artifact("shared").image_metadata == {"slides": [2], **guidance}
    assert artifacts.artifact("moved").image_metadata["slides"] == [2]
    refs = SkillRuntime._artifact_refs(
        None,
        SimpleNamespace(artifacts=artifacts),
        SimpleNamespace(pages=(2,), role="slide", asset_ids=()),
    )
    assert {row["artifact_id"] for row in refs} == {"shared", "moved", "unassigned", "unknown"}
    text = catalog.read_text()
    assert "- slides: 2\n" in text and "retired.png" not in text and "fabricated" not in text
    assert "First view\n  Second view" in text and guidance["subject_region"] in text and guidance["notes"] in text
    assert artifacts.artifact("unassigned").image_metadata["slides"] == []
    assert "slides" not in artifacts.artifact("unknown").image_metadata
    assert (workspace.paths.assets / "retired.png").is_file()
    marker = json.loads((workspace.paths.root / MARKER).read_text())
    backup = json.loads((workspace.paths.root / marker["backup"] / "_state/artifacts.json").read_text())
    assert backup["artifacts"]["retired"]["image_metadata"]["slides"] == [2]
    assert ledger.issue(moved.issue_id).pages == (2,)
    assert ledger.issue(removed.issue_id).status.value == "rejected"
    assert ledger.issue(removed.issue_id).evidence == removed.evidence


def test_failed_catalog_projection_rolls_back_machine_consumers_and_pages(deck, monkeypatch):
    from state import revisions
    workspace, artifacts, *_ = deck
    register_image(deck, "moved", slides=[3])
    catalog = workspace.paths.assets / "catalog.md"
    catalog.write_text("# Original catalog\n")
    uid = workspace.page(2).page_uid
    prepare(deck, {"kind": "delete_page", "targets": [uid]})
    paths = [workspace.path, artifacts.path, catalog, workspace.paths.page_html(3)]
    before = {path: path.read_bytes() for path in paths}
    core = revisions.workspace_skill_module(workspace.paths.root, "deck_core")
    def fail_projection(*_):
        catalog.write_text("partial projection")
        raise RuntimeError("projection_failure")
    monkeypatch.setattr(core, "write_image_catalog", fail_projection)
    with pytest.raises(RuntimeError, match="projection_failure"):
        command(deck, "delete-page", f"--revision-id revision-1 --operation-id op-1 --uid {uid}")
    assert {path: path.read_bytes() for path in paths} == before
    assert artifacts.artifact("moved").image_metadata["slides"] == [3]
    assert json.loads((workspace.paths.root / MARKER).read_text())["status"] == "rolled_back"


def test_uncommitted_structure_is_recovered_on_resume(deck, monkeypatch):
    workspace, artifacts, owners, _ = deck
    uid = workspace.page(1).page_uid
    prepare(deck, {"kind": "delete_page", "targets": [uid]})
    before = workspace.paths.page_html(1).read_bytes()
    from state import revisions
    real_recover = revisions.recover_revision
    def simulate_crash(*_):
        workspace.paths.page_html(1).write_text("half changed")
        monkeypatch.setattr(revisions, "recover_revision", lambda _root: None)
        raise KeyboardInterrupt("killed before commit")
    with pytest.raises(KeyboardInterrupt):
        RevisionTransaction(workspace, artifacts, owners).run(simulate_crash)
    assert json.loads((workspace.paths.root / MARKER).read_text())["status"] == "prepared"
    monkeypatch.setattr(revisions, "recover_revision", real_recover)
    resumed = WorkspaceState(workspace.paths.root)
    assert resumed.paths.page_html(1).read_bytes() == before
    assert resumed.page(1).page_uid == uid
    assert json.loads((workspace.paths.root / MARKER).read_text())["status"] == "rolled_back"


def test_process_crash_keeps_child_lock_until_safe_recovery(deck):
    import fcntl
    import subprocess
    import sys
    workspace = deck[0]
    root = workspace.paths.root
    uid = workspace.page(1).page_uid
    prepare(deck, {"kind":"delete_page", "targets":[uid]})
    before = workspace.paths.page_html(1).read_bytes()
    gate = root / "_runtime/tmp/release-test-child"
    child_code = """
import sys,time
from pathlib import Path
root=Path(sys.argv[1]); deadline=time.monotonic()+10
while not (root/'_runtime/tmp/release-test-child').exists() and time.monotonic()<deadline:
    time.sleep(.02)
(root/'slides/slide_01.html').write_text('late write from orphan child')
"""
    crash_code = """
import os,subprocess,sys
from pathlib import Path
from state.workspace import WorkspaceState
from state.artifacts import ArtifactStore
from state.revisions import RevisionTransaction
from owner.registry import OwnerRegistry
root=Path(sys.argv[1]); workspace=WorkspaceState(root)
def execute(token, fd):
    workspace.paths.page_html(1).write_text('partial change')
    subprocess.Popen([sys.executable,'-c',sys.argv[2],str(root)],pass_fds=(fd,),
                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    os._exit(37)
RevisionTransaction(workspace,ArtifactStore(root,workspace_state=workspace),
                    OwnerRegistry(root,deck_id=workspace.deck_id)).run(execute)
"""
    env = {**os.environ, "PYTHONPATH":str(SKILL.parents[1] / "harnesses/mural-presenter-v1")}
    try:
        process = subprocess.run([sys.executable,"-c",crash_code,str(root),child_code],
                                 env=env, capture_output=True, text=True, timeout=15)
        assert process.returncode == 37, process.stderr
        assert json.loads((root / MARKER).read_text())["status"] == "prepared"
        with (root / "_state/state.lock").open("a+b") as lock:
            with pytest.raises(BlockingIOError):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        gate.write_text("release")
        resumed = WorkspaceState(root)
        assert resumed.paths.page_html(1).read_bytes() == before
        assert resumed.page(1).page_uid == uid
        assert json.loads((root / MARKER).read_text())["status"] == "rolled_back"
    finally:
        gate.write_text("release")
