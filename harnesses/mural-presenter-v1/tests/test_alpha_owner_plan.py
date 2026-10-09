"""固定 Skill 的正文与当前创作配置完整进入所属 Owner，恢复不扩大页范围。"""
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from runtime.production import SkillSnapshot, read_scopes_for_role
from runtime.shared_objects import SharedSkillStore
from runtime.tools import ToolContext
from state.workspace import WorkspaceState
from test_owner_asset_projection import _setup


@pytest.mark.parametrize('assigned', [(1,), (1, 3)])
def test_fixed_alpha_runner_preserves_owned_markdown_and_revision_evidence(tmp_path, monkeypatch, assigned):
    root = tmp_path / 'deck'
    WorkspaceState(root)
    config = {'working_language': 'zh', 'presentation_language': 'zh', 'target_page_count': 3}
    (root / 'config.json').write_text(json.dumps(config))
    runtime, services, task, agent, calls = _setup(root, monkeypatch, pages=assigned)
    alpha = Path(__file__).resolve().parents[3] / 'skills/mural-presenter-v1'
    ref = SharedSkillStore.for_workspace(root).pin(services.workspace.paths, alpha, skill_name=alpha.name)
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    snapshot = SkillSnapshot(source=alpha, target=ref.object_root, name=alpha.name,
        tree_sha256=ref.tree_sha256, main_sha256=sha(ref.object_root / 'SKILL.md'),
        role_sha256={role: sha(ref.object_root / f'roles/{role}.md') for role in ('slide', 'research', 'image')},
        paths=services.workspace.paths)
    runtime.prepared.snapshot = snapshot
    monkeypatch.setattr(runtime, '_owner_tool_context', lambda _services, row: ToolContext(
        row.owner_id, row.role, read_scopes=read_scopes_for_role('slide', skill_name=alpha.name, pages=row.pages)))
    shared = '# Evidence deck\n## 任务说明\n自学使用，解释留在页面。\n## 整体设计规范\n同源字体与色彩。\n'
    (root / 'deck.md').write_text(shared)
    (root / 'briefs').mkdir(exist_ok=True)
    texts = {}
    for page in (1, 2, 3):
        texts[page] = ('# 本页主张\n## 本页任务\n对照证据，保留单位与限制。\n'
            '## 页面内容与证据\n| 指标 | 值 | 单位 |\n| --- | --- | --- |\n| 样本 | 12.5 | mg |\n'
            + ''.join(f'本页{page}的证据段{k}：定义、原文定位与不确定性均不可因短摘要丢失。\n' for k in range(80))
            + '\n## 视觉表达与约束\n表格展示可比条件，保留数值误差，不能当成因果。\n'
            + '## 讲稿\n先读定义。\n\n- 再比较条件。\n- 解释不确定性。\n')
        (root / f'briefs/slide_{page:02d}.md').write_text(texts[page])
    runner = runtime._owner_runner(services, agent, 'slide')
    runner(task, None)
    first = calls[-1]
    packet = json.loads(first['initial_messages'][0]['content'])['owner_packet']
    plan = packet['handoffs']['plan']
    assert packet['creation_config'] == config
    role_prompt = snapshot.role_text('slide').rstrip()
    assert first['system'].startswith(role_prompt)
    assert '## Runtime language policy' in first['system'][len(role_prompt):]
    assert '- working_language: zh (Chinese)' in first['system']
    assert (snapshot.target / 'roles/slide.md').read_bytes() == (alpha / 'roles/slide.md').read_bytes()
    assert not any(path.is_symlink() for path in snapshot.target.rglob('*'))
    assert plan['shared'] == shared.strip()
    assert [row['number'] for row in plan['pages']] == list(assigned)
    for row in plan['pages']:
        page = row['number']
        assert row == {'number': page, 'source': f'briefs/slide_{page:02d}.md', 'section': '本页主张', 'text': texts[page].strip()}
    assert not first['tool_context'].may_read('briefs/slide_02.md')
    stored = services.owner_contexts.load(task.owner_id)
    assert stored.packet.to_dict() == packet
    assert stored.artifact_hashes['config.json'] == sha(root / 'config.json')
    for page in assigned:
        assert stored.artifact_hashes[f'briefs/slide_{page:02d}.md'] == sha(root / f'briefs/slide_{page:02d}.md')
    (root / 'briefs/slide_02.md').write_text(texts[2] + '\n只属于其他页的新证据。\n')
    runner(task, None)
    assert calls[-1]['checkpoint'].messages == stored.segment.messages
    page = assigned[-1]
    path = root / f'briefs/slide_{page:02d}.md'
    revised = texts[page] + '\n## 新证据\n本次反例推翻旧解释；来源附录B，第7页。\n'
    path.write_text(revised)
    runner(task, None)
    assert calls[-1]['initial_messages'] == ()
    current = json.loads(calls[-1]['checkpoint'].messages[-1]['content'])['owner_packet']
    assert current['handoffs']['plan']['pages'][-1]['text'] == revised.strip()
    assert current['handoffs']['changed_inputs'] == [f'briefs/slide_{page:02d}.md']
    assert sha(path) == services.owner_contexts.load(task.owner_id).artifact_hashes[f'briefs/slide_{page:02d}.md']
    task = replace(task, goal='按当前 brief 修正本页来源说明；保留图表、数字和其余页面。')
    runner(task, None)
    repair_packet = json.loads(calls[-1]['checkpoint'].messages[-1]['content'])['owner_packet']
    assert repair_packet['goal'] == task.goal
    assert repair_packet['handoffs']['plan'] == current['handoffs']['plan']
    assert repair_packet['owner_id'] == packet['owner_id']
    assert path.read_text() == revised
    config['working_language'] = 'en'
    (root / 'config.json').write_text(json.dumps(config))
    runner(task, None)
    refreshed = json.loads(calls[-1]['checkpoint'].messages[-1]['content'])['owner_packet']
    assert refreshed['creation_config'] == config
    assert refreshed['handoffs']['changed_inputs'] == ['config.json']
    assert refreshed['owner_id'] == packet['owner_id']
    assert json.loads(first['initial_messages'][0]['content'])['owner_packet']['creation_config']['working_language'] == 'zh'
