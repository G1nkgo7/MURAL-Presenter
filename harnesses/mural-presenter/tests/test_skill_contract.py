import re
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[3]
SKILL = REPO / "skills/mural-presenter"


class SkillPackageContractTest(unittest.TestCase):
    def test_frontmatter_and_main_workflow_are_compact_and_well_formed(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"))
        frontmatter = text.split("---", 2)[1]
        keys = {
            match.group(1)
            for match in re.finditer(r"^([a-z][a-z0-9_-]*):", frontmatter, re.M)
        }
        self.assertEqual(keys, {"name", "description"})
        self.assertRegex(frontmatter, r"(?m)^name: mural-presenter$")

        sections = [
            int(value)
            for value in re.findall(r"(?m)^## ([1-9])\. ", text)
        ]
        self.assertEqual(sections, list(range(1, 10)))
        self.assertLessEqual(len(text.splitlines()), 500)

    def test_reference_and_script_tables_resolve_to_packaged_files(self):
        text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        reference_section = text.split("## 8. 按需参考与模板资产", 1)[1].split(
            "## 9. 脚本", 1
        )[0]
        listed_patterns = set(
            re.findall(r"(?m)^\| `([^`]+\.md)` \|", reference_section)
        )
        listed_references = set()
        for pattern in listed_patterns:
            matches = (SKILL / "references").glob(pattern)
            listed_references.update(
                path.relative_to(SKILL / "references").as_posix()
                for path in matches
                if path.is_file()
            )
        packaged_references = {
            path.relative_to(SKILL / "references").as_posix()
            for path in (SKILL / "references").rglob("*.md")
        }
        self.assertEqual(listed_references, packaged_references)

        script_section = text.split("## 9. 脚本", 1)[1]
        listed_scripts = {
            cell.split()[0]
            for cell in re.findall(r"(?m)^\| `([^`]+)` \|", script_section)
            if cell.split()[0].endswith(".py")
        }
        packaged_scripts = {path.name for path in (SKILL / "scripts").glob("*.py")}
        self.assertEqual(listed_scripts, packaged_scripts)

    def test_role_cards_are_explicit_and_nonduplicated(self):
        cards = {path.name for path in (SKILL / "roles").glob("*.md")}
        self.assertEqual(
            cards,
            {"material.md", "research.md", "image.md", "slide.md", "review.md"},
        )
        for name in cards:
            text = (SKILL / "roles" / name).read_text(encoding="utf-8")
            self.assertIn("status:", text, name)

    def test_progressive_design_navigation_resolves_without_bulk_reading(self):
        scenario_index = (SKILL / "references/scenario-routing.md").read_text(
            encoding="utf-8"
        )
        category_links = set(
            re.findall(r"\((slide-categories/[^)]+\.md)\)", scenario_index)
        )
        packaged_categories = {
            path.relative_to(SKILL / "references").as_posix()
            for path in (SKILL / "references/slide-categories").glob("*.md")
        }
        self.assertEqual(category_links, packaged_categories)

        style_index = (SKILL / "references/style-routing.md").read_text(
            encoding="utf-8"
        )
        style_links = set(
            re.findall(r"\((style-systems/[^)]+\.md)\)", style_index)
        )
        packaged_styles = {
            path.relative_to(SKILL / "references").as_posix()
            for path in (SKILL / "references/style-systems").glob("*.md")
        }
        self.assertEqual(style_links, packaged_styles)

        family_links = set(
            re.findall(r"\((style-families/[^)]+\.md)\)", style_index)
        )
        packaged_families = {
            path.relative_to(SKILL / "references").as_posix()
            for path in (SKILL / "references/style-families").glob("*.md")
        }
        self.assertEqual(family_links, packaged_families)

        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("只读取它链接的一个", skill_text)
        self.assertIn("后续 Agent 消费设计合同", skill_text)

        planning = (SKILL / "references/planning-contract.md").read_text(
            encoding="utf-8"
        )
        for field in (
            "primary_scenario",
            "category_reference",
            "visual_thesis",
            "signature_visual",
            "selected_references",
            "typography_roles",
            "shape_grammar",
            "special_page_system",
            "density_rhythm",
        ):
            self.assertIn(field, planning)
        slide_role = (SKILL / "roles/slide.md").read_text(encoding="utf-8")
        self.assertIn("shape-grammar.md", slide_role)
        self.assertIn("charts-and-diagrams.md", slide_role)

    def test_relative_markdown_links_resolve_and_skill_has_no_auxiliary_clutter(self):
        for document in SKILL.rglob("*.md"):
            text = document.read_text(encoding="utf-8")
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
                if "://" in target or target.startswith(("#", "/")):
                    continue
                path = target.split("#", 1)[0]
                self.assertTrue((document.parent / path).exists(), f"{document}: {path}")

        for name in ("README.md", "CHANGELOG.md", "INSTALLATION_GUIDE.md"):
            self.assertFalse((SKILL / name).exists())

    def test_speed_contracts_preserve_batching_and_progressive_review(self):
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        image_role = (SKILL / "roles/image.md").read_text(encoding="utf-8")
        slide_role = (SKILL / "roles/slide.md").read_text(encoding="utf-8")
        review_role = (SKILL / "roles/review.md").read_text(encoding="utf-8")

        self.assertIn("一个工具回合写入多个", skill_text)
        self.assertIn("一次 Vision 检查整组", image_role)
        self.assertIn("用一次 `vision_analyze` 查看这张组联系表", slide_role)
        self.assertIn("不要在开场一次通读全部逐页计划", review_role)
        self.assertIn("每批只加载对应页", review_role)

    def test_bitmap_first_visual_contract(self):
        skill_text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        image_role = (SKILL / "roles/image.md").read_text(encoding="utf-8")
        slide_role = (SKILL / "roles/slide.md").read_text(encoding="utf-8")
        planning = (SKILL / "references/planning-contract.md").read_text(encoding="utf-8")

        self.assertIn("不要为省事把本可成为主画面的内容降级成抽象几何", skill_text)
        self.assertIn("过程切面", image_role)
        self.assertIn("优先使用计划中的真实/生成图片", slide_role)
        self.assertIn("半屏或全屏主体视觉不得规划为 SVG", planning)


if __name__ == "__main__":
    unittest.main()
