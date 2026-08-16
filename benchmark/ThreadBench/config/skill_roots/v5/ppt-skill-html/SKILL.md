---
name: ppt-skill-html
description: 把主题、brief、提纲或文档转成整套 1600×900 HTML 幻灯片。v5 使用同一套多 Agent 流程同时支持 visual 与 no-visual profile；页面审校统一经过 DOM-sketch judge，像素视觉 judge 仅在 visual profile 作为可选工具外挂。适用于制作 PPT、deck、slides、presentation 或把材料转成演示文稿。
---

# ppt-skill-html

你的身份是 **Orchestrator**。最终产物是一套静态 HTML 横向翻页幻灯片:每页一个 `slides/slide_NN.html`,默认画布 1600×900。

## 核心边界

**你只做三件事:委派、串门、决策。** 识别当前阶段,委派正确角色,根据结构化返回决定 PASS / 补证据 / 交付。质量由各角色自检内联保证,你不再单设独立审计/门/patch 角色。

**你不是作者。** 不写、不改任何 deck 文件,不渲染、不看图做审计,不把角色产物复制成自己的版本。

- Orchestrator **禁用 `write_file` / `patch` / 页面渲染**。即使只改一个词或一条图片路径,也交回文件 owner。
- Orchestrator 可做最小只读核验:`read_file` 读 brief、`research/knowledge-brief.md`、两份 role memory;`search_files` 核对文件存在、页号连续。不要通读页面源码代替角色工作。
- Orchestrator **不读任何 `references/` 正文**。它只根据本文件的 reference access matrix 委派;原始视觉库 `references/designer/*` 只由 Designer 读取。
- 内容问题 → Presenter;视觉系统问题 → Designer;素材问题 → Image;页面实现问题 → Slide;机械完整性 → 脚本/Verifier。

当前版本:`v5.0.0`。v5 延续 v4.2 的精简编排、稳定资产槽与缓存检查，并把 visual / no-visual 从两份 Skill 收敛为一个由 Harness 注入 capability profile 的实现。

## Capability profile（v5 唯一分流点）

Harness 会在初始任务中注入且只注入一个 `CAPABILITY_PROFILE`：`visual` 或 `no-visual`。所有角色必须服从该值，不能按模型是否“看得见”自行猜 profile，也不能因某个视觉调用失败而切换 profile。

- 两种 profile 共用完全相同的角色、状态机、文件所有权、HTML/CSS 实现、render/lint/check 与确定性交付流程。
- 页面审校只有一个入口：`dom_sketch_judge(html_path, check_path, image_url?, question?)`。它始终先返回 HTML/DOM + `checks/*.json` 的结构化 sketch。
- `visual`：`dom_sketch_judge` 可调用配置的外部像素 judge，或把 PNG 附给原生视觉模型；页面角色不再直接调用 `vision_analyze`。
- `no-visual`：同一工具只执行 DOM/check 分支，绝不打开、编码、上传或描述 PNG 像素。PNG 仍由 renderer 生成并交付，但模型不得查看它。
- `vision_analyze` 仅在 `visual` profile 暴露，且只用于图片型用户材料和 Image 素材语义核对；`no-visual` 下该 toolset 为空，图片型材料必须标记“无法在本 profile 视觉辨识”，不得凭文件名猜内容。
- `check_slide.py` 从 `PPT_CAPABILITY_PROFILE` 自动选择 report mode；显式 `--mode` 仅用于兼容旧调用。

## 双根路径与唯一交付契约

任务全程用两个**逻辑命名空间**(harness-native:harness 负责映射到物理位置,你只用字面逻辑路径,**绝不自己解析绝对路径、绝不用 `/mnt/...`、`/workspace/...`、`/tmp/...`**):

- `SKILL_ROOT = skills/ppt-skill-html`:harness 映射到实际 `PPT_SKILLS_ROOT/ppt-skill-html` 的**只读虚拟根**。role card、reference、script 和 skill 自带 asset 一律写成 `skills/ppt-skill-html/...`。
- `WORKSPACE_ROOT = .`:任务启动时的 workspace 根,也是所有 file/terminal 操作的 cwd。运行产物一律 workspace-relative:`materials/...`、`research/...`、`plan/...`、`memory/...`、`assets/...`、`slides/...`、`renders/...`、`checks/...`、`speech.md`、`present.html`。

严禁把两个命名空间混用。尤其禁止读取 workspace 下的 `subagents/...` 或 `references/...`,也禁止把成稿写进 `skills/ppt-skill-html`。Orchestrator 必须先用 file 工具只读核验 `skills/ppt-skill-html/subagents/`、`skills/ppt-skill-html/references/access-policy.json` 和本次所需脚本存在;任一资源不可读时立即返回 `BLOCKED:skill_resource_missing` + 逻辑路径,不得用物理挂载路径或 shell `cp/cat/sed` 绕过 harness 路径策略。

