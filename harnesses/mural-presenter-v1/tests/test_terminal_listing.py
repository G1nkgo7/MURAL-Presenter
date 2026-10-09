"""受限目录/文本查询走真实工具事务，任何路径都不交给 shell。"""
import json
import errno
import os
import shlex
from types import SimpleNamespace

import pytest

from runtime.production import ProductionToolServices, read_scopes_for_role, ROLE_SCRIPT_ENTRIES
from runtime.tools import ToolCall, ToolContext, ToolContractError, ToolRuntime, WriteScope, register_tools
from state.workspace import WorkspaceState


@pytest.fixture
def listing(tmp_path, monkeypatch):
    services = ProductionToolServices.__new__(ProductionToolServices)
    services.workspace = WorkspaceState(tmp_path / "deck")
    skill = tmp_path / "pinned-skill"
    (skill / "references/style").mkdir(parents=True)
    (skill / "roles").mkdir()
    (skill / "scripts/_internal").mkdir(parents=True)
    for name in ("SKILL.md", "references/style-index.md", "references/style/editorial.md",
                 "roles/slide.md", "roles/image.md", "scripts/slide.py", "scripts/orchestrator.py",
                 "scripts/image.py", "scripts/_internal/private.py"):
        (skill / name).write_text("fixed Skill reference", encoding="utf-8")
    prefix = "skills/mural-presenter-v1"
    services.snapshot = SimpleNamespace(name="mural-presenter-v1", target=skill,
        resolve_skill_path=lambda path: skill / path.removeprefix(prefix + "/"))
    root = services.workspace.paths.root
    for name in ("base.css", "deck.md", "knowledge.md", "assets/hero.png", "assets/.hidden.png",
                 "slides/slide_01.html", "slides/slide_02.html", "renders/slide_01.png",
                 "_state/page-index.json", "_state/private.json", "_trace/secret.txt", ".env"):
        (root / name).write_text("test content", encoding="utf-8")

    def never_spawn(*_args, **_kwargs):
        pytest.fail("read-only queries must not execute a subprocess or load a Skill command parser")

    monkeypatch.setattr("runtime.production._run_command", never_spawn)
    monkeypatch.setattr("runtime.production.load_skill_module", never_spawn)
    runtime = ToolRuntime()
    register_tools(runtime, {"terminal": services.terminal}, require_complete=False)

    def call(command, role="orchestrator", scopes=None, tool_use_id=None):
        context = ToolContext("test-owner", role,
            read_scopes=read_scopes_for_role(role, skill_name=services.snapshot.name, pages=[1])
            if scopes is None else scopes)
        return runtime.execute(context, ToolCall(tool_use_id or f"ls-{len(runtime.events)}", "terminal", {"command": command}))

    return services, runtime, call


def output(result):
    assert result.ok, result.content
    return json.loads(result.content)


def paths(result):
    return {json.loads(line.split("\t")[-1]) for line in output(result)["stdout"].splitlines()}


@pytest.mark.parametrize("command", ["ls", "ls .", "ls -la", "ls -al .", "ls -a -l -- ./"])
def test_root_listing_filters_hidden_and_internal_paths(listing, command):
    _, _, call = listing
    names = paths(call(command))
    assert {"assets/", "slides/", "base.css", "deck.md", "skills/"} <= names
    assert not {".env", "_runtime/", "inputs/", "_state/", "_trace/"} & names
    assert not output(call(command))["truncated"]


@pytest.mark.parametrize("role", ["orchestrator", "image", "slide"])
def test_authorized_assets_and_long_metadata(listing, role):
    services, _, call = listing
    assert paths(call("ls assets/", role)) == {"assets/hero.png"}
    result = output(call("ls -la assets", role))
    assert paths(call("ls -a assets", role)) == {"assets/hero.png", "assets/.hidden.png"}
    assert f"file\t{(services.workspace.paths.root / 'assets/hero.png').stat().st_size}\t" in result["stdout"]
    assert "Z\t" in result["stdout"]


