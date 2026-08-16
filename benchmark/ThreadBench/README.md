# THREAD-Bench

THREAD-Bench（Tracing Handoffs, Requirements, and Execution Across Deck
Generation）是一个面向长程演示文稿 Agent 的 process-aware、artifact-grounded
benchmark。它同时检查生成过程中的关键交接，以及最终 Deck 的知识、叙事、视觉和逐页质量。

仓库仍沿用 `NovaPresent-bench` 名称；当前公开 benchmark 名称统一为 **THREAD-Bench**。

## 当前正式合同

- 正式 case：`Education/Education-lh-Animal_navigation`
- Case-specific rubric：`v004`
- Intermediate：Research、Planning、Image、Slide Production 四组过程能力
- Final：Knowledge、六个 Deck 维度、五个 Page 维度
- 缺陷策略：`ordinary` / `major` 两档，维度内累计扣分
- Final 聚合：Deck 和 Page 分别执行一次最弱维度衰减，`alpha=0.3`
- Preaudit：可选，不直接评分，只向正式 Judge 提供与当前 criterion 相关的缺陷

完整设计见 [当前评测方案](rubric/LH-PresentBenchmark_current.md)。冻结的论文式说明保留在
[`rubric/THREAD-Bench.pdf`](rubric/THREAD-Bench.pdf)。

## 评测链路

```text
instruction.md
    -> generate_ppt.sh
    -> canonical slides/*.html + renders/*.png + generation_metadata.json
    -> extract_final_html_content.py
    -> derived_html_content/content.md + page_index.json
    -> judge.sh
    -> run_dimension_rubric_judge.py
       + judge/judge_transport_and_artifact_runtime.py
    -> Intermediate / Final criterion results
    -> dimension aggregation + major-defect penalty + alpha=0.3 decay
    -> score_result.json
```

## 快速开始

### 1. 安装依赖

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

复制密钥模板。`.env` 只保存 API Key；Judge、preaudit 和 HTML 提取的模型接口配置统一
保存在 `configs/judge.env`。不要提交真实密钥或 `model_envs/`：

```bash
cp .env.example .env
```

### 2. 生成 Deck

生成固定使用 `PRESENTER_ROOT` 指向的 MURAL Presenter Skill/Harness 冻结包。
Harness 根据 query 自动确定过程与交付语言。
`data/` 不随源码仓库发布，请先在本地准备 case，并通过绝对路径指定 `CASE_DIR`。

```bash
export CASE_DIR=/absolute/path/to/Education-lh-Animal_navigation
export ENV_FILE=/absolute/path/to/model.env
export PRESENTER_ROOT=/absolute/path/to/frozen-mural-presenter
export RUN_ID="demo-$(date -u +%Y%m%dT%H%M%SZ)"

./generate_ppt.sh <model-tag>
```

标准生成产物位于：

```text
<CASE_DIR>/outputs/<model-tag>/<run-id>/
├── slides/slide_NN.html
├── renders/slide_NN.png
├── generation_metadata.json
└── artifact_adaptation.json
```

生成入口不修改 Agent 生成的页面内容，不截断或补齐页数。有限适配只允许包装/拆分 HTML、
规范化文件名和补渲缺失 PNG，并在 metadata 中单独记录。

### 3. 提取最终 HTML 内容

Final Judge 不直接把整份 HTML 源码作为知识证据。先为选定 run 生成共享的内容提取结果：

```bash
python scripts/extract_final_html_content.py \
  --run-dir "$CASE_DIR/outputs/<model-tag>/<run-id>"
```

该步骤从 `.env` 读取 `DEEPSEEK_API_KEY`，从 `configs/judge.env` 读取
`DEEPSEEK_BASE_URL`，输出写入 run 下的 `derived_html_content/`。

### 4. 运行 Judge

```bash
CASE_DIR="$CASE_DIR" \
GEN_MODELS="<model-tag>" \
GEN_RUN="<run-id>" \
RUBRIC_REVISION=v004 \
EVALUATION_REVISION=v004-local \
GROUP_MIN_DECAY_ALPHA=0.3 \
./judge.sh
```

未显式传入 `RUBRIC_REVISION` 时，`judge.sh` 优先读取 `case.yaml` 的
`active_rubric_revision`。当前正式 case 已固定为 `v004`。

