# MURAL Presenter v1

重构后的单内核 Harness，默认配对 `skills/mural-presenter-v1`。只服务 layout v2；
冻结的 v1.0 与原 workspace 保持独立，不迁移、不删除、不由新代码兼容。

当前状态与本轮验证见 [收口记录](../../docs/mural-v1-consolidation-20260905.md)，
合并前功能差异见 [历史审计](../../docs/mural-v1-harness-audit-20260905.md)。
单一 Skill 入口、分页规划、Owner 续跑与 alpha 审稿进展见 [当前计划](../../docs/clean-skill-persistent-owner-plan-20260908.md)。
旧阶段报告已保留在 [docs/archive](docs/archive)，其时间点结论不代表当前完成度。
唯一维护源码就是本目录；旧候选已移入 archive，不是运行入口。8001 与 release pointer 的
实际发布状态以收口记录为准。

## 入口与职责

- `infer.py`：inference CLI、PresenterKernel、控制工具接线。
- `synthesize.py`：复用同一单 Deck 路径的合成与 JSONL 批处理。
- `runtime/`：唯一 Agent loop、Provider/streaming/recovery、角色工具、附件、Skill object、资源槽及实时轨迹。
- `owner/`、`orchestration/`：Owner 身份/上下文、结构化委派、依赖、求助与恢复。
- `state/`：页面身份、Artifact/lease/同源回执、结构事务与中断回滚。
- `qc/`、`export/`、`validation/`：内容/像素证据、问题闭合、交付/训练准入及机械校验。

语义分组、素材路线、设计与修改决策属于 Agent；Harness 不用关键词替模型判断内容与审美。

## 工作区与共享对象

`_state/workspace.json` 先建立唯一 layout v2 路径映射和 Deck 身份。
当前唯一 Skill 用 `deck.md` 保存整册目标、大纲和完整设计规范，`briefs/slide_NN.md` 保存独立分页正文。
页号从文件名取得，文稿使用普通标题与明确讲稿章节；页型是自然语言叙事意图，不解析为控制字段，也不内联第二套分页计划。
Harness 在页面分配或 Orchestrator 首次写入前自动建立同一最小运行容器，不预置可见标题或按页型选版式；实际构图由页面作者按计划与 reference 实现，初始化事件不进入 Agent 对话。
实际共享样式直接写入 `base.css`，不从 Markdown 的自然语言、表格或 CSS 块编译，也不另建 design.md 或可编辑 JSON 页表。
Owner Packet 提取共享段与所属页的完整原文，并保留实际计划路径和 hash；结构修订操作相同真源。
各 Research 维护任务回执中分配的 `research/*.md`，主 Agent 将本册采用的结论与边界整合到 `knowledge.md`；原始解析证据仍在 `inputs/`，临时文件在 `_runtime/tmp/`。

根 `config.json` 是当前创作设置的唯一来源：working_language、presentation_language、target_page_count，以及按需的 speech_language。
入口参数只用于新 workspace 初始化，未决语言由 Orchestrator 理解 Query 后写入；专家只读，不从 Markdown 或旧 RunSpec 回填。
`_state/run.json` 保留原始运行记录；Provider、权限、并发和状态不放进创作配置。改工作语言只更新后续上下文，
改呈现语言要求原责任方修改并复核，改目标页数不自动增删页面；配置进入现有 checkpoint，完整 JSON 不作为每页视觉资源。

不创建 `plan/`、workspace 内 `skills/`、根 `tmp/` 或作者态
`assets/fonts/`。逻辑命令 `skills/<选中名称>/scripts/*.py` 由 Tool runner
解析到 `_state/skill-ref.json` 固定的不可变 Skill object。字体由共享对象固定引用，
普通制作直接使用共享字体源，便携 ZIP 的 staging 才生成子集和包内 `assets/fonts/`。

