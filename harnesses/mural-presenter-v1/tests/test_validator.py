"""计划只校验可执行结构，不把自然语言审美和责任分组变成旧表单。"""
from pathlib import Path
import json
import pytest


@pytest.fixture
def core(monkeypatch):
    root = Path(__file__).resolve().parents[3]
    monkeypatch.syspath_prepend(str(root / 'skills/mural-presenter-v1/scripts'))
    from _internal import deck_core
    return deck_core


def test_breathing_page_intent_is_preserved_without_aesthetic_keyword_gates(tmp_path, core):
    (tmp_path / 'config.json').write_text(json.dumps({'working_language': 'zh',
        'presentation_language': 'zh', 'target_page_count': 1}))
    (tmp_path / 'deck.md').write_text('# Physics\n## 整体设计\n以粉笔小车和公式贯穿全篇。\n')
    (tmp_path / 'briefs').mkdir()
    text = '# P01 — F = ma：从实验走向模型\n'
    text += '## 本页任务\n结尾留出思考空间，以粉笔小车和公式呼应开场。\n'
    text += '## 页面内容与证据\n收束命题与思考问题。\n## 视觉表达与约束\n留白方向由创作者判断。\n'
    (tmp_path / 'briefs/slide_01.md').write_text(text)
    _, pages = core._load_plans(tmp_path, 1)
    assert not {'page_type', 'role', 'composition'} & pages[0].keys()
    assert pages[0]['text'] == text.strip()


def test_execution_ownership_is_not_duplicated_in_plan(tmp_path, core):
    (tmp_path / 'config.json').write_text(json.dumps({'working_language': 'en',
        'presentation_language': 'en', 'target_page_count': 1}))
    (tmp_path / 'deck.md').write_text('# Deck\n## Overall design\nShared typography.\n')
    (tmp_path / 'briefs').mkdir()
    (tmp_path / 'briefs/slide_01.md').write_text('# P01 — One\n- job: explain\n')
    _, pages = core._load_plans(tmp_path, 1)
    assert not {'production_group_explicit', 'composition', 'owner_id'} & pages[0].keys()


def test_retired_planning_apis_and_tool_schema_are_absent(core):
    from runtime.tools import STABLE_TOOL_SCHEMAS
    for name in ('_strict_plan_contract', '_load_plans_thin', '_parse_deck', '_parse_slide', 'apply_plan_batch',
                 '_SLIDE_METADATA_ALIASES', '_DECK_METADATA_ALIASES', '_thin_deck_quality_warnings'):
        assert not hasattr(core, name)
    assert not {'delegate_next_event', 'delegate_collect', 'canvas_select'}.intersection(STABLE_TOOL_SCHEMAS)
    assert 'delegate_wait' in STABLE_TOOL_SCHEMAS
