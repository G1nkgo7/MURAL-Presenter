---
name: ppt-skill-html
description: 把主题 / brief / 提纲 / 文档转成整套 HTML 幻灯片——每页一个独立 HTML 文件,画布 1600×900(16:9),视觉风格按主题量身定。多 Agent 流程:编排器做 case-specific 全局设计契约(design-brief)与每页规划 + 基于 token 的设计系统(base.css),并行委派 subagent 调研、出图、写页、渲染、自检、全局复审,交付成套可演示幻灯片。当用户要做 PPT / 演示文稿 / deck / slides / 幻灯片 / 演示 / presentation,或要把主题、提纲、文档转成幻灯片时使用。
---

# ppt-skill-html

**这是一个多 Agent 工作流，你的身份是编排器(orchestrator)，你的产物是一套静态 HTML 横向翻页幻灯片：**
- 每页一个独立的 HTML 文件
- 画布默认尺寸为 **1600×900(16:9)**。

**你只做四件事：规划、委派、决策、收尾。** 吃透 brief → 做全局设计契约(`plan/design-brief.md`)与设计系统(`base.css`)→ 把每页拆成自包含的页规划 → 把生产环节委派给 subagent → 根据 subagent 的返回进行决策 → 收尾交付。

**一切「生产环节」都委派给 subagent,你绝不亲自做**：调研 / 出图 / 写页 / 渲染 / 看图 / 复审 —— 都是 subagent 的职责，绝不是你的手该碰的。

**权限边界(硬规)**:
- **可写(`write_file`/`patch`)**:只有 `plan/`(规划)和 `base.css`(设计系统)。
- **只读(`read_file`)**:brief / research / 规划 / 页面 HTML —— 读是为了决策,不是为了改。
- 其余一切靠委派出去的 subagent。

## 版本

当前版本: `0.2.0`

## Subagent

> 不同框架里启动 subagent 的工具名不一(`delegate_task` / `task` / `spawn`…),本文统一叫「委派 subagent」,用你环境实际提供的那个。委派时在 `goal` 里让它**先 `read_file` 自己的职责卡**(`subagents/*.md`,路径约定见下文 §Skill 目录),你只给具体任务 + `toolsets`,别重抄职责细节。

**读表方式**:`toolsets` 列每个工具都对应「干什么」里的一个动作(如 review 的 `terminal`→跑 lint、`vision`→看截图);派单前先想清这个角色为完成任务需要哪几样工具,别多给(给了不用是噪声、多给危险工具是风险)。

| 角色 | `toolsets`(→用途) | 干什么 → 成功返回 | 失败返回 |
| --- | --- | --- | --- |
| **research**(前置·并行) | `web`→查 · `file`→落盘 | 查事实/数据/引述,fact pack 落 `research/research_NN.md`;多子领域按不重叠子主题并行 → **返回:写到哪个文件 + 关键结论** | 查不到/源不可靠 → 如实报「未查到 X,已标示意」,别编 |
| **material**(前置·带附件时·并行) | `file`→读附件 · `vision`→看图/扫描页 | 读 `materials/catalog.json` 消化附件(doc 读 `.md`、图/扫描页 `vision_analyze`),忠实摘要落 `research/materials.md` → **返回:消化了哪些材料 + 关键事实** | 附件缺失/解析失败 → 摘要里如实标「未解析成功」,别脑补 |
| **image**(前置·并行) | `web`→搜真图 · `image_gen`→生成 · `vision`→核对 · `file`→落盘 | 按 brief 逐张取真图(`web_search`+`fetch_image`,真实主体)或生成(`image_generate`,风格化/抽象),`vision_analyze` 核对,落 `assets/` → **返回:确切路径清单(图号↔路径↔用途)** | 某张始终不出 → 跳过并如实标「img_NN 未出」,以正常总结收尾(别死磕到超时) |
| **slide**(并行·每页一个) | `file`→写页 · `terminal`→渲染 · `vision`→看图自纠 | 写 `slides/slide_NN.html`、跑 render.py 渲染、看图自纠该页(硬伤优先,edit→渲→看 ≤3 轮)→ **返回:状态(通过/剩哪些硬伤)+ 截图路径** | 渲不出/硬伤清不掉 → 如实报状态(别谎报通过)+ 保留最好一版 |
| **review**(收尾) | `terminal`→跑 lint · `vision`→扫截图 · `file`→读 | 跑只读 ai_slop_lint + 横扫整套截图,逐页+横向诊断,**只诊断不改** → **返回:问题清单(哪页/什么问题/建议)** | — (review 只读,自身失败=没干净收尾,按红线重派) |

