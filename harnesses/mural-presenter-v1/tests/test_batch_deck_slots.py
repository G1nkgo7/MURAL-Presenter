"""Shared query admission for overlapping K3 recovery and fresh batches."""
import json
import time

import pytest

from runtime.production import ProductionSettings, ResourceSlots
from runtime.config import load_runtime_config
from runtime.recovery import ResourceSlotTimeout


def test_deck_limit_is_shared_between_independent_batch_workers(tmp_path):
    config = tmp_path / 'runtime.json'
    value = load_runtime_config({})
    value['decks'].update(total=1, per_model=1, owners=2)
    value['resources']['directory'] = str(tmp_path / 'shared')
    config.write_text(json.dumps(value))
    env = {'MURAL_RUNTIME_CONFIG': str(config)}
    settings = ProductionSettings(skill_source=tmp_path / 'skill', model_concurrency=128)
    first = ResourceSlots(tmp_path / 'recovery', settings, environment=env)
    second = ResourceSlots(tmp_path / 'fresh', settings, environment=env)
    assert first.limits['deck'] == second.limits['deck'] == 1
    with first.acquire('deck', deadline_monotonic=time.monotonic() + 2):
        with pytest.raises(ResourceSlotTimeout):
            with second.acquire('deck', deadline_monotonic=time.monotonic() + .1):
                pytest.fail('Two batches exceeded the one-deck shared limit')
    with second.acquire('deck', deadline_monotonic=time.monotonic() + 2):
        assert len(list((tmp_path / 'shared').glob('deck-*.lease'))) == 1
    assert not list((tmp_path / 'shared').glob('deck-*.lease'))


def test_deck_limit_does_not_replace_provider_concurrency(tmp_path):
    config = tmp_path / 'runtime.json'
    value = load_runtime_config({})
    value['decks'].update(total=64, per_model=64, owners=32)
    value['resources']['directory'] = str(tmp_path / 'shared')
    config.write_text(json.dumps(value))
    slots = ResourceSlots(tmp_path / 'worker', ProductionSettings(skill_source=tmp_path / 'skill', model_concurrency=128),
                          environment={'MURAL_RUNTIME_CONFIG': str(config)})
    assert slots.limits['deck'] == 64
    assert slots.limits['model'] == 128
