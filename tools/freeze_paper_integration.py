"""仅冻结部署集成；核心源码必须先通过原实验清单校验。"""

import json
from pathlib import Path
import subprocess

from check_paper_release import check_core

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    core = check_core()
    original = json.loads((ROOT / "configs/releases/upstream-0919/release.json").read_text())
    release = json.loads(json.dumps(original))
    files = set(original["integration"]["files"])
    files = {name for name in files if not name.startswith("configs/data_pipeline/")}
    files.update(str(path.relative_to(ROOT)) for path in (ROOT / "webui/studio/app").glob("*.py"))
    for directory in ("webui/studio/static", "webui/studio/templates"):
        files.update(str(path.relative_to(ROOT)) for path in (ROOT / directory).rglob("*") if path.is_file())
    files.update({"webui/.env.example", "webui/Dockerfile", "webui/compose.yaml",
                  "webui/start.sh", "webui/start.ps1", "webui/RELEASE_MANIFEST.json"})
    files.update({"scripts/runtime_environment.py", "scripts/run_inference.py",
                  "tools/check_paper_release.py", "tools/freeze_paper_integration.py"})
    release["release"] = "mural-paper-0919-portable-v1"
    release["channel"] = "paper-release-candidate"
    release["git"] = {
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "worktree_state": "Portable integration snapshot; original frozen core verified independently by publication inventory."
    }
    release["integration"]["files"] = sorted(files)
    release["integration"]["tree_sha256"] = core["guard"]["integration_digest"](ROOT, sorted(files))
    release["lineage"]["upstream_release"] = original["release"]
    release["lineage"]["note"] = "Skill/Harness unchanged; portable launch configuration and matching WebUI integrated."
    release["validation"] = {
        "scope": "Original core inventory and current integration digests verified. Test results are documented separately.",
        "model_requests": "not_run",
    }
    (ROOT / "configs/releases/mural-paper-v1.json").write_text(json.dumps(release, indent=2) + "\n")
    print(release["release"], release["integration"]["tree_sha256"])


if __name__ == "__main__":
    main()
