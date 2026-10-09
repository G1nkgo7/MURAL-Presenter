import asyncio
import json
import sqlite3
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from app import db, jobs, titles
from workspace_fixture import write_workspace_manifest


NEWTON_QUERY = (
    "I don't want to teach from handouts crammed full of text anymore; "
    "this time the topic is how Newton's second law moves from experiment "
    "to model. The class has just started 10th grade."
)


def test_english_fallback_extracts_topic_instead_of_prompt_prefix():
    assert titles.fallback_title(NEWTON_QUERY) == (
        "Newton's second law: experiment to model"
    )


def test_short_or_unstructured_query_uses_neutral_fallback():
    assert titles.fallback_title("复") == "新演示"
    assert titles.fallback_title("do it") == "New presentation"


def test_short_technical_query_preserves_ambiguous_term():
    assert titles.fallback_title("3页；帮我介绍下 opd 算法；卡通画风") == "OPD 算法"


def test_plan_heading_is_the_model_failure_fallback():
    plan = "# Deck Plan · OPD 算法入门（卡通画风）\n\n- 目标: 解释算法"
    assert titles.plan_fallback_title("介绍 opd 算法", plan) == "OPD 算法入门"


def test_manual_title_is_never_replaced_by_legacy_cleanup():
    manual = "帮我记住的自定义名称"
    assert titles.display_title(manual, "帮我制作一份很长的演示", "manual") == manual


def test_legacy_raw_or_assistant_title_is_repaired_for_display():
    assert titles.display_title(
        "I don't want to teach from han…", NEWTON_QUERY, "legacy"
    ) == "Newton's second law: experiment to model"
    assert titles.display_title("你好！我是标题编辑", "复", "legacy") == "新演示"


def test_task_model_prefers_configured_pptagent(monkeypatch):
    registry = {
        "opus-5": {"model": "claude-opus-5"},
        "deployment-model": {"model": "pptagent-qwen35-27b-ckpt1764"},
    }
    monkeypatch.setenv("STUDIO_TITLE_TASK_MODEL_KEY", "deployment-model")
    assert titles.select_task_model(registry)["model"].startswith("pptagent-")


def test_account_task_model_selection_overrides_deployment_default(monkeypatch):
    registry = {
        "deployment-model": {"model": "pptagent-27b-a"},
        "custom:7": {"model": "pptagent-27b-b"},
    }
    monkeypatch.setenv("STUDIO_TITLE_TASK_MODEL_KEY", "deployment-model")
    assert titles.select_task_model(registry, "custom:7")["model"] == "pptagent-27b-b"
    assert titles.select_task_model(registry, "missing")["model"] == "pptagent-27b-a"


def test_qwen_title_call_disables_thinking_and_parses_json(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{
                    "message": {
                        "content": json.dumps({
                            "title": "Newton's Second Law: Experiment to Model"
                        })
                    }
                }]
            }).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(titles.urllib.request, "urlopen", fake_urlopen)
    value = titles.summarize_with_agent(
        NEWTON_QUERY,
        {
            "url": "http://127.0.0.1:8000/v1",
            "model": "pptagent-qwen35-27b-ckpt1764",
            "thinking_transport": "chat_template_kwargs",
        },
        timeout=3,
    )
    assert value == "Newton's Second Law: Experiment to Model"
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert captured["body"]["stream"] is False


def test_invalid_assistant_chatter_falls_back(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": "你好！我是标题编辑，请告诉我主题"}}]
            }).encode()

    monkeypatch.setattr(titles.urllib.request, "urlopen", lambda *_a, **_k: Response())
    assert titles.summarize_with_agent(
        "复", {"url": "http://127.0.0.1/v1", "model": "pptagent-27b"}
    ) == "新演示"


