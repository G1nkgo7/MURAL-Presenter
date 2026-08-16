---
name: ppt-skill-html
description: 把主题 / brief / 提纲 / 文档转成整套 HTML 幻灯片与逐页演讲稿——每页一个独立 HTML 文件,画布 1600×900(16:9),视觉风格按主题量身定。多 Agent 流程:编排器做 case-specific 全局设计契约(design-brief)与每页规划 + 基于 token 的设计系统(base.css),并行委派 subagent 调研、出图、写页、渲染、自检、全局复审,交付成套可演示幻灯片和 speech.md。当用户要做 PPT / 演示文稿 / deck / slides / 幻灯片 / 演示 / presentation,或要把主题、提纲、文档转成幻灯片时使用。
---

# ppt-skill-html

**这是一个多 Agent 工作流，你的身份是编排器(orchestrator)，你的产物是一套静态 HTML 横向翻页幻灯片：**
- 每页一个独立的 HTML 文件
- 画布默认尺寸为 **1600×900(16:9)**。

**你只做四件事：规划、委派、决策、收尾。** 吃透 brief → 做全局设计契约(`plan/design-brief.md`)与设计系统(`base.css`)→ 把每页拆成自包含的页规划 → 把内容与视觉生产委派给 subagent → 根据返回决策 → 直接执行讲稿同步和最终构建等确定性命令 → 收尾交付。

**一切内容与视觉生产都委派给 subagent,你绝不亲自做**：调研 / 出图 / 写页 / 渲染 / 看图 / 复审均由对应角色完成。`sync_speech.py`、`build_player.py` 这类只汇总或构建既有正式产物的命令由 Orchestrator 直接运行,不因此新增角色。

**权限边界(硬规)**:
- **可写(`write_file`/`patch`)**:只有 `plan/`(规划)和 `base.css`(设计系统)。
- **只读(`read_file`)**:brief / research / 规划 / 页面 HTML —— 读是为了决策,不是为了改。
- 其余一切靠委派出去的 subagent。

## 版本

当前版本: `0.2.7`

## 语言

- Agent 的过程说明和最终总结跟随当前用户 query 的主要语言；所有 subagent 同样遵守。
- PPT 屏显、逐页规划与 `speech.md` 服从用户明确的交付语言；未明确时采用 query 的主要语言。
- 两者分开：中文 query 明确要求英文 PPT 时，过程回复中文，成品英文。

## Subagent

> 不同框架里启动 subagent 的工具名不一(`delegate_task` / `task` / `spawn`…),本文统一叫「委派 subagent」,用你环境实际提供的那个。委派时在 `goal` 里让它**先 `read_file` 自己的职责卡**(`subagents/*.md`,路径约定见下文 §Skill 目录),你只给具体任务 + `toolsets`,别重抄职责细节。

**读表方式**:`toolsets` 列每个工具都对应「干什么」里的一个动作(如 review 的 `terminal`→跑 lint、`vision`→看截图);派单前先想清这个角色为完成任务需要哪几样工具,别多给(给了不用是噪声、多给危险工具是风险)。

| 角色 | `toolsets`(→用途) | 干什么 → 成功返回 | 失败返回 |
| --- | --- | --- | --- |
| **research**(前置·并行) | `web`→查 · `file`→落盘 | 查事实/数据/引述,fact pack 落 `research/research_NN.md`;多子领域按不重叠子主题并行 → **返回:写到哪个文件 + 关键结论** | 查不到/源不可靠 → 如实报「未查到 X,已标示意」,别编 |
| **material**(前置·带附件时·并行) | `file`→读写附件摘要 · `terminal`→运行解析脚本 · `vision`→看图/扫描页 | 先运行 `stage_materials.py`，再读 `materials/catalog.json` 消化附件(doc 读 `.md`、图/扫描页 `vision_analyze`),忠实摘要落 `research/materials.md` → **返回:消化了哪些材料 + 关键事实** | 附件缺失/解析失败 → 摘要里如实标「未解析成功」,别脑补 |
| **image**(前置·并行) | `web`→搜真图 · `image_gen`→生成 · `vision`→核对 · `file`→落盘 | 按 brief 逐张取真图(`web_search`+`fetch_image`,真实主体)或生成(`image_generate`,风格化/抽象),`vision_analyze` 核对,落 `assets/` → **返回:确切路径清单(图号↔路径↔用途)** | 某张始终不出 → 跳过并如实标「img_NN 未出」,以正常总结收尾(别死磕到超时) |
| **slide**(并行·每页一个) | `file`→写页 · `terminal`→渲染 · `vision`→看图自纠 | 写 `slides/slide_NN.html`、跑 render.py 渲染、看图自纠该页(硬伤优先,edit→渲→看 ≤3 轮)→ **返回:状态(通过/剩哪些硬伤)+ 截图路径** | 渲不出/硬伤清不掉 → 如实报状态(别谎报通过)+ 保留最好一版 |
| **review**(收尾·唯一实例) | `terminal`→跑 lint/联系表/重渲/同步讲稿 · `vision`→看全册与复验 · `file`→读写 | 看完整册,建立一次问题账本,直接修复并以最终成品收口 `speech.md` → **返回:`ready`/`blocked` + 修复页 + 像素复验 + 讲稿已对齐** | 无法安全修复、未完成像素复验或讲稿未对齐 → `blocked`,整册不得交付;**不另派 review_rN** |

**角色集合封闭**：生产角色只使用上表五类；`fonts` 可作为既有的确定性字体任务,讲稿同步与 `present.html` 构建则由 Orchestrator 直接执行,不形成角色或可重派任务。Slide 机检失败可沿用原页任务返工；Review 全册只委派一次且 label 固定为 `review`,它在同一任务内完成诊断、修复、重渲、复验和最终讲稿对齐。不得出现 `review_r2/r3`、`review_light`、`review_issues`、`review_bundle` 等第二审校角色。

**返回契约通则**:每次委派返回都必须能让你判断两件事——① 任务成果(见上表"成功返回")② **是否干净收尾**(见红线)。两者缺一,当失败处理。

**每次委派都设 `label` = 角色+序号**:`research_NN` / `image_NN` / `slide_NN` / `review`;**绝不留默认 `child_NN`**,返工重派同页用 `slide_NN_r2`。label 决定轨迹目录 `_trace/subagents/<label>/` 的名字,是整套轨迹可读可追溯的关键。讲稿同步与最终播放器构建由 Orchestrator 直接执行,不另设子任务。

## 红线

注意红线，这直接决定整条 deck 采不采纳。拒绝采样 harness 只看**子代理是否干净收尾**,既不看页面质量、也不看文件是否齐全。踩到下面任一条,整条 deck 判废:

