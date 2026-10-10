import asyncio
from html.parser import HTMLParser
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode

import pytest

from app import service_config


def test_static_runtime_defaults_match_mural_v1_contract() -> None:
    assert service_config.DEFAULT_MAX_TOKENS == 40960
    assert service_config.DEFAULT_STATIC_MAX_TURNS == 240
    assert service_config.DEFAULT_STATIC_SUBAGENT_MAX_TURNS == 240
    assert service_config.DEFAULT_DYNAMIC_MAX_TURNS == 4096
    assert service_config.DEFAULT_STREAMING_ENABLED is False


def test_streaming_checkbox_defaults_off_when_server_field_is_missing() -> None:
    source = (Path(__file__).resolve().parents[1] / "static/app.js").read_text()
    assert '$("#service-streaming-enabled").checked = generation.streaming_enabled === true;' in source
    reset = source[source.index('$("#service-generation-reset")'):]
    assert '$("#service-streaming-enabled").checked = false;' in reset


def test_owner_budget_is_exported_under_v1_environment_name(monkeypatch):
    monkeypatch.setattr(service_config, 'runtime_payload', lambda _: {
        'generation': {'max_tokens': 4096, 'dynamic_max_turns': 100, 'static_max_turns': 80,
                       'static_subagent_max_turns': 60, 'streaming_enabled': True}})
    env = service_config.runtime_env(1)
    assert env['OWNER_MAX_TURNS'] == '60'
    assert env['AGENT_MAX_TOKENS'] == '4096'
    assert env['OWNER_MAX_TOKENS'] == '4096'
    assert env['ORCHESTRATOR_MAX_TOKENS'] == '4096'
    assert env['SLIDE_OWNER_MAX_TOKENS'] == '4096'
    assert env['RESEARCH_OWNER_MAX_TOKENS'] == '4096'
    assert env['IMAGE_OWNER_MAX_TOKENS'] == '4096'
    assert env['CRITIC_MAX_TOKENS'] == '4096'


def test_role_token_env_caps_static_v1_routine_turns():
    assert service_config.role_token_env(40960) == {
        'ORCHESTRATOR_MAX_TOKENS': '16384',
        'SLIDE_OWNER_MAX_TOKENS': '20000',
        'RESEARCH_OWNER_MAX_TOKENS': '12000',
        'IMAGE_OWNER_MAX_TOKENS': '8192',
        'CRITIC_MAX_TOKENS': '8192',
    }


def test_shared_provider_file_is_the_only_deployment_image_default(tmp_path, monkeypatch):
    shared = tmp_path / "providers.env"
    shared.write_text("IMAGE_BASE_URL=https://image.test/v1/\nIMAGE_API_KEY=shared-key\n"
                      "IMAGE_MODEL=gpt-image-2-pro-all\nSERPER_API_KEY=search-key\n"
                      "SERPER_BASE_URL=https://search.test\n")
    monkeypatch.setattr(service_config, "PROVIDER_ENV_PATH", shared, raising=False)
    monkeypatch.setenv("SENSENOVA_IMAGE_MODEL", "retired-adobe")
    monkeypatch.setenv("SENSENOVA_IMAGE_API_KEY", "stale-key")
    env = service_config.system_runtime_env()
    assert env == {"IMAGE_PROVIDER": "openai_images", "IMAGE_BASE_URL": "https://image.test/v1",
                   "IMAGE_API_KEY": "shared-key", "IMAGE_MODEL": "gpt-image-2-pro-all",
                   "SERPER_BASE_URL": "https://search.test", "SERPER_API_KEY": "search-key"}