def test_plan_title_request_preserves_ambiguous_term(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({
                "choices": [{"message": {"content": '{"title":"实时派单方法"}'}}]
            }).encode()

    def fake_urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode())
        return Response()

    monkeypatch.setattr(titles.urllib.request, "urlopen", fake_urlopen)
    plan = "# Deck Plan · OPD 算法入门（卡通画风）\n\n- 目标: 解释 OPD"
    value = titles.summarize_after_plan(
        "3页；帮我介绍下 opd 算法；卡通画风",
        plan,
        {"url": "http://127.0.0.1/v1", "model": "pptagent-27b"},
    )
    assert value == "OPD 算法入门"
    request_text = captured["body"]["messages"][1]["content"]
    assert "<plan>" in request_text
    assert "OPD 算法入门" in request_text


@pytest.fixture
def title_db(tmp_path, monkeypatch):
    database = tmp_path / "titles.db"

    def connect():
        con = sqlite3.connect(database)
        con.row_factory = sqlite3.Row
        return con

    con = connect()
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO conversations(id,user_id,title,created_at,updated_at) "
                "VALUES(1,1,'OPD 算法：原理、流程与应用',0,0)")
    con.execute("INSERT INTO decks(id,conversation_id,title,title_source,title_locked,"
                "title_updated_at,user_id,seed_json,created_at,started_at,status,run_dir) "
                "VALUES(254,1,'OPD 算法：原理、流程与应用','task_model',0,0,1,?,0,100,'running','')",
                ('{"user_query":"介绍 OPD 算法"}',))
    con.commit()
    con.close()
    monkeypatch.setattr(jobs, "connect", connect)
    return connect


def _update_title_row(connect, **values):
    con = connect()
    con.execute("UPDATE decks SET " + ",".join(f"{key}=?" for key in values), tuple(values.values()))
    con.commit()
    con.close()


def test_plan_title_replaces_query_title_but_not_manual_title(title_db):
    connect = title_db

    jobs._store_plan_title(254, "OPD 算法入门")
    con = connect()
    assert tuple(con.execute("SELECT title,title_source FROM decks").fetchone()) == (
        "OPD 算法入门", "plan"
    )
    con.execute(
        "UPDATE decks SET title='我的 OPD',title_source='manual',title_locked=1"
    )
    con.commit()
    con.close()
    assert not jobs._store_plan_title(254, "不应覆盖")
    assert jobs._load_deck(254)["title"] == "我的 OPD"


def test_existing_plan_title_can_follow_revised_plan(title_db):
    assert jobs._store_plan_title(254, "OPD 算法入门")
    before = jobs._load_deck(254)
    assert jobs._store_plan_title(254, "OPD：逐步评分", expected=before)
    assert jobs._load_deck(254)["title"] == "OPD：逐步评分"
    con = title_db()
    assert con.execute("SELECT title FROM conversations").fetchone()[0] == "OPD：逐步评分"
    con.close()


@pytest.mark.parametrize("change", [
    {"title": "人工标题", "title_source": "manual", "title_locked": 1},
    {"title": "更新的计划标题", "title_source": "plan"},
    {"started_at": 101},
    {"seed_json": '{"user_query":"新的主题"}'},
    {"title_updated_at": 102},
    {"status": "interrupted"},
])
def test_late_title_does_not_overwrite_newer_input(title_db, change):
    before = jobs._load_deck(254)
    _update_title_row(title_db, **change)
    assert not jobs._store_plan_title(254, "迟到的标题", expected=before)
    assert dict(jobs._load_deck(254)) == {**dict(before), **change}


def test_manual_conversation_title_is_preserved(title_db):
    con = title_db()
    con.execute("UPDATE conversations SET title='我的项目' ")
    con.commit()
    con.close()
    assert jobs._store_plan_title(254, "OPD 算法入门")
    con = title_db()
    assert con.execute("SELECT title FROM conversations").fetchone()[0] == "我的项目"
    con.close()