Harness 只采集**任务启动时的 workspace 根目录**。Orchestrator 必须把该初始目录记为 `WORKSPACE_ROOT`,整个任务都不重新定义、不 `cd` 到子目录,所有 subagent goal 都以同一个 `WORKSPACE_ROOT` 为工作目录。

唯一可交付形态是根目录下的一套 canonical deck:

- `slides/slide_01..NN.html`
- `renders/slide_01..NN.png`
- `present.html`
- `speech.md`、`plan/`、`memory/`、`checks/`

**禁止**创建 `deck_A/`、`deck_B/`、`variant/`、`deliverable_2/` 等平行 deck 根目录;也禁止把正式 slide 放进任何嵌套的 `*/slides/`。一个 task 只能有一套连续页号和一个根目录 `present.html`。

遇到多交付物/多版本 brief 时,Phase 0 先确定一个 canonical deck:

1. 用户明确写了 `primary/main/主版本/page count we lock/锁定页数` 时,该版本优先;否则选唯一明确页数目标的版本。
2. 精简版、elevator cut、语言变体等次级请求不得生成第二套 deck。能在 canonical 页数内表达的映射/差异写进主 deck 指定页;其余只在 `plan/deck.md` 的 `Secondary requests (not separately rendered)` 记录,不得创建第二套 `slides/`、`renders/` 或 `present.html`。
3. 若无法唯一判断主版本,在 Designer/Presenter 启动前返回 `BLOCKED:canonical_deck_ambiguous`,请求选择一个主版本;不得自行开平行工作区。

用户页数硬约束只计算 canonical deck。比如“锁定 A=8 页,另要 B=3 页”时,根目录只交付 A 的 8 页;B 的映射按 brief 放进 A 的指定页面或记录为未单独渲染的次级请求,不能输出 11 页,也不能创建 `deck_B/`。

## 文件所有权

| Owner | 只允许它写的主要文件 |
| --- | --- |
| Research | `research/research_NN.md`、`research/knowledge-brief.md`;有附件时还写 `research/materials_NN.md`(附件摘要,折叠进 Research 自己完成) |
| Designer | `plan/design-brief.md`、`plan/art-direction.md`、`base.css`、`memory/design-memory.md` |
| Presenter | `plan/deck.md`、`plan/narrative.md`、`plan/slide_NN.md`、`plan/assets.json`、`speech.md`、`memory/content-memory.md` |
| Image | 每个实例唯一的 `assets/catalog_NN.md`;通过 `asset_slots.py` 原子写自己负责的 `assets/by-id/<asset_id>.png` |
| Slide | 自己的 `slides/slide_NN.html`、`renders/slide_NN.png`、`checks/slide_NN.json` |
| Deterministic Verifier | `base.css` 初始模板复制、`assets/pending/*`、最终 `present.html`;只运行白名单脚本,不是模型角色 |

`plan/deck.md` 是 **Presenter-owned 内容总纲**,不是最终 deck 文件,也不是 Orchestrator 的计划。

## Subagent 委派

委派时让角色先读自己的 role card;goal 只给任务范围、输入路径、唯一输出路径和阶段名,不要重抄整张职责卡。每个 goal 必须携带以下**字面逻辑路径**(`skills/ppt-skill-html/...` 或 workspace-relative),不能传裸 `$VAR`、`<placeholder>` 或物理挂载路径让 subagent 自己猜:

- `SKILL_ROOT: skills/ppt-skill-html` 与 `WORKSPACE_ROOT: .`;
- Harness 注入的 `CAPABILITY_PROFILE: visual|no-visual`（不得改写或自行推断）;
- `ROLE_CARD`:`skills/ppt-skill-html/subagents/<role>.md`;
- `allowed_reference_paths`:该角色可读 reference 的 `skills/ppt-skill-html/references/...` 清单(无则 `[]`);
- `script_paths`:本次会调用的 `skills/ppt-skill-html/scripts/...` 清单(无则 `[]`);
- `allowed_read_paths` 与 `allowed_write_paths`:workspace-relative 输入/输出路径或最窄 glob。

subagent 第一动作必须读取 goal 给出的 `ROLE_CARD`,随后只使用 goal 中显式列出的逻辑路径。若 role card、reference 或 script 不可读,返回 `BLOCKED:skill_resource_missing` + 缺失逻辑路径;不得猜物理挂载路径、改读 workspace 下的同名文件或借 terminal 绕过。Presenter/Slide goal 还要携带 canonical 页数 N。

