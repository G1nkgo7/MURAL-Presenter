"""通过真实素材登记验证参数保真；不经过 shell，不把备注标点当控制语法。"""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shlex
import sys
import threading
from types import SimpleNamespace

from PIL import Image
import pytest

from runtime.production import ProductionToolServices, load_skill_module
from runtime.tools import ToolContext, ToolContractError, WriteScope
from state.artifacts import ArtifactStore
from state.workspace import WorkspaceState


@pytest.fixture
def registration(tmp_path):
    skill = Path(__file__).resolve().parents[3] / "skills/mural-presenter-v1"
    root = tmp_path / "deck"
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = WorkspaceState(root)
    services.artifacts = ArtifactStore(root, workspace_state=services.workspace)
    services.settings = SimpleNamespace(command_timeout_s=30)
    services.snapshot = SimpleNamespace(name=skill.name, target=skill, tree_sha256="test-literal-arguments",
        resolve_skill_path=lambda logical: skill / Path(logical).relative_to(f"skills/{skill.name}"),
        capture_runtime_fonts=lambda: None)
    services._command_environment = lambda: {
        **os.environ, "TMPDIR": str(root / "_runtime/tmp"), "PYTHONDONTWRITEBYTECODE": "1"}
    (root / "base.css").write_text(":root {--canvas-w:1600px;--canvas-h:900px}")
    Image.new("RGB", (32, 24), "navy").save(root / "assets/hero.png")
    context = ToolContext("image-owner", "image", read_scopes=(WriteScope("inputs", recursive=True),),
        write_scopes=(WriteScope("assets", recursive=True),))
    prefix = f"python skills/{skill.name}/scripts/image.py"
    return services, context, prefix


def _command(prefix):
    return prefix + " register . --id hero --path assets/hero.png --kind generated --slides 1"


@pytest.mark.parametrize("action", ["audit", "scaffold", "sync-speech", "finalize"])
@pytest.mark.parametrize("suffix", [".", "--help"])
def test_agent_deck_lifecycle_commands_are_internal(registration, monkeypatch, action, suffix):
    services, _context, _prefix = registration
    context = ToolContext("orchestrator", "orchestrator")
    prefix = f"python skills/{services.snapshot.name}/scripts/orchestrator.py"

    def unexpected_execution(*_args, **_kwargs):
        pytest.fail("Agent lifecycle commands must not inspect internal state or spawn a command")

    monkeypatch.setattr("runtime.production._run_command", unexpected_execution)
    with pytest.raises(ToolContractError) as error:
        services.terminal(context, {"command": f"{prefix} {action} {suffix}"})
    assert error.value.code == "terminal_role_denied"


def test_agent_orchestrator_help_exposes_only_authoring_commands(registration, monkeypatch):
    services, _context, _prefix = registration
    prefix = f"python skills/{services.snapshot.name}/scripts/orchestrator.py"

    def unexpected_execution(*_args, **_kwargs):
        pytest.fail("Agent help must use the authoring parser, not the internal CLI")

    monkeypatch.setattr("runtime.production._run_command", unexpected_execution)
    result = services.terminal(ToolContext("orchestrator", "orchestrator"), {"command": prefix + " --help"})
    assert result["returncode"] == 0
    for action in ("audit", "scaffold", "sync-speech", "finalize"):
        assert action not in result["stdout"]
    assert "inspect-plans" in result["stdout"] and "prepare-revision" in result["stdout"]


def test_agent_invalid_command_feedback_does_not_reintroduce_lifecycle(registration):
    services, _context, _prefix = registration
    prefix = f"python skills/{services.snapshot.name}/scripts/orchestrator.py"
    with pytest.raises(ToolContractError) as error:
        services._validated_command("orchestrator", prefix + " not-a-command .")
    assert error.value.code == "terminal_argument_invalid"
    for action in ("audit", "scaffold", "sync-speech", "finalize"):
        assert action not in str(error.value)


