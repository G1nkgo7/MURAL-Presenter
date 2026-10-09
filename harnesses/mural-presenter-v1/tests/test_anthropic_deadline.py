"""本地真实 HTTP 验收：总时限取消连接后才释放模型资源槽。"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import threading
import time

import httpx
import pytest

from runtime.config import ProviderSettings
from runtime.production import ProductionSettings, ResourceSlots
from runtime.session import AnthropicTransport, ModelRequest, ModelSession, ProviderChannel, replay_provider_events


@pytest.mark.parametrize("mode", ["silent", "trickle"])
def test_nonstreaming_total_deadline_closes_real_socket_before_slot_release(tmp_path, monkeypatch, mode):
    disconnected = threading.Event()
    stopped = threading.Event()
    calls = []
    client_closed = []
    original_close = httpx.AsyncClient.aclose

    async def close_client(client):
        await original_close(client)
        client_closed.append(True)

    monkeypatch.setattr(httpx.AsyncClient, "aclose", close_client)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            calls.append(self.path)
            self.connection.settimeout(3)
            if mode == "silent":
                if not self.connection.recv(1):
                    disconnected.set()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100000")
            self.end_headers()
            try:
                while not stopped.wait(.02):
                    self.wfile.write(b" ")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                disconnected.set()

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .02})
    worker.start()
    settings = ProviderSettings(api_format="anthropic", model="synthetic-model",
        base_url=f"http://127.0.0.1:{server.server_port}", streaming=False,
        total_timeout_s=1, max_attempts=1, prompt_cache=False, prompt_cache_messages=False)
    skill_source = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    slots = ResourceSlots(tmp_path, ProductionSettings(skill_source=skill_source, model_concurrency=1), environment={})

    @contextmanager
    def slot():
        with slots.acquire("model", deadline_monotonic=time.monotonic() + 2):
            try:
                yield
            finally:
                assert client_closed, "client cleanup must finish before the slot is released"

    events = []
    session = ModelSession(settings=settings, channels=(ProviderChannel("synthetic-key", "local"),),
        transport=AnthropicTransport(settings), event_sink=events.append,
        slot_factory=slot)
    request = ModelRequest(model=settings.model, system="synthetic",
        messages=({"role": "user", "content": "go"},), max_tokens=32)
    started = time.monotonic()
    try:
        result = session.call(request)
        elapsed = time.monotonic() - started
        assert result.failure.kind == "timeout" and result.failure.timeout_phase == "total_request"
        assert .8 <= elapsed < 2.5
        assert disconnected.wait(1), f"cancelled client must close its socket: calls={calls}, closed={client_closed}, elapsed={elapsed:.3f}s"
        assert calls == ["/v1/messages"]
        with slots.acquire("model", deadline_monotonic=time.monotonic() + .2):
            pass
        assert session.usage.summary()["attempts"] == session.usage.summary()["failed_attempts"] == 1
        replay = replay_provider_events(events)
        assert replay.failures == 1 and not replay.open_attempt_ids
        assert events[-1]["event_type"] == "provider_attempt_failed"
    finally:
        stopped.set()
        server.shutdown()
        worker.join(4)
        server.server_close()
    assert not worker.is_alive()


def test_sync_anthropic_entry_rejects_nested_running_loop():
    settings = ProviderSettings(api_format="anthropic", model="synthetic", base_url="https://provider.test")
    transport = AnthropicTransport(settings)
    request = ModelRequest(model="synthetic", system="synthetic", messages=(), max_tokens=32)

    async def invoke():
        with pytest.raises(RuntimeError, match="synchronous caller"):
            transport.complete(ProviderChannel("synthetic", "local"), request, total_timeout_s=1)

    asyncio.run(invoke())


def test_unlimited_request_waits_for_result_and_remains_cancellable():
    from runtime.session import _request_with_deadline, _PUBLIC_DELTA_LOCAL

    async def scenario():
        async def complete():
            await asyncio.sleep(.02)
            return "actual result"
        assert await _request_with_deadline(complete(), None) == "actual result"
        stop, closed = threading.Event(), threading.Event()
        previous = getattr(_PUBLIC_DELTA_LOCAL, "stop_event", None)
        _PUBLIC_DELTA_LOCAL.stop_event = stop
        async def pending():
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        try:
            asyncio.get_running_loop().call_later(.02, stop.set)
            with pytest.raises(InterruptedError, match="owner_interrupted"):
                await _request_with_deadline(pending(), None)
            assert closed.is_set()
        finally:
            _PUBLIC_DELTA_LOCAL.stop_event = previous
    asyncio.run(scenario())