def test_visual_critic_has_one_control_inside_user_settings() -> None:
    class Locations(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parents = []
            self.locations = {}

        def handle_starttag(self, tag, attrs):
            element_id = dict(attrs).get("id")
            if element_id:
                self.locations.setdefault(element_id, []).append(tuple(self.parents))
            if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
                self.parents.append((tag, element_id))

        def handle_endtag(self, tag):
            for index in range(len(self.parents) - 1, -1, -1):
                if self.parents[index][0] == tag:
                    del self.parents[index:]
                    break

    parser = Locations()
    template = Path(__file__).resolve().parents[1] / "templates/app.html"
    parser.feed(template.read_text())
    for element_id in ("service-visual-critic-control", "service-visual-critic-enabled", "service-visual-critic-help"):
        assert len(parser.locations[element_id]) == 1
        ancestor_ids = {element[1] for element in parser.locations[element_id][0]}
        assert {"service-dialog", "service-form"} <= ancestor_ids
        assert "model-dialog" not in ancestor_ids
        assert "common-settings" not in ancestor_ids
    assert "visual-critic-toggle" not in parser.locations
    source = (template.parent.parent / "static/app.js").read_text()
    assert "studio_visual_critic" not in source
    assert 'body.set("enable_visual_critic"' not in source
    assert 'enable_visual_critic: $("#service-visual-critic-enabled").checked' in source


def test_title_task_model_is_saved_per_account(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(service_config, "CONFIG_DIR", tmp_path)
    assert service_config.title_task_model_key(17) == service_config.DEFAULT_TITLE_TASK_MODEL
    service_config.update(
        17,
        image_enabled=False,
        image_base_url="",
        image_model="",
        image_api_key=None,
        clear_image_api_key=False,
        search_enabled=False,
        search_base_url="",
        search_api_key=None,
        clear_search_api_key=False,
        title_task_model="custom:27",
    )
    assert service_config.title_task_model_key(17) == "custom:27"


@pytest.fixture
def critic_settings_api(tmp_path, monkeypatch):
    from app import db, main

    monkeypatch.setattr(service_config, "CONFIG_DIR", tmp_path / "user-configs")
    monkeypatch.setattr(service_config, "system_runtime_env", lambda: {})
    monkeypatch.setattr(main.dynamic, "_model_registry", lambda user: {
        service_config.DEFAULT_TITLE_TASK_MODEL: {"model": "test-model"},
    })
    monkeypatch.setattr(main, "_generation_selection", lambda *args:
                        ("deployment-model", "mural-presenter-v1", "mural-presenter-v1"))
    monkeypatch.setattr(main.engine, "resolve_thinking", lambda *args, **kwargs: {
        "requested": False, "effective": False, "mode": "off", "transport": "none",
        "requested_effort": "", "effective_effort": "",
    })
    monkeypatch.setattr(main.jobs, "enqueue", lambda *args: None)
    monkeypatch.setattr(main.titles, "schedule_summary", lambda *args: None)
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    user = {"id": 17}
    monkeypatch.setattr(main.app, "dependency_overrides", {
        **main.app.dependency_overrides,
        main.current_user: lambda: user,
        main.get_db: lambda: con,
    })

    def request(method, path, payload=None, *, form=False, files=None):
        async def run():
            body = (urlencode(payload) if form else json.dumps(payload)).encode()
            content_type = b"application/x-www-form-urlencoded" if form else b"application/json"
            if files:
                boundary = "mural-test-boundary"
                parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                         for key, value in payload.items()]
                parts.extend(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{name}"\r\n'
                             f'Content-Type: application/json\r\n\r\n{value}\r\n'
                             for key, (name, value) in files.items())
                body = ("".join(parts) + f"--{boundary}--\r\n").encode()
                content_type = f"multipart/form-data; boundary={boundary}".encode()
            messages = []

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            async def send(message):
                messages.append(message)

            await main.app({
                "type": "http", "asgi": {"version": "3.0"}, "method": method,
                "path": path, "raw_path": path.encode(), "root_path": "",
                "scheme": "http", "query_string": b"", "http_version": "1.1",
                "headers": [(b"content-type", content_type)],
                "client": ("test", 1), "server": ("test", 80),
            }, receive, send)
            status = next(item["status"] for item in messages if item["type"] == "http.response.start")
            data = json.loads(b"".join(item.get("body", b"") for item in messages
                                       if item["type"] == "http.response.body"))
            assert status == 200, data
            return data

        return asyncio.run(run())

    try:
        yield request, con, user
    finally:
        con.close()


@pytest.mark.parametrize("query,lang,expected", [
    ("Create a bedtime dinosaur presentation", None, "en"),
    ("Create a bedtime dinosaur presentation", "", "en"),
    ("Create a bedtime dinosaur presentation", "auto", "en"),
    ("制作一份睡前恐龙故事演示", None, "zh"),
    ("制作一份睡前恐龙故事演示", "auto", "zh"),
    ("Create a bedtime dinosaur presentation", "zh", "zh"),
    ("制作一份睡前恐龙故事演示", "en", "en"),
    ("制作一份关于 AI 和 UI 的演示", "auto", "zh"),
    ("Create an AI presentation about 北京", "auto", "en"),
])
def test_deck_language_selection_and_query_detection(critic_settings_api, query, lang, expected):
    request, con, _user = critic_settings_api
    form = {"query": query}
    if lang is not None:
        form["lang"] = lang
    deck = request("POST", "/api/decks", form, form=True)
    seed = json.loads(con.execute("SELECT seed_json FROM decks WHERE id=?", (deck["deck_id"],)).fetchone()[0])
    assert seed["lang"] == expected
    assert seed["query"] == query


def test_critic_setting_roundtrips_per_account_and_keeps_existing_deck(critic_settings_api):
    request, con, user = critic_settings_api
    endpoint = "/api/settings/services"
    assert request("GET", endpoint)["generation"]["enable_visual_critic"] is False
    assert request("PUT", endpoint, {"enable_visual_critic": True})["generation"]["enable_visual_critic"] is True
    assert request("PUT", endpoint, {})["generation"]["enable_visual_critic"] is True
    deck = request("POST", "/api/decks", {"query": "3 页测试"}, form=True)
    request("PUT", endpoint, {"enable_visual_critic": False})
    assert request("GET", endpoint)["generation"]["enable_visual_critic"] is False
    seed = json.loads(con.execute("SELECT seed_json FROM decks WHERE id=?", (deck["deck_id"],)).fetchone()[0])
    assert seed["requested_visual_critic"] is True
    user["id"] = 18
    assert request("GET", endpoint)["generation"]["enable_visual_critic"] is False
    request("PUT", endpoint, {"enable_visual_critic": True})
    user["id"] = 17
    assert request("GET", endpoint)["generation"]["enable_visual_critic"] is False


@pytest.mark.parametrize("saved,override,mode,requested,effective", [
    (False, None, "inference", False, False),
    (True, None, "inference", True, True),
    (True, 0, "inference", False, False),
    (False, 1, "inference", True, True),
    (False, None, "synthesis", False, False),
    (True, 0, "synthesis", False, False),
])
def test_new_deck_pins_critic_request_and_effective_policy(
    critic_settings_api, saved, override, mode, requested, effective,
):
    from app import engine

    request, con, _user = critic_settings_api
    request("PUT", "/api/settings/services", {"enable_visual_critic": saved})
    form = {"query": "3 页测试", "run_mode": mode}
    if override is not None:
        form["enable_visual_critic"] = override
    deck = request("POST", "/api/decks", form, form=True)
    seed = json.loads(con.execute("SELECT seed_json FROM decks WHERE id=?", (deck["deck_id"],)).fetchone()[0])
    assert seed["requested_visual_critic"] is requested
    assert seed["effective_visual_critic"] is effective
    assert seed["runtime_limits"]["requested_visual_critic"] is requested
    assert seed["runtime_limits"]["effective_visual_critic"] is effective
    assert seed["runtime_limits"]["enable_visual_critic"] is effective
    assert engine.external_visual_critic_env(seed["enable_visual_critic"], seed["run_mode"]) == {
        "ENABLE_EXTERNAL_VISUAL_CRITIC": "1" if effective else "0",
    }


@pytest.mark.parametrize("mode,saved,override,effective", [
    (None, False, None, False),
    ("inference", True, None, True),
    ("synthesis", False, None, False),
    ("synthesis", True, 0, False),
])
def test_batch_pins_run_mode_and_critic_policy(critic_settings_api, mode, saved, override, effective):
    request, con, _user = critic_settings_api
    request("PUT", "/api/settings/services", {"enable_visual_critic": saved})
    fields = {"model": "deployment-model", "skill": "mural-presenter-v1", "thinking": "0"}
    if mode is not None:
        fields["run_mode"] = mode
    if override is not None:
        fields["enable_visual_critic"] = str(override)
    response = request("POST", "/api/batches", fields, files={
        "query_file": ("queries.jsonl", '{"query":"三页测试"}\n{"query":"Three slides"}\n'),
    })
    decks = response["decks"]
    assert len(decks) == 2
    for deck in decks:
        seed = json.loads(con.execute("SELECT seed_json FROM decks WHERE id=?", (deck["id"],)).fetchone()[0])
        assert seed["run_mode"] == (mode or "inference")
        assert seed["requested_visual_critic"] is (saved if override is None else bool(override))
        assert seed["effective_visual_critic"] is effective


def test_batch_form_exposes_explicit_synthesis_mode():
    class Modes(HTMLParser):
        active = False
        values = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "select":
                self.active = attrs.get("name") == "run_mode"
            if tag == "option" and self.active:
                self.values.append(attrs["value"])

        def handle_endtag(self, tag):
            if tag == "select":
                self.active = False

    template = Path(__file__).resolve().parents[1] / "templates/app.html"
    form = template.read_text().split('id="batch-form"', 1)[1].split("</form>", 1)[0]
    parser = Modes()
    parser.feed(form)
    assert parser.values == ["inference", "synthesis"]


def test_critic_settings_browser(tmp_path):
    import os
    from app import engine, main
    from playwright.sync_api import sync_playwright
    from test_startup_observability import _js_source

    studio = Path(__file__).resolve().parents[1]
    html = main.templates.env.get_template("app.html").render(
        request={"scope": {}},
        authenticated=True, auth_enabled=False, dynamic_enabled=False, edition="v1",
        user={"id": 17, "display_name": "测试用户", "role": "user"},
        models=[("deployment-model", "测试模型", "openai")], pipelines=[], skills=[],
        default_model="deployment-model", default_pipeline="mural-presenter-v1",
        default_skill="mural-presenter-v1",
    )
    script = "const $=s=>document.querySelector(s); let serviceSettings=null;\n" + _js_source([
        "syncServiceCards", "syncImageProviderFields", "applyServiceSettings",
    ])
    browser_env = engine.render_env({**os.environ, "TMPDIR": str(tmp_path)})
    executable = browser_env.get("PPT_SKILL_BROWSER_EXE")
    if not executable:
        pytest.skip("deployed Chromium is not available")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=executable, env=browser_env,
                                     headless=True, args=["--no-sandbox"])
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.route("**/*", lambda route: route.abort())
            page.set_content(html)
            page.add_style_tag(content=(studio / "static/app.css").read_text())
            page.add_script_tag(content=script)
            page.evaluate("document.querySelector('#service-modal').hidden=false")
            control = page.locator("#service-visual-critic-control")
            checkbox = page.locator("#service-visual-critic-enabled")
            page.evaluate("applyServiceSettings({generation:{}})")
            assert not checkbox.is_checked()
            page.evaluate("applyServiceSettings({generation:{enable_visual_critic:true}})")
            assert checkbox.is_checked()
            control.locator(".service-switch").click()
            assert not checkbox.is_checked()
            for width, label in ((1280, "desktop"), (390, "narrow")):
                page.set_viewport_size({"width": width, "height": 900})
                page.evaluate("applyServiceSettings({generation:{enable_visual_critic:true}})")
                assert control.get_by_text("独立视觉 Critic", exact=True).is_visible()
                assert control.locator(".service-switch").is_visible()
                assert page.locator("#canvas-geometry").count() == 0
                assert control.evaluate("el=>el.scrollWidth<=el.clientWidth+1")
                page.screenshot(path=str(tmp_path / f"critic-settings-{label}.png"))
        finally:
            browser.close()
