from __future__ import annotations

from pathlib import Path
import os
import sys
import json

import pytest


HARNESS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARNESS))


def pytest_runtest_setup(item):
    from geometry_fixture import check_cached_browser_failure
    check_cached_browser_failure()


@pytest.fixture(scope="session", autouse=True)
def isolated_shared_objects(tmp_path_factory):
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("MURAL_STUDIO_DATA_DIR", str(tmp_path_factory.mktemp("shared-data")))
        config = json.loads((HARNESS.parents[1] / "configs/runtime.json").read_text())
        config["resources"]["directory"] = str(tmp_path_factory.mktemp("resource-slots"))
        config["mineru"]["endpoints"] = ["http://127.0.0.1:9", "http://127.0.0.1:10"]
        config_path = tmp_path_factory.mktemp("config") / "runtime.json"
        config_path.write_text(json.dumps(config))
        patch.setenv("MURAL_RUNTIME_CONFIG", str(config_path))
        patch.delenv("MURAL_RENDER_BROWSER_ENDPOINT", raising=False)
        from state import workspace as workspace_module
        from geometry_fixture import require_browser

        load_module = workspace_module.workspace_skill_module
        guarded_modules = set()

        def load_checked_module(root, name):
            module = load_module(root, name)
            if name == "geometry" and callable(getattr(module, "_read_css", None)) and id(module) not in guarded_modules:
                read_css = module._read_css

                def checked_read_css(root, css):
                    require_browser(tmp_path_factory.getbasetemp())
                    return read_css(root, css)

                patch.setattr(module, "_read_css", checked_read_css)
                guarded_modules.add(id(module))
            return module

        patch.setattr(workspace_module, "workspace_skill_module", load_checked_module)
        yield