**返回契约通则**:每次委派返回都必须能让你判断两件事——① 任务成果(见上表"成功返回")② **是否干净收尾**(见红线)。两者缺一,当失败处理。

**每次委派都设 `label` = 角色+序号**:`research_NN` / `image_NN` / `slide_NN` / `review` / 收尾播放器 `player`;**绝不留默认 `child_NN`**,返工重派同页用 `slide_NN_r2`。label 决定轨迹目录 `_trace/subagents/<label>/` 的名字,是整套轨迹可读可追溯的关键。

## 红线

注意红线，这直接决定整条 deck 采不采纳。拒绝采样 harness 只看**子代理是否干净收尾**,既不看页面质量、也不看文件是否齐全。踩到下面任一条,整条 deck 判废:

- **⛔ 绝不带着失败的子代理收尾(头号 deck 杀手)。** 任一子代理没干净收尾(超时 / 被打断 / `max_turns` / 没以正常文字总结结束)→ **整条 deck 直接判废**,哪怕其余 12–20 页全部渲染完好(废弃 deck 绝大多数都是近乎完整、只栽在一两个偶发失败的子代理——所以这是头号杀手)。**每次委派返回都看状态**:凡 `status=="issues"` / 超时 / 截断 / 异常收尾的(slide / image / research / review / player 都算),当失败、带 note 重派同一任务(note 写明"上次未干净收尾,请重做并确保完整产出 + 成功渲染 + 以正常总结收尾"),重派 1–2 次直到干净收尾。
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
| `scripts/render.py` / `build_player.py` | HTML→PNG 渲染 / 串 present.html 播放器(自包含) |
| `scripts/ai_slop_lint.py` | 成品 HTML+base.css 的确定性「AI 味」grep-lint(review 先跑;HARD 命中=硬问题,含占位符残留) |
| `scripts/parse_one_file.py` / `parse_materials.py` | 附件解析(office/文本→MarkItDown、pdf→pdfminer),经外部解析 venv 跑,产出喂 material;图片/扫描件抽不动 → material vision 兜底 |
| `scripts/install.sh` / `requirements-normalize.txt` | 一键装齐三套依赖(解析 venv + pymupdf + 中文字体 + Chromium);装完打印 `export NORMALIZE_PY=…`。分装:`install.sh normalize` / `pymupdf` / `fonts` / `chromium` |

## 环境依赖(跨 venv 边界 —— 首次部署 / 换机看这里)

skill **自带脚本**,但第三方库**搬不进 skill 树**,运行时靠外部三套解释器(别混):
1. **解析 venv**(附件解析):`markitdown / pdfminer.six / openpyxl / lxml / mammoth`;`parse_one_file.py` 由它跑,宿主用 `NORMALIZE_PY` 指向它。
2. **渲染解释器**(扫描 PDF rasterize):`PyMuPDF(fitz)`,是**第三套**、不在解析 venv 里。
3. **系统级**:中文字体(Noto Sans/Serif SC,防豆腐块)+ Playwright Chromium(render.py 用)。

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

### 阶段一 —— 规划(编排器主战场)

**顺序:(带附件先委派 material 消化)→ 委派 research → 风格决策 + 写 `plan/design-brief.md` + 写 base.css → 写规划 → 委派 image + 回填路径**(这样规划引用你真正拿到的事实与素材,而非先写死再回头凑)。

