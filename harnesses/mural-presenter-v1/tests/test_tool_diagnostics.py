from __future__ import annotations

import json
import os
import re
from pathlib import Path
import sys
import threading
import time

import pytest

from runtime.diagnostics import CommandFailed, command_scope, run_command
from runtime.live_trace import LiveEventJournal
from runtime.production import _run_command
from runtime.tools import ToolCall, ToolContext, ToolDefinition, ToolRuntime, replay_tool_transactions
from runtime.production import ProductionToolServices
from runtime.tools import ToolContractError
from state.workspace import WorkspaceState

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "webui/studio"))
from app import trace


@pytest.mark.parametrize("enabled", [True, False])
def test_preview_preserves_model_config_and_normal_content_fields(enabled):
    from runtime.live_trace import preview_value
    value = {"model_config": {"thinking": enabled, "effort": "medium"},
        "thinking": "private-thought", "thinking_signature": "private-signature",
        "api_key": "private-key", "reply": "visible private-key"}
    public = preview_value(value)
    assert public["model_config"] == {"thinking": enabled, "effort": "medium"}
    assert public == value


def runtime(root, script, *, timeout=5, environment=None):
    WorkspaceState(root)
    journal = LiveEventJournal(root / "_trace/live-events.jsonl")
    tool = ToolRuntime(event_sink=journal.record_tool_event)
    def handler(_context, _args):
        result = _run_command([sys.executable, "-c", script], cwd=root, timeout_s=timeout,
                              environment=environment or os.environ)
        return {"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
                "diagnostics": result.diagnostics}
    tool.register(ToolDefinition("terminal", "fixture", {"type": "object"}, handler, frozenset({"image"})))
    context = ToolContext("image-owner", "image", metadata={"workspace_root": str(root), "task_id": "image-task", "attempt": 2, "turn": 3, "segment_id": "image-segment"})
    return tool, context, journal


def test_failed_command_preserves_both_streams_and_identity(tmp_path):
    tool, context, journal = runtime(tmp_path, "import sys; print('asset-A: HTTP 403'); print('generic failure',file=sys.stderr); sys.exit(1)")
    result = tool.execute(context, ToolCall("failed-command", "terminal", {}))
    assert not result.ok and result.error_code == "skill_command_failed"
    payload = json.loads(result.content)
    assert "HTTP 403" in payload["stdout"] and "generic failure" in payload["stderr"]
    feed = trace.tool_output_payload(str(tmp_path), "failed-command")
    assert feed["task_id"] == "image-task" and feed["attempt"] == 2
    assert feed["complete"] and {r["stream"] for r in feed["rows"]} == {"stdout", "stderr", "meta"}
    before = journal.sequence
    assert tool.execute(context, ToolCall("failed-command", "terminal", {})) == result
    assert journal.sequence == before
    assert replay_tool_transactions(tool.events).results == 1


@pytest.mark.parametrize("exit_code,prefix,expected", [
    (2, "page_body_unfilled: ", "page_body_unfilled"),
    (1, "page_body_unfilled: ", "skill_command_failed"),
    (2, "example of page_body_unfilled: ", "skill_command_failed"),
    (0, "page_body_unfilled: ", ""),
])
def test_page_body_error_preserves_detail_and_closed_transaction(tmp_path, exit_code, prefix, expected):
    message = prefix + "slides/slide_01.html: move existing body into SLIDE_AGENT_FILL_START/SLIDE_AGENT_FILL_END."
    tool, context, journal = runtime(tmp_path,
        f"import sys; print({message!r}); sys.exit({exit_code})")
    call = ToolCall("body-command", "terminal", {})
    result = tool.execute(context, call)
    assert result.ok == (exit_code == 0)
    assert (result.error_code or "") == expected
    payload = json.loads(result.content)
    assert message in payload["stdout"]
    if expected == "page_body_unfilled":
        assert payload["detail"] == message and payload["error_code"] == expected
    sequence = journal.sequence
    assert tool.execute(context, call) == result
    assert journal.sequence == sequence
    assert replay_tool_transactions(tool.events).results == 1
    tool.assert_closed()