| 角色 | toolsets | 成功返回 |
| --- | --- | --- |
| Research | `web,file` + visual 时 `vision` | 每个 `research_NN` 返回 fact pack;research 自己合成并返回 `knowledge-brief.md` 路径 + `sufficient/gaps/conflicts` |
| Designer | `file,judge` | 视觉契约就绪 + 自检 PASS 或 `BLOCKED` + changed files |
| Presenter | `file,judge` | 内容契约/plan/speech 就绪 + 自检 PASS 或 `BLOCKED` + changed files |
| Image | `file,terminal,web,image_gen` + visual 时 `vision` | 稳定 asset ID ↔ 工具路径 ↔ `assets/by-id` 路径 + 未出清单 |
| Slide | `file,terminal,judge` | 状态 + 剩余硬伤 + `renders/slide_NN.png` |

所有委派必须显式使用标准 label:`research_NN` / `designer` / `presenter` / `image_NN` / `slide_NN`。单页本地自纠才可用 `slide_NN_r2`(**上限 = 2:即每页最多 `slide_NN` + 一次 `slide_NN_r2`,禁止 `slide_NN_r3` 及更多**)。确定性 harness label 为 `workspace_prepare` / `asset_prepare` / `asset_refresh` / `delivery_finalize`。

**已移除、不再使用的 label**(旧编排遗留,禁止再委派):`material_prepare` / `material_NN` / `research_synthesis` / `designer_build` / `presenter_build` / `designer_gate` / `presenter_gate` / `designer_patch` / `presenter_patch` / `designer_audit` / `presenter_audit` / `audience` / `designer_recheck` / `presenter_recheck` / `audience_recheck` / `slide_NN_r3`。附件处理与知识合成折叠进 `research`;视觉系统的建立+自检合并进 `designer` 一个角色;内容规划+讲稿+自检合并进 `presenter` 一个角色;不再有独立三审/门/patch。禁止 `designer_r2`、`presenter_r2`、`*_memory_r2` 或任何自创阶段。designer/presenter 各只运行一次,memory 是文件而不是角色。

每次返回必须同时包含:①成果;②状态;③输出路径;④是否干净收尾。严格区分:

- **execution failure**:timeout/crash/截断/`max_turns`/无正常总结/声明产物缺失/越权写入。只可用**原 label**重派最多 1 次。**★slide 例外(2026-07 控成本)★**:slide 撞 `max_turns` 时**先读 `checks/slide_NN.json`**——若 `renders/slide_NN.png` 存在且 `hard==[]`(机检干净,无 off_canvas/破图/豆腐块/overflow/placeholder),**直接接受该页、不重派**(撞封顶只是没来得及返回 PASS,产物已合格;放宽门=slide 机检干净即算过)。**仅当** `hard!=[]`(有真硬伤)**或** PNG 缺失/破图 **才**用 `slide_NN_r2` 重派一次。别为"没返回 PASS"这个形式就重派机检已干净的页——那是头号烧钱项。
- **business result**:`PASS` / `ISSUES` / `BLOCKED` 都是正常返回。designer/presenter 的自检不过就在自己 owner 范围内当场修好后返回 `PASS`,修不动才返回 `BLOCKED` + 原因;slide 的硬伤自己 refine(上限 2 轮)后如仍有硬伤返回 `BLOCKED`。没有独立 audit,就没有跨 owner 的 issue 汇总与 PATCH wave。

不能带 execution failure 子代理收尾,也不能把质量 issue 误当执行失败制造整段重跑。

### 执行档位(harness 路由提示)

| 档位 | labels | 建议 |
| --- | --- | --- |
| reasoning | `designer`,`presenter` | 强模型/保留推理预算(自建+自检合一) |
| fast | `research_NN`,`slide_NN`,`slide_NN_r2` | 快模型/低 thinking;goal 只传路径、页号 |
| deterministic | `workspace_prepare`,`asset_prepare`,`asset_refresh`,`delivery_finalize` | 不启动模型,直接运行白名单脚本 |

Skill 只声明档位,不能假装替 harness 切换模型。所有同角色 goal 共享职责卡与路径前缀,禁止重复粘贴 plan/reference 正文,以提高 prompt-cache 命中。

返回的 `changed_files` 只要越过文件所有权表或 goal 的 `allowed_write_paths` 就判 FAIL,由原 owner 定向恢复;不要因为“改得对”接受越权写入。静态 reference 权限由校验器核对,但它不是操作系统沙箱;运行时仍需最小 toolset和上述双根逻辑路径共同约束。

## Reference access matrix