@pytest.fixture
def title_workspace(tmp_path):
    write_workspace_manifest(tmp_path)
    (tmp_path / "config.json").write_text(json.dumps({
        "working_language": "zh", "presentation_language": "zh", "target_page_count": 2,
    }))
    (tmp_path / "briefs").mkdir()
    (tmp_path / "deck.md").write_text(
        "# OPD 算法入门\n\n## 任务说明\n解释教师逐步评分，面向算法初学者。\n"
        "\n## 整体设计规范\n同源字体与色彩，以例子解释每一步的作用。\n"
        "\n## 参考代码\n```css\nbody { color: red; }\n/* goal layout: red */\n```\n"
    )
    (tmp_path / "base.css").write_text("body { color: red; }\n")
    (tmp_path / "briefs/slide_01.md").write_text(
        "# 每一步都有回声\n## 本页任务\n解释 OPD 的教师逐步评分机制。\n"
        "## 页面内容与证据\n教师为学生的每一步提供反馈。\n"
        "## 视觉表达与约束\n用同一案例连接多次反馈，不夸大适用范围。\n"
        "## 讲稿\n只在演讲中使用的长解释。\n"
    )
    (tmp_path / "briefs/slide_02.md").write_text("# 奖励如何反馈\n## 本页任务\n给出例子。\n")
    return tmp_path


def test_context_uses_deck_topic_and_separate_p1_plan(title_workspace):
    context = jobs._title_plan(title_workspace)
    assert "# OPD 算法入门" in context
    assert "P1 title: 每一步都有回声" in context
    assert "OPD 的教师逐步评分机制" in context
    assert "教师为学生的每一步提供反馈" in context
    assert "面向算法初学者" in context
    assert "P2 title: 奖励如何反馈" in context
    assert "color: red" not in context
    assert "只在演讲中使用的长解释" not in context
    (title_workspace / "briefs/slide_01.md").write_text("# OPD 新标题\n## 任务\n新主题。\n")
    assert jobs._title_plan(title_workspace) != context


def test_heading_alone_is_not_a_complete_plan(title_workspace):
    (title_workspace / "briefs/slide_02.md").unlink()
    assert jobs._title_plan(title_workspace) == ""


def test_null_target_does_not_invent_missing_pages(title_workspace):
    (title_workspace / "briefs/slide_02.md").unlink()
    path = title_workspace / "config.json"
    config = json.loads(path.read_text())
    config["target_page_count"] = None
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    assert "P1 title: 每一步都有回声" in jobs._title_plan(title_workspace)
    assert path.read_bytes() == before
    assert not (title_workspace / "briefs/slide_02.md").exists()


@pytest.mark.parametrize("invalid", [None, "{", {"working_language": None, "presentation_language": "zh", "target_page_count": 2}])
def test_missing_or_unresolved_creation_config_does_not_refine_title(title_workspace, invalid):
    path = title_workspace / "config.json"
    if invalid is None:
        path.unlink()
    else:
        path.write_text(invalid if isinstance(invalid, str) else json.dumps(invalid))
    assert jobs._title_plan(title_workspace) == ""


def test_titles_keep_natural_non_chinese_sections(title_workspace):
    path = title_workspace / "briefs/slide_01.md"
    path.write_text(
        "# Feedback at Every Step\n## Why this matters\nExplain the teacher's intermediate feedback.\n"
        "## What the audience sees\nA worked example with two successive steps.\n"
        "## Speaker notes\nPrivate delivery rehearsal.\n"
    )
    context = jobs._title_plan(title_workspace)
    assert "teacher's intermediate feedback" in context and "two successive steps" in context
    assert "Private delivery rehearsal" not in context


def test_narration_and_css_changes_do_not_refine_the_topic(title_workspace):
    context = jobs._title_plan(title_workspace)
    brief = title_workspace / "briefs/slide_01.md"
    brief.write_text(brief.read_text().replace("只在演讲中使用的长解释。", "修改讲述节奏。" * 3000))
    shared = title_workspace / "deck.md"
    shared.write_text(shared.read_text().replace("red", "blue"))
    (title_workspace / "base.css").write_text("body { color: black; }\n")
    assert jobs._title_plan(title_workspace) == context


def test_p1_is_not_truncated_by_long_shared_direction():
    context = titles.plan_title_input(
        "# 牛来\n- 目标: " + "很长的背景。" * 1500,
        [{"number": 1, "title": "手搓电影", "body": "## 主题\n两人的动画电影。\n"}],
    )
    assert "P1 title: 手搓电影" in context
    assert "两人的动画电影" in context
    assert len(context) <= 3200


