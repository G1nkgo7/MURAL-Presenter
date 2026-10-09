from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading

import pytest

from owner.registry import OwnerRecord, OwnerRegistry, OwnerRegistryError, OwnerStatus
from state.locking import workspace_lock


def test_owner_identity_comes_from_accepted_group_and_membership(tmp_path: Path) -> None:
    registry = OwnerRegistry(tmp_path, deck_id="deck-001")

    first = registry.accept(role="slide", pages=[3, 2], group_id="evidence")
    duplicate = registry.accept(role="slide", pages=[2, 3], group_id="evidence")

    assert duplicate == first
    assert first.pages == (2, 3)
    assert set(first.to_dict()) == {
        "owner_id",
        "role",
        "pages",
        "group_id",
        "attempt",
        "status",
        "dependencies",
        "output_path",
        "handoff_from",
    }

    registry.transition(first.owner_id, attempt=1, status=OwnerStatus.RUNNING)
    registry.transition(first.owner_id, attempt=1, status=OwnerStatus.COMPLETED)
    second_attempt = registry.accept(
        role="slide", pages=[2, 3], group_id="evidence", dependencies=["task-a"]
    )

    assert second_attempt.owner_id == first.owner_id
    assert second_attempt.attempt == 2
    assert second_attempt.dependencies == ("task-a",)


@pytest.mark.parametrize("pages,group_id", [
    ([1], "opening"), ([1], "repair"), ([1, 3], "opening"),
    ([1, 2], "renamed"), ([5], "opening"),
])
def test_ordinary_delegation_cannot_split_or_replace_slide_owner(tmp_path, pages, group_id):
    registry = OwnerRegistry(tmp_path, deck_id="deck-002")
    first = registry.accept(role="slide", pages=[1, 2], group_id="opening")
    before = registry.path.read_bytes()
    with pytest.raises(OwnerRegistryError, match="page_owner_conflict") as caught:
        registry.accept(role="slide", pages=pages, group_id=group_id)
    assert first.owner_id in str(caught.value)
    assert "opening" in str(caught.value) and "[1, 2]" in str(caught.value)
    assert registry.path.read_bytes() == before
    assert not registry.list_archived()


@pytest.mark.parametrize("status", [OwnerStatus.COMPLETED, OwnerStatus.FAILED, OwnerStatus.CANCELLED])
def test_terminal_owner_still_owns_pages_and_batch_rejection_is_atomic(tmp_path, status):
    registry = OwnerRegistry(tmp_path, deck_id="terminal-owner")
    original = registry.accept(role="slide", pages=[3, 4], group_id="contrast")
    registry.transition(original.owner_id, attempt=1, status=OwnerStatus.RUNNING)
    registry.transition(original.owner_id, attempt=1, status=status)
    before = registry.path.read_bytes()
    with pytest.raises(OwnerRegistryError, match="page_owner_conflict"):
        registry.accept_many([
            {"role": "slide", "pages": [5], "group_id": "new-page"},
            {"role": "slide", "pages": [3], "group_id": "repair"},
        ])
    assert registry.path.read_bytes() == before
    resumed = registry.accept(role="slide", pages=[3, 4], group_id="contrast")
    assert resumed.owner_id == original.owner_id and resumed.attempt == 2


def test_stale_attempt_cannot_overwrite_current_owner(tmp_path: Path) -> None:
    registry = OwnerRegistry(tmp_path, deck_id="deck-003")
    first = registry.accept(role="research", group_id="facts")
    registry.transition(first.owner_id, attempt=1, status=OwnerStatus.RUNNING)
    registry.transition(first.owner_id, attempt=1, status=OwnerStatus.COMPLETED)
    current = registry.accept(role="research", group_id="facts")

    with pytest.raises(OwnerRegistryError, match="stale owner attempt"):
        registry.transition(first.owner_id, attempt=1, status=OwnerStatus.FAILED)

    assert current.attempt == 2
    assert registry.get(first.owner_id).status == OwnerStatus.PENDING


def test_parallel_accept_is_atomic_and_reuses_one_active_owner(tmp_path: Path) -> None:
    def accept_once() -> tuple[str, int]:
        registry = OwnerRegistry(tmp_path, deck_id="deck-004")
        record = registry.accept(role="image", pages=[4], group_id="hero")
        return record.owner_id, record.attempt

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _index: accept_once(), range(12)))

    assert len(set(results)) == 1


def test_wave_accept_is_one_transaction_and_rejects_duplicate_groups(
    tmp_path: Path,
) -> None:
    registry = OwnerRegistry(tmp_path, deck_id="deck-wave")

    with pytest.raises(OwnerRegistryError, match="duplicate group_id"):
        registry.accept_many(
            [
                {"role": "research", "group_id": "shared"},
                {"role": "slide", "pages": [1], "group_id": "shared"},
            ],
            reject_unfinished=True,
        )

    assert registry.list_current() == ()
    accepted = registry.accept_many(
        [
            {"role": "research", "group_id": "sources"},
            {"role": "slide", "pages": [1], "group_id": "page-01"},
        ],
        reject_unfinished=True,
    )
    assert [record.group_id for record in accepted] == ["sources", "page-01"]