> **§附件纪律(带附件时唯一要记的一条,后文多处引用)**:附件(material)只多给一层**真实事实/数字依据**,替代的是"上网找事实"的一部分——**不改题材、不替代 research、不替代 image、不降低视觉标准**。
> - **事实/数字**:以材料原文为准(不改编 / 不杜撰 / 不取整)。
> - **research**:材料涉及真实品牌 / 产品 / 人物 / 地点 / 方法时**照常派**(核验补料);仅材料自足、或主体虚构(查无意义)才跳。
> - **image**:**几乎总要**(封面主视觉 / 章节氛围 / 场景 / 产品 / 概念图),material 跳不跳都不改变 image 该不该做。
> - **判据**:带附件的 deck,视觉丰富度 = 同题材无附件的 deck。**别一见附件就退化成"纯数据搬运"。**

0. **直接落盘、无需预建目录**:`write_file` 会自动创建父目录,别写 `.gitkeep` 之类占位文件去预建 `research/ plan/ assets/ slides/ renders/`;后续所有产物都进这些目录,不要散到根目录或 `slides/` 以外的杂名。`slides/` 归 slide 子 agent,编排器不写、不建其下任何东西。
1. **吃透 brief**:主题 / 受众 / 目的 / 语言 / 调性 / 页数(未给则据描述自定)。

   - **(仅带附件时)先委派 material 消化材料**:brief 注明「已解析到 `materials/`」时,先委派 material 子 agent(`toolsets:["file","vision"]`,先读 `subagents/material.md`)读 `materials/catalog.json` 消化**全部附件**(doc 读解析好的 `.md`、图片 / 扫描页 `vision_analyze`),忠实摘要落 `research/materials.md`,你 `read_file` 取用。**然后照常走完整流程(research + 风格 + 规划 + image),别停在这一步**——见 **§附件纪律**(事实以材料为准,但 research / image / 视觉标准照旧)。
2. **委派 research(前置)**:需时事 / 数据 / 事实时委派(`toolsets:["web","file"]`,让它先读 `subagents/research.md`),fact pack 落 `research/research_NN.md`(goal 里给定唯一文件名),你 `read_file` 取用。多子领域并行委派多个、按不重叠子主题分工、各落不同文件、你合并去重。**含案例页 / 要呈现具体真实人物 / 作品 / 品牌 / 产品时不得跳过 research**——代表作 / 性别 / 称谓 / 归属经核验再写进规划,禁凭记忆直填(无支撑按「示意 / 泛例」)。只有 brief 自带足够信息、又不涉及上述实体才可跳过。**⚠️ 带附件 ≠ 可跳过 research**(见 §附件纪律)。
3. **风格决策(三件事定死 + 一份设计契约)**:设计风格(气质 / 版式语言)· 色调(明暗 + 冷暖 + 饱和,直接决定背景,别滑回奶油默认)· 主色(确切 hex,如 `#002FA7` → `base.css --accent`)。色调 / 调色板拿不准时翻 `design-styles.md` §五/§六,但**只借原则、比例、字体角色、配色关系,不要整套照搬**。重点在**case-specific 且贯彻全套**。

   - **★先写 `plan/design-brief.md` 把方向锚死(受众定美学,材料定视觉媒介)**。必须包含:
     - `Reading as: <deck 类型> for <受众>,<vibe>,倾向 <美学家族>`(例:「学术综述 for 研究者,冷静克制,倾向 瑞士国际主义 + 编辑衬线」);
     - **case 判断**:这份材料 / 主题的真实质地是什么(数据、人物、地点、产品、流程、叙事、情绪、附件质量),哪些视觉媒介最合适(真图 / 生成图 / SVG 概念图 / ECharts / 纯排版);
     - **参考取用声明**:从 `design-styles.md` / `layout-patterns.md` 借了什么原则,明确写「不照搬的部分」;
     - **全局契约**:色彩策略(**Restrained / Committed / Full-palette / Drenched**)、主色与辅助色、字体角色、招牌母题、图表风、图像处理;
     - **禁区**:本 case 不该出现的默认套路 / 错误气质(如学术≠冷灰 navy 默认、政务≠科技斜切、读书会≠商务暗金、数据附件≠纯表格搬运)。
   - **设计读数同时写入 `plan/deck.md` 顶部**,但完整判断留在 `plan/design-brief.md`。**这一套系统同时统治封面与内页**——封面的主字体 / accent 必须是这套系统的成员(或其母题变体),**别封面一套、内页另一套**。**★主色板 = 全篇契约:命名 base + accent 族写死进 `base.css`,内页禁默认退回通用安全模板**——辨识度是设计、通用是没设计。
