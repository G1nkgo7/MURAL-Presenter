import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import trace


class PageHistoryTests(unittest.TestCase):
    def test_specialist_artifacts_accepts_v3_numbered_research_briefs(self):
        with tempfile.TemporaryDirectory(prefix="studio-specialist-research-") as tmp:
            run = Path(tmp)
            research = run / "research"
            research.mkdir()
            (research / "research_01.md").write_text("evidence", encoding="utf-8")

            data = trace.specialist_artifacts(str(run))

            self.assertTrue(data["research"]["brief_ready"])

    def test_specialist_artifacts_keeps_image_agent_galleries_separate(self):
        with tempfile.TemporaryDirectory(prefix="studio-specialist-artifacts-") as tmp:
            run = Path(tmp)
            assets = run / "assets"
            assets.mkdir()
            for name in ("image-a.png", "image-b.png", "image-c.png", "image-d.png"):
                (assets / name).write_bytes(name.encode("utf-8"))
            first = run / "_trace" / "subagents" / "image_01"
            second = run / "_trace" / "subagents" / "image_02"
            retry = run / "_trace" / "subagents" / "image_02_r2"
            first.mkdir(parents=True)
            second.mkdir(parents=True)
            retry.mkdir(parents=True)
            (first / "tool_log.json").write_text(json.dumps([
                {"name": "vision_analyze", "args": {"image_url": "assets/image-a.png"}},
                {"name": "vision_analyze", "args": {"image_url": "assets/image-b.png"}},
            ]), encoding="utf-8")
            (second / "tool_log.json").write_text(json.dumps([
                {"name": "vision_analyze", "args": {"image_url": "assets/image-c.png"}},
            ]), encoding="utf-8")
            (retry / "messages.json").write_text(json.dumps([
                {"content": "复检 assets/image-d.png"},
            ]), encoding="utf-8")

            data = trace.specialist_artifacts(str(run))

            self.assertEqual(data["image"]["count"], 4)
            self.assertEqual(
                [item["path"] for item in data["agents"]["image_01"]["images"]],
                ["assets/image-a.png", "assets/image-b.png"],
            )
            self.assertEqual(
                [item["path"] for item in data["agents"]["image_02"]["images"]],
                ["assets/image-c.png", "assets/image-d.png"],
            )

    def test_specialist_artifacts_ignores_images_only_read_from_catalog(self):
        with tempfile.TemporaryDirectory(prefix="studio-specialist-owned-images-") as tmp:
            run = Path(tmp)
            assets = run / "assets"
            agent = run / "_trace" / "subagents" / "image-dividers"
            assets.mkdir()
            agent.mkdir(parents=True)
            (assets / "owned.png").write_bytes(b"owned")
            (assets / "other-agent.png").write_bytes(b"other")
            (agent / "messages.json").write_text(json.dumps([
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "gen-1", "name": "image_generate", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "gen-1", "content": "assets/owned.png"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "read-1", "name": "read_file", "input": {"path": "assets/catalog.json"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "read-1", "content": "assets/owned.png\nassets/other-agent.png"},
                    ],
                },
            ]), encoding="utf-8")

            data = trace.specialist_artifacts(str(run))

            self.assertEqual(
                [item["path"] for item in data["agents"]["image-dividers"]["images"]],
                ["assets/owned.png"],
            )

    def test_specialist_artifacts_uses_fetch_image_results_as_ownership(self):
        with tempfile.TemporaryDirectory(prefix="studio-specialist-fetched-images-") as tmp:
            run = Path(tmp)
            assets = run / "assets"
            agent = run / "_trace" / "subagents" / "image-species"
            assets.mkdir()
            agent.mkdir(parents=True)
            (assets / "species.jpg").write_bytes(b"species")
            (assets / "other-agent.png").write_bytes(b"other")
            (agent / "messages.json").write_text(json.dumps([
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "fetch-1", "name": "fetch_image", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "fetch-1", "content": "assets/species.jpg"},
                    ],
                },
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "read-1", "name": "read_file", "input": {"path": "assets/catalog.json"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "read-1",
                            "content": "assets/species.jpg\nassets/other-agent.png",
                        },
                    ],
                },
            ]), encoding="utf-8")

            data = trace.specialist_artifacts(str(run))

            self.assertEqual(
                [item["path"] for item in data["agents"]["image-species"]["images"]],
                ["assets/species.jpg"],
            )

    def test_specialist_artifacts_keeps_all_images_and_contact_sheet_separate(self):
        with tempfile.TemporaryDirectory(prefix="studio-specialist-all-assets-") as tmp:
            run = Path(tmp)
            assets = run / "assets"
            agent = run / "_trace" / "subagents" / "image"
            assets.mkdir()
            agent.mkdir(parents=True)
            paths = [f"assets/image-{index:02d}.png" for index in range(15)]
            for rel in paths:
                (run / rel).write_bytes(rel.encode("utf-8"))
            (assets / "contact-sheet.png").write_bytes(b"sheet")
            (agent / "messages.json").write_text(json.dumps([
                {"content": "\n".join(paths + ["assets/contact-sheet.png"])}
            ]), encoding="utf-8")

            data = trace.specialist_artifacts(str(run))

            self.assertEqual(len(data["agents"]["image"]["images"]), 15)
            self.assertEqual(
                data["agents"]["image"]["contact_sheet"]["path"],
                "assets/contact-sheet.png",
            )

    def test_specialist_artifacts_exposes_per_asset_provenance(self):
        with tempfile.TemporaryDirectory(prefix="studio-specialist-provenance-") as tmp:
            run = Path(tmp)
            assets = run / "assets"
            agent = run / "_trace" / "subagents" / "image"
            assets.mkdir()
            agent.mkdir(parents=True)
            (assets / "real.jpg").write_bytes(b"real")
            (assets / "made.png").write_bytes(b"made")
            (assets / "catalog.json").write_text(json.dumps({
                "schema_version": 1,
                "assets": [
                    {"path": "assets/real.jpg", "origin": "downloaded", "source_url": "https://example.com/real.jpg"},
                    {"path": "assets/made.png", "origin": "generated", "generator_model": "image-model"},
                ],
            }), encoding="utf-8")
            (agent / "messages.json").write_text(json.dumps([
                {"content": "assets/real.jpg\nassets/made.png"},
            ]), encoding="utf-8")

            data = trace.specialist_artifacts(str(run))
            items = {item["path"]: item for item in data["agents"]["image"]["images"]}

            self.assertTrue(data["image"]["catalog_ready"])
            self.assertEqual(items["assets/real.jpg"]["origin"], "downloaded")
            self.assertEqual(items["assets/real.jpg"]["source_url"], "https://example.com/real.jpg")
            self.assertEqual(items["assets/made.png"]["origin"], "generated")
            self.assertEqual(items["assets/made.png"]["generator_model"], "image-model")

    def test_specialist_artifacts_counts_only_ready_asset_ids(self):
        with tempfile.TemporaryDirectory(prefix="studio-specialist-ready-assets-") as tmp:
            run = Path(tmp)
            assets = run / "assets"
            agent = run / "_trace" / "subagents" / "image_hero"
            assets.mkdir()
            agent.mkdir(parents=True)
            for name in ("ready.png", "candidate.png", "discarded.png"):
                (assets / name).write_bytes(name.encode("utf-8"))
            (assets / "contact-sheet-hero.png").write_bytes(b"sheet")
            (assets / "catalog.json").write_text(json.dumps({
                "schema_version": 2,
                "assets": [
                    {"path": "assets/ready.png", "asset_id": "hero", "group_id": "hero", "status": "ready"},
                    {"path": "assets/candidate.png", "asset_id": "support", "group_id": "hero", "status": "needs_review"},
                    {"path": "assets/discarded.png", "asset_id": "old-hero", "group_id": "hero", "status": "rejected"},
                ],
            }), encoding="utf-8")
            (agent / "messages.json").write_text(json.dumps([{
                "content": (
                    "assets/ready.png\nassets/candidate.png\nassets/discarded.png\n"
                    "assets/contact-sheet-hero.png"
                ),
            }]), encoding="utf-8")

            data = trace.specialist_artifacts(str(run))

            self.assertEqual(data["image"]["count"], 1)
            self.assertEqual(data["agents"]["image_hero"]["count"], 1)
            self.assertEqual(
                data["agents"]["image_hero"]["contact_sheet"]["path"],
                "assets/contact-sheet-hero.png",
            )
            self.assertEqual(len(data["agents"]["image_hero"]["images"]), 3)

    def test_specialist_artifacts_deduplicates_identical_image_aliases(self):
        with tempfile.TemporaryDirectory(prefix="studio-specialist-image-alias-") as tmp:
            run = Path(tmp)
            assets = run / "assets"
            agent = run / "_trace" / "subagents" / "image"
            assets.mkdir()
            agent.mkdir(parents=True)
            payload = b"same generated image bytes"
            (assets / "img_12345678ab.png").write_bytes(payload)
            (assets / "dino_brachiosaurus.png").write_bytes(payload)
            (assets / "catalog.json").write_text(json.dumps({
                "schema_version": 1,
                "assets": [{
                    "path": "assets/img_12345678ab.png",
                    "origin": "generated",
                    "generator_model": "image-model",
                }],
            }), encoding="utf-8")
            (agent / "messages.json").write_text(json.dumps([{
                "content": "assets/img_12345678ab.png\nassets/dino_brachiosaurus.png",
            }]), encoding="utf-8")

            data = trace.specialist_artifacts(str(run))

            self.assertEqual(data["image"]["count"], 1)
            self.assertEqual(len(data["agents"]["image"]["images"]), 1)
            item = data["agents"]["image"]["images"][0]
            self.assertEqual(item["path"], "assets/dino_brachiosaurus.png")
            self.assertEqual(item["origin"], "generated")
            self.assertEqual(item["generator_model"], "image-model")

    def test_livefeed_keeps_specialist_agents_and_merges_retries(self):
        with tempfile.TemporaryDirectory(prefix="studio-livefeed-") as tmp:
            log = Path(tmp) / "job.log"
            log.write_text(
                "[u1_d1/orchestrator] [1] 💬 开始拆解任务。\n"
                "[u1_d1/research] [1] 💬 已形成研究简报。\n"
                "[u1_d1/slide_01] 🔧 write_file({\"path\": \"slides/slide_01.html\"})\n"
                "[u1_d1/slide_01_r2] [2] 💬 第 1 页修订完成。\n"
                "[u1_d1/review] [1] 💬 整册审校完成。\n",
                encoding="utf-8",
            )

            agents = trace.livefeed(str(log))["agents"]

            self.assertEqual(set(agents), {"orch", "research", "slide_01", "review"})
            self.assertEqual(len(agents["slide_01"]), 2)
            self.assertIn("研究简报", agents["research"][0]["s"])
            sequence = [event["seq"] for events in agents.values() for event in events]
            self.assertEqual(len(sequence), len(set(sequence)))

    def test_livefeed_merges_historical_orchestrator_aliases(self):
        with tempfile.TemporaryDirectory(prefix="studio-orch-aliases-") as tmp:
            log = Path(tmp) / "job.log"
            log.write_text(
                "[u1_d1/orchestrator] [1] 💬 正在编写规划。\n"
                "[u1_d1/orch] [2] 💬 正在编写规划。\n"
                "[u1_d1/orchestrator_r2] [3] 💬 正在补充页面地图。\n",
                encoding="utf-8",
            )

            agents = trace.livefeed(str(log))["agents"]

            self.assertEqual(set(agents), {"orch"})
            self.assertEqual(
                [event["s"] for event in agents["orch"] if event["k"] == "text"],
                ["正在编写规划。", "正在补充页面地图。"],
            )

    def test_snapshot_normalizes_orchestrator_activity_alias(self):
        with tempfile.TemporaryDirectory(prefix="studio-orch-activity-") as tmp:
            run = Path(tmp)
            log = run / "job.log"
            log.write_text(
                "[u1_d1/orchestrator] 🔧 read_file({\"path\": \"plan/deck.md\"})\n",
                encoding="utf-8",
            )

            progress = trace.snapshot(str(run), status="running", log_path=str(log))

            self.assertEqual(progress["activity"]["agent"], "orch")

    def test_livefeed_hides_wrong_language_process_prose_but_keeps_tools(self):
        with tempfile.TemporaryDirectory(prefix="studio-livefeed-language-") as tmp:
            run = Path(tmp)
            plan = run / "plan"
            plan.mkdir()
            (plan / "deck.md").write_text(
                "# Deck\n- response_language：中文\n", encoding="utf-8"
            )
            log = run / "job.log"
            log.write_text(
                "[u1_d1/slide-content] [1] 💬 Now let me fix slide 04.\n"
                "[u1_d1/slide-content] 🔧 patch({\"path\": \"slides/slide_04.html\"})\n"
                "[u1_d1/slide-content] [2] 💬 第 4 页已完成修复。\n",
                encoding="utf-8",
            )

            events = trace.livefeed(str(log), run_dir=str(run))["agents"]["slide-content"]

            self.assertEqual([event["k"] for event in events], ["tool", "text"])
            self.assertEqual(events[-1]["s"], "第 4 页已完成修复。")

    def test_livefeed_recovers_named_roles_from_historical_child_configs(self):
        with tempfile.TemporaryDirectory(prefix="studio-child-role-aliases-") as tmp:
            run = Path(tmp)
            image = run / "_trace" / "subagents" / "child_01"
            slides = run / "_trace" / "subagents" / "child_02"
            review = run / "_trace" / "subagents" / "child_03"
            for path in (image, slides, review):
                path.mkdir(parents=True)
            (image / "config.json").write_text(json.dumps({
                "label": "child_01",
                "task": "你的身份是 child_01。\n\n[image-motif] 生成两张母题图",
            }, ensure_ascii=False), encoding="utf-8")
            (slides / "config.json").write_text(json.dumps({
                "label": "child_02",
                "task": "你的身份是 child_02。\n\n[slide-bookends] 制作封面和结尾",
            }, ensure_ascii=False), encoding="utf-8")
            (review / "config.json").write_text(json.dumps({
                "label": "child_03",
                "task": "你的身份是 child_03。\n\n[review-final] 复核整册",
            }, ensure_ascii=False), encoding="utf-8")
            log = run / "job.log"
            log.write_text(
                "[u1_d1/child_01] [1] 💬 图片已经准备。\n"
                "[u1_d1/child_02] [1] 💬 封面和结尾已经完成。\n"
                "[u1_d1/child_03] [1] 💬 整册复核完成。\n",
                encoding="utf-8",
            )

            agents = trace.livefeed(str(log), run_dir=str(run))["agents"]

            self.assertEqual(set(agents), {"image_motif", "slide_group_bookends", "review"})

    def test_specialist_artifacts_recovers_child_image_and_research_details(self):
        with tempfile.TemporaryDirectory(prefix="studio-child-specialist-artifacts-") as tmp:
            run = Path(tmp)
            assets = run / "assets"
            assets.mkdir()
            (assets / "motif.png").write_bytes(b"image")
            (run / "research").mkdir()
            (run / "research" / "knowledge-brief.md").write_text("brief", encoding="utf-8")

            image = run / "_trace" / "subagents" / "child_01"
            research = run / "_trace" / "subagents" / "child_02"
            image.mkdir(parents=True)
            research.mkdir(parents=True)
            (image / "config.json").write_text(json.dumps({
                "task": "你的身份是 child_01。\n\n[image-motif] 生成配图",
            }, ensure_ascii=False), encoding="utf-8")
            (image / "messages.json").write_text(json.dumps([
                {"content": "已生成 assets/motif.png"},
            ], ensure_ascii=False), encoding="utf-8")
            (research / "config.json").write_text(json.dumps({
                "task": "你的身份是 child_02。\n\n[research-market] 核验市场事实",
            }, ensure_ascii=False), encoding="utf-8")
            (research / "tool_log.json").write_text(json.dumps([
                {"name": "web_search", "args": {"query": "城市热岛 遥感 数据"}},
            ], ensure_ascii=False), encoding="utf-8")
            (research / "messages.json").write_text(json.dumps([
                {"content": "- NASA Earth Observatory\nhttps://earthobservatory.nasa.gov/example"},
            ], ensure_ascii=False), encoding="utf-8")

            data = trace.specialist_artifacts(str(run))

            self.assertEqual(
                [item["path"] for item in data["agents"]["image_motif"]["images"]],
                ["assets/motif.png"],
            )
            self.assertEqual(data["agents"]["research_market"]["queries"], ["城市热岛 遥感 数据"])
            self.assertEqual(
                data["agents"]["research_market"]["sources"][0]["url"],
                "https://earthobservatory.nasa.gov/example",
            )

    def test_livefeed_restores_complete_subagent_summary_after_single_line_log(self):
        with tempfile.TemporaryDirectory(prefix="studio-livefeed-summary-") as tmp:
            run = Path(tmp)
            agent = run / "_trace" / "subagents" / "slide_02"
            agent.mkdir(parents=True)
            summary = (
                "我是 slide_02，已产出第 02 页。\n\n"
                "- 版式：完成目录结构与视觉层级。\n"
                "- 自检：完成两轮渲染并清除遮挡。\n"
                "- 状态：通过。"
            )
            (agent / "summary.md").write_text(summary, encoding="utf-8")
            log = run / "job.log"
            log.write_text(
                "[u1_d1/slide_02] 🔧 write_file({\"path\": \"slides/slide_02.html\"})\n"
                "[u1_d1/slide_02] [8] 💬 我是 slide_02，已产出第 02 页。\n",
                encoding="utf-8",
            )

            events = trace.livefeed(str(log), run_dir=str(run))["agents"]["slide_02"]

            messages = [event["s"] for event in events if event["k"] == "text"]
            self.assertEqual(messages, [summary])
            self.assertIn("完成两轮渲染", messages[0])

    def test_livefeed_does_not_move_stale_subagent_summary_into_revision(self):
        with tempfile.TemporaryDirectory(prefix="studio-livefeed-revision-") as tmp:
            run = Path(tmp)
            old_image = run / "_trace" / "subagents" / "image_initial"
            current_review = run / "_trace" / "subagents" / "review"
            old_image.mkdir(parents=True)
            current_review.mkdir(parents=True)
            (old_image / "summary.md").write_text(
                "初版图片 Agent 已生成并检查全部视觉素材。", encoding="utf-8"
            )
            (current_review / "summary.md").write_text(
                "本轮已按修改意见复核第五页。", encoding="utf-8"
            )
            log = run / "job.log"
            log.write_text(
                "[u1_d1/orchestrator] [1] 💬 开始修改第五页。\n"
                "[u1_d1/review] [2] 💬 本轮已按修改意见复核第五页。\n",
                encoding="utf-8",
            )

            agents = trace.livefeed(str(log), run_dir=str(run))["agents"]

            self.assertEqual(set(agents), {"orch", "review"})
            self.assertNotIn("image_initial", agents)

    def test_livefeed_maps_grouped_pages_to_their_group_agent(self):
        with tempfile.TemporaryDirectory(prefix="studio-grouped-livefeed-") as tmp:
            run = Path(tmp)
            plan = run / "plan"
            plan.mkdir()
            (plan / "slide_02.md").write_text(
                "# slide_02\n- production_group: dividers\n", encoding="utf-8")
            (plan / "slide_04.md").write_text(
                "# slide_04\n- production_group: dividers\n", encoding="utf-8")
            (plan / "slide_06.md").write_text(
                "# slide_06\n- production_group: chapter-bloom\n", encoding="utf-8")
            log = run / "job.log"
            log.write_text(
                "[u1_d1/slide_group_dividers] 🔧 read_file({\"path\": \"plan/slide_02.md\"})\n"
                "[u1_d1/slide_group_dividers] 🔧 terminal({\"command\": \"python deck.py render-group . --group dividers --pages 02,04\"})\n"
                "[u1_d1/slide_group_chapter-bloom] 🔧 write_file({\"path\": \"slides/slide_06.html\"})\n",
                encoding="utf-8",
            )

            feed = trace.livefeed(str(log), run_dir=str(run))

            self.assertEqual(feed["page_agents"], {
                "2": "slide_group_dividers",
                "4": "slide_group_dividers",
                "6": "slide_group_chapter-bloom",
            })
            self.assertIn("slide_group_dividers", feed["agents"])
            self.assertIn("slide_group_chapter-bloom", feed["agents"])

    def test_livefeed_maps_current_group_labels_and_fullwidth_plan_colon(self):
        with tempfile.TemporaryDirectory(prefix="studio-current-grouped-livefeed-") as tmp:
            run = Path(tmp)
            plan = run / "plan"
            agent = run / "_trace" / "subagents" / "slide-g08"
            plan.mkdir()
            agent.mkdir(parents=True)
            (plan / "slide_02.md").write_text(
                "# slide_02\n- production_group：g08-summary\n", encoding="utf-8")
            (plan / "slide_28.md").write_text(
                "# slide_28\n- production_group：g08-summary\n", encoding="utf-8")
            (agent / "config.json").write_text(json.dumps({
                "label": "slide-g08",
                "task": "你的身份是 slide-g08。\n\nSlide Group g08-summary [02, 28]：制作目录和总结。",
            }, ensure_ascii=False), encoding="utf-8")
            log = run / "job.log"
            log.write_text(
                "[u1_d1/slide-g08] 🔧 read_file({\"path\": \"plan/slide_02.md\"})\n"
                "[u1_d1/slide-g08] 🔧 write_file({\"path\": \"slides/slide_28.html\"})\n",
                encoding="utf-8",
            )

            feed = trace.livefeed(str(log), run_dir=str(run))

            self.assertEqual(feed["page_agents"], {
                "2": "slide_group_g08-summary",
                "28": "slide_group_g08-summary",
            })
            self.assertIn("slide_group_g08-summary", feed["agents"])

    def test_page_history_keeps_real_versions_and_review(self):
        with tempfile.TemporaryDirectory(prefix="studio-trace-") as tmp:
            run = Path(tmp)
            slide_images = run / "_trace" / "subagents" / "slide_01" / "images"
            review_images = run / "_trace" / "subagents" / "review" / "images"
            slide_images.mkdir(parents=True)
            review_images.mkdir(parents=True)
            (slide_images / "view_001.png").write_bytes(b"first")
            (slide_images / "view_002.png").write_bytes(b"second")
            (review_images / "view_001.png").write_bytes(b"review")

            events = [
                {"clock": "10:00:00", "label": "slide_01", "message": "🔧 terminal({\"command\": \"python deck.py render . --page 1\"})"},
                {"clock": "10:00:01", "label": "slide_01", "message": "🔧 vision_analyze({\"image_url\": \"renders/slide_01.png\", \"question\": \"检查标题层级与投影可读性\"})"},
                {"clock": "10:00:02", "label": "slide_01", "message": "🔧 patch({\"path\": \"slides/slide_01.html\", \"old_string\": \"#slide-01 .title { font-size: 80px; }\", \"new_string\": \"#slide-01 .title { font-size: 96px; }\"})"},
                {"clock": "10:00:03", "label": "slide_01", "message": "🔧 terminal({\"command\": \"python deck.py render . --page 1\"})"},
                {"clock": "10:00:04", "label": "slide_01", "message": "🔧 vision_analyze({\"image_url\": \"renders/slide_01.png\", \"question\": \"复查标题与边界\"})"},
                {"clock": "10:00:05", "label": "slide_01", "message": "[9] 💬 标题层级清晰，页面边界完整，无溢出。"},
                {"clock": "10:01:00", "label": "review", "message": "🔧 vision_analyze({\"image_url\": \"renders/slide_01.png\", \"question\": \"整册复核封面焦点与一致性\"})"},
                {"clock": "10:01:02", "label": "review", "message": "[2] 💬 整册复审完成。\n- **P01 封面**：焦点明确，标题和主视觉一致。"},
            ]
            event_file = run / "_trace" / "events.jsonl"
            event_file.parent.mkdir(exist_ok=True)
            event_file.write_text(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
                encoding="utf-8",
            )

            data = trace.page_history(str(run), 1)

            self.assertEqual(data["total"], 3)
            self.assertEqual([item["stage"] for item in data["items"]], ["build", "build", "review"])
            self.assertEqual(data["items"][0]["changes"], 1)
            self.assertIn("font-size", data["items"][0]["change_notes"][0])
            self.assertEqual(data["items"][0]["image_url"], "_trace/subagents/slide_01/images/view_001.png")
            self.assertEqual(data["items"][1]["image_url"], "_trace/subagents/slide_01/images/view_002.png")
            self.assertIn("无溢出", data["items"][1]["judgment"])
            self.assertEqual(data["items"][2]["image_url"], "_trace/subagents/review/images/view_001.png")
            self.assertIn("焦点明确", data["items"][2]["judgment"])

    def test_page_history_reads_v3_per_agent_trace_and_two_digit_views(self):
        with tempfile.TemporaryDirectory(prefix="studio-trace-v3-") as tmp:
            run = Path(tmp)
            slide = run / "_trace" / "subagents" / "slide_02"
            review = run / "_trace" / "subagents" / "review_01"
            (slide / "images").mkdir(parents=True)
            (review / "images").mkdir(parents=True)
            (slide / "images" / "view_01.png").write_bytes(b"build")
            (review / "images" / "view_02.png").write_bytes(b"review")
            (slide / "tool_log.json").write_text(json.dumps([
                {"turn": 1, "name": "terminal", "args": {"command": "python render.py slides/slide_02.html renders/slide_02.png"}},
                {"turn": 2, "name": "vision_analyze", "args": {"image_url": "renders/slide_02.png", "question": "检查标题和边界"}},
                {"turn": 3, "name": "patch", "args": {"path": "slides/slide_02.html", "old_string": ".title {font-size:40px}", "new_string": ".title {font-size:48px}"}},
            ], ensure_ascii=False), encoding="utf-8")
            (slide / "summary.md").write_text("标题清晰，页面边界完整。", encoding="utf-8")
            (review / "tool_log.json").write_text(json.dumps([
                {"turn": 1, "name": "vision_analyze", "args": {"image_url": "renders/slide_01.png", "question": "检查封面"}},
                {"turn": 1, "name": "vision_analyze", "args": {"image_url": "renders/slide_02.png", "question": "复核第二页"}},
            ], ensure_ascii=False), encoding="utf-8")
            (review / "summary.md").write_text(
                "## 问题清单\n\n- **slide_02**：层级清楚，无溢出。\n", encoding="utf-8")

            data = trace.page_history(str(run), 2)

            self.assertEqual(data["total"], 2)
            self.assertEqual([item["stage"] for item in data["items"]], ["build", "review"])
            self.assertEqual(data["items"][0]["image_url"], "_trace/subagents/slide_02/images/view_01.png")
            self.assertEqual(data["items"][0]["changes"], 1)
            self.assertIn("font-size", data["items"][0]["change_notes"][0])
            self.assertEqual(data["items"][1]["image_url"], "_trace/subagents/review_01/images/view_02.png")
            self.assertIn("层级清楚", data["items"][1]["judgment"])

    def test_page_history_reads_each_v3_vision_verdict_from_messages(self):
        with tempfile.TemporaryDirectory(prefix="studio-trace-v3-verdict-") as tmp:
            run = Path(tmp)
            slide = run / "_trace" / "subagents" / "slide_03"
            (slide / "images").mkdir(parents=True)
            (slide / "images" / "view_01.png").write_bytes(b"first")
            (slide / "images" / "view_02.png").write_bytes(b"second")
            (slide / "tool_log.json").write_text(json.dumps([
                {"turn": 1, "name": "vision_analyze", "args": {
                    "image_url": "renders/slide_03.png", "question": "第一次检查"}},
                {"turn": 2, "name": "patch", "args": {
                    "path": "slides/slide_03.html",
                    "old_string": ".title { font-size: 40px; }",
                    "new_string": ".title { font-size: 48px; }"}},
                {"turn": 3, "name": "vision_analyze", "args": {
                    "image_url": "renders/slide_03.png", "question": "最终验收"}},
            ], ensure_ascii=False), encoding="utf-8")
            (slide / "summary.md").write_text("整组最终总结，不应覆盖每次检查结果。", encoding="utf-8")
            (slide / "messages.json").write_text(json.dumps([
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "vision-1", "name": "vision_analyze",
                    "input": {"image_url": "renders/slide_03.png", "question": "第一次检查"}}]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "vision-1", "content": [
                        {"type": "text", "text": "正在查看 renders/slide_03.png。"},
                        {"type": "image", "shot": "images/view_01.png"}]}]},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "标题偏小，需要增大字号。"},
                    {"type": "tool_use", "id": "patch-1", "name": "patch", "input": {}}]},
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "vision-2", "name": "vision_analyze",
                    "input": {"image_url": "renders/slide_03.png", "question": "最终验收"}}]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "vision-2", "content": [
                        {"type": "text", "text": "正在查看 renders/slide_03.png。"},
                        {"type": "image", "shot": "images/view_02.png"}]}]},
                {"role": "assistant", "content": [{"type": "text", "text": "复查通过，无溢出或遮挡。"}]},
            ], ensure_ascii=False), encoding="utf-8")

            data = trace.page_history(str(run), 3)

            self.assertEqual(data["total"], 2)
            self.assertEqual(data["items"][0]["judgment"], "标题偏小，需要增大字号。")
            self.assertEqual(data["items"][1]["judgment"], "复查通过，无溢出或遮挡。")

    def test_v3_page_history_keeps_initial_checks_before_in_place_revision(self):
        with tempfile.TemporaryDirectory(prefix="studio-trace-v3-revision-") as tmp:
            run = Path(tmp)
            initial = run / "_trace" / "subagents" / "slide_04"
            revision = run / "_trace" / "subagents" / "slide-04-08"
            request = run / "_trace" / "revisions" / "revision_001" / "request.json"
            for agent, marker in ((initial, "初版检查"), (revision, "修改后复查")):
                (agent / "images").mkdir(parents=True)
                (agent / "images" / "view_01.png").write_bytes(marker.encode("utf-8"))
                (agent / "config.json").write_text("{}", encoding="utf-8")
                (agent / "tool_log.json").write_text(json.dumps([{
                    "turn": 1,
                    "name": "vision_analyze",
                    "args": {"image_url": "renders/slide_04.png", "question": marker},
                }], ensure_ascii=False), encoding="utf-8")
                (agent / "summary.md").write_text(marker, encoding="utf-8")
            request.parent.mkdir(parents=True)
            request.write_text(json.dumps({
                "revision_no": 1,
                "created_at": 200,
            }), encoding="utf-8")
            os.utime(initial / "config.json", (100, 100))
            os.utime(revision / "config.json", (300, 300))

            data = trace.page_history(str(run), 4)

            self.assertEqual([item["prompt"] for item in data["items"]], ["初版检查", "修改后复查"])
            self.assertEqual([item["revision_no"] for item in data["items"]], [0, 1])

    def test_page_history_uses_exact_message_shot_when_a_vision_snapshot_is_missing(self):
        with tempfile.TemporaryDirectory(prefix="studio-trace-v3-missing-shot-") as tmp:
            run = Path(tmp)
            slide = run / "_trace" / "subagents" / "slide_group"
            (slide / "images").mkdir(parents=True)
            (slide / "images" / "view_01.png").write_bytes(b"page-8-first")
            (slide / "images" / "view_02.png").write_bytes(b"page-8-final")
            (slide / "images" / "view_03.png").write_bytes(b"page-12-final")
            (slide / "tool_log.json").write_text(json.dumps([
                {"turn": 1, "name": "vision_analyze", "args": {
                    "image_url": "renders/slide_08.png", "question": "第一次检查"}},
                {"turn": 2, "name": "vision_analyze", "args": {
                    "image_url": "renders/slide_12.png", "question": "本次没有保存快照"}},
                {"turn": 3, "name": "vision_analyze", "args": {
                    "image_url": "renders/slide_08.png", "question": "最终验收"}},
                {"turn": 3, "name": "vision_analyze", "args": {
                    "image_url": "renders/slide_12.png", "question": "第十二页最终验收"}},
            ], ensure_ascii=False), encoding="utf-8")
            (slide / "messages.json").write_text(json.dumps([
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "vision-1", "name": "vision_analyze",
                    "input": {"image_url": "renders/slide_08.png"}}]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "vision-1", "content": [
                        {"type": "image", "shot": "images/view_01.png"}]}]},
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "vision-2", "name": "vision_analyze",
                    "input": {"image_url": "renders/slide_12.png"}}]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "vision-2", "content": [
                        {"type": "text", "text": "检查调用失败，没有快照。"}]}]},
                {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "vision-3", "name": "vision_analyze",
                    "input": {"image_url": "renders/slide_08.png"}}, {
                    "type": "tool_use", "id": "vision-4", "name": "vision_analyze",
                    "input": {"image_url": "renders/slide_12.png"}}]},
                {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "vision-3", "content": [
                        {"type": "image", "shot": "images/view_02.png"}]}, {
                    "type": "tool_result", "tool_use_id": "vision-4", "content": [
                        {"type": "image", "shot": "images/view_03.png"}]}]},
                {"role": "assistant", "content": [{
                    "type": "text", "text": "第 08 页与第 12 页最终验收通过。"}]},
            ], ensure_ascii=False), encoding="utf-8")

            data = trace.page_history(str(run), 8)

            self.assertEqual(data["total"], 2)
            self.assertEqual(data["items"][0]["image_url"],
                             "_trace/subagents/slide_group/images/view_01.png")
            self.assertEqual(data["items"][1]["image_url"],
                             "_trace/subagents/slide_group/images/view_02.png")

    def test_page_history_rejects_non_page_values(self):
        self.assertEqual(trace.page_history("/missing", 0), {"page": 0, "items": []})

    def test_page_speech_returns_only_the_selected_slide(self):
        with tempfile.TemporaryDirectory(prefix="studio-speech-") as tmp:
            run = Path(tmp)
            (run / "speech.md").write_text(
                """# 演讲备注

## Slide 01 — 开场

开场 30 秒，介绍主题。

### Evidence and sources

- 来源 A

## Slide 02 — 核心结论

- 先讲结论
- 再讲证据

### Evidence and sources

- 来源 B
- 来源 C
""",
                encoding="utf-8",
            )

            speech = trace.page_speech(str(run), 2)

            self.assertTrue(speech["exists"])
            self.assertEqual(speech["title"], "核心结论")
            self.assertIn("先讲结论", speech["notes"])
            self.assertNotIn("开场 30 秒", speech["notes"])
            self.assertIn("来源 B", speech["evidence"])
            self.assertEqual(speech["source"], "speech.md")

    def test_page_speech_supports_legacy_plan_path_and_chinese_heading(self):
        with tempfile.TemporaryDirectory(prefix="studio-speech-legacy-") as tmp:
            run = Path(tmp)
            (run / "plan").mkdir()
            (run / "plan" / "speech.md").write_text(
                "## 第 3 页：收束\n\n讲清楚下一步。\n",
                encoding="utf-8",
            )

            speech = trace.page_speech(str(run), 3)

            self.assertTrue(speech["exists"])
            self.assertEqual(speech["title"], "收束")
            self.assertEqual(speech["notes"], "讲清楚下一步。")
            self.assertEqual(speech["source"], "plan/speech.md")

    def test_page_speech_reads_v3_heading_and_reference_section(self):
        with tempfile.TemporaryDirectory(prefix="studio-speech-v3-") as tmp:
            run = Path(tmp)
            (run / "speech.md").write_text(
                "# 第 01 页｜封面页\n\n## 讲述内容\n\n欢迎来到今天的分享。\n\n"
                "## 参考资料（不朗读）\n\n- 来源 A\n\n"
                "# 第 02 页｜核心结论\n\n## 讲述内容\n\n第二页讲稿。\n",
                encoding="utf-8",
            )

            speech = trace.page_speech(str(run), 1)

            self.assertTrue(speech["exists"])
            self.assertEqual(speech["title"], "封面页")
            self.assertEqual(speech["notes"], "欢迎来到今天的分享。")
            self.assertIn("来源 A", speech["evidence"])
            self.assertNotIn("讲述内容", speech["notes"])


if __name__ == "__main__":
    unittest.main()