def test_single_owner_can_navigate_but_not_discover_sibling_pages(listing):
    _, _, call = listing
    assert paths(call("ls slides", "slide")) == {"slides/slide_01.html"}
    assert paths(call("ls renders", "slide")) == {"renders/slide_01.png"}
    assert {"deck.md", "knowledge.md"} <= paths(call("ls .", "slide"))
    for command in ("ls _state", "ls _trace", "ls slides/slide_02.html"):
        result = call(command, "slide")
        assert not result.ok and result.error_code == "read_scope_denied"


def test_read_file_cannot_open_internal_state_trace_or_owned_diagnostics(listing):
    services, _, _ = listing
    root = services.workspace.paths.root
    diagnostic = root / "_trace/tool-output" / ("a" * 64 + ".jsonl")
    diagnostic.parent.mkdir(parents=True)
    diagnostic.write_text(
        json.dumps({"record": {"owner_id": "test-owner"}}) + "\n",
        encoding="utf-8",
    )
    context = ToolContext(
        "test-owner",
        "orchestrator",
        read_scopes=read_scopes_for_role(
            "orchestrator", skill_name=services.snapshot.name, pages=[1]
        ),
    )
    for path in ("_state/private.json", "_trace/secret.txt", services.workspace.paths.relative(diagnostic)):
        with pytest.raises(ToolContractError) as error:
            services.read_file(context, {"path": path})
        assert error.value.code == "read_scope_denied"
        assert "_state/" not in str(error.value) and "_trace/" not in str(error.value)
        assert "<private runtime path>" in str(error.value)


def test_only_authorized_attachment_branches_are_discoverable(listing):
    services, _, call = listing
    root = services.workspace.paths.root
    for identity in ("allowed", "other"):
        folder = root / "inputs/originals" / identity
        folder.mkdir(parents=True)
        (folder / "file.png").write_bytes(b"test attachment")
    scopes = (WriteScope("inputs/originals/allowed/file.png", recursive=False),)
    assert paths(call("ls inputs", scopes=scopes)) == {"inputs/originals/"}
    assert paths(call("ls inputs/originals", scopes=scopes)) == {"inputs/originals/allowed/"}
    assert call("ls inputs/originals/other", scopes=scopes).error_code == "read_scope_denied"
    assert call("ls", scopes=()).error_code == "read_scope_denied"


def test_fixed_skill_namespace_is_read_only_and_does_not_create_a_directory(listing):
    services, _, call = listing
    prefix = "skills/" + services.snapshot.name
    assert paths(call("ls skills")) == {prefix + "/"}
    assert paths(call(f"ls {prefix}")) == {prefix + "/SKILL.md", prefix + "/references/", prefix + "/scripts/", prefix + "/roles/"}
    assert paths(call(f"ls {prefix}/roles", "slide")) == {prefix + "/roles/slide.md", prefix + "/roles/image.md"}
    assert paths(call(f"ls {prefix}/references/style")) == {prefix + "/references/style/editorial.md"}
    assert paths(call(f"ls {prefix}/scripts")) == {prefix + "/scripts/orchestrator.py", prefix + "/scripts/image.py"}
    assert call("ls skills/another-skill").error_code == "read_scope_denied"
    assert not (services.workspace.paths.root / "skills").exists()


@pytest.mark.parametrize("path", ["..", "assets/../../outside", "/etc", "skills/../.."])
def test_noncanonical_paths_fail_before_filesystem_access(listing, path):
    _, _, call = listing
    result = call("ls " + shlex.quote(path))
    assert not result.ok and result.error_code == "invalid_path"