可用 `EVALUATION_SCOPE=intermediate|final|all` 控制评测范围。Judge 和 preaudit 配置统一修改
`configs/judge.env`：

```dotenv
JUDGE_MODEL=gemini-3.5-flash
INTERMEDIATE_JUDGE_PROVIDER=openai
INTERMEDIATE_JUDGE_BASE_URL=https://tokenhub.sensetime.com/v1
INTERMEDIATE_JUDGE_API_KEY_ENV=GEMINI_API_KEY

FINAL_JUDGE_PROVIDER=openai
FINAL_JUDGE_BASE_URL=https://tokenhub.sensetime.com/v1
FINAL_JUDGE_API_KEY_ENV=GEMINI_API_KEY

PREAUDIT_MODEL=gemini-3.1-pro-preview
USE_FINAL_PREAUDIT=1
PREAUDIT_JUDGE_PROVIDER=openai
PREAUDIT_JUDGE_BASE_URL=https://tokenhub.sensetime.com/v1
PREAUDIT_JUDGE_API_KEY_ENV=GEMINI_API_KEY
```

`*_API_KEY_ENV` 保存的是 `.env` 中密钥变量的名称，不是密钥本身。`JUDGE_MODEL` 同时作为
Intermediate 和 Final Judge 的默认模型；已有的
`INTERMEDIATE_JUDGE_MODEL`、`FINAL_JUDGE_MODEL` 可以分别覆盖它。
`PREAUDIT_MODEL` 只控制可选 preaudit，不会改变正式 Judge 模型。

默认分别读取仓库根目录 `.env` 和 `configs/judge.env`。需要切换文件时使用
`JUDGE_SECRETS_FILE=/path/to/secrets.env` 或
`JUDGE_CONFIG_FILE=/path/to/another-judge.env`。

参数定义和默认值由 `scripts/run_dimension_rubric_judge.py` 维护：直接调用时使用
`--judge-model`、`--preaudit-model` 和 `--use-final-preaudit`。`judge.sh` 只负责加载
密钥与 Judge 配置、选择 case/run/rubric，并把参数转交给 Python runner。

## Dashboard

Dashboard 作为独立部署工具维护，不随 benchmark 源码仓库发布。它只读扫描本地 case、
rubric、generation 和 evaluation 产物。

## 仓库结构

```text
config/                       评分策略和固定配置
configs/judge.env             Judge/Preaudit/HTML 提取接口、密钥引用和开关
data/                         本地 case、生成与评测数据；整个目录不提交
docs/                         使用、结构和维护说明
prompts/judges/               Judge system prompts
rubric/                       benchmark 方案、通用 rubric 和冻结文档
scripts/                      正式生成/评测辅助模块与 CLI
scripts/analysis/             评分后处理和离线分析
scripts/curation/             数据/query 整理工具
scripts/diagnostics/          显式诊断工具
scripts/rubric_authoring/     rubric 格式转换与 XLSX 生成
scripts/ckpt800_service/      保留的 ckpt800 服务启动和恢复工具
外部固定 Presenter 包：由环境变量 PRESENTER_ROOT 指定
```

脚本职责和依赖边界见 [`scripts/README.md`](scripts/README.md)，更详细的运行说明见
[`docs/getting-started.md`](docs/getting-started.md)，发布边界见
[`docs/repository-layout.md`](docs/repository-layout.md)。

## 验证

```bash
bash -n generate_ppt.sh judge.sh scripts/ckpt800_service/*.sh
```

语法检查和 dry-run 只能证明本地合同与路由有效；真实 benchmark 结果还必须检查生成 metadata、
完整 canonical artifacts、Judge request/response 和最终 `score_result.json`。本地回归测试不随
源码仓库发布。

## 发布边界

- 不提交 `.env`、`model_envs/`、API key 或服务凭证。
- 不提交 Dashboard runtime、`.supervision/`、临时目录和 Python cache。
- `data/`、`data_old/`、Dashboard、历史 smoke runs 和本机恢复目录不属于当前源码发布范围。
- 需要公开 case 或评测结果时，使用独立制品或专门的数据发布仓库。