def test_argparse_help_choices_and_wrapped_flags_are_not_json(tmp_path):
    text = 'usage: tool.py [-h]\n  {move-page,insert-page,delete-page}\n  [--revision-id REVISION_ID]\noptions:\n  --before UID\n'
    result = run_command([sys.executable, '-c', f'print({text!r},end="")'],
        cwd=tmp_path, timeout_s=3, environment={})
    assert result.stdout == text
    assert not result.diagnostics['truncated']


def test_completed_command_does_not_remove_a_concurrent_commands_tmp_parent(tmp_path):
    WorkspaceState(tmp_path)
    system = tmp_path / "_runtime/tmp/system"
    system.mkdir(parents=True, exist_ok=True)
    # Another command may already have prepared this environment but not started its child yet.
    environment = {**os.environ, "TMPDIR": str(system)}
    _run_command([sys.executable, "-c", "pass"], cwd=tmp_path, timeout_s=3, environment=environment)
    assert system.is_dir()
    result = _run_command([sys.executable, "-c",
                          "import os,tempfile; p=tempfile.TemporaryDirectory(dir=os.environ['TMPDIR']); p.cleanup()"],
                         cwd=tmp_path, timeout_s=3, environment=environment)
    assert result.returncode == 0 and system.is_dir()


def test_incomplete_json_retains_normal_fields_and_never_swallows_following_output(tmp_path):
    text = '{\n "messages": [\n "normal-message",\n "signature": "private-signature"\nstatus:PASS\n'
    result = run_command([sys.executable, '-c', f'print({text!r},end="")'],
        cwd=tmp_path, timeout_s=3, environment={})
    assert 'normal-message' in result.stdout and 'status:PASS' in result.stdout
    assert result.stdout == text
    assert not result.diagnostics['truncated']


def test_scaffold_css_receipt_and_normal_words_remain_visible(tmp_path):
    text = '--font-body: "Inter", sans-serif;\n--font-display-weight: 700;\nbody: 说明\nsystem: ready\ntoken: 2048\nstatus:PASS\nscaffold:13 pages\n'
    result = run_command([sys.executable, '-c', f'print({text!r},end="")'],
        cwd=tmp_path, timeout_s=3, environment={"MAX_TOKENS": "2048"})
    assert result.stdout == text
    assert not result.diagnostics['truncated'] and not result.diagnostics['omission_reasons']
    from runtime.live_trace import preview_value
    public = {"body": "page body", "system": "ready", "token": 2048, "font_token": "Inter"}
    assert preview_value(public) == public


def test_output_examples_and_urls_are_preserved_without_content_scanning(tmp_path):
    text = 'api_key="opaque-value" status:PASS pages:13\nAuthorization: Bearer private-token\nscaffold:13 pages\nhttps://example.test/?page=3&token=private-query\n'
    result = run_command([sys.executable, '-c', f'print({text!r},end="")'],
        cwd=tmp_path, timeout_s=3, environment={})
    assert result.stdout == text
    assert 'status:PASS pages:13' in result.stdout and 'scaffold:13 pages' in result.stdout
    assert 'page=3' in result.stdout and not result.diagnostics['truncated']


def test_command_environment_is_not_copied_into_output_records(tmp_path):
    with command_scope('connection-env', {'workspace_root': str(tmp_path)}, lambda _event: None):
        result = run_command([sys.executable, '-c', "print('status:PASS')"], cwd=tmp_path, timeout_s=3,
            environment={'MODEL_API_KEY': 'connection-only-fixture'})
    assert 'connection-only-fixture' not in (tmp_path / result.diagnostics['ref']).read_text()
    assert not result.diagnostics['redacted']


def test_output_is_durable_before_tool_returns_and_cursor_does_not_repeat(tmp_path):
    script = "from pathlib import Path\nimport time\nprint('asset-A: download started',flush=True)\nwhile not Path('release').exists(): time.sleep(.02)\nprint('asset-A: completed')"
    tool, context, _journal = runtime(tmp_path, script)
    results = []
    thread = threading.Thread(target=lambda: results.append(tool.execute(context, ToolCall("live-command", "terminal", {}))))
    thread.start()
    try:
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                payload = trace.tool_output_payload(str(tmp_path), "live-command")
                if any("download started" in row.get("text", "") for row in payload["rows"]):
                    break
            except FileNotFoundError:
                pass
            time.sleep(.02)
        else:
            pytest.fail("output was not readable while tool was running")
        assert thread.is_alive() and not payload["complete"]
        cursor = payload["next_cursor"]
        assert trace.tool_output_payload(str(tmp_path), "live-command", cursor=cursor)["rows"] == []
    finally:
        (tmp_path / "release").touch()
        thread.join(5)
    assert results[0].ok
    after = trace.tool_output_payload(str(tmp_path), "live-command", cursor=cursor)
    assert after["complete"] and any("completed" in row.get("text", "") for row in after["rows"])
    assert not any("download started" in row.get("text", "") for row in after["rows"])