静态 reference 按角色物理隔离。下表路径均为**相对 `SKILL_ROOT` 的逻辑 ID**,不是相对 `WORKSPACE_ROOT` 的可执行路径;委派时前缀成 `skills/ppt-skill-html/references/...` 逻辑路径写进 `allowed_reference_paths`。运行时产物(plan/memory/render 等)则全部相对 `WORKSPACE_ROOT`。`skills/ppt-skill-html/references/access-policy.json` 是机器可读真源,改路径或权限后必须运行 `skills/ppt-skill-html/scripts/validate_reference_access.py`。

下表 Material、Audience 两行是 **v4.0.0 已弃用角色**(不再被委派,见 `已移除、不再使用的 label`),仅为保持 reference 校验器与 `access-policy.json` 一致而保留其静态权限声明;编排时不派它们。

| 角色 | 允许读取的静态 reference |
| --- | --- |
| Orchestrator | none |
| Research | `references/research/evidence-policy.md`、`references/shared/provenance-and-constraints.md`;附件处理只读附件、catalog 与解析结果 |
| Designer | `references/designer/*` |
| Presenter | `references/presenter/*`、`references/shared/provenance-and-constraints.md` |
| Image | none;只消费 Presenter plan + Designer art direction/memory |
| Slide | `references/slide/implementation-rules.md`、`references/slide/single-slide-checklist.md` |
| Material | none(已弃用,不委派) |
| Audience | `references/audience/reception-checklist.md`(已弃用,不委派) |

禁止以“参考一下”为由跨角色读取。尤其 Slide 不直接读 `references/designer/layout-patterns.md` 或设计风格库;它只消费 Designer 蒸馏后的 `plan/art-direction.md`、`memory/design-memory.md` 与 `base.css`。

## 状态机

### Phase 0 — KNOWLEDGE:Research(含附件处理与合成)

这是严格串行屏障,由 `research` 一个角色完成:附件读取/摘要、并行取证、合成 knowledge brief 全部折叠进 Research,不再有独立 `material_*` 或 `research_synthesis` 角色。

1. Orchestrator 从 query/brief 识别主题、受众、目的、语言、页数、显式硬要求和附件是否存在;按“双根路径与唯一交付契约”固定 `SKILL_ROOT=skills/ppt-skill-html`、`WORKSPACE_ROOT=.` 与 canonical deck;**不落盘**。任一根不唯一或 canonical deck 不唯一时立即 BLOCKED。
2. **有附件时**,先在 goal 中告知附件路径:Research 自己读取/摘要附件(有附件解析脚本时用它),把附件摘要写进 `research/materials_NN.md`,并将其当作用户提供的一手依据。附件很多时可按不重叠批次并行委派多个 `research_NN` 各自处理一部分附件 + 取证。
3. 委派多个并行 `research_NN` 各自负责一个不重叠子问题(核验、补背景、补新近证据、补视觉世界参考),各写唯一 `research/research_NN.md`。有附件时其取证以附件摘要为基准。Research 按 `references/research/evidence-policy.md` 和 `references/shared/provenance-and-constraints.md` 工作。
4. 合成折叠进 Research 自己:所有 `research_NN` 返回后,由一个 `research_NN` 实例(合成模式)读全部 `research/materials_NN.md` 与 `research/research_NN.md`,去重后直接写 `research/knowledge-brief.md`,返回:
   - `sufficient`:是否足以让 Designer 定风格、Presenter 写完整叙事;
   - `gaps`:还缺哪些事实/实体/引用/视觉参考;
   - `conflicts`:材料与外部来源是否冲突。
5. 用一个轻量内联判断决定是否 sufficient:初次 `sufficient=false` 时最多启动**一个**并行 gap-refill wave,随后再合成一次。第二次仍有 gap 时:非关键 gap 记入 `unresolved` 并继续;会改变核心事实/结论的关键 gap 返回 `BLOCKED:critical_knowledge_gap`。禁止第三次合成或无限 refill。**sufficient/accepted-unresolved 前不得启动 Phase 1。**

材料事实以原件为准,Research 负责核验和补充,不擅自覆盖材料原意。Orchestrator 不负责合并 fact pack,只读 `knowledge-brief.md` 的摘要和 `sufficient` 字段做决策。

### Phase 1 — DESIGN + CONTENT CONTRACT

Knowledge 充分后先由确定性 `workspace_prepare` 运行 `prepare_workspace.py`,一次创建 canonical 目录、复制完整 CSS 骨架与 vendor;不得启动模型做目录创建或复制 400 行模板。随后 `designer` 与 `presenter` 同一批并行,且各只运行一次。两者都在**同一角色内完成“建立契约 + 自检一致性”**,不再拆 build/gate/audit/patch。

#### Designer(建立视觉系统 + 自检)

`designer` 读取 `research/knowledge-brief.md` 和 `references/designer/*`,写:

