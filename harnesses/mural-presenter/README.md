# MURAL Presenter Harness：推理与训练数据合成

正式 Harness 使用同一个入口 `infer.py`，通过 `--mode` 选择两种运行画像。
两种模式共用同一套 Skill、Agent 编排、验收逻辑和 Deck 产物，区别只在运行时上下文维护与
训练轨迹完整性要求。

## 中英双语自动路由

Harness 根据原始 query 的主要语言自动选择 Agent 工作合同，WebUI 不需要提供手动语言开关：

| Query 主要语言 | Skill 入口 | 子 Agent 职责卡 |
| --- | --- | --- |
| 中文 | `skills/mural-presenter/SKILL.md` | `subagents/<role>.md` |
| English | `skills/mural-presenter/SKILL.en.md` | `subagents/<role>.en.md` |

路由只决定 `response_language` 和 Agent 读取的执行合同；PPT 屏显、规划与讲稿的
`deliverable_language` 仍以用户明确要求为准。例如英文 query 要求交付中文 PPT，Agent 使用
英文职责卡，但产出中文屏显与讲稿。

每个 Deck 的 `_trace/skill-snapshot.json` 会同时记录 `language` 和实际入口 `entry`；中英文共用
同一份脚本、references、素材目录和机器合同，不复制 Harness 运行时。

## 两种创作画像

`--authoring-profile` 控制的是页面创作权，不是推理/合成轨迹模式：

| 画像 | WebUI 名称 | 页面生产方式 | 适用场景 |
| --- | --- | --- | --- |
| `stable`（默认） | MURAL Presenter | 同构页可由 Production Group 统一生产 | 弱模型、批量基准、稳定交付 |
| `creative` | MURAL Presenter · Creative | 强制一页一个 Slide Agent；页面 Agent 可重写正文并独立构图 | Opus 等强模型、演讲型/教学型高完成度演示 |

Creative 画像只放开主题解释、正文措辞和页面构图；附件优先级、`must_present` 屏显、素材来源、
字体交付、渲染、最终 Review、`deck.py build/audit` 等工程门与默认画像完全相同。每个 Deck 的
`_trace/skill-snapshot.json` 会记录 `authoring_profile`，因此训练数据和评测结果可明确区分。

命令行示例：

```bash
uv run python infer.py \
  --query "做一份 8 页、适合现场演讲的研究报告" \
  --batch demo-creative \
  --mode inference \
  --authoring-profile creative
```

也可设置 `MURAL_AUTHORING_PROFILE=creative`，或在输入 JSONL 的单条样本中写
`{"authoring_profile":"creative"}`；样本级设置优先于进程默认值。

## 两种模式

| 行为 | `inference`（默认） | `synthesis` |
| --- | --- | --- |
| 用途 | WebUI/日常生成，交付速度和 Token 优先 | 构造可训练、可复放的 Agent 轨迹 |
| 已消费图片 | 从后续活跃上下文释放 | 完整保留到任务结束 |
| 早期历史 | 超阈值后压缩，完整调试 Trace 仍保留 | 不压缩，模型实际上下文与落盘消息序列一致 |
| 图片证据 | 保存调试快照，不作为接收硬门 | 写 SHA-256 清单并校验快照存在 |
| 样本验收 | 按 Deck、渲染、Review 和交付合同验收 | 在相同验收之上，要求编排器与所有子 Agent 的轨迹完整 |

`synthesis` 的“无损”边界是：模型实际看见的消息序列不经过 Harness 的图片释放或历史
压缩；图片在 `messages.json` 中以 shot 引用表示，原始模型输入字节保存在对应 `images/`
文件，并由 `multimodal-manifest.json` 记录 SHA-256。它不表示公开模型服务内部未返回的
隐藏状态也能被恢复。

模型 API 实际返回的 reasoning 也属于轨迹：Anthropic 有签名 thinking 保存 `thinking + signature`；
DeepSeek/Qwen 等 OpenAI 兼容接口的无签名 `reasoning_content` 保存为 `{type: thinking, thinking: ...}`。
无签名 thinking 只在下一轮 API 的发送副本中被移除，不会回灌触发 Anthropic signature 校验，
也不会从 `messages.json` 中丢失。对于 API 从未返回的内部隐藏思考，Harness 仍无法恢复。

## 直接运行

在仓库根目录执行：

```bash
cd harnesses/mural-presenter

# 推理模式：单条生成，默认模式
uv run python infer.py \
  --query "做一份 8 页的多智能体演示文稿" \
  --batch demo-infer \
  --mode inference

# 合成模式：批量生成训练轨迹
uv run python infer.py \
  --input /absolute/path/to/briefs.jsonl \
  --batch train-synthesis-v1 \
  --workers 4 \
  --mode synthesis

# 合成任务断点续跑
uv run python infer.py \
  --input /absolute/path/to/briefs.jsonl \
  --batch train-synthesis-v1 \
  --workers 4 \
  --mode synthesis \
  --resume
```