`delegate_submit` 用稳定 `task_id` 向持久任务图增量提交，`depends_on` 引用任意上游任务。
Orchestrator 在闭合事务后自然结束一轮即可进入自动等待；原事件条件唤醒同一会话，事件作为真实 Harness 输入，
不伪造工具调用。`delegate_wait` 只保留旧 checkpoint 的执行兼容，不再向新模型公布；没有 Wave 屏障或额外 collect。
重复提交相同 ID 不重复执行，依赖失败的下游标为 skipped；resume 延续任务身份与事件游标。
Owner 求助等待时保留原调用栈并释放执行槽；获得答复后与新任务共用 FIFO，重新受 `max_workers` 限制。
每个实际等待的 Owner 仍保留一个线程。关闭或线程启动失败会记录可恢复中断，不伪造完成或 Provider 失败。
讲稿在分页计划唯一的 `## 讲稿` 或 `## Speaker notes` 正文中修改，结束或 export 时由后台派生 `speech.md`，不要求 Agent 执行收尾命令。
固定 Skill 的 references 是只读知识，按角色当前任务选读；其他页面、私有状态与脚本源码权限不因此开放。

页面只有当前 HTML、PNG、引用资源和 inspection 同源才可 ready。增删/移动页必须走
Harness 的结构事务；Skill 不直接修改 Owner、Artifact 或 Issue Ledger。

## 运行环境

使用 WebUI 的正式 `webui/inference/.venv/bin/python` 执行 Harness、渲染与附件/OCR；
`PPTAGENT_ENGINE_PYTHON`、`NORMALIZE_PY` 应指向同一解释器。
`PPT_FONT_SOURCE_DIRS` 指向部署的字体目录，不依赖交互 shell 偶然安装的包。

`MURAL_SKILL_DIR` 是唯一 Skill 选择变量，直接指向具体 Skill 根目录，而不是它的父目录。
未指定时使用正式 `skills/mural-presenter-v1`；旧 `MURAL_SKILLS_DIR` 不参与选择。
新 workspace 固定选中版本；resume 继续原 `_state/skill-ref.json`，不随环境变量或源码变化换版本。
此前 alpha 已并入唯一的 mural-presenter-v1，原正式版归档；发布仍固定并校验真实字节，不可变对象不含指向活动源码的软链接。
源码修改不代表通过正式发布审核：production 仍检查所选树与已批准 release 的 hash。

Provider 使用 `MODEL_API_FORMAT`、`MODEL`、`MODEL_BASE_URL` 与部署注入的凭证；
`MODEL_STREAMING` 可切换流式。`runtime.env` 仅存公开默认值，不放密钥。
`MODEL_RETURN_TOKEN_IDS` 默认关闭，仅在对应 vLLM 部署明确支持时设为 `1`，
只为流式 Chat Completions 添加顶层 `return_token_ids=true`。token-only 事件不进入
对话内容，仍须获得完整工具参数及正常流结束；此项不能修复长 prefill 或完全无 token 输出时的链路断开。
Qwen3.8-27B 的 `MODEL_REASONING_TRANSPORT=chat_template_kwargs` 将已校验的强度传入
模板：thinking 开启时，`low/medium` 同时传顶层与嵌套同值，`xhigh` 只传模板字段，
避免 LightLLM 顶层枚举拒绝。thinking 关闭时不传 effort。不将已发送参数等同于服务端已确认生效；
部署应对照实际模板或 prompt tokens，不能只看 HTTP 200。当前 LightLLM 不开启 token IDs。
WebUI 的共享通道池、生图和搜索默认值只由根 `.secrets/.env` 维护，派发时转换为
本 Harness 的环境契约；不再读取独立 Opus 池文件或为 v1 生成 dotenv 副本。
`MODEL_CHANNEL_BALANCE=primary-first` 使作者及同池隔离审计采用主备顺序；
瞬态故障走现有 failover，停用账号不会自动重试。

GPT-6 Responses 可按模型单独设置 `SENSENOVA_MODEL5_ROUTE_POOL_PATH`，WebUI 仅对该模型
派发 `MODEL_ROUTE_POOL_PATH`；不设置时原有 Opus/Qwen 纯 key 号池完全不变。
该私有 JSON 使用 `schema: mural.gpt6-route-pool.v1`，顶层 `api_format`、`base_url`、
`model` 必须匹配当前部署。`channels` 每项包含稳定 `id`、实际 `model`、`key_env_file`
及 `key_env`；凭证文件路径相对号池 JSON 解析，密钥仍只存于私有凭证文件。
每个通道必须声明自己真实可用的模型路由；例如 `gpt-6-astra/azure/sfa`、
`gpt-6-astra/azure_L/qwb`、`gpt-6-astra/mix_L/sfj` 或 `gpt-6-astra/azure/sfh`。
目前只支持同一 endpoint 下显式配置的 GPT-6 Responses 路由，不做跨模型系列降级。
选择和失败切换同时绑定模型路由与 key，健康状态按实际模型路由隔离；已有 signed
affinity 仍须遵守恢复边界。Provider 事件与 raw 的 `model` 为实际路由，`requested_model`
为逻辑模型；这些仅属审计元数据，不注入模型对话。不要同时设置旧 `MODEL_POOL_ENV_PATH`。

