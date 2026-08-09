import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import service_config


class ServiceConfigTests(unittest.TestCase):
    def test_deployment_environment_maps_namespaced_services(self):
        configured = {
            "SENSENOVA_IMAGE_BASE_URL": "https://images.example/v1/",
            "SENSENOVA_IMAGE_API_KEY": "image-secret",
            "SENSENOVA_IMAGE_MODEL": "image-model",
            "SENSENOVA_SEARCH_BASE_URL": "https://search.example/",
            "SENSENOVA_SEARCH_API_KEY": "search-secret",
        }
        with mock.patch.dict("os.environ", configured, clear=True):
            self.assertEqual(service_config.system_runtime_env(), {
                "OPENAI_BASE_URL": "https://images.example/v1",
                "OPENAI_API_KEY": "image-secret",
                "IMAGE_API_KEY": "image-secret",
                "IMAGE_MODEL": "image-model",
                "SERPER_BASE_URL": "https://search.example",
                "SERPER_API_KEY": "search-secret",
            })

    def test_deployment_environment_does_not_consume_ambient_generic_keys(self):
        with mock.patch.dict("os.environ", {
            "OPENAI_API_KEY": "outer-agent-key",
            "OPENAI_BASE_URL": "https://outer-agent.example/v1",
            "SERPER_API_KEY": "outer-search-key",
        }, clear=True):
            self.assertEqual(service_config.system_runtime_env(), {})

    def test_is_user_scoped_and_never_returns_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            service_config, "CONFIG_DIR", Path(tmp) / "user_configs"
        ):
            service_config.update(
                7,
                image_enabled=True,
                image_base_url="https://images.example/v1/",
                image_model="image-model",
                image_api_key="image-secret",
                clear_image_api_key=False,
                search_enabled=True,
                search_base_url="https://search.example/",
                search_api_key="search-secret",
                clear_search_api_key=False,
            )

            public = service_config.public_payload(7)
            self.assertTrue(public["image_generation"]["has_api_key"])
            self.assertTrue(public["web_search"]["has_api_key"])
            self.assertNotIn("api_key", public["image_generation"])
            raw = (service_config.CONFIG_DIR / "7.json").read_text(encoding="utf-8")
            self.assertNotIn("image-secret", raw)
            self.assertNotIn("search-secret", raw)
            self.assertFalse(service_config.public_payload(8)["image_generation"]["enabled"])

            env = service_config.runtime_env(7)
            self.assertEqual(env["OPENAI_BASE_URL"], "https://images.example/v1")
            self.assertEqual(env["IMAGE_API_KEY"], "image-secret")
            self.assertEqual(env["SERPER_BASE_URL"], "https://search.example")
            self.assertEqual(env["SERPER_API_KEY"], "search-secret")
            self.assertEqual(env["STUDIO_AGENT_MAX_TOKENS"], "40960")
            self.assertEqual(env["STUDIO_MAX_TURNS"], "4096")
            self.assertEqual(env["CLEAN_MAX_TURNS"], "4096")
            self.assertEqual(env["CLEAN_CHILD_MAX_TURNS"], "200")
            self.assertEqual(env["SUBAGENT_MAX_TURNS"], "200")

    def test_generation_limits_are_validated_and_exported(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            service_config, "CONFIG_DIR", Path(tmp) / "user_configs"
        ):
            args = dict(
                image_enabled=False, image_base_url="", image_model="",
                image_api_key=None, clear_image_api_key=False,
                search_enabled=False, search_base_url="", search_api_key=None,
                clear_search_api_key=False,
            )
            service_config.update(
                9, max_tokens=32768, static_max_turns=640,
                static_subagent_max_turns=73, dynamic_max_turns=2048, **args,
            )
            self.assertEqual(service_config.generation_limits(9), {
                "max_tokens": 32768,
                "static_max_turns": 640,
                "static_subagent_max_turns": 73,
                "dynamic_max_turns": 2048,
            })
            env = service_config.runtime_env(9)
            self.assertEqual(env["MAX_TOKENS"], "32768")
            self.assertEqual(env["CLEAN_MAX_TURNS"], "640")
            self.assertEqual(env["CLEAN_CHILD_MAX_TURNS"], "73")
            self.assertEqual(env["SUBAGENT_MAX_TURNS"], "73")
            with self.assertRaises(ValueError):
                service_config.update(9, max_tokens=1000, **args)

    def test_blank_key_keeps_existing_and_clear_removes_it(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            service_config, "CONFIG_DIR", Path(tmp) / "user_configs"
        ):
            args = dict(
                user_id=3,
                image_enabled=True,
                image_base_url="http://image.local/v1",
                image_model="img",
                search_enabled=False,
                search_base_url="",
                search_api_key=None,
                clear_search_api_key=False,
            )
            service_config.update(image_api_key="first", clear_image_api_key=False, **args)
            service_config.update(image_api_key=None, clear_image_api_key=False, **args)
            self.assertEqual(service_config.runtime_env(3)["IMAGE_API_KEY"], "first")
            service_config.update(image_api_key=None, clear_image_api_key=True, **args)
            self.assertEqual(service_config.runtime_env(3)["IMAGE_API_KEY"], "")

    def test_file_is_one_versioned_document(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            service_config, "CONFIG_DIR", Path(tmp) / "user_configs"
        ):
            service_config.update(
                2,
                image_enabled=False,
                image_base_url="",
                image_model="",
                image_api_key=None,
                clear_image_api_key=False,
                search_enabled=False,
                search_base_url="",
                search_api_key=None,
                clear_search_api_key=False,
            )
            parsed = json.loads(
                (service_config.CONFIG_DIR / "2.json").read_text(encoding="utf-8")
            )
            self.assertEqual(parsed["version"], 2)
            self.assertEqual(
                set(parsed), {"version", "image_generation", "web_search", "generation"}
            )


if __name__ == "__main__":
    unittest.main()
