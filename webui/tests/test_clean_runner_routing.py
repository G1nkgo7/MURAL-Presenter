import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_versioned_clean_profiles_use_the_broker_owning_runner() -> None:
    source = (ROOT / "inference/serve_one.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    selector = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_uses_clean_runner"
    )
    namespace = {}
    exec(
        compile(ast.Module(body=[selector], type_ignores=[]), "serve_one.py", "exec"),
        namespace,
    )
    routes_clean = namespace["_uses_clean_runner"]
    assert routes_clean({"pipeline": {"skill_mode": "clean-bilingual"}})
    assert routes_clean(
        {"pipeline": {"skill_mode": "clean-bilingual-single-slide"}}
    )
    assert routes_clean(
        {"pipeline": {"skill_mode": "mural-presenter-v0.3-adaptive"}}
    )
    assert not routes_clean({"pipeline": {"skill_mode": "mural-presenter"}})