def test_symlinks_are_omitted_and_never_followed_in_any_component(listing):
    services, _, call = listing
    root = services.workspace.paths.root
    (root / "assets/outside").symlink_to(root.parent, target_is_directory=True)
    (root / "assets/state").symlink_to(root / "_state", target_is_directory=True)
    (root / "assets/cycle").symlink_to("cycle")
    (root / "assets/file").symlink_to(root / ".env")
    prefix = "skills/" + services.snapshot.name
    (services.snapshot.target / "references/escape").symlink_to(root.parent, target_is_directory=True)
    assert paths(call("ls -a assets")) == {"assets/hero.png", "assets/.hidden.png"}
    assert prefix + "/references/escape/" not in paths(call(f"ls {prefix}/references"))
    for path in ("assets/outside", "assets/outside/deck", "assets/state", "assets/cycle",
                 "assets/file", f"{prefix}/references/escape/deck"):
        result = call("ls " + path)
        assert not result.ok and result.error_code == "symlink_forbidden"
        assert str(root.parent) not in result.content


def test_symlink_swap_between_parent_opens_cannot_escape(listing, monkeypatch):
    services, _, call = listing
    root = services.workspace.paths.root
    (root / "assets/nested").mkdir()
    open_file = os.open

    def swap(path, flags, *args, **kwargs):
        if path == "nested":
            (root / "assets/nested").rename(root / "assets/saved")
            (root / "assets/nested").symlink_to(root / "_state", target_is_directory=True)
        return open_file(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap)
    result = call("ls assets/nested")
    assert not result.ok and result.error_code == "symlink_forbidden"


@pytest.mark.parametrize("command", [
    "ls -R .", "ls -L assets", "ls --recursive", "ls assets; touch marker",
    "ls assets && touch marker", "ls assets | cat", "ls assets > marker", "ls\ntouch marker",
    "ls $(touch marker)", "ls `touch marker`", "bash -c 'ls -la'", "python -c 'print(1)'",
    "cat deck.md", "env ls -la", "/bin/ls -la", "ls 'unterminated",
])
def test_unsupported_flags_shells_and_combined_commands_never_execute(listing, command):
    services, _, call = listing
    result = call(command)
    assert not result.ok
    assert not (services.workspace.paths.root / "marker").exists()


def test_quoted_punctuation_is_a_literal_directory_name(listing):
    services, _, call = listing
    name = "assets/描述 A;描述 B $(touch marker)"
    folder = services.workspace.paths.root / name
    folder.mkdir()
    (folder / "file.txt").write_text("allowed", encoding="utf-8")
    assert paths(call("ls " + shlex.quote(name))) == {name + "/file.txt"}
    assert not (services.workspace.paths.root / "marker").exists()


@pytest.mark.parametrize("command,expected", [
    ("ls -al briefs 2>/dev/null; ls -al skills 2>/dev/null | head -20", ["briefs", "skills"]),
    ("ls -al assets/ 2>/dev/null; ls -al", ["assets", "."]),
    ("ls assets;ls skills|head -n 20;", ["assets", "skills"]),
    ("ls;ls", [".", "."]),
])
def test_read_only_listing_combinations_use_one_idempotent_transaction(listing, command, expected):
    services, runtime, call = listing
    (services.workspace.paths.root / "briefs").mkdir(exist_ok=True)
    result = call(command, tool_use_id="listing-combination")
    data = output(result)
    assert [row["path"] for row in data["directories"]] == expected
    assert [row["command_index"] for row in data["directories"]] == [1, 2]
    assert call(command, tool_use_id="listing-combination") == result
    assert [event["event_type"] for event in runtime.events] == ["tool_use", "tool_result"]
    runtime.assert_closed()
    assert not (services.workspace.paths.root / "skills").exists()