并发、资源目录与 MinerU 参数唯一来源是根 `configs/runtime.json`，环境仅保留绝对路径指针
`MURAL_RUNTIME_CONFIG`。Owner、Deck 与跨 Deck 资源上限的区别见 [运行并发](../../configs/README.md#运行并发)，
不在 Skill 写死数量。不同 endpoint/model 与隔离审计也共用 Provider 总预算，旧并发/资源 env 不再覆盖。

### 上下文压缩与训练预算

`runtime.env` 的压缩策略由 ProductionSettings 统一读取并记入运行状态：
`COMPACT_TRIGGER_RATIO=0.90`、`COMPACT_TARGET_TOKENS=49152`、`COMPACT_SUMMARY_TARGET_TOKENS=8192`。
合成的有效窗口取 Provider 窗口与 `TRAINING_SAMPLE_MAX_TOKENS=131072` 的较小值；
输入超过软水位，或有效窗口扣除本轮最大输出和安全余量后的容量，或图片/请求字节预算时，在闭合工具边界压缩。
1K 为 1024 tokens：保留主 Agent 40K、Owner 32K 输出与 8K 余量时，输入容量分别为 80K、88K，不是 115.2K。

内部摘要复用现有 Provider、号池、资源槽与 raw 记录，使用 `request_kind=context_compaction`，不开制作工具。
8K 是可见交接目标，不限制含 thinking 的总生成；压后 48K 是完整输入目标。摘要更新当前约束、决策、版本、反馈与下一步，
近期上下文按 token 选择完整交互，并保留未消费和显式保护的图片。必要输入超过软目标但仍合规时记录原因；
摘要失败或必要输入超容量时保留原 checkpoint，不先切段，也不把摘要调用算作 Owner 已看图。

内部摘要请求与结果保留在 raw，不自动成为 PPT 创作监督；实际送入下一段的摘要属于真实训练输入，
继承的 assistant 动作仍为 context_only。可另行评估压缩任务训练集，本入口不混入创作样本。
事件中的压前/压后 token 是估算，Provider usage 另记；最终样本仍须按目标训练模板完整编码验收 128K，超长保留 raw 并报告，不静默截断。

```bash
webui/inference/.venv/bin/python harnesses/mural-presenter-v1/infer.py --query '<任务>' --workspace '<新目录>'
webui/inference/.venv/bin/python harnesses/mural-presenter-v1/synthesize.py --query '<任务>' --workspace '<新目录>'
```

源码变化不能沿用旧 release hash 冒充发布；本地验证与正式 release 校验是不同步骤。

## 附件解析与网页正文

上传只保存原件，Harness 建立轻量索引后后台调用内部唯一解析器，不阻塞 Orchestrator 首轮。
既有附件 manifest 记录 queued/processing/completed/partial/failed；完成通知进入现有事件流。
Research 接手附件理解与必要的外部调研，写入各自分配的研究稿；没有独立 Material 角色或解析 CLI。
正文、来源定位和图片留在 `inputs/`，Research 的理解不替代原始证据。

- PDF、DOCX、PPTX、XLSX 和需要内容提取的 PNG/JPEG 使用独立 MinerU 3.4.5 服务；不保留本地 OCR 第二入口。
- TXT/Markdown/CSV/TSV/JSON/JSONL 保持原件字节；显式 style_reference 不自动解析，网页走 `web_extract`，DOC/PPT/XLS 明确未支持。
- `configs/runtime.json` 的 `mineru.endpoints` 是唯一服务地址配置：恰好两个有序地址，GPU 在前、CPU 在后。
  WebUI、独立 CLI 与服务启动读取同一份 JSON，凭证和模型协议仍保留原敏感配置入口。
- 仅连接失败、超时、不健康或 429/502/503/504 尝试一次 CPU；请求、协议、版本、MIME 错误以及 `partial` 不切换。
  两端失败明确报告，不能让模型逐页视觉 OCR 整份文件。原页仍可按需查看和裁取漏提素材。

`parse` 回执提供实际 `text_path/media_paths/middle_json_path/content_list_path`；解析缓存按原件 hash、
解析器/服务版本、参数、后端及服务身份复用；同任务 CPU 成功后的 resume 不重跑，新 workspace 仍优先 GPU。
Office 的来源单位分别为 DOCX 逻辑块、PPTX 幻灯片、XLSX 工作表；隔离 LibreOffice 派生原生图表，
保留缺缓存公式及受控计算证据，不启用宏或外部链接。不能把 `completed` 理解为材料已被模型核实或所有图片绝对无遗漏。
单份文档页数不受模型视觉上下文预算限制；模型只按需看关键原页，不能用逐页视觉调用代替基础识别。

Research 的 `web_extract` 将完整的结构化网页/PDF 正文保存在 Harness 私有缓存；
`max_chars` 仅截断本次工具展示，Orchestrator/Research 可把返回的 `source_ref` 原样交给 `read_file` 续读，模型不会看到物理缓存路径。
网页不启动浏览器，不能保证动态站点或扫描 PDF 的正文可提取；失败会明确提示，不返回假全文。

## 观测与验证

`_trace/live-events.jsonl` 追加公开角色消息/允许展示的摘要、实时工具事务、Provider 与审计状态。
`tool_use_id` 配对工具起止；WebUI 用 sequence/cursor 增量消费。兼容离线事件、
checkpoint、lineage 和训练准入保留。运行事件和中断判定只以 live journal 为真源；
恢复上下文仍读取私有闭合 checkpoint。
`agent-events`、`provider-events`、`tool-events` 在结束时单向派生，可从 journal 重建，
不再反向导入。认证头在 HTTP 记录入口排除，连接配置只记录无密钥的 manifest；
正文、工具参数和回执不做全文扫描或字符串脱敏，图片按原始字节外置并记录 hash。
Provider 原生签名保留在受限 raw/checkpoint，公开消息及训练消息不包含原生签名块。
展示摘要仍有显式长度限制，不回写 raw。若调用方把秘密主动写入正文或命令输出，
该内容会原样留存；不要将连接密钥放入模型输入、命令参数或输出。

`verification_submit` 的待修项只要求 `positions / problem / suggested_fix`，跨角色时可选
`target`。Harness 绑定当前产物身份并返回路由；已报告问题必须闭合，不能免除当前像素检查。
启用独立 Critic 时，`inspect` 在原工具事务中交回当前 PNG、公开反馈和执行事实；Critic
与 Owner 使用同一模型和 Provider 池，但每次只收到通用 rubric、实际图片、目标默认重点及 question 补充问题。Owner 核实意见并自行
决定修改或结束；无 READY/REVISE 协议、语义否决、人工 think 注入或分歧专属 loss mask。
同版反馈可复用，图片消费、资源哈希和当前 inspection 的机械校验仍保留。

视觉入口仅为 `inspect`：`page` 接受页号或页号数组，`path` 查看素材 / 附件图，两者二选一。
多页使用带页号的联系表，只补渲染缺失或过期 PNG，不以前置单页检查放行；联系表回执不替代全分辨率单页检查。
Orchestrator 可以只读复核已委派页面，不因此获得写权限或原作者检查身份；历史工具名称只用于读取旧轨迹，不提供执行别名。

训练继续使用 `mural.training-segment.v2`：Owner 的意见输入属于 inspect 工具上下文；
独立 `critic` segment 保存实际 rubric、传输图片、公开回复和来源引用，不拼接跨页会话。
原始渲染与传输图片的哈希分别记录，缓存复用不生成新 Critic 样本。
运行期段记录在 `_trace/raw/segments/`，图片与历史产物共用 `_trace/raw/blobs/`；
每个 Owner 的最新私有恢复点仅存一份消息，加载时校验并还原图片，签名及图片上下文不因存储去重丢失。
合成完成不再自动写 training 清单或运行内容 QC。必要视觉 Critic 未完成仍须原地续跑；意见分歧不拒收。
批量 raw 完成后显式导出，清单直接引用原 raw 段和图片，不复制第二份；仍需后处理 QC 才能筛选 clean：

```bash
PYTHONPATH=harnesses/mural-presenter-v1 webui/inference/.venv/bin/python -m export.training '<workspace-1>' '<workspace-2>'
```

该命令拒绝未完成/仍活动的任务，核对当前交付、历史图片、段边界与 lineage，不调用模型或删除恢复点。
旧工作区不自动迁移；新布局必须使用新 workspace，不把旧训练段目录缺失当成可冷启动的空历史。

离线耗用统计只读原轨迹，向 stdout 输出 JSON，不创建新状态或调用模型：

```bash
PYTHONPATH=harnesses/mural-presenter-v1 webui/inference/.venv/bin/python -m export.readable --stats '<workspace>'
```

可传入多个 workspace；按真实请求、角色和页面参与统计，区分排队/调用耗时、不同 API 的缓存 Token 口径及缺失覆盖。
并发耗时之和不是整册运行时间，工具成功不是检查闭合；未记录的价格、质量改善和单问题修复成本不推断。

批处理在实际开始/结束时逐 attempt flush/fsync，用稳定 attempt ID 去重；resume
会补记磁盘已有的完成结果。中断的尝试计入预算，不默认为零次重试。

测试入口为 `tests/`；正式环境下运行测试，并把 basetemp 指向显式的工作区临时目录，
不使用 `/tmp`。真实 Provider、复杂附件、WebUI 截图对照另做 canary，不能用 mock
覆盖率宣称完整生产验收。

纯逻辑测试按需执行，不启动浏览器。通过 WorkspaceState 间接读取真实 CSS 的测试，以及使用
`browser_environment` 的渲染/导出测试，会在首次实际需要浏览器时做一次 8 秒上限的 CSS/DOM
预检；成功按本进程环境复用，失败以 `browser_preflight_failed`、退出码 4 中止本轮，而不是
让每个用例重复等待 45 秒或静默 skip。新增直接调用 Playwright 的测试也应使用该 fixture。
此退出不是测试通过：先核对依赖、端点和本机进程/IPC 权限，再在获授权的环境重跑；不修改
生产沙箱限制，不上调业务超时。测试不继承正式浏览器端点，临时浏览器仍与运行中的 Deck 隔离。

回归分三层，不按文件年龄删除有效合同：下面的生成核心组是每次冻结必跑门；改到渲染、
画布、字体、导出或训练转换时，再补该领域的真实集成文件；完整历史矩阵只在大版本、定时
任务或跨领域改动时运行。2026-09-15 当前核心组 951 passed，pytest 58.30 秒。完整套件
已有 2927 项并混合大量浏览器/导出矩阵，不能再作为每个窄发布的同步前置，也不能把环境
失败或旧内部表示断言误报成生产逻辑失败。保留尺寸、协议、恢复和 hash 覆盖；重复用例应
合并到共享夹具或参数矩阵，外部行为相同的旧实现断言应更新或移出核心门。

以下命令从仓库根目录运行；沿用正式 inference Python，本地临时目录不与服务共享：

```bash
regression_daily_tmp=$(mktemp -d /workspace/hejiatong/codex-home/tmp/harness-daily-fast.XXXXXX)
TMPDIR="$regression_daily_tmp" PYTHONDONTWRITEBYTECODE=1 \
  webui/inference/.venv/bin/python -m pytest -q -p no:cacheprovider \
  --basetemp="$regression_daily_tmp/pytest" --junitxml="$regression_daily_tmp/daily.xml" \
  harnesses/mural-presenter-v1/tests/test_{config,startup,environment_contract,doctor_cli,attachment_intent,material_parse_contract,attachments_v2}.py \
  harnesses/mural-presenter-v1/tests/test_{tools,tool_catalog,session,context_compaction,agent_runtime,provider_transports,anthropic_deadline,openai_deadline}.py \
  harnesses/mural-presenter-v1/tests/test_{orchestration_manager,orchestration_deadlock,owner_queue_scheduling,owner_continuation,owner_context,owner_registry,owner_interruption,runtime_owner_shutdown}.py \
  harnesses/mural-presenter-v1/tests/test_{resource_slots,recovery,raw_agent_trace,live_trace,trace_costs,review_queue_budget,release_guard}.py \
  harnesses/mural-presenter-v1/tests/test_{skill_selection,light_skill_contract,shared_entry,shared_module_concurrency,visual_context}.py
```

本次计时、热点和领域清单见
`artifacts/validation/mineru-unified-20260909/regression-runtime-audit.md`；每片的 JSON/XML
保存精确命令与结果。慢项主要是真实渲染/导出集成测试，并非大量纯函数测试。