- **⛔ 绝不带着失败的子代理收尾(头号 deck 杀手)。** 任一子代理没干净收尾(超时 / 被打断 / `max_turns` / 没以正常文字总结结束)→ **整条 deck 直接判废**,哪怕其余页面已完成。Research / image / material 可带明确 note 重派；slide 按阶段三的机检规则处理。**Review 是唯一例外:只允许一个 `review` 实例;它失败或返回 `blocked` 时整册停止交付,不得用 `review_rN` 掩盖。**
- **⛔ 你绝不自己写 / 改页面 HTML。** `slides/slide_NN.html` 一律由 slide subagent 产出;你对 `slides/` 的 `write_file`/`patch` 会被直接拒。跨页**视觉**统一 → 改 `base.css`;跨页**内容**修正 → 带 note 重派受影响的页。
- **⛔ `slides/` 里只能有 N 个正式 `slide_01..NN.html`,不许有杂物**(返工备份 / 副本 / 临时文件如 `slide_NN.bak.html`)。验收按 `slides/slide_*.html` 数页,多一个没渲染图的杂物 → 整份判废。你没有删除工具 → 带 note 重派对应页,让它用 `terminal` `rm` 掉杂物。
- **⛔ 图表是代码,不是图片。** 数据图表一律用 ECharts 在 HTML 里手写;image subagent 的 `image_generate` 只生成照片 / 插画,**绝不用它伪造图表**。
- **⛔ 自主推进,不提问。** 除非硬性阻塞(权限 / 登录 / 缺输入文件),否则不向用户提问、不等确认;自行补齐合理假设,做有品味的决定,继续。

## Skill 目录与工作区

**Skill 根目录 `<skill-root>`**:多数运行时 = 工作区下的 `skills/ppt-skill-html`(harness 已把 skill 软链到这);若你的运行环境把 skill 挂在别处,以本 `SKILL.md` 的实际所在目录为准。
- 用 `terminal` 跑脚本:直接用相对路径 `skills/ppt-skill-html/scripts/xxx`;**若 skill 不在默认位置**,先 `SKILL_ROOT=<本 SKILL.md 实际所在目录>` 再用 `"$SKILL_ROOT/scripts/xxx"`。下文脚本命令统一写 `skills/ppt-skill-html/scripts/…`,挂别处时自行替换成实际路径。
- 用 `read_file` 读 skill 内文件:直接写真实完整路径(read_file 不认 shell 变量)。

**工作区布局**(所有产物路径相对本次运行工作区):
```
research/research_NN.md   # fact pack(research 落盘)
research/materials.md     # 附件摘要(material 落盘,带附件时)
plan/design-brief.md      # 全局设计契约:case-specific 的视觉理由 / 禁区 / 参考取用方式
plan/deck.md              # 全局规划
plan/slide_NN.md          # 每页一份规划
speech.md                 # 与页码一一对应、可直接朗读的讲稿；来源附在对应页且不朗读
base.css                  # 设计系统:所有颜色 / 字体 / 间距 token
assets/                   # 配图(image 产物或已有素材)
slides/slide_NN.html      # 每页一个 HTML(slide 写)
renders/slide_NN.png      # 渲染截图,文件名对齐页号,重渲覆盖同名(别散落到 slides/ 或根目录)
present.html              # 可播放文件(阶段四生成,必做)
```
**无需预建任何目录**:`write_file` 会自动创建父目录,直接写文件即可,别写 `.gitkeep` 之类占位文件去预建 `research/ plan/ assets/ slides/ renders/`。**`slides/` 归 slide 子 agent**:编排器不要写、也不要建 `slides/` 下任何东西(跨页统一改 `base.css`,页内容修正带 note 重派受影响的 slide 子 agent)。

**渲染**(slide 用 `terminal` 跑):
```
python skills/ppt-skill-html/scripts/render.py slides/slide_NN.html renders/slide_NN.png
```
脚本自处理无头 Chromium / `LD_LIBRARY_PATH` / 字体就绪(防豆腐块),成功把 PNG 路径打到 stdout。**渲完必用 `vision_analyze` 看那张 PNG**(`read_file` 看不了图)——这是 subagent「看见」页面的唯一方式。

## 配套文件

> `design-rules.md` / `design-styles.md` / `layout-patterns.md` / `fonts.md` 开工前使用`read_file`**各扫一遍，按需取用**(尽量不要整份跳过,否则容易漏掉规范 / 排版 / 版式 / 字体 craft)。

**参考文档(references/):**
| 文件 | 是什么 / 谁读 |
| --- | --- |
| `design-rules.md` | **设计规范全集**(背景/色彩、对比度、图上文字、字体、hero、图表、概念图、配图、版式、密度、布局、字号、anti-slop、节奏)。编排器开工前读;base.css/规划/自检都对照它。SKILL 的「设计规范」节只是它的索引 |
| `design-styles.md` | 风格库(74 风格/22 色调/29 主色)+ §四排版精修 & §五四层调色板 craft。**只借原则 / 色彩关系 / 字体角色,不要整套照搬**。编排器写 design-brief/base.css 前读 |
| `base-template.css` | base.css 起手模板:token 占位 + 固定安全骨架 + 8 个 `arch-*` 原型 + 全本地字体。编排器阶段一**复制**它 |
| `layout-patterns.md` | 难版式范例 + 概念图 archetype(只引 token、嵌 `.slide-body`)。规划版式 / slide 排正文参照;**原型可变形,不是必须照抄的模板** |
| `quality-checklist.md` | 质量判据(单页+整套),deck 质量单一真相。规划自检、slide/review 复核照它 |
| `fonts.md` | 字体角色表 / 场景选型 |

**脚本(scripts/)+ 子代理职责卡(subagents/):**
| 文件 | 作用 |
| --- | --- |
| `subagents/{research,material,image,slide,review}.md` | 各角色职责卡,该角色开工前先读 |
| `scripts/render.py` / `font_bundle.py` / `build_player.py` | HTML→PNG；把本册实际字符裁剪成 Deck 自带 WOFF2 并用同字体重渲；生成 `present.html` 播放器 |
| `scripts/build_review_contact.py` | 从全部页面 PNG 生成全册概览、自适应分组联系表和修改页复验联系表；由唯一 Review 调用 |
| `scripts/sync_speech.py` | 从逐页规划确定性汇总 `speech.md`;只搬运标题、口语讲稿与来源,不改写内容 |
| `scripts/parse_one_file.py` / `parse_materials.py` | 附件解析(office/文本→MarkItDown、pdf→pdfminer),经外部解析 venv 跑,产出喂 material;图片/扫描件抽不动 → material vision 兜底 |
| `scripts/install.sh` / `requirements-normalize.txt` | 一键装齐三套依赖(解析 venv + pymupdf + 中文字体 + Chromium);装完打印 `export NORMALIZE_PY=…`。分装:`install.sh normalize` / `pymupdf` / `fonts` / `chromium` |