def test_internal_cli_and_image_finalize_remain_available(registration):
    services, _context, prefix = registration
    cli = load_skill_module(services.snapshot.target, services.snapshot.tree_sha256, "cli")
    for action in ("audit", "scaffold", "sync-speech", "finalize"):
        assert cli.build_parser("orchestrator").parse_args([action, "."]).command == action
    _argv, role, action, request = services._validated_command("orchestrator", prefix + " finalize .")
    assert role == "image" and action == "finalize" and request.root == "."


@pytest.fixture
def scoped_input_images(registration):
    services, context, prefix = registration
    sources = ("inputs/originals/att-012345abcdef/a.png", "inputs/originals/att-fedcba987654/b.png")
    for source, color in zip(sources, ("navy", "orange")):
        path = services.workspace.paths.root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), color).save(path)
    scoped = ToolContext(context.owner_id, context.role,
        read_scopes=(WriteScope(sources[0], recursive=False),), write_scopes=context.write_scopes)
    return services, scoped, prefix, sources


@pytest.mark.parametrize("action,options", [
    ("register-user", ""),
    ("crop", " --box 0.1,0.1,0.9,0.9"),
    ("crop-material", " --box 0.1,0.1,0.9,0.9"),
    ("extract-subject", " --box 0.1,0.1,0.9,0.9"),
])
def test_image_input_commands_reject_unreadable_attachment_before_dispatch(scoped_input_images, monkeypatch, action, options):
    services, context, prefix, (allowed, denied) = scoped_input_images
    root = services.workspace.paths.root
    output = root / "assets/forbidden-copy.png"
    before = (root / denied).read_bytes()
    assert context.may_read(allowed) and not context.may_read(denied)

    def unexpected_access(*_args, **_kwargs):
        pytest.fail("unreadable attachment must be rejected before materialization or subprocess execution")

    monkeypatch.setattr("runtime.production._run_command", unexpected_access)
    monkeypatch.setattr("runtime.production.materialize_page_image", unexpected_access)
    with pytest.raises(ToolContractError) as error:
        services.terminal(context, {"command": f"{prefix} {action} . --source {denied} --output assets/forbidden-copy.png{options}"})
    assert error.value.code == "read_scope_denied"
    assert not output.exists() and not (root / "_trace/user-images.json").exists()
    assert (root / denied).read_bytes() == before


def test_register_user_executes_for_authorized_ordinary_image(scoped_input_images):
    services, context, prefix, (allowed, denied) = scoped_input_images
    root = services.workspace.paths.root
    before = {source: (root / source).read_bytes() for source in (allowed, denied)}
    result = services.terminal(context, {
        "command": f"{prefix} register-user . --source {allowed} --output assets/authorized-copy.png"})
    assert result["returncode"] == 0
    with Image.open(root / "assets/authorized-copy.png") as output, Image.open(root / allowed) as source:
        assert output.size == source.size == (64, 48)
        assert output.convert("RGB").tobytes() == source.convert("RGB").tobytes()
    assert {source: (root / source).read_bytes() for source in before} == before


@pytest.mark.parametrize("argument,expected", [
    ("'描述 A;描述 B；\nA|B && C'", "描述 A;描述 B；\nA|B && C"),
    ('"描述 A;描述 B；\nA|B && C"', "描述 A;描述 B；\nA|B && C"),
    (r"A\;B\|C\&\&D", "A;B|C&&D"),
    ("A;B；C", "A;B；C"),
])
def test_real_register_preserves_quoted_escaped_and_unicode_notes(registration, argument, expected):
    services, context, prefix = registration
    result = services.terminal(context, {"command": _command(prefix) + " --notes " + argument})
    assert result["returncode"] == 0
    root = services.workspace.paths.root
    reloaded = ArtifactStore(root, workspace_state=services.workspace)
    image = reloaded.catalog()["images"][0]
    assert image["image_metadata"]["notes"] == expected
    assert (image["pixel_width"], image["pixel_height"]) == (32, 24)
    assert image["sha256"]
    assert not (root / "canvas.json").exists()


