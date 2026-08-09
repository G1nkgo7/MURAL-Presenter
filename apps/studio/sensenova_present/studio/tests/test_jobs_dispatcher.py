import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import jobs
from app.db import SCHEMA


class ImmediateDeckDispatchTests(unittest.TestCase):
    def test_non_positive_limit_schedules_deck_immediately(self):
        loop = mock.Mock()
        with (
            mock.patch.object(jobs, "MAX_PER_MODEL", 0),
            mock.patch.object(jobs, "_loop", loop),
            mock.patch.object(jobs, "_queues", {"ready": mock.Mock()}),
        ):
            jobs.enqueue(25)

        loop.call_soon_threadsafe.assert_called_once_with(
            jobs._schedule_unbounded, 25
        )

    def test_positive_limit_keeps_optional_per_model_queue(self):
        loop = mock.Mock()
        queue = mock.Mock()
        with (
            mock.patch.object(jobs, "MAX_PER_MODEL", 2),
            mock.patch.object(jobs, "_loop", loop),
            mock.patch.object(jobs, "_queues", {"model": queue}),
            mock.patch.object(jobs, "_model_key", return_value="model"),
        ):
            jobs.enqueue(25)

        loop.call_soon_threadsafe.assert_called_once_with(queue.put_nowait, 25)


class ImmediateDeckRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_guard_is_released_after_run(self):
        jobs._scheduled.add(25)
        row = {"id": 25, "status": "queued"}
        try:
            with (
                mock.patch.object(jobs, "_load_deck", return_value=row),
                mock.patch.object(jobs, "_process", new=mock.AsyncMock()) as process,
            ):
                await jobs._run_scheduled(25)
            process.assert_awaited_once_with(25)
            self.assertNotIn(25, jobs._scheduled)
        finally:
            jobs._scheduled.discard(25)