输入 JSONL 每行至少包含 `query`：

```json
{"query":"为机器学习课程制作 8 页讲义","lang":"zh","slide_count":8}
```

也可用环境变量设置默认画像：

```bash
export MURAL_RUN_MODE=synthesis
```

命令行 `--mode` 优先。推理与合成应使用不同的 `--batch` 名称；不要对同一个已完成 batch
切换模式后使用 `--resume`，因为完成样本会按 manifest 跳过。

## 模型组合与多模态 SFT

一次 rollout 可以组合：

- 一个主 Agent 模型：可为原生多模态模型，也可为纯文本模型；
- 一个可选的独立 Vision 模型：纯文本主模型通过 `vision_analyze` 工具使用；
- 可选的生图模型与搜索服务，它们属于工具环境，不替代主 Agent。

纯文本主模型外挂 Vision 的典型配置：

```bash
export MODEL_BACKEND=openai
export STUDENT_BASE_URL=https://main-model.example/v1
export STUDENT_MODEL=my-text-agent-model
export STUDENT_API_KEY=replace-me

export VISION_BACKEND=one_shot
export VISION_ONESHOT_BASE_URL=https://vision-model.example/v1
export VISION_ONESHOT_MODEL=my-vision-model
export VISION_ONESHOT_API_KEY=replace-me

uv run python infer.py \
  --input /absolute/path/to/briefs.jsonl \
  --batch text-plus-vision-synthesis-v1 \
  --workers 4 \
  --mode synthesis
```

这种组合会生成两类可对齐证据：

1. 主 Agent 的 `messages.json`：记录 `vision_analyze` 工具调用、视觉文字结果以及后续决策，
   适合训练“会调用视觉工具并使用结果”的 Agent；
2. `aux_calls/vision-one-shot.jsonl` + `images/` + `multimodal-manifest.json`：记录图片字节
   SHA-256、问题、Vision 模型、完整回答和 usage，可打包为图片 + 问题 → 视觉回答的原生
   多模态 SFT 样本。

因此，同一批原始合成数据可以用于多模态模型训练，但必须在 pack 阶段选择训练目标：

| Pack 目标 | 输入/标签 | 能训练什么 |
| --- | --- | --- |
| Tool-use Agent SFT | 主轨迹：文本上下文 + `vision_analyze` 调用 + 工具文字结果 | 纯文本或多模态 Agent 的视觉工具使用能力 |
| Native Vision SFT | 辅助轨迹：图片 + question → `response_text` | 多模态模型直接读图、审校和回答能力 |
| Joint mixture | 上述两类样本按来源字段混合，不把两条调用伪装成同一条 | 同一多模态模型兼顾直接读图与工具编排 |

注意：纯文本主模型从未直接看到图片，因此不能把它在工具结果之后的主轨迹回答，冒充为
“主模型直接根据图片产生”的监督标签。原始数据支持原生多模态训练，是因为独立 Vision 调用
本身保存了图片输入与回答；打包器必须按上述边界转换。一个 batch 内的主模型配置是进程级
统一的；要比较多个主模型，应为每个模型使用独立 `--batch`，避免 provenance 混合。

## 输出路径

默认产物位于 Harness 目录：

```text
harnesses/mural-presenter/
├── runs/<batch>/<sample_id>/
│   ├── slides/、renders/、assets/、speech.md、present.html
│   └── _trace/
│       ├── orchestrator/
│       │   ├── messages.json
│       │   ├── tool_log.json
│       │   ├── images/
│       │   └── multimodal-manifest.json   # synthesis 才要求
│       └── subagents/<label>/             # 每个子 Agent 同样一套轨迹
└── log/<batch>.manifest.jsonl             # 每条含 run_mode 与接收状态
```

可用 `PPT_RUNS_ROOT`、`PPT_LOGS_ROOT` 和 `WORK_ROOT` 分别覆盖持久 Run、manifest 和本地
高速工作目录。批量合成建议让 `WORK_ROOT` 指向本地 SSD，完成后 Harness 会原子回写
`PPT_RUNS_ROOT`。

## 合成完整性门

合成模式仅在以下条件同时满足时把样本记为 `completed`：

1. Deck、最终渲染、讲稿、播放器和 Review 满足正常交付验收；
2. 编排器及全部子 Agent 均正常写出 `messages.json` 和 `tool_log.json`；
3. 每个轨迹引用的图片快照存在，且 `multimodal-manifest.json` 已记录其字节数和 SHA-256；
4. 任一轨迹缺图或清单不完整时，整条样本记为 `rejected`，不会静默进入训练集。

离线验证双模式合同：

```bash
uv run pytest tests/test_dual_mode.py -q
```