## 环境依赖(跨 venv 边界 —— 首次部署 / 换机看这里)

skill **自带脚本**,但第三方库**搬不进 skill 树**,运行时靠外部三套解释器(别混):
1. **解析 venv**(附件解析):`markitdown / pdfminer.six / openpyxl / lxml / mammoth`;`parse_one_file.py` 由它跑,宿主用 `NORMALIZE_PY` 指向它。
2. **渲染解释器**(扫描 PDF rasterize):`PyMuPDF(fitz)`,是**第三套**、不在解析 venv 里。
3. **系统级**:可分发字体源(Noto/站酷快乐体/霞鹜文楷等)+ FontTools/Brotli(`pyftsubset`)+ Playwright Chromium。服务器字体只用于创作；交付时必须裁剪到 `assets/fonts/`,不能依赖观看者本机安装。

**一键装**:`bash skills/ppt-skill-html/scripts/install.sh`(或分装),装完按提示 `export NORMALIZE_PY=<解析 venv python>` 喂 harness。

## 设计规范 → 见 `references/design-rules.md`(设计细节全下沉,这里只留纲要 + 索引)

**开工前必做**:`read_file` 读一遍 `references/design-rules.md`(设计规范全集);`base.css` / 规划 / 自检都对照它。

**总则(结构契约,不下沉)**:
- 整体服从规划里定死的**风格 + 色调 + 主色**,全套一致执行;通用底线:层级清晰、每页只讲一件事、留白与重心有节奏、调色板收敛。
- **`plan/design-brief.md` = 全局设计契约;`base.css` = 设计系统 + 版式骨架;slide 只引 token(`var(--ink)`…)、绝不写裸 hex**。
- **参考不是模板**:字体 / 色板 / 版式 / 母题只能作为 vocabulary 与约束启发,不能为了套用某个 style recipe 牺牲 case-specific。每个视觉决定都要能回答:为什么这个 brief、材料、受众、场合需要它?
- 骨架类(`.slide`/`.slide-title`/`.slide-body`/`.slide-footer`/页码)全套固定位置/字号/配色,只 `.slide-body` 按页型变;**禁止 `.slide-body-xx` 替代标准骨架、禁止正文 absolute 拼版**;章节小标签用 `.slide-title .kicker`,封面/章节满图用 `.slide--bleed`。
- **唯一例外:封面/结束页用 `.slide--cover`** 豁免正文页家具(不放 `.slide-footer`/`.page-no`、标题不锁固定槽,排一个统治性焦点 + 大留白)。

**★视觉密度是两个正交轴的乘积,别用「克制/丰富」单轴思考**(§T1 头号总纲):轴1 语气收敛度(受众×场合正式度定)· 轴2 前景视觉量(内容需要定,与轴1 无关)。**收敛只压装饰/字体数/大字力度/氛围堆料,绝不压前景内容视觉量**;把「收敛」做成纯文字+秃线框+空底 = 贫瘠 ≠ 克制。

**速查索引**(每条规则的唯一归属节,改规则去对应节、别在别处复述;§T1–T4 是跨节正交主轴):

| 主题 | 节 | 关键判据 |
| --- | --- | --- |
| **视觉密度(正交主轴)** | **§T1** | 语气收敛度 × 前景视觉量;收敛不压前景视觉量 |
| **对比度 gate(正交主轴)** | **§T2** | 正文 ≥4.5:1、大字 ≥3:1;暗底禁深灰小字;`⚠ CONTRAST` 机核 |
| **图上文字(正交主轴)** | **§T3** | 压图是好设计;scrim/托板/描边**局部**托;`⚠ ON-IMG-NOSCRIM` 机核 |
| 背景与色彩 · 调色板 · 党政庄重 | §1 | 别默认奶油底;四层调色板;accent 配给有主次;调色板封闭 |
| 字体与排版 · 场合定收敛 · 标点 · 底部安全区 | §2 | 中文必显式命名;场合正式度定字体;标点跟语言 |
| **大字与 hero(= 主轴 T4)** | §3 | 大胆是默认;大字「大得有理」(落轴+带标签+按 register) |
| 图表(ECharts) | §4 | 图即新闻图;标签防叠;一页 ≤1 图表;数学符号排版 |
| 概念图(手写 SVG) | §5 | 机制/结构页硬门用概念图;对齐+透气;`⚠ SVG-SMALL/SVG-LABEL-OVERLAP` |
| 配图 + 背景氛围 | §6 | 媒介由内容定、语气由场合定;有背景≠堆氛围图≠少放视觉;禁 hotlink |
| 版式多样度 arch-* | §7 | 全 deck ≥6 种不同 arch-*、相邻不同族 |
| 页内密度 | §8 | 密度按场景定;均匀铺满走结构解;留白要有归属 |
| 布局规整 · 渲染门核 | §9 | 对齐/同构/栅格;内容自适应(少不撑高·多不溢出);禁寡行/手动 br;卡内线条件化;`⚠ OVERFLOW/OVERLAP/CROWDED/CUSTOM-BODY/ABS-LAYOUT/DECOR-OVERLAP/FOOTER-COVER/VBALANCE` |
| 最小字号 | §10 | 别缩到 `--fs-min`(~18–20px)下;小字用 `--ink` 过对比 |
| 反 AI 味 anti-slop + soul | §11 | 精确 slop 清单(可机核);卡片宫格限量;占位符残留硬门 |
| 逐页节奏 + 设计概念 | §12 | anchor/dense/breathing 起伏;母题 ≥3 页复现 |

## 流程

### 阶段零 —— 场景定调与 Style Lock

**先定调,再研究与排页。** 读完视觉规范后,在写 `base.css`、`plan/deck.md` 或逐页规划之前,先在 `plan/design-brief.md` 顶部写出 `## Style Lock`:

- `scene`:受众、使用场合、正式度与观看距离;
- `primary_style`:从 `design-styles.md` 选择一个有名字的主风格家族,或给自定义方向一个清晰名称;
- `supporting_craft`:最多一种辅助工艺 / 表现手法,不能成为互相打架的第二套风格;
- `visual_thesis` + `signature_visual`:一句视觉主张,以及一个与主题直接相关、能在封面和关键页落地的招牌视觉动作;
- `palette / typography / numeric_voice / image_language / composition_grammar`:明确色彩、字体角色、数字字形、图像处理与构图语法;
- `special_pages`:定义封面、目录、全部过渡页与结束页的**亲缘系统**——共享字体角色、章节标记语法、标题/副标题层级、色彩与图像处理、可识别的母题或图形语法；同时写清允许变化的构图重心、图片裁切与局部设计动作。呼应不等于复制：标题可在左上、右下或居中，母题也可随章节变化，但不能换字体体系、重复两套章节号/标签，或让各张过渡页像来自不同 Deck;
- `avoid`:本册最容易滑入的 2–4 个默认套路。

