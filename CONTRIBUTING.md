# Contributing

Use a topic branch and keep changes scoped. The current paper source pair is frozen to the 0919
experimental inventory; do not update its hashes merely to make validation pass.
Deployment and WebUI changes belong outside the frozen pair and require integration tests plus
a refreshed, explicitly named integration manifest. Record what was tested and whether model
requests were made. Keep credentials, datasets, generated workspaces and the separate site local.

Run `python tools/check_paper_release.py` before submitting changes. Follow the launch and
validation commands in README.md. Licensing and final paper metadata remain author decisions.

The root `pytest.ini` selects portable integration tests. The CI workflow explicitly selects
the release/configuration contracts and current WebUI tests as well. Upstream historical tests
may require archived runtimes or external canary assets that are not part of this source package.
See the release verification record below for tested scope and known failures; a passing packaging CI is not a
claim that the complete experimental suite or benchmark has passed.

The public branch contains the runtime, WebUI and their maintenance files. Keep benchmark,
root docs, examples, design drafts, training and paper workspaces local; do not force-add them.
Keep public brand exports in `assets/logo/exports/` and method figures in `assets/figures/`.
Both README editions should retain the project identity and method overview alongside current usage.
Keep the large light/dark MURAL header visible. When the paper name changes, update its typography
while preserving the existing mascot and historical exports. Put detailed provenance and validation
records here or in linked asset guides so the overview and quick start remain easy to find.
Keep paper terminology, training checkpoints and evaluation scope consistent across both editions.
Keep each README in one language: `README.md` is the English repository homepage and
`README_zh-CN.md` is the complete Chinese edition. Keep their content aligned and their language
links reciprocal; the English link in the Chinese edition returns to the repository homepage.
Do not append a second language to the homepage README.
Record manuscript figure sources in `assets/figures/README.md`; keep the runtime source pin separate
from model checkpoint identities and do not imply that unpublished experiment artifacts are bundled.
The frozen core's own docs, tests and assets remain part of its original inventory.
After reviewing and testing integration changes, commit the source changes, then run
`python tools/freeze_paper_integration.py` and `python tools/check_paper_release.py`.
Commit the resulting manifest separately so its source commit identifies the reviewed source.

## Release verification record

<details>
<summary>Source inventory and hash verification</summary>

The active manifest is `configs/releases/mural-paper-v1.json`.
`configs/releases/upstream-0919/` retains the original 319-file publication inventory,
its pin and its release hashes. Historical inventory entries record provenance; they do not
require publishing the original internal pipeline configurations or benchmark scripts.

All 55 Skill and 199 Harness files retain the original combined SHA-256:
`378c3ac7fa26d06b91f5408c7a25e7350b9b37e712611a322e387ae13a2d64e3`.
Historical examples and links inside that frozen source may refer to the original environment;
use the root README's portable launch instructions. Two unreadable upstream WebUI tests were not
imported. The portable integration has a separate release identity and digest.

```bash
python tools/check_paper_release.py
```

This verifies the frozen core, active integration and tracked release boundaries.
Do not refresh core hashes to conceal source changes. Integration updates require their own
review, tests and manifest updates following the workflow above.

</details>

<details>
<summary>Validation record and known limits</summary>

CI commands are in [.github/workflows/public-surface.yml](.github/workflows/public-surface.yml).
They cover portable configuration, HTTP and `/studio` routing, release/configuration contracts,
and active WebUI configuration. Use isolated test data, not an existing Studio database.

The frozen configuration tests run with `-p tools.pytest_portable_runtime`. This test-only
adapter redirects the `runtime.env` configuration path into the existing isolated fixture;
all deployment timeout assertions and the 0919 source bytes remain unchanged. Without it,
three tests read an upstream machine path and fail on a clean runner.
On 2026-10-12, local validation with this adapter passed 9 portable integration tests,
95 runtime contract tests and 139 WebUI tests, with one browser test skipped. Source
verification and the example launcher preflight also passed; no model requests were made.

The compact public tree was checked on Linux on 2026-10-10: 10 portable/source-digest tests,
95 release/configuration tests and 139 WebUI tests passed; one browser test was skipped.
The example launcher preflight passed. These checks used an isolated Git worktree and existing
locked-dependency test environments, without model requests or production data.

The complete historical suite has not passed. The frozen source retains these known failures:

- `test_agent_runtime.py::test_resume_preserves_inspect_feedback_as_tool_context_without_reconciliation`:
  resumed tool content is a string where the assertion expects text/image parts.
- `test_creation_config_runtime.py::test_presentation_change_requires_new_render_and_real_visual_consumption`:
  the second inspection returns `inspection_limit_reached` after a language change.

Two historical tests cannot collect without unpublished canary modules
(`test_real_webui_canary.py`, `test_refero_font_audit.py`); some legacy WebUI tests require archived
runtimes. Packaging CI deliberately selects its documented scope. Docker builds, Windows/macOS
startup and the complete 238-case experiment have not been validated by these packaging checks.
Local deployment smoke tests are not paper-score reproduction.

</details>

## 发布验证记录

<details>
<summary>源码清单与哈希校验</summary>

当前发布清单为 `configs/releases/mural-paper-v1.json`。
`configs/releases/upstream-0919/` 保留原始 319 文件发布清单、版本 pin 和各项哈希。
历史清单只证明来源，其中记录的内部数据流程配置及评测脚本不作为本次发布依赖。

