import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from app import main
from app.db import SCHEMA


class StaticRevisionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.execute(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES(1,'alice','x',1)"
        )
        self.con.execute(
            "INSERT INTO conversations(id,user_id,title,created_at,updated_at) "
            "VALUES(3,1,'deck',1,1)"
        )

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def _insert_parent(self, status: str = "completed") -> None:
        seed = {
            "query": "做一份城市更新演示",
            "user_query": "做一份城市更新演示",
            "slide_count": 8,
        }
        self.con.execute(
            "INSERT INTO decks("
            "id,user_id,conversation_id,title,seed_json,status,model,pipeline,"
            "skill_version,run_dir,slide_count,created_at"
            ") VALUES(10,1,3,'城市更新',?,?,?,?,?,?,8,1)",
            (
                json.dumps(seed, ensure_ascii=False),
                status,
                "model",
                "infer",
                "long-horizon-grouped",
                str(Path(self.temp.name) / "parent"),
            ),
        )
        self.con.execute(
            "INSERT INTO messages(conversation_id,role,content,created_at) VALUES(3,'user',?,1)",
            (seed["user_query"],),
        )
        self.con.execute(
            "INSERT INTO messages(conversation_id,role,content,deck_id,created_at) "
            "VALUES(3,'assistant','正在生成 PPT…',10,1)"
        )
        self.con.commit()

    def test_completed_deck_queues_an_in_place_revision(self):
        self._insert_parent("completed")
        original_run_dir = str(Path(self.temp.name) / "parent")
        with mock.patch.object(main.jobs, "enqueue") as enqueue, mock.patch.object(
            main.jobs, "_engine_alive", return_value=False
        ):
            payload = main.continue_deck(
                10,
                main.DeckContinueRequest(message="精简第 3 页并增加配图"),
                user={"id": 1},
                con=self.con,
            )

        self.assertEqual(payload["status"], "queued")
        self.assertTrue(payload["in_place"])
        self.assertEqual(payload["deck_id"], 10)
        enqueue.assert_called_once_with(10)
        row = self.con.execute(
            "SELECT * FROM decks WHERE id = 10"
        ).fetchone()
        self.assertIsNone(row["parent_deck_id"])
        self.assertEqual(row["revision_no"], 1)
        self.assertEqual(row["slide_count"], 8)
        self.assertEqual(row["run_dir"], original_run_dir)
        self.assertEqual(json.loads(row["seed_json"])["_revision"]["instruction"], "精简第 3 页并增加配图")
        self.assertTrue(json.loads(row["seed_json"])["_revision"]["in_place"])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM decks").fetchone()[0], 1)
        messages = self.con.execute(
            "SELECT role,content,deck_id FROM messages ORDER BY id"
        ).fetchall()
        self.assertEqual(
            [message["role"] for message in messages],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(messages[-1]["deck_id"], 10)
        turns = main._deck_conversation_turns(self.con, row, json.loads(row["seed_json"]))
        self.assertEqual([turn["role"] for turn in turns], ["user", "assistant", "user", "assistant"])
        self.assertEqual([turn.get("current", False) for turn in turns], [False, False, False, True])
        self.assertEqual(turns[1]["status"], "completed")
        self.assertEqual(turns[-1]["status"], "queued")

    def test_running_deck_rejects_concurrent_in_place_followup(self):
        self._insert_parent("running")
        with mock.patch.object(main.jobs, "enqueue") as enqueue, mock.patch.object(
            main.jobs, "_engine_alive", return_value=True
        ):
            with self.assertRaises(HTTPException) as raised:
                main.continue_deck(
                    10,
                    main.DeckContinueRequest(message="封面换成大图"),
                    user={"id": 1},
                    con=self.con,
                )
        self.assertEqual(raised.exception.status_code, 409)
        enqueue.assert_not_called()

    def test_historical_turn_feed_reads_the_archived_previous_log(self):
        self._insert_parent("completed")
        run_dir = Path(self.temp.name) / "parent"
        archive = run_dir / "_trace" / "revisions" / "revision_001"
        archive.mkdir(parents=True)
        previous = archive / "job-before.log"
        previous.write_text(
            "[deck/orch] [0] 💬 开始制作初版\n"
            "[deck/image_initial] [1] 💬 已准备初版配图\n",
            encoding="utf-8",
        )
        (archive / "specialist-artifacts.json").write_text(json.dumps({
            "agents": {
                "image_initial": {
                    "catalog_ready": True,
                    "count": 1,
                    "contact_sheet": None,
                    "images": [{"path": "assets/hero.png", "name": "hero.png", "mtime": 1}],
                }
            },
            "image": {"catalog_ready": True, "count": 1, "contact_sheet": None, "images": []},
            "research": {"brief_ready": False, "queries": [], "sources": []},
            "material": {"count": 0, "files": []},
        }, ensure_ascii=False), encoding="utf-8")
        self.con.execute("UPDATE decks SET revision_no=1 WHERE id=10")
        self.con.commit()

        payload = main.deck_turn_feed(
            10, 0, user={"id": 1}, con=self.con,
        )

        self.assertIn("orch", payload["agents"])
        self.assertEqual(payload["agents"]["orch"][0]["s"], "开始制作初版")
        self.assertEqual(
            payload["specialist_artifacts"]["agents"]["image_initial"]["images"][0]["path"],
            "assets/hero.png",
        )

    def test_revision_workspace_archives_the_previous_turn_image_index(self):
        run_dir = Path(self.temp.name) / "archive-parent"
        image_agent = run_dir / "_trace" / "subagents" / "image_initial"
        assets = run_dir / "assets"
        image_agent.mkdir(parents=True)
        assets.mkdir()
        (run_dir / "present.html").write_text("<html></html>", encoding="utf-8")
        (run_dir / "result.json").write_text('{"status":"completed"}', encoding="utf-8")
        (assets / "hero.png").write_bytes(b"image")
        (image_agent / "messages.json").write_text(
            json.dumps([{"content": "已生成 assets/hero.png"}], ensure_ascii=False),
            encoding="utf-8",
        )
        previous_log = Path(self.temp.name) / "previous.log"
        previous_log.write_text(
            "[deck/orchestrator] [0] 💬 开始制作初版\n"
            "[deck/image_initial] [1] 💬 已准备初版配图\n",
            encoding="utf-8",
        )
        row = {
            "id": 10,
            "run_dir": str(run_dir),
            "seed_json": json.dumps({"ppt_output": "static_html"}),
        }

        with mock.patch.object(main.jobs.engine, "log_path", return_value=previous_log):
            main.jobs._prepare_revision_workspace(row, {
                "parent_deck_id": 10,
                "revision_no": 1,
                "instruction": "调整第五页",
                "in_place": True,
            })

        archive = run_dir / "_trace" / "revisions" / "revision_001"
        artifact_snapshot = json.loads(
            (archive / "specialist-artifacts.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            artifact_snapshot["agents"]["image_initial"]["images"][0]["path"],
            "assets/hero.png",
        )
        self.assertTrue((archive / "job-before.log").is_file())

    def test_waiting_revision_cannot_spawn_an_unbounded_chain(self):
        self._insert_parent("waiting")
        with self.assertRaises(HTTPException) as raised:
            main.continue_deck(
                10,
                main.DeckContinueRequest(message="再改一次"),
                user={"id": 1},
                con=self.con,
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_newer_slide_fragment_temporarily_supersedes_stale_present_player(self):
        self._insert_parent("running")
        run_dir = Path(self.temp.name) / "parent"
        slides = run_dir / "slides"
        renders = run_dir / "renders"
        slides.mkdir(parents=True)
        renders.mkdir()
        (run_dir / "base.css").write_text(
            ":root{--canvas-w:1280px;--canvas-h:720px}", encoding="utf-8"
        )
        present = run_dir / "present.html"
        fragment = slides / "slide_04.html"
        render = renders / "slide_04.png"
        present.write_text("<html>old player</html>", encoding="utf-8")
        fragment.write_text(
            '<section class="slide" data-slide="4"><img src="../assets/new.jpg"></section>',
            encoding="utf-8",
        )
        render.write_bytes(b"new thumbnail")
        os.utime(fragment, (100, 100))
        os.utime(render, (100, 100))
        os.utime(present, (200, 200))

        self.assertTrue(main._static_html_preview_state(str(run_dir))["final"])

        os.utime(fragment, (300, 300))
        preview = main._static_html_preview_state(str(run_dir))
        self.assertTrue(preview["ready"])
        self.assertFalse(preview["final"])
        response = main.deck_file_path(
            10, "present.html", user={"id": 1}, con=self.con,
        )
        self.assertEqual(response.headers["x-sensenova-preview"], "provisional")
        self.assertIn(b"../assets/new.jpg", response.body)

    def test_unsupported_pipeline_does_not_expose_fake_revision(self):
        self._insert_parent("completed")
        self.con.execute(
            "UPDATE decks SET pipeline='legacy-no-revision' WHERE id=10"
        )
        self.con.commit()
        with mock.patch.dict(
            main.engine.PIPELINES,
            {"legacy-no-revision": {"caps": ["attachments"]}},
        ):
            with self.assertRaises(HTTPException) as raised:
                main.continue_deck(
                    10,
                    main.DeckContinueRequest(message="换一张封面图"),
                    user={"id": 1},
                    con=self.con,
                )
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