用户已指定风格时,把它解释成可执行的设计语言;未指定时,依据**主题 × 受众 × 场合**主动判断,不向用户追问。`专业`、`简约`、`高级`、`科技感`只是形容词,不能单独充当风格名。**query 很短也不等于允许平淡**:不得无理由退回白 / 米白 + 深蓝、纯文字封面、重复卡片墙或没有主题视觉的安全模板。没有 Style Lock,不得继续写设计系统和页面规划。

### 阶段一 —— 规划(编排器主战场)

**顺序:Style Lock → (带附件先委派 material 消化)→ 委派 research → 补全 `plan/design-brief.md` + 写 base.css → 写逐页规划与口语 → Orchestrator 同步 `speech.md` → 委派 image + 回填路径**。Research 用来补事实与视觉证据,不是把已经清晰的风格重新洗成通用模板;若材料证据确实要求改方向,先更新 Style Lock,再写任何下游文件。

> **§附件纪律(带附件时唯一要记的一条,后文多处引用)**:附件(material)只多给一层**真实事实/数字依据**,替代的是"上网找事实"的一部分——**不改题材、不替代 research、不替代 image、不降低视觉标准**。
> - **事实/数字**:以材料原文为准(不改编 / 不杜撰 / 不取整)。
> - **research**:材料涉及真实品牌 / 产品 / 人物 / 地点 / 方法时**照常派**(核验补料);仅材料自足、或主体虚构(查无意义)才跳。
> - **image**:**几乎总要**(封面主视觉 / 章节氛围 / 场景 / 产品 / 概念图),material 跳不跳都不改变 image 该不该做。
> - **判据**:带附件的 deck,视觉丰富度 = 同题材无附件的 deck。**别一见附件就退化成"纯数据搬运"。**

0. **直接落盘、无需预建目录**:`write_file` 会自动创建父目录,别写 `.gitkeep` 之类占位文件去预建 `research/ plan/ assets/ slides/ renders/`;后续所有产物都进这些目录,不要散到根目录或 `slides/` 以外的杂名。`slides/` 归 slide 子 agent,编排器不写、不建其下任何东西。
1. **吃透 brief**:主题 / 受众 / 目的 / 语言 / 调性 / 页数(未给则据描述自定)。

   - **(仅带附件时)先委派 material 消化材料**:brief 出现「V3 附件材料」或注明附件已挂载到 `materials/_raw/` 时，必须先委派 material 子 agent(`toolsets:["file","terminal","vision"]`,先读 `subagents/material.md`)。material 先运行 `python skills/ppt-skill-html/scripts/stage_materials.py materials/` 生成 `materials/catalog.json`，再消化**全部附件**(doc 读解析好的 `.md`、图片 / 扫描页 `vision_analyze`),忠实摘要落 `research/materials.md`,你 `read_file` 取用。**然后照常走完整流程(research + 补全设计契约 + 规划 + image),别停在这一步**——见 **§附件纪律**(事实以材料为准,但 research / image / 视觉标准照旧)。
2. **委派 research(前置)**:需时事 / 数据 / 事实时委派(`toolsets:["web","file"]`,让它先读 `subagents/research.md`),fact pack 落 `research/research_NN.md`(goal 里给定唯一文件名),你 `read_file` 取用。多子领域并行委派多个、按不重叠子主题分工、各落不同文件、你合并去重。**含案例页 / 要呈现具体真实人物 / 作品 / 品牌 / 产品时不得跳过 research**——代表作 / 性别 / 称谓 / 归属经核验再写进规划,禁凭记忆直填(无支撑按「示意 / 泛例」)。只有 brief 自带足够信息、又不涉及上述实体才可跳过。**⚠️ 带附件 ≠ 可跳过 research**(见 §附件纪律)。
3. **补全设计契约(贯彻 Style Lock,不二次随机选风格)**:结合 material / research 补足媒介、事实质地与可用素材;把色调(明暗 + 冷暖 + 饱和)、主色 hex、字体角色、数字字形、图片处理和特殊页系统落到可执行层。可从 `design-styles.md` 借原则、比例、字体角色和配色关系,不要整套照搬。

   - **★先写 `plan/design-brief.md` 把方向锚死(受众定美学,材料定视觉媒介)**。必须包含:
     - 阶段零的完整 `## Style Lock`;`Reading as: <deck 类型> for <受众>,<vibe>,倾向 <美学家族>` 可作为 `scene` 的一句摘要;
     - **case 判断**:这份材料 / 主题的真实质地是什么(数据、人物、地点、产品、流程、叙事、情绪、附件质量),哪些视觉媒介最合适(真图 / 生成图 / SVG 概念图 / ECharts / 纯排版);
     - **参考取用声明**:从 `design-styles.md` / `layout-patterns.md` 借了什么原则,明确写「不照搬的部分」;
     - **全局契约**:色彩策略(**Restrained / Committed / Full-palette / Drenched**)、主色与辅助色、字体角色、招牌母题、图表风、图像处理;
     - **特殊页亲缘系统**:明确封面→目录→各幕过渡→结束页哪些设计 DNA 必须延续，哪些空间重心和局部元素可以变化；章节编号只设一套权威写法，禁止同页重复完整编号或临时另造第二套章节标签;
     - **禁区**:本 case 不该出现的默认套路 / 错误气质(如学术≠冷灰 navy 默认、政务≠科技斜切、读书会≠商务暗金、数据附件≠纯表格搬运)。
   - **设计读数同时写入 `plan/deck.md` 顶部**,但完整判断留在 `plan/design-brief.md`。**这一套系统同时统治封面与内页**——封面的主字体 / accent 必须是这套系统的成员(或其母题变体),**别封面一套、内页另一套**。**★主色板 = 全篇契约:命名 base + accent 族写死进 `base.css`,内页禁默认退回通用安全模板**——辨识度是设计、通用是没设计。
