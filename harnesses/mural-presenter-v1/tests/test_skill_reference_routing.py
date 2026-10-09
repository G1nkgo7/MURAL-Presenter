from pathlib import Path
import os
import re

import pytest

from runtime.production import read_scopes_for_role, write_scopes_for_role
from runtime.tools import ToolContext


REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills/mural-presenter-v1"
LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _anchors(text):
    explicit = set(re.findall(r'<a id="([^"]+)"', text))
    headings = re.findall(r"^#{1,6}\s+(.+)$", text, re.MULTILINE)
    return explicit | {
        re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        for title in headings
    }


def test_skill_reference_graph_has_no_broken_links_or_unrouted_documents():
    pending = [SKILL / "SKILL.md", *sorted((SKILL / "roles").glob("*.md"))]
    visited = set()
    while pending:
        source = pending.pop()
        if source in visited:
            continue
        visited.add(source)
        for link in LINK.findall(source.read_text()):
            assert "://" not in link, (source, link)
            relative, _, anchor = link.partition("#")
            target = (source.parent / relative).absolute() if relative else source
            # 文档路由按逻辑位置验证；固定对象发布另有共享资源链接的完整性测试。
            target = Path(os.path.normpath(target))
            assert target.is_relative_to(SKILL), (source, link)
            assert target.is_file(), (source, link)
            if anchor:
                assert anchor in _anchors(target.read_text()), (source, link)
            if target.suffix == ".md":
                pending.append(target)
    assert visited == set(SKILL.rglob("*.md"))
    assert not (SKILL / "references/deck-example.md").exists()


def test_style_routes_preserve_available_styles_and_resolve_to_one_card():
    old = (REPO / "archive/skills-20260911/pre-alpha-mural-presenter-v1/references/style-index.md").read_text()
    new = (SKILL / "references/style-index.md").read_text()
    old_ids = re.findall(r"^\| (\d{2}) \|", old, re.MULTILINE)
    new_ids = re.findall(r"^\| (\d{2}) \|", new, re.MULTILINE)
    assert new_ids == old_ids
    routes = re.findall(r"\]\((style/[^)#]+\.md)#style-(\d{2})\)", new)
    assert sorted(identifier for _, identifier in routes) == sorted(old_ids)
    for card, identifier in routes:
        text = (SKILL / "references" / card).read_text()
        assert text.count(f'<a id="style-{identifier}"') == 1
        assert re.search(rf"^## {identifier} · ", text, re.MULTILINE)


def test_bench_delivery_rules_preserve_requirements_facts_and_real_citations():
    slide = (SKILL / "roles/slide.md").read_text()
    research = (SKILL / "roles/research.md").read_text()
    plan = (SKILL / "references/planning/plan-contract.md").read_text()
    review = (SKILL / "references/pages/review.md").read_text()
    assert "真实文献引用、附件页码、Figure 编号和必要署名不是内部元数据" in slide
    assert "用户指定页位、原文或屏显内容不能移入讲稿" in slide
    for text in (slide, research, plan):
        assert "完成状态" in text and "确定程度" in text
    for text in (slide, research, review):
        assert "一基页码" in text or "一基定位" in text
        assert "Figure" in text
    assert all(label in review for label in ("任务覆盖", "标题与证据", "引用定位"))
    assert "只有用户或本次任务明确要求引用时才按要求上屏" in slide
    assert "普通任务不因缺少未要求的脚注返修" in review
    assert "纯文字仅" in (SKILL / "SKILL.md").read_text()
    policy = (REPO / "benchmark/PresentBench/local/authoring-policy.md").read_text()
    assert "Enable concise on-slide citations" in policy
    assert "one-based" in policy and "guessed" in policy
    for path in ("references/pages/content-pages.md", "references/pages/special-pages.md"):
        text = (SKILL / path).read_text()
        assert "Slide 引用规则" in text and "来源定位默认不上屏" not in text


def test_specialist_role_cards_stay_thin_and_exclude_runtime_internals():
    for path in sorted((SKILL / "roles").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert len(text) < 5000, (path, len(text))


def test_author_facing_skill_docs_do_not_expose_runtime_internal_fields():
    forbidden = {
        "_trace", "_state", "checkpoint", "max_tokens", "owner_id",
        "inspection_id", "png_sha256", "sha256", "128K",
        "training export", "训练导出",
    }
    for path in sorted(SKILL.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        assert all(token not in text for token in forbidden), path


def test_design_calibration_and_optional_layout_primitives_are_routed():
    main = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    calibration = (SKILL / "references/design-calibration.md").read_text(encoding="utf-8")
    css = (SKILL / "assets/base.css").read_text(encoding="utf-8")
    assert "references/design-calibration.md" in main
    assert "不新增 JSON schema" in calibration
    for name in ("hero", "focus", "split", "compare", "sequence"):
        assert f".deck-layout--{name}" in css

    # The runtime scaffold remains page-owned: primitives are opt-in, not hidden page types.
    skeleton = (SKILL / "scripts/_internal/deck_core.py").read_text(encoding="utf-8")
    assert 'class="page-stage"' in skeleton
    assert "deck-layout--" not in skeleton


def test_author_protocol_hides_page_lifecycle_commands():
    paths = (
        SKILL / "SKILL.md",
        SKILL / "roles/slide.md",
        SKILL / "references/runtime/commands.md",
        SKILL / "references/runtime/html-contract.md",
        SKILL / "references/planning/plan-contract.md",
    )
    text = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    for command in (
        "orchestrator.py scaffold",
        "orchestrator.py finalize",
        "orchestrator.py sync-speech",
        "slide.py render",
        "slide.py render-group",
    ):
        assert command not in text


@pytest.mark.parametrize("role", ["orchestrator", "research", "image", "slide"])
def test_fixed_skill_knowledge_readable_without_expanding_workspace_authority(role):
    context = ToolContext(
        role, role,
        read_scopes=read_scopes_for_role(role, skill_name=SKILL.name, pages=(1,)),
        write_scopes=write_scopes_for_role(role, pages=(1,)),
    )
    for reference in [*(SKILL / "references").rglob("*.md"), *(SKILL / "roles").glob("*.md")]:
        logical = f"skills/{SKILL.name}/{reference.relative_to(SKILL).as_posix()}"
        assert context.may_read(logical)
        assert not context.may_write(logical)
        for link in LINK.findall(reference.read_text()):
            relative = link.partition("#")[0]
            target = Path(os.path.normpath(reference.parent / relative)) if relative else reference
            assert context.may_read(f"skills/{SKILL.name}/{target.relative_to(SKILL).as_posix()}"), (role, reference, link)
    assert not context.may_read("skills/another-skill/references/style-index.md")
    assert not context.may_read("skills/another-skill/roles/slide.md")
    assert not context.may_read(f"skills/{SKILL.name}/scripts/_internal/deck_core.py")
    assert context.may_read("slides/slide_02.html") is (role == "orchestrator")
    assert not context.may_write("slides/slide_02.html")
    assert context.may_read("knowledge.md")
    assert context.may_write("knowledge.md") is (role == "orchestrator")
    assert context.may_read("research/topic-evidence.md") is (role in {"orchestrator", "research"})
    for alias in ("material.md", "research.md"):
        assert not context.may_read(alias) and not context.may_write(alias)
    if role == "orchestrator":
        assert context.may_read("briefs/slide_02.md")
        assert context.may_write("briefs/slide_02.md")
    else:
        assert not context.may_read("briefs/slide_02.md")
        assert not context.may_write("briefs/slide_01.md")