55 个 Skill 文件和 199 个 Harness 文件的原始联合 SHA-256 为：
`378c3ac7fa26d06b91f5408c7a25e7350b9b37e712611a322e387ae13a2d64e3`。
冻结源码中的历史示例或链接可能指向原始环境，实际使用以根目录 README 的便携入口为准。
两个无法读取的上游 WebUI 测试未导入。便携集成单独维护版本身份和摘要。

```bash
python tools/check_paper_release.py
```

该命令检查冻结核心、当前集成及 Git 发布边界。不得为绕过检查而刷新核心哈希。
集成层改动需单独审查、验证并更新清单，流程见贡献说明。

</details>

<details>
<summary>验证记录与已知限制</summary>

自动检查命令见 [.github/workflows/public-surface.yml](.github/workflows/public-surface.yml)，
覆盖便携配置、HTTP 和 `/studio` 路由、版本/配置合约及当前 WebUI 配置。测试应使用独立数据目录。

冻结配置测试通过 `-p tools.pytest_portable_runtime` 加载测试适配层，将 `runtime.env`
中的配置路径指向已有的隔离 fixture。全部部署超时断言与 0919 源码字节保持不变；
缺少该适配时，三项测试会读取上游机器路径，在干净环境中失败。
2026-10-12 使用该适配进行本地验证：9 项便携集成、95 项运行时合约和 139 项 WebUI
测试通过，1 项浏览器测试跳过；源码校验与示例启动预检通过，未调用模型。

2026-10-10 在 Linux 上验证精简公开文件树：便携入口及源码摘要测试 10 项、版本/配置测试 95 项、
WebUI 测试 139 项通过，1 项浏览器测试跳过；示例启动预检通过。验证使用独立 Git 工作树和已有的
锁定依赖测试环境，不调用模型，不访问生产数据。

完整历史测试套件尚未全部通过。冻结源码保留了以下已知失败：

- `test_agent_runtime.py::test_resume_preserves_inspect_feedback_as_tool_context_without_reconciliation`：
  续跑工具内容为字符串，测试期望文本/图片内容块。
- `test_creation_config_runtime.py::test_presentation_change_requires_new_render_and_real_visual_consumption`：
  更改演示语言后，第二次检查返回 `inspection_limit_reached`。

`test_real_webui_canary.py`、`test_refero_font_audit.py` 缺少未发布的 canary 模块；部分历史
WebUI 测试依赖归档运行时。因此 CI 显式选择公开范围，并不代表完整历史套件通过。
Docker 构建、Windows/macOS 启动和完整 238 案例实验未由这些打包检查验证。
本机真实生成冒烟测试也不能替代论文实验复现。

</details>

## Repository layout

```text
MURAL-Presenter/
├── README.md / README_zh-CN.md
├── skills/mural-presenter-v1/       # Frozen authoring Skill and tools
├── harnesses/mural-presenter-v1/    # Frozen inference and delivery runtime
├── webui/                          # UI, server, dependency locks and launchers
├── assets/                         # Public logos, icons and method diagrams
├── configs/                        # Runtime template, MinerU and release provenance
├── fonts/                          # Bundled rendering fonts
├── scripts/                        # Portable CLI and configuration adapters
├── tools/                          # Source verification and integration freezing
├── tests/                          # Public integration regression tests
└── .github/                        # CI and contribution templates
```

The core directory names and internal assets, references, docs and tests are part of its frozen
identity. Keep them intact. Public logos and diagrams live in `assets/logo/exports/` and
`assets/figures/`; [figure provenance](assets/figures/README.md) records the current manuscript exports.
Design drafts remain local. Root-level `benchmark/`, `docs/`, `examples/`,
`paper/`, training material and the independent `site/` are not included in this release.
Deployment credentials, environments, databases and generated cases remain local and ignored.
Usage is in the root README; maintenance and validation records are kept here.

## 公开目录

```text
MURAL-Presenter/
├── README.md / README_zh-CN.md
├── skills/mural-presenter-v1/       # 冻结的创作 Skill 与工具
├── harnesses/mural-presenter-v1/    # 冻结的推理与交付运行时
├── webui/                          # 前端、服务端、依赖锁和启动器
├── assets/                         # 正式 logo、图标与方法配图
├── configs/                        # 运行配置模板、MinerU、版本来源清单
├── fonts/                          # 渲染所需字体
├── scripts/                        # 便携命令行入口和配置适配
├── tools/                          # 源码校验和集成版本冻结
├── tests/                          # 公开集成回归测试
└── .github/                        # 持续集成与贡献模板
```

Skill/Harness 的目录名及其内部 assets、references、docs、tests 都属于冻结版本，保持完整。
正式品牌素材位于 `assets/logo/exports/`，方法配图位于 `assets/figures/`；
[配图来源记录](assets/figures/README.md)说明当前论文配图与历史版本。设计草稿保留本地。
根目录 `benchmark/`、`docs/`、`examples/`、`paper/`、训练材料和独立
`site/` 不纳入本次发布。密钥、环境、数据库和生成 case 也不上传。使用说明见根目录 README，维护与验证记录保留在本文件。
本机保留这些目录不代表它们会进入 Git 提交。

Keep the provisional BibTeX in both READMEs aligned with `CITATION.cff`. Until the authors provide final metadata, retain Anonymous Authors and do not add a venue, DOI or publication URL.
