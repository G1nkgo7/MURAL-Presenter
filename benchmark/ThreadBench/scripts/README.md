# Scripts

`scripts/` 顶层只保留由 shell 或用户直接执行的 Python CLI；可复用模块分别放在
`generation/` 和 `judge/`。

## 正式链路

- `select_valid_generation_run.py` 调用
  `generation/generation_run_validation.py`：只读校验并选择 generation run。
- `adapt_generation_artifacts.py`、`write_generation_*.py` 和
  `generation/generation_artifact_provenance.py`：Long-Horizon Presenter 产物适配、attempt、
  metadata 和 provenance。
- `run_dimension_rubric_judge.py` 及其直接依赖：当前 THREAD-Bench 长程评测；模型接口为
  `--judge-model`、`--preaudit-model` 和 `--use-final-preaudit`，角色专用 Judge 参数可覆盖
  公共模型参数。Shell 入口统一从 `configs/judge.env` 读取模型配置，并通过 `*_API_KEY_ENV`
  引用 `.env` 中的密钥。
- `judge/judge_transport_and_artifact_runtime.py`：当前 Judge 共用的模型传输、响应解析、
  token 汇总和生成产物检查。
- `extract_final_html_content.py` 调用 `judge/final_html_content_extraction.py`：Final 评测前的
  共享 HTML 内容提取。

## 内部模块

- `generation/generation_artifact_provenance.py`：判断导出产物是否保持原始生成内容。
- `generation/generation_run_validation.py`：校验 canonical HTML/PNG run 并选择有效 run。
- `judge/dimension_rubric_contracts.py`：rubric 合同、evidence 组装和维度聚合。
- `judge/deterministic_criterion_evaluators.py`：不调用模型的 criterion 检查与观察。
- `judge/defect_penalty_scoring.py`：缺陷扣分和最弱维度衰减。
- `judge/final_output_preaudit.py`：Final content/visual preaudit。
- `judge/final_html_content_extraction.py`：Final HTML 内容的 LLM 提取和缓存。
- `judge/judge_transport_and_artifact_runtime.py`：模型传输、响应解析、token 汇总和产物检查。

## 独立工具

- `analysis/`：评分后处理；当前包含 group minimum decay 重聚合。
- `curation/rewrite_ppt_query.py`：只将 DeepResearchBench II 的 `content.task`
  改写为两部分式静态 HTML PPT query。单条输入可使用位置参数、`--input` 或 stdin；
  `--jsonl` 批量模式只提取每行的 `content.task`，Prompt 位于
  `prompts/curation/deepresearch_to_ppt_query.md`。使用 `--case-dir` 时读取目录内的
  `ref.json` 与 `case_metadata.json`，并默认写入同目录的 `instruction.md`。模型输出未通过
  合同校验时会携带错误反馈重试，默认最多尝试 3 次，可用 `--max-attempts` 调整。
- `diagnostics/`：显式后端能力探针。
- `rubric_authoring/`：rubric JSON/YAML/XLSX 转换与生成。
- `ckpt800_service/`：ckpt800 服务启动、探测、诊断和 watchdog。

独立工具通过脚本文件直接执行；移动工具时必须同时更新其项目根目录推导、兄弟模块导入、测试和文档引用。
