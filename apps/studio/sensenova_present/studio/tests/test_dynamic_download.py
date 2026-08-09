import tempfile
import unittest
from pathlib import Path

from studio.app.dynamic import _dynamic_delivery_files


class DynamicDownloadTests(unittest.TestCase):
    def test_delivery_keeps_local_assets_and_excludes_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "assets").mkdir()
            (root / "shots").mkdir()
            (root / "_trace").mkdir()
            (root / "deck.html").write_text('<img src="assets/hero.png">', encoding="utf-8")
            (root / "assets" / "hero.png").write_bytes(b"png")
            (root / "assets" / "theme.css").write_text("body{}", encoding="utf-8")
            (root / "shots" / "page_01.png").write_bytes(b"shot")
            (root / "_trace" / "tool.json").write_text("{}", encoding="utf-8")
            (root / "events.jsonl").write_text("{}\n", encoding="utf-8")
            (root / "messages.json").write_text("[]", encoding="utf-8")
            (root / "helper.py").write_text("pass", encoding="utf-8")

            exported = {
                rel.as_posix() for _, rel in _dynamic_delivery_files(root)
            }

            self.assertEqual(
                exported,
                {"deck.html", "assets/hero.png", "assets/theme.css"},
            )


if __name__ == "__main__":
    unittest.main()