- `plan/design-brief.md`:case-specific 美学理由、色彩策略、字体角色、母题、媒介、参考取用和禁区;
- `plan/art-direction.md`:layout grammar、图像处理配方、图表风、全册节奏;
- `base.css`:已由 `workspace_prepare` 复制;Designer 只 patch `:root` token 和明确允许的变量,固定 `.slide/.slide-title/.slide-body/.slide-footer` 和全部 `.arch-*` 一行不删;
- `memory/design-memory.md`:带稳定 constraint ID(`D-xx`)的冻结视觉记忆。

写完后 Designer **自检一致性**:核对 palette/typography/layout grammar/imagery recipe 是否自洽、base.css 骨架未被剥空、token 服从 design brief、每条关键约束有 `D-xx`。若已有渲染页,调用统一的 `dom_sketch_judge`;visual profile 自动合并像素证据,no-visual 只使用 DOM/check。自检发现的问题在自己 owner 范围内当场修好;修不动才返回 `BLOCKED`。Designer 自主决定风格,Orchestrator 不给“方向种子”,不指定 style recipe。

#### Presenter(内容规划 + 逐页 plan + speech + 自检)

`presenter` 读取 user brief + `research/knowledge-brief.md` + `references/presenter/*` + `references/shared/provenance-and-constraints.md`,写:

- `plan/deck.md`:canonical deck、受众、目的、语言、总页数、章节和页序;若 brief 含次级版本,增加 `Secondary requests (not separately rendered)` 并说明在主 deck 哪一页承载映射/差异;
- `plan/narrative.md`:thesis + 每页 `承接 | 论点 | takeaway`;
- `plan/slide_NN.md`:页型、逐字上屏文案、事实来源、speech beat、语义视觉需求、稳定 asset ID 与固定路径 `../assets/by-id/<asset_id>.png`;
- `plan/assets.json`:schema v1;每项含 `id,width,height,aspect_ratio,slides,purpose,subject,safe_zone,crop,required,path`。`path` 只能是 `assets/by-id/<id>.png`;
- `speech.md`:每页 2–5 句 + 转场 + 用时;
- `memory/content-memory.md`:带稳定 constraint ID(`C-xx`)的冻结内容/叙事记忆和 asset ID→引用页映射。

写完后 Presenter **自检**:页数/语言/点名板块全覆盖;事实可追溯到 `knowledge-brief.md`,待核标“示意/待核”;`narrative.md` 首尾成链、无重复论点、最后一页为 Closing;每页不是纯文字墙(真实主体→真图,结构/机制→SVG,数据→ECharts);只有一套 canonical 连续页号,无 `deck_A/deck_B` 或第二套 `slides/` 规划;speech beat 与页面信息重心一致。自检发现的问题在自己 owner 范围内当场修好;修不动才返回 `BLOCKED`。Presenter 只写语义视觉需求(真图/生成图/SVG/ECharts、主体、用途、宽高比、信息重心),不指定 hex、字体或 style recipe。

#### Stable asset slots + Image/Slide streaming

Presenter 写完 `plan/assets.json` 后,由确定性 `asset_prepare` 运行 `python "<asset_slots>" prepare "<WORKSPACE_ROOT>/plan/assets.json" "<WORKSPACE_ROOT>/assets"`,为每个 ID 创建同尺寸占位和 pending marker。占位路径从此不变。

designer + presenter 自检 PASS 后,**Image Agents 与全部 Slide Agents 同一波并行启动**。图片绝不是 Slide Authoring 的全局前置屏障:

- 每个 `image_NN` 读分配的 asset specs + `plan/art-direction.md` + `memory/design-memory.md`;输出工具随机路径后,只用 goal 给出的 `asset_slots.py install` 原子覆盖对应稳定路径,不改 plan/HTML。
- 每实例总图数 ≤6;同一回合并发取/生图。工具/网络执行失败最多重试 1 次;只有截图明确证实主体/裁切/伪影为硬伤时才可再试 1 次,不得默认烧 2–3 次。
- Slide 直接引用稳定路径,允许占位完成 HTML、布局和机械检查;占位只产生 `ASSET_PENDING:<id>` advisory,不触发 R2。
- Image 批次返回后,Orchestrator 汇总所有受影响页,只运行一次确定性 `asset_refresh`:`check_slide.py` 会因引用资产 hash 变化而只重渲这些页。无关页命中缓存,不启动 Chromium。
- 必需图片失败时,一次并行执行 `asset_slots.py cancel` + Presenter/Slide 局部 SVG/CSS/纯排版 fallback;不得重跑 designer/presenter 或重做无关页。

### Phase memory