def test_orchestrator_claim_is_persistent_reentrant_and_expands_one_real_owner(tmp_path):
    registry = OwnerRegistry(tmp_path, deck_id="direct-author")
    assert registry.page_owner(1) is None
    with workspace_lock(tmp_path):
        first = registry.claim_orchestrator_page(3)
    assert first.owner_id == first.group_id == first.role == "orchestrator"
    assert first.attempt == 1 and first.status == OwnerStatus.RUNNING and not first.dependencies
    before = registry.path.read_bytes()
    assert registry.claim_orchestrator_page(3) == first
    assert registry.path.read_bytes() == before
    registry.transition("orchestrator", attempt=1, status=OwnerStatus.INTERRUPTED)
    restored = OwnerRegistry(tmp_path, deck_id="direct-author")
    expanded = restored.claim_orchestrator_page(1)
    assert expanded.pages == (1, 3) and expanded.owner_id == first.owner_id
    assert expanded.attempt == 1 and expanded.status == OwnerStatus.INTERRUPTED
    assert restored.page_owner(1) == restored.page_owner(3) == expanded
    assert restored.page_owner(2) is None and restored.list_current() == (expanded,)
    assert not restored.list_archived() and not (tmp_path / "_state/orchestration.json").exists()


@pytest.mark.parametrize("status", [OwnerStatus.COMPLETED, OwnerStatus.FAILED, OwnerStatus.CANCELLED])
def test_terminal_main_or_child_owner_cannot_be_taken_over(tmp_path, status):
    registry = OwnerRegistry(tmp_path, deck_id="direct-terminal")
    main = registry.claim_orchestrator_page(1)
    child = registry.accept(role="slide", pages=[2, 3], group_id="details")
    registry.transition(child.owner_id, attempt=1, status=OwnerStatus.RUNNING)
    for owner in (main, child):
        registry.transition(owner.owner_id, attempt=1, status=status)
    before = registry.path.read_bytes()
    with pytest.raises(OwnerRegistryError, match="page_owner_conflict.*orchestrator"):
        registry.accept(role="slide", pages=[1], group_id="take-main-page")
    with pytest.raises(OwnerRegistryError, match="page_owner_conflict.*details"):
        registry.claim_orchestrator_page(2)
    assert registry.path.read_bytes() == before


def test_material_consumer_pages_do_not_own_page_sources(tmp_path):
    registry = OwnerRegistry(tmp_path, deck_id="consumers")
    registry.accept(role="image", pages=[1, 2], group_id="visuals")
    registry.accept(role="research", pages=[1], group_id="facts")
    assert registry.page_owner(1) is None
    main = registry.claim_orchestrator_page(1)
    child = registry.accept(role="slide", pages=[2], group_id="details")
    assert registry.page_owner(1) == main and registry.page_owner(2) == child


@pytest.mark.parametrize("role", ["research", "image", "slide", "orchestrator"])
def test_delegation_cannot_create_or_replace_main_identity(tmp_path, role):
    registry = OwnerRegistry(tmp_path, deck_id="reserved-identity")
    before = registry.snapshot()
    with pytest.raises(OwnerRegistryError, match="reserved|unsupported owner role"):
        registry.accept(role=role, pages=[1], group_id="orchestrator")
    assert registry.snapshot() == before and not registry.path.exists()


@pytest.mark.parametrize("role,owner_id,group_id", [
    ("orchestrator", "other", "orchestrator"),
    ("orchestrator", "orchestrator", "other"),
    ("slide", "orchestrator", "other"),
    ("research", "other", "orchestrator"),
])
def test_main_owner_record_requires_exact_identity(role, owner_id, group_id):
    with pytest.raises(ValueError, match="orchestrator"):
        OwnerRecord(owner_id, role, (1,), group_id, 1, OwnerStatus.RUNNING)


@pytest.mark.parametrize("method", ["page_owner", "claim_orchestrator_page"])
@pytest.mark.parametrize("page", [0, -1, True, "1"])
def test_page_ownership_methods_require_real_positive_page_number(tmp_path, method, page):
    registry = OwnerRegistry(tmp_path, deck_id="typed-page")
    with pytest.raises(OwnerRegistryError, match="positive integer"):
        getattr(registry, method)(page)


@pytest.mark.parametrize("attempt", range(3))
def test_main_claim_and_child_accept_race_has_exactly_one_winner(tmp_path, attempt):
    registry = OwnerRegistry(tmp_path, deck_id=f"race-{attempt}")
    other = OwnerRegistry(tmp_path, deck_id=f"race-{attempt}")
    barrier = threading.Barrier(2)

    def compete(main):
        barrier.wait(timeout=3)
        try:
            return registry.claim_orchestrator_page(1) if main else other.accept(
                role="slide", pages=[1], group_id="child")
        except OwnerRegistryError as exc:
            assert "page_owner_conflict" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, [True, False]))
    winners = [record for record in results if record is not None]
    assert len(winners) == 1 and registry.list_current() == tuple(winners)
    assert registry.page_owner(1) == winners[0]


def test_parallel_main_claims_merge_pages_without_changing_identity(tmp_path):
    def claim(page):
        return OwnerRegistry(tmp_path, deck_id="parallel-main").claim_orchestrator_page(page)

    with ThreadPoolExecutor(max_workers=4) as pool:
        records = list(pool.map(claim, [3, 1, 2, 4, 1, 3]))
    registry = OwnerRegistry(tmp_path, deck_id="parallel-main")
    assert {record.owner_id for record in records} == {"orchestrator"}
    assert registry.get("orchestrator").pages == (1, 2, 3, 4)
