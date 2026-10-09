import tempfile
import unittest
from pathlib import Path
from unittest import mock

from studio.app import custom_models, service_config


class ServiceConfigProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.provider_env = root / "providers.env"
        self.provider_env.write_text("")
        self.patches = [
            mock.patch.object(service_config, "PROVIDER_ENV_PATH", self.provider_env),
            mock.patch.object(service_config, "CONFIG_DIR", root / "configs"),
            mock.patch.object(custom_models, "_KEY_FILE", root / ".key"),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temp.cleanup()

    def test_sensenova_provider_persists_and_reaches_job_env(self):
        service_config.update(
            7,
            image_enabled=True,
            image_provider="sensenova_u1",
            image_base_url="https://token.sensenova.cn/v1/",
            image_model="u1-model",
            image_api_key="secret",
            clear_image_api_key=False,
            search_enabled=False,
            search_base_url="",
            search_api_key=None,
            clear_search_api_key=False,
        )
        public = service_config.public_payload(7)["image_generation"]
        self.assertEqual(public["provider"], "sensenova_u1")
        self.assertTrue(public["has_api_key"])
        env = service_config.runtime_env(7)
        self.assertEqual(env["IMAGE_PROVIDER"], "sensenova_u1")
        self.assertEqual(env["IMAGE_BASE_URL"], "https://token.sensenova.cn/v1")
        self.assertEqual(env["IMAGE_MODEL"], "u1-model")
        self.assertEqual(env["IMAGE_API_KEY"], "secret")
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertNotIn("OPENAI_API_KEY", env)

    def test_deployment_image_config_does_not_override_main_model_env(self):
        self.provider_env.write_text(
            "IMAGE_PROVIDER=openai_images\nIMAGE_BASE_URL=https://image.example/v1/\n"
            "IMAGE_API_KEY=deployment-secret\nIMAGE_MODEL=gpt-image-2-pro-all\n"
        )
        env = service_config.system_runtime_env()
        self.assertEqual(env["IMAGE_PROVIDER"], "openai_images")
        self.assertEqual(env["IMAGE_BASE_URL"], "https://image.example/v1")
        self.assertEqual(env["IMAGE_API_KEY"], "deployment-secret")
        self.assertEqual(env["IMAGE_MODEL"], "gpt-image-2-pro-all")
        self.assertNotIn("OPENAI_BASE_URL", env)
        self.assertNotIn("OPENAI_API_KEY", env)
        public = service_config.public_payload(7)["image_generation"]
        self.assertTrue(public["available"])
        self.assertEqual(public["source"], "deployment")

    def test_deployment_services_use_the_same_file_without_alias_pointers(self):
        self.provider_env.write_text(
            "IMAGE_API_KEY=shared-image\nIMAGE_BASE_URL=https://image.example/v1\n"
            "IMAGE_MODEL=gpt-image-2-pro-all\nSERPER_API_KEY=shared-search\n"
            "SERPER_BASE_URL=https://search.example\n",
            encoding="utf-8",
        )
        with mock.patch.dict(
            "os.environ",
            {
                "SENSENOVA_IMAGE_BASE_URL": "https://image.example/v1",
                "SENSENOVA_IMAGE_MODEL": "image-model",
                "SENSENOVA_IMAGE_API_KEY_ENV": "TOKENHUB_IMAGE_KEY",
                "SENSENOVA_SEARCH_BASE_URL": "https://search.example",
                "SENSENOVA_SEARCH_API_KEY_ENV": "SERPER_API_KEY",
            },
            clear=False,
        ):
            env = service_config.system_runtime_env()
        self.assertEqual(env["IMAGE_API_KEY"], "shared-image")
        self.assertEqual(env["SERPER_API_KEY"], "shared-search")
        self.assertEqual(env["IMAGE_MODEL"], "gpt-image-2-pro-all")

    def test_unknown_provider_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "不支持的生图服务类型"):
            service_config.update(
                7,
                image_enabled=False,
                image_provider="unknown",
                image_base_url="",
                image_model="",
                image_api_key=None,
                clear_image_api_key=False,
                search_enabled=False,
                search_base_url="",
                search_api_key=None,
                clear_search_api_key=False,
            )

    def test_provider_environment_does_not_reintroduce_mineru_configuration(self):
        value = "http://gpu.example:8200,http://127.0.0.1:8201"
        self.provider_env.write_text("MURAL_MINERU_ENDPOINTS=" + value + "\n")
        with mock.patch.dict("os.environ", {"MURAL_MINERU_ENDPOINTS": "http://untrusted.example"}):
            env = service_config.system_runtime_env()
        self.assertEqual([key for key in env if key.startswith("MURAL_MINERU_")], [])

    def test_runtime_limits_use_harness_reader_and_ignore_old_environment_overrides(self):
        config = service_config.load_runtime_config({})
        pointer = Path(self.temp.name) / "runtime.json"
        config["decks"] = {"total": 5, "per_model": 2, "owners": 7}
        import json
        pointer.write_text(json.dumps(config))
        actual = service_config.load_runtime_config({"MURAL_RUNTIME_CONFIG": str(pointer),
            "STUDIO_MAX_PER_MODEL": "99", "STUDIO_MAX_CONCURRENT": "99",
            "MURAL_MINERU_ENDPOINTS": "http://ignored.example"})
        self.assertEqual(actual["decks"], config["decks"])
        self.assertEqual(actual["mineru"], config["mineru"])
        self.assertIs(service_config.load_runtime_config, service_config._runtime_config.load_runtime_config)

    def test_launch_check_uses_canonical_provider_file_without_private_values(self):
        from contextlib import redirect_stdout
        import io
        import json
        import runpy
        import sys

        self.provider_env.write_text("IMAGE_BASE_URL=https://image.example/v1\nIMAGE_API_KEY=canonical-image-secret\n"
            "IMAGE_MODEL=gpt-image-2-pro-all\nSERPER_BASE_URL=https://search.example\nSERPER_API_KEY=canonical-search-secret\n")
        launch_env = self.provider_env.with_name("launch.env")
        launch_env.write_text("")
        launch = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/launch.py"))
        output = io.StringIO()
        with mock.patch.dict("os.environ", {"SENSENOVA_IMAGE_BASE_URL": "https://retired.example",
                "SENSENOVA_IMAGE_API_KEY": "retired-image-secret"}, clear=True), \
                mock.patch.object(sys, "path", list(sys.path)), \
                mock.patch.object(sys, "argv", ["launch.py", "--check", "--no-install", "--no-browser-install", "--env-file", str(launch_env)]), \
                mock.patch.dict(launch["main"].__globals__, {"_verify_mural_release": lambda: {
                    "release": "test-release", "source_commit": "test-commit", "dirty_override": False},
                    "_find_browser": lambda _root: None}), redirect_stdout(output):
            self.assertEqual(launch["main"](), 0)
        public = json.loads(output.getvalue())["external_services"]
        self.assertEqual(public["provider_environment_file"], str(self.provider_env))
        self.assertEqual(public["image_model"], "gpt-image-2-pro-all")
        self.assertEqual(public["image_url"], "https://image.example/v1")
        self.assertEqual(public["search_url"], "https://search.example")
        for hidden in ("canonical-image-secret", "canonical-search-secret", "retired.example", "retired-image-secret"):
            self.assertNotIn(hidden, output.getvalue())


if __name__ == "__main__":
    unittest.main()
