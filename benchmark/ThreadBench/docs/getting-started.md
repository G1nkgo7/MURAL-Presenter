# 运行 THREAD-Bench

本文描述当前 `v004` case-specific rubric 的标准执行顺序。所有路径示例均从仓库根目录运行。

## 1. 环境

核心 Python 依赖来自 `requirements.txt`。Playwright 用于 HTML/DOM 和渲染相关检查：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

`.env` 只保存 API Key；Judge/Preaudit 的模型、provider、URL、密钥变量引用和开关，以及
HTML 提取服务 URL，统一保存在 `configs/judge.env`。每个生成模型的配置通过仓库外部的
`ENV_FILE` 传入。真实密钥和生成模型环境文件都不应提交。

## 2. 选择 case

`data/` 是本地数据目录，不随源码仓库发布。准备好 case 后使用绝对路径指定：

```bash
export CASE_DIR=/absolute/path/to/Education-lh-Animal_navigation
```

正式 rubric 由 `case.yaml.active_rubric_revision` 指定。当前值为 `v004`。

## 3. 生成

```bash
export ENV_FILE=/absolute/path/to/model.env
export PRESENTER_ROOT=/absolute/path/to/frozen-mural-presenter
export RUN_ID="thread-bench-$(date -u +%Y%m%dT%H%M%SZ)"
./generate_ppt.sh <model-tag>
```

生成固定使用 `PRESENTER_ROOT` 指向的 MURAL Presenter 冻结包。不再选择或加载其他
Skill/Harness；语言由 Harness 从原始 query 自动判断。

有效 run 必须具有连续编号且一一对应的 `slides/slide_NN.html` 和
`renders/slide_NN.png`，并在 `generation_metadata.json` 中通过产物合同。

## 4. HTML 内容提取

```bash
python scripts/extract_final_html_content.py \
  --run-dir "$CASE_DIR/outputs/<model-tag>/$RUN_ID"
```

输出合同：

```text
derived_html_content/
├── content.md
└── page_index.json
```

提取脚本默认从 `.env` 读取 `DEEPSEEK_API_KEY`，从 `configs/judge.env` 读取
`DEEPSEEK_BASE_URL`。

只运行 `EVALUATION_SCOPE=intermediate` 时不要求该步骤；包含 Final 时必须先完成。

## 5. 评测

```bash
CASE_DIR="$CASE_DIR" \
GEN_MODELS="<model-tag>" \
GEN_RUN="$RUN_ID" \
RUBRIC_REVISION=v004 \
EVALUATION_REVISION=v004-local \
EVALUATION_SCOPE=all \
GROUP_MIN_DECAY_ALPHA=0.3 \
./judge.sh
```

主要开关：

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `EVALUATION_SCOPE` | `all` | `intermediate`、`final` 或 `all` |
| `GROUP_MIN_DECAY_ALPHA` | `0.3` | Deck/Page 最弱维度衰减系数 |
| `JUDGE_CONFIG_FILE` | `configs/judge.env` | Judge/Preaudit 配置文件 |
| `JUDGE_SECRETS_FILE` | `.env` | API 密钥文件 |
| `OVERWRITE` | `1` | 是否覆盖同一 evaluation profile |
| `RESUME` | `1` | 对未完成的 criterion 结果做续跑 |
| `MOCK_JUDGE` | `0` | 本地合同测试，不产生真实 benchmark 分数 |

统一在 `configs/judge.env` 中设置模型和接口：

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

其中 `*_API_KEY_ENV` 指向 `.env` 中的变量名，例如：

```dotenv
GEMINI_API_KEY=<secret>
```

切换另一套非敏感配置时可运行：

```bash
JUDGE_CONFIG_FILE=/absolute/path/to/another-judge.env ./judge.sh
```

模型参数的解析由 `scripts/run_dimension_rubric_judge.py` 负责。直接调用 runner 时使用：

```bash
python scripts/run_dimension_rubric_judge.py \
  <其他必需参数> \
  --judge-model gemini-3.5-flash \
  --preaudit-model gemini-3.1-pro-preview \
  --use-final-preaudit
```

`--judge-model` 同时设置 Intermediate/Final Judge。若同时提供
`--intermediate-judge-model` 或 `--final-judge-model`，角色专用参数优先。
旧的 `--preaudit-judge-model` 仍作为 CLI 兼容别名；新配置统一使用
`--preaudit-model` / `PREAUDIT_MODEL`。

正式结果必须同时检查 `run_config.json`、逐 criterion Judge 结果和
`score_result.json`，不能只以进程退出码或 HTTP 200 判断成功。

## 6. Dashboard

Dashboard 作为独立部署工具维护，不随 benchmark 源码仓库发布。部署时让它只读访问本地
case、generation 和 evaluation 目录。
