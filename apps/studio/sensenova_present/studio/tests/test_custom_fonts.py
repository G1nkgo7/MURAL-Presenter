import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException, UploadFile

from studio.app import custom_fonts, engine


class CustomFontTest(unittest.TestCase):
    def _font_bytes(self) -> tuple[str, bytes]:
        candidates = list((Path.home() / ".fonts").glob("NotoSansSC*.ttf"))
        if not candidates:
            candidates = list((Path.home() / ".fonts").glob("Archivo*.ttf"))
        if not candidates:
            self.skipTest("no readable test font installed")
        path = candidates[0]
        return path.name, path.read_bytes()

    def test_uploaded_font_requires_authorization(self):
        name, data = self._font_bytes()
        upload = UploadFile(filename=name, file=io.BytesIO(data))
        with self.assertRaises(HTTPException):
            custom_fonts.store_custom_fonts(7, [upload], "{}", False)

    def test_store_font_and_role_metadata(self):
        name, data = self._font_bytes()
        upload = UploadFile(filename=name, file=io.BytesIO(data))
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(
            custom_fonts.engine, "deck_uploads_dir", return_value=Path(temporary)
        ):
            config = custom_fonts.store_custom_fonts(
                7, [upload], '{"title":"custom:' + name + '","body":"builtin:Noto Sans SC"}', True
            )
        self.assertEqual(config["roles"]["title"]["kind"], "custom")
        self.assertEqual(config["roles"]["body"]["family"], "Noto Sans SC")
        self.assertEqual(config["fonts"][0]["sha256"], hashlib.sha256(data).hexdigest())
        self.assertGreater(config["fonts"][0]["glyph_count"], 0)

    def test_presenter_pair_and_composer_expose_custom_font_capability(self):
        self.assertIn("custom_fonts", engine.PIPELINES["mural-presenter-harness"]["caps"])
        template = (Path(__file__).resolve().parents[1] / "templates/app.html").read_text(encoding="utf-8")
        script = (Path(__file__).resolve().parents[1] / "static/app.js").read_text(encoding="utf-8")
        self.assertIn('id="font-input"', template)
        self.assertIn('id="font-license-ack"', template)
        self.assertIn('body.append("font_files", f)', script)


if __name__ == "__main__":
    unittest.main()