@pytest.mark.parametrize("suffix,count", [("head", 10), ("head -3", 3), ("head -n 3", 3), ("head -n3", 3)])
def test_head_is_per_ls_and_does_not_limit_later_command(listing, suffix, count):
    services, _, call = listing
    folder = services.workspace.paths.root / "assets/numbered"
    folder.mkdir()
    for number in range(12):
        (folder / f"{number:02d}.txt").touch()
    value = output(call(f"ls assets/numbered | {suffix}; ls skills"))
    assert [row["returned_count"] for row in value["directories"]] == [count, 1]
    assert value["directories"][0]["truncated"] and value["truncated"]
    assert not value["directories"][1]["truncated"]
    assert len(value["directories"][0]["stdout"].splitlines()) == count + 1


def test_redirected_errors_remain_structured_and_do_not_hide_permission_failures(listing):
    services, _, call = listing
    (services.workspace.paths.root / "assets/escape").symlink_to(services.workspace.paths.root / "_state", target_is_directory=True)
    result = call("ls assets/vendor 2>/dev/null; ls _runtime 2> /dev/null; ls assets/escape 2>/dev/null; ls assets")
    assert not result.ok and result.error_code == "terminal_list_partial_failure"
    data = json.loads(result.content)
    assert [row["error_code"] for row in data["directories"][:3]] == ["path_not_found", "read_scope_denied", "symlink_forbidden"]
    assert all(row["stderr_suppressed"] for row in data["directories"][:3])
    assert "read_scope_denied" not in data["stdout"]
    assert data["directories"][-1]["returncode"] == 0
    assert not (services.workspace.paths.root / "assets/vendor").exists()


def test_quoted_operator_and_redirection_text_stays_a_literal_path(listing):
    services, _, call = listing
    folder = services.workspace.paths.root / "assets/2>marker; | head -1"
    folder.mkdir()
    (folder / "file.txt").touch()
    assert paths(call("ls " + shlex.quote(folder.relative_to(services.workspace.paths.root).as_posix()))) == {
        'assets/2>marker; | head -1/file.txt'}
    assert not (services.workspace.paths.root / "marker").exists()


@pytest.mark.parametrize("command", [
    "ls assets; touch marker", "ls assets; python -c 'print(1)'", "ls assets | head -2; rm marker",
    "ls assets 2>marker; ls", "ls assets > /dev/null", "ls assets 2 > /dev/null",
    "ls assets 2>&1", "ls assets &> /dev/null", "ls assets && ls skills",
    "ls assets | cat", "ls assets | head -2 | cat", "ls assets | head -0", "ls assets | head --help",
    "ls assets;;ls skills", "ls assets 2>/dev/null$(touch marker)", "ls assets\nls skills",
])
def test_entire_listing_combination_is_validated_before_any_directory_access(listing, monkeypatch, command):
    services, _, call = listing
    monkeypatch.setattr(services, "_directory_entries", lambda *_a, **_kw: pytest.fail("invalid suffix must not partially execute"))
    result = call(command)
    assert not result.ok and result.error_code == "terminal_argument_invalid"
    assert not (services.workspace.paths.root / "marker").exists()


def test_missing_directories_and_files_are_not_successful_listings(listing):
    _, _, call = listing
    assert call("ls assets/missing").error_code == "path_not_found"
    assert call("ls assets/hero.png").error_code == "not_a_directory"


@pytest.mark.parametrize("role", ROLE_SCRIPT_ENTRIES)
def test_public_entry_is_discoverable_and_readable_without_granting_other_commands(listing, role):
    services, _, call = listing
    prefix = "skills/" + services.snapshot.name + "/scripts/"
    expected = prefix + ROLE_SCRIPT_ENTRIES[role]
    exposed = {expected, prefix + "image.py"} if role == "orchestrator" else {expected}
    assert paths(call("ls " + prefix, role)) == exposed
    context = ToolContext("owner", role, read_scopes=read_scopes_for_role(role, skill_name=services.snapshot.name))
    assert services.read_file(context, {"path": expected})["content"] == "1|fixed Skill reference"
    assert not context.may_write(expected)
    assert call("ls " + prefix + "_internal", role).error_code == "read_scope_denied"
    for other, script in ROLE_SCRIPT_ENTRIES.items():
        if prefix + script in exposed:
            continue
        assert not context.may_read(prefix + script)
        with pytest.raises(ToolContractError) as error:
            services._validated_command(role, "python " + prefix + script + " --help")
        assert error.value.code == "terminal_role_denied"