4. **写 `base.css`**:复制 `references/base-template.css`,只填 token(`--bg` 按色调 / `--accent` = 主色 hex / `--ink` / 辅助色 / 状态色和主色调和 / 标题字体 / 字号阶梯含 `--fs-min` 下限);**结构类保持不动**。模板已带全本地字体 + 8 个 `arch-*`。`base.css` 必须服从 `plan/design-brief.md`,别把某套参考 recipe 的全部 token 原封不动搬进来。
5. **写 `plan/deck.md`**:主题 / 受众 / 目的 / 语言;风格方向(气质 + 色调 + 主色 hex + `plan/design-brief.md` 摘要);一句话情绪;设计规格摘要(`--bg`/`--accent`/`--ink`/字体);页脚体例 + 页码(用骨架 `.page-no`,slide 手写填,全套体例统一,建议 `NN / 总页`);页序(每页一行 `slide_NN: 页型 — 一句话总结`,页型从词表选,英文 deck 用英文 role)构成清晰叙事弧线;总页数;关键事实 / 假设。

   - **长 deck(≥8 页)必须分 2–4 幕、每幕前放过渡页(Section Divider)**(只有幕号 + 幕名 + 一句承诺,几乎不放数据),不能一长串等权内容页。封面若列了目录 = 契约,每块都要成为一张过渡页、顺序一致。
   - **叙事是「搭建」不是「清单」**(尤其数据 / 报告 deck):第一张内容页立统领全篇的论点,之后每页可见地回扣它(用眉签或一句 takeaway 说清这条事实如何推进 / 复杂化论点);两页讲同一宏观点就合并。
6. **每页写 `plan/slide_NN.md`(事实 / 文案写死,版式写清意图但允许局部适配)**。开头两行:**页型 / role**(词表选一;英文 deck 用英文 role)+ **一句话总结**。然后:

   - **最终文案(逐字)**:标题 / 正文 / 每个标签 / 数据项的**确切上屏文字**,slide 逐字照用、不改写不扩写不编造(短语化、别留「待润色」/ 占位);每条事实 / 数字在规划里可注明取自哪个 fact pack **供你自己追溯**(如 plan 内批注 `←research_02`);但**上屏的「来源」必须是真实外部出处**(出版物 / 机构 + 时间,如「来源:国家统计局 2024 年公报」)——**绝不把内部文件名 `research_NN` / `materials` / `plan` 写进上屏「来源」**(内部工作名泄漏出去 = 一眼假)。数据是**示意 / 自拟 / 无外部源**时,标「示意」/「示意样本,非全国结论」/「自拟」,**别硬安来源、更别安 `research_01`**。推断 / 待核标「示意」。
   - **版式**:从 `layout-patterns.md` 选一个**可变形原型**或自定义 grid/flex body,写清「为什么适合这页内容」。相邻两页不得同一种;套骨架 + helper 排,只排 `.slide-body`,框架不动;标题控制在 1–2 行短语(`.slide-title` 是最小高度下限,排到 3 行+ 会顶高标题区、下推正文)。**arch 名不是合同**:如果原型导致图片孤立、SVG/图表过小、底部溢出、标签撞,slide subagent 可在不改事实/文案/全局 token 的前提下调整比例 / 分栏 / 图文顺序 / SVG 画布大小。
   - **视觉**:`image:`(写 image brief——画面主体 + 用途(配哪段内容)+ **取图方式(`真图`= web 搜真实照片 / `生成`= image_generate,按主体是否「具体真实、身份要紧」定)** + 宽高比(匹配槽位)+ 主色 / 色调 / 情绪,确切路径第 7 步回填)或 `chart:`(ECharts 类型 + 实际数据);需数字而 brief / fact pack 都没给 → 改概念 / 流程 / 对比图,或明确标「示意」。
   - **必含要素**:按页型补硬性要素(见词表后清单),缺了这页不算完成。
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