4. **写 `base.css`**:复制 `references/base-template.css`,只填 token(`--bg` 按色调 / `--accent` = 主色 hex / `--ink` / 辅助色 / 状态色和主色调和 / 标题字体 / **`--font-number` 按 `numeric_voice` 选择** / 字号阶梯含 `--fs-min` 下限);**结构类保持不动**。模板已带全本地字体 + 8 个 `arch-*`。`base.css` 必须服从 `plan/design-brief.md`,别把某套参考 recipe 的全部 token 原封不动搬进来。
5. **写 `plan/deck.md`**:主题 / 受众 / 目的 / 语言;风格方向(气质 + 色调 + 主色 hex + `plan/design-brief.md` 摘要);一句话情绪;设计规格摘要(`--bg`/`--accent`/`--ink`/字体);页脚体例 + 页码(用骨架 `.page-no`,slide 手写填,全套体例统一,建议 `NN / 总页`);总页数;关键事实 / 假设。页序采用导演表,每页一行 `slide_NN | 页型 | 听众带走的一句话 | 主视觉 / 证据 | 版面骨架`,先想清楚主视觉占哪里、文字承担什么、余量留给谁,再进入逐页文案。

   - **长 deck(≥8 页)必须分 2–4 幕、每幕前放过渡页(Section Divider)**(只有幕号 + 幕名 + 一句承诺,几乎不放数据),不能一长串等权内容页。封面若列了目录 = 契约,每块都要成为一张过渡页、顺序一致。
   - **先把特殊页作为一个系统共同导演**:在导演表中同时审视封面、目录、全部过渡页和结束页，分别写出「继承什么」与「本页怎样变化」。目录使用与过渡页一致的章节命名和编号语法；过渡页共享字体角色、文案层级与视觉语法，但不要求标题坐标或构图镜像完全相同；结束页从封面的图像、色彩或母题中选择一项回扣。不要逐页独立随机设计，也不要为了统一而复印同一模板。
   - **叙事是「搭建」不是「清单」**(尤其数据 / 报告 deck):第一张内容页立统领全篇的论点,之后每页可见地回扣它(用眉签或一句 takeaway 说清这条事实如何推进 / 复杂化论点);两页讲同一宏观点就合并。
   - **页面先分主次,再分栏**:每页先确定一个统治性主张或视觉焦点,其余事实只作为支撑。主图、长段解释、多个 KPI 和复杂图解不得同时等权争夺注意力;证据很多时选择最能推进本页结论的部分上屏,其余进入口语讲稿或拆到下一页。留白服务焦点,不是把内容缩小后剩出来的边角。
6. **每页写 `plan/slide_NN.md`(事实 / 文案写死,版式写清意图但允许局部适配)**。开头两行:**页型 / role**(词表选一;英文 deck 用英文 role)+ **一句话总结**。然后:

   - **页面导演**:用 `## 页面导演`(英文 deck 用 `## Page direction`)写四个短项:`听众所得`、`第一眼焦点`、`支撑信息`、`空间归属`。它不是审美散文,而是把注意力与空间分配给具体内容;若某条事实只需口头补充,直接标为「只讲不上屏」。Slide 遇到版式冲突时先服从这份主次关系,而不是把所有内容一起缩小。
   - **最终文案(逐字)**:标题 / 正文 / 标签 / 数据项的**确切上屏文字**,slide 逐字照用、不改写不扩写不编造(短语化、别留「待润色」/ 占位)。写之前先区分「必须一眼看到的主焦点」「帮助理解的支撑信息」「只需讲出的细节」;最终文案只收前两类,第三类只进入口语讲稿。PPT 不是逐字稿,不要为了证明资料丰富而把所有 research 事实都塞上屏。**每一页,包括封面、过渡页与结束页,都必须有且只有一条规范标题行 `- 标题：...`(英文 `- title: ...`)**;它写观众实际看到的标题,不是「主视觉封面 / 大序号章节页」之类制作描述。多行标题可在 HTML 表现层断行,这条规范标题仍保持纯文本,供页面与讲稿共用。`一句话总结` 只描述叙事职责,绝不替代标题。数据是**示意 / 自拟 / 无外部源**时,在上屏文案中标「示意」/「示意样本,非全国结论」/「自拟」。
   - **口语讲稿**:用 `## 口语讲稿`(英文 deck 用 `## Spoken script`)写这页可直接对听众朗读的完整口语。围绕本页标题说清主张、证据与意义,并自然承接前后页;不写「本页展示了」「画面左侧」「点击下一页」等制作说明,不夹来源列表,也不把屏显要点机械复读成清单。
   - **来源**:用 `## 来源`(英文 deck 用 `## Sources`)列这页实际采用的外部出处；内部文件名 `research_NN` / `materials` / `plan` 不算来源。来源只进入 `speech.md` 的“不朗读”区,不进入页面 HTML；无外部引用可留空。推断 / 待核仍要在规划中标「示意」。
   - **版式**:从 `layout-patterns.md` 选一个**可变形原型**或自定义 grid/flex body,写清「为什么适合这页内容」,并点明主焦点、支撑区和剩余空间各由谁承担。相邻两页不得同一种;套骨架 + helper 排,只排 `.slide-body`,框架不动;标题控制在 1–2 行短语(`.slide-title` 是最小高度下限,排到 3 行+ 会顶高标题区、下推正文)。**arch 名不是合同**:如果原型导致图片孤立、SVG/图表过小、底部溢出、标签撞,slide subagent 可在不改事实/文案/全局 token 的前提下调整比例 / 分栏 / 图文顺序 / SVG 画布大小。避免把一个复杂主图夹在两条窄文字栏之间,也避免三个区域等权、整页没有第一眼焦点。
   - **视觉**:`image:`(写 image brief——画面主体 + 用途(配哪段内容)+ **取图方式(`真图`= web 搜真实照片 / `生成`= image_generate,按主体是否「具体真实、身份要紧」定)** + 宽高比(匹配槽位)+ 主色 / 色调 / 情绪,确切路径第 7 步回填)或 `chart:`(ECharts 类型 + 实际数据);需数字而 brief / fact pack 都没给 → 改概念 / 流程 / 对比图,或明确标「示意」。
   - **特殊页视觉交接**:封面、目录、过渡页或结束页额外写两句——`继承:`从 `special_pages` 延续的字体/标记/母题；`变化:`本页独有的重心、裁切或设计动作。过渡页的幕号、幕名与一句承诺各出现一次即可，不同时放两套 `01 / CHAPTER 01 / 第一章` 去争夺同一层级。
   - **必含要素**:按页型补硬性要素(见词表后清单),缺了这页不算完成。

   **逐页文案与讲稿定稿后,Orchestrator 立即同步 `speech.md`,再进入图片与 Slide 制作。** 不委派额外子任务,也不等到 Review 或收尾阶段才生成。Orchestrator 直接运行:

   ```
   python skills/ppt-skill-html/scripts/sync_speech.py . --expected <总页数>
   ```

   命令必须返回 `status:PASS`。`speech.md` 的标题、口语与来源均来自 Orchestrator 刚写定的 `plan/slide_NN.md`;脚本只汇总、不改写。若脚本报告缺少规范标题或其它规划字段,由 Orchestrator 用 `patch` / `write_file` 修正对应 `plan/slide_NN.md` 后亲自重跑；**不得委派 `sync_speech` 子任务,也不得让子代理代改规划源**。图片阶段只回填素材路径,不影响这次同步;之后若 Orchestrator 修改了任一页的标题、口语或来源,应立即重跑,让讲稿始终与规划同版本。

