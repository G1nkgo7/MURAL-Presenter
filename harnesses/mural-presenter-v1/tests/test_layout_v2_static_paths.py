"""静态守卫：新 Harness/Skill 生产源码只允许纯 layout-v2 路径。

允许的例外仅有：
- Tool runner 识别的 ``skills/mural-presenter-v1/...`` 逻辑命令别名；
- exporter staging/ZIP 内的 ``assets/fonts/...``（只存在于导出打包，不是作者 workspace）；
- 用于“必须拒绝”的 legacy 拒绝清单（薄读取器 workspace_paths.py 内）。
禁止其它 ``plan/``、``materials/``、workspace ``assets/fonts`` 路径；research/ 是明确分配的任务稿目录，
以及 ``is_legacy`` / ``LEGACY_LAYOUT`` / ``resolve_logical`` 的 legacy 分支。
"""
from __future__ import annotations

import re
from pathlib import Path


REPOSITORY = Path(__file__).resolve().parents[3]
HARNESS = REPOSITORY / "harnesses" / "mural-presenter-v1"
SKILL = REPOSITORY / "skills" / "mural-presenter-v1"

# 生产源码目录（不含测试与 __pycache__）。
_HARNESS_DIRS = ("runtime", "state", "export", "qc", "orchestration", "owner")
_SKILL_GLOBS = ("scripts/*.py", "scripts/_internal/*.py")

# 路径形态的 legacy 命中：作为路径分量或前缀出现。
_LEGACY_PATTERNS = (
    re.compile(r"design-brief\.md|slide_brief_pattern|plan_batch"),
    re.compile(r"CLEAN_STRICT_PLAN_CONTRACT|delegate_next_event|delegate_collect|submit_wave|collect_wave"),
    re.compile(r"""["'`]plan/"""),          # "plan/..."
    re.compile(r"""["'`]materials/"""),      # "materials/..."
    re.compile(r"""/ ["']plan["']"""),       # root / "plan"
    re.compile(r"""/ ["']materials["']"""),  # root / "materials"
    re.compile(r"""root / ["']tmp["']"""),   # root / "tmp"  (root scratch)
    re.compile(r"\bis_legacy\b"),
    re.compile(r"\bLEGACY_LAYOUT\b"),
    re.compile(r"\bresolve_logical\b"),
)

# 允许 assets/fonts 出现的文件（exporter staging / ZIP 打包）。
_ASSETS_FONTS_ALLOWED = {
    HARNESS / "export" / "delivery.py",
}

# 允许保留 legacy 拒绝清单的文件（薄读取器把它们当作 must-reject 输入）。
_LEGACY_LIST_ALLOWED = {
    HARNESS / "runtime" / "workspace_paths.py",
    SKILL / "scripts" / "_internal" / "workspace_paths.py",
}


def _production_files() -> list[Path]:
    files: list[Path] = []
    for name in _HARNESS_DIRS:
        base = HARNESS / name
        if base.is_dir():
            files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    for pattern in _SKILL_GLOBS:
        files.extend(
            p for p in SKILL.glob(pattern) if "__pycache__" not in p.parts
        )
    return sorted(set(files))


def test_no_legacy_paths_in_production_source() -> None:
    offenders: list[str] = []
    for path in _production_files():
        # 薄读取器/解析器把 legacy 路径写进拒绝清单与合同说明，是合法的“must-reject”定义。
        if path in _LEGACY_LIST_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in _LEGACY_PATTERNS:
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPOSITORY)}:{lineno}: {line.strip()}")
    assert not offenders, "legacy path handling remains:\n" + "\n".join(offenders)


def test_assets_fonts_only_in_exporter_staging() -> None:
    offenders: list[str] = []
    marker = re.compile(r"assets/fonts")
    for path in _production_files():
        if path in _ASSETS_FONTS_ALLOWED or path in _LEGACY_LIST_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if marker.search(line):
                offenders.append(f"{path.relative_to(REPOSITORY)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "author-workspace assets/fonts path remains (only exporter staging may "
        "reference assets/fonts):\n" + "\n".join(offenders)
    )


def test_only_skill_alias_is_the_allowed_logical_prefix() -> None:
    # 逻辑 skills/ 前缀来自已固定 Skill；不得回到旧 workspace-local 路径。
    marker = re.compile(r"""["'`]skills/([^/"'`]+)/""")
    offenders: list[str] = []
    for path in _production_files():
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in marker.finditer(line):
                name = match.group(1)
                # 调度器仅识别动态别名的语法；实际执行仍校验固定 Skill，不放行其它路径字面量。
                if (path == HARNESS / "runtime" / "tool_batch.py" and match.start() > 0
                        and line[match.start() - 1:].startswith(r'r"skills/[^/]+/scripts/material\.py"')):
                    continue
                if name in {"mural-presenter-v1", "{self.name}", "{skill_name}", "{self.snapshot.name}", "<name>"}:
                    continue
                offenders.append(
                    f"{path.relative_to(REPOSITORY)}:{lineno}: {line.strip()}"
                )
    assert not offenders, "unexpected logical skills/ prefix:\n" + "\n".join(offenders)