@pytest.mark.parametrize("command", ["ls assets slides", "ls -al assets/ slides/", "ls -a -l -- assets slides"])
def test_multiple_directories_share_one_transaction_and_group_output(listing, command):
    _, runtime, call = listing
    result = output(call(command))
    assert result["returncode"] == 0
    assert [row["path"] for row in result["directories"]] == ["assets", "slides"]
    assert '"assets":\n' in result["stdout"] and '"slides":\n' in result["stdout"]
    assert all(row["returncode"] == 0 for row in result["directories"])
    assert [event["event_type"] for event in runtime.events] == ["tool_use", "tool_result"]
    runtime.assert_closed()


def test_multi_directory_spaces_partial_failure_and_read_only_vendor(listing):
    services, runtime, call = listing
    root = services.workspace.paths.root
    (root / "assets/with spaces").mkdir()
    (root / "assets/with spaces/image.png").write_text("asset")
    (root / "assets/link").symlink_to(root / "_state", target_is_directory=True)
    result = call('ls -al "assets/with spaces" assets/vendor ../outside _runtime assets/link slides')
    assert not result.ok and result.error_code == "terminal_list_partial_failure"
    payload = json.loads(result.content)
    assert payload["returncode"] == 1
    entries = payload["directories"]
    assert entries[0]["returncode"] == 0 and "image.png" in entries[0]["stdout"]
    assert entries[-1]["returncode"] == 0 and "slide_01.html" in entries[-1]["stdout"]
    assert [row["error_code"] for row in entries[1:-1]] == [
        "path_not_found", "invalid_path", "read_scope_denied", "symlink_forbidden"]
    assert not (root / "assets/vendor").exists()
    assert len(runtime.events) == 2 and runtime.events[-1]["ok"] is False
    runtime.assert_closed()


@pytest.mark.parametrize("number,code", [(errno.EACCES, "directory_permission_denied"), (errno.EIO, "directory_io_error")])
def test_directory_os_error_categories(listing, monkeypatch, number, code):
    _, _, call = listing
    original = os.open
    def failing(path, *args, **kwargs):
        if path == "assets":
            raise OSError(number, os.strerror(number))
        return original(path, *args, **kwargs)
    monkeypatch.setattr(os, "open", failing)
    assert call("ls assets").error_code == code


def test_large_listing_is_bounded_and_explicitly_truncated(listing):
    services, _, call = listing
    for index in range(201):
        (services.workspace.paths.root / "assets" / f"{index:03}.txt").touch()
    result = output(call("ls assets"))
    assert result["truncated"] is True
    assert len(result["stdout"].splitlines()) == 201


def test_multiple_directories_cannot_multiply_output_limit(listing):
    services, _, call = listing
    for name in ("one", "two"):
        folder = services.workspace.paths.root / "assets" / name
        folder.mkdir()
        for index in range(150):
            (folder / f"{index:03}.txt").touch()
    result = output(call("ls assets/one assets/two assets/one"))
    assert result["returned_count"] == 200 and result["truncated"]
    assert [row["returned_count"] for row in result["directories"]] == [150, 50, 0]
    assert [row["truncated"] for row in result["directories"]] == [False, True, True]
    combined = output(call("ls assets/one | head -999; ls assets/two; ls assets/one"))
    assert combined["returned_count"] == 200 and combined["truncated"]
    assert [row["returned_count"] for row in combined["directories"]] == [150, 50, 0]