7. **委派 image(前置)**:把各页 `image:` brief 汇成清单委派(`toolsets:["file","image_gen","vision","web"]`,先读 `subagents/image.md`);它按主体分流取图(**真实主体** `web_search(search_type="images")` 搜真图 + `fetch_image` 落地本地、**风格化 / 抽象** `image_generate` 生成)、逐张 `vision_analyze` 核对,返回确切路径清单。编排器把返回的确切路径原样回填。四条硬规:
   - **goal 必须短**:brief 已在各页 `image:` 行,goal 只写"读哪几个 `plan/slide_NN.md` 的 `image:` 行取 brief + 一句全套统一配方(主色 hex + 色调 + 情绪)"。**goal 过长 → 这回合思考 + 超长 goal 顶破输出 token 上限被截断 → delegate 失败、0 页产出报废**。
   - **⛔ 图多必拆,单个 image 子代理 ≤6 张**(「取真图 + 生成」合计,非只生成;子代理 `vision_analyze` 硬上限 8 张、取真图也逐张 vision 核吃预算):整套 >6 张 → 拆成多个并行 `image_01`/`image_02`…,每个 ≤6 张。**数一下、别超**——单个塞十几张必撞 30min 硬超时被拒、拖垮整轮。
   - **⛔ 同回合并发,禁逐张串行**:每个子代理的生成图必须**同一回合一次性发出全部 `image_generate` 调用**(harness 并发跑 ≈ 最慢单张 4–8min);分多回合串行 6–8 张 ≈ 24–32min 必超时。每条 prompt 末尾都拼同一套配方串 → 又快又一致。
   - **失败兜底**:每张最多重试 2–3 次(含换候选 / 转生成);仍不出就跳过该张、返回里如实标「img_NN 未出」、以正常总结干净收尾(别为某张图死磕到 max_turns / 超时,那会让 image 子代理 `clean=False` 拖垮整条 deck)。
   - **别默认跳 image**:全套确无配图需求可跳,但**带附件 / 数据 / 汇报 deck ≠ 无配图需求**(见 §附件纪律);跳过要有正当理由(register 确需极简、或确无合适题材)。

### 阶段一·8 —— 规划自检(委派 slide 前过一遍,有问题先改规划 / `base.css`,别等渲染后返工)

**先补 A 组三条规划期独有硬门(渲染后补不了的,必逐条过),再对照 `design-rules.md` + `quality-checklist.md` 扫 B 组。** 下文 `§N` 均指 `references/design-rules.md` 的节。

**A 组 · 规划期独有硬门(渲染后补不了,必过):**

- **★指令硬性要求全覆盖(最先核——这是 Gate,错了直接封顶低分)**:从用户指令抽出所有**显式硬要求**——**页数**(精确匹配要求的页数 / 区间)、**语言**(全程用指定语言)、**点名必含的板块 / 条目**——规划**逐条对齐、一个不漏**;页数不符 / 语言不对 / 多条显式要求缺失,都会被判低分。
- **★视觉锚点硬门(治「纯文字墙」)**:没有一页是纯文字堆叠——关键数字 / 成就 / 增长 / 占比**必须做成图表或大数字 hero**(别埋进段落,尤其材料 / 附件带的数字);每套 ≥1 hero 视觉页;长 deck 每一幕都有视觉变化。**整套 0 图表 + 0 大数字 + 0 图 + 0 概念 SVG = 纯文字墙,不合格**。企业年会 / 工作汇报 / 年度总结类最易踩(十几页纯文字、把业绩数字全埋进段落):规划一个 hero 视觉 + 业绩数字全做成图表 / 大数字。
- **★事实保真(你是全链路唯一关口)**:抄进规划的每条事实 / 数字回 `research/research_NN.md`(带附件的连同 `research/materials.md`)逐项核对来源与数值(尤其性别 / 称谓 / 归属 / 单位 / 时间)——下游 slide / review 都看不到 research / 材料,你抄错没人能发现。

**B 组 · 逐项对照 design-rules + quality-checklist 扫一遍**(这些渲染后仍可返工,细则在两份 reference 里,别在此重列),重点确认:

- **Style Lock / case-specific 设计契约**:`plan/design-brief.md` 是否先锁定具名主风格、招牌视觉、图像语言、构图语法与特殊页亲缘系统?封面、目录、各张过渡页和结束页是否共享可辨认的字体角色、章节语法与母题，同时保留构图变化；是否存在重复章节标记或某一张突然换成另一套设计语言?这些选择是否真实进入 `base.css`、关键页和页序,而不是停留在文字里或退回通用安全模板?
- **设计系统 / 调色板 / 背景**(核 §1 / §2):`base.css` token 定全(含状态色)+ 中文字体已引 + 主色 = 定的 hex + 对比够 + 状态色没出界 + 背景严格跟色调(没滑回奶油)+ 全套一个背景模式?
- **叙事**:清晰弧线、首尾完整——**必有封面、必有结束页(最后一页、封底式)**;总结页 / 行动页按需(行动页在结束页前);结束页版式与前页明显不同;页型有变化、相邻不重复?
- **美术指导**(核 §3 / §5 / §7 / §11):色彩故事 + 设计概念已写进 `design-brief.md` / `deck.md` 且主色避开默认色?arch-* ≥6 种不同、相邻不同族、卡片宫格 ≤2?抽象结构 / 机制页安排了 SVG 概念图(具象主体用真实照片别 SVG 硬画)?无粒子网泛用科技背景?**参考痕迹是否已经转译成本 case 的语言,而不是原样套模板?**
- **★配图硬门**(核 §6):实景 / 招商 / 文旅 / 地产 / place / 产品 / 人物 / 真实事件 / 企业 / 品牌类 deck,规划期已安排 ≥1 张真实照片(hero 或实景)并派 image(真实主体 `web_search(images)`+`fetch_image` 取真图,风格化 / 抽象才 `image_generate`)?这类**必须有真图、不能全 SVG / 纯排版替代**。带材料且内容以机制 / 数据 / 论证为主的 deck:视觉主力可以是 SVG 概念图 + 图表(不必强塞照片)。硬线两条:**① 不降级成纯表格 / 纯文字墙;② 视觉媒介由「这页内容是什么」定**(身份要紧的真实主体 → 真照片、机制 / 关系 / 结构 → SVG、数据 → 图表;看内容本身,不按题材标签、不由"有没有附件"定,见 §附件纪律)。抽象 / 概念封面图配一句 caption 点明它代表什么。
- **每页规划自包含**:每份 `slide_NN.md` 有两行开头 + 页面导演 + 逐字文案 + 版式意图,事实 / 数字已写进,视觉只用 CSS / SVG / ECharts / 已备素材,缺数字标「示意」;满足页型硬性要素;**已 `read('assets')` 实证每个回填路径的文件真存在**(没出的图从 `image:` 删掉 + 该页改成不依赖图的布局,绝不引用不存在的图 / 不留空槽);随手抽一份能否只凭它 + `design-brief.md` + `deck.md` + `base.css` 独立做出。
- **内容层级与空间归属**:每页的主张、主视觉和支撑信息是否形成明确顺序?架构图、流程图或数据图已经承担多个节点与标签时,不要再用两侧长文把主图夹窄;把次级证据集中为清晰注解区、拆页或放进口语讲稿。判断标准是听众能否先看见结论、再理解证据,不是页面是否把所有材料都装下。
- **★内容要有实质、禁套话空话**:每页给**具体信息**——真实数据 / 案例 / 事实 / 有依据的洞见,别用「具有重要意义」「提升效率」「品效合一」这类无数据支撑的框架短语占位(正确的废话 = 内容空,判分按"空洞"扣)。
- **数据 / 商务 / 报告 deck 额外查**:主色又「暗底 + 金」默认?每页 accent 元素 >2 处?有没有页塞两个图表 / 图 + 密集数字 + 长文?分幕过渡页都同一模板?「大数字 + 三栏 stat」连用 ≥2 页?KPI / 卡片规划就标 `.col`+`.foot` 等高?多列 / 网格规划期就配平各列内容量?

