import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import custom_models
from app.db import SCHEMA

import sqlite3


class CustomModelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        self.con.execute(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES(1,'alice','x',1)"
        )
        self.con.execute(
            "INSERT INTO users(id,username,password_hash,created_at) VALUES(2,'bob','x',1)"
        )
        self.key_file = Path(self.tmp.name) / "model.key"
        self.key_patch = patch.object(custom_models, "_KEY_FILE", self.key_file)
        self.key_patch.start()

    def tearDown(self):
        self.key_patch.stop()
        self.con.close()
        self.tmp.cleanup()

    def test_create_encrypts_secret_and_public_payload_never_returns_it(self):
        row = custom_models.create(
            self.con, 1, "视觉模型", "vision-v1", "https://example.com/v1/", "sk-secret"
        )
        self.assertNotEqual(row["api_key_enc"], "sk-secret")
        self.assertNotIn("sk-secret", row["api_key_enc"])
        self.assertEqual(custom_models.runtime_config(row)["api_key"], "sk-secret")
        payload = custom_models.public_payload(row)
        self.assertTrue(payload["has_api_key"])
        self.assertNotIn("api_key", payload)
        self.assertNotIn("api_key_enc", payload)
        self.assertEqual(payload["base_url"], "https://example.com/v1")

    def test_models_are_scoped_to_their_owner(self):
        row = custom_models.create(self.con, 1, "A", "a", "http://127.0.0.1:9000/v1", "")
        key = custom_models.key_for(row["id"])
        self.assertIsNotNone(custom_models.get_owned(self.con, 1, key))
        self.assertIsNone(custom_models.get_owned(self.con, 2, key))
        self.assertFalse(custom_models.delete(self.con, 2, key))
        self.assertTrue(custom_models.delete(self.con, 1, key))
        self.assertIsNone(custom_models.get_owned(self.con, 1, key))
        self.assertIsNotNone(custom_models.get_owned(self.con, 1, key, active_only=False))

    def test_rejects_non_http_base_url(self):
        with self.assertRaisesRegex(ValueError, "http"):
            custom_models.create(self.con, 1, "A", "a", "file:///tmp/model", "")


if __name__ == "__main__":
    unittest.main()