class StaticRevisionWorkspaceTests(unittest.IsolatedAsyncioTestCase):
    def test_presenter_existing_player_is_audited_and_repaired_before_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "deck"
            skill_root = root / "skill"
            (run_dir / "slides").mkdir(parents=True)
            (skill_root / "scripts").mkdir(parents=True)
            (run_dir / "slides/slide_01.html").write_text("<html></html>", encoding="utf-8")
            (run_dir / "present.html").write_text("<html></html>", encoding="utf-8")
            (skill_root / "scripts/deck.py").write_text("# test", encoding="utf-8")
            row = {"id": 12, "seed_json": "{}", "skill_version": "long-horizon-presenter"}
            failed = mock.Mock(returncode=1, stdout="", stderr="missing runtime")
            passed = mock.Mock(returncode=0, stdout="PASS", stderr="")
            with (
                mock.patch.object(jobs.engine, "canon_skill", return_value="long-horizon-presenter"),
                mock.patch.dict(jobs.engine.SKILLS, {
                    "long-horizon-presenter": {"path": str(skill_root)}
                }, clear=False),
                mock.patch.object(jobs.engine, "_engine_python", return_value="python"),
                mock.patch.object(jobs.engine, "log_path", return_value=root / "job.log"),
                mock.patch.object(jobs.subprocess, "run", side_effect=[failed, passed, passed]) as run,
            ):
                error = jobs._ensure_static_delivery(row, run_dir)
            self.assertIsNone(error)
            self.assertEqual([call.args[0][2] for call in run.call_args_list], ["audit", "build", "audit"])

    def test_successful_delivery_requires_present_html(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "renders").mkdir()
            (run_dir / "renders" / "slide_01.png").write_bytes(b"render")
            row = {"id": 12, "seed_json": "{}", "skill_version": "visual-craft"}
            with (
                mock.patch.object(jobs, "_load_deck", return_value=row),
                mock.patch.object(
                    jobs, "_ensure_static_delivery", return_value="missing present"
                ),
                mock.patch.object(jobs, "_fail_deck") as fail_deck,
            ):
                jobs._finalize(12, run_dir, rc=0)
            fail_deck.assert_called_once_with(12, "missing present")

    def test_rendered_pngs_do_not_override_failed_final_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "renders").mkdir()
            (run_dir / "renders" / "slide_01.png").write_bytes(b"render")
            (run_dir / "result.json").write_text(
                json.dumps({
                    "status": "rejected",
                    "reason": "最终 Review 未自然返回 ready",
                }),
                encoding="utf-8",
            )
            row = {
                "id": 14,
                "seed_json": "{}",
                "skill_version": "long-horizon-presenter",
            }
            with (
                mock.patch.object(jobs, "_load_deck", return_value=row),
                mock.patch.object(jobs, "_fail_deck") as fail_deck,
                mock.patch.object(jobs, "_ensure_static_delivery") as ensure_delivery,
            ):
                jobs._finalize(14, run_dir, rc=0)
            ensure_delivery.assert_not_called()
            fail_deck.assert_called_once_with(14, "最终 Review 未自然返回 ready")

    def test_in_place_revision_reuses_workspace_and_archives_only_run_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "deck-7"
            run_dir.mkdir()
            (run_dir / "present.html").write_text("<html>original</html>", encoding="utf-8")
            (run_dir / "result.json").write_text('{"status":"completed"}', encoding="utf-8")
            previous_log = Path(tmp) / "7.log"
            previous_log.write_text("initial run", encoding="utf-8")
            row = {"id": 7, "run_dir": str(run_dir)}
            revision = {
                "parent_deck_id": 7,
                "revision_no": 1,
                "instruction": "精简第 2 页",
                "in_place": True,
            }
            with mock.patch.object(jobs.engine, "log_path", return_value=previous_log):
                jobs._prepare_revision_workspace(row, revision)

            self.assertEqual((run_dir / "present.html").read_text(encoding="utf-8"), "<html>original</html>")
            self.assertFalse((run_dir / "result.json").exists())
            archive = run_dir / "_trace" / "revisions" / "revision_001"
            self.assertTrue((archive / "result-before.json").is_file())
            self.assertEqual((archive / "job-before.log").read_text(encoding="utf-8"), "initial run")
            request = json.loads((archive / "request.json").read_text(encoding="utf-8"))
            self.assertEqual(request["deck_id"], 7)
            self.assertTrue(request["in_place"])

    def test_revision_clones_formal_artifacts_without_runtime_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent_dir = root / "parent"
            target_dir = root / "revision"
            (parent_dir / "plan").mkdir(parents=True)
            (parent_dir / "assets").mkdir(parents=True)
            (parent_dir / "_trace" / "orchestrator").mkdir(parents=True)
            (parent_dir / "present.html").write_text("<html></html>", encoding="utf-8")
            (parent_dir / "plan" / "deck.md").write_text("# deck", encoding="utf-8")
            (parent_dir / "result.json").write_text("{}", encoding="utf-8")
            (parent_dir / "assets" / "source.txt").write_text("asset", encoding="utf-8")
            (parent_dir / "assets" / "alias.txt").symlink_to("source.txt")
            (parent_dir / "_trace" / "orchestrator" / "messages.json").write_text(
                "[]", encoding="utf-8"
            )
            parent = {"id": 7, "status": "completed", "run_dir": str(parent_dir)}
            child = {"id": 8, "run_dir": str(target_dir)}
            revision = {
                "parent_deck_id": 7,
                "revision_no": 1,
                "instruction": "精简第 2 页",
            }
            with mock.patch.object(jobs, "_load_deck", return_value=parent):
                jobs._prepare_revision_workspace(child, revision)

            self.assertTrue((target_dir / "present.html").is_file())
            self.assertTrue((target_dir / "plan" / "deck.md").is_file())
            self.assertFalse((target_dir / "result.json").exists())
            self.assertFalse((target_dir / "_trace" / "orchestrator").exists())
            self.assertFalse((target_dir / "assets" / "alias.txt").is_symlink())
            self.assertEqual(
                (target_dir / "assets" / "alias.txt").read_text(encoding="utf-8"),
                "asset",
            )
            marker = json.loads(
                (target_dir / "_trace" / "revision-parent.json").read_text(encoding="utf-8")
            )
            self.assertEqual(marker["parent_deck_id"], 7)
            self.assertEqual(marker["revision_no"], 1)

    async def test_waiting_revision_can_be_canceled_without_a_process(self):
        row = {"id": 9, "status": "waiting"}
        with (
            mock.patch.object(jobs, "_load_deck", return_value=row),
            mock.patch.object(jobs, "_set_status") as set_status,
        ):
            killed = await jobs.cancel(9)
        self.assertTrue(killed)
        set_status.assert_called_once()
        self.assertEqual(set_status.call_args.args[:2], (9, "interrupted"))
        self.assertEqual(set_status.call_args.kwargs["error"], "用户已停止")

    def test_user_stop_wins_over_completed_result_and_existing_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "renders").mkdir()
            (run_dir / "renders" / "slide_01.png").write_bytes(b"render")
            (run_dir / "result.json").write_text(
                '{"status":"completed"}', encoding="utf-8"
            )
            row = {"id": 13, "seed_json": "{}", "skill_version": "visual-craft"}
            jobs._user_canceled.add(13)
            try:
                with (
                    mock.patch.object(jobs, "_load_deck", return_value=row),
                    mock.patch.object(jobs, "_set_status") as set_status,
                    mock.patch.object(jobs, "_ensure_static_delivery") as ensure_delivery,
                    mock.patch.object(jobs, "_settle_waiting_revisions") as settle,
                ):
                    jobs._finalize(13, run_dir, rc=0)
                set_status.assert_called_once()
                self.assertEqual(set_status.call_args.args[:2], (13, "interrupted"))
                self.assertEqual(set_status.call_args.kwargs["error"], "用户已停止")
                ensure_delivery.assert_not_called()
                settle.assert_called_once_with(13, False)
            finally:
                jobs._user_canceled.discard(13)

    def test_revision_is_not_marked_complete_from_copied_pngs(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            (run_dir / "renders").mkdir()
            (run_dir / "renders" / "slide_01.png").write_bytes(b"copied")
            row = {
                "id": 11,
                "seed_json": json.dumps({"_revision": {"parent_deck_id": 7}}),
            }
            with (
                mock.patch.object(jobs, "_load_deck", return_value=row),
                mock.patch.object(jobs, "_fail_deck") as fail_deck,
            ):
                jobs._finalize(11, run_dir, rc=1)
            fail_deck.assert_called_once()
            self.assertEqual(fail_deck.call_args.args[0], 11)

    def test_completed_parent_releases_waiting_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "studio.db"

            def connect():
                con = sqlite3.connect(db_path)
                con.row_factory = sqlite3.Row
                return con

            con = connect()
            con.executescript(SCHEMA)
            con.execute(
                "INSERT INTO users(id,username,password_hash,created_at) "
                "VALUES(1,'alice','x',1)"
            )
            con.execute(
                "INSERT INTO decks(id,user_id,title,seed_json,status,created_at) "
                "VALUES(7,1,'parent','{}','completed',1)"
            )
            con.execute(
                "INSERT INTO decks(id,user_id,parent_deck_id,title,seed_json,status,created_at) "
                "VALUES(8,1,7,'revision','{}','waiting',2)"
            )
            con.commit()
            con.close()

            with (
                mock.patch.object(jobs, "connect", side_effect=connect),
                mock.patch.object(jobs, "enqueue") as enqueue,
            ):
                jobs._settle_waiting_revisions(7, True)

            con = connect()
            status = con.execute("SELECT status FROM decks WHERE id=8").fetchone()[0]
            con.close()
            self.assertEqual(status, "queued")
            enqueue.assert_called_once_with(8)


if __name__ == "__main__":
    unittest.main()