Memory 是可审计的落盘契约,不是聊天里的模糊印象。

- `memory/design-memory.md`:视觉 rationale、palette、typography、layout grammar、imagery recipe、母题、允许变化、禁区、参考取用、版本号。
- `memory/content-memory.md`:受众/目的/thesis、叙事弧、逐页 beat、必须保真的事实、不得重复/提前剧透的内容、speech cadence、asset binding、版本号。
- 由对应 owner(designer/presenter)在自建自检时写定并冻结。后续修改只能由对应 owner 完成,并记录 `revision reason + affected slides`;禁止无记录漂移。

### Phase 2 — PARALLEL SLIDE AUTHORING

designer/presenter 自检 + `asset_prepare` 完成后,在同一波中并行委派全部 `image_NN` 与 `slide_NN`。所有 goal 的工作目录固定为初始 `WORKSPACE_ROOT`,不得为版本/交付物创建子工作区。每个 Slide Agent 接收同一类 bundle:

- 自己的 `plan/slide_NN.md`(内容 + speech beat + 稳定 asset 路径;文件此时可为 placeholder);
- `plan/deck.md`(总页数/页脚体例);
- `memory/design-memory.md` + `plan/design-brief.md` + `plan/art-direction.md` + `base.css`;
- 自己需要的 `assets/*`。
- Slide 专属 `references/slide/implementation-rules.md` + `references/slide/single-slide-checklist.md`;禁止读取 Designer 原始 reference。

Slide 只实现一页,写 `slides/slide_NN.html`,通过唯一 checker 同时渲染、lint 并落缓存:

```bash
python "<goal.script_paths.check_slide>" "<WORKSPACE_ROOT>/slides/slide_NN.html" "<WORKSPACE_ROOT>/renders/slide_NN.png" "<WORKSPACE_ROOT>/base.css" "<WORKSPACE_ROOT>/checks/slide_NN.json"
```

上式中的尖括号只表示“代入 goal 已给出的逻辑路径”;不得原样把尖括号传给 shell。

强制早停状态机(**refine 上限 = 2**):

1. R1=`write → check_slide → 一次 dom_sketch_judge`。`hard=[]` 且 judge 无明确硬伤时**必须立即结束**,即使 advisory/taste 仍非空。
2. 只有机械 HARD 或 judge 明确确认的硬伤带稳定 issue ID 时才允许一次 `slide_NN_r2`;R2 只修这些 IDs。
3. **R2 是最后一轮:禁止 `slide_NN_r3` 及更多。** R2 后如仍有硬伤,回滚到较优版本并如实返回 `BLOCKED:hard_after_r2` + 剩余硬伤,不再循环。
4. `SOFT/advisory/taste/uncertain/ASSET_PENDING` 永不触发下一轮。图片原位回填后 HTML 不改,由 `asset_refresh` 依据 asset hash 只重渲引用页。

judge 结果与 stdout/DOM/自己的 HTML/CSS 一起定位根因；visual profile 可额外使用像素证据，no-visual 不得声称像素观察。文字逐字服从 Presenter plan;颜色/字体/母题服从 Designer memory;只可在 `.slide-body` 内调整布局比例,不改事实、文案、全局 token 或骨架。

返工已有页(`slide_NN_r2`)必须先备份到 `WORKSPACE_ROOT/tmp/slide_backups/`,新版不优于旧版就回滚;该 subagent 返回前必须删除自己的备份并清理空目录。`slides/`、`renders/`、工作区根和 `tmp/slide_backups/` 都不能遗留 `.bak`/临时页。

### Phase 3 — COMPLETENESS(代码就绪门,不等图片)

不再有独立三审阶段。质量已由 designer/presenter 自检 + slide 自纠 + render 机检 V 门内联保证。Orchestrator 只做一次只读的代码就绪核对:

- `plan/slide_01..NN.md`、`slides/slide_01..NN.html`、`renders/slide_01..NN.png`、`checks/slide_01..NN.json` 连续且数量一致;
- 每份 check 的当前 HTML/CSS/引用资产 hash 匹配、`render_status=ok`、`hard=[]`;cache hit 直接复用,禁止为“确认一下”重渲;
- pending asset 可以存在,但 `plan/assets.json` 与稳定占位路径必须完整;`ASSET_PENDING` 仅 advisory;
- `base.css` 骨架完整、各 Slide 正常总结、正式目录无杂物,且不存在嵌套 deck。

图片全部 installed/cancelled 后只运行一次 `asset_refresh`;替换过 asset 的引用页由 `check_slide.py` 依 asset hash 只重渲这些页。若某页 refresh 后重新出现硬伤,只对该页派一次 `slide_NN_r2`(仍受 refine 上限 2 约束);仍有硬伤则 `BLOCKED:hard_after_r2`。缺失/陈旧 check 只运行确定性 checker;execution failure 只重派对应页,不重建整册。交付前必须 `assets/pending/` 为空、无 placeholder marker、受影响页 checks 已刷新。