@pytest.fixture
def watcher(title_db, title_workspace, monkeypatch):
    calls = []
    _update_title_row(title_db, run_dir=str(title_workspace))
    monkeypatch.setattr(jobs.dynamic, "_model_registry", lambda _user: {"task": {}})
    monkeypatch.setattr(jobs.service_config, "title_task_model_key", lambda _uid: "task")

    def summarize(query, plan, model, **kwargs):
        calls.append((plan, kwargs))
        return f"规划标题{len(calls)}"

    async def inline_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, .002 if timeout == 2 else timeout)

    monkeypatch.setattr(titles, "summarize_after_plan", summarize)
    monkeypatch.setattr(asyncio, "to_thread", inline_thread)
    monkeypatch.setattr(asyncio, "wait_for", fast_wait_for)

    async def until_called():
        for _ in range(100):
            if calls:
                return
            await asyncio.sleep(.002)
        pytest.fail("title watcher did not refine the completed plan")

    return SimpleNamespace(root=title_workspace, connect=title_db, calls=calls, until_called=until_called)


@pytest.mark.parametrize("changed", [False, True])
def test_initial_and_final_titles_only_follow_semantic_changes(watcher, changed):
    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(jobs._refresh_title_after_plan(
            254, jobs._load_deck(254), watcher.root, stop,
        ))
        await watcher.until_called()
        assert jobs._load_deck(254)["title"] == "规划标题1"
        path = watcher.root / ("briefs/slide_01.md" if changed else "base.css")
        original = path.read_text()
        path.write_text(original.replace("每一步都有回声", "教师与学生的合作") if changed
                        else original.replace("red", "blue"))
        _update_title_row(watcher.connect, status="completed")
        stop.set()
        await task
        assert len(watcher.calls) == (2 if changed else 1)
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["revision", "resume", "adopted"])
@pytest.mark.parametrize("changed", [False, True])
def test_revision_resume_and_adoption_refresh(watcher, mode, changed):
    seed = {"user_query": "介绍 OPD 算法"}
    if mode == "revision":
        seed["_revision"] = {"instruction": "只修改 P1 标题"}
    elif mode == "resume":
        seed["_resume_workspace"] = True
    _update_title_row(watcher.connect, seed_json=json.dumps(seed), title_source="plan")
    baseline = jobs._title_plan(watcher.root) if mode != "adopted" else None
    if changed:
        (watcher.root / "briefs/slide_01.md").write_text("# 新的封面标题\n## 内容\n解释。\n")
    _update_title_row(watcher.connect, status="completed")

    async def run():
        stop = asyncio.Event()
        stop.set()
        await jobs._refresh_title_after_plan(254, jobs._load_deck(254), watcher.root, stop,
                                             initial_plan=baseline)
    asyncio.run(run())
    assert len(watcher.calls) == (1 if changed or mode == "adopted" else 0)


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_failed_or_stopped_run_does_not_apply_final_title(watcher, status):
    _update_title_row(watcher.connect, status=status)

    async def run():
        stop = asyncio.Event()
        stop.set()
        await jobs._refresh_title_after_plan(254, jobs._load_deck(254), watcher.root, stop)
    asyncio.run(run())
    assert not watcher.calls


def test_manual_title_avoids_model_call(watcher):
    _update_title_row(watcher.connect, title_source="manual", title_locked=1)
    asyncio.run(jobs._refresh_title_after_plan(254, jobs._load_deck(254), watcher.root, asyncio.Event()))
    assert not watcher.calls


def test_plan_changing_during_model_call_discards_stale_result(watcher, monkeypatch):
    def changed_during_call(*args, **kwargs):
        (watcher.root / "briefs/slide_01.md").write_text("# 已经改变的主题\n")
        return "过期的计划标题"

    monkeypatch.setattr(titles, "summarize_after_plan", changed_during_call)
    _update_title_row(watcher.connect, status="completed")

    async def run():
        stop = asyncio.Event()
        stop.set()
        await jobs._refresh_title_after_plan(254, jobs._load_deck(254), watcher.root, stop)
    asyncio.run(run())
    assert jobs._load_deck(254)["title_source"] == "task_model"