def test_timeout_keeps_partial_output_and_closes_transaction(tmp_path):
    tool, context, _journal = runtime(tmp_path, "import time,sys; print('before timeout',flush=True); print('stderr before timeout',file=sys.stderr,flush=True); time.sleep(30)", timeout=3)
    result = tool.execute(context, ToolCall("timeout-command", "terminal", {}))
    payload = json.loads(result.content)
    assert not result.ok and payload["diagnostics"]["termination"] == "timeout"
    assert "before timeout" in payload["stdout"] and "stderr before timeout" in payload["stderr"]
    assert trace.tool_output_payload(str(tmp_path), "timeout-command")["complete"]
    tool.assert_closed()


def test_spawn_failure_has_no_exit_code_and_keeps_os_error(tmp_path):
    with command_scope("spawn-failed", {"workspace_root": str(tmp_path)}, lambda _event: None):
        with pytest.raises(CommandFailed) as caught:
            run_command([str(tmp_path / "missing-program")], cwd=tmp_path, timeout_s=3, environment=os.environ)
    result = caught.value.result
    assert result.returncode is None and result.diagnostics["termination"] == "spawn_failed"
    assert "FileNotFoundError" in result.stderr


def test_signal_exit_retains_stdout_and_signal_name(tmp_path):
    tool, context, _journal = runtime(tmp_path, "import os,signal; print('before signal',flush=True); os.kill(os.getpid(),signal.SIGTERM)")
    result = tool.execute(context, ToolCall("signal-command", "terminal", {}))
    payload = json.loads(result.content)
    assert not result.ok and payload["diagnostics"]["termination"] == "signal"
    assert payload["diagnostics"]["signal"] == "SIGTERM"
    assert "before signal" in payload["stdout"]


def test_output_stream_preserves_split_text_urls_and_json(tmp_path):
    script = """import os,sys,json,time
os.write(1,b'api_'); time.sleep(.05); os.write(1,b'key=opaque-private\\n')
print(os.environ['TEST_API_KEY'])
print('https://user:pw@example.test/a?token=private-query')
print(json.dumps({'thinking': 'private-thought', 'thinking_signature': 'private-signature', 'messages':[{'content':'normal-message'}], 'ok':'visible'},indent=2))
print('Cookie: private-cookie')
print('Authorization: Bearer private-bearer')
"""
    tool, context, _journal = runtime(tmp_path, script, environment={**os.environ, "TEST_API_KEY": "unrecognizable-value"})
    result = tool.execute(context, ToolCall("safe-command", "terminal", {}))
    assert result.ok
    files = "\n".join(p.read_text() for p in (tmp_path / "_trace").rglob("*.jsonl"))
    text = files + result.content
    for secret in ("opaque-private", "unrecognizable-value", "private-query", "user:pw", "private-thought", "private-signature", "private-cookie", "private-bearer"):
        assert secret in text
    assert "visible" in text
    assert "normal-message" in text


def test_output_limit_and_symlink_cursor_boundaries(tmp_path):
    WorkspaceState(tmp_path)
    events = []
    with command_scope("bounded-command", {"workspace_root": str(tmp_path)}, events.append):
        result = run_command([sys.executable, "-c", "print('abcdef'*1000)"], cwd=tmp_path, timeout_s=3, environment=os.environ, output_limit=1024)
    assert result.diagnostics["truncated"]
    assert (tmp_path / result.diagnostics["ref"]).stat().st_size < 2048
    tool, context, _journal = runtime(tmp_path, "print('hello')")
    tool.execute(context, ToolCall("safe-command", "terminal", {}))
    with pytest.raises(ValueError):
        trace.tool_output_payload(str(tmp_path), "safe-command", cursor=1)
    with pytest.raises(ValueError):
        trace.tool_output_payload(str(tmp_path), "../secret")
    with pytest.raises(FileNotFoundError):
        trace.tool_output_payload(str(tmp_path), "unknown-command")