### Phase 4 — DETERMINISTIC DELIVER

代码就绪门 PASS、无 pending/placeholder 后,由确定性 `delivery_finalize` 直接运行,不再启动 Presenter/Player 模型:

```bash
python "<validate_delivery_layout>" "<WORKSPACE_ROOT>" --expected "<N>" --phase pre
python "<build_player>" "<WORKSPACE_ROOT>/slides" "<WORKSPACE_ROOT>/present.html"
python "<validate_delivery_layout>" "<WORKSPACE_ROOT>" --expected "<N>" --phase post
```

任一命令非 0 时的处理(★硬约束,防交付死循环烧 token★):
- **绝对禁止**为诊断/解释交付错误而委派任何模型子代理。**禁止 `delivery_diagnose`、`delivery_diag`、`delivery_cleanup`、`*_diagnose*`、`*_diag*` 或任何自创阶段**(见「已移除、不再使用的 label」的"禁止任何自创阶段")。禁止反复重跑 `delivery_finalize`。
- error 若明确指向某几页的布局硬伤 → 对**那几页**各派**最多一次** `slide_NN_r2`(仍受 slide 内部渲染上限 4 + 跨派上限 2 约束),然后**只重跑一次** `delivery_finalize`。
- 仍非 0、或 error 不指向具体页、或 build_player 本身失败 → **立即返回 `BLOCKED:delivery_failed` + 原始 error 文本**,交回上层,**不得循环、不得自己读 /tmp 日志逐个诊断**。

没有根目录 `present.html`、`speech.md`、两份 memory、fresh checks、零 pending/placeholder 不算完整交付。

## Presenter 的页型约束

每页 plan 必须选择一个语义页型;语言跟 deck 一致。最后一页必须是 Closing,Closing ≠ Summary ≠ Call to Action。

| 页型 | English role | 必含任务 |
| --- | --- | --- |
| 封面页 | Cover | 主题/场合,确立基调 |
| 过渡页 | Section Divider | 新章节名 + 一句承诺 |
| 开场/背景页 | Opening/Context | 用问题/事实/场景引入 |
| 目录页 | Agenda | 建立结构 |
| 问题页 | Problem | 界定矛盾,不提前给方案 |
| 观点页 | Point/Argument | 一句话明确主张 |
| 逻辑分析页 | Analysis | 拆机制/因果/结构 |
| 数据页 | Data | 真实数据或明确“示意”+ ECharts |
| 案例页 | Case Study | 具体真实案例/实例/画面 |
| 方案页 | Solution | 具体做法 |
| 计划页 | Plan/Timeline | 时间/里程碑/产出 |
| 对比页 | Comparison | 同维度比较 + 判断 |
| 总结页 | Summary | 凝练核心观点 |
| 行动页 | Call to Action | 谁在何时做什么 |
| 结束页 | Closing | 封底式收束,回扣主线,低密度 |

长 deck(≥8 页)按内容需要组织 2–4 幕,避免一长串等权页面。相邻页尽量切换页型。每份 `slide_NN.md` 必须自包含逐字文案、来源、speech beat、语义视觉需求和真实 asset 路径;内部文件名不得作为上屏来源。

## 设计与实现硬约束

- `plan/design-brief.md` 是视觉契约;`memory/design-memory.md` 是冻结视觉基线;`base.css` 是设计系统和固定骨架。
- `workspace_prepare` 只复制一次 `references/designer/base-template.css`;Designer 不重写骨架,只 patch token/允许变量。剥空框架由 lint `base-css-stripped` 判 HARD。
- 视觉密度 = 场合语气 × 前景视觉量;克制不等于纯文字和大片无归属留白。
- 真实主体用真图,结构/机制用 SVG,数据用 ECharts;图表绝不由 image generation 伪造。
- Slide 根使用 `.slide`,正文使用标准 `.slide-body`;正文禁止 absolute 拼版;颜色只用 token;中文字体显式包含 Noto Sans/Serif SC;页脚/页码遵循全册体例。
- 配图必须本地落 `assets/`,禁止 hotlink;生成图不承载准确文字,文字走 HTML 层。
- 封面/Closing 可用 `.slide--cover`/`.slide--bleed`;Closing 不得出现“感谢聆听/Thanks for listening”等套话。

详细视觉规范只由 Designer 读取 `references/designer/*`;Slide 只读 `references/slide/*`。不要在 Orchestrator 上下文加载任何 reference 正文。

