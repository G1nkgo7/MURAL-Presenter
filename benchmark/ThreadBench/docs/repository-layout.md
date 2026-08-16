# 仓库结构与发布边界

## 正式源码

- `generate_ppt.sh`：单模型或 `--all-envs` 生成入口。
- `judge.sh`：加载 `.env` 密钥和 `configs/judge.env` 配置，选择 rubric/run，并调用 Python
  Judge runner 的薄封装。
- `configs/judge.env`：统一保存 Judge/Preaudit 模型、provider、URL、密钥变量引用和开关，
  以及 HTML 提取服务 URL；不保存真实密钥。
- `scripts/run_dimension_rubric_judge.py`：当前长程评测的参数和执行主入口。
- `scripts/` 顶层：由 shell 直接调用的 Python CLI。
- `scripts/generation/`：生成 run 校验和 artifact provenance 库。
- `scripts/judge/`：criterion-v2 Judge 的合同、评分、preaudit、HTML 提取和传输库。
- `PRESENTER_ROOT`：生成唯一使用的 MURAL Presenter Skill/Harness 冻结包路径；
  不复制进 benchmark 仓库。

## 工具目录

- `scripts/analysis/`：对既有评测结果做可审计派生。
- `scripts/curation/`：构造或整理 benchmark 输入，不参与正式评分。
- `scripts/rubric_authoring/`：历史和当前 rubric 的格式转换或工作簿生成。
- `scripts/ckpt800_service/`：保留的 ckpt800 服务运维工具，不由 benchmark 入口自动调用。
- `docs/legacy/`：不属于当前运行合同、但仍有历史解释价值的设计记录。
- `benchmark_hub/`：部署环境可选的静态 benchmark 导航页。

## 数据边界

`data/` 和 `data_old/` 整体作为本地数据目录，不进入源码仓库。case 目录通常包含：

```text
case.yaml
instruction.md
rubric_versions/<version>/*
```

`outputs/`、`evaluations/`、trace、图片、日志和临时恢复目录同样属于本地运行制品。需要公开
case 或结果时，应选择固定版本作为独立制品或发布到专门的数据仓库。

当前源码仓库保留通用 rubric、Judge 合同和执行代码；case-specific rubric 随本地 case 数据
管理，不进入当前发布范围。

## 当前 Judge 边界

仓库只保留 split Intermediate/Final rubric 的 criterion-v2 评测链。旧版 single/multi-page
Judge 入口已经移除；当前 runner 共用的模型传输、响应解析和产物检查集中在
`scripts/judge/judge_transport_and_artifact_runtime.py`，评分合同保留在
`scripts/judge/dimension_rubric_contracts.py`。

三个长期驻留的 `supervise_*` 已移除。正式批量运行应由外部作业系统调用稳定的
`generate_ppt.sh` / `judge.sh` 入口；run 的只读校验和选择逻辑保留在
`scripts/generation/generation_run_validation.py`。