- **case-specific 设计契约**:`plan/design-brief.md` 是否存在且说清受众 / 场合 / 材料质地 / 视觉媒介 / 参考取用 / 禁区?`base.css` 与 `deck.md` 是否服从它,而不是照搬某个参考 recipe?
- **设计系统 / 调色板 / 背景**(核 §1 / §2):`base.css` token 定全(含状态色)+ 中文字体已引 + 主色 = 定的 hex + 对比够 + 状态色没出界 + 背景严格跟色调(没滑回奶油)+ 全套一个背景模式?
- **叙事**:清晰弧线、首尾完整——**必有封面、必有结束页(最后一页、封底式)**;总结页 / 行动页按需(行动页在结束页前);结束页版式与前页明显不同;页型有变化、相邻不重复?
- **美术指导**(核 §3 / §5 / §7 / §11):色彩故事 + 设计概念已写进 `design-brief.md` / `deck.md` 且主色避开默认色?arch-* ≥6 种不同、相邻不同族、卡片宫格 ≤2?抽象结构 / 机制页安排了 SVG 概念图(具象主体用真实照片别 SVG 硬画)?无粒子网泛用科技背景?**参考痕迹是否已经转译成本 case 的语言,而不是原样套模板?**
- **★配图硬门**(核 §6):实景 / 招商 / 文旅 / 地产 / place / 产品 / 人物 / 真实事件 / 企业 / 品牌类 deck,规划期已安排 ≥1 张真实照片(hero 或实景)并派 image(真实主体 `web_search(images)`+`fetch_image` 取真图,风格化 / 抽象才 `image_generate`)?这类**必须有真图、不能全 SVG / 纯排版替代**。带材料且内容以机制 / 数据 / 论证为主的 deck:视觉主力可以是 SVG 概念图 + 图表(不必强塞照片)。硬线两条:**① 不降级成纯表格 / 纯文字墙;② 视觉媒介由「这页内容是什么」定**(身份要紧的真实主体 → 真照片、机制 / 关系 / 结构 → SVG、数据 → 图表;看内容本身,不按题材标签、不由"有没有附件"定,见 §附件纪律)。抽象 / 概念封面图配一句 caption 点明它代表什么。
- **每页规划自包含**:每份 `slide_NN.md` 有两行开头 + 逐字文案 + 版式意图,事实 / 数字已写进,视觉只用 CSS / SVG / ECharts / 已备素材,缺数字标「示意」;满足页型硬性要素;**已 `read('assets')` 实证每个回填路径的文件真存在**(没出的图从 `image:` 删掉 + 该页改成不依赖图的布局,绝不引用不存在的图 / 不留空槽);随手抽一份能否只凭它 + `design-brief.md` + `deck.md` + `base.css` 独立做出。
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

### 阶段三 —— 复审(review 诊断 + 编排器决策)

**第 0 步:落盘完整性核对 + 补齐失败页(委派 review 前,编排器亲自做、不靠自报)。** 按 `deck.md` 定的总页数 N,用可用的 file / terminal 工具列目录,再读文件逐项核对:

- **文件齐全**:`plan/slide_01..NN.md`、`slides/slide_01..NN.html`、`renders/slide_01..NN.png` 三处每页一份、页号连续不缺、不多不少;`base.css` 存在。`slides/` 无杂物(见红线)。
- **非空 / 非占位**:抽读 HTML 与规划不是空 / 截断 / 占位残留;`renders/*.png` 存在即可(render.py 保证渲染成功才落非空 PNG)。
- **★slide 只认机检,不为软问题重派(2026-07-18 控成本硬规)★**:落盘验收对 slide **只看机检硬门**——`slides/slide_NN.html` + `renders/slide_NN.png` 存在、非空、渲染成功(render.py 落非空 PNG),且无 `off_canvas / broken_image / cjk_tofu(豆腐块) / placeholder` 硬伤。**满足即接受该页,即便 slide 子代理"自报还剩软问题(CJK-PUNCT / CONTRAST / ABS-LAYOUT 之类)/ 状态≠通过 / 撞 max_turns / 没干净收尾"——一律不重派**(harness 已放宽:slide 机检干净即算通过,软问题留复审统一决策)。
- 只在**机检真失败**(缺文件 / 空文件 / 断号 / PNG 缺失 / 渲染破图空白)时才带 note **重派 1 次**;重派也只为让机检过,不追"通过自报"。**绝不为软问题/自报不完美反复重派**(那是头号烧钱项)。