def test_resume_closes_only_open_transaction_preserving_diagnostics(tmp_path):
    WorkspaceState(tmp_path)
    journal = LiveEventJournal(tmp_path / "_trace/live-events.jsonl")
    journal.record_tool_event({"event_type": "tool_use", "tool_use_id": "interrupted-command", "tool_name": "terminal", "owner_id": "image-owner", "role": "image"})
    with command_scope("interrupted-command", {"workspace_root": str(tmp_path)}, lambda p: journal.record_tool_event({"event_type":"tool_output", "tool_use_id":"interrupted-command", "tool_name":"terminal", "owner_id":"image-owner", "role":"image", **p})):
        run_command([sys.executable,"-c","print('saved before interruption')"], cwd=tmp_path, timeout_s=3, environment=os.environ)
    before = trace.tool_output_payload(str(tmp_path), "interrupted-command")
    reopened = LiveEventJournal(journal.path)
    assert len(reopened.close_interrupted_events()) == 1
    assert reopened.close_interrupted_events() == ()
    after = trace.tool_output_payload(str(tmp_path), "interrupted-command")
    assert before["rows"] == after["rows"] and after["complete"]


def test_image_partial_download_preserves_per_asset_http_and_decode_errors(tmp_path):
    scripts = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"
    script = f"""import sys
sys.path.insert(0, {str(scripts)!r})
from pathlib import Path
from io import BytesIO
from PIL import Image
from _internal import deck_core as dc
entries = [{{'id':key, 'kind':'real', 'status':'pending', 'path':'assets/'+key+'.png', 'download':'https://example.test/'+key}} for key in ('good','blocked','invalid')]
dc._registered_images = lambda root: entries
image=BytesIO(); Image.new('RGB',(8,8)).save(image,format='PNG')
def download(url, **kwargs):
    if url.endswith('blocked'):
        response=dc.requests.Response(); response.status_code=403
        raise dc.requests.HTTPError('access denied',response=response)
    return b'not an image' if url.endswith('invalid') else image.getvalue()
dc._download_image_payload = download
dc.fetch_images(Path('.'))
"""
    tool, context, _journal = runtime(tmp_path, script, timeout=30)
    result = tool.execute(context, ToolCall("partial-images", "terminal", {}))
    assert not result.ok
    payload = json.loads(result.content)
    records = {item["asset_id"]:item for item in payload["diagnostics"]["records"] if item["status"] != "started"}
    assert records["good"]["status"] == "succeeded"
    assert records["blocked"]["error_kind"] == "http" and records["blocked"]["http_status"] == 403
    assert records["invalid"]["error_kind"] == "image_decode"
    assert "access denied" in payload["stdout"] and "ValueError" in payload["stderr"]
    receipts = json.loads((tmp_path / "_trace/image-download-receipts.json").read_text())
    assert set(receipts["assets"]) == {"good"}


def test_owner_may_read_own_diagnostics_but_not_another_owners(tmp_path):
    workspace = WorkspaceState(tmp_path)
    tool, context, _journal = runtime(tmp_path, "print('own diagnostic evidence')")
    result = tool.execute(context, ToolCall("owned-output", "terminal", {}))
    relative = json.loads(result.content)["diagnostics"]["ref"]
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = workspace
    assert "own diagnostic evidence" in services.read_file(context, {"path":relative})["content"]
    assert "own diagnostic evidence" in services.read_file(ToolContext("orchestrator","orchestrator"), {"path":relative})["content"]
    with pytest.raises(ToolContractError):
        services.read_file(ToolContext("another-owner","image"), {"path":relative})


def test_plain_bracket_logs_are_not_mistaken_for_json_and_long_lines_are_marked(tmp_path):
    tool, context, _journal = runtime(tmp_path, "print('[INFO] readable log'); print('x'*70000); print('after large output')")
    payload = json.loads(tool.execute(context, ToolCall("long-line","terminal",{})).content)
    assert "[INFO] readable log" in payload["stdout"] and "after large output" in payload["stdout"]
    assert payload["diagnostics"]["truncated"]
    assert "line_limit" in payload["diagnostics"]["omission_reasons"]