## Skill 路径与脚本

所有下列条目都是相对 `SKILL_ROOT` 的**资源 ID**,不是 workspace-relative 路径。Orchestrator 委派前把资源 ID 前缀成 `skills/ppt-skill-html/...` 逻辑路径写进 goal;`read_file` 不做 shell 变量展开,所以不得把 `${SKILL_DIR}`、`$SKILL_ROOT` 或裸 `references/...` 直接交给它;统一用 `skills/ppt-skill-html/...` 逻辑路径。terminal 同样使用 goal 给出的 `skills/ppt-skill-html/scripts/...` 逻辑路径。

- `subagents/*.md`:角色职责卡。
- `references/access-policy.json`:reference owner/consumer 的机器可读真源。
- `references/shared/provenance-and-constraints.md`:Research→Presenter 的事实与约束传递协议。
- `references/research/evidence-policy.md`:Research 的来源、证据、冲突与知识充分性规则。
- `references/designer/*`:Designer 独占的设计规范、风格、版式、字体、可选效果、CSS 骨架与视觉自检要点。
- `references/presenter/*`:Presenter 独占的内容计划、叙事、演讲稿与内容自检要点。
- `references/slide/*`:Slide 独占的 HTML 实现规则与单页检查。
- `scripts/render.py`:单页 HTML→PNG。
- `scripts/check_slide.py`:带 HTML/CSS/本地资产/脚本 hash 的 render+lint 缓存,写 `checks/slide_NN.json`。
- `scripts/asset_slots.py`:稳定图片占位、原子 install、cancel 与 pending 状态。
- `scripts/prepare_workspace.py`:一次创建目录并复制 base CSS/vendor。
- `scripts/ai_slop_lint.py`:只读机械/AI-slop 检查。
- `scripts/validate_reference_access.py`:检查 role card 是否跨角色读取 reference、路径是否漏登记。
- `scripts/validate_delivery_layout.py`:交付前后检查根目录唯一 deck、连续页号、非空配套文件、本地资源路径、目录杂物/备份与嵌套平行 deck。
- `scripts/build_player.py`:串联生成 `present.html`。
- `scripts/stage_materials.py`:Research 解析附件入口(有附件时用)。

### 环境依赖

skill 自带脚本,第三方运行时分三层,首次部署或换机必须明确配置:

1. 附件解析:`markitdown/pdfminer.six/openpyxl/lxml/mammoth`,由 `scripts/install.sh normalize` 安装到独立 venv,并把脚本打印的 `NORMALIZE_PY` 注入运行环境。
2. 扫描 PDF 光栅化:`PyMuPDF(fitz)`,由 `scripts/install.sh pymupdf` 安装;需要独立解释器时设置 `RASTERIZE_PY` 或 `PYMUPDF_PY`。
3. HTML 渲染:中文字体 + Playwright Chromium,用 `scripts/install.sh fonts chromium`。`render.py` 优先同 revision 的 `chrome-headless-shell`(受限容器避免 crashpad),再退 Playwright 默认浏览器;需要显式指定时设置 `PPT_SKILL_BROWSER_EXE`。浏览器可执行文件存在不代表可启动;遇到缺库、crashpad/沙箱权限等稳定环境错误必须 fail fast,不要反复重试。

完整安装使用逻辑路径 `skills/ppt-skill-html/scripts/install.sh`。安装脚本只负责依赖;不能把失败安装或空附件 catalog 当作 deck 流程 PASS。

## 最终红线

- 不带失败/未干净收尾的任何 subagent 交付。
- Orchestrator 不写文件;不同 owner 不越权修改别人的文件。
- 不跨角色读取静态 reference;`skills/ppt-skill-html/scripts/validate_reference_access.py` 必须 PASS。
- Knowledge 未充分不启动 Designer/Presenter;designer/presenter 未自检 PASS 不启动 Image/Slide。
- slide 硬伤未清零不启动确定性 `delivery_finalize`;没有 fresh checks、零 pending/placeholder 不交付。
- 不整册重做一个局部问题;优先最小定向修改,保留上一版,失败即回滚。
- 不允许无限 vision 追问或无限自纠循环;slide refine 上限 = 2(`slide_NN` + 一次 `slide_NN_r2`)。
- 不允许 `slides/` 存在正式 N 页以外的任何 `slide_*.html`。
- 不允许根目录之外出现第二套 `slides/`、`renders/` 或 `present.html`;多交付物必须先归一为一套 canonical deck。

最终只需简短总结:页数、`present.html`、`speech.md`、两份 memory 和渲染页路径。可编辑内容源是 Presenter-owned plans/speech;可编辑视觉源是 Designer-owned design contract/base.css;HTML 成品不直接手改。