def test_real_register_and_finalize_keep_shell_like_text_literal(registration):
    services, context, prefix = registration
    root = services.workspace.paths.root
    marker = root / "must-not-be-created"
    notes = f"Owner's notes; 中文；\n$(touch {marker}) `touch {marker}` | && > &"
    services.terminal(context, {"command": _command(prefix) + " --notes " + shlex.quote(notes)})
    before = services.artifacts.path.read_bytes()
    services.terminal(context, {"command": prefix + " finalize ."})
    assert services.artifacts.path.read_bytes() == before
    assert services.artifacts.catalog()["images"][0]["image_metadata"]["notes"] == notes
    assert "- notes: " + notes.replace("\n", "\n  ") in (root / "assets/catalog.md").read_text()
    assert not marker.exists()


@pytest.mark.parametrize("operator", [";", "|", "&&", "&", ">", "\n"])
def test_actual_cli_rejects_extra_shell_arguments_without_executing_them(registration, operator):
    services, context, prefix = registration
    root = services.workspace.paths.root
    marker = root / "must-not-be-created"
    suffix = f" {operator} {shlex.quote(str(marker))}" if operator == ">" else f" {operator} touch {shlex.quote(str(marker))}"
    with pytest.raises(ToolContractError) as error:
        services.terminal(context, {"command": _command(prefix) + " --notes A" + suffix})
    assert error.value.code == "terminal_argument_invalid"
    assert "unrecognized arguments" in str(error.value)
    assert not marker.exists()
    assert services.artifacts.catalog()["images"] == []


@pytest.mark.parametrize("template,code", [
    ("bash -c '{command}'", "terminal_interpreter_denied"),
    ("sh -c '{command}'", "terminal_interpreter_denied"),
    ("python -c 'print(1)'", "terminal_role_denied"),
    ("python skills/foreign/scripts/image.py register .", "terminal_role_denied"),
    ("{prefix} render .", "terminal_argument_invalid"),
    ("{prefix} finalize /outside", "terminal_root_denied"),
    ("{prefix} register --id hero .", "terminal_argument_invalid"),
    ("{command} --notes 'unterminated", "terminal_parse_failed"),
])
def test_wrappers_foreign_paths_and_malformed_commands_never_dispatch(registration, monkeypatch, template, code):
    services, context, prefix = registration
    def unexpected_execution(*args, **kwargs):
        pytest.fail("invalid interpreter/Skill/action/workspace must not execute")
    monkeypatch.setattr("runtime.production._run_command", unexpected_execution)
    with pytest.raises(ToolContractError) as error:
        services.terminal(context, {"command": template.format(prefix=prefix, command=_command(prefix))})
    assert error.value.code == code


def test_concurrent_cli_parsers_preserve_process_stream_identity(registration, monkeypatch):
    services, _context, prefix = registration
    cli = load_skill_module(services.snapshot.target, services.snapshot.tree_sha256, "cli")
    streams = (sys.stdout, sys.stderr)
    first_entered, second_entered, first_exited = (threading.Event() for _ in range(3))
    observed = {}

    def build_parser(role):
        parser = cli.build_parser(role)
        parse = parser.parse_args

        def parse_interleaved(argv):
            name = argv[-1]
            observed[name] = (sys.stdout, sys.stderr)
            if name == "first":
                first_entered.set()
                assert second_entered.wait(3)
            else:
                second_entered.set()
                assert first_exited.wait(3)
            return parse(argv)

        parser.parse_args = parse_interleaved
        return parser

    monkeypatch.setattr("runtime.production.load_skill_module", lambda *_: SimpleNamespace(build_parser=build_parser))

    def validate(name):
        try:
            return services._validated_command("image", _command(prefix) + f" --notes {name}")
        finally:
            if name == "first":
                first_exited.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(validate, "first")
            assert first_entered.wait(2)
            second = pool.submit(validate, "second")
            assert first.result(timeout=4)[3].notes == "first"
            assert second.result(timeout=4)[3].notes == "second"
        assert sys.stdout is streams[0] and sys.stderr is streams[1]
        assert all(stdout is streams[0] and stderr is streams[1] for stdout, stderr in observed.values())
    finally:
        sys.stdout, sys.stderr = streams
