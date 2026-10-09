"""唯一 Skill 根配置、引用闭合及 resume 的不可变身份。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from runtime.production import ProductionError, ProductionSettings, ProductionToolServices, SkillSnapshot, read_scopes_for_role
from runtime.release_guard import release_tree_digest
from runtime.shared_fonts import SharedFontStore
from runtime.shared_objects import SharedObjectError, SharedSkillStore
from runtime.tools import ToolContext, ToolContractError
from state.workspace import WorkspaceState


def _source(root, label):
    root.mkdir(parents=True)
    (root / 'SKILL.md').write_text(f'# {label}\n')
    (root / 'roles').mkdir()
    for role in ('research', 'image', 'slide'):
        (root / 'roles' / f'{role}.md').write_text(f'{label} {role}\n')
    (root / 'references').mkdir()
    (root / 'references/runtime-contract.md').write_text(f'{label} reference\n')
    return root


@pytest.fixture
def skill_sources(tmp_path):
    source = _source(tmp_path / 'skills/current', 'current')
    alpha = _source(tmp_path / 'skills/alpha', 'alpha')
    (source / 'scripts').mkdir()
    (source / 'scripts/slide.py').write_text('print("current renderer")\n')
    (source / 'scripts/orchestrator.py').write_text('print("current planner")\n')
    (source / 'scripts/_internal').mkdir()
    cli = Path(__file__).resolve().parents[3] / 'skills/mural-presenter-v1/scripts/_internal/cli.py'
    (source / 'scripts/_internal/cli.py').write_bytes(cli.read_bytes())
    (source / 'assets').mkdir()
    (source / 'assets/base.css').write_text(':root { --canvas-w: 1600px; --canvas-h: 900px; }\n')
    for name in ('scripts', 'assets', 'references/typography-routes.json', 'references/font-substitutions.json'):
        if name.endswith('.json'):
            (source / name).write_text('{}\n')
        target = Path(os.path.relpath(source / name, (alpha / name).parent))
        (alpha / name).symlink_to(target, target_is_directory=name in {'scripts', 'assets'})
    return source, alpha


def test_one_skill_dir_selects_specific_root_and_old_parent_variable_has_no_effect(tmp_path):
    default = ProductionSettings.from_environment({}).skill_source
    assert default.name == 'mural-presenter-v1'
    assert ProductionSettings.from_environment({'MURAL_SKILLS_DIR': str(tmp_path / 'obsolete')}).skill_source == default
    explicit = tmp_path / 'skills/alpha'
    assert ProductionSettings.from_environment({'MURAL_SKILL_DIR': str(explicit),
        'MURAL_SKILLS_DIR': str(tmp_path / 'obsolete')}).skill_source == explicit


def test_shared_resource_bytes_are_closed_into_immutable_object(tmp_path, skill_sources):
    source, alpha = skill_sources
    store = SharedSkillStore(tmp_path / 'data')
    state = WorkspaceState(tmp_path / 'deck')
    ref = store.pin(state.paths, alpha, skill_name=alpha.name)
    assert ref.tree_sha256 == release_tree_digest((alpha,), relative_to=alpha)
    rows = json.loads(ref.object_manifest.read_text())['files']
    assert hashlib.sha256(''.join(f"{row['sha256']}  ./{row['path']}\n" for row in rows).encode()).hexdigest() == ref.tree_sha256
    assert not any(path.is_symlink() for path in ref.object_root.rglob('*'))
    assert (ref.object_root / 'scripts/slide.py').read_bytes() == (source / 'scripts/slide.py').read_bytes()
    assert (ref.object_root / 'assets/base.css').read_bytes() == (source / 'assets/base.css').read_bytes()
    before = (ref.object_root / 'scripts/slide.py').read_bytes()
    (source / 'scripts/slide.py').write_text('print("updated renderer")\n')
    (alpha / 'SKILL.md').write_text('updated alpha instructions\n')
    resumed = store.pin(state.paths, tmp_path / 'missing/other', skill_name='other')
    assert resumed == ref and (resumed.object_root / 'scripts/slide.py').read_bytes() == before
    newer = store.pin(WorkspaceState(tmp_path / 'new-deck').paths, alpha, skill_name=alpha.name)
    assert newer.tree_sha256 != ref.tree_sha256
    assert (newer.object_root / 'scripts/slide.py').read_bytes() != before
    with pytest.raises(SharedObjectError, match='different Skill'):
        store.load_ref(state.paths, expected_skill_name='other')


@pytest.mark.parametrize('case', ['outside', 'document', 'cycle', 'absolute', 'nested', 'wrong-resource'])
def test_source_links_are_limited_to_same_skills_resource_positions(tmp_path, skill_sources, case):
    source, alpha = skill_sources
    if case == 'document':
        (alpha / 'SKILL.md').unlink()
        (alpha / 'SKILL.md').symlink_to('../current/SKILL.md')
    elif case == 'nested':
        (source / 'scripts/nested').symlink_to('.', target_is_directory=True)
    else:
        (alpha / 'scripts').unlink()
        if case == 'outside':
            outside = tmp_path / 'outside/scripts'
            outside.mkdir(parents=True)
            (alpha / 'scripts').symlink_to('../../outside/scripts', target_is_directory=True)
        elif case == 'cycle':
            (alpha / 'scripts').symlink_to('scripts', target_is_directory=True)
        elif case == 'absolute':
            (alpha / 'scripts').symlink_to(source / 'scripts', target_is_directory=True)
        else:
            (alpha / 'scripts').symlink_to('../current/assets', target_is_directory=True)
    with pytest.raises(RuntimeError, match='source.*(symlink|skills directory)|symlink'):
        release_tree_digest((alpha,), relative_to=alpha)


def test_snapshot_binds_main_roles_refs_commands_and_resume_to_one_pin(tmp_path, skill_sources, monkeypatch):
    source, alpha = skill_sources
    monkeypatch.setattr(SharedFontStore, 'pin_workspace', lambda *_a, **_k: None)
    env = {'MURAL_SKILL_DIR': str(alpha), 'MURAL_STUDIO_DATA_DIR': str(tmp_path / 'data')}
    settings = ProductionSettings.from_environment(env)
    root = tmp_path / 'deck'
    snapshot = SkillSnapshot.stage(root, settings.skill_source, env)
    assert snapshot.name == 'alpha' and snapshot.main_text() == '# alpha\n' + snapshot.prompt_suffix
    assert snapshot.main_sha256 == hashlib.sha256(b'# alpha\n').hexdigest()
    provenance = json.loads((root / '_trace/skill-provenance.json').read_text())
    assert provenance['runtime_prompt_suffix'] == snapshot.prompt_suffix
    for role in ('research', 'image', 'slide'):
        assert snapshot.role_text(role) == f'alpha {role}\n' + snapshot.prompt_suffix
    with pytest.raises(ValueError, match='未知生产角色'):
        snapshot.role_text('material')
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.snapshot, services.workspace = snapshot, WorkspaceState(root)
    context = ToolContext('slide', 'slide', read_scopes=read_scopes_for_role('slide', skill_name=snapshot.name, pages=(1,)))
    assert 'alpha reference' in services.read_file(context, {'path': 'skills/alpha/references/runtime-contract.md'})['content']
    assert context.may_read('briefs/slide_01.md') and not context.may_read('briefs/slide_02.md')
    assert not context.may_read('skills/current/references/runtime-contract.md')
    argv, role, action, args = services._validated_command('slide', 'python skills/alpha/scripts/slide.py render . --page 1')
    assert Path(argv[1]) == snapshot.target / 'scripts/slide.py'
    assert (role, action) == ('slide', 'render')
    assert args.page == 1 and args.root == '.'
    with pytest.raises(ToolContractError, match='不能执行'):
        services._validated_command('slide', 'python skills/current/scripts/slide.py render . --page 1')
    with pytest.raises(ProductionError, match='逻辑路径'):
        snapshot.resolve_skill_path('skills/alpha/../SKILL.md')
    changed = {**env, 'MURAL_SKILL_DIR': str(tmp_path / 'missing-root')}
    selected = ProductionSettings.from_environment(changed)
    resumed = SkillSnapshot.stage(root, selected.skill_source, changed)
    assert resumed.name == snapshot.name and resumed.tree_sha256 == snapshot.tree_sha256
    assert resumed.main_text() == snapshot.main_text()
    assert not (root / 'skills').exists()


def test_missing_selected_source_fails_only_when_new_workspace_needs_it(tmp_path):
    settings = ProductionSettings.from_environment({'MURAL_SKILL_DIR': str(tmp_path / 'missing')})
    with pytest.raises(SharedObjectError, match='incomplete'):
        SkillSnapshot.stage(tmp_path / 'deck', settings.skill_source, {'MURAL_STUDIO_DATA_DIR': str(tmp_path / 'data')})


@pytest.mark.parametrize('role', ['orchestrator', 'research', 'image', 'slide'])
def test_skill_link_guidance_advertises_readable_pinned_paths_without_aliases(tmp_path, skill_sources, monkeypatch, role):
    _, alpha = skill_sources
    references = ('references/planning/plan-contract.md', 'references/page-index.md',
                  'references/style-index.md', 'references/style/editorial.md')
    for relative in references:
        path = alpha / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f'pinned {relative}\n')
    monkeypatch.setattr(SharedFontStore, 'pin_workspace', lambda *_a, **_k: None)
    snapshot = SkillSnapshot.stage(tmp_path / 'deck', alpha, {'MURAL_STUDIO_DATA_DIR': str(tmp_path / 'data')})
    prompt = snapshot.main_text() if role == 'orchestrator' else snapshot.role_text(role)
    examples = [line.split(' → ', 1)[1] for line in prompt.splitlines() if ' → ' in line]
    assert set(examples) == {f'skills/alpha/{relative}' for relative in references if not relative.endswith('style-index.md')}
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.snapshot, services.workspace = snapshot, WorkspaceState(tmp_path / 'deck')
    context = ToolContext(role, role, read_scopes=read_scopes_for_role(role, skill_name=snapshot.name, pages=(1,)))
    for relative in references:
        (alpha / relative).write_text('source changed after pin\n')
        logical = f'skills/alpha/{relative}'
        assert services.read_file(context, {'path': logical})['content'] == f'1|pinned {relative}'
        with pytest.raises(ToolContractError) as denied:
            services.read_file(context, {'path': relative})
        assert denied.value.code == 'read_scope_denied'
    with pytest.raises(ToolContractError) as denied:
        services.read_file(context, {'path': 'skills/current/references/runtime-contract.md'})
    assert denied.value.code == 'read_scope_denied'
    assert not (tmp_path / 'deck/skills').exists()


def test_unreleased_selection_cannot_borrow_default_production_approval(tmp_path, skill_sources, monkeypatch):
    source, alpha = skill_sources
    monkeypatch.setattr(SharedFontStore, 'pin_workspace', lambda *_a, **_k: None)
    state = WorkspaceState(tmp_path / 'deck')
    release = {'policy': 'production', 'actual': {'skill': release_tree_digest((source,), relative_to=source)}}
    release_path = state.paths.state / 'release.json'
    release_path.write_text(json.dumps(release))
    env = {'MURAL_STUDIO_DATA_DIR': str(tmp_path / 'data')}
    with pytest.raises(SharedObjectError, match='not authorized'):
        SkillSnapshot.stage(state.paths.root, alpha, env)
    assert not state.paths.skill_ref.exists()
    release_path.write_text(json.dumps({**release, 'policy': 'development', 'dirty_override': True}))
    chosen = SkillSnapshot.stage(state.paths.root, alpha, env)
    release_path.write_text(json.dumps(release))
    resumed = SkillSnapshot.stage(state.paths.root, tmp_path / 'missing', env)
    assert resumed.tree_sha256 == chosen.tree_sha256


def test_production_skill_ref_uses_effective_release_name(tmp_path, skill_sources, monkeypatch):
    source, _ = skill_sources
    monkeypatch.setattr(SharedFontStore, 'pin_workspace', lambda *_a, **_k: None)
    state = WorkspaceState(tmp_path / 'deck')
    skill_hash = release_tree_digest((source,), relative_to=source)
    release = {
        'release': 'mural-paper-v1-stable-test',
        'policy': 'production',
        'actual': {'skill': skill_hash},
    }
    (state.paths.state / 'release.json').write_text(json.dumps(release))
    snapshot = SkillSnapshot.stage(state.paths.root, source, {'MURAL_STUDIO_DATA_DIR': str(tmp_path / 'data')})
    ref = json.loads(state.paths.skill_ref.read_text())
    assert snapshot.release == 'mural-paper-v1-stable-test'
    assert ref['release_id'] == 'mural-paper-v1-stable-test'
    assert ref['object_identity'].startswith('unreleased-')
