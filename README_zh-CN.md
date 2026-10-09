# MURAL Presenter

本次整理的验证范围及冻结版本已知测试失败见[验证记录](docs/validation.md)。

**Multi-Agent Unified Revision-Aware Authoring for Long-Horizon Presentations** 的论文实现。

本版本以 `mural-presentbench-qwen35-9b-20260919T163556Z` 实验冻结包为基准。
Skill 与 Harness 全部文件保持该快照的原始字节；正式仓库同时提供配套 WebUI。
部署路径由启动层解析，不要求使用原作者的机器、目录或模型服务。

[English](README.md) · [目录说明](docs/repository-layout.md) ·
[实验复现](docs/reproduction.md) · [版本来源](docs/release-provenance.md) · [WebUI](webui/README.md)

## 安装与启动

需要 Python 3.12、uv，以及你自己的模型服务。

```bash
uv sync --project webui/studio --frozen
uv sync --project webui/inference --frozen
cp webui/.env.example webui/.env
bash webui/start.sh
```

打开启动器打印的地址，默认 `http://127.0.0.1:8001`。Windows 使用 `webui/start.ps1`。
模型可在 WebUI 中配置。首次启动可能下载 Chromium；部署配置自动生成到被 Git 忽略的
`configs/runtime.local.json`，其中目录会解析为当前仓库内的绝对路径。
如需修改并发、MinerU 的 GPU/CPU 服务地址，编辑这份本地配置；也可用
`MURAL_RUNTIME_CONFIG` 指定已有配置。不会覆盖现有凭据、数据库和生成任务。

正式入口只有一套：

- `skills/mural-presenter-v1/`：创作协议、角色说明、渲染和导出工具。
- `harnesses/mural-presenter-v1/`：推理、合成、状态管理、模型调用与交付。
- `webui/`：配套前端、服务端和运行依赖。

## 命令行推理

```bash
webui/inference/.venv/bin/python scripts/run_inference.py \
  --query "制作一份六页的研究介绍演示" \
  --workspace "$PWD/artifacts/example"
```

使用 `--help` 查看参数；使用 `--doctor --json artifacts/doctor.json` 生成环境报告。
该入口负责部署配置，再调用未修改的 0919 Harness。PDF、Office、图片附件解析依赖
外部 MinerU 服务；不包含附件的文本任务不需要该服务。模型、搜索和生图服务由使用者配置。

## 校验与复现边界

```bash
python tools/check_paper_release.py
```

当前发布清单位于 `configs/releases/mural-paper-v1.json`；原实验清单和文件哈希保留在
`configs/releases/upstream-0919/`。便携化集成单独记录身份，不沿用旧集成哈希冒充原包。
本仓库尚未据此重新运行完整模型实验，不把原始测试记录等同于本次验证。
详见 [实验复现](docs/reproduction.md)。

`site/` 展示网站独立维护，不参与论文代码发布；本地网站文件及内部 Git 历史保留。
历史代码仍可从原 Git 分支和本地备份恢复，不作为当前运行入口。

## 许可与引用

项目整体许可证等待权利人确认；第三方原有许可证及声明继续保留。
最终论文元数据和论文版本 tag 在作者确认后补齐，不填写虚构作者、DOI 或实验结果。
