import json
import tempfile
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "distillation"))
from attachments_runtime import build_initial_user_content, stage_seed_attachments
from serve_one import _patch_attachment_message_content


def _write_png(path: Path):
    from PIL import Image

    Image.new("RGB", (8, 6), (80, 120, 200)).save(path, format="PNG")


class AttachmentRuntimeTests(unittest.TestCase):
    def test_stage_seed_attachments_copies_raw_files_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "source.txt"
            src.write_text("raw attachment", encoding="utf-8")
            run_dir = root / "run"
            seed = {
                "_raw_attachments": [{
                    "source_path": str(src),
                    "workspace_rel": "attachments/raw/source.txt",
                    "name": "source.txt",
                    "type": "txt",
                    "size": src.stat().st_size,
                }]
            }

            count = stage_seed_attachments(seed, run_dir)

            self.assertEqual(count, 1)
            self.assertEqual((run_dir / "attachments" / "raw" / "source.txt").read_text(encoding="utf-8"), "raw attachment")
            manifest = json.loads((run_dir / "attachments" / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["raw_attachments"][0]["path"], "attachments/raw/source.txt")
            self.assertEqual(manifest["raw_attachments"][0]["name"], "source.txt")

    def test_stage_seed_attachments_builds_inline_image_message_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = root / "picture.png"
            _write_png(src)
            run_dir = root / "run"
            seed = {
                "_attachment_images": [{
                    "source_path": str(src),
                    "workspace_rel": "attachments/picture.png",
                    "name": "picture.png",
                    "kind": "image",
                }]
            }

            count = stage_seed_attachments(seed, run_dir)
            content = build_initial_user_content(seed, "make slides")

            self.assertEqual(count, 1)
            self.assertIsInstance(content, list)
            self.assertEqual(content[0]["type"], "text")
            image_blocks = [block for block in content if block.get("type") == "image"]
            self.assertEqual(len(image_blocks), 1)
            self.assertEqual(image_blocks[0]["source"]["type"], "base64")
            self.assertEqual(image_blocks[0]["source"]["media_type"], "image/png")
            self.assertGreater(len(image_blocks[0]["source"]["data"]), 20)

    def test_versioned_pipeline_seed_to_brief_is_wrapped_with_image_blocks(self):
        pipe = SimpleNamespace(seed_to_brief=lambda seed: "make slides")
        seed = {
            "_attachment_message_images": [{
                "name": "picture.png",
                "kind": "image",
                "path": "attachments/picture.png",
                "source": {"type": "base64", "media_type": "image/png", "data": "abc123"},
            }]
        }

        _patch_attachment_message_content(pipe)
        content = pipe.seed_to_brief(seed)

        self.assertIsInstance(content, list)
        self.assertEqual([b["type"] for b in content if b["type"] == "image"], ["image"])
        self.assertEqual(content[-1]["source"]["data"], "abc123")

    def test_versioned_pipeline_wrapper_preserves_staged_manifest_argument(self):
        calls = []

        def seed_to_brief(seed, staged_manifest=None):
            calls.append((seed, staged_manifest))
            return f"attachments={len(staged_manifest or [])}"

        pipe = SimpleNamespace(seed_to_brief=seed_to_brief)
        seed = {"query": "make slides"}
        staged = [{"name": "lecture.pdf", "raw": "materials/_raw/lecture.pdf"}]

        _patch_attachment_message_content(pipe)
        content = pipe.seed_to_brief(seed, staged)

        self.assertEqual(content, "attachments=1")
        self.assertEqual(calls, [(seed, staged)])


if __name__ == "__main__":
    unittest.main()