@pytest.mark.parametrize("adopted", [False, True])
def test_dispatch_finishes_without_waiting_for_title_model(watcher, monkeypatch, adopted):
    _update_title_row(watcher.connect, status="running" if adopted else "queued")
    monkeypatch.setattr(jobs, "_workers", [])
    monkeypatch.setattr(jobs, "_active", {})
    monkeypatch.setattr(jobs, "_scheduled", set())
    monkeypatch.setattr(jobs, "_drain_queues", lambda: None)

    async def completed(*args):
        _update_title_row(watcher.connect, status="completed")

    monkeypatch.setattr(jobs, "_process", completed)
    monkeypatch.setattr(jobs, "_adopt", completed)

    async def run():
        await jobs._run_scheduled(254, watcher.root if adopted else None)
        assert jobs._load_deck(254)["status"] == "completed"
        assert not watcher.calls
        await asyncio.gather(*jobs._workers)
        assert len(watcher.calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("failure", ["network", "invalid", "unconfigured"])
def test_refinement_failure_preserves_existing_plan_title(monkeypatch, failure):
    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def read(self): return b'{"choices": [{"message": {"content": "Hello!"}}]}'

    def urlopen(*args, **kwargs):
        if failure == "network":
            raise TimeoutError("title request timed out")
        return Response()

    monkeypatch.setattr(titles.urllib.request, "urlopen", urlopen)
    model = {} if failure == "unconfigured" else {"url": "http://title.test/v1", "model": "test"}
    assert titles.summarize_after_plan("介绍 OPD 算法", "# 新的计划标题", model,
                                       previous_title="有效旧标题") == "有效旧标题"


def test_startup_reconciles_finished_orphan_without_relaunch(watcher, monkeypatch):
    (watcher.root / "result.json").write_text('{}')
    for name, value in (("_workers", []), ("_loop", None), ("_limits", {})):
        monkeypatch.setattr(jobs, name, value)
    monkeypatch.setattr(jobs.service_config, "load_runtime_config", lambda: {"decks": {}})
    monkeypatch.setattr(jobs, "_engine_alive", lambda _id: False)
    monkeypatch.setattr(jobs, "_finalize", lambda *_args: _update_title_row(watcher.connect, status="completed"))
    monkeypatch.setattr(jobs, "enqueue", lambda *_args: pytest.fail("must not restart a finished engine"))

    async def run():
        await jobs.start()
        assert jobs.title_refresh_pending(254)
        await asyncio.gather(*jobs._workers)
        assert not jobs.title_refresh_pending(254)
        assert len(watcher.calls) == 1
    asyncio.run(run())


def test_title_model_exception_does_not_fail_completed_deck(watcher, monkeypatch):
    def broken(*args, **kwargs):
        raise TimeoutError("test title timeout")

    monkeypatch.setattr(titles, "summarize_after_plan", broken)
    _update_title_row(watcher.connect, status="completed")

    async def run():
        stop = asyncio.Event()
        stop.set()
        await jobs._refresh_title_after_plan(254, jobs._load_deck(254), watcher.root, stop)
    asyncio.run(run())
    assert jobs._load_deck(254)["status"] == "completed"
    assert jobs._load_deck(254)["title_source"] == "task_model"


def test_deck_list_reports_only_live_title_refinement(watcher, monkeypatch):
    from app import main

    monkeypatch.setattr(jobs, "_workers", [])
    monkeypatch.setattr(main.features, "DYNAMIC_ENABLED", False)
    monkeypatch.setattr(main, "_model_label", lambda *_args: "test")
    _update_title_row(watcher.connect, status="completed")

    async def run():
        stop = asyncio.Event()
        task = asyncio.create_task(stop.wait(), name="deck-title-254")
        jobs._workers.append(task)
        con = watcher.connect()
        assert main.list_decks(user={"id": 1}, con=con)["decks"][0]["title_refresh_pending"]
        stop.set()
        await task
        assert not main.list_decks(user={"id": 1}, con=con)["decks"][0]["title_refresh_pending"]
        con.close()
    asyncio.run(run())


def test_sse_title_comes_from_current_database_not_connection_snapshot(watcher, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "connect", watcher.connect)
    monkeypatch.setattr(main, "_model_label", lambda *_args: "test")
    monkeypatch.setattr(main, "_deck_conversation_turns", lambda *_args: [])
    monkeypatch.setattr(main, "_canvas_spec_payload", lambda *_args: {})
    monkeypatch.setattr(main, "_static_html_preview_state", lambda *_args: {})
    monkeypatch.setattr(main, "_static_html_preview_for_status", lambda *_args: {
        "ready": False, "final": False, "failed_intermediate": False, "stamp": 0,
    })
    monkeypatch.setattr(main.trace, "snapshot", lambda *_args, **_kwargs: {"phase": "rendering"})
    _update_title_row(watcher.connect, status="completed")

    async def disconnected(): return False

    async def run():
        response = await main.deck_events(254, SimpleNamespace(is_disconnected=disconnected), user={"id": 1})
        _update_title_row(watcher.connect, title="OPD：逐步评分", title_source="plan")
        event = await anext(response.body_iterator)
        payload = json.loads(event.removeprefix("data: "))
        assert payload["title"] == "OPD：逐步评分"
        assert payload["display_title"] == "OPD：逐步评分"
        await response.body_iterator.aclose()
    asyncio.run(run())


def test_live_title_updates_sidebar_and_current_editor_only():
    from test_startup_observability import _js

    result = _js(["historyTitle", "syncStaticTitle"], """
syncStaticTitle(295,{title:'新的整册主题',title_source:'plan'});
syncStaticTitle(296,{title:'另一份主题',title_source:'plan'});
syncStaticTitle(295,{title:'新的整册主题',title_source:'plan'});
console.log(JSON.stringify({rows:historyRows,title:nodes['#ed-title'].textContent,topbars,renders}));
""", setup="""
const historyRows=[{id:295,kind:'static',title:'旧标题'},{id:296,kind:'static',title:'另一旧标题'}];
const ed={kind:'static',id:295},nodes={'#ed-title':{textContent:'旧标题'},'#history-search-modal':{hidden:true}};
const $=key=>nodes[key],topbars=[];let renders=0;
const setTopbar=title=>topbars.push(title),renderHistoryRows=()=>renders++,renderHistorySearchResults=()=>{};
const editorPresentationKind=()=> 'static';
""")
    assert [row["display_title"] for row in result["rows"]] == ["新的整册主题", "另一份主题"]
    assert result["title"] == "新的整册主题"
    assert result["topbars"] == ["新的整册主题"]
    assert result["renders"] == 2


def test_sidebar_waits_for_final_title_then_stops_polling():
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    function = source.split("async function loadDecks(active) {")[1].split("\nfunction openDeckFromLocationHash")[0]
    setup = """
let historyRows=[],historyTitleTimer=null,requests=0;const scheduled=[],synced=[];
const ed={id:295},isAuthenticated=true,trajectoryMode=false,dynamicEnabled=false;
const $=()=>({hidden:true}),syncDynamicModels=()=>{},renderHistoryRows=()=>{},renderHistorySearchResults=()=>{};
const syncStaticTitle=(id,row)=>synced.push(row.title),clearTimeout=()=>{};
const setTimeout=(callback,delay)=>{scheduled.push(callback);return scheduled.length};
const jget=async()=>({decks:[{id:295,title:++requests===1?'旧标题':'最终主题',
    status:'completed',title_refresh_pending:requests===1}]});
"""
    body = """
(async()=>{await loadDecks(295);await scheduled[0]();
console.log(JSON.stringify({requests,timers:scheduled.length,synced,title:historyRows[0].title}));})();
"""
    result = subprocess.run(["node", "-e", setup + "async function loadDecks(active) {" + function + body],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"requests": 2, "timers": 1,
                                         "synced": ["旧标题", "最终主题"], "title": "最终主题"}
