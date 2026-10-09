"""runtime.json 的全局资源池保留跨 Deck、跨进程 FIFO 与恢复语义。"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
import fcntl
import json
import multiprocessing
import threading
import time

import pytest

from runtime.config import load_runtime_config
from runtime.production import ProductionError, ProductionSettings, ResourceSlots, ResourceSlotTimeout


@pytest.fixture(autouse=True)
def runtime_configuration(tmp_path, monkeypatch):
    config = load_runtime_config()
    config["resources"]["directory"] = str(tmp_path / "shared-slots")
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("MURAL_RUNTIME_CONFIG", str(path))
    return {"MURAL_RUNTIME_CONFIG": str(path)}


def _settings(environment=None) -> ProductionSettings:
    return ProductionSettings.from_environment(environment)


@contextmanager
def _hold_capacity(slots, resource="model", *, available=0):
    # 使用真实 JSON 预算；FIFO 用例只暂留一两个空槽，不另造低容量配置。
    assert slots.limits[resource] >= available
    with ExitStack() as holders:
        for _ in range(slots.limits[resource] - available):
            holders.enter_context(slots.acquire(resource, deadline_monotonic=time.monotonic() + 5))
        yield


def test_default_slot_root_comes_from_runtime_json(tmp_path: Path) -> None:
    slots = ResourceSlots(tmp_path, _settings(), environment={})
    assert slots.root == Path(load_runtime_config()["resources"]["directory"])
    assert slots.root != (tmp_path / "_state" / "resource-slots").resolve()
    assert slots.root.is_dir()


def test_runtime_json_ignores_legacy_directory_and_capacity_overrides(tmp_path: Path, runtime_configuration) -> None:
    env = {**runtime_configuration, "MURAL_RESOURCE_SLOTS_DIR": str(tmp_path / "ignored"),
           "MODEL_CONCURRENCY": "1", "SLIDE_CONCURRENCY": "1", "RENDER_CONCURRENCY": "1",
           "PPT_RENDER_CONCURRENCY": "1", "WEB_SEARCH_CONCURRENCY": "99", "IMAGE_GENERATION_CONCURRENCY": "99"}
    resources = load_runtime_config(env)["resources"]
    settings = _settings(env)
    deck_a = tmp_path / "deck-a"
    deck_b = tmp_path / "deck-b"
    slots_a = ResourceSlots(deck_a, settings, environment=env)
    slots_b = ResourceSlots(deck_b, settings, environment=env)
    assert slots_a.root == slots_b.root == Path(resources["directory"])
    assert not (tmp_path / "ignored").exists()
    assert settings.model_concurrency == slots_a.limits["model"] == resources["provider"]
    assert settings.render_concurrency == slots_a.limits["render"] == resources["render"]
    assert settings.web_search_concurrency == resources["web_search"]
    assert settings.image_generation_concurrency == resources["image_generation"]


@pytest.mark.parametrize("resource", ["model", "render", "web_search"])
def test_shared_root_enforces_cross_deck_global_limit(tmp_path: Path, runtime_configuration, resource) -> None:
    slots_a = ResourceSlots(tmp_path / "a", _settings(), environment=runtime_configuration)
    slots_b = ResourceSlots(tmp_path / "b", _settings(), environment=runtime_configuration)
    with _hold_capacity(slots_a, resource):
        with pytest.raises(ResourceSlotTimeout):
            with slots_b.acquire(
                resource, deadline_monotonic=time.monotonic() + 0.3
            ):
                pass


def test_model_and_render_have_distinct_global_budgets(tmp_path: Path) -> None:
    slots_a = ResourceSlots(tmp_path / "a", _settings(), environment={})
    slots_b = ResourceSlots(tmp_path / "b", _settings(), environment={})
    assert slots_a.root == slots_b.root
    with _hold_capacity(slots_a, "model"):
        with slots_b.acquire("render", deadline_monotonic=time.monotonic() + 0.5):
            pass


def test_different_models_endpoints_and_critic_cannot_bypass_global_capacity(tmp_path: Path, runtime_configuration) -> None:
    env = {**runtime_configuration, "MODEL_BASE_URL": "https://a.test/v1", "MODEL": "27b"}
    a = ResourceSlots(tmp_path / "a", _settings(), environment=env)
    b = ResourceSlots(tmp_path / "b", _settings(), environment=env)
    other = ResourceSlots(tmp_path / "c", _settings(), environment={**env, "MODEL_BASE_URL": "https://b.test/v1"})
    with _hold_capacity(a):
        with pytest.raises(ResourceSlotTimeout):
            with b.acquire("critic", deadline_monotonic=time.monotonic() + .1):
                pass
        with pytest.raises(ResourceSlotTimeout):
            with b.acquire("model", model="different-model", deadline_monotonic=time.monotonic() + .1):
                pass
        with pytest.raises(ResourceSlotTimeout):
            with other.acquire("model", deadline_monotonic=time.monotonic() + .1):
                pass
    assert len(list(a.root.glob("model-*.queue"))) == 1
    assert not list(a.root.glob("critic-*.queue"))


def test_process_exit_releases_shared_slot(tmp_path: Path, runtime_configuration) -> None:
    context = multiprocessing.get_context("fork")
    env = runtime_configuration
    ready = context.Event()
    def hold():
        slots = ResourceSlots(tmp_path / "child", _settings(), environment=env)
        with _hold_capacity(slots, "web_search"):
            ready.set()
            time.sleep(10)
    child = context.Process(target=hold)
    child.start()
    try:
        assert ready.wait(5)
        parent_slots = ResourceSlots(tmp_path / "parent", _settings(), environment=env)
        with pytest.raises(ResourceSlotTimeout):
            with parent_slots.acquire("web_search", deadline_monotonic=time.monotonic() + .1):
                pass
        child.terminate()
        child.join(5)
        with _hold_capacity(parent_slots, "web_search"):
            pass
    finally:
        if child.is_alive():
            child.terminate()
        child.join(5)


def _wait_for_ticket_count(root: Path, count: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if len(list(root.glob("*.queue/*.ticket"))) == count:
            return
        time.sleep(.01)
    assert len(list(root.glob("*.queue/*.ticket"))) == count


def _waiter(root, environment, settings, label, results, release, timeout=5):
    try:
        with ResourceSlots(root, settings, environment=environment).acquire(
            "model", deadline_monotonic=time.monotonic() + timeout
        ):
            results.put((label, "acquired"))
            assert release.wait(5)
    except ResourceSlotTimeout:
        results.put((label, "timeout"))


def _cancelled_waiter(root, environment, settings, results, stop):
    try:
        with ResourceSlots(root, settings, environment=environment).acquire(
            "model", deadline_monotonic=time.monotonic() + 5, stop_event=stop
        ):
            results.put(("cancelled", "acquired"))
    except InterruptedError:
        results.put(("cancelled", "interrupted"))


def _finish(children, releases):
    for release in releases:
        release.set()
    for child in children:
        child.join(2)
        if child.is_alive():
            child.terminate()
            child.join(2)


def test_four_waiters_keep_ticket_order_after_holder_failure(tmp_path, runtime_configuration):
    context = multiprocessing.get_context("fork")
    environment = runtime_configuration
    slots = ResourceSlots(tmp_path, _settings(), environment=environment)
    results = context.Queue()
    releases = [context.Event() for _ in range(4)]
    children = []
    try:
        with _hold_capacity(slots, available=1):
            with pytest.raises(RuntimeError, match="holder failed"):
                with slots.acquire("model"):
                    for label in range(4):
                        child = context.Process(target=_waiter, args=(
                            tmp_path, environment, _settings(), label, results, releases[label]))
                        child.start()
                        children.append(child)
                        _wait_for_ticket_count(slots.root, label + 1)
                    raise RuntimeError("holder failed")
            for label in range(4):
                assert results.get(timeout=5) == (label, "acquired")
                releases[label].set()
    finally:
        _finish(children, releases)
    assert all(child.exitcode == 0 for child in children)
    _wait_for_ticket_count(slots.root, 0)


def test_queue_head_timeout_does_not_block_next_waiter(tmp_path, runtime_configuration):
    context = multiprocessing.get_context("fork")
    environment = runtime_configuration
    slots = ResourceSlots(tmp_path, _settings(), environment=environment)
    results = context.Queue()
    releases = [context.Event(), context.Event()]
    children = []
    try:
        with _hold_capacity(slots):
            for label, timeout in [(0, .3), (1, 5)]:
                child = context.Process(target=_waiter, args=(
                    tmp_path, environment, _settings(), label, results, releases[label], timeout))
                child.start()
                children.append(child)
                _wait_for_ticket_count(slots.root, label + 1)
            assert results.get(timeout=3) == (0, "timeout")
            _wait_for_ticket_count(slots.root, 1)
        assert results.get(timeout=3) == (1, "acquired")
    finally:
        _finish(children, releases)
    assert all(child.exitcode == 0 for child in children)
    _wait_for_ticket_count(slots.root, 0)


def test_cancelled_queue_head_does_not_block_next_waiter(tmp_path, runtime_configuration):
    context = multiprocessing.get_context("fork")
    environment = runtime_configuration
    settings = _settings()
    slots = ResourceSlots(tmp_path, settings, environment=environment)
    results = context.Queue()
    stop = context.Event()
    release = context.Event()
    cancelled = context.Process(target=_cancelled_waiter, args=(
        tmp_path, environment, settings, results, stop))
    next_waiter = context.Process(target=_waiter, args=(
        tmp_path, environment, settings, "next", results, release))
    try:
        with _hold_capacity(slots):
            cancelled.start()
            _wait_for_ticket_count(slots.root, 1)
            next_waiter.start()
            _wait_for_ticket_count(slots.root, 2)
            stop.set()
            assert results.get(timeout=3) == ("cancelled", "interrupted")
            _wait_for_ticket_count(slots.root, 1)
        assert results.get(timeout=3) == ("next", "acquired")
    finally:
        release.set()
        _finish([cancelled, next_waiter], [release])
    assert cancelled.exitcode == next_waiter.exitcode == 0
    _wait_for_ticket_count(slots.root, 0)


def test_killed_local_queue_head_is_pruned_by_process_identity_without_ttl(tmp_path, runtime_configuration):
    context = multiprocessing.get_context("fork")
    environment = runtime_configuration
    slots = ResourceSlots(tmp_path, _settings(), environment=environment)
    results = context.Queue()
    releases = [context.Event() for _ in range(3)]
    children = []
    try:
        with _hold_capacity(slots, available=1):
            with slots.acquire("model"):
                for label in range(3):
                    child = context.Process(target=_waiter, args=(
                        tmp_path, environment, _settings(), label, results, releases[label]))
                    child.start()
                    children.append(child)
                    _wait_for_ticket_count(slots.root, label + 1)
                children[0].terminate()
                children[0].join(2)
                _wait_for_ticket_count(slots.root, 2)
            for label in [1, 2]:
                assert results.get(timeout=3) == (label, "acquired")
                releases[label].set()
    finally:
        _finish(children, releases)
    assert all(child.exitcode == 0 for child in children[1:])
    _wait_for_ticket_count(slots.root, 0)


def test_ticket_scan_stops_after_first_live_prefix(tmp_path, runtime_configuration, monkeypatch):
    slots = ResourceSlots(tmp_path, _settings(), environment=runtime_configuration)
    queue = slots.root / "prefix.queue"
    queue.mkdir()
    record = {"host": {**slots.host, "hostname": "foreign"}, "pid": 99999999,
              "start": "remote-start", "token": "remote-token"}
    tickets = [queue / f"{number:020d}.ticket" for number in range(1, 4)]
    for ticket in tickets:
        ticket.write_text(json.dumps(record), encoding="utf-8")
    checked = []
    live_claim = slots._live_claim

    def tracked_live_claim(path):
        checked.append(path)
        return live_claim(path)

    monkeypatch.setattr(slots, "_live_claim", tracked_live_claim)
    assert slots._ticket_order(queue) == tickets
    assert checked == tickets[:1]


def test_ticket_queue_fills_two_available_slots_without_exceeding_global_capacity(tmp_path, runtime_configuration):
    context = multiprocessing.get_context("fork")
    environment = runtime_configuration
    settings = _settings()
    slots = ResourceSlots(tmp_path, settings, environment=environment)
    results = context.Queue()
    releases = [context.Event(), context.Event()]
    children = []
    try:
        with _hold_capacity(slots, available=2), slots.acquire("model"):
            for label in range(2):
                child = context.Process(target=_waiter, args=(
                    tmp_path, environment, settings, label, results, releases[label]))
                child.start()
                children.append(child)
                if label == 0:
                    assert results.get(timeout=3) == (0, "acquired")
            _wait_for_ticket_count(slots.root, 1)
            assert not results._reader.poll(.1)
            releases[0].set()
            assert results.get(timeout=3) == (1, "acquired")
    finally:
        _finish(children, releases)
    assert all(child.exitcode == 0 for child in children)


def test_foreign_host_capacity_does_not_depend_on_shared_flock(tmp_path, runtime_configuration, monkeypatch):
    a = ResourceSlots(tmp_path / "a", _settings(), environment=runtime_configuration)
    b = ResourceSlots(tmp_path / "b", _settings(), environment=runtime_configuration)
    b.host = {**b.host, "hostname": "another-host"}
    monkeypatch.setattr(fcntl, "flock", lambda *_: None)
    with _hold_capacity(a):
        with pytest.raises(ResourceSlotTimeout):
            with b.acquire("model", deadline_monotonic=time.monotonic() + .2):
                pytest.fail("foreign host exceeded global capacity")
        assert len(list(a.root.glob("model-*.lease"))) == a.limits["model"]
    with b.acquire("model", deadline_monotonic=time.monotonic() + 1):
        pass


def test_foreign_holder_is_not_deleted_based_on_local_pid_or_age(tmp_path, runtime_configuration):
    slots = ResourceSlots(tmp_path, _settings(), environment=runtime_configuration)
    lease = slots.root / "foreign.lease"
    record = {"host": {**slots.host, "hostname": "foreign"}, "pid": 99999999,
              "start": "old-boot-start", "token": "foreign-identity"}
    lease.write_text(json.dumps(record))
    with slots._local_guard():
        assert slots._live_claim(lease)
    assert json.loads(lease.read_text()) == record


def test_actual_queue_deadline_never_emits_provider_attempt(tmp_path):
    from runtime.config import ProviderSettings
    from runtime.session import ModelRequest, ModelSession, ProviderChannel, replay_provider_events
    from types import SimpleNamespace

    def unexpected_request(*args, **kwargs):
        pytest.fail("queued request must not contact a Provider")

    slots = ResourceSlots(tmp_path, _settings(), environment={})
    events = []
    session = ModelSession(settings=ProviderSettings(api_format="anthropic", model="synthetic", base_url="https://provider.test"),
        channels=(ProviderChannel("synthetic", "a"),), transport=SimpleNamespace(complete=unexpected_request),
        event_sink=events.append,
        slot_factory=lambda: slots.acquire("model", deadline_monotonic=time.monotonic() + .1))
    with _hold_capacity(slots):
        result = session.call(ModelRequest(model="synthetic", system="synthetic", messages=(), max_tokens=32))
    assert result.failure.kind == "resource_queue_timeout" and result.attempts == 0
    assert [event["event_type"] for event in events] == ["resource_wait_started", "resource_wait_failed"]
    assert replay_provider_events(events).attempts == session.usage.summary()["attempts"] == 0
    _wait_for_ticket_count(slots.root, 0)


@pytest.fixture
def candidate_pool(tmp_path, runtime_configuration):
    import hashlib

    config_path = Path(runtime_configuration["MURAL_RUNTIME_CONFIG"])
    config = json.loads(config_path.read_text())
    config["resources"]["provider"] = 128
    config_path.write_text(json.dumps(config))
    slots = ResourceSlots(tmp_path, _settings(), environment=runtime_configuration)
    assert slots.limits["model"] == 128
    pool = hashlib.sha256(b"model").hexdigest()[:16]
    paths = [slots.root / f"model-{pool}-{index:03d}.lease" for index in range(128)]
    foreign = {"host": {**slots.host, "hostname": "foreign-candidate-holder"},
               "pid": 99999999, "start": "remote-start", "token": "remote-token"}
    return slots, pool, paths, foreign


def _write_candidate_claims(paths, record):
    for path in paths:
        path.write_text(json.dumps(record), encoding="utf-8")


def _assert_no_candidate_waiter_claims(slots):
    assert not list(slots.root.glob(".claim-*"))
    assert not list(slots.root.glob("*.queue/*.ticket"))
    assert not list(slots.root.glob("*.queue/guard.lease"))


def test_slot_candidates_are_bounded_vacant_first_without_claim_reads(candidate_pool, monkeypatch):
    slots, pool, paths, foreign = candidate_pool
    _write_candidate_claims(paths[:70], foreign)
    unrelated = slots.root / "render-other-000.lease"
    outside = slots.root / f"model-{pool}-128.lease"
    _write_candidate_claims([unrelated, outside], foreign)
    before = {p: p.read_bytes() for p in paths[:70] + [unrelated, outside]}
    monkeypatch.setattr(slots, "_live_claim", lambda *_: pytest.fail("ordering must not read claims"))
    candidates = slots._slot_candidates("model", pool, 128)
    assert candidates == paths[70:] + paths[:70]
    assert len(candidates) == len(set(candidates)) == 128
    assert {p: p.read_bytes() for p in before} == before


def test_slot_candidates_reduce_reads_and_run_only_at_guarded_head(candidate_pool, monkeypatch):
    slots, pool, paths, foreign = candidate_pool
    _write_candidate_claims(paths[:70], foreign)
    checked = []
    live_claim = slots._live_claim
    candidates = slots._slot_candidates
    calls = []

    def tracked_live_claim(path):
        checked.append(path)
        return live_claim(path)

    def guarded_candidates(name, identity, limit):
        queue = slots.root / f"{name}-{identity}.queue"
        guard = json.loads((queue / "guard.lease").read_text())
        waiting = sorted(queue.glob("*.ticket"))
        if waiting:
            assert json.loads(waiting[0].read_text())["token"] == guard["token"]
        calls.append((name, identity, limit))
        return candidates(name, identity, limit)

    monkeypatch.setattr(slots, "_live_claim", tracked_live_claim)
    monkeypatch.setattr(slots, "_slot_candidates", guarded_candidates)
    with slots.acquire("model", deadline_monotonic=time.monotonic() + 2):
        assert len(list(slots.root.glob("model-*.lease"))) == 71
    assert calls == [("model", pool, 128)]
    assert [p for p in checked if p in paths] == [paths[70]]
    assert len(checked) <= 8
    assert all(json.loads(p.read_text()) == foreign for p in paths[:70])
    _assert_no_candidate_waiter_claims(slots)


def test_vacant_uncontended_acquire_uses_one_guard_without_a_ticket(candidate_pool, monkeypatch):
    from contextlib import contextmanager

    slots, _, _, _ = candidate_pool
    guard_calls = []
    original = slots._queue_guard

    @contextmanager
    def tracked_guard(*args, **kwargs):
        with original(*args, **kwargs):
            guard_calls.append(args[0])
            yield

    monkeypatch.setattr(slots, "_queue_guard", tracked_guard)
    with slots.acquire("model", deadline_monotonic=time.monotonic() + 1):
        assert len(guard_calls) == 1
        assert not list(slots.root.glob("*.queue/*.ticket"))
        assert len(list(slots.root.glob("model-*.lease"))) == 1
    _assert_no_candidate_waiter_claims(slots)


def test_vacant_fast_path_cannot_overtake_existing_foreign_ticket(candidate_pool):
    slots, pool, _, foreign = candidate_pool
    queue = slots.root / f"model-{pool}.queue"
    queue.mkdir()
    head = queue / "00000000000000000001.ticket"
    head.write_text(json.dumps(foreign), encoding="utf-8")
    with pytest.raises(ResourceSlotTimeout):
        with slots.acquire("model", deadline_monotonic=time.monotonic() + .05):
            pytest.fail("free capacity must not bypass an existing FIFO waiter")
    assert json.loads(head.read_text()) == foreign
    assert not list(slots.root.glob("model-*.lease"))
    assert list(queue.glob("*.ticket")) == [head]
    assert not list(slots.root.glob(".claim-*"))


@pytest.mark.parametrize("vacancy_race", [False, True])
def test_slot_candidates_fall_back_to_dead_local_claim_without_remote_reclaim(
        candidate_pool, monkeypatch, vacancy_race):
    import os

    slots, pool, paths, foreign = candidate_pool
    _write_candidate_claims(paths[1:] if vacancy_race else paths, foreign)
    dead = {"host": slots.host, "pid": os.getpid(), "start": "previous-process-start",
            "token": "dead-local-token"}
    paths[-1].write_text(json.dumps(dead))
    for path in paths[1:-1]:
        os.utime(path, (1, 1))
    attempted = []
    link_claim = slots._link_claim
    winner = slots.root / "foreign-race-winner.json"
    winner.write_text(json.dumps(foreign))

    def racing_link_claim(path, claim):
        if path in paths:
            attempted.append(path)
            if vacancy_race and path == paths[0] and not path.exists():
                os.link(winner, path)
        return link_claim(path, claim)

    monkeypatch.setattr(slots, "_link_claim", racing_link_claim)
    with slots.acquire("model", deadline_monotonic=time.monotonic() + 2):
        assert len(list(slots.root.glob("model-*.lease"))) == 128
        assert json.loads(paths[-1].read_text())["token"] != dead["token"]
    assert attempted[0] == paths[0] and attempted[-1] == paths[-1]
    assert not paths[-1].exists()
    assert all(json.loads(p.read_text()) == foreign for p in paths[:-1])
    _assert_no_candidate_waiter_claims(slots)


class _LinearCandidateSlots(ResourceSlots):
    # Same ticket/link protocol, but the pre-optimization numeric scan order.
    def _slot_candidates(self, name, pool, limit):
        return [self.root / f"{name}-{pool}-{index:03d}.lease" for index in range(limit)]


def test_slot_candidates_old_and_new_readers_share_all_128_slots(candidate_pool, runtime_configuration, tmp_path):
    slots, _, paths, _ = candidate_pool
    old = _LinearCandidateSlots(tmp_path / "old", _settings(), environment=runtime_configuration)
    with ExitStack() as holders:
        for reader in [old] * 64 + [slots] * 64:
            holders.enter_context(reader.acquire("model", deadline_monotonic=time.monotonic() + 5))
        assert all(path.exists() for path in paths)
        assert set(slots.root.glob("model-*.lease")) == set(paths)
        for reader in (old, slots):
            with pytest.raises(ResourceSlotTimeout):
                with reader.acquire("model", deadline_monotonic=time.monotonic() + .05):
                    pytest.fail("mixed readers exceeded the shared 128-slot budget")
        assert not list(slots.root.glob("*.queue/*.ticket"))
    assert not list(slots.root.glob("model-*.lease"))
    _assert_no_candidate_waiter_claims(slots)


def _candidate_order_waiter(root, environment, legacy, label, results, release, stop):
    cls = _LinearCandidateSlots if legacy else ResourceSlots
    try:
        with cls(root, _settings(environment), environment=environment).acquire(
                "model", deadline_monotonic=time.monotonic() + 5, stop_event=stop):
            results.put((label, "acquired"))
            assert release.wait(5)
    except InterruptedError:
        results.put((label, "interrupted"))


@pytest.mark.parametrize("legacy_first,cancel_head", [(True, False), (False, True)])
def test_slot_candidates_mixed_readers_keep_fifo_and_cancel_cleanup(
        candidate_pool, runtime_configuration, tmp_path, legacy_first, cancel_head):
    slots, _, paths, foreign = candidate_pool
    _write_candidate_claims(paths, foreign)
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    releases = [context.Event(), context.Event()]
    stops = [context.Event(), context.Event()]
    children = []
    try:
        for label, legacy in enumerate([legacy_first, not legacy_first]):
            child = context.Process(target=_candidate_order_waiter, args=(
                tmp_path, runtime_configuration, legacy, label, results, releases[label], stops[label]))
            child.start()
            children.append(child)
            _wait_for_ticket_count(slots.root, label + 1)
        if cancel_head:
            stops[0].set()
            assert results.get(timeout=3) == (0, "interrupted")
            _wait_for_ticket_count(slots.root, 1)
        paths[-1].unlink()
        first = 1 if cancel_head else 0
        assert results.get(timeout=3) == (first, "acquired")
        assert len(list(slots.root.glob("model-*.lease"))) == 128
        if not cancel_head:
            assert not results._reader.poll(.05)
            releases[0].set()
            assert results.get(timeout=3) == (1, "acquired")
    finally:
        _finish(children, releases)
    assert all(child.exitcode == 0 for child in children)
    assert all(json.loads(p.read_text()) == foreign for p in paths[:-1])
    assert not paths[-1].exists()
    _assert_no_candidate_waiter_claims(slots)


def _publish_hot_limits(path, config):
    temporary = path.with_suffix('.pending')
    temporary.write_text(json.dumps(config), encoding='utf-8')
    temporary.replace(path)


@pytest.fixture
def hot_limits_pool(tmp_path, runtime_configuration):
    baseline = Path(runtime_configuration['MURAL_RUNTIME_CONFIG'])
    config = json.loads(baseline.read_text())
    config['decks'].update(total=1, per_model=1, owners=1)
    for key in ('provider', 'render', 'web_search', 'image_generation'):
        config['resources'][key] = 1
    _publish_hot_limits(baseline, config)
    hot_path = tmp_path / 'operator-limits.json'
    _publish_hot_limits(hot_path, config)
    environment = {**runtime_configuration, 'MURAL_RESOURCE_LIMITS_FILE': str(hot_path)}
    settings = _settings(environment)
    slots = ResourceSlots(tmp_path / 'hot-workspace', settings, environment=environment)
    return slots, settings, hot_path, config, baseline, environment


def _set_hot_capacity(config, resource, capacity):
    if resource == 'deck':
        config['decks']['total'] = capacity
    else:
        config['resources']['provider' if resource == 'model' else resource] = capacity


@pytest.mark.parametrize('resource', ['model', 'render', 'web_search', 'image_generation', 'deck'])
def test_hot_limits_same_object_expands_without_changing_baseline(hot_limits_pool, resource):
    slots, settings, hot_path, config, baseline, _ = hot_limits_pool
    baseline_bytes = baseline.read_bytes()
    settings_before = (settings.model_concurrency, settings.render_concurrency,
                       settings.web_search_concurrency, settings.image_generation_concurrency)
    with slots.acquire(resource, deadline_monotonic=time.monotonic() + 1):
        with pytest.raises(ResourceSlotTimeout):
            with slots.acquire(resource, deadline_monotonic=time.monotonic() + .1):
                pytest.fail('initial capacity one admitted a second holder')
        _set_hot_capacity(config, resource, 2)
        _publish_hot_limits(hot_path, config)
        with slots.acquire(resource, deadline_monotonic=time.monotonic() + 1):
            assert slots.limits[resource] == 2
            assert len(list(slots.root.glob(resource + '-*.lease'))) == 2
    assert baseline.read_bytes() == baseline_bytes
    assert settings_before == (settings.model_concurrency, settings.render_concurrency,
                               settings.web_search_concurrency, settings.image_generation_concurrency)
    _assert_no_candidate_waiter_claims(slots)


def test_hot_limits_already_queued_waiter_observes_expansion(hot_limits_pool):
    slots, _, hot_path, config, _, _ = hot_limits_pool
    acquired, release, stop = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def wait():
        try:
            with slots.acquire('model', deadline_monotonic=time.monotonic() + 3, stop_event=stop):
                acquired.set()
                if not release.wait(3):
                    raise AssertionError('test waiter release timed out')
        except BaseException as exc:
            errors.append(exc)

    waiter = threading.Thread(target=wait, daemon=True)
    with slots.acquire('model', deadline_monotonic=time.monotonic() + 1):
        try:
            waiter.start()
            _wait_for_ticket_count(slots.root, 1)
            assert not acquired.is_set()
            _set_hot_capacity(config, 'model', 2)
            changed = time.monotonic()
            _publish_hot_limits(hot_path, config)
            assert acquired.wait(1.5), 'queued head did not reread expanded limits'
            assert time.monotonic() - changed < 1.5
            assert len(list(slots.root.glob('model-*.lease'))) == 2
        finally:
            release.set()
            stop.set()
            waiter.join(2)
    assert not waiter.is_alive()
    assert not errors
    _assert_no_candidate_waiter_claims(slots)


@pytest.mark.parametrize('resource', ['model', 'render', 'web_search', 'image_generation', 'deck'])
def test_hot_limits_shrink_counts_live_high_index_before_new_admission(hot_limits_pool, resource):
    slots, _, hot_path, config, _, _ = hot_limits_pool
    _set_hot_capacity(config, resource, 2)
    _publish_hot_limits(hot_path, config)
    with ExitStack() as low, ExitStack() as high:
        low.enter_context(slots.acquire(resource, deadline_monotonic=time.monotonic() + 1))
        high.enter_context(slots.acquire(resource, deadline_monotonic=time.monotonic() + 1))
        leases = sorted(slots.root.glob(resource + '-*.lease'))
        assert [int(p.stem.rsplit('-', 1)[1]) for p in leases] == [0, 1]
        high_bytes = leases[1].read_bytes()
        _set_hot_capacity(config, resource, 1)
        _publish_hot_limits(hot_path, config)
        low.close()
        assert not leases[0].exists() and leases[1].exists()
        with pytest.raises(ResourceSlotTimeout):
            with slots.acquire(resource, deadline_monotonic=time.monotonic() + .15):
                pytest.fail('vacant low index bypassed live high-index occupancy after shrink')
        assert slots.limits[resource] == 1
        assert leases[1].read_bytes() == high_bytes
        assert not leases[0].exists()
    with slots.acquire(resource, deadline_monotonic=time.monotonic() + 1):
        assert len(list(slots.root.glob(resource + '-*.lease'))) == 1
    _assert_no_candidate_waiter_claims(slots)


def test_hot_limits_shrink_preserves_unknown_foreign_high_index(hot_limits_pool):
    import hashlib

    slots, _, hot_path, config, _, _ = hot_limits_pool
    pool = hashlib.sha256(b'model').hexdigest()[:16]
    high = slots.root / f'model-{pool}-001.lease'
    foreign = {'host': {**slots.host, 'hostname': 'foreign-hot-holder'},
               'pid': 99999999, 'start': 'unknown-remote-start', 'token': 'foreign-token'}
    high.write_text(json.dumps(foreign), encoding='utf-8')
    with pytest.raises(ResourceSlotTimeout):
        with slots.acquire('model', deadline_monotonic=time.monotonic() + .15):
            pytest.fail('unknown foreign high-index occupancy was ignored')
    assert json.loads(high.read_text()) == foreign
    _assert_no_candidate_waiter_claims(slots)


def test_hot_limits_directory_change_is_rejected_without_partial_effect(hot_limits_pool, tmp_path):
    slots, _, hot_path, config, baseline, _ = hot_limits_pool
    previous, root, baseline_bytes = dict(slots.limits), slots.root, baseline.read_bytes()
    with slots.acquire('model', deadline_monotonic=time.monotonic() + 1):
        lease = next(slots.root.glob('model-*.lease'))
        lease_bytes = lease.read_bytes()
        config['resources'].update(directory=str(tmp_path / 'forbidden-new-pool'), provider=256, render=256)
        _publish_hot_limits(hot_path, config)
        with pytest.raises(ProductionError):
            slots._refresh_limits()
        with pytest.raises(ProductionError):
            with slots.acquire('model', deadline_monotonic=time.monotonic() + .2):
                pytest.fail('invalid root switch admitted a claim')
        assert slots.root == root and slots.limits == previous
        assert lease.read_bytes() == lease_bytes
        assert not (tmp_path / 'forbidden-new-pool').exists()
    assert baseline.read_bytes() == baseline_bytes
    _assert_no_candidate_waiter_claims(slots)


@pytest.mark.parametrize('invalid', ['bad_json', 'incomplete', 'zero', 'missing_file'])
def test_hot_limits_invalid_update_fails_closed_without_partial_effect(hot_limits_pool, invalid):
    slots, _, hot_path, config, _, _ = hot_limits_pool
    previous = dict(slots.limits)
    config['resources']['provider'] = 256
    if invalid == 'bad_json':
        hot_path.write_text('{bad-json', encoding='utf-8')
    elif invalid == 'incomplete':
        del config['resources']['render']
        _publish_hot_limits(hot_path, config)
    elif invalid == 'zero':
        config['resources']['render'] = 0
        _publish_hot_limits(hot_path, config)
    else:
        hot_path.unlink()
    with pytest.raises(ProductionError):
        slots._refresh_limits()
    with pytest.raises(ProductionError):
        with slots.acquire('model', deadline_monotonic=time.monotonic() + .2):
            pytest.fail('invalid hot configuration admitted new work')
    assert slots.limits == previous
    assert not list(slots.root.glob('model-*.lease'))
    _assert_no_candidate_waiter_claims(slots)
    config['resources']['provider'] = config['resources']['render'] = 1
    _publish_hot_limits(hot_path, config)
    with slots.acquire('model', deadline_monotonic=time.monotonic() + 1):
        assert slots.limits['model'] == 1


@pytest.mark.parametrize('bad_hot_file', [False, True])
def test_hot_limits_without_optin_keeps_original_settings(hot_limits_pool, tmp_path, bad_hot_file):
    _, settings, hot_path, config, _, environment = hot_limits_pool
    environment = {k: v for k, v in environment.items() if k != 'MURAL_RESOURCE_LIMITS_FILE'}
    slots = ResourceSlots(tmp_path / 'fixed-workspace', settings, environment=environment)
    previous = dict(slots.limits)
    config['resources']['provider'] = 256
    if bad_hot_file:
        hot_path.write_text('{bad-json', encoding='utf-8')
    else:
        _publish_hot_limits(hot_path, config)
    slots._refresh_limits()
    with slots.acquire('model', deadline_monotonic=time.monotonic() + 1):
        with pytest.raises(ResourceSlotTimeout):
            with slots.acquire('model', deadline_monotonic=time.monotonic() + .1):
                pytest.fail('non-opted-in object changed its fixed capacity')
    assert slots.limits == previous
    assert settings.model_concurrency == 1
    _assert_no_candidate_waiter_claims(slots)
