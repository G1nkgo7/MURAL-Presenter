# Markdown 规划契约

唯一规划源是可读 Markdown：

```text
plan/deck.md
plan/slide_01.md ... plan/slide_NN.md
```

不再维护整册 JSON/YAML 页表。全局决定写在 `deck.md`，本页证据写在对应逐页计划。

## `plan/deck.md`

````markdown
# Deck Plan
- title: 面向听众的 Deck 标题
- footer: 可选的短页脚

## Resolved deck brief
- language: zh
- page_count: 24
- audience: 主要听众与决策者
- image_mode: 混合；真实对象优先检索，机制关系使用代码视觉
- rationale: 根据原始请求作出上述判断的一行理由。

## 受众与目标
谁在听、谁在讲、讲完希望发生什么。

## 叙事与页面地图
论证、章节、页面职责/标题、前后衔接、节奏、峰值页，以及每张叙事页的主要证据模式
或视觉事件。

## 视觉故事板
按页或连续页段写清主要视觉事件：真实/生成图片、数据图、机制图、文件证据、强排印
或有意识的停顿。检查封面、章节转折和内容峰值是否各有可执行支点，避免视觉计划在
逐页拆分时全部退化为文字面板。

## 视觉契约
配色命题及来源线索、主导视觉性格，以及属于本主题的色彩角色、字体角色、网格、
图片处理、内容画布、页族语法与反默认项。

## 特殊页
封面 Hero；过渡页编号/母题/字体/空间系统；结尾策略。

## 主题变量
下列值只演示一种有彩色路线，不是可复用默认值；必须根据本册配色命题重新选择。

```css
:root {
  --content-canvas: #d9c7f3;
  --surface: #f3a6b8;
  --ink: #241532;
  --muted: #66536f;
  --accent: #e94f37;
  --accent-2: #007f73;
  --special-bg: #492a7a;
  --special-ink: #fff2a8;
  --font-display: var(--font-heavy);
  --font-body: var(--font-sans);
}
```
````

`Resolved deck brief` 是内部任务解析：用户明确要求优先，只对缺失项根据请求的主要
语言、受众、复杂度与交付场景作判断。`image_mode` 是简短策略描述，不是固定枚举；
逐页视觉需求仍需更具体。说明版语言永远不决定 `language`。

Theme Tokens 只包含 CSS 自定义变量。脚本把它合入已准备的 `base.css`；Orchestrator
不写结构 CSS。实际颜色必须为当前 Deck 主动选择；不能照抄示例，也不能因为基础样式
已经存在，就直接沿用其中的中性兜底配色。

## `plan/slide_NN.md`

即使内容是中文，也固定使用以下英文 key：

```markdown
# slide_18
- role: 解释适老化硬件系统
- page_type: process
- page_family: arch-diagram
- composition: data-focus
- visual_evidence: code_only
- canvas_variant: base
- show_footer: true

## 叙事
本页必须让听众理解什么，以及如何承接前后页。

## 证据
- 事实或论点，并注明边界/假设。
- 适用时保留完整来源 URL 与日期。

## 屏显文案（定版）
- eyebrow: SYSTEM
- title: 一条有听众逻辑的标题
- subtitle: 可选的解释性副标题
- section_index:

## 语义视觉需求
需要看见的关系或对象，以及为什么重要。写真实性和标题安全区，不写组件树或精确坐标。

## 构图蓝图
- focal: 第一眼应该看见的证据、对象、数字或结论。
- reading_path: 听众从主焦点到结论、再到支撑信息的阅读顺序。
- primary: 骨架主区域承载什么。
- secondary: 骨架辅助区域承载什么；不能重复标题。

## 渲染锚点
只写真正影响连续性的自包含锚点；写清共享语义或 token，不要写装饰配方，也不要让
Slide 读取或“沿用 Pxx”。

## 约束
只写本页特有的硬要求或禁用论点。

## 讲稿节拍
讲者解释、边界与转场。
```

必需 metadata：

- `role`；
- 小写 kebab token 的 `page_type`；
- 小写 kebab token 的 `page_family`；
- `visual_evidence`：`required`、`preferred`、`code_only` 或 `none`。

普通内容页应同时填写 `composition` 与构图蓝图。若漏填，校验只告警，scaffold 会按
`page_type / page_family` 推断安全起点，不阻断整册；显式规划仍更好，因为能保留原定
主焦点与阅读路径。`composition` 从以下可改写骨架中选择：

- `visual-split`：主视觉/证据与解释分屏，可用 `.is-reversed` 翻转；
- `data-focus`：大图表、地图或机制图 + 窄注释区；
- `comparison`：两个真正可比的同级区域；
- `sequence`：时间线、流程或路径为主，保留支撑结论；
- `matrix`：表格、矩阵或清单为主，保留解释性结论；
- `editorial`：强观点或叙事文本与证据形成非对称关系；
- `freeform`：只有确有清楚且不同于以上骨架的构图动作时使用。

脚本只生成区域骨架和默认比例，不生成组件样式。Slide 可以反转、重叠、改变比例和
表面处理；选择骨架是为了保证首稿有主阅读路径，不是让所有页面像同一模板。

`special_layout` 只用于特殊页，并且特殊页必须填写：

- cover：`split`、`centered`、`lower-third` 或 `poster`；
- section-divider：`number-copy`、`centered`、`visual-split`、
  `vertical-rail` 或 `banded`；
- closing：`centered`、`split` 或 `lower-third`。

`special_layout` 只选择稳定的结构页头网格；`page_family` 仍是自由的主题艺术方向。
例如过渡页可以同时使用 `special_layout: visual-split` 与
`page_family: archive-collision`。

`canvas_variant` 默认 `base`，只有整页色场有明确目的时才改。`show_footer` 在内容页
默认 true、特殊页默认 false。

`title` 必填，`eyebrow` / `subtitle` 可选。`section-divider` 的 `section_index`
只允许数字（如 `02`），不能写 `CHAPTER 02`。封面、过渡、结尾页型分别为
`cover`、`section-divider`、`closing`；其他 page_type 都属于普通内容页。

实质内容页需要 Evidence/证据章节。保留 `speech.md` 所需的决定性事实、边界和来源
URL，但不粘贴长篇来源原文，也不重复全册视觉契约；Slide 不把原始来源放到画布。
逐页计划定结论和证据，不定完整最终正文与布局。原始请求简短时，每页仍应补入让它
值得存在的具体事实、案例、对比、机制或影响，不能用通用要点把计划形式填满。

## 命令

```bash
python skills/mural-presenter-v0-2-zh/scripts/deck.py validate-plans . --expected N
python skills/mural-presenter-v0-2-zh/scripts/deck.py scaffold-from-plans . --expected N
python skills/mural-presenter-v0-2-zh/scripts/deck.py sync-speech . --expected N
```

`scaffold-from-plans` 默认只写缺失 HTML，不覆盖已完成页面。只有显式 `--force` 才可
覆盖；Slide 开始后不要使用 `--force`。
