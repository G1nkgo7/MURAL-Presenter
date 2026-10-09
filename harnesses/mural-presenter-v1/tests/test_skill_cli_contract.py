"""脚本与 Harness 使用同一纯语法；workspace 位置由 argparse 决定。"""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1/scripts"


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("skill_cli_contract", SCRIPTS / "_internal/cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("role, action, flags", [
    ("image", "register", ["--id", "hero", "--path", "assets/hero.png", "--kind", "generated"]),
    ("image", "crop", ["--source", "inputs/page.png", "--output", "assets/hero.png", "--box", "0,0,1,1"]),
    ("orchestrator", "inspect-plans", ["--expected", "3"]),
    ("orchestrator", "prepare-revision", ["--operations", "_state/edit operations.json"]),
    ("slide", "render", ["--page", "2"]),
    ("slide", "render-group", ["--group", "g1", "--pages", "2,3"]),
])
def test_workspace_is_a_positional_value_not_a_fixed_argv_slot(cli, role, action, flags):
    before = cli.parse_args(role, [action, "workspace with spaces", *flags])
    after = cli.parse_args(role, [action, *flags, "workspace with spaces"])
    between = cli.parse_args(role, [action, *flags[:2], "workspace with spaces", *flags[2:]])
    assert vars(before) == vars(after) == vars(between)
    assert before.root == "workspace with spaces"


def test_parser_preserves_literal_text_and_leaves_root_authority_to_harness(cli):
    notes = "$(not a command); `literal`\nquoted details | more"
    args = cli.parse_args("image", ["register", "--id", "hero", "--path", "assets/hero.png",
                                  "--kind", "generated", "--notes", notes, "../other-workspace"])
    assert args.notes == notes and args.root == "../other-workspace"


@pytest.mark.parametrize("role", ["image", "orchestrator", "slide"])
def test_help_needs_only_standard_library_and_creates_no_workspace(role, tmp_path):
    before = tuple(tmp_path.iterdir())
    result = subprocess.run([sys.executable, "-S", str(SCRIPTS / f"{role}.py"), "--help"],
                            cwd=tmp_path, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout and result.stderr == ""
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize("role, args", [
    ("orchestrator", ["render", ".", "--page", "1"]),
    ("slide", ["finalize", "."]),
    ("image", ["parse", "."]),
    ("image", ["register", ".", "--id", "hero"]),
    ("image", ["register", ".", "--id", "hero", "--path", "assets/h.png", "--kind", "generated", "--slides", "0,2"]),
    ("image", ["register", ".", "--id", "hero", "--path", "assets/h.png", "--kind", "generated", "--slides", ""]),
])
def test_role_actions_and_required_flags_have_one_contract(cli, role, args):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(role, args)
    assert exc.value.code == 2


def test_material_owner_has_no_parser(cli):
    with pytest.raises(ValueError, match="unsupported script role"):
        cli.build_parser("material")
