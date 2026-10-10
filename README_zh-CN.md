# MURAL Presenter

**Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations** 的论文实现。

本仓库发布 0919 创作 Skill、配套 Harness 和 WebUI。254 个核心源码文件保持
`mural-presentbench-qwen35-9b-20260919T163556Z` 实验快照的原始字节，部署路径由启动层解析。

[English](README.md) · [WebUI 使用说明](webui/README.md) · [贡献说明](CONTRIBUTING.md)

## 安装与启动

需要 Python 3.12、uv，以及自己的模型服务和凭据。

```bash
uv sync --project webui/studio --frozen
uv sync --project webui/inference --frozen
cp webui/.env.example webui/.env
bash webui/start.sh
```

仅首次部署复制示例配置；已有 `.env` 应保留。打开启动器打印的地址，默认
`http://127.0.0.1:8001`。模型可在 WebUI 或部署环境中配置；Windows 使用 `webui/start.ps1`。
首次启动可能下载 Chromium。启动器生成被忽略的 `configs/runtime.local.json`，不会覆盖已有配置。
在其中设置并发及按顺序排列的 MinerU GPU/CPU 服务地址，或用 `MURAL_RUNTIME_CONFIG` 指定已有文件。
PDF、Office、图片附件解析需要 MinerU；纯文本任务不需要附件解析服务。搜索和生图服务分别配置。

## 命令行推理

```bash
webui/inference/.venv/bin/python scripts/run_inference.py \
  --query "制作一份六页的研究介绍演示" \
  --workspace "$PWD/artifacts/example"
```

使用 `--help` 查看参数，或用 `--doctor --json artifacts/doctor.json` 生成诊断报告。
新任务使用新工作区；保留历史工作区及其固定的运行版本。

## 公开目录

```text
MURAL-Presenter/
├── README.md / README_zh-CN.md
├── skills/mural-presenter-v1/       # 冻结的创作 Skill 与工具
├── harnesses/mural-presenter-v1/    # 冻结的推理与交付运行时
├── webui/                          # 前端、服务端、依赖锁和启动器
├── configs/                        # 运行配置模板、MinerU、版本来源清单
├── fonts/                          # 渲染所需字体
├── scripts/                        # 便携命令行入口和配置适配
├── tools/                          # 源码校验和集成版本冻结
├── tests/                          # 公开集成回归测试
└── .github/                        # 持续集成与贡献模板
```

Skill/Harness 的目录名及其内部 assets、references、docs、tests 都属于冻结版本，保持完整。
根目录 `benchmark/`、`docs/`、`examples/`、品牌素材 `assets/`、`paper/`、训练材料和独立
`site/` 不纳入本次发布。密钥、环境、数据库和生成 case 也不上传。必要说明已合并到 README。
本机保留这些目录不代表它们会进入 Git 提交。

## 版本来源与校验

当前发布清单为 `configs/releases/mural-paper-v1.json`。
`configs/releases/upstream-0919/` 保留原始 319 文件发布清单、版本 pin 和各项哈希。
历史清单只证明来源，其中记录的内部数据流程配置及评测脚本不作为本次发布依赖。

55 个 Skill 文件和 199 个 Harness 文件的原始联合 SHA-256 为：
`378c3ac7fa26d06b91f5408c7a25e7350b9b37e712611a322e387ae13a2d64e3`。
冻结源码中的历史示例或链接可能指向原始环境，实际使用以本 README 的便携入口为准。
两个无法读取的上游 WebUI 测试未导入。便携集成单独维护版本身份和摘要。

```bash
python tools/check_paper_release.py
```

该命令检查冻结核心、当前集成及 Git 发布边界。不得为绕过检查而刷新核心哈希。
集成层改动需单独审查、验证并更新清单，流程见贡献说明。

## 实验范围

本次发布范围是运行系统源码，不包含完整实验制品。
参考 0919 实验使用 Qwen3.5-9B，开启 thinking，关闭 Critic、搜索和生图，运行 238 个
PresentBench 案例，每个案例最多生成 3 次。这不是所有 WebUI 模型的默认配置。

复现论文分数还需要精确输入及校验信息、模型权重和对话模板、评测适配器、字体补丁、Judge
配置及结果覆盖统计。本包不提供完整 benchmark、训练流水线、模型权重和论文结果表。
原评测字体库大于本仓库附带字体库。程序运行成功或源码校验通过，不等于论文分数已复现。

## 验证与已知限制

自动检查命令见 [.github/workflows/public-surface.yml](.github/workflows/public-surface.yml)，
覆盖便携配置、HTTP 和 `/studio` 路由、版本/配置合约及当前 WebUI 配置。测试应使用独立数据目录。

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

## 许可与引用

项目整体许可证等待权利人确认。第三方原有条款继续适用，参见
[第三方声明](webui/THIRD_PARTY_NOTICES.md)及[字体记录](fonts/FONTS.md)。
最终作者、论文 URL/DOI、引用信息和固定论文 tag 待确认，不填写虚构元数据。
