import unittest
import asyncio
from unittest import mock

from studio.app import jobs


class CustomModelDispatchTests(unittest.TestCase):
    def test_custom_models_keep_separate_bounded_queue_keys(self):
        with mock.patch.object(jobs, "_load_deck", return_value={"model": "custom:7"}):
            self.assertEqual(jobs._model_key(17), "custom:7")

    def test_dispatch_does_not_require_builtin_model_queues(self):
        loop = mock.Mock()
        with (
            mock.patch.object(jobs, "_loop", loop),
            mock.patch.object(jobs, "_queues", {}),
        ):
            jobs.enqueue(17)

        loop.call_soon_threadsafe.assert_called_once_with(
            jobs._enqueue_bounded, 17
        )


class DeckCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rows = {}
        self.gates = {}
        self.alive = set()
        self.started = []
        self.adopted = []
        self.failures = []

        async def process(deck_id):
            self.started.append(deck_id)
            self.rows[deck_id]["status"] = "running"
            if deck_id in self.failures:
                raise RuntimeError("synthetic failure")
            await self.gates[deck_id].wait()
            self.rows[deck_id]["status"] = "completed"

        async def adopt(deck_id, _run_dir):
            self.adopted.append(deck_id)
            await self.gates[deck_id].wait()
            self.alive.discard(deck_id)
            self.rows[deck_id]["status"] = "completed"

        self.patches = [
            mock.patch.object(jobs, "_queues", {}),
            mock.patch.object(jobs, "_scheduled", set()),
            mock.patch.object(jobs, "_active", {}),
            mock.patch.object(jobs, "_running", {}),
            mock.patch.object(jobs, "_workers", []),
            mock.patch.object(jobs, "_user_canceled", set()),
            mock.patch.object(jobs, "_limits", {"total": 3, "per_model": 2, "owners": 12}),
            mock.patch.object(jobs, "_loop", asyncio.get_running_loop()),
            mock.patch.object(jobs, "_load_deck", side_effect=self.rows.get),
            mock.patch.object(jobs, "_engine_alive", side_effect=lambda deck_id: deck_id in self.alive),
            mock.patch.object(jobs, "_set_status", side_effect=self.set_status),
            mock.patch.object(jobs, "_fail_deck", side_effect=lambda deck_id, error: self.set_status(deck_id, "failed", error=error)),
            mock.patch.object(jobs, "_process", side_effect=process),
            mock.patch.object(jobs, "_adopt", side_effect=adopt),
        ]
        for patch in self.patches:
            patch.start()

    def set_status(self, deck_id, status, **fields):
        self.rows[deck_id].update(status=status, **fields)

    def add(self, deck_id, model="custom:1", status="queued"):
        self.rows[deck_id] = {"id": deck_id, "model": model, "status": status, "run_dir": "unused-test-path"}
        self.gates[deck_id] = asyncio.Event()

    async def settle(self):
        for _ in range(6):
            await asyncio.sleep(0)

    async def asyncTearDown(self):
        for row in self.rows.values():
            if row["status"] == "queued":
                row["status"] = "interrupted"
        for gate in self.gates.values():
            gate.set()
        await self.settle()
        await asyncio.gather(*jobs._workers)
        for patch in reversed(self.patches):
            patch.stop()

    async def test_total_12_and_model_8_apply_to_mixed_custom_models(self):
        jobs._limits = {"total": 12, "per_model": 8, "owners": 12}
        for deck_id in range(20):
            self.add(deck_id, "custom:1" if deck_id < 10 else "custom:2")
            jobs._enqueue_bounded(deck_id)
            jobs._enqueue_bounded(deck_id)
        await self.settle()
        self.assertEqual(len(self.started), 12)
        self.assertEqual(list(jobs._active.values()).count("custom:1"), 8)
        self.assertEqual(list(jobs._active.values()).count("custom:2"), 4)
        self.assertEqual(len(jobs._scheduled), 20)
        self.gates[0].set()
        await self.settle()
        self.assertEqual(len(jobs._active), 12)
        self.assertEqual(list(jobs._active.values()).count("custom:1"), 8)
        self.assertEqual(self.started.count(8), 1)

    async def test_adopt_counts_before_dispatch_and_repeated_start_does_not_duplicate(self):
        for deck_id in range(3):
            self.add(deck_id, "custom:1" if deck_id < 2 else "custom:2", status="running")
            self.alive.add(deck_id)
        self.add(3)
        self.add(4, "custom:2")
        connection = mock.Mock()
        def query(sql):
            if "status IN" in sql:
                return [dict(self.rows[deck_id]) for deck_id in (0, 1, 2)]
            if "status = 'queued'" in sql:
                return [{"id": 3}, {"id": 4}]
            return []
        connection.execute.side_effect = query
        with mock.patch.object(jobs, "connect", return_value=connection), \
                mock.patch.object(jobs.service_config, "load_runtime_config", return_value={"decks": jobs._limits}) as config:
            await jobs.start()
            await jobs.start()
            await self.settle()
        self.assertEqual(config.call_count, 2)
        self.assertEqual(self.adopted, [0, 1, 2])
        self.assertEqual(self.started, [])
        self.gates[0].set()
        await self.settle()
        self.assertEqual(self.started, [3])
        self.assertEqual(len(jobs._active), 3)

    async def test_failure_and_queued_cancel_release_without_second_launch(self):
        jobs._limits = {"total": 1, "per_model": 1, "owners": 12}
        for deck_id in range(3):
            self.add(deck_id)
        self.failures.append(0)
        for deck_id in range(3):
            jobs._enqueue_bounded(deck_id)
        self.assertTrue(await jobs.cancel(2))
        await self.settle()
        self.assertEqual(self.started, [0, 1])
        self.assertEqual(self.rows[0]["status"], "failed")
        self.assertEqual(self.rows[2]["status"], "interrupted")
        self.assertNotIn(2, jobs._scheduled)
        self.assertEqual(set(jobs._active), {1})

    async def test_running_cancel_keeps_slot_until_process_exits(self):
        jobs._limits = {"total": 1, "per_model": 1, "owners": 12}
        self.add(0)
        self.add(1)
        jobs._enqueue_bounded(0)
        jobs._enqueue_bounded(1)
        await self.settle()
        process = mock.Mock()
        jobs._running[0] = process
        self.assertTrue(await jobs.cancel(0))
        process.terminate.assert_called_once()
        self.assertEqual(set(jobs._active), {0})
        self.assertEqual(self.started, [0])
        self.gates[0].set()
        await self.settle()
        self.assertEqual(self.started, [0, 1])

    async def test_adopted_cancel_releases_after_watcher_and_deduplicates_enqueue(self):
        jobs._limits = {"total": 1, "per_model": 1, "owners": 12}
        self.add(0, status="running")
        self.alive.add(0)
        self.add(1)
        jobs._enqueue_bounded(0)
        jobs._enqueue_bounded(0)
        jobs._enqueue_bounded(1)
        await self.settle()
        with mock.patch.object(jobs, "_kill_engine_procs", return_value=True) as kill:
            self.assertTrue(await jobs.cancel(0))
            kill.assert_called_once_with(0)
        self.assertEqual(self.adopted, [0])
        self.assertEqual(self.started, [])
        self.gates[0].set()
        await self.settle()
        self.assertEqual(self.started, [1])

    async def test_restart_counts_interrupted_process_until_it_really_exits(self):
        jobs._limits = {"total": 1, "per_model": 1, "owners": 12}
        self.add(0, status="interrupted")
        self.alive.add(0)
        self.add(1)
        self.add(2, status="interrupted")
        connection = mock.Mock()
        connection.execute.side_effect = lambda sql: ([dict(self.rows[0]), dict(self.rows[2])]
            if "status IN" in sql else [{"id": 1}] if "status = 'queued'" in sql else [])
        with mock.patch.object(jobs, "connect", return_value=connection), \
                mock.patch.object(jobs.service_config, "load_runtime_config", return_value={"decks": jobs._limits}):
            await jobs.start()
            await self.settle()
        self.assertEqual(self.adopted, [0])
        self.assertEqual(self.started, [])
        self.assertEqual(self.rows[2]["status"], "interrupted")
        self.gates[0].set()
        await self.settle()
        self.assertEqual(self.started, [1])


if __name__ == "__main__":
    unittest.main()
