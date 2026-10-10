"""校验论文源码身份与便携部署集成，不启动模型或读取运行数据。"""

import hashlib
import json
from pathlib import Path
import runpy
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ASSET_ROOTS = ("assets/logo/exports/", "assets/figures/")
README_ASSETS = {
    "assets/logo/exports/mural-mark-256.png",
    "assets/figures/cross-page-dependencies.png",
    "assets/figures/roles-and-tools.png",
    "assets/figures/ownership-workflow.png",
}


def check_core(root: Path = ROOT) -> dict:
    provenance = root / "configs/releases/upstream-0919"
    pin = json.loads((provenance / "pin.json").read_text())
    publication_bytes = (provenance / "publication.json").read_bytes()
    assert hashlib.sha256(publication_bytes).hexdigest() == pin["manifest_sha256"]
    publication = json.loads(publication_bytes)
    count = 0
    for record in publication["inventory"]:
        if record["path"].startswith(("skills/", "harnesses/")):
            path = root / record["path"]
            assert path.is_file(), f"Missing frozen source: {record['path']}"
            assert hashlib.sha256(path.read_bytes()).hexdigest() == record["sha256"], (
                f"Frozen source changed: {record['path']}"
            )
            count += 1
    guard = runpy.run_path(str(root / "harnesses/mural-presenter-v1/runtime/release_guard.py"))
    actual = guard["release_tree_digest"](
        (root / "skills/mural-presenter-v1", root / "harnesses/mural-presenter-v1"),
        relative_to=root,
    )
    assert actual == pin["pair_tree_sha256"], "Frozen pair contains changed or extra source files"
    return {"source_files": count, "pair_tree_sha256": actual, "guard": guard}


def main() -> None:
    core = check_core()
    report = core["guard"]["verify_runtime_release"]("webui", repository_root=ROOT)
    assert not report["mismatches"], "Release verification must not rely on a dirty override"
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    forbidden = ("site/", ".snapshots/", ".secrets/", ".runtime/", "archive/", "paper/",
                 "research/", "mainline/", "benchmark/", "docs/", "examples/",
                 "train/", "data/", "artifacts/", "configs/experiments/", "webui/docs/")
    for name in filter(None, tracked):
        parts = Path(name).parts
        assert not name.startswith(forbidden), f"Local-only path is tracked: {name}"
        if name.startswith("assets/"):
            assert name.startswith(PUBLIC_ASSET_ROOTS), f"Design draft is tracked: {name}"
        assert ".git" not in parts and ".venv" not in parts and "node_modules" not in parts
        assert not (Path(name).name.startswith(".env") and Path(name).name != ".env.example"), name
    assert README_ASSETS <= set(tracked), "README logos or method diagrams are not tracked"
    release = json.loads((ROOT / "configs/releases/mural-paper-v1.json").read_text())
    missing = set(release["integration"]["files"]) - set(tracked)
    assert not missing, f"Release depends on untracked files: {sorted(missing)}"
    print(json.dumps({"release": report["release"], "frozen_core_files": core["source_files"],
                      "pair_tree_sha256": core["pair_tree_sha256"], "integration": "verified"}, indent=2))


if __name__ == "__main__":
    main()