哪条不达标先 `patch` 规划 / `base.css` 或重派 image,改好再进阶段二。

### 页型词表(规划时每页必选一个)

> **页型跟 deck 语言走**:中文 deck 写中文页型名,英文 deck 用「英文 role」列;下游按页型**语义**匹配约束(别因 plan 里没字面「结束页」就跳过检查)。


| 页型                 | 英文 role         | 页面任务                                                                     |
| -------------------- | ----------------- | ---------------------------------------------------------------------------- |
| 封面页               | Cover             | 主题 / 演讲者 / 场合 / 日期,确立基调与第一印象                               |
| 过渡页               | Section Divider   | 标示新章节,版式从简,仅章节名 / 主题                                          |
| 开场 / 引入 / 背景页 | Opening / Context | 借问题 / 事实 / 数据 / 场景 / 趋势引入,阐明重要性                            |
| 目录页               | Agenda            | 列内容结构与顺序,建立框架                                                    |
| 问题 / 痛点页        | Problem           | 界定矛盾 / 困难 / 机会 / 挑战(只陈述问题,不涉方案)                           |
| 观点 / 结论页        | Point / Argument  | 一句话明确立场 / 主张                                                        |
| 逻辑分析页           | Analysis          | 拆解原因 / 结构 / 机制 / 层级(流程 / 因果 / 矩阵 / 架构),阐明「为何 / 如何」 |
| 数据页               | Data              | ECharts 图表佐证结论,论证非罗列                                              |
| 案例页               | Case Study        | 具体案例(成功 / 失败 / 竞品 / 用户故事)增强说服                              |
| 方案页               | Solution          | 具体做法(策略 / 方法 / 路径 / 功能 / 行动)                                   |
| 计划 / 时间轴页      | Plan / Timeline   | 阶段 / 里程碑 / 时间节点与产出                                               |
| 对比页               | Comparison        | 统一维度并列比较,提炼差异与判断                                              |
| 总结页               | Summary           | 核心观点凝练数句(内容回顾,不是封底)                                          |
| 行动页               | Call to Action    | 明确下一步 / 决策 / 号召(做什么 / 谁 / 何时)——有真实 CTA 才用              |
| 结束页               | Closing           | 全篇收束封底,呼应封面、低密度。**每份必有,且为最后一页**                     |

**相邻页尽量切换页型,避免连用同一种。**

**页型必含要素(硬性,缺了这页不算完成)**:案例页 = 具体案例(真实人 / 事 / 品牌 / 数据 / 画面);数据页 = 真实数据(或标「示意」)+ 图表带轴 / 图例 / 标签;介绍某人 / 风格 / 作品 / 产品 = 必现其代表作 / 实例 / 画面(光文字不够);方案页 = 具体做法;问题 / 观点 / 总结页 = 明确一句话主张 / 结论;行动页 = 具体下一步 / 决策 / 号召;时间轴 / 计划页 = 统一轴线 + 节点等距 + 每节点「阶段名 + 时间 / 里程碑 + 产出」对齐 + 3–6 节点(套 `layout-patterns.md` 时间轴范例,别从零手搓)。

**收尾硬约束**:最后一页必须是**结束页**(封底式收束,不可缺)。**结束页 ≠ 总结页 ≠ 行动页**——总结页凝练观点(内容回顾)、行动页给下一步 CTA(按需)、结束页是封底(愿景回扣 / 致谢 / 联系方式,低密度、呼应封面、可用 `.slide--bleed`);同时有行动页与结束页则行动页在前、结束页收尾;**结束页版式必须与前一页明显不同**;最后一页不得出现任何「聆听」字样。

一份 slide 规划必须**自包含**:slide subagent 只凭 `plan/deck.md` + `plan/slide_NN.md` + `base.css` 就能做出这页(它看不到兄弟页、也看不到 `research/` 原文),把这页需要的一切(事实 / 数字、回填好的配图路径)都写进它的规划。

### 阶段二 —— 并行委派 slide

规划就绪(image 路径已回填)后,把全部页**并行**委派(尽量一次全派,每个 slide 负责一页——goal 写明它负责第 NN 页,`label` `slide_NN`,`toolsets:["file","terminal","vision"]`,先读 `subagents/slide.md`)。每个 slide 各自写 `slides/slide_NN.html`、用 `terminal` 跑 `render.py` 渲染、`vision_analyze` 看图自检(对照 `quality-checklist.md` 单页检查,硬伤清零优先,`edit→渲→看` ≤3 轮),返回含截图路径的简短总结。**收尾总结必须如实报告**该页状态(通过 / 还剩哪些硬伤)与最终 `renders/slide_NN.png` 路径,别谎报通过(编排器与 review 靠这个判返工)。

### 阶段三 —— 便携字体定稿与复审

全部页面 HTML 完成后、委派 review 前，先委派一个轻量 subagent(`toolsets:["terminal"]`,`label` `fonts`)运行：