def test_browser_expands_live_stdout_and_later_failure_without_duplicates(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    from app import engine
    studio = Path(__file__).resolve().parents[3] / "webui/studio"
    browser_env = engine.render_env({**os.environ, "PPTAGENT_ENGINE_PYTHON": str(studio.parent / "inference/.venv/bin/python"), "TMPDIR":str(tmp_path)})
    executable = browser_env.get("PPT_SKILL_BROWSER_EXE")
    if not executable:
        pytest.skip("deployed Chromium is unavailable")
    source = (studio / "static/app.js").read_text()
    names = ("escapeHtml", "workflowToolOutputState", "workflowToolOutputBody", "workflowToolOutputMarkup", "loadWorkflowToolOutput", "bindWorkflowTraceControls")
    functions = []
    for name in names:
        start = re.search(r"^(?:async )?function " + name + r"\(", source, re.M).start()
        end = re.search(r"^(?:async )?function ", source[start+1:], re.M)
        functions.append(source[start:start+1+end.start()] if end else source[start:])
    script = "from pathlib import Path\nimport time,sys\nprint('asset-A: download started',flush=True)\nwhile not Path('release').exists(): time.sleep(.02)\nprint('asset-A: HTTP 403',flush=True)\nprint('generic download failure',file=sys.stderr)\nsys.exit(1)"
    tool, context, _journal = runtime(tmp_path, script, timeout=30)
    results = []
    thread = threading.Thread(target=lambda: results.append(tool.execute(context, ToolCall("browser-command","terminal",{}))))
    thread.start()
    try:
        with playwright.sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=executable, env=browser_env, headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width":1000,"height":760})
                page.route("http://diagnostics.test/", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body='<h2>工具诊断 · 模拟事件验收</h2><main id="workflow-trace-messages" style="margin:24px;max-width:920px"></main>'))
                def response(route):
                    from urllib.parse import urlsplit, parse_qs
                    cursor = int(parse_qs(urlsplit(route.request.url).query).get("cursor",[0])[0])
                    route.fulfill(content_type="application/json", body=json.dumps(trace.tool_output_payload(str(tmp_path),"browser-command",cursor=cursor)))
                page.route("**/api/decks/217/tool-output/**", response)
                page.goto("http://diagnostics.test/")
                page.add_style_tag(path=str(studio / "static/app.css"))
                page.add_script_tag(content="\n".join(functions) + "\nconst ed={id:217},trajectoryMode=false; const $=s=>document.querySelector(s); $('#workflow-trace-messages').innerHTML=workflowToolOutputMarkup('browser-command'); bindWorkflowTraceControls();")
                page.click("[data-tool-output-toggle]")
                page.wait_for_function("async()=>{await loadWorkflowToolOutput('browser-command',{force:true});return document.querySelector('.workflow-tool-output-body').textContent.includes('download started')}")
                assert thread.is_alive()
                assert "工具执行中" in page.locator(".workflow-tool-output-body").inner_text()
                page.screenshot(path=str(tmp_path / "tool-output-running.png"), animations="disabled")
                (tmp_path / "release").touch()
                thread.join(5)
                assert not results[0].ok
                page.evaluate("loadWorkflowToolOutput('browser-command',{force:true})")
                text = page.locator(".workflow-tool-output-body").inner_text()
                assert "工具执行失败" in text and "HTTP 403" in text and "generic download failure" in text
                # argv 中保留脚本原文；真实 stdout 事件仍只有一条。
                assert text.count("download started") == 2
                rows = trace.tool_output_payload(str(tmp_path), 'browser-command')['rows']
                assert sum(row.get('stream') == 'stdout' and 'download started' in row.get('text', '') for row in rows) == 1
                page.evaluate("loadWorkflowToolOutput('browser-command',{force:true})")
                assert page.locator(".workflow-tool-output-body").inner_text() == text
                page.screenshot(path=str(tmp_path / "tool-output-failed.png"), animations="disabled")
            finally:
                browser.close()
    finally:
        (tmp_path / "release").touch()
        thread.join(5)