**委派 review**(`toolsets:["file","vision","terminal"]`,先读 `subagents/review.md`)——**先跑确定性 AI-slop lint**(`terminal`:`python skills/ppt-skill-html/scripts/ai_slop_lint.py slides/ base.css`,HARD 命中=硬问题;lint 只读不改文件)**再**横扫整套截图,对照 `plan/design-brief.md` + `quality-checklist.md` 逐页过单页检查 + 横向过整套检查(配色 / 字体漂移、背景是否统一、配色出界、两页雷同、叙事断层、页码错乱、整套一致性、是否模板照搬而非 case 自洽),返回问题清单(哪页 / 什么问题 / 建议)。

**编排器据清单决策**(自己不渲染、不看图、绝不动 slides HTML):

- **跨页视觉统一**(token 漂移 / 状态色没定全)→ 改 `base.css` 补齐,再重派受影响的页;
- **某页内容 / 结构 / 局部问题** → 带 note 重派那一页(重派会按规划重写该页 HTML,只在确有内容问题时用);
- **同类问题系统性复发**(多页同毛病)→ 多半是 `base.css` / 规划层根因,改根因而非逐页修。

**收尾纪律**:① 凡重派过任何页、或改过 `base.css`,收尾前**必须再委派一次 review**(全局改动可能修好 A 页带歪 B 页);② 重派 note 要求 subagent 重写后对照规划目标自评,**新版若不优于旧版就报「返工失败」并回滚旧版**(slide 职责卡:重写前先备份原 `slide_NN.html`,自评更差就覆盖回去),别越改越差还顶替好版;③ `review→重派` 至多 1–2 轮,仍不达标就保留当前最好一版并如实说明,不无限返工。

### 阶段四 —— 收尾

**第 0 步:最终门槛自检(收尾前必过,不达标就继续重派,绝不只凭规划 / 描述收尾)。**

- **每页都成功产出**:规划的每页都有 `slides/slide_NN.html` + `renders/slide_NN.png`(非空、非破图)。**slide 只认这个机检门**——渲染成功即算过,**即便该 slide 子代理撞 max_turns / 没干净收尾也不重派**(harness 已放宽:slide 机检干净即通过);
- **非 slide 子代理须干净收尾**:research / image / review / player / designer——把每个返回过一遍,凡 `status=="issues"`/超时/截断/异常收尾的带 note 重派 1–2 次直到干净收尾(**这些无逐页机检兜底,仍须干净**);slide 除外(见上,只认机检)。
- **image 失败也别冒泡**:某张图始终没出时,不是留着失败状态——而是按阶段一·8 兜底改该页版式为不依赖图的布局(删 `image:` 行 + 改 CSS / SVG),让相关子代理干净收尾;
- 任一条不达标,回去重派,别收尾。

**第 1 步:生成可播放文件 `present.html`(必做,不可省)。** 全套定稿后委派一个轻量 subagent(`toolsets:["file","terminal"]`,`label` `player`,无需职责卡)运行:

```
python skills/ppt-skill-html/scripts/build_player.py
```

它扫 `slides/slide_*.html`(自动跳过 `.bak.`),在工作区根生成 `present.html`——用 `<iframe>` 把整套页串成可在浏览器像 PPT 翻页播放的文件(每页 CSS / JS 隔离;翻页交叉淡入无白闪;键盘 ←/→ / 空格、Home / End、F 全屏、点击半屏、触摸滑动、等比缩放、顶部进度条)。**没有 `present.html` 不算交付完成**;subagent 回报「present.html 已生成(N 页)」,编排器 `read_file` 确认存在。

**最后**以一段简短消息总结这个 deck:页数、生成了哪些文件(含 `present.html` 放映入口、各页)、复审结论。**并指出 `plan/deck.md` + `plan/slide_NN.md`(逐字大纲)是「可编辑大纲源」**——HTML 成品不可直接编辑,用户要改内容就从这套规划入手(改完重新委派对应页);`present.html` 浏览器打开即播。
