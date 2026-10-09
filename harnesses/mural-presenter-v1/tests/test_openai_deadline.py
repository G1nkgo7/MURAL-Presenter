"""真实 localhost HTTP 验收：持续字节不能延长总时限，取消先于槽位释放。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
import time

import httpx
import pytest

from runtime.config import ProviderSettings
from runtime.production import ProductionSettings, ResourceSlots
from runtime.session import (
    ModelRequest, ModelSession, OpenAIChatCompatibleTransport,
    OpenAIResponsesTransport, ProviderChannel, replay_provider_events,
)


TRANSPORTS = [
    ("openai_chat_compatible", OpenAIChatCompatibleTransport, "/v1/chat/completions"),
    ("openai_responses", OpenAIResponsesTransport, "/v1/responses"),
]


@pytest.mark.parametrize("api_format,transport_type,endpoint", TRANSPORTS)
@pytest.mark.parametrize("mode", ["silent", "trickle", "stream_partial", "stream_delta"])
def test_real_deadline_cancels_io_before_releasing_slot(
    tmp_path, monkeypatch, api_format, transport_type, endpoint, mode,
):
    disconnected = threading.Event()
    stopped = threading.Event()
    calls = []
    closed_clients = []
    closed_at_slot_release = []
    original_exit = httpx.AsyncClient.__aexit__

    async def exit_client(client, *args):
        await original_exit(client, *args)
        closed_clients.append(client.is_closed)

    monkeypatch.setattr(httpx.AsyncClient, "__aexit__", exit_client)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(self.path)
            self.connection.settimeout(2)
            if mode == "silent":
                if not self.connection.recv(1):
                    disconnected.set()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if mode.startswith("stream") else "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            if mode == "stream_delta":
                event = ({"choices": [{"delta": {"content": "x"}}]}
                         if api_format == "openai_chat_compatible"
                         else {"type": "response.output_text.delta", "delta": "x"})
                chunk = ("data: " + json.dumps(event) + "\n\n").encode()
            else:
                chunk = b" "
            end = time.monotonic() + 1.5
            try:
                while time.monotonic() < end and not stopped.wait(.02):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
    worker.start()
    settings = ProviderSettings(
        api_format=api_format, model="synthetic", base_url=f"http://127.0.0.1:{server.server_port}",
        streaming=mode.startswith("stream"), total_timeout_s=2, idle_timeout_s=2,
        max_attempts=1, prompt_cache=False, prompt_cache_messages=False,
    )
    skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    slots = ResourceSlots(tmp_path, ProductionSettings(skill_source=skill, model_concurrency=1), environment={})

    @contextmanager
    def slot():
        with slots.acquire("model", deadline_monotonic=time.monotonic() + 2):
            try:
                yield
            finally:
                closed_at_slot_release.append(bool(closed_clients) and all(closed_clients))

    events = []
    session = ModelSession(
        settings=settings, channels=(ProviderChannel("synthetic-key", "local"),),
        transport=transport_type(settings), event_sink=events.append, slot_factory=slot,
    )
    request = ModelRequest(model="synthetic", system="synthetic", messages=({"role": "user", "content": "go"},), max_tokens=32)
    started = time.monotonic()
    try:
        result = session.call(request, request_budget_s=.4)
        elapsed = time.monotonic() - started
        assert .3 <= elapsed < 1.1, f"total deadline overrun: {elapsed:.3f}s"
        assert result.failure.kind == "timeout" and result.failure.timeout_phase == "total_request"
        assert disconnected.wait(.5), "cancelled request must close its real socket"
        assert calls == [endpoint]
        assert closed_at_slot_release == [True]
        with slots.acquire("model", deadline_monotonic=time.monotonic() + .2):
            pass
        assert session.usage.summary()["attempts"] == session.usage.summary()["failed_attempts"] == 1
        replay = replay_provider_events(events)
        assert replay.failures == 1 and not replay.open_attempt_ids
        assert events[-1]["event_type"] == "provider_attempt_failed"
    finally:
        stopped.set()
        server.shutdown()
        worker.join(3)
        server.server_close()
    assert not worker.is_alive()


@pytest.mark.parametrize("api_format,transport_type,endpoint", TRANSPORTS)
def test_sync_openai_entry_rejects_nested_running_loop(api_format, transport_type, endpoint):
    settings = ProviderSettings(api_format=api_format, model="synthetic", base_url="http://127.0.0.1:1")
    transport = transport_type(settings)
    request = ModelRequest(model="synthetic", system="synthetic", messages=(), max_tokens=32)

    async def invoke():
        with pytest.raises(RuntimeError, match="synchronous caller"):
            transport.complete(ProviderChannel("synthetic", "local"), request, total_timeout_s=1)

    asyncio.run(invoke())