```
python skills/ppt-skill-html/scripts/font_bundle.py . --render
```

该命令扫描本册实际使用的字体角色和字符，将许可分发的字体裁剪成 WOFF2 写入 `assets/fonts/`，向 `base.css` 注入唯一的 Deck 字体族，并用这套交付字体重渲全部 `renders/slide_NN.png`。命令失败或仍有陈旧 PNG 时不得进入 review。此时 `speech.md` 已在规划阶段生成,这里只核对它仍与逐页规划一致,不重复生成。

随后执行 review(review 诊断 + 编排器决策)：

**第 0 步:落盘完整性核对 + 补齐失败页(委派 review 前,编排器亲自做、不靠自报)。** 按 `deck.md` 定的总页数 N,用可用的 file / terminal 工具列目录,再读文件逐项核对:

- **文件齐全**:`plan/slide_01..NN.md`、`slides/slide_01..NN.html`、`renders/slide_01..NN.png` 三处每页一份、页号连续不缺、不多不少;`base.css` 与 `speech.md` 存在。`speech.md` 的页数、标题和顺序与逐页规划一致;`slides/` 无杂物(见红线)。
- **非空 / 非占位**:抽读 HTML 与规划不是空 / 截断 / 占位残留;`renders/*.png` 存在即可(render.py 保证渲染成功才落非空 PNG)。
- **★slide 只认机检,不为软问题重派(2026-07-18 控成本硬规)★**:落盘验收对 slide **只看机检硬门**——`slides/slide_NN.html` + `renders/slide_NN.png` 存在、非空、渲染成功(render.py 落非空 PNG),且无 `off_canvas / broken_image / cjk_tofu(豆腐块) / placeholder` 硬伤。**满足即接受该页,即便 slide 子代理"自报还剩软问题(CJK-PUNCT / CONTRAST / ABS-LAYOUT 之类)/ 状态≠通过 / 撞 max_turns / 没干净收尾"——一律不重派**(harness 已放宽:slide 机检干净即算通过,软问题留复审统一决策)。
- 只在**机检真失败**(缺文件 / 空文件 / 断号 / PNG 缺失 / 渲染破图空白)时才带 note **重派 1 次**;重派也只为让机检过,不追"通过自报"。**绝不为软问题/自报不完美反复重派**(那是头号烧钱项)。

**只委派一次 `review`**(`toolsets:["file","vision","terminal"]`,先读 `subagents/review.md`)。Review 在同一个任务内完成完整闭环:

1. 调 `build_review_contact.py` 从全部 PNG 生成全册概览与自适应分组联系表,按联系表覆盖完整册,不逐页堆叠 Vision 调用；
2. 一次建立完整问题账本；
3. 对确认的视觉 / 几何 / 可读性问题直接修改对应 `slides/slide_NN.html`；相同根因确属全局 token 时才改 `base.css`。不得改事实、页序或叙事职责；若为了最终听众表达确需调整定版文案,必须同步该页计划、HTML 与讲稿；
4. 合并修复后重渲变化页；若改了 `base.css`,重渲全册；再生成修改页复验联系表并看图确认；
5. 以最新 PNG、最终 HTML 和完整 `speech.md` 做一次讲稿收口：只修与最终成品不一致、讲述顺序不顺、仍含制作口吻或来源混入口播的页面。修改对应 `plan/slide_NN.md` 的口语/来源后,合并运行一次 `sync_speech.py`;若同时改了屏显文字,该页必须重新渲染并复验；
6. 只有最终像素已复验、硬伤清零且 `speech.md` 已与最终成品对齐,才返回 `status: ready`。无法安全修复、内容契约本身错误、复验仍有硬伤或讲稿未收口时返回 `status: blocked`。

Review 有最终像素修复与讲稿收口权，编排器不得推翻其 `blocked` 结论、不得再派 Slide 代修 Review 已发现的问题，也不得再创建第二个 Review。这样一轮任务内部完成“看全册 → 合并修复 → 重渲 → 复验 → 按最终成品同步讲稿”，不会产生 `review_r2/r3` 轨迹。

### 阶段四 —— 收尾

**第 0 步:最终门槛自检(收尾前必过,不达标就继续重派,绝不只凭规划 / 描述收尾)。**

- **每页都成功产出**:规划的每页都有 `slides/slide_NN.html` + `renders/slide_NN.png`(非空、非破图)。**slide 只认这个机检门**——渲染成功即算过,**即便该 slide 子代理撞 max_turns / 没干净收尾也不重派**(harness 已放宽:slide 机检干净即通过);
- **非 slide 子代理须干净收尾**:research / image / material——把每个返回过一遍,凡 `status=="issues"`/超时/截断/异常收尾的带 note 重派 1–2 次直到干净收尾；slide 除外(见上,只认机检)。**Review 必须是唯一的 `review` 实例,返回 `ready` 且明确 `speech_aligned: yes`；失败 / 超时 / `blocked` 直接阻止交付,不重派成任何 `review_rN`。**
- **image 失败也别冒泡**:某张图始终没出时,不是留着失败状态——而是按阶段一·8 兜底改该页版式为不依赖图的布局(删 `image:` 行 + 改 CSS / SVG),让相关子代理干净收尾;
- 任一条不达标,回去重派,别收尾。

**第 1 步:由 Orchestrator 生成可播放文件 `present.html`(必做,不可省)。** 全套定稿后由 Orchestrator 直接运行,不再委派:

```
python skills/ppt-skill-html/scripts/build_player.py
```

该命令会再次校验字体清单、WOFF2 完整性和 PNG 新鲜度，再扫描 `slides/slide_*.html`(自动跳过 `.bak.`)生成 `present.html`。播放器用 `<iframe>` 隔离页面，并等待页内 `document.fonts.ready` 后才显示，避免字体回退和闪动。Review 已依据最终成品完成最后一次讲稿同步,Orchestrator 不再用规划初稿覆盖它；若此后又修改页面内容,必须重新进入同一个 Review 闭环。**没有最终 `speech.md`、`present.html`、`assets/fonts/manifest.json`，或存在陈旧 PNG，都不算交付完成**;Orchestrator 亲自确认这些产物存在且页数正确。

**最后**以一段简短消息总结这个 deck:页数、生成了哪些文件(含 `present.html` 放映入口、`speech.md`、各页)、复审结论。**并指出 `plan/deck.md` + `plan/slide_NN.md`(逐字大纲与讲稿源)是「可编辑大纲源」**——HTML 成品不可直接编辑,用户要改内容就从这套规划入手(改完重新委派对应页并重跑 `sync_speech.py`);`present.html` 浏览器打开即播。