def test_listing_preserves_role_contract_and_transaction_pairing(listing):
    services, runtime, call = listing
    before = {path.relative_to(services.workspace.paths.root): path.read_bytes()
              for path in services.workspace.paths.root.rglob("*") if path.is_file()}
    first = call("ls -la", tool_use_id="same-ls")
    assert first.ok
    assert call("ls -la", tool_use_id="same-ls") == first
    assert not call("ls", "research").ok
    runtime.assert_closed()
    assert [event["event_type"] for event in runtime.events] == ["tool_use", "tool_result", "tool_use", "tool_result"]
    assert {path.relative_to(services.workspace.paths.root): path.read_bytes()
            for path in services.workspace.paths.root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("command", [
    'grep -rn -i "motion" skills/mural-presenter-v1/references/style/ | head -40',
    'grep -rniF "motion" skills/mural-presenter-v1/references/style/|head -n 40',
    'rg -ni --max-results 40 "motion" skills/mural-presenter-v1/references/style/',
])
def test_search_uses_pinned_skill_and_one_real_transaction(listing, command):
    services, runtime, call = listing
    target = services.snapshot.target / "references/style/editorial.md"
    target.write_text("before\nMotion guidance\nmore motion\nafter\n")
    before = target.read_bytes()
    first = call(command, tool_use_id="fixed-search")
    value = output(first)
    assert value["mode"] == "fixed_text" and value["search_complete"]
    assert value["max_results"] == 40 and value["returned_count"] == 2
    assert [row["line"] for row in value["matches"]] == [2, 3]
    assert "editorial.md:2:Motion guidance" in value["stdout"]
    assert call(command, tool_use_id="fixed-search") == first
    assert [event["event_type"] for event in runtime.events] == ["tool_use", "tool_result"]
    assert not (services.workspace.paths.root / "skills").exists()
    assert target.read_bytes() == before
    runtime.assert_closed()


def test_literal_search_has_no_arbitrary_pattern_length_limit(listing):
    services, runtime, call = listing
    phrase = "Long literal content " * 300
    (services.workspace.paths.root / "deck.md").write_text(phrase)
    value = output(call("rg '" + phrase + "' deck.md"))
    assert value["returned_count"] == 1
    assert value["matches"][0]["line_truncated"]
    runtime.assert_closed()


def test_search_only_reads_current_owner_pages_and_public_script(listing):
    services, _, call = listing
    root = services.workspace.paths.root
    (root / "slides/slide_01.html").write_text("motion allowed")
    (root / "slides/slide_02.html").write_text("motion PRIVATE SIBLING")
    (root / ".env").write_text("motion PRIVATE KEY")
    (root / "_trace/secret.txt").write_text("motion PRIVATE TRACE")
    result = output(call("rg -n motion .", "slide"))
    assert [row["path"] for row in result["matches"]] == ["slides/slide_01.html"]
    assert "PRIVATE" not in json.dumps(result)
    for path in ("slides/slide_02.html", ".env", "_trace/secret.txt", "skills/wrong/references"):
        assert call("rg motion " + path, "slide").error_code == "read_scope_denied"
    prefix = "skills/" + services.snapshot.name + "/scripts/"
    result = output(call("rg fixed " + prefix, "slide"))
    assert [row["path"] for row in result["matches"]] == [prefix + "slide.py"]
    assert not call("rg fixed " + prefix, "research").ok


def test_search_spaces_literal_punctuation_and_missing_paths(listing):
    services, _, call = listing
    root = services.workspace.paths.root
    path = root / "assets/with spaces.md"
    path.write_text("a.*b\n$(touch marker); echo nope\n|\n")
    assert output(call('rg -nF "a.*b" "assets/with spaces.md"'))["returned_count"] == 1
    assert output(call('rg -nF "$(touch marker); echo nope" "assets/with spaces.md"'))["returned_count"] == 1
    assert output(call('rg "|" "assets/with spaces.md"'))["returned_count"] == 1
    assert not (root / "marker").exists()
    result = call('rg -n "a.*b" "assets/with spaces.md" assets/vendor')
    assert not result.ok and result.error_code == "terminal_search_partial_failure"
    value = json.loads(result.content)
    assert value["returned_count"] == 1 and value["returncode"] == 1
    assert value["errors"][0]["error_code"] == "path_not_found"
    assert not value["search_complete"] and not (root / "assets/vendor").exists()
    denied = call('rg "a.*b" "assets/with spaces.md" .env | head -1')
    assert denied.error_code == "terminal_search_partial_failure"
    assert json.loads(denied.content)["errors"][0]["error_code"] == "read_scope_denied"


@pytest.mark.parametrize("command", [
    "grep -P motion assets", "rg --follow motion assets", "rg motion assets | cat",
    "rg motion assets | head -40 | cat", "rg motion assets && touch marker",
    "rg motion assets > marker", "rg motion assets; touch marker", "bash -c 'rg motion assets'",
    "rg --max-results 201 motion assets", "rg motion assets | head -0",
    "rg --max-results -1 motion assets", "rg motion assets | head --help",
])
def test_search_unsupported_operations_never_execute(listing, command):
    services, _, call = listing
    assert not call(command).ok
    assert not (services.workspace.paths.root / "marker").exists()


@pytest.mark.parametrize("path", ["..", "assets/../../outside", "/etc", "skills/../.."])
def test_search_rejects_path_escape(listing, path):
    _, _, call = listing
    assert call("rg motion " + shlex.quote(path)).error_code == "invalid_path"


def test_search_never_follows_file_or_directory_symlinks(listing):
    services, _, call = listing
    root = services.workspace.paths.root
    (root / "assets/linked").symlink_to(root / ".env")
    (root / "assets/dir").symlink_to(root / "_state", target_is_directory=True)
    (root / "assets/cycle").symlink_to("cycle")
    for path in ("assets/linked", "assets/dir", "assets/dir/private.json", "assets/cycle"):
        assert call("rg test " + path).error_code == "symlink_forbidden"
    result = output(call("rg test assets"))
    assert [row["path"] for row in result["matches"]] == ["assets/hero.png"]


def test_search_file_swapped_to_symlink_after_listing_cannot_escape(listing, monkeypatch):
    services, _, call = listing
    root = services.workspace.paths.root
    path = root / "assets/swap.txt"
    path.write_text("authorized")
    original = os.open
    def swapping(name, *args, **kwargs):
        if name == "swap.txt":
            path.unlink()
            path.symlink_to(root / ".env")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(os, "open", swapping)
    result = call("rg test assets")
    assert not result.ok and result.error_code == "symlink_forbidden"
    assert "test content" not in "\n".join(row["text"] for row in json.loads(result.content)["matches"] if row["path"] == "assets/swap.txt")


def test_search_budget_no_matches_binary_and_long_lines(listing):
    services, _, call = listing
    root = services.workspace.paths.root
    (root / "assets/text.md").write_text("\n".join(f"motion {n}" for n in range(250)))
    value = output(call("rg motion assets | head -40"))
    assert value["returned_count"] == 40 and value["truncated"]
    assert value["truncation_reason"] == "result_limit" and not value["search_complete"]
    value = output(call("rg motion assets | head -999"))
    assert value["returned_count"] == 200 and value["truncated"]
    value = output(call("grep -n absent assets/text.md"))
    assert value["returned_count"] == 0 and value["search_complete"]
    assert call("grep absent assets").error_code == "search_directory_requires_recursive"
    (root / "assets/binary").write_bytes(b"motion\x00binary")
    (root / "assets/large").write_text("motion" + "x" * (2 * 1024 * 1024))
    (root / "assets/long.md").write_text("motion " + "x" * 2000)
    value = output(call("rg motion assets/binary assets/large assets/long.md"))
    assert not value["search_complete"]
    assert {row["reason"] for row in value["skipped"]} == {"not_utf8_text", "file_byte_limit"}
    assert value["matches"][0]["line_truncated"]
    assert len(value["matches"][0]["text"]) == 1000
